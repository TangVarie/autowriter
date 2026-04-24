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


def _make_anthropic_client() -> anthropic.Anthropic:
    """One consistent Anthropic client factory — always honours
    ANTHROPIC_BASE_URL so backend utility calls route through the same proxy
    as generation calls."""
    client_kwargs: dict = {"api_key": config.ANTHROPIC_API_KEY}
    if config.ANTHROPIC_BASE_URL:
        client_kwargs["base_url"] = config.ANTHROPIC_BASE_URL
    return anthropic.Anthropic(**client_kwargs)


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
2. "rule"  —— 一条会长期生效的约束或偏好，未来每次生成都要遵守。
3. "taste" —— 纯审美风格描述，难以明确执行或检查的感受性观察（例："想要更轻盈的节奏感"）。走调教笔记。
4. "session" —— 仅适用于本次或近期生成的临时上下文（例："本次突出柑橘口味"、"这批主打周五场景"），或实验性试探（"试试数字开头"）。不值得永久保留。

关键判定准则：

A. **包含"不要 / 别 / 避免 / 禁止 / 不能 / 少用 / 多用"等约束型措辞的反馈，一律归为 rule**，
   即使里面含有"太多"、"太少"、"过于"等模糊量词 —— 用户意图明确，且可以作为生成时的自检点。
   例：
   - "不要使用太多个人情绪自述性文字" → rule
   - "标题避免数字开头" → rule
   - "少用感叹号" → rule
   - "别用'微醉'" → rule

B. 正向的具体要求（带可操作主语/宾语）也归为 rule。
   例："标题要带场景感" → rule；"开头先讲故事再讲产品" → rule

C. 只有纯描述感受、无法落到具体生成动作的，才归 taste。
   例："想要更有温度" → taste；"希望节奏感更好" → taste

D. 本次 / 这批 / 这次 / 试试 / 临时 等明显时限词 → session。

E. 已经和现有硬规则表达同一件事 → merge（输出现有那条的 id）。

若是 rule：判断 scope。与某个品牌/产品强相关 → "project"；对所有小红书文案普遍成立 → "global"。
content 字段规范化成 ≤ 50 字的中文短句。

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
        client = _make_anthropic_client()
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


def _dedup_calibration_lines(text: str, max_chars: int = 1200) -> str:
    """
    Line-level dedup for calibration notes.

    Multiple writers can add observations (manual-refine auto update, per-
    iteration incremental, full-batch reflection, merger's taste path, user's
    manual save); each path believes it's "merging" but without guarantees.
    This normalises every persisted copy: strip bullet prefixes, drop near-
    exact duplicates (first 15 normalised chars as the key), cap at
    ``max_chars`` by dropping the oldest survivors.
    """
    if not text:
        return ""
    lines = text.split("\n")
    seen: set[str] = set()
    clean_lines: list[str] = []
    for raw in lines:
        stripped = raw.strip().lstrip("-•·*· ").strip()
        if not stripped:
            continue
        key = " ".join(stripped.split())[:15].lower()
        if not key or key in seen:
            continue
        seen.add(key)
        clean_lines.append(f"- {stripped}")

    if not clean_lines:
        return ""

    joined = "\n".join(clean_lines)
    while len(joined) > max_chars and len(clean_lines) > 1:
        clean_lines.pop(0)  # drop oldest
        joined = "\n".join(clean_lines)
    return joined


def save_calibration_notes(
    db_client: Client,
    project_id: str,
    notes: str,
) -> str:
    """
    Single choke point for writing ``projects.calibration_notes``.  Runs
    ``_dedup_calibration_lines`` first so any writer gets the same invariant
    enforced regardless of whether it's the merger's taste path, the per-
    iteration incremental update, the diff-from-manual-edit path, the full-
    batch reflection, or a user's manual save from the preview editor.
    Returns the deduped text that was persisted.
    """
    deduped = _dedup_calibration_lines(notes or "")
    try:
        db.update_project(db_client, project_id, {"calibration_notes": deduped})
    except Exception:
        pass
    return deduped


def _append_taste_to_calibration(
    db_client: Client,
    project_id: str,
    observation: str,
) -> None:
    """Append a single taste observation to the project's calibration notes.

    Keeps the notes bounded (≤ 800 characters total) by deduping near-exact
    matches and trimming the oldest entries once capacity is exceeded.
    """
    try:
        proj = db.get_project(db_client, project_id)
    except Exception:
        return
    if not proj:
        return

    existing = (proj.get("calibration_notes") or "").rstrip()
    line = observation.lstrip("-•· ").strip()
    if not line:
        return

    merged = (existing + "\n- " + line) if existing else f"- {line}"
    save_calibration_notes(db_client, project_id, merged)


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
        client = _make_anthropic_client()
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
你是一个内容策划顾问，负责维护项目的「调教笔记」。调教笔记是项目级感受性偏好的集合。

你会收到现有调教笔记，以及来自本批次的用户显式信号（仅限两类，按权重从高到低）：

  【信号 A · 最高权重】手动精修差异：AI 原版 vs 用户手动改后的版本
    - 这是金标准：用户亲手把 AI 的写法改成他要的样子，每一处差异都是意图明确的偏好
    - 重点提炼：改动的方向（加 / 删 / 换）、改的部位（标题 / 开头 / 结尾 / 句式 / 用词 / 标点）

  【信号 B · 次要权重】迭代反馈：用户改写某条时写下的反馈文字 + 前后版本
    - 用户的反馈可能表达含糊或带情绪，仅作辅助参考

硬约束：

- **只能**从上述两类显式信号里提炼观察
- 同一条现象若 A 和 B 都有覆盖，以 A（手动差异）为准；若冲突，信 A 不信 B
- **不能**从单纯的"已通过"文案里推断风格偏好（通过只等于"可用"，不等于"用户喜欢这个风格"）
- **不能**对用户没有改、没有反馈、没有提及的细节下结论
- **不能**泛化"小红书通用经验"，调教笔记只记录这个用户/项目特有的偏好
- 没有清晰信号的观察一律不加；宁可让笔记变短也不要凑字数
- 若本批次信号与现有笔记冲突，以新信号为准；若与现有某条同义，不新增
- 若本批次没有足够强的信号，直接返回「现有调教笔记」原文，不要硬凑

观察格式（极重要 —— 三层结构）：
每条观察应由三部分组成：**方向 + 例子 + 为什么/风格关联**。
- 方向：抽象描述用户偏好的类别（如"标题偏好…"、"结尾倾向…"、"开头避免…"）
- 例子：用简短的原文片段作为佐证，放括号里（一次迭代里最显著的那 1-2 处）
- 为什么：一句话解释这个偏好呼应的调性/场景（可选，但强烈建议）

好坏对比：
- 不好（仅例子，读起来像机械替换指令）：
  · "'3个真相' → '真相'"
  · "删除'说人话就是'等解释性过渡句"
- 不好（仅方向，读起来像空泛口号）：
  · "标题偏好不带量化修饰"
  · "拒绝解释性过渡"
- 好（方向 + 例子 + 为什么）：
  · "标题偏好不带量化修饰（如'3个真相' → '真相'），呼应用户不张扬的低调基调"
  · "拒绝解释性过渡（删除'说人话就是'等），希望结论直给不啰嗦"
  · "开头偏好不经意发现式的低姿态叙事（'深夜查文献我发现' → '查文献一不小心发现'），弱化自我专业感"

每条观察应该能独立作用于**任何一篇新文案**，不只解释这一次的改动。

输出格式：纯文本，每条观察用「-」开头，最多 15 条，总长 ≤ 600 字。
只输出调教笔记正文，不要有任何标题、前言、解释、JSON 或 Markdown。"""


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
    # Only feed Claude explicit user signals — things the user has demonstrably
    # reacted to.  Feeding unchanged-and-unreferenced "approved" drafts invites
    # Claude to invent style rules from incidental choices.  Two signal types:
    #   1. iteration chains that carry feedback text between versions
    #   2. manual-edit diffs (latest version has ai_engine='manual')
    iterated_with_feedback: list[dict] = []
    manual_edits: list[tuple[dict, dict]] = []

    for item in items_with_versions:
        versions = sorted(item.get("versions") or [], key=lambda v: v.get("version_num", 0))
        if len(versions) < 2:
            continue
        # Iteration chain with at least one non-manual feedback line
        non_manual_feedback = [
            v for v in versions[1:]
            if v.get("feedback") and v.get("feedback").strip() and v.get("feedback") != "手动精修"
        ]
        if non_manual_feedback:
            iterated_with_feedback.append(item)
        # Manual edit: latest version is user-authored, preceded by an AI version
        last = versions[-1]
        if (last.get("ai_engine") or "").lower() == "manual":
            prev = versions[-2]
            manual_edits.append((prev, last))

    if not iterated_with_feedback and not manual_edits:
        # No explicit signals → leave notes untouched.  Unchanged drafts and
        # bare "approved" marks don't count as teaching material.
        return existing_notes

    sections: list[str] = []

    # Signal A (highest-weight) first: manual精修 diffs.
    if manual_edits:
        diffs = []
        for prev, last in manual_edits[:6]:
            diffs.append(
                "AI 原版：\n"
                f"  标题：{prev.get('title','')}\n"
                f"  正文：{(prev.get('body','') or '')[:220].split(chr(10))[0]}\n"
                "用户手动版：\n"
                f"  标题：{last.get('title','')}\n"
                f"  正文：{(last.get('body','') or '')[:220].split(chr(10))[0]}"
            )
        sections.append("【信号 A · 手动精修差异（最高权重）】\n" + "\n\n---\n".join(diffs))

    # Signal B (secondary): iteration feedback chains.
    if iterated_with_feedback:
        chains = []
        for item in iterated_with_feedback[:6]:
            vs = sorted(item.get("versions") or [], key=lambda v: v.get("version_num", 0))
            steps = []
            for v in vs:
                fb = (v.get("feedback") or "").strip()
                title = v.get("title", "")
                body = (v.get("body", "") or "")[:100].split("\n")[0]
                vn = v.get("version_num", "?")
                if fb and fb != "手动精修":
                    steps.append(f"  v{vn}《{title}》\n  → 用户反馈：{fb}")
                else:
                    steps.append(f"  v{vn}《{title}》正文节选：{body}")
            chains.append("\n".join(steps))
        sections.append("【信号 B · 迭代反馈链（次要权重）】\n" + "\n\n---\n".join(chains))

    user_content = f"项目名称：{project_name}\n\n"
    if existing_notes and existing_notes.strip():
        user_content += f"现有调教笔记：\n{existing_notes.strip()}\n\n"
    user_content += (
        "本批次的显式用户信号：\n"
        + "\n\n".join(sections)
        + "\n\n按系统提示更新调教笔记；只能基于上述显式信号做观察。"
    )

    client = _make_anthropic_client()
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
   - 有 → 把该信号**抽象成一条方向性观察**（≤ 40 字）加进现有笔记；如果和现有某条同义则不新增，保持不动
   - 没有（例如只是明显的事实性修改、错别字、个人一次性上下文）→ 直接返回现有笔记原文
2. 如果现有笔记已有明显冲突/陈旧的观察，可以精简或替换；但保守优先，除非新信号很强

观察格式（三层结构）：
每条观察 = 方向 + 例子（括号内原文佐证）+ 为什么/风格关联。
- 不好（仅例子）："'3个真相' → '真相'"
- 不好（仅方向）："标题偏好不带量化修饰"
- 好："标题偏好不带量化修饰（如'3个真相' → '真相'），呼应用户不张扬的低调基调"

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
        client = _make_anthropic_client()
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
        updated = save_calibration_notes(db_client, project_id, updated)
    except Exception:
        return None
    return updated


# Diff-driven calibration: when a user manually rewrites an AI draft we
# can't ask "what changed and why" — we can only compare the two texts.
# Ask Claude to read both and extract any taste signals worth keeping.
_CALIB_MANUAL_EDIT_SYSTEM = """\
你是内容策划顾问，负责维护调教笔记。

你收到一条 AI 原版文案 + 用户手动精修后的版本，以及现有调教笔记。
不是简单的改错 —— 两者之间的每一处差异都反映了用户的隐性审美偏好。

你要做的：
1. 逐项对比：标题用词 / 开头切入 / 句式 / 结尾 / 标点 / 段落结构 / 情绪强度
2. 把差异里可归纳的偏好**抽象**成 ≤ 40 字的观察，加进现有笔记
3. 若与现有某条同义，不新增；若无显著信号，保留现有笔记原文

观察格式（三层结构，手动 diff 最容易诱导你只写"X 改为 Y"，一定要抽象起来）：
每条观察 = 方向 + 例子（括号内保留原文片段作佐证）+ 为什么/风格关联。
- 不好（仅例子）："删除'说人话就是'等解释性过渡句"
- 不好（仅方向）："拒绝解释性过渡"
- 好："拒绝解释性过渡（删除'说人话就是'等），希望结论直给不啰嗦"
- 好："结尾偏好留白（在'它是天然存在于…'处截断），营造未完成感"
- 好："倾向口语连接词（'然鹅''嘛'等）带出随意感"

输出纯文本的更新后调教笔记全文，每条用「-」开头，总长 ≤ 600 字。
不要解释、不要前缀、不要 JSON，只输出笔记正文。"""


def update_calibration_from_manual_edit(
    db_client: Client,
    project_id: str,
    ai_title: str,
    ai_body: str,
    manual_title: str,
    manual_body: str,
) -> Optional[str]:
    """
    Diff an AI-generated draft against a user's hand-edited version and let
    Claude extract taste signals into the project's calibration notes.

    Called silently after the user saves a manual edit; failure returns None
    and does not break the save flow.
    """
    if not config.ANTHROPIC_API_KEY:
        return None
    # Skip the call if there's effectively no change — not worth a token spend.
    if (ai_title or "").strip() == (manual_title or "").strip() and \
       (ai_body or "").strip() == (manual_body or "").strip():
        return None
    try:
        proj = db.get_project(db_client, project_id)
    except Exception:
        return None
    if not proj:
        return None
    existing = (proj.get("calibration_notes") or "").strip()

    user_content = (
        f"项目：{proj.get('name','')}\n\n"
        + (f"现有调教笔记：\n{existing}\n\n" if existing else "现有调教笔记：(空)\n\n")
        + "AI 原版：\n"
        + f"  标题：{ai_title}\n"
        + f"  正文：{(ai_body or '')[:400]}\n\n"
        + "用户手动精修后：\n"
        + f"  标题：{manual_title}\n"
        + f"  正文：{(manual_body or '')[:400]}\n\n"
        + "请对比差异，按系统提示更新调教笔记。"
    )

    try:
        client = _make_anthropic_client()
        resp = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=900,
            system=_CALIB_MANUAL_EDIT_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        updated = resp.content[0].text.strip()
    except Exception:
        return None

    if not updated or updated == existing:
        return None
    try:
        updated = save_calibration_notes(db_client, project_id, updated)
    except Exception:
        return None
    return updated


# One-shot cleanup: compress mechanical "X → Y" lines accumulated under older
# prompts into directional taste observations.  Used by the Project Settings
# "🧹 整理当前笔记" button.
_CALIB_REWRITE_SYSTEM = """\
你是内容策划顾问。收到一段已有的调教笔记，其中可能包含大量纯粹机械替换式条目
（例如："'3个真相' 改为 '真相'"、"删除'说人话就是'等过渡句"、"用'然鹅'替代连接词"）。

你的任务：把这些零散指令**重写成三层结构的方向性观察**，
例子保留（它们让观察具象可验证），但要补全方向和为什么。

每条观察格式 = 方向 + 例子（括号内原文佐证）+ 为什么/风格关联：
- 方向在前（抽象表述用户偏好的类别）
- 例子在括号里，尽量保留原笔记里已有的原文片段做佐证
- 最后一句"为什么"或"呼应什么风格"，让读者理解观察背后的意图

原则：
- 每条观察 ≤ 60 字，能独立作用于任何一篇新文案
- 同类条目合并（若 5 条都在说"删除解释性过渡"，合成 1 条）
- 纯粹只是机械指令、抽象不出任何方向的 → 丢掉
- 不要凭空编造用户没体现过的偏好

改写示范：
- "'3个真相' → '真相'，避免过度量化"
  → "标题偏好不带量化修饰（如'3个真相' → '真相'），呼应不张扬的低调基调"
- "用'然鹅''嘛'替代书面连接词"
  → "倾向口语连接词（'然鹅''嘛'等）带出随意感，避免书面腔"
- "删除'说人话就是'等解释性过渡句"
  → "拒绝解释性过渡（删除'说人话就是'等），希望结论直给不啰嗦"
- "结尾在'它是天然存在于…'处截断"
  → "结尾偏好留白（在'它是天然存在于…'处截断），营造未完成感"

输出整理后的调教笔记全文，每条「-」开头，最多 15 条，总长 ≤ 600 字。
只输出笔记正文，不要其他任何内容。"""


def simplify_calibration_notes(existing_notes: str) -> Optional[str]:
    """Run the existing calibration notes through Claude with an explicit
    "compress mechanical lines into directional observations" prompt.  Returns
    the cleaned notes (or None on failure / empty input).  Does not persist —
    caller shows a preview and asks the user to confirm before saving."""
    existing = (existing_notes or "").strip()
    if not existing:
        return None
    if not config.ANTHROPIC_API_KEY:
        return None
    try:
        client = _make_anthropic_client()
        resp = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=900,
            system=_CALIB_REWRITE_SYSTEM,
            messages=[{"role": "user", "content": f"现有调教笔记：\n{existing}\n\n请按系统提示整理成方向性观察。"}],
        )
        cleaned = resp.content[0].text.strip()
    except Exception:
        return None
    if not cleaned or cleaned == existing:
        return None
    return cleaned


# ── Streamlit memory management UI ────────────────────────────────────────

def render_memory_manager(
    db_client: Client,
    user_id: str,
    project_id: Optional[str] = None,
    project_name: str = "",
) -> None:
    """Render the full memory management page."""
    st.header("🧠 记忆管理")

    cap = int(getattr(config, "MAX_INJECTED_MEMORIES_PER_SCOPE", 40) or 40)
    st.caption(
        f"每次生成会向 AI 注入每个范围下最多 {cap} 条规则："
        f"最近 7 天新增的规则必入，剩余名额按使用频次填充老规则。"
        f"列表里看到但没进 prompt 的不会丢失，只是暂不参与当次生成。"
    )

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
