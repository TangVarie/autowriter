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
import clients
import db
import telemetry


def _make_anthropic_client() -> anthropic.Anthropic:
    """One consistent Anthropic client factory — always honours
    ANTHROPIC_BASE_URL so backend utility calls route through the same proxy
    as generation calls.

    现在转发到 ``clients.get_anthropic_client()`` 拿到 process-global 单例：
    之前 6 个调用点每次都新建 anthropic.Anthropic(...) 重建 httpx 连接池，
    多角色并行 + 队列模式下浪费明显。函数签名保留兼容老代码。
    """
    return clients.get_anthropic_client()


# ── Prompt assembly ────────────────────────────────────────────────────────

def filter_soft_by_relevance(
    memories: list[dict],
    context_text: str,
    threshold: float = 0.45,
    report_sink: Optional[dict] = None,
) -> list[dict]:
    """
    Drop ``severity='soft'`` rules whose embedding is semantically far from
    ``context_text`` (the current generation's tactic + key_messages + extra
    instructions).  Hard rules pass through unchanged.  Rules without a
    stored embedding (legacy / backfill-pending) also pass through so the
    feature degrades cleanly on un-migrated deployments.

    ``threshold`` is intentionally low (0.45 default): the goal is to drop
    obviously-irrelevant rules ("不要数字开头" when generating an emoji
    batch), not to be precise.  False negatives (keeping a marginal rule)
    are much cheaper than false positives (silently dropping a relevant
    one the user expects to be active).

    Returns the filtered list in the same order.  No-op (returns the input)
    when embeddings aren't configured or ``context_text`` is empty.

    Day 4：``report_sink`` 给调用方留一份"哪些规则被滤掉、为什么"，UI
    可以渲染成"本次注入了 X 条偏好规则，过滤了 Y 条（below_threshold /
    no_embedding / no_embedding_api）"。
    """
    def _record_drop(m: dict, reason: str, score: Optional[float] = None) -> None:
        if report_sink is None:
            return
        entry = {
            "reason": reason,
            "content": (m.get("content") or "")[:80],
        }
        if score is not None:
            entry["score"] = round(float(score), 3)
        report_sink.setdefault("filtered", []).append(entry)

    if not memories or not context_text or not context_text.strip():
        return memories
    try:
        import dedup as _dedup
    except Exception:
        return memories
    if not _dedup.embeddings_available():
        # 整体降级：所有 soft 规则都按"无 embedding API"通过，但记一条总账
        if report_sink is not None:
            report_sink["soft_filter_mode"] = "no_embedding_api"
        return memories
    ctx_vecs = _dedup.embed_texts([context_text.strip()])
    if not ctx_vecs or not ctx_vecs[0]:
        if report_sink is not None:
            report_sink["soft_filter_mode"] = "ctx_embed_failed"
        return memories
    ctx = ctx_vecs[0]
    if report_sink is not None:
        report_sink["soft_filter_mode"] = "active"
        report_sink["soft_filter_threshold"] = threshold
    out: list[dict] = []
    for m in memories:
        if (m.get("severity") or "soft").lower() == "hard":
            out.append(m)
            continue
        vec = m.get("embedding")
        if not vec:
            out.append(m)  # legacy: no embedding stored ⇒ keep
            continue
        try:
            score = _dedup.cosine_similarity(ctx, vec)
        except Exception:
            out.append(m)
            continue
        if score >= threshold:
            out.append(m)
        else:
            _record_drop(m, "below_threshold", score=score)
    return out


def build_system_prompt(
    base_prompt: str,
    global_memories: list[dict],
    project_memories: list[dict],
    tactic_suffix: str = "",
    calibration_notes: str = "",
    positive_examples: Optional[list[dict]] = None,
    negative_examples: Optional[list[dict]] = None,
    session_instructions: Optional[list[dict]] = None,
    report_sink: Optional[dict] = None,
) -> str:
    """
    Assemble the final system prompt as a three-tier priority stack so the
    model can distinguish a non-negotiable rule from a soft preference.

    Tiers (highest priority last so the model reads them most recently):
      P0 — hard constraints: ``severity='hard'`` memories (compliance / brand
           lines) + base_prompt + tactic_suffix.  Must be 100% respected.
      P1 — soft preferences: ``severity='soft'`` memories + calibration notes
           + positive/negative few-shots.  Applied when relevant; defer if
           they conflict with the current batch's tactic or key messages.
      P2 — session-only instructions: ad-hoc rules valid for this batch only.

    This replaces the previous flat "必须执行，每条都要主动检查" wall that
    drowned hard requirements in soft preferences and pushed the model to
    apply unrelated rules out of context.

    Memories without a ``severity`` field (legacy rows) default to ``soft``.

    Day 4：``report_sink`` 收集本次实际注入了多少条各类型规则，
    队列 worker 会把它挂到 metrics.set_meta("injection", ...)，UI 显示
    "硬 N / 软 M / 会话 K / 调校 X 字" 徽章。
    """
    def _is_hard(m: dict) -> bool:
        return (m.get("severity") or "soft").lower() == "hard"

    hard_global   = [m for m in (global_memories  or []) if _is_hard(m)]
    soft_global   = [m for m in (global_memories  or []) if not _is_hard(m)]
    hard_project  = [m for m in (project_memories or []) if _is_hard(m)]
    soft_project  = [m for m in (project_memories or []) if not _is_hard(m)]

    if report_sink is not None:
        report_sink["hard_global"]      = len(hard_global)
        report_sink["soft_global"]      = len(soft_global)
        report_sink["hard_project"]     = len(hard_project)
        report_sink["soft_project"]     = len(soft_project)
        report_sink["session"]          = len(session_instructions or [])
        report_sink["calibration_chars"] = len((calibration_notes or "").strip())
        report_sink["pos_examples"]     = len(positive_examples or [])
        report_sink["neg_examples"]     = len(negative_examples or [])

    parts: list[str] = [base_prompt.strip()]

    if tactic_suffix.strip():
        parts.append(f"\n{tactic_suffix.strip()}")

    # ── P0 ────────────────────────────────────────────────────────────────
    p0_lines: list[str] = []
    if hard_global:
        p0_lines.append("[通用硬约束]")
        p0_lines.extend(f"• {m['content']}" for m in hard_global)
    if hard_project:
        if p0_lines:
            p0_lines.append("")
        p0_lines.append("[项目硬约束]")
        p0_lines.extend(f"• {m['content']}" for m in hard_project)
    if p0_lines:
        parts.append(
            "\n---【P0 · 不可违反的硬约束】---\n"
            "本节每一条都必须 100% 满足；若与下方偏好冲突，以此节为准。\n\n"
            + "\n".join(p0_lines)
        )

    # ── P1 ────────────────────────────────────────────────────────────────
    p1_sections: list[str] = []
    if soft_global:
        bullets = "\n".join(f"• {m['content']}" for m in soft_global)
        p1_sections.append(f"[通用偏好]\n{bullets}")
    if soft_project:
        bullets = "\n".join(f"• {m['content']}" for m in soft_project)
        p1_sections.append(f"[项目偏好]\n{bullets}")
    if calibration_notes and calibration_notes.strip():
        p1_sections.append(f"[调校笔记 · 感受性观察]\n{calibration_notes.strip()}")
    if positive_examples:
        ex_blocks = []
        for ex in positive_examples[:5]:
            body_preview = (ex.get("body") or "")[:200].split("\n")[0]
            ex_blocks.append(f"标题：{ex['title']}\n正文节选：{body_preview}")
        p1_sections.append(
            "[优质正案例 · 学习风格/结构/切入]\n"
            "严禁直接复用例子里的标题主干、开场句、具体比喻；只可借鉴节奏与角度。\n"
            + "\n\n".join(ex_blocks)
        )
    if negative_examples:
        ex_blocks = []
        for ex in negative_examples[:3]:
            body_preview = (ex.get("body") or "")[:120].split("\n")[0]
            ex_blocks.append(f"标题：{ex['title']}\n正文节选：{body_preview}")
        p1_sections.append(
            "[反面案例 · 主动规避]\n"
            + "\n\n".join(ex_blocks)
        )
    if p1_sections:
        parts.append(
            "\n---【P1 · 项目调性偏好】---\n"
            "请理解每条意图、在本批 tactic / 关键卖点适用时再应用；明显不适用时可以让位，"
            "不必为了套用规则扭曲文案。与 P0 冲突时以 P0 为准。\n\n"
            + "\n\n".join(p1_sections)
        )

    # ── P2 ────────────────────────────────────────────────────────────────
    if session_instructions:
        bullets = "\n".join(f"• {m['content']}" for m in session_instructions if m.get("content"))
        if bullets:
            parts.append(
                "\n---【P2 · 本次会话临时指令】---\n"
                "用户在本次对话中提出的要求，本批生成期间严格遵守；过期失效。"
                "与 P0 冲突时仍以 P0 为准。\n"
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

A. 含"不要 / 别 / 避免 / 禁止 / 不能 / 少用 / 多用"等约束型措辞、**且未出现"本次 / 这批 / 这次 /
   试试 / 临时"等时限词** 的反馈，归为 rule。**只要句子里出现任何一个时限词就降级为 session，
   不进永久 rule 库**，避免把一次性建议硬化为长期约束。
   例：
   - "不要使用太多个人情绪自述性文字"           → rule（无时限词）
   - "本次不要使用太多个人情绪自述性文字"        → session（"本次"）
   - "标题避免数字开头"                          → rule
   - "这批标题避免数字开头"                      → session
   - "少用感叹号"                                → rule
   - "试试少用感叹号"                            → session
   - "别用'微醉'"                                → rule

B. 正向的具体要求（带可操作主语/宾语）也归为 rule。
   例："标题要带场景感" → rule；"开头先讲故事再讲产品" → rule

C. 只有纯描述感受、无法落到具体生成动作的，才归 taste。
   例："想要更有温度" → taste；"希望节奏感更好" → taste

D. 本次 / 这批 / 这次 / 试试 / 临时 等明显时限词 → session。

E. 已经和现有硬规则表达同一件事 → merge（输出现有那条的 id）。

若是 rule：判断 scope。与某个品牌/产品强相关 → "project"；对所有小红书文案普遍成立 → "global"。
若是 rule：还要判断 severity（仅 rule 时输出）：
   - "hard" —— 合规、底线、品牌禁忌（违反会出大问题，例："不得宣称疗效"、"严禁出现竞品名"、"禁止使用'最'字"）。
                hard 规则会进入 P0 优先级，每条都必须 100% 满足。**谨慎使用：宁可漏判也不要把一般偏好判为 hard。**
   - "soft" —— 一般风格/语气/结构偏好（违反会影响品质但不致命，例："标题偏短"、"少用感叹号"、"避免数字开头"）。
                soft 规则进入 P1 优先级，会被模型在合适时应用。**默认就归 soft。**

若是 rule：可选输出 applicability（≤ 16 字，描述规则适用的部位）：
   "标题"、"正文开头"、"正文结尾"、"关键词"、"全局"、或具体 tactic 名。
content 字段规范化成 ≤ 50 字的中文短句。

严格输出 JSON，无其他文字：
{
  "action": "merge" | "rule" | "taste" | "session",
  "target_id": "<仅 merge 时有；现有记忆的 id>",
  "scope": "project" | "global",     // 仅 rule 时有
  "severity": "hard" | "soft",       // 仅 rule 时有；默认 "soft"
  "applicability": "<可选；≤16字>",  // 仅 rule 时；不填 = 全局
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
        # Hard vs soft tier：合规级硬规则不能因 merger 回退（merge → rule）而被
        # 静默降级为 soft。merger 在判 ``merge`` 时往往不返回 severity（因为本意
        # 是累加旧规则），一旦目标 id 缺失回退到 rule，若仅看 ``data.get("severity")``
        # 就只能拿到默认 soft——用户写了"严禁/必须"也丢到 P1，P0 兜底失效。
        # 这里额外做一次原文文本探测，含合规级触发词时强制升回 hard。
        severity = (data.get("severity") or "soft").lower()
        _HARD_CUES = (
            "禁止", "严禁", "不得", "必须", "杜绝", "不能出现",
            "绝对不要", "一律不", "违反法规", "合规",
        )
        if severity != "hard" and any(cue in clean for cue in _HARD_CUES):
            severity = "hard"
        out["severity"] = "hard" if severity == "hard" else "soft"
        applicability = (data.get("applicability") or "").strip()
        if applicability:
            out["applicability"] = applicability[:32]
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
            severity=decision.get("severity", "soft"),
            applicability=decision.get("applicability"),
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


def _normalize_line(raw: str) -> str:
    """归一化一行观察文本：剥前缀符号、压缩空白、转小写。
    用作 dedup key 的输入（C2：用归一化全文比对，避免前缀相同就误判重复）。
    """
    stripped = (raw or "").strip().lstrip("-•·*· ").strip()
    return " ".join(stripped.split()).lower()


def _line_key(raw: str) -> str:
    """单行的精确去重 key：归一化全文。

    C2 修复：之前用前 15 字符作为 key，会把"标题偏好不带量化修饰（如3个真相）"
    和"标题偏好不带量化修饰（如5个步骤）"误判为同一条。改为归一化全文后
    任何后缀差异都会保留。
    """
    return _normalize_line(raw)


def _dedup_calibration_lines(
    text: str,
    max_chars: int = 4000,
    dropped_sink: Optional[list[str]] = None,
) -> str:
    """对调教笔记做行级去重 + 软上限截断。

    多个写入入口（手动精修自动更新 / 每次迭代增量 / 整批反思 / 反馈分类
    走 taste / 用户手动保存）都会通过 ``save_calibration_notes``，最终都
    会调到这里。每条观察按 _line_key（归一化全文 hash）去重；超过
    ``max_chars`` 时丢最旧的观察。

    上限 4000 字是软上限，对应大约 30-50 条观察；2026-05 之前默认是 1200
    字，太紧——LLM 重写路径在容量压力下会静默删旧观察。生成时如果还需要
    进一步裁剪，通过 ``filter_soft_by_relevance`` / 调用方 cap 处理，不在
    这里加硬限制。

    被窗口淘汰的旧行不会真正消失：写入路径会把完整 before_text 落到
    ``calibration_note_audit`` 表，UI 的历史查看器可以回看到。``dropped_sink``
    给调用方一个直接拿到本次淘汰行的入口，用于埋点/告知用户。
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
        key = _line_key(stripped)
        if not key or key in seen:
            continue
        seen.add(key)
        clean_lines.append(f"- {stripped}")

    if not clean_lines:
        return ""

    joined = "\n".join(clean_lines)
    while len(joined) > max_chars and len(clean_lines) > 1:
        dropped = clean_lines.pop(0)
        if dropped_sink is not None:
            dropped_sink.append(dropped)
        joined = "\n".join(clean_lines)
    return joined


def _parse_new_observations(raw: str) -> list[str]:
    """
    Extract candidate "new observation" lines from the LLM's response under
    the append-only contract: every line beginning with ``-`` is an
    observation, the literal token ``NONE`` (case-insensitive) means no new
    observation, blank lines and prose explanations are ignored.

    Returns the cleaned observation strings (no leading bullet), preserving
    order.  Empty list = nothing to append.
    """
    if not raw:
        return []
    cleaned = raw.strip()
    if not cleaned or cleaned.upper() == "NONE":
        return []
    out: list[str] = []
    for line in cleaned.splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.startswith(("-", "•", "*", "·")):
            # ignore prose ("以下是新观察：...") — only honour bullet lines
            continue
        text = line.lstrip("-•·* ").strip()
        if not text or text.upper() == "NONE":
            continue
        if len(text) > 120:  # safety: drop monologue lines that slipped through
            continue
        out.append(text)
    return out


def _merge_new_observations(existing: str, new_lines: list[str]) -> str:
    """Pure function：把 ``new_lines`` 合并到 ``existing`` 末尾。

    去重 key = 归一化全文（C2：从前 15 字升级为全文，前缀相同但内容不同
    的观察不再被误判为重复）。已存在的观察永远不会被删除。
    """
    existing = (existing or "").rstrip()
    if not new_lines:
        return existing

    existing_keys: set[str] = {
        _line_key(raw) for raw in existing.splitlines() if raw.strip()
    }
    existing_keys.discard("")

    appended: list[str] = []
    for line in new_lines:
        line = line.strip()
        if not line:
            continue
        key = _line_key(line)
        if not key or key in existing_keys:
            continue
        existing_keys.add(key)
        appended.append(f"- {line}")

    if not appended:
        return existing
    return (existing + "\n" + "\n".join(appended)) if existing else "\n".join(appended)


def _append_new_observations(
    db_client: Client,
    project_id: str,
    new_lines: list[str],
    source: str = "unknown",
) -> Optional[str]:
    """
    Read the project's current calibration_notes, append ``new_lines`` (deduped
    against what's already there), persist the result.  Returns the persisted
    text or None on failure / no-op.

    This is the choke point that replaces the LLM-rewrite path: callers that
    used to ask Claude for the whole new notes text now ask Claude for a list
    of new lines and let this helper merge them.  No older observation is
    ever removed by an append — only the user's explicit edit or the soft
    ``max_chars`` cap (4000) inside ``_dedup_calibration_lines`` can drop a
    line.

    ``source`` 透传给 ``save_calibration_notes`` 给审计表用。
    """
    if not new_lines:
        return None
    try:
        proj = db.get_project(db_client, project_id)
    except Exception as exc:
        telemetry.log_event(
            "calibration_update_read_failed",
            project_id=project_id, source=source, error=str(exc)[:200],
        )
        return None
    if not proj:
        return None
    merged = _merge_new_observations(proj.get("calibration_notes") or "", new_lines)
    if not merged or merged == (proj.get("calibration_notes") or "").rstrip():
        return None
    try:
        return save_calibration_notes(db_client, project_id, merged, source=source)
    except Exception:
        # save_calibration_notes has already logged the error;
        # this caller is the iteration/incremental background path so we
        # swallow here to avoid blowing up the queue worker.
        return None


def save_calibration_notes(
    db_client: Client,
    project_id: str,
    notes: str,
    source: str = "unknown",
) -> str:
    """
    Single choke point for writing ``projects.calibration_notes``.  Runs
    ``_dedup_calibration_lines`` first so any writer gets the same invariant
    enforced regardless of whether it's the merger's taste path, the per-
    iteration incremental update, the diff-from-manual-edit path, the full-
    batch reflection, or a user's manual save from the preview editor.
    Returns the deduped text that was persisted.

    ``source`` 标记触发本次写入的入口（``iteration`` / ``manual_edit`` /
    ``batch_reflection`` / ``merger_taste`` / ``user_manual``），仅用于
    审计表追踪；不影响写入逻辑。

    每次写入都会同步往 ``calibration_note_audit`` 表插一条 before / append /
    after 记录（C1 审计闭环）。审计失败不会影响主写入。

    数据完整性原则（2026-05 Day 1）：``update_project`` 失败属于数据丢失
    点，会向上抛；本函数的所有上层调用都被包在 try/except + UI 告警里，
    不再有"看似成功实际失败"的状态。
    """
    dropped: list[str] = []
    deduped = _dedup_calibration_lines(notes or "", dropped_sink=dropped)

    # 先读旧值用于 audit before；与下面 update_project 是同一次会话窗口
    before_text = ""
    try:
        proj = db.get_project(db_client, project_id)
        before_text = (proj.get("calibration_notes") or "") if proj else ""
    except Exception as exc:
        # 读旧值只是为了 audit before，失败不阻塞写入主流程，
        # 但要留痕方便排错
        telemetry.log_event(
            "calibration_before_read_failed",
            project_id=project_id, source=source, error=str(exc)[:200],
        )

    try:
        db.update_project(db_client, project_id, {"calibration_notes": deduped})
    except Exception as exc:
        telemetry.log_event(
            "calibration_save_error",
            project_id=project_id, source=source, error=str(exc)[:200],
        )
        raise

    if dropped:
        telemetry.log_event(
            "calibration_window_dropped",
            project_id=project_id, source=source, count=len(dropped),
        )

    # 计算本次新增的观察行：deduped 中存在但 before 中不存在的（按 _line_key 比对）
    before_keys = {
        _line_key(l) for l in before_text.splitlines() if l.strip()
    }
    before_keys.discard("")
    append_lines = []
    for l in deduped.splitlines():
        key = _line_key(l)
        if key and key not in before_keys:
            append_lines.append(l.strip())

    # 只在内容真的变化时记审计，避免空操作刷屏
    if before_text != deduped:
        db.insert_calibration_audit(
            db_client, project_id, source,
            before_text=before_text,
            append_lines=append_lines,
            after_text=deduped,
        )

    return deduped


def _append_taste_to_calibration(
    db_client: Client,
    project_id: str,
    observation: str,
) -> None:
    """往项目的调教笔记追加一条感受性观察。

    走的是同一个 ``save_calibration_notes`` 入口，因此自动享受 _line_key 全文去重、
    ``_dedup_calibration_lines`` 的软上限（4000 字，约 30-50 条）、以及
    审计表写入。``source="merger_taste"`` 标记来自分类器的 taste 路径。
    """
    try:
        proj = db.get_project(db_client, project_id)
    except Exception as exc:
        telemetry.log_event(
            "calibration_taste_read_failed",
            project_id=project_id, error=str(exc)[:200],
        )
        return
    if not proj:
        return

    existing = (proj.get("calibration_notes") or "").rstrip()
    line = observation.lstrip("-•· ").strip()
    if not line:
        return

    merged = (existing + "\n- " + line) if existing else f"- {line}"
    try:
        save_calibration_notes(db_client, project_id, merged, source="merger_taste")
    except Exception:
        # save_calibration_notes already logged; this is a best-effort taste
        # append called from the feedback classifier, don't block the caller.
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
你是一个内容策划顾问，负责往项目的「调教笔记」追加新的观察。

调教笔记是越滚越大的偏好集合，**绝对不允许重写、压缩或删除任何既有条目**。
你只负责判断：本批次的显式信号里，是否包含**现有笔记尚未覆盖**的新观察？

你会收到：
  • 现有调教笔记全文（仅供查重；你**不要**对它做任何改写）
  • 本批次用户显式信号（两类按权重从高到低）：
    【信号 A · 最高权重】手动精修差异：AI 原版 vs 用户手动改后的版本
      - 这是金标准：用户亲手把 AI 的写法改成他要的样子，每一处差异都是明确偏好
      - 重点提炼：改动方向（加 / 删 / 换）、改的部位（标题 / 开头 / 结尾 / 句式 / 用词 / 标点）
    【信号 B · 次要权重】迭代反馈：用户改写某条时写下的反馈文字 + 前后版本
      - 用户反馈可能含糊或带情绪，仅作辅助参考

硬约束：
- **只能**从上述两类显式信号里提炼观察
- 同一现象 A 和 B 冲突时信 A
- **不能**从单纯的"已通过"文案里推断偏好（通过 ≠ 喜欢这个风格）
- **不能**对用户没改过、没反馈过的细节下结论
- **不能**泛化"小红书通用经验"
- 若本批次信号在现有笔记里已有同义条目（哪怕措辞不同），**不要重复添加**
- 若本批次信号弱、或全部已被现有笔记覆盖，**直接输出 NONE**

观察格式（每条 ≤ 40 字）—— 三层结构：方向 + 例子（括号内）+ 为什么/风格关联。
- 不好（仅例子）：「'3个真相' → '真相'」
- 不好（仅方向）：「标题偏好不带量化修饰」
- 好：「标题偏好不带量化修饰（如'3个真相' → '真相'），呼应不张扬基调」
- 好：「拒绝解释性过渡（删除'说人话就是'等），希望结论直给不啰嗦」

输出格式（极重要）：
- 仅输出本批次需要**新追加**的观察行，每条以「-」开头，每行一条
- 最多 5 条新观察（信号通常没那么多）
- 若没有新观察可加：输出单独一行 NONE
- 不要前言、解释、JSON、Markdown、不要重复现有笔记里的任何条目"""


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
        user_content += f"现有调教笔记（仅供查重，不要改写）：\n{existing_notes.strip()}\n\n"
    else:
        user_content += "现有调教笔记：(空)\n\n"
    user_content += (
        "本批次的显式用户信号：\n"
        + "\n\n".join(sections)
        + "\n\n按系统提示，仅输出本批次新追加的观察（或单独一行 NONE）。"
    )

    client = _make_anthropic_client()
    resp = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=512,
        system=_CALIBRATION_SYSTEM,
        messages=[{"role": "user", "content": user_content}],
    )
    raw = resp.content[0].text.strip()
    new_lines = _parse_new_observations(raw)
    merged = _merge_new_observations(existing_notes or "", new_lines)
    # Returning the unchanged existing text would cause the caller to overwrite
    # the row with itself; signal "no change" with the original string so the
    # caller's no-op check (notes == existing) catches it.
    return merged


# Incremental calibration update: runs after every iteration, not only on
# full-batch approval.  Every iteration carries a "why" signal — we don't
# want to wait until the whole batch is approved to learn from it.
_CALIB_INCREMENTAL_SYSTEM = """\
你是内容策划顾问，负责往调教笔记**追加**新观察（绝不允许删除或改写既有条目）。

收到一条新的迭代记录（原版 → 用户反馈 → 迭代后版本），以及现有调教笔记全文。
你的唯一任务：判断这条迭代是否包含**现有笔记尚未覆盖**的新偏好信号。

判定规则：
- 是显著新信号 → 输出 1 条新观察（≤ 40 字，三层结构：方向 + 例子 + 为什么）
- 信号弱、是事实性修改、错别字、一次性上下文、或已被现有笔记覆盖 → 输出单独一行 NONE

观察格式示例：
- 不好（仅例子）：「'3个真相' → '真相'」
- 不好（仅方向）：「标题偏好不带量化修饰」
- 好：「标题偏好不带量化修饰（如'3个真相' → '真相'），呼应不张扬基调」

输出格式（极重要）：
- 仅输出本次需要追加的 1 条新观察行，以「-」开头
- 若没有：输出单独一行 NONE
- 严禁重复现有笔记里的任何条目；严禁输出已有笔记的全文或片段；严禁前言、解释、JSON、Markdown"""


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
        + (
            f"现有调教笔记（仅供查重，不要改写）：\n{existing}\n\n"
            if existing else "现有调教笔记：(空)\n\n"
        )
        + "本次迭代：\n"
        + f"  v旧 《{old_title}》 {old_body_preview}\n"
        + f"  反馈：{feedback.strip()}\n"
        + f"  v新 《{new_title}》 {new_body_preview}\n\n"
        + "按系统提示输出 1 条新观察（或 NONE）。"
    )

    try:
        client = _make_anthropic_client()
        resp = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=200,
            system=_CALIB_INCREMENTAL_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = resp.content[0].text.strip()
    except Exception:
        return None

    new_lines = _parse_new_observations(raw)
    if not new_lines:
        return None
    try:
        return _append_new_observations(
            db_client, project_id, new_lines, source="iteration"
        )
    except Exception:
        return None


# Diff-driven calibration: when a user manually rewrites an AI draft we
# can't ask "what changed and why" — we can only compare the two texts.
# Ask Claude to read both and extract any taste signals worth keeping.
_CALIB_MANUAL_EDIT_SYSTEM = """\
你是内容策划顾问，负责往调教笔记**追加**新观察（绝不允许删除或改写既有条目）。

你收到一条 AI 原版文案 + 用户手动精修后的版本 + 现有调教笔记全文。
两者之间的每一处差异都反映了用户的隐性审美偏好。

你要做的：
1. 逐项对比差异点：标题用词 / 开头切入 / 句式 / 结尾 / 标点 / 段落结构 / 情绪强度
2. 把可归纳的偏好抽象成 ≤ 40 字的观察（手动 diff 最容易诱导你只写"X 改为 Y"，一定要抽象起来）
3. 严格按现有笔记查重，**已被覆盖的偏好不要重复添加**
4. 若无显著新信号、或所有差异都已被现有笔记覆盖 → 输出单独一行 NONE

观察格式（三层结构）：方向 + 例子（括号内）+ 为什么/风格关联。
- 不好（仅例子）：「删除'说人话就是'等解释性过渡句」
- 不好（仅方向）：「拒绝解释性过渡」
- 好：「拒绝解释性过渡（删除'说人话就是'等），希望结论直给不啰嗦」
- 好：「结尾偏好留白（'它是天然存在于…'处截断），营造未完成感」
- 好：「倾向口语连接词（'然鹅''嘛'等）带出随意感」

输出格式（极重要）：
- 仅输出本次需要追加的新观察行，每条以「-」开头，每行一条，最多 3 条
- 若没有新观察：输出单独一行 NONE
- 严禁重复现有笔记里的任何条目；严禁输出已有笔记的全文或片段；严禁前言、解释、JSON、Markdown"""


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
        + (
            f"现有调教笔记（仅供查重，不要改写）：\n{existing}\n\n"
            if existing else "现有调教笔记：(空)\n\n"
        )
        + "AI 原版：\n"
        + f"  标题：{ai_title}\n"
        + f"  正文：{(ai_body or '')[:400]}\n\n"
        + "用户手动精修后：\n"
        + f"  标题：{manual_title}\n"
        + f"  正文：{(manual_body or '')[:400]}\n\n"
        + "对比差异，按系统提示输出 0-3 条新观察（或 NONE）。"
    )

    try:
        client = _make_anthropic_client()
        resp = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=400,
            system=_CALIB_MANUAL_EDIT_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = resp.content[0].text.strip()
    except Exception:
        return None

    new_lines = _parse_new_observations(raw)
    if not new_lines:
        return None
    try:
        return _append_new_observations(
            db_client, project_id, new_lines, source="manual_edit"
        )
    except Exception:
        return None


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

# ─────────────────────────────────────────────────────────────────────────
# 记忆管理页面
# ─────────────────────────────────────────────────────────────────────────
#
# 页面分四段（从上到下）：
#   1. 标题 + 一句话总览
#   2. "本次会注入哪些规则" 折叠预览（默认收起）
#   3. 通用记忆 / 项目记忆 两个 tab，列出所有规则
#   4. 手动添加 + 导入导出 + 补算 embedding（折叠到底部工具区）
#
# 单条规则行的设计原则：
#   - 主屏幕只显示「内容 + 频率 + 删除按钮 + 候选才有的"确认"按钮」。
#   - 进阶操作（severity 切换 / 静音 / 转调校笔记）收纳到行末的 ⚙ 弹出框，
#     避免主列表被一排按钮挤花。
#   - 旁边的 emoji badge（🔒/🔕/标签）替代文字说明，扫一眼就能区分。
#
# 修改入口：render_memory_manager → 4 个内部 helper（_render_inject_preview
# / _render_memory_table / _render_memory_row / _render_bottom_tools）。

def render_memory_manager(
    db_client: Client,
    user_id: str,
    project_id: Optional[str] = None,
    project_name: str = "",
) -> None:
    """记忆管理页面的入口。

    Streamlit 调用方式：在主菜单点 "🧠 记忆管理" 时执行。会渲染：
    总览 → 注入预览（折叠）→ 通用/项目两个 tab → 底部工具区。
    """
    st.header("🧠 记忆管理")

    cap = int(getattr(config, "MAX_INJECTED_MEMORIES_PER_SCOPE", 12) or 12)
    # 一句话告诉用户：硬约束必入；偏好按数量上限挑；静音的不会被用。
    # 详细规则在下面的 expander 里，不在主屏占用空间。
    st.caption(f"硬约束每条必用 · 偏好最多注入 {cap} 条/范围 · 静音的暂不参与生成")
    with st.expander("📘 完整规则说明", expanded=False):
        st.markdown(
            "- **硬约束（🔒）**：合规底线、品牌禁忌。每条都必须 100% 满足，没有数量上限。\n"
            f"- **偏好**：风格、用词倾向。每个范围最多注入 {cap} 条（近 7 天新增必入，其余按频次）。\n"
            "- **静音（🔕）**：临时禁用 24 小时，不删除；倒计时结束自动恢复。\n"
            "- 列表里看到但未注入的规则不会丢失，只是本次没参与。"
        )

    # 注入预览：让用户看到「本次生成实际会注入哪些规则」，不用看 prompt 也能 debug
    _render_inject_preview(db_client, user_id, project_id, project_name)

    tab_global, tab_project = st.tabs(["通用记忆", f"项目记忆（{project_name or '当前项目'}）"])
    with tab_global:
        _render_memory_table(
            db_client, user_id, scope="global", project_id=None,
            label="通用记忆", current_project_id=project_id,
        )
    with tab_project:
        if not project_id:
            st.info("请先选择一个项目。")
        else:
            _render_memory_table(
                db_client, user_id, scope="project",
                project_id=project_id, label="项目记忆",
                current_project_id=project_id,
            )

    # 底部工具区：手动添加 / 导入导出 / 补算 embedding 都折叠在这
    _render_bottom_tools(db_client, user_id, project_id)


def _render_bottom_tools(
    db_client: Client, user_id: str, project_id: Optional[str]
) -> None:
    """底部工具区：3 个折叠卡片 = 手动添加 / 导入导出 / 补算 embedding。

    刻意全部折叠默认收起，避免日常使用看记忆列表时被工具按钮干扰。
    """
    st.divider()

    # 卡片 1：手动添加单条记忆
    # Day 3：硬规则支持结构化录入（禁用词 / 必含词 / 最大字数 / 禁用正则）。
    # 这里不用 st.form——form 内无法根据 selectbox 当前选项条件渲染 payload
    # 字段（form 只在 submit 时回传所有状态），改用 plain widgets + button。
    with st.expander("➕ 手动添加记忆", expanded=False):
        col_scope, col_sev = st.columns([1, 1])
        with col_scope:
            scope_choice = st.selectbox(
                "类型", ["项目记忆", "通用记忆"], key="add_mem_scope",
            )
        with col_sev:
            severity_choice = st.selectbox(
                "严格程度",
                ["软规则（偏好）", "硬规则（必须执行）"],
                key="add_mem_severity",
                help="软规则只在与当前生成上下文相关时注入；硬规则永远注入，结构化 kind 会被确定性校验。",
            )
        is_hard = severity_choice.startswith("硬")

        rule_kind = None
        rule_payload: Optional[dict] = None
        auto_content = ""
        if is_hard:
            kind_choice = st.selectbox(
                "硬规则类型",
                ["自由文本", "禁用词 (forbidden_word)", "必含词 (required_phrase)",
                 "最大字数 (max_len)", "禁用正则 (forbidden_regex)"],
                key="add_mem_kind",
                help="结构化类型可被确定性校验（命中后标 needs_revision）；自由文本走正则抽取兜底。",
            )
            if kind_choice.startswith("禁用词"):
                rule_kind = "forbidden_word"
                target = st.text_input("目标词", key="add_mem_target_fw").strip()
                if target:
                    rule_payload = {"target": target}
                    auto_content = f"禁止出现「{target}」"
            elif kind_choice.startswith("必含词"):
                rule_kind = "required_phrase"
                target = st.text_input("目标词", key="add_mem_target_rp").strip()
                if target:
                    rule_payload = {"target": target}
                    auto_content = f"必须包含「{target}」"
            elif kind_choice.startswith("最大字数"):
                rule_kind = "max_len"
                col_s, col_n = st.columns([1, 1])
                with col_s:
                    scope_target = st.selectbox(
                        "作用范围", ["标题", "正文", "开头"], key="add_mem_scope_target",
                    )
                with col_n:
                    n_chars = st.number_input(
                        "字数上限", min_value=1, max_value=2000, value=20,
                        step=1, key="add_mem_n",
                    )
                rule_payload = {"scope": scope_target, "n": int(n_chars)}
                auto_content = f"{scope_target}不超过 {int(n_chars)} 字"
            elif kind_choice.startswith("禁用正则"):
                rule_kind = "forbidden_regex"
                pattern = st.text_input(
                    "正则表达式",
                    key="add_mem_pattern",
                    help="可用内联标志：(?i) 不区分大小写。例：(?i)\\d{4}年",
                ).strip()
                if st.button(
                    "🧪 测试编译", key="add_mem_pattern_test", use_container_width=True,
                ):
                    if not pattern:
                        st.warning("请先填正则。")
                    else:
                        try:
                            import re as _re
                            _re.compile(pattern)
                            st.success("正则编译通过。")
                        except _re.error as exc:
                            st.error(f"正则编译失败：{exc}")
                if pattern:
                    rule_payload = {"pattern": pattern}
                    auto_content = f"禁止匹配正则「{pattern}」"
            else:
                rule_kind = "free_text"

        # 内容字段：结构化 kind 已自动生成可读描述。Streamlit 的 keyed widget
        # 一旦写入 session_state 就会忽略后续的 value=，所以这里在渲染前主动
        # 同步：当用户切换 kind / 改 payload 导致 auto_content 变化时，把新值
        # 灌进 session_state["add_mem_content"]，避免 content 与 rule_payload
        # 长期错位（比如 payload 改成"新"但 content 还显示"禁止『最』"）。
        content_default = auto_content if auto_content else ""
        _last_auto_key = "_add_mem_auto_content_last"
        if auto_content and st.session_state.get(_last_auto_key) != auto_content:
            st.session_state["add_mem_content"] = auto_content
            st.session_state[_last_auto_key] = auto_content
        elif not auto_content:
            # 切回自由文本时让用户继续手填
            st.session_state.pop(_last_auto_key, None)
        content = st.text_input(
            "记忆内容（可读描述）",
            value=content_default,
            placeholder="例：标题带数字点击率更高",
            key="add_mem_content",
        )

        if st.button("➕ 添加", key="add_mem_submit", use_container_width=True):
            _do_save_memory = False
            if not content.strip():
                st.warning("请填写记忆内容。")
            elif is_hard and rule_kind not in (None, "free_text") and rule_payload is None:
                st.warning("结构化硬规则的目标字段不能为空。")
            elif is_hard and rule_kind == "forbidden_regex":
                # 保存前再编译一次：用户可能没点"测试编译"就直接提交。否则
                # 入库后 validator.check_hard_rules 只会 silent continue（参见
                # validator.py:233），硬规则永久失效但用户无感知。
                import re as _re
                try:
                    _re.compile((rule_payload or {}).get("pattern", ""))
                    _do_save_memory = True
                except _re.error as exc:
                    st.error(f"正则无法编译，请先在「🧪 测试编译」里修复：{exc}")
            else:
                _do_save_memory = True

            if _do_save_memory:
                scope = "project" if scope_choice == "项目记忆" else "global"
                pid = project_id if scope == "project" else None
                try:
                    db.upsert_memory(
                        db_client, user_id,
                        scope=scope,
                        content=content.strip(),
                        source_feedback="手动添加",
                        project_id=pid,
                        auto_confirm_threshold=1,
                        severity="hard" if is_hard else "soft",
                        rule_kind=rule_kind if is_hard else None,
                        rule_payload=rule_payload if is_hard else None,
                    )
                except Exception as exc:
                    st.error(f"保存失败：{exc}")
                else:
                    st.success("记忆已添加。")
                    # 清掉本次填的字段，下次进入是干净状态。所有 add_mem_*
                    # 前缀都要清，否则 severity / scope / n / 上次 auto_content
                    # 会残留串到下一次添加。
                    for k in list(st.session_state.keys()):
                        if k.startswith("add_mem_") or k == _last_auto_key:
                            st.session_state.pop(k, None)
                    st.rerun()

    # 卡片 2：导出 / 导入 JSON
    with st.expander("📦 导出 / 导入（JSON）", expanded=False):
        col_export, col_import = st.columns(2)
        with col_export:
            all_memories = db.list_memories(db_client, user_id)
            if all_memories:
                export_data = [
                    {
                        "scope":           m.get("scope", ""),
                        "content":         m.get("content", ""),
                        "source_feedback": m.get("source_feedback", ""),
                        "frequency":       m.get("frequency", 1),
                        "status":          m.get("status", "candidate"),
                    }
                    for m in all_memories
                ]
                st.download_button(
                    label="⬇️ 导出全部记忆",
                    data=json.dumps(export_data, ensure_ascii=False, indent=2).encode("utf-8"),
                    file_name="memories_export.json",
                    mime="application/json",
                    use_container_width=True,
                )
            else:
                st.caption("暂无记忆可导出。")
        with col_import:
            uploaded = st.file_uploader("上传 JSON 文件", type=["json"], key="mem_import")
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

    # 卡片 3：embedding 补算（仅在配置了 GOOGLE_API_KEY 时显示）
    try:
        import dedup as _dedup
        embedding_ready = _dedup.embeddings_available()
    except Exception:
        embedding_ready = False
    if embedding_ready:
        with st.expander("🔄 给老规则补算 embedding（相关性筛选用）", expanded=False):
            st.caption(
                "新建/迭代得到的规则会自动算 embedding。系统升级之前的老规则没有 embedding，"
                "相关性筛选时会全部放行（不会丢失）。点下面的按钮分批补算，"
                "每次最多 50 条，可以重复点击。"
            )
            if st.button("立即补算（最多 50 条）", use_container_width=True, key="backfill_mem_embed"):
                # backfill_memory_embeddings 现在返回 dict，能区分"无需补"和
                # 各种真错误（schema 缺列 / 查询失败 / 没配 SDK）。之前一律
                # 显示"没有需要补算的记忆"，schema 缺列时用户也看到这个，
                # 还以为"点了没反应"。
                result = db.backfill_memory_embeddings(db_client, user_id, max_rows=50)
                status_code = result.get("status")
                if status_code == "ok":
                    n = result.get("updated", 0)
                    if n > 0:
                        st.success(f"已补算 {n} 条。")
                    else:
                        st.info("查到候选记忆但补算 0 条（embedding API 返回为空）。")
                elif status_code == "noop":
                    st.info("没有需要补算的记忆——所有记忆都已有 embedding。")
                elif status_code == "no_embedding_sdk":
                    st.warning("Embedding SDK 不可用（未配 GOOGLE_API_KEY 或 google-genai 未安装）。")
                elif status_code == "schema_missing":
                    st.error(
                        f"⚠ {result.get('hint', '数据库缺列')}。\n\n"
                        "去 Supabase SQL Editor 运行：\n"
                        "```sql\n"
                        "CREATE EXTENSION IF NOT EXISTS vector;\n"
                        "ALTER TABLE memories ADD COLUMN IF NOT EXISTS embedding vector(768);\n"
                        "ALTER TABLE versions ADD COLUMN IF NOT EXISTS embedding vector(768);\n"
                        "```"
                    )
                else:
                    st.error(f"补算失败：{result.get('error', '未知错误')}")
                st.rerun()


def _render_memory_table(
    db_client: Client,
    user_id: str,
    scope: str,
    project_id: Optional[str],
    label: str,
    current_project_id: Optional[str] = None,
) -> None:
    """列出某个范围（通用 / 项目）下的全部规则。

    布局：已确认 → 候选 两个分段；已确认行按 hard-first 排序，让最重要的
    硬约束总是出现在最上面。
    """
    memories = db.list_memories(db_client, user_id, scope=scope, project_id=project_id)
    if not memories:
        st.info(f"暂无{label}。")
        return

    confirmed  = [m for m in memories if m["status"] == "confirmed"]
    candidates = [m for m in memories if m["status"] == "candidate"]
    confirmed.sort(
        key=lambda m: (
            0 if (m.get("severity") or "soft").lower() == "hard" else 1,
            -int(m.get("frequency") or 0),
            str(m.get("created_at") or ""),
        )
    )

    if confirmed:
        st.markdown("**✅ 已确认**")
        for m in confirmed:
            _render_memory_row(
                db_client, m, show_confirm=False,
                current_project_id=current_project_id,
            )

    if candidates:
        st.markdown("**⏳ 候选中**（出现次数不足，未生效）")
        for m in candidates:
            _render_memory_row(
                db_client, m, show_confirm=True,
                current_project_id=current_project_id,
            )


def _safe_update_memory(
    db_client: Client, memory_id: str, updates: dict
) -> tuple[bool, str]:
    """对 db.update_memory 的兼容封装：旧部署可能还没运行新列迁移。

    返回 (是否成功, 提示文案)。若新列不存在，会剥离新列重试，让基础操作
    仍然成功，并把提示文案显示给用户（让 ta 知道要去 Supabase 跑迁移）。
    """
    try:
        db.update_memory(db_client, memory_id, updates)
        return True, ""
    except Exception as exc:
        msg = str(exc)
        new_cols = {
            "severity", "applicability", "muted_until",
            "rule_kind", "rule_payload",
        }
        # 只有错误消息里**同时**出现 PostgREST 列缺失的明确信号（"column ..."
        # 或 "does not exist"）且包含新列名时才剥列重试。之前裸 substring 命中
        # 会把权限/网络/序列化错误也当作"老部署没迁移"，剥掉用户实际填的字段
        # 后悄悄写一半。
        looks_like_schema_drift = (
            ("column" in msg.lower() or "does not exist" in msg.lower())
            and any(col in msg for col in new_cols)
        )
        if looks_like_schema_drift:
            stripped = {k: v for k, v in updates.items() if k not in new_cols}
            if stripped:
                try:
                    db.update_memory(db_client, memory_id, stripped)
                except Exception:
                    pass
            return False, "数据库还没运行新列迁移（severity / applicability / muted_until / rule_kind / rule_payload）"
        return False, msg[:120]


# popover 是 Streamlit 1.32+ 的能力；旧版本回退到 expander
_POPOVER = getattr(st, "popover", None)


def _render_memory_row(
    db_client: Client,
    memory: dict,
    show_confirm: bool,
    current_project_id: Optional[str] = None,
) -> None:
    """渲染一条记忆。主视觉：徽章 + 内容 + 频率 + 主操作按钮。

    主操作（直接显示在行尾）：
      - "✓" 确认  （仅候选行）
      - "🗑" 删除  （所有行）

    进阶操作（收纳在行末 "⚙" popover 里，平时不占空间）：
      - severity 硬↔偏 切换
      - 24h 静音 / 解除
      - 转为调校笔记（仅项目记忆 + 项目上下文存在时）

    徽章用 emoji + 颜色块，扫一眼就能区分；不用文字 badge 是为了节省横向空间。
    """
    import html as _html
    from datetime import datetime, timedelta, timezone

    mem_id = memory["id"]
    severity = (memory.get("severity") or "soft").lower()
    applicability = (memory.get("applicability") or "").strip()
    # 用 db.is_memory_muted_now 统一判断（解析 aware UTC datetime 后比较）。
    # 之前 UI 用 ``str(muted_until) > datetime.utcnow().isoformat()`` 字符串
    # 字典序比较，写入端是 aware ISO 而读取端是 naive ISO，边界条件下静音
    # "刚到期"会判错；与 db._is_muted 也不完全一致。
    is_muted = db.is_memory_muted_now(memory.get("muted_until"))

    # ── 渲染主行：徽章 + 内容 + 频率 + 主操作按钮 ─────────────────────
    badge_html = ""
    if severity == "hard":
        badge_html += (
            "<span style='background:#FFE4E1;color:#B22222;padding:1px 6px;"
            "border-radius:3px;font-size:11px;margin-right:6px'>🔒 硬</span>"
        )
    if applicability:
        badge_html += (
            f"<span style='background:#F5F5F5;color:#666;padding:1px 6px;"
            f"border-radius:3px;font-size:11px;margin-right:6px'>"
            f"{_html.escape(applicability)}</span>"
        )
    if is_muted:
        badge_html += (
            "<span style='background:#FFF8DC;color:#8B4513;padding:1px 6px;"
            "border-radius:3px;font-size:11px;margin-right:6px'>🔕 静音</span>"
        )

    col_main, col_freq, col_more, col_confirm, col_del = st.columns([8, 1, 1, 1, 1])
    with col_main:
        safe_content = _html.escape(memory["content"])
        safe_source = _html.escape((memory.get("source_feedback") or "")[:30])
        st.markdown(
            f"{badge_html}{safe_content} "
            f"<small style='color:grey'>· 来源：{safe_source}</small>",
            unsafe_allow_html=True,
        )
    with col_freq:
        st.caption(f"×{memory['frequency']}")
    with col_more:
        _render_advanced_actions(
            db_client, memory, severity, is_muted, current_project_id
        )
    with col_confirm:
        if show_confirm:
            if st.button("✓", key=f"confirm_{mem_id}", help="确认这条候选规则，使其生效"):
                db.update_memory(db_client, mem_id, {"status": "confirmed"})
                st.rerun()
    with col_del:
        if st.button("🗑", key=f"del_mem_{mem_id}", help="永久删除这条规则"):
            db.delete_memory(db_client, mem_id)
            st.rerun()


def _render_advanced_actions(
    db_client: Client,
    memory: dict,
    severity: str,
    is_muted: bool,
    current_project_id: Optional[str],
) -> None:
    """单条记忆的进阶操作菜单（"⚙" popover）。

    包含 3 个不常用但偶尔需要的功能：升降 severity、24h 静音、转调校笔记。
    这些之前散在主行里 5 个按钮一字排开，太挤；折叠后只有点开才出现。
    """
    from datetime import datetime, timedelta, timezone
    mem_id = memory["id"]

    # 旧版 Streamlit 没有 popover 时回退到 expander，效果稍逊但功能不变
    container_factory = (
        _POPOVER("⚙", help="进阶设置") if _POPOVER is not None
        else st.expander("⚙ 设置", expanded=False)
    )
    with container_factory:
        # ── severity 切换 ────────────────────────────────────────────
        if severity == "hard":
            st.caption("当前为 🔒 硬约束（每条必须 100% 满足）")
            if st.button("降级为偏好", key=f"sev_soft_{mem_id}", use_container_width=True):
                ok, hint = _safe_update_memory(db_client, mem_id, {"severity": "soft"})
                if not ok and hint:
                    st.warning(hint)
                st.rerun()
        else:
            st.caption("当前为偏好（按相关性 + 数量上限注入）")
            if st.button("升级为 🔒 硬约束", key=f"sev_hard_{mem_id}", use_container_width=True,
                         help="标为硬约束后会进入 P0 优先级，每条必满足；只用于合规/品牌底线"):
                ok, hint = _safe_update_memory(db_client, mem_id, {"severity": "hard"})
                if not ok and hint:
                    st.warning(hint)
                st.rerun()

        st.divider()

        # ── 24h 静音 ────────────────────────────────────────────────
        if is_muted:
            if st.button("🔔 解除静音", key=f"unmute_{mem_id}", use_container_width=True):
                ok, hint = _safe_update_memory(db_client, mem_id, {"muted_until": None})
                if not ok and hint:
                    st.warning(hint)
                st.rerun()
        else:
            if st.button("🔕 静音 24 小时", key=f"mute_{mem_id}", use_container_width=True,
                         help="临时禁用 24 小时（不删除；倒计时结束自动恢复）"):
                until = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
                ok, hint = _safe_update_memory(db_client, mem_id, {"muted_until": until})
                if not ok and hint:
                    st.warning(hint)
                st.rerun()

        # ── 转为调校笔记（误归 rule 的反馈降级到 taste）────────────────
        if current_project_id:
            st.divider()
            if st.button("→ 转为调校笔记", key=f"to_taste_{mem_id}", use_container_width=True,
                         help="如果这条更像感受性偏好而不是硬规则，可以转到调校笔记。会从规则库移除。"):
                _append_new_observations(
                    db_client, current_project_id, [memory["content"]],
                    source="user_manual",
                )
                db.delete_memory(db_client, mem_id)
                st.success("已转为调校笔记。")
                st.rerun()


def _render_inject_preview(
    db_client: Client,
    user_id: str,
    project_id: Optional[str],
    project_name: str,
) -> None:
    """折叠预览：本次生成实际会注入哪些规则。

    用途：当模型表现不符合预期时（"为什么这条规则没生效？"或"它怎么又用了这条？"），
    展开这块就能看到 P0/P1/P2 三层各有哪些条目。不需要去翻 system_prompt。
    """
    with st.expander("📋 预览本次会注入的规则", expanded=False):
        if not project_id:
            st.caption("选择一个项目后才能预览。")
            return

        try:
            global_mems, project_mems = db.get_confirmed_memories(
                db_client, user_id, project_id=project_id
            )
            session_instr = db.get_session_instructions(
                db_client, user_id, project_id=project_id
            )
        except Exception as exc:
            st.warning(f"读取记忆失败：{exc}")
            return

        # 按 severity 拆分：hard 进 P0、soft 进 P1；session 单独是 P2
        def _split(mems: list[dict]) -> tuple[list[dict], list[dict]]:
            hard = [m for m in mems if (m.get("severity") or "soft").lower() == "hard"]
            soft = [m for m in mems if (m.get("severity") or "soft").lower() != "hard"]
            return hard, soft

        hg, sg = _split(global_mems)
        hp, sp = _split(project_mems)
        total_hard    = len(hg) + len(hp)
        total_soft    = len(sg) + len(sp)
        total_session = len([s for s in (session_instr or []) if s.get("content")])

        st.caption(
            f"P0 硬约束 {total_hard} 条 · P1 偏好 {total_soft} 条 · "
            f"P2 会话指令 {total_session} 条"
        )

        if total_hard:
            st.markdown("**🔒 P0 硬约束**（100% 必须满足）")
            for m in hg:
                st.markdown(f"- 〔通用〕{m['content']}")
            for m in hp:
                st.markdown(f"- 〔项目〕{m['content']}")
        if total_soft:
            st.markdown("**P1 软偏好**（相关时应用）")
            for m in sg:
                st.markdown(f"- 〔通用〕{m['content']}")
            for m in sp:
                st.markdown(f"- 〔项目〕{m['content']}")
        if total_session:
            st.markdown("**⏱ P2 会话临时指令**（本批次有效）")
            for s in session_instr or []:
                if s.get("content"):
                    st.markdown(f"- {s['content']}")
        if not (total_hard or total_soft or total_session):
            st.caption("当前没有任何规则会被注入。")
