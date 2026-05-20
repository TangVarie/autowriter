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
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Optional


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
            self._running_phases[name] = time.monotonic()
        except Exception:
            pass

    def stop_phase(self, name: str) -> None:
        """结束一个 start_phase 开的阶段；耗时累加到 phase_ms[name]。"""
        try:
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

    # ── 计数器 ───────────────────────────────────────────────────────
    def incr(self, key: str, n: int = 1) -> None:
        """累加计数器；不存在的 key 会从 0 起算。"""
        try:
            self.counters[key] = self.counters.get(key, 0) + int(n)
        except Exception:
            pass

    def set_meta(self, key: str, value) -> None:
        """挂任意元数据（例如 ``saved=10``、``had_errors=True``）。"""
        try:
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
        """
        total_ms = (time.monotonic() - self._started_at) * 1000.0
        return {
            "batch_id":   self.batch_id,
            "project_id": self.project_id,
            "mode":       self.mode,
            "count":      self.count,
            "engines":    list(self.engines),
            "total_ms":   round(total_ms, 1),
            "phase_ms":   {k: round(v, 1) for k, v in self.phase_ms.items()},
            "counters":   dict(self.counters),
            "meta":       copy.deepcopy(self.meta),
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
            print(f"{_LOG_PREFIX} {json.dumps(data, ensure_ascii=False)}",
                  file=sys.stdout, flush=True)
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
        print(f"{_LOG_PREFIX} {json.dumps(payload, ensure_ascii=False)}",
              file=sys.stdout, flush=True)
    except Exception:
        pass
