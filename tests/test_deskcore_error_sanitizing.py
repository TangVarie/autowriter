"""工具真故障时, 异常原文不回给调用方、也不进日志(外部评审 11-04 X1a, 2026-11-04)。

PostgREST 的 APIError 消息里带 "Failing row contains (…)" / "Key (…)=(…)" —— 就是那一行稿子的原文。原来 REST 的 500 把
``类型: 消息`` 截 300 字回给调用方、``logger.exception`` 原样记进日志; MCP 那一路日志只记类型, 可 isError 的文本是完整的
str(exc)。logger_utils.SecretMaskingFormatter 定义了却没人装。这里钉住: 500 / MCP 真故障只回 ``<类型> (ref <8 位>)``, 同一个
ref 在服务端那行日志里, 日志不带消息; 正常拒绝那几类(400/403/404/409)的消息照旧原样回; 根 logger 的 handler 装上了打码的
formatter。
"""

from __future__ import annotations

import io
import logging
import re

import anyio
import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
pytest.importorskip("mcp")

from mcp import types                               # noqa: E402

import deskcore.app as A                            # noqa: E402
from deskcore import tools                          # noqa: E402
from deskcore.identity import Caller                # noqa: E402
from logger_utils import SecretMaskingFormatter     # noqa: E402

KEYS = '{"k-test": {"user_id": "22222222-2222-2222-2222-222222222222", "name": "t"}}'
AUTH = {"Authorization": "Bearer k-test"}
MARKER = "ROWMARKER-稿子原文"
FAKE_KEY = "sk-ant-api03-" + "Z" * 48
REF = re.compile(r"APIError \(ref ([0-9a-f]{8})\)")


class APIError(Exception):
    """形状同 postgrest 的 APIError: 带 code, 消息里有行内容。"""
    code = "23502"


def _boom(*a, **k):
    raise APIError(f'null value in column "title" violates not-null constraint; '
                   f"Failing row contains ({MARKER}, {FAKE_KEY})")


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("DESKCORE_KEYS", KEYS)
    return fastapi_testclient.TestClient(A.app, raise_server_exceptions=False)


def test_rest_500_returns_only_the_type_and_a_ref(client, monkeypatch, caplog):
    monkeypatch.setattr(A, "_call_tool", _boom)
    with caplog.at_level(logging.INFO):
        r = client.post("/tool/check_drafts", json={"project_id": "p-1"}, headers=AUTH)
    assert r.status_code == 500
    m = REF.fullmatch(r.json()["detail"])
    assert m, r.json()
    assert MARKER not in r.text and FAKE_KEY not in r.text
    recs = [x for x in caplog.records if x.name == "deskcore" and "rest tool check_drafts failed" in x.getMessage()]
    assert len(recs) == 1 and recs[0].levelno == logging.ERROR
    msg = recs[0].getMessage()
    assert f"ref={m.group(1)}" in msg and "APIError code=23502" in msg and "File " in msg
    assert MARKER not in caplog.text and FAKE_KEY not in caplog.text, "日志里不带异常消息"
    assert recs[0].exc_info is None


@pytest.mark.parametrize("exc, status", [
    (tools.InvalidInput("一次最多 50 条"), 400),
    (PermissionError("project x 不属于当前调用者"), 403),
])
def test_expected_refusals_keep_their_message(client, monkeypatch, exc, status):
    def _raise(*a, **k):
        raise exc
    monkeypatch.setattr(A, "_call_tool", _raise)
    r = client.post("/tool/check_drafts", json={}, headers=AUTH)
    assert r.status_code == status and r.json()["detail"] == str(exc)


def _mcp_call(monkeypatch, fn):
    monkeypatch.setattr(tools, "TOOLS", {"t": (fn, True)})
    mcp = A._register_mcp()
    out = {}

    async def run():
        token = A._caller.set(Caller("11111111-1111-1111-1111-111111111111"))
        try:
            handler = mcp._mcp_server.request_handlers[types.CallToolRequest]
            req = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(
                name="t", arguments={"project_id": "p-1"}))
            out["result"] = (await handler(req)).root
        finally:
            A._caller.reset(token)
    anyio.run(run)
    return out["result"]


def test_mcp_unexpected_error_is_sanitized(monkeypatch, caplog):
    def tool(project_id: str, _user_id=None):
        _boom()

    with caplog.at_level(logging.INFO):
        res = _mcp_call(monkeypatch, tool)
    assert res.isError is True
    text = res.content[0].text
    m = re.fullmatch(r"Error executing tool t: APIError \(ref ([0-9a-f]{8})\)", text)
    assert m, text
    assert MARKER not in text and FAKE_KEY not in text
    assert any(f"ref={m.group(1)}" in x.getMessage() for x in caplog.records if x.name == "deskcore")
    assert MARKER not in caplog.text and FAKE_KEY not in caplog.text


def test_mcp_expected_refusal_text_is_unchanged(monkeypatch):
    def tool(project_id: str, _user_id=None):
        raise tools.InvalidInput("一次最多 50 条")

    res = _mcp_call(monkeypatch, tool)
    assert res.isError is True and res.content[0].text == "Error executing tool t: 一次最多 50 条"


def test_root_handlers_get_the_masking_formatter():
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    root = logging.getLogger()
    root.addHandler(h)
    try:
        assert A._install_log_masking() >= 1
        assert isinstance(h.formatter, SecretMaskingFormatter)
        assert h.formatter._fmt == "%(levelname)s:%(name)s:%(message)s", "格式串照旧"
        assert A._install_log_masking() == 0, "重复调安全: 已经装上的不再换"
        logging.getLogger("deskcore.test").warning("leaked %s", FAKE_KEY)
        assert FAKE_KEY not in buf.getvalue() and "WARNING:deskcore.test:leaked" in buf.getvalue()
    finally:
        root.removeHandler(h)
