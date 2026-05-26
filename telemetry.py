"""
批次指标埋点 / 可观测性
─────────────────────────────────────────────────────────────────────────

用途：当生成"慢 / 卡 / 重"时，能快速定位是 LLM / DB / embedding / 网络
哪一段拖了后腿，以及去重和重生机制是否真的在工作。

设计原则：
  - 零侵入：用 ``with metrics.phase("llm"):`` 这种上下文管理器包裹关键
    代码段，不污染主业务逻辑。
  - 失败安全：埋点失败、写日志失败都不影响主流程；最坏情况就是这一批
    缺少指标。
  - 同时输出两份数据：
    1. stdout 一行结构化 JSON（Streamlit 日志 / Sentry / Datadog 都能解析）
    2. status 字典里挂一份给前端 UI 实时展示

调用示例
─────────
::

    m = telemetry.BatchMetrics(batch_id, project_id, mode="queue", count=10)
    with m.phase("llm"):
        results = generate_batch(...)
    with m.phase("db_save"):
        save(...)
    m.incr("dedup_text_hits", 3)
    m.incr("regen_attempts")
    m.close(status_dict)   # 写入 status["metrics"] + stdout 一行 JSON

可用阶段名（约定，不强制）：``setup`` / ``llm`` / ``db_save`` /
``embedding`` / ``dedup_check`` / ``regen``。

可用计数器（约定）：
  - ``dedup_text_hits``     —— 文本去重命中次数
  - ``dedup_semantic_hits`` —— 语义去重命中次数（cos ≥ 阈值）
  - ``regen_attempts``      —— 触发自动重生的次数
  - ``regen_success``       —— 重生后通过去重的次数
  - ``llm_calls``           —— 调用 LLM 的次数（含重生）
"""

from __future__ import annotations

import copy
import json
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Optional

import logger_utils  # R-023: stdout 日志写出前脱敏 secret


# stdout 日志前缀；用于在大量日志里 grep 一行的指标摘要
_LOG_PREFIX = "[BATCH_METRICS]"


@dataclass
class BatchMetrics:
    """单批次的指标记录器。

    一般生命周期：调用方在生成开始时创建一个实例，过程中用 ``phase``
    / ``incr`` 写入数据，最后用 ``close(status)`` 把指标输出到日志 +
    挂到 status 字典。
    """

    batch_id:   str = ""
    project_id: str = ""
    mode:       str = "queue"  # "queue" | "quick"
    count:      int = 0  # 计划生成的 item 数
    engines:    list[str] = field(default_factory=list)

    # 各阶段累积毫秒数；按需写入（不在字典里就视为 0）
    phase_ms:   dict[str, float] = field(default_factory=dict)
    # 计数器：dedup_text_hits、regen_attempts 等
    counters:   dict[str, int]   = field(default_factory=dict)
    # 任意附加元数据，调用方可以挂结果摘要
    meta:       dict             = field(default_factory=dict)

    _started_at: float = field(default_factory=time.monotonic)
    # ``incr`` / ``start_phase`` / ``stop_phase`` 可能在 ThreadPoolExecutor 的多个
    # worker 里并发触发（generate_batch_multi_role 把角色分发到线程池）。GIL 不
    # 保证 ``dict.get + setitem`` 复合操作的原子性，所以加一把轻量锁。
    # repr=False / compare=False：Lock 不可比较 / repr 没意义，避免污染 __eq__/__repr__。
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False,
    )

    # ── 阶段计时 ─────────────────────────────────────────────────────
    # 提供两种用法：
    #   1. 上下文管理器：``with m.phase("llm"): ...``  —— 包裹小块代码最简洁
    #   2. start/stop：``m.start_phase("llm"); ...; m.stop_phase("llm")``
    #      —— 不想为了埋点重排缩进的大块代码用这种，跨多个语句最方便
    #
    # 同一个 name 多次累加；失败照样向上抛但耗时也会记录。
    _running_phases: dict[str, float] = field(default_factory=dict)

    def start_phase(self, name: str) -> None:
        """开启一个阶段计时；可与同名 stop_phase 配对，跨任意行使用。

        如果该阶段已在计时（嵌套或忘了 stop），覆盖起点重新计；
        不抛错，确保埋点本身不会拖垮主流程。
        """
        try:
            with self._lock:
                self._running_phases[name] = time.monotonic()
        except Exception:
            pass

    def stop_phase(self, name: str) -> None:
        """结束一个 start_phase 开的阶段；耗时累加到 phase_ms[name]。"""
        try:
            with self._lock:
                t0 = self._running_phases.pop(name, None)
                if t0 is None:
                    return
                self.phase_ms[name] = self.phase_ms.get(name, 0.0) + (time.monotonic() - t0) * 1000.0
        except Exception:
            pass

    @contextmanager
    def phase(self, name: str):
        """计时上下文：进入时 start_phase，退出时 stop_phase。
        与 start/stop 等价，挑顺手的用就行。
        """
        self.start_phase(name)
        try:
            yield
        finally:
            self.stop_phase(name)

    # ── Token / cost 聚合 ────────────────────────────────────────────
    # 每次 LLM 调用拿到 token_usage 后调用 add_tokens 累加；最终结果
    # 挂在 meta["token_totals"]，结构：
    #   {
    #     "input": N, "output": N, "cache_read": N, "cache_create": N,
    #     "thinking": N, "cost_usd": float,
    #     "by_model": {"claude/claude-opus-4-7": {...}, "gemini/gemini-2.5-pro": {...}},
    #     "by_source": {"main": {...}, "compliance_recheck": {...}, ...},
    #   }
    # ``source`` 区分主生成 vs 内部辅助调用（合规复审 / 选优 / 精修），
    # 面板能分别展示"为本批生成花了多少 / 为合规检查花了多少"。
    def add_tokens(self, model_full: str, usage: dict, source: str = "main") -> None:
        if not isinstance(usage, dict) or not usage:
            return
        try:
            import config as _config  # local import: telemetry has no top-level deps
            cost = _config.estimate_cost_usd(model_full, usage)
        except Exception:
            cost = 0.0
        keys = ("input", "output", "cache_read", "cache_create", "thinking")
        try:
            with self._lock:
                totals = self.meta.setdefault(
                    "token_totals",
                    {"by_model": {}, "by_source": {}, "by_source_model": {}},
                )
                for k in keys:
                    totals[k] = totals.get(k, 0) + int(usage.get(k) or 0)
                totals["cost_usd"] = totals.get("cost_usd", 0.0) + cost

                per_model = totals["by_model"].setdefault(model_full, {})
                for k in keys:
                    per_model[k] = per_model.get(k, 0) + int(usage.get(k) or 0)
                per_model["cost_usd"] = per_model.get("cost_usd", 0.0) + cost

                per_src = totals["by_source"].setdefault(source, {})
                for k in keys:
                    per_src[k] = per_src.get(k, 0) + int(usage.get(k) or 0)
                per_src["cost_usd"] = per_src.get("cost_usd", 0.0) + cost

                # Phase 2.1 review fix: by_source × by_model 二维交叉。
                # ``_commit_session_tokens`` 只看 source='main' 的 token 累加到
                # session.running_input_tokens —— compliance_recheck / multi_role
                # _select / multi_role_refine / dedup_regen 这些不走 session
                # prefix 的辅助调用本来就不该计入 session 窗口。原有 by_model
                # 是各 source 总和, 不区分,精确归集需要二维 dict。
                by_src_model = totals.setdefault("by_source_model", {})
                src_models = by_src_model.setdefault(source, {})
                per_sm = src_models.setdefault(model_full, {})
                for k in keys:
                    per_sm[k] = per_sm.get(k, 0) + int(usage.get(k) or 0)
                per_sm["cost_usd"] = per_sm.get("cost_usd", 0.0) + cost
        except Exception:
            pass

    # ── 计数器 ───────────────────────────────────────────────────────
    def incr(self, key: str, n: int = 1) -> None:
        """累加计数器；不存在的 key 会从 0 起算。

        多线程 worker 同时 incr 同一个 key 时，``dict.get + setitem`` 不是原子的
        （GIL 只保证单字节码原子），不加锁会偶发丢更新。``threading.Lock`` 开销
        在埋点路径可忽略。
        """
        try:
            with self._lock:
                self.counters[key] = self.counters.get(key, 0) + int(n)
        except Exception:
            pass

    def set_meta(self, key: str, value) -> None:
        """挂任意元数据（例如 ``saved=10``、``had_errors=True``）。"""
        try:
            with self._lock:
                self.meta[key] = value
        except Exception:
            pass

    # ── 结束 ─────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        """打平成一个 dict，方便序列化 / 比较 / 测试。

        meta 走 deepcopy：调用方常把 ``inject_report`` dict 引用挂进
        ``meta["injection"]``，且 worker 线程在后续 plan 里仍会写 inject_report。
        如果只浅拷贝，主线程渲染看板时读到的是一份正在被改的 dict，可能命中
        list 迭代中 size 变化或 setdefault append 半成品。

        快照期间取锁，确保和 ``incr``/``stop_phase`` 看到的是一致瞬时值。
        """
        total_ms = (time.monotonic() - self._started_at) * 1000.0
        with self._lock:
            phase_ms_snapshot = dict(self.phase_ms)
            counters_snapshot = dict(self.counters)
            meta_snapshot     = copy.deepcopy(self.meta)
        return {
            "batch_id":   self.batch_id,
            "project_id": self.project_id,
            "mode":       self.mode,
            "count":      self.count,
            "engines":    list(self.engines),
            "total_ms":   round(total_ms, 1),
            "phase_ms":   {k: round(v, 1) for k, v in phase_ms_snapshot.items()},
            "counters":   counters_snapshot,
            "meta":       meta_snapshot,
        }

    def close(
        self,
        status: Optional[dict] = None,
        persist: Optional[Callable[[dict], None]] = None,
    ) -> dict:
        """把指标写到 stdout 一行 JSON，并挂到 status["metrics_list"]。

        ``status`` 可以是 _queue_worker / _quick_gen_worker 的状态字典；
        多批次的队列会在 status["metrics_list"] 累积每批的快照，供
        UI 渲染一个简单的"本次队列耗时构成"卡片。

        Day 5：``persist`` 是可选的持久化钩子（典型是 lambda d: db.insert_batch_metrics(...)），
        在 stdout + status 写入完成后调用。失败不抛——埋点掉链子不能拖死生成主流程。
        """
        data = self.to_dict()
        # 1. stdout 一行 JSON（生产环境会被 Streamlit / 容器日志捕获）
        try:
            _line = logger_utils.mask_secrets(json.dumps(data, ensure_ascii=False))
            print(f"{_LOG_PREFIX} {_line}", file=sys.stdout, flush=True)
        except Exception:
            pass
        # 2. 挂到 status 上给 UI 用
        if status is not None:
            try:
                lst = status.setdefault("metrics_list", [])
                lst.append(data)
                status["last_metrics"] = data
            except Exception:
                pass
        # 3. 可选：落库持久化
        if persist is not None:
            try:
                persist(data)
            except Exception:
                # persist 内部已自己埋点；这里再吞一层避免上调用方需要 try
                pass
        return data


# 便捷函数：一次性记录一个简单事件（不需要计时上下文时用）
def log_event(event: str, **fields) -> None:
    """打印一行结构化日志（不需要计时时用）。

    例：``telemetry.log_event("queue_started", n_plans=4, user_id=uid)``
    """
    try:
        payload = {"event": event, **fields}
        line = logger_utils.mask_secrets(json.dumps(payload, ensure_ascii=False))
        print(f"{_LOG_PREFIX} {line}", file=sys.stdout, flush=True)
    except Exception:
        pass
