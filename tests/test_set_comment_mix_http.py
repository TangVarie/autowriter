"""set_comment_mix 在 HTTP 那一层(2026-11-11, docs/deskcore.md §3.23): REST、MCP、/tools、/health。

钉住: 留痕里的 by 是服务端那把钥匙的备注名(app 注入), 调用方自己传的 ``_caller_name`` / ``_user_id`` 不算, 钥匙本身不进库、
不进回包; 报的那几句是 400、别人的项目是 403; MCP 的入参只有 project_id 和 mix。
全部是合成项目、合成品牌、假列名和测试里现编的钥匙。纯函数那一层在 tests/test_set_comment_mix.py。
"""
from __future__ import annotations

import json

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

import deskcore.app as A                                  # noqa: E402
from deskcore import core, tools                          # noqa: E402
from tests.test_comment_set_score import _client          # noqa: E402
from tests.test_deskcore_score_and_comments import ME     # noqa: E402
from tests.test_set_comment_mix import _params, _pid, _writes  # noqa: E402

FAKE_KEY = "k-mixtest-0123456789abcdefFAKE"               # 测试里现编的, 不是任何真钥匙
KEYS = json.dumps({FAKE_KEY: {"user_id": ME, "name": "示例写手甲"}}, ensure_ascii=False)
H = {"X-Deskcore-Key": FAKE_KEY}


@pytest.fixture()
def http(monkeypatch):
    c = _client()
    monkeypatch.setenv("DESKCORE_KEYS", KEYS)
    monkeypatch.setattr(core, "sb", lambda: c)
    return fastapi_testclient.TestClient(A.app, raise_server_exceptions=False), c


def test_rest_call_changes_the_mix_and_signs_the_log_with_the_key_name_not_the_key(http):
    client, c = http
    r = client.post("/tool/set_comment_mix", headers=H, json={
        "project_id": _pid(c), "mix": {"brand_max": 60},
        "_caller_name": "冒名", "_user_id": "22222222-2222-2222-2222-222222222222"})
    assert r.status_code == 200, r.text
    out = r.json()["result"]
    assert out["changed"] is True and out["summary"] == "「示例项目」一组评论的配比改了: 提品牌的上限 50% → 60%。"
    (entry,) = _params(c)[core.COMMENT_SET_LOG_KEY]
    assert entry["by"] == "示例写手甲", "署名是服务端那把钥匙的备注名, 调用方传的不算"
    assert FAKE_KEY not in c.rows["projects"][0]["default_params"] and FAKE_KEY not in r.text


def test_a_caller_supplied_signature_is_dropped_for_every_tool(http):
    """``_caller_name`` 和 ``_user_id`` 一样是服务端注入的: 调用方自己带的一律先摘掉, 不要它的工具也不会因为多了个参数炸成 500。"""
    client, _c = http
    r = client.post("/tool/get_protocol", headers=H, json={"_caller_name": "冒名", "_user_id": "冒名"})
    assert r.status_code == 200, r.text[:200]
    assert A._INJECTED_PARAMS == ("_user_id", "_caller_name")


@pytest.mark.parametrize("body, status, part", [
    ({"mix": {"column": "另一列"}}, 400, "评论贴进飞书哪一列在这里改不了"),
    ({"mix": {"brand_max": 0.71}}, 400, "超过了 100%"),
    ({"mix": {}}, 400, "mix 里没有要改的"),
])
def test_rest_refusals_are_400_with_the_sentence(http, body, status, part):
    client, c = http
    r = client.post("/tool/set_comment_mix", headers=H, json=dict(body, project_id=_pid(c)))
    assert r.status_code == status and part in r.json()["detail"], r.text
    assert _writes(c) == []


def test_rest_someone_elses_project_is_403(http, monkeypatch):
    client, c = http
    c.rows["projects"][0]["owner_id"] = "22222222-2222-2222-2222-222222222222"
    r = client.post("/tool/set_comment_mix", headers=H, json={"project_id": _pid(c), "mix": {"brand_max": 0.6}})
    assert r.status_code == 403 and _writes(c) == []


def test_listed_in_tools_health_and_mcp(http, monkeypatch):
    anyio = pytest.importorskip("anyio")
    client, _c = http

    class _Dead:
        def table(self, *_a, **_k):
            raise RuntimeError("no db in this test")
    monkeypatch.setattr(A, "_health_probe_client", lambda: _Dead())
    assert "set_comment_mix" in client.get("/health").json()["tools"]
    listed = {t["name"]: t for t in client.get("/tools", headers=H).json()["tools"]}
    assert listed["set_comment_mix"]["needs_identity"] is True
    assert listed["set_comment_mix"]["description"] == tools.set_comment_mix.__doc__.strip()
    assert A._mcp is not None
    by = {t.name: t for t in anyio.run(A._mcp.list_tools)}
    schema = by["set_comment_mix"].inputSchema
    assert set(schema["properties"]) == {"project_id", "mix"} and sorted(schema["required"]) == ["mix", "project_id"]
    assert by["set_comment_mix"].description.startswith("改这个项目一组评论的配比")


def test_mcp_call_injects_the_name_and_hides_internal_params(http):
    anyio = pytest.importorskip("anyio")
    client, c = http
    token = A._caller.set(A.identity.Caller(ME, "示例写手乙", True))
    try:
        anyio.run(lambda: A._mcp.call_tool("set_comment_mix", {"project_id": _pid(c), "mix": {"short_min": None},
                                                               "_caller_name": "冒名"}))
    finally:
        A._caller.reset(token)
    (entry,) = _params(c)[core.COMMENT_SET_LOG_KEY]
    assert entry["by"] == "示例写手乙" and "short_min" not in entry["after"]


# ── 复审 11-11: 不是有限的数、怪数字 —— 两条路都是那一句, 都不写 ─────────────────────────────────

MCP_H = {**H, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture()
def live(http):
    """真走一遍 SDK: 另起一份 MCP 子应用(SDK 的会话管理每份只能起一次, 全局那份留给 tests/test_deskcore_cors.py),
    外面包的是线上同一个 ``_McpFiniteNumbers``; 身份照 auth_middleware 的做法放进 ``_caller``。"""
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


def test_the_mounted_mcp_app_is_wrapped_by_the_guard():
    (mount,) = [r for r in A.app.routes if getattr(r, "path", None) == "/mcp"]
    assert isinstance(mount.app, A._McpFiniteNumbers)


def _mcp_body(c, raw_mix: str, tool: str = "set_comment_mix") -> bytes:
    return ('{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"%s","arguments":'
            '{"project_id":"%s","mix":%s}}}' % (tool, _pid(c), raw_mix)).encode("utf-8")


def _mcp_result(client, c, raw_mix: str) -> tuple[bool, str]:
    r = client.post("/", headers=MCP_H, content=_mcp_body(c, raw_mix))
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    return bool(result.get("isError")), result["content"][0]["text"]


@pytest.mark.parametrize("raw_mix, sentence", [
    ('{"brand_max": 1e400}', "提品牌的上限(brand_max)要是一个数: 小数(0.5)或百分数(50、\"50%\")都行, 现在是 '1e400'。"),
    ('{"neutral_min": NaN}', "中性评论的下限(neutral_min)要是一个数: 小数(0.5)或百分数(50、\"50%\")都行, 现在是 'NaN'。"),
    ('{"short_min": -1e999}', "一句话短评的下限(short_min)要是一个数: 小数(0.5)或百分数(50、\"50%\")都行, 现在是 '-1e999'。"),
    ('{"short_chars": Infinity}', "一句话短评的字数(short_chars)要是 6~40 的整数, 现在是 'Infinity'。"),
    ('{"brand_max": 0.6, "short_chars": -Infinity}', "一句话短评的字数(short_chars)要是 6~40 的整数, 现在是 '-Infinity'。"),
    ('{"short_chars": "²"}', "一句话短评的字数(short_chars)要是 6~40 的整数, 现在是 '²'。"),
])
def test_mcp_a_number_that_is_not_finite_is_refused_not_read_as_remove_this_limit(live, raw_mix, sentence):
    """SDK 会把 NaN / Infinity / 1e400 悄悄换成 null, 而 null 在这个工具里是「去掉这一项」: 原来这几个请求各关掉一项检查、
    回 changed=true。现在和 REST 一样是那一句, 什么都不写。"""
    client, c = live
    before = c.rows["projects"][0]["default_params"]
    is_error, text = _mcp_result(client, c, raw_mix)
    assert is_error is True and sentence in text, text
    assert "(ref " not in text and "ValueError" not in text and "OverflowError" not in text
    assert _writes(c) == [] and c.rows["projects"][0]["default_params"] == before


def test_mcp_a_real_null_still_removes_the_limit(live):
    client, c = live
    is_error, text = _mcp_result(client, c, '{"short_min": null, "brand_max": 0.6}')
    assert is_error is False and json.loads(text)["changed"] is True
    assert _params(c)["comment_set"] == {"brand_max": 0.6, "neutral_min": 0.3, "short_chars": 15, "column": "测试列"}


@pytest.mark.parametrize("raw, status, sentence", [
    ('{"short_chars": 1e400}', 400, "一句话短评的字数(short_chars)要是 6~40 的整数, 现在是 inf。"),
    ('{"short_chars": NaN}', 400, "一句话短评的字数(short_chars)要是 6~40 的整数, 现在是 nan。"),
    ('{"short_chars": "²"}', 400, "一句话短评的字数(short_chars)要是 6~40 的整数, 现在是 '²'。"),
    ('{"brand_max": 1e400}', 400, "提品牌的上限(brand_max)要是一个数"),
    ('{"neutral_min": NaN}', 400, "中性评论的下限(neutral_min)要是一个数"),
])
def test_rest_odd_numbers_are_400_with_the_sentence_not_500(http, raw, status, sentence):
    client, c = http
    body = ('{"project_id": "%s", "mix": %s}' % (_pid(c), raw)).encode("utf-8")
    r = client.post("/tool/set_comment_mix", headers={**H, "Content-Type": "application/json"}, content=body)
    assert r.status_code == status and sentence in r.json()["detail"], r.text
    assert _writes(c) == []


def test_only_a_set_comment_mix_call_with_a_non_finite_number_is_rewritten(http):
    """别的请求一个字节不动(回来的就是传进去的那个对象)。"""
    _client_, c = http
    same = [
        _mcp_body(c, '{"brand_max": 0.6}'),                                   # 正常的调用
        _mcp_body(c, '{"brand_max": null}'),
        _mcp_body(c, '{"brand_max": 1e400}', tool="score_comments"),          # 别的工具
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"record_rule","arguments":'
        b'{"content":"set_comment_mix","x":NaN}}}',                           # 只是文字里提到了它
        b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"name":"set_comment_mix","x":NaN}}',
        b'[{"method":"tools/call","params":{"name":"set_comment_mix","arguments":{"mix":{"brand_max":NaN}}}}]',
        b'{"params":{"name":"set_comment_mix"}, not json', b"", b"set_comment_mix",
    ]
    for body in same:
        assert A._spell_out_non_finite(body) is body, body[:80]
    fixed = json.loads(A._spell_out_non_finite(_mcp_body(c, '{"brand_max": 1e400, "neutral_min": -Infinity, "short_chars": 12}')))
    assert fixed["params"]["arguments"]["mix"] == {"brand_max": "1e400", "neutral_min": "-Infinity", "short_chars": 12}
    assert fixed["id"] == 1 and fixed["params"]["arguments"]["project_id"] == _pid(c)


@pytest.mark.parametrize("name", ["\\u0073et_comment_mix", "set_comment_mi\\u0078", "set\\u005fcomment\\u005Fmix"])
def test_mcp_a_tool_name_written_with_json_escapes_is_still_guarded(live, name):
    """Codex 11-11: 工具名写成转义的, 字节里找不到 ``set_comment_mix``, 解出来却是它 —— 原来粗筛直接放过, 1e400 又变回 null。"""
    client, c = live
    before = c.rows["projects"][0]["default_params"]
    body = _mcp_body(c, '{"short_min": 1e400}', tool=name)
    assert b"set_comment_mix" not in body
    assert json.loads(A._spell_out_non_finite(body))["params"]["arguments"]["mix"] == {"short_min": "1e400"}
    r = client.post("/", headers=MCP_H, content=body)
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result.get("isError") is True and "现在是 '1e400'" in result["content"][0]["text"], result
    assert _writes(c) == [] and c.rows["projects"][0]["default_params"] == before


def test_a_body_with_backslashes_for_another_tool_is_still_untouched(http):
    _client_, c = http
    for body in (_mcp_body(c, '{"brand_max": 1e400, "note": "a\\nb"}', tool="score_comments"),
                 _mcp_body(c, '{"brand_max": 0.6, "note": "\\u4e2d"}'),            # 这个工具, 但没有坏的数
                 b'{"x": "\\u0073et_comment_mix", not json'):
        assert A._spell_out_non_finite(body) is body, body[:80]


def test_the_body_guard_hands_the_body_on_whole_and_passes_everything_else_through():
    anyio = pytest.importorskip("anyio")
    seen = []

    async def inner(scope, receive, send):
        first = await receive()
        seen.append((scope.get("method"), dict(scope.get("headers") or []), first, await receive()))

    def run(method, messages):
        queue = list(messages)

        async def receive():
            return queue.pop(0) if queue else {"type": "http.disconnect"}

        async def go():
            scope = {"type": "http", "method": method, "headers": [(b"content-length", b"7")]}
            await A._McpFiniteNumbers(inner)(scope, receive, None)
        anyio.run(go)
        return seen.pop()

    method, headers, first, second = run("POST", [{"type": "http.request", "body": b'{"a"', "more_body": True},
                                                  {"type": "http.request", "body": b": 1}", "more_body": False}])
    assert first == {"type": "http.request", "body": b'{"a": 1}', "more_body": False}, "分块来的拼成一块交下去"
    assert second == {"type": "http.disconnect"} and headers[b"content-length"] == b"7", "没改的请求体, 头也不动"
    _m, _h, first, second = run("POST", [{"type": "http.request", "body": b'{"a"', "more_body": True},
                                         {"type": "http.disconnect"}])
    assert first == {"type": "http.request", "body": b'{"a"', "more_body": True} and second == {"type": "http.disconnect"}
    _m, _h, first, _s = run("GET", [{"type": "http.request", "body": b"x", "more_body": False}])
    assert first == {"type": "http.request", "body": b"x", "more_body": False}
    raw = (b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"set_comment_mix","arguments":'
           b'{"mix":{"brand_max":NaN}}}}')
    _m, headers, first, _s = run("POST", [{"type": "http.request", "body": raw, "more_body": False}])
    assert json.loads(first["body"])["params"]["arguments"]["mix"] == {"brand_max": "NaN"}
    assert headers[b"content-length"] == str(len(first["body"])).encode()
