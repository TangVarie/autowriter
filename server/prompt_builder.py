"""System-prompt assembly.

Port of the pure-Python logic from memory.build_system_prompt (memory.py:27-83)
— reproduced here so the server module does not import streamlit/supabase.
The calibration_notes and custom_roles branches from the original are dropped
per the plan; the rest of the layout (order, headings, bullet formatting) is
preserved exactly so generation quality stays comparable.

Examples are expected as dicts with {"title": str, "body": str}.
Memories as dicts with {"content": str}.
"""

from __future__ import annotations


def build_system_prompt(
    base_prompt: str,
    global_memories: list[dict] | None = None,
    project_memories: list[dict] | None = None,
    tactic_suffix: str = "",
    positive_examples: list[dict] | None = None,
    negative_examples: list[dict] | None = None,
) -> str:
    parts: list[str] = [base_prompt.strip()]

    if tactic_suffix and tactic_suffix.strip():
        parts.append(f"\n{tactic_suffix.strip()}")

    if global_memories:
        bullets = "\n".join(f"• {m['content']}" for m in global_memories if m.get("content"))
        if bullets:
            parts.append(
                "\n---通用记忆（必须执行，每条都要主动检查）---\n" + bullets
            )

    if project_memories:
        bullets = "\n".join(f"• {m['content']}" for m in project_memories if m.get("content"))
        if bullets:
            parts.append(
                "\n---项目记忆（必须执行，每条都要主动检查）---\n" + bullets
            )

    if positive_examples:
        ex_blocks = []
        for ex in positive_examples[:5]:
            body_preview = (ex.get("body") or "")[:200].split("\n")[0]
            ex_blocks.append(
                f"标题：{ex.get('title', '')}\n正文节选：{body_preview}"
            )
        parts.append(
            "\n---优质正案例（学习这些文案的风格、结构和切入角度，这是我们想要的方向）---\n"
            + "\n\n".join(ex_blocks)
        )

    if negative_examples:
        ex_blocks = []
        for ex in negative_examples[:3]:
            body_preview = (ex.get("body") or "")[:120].split("\n")[0]
            ex_blocks.append(
                f"标题：{ex.get('title', '')}\n正文节选：{body_preview}"
            )
        parts.append(
            "\n---反面案例（分析这些文案存在的问题，生成时主动规避）---\n"
            + "\n\n".join(ex_blocks)
        )

    return "\n".join(parts)


def parse_examples_text(raw: str) -> list[dict]:
    """Parse a multi-line text field into [{"title":..., "body":...}, ...].

    Accepts two formats from a single Bitable "多行文本" cell:

      Format A (一行一条，标题 | 正文):
        好物分享 | 这支口红真的巨好用，显白显气质...
        通勤穿搭 | 西装+奶白短靴绝配...

      Format B (空行分隔的块，第一行标题，后续为正文):
        好物分享
        这支口红真的巨好用，显白显气质...

        通勤穿搭
        西装+奶白短靴绝配...
    """
    raw = (raw or "").strip()
    if not raw:
        return []

    results: list[dict] = []

    # Format A: every non-empty line contains `|`
    lines = [l for l in raw.splitlines() if l.strip()]
    if lines and all("|" in l for l in lines):
        for line in lines:
            title, _, body = line.partition("|")
            results.append({"title": title.strip(), "body": body.strip()})
        return results

    # Format B: split on blank lines
    for block in [b.strip() for b in raw.split("\n\n") if b.strip()]:
        block_lines = block.splitlines()
        title = block_lines[0].strip()
        body = "\n".join(block_lines[1:]).strip()
        results.append({"title": title, "body": body})
    return results
