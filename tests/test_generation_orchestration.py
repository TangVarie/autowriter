"""审计 SUP-012 · 两条生成编排的**特征化测试**(characterization test)。

``_queue_worker_impl``(457 行)与 ``_quick_gen_worker``(373 行)是两份重复编排,
而且**已经漂移过一次**(原 app.py:3134 的注释自陈)。合并它们之前必须先把两条
路径**当前的行为录下来**——否则合并之后没人说得清哪些差异是本来就有的、哪些
是合并时不小心引进来的。

判据不是"结果看起来对", 而是**逐次外部调用的序列**: 调了谁、按什么顺序、
关键入参是什么。替身与录音机在 tests/genfakes.py。

── 这份录音说明了什么 ──────────────────────────────────────────────────
两条路径的主干是**逐步一致的 23 步**。真正的差异只有五处, 全部在下面
``test_the_only_differences_are_these_five`` 里逐条钉住。**能合并的信心来自
这份录音, 不是来自"读起来差不多"。**
"""

from __future__ import annotations

import threading

from tests.genfakes import PLAN, fresh_status, patched

# 两条路径**共有**的主干。这 23 步的顺序是行为契约的核心 ——
# 有数据依赖的地方顺序不能动(例: 历史向量池必须在 _save_batch_results **之前**
# 预热, 否则本批刚落库的无向量版本会挤占 limit 名额, 把真历史挤出查重池)。
SHARED_PIPELINE = [
    "db.get_confirmed_memories",
    "db.list_example_items",
    "db.list_example_items",
    "db.get_session_instructions",
    "mem.filter_soft_by_relevance",
    "mem.filter_soft_by_relevance",
    "lib.build_brief",
    "lib.fetch_flywheel_lessons",
    "mem.render_flywheel_block",
    "mem.build_layered_system_prompt",
    "db.create_batch",
    "db.get_recent_titles_and_openings",
    "dedup.embeddings_available",
    "_resolve_engine_sessions",
    "gen.generate_batch",
    "_commit_session_tokens",
    "_update_session_occupancy",
    "_save_batch_results",
    "_run_semantic_dedup_pass",
    "validator.filter_hard",
    "validator.filter_hard",
    "_run_hard_constraint_check",
    "db.insert_batch_metrics",
]


def _run_quick(monkeypatch, plan=None, **kw):
    import generation_service as gs
    with patched(monkeypatch, **kw) as rec:
        st = fresh_status()
        gs.quick_gen_worker(dict(plan or PLAN), "u1", object(), st)
    return rec, st


def _run_queue(monkeypatch, plans=None, **kw):
    import generation_service as gs
    with patched(monkeypatch, **kw) as rec:
        st = fresh_status()
        gs.queue_worker_impl([dict(p) for p in (plans or [PLAN])],
                             "u1", object(), st, threading.Event())
    return rec, st


def _tail(names: list[str]) -> list[str]:
    """去掉前缀的"取项目"那一步, 剩下的就是共有主干。"""
    head = {"db.get_project", "db.list_projects", "proj.get_tactic_prompt_suffix"}
    return [n for n in names if n not in head]


# ══════════════════════════════════════════════════════════════════════
# 1 · 两条路径的主干必须一致
# ══════════════════════════════════════════════════════════════════════

def test_quick_runs_the_shared_pipeline_in_order(monkeypatch):
    rec, st = _run_quick(monkeypatch)
    assert _tail(rec.names()) == SHARED_PIPELINE, rec.names()
    assert st["phase"] == "done" and st["done"] is True
    assert st["errors"] == []


def test_queue_runs_the_shared_pipeline_in_order(monkeypatch):
    rec, st = _run_queue(monkeypatch)
    assert _tail(rec.names()) == SHARED_PIPELINE, rec.names()
    assert st["phase"] == "done" and st["done"] is True
    assert st["errors"] == []


def test_both_paths_agree_step_for_step(monkeypatch):
    """**这条是 SUP-012 的核心断言。**

    两条编排如果在主干上就已经不一样, 那"合并"就不是重构而是改行为。这条把
    "它们确实是同一条流水线"变成可执行的事实, 而不是读代码读出来的印象。
    """
    q_rec, _ = _run_quick(monkeypatch)
    k_rec, _ = _run_queue(monkeypatch)
    assert _tail(q_rec.names()) == _tail(k_rec.names())


# ══════════════════════════════════════════════════════════════════════
# 2 · 差异只有这五处
# ══════════════════════════════════════════════════════════════════════

def test_the_only_differences_are_these_five(monkeypatch):
    """把两条路径**全部**的差异逐条钉住。

    合并的时候每一条都要有明确去向 —— 而不是"合完了跑跑看"。
    """
    q_rec, q_st = _run_quick(monkeypatch)
    k_rec, k_st = _run_queue(monkeypatch)

    # ① 取项目的方式: quick 逐个查, queue 批量预取(SUP-004 同款的 N+1 处理)
    assert "db.get_project" in q_rec.names() and "db.list_projects" not in q_rec.names()
    assert "db.list_projects" in k_rec.names() and "db.get_project" not in k_rec.names()

    # ② 战术后缀的取用时机不同(与记忆读取之间没有数据依赖, 纯顺序差异)
    q, k = q_rec.names(), k_rec.names()
    assert q.index("proj.get_tactic_prompt_suffix") > q.index("db.get_confirmed_memories")
    assert k.index("proj.get_tactic_prompt_suffix") < k.index("db.get_confirmed_memories")

    # ③ 错误前缀: queue 要标出是第几个计划, quick 只有一个批次不需要
    assert q_rec.info("_save_batch_results") == [""]
    assert k_rec.info("_save_batch_results")[0].startswith("计划 1")

    # ④ status 的键不同 —— UI 那两个面板读的不是同一组
    assert {"batch_id", "saved_count", "n_results"} <= set(q_st)
    assert {"total", "current", "completed"} <= set(k_st)
    assert k_st["completed"] == [{"plan_idx": 0, "batch_id": "batch-1",
                                  "project_name": "测试项目", "saved": 1}]

    # ⑤ metrics 的 mode 不同(同一张表靠它区分两种模式)
    assert q_st["last_metrics"]["mode"] == "quick"
    assert k_st["last_metrics"]["mode"] == "queue"


# ══════════════════════════════════════════════════════════════════════
# 3 · queue 独有的跨 plan 行为 —— 合并时最容易弄丢的部分
# ══════════════════════════════════════════════════════════════════════

def test_same_project_plans_reuse_the_caches(monkeypatch):
    """同一项目的第二个 plan **不该**再查一遍记忆 / 正负例 / 会话指令。

    这是队列相对快速生成唯一实打实的性能优势(4 次往返 × 每个 plan)。合并时
    如果把缓存丢了, 功能全对、只是慢 —— 而"慢"没有任何断言会红。
    """
    rec, _ = _run_queue(monkeypatch, plans=[PLAN, dict(PLAN, tactic="战术B")])
    n = rec.names()
    assert n.count("db.get_confirmed_memories") == 1, n
    assert n.count("db.list_example_items") == 2, n        # positive + negative, 只一轮
    assert n.count("db.get_session_instructions") == 1, n
    # 但每个 plan 都要各自建批次、各自生成
    assert n.count("db.create_batch") == 2
    assert n.count("gen.generate_batch") == 2


def test_different_projects_do_not_share_caches(monkeypatch):
    """缓存必须**按项目分桶** —— 串了就是把 A 项目的硬规则喂给 B 项目。"""
    rec, _ = _run_queue(monkeypatch,
                        plans=[PLAN, dict(PLAN, project_id="proj-2",
                                          project_name="第二个")],
                        project_ids=("proj-1", "proj-2"))
    n = rec.names()
    assert n.count("db.get_confirmed_memories") == 2, n
    assert n.count("db.get_session_instructions") == 2, n


def test_projects_are_prefetched_once_not_per_plan(monkeypatch):
    """预取只发一次 —— 这是 queue 相对 quick 的另一处 N+1 处理。"""
    rec, _ = _run_queue(monkeypatch, plans=[PLAN] * 3)
    assert rec.names().count("db.list_projects") == 1


def test_stop_event_halts_between_plans(monkeypatch):
    """停止是**在两个 plan 之间**生效, 不是硬切断正在跑的那一个。

    worker 的 SIGTERM 优雅退出靠的就是这个语义(见 worker.py 的 handler)。
    """
    import generation_service as gs
    ev = threading.Event()
    ev.set()
    with patched(monkeypatch) as rec:
        st = fresh_status()
        gs.queue_worker_impl([dict(PLAN), dict(PLAN)], "u1", object(), st, ev)
    assert "gen.generate_batch" not in rec.names(), rec.names()
    assert st["phase"] == "done"


# ══════════════════════════════════════════════════════════════════════
# 4 · 两条路径的失败口径也要一致
# ══════════════════════════════════════════════════════════════════════

def test_quick_records_failure_without_crashing(monkeypatch):
    import generation_service as gs
    with patched(monkeypatch) as rec:
        monkeypatch.setattr(gs.gen_module, "generate_batch",
                            lambda **k: (_ for _ in ()).throw(RuntimeError("引擎挂了")))
        st = fresh_status()
        gs.quick_gen_worker(dict(PLAN), "u1", object(), st)
    assert any("引擎挂了" in e for e in st["errors"]), st["errors"]
    assert st["phase"] == "done" and st["done"] is True


def test_queue_keeps_going_after_one_plan_fails(monkeypatch):
    """一个 plan 挂了不能拖垮整队 —— 这是队列模式存在的理由之一。"""
    import generation_service as gs
    calls = {"n": 0}

    def _flaky(**k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("第一个挂了")
        return []

    with patched(monkeypatch):
        monkeypatch.setattr(gs.gen_module, "generate_batch", _flaky)
        st = fresh_status()
        gs.queue_worker_impl([dict(PLAN), dict(PLAN)], "u1", object(),
                             st, threading.Event())
    assert calls["n"] == 2, "第一个 plan 失败之后没有继续跑第二个"
    assert any("第一个挂了" in e for e in st["errors"]), st["errors"]
    assert st["phase"] == "done"


def test_status_always_lands_on_done(monkeypatch):
    """无论怎么炸, phase 都要回到 done。

    卡在 running 的后果很具体: UI 的启动/停止按钮全 disabled, 用户没有逃生口
    (``_queue_worker`` 那个 finally shim 就是为此存在的)。
    """
    import generation_service as gs
    for runner, args in (
        (gs.quick_gen_worker, (dict(PLAN), "u1", object())),
        (gs.queue_worker, ([dict(PLAN)], "u1", object())),
    ):
        with patched(monkeypatch):
            monkeypatch.setattr(gs.db, "get_project",
                                lambda *a, **k: (_ for _ in ()).throw(SystemExit(1)))
            monkeypatch.setattr(gs.db, "list_projects",
                                lambda *a, **k: (_ for _ in ()).throw(SystemExit(1)))
            st = fresh_status()
            extra = (threading.Event(),) if runner is gs.queue_worker else ()
            try:
                runner(*args, st, *extra)
            except SystemExit:
                pass
            assert st["phase"] == "done", (runner.__name__, st["phase"])
            assert st["done"] is True and st["running"] is False
