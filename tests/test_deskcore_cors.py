"""浏览器侧的 MCP 客户端能不能连上 —— 也就是 CORS 这一层。

这一层的故障有个共同点: **服务端一点痕迹都不留, 客户端报的又是传输层错误**。
浏览器对"预检没通过"的报法是 ``TypeError: fetch failed``, 不是 401 ——
拿着这个错去查, 看起来像网络断了、像证书坏了、像地址写错了, 唯独不像
"你少发了一个响应头"。

而所有手工验证都绕开它: curl 不做预检, 浏览器地址栏打 /health 是同源导航
也不做预检, REST 自测同样是 curl。于是 /health 全绿、curl 全绿, 而任何在
浏览器 / Electron 渲染层里发请求的 MCP 客户端一个都连不上。

⚠️ 这些断言写的是**不许出现的形态**, 不是当前的写法:
  · 预检不许是 401 —— 预检按规范就不带自定义头, 拿它去过鉴权必然失败
  · 401 本身也必须带 CORS 头 —— 否则客户端连"是鉴权问题"都读不到
  · 通配 origin 与 allow_credentials 不许同时开 —— 浏览器会拒绝整个响应
"""

from __future__ import annotations

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

from fastapi.middleware.cors import CORSMiddleware   # noqa: E402

import deskcore.app as A                             # noqa: E402


KEYS = ('{"k-test": {"user_id": "22222222-2222-2222-2222-222222222222",'
        ' "name": "t"}}')
ORIGIN = "http://localhost:5173"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("DESKCORE_KEYS", KEYS)
    return fastapi_testclient.TestClient(A.app, raise_server_exceptions=False)


def _preflight(client, path="/mcp", method="POST",
               headers="content-type,x-deskcore-key"):
    """一个**规范形态**的预检: 带 Origin 与 Access-Control-Request-*,
    不带任何自定义头(浏览器就是这么发的)。"""
    return client.options(path, headers={
        "Origin": ORIGIN,
        "Access-Control-Request-Method": method,
        "Access-Control-Request-Headers": headers,
    })


# ══════════════════════════════════════════════════════════════════════
# 预检
# ══════════════════════════════════════════════════════════════════════

def test_preflight_is_not_401(client):
    """预检撞上鉴权 = 整个客户端连不上, 且报成传输层错误。

    这是本文件存在的唯一理由。预检不带 key **是规范要求的**, 不是客户端
    的疏忽, 所以"让客户端在预检里带上 key"不是修法。
    """
    r = _preflight(client)
    assert r.status_code != 401, (
        "预检被鉴权拦了 —— 浏览器侧会报 fetch failed, 查不出是鉴权问题")
    assert r.status_code < 400, f"预检返回 {r.status_code}"


def test_preflight_allows_the_key_header(client):
    """自定义头没被允许 = 预检过了也白过, 真请求照样发不出去。"""
    r = _preflight(client)
    allowed = r.headers.get("access-control-allow-headers", "").lower()
    assert allowed, "预检没回 access-control-allow-headers"
    assert "*" in allowed or "x-deskcore-key" in allowed, (
        f"X-Deskcore-Key 不在允许名单里: {allowed!r}")


def test_preflight_carries_an_allow_origin(client):
    r = _preflight(client)
    got = r.headers.get("access-control-allow-origin", "")
    assert got in ("*", ORIGIN), f"预检没回可用的 allow-origin: {got!r}"


@pytest.mark.parametrize("verb", ["POST", "GET", "DELETE"])
def test_all_three_mcp_verbs_survive_preflight(client, verb):
    """streamable HTTP 三个动词都用得到: POST 发消息 / GET 开事件流 /
    DELETE 拆会话。漏一个的表现同样是 fetch failed。"""
    r = _preflight(client, method=verb)
    assert r.status_code < 400, f"{verb} 预检失败: {r.status_code}"
    allowed = r.headers.get("access-control-allow-methods", "").lower()
    assert "*" in allowed or verb.lower() in allowed, (
        f"{verb} 不在 allow-methods 里: {allowed!r}")


# ══════════════════════════════════════════════════════════════════════
# 顺序 —— CORS 必须裹在鉴权外面
# ══════════════════════════════════════════════════════════════════════

def test_a_401_still_carries_cors_headers(client):
    """鉴权失败时**也**要带 CORS 头, 否则客户端读不到这个 401。

    没有 allow-origin 的响应, 浏览器直接不交给 JS —— 客户端拿到的还是
    fetch failed。也就是说 CORS 装在鉴权里面等于没装: 唯一会被拦的请求
    (401) 恰恰是最需要被读到的那个。
    """
    r = client.post("/mcp", headers={"Origin": ORIGIN}, json={})
    assert r.status_code == 401, "这条前提坏了: 无 key 应该是 401"
    got = r.headers.get("access-control-allow-origin", "")
    assert got in ("*", ORIGIN), (
        f"401 上没有 allow-origin({got!r}) —— CORS 中间件装在鉴权里面了")


def _cors_layer():
    for m in A.app.user_middleware:
        if getattr(m, "cls", None) is CORSMiddleware:
            return m
    raise AssertionError("app 上根本没装 CORSMiddleware")


def test_cors_is_the_outermost_middleware():
    """Starlette 里 user_middleware[0] 就是最外层(add_middleware 是 insert(0))。

    直接断言位置而不只断言行为: 行为测试会在"某天有人在 CORS 之后又加了
    一层中间件"时静默失效, 而那正是这个 bug 复发的方式。
    """
    layers = A.app.user_middleware
    assert layers, "一层中间件都没有"
    assert getattr(layers[0], "cls", None) is CORSMiddleware, (
        "CORS 不在最外层 —— 注册顺序错了, 预检会先撞上鉴权。"
        f"当前最外层是 {getattr(layers[0], 'cls', layers[0])}")


# ══════════════════════════════════════════════════════════════════════
# 配置口径
# ══════════════════════════════════════════════════════════════════════

def _cors_kwargs():
    m = _cors_layer()
    kw = dict(getattr(m, "kwargs", {}) or {})
    if not kw and getattr(m, "options", None):        # 老版 Starlette
        kw = dict(m.options)
    return kw


def test_wildcard_origin_never_ships_with_credentials():
    """两者同时开时浏览器【拒绝整个响应】—— 比不配还糟, 因为看着像配了。"""
    kw = _cors_kwargs()
    if "*" in (kw.get("allow_origins") or []):
        assert kw.get("allow_credentials") is not True, (
            "allow_origins=* 且 allow_credentials=True —— 浏览器会拒绝整个响应")


def test_session_id_is_exposed_to_the_client():
    """跨源时浏览器默认只放行六个安全响应头。会话 ID 不显式暴露的话,
    有状态模式下客户端拿不到 Mcp-Session-Id, 第二个请求就掉线。"""
    exposed = [h.lower() for h in (_cors_kwargs().get("expose_headers") or [])]
    assert "mcp-session-id" in exposed or "*" in exposed, (
        f"Mcp-Session-Id 没暴露给客户端: {exposed}")


def test_health_echoes_the_two_lists_that_break_clients_silently(client):
    """origin 和 host 两份名单配错都只在客户端侧报错, 服务端不留痕。
    回显出来才排得了 —— 之前代码注释里写着"会回显", 实际没有。"""
    cfg = client.get("/health").json()["config"]
    assert "mcp_allowed_origins" in cfg, "/health 没回显 CORS origin 名单"
    assert "mcp_allowed_hosts" in cfg, "/health 没回显 MCP host 名单"


# ══════════════════════════════════════════════════════════════════════
# /mcp 不带斜杠不许 307
# ══════════════════════════════════════════════════════════════════════
# 同族的第三个问题: Mount("/mcp") 的正则匹配不上光秃秃的 /mcp, 于是
# redirect_slashes 兜底成 307 → /mcp/。而文档、CI、给客户端的地址全都是不带
# 斜杠的那个。平时看不出来, 是因为验证用的东西都自动跟随重定向。

_INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "t", "version": "1"}}}
_H = {"X-Deskcore-Key": "k-test",
      "Accept": "application/json, text/event-stream",
      "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def live_client():
    """带 lifespan 的 client —— MCP 的 session manager 要真的跑起来。

    ⚠️ 必须是 module 级: ``StreamableHTTPSessionManager.run()`` 每个实例只
    允许调用一次, 而 _mcp 是模块级单例。函数级 fixture 会在第二个用到它的
    测试上抛 "can only be called once per instance"。
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DESKCORE_KEYS", KEYS)
        with fastapi_testclient.TestClient(A.app, raise_server_exceptions=False) as c:
            yield c


def test_bare_mcp_path_is_not_a_redirect(live_client):
    """``/mcp`` 必须**当场**应答, 不许 307 到 ``/mcp/``。

    ⚠️ ``follow_redirects=False`` 是这条的全部意义 —— 默认跟随的话 307 和 200
    看起来一模一样, 这个 bug 就是这么藏了这么久的。
    """
    r = live_client.post("/mcp", json=_INIT, headers=_H, follow_redirects=False)
    assert r.status_code != 307, (
        "/mcp 仍在 307 跳转 —— 跨源时这一跳是独立的失败路径, 报法同样是 fetch failed")
    assert r.status_code == 200, f"/mcp 直接应答失败: {r.status_code} {r.text[:200]}"
    assert r.json()["result"]["serverInfo"]["name"] == "deskcore"


def test_the_slashed_path_still_works(live_client):
    """改写不许把原来能用的那条弄坏。"""
    r = live_client.post("/mcp/", json=_INIT, headers=_H, follow_redirects=False)
    assert r.status_code == 200, f"/mcp/ 坏了: {r.status_code} {r.text[:200]}"


@pytest.mark.parametrize("path", ["/health", "/ready", "/mcpfoo", "/tool/list_projects"])
def test_normalize_only_touches_the_exact_bare_path(live_client, path):
    """只改写字面量 ``/mcp``。前缀撞名的路径(/mcpfoo)绝不能被顺手改掉 ——
    那会把一个 404 变成一次真的 MCP 调用。"""
    r = live_client.get(path, follow_redirects=False)
    assert r.status_code != 200 or path in ("/health", "/ready"), (
        f"{path} 被改写到 MCP 上去了")
    if path == "/mcpfoo":
        assert r.status_code in (401, 404), f"/mcpfoo 应该是 404/401, 实际 {r.status_code}"


# ══════════════════════════════════════════════════════════════════════
# 没有因此把门打开
# ══════════════════════════════════════════════════════════════════════

def test_cors_did_not_turn_into_an_auth_bypass(client):
    """带上 Origin 不等于免鉴权。CORS 只管浏览器读不读得到响应,
    真正的门还是 identity 那层 —— 这条守的就是它没被顺手撬开。"""
    r = client.post("/tool/list_projects", headers={"Origin": ORIGIN}, json={})
    assert r.status_code == 401, (
        f"带 Origin 的请求拿到了 {r.status_code} —— 鉴权被绕过了")


def test_preflight_does_not_leak_a_body(client):
    """预检是协议握手, 不该带任何业务内容。"""
    r = _preflight(client)
    assert not r.content or r.content in (b"OK", b'"OK"'), (
        f"预检回了内容: {r.content[:200]!r}")
