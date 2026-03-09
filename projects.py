"""
Project management helpers for XHS Content Workstation.

Wraps db.py calls with higher-level logic:
  - project selector / switcher for Streamlit sidebar
  - reference-file upload to Supabase Storage
  - tactic configuration helpers
"""

from __future__ import annotations

import json
import os
import io
from typing import Optional

import streamlit as st
from supabase import Client

import db


# ── Streamlit session helpers ──────────────────────────────────────────────

def get_current_project_id() -> Optional[str]:
    return st.session_state.get("current_project_id")


def set_current_project(project_id: str) -> None:
    st.session_state["current_project_id"] = project_id


def get_current_project(client: Client) -> Optional[dict]:
    pid = get_current_project_id()
    if not pid:
        return None
    return db.get_project(client, pid)


# ── Sidebar project switcher ───────────────────────────────────────────────

def render_project_switcher(client: Client, user_id: str) -> Optional[dict]:
    """
    Render a project selector in the sidebar.
    Returns the currently selected project dict, or None.
    """
    projects = db.list_projects(client, user_id)

    with st.sidebar:
        st.markdown("### 📂 项目")

        if not projects:
            st.info("暂无项目，请创建第一个项目。")
            _render_new_project_form(client, user_id)
            return None

        project_names = [p["name"] for p in projects]
        project_ids = [p["id"] for p in projects]

        current_idx = 0
        current_pid = get_current_project_id()
        if current_pid and current_pid in project_ids:
            current_idx = project_ids.index(current_pid)

        selected_idx = st.selectbox(
            "切换项目",
            range(len(project_names)),
            format_func=lambda i: project_names[i],
            index=current_idx,
            label_visibility="collapsed",
        )
        selected_project = projects[selected_idx]
        set_current_project(selected_project["id"])

        col1, col2 = st.columns(2)
        with col1:
            if st.button("⚙️ 项目设置", use_container_width=True):
                st.session_state["show_project_settings"] = True
        with col2:
            if st.button("➕ 新建项目", use_container_width=True):
                st.session_state["show_new_project_form"] = True

        if st.session_state.get("show_new_project_form"):
            _render_new_project_form(client, user_id)

        return selected_project


def _render_new_project_form(client: Client, user_id: str) -> None:
    with st.form("new_project_form", clear_on_submit=True):
        st.markdown("#### 新建项目")
        name = st.text_input("项目名称 *", placeholder="例：RIO轻享 Q2")
        brand = st.text_input("品牌名称", placeholder="例：RIO")
        submitted = st.form_submit_button("创建", use_container_width=True)
    if submitted:
        if not name:
            st.error("项目名称不能为空。")
            return
        project = db.create_project(
            client, user_id, name=name, brand=brand,
            tactics=_default_tactics(),
        )
        set_current_project(project["id"])
        st.session_state.pop("show_new_project_form", None)
        st.success(f"项目「{name}」创建成功！")
        st.rerun()


# ── Project settings page ──────────────────────────────────────────────────

def render_project_settings(client: Client, project: dict, user_id: str) -> None:
    """Full-page project settings editor."""
    st.header(f"⚙️ 项目设置 — {project['name']}")

    tab_basic, tab_prompt, tab_tactics, tab_files = st.tabs(
        ["基本信息", "System Prompt", "战术方向", "参考文件"]
    )

    with tab_basic:
        _render_basic_settings(client, project)

    with tab_prompt:
        _render_prompt_settings(client, project)

    with tab_tactics:
        _render_tactics_settings(client, project)

    with tab_files:
        _render_file_settings(client, project, user_id)

    # Danger zone
    st.divider()
    with st.expander("⚠️ 危险操作"):
        st.warning("删除后无法恢复，包含的所有批次和文案也将被删除。")
        if st.button("🗑️ 删除此项目", type="primary"):
            db.delete_project(client, project["id"])
            st.session_state.pop("current_project_id", None)
            st.session_state.pop("show_project_settings", None)
            st.success("项目已删除。")
            st.rerun()


def _render_basic_settings(client: Client, project: dict) -> None:
    with st.form("basic_settings"):
        name = st.text_input("项目名称", value=project.get("name", ""))
        brand = st.text_input("品牌名称", value=project.get("brand", ""))
        submitted = st.form_submit_button("保存基本信息")
    if submitted:
        db.update_project(client, project["id"], {"name": name, "brand": brand})
        st.success("已保存。")
        st.rerun()


def _render_prompt_settings(client: Client, project: dict) -> None:
    st.markdown(
        "在此粘贴或输入项目的战术框架 Prompt（System Prompt）。"
        "生成时会自动注入记忆。"
    )
    with st.form("prompt_settings"):
        prompt = st.text_area(
            "System Prompt",
            value=project.get("system_prompt", ""),
            height=400,
            placeholder="在此粘贴您的战术框架提示词...",
        )
        submitted = st.form_submit_button("保存 System Prompt")
    if submitted:
        db.update_project(client, project["id"], {"system_prompt": prompt})
        st.success("System Prompt 已保存。")


def _render_tactics_settings(client: Client, project: dict) -> None:
    tactics = _parse_json_field(project.get("tactics"), [])
    st.markdown("配置本项目可用的战术方向。每个方向可附加独立的 Prompt 补充说明。")

    updated_tactics = []
    for i, tactic in enumerate(tactics):
        col1, col2, col3 = st.columns([3, 5, 1])
        with col1:
            tname = st.text_input(f"名称 {i+1}", value=tactic.get("name", ""), key=f"t_name_{i}")
        with col2:
            tprompt = st.text_input(
                f"Prompt 补充 {i+1}", value=tactic.get("prompt_suffix", ""), key=f"t_prompt_{i}"
            )
        with col3:
            remove = st.button("✕", key=f"t_rm_{i}")
        if not remove:
            updated_tactics.append({"name": tname, "prompt_suffix": tprompt})

    col_add, col_save = st.columns(2)
    with col_add:
        if st.button("➕ 添加战术方向"):
            updated_tactics.append({"name": "新战术方向", "prompt_suffix": ""})
    with col_save:
        if st.button("💾 保存战术方向"):
            db.update_project(client, project["id"], {"tactics": json.dumps(updated_tactics)})
            st.success("战术方向已保存。")
            st.rerun()


def _render_file_settings(client: Client, project: dict, user_id: str) -> None:
    st.markdown("上传参考文件（产品图、竞品截图等）存储在云端，供生成时使用。")

    uploaded = st.file_uploader(
        "上传参考图片",
        type=["jpg", "jpeg", "png", "webp", "pdf"],
        accept_multiple_files=True,
    )
    if uploaded and st.button("📤 上传所选文件"):
        ref_files = _parse_json_field(project.get("reference_files"), [])
        for f in uploaded:
            try:
                path = f"projects/{project['id']}/{f.name}"
                client.storage.from_("reference-files").upload(
                    path, f.read(), file_options={"content-type": f.type}
                )
                public_url = client.storage.from_("reference-files").get_public_url(path)
                ref_files.append({"name": f.name, "url": public_url, "type": f.type})
                st.success(f"✅ {f.name}")
            except Exception as e:
                st.error(f"上传失败：{f.name} — {e}")
        db.update_project(client, project["id"], {"reference_files": json.dumps(ref_files)})
        st.rerun()

    # Show existing files
    ref_files = _parse_json_field(project.get("reference_files"), [])
    if ref_files:
        st.markdown("**已上传的参考文件：**")
        for idx, rf in enumerate(ref_files):
            col1, col2 = st.columns([5, 1])
            with col1:
                st.markdown(f"📎 [{rf['name']}]({rf['url']})")
            with col2:
                if st.button("删除", key=f"del_ref_{idx}"):
                    ref_files.pop(idx)
                    db.update_project(
                        client, project["id"], {"reference_files": json.dumps(ref_files)}
                    )
                    st.rerun()


# ── Tactic helpers ─────────────────────────────────────────────────────────

def get_tactic_names(project: dict) -> list[str]:
    tactics = _parse_json_field(project.get("tactics"), [])
    names = [t.get("name", "") for t in tactics if t.get("name")]
    return names or ["默认战术"]


def get_tactic_prompt_suffix(project: dict, tactic_name: str) -> str:
    tactics = _parse_json_field(project.get("tactics"), [])
    for t in tactics:
        if t.get("name") == tactic_name:
            return t.get("prompt_suffix", "")
    return ""


# ── Misc helpers ───────────────────────────────────────────────────────────

def _default_tactics() -> list[dict]:
    return [
        {"name": "情感种草", "prompt_suffix": ""},
        {"name": "产品测评", "prompt_suffix": ""},
        {"name": "生活场景", "prompt_suffix": ""},
    ]


def _parse_json_field(value, default):
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default
