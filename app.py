"""
XHS Content Workstation — Main Streamlit Application
小红书内容自动化工作台 v2.0

Entry point: streamlit run app.py
"""

from __future__ import annotations

import json
from typing import Optional

import streamlit as st

import config
import auth
import db
import projects as proj_module
import memory as mem_module
import generator as gen_module
import image_handler
import exporter

# ── Page config ────────────────────────────────────────────────────────────
st.set_page_config(
    page_title=config.APP_TITLE,
    page_icon="🍵",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ─────────────────────────────────────────────────────────────
st.markdown(
    """
<style>
/* Copy card styles */
.copy-card {
    border: 1px solid #e0e0e0;
    border-radius: 12px;
    padding: 16px;
    margin-bottom: 12px;
    background: #fafafa;
}
.copy-title {
    font-size: 1.1rem;
    font-weight: 700;
    color: #1a1a1a;
}
.copy-meta {
    font-size: 0.75rem;
    color: #888;
    margin-top: 2px;
}
.tag {
    display: inline-block;
    background: #f0f0f0;
    border-radius: 4px;
    padding: 2px 8px;
    font-size: 0.75rem;
    margin-right: 4px;
}
.status-approved { color: #22c55e; font-weight: 600; }
.status-revision { color: #f59e0b; font-weight: 600; }
.status-pending  { color: #94a3b8; font-weight: 600; }
</style>
""",
    unsafe_allow_html=True,
)


# ── Authentication gate ────────────────────────────────────────────────────
db_client, current_user = auth.require_auth()
user_id: str = current_user["id"]


# ── Sidebar ────────────────────────────────────────────────────────────────
with st.sidebar:
    # User info + logout
    st.markdown(
        f"**👤 {current_user['email']}**  \n"
        f"<small style='color:grey'>v{config.APP_VERSION}</small>",
        unsafe_allow_html=True,
    )
    if st.button("退出登录", use_container_width=True):
        auth.sign_out()
        st.rerun()
    st.divider()

# Project switcher (also rendered in sidebar via projects module)
selected_project = proj_module.render_project_switcher(db_client, user_id)

with st.sidebar:
    st.divider()
    # Navigation
    page = st.radio(
        "导航",
        ["生成工作台", "审核与迭代", "记忆管理", "项目设置", "批次历史"],
        label_visibility="collapsed",
    )


# ── Route to pages ─────────────────────────────────────────────────────────

if selected_project is None and page not in ("项目设置",):
    st.info("👈 请先在左侧创建或选择一个项目，然后开始使用。")
    st.stop()


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: 生成工作台
# ═══════════════════════════════════════════════════════════════════════════

def page_generate(project: dict) -> None:
    st.title("✍️ 生成工作台")

    project_name = project.get("name", "")
    brand = project.get("brand", "")
    base_prompt = project.get("system_prompt", "")
    tactic_names = proj_module.get_tactic_names(project)

    # ── Left panel: generation controls ────────────────────────────────
    with st.sidebar:
        st.markdown("### 生成参数")

        tactic = st.selectbox("战术方向", tactic_names)
        count = st.slider("生成数量", 1, config.MAX_GENERATION_COUNT, config.DEFAULT_GENERATION_COUNT)

        engine_mode = st.radio(
            "AI 引擎模式",
            ["单引擎", "多引擎比稿"],
            help="多引擎比稿会同时用 Claude 和 Gemini 生成，便于对比。",
        )

        if engine_mode == "单引擎":
            selected_engine_raw = st.selectbox(
                "选择引擎",
                gen_module.AVAILABLE_ENGINES,
                format_func=lambda e: e.upper(),
            )
            engines = [selected_engine_raw]
        else:
            engines = gen_module.AVAILABLE_ENGINES[:2]
            if len(engines) < 2:
                st.warning("Gemini 未配置，将仅使用 Claude。")
                engines = ["claude"]

        with st.expander("⚙️ 高级参数"):
            target_audience = st.text_input("目标人群", placeholder="例：25-35岁职场女性")
            key_messages = st.text_input("核心卖点/关键词", placeholder="例：低度数、清爽、派对感")
            tone = st.text_input("语气偏好", placeholder="例：活泼口语化、朋友间分享")
            extra_instructions = st.text_area("补充说明", height=80, placeholder="其他要求...")

    # ── Image upload ──────────────────────────────────────────────────
    st.markdown("#### 参考图片（可选）")
    encoded_images = image_handler.render_image_uploader()

    # ── Memory status ─────────────────────────────────────────────────
    global_mems, project_mems = db.get_confirmed_memories(
        db_client, user_id, project_id=project["id"]
    )
    mem_count = len(global_mems) + len(project_mems)
    if mem_count > 0:
        st.info(f"🧠 已载入 {mem_count} 条确认记忆（{len(global_mems)} 通用 + {len(project_mems)} 项目）")

    # ── Generate button ────────────────────────────────────────────────
    if st.button("🚀 开始生成", type="primary", use_container_width=True):
        if not base_prompt.strip():
            st.warning("⚠️ 当前项目尚未配置 System Prompt，请先在「项目设置」中填写。")
            st.stop()

        # Build full system prompt
        tactic_suffix = proj_module.get_tactic_prompt_suffix(project, tactic)
        full_system_prompt = mem_module.build_system_prompt(
            base_prompt=base_prompt,
            global_memories=global_mems,
            project_memories=project_mems,
            tactic_suffix=tactic_suffix,
        )

        # Create batch record
        batch_params = {
            "target_audience": target_audience,
            "key_messages": key_messages,
            "tone": tone,
            "extra_instructions": extra_instructions,
        }
        batch = db.create_batch(
            db_client, user_id,
            project_id=project["id"],
            tactic=tactic,
            params=batch_params,
            ai_engines=engines,
        )
        batch_id = batch["id"]

        # Progress UI
        progress_bar = st.progress(0.0, text="正在初始化…")

        def update_progress(pct: float, msg: str) -> None:
            progress_bar.progress(pct, text=msg)

        # Generate
        try:
            generation_results = gen_module.generate_batch(
                system_prompt=full_system_prompt,
                tactic=tactic,
                count=count,
                engines=engines,
                target_audience=target_audience,
                key_messages=key_messages,
                tone=tone,
                extra_instructions=extra_instructions,
                images=encoded_images or None,
                progress_callback=update_progress,
            )
        except Exception as e:
            st.error(f"生成失败：{e}")
            return

        # Persist to DB
        saved_count = 0
        for slot in generation_results:
            item = db.create_item(db_client, user_id, batch_id)
            for version_result in slot["versions"]:
                db.create_version(
                    db_client,
                    item_id=item["id"],
                    ai_engine=version_result.ai_engine,
                    title=version_result.title,
                    body=version_result.body,
                    keywords=version_result.keywords,
                    token_usage=version_result.token_usage,
                )
                saved_count += 1

        progress_bar.progress(1.0, text="生成完成！")
        st.success(f"✅ 生成完成！共 {len(generation_results)} 篇，{saved_count} 个版本。")

        # Store batch_id for review page
        st.session_state["review_batch_id"] = batch_id
        st.info("👉 前往「审核与迭代」页面查看结果。")


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: 审核与迭代
# ═══════════════════════════════════════════════════════════════════════════

QUICK_FEEDBACK_TAGS = [
    "语气再软一点", "更口语化", "调皮一点", "更专业",
    "加个生活场景", "突出产品卖点", "增加情感共鸣", "换个角度写",
    "开头换个钩子", "标题太长了", "加个反转结构", "结尾加CTA",
    "太硬广，软一下", "注意平台敏感词", "别提竞品", "检查蓝字覆盖",
]


def page_review(project: dict) -> None:
    st.title("🔍 审核与迭代")

    # Batch selector
    batches = db.list_batches(db_client, project["id"])
    if not batches:
        st.info("暂无批次，请先在「生成工作台」生成内容。")
        return

    batch_options = {
        f"{b['tactic']} · {b['created_at'][:10]} ({b['id'][:8]})": b["id"]
        for b in batches
    }
    default_key = None
    stored_bid = st.session_state.get("review_batch_id")
    for k, v in batch_options.items():
        if v == stored_bid:
            default_key = k
            break

    selected_batch_label = st.selectbox(
        "选择批次",
        list(batch_options.keys()),
        index=list(batch_options.keys()).index(default_key) if default_key else 0,
    )
    batch_id = batch_options[selected_batch_label]
    selected_batch = next(b for b in batches if b["id"] == batch_id)

    # Load items
    items = db.list_items(db_client, batch_id)
    if not items:
        st.info("该批次暂无文案。")
        return

    # Summary stats
    pending = sum(1 for it in items if it["status"] == "pending")
    approved = sum(1 for it in items if it["status"] == "approved")
    revision = sum(1 for it in items if it["status"] == "needs_revision")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("总篇数", len(items))
    col2.metric("待审核", pending)
    col3.metric("已通过", approved)
    col4.metric("待修改", revision)

    st.divider()

    # Collect all feedbacks for memory ingestion
    new_feedbacks_this_session: list[str] = []

    # ── Item cards ─────────────────────────────────────────────────────
    for item in items:
        versions = sorted(item.get("versions", []), key=lambda v: v.get("version_num", 0))
        if not versions:
            continue
        _render_item_card(
            item, versions, selected_batch, project,
            new_feedbacks_this_session,
        )

    # ── Batch actions ──────────────────────────────────────────────────
    st.divider()
    st.markdown("### 批量操作 & 导出")

    col_exp, col_feishu, col_mem = st.columns(3)

    with col_exp:
        approved_items = _collect_approved_items(items)
        if approved_items and st.button("📄 导出 Word", use_container_width=True):
            docx_bytes = exporter.build_word_document(
                items=approved_items,
                project_name=project.get("name", ""),
                brand=project.get("brand", ""),
                tactic=selected_batch.get("tactic", ""),
            )
            st.download_button(
                label="⬇️ 下载 Word 文件",
                data=docx_bytes,
                file_name=f"xhs_{project.get('brand','')}_稿件.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                use_container_width=True,
            )

    with col_feishu:
        if st.button("🔔 推送到飞书", use_container_width=True):
            if not config.FEISHU_WEBHOOK_URL:
                st.warning("飞书 Webhook 未配置（FEISHU_WEBHOOK_URL）。")
            else:
                approved_items = _collect_approved_items(items)
                ok = exporter.push_to_feishu(
                    items=approved_items,
                    project_name=project.get("name", ""),
                    brand=project.get("brand", ""),
                    tactic=selected_batch.get("tactic", ""),
                )
                if ok:
                    st.success("已推送到飞书。")
                else:
                    st.error("飞书推送失败，请检查 Webhook 配置。")

    with col_mem:
        if st.button("💾 沉淀反馈记忆", use_container_width=True):
            _ingest_all_feedbacks(project, new_feedbacks_this_session)


def _render_item_card(
    item: dict,
    versions: list[dict],
    batch: dict,
    project: dict,
    feedback_collector: list[str],
) -> None:
    """Render a single copy item card with review controls."""
    item_id = item["id"]
    status = item["status"]

    # Use best version if set, else latest
    best_vid = item.get("best_version_id")
    if best_vid:
        display_version = next(
            (v for v in versions if v["id"] == best_vid), versions[-1]
        )
    else:
        display_version = versions[-1]

    status_icon = {"pending": "⏳", "approved": "✅", "needs_revision": "✏️"}.get(status, "⏳")
    status_label = {"pending": "待审核", "approved": "已通过", "needs_revision": "待修改"}.get(
        status, "待审核"
    )

    with st.expander(
        f"{status_icon} {display_version.get('title', '（无标题）')}  "
        f"— {status_label}  ·  v{display_version.get('version_num', 1)}  "
        f"·  {display_version.get('ai_engine', '').upper()}",
        expanded=(status in ("pending", "needs_revision")),
    ):
        # Multi-version comparison (if multiple engines were used)
        if len(versions) > 1:
            _render_version_comparison(versions, item_id)
        else:
            _render_single_version(display_version)

        # Status controls
        col_approve, col_revise = st.columns(2)
        with col_approve:
            if st.button("✅ 通过", key=f"approve_{item_id}", use_container_width=True):
                db.update_item_status(db_client, item_id, "approved")
                st.rerun()
        with col_revise:
            if st.button("✏️ 需修改", key=f"revise_{item_id}", use_container_width=True):
                db.update_item_status(db_client, item_id, "needs_revision")
                st.rerun()

        # Feedback & iteration
        if status in ("pending", "needs_revision"):
            st.markdown("**修改意见：**")

            # Quick tags
            selected_tags = st.multiselect(
                "快捷反馈标签",
                QUICK_FEEDBACK_TAGS,
                key=f"tags_{item_id}",
                label_visibility="collapsed",
            )
            feedback_text = st.text_area(
                "详细反馈（可选）",
                key=f"feedback_{item_id}",
                height=80,
                placeholder="在此填写具体修改意见...",
            )

            combined_feedback = (
                "、".join(selected_tags)
                + ("；" + feedback_text if feedback_text else "")
            ).strip("、；")

            if combined_feedback:
                feedback_collector.append(combined_feedback)

            # Engine selector for iteration
            iter_engine = st.selectbox(
                "用哪个引擎重新生成",
                gen_module.AVAILABLE_ENGINES,
                format_func=lambda e: f"{e.upper()} 重新生成",
                key=f"iter_engine_{item_id}",
            )

            if st.button("🔄 重新生成", key=f"regen_{item_id}", use_container_width=True):
                if not combined_feedback:
                    st.warning("请先填写修改意见或选择快捷标签。")
                else:
                    _run_iteration(item, versions, batch, project, combined_feedback, iter_engine)


def _render_single_version(version: dict) -> None:
    title = version.get("title", "")
    body = version.get("body", "")
    keywords = version.get("keywords", [])

    title_len = len(title)
    title_color = "green" if 15 <= title_len <= 22 else "orange"
    st.markdown(
        f"<div class='copy-title'>{title}</div>"
        f"<div class='copy-meta'>标题字数：<span style='color:{title_color}'>{title_len}字</span></div>",
        unsafe_allow_html=True,
    )
    st.markdown(body.replace("\n", "  \n"))
    if keywords:
        kw_html = " ".join(f"<span class='tag'>#{k}</span>" for k in keywords)
        st.markdown(kw_html, unsafe_allow_html=True)


def _render_version_comparison(versions: list[dict], item_id: str) -> None:
    """Side-by-side multi-engine version comparison."""
    # Group by engine
    by_engine: dict[str, list[dict]] = {}
    for v in versions:
        engine = v.get("ai_engine", "unknown")
        by_engine.setdefault(engine, []).append(v)

    cols = st.columns(len(by_engine))
    engine_list = list(by_engine.keys())

    for col_idx, (col, engine) in enumerate(zip(cols, engine_list)):
        with col:
            st.markdown(f"**{engine.upper()} 版本**")
            latest = sorted(by_engine[engine], key=lambda x: x.get("version_num", 0))[-1]
            _render_single_version(latest)
            if st.button(f"选为最佳（{engine.upper()}）", key=f"best_{item_id}_{engine}"):
                db.update_item_status(
                    db_client, item_id, "approved",
                    best_version_id=latest["id"]
                )
                st.rerun()


def _run_iteration(
    item: dict,
    versions: list[dict],
    batch: dict,
    project: dict,
    feedback: str,
    engine_name: str,
) -> None:
    """Run one iteration for an item and save the new version."""
    base_prompt = project.get("system_prompt", "")
    global_mems, project_mems = db.get_confirmed_memories(
        db_client, user_id, project_id=project["id"]
    )
    tactic = batch.get("tactic", "")
    tactic_suffix = proj_module.get_tactic_prompt_suffix(project, tactic)
    full_system_prompt = mem_module.build_system_prompt(
        base_prompt=base_prompt,
        global_memories=global_mems,
        project_memories=project_mems,
        tactic_suffix=tactic_suffix,
    )

    batch_params = batch.get("params") or {}
    if isinstance(batch_params, str):
        try:
            batch_params = json.loads(batch_params)
        except Exception:
            batch_params = {}

    original_user_prompt = gen_module.reconstruct_user_prompt(batch_params, tactic)

    with st.spinner(f"正在用 {engine_name.upper()} 迭代…"):
        result = gen_module.iterate_copy(
            system_prompt=full_system_prompt,
            original_user_prompt=original_user_prompt,
            version_history=versions,
            feedback=feedback,
            engine_name=engine_name,
        )

    if result.error:
        st.error(f"迭代失败：{result.error}")
        return

    db.create_version(
        db_client,
        item_id=item["id"],
        ai_engine=result.ai_engine,
        title=result.title,
        body=result.body,
        keywords=result.keywords,
        feedback=feedback,
        token_usage=result.token_usage,
    )
    # Reset item to pending so it gets reviewed again
    db.update_item_status(db_client, item["id"], "pending")
    st.success("迭代成功！")
    st.rerun()


def _collect_approved_items(items: list[dict]) -> list[dict]:
    """Build a flat list of approved items for export."""
    result = []
    for item in items:
        if item["status"] != "approved":
            continue
        versions = sorted(item.get("versions", []), key=lambda v: v.get("version_num", 0))
        if not versions:
            continue
        best_vid = item.get("best_version_id")
        if best_vid:
            v = next((x for x in versions if x["id"] == best_vid), versions[-1])
        else:
            v = versions[-1]
        result.append({
            "title": v.get("title", ""),
            "body": v.get("body", ""),
            "keywords": v.get("keywords", []),
            "ai_engine": v.get("ai_engine", ""),
            "version_num": v.get("version_num", 1),
        })
    return result


def _ingest_all_feedbacks(project: dict, feedbacks: list[str]) -> None:
    """Persist feedbacks as memory candidates."""
    if not feedbacks:
        st.info("本次没有新的反馈需要沉淀。")
        return
    unique = list(dict.fromkeys(feedbacks))
    with st.spinner("正在分析并沉淀反馈记忆…"):
        results = mem_module.ingest_batch_feedbacks(
            db_client, user_id, unique,
            project_id=project["id"],
            project_name=project.get("name", ""),
        )
    st.success(f"已沉淀 {len(results)} 条反馈记忆。前往「记忆管理」查看。")


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: 项目设置
# ═══════════════════════════════════════════════════════════════════════════

def page_project_settings(project: Optional[dict]) -> None:
    if project is None:
        st.info("请先在左侧创建或选择一个项目。")
        return
    proj_module.render_project_settings(db_client, project, user_id)


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: 记忆管理
# ═══════════════════════════════════════════════════════════════════════════

def page_memory(project: Optional[dict]) -> None:
    pid = project["id"] if project else None
    pname = project.get("name", "") if project else ""
    mem_module.render_memory_manager(db_client, user_id, project_id=pid, project_name=pname)


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: 批次历史
# ═══════════════════════════════════════════════════════════════════════════

def page_history(project: dict) -> None:
    st.title("📋 批次历史")

    batches = db.list_batches(db_client, project["id"], limit=50)
    if not batches:
        st.info("暂无历史批次。")
        return

    for batch in batches:
        batch_params = batch.get("params") or {}
        if isinstance(batch_params, str):
            try:
                batch_params = json.loads(batch_params)
            except Exception:
                batch_params = {}

        items = db.list_items(db_client, batch["id"])
        approved = sum(1 for i in items if i["status"] == "approved")
        pending = sum(1 for i in items if i["status"] == "pending")
        revision = sum(1 for i in items if i["status"] == "needs_revision")

        with st.expander(
            f"📦 {batch['tactic']}  ·  {batch['created_at'][:16]}  "
            f"·  共{len(items)}篇（✅{approved} ⏳{pending} ✏️{revision}）"
        ):
            col1, col2 = st.columns(2)
            with col1:
                st.markdown(f"**批次ID：** `{batch['id'][:8]}…`")
                engines_raw = batch.get("ai_engines", "[]")
                if isinstance(engines_raw, str):
                    try:
                        engines_raw = json.loads(engines_raw)
                    except Exception:
                        engines_raw = [engines_raw]
                st.markdown(f"**AI引擎：** {', '.join(e.upper() for e in engines_raw)}")
            with col2:
                st.markdown(f"**目标人群：** {batch_params.get('target_audience', '—')}")
                st.markdown(f"**核心卖点：** {batch_params.get('key_messages', '—')}")

            if items:
                if st.button("查看此批次", key=f"view_batch_{batch['id']}"):
                    st.session_state["review_batch_id"] = batch["id"]
                    st.rerun()


# ═══════════════════════════════════════════════════════════════════════════
# ROUTER
# ═══════════════════════════════════════════════════════════════════════════

if page == "生成工作台":
    if selected_project:
        page_generate(selected_project)
elif page == "审核与迭代":
    if selected_project:
        page_review(selected_project)
elif page == "记忆管理":
    page_memory(selected_project)
elif page == "项目设置":
    page_project_settings(selected_project)
elif page == "批次历史":
    if selected_project:
        page_history(selected_project)
