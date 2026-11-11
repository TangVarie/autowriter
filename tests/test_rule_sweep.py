"""规则检查的后台补查(2026-11-11, 审计 S3 / JP-3)。

新稿的规则检查靠模型在 score_drafts 之后多调一次 check_rules; 它没调的那些稿子账本里就没有规则题的答案, 拿不来校准。
每次存完档在改稿复查的同一个池子上、排在它后面, 给这个项目还没有答案的旧稿补查一轮。钉的是:

  · 真存进去了才排, 排在改稿复查后面; 同一个调用者、同一个 client; 不进回包、不写 batch_metrics、next_step 一字不动;
  · 只看近 21 天、建了超过 3 分钟、每篇交出去的那一版; 补录副本、别的项目、账本里已经有 shadow-rules 答案的不查
    (只有 primary 答案的照查);
  · 旧的在前, 一次最多 20 版, 多的留给下一次; tactic 传空(旧稿那一场的没存, 不拿这一场的去判);
  · 进程里记着: 已经有答案的不再问账本; 查过一次的 6 小时内不再查(不然旧的在前会一直卡在查了也没有行的那几版上);
  · 同一个项目同时只排一个; 没配判定服务 / RULE_SWEEP_AUTO=0 / 池子关着 什么都不做; 怎么失败都不抛。

所有稿子、规则都是合成的示例。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import pytest

import config
from deskcore import core, store
from tests.fakes import FakeClient
from tests.test_check_rules import ME as RULES_ME, PROJ as RULES_PROJ
from tests.test_check_rules import V1, V2, full_client, kicks, version, wired  # noqa: F401 — 夹具
from tests.test_deskcore_commit_judge import BODY_A, BODY_A2, BODY_B, ME, PROJ, _client, _embed
from tests.test_revision_recheck import CANNED, _InlinePool, inline_pool  # noqa: F401 — 夹具
from tests.test_rule_compile import _fresh_compile_state, jev  # noqa: F401 — 夹具

OTHER_PROJ = "bbbbbbbb-0000-4000-8000-0000000000b2"
NFA = "truth_vault.note_feature_answers"
NOW = datetime.now(timezone.utc)


def vid(n: int) -> str:
    return f"cccccccc-0000-4000-8000-{n:012x}"


def _ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat()


def ver(n: int, *, days: float = 2, minutes: float = 0, project_id: str = PROJ, item: str | None = None,
        best: int | None = -1, num: int = 1, source: str = "deskcore") -> dict:
    """一版稿子(假库没有真 join: item / batch 直接嵌在版本行里)。``best``: -1 = 这一版就是交出去的那版。"""
    return {"id": vid(n), "item_id": item or f"item-{n}", "version_num": num,
            "created_at": _ago(days=days, minutes=minutes),
            "items": {"best_version_id": vid(n) if best == -1 else (vid(best) if best is not None else None),
                      "batches": {"project_id": project_id, "params": {"source": source}}}}


def answer(n: int, *, run_tag: str = "shadow-rules", qid: str = "q1", subject_type: str = "aw_version") -> dict:
    return {"subject_type": subject_type, "subject_id": vid(n), "run_tag": run_tag, "question_id": qid,
            "question_version": 1, "extractor": "jev:example", "answer": "否"}


def world(versions=(), answers=()) -> FakeClient:
    return FakeClient(rows={"projects": [{"id": PROJ, "name": "示例项目", "owner_id": ME}],
                            "versions": list(versions), NFA: list(answers)})


@pytest.fixture(autouse=True)
def _fresh_sweep_state(monkeypatch):
    monkeypatch.setattr(core, "_RULE_SWEEP_ACTIVE", set())
    monkeypatch.setattr(core, "_RULE_SWEEP_SEEN", {})
    monkeypatch.setattr(config, "RULE_SWEEP_AUTO", True)


@pytest.fixture
def checks(jev, monkeypatch):                          # noqa: F811 — jev: 判定服务算配上了
    """check_rules 换成录音机: 每版回一行 judge_status=ok。"""
    calls: list[dict] = []

    def fake(client, project_id, version_ids, *, user_id=None, tactic=""):
        calls.append({"client": client, "project_id": project_id, "version_ids": list(version_ids),
                      "user_id": user_id, "tactic": tactic})
        return {"status": "checked", "versions": [{"version_id": v, "judge_status": "ok"} for v in version_ids]}
    monkeypatch.setattr(core, "check_rules", fake)
    return calls


# ══════════════════════════════════════════════════════════════════════
# 1 · 两次读
# ══════════════════════════════════════════════════════════════════════

def test_listing_keeps_one_delivered_version_per_draft_oldest_first():
    c = world([
        ver(1, days=5),
        ver(2, days=9),
        ver(3, days=1, item="item-x", best=4, num=1),            # 改过: 旧版不是交出去的那版
        ver(4, days=0.5, item="item-x", best=4, num=2),
        ver(5, days=3, item="item-y", best=None, num=1),         # best 没指: 这段时间里最新那一版
        ver(6, days=2, item="item-y", best=None, num=2),
        ver(7, days=4, source="ingest"),                         # 补录副本
        ver(8, days=4, project_id=OTHER_PROJ),                   # 别的项目
        ver(9, days=30),                                         # 窗口外
        ver(10, days=0, minutes=1),                              # 刚存的
        ver(11, days=6, item="item-z", best=12),                 # 交出去的那版刚改完, 还不到 3 分钟
        ver(12, days=0, minutes=1, item="item-z", best=12, num=2),
    ])
    rows = store.delivered_versions_between(c, PROJ, NOW - timedelta(days=21), NOW - timedelta(minutes=3))
    assert [r["id"] for r in rows] == [vid(2), vid(1), vid(6), vid(4)], "旧的在前"
    assert set(rows[0]) == {"id", "created_at"}
    (call,) = [x for x in c.calls if x["table"] == "versions"][:1]
    assert "title" not in call["cols"] and "body" not in call["cols"], "不取标题正文"
    assert ("eq", "items.batches.project_id", PROJ) in call["filters"]


def test_only_shadow_rules_answers_on_desk_versions_count():
    c = world(answers=[answer(1), answer(1, qid="q2"), answer(2, run_tag="primary"),
                       answer(3, subject_type="note"), answer(5)])
    got = store.versions_with_answers(c, [vid(1), vid(2), vid(3), vid(4)], run_tag="shadow-rules")
    assert got == {vid(1)}, "存档那次的 primary 答案不算查过规则; 没问的版本不回"
    assert store.versions_with_answers(c, [], run_tag="shadow-rules") == set()
    assert [x["table"] for x in c.calls] == [NFA, NFA], "一批一页 + 一次空页"
    assert c.calls[0]["cols"] == "subject_id", "只取 subject_id, 不取答案和 evidence"


def test_answers_are_asked_in_small_chunks_and_paged():
    ids = [vid(i) for i in range(1, 46)]
    c = world(answers=[answer(i, qid=f"q{k}") for i in range(1, 46, 2) for k in range(3)])
    c.max_rows = 7                                               # 服务端钳短: 每页都是短页
    got = store.versions_with_answers(c, ids, run_tag="shadow-rules")
    assert got == {vid(i) for i in range(1, 46, 2)}
    chunks = {tuple(f[2]) for x in c.calls for f in x["filters"] if f[0] == "in"}
    assert sorted(len(ch) for ch in chunks) == [5, 20, 20]


# ══════════════════════════════════════════════════════════════════════
# 2 · 后台那一段本身
# ══════════════════════════════════════════════════════════════════════

def test_unanswered_old_versions_are_checked_oldest_first(checks, caplog):
    c = world([ver(1, days=2), ver(2, days=9), ver(3, days=5), ver(4, days=7), ver(5, days=0, minutes=1)],
              [answer(4), answer(2, run_tag="primary")])
    with caplog.at_level(logging.INFO, logger="deskcore.core"):
        out = core._rule_sweep(c, PROJ, ME)
    assert out == {"status": "checked", "candidates": 4, "answered": 1, "checked": 3, "judged": 3, "skipped": 0}
    assert [(x["version_ids"], x["tactic"]) for x in checks] == [([vid(2), vid(3), vid(1)], "")], "一次调用, 旧的在前"
    assert all(x["client"] is c and x["user_id"] == ME and x["project_id"] == PROJ for x in checks)
    assert "batch_metrics" not in c.rows and not [x for x in c.calls if x["op"] != "select"], "只读, 什么都不写"
    lines = [r.getMessage() for r in caplog.records if "规则补查" in r.getMessage()]
    assert len(lines) == 1 and "查了 3 版" in lines[0] and "跳过 0 版" in lines[0], lines
    assert caplog.records[-1].levelno == logging.INFO


def test_at_most_twenty_per_run_and_the_rest_wait_for_the_next_save(checks, monkeypatch):
    c = world([ver(i, days=20 - i * 0.5) for i in range(1, 31)])
    out = core._rule_sweep(c, PROJ, ME)
    assert (out["checked"], out["skipped"]) == (20, 10)
    assert checks[0]["version_ids"] == [vid(i) for i in range(1, 21)], "最旧的 20 版"
    # 下一次存档: 上一轮查的在这个进程里 6 小时内不再查(假库里它们还是没有行), 轮到后面 10 版
    out = core._rule_sweep(c, PROJ, ME)
    assert (out["checked"], out["skipped"]) == (10, 20)
    assert checks[1]["version_ids"] == [vid(i) for i in range(21, 31)], "不会卡在查了也没有行的那 20 版上"
    out = core._rule_sweep(c, PROJ, ME)
    assert out["status"] == "nothing_to_check" and (out["checked"], out["skipped"]) == (0, 30) and len(checks) == 2
    # 6 小时之后: 还是没有行的再查一次
    later = core.time.monotonic() + core.RULE_SWEEP_RETRY_AFTER_SEC + 1

    class _Later:
        def __getattr__(self, name):
            return getattr(__import__("time"), name)

        @staticmethod
        def monotonic() -> float:
            return later
    monkeypatch.setattr(core, "time", _Later())
    out = core._rule_sweep(c, PROJ, ME)
    assert out["checked"] == 20 and checks[2]["version_ids"] == [vid(i) for i in range(1, 21)]


def test_versions_known_to_have_answers_are_not_asked_again(checks):
    c = world([ver(1), ver(2, days=3)], [answer(1), answer(2)])
    assert core._rule_sweep(c, PROJ, ME)["answered"] == 2 and checks == []
    asked = len([x for x in c.calls if x["table"] == NFA])
    out = core._rule_sweep(c, PROJ, ME)
    assert out["answered"] == 2 and out["status"] == "nothing_to_check"
    assert len([x for x in c.calls if x["table"] == NFA]) == asked, "有答案的记在进程里, 不再问账本"


def test_the_sweep_passes_the_ownership_gate_before_it_reads_anything(checks, caplog):
    """排它的 commit_drafts 过过闸; 后台这一段在另一个线程、晚一会儿才跑, 读版本之前自己再过一次。"""
    c = world([ver(1)])
    with caplog.at_level(logging.WARNING, logger="deskcore.core"):
        out = core._rule_sweep(c, PROJ, "22222222-2222-2222-2222-222222222222")       # 不是这个项目的 owner
    assert out["status"] == "error" and checks == []
    assert [x["table"] for x in c.calls] == ["projects"], "没过闸: 版本和账本一行都没读"


def test_checked_counts_only_what_check_rules_really_looked_at(jev, monkeypatch):    # noqa: F811
    """这个项目这会儿没有能查的规则(check_rules 回 nothing_to_check, 一版都不列): 不算查了, 记进跳过。"""
    monkeypatch.setattr(core, "check_rules", lambda *a, **k: {"status": "nothing_to_check", "versions": []})
    out = core._rule_sweep(world([ver(1), ver(2)]), PROJ, ME)
    assert (out["checked"], out["judged"], out["skipped"]) == (0, 0, 2)


def test_versions_over_the_check_rules_limit_are_passed_on(jev, monkeypatch):        # noqa: F811
    monkeypatch.setattr(core, "RULE_SWEEP_MAX_VERSIONS", 5)
    seen: list[list[str]] = []

    def paged(client, project_id, version_ids, *, user_id=None, tactic=""):
        seen.append(list(version_ids))
        res = {"status": "checked", "versions": [{"version_id": v, "judge_status": "timeout"} for v in version_ids[:2]]}
        if len(version_ids) > 2:
            res["remaining_version_ids"] = list(version_ids[2:])
        return res
    monkeypatch.setattr(core, "check_rules", paged)
    out = core._rule_sweep(world([ver(i, days=10 - i) for i in range(1, 6)]), PROJ, ME)
    assert [len(x) for x in seen] == [5, 3, 1]
    assert (out["checked"], out["judged"], out["skipped"]) == (5, 0, 0), "没判回来的不算 judged"


@pytest.mark.parametrize("where", ["listing", "ledger", "check_rules"])
def test_any_failure_is_logged_never_raised(jev, monkeypatch, caplog, where):         # noqa: F811
    def boom(*a, **k):
        raise RuntimeError("示例故障")
    if where == "listing":
        monkeypatch.setattr(store, "delivered_versions_between", boom)
    elif where == "ledger":
        monkeypatch.setattr(store, "versions_with_answers", boom)
    else:
        monkeypatch.setattr(core, "check_rules", boom)
    with caplog.at_level(logging.WARNING, logger="deskcore.core"):
        out = core._rule_sweep(world([ver(1)]), PROJ, ME)
    assert out["status"] == "error" and out["checked"] == 0
    assert any("规则补查没跑完" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("off", ["env", "judge"])
def test_off_or_not_configured_reads_nothing(checks, monkeypatch, off):
    if off == "env":
        monkeypatch.setattr(config, "RULE_SWEEP_AUTO", False)
    else:
        monkeypatch.setattr(config, "JUDGE_URL", "")
    c = world([ver(1)])
    assert core._rule_sweep(c, PROJ, ME)["status"] == "off"
    assert c.calls == [] and checks == []
    pool = _InlinePool()
    core._REVISION_RECHECK_POOL[0] = pool
    assert core._schedule_rule_sweep(c, PROJ, ME, {"written": 1}) is False and pool.submitted == []


def test_the_switch_is_on_by_default():
    import importlib
    import os
    saved = os.environ.pop("RULE_SWEEP_AUTO", None)
    try:
        assert importlib.reload(config).RULE_SWEEP_AUTO is True
        os.environ["RULE_SWEEP_AUTO"] = "0"
        assert importlib.reload(config).RULE_SWEEP_AUTO is False
    finally:
        os.environ.pop("RULE_SWEEP_AUTO", None)
        if saved is not None:
            os.environ["RULE_SWEEP_AUTO"] = saved
        importlib.reload(config)


# ══════════════════════════════════════════════════════════════════════
# 3 · 排队
# ══════════════════════════════════════════════════════════════════════

class _HoldingPool:
    """投进来先不跑(像真池子里排着队), ``run_all`` 时按投的顺序跑。"""

    def __init__(self):
        self.jobs: list[tuple] = []

    def submit(self, fn, *args):
        self.jobs.append((fn, args))
        import concurrent.futures as cf
        return cf.Future()

    def run_all(self):
        jobs, self.jobs = self.jobs, []
        return [fn(*args) for fn, args in jobs]


def test_one_sweep_per_project_at_a_time(checks):
    pool = _HoldingPool()
    core._REVISION_RECHECK_POOL[0] = pool
    c = world([ver(1)])
    assert core._schedule_rule_sweep(c, PROJ, ME, {"written": 2}) is True
    assert core._schedule_rule_sweep(c, PROJ, ME, {"written": 2}) is False, "已经有一个在排"
    assert core._schedule_rule_sweep(c, OTHER_PROJ, ME, {"written": 1}) is True, "别的项目不受影响"
    assert len(pool.jobs) == 2
    pool.run_all()
    assert core._RULE_SWEEP_ACTIVE == set()
    assert core._schedule_rule_sweep(c, PROJ, ME, {"written": 2}) is True, "跑完了才能再排"


def test_nothing_is_scheduled_when_nothing_was_archived_or_the_pool_is_off(checks):
    c = world([ver(1)])
    assert core._REVISION_RECHECK_POOL[0] is None, "conftest 默认关着"
    assert core._schedule_rule_sweep(c, PROJ, ME, {"written": 3}) is False
    pool = _InlinePool()
    core._REVISION_RECHECK_POOL[0] = pool
    assert core._schedule_rule_sweep(c, PROJ, ME, {"written": 0}) is False
    assert core._schedule_rule_sweep(c, PROJ, ME, {}) is False
    assert pool.submitted == [] and checks == [] and core._RULE_SWEEP_ACTIVE == set()


def test_a_pool_that_refuses_the_job_frees_the_project_and_does_not_raise(checks):
    class _Closed:
        def submit(self, fn, *args):
            raise RuntimeError("cannot schedule new futures after shutdown")
    core._REVISION_RECHECK_POOL[0] = _Closed()
    assert core._schedule_rule_sweep(world(), PROJ, ME, {"written": 1}) is False
    assert core._RULE_SWEEP_ACTIVE == set()


def test_a_crashing_sweep_frees_the_project(checks, monkeypatch):
    def boom(*a, **k):
        raise KeyboardInterrupt()                      # 连 _rule_sweep 自己都兜不住的那种
    monkeypatch.setattr(core, "_rule_sweep", boom)
    core._RULE_SWEEP_ACTIVE.add(PROJ)
    with pytest.raises(KeyboardInterrupt):
        core._rule_sweep_job(world(), PROJ, ME)
    assert core._RULE_SWEEP_ACTIVE == set()


# ══════════════════════════════════════════════════════════════════════
# 4 · 接在 commit_drafts 后面
# ══════════════════════════════════════════════════════════════════════

def _age(c, minutes: float = 10) -> None:
    """把假库里已经存的版本拨成 ``minutes`` 分钟之前建的, 并把 item / batch 嵌进版本行(假库没有真 join)。"""
    items = {i["id"]: i for i in c.rows.get("items", [])}
    batches = {b["id"]: b for b in c.rows.get("batches", [])}
    for v in c.rows.get("versions", []):
        it = items.get(v.get("item_id"))
        if not it:
            continue
        b = batches.get(it.get("batch_id")) or {}
        v["created_at"] = _ago(minutes=minutes)
        v["items"] = {**(v.get("items") or {}), "id": it["id"], "best_version_id": it.get("best_version_id"),
                      "batches": {"project_id": b.get("project_id", PROJ), "params": b.get("params")}}


def test_each_archive_queues_one_sweep_behind_the_revision_recheck_and_the_reply_is_unchanged(inline_pool, checks,
                                                                                               monkeypatch):
    order: list[str] = []
    real_recheck, real_sweep = core._revision_rule_recheck, core._rule_sweep_job
    monkeypatch.setattr(core, "_revision_rule_recheck", lambda *a: order.append("recheck") or real_recheck(*a))
    monkeypatch.setattr(core, "_rule_sweep_job", lambda *a: order.append("sweep") or real_sweep(*a))

    c = _client()
    first = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A}], user_id=ME, tactic="示例战术")
    assert order == ["sweep"], "新稿存档也排一个补查"
    assert checks == [], "刚存的这一版不到 3 分钟, 留给模型自己那次 check_rules"
    v1 = first["version_ids"][0]

    # 没开补查时的回包, 拿来对: 补查不能在回包里留下任何东西
    core._REVISION_RECHECK_POOL[0] = None
    c0 = _client()
    f0 = core.commit_drafts(c0, PROJ, [{"title": "秋招等回音", "body": BODY_A}], user_id=ME, tactic="示例战术")
    _embed(c0)
    plain = core.commit_drafts(c0, PROJ, [{"title": "秋招等回音", "body": BODY_A2,
                                           "replaces_version_id": f0["version_ids"][0]},
                                          {"title": "绩点二点八", "body": BODY_B}], user_id=ME, tactic="示例战术")
    core._REVISION_RECHECK_POOL[0] = inline_pool

    _embed(c)
    order.clear()
    out = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A2, "replaces_version_id": v1},
                                       {"title": "绩点二点八", "body": BODY_B}], user_id=ME, tactic="示例战术")
    assert order == ["recheck", "sweep"], "排在改稿复查后面"
    v2 = out["replaced"][0]["version_id"]
    assert [x["version_ids"] for x in checks] == [[v2]], "改稿复查查了新那版; 这几版都不到 3 分钟, 补查一版都不碰"
    uuids = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    assert uuids.sub("<id>", out["next_step"]) == uuids.sub("<id>", plain["next_step"]), "next_step 一字不动"
    assert set(out) == set(plain), "回包的键一个不多"
    assert "sweep" not in repr(out) and "补查" not in repr(out)
    assert out["check_rules_version_ids"] == [v for v in out["version_ids"] if v != v2]
    rows = c.rows.get("batch_metrics") or []
    assert all("sweep" not in repr(r) for r in rows), "不写 batch_metrics"


def test_an_old_unchecked_draft_is_picked_up_by_the_next_archive(inline_pool, checks):
    c = _client()
    first = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A}], user_id=ME, tactic="示例战术")
    v1 = first["version_ids"][0]
    assert checks == []
    _age(c)
    n_metrics = len(c.rows.get("batch_metrics") or [])
    out = core.commit_drafts(c, PROJ, [{"title": "绩点二点八", "body": BODY_B}], user_id=ME, tactic="流量向示例")
    assert out["written"] == 1
    (call,) = checks
    assert v1 in call["version_ids"] and out["version_ids"][0] not in call["version_ids"], \
        "上一次没查的那一版补上了; 刚存的这一版留给模型自己那次 check_rules"
    assert call["user_id"] == ME and call["tactic"] == "", "这次存档的调用者; 不拿这一场的 tactic 去判旧稿"
    assert checks[0]["client"] is c, "和改稿复查一样, 用存档那次的 client"
    assert len(c.rows.get("batch_metrics") or []) == n_metrics + 1, "只有这次存档自己那一行"


def test_a_failing_sweep_never_breaks_the_archive(inline_pool, checks, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("示例故障")
    monkeypatch.setattr(store, "delivered_versions_between", boom)
    out = core.commit_drafts(_client(), PROJ, [{"title": "秋招等回音", "body": BODY_A}], user_id=ME)
    assert out["written"] == 1 and core._RULE_SWEEP_ACTIVE == set()


# ══════════════════════════════════════════════════════════════════════
# 5 · 真的 check_rules + 账本: 查过一次的, 下一次就不再花 Jev 的钱
# ══════════════════════════════════════════════════════════════════════

def test_real_check_rules_writes_the_ledger_and_the_next_sweep_spends_nothing(wired):      # noqa: F811
    """用 test_check_rules 那一套合成规则真跑: 规则题按 run_tag=shadow-rules、subject_id=版本 id 发给 Jev(假 Jev 把答案
    落进假账本), 下一次补查看见账本里有行, 一个请求都不发。"""
    def old(v: str, days: float) -> dict:
        return {**version(v), "item_id": f"item-{v[:8]}-{days}", "version_num": 1, "created_at": _ago(days=days),
                "items": {"id": f"item-{v[:8]}", "best_version_id": v,
                          "batches": {"project_id": RULES_PROJ, "params": {"source": "deskcore"}}}}
    c = full_client(versions=[old(V1, 3), old(V2, 8)])
    plain = wired.judge

    def judge_and_write(payload):
        if payload.get("write"):
            c.rows.setdefault(NFA, []).append({
                "subject_type": payload["subject_type"], "subject_id": payload["subject_id"],
                "run_tag": payload["run_tag"], "question_id": "q1", "question_version": 1,
                "extractor": "jev:example", "answer": "否"})
        return plain(payload)
    wired.judge = judge_and_write

    out = core._rule_sweep(c, RULES_PROJ, RULES_ME)
    assert (out["status"], out["checked"], out["judged"], out["skipped"]) == ("checked", 2, 2, 0)
    sent = [x["json"] for x in wired.calls if x["url"].endswith("/judge_draft")]
    assert [p["subject_id"] for p in sent] == [V2, V1] or {p["subject_id"] for p in sent} == {V1, V2}
    assert all(p["run_tag"] == "shadow-rules" and p["write"] is True and p["subject_type"] == "aw_version" for p in sent)
    assert "batch_metrics" not in c.rows, "什么都不往 batch_metrics 写"

    core._RULE_SWEEP_SEEN.clear()                      # 像进程重启了一次: 记录在账本里, 不在进程里
    n = len(wired.calls)
    out = core._rule_sweep(c, RULES_PROJ, RULES_ME)
    assert (out["status"], out["answered"], out["checked"]) == ("nothing_to_check", 2, 0)
    assert len(wired.calls) == n, "模型(或上一次补查)查过的, 这里一次 Jev 都不多花"
