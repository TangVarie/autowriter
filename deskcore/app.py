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
          DESKCORE_ALLOWED_HOSTS  可选, 逗号分隔; 设了才开 MCP 的 Host 校验
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

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import identity, tools, vocab

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


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path.rstrip("/") in ("/health", ""):
        return await call_next(request)     # 健康检查不带 key
    try:
        caller = identity.resolve(_extract_key(request))
    except identity.AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    token = _caller.set(caller)
    try:
        return await call_next(request)
    finally:
        _caller.reset(token)


@app.get("/health")
def health() -> dict:
    """回显实际解析到的配置 —— 让配错当场可见。

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

    db_ok, db_note = True, "ok"
    try:
        db.get_service_client().table("projects").select("id").limit(1).execute()
    except Exception as exc:  # noqa: BLE001
        db_ok, db_note = False, f"{type(exc).__name__}: {exc}"[:160]

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
            # 注意 /health 本身仍返 200: Railway 只看状态码, 而库瞬断这种可恢复
            # 故障不该把整个部署卡住。真正的越权风险已经在 identity.resolve
            # 那一层 fail-closed 掉了, 不靠状态码兜。
            "auth": {"ok": auth_ok, "note": auth_note},
            "anonymous_allowed": identity.anonymous_allowed(),
            "st_cache_disabled": os.environ.get("AW_DISABLE_ST_CACHE"),
        },
    }


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

    allowed = [h.strip() for h in
               (os.environ.get("DESKCORE_ALLOWED_HOSTS") or "").split(",") if h.strip()]
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
