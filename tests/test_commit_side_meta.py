"""存档那一行指标(batch_metrics, meta.mode = deskcore_commit)里另外三小块(2026-11-11, docs/deskcore.md §3.8):

  · ``flywheel_lessons`` —— 这一场借卡实际带出去几张经验卡、什么结局(审计 A3: 原来只记规律条数, 「没拿到卡的场」量不出来);
  · ``skill_versions``   —— 近 24 小时报上来的 skill 版本构成(审计 A8: 计数原来只在进程内存里, 要带 key 读, 部署就清零);
  · ``rule_compile``     —— 这个项目最近一次规则编译还有几条没编出来、是哪几种错(审计 A5: 原来只在服务日志里)。

钉住: 数是对的; 只有数和固定的词(卡的内容、乱填的版本串、Jev 报错里的规则原文都不进这一行); 不进回包; 哪一块算不出来就不带
哪一块, 存档和这一行照常。全部是合成项目、现编的文字。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import pytest

import config
import librarian_client as lib
from deskcore import core, skillcheck, tools
from tests.fakes import FakeClient
from tests.test_flywheel_arm import ME, PATTERNS, PID, _Librarian, _client, _commit_meta, _commit_out
from tests.test_rule_compile import (PROJ, FakeJev, _batch_of_ten_fails_on, _error_result, jev_sha, make_client, mem,
                                     question_result)

RULE_MARK = "规则原文标记丙RQ3"
RAW_VERSION = "乱填的版本串标记丁"


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """进程内存里的几样记录每条用例从空的开始。"""
    monkeypatch.setattr(core, "_borrowed_arms", {})
    monkeypatch.setattr(core, "_patterns_shown", {})
    monkeypatch.setattr(core, "_lessons_shown", {})
    monkeypatch.setattr(core, "_ARMS_SINCE", datetime(2000, 1, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(core, "_RULE_COMPILE_LAST", {})
    monkeypatch.setattr(core, "_RULE_COMPILE_STATE", {})
    monkeypatch.setattr(core, "_RULE_COMPILE_FAILED", {})
    monkeypatch.setattr(core, "_RULE_COMPILE_JEV", {"slow_until": 0.0})
    monkeypatch.setattr(core, "_RULE_COMPILE_INFLIGHT", {})
    monkeypatch.setattr(config, "RULE_COMPILE_AUTO", False)
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    monkeypatch.setattr(core, "flywheel_arm", lambda pid, uid, **kw: "patterns")
    skillcheck.reset()
    yield
    skillcheck.reset()


# ── flywheel_lessons ─────────────────────────────────────────────────────────────────────────────

def test_a_commit_with_no_borrow_on_record_says_so(monkeypatch):
    out, _params, meta = _commit_out(monkeypatch)
    assert meta["flywheel_lessons"] == {"borrows": 0}, "没借过 / 进程重启过: 不知道, 不编一个数"
    assert "rule_compile" not in meta and "skill_versions" not in meta, "直接调 core、本进程没编过规则: 这两块不带"
    assert "flywheel_lessons" not in json.dumps(out, ensure_ascii=False), "只进指标, 不进回包"


def test_cards_delivered_by_the_brief_and_a_follow_up_borrow_are_counted(monkeypatch):
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", _Librarian(PATTERNS))
    brief = core.build_writing_brief(_client(), PID, user_id=ME, brief={})
    assert len(brief["lessons"]) == 1

    def empty(brief, *, status=None):
        status.update({"state": lib.BORROW_EMPTY, "count": 0, "elapsed_ms": 3, "detail": "", "patterns": []})
        return []
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", empty)
    core.borrow_lessons(_client(), PID, user_id=ME, flywheel_arm=brief["flywheel_arm"])
    _out, params, meta = _commit_out(monkeypatch, flywheel_arm=brief["flywheel_arm"])
    assert meta["flywheel_lessons"] == {"borrows": 2, "shown": 0, "status": "empty", "shown_max": 1,
                                        "statuses": {"borrowed": 1, "empty": 1}}
    assert "flywheel_lessons" not in params, "不进 batches.params, 不碰分臂那套账"
    assert meta["flywheel_patterns_shown"] == 1, "规律条数照旧"
    assert "source_note_id" not in json.dumps(meta["flywheel_lessons"]), "卡的内容不进指标"


def test_a_timed_out_brief_counts_zero_cards_even_if_the_selection_finishes_later(monkeypatch):
    monkeypatch.setattr(config, "OPEN_PROJECT_BORROW_SEC", 0.01)
    done = threading.Event()

    def slow(brief, *, status=None):
        done.wait(2)
        status.update({"state": lib.BORROW_BORROWED, "count": 1, "elapsed_ms": 900, "detail": "", "patterns": []})
        return [{"source_note_id": "n1"}]
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", slow)
    try:
        brief = core.build_writing_brief(_client(), PID, user_id=ME, brief={})
        assert brief["lessons_status"]["status"] == lib.BORROW_TIMEOUT
    finally:
        done.set()
    import time
    time.sleep(0.05)                                      # 后台那次选卡这时跑完了: 这一份简报里没有它, 不算
    _out, _params, meta = _commit_out(monkeypatch, flywheel_arm=brief["flywheel_arm"])
    assert meta["flywheel_lessons"] == {"borrows": 1, "shown": 0, "status": "timeout", "shown_max": 0,
                                        "statuses": {"timeout": 1}}


def test_another_projects_borrow_is_not_counted(monkeypatch):
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", _Librarian(PATTERNS))
    core._note_lessons_shown("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", ME, {"lessons": [{}, {}], "status": "borrowed"})
    core._note_lessons_shown(PID, "22222222-2222-2222-2222-222222222222", {"lessons": [{}], "status": "borrowed"})
    assert core._lessons_meta(PID, ME) == {"borrows": 0}


# ── skill_versions ───────────────────────────────────────────────────────────────────────────────

def _score_through_the_tool(monkeypatch) -> dict:
    c = FakeClient(rows={"projects": [{"id": PID, "name": "项目", "brand": "A", "owner_id": ME,
                                       "calibration_notes": "", "tactics": "[]", "custom_roles": []}]})
    c.rpc_impl["deskcore_commit_fingerprints"] = lambda args: [
        {"idx": i, "status": core.COMMIT_STATUS_INSERTED, "collided_with": None, "detail": None}
        for i in range(len(args.get("_rows") or []))]
    monkeypatch.setattr(core, "sb", lambda: c)
    out = tools.score_drafts(PID, [{"title": "标题一", "body": "正文一二三四五六七八"}], _user_id=ME)
    assert out["written"] == 1
    return {"out": out, "row": [r for r in c.rows["batch_metrics"] if r["meta"]["mode"] == "deskcore_commit"][-1]}


def test_the_score_tool_stores_the_skill_version_counts(monkeypatch):
    current = tools.protocol_text()[1]
    old = "0123456789ab"
    skillcheck.report(ME, current, current)
    skillcheck.report(ME, old, current)
    skillcheck.report(ME, RAW_VERSION, current)           # 认不出 12 位十六进制: 按旧版算, 原文不进指标
    skillcheck.report(ME, None, current)
    got = _score_through_the_tool(monkeypatch)
    assert got["row"]["meta"]["skill_versions"] == {"current": current, "up_to_date": 1, "outdated": 2, "unknown": 1,
                                                    "outdated_versions": {old: 1, "other": 1}}
    blob = json.dumps(got["row"], ensure_ascii=False)
    assert RAW_VERSION not in blob and RAW_VERSION[:12] not in blob, "乱填的版本串不进指标"
    assert "skill_versions" not in json.dumps(got["out"], ensure_ascii=False), "只进指标, 不进回包"


def test_an_unreadable_protocol_only_drops_that_block(monkeypatch):
    monkeypatch.setattr(tools, "protocol_text", lambda: (_ for _ in ()).throw(RuntimeError("协议文件读不了")))
    got = _score_through_the_tool(monkeypatch)
    assert "skill_versions" not in got["row"]["meta"] and got["row"]["meta"]["flywheel_lessons"] == {"borrows": 0}


def test_a_block_that_cannot_be_computed_does_not_lose_the_row(monkeypatch):
    monkeypatch.setattr(core, "_lessons_meta", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    out, _params, meta = _commit_out(monkeypatch)
    assert out["written"] == 1 and meta["mode"] == "deskcore_commit" and "flywheel_lessons" not in meta


# ── rule_compile ─────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("error, kind", [
    ("生成端调用失败：GeneratorStopped: 模型拒答（stop_reason=refusal）", "refusal"),
    ("生成端调用失败：GeneratorStopped: 输出被截断（stop_reason=max_tokens）", "truncated"),
    ("生成端调用失败：GeneratorHTTPError: HTTP 529 overloaded", "generator_http_529"),
    ("生成端调用失败：TimeoutError: timed out", "generator_timeout"),
    ("生成端调用失败：URLError: connection refused", "generator_failed"),
    (f"模型输出不合格（已重试一次）：{RULE_MARK} 的 ask 不是问句", "bad_output"),
    ("Jev 回的 sha aaaa 和这边算的 bbbb 对不上", "sha_mismatch"),
    ("Jev 的回包里没有这条", "missing_in_response"),
    ("规则编译期间被删, 结果没存", "rule_deleted"),
    ("没有编出结果", "no_result"), ("没编出结果", "no_result"),
    (f"别的什么原因 {RULE_MARK}", "other"), (None, "other"),
])
def test_compile_errors_are_reduced_to_fixed_kinds(error, kind):
    assert core._compile_error_kind(error) == kind


def _failed(rule: dict, error: str) -> dict:
    return {"id": rule["id"], "sha": jev_sha(rule["text"], rule.get("applicability")), "verdict": None, "code": None,
            "questions": [], "coverage": None, "uncovered": "", "why": "", "error": error}


@pytest.fixture
def jev(monkeypatch):
    import requests
    monkeypatch.setattr(config, "JUDGE_URL", "https://judge.example.invalid/")
    monkeypatch.setattr(config, "JUDGE_API_KEY", "jk-admin")
    fake = FakeJev()
    monkeypatch.setattr(requests, "post", fake.post)
    return fake


def test_the_commit_row_says_how_many_rules_are_uncompiled_and_why(monkeypatch, jev):
    rules = [mem(1, f"示例规则一: {RULE_MARK}", severity="hard"), mem(2, "示例规则二: 标题不要太长", severity="hard"),
             mem(3, "示例规则三: 结尾留一个问题")]
    refused = {rules[0]["id"]: f"生成端调用失败：GeneratorStopped: 模型拒答（stop_reason=refusal）{RULE_MARK}",
               rules[1]["id"]: "生成端调用失败：GeneratorHTTPError: HTTP 529 overloaded"}
    jev.compile_one = lambda r: (_failed(r, refused[r["id"]]) if r["id"] in refused
                                 else question_result(r["id"], jev_sha(r["text"], r.get("applicability"))))
    c = make_client(rules)
    c.rpc_impl["deskcore_commit_fingerprints"] = lambda args: [
        {"idx": i, "status": core.COMMIT_STATUS_INSERTED, "collided_with": None, "detail": None}
        for i in range(len(args.get("_rows") or []))]
    assert core._rule_compile_meta(PROJ) is None, "本进程还没编过这个项目: 不带这一块"
    assert core.compile_rules(c, PROJ, dry_run=True)["status"] == "dry_run"
    assert core._rule_compile_meta(PROJ) is None, "只数不发的那一趟不算"

    rep = core.compile_rules(c, PROJ, skip_recent_failures=True)
    assert rep["status"] == "partial" and rep["compiled"] == 1
    out = core.commit_drafts(c, PROJ, [{"title": "标题一", "body": "正文一二三四五六七八"}], user_id=ME)
    assert out["written"] == 1
    block = _commit_meta(c)["rule_compile"]
    assert 0 <= block.pop("age_s") < 60
    assert block == {"status": "partial", "uncompiled": 2, "not_compilable": 0,
                     "error_kinds": {"refusal": 1, "generator_http_529": 1}}
    assert RULE_MARK not in json.dumps(_commit_meta(c)["rule_compile"], ensure_ascii=False), "报错里的规则原文不进指标"
    assert "rule_compile" not in json.dumps(out, ensure_ascii=False), "只进指标, 不进回包"

    # 下一轮: 拒答的那条还在「编失败 6 小时内不重编」里没发(529 那条 30 分钟后才重发), 这一轮没有新错 —— 还是 2 条没编出来,
    # 错误种类沿用上一轮的, 另记它们是多久之前的
    rep = core.compile_rules(c, PROJ, skip_recent_failures=True)
    assert rep["status"] == "up_to_date" and rep["held_after_failure"] == 2
    block = core._rule_compile_meta(PROJ)
    assert block["status"] == "up_to_date" and block["uncompiled"] == 2
    assert block["error_kinds"] == {"refusal": 1, "generator_http_529": 1} and "error_kinds_age_s" in block

    # 都编出来了: 这一块说 0 条, 不再带错误种类
    jev.compile_one = lambda r: question_result(r["id"], jev_sha(r["text"], r.get("applicability")))
    assert core.compile_rules(c, PROJ)["status"] == "ok"
    block = core._rule_compile_meta(PROJ)
    assert (block["status"], block["uncompiled"]) == ("ok", 0) and "error_kinds" not in block


def test_a_whole_batch_failure_is_counted_by_its_status(monkeypatch, jev):
    jev.compile_status, jev.compile_detail = 502, f"上游失败 {RULE_MARK}"
    c = make_client([mem(1), mem(2)])
    rep = core.compile_rules(c, PROJ)
    assert rep["compiled"] == 0
    block = core._rule_compile_meta(PROJ)
    assert block["uncompiled"] == 2 and block["error_kinds"] == {f"batch_{rep['status']}": 2}
    assert RULE_MARK not in json.dumps(block, ensure_ascii=False)


# ── rule_compile × 拆小重发: 种类按重发之后的结局数 ──────────────────────────────────────────────

def test_a_whole_batch_failure_that_the_resend_recovers_leaves_no_error_kinds(monkeypatch, jev):
    """第一次整批 502(那一下没通), 拆成 ≤ 3 条重发都编出来了: 这一块说 0 条没编出来, 不带错误种类 —— 报告里留着的那笔
    batch_errors(第一次的)不算。"""
    jev.compile_detail = "规则一条都没编出来：生成端调用失败：GeneratorHTTPError: HTTP 529 overloaded"
    jev.compile_status_fn = lambda body: 502 if len(body["rules"]) > 3 else 200
    rep = core.compile_rules(make_client([mem(i) for i in range(1, 6)]), PROJ)
    assert rep["status"] == "ok" and rep["batch_errors"][0]["status"] == "jev_failed"
    assert (rep["resent"], rep["recovered"]) == (5, 5)
    block = core._rule_compile_meta(PROJ)
    assert (block["status"], block["uncompiled"]) == ("ok", 0) and "error_kinds" not in block


def test_only_the_rules_that_fail_again_after_the_resend_are_counted_by_kind(monkeypatch, jev):
    """一批 10 条里 1 条让模型输出不合格, 这一批 10 条都带 error 回来; 重发之后只有和它同一小批的 3 条还没编出来。
    原来(没有重发)是 10 条 bad_output。日志那一行和这一块是同一套词、同一个数。"""
    import logging
    rules = [mem(i, severity="hard") for i in range(1, 11)]
    _batch_of_ten_fails_on(jev, monkeypatch, {rules[4]["id"]}, error=f"模型输出不合格（已重试一次）：{RULE_MARK}")
    records: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda rec: records.append(rec.getMessage())
    logging.getLogger("deskcore.core").addHandler(handler)
    try:
        rep = core.compile_rules(make_client(rules), PROJ)
    finally:
        logging.getLogger("deskcore.core").removeHandler(handler)
    assert rep["status"] == "partial" and (rep["resent"], rep["recovered"], len(rep["errors"])) == (10, 7, 3)
    block = core._rule_compile_meta(PROJ)
    assert block["uncompiled"] == 3 and block["error_kinds"] == {"bad_output": 3}
    assert RULE_MARK not in json.dumps(block, ensure_ascii=False)
    (line,) = [m for m in records if "拆小重发" in m and "没编出来" in m]
    assert "还有 3 条没编出来" in line and "bad_output: " in line


def test_a_resend_that_fails_again_as_a_whole_is_counted_once_per_rule(monkeypatch, jev):
    """第一次整批 502、重发的小请求又是 502: 每条按重发那次的结局记一次(生成端 529), 不再按第一次那一批多数一遍。"""
    jev.compile_status = 502
    jev.compile_detail = "规则一条都没编出来：生成端调用失败：GeneratorHTTPError: HTTP 529 overloaded"
    rep = core.compile_rules(make_client([mem(1), mem(2)]), PROJ)
    assert rep["status"] == "jev_failed" and len(rep["batch_errors"]) == 1 and len(rep["errors"]) == 2
    block = core._rule_compile_meta(PROJ)
    assert block["uncompiled"] == 2 and block["error_kinds"] == {"generator_http_529": 2}


def test_a_batch_that_is_not_resent_is_counted_by_its_status_and_its_size(monkeypatch, jev):
    """读超时不重发(照旧停整次运行): 那一批按 batch_timeout 记条数; 后面没发的几条只在 uncompiled 里。"""
    import requests
    monkeypatch.setattr(config, "RULE_COMPILE_BATCH", 3)
    jev.raise_exc = requests.Timeout("read timed out (synthetic)")
    rep = core.compile_rules(make_client([mem(i) for i in range(1, 6)]), PROJ)
    assert rep["status"] == "timeout" and rep["not_sent"] == 2 and "resent" not in rep
    block = core._rule_compile_meta(PROJ)
    assert block["uncompiled"] == 5 and block["error_kinds"] == {"batch_timeout": 3}


def test_rules_left_unsent_when_a_resend_is_stopped_keep_their_first_failure_kind(monkeypatch, jev):
    """重发的第一个小请求被挡下(整次运行停): 这一批 5 条照第一次的失败各记一次, 被挡的那个小请求不再另数 3 条。"""
    import requests
    monkeypatch.setattr(config, "RULE_COMPILE_BATCH", 5)
    jev.compile_one = lambda r: _error_result(r, "生成端调用失败：GeneratorStopped: 模型拒答（stop_reason=refusal）")
    real_post = requests.post

    def post(url, **kw):
        if url.endswith("/compile_rules") and len(jev.compile_calls()) == 1:         # 第 2 个请求 = 第一小批重发
            jev.compile_status, jev.compile_detail = 403, "policy: 示例项目没清出境"
        return real_post(url, **kw)
    monkeypatch.setattr(requests, "post", post)
    rep = core.compile_rules(make_client([mem(i) for i in range(1, 8)]), PROJ)
    assert rep["status"] == "policy_blocked" and rep["not_sent"] == 2 and len(rep["errors"]) == 5
    assert [b["status"] for b in rep["batch_errors"]] == ["policy_blocked"]
    block = core._rule_compile_meta(PROJ)
    assert block["uncompiled"] == 7 and block["error_kinds"] == {"refusal": 5}


def test_an_unreadable_pool_records_the_status_without_a_count(monkeypatch, jev):
    c = make_client([mem(1)])
    monkeypatch.setattr(core.store, "shared_memories", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("库读不了")))
    assert core.compile_rules(c, PROJ)["status"] == "error"
    block = core._rule_compile_meta(PROJ)
    assert block["status"] == "error" and "uncompiled" not in block, "规则池读不出来: 没编出来几条不知道, 不写 0"
