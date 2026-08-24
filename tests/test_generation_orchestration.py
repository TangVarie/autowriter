"""审计 SUP-012 · 两条生成编排的**特征化测试**(characterization test)。

``_queue_worker_impl``(457 行)与 ``_quick_gen_worker``(373 行)是两份重复编排,
而且**已经漂移过一次**(原 app.py:3134 的注释自陈)。合并它们之前必须先把两条
路径**当前的行为录下来**——否则合并之后没人说得清哪些差异是本来就有的、哪些
是合并时不小心引进来的。

判据不是"结果看起来对", 而是**逐次外部调用的序列**: 调了谁、按什么顺序、
关键入参是什么。替身与录音机在 tests/genfakes.py。

── 这份录音说明了什么 ──────────────────────────────────────────────────
两条路径的主干是**逐步一致的 23 步**。差异有 7 处, 逐条钉在下面。

⚠️ **第一版我数出来是 5 处, 那是错的** —— 因为录音机记得太粗: 它只记了
``filter_soft_by_relevance`` 的 ``{n, has_context}``, 没记那段**文本本身**,
于是"两条路径拿什么做相关性过滤"的差异完全测不到。把 text / extra_instructions
原样记下来之后, 又多出两处。

教训与整个审计一贯的那条一致: **录音机记得不够细, 得到的绿是假的。**
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
    # ⚠️ 这两行在 SUP-012 抽 _persist_and_check 时**从下面挪到了这里**。
    # 挪动的理由必须写下来, 否则这份"金序列"就退化成橡皮图章 —— 谁改红了就
    # 顺手改一下, 那它就不再证明任何事情。
    #
    # 理由: validator.filter_hard 是一个**纯列表推导**(validator.py:321,
    # 只按 severity 过滤, 不改入参、不碰库), 所以它算在落库之前还是之后,
    # 语义上完全一样。它挪上来只是因为现在作为参数传给 _persist_and_check。
    # 这是这次抽取里**唯一**一处顺序变化。
    "validator.filter_hard",
    "validator.filter_hard",
    "_save_batch_results",
    "_run_semantic_dedup_pass",
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
# 2 · 差异一共 7 处, 逐条钉住
# ══════════════════════════════════════════════════════════════════════

def test_difference_1_how_the_project_is_fetched(monkeypatch):
    """quick 逐个 get_project; queue 批量 list_projects 预取(SUP-004 同款)。"""
    q_rec, _ = _run_quick(monkeypatch)
    k_rec, _ = _run_queue(monkeypatch)
    assert "db.get_project" in q_rec.names() and "db.list_projects" not in q_rec.names()
    assert "db.list_projects" in k_rec.names() and "db.get_project" not in k_rec.names()


def test_difference_2_tactic_suffix_ordering(monkeypatch):
    """取战术后缀的时机不同。与记忆读取之间没有数据依赖 —— 纯顺序差异。"""
    q = _run_quick(monkeypatch)[0].names()
    k = _run_queue(monkeypatch)[0].names()
    assert q.index("proj.get_tactic_prompt_suffix") > q.index("db.get_confirmed_memories")
    assert k.index("proj.get_tactic_prompt_suffix") < k.index("db.get_confirmed_memories")


def test_difference_3_error_prefix(monkeypatch):
    """queue 要标出是第几个计划, quick 只有一个批次不需要。"""
    q_rec, _ = _run_quick(monkeypatch)
    k_rec, _ = _run_queue(monkeypatch)
    assert q_rec.info("_save_batch_results")[0]["prefix"] == ""
    assert k_rec.info("_save_batch_results")[0]["prefix"].startswith("计划 1")


def test_difference_4_status_keys(monkeypatch):
    """UI 那两个面板读的不是同一组键。"""
    _, q_st = _run_quick(monkeypatch)
    _, k_st = _run_queue(monkeypatch)
    assert {"batch_id", "saved_count", "n_results"} <= set(q_st)
    assert {"total", "current", "completed"} <= set(k_st)
    assert k_st["completed"] == [{"plan_idx": 0, "batch_id": "batch-1",
                                  "project_name": "测试项目", "saved": 1}]


def test_difference_5_metrics_mode(monkeypatch):
    """同一张 batch_metrics 表靠 mode 区分两种模式。"""
    assert _run_quick(monkeypatch)[1]["last_metrics"]["mode"] == "quick"
    assert _run_queue(monkeypatch)[1]["last_metrics"]["mode"] == "queue"


def test_difference_6_queue_has_no_image_support(monkeypatch):
    """**queue 完全不支持图片** —— 这不是漂移, 是产品上就没做。

    队列的 plan 字典里根本没有 ``image_prompt`` / ``images`` 两个键(app.py 里
    "添加计划"构造的那份), 而 ``_queue_worker_impl`` 是显式 ``images=None``。
    quick 则会把 image_prompt 拼进 extra_instructions, 并让它参与 soft 规则的
    相关性过滤。

    合并的时候**不能想当然地"统一"** —— 给 queue 加上图片支持是产品变更,
    不是重构。这条把"现状就是这样"钉住, 免得合并时顺手改了。
    """
    plan = dict(PLAN, image_prompt="一张产品图，白底")
    q_rec, _ = _run_quick(monkeypatch, plan=plan)
    k_rec, _ = _run_queue(monkeypatch, plans=[plan])

    assert "参考图片说明" in (q_rec.info("gen.generate_batch")[0]["extra"] or "")
    assert (k_rec.info("gen.generate_batch")[0]["extra"] or "") == ""

    # 相关性过滤的上下文也跟着不同
    assert "白底" in q_rec.info("mem.filter_soft_by_relevance")[0]["text"]
    assert "白底" not in k_rec.info("mem.filter_soft_by_relevance")[0]["text"]


def test_difference_7_empty_system_prompt_diverges(monkeypatch):
    """**这一处是真正的行为分歧, 合并必须显式选边。**

    项目的 system_prompt 是空的时候:
      · queue —— 拒绝这个 plan, 写一条"项目未配置 System Prompt"的错误;
      · quick —— **照常生成**, 拿一个空的 base_prompt 去调模型。

    quick 那边等于"安静地产出一批不带项目人格的稿子"—— 正是本次审计一直在追的
    那类失败。但改掉它是**用户可见的行为变更**(现在能跑的流程会开始报错),
    所以合并时把它做成参数, 两边各自保留现状, 由人来拍板统一到哪一边。
    """
    import tests.genfakes as gf
    monkeypatch.setitem(gf.PROJECT, "system_prompt", "   ")

    q_rec, q_st = _run_quick(monkeypatch)
    assert "gen.generate_batch" in q_rec.names(), "quick 现在是照常生成的"
    assert q_st["errors"] == []

    k_rec, k_st = _run_queue(monkeypatch)
    assert "gen.generate_batch" not in k_rec.names(), "queue 现在是拒绝的"
    assert any("System Prompt" in e for e in k_st["errors"]), k_st["errors"]


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


def test_queue_shares_one_dedup_pool_across_plans(monkeypatch):
    """跨批语义去重池必须是**同一个对象**在 plan 之间传下去。

    这是队列相对快速生成最核心的能力: "4 批 × 10 篇 → 30 篇重复"就是因为每批
    只看得到 DB 里已落库的标题, 同队列前面批次的还没进去。合并时如果每个 plan
    各建一个池, 功能全对、跨批去重悄悄失效 —— 而那**不会有任何断言变红**,
    除非像这里一样把池的身份记下来。
    """
    rec, _ = _run_queue(monkeypatch, plans=[PLAN, dict(PLAN, tactic="战术B")])
    pools = [i["pool_id"] for i in rec.info("_run_semantic_dedup_pass")]
    assert len(pools) == 2 and pools[0] == pools[1], pools


def test_quick_uses_its_own_throwaway_pool(monkeypatch):
    """quick 只有一个批次, 用一个临时池 —— 但池里要**按 project_id 分桶**,
    否则 _run_semantic_dedup_pass 取不到历史那一段。"""
    rec, _ = _run_quick(monkeypatch, embeddings=True)
    info = rec.info("_run_semantic_dedup_pass")[0]
    assert info["project_id"] == "proj-1"
    assert info["pool_keys"] is not None


def test_hard_rules_come_from_both_scopes(monkeypatch):
    """硬约束校验拿的是 global + project 两份 filter_hard 的**并集**。

    只传一份的后果是安静的: 少的那半永远不校验, 而返回值看起来完全正常。
    """
    for rec in (_run_quick(monkeypatch)[0], _run_queue(monkeypatch)[0]):
        assert rec.names().count("validator.filter_hard") == 2
        assert rec.info("_run_hard_constraint_check")[0]["n_rules"] == 0


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
