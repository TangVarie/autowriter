"""判出来了、账本没写进去(外部评审 11-04 F04, 2026-11-04)。

Jev 的 /judge_draft 写 TV 账本失败照样回 200: 答案照给, ``written`` 是 0、``write_error`` 里说一句。原来 judge_client 只读
``written``、丢掉 ``write_error``, 存档那一路记成 judge_status=ok、不计数不报警 —— 账本悄悄少一篇(check_rules 那一路早有
_ledger_write_failed 管着, 存档这一路没有)。这里钉住: judge_status 仍是 ok(判定本身成功了), 但 write_error 带回来(脱敏、截断),
存档记 meta.judge[].ledger_error、counters.judge_ledger_errors、一条 WARNING 和一条埋点; 回包里不出现(写手的分数不受影响)。
"""

from __future__ import annotations

import logging

import pytest
import requests

import config
import judge_client as J
import telemetry
from deskcore import core
from tests.test_deskcore_commit_judge import BODY_A, BODY_B, ME, PROJ, _client
from tests.test_judge_client import PAYLOAD, _OK_BODY, _Resp

WERR = "账本已确认写入 0/23 行, 第 1 批失败: OSError sk-ant-api03-" + "A" * 40


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(config, "JUDGE_URL", "https://judge.example.invalid/")
    monkeypatch.setattr(config, "JUDGE_API_KEY", "jk-secret")
    monkeypatch.setattr(config, "JUDGE_TIMEOUT_SEC", 8.0)
    reply = {"body": dict(_OK_BODY)}
    monkeypatch.setattr(requests, "post", lambda url, **kw: _Resp(200, reply["body"]))
    return reply


def test_a_200_with_a_write_error_stays_ok_but_carries_the_error(wired):
    wired["body"] = {**_OK_BODY, "written": 0, "write_error": WERR + " " + "x" * 600}
    out = J.judge_draft(PAYLOAD)
    assert out["judge_status"] == J.JUDGE_OK and out["written"] == 0
    assert out["ledger_ok"] is False
    assert out["write_error"].startswith("账本已确认写入 0/23 行")
    assert "sk-ant-api03-AAAA" not in out["write_error"], "先脱敏"
    assert len(out["write_error"]) <= 300, "再截断"


def test_written_missing_also_counts_as_a_ledger_failure(wired):
    wired["body"] = {k: v for k, v in _OK_BODY.items() if k != "written"}
    out = J.judge_draft(PAYLOAD)
    assert out["judge_status"] == J.JUDGE_OK and out["ledger_ok"] is False and "write_error" not in out


def test_a_normal_200_is_ledger_ok_without_a_write_error(wired):
    out = J.judge_draft(PAYLOAD)
    assert out["judge_status"] == J.JUDGE_OK and out["ledger_ok"] is True and "write_error" not in out


# ── 存档那一路 ────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def judge_reply(monkeypatch):
    monkeypatch.setattr(config, "JUDGE_URL", "https://judge.example.invalid")
    monkeypatch.setattr(config, "JUDGE_API_KEY", "jk")
    monkeypatch.setattr(config, "JUDGE_TIMEOUT_SEC", 5.0)
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    bodies = {}

    def fake_post(url, *, json=None, **kw):
        return _Resp(200, bodies.get(json["title"], {**_OK_BODY, "subject_id": json["subject_id"], "written": 23}))
    monkeypatch.setattr(requests, "post", fake_post)
    events = []
    real = telemetry.log_event
    monkeypatch.setattr(telemetry, "log_event", lambda name, **kw: (events.append((name, kw)), real(name, **kw)))
    return bodies, events


def test_score_drafts_records_ledger_failures_in_metrics_and_warns(judge_reply, caplog):
    bodies, events = judge_reply
    bodies["账本没写进去的那篇"] = {**_OK_BODY, "written": 0, "write_error": WERR}
    c = _client()
    with caplog.at_level(logging.WARNING, logger=core.logger.name):
        out = core.commit_drafts(c, PROJ, [{"title": "账本没写进去的那篇", "body": BODY_A},
                                           {"title": "正常的那篇", "body": BODY_B}], user_id=ME)
    j = out["judge"]
    assert j["summary"] == {"ok": 2}, "判定本身成功了: 仍是 ok"
    assert all(core.JUDGE_LEDGER_ERROR_KEY not in r and "ledger_error" not in r for r in j["results"]), "不进回包"
    bad, good = out["version_ids"]

    (row,) = c.rows["batch_metrics"]
    assert row["counters"]["judge_ledger_errors"] == 1 and row["counters"]["judge_ok"] == 2
    by_vid = {e["version_id"]: e for e in row["meta"]["judge"]}
    assert by_vid[bad]["status"] == "ok" and by_vid[bad]["written"] == 0
    assert by_vid[bad]["ledger_error"].startswith("账本已确认写入 0/23 行")
    assert "sk-ant-api03-AAAA" not in by_vid[bad]["ledger_error"]
    assert "ledger_error" not in by_vid[good]

    assert any("账本" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)
    led = [kw for name, kw in events if name == "deskcore_commit_judge_ledger"]
    assert led and led[0]["judge_ledger_errors"] == 1 and led[0]["version_ids"] == [bad]


def test_a_clean_commit_has_no_ledger_counter_or_event(judge_reply):
    _bodies, events = judge_reply
    c = _client()
    core.commit_drafts(c, PROJ, [{"title": "正常的那篇", "body": BODY_B}], user_id=ME)
    (row,) = c.rows["batch_metrics"]
    assert "judge_ledger_errors" not in row["counters"]
    assert all("ledger_error" not in e for e in row["meta"]["judge"])
    assert not [1 for name, _ in events if name == "deskcore_commit_judge_ledger"]
