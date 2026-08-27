"""deskcore/app.py — 写作台内核服务 (TV D-041 / R-034)。

三个入口, 同一套 tools:
  1. MCP over streamable HTTP   /mcp                ← WorkBuddy / Claude Code / CodeBuddy
  2. 纯 REST                     POST /tool/{name}   ← 兜底 + 自测 + 不支持 MCP 的平台
  3. CLI                        deskcore/cli.py      ← 本地 dry-run 与 selftest

鉴权与身份 (X-Deskcore-Key → user_id, 见 identity.py):
  key 支持三种传法, 优先级从高到低 ——
    a) header  X-Deskcore-Key: <key>
    b) header  Authorization: Bearer <key>
    c) query   ?key=<key>
  三种都收是因为: WorkBuddy 的 HTTP MCP 能不能配自定义 header, 官方更新日志
  只说了支持 HTTP MCP 和 OAuth, 没有权威文档。留 b/c 两条退路, 免得协议层
  卡住整个方案。

降级:
  读类工具出错 → 返回带 error 的可用结构 + 服务端 logger.exception 留痕。
  check_drafts 例外 → 出错抛 500。查重静默放行 = 重演 config.py:132 那个
  ENABLE_DEDUP_REGEN 默认关着的老问题。

部署 (Railway / Render, 与 worker.py 同为独立 service):
  start:  uvicorn deskcore.app:app --host 0.0.0.0 --port $PORT
  env:    SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY   (service_role, 绕 RLS)
          GOOGLE_API_KEY          embedding; 不设则查重降级为纯确定性
          DESKCORE_KEYS 或 DESKCORE_API_KEY + DESKCORE_DEFAULT_USER_ID
          ⚠️ 【必填】。都不配时所有请求 401(ROB-003 起 fail-closed) —— 本服务
             持 service_role 绕 RLS, 匿名放行等于开放全部租户数据。本地开发
             要免 key 跑, 显式设 DESKCORE_ALLOW_ANONYMOUS=1。
          LIBRARIAN_URL / LIBRARIAN_API_KEY          借爆款经验卡; 不设则跳过
          DESKCORE_ALLOWED_HOSTS    可选, 逗号分隔; 设了才开 MCP 的 Host 校验
          DESKCORE_ALLOWED_ORIGINS  可选, 逗号分隔的完整 origin; 不设 = 允许全部。
             放开是安全的: 身份靠显式传的 key, 从不靠 cookie, 浏览器不会自动
             附上 —— 没有 CSRF 面。见 CORS 那段。
          ⚠️ 【不需要】ANTHROPIC_API_KEY / DESKCORE_MODEL —— deskcore 不调 LLM,
             推理全部归调用方模型。见 core.py 里"故意没有 resolve_model"那段。
  见 deskcore/railway.json。

⚠️ import 顺序: 本模块通过包 __init__ 先设 AW_DISABLE_ST_CACHE=1 再 import db
(R-042; 同 worker.py:56)。别在 __init__ 之前 import db。
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os

import anyio
import anyio.to_thread
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import core, identity, tools, vocab

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("deskcore")

SERVICE = "deskcore"
VERSION = "1"

# 当前请求的调用者。MCP 的 ASGI 子应用拿不到 FastAPI 的依赖注入, 用 contextvar
# 在中间件里塞、在工具里取, 是最省事且协程安全的做法。
#
# ⚠️ REST 路由把工具丢进线程池跑 —— starlette 的 run_in_threadpool 会把当前
# contextvars 一并复制过去, 所以 _caller 在工作线程里取得到。若以后换成裸
# threading.Thread, 必须自己 copy_context(), 否则身份会丢成 anonymous。
_caller: contextvars.ContextVar[identity.Caller] = contextvars.ContextVar(
    "deskcore_caller", default=identity.Caller(None, "anonymous", False))


def _extract_key(request: Request) -> str | None:
    key = request.headers.get("x-deskcore-key")
    if key:
        return key
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.query_params.get("key")


def _call_tool(name: str, args: dict) -> object:
    entry = tools.TOOLS.get(name)
    if entry is None:
        raise KeyError(name)
    fn, needs_user = entry
    kwargs = dict(args or {})
    kwargs.pop("_user_id", None)          # 不允许调用方伪造身份
    if needs_user:
        kwargs["_user_id"] = _caller.get().user_id
    return fn(**kwargs)


# ⚠️ _mcp 必须在 app 之前建好: FastAPI 的 lifespan 要在构造时传进去, 而
# lifespan 里要用 _mcp.session_manager。_register_mcp 定义在文件更下面, 靠
# 模块末尾的赋值太晚 —— 所以这里前置声明, 末尾再真正注册路由。
_mcp = None


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    """把 MCP 的 session manager 跑起来。

    ⚠️ 这个不是可选的样板 —— 没有它 /mcp 【整个不能用】。
    app.mount() 挂子应用时, ASGI 的 lifespan 事件【只发给顶层 app】, 不会往
    被 mount 的子应用传。而 FastMCP 的 streamable_http_app() 把
    StreamableHTTPSessionManager.run() 放在它自己的 lifespan 里。于是 session
    manager 的 task group 永远没启动, 每个 MCP 请求在
    streamable_http_manager.py 里抛
    `RuntimeError: Task group is not initialized. Make sure to use run().`

    最坏的地方是【/health 完全正常】: 库通、词表校验和对、鉴权也对, Railway
    healthcheck 一路绿, 服务显示 healthy —— 然后每个 MCP 调用 500。
    round-6 那个冒烟测试只打了 /health 和 /tool/, 正好绕过这里, 所以它一路绿
    到 review。CI 现在会真发一次 initialize 握手。(codex review)

    stateless_http=True 也救不了 —— 无状态指的是不保留跨请求会话, task group
    照样要先起来。
    """
    if _mcp is None:
        yield
        return
    async with _mcp.session_manager.run():
        yield


app = FastAPI(title="deskcore · 写作台内核", version=VERSION,
              lifespan=_lifespan)


# 免鉴权的探针路径。平台的 liveness/readiness 探测**不会**带 key, 所以这两个
# 必须在这里放行 —— 否则 /ready 一律 401, 编排读到的永远是"不健康"。
#
# ⚠️ 它们的返回体是有信息量的(库连通、鉴权配没配、词表校验和)。这是刻意的
# 取舍: 配错要当场可见。但**不许**再往里加任何业务数据或租户信息 —— 这两条
# 路径没有调用者身份, 加什么就等于对全世界公开什么。
_UNAUTHENTICATED_PATHS = frozenset({"/health", "/ready", ""})


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path.rstrip("/") in _UNAUTHENTICATED_PATHS:
        return await call_next(request)     # 健康检查/就绪探测不带 key
    try:
        caller = identity.resolve(_extract_key(request))
    except identity.AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    token = _caller.set(caller)
    try:
        return await call_next(request)
    finally:
        _caller.reset(token)


# ══════════════════════════════════════════════════════════════════════
# /mcp 不带斜杠时不许再走一次 307
# ══════════════════════════════════════════════════════════════════════
# app.mount("/mcp", ...) 建的 Mount 正则是 ``^/mcp(?P<path>/.*)$`` —— 光秃秃
# 的 ``/mcp`` **匹配不上**, 于是落到 Starlette 的 redirect_slashes 兜底:
# 307 跳到 /mcp/。而文档、CI、以及给 WorkBuddy / Claude Code 的地址写的都是
# 不带斜杠的 /mcp, 也就是说**每一个 MCP 请求都要先吃一跳**。
#
# 平时看不出来是因为验证用的东西都自动跟随重定向(curl -L、TestClient、
# httpx 默认)。但跨源时这一跳是独立的一条失败路径: 预检是针对 /mcp 做的,
# 跳到 /mcp/ 之后按 Fetch 规范要重新预检, 各家实现对"预检过的请求能不能跟
# 重定向"处理并不一致 —— 失败的报法同样是 fetch failed, 同样零痕迹。
#
# 写成**纯 ASGI** 中间件而不是 @app.middleware("http"): 后者是
# BaseHTTPMiddleware, 会把响应体收进内存再吐出去, 对 MCP 的 SSE 流是有害的。
# 这里只改 scope 里的一个字符串, 不碰 receive/send。
class _NormalizeMcpPath:
    """把 ``/mcp`` 就地改写成 ``/mcp/``, 让它直接命中 Mount 而不是先 307。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") == "/mcp":
            scope = dict(scope)
            scope["path"] = "/mcp/"
            if scope.get("raw_path"):
                # raw_path 若留着旧值, 下游按它重建 URL 时会与 path 打架。
                scope["raw_path"] = b"/mcp/"
        await self.app(scope, receive, send)


app.add_middleware(_NormalizeMcpPath)


# ══════════════════════════════════════════════════════════════════════
# CORS —— 必须注册在以上所有中间件【之后】(它要在最外层)
# ══════════════════════════════════════════════════════════════════════
# ⚠️ 顺序不是风格问题, 是这段能不能起作用的全部。Starlette 的
# ``add_middleware`` 是 ``insert(0, ...)``, 构建时又 reversed 着往外裹 ——
# **最后注册的跑在最外层**。CORS 必须在最外层, 因为浏览器的预检
# (OPTIONS)【按规范就是不带任何自定义头的】, 也就不带 X-Deskcore-Key。
# 它要是先撞上 auth_middleware, 拿到的是 401, 而浏览器对"预检没通过"的
# 报法是 **fetch failed / TypeError**, 不是 401 ——
# 客户端那头看到的是一个传输层错误, 完全看不出是鉴权的事。
#
# 这个洞之前一直看不见, 因为**手工验证全部绕开了 CORS**: curl 不做预检,
# 浏览器地址栏直接打 /health 是同源导航也不做预检, REST 的 /tool/{name}
# 自测同样是 curl。于是 /health 全绿、curl 全绿, 而任何在浏览器/Electron
# 渲染层里发请求的 MCP 客户端【一个都连不上】。又是同一种病:
# "部署显示健康、功能整个不可用"(见 _lifespan 与 _register_mcp 里的两处)。
#
# allow_credentials 必须是 False:
#   · 本服务的身份靠**显式传的 key**(header 或 ?key=), 从不靠 cookie。
#     浏览器不会自动附上 key, 所以恶意页面即使能发起请求也拿不到任何东西
#     —— 没有 CSRF 面, 放开 origin 是安全的。
#   · 而且开了 credentials 时浏览器【拒绝】通配的 Access-Control-Allow-Origin,
#     两者不能同时要。
# 要收紧就配 DESKCORE_ALLOWED_ORIGINS(逗号分隔的完整 origin,
# 形如 https://app.example.com)。当前生效值 /health 会回显。
_ALLOWED_ORIGINS = [o.strip() for o in
                    (os.environ.get("DESKCORE_ALLOWED_ORIGINS") or "").split(",")
                    if o.strip()] or ["*"]

# MCP 传输层的 Host 白名单(DNS rebinding 保护), 与上面的 CORS origin 是两码事:
# 这个看的是请求的 Host 头, CORS 看的是 Origin 头。留在这儿是为了让 /health
# 能回显 —— 见 _register_mcp 里那段说明, 白名单配错的表现是 421。
_ALLOWED_HOSTS = [h.strip() for h in
                  (os.environ.get("DESKCORE_ALLOWED_HOSTS") or "").split(",")
                  if h.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=False,
    # MCP 的 streamable HTTP 三个动词都用得到: POST 发消息, GET 开事件流,
    # DELETE 拆会话。少写一个的表现同样是预检失败 → fetch failed。
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    # 放开而不是逐个列: 客户端实际会带 X-Deskcore-Key / Authorization /
    # Content-Type / Accept / Mcp-Session-Id / MCP-Protocol-Version /
    # Last-Event-ID, 而这份名单会随 MCP 协议版本变。credentials 已关,
    # 通配没有额外风险; 漏列一个的代价却是整条连不上且报成传输错。
    allow_headers=["*"],
    # 跨源时浏览器默认只让 JS 读到六个"安全"响应头, 会话 ID 必须显式暴露,
    # 否则有状态模式下客户端拿不到 Mcp-Session-Id, 第二个请求就掉线。
    expose_headers=["Mcp-Session-Id", "MCP-Protocol-Version"],
    max_age=600,
)


# /health 专用的线程额度(审计 ROB-004)。
#
# 事故形状: 工具调用全部走 starlette 的**默认** thread limiter(40 个额度),
# 而 `def health()` 这种同步路由**也走同一个 limiter**。check_drafts 这类几十秒
# 的调用一多, /health 就排在它们后面 —— 平台健康检查超时 → 重启容器 → 正在跑
# 的调用全断。deskcore/app.py 下面那段注释描述的就是同款事故的另一半。
#
# 根因已经由 migrations/004 把比对下推到库里堵掉了(工具调用不再是几十秒),
# 但"健康检查和业务抢同一个池子"这件事本身仍然是个雷: 换个慢查询就复发。
# 给 /health 一个**私有** limiter, 它就永远不排在工具后面 —— anyio 的
# to_thread.run_sync 只认传进去的 limiter, 不再碰默认那个。
#
# 额度 2 而不是 1: 平台的健康检查和人工 curl 可能同时打进来, 1 会让后者干等。
_HEALTH_LIMITER = anyio.CapacityLimiter(2)

# 单次探测的墙钟上限。库连接卡死(TCP 黑洞)时 /health 会一直挂着不返回 ——
# 平台照样判超时重启, 而且【没有任何信息】说明卡在哪。
_HEALTH_PROBE_TIMEOUT = float(os.environ.get("DESKCORE_HEALTH_PROBE_TIMEOUT", "5") or 5)


_HEALTH_PROBE_CLIENT = None


def _health_probe_client():
    """/health 专用的 Supabase client: 带**网络层**超时。

    ``db.get_service_client()`` 用的是 supabase-py 的默认超时口径, 对
    "TCP 黑洞"这种连接建起来了、就是不回数据的情况可能一直等下去 —— 而那正是
    ROB-004 要防的场景。这里显式把 postgrest 的超时压到探测预算之内, 让阻塞
    调用**必然会结束**, 上面被放弃的线程才能收场而不是永久驻留。

    留成模块级单例: /health 会被平台每隔几十秒打一次, 每次新建 client 就是
    ROB-013 那个 FD 泄漏的翻版。
    """
    global _HEALTH_PROBE_CLIENT
    if _HEALTH_PROBE_CLIENT is None:
        import config
        import db
        from supabase import create_client
        from supabase.client import ClientOptions
        key = getattr(config, "SUPABASE_SERVICE_ROLE_KEY", "")
        if not key:
            # 没配 service_role 时退回原路径, 让 _db_probe 照常报出那条错。
            return db.get_service_client()
        _HEALTH_PROBE_CLIENT = create_client(
            config.SUPABASE_URL, key,
            options=ClientOptions(
                schema="autowriter",
                # 略小于墙钟预算: 让网络层先于 fail_after 收手, 这样常见情况下
                # 我们拿到的是一条**有信息的**超时错误, 而不是被取消掉的空壳。
                postgrest_client_timeout=max(1.0, _HEALTH_PROBE_TIMEOUT - 1.0),
            ),
        )
    return _HEALTH_PROBE_CLIENT


async def _probe(fn, fallback):
    """在 /health 私有线程额度里跑一个阻塞探测, 超时返回 fallback。

    ⚠️ ``abandon_on_cancel=True`` 不是可有可无的调参, **没有它这个超时根本不
    生效**(codex review 2026-08-24, 已实测)。``to_thread.run_sync`` 默认
    ``abandon_on_cancel=False`` —— 取消要等工作线程自己返回才生效, 于是
    ``fail_after`` 形同虚设:

        with anyio.fail_after(0.3):
            await to_thread.run_sync(sleep_1s2)   # 1.20s 后【返回了值】, 没抛超时

    也就是说改之前, /health 在"库卡住不回"这个**本条修复唯一针对的场景**下
    照样会一直挂着。设成 True 之后同一段代码 0.31s 抛 TimeoutError。

    代价: 被放弃的线程还在后台跑(Python 没法中断阻塞的 socket 读)。实测 anyio
    在取消时就把 limiter 名额还回来了, 所以放弃的线程不会占住私有额度; 但它
    仍然占着一个 OS 线程和一条连接, 直到底层调用自己超时 —— 所以真正的兜底是
    下面 ``_health_probe_client`` 给的**网络层**超时, 让那条调用必然会结束。
    两层缺一不可: 网络超时保证线程能收场, ``fail_after`` 保证 /health 按时应答。
    """
    try:
        with anyio.fail_after(_HEALTH_PROBE_TIMEOUT):
            return await anyio.to_thread.run_sync(
                fn, limiter=_HEALTH_LIMITER, abandon_on_cancel=True)
    except TimeoutError:
        return fallback
    except Exception as exc:  # noqa: BLE001
        logger.warning("health probe failed: %s", exc)
        return fallback


async def _collect_health() -> dict:
    """算出那份健康回显。``/health`` 与 ``/ready`` **共用这一份**。

    分成两个端点但只有一份计算, 是为了让它们不可能漂: 两边各算一次的话,
    迟早出现"live 说好、ready 说坏"却是因为判据写岔了, 而不是真的状态不同。

    ⚠️ 这个回显是【刻意的】: TV docs/19:180-200 记过一次事故, librarian 的模型
    env 变量名配错, 每次 LLM 调用失败被 except 吞掉降级成 [], 外面看永远 200,
    查了很久。

    deskcore 现在一次 LLM 调用都没有(蒸馏搬给调用方模型了), 所以"模型配错"这个
    故障类别已经不存在。剩下要回显的是库连通、embedding 可用性、vendor 词表
    校验和、鉴权配置 —— 每一项都能独立地把服务变成"看着健康、实际残废"。
    """
    # ⚠️ config 是给下面 librarian 那行的 getattr(config, "LIBRARIAN_URL", "") 用的。
    # 它一度被删掉过: round-5 把 model 那行改成走 core.resolve_model() 之后, 我用
    # `grep "config\."` 判定 config 没人用了 —— 那个模式匹配不到 getattr(config, ...)
    # (config 后面没有点), 于是删了 import, /health 每次请求都 NameError 500。
    # 而 railway.json 把 /health 配成健康检查路径, 服务永远不会 healthy。
    # py_compile 抓不到(NameError 是运行期), CI 的 import 图又刻意不 import app.py。
    # 现在 CI 有一步真的起 TestClient 打 /health, 这类问题才会当场红。
    import config
    import db
    import dedup

    def _db_probe() -> tuple[bool, str]:
        try:
            _health_probe_client().table("projects").select("id").limit(1).execute()
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"[:160]
        return True, "ok"

    # ROB-004: 这次探测是 /health 里唯一会打网络的一步, 放进私有线程额度 +
    # 墙钟上限。超时按【不健康】报, 但要把"是超时"写进 note —— 报成
    # "连不上"和报成"卡住了"是两种完全不同的排查方向。
    db_ok, db_note = await _probe(
        _db_probe,
        (False, f"probe timeout >{_HEALTH_PROBE_TIMEOUT:g}s —— 库没有拒绝连接, "
                f"是【卡住不回】(连接池耗尽 / 网络黑洞); 查 Supabase 连接数"),
    )

    vocab_ok, vocab_note = vocab.vendor_checksum_ok()
    emb_ok = dedup.embeddings_available()
    # 只调一次 —— 之前顶上算 auth_ok、下面回显时又调了一遍, 两次之间 env 若
    # 被改过, 回显的 note 和参与 ok 的判断会对不上。
    auth_ok, auth_note = identity.auth_health()

    return {
        "ok": db_ok and vocab_ok and auth_ok,
        "service": SERVICE,
        "version": VERSION,
        "tools": sorted(tools.TOOLS),
        "config": {
            # deskcore 【不调 LLM】。蒸馏搬给调用方模型之后, 服务端一次
            # Anthropic 调用都没有了 —— 所以这里没有模型名可回显, 也就没有
            # "配错模型" 这个故障类别了。见 core.py 里那段"故意没有 resolve_model"。
            # 保留这个字段是为了让读 /health 的人当场知道这是【设计如此】,
            # 而不是漏报了。
            "llm": "none —— 推理归调用方模型(WorkBuddy/Claude Code), "
                   "deskcore 只做数据操作, 不需要 ANTHROPIC_API_KEY / DESKCORE_MODEL",
            "supabase": {"ok": db_ok, "note": db_note},
            "embeddings": {
                "ok": emb_ok,
                "note": ("ok" if emb_ok else
                         "GOOGLE_API_KEY 未配 —— 查重降级为纯确定性(开头精确 + "
                         "四字串重合仍有效), 同角度换说法的标题会漏过"),
            },
            "vendored_vocab": {
                "ok": vocab_ok, "note": vocab_note,
                "version": vocab.VOCAB_VERSION, "authority": vocab.VOCAB_AUTHORITY,
                "combination_space": vocab.combination_space(),
            },
            "librarian": {"configured": bool(os.environ.get("LIBRARIAN_URL")
                                             or getattr(config, "LIBRARIAN_URL", ""))},
            # auth_health 把三态分开: 配好了 / 配了但坏了(全 401) / 没配。
            # ROB-003 之后"没配"也是全 401 —— 不再静默放行, 只有显式设了
            # DESKCORE_ALLOW_ANONYMOUS=1 才放行(那时 note 里会写明是 dev 模式)。
            # 注意 ``/health`` 本身仍返 200(见该端点的说明); 要一个**状态码**能
            # 反映 ok 的, 用 ``/ready`` —— 它不 ready 时返 503。
            "auth": {"ok": auth_ok, "note": auth_note},
            "anonymous_allowed": identity.anonymous_allowed(),
            # 这两条口径配错时的表现都是【客户端侧的传输层错误】, 服务端不留
            # 任何痕迹: origin 不在名单 → 浏览器报 fetch failed; host 不在名单
            # → 421 Misdirected Request。回显出来才能当场对着排, 否则只能靠猜。
            # (_register_mcp 里那句"/health 会回显当前生效的口径"以前是**假的**
            # —— 写了但没实现, 又是一次"说有其实没有"。现在补上。)
            "mcp_allowed_origins": _ALLOWED_ORIGINS,
            "mcp_allowed_hosts": _ALLOWED_HOSTS or "(不校验)",
            "st_cache_disabled": os.environ.get("AW_DISABLE_ST_CACHE"),
        },
    }


@app.get("/health")
async def health() -> dict:
    """**liveness** —— 进程还在、路由还通。永远 200。

    为什么它不跟着 ``ok`` 变状态码(这是刻意的, 别顺手改):
    ``deskcore/railway.json`` 把它配成 healthcheckPath, 而 Railway 对健康检查
    失败的反应是**重启容器**。库瞬断、Supabase 连接数打满这类可恢复故障, 重启
    一遍既治不好也救不回来, 只会把一个还能服务一部分请求的实例变成重启风暴。

    要一个能反映"现在能不能好好干活"的**状态码**, 用 ``/ready``。
    body 里的 ``ok`` 和各项 note 在两个端点上是同一份数据。
    """
    return await _collect_health()


@app.get("/ready")
async def ready():
    """**readiness** —— 库通不通、鉴权配没配好、vendor 词表校验过没有。

    不 ready 时返 **503**(跨库审计 2026-08-24 ROB-005)。这条是给编排/网关用的:
    ``ok=false`` 却回 200, 平台就会继续把流量送给一个"看着健康、实际残废"的
    实例, 而用户侧只看到一串失败。

    ⚠️ ``railway.json`` 目前仍指向 ``/health``(liveness)。要不要把
    healthcheckPath 换成这里, 是**部署行为的变更** —— 换了之后, 部署时如果库
    刚好不通, 这次发布会判失败而不是上线后带病运行。那是个产品决定, 不在这次
    改动范围内。
    """
    payload = await _collect_health()
    if payload.get("ok"):
        return payload
    return JSONResponse(payload, status_code=503)


@app.get("/tools")
def list_tools() -> dict:
    """工具清单 + 说明, 给不支持 MCP 的平台看。"""
    return {"tools": [
        {"name": n, "description": (f.__doc__ or "").strip(), "needs_identity": needs}
        for n, (f, needs) in sorted(tools.TOOLS.items())
    ]}


@app.post("/tool/{name}")
async def rest_tool(name: str, request: Request):
    """纯 REST 调用通道。body = 工具参数的 JSON object。

    存在理由: ① 自测(curl 就能验) ② MCP 协议层万一在某个平台不通的退路。
    """
    if name not in tools.TOOLS:
        raise HTTPException(status_code=404, detail=f"unknown tool: {name}")
    try:
        args = await request.json()
    except Exception:
        args = {}
    if not isinstance(args, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    try:
        # ⚠️ 必须过线程池。工具里全是【同步】调用(Supabase / embedding, record_edit
        # 还会打 Anthropic), 直接在 async 路由里跑会占住 uvicorn 唯一的事件循环 ——
        # 一个慢请求把不相干的 REST / MCP / health 全部拖住。
        # 本仓的 worker 服务踩过同一类坑(subprocess.run 堵死 asyncio → /health 失联
        # → 平台健康检查超时重启容器 → 杀掉正在跑的任务)。
        result = await run_in_threadpool(_call_tool, name, args)
        return {"result": result}
    except HTTPException:
        raise
    except core.ProjectNotFound as exc:
        # 项目 ID 打错 / 项目已删 → 404, 【不是】500。
        # 这跟旁边那条 403 是同一个道理: 500 对调用方的意思是"服务端坏了,
        # 待会儿重试", 于是模型会拿同一个错 ID 一直试; 而且它会把正常的
        # "查无此项目" 计进错误监控。core 里特意把"找不到"和"不是你的"分成
        # 两个异常, 适配层不映射就等于白分。
        logger.warning("tool %s: %s", name, exc)
        raise HTTPException(status_code=404, detail=str(exc)[:300])
    except PermissionError as exc:
        # 归属校验拒绝(审计 COR-015) → 403, 【不是】500。
        # 500 对调用方的意思是"服务端坏了, 待会儿重试", 于是模型会拿同一个错
        # project_id 一直试; 403 才说得清是"这个项目不是你的"。顺带也别让越权
        # 尝试去污染错误监控 —— 它是正常拒绝, 不是故障。
        # 不打 exception 堆栈: 这不是 bug, 但要留一行可审计的痕迹。
        logger.warning("tool %s denied: %s", name, exc)
        raise HTTPException(status_code=403, detail=str(exc)[:300])
    except Exception as exc:  # noqa: BLE001
        # check_drafts 走到这里 = 查重真的挂了, 必须 500 不能装作没事。
        logger.exception("tool %s failed", name)
        raise HTTPException(status_code=500,
                            detail=f"{type(exc).__name__}: {exc}"[:300])


# ── MCP ──────────────────────────────────────────────────────────────
# SDK 缺失时服务照常起(REST 可用), 只是 /mcp 不挂 —— 免得一个可选依赖把整个
# 服务拖 down。

def _register_mcp():
    try:
        from mcp.server.fastmcp import FastMCP
    except Exception:
        logger.warning("mcp SDK not available; /mcp disabled "
                       "(REST /tool/{name} still works)")
        return None

    # ⚠️ streamable_http_path 必须设成 "/": FastMCP 的默认值是 "/mcp",
    # 子应用自己就带 /mcp 路由; 再 app.mount("/mcp", ...) 会让真实端点变成
    # /mcp/mcp —— 文档里给 WorkBuddy / Claude Code 的地址是 /mcp, 初始化请求
    # 会打到空处, 而且不报错只是 404。(实测: 默认路由 ['/mcp'], 设 "/" 后 ['/'])
    # ⚠️ transport_security 必须显式传, 否则 /mcp 在 Railway 上【每个请求 421】。
    # FastMCP 的 host 默认是 "127.0.0.1", 而它看到 localhost 就【自动打开】
    # DNS rebinding 保护, 白名单写死成 127.0.0.1:* / localhost:* / [::1]:*
    # (fastmcp/server.py:180-185)。线上 Host 头是 Railway 的公网域名, 不在名单里
    # → TransportSecurityMiddleware 回 421 Misdirected Request。
    # 而 /health 一切正常, 所以又是一个"部署显示健康、功能整个不可用"。
    #
    # 这里默认【关掉】而不是猜一个白名单: DNS rebinding 防的是浏览器打本机
    # 服务那种场景, 我们是带 key 的公网服务, 本来就靠 identity 那层挡;
    # 而白名单写错的表现是 421, 看起来像客户端的问题, 极难查。
    # 要收紧就配 DESKCORE_ALLOWED_HOSTS(逗号分隔, 支持 "host:*" 通配),
    # /health 会回显当前生效的口径。
    from mcp.server.transport_security import TransportSecuritySettings

    allowed = _ALLOWED_HOSTS
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(allowed),
        allowed_hosts=allowed,
        allowed_origins=allowed,
    )
    mcp = FastMCP(name="deskcore", stateless_http=True, json_response=True,
                  streamable_http_path="/", transport_security=security)

    def _wrap(fn, needs_user: bool):
        # 把 _user_id 从签名里摘掉再注册 —— 模型不该看到它, 也不该能传它。
        import functools
        import inspect

        sig = inspect.signature(fn)
        params = [p for k, p in sig.parameters.items() if not k.startswith("_")]

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            kwargs.pop("_user_id", None)
            if needs_user:
                kwargs["_user_id"] = _caller.get().user_id
            return fn(*args, **kwargs)

        wrapper.__signature__ = sig.replace(parameters=params)
        return wrapper

    for name, (fn, needs_user) in tools.TOOLS.items():
        mcp.add_tool(_wrap(fn, needs_user), name=name,
                     description=(fn.__doc__ or "").strip())
    return mcp


_mcp = _register_mcp()
if _mcp is not None:
    # 子应用请求同样先过外层 auth_middleware, 所以 _caller 在工具里取得到。
    app.mount("/mcp", _mcp.streamable_http_app())
