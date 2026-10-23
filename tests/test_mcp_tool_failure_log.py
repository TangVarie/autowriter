"""W-29: MCP 工具失败要在服务端留一行痕, 而调用方看到的 isError 一模一样。

MCP 的工具报错是 isError + HTTP 200, FastMCP 自己把异常收成错误结果, deskcore 原来一行日志都没有 —— 10-20~10-22
查「score_drafts 到底调了没有、是不是半路炸了」时, 能看的只有 edge 日志里一串 200。

这里钉的是【被禁止的形态】:
  · 工具炸了服务端没有任何记录;
  · 记录里出现稿子的标题 / 正文(参数和异常消息都可能带着它们)、完整的 user_id;
  · 留痕改变了 MCP 的行为(异常被吞、换了一个、或者 isError 的文本变了)。
"""

from __future__ import annotations

import logging

import anyio
import pytest

pytest.importorskip("mcp")
pytest.importorskip("fastapi")

from mcp import types                              # noqa: E402

import deskcore.app as A                           # noqa: E402
from deskcore import core, tools                   # noqa: E402
from deskcore.identity import Caller               # noqa: E402

ME = "11111111-1111-1111-1111-111111111111"
PID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
DRAFTS = [{"title": "SECRET-T 秘密标题", "body": "SECRET-B 秘密正文"}]


def _call(monkeypatch, fn, args, *, via_server=False):
    """装一个只含 fn 的工具表, 走真的 _register_mcp / _wrap, 以 ME 的身份调一次。"""
    monkeypatch.setattr(tools, "TOOLS", {"t": (fn, True)})
    mcp = A._register_mcp()
    out: dict = {}

    async def run():
        token = A._caller.set(Caller(ME))
        try:
            if via_server:
                handler = mcp._mcp_server.request_handlers[types.CallToolRequest]
                req = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(
                    name="t", arguments=args))
                out["result"] = (await handler(req)).root
            else:
                try:
                    out["result"] = await mcp._tool_manager.call_tool("t", args)
                except Exception as exc:        # noqa: BLE001
                    out["error"] = exc
        finally:
            A._caller.reset(token)
    anyio.run(run)
    return out


def _mine(caplog):
    return [r for r in caplog.records if r.name == "deskcore" and "mcp tool" in r.getMessage()]


def test_failure_is_logged_without_text_and_reraised_unchanged(monkeypatch, caplog):
    raised = []

    def tool(project_id: str, drafts: list[dict], _user_id=None):
        exc = RuntimeError("撞车: " + drafts[0]["title"] + " / " + drafts[0]["body"])
        raised.append(exc)
        raise exc

    with caplog.at_level(logging.INFO):
        out = _call(monkeypatch, tool, {"project_id": PID, "drafts": DRAFTS})
    # 行为不变: FastMCP 照旧把它包成 ToolError, 原异常就是 cause, 消息原样
    err = out["error"]
    assert err.__cause__ is raised[0]
    assert "SECRET-T" in str(err), "调用方看到的错误文本不该被留痕改掉"
    recs = _mine(caplog)
    assert len(recs) == 1 and recs[0].levelno == logging.ERROR
    msg = recs[0].getMessage()
    for must in ("mcp tool t failed", "RuntimeError", f"project={PID}", f"user={ME[:8]}"):
        assert must in msg, (must, msg)
    assert "File " in msg, "要带堆栈帧, 才知道炸在哪"
    # 日志里不许有参数原文 / 异常消息 / 完整 user_id
    assert "SECRET" not in caplog.text and "秘密" not in caplog.text
    assert ME not in caplog.text
    assert recs[0].exc_info is None, "exc_info 会把异常消息带进日志"


def test_server_still_answers_is_error_with_the_same_text(monkeypatch, caplog):
    def tool(project_id: str, drafts: list[dict], _user_id=None):
        raise RuntimeError("查重挂了: " + drafts[0]["title"])

    with caplog.at_level(logging.INFO):
        res = _call(monkeypatch, tool, {"project_id": PID, "drafts": DRAFTS}, via_server=True)["result"]
    assert res.isError is True
    assert res.content[0].text == "Error executing tool t: 查重挂了: " + DRAFTS[0]["title"]
    assert len(_mine(caplog)) == 1 and "SECRET" not in caplog.text


PG_TIMEOUT = "canceling statement due to statement timeout; detail: "


def test_driver_error_code_is_logged_but_not_its_message(monkeypatch, caplog):
    class APIError(Exception):
        code = "57014"

    def tool(project_id: str, drafts: list[dict], _user_id=None):
        raise APIError(PG_TIMEOUT + drafts[0]["body"])

    with caplog.at_level(logging.INFO):
        _call(monkeypatch, tool, {"project_id": PID, "drafts": DRAFTS})
    msg = _mine(caplog)[0].getMessage()
    assert "APIError code=57014" in msg
    # 堆栈帧里是源码行(上面那行 raise), 不是运行时的值: 消息本身和参数都不该出现
    assert "SECRET" not in caplog.text and "statement timeout" not in caplog.text


@pytest.mark.parametrize("exc", [
    PermissionError("project x 不属于当前调用者"),
    core.ProjectNotFound("project not found"),
    tools.InvalidInput("一次最多 50 条"),
])
def test_expected_refusals_are_warnings_without_stack(monkeypatch, caplog, exc):
    def tool(project_id: str, drafts: list[dict], _user_id=None):
        raise exc

    with caplog.at_level(logging.INFO):
        out = _call(monkeypatch, tool, {"project_id": PID, "drafts": DRAFTS})
    assert out["error"].__cause__ is exc
    recs = _mine(caplog)
    assert len(recs) == 1 and recs[0].levelno == logging.WARNING
    assert "refused" in recs[0].getMessage() and type(exc).__name__ in recs[0].getMessage()
    assert "File " not in recs[0].getMessage()


def test_error_shaped_result_is_noted_at_info_and_returned_as_is(monkeypatch, caplog):
    payload = {"error": "RuntimeError: SECRET-T 秘密标题", "hint": "写稿可以继续"}

    def tool(project_id: str, _user_id=None):
        return dict(payload)

    with caplog.at_level(logging.INFO):
        out = _call(monkeypatch, tool, {"project_id": PID})
    assert out["result"] == payload
    recs = _mine(caplog)
    assert len(recs) == 1 and recs[0].levelno == logging.INFO
    assert "error-shaped" in recs[0].getMessage() and f"project={PID}" in recs[0].getMessage()
    assert "SECRET" not in caplog.text


def test_status_error_result_is_noted_too(monkeypatch, caplog):
    def tool(project_id: str, _user_id=None):
        return {"project_id": project_id, "status": "error", "reason": "rule_compiles_unreadable"}

    with caplog.at_level(logging.INFO):
        _call(monkeypatch, tool, {"project_id": PID})
    assert [r.levelno for r in _mine(caplog)] == [logging.INFO]


def test_successful_call_logs_nothing(monkeypatch, caplog):
    def tool(project_id: str, _user_id=None):
        return {"ok": True, "status": "done", "error": None}

    with caplog.at_level(logging.INFO):
        out = _call(monkeypatch, tool, {"project_id": PID})
    assert out["result"] == {"ok": True, "status": "done", "error": None}
    assert _mine(caplog) == []


def test_project_id_is_trimmed_and_cannot_split_the_log_line(monkeypatch, caplog):
    def tool(project_id: str, _user_id=None):
        raise RuntimeError("x")

    with caplog.at_level(logging.INFO):
        _call(monkeypatch, tool, {"project_id": "p\n" + "z" * 200})
    first_line = _mine(caplog)[0].getMessage().splitlines()[0]
    assert "project=p" + "z" * 39 + " user=" in first_line
