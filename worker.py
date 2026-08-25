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

已注册的 handler
────────────────
``noop``            部署连通性验证 (领取 → 成功 → 写回)
``generate_batch``  跑一整个队列(多个 plan)
``quick_gen``       跑单个 plan

后两个靠 ``generation_service``(审计 ROB-001/002/SUP-011 从 app.py 搬出来的
生成编排)。它们**不幂等** —— 见各自的 docstring 与
``db.NON_IDEMPOTENT_JOB_KINDS``。

⚠️ **Streamlit UI 目前仍走原来的 daemon thread 路径**, 还没改成入队。也就是说
ROB-001(进程重启丢批次) / ROB-002(JWT 中途过期)在 UI 那条路上**还没关掉** ——
这一步只是把 worker 这一侧准备好: handler 有了、进度回得来、不幂等有保护。
把 UI 切过来是下一步, 那是**行为变更**(生成从"页面开着才跑"变成"关了页面也跑"),
要单独推、单独验。
"""

from __future__ import annotations

import os
import signal
import socket
import threading
import time
import traceback

# R-042: 必须在 import db **之前**设置 —— worker 进程里 streamlit 可 import,
# db 的缓存 shim 会走真 st.cache_data(跨进程缓存, app 的 .clear() 失效不了它,
# Phase 2 的 handler 会拿 30-60s 旧记忆/批次)。本进程禁用, 退化为直查。
os.environ.setdefault("AW_DISABLE_ST_CACHE", "1")

import config
import db
import telemetry
# ⚠️ 必须在上面那行 AW_DISABLE_ST_CACHE 之后 —— generation_service 会 import db,
# 而 db 的缓存 shim 在 import 期就决定走不走真的 st.cache_data(R-042)。
# 顶层 import 而不是在 handler 里懒加载: worker 存在的全部意义就是跑这些
# handler, 它 import 不动的话应该**开机就炸**, 而不是等到领到第一个 job。
import generation_service as gen_service
# 只为了 KNOWN_ENGINES —— payload 校验要挡的是"引擎名是编的", 而合法名字这份
# 清单只能有一处定义(generator.get_engine 用的是同一个 frozenset)。
import generator


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
#
# ⚠️ **它只管"不再领新的", 绝不能同时当作"切断当前这个"的信号。**
# ``process_one`` 在 handler 正常返回后是**无条件** finish_job_success 的 ——
# 所以任何"提前收手然后正常返回"都会变成一个 100% 的假成功, 把没跑的活儿
# 静默丢掉。要么跑完, 要么抛异常; 没有第三种。(见 _generate_batch_handler。)
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


# ── Phase 2: 生成 handler ─────────────────────────────────────────────────
# 前置(已完成): 生成编排搬到 generation_service.py, 不再依赖 streamlit。
# 审计 ROB-001 / ROB-002 / SUP-011 见那个模块的文件头。


class _JobStatus(dict):
    """把编排写进 ``status`` 的进度, 镜像到 ``jobs`` 行上。

    生成编排是拿一个 dict 当"进度总线"写的 —— Streamlit 那边把同一个对象放进
    ``session_state``, UI 直接轮询它。worker 进程里没有 session_state, 所以用
    一个 dict 子类把 ``progress`` / ``message`` 的写入转成
    ``db.update_job_progress``, UI 改轮询 jobs 行就能看到同样的东西。

    **为什么是 dict 子类而不是改编排的签名**: 编排里有几十处 ``status[...] =``,
    换成回调要动的地方太多, 而这一步的全部价值是"行为没变"。子类让编排一行
    不用改 —— 它拿到的仍然是个普通 dict。

    ⚠️ **必须节流**。LLM 的 progress_callback 会几十上百次地写 ``progress``,
    每次发一条 UPDATE 就是拿数据库当日志用: 一次批量生成能打出几千条写请求,
    而 jobs 行只有一个整数百分比有意义。这里的判据是"整数百分比真的变了, 或者
    离上次上报超过 ``_PROGRESS_MIN_INTERVAL`` 秒"。
    """

    _PROGRESS_MIN_INTERVAL = 3.0

    def __init__(self, sb, job_id: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sb = sb
        self._job_id = job_id
        self._last_pct = -1
        self._last_at = 0.0
        # 上一次上报是替哪个 plan 报的。用来识别"换 plan 了, 手上这个
        # progress 是上一个 plan 留下的"—— 见 _maybe_report。
        self._plan_idx: int | None = None

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if key in ("progress", "message", "current"):
            self._maybe_report()

    def _maybe_report(self) -> None:
        # ``progress`` 是**单个 plan 内部**的 [0,1]; ``current`` 是正在跑第几个
        # plan(0 起), ``total`` 是这个 job 一共几个 plan。
        #
        # ⚠️ 不能直接拿 progress 当 job 进度。队列编排每个 plan 都会写
        # progress, 所以第一个 plan 之后这个键**一直在**, 原来那句
        # "没写 progress 时退回 current/total" 就再也走不到了 —— 持久化的 job
        # 进度于是 0→100、0→100 地反复, 而不是跨队列单调推进。
        #
        # 两个都在的时候要合起来算: 已完成的 plan 数 + 当前这个跑了多少。
        raw = self.get("progress")
        total = self.get("total") or 0
        if total:
            done = self.get("current") or 0        # 当前 plan 的下标 = 已完成数
            # ⚠️ 编排是先写 current 再写这个 plan 的第一个 progress 的。换 plan
            # 那一瞬间, self["progress"] 还是**上一个 plan** 留下的 1.0 ——
            # 照用就会先冲高再掉回来(实测 …33 → 66 → 33…)。换了 plan 就当
            # 这个 plan 才刚开始, 下一次 progress 写进来时自然会接上。
            # 只有"从某个 plan 换到另一个 plan"才算换; 第一次上报(还没记过
            # 下标)要照用手上的 progress, 否则一个刚开跑的 job 的首帧会被
            # 无谓地压成 0。
            stale = self._plan_idx is not None and done != self._plan_idx
            self._plan_idx = done
            intra = 0.0 if stale else (float(raw) if raw is not None else 0.0)
            raw = (done + max(0.0, min(1.0, intra))) / total
        elif raw is None:
            raw = 0.0
        try:
            pct = int(max(0.0, min(1.0, float(raw))) * 100)
        except (TypeError, ValueError):
            return
        now = time.time()
        if pct == self._last_pct and (now - self._last_at) < self._PROGRESS_MIN_INTERVAL:
            return
        self._last_pct, self._last_at = pct, now
        try:
            _report_progress(self._sb, self._job_id, pct, self.get("message"))
        except Exception:
            # 进度上报失败绝不能把生成本身弄挂 —— 那是本末倒置。
            # db.update_job_progress 自己已经埋点了。
            pass


def _fresh_status(sb, job_id: str, **extra) -> "_JobStatus":
    """编排期望的初始 status —— 与 app.py 里 UI 起线程时构造的那份保持一致。

    ⚠️ 少一个 key 的后果是安静的: 编排里多处 ``status.setdefault(...)`` 会兜住,
    但 UI 侧读不到就只是"什么都不显示"。所以这里照抄一份完整的。
    """
    base = {
        "running": True, "done": False, "total": 0, "current": 0,
        "message": "准备中…", "completed": [], "errors": [],
        "warnings": [], "embedding_missing": [],
        "phase": "starting", "_lock": threading.Lock(),
    }
    base.update(extra)
    return _JobStatus(sb, job_id, base)


def _job_result(status: dict, **extra) -> dict:
    """把 status 收敛成落进 ``jobs.result`` 的那份摘要。

    ``_lock`` 不能进去 —— 它是个 ``threading.Lock``, json 序列化直接抛,
    而那会让一次**成功**的生成在写回结果时失败。
    """
    out = {
        "errors": list(status.get("errors") or []),
        "warnings": list(status.get("warnings") or []),
        "embedding_missing": list(status.get("embedding_missing") or []),
        "message": status.get("message"),
    }
    out.update(extra)
    return out


# ── payload 校验 ────────────────────────────────────────────────────────
#
# ⚠️ **Streamlit 控件上的上限不是安全边界**。UI 里 count 有 number_input 的
# max_value、引擎是 multiselect、角色数是 2-6 的 slider —— 这些都只约束"从
# 界面走"的那条路。而 jobs 的 RLS 策略只要求 user_id = auth.uid(), payload
# 是自由 JSONB: 任何已登录用户都可以直接 INSERT 一条自己想要的 payload。
#
# 不校验的后果不是"参数怪":
#   · generator.py 里 ThreadPoolExecutor(max_workers=len(drafts)) 随 count
#     线性长, 另一处 max_workers = 角色数 × 引擎数 且**引擎列表不去重** ——
#     一条精心构造的 job 就能把 worker 的线程开爆;
#   · 每一路都是真的付费模型调用, count 没上限等于账单没上限。
#
# 所以这几个上限必须在 worker 这一层**再挡一次**, 而且是**拒绝**不是夹逼:
# 夹逼会把一条攻击性 payload 悄悄变成一条正常任务, 日志里什么都看不见。

_MAX_PLANS_PER_JOB = 50          # 一个队列 job 里最多几个 plan
_MAX_ROLES = 6                   # 与 UI 的 slider 上限一致
_MIN_ROLES = 2


def _validate_plan(plan: dict) -> None:
    """就地校验(并规范化)一个 plan。不合法就抛 ValueError。

    ``engines`` 会被**就地去重**(保序) —— 池的大小是 角色数 × 引擎数, 重复项
    是成倍放大它的最省事办法, 而重复地跑同一个引擎对用户没有任何价值。
    """
    if not isinstance(plan, dict):
        raise ValueError(f"plan 必须是对象, 收到 {type(plan).__name__}")
    if not str(plan.get("project_id") or "").strip():
        raise ValueError("plan 里没有 project_id")

    try:
        count = int(plan.get("count", config.DEFAULT_GENERATION_COUNT))
    except (TypeError, ValueError):
        raise ValueError(f"count 不是整数: {plan.get('count')!r}") from None
    if not 1 <= count <= config.MAX_GENERATION_COUNT:
        raise ValueError(
            f"count={count} 超出 1..{config.MAX_GENERATION_COUNT}")
    plan["count"] = count

    engines = plan.get("engines", ["claude"])
    if not isinstance(engines, list) or not engines:
        raise ValueError(f"engines 必须是非空列表, 收到 {engines!r}")
    seen: list[str] = []
    for e in engines:
        if not isinstance(e, str) or e not in generator.KNOWN_ENGINES:
            raise ValueError(
                f"未知引擎 {e!r}; 认得的只有 {sorted(generator.KNOWN_ENGINES)}")
        if e not in seen:
            seen.append(e)
    plan["engines"] = seen

    if plan.get("use_multi_role"):
        try:
            n_roles = int(plan.get("n_roles", 3))
        except (TypeError, ValueError):
            raise ValueError(f"n_roles 不是整数: {plan.get('n_roles')!r}") from None
        if not _MIN_ROLES <= n_roles <= _MAX_ROLES:
            raise ValueError(f"n_roles={n_roles} 超出 {_MIN_ROLES}..{_MAX_ROLES}")
        plan["n_roles"] = n_roles


@register("generate_batch")
def _generate_batch_handler(job: dict, sb) -> dict:
    """跑一整个队列(多个 plan)。

    ⚠️ **这个 job 不是幂等的**。每个 plan 跑完就往 items/versions 落库了, 重试
    会把已经成功的那几个 plan 再生成一遍 —— 用户看到的是凭空多出来的重复内容,
    而且没有任何地方会报错。所以:

      · 入队时 ``max_attempts`` 必须是 1 —— ``db.insert_job`` 对
        ``NON_IDEMPOTENT_JOB_KINDS`` 里的 kind 会强制改成 1;
      · handler **不因为个别 plan 失败就抛异常** —— 那些失败已经记在
        ``status["errors"]`` 里, 抛出去只会触发整批重跑。
        只有"整批一步都没走成"(payload 坏了)才抛。

    ⚠️ **排空时不能把 _shutdown 传进编排**。曾经是传的, 想法是"SIGTERM 时在
    两个 plan 之间收手"; 实际后果是: 编排提前 return → handler 正常返回 →
    ``process_one`` 无条件 ``finish_job_success`` → job 变成终态 success、
    进度 100%, **剩下的 plan 永久跳过而且没有任何地方报错**。滚动发布每次
    都会踩, 而且因为 kind 在 ``NON_IDEMPOTENT_JOB_KINDS`` 里、max_attempts
    被强制成 1, 也不能靠重排兜回来。

    所以排空契约就按本文件开头写的那样: ``_shutdown`` 只管**停止领新 job**,
    当前这个 job 由 handler 跑到底。编排因此拿一个自己的、永远不会被 set 的
    事件。宽限期用完被 SIGKILL 的话, job 留在 running 由 sweeper 判失败 ——
    那是**看得见**的失败, 比一个 100% 的假 success 好。
    """
    payload = job.get("payload") or {}
    plans = payload.get("plans")
    if not isinstance(plans, list) or not plans:
        raise ValueError("generate_batch 的 payload 里没有 plans —— 无法执行")
    # job 级的检查排在 plan 级前面: 没有 user_id 是这条 job 整个不成立,
    # 先报它比报"第 3 个 plan 的 count 不对"有用得多。
    user_id = job.get("user_id")
    if not user_id:
        raise ValueError("generate_batch 的 job 没有 user_id")
    if len(plans) > _MAX_PLANS_PER_JOB:
        raise ValueError(
            f"一个 job 里有 {len(plans)} 个 plan, 超出上限 {_MAX_PLANS_PER_JOB}")
    for n, p in enumerate(plans):
        try:
            _validate_plan(p)
        except ValueError as exc:
            raise ValueError(f"第 {n + 1} 个 plan 不合法: {exc}") from None

    status = _fresh_status(sb, job["id"], total=len(plans))
    # queue_worker 是那个"保证 phase 一定落回 done"的外层 shim, 它吞掉一切异常。
    # 这里正是要它这个性质 —— 见上面关于不幂等的说明。
    gen_service.queue_worker(plans, user_id, sb, status, threading.Event())

    completed = list(status.get("completed") or [])
    return _job_result(status, plans=len(plans), completed=len(completed))


@register("quick_gen")
def _quick_gen_handler(job: dict, sb) -> dict:
    """跑单个 plan(快速生成)。不幂等的理由与 generate_batch 相同。"""
    payload = job.get("payload") or {}
    plan = payload.get("plan")
    if not isinstance(plan, dict) or not plan:
        raise ValueError("quick_gen 的 payload 里没有 plan —— 无法执行")
    _validate_plan(plan)
    user_id = job.get("user_id")
    if not user_id:
        raise ValueError("quick_gen 的 job 没有 user_id")

    status = _fresh_status(sb, job["id"], progress=0.0,
                           batch_id=None, saved_count=0, n_results=0)
    gen_service.quick_gen_worker(plan, user_id, sb, status)
    return _job_result(status, batch_id=status.get("batch_id"),
                       saved_count=status.get("saved_count"),
                       n_results=status.get("n_results"))


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
        db.heartbeat_job(sb, job_id, worker_id=WORKER_ID)
        stop_event.wait(HEARTBEAT_INTERVAL)


# ── 处理一个 job ────────────────────────────────────────────────────────
def process_one(sb, job: dict) -> None:
    job_id = job["id"]
    kind = job.get("kind")
    handler = HANDLERS.get(kind)
    if handler is None:
        # 无 handler 不该重试(重试也还是没 handler) → 直接 failed。
        db.mark_job_failed(
            sb, job_id, f"no handler registered for kind={kind}", worker_id=WORKER_ID,
        )
        telemetry.log_event("job_no_handler", job_id=str(job_id), kind=kind)
        return

    stop_hb = threading.Event()
    hb = threading.Thread(
        target=_heartbeat_loop, args=(sb, job_id, stop_hb), daemon=True
    )
    hb.start()
    try:
        result = handler(job, sb)
        # worker_id 守 CAS: 若处理期间本 job 已被 sweeper 退回 + 他人重领,
        # 这次成功会被跳过(不覆盖新 attempt)。
        db.finish_job_success(
            sb, job_id, result if isinstance(result, dict) else {"result": result},
            worker_id=WORKER_ID,
        )
        telemetry.log_event("job_success", job_id=str(job_id), kind=kind, worker=WORKER_ID)
    except Exception:
        tb = traceback.format_exc()
        # expected_claimed_by=本 worker: 仅当这行仍归我时才退回/置败。
        new_status = db.fail_or_requeue_job(sb, job, tb, expected_claimed_by=WORKER_ID)
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
            # review #2: sweep 放在领取之前、每轮都调用(内部按 SWEEP_INTERVAL
            # 时间节流)。之前只在"队列空"分支扫, 持续 backlog 下永远不扫 →
            # 崩掉的 worker 留下的 running 行得不到回收。
            maybe_sweep(sb)
            job = db.claim_one_job(sb, WORKER_ID, WORKER_KINDS)
            if job is None:
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
