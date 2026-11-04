"""改稿的后台规则复查(AU-F07, 2026-11-04)。

带 replaces_version_id 改过重存的那几版不进 check_rules_version_ids(改一轮就够), 编译好的语义规则就一直没查过它们
(审计 11-02 F07)。W-42 之后合规问题由 AI 自己改, 改稿会变多: 存完档在后台对这几版跑一次 check_rules, 摘成只有数和
规则 id 的一小块, 并进这次存档那一行 batch_metrics 的 meta.revision_rule_recheck。钉的是:

  · 只有真改过重存的版本才排, 只查这几版; 新稿不排(它们照旧由模型调 check_rules);
  · 结果只进那一行指标的 meta, 原来的 meta 一个键不少; 回包里没有, 写手看不到;
  · 只留数和规则 id, 不留稿子、规则原文、Jev 指出的句子;
  · check_rules 一次查不完的接着查; 它抛了也只记一笔 error, 不冒泡; 写回失败只记日志;
  · 池子关着 / 排满了 / 指标那一行没插成 就不跑。

所有稿子、规则都是合成的示例。
"""

from __future__ import annotations

import concurrent.futures as cf
import threading

import pytest

from deskcore import core, store
from tests.test_check_rules import (R_HARD, R_SOFT, V1, full_client, kicks, wired)  # noqa: F401 — 夹具
from tests.test_check_rules import ME as RULES_ME, PROJ as RULES_PROJ
from tests.test_deskcore_commit_judge import BODY_A, BODY_A2, BODY_B, ME, PROJ, _client, _embed
from tests.test_rule_compile import _fresh_compile_state, jev  # noqa: F401 — 夹具


class _InlinePool:
    """投进来就地跑完, 回一个已经完成的 Future(和真池子一样, 异常放进 Future)。"""

    def __init__(self):
        self.submitted: list[tuple] = []

    def submit(self, fn, *args):
        self.submitted.append(args)
        fut: cf.Future = cf.Future()
        try:
            fut.set_result(fn(*args))
        except BaseException as exc:                    # noqa: BLE001
            fut.set_exception(exc)
        return fut


@pytest.fixture
def inline_pool():
    pool = _InlinePool()
    core._REVISION_RECHECK_POOL[0] = pool              # conftest 的 _revision_recheck_off 用完换回去
    return pool


CANNED = {"status": "checked", "uncompiled_rules": 1,
          "summary": {"versions": 1, "must_fix": 1, "suggest": 1, "unsure": 0, "not_finished": 0, "unjudged": 0},
          "versions": [{"version_id": "v-new", "title": "示例标题", "judge_status": "ok",
                        "violations": [{"rule_id": "r-hard", "severity": "hard", "source": "jev", "p": 0.9,
                                        "what": "必须改: 示例", "evidence": "示例产品能治好所有问题"},
                                       {"rule_id": "r-soft", "severity": "soft", "source": "code",
                                        "what": "建议改: 示例"}],
                        "unsure": [], "counts": {"must_fix": 1, "suggest": 1, "unsure": 0, "unjudged": 0}}],
          "next_step": "「必须改」的照 what / evidence 改完"}


def _metrics(c) -> list[dict]:
    return c.rows.get("batch_metrics") or []


# ══════════════════════════════════════════════════════════════════════
# 1 · 接在 commit_drafts 后面
# ══════════════════════════════════════════════════════════════════════

def test_only_the_revised_versions_are_rechecked_and_the_result_stays_in_the_metrics_row(inline_pool, monkeypatch):
    calls: list[dict] = []

    def fake_check_rules(client, project_id, version_ids, *, user_id=None, tactic=""):
        calls.append({"project_id": project_id, "version_ids": list(version_ids), "user_id": user_id, "tactic": tactic})
        return dict(CANNED)
    monkeypatch.setattr(core, "check_rules", fake_check_rules)

    c = _client()
    first = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A}], user_id=ME)
    assert calls == [] and inline_pool.submitted == [], "新稿不排: 它们照旧由模型调 check_rules"
    assert "revision_rule_recheck" not in _metrics(c)[0]["meta"]

    v1 = first["version_ids"][0]
    _embed(c)
    out = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A2, "replaces_version_id": v1},
                                       {"title": "绩点二点八", "body": BODY_B}], user_id=ME, tactic="示例战术")
    assert out["written"] == 2 and len(out["replaced"]) == 1
    v2 = out["replaced"][0]["version_id"]
    assert out["check_rules_version_ids"] == [v for v in out["version_ids"] if v != v2], "写手那边的口径不变"
    assert calls == [{"project_id": PROJ, "version_ids": [v2], "user_id": ME, "tactic": "示例战术"}], \
        "只查改过重存的那一版, 同一个调用者、同一个 tactic"

    row = _metrics(c)[-1]
    block = row["meta"]["revision_rule_recheck"]
    assert block["status"] == "checked" and block["revised"] == 1
    assert block["must_fix"] == 1 and block["suggest"] == 1 and block["uncompiled_rules"] == 1
    assert block["versions"] == [{"version_id": "v-new", "judge_status": "ok", "must_fix": 1, "suggest": 1,
                                  "unsure": 0, "rule_ids": ["r-hard", "r-soft"]}]
    assert isinstance(block["elapsed_ms"], int) and block["at"]
    # 原来那份 meta 一个键不少
    assert row["meta"]["mode"] == "deskcore_commit" and row["meta"]["batch_id"] == out["batch_id"]
    assert "revised_item_ids" in row["meta"]
    # 不留稿子、规则原文、Jev 指出的句子
    text = repr(block)
    for leak in ("示例产品能治好所有问题", "必须改", "示例标题", BODY_A2[:20]):
        assert leak not in text, leak
    # 回包里没有, 写手看不到
    assert "revision_rule_recheck" not in repr(out)


def test_nothing_is_scheduled_when_the_pool_is_off_or_the_metrics_row_is_missing(monkeypatch):
    called: list = []
    monkeypatch.setattr(core, "check_rules", lambda *a, **k: called.append(a) or dict(CANNED))
    out = {"replaced": [{"index": 0, "version_id": "v-new"}]}
    assert core._REVISION_RECHECK_POOL[0] is None, "conftest 默认关着"
    assert core._schedule_revision_recheck(None, PROJ, ME, out, "", "row-1", {}) is False
    pool = _InlinePool()
    core._REVISION_RECHECK_POOL[0] = pool
    assert core._schedule_revision_recheck(None, PROJ, ME, out, "", None, {}) is False, "指标那一行没插成就不跑"
    assert core._schedule_revision_recheck(None, PROJ, ME, {"replaced": []}, "", "row-1", {}) is False
    assert pool.submitted == [] and called == []


def test_a_full_queue_skips_this_recheck(monkeypatch):
    class _Busy:
        def submit(self, fn, *args):                    # pragma: no cover — 排不上就不会走到这里
            raise AssertionError("排满了还往池子里投")
    core._REVISION_RECHECK_POOL[0] = _Busy()
    monkeypatch.setattr(core, "_REVISION_RECHECK_SLOTS", threading.BoundedSemaphore(1))
    assert core._REVISION_RECHECK_SLOTS.acquire(blocking=False)
    out = {"replaced": [{"index": 0, "version_id": "v-new"}]}
    assert core._schedule_revision_recheck(None, PROJ, ME, out, "", "row-1", {}) is False


def test_a_failing_metrics_insert_does_not_schedule_and_does_not_break_the_archive(inline_pool, monkeypatch):
    monkeypatch.setattr(core, "check_rules", lambda *a, **k: dict(CANNED))

    def boom(*a, **k):
        raise RuntimeError("batch_metrics 写不进去")
    c = _client()
    first = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A}], user_id=ME)
    monkeypatch.setattr(store, "insert_commit_metrics", boom)
    _embed(c)
    out = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A2,
                                        "replaces_version_id": first["version_ids"][0]}], user_id=ME)
    assert out["written"] == 1 and inline_pool.submitted == []


# ══════════════════════════════════════════════════════════════════════
# 2 · 后台那一段本身
# ══════════════════════════════════════════════════════════════════════

def _row_client():
    from tests.fakes import FakeClient
    return FakeClient(rows={"batch_metrics": [{"id": "row-1", "meta": {"mode": "deskcore_commit", "x": 1}}]})


def test_check_rules_errors_are_recorded_not_raised(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("规则池读不出来")
    monkeypatch.setattr(core, "check_rules", boom)
    c = _row_client()
    block = core._revision_rule_recheck(c, PROJ, ME, ["v-new"], "", "row-1", {"mode": "deskcore_commit", "x": 1})
    assert block["status"] == "error" and "规则池读不出来" in block["error"] and block["revised"] == 1
    meta = c.rows["batch_metrics"][0]["meta"]
    assert meta["x"] == 1 and meta["revision_rule_recheck"]["status"] == "error"


def test_a_failing_write_back_is_only_logged(monkeypatch):
    monkeypatch.setattr(core, "check_rules", lambda *a, **k: dict(CANNED))

    def boom(*a, **k):
        raise RuntimeError("写不回去")
    monkeypatch.setattr(store, "update_commit_metrics_meta", boom)
    block = core._revision_rule_recheck(_row_client(), PROJ, ME, ["v-new"], "", "row-1", {})
    assert block["status"] == "checked"


def test_versions_left_over_are_checked_in_the_next_call(monkeypatch):
    seen: list[list[str]] = []

    def paged(client, project_id, version_ids, *, user_id=None, tactic=""):
        seen.append(list(version_ids))
        res = {"status": "checked", "uncompiled_rules": 0,
               "summary": {"versions": 1, "must_fix": 0, "suggest": 1, "unsure": 0, "not_finished": 0, "unjudged": 0},
               "versions": [{"version_id": version_ids[0], "judge_status": "ok", "violations": [],
                             "counts": {"must_fix": 0, "suggest": 1, "unsure": 0, "unjudged": 0}}]}
        if len(version_ids) > 1:
            res["remaining_version_ids"] = list(version_ids[1:])
        return res
    monkeypatch.setattr(core, "check_rules", paged)
    block = core._revision_rule_recheck(_row_client(), PROJ, ME, ["a", "b", "c"], "", "row-1", {})
    assert seen == [["a", "b", "c"], ["b", "c"], ["c"]]
    assert block["suggest"] == 3 and [v["version_id"] for v in block["versions"]] == ["a", "b", "c"]


def test_real_check_rules_on_a_revised_version_is_digested_without_text(wired):    # noqa: F811
    """用 test_check_rules 那一套合成规则真跑一次 check_rules: 必须改 / 建议改的条数和规则 id 进 meta, 证据句不进。"""
    c = full_client()
    c.rows.setdefault("batch_metrics", []).append({"id": "row-9", "meta": {"mode": "deskcore_commit"}})
    block = core._revision_rule_recheck(c, RULES_PROJ, RULES_ME, [V1], "", "row-9", {"mode": "deskcore_commit"})
    assert block["status"] == "checked" and block["must_fix"] >= 1
    (v,) = block["versions"]
    assert v["version_id"] == V1 and R_HARD["id"] in v["rule_ids"] and R_SOFT["id"] in v["rule_ids"]
    assert "能治好所有问题" not in repr(block) and "不能写疗效" not in repr(block)
    assert c.rows["batch_metrics"][0]["meta"]["revision_rule_recheck"] == block
    assert any(x["url"].endswith("/judge_draft") for x in wired.calls), "同 check_rules: 规则题照样发给 Jev"
