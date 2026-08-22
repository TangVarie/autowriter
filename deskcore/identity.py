"""deskcore/identity.py — 调用方身份解析。

「项目规则共享 + 个人风格私有」要成立, 服务端必须知道【是谁在调用】。
WorkBuddy / Claude Code 那边没有统一的用户身份可透传, 所以用最朴素可靠的
办法: 一人一把 key, 服务端把 key 映射成 user_id。

配置(二选一, 优先 DESKCORE_KEYS):

  DESKCORE_KEYS='{"k-ziao-xxxx": {"user_id": "<uuid>", "name": "Ziao"},
                  "k-xiaoyu-yyy": {"user_id": "<uuid>", "name": "小鱼"}}'

  或单用户模式:
  DESKCORE_API_KEY=xxxx
  DESKCORE_DEFAULT_USER_ID=<uuid>

两个都不设 = dev 模式, 放行且 user_id 为 None(此时个人层全部退化为共享层)。

⚠️ user_id 要用 autowriter.projects.owner_id / items.user_id 里【已有的】
那个 UUID, 不要新造 —— 否则历史正负例读不到(RUNBOOK.md:150-153 记过一次:
写了 service account UUID 导致 RLS 屏蔽, list_example_items 永远 0 行,
飞轮静默断开)。
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger("deskcore")


class Caller:
    __slots__ = ("user_id", "name", "authenticated")

    def __init__(self, user_id: str | None, name: str = "", authenticated: bool = False):
        self.user_id = user_id
        self.name = name
        self.authenticated = authenticated

    def __repr__(self) -> str:  # pragma: no cover
        return f"Caller(name={self.name!r}, user_id={self.user_id!r})"


class AuthError(Exception):
    """鉴权失败。调用方应转成 401。"""


class _MalformedKeyMap(Exception):
    """DESKCORE_KEYS 配了但解析不了。【绝不能】退化成"没配"。"""


def _key_map() -> dict[str, dict]:
    """解析 DESKCORE_KEYS。配了但格式不对时抛 _MalformedKeyMap, 不返回空 map。

    ⚠️ 这里曾经是 fail-open 的: JSON 写错 → 返回 {} → resolve() 看到"既没有
    key map 也没有单 key" → 判定为 dev 模式 → 【放行所有请求】。生产上一个
    逗号写错就等于把项目数据和全部写工具匿名开放出去。
    显式配了就必须当成"打算开鉴权", 解析失败一律 401。
    """
    raw = os.environ.get("DESKCORE_KEYS")
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _MalformedKeyMap(f"DESKCORE_KEYS is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise _MalformedKeyMap("DESKCORE_KEYS must be a JSON object")
    return parsed


def auth_configured() -> bool:
    """鉴权是否已配置。配了但坏掉也算"已配"(会 401), 不算未配。"""
    if (os.environ.get("DESKCORE_KEYS") or "").strip():
        return True
    return bool(os.environ.get("DESKCORE_API_KEY"))


def auth_health() -> tuple[bool, str]:
    """给 /health 用: 鉴权配置是否可用。配坏了要当场看得见。"""
    try:
        keys = _key_map()
    except _MalformedKeyMap as exc:
        return False, f"{exc} —— 所有请求都会 401, 服务实际不可用"
    if keys:
        missing = [k[:6] + "…" for k, v in keys.items()
                   if not (v or {}).get("user_id", "").strip()]
        if missing:
            return False, f"DESKCORE_KEYS 有条目缺 user_id: {missing}"
        return True, f"{len(keys)} key(s)"
    if os.environ.get("DESKCORE_API_KEY"):
        if not (os.environ.get("DESKCORE_DEFAULT_USER_ID") or "").strip():
            return False, "配了 DESKCORE_API_KEY 但 DESKCORE_DEFAULT_USER_ID 为空"
        return True, "single-key mode"
    return False, "未配鉴权 = dev 模式全放行; 生产必须配"


def resolve(provided: str | None) -> Caller:
    """把请求里的 key 解析成 Caller。未配鉴权 = dev 模式放行。"""
    try:
        keys = _key_map()
    except _MalformedKeyMap as exc:
        # 配坏了 → 401, 【不是】dev 模式。见 _key_map 的说明。
        logger.error("%s", exc)
        raise AuthError(f"server auth misconfigured: {exc}") from exc

    single = os.environ.get("DESKCORE_API_KEY")

    if not keys and not single:
        return Caller(os.environ.get("DESKCORE_DEFAULT_USER_ID") or None,
                      name="dev", authenticated=False)

    if not provided:
        raise AuthError("missing X-Deskcore-Key")

    if keys:
        entry = keys.get(provided)
        if entry is None:
            raise AuthError("invalid X-Deskcore-Key")
        uid = (entry.get("user_id") or "").strip()
        if not uid:
            # 配置错误不能降级成"共享一切"。user_id 为空时, 下游按 user 过滤的
            # 查询(正负例/调校笔记)条件会失效 —— open_project 会把【所有人】的
            # 私有写作样本一起返回。宁可 401 让人当场发现。
            raise AuthError(
                f"DESKCORE_KEYS entry for {entry.get('name') or 'this key'} has no "
                "user_id; refusing to authenticate (a missing user_id would expose "
                "every user's private examples). Fix the key map.")
        return Caller(uid, name=entry.get("name") or "", authenticated=True)

    if provided != single:
        raise AuthError("invalid X-Deskcore-Key")
    default_uid = (os.environ.get("DESKCORE_DEFAULT_USER_ID") or "").strip()
    if not default_uid:
        raise AuthError(
            "DESKCORE_API_KEY is set but DESKCORE_DEFAULT_USER_ID is empty; "
            "refusing to authenticate (see above).")
    return Caller(default_uid, name="default", authenticated=True)
