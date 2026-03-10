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
) -> str:
    """
    Assemble the final system prompt in the documented order:
      1. Base system prompt (tactical framework)
      2. Tactic-specific suffix (if any)
      3. Global memories
      4. Project memories
      5. Calibration notes (qualitative observations, not rules)
      6. Positive examples (few-shot: what good looks like)
      7. Negative examples (few-shot: what to avoid)
    """
    parts: list[str] = [base_prompt.strip()]

    if tactic_suffix.strip():
        parts.append(f"\n{tactic_suffix.strip()}")

    if global_memories:
        bullets = "\n".join(f"• {m['content']}" for m in global_memories)
        parts.append(f"\n---通用记忆---\n{bullets}")

    if project_memories:
        bullets = "\n".join(f"• {m['content']}" for m in project_memories)
        parts.append(f"\n---项目记忆---\n{bullets}")

    if calibration_notes and calibration_notes.strip():
        parts.append(f"\n---调校笔记---\n{calibration_notes.strip()}")

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

    return "\n".join(parts)


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
    """Process a list of feedback strings (from a full review round)."""
    results = []
    for fb in feedbacks:
        if fb and fb.strip():
            result = ingest_feedback(
                db_client, user_id, fb.strip(),
                project_id=project_id, project_name=project_name
            )
            results.append(result)
    return results


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
