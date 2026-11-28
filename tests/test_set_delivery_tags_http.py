"""set_delivery_tags 在 HTTP 那一层(2026-11-28, docs/deskcore.md §3.41): REST、MCP、/tools、/health。

钉住: 留痕里的 by 是服务端那把钥匙的备注名(app 注入), 调用方自己传的 ``_caller_name`` / ``_user_id`` 不算; 报的那几句是 400、
别人的项目是 403; MCP 的入参只有 project_id、content(整数)、fixed(一组字), 后两个可选。
MCP 那条路上 SDK 会悄悄改掉的值(复审 11-28): content / fixed 写成 NaN / Infinity / 1e400 原来变成 null —— 在这个工具里 null 是
「这一样不动」, 同一次给的 fixed 照改、个数悄悄没动; content 写成 true / false 原来被 SDK 读成 1 / 0。现在都是拒, 一个字不写。
全部是合成项目、合成话题和测试里现编的钥匙。纯函数那一层在 tests/test_set_delivery_tags.py。
"""
from __future__ import annotations

import json
import math

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

import deskcore.app as A                                  # noqa: E402
from deskcore import core, tools                          # noqa: E402
from tests.test_set_delivery_tags import ME, OTHER, PROJ, _client, _params, _row, _tags, _writes  # noqa: E402

FAKE_KEY = "k-tagstest-0123456789abcdefFAKE"              # 测试里现编的, 不是任何真钥匙
KEYS = json.dumps({FAKE_KEY: {"user_id": ME, "name": "示例写手甲"}}, ensure_ascii=False)
H = {"X-Deskcore-Key": FAKE_KEY}
MCP_H = {**H, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture()
def http(monkeypatch):
    c = _client()
    monkeypatch.setenv("DESKCORE_KEYS", KEYS)
    monkeypatch.setattr(core, "sb", lambda: c)
    return fastapi_testclient.TestClient(A.app, raise_server_exceptions=False), c


def test_rest_call_changes_the_tags_and_signs_the_log_with_the_key_name_not_the_key(http):
    client, c = http
    r = client.post("/tool/set_delivery_tags", headers=H, json={
        "project_id": PROJ, "content": 9, "fixed": [],
        "_caller_name": "冒名", "_user_id": OTHER})
    assert r.status_code == 200, r.text
    out = r.json()["result"]
    assert out["changed"] is True and out["after"] == {"content": 9, "fixed": []}
    assert out["delivery_requirements"]["tags"] == 9
    (entry,) = _params(c)[core.DELIVERY_TAGS_LOG_KEY]
    assert entry["by"] == "示例写手甲", "署名是服务端那把钥匙的备注名, 调用方传的不算"
    assert FAKE_KEY not in _row(c)["default_params"] and FAKE_KEY not in r.text


@pytest.mark.parametrize("body, part", [
    ({"content": 11}, "要是 0~10 的整数, 现在是 11"),
    ({"content": True}, "要是 0~10 的整数, 现在是 True"),
    ({"content": "九"}, "要是 0~10 的整数"),
    ({"fixed": "示例话题"}, "固定话题(fixed)要是一组话题"),
    ({"fixed": ["两个 词"]}, "不是一个话题"),
    ({}, "content 和 fixed 至少给一个"),
    ({"content": None, "fixed": None}, "content 和 fixed 至少给一个"),
])
def test_rest_refusals_are_400_with_the_sentence(http, body, part):
    client, c = http
    r = client.post("/tool/set_delivery_tags", headers=H, json=dict(body, project_id=PROJ))
    assert r.status_code == 400 and part in r.json()["detail"], r.text
    assert _writes(c) == []


def test_rest_no_template_is_400_and_someone_elses_project_is_403(http):
    client, c = http
    _row(c)["default_params"] = "{}"
    r = client.post("/tool/set_delivery_tags", headers=H, json={"project_id": PROJ, "content": 9})
    assert r.status_code == 400 and r.json()["detail"] == core.DTAGS_NO_TEMPLATE
    _row(c)["owner_id"] = OTHER
    r = client.post("/tool/set_delivery_tags", headers=H, json={"project_id": PROJ, "content": 9})
    assert r.status_code == 403 and _writes(c) == []


@pytest.mark.parametrize("raw, shown", [("1e400", "inf"), ("NaN", "nan"), ("-Infinity", "-inf")])
def test_rest_odd_numbers_are_400_with_the_sentence_not_500(http, raw, shown):
    client, c = http
    body = ('{"project_id": "%s", "content": %s, "fixed": []}' % (PROJ, raw)).encode("utf-8")
    r = client.post("/tool/set_delivery_tags", headers={**H, "Content-Type": "application/json"}, content=body)
    assert r.status_code == 400 and f"现在是 {shown}。" in r.json()["detail"], r.text
    assert _writes(c) == []


def test_listed_in_tools_health_and_mcp_with_only_the_three_parameters(http, monkeypatch):
    anyio = pytest.importorskip("anyio")
    client, _c = http

    class _Dead:
        def table(self, *_a, **_k):
            raise RuntimeError("no db in this test")
    monkeypatch.setattr(A, "_health_probe_client", lambda: _Dead())
    assert "set_delivery_tags" in client.get("/health").json()["tools"]
    listed = {t["name"]: t for t in client.get("/tools", headers=H).json()["tools"]}
    assert listed["set_delivery_tags"]["needs_identity"] is True
    assert listed["set_delivery_tags"]["description"] == tools.set_delivery_tags.__doc__.strip()
    assert A._mcp is not None
    by = {t.name: t for t in anyio.run(A._mcp.list_tools)}
    schema = by["set_delivery_tags"].inputSchema
    assert set(schema["properties"]) == {"project_id", "content", "fixed"}, "_user_id / _caller_name 不露出来"
    assert schema["required"] == ["project_id"], "content / fixed 都可选"
    content, fixed = schema["properties"]["content"], schema["properties"]["fixed"]
    assert {"type": "integer"} in content["anyOf"] and {"type": "null"} in content["anyOf"] and content["default"] is None
    assert {"type": "array", "items": {"type": "string"}} in fixed["anyOf"] and fixed["default"] is None
    assert by["set_delivery_tags"].description.startswith("改这个项目交付稿的话题")


def test_mcp_call_injects_the_name_and_hides_internal_params(http):
    anyio = pytest.importorskip("anyio")
    _client_, c = http
    token = A._caller.set(A.identity.Caller(ME, "示例写手乙", True))
    try:
        anyio.run(lambda: A._mcp.call_tool("set_delivery_tags", {"project_id": PROJ, "fixed": ["新话题"],
                                                                 "_caller_name": "冒名", "_user_id": OTHER}))
    finally:
        A._caller.reset(token)
    (entry,) = _params(c)[core.DELIVERY_TAGS_LOG_KEY]
    assert entry["by"] == "示例写手乙" and entry["after"]["fixed"] == ["新话题"]


# ── MCP 那条路上 SDK 会悄悄改掉的值 ────────────────────────────────────────────────────────────

@pytest.fixture()
def live(http):
    """真走一遍 SDK: 另起一份 MCP 子应用, 外面包的是线上同一个 ``_McpFiniteNumbers``(同 tests/test_set_comment_mix_http.py)。"""
    _client_, c = http
    inner = A._McpFiniteNumbers(A._register_mcp().streamable_http_app())

    async def as_caller(scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        token = A._caller.set(A.identity.Caller(ME, "示例写手甲", True))
        try:
            await inner(scope, receive, send)
        finally:
            A._caller.reset(token)
    with fastapi_testclient.TestClient(as_caller, raise_server_exceptions=False) as client:
        yield client, c


def _body(args: str, tool: str = "set_delivery_tags") -> bytes:
    return ('{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"%s","arguments":{"project_id":"%s",%s}}}'
            % (tool, PROJ, args)).encode("utf-8")


def _call(client, args: str, tool: str = "set_delivery_tags") -> tuple[bool, str]:
    r = client.post("/", headers=MCP_H, content=_body(args, tool))
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    return bool(result.get("isError")), result["content"][0]["text"]


@pytest.mark.parametrize("args, where", [
    ('"content": NaN, "fixed": []', "content"),
    ('"content": 1e400, "fixed": ["新话题"]', "content"),
    ('"content": -Infinity, "fixed": []', "content"),
    ('"content": true, "fixed": []', "content"),
    ('"content": false', "content"),
    ('"content": true', "content"),
    ('"content": 9, "fixed": NaN', "fixed"),
    ('"fixed": Infinity', "fixed"),
    ('"content": 9, "fixed": ["新话题", NaN]', "fixed"),
])
def test_mcp_a_value_the_sdk_would_quietly_change_is_refused_and_nothing_is_written(live, args, where):
    """原来: NaN / 1e400 的 content 变成 null(「个数不动」), 同一次的 fixed 照改, 回 changed=true; true 变成 1 个话题。"""
    client, c = live
    before = _row(c)["default_params"]
    is_error, text = _call(client, args)
    assert is_error is True and where in text, text
    assert "(ref " not in text, "是参数不对, 不是服务坏了"
    assert _writes(c) == [] and _row(c)["default_params"] == before


def test_mcp_a_real_null_still_means_leave_it_alone(live):
    client, c = live
    is_error, text = _call(client, '"content": null, "fixed": ["新话题"]')
    assert is_error is False and json.loads(text)["changed"] is True
    assert _tags(c)["content"] == 4 and _tags(c)["fixed"] == ["新话题"]


@pytest.mark.parametrize("args, content", [('"content": 9', 9), ('"content": 9.0', 9), ('"content": "9"', 9)])
def test_mcp_ordinary_numbers_go_through(live, args, content):
    client, c = live
    is_error, text = _call(client, args)
    assert is_error is False, text
    assert _tags(c)["content"] == content


def test_mcp_a_stringified_list_for_fixed_is_read_as_the_list(live):
    """SDK 自己会把一串 JSON 写的列表解开(模型常这么传); REST 那条路不解, 回「要是一组话题」。"""
    client, c = live
    is_error, text = _call(client, '"fixed": "[\\"新话题甲\\", \\"#新话题乙\\"]"')
    assert is_error is False, text
    assert _tags(c)["fixed"] == ["新话题甲", "新话题乙"]


def test_only_set_delivery_tags_top_level_values_are_rewritten():
    same = [
        _body('"content": 9, "fixed": []'),                                       # 正常的调用
        _body('"content": null, "fixed": null'),
        _body('"content": 9, "fixed": ["新话题", NaN]'),                           # 列表里面的不动: SDK 换成 null 照样拒
        _body('"content": NaN, "x": 1', tool="record_rule"),                      # 别的工具也有 content 参数
        _body('"content": true', tool="record_rule"),
        _body('"note": NaN, "content": 9'),                                       # 这个工具没有的参数不管(SDK 会拒它)
        b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"name":"set_delivery_tags","content":NaN}}',
        b'[{"method":"tools/call","params":{"name":"set_delivery_tags","arguments":{"content":NaN}}}]',
        b'{"params":{"name":"set_delivery_tags"}, not json', b"set_delivery_tags",
    ]
    for body in same:
        assert A._spell_out_non_finite(body) is body, body[:90]
    fixed = json.loads(A._spell_out_non_finite(_body('"content": 1e400, "fixed": ["甲", NaN, 2.5], "x": -Infinity')))
    args = fixed["params"]["arguments"]
    assert args["content"] == "1e400" and args["project_id"] == PROJ and fixed["id"] == 1
    assert args["fixed"][:1] == ["甲"] and math.isnan(args["fixed"][1]) and args["fixed"][2] == 2.5, \
        "列表里面的照原样写回 NaN(SDK 换成 null 之后拒), 不变成一个叫 NaN 的话题"
    assert args["x"] == float("-inf")
    assert json.loads(A._spell_out_non_finite(_body('"content": false')))["params"]["arguments"]["content"] == "false"
    assert json.loads(A._spell_out_non_finite(_body('"fixed": NaN')))["params"]["arguments"]["fixed"] == "NaN"
    mix = json.loads(A._spell_out_non_finite(
        _body('"mix": {"brand_max": NaN}, "content": NaN', tool="set_comment_mix")))
    assert mix["params"]["arguments"] == {"project_id": PROJ, "mix": {"brand_max": "NaN"}, "content": "NaN"}, \
        "set_comment_mix 照旧: 整个请求体里的都换"


@pytest.mark.parametrize("name", ["\\u0073et_delivery_tags", "set_delivery_tag\\u0073", "set\\u005fdelivery\\u005Ftags"])
def test_mcp_a_tool_name_written_with_json_escapes_is_still_guarded(live, name):
    client, c = live
    before = _row(c)["default_params"]
    body = _body('"content": 1e400, "fixed": []', tool=name)
    assert b"set_delivery_tags" not in body
    assert json.loads(A._spell_out_non_finite(body))["params"]["arguments"]["content"] == "1e400"
    r = client.post("/", headers=MCP_H, content=body)
    assert r.status_code == 200 and r.json()["result"].get("isError") is True, r.text
    assert _writes(c) == [] and _row(c)["default_params"] == before
