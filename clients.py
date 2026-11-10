"""
统一管理外部 SDK client 单例 + 重试 middleware。

为什么单独抽一个模块
─────────────────────
之前 anthropic.Anthropic / google_genai.Client 在 generator.py / memory.py /
dedup.py 至少 6 处分散构造，每次构造意味着重建 httpx 连接池。在多角色并行
+ 队列模式下，单批可能 new 5+ 次 client，启动延迟和 GC 压力都没必要。

同时 ``_call_with_retry`` 之前只在 Anthropic 路径用，并且只能 retry Anthropic
特定异常。其它路径（Supabase / Storage）需要重试时只能手写 try/except，
统一性差。

本模块提供：
  - ``get_anthropic_client()`` ：process-global Anthropic 单例（线程安全）
  - ``get_genai_client()``     ：process-global google-genai 单例（同）
  - ``reset_clients()``        ：测试 / 配置 reload 时清空缓存
  - ``with_retry(fn, ...)``    ：通用指数退避，可配置哪些异常 retryable
  - ``with_anthropic_retry(fn)`` ：Anthropic 特化版本（429/502/503/529 +
    connection / timeout 重试）

注意 ``@st.cache_resource`` 也能实现 process-global 单例，但本模块用纯
threading.Lock 不依赖 Streamlit——worker 线程没有 ScriptRunContext，调
st.cache_resource 装饰的函数会触发 "missing context" warning。

备用中转站(D-148, 可选; 机制在 ``relay_failover.py``, 三仓同一份)
─────────────────────────────────────────────────────────────────
``ANTHROPIC_BASE_URL_BACKUP`` + ``ANTHROPIC_API_KEY_BACKUP`` 都配了才启用; 没配
(或 ``RELAY_FORCE=primary``)时上面两个函数走的就是原来那份代码, 一个字节不差。
配了之后 ``get_anthropic_client()`` 返回一个代理(``RelayAnthropicClient``):
``ClaudeEngine`` 把 client 缓存在 ``self._client``、每个 ``call_fn`` 都闭包住一个
具体 client, 只有代理才能在【每次尝试】时决定打主站还是备用站。
``with_anthropic_retry`` 的每一次尝试 = 先打主站一次(不重试, 限时 60 秒), 主站
超时 / 连不上 / 429 / 5xx / 401-404 / 流式中途 60 秒没新字节或断开, 就把同一次尝试改打
备用站; 主站连续 2 次失败
(或连续 2 次非流式调用慢过 30 秒)熔断 20 分钟, 期间直接走备用站。
日志只写 primary / backup、状态码、错误类型、耗时和次数, 从不写地址、key 或报错原文。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable, Iterable, Optional, TypeVar

import anthropic
import httpx

import config
import relay_failover


def _relay_getenv(name: str) -> Optional[str]:
    """relay_failover 读配置走 config(先 env 再 st.secrets); config 里没有的名字退回 env。"""
    if hasattr(config, name):
        v = getattr(config, name)
        return None if v is None else str(v)
    return os.environ.get(name)


relay_failover.configure(_relay_getenv)

# google-genai 是可选依赖（embedding 需要，但生成不强求）。Import 失败时把
# get_genai_client 退化为返回 None，调用方据此 fallback 到纯文本路径。
try:
    from google import genai as _google_genai
    _GENAI_AVAILABLE = True
except Exception:
    _google_genai = None  # type: ignore[assignment]
    _GENAI_AVAILABLE = False


# ── Anthropic singleton ───────────────────────────────────────────────────

_anthropic_client: Optional[anthropic.Anthropic] = None
_anthropic_lock = threading.Lock()


# ── 备用中转站 (D-148): 只在配了备用站时才用到下面这些 ──────────────────────────

AW_PRIMARY_TIMEOUT_S = 60.0   # 自动模式下主站单次限时(只限主站; 流式时是两次收到字节之间的空闲上限)
AW_SLOW_S = 30.0              # 非流式调用主站成功但慢过这个数 → 结果照用, 但按熔断失败计
_relay_lock = threading.Lock()
_relay_clients: dict = {}     # "primary"(限时) | "primary_plain"(原样) | "backup"
_relay_proxy = None
_relay_tls = threading.local()  # .target "primary" | "backup" | None ; .streamed bool


class StreamIdleTimeout(TimeoutError):
    """主站流式【中途】超时(60 秒没有新字节)。

    SDK 只包装"拿到响应头之前"的网络错误(APITimeoutError / APIConnectionError);
    流已经开始之后的卡住 / 断开, 是从 ``get_final_message`` 里原样冒出来的
    ``httpx.ReadTimeout`` / ``httpx.RemoteProtocolError`` 之类 —— 共享核心不认这些类名,
    老的重试判据也不认, 主站卡在半路就会直接抛给调用方, 根本不切。
    所以只在【主站那次尝试】里把它们换成核心认的 TimeoutError / ConnectionError:
    这类异常一定触发切换, 由备用站的结果或异常取而代之, 永远不会抛到调用方手里。
    备用站那次尝试不换, 原样抛(= 老行为)。消息是固定文案, 不带原异常内容。
    """


class StreamDisconnected(ConnectionError):
    """主站流式中途断开(见 ``StreamIdleTimeout``)。"""


_STREAM_TIMEOUT_ERRORS = (httpx.TimeoutException,)
_STREAM_DROP_ERRORS = (httpx.NetworkError, httpx.RemoteProtocolError)


def _relay_real_client(slot: str):
    c = _relay_clients.get(slot)
    if c is not None:
        return c
    with _relay_lock:
        c = _relay_clients.get(slot)
        if c is None:
            if slot == "backup":
                c = relay_failover.sdk_backup_client(anthropic, max_retries=0)
            else:
                kw: dict = {"api_key": config.ANTHROPIC_API_KEY}
                if config.ANTHROPIC_BASE_URL:
                    kw["base_url"] = config.ANTHROPIC_BASE_URL
                kw["max_retries"] = 0
                if slot == "primary":
                    kw["timeout"] = relay_failover.primary_timeout_s(AW_PRIMARY_TIMEOUT_S)
                c = anthropic.Anthropic(**kw)
            _relay_clients[slot] = c
        return c


def _relay_slot() -> str:
    t = getattr(_relay_tls, "target", None)
    if t in ("primary", "backup"):
        return t
    # 不在 with_anthropic_retry 里的调用: 只走备用 → 备用站; 否则原样的主站 client(不限时)
    return "backup" if relay_failover.mode() == relay_failover.MODE_BACKUP_ONLY else "primary_plain"


class _MessagesShim:
    def __init__(self, real):
        self._real = real

    def create(self, **kw):
        return self._real.create(**kw)

    def stream(self, **kw):
        _relay_tls.streamed = True     # 流式不按"慢"判: 长输出本来就要几十秒
        return self._real.stream(**kw)

    def __getattr__(self, name):
        return getattr(self._real, name)


class RelayAnthropicClient:
    """只在配了备用站时由 get_anthropic_client() 返回。

    每次访问 ``.messages`` 时按当前线程正在跑的那次尝试(primary / backup)取真 client,
    所以缓存了 client 引用的调用方(ClaudeEngine、各个 call_fn 闭包)也能切换。
    """

    @property
    def messages(self):
        return _MessagesShim(_relay_real_client(_relay_slot()).messages)

    def __getattr__(self, name):
        return getattr(_relay_real_client(_relay_slot()), name)


def _get_relay_proxy() -> RelayAnthropicClient:
    global _relay_proxy
    if _relay_proxy is not None:
        return _relay_proxy
    with _relay_lock:
        if _relay_proxy is None:
            _relay_proxy = RelayAnthropicClient()
        return _relay_proxy


def relay_status() -> dict:
    """备用中转站状态(模式 / 是否配置 / 开关 / 熔断 / 本进程计数), 不含地址和 key。"""
    return relay_failover.health(include_counts=True)


def get_anthropic_client() -> anthropic.Anthropic:
    """返回 process-global Anthropic client，懒加载 + 线程安全。

    多线程下 double-checked locking：先无锁读快路径，未命中才上锁创建。
    httpx 连接池由 Anthropic SDK 内部维护，单例化等于全 process 复用一个
    连接池，避免每次调用 messages.create 都重建。

    配了备用中转站(D-148)时返回 ``RelayAnthropicClient`` 代理; 没配时就是下面原来那段。
    """
    global _anthropic_client
    if relay_failover.mode() != relay_failover.MODE_PRIMARY_ONLY:
        return _get_relay_proxy()  # type: ignore[return-value]
    if _anthropic_client is not None:
        return _anthropic_client
    with _anthropic_lock:
        if _anthropic_client is not None:
            return _anthropic_client
        kwargs: dict = {"api_key": config.ANTHROPIC_API_KEY}
        if config.ANTHROPIC_BASE_URL:
            kwargs["base_url"] = config.ANTHROPIC_BASE_URL
        # R-042: 关掉 SDK 内置重试(默认 2 次)。所有调用点都包在
        # with_anthropic_retry(外层至多 6 次尝试)里, 双层叠乘 = 持续 429 时
        # 最多 ~18 个 HTTP 请求、内外退避相加可阻塞 daemon 线程数分钟。
        # 收敛为单层: 重试策略只在 with_anthropic_retry 一处(500/504 已补)。
        kwargs["max_retries"] = 0
        _anthropic_client = anthropic.Anthropic(**kwargs)
        return _anthropic_client


# ── google-genai singleton ────────────────────────────────────────────────

_genai_client = None
_genai_lock = threading.Lock()


def get_genai_client():
    """返回 process-global google-genai Client；未安装 / 未配 key 时返回 None。

    调用方约定：拿到 None 时退化到 text-only 路径，不抛错。
    """
    global _genai_client
    if _genai_client is not None:
        return _genai_client
    if not _GENAI_AVAILABLE or not getattr(config, "GOOGLE_API_KEY", ""):
        return None
    with _genai_lock:
        if _genai_client is not None:
            return _genai_client
        kwargs: dict = {"api_key": config.GOOGLE_API_KEY}
        if getattr(config, "GOOGLE_BASE_URL", ""):
            kwargs["http_options"] = {"base_url": config.GOOGLE_BASE_URL}
        try:
            _genai_client = _google_genai.Client(**kwargs)
        except Exception:
            _genai_client = None
        return _genai_client


def genai_available() -> bool:
    """True iff google-genai SDK is importable AND a client could be built."""
    return get_genai_client() is not None


def reset_clients() -> None:
    """强制下次调用重新创建。仅在测试或显式配置 reload 后用。"""
    global _anthropic_client, _genai_client, _relay_proxy
    with _anthropic_lock:
        _anthropic_client = None
    with _relay_lock:
        _relay_clients.clear()
        _relay_proxy = None
    with _genai_lock:
        _genai_client = None
    # R-042: generator._ENGINE_CACHE 里的引擎实例在构造时缓存了 client 引用,
    # 只清本模块单例的话, 换 key/base_url 后所有生成仍走旧 client —— reset
    # 语义对生成路径失效。经 sys.modules 清(不直接 import generator, 避免
    # clients ↔ generator 循环导入)。
    import sys
    _gen = sys.modules.get("generator")
    if _gen is not None:
        try:
            _gen._ENGINE_CACHE.clear()
        except Exception:
            pass


# ── Retry middleware ──────────────────────────────────────────────────────

T = TypeVar("T")


def with_retry(
    fn: Callable[[], T],
    *,
    retryable: Iterable[type[BaseException]] = (Exception,),
    max_retries: int = 5,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    is_retryable: Optional[Callable[[BaseException], bool]] = None,
    on_retry: Optional[Callable[[int, BaseException], None]] = None,
) -> T:
    """通用指数退避重试。

    Args:
        fn: 无参 callable；每次重试都调一次（调用方负责让 fn 幂等）。
        retryable: 触发重试的异常类型白名单。不在白名单的异常立即抛出。
        max_retries: 最多重试次数（实际尝试次数 = max_retries + 1）。
        base_delay: 首次重试前的等待秒数。
        max_delay: 退避上限。
        is_retryable: 进一步过滤（例如 APIStatusError 只在 status_code ∈
            {429, 502, 503, 529} 时才重试）。返回 False 立即抛出。
        on_retry: 每次重试前的钩子（attempt_index_1based, exc）；用来打日志。

    Raises:
        最后一次失败的异常（保留原始 traceback）。
    """
    delay = base_delay
    last_error: Optional[BaseException] = None
    retryable_tuple = tuple(retryable)
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except retryable_tuple as exc:
            if is_retryable is not None and not is_retryable(exc):
                raise
            last_error = exc
            if attempt >= max_retries:
                break
            if on_retry is not None:
                try:
                    on_retry(attempt + 1, exc)
                except Exception:
                    pass
            time.sleep(delay)
            delay = min(delay * 2, max_delay)
    # 控制流到这里说明 retryable_tuple 命中但已达上限；last_error 必非 None。
    assert last_error is not None
    raise last_error


def _is_anthropic_transient(exc: BaseException) -> bool:
    """判断 Anthropic 异常是否是可重试的暂时性错误。"""
    if isinstance(exc, anthropic.RateLimitError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        # R-042: 补 500/504 —— Anthropic 文档视 5xx 为可重试; 旧表漏掉它们时
        # 还有 SDK 内置重试兜底, 现在 SDK 层已关(max_retries=0), 必须补全。
        return exc.status_code in (429, 500, 502, 503, 504, 529)
    if isinstance(exc, anthropic.APIConnectionError):
        return True
    return False


def with_anthropic_retry(
    fn: Callable[[], T],
    *,
    max_retries: int = 5,
) -> T:
    """``with_retry`` 的 Anthropic 特化版本：等价于历史上的
    ``generator._call_with_retry``，保留兼容（参数语义一致）。

    Retries on:
      - RateLimitError (429)
      - APIStatusError with transient codes: 429, 502, 503, 529
      - APIConnectionError / APITimeoutError

    配了备用中转站(D-148)时, 外层重试不变, 每一次尝试改成 ``relay_failover.call``:
    主站一次(限时、不重试) → 该切就把同一个 ``fn`` 在备用站上再跑一次; 重试判据作用在
    ``call`` 抛出来的那个异常上(切过去之后就是备用站的异常, 原样不改)。
    """
    relay_mode = relay_failover.mode()
    if relay_mode == relay_failover.MODE_PRIMARY_ONLY:
        return with_retry(
            fn,
            retryable=(
                anthropic.RateLimitError,
                anthropic.APIStatusError,
                anthropic.APIConnectionError,
            ),
            max_retries=max_retries,
            base_delay=2.0,
            max_delay=60.0,
            is_retryable=_is_anthropic_transient,
        )

    def _unit(target: str):
        prev = getattr(_relay_tls, "target", None)
        _relay_tls.target, _relay_tls.streamed = target, False
        try:
            if target != "primary":
                return fn()
            failed = None
            try:
                return fn()
            except _STREAM_TIMEOUT_ERRORS:
                failed = StreamIdleTimeout
            except _STREAM_DROP_ERRORS:
                failed = StreamDisconnected
            # 在 except 外面抛: 主站原异常(可能带地址)连 __context__ 都不挂
            raise failed("primary stream stalled or dropped")
        finally:
            _relay_tls.target = prev

    def _slow() -> float:
        return 0.0 if getattr(_relay_tls, "streamed", False) else relay_failover.slow_s(AW_SLOW_S)

    return with_retry(
        lambda: relay_failover.call(_unit, relay_mode=relay_mode,
                                    primary_url=config.ANTHROPIC_BASE_URL or "", slow_after_s=_slow),
        retryable=(
            anthropic.RateLimitError,
            anthropic.APIStatusError,
            anthropic.APIConnectionError,
        ),
        max_retries=max_retries,
        base_delay=2.0,
        max_delay=60.0,
        is_retryable=_is_anthropic_transient,
    )


# ── Gemini 重试 ───────────────────────────────────────────────────────────
# google-genai SDK 的错误层级在不同版本里漂得比较厉害：旧版直接抛 ValueError /
# RuntimeError；新版引入 ``google.genai.errors`` 模块带 ``APIError`` /
# ``ClientError`` / ``ServerError`` / ``FailedPreconditionError`` 等。本函数同时
# 兼容两套：能 import ``errors`` 就按状态码 / 类型精细过滤；不能就退化到看
# 异常消息中的关键词（``429`` / ``rate``/``timeout``/``unavailable``/``5xx``）。

_GEMINI_TRANSIENT_PATTERNS = (
    "429", "rate limit", "rate_limit", "ratelimit",
    "500", "502", "503", "504", "529",
    "internal error", "service unavailable", "deadline exceeded",
    "timeout", "timed out", "connection",
    "resource exhausted", "resource_exhausted",
)


def _is_gemini_transient(exc: BaseException) -> bool:
    """判断 Gemini SDK 抛出的异常是不是可重试的暂时性错误。"""
    # 优先用 SDK 自带的错误类型
    try:
        from google.genai import errors as _genai_errors  # type: ignore
        # ClientError 通常是 4xx；只在 429 重试
        if isinstance(exc, getattr(_genai_errors, "ClientError", ())):
            code = getattr(exc, "code", None)
            if code in (429,):
                return True
            return False
        # ServerError = 5xx，全部重试
        if isinstance(exc, getattr(_genai_errors, "ServerError", ())):
            return True
        # APIError 兜底（其它已知 API 错误）
        if isinstance(exc, getattr(_genai_errors, "APIError", ())):
            code = getattr(exc, "code", None)
            if code in (429, 500, 502, 503, 504, 529):
                return True
    except Exception:
        pass
    # 退化：消息里关键词匹配
    msg = str(exc).lower()
    return any(pat in msg for pat in _GEMINI_TRANSIENT_PATTERNS)


def with_gemini_retry(
    fn: Callable[[], T],
    *,
    max_retries: int = 4,
) -> T:
    """``with_retry`` 的 Gemini 特化版本。

    Retries on transient errors (429, 5xx, timeout, connection).  Non-retryable
    errors (400 InvalidArgument, 403, content blocked) are raised immediately
    so the user sees a real failure instead of waiting through 4 backoffs.
    """
    return with_retry(
        fn,
        retryable=(Exception,),  # Gemini SDK 错误层级不稳，用 is_retryable 收口
        max_retries=max_retries,
        base_delay=2.0,
        max_delay=30.0,
        is_retryable=_is_gemini_transient,
    )
