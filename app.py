"""
XHS Content Workstation — Main Streamlit Application
小红书内容自动化工作台 v2.0

Entry point: streamlit run app.py
"""

from __future__ import annotations

import copy
import html as _html
import json
import re
import threading
import time
import uuid
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
import dedup as dedup_module
import telemetry
import validator
import logger_utils  # R-023: 错误展示前脱敏 secret
import librarian_client  # R-032: TV 飞轮馆员 pull 客户端

# ── 生成编排搬到 generation_service.py(审计 ROB-001/002 · SUP-011/012)──
# 那些函数一个 st.* 都不碰, 但定义写在 app.py 里就只能跟着 Streamlit 进程
# 活 —— 进程一重启, 正在跑的队列整批蒸发。搬出去之后 worker.py 也 import
# 得动, 队列才谈得上落地。这里 as 出来的名字与搬迁前完全一致, UI 代码一行没动。
from generation_service import (
    NULL_LOCK as _NULL_LOCK,
    queue_worker as _queue_worker,
    quick_gen_worker as _quick_gen_worker,
)

_BEIJING_TZ = timezone(timedelta(hours=8))

# Streamlit's @st.fragment auto-reruns only the decorated block, leaving
# the rest of the page alone.  Available since Streamlit 1.33; we fall back
# to the plain function on older versions.
_FRAGMENT = getattr(st, "fragment", None)


def _queue_banner_body() -> None:
    """The actual banner content.  Factored out so the same code runs both
    in fragment mode and as a normal function on Streamlit < 1.33."""
    qs = st.session_state.get("queue_state")
    if not qs:
        return
    completed = len(qs.get("completed", []))
    total     = qs.get("total", 0)
    msg       = qs.get("message", "")
    errors    = qs.get("errors", [])

    # 状态卡死逃生口：phase 在 starting / stopping 过渡态停留太久（worker 异常
    # 退出 + finally 未及时跑完）会让启动 / 停止按钮全 disabled，用户除了重启
    # 浏览器没有出路。这里给一个手动重置入口。is_running=False 时如果 phase
    # 还卡在 starting/stopping 就显示。
    cur_phase_b = qs.get("phase")
    if (not qs.get("running")) and cur_phase_b in ("starting", "stopping"):
        bcol_warn, bcol_reset = st.columns([5, 1])
        with bcol_warn:
            st.warning(
                f"⚠ 队列状态卡在「{cur_phase_b}」。worker 可能已经退出。"
                "点击右侧重置以恢复操作。"
            )
        with bcol_reset:
            if st.button("🔄 重置", key="reset_stuck_queue", use_container_width=True):
                st.session_state.pop("queue_state", None)
                st.session_state.pop("queue_stop_event", None)
                st.rerun()
        return

    if qs.get("running"):
        banner = st.container()
        with banner:
            bcol_txt, bcol_btn = st.columns([5, 1])
            with bcol_txt:
                st.info(f"🔄 队列生成中 ({completed}/{total}) — {msg}")
            with bcol_btn:
                if st.button("⏹ 停止", key="global_stop_queue", use_container_width=True):
                    # 与 tab 内的"停止队列"按钮保持一致：先写 phase=stopping 让
                    # 其它按钮立刻 disabled，再发 stop_event。否则两个入口的状态
                    # 不一致，用户在 tab 内/banner 上各点一次就乱掉。
                    try:
                        with (qs.get("_lock") or _NULL_LOCK):
                            qs["phase"] = "stopping"
                    except Exception:
                        qs["phase"] = "stopping"
                    evt = st.session_state.get("queue_stop_event")
                    if evt:
                        evt.set()
    elif qs.get("done"):
        comp_list = qs.get("completed", [])
        n_total   = len(comp_list)
        n_empty   = sum(1 for c in comp_list if not c.get("saved"))
        n_content = n_total - n_empty
        # 没进 completed 的计划 = 落库前就崩了 / setup 失败（缺 system prompt 等）/
        # 被中途停掉 —— 它们只 append 到 errors、不进 completed。这些和"完成但 0 内容"
        # 一样都算失败，不能当成绿色"提示"放过（review #39：否则崩在 append 之前的
        # 计划会被 elif 分支伪装成 advisory ✅）。
        n_no_content = n_empty + max(0, total - n_total)
        # 同一个 API 错误会按每条版本重复 N 次，去重后再展示，免得糊一整屏。
        # R-040: worker 写进 errors 的是原始 str(exc) —— 渲染前统一脱敏,
        # 不能只靠错误面板那一处(R-023 的目标是所有 UI 错误展示路径)。
        uniq_errors = [logger_utils.mask_secrets(e) for e in dict.fromkeys(errors)]
        bcol_txt, bcol_btn = st.columns([5, 1])
        with bcol_txt:
            if n_no_content:
                sample = "；".join(uniq_errors[:4])
                more = f"…（另有 {len(uniq_errors) - 4} 条）" if len(uniq_errors) > 4 else ""
                st.warning(
                    f"⚠️ 队列完成：{n_content}/{total} 个批次有内容，"
                    f"**{n_no_content} 个批次没产出内容**（模型 API 报错/超额，或计划失败/中断）。"
                    + (f"\n\n错误：{sample}{more}" if uniq_errors else "")
                )
            elif uniq_errors:
                # 有内容但带提示（去重命中 / 硬约束标记等）：如实展示，但不报成失败。
                st.info(
                    f"✅ 队列完成！{n_total} 个批次已保存，另有 {len(uniq_errors)} 条提示："
                    + "；".join(uniq_errors[:4])
                )
            else:
                st.success(f"✅ 队列完成！{n_total} 个批次已保存 — {msg}")
        with bcol_btn:
            if st.button("清除", key="clear_queue_status", use_container_width=True):
                st.session_state.pop("queue_state", None)
                st.session_state.pop("queue_stop_event", None)
                st.rerun()

        # Phase 2.3: 本次运行有 session 因占满窗口被自动封窗(下批已自动开新窗)
        sealed = sorted(set(qs.get("sealed_engines") or []))
        if sealed:
            st.caption(
                f"🪟 {'、'.join(e.upper() for e in sealed)} 的会话窗口已满，已自动封窗并开新窗"
                "（新窗自动继承最近 50 条已通过历史，避重不断）。"
            )

        # ── 非阻塞警告（embedding 降级 / 历史向量加载失败等）─────────────
        warnings_list = qs.get("warnings") or []
        embedding_missing = qs.get("embedding_missing") or []
        if warnings_list or embedding_missing:
            with st.expander(
                f"⚠ 本次队列运行警告 ({len(warnings_list) + (1 if embedding_missing else 0)})",
                expanded=False,
            ):
                for w in warnings_list[:20]:
                    st.markdown(f"- {w}")
                if len(warnings_list) > 20:
                    st.caption(f"…还有 {len(warnings_list) - 20} 条")
                if embedding_missing:
                    st.markdown(
                        f"- **{len(embedding_missing)} 条版本缺少向量**："
                        "Supabase upsert/update 失败，本次写入未存到 versions.embedding 列；"
                        "后续跨批次去重会读不到这些向量，可能导致重复率上升。"
                    )

    # Day 4：去重 + 注入指标看板（每批一张卡）
    # 移到 if/elif 之外：队列还在跑时，每完成一批就 append 一条到
    # metrics_list，fragment 每 2s rerun 一次，用户能实时看到已完成批次的
    # 数据，不必等全部跑完才出现。``_render_queue_dashboard`` 内部已经做
    # 了"空 metrics_list 直接 return"的判断。
    try:
        _render_queue_dashboard(qs)
    except Exception as exc:
        # 任何渲染失败（数据形态异常 / DB roundtrip 字符串等）都不应让
        # 整页变成 Streamlit 红色 ErrorBox——挂一行 caption 让用户知道
        # 面板坏了但生成本身没受影响。
        st.caption(f"⚠ 指标面板渲染失败：{exc}")


def _fmt_tok(n: int) -> str:
    """Token 数缩写：12345 → 12.3K，1234567 → 1.23M。卡片宽度有限，
    全位数会换行；K/M 比"千/万"更国际、跟成本面板一致。"""
    if not n:
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(int(n))


def _short_model(model_full: str) -> str:
    """``claude/claude-sonnet-4-5-20250929`` → ``claude · sonnet-4-5``。

    去掉重复的引擎名前缀和末尾日期戳，让 metric 行标题不长。新模型自然兼容
    （不依赖白名单），未知 id 直接原样返回。
    """
    if "/" not in (model_full or ""):
        return model_full or "?"
    eng, m = model_full.split("/", 1)
    if m.startswith(eng + "-"):
        m = m[len(eng) + 1:]
    parts = m.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 8:
        m = parts[0]
    return f"{eng} · {m}"


def _render_token_row(label: str, u: dict, cost_field: str = "cost_usd") -> None:
    """渲染一行 5 列 token metric。``u`` 是 token usage dict（含 input /
    cache_read / cache_create / output / cost_usd）；``label`` 在上面挂一行
    caption。"""
    if label:
        st.caption(label)
    cols = st.columns(5)
    cols[0].metric("input",        _fmt_tok(int(u.get("input") or 0)))
    cols[1].metric("cache_read",   _fmt_tok(int(u.get("cache_read") or 0)))
    cols[2].metric("cache_create", _fmt_tok(int(u.get("cache_create") or 0)))
    cols[3].metric("output",       _fmt_tok(int(u.get("output") or 0)))
    cols[4].metric("≈ 成本",       f"${float(u.get(cost_field) or 0):.4f}")


def _render_token_panel(meta) -> None:
    """渲染本批的 token 用量 + 估算费用 + cache 命中。两个面板（队列实时
    / 历史回看）共享。

    ``meta`` 通常是 ``BatchMetrics.to_dict()["meta"]``，但历史页从 DB 取出的
    JSONB 字段可能以字符串形态回来（同文件的 phase_ms / counters 都需要 json
    解一次，meta 同样要做），所以这里再容错一层 JSON parse，确保历史 expander
    不会因为 str.get 崩溃整页。

    多引擎批次按引擎拆行（A 方案）——每个引擎独立一行 5 列 metric，下面挂
    一行"合计" caption；单引擎/无 by_model 元数据时退化成单行聚合，跟旧行为
    一致。
    """
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    if not isinstance(meta, dict):
        return
    totals = meta.get("token_totals") or {}
    if isinstance(totals, str):
        try:
            totals = json.loads(totals)
        except Exception:
            totals = {}
    if not isinstance(totals, dict) or not totals:
        return
    try:
        cost = float(totals.get("cost_usd") or 0.0)
        by_model = totals.get("by_model") or {}
        saved = config.estimate_cache_savings_usd(by_model) if isinstance(by_model, dict) else 0.0

        per_model_rows: list[tuple[str, dict]] = []
        if isinstance(by_model, dict):
            for mid, u in by_model.items():
                if isinstance(u, dict):
                    per_model_rows.append((mid, u))

        if len(per_model_rows) >= 2:
            # 多引擎：按引擎一行 metric + 一行合计
            # 排序按成本降序，最贵的引擎排最上面便于一眼看到主要支出
            per_model_rows.sort(key=lambda x: float(x[1].get("cost_usd") or 0), reverse=True)
            for mid, u in per_model_rows:
                _render_token_row(f"**{_short_model(mid)}**", u)
            st.caption(
                f"**合计**　input {_fmt_tok(int(totals.get('input') or 0))} · "
                f"cache_read {_fmt_tok(int(totals.get('cache_read') or 0))} · "
                f"cache_create {_fmt_tok(int(totals.get('cache_create') or 0))} · "
                f"output {_fmt_tok(int(totals.get('output') or 0))} · "
                f"≈ ${cost:.4f}"
            )
        else:
            # 单引擎或没 by_model 数据：直接显示聚合 totals
            _render_token_row("", totals)

        extras: list[str] = []
        if saved > 0:
            extras.append(f"🟢 cache 已省 ≈ ${saved:.4f}")
        thinking = int(totals.get("thinking") or 0)
        if thinking:
            extras.append(f"thinking {_fmt_tok(thinking)}")
        if extras:
            st.caption(" · ".join(extras))

        by_source = totals.get("by_source") or {}
        if isinstance(by_source, dict) and (len(by_source) > 1 or "compliance_recheck" in by_source):
            # 多个来源（主生成 + 合规复审等）时拆开展示，避免"main 占了多少 / 内部
            # 辅助调用占了多少"被合并后看不出来。
            src_lines = []
            for src, u in by_source.items():
                if not isinstance(u, dict):
                    continue
                label = {"main": "主生成", "compliance_recheck": "合规复审",
                         "dedup_regen": "去重重生",
                         "multi_role_select": "三省选优",
                         "multi_role_refine": "三省精修"}.get(src, src)
                src_lines.append(f"{label}: ${float(u.get('cost_usd') or 0):.4f}")
            if src_lines:
                st.caption("分来源：" + " · ".join(src_lines))
    except Exception as exc:
        # token panel 本身崩了不要把整个批次卡片带下水；面板只是观测，
        # 出错就降级成一行错误提示，让用户至少能看到耗时和去重数据。
        st.caption(f"⚠ token 面板数据异常：{exc}")


def _render_queue_dashboard(qs: dict, title: str = "本次队列指标") -> None:
    """渲染队列 / 快速生成每个批次的指标卡：阶段计时 + 去重/重生/违规计数
    + 注入摘要 + token/cost 面板。

    数据源是 ``metrics.close()`` 落到 ``qs["metrics_list"]`` 的每批快照；
    本函数只读、纯展示，不做任何 DB I/O，调用频率与刷新成本都可忽略。
    队列 worker 和 quick-gen worker 都往各自 status 的 ``metrics_list`` 挂,
    所以这个 dashboard 两条路径通用——``title`` 区分文案(队列/单次生成)。

    生成中默认展开（用户想看实时数据），完成后默认折叠（成功 banner 已经
    在上面、不抢焦点）。每个批次卡片用 try/except 包，单批数据异常不会
    让其余批次的卡片一起跟着崩。
    """
    metrics_list = qs.get("metrics_list") or []
    if not metrics_list:
        return
    is_running = bool(qs.get("running"))
    with st.expander(
        f"📊 {title}（{len(metrics_list)} 批）",
        expanded=is_running,
    ):
        for idx, m in enumerate(metrics_list):
            try:
                _render_batch_card(idx, m)
            except Exception as exc:
                st.caption(f"⚠ 批次 {idx + 1} 卡片渲染失败：{exc}")
            if idx < len(metrics_list) - 1:
                st.divider()


def _render_batch_card(idx: int, m: dict) -> None:
    """单批指标卡片。提出来是为了让 _render_queue_dashboard 的 try/except
    粒度落到"单卡片"——某一批数据形态异常时其它批照常展示。"""
    engines = ", ".join(m.get("engines") or []) or "?"
    st.markdown(
        f"**批次 {idx + 1}** · "
        f"`{(m.get('batch_id') or '')[:8]}…` · "
        f"{m.get('count', 0)} 条 · 引擎 {engines} · "
        f"总耗时 **{m.get('total_ms', 0) / 1000:.1f} s**"
    )

    phase_ms = m.get("phase_ms") or {}
    cols = st.columns(4)
    for col, (k, label) in zip(cols, [
        ("setup", "setup"),
        ("llm", "llm"),
        ("db_save", "db_save"),
        ("embedding", "embedding"),
    ]):
        with col:
            st.metric(label, f"{phase_ms.get(k, 0) / 1000:.1f} s")

    _render_token_panel(m.get("meta") or {})

    counters = m.get("counters") or {}
    counter_keys = [
        ("dedup_text_hits", "文本去重命中"),
        ("dedup_semantic_hits", "语义去重命中"),
        ("regen_attempts", "重生尝试"),
        ("regen_success", "重生成功"),
        ("hard_rule_violations", "硬规则违反"),
        ("embedding_missing", "缺向量"),
    ]
    ccols = st.columns(len(counter_keys))
    for col, (k, label) in zip(ccols, counter_keys):
        with col:
            st.metric(label, counters.get(k, 0))

    meta = m.get("meta") or {}
    injection = meta.get("injection") or {}
    dedup_mode = meta.get("dedup_mode", "vector")
    dedup_threshold = meta.get("dedup_threshold")
    if injection or dedup_mode != "vector" or dedup_threshold is not None:
        hard_n = (injection.get("hard_global", 0) + injection.get("hard_project", 0))
        soft_n = (injection.get("soft_global", 0) + injection.get("soft_project", 0))
        sess_n = injection.get("session", 0)
        calib_chars = injection.get("calibration_chars", 0)
        filtered = injection.get("filtered") or []
        badges = [
            f"硬 {hard_n}", f"软 {soft_n}", f"会话 {sess_n}",
            f"调校 {calib_chars} 字", f"过滤 {len(filtered)} 条",
        ]
        if dedup_threshold is not None:
            badges.append(f"阈值 {dedup_threshold:.2f}")
        if dedup_mode != "vector":
            badges.append(f"去重 {dedup_mode}")
        st.caption(" · ".join(badges))

        if filtered:
            with st.expander(
                f"查看本批被过滤的 {len(filtered)} 条规则",
                expanded=False,
            ):
                by_reason: dict[str, list[dict]] = {}
                for f in filtered:
                    by_reason.setdefault(f.get("reason", "?"), []).append(f)
                for reason, items in by_reason.items():
                    reason_label = {
                        "below_threshold": "相关度低于阈值",
                        "muted": "已静音",
                        "capped": "超过条数上限",
                        "no_embedding": "缺 embedding",
                    }.get(reason, reason)
                    st.markdown(f"**{reason_label}** ({len(items)} 条)")
                    for item in items[:10]:
                        score = item.get("score")
                        score_text = (
                            f" (相似度 {score:.2f})" if score is not None else ""
                        )
                        st.markdown(f"- {item.get('content', '')}{score_text}")
                    if len(items) > 10:
                        st.caption(f"…还有 {len(items) - 10} 条")


if _FRAGMENT is not None:
    # When a queue is running, this fragment auto-reruns every 2 s without
    # forcing the rest of the page to re-execute.  Massive perceived-perf
    # win: pre-fragment, every 2 s the entire 3000+ line script re-ran just
    # to update the progress bar, which is why other UI felt frozen during
    # batch runs.
    _queue_banner = _FRAGMENT(run_every=2.0)(_queue_banner_body)
else:
    _queue_banner = _queue_banner_body


def _running_snapshot_body(qs_key: str = "queue_state") -> None:
    """Live progress bar that polls queue state via fragment refresh.

    ``qs_key`` is the session_state key to read (queue tab uses queue_state;
    the quick-generate path uses queue_state_qg).
    """
    qs = st.session_state.get(qs_key)
    if not qs or not qs.get("running"):
        return
    completed = len(qs.get("completed", []))
    total     = qs.get("total", 0)
    progress  = qs.get("progress")
    if progress is None and total:
        progress = completed / total
    elif progress is None:
        progress = 0.0
    st.progress(progress, text=qs.get("message", "生成中…"))
    st.caption("生成在后台运行，可切换到其他页面。")


if _FRAGMENT is not None:
    _render_running_snapshot = _FRAGMENT(run_every=2.0)(_running_snapshot_body)
else:
    _render_running_snapshot = _running_snapshot_body


def _qg_key(project_id: Optional[str] = None) -> str:
    """``quick_gen_state`` 的 session_state 键。按项目隔离，避免在项目 A 跑生成
    后切到项目 B，B 的「生成」tab 顶部还在显示 A 的「✅ 生成完成」横幅，把 A
    的 batch_id 误塞给 B 的「前往审核」按钮。"""
    pid = project_id or st.session_state.get("current_project_id", "")
    return f"quick_gen_state_{pid}" if pid else "quick_gen_state"


def _rb_key(project_id: Optional[str] = None) -> str:
    """``review_batch_id`` 的 session_state 键，同样按项目隔离。"""
    pid = project_id or st.session_state.get("current_project_id", "")
    return f"review_batch_id_{pid}" if pid else "review_batch_id"


def _quick_gen_snapshot_body() -> None:
    """Live progress for the quick-generate path, mirrors _running_snapshot_body
    but reads from the per-project ``quick_gen_state_<pid>`` key."""
    qgs = st.session_state.get(_qg_key())
    if not qgs or not qgs.get("running"):
        return
    pct = qgs.get("progress", 0.0)
    msg = qgs.get("message", "生成中…")
    st.progress(pct, text=msg)
    st.caption("生成在后台运行，可切换到其他页面。")


if _FRAGMENT is not None:
    _render_quick_gen_snapshot = _FRAGMENT(run_every=2.0)(_quick_gen_snapshot_body)
else:
    _render_quick_gen_snapshot = _quick_gen_snapshot_body


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
    page_icon="✦",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ─────────────────────────────────────────────────────────────
st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

/* ═══════════════════════════════════════════
   Design tokens — Studio-grade (hard-edge + lime)
   No rounded corners. Lines are the primary
   structural element. Lime accent only for
   focus / active / chapter numbering.
   ═══════════════════════════════════════════ */
:root {
  /* Palette */
  --accent:        #C6F75C;
  --accent-2:      #A8E028;
  --accent-soft:   #E9FBB8;
  --bg:            #FFFFFF;
  --bg-soft:       #F5F5F3;
  --card:          #FFFFFF;
  --card-inverse:  #0A0A0A;
  --text-1:        #0A0A0A;
  --text-2:        #5A5A5A;
  --text-3:        #9A9A9A;
  --text-on-dark:  #FFFFFF;
  --border:        #E5E5E2;
  --border-mid:    #C9C9C3;
  --border-strong: #0A0A0A;
  --green:         #16A34A;
  --green-soft:    #F0FDF4;
  --amber:         #D97706;
  --amber-soft:    #FFFBEB;
  --slate:         #64748B;
  --slate-soft:    #F8FAFC;

  /* Shadows — kept extremely subtle; studio style relies on lines not depth */
  --shadow-sm:     0 1px 0 rgba(10,10,10,.04);
  --shadow-md:     0 2px 0 rgba(10,10,10,.06);

  /* Radii — hard edges */
  --r-sm:   0px;
  --r-md:   2px;
  --r-lg:   0px;
  --r-pill: 0px;

  /* Line weights */
  --line-thin:   1px;
  --line:        1.5px;
  --line-heavy:  2px;

  /* Font sizes (type scale) */
  --fs-hero:    3.25rem;
  --fs-title:   1.75rem;
  --fs-card:    1.125rem;
  --fs-body:    0.9375rem;
  --fs-meta:    0.75rem;
  --fs-tag:     0.72rem;

  /* Spacing rhythm */
  --sp-section: 3.5rem;
  --sp-block:   1.75rem;
  --sp-card:    1rem;
  --sp-line:    0.5rem;

  /* Fonts */
  --font-sans: "Space Grotesk", -apple-system, BlinkMacSystemFont, "Inter",
               "Segoe UI", Helvetica, Arial, sans-serif;
  --font-mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
}

/* ── App background & typography ── */
.stApp, .stApp > .main {
  background: var(--bg) !important;
  font-family: var(--font-sans);
  color: var(--text-1);
}

/* ── Sidebar — left rail with chapter nav ── */
[data-testid="stSidebar"] {
  background: var(--bg) !important;
  border-right: var(--line) solid var(--border-strong) !important;
}
[data-testid="stSidebar"] .stMarkdown p,
[data-testid="stSidebar"] .stMarkdown small,
[data-testid="stSidebar"] label {
  color: var(--text-2) !important;
  font-size: 0.8125rem !important;
}
[data-testid="stSidebar"] .stRadio label {
  font-size: 0.9rem !important;
  font-weight: 500;
  color: var(--text-1) !important;
}

/* ── Sidebar nav — hard-edge chapter list (full-width rows) ── */
/* Nuclear: BaseWeb renders each radio option inside
   <div data-baseweb="radio"> which defaults to inline-block
   (content-width). Target those attribute wrappers directly. */
[data-testid="stSidebar"] [data-baseweb="radio-group"],
[data-testid="stSidebar"] [data-baseweb="radio"] {
  display: block !important;
  width: 100% !important;
  max-width: 100% !important;
  box-sizing: border-box !important;
}
/* Force every ancestor container to the full sidebar width */
[data-testid="stSidebar"] [data-testid="stRadio"],
[data-testid="stSidebar"] [data-testid="stRadio"] > div,
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"],
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] > div,
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] > * {
  width: 100% !important;
  max-width: 100% !important;
}
/* Switch the radiogroup from its default flex-wrap (which sizes
   each item to its content) to simple block stacking */
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] {
  display: block !important;
  border-top: var(--line-thin) solid var(--border) !important;
}
/* BaseWeb wraps radio items 2 levels deep; force EVERY div inside
   the radiogroup to full-width block so the final <label> actually
   resolves width:100% against the sidebar, not a content-sized
   wrapper */
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] div {
  display: block !important;
  width: 100% !important;
  max-width: 100% !important;
}
/* Item label = a full-width row */
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label {
  display: flex !important;
  align-items: center !important;
  width: 100% !important;
  box-sizing: border-box !important;
  border-radius: 0 !important;
  padding: 12px 14px 12px 18px !important;
  margin: 0 !important;
  transition: background 0.12s;
  cursor: pointer;
  border: none !important;
  border-bottom: var(--line-thin) solid var(--border) !important;
  border-left: 3px solid transparent !important;
  letter-spacing: 0.01em;
}
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label:hover {
  background: var(--bg-soft) !important;
}
/* Hide the default radio dot */
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label > div:first-child {
  display: none !important;
}
/* Text element spans the whole row */
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label > div:last-child,
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label > div:last-child > p {
  width: 100% !important;
  margin: 0 !important;
}
/* Selected nav item — lime left rail + bold label */
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label:has(input:checked) {
  background: var(--bg-soft) !important;
  border-left: 3px solid var(--accent) !important;
}
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label:has(input:checked) p,
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label:has(input:checked) div {
  color: var(--text-1) !important;
  font-weight: 700 !important;
}

/* ── Forms — hard-edge bordered frame ── */
[data-testid="stForm"] {
  border: var(--line) solid var(--text-1) !important;
  border-radius: 0 !important;
  padding: 1.5rem !important;
  background: var(--card) !important;
}
/* Forms inside the sidebar stay borderless to save width */
[data-testid="stSidebar"] [data-testid="stForm"] {
  border: none !important;
  padding: 0 !important;
}

/* ── Main content area ── */
.main .block-container {
  padding-top: 2.5rem !important;
  padding-bottom: 5rem !important;
  max-width: 1180px !important;
}

/* ── Headings ── */
h1 { font-size: var(--fs-title) !important; font-weight: 700 !important;
     color: var(--text-1) !important; letter-spacing: -0.03em;
     line-height: 1.1 !important; }
h2 { font-size: 1.375rem !important; font-weight: 700 !important;
     color: var(--text-1) !important; letter-spacing: -0.02em; }
h3 { font-size: 1.05rem !important; font-weight: 600 !important;
     color: var(--text-1) !important; letter-spacing: -0.01em; }

/* ── Buttons — hard-edge square ── */
.stButton > button {
  border-radius: 0 !important;
  font-size: 0.875rem !important;
  font-weight: 500 !important;
  padding: 0.6rem 1.1rem !important;
  border: var(--line) solid var(--text-1) !important;
  background: var(--card) !important;
  color: var(--text-1) !important;
  box-shadow: none !important;
  transition: background 0.12s, color 0.12s !important;
  letter-spacing: 0.005em;
}
.stButton > button:hover {
  background: var(--text-1) !important;
  color: var(--text-on-dark) !important;
  transform: none;
  box-shadow: none !important;
}
/* Primary / type=primary button — solid black square */
.stButton > button[kind="primary"] {
  background: var(--text-1) !important;
  border-color: var(--text-1) !important;
  color: var(--text-on-dark) !important;
  box-shadow: none !important;
  padding: 0.65rem 1.3rem !important;
}
.stButton > button[kind="primary"]:hover {
  background: var(--accent) !important;
  border-color: var(--text-1) !important;
  color: var(--text-1) !important;
}
/* Form submit buttons inherit primary treatment */
[data-testid="stFormSubmitButton"] > button {
  background: var(--text-1) !important;
  border: var(--line) solid var(--text-1) !important;
  color: var(--text-on-dark) !important;
  border-radius: 0 !important;
  padding: 0.65rem 1.3rem !important;
  font-weight: 500 !important;
}
[data-testid="stFormSubmitButton"] > button:hover {
  background: var(--accent) !important;
  border-color: var(--text-1) !important;
  color: var(--text-1) !important;
}

/* ── Inputs, textareas, selects — hard-edge ── */
.stTextInput > div > div > input,
.stTextArea > div > div > textarea,
.stSelectbox > div > div > div,
.stNumberInput > div > div > input,
.stDateInput > div > div > input {
  border-radius: 0 !important;
  border: var(--line) solid var(--border-mid) !important;
  background: var(--card) !important;
  font-size: 0.9rem !important;
  color: var(--text-1) !important;
  box-shadow: none !important;
  transition: border-color 0.15s !important;
}
.stTextInput > div > div > input:focus,
.stTextArea > div > div > textarea:focus,
.stNumberInput > div > div > input:focus {
  border-color: var(--text-1) !important;
  box-shadow: 0 0 0 3px rgba(198,247,92,.50) !important;
  outline: none !important;
}

/* ── Sliders ── */
[data-testid="stSlider"] .stSlider div[role="slider"] {
  background: var(--text-1) !important;
  border-radius: 0 !important;
}
[data-testid="stSlider"] [data-baseweb="slider"] > div > div > div {
  background: var(--accent) !important;
}

/* ── Expanders — hard-edge card ── */
.stExpander {
  border: var(--line) solid var(--border-strong) !important;
  border-radius: 0 !important;
  background: var(--card) !important;
  box-shadow: none !important;
  margin-bottom: 0 !important;
  margin-top: -1.5px !important;  /* collapse shared borders */
  overflow: hidden;
  transition: none;
}
.stExpander:hover {
  box-shadow: none !important;
  border-color: var(--border-strong) !important;
}
[data-testid="stExpander"] > details > summary {
  padding: 18px 22px !important;
  background: var(--card) !important;
  font-size: 0.95rem !important;
  font-weight: 500 !important;
  color: var(--text-1) !important;
  border-radius: 0 !important;
  letter-spacing: -0.005em;
}
[data-testid="stExpander"] > details[open] > summary {
  border-bottom: var(--line-thin) solid var(--border-strong) !important;
  border-radius: 0 !important;
  background: var(--card) !important;
}
[data-testid="stExpander"] > details > div {
  padding: 22px !important;
  background: var(--card) !important;
}

/* ── Dark copy card (approved status) ── */
.card-status-marker { display: none; }
.element-container:has(.card-status-marker) { height: 0; margin: 0 !important; padding: 0 !important; }
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] {
  background: var(--card-inverse) !important;
  border-color: var(--card-inverse) !important;
}
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] summary,
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] summary * {
  background: var(--card-inverse) !important;
  color: var(--text-on-dark) !important;
}
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] > details[open] > summary {
  border-bottom-color: #2A2A2A !important;
}
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] > details > div {
  background: var(--card-inverse) !important;
  color: var(--text-on-dark) !important;
}
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] .copy-title,
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] .copy-body,
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] p,
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] h1,
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] h2,
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] h3,
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] label,
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] span {
  color: var(--text-on-dark) !important;
}
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] .copy-body {
  border-left-color: var(--accent) !important;
}
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] .stButton > button {
  border-color: #FFFFFF !important;
  color: var(--text-on-dark) !important;
  background: transparent !important;
}
.element-container:has(.card-approved) + .element-container [data-testid="stExpander"] .stButton > button:hover {
  background: var(--accent) !important;
  color: var(--text-1) !important;
  border-color: var(--accent) !important;
}

/* ── Tabs — hard-edge ── */
.stTabs [data-baseweb="tab-list"] {
  gap: 0 !important;
  border-bottom: var(--line) solid var(--border-strong) !important;
}
.stTabs [data-testid="stTab"] {
  border-radius: 0 !important;
  font-size: 0.82rem !important;
  font-weight: 600 !important;
  color: var(--text-3) !important;
  padding: 12px 18px !important;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  border-right: var(--line-thin) solid var(--border) !important;
}
.stTabs [aria-selected="true"] {
  color: var(--text-1) !important;
  font-weight: 700 !important;
  border-bottom: 3px solid var(--accent) !important;
  background: var(--bg-soft) !important;
}

/* ── Info / warning / error / success boxes ── */
[data-testid="stAlert"] {
  border-radius: 0 !important;
  border: var(--line) solid var(--border-strong) !important;
  font-size: 0.875rem !important;
}

/* ── Dividers — use thin lines, large rhythm ── */
hr {
  border: none !important;
  border-top: var(--line-thin) solid var(--border) !important;
  margin: 1.5rem 0 !important;
}

/* ── Metrics — numbers over a black underline ── */
[data-testid="stMetric"] {
  background: transparent !important;
  border: none !important;
  border-bottom: var(--line-heavy) solid var(--text-1) !important;
  border-radius: 0 !important;
  padding: 14px 0 10px !important;
  box-shadow: none !important;
  transition: none;
}
[data-testid="stMetric"]:hover {
  box-shadow: none !important;
}
[data-testid="stMetricLabel"] {
  font-size: var(--fs-tag) !important;
  font-weight: 600 !important;
  text-transform: uppercase !important;
  letter-spacing: 0.12em !important;
  color: var(--text-3) !important;
}
[data-testid="stMetricValue"] {
  font-size: 2.25rem !important;
  font-weight: 700 !important;
  color: var(--text-1) !important;
  line-height: 1 !important;
  letter-spacing: -0.03em;
  margin-top: 4px;
}

/* ── Checkboxes & radios ── */
.stCheckbox label, .stRadio label {
  font-size: 0.875rem !important;
}

/* ── Progress bar — lime fill, hard edges ── */
.stProgress > div > div > div {
  background: var(--accent) !important;
  border-radius: 0 !important;
}
.stProgress > div > div {
  border-radius: 0 !important;
  background: var(--border) !important;
  height: 4px !important;
}

/* ── Multiselect — hard-edge lime tag ── */
.stMultiSelect > div > div {
  border-radius: 0 !important;
  border: var(--line) solid var(--border-mid) !important;
  background: var(--card) !important;
  font-size: 0.875rem !important;
}
.stMultiSelect span[data-baseweb="tag"] {
  background: var(--accent) !important;
  border: var(--line-thin) solid var(--text-1) !important;
  border-radius: 0 !important;
  font-size: 0.75rem !important;
  font-weight: 600 !important;
  color: var(--text-1) !important;
  letter-spacing: 0.01em;
}

/* ── Spinner ── */
.stSpinner > div {
  border-top-color: var(--text-1) !important;
}

/* ── Download button — hard-edge ── */
.stDownloadButton > button {
  border-radius: 0 !important;
  border: var(--line) solid var(--text-1) !important;
  background: var(--card) !important;
  font-size: 0.875rem !important;
  font-weight: 500 !important;
  color: var(--text-1) !important;
  padding: 0.6rem 1.1rem !important;
}
.stDownloadButton > button:hover {
  background: var(--text-1) !important;
  color: var(--text-on-dark) !important;
}

/* ════════════════════════════════════════════
   Studio-grade custom components
   ════════════════════════════════════════════ */

/* ── Hero page header (chapter + title + deco) ── */
.hero {
  display: grid;
  grid-template-columns: 1fr auto;
  align-items: start;
  gap: 2rem;
  margin-bottom: 2.5rem;
  padding-bottom: 2rem;
  border-bottom: var(--line-heavy) solid var(--text-1);
}
.hero-inner { min-width: 0; }
.hero-tag {
  font-family: var(--font-mono);
  font-size: var(--fs-tag);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.14em;
  color: var(--text-1);
  margin-bottom: 1.25rem;
  display: inline-flex; align-items: baseline; gap: 0.4em;
}
.hero-tag .hero-tag-num {
  color: var(--text-1);
  background: var(--accent);
  border: var(--line-thin) solid var(--text-1);
  padding: 3px 8px;
  line-height: 1;
}
.hero-tag .hero-tag-name {
  color: var(--text-1);
}
.hero-title {
  font-size: var(--fs-hero) !important;
  font-weight: 700 !important;
  letter-spacing: -0.04em !important;
  line-height: 0.98 !important;
  margin: 0 !important;
  color: var(--text-1);
}
.hero-sub {
  margin-top: 1rem;
  font-size: 0.95rem;
  color: var(--text-2);
  max-width: 48ch;
  line-height: 1.5;
}
.hero-deco {
  font-size: 4.5rem;
  line-height: 1;
  color: var(--text-1);
  font-weight: 700;
  align-self: start;
  padding-top: 0.5rem;
  user-select: none;
}

/* ── Section tag (▸ NUM / NAME) ── */
.section-tag {
  font-family: var(--font-mono);
  font-size: var(--fs-tag);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.14em;
  color: var(--text-1);
  margin: 0 0 0.75rem 0;
  display: inline-flex; align-items: center; gap: 0.5em;
}
.section-tag::before {
  content: "▸";
  color: var(--accent-2);
  font-size: 1em;
}
.section-divider {
  border: none !important;
  border-top: var(--line-thin) solid var(--text-1) !important;
  margin: var(--sp-section) 0 2rem !important;
  opacity: 1 !important;
}

/* ── Stat row (row of underlined numbers) ── */
.stat-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 0;
  margin: 0 0 2rem 0;
  border-top: var(--line-thin) solid var(--text-1);
  border-bottom: var(--line-heavy) solid var(--text-1);
}
.stat-badge {
  display: flex; flex-direction: column; align-items: flex-start; gap: 6px;
  background: transparent;
  border: none;
  border-right: var(--line-thin) solid var(--border);
  border-radius: 0;
  padding: 14px 18px;
  min-width: 0;
  transition: background 0.12s;
}
.stat-badge:last-child { border-right: none; }
.stat-badge:hover {
  background: var(--bg-soft);
  border-color: var(--border);
  transform: none;
  box-shadow: none;
}
.stat-badge .sb-num {
  font-size: 2rem; font-weight: 700; color: var(--text-1); line-height: 1;
  letter-spacing: -0.03em;
  font-variant-numeric: tabular-nums;
}
.stat-badge .sb-lbl {
  font-family: var(--font-mono);
  font-size: var(--fs-tag); font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.12em;
  color: var(--text-3);
}
.stat-badge.green  .sb-num { border-bottom: 2px solid var(--green); padding-bottom: 2px; }
.stat-badge.amber  .sb-num { border-bottom: 2px solid var(--amber); padding-bottom: 2px; }
.stat-badge.slate  .sb-num { border-bottom: 2px solid var(--text-3); padding-bottom: 2px; }
.stat-badge.accent .sb-num { border-bottom: 2px solid var(--accent-2); padding-bottom: 2px; }

/* ── Stat-filter button row (review page) ─────────────────────
   Four clickable KPI cards. Each shows a mono label on top and
   a big number below. Cards are colour-coded via a 3px top rule
   (slate / amber / green / accent). Active = solid black with
   lime rule. Scoped via :has() + adjacent sibling so it doesn't
   touch other buttons on the page. */
.element-container:has(.review-stat-filters) + [data-testid="stHorizontalBlock"] .stButton > button {
  height: auto !important;
  min-height: 88px !important;
  padding: 14px 16px 16px !important;
  text-align: left !important;
  border: var(--line) solid var(--border-mid) !important;
  background: var(--card) !important;
  color: var(--text-1) !important;
  border-radius: 0 !important;
  border-top: 3px solid var(--text-3) !important;
  transition: background 0.12s, border-color 0.12s;
  display: flex !important;
  flex-direction: column !important;
  align-items: flex-start !important;
  justify-content: space-between !important;
  white-space: normal !important;
  gap: 8px;
}
.element-container:has(.review-stat-filters) + [data-testid="stHorizontalBlock"] .stButton > button:hover {
  background: var(--bg-soft) !important;
  border-color: var(--border-mid) !important;
  color: var(--text-1) !important;
}
/* Per-column top-rule colour: slate / amber / green / lime */
.element-container:has(.review-stat-filters) + [data-testid="stHorizontalBlock"] > div:nth-child(1) .stButton > button { border-top-color: var(--text-3) !important; }
.element-container:has(.review-stat-filters) + [data-testid="stHorizontalBlock"] > div:nth-child(2) .stButton > button { border-top-color: var(--amber)  !important; }
.element-container:has(.review-stat-filters) + [data-testid="stHorizontalBlock"] > div:nth-child(3) .stButton > button { border-top-color: var(--green)  !important; }
.element-container:has(.review-stat-filters) + [data-testid="stHorizontalBlock"] > div:nth-child(4) .stButton > button { border-top-color: var(--accent-2) !important; }
/* Active filter — solid black card with lime top rule */
.element-container:has(.review-stat-filters) + [data-testid="stHorizontalBlock"] .stButton > button[kind="primary"] {
  background: var(--text-1) !important;
  color: var(--text-on-dark) !important;
  border-color: var(--text-1) !important;
  border-top: 3px solid var(--accent) !important;
}
.element-container:has(.review-stat-filters) + [data-testid="stHorizontalBlock"] .stButton > button[kind="primary"]:hover {
  background: var(--text-1) !important;
  color: var(--text-on-dark) !important;
}
/* First paragraph = the label (small, monospace, uppercase) */
.element-container:has(.review-stat-filters) + [data-testid="stHorizontalBlock"] .stButton > button p:first-child {
  font-family: var(--font-mono) !important;
  font-size: var(--fs-tag) !important;
  font-weight: 600 !important;
  letter-spacing: 0.14em !important;
  text-transform: uppercase !important;
  color: var(--text-3) !important;
  margin: 0 !important;
  line-height: 1.2 !important;
}
.element-container:has(.review-stat-filters) + [data-testid="stHorizontalBlock"] .stButton > button[kind="primary"] p:first-child {
  color: var(--accent) !important;
}
/* Second paragraph = the number (big) */
.element-container:has(.review-stat-filters) + [data-testid="stHorizontalBlock"] .stButton > button p:last-child {
  font-size: 2.15rem !important;
  font-weight: 700 !important;
  letter-spacing: -0.03em !important;
  line-height: 1 !important;
  margin: 0 !important;
  font-variant-numeric: tabular-nums;
  align-self: flex-end;
}

/* ── Status tag (square, monospace) ── */
.status-pill {
  display: inline-flex; align-items: center; gap: 5px;
  font-family: var(--font-mono);
  border-radius: 0;
  font-size: var(--fs-tag); font-weight: 600;
  padding: 3px 10px; line-height: 1.5;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.status-pill.approved {
  background: var(--accent); color: var(--text-1);
  border: var(--line-thin) solid var(--text-1);
}
.status-pill.revision {
  background: var(--amber-soft); color: var(--amber);
  border: var(--line-thin) solid var(--amber);
}
.status-pill.pending {
  background: var(--bg-soft); color: var(--text-2);
  border: var(--line-thin) solid var(--border-mid);
}

/* ── Copy content ── */
.copy-title {
  font-size: var(--fs-card); font-weight: 700;
  color: var(--text-1); line-height: 1.3;
  margin-bottom: 6px;
  letter-spacing: -0.02em;
}
.copy-meta {
  font-family: var(--font-mono);
  font-size: var(--fs-meta); color: var(--text-3);
  margin-bottom: 14px;
  display: flex; align-items: center; gap: 10px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.copy-meta .len-ok  { color: var(--green); font-weight: 600; }
.copy-meta .len-bad { color: var(--amber); font-weight: 600; }
.copy-body {
  font-size: 0.94rem; line-height: 1.75;
  color: var(--text-1); white-space: pre-wrap;
  border-left: 2px solid var(--text-1);
  padding-left: 18px; margin: 14px 0;
}

/* ── Keyword tags — square lime chips ── */
.tag {
  display: inline-flex; align-items: center;
  background: var(--accent-soft);
  border: var(--line-thin) solid var(--text-1);
  border-radius: 0;
  padding: 3px 10px;
  font-size: 0.72rem; font-weight: 600;
  color: var(--text-1);
  margin-right: 6px; margin-bottom: 5px;
  letter-spacing: 0.01em;
  transition: background 0.12s;
}
.tag:hover { background: var(--accent); }

/* ── Engine badge — monospace, black/accent ── */
.engine-badge {
  display: inline-flex; align-items: center; gap: 4px;
  font-family: var(--font-mono);
  background: var(--text-1); color: var(--text-on-dark);
  border-radius: 0; padding: 3px 8px;
  font-size: var(--fs-tag); font-weight: 600;
  letter-spacing: 0.1em; text-transform: uppercase;
}
.engine-badge.gemini,
.engine-badge.claude { background: var(--text-1); }

/* ── Section label (▸ TEXT — hard-edge studio tag) ── */
.section-label {
  display: inline-flex; align-items: center;
  font-family: var(--font-mono);
  background: transparent;
  color: var(--text-1);
  border: none;
  border-left: 3px solid var(--accent);
  border-radius: 0;
  padding: 2px 0 2px 10px;
  font-size: var(--fs-tag); font-weight: 700;
  letter-spacing: 0.14em;
  margin: 1rem 0 0.75rem;
  text-transform: uppercase;
}

/* ── Memory row — hard-edge list item ── */
.mem-card {
  background: var(--card);
  border: none;
  border-bottom: var(--line-thin) solid var(--border);
  border-radius: 0;
  padding: 14px 4px;
  margin-bottom: 0;
  font-size: 0.9rem;
  color: var(--text-1);
  transition: background 0.12s;
}
.mem-card:hover {
  background: var(--bg-soft);
  border-color: var(--border);
  box-shadow: none;
}

/* ── Sidebar user block ── */
.user-block {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 12px;
  background: var(--bg-soft);
  border-radius: 0;
  border: var(--line-thin) solid var(--border);
  margin-bottom: 10px;
}
.user-avatar {
  width: 32px; height: 32px;
  background: var(--text-1); color: var(--accent);
  border-radius: 0;
  display: flex; align-items: center; justify-content: center;
  font-family: var(--font-mono);
  font-size: 0.85rem; font-weight: 700;
  flex-shrink: 0;
}
.user-email { font-size: 0.78rem; font-weight: 500; color: var(--text-1); word-break: break-all; }
.user-ver   { font-family: var(--font-mono); font-size: 0.68rem; color: var(--text-3); letter-spacing: 0.06em; }

/* ── Brand header in sidebar — studio wordmark ── */
.brand-header {
  display: grid;
  grid-template-columns: auto 1fr;
  align-items: center;
  gap: 10px;
  padding: 4px 0 16px;
  border-bottom: var(--line) solid var(--text-1);
  margin-bottom: 14px;
}
.brand-logo {
  font-size: 1.6rem; line-height: 1;
  color: var(--text-1);
  font-weight: 700;
  width: 32px; height: 32px;
  display: flex; align-items: center; justify-content: center;
  background: var(--text-1); color: var(--accent);
}
.brand-name {
  font-size: 0.95rem; font-weight: 700;
  color: var(--text-1); line-height: 1.2;
  letter-spacing: -0.02em;
  text-transform: uppercase;
}
.brand-sub {
  font-family: var(--font-mono);
  font-size: 0.65rem; color: var(--text-3);
  letter-spacing: 0.12em;
  text-transform: uppercase;
}

/* ── Chapter list in sidebar (nav header) ── */
.nav-heading {
  font-family: var(--font-mono);
  font-size: var(--fs-tag);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.14em;
  color: var(--text-3);
  margin: 1.5rem 0 0.5rem;
  padding-left: 4px;
}

/* ── CTA square (36px arrow button) ── */
.cta-square {
  display: inline-flex; align-items: center; justify-content: center;
  width: 36px; height: 36px;
  background: var(--text-1); color: var(--text-on-dark);
  border: var(--line) solid var(--text-1);
  border-radius: 0;
  font-size: 1.05rem;
  line-height: 1;
  transition: background 0.12s, color 0.12s;
  user-select: none;
}
.cta-square:hover {
  background: var(--accent);
  color: var(--text-1);
}

/* ── Login — studio two-column landing ── */
.login-wrap {
  display: grid;
  grid-template-columns: 1.1fr 1fr;
  gap: 4rem;
  align-items: center;
  min-height: 70vh;
  padding: 2rem 0;
}
@media (max-width: 860px) {
  .login-wrap { grid-template-columns: 1fr; gap: 2rem; }
}
.login-hero {
  text-align: left;
  padding: 0;
}
.login-hero .lh-mark {
  display: inline-flex;
  align-items: center; justify-content: center;
  width: 44px; height: 44px;
  background: var(--text-1); color: var(--accent);
  font-size: 1.6rem; font-weight: 700;
  margin-bottom: 1.75rem;
}
.login-hero .lh-badge {
  display: inline-block;
  font-family: var(--font-mono);
  background: var(--accent);
  color: var(--text-1);
  border: var(--line-thin) solid var(--text-1);
  border-radius: 0;
  padding: 3px 10px;
  font-size: var(--fs-tag); font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  margin-bottom: 1.25rem;
}
.login-hero .lh-title {
  font-size: 3rem; font-weight: 700;
  color: var(--text-1); letter-spacing: -0.04em;
  line-height: 0.98; margin: 0 0 0.75rem 0;
}
.login-hero .lh-sub {
  color: var(--text-2); font-size: 0.95rem;
  line-height: 1.5;
  max-width: 40ch;
}
.login-hero .lh-meta {
  margin-top: 2rem;
  display: flex; flex-wrap: wrap; gap: 1.5rem;
  padding-top: 1rem;
  border-top: var(--line-thin) solid var(--text-1);
  font-family: var(--font-mono);
  font-size: 0.68rem; color: var(--text-3);
  letter-spacing: 0.14em;
  text-transform: uppercase;
}
.login-hero .lh-meta b {
  color: var(--text-1);
  font-weight: 700;
  margin-right: 4px;
}
.login-frame {
  padding: 2rem;
  border: var(--line) solid var(--text-1);
  background: var(--card);
}
.login-frame-head {
  font-family: var(--font-mono);
  font-size: var(--fs-tag);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.14em;
  color: var(--text-1);
  margin-bottom: 1.25rem;
  padding-bottom: 0.75rem;
  border-bottom: var(--line-thin) solid var(--text-1);
}

/* Tab-like horizontal radio (replaces st.tabs which loses active state on rerun).
   Scoped via the radio's label="main_tab" — only this one block gets the
   tab look, other radios in the app remain normal. */
div[role="radiogroup"]:has(> label[data-baseweb="radio"]:first-child input[type="radio"][value="✍️ 快速生成"]),
div[role="radiogroup"]:has(> label[data-baseweb="radio"]:first-child input[type="radio"][value="📋 批次队列"]) {
  display: flex;
  gap: 0;
  border-bottom: var(--line-thin) solid var(--text-2);
  margin-bottom: 1rem;
}
div[role="radiogroup"]:has(input[type="radio"][value^="✍️"]) > label,
div[role="radiogroup"]:has(input[type="radio"][value^="📋"]) > label {
  flex: 0 0 auto;
  padding: 0.55rem 1.25rem;
  margin: 0 -1px 0 0;
  cursor: pointer;
  border: var(--line-thin) solid var(--text-2);
  border-bottom: none;
  background: var(--bg);
  font-weight: 500;
  font-size: 0.92rem;
  letter-spacing: 0.02em;
  transition: background 0.12s;
}
div[role="radiogroup"]:has(input[type="radio"][value^="✍️"]) > label:hover,
div[role="radiogroup"]:has(input[type="radio"][value^="📋"]) > label:hover {
  background: var(--bg-2);
}
div[role="radiogroup"]:has(input[type="radio"][value^="✍️"]) > label:has(input:checked),
div[role="radiogroup"]:has(input[type="radio"][value^="📋"]) > label:has(input:checked) {
  background: var(--text-1);
  color: var(--bg);
  border-color: var(--text-1);
}
/* Hide the actual radio dot, we use background-color for active indication */
div[role="radiogroup"]:has(input[type="radio"][value^="✍️"]) > label > div:first-child,
div[role="radiogroup"]:has(input[type="radio"][value^="📋"]) > label > div:first-child {
  display: none;
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
    # Brand header — studio wordmark
    st.markdown(
        "<div class='brand-header'>"
        "<div class='brand-logo'>✦</div>"
        "<div><div class='brand-name'>AutoWriter</div>"
        f"<div class='brand-sub'>XHS · v{config.APP_VERSION}</div></div>"
        "</div>",
        unsafe_allow_html=True,
    )

    # User block
    safe_email = _html.escape(current_user['email'])
    avatar_char = _html.escape(current_user['email'][0].upper())
    st.markdown(
        f"<div class='user-block'>"
        f"<div class='user-avatar'>{avatar_char}</div>"
        f"<div><div class='user-email'>{safe_email}</div><div class='user-ver'>SIGNED IN</div></div>"
        f"</div>",
        unsafe_allow_html=True,
    )
    if st.button("退出登录", use_container_width=True):
        auth.sign_out()
        st.rerun()

def _render_error_panel(err: Exception) -> None:
    """Surface a real error message + remediation hints instead of letting
    Streamlit show its redacted red box.  Used by the auth-step and router-
    step error boundaries below."""
    import traceback as _traceback
    import postgrest.exceptions as _pg_exc

    st.error("⚠️ 页面渲染时发生错误。原始信息：")
    err_repr = repr(err)
    err_msg = str(err)
    is_pg = isinstance(err, _pg_exc.APIError)
    pg_detail: dict = {}
    if is_pg:
        try:
            arg = err.args[0] if err.args else {}
            if isinstance(arg, dict):
                pg_detail = arg
        except Exception:
            pass
        st.code(
            logger_utils.mask_secrets(
                "PostgREST APIError\n"
                f"message: {pg_detail.get('message', err_msg)}\n"
                f"code:    {pg_detail.get('code', '')}\n"
                f"hint:    {pg_detail.get('hint', '')}\n"
                f"details: {pg_detail.get('details', '')}"
            ),
            language="text",
        )
    else:
        st.code(logger_utils.mask_secrets(err_repr), language="text")

    with st.expander("🔍 完整 traceback（截图发给开发者最有用）", expanded=False):
        st.code(logger_utils.mask_secrets(_traceback.format_exc()), language="text")

    st.markdown("**常见排查方向**：")
    if is_pg:
        msg_lower = (str(pg_detail.get("message", "")) + " " + err_msg).lower()
        code_str = str(pg_detail.get("code", "")).strip()
        if "column" in msg_lower and ("does not exist" in msg_lower or "not found" in msg_lower):
            st.markdown(
                "- 看起来数据库少一列。最近版本（2.7.3）需要 `ALTER TABLE items "
                "ADD COLUMN IF NOT EXISTS feedback_draft TEXT;`，去 Supabase SQL Editor 跑一下。"
            )
        if "jwt" in msg_lower or "expired" in msg_lower or "401" in code_str or "401" in msg_lower:
            st.markdown("- token 过期或无效。点击下方「🔄 重置会话」按钮重登一次即可。")
        if "row level security" in msg_lower or "permission" in msg_lower or "rls" in msg_lower or code_str.startswith("42"):
            st.markdown(
                "- RLS 策略阻止了访问。确认登录的账号和数据所属账号一致；"
                "新建项目失败时常见此错。"
            )
        if "could not connect" in msg_lower or "timeout" in msg_lower:
            st.markdown(
                "- 数据库连不上。检查 Supabase 项目是否被自动暂停（免费版闲置一段时间会暂停），"
                "去 Supabase Dashboard 唤醒一下。"
            )
    st.markdown(
        "- 在 Streamlit Cloud 控制台右下角点 **Manage app** → **Logs** "
        "可看到完整 stderr，比这里的截图更详细。"
    )

    if st.button("🔄 重置会话并重登"):
        auth.sign_out()
        st.rerun()
    st.stop()


# Project switcher (also rendered in sidebar via projects module).  Wrapped
# because list_projects can hit RLS / schema / token issues right at app
# entry, which would otherwise be redacted by Streamlit's red box.
try:
    selected_project = proj_module.render_project_switcher(db_client, user_id)
except Exception as _switcher_err:
    _render_error_panel(_switcher_err)

_NAV_ITEMS = {
    "01 · 生成":  "生成工作台",
    "02 · 审核":  "审核与迭代",
    "03 · 导出":  "导出中心",
    "04 · 记忆":  "记忆管理",
    "05 · 设置":  "项目设置",
    "06 · 历史":  "批次历史",
}

# 程序化跳转入口：其它页面（如 page_history 的「查看此批次」按钮、
# Quick gen 完成后的"前往审核"提示）写 ``_force_page`` 让 sidebar radio
# 在下次 rerun 时把选项切到指定页。要在 radio 渲染前生效 —— Streamlit 允许
# 在 widget 渲染前修改 session_state[key] 来设置该 widget 的当前值。
_FORCED_NAV = st.session_state.pop("_force_page", None)
if _FORCED_NAV:
    _was_already_on = (
        st.session_state.get("xhs_nav_radio") in {
            k for k, v in _NAV_ITEMS.items() if v == _FORCED_NAV
        }
    )
    for _nav_key, _page_name in _NAV_ITEMS.items():
        if _page_name == _FORCED_NAV:
            st.session_state["xhs_nav_radio"] = _nav_key
            break
    # 当目标页就是当前页时 radio 不会有视觉变化，用户以为按钮失效。
    # 用 toast 给出一行可见反馈（比 st.success 自动消失更轻量）。
    if _was_already_on:
        try:
            st.toast(f"已跳转到「{_FORCED_NAV}」（已在该页）", icon="✅")
        except Exception:
            pass

with st.sidebar:
    st.markdown("<div class='nav-heading'>▸ CHAPTERS</div>", unsafe_allow_html=True)
    _nav_choice = st.radio(
        "导航",
        list(_NAV_ITEMS.keys()),
        label_visibility="collapsed",
        key="xhs_nav_radio",
    )
    page = _NAV_ITEMS[_nav_choice]


# ── Global queue banner ────────────────────────────────────────────────────
_queue_banner()

# R-027: schema 漂移一次性告警。update_project 撞"列不存在"剥列后，由
# db._record_schema_drift 把缺失列塞进 session_state；这里 pop 出来显式提示，
# 避免"UI 改了值但 DB 没生效"无感。
_drift_cols = st.session_state.pop("_schema_drift_cols", None)
if _drift_cols:
    st.warning(
        "⚠️ 以下字段未能写入数据库（schema 滞后，已自动跳过这些列）："
        f"`{', '.join(_drift_cols)}`。项目的其它字段已正常保存；"
        "请运维确认 AutoWriter 的 ALTER TABLE 迁移是否已在 Supabase 跑过。"
    )

# ── Route to pages ─────────────────────────────────────────────────────────

if selected_project is None and page not in ("项目设置",):
    st.info("👈 请先在左侧创建或选择一个项目，然后开始使用。")
    st.stop()


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: 生成工作台
# ═══════════════════════════════════════════════════════════════════════════

def _hero_header(chapter: str, title: str, subtitle: str = "") -> None:
    """Render a studio-grade page hero.

    chapter  — "01 / GENERATE" (number is split on first '/' to style it)
    title    — big hero sentence
    subtitle — small gray description (optional)
    """
    if " / " in chapter:
        num, name = chapter.split(" / ", 1)
    else:
        num, name = chapter, ""
    tag_html = (
        f"<span class='hero-tag-num'>{_html.escape(num)}</span>"
        + (f"<span class='hero-tag-name'>/ {_html.escape(name)}</span>" if name else "")
    )
    sub_html = f"<div class='hero-sub'>{_html.escape(subtitle)}</div>" if subtitle else ""
    st.markdown(
        f"<section class='hero'>"
        f"<div class='hero-inner'>"
        f"<div class='hero-tag'>{tag_html}</div>"
        f"<h1 class='hero-title'>{_html.escape(title)}</h1>"
        f"{sub_html}"
        f"</div>"
        f"<div class='hero-deco'>✦</div>"
        f"</section>",
        unsafe_allow_html=True,
    )


def _section_divider() -> None:
    """Hard-edge section divider (heavy black horizontal line)."""
    st.markdown("<hr class='section-divider' />", unsafe_allow_html=True)



def _rerun_app() -> None:
    """Trigger an app-level rerun even when called from inside an ``@st.fragment``.

    Streamlit 1.37+ 支持 ``st.rerun(scope="app")``，可以从 fragment 内部触发
    完整页面重跑（让 sidebar / banner / tab 状态都更新）。旧版本退化到默认
    rerun ——在 fragment 内是 fragment-scope rerun，但 banner 是独立 fragment
    每 2s 自动刷新，体验差异可接受。
    """
    try:
        st.rerun(scope="app")
    except TypeError:
        st.rerun()


def _render_queue_tab_body() -> None:
    """Render the batch queue builder and executor UI.

    抽成独立函数后用 ``@st.fragment`` 包装（见底部 ``_render_queue_tab`` 赋值）。
    fragment 让"加/删/改 plan"等高频操作只触发本 fragment 局部 rerun，不再
    导致：
      - 外层 ``st.tabs`` 重置 active tab（用户加批次时"跳回快速生成"）
      - 整页 100+ widget 全部重渲染（多 plan 时调参数明显卡）
      - 上次未交互的 expander 被强制按 ``expanded=(i==len-1)`` 折叠
    """
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

    # 给每个 plan 一个稳定的 _id（uuid），widget key 用 _id 而不是 list index。
    # 否则用户删除中间某条计划后，后面所有 plan 的 index 漂移，session_state
    # 里旧 index 的 widget 值会被新位置的 plan 误用（"明明改了又跳回原值"）。
    for _p in plans:
        if not _p.get("_id"):
            _p["_id"] = uuid.uuid4().hex[:12]

    # 取出本轮"刚加的 plan id"——只有那一个 expander 强制 expanded=True 显示
    # 给用户。其它 plan 不传 expanded 参数，让 Streamlit 客户端保留用户上次
    # 手动点开/折叠的状态（之前用 ``expanded=(i==len-1)`` 会在每次 rerun 把
    # 用户已展开的中间项强制折叠）。
    _just_added_id = st.session_state.pop("_queue_just_added_plan_id", None)

    # ── Plan list ──────────────────────────────────────────────────────
    plans_to_delete: list[int] = []
    for i, plan in enumerate(plans):
        pid_key = plan["_id"]
        expander_label = (
            f"计划 {i+1} — {plan.get('project_name', '?')} · "
            f"{plan.get('tactic', '通用') or '通用'} · "
            f"{'/'.join(e.upper() for e in plan.get('engines', ['claude']))} · "
            f"{plan.get('count', 1)} 篇"
        )
        # expander 加 ``key`` 参数（Streamlit 1.43+）让展开状态按 key 持久化。
        # 这彻底解决"选 Claude 模型后 expander 折叠"问题——之前 selectbox
        # change → fragment rerun → expander 重新挂载 → client 展开状态丢失。
        # 老 Streamlit 不支持 key 时 fallback 到没 key 的写法。
        _exp_key = f"plan_exp_{pid_key}"
        try:
            if pid_key == _just_added_id:
                _expander_ctx = st.expander(expander_label, expanded=True, key=_exp_key)
            else:
                _expander_ctx = st.expander(expander_label, key=_exp_key)
        except TypeError:
            # Streamlit < 1.43: st.expander 没 key 参数
            if pid_key == _just_added_id:
                _expander_ctx = st.expander(expander_label, expanded=True)
            else:
                _expander_ctx = st.expander(expander_label)
        with _expander_ctx:
            pc1, pc2 = st.columns(2)
            with pc1:
                sel_pid = st.selectbox(
                    "项目", proj_ids,
                    format_func=lambda pid: proj_id_to_name.get(pid, pid),
                    index=proj_ids.index(plan["project_id"]) if plan["project_id"] in proj_ids else 0,
                    key=f"qp_proj_{pid_key}",
                )
                plan["project_id"]   = sel_pid
                plan["project_name"] = proj_id_to_name.get(sel_pid, "")

                sel_proj = proj_id_to_obj.get(sel_pid, {})
                plan_tactic_names = proj_module.get_tactic_names(sel_proj)
                tactic_opts = ["（无）"] + plan_tactic_names
                t_idx = tactic_opts.index(plan.get("tactic", "（无）")) if plan.get("tactic", "（无）") in tactic_opts else 0
                sel_tactic = st.selectbox("战术方向", tactic_opts, index=t_idx, key=f"qp_tactic_{pid_key}")
                plan["tactic"] = "" if sel_tactic == "（无）" else sel_tactic

            with pc2:
                plan["count"] = st.number_input(
                    "篇数", min_value=1, max_value=config.MAX_GENERATION_COUNT,
                    value=plan.get("count", 3), key=f"qp_count_{pid_key}",
                )
                q_eng_mode = st.radio(
                    "模式", ["单引擎", "多引擎比稿", "🎭 三省法"],
                    horizontal=True, key=f"qp_eng_mode_{pid_key}",
                    index=2 if plan.get("use_multi_role") else (1 if len(plan.get("engines", [])) > 1 else 0),
                )
                plan["use_multi_role"] = (q_eng_mode == "🎭 三省法")
                if q_eng_mode == "单引擎":
                    q_eng = st.selectbox(
                        "引擎", gen_module.AVAILABLE_ENGINES,
                        format_func=lambda e: "Claude" if e == "claude" else "Gemini",
                        key=f"qp_eng_{pid_key}",
                    )
                    plan["engines"] = [q_eng]
                elif q_eng_mode == "多引擎比稿":
                    plan["engines"] = gen_module.AVAILABLE_ENGINES[:2] or ["claude"]
                else:
                    # 三省法：默认 Claude，可选加 Gemini 增加多样性
                    q_mr_eng = st.multiselect(
                        "参与角色起草的引擎",
                        gen_module.AVAILABLE_ENGINES,
                        default=plan.get("engines", ["claude"]),
                        format_func=lambda e: "Claude" if e == "claude" else "Gemini",
                        key=f"qp_mr_eng_{pid_key}",
                    )
                    plan["engines"] = q_mr_eng or ["claude"]
                    plan["n_roles"] = st.slider(
                        "抽取角色数", min_value=2, max_value=6,
                        value=plan.get("n_roles", 3), key=f"qp_nroles_{pid_key}",
                        help="每次从角色池中随机抽取，数量越多并行路数越多",
                    )

            q_em: dict[str, str] = {}
            qm1, qm2 = st.columns(2)
            _saved_em = plan.get("engine_models") or {}
            if "claude" in plan["engines"]:
                _saved_cm = _saved_em.get("claude", config.CLAUDE_MODEL)
                _cm_keys = list(config.CLAUDE_MODELS.keys())
                with qm1:
                    q_em["claude"] = st.selectbox(
                        "Claude 模型", _cm_keys,
                        format_func=lambda m: config.CLAUDE_MODELS.get(m, m),
                        index=_cm_keys.index(_saved_cm) if _saved_cm in _cm_keys else 0,
                        key=f"qp_cm_{pid_key}",
                    )
            if "gemini" in plan["engines"]:
                _saved_gm = _saved_em.get("gemini", config.GEMINI_MODEL)
                _gm_keys = list(config.GEMINI_MODELS.keys())
                with qm2:
                    q_em["gemini"] = st.selectbox(
                        "Gemini 模型", _gm_keys,
                        format_func=lambda m: config.GEMINI_MODELS.get(m, m),
                        index=_gm_keys.index(_saved_gm) if _saved_gm in _gm_keys else 0,
                        key=f"qp_gm_{pid_key}",
                    )
            plan["engine_models"] = q_em

            # Claude thinking is now selected via model name (-thinking suffix);
            # only Gemini keeps a runtime toggle.
            if "gemini" in plan["engines"]:
                plan["gemini_use_thinking"] = st.checkbox(
                    "Gemini 思考模式",
                    value=plan.get("gemini_use_thinking", False),
                    key=f"qp_gthink_{pid_key}",
                    help="thinking_budget=-1 动态分配（Gemini 3.x 默认开启思考）",
                )
                # R-033: google-genai 0.x 没有 thinking_budget 字段 → thinking
                # 控制(开 budget=-1 / 关 budget=0)都会被 runtime 降级忽略(不再
                # 像之前那样整批崩)。两个方向都要明示, 不能只在"开"时提示:
                # 关掉开关时, 默认开启思考的模型(2.5 Pro/Flash · 3.x)照样会跑
                # 思考并产生 thinking token 费用——用户本想省钱却被静默忽略
                # (PR #44 review 命中)。提示与开关状态无关, 只要 SDK 不支持就显示。
                if not gen_module.gemini_thinking_supported():
                    st.caption(
                        "⚠️ 当前部署的 google-genai SDK(<1.x)不支持 thinking 控制："
                        "「思考模式」开关无论开/关都会被忽略、模型按其默认行为运行。"
                        "对默认开启思考的模型(2.5 Pro / 2.5 Flash / 3.x)，关掉开关也"
                        "无法省下 thinking token 费用。升级 google-genai>=1.x 后此开关"
                        "才生效(见 requirements.txt 注释)。"
                    )

            plan["extra_instructions"] = st.text_input(
                "补充说明", value=plan.get("extra_instructions", ""),
                placeholder="可选", key=f"qp_extra_{pid_key}",
            )

            # Day 5：本计划策略（覆盖项目默认）
            strategy_options = ["项目默认", "稳定优先 (stable)", "吞吐优先 (throughput)"]
            cur_s = plan.get("strategy")
            if cur_s == "stable":
                cur_idx = 1
            elif cur_s == "throughput":
                cur_idx = 2
            else:
                cur_idx = 0
            strategy_choice = st.selectbox(
                "本计划策略",
                strategy_options,
                index=cur_idx,
                key=f"qp_strategy_{pid_key}",
                help="覆盖项目默认队列策略。「稳定」更严格，重复率低但慢；「吞吐」更快但重复率可能上升。",
            )
            if strategy_choice == "项目默认":
                plan["strategy"] = None
            elif strategy_choice.startswith("稳定"):
                plan["strategy"] = "stable"
            elif strategy_choice.startswith("吞吐"):
                plan["strategy"] = "throughput"

            if st.button("🗑 删除此计划", key=f"qp_del_{pid_key}"):
                plans_to_delete.append(i)

    for idx in sorted(plans_to_delete, reverse=True):
        plans.pop(idx)
    if plans_to_delete:
        # fragment 内 rerun 就够：只重渲染本 fragment，外层 tabs / sidebar 不动
        st.rerun()

    # ── Controls ───────────────────────────────────────────────────────
    btn_col1, btn_col2 = st.columns(2)
    with btn_col1:
        if st.button("➕ 添加计划", use_container_width=True, disabled=is_running):
            default_pid = proj_ids[0]
            new_id = uuid.uuid4().hex[:12]
            plans.append({
                "_id":                 new_id,
                "project_id":          default_pid,
                "project_name":        proj_id_to_name.get(default_pid, ""),
                "tactic":              "",
                "engines":             ["claude"],
                "engine_models":       {"claude": config.CLAUDE_MODEL},
                "count":               3,
                "use_thinking":        False,
                "gemini_use_thinking": False,
                "use_multi_role":      False,
                "n_roles":             3,
                "extra_instructions":  "",
            })
            # 让本次新增的 plan 在下次渲染时强制 expanded=True（一次性）；
            # 其它已存在 plan 保留客户端展开/折叠状态。
            st.session_state["_queue_just_added_plan_id"] = new_id
            st.rerun()

    with btn_col2:
        # Day 4: 用 phase 状态机替代直接读 running 标志位，避免高频点击竞态。
        # 状态机：idle → starting → running → stopping → done → idle（点击"清除"）
        cur_phase = (qs.get("phase") or ("running" if is_running else "idle"))
        is_transitional = cur_phase in ("starting", "stopping")
        if cur_phase in ("idle", "done") and not is_running:
            if st.button(
                "🚀 启动队列", type="primary", use_container_width=True,
                disabled=(not plans or is_transitional),
            ):
                # 关键：在 thread.start() 之前就把 running=True / phase=starting
                # 写到 session_state，下一次 rerun 不会再走进这个分支创建第二个
                # worker。
                stop_evt = threading.Event()
                queue_lock = threading.Lock()
                status: dict = {
                    "running": True, "done": False, "total": 0,
                    "current": 0, "message": "准备中…",
                    "completed": [], "errors": [],
                    "warnings": [], "embedding_missing": [],
                    "phase": "starting",
                    "_lock": queue_lock,
                }
                st.session_state["queue_state"]      = status
                st.session_state["queue_stop_event"] = stop_evt
                t = threading.Thread(
                    target=_queue_worker,
                    args=(copy.deepcopy(plans), user_id, db_client, status, stop_evt),
                    daemon=True,
                )
                t.start()
                # 启动队列要让 banner（独立 fragment）立刻出现，外层 page 状态
                # 也要刷新，所以触发 app-level rerun 而不是 fragment-only。
                _rerun_app()
        else:
            if st.button(
                "⏹ 停止队列", use_container_width=True,
                disabled=is_transitional,
            ):
                with (qs.get("_lock") or _NULL_LOCK):
                    qs["phase"] = "stopping"
                evt = st.session_state.get("queue_stop_event")
                if evt:
                    evt.set()
                _rerun_app()

    # Live status within tab
    if is_running:
        # The fragment-based _queue_banner already auto-refreshes the
        # progress info every 2 s without re-running the whole page.
        # Keep a static snapshot here for users on the queue tab, but no
        # longer time.sleep + st.rerun — that was the main cause of the
        # 1.5 s "click and wait" lag during batch runs (every interaction
        # was racing the next forced rerun).
        _render_running_snapshot()

    # Completed results summary
    if qs.get("done") and qs.get("completed"):
        st.markdown("<div class='section-label' style='margin-top:16px'>已完成的批次</div>", unsafe_allow_html=True)
        for item in qs["completed"]:
            saved = item.get("saved", 0)
            head = f"{item['project_name']} · 批次 {item['batch_id'][:8]}…"
            if saved > 0:
                st.success(f"✅ {head} · 已保存 {saved} 个版本")
            else:
                # saved==0 = 整批一条都没生成出来（模型 API 报错 / 超额）。绝不能再
                # 显示绿色 ✅，否则用户以为成功了，去审核页却空空如也、还摸不着头脑。
                st.warning(
                    f"⚠️ {head} · 生成失败，0 个版本"
                    "（模型 API 报错或超额，未保存任何内容；详见上方完成提示）"
                )


# 包成 fragment：高频交互（加/删/改 plan）只 rerun 本 fragment 而不是整页，
# 解决三个 UX 痛点：
#   1. 加批次时 ``st.tabs`` 不再被重置（之前 page rerun 会跳回"快速生成"）
#   2. 改字段时只重渲染队列 tab 内部，多 plan 时不卡
#   3. expander 的客户端展开状态不被强制重置
# Streamlit < 1.33 没有 fragment 时退化到普通函数，行为与旧版完全一致。
if _FRAGMENT is not None:
    _render_queue_tab = _FRAGMENT(_render_queue_tab_body)
else:
    _render_queue_tab = _render_queue_tab_body


def page_generate(project: dict) -> None:
    pname = _html.escape(project.get("name", ""))
    _hero_header("01 / GENERATE", "构思你的下一个爆款。", f"项目 · {pname}")

    project_name = project.get("name", "")
    base_prompt  = project.get("system_prompt", "")
    tactic_names = proj_module.get_tactic_names(project)

    # ── Sidebar: quick-generate controls ──────────────────────────────
    with st.sidebar:
        st.markdown(
            "<div class='nav-heading'>▸ QUICK GEN</div>",
            unsafe_allow_html=True,
        )

        if tactic_names:
            tactic = st.selectbox("战术方向", ["（不使用战术方向）"] + tactic_names)
            if tactic == "（不使用战术方向）":
                tactic = ""
        else:
            st.caption("未配置战术方向，将不应用战术方向。")
            tactic = ""
        count = st.slider("生成数量", 1, config.MAX_GENERATION_COUNT, config.DEFAULT_GENERATION_COUNT)

        use_multi_role = st.toggle(
            "🎭 多角色起草（三省法）",
            help="从角色池中随机抽取 N 个视角并行起草，AI 自动评选最优版并附评审意见。"
                 "质量下限更高，延迟与单次生成基本相同。与多引擎比稿互斥。",
        )

        engine_models: dict[str, str] = {}

        if use_multi_role:
            _proj_custom_roles = proj_module._parse_json_field(project.get("custom_roles"), [])
            _role_pool_size = len(_proj_custom_roles) if _proj_custom_roles else len(gen_module.CREATIVE_ROLES_POOL)
            n_roles = st.slider(
                "抽取角色数", min_value=2, max_value=min(_role_pool_size, 6), value=3,
                help=f"每次从角色池（{_role_pool_size} 个）中随机抽取，引入不可预测性。角色池可在「项目设置 → 角色池」中配置。",
            )
            # Multi-role mode: choose which engines participate
            engines = st.multiselect(
                "参与角色起草的引擎",
                gen_module.AVAILABLE_ENGINES,
                default=["claude"],
                format_func=lambda e: "Claude" if e == "claude" else "Gemini",
                help=f"选两个引擎：{n_roles}角色×2引擎={n_roles*2}路并行，差异性最大",
            ) or ["claude"]
            if "claude" in engines:
                engine_models["claude"] = st.selectbox(
                    "Claude 模型",
                    list(config.CLAUDE_MODELS.keys()),
                    index=list(config.CLAUDE_MODELS.keys()).index(config.CLAUDE_MODEL)
                          if config.CLAUDE_MODEL in config.CLAUDE_MODELS else 0,
                    format_func=lambda m: config.CLAUDE_MODELS.get(m, m),
                )
            if "gemini" in engines:
                engine_models["gemini"] = st.selectbox(
                    "Gemini 模型",
                    list(config.GEMINI_MODELS.keys()),
                    index=list(config.GEMINI_MODELS.keys()).index(config.GEMINI_MODEL)
                          if config.GEMINI_MODEL in config.GEMINI_MODELS else 0,
                    format_func=lambda m: config.GEMINI_MODELS.get(m, m),
                )
            use_thinking = False
            gemini_use_thinking = False
            if "gemini" in engines:
                gemini_use_thinking = st.checkbox(
                    "Gemini 思考模式",
                    help="thinking_budget=-1 动态分配；对 2.5 Pro 效果明显。",
                )
                # R-039: 与队列 tab 的 R-033 提示对齐 —— quick gen 此前缺失,
                # SDK<1.x 时开关被静默忽略(关也省不下 thinking 费用)。
                if not gen_module.gemini_thinking_supported():
                    st.caption(
                        "⚠️ 当前 google-genai SDK(<1.x)不支持 thinking 控制：开关"
                        "开/关都会被忽略、模型按默认行为运行(升级依赖后生效)。"
                    )
        else:
            # Standard mode: single or multi-engine
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
            if "gemini" in engines:
                gemini_use_thinking = st.checkbox(
                    "Gemini：思考模式",
                    help="thinking_budget=-1 动态分配；对 2.5 Pro 效果明显。",
                )
                # R-039: 同上 —— SDK 能力提示补齐
                if not gen_module.gemini_thinking_supported():
                    st.caption(
                        "⚠️ 当前 google-genai SDK(<1.x)不支持 thinking 控制：开关"
                        "开/关都会被忽略、模型按默认行为运行(升级依赖后生效)。"
                    )

        with st.expander("⚙️ 高级参数"):
            # R-039: 必须带 per-project key。无 key 时 Streamlit 按(类型+label+
            # 参数)算 widget 身份, 切换项目后这些 proto 不变 → A 项目填的卖点
            # 原样带进 B 项目的生成(本文件其余 widget 均已按 pid 隔离, 这四个
            # 是漏网)。
            _qpid = project["id"]
            target_audience   = st.text_input(
                "目标人群", placeholder="例：25-35岁职场女性",
                key=f"qg_adv_aud_{_qpid}")
            key_messages      = st.text_input(
                "核心卖点/关键词", placeholder="例：低度数、清爽、派对感",
                key=f"qg_adv_msg_{_qpid}")
            tone              = st.text_input(
                "语气偏好", placeholder="例：活泼口语化、朋友间分享",
                key=f"qg_adv_tone_{_qpid}")
            extra_instructions = st.text_area(
                "补充说明", height=80, placeholder="其他要求...",
                key=f"qg_adv_extra_{_qpid}")

    # ── Main area: tab switcher ────────────────────────────────────────
    # 之前用 ``st.tabs``，但它没有 ``key`` 参数 —— active tab 是 client-side
    # state，rerun 后（即使 fragment scope）只要 widget tree 长度变化到某个
    # 阈值（实测加第 3、4 个 plan 时必现）Streamlit 内部就会重置回第一个
    # tab。换用 radio + session_state key 持久化 active tab，任何 rerun 都
    # 不会重置；CSS 已经把这个 radio 渲染成 tab 外观。
    _MAIN_TABS = ["✍️ 快速生成", "📋 批次队列"]
    _active_main_tab = st.radio(
        "main_tab",
        _MAIN_TABS,
        horizontal=True,
        key="_main_gen_tab",
        label_visibility="collapsed",
    )

    # ── TAB 1: Quick generate ──────────────────────────────────────────
    if _active_main_tab == _MAIN_TABS[0]:
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
        pos_examples = db.list_example_items(db_client, project["id"], "positive", limit=5)
        neg_examples = db.list_example_items(db_client, project["id"], "negative", limit=3)
        calibration_notes = project.get("calibration_notes") or ""

        context_parts = []
        mem_count = len(global_mems) + len(project_mems)
        if mem_count > 0:
            context_parts.append(f"🧠 {mem_count} 条记忆")
        if calibration_notes.strip():
            context_parts.append("📝 调校笔记")
        if pos_examples:
            context_parts.append(f"⭐ {len(pos_examples)} 个正案例")
        if neg_examples:
            context_parts.append(f"👎 {len(neg_examples)} 个反案例")
        if context_parts:
            st.info("已载入上下文：" + " · ".join(context_parts))

        # 按项目隔离 quick_gen_state — 跨项目切换不会串数据
        qg_key     = _qg_key(project["id"])
        rb_key     = _rb_key(project["id"])
        qgs        = st.session_state.get(qg_key)
        qg_running = bool(qgs and qgs.get("running"))
        qg_done    = bool(qgs and qgs.get("done"))
        qg_phase   = (qgs or {}).get("phase", "idle")

        if qg_running:
            # Fragment-driven polling replaces the old sleep+rerun loop;
            # the quick-generate path now refreshes the progress bar
            # without forcing the whole script to re-execute.
            _render_quick_gen_snapshot()
        elif qg_done:
            errors_list = qgs.get("errors", [])
            if errors_list:
                # R-040: 渲染前脱敏(worker 写入的是原始 str(exc))
                st.error("部分内容生成失败：\n" + "\n".join(
                    f"• {logger_utils.mask_secrets(e)}"
                    for e in list(dict.fromkeys(errors_list))
                ))
            saved = qgs.get("saved_count", 0)
            n_res = qgs.get("n_results", 0)
            if saved > 0:
                st.success(f"✅ 生成完成！共 {n_res} 篇，{saved} 个版本。")
            elif not errors_list:
                st.warning("生成完成，但没有内容被保存，请检查配置。")
            # Phase 2.3: session 占满窗口被自动封窗(下批已自动开新窗)
            qg_sealed = sorted(set(qgs.get("sealed_engines") or []))
            if qg_sealed:
                st.caption(
                    f"🪟 {'、'.join(e.upper() for e in qg_sealed)} 的会话窗口已满，"
                    "已自动封窗并开新窗（新窗自动继承最近 50 条已通过历史，避重不断）。"
                )
            # 快速生成的指标卡片(token/cache/cost/耗时/去重)——之前只有队列
            # banner 渲染, quick gen 漏了, 导致"快速生成完没数据卡片"。
            # qgs["metrics_list"] 由 _quick_gen_worker 的 metrics.close(status)
            # 挂上, 跟队列共用 _render_queue_dashboard。
            try:
                _render_queue_dashboard(qgs, title="本次生成指标")
            except Exception as exc:
                st.caption(f"⚠ 指标面板渲染失败：{exc}")
            # Day 2: 降级 / 缺向量等非阻塞告警
            qg_warnings = qgs.get("warnings") or []
            qg_missing = qgs.get("embedding_missing") or []
            if qg_warnings or qg_missing:
                with st.expander(
                    f"⚠ 运行警告 ({len(qg_warnings) + (1 if qg_missing else 0)})",
                    expanded=False,
                ):
                    for w in qg_warnings[:20]:
                        st.markdown(f"- {w}")
                    if qg_missing:
                        st.markdown(
                            f"- **{len(qg_missing)} 条版本缺少向量**："
                            "embedding 写入失败，本次去重已退化，后续跨批次去重可能受影响。"
                        )
            bid = qgs.get("batch_id")
            if bid:
                st.session_state[rb_key] = bid
                go_col, regen_col = st.columns(2)
                with go_col:
                    if st.button("👉 前往审核页", type="primary", use_container_width=True):
                        st.session_state["_force_page"] = "审核与迭代"
                        st.rerun()
                with regen_col:
                    if st.button("🔄 再次生成", use_container_width=True):
                        st.session_state.pop(qg_key, None)
                        st.rerun()
            else:
                if st.button("🔄 再次生成", use_container_width=True):
                    st.session_state.pop(qg_key, None)
                    st.rerun()
        else:
            if st.button(
                "🚀 开始生成", type="primary", use_container_width=True,
                disabled=(not base_prompt.strip()) or qg_phase == "starting",
            ):
                if not base_prompt.strip():
                    st.warning("⚠️ 当前项目尚未配置 System Prompt，请先在「项目设置」中填写。")
                    st.stop()

                qg_plan = {
                    "project_id":       project["id"],
                    "tactic":           tactic,
                    "engines":          engines,
                    "engine_models":    engine_models,
                    "count":            count,
                    "use_thinking":     use_thinking,
                    "gemini_use_thinking": gemini_use_thinking,
                    "use_multi_role":   use_multi_role,
                    "n_roles":          n_roles if use_multi_role else 3,
                    "target_audience":  target_audience,
                    "key_messages":     key_messages,
                    "tone":             tone,
                    "extra_instructions": extra_instructions,
                    "image_prompt":     image_prompt,
                    "images":           encoded_images or [],
                }
                qg_status: dict = {
                    "running": True, "done": False,
                    "message": "准备中…", "progress": 0.0,
                    "batch_id": None, "saved_count": 0,
                    "n_results": 0, "errors": [],
                    "warnings": [], "embedding_missing": [],
                    "phase": "starting",
                    "_lock": threading.Lock(),
                }
                st.session_state[qg_key] = qg_status
                threading.Thread(
                    target=_quick_gen_worker,
                    args=(qg_plan, user_id, db_client, qg_status),
                    daemon=True,
                ).start()
                st.rerun()

    # ── TAB 2: Batch Queue ─────────────────────────────────────────────
    elif _active_main_tab == _MAIN_TABS[1]:
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
    _hero_header("02 / REVIEW", "打磨每一篇成稿。", f"项目 · {pname}")

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
    # 按项目读 review_batch_id；老的全局 key 同时检查一遍以兼容迁移前的存量
    stored_bid = st.session_state.get(_rb_key(project["id"])) \
        or st.session_state.get("review_batch_id")
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

    # Stat-filter row — 4 clickable cards, the active one acts as
    # the current filter. Replaces the old stat-row + radio pair.
    current_filter = st.session_state.setdefault("review_filter", "all")

    # Marker used by CSS (:has() adjacent selector) to scope the
    # big-number button treatment to this one block only.
    st.markdown(
        "<div class='review-stat-filters' style='display:none'></div>",
        unsafe_allow_html=True,
    )
    _stat_defs = [
        ("all",            "总计",   len(items)),
        ("pending",        "待审核", pending),
        ("approved",       "已通过", approved),
        ("needs_revision", "待修改", revision),
    ]
    _cols = st.columns(4, gap="small")
    # Label on top (small, monospace, uppercase), number below (big)
    for _col, (_key, _label, _num) in zip(_cols, _stat_defs):
        with _col:
            if st.button(
                f"{_label}\n\n{_num}",
                key=f"rf_{_key}",
                use_container_width=True,
                type="primary" if current_filter == _key else "secondary",
            ):
                st.session_state["review_filter"] = _key
                st.rerun()

    if current_filter == "all":
        filtered_items = items
    else:
        filtered_items = [it for it in items if it["status"] == current_filter]

    st.divider()

    if not filtered_items:
        st.info("当前筛选条件下没有文案。")

    # ── Item cards ─────────────────────────────────────────────────────
    # 包 fragment：点单个卡片的"通过/打回/迭代/标记案例"等按钮只 rerun 那
    # 一个卡片，不重渲染整页（不重查 list_items、不重渲染其它卡片、不重
    # 跑 sidebar）。审核页 N=10+ 卡片时按钮响应明显变快。
    # Trade-off：顶部 stat row 的"待审 X 篇"数字在卡片状态变后不立刻更新，
    # 等下次自然 page rerun（切批次 / 切 tab / 刷页）才刷新。可接受 —— 用户
    # 最关心的是"我点了通过那张卡片变了没"，统计数字延迟无感。
    rendered = 0
    skipped_no_version = 0
    for item in filtered_items:
        versions = sorted(item.get("versions", []), key=lambda v: v.get("version_num", 0))
        if not versions:
            # 没有任何 version 的孤儿 item —— 通常是生成时模型 API 整批报错留下的
            # （例如 Gemini 中转通道挂掉/超额）。不渲染空卡片，但记下来：若整屏都是
            # 这种，下面给一句明确解释，免得用户看到"总计 N / 待审核 N"却一条都点不
            # 开，误以为是审核页坏了。
            skipped_no_version += 1
            continue
        _render_item_card_fragment(item, versions, selected_batch, project)
        rendered += 1

    if rendered == 0 and skipped_no_version > 0:
        st.warning(
            f"⚠️ 本批次有 {skipped_no_version} 条记录，但**都没有生成出内容**"
            "（生成时模型 API 报错或超额，没有保存任何版本）。\n\n"
            "这批是空的，不是审核页的问题。建议：①换其它模型（如 Claude）重新生成；"
            "②到「05 · 设置」确认该引擎的 API key / 中转额度是否正常。"
        )

    # ── 太子自动学习：批次审完后静默更新调教笔记 ──────────────────────────
    # 触发条件：本批次没有任何 pending 项（用户对每一条都做了决定 —— 不论是
    # 采纳 / 打回 / 修改），且这批次历史上还没自动反思过。"全部通过" 不是
    # 必要条件 —— 用户标 needs_revision 表示"这条不要这种调性"也是有效信号
    # （generate_calibration_notes 内部按 iteration feedback 和 manual edit 选信号，
    # 单纯 approved / needs_revision 状态本身不会污染笔记，无信号时会自动空跑）。
    #
    # 双闸门：(1) 持久化的 batches.auto_calibrated_at — 这批次历史上反思过没？
    #          浏览器刷新 / 重登都不会让它重跑（之前的 bug：只看 session_state，
    #          切回审核页又会重新跑一次 Claude，浪费 token + 让用户等 spinner）；
    #         (2) session 内的 _taizi_key — 防止同一次 page rerun 里重复触发。
    _taizi_key = f"taizi_{batch_id}"
    already_calibrated = bool(selected_batch.get("auto_calibrated_at"))
    batch_fully_handled = bool(items) and all(it["status"] != "pending" for it in items)
    if (batch_fully_handled
            and not already_calibrated
            and not st.session_state.get(_taizi_key)):
        st.session_state[_taizi_key] = True
        _auto_update_calibration_notes(project, batch_id, items)

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

    col_feishu, col_calib = st.columns(2)

    with col_feishu:
        if st.button("🔔 推送本批次到飞书", use_container_width=True):
            if not config.FEISHU_WEBHOOK_URL:
                st.warning("飞书 Webhook 未配置（FEISHU_WEBHOOK_URL）。")
            else:
                # Stamp project_id (items 表无此列) — _collect_approved_items
                # 会读取来挂 lineage。用 comprehension 不动 list_items 缓存。
                _pid = selected_batch.get("project_id")
                approved_items = _collect_approved_items(
                    [{**it, "_project_id": _pid} for it in items]
                )
                ok = exporter.push_to_feishu(
                    items=approved_items,
                    project_name=project.get("name", ""),
                    brand=project.get("brand", ""),
                    tactic=selected_batch.get("tactic", ""),
                )
                st.success("已推送到飞书。") if ok else st.error("飞书推送失败，请检查 Webhook 配置。")

    with col_calib:
        if st.button("🧠 更新调教笔记", use_container_width=True):
            _generate_calibration_notes_ui(project, batch_id, items)
        st.caption("对整个批次做整体反思；日常每次迭代已自动增量更新。")

    # 调教笔记预览 + 确认保存
    calib_key = f"pending_calibration_{batch_id}"
    if calib_key in st.session_state:
        st.markdown("---")
        st.markdown("<div class='section-label'>调教笔记预览（可编辑后保存）</div>", unsafe_allow_html=True)
        edited = st.text_area(
            "调教笔记",
            value=st.session_state[calib_key],
            height=240,
            key=f"calib_edit_{batch_id}",
            label_visibility="collapsed",
        )
        save_col, discard_col = st.columns(2)
        with save_col:
            if st.button("💾 保存到项目设置", key=f"save_calib_{batch_id}", use_container_width=True):
                try:
                    mem_module.save_calibration_notes(
                        db_client, project["id"], edited, source="user_manual"
                    )
                except Exception as exc:
                    st.error(f"保存失败：{logger_utils.mask_secrets(str(exc))}。预览内容保留，可重试。")
                else:
                    # .pop 而不是 del：fragment + 并发 rerun 下 calib_key 可能已被
                    # 别的 path 清掉，del 会 KeyError 让按钮看起来"点了报错"。
                    st.session_state.pop(calib_key, None)
                    st.success("调教笔记已保存，下次生成时生效。")
                    st.rerun()
        with discard_col:
            if st.button("✕ 放弃", key=f"discard_calib_{batch_id}", use_container_width=True):
                st.session_state.pop(calib_key, None)
                st.rerun()


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

    # Resolve thinking flag from batch params for the badge
    _bp = batch.get("params") or {}
    if isinstance(_bp, str):
        try:
            _bp = json.loads(_bp)
        except Exception:
            _bp = {}
    _thinking_on = (
        _bp.get("use_thinking", False) if engine_short == "claude"
        else _bp.get("gemini_use_thinking", False)
    )

    expander_label = (
        f"{status_icon} {title_str[:60]}{'…' if len(title_str) > 60 else ''}  "
        f"— {status_label} · v{ver_num}"
    )
    # Status marker: CSS uses :has() on this sibling to style the following
    # expander (approved → dark card; other statuses → default white card).
    st.markdown(
        f"<div class='card-status-marker card-{status}'></div>",
        unsafe_allow_html=True,
    )
    with st.expander(expander_label, expanded=(status in ("pending", "needs_revision"))):
        # Engine badge: show full model name + thinking indicator
        eng_cls = engine_short if engine_short in ("claude", "gemini") else ""
        thinking_tag = "&nbsp;🧠" if _thinking_on else ""
        st.markdown(
            f"<span class='engine-badge {eng_cls}'>{_html.escape(engine_raw.upper())}{thinking_tag}</span>",
            unsafe_allow_html=True,
        )

        # AI review notes (from multi-role drafting)
        ai_notes = item.get("ai_review_notes", "")
        if ai_notes:
            with st.expander("🔍 AI 评审意见", expanded=False):
                st.caption(ai_notes)

        # Multi-version comparison (if multiple engines were used)
        unique_engines = {v.get("ai_engine", "").split("/")[0] for v in versions}
        is_multi_engine = len(unique_engines) > 1
        best_vid = item.get("best_version_id")

        if is_multi_engine and not best_vid:
            # 对比模式：两引擎并排，各自独立改写，全局迭代区隐藏
            _render_version_comparison(versions, item_id, item, batch, project, _bp)
            show_global_iteration = False
        else:
            # 单篇模式（单引擎 或 已选最佳）
            _render_single_version(display_version)
            show_global_iteration = True

        # Status controls（始终显示）
        col_approve, col_revise = st.columns(2)
        with col_approve:
            if st.button("✅ 通过", key=f"approve_{item_id}", use_container_width=True):
                db.update_item_status(db_client, item_id, "approved")
                st.rerun()
        with col_revise:
            if st.button("✏️ 需修改", key=f"revise_{item_id}", use_container_width=True):
                db.update_item_status(db_client, item_id, "needs_revision")
                st.rerun()

        # Example label controls
        example_label = item.get("example_label")
        ex_col1, ex_col2, ex_col3 = st.columns(3)
        with ex_col1:
            is_pos = example_label == "positive"
            if st.button(
                "⭐ 正案例" if not is_pos else "⭐ 已标为正案例",
                key=f"pos_ex_{item_id}",
                use_container_width=True,
                type="primary" if is_pos else "secondary",
            ):
                db.set_item_example_label(db_client, item_id, None if is_pos else "positive")
                st.rerun()
        with ex_col2:
            is_neg = example_label == "negative"
            if st.button(
                "👎 反案例" if not is_neg else "👎 已标为反案例",
                key=f"neg_ex_{item_id}",
                use_container_width=True,
                type="primary" if is_neg else "secondary",
            ):
                db.set_item_example_label(db_client, item_id, None if is_neg else "negative")
                st.rerun()
        with ex_col3:
            if example_label:
                label_text = "⭐ 正案例" if example_label == "positive" else "👎 反案例"
                st.caption(f"已标记：{label_text}")

        # Feedback & iteration（对比模式未选最佳时隐藏，由各引擎列内部处理）
        if show_global_iteration and status in ("pending", "needs_revision"):
            st.markdown("<div class='section-label' style='margin-top:12px'>修改意见</div>", unsafe_allow_html=True)

            # Restore any previously-typed feedback that didn't make it through
            # iteration (token expiry, server error, etc.) so the user doesn't
            # have to re-type. Saved by db.save_feedback_draft on submit, cleared
            # by db.clear_feedback_draft on iteration success.
            saved_draft = (item.get("feedback_draft") or "").strip()
            feedback_key = f"feedback_{item_id}"
            if saved_draft and feedback_key not in st.session_state:
                st.session_state[feedback_key] = saved_draft
                st.caption("📌 已自动恢复你上次没保存成功的反馈")

            # Auto-save: on every rerun, if the textarea / tags state differs
            # from what's in DB, persist it.  Doesn't trigger any extra rerun
            # itself — just piggybacks on whatever rerun the user already
            # caused (clicking around, switching tabs, etc.) so an unexpected
            # crash mid-typing doesn't lose the draft.
            _auto_tags = st.session_state.get(f"tags_{item_id}", [])
            _auto_text = st.session_state.get(feedback_key, "")
            _auto_combined = (
                "、".join(_auto_tags)
                + ("；" + _auto_text if _auto_text else "")
            ).strip("、；")
            if _auto_combined and _auto_combined != saved_draft:
                try:
                    db.save_feedback_draft(db_client, item_id, _auto_combined)
                except Exception:
                    pass

            # Quick tags
            selected_tags = st.multiselect(
                "快捷反馈标签",
                QUICK_FEEDBACK_TAGS,
                key=f"tags_{item_id}",
                label_visibility="collapsed",
            )
            feedback_text = st.text_area(
                "详细反馈（可选）",
                key=feedback_key,
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

            # Claude thinking is selected via model name; no runtime toggle.
            iter_use_thinking = False

            if st.button("🔄 重新生成", key=f"regen_{item_id}", use_container_width=True):
                if not combined_feedback:
                    st.warning("请先填写修改意见或选择快捷标签。")
                else:
                    # Persist the typed feedback BEFORE the AI call so it
                    # survives any failure during iteration (token expiry,
                    # server error, network drop).  Cleared on success.
                    db.save_feedback_draft(db_client, item_id, combined_feedback)
                    # 已选最佳时只用该引擎的版本作历史，避免跨引擎混淆
                    disp_engine_short = display_version.get("ai_engine", "").split("/")[0]
                    iter_versions = (
                        [v for v in versions if v.get("ai_engine", "").split("/")[0] == disp_engine_short]
                        if best_vid else versions
                    )
                    _run_iteration(
                        item, iter_versions, batch, project,
                        combined_feedback, iter_engine, iter_model,
                        use_thinking_override=iter_use_thinking,
                    )

        # ── Manual edit ───────────────────────────────────────────────
        with st.expander("✏️ 手动精修", expanded=False):
            cur_title    = display_version.get("title", "") or ""
            cur_body     = display_version.get("body", "") or ""
            cur_keywords = _normalise_keywords(display_version.get("keywords"))
            base_kw_str  = "，".join(cur_keywords)

            # Widget keys include the version id so that switching the "best"
            # pick in multi-engine compare mode gives us a fresh widget (else
            # Streamlit keeps the session_state value from the first render
            # and ignores the new ``value=`` prop).
            vid = display_version.get("id", "latest")
            title_key = f"edit_title_{item_id}_{vid}"
            body_key  = f"edit_body_{item_id}_{vid}"
            kw_key    = f"edit_kw_{item_id}_{vid}"
            save_key  = f"save_edit_{item_id}_{vid}"

            # Load any saved manual-edit draft for this exact version.  A
            # draft is only used as the initial widget value if it was
            # captured against the same base version_id we're showing — that
            # way switching the "best" pick doesn't surface stale text.
            raw_draft = item.get("manual_edit_draft")
            saved_draft: dict = {}
            if isinstance(raw_draft, dict):
                saved_draft = raw_draft
            elif isinstance(raw_draft, str) and raw_draft.strip():
                try:
                    saved_draft = json.loads(raw_draft)
                except Exception:
                    saved_draft = {}

            draft_applies = saved_draft.get("base_version_id") == display_version.get("id")
            restored = False
            if draft_applies:
                if title_key not in st.session_state and saved_draft.get("title") is not None:
                    st.session_state[title_key] = saved_draft.get("title", "")
                    restored = True
                if body_key not in st.session_state and saved_draft.get("body") is not None:
                    st.session_state[body_key] = saved_draft.get("body", "")
                    restored = True
                if kw_key not in st.session_state and saved_draft.get("keywords_raw") is not None:
                    st.session_state[kw_key] = saved_draft.get("keywords_raw", "")
                    restored = True
            if restored:
                st.caption("📌 已自动恢复你上次没保存成功的手动精修")

            # Auto-save: any divergence from the base version → write to DB.
            # Piggybacks on existing reruns, no extra UI cost (see 2.7.6).
            _cur_title_in_state = st.session_state.get(title_key, cur_title)
            _cur_body_in_state  = st.session_state.get(body_key, cur_body)
            _cur_kw_in_state    = st.session_state.get(kw_key, base_kw_str)
            if (_cur_title_in_state != cur_title
                or _cur_body_in_state != cur_body
                or _cur_kw_in_state != base_kw_str):
                payload = {
                    "title": _cur_title_in_state,
                    "body":  _cur_body_in_state,
                    "keywords_raw": _cur_kw_in_state,
                    "base_version_id": display_version.get("id"),
                }
                if payload != saved_draft:
                    try:
                        db.save_manual_edit_draft(db_client, item_id, payload)
                    except Exception:
                        pass

            edit_title = st.text_input(
                "标题", value=cur_title, key=title_key,
            )
            edit_body = st.text_area(
                "正文", value=cur_body, height=300, key=body_key,
            )
            edit_kw_raw = st.text_input(
                "关键词（逗号分隔）",
                value=base_kw_str,
                key=kw_key,
                placeholder="关键词1，关键词2，关键词3",
            )

            if st.button("💾 保存手动修改并通过", key=save_key, use_container_width=True):
                new_kw = [k.strip() for k in re.split(r"[,，、\s]+", edit_kw_raw) if k.strip()]
                new_title = edit_title.strip()
                new_body  = edit_body.strip()
                new_version = db.create_version(
                    db_client,
                    item_id=item_id,
                    ai_engine="manual",
                    title=new_title,
                    body=new_body,
                    keywords=new_kw,
                    feedback="手动精修",
                )
                try:
                    mem_module.update_calibration_from_manual_edit(
                        db_client,
                        project_id=project["id"],
                        ai_title=cur_title,
                        ai_body=cur_body,
                        manual_title=new_title,
                        manual_body=new_body,
                    )
                except Exception:
                    pass
                db.update_item_status(
                    db_client, item_id, "approved",
                    best_version_id=new_version["id"],
                )
                # Drop the auto-saved draft now that the user committed.
                try:
                    db.clear_manual_edit_draft(db_client, item_id)
                except Exception:
                    pass
                st.success("已保存修改并标记为通过。调教笔记已同步更新。")
                st.rerun()


# 包成 fragment：每个卡片独立 rerun，按钮点击不再触发整页重渲染。
# Streamlit < 1.33 没有 fragment 时退化到普通函数（与 page-rerun 行为相同）。
if _FRAGMENT is not None:
    _render_item_card_fragment = _FRAGMENT(_render_item_card)
else:
    _render_item_card_fragment = _render_item_card


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

    safe_title = _html.escape(title)
    safe_body  = _html.escape(body)

    st.markdown(
        f"<div class='copy-title'>{safe_title}</div>"
        f"<div class='copy-meta'><span class='len-neutral'>{title_len} 字</span></div>"
        f"<div class='copy-body'>{safe_body}</div>",
        unsafe_allow_html=True,
    )
    if keywords:
        kw_html = " ".join(f"<span class='tag'>#{_html.escape(k)}</span>" for k in keywords)
        st.markdown(kw_html, unsafe_allow_html=True)

    # Single copy-ready block (title + body + tags) — Streamlit's built-in
    # code-block copy icon gives one-click copy of everything at once.
    if title or body:
        with st.expander("📋 复制全文（标题 + 正文 + 标签）", expanded=False):
            parts: list[str] = []
            if title:
                parts.append(f"【标题】\n{title}")
            if body:
                kw_line = " ".join(f"#{k}" for k in keywords) if keywords else ""
                body_block = f"【正文】\n{body}"
                if kw_line:
                    body_block += f"\n\n{kw_line}"
                parts.append(body_block)
            elif keywords:
                parts.append(" ".join(f"#{k}" for k in keywords))
            st.code("\n\n".join(parts), language=None)

    if not title and raw_text:
        with st.expander("⚠️ 解析失败 — 查看原始 AI 输出", expanded=True):
            st.code(raw_text, language=None)


def _render_version_comparison(
    versions: list[dict],
    item_id: str,
    item: dict,
    batch: dict,
    project: dict,
    _bp: dict,
) -> None:
    """Side-by-side multi-engine version comparison with per-engine iteration."""
    by_engine: dict[str, list[dict]] = {}
    for v in versions:
        engine = v.get("ai_engine", "unknown")
        by_engine.setdefault(engine, []).append(v)

    cols = st.columns(len(by_engine))
    engine_list = list(by_engine.keys())
    status = item["status"]

    for col, engine in zip(cols, engine_list):
        with col:
            eng_short = engine.split("/")[0].lower()
            eng_cls = eng_short if eng_short in ("claude", "gemini") else ""
            st.markdown(
                f"<span class='engine-badge {eng_cls}'>{_html.escape(engine.upper())}</span>",
                unsafe_allow_html=True,
            )
            latest = sorted(by_engine[engine], key=lambda x: x.get("version_num", 0))[-1]
            _render_single_version(latest)

            # 选为最佳：只记录 best_version_id，不直接通过
            if st.button("选为最佳", key=f"best_{item_id}_{engine}"):
                db.update_item_status(
                    db_client, item_id, status,
                    best_version_id=latest["id"],
                )
                st.rerun()

            # 每引擎独立迭代入口
            if status in ("pending", "needs_revision"):
                with st.expander("🔄 改写此版", expanded=False):
                    eng_versions = [
                        v for v in versions
                        if v.get("ai_engine", "").split("/")[0] == eng_short
                    ]
                    # Restore last-saved draft (shared per-item across engines)
                    eng_saved_draft = (item.get("feedback_draft") or "").strip()
                    eng_feedback_key = f"feedback_{item_id}_{engine}"
                    if eng_saved_draft and eng_feedback_key not in st.session_state:
                        st.session_state[eng_feedback_key] = eng_saved_draft

                    # Auto-save: persist draft on every rerun if it's diverged
                    # from DB.  Same rationale as the global iteration form —
                    # piggyback on existing reruns, no extra UI cost.
                    _eng_auto_tags = st.session_state.get(f"tags_{item_id}_{engine}", [])
                    _eng_auto_text = st.session_state.get(eng_feedback_key, "")
                    _eng_auto_combined = (
                        "、".join(_eng_auto_tags)
                        + ("；" + _eng_auto_text if _eng_auto_text else "")
                    ).strip("、；")
                    if _eng_auto_combined and _eng_auto_combined != eng_saved_draft:
                        try:
                            db.save_feedback_draft(db_client, item_id, _eng_auto_combined)
                        except Exception:
                            pass

                    sel_tags = st.multiselect(
                        "快捷标签",
                        QUICK_FEEDBACK_TAGS,
                        key=f"tags_{item_id}_{engine}",
                        label_visibility="collapsed",
                    )
                    fb_text = st.text_area(
                        "详细反馈（可选）",
                        key=eng_feedback_key,
                        height=68,
                        placeholder="修改意见...",
                    )
                    combined_fb = (
                        "、".join(sel_tags) + ("；" + fb_text if fb_text else "")
                    ).strip("、；")

                    if eng_short == "claude":
                        iter_model = st.selectbox(
                            "模型",
                            list(config.CLAUDE_MODELS.keys()),
                            format_func=lambda m: config.CLAUDE_MODELS.get(m, m),
                            key=f"iter_model_{item_id}_{engine}",
                            index=list(config.CLAUDE_MODELS.keys()).index(config.CLAUDE_MODEL)
                                  if config.CLAUDE_MODEL in config.CLAUDE_MODELS else 0,
                        )
                        iter_think = False
                    else:
                        iter_model = st.selectbox(
                            "模型",
                            list(config.GEMINI_MODELS.keys()),
                            format_func=lambda m: config.GEMINI_MODELS.get(m, m),
                            key=f"iter_model_{item_id}_{engine}",
                            index=list(config.GEMINI_MODELS.keys()).index(config.GEMINI_MODEL)
                                  if config.GEMINI_MODEL in config.GEMINI_MODELS else 0,
                        )
                        iter_think = False

                    if st.button("🔄 改写", key=f"regen_{item_id}_{engine}", use_container_width=True):
                        if not combined_fb:
                            st.warning("请先填写修改意见或选择快捷标签。")
                        else:
                            db.save_feedback_draft(db_client, item_id, combined_fb)
                            _run_iteration(
                                item, eng_versions, batch, project,
                                combined_fb, eng_short, iter_model,
                                use_thinking_override=iter_think,
                            )


def _run_iteration(
    item: dict,
    versions: list[dict],
    batch: dict,
    project: dict,
    feedback: str,
    engine_name: str,
    model_override: str = "",
    use_thinking_override: bool | None = None,
) -> None:
    """Run one iteration for an item and save the new version."""
    project_id = project["id"]
    batch_id   = batch.get("id")

    # R-042: 反馈的记忆沉淀(ingest)挪到**迭代成功之后**(见函数尾部)。
    # 旧顺序在 iterate 调用前就 ingest —— LLM 失败后用户重试同一条反馈会
    # 二次沉淀(merge 计数虚增 / 重复 session 指令); 且失败路径上反馈已
    # 变成规则, 与"迭代没发生"的事实不符。迭代 prompt 本身直接用 feedback
    # 文本, 不依赖 ingest 结果, 挪动无行为影响。
    ingest_action: Optional[str] = None

    base_prompt = project.get("system_prompt", "")
    global_mems, project_mems = db.get_confirmed_memories(
        db_client, user_id, project_id=project_id
    )
    session_instr = db.get_session_instructions(db_client, user_id, project_id=project_id)
    tactic = batch.get("tactic", "")
    tactic_suffix = proj_module.get_tactic_prompt_suffix(project, tactic)
    # Phase 1：同 batch 生成路径，iterate 走 layered—— 跟同项目主生成共享
    # cache（5 分钟 TTL 内）。
    full_system_prompt = mem_module.build_layered_system_prompt(
        base_prompt=base_prompt,
        global_memories=global_mems,
        project_memories=project_mems,
        tactic_suffix=tactic_suffix,
        calibration_notes=project.get("calibration_notes") or "",
        session_instructions=session_instr or None,
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
    # If caller passes an explicit thinking override, honour it; else fall back to batch setting
    if use_thinking_override is not None:
        thinking_flag = use_thinking_override
    else:
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
        st.error(f"迭代失败：{logger_utils.mask_secrets(result.error)}")
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

    # Incrementally update calibration notes from this single iteration.
    # Every correction the user makes carries a "why" — capture it now rather
    # than waiting for the whole batch to go green.  Runs silently; failure
    # does not block the main flow.
    try:
        prev_version = versions[-1] if versions else {}
        mem_module.update_calibration_from_iteration(
            db_client,
            project_id=project_id,
            old_title=prev_version.get("title", "") or "",
            old_body=prev_version.get("body", "") or "",
            feedback=feedback or "",
            new_title=result.title or "",
            new_body=result.body or "",
        )
    except Exception:
        pass

    # Reset item to pending so it gets reviewed again.
    # R-036: 同时清掉 best_version_id —— 旧指针不清的话, 卡片的展示版本
    # (_render_item_card 按 best_vid 选)和导出(_collect_approved_items 同
    # 口径)会一直停在迭代前的旧版本, 用户看到"迭代成功!"但内容纹丝不动。
    # 清掉后回到"无最佳 → 取最新版本"的默认行为, 新版本立即可见。
    db.update_item_status(db_client, item["id"], "pending", clear_best_version=True)

    # Iteration succeeded — drop the saved draft so the textarea doesn't
    # auto-restore it on the next render.
    db.clear_feedback_draft(db_client, item["id"])

    # R-042: 同步清掉本 item 的反馈 widget state(feedback_*/tags_*, 含比稿
    # 模式的 _{engine} 变体)。只清 DB 草稿不清 widget 的话, 下一个 rerun
    # 卡片的自动保存会拿 session_state 里的旧反馈把刚清掉的草稿"复活",
    # textarea 留着旧反馈诱导误点二次迭代("成功即清"契约被确定性打破)。
    _item_id = item["id"]
    for _k in [k for k in list(st.session_state.keys())
               if k.startswith(f"feedback_{_item_id}") or k.startswith(f"tags_{_item_id}")]:
        st.session_state.pop(_k, None)

    # R-042: 迭代确认成功后才沉淀反馈(旧逻辑在 LLM 调用前 ingest, 失败重试
    # 会双沉淀)。ingest 自身异常不影响已成功的迭代 —— 吞掉只丢 toast。
    if feedback and feedback.strip():
        try:
            ingest_result = mem_module.ingest_user_instruction(
                db_client, user_id, feedback,
                project_id=project_id,
                project_name=project.get("name", ""),
                batch_id=batch_id,
            )
            ingest_action = (ingest_result or {}).get("action")
        except Exception as exc:
            telemetry.log_event(
                "iteration_ingest_failed", item_id=str(_item_id), error=str(exc)[:200],
            )

    # Surface the merger's routing decision so the user can see whether
    # their feedback became a permanent rule, a taste note, or a 24h
    # session instruction — otherwise it feels like typed reasons vanish.
    ingest_label = {
        "rule":    "📌 反馈已沉淀为永久规则（记忆管理里可查）",
        "merge":   "📌 反馈并入了已有规则（使用次数 +1）",
        "taste":   "🎨 反馈已追加到调教笔记",
        "session": "⏳ 反馈已加入 24h 会话指令",
    }.get(ingest_action or "")
    if ingest_label:
        st.toast(ingest_label, icon="✅")

    st.success("迭代成功！")
    st.rerun()


def _collect_approved_items(items: list[dict]) -> list[dict]:
    """Build a flat list of approved items for export.

    2026-05-21：每条 dict 额外带 4 个 lineage id (project / batch / item /
    version)，给 exporter 把 ``_source_autowriter_*`` 列写进 Excel / Word，
    供 TV ingest 反向归因。``project_id`` 不在 items 表里——调用方在传入前
    要把 batch.project_id stamp 到 ``_project_id`` 上（见 list_items 各
    caller）。
    """
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
            # lineage for TV reverse-attribution
            "project_id": item.get("project_id") or item.get("_project_id"),
            "batch_id":   item.get("batch_id"),
            "item_id":    item.get("id"),
            "version_id": v.get("id"),
        })
    return result


def _auto_update_calibration_notes(project: dict, batch_id: str, items: list[dict]) -> None:
    """
    太子自动学习：批次全部通过后静默生成并保存调教笔记，无需人工确认。
    失败时不打断用户操作，但会埋点 + toast 提示一次，避免"看似学习了实际没存"。

    成功路径（包括 LLM 返回但无新观察）会把 ``batches.auto_calibrated_at`` 标记
    为本次时间戳，确保下次打开同一批次（甚至换浏览器 / 重登）不再重复反思。
    失败路径不打标记 — 下次进来还会再试一次。
    """
    # R-036: 基线必须现读 DB 行, 不能用页面渲染时的 project 快照 ——
    # 快照来自 list_projects(ttl=60 缓存), "迭代几条 → 60 秒内全批审完"
    # 这个常见流程里, 迭代刚 CAS 追加的笔记不在快照里; 反思以 stale 基线
    # 生成全文再覆盖写, 刚追加的观察被静默抹掉(lost update)。
    # 同时把原始列值(未 rstrip)留作 CAS witness —— save_calibration_notes
    # 的 .eq() 比较的是原始值, 传 rstrip 过的会永远冲突。
    fresh = None
    try:
        fresh = db.get_project(db_client, project["id"])
    except Exception:
        pass
    raw_existing = ((fresh or project).get("calibration_notes") or "")
    existing = raw_existing.rstrip()
    try:
        with st.spinner("🧠 太子学习中…"):
            notes = mem_module.generate_calibration_notes(
                project_name=project.get("name", ""),
                existing_notes=existing,
                items_with_versions=items,
            )
            # Append-only contract: generate_calibration_notes returns the
            # existing text plus any newly appended observations.  Skip the
            # save when nothing changed so the row's timestamp / dedup ordering
            # stays untouched.
            if notes and notes.rstrip() != existing:
                # R-036: 带 CAS witness 写入 —— 反思期间(LLM 调用要几秒)若有
                # 并发写(迭代沉淀/merger taste), CAS 冲突抛出 → 走下面 except:
                # 本次不保存也不打 auto_calibrated 标记, 下次打开批次重试,
                # 不再盲覆盖别人刚写的内容。
                mem_module.save_calibration_notes(
                    db_client, project["id"], notes, source="batch_reflection",
                    expected_before_text=raw_existing,
                )
                st.toast("🧠 调教笔记已新增观察（太子学习完成）")
            # 不论这次有没有新观察，标记"已学过"；下次同样的批次没必要再花 token
            db.mark_batch_auto_calibrated(db_client, batch_id)
    except Exception as exc:
        telemetry.log_event(
            "batch_reflection_save_failed",
            project_id=project.get("id"), batch_id=batch_id, error=str(exc)[:200],
        )
        st.toast("⚠ 太子学习失败，已记录日志（不影响主流程）")


def _generate_calibration_notes_ui(project: dict, batch_id: str, items: list[dict]) -> None:
    """Ask AI to reflect on this batch's *explicit* user signals and update
    calibration notes.  Only iteration feedback and manual-edit diffs count —
    bare "approved" drafts are ignored by the generator."""
    calib_key = f"pending_calibration_{batch_id}"
    existing = (project.get("calibration_notes") or "").strip()

    # Count explicit signals for the pre-flight message.
    signal_count = 0
    for it in items:
        versions = sorted(it.get("versions") or [], key=lambda v: v.get("version_num", 0))
        if len(versions) < 2:
            continue
        has_feedback = any(
            (v.get("feedback") or "").strip() and v.get("feedback") != "手动精修"
            for v in versions[1:]
        )
        has_manual_edit = (versions[-1].get("ai_engine") or "").lower() == "manual"
        if has_feedback or has_manual_edit:
            signal_count += 1

    if signal_count == 0:
        st.info(
            "本批次没有可学习的显式信号（需要至少 1 条带文字反馈的迭代，"
            "或一次手动精修）。调教笔记保持不变。"
        )
        return

    with st.spinner("AI 正在分析本批次互动，生成调教笔记…"):
        try:
            notes = (mem_module.generate_calibration_notes(
                project_name=project.get("name", ""),
                existing_notes=existing,
                items_with_versions=items,
            ) or "").strip()
            if not notes or notes == existing:
                st.info("本批次的显式信号与现有笔记一致，调教笔记保持不变。")
                return
            st.session_state[calib_key] = notes
            st.rerun()
        except Exception as e:
            st.error(f"生成失败：{logger_utils.mask_secrets(str(e))}")


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: 导出中心
# ═══════════════════════════════════════════════════════════════════════════

def page_export(project: dict) -> None:
    pname = _html.escape(project.get("name", ""))
    _hero_header("03 / EXPORT", "装箱发货。", f"项目 · {pname}")

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
    # Bulk-load items for all batches in ONE round trip. 之前是按 batch 循环
    # list_items（50 个批次 = 50 次 RT），导出页随批次增加越来越慢。改成
    # 单次 in_(batch_ids) 查询 + client 侧分桶。
    batch_ids = [b["id"] for b in batches]
    items_by_batch = db.list_items_for_batches(db_client, batch_ids)
    batch_meta: list[dict] = []
    for batch in batches:
        items    = items_by_batch.get(batch["id"], [])
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
                # R-040: tactic 来自用户自定义战术名 —— 全文件唯一漏 escape 的
                # unsafe_allow_html 注入点(存储型自 XSS), 与同行 label 对齐处理。
                f"{_html.escape(tactic)} · {approved}/{total} 篇已通过</span>",
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
    # R-039: 结果 key 按项目隔离 —— 全局 key 在切项目后仍显示上一项目的
    # "文件已就绪"下载按钮(bytes/文件名都是旧项目的, 误下载风险)。
    # _qg_key/_rb_key 同类问题早已修过, 这个漏了。
    _exp_key = f"export_center_result_{project['id']}"
    exp_state = st.session_state.get(_exp_key)
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
                st.session_state.pop(_exp_key, None)
                st.rerun()

    # Map batch_id → items for fast lookup (already loaded above)
    batch_items_map = {meta["batch"]["id"]: meta["items"] for meta in batch_meta}

    # 导出页是单 project scope，所有 batch 共用同一个 project_id。
    # _collect_approved_items 用它给 export 行挂 lineage。
    _pid = project.get("id")

    gen_label = f"📥 生成导出文件（{n_selected} 个批次）" if n_selected else "📥 生成导出文件"
    if st.button(gen_label, type="primary", disabled=(n_selected == 0), use_container_width=True):
        all_items: list[dict] = []
        for bid in selected_ids:
            all_items.extend(_collect_approved_items(
                [{**it, "_project_id": _pid} for it in batch_items_map.get(bid, [])]
            ))

        if not all_items:
            st.warning("选中的批次中没有已通过的内容，请先在「审核与迭代」中通过稿件。")
        else:
            try:
                xlsx_bytes = exporter.build_combined_excel(all_items)
                brand = project.get("brand", "") or project_name
                filename = f"xhs_{brand}_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
                st.session_state[_exp_key] = {
                    "bytes":    xlsx_bytes,
                    "count":    len(all_items),
                    "n_batches": n_selected,
                    "filename": filename,
                }
                st.rerun()
            except RuntimeError as e:
                st.error(logger_utils.mask_secrets(str(e)))

    # ── Feishu push (multi-batch) ───────────────────────────────────────
    if config.FEISHU_WEBHOOK_URL and n_selected > 0:
        st.divider()
        if st.button("🔔 推送选中批次到飞书", use_container_width=True):
            all_items = []
            for bid in selected_ids:
                all_items.extend(_collect_approved_items(
                    [{**it, "_project_id": _pid} for it in batch_items_map.get(bid, [])]
                ))
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
    _hero_header("05 / SETTINGS", "配置你的工作流。", project.get("name", ""))
    proj_module.render_project_settings(db_client, project, user_id)


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: 记忆管理
# ═══════════════════════════════════════════════════════════════════════════

def page_memory(project: Optional[dict]) -> None:
    pid   = project["id"] if project else None
    pname = project.get("name", "") if project else ""
    _hero_header("04 / MEMORY", "训练你的风格。", pname)
    mem_module.render_memory_manager(db_client, user_id, project_id=pid, project_name=pname)


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: 批次历史
# ═══════════════════════════════════════════════════════════════════════════

def page_history(project: dict) -> None:
    _hero_header("06 / HISTORY", "回顾每一次生成。", f"项目 · {project.get('name', '')}")
    # 主体内容包 fragment：删除批次、点"查看此批次"等操作触发 fragment-only
    # rerun，不重渲染 sidebar / header（50 个 batch 重渲染本身就慢，再叠 page
    # rerun 整体感觉就是卡）。Fragment 内的 ``_force_page`` + ``_rerun_app``
    # 仍然能跳转到审核页，因为这两个都是显式 app-scope rerun。
    _page_history_body(project)


def _render_session_window_panel(project: dict) -> None:
    """Phase 2.3: 展示本项目各引擎「生成会话」(session)的窗口占用 + 手动封窗。

    occupancy = ``last_prefix_tokens / window_limit``——last_prefix_tokens 是
    最近一批单次主调用的 prefix 大小, 即"这个跨批对话现在多满"。占用达
    ``SESSION_SEAL_THRESHOLD`` 会在生成后自动封窗; 这里也提供手动"封窗重开"。
    封窗后下一批自动路由到新 session(懒同步最近 50 条 approved 历史, 避重
    接续 + dedup_block 照常注入), 不会丢避重信号。
    """
    sessions = db.list_project_sessions(db_client, project["id"], status="active")
    threshold = config.SESSION_SEAL_THRESHOLD
    with st.expander(
        f"🪟 生成会话窗口 · 缓存复用（{len(sessions)} 个活跃）", expanded=False,
    ):
        if not sessions:
            st.caption("本项目还没有活跃的生成会话——生成一批内容后出现。")
            return
        st.caption(
            f"占用达 {threshold:.0%} 自动封窗并开新窗；新窗自动继承最近 50 条已通过历史，"
            "避重不断。也可手动封窗重开（例如刚大改了项目人格 / 想换个开局）。"
        )
        for s in sessions:
            sid = s.get("id")
            engine = s.get("engine", "?")
            model_id = s.get("model_id", "")
            window = int(s.get("window_limit") or 0) or 1
            used = int(s.get("last_prefix_tokens") or 0)
            pct = used / window
            hist_pairs = db.count_session_messages(db_client, sid) // 2
            near = pct >= threshold
            icon = "🔴" if near else ("🟡" if pct >= threshold * 0.75 else "🟢")
            label = _short_model(f"{engine}/{model_id}")
            row, btn = st.columns([5, 1])
            with row:
                st.markdown(
                    f"{icon} **{label}** — 占用 **{pct:.1%}**　"
                    f"（{_fmt_tok(used)} / {_fmt_tok(window)}）· 约 {hist_pairs} 篇历史在复用"
                )
                st.progress(min(max(pct, 0.0), 1.0))
                if near:
                    st.caption("⚠️ 已接近上限，下一批会自动开新窗。")
            with btn:
                if st.button(
                    "封窗重开", key=f"seal_sess_{sid}",
                    help="封存当前窗口；下一批开新窗，自动继承最近 50 条已通过历史",
                ):
                    if db.seal_session(db_client, sid, "manual"):
                        st.success("已封窗，下批将开新窗。")
                        st.rerun()
                    else:
                        st.error("封窗失败，请重试。")


def _page_history_body_impl(project: dict) -> None:
    # Phase 2.3: session 窗口占用面板(总在最前, 即便还没有批次也显示)
    _render_session_window_panel(project)

    batches = db.list_batches(db_client, project["id"], limit=50)
    if not batches:
        st.info("暂无历史批次。")
        return

    # Pre-load item counts for all batches to avoid N+1 queries
    batch_item_counts = db.get_batch_item_counts(db_client, [b["id"] for b in batches])

    # Day 5: 批次指标快照（性能 / 去重 / 注入），与 batches 一同预加载
    metrics_rows = db.list_batch_metrics(
        db_client, project["id"], limit=200, user_id=user_id,
    )
    metrics_by_batch = {m.get("batch_id"): m for m in metrics_rows if m.get("batch_id")}

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

            btn_col, del_col = st.columns([3, 1])
            with btn_col:
                if counts['total'] > 0:
                    if st.button("查看此批次", key=f"view_batch_{batch['id']}"):
                        # 跳转审核页需要 app-scope rerun（让 sidebar radio 重渲染
                        # 并切到审核页）。fragment-only rerun 不会触发 sidebar，
                        # 用户会看到 _force_page 被 set 但页面不切。
                        st.session_state[_rb_key(project["id"])] = batch["id"]
                        st.session_state["_force_page"] = "审核与迭代"
                        _rerun_app()
            with del_col:
                confirm_key = f"confirm_del_{batch['id']}"
                if st.session_state.get(confirm_key):
                    st.warning("确认删除？此操作不可撤销。")
                    yes_col, no_col = st.columns(2)
                    with yes_col:
                        if st.button("确认删除", key=f"do_del_{batch['id']}", type="primary"):
                            db.delete_batch(db_client, batch["id"])
                            st.session_state.pop(confirm_key, None)
                            st.success("已删除")
                            st.rerun()
                    with no_col:
                        if st.button("取消", key=f"cancel_del_{batch['id']}"):
                            st.session_state.pop(confirm_key, None)
                            st.rerun()
                else:
                    if st.button("🗑️ 删除批次", key=f"del_batch_{batch['id']}"):
                        st.session_state[confirm_key] = True
                        st.rerun()

            # Day 5: 性能 / 去重 / 注入指标（如果该批次曾被新代码记录过）
            m = metrics_by_batch.get(batch["id"])
            if m:
                with st.expander("📊 性能指标"):
                    phase_ms = m.get("phase_ms") or {}
                    if isinstance(phase_ms, str):
                        try:
                            phase_ms = json.loads(phase_ms)
                        except Exception:
                            phase_ms = {}
                    pcols = st.columns(4)
                    for col, (k, label) in zip(pcols, [
                        ("setup", "setup"),
                        ("llm", "llm"),
                        ("db_save", "db_save"),
                        ("embedding", "embedding"),
                    ]):
                        with col:
                            st.metric(label, f"{(phase_ms.get(k) or 0) / 1000:.1f} s")

                    counters = m.get("counters") or {}
                    if isinstance(counters, str):
                        try:
                            counters = json.loads(counters)
                        except Exception:
                            counters = {}
                    counter_keys = [
                        ("dedup_text_hits", "文本去重"),
                        ("dedup_semantic_hits", "语义去重"),
                        ("regen_attempts", "重生"),
                        ("regen_success", "重生成功"),
                        ("hard_rule_violations", "硬规则违反"),
                        ("embedding_missing", "缺向量"),
                    ]
                    ccols = st.columns(len(counter_keys))
                    for col, (k, label) in zip(ccols, counter_keys):
                        with col:
                            st.metric(label, counters.get(k, 0))

                    _render_token_panel(m.get("meta") or {})

                    injection = m.get("injection") or {}
                    if isinstance(injection, str):
                        try:
                            injection = json.loads(injection)
                        except Exception:
                            injection = {}
                    meta = m.get("meta") or {}
                    if isinstance(meta, str):
                        try:
                            meta = json.loads(meta)
                        except Exception:
                            meta = {}
                    if injection or meta:
                        badges = []
                        hard_n = injection.get("hard_global", 0) + injection.get("hard_project", 0)
                        soft_n = injection.get("soft_global", 0) + injection.get("soft_project", 0)
                        if hard_n or soft_n or injection.get("session"):
                            badges.append(
                                f"注入：硬 {hard_n} / 软 {soft_n} / 会话 {injection.get('session', 0)}"
                            )
                        if injection.get("calibration_chars"):
                            badges.append(f"调校 {injection['calibration_chars']} 字")
                        filtered = injection.get("filtered") or []
                        if filtered:
                            badges.append(f"过滤 {len(filtered)} 条")
                        thr = meta.get("dedup_threshold")
                        if thr is not None:
                            badges.append(f"阈值 {float(thr):.2f}")
                        dmode = meta.get("dedup_mode")
                        if dmode and dmode != "vector":
                            badges.append(f"去重 {dmode}")
                        if badges:
                            st.caption(" · ".join(badges))


# 包成 fragment（与 _render_queue_tab 同模式）：批次列表的删除 / "查看此批次"
# 等点击只 rerun 本 fragment，避免重渲染整页（50 个 expander + cached 但仍
# 占重的查询）。``_force_page`` 内部用 ``_rerun_app`` 切到审核页，那是显式
# app-scope rerun，跨 fragment 边界正常生效。
if _FRAGMENT is not None:
    _page_history_body = _FRAGMENT(_page_history_body_impl)
else:
    _page_history_body = _page_history_body_impl


# ═══════════════════════════════════════════════════════════════════════════
# ROUTER
# ═══════════════════════════════════════════════════════════════════════════

def _render_page() -> None:
    """Dispatch to the selected page.  Wrapped by an error boundary below
    so unhandled DB / API errors don't show Streamlit's redacted red box —
    the user sees the actual cause and can recover without re-logging in."""
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


try:
    _render_page()
except Exception as _err:
    _render_error_panel(_err)
