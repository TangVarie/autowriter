"""规则编译(2026-10-20, D-121): 规则交给 Jev 的 POST /compile_rules 编成写后检查, 按 (规则 id, sha) 缓存进 rule_compiles。

钉的是:

  · sha 与 Jev ``rules.rule_sha`` 逐字节一致, 拼题库与 Jev ``rules.to_project_bank`` 逐字节一致(期望值是 Jev 那边对
    合成样例算出来、抄在这里的 —— 两边一漂, 缓存永远命中不了、每次都重编白花钱);
  · 已经有 (id, sha) 一行的不再发; 原文改了 sha 变、重编; ``force`` 全部重编; 带 error 的不存、报出来; 一批 ≤ 60 条;
  · Jev 那边怎么失败都不抛(超时 / 出境口径 / 没接 tv-map / 没配), 回报告; 被挡下的后面几批不发;
  · 后台补编: 每个项目同时一个线程、冷却之内不重复起, open_project / record_rule 不等它; record_rule 不等冷却;
  · CLI compile-rules 一个项目一行计数, 没编成退出码 1; --all 里 owner 的 global 规则一次运行只发一次(--force、--dry-run
    也是); dry-run 和真编同一个结局(没接 tv-map / 没配 JUDGE_* 照样报);
  · 复审(2026-10-20): 读超时之后后面几批不发、后台停一阵; 整批被挡不按规则记失败(owner 的 global 规则在别的项目照编);
    编译期间删掉的规则不连累同一批别的结果。

所有规则原文、题目都是合成的示例。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time

import pytest
import requests

import config
from deskcore import cli, core, rulecompile, store
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
PROJ = "aaaaaaaa-0000-4000-8000-0000000000a1"
OTHER_PROJ = "bbbbbbbb-0000-4000-8000-0000000000b2"
TV = "DEMO_phase1"


# 假 Jev 回包里的 model(合成的名字)。不能是 mock: Jev 在 JUDGE_MOCK 下回的 model 是 mock, 那种结果 compile_rules 一条不存
FAKE_COMPILER = "example-compiler"


def mid(i: int) -> str:
    return f"{i:08x}-1111-4111-8111-111111111111"


def mem(i: int, content: str | None = None, *, severity: str = "soft", scope: str = "project",
        project_id: str | None = PROJ, user_id: str = ME, applicability: str | None = None,
        rule_kind: str | None = None, rule_payload=None, status: str = "confirmed") -> dict:
    return {"id": mid(i), "content": content if content is not None else f"示例规则第 {i} 条: 正文不要写虚构的示例宣称",
            "severity": severity, "scope": scope, "project_id": project_id if scope == "project" else None,
            "user_id": user_id, "status": status, "memory_type": "rule", "muted_until": None,
            "rule_kind": rule_kind, "rule_payload": rule_payload, "created_at": f"2026-10-{(i % 28) + 1:02d}T00:00:00+00:00",
            "frequency": 1, "applicability": applicability}


def jev_sha(text: str, applicability: str | None = None) -> str:
    """测试里独立写一份 Jev 的算法(不调 rulecompile.rule_sha), 给假 Jev 用。"""
    app = (applicability or "").strip()
    raw = text if not app else text + "\x00" + app
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def jev_qid(rule_id: str, sha: str, n: int, q: dict) -> str:
    short = "".join(ch for ch in rule_id.lower() if ch.isalnum())[:8] or "r"
    qh = hashlib.sha256(json.dumps({"ask": q["ask"], "pass_if": q["pass_if"], "scope": q["scope"]},
                                   ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:6]
    return f"{short}_{sha[:6]}_{n}_{qh}"


def question_result(rule_id: str, sha: str, *, ask="正文有没有出现虚构的示例宣称？", pass_if="否", scope="body",
                    note="示例要点", code=None) -> dict:
    q = {"ask": ask, "pass_if": pass_if, "scope": scope, "note": note}
    return {"id": rule_id, "sha": sha, "verdict": "questions", "code": code,
            "questions": [{"qid": jev_qid(rule_id, sha, 1, q), **q}], "coverage": "full", "uncovered": "",
            "why": "要读懂内容"}


def code_result(rule_id: str, sha: str, code: dict) -> dict:
    return {"id": rule_id, "sha": sha, "verdict": "code", "code": code, "questions": [], "coverage": "full",
            "uncovered": "", "why": "代码能查"}


def stored(rule: dict, result: dict | None = None) -> dict:
    sha = jev_sha(rule["content"], rule.get("applicability"))
    return {"memory_id": rule["id"], "sha": sha, "result": result or question_result(rule["id"], sha),
            "model": FAKE_COMPILER, "compiled_at": "2026-10-20T00:00:00+00:00"}


def make_client(memories=(), compiles=(), *, maps=True, projects=None, versions=()) -> FakeClient:
    rows = {
        "projects": projects or [
            {"id": PROJ, "name": "示例品牌-产品向", "brand": "示例品牌", "owner_id": ME, "calibration_notes": "",
             "tactics": "[]", "custom_roles": []},
            {"id": OTHER_PROJ, "name": "别人的项目", "brand": "别家", "owner_id": OTHER, "calibration_notes": "",
             "tactics": "[]", "custom_roles": []}],
        "memories": list(memories),
        "rule_compiles": list(compiles),
        "tv_project_map": [{"tv_project_id": TV, "project_id": PROJ, "ingest_target": True}] if maps else [],
        "truth_vault.projects": [{"project_id": TV, "category": "示例品类"}],
        "versions": list(versions),
    }
    return FakeClient(rows=rows)


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.text = status, body, json.dumps(body, ensure_ascii=False) if body else ""

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class FakeJev:
    """假的 Jev(只回 /compile_rules 与 /judge_draft)。``compile_one`` / ``judge`` 可以换掉来造各种回包。"""

    def __init__(self):
        self.calls: list[dict] = []
        self.compile_one = lambda r: question_result(r["id"], jev_sha(r["text"], r.get("applicability")))
        self.compile_status = 200
        self.compile_detail = None
        self.compile_status_fn = None              # body → 状态码(按请求决定成败, 比如只挡处方药品类)
        self.judge = lambda payload: {"passed": True, "plan": [], "detail": {}, "written": 0, "write_error": None}
        self.model = FAKE_COMPILER                 # /compile_rules 回包里的 model; 设成 mock 就是 JUDGE_MOCK 下的 Jev
        self.raise_exc = None
        self.lock = threading.Lock()

    def post(self, url, *, headers=None, json=None, timeout=None, **kw):
        with self.lock:
            self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if self.raise_exc is not None:
            raise self.raise_exc
        if url.endswith("/compile_rules"):
            status = self.compile_status_fn(json) if self.compile_status_fn else self.compile_status
            if status != 200:
                return _Resp(status, {"detail": self.compile_detail or "失败"})
            results = [self.compile_one(r) for r in json["rules"]]
            return _Resp(200, {"project": json["project"], "category": json.get("category"), "model": self.model,
                               "rules": results, "project_bank": None, "errors": sum(1 for r in results if r.get("error")),
                               "calls": 1, "usage": {}, "latency_ms": 3, "policy": {"project": json["project"]}})
        if url.endswith("/judge_draft"):
            return _Resp(200, self.judge(json))
        return _Resp(404, {"detail": "no such path"})

    def compile_calls(self) -> list[dict]:
        return [c for c in self.calls if c["url"].endswith("/compile_rules")]


@pytest.fixture(autouse=True)
def _fresh_compile_state(monkeypatch):
    """进程内的后台补编状态每条用例清空; 默认关掉后台补编(要测它的用例自己打开), 免得线程跑进别的断言。"""
    monkeypatch.setattr(core, "_RULE_COMPILE_STATE", {})
    monkeypatch.setattr(core, "_RULE_COMPILE_FAILED", {})
    monkeypatch.setattr(core, "_RULE_COMPILE_SLOTS", threading.BoundedSemaphore(1))
    monkeypatch.setattr(core, "_RULE_COMPILE_JEV", {"slow_until": 0.0})
    monkeypatch.setattr(core, "_RULE_COMPILE_INFLIGHT", {})
    monkeypatch.setattr(config, "RULE_COMPILE_AUTO", False)
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)


@pytest.fixture
def jev(monkeypatch):
    monkeypatch.setattr(config, "JUDGE_URL", "https://judge.example.invalid/")
    monkeypatch.setattr(config, "JUDGE_API_KEY", "jk-admin")
    fake = FakeJev()
    monkeypatch.setattr(requests, "post", fake.post)
    return fake


# ══════════════════════════════════════════════════════════════════════
# 1 · 和 Jev 逐字节一致
# ══════════════════════════════════════════════════════════════════════

# 期望值是 JevforCoentent judge/rules.py 的 rule_sha / make_qid / to_project_bank 对下面这些合成输入算出来的。
@pytest.mark.parametrize("text, app, want", [
    ("标题不要出现「示例词」", None, "a188e0afbea1"),
    ("标题不要出现「示例词」", "产品向", "9827fed80dab"),
    ("标题不要出现「示例词」", "  产品向  ", "9827fed80dab"),     # applicability 去首尾空白
    ("标题不要出现「示例词」", "   ", "a188e0afbea1"),            # 去完是空的 = 没有
    ("  前后带空白的规则原文 \n", None, "274ef35fb1ce"),            # 原文原样, 不去空白
    ("Synthetic rule text", "title", "a6685dff937c"),
])
def test_rule_sha_matches_jev(text, app, want):
    assert rulecompile.rule_sha(text, app) == want


def test_project_bank_matches_jev_to_project_bank():
    rid = "aaaaaaaa-1111-2222-3333-444444444444"
    sha = rulecompile.rule_sha("正文不要写虚构的示例宣称")
    q = {"ask": "正文有没有出现虚构的示例宣称？", "pass_if": "否", "scope": "body", "note": "示例"}
    qid = jev_qid(rid, sha, 1, q)
    assert qid == "aaaaaaaa_1eb261_1_a76974", "题号算法和 Jev make_qid 一致"
    bank = rulecompile.project_bank([{"id": rid, "sha": sha, "verdict": "questions", "questions": [{"qid": qid, **q}]}])
    assert bank == {
        "bank_version": "rules-729333d9a32c",
        "ambiguity": {"choice_top_min": 0.6, "choice_margin_min": 0.2, "noul_band": [0.35, 0.65]},
        "questions": [{"id": "aaaaaaaa_1eb261_1_a76974", "type": "noul", "scope": "body",
                       "instructions": {"zh": "正文有没有出现虚构的示例宣称？"},
                       "criteria": {"true": {"zh": "是"}, "false": {"zh": "否"}}, "evidence": "on_true",
                       "feeds": "写作台规则 aaaaaaaa-1111-2222-3333-444444444444（sha 1eb261b7b973）：答「否」算守住；只作建议，不进 hard_rules",
                       "on_ambiguous": "只提示"}]}


def test_project_bank_skips_bad_and_duplicate_questions_instead_of_raising():
    sha = rulecompile.rule_sha("x")
    good = question_result(mid(1), sha)
    dup = dict(good, id=mid(2))                                  # 同一个题号又出现一次
    bad = question_result(mid(3), sha)
    bad["questions"][0]["qid"] = "不是题号"
    err = dict(question_result(mid(4), sha), error="模型输出不合格")
    bank = rulecompile.project_bank([good, dup, bad, err, code_result(mid(5), sha, {"kind": "max_len", "max": 20,
                                                                                     "where": "title"})])
    assert [q["id"] for q in bank["questions"]] == [good["questions"][0]["qid"]]
    assert rulecompile.project_bank([err]) is None


# ══════════════════════════════════════════════════════════════════════
# 2 · 编译
# ══════════════════════════════════════════════════════════════════════

def test_only_uncompiled_rules_are_sent_and_results_are_stored(jev):
    r1, r2 = mem(1), mem(2, severity="hard", applicability="标题")
    r3 = mem(3, scope="global", project_id=None)                 # owner 自己的 global 规则也在池子里
    r_other_owner_global = mem(4, scope="global", project_id=None, user_id=OTHER)
    c = make_client([r1, r2, r3, r_other_owner_global], [stored(r1)])
    rep = core.compile_rules(c, PROJ)
    assert rep["status"] == "ok", rep
    assert (rep["pool"], rep["cached"], rep["to_compile"], rep["compiled"]) == (3, 1, 2, 2)
    (call,) = jev.compile_calls()
    assert call["url"] == "https://judge.example.invalid/compile_rules"
    assert call["headers"] == {"X-Judge-Key": "jk-admin"}, "管理 key"
    assert call["timeout"] == (3.0, 420.0), "读超时 ≥ 400 秒(Jev 最坏约 361 秒)"
    body = call["json"]
    assert body["project"] == TV and body["category"] == "示例品类"
    sent = {r["id"]: r for r in body["rules"]}
    assert set(sent) == {r2["id"], r3["id"]}, "已编过的不发, 别人的 global 不在池子里"
    assert sent[r2["id"]] == {"id": r2["id"], "text": r2["content"], "severity": "hard", "applicability": "标题"}
    assert "applicability" not in sent[r3["id"]]
    rows = {(r["memory_id"], r["sha"]): r for r in c.rows["rule_compiles"]}
    sha2 = jev_sha(r2["content"], "标题")
    assert (r2["id"], sha2) in rows and rows[(r2["id"], sha2)]["result"]["verdict"] == "questions"
    assert rows[(r2["id"], sha2)]["model"] == FAKE_COMPILER
    assert rep["verdicts"] == {"questions": 2}
    # 再跑一次: 全部命中缓存, 一个请求都不发
    rep = core.compile_rules(c, PROJ)
    assert rep["status"] == "up_to_date" and rep["to_compile"] == 0 and len(jev.compile_calls()) == 1


def test_edited_rule_gets_a_new_sha_and_is_recompiled(jev):
    r1 = mem(1)
    c = make_client([dict(r1, content="改过之后的示例规则原文")], [stored(r1)])   # 缓存里是改之前那一版
    rep = core.compile_rules(c, PROJ)
    assert rep["to_compile"] == 1 and rep["compiled"] == 1
    assert len(c.rows["rule_compiles"]) == 2, "旧 sha 那行留着(改回去不再花钱), 新 sha 另起一行"


def test_direction_changes_the_sha_too(jev):
    r1 = mem(1)
    c = make_client([dict(r1, applicability="流量向")], [stored(r1)])
    assert core.compile_rules(c, PROJ)["to_compile"] == 1


def test_force_recompiles_and_overwrites(jev):
    r1, r2 = mem(1), mem(2)
    c = make_client([r1, r2], [stored(r1), stored(r2)])
    jev.compile_one = lambda r: code_result(r["id"], jev_sha(r["text"]),
                                            {"kind": "forbidden_word", "words": ["示例词"], "where": ["title"]})
    rep = core.compile_rules(c, PROJ, force=True)
    assert rep["status"] == "ok" and rep["to_compile"] == 2 and rep["compiled"] == 2
    assert len(c.rows["rule_compiles"]) == 2, "撞上 (memory_id, sha) 就覆盖, 不另起一行"
    assert all(r["result"]["verdict"] == "code" for r in c.rows["rule_compiles"])


def test_error_rows_are_reported_not_stored(jev):
    r1, r2 = mem(1), mem(2)
    c = make_client([r1, r2])

    def one(r):
        sha = jev_sha(r["text"])
        if r["id"] == r2["id"]:
            return {"id": r["id"], "sha": sha, "verdict": None, "code": None, "questions": [], "coverage": None,
                    "uncovered": "", "why": "", "error": "模型输出不合格（已重试一次）：示例原因"}
        return question_result(r["id"], sha)
    jev.compile_one = one
    rep = core.compile_rules(c, PROJ)
    assert rep["status"] == "partial" and rep["compiled"] == 1
    assert [e["memory_id"] for e in rep["errors"]] == [r2["id"]] and "示例原因" in rep["errors"][0]["error"]
    assert [r["memory_id"] for r in c.rows["rule_compiles"]] == [r1["id"]], "带 error 的不存, 下次再编"
    # 后台路径: 刚失败过的同一版本这次不再发(每编一次都花钱); CLI 路径照常重发
    rep = core.compile_rules(c, PROJ, skip_recent_failures=True)
    assert rep["status"] == "up_to_date" and rep["held_after_failure"] == 1 and len(jev.compile_calls()) == 1
    core.compile_rules(c, PROJ)
    assert len(jev.compile_calls()) == 2 and [r["id"] for r in jev.compile_calls()[-1]["json"]["rules"]] == [r2["id"]]


def test_sha_mismatch_is_not_stored(jev):
    c = make_client([mem(1)])
    jev.compile_one = lambda r: question_result(r["id"], "0" * 12)
    rep = core.compile_rules(c, PROJ)
    assert rep["status"] == "error" and "对不上" in rep["errors"][0]["error"]
    assert c.rows["rule_compiles"] == []


def test_batches_hold_at_most_60_rules(jev):
    c = make_client([mem(i) for i in range(1, 131)])
    rep = core.compile_rules(c, PROJ)
    assert [len(x["json"]["rules"]) for x in jev.compile_calls()] == [60, 60, 10]
    assert rep["batches"] == 3 and rep["compiled"] == 130 and rep["status"] == "ok"


def test_rules_over_jev_limits_are_not_sent(jev):
    long_rule = mem(1, content="长" * (rulecompile.MAX_TEXT_CHARS + 1))
    long_app = mem(2, applicability="部" * 101)
    c = make_client([long_rule, long_app, mem(3)])
    rep = core.compile_rules(c, PROJ)
    assert {x["memory_id"] for x in rep["not_compilable"]} == {long_rule["id"], long_app["id"]}
    assert [r["id"] for r in jev.compile_calls()[0]["json"]["rules"]] == [mid(3)], "一条坏的不能让整批 422"


@pytest.mark.parametrize("make_fail, status", [
    (lambda j: setattr(j, "raise_exc", requests.Timeout("read timed out")), "timeout"),
    (lambda j: (setattr(j, "compile_status", 403), setattr(j, "compile_detail", "policy: 示例项目没清出境")), "policy_blocked"),
    (lambda j: (setattr(j, "compile_status", 502), setattr(j, "compile_detail", "规则一条都没编出来")), "jev_failed"),
])
def test_jev_failure_never_raises(jev, make_fail, status):
    c = make_client([mem(i) for i in range(1, 71)])
    make_fail(jev)
    rep = core.compile_rules(c, PROJ)
    assert rep["status"] == status and rep["compiled"] == 0 and c.rows["rule_compiles"] == []
    if status == "policy_blocked":
        assert len(jev.compile_calls()) == 1 and rep["not_sent"] == 10, "被出境口径挡下: 后面那批一样会被挡, 不发"
    elif status == "timeout":
        # 2026-10-20 复审: 读超时过了 Jev 最坏的 ~361 秒, 超时那批多半还在 Jev 那边编、照样计费; 再发后面几批只是多付钱
        assert len(jev.compile_calls()) == 1 and rep["not_sent"] == 10 and rep["stopped_by"] == "timeout"
    else:
        assert len(jev.compile_calls()) == 2, "Jev 回了 502(答了、几乎没花钱)只算这一批, 下一批照发"


def test_not_configured_and_no_tv_map_send_nothing(jev, monkeypatch):
    rep = core.compile_rules(make_client([mem(1)], maps=False), PROJ)
    assert rep["status"] == "not_configured" and "tv-map" in rep["detail"] and jev.calls == []
    monkeypatch.setattr(config, "JUDGE_URL", "")
    rep = core.compile_rules(make_client([mem(1)]), PROJ)
    assert rep["status"] == "not_configured" and jev.calls == []


def test_dry_run_counts_and_sends_nothing(jev):
    r1 = mem(1)
    rep = core.compile_rules(make_client([r1, mem(2)], [stored(r1)]), PROJ, dry_run=True)
    assert (rep["status"], rep["cached"], rep["to_compile"]) == ("dry_run", 1, 1) and jev.calls == []


def test_missing_table_is_a_report_not_a_crash(jev):
    c = make_client([mem(1)])
    c.missing_columns = {"rule_compiles": {"memory_id"}}
    rep = core.compile_rules(c, PROJ)
    assert rep["status"] == "error" and "015" in rep["detail"] and jev.calls == [], "读不了缓存就不去花编译的钱"


def test_gated_when_a_caller_is_given(jev):
    with pytest.raises(PermissionError):
        core.compile_rules(make_client([mem(1)]), OTHER_PROJ, user_id=ME)
    with pytest.raises(core.ProjectNotFound):
        core.compile_rules(make_client([mem(1)]), "cccccccc-0000-4000-8000-0000000000c3")


# ══════════════════════════════════════════════════════════════════════
# 3 · 后台补编
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture
def blocking_compile(monkeypatch):
    """把 compile_rules 换成一个会卡住的假件: 测"只起一个线程""调用方不等"。"""
    monkeypatch.setattr(config, "RULE_COMPILE_AUTO", True)
    monkeypatch.setattr(config, "JUDGE_URL", "https://judge.example.invalid/")
    monkeypatch.setattr(config, "JUDGE_API_KEY", "jk-admin")
    gate = threading.Event()
    calls: list[dict] = []

    def fake(client, project_id, **kw):
        calls.append({"project_id": project_id, **kw})
        gate.wait(5)
        return {"status": "ok", "pool": 1, "cached": 0, "compiled": 1, "errors": []}
    monkeypatch.setattr(core, "compile_rules", fake)
    return gate, calls


def _wait_idle(pid: str, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = core._RULE_COMPILE_STATE.get(pid)
        if st is not None and not st["running"]:
            return
        time.sleep(0.01)
    raise AssertionError("后台线程没收尾")


def test_background_compile_starts_once_and_never_blocks(blocking_compile):
    gate, calls = blocking_compile
    c = make_client([mem(1)])
    t0 = time.monotonic()
    assert core._kick_rule_compile(c, PROJ, reason="open_project") == "started"
    assert core._kick_rule_compile(c, PROJ, reason="open_project") == "running", "同一个项目同时只有一个线程"
    assert time.monotonic() - t0 < 0.5, "触发的那一下不等编译"
    gate.set()
    _wait_idle(PROJ)
    assert len(calls) == 1 and calls[0]["skip_recent_failures"] is True
    assert core._kick_rule_compile(c, PROJ, reason="open_project") == "cooldown", "刚编完, 冷却之内不再起"
    assert core._kick_rule_compile(c, PROJ, reason="record_rule", bypass_cooldown=True) == "started", \
        "刚记的规则不等冷却"
    _wait_idle(PROJ)
    assert len(calls) == 2


def test_record_rule_during_a_run_queues_one_more_round(blocking_compile):
    gate, calls = blocking_compile
    c = make_client([mem(1)])
    assert core._kick_rule_compile(c, PROJ, reason="open_project") == "started"
    assert core._kick_rule_compile(c, PROJ, reason="record_rule", bypass_cooldown=True) == "queued"
    gate.set()
    _wait_idle(PROJ)
    assert len(calls) == 2, "正在编时记的规则, 那个线程跑完再来一轮(它读池子时这条可能还没写进去)"


def test_held_after_policy_block(monkeypatch, blocking_compile):
    gate, calls = blocking_compile
    gate.set()
    monkeypatch.setattr(core, "compile_rules", lambda client, pid, **kw: (calls.append(kw) or
                                                                          {"status": "policy_blocked", "errors": []}))
    c = make_client([mem(1)])
    assert core._kick_rule_compile(c, PROJ, reason="open_project") == "started"
    _wait_idle(PROJ)
    core._RULE_COMPILE_STATE[PROJ]["last"] -= core.RULE_COMPILE_COOLDOWN_SEC + 1     # 冷却过了
    assert core._kick_rule_compile(c, PROJ, reason="open_project") == "held", "被挡下的项目一小时内不再试"


def test_one_project_compiles_at_a_time_per_process(blocking_compile):
    """部署后第一轮各项目同时触发: 排队编, 同一个 owner 的 global 规则不会被两个项目各编一遍。"""
    gate, calls = blocking_compile
    c = make_client([mem(1)])
    assert core._kick_rule_compile(c, PROJ, reason="open_project") == "started"
    assert core._kick_rule_compile(c, OTHER_PROJ, reason="open_project") == "started", "每个项目各一个线程"
    time.sleep(0.2)
    assert len(calls) == 1, "第二个项目在排队, 还没开始编"
    gate.set()
    _wait_idle(PROJ)
    _wait_idle(OTHER_PROJ)
    assert {x["project_id"] for x in calls} == {PROJ, OTHER_PROJ}


def test_cli_turns_background_compile_off(monkeypatch):
    c = make_client([mem(1)])
    monkeypatch.setattr(core, "sb", lambda: c)
    monkeypatch.setattr(config, "RULE_COMPILE_AUTO", True)
    cli.main(["projects", "--user", ME])
    assert config.RULE_COMPILE_AUTO is False, "CLI 进程跑完就退出, 后台线程会被杀在半路(Jev 照样花钱、结果没人存)"


def test_auto_off_or_judge_not_configured_starts_nothing(blocking_compile, monkeypatch):
    _gate, calls = blocking_compile
    c = make_client([mem(1)])
    monkeypatch.setattr(config, "RULE_COMPILE_AUTO", False)
    assert core._kick_rule_compile(c, PROJ, reason="open_project") == "off"
    monkeypatch.setattr(config, "RULE_COMPILE_AUTO", True)
    monkeypatch.setattr(config, "JUDGE_URL", "")
    assert core._kick_rule_compile(c, PROJ, reason="open_project") == "off"
    assert calls == []


def test_open_project_and_record_rule_trigger_without_waiting(blocking_compile, monkeypatch):
    gate, calls = blocking_compile
    kicks: list[tuple] = []
    real_kick = core._kick_rule_compile

    def spy(client, project_id, **kw):
        kicks.append((project_id, kw.get("reason"), kw.get("bypass_cooldown", False)))
        return real_kick(client, project_id, **kw)
    monkeypatch.setattr(core, "_kick_rule_compile", spy)
    monkeypatch.setattr(core, "_borrow_for_brief_bounded", lambda *a, **k: {
        "lessons": [], "status": "not_configured", "count": 0, "elapsed_ms": 0, "detail": "", "flywheel_arm": None})
    c = make_client([mem(1)])
    t0 = time.monotonic()
    brief = core.build_writing_brief(c, PROJ, user_id=ME, brief={})
    assert brief["project_id"] == PROJ and time.monotonic() - t0 < 2.0, "简报不等编译(编译那边还卡着)"
    assert kicks == [(PROJ, "open_project", False)]
    monkeypatch.setattr(core.db, "upsert_memory", lambda client, **kw: {"id": mid(9), "severity": kw["severity"]})
    out = core.record_rule(c, PROJ, "示例规则: 结尾不要写虚构的示例口号", user_id=ME)
    assert out["memory_id"] == mid(9)
    assert kicks[-1] == (PROJ, "record_rule", True), "record_rule 不等冷却"
    gate.set()
    _wait_idle(PROJ)


@pytest.mark.parametrize("action,direction,status", [("promote", "", "candidate"),
                                                     ("set_direction", "流量向", "confirmed"),
                                                     ("unmute", "", "confirmed")])
@pytest.mark.parametrize("scope", ["project", "global"])
def test_activating_or_retargeting_a_rule_starts_compiling_immediately(monkeypatch, action, direction, status, scope):
    kicks = []
    monkeypatch.setattr(core, "_kick_rule_compile", lambda client, pid, **kw: kicks.append((pid, kw)))
    rule = mem(19, status=status, scope=scope)
    if action == "unmute":
        rule["muted_until"] = "2999-01-01T00:00:00+00:00"
    client = make_client([rule])
    core.set_rule_state(client, rule["id"], action, direction=direction, user_id=ME)
    assert kicks == [(PROJ, {"reason": "set_rule_state", "bypass_cooldown": True})]


def test_worker_survives_a_crashing_compile(monkeypatch, blocking_compile):
    _gate, _calls = blocking_compile

    def boom(client, pid, **kw):
        raise RuntimeError("库抖了一下")
    monkeypatch.setattr(core, "compile_rules", boom)
    c = make_client([mem(1)])
    assert core._kick_rule_compile(c, PROJ, reason="open_project") == "started"
    _wait_idle(PROJ)
    assert core._RULE_COMPILE_STATE[PROJ]["running"] is False


# ══════════════════════════════════════════════════════════════════════
# 4 · CLI
# ══════════════════════════════════════════════════════════════════════

def test_cli_compile_rules_one_project(jev, monkeypatch, capsys):
    r1 = mem(1)
    c = make_client([r1, mem(2)], [stored(r1)])
    monkeypatch.setattr(core, "sb", lambda: c)
    assert cli.main(["compile-rules", "--project", PROJ, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "池子 2 条" in out and "已编过 1" in out and "要编 1" in out and "dry_run" in out and jev.calls == []
    assert cli.main(["compile-rules", "--project", PROJ]) == 0
    assert "这次编进 1" in capsys.readouterr().out


def test_cli_compile_rules_all_and_exit_code(jev, monkeypatch, capsys):
    mine = mem(1)
    theirs = mem(2, project_id=OTHER_PROJ, user_id=OTHER)
    c = make_client([mine, theirs])                    # OTHER_PROJ 没接 tv-map → not_configured
    monkeypatch.setattr(core, "sb", lambda: c)
    assert {p["id"] for p in store.projects_with_rules(c)} == {PROJ, OTHER_PROJ}
    assert cli.main(["compile-rules", "--all"]) == 1, "有项目没编成, 退出码 1"
    out = capsys.readouterr().out
    assert "✓ 示例品牌-产品向" in out and "✗ 别人的项目" in out and "not_configured" in out and "合计 2 个项目" in out


def test_projects_with_rules_counts_owners_of_global_rules():
    c = make_client([mem(1, scope="global", project_id=None, user_id=OTHER), mem(2, status="candidate")])
    assert [p["id"] for p in store.projects_with_rules(c)] == [OTHER_PROJ], \
        "OTHER 有 global 规则 → 他的项目要编; PROJ 只有试用档的规则 → 不编"


# ══════════════════════════════════════════════════════════════════════
# 5 · 复审(2026-10-20)
# ══════════════════════════════════════════════════════════════════════

PROJ2 = "aaaaaaaa-0000-4000-8000-0000000000a2"
PROJ3 = "aaaaaaaa-0000-4000-8000-0000000000a3"
RX_PROJ = "aaaaaaaa-0000-4000-8000-0000000000a9"


def _project(pid: str, name: str, owner: str = ME) -> dict:
    return {"id": pid, "name": name, "brand": "示例品牌", "owner_id": owner, "calibration_notes": "",
            "tactics": "[]", "custom_roles": []}


def _three_projects(memories, compiles=()) -> FakeClient:
    pids = (PROJ, PROJ2, PROJ3)
    return FakeClient(rows={
        "projects": [_project(p, f"示例项目-{p[-2:]}") for p in pids],
        "memories": list(memories), "rule_compiles": list(compiles),
        "tv_project_map": [{"tv_project_id": TV, "project_id": p, "ingest_target": True} for p in pids],
        "truth_vault.projects": [{"project_id": TV, "category": "示例品类"}], "versions": []})


def _sent_ids(jev) -> list[str]:
    return [r["id"] for call in jev.compile_calls() for r in call["json"]["rules"]]


def _total_line(out: str) -> str:
    return next(line for line in out.splitlines() if line.startswith("合计"))


def test_all_sends_each_shared_global_rule_once_with_force_and_dry_run(jev, monkeypatch, capsys):
    """owner 的 global 规则在他每个项目的池子里: 原来 --all --force 按项目数重编、重付钱(3 个项目 × 5 条 = 发 15 条),
    --all --dry-run 的「要编」合计也按项目数算(15), 和真编实际发的(5)对不上。"""
    globs = [mem(i, scope="global", project_id=None) for i in range(1, 6)]
    c = _three_projects(globs)
    monkeypatch.setattr(core, "sb", lambda: c)
    assert cli.main(["compile-rules", "--all", "--dry-run"]) == 0
    assert "要编 5 ·" in _total_line(capsys.readouterr().out) and jev.calls == []
    assert cli.main(["compile-rules", "--all"]) == 0
    assert sorted(_sent_ids(jev)) == sorted(g["id"] for g in globs)
    capsys.readouterr()
    jev.calls.clear()
    assert cli.main(["compile-rules", "--all", "--force"]) == 0
    sent = _sent_ids(jev)
    assert len(sent) == 5 and set(sent) == {g["id"] for g in globs}, "--force 也是一次运行每条只发一次"
    out = capsys.readouterr().out
    assert "这次编进 5 ·" in _total_line(out) and "前面的项目已经编过" in out
    assert len(c.rows["rule_compiles"]) == 5


def test_seen_does_not_swallow_a_rule_that_failed_in_an_earlier_project(jev):
    """seen 只收真写进库的: 前一个项目没编成的, 后一个项目照发。"""
    g = mem(1, scope="global", project_id=None)
    c = _three_projects([g])
    seen: set = set()
    jev.compile_status = 502
    assert core.compile_rules(c, PROJ, seen=seen)["status"] == "jev_failed" and seen == set()
    jev.compile_status = 200
    rep = core.compile_rules(c, PROJ2, seen=seen)
    assert rep["status"] == "ok" and rep["compiled"] == 1 and seen == {(g["id"], jev_sha(g["content"]))}


def test_dry_run_reports_what_the_real_run_would_hit(jev, monkeypatch, capsys):
    """原来 dry-run 在查 JUDGE_* 和 tv-map 之前就回了: 没接 tv-map 的项目 dry-run 打 ✓ 退出 0, 真编却是 ✗ not_configured。"""
    c = make_client([mem(1)], maps=False)
    monkeypatch.setattr(core, "sb", lambda: c)
    assert cli.main(["compile-rules", "--project", PROJ, "--dry-run"]) == 1
    out = capsys.readouterr().out
    assert "✗" in out and "not_configured" in out and "tv-map" in out and jev.calls == []
    c2 = make_client([mem(1)])
    monkeypatch.setattr(core, "sb", lambda: c2)
    monkeypatch.setattr(config, "JUDGE_URL", "")
    assert cli.main(["compile-rules", "--project", PROJ, "--dry-run"]) == 1
    assert "not_configured" in capsys.readouterr().out and jev.calls == []


def test_a_policy_block_in_one_project_does_not_hold_global_rules_elsewhere(jev):
    """owner 有一个处方药项目和一个普通项目, 加一条 global 规则。处方药项目先编、整批被出境口径挡下: 原来批里每条规则都
    按 (id, sha) 记失败(进程级, 不分项目), 普通项目的后台补编随后把这条 global 规则当「刚失败过」扣 6 小时不编。"""
    g = mem(1, scope="global", project_id=None)
    c = FakeClient(rows={
        "projects": [_project(PROJ, "示例品牌-产品向"), _project(RX_PROJ, "示例处方药项目")],
        "memories": [g], "rule_compiles": [],
        "tv_project_map": [{"tv_project_id": TV, "project_id": PROJ, "ingest_target": True},
                           {"tv_project_id": "DEMO_rx", "project_id": RX_PROJ, "ingest_target": True}],
        "truth_vault.projects": [{"project_id": TV, "category": "示例品类"},
                                 {"project_id": "DEMO_rx", "category": core._RX_CATEGORY}],
        "versions": []})
    jev.compile_status_fn = lambda body: 403 if body.get("category") == core._RX_CATEGORY else 200
    jev.compile_detail = "policy: 示例处方药项目没清出境"
    rep = core.compile_rules(c, RX_PROJ, skip_recent_failures=True)
    assert rep["status"] == "policy_blocked" and core._RULE_COMPILE_FAILED == {}
    rep = core.compile_rules(c, PROJ, skip_recent_failures=True)
    assert rep["status"] == "ok" and rep["to_compile"] == 1 and "held_after_failure" not in rep
    assert len(jev.compile_calls()) == 2 and [r["memory_id"] for r in c.rows["rule_compiles"]] == [g["id"]]


def test_per_rule_failures_are_still_held(jev):
    """按规则记的那几种(超时 / Jev 失败 / 单条 error)照旧记: 别让后台每次都去付一遍钱。"""
    c = make_client([mem(1)])
    jev.compile_status = 502
    assert core.compile_rules(c, PROJ)["status"] == "jev_failed"
    rep = core.compile_rules(c, PROJ, skip_recent_failures=True)
    assert rep["status"] == "up_to_date" and rep["held_after_failure"] == 1


def test_worker_holds_the_project_after_a_timeout(monkeypatch, blocking_compile):
    gate, calls = blocking_compile
    gate.set()
    monkeypatch.setattr(core, "compile_rules", lambda client, pid, **kw: (calls.append(kw) or {
        "status": "partial", "stopped_by": "timeout", "pool": 70, "cached": 0, "compiled": 60, "errors": []}))
    c = make_client([mem(1)])
    assert core._kick_rule_compile(c, PROJ, reason="open_project") == "started"
    _wait_idle(PROJ)
    st = core._RULE_COMPILE_STATE[PROJ]
    assert st["last_status"] == "timeout" and st["hold_until"] > time.monotonic() + core.RULE_COMPILE_TIMEOUT_HOLD_SEC - 60
    st["last"] -= core.RULE_COMPILE_COOLDOWN_SEC + 1                    # 冷却过了
    assert core._kick_rule_compile(c, PROJ, reason="open_project") == "held", "Jev 慢成这样, 后台一阵子不再去撞"


def test_a_rule_deleted_mid_compile_does_not_throw_away_the_rest(jev, monkeypatch):
    """一批是一条 INSERT … ON CONFLICT: 编的几分钟里有一条规则被硬删, 外键让整条失败。原来整批已付钱的结果全丢、
    status=error, 也不记失败 —— 下一次触发把另外几条再编(再付)一遍。"""
    rules = [mem(i) for i in range(1, 4)]
    c = make_client(rules)
    victim = rules[1]["id"]
    plain = jev.compile_one

    def one(r):
        if r["id"] == victim:                                           # AW 页上硬删
            c.rows["memories"] = [m for m in c.rows["memories"] if m["id"] != victim]
        return plain(r)
    jev.compile_one = one
    real_upsert = store.rule_compiles_upsert

    def fk_upsert(sb, rows):
        alive = {m["id"] for m in sb.rows["memories"]}
        if any(r["memory_id"] not in alive for r in rows):
            raise RuntimeError('insert or update on table "rule_compiles" violates foreign key constraint')
        return real_upsert(sb, rows)
    monkeypatch.setattr(store, "rule_compiles_upsert", fk_upsert)
    rep = core.compile_rules(c, PROJ)
    assert rep["status"] == "partial" and rep["compiled"] == 2 and rep["verdicts"] == {"questions": 2}
    assert {r["memory_id"] for r in c.rows["rule_compiles"]} == {rules[0]["id"], rules[2]["id"]}
    assert [e["memory_id"] for e in rep["errors"]] == [victim] and "被删" in rep["errors"][0]["error"]
    assert core._RULE_COMPILE_FAILED == {}, "规则没了, 不用记失败"


def test_a_write_that_keeps_failing_holds_those_rules_in_the_background(jev, monkeypatch):
    c = make_client([mem(1), mem(2)])

    def broken(sb, rows):
        raise RuntimeError("库抖了一下")
    monkeypatch.setattr(store, "rule_compiles_upsert", broken)
    rep = core.compile_rules(c, PROJ)
    assert rep["status"] == "error" and "写不进" in rep["detail"] and rep["compiled"] == 0
    rep = core.compile_rules(c, PROJ, skip_recent_failures=True)
    assert rep["status"] == "up_to_date" and rep["held_after_failure"] == 2 and len(jev.compile_calls()) == 1, \
        "编出来却没写进去的, 后台不马上再付一遍钱"


# ── 读超时之后整个进程都停(2026-10-20 第二轮复审) ─────────────────────────────────

def _jev_times_out_on_compile(jev, monkeypatch) -> None:
    """/compile_rules 一律读超时(Jev 那边多半还在编、照样计费), 别的路径照常。"""
    plain = jev.post

    def post(url, **kw):
        if url.endswith("/compile_rules"):
            with jev.lock:
                jev.calls.append({"url": url, "headers": kw.get("headers"), "json": kw.get("json"),
                                  "timeout": kw.get("timeout")})
            raise requests.Timeout("read timed out (synthetic)")
        return plain(url, **kw)
    monkeypatch.setattr(requests, "post", post)


def _own_rules_and_a_global():
    g = mem(1, scope="global", project_id=None)
    return g, [g] + [mem(10 + i, project_id=p) for i, p in enumerate((PROJ, PROJ2, PROJ3))]


def test_all_stops_at_the_first_read_timeout_and_names_the_projects_not_tried(jev, monkeypatch, capsys):
    """原来 --all 在一个项目读超时之后接着发下一个项目: 每个项目一次 420 秒的读超时, Jev 那边每批照编照收钱, 结果没人收;
    owner 的 global 规则每个项目各发一遍(seen 只记写进库的)。一个项目里早就是读超时就停 —— --all 跨项目也要停。"""
    g, rules = _own_rules_and_a_global()
    c = _three_projects(rules)
    monkeypatch.setattr(core, "sb", lambda: c)
    _jev_times_out_on_compile(jev, monkeypatch)
    assert cli.main(["compile-rules", "--all"]) == 1
    assert len(jev.compile_calls()) == 1, "读超时之后别的项目一批都不发"
    assert _sent_ids(jev).count(g["id"]) == 1
    out = capsys.readouterr().out
    tried = [line for line in out.splitlines() if line.startswith("✗ ")]
    assert len(tried) == 1 and "[timeout]" in tried[0]
    names = {p["id"]: p["name"] for p in c.rows["projects"]}
    not_tried = [pid for pid in names if pid not in tried[0]]
    assert len(not_tried) == 2
    stop = next(line for line in out.splitlines() if "这次没试" in line)
    assert all(names[pid] in stop and pid in stop for pid in not_tried), "列出没试的项目"
    assert "2 个项目这次没试" in _total_line(out)


def test_a_timed_out_batch_counts_as_sent_for_the_rest_of_the_run(jev, monkeypatch):
    """--all 共用的 seen: 读超时那批 Jev 多半还在编、照样计费, 同一次运行里不再发。"""
    g, rules = _own_rules_and_a_global()
    c = _three_projects(rules)
    _jev_times_out_on_compile(jev, monkeypatch)
    seen: set = set()
    rep = core.compile_rules(c, PROJ, seen=seen)
    assert rep["status"] == "timeout"
    sent = {(r["id"], jev_sha(r["text"])) for r in jev.compile_calls()[0]["json"]["rules"]}
    assert (g["id"], jev_sha(g["content"])) in sent and seen == sent


def test_a_read_timeout_holds_background_compiles_for_every_project(jev, monkeypatch):
    """原来后台的读超时 hold 只记在那一个项目上: 排队等着的别的项目一拿到槽位就发, 一个接一个读超时(每个 7 分钟、
    每个都在 Jev 那边计费)。现在任何一次编译读超时, 整个进程 RULE_COMPILE_TIMEOUT_HOLD_SEC 之内不再给任何项目发。"""
    monkeypatch.setattr(config, "RULE_COMPILE_AUTO", True)
    _g, rules = _own_rules_and_a_global()
    c = _three_projects(rules)
    _jev_times_out_on_compile(jev, monkeypatch)
    pids = (PROJ, PROJ2, PROJ3)
    with core._RULE_COMPILE_SLOTS:                     # 部署后第一轮: 三个项目同时触发, 排着队等槽位
        for p in pids:
            assert core._kick_rule_compile(c, p, reason="open_project") == "started"
    for p in pids:
        _wait_idle(p)
    assert len(jev.compile_calls()) == 1, "第一个读超时之后, 排着队的项目不再发"
    for p in pids:
        core._RULE_COMPILE_STATE[p]["last"] -= core.RULE_COMPILE_COOLDOWN_SEC + 1           # 冷却过了
    other = next(p for p in pids if core._RULE_COMPILE_STATE[p]["last_status"] != "timeout")
    assert core._kick_rule_compile(c, other, reason="open_project") == "jev_slow"
    assert core._kick_rule_compile(c, other, reason="record_rule", bypass_cooldown=True) == "jev_slow", \
        "刚记的规则也一样: Jev 慢下来不是规则的事, 发过去一样读超时、一样计费"
    assert len(jev.compile_calls()) == 1
    out = core.check_rules(c, other, ["cccccccc-0000-4000-8000-0000000000c1"], user_id=ME)
    assert out["rules"]["uncompiled_reason"] == "jev_slow" and "Jev 超时" in out["next_step"]
    core._RULE_COMPILE_JEV["slow_until"] = time.monotonic() - 1                                # 停够了
    assert core._kick_rule_compile(c, other, reason="open_project") == "started"
    _wait_idle(other)


# ── 文档(复审 2026-10-20: 下面三处原来和代码对不上) ─────────────────────────────

_REPO = __import__("pathlib").Path(__file__).resolve().parent.parent


def _runbook_1_11() -> str:
    text = (_REPO / "docs" / "deskcore-runbook.md").read_text(encoding="utf-8")
    start = text.index("### 1.11 `015_rule_compiles`")
    return text[start:text.index("\n### ", start + 10)]


def test_migrations_readme_says_check_rules_errors_without_015():
    row = next(line for line in (_REPO / "migrations" / "README.md").read_text(encoding="utf-8").splitlines()
               if line.startswith("| `015_rule_compiles.sql`"))
    broken = row.rsplit("|", 2)[-2]                                   # 「不跑会怎样」那一格
    # 第三轮复审: 原来是「报错」(REST 500 / MCP 工具错误、没有日志), 现在回 status=error、记日志
    assert "`check_rules` 回 `status: error`" in broken and "不是 500" in broken
    assert "只能说「规则还没编好」" not in broken
    assert "读缓存" in broken and "不花编译的钱" in broken


def test_runbook_counts_distinct_rules_from_the_pool():
    sec = _runbook_1_11()
    assert "count(distinct m.id) as rules" in sec and "count(*) as rules" not in sec
    assert "count(distinct c.memory_id) as compiled" in sec
    assert "m.memory_type is null or m.memory_type = 'rule'" in sec and "m.muted_until" in sec


def test_rollout_turns_background_compile_off_while_the_cli_backfills():
    sec = _runbook_1_11()
    off = sec.index("RULE_COMPILE_AUTO=0")
    assert off < sec.index("compile-rules --all --dry-run") < sec.index("跑完去掉 `RULE_COMPILE_AUTO`")
    assert "不要对线上跑 `compile-rules`" in sec
    doc = (_REPO / "docs" / "deskcore.md").read_text(encoding="utf-8")
    assert "**这个进程里**不会编两遍" in doc and "先设 `RULE_COMPILE_AUTO=0`" in doc


# ══════════════════════════════════════════════════════════════════════
# 复审(2026-10-20 第三轮)
# ══════════════════════════════════════════════════════════════════════

def test_results_from_a_mock_jev_are_never_cached(jev):
    """JUDGE_MOCK=1 的 Jev(测试 / 本地起的)回 200、model 是 mock、按关键词给假结果。原来照真的存: 缓存键是 (规则 id, sha),
    不带 model, 以后 JUDGE_URL 指回真的 Jev 这些假题、假代码照样一直命中, 只有 --force 换得掉。"""
    jev.model = "mock"
    c = make_client([mem(i) for i in range(1, 71)])
    rep = core.compile_rules(c, PROJ)
    assert rep["status"] == "jev_mock" and rep["compiled"] == 0 and c.rows["rule_compiles"] == []
    assert len(jev.compile_calls()) == 1 and rep["not_sent"] == 10 and rep["stopped_by"] == "jev_mock", \
        "后面几批一样是假的, 不发"
    assert "mock" in rep["detail"] and rep["batch_errors"][0]["status"] == "jev_mock"
    assert core._RULE_COMPILE_FAILED == {}, "不是规则的事, 不按规则扣 6 小时"
    assert core._RULE_COMPILE_JEV["slow_until"] == 0.0
    jev.model = FAKE_COMPILER                                     # JUDGE_URL 指回真的: 照常编, 照常存
    rep = core.compile_rules(c, PROJ)
    assert rep["status"] == "ok" and rep["compiled"] == 70


def test_a_mock_jev_holds_the_project_and_says_so(jev, monkeypatch):
    monkeypatch.setattr(config, "RULE_COMPILE_AUTO", True)
    jev.model = "mock"
    c = make_client([mem(1)], versions=[])
    assert core._kick_rule_compile(c, PROJ, reason="open_project") == "started"
    _wait_idle(PROJ)
    st = core._RULE_COMPILE_STATE[PROJ]
    assert st["last_status"] == "jev_mock" and st["hold_until"] > time.monotonic() + core.RULE_COMPILE_HOLD_SEC - 60
    assert c.rows["rule_compiles"] == []
    st["last"] -= core.RULE_COMPILE_COOLDOWN_SEC + 1
    assert core._uncompiled_reason(c, PROJ, [(mid(1), jev_sha(mem(1)["content"]))], "held")[0] == "jev_mock"


def _jev_cannot_connect_on_compile(jev, monkeypatch) -> None:
    """/compile_rules 一律连接超时(3 秒没连上: 请求一个字节都没发出去, Jev 没在编、没计费)。"""
    plain = jev.post

    def post(url, **kw):
        if url.endswith("/compile_rules"):
            with jev.lock:
                jev.calls.append({"url": url, "headers": kw.get("headers"), "json": kw.get("json"),
                                  "timeout": kw.get("timeout")})
            raise requests.ConnectTimeout("connect timed out (synthetic, nothing sent)")
        return plain(url, **kw)
    monkeypatch.setattr(requests, "post", post)


def test_a_connect_timeout_is_not_handled_like_a_billed_read_timeout(jev, monkeypatch, capsys):
    """ConnectTimeout 同时是 Timeout 的子类: 原来落进读超时那一支, 详情写「420s 内没回」, 当成「Jev 多半还在编、照样计费」
    —— 整个进程的后台补编停 30 分钟、整批规则扣 6 小时、compile-rules --all 整个停下。可连接超时时 Jev 什么都没收到。"""
    g, rules = _own_rules_and_a_global()
    c = _three_projects(rules)
    monkeypatch.setattr(core, "sb", lambda: c)
    _jev_cannot_connect_on_compile(jev, monkeypatch)
    rep = core.compile_rules(c, PROJ)
    assert rep["status"] == "unavailable" and "没发出去" in rep["detail"] and "内没回" not in rep["detail"]
    assert core._RULE_COMPILE_JEV["slow_until"] == 0.0, "不停整个进程"
    assert core._RULE_COMPILE_FAILED == {}, "不是规则的事, 不按规则扣 6 小时"
    jev.calls.clear()
    assert cli.main(["compile-rules", "--all"]) == 1, "没编成照样退出码 1"
    out = capsys.readouterr().out
    assert "这次没试" not in out and "照样计费" not in out
    assert len([line for line in out.splitlines() if line.startswith("✗ ")]) == 3, "--all 接着试下一个项目"
    monkeypatch.setattr(config, "RULE_COMPILE_AUTO", True)
    assert core._kick_rule_compile(c, PROJ2, reason="record_rule", bypass_cooldown=True) == "started"
    _wait_idle(PROJ2)


def test_the_writer_side_judge_still_calls_a_connect_timeout_a_timeout(jev, monkeypatch):
    """写手侧(judge_draft_full)照旧记 timeout(「这次慢了」, 同 judge_draft), 只是详情不再说「N 秒内没回」。"""
    import judge_client
    jev.raise_exc = requests.ConnectTimeout("connect timed out (synthetic)")
    res = judge_client.judge_draft_full({"subject_id": "x"}, timeout=8)
    assert res["judge_status"] == judge_client.JUDGE_TIMEOUT
    assert "没发出去" in res["detail"] and "内没回" not in res["detail"]
    res = judge_client.compile_rules({"project": TV, "rules": []})
    assert res["judge_status"] == judge_client.JUDGE_UNAVAILABLE


def test_a_compile_cut_off_by_shutdown_is_logged(jev, monkeypatch, caplog):
    """后台补编是 daemon 线程: 进程退出(Railway 重新部署, 改一个变量就重新部署)时在路上的那一批被杀在半路, Jev 照样编完、
    计费, 结果没存, 新进程再发一遍。原来一行日志都没有。现在每发一批记一行 INFO, app 收尾时对在路上的那批记 WARNING。"""
    import anyio
    A = pytest.importorskip("deskcore.app")      # CI 的 smoke 不装 fastapi
    monkeypatch.setattr(config, "RULE_COMPILE_AUTO", True)
    gate = threading.Event()
    compile_one = jev.compile_one
    jev.compile_one = lambda r: (gate.wait(5), compile_one(r))[1]
    c = make_client([mem(1), mem(2)])
    with caplog.at_level("INFO", logger="deskcore"):
        try:
            assert core._kick_rule_compile(c, PROJ, reason="open_project") == "started"
            end = time.monotonic() + 5
            while PROJ not in core._RULE_COMPILE_INFLIGHT and time.monotonic() < end:
                time.sleep(0.01)
            assert core._RULE_COMPILE_INFLIGHT[PROJ]["rules"] == 2
            monkeypatch.setattr(A, "_mcp", None)

            async def lifespan():
                async with A._lifespan(A.app):
                    pass
            anyio.run(lifespan)
        finally:
            gate.set()
        _wait_idle(PROJ)
    msgs = [(r.levelname, r.getMessage()) for r in caplog.records]
    assert any(lv == "INFO" and "发一批 2 条" in m and PROJ in m for lv, m in msgs), "发之前记一行"
    cut = [m for lv, m in msgs if lv == "WARNING" and "被进程退出打断" in m]
    assert len(cut) == 1 and PROJ in cut[0] and "计费" in cut[0]
    assert core._RULE_COMPILE_INFLIGHT == {} and len(c.rows["rule_compiles"]) == 2
    assert core.log_rule_compiles_cut_off() == [], "编完了就不在路上了"


# ══════════════════════════════════════════════════════════════════════
# 独立复审 on aw E: hold 不清、没计费的生成端过载扣 6 小时
# ══════════════════════════════════════════════════════════════════════

def test_a_successful_round_clears_the_project_hold(monkeypatch, blocking_compile):
    """Jev 重新部署那一阵回 503(unavailable): 项目 hold 1 小时。几分钟后 Jev 好了, record_rule(不等 hold)那一轮编成了
    (ok)。原来 hold_until 只在 hold 时设、从不清: 之后在 AW 页改的规则、set_rule_state 改的方向照样被扣到 hold 到期,
    check_rules 还说「上次整批没编成(ok)」。"""
    gate, calls = blocking_compile
    gate.set()
    statuses = iter(["unavailable", "up_to_date", "ok", "ok"])
    monkeypatch.setattr(core, "compile_rules", lambda client, pid, **kw: (calls.append(kw) or
                                                                          {"status": next(statuses), "errors": []}))
    c = make_client([mem(1)])
    missing = [(mid(1), jev_sha(mem(1)["content"]))]

    def cooled():
        _wait_idle(PROJ)
        core._RULE_COMPILE_STATE[PROJ]["last"] -= core.RULE_COMPILE_COOLDOWN_SEC + 1     # 冷却过了
    assert core._kick_rule_compile(c, PROJ, reason="open_project") == "started"
    cooled()
    assert core._kick_rule_compile(c, PROJ, reason="open_project") == "held"
    # 一轮什么都没发(up_to_date): 说明不了 Jev 好了, hold 照旧; 说原因时说起 hold 的那一轮, 不是被盖掉的 last_status
    assert core._kick_rule_compile(c, PROJ, reason="record_rule", bypass_cooldown=True) == "started"
    cooled()
    assert core._kick_rule_compile(c, PROJ, reason="open_project") == "held"
    reason, why, _ = core._uncompiled_reason(c, PROJ, missing, "held")
    assert reason == "held" and "(unavailable)" in why and "up_to_date" not in why
    # record_rule 那一轮真编成了: hold 清掉, 改过的规则下一次触发就编
    assert core._kick_rule_compile(c, PROJ, reason="record_rule", bypass_cooldown=True) == "started"
    cooled()
    assert core._RULE_COMPILE_STATE[PROJ]["hold_until"] == 0.0
    assert core._kick_rule_compile(c, PROJ, reason="check_rules") == "started", "Jev 好了, 项目不再 hold"
    _wait_idle(PROJ)
    assert len(calls) == 4


# Jev 的原样: compile_batch 写「生成端调用失败：{type(exc).__name__}: {exc}」, GeneratorHTTPError 是「HTTP 529 Overloaded：…」;
# rules_api 整批都失败时回 502「规则一条都没编出来：{results[0]['error']}」
OVERLOADED = '生成端调用失败：GeneratorHTTPError: HTTP 529 Overloaded：{"type":"error","error":{"type":"overloaded_error"}}'
REFUSED = "生成端调用失败：GeneratorStopped: 模型拒答（stop_reason=refusal）"                  # 已计费
GATEWAY = "生成端调用失败：GeneratorHTTPError: HTTP 504 Gateway Timeout：upstream timed out"   # 上游可能已经计费


class _Later:
    """core 里的 time, 但 monotonic 往后拨 ``sec`` 秒。"""

    def __init__(self, sec: float):
        self.sec = sec

    def __getattr__(self, name):
        return getattr(time, name)

    def monotonic(self) -> float:
        return time.monotonic() + self.sec


@pytest.mark.parametrize("error, retried", [(OVERLOADED, True), (REFUSED, False), (GATEWAY, False)],
                         ids=["overloaded", "refused", "gateway-timeout"])
@pytest.mark.parametrize("how", ["502", "200"])
def test_a_free_generator_overload_is_retried_after_half_an_hour_not_six(jev, monkeypatch, error, retried, how):
    """部署后各项目一起编, Jev 生成端 529 过载(重试一次还是): 没出输出、没计费。原来和花了钱的失败一样按规则扣 6 小时,
    整个池子半天没检查。现在只扣 RULE_COMPILE_TRANSIENT_RETRY_SEC; 拒答 / 网关超时(可能已经计费)照旧 6 小时。"""
    c = make_client([mem(1)])
    if how == "502":
        jev.compile_status, jev.compile_detail = 502, "规则一条都没编出来：" + error
    else:
        jev.compile_one = lambda r: {"id": r["id"], "sha": jev_sha(r["text"]), "verdict": None, "code": None,
                                     "questions": [], "coverage": None, "uncovered": "", "why": "", "error": error}
    assert core.compile_rules(c, PROJ, skip_recent_failures=True)["status"] in ("jev_failed", "error")
    jev.compile_status, jev.compile_one = 200, FakeJev().compile_one             # Jev 好了
    monkeypatch.setattr(core, "time", _Later(31 * 60))                          # 半小时多一点之后
    rep = core.compile_rules(c, PROJ, skip_recent_failures=True)
    if retried:
        assert rep["status"] == "ok" and rep["compiled"] == 1 and len(jev.compile_calls()) == 2
    else:
        assert rep["status"] == "up_to_date" and rep["held_after_failure"] == 1 and len(jev.compile_calls()) == 1


def test_global_activation_uses_an_owned_mapped_project(monkeypatch):
    kicks = []
    monkeypatch.setattr(core, '_kick_rule_compile', lambda client, pid, **kw: kicks.append(pid))
    projects = [{'id': OTHER_PROJ, 'name': 'A unmapped', 'owner_id': ME},
                {'id': PROJ, 'name': 'B mapped', 'owner_id': ME}]
    rule = mem(19, status='candidate', scope='global')
    core.set_rule_state(make_client([rule], projects=projects), rule['id'], 'promote', user_id=ME)
    assert kicks == [PROJ]


def test_global_activation_without_an_owned_mapped_project_starts_nothing(monkeypatch):
    kicks = []
    monkeypatch.setattr(core, '_kick_rule_compile', lambda client, pid, **kw: kicks.append(pid))
    rule = mem(19, status='candidate', scope='global')
    core.set_rule_state(make_client([rule], maps=False), rule['id'], 'promote', user_id=ME)
    assert kicks == []
