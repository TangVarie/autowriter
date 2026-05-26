"""
worker.py — DB-backed job 队列的后台 worker 进程 (R-018)。

独立于 Streamlit 进程运行, 从 ``autowriter.jobs`` 表领取并处理任务。Streamlit
进程 reload / 容器滚动发布 / OOM 被 kill 都不会丢正在跑的任务: worker 在另一个
进程里, 任务状态全在 DB, 心跳超时由 sweeper 退回重试。

运行::

    python worker.py

依赖与 app 同一份 requirements; worker 不需要 ScriptRunContext, 不调任何
``st.*`` —— 它调的是 generator / db / memory 等纯业务函数。

环境变量
────────
    SUPABASE_URL                 必填
    SUPABASE_SERVICE_ROLE_KEY    必填。service_role 绕 RLS 领取任意用户的 job,
                                 必须从环境注入, 不要硬编码进镜像/源码; 泄露立即
                                 在 Supabase Dashboard rotate。
    WORKER_ID_PREFIX             默认 "aw-worker"; 多机部署建议带主机名便于排错。
    WORKER_KINDS                 默认空(领所有 kind)。逗号分隔可限定, 例如
                                 "generate_batch,quick_gen"。
    POLL_INTERVAL_SECONDS        默认 2; 队列空时轮询间隔。
    HEARTBEAT_INTERVAL_SECONDS   默认 15; 处理中心跳频率。
    HEARTBEAT_TIMEOUT_SECONDS    默认 90; 超过此时长无心跳 sweeper 回收。
    SWEEP_INTERVAL_SECONDS       默认 60; 主循环 idle 时多久扫一次死 job。

部署
────
Procfile 已加 ``worker: python worker.py`` —— Render / Railway / Heroku 类
平台会把它作为独立 service / dyno 跑。**Streamlit Cloud 不支持后台进程**,
那种部署要另起一台常驻主机 (systemd / supervisor)。Supabase Edge Functions
也不适用 (Deno 上限 ~2 分钟, 单 batch 经常 >10 分钟)。

进度 (Phase 1)
──────────────
当前只注册 ``noop`` handler 做部署连通性验证 (领取 → 成功 → 写回)。
``generate_batch`` / ``quick_gen`` handler —— 把 app.py 的生成编排逻辑搬进
worker —— 是 Phase 2; 在那之前 Streamlit UI 仍走原 daemon thread 路径, 本
worker 不影响现有行为。Phase 2 接入点见下方 ``# Phase 2 TODO``。
"""

from __future__ import annotations

import os
import signal
import socket
import threading
import time
import traceback

import config
import db
import telemetry


# ── 配置（全部从环境读, 带默认值）────────────────────────────────────────
WORKER_ID = (
    f"{os.environ.get('WORKER_ID_PREFIX', 'aw-worker')}"
    f"-{socket.gethostname()}-{os.getpid()}"
)
# 空 → None = 领所有 kind; 否则只领白名单内的(让不同 worker 分工)
WORKER_KINDS = [
    k.strip() for k in os.environ.get("WORKER_KINDS", "").split(",") if k.strip()
] or None
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "2"))
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL_SECONDS", "15"))
HEARTBEAT_TIMEOUT = int(os.environ.get("HEARTBEAT_TIMEOUT_SECONDS", "90"))
SWEEP_INTERVAL = int(os.environ.get("SWEEP_INTERVAL_SECONDS", "60"))


# 优雅退出: SIGTERM/SIGINT 时停止领新 job, 当前 job 由 handler 跑完(daemon
# 心跳线程随进程退出)。容器滚动发布先发 SIGTERM, 这样不会硬切断正在跑的任务。
_shutdown = threading.Event()


def _on_signal(sig, frame) -> None:
    telemetry.log_event("worker_signal_drain", worker=WORKER_ID, sig=int(sig))
    _shutdown.set()


# ── handler 注册表 ──────────────────────────────────────────────────────
# handler 签名: ``def handler(job: dict, sb) -> dict``。返回的 dict 落到
# jobs.result; 抛异常 → fail_or_requeue_job(按 attempts 重试或置 failed)。
HANDLERS: dict = {}


def register(kind: str):
    """把 ``def my_handler(job, sb): ...`` 注册为 kind=X 的处理器。"""
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


@register("noop")
def _noop_handler(job: dict, sb) -> dict:
    """Phase 1 部署连通性验证: 立即成功, 回显 payload。

    用法: 在 Supabase SQL Editor 或 UI 入队一条
    ``INSERT INTO autowriter.jobs (kind, payload, user_id) VALUES
    ('noop', '{"hi": 1}', '<你的 user uuid>')``, 看 worker 是否领取并把
    status 写成 success、result 带 echo。
    """
    return {"ok": True, "echo": job.get("payload"), "worker": WORKER_ID}


# ── Phase 2 TODO ──────────────────────────────────────────────────────────
# 在这里 @register("generate_batch") / @register("quick_gen") 真正的 handler。
# 前置: 把 app.py 里的生成编排(_save_batch_results / _run_semantic_dedup_pass /
# _resolve_engine_sessions / _commit_session_tokens / _run_hard_constraint_check
# / _resolve_queue_strategy 等)抽到一个不依赖 streamlit 的模块(例如
# generation_service.py), 让 handler 能在 worker 进程里 import 复用。handler 内
# 用 _report_progress(sb, job["id"], pct, msg) 写进度, UI 轮询 jobs 行展示。
# 这步与 R-020(拆 app.py)天然重叠, 放 Phase 2 一起做。


def _report_progress(sb, job_id: str, pct: int, message: str | None = None) -> None:
    """handler 调它更新进度; UI 轮询同一 job 行即可看到。"""
    db.update_job_progress(sb, job_id, pct, message)


# ── 心跳线程 ────────────────────────────────────────────────────────────
def _heartbeat_loop(sb, job_id: str, stop_event: threading.Event) -> None:
    """独立线程: 仅刷心跳, 不跑业务。

    单独起线程的关键: handler 里一次 LLM 调用可能阻塞几十秒到几分钟, 如果心跳
    和业务在同一线程, 这期间没法刷心跳, sweeper 会误判 worker 死掉把任务退回 →
    重复执行。独立心跳线程让长调用期间心跳照常。
    """
    while not stop_event.is_set():
        db.heartbeat_job(sb, job_id)
        stop_event.wait(HEARTBEAT_INTERVAL)


# ── 处理一个 job ────────────────────────────────────────────────────────
def process_one(sb, job: dict) -> None:
    job_id = job["id"]
    kind = job.get("kind")
    handler = HANDLERS.get(kind)
    if handler is None:
        # 无 handler 不该重试(重试也还是没 handler) → 直接 failed。
        db.mark_job_failed(sb, job_id, f"no handler registered for kind={kind}")
        telemetry.log_event("job_no_handler", job_id=str(job_id), kind=kind)
        return

    stop_hb = threading.Event()
    hb = threading.Thread(
        target=_heartbeat_loop, args=(sb, job_id, stop_hb), daemon=True
    )
    hb.start()
    try:
        result = handler(job, sb)
        db.finish_job_success(
            sb, job_id, result if isinstance(result, dict) else {"result": result},
        )
        telemetry.log_event("job_success", job_id=str(job_id), kind=kind, worker=WORKER_ID)
    except Exception:
        tb = traceback.format_exc()
        new_status = db.fail_or_requeue_job(sb, job, tb)
        telemetry.log_event(
            "job_error", job_id=str(job_id), kind=kind,
            status=new_status, error=tb[:300],
        )
    finally:
        stop_hb.set()


# ── 死 job sweeper（主循环 idle 时定期顺手扫）────────────────────────────
_last_sweep = 0.0


def maybe_sweep(sb) -> None:
    global _last_sweep
    if time.time() - _last_sweep < SWEEP_INTERVAL:
        return
    _last_sweep = time.time()
    recovered = db.sweep_dead_jobs(sb, HEARTBEAT_TIMEOUT)
    if recovered:
        telemetry.log_event("job_sweep_recovered", count=recovered, worker=WORKER_ID)


# ── 主循环 ──────────────────────────────────────────────────────────────
def main() -> None:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    sb = db.get_service_client()
    telemetry.log_event("worker_start", worker=WORKER_ID, kinds=WORKER_KINDS or "ALL")
    while not _shutdown.is_set():
        try:
            job = db.claim_one_job(sb, WORKER_ID, WORKER_KINDS)
            if job is None:
                maybe_sweep(sb)
                _shutdown.wait(POLL_INTERVAL)
                continue
            process_one(sb, job)
        except Exception:
            # 主循环任何意外(网络抖动 / RPC 异常)都不该让 worker 退出; 退避后重试。
            telemetry.log_event(
                "worker_loop_error", worker=WORKER_ID,
                error=traceback.format_exc()[:300],
            )
            _shutdown.wait(POLL_INTERVAL * 5)
    telemetry.log_event("worker_stop", worker=WORKER_ID)


if __name__ == "__main__":
    main()
