"""人感题和暗题的影子判定(2026-11-15, 十三项第 7 项)。

存档那次判定发的是 judge_paras=never, 人感题和暗题在账本里一行都没有。存完档在后台对每一版另发一次只判它们的,
答案由 Jev 写账本(run_tag=shadow-human)。钉的是:

  · 请求体: 只带人感题库、逐段全判、write=true、自己的 run_tag、不要账本行;
  · 只对真存进去、有版本、正文不空的那几版发; 项目号 / 品类和存档那次判定同一套(没接 tv_project_map 就不发);
  · 写手看不到: 回包一个字不多, 参考分、next_step 和关着的时候一模一样;
  · 只有计数进那一行指标的 meta.shadow_human, Jev 回来的修改单不留; 改稿复查那一块不被盖掉;
  · 默认开, DESKCORE_SHADOW_HUMAN=0 关; 没配判定服务 / 池子关着 / 排满了就不跑; 任何错都不冒泡。

所有稿子都是合成的示例。
"""

from __future__ import annotations

import copy
import importlib

import pytest

import config
import judge_client
from deskcore import core, store
from tests.test_deskcore_commit_judge import (BODY_A, BODY_A2, BODY_B, CALLER_VID, ME, PROJ, _client, _embed,
                                              judge_on)  # noqa: F401 — 夹具
from tests.test_revision_recheck import _InlinePool

SECRET_FIX = "第 2 段有一项不公开的检查没过"


@pytest.fixture(autouse=True)
def _no_embeddings(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)


@pytest.fixture
def shadow(monkeypatch, judge_on):  # noqa: F811
    """接上 judge(存档那次的判定是录音机), 影子判定的池子换成就地跑的, judge_human_shadow 换成录音机。"""
    pool = _InlinePool()
    core._SHADOW_HUMAN_POOL[0] = pool                  # conftest 的 _shadow_human_off 用完换回去
    rec = {"payloads": [], "timeouts": [], "pool": pool,
           "reply": lambda p: {"judge_status": judge_client.JUDGE_OK, "http_status": 200, "elapsed_ms": 9, "detail": "",
                               "response": {"passed": False, "plan": [{"kind": "para", "qid": "hidden",
                                                                       "instruction": SECRET_FIX}],
                                            "calls": 4, "written": 24, "write_error": None, "rows": 24}}}

    def fake(payload, *, timeout=None):
        rec["payloads"].append(payload)
        rec["timeouts"].append(timeout)
        return rec["reply"](payload)
    monkeypatch.setattr(judge_client, "judge_human_shadow", fake)
    return rec


def _metrics(c):
    return [r for r in c.rows.get("batch_metrics") or [] if (r.get("meta") or {}).get("mode") == "deskcore_commit"]


# ── 请求体 ─────────────────────────────────────────────────────────────────────────────

def test_the_request_asks_only_the_human_bank_for_every_paragraph_and_writes_under_its_own_tag():
    p = judge_client.build_human_request(version_id="v-1", title="标题", body="正文", project="DEMO_phase1",
                                         category="教育")
    assert p == {"subject_id": "v-1", "subject_type": "aw_version", "project": "DEMO_phase1", "title": "标题",
                 "body": "正文", "banks": ["human_feel_para_v0.2"], "judge_paras": "always", "write": True,
                 "run_tag": "shadow-human", "return_rows": False, "category": "教育"}
    assert p["run_tag"] not in ("primary", judge_client.RULES_RUN_TAG, judge_client.TOOL_RUN_TAG)
    assert "category" not in judge_client.build_human_request(version_id="v", title="", body="b", project="P")
    assert "brief" not in p and "hard_rules" not in p and "project_bank" not in p


def test_the_client_call_never_raises_and_drops_ledger_rows(monkeypatch):
    monkeypatch.setattr(config, "JUDGE_URL", "https://judge.example.invalid")
    monkeypatch.setattr(config, "JUDGE_API_KEY", "jk")
    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"passed": True, "plan": [], "calls": 3, "written": 18, "ledger_rows": [{"question_id": "x"}]}

    def post(url, **kw):
        seen.update(url=url, timeout=kw["timeout"], body=kw["json"])
        return _Resp()
    monkeypatch.setattr(judge_client.requests, "post", post)
    out = judge_client.judge_human_shadow({"subject_id": "v"})
    assert out["judge_status"] == "ok" and "ledger_rows" not in out["response"]
    assert seen["url"].endswith("/judge_draft") and seen["timeout"][1] == config.SHADOW_HUMAN_TIMEOUT_SEC

    def boom(url, **kw):
        raise RuntimeError("socket closed")
    monkeypatch.setattr(judge_client.requests, "post", boom)
    assert judge_client.judge_human_shadow({"subject_id": "v"})["judge_status"] == "error"


# ── 接在 commit_drafts 后面 ────────────────────────────────────────────────────────────────

def test_every_stored_version_is_judged_in_the_background_and_only_counts_are_kept(shadow):
    c = _client()
    out = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A},
                                       {"title": "绩点二点八", "body": BODY_B},
                                       {"title": "只有标题", "body": ""}], user_id=ME)
    assert out["written"] == 2
    assert sorted(p["subject_id"] for p in shadow["payloads"]) == sorted(out["version_ids"])
    for p in shadow["payloads"]:
        assert p["project"] == "TUGE_phase1" and p["category"] == "教育", "项目号 / 品类同存档那次判定"
        assert p["banks"] == ["human_feel_para_v0.2"] and p["run_tag"] == "shadow-human" and p["write"] is True
    assert {p["body"] for p in shadow["payloads"]} == {BODY_A, BODY_B}

    block = _metrics(c)[-1]["meta"]["shadow_human"]
    assert block["status"] == "judged" and block["versions"] == 2 and block["sent"] == 2 and block["ok"] == 2
    assert block["statuses"] == {"ok": 2} and block["calls"] == 8 and block["written"] == 48
    assert block["ledger_errors"] == 0 and block["skipped"] == 0
    assert isinstance(block["elapsed_ms"], int) and block["at"]
    assert set(block) == {"status", "versions", "sent", "ok", "statuses", "calls", "written", "ledger_errors",
                          "skipped", "elapsed_ms", "at"}, "只有计数"
    # Jev 回来的修改单(含暗题那一句)哪儿都不留; 回包里没有这件事
    assert SECRET_FIX not in repr(c.rows) and SECRET_FIX not in repr(out)
    assert "shadow_human" not in repr(out) and "shadow-human" not in repr(out)
    # 原来那份 meta 一个键不少
    assert _metrics(c)[-1]["meta"]["batch_id"] == out["batch_id"]


def test_the_writer_sees_exactly_what_they_saw_with_it_off(shadow, monkeypatch):
    def run(on):
        monkeypatch.setattr(config, "SHADOW_HUMAN", on)
        return core.commit_drafts(_client(), PROJ, [{"title": "秋招等回音", "body": BODY_A}], user_id=ME)
    off = run(False)
    assert shadow["payloads"] == [], "关着就一个请求都不发"
    on = run(True)
    assert len(shadow["payloads"]) == 1

    def scrub(o):
        o = copy.deepcopy(o)
        for k in ("batch_id", "version_ids", "elapsed_ms", "phase_ms", "check_rules_version_ids", "view_url",
                  "delivery", "delivery_text"):
            o.pop(k, None)
        for r in (o.get("judge") or {}).get("results") or []:
            r.pop("version_id", None)
            r.pop("elapsed_ms", None)
        return o
    a, b = scrub(off), scrub(on)
    assert a["judge"] == b["judge"], "参考分和修改建议不受影响"
    assert set(off) == set(on), "回包的键一个不多"
    for k in set(a) - {"next_step"}:
        assert a[k] == b[k], k

    def text(o):
        t = o.get("next_step") or ""
        for vid in o["version_ids"] + [o["batch_id"]]:
            t = t.replace(str(vid), "<id>")
        return t
    assert text(off) == text(on), "念给模型的那段话一个字不差"


def test_the_default_is_on_and_only_the_env_switch_turns_it_off(monkeypatch):
    try:
        monkeypatch.delenv("DESKCORE_SHADOW_HUMAN", raising=False)
        assert importlib.reload(config).SHADOW_HUMAN is True, "不配任何环境变量就是开"
        for off in ("0", "false", "off"):
            monkeypatch.setenv("DESKCORE_SHADOW_HUMAN", off)
            assert importlib.reload(config).SHADOW_HUMAN is False
        monkeypatch.setenv("DESKCORE_SHADOW_HUMAN", "1")
        assert importlib.reload(config).SHADOW_HUMAN is True
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_nothing_runs_without_a_judge_or_with_the_pool_closed(shadow, monkeypatch):
    targets = [{"index": 0, "version_id": "v", "caller_supplied": False, "title": "t", "body": "b"}]
    assert core._schedule_shadow_human(None, PROJ, ME, [], "row", {}) is False, "没有存进去的版本"
    monkeypatch.setattr(config, "JUDGE_URL", "")
    assert core._schedule_shadow_human(None, PROJ, ME, targets, "row", {}) is False, "没配判定服务"
    monkeypatch.setattr(config, "JUDGE_URL", "https://judge.example.invalid")
    core._SHADOW_HUMAN_POOL[0] = None
    assert core._schedule_shadow_human(None, PROJ, ME, targets, "row", {}) is False, "池子关着(测试里默认)"
    assert shadow["payloads"] == []


def test_a_full_queue_skips_the_batch_and_a_dead_pool_never_breaks_the_commit(shadow, monkeypatch, caplog):
    targets = [{"index": 0, "version_id": "v", "caller_supplied": False, "title": "t", "body": "b"}]
    monkeypatch.setattr(core, "_bounded_submit", lambda *a, **k: None)
    assert core._schedule_shadow_human(None, PROJ, ME, targets, "row", {}) is False
    assert "排队已满" in caplog.text

    def boom(*a, **k):
        raise RuntimeError("cannot schedule new futures after shutdown")
    monkeypatch.setattr(core, "_bounded_submit", boom)
    out = core.commit_drafts(_client(), PROJ, [{"title": "秋招等回音", "body": BODY_A}], user_id=ME)
    assert out["written"] == 1 and "没排上" in caplog.text


def test_an_unmapped_project_sends_nothing(shadow):
    c = _client(maps=[])
    core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A}], user_id=ME)
    assert shadow["payloads"] == [], "认不出 TV 项目号: 数据出境口径下不发"
    block = _metrics(c)[-1]["meta"]["shadow_human"]
    assert block["status"] == "not_configured" and block["sent"] == 0


def test_failures_and_ledger_errors_are_counted_and_never_raise(shadow, caplog):
    replies = iter([
        {"judge_status": "policy_blocked", "http_status": 403, "elapsed_ms": 1, "detail": "policy: 示例", "response": None},
        {"judge_status": "ok", "http_status": 200, "elapsed_ms": 1, "detail": "",
         "response": {"passed": True, "plan": [], "calls": 5, "written": 0, "write_error": "upsert failed"}},
    ])
    shadow["reply"] = lambda p: next(replies)
    c = _client()
    out = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A},
                                       {"title": "绩点二点八", "body": BODY_B}], user_id=ME)
    assert out["written"] == 2
    block = _metrics(c)[-1]["meta"]["shadow_human"]
    assert block["statuses"] == {"policy_blocked": 1, "ok": 1} and block["ok"] == 1
    assert block["ledger_errors"] == 1 and block["calls"] == 5 and block["status"] == "judged"
    assert "账本没写进去" in caplog.text and "upsert failed" not in repr(block)

    def boom(p):
        raise RuntimeError("unexpected")
    shadow["reply"] = boom
    c = _client()
    out = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A}], user_id=ME)
    assert out["written"] == 1
    assert _metrics(c)[-1]["meta"]["shadow_human"]["status"] == "error"


def test_a_batch_is_capped_and_the_rest_is_counted_as_skipped(shadow, monkeypatch):
    monkeypatch.setattr(core, "SHADOW_HUMAN_MAX_VERSIONS", 1)
    c = _client()
    core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A},
                                 {"title": "绩点二点八", "body": BODY_B}], user_id=ME)
    block = _metrics(c)[-1]["meta"]["shadow_human"]
    assert len(shadow["payloads"]) == 1 and block["sent"] == 1 and block["skipped"] == 1 and block["versions"] == 2


def _bare(status, http=None):
    return {"judge_status": status, "http_status": http, "elapsed_ms": 1, "detail": "", "response": None}


def _five(c):
    """五版直接交给后台那一步(不走存档: 五篇合成稿互相太像会被查重拦掉, 这里钉的只是发不发)。"""
    targets = [{"index": i, "version_id": f"v{i}", "caller_supplied": False, "title": f"示例{i}", "body": f"正文{i}"}
               for i in range(5)]
    return core._shadow_human(c, PROJ, ME, targets, None, {})


def test_a_timeout_stops_the_batch_and_the_unsent_versions_are_counted_as_skipped(shadow):
    """Jev 慢: 一版读超时了就不再追着发剩下的(每一次被放弃的请求 Jev 那边还在逐段跑)。"""
    replies = iter([shadow["reply"](None), _bare("timeout")])
    shadow["reply"] = lambda p: next(replies)          # 第三次再要就 StopIteration → status=error, 下面的断言会挂
    block = _five(_client())
    assert len(shadow["payloads"]) == 2 and block["sent"] == 2 and block["ok"] == 1
    assert block["skipped"] == 3 and block["stopped_early"] is True and block["versions"] == 5
    assert block["statuses"] == {"ok": 1, "timeout": 1} and block["status"] == "judged"


def test_two_unavailable_in_a_row_stop_the_batch_but_one_does_not(shadow):
    ok = shadow["reply"](None)
    replies = iter([_bare("unavailable", 404), ok, _bare("unavailable", 503), _bare("unavailable", 503)])
    shadow["reply"] = lambda p: next(replies)
    block = _five(_client())
    assert len(shadow["payloads"]) == 4 and block["skipped"] == 1 and block["stopped_early"] is True
    assert block["statuses"] == {"unavailable": 3, "ok": 1}

    replies = iter([_bare("policy_blocked", 403)] * 5)  # 别的结局不停: 照旧每一版都发、都记
    shadow["reply"] = lambda p: next(replies)
    shadow["payloads"].clear()
    block = _five(_client())
    assert len(shadow["payloads"]) == 5 and block["skipped"] == 0 and "stopped_early" not in block


def test_a_timeout_on_the_last_version_is_not_an_early_stop(shadow):
    shadow["reply"] = lambda p: _bare("timeout")
    c = _client()
    core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A}], user_id=ME)
    block = _metrics(c)[-1]["meta"]["shadow_human"]
    assert block["sent"] == 1 and block["skipped"] == 0 and "stopped_early" not in block
    assert block["status"] == "none_judged"


def test_the_queue_holds_far_more_batches_than_the_revision_recheck_and_still_one_worker():
    """一批要跑几分钟、排不上的没有补判: 名额不能照抄几秒一件的改稿复查那 4 个。"""
    assert core.SHADOW_HUMAN_QUEUE >= 32
    assert core._SHADOW_HUMAN_SLOTS._initial_value == core.SHADOW_HUMAN_QUEUE
    import concurrent.futures as cf
    import threading
    gate, pool = threading.Event(), cf.ThreadPoolExecutor(max_workers=1)
    slots = threading.BoundedSemaphore(core.SHADOW_HUMAN_QUEUE)
    try:
        futs = [core._bounded_submit(pool, slots, gate.wait, 5) for _ in range(core.SHADOW_HUMAN_QUEUE + 1)]
        assert all(f is not None for f in futs[:-1]) and futs[-1] is None, "满了才跳过"
    finally:
        gate.set()
        pool.shutdown(wait=True)


def test_a_removed_bank_reads_as_unavailable_not_bad_request():
    """文档里写的口径: Jev 找不到题库回 404, 归 unavailable; 升版本而旧文件还在则没有任何报错。"""
    assert judge_client._status_for_http_error(404, "没有这个题库") == judge_client.JUDGE_UNAVAILABLE


def test_the_job_checks_access_again_before_sending(shadow, monkeypatch):
    c = _client()
    targets = [{"index": 0, "version_id": CALLER_VID, "caller_supplied": True, "title": "t", "body": "正文"}]
    block = core._shadow_human(c, PROJ, "99999999-9999-9999-9999-999999999999", targets, None, {})
    assert block["status"] == "error" and shadow["payloads"] == [], "不是这个项目的人: 一篇都不发"


def test_a_metrics_row_that_was_not_inserted_does_not_stop_the_judging(shadow, monkeypatch):
    wrote = []
    monkeypatch.setattr(store, "update_commit_metrics_meta", lambda *a, **k: wrote.append(a))
    c = _client()
    first = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A}], user_id=ME)
    shadow["payloads"].clear()
    wrote.clear()
    targets = [{"index": 0, "version_id": first["version_ids"][0], "caller_supplied": False, "title": "t",
                "body": "正文"}]
    block = core._shadow_human(c, PROJ, ME, targets, None, {})
    assert block["status"] == "judged" and len(shadow["payloads"]) == 1 and wrote == []

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(store, "update_commit_metrics_meta", boom)
    assert core._shadow_human(c, PROJ, ME, targets, "row-1", {})["status"] == "judged", "写回失败只记日志"


def test_the_revision_recheck_block_and_the_shadow_block_both_survive(shadow, monkeypatch):
    """两个后台活往同一行 meta 里各并一小块: 谁后写都带着先写的那一块。"""
    core._REVISION_RECHECK_POOL[0] = _InlinePool()      # conftest 的 _revision_recheck_off 用完换回去
    monkeypatch.setattr(core, "check_rules", lambda *a, **k: {
        "status": "checked", "uncompiled_rules": 0, "summary": {}, "versions": []})
    c = _client()
    first = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A}], user_id=ME)
    _embed(c)
    out = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A2,
                                        "replaces_version_id": first["version_ids"][0]}], user_id=ME)
    assert len(out["replaced"]) == 1
    meta = _metrics(c)[-1]["meta"]
    assert meta["revision_rule_recheck"]["status"] == "checked"
    assert meta["shadow_human"]["status"] == "judged" and meta["shadow_human"]["sent"] == 1, "改过重存的那一版也判"
    assert meta["mode"] == "deskcore_commit"
    assert shadow["payloads"][-1]["subject_id"] == out["replaced"][0]["version_id"]
