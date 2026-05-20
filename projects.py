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
import generator as gen_module
import image_handler
import memory as mem_module


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
        st.markdown(
            "<div class='nav-heading'>▸ PROJECT</div>",
            unsafe_allow_html=True,
        )

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

        if st.button("➕ 新建项目", use_container_width=True):
            st.session_state["show_new_project_form"] = True

        if st.session_state.get("show_new_project_form"):
            _render_new_project_form(client, user_id)

        return selected_project


def _render_new_project_form(client: Client, user_id: str) -> None:
    with st.form("new_project_form", clear_on_submit=True):
        st.markdown(
            "<div class='nav-heading' style='margin:0 0 0.5rem'>▸ NEW PROJECT</div>",
            unsafe_allow_html=True,
        )
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

    tab_basic, tab_prompt, tab_tactics, tab_roles, tab_files = st.tabs(
        ["基本信息", "System Prompt", "战术方向", "角色池", "参考文件"]
    )

    with tab_basic:
        _render_basic_settings(client, project)

    with tab_prompt:
        _render_prompt_settings(client, project)

    with tab_tactics:
        _render_tactics_settings(client, project)

    with tab_roles:
        _render_roles_settings(client, project)

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

        st.markdown("---")
        st.markdown("**跨批去重严格度**")
        st.caption(
            "三档预设覆盖大多数场景。新品类/同质化任务建议先用「严格」压重复，"
            "稳定后再放宽。未设置（NULL）时回退到全局默认（环境变量 `DEDUP_SEMANTIC_THRESHOLD`，标准 0.92）。"
        )
        cur_threshold = project.get("semantic_dedup_threshold")
        preset_options = ["项目默认", "宽松 0.88", "标准 0.92", "严格 0.95", "自定义"]
        if cur_threshold is None:
            cur_preset = "项目默认"
        elif abs(cur_threshold - 0.88) < 1e-6:
            cur_preset = "宽松 0.88"
        elif abs(cur_threshold - 0.92) < 1e-6:
            cur_preset = "标准 0.92"
        elif abs(cur_threshold - 0.95) < 1e-6:
            cur_preset = "严格 0.95"
        else:
            cur_preset = "自定义"
        preset = st.radio(
            "严格度预设",
            preset_options,
            index=preset_options.index(cur_preset),
            horizontal=True,
            key=f"sdt_preset_{project['id']}",
            label_visibility="collapsed",
        )
        custom_threshold = None
        # 切回非自定义档时清掉残留的 slider 值，否则下次再切回"自定义"会显示
        # 上次手填的数字（不是 DB 里的 cur_threshold），用户以为没保存。
        if preset != "自定义":
            st.session_state.pop(f"sdt_slider_{project['id']}", None)
        if preset == "自定义":
            custom_threshold = st.slider(
                "自定义阈值",
                min_value=0.85, max_value=0.95,
                value=float(cur_threshold) if cur_threshold is not None else 0.92,
                step=0.01,
                key=f"sdt_slider_{project['id']}",
            )

        st.markdown("---")
        st.markdown("**队列默认策略**")
        st.caption(
            "稳定优先：阈值上调至 0.95、自动重生开启、重试 3 次（重复率最低，速度慢）。"
            "吞吐优先：默认阈值、自动重生关闭（速度最快，重复率可能上升）。"
            "未设置时单条计划可在队列页临时覆盖。"
        )
        strategy_options = ["未设置", "稳定优先 (stable)", "吞吐优先 (throughput)"]
        cur_strategy = project.get("queue_strategy")
        if cur_strategy == "stable":
            strategy_idx = 1
        elif cur_strategy == "throughput":
            strategy_idx = 2
        else:
            strategy_idx = 0
        strategy = st.selectbox(
            "队列策略",
            strategy_options,
            index=strategy_idx,
            key=f"qs_default_{project['id']}",
            label_visibility="collapsed",
        )

        submitted = st.form_submit_button("保存基本信息")
    if submitted:
        if preset == "项目默认":
            resolved_threshold = None
        elif preset == "宽松 0.88":
            resolved_threshold = 0.88
        elif preset == "标准 0.92":
            resolved_threshold = 0.92
        elif preset == "严格 0.95":
            resolved_threshold = 0.95
        else:
            resolved_threshold = float(custom_threshold) if custom_threshold else None

        resolved_strategy = (
            None if strategy == "未设置"
            else ("stable" if strategy.startswith("稳定") else "throughput")
        )

        db.update_project(client, project["id"], {
            "name": name,
            "brand": brand,
            "semantic_dedup_threshold": resolved_threshold,
            "queue_strategy": resolved_strategy,
        })
        st.success("已保存。")
        st.rerun()


def _render_prompt_settings(client: Client, project: dict) -> None:
    st.markdown(
        "系统提示词由两个模块组成，生成时会自动拼接（语态 → 执行），并注入项目记忆。"
        "每个模块都可以直接粘贴文本，也可以上传 `.md` 文件。"
    )

    # ── Backward-compat: if new fields are empty but legacy field has content,
    #    pre-fill execution module from the legacy field so existing projects
    #    don't lose their prompts on first open.
    tone_default = project.get("system_prompt_tone") or ""
    exec_default = project.get("system_prompt_exec") or ""
    if not tone_default and not exec_default:
        exec_default = project.get("system_prompt") or ""

    # ── 语态校准模块 ───────────────────────────────────────────────────────
    st.markdown("#### 🎙️ 语态校准模块")
    st.caption("定义品牌语气、人设、句式风格等语言层面的校准规则。")

    # session_state key 带 project_id 防止跨项目串数据。之前 ``tone_textarea``
    # / ``exec_textarea`` 是全局 key，setdefault 切项目不重新初始化 → 用户看
    # 到"原有信息被清空"（实际是别的项目的内容覆盖了显示，DB 里没动）。
    _pid = project["id"]
    _tone_key = f"tone_textarea_{_pid}"
    _exec_key = f"exec_textarea_{_pid}"
    _tone_upload_key = f"tone_md_upload_{_pid}"
    _exec_upload_key = f"exec_md_upload_{_pid}"

    st.session_state.setdefault(_tone_key, tone_default)

    tone_upload = st.file_uploader(
        "从 .md 文件导入语态模块",
        type=["md", "txt"],
        key=_tone_upload_key,
    )
    if tone_upload is not None:
        try:
            st.session_state[_tone_key] = tone_upload.read().decode("utf-8")
            st.success(f"已读取：{tone_upload.name}（{len(st.session_state[_tone_key])} 字符）")
        except Exception as e:
            st.error(f"读取失败：{e}")

    tone_text = st.text_area(
        "语态校准提示词",
        height=280,
        placeholder="在此粘贴语态校准 Prompt，或通过上方上传 .md 文件...",
        key=_tone_key,
    )

    st.divider()

    # ── 执行模块 ──────────────────────────────────────────────────────────
    st.markdown("#### 🎯 执行模块")
    st.caption("定义内容结构、选题逻辑、具体写作指令等内容层面的执行规则。")

    st.session_state.setdefault(_exec_key, exec_default)

    exec_upload = st.file_uploader(
        "从 .md 文件导入执行模块",
        type=["md", "txt"],
        key=_exec_upload_key,
    )
    if exec_upload is not None:
        try:
            st.session_state[_exec_key] = exec_upload.read().decode("utf-8")
            st.success(f"已读取：{exec_upload.name}（{len(st.session_state[_exec_key])} 字符）")
        except Exception as e:
            st.error(f"读取失败：{e}")

    exec_text = st.text_area(
        "执行模块提示词",
        height=280,
        placeholder="在此粘贴执行模块 Prompt，或通过上方上传 .md 文件...",
        key=_exec_key,
    )

    # ── Preview of combined prompt ────────────────────────────────────────
    combined = ""
    if tone_text.strip() and exec_text.strip():
        combined = tone_text.strip() + "\n\n" + exec_text.strip()
    elif tone_text.strip():
        combined = tone_text.strip()
    else:
        combined = exec_text.strip()

    with st.expander(f"👁️ 预览完整 System Prompt（{len(combined)} 字符）", expanded=False):
        st.code(combined, language="markdown")

    # 保护：tone + exec 都为空时禁用保存按钮，防止误点把原 system_prompt 写空。
    _both_empty = not tone_text.strip() and not exec_text.strip()
    if _both_empty:
        st.caption("⚠ 语态模块和执行模块都是空白，保存会清掉项目的 System Prompt。")
    if st.button(
        "💾 保存 System Prompt",
        use_container_width=True, type="primary",
        disabled=_both_empty,
    ):
        try:
            db.update_project(client, project["id"], {
                "system_prompt_tone": tone_text,
                "system_prompt_exec": exec_text,
                "system_prompt": combined,
            })
            st.success("System Prompt 已保存。生成时将使用合并后的完整版本。")
        except Exception as exc:
            st.error(f"保存失败：{exc}")

    # ── 调校笔记 ──────────────────────────────────────────────────────────
    st.divider()
    st.markdown("#### 📝 调教笔记")
    st.caption(
        "AI 基于你每轮审稿互动（通过了什么、改掉了什么、怎么改的）自动提炼的品味积累，"
        "不是你手写的规则，而是 AI 对你审美倾向的内化认知。"
        "在「审核与迭代」页点击「🧠 更新调教笔记」自动生成，每次生成时原文注入 System Prompt。"
        "也可在此手动补充或微调。"
    )
    # session_state key 必须带 project_id —— 之前 ``calibration_notes_textarea``
    # 是全局 key，切项目时 setdefault 看到已存在不更新，导致项目 B 的笔记框
    # 显示项目 A 的内容（A 是空就显示空），而下方"变更历史"是直接查 audit
    # 表的，显示项目 B 真实的历史 —— 用户看到"框空但历史有 N 条"的诡异状态，
    # 实际 DB 里数据完好，只是 UI 串了。其它"原有信息被清空"投诉同理。
    _calib_key = f"calibration_notes_textarea_{project['id']}"
    st.session_state.setdefault(_calib_key, project.get("calibration_notes") or "")
    calibration_text = st.text_area(
        "调教笔记",
        height=200,
        placeholder="（空白时由审稿页「🧠 更新调教笔记」自动生成）",
        key=_calib_key,
        label_visibility="collapsed",
    )

    col_save, col_tidy = st.columns([1, 1])
    with col_save:
        if st.button("💾 保存调教笔记", use_container_width=True):
            try:
                mem_module.save_calibration_notes(
                    client, project["id"], calibration_text, source="user_manual",
                )
                st.success("调教笔记已保存，下次生成时生效。")
            except Exception as exc:
                st.error(f"保存失败：{exc}。请稍后重试，或查看日志定位问题。")
    with col_tidy:
        if st.button(
            "🧹 让 AI 整理（抽象化机械条目）",
            use_container_width=True,
            help="把'X → Y'这种机械替换条目重写成「方向 + 例子 + 原因」三层结构的方向性观察。预览后可选择保存。",
        ):
            with st.spinner("AI 正在整理笔记…"):
                cleaned = mem_module.simplify_calibration_notes(calibration_text)
            if not cleaned:
                st.info("没有明显可抽象的条目，笔记保持不变。")
            else:
                st.session_state["_calib_tidy_preview"] = cleaned
                st.rerun()

    tidy_key = "_calib_tidy_preview"
    if tidy_key in st.session_state:
        st.markdown("---")
        st.markdown("<div class='section-label'>整理后预览（可编辑再保存）</div>", unsafe_allow_html=True)
        preview = st.text_area(
            "整理后的调教笔记",
            value=st.session_state[tidy_key],
            height=240,
            key=f"calib_tidy_edit_{project['id']}",
            label_visibility="collapsed",
        )
        c1, c2 = st.columns(2)
        with c1:
            if st.button("✅ 用整理后的版本替换", use_container_width=True):
                try:
                    mem_module.save_calibration_notes(
                        client, project["id"], preview, source="user_manual",
                    )
                except Exception as exc:
                    st.error(f"保存失败：{exc}。整理后的内容保留在预览区，可重试。")
                else:
                    # Drop the textarea's cached state — Streamlit forbids
                    # assigning to a widget's session_state key after the widget
                    # has already rendered in this run.  Popping is allowed, and
                    # the next rerun will repopulate via ``setdefault`` using the
                    # freshly-saved project.calibration_notes value.
                    st.session_state.pop("calibration_notes_textarea", None)
                    st.session_state.pop(tidy_key, None)
                    st.success("已替换并保存。")
                    st.rerun()
        with c2:
            if st.button("✕ 放弃整理结果", use_container_width=True):
                del st.session_state[tidy_key]
                st.rerun()

    # ── 审计：调教笔记的写入历史（C1 + 2026-05 分页）─────────────────────
    # 用途：当用户怀疑"为什么这条观察突然不见了 / 多出来了"时，可以
    # 直接看 before/after diff 定位是哪个入口（迭代反馈 / 手动精修 /
    # 批次反思 / 用户手动保存）写入的。
    #
    # 2026-05 改造：分页 + 总数指示，从根本上消除"窗口截断 → 看似丢失"
    # 的体感问题——被 4000 字软上限淘汰的旧行依然保留在历史的 before_text
    # 里，用户可以按时间滚回去看。
    pid = project["id"]
    total = db.count_calibration_audit(client, pid)
    page_size = 30
    cursor_key = f"_calib_hist_cursor_{pid}"
    accum_key = f"_calib_hist_accum_{pid}"

    label_suffix = f"（共 {total} 条）" if total else "（启用审计后累积）"
    with st.expander(f"📜 查看变更历史{label_suffix}", expanded=False):
        if total == 0:
            st.caption("尚无历史记录。")
        else:
            accumulated: list[dict] = st.session_state.get(accum_key, [])
            # 缓存失效：当 DB 总数 > 已缓存累积，说明本次访问期间有新写入
            # （别的入口保存了笔记 / 太子学习落库等），重新拉第一页。否则
            # 用户保存完笔记后看不到"为什么我刚加的不见了"，因为"加载更早"
            # 的游标只往 created_at 更小走，新写入永远拉不进来。
            if accumulated and len(accumulated) < total:
                accumulated = db.list_calibration_audit(client, pid, limit=page_size)
                st.session_state[accum_key] = accumulated
                st.session_state[cursor_key] = (
                    accumulated[-1].get("created_at") if accumulated else None
                )
            elif not accumulated:
                accumulated = db.list_calibration_audit(client, pid, limit=page_size)
                st.session_state[accum_key] = accumulated
                st.session_state[cursor_key] = (
                    accumulated[-1].get("created_at") if accumulated else None
                )

            for row in accumulated:
                source_label = {
                    "iteration":        "迭代反馈",
                    "manual_edit":      "手动精修",
                    "batch_reflection": "批次反思",
                    "merger_taste":     "反馈分类为 taste",
                    "user_manual":      "用户手动保存",
                }.get(row.get("source"), row.get("source") or "未知")
                created = (row.get("created_at") or "")[:19].replace("T", " ")
                appended = row.get("append_lines") or []
                before_text = row.get("before_text") or ""
                after_text = row.get("after_text") or ""

                st.markdown(
                    f"**{created}** · _{source_label}_ · 新增 {len(appended)} 条观察"
                )
                if appended:
                    for line in appended[:5]:
                        st.markdown(f"  - {line}")
                    if len(appended) > 5:
                        st.caption(f"…还有 {len(appended) - 5} 条")

                # 计算 before 中存在但 after 已无的行 —— 这就是窗口淘汰的旧行
                before_lines = {
                    l.strip().lstrip("-•·*· ").strip()
                    for l in before_text.splitlines() if l.strip()
                }
                after_lines = {
                    l.strip().lstrip("-•·*· ").strip()
                    for l in after_text.splitlines() if l.strip()
                }
                window_dropped = before_lines - after_lines
                if window_dropped:
                    with st.expander(
                        f"⏏ 本次被窗口淘汰 {len(window_dropped)} 条（仍可在更早的历史中回看）",
                        expanded=False,
                    ):
                        for line in list(window_dropped)[:20]:
                            st.markdown(f"  - {line}")
                        if len(window_dropped) > 20:
                            st.caption(f"…还有 {len(window_dropped) - 20} 条")
                st.divider()

            if len(accumulated) < total:
                if st.button(
                    f"⬇ 加载更早历史（已加载 {len(accumulated)} / {total}）",
                    key=f"calib_hist_more_{pid}",
                    use_container_width=True,
                ):
                    cursor = st.session_state.get(cursor_key)
                    older = db.list_calibration_audit(
                        client, pid, limit=page_size, before_ts=cursor,
                    )
                    if older:
                        accumulated.extend(older)
                        st.session_state[accum_key] = accumulated
                        st.session_state[cursor_key] = (
                            older[-1].get("created_at") or cursor
                        )
                        st.rerun()


def _render_tactics_settings(client: Client, project: dict) -> None:
    tactics = _parse_json_field(project.get("tactics"), [])
    st.markdown("配置本项目可用的战术方向。每个方向可附加独立的 Prompt 补充说明。可以删除全部战术方向，生成时将不使用战术方向。")

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
            if st.button("🗑️", key=f"t_rm_{i}"):
                # Auto-save with this tactic removed
                remaining = [
                    {"name": t.get("name", ""), "prompt_suffix": t.get("prompt_suffix", "")}
                    for j, t in enumerate(tactics) if j != i
                ]
                db.update_project(client, project["id"], {"tactics": json.dumps(remaining)})
                st.success(f"已删除战术方向「{tactic.get('name', '')}」")
                st.rerun()
        updated_tactics.append({"name": tname, "prompt_suffix": tprompt})

    if not tactics:
        st.info("暂无战术方向。生成时将不应用任何战术方向。")

    col_add, col_save = st.columns(2)
    with col_add:
        if st.button("➕ 添加战术方向"):
            new_tactics = updated_tactics + [{"name": "新战术方向", "prompt_suffix": ""}]
            db.update_project(client, project["id"], {"tactics": json.dumps(new_tactics)})
            st.rerun()
    with col_save:
        if st.button("💾 保存战术方向"):
            db.update_project(client, project["id"], {"tactics": json.dumps(updated_tactics)})
            st.success("战术方向已保存。")
            st.rerun()


def _render_file_settings(client: Client, project: dict, user_id: str) -> None:
    st.markdown("上传参考文件（产品图、竞品截图、数据表格、文档等）存储在云端，供生成时参考。")

    uploaded = st.file_uploader(
        "上传参考文件",
        type=["jpg", "jpeg", "png", "webp", "pdf", "txt", "md",
              "csv", "xlsx", "xls", "docx", "doc", "zip"],
        accept_multiple_files=True,
    )
    if uploaded and st.button("📤 上传所选文件"):
        ref_files = _parse_json_field(project.get("reference_files"), [])
        for f in uploaded:
            try:
                # 用与 image_handler 相同的安全策略：转义 + 随机后缀，避免同名
                # 重复上传时撞 Supabase Storage 的 409。原始 name 仍保留在
                # metadata.name 供 UI 展示，storage_name 用于后续清理 / 审计。
                storage_name = image_handler._safe_storage_name(
                    f.name, mime_type=f.type,
                )
                path = f"projects/{project['id']}/{storage_name}"
                client.storage.from_("reference-files").upload(
                    path, f.read(), file_options={"content-type": f.type}
                )
                public_url = client.storage.from_("reference-files").get_public_url(path)
                ref_files.append({
                    "name":         f.name,
                    "storage_name": storage_name,
                    "url":          public_url,
                    "type":         f.type,
                })
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
            _ft = rf.get("name", "").rsplit(".", 1)[-1].lower()
            _icon = {
                "jpg": "🖼️", "jpeg": "🖼️", "png": "🖼️", "webp": "🖼️",
                "pdf": "📄", "txt": "📝", "md": "📝",
                "csv": "📊", "xlsx": "📊", "xls": "📊",
                "docx": "📃", "doc": "📃", "zip": "🗜️",
            }.get(_ft, "📎")
            with col1:
                st.markdown(f"{_icon} [{rf['name']}]({rf['url']})")
            with col2:
                if st.button("删除", key=f"del_ref_{idx}"):
                    ref_files.pop(idx)
                    db.update_project(
                        client, project["id"], {"reference_files": json.dumps(ref_files)}
                    )
                    st.rerun()


def _render_roles_settings(client: Client, project: dict) -> None:
    st.markdown(
        "配置三省法中书省使用的**角色池**。每次生成时从池中随机抽取 N 个角色起草，"
        "角色越多样、描述越具体，产出差异性越大。"
    )

    custom = _parse_json_field(project.get("custom_roles"), [])
    # If project has no custom roles configured, show the default pool as reference
    using_default = not custom
    pool = custom if custom else gen_module.CREATIVE_ROLES_POOL

    if using_default:
        st.info("当前使用默认角色池（6个内置角色）。修改后将覆盖默认配置，仅对此项目生效。")

    updated_roles = []
    for i, role in enumerate(pool):
        col1, col2, col3 = st.columns([2, 5, 1])
        with col1:
            rname = st.text_input(f"角色名 {i+1}", value=role.get("name", ""), key=f"r_name_{i}")
        with col2:
            rprompt = st.text_area(
                f"角色 Prompt {i+1}",
                value=role.get("prompt_suffix", ""),
                height=80,
                key=f"r_prompt_{i}",
            )
        with col3:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("🗑️", key=f"r_rm_{i}"):
                remaining = [
                    {"id": r.get("id", f"role_{j}"), "name": r.get("name", ""), "prompt_suffix": r.get("prompt_suffix", "")}
                    for j, r in enumerate(pool) if j != i
                ]
                db.update_project(client, project["id"], {"custom_roles": json.dumps(remaining)})
                st.success(f"已删除角色「{role.get('name', '')}」")
                st.rerun()
        updated_roles.append({
            "id": role.get("id", f"role_{i}"),
            "name": rname,
            "prompt_suffix": rprompt,
        })

    col_add, col_save, col_reset = st.columns(3)
    with col_add:
        if st.button("➕ 添加角色", use_container_width=True):
            new_roles = updated_roles + [{"id": f"custom_{len(updated_roles)}", "name": "新角色", "prompt_suffix": ""}]
            db.update_project(client, project["id"], {"custom_roles": json.dumps(new_roles)})
            st.rerun()
    with col_save:
        if st.button("💾 保存角色池", use_container_width=True):
            db.update_project(client, project["id"], {"custom_roles": json.dumps(updated_roles)})
            st.success("角色池已保存。")
            st.rerun()
    with col_reset:
        if st.button("↩️ 恢复默认池", use_container_width=True):
            db.update_project(client, project["id"], {"custom_roles": json.dumps([])})
            st.success("已恢复为默认角色池。")
            st.rerun()


# ── Tactic helpers ─────────────────────────────────────────────────────────

def get_tactic_names(project: dict) -> list[str]:
    """Return list of tactic names. Empty list means no tactics configured."""
    tactics = _parse_json_field(project.get("tactics"), [])
    return [t.get("name", "") for t in tactics if t.get("name")]


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
