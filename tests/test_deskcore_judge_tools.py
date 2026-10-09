"""写手侧的判稿入口并进 deskcore(2026-10-09): judge_draft / repair_plan_for / list_banks。

以前写手要在自己机器上起 judge 仓的 judge.mcp_server(本地 checkout + 一套 JUDGE_* env + 一把发到写手机器的 key)。
现在写手只有 deskcore 这一个 HTTP MCP、一把 deskcore key; deskcore 用服务端的管理 key 转发给 judge。钉住:

  1. 请求形状: POST /judge_draft, X-Judge-Key = 服务端 key, write / return_rows 钉死 false, subject_id "draft",
     项目号 / 品类按 tv_project_map(与 commit 的影子判定同一条路);
  2. 返回: judge 抹过暗题的视图原样回, ledger_rows 永远不到写手手里; repair_plan_for 只拿修改单;
  3. 降级: 没配 / 没接 tv-map / 403 policy / 超时 / 连不上 → judge_status + detail, 一次都不抛; 没配和没接 tv-map
     一个请求都不发;
  4. 归属: 别人的项目 PermissionError(与所有写类工具同一条 COR-015);
  5. 注册: 三个都在 TOOLS 里且 needs_user=True, MCP schema 里没有 _user_id(由 test_e2e_protocol_flow 的
     set(by) == set(TOOLS) 盖住)。
"""
from __future__ import annotations

import pytest
import requests

import config
import judge_client
from deskcore import core, tools
from tests.test_deskcore_commit_judge import ME, PROJ, _client

TITLE, BODY = "标题：戒烟第三天", "上周办了张健身卡，第一段讲事。\n第二段讲感受，挺累的但开心。\n大家怎么看，是先戒烟还是边练边戒？"
SERVICE = {"subject_id": "draft", "passed": False, "invalid_reason": None,
           "profile": {"speech_act": "分享"}, "hard_fails": [["platform_health_v0.1", "efficacy_claim", "是", 0.68, "立马就压住了"]],
           "unjudged": [], "para_stats": {"n": 3}, "plan": [{"bank": "platform_health_v0.1", "qid": "efficacy_claim", "evidence": "立马就压住了"}],
           "recorded": [], "calls": 11, "usage": {"input": 1}, "banks": {"feature_questions_v0_1": {"version": "fq-v0.1", "sha256": "ba0f"}},
           "ignored_banks": [], "hard_rules": {}, "policy": {"published": False, "project": "TUGE_phase1", "dropped_banks": []},
           "rows": 31, "ledger_rows": [{"question_id": "hidden_q", "answer": "是"}], "written": None, "write_error": None}


class _Resp:
    def __init__(self, status, body=None, *, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(config, "JUDGE_URL", "https://judge.example.invalid/")
    monkeypatch.setattr(config, "JUDGE_API_KEY", "jk-admin")
    monkeypatch.setattr(config, "JUDGE_DRAFT_TOOL_TIMEOUT_SEC", 18.0)
    calls: list = []
    reply = {"fn": lambda method, url, kw: _Resp(200, dict(SERVICE, ledger_rows=list(SERVICE["ledger_rows"])))}

    def fake_post(url, *, headers=None, json=None, timeout=None, **kw):
        calls.append(("POST", url, headers, json, timeout))
        return reply["fn"]("POST", url, {"json": json})

    def fake_get(url, *, headers=None, timeout=None, **kw):
        calls.append(("GET", url, headers, None, timeout))
        return reply["fn"]("GET", url, {})
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)
    return calls, reply


def test_judge_draft_posts_without_write_and_returns_the_redacted_view(wired):
    calls, _ = wired
    c = _client()
    out = core.judge_draft(c, PROJ, TITLE, BODY, user_id=ME)
    assert out["judge_status"] == "ok" and out["project"] == "TUGE_phase1" and out["category"] == "教育"
    assert out["plan"] == SERVICE["plan"] and out["hard_fails"] == SERVICE["hard_fails"] and out["profile"] == SERVICE["profile"]
    assert "ledger_rows" not in out, "账本行(含暗题)永远不到写手手里"
    assert "影子期" in out["note"]
    (method, url, headers, body, timeout), = calls
    assert method == "POST" and url == "https://judge.example.invalid/judge_draft"
    assert headers == {"X-Judge-Key": "jk-admin"} and timeout == (3.0, 18.0)
    assert body["write"] is False and body["return_rows"] is False and body["subject_id"] == "draft"
    assert body["subject_type"] == "aw_version" and body["judge_paras"] == "on_fail" and body["run_tag"] == "mcp"
    assert body["project"] == "TUGE_phase1" and body["category"] == "教育" and body["title"] == TITLE and body["body"] == BODY
    assert "banks" not in body and "brief" not in body, "没给的不发, judge 用默认三层"


def test_optional_args_pass_through_and_tool_layer_forwards(wired, monkeypatch):
    calls, _ = wired
    c = _client()
    monkeypatch.setattr(core, "sb", lambda: c)
    out = tools.judge_draft(PROJ, TITLE, BODY, judge_paras="always", banks=["feature_questions_v0_1"],
                            brief={"want": ["x"]}, hard_rules={"fq:q1": "否"}, target={"q": 1}, validated=["q2"], _user_id=ME)
    assert out["judge_status"] == "ok"
    body = calls[-1][3]
    assert body["judge_paras"] == "always" and body["banks"] == ["feature_questions_v0_1"] and body["brief"] == {"want": ["x"]}
    assert body["hard_rules"] == {"fq:q1": "否"} and body["target"] == {"q": 1} and body["validated"] == ["q2"]
    assert body["write"] is False and body["return_rows"] is False, "写手侧的参数改不了这两条"


def test_repair_plan_for_returns_only_the_plan(wired):
    calls, reply = wired
    c = _client()
    out = core.repair_plan_for(c, PROJ, TITLE, BODY, user_id=ME)
    assert out["judge_status"] == "ok" and out["plan"] == SERVICE["plan"] and out["recorded"] == [] and out["passed"] is False
    assert out["policy"] == SERVICE["policy"] and out["calls"] == 11
    assert not ({"profile", "hard_fails", "ledger_rows", "para_stats"} & set(out))
    assert calls[-1][3]["judge_paras"] == "on_fail"
    reply["fn"] = lambda m, u, kw: _Resp(200, {"results": []})
    out = core.repair_plan_for(c, PROJ, TITLE, BODY, user_id=ME)
    assert out["judge_status"] == "error" and "plan" in out["detail"]


def test_not_configured_and_no_tv_map_send_nothing(wired, monkeypatch):
    calls, _ = wired
    monkeypatch.setattr(config, "JUDGE_URL", "")
    out = core.judge_draft(_client(), PROJ, TITLE, BODY, user_id=ME)
    assert out["judge_status"] == "not_configured" and "JUDGE_URL" in out["detail"] and calls == []
    assert core.list_banks()["judge_status"] == "not_configured" and calls == []
    monkeypatch.setattr(config, "JUDGE_URL", "https://judge.example.invalid")
    out = core.judge_draft(_client(maps=[]), PROJ, TITLE, BODY, user_id=ME)
    assert out["judge_status"] == "not_configured" and "tv-map add" in out["detail"] and calls == []


def test_bad_input_is_refused_locally(wired):
    calls, _ = wired
    c = _client()
    assert core.judge_draft(c, PROJ, TITLE, BODY, user_id=ME, judge_paras="maybe")["judge_status"] == "bad_request"
    assert core.judge_draft(c, PROJ, TITLE, "   ", user_id=ME)["judge_status"] == "bad_request"
    assert calls == []


def test_policy_timeout_and_unavailable_become_statuses_not_exceptions(wired):
    calls, reply = wired
    c = _client()
    reply["fn"] = lambda m, u, kw: _Resp(403, {"detail": "policy: 处方药项目 OKMAN 的未发布稿不出境"})
    out = core.judge_draft(c, PROJ, TITLE, BODY, user_id=ME)
    assert out["judge_status"] == "policy_blocked" and out["http_status"] == 403 and "policy:" in out["detail"]

    def _timeout(m, u, kw):
        raise requests.Timeout("read timed out")
    reply["fn"] = _timeout
    assert core.judge_draft(c, PROJ, TITLE, BODY, user_id=ME)["judge_status"] == "timeout"

    def _down(m, u, kw):
        raise requests.ConnectionError("refused")
    reply["fn"] = _down
    assert core.repair_plan_for(c, PROJ, TITLE, BODY, user_id=ME)["judge_status"] == "unavailable"
    reply["fn"] = lambda m, u, kw: _Resp(200, None, text="<html>")
    out = core.judge_draft(c, PROJ, TITLE, BODY, user_id=ME)
    assert out["judge_status"] == "error" and "JSON" in out["detail"]


def test_other_users_project_is_refused_before_any_request(wired):
    calls, _ = wired
    with pytest.raises(PermissionError):
        core.judge_draft(_client(), PROJ, TITLE, BODY, user_id="22222222-2222-2222-2222-222222222222")
    assert calls == []


def test_list_banks_gets_with_the_server_key(wired):
    calls, reply = wired
    banks = [{"name": "feature_questions_v0_1", "version": "fq-v0.1", "layer": "general", "questions": ["q1"], "hidden": 0}]
    reply["fn"] = lambda m, u, kw: _Resp(200, list(banks))
    out = core.list_banks()
    assert out["judge_status"] == "ok" and out["banks"] == banks
    (method, url, headers, body, timeout), = calls
    assert method == "GET" and url.endswith("/banks") and headers == {"X-Judge-Key": "jk-admin"} and body is None


def test_tools_are_registered_and_need_identity():
    for name in ("judge_draft", "repair_plan_for", "list_banks"):
        assert name in tools.TOOLS and tools.TOOLS[name][1] is True, name
    assert "影子期" in (tools.judge_draft.__doc__ or "") and "不要循环" in (tools.judge_draft.__doc__ or "")


def test_build_tool_request_pins_write_and_rows():
    p = judge_client.build_tool_request(title="t", body="b", project="TUGE_phase1", category=None)
    assert p["write"] is False and p["return_rows"] is False and p["subject_id"] == "draft" and "category" not in p
