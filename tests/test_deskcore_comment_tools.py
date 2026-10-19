import pytest
import requests
import concurrent.futures as cf
import threading
import time
import judge_client

from deskcore import core, tools
from tests.test_deskcore_judge_tools import wired
from tests.test_deskcore_commit_judge import ME, PROJ, OTHER_PROJ, _client


def test_single_comment_contract_and_authenticated_project(wired):
    calls, reply = wired
    reply["fn"] = lambda *args: type("Reply", (), {"status_code": 200, "json": lambda self:
        {"passed": False, "plan": [{"qid": "register"}], "needs_review": True,
         "profile": {}, "hard_fails": [], "ambiguous": [], "ledger_rows": [{"secret": "x"}]}})()
    out = core.comment_repair_plan_for(_client(), PROJ, "标题", "正文", "评论", {"id": "q", "speech_act": ["提问"]}, user_id=ME)
    assert out["judge_status"] == "ok" and out["needs_review"] is True
    assert "ledger_rows" not in out
    method, url, headers, body, timeout = calls[0]
    assert url.endswith("/comment_repair_plan_for") and body["project"] == "TUGE_phase1"
    assert body["category"] == "教育" and "write" not in body and "published" not in body
    assert 0 < timeout[1] <= 18


def test_comment_tools_preserve_ownership_and_bad_response_is_not_ok(wired):
    calls, reply = wired
    client = _client()
    client.rows["projects"].append({"id": OTHER_PROJ, "owner_id": "someone-else"})
    with pytest.raises(PermissionError):
        core.judge_thread(client, OTHER_PROJ, "t", "b", [{"text": "x"}], user_id=ME)
    assert calls == []
    reply["fn"] = lambda *args: type("Reply", (), {"status_code": 200, "json": lambda self: {"detail": "oops"}})()
    out = core.judge_thread(_client(), PROJ, "t", "b", [{"text": "x"}], user_id=ME)
    assert out["judge_status"] == "error" and "passed" not in out


def test_comments_timeout_and_registered_tools(wired):
    _, reply = wired
    def fail(*args):
        raise requests.Timeout("slow")
    reply["fn"] = fail
    out = core.judge_thread(_client(), PROJ, "t", "b", [{"text": "x"}], user_id=ME)
    assert out["judge_status"] == "timeout" and "passed" not in out
    # D-120: 写手侧换成 comments_for / score_comments(按链接取帖子、整组判); 单条 / 整组两个旧入口只留在 core
    assert tools.TOOLS["comments_for"][1] and tools.TOOLS["score_comments"][1]
    assert "judge_thread" not in tools.TOOLS and "comment_repair_plan_for" not in tools.TOOLS


@pytest.mark.parametrize("body", [
    {"passed": True, "plan": []},
    {"passed": True, "plan": [], "needs_review": "false"},
    {"passed": True, "plan": [], "needs_review": True},
])
def test_missing_or_contradictory_review_contract_is_not_ok(wired, body):
    _, reply = wired
    reply["fn"] = lambda *args: type("Reply", (), {"status_code": 200, "json": lambda self: body})()
    out = core.comment_repair_plan_for(_client(), PROJ, "标题", "正文", "评论", {}, user_id=ME)
    assert out["judge_status"] == "error" and "passed" not in out


def test_http_phase_has_outer_deadline_and_surviving_work_is_bounded(monkeypatch):
    release, entered = threading.Event(), threading.Event()
    pool, slots = cf.ThreadPoolExecutor(max_workers=1), threading.BoundedSemaphore(1)
    monkeypatch.setattr(core, "_COMMENT_POOL", pool)
    monkeypatch.setattr(core, "_COMMENT_SLOTS", slots)
    monkeypatch.setattr(judge_client, "configured", lambda: True)
    monkeypatch.setattr(judge_client, "tool_timeout_sec", lambda: 0.7)
    monkeypatch.setattr(core, "_judge_project_context", lambda *a: {"project": "TUGE", "category": "教育"})
    def slow(*a, **kw):
        entered.set()
        assert release.wait(3)
        return {"judge_status": "ok", "response": {"passed": True}}
    monkeypatch.setattr(judge_client, "judge_comment_tool", slow)
    try:
        started = time.monotonic()
        out = core._comment_tool_call(None, PROJ, "/judge_thread", {})
        assert entered.is_set() and time.monotonic() - started < 1.2
        assert out["judge_status"] == "timeout" and "passed" not in out
        # Timeout did not release the running I/O slot: repeated calls cannot start more workers.
        for _ in range(20):
            assert core._comment_tool_call(None, PROJ, "/judge_thread", {})["judge_status"] == "timeout"
        assert len(pool._threads) == 1
    finally:
        release.set()
        pool.shutdown(wait=True)
