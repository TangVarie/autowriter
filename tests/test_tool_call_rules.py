"""两条线接起来的那一缝(2026-11-11): 每次工具调用留的那一行(deskcore/calllog.py) × check_rules 的新回包 × 存档之后的规则补查。

钉住:
  1. check_rules 经 REST / MCP 调一次, 那一行的 ``meta.rules`` 里是回包的数: 怎么收场、查了几版、必须改 / 建议改 / 拿不准 /
     没判出来(其中 Jev 指不出是哪一句的)各几条、列出来的 unsure_rules 几条(硬约束几条)、规则池里各是怎么查的 / 为什么没查;
     规则 id、规则原文、题面、Jev 指出的那一句一个字都不进;
  2. 没查成(编译结果读不出来)和一条都查不了的两种收场也留得下来;
  3. 存档之后的规则补查(core._rule_sweep)调的 check_rules **不留**工具调用那一行, 也不写 batch_metrics: 库里只有这次
     score_drafts 自己的两行(存档那一行 + 工具调用那一行), 补查里记的数进不了 score_drafts 的那一行。
全部是合成项目、合成规则、现编的文字和测试里现编的钥匙。
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi.testclient")
pytest.importorskip("mcp")

from deskcore import calllog, core                                          # noqa: E402
from tests.test_check_rules import (BODY, ME, PROJ, R_CMT, R_HARD, R_SOFT, R_UNCOMPILED, V1, V2, V_THEIRS,  # noqa: E402
                                    _multi, answers, full_client, q_hard, q_soft, qid_of, version)
from tests.test_check_rules import kicks, wired                              # noqa: E402,F401 — 夹具
from tests.test_deskcore_commit_judge import BODY_A, BODY_B                  # noqa: E402
from tests.test_deskcore_commit_judge import ME as COMMIT_ME                 # noqa: E402
from tests.test_deskcore_commit_judge import PROJ as COMMIT_PROJ             # noqa: E402
from tests.test_deskcore_commit_judge import _client as _commit_client       # noqa: E402
from tests.test_revision_recheck import inline_pool                          # noqa: E402,F401 — 夹具
from tests.test_rule_compile import (OTHER_PROJ, Q_COMMENT, Q_COMMENT_2, _fresh_compile_state, jev, jev_sha,  # noqa: E402,F401
                                     mem, question_result, stored)
from tests.test_rule_sweep import _age, _fresh_sweep_state                    # noqa: E402,F401 — 夹具
from tests.test_tool_call_log import H, _mcp_call, _no_text, _rest, _rows, log  # noqa: E402,F401 — log 是夹具

assert ME == COMMIT_ME, "两套假件用的是同一个调用者(现编的钥匙对的就是它)"

RULE_MARK, ASK_MARK, EVIDENCE_MARK = "规则原文标记戊RK5", "题面标记己QM6", "证据标记庚EV7"

R_LEAN_HARD = mem(301, f"示例硬规则(拿不准, 偏向没守住): {RULE_MARK}", severity="hard")
R_LEAN_SOFT = mem(302, "示例软规则(拿不准, 偏向没守住)")
R_KEPT = mem(303, "示例软规则(拿不准, 偏向守住)")
R_UNLOCATED = mem(304, "示例软规则: 不要写成说明书")


def _q(rule: dict) -> dict:
    return question_result(rule["id"], jev_sha(rule["content"], rule.get("applicability")),
                           ask=f"正文有没有违反这一条{ASK_MARK}？", pass_if="否", note=f"要点 {rule['content']}")


def _rules_world():
    """两版稿子; 规则池里: 一条必须改(带 Jev 指出的那一句)、一条建议改、两条拿不准偏向没守住(一硬一软)、一条拿不准偏向
    守住、一条 Jev 指不出是哪一句、一条题全在问评论(查不了)、一条还没编。"""
    cmt = _multi(R_CMT, Q_COMMENT, Q_COMMENT_2)
    rules = [R_HARD, R_SOFT, R_LEAN_HARD, R_LEAN_SOFT, R_KEPT, R_UNLOCATED, R_CMT, R_UNCOMPILED]
    compiles = ([stored(R_HARD, q_hard()), stored(R_SOFT, q_soft()), stored(R_CMT, cmt)]
                + [stored(r, _q(r)) for r in (R_LEAN_HARD, R_LEAN_SOFT, R_KEPT, R_UNLOCATED)])
    by_qid = {
        qid_of(q_hard()): {"answer": "是", "p": 0.91, "ambiguous": False, "evidence": f"第三句{EVIDENCE_MARK}。"},
        qid_of(q_soft()): {"answer": "否", "p": 0.83, "ambiguous": False},
        qid_of(_q(R_LEAN_HARD)): {"answer": "是", "p": 0.6, "ambiguous": False},
        qid_of(_q(R_LEAN_SOFT)): {"answer": "是", "p": 0.62, "ambiguous": False},
        qid_of(_q(R_KEPT)): {"answer": "否", "p": 0.61, "ambiguous": False},
        qid_of(_q(R_UNLOCATED)): {"answer": "是", "p": 0.9, "ambiguous": False, "evidence": None,
                                  "invalid_reason": "evidence_not_found"},
    }
    client = full_client(memories=rules, compiles=compiles,
                         versions=[version(V1), version(V2, body=BODY + "第五句另起一段。"),
                                   version(V_THEIRS, OTHER_PROJ)])
    return client, by_qid


# ── 1. check_rules 的那一行: 新回包的数 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("channel", ["rest", "mcp"])
def test_check_rules_leaves_the_counts_of_the_slimmer_reply(log, wired, monkeypatch, channel):       # noqa: F811
    c, by_qid = _rules_world()
    wired.judge = answers(**by_qid)
    monkeypatch.setattr(core, "sb", lambda: c)
    args = {"project_id": PROJ, "version_ids": [V1, V2, V_THEIRS]}
    if channel == "rest":
        r = _rest().post("/tool/check_rules", headers=H, json=args)
        assert r.status_code == 200, r.text
        reply = r.json()["result"]
    else:
        res = _mcp_call("check_rules", args)
        assert not res.isError, res.content
        reply = res.structuredContent if isinstance(res.structuredContent, dict) and "status" in res.structuredContent \
            else json.loads(res.content[0].text)
    # 回包是 2026-11-11 那一版的形状: 拿不准 / 没判出来的每篇只有条数, 偏向没守住的按规则汇在最外层
    assert reply["status"] == "checked" and all("unsure" not in v and "unjudged" not in v for v in reply["versions"])
    assert [e["severity"] for e in reply["unsure_rules"]] == ["hard", "soft"]

    (row,) = _rows(c)
    meta = row["meta"]
    assert (meta["tool"], meta["channel"], meta["outcome"], meta["items"]) == ("check_rules", channel, "ok", 3)
    assert row["project_id"] == PROJ and row["batch_id"] is None
    assert meta["rules"] == {
        "status": "checked", "versions": 2,
        "must_fix": 2, "suggest": 2, "unsure": 6, "unjudged": 2, "evidence_not_found": 2, "not_finished": 0,
        "flagged": 2, "judged": 2,
        "unsure_rules": 2, "unsure_rules_hard": 1,
        "excluded": 1,
        "pool": {"pool": 8, "by_jev": 6, "by_code": 0, "uncompiled": 1, "not_checkable": 0, "code_unavailable": 1},
    }
    # 和回包对得上(数是从回包里取的, 不是另算的)
    assert {k: meta["rules"][k] for k in reply["summary"]} == reply["summary"]
    assert meta["rules"]["unsure_rules"] == len(reply["unsure_rules"])
    blob = _no_text(row, RULE_MARK, ASK_MARK, EVIDENCE_MARK)
    for rule in (R_HARD, R_SOFT, R_LEAN_HARD, R_LEAN_SOFT, R_CMT):
        assert rule["id"] not in blob, "规则 id 不进这一行"
    assert V1 not in blob and V2 not in blob, "版本 id 不进这一行"


# ── 2. 没查成 / 一条都查不了 ───────────────────────────────────────────────────────────────────────

def test_an_unreadable_rule_cache_is_an_error_result_row(log, wired, monkeypatch):                   # noqa: F811
    c = full_client()
    monkeypatch.setattr(core, "sb", lambda: c)
    monkeypatch.setattr(core.store, "rule_compiles_for", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("PGRST205")))
    r = _rest().post("/tool/check_rules", headers=H, json={"project_id": PROJ, "version_ids": [V1]})
    assert r.status_code == 200 and r.json()["result"]["status"] == "error"
    (row,) = _rows(c)
    assert row["meta"]["outcome"] == "error_result" and row["project_id"] is None
    assert row["meta"]["rules"]["status"] == "error" and row["meta"]["rules"]["pool"]["pool"] > 0
    _no_text(row, "PGRST205")


def test_nothing_to_check_keeps_why_nothing_was_checked(log, wired, monkeypatch):                    # noqa: F811
    """规则的题全在问评论: 一道题都不发, 这一行说得出是「查不了」而不是「都守住了」。"""
    c = full_client(memories=[R_CMT], compiles=[stored(R_CMT, _multi(R_CMT, Q_COMMENT))], versions=[version(V1)])
    monkeypatch.setattr(core, "sb", lambda: c)
    r = _rest().post("/tool/check_rules", headers=H, json={"project_id": PROJ, "version_ids": [V1]})
    assert r.status_code == 200 and r.json()["result"]["status"] == "nothing_to_check"
    (row,) = _rows(c)
    assert row["meta"]["rules"] == {"status": "nothing_to_check",
                                    "pool": {"pool": 1, "by_jev": 0, "by_code": 0, "uncompiled": 0, "not_checkable": 0,
                                             "code_unavailable": 1}}
    _no_text(row)


# ── 3. 存档之后的规则补查不留行 ─────────────────────────────────────────────────────────────────────

def test_the_catch_up_sweep_leaves_no_tool_call_row_and_no_metrics_row(log, inline_pool, jev, monkeypatch):  # noqa: F811
    """score_drafts 经 REST 存两次: 第二次存档排的补查去查第一次那一版(就地跑, 和 score_drafts 在同一个线程里, 那一次调用
    收数的字典还装着)。补查调的 check_rules 这里换成会往调用留痕里记数的录音机 —— 记的数进不了任何一行。"""
    seen: list[dict] = []

    def recording_check_rules(client, project_id, version_ids, *, user_id=None, tactic=""):
        seen.append({"version_ids": list(version_ids), "collecting": calllog.active()})
        calllog.note(swept=len(version_ids), post8="deadbeef")
        return {"status": "checked", "versions": [{"version_id": v, "judge_status": "ok"} for v in version_ids],
                "summary": {"versions": len(version_ids), "must_fix": 7}}
    monkeypatch.setattr(core, "check_rules", recording_check_rules)
    c = _commit_client()
    monkeypatch.setattr(core, "sb", lambda: c)

    first = _rest().post("/tool/score_drafts", headers=H,
                         json={"project_id": COMMIT_PROJ, "drafts": [{"title": "秋招等回音", "body": BODY_A}]})
    assert first.status_code == 200 and first.json()["result"]["written"] == 1, first.text
    v1 = first.json()["result"]["version_ids"][0]
    assert seen == [], "刚存的这一版不到 3 分钟, 补查不碰"
    _age(c)
    second = _rest().post("/tool/score_drafts", headers=H,
                          json={"project_id": COMMIT_PROJ, "drafts": [{"title": "绩点二点八", "body": BODY_B}]})
    assert second.status_code == 200 and second.json()["result"]["written"] == 1, second.text
    (call,) = seen
    assert v1 in call["version_ids"] and second.json()["result"]["version_ids"][0] not in call["version_ids"], "补查真跑了"
    assert call["collecting"] is False, "跑的时候没有人在替哪一次调用收数"

    rows = _rows(c)
    assert [r["meta"]["tool"] for r in rows] == ["score_drafts", "score_drafts"], "只有两次 score_drafts 自己的那一行"
    for row in rows:
        assert "rules" not in row["meta"] and "comments" not in row["meta"]
        assert "swept" not in json.dumps(row) and "deadbeef" not in json.dumps(row), "补查里记的数没进 score_drafts 那一行"
    by_mode: dict = {}
    for r in c.rows["batch_metrics"]:
        by_mode[r["meta"]["mode"]] = by_mode.get(r["meta"]["mode"], 0) + 1
    assert by_mode == {"deskcore_commit": 2, "tool_call": 2}, "补查不往 batch_metrics 写任何一行"
    for r in c.rows["batch_metrics"]:
        assert "must_fix" not in json.dumps(r.get("meta", {}).get("rules", {})) and "sweep" not in json.dumps(r)
