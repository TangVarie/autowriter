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
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Iterable, Optional, TypeVar

import anthropic

import config

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


def get_anthropic_client() -> anthropic.Anthropic:
    """返回 process-global Anthropic client，懒加载 + 线程安全。

    多线程下 double-checked locking：先无锁读快路径，未命中才上锁创建。
    httpx 连接池由 Anthropic SDK 内部维护，单例化等于全 process 复用一个
    连接池，避免每次调用 messages.create 都重建。
    """
    global _anthropic_client
    if _anthropic_client is not None:
        return _anthropic_client
    with _anthropic_lock:
        if _anthropic_client is not None:
            return _anthropic_client
        kwargs: dict = {"api_key": config.ANTHROPIC_API_KEY}
        if config.ANTHROPIC_BASE_URL:
            kwargs["base_url"] = config.ANTHROPIC_BASE_URL
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
    global _anthropic_client, _genai_client
    with _anthropic_lock:
        _anthropic_client = None
    with _genai_lock:
        _genai_client = None


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
        return exc.status_code in (429, 502, 503, 529)
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
    """
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
