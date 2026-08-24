"""审计 ROB-001 / ROB-002 / SUP-011 · worker 的生成 handler。

``worker.py`` 那套 job 队列(领取 / 心跳 / sweeper / 退避重试 / 优雅退出)早就
全套部署好了, 却只挂着一个 ``noop`` handler —— 运维成本照付, 而它本该解决的
问题(进程重启丢批次 / JWT 中途过期)一个没解。这就是 SUP-011。

编排搬进 ``generation_service`` 之后 handler 才写得出来。这里验三件事:

  1. **进度桥接**: 编排拿 dict 当进度总线写, worker 里要镜像到 jobs 行 —— 且
     **必须节流**, 否则一次批量生成能打出几千条 UPDATE;
  2. **不幂等的保护**: 这两个 kind 重试一次就是凭空多出重复内容, 入队时
     max_attempts 必须被强制成 1;
  3. **失败口径**: 个别 plan 失败**不能**抛异常(抛了会触发整批重跑),
     但 payload 坏了必须抛。

⚠️ 这些用例都不连库、不调 LLM —— 用假 client + 把编排换成探针。它们验的是
handler 这一层的接线, 不是生成本身。
"""

from __future__ import annotations

import threading

import pytest

import db
import worker


class _FakeSB:
    """只实现 update_job_progress 会用到的链式调用, 并记下每次写。"""

    def __init__(self):
        self.updates: list[dict] = []

    def table(self, name):
        outer = self

        class _Q:
            def __init__(self):
                self.patch = None

            def update(self, patch):
                self.patch = patch
                return self

            def eq(self, *a, **k):
                return self

            def execute(self):
                outer.updates.append(self.patch)
                return type("R", (), {"data": [{"id": "j1"}]})()

        return _Q()


# ══════════════════════════════════════════════════════════════════════
# 1 · 进度桥接与节流
# ══════════════════════════════════════════════════════════════════════

def test_progress_is_mirrored_to_the_jobs_row():
    sb = _FakeSB()
    st = worker._fresh_status(sb, "j1", total=2)
    st["progress"] = 0.5
    assert sb.updates, "写了 progress 却没有任何一条 jobs 更新"
    assert sb.updates[-1]["progress_pct"] == 50, sb.updates[-1]


def test_progress_falls_back_to_current_over_total():
    """队列模式在 plan 之间只动 ``current``, 不写 ``progress``。

    不兜这一路的话, 队列模式的 job 进度会一直卡在 0 —— 而"看不见它在动"正是
    ROB-001 里用户抱怨的那一半。
    """
    sb = _FakeSB()
    st = worker._fresh_status(sb, "j1", total=4)
    st["current"] = 3
    assert sb.updates[-1]["progress_pct"] == 75, sb.updates[-1]


def test_progress_is_throttled():
    """同一个百分比重复写不该反复发 UPDATE。

    LLM 的 progress_callback 会几十上百次地写 progress。不节流的话一次批量生成
    能打出几千条写请求, 而 jobs 行上只有一个整数百分比有意义 —— 那是拿数据库
    当日志用。
    """
    sb = _FakeSB()
    st = worker._fresh_status(sb, "j1", total=1)
    for _ in range(200):
        st["progress"] = 0.42          # 同一个百分比, 反复写
    assert len(sb.updates) == 1, f"节流失效, 发了 {len(sb.updates)} 条"

    # 百分比真的变了就要立刻上报, 不能被节流吃掉
    st["progress"] = 0.43
    assert len(sb.updates) == 2 and sb.updates[-1]["progress_pct"] == 43


def test_progress_reporting_failure_never_breaks_generation():
    """上报进度失败绝不能把生成本身弄挂 —— 那是本末倒置。"""
    class _Boom:
        def table(self, name):
            raise RuntimeError("库抖了")

    st = worker._fresh_status(_Boom(), "j1", total=1)
    st["progress"] = 0.5               # 不抛就算过
    assert st["progress"] == 0.5


def test_job_result_drops_the_lock():
    """``_lock`` 是个 threading.Lock, 进 result 会让 json 序列化直接抛 ——
    而那会让一次**成功**的生成在写回结果时失败。"""
    st = {"_lock": threading.Lock(), "errors": ["e"], "warnings": [],
          "embedding_missing": [], "message": "done"}
    out = worker._job_result(st, batch_id="b1")
    assert "_lock" not in out
    import json
    json.dumps(out)                    # 不抛就算过
    assert out["errors"] == ["e"] and out["batch_id"] == "b1"


def test_fresh_status_has_every_key_the_ui_reads():
    """少一个 key 的后果是安静的: 编排里的 setdefault 会兜住, UI 侧只是
    "什么都不显示"。所以照着 app.py 起线程时那份构造一份完整的。"""
    sb = _FakeSB()
    st = worker._fresh_status(sb, "j1")
    for k in ("running", "done", "total", "current", "message", "completed",
              "errors", "warnings", "embedding_missing", "phase", "_lock"):
        assert k in st, f"缺 {k}"


# ══════════════════════════════════════════════════════════════════════
# 2 · 不幂等的保护
# ══════════════════════════════════════════════════════════════════════

def test_generation_kinds_are_forced_to_a_single_attempt(monkeypatch):
    """这两个 kind 重试一次 = 已经成功的 plan 再生成一遍 = 凭空多出重复内容,
    而且哪儿都不报错(job 那边看起来只是"重试了一次然后成功")。"""
    seen = []

    class _SB:
        def table(self, name):
            class _Q:
                def insert(_, row):
                    seen.append(row)
                    return _

                def execute(_):
                    return type("R", (), {"data": [dict(seen[-1], id="j1")]})()
            return _Q()

    for kind in sorted(db.NON_IDEMPOTENT_JOB_KINDS):
        seen.clear()
        db.insert_job(_SB(), kind, {"x": 1}, "u1")            # 用默认 max_attempts=3
        assert seen[-1]["max_attempts"] == 1, (kind, seen[-1])
        seen.clear()
        db.insert_job(_SB(), kind, {"x": 1}, "u1", max_attempts=5)
        assert seen[-1]["max_attempts"] == 1, (kind, seen[-1])

    # 幂等的 kind 不受影响
    seen.clear()
    db.insert_job(_SB(), "noop", {"x": 1}, "u1")
    assert seen[-1]["max_attempts"] == 3, seen[-1]


def test_both_generation_handlers_are_registered():
    """SUP-011 的字面内容: 队列全套部署好了却只挂着 noop。"""
    assert {"generate_batch", "quick_gen"} <= set(worker.HANDLERS), sorted(worker.HANDLERS)


# ══════════════════════════════════════════════════════════════════════
# 3 · 失败口径
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("kind,payload", [
    ("generate_batch", {}),
    ("generate_batch", {"plans": []}),
    ("generate_batch", {"plans": "not-a-list"}),
    ("quick_gen", {}),
    ("quick_gen", {"plan": []}),
])
def test_bad_payload_raises(kind, payload):
    """payload 坏了要抛 —— 那是"一步都没走成", 让 job 落 failed 是对的。"""
    with pytest.raises(ValueError):
        worker.HANDLERS[kind]({"id": "j1", "user_id": "u1", "payload": payload}, _FakeSB())


def test_missing_user_id_raises():
    with pytest.raises(ValueError, match="user_id"):
        worker.HANDLERS["generate_batch"](
            {"id": "j1", "payload": {"plans": [{"a": 1}]}}, _FakeSB())


def test_partial_plan_failure_does_not_raise(monkeypatch):
    """个别 plan 失败**不能**抛异常。

    抛出去 → fail_or_requeue_job → 整批重跑 → 已经成功的那几个 plan 再生成一遍。
    那些失败已经记在 status["errors"] 里, 由人看见、由人决定重跑哪一段。
    """
    import generation_service as gen_service

    def _fake_queue_worker(plans, user_id, sb, status, stop_event):
        status["completed"] = ["plan-0"]
        status["errors"] = ["plan-1 挂了"]
        status["message"] = "1 成功, 1 失败"

    monkeypatch.setattr(gen_service, "queue_worker", _fake_queue_worker)
    out = worker.HANDLERS["generate_batch"](
        {"id": "j1", "user_id": "u1", "payload": {"plans": [{"a": 1}, {"b": 2}]}},
        _FakeSB())
    assert out["completed"] == 1 and out["plans"] == 2
    assert out["errors"] == ["plan-1 挂了"]


def test_handler_passes_the_shutdown_event_so_sigterm_drains(monkeypatch):
    """停止信号要复用 worker 的 ``_shutdown`` —— 收到 SIGTERM 时编排在两个 plan
    之间收手, 而不是被硬切断。容器滚动发布正是靠这个不丢半个批次。"""
    import generation_service as gen_service
    captured = {}

    def _fake(plans, user_id, sb, status, stop_event):
        captured["stop_event"] = stop_event

    monkeypatch.setattr(gen_service, "queue_worker", _fake)
    worker.HANDLERS["generate_batch"](
        {"id": "j1", "user_id": "u1", "payload": {"plans": [{"a": 1}]}}, _FakeSB())
    assert captured["stop_event"] is worker._shutdown


def test_quick_gen_reports_batch_id_back(monkeypatch):
    import generation_service as gen_service

    def _fake(plan, user_id, sb, status):
        status["batch_id"] = "b-42"
        status["saved_count"] = 3
        status["n_results"] = 3

    monkeypatch.setattr(gen_service, "quick_gen_worker", _fake)
    out = worker.HANDLERS["quick_gen"](
        {"id": "j1", "user_id": "u1", "payload": {"plan": {"project_id": "p"}}},
        _FakeSB())
    assert out["batch_id"] == "b-42" and out["saved_count"] == 3
