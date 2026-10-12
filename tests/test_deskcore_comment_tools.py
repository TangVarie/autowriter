import pytest
import requests

from deskcore import core, tools
from tests.test_deskcore_judge_tools import wired
from tests.test_deskcore_commit_judge import ME, PROJ, OTHER_PROJ, _client


def test_single_comment_contract_and_authenticated_project(wired):
    calls, reply = wired
    reply["fn"] = lambda *args: type("Reply", (), {"status_code": 200, "json": lambda self:
        {"passed": False, "plan": [{"qid": "register"}], "needs_review": True, "ledger_rows": [{"secret": "x"}]}})()
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
    assert tools.TOOLS["judge_thread"][1] and tools.TOOLS["comment_repair_plan_for"][1]
