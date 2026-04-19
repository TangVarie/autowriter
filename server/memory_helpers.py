"""Pure-LLM helpers for feedback classification and calibration-note generation.

Both are ports of the corresponding functions in the root ``memory.py`` (lines
99–134 classify_feedback, 199–258 generate_calibration_notes) but stripped of
streamlit / Supabase / db dependencies. They return plain values; storing the
results is the caller's job.

Model + API-key config comes from the root ``config.py`` so these helpers
reuse whatever CLAUDE_MODEL / ANTHROPIC_BASE_URL you've already set for
generator.py.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic
import config as _root_config


_CLASSIFY_SYSTEM = """你是一个内容运营助手，专门负责整理小红书文案反馈记忆。

用户会给你一条反馈意见。你需要：
1. 判断这条反馈属于：
   - "project"（项目记忆）：只与特定品牌/产品相关，例如"RIO不用'微醉'"
   - "global"（通用记忆）：适用于所有小红书文案的通用技巧，例如"标题带数字效果好"
2. 提炼出一条简洁的记忆内容（不超过50字的中文）

以 JSON 格式回复：{"scope": "project"|"global", "content": "记忆内容"}
只返回 JSON，不要有其他文字。"""


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


def _anthropic_client() -> anthropic.Anthropic:
    kwargs: dict[str, Any] = {"api_key": _root_config.ANTHROPIC_API_KEY}
    if _root_config.ANTHROPIC_BASE_URL:
        kwargs["base_url"] = _root_config.ANTHROPIC_BASE_URL
    return anthropic.Anthropic(**kwargs)


def classify_feedback(
    feedback_text: str,
    project_name: str = "",
) -> tuple[str, str]:
    """Classify a raw feedback string into (scope, distilled content).

    scope ∈ {"project", "global"}. On any failure falls back to
    ("project", feedback_text) so the caller always has a usable tuple.
    """
    feedback_text = (feedback_text or "").strip()
    if not feedback_text:
        return "project", ""
    if not _root_config.ANTHROPIC_API_KEY:
        return "project", feedback_text

    try:
        user_msg = feedback_text
        if project_name:
            user_msg = f"[当前项目：{project_name}]\n反馈：{feedback_text}"
        resp = _anthropic_client().messages.create(
            model=_root_config.CLAUDE_MODEL,
            max_tokens=256,
            system=_CLASSIFY_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip()
        parsed = json.loads(raw)
        scope = parsed.get("scope", "project")
        content = parsed.get("content", feedback_text)
        if scope not in ("project", "global"):
            scope = "project"
        return scope, str(content)
    except Exception:
        return "project", feedback_text


def generate_calibration_notes(
    *,
    project_name: str,
    existing_notes: str,
    approved_items: list[dict],
    iterated_items: list[dict],
) -> str:
    """Generate/update calibration notes from interaction history.

    ``approved_items`` / ``iterated_items`` are lists of dicts with:
      {"title": str, "body": str,
       "versions": [{"version_num": int, "title": str, "body": str, "feedback": str}, ...]}

    Returns updated calibration notes as plain text. If there is nothing to
    learn from, returns the existing notes unchanged.
    """
    if not _root_config.ANTHROPIC_API_KEY:
        return existing_notes

    sections: list[str] = []

    if approved_items:
        parts = []
        for i, item in enumerate(approved_items[:8], 1):
            title = item.get("title", "")
            body = (item.get("body", "") or "")[:200].split("\n")[0]
            parts.append(f"{i}. 标题：{title}\n   正文节选：{body}")
        sections.append("【已通过文案】\n" + "\n\n".join(parts))

    if iterated_items:
        chains = []
        for item in iterated_items[:6]:
            versions = sorted(
                item.get("versions", []) or [],
                key=lambda v: v.get("version_num", 0),
            )
            steps = []
            for v in versions:
                fb = v.get("feedback", "")
                title = v.get("title", "")
                body = (v.get("body", "") or "")[:100].split("\n")[0]
                vn = v.get("version_num", "?")
                if fb:
                    steps.append(f"  v{vn}《{title}》\n  → 反馈：{fb}")
                else:
                    steps.append(f"  v{vn}《{title}》正文：{body}")
            if steps:
                chains.append("\n".join(steps))
        if chains:
            sections.append("【迭代过程记录】\n" + "\n\n---\n".join(chains))

    if not sections:
        return existing_notes

    user_content = f"项目名称：{project_name}\n\n"
    if existing_notes and existing_notes.strip():
        user_content += f"现有调教笔记：\n{existing_notes.strip()}\n\n"
    user_content += "本次互动记录：\n" + "\n\n".join(sections) + "\n\n请生成更新后的调教笔记。"

    try:
        resp = _anthropic_client().messages.create(
            model=_root_config.CLAUDE_MODEL,
            max_tokens=1024,
            system=_CALIBRATION_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        return resp.content[0].text.strip()
    except Exception:
        return existing_notes
