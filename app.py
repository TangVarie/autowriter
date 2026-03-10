"""
XHS Content Workstation — Main Streamlit Application
小红书内容自动化工作台 v2.0

Entry point: streamlit run app.py
"""

from __future__ import annotations

import html as _html
import json
import re
import threading
from datetime import datetime, timezone, timedelta
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

_BEIJING_TZ = timezone(timedelta(hours=8))


# ── Generation Queue ────────────────────────────────────────────────────────

def _queue_worker(
    plans: list[dict],
    user_id: str,
    db_client,
    status: dict,
    stop_event: threading.Event,
) -> None:
    """Background daemon thread: executes queued generation plans sequentially."""
    status["running"] = True
    status["done"] = False
    status["total"] = len(plans)
    status.setdefault("completed", [])
    status.setdefault("errors", [])

    for idx, plan in enumerate(plans):
        if stop_event.is_set():
            status["message"] = f"已停止（完成 {len(status['completed'])}/{len(plans)}）"
            break

        proj_name = plan.get("project_name", f"计划{idx+1}")
        tactic    = plan.get("tactic", "")
        status["current"] = idx
        status["message"] = f"计划 {idx+1}/{len(plans)} — {proj_name} · {tactic or '通用'}"

        try:
            project_id = plan["project_id"]
            all_projects = db.list_projects(db_client, user_id)
            project = next((p for p in all_projects if p["id"] == project_id), None)
            if not project:
                status["errors"].append(f"计划 {idx+1}：找不到项目 {project_id}")
                continue

            base_prompt = project.get("system_prompt", "")
            if not base_prompt.strip():
                status["errors"].append(f"计划 {idx+1}：项目未配置 System Prompt")
                continue

            tactic_suffix  = proj_module.get_tactic_prompt_suffix(project, tactic) if tactic else ""
            global_mems, project_mems = db.get_confirmed_memories(
                db_client, user_id, project_id=project_id
            )
            full_system_prompt = mem_module.build_system_prompt(
                base_prompt=base_prompt,
                global_memories=global_mems,
                project_memories=project_mems,
                tactic_suffix=tactic_suffix,
            )

            engines          = plan.get("engines", ["claude"])
            count            = plan.get("count", 1)
            engine_models    = plan.get("engine_models", {})
            use_thinking     = plan.get("use_thinking", False)
            gemini_thinking  = plan.get("gemini_use_thinking", False)
            extra_instr      = plan.get("extra_instructions", "")

            batch_params = {
                "target_audience":  plan.get("target_audience", ""),
                "key_messages":     plan.get("key_messages", ""),
                "tone":             plan.get("tone", ""),
                "extra_instructions": extra_instr,
                "use_thinking":     use_thinking,
                "gemini_use_thinking": gemini_thinking,
                "engine_models":    engine_models,
            }
            batch = db.create_batch(
                db_client, user_id,
                project_id=project_id,
                tactic=tactic or "通用",
                params=batch_params,
                ai_engines=engines,
            )
            batch_id = batch["id"]
            historical_titles = db.get_recent_titles(db_client, project_id)

            _total   = count * len(engines)
            _done_n  = [0]
            def _progress(pct: float, msg: str, _idx=idx, _n=len(plans), _t=_total) -> None:
                status["message"] = f"计划 {_idx+1}/{_n} — {msg}"

            generation_results = gen_module.generate_batch(
                system_prompt=full_system_prompt,
                tactic=tactic,
                count=count,
                engines=engines,
                target_audience=plan.get("target_audience", ""),
                key_messages=plan.get("key_messages", ""),
                tone=plan.get("tone", ""),
                extra_instructions=extra_instr,
                images=None,
                progress_callback=_progress,
                historical_titles=historical_titles or None,
                use_thinking=use_thinking,
                engine_models=engine_models or None,
                gemini_use_thinking=gemini_thinking,
            )

            saved = 0
            for slot in generation_results:
                item = db.create_item(db_client, user_id, batch_id)
                for vr in slot["versions"]:
                    if vr.error and not vr.title:
                        continue
                    db.create_version(
                        db_client,
                        item_id=item["id"],
                        ai_engine=vr.ai_engine,
                        title=vr.title,
                        body=vr.body,
                        keywords=vr.keywords,
                        token_usage=vr.token_usage,
                    )
                    saved += 1

            status["completed"].append({
                "plan_idx":    idx,
                "batch_id":    batch_id,
                "project_name": proj_name,
                "saved":       saved,
            })

        except Exception as exc:
            status["errors"].append(f"计划 {idx+1}：{exc}")

    status["running"] = False
    status["done"]    = True
    if not stop_event.is_set():
        n_ok  = len(status["completed"])
        n_err = len(status["errors"])
        status["message"] = f"全部完成 ✓ {n_ok} 计划成功" + (f"，{n_err} 失败" if n_err else "")


def _queue_banner() -> None:
    """Render a sticky progress banner when a queue is running or just finished."""
    qs = st.session_state.get("queue_state")
    if not qs:
        return
    completed = len(qs.get("completed", []))
    total     = qs.get("total", 0)
    msg       = qs.get("message", "")
    errors    = qs.get("errors", [])

    if qs.get("running"):
        banner = st.container()
        with banner:
            bcol_txt, bcol_btn = st.columns([5, 1])
            with bcol_txt:
                st.info(f"🔄 队列生成中 ({completed}/{total}) — {msg}")
            with bcol_btn:
                if st.button("⏹ 停止", key="global_stop_queue", use_container_width=True):
                    evt = st.session_state.get("queue_stop_event")
                    if evt:
                        evt.set()
    elif qs.get("done"):
        bcol_txt, bcol_btn = st.columns([5, 1])
        with bcol_txt:
            if errors:
                st.warning(f"✅ 队列完成 — {completed}/{total} 成功，{len(errors)} 失败：" + "；".join(errors))
            else:
                st.success(f"✅ 队列完成！{completed} 个批次已保存 — {msg}")
        with bcol_btn:
            if st.button("清除", key="clear_queue_status", use_container_width=True):
                st.session_state.pop("queue_state", None)
                st.session_state.pop("queue_stop_event", None)
                st.rerun()


def _format_batch_label(batch: dict, project_name: str = "") -> str:
    """Format a batch label consistently: project · tactic · date · time (Beijing)."""
    tactic = batch.get("tactic", "通用")
    created_at = batch.get("created_at", "")
    # Parse and convert to Beijing time
    try:
        # Supabase returns ISO format like "2024-03-09T14:30:00.123456+00:00"
        dt_str = created_at.replace("Z", "+00:00")
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        beijing_dt = dt.astimezone(_BEIJING_TZ)
        time_str = beijing_dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        time_str = created_at[:16] if created_at else "未知时间"

    parts = []
    if project_name:
        parts.append(project_name)
    if tactic:
        parts.append(tactic)
    parts.append(time_str)
    return " · ".join(parts)


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
/* ═══════════════════════════════════════════
   Design tokens
   ═══════════════════════════════════════════ */
:root {
  --accent:        #D4412A;
  --accent-soft:   #FDF1EF;
  --accent-border: #F2C4BB;
  --bg:            #F7F6F3;
  --card:          #FFFFFF;
  --border:        #E8E4DF;
  --border-mid:    #D5D0C9;
  --text-1:        #1A1714;
  --text-2:        #5C5752;
  --text-3:        #9E9992;
  --green:         #16A34A;
  --green-soft:    #F0FDF4;
  --amber:         #D97706;
  --amber-soft:    #FFFBEB;
  --slate:         #64748B;
  --slate-soft:    #F8FAFC;
  --shadow-xs:     0 1px 3px rgba(0,0,0,.06);
  --shadow-sm:     0 2px 8px rgba(0,0,0,.07), 0 1px 3px rgba(0,0,0,.05);
  --shadow-md:     0 4px 16px rgba(0,0,0,.08), 0 2px 6px rgba(0,0,0,.05);
  --r-sm:  8px;
  --r-md:  12px;
  --r-lg:  16px;
  --r-xl:  20px;
}

/* ── App background ── */
.stApp, .stApp > .main {
  background: var(--bg) !important;
  font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI",
               Helvetica, Arial, sans-serif;
}

/* ── Sidebar ── */
[data-testid="stSidebar"] {
  background: #FFFFFF !important;
  border-right: 1.5px solid var(--border) !important;
}
[data-testid="stSidebar"] .stMarkdown p,
[data-testid="stSidebar"] .stMarkdown small,
[data-testid="stSidebar"] label {
  color: var(--text-2) !important;
  font-size: 0.8125rem !important;
}
[data-testid="stSidebar"] .stRadio label {
  font-size: 0.875rem !important;
  font-weight: 500;
  color: var(--text-1) !important;
}

/* ── Sidebar nav pills ── */
[data-testid="stSidebar"] [data-testid="stRadio"] > div {
  gap: 2px;
}
[data-testid="stSidebar"] [data-testid="stRadio"] label {
  border-radius: var(--r-sm) !important;
  padding: 8px 12px !important;
  transition: background 0.15s;
  cursor: pointer;
}
[data-testid="stSidebar"] [data-testid="stRadio"] label:hover {
  background: var(--bg) !important;
}

/* ── Main content area ── */
.main .block-container {
  padding-top: 2rem !important;
  padding-bottom: 4rem !important;
  max-width: 1100px !important;
}

/* ── Headings ── */
h1 { font-size: 1.75rem !important; font-weight: 700 !important;
     color: var(--text-1) !important; letter-spacing: -0.02em; }
h2 { font-size: 1.375rem !important; font-weight: 600 !important;
     color: var(--text-1) !important; letter-spacing: -0.01em; }
h3 { font-size: 1.125rem !important; font-weight: 600 !important;
     color: var(--text-1) !important; }

/* ── Buttons ── */
.stButton > button {
  border-radius: var(--r-sm) !important;
  font-size: 0.875rem !important;
  font-weight: 500 !important;
  padding: 0.5rem 1rem !important;
  border: 1.5px solid var(--border) !important;
  background: var(--card) !important;
  color: var(--text-1) !important;
  box-shadow: var(--shadow-xs) !important;
  transition: all 0.15s ease !important;
}
.stButton > button:hover {
  border-color: var(--border-mid) !important;
  box-shadow: var(--shadow-sm) !important;
  background: #FAFAF8 !important;
}
/* Primary / type=primary button */
.stButton > button[kind="primary"] {
  background: var(--text-1) !important;
  border-color: var(--text-1) !important;
  color: #FFFFFF !important;
  box-shadow: var(--shadow-sm) !important;
}
.stButton > button[kind="primary"]:hover {
  background: #2D2926 !important;
  border-color: #2D2926 !important;
  box-shadow: var(--shadow-md) !important;
}

/* ── Inputs, textareas, selects ── */
.stTextInput > div > div > input,
.stTextArea > div > div > textarea,
.stSelectbox > div > div > div {
  border-radius: var(--r-sm) !important;
  border: 1.5px solid var(--border) !important;
  background: var(--card) !important;
  font-size: 0.875rem !important;
  color: var(--text-1) !important;
  box-shadow: var(--shadow-xs) !important;
  transition: border-color 0.15s !important;
}
.stTextInput > div > div > input:focus,
.stTextArea > div > div > textarea:focus {
  border-color: var(--border-mid) !important;
  box-shadow: 0 0 0 3px rgba(212,65,42,.08) !important;
  outline: none !important;
}

/* ── Sliders ── */
[data-testid="stSlider"] .stSlider div[role="slider"] {
  background: var(--text-1) !important;
}

/* ── Expanders (copy cards) ── */
.stExpander {
  border: 1.5px solid var(--border) !important;
  border-radius: var(--r-md) !important;
  background: var(--card) !important;
  box-shadow: var(--shadow-xs) !important;
  margin-bottom: 10px !important;
  overflow: hidden;
  transition: box-shadow 0.2s, border-color 0.2s;
}
.stExpander:hover {
  box-shadow: var(--shadow-sm) !important;
  border-color: var(--border-mid) !important;
}
[data-testid="stExpander"] > details > summary {
  padding: 14px 16px !important;
  background: var(--card) !important;
  font-size: 0.9rem !important;
  font-weight: 500 !important;
  color: var(--text-1) !important;
  border-radius: var(--r-md) !important;
}
[data-testid="stExpander"] > details[open] > summary {
  border-bottom: 1.5px solid var(--border) !important;
  border-radius: var(--r-md) var(--r-md) 0 0 !important;
  background: #FBFAF8 !important;
}
[data-testid="stExpander"] > details > div {
  padding: 16px !important;
  background: var(--card) !important;
}

/* ── Tabs ── */
.stTabs [data-testid="stTab"] {
  border-radius: var(--r-sm) var(--r-sm) 0 0 !important;
  font-size: 0.875rem !important;
  font-weight: 500 !important;
  color: var(--text-2) !important;
}
.stTabs [aria-selected="true"] {
  color: var(--text-1) !important;
  border-bottom: 2.5px solid var(--text-1) !important;
}

/* ── Info / warning / error / success boxes ── */
[data-testid="stAlert"] {
  border-radius: var(--r-md) !important;
  border: 1.5px solid transparent !important;
  font-size: 0.875rem !important;
}

/* ── Dividers ── */
hr {
  border: none !important;
  border-top: 1.5px solid var(--border) !important;
  margin: 1.25rem 0 !important;
}

/* ── Metrics ── */
[data-testid="stMetric"] {
  background: var(--card) !important;
  border: 1.5px solid var(--border) !important;
  border-radius: var(--r-md) !important;
  padding: 16px 20px !important;
  box-shadow: var(--shadow-xs) !important;
}
[data-testid="stMetricLabel"] {
  font-size: 0.75rem !important;
  font-weight: 600 !important;
  text-transform: uppercase !important;
  letter-spacing: 0.06em !important;
  color: var(--text-3) !important;
}
[data-testid="stMetricValue"] {
  font-size: 1.75rem !important;
  font-weight: 700 !important;
  color: var(--text-1) !important;
  line-height: 1.2 !important;
}

/* ── Checkboxes & radios ── */
.stCheckbox label, .stRadio label {
  font-size: 0.875rem !important;
}

/* ── Progress bar ── */
.stProgress > div > div > div {
  background: var(--text-1) !important;
  border-radius: 99px !important;
}
.stProgress > div > div {
  border-radius: 99px !important;
  background: var(--border) !important;
}

/* ── Multiselect ── */
.stMultiSelect > div > div {
  border-radius: var(--r-sm) !important;
  border: 1.5px solid var(--border) !important;
  background: var(--card) !important;
  font-size: 0.875rem !important;
}
.stMultiSelect span[data-baseweb="tag"] {
  background: var(--bg) !important;
  border: 1px solid var(--border-mid) !important;
  border-radius: 6px !important;
  font-size: 0.78rem !important;
  color: var(--text-1) !important;
}

/* ── Spinner ── */
.stSpinner > div {
  border-top-color: var(--text-1) !important;
}

/* ── Download button ── */
.stDownloadButton > button {
  border-radius: var(--r-sm) !important;
  border: 1.5px solid var(--border) !important;
  background: var(--card) !important;
  font-size: 0.875rem !important;
  font-weight: 500 !important;
  box-shadow: var(--shadow-xs) !important;
}

/* ════════════════════════════════════════════
   App-specific custom components
   ════════════════════════════════════════════ */

/* ── Page header band ── */
.page-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 1.5rem;
  padding-bottom: 1rem;
  border-bottom: 1.5px solid var(--border);
}
.page-header-icon {
  font-size: 1.5rem;
  width: 44px; height: 44px;
  display: flex; align-items: center; justify-content: center;
  background: var(--bg);
  border: 1.5px solid var(--border);
  border-radius: var(--r-md);
}
.page-header-text h1 { margin: 0 !important; font-size: 1.5rem !important; }
.page-header-text p  { margin: 0 !important; font-size: 0.8125rem !important;
                       color: var(--text-2) !important; margin-top: 2px !important; }

/* ── Stat badge row ── */
.stat-row {
  display: flex; gap: 10px; flex-wrap: wrap;
  margin-bottom: 1.25rem;
}
.stat-badge {
  display: flex; align-items: center; gap: 8px;
  background: var(--card);
  border: 1.5px solid var(--border);
  border-radius: var(--r-md);
  padding: 10px 16px;
  min-width: 100px;
  box-shadow: var(--shadow-xs);
}
.stat-badge .sb-num {
  font-size: 1.375rem; font-weight: 700; color: var(--text-1); line-height: 1;
}
.stat-badge .sb-lbl {
  font-size: 0.71rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.05em; color: var(--text-3);
}
.stat-badge.green  { border-color: #BBF7D0; }
.stat-badge.amber  { border-color: #FDE68A; }
.stat-badge.slate  { border-color: var(--border); }
.stat-badge.accent { border-color: var(--accent-border); }

/* ── Status pill ── */
.status-pill {
  display: inline-flex; align-items: center; gap: 5px;
  border-radius: 99px; font-size: 0.72rem; font-weight: 600;
  padding: 3px 10px; line-height: 1.4;
  letter-spacing: 0.02em;
}
.status-pill.approved {
  background: var(--green-soft); color: var(--green);
  border: 1px solid #BBF7D0;
}
.status-pill.revision {
  background: var(--amber-soft); color: var(--amber);
  border: 1px solid #FDE68A;
}
.status-pill.pending {
  background: var(--slate-soft); color: var(--slate);
  border: 1px solid #CBD5E1;
}

/* ── Copy content ── */
.copy-title {
  font-size: 1.0625rem; font-weight: 700;
  color: var(--text-1); line-height: 1.4;
  margin-bottom: 4px;
}
.copy-meta {
  font-size: 0.74rem; color: var(--text-3);
  margin-bottom: 10px;
  display: flex; align-items: center; gap: 8px;
}
.copy-meta .len-ok  { color: var(--green); font-weight: 600; }
.copy-meta .len-bad { color: var(--amber); font-weight: 600; }
.copy-body {
  font-size: 0.9rem; line-height: 1.75;
  color: var(--text-1); white-space: pre-wrap;
  border-left: 3px solid var(--border);
  padding-left: 14px; margin: 10px 0;
}

/* ── Keyword tags ── */
.tag {
  display: inline-flex; align-items: center;
  background: var(--bg);
  border: 1px solid var(--border-mid);
  border-radius: 6px;
  padding: 3px 9px;
  font-size: 0.74rem; font-weight: 500;
  color: var(--text-2);
  margin-right: 5px; margin-bottom: 4px;
  transition: all 0.1s;
}
.tag:hover { border-color: var(--border-mid); background: var(--card); }

/* ── Engine badge ── */
.engine-badge {
  display: inline-flex; align-items: center; gap: 4px;
  background: var(--text-1); color: #fff;
  border-radius: 5px; padding: 2px 8px;
  font-size: 0.68rem; font-weight: 700;
  letter-spacing: 0.04em; text-transform: uppercase;
}
.engine-badge.gemini {
  background: linear-gradient(135deg, #1A73E8, #0F47AF);
}
.engine-badge.claude {
  background: linear-gradient(135deg, #C96442, #A0522D);
}

/* ── Section label ── */
.section-label {
  font-size: 0.74rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.07em;
  color: var(--text-3); margin-bottom: 8px;
}

/* ── Memory row ── */
.mem-card {
  background: var(--card); border: 1.5px solid var(--border);
  border-radius: var(--r-md); padding: 12px 16px;
  margin-bottom: 8px; box-shadow: var(--shadow-xs);
  font-size: 0.875rem; color: var(--text-1);
}

/* ── Sidebar user block ── */
.user-block {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 12px;
  background: var(--bg); border-radius: var(--r-md);
  border: 1.5px solid var(--border);
  margin-bottom: 8px;
}
.user-avatar {
  width: 32px; height: 32px;
  background: var(--text-1); color: #fff;
  border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-size: 0.875rem; font-weight: 700;
  flex-shrink: 0;
}
.user-email { font-size: 0.8rem; font-weight: 500; color: var(--text-1); }
.user-ver   { font-size: 0.7rem; color: var(--text-3); }

/* ── Brand header in sidebar ── */
.brand-header {
  display: flex; align-items: center; gap: 10px;
  padding: 16px 0 12px;
  border-bottom: 1.5px solid var(--border);
  margin-bottom: 12px;
}
.brand-logo {
  font-size: 1.5rem; line-height: 1;
}
.brand-name {
  font-size: 0.9375rem; font-weight: 700;
  color: var(--text-1); line-height: 1.2;
}
.brand-sub {
  font-size: 0.7rem; color: var(--text-3);
}
</style>
""",
    unsafe_allow_html=True,
)


# ── Authentication gate ────────────────────────────────────────────────────
db_client, current_user = auth.require_auth()
user_id: str = current_user["id"]


# ── Sidebar ────────────────────────────────────────────────────────────────
with st.sidebar:
    # Brand header
    st.markdown(
        "<div class='brand-header'>"
        "<div class='brand-logo'>🍵</div>"
        "<div><div class='brand-name'>内容工作台</div>"
        f"<div class='brand-sub'>XHS Workstation · v{config.APP_VERSION}</div></div>"
        "</div>",
        unsafe_allow_html=True,
    )

    # User block
    safe_email = _html.escape(current_user['email'])
    avatar_char = _html.escape(current_user['email'][0].upper())
    st.markdown(
        f"<div class='user-block'>"
        f"<div class='user-avatar'>{avatar_char}</div>"
        f"<div><div class='user-email'>{safe_email}</div></div>"
        f"</div>",
        unsafe_allow_html=True,
    )
    if st.button("退出登录", use_container_width=True):
        auth.sign_out()
        st.rerun()
    st.divider()

# Project switcher (also rendered in sidebar via projects module)
selected_project = proj_module.render_project_switcher(db_client, user_id)

_NAV_ITEMS = {
    "✍️  生成": "生成工作台",
    "🔍  审核": "审核与迭代",
    "📤  导出": "导出中心",
    "🧠  记忆": "记忆管理",
    "⚙️  项目": "项目设置",
    "📋  历史": "批次历史",
}

with st.sidebar:
    st.divider()
    _nav_choice = st.radio(
        "导航",
        list(_NAV_ITEMS.keys()),
        label_visibility="collapsed",
    )
    page = _NAV_ITEMS[_nav_choice]


# ── Global queue banner ────────────────────────────────────────────────────
_queue_banner()

# ── Route to pages ─────────────────────────────────────────────────────────

if selected_project is None and page not in ("项目设置",):
    st.info("👈 请先在左侧创建或选择一个项目，然后开始使用。")
    st.stop()


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: 生成工作台
# ═══════════════════════════════════════════════════════════════════════════

def _page_header(icon: str, title: str, subtitle: str = "") -> None:
    sub_html = f"<p>{_html.escape(subtitle)}</p>" if subtitle else ""
    st.markdown(
        f"<div class='page-header'>"
        f"<div class='page-header-icon'>{icon}</div>"
        f"<div class='page-header-text'><h1>{_html.escape(title)}</h1>{sub_html}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )


def _render_queue_tab() -> None:
    """Render the batch queue builder and executor UI."""
    st.markdown(
        "<div class='section-label'>批次队列</div>"
        "<p style='font-size:0.85rem;color:var(--text-2);margin-top:4px;margin-bottom:16px'>"
        "添加多个生成计划，点击「启动队列」后在后台依次执行，切换页面不影响生成。</p>",
        unsafe_allow_html=True,
    )

    # Load projects for the dropdown
    all_projects = db.list_projects(db_client, user_id)
    if not all_projects:
        st.info("暂无项目，请先在「项目设置」中创建项目。")
        return

    proj_id_to_obj  = {p["id"]: p for p in all_projects}
    proj_id_to_name = {p["id"]: p.get("name", "未命名") for p in all_projects}
    proj_ids        = list(proj_id_to_obj.keys())

    # Session state
    st.session_state.setdefault("gen_queue_plans", [])
    plans: list[dict] = st.session_state["gen_queue_plans"]
    qs = st.session_state.get("queue_state", {})
    is_running = qs.get("running", False)

    # ── Plan list ──────────────────────────────────────────────────────
    plans_to_delete: list[int] = []
    for i, plan in enumerate(plans):
        with st.expander(
            f"计划 {i+1} — {plan.get('project_name', '?')} · "
            f"{plan.get('tactic', '通用') or '通用'} · "
            f"{'/'.join(e.upper() for e in plan.get('engines', ['claude']))} · "
            f"{plan.get('count', 1)} 篇",
            expanded=(i == len(plans) - 1),
        ):
            pc1, pc2 = st.columns(2)
            with pc1:
                sel_pid = st.selectbox(
                    "项目", proj_ids,
                    format_func=lambda pid: proj_id_to_name.get(pid, pid),
                    index=proj_ids.index(plan["project_id"]) if plan["project_id"] in proj_ids else 0,
                    key=f"qp_proj_{i}",
                )
                plan["project_id"]   = sel_pid
                plan["project_name"] = proj_id_to_name.get(sel_pid, "")

                sel_proj = proj_id_to_obj.get(sel_pid, {})
                plan_tactic_names = proj_module.get_tactic_names(sel_proj)
                tactic_opts = ["（无）"] + plan_tactic_names
                t_idx = tactic_opts.index(plan.get("tactic", "（无）")) if plan.get("tactic", "（无）") in tactic_opts else 0
                sel_tactic = st.selectbox("战术方向", tactic_opts, index=t_idx, key=f"qp_tactic_{i}")
                plan["tactic"] = "" if sel_tactic == "（无）" else sel_tactic

            with pc2:
                plan["count"] = st.number_input(
                    "篇数", min_value=1, max_value=config.MAX_GENERATION_COUNT,
                    value=plan.get("count", 3), key=f"qp_count_{i}",
                )
                q_eng_mode = st.radio(
                    "引擎", ["单引擎", "多引擎"], horizontal=True, key=f"qp_eng_mode_{i}",
                )
                if q_eng_mode == "单引擎":
                    q_eng = st.selectbox(
                        "选择引擎", gen_module.AVAILABLE_ENGINES,
                        format_func=lambda e: "Claude" if e == "claude" else "Gemini",
                        key=f"qp_eng_{i}",
                    )
                    plan["engines"] = [q_eng]
                else:
                    plan["engines"] = gen_module.AVAILABLE_ENGINES[:2] or ["claude"]

            q_em: dict[str, str] = {}
            qm1, qm2 = st.columns(2)
            if "claude" in plan["engines"]:
                with qm1:
                    q_em["claude"] = st.selectbox(
                        "Claude 模型", list(config.CLAUDE_MODELS.keys()),
                        format_func=lambda m: config.CLAUDE_MODELS.get(m, m),
                        index=list(config.CLAUDE_MODELS.keys()).index(config.CLAUDE_MODEL)
                              if config.CLAUDE_MODEL in config.CLAUDE_MODELS else 0,
                        key=f"qp_cm_{i}",
                    )
            if "gemini" in plan["engines"]:
                with qm2:
                    q_em["gemini"] = st.selectbox(
                        "Gemini 模型", list(config.GEMINI_MODELS.keys()),
                        format_func=lambda m: config.GEMINI_MODELS.get(m, m),
                        index=list(config.GEMINI_MODELS.keys()).index(config.GEMINI_MODEL)
                              if config.GEMINI_MODEL in config.GEMINI_MODELS else 0,
                        key=f"qp_gm_{i}",
                    )
            plan["engine_models"] = q_em

            qt1, qt2 = st.columns(2)
            with qt1:
                plan["use_thinking"] = st.checkbox(
                    "Claude Thinking", value=plan.get("use_thinking", False), key=f"qp_think_{i}"
                )
            with qt2:
                plan["gemini_use_thinking"] = st.checkbox(
                    "Gemini 思考", value=plan.get("gemini_use_thinking", False), key=f"qp_gthink_{i}"
                )

            plan["extra_instructions"] = st.text_input(
                "补充说明", value=plan.get("extra_instructions", ""),
                placeholder="可选", key=f"qp_extra_{i}",
            )

            if st.button("🗑 删除此计划", key=f"qp_del_{i}"):
                plans_to_delete.append(i)

    for idx in sorted(plans_to_delete, reverse=True):
        plans.pop(idx)
    if plans_to_delete:
        st.rerun()

    # ── Controls ───────────────────────────────────────────────────────
    btn_col1, btn_col2 = st.columns(2)
    with btn_col1:
        if st.button("➕ 添加计划", use_container_width=True, disabled=is_running):
            default_pid = proj_ids[0]
            plans.append({
                "project_id":       default_pid,
                "project_name":     proj_id_to_name.get(default_pid, ""),
                "tactic":           "",
                "engines":          ["claude"],
                "engine_models":    {"claude": config.CLAUDE_MODEL},
                "count":            3,
                "use_thinking":     False,
                "gemini_use_thinking": False,
                "extra_instructions": "",
            })
            st.rerun()

    with btn_col2:
        if not is_running:
            if st.button(
                "🚀 启动队列", type="primary", use_container_width=True,
                disabled=(not plans),
            ):
                stop_evt = threading.Event()
                status: dict = {
                    "running": False, "done": False, "total": 0,
                    "current": 0, "message": "准备中…",
                    "completed": [], "errors": [],
                }
                st.session_state["queue_state"]      = status
                st.session_state["queue_stop_event"] = stop_evt
                t = threading.Thread(
                    target=_queue_worker,
                    args=(list(plans), user_id, db_client, status, stop_evt),
                    daemon=True,
                )
                t.start()
                st.rerun()
        else:
            if st.button("⏹ 停止队列", use_container_width=True):
                evt = st.session_state.get("queue_stop_event")
                if evt:
                    evt.set()
                st.rerun()

    # Live status within tab
    if is_running:
        completed = len(qs.get("completed", []))
        total     = qs.get("total", 0)
        st.progress(
            completed / total if total else 0,
            text=qs.get("message", "生成中…"),
        )
        st.caption("生成在后台运行，可切换到其他页面。")

    # Completed results summary
    if qs.get("done") and qs.get("completed"):
        st.markdown("<div class='section-label' style='margin-top:16px'>已完成的批次</div>", unsafe_allow_html=True)
        for item in qs["completed"]:
            st.success(
                f"✅ {item['project_name']} · 批次 {item['batch_id'][:8]}… · "
                f"已保存 {item['saved']} 个版本"
            )


def page_generate(project: dict) -> None:
    pname = _html.escape(project.get("name", ""))
    _page_header("✍️", "生成工作台", f"项目：{pname}")

    project_name = project.get("name", "")
    base_prompt  = project.get("system_prompt", "")
    tactic_names = proj_module.get_tactic_names(project)

    # ── Sidebar: quick-generate controls ──────────────────────────────
    with st.sidebar:
        st.markdown("### 生成参数")

        if tactic_names:
            tactic = st.selectbox("战术方向", ["（不使用战术方向）"] + tactic_names)
            if tactic == "（不使用战术方向）":
                tactic = ""
        else:
            st.caption("未配置战术方向，将不应用战术方向。")
            tactic = ""
        count = st.slider("生成数量", 1, config.MAX_GENERATION_COUNT, config.DEFAULT_GENERATION_COUNT)

        engine_mode = st.radio(
            "AI 引擎模式",
            ["单引擎", "多引擎比稿"],
            help="多引擎比稿会同时用 Claude 和 Gemini 生成，便于对比。",
        )
        if engine_mode == "单引擎":
            engines = [st.selectbox(
                "引擎", gen_module.AVAILABLE_ENGINES,
                format_func=lambda e: "Claude" if e == "claude" else "Gemini",
            )]
        else:
            engines = gen_module.AVAILABLE_ENGINES[:2]
            if len(engines) < 2:
                st.warning("Gemini 未配置，将仅使用 Claude。")
                engines = ["claude"]

        engine_models: dict[str, str] = {}
        if "claude" in engines:
            engine_models["claude"] = st.selectbox(
                "Claude 模型", list(config.CLAUDE_MODELS.keys()),
                index=list(config.CLAUDE_MODELS.keys()).index(config.CLAUDE_MODEL)
                      if config.CLAUDE_MODEL in config.CLAUDE_MODELS else 0,
                format_func=lambda m: config.CLAUDE_MODELS.get(m, m),
            )
        if "gemini" in engines:
            engine_models["gemini"] = st.selectbox(
                "Gemini 模型", list(config.GEMINI_MODELS.keys()),
                index=list(config.GEMINI_MODELS.keys()).index(config.GEMINI_MODEL)
                      if config.GEMINI_MODEL in config.GEMINI_MODELS else 0,
                format_func=lambda m: config.GEMINI_MODELS.get(m, m),
            )

        use_thinking = False
        gemini_use_thinking = False
        if "claude" in engines:
            use_thinking = st.checkbox(
                "Claude：Extended Thinking",
                help="Opus 4.6 → effort=high；其他模型 → budget_tokens=8000。速度明显变慢。",
            )
            if use_thinking:
                sel = engine_models.get("claude", "")
                st.caption("effort=high, max 32k" if "opus-4-6" in sel else "budget=8k, max 16k")
        if "gemini" in engines:
            gemini_use_thinking = st.checkbox(
                "Gemini：思考模式",
                help="thinking_budget=-1 动态分配；对 2.5 Pro 效果明显。",
            )

        with st.expander("⚙️ 高级参数"):
            target_audience   = st.text_input("目标人群", placeholder="例：25-35岁职场女性")
            key_messages      = st.text_input("核心卖点/关键词", placeholder="例：低度数、清爽、派对感")
            tone              = st.text_input("语气偏好", placeholder="例：活泼口语化、朋友间分享")
            extra_instructions = st.text_area("补充说明", height=80, placeholder="其他要求...")

    # ── Main area: tabs ────────────────────────────────────────────────
    tab_quick, tab_queue = st.tabs(["✍️  快速生成", "📋  批次队列"])

    # ── TAB 1: Quick generate ──────────────────────────────────────────
    with tab_quick:
        st.markdown("<div class='section-label' style='margin-bottom:6px'>参考图片（可选）</div>", unsafe_allow_html=True)
        encoded_images = image_handler.render_image_uploader()
        image_prompt = ""
        if encoded_images:
            image_prompt = st.text_area(
                "图片提示词",
                placeholder="说明图片内容或用途。例：这是品牌产品实拍图，请参考图片视觉风格和产品细节进行创作。",
                height=80,
                key="image_prompt_input",
                help="上传图片后填写，可显著提升 AI 对图片的利用率。",
            )

        global_mems, project_mems = db.get_confirmed_memories(
            db_client, user_id, project_id=project["id"]
        )
        mem_count = len(global_mems) + len(project_mems)
        if mem_count > 0:
            st.info(f"🧠 已载入 {mem_count} 条确认记忆（{len(global_mems)} 通用 + {len(project_mems)} 项目）")

        if st.button("🚀 开始生成", type="primary", use_container_width=True):
            if not base_prompt.strip():
                st.warning("⚠️ 当前项目尚未配置 System Prompt，请先在「项目设置」中填写。")
                st.stop()

            tactic_suffix = proj_module.get_tactic_prompt_suffix(project, tactic) if tactic else ""
            full_system_prompt = mem_module.build_system_prompt(
                base_prompt=base_prompt,
                global_memories=global_mems,
                project_memories=project_mems,
                tactic_suffix=tactic_suffix,
            )

            combined_extra = extra_instructions
            if image_prompt:
                combined_extra = (combined_extra + "\n\n【参考图片说明】\n" + image_prompt).strip()

            batch_params = {
                "target_audience": target_audience,
                "key_messages": key_messages,
                "tone": tone,
                "extra_instructions": extra_instructions,
                "use_thinking": use_thinking,
                "gemini_use_thinking": gemini_use_thinking,
                "engine_models": engine_models,
            }
            batch = db.create_batch(
                db_client, user_id,
                project_id=project["id"],
                tactic=tactic or "通用",
                params=batch_params,
                ai_engines=engines,
            )
            batch_id = batch["id"]

            progress_bar = st.progress(0.0, text="正在初始化…")

            def update_progress(pct: float, msg: str) -> None:
                progress_bar.progress(pct, text=msg)

            historical_titles = db.get_recent_titles(db_client, project["id"])

            try:
                generation_results = gen_module.generate_batch(
                    system_prompt=full_system_prompt,
                    tactic=tactic,
                    count=count,
                    engines=engines,
                    target_audience=target_audience,
                    key_messages=key_messages,
                    tone=tone,
                    extra_instructions=combined_extra,
                    images=encoded_images or None,
                    progress_callback=update_progress,
                    historical_titles=historical_titles or None,
                    use_thinking=use_thinking,
                    engine_models=engine_models or None,
                    gemini_use_thinking=gemini_use_thinking,
                )
            except Exception as e:
                st.error(f"生成失败：{e}")
                return

            saved_count = 0
            error_messages: list[str] = []
            for slot in generation_results:
                item = db.create_item(db_client, user_id, batch_id)
                for version_result in slot["versions"]:
                    if version_result.error and not version_result.title:
                        error_messages.append(f"[{version_result.ai_engine.upper()}] {version_result.error}")
                        continue
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
            if error_messages:
                st.error("部分内容生成失败：\n" + "\n".join(f"• {e}" for e in list(dict.fromkeys(error_messages))))
            if saved_count > 0:
                st.success(f"✅ 生成完成！共 {len(generation_results)} 篇，{saved_count} 个版本。")
            elif not error_messages:
                st.warning("生成完成，但没有内容被保存，请检查配置。")
            st.session_state["review_batch_id"] = batch_id
            st.info("👉 前往「审核与迭代」页面查看结果。")

    # ── TAB 2: Batch Queue ─────────────────────────────────────────────
    with tab_queue:
        _render_queue_tab()


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
    pname = _html.escape(project.get("name", ""))
    _page_header("🔍", "审核与迭代", f"项目：{pname}")

    # Batch selector
    batches = db.list_batches(db_client, project["id"])
    if not batches:
        st.info("暂无批次，请先在「生成工作台」生成内容。")
        return

    project_name = project.get("name", "")
    # Use batch ID suffix to ensure uniqueness in case of same tactic+time
    batch_options = {
        f"{_format_batch_label(b, project_name)} ({b['id'][:6]})": b["id"]
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

    pending  = sum(1 for it in items if it["status"] == "pending")
    approved = sum(1 for it in items if it["status"] == "approved")
    revision = sum(1 for it in items if it["status"] == "needs_revision")

    # Stats row
    st.markdown(
        f"<div class='stat-row'>"
        f"<div class='stat-badge slate'>"
        f"  <div><div class='sb-num'>{len(items)}</div><div class='sb-lbl'>总计</div></div>"
        f"</div>"
        f"<div class='stat-badge amber'>"
        f"  <div><div class='sb-num'>{pending}</div><div class='sb-lbl'>待审核</div></div>"
        f"</div>"
        f"<div class='stat-badge green'>"
        f"  <div><div class='sb-num'>{approved}</div><div class='sb-lbl'>已通过</div></div>"
        f"</div>"
        f"<div class='stat-badge accent'>"
        f"  <div><div class='sb-num'>{revision}</div><div class='sb-lbl'>待修改</div></div>"
        f"</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

    filter_options = ["全部", "⏳ 待审核", "✅ 已通过", "✏️ 待修改"]
    selected_filter = st.radio(
        "筛选状态", filter_options, horizontal=True, label_visibility="collapsed",
    )

    _fmap = {"⏳ 待审核": "pending", "✅ 已通过": "approved", "✏️ 待修改": "needs_revision"}
    if selected_filter in _fmap:
        filtered_items = [it for it in items if it["status"] == _fmap[selected_filter]]
    else:
        filtered_items = items

    st.divider()

    if not filtered_items:
        st.info("当前筛选条件下没有文案。")

    # ── Item cards ─────────────────────────────────────────────────────
    for item in filtered_items:
        versions = sorted(item.get("versions", []), key=lambda v: v.get("version_num", 0))
        if not versions:
            continue
        _render_item_card(item, versions, selected_batch, project)

    # ── Quick batch actions ────────────────────────────────────────────
    if pending > 0 or revision > 0:
        st.divider()
        col_approve_all, col_spacer = st.columns([1, 3])
        with col_approve_all:
            if st.button("✅ 全部通过", key="approve_all_btn", use_container_width=True):
                for it in items:
                    if it["status"] in ("pending", "needs_revision"):
                        db.update_item_status(db_client, it["id"], "approved")
                st.rerun()

    # ── Batch actions ──────────────────────────────────────────────────
    st.divider()
    st.markdown("<div class='section-label'>批量操作</div>", unsafe_allow_html=True)
    st.caption("多批次合并导出请前往「📤 导出中心」页面。")

    col_feishu, col_mem = st.columns(2)

    with col_feishu:
        if st.button("🔔 推送本批次到飞书", use_container_width=True):
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
                st.success("已推送到飞书。") if ok else st.error("飞书推送失败，请检查 Webhook 配置。")

    with col_mem:
        if st.button("💾 沉淀反馈记忆", use_container_width=True):
            _ingest_all_feedbacks(project, batch_id)


def _render_item_card(
    item: dict,
    versions: list[dict],
    batch: dict,
    project: dict,
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

    status_icon  = {"pending": "⏳", "approved": "✅", "needs_revision": "✏️"}.get(status, "⏳")
    status_label = {"pending": "待审核", "approved": "已通过", "needs_revision": "待修改"}.get(status, "待审核")
    engine_raw = display_version.get("ai_engine", "")
    engine_short = engine_raw.split("/")[0].lower() if engine_raw else ""
    ver_num = display_version.get("version_num", 1)
    title_str = display_version.get("title", "（无标题）") or "（无标题）"

    expander_label = (
        f"{status_icon} {title_str[:60]}{'…' if len(title_str) > 60 else ''}  "
        f"— {status_label} · v{ver_num}"
    )
    with st.expander(expander_label, expanded=(status in ("pending", "needs_revision"))):
        # Engine badge
        eng_cls = engine_short if engine_short in ("claude", "gemini") else ""
        st.markdown(
            f"<span class='engine-badge {eng_cls}'>{_html.escape(engine_raw.upper())}</span>",
            unsafe_allow_html=True,
        )

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
            st.markdown("<div class='section-label' style='margin-top:12px'>修改意见</div>", unsafe_allow_html=True)

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

            # Engine + model selector for iteration
            iter_col_eng, iter_col_mod = st.columns(2)
            with iter_col_eng:
                iter_engine = st.selectbox(
                    "引擎",
                    gen_module.AVAILABLE_ENGINES,
                    format_func=lambda e: "Claude" if e == "claude" else "Gemini",
                    key=f"iter_engine_{item_id}",
                )
            with iter_col_mod:
                if iter_engine == "claude":
                    iter_model = st.selectbox(
                        "模型",
                        list(config.CLAUDE_MODELS.keys()),
                        format_func=lambda m: config.CLAUDE_MODELS.get(m, m),
                        key=f"iter_model_{item_id}",
                        index=list(config.CLAUDE_MODELS.keys()).index(config.CLAUDE_MODEL)
                              if config.CLAUDE_MODEL in config.CLAUDE_MODELS else 0,
                    )
                else:
                    iter_model = st.selectbox(
                        "模型",
                        list(config.GEMINI_MODELS.keys()),
                        format_func=lambda m: config.GEMINI_MODELS.get(m, m),
                        key=f"iter_model_{item_id}",
                        index=list(config.GEMINI_MODELS.keys()).index(config.GEMINI_MODEL)
                              if config.GEMINI_MODEL in config.GEMINI_MODELS else 0,
                    )

            if st.button("🔄 重新生成", key=f"regen_{item_id}", use_container_width=True):
                if not combined_feedback:
                    st.warning("请先填写修改意见或选择快捷标签。")
                else:
                    _run_iteration(item, versions, batch, project, combined_feedback, iter_engine, iter_model)

        # ── Manual edit ───────────────────────────────────────────────
        with st.expander("✏️ 手动精修", expanded=False):
            cur_title    = display_version.get("title", "") or ""
            cur_body     = display_version.get("body", "") or ""
            cur_keywords = _normalise_keywords(display_version.get("keywords"))

            edit_title = st.text_input(
                "标题", value=cur_title, key=f"edit_title_{item_id}",
            )
            edit_body = st.text_area(
                "正文", value=cur_body, height=300, key=f"edit_body_{item_id}",
            )
            edit_kw_raw = st.text_input(
                "关键词（逗号分隔）",
                value="，".join(cur_keywords),
                key=f"edit_kw_{item_id}",
                placeholder="关键词1，关键词2，关键词3",
            )

            if st.button("💾 保存手动修改并通过", key=f"save_edit_{item_id}", use_container_width=True):
                new_kw = [k.strip() for k in re.split(r"[,，、\s]+", edit_kw_raw) if k.strip()]
                db.create_version(
                    db_client,
                    item_id=item_id,
                    ai_engine="manual",
                    title=edit_title.strip(),
                    body=edit_body.strip(),
                    keywords=new_kw,
                    feedback="手动精修",
                )
                db.update_item_status(db_client, item_id, "approved")
                st.success("已保存修改并标记为通过。")
                st.rerun()


def _normalise_keywords(raw) -> list[str]:
    """Ensure keywords is always a clean list of strings."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(k).strip() for k in raw if str(k).strip()]
    if isinstance(raw, str):
        # Could be a JSON string like '["k1","k2"]' or plain comma-separated
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(k).strip() for k in parsed if str(k).strip()]
        except Exception:
            pass
        return [k.strip() for k in re.split(r'[,，、\s]+', raw) if k.strip()]
    return []


def _render_single_version(version: dict) -> None:
    title    = version.get("title", "") or ""
    body     = version.get("body", "") or ""
    keywords = _normalise_keywords(version.get("keywords"))
    raw_text = version.get("raw_text", "")

    title_len = len(title)
    len_cls   = "len-ok" if 15 <= title_len <= 22 else "len-bad"
    len_tip   = "✓ 长度适中" if 15 <= title_len <= 22 else f"⚠ {title_len} 字（建议 15-22）"

    safe_title = _html.escape(title)
    safe_body  = _html.escape(body)

    st.markdown(
        f"<div class='copy-title'>{safe_title}</div>"
        f"<div class='copy-meta'><span class='{len_cls}'>{len_tip}</span></div>"
        f"<div class='copy-body'>{safe_body}</div>",
        unsafe_allow_html=True,
    )
    if keywords:
        kw_html = " ".join(f"<span class='tag'>#{_html.escape(k)}</span>" for k in keywords)
        st.markdown(kw_html, unsafe_allow_html=True)

    if not title and raw_text:
        with st.expander("⚠️ 解析失败 — 查看原始 AI 输出", expanded=True):
            st.code(raw_text, language=None)


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
            eng_cls = engine.split("/")[0].lower()
            eng_cls = eng_cls if eng_cls in ("claude", "gemini") else ""
            st.markdown(
                f"<span class='engine-badge {eng_cls}'>{_html.escape(engine.upper())}</span>",
                unsafe_allow_html=True,
            )
            latest = sorted(by_engine[engine], key=lambda x: x.get("version_num", 0))[-1]
            _render_single_version(latest)
            if st.button(f"选为最佳", key=f"best_{item_id}_{engine}"):
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
    model_override: str = "",
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

    _bp = batch_params if isinstance(batch_params, dict) else {}
    iter_use_thinking = _bp.get("use_thinking", False)
    iter_gemini_thinking = _bp.get("gemini_use_thinking", False)
    # Use explicitly selected model, fall back to batch-saved model
    iter_model = model_override or (_bp.get("engine_models") or {}).get(engine_name, "")
    thinking_flag = iter_use_thinking if engine_name == "claude" else (iter_gemini_thinking if engine_name == "gemini" else False)
    with st.spinner(f"正在用 {engine_name.upper()}{' (深度思考)' if thinking_flag else ''} 迭代…"):
        result = gen_module.iterate_copy(
            system_prompt=full_system_prompt,
            original_user_prompt=original_user_prompt,
            version_history=versions,
            feedback=feedback,
            engine_name=engine_name,
            use_thinking=thinking_flag,
            model=iter_model,
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


def _ingest_all_feedbacks(project: dict, batch_id: str) -> None:
    """Collect all feedbacks from this batch's versions and persist as memory candidates."""
    # Collect feedbacks from DB (not from session state, which resets on rerun)
    items = db.list_items(db_client, batch_id)
    feedbacks: list[str] = []
    for item in items:
        for v in item.get("versions", []):
            fb = v.get("feedback")
            if fb and fb.strip():
                feedbacks.append(fb.strip())
    if not feedbacks:
        st.info("本批次没有反馈记录需要沉淀。")
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
# PAGE: 导出中心
# ═══════════════════════════════════════════════════════════════════════════

def page_export(project: dict) -> None:
    pname = _html.escape(project.get("name", ""))
    _page_header("📤", "导出中心", f"项目：{pname}")

    batches = db.list_batches(db_client, project["id"], limit=50)
    if not batches:
        st.info("暂无批次，请先在「生成工作台」生成内容。")
        return

    st.markdown(
        "<p style='font-size:0.875rem;color:var(--text-2);margin-bottom:16px'>"
        "勾选要导出的批次，点击「生成导出文件」统一输出为 Excel。"
        "每篇内容独占一个单元格，格式为：标题 / 正文 / 关键词。</p>",
        unsafe_allow_html=True,
    )

    project_name = project.get("name", "")

    # ── Batch selector table ────────────────────────────────────────────
    # Pre-load approved counts for all batches in bulk (one query per batch,
    # acceptable for ≤50 batches; can be optimised later with a view).
    batch_meta: list[dict] = []
    for batch in batches:
        items = db.list_items(db_client, batch["id"])
        approved = sum(1 for it in items if it["status"] == "approved")
        total    = len(items)
        batch_meta.append({
            "batch":    batch,
            "items":    items,
            "approved": approved,
            "total":    total,
        })

    batch_selections: dict[str, bool] = {}
    for meta in batch_meta:
        batch    = meta["batch"]
        approved = meta["approved"]
        total    = meta["total"]
        label    = _format_batch_label(batch, project_name)
        tactic   = batch.get("tactic", "通用") or "通用"

        col_ck, col_info = st.columns([1, 11])
        with col_ck:
            checked = st.checkbox(
                "选择", key=f"exp_batch_{batch['id']}",
                label_visibility="collapsed",
                value=st.session_state.get(f"exp_batch_{batch['id']}", False),
            )
        with col_info:
            badge_color = "green" if approved == total and total > 0 else "amber"
            st.markdown(
                f"<span style='font-size:0.9rem;font-weight:500'>{_html.escape(label)}</span>"
                f"&nbsp;&nbsp;<span style='font-size:0.8rem;color:var(--text-3)'>"
                f"{tactic} · {approved}/{total} 篇已通过</span>",
                unsafe_allow_html=True,
            )
        batch_selections[batch["id"]] = checked

    selected_ids = [bid for bid, v in batch_selections.items() if v]
    n_selected   = len(selected_ids)

    # Quick-select buttons
    qcol1, qcol2, _ = st.columns([2, 2, 8])
    with qcol1:
        if st.button("全选", key="exp_select_all"):
            for meta in batch_meta:
                st.session_state[f"exp_batch_{meta['batch']['id']}"] = True
            st.rerun()
    with qcol2:
        if st.button("取消全选", key="exp_deselect_all"):
            for meta in batch_meta:
                st.session_state[f"exp_batch_{meta['batch']['id']}"] = False
            st.rerun()

    st.divider()

    # ── Export controls ─────────────────────────────────────────────────
    # Show previously generated export if available
    exp_state = st.session_state.get("export_center_result")
    if exp_state:
        st.success(f"文件已就绪，共 {exp_state['count']} 篇内容，来自 {exp_state['n_batches']} 个批次。")
        dl_col, clr_col = st.columns([3, 1])
        with dl_col:
            st.download_button(
                label=f"📊 下载 Excel（{exp_state['count']} 篇）",
                data=exp_state["bytes"],
                file_name=exp_state["filename"],
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                type="primary",
            )
        with clr_col:
            if st.button("清除", key="exp_clear", use_container_width=True):
                del st.session_state["export_center_result"]
                st.rerun()

    # Map batch_id → items for fast lookup (already loaded above)
    batch_items_map = {meta["batch"]["id"]: meta["items"] for meta in batch_meta}

    gen_label = f"📥 生成导出文件（{n_selected} 个批次）" if n_selected else "📥 生成导出文件"
    if st.button(gen_label, type="primary", disabled=(n_selected == 0), use_container_width=True):
        all_items: list[dict] = []
        for bid in selected_ids:
            all_items.extend(_collect_approved_items(batch_items_map.get(bid, [])))

        if not all_items:
            st.warning("选中的批次中没有已通过的内容，请先在「审核与迭代」中通过稿件。")
        else:
            try:
                xlsx_bytes = exporter.build_combined_excel(all_items)
                brand = project.get("brand", "") or project_name
                filename = f"xhs_{brand}_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
                st.session_state["export_center_result"] = {
                    "bytes":    xlsx_bytes,
                    "count":    len(all_items),
                    "n_batches": n_selected,
                    "filename": filename,
                }
                st.rerun()
            except RuntimeError as e:
                st.error(str(e))

    # ── Feishu push (multi-batch) ───────────────────────────────────────
    if config.FEISHU_WEBHOOK_URL and n_selected > 0:
        st.divider()
        if st.button("🔔 推送选中批次到飞书", use_container_width=True):
            all_items = []
            for bid in selected_ids:
                all_items.extend(_collect_approved_items(batch_items_map.get(bid, [])))
            if not all_items:
                st.warning("选中的批次中没有已通过的内容。")
            else:
                ok = exporter.push_to_feishu(
                    items=all_items,
                    project_name=project_name,
                    brand=project.get("brand", ""),
                    tactic="混合批次",
                )
                st.success("已推送到飞书。") if ok else st.error("飞书推送失败，请检查 Webhook 配置。")


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: 项目设置
# ═══════════════════════════════════════════════════════════════════════════

def page_project_settings(project: Optional[dict]) -> None:
    if project is None:
        st.info("请先在左侧创建或选择一个项目。")
        return
    _page_header("⚙️", "项目设置", project.get("name", ""))
    proj_module.render_project_settings(db_client, project, user_id)


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: 记忆管理
# ═══════════════════════════════════════════════════════════════════════════

def page_memory(project: Optional[dict]) -> None:
    pid   = project["id"] if project else None
    pname = project.get("name", "") if project else ""
    _page_header("🧠", "记忆管理", pname)
    mem_module.render_memory_manager(db_client, user_id, project_id=pid, project_name=pname)


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: 批次历史
# ═══════════════════════════════════════════════════════════════════════════

def page_history(project: dict) -> None:
    _page_header("📋", "批次历史", f"项目：{project.get('name', '')}")

    batches = db.list_batches(db_client, project["id"], limit=50)
    if not batches:
        st.info("暂无历史批次。")
        return

    # Pre-load item counts for all batches to avoid N+1 queries
    batch_item_counts = db.get_batch_item_counts(db_client, [b["id"] for b in batches])

    for batch in batches:
        batch_params = batch.get("params") or {}
        if isinstance(batch_params, str):
            try:
                batch_params = json.loads(batch_params)
            except Exception:
                batch_params = {}

        counts = batch_item_counts.get(batch["id"], {"total": 0, "approved": 0, "pending": 0, "needs_revision": 0})

        batch_label = _format_batch_label(batch, project.get("name", ""))
        with st.expander(
            f"📦 {batch_label}  ·  共{counts['total']}篇"
            f"（✅{counts['approved']} ⏳{counts['pending']} ✏️{counts['needs_revision']}）"
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

            if counts['total'] > 0:
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
elif page == "导出中心":
    if selected_project:
        page_export(selected_project)
elif page == "记忆管理":
    page_memory(selected_project)
elif page == "项目设置":
    page_project_settings(selected_project)
elif page == "批次历史":
    if selected_project:
        page_history(selected_project)
