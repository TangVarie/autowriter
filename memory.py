"""
Feedback Memory System for XHS Content Workstation.

Responsibilities:
  1. Assemble the full system prompt (base + global memories + project memories).
  2. Analyse raw user feedback and classify it as project or global memory.
  3. Persist new memory candidates (or increment existing ones) via db.py.
  4. Provide a Streamlit management UI for viewing/editing memories.
"""

from __future__ import annotations

import io
import json
from typing import Optional

import anthropic
import streamlit as st
from supabase import Client

import config
import db


# ── Prompt assembly ────────────────────────────────────────────────────────

def build_system_prompt(
    base_prompt: str,
    global_memories: list[dict],
    project_memories: list[dict],
    tactic_suffix: str = "",
    calibration_notes: str = "",
    positive_examples: Optional[list[dict]] = None,
    negative_examples: Optional[list[dict]] = None,
    session_instructions: Optional[list[dict]] = None,
) -> str:
    """
    Assemble the final system prompt.

    Assembly order (lowest priority first, highest priority last — so the most
    authoritative instructions are the last thing the model reads):
      1. Base system prompt (tactical framework)
      2. Tactic-specific suffix (if any)
      3. Account-level memories (``scope='global'`` — per user, across projects)
      4. Project-level memories
      5. Calibration notes (qualitative observations, not rules)
      6. Positive examples (few-shot: what good looks like)
      7. Negative examples (few-shot: what to avoid)
      8. Session-level instructions — ad-hoc rules from the current conversation;
         the highest priority tier, supersedes any lower-priority memory
    """
    parts: list[str] = [base_prompt.strip()]

    if tactic_suffix.strip():
        parts.append(f"\n{tactic_suffix.strip()}")

    if global_memories:
        bullets = "\n".join(f"• {m['content']}" for m in global_memories)
        parts.append(f"\n---通用记忆（必须执行，每条都要主动检查）---\n{bullets}")

    if project_memories:
        bullets = "\n".join(f"• {m['content']}" for m in project_memories)
        parts.append(f"\n---项目记忆（必须执行，每条都要主动检查）---\n{bullets}")

    if calibration_notes and calibration_notes.strip():
        parts.append(f"\n---调校笔记（理解并内化这些审美偏好，生成内容时主动应用）---\n{calibration_notes.strip()}")

    if positive_examples:
        ex_blocks = []
        for ex in positive_examples[:5]:
            body_preview = (ex.get("body") or "")[:200].split("\n")[0]
            ex_blocks.append(f"标题：{ex['title']}\n正文节选：{body_preview}")
        parts.append(
            "\n---优质正案例（学习这些文案的风格、结构和切入角度，这是我们想要的方向）---\n"
            + "\n\n".join(ex_blocks)
        )

    if negative_examples:
        ex_blocks = []
        for ex in negative_examples[:3]:
            body_preview = (ex.get("body") or "")[:120].split("\n")[0]
            ex_blocks.append(f"标题：{ex['title']}\n正文节选：{body_preview}")
        parts.append(
            "\n---反面案例（分析这些文案存在的问题，生成时主动规避）---\n"
            + "\n\n".join(ex_blocks)
        )

    if session_instructions:
        bullets = "\n".join(f"• {m['content']}" for m in session_instructions if m.get("content"))
        if bullets:
            parts.append(
                "\n---当前会话临时指令（本轮最高优先级，高于项目记忆与通用记忆）---\n"
                "这些是用户在本次对话中刚刚提出的要求；本轮所有产出必须严格遵守，直到用户改口。\n"
                + bullets
            )

    return "\n".join(parts)


def record_session_instruction(
    db_client: Client,
    user_id: str,
    text: str,
    project_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    ttl_hours: int = 24,
) -> Optional[dict]:
    """
    Low-level helper: write a session-level memory directly, bypassing the
    merger.  Kept for imports that may still reference it; prefer
    :func:`ingest_user_instruction` for all new call sites.
    """
    clean = (text or "").strip()
    if not clean:
        return None
    return db.insert_session_instruction(
        db_client,
        user_id=user_id,
        content=clean,
        source_feedback=clean,
        project_id=project_id,
        source_batch_id=batch_id,
        ttl_hours=ttl_hours,
    )


# ── AI-driven merger: classify a user instruction and route it ───────────

_MERGER_SYSTEM = """你是一个内容运营的记忆整理助手。你要帮用户把一条刚输入的反馈/指令，分流到最合适的记忆层。

四种动作，严格四选一：
1. "merge" —— 新反馈和现有某条硬规则属于同一件事（即使措辞不同），把它合并到那条。输出该条的 id。
2. "rule"  —— 一条全新的硬性规则，可精确执行（例："标题不要带数字"、"RIO 不用'微醉'"）。
3. "taste" —— 审美偏好、感受性观察，不是可精确执行的规则（例："喜欢有温度的收尾"、"不要太说教口吻"）。这类走调教笔记。
4. "session" —— 仅适用于本次或近期生成的临时上下文（例："本次突出柑橘口味"、"这批主打周五场景"），或实验性试探（"试试数字开头"）。不值得永久保留。

判定提示：
- 一般性 / 未来也成立 → rule
- 长期审美但难量化 → taste
- 仅本次有效 / 一次性试探 → session
- 已有同义硬规则 → merge

若是 rule：判断 scope。与某个品牌/产品强相关 → "project"；对所有小红书文案普遍成立 → "global"。
若是 taste：把观察浓缩成一句，方便追加进调教笔记。
所有 content 字段都要规范化成 ≤ 50 字的中文短句。

严格输出 JSON，无其他文字：
{
  "action": "merge" | "rule" | "taste" | "session",
  "target_id": "<仅 merge 时有；现有记忆的 id>",
  "scope": "project" | "global",    // 仅 rule 时有
  "content": "<规范化后的短句>",
  "reason": "<不超过 20 字的判定依据>"
}"""


def classify_and_merge_feedback(
    db_client: Client,
    user_id: str,
    feedback_text: str,
    project_id: Optional[str] = None,
    project_name: str = "",
) -> dict:
    """
    Ask Claude to route the feedback into one of four buckets (merge / rule /
    taste / session).  Returns the parsed JSON dict (with sane fallbacks on
    error).  Caller is responsible for applying the routing decision.
    """
    clean = (feedback_text or "").strip()
    if not clean:
        return {"action": "session", "content": "", "reason": "empty"}

    fallback = {
        "action": "session",
        "content": clean[:50],
        "reason": "classifier fallback",
    }
    if not config.ANTHROPIC_API_KEY:
        return fallback

    # Pull existing confirmed rules so the merger can spot semantic duplicates.
    global_mems, project_mems = db.get_confirmed_memories(
        db_client, user_id, project_id=project_id
    )
    existing_lines: list[str] = []
    for m in project_mems[:30]:
        existing_lines.append(f"- [id={m['id']}] (项目) {m['content']}")
    for m in global_mems[:30]:
        existing_lines.append(f"- [id={m['id']}] (通用) {m['content']}")
    existing_block = "\n".join(existing_lines) if existing_lines else "(无)"

    user_msg = (
        (f"[当前项目：{project_name}]\n" if project_name else "")
        + f"现有硬规则（仅供 merge 判定）：\n{existing_block}\n\n"
        + f"用户刚输入：\n{clean}"
    )

    try:
        client_kwargs: dict = {"api_key": config.ANTHROPIC_API_KEY}
        if config.ANTHROPIC_BASE_URL:
            client_kwargs["base_url"] = config.ANTHROPIC_BASE_URL
        client = anthropic.Anthropic(**client_kwargs)
        resp = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=400,
            system=_MERGER_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip()
        # strip ```json fences if the model wrapped them
        import re as _re
        cleaned = _re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
        data = json.loads(cleaned)
    except Exception:
        return fallback

    action = data.get("action")
    if action not in ("merge", "rule", "taste", "session"):
        return fallback
    out: dict = {
        "action": action,
        "content": (data.get("content") or clean)[:80].strip(),
        "reason": (data.get("reason") or "")[:60].strip(),
        "source_feedback": clean,
    }
    if action == "merge":
        out["target_id"] = str(data.get("target_id") or "").strip()
        if not out["target_id"]:
            # merger said merge but gave no id — safer to treat as rule
            out["action"] = "rule"
            out["scope"] = "project"
    if out["action"] == "rule":
        scope = data.get("scope")
        out["scope"] = "global" if scope == "global" else "project"
    return out


def ingest_user_instruction(
    db_client: Client,
    user_id: str,
    feedback_text: str,
    project_id: Optional[str] = None,
    project_name: str = "",
    batch_id: Optional[str] = None,
    session_ttl_hours: int = 24,
) -> dict:
    """
    The one capture pipeline for every user-typed instruction (Quick Generate
    extra_instructions, iteration feedback, manual memory add).  Routes through
    the merger into: merge existing rule / new rule / taste calibration note /
    session memory.  Returns ``{"action": ..., "result": <raw row or note>}``.
    """
    clean = (feedback_text or "").strip()
    if not clean:
        return {"action": "skip", "reason": "empty"}

    # Feature flag — fall back to legacy session-only write if disabled.
    if not getattr(config, "ENABLE_MEMORY_MERGE", True):
        row = record_session_instruction(
            db_client, user_id, clean,
            project_id=project_id, batch_id=batch_id, ttl_hours=session_ttl_hours,
        )
        return {"action": "session", "result": row, "reason": "merger disabled"}

    decision = classify_and_merge_feedback(
        db_client, user_id, clean,
        project_id=project_id, project_name=project_name,
    )
    action = decision["action"]

    if action == "merge":
        try:
            row = db.increment_memory_frequency(db_client, decision["target_id"])
            return {"action": "merge", "result": row, "reason": decision.get("reason", "")}
        except Exception:
            # Fall back to creating a new rule if the target id is stale
            action = "rule"
            decision.setdefault("scope", "project")

    if action == "rule":
        scope = decision.get("scope", "project")
        pid = project_id if scope == "project" else None
        row = db.upsert_memory(
            db_client,
            user_id=user_id,
            scope=scope,
            content=decision["content"],
            source_feedback=clean,
            project_id=pid,
            auto_confirm_threshold=1,  # a single utterance is enough
            force_confirmed=True,
        )
        return {"action": "rule", "result": row, "reason": decision.get("reason", "")}

    if action == "taste":
        note = (decision.get("content") or clean).strip()
        if note and project_id:
            _append_taste_to_calibration(db_client, project_id, note)
        return {"action": "taste", "result": note, "reason": decision.get("reason", "")}

    # session (default)
    row = record_session_instruction(
        db_client, user_id, decision.get("content") or clean,
        project_id=project_id, batch_id=batch_id, ttl_hours=session_ttl_hours,
    )
    return {"action": "session", "result": row, "reason": decision.get("reason", "")}


def _append_taste_to_calibration(
    db_client: Client,
    project_id: str,
    observation: str,
) -> None:
    """Append a single taste observation to the project's calibration notes.

    Keeps the notes bounded (≤ 600 characters total) by deduping near-exact
    matches and trimming the oldest entries once capacity is exceeded.
    """
    try:
        proj = db.get_project(db_client, project_id)
    except Exception:
        return
    if not proj:
        return

    existing = (proj.get("calibration_notes") or "").rstrip()
    line = f"- {observation.lstrip('-• ').strip()}"

    # Skip if the observation already appears (case-insensitive, whitespace-collapsed).
    existing_norm = " ".join(existing.split())
    if observation[:20] and observation[:20] in existing_norm:
        return

    merged = (existing + "\n" + line) if existing else line
    if len(merged) > 800:
        # Drop oldest lines until we fit.  Lines are stored newest-last.
        lines = merged.split("\n")
        while lines and len("\n".join(lines)) > 800:
            lines.pop(0)
        merged = "\n".join(lines)

    try:
        db.update_project(db_client, project_id, {"calibration_notes": merged})
    except Exception:
        pass


# ── AI-based feedback classification ──────────────────────────────────────

_CLASSIFY_SYSTEM = """你是一个内容运营助手，专门负责整理小红书文案反馈记忆。

用户会给你一条反馈意见。你需要：
1. 判断这条反馈属于：
   - "project"（项目记忆）：只与特定品牌/产品相关，例如"RIO不用'微醉'"
   - "global"（通用记忆）：适用于所有小红书文案的通用技巧，例如"标题带数字效果好"
2. 提炼出一条简洁的记忆内容（不超过50字的中文）

以 JSON 格式回复：{"scope": "project"|"global", "content": "记忆内容"}
只返回 JSON，不要有其他文字。"""


def classify_feedback(
    feedback_text: str,
    project_name: str = "",
) -> tuple[str, str]:
    """
    Use Claude to classify a feedback string.
    Returns (scope, content) where scope is 'project' or 'global'.
    Falls back to ('project', feedback_text) on any error.
    """
    if not config.ANTHROPIC_API_KEY:
        return "project", feedback_text

    try:
        client_kwargs: dict = {"api_key": config.ANTHROPIC_API_KEY}
        if config.ANTHROPIC_BASE_URL:
            client_kwargs["base_url"] = config.ANTHROPIC_BASE_URL
        client = anthropic.Anthropic(**client_kwargs)
        user_msg = feedback_text
        if project_name:
            user_msg = f"[当前项目：{project_name}]\n反馈：{feedback_text}"

        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=256,
            system=_CLASSIFY_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = response.content[0].text.strip()
        parsed = json.loads(raw)
        scope = parsed.get("scope", "project")
        content = parsed.get("content", feedback_text)
        if scope not in ("project", "global"):
            scope = "project"
        return scope, content
    except Exception:
        return "project", feedback_text


# ── Feedback ingestion ─────────────────────────────────────────────────────

def ingest_feedback(
    db_client: Client,
    user_id: str,
    feedback_text: str,
    project_id: Optional[str] = None,
    project_name: str = "",
) -> dict:
    """
    Classify feedback and upsert it into the memories table.
    Returns the resulting memory row.
    """
    scope, content = classify_feedback(feedback_text, project_name=project_name)
    pid = project_id if scope == "project" else None
    return db.upsert_memory(
        db_client,
        user_id=user_id,
        scope=scope,
        content=content,
        source_feedback=feedback_text,
        project_id=pid,
        auto_confirm_threshold=config.MEMORY_AUTO_CONFIRM_THRESHOLD,
    )


def ingest_batch_feedbacks(
    db_client: Client,
    user_id: str,
    feedbacks: list[str],
    project_id: Optional[str] = None,
    project_name: str = "",
) -> list[dict]:
    """
    Process a list of feedback strings (typically from the manual "沉淀反馈
    记忆" button after review).  Routes each feedback through the AI merger
    when enabled; falls back to the legacy classifier otherwise.
    """
    results = []
    for fb in feedbacks:
        if not fb or not fb.strip():
            continue
        if getattr(config, "ENABLE_MEMORY_MERGE", True):
            result = ingest_user_instruction(
                db_client, user_id, fb.strip(),
                project_id=project_id, project_name=project_name,
            )
        else:
            result = ingest_feedback(
                db_client, user_id, fb.strip(),
                project_id=project_id, project_name=project_name,
            )
        results.append(result)
    return results


# ── AI-generated calibration notes ────────────────────────────────────────

_CALIBRATION_SYSTEM = """\
你是一个内容策划顾问，负责帮创作者形成对 AI 的「品味校准」。

你会收到一批内容创作的互动记录（原始生成、用户反馈、迭代修改、最终通过情况），以及之前已有的调教笔记（如有）。

你的任务是生成/更新「调教笔记」。调教笔记的特点：
- 不是规则列表，而是感受性的观察（例："用户喜欢有温度的收尾，而不是 call-to-action 式结尾"）
- 关注「为什么这样被接受/拒绝」，而不是「什么词不能用」（那是记忆的职责）
- 捕捉用户说不清楚但行为里体现出来的隐性审美偏好
- 适当保留之前笔记中仍然成立的观察，融入新的发现

输出格式：纯文本，每条观察用「-」开头，不超过 15 条，总长不超过 600 字。
只输出调教笔记正文，不要有任何标题或前缀说明。"""


def generate_calibration_notes(
    project_name: str,
    existing_notes: str,
    items_with_versions: list[dict],
) -> str:
    """
    Generate/update calibration notes by analysing iteration history.

    items_with_versions: list of item dicts, each with a 'versions' list
    (sorted ascending by version_num; each version has title, body, feedback).
    Returns updated calibration notes as plain text.
    """
    sections: list[str] = []

    approved = [it for it in items_with_versions if it.get("status") == "approved"]
    iterated = [it for it in items_with_versions if len(it.get("versions", [])) > 1]

    if approved:
        parts = []
        for i, item in enumerate(approved[:8], 1):
            vs = item.get("versions", [])
            final = vs[-1] if vs else {}
            title = final.get("title", "")
            body = (final.get("body", "") or "")[:200].split("\n")[0]
            parts.append(f"{i}. 标题：{title}\n   正文节选：{body}")
        sections.append("【已通过文案】\n" + "\n\n".join(parts))

    if iterated:
        chains = []
        for item in iterated[:6]:
            vs = sorted(item.get("versions", []), key=lambda v: v.get("version_num", 0))
            steps = []
            for v in vs:
                fb = v.get("feedback", "")
                title = v.get("title", "")
                body = (v.get("body", "") or "")[:100].split("\n")[0]
                vn = v.get("version_num", "?")
                if fb:
                    steps.append(f"  v{vn}《{title}》\n  → 反馈：{fb}")
                else:
                    steps.append(f"  v{vn}《{title}》正文：{body}")
            chains.append("\n".join(steps))
        sections.append("【迭代过程记录】\n" + "\n\n---\n".join(chains))

    if not sections:
        return existing_notes  # 没有足够数据，保持原样

    user_content = f"项目名称：{project_name}\n\n"
    if existing_notes and existing_notes.strip():
        user_content += f"现有调教笔记：\n{existing_notes.strip()}\n\n"
    user_content += "本次互动记录：\n" + "\n\n".join(sections) + "\n\n请生成更新后的调教笔记。"

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=1024,
        system=_CALIBRATION_SYSTEM,
        messages=[{"role": "user", "content": user_content}],
    )
    return resp.content[0].text.strip()


# Incremental calibration update: runs after every iteration, not only on
# full-batch approval.  Every iteration carries a "why" signal — we don't
# want to wait until the whole batch is approved to learn from it.
_CALIB_INCREMENTAL_SYSTEM = """\
你是内容策划顾问，负责维护调教笔记（项目级的感受性偏好集合）。

收到一条新的迭代记录（原版 → 用户反馈 → 迭代后版本），以及现有调教笔记。
你要判断：
1. 这条迭代里是否包含值得进调教笔记的审美/偏好信号？
   - 有 → 把该信号浓缩成一条（≤ 40 字）加进现有笔记；如果和现有某条同义则不新增，保持不动
   - 没有（例如只是明显的事实性修改、错别字、个人一次性上下文）→ 直接返回现有笔记原文
2. 如果现有笔记已有明显冲突/陈旧的观察，可以精简或替换；但保守优先，除非新信号很强

输出格式：纯文本调教笔记全文，每条用「-」开头，总长不超过 600 字。
不要解释、不要前缀、不要 JSON，只输出笔记正文。"""


def update_calibration_from_iteration(
    db_client: Client,
    project_id: str,
    old_title: str,
    old_body: str,
    feedback: str,
    new_title: str,
    new_body: str,
) -> Optional[str]:
    """
    Incrementally update the project's calibration notes from a single
    iteration triple (old → feedback → new).  Called after every successful
    iteration, so every user correction has a chance to teach the system.

    Returns the updated notes string, or None if the call failed / there was
    nothing to update (notes are persisted as a side effect on success).
    """
    if not (feedback or "").strip():
        return None
    if not config.ANTHROPIC_API_KEY:
        return None
    try:
        proj = db.get_project(db_client, project_id)
    except Exception:
        return None
    if not proj:
        return None
    existing = (proj.get("calibration_notes") or "").strip()

    old_body_preview = (old_body or "")[:180].split("\n")[0]
    new_body_preview = (new_body or "")[:180].split("\n")[0]
    user_content = (
        f"项目：{proj.get('name','')}\n\n"
        + (f"现有调教笔记：\n{existing}\n\n" if existing else "现有调教笔记：(空)\n\n")
        + "本次迭代：\n"
        + f"  v旧 《{old_title}》 {old_body_preview}\n"
        + f"  反馈：{feedback.strip()}\n"
        + f"  v新 《{new_title}》 {new_body_preview}\n\n"
        + "请按系统提示更新调教笔记。"
    )

    try:
        client_kwargs: dict = {"api_key": config.ANTHROPIC_API_KEY}
        if config.ANTHROPIC_BASE_URL:
            client_kwargs["base_url"] = config.ANTHROPIC_BASE_URL
        client = anthropic.Anthropic(**client_kwargs)
        resp = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=900,
            system=_CALIB_INCREMENTAL_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        updated = resp.content[0].text.strip()
    except Exception:
        return None

    if not updated or updated == existing:
        return None
    try:
        db.update_project(db_client, project_id, {"calibration_notes": updated})
    except Exception:
        return None
    return updated


# ── Streamlit memory management UI ────────────────────────────────────────

def render_memory_manager(
    db_client: Client,
    user_id: str,
    project_id: Optional[str] = None,
    project_name: str = "",
) -> None:
    """Render the full memory management page."""
    st.header("🧠 记忆管理")

    tab_global, tab_project = st.tabs(["通用记忆", f"项目记忆（{project_name or '当前项目'}）"])

    with tab_global:
        _render_memory_table(
            db_client, user_id, scope="global", project_id=None,
            label="通用记忆"
        )

    with tab_project:
        if not project_id:
            st.info("请先选择一个项目。")
        else:
            _render_memory_table(
                db_client, user_id, scope="project",
                project_id=project_id, label="项目记忆"
            )

    # Manual add
    st.divider()
    st.markdown("#### ➕ 手动添加记忆")
    with st.form("add_memory_form", clear_on_submit=True):
        col1, col2 = st.columns([1, 3])
        with col1:
            scope_choice = st.selectbox("类型", ["项目记忆", "通用记忆"])
        with col2:
            content = st.text_input("记忆内容", placeholder="例：标题带数字点击率更高")
        add_submitted = st.form_submit_button("添加", use_container_width=True)

    if add_submitted and content.strip():
        scope = "project" if scope_choice == "项目记忆" else "global"
        pid = project_id if scope == "project" else None
        db.upsert_memory(
            db_client, user_id,
            scope=scope,
            content=content.strip(),
            source_feedback="手动添加",
            project_id=pid,
            auto_confirm_threshold=1,  # manual → immediately confirmed
        )
        st.success("记忆已添加。")
        st.rerun()

    # Export / Import
    st.divider()
    st.markdown("#### 📦 记忆导出 & 导入")

    col_export, col_import = st.columns(2)

    with col_export:
        all_memories = db.list_memories(db_client, user_id)
        if all_memories:
            export_data = []
            for m in all_memories:
                export_data.append({
                    "scope": m.get("scope", ""),
                    "content": m.get("content", ""),
                    "source_feedback": m.get("source_feedback", ""),
                    "frequency": m.get("frequency", 1),
                    "status": m.get("status", "candidate"),
                })
            export_json = json.dumps(export_data, ensure_ascii=False, indent=2)
            st.download_button(
                label="⬇️ 导出记忆 (JSON)",
                data=export_json.encode("utf-8"),
                file_name="memories_export.json",
                mime="application/json",
                use_container_width=True,
            )
        else:
            st.caption("暂无记忆可导出。")

    with col_import:
        uploaded = st.file_uploader("导入记忆 (JSON)", type=["json"], key="mem_import")
        if uploaded and st.button("📥 开始导入", use_container_width=True):
            try:
                import_data = json.loads(uploaded.read().decode("utf-8"))
                if not isinstance(import_data, list):
                    st.error("JSON 格式错误：需要一个数组。")
                else:
                    imported = 0
                    for m in import_data:
                        scope = m.get("scope", "project")
                        if scope not in ("project", "global"):
                            scope = "project"
                        pid = project_id if scope == "project" else None
                        db.upsert_memory(
                            db_client, user_id,
                            scope=scope,
                            content=m.get("content", ""),
                            source_feedback=m.get("source_feedback", "导入"),
                            project_id=pid,
                            auto_confirm_threshold=1,
                        )
                        imported += 1
                    st.success(f"已导入 {imported} 条记忆。")
                    st.rerun()
            except json.JSONDecodeError:
                st.error("JSON 解析失败，请检查文件格式。")


def _render_memory_table(
    db_client: Client,
    user_id: str,
    scope: str,
    project_id: Optional[str],
    label: str,
) -> None:
    memories = db.list_memories(db_client, user_id, scope=scope, project_id=project_id)
    if not memories:
        st.info(f"暂无{label}。")
        return

    confirmed = [m for m in memories if m["status"] == "confirmed"]
    candidates = [m for m in memories if m["status"] == "candidate"]

    if confirmed:
        st.markdown("**✅ 已确认**")
        for m in confirmed:
            _render_memory_row(db_client, m, show_confirm=False)

    if candidates:
        st.markdown("**⏳ 候选中（出现次数不足）**")
        for m in candidates:
            _render_memory_row(db_client, m, show_confirm=True)


def _render_memory_row(db_client: Client, memory: dict, show_confirm: bool) -> None:
    import html as _html
    col1, col2, col3, col4 = st.columns([5, 1, 1, 1])
    with col1:
        safe_content = _html.escape(memory['content'])
        safe_source = _html.escape(memory.get('source_feedback', '')[:30])
        st.markdown(
            f"{safe_content} "
            f"<small style='color:grey'>（来源：{safe_source}）</small>",
            unsafe_allow_html=True,
        )
    with col2:
        st.caption(f"×{memory['frequency']}")
    with col3:
        if show_confirm:
            if st.button("确认", key=f"confirm_{memory['id']}"):
                db.update_memory(db_client, memory["id"], {"status": "confirmed"})
                st.rerun()
    with col4:
        if st.button("删除", key=f"del_mem_{memory['id']}"):
            db.delete_memory(db_client, memory["id"])
            st.rerun()
