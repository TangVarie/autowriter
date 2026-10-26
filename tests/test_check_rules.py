"""check_rules(2026-10-20, D-121): 存完档之后, 按 Jev 编好的规则逐篇查, 只作建议。

钉的是:

  · 归属: 别人的项目 403; 别的项目的 / 不存在的 / 不是 UUID 的版本不查、不发, 列进 excluded;
  · 池子同简报: soft 规则过方向闸, 标了另一个方向的题不进题库;
  · 发给 Jev 的请求: 只带内联题库(banks=[]、desk_rules、shadow-rules、不判段、write=true、subject_id=versions.id),
    题不进 hard_rules / brief;
  · 读答案: hard → 必须改, soft → 建议改; 歧义或 p < 0.7 → 拿不准; 带 invalid_reason 的(evidence_not_found 也是,
    同 Jev 的契约)→ 没判出来;
  · 到点没回来 → 没判完, 不算守住; 代码查的结果照样有效;
  · 编出来的 code 只在规则没有人设 rule_kind 时跑, 不安全的正则不跑;
  · 没有可查的规则 → 一句话, 什么都不发; 有没编的规则 → 报个数、触发后台补编;
  · 查的是交付出去的那一份: 模板管话题时 Jev 判的、代码查的正文都摘掉了末尾那行话题(delivery.delivered_post);
    方向闸同简报和 score_drafts, 按项目名 + 写手传回来的 tactic 判。

所有规则、题目、稿子都是合成的示例。
"""

from __future__ import annotations

import json
import time
import uuid

import pytest

import config
from deskcore import core, delivery, rulecheck, rulecompile, tools
# jev / _fresh_compile_state 是那边的夹具(后者 autouse: 清后台补编的进程内状态、默认关掉后台补编), 引进来才生效
from tests.test_rule_compile import (ME, OTHER_PROJ, PROJ, _fresh_compile_state, code_result, jev,  # noqa: F401
                                     jev_sha, make_client, mem, question_result, stored)

V1 = "cccccccc-0000-4000-8000-0000000000c1"
V2 = "cccccccc-0000-4000-8000-0000000000c2"
V_THEIRS = "dddddddd-0000-4000-8000-0000000000d1"
TITLE = "示例标题: 一次合成的体验记录"
BODY = ("这是一段合成的正文, 只用来测试。第一句讲一件小事。第二句讲当时的感受。"
        "第三句说这个示例产品能治好所有问题。第四句收个尾。") * 2


def version(vid: str, project_id: str = PROJ, *, title: str = TITLE, body: str = BODY) -> dict:
    return {"id": vid, "title": title, "body": body,
            "items": {"id": f"item-{vid[:8]}", "batches": {"project_id": project_id}}}


# 规则池: 名字带「产品向」的项目 → 方向是产品向
R_HARD = mem(1, "示例硬规则: 不能写疗效", severity="hard")
R_SOFT = mem(2, "示例软规则: 要写出持续了多久")
R_OTHER_DIR = mem(3, "示例软规则: 不提品牌名", applicability="流量向")       # 方向闸挡掉
R_CODE = mem(4, "示例软规则: 标题不用「示例禁词」")
R_HANDSET = mem(5, "示例硬规则: 正文不用「人设禁词」", severity="hard", rule_kind="forbidden_word",
                rule_payload={"words": ["人设禁词"], "where": ["body"]})
R_UNSAFE = mem(6, "示例软规则: 不要连着笑")
R_NOT_CHECKABLE = mem(7, "示例规则: 交付文件按日期命名")
R_UNCOMPILED = mem(8, "示例规则: 刚记下还没编")


def q_hard(rule=R_HARD):
    sha = jev_sha(rule["content"], rule.get("applicability"))
    return question_result(rule["id"], sha, ask="正文有没有把示例产品说成能治好病？", pass_if="否", scope="body",
                           note="不能写疗效")


def q_soft(rule=R_SOFT):
    sha = jev_sha(rule["content"], rule.get("applicability"))
    return question_result(rule["id"], sha, ask="正文有没有写出持续了多久？", pass_if="是", scope="body",
                           note="写出持续时间")


def q_other_dir():
    sha = jev_sha(R_OTHER_DIR["content"], R_OTHER_DIR["applicability"])
    return question_result(R_OTHER_DIR["id"], sha, ask="正文有没有提到品牌名？", pass_if="否", scope="body")


def qid_of(res: dict) -> str:
    return res["questions"][0]["qid"]


def full_client(*, memories=None, compiles=None, versions=None):
    mems = memories if memories is not None else [R_HARD, R_SOFT, R_OTHER_DIR, R_CODE, R_HANDSET, R_UNSAFE,
                                                  R_NOT_CHECKABLE, R_UNCOMPILED]
    sha = lambda r: jev_sha(r["content"], r.get("applicability"))                     # noqa: E731
    comp = compiles if compiles is not None else [
        stored(R_HARD, q_hard()), stored(R_SOFT, q_soft()), stored(R_OTHER_DIR, q_other_dir()),
        stored(R_CODE, code_result(R_CODE["id"], sha(R_CODE),
                                   {"kind": "forbidden_word", "words": ["示例禁词"], "where": ["title"]})),
        stored(R_HANDSET, code_result(R_HANDSET["id"], sha(R_HANDSET),
                                      {"kind": "forbidden_word", "words": ["合成"], "where": ["body"]})),
        stored(R_UNSAFE, code_result(R_UNSAFE["id"], sha(R_UNSAFE),
                                     {"kind": "forbidden_regex", "pattern": "(哈|哈哈)+$", "where": ["body"]})),
        stored(R_NOT_CHECKABLE, {"id": R_NOT_CHECKABLE["id"], "sha": sha(R_NOT_CHECKABLE), "verdict": "not_checkable",
                                 "code": None, "questions": [], "coverage": None, "uncovered": "", "why": "交付"}),
    ]
    vers = versions if versions is not None else [version(V1), version(V2, title="示例禁词也在这个标题里"),
                                                  version(V_THEIRS, OTHER_PROJ)]
    return make_client(mems, comp, versions=vers)


def answers(**by_qid):
    """{qid: item} → 一个 /judge_draft 的回包工厂(每篇同样的答案)。"""
    return lambda payload: {"subject_id": payload["subject_id"], "passed": True, "plan": [],
                            "detail": {"desk_rules": dict(by_qid)}, "written": len(by_qid), "write_error": None}


@pytest.fixture
def kicks(monkeypatch):
    got: list[tuple] = []
    monkeypatch.setattr(core, "_kick_rule_compile",
                        lambda client, pid, **kw: got.append((pid, kw.get("reason"))) or "started")
    return got


@pytest.fixture
def wired(jev, kicks, monkeypatch):           # noqa: F811 — jev 是 test_rule_compile 里的夹具
    monkeypatch.setattr(config, "JUDGE_DRAFT_TOOL_TIMEOUT_SEC", 18.0)
    jev.judge = answers(**{
        qid_of(q_hard()): {"answer": "是", "p": 0.91, "ambiguous": False, "evidence": "第三句说这个示例产品能治好所有问题。"},
        qid_of(q_soft()): {"answer": "否", "p": 0.83, "ambiguous": False},
    })
    return jev


# ══════════════════════════════════════════════════════════════════════
# 1 · 正常一轮
# ══════════════════════════════════════════════════════════════════════

def test_hard_is_must_fix_soft_is_suggest_and_code_runs(wired, kicks):
    c = full_client()
    out = core.check_rules(c, PROJ, [V1, V2], user_id=ME)
    assert out["status"] == "checked"
    v1, v2 = out["versions"]
    assert v1["version_id"] == V1 and v1["judge_status"] == "ok"
    hard = [e for e in v1["violations"] if e["rule_id"] == R_HARD["id"]]
    assert hard == [{"rule_id": R_HARD["id"], "severity": "hard", "source": "jev", "p": 0.91,
                     "what": "必须改: 不能写疗效(「正文有没有把示例产品说成能治好病？」判了「是」)",
                     "evidence": "第三句说这个示例产品能治好所有问题。"}]
    soft = [e for e in v1["violations"] if e["rule_id"] == R_SOFT["id"]]
    assert len(soft) == 1 and soft[0]["what"].startswith("建议改: 写出持续时间") and "evidence" not in soft[0]
    assert v1["violations"][0]["severity"] == "hard", "必须改排前面"
    # 编出来的 code: 标题禁词只在 V2 的标题里
    code_hits = [e for e in v2["violations"] if e["source"] == "code"]
    assert [(e["rule_id"], e["severity"]) for e in code_hits] == [(R_CODE["id"], "soft")]
    assert code_hits[0]["what"].startswith("建议改: 标题里有禁用词「示例禁词」")
    assert not any(e["source"] == "code" for e in v1["violations"])
    assert out["summary"] == {"versions": 2, "must_fix": 2, "suggest": 3, "unsure": 0, "not_finished": 0, "unjudged": 0}
    assert v1["counts"] == {"must_fix": 1, "suggest": 1, "unsure": 0, "unjudged": 0}
    assert "必须改" in out["next_step"] and "replaces_version_id" in out["next_step"] and "不拦交付" in out["next_step"]
    # 池子: 8 条里方向闸挡掉 1 条
    assert out["rules"]["pool"] == 7 and out["rules"]["by_jev"] == 2 and out["rules"]["not_checkable"] == 1
    assert out["uncompiled_rules"] == 1 and kicks == [(PROJ, "check_rules")], "有没编的 → 报个数、后台补编"
    assert out["rules"]["uncompiled_reason"] == "compiling"
    assert "还有 1 条规则还没有编译结果(后台在编)" in out["next_step"]


def test_request_carries_only_the_rules_bank(wired):
    core.check_rules(full_client(), PROJ, [V1], user_id=ME)
    (call,) = [x for x in wired.calls if x["url"].endswith("/judge_draft")]
    body = call["json"]
    assert body["subject_id"] == V1 and body["subject_type"] == "aw_version"
    assert body["banks"] == [] and body["project_bank_name"] == "desk_rules" and body["run_tag"] == "shadow-rules"
    assert body["judge_paras"] == "never" and body["write"] is True and body["return_rows"] is False
    assert body["project"] == "DEMO_phase1" and body["category"] == "示例品类"
    assert body["title"] == TITLE and body["body"] == BODY
    assert "hard_rules" not in body and "brief" not in body, "规则题只作建议, 不进 passed"
    qids = [q["id"] for q in body["project_bank"]["questions"]]
    assert qids == [qid_of(q_hard()), qid_of(q_soft())], "流量向的那条被方向闸挡掉, 不进题库"
    assert call["headers"] == {"X-Judge-Key": "jk-admin"} and call["timeout"][1] <= 18.0


# ══════════════════════════════════════════════════════════════════════
# 2 · 读答案
# ══════════════════════════════════════════════════════════════════════

def test_ambiguous_and_low_confidence_are_unsure_not_violations(wired):
    wired.judge = answers(**{
        qid_of(q_hard()): {"answer": "是", "p": 0.58, "ambiguous": True, "evidence": "第三句。"},
        qid_of(q_soft()): {"answer": "是", "p": 0.66, "ambiguous": False},       # 偏向守住, 但把握不到 0.7
    })
    out = core.check_rules(full_client(memories=[R_HARD, R_SOFT],
                                       compiles=[stored(R_HARD, q_hard()), stored(R_SOFT, q_soft())]),
                           PROJ, [V1], user_id=ME)
    (v1,) = out["versions"]
    assert v1["violations"] == []
    assert [(e["rule_id"], e["what"][:4]) for e in v1["unsure"]] == [(R_HARD["id"], "拿不准:"), (R_SOFT["id"], "拿不准:")]
    assert v1["unsure"][0]["p"] == 0.58 and "把握 0.58" in v1["unsure"][0]["what"]
    assert out["summary"]["unsure"] == 2 and "拿不准" in out["next_step"]


def test_evidence_not_found_is_unjudged_not_a_violation(wired):
    """Jev README「规则编译 → 送回 /judge_draft」: invalid_reason 不为空的题当「没判出来」, 不报违反也不报守住;
    evidence_not_found(答了「是」却指不出依据句)在 TV 账本里 answer 是 NULL。原来这里照算违反: 硬规则报「必须改」,
    写手照一个账本里记成无效的答案去改稿, 还不知道改哪一句(2026-10-20 复审)。"""
    wired.judge = answers(**{
        qid_of(q_hard()): {"answer": "是", "p": 0.88, "ambiguous": False, "evidence": None,
                           "invalid_reason": "evidence_not_found"},
        qid_of(q_soft()): {"answer": None, "p": None, "ambiguous": False, "invalid_reason": "no_title"},
    })
    out = core.check_rules(full_client(memories=[R_HARD, R_SOFT],
                                       compiles=[stored(R_HARD, q_hard()), stored(R_SOFT, q_soft())]),
                           PROJ, [V1], user_id=ME)
    (v1,) = out["versions"]
    assert v1["violations"] == [] and v1["unsure"] == [], "无效的答案不报必须改"
    assert v1["counts"]["must_fix"] == 0 and out["summary"]["must_fix"] == 0
    assert v1["counts"]["unjudged"] == 2 and v1["unjudged"] == [
        {"rule_id": R_HARD["id"], "reason": "evidence_not_found"}, {"rule_id": R_SOFT["id"], "reason": "no_title"}], \
        "evidence_not_found / no_title 之类: 没判出来, 不报违反也不报守住"
    assert v1["judge_status"] == "not_judged", "两道题都没判出来, 和没判完一样"
    assert "必须改" not in out["next_step"] and "都守住了" not in out["next_step"]


def test_read_answers_unit():
    q = {"a_1": {"rule_id": "r1", "severity": "soft", "ask": "有没有写出时长？", "pass_if": "是", "note": ""}}
    got = rulecompile.read_answers(q, {"a_1": {"answer": "是", "p": 0.95, "ambiguous": False}})
    assert got == {"violations": [], "unsure": [], "unjudged": []}, "守住了就什么都不报"
    got = rulecompile.read_answers(q, {})
    assert got["unjudged"] == [{"rule_id": "r1", "reason": "missing"}]
    got = rulecompile.read_answers(q, {"a_1": {"answer": "否", "p": 0.9, "ambiguous": False,
                                               "invalid_reason": "evidence_not_found"}})
    assert got["unjudged"] and not got["violations"]
    hard = {"b_1": {"rule_id": "r2", "severity": "hard", "ask": "有没有写示例宣称？", "pass_if": "否", "note": ""}}
    got = rulecompile.read_answers(hard, {"b_1": {"answer": "是", "p": 0.95, "ambiguous": False, "evidence": None,
                                                  "invalid_reason": "evidence_not_found"}})
    assert got == {"violations": [], "unsure": [], "unjudged": [{"rule_id": "r2", "reason": "evidence_not_found"}]}, \
        "pass_if「否」答了「是」却没有依据句: 同 Jev 契约, 没判出来, 不是违反"


# ══════════════════════════════════════════════════════════════════════
# 3 · 时限
# ══════════════════════════════════════════════════════════════════════

def test_not_back_in_time_is_not_finished_never_passing(wired, monkeypatch):
    monkeypatch.setattr(config, "JUDGE_DRAFT_TOOL_TIMEOUT_SEC", 1.0)

    def slow(payload):
        time.sleep(1.6)
        return answers()(payload)
    wired.judge = slow
    t0 = time.monotonic()
    out = core.check_rules(full_client(), PROJ, [V1, V2], user_id=ME)
    assert time.monotonic() - t0 < 1.5, "整次共用一个截止, 不等慢的那几个"
    for v in out["versions"]:
        assert v["judge_status"] == "timeout" and v["judge_note"].startswith("没判完")
        assert not any(e["source"] == "jev" for e in v["violations"])
    assert out["summary"]["not_finished"] == 2 and "没判完" in out["next_step"]
    assert any(e["source"] == "code" for e in out["versions"][1]["violations"]), "代码查的照样有效"


def test_jev_not_configured_still_runs_code(kicks, monkeypatch):
    monkeypatch.setattr(config, "JUDGE_URL", "")
    calls = []
    monkeypatch.setattr(core.judge_client, "judge_draft_full", lambda *a, **k: calls.append(a) or {})
    out = core.check_rules(full_client(), PROJ, [V2], user_id=ME)
    (v2,) = out["versions"]
    assert v2["judge_status"] == "not_configured" and "没判成" in v2["judge_note"] and calls == []
    assert [e["rule_id"] for e in v2["violations"]] == [R_CODE["id"]]
    assert out["summary"]["not_finished"] == 1


# ══════════════════════════════════════════════════════════════════════
# 4 · 编出来的 code
# ══════════════════════════════════════════════════════════════════════

def test_compiled_code_runs_only_without_a_hand_set_check_and_unsafe_regex_is_refused(wired):
    body = BODY + "哈哈哈哈"
    out = core.check_rules(full_client(versions=[version(V1, body=body)]), PROJ, [V1], user_id=ME)
    (v1,) = out["versions"]
    ids = {e["rule_id"] for e in v1["violations"] if e["source"] == "code"}
    assert R_HANDSET["id"] not in ids, "人手设过 check 的规则, 编出来的 code 不跑(score_drafts 已经按人设的查过)"
    assert R_UNSAFE["id"] not in ids and out["rules"]["code_refused"] == 1, "会灾难性回溯的正则不跑"
    assert out["rules"]["by_code"] == 1, "没跑的那条不算「代码查了」"
    assert "1 条规则这次查不了" in out["next_step"]


# AW 页存得进去、rulecheck 不收的人设 check(第三轮复审): validator 放过、量词形状那道拒掉的正则; max_len 的「开头」老形状
UNRUNNABLE_HAND_SET = [("forbidden_regex", {"pattern": "(哈|哈哈)+"}), ("max_len", {"scope": "开头", "n": 20})]


@pytest.mark.parametrize("kind, payload, runs", [
    (None, None, True), ("", None, True), ("free_text", None, True), ("  free_text ", None, True),
    ("forbidden_word", {"words": ["人设禁词"], "where": ["body"]}, False), ("max_len", {"max": 20, "where": "title"}, False),
    ("forbidden_word", None, True), *[(k, p, True) for k, p in UNRUNNABLE_HAND_SET],
    # 写作台 record_rule 撤掉了代码检查(free_text + 标记; codex review on aw#115): 编出来的 code 也不跑
    ("free_text", dict(rulecheck.WITHDRAWN), False), ("free_text", json.dumps(rulecheck.WITHDRAWN), False),
])
def test_only_a_runnable_hand_set_check_suppresses_compiled_code(kind, payload, runs):
    """2026-10-20 复审: AW 记忆页存硬规则默认就是「自由文本」= free_text, 和没设分不开, 不能当成「不用代码查」;
    只有 rulecheck 真会跑的人设 check(score_drafts 已经查过)才让编出来的 code 让路。第三轮复审: rule_kind 对、payload
    跑不了的也不算 —— score_drafts 只把它记成 skipped, 编出来的 code 再让路, 这条规则就两边都没查。"""
    rule = mem(1, rule_kind=kind, rule_payload=payload)
    res = code_result(rule["id"], "0" * 12, {"kind": "forbidden_word", "words": ["示例"], "where": ["body"]})
    rules, unavailable = rulecompile.code_rules([(rule, res)])
    assert unavailable == []
    assert (len(rules) == 1) is runs
    if runs:
        assert rules[0]["rule_kind"] == "forbidden_word" and rules[0]["rule_payload"] == {"words": ["示例"], "where": ["body"]}


def test_free_text_hard_rule_from_the_aw_ui_gets_its_compiled_code_run(wired):
    rule = mem(11, "示例硬规则: 标题不许出现「示例禁词」", severity="hard", rule_kind="free_text")
    res = code_result(rule["id"], jev_sha(rule["content"]),
                      {"kind": "forbidden_word", "words": ["示例禁词"], "where": ["title"]})
    out = core.check_rules(full_client(memories=[rule], compiles=[stored(rule, res)],
                                       versions=[version(V2, title="示例禁词也在这个标题里")]), PROJ, [V2], user_id=ME)
    assert out["status"] == "checked" and out["rules"]["by_code"] == 1
    (v2,) = out["versions"]
    assert [e["what"] for e in v2["violations"]] == ["必须改: 标题里有禁用词「示例禁词」"]


def test_a_check_withdrawn_at_the_desk_does_not_run_its_compiled_code(wired):
    """写手用 record_rule 的 {"kind": "free_text"} 撤掉了这条的代码检查: score_drafts、生成链路都不查了, check_rules 里编出来的
    code 也不能再报「必须改」(codex review on aw#115: 撤掉的检查不能从别的路再长回来)。"""
    kind, payload = rulecheck.normalize_check({"kind": "free_text"})
    rule = mem(11, "示例硬规则: 标题不许出现「示例禁词」", severity="hard", rule_kind=kind, rule_payload=payload)
    res = code_result(rule["id"], jev_sha(rule["content"]),
                      {"kind": "forbidden_word", "words": ["示例禁词"], "where": ["title"]})
    out = core.check_rules(full_client(memories=[rule], compiles=[stored(rule, res)],
                                       versions=[version(V2, title="示例禁词也在这个标题里")]), PROJ, [V2], user_id=ME)
    assert out["rules"]["by_code"] == 0
    assert all(not v["violations"] for v in out["versions"])


def test_a_compiled_regex_that_runs_out_of_time_is_unchecked_not_held(wired, monkeypatch):
    """编出来的正则也走限时的引擎(validator.search_user_regex): 没查完的那条从停下的那篇起列进 code_unchecked, 不算违规,
    next_step 不能说「都守住了」(原来 check_rules 不看 timed_out, 一条超时的硬规则照算守住了)。"""
    monkeypatch.setattr(config, "REGEX_SEARCH_TIMEOUT_SEC", 0.1)
    slow = "a*aaa*aaa*aaa*aaa*aaa*aaa*aaa*b"
    rule = mem(12, "示例硬规则: 正文不许出现某种写法", severity="hard")
    res = code_result(rule["id"], jev_sha(rule["content"]),
                      {"kind": "forbidden_regex", "pattern": slow, "where": ["body"]})
    vs = [version(V1, body=BODY), version(V2, body=BODY + "a" * 2000)]
    out = core.check_rules(full_client(memories=[rule], compiles=[stored(rule, res)], versions=vs), PROJ, [V1, V2],
                           user_id=ME)
    assert out["rules"]["code_timed_out"] == 1
    by_id = {v["version_id"]: v for v in out["versions"]}
    assert "code_unchecked" not in by_id[V1], "停下之前那篇查完了"
    assert by_id[V2]["code_unchecked"] == [{"rule_id": rule["id"], "severity": "hard", "rule": rule["content"]}]
    assert not by_id[V2]["violations"] and out["summary"]["code_unchecked"] == 1
    assert "都守住了" not in out["next_step"] and "code_unchecked" in out["next_step"]


# ══════════════════════════════════════════════════════════════════════
# 5 · 归属与入参
# ══════════════════════════════════════════════════════════════════════

def test_someone_elses_project_is_refused(wired):
    with pytest.raises(PermissionError):
        core.check_rules(full_client(), OTHER_PROJ, [V_THEIRS], user_id=ME)
    assert wired.calls == []


def test_versions_outside_the_project_are_excluded_and_never_sent(wired):
    out = core.check_rules(full_client(), PROJ, [V1, V_THEIRS, "不是-uuid", V1.upper()], user_id=ME)
    assert [v["version_id"] for v in out["versions"]] == [V1], "大写的同一个 id 认成同一篇, 只查一次"
    assert {(e["version_id"], e["reason"]) for e in out["excluded"]} == {
        (V_THEIRS, "version_not_in_project"), ("不是-uuid", "version_not_in_project")}
    sent = [x["json"]["subject_id"] for x in wired.calls if x["url"].endswith("/judge_draft")]
    assert sent == [V1], "别的项目的版本一个字都不发"


def test_no_versions_of_this_project(wired):
    out = core.check_rules(full_client(), PROJ, [V_THEIRS], user_id=ME)
    assert out["status"] == "no_versions" and out["versions"] == [] and wired.calls == []


@pytest.mark.parametrize("bad", [[], "not-a-list", [V1, ""], [V1, 3]])
def test_bad_input_is_400_at_the_tool_layer(bad, monkeypatch, kicks):
    c = full_client()
    monkeypatch.setattr(core, "sb", lambda: c)
    with pytest.raises(tools.InvalidInput):
        tools.check_rules(PROJ, bad, _user_id=ME)


def test_tool_is_registered_with_identity():
    fn, needs_user = tools.TOOLS["check_rules"]
    assert fn is tools.check_rules and needs_user is True
    assert "_safe(" not in __import__("inspect").getsource(tools.check_rules), "不许包成看起来成功的结果"


# ══════════════════════════════════════════════════════════════════════
# 6 · 没东西可查
# ══════════════════════════════════════════════════════════════════════

def test_empty_pool_is_a_friendly_no_op(wired, kicks):
    out = core.check_rules(full_client(memories=[], compiles=[]), PROJ, [V1], user_id=ME)
    assert out["status"] == "nothing_to_check" and out["versions"] == [] and "没有可查的规则" in out["next_step"]
    assert wired.calls == [] and kicks == []


def test_everything_still_compiling_says_so(wired, kicks):
    out = core.check_rules(full_client(memories=[R_HARD, R_SOFT], compiles=[]), PROJ, [V1], user_id=ME)
    assert out["status"] == "nothing_to_check" and out["uncompiled_rules"] == 2
    assert "还没有编译结果(2 条, 后台在编)" in out["next_step"] and wired.calls == [] and kicks == [(PROJ, "check_rules")]


def test_only_not_checkable_rules(wired, kicks):
    rule = R_NOT_CHECKABLE
    res = {"id": rule["id"], "sha": jev_sha(rule["content"]), "verdict": "not_checkable", "code": None,
           "questions": [], "coverage": None, "uncovered": "", "why": "交付"}
    out = core.check_rules(full_client(memories=[rule], compiles=[stored(rule, res)]), PROJ, [V1], user_id=ME)
    assert out["status"] == "nothing_to_check" and "查不了" in out["next_step"] and wired.calls == []


# ══════════════════════════════════════════════════════════════════════
# 7 · 复审(2026-10-20): 没判出来 / 查不了 / 没编译结果的, 都不许说成守住了
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("reply, note", [
    (lambda q: {"desk_rules": {q: {"answer": None, "p": None, "ambiguous": False, "invalid_reason": "out_of_vocab"}}},
     "out_of_vocab"),
    (lambda q: {}, "回包里没有规则题的答案"),                      # detail 里没有 desk_rules 这个键
    (lambda q: None, "回包里没有规则题的答案"),                    # 连 detail 都没有
])
def test_a_version_whose_rule_questions_all_came_back_unjudged_is_not_kept(wired, reply, note):
    """Jev 回了 200, 但唯一那道硬规则题没判出来: 原来只记个 row["unjudged"], summary 不看它, next_step 照说
    「这次查的规则都守住了」。"""
    qid = qid_of(q_hard())
    wired.judge = lambda payload: {"subject_id": payload["subject_id"], "passed": True, "plan": [],
                                   **({"detail": reply(qid)} if reply(qid) is not None else {})}
    out = core.check_rules(full_client(memories=[R_HARD], compiles=[stored(R_HARD, q_hard())]), PROJ, [V1], user_id=ME)
    (v1,) = out["versions"]
    assert v1["judge_status"] == "not_judged" and "一道都没判出来" in v1["judge_note"] and note in v1["judge_note"]
    assert v1["violations"] == [] and v1["counts"]["unjudged"] == 1
    assert out["summary"]["not_finished"] == 1
    assert out["summary"]["unjudged"] == v1["counts"]["unjudged"]
    assert "都守住了" not in out["next_step"] and "没判完" in out["next_step"]


@pytest.mark.parametrize("compiled", ["not_checkable", "questions", "missing"])
def test_hand_set_checks_are_counted_without_compiled_code(wired, compiled):
    result = q_soft(R_HANDSET)
    if compiled == "not_checkable":
        result = {**result, "verdict": "not_checkable", "questions": []}
    compiles = [] if compiled == "missing" else [stored(R_HANDSET, result)]
    out = core.check_rules(full_client(memories=[R_HANDSET], compiles=compiles, versions=[version(V1)]),
                           PROJ, [V1], user_id=ME)
    assert out["rules"]["by_score_drafts"] == 1
    assert "流程、交付、写作方向" not in out["next_step"]


@pytest.mark.parametrize("bad_question", [{"ask": "坏的题"}, "bad", None, "duplicate"])
def test_skipped_cached_questions_never_report_the_whole_rule_kept(wired, bad_question):
    result = q_soft()
    first = result["questions"][0]
    result["questions"].append(dict(first) if bad_question == "duplicate" else bad_question)
    wired.judge = answers(**{first["qid"]: {"answer": "是", "p": 0.95, "ambiguous": False}})
    out = core.check_rules(full_client(memories=[R_SOFT], compiles=[stored(R_SOFT, result)], versions=[version(V1)]),
                           PROJ, [V1], user_id=ME)
    assert out["versions"][0]["counts"]["must_fix"] == 0
    assert out["rules"]["questions_unchecked"] == 1
    assert out["rules"]["questions_unchecked_rules"] == [R_SOFT["id"]]
    assert "都守住了" not in out["next_step"] and "没查" in out["next_step"]


def test_queued_judge_calls_use_the_remaining_shared_budget(wired, monkeypatch):
    monkeypatch.setattr(core, "CHECK_RULES_WORKERS", 1)
    ticks = [1000.0]             # 固定起点: 用真 monotonic 起点时 (s+10)-s 不一定正好是 10, CI 上出过 8.000000000000028
    clock = type("Clock", (), {"monotonic": staticmethod(lambda: ticks[0])})()
    monkeypatch.setattr(core, "time", clock)
    calls = []

    def judge(payload, *, timeout):
        calls.append(timeout)
        ticks[0] += 10
        return {"judge_status": core.judge_client.JUDGE_OK, "response": {"detail": {}}}

    monkeypatch.setattr(core.judge_client, "judge_draft_full", judge)
    ids = [str(uuid.UUID(int=i + 1)) for i in range(3)]
    core.check_rules(full_client(memories=[R_SOFT], compiles=[stored(R_SOFT, q_soft())],
                                 versions=[version(vid) for vid in ids]), PROJ, ids, user_id=ME)
    assert calls == pytest.approx([18.0, 8.0]), "第三个任务起跑时预算已用完, 不许再发请求"


@pytest.mark.parametrize("questions", [[], 123, {"qid": "bad"}, [None]])
def test_a_wholly_unusable_question_cache_is_reported_without_crashing(wired, questions):
    result = {**q_soft(), "questions": questions}
    out = core.check_rules(full_client(memories=[R_SOFT], compiles=[stored(R_SOFT, result)], versions=[version(V1)]),
                           PROJ, [V1], user_id=ME)
    assert out["status"] == "nothing_to_check" and out["rules"]["questions_unchecked"] == 1
    assert "没查" in out["next_step"] and "都守住了" not in out["next_step"]
    assert wired.calls == []


def test_partly_unjudged_versions_say_how_many_questions_were_not_judged(wired):
    wired.judge = answers(**{qid_of(q_hard()): {"answer": "否", "p": 0.93, "ambiguous": False}})   # 软规则那题缺了
    out = core.check_rules(full_client(memories=[R_HARD, R_SOFT],
                                       compiles=[stored(R_HARD, q_hard()), stored(R_SOFT, q_soft())]),
                           PROJ, [V1], user_id=ME)
    (v1,) = out["versions"]
    assert v1["judge_status"] == "ok" and v1["violations"] == [] and v1["unsure"] == []
    assert v1["counts"]["unjudged"] == 1 and v1["unjudged"] == [{"rule_id": R_SOFT["id"], "reason": "missing"}]
    assert out["summary"]["unjudged"] == 1 and out["summary"]["not_finished"] == 0
    assert "另有 1 道规则题这次没判出来" in out["next_step"] and "都守住了" not in out["next_step"]


def test_all_kept_is_said_only_when_everything_was_actually_judged(wired):
    wired.judge = answers(**{qid_of(q_hard()): {"answer": "否", "p": 0.93, "ambiguous": False},
                             qid_of(q_soft()): {"answer": "是", "p": 0.9, "ambiguous": False}})
    out = core.check_rules(full_client(memories=[R_HARD, R_SOFT],
                                       compiles=[stored(R_HARD, q_hard()), stored(R_SOFT, q_soft())]),
                           PROJ, [V1], user_id=ME)
    assert out["summary"]["unjudged"] == 0 and "这次查的规则都守住了" in out["next_step"]


BODY_WITH_TAGS = BODY + "\n#其他话题 #另一个话题"


def _tag_rules():
    r_req = mem(21, "示例硬规则: 每篇都要带示例话题", severity="hard")
    r_cnt = mem(22, "示例软规则: 话题最多一个")
    r_cmt = mem(23, "示例软规则: 评论里不提示例词")
    r_mix = mem(24, "示例软规则: 正文和话题都不用「不存在的示例词」")
    codes = {r_req["id"]: {"kind": "required_phrase", "any": ["示例话题"], "where": ["tags"]},
             r_cnt["id"]: {"kind": "forbidden_regex", "pattern": r"^\S+\s+\S", "where": ["tags"]},
             r_cmt["id"]: {"kind": "forbidden_word", "words": ["示例词"], "where": "comments"},
             r_mix["id"]: {"kind": "forbidden_word", "words": ["不存在的示例词"], "where": ["body", "tags"]}}
    rules = [r_req, r_cnt, r_cmt, r_mix]
    return rules, [stored(r, code_result(r["id"], jev_sha(r["content"]), codes[r["id"]])) for r in rules]


def test_compiled_code_on_tags_or_comments_is_not_run_and_not_counted_as_checked(wired):
    """版本行只存标题和正文: 写手传的 tags / comments、交付模板的固定话题都不在。原来话题只从正文的 #话题 里拼, 「每篇
    要带某个话题」对每篇误报必须改, 「话题最多一个」照正文末尾那行报; 评论永远是空的, 查了等于没查却算进 by_code。"""
    rules, comps = _tag_rules()
    out = core.check_rules(full_client(memories=rules, compiles=comps, versions=[version(V1, body=BODY_WITH_TAGS)]),
                           PROJ, [V1], user_id=ME)
    (v1,) = out["versions"]
    assert v1["violations"] == [], "话题 / 评论上的检查不跑(score_drafts 手里有写手的 tags 才查得对)"
    assert out["rules"]["by_code"] == 1 and out["rules"]["code_unavailable"] == 3, "正文 + 话题那条只跑正文那一半"
    assert "话题 / 评论" in out["next_step"] and "都守住了" not in out["next_step"]


def test_only_tag_rules_is_nothing_to_check_and_says_why(wired):
    rules, comps = _tag_rules()
    out = core.check_rules(full_client(memories=rules[:3], compiles=comps[:3], versions=[version(V1)]),
                           PROJ, [V1], user_id=ME)
    assert out["status"] == "nothing_to_check" and out["rules"]["code_unavailable"] == 3
    assert "话题 / 评论" in out["next_step"] and "流程、交付、写作方向" not in out["next_step"]
    assert wired.calls == []


def test_code_rules_strips_unstored_parts():
    rule = mem(1)
    res = lambda code: code_result(rule["id"], "0" * 12, code)                         # noqa: E731
    assert rulecompile.code_rules([(rule, res({"kind": "required_phrase", "any": ["x"], "where": ["tags"]}))]) \
        == ([], [rule["id"]])
    assert rulecompile.code_rules([(rule, res({"kind": "forbidden_word", "words": ["x"], "where": "comments"}))]) \
        == ([], [rule["id"]])
    (got,), none = rulecompile.code_rules([(rule, res({"kind": "forbidden_word", "words": ["x"],
                                                       "where": ["title", "tags", "comments"]}))])
    assert none == [] and got["rule_payload"]["where"] == ["title"]
    (got,), _ = rulecompile.code_rules([(rule, res({"kind": "forbidden_word", "words": ["x"]}))])
    assert "where" not in got["rule_payload"], "没给 where 的不动(默认标题 + 正文)"
    (got,), _ = rulecompile.code_rules([(rule, res({"kind": "max_len", "max": 20, "where": "title"}))])
    assert got["rule_payload"] == {"max": 20, "where": "title"}
    assert rulecompile.code_rules([(rule, res({"kind": "required_phrase", "any": ["x"], "where": ["body", "tags"]}))]) \
        == ([], [rule["id"]]), "必须带的话术: 看不见的那一处(话题)可能就有, 只查正文会误报"
    (got,), _ = rulecompile.code_rules([(rule, res({"kind": "forbidden_word", "words": ["x"],
                                                    "where": ["body", "tags"]}))])
    assert got["rule_payload"]["where"] == ["body"], "禁用词只查看得见的那一半: 查到的照样是真的"


def test_a_project_without_tv_map_is_not_said_to_be_compiling(jev, kicks):     # noqa: F811
    """没接 tv-map 的项目永远编不出来, 原来每批都说「还在后台编译」。"""
    c = make_client([R_HARD], [], maps=False, versions=[version(V1)])
    assert core.compile_rules(c, PROJ)["status"] == "not_configured"
    out = core.check_rules(c, PROJ, [V1], user_id=ME)
    assert out["status"] == "nothing_to_check" and out["rules"]["uncompiled_reason"] == "no_tv_map"
    assert "在后台编译" not in out["next_step"] and "后台在编" not in out["next_step"]
    assert "还没有编译结果" in out["next_step"] and "tv-map" in out["next_step"]


_REAL_KICK = core._kick_rule_compile


def test_uncompiled_reasons(jev, monkeypatch):                                    # noqa: F811
    c = full_client(memories=[R_HARD, R_SOFT], compiles=[stored(R_HARD, q_hard())])
    jev.judge = answers(**{qid_of(q_hard()): {"answer": "否", "p": 0.93, "ambiguous": False}})
    # 后台补编关着(RULE_COMPILE_AUTO=0): 不说在编
    out = core.check_rules(c, PROJ, [V1], user_id=ME)
    assert out["rules"]["uncompiled_reason"] == "auto_off" and "后台补编关着" in out["next_step"]
    assert "后台在编" not in out["next_step"]
    # 上次编失败了(同一版本 6 小时内后台不再编)
    core._RULE_COMPILE_FAILED[(R_SOFT["id"], jev_sha(R_SOFT["content"]))] = time.monotonic()
    monkeypatch.setattr(config, "RULE_COMPILE_AUTO", True)
    monkeypatch.setattr(core, "_kick_rule_compile", lambda *a, **k: "cooldown")
    out = core.check_rules(c, PROJ, [V1], user_id=ME)
    assert out["rules"]["uncompiled_reason"] == "compile_failed" and out["rules"]["compile_failed"] == 1
    assert "上次编失败了" in out["next_step"] and "后台在编" not in out["next_step"]
    # 整批被出境口径挡下, 项目在 hold 里
    core._RULE_COMPILE_FAILED.clear()
    core._RULE_COMPILE_STATE[PROJ] = {"running": False, "again": False, "last": None,
                                      "hold_until": time.monotonic() + 3600, "last_status": "policy_blocked"}
    monkeypatch.setattr(core, "_kick_rule_compile", _REAL_KICK)          # 真的触发器: 项目在 hold 里 → held
    out = core.check_rules(c, PROJ, [V1], user_id=ME)
    assert out["rules"]["uncompiled_reason"] == "policy_blocked" and "出境口径" in out["next_step"]
    # Jev 没配
    monkeypatch.setattr(config, "JUDGE_URL", "")
    out = core.check_rules(c, PROJ, [V1], user_id=ME)
    assert out["rules"]["uncompiled_reason"] == "judge_not_configured" and "Jev 没配置" in out["next_step"]



def test_missing_rule_compiles_table_is_an_error_not_a_quiet_no_op(wired, kicks, caplog):
    """migrations/015 没跑: check_rules 回 status=error(写手那边是这次规则没查成、不能说守住了), 不是「规则还没编好」。
    第三轮复审: 原来直接抛 —— REST 500(模型读成「待会儿重试」)、MCP 工具错误, deskcore 一行日志都没有。"""
    c = full_client()
    c.missing_columns = {"rule_compiles": {"memory_id"}}
    with caplog.at_level("ERROR", logger="deskcore"):
        out = core.check_rules(c, PROJ, [V1], user_id=ME)
    assert out["status"] == "error" and out["versions"] == [] and out["reason"] == "rule_compiles_unreadable"
    assert "没查成" in out["next_step"] and "不能说规则守住了" in out["next_step"] and "不用重试" in out["next_step"]
    assert "守住了。" not in out["next_step"] and "还没有编译结果" not in out["next_step"]
    assert wired.calls == [] and kicks == [], "读不了缓存: 不发 Jev, 也不触发补编(一样读不了)"
    assert any(r.levelname == "ERROR" and PROJ in r.getMessage() and "rule_compiles" in r.getMessage()
               for r in caplog.records), "服务端要有一行带项目号的日志"


# ══════════════════════════════════════════════════════════════════════
# 8 · 每篇列多少 / 整篇判不了 / 空正文
# ══════════════════════════════════════════════════════════════════════

def test_long_lists_are_capped_but_counts_stay_whole(wired):
    hard = [mem(40 + i, f"示例硬规则第 {i} 条", severity="hard") for i in range(10)]
    soft = [mem(60 + i, f"示例软规则第 {i} 条") for i in range(10)]
    shaky = [mem(80 + i, f"示例软规则(拿不准)第 {i} 条") for i in range(7)]
    rules = hard + soft + shaky

    def q(r):
        return question_result(r["id"], jev_sha(r["content"]), ask=f"正文有没有违反{r['content']}？", pass_if="否")
    comps = [stored(r, q(r)) for r in rules]
    by_qid = {}
    for r in hard + soft:
        by_qid[q(r)["questions"][0]["qid"]] = {"answer": "是", "p": 0.9, "ambiguous": False, "evidence": "第一句讲一件小事。"}
    for r in shaky:
        by_qid[q(r)["questions"][0]["qid"]] = {"answer": "是", "p": 0.55, "ambiguous": True}
    wired.judge = answers(**by_qid)
    out = core.check_rules(full_client(memories=rules, compiles=comps, versions=[version(V1)]), PROJ, [V1], user_id=ME)
    (v1,) = out["versions"]
    assert len(v1["violations"]) == core.CHECK_RULES_MAX_LISTED
    assert [e["severity"] for e in v1["violations"]] == ["hard"] * 10 + ["soft"] * 2, "必须改的先保"
    assert len(v1["unsure"]) == core.CHECK_RULES_MAX_UNSURE
    assert v1["more"] == (20 - core.CHECK_RULES_MAX_LISTED) + (7 - core.CHECK_RULES_MAX_UNSURE)
    assert v1["counts"] == {"must_fix": 10, "suggest": 10, "unsure": 7, "unjudged": 0}
    assert {k: out["summary"][k] for k in ("must_fix", "suggest", "unsure")} == {"must_fix": 10, "suggest": 10, "unsure": 7}


def test_must_fix_is_never_cut_even_past_the_cap(wired):
    """必须改全列(同 score_drafts 的 rule_checks / rulecheck.top_hits): 截掉的只剩个数, 写手不知道是哪条, 会当没问题交出去。
    建议改补到上限为止, 必须改已经超过上限时一条都不列, 只报条数。"""
    hard = [mem(100 + i, f"示例硬规则第 {i} 条", severity="hard") for i in range(core.CHECK_RULES_MAX_LISTED + 3)]
    soft = [mem(130 + i, f"示例软规则第 {i} 条") for i in range(4)]

    def q(r):
        return question_result(r["id"], jev_sha(r["content"]), ask=f"正文有没有违反{r['content']}？", pass_if="否")
    wired.judge = answers(**{q(r)["questions"][0]["qid"]: {"answer": "是", "p": 0.9, "ambiguous": False}
                             for r in hard + soft})
    out = core.check_rules(full_client(memories=hard + soft, compiles=[stored(r, q(r)) for r in hard + soft],
                                       versions=[version(V1)]), PROJ, [V1], user_id=ME)
    (v1,) = out["versions"]
    assert [e["severity"] for e in v1["violations"]] == ["hard"] * len(hard)
    assert v1["more"] == len(soft) and v1["counts"]["must_fix"] == len(hard)


def test_a_reply_with_a_top_level_invalid_reason_is_not_judged(wired):
    wired.judge = lambda payload: {"subject_id": payload["subject_id"], "passed": True, "plan": [],
                                   "invalid_reason": "too_short"}
    out = core.check_rules(full_client(memories=[R_HARD], compiles=[stored(R_HARD, q_hard())]), PROJ, [V1], user_id=ME)
    (v1,) = out["versions"]
    assert v1["judge_status"] == "not_judged" and "规则题没判" in v1["judge_note"] and "too_short" in v1["judge_note"]
    assert not any(e["source"] == "jev" for e in v1["violations"])
    assert out["summary"]["not_finished"] == 1 and "都守住了" not in out["next_step"]


def test_an_empty_body_is_not_sent_but_code_checks_still_count(wired):
    out = core.check_rules(full_client(versions=[version(V1), version(V2, title="示例禁词也在这个标题里", body="  \n ")]),
                           PROJ, [V1, V2], user_id=ME)
    v1, v2 = out["versions"]
    assert v2["judge_status"] == "empty_body" and "正文是空的" in v2["judge_note"]
    sent = [x["json"]["subject_id"] for x in wired.calls if x["url"].endswith("/judge_draft")]
    assert sent == [V1], "空正文那篇不发"
    assert out["summary"]["not_finished"] == 0, "空正文不算没判完(本来就没东西可判)"
    assert [e["rule_id"] for e in v2["violations"] if e["source"] == "code"] == [R_CODE["id"]]


# ══════════════════════════════════════════════════════════════════════
# 7 · 查的是交付出去的那一份
# ══════════════════════════════════════════════════════════════════════

TPL_TAGS = {"layout": "blocks", "tags": {"content": 2}}
TAIL = "\n#示例话题甲 #示例话题乙"


def _project(name: str = "示例品牌-产品向", default_params=None) -> list[dict]:
    row = {"id": PROJ, "name": name, "brand": "示例品牌", "owner_id": ME, "calibration_notes": "",
           "tactics": "[]", "custom_roles": []}
    if default_params is not None:
        row["default_params"] = default_params
    return [row]


def _body_rules():
    r_forbid = mem(31, "示例软规则: 正文不用「示例话题甲」")
    r_need = mem(32, "示例硬规则: 正文要提到「示例话题乙」", severity="hard")
    comps = [stored(r_forbid, code_result(r_forbid["id"], jev_sha(r_forbid["content"]),
                                          {"kind": "forbidden_word", "words": ["示例话题甲"], "where": ["body"]})),
             stored(r_need, code_result(r_need["id"], jev_sha(r_need["content"]),
                                        {"kind": "required_phrase", "any": ["示例话题乙"], "where": ["body"]})),
             stored(R_HARD, q_hard())]
    return [R_HARD, r_forbid, r_need], comps


def _judged(calls) -> dict:
    (call,) = [x for x in calls if x["url"].endswith("/judge_draft")]
    return call["json"]


def test_jev_and_code_see_the_delivered_body_when_the_template_lifts_trailing_tags(wired):
    """版本行存的是写手传的原稿(末尾带一行 #话题); 模板管话题时交付稿把那一行摘进话题行。Jev 判的、代码查的都要是
    摘掉之后的正文, 和 delivery.text 里【正文】后面那一段一样 —— 拿原稿查的话, 话题行里的词被当成「正文里有」,
    正文必须带的话术也能靠被摘走的那行混过去。"""
    rules, comps = _body_rules()
    c = make_client(rules, comps, projects=_project(default_params=json.dumps({"delivery": TPL_TAGS})),
                    versions=[version(V1, title=f"  {TITLE} ", body=BODY + TAIL)])
    out = core.check_rules(c, PROJ, [V1], user_id=ME)
    sent = _judged(wired.calls)
    assert sent["title"] == TITLE and sent["body"] == BODY, "Jev 判的是交付出去的正文, 末尾那行话题不在里面"
    text = delivery.assemble(TPL_TAGS, [{"title": f"  {TITLE} ", "body": BODY + TAIL}])["text"]
    assert text.startswith(f"【标题】{sent['title']}\n【正文】{sent['body']}\n#"), "和交付稿逐字对得上"
    (v1,) = out["versions"]
    code = [(e["rule_id"], e["severity"]) for e in v1["violations"] if e["source"] == "code"]
    assert code == [(rules[2]["id"], "hard")], "只在话题行里的词不算正文里有; 只在话题行里的话术也不算正文带了"


def test_without_a_tag_template_the_body_is_judged_as_written(wired):
    """模板不管话题(或没配模板)时交付稿的正文就是原稿: 末尾那行 #话题 留在正文里, 照原样判。"""
    rules, comps = _body_rules()
    for params in (None, json.dumps({"delivery": {"layout": "blocks"}})):
        wired.calls.clear()
        c = make_client(rules, comps, projects=_project(default_params=params),
                        versions=[version(V1, body=BODY + TAIL)])
        out = core.check_rules(c, PROJ, [V1], user_id=ME)
        assert _judged(wired.calls)["body"] == BODY + TAIL
        (v1,) = out["versions"]
        assert [e["rule_id"] for e in v1["violations"] if e["source"] == "code"] == [rules[1]["id"]]


def test_a_broken_template_falls_back_to_the_stored_text(wired):
    """模板坏了(有人手改了库): 同 score_drafts, 不拼交付稿、照原稿查, 不报错。"""
    rules, comps = _body_rules()
    c = make_client(rules, comps, projects=_project(default_params="{不是 JSON"),
                    versions=[version(V1, body=BODY + TAIL)])
    out = core.check_rules(c, PROJ, [V1], user_id=ME)
    assert out["status"] == "checked" and _judged(wired.calls)["body"] == BODY + TAIL


@pytest.mark.parametrize("tpl", [TPL_TAGS, {"layout": "blocks"}, None], ids=["tags", "no-tags", "no-template"])
@pytest.mark.parametrize("tail", [
    TAIL,                                   # 末尾一整行话题
    "示例收尾#示例话题甲 #示例话题乙",       # 最后一句后面直接跟着一串
    "最后祝大家#示例话题[话题]#顺利收尾",    # 话题后面还接着正文: 整句不摘
    "学了示例C#入门 #示例话题甲",            # C#入门 不是话题, 后面那个照摘
    "详情见 https://example.com/#faq",       # 网址里的 #片段 不是话题
], ids=["tag-line", "inline-run", "prose-after-tag", "c-sharp", "url-fragment"])
def test_check_rules_sees_exactly_what_score_drafts_rule_checks_sees(wired, monkeypatch, tpl, tail):
    """check_rules 和 score_drafts 的 rule_checks 查的是同一份交付稿(core._as_delivered; aw#115 第三轮之后哪个 # 算话题
    只认 validator.topic_tag_matches): 代码查看见的标题、正文、话题、评论逐字一样, 发给 Jev 的正文也是这一份。版本行里
    没有写手单独传的 tags / comments, 所以存档那一篇也不带。"""
    seen: list[list[dict]] = []
    real = rulecheck.check

    def spy(rules, drafts, *, indices=None, tags=None, expects=()):
        seen.append([rulecheck._parts(d, None if tags is None else tags[k]) for k, d in enumerate(drafts)])
        return real(rules, drafts, indices=indices, tags=tags, expects=expects)
    monkeypatch.setattr(rulecheck, "check", spy)

    rules, comps = _body_rules()
    draft = {"title": TITLE, "body": BODY + tail}
    projects = _project(default_params=None if tpl is None else json.dumps({"delivery": tpl}))
    c = make_client(rules, comps, projects=projects, versions=[version(V1, body=draft["body"])])
    core._delivery_and_rule_checks(c, projects[0], PROJ, ME, [draft], [0], {})
    out = core.check_rules(c, PROJ, [V1], user_id=ME)
    assert out["status"] == "checked"
    by_score, by_check = seen
    assert by_check == by_score, "两条路看见的交付稿不一样"
    post = delivery.delivered_post(tpl, draft) if tpl is not None else None
    want = post[0] if post is not None else draft["body"]
    assert by_check[0]["body"] == [want] and _judged(wired.calls)["body"] == want


def test_tactic_feeds_the_direction_gate_like_the_brief(wired):
    """项目名不带方向、靠 open_project 的 tactic 判出方向的那场: 简报挡掉的「另一个方向」的规则这里也不发给 Jev。
    不传 tactic 判不出方向, 全部放行(同简报的保守方向)。"""
    comps = [stored(R_HARD, q_hard()), stored(R_OTHER_DIR, q_other_dir())]

    def bank(**kw) -> list[str]:
        wired.calls.clear()
        c = make_client([R_HARD, R_OTHER_DIR], comps, projects=_project(name="示例品牌"), versions=[version(V1)])
        core.check_rules(c, PROJ, [V1], user_id=ME, **kw)
        return [q["id"] for q in _judged(wired.calls)["project_bank"]["questions"]]

    assert bank() == [qid_of(q_hard()), qid_of(q_other_dir())]
    assert bank(tactic="产品向") == [qid_of(q_hard())]


def test_the_tool_passes_tactic_through(monkeypatch):
    got: dict = {}
    monkeypatch.setattr(core, "sb", lambda: "client")
    monkeypatch.setattr(core, "check_rules", lambda client, pid, vids, **kw: got.update(kw) or {"status": "checked"})
    tools.check_rules(PROJ, [V1], tactic="产品向", _user_id=ME)
    assert got == {"user_id": ME, "tactic": "产品向"}
    tools.check_rules(PROJ, [V1], _user_id=ME)
    assert got["tactic"] == ""


# ══════════════════════════════════════════════════════════════════════
# 9 · 复审(2026-10-20 第二轮)
# ══════════════════════════════════════════════════════════════════════

_SYNTH_CHARS = "的一是在不了有和人这中大为上个我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种面而方后多定"


def _synthetic_drafts(n: int) -> list[dict]:
    """n 篇互不撞车的合成稿(每篇各用一个种子随机取字)。"""
    import random
    out = []
    for i in range(n):
        rng = random.Random(1000 * i + 7)
        out.append({"title": f"合成标题第{i:02d}篇", "body": "".join(rng.choice(_SYNTH_CHARS) for _ in range(260))})
    return out


def _embed_versions(c) -> None:
    """假库没有真 join: versions_in_project 走 ``items!inner → batches!inner``, 行里手动嵌上(同 draft_identity 的 _embed)。"""
    items = {i["id"]: i for i in c.rows.get("items", [])}
    batches = {b["id"]: b for b in c.rows.get("batches", [])}
    for v in c.rows.get("versions", []):
        it = items.get(v.get("item_id"))
        if it:
            v["items"] = {"id": it["id"], "batches": {"project_id": (batches.get(it.get("batch_id")) or {}).get("project_id")}}


def test_a_batch_over_the_per_call_cap_goes_from_score_drafts_through_check_rules(wired):
    """score_drafts 一批不限篇数(22 篇的批次出现过), check_rules 一次只查 CHECK_RULES_MAX_VERSIONS 篇。原来协议 5a 说把
    check_rules_version_ids 原样传、一批只调一次, 超过 20 篇时 check_rules 直接报错, 写手照协议说「这次规则没查成」——
    整批都没查。现在原样传也行: 查前 20 篇, 剩下的放进 remaining_version_ids, next_step 让再调一次。"""
    n = core.CHECK_RULES_MAX_VERSIONS + 2
    c = full_client(memories=[R_HARD], compiles=[stored(R_HARD, q_hard())], versions=[])
    c.rpc_impl["deskcore_commit_fingerprints"] = lambda args: [
        {"idx": i, "status": core.COMMIT_STATUS_INSERTED, "collided_with": None, "detail": None}
        for i in range(len(args.get("_rows") or []))]
    saved = core.commit_drafts(c, PROJ, _synthetic_drafts(n), user_id=ME)
    ids = saved["check_rules_version_ids"]
    assert saved["written"] == n and len(ids) == n
    assert "remaining_version_ids" in saved["next_step"], "超过一次能查的篇数时, 存档的 next_step 先说清要分几次"
    _embed_versions(c)
    wired.calls.clear()

    first = core.check_rules(c, PROJ, ids, user_id=ME)                 # 协议 5a: 原样传
    assert first["status"] == "checked"
    assert [v["version_id"] for v in first["versions"]] == ids[:core.CHECK_RULES_MAX_VERSIONS]
    assert first["remaining_version_ids"] == ids[core.CHECK_RULES_MAX_VERSIONS:]
    assert "remaining_version_ids" in first["next_step"] and "再调一次 check_rules" in first["next_step"]

    second = core.check_rules(c, PROJ, first["remaining_version_ids"], user_id=ME)
    assert [v["version_id"] for v in second["versions"]] == ids[core.CHECK_RULES_MAX_VERSIONS:]
    assert "remaining_version_ids" not in second and "remaining_version_ids" not in second["next_step"]
    sent = [x["json"]["subject_id"] for x in wired.calls if x["url"].endswith("/judge_draft")]
    assert sorted(sent) == sorted(ids), "每篇新稿都查了, 各查一次"


def test_the_tool_takes_a_long_list_and_hands_back_the_rest(wired, monkeypatch):
    ids = [f"{i:08x}-0000-4000-8000-000000000000" for i in range(core.CHECK_RULES_MAX_VERSIONS + 1)]
    c = full_client(versions=[version(v) for v in ids])
    monkeypatch.setattr(core, "sb", lambda: c)
    out = tools.check_rules(PROJ, ids, _user_id=ME)
    assert len(out["versions"]) == core.CHECK_RULES_MAX_VERSIONS and out["remaining_version_ids"] == ids[-1:]
    exact = core.check_rules(c, PROJ, ids[:core.CHECK_RULES_MAX_VERSIONS], user_id=ME)
    assert len(exact["versions"]) == core.CHECK_RULES_MAX_VERSIONS and "remaining_version_ids" not in exact


def test_no_versions_in_the_first_part_still_hands_back_the_rest(wired):
    """前 20 个里一个本项目的都没有: 这次什么都不查, 剩下的照样还回去, 不能让写手以为整批都不是这个项目的。"""
    ids = [f"{i:08x}-0000-4000-8000-0000000000ff" for i in range(core.CHECK_RULES_MAX_VERSIONS)] + [V1]
    out = core.check_rules(full_client(), PROJ, ids, user_id=ME)
    assert out["status"] == "no_versions" and out["remaining_version_ids"] == [V1]
    assert "remaining_version_ids" in out["next_step"]


def _never_checkable():
    too_long = mem(90, "示例硬规则正文。" * 600, severity="hard")                     # 超过 Jev 的字段上限: 编不了
    unsafe = mem(91, "示例硬规则: 不要连着写感叹", severity="hard")
    unsafe_res = code_result(unsafe["id"], jev_sha(unsafe["content"]),
                             {"kind": "forbidden_regex", "pattern": "(哈|哈哈)+$", "where": ["body"]})
    return too_long, unsafe, unsafe_res


def test_rules_that_can_never_be_checked_are_not_said_to_hold(wired):
    """一条软规则题判了守住; 一条硬规则原文太长编不了(永远不发给 Jev), 一条硬规则编出来的正则不安全(永远不跑)。原来
    next_step 只看没编译结果 / 话题评论两种, 照说「这次查的规则都守住了」, 两条硬规则从来没查过。"""
    too_long, unsafe, unsafe_res = _never_checkable()
    wired.judge = answers(**{qid_of(q_soft()): {"answer": "是", "p": 0.9, "ambiguous": False}})
    out = core.check_rules(full_client(memories=[R_SOFT, too_long, unsafe],
                                       compiles=[stored(R_SOFT, q_soft()), stored(unsafe, unsafe_res)],
                                       versions=[version(V1)]), PROJ, [V1], user_id=ME)
    assert out["status"] == "checked" and out["summary"]["must_fix"] == 0
    assert out["rules"]["not_compilable"] == 1 and out["rules"]["code_refused"] == 1
    assert out["rules"]["by_code"] == 0, "不安全没跑的那条不算「代码查了」"
    assert "都守住了" not in out["next_step"]
    assert "2 条规则这次查不了" in out["next_step"] and "不能说它们守住了" in out["next_step"]


def test_only_a_rule_too_long_to_compile_is_not_called_a_process_rule(wired):
    too_long, _unsafe, _res = _never_checkable()
    out = core.check_rules(full_client(memories=[too_long], compiles=[], versions=[version(V1)]), PROJ, [V1], user_id=ME)
    assert out["status"] == "nothing_to_check" and out["rules"]["not_compilable"] == 1
    assert "流程、交付、写作方向" not in out["next_step"]
    assert "1 条规则这次查不了" in out["next_step"] and "不能说它们守住了" in out["next_step"] and wired.calls == []


# ══════════════════════════════════════════════════════════════════════
# 10 · 复审(2026-10-20 第三轮)
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("kind, payload", UNRUNNABLE_HAND_SET)
def test_an_unrunnable_hand_set_check_does_not_hide_the_compiled_code(wired, kind, payload):
    """AW 页存进去的 check rulecheck 跑不了: score_drafts 只记一笔 skipped, 写手什么都看不见。原来这里也让编出来的 code
    让路 —— 这条硬规则两边都没查; 池子里只有它时说「都是查不了的那种(流程、交付、写作方向)」, 混着别的规则时说「都守住了」。"""
    rule = mem(12, "示例硬规则: 正文不要出现「示例禁词」", severity="hard", rule_kind=kind, rule_payload=payload)
    res = code_result(rule["id"], jev_sha(rule["content"]),
                      {"kind": "forbidden_word", "words": ["示例禁词"], "where": ["body"]})
    body = BODY + "这里有示例禁词。"
    hits, stats = rulecheck.check([rule], [{"title": TITLE, "body": body}])
    assert hits == [] and stats == {"rules": 0, "skipped": 1, "skipped_ids": [rule["id"]]}, \
        "score_drafts 那边: 人设的跑不了, 只记 skipped(带上是哪一条, aw#115 起报进 rule_checks_unchecked)"
    wired.judge = answers(**{qid_of(q_soft()): {"answer": "是", "p": 0.9, "ambiguous": False}})
    for memories, compiles in (([rule], [stored(rule, res)]),
                               ([rule, R_SOFT], [stored(rule, res), stored(R_SOFT, q_soft())])):
        out = core.check_rules(full_client(memories=memories, compiles=compiles, versions=[version(V1, body=body)]),
                               PROJ, [V1], user_id=ME)
        assert out["status"] == "checked" and out["rules"]["by_code"] == 1
        (v1,) = out["versions"]
        assert [(e["rule_id"], e["source"], e["severity"]) for e in v1["violations"]] == [(rule["id"], "code", "hard")]
        assert "示例禁词" in v1["violations"][0]["what"] and out["summary"]["must_fix"] == 1
        assert "都守住了" not in out["next_step"] and "流程、交付、写作方向" not in out["next_step"]


def test_a_rule_compiled_only_in_part_is_not_said_to_hold(wired):
    """Jev 编译时标了 coverage=partial(一条规则的要求没全装进题 / 代码, 没装进去的写在 uncovered)。原来这边一处都不读:
    「标题不超过 20 字, 正文要带某句话术」只编出了标题那一半, 标题短就报「这次查的规则都守住了」。"""
    rule = mem(13, "示例硬规则: 标题不超过 20 字, 正文要带示例话术", severity="hard")
    res = {**code_result(rule["id"], jev_sha(rule["content"]), {"kind": "max_len", "max": 20, "where": "title"}),
           "coverage": "partial", "uncovered": "正文要带示例话术没查"}
    out = core.check_rules(full_client(memories=[rule], compiles=[stored(rule, res)],
                                       versions=[version(V1, title="短标题")]), PROJ, [V1], user_id=ME)
    assert out["status"] == "checked" and out["rules"]["by_code"] == 1 and out["versions"][0]["violations"] == []
    assert out["rules"]["partial"] == 1
    assert out["rules"]["partial_rules"] == [{"rule_id": rule["id"], "uncovered": "正文要带示例话术没查"}]
    assert "都守住了" not in out["next_step"] and "有 1 条规则只查了一部分" in out["next_step"]


def test_partial_counts_only_rules_that_were_actually_checked(wired):
    """partial 只数这次真查了的: 题进了题库的数; 编出来的正则不安全、根本没跑的那条是「查不了」(code_refused), 不重复算。"""
    soft = mem(14, "示例软规则: 正文写出持续了多久, 语气也别像广告")
    q = {**q_soft(soft), "coverage": "partial", "uncovered": "语气那一半没编进去"}
    unsafe = mem(15, "示例硬规则: 不要连着笑, 也不要堆感叹号", severity="hard")
    unsafe_res = {**code_result(unsafe["id"], jev_sha(unsafe["content"]),
                                {"kind": "forbidden_regex", "pattern": "(哈|哈哈)+$", "where": ["body"]}),
                  "coverage": "partial", "uncovered": "感叹号那一半没编进去"}
    wired.judge = answers(**{qid_of(q): {"answer": "是", "p": 0.9, "ambiguous": False}})
    out = core.check_rules(full_client(memories=[soft, unsafe], compiles=[stored(soft, q), stored(unsafe, unsafe_res)],
                                       versions=[version(V1)]), PROJ, [V1], user_id=ME)
    assert out["rules"]["code_refused"] == 1 and out["rules"]["by_code"] == 0
    assert out["rules"]["partial"] == 1 and [p["rule_id"] for p in out["rules"]["partial_rules"]] == [soft["id"]]
    assert "1 条规则这次查不了" in out["next_step"] and "有 1 条规则只查了一部分" in out["next_step"]


@pytest.mark.parametrize("reply", [
    {"written": 0, "write_error": "账本一行没写: 示例错误"},
    {"write_error": None},                                  # 回包里没有 written: write=true 时 Jev 一定给, 没给就是没落账
])
def test_a_failed_ledger_write_is_logged_and_counted_but_not_told_to_the_writer(wired, caplog, monkeypatch, reply):
    """check_rules 发 write=true, 是为了让规则题的答案按版本落进 TV 账本、以后拿来校准; Jev 账本写失败照样回 200
    (written=0 + write_error)。原来只读 detail.desk_rules: 影子期要攒的数据次次丢了也没人知道。"""
    import telemetry
    events: list[tuple] = []
    monkeypatch.setattr(telemetry, "log_event", lambda event, **kw: events.append((event, kw)))
    qid = qid_of(q_soft())
    wired.judge = lambda p: {"subject_id": p["subject_id"], "passed": True, "plan": [], **reply,
                             "detail": {"desk_rules": {qid: {"answer": "是", "p": 0.9, "ambiguous": False}}}}
    c = full_client(memories=[R_SOFT], compiles=[stored(R_SOFT, q_soft())], versions=[version(V1), version(V2)])
    with caplog.at_level("WARNING", logger="deskcore"):
        out = core.check_rules(c, PROJ, [V1, V2], user_id=ME)
    assert out["rules"]["ledger_write_errors"] == 2
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING" and "账本" in r.getMessage()]
    assert any(V1 in m for m in warned) and any(V2 in m for m in warned), "日志里带版本号"
    (ev,) = [kw for e, kw in events if e == "deskcore_check_rules_ledger"]
    assert ev["ledger_write_errors"] == 2 and sorted(ev["version_ids"]) == sorted([V1, V2])
    assert "账本" not in out["next_step"] and "这次查的规则都守住了" in out["next_step"], "写手那边的结论不受影响"

    caplog.clear()
    events.clear()
    wired.judge = answers(**{qid: {"answer": "是", "p": 0.9, "ambiguous": False}})
    out = core.check_rules(c, PROJ, [V1], user_id=ME)
    assert "ledger_write_errors" not in out["rules"], "落账成功的不记"
    assert not [e for e, _kw in events if e == "deskcore_check_rules_ledger"]


def test_with_writer_tags_only_title_and_body_match_the_delivered_text(wired, monkeypatch):
    """写手单独传了 tags 的那篇: 交付稿的话题行按写手传的拼, 版本行里却没存它们 —— check_rules 拼出来的话题对不上, 只是
    下限; 标题、正文照样和 score_drafts、delivery.text 一字不差。编出来的 code 里看话题的本来就不跑(UNSTORED_PARTS)。"""
    seen: list[list[dict]] = []
    real = rulecheck.check

    def spy(rules, drafts, *, indices=None, tags=None, expects=()):
        seen.append([rulecheck._parts(d, None if tags is None else tags[k]) for k, d in enumerate(drafts)])
        return real(rules, drafts, indices=indices, tags=tags, expects=expects)
    monkeypatch.setattr(rulecheck, "check", spy)

    tpl = {"layout": "blocks", "tags": {"content": 2, "fixed": ["示例固定"], "conditional": []}}
    rules, comps = _body_rules()
    draft = {"title": TITLE, "body": BODY + "\n#示例尾甲 #示例尾乙", "tags": ["示例传甲", "示例传乙"]}
    projects = _project(default_params=json.dumps({"delivery": tpl}))
    c = make_client(rules, comps, projects=projects, versions=[version(V1, body=draft["body"])])
    core._delivery_and_rule_checks(c, projects[0], PROJ, ME, [draft], [0], {})
    out = core.check_rules(c, PROJ, [V1], user_id=ME)
    assert out["status"] == "checked"
    by_score, by_check = seen
    assert by_check[0]["title"] == by_score[0]["title"] and by_check[0]["body"] == by_score[0]["body"]
    text = delivery.assemble(tpl, [draft])["text"]
    assert text.startswith(f"【标题】{by_check[0]['title'][0]}\n【正文】{by_check[0]['body'][0]}\n#"), "标题正文和交付稿对得上"
    assert by_check[0]["tags"] != by_score[0]["tags"], "写手传的 tags 没存: 这边的话题只是下限"


def test_docs_do_not_claim_tags_and_comments_match_the_delivered_text():
    """第三轮复审: docs §3.11 和 _delivered_text 原来说话题、评论两边一字不差 —— 只在写手没传 tags、没传评论时成立。
    以后谁照着这句把话题从 UNSTORED_PARTS 里拿出来, 写手传了 tags 的稿子就会按错的话题判。"""
    from pathlib import Path
    doc = (Path(__file__).resolve().parent.parent / "docs" / "deskcore.md").read_text(encoding="utf-8")
    sec = doc[doc.index("### 3.11 "):]
    sec = sec[:sec.index("\n### ")]
    assert "两边一字不差；哪个 `#` 算话题" not in sec
    assert "只有写手没传 `tags` 时一样" in sec
    assert "只是下限" in core._delivered_text.__doc__
    assert "交付模板的固定话题也不随稿子存" not in Path(rulecompile.__file__).read_text(encoding="utf-8")


def test_rest_and_mcp_answer_instead_of_erroring_when_rule_compiles_is_missing(monkeypatch, wired, caplog):
    """015 没跑时 PostgREST 回 PGRST205。原来 check_rules 直接抛: REST 是 500 —— 对模型的意思是「待会儿重试」, 而每次存档的
    next_step 都让它来调; MCP(主通道)是一个工具错误, deskcore 一行日志都没有。现在两条路都是一份 status=error 的结果 +
    一句「这次规则没查成」, 服务端记一行带项目号的 ERROR。"""
    pytest.importorskip("mcp")
    pytest.importorskip("fastapi")
    import asyncio
    from fastapi.testclient import TestClient
    import deskcore.app as A
    app = A.app
    c = full_client()

    def no_table(q, _real=c._execute):
        if q.table_name == "rule_compiles":
            raise RuntimeError("{'code': 'PGRST205', 'message': \"Could not find the table "
                               "'autowriter.rule_compiles' in the schema cache\"}")
        return _real(q)
    monkeypatch.setattr(c, "_execute", no_table)
    monkeypatch.setenv("DESKCORE_KEYS", json.dumps({"test-key": {"user_id": ME, "name": "测试"}}))
    monkeypatch.setattr(core, "sb", lambda: c)
    r = TestClient(app, raise_server_exceptions=False).post(
        "/tool/check_rules", headers={"X-Deskcore-Key": "test-key"}, json={"project_id": PROJ, "version_ids": [V1]})
    assert r.status_code == 200, r.text[:300]
    got = r.json()["result"]
    assert got["status"] == "error" and "没查成" in got["next_step"] and wired.calls == []

    class Caller:
        user_id = ME
    token = A._caller.set(Caller())
    try:
        with caplog.at_level("ERROR", logger="deskcore"):
            res = asyncio.run(A._mcp.call_tool("check_rules", {"project_id": PROJ, "version_ids": [V1]}))
    finally:
        A._caller.reset(token)
    assert "没查成" in json.dumps(res, ensure_ascii=False, default=str), "MCP 回的是结果, 不是工具错误"
    assert any(r.levelname == "ERROR" and PROJ in r.getMessage() for r in caplog.records)


# ══════════════════════════════════════════════════════════════════════
# 11 · 独立复审 on aw E: 没查的 / 只查了一半的 / 没列全的, 都不许说成守住了或说成别的
# ══════════════════════════════════════════════════════════════════════

WITHDRAWN_TEXT = "示例硬规则: 标题正文都不许出现「示例禁词」"
WITHDRAWN_CODE = {"kind": "forbidden_word", "words": ["示例禁词"], "where": ["title", "body"]}


def test_a_withdrawn_check_that_jev_compiled_to_code_only_is_not_said_to_hold(wired):
    """写手记了一条带词表 check 的硬规则, Jev 把它编成了同一份代码检查(没有规则题); 写手再用 {"kind": "free_text"} 撤掉 check。
    撤掉不改 sha, 不会重编; score_drafts 不查它, check_rules 里编出来的 code 也不跑 —— 什么都没查它。原来它哪个桶都不进,
    next_step 照说「这次查的规则都守住了」; 只剩它一条时说成「流程、交付、写作方向」那种查不了。"""
    c = full_client(memories=[R_SOFT], compiles=[stored(R_SOFT, q_soft())],
                    versions=[version(V1, body=BODY + "这里有示例禁词。")])
    rid = core.record_rule(c, PROJ, WITHDRAWN_TEXT, severity="hard", user_id=ME,
                           check={"kind": "forbidden_word", "words": ["示例禁词"]})["memory_id"]
    wired.compile_one = lambda r: (code_result(r["id"], jev_sha(r["text"], r.get("applicability")), WITHDRAWN_CODE)
                                   if r["text"] == WITHDRAWN_TEXT else question_result(r["id"], jev_sha(r["text"])))
    assert core.compile_rules(c, PROJ)["status"] == "ok"
    core.record_rule(c, PROJ, WITHDRAWN_TEXT, severity="hard", user_id=ME, check={"kind": "free_text"})
    assert core.compile_rules(c, PROJ)["status"] == "up_to_date", "撤掉不改 sha, 不重编"
    wired.judge = answers(**{qid_of(q_soft()): {"answer": "是", "p": 0.9, "ambiguous": False}})
    out = core.check_rules(c, PROJ, [V1], user_id=ME)
    assert out["status"] == "checked" and out["versions"][0]["violations"] == []
    assert "都守住了" not in out["next_step"] and "有 1 条规则撤掉了代码检查" in out["next_step"]
    assert out["rules"]["code_withdrawn"] == 1 and out["rules"]["by_code"] == 0

    row = next(m for m in c.rows["memories"] if m["id"] == rid)
    comp = next(r for r in c.rows["rule_compiles"] if r["memory_id"] == rid)
    alone = core.check_rules(make_client([row], [comp], versions=[version(V1)]), PROJ, [V1], user_id=ME)
    assert alone["status"] == "nothing_to_check"
    assert "流程、交付、写作方向" not in alone["next_step"] and "有 1 条规则撤掉了代码检查" in alone["next_step"]
    assert "不能说它们守住了" in alone["next_step"] and alone["rules"]["code_withdrawn"] == 1


def test_a_project_whose_compiled_rules_are_hand_set_is_not_told_they_are_process_rules(wired, kicks):
    """人手设过能跑的 check 的规则, 编出来的 code 让路(score_drafts 存档时已经按人设的查过)。池子里只有它(或者只有它和一条真的
    not_checkable)时 nothing_to_check, 原来说「这个项目的规则都是查不了的那种(流程、交付、写作方向), 不用做什么」——
    score_drafts 刚刚就这条报过「必须改」。"""
    sha = jev_sha(R_HANDSET["content"])
    hs = stored(R_HANDSET, code_result(R_HANDSET["id"], sha, {"kind": "forbidden_word", "words": ["人设禁词"],
                                                               "where": ["body"]}))
    nc = stored(R_NOT_CHECKABLE, {"id": R_NOT_CHECKABLE["id"], "sha": jev_sha(R_NOT_CHECKABLE["content"]),
                                  "verdict": "not_checkable", "code": None, "questions": [], "coverage": None,
                                  "uncovered": "", "why": "交付"})
    for memories, compiles in (([R_HANDSET], [hs]), ([R_HANDSET, R_NOT_CHECKABLE], [hs, nc])):
        out = core.check_rules(full_client(memories=memories, compiles=compiles, versions=[version(V1)]),
                               PROJ, [V1], user_id=ME)
        assert out["status"] == "nothing_to_check"
        assert "流程、交付、写作方向" not in out["next_step"] and "不用做什么" not in out["next_step"]
        assert "score_drafts 存档时已经按它查过" in out["next_step"] and "「必须改」照改" in out["next_step"]
        assert out["rules"]["by_score_drafts"] == 1, "rules 里也数得到它(原来池子 1 条、各项全是 0)"
    assert wired.calls == []
    only_nc = core.check_rules(full_client(memories=[R_NOT_CHECKABLE], compiles=[nc], versions=[version(V1)]),
                               PROJ, [V1], user_id=ME)
    assert "流程、交付、写作方向" in only_nc["next_step"], "真的全是 not_checkable 的照旧这么说"


def test_a_hit_found_before_the_regex_ran_out_of_time_still_has_to_be_fixed(wired, monkeypatch):
    """rulecheck 先查标题、再查正文, 超时时已经找到的命中照报: 同一条规则同一篇既在 violations(必须改)里, 又在
    code_unchecked 里。原来 next_step 只说 code_unchecked「不是违规」, 写手会当成连那条必须改一起撤了(score_drafts 的
    rule_checks_unchecked 第六轮复审就是这么改的: 之前查出来的照改)。"""
    monkeypatch.setattr(config, "REGEX_SEARCH_TIMEOUT_SEC", 0.1)
    slow = "a*aaa*aaa*aaa*aaa*aaa*aaa*aaa*b"
    rule = mem(16, "示例硬规则: 标题正文都不许出现某种写法", severity="hard")
    res = code_result(rule["id"], jev_sha(rule["content"]),
                      {"kind": "forbidden_regex", "pattern": slow, "where": ["title", "body"]})
    out = core.check_rules(full_client(memories=[rule], compiles=[stored(rule, res)],
                                       versions=[version(V1, title="a" * 14 + "b", body=BODY + "a" * 3000)]),
                           PROJ, [V1], user_id=ME)
    (v1,) = out["versions"]
    assert [(e["rule_id"], e["what"][:3]) for e in v1["violations"]] == [(rule["id"], "必须改")]
    assert [u["rule_id"] for u in v1["code_unchecked"]] == [rule["id"]]
    assert "同一篇里已经列在 violations 的照改" in out["next_step"]
    assert "没查完的那部分不是违规" in out["next_step"]


def test_writer_protocol_and_docs_name_every_case_that_cannot_be_called_held():
    """协议 5a 列着所有「不能说守住了」的情形, 原来唯独没有 code_unchecked(代码没查完); docs §3.11「这句只在…全是 0 时
    才说」也漏了它。rules.by_code 原来写成「真跑了的条数」, 限时引擎一篇都没搜成的正则也算在里面。"""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    proto = Path(core.__file__).with_name("protocol.md").read_text(encoding="utf-8")
    skill = (root / "skills" / "bywood-writing-desk" / "SKILL.md").read_text(encoding="utf-8")
    for text in (proto, skill):
        sec = text[text.index("**5a. `check_rules`**"):text.index("**5b.")]
        assert "`code_unchecked`" in sec and "已经列出来的「必须改」照改" in sec
        assert "`rules.code_withdrawn`" in sec
    doc = (root / "docs" / "deskcore.md").read_text(encoding="utf-8")
    sec = doc[doc.index("### 3.11 "):]
    sec = sec[:sec.index("\n### ")]
    held = sec[sec.index("现在这句只在"):]
    held = held[:held.index("才说")]
    assert "代码没查完" in held and "撤掉了代码检查又没有题" in held and "正文是空的" in held
    assert "（那是真跑了的条数）" not in sec and "`rules.code_timed_out`）" in sec
    assert "code_timed_out" in tools.check_rules.__doc__ and "by_code" in tools.check_rules.__doc__


def test_compiled_code_that_also_looks_at_comments_or_tags_is_only_partly_checked(wired):
    """Jev 编出来的 where 混着标题 / 正文和评论 / 话题: 评论、话题不随稿子存, 只跑标题 / 正文那一半。原来照算 by_code、
    不算 partial, next_step 说「这次查的规则都守住了」—— 评论里违了硬规则也报守住了。看得见的那一半命中照报。"""
    r_cmt = mem(25, "示例硬规则: 标题、正文和评论里都不能出现「示例联系方式」", severity="hard")
    r_tag = mem(26, "示例软规则: 正文和话题里都不用「示例平替」")
    codes = {r_cmt["id"]: {"kind": "forbidden_word", "words": ["示例联系方式"], "where": ["title", "body", "comments"]},
             r_tag["id"]: {"kind": "forbidden_word", "words": ["示例平替"], "where": ["body", "tags"]}}
    comps = [stored(r, code_result(r["id"], jev_sha(r["content"]), codes[r["id"]])) for r in (r_cmt, r_tag)]
    out = core.check_rules(full_client(memories=[r_cmt, r_tag], compiles=comps,
                                       versions=[version(V1), version(V2, body=BODY + "示例联系方式在这里。")]),
                           PROJ, [V1, V2], user_id=ME)
    v1, v2 = out["versions"]
    assert v1["violations"] == [] and [e["rule_id"] for e in v2["violations"]] == [r_cmt["id"]], "看得见的那一半照报"
    only_v1 = core.check_rules(full_client(memories=[r_cmt, r_tag], compiles=comps, versions=[version(V1)]),
                               PROJ, [V1], user_id=ME)
    assert only_v1["summary"]["must_fix"] == 0 and "都守住了" not in only_v1["next_step"]
    assert "有 2 条规则只查了一部分" in out["next_step"]
    assert out["rules"]["by_code"] == 2 and out["rules"]["partial"] == 2
    assert {p["rule_id"]: p["uncovered"] for p in out["rules"]["partial_rules"]} == {
        r_cmt["id"]: "评论那部分没查(不随稿子存)", r_tag["id"]: "话题标签那部分没查(不随稿子存)"}
    (row,), _ = rulecompile.code_rules([(r_cmt, comps[0]["result"])])
    assert row["rule_payload"]["where"] == ["title", "body"] and row["stripped"] == ["comments"]


def test_an_empty_body_is_not_said_to_have_kept_the_rule_questions(wired):
    """score_drafts 收标题有、正文空的稿子; check_rules 不发 Jev(empty_body), 标题上的题、「正文要带…」的题都没问。原来不算
    没判完、也不另计数, 题库里的硬规则题一道没判, next_step 照说「这次查的规则都守住了」。"""
    wired.judge = answers(**{qid_of(q_hard()): {"answer": "否", "p": 0.93, "ambiguous": False}})
    out = core.check_rules(full_client(memories=[R_HARD], compiles=[stored(R_HARD, q_hard())],
                                       versions=[version(V1), version(V2, body="")]), PROJ, [V1, V2], user_id=ME)
    v1, v2 = out["versions"]
    assert v1["judge_status"] == "ok" and v2["judge_status"] == "empty_body"
    assert "都守住了" not in out["next_step"] and "有 1 篇正文是空的, 规则题没判" in out["next_step"]
    assert out["summary"]["not_finished"] == 0 and out["summary"]["empty_body"] == 1


def test_items_past_the_per_version_cap_are_counted_in_next_step(wired):
    """每篇建议改补到 12 条、拿不准最多 5 条, 多的只剩每篇的 more。原来 next_step 和工具说明都不提 more, 写手照列出来的说完
    就算了, 用户不知道还有几条软规则没守住(score_drafts 那边有 RULE_CHECKS_MORE_NEXT_STEP)。"""
    soft = [mem(140 + i, f"示例软规则第 {i} 条") for i in range(core.CHECK_RULES_MAX_LISTED + 2)]
    shaky = [mem(170 + i, f"示例软规则(拿不准)第 {i} 条") for i in range(core.CHECK_RULES_MAX_UNSURE + 1)]

    def q(r):
        return question_result(r["id"], jev_sha(r["content"]), ask=f"正文有没有违反{r['content']}？", pass_if="否")
    by_qid = {q(r)["questions"][0]["qid"]: {"answer": "是", "p": 0.9, "ambiguous": False} for r in soft}
    by_qid.update({q(r)["questions"][0]["qid"]: {"answer": "是", "p": 0.55, "ambiguous": True} for r in shaky})
    wired.judge = answers(**by_qid)
    out = core.check_rules(full_client(memories=soft + shaky, compiles=[stored(r, q(r)) for r in soft + shaky],
                                       versions=[version(V1), version(V2)]), PROJ, [V1, V2], user_id=ME)
    assert [v["more"] for v in out["versions"]] == [3, 3]
    assert "还有 6 条建议改 / 拿不准没列出来" in out["next_step"] and "如实告诉用户" in out["next_step"]
    assert out["summary"]["more"] == 6
    assert "more" in tools.check_rules.__doc__ and "12 条" in tools.check_rules.__doc__
