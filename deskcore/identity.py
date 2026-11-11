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

两个都不设 = **拒绝所有请求(401)**。本地开发要免 key 跑, 显式设
``DESKCORE_ALLOW_ANONYMOUS=1`` —— 见 ``resolve`` 里那段的完整理由。

⚠️ user_id 要用 autowriter.projects.owner_id / items.user_id 里【已有的】
那个 UUID, 不要新造 —— 否则历史正负例读不到(RUNBOOK.md:150-153 记过一次:
写了 service account UUID 导致 RLS 屏蔽, list_example_items 永远 0 行,
飞轮静默断开)。
"""

from __future__ import annotations

import collections
import json
import logging
import os
import threading
from datetime import datetime, timezone

logger = logging.getLogger("deskcore")


class Caller:
    __slots__ = ("user_id", "name", "authenticated")

    def __init__(self, user_id: str | None, name: str = "", authenticated: bool = False):
        self.user_id = user_id
        self.name = name
        self.authenticated = authenticated

    def __repr__(self) -> str:  # pragma: no cover
        return f"Caller(name={self.name!r}, user_id={self.user_id!r})"


# 被拒的鉴权请求按原因计数(2026-11-11)。**固定的一组**: /health 不带鉴权, 计数的键不能由请求里的东西决定。
#   missing                 没带钥匙
#   invalid                 带了, 对不上, 形状上没看出毛病
#   invalid_whitespace      对不上, 而且首尾或中间有空白
#   invalid_quotes          对不上, 而且首尾带引号
#   invalid_ellipsis        对不上, 而且带省略号(… / ... / ⋯): 多半是把截短的示例原样贴了
#   invalid_bearer_prefix   对不上, 而且开头多了 "Bearer "(把 Authorization 的写法填进了 X-Deskcore-Key)
#   query_param             钥匙放在 ?key= 里(10-23 起不收)
#   misconfigured           服务端自己的钥匙配置有问题(没配 / 写坏 / 缺 user_id)
#   other                   上面都不是(留着兜底, 平时应当是 0)
AUTH_REJECT_REASONS = ("missing", "invalid", "invalid_whitespace", "invalid_quotes", "invalid_ellipsis",
                       "invalid_bearer_prefix", "query_param", "misconfigured", "other")


class AuthError(Exception):
    """鉴权失败。调用方应转成 401。

    ``reason`` 是 ``AUTH_REJECT_REASONS`` 里的一个(/health 的计数按它记); ``shape`` 只有「带了钥匙但对不上」时有:
    ``key_shape`` 回的那份, 说的只是**对方发来的那一串**长什么样。"""

    def __init__(self, message: str, *, reason: str = "other", shape: dict | None = None):
        super().__init__(message)
        self.reason = reason if reason in AUTH_REJECT_REASONS else "other"
        self.shape = shape


MISCONFIGURED_KEY_MAP = ("server auth misconfigured: 服务端的钥匙配置(DESKCORE_KEYS)解析不了, 所有请求都会 401; "
                         "哪里不对看 /health 的 auth.note 或服务端日志。")


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
    # ⚠️ 每个 value 也必须是对象。写成 {"k": "<uuid>"}(少写一层)是很自然的手误,
    # 而它是【合法 JSON】—— 于是这里放行, resolve() 和 auth_health() 后面对着
    # 一个字符串调 .get() 抛 AttributeError, 结果是 500 而不是 401 或一句能读懂
    # 的配置错误。/health 是 Railway 的健康检查路径, 它 500 = 部署起不来。
    # 配置错误必须在【解析这一步】就变成可读的 401, 不能漏到运行期。
    # (codex review)
    # 2026-11-11: 只报第几条(从 1 数), 不带钥匙的前几位 —— 这句话会原样进 401 和不带鉴权的 /health。
    bad = [i for i, v in enumerate(parsed.values(), 1) if not isinstance(v, dict)]
    if bad:
        raise _MalformedKeyMap(
            f"DESKCORE_KEYS 第 {bad[:5]} 条的值不是对象 —— "
            '每个 key 要写成 {"<key>": {"user_id": "<uuid>", "name": "..."}}')
    return parsed


def _unusable_key_positions(keys: dict) -> list[int]:
    """第几条(从 1 数)的 key 不可能被客户端原样发上来: 带省略号、空白或非 ASCII 可见字符。

    2026-11-03~11-04: 一份把钥匙截成 ``k-xxx-…`` 的示例被原样贴进了 Railway, 三个写稿写手的 key 全部对不上,
    写作台一天多没人连得上, 而 /health 一直报 "4 key(s)"。只报位置, 不报 key 和名字 —— /health 不带鉴权。
    """
    out = []
    for i, k in enumerate(keys, 1):
        if (not k or k.endswith("...")
                or any(not (33 <= ord(ch) <= 126) for ch in k)):
            out.append(i)
    return out


def auth_configured() -> bool:
    """鉴权是否已配置。配了但坏掉也算"已配"(会 401), 不算未配。"""
    if (os.environ.get("DESKCORE_KEYS") or "").strip():
        return True
    return bool(os.environ.get("DESKCORE_API_KEY"))


def anonymous_allowed() -> bool:
    """是否显式允许免 key 匿名访问(仅本地开发)。默认 False。"""
    return os.environ.get("DESKCORE_ALLOW_ANONYMOUS", "") in ("1", "true", "True")


def auth_health() -> tuple[bool, str]:
    """给 /health 用: 鉴权配置是否可用。配坏了要当场看得见。"""
    try:
        keys = _key_map()
    except _MalformedKeyMap as exc:
        return False, f"{exc} —— 所有请求都会 401, 服务实际不可用"
    if keys:
        # 只报第几条(从 1 数), 不带钥匙的前几位: /health 不带鉴权(2026-11-11)
        missing = [i for i, v in enumerate(keys.values(), 1)
                   if not str((v or {}).get("user_id") or "").strip()]
        if missing:
            return False, f"DESKCORE_KEYS 第 {missing} 条缺 user_id"
        unusable = _unusable_key_positions(keys)
        if unusable:
            return False, (
                f"DESKCORE_KEYS 第 {unusable} 条的钥匙看着是截短或占位的(带「…」/「...」、"
                "空白或非 ASCII 字符)—— 客户端发不出这样的请求头, 这几个人会一直 401。"
                "换成完整钥匙")
        return True, f"{len(keys)} key(s)"
    if os.environ.get("DESKCORE_API_KEY"):
        if not (os.environ.get("DESKCORE_DEFAULT_USER_ID") or "").strip():
            return False, "配了 DESKCORE_API_KEY 但 DESKCORE_DEFAULT_USER_ID 为空"
        return True, "single-key mode"
    if anonymous_allowed():
        return False, (
            "未配鉴权, 且 DESKCORE_ALLOW_ANONYMOUS=1 显式开了匿名放行 —— "
            "dev 模式。生产环境务必去掉这个变量并配 DESKCORE_KEYS"
        )
    return False, (
        "未配鉴权 —— 所有请求都会 401, 服务实际不可用。"
        "配 DESKCORE_KEYS(推荐)或 DESKCORE_API_KEY + DESKCORE_DEFAULT_USER_ID; "
        "本地开发要免 key 跑就设 DESKCORE_ALLOW_ANONYMOUS=1"
    )


# ══════════════════════════════════════════════════════════════════════
# 钥匙对不上时: 只说对方发来的那一串长什么样(2026-11-11)
# ══════════════════════════════════════════════════════════════════════
# 写内容的写手连不上, 看到的只有 "401 invalid X-Deskcore-Key", 谁也说不出错在哪: 是贴了截短的示例、多了一层引号、
# 把 Bearer 的写法填进了 X-Deskcore-Key, 还是拿的是旧钥匙。401 现在自己说: 收到的这一串多长、最后 4 位是什么、
# 看着有没有空格 / 引号 / 省略号 / 多出来的 Bearer / 非英文字符 / 示例占位。
#
# ⚠️ 这一段**只看对方发来的那一串**, 不碰服务端配的钥匙: 不说配了几把、不和哪一把比像不像、不提示「前几位对了」。
#    ``key_shape`` 的入参只有 ``provided``, 这条界线靠签名守着 —— 别往里传 key map。
# ⚠️ 回给调用方的最多带最后 4 位(而且整串不到 8 位时不带: 那等于把它原样念回去); 日志和 /health 里连最后 4 位都没有。
_ELLIPSES = ("…", "...", "⋯", "。。。")
_QUOTES = "\"'`“”‘’「」"
_PLACEHOLDER_MARKS = ("<", ">", "＜", "＞", "xxx", "XXX", "你的", "示例")   # 另加不分大小写的 your
_LAST4_MIN_LENGTH = 8
_SHAPE_FLAGS = ("whitespace_around", "whitespace_inside", "quotes", "ellipsis", "bearer_prefix", "non_ascii",
                "placeholder")
_FLAG_WORDS = {"whitespace_around": "首尾带空格", "whitespace_inside": "中间有空格", "quotes": "带引号",
               "ellipsis": "带省略号", "bearer_prefix": "开头多了 Bearer(这一栏只放钥匙本身)",
               "non_ascii": "有非英文字符",
               "placeholder": "像示例里的占位"}
# 先说贴在哪儿、再说怎么贴(复审 11-11): 这一句也会被对话里的模型转述, 光说「重新粘贴一遍」容易变成把钥匙贴进对话。
INVALID_KEY_HOW = ("请对照发给你的那把钥匙, 在客户端的写作台连接设置(mcp.json)里把整把钥匙重新粘贴一遍: 不带空格、不带省略号, "
                   "钥匙外面只留一对引号; 然后重启客户端、开一个新对话。")


def _as_typed(provided: str) -> str:
    """请求头到这里是按 latin-1 解出来的: 客户端按 UTF-8 发的「…」是三个字节, 变成三个怪字符。能按 UTF-8 还原就还原,
    长度和最后 4 位才和写手自己配置里看到的对得上; 还原不了(本来就是 latin-1, 或已经是正常的字符串)原样用。"""
    try:
        return provided.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return provided


def _visible(ch: str) -> str:
    if ch.isspace():
        return "␣"
    return ch if ch.isprintable() else "?"


def key_shape(provided: str) -> dict:
    """对方发来的那一串长什么样: 长度、最后 4 位(整串不到 8 位时是 None)、几个是 / 否。纯函数, 只看这一串。"""
    text = _as_typed(provided or "")
    core_ = text.strip()
    low = core_.lower()
    bearer = low == "bearer" or (low.startswith("bearer") and core_[6:7].isspace())
    rest = core_[6:].strip() if bearer else core_          # Bearer 后面那个空格不另算「中间有空格」
    return {
        "length": len(text),
        "last4": "".join(_visible(ch) for ch in text[-4:]) if len(text) >= _LAST4_MIN_LENGTH else None,
        "whitespace_around": core_ != text,
        "whitespace_inside": any(ch.isspace() for ch in rest),
        "quotes": bool(core_) and (core_[0] in _QUOTES or core_[-1] in _QUOTES),
        "ellipsis": any(e in text for e in _ELLIPSES),
        "bearer_prefix": bearer,
        "non_ascii": any(ord(ch) > 126 for ch in text),
        "placeholder": any(m in text for m in _PLACEHOLDER_MARKS) or "your" in low,
    }


def shape_flags(shape: dict | None) -> list[str]:
    """``key_shape`` 里是「是」的那几项的名字, 固定顺序。日志用它(日志里没有最后 4 位)。"""
    return [f for f in _SHAPE_FLAGS if (shape or {}).get(f)]


def _invalid_reason(shape: dict) -> str:
    """对不上的钥匙记在哪个原因下: 最可能就是病根的排前面(多出来的 Bearer、引号、省略号、空格), 都没有记 invalid。"""
    if shape["bearer_prefix"]:
        return "invalid_bearer_prefix"
    if shape["quotes"]:
        return "invalid_quotes"
    if shape["ellipsis"]:
        return "invalid_ellipsis"
    if shape["whitespace_around"] or shape["whitespace_inside"]:
        return "invalid_whitespace"
    return "invalid"


def invalid_key_detail(shape: dict) -> str:
    """401 回给调用方的那一句: 开头照旧是 ``invalid X-Deskcore-Key``(runbook 和排查脚本按它认), 后面只说形状和怎么办。"""
    tail = f", 最后 4 位「{shape['last4']}」" if shape.get("last4") else ""
    flags = [_FLAG_WORDS[f] for f in shape_flags(shape)]
    seen = ("; " + "、".join(flags)) if flags else "; 没看出空格、引号、省略号这类问题"
    return f"invalid X-Deskcore-Key: 服务器不认识收到的这把钥匙(长度 {shape['length']}{tail}{seen})。{INVALID_KEY_HOW}"


def _invalid_key(provided: str) -> AuthError:
    shape = key_shape(provided)
    return AuthError(invalid_key_detail(shape), reason=_invalid_reason(shape), shape=shape)


def _sendable(key: str) -> bool:
    """整串都是可见 ASCII(33~126): 客户端能原样放进请求头的钥匙就是这个样子(``_unusable_key_positions`` 同一条线)。"""
    return bool(key) and all(33 <= ord(ch) <= 126 for ch in key)


def _lookup_forms(provided: str) -> list[str]:
    """拿去查的写法: 原样, 再加去掉首尾空白的(和原样不一样、而且去完整串是可见 ASCII 时)。

    为什么可以去首尾空白: 钥匙走的是 HTTP 头, 头的值首尾的空白按规范不算值的一部分(多数服务器在到这里之前就去掉了
    空格和 tab; 不间断空格、全角空格这类它们不管, 复制粘贴时又最容易带上)。去掉之后**整把**对上了配的钥匙, 说明对方手里
    就是这把, 放行不多给任何人东西。原样那一份先查, 所以原来能过的一个不少。
    空白有两种到法: 直接是那个字符, 或客户端按 UTF-8 发、到这里按 latin-1 解成了两三个怪字符(``_as_typed`` 还原的那种)
    —— 所以去空白的有两份: 原样去的, 还原之后去的。
    ⚠️ 去完之后**整串是可见 ASCII** 才拿去查(复审 11-11)。还原那一步本身不能多放行任何东西: 不加这一条, 配置里一把
    带「…」的钥匙(11-03 那种把截短的示例 ``k-xxx-…`` 原样贴进 DESKCORE_KEYS 的)就从「客户端发不出来、一直 401」变成
    「谁照着示例发谁就过」—— 那一串不是秘密。UTF-8 里 0x80 以上的字节还原不出 ASCII, 所以加了这一条之后, 多出来的两份
    和原样相比只可能少了首尾的空白。
    只去首尾: 中间的空格、引号、省略号、多出来的 Bearer 都不动 —— 那些改了就是在替对方猜钥匙, 照实 401 并说出来。
    去完是空的不查: 空串不是钥匙。"""
    forms = [provided]
    for cand in (provided.strip(), _as_typed(provided).strip()):
        if _sendable(cand) and cand not in forms:
            forms.append(cand)
    return forms


# ── 被拒的请求: 计数 + 一行日志 ──────────────────────────────────────────────────────────────────
_REJECTIONS: collections.Counter = collections.Counter()
_REJECTIONS_LOCK = threading.Lock()
_LAST_REJECTION: dict = {"at": None}


def record_rejection(exc: AuthError, *, path: str = "", via: str = "") -> None:
    """HTTP 层每拒一次调一次: 按原因记一个数, 记一行 WARNING(Railway 日志里能看到)。自己不抛。

    日志里只有原因、路径(不带查询串)、钥匙是从哪儿传的、长度和那几个是 / 否 —— **没有钥匙, 也没有最后 4 位**:
    日志留得比一次 401 久, 看得到的人也多。"""
    try:
        reason = getattr(exc, "reason", "other")
        reason = reason if reason in AUTH_REJECT_REASONS else "other"
        with _REJECTIONS_LOCK:
            _REJECTIONS[reason] += 1
            _LAST_REJECTION["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        shape = getattr(exc, "shape", None)
        where = "".join(ch for ch in str(path)[:80] if ch.isprintable() and not ch.isspace()) or "-"
        logger.warning("auth rejected: reason=%s path=%s via=%s len=%s flags=%s",
                       reason, where, via or "-", shape["length"] if shape else "-",
                       ",".join(shape_flags(shape)) or "-")
    except Exception:  # noqa: BLE001 — 留痕不能把 401 变成 500
        logger.warning("auth rejected (the rejection could not be recorded)")


def auth_rejections() -> dict:
    """给 /health 用: 进程启动以来被拒的鉴权请求, 按原因数, 外加最后一次是什么时候(UTC)。

    ⚠️ /health 不带鉴权: 这里只有固定的几个原因名、个数和一个时间, 没有钥匙的任何一部分, 没有路径, 没有是谁。"""
    with _REJECTIONS_LOCK:
        return {"since_start": {r: _REJECTIONS.get(r, 0) for r in AUTH_REJECT_REASONS},
                "last_at": _LAST_REJECTION["at"]}


def resolve(provided: str | None) -> Caller:
    """把请求里的 key 解析成 Caller。未配鉴权 = dev 模式放行。"""
    try:
        keys = _key_map()
    except _MalformedKeyMap as exc:
        # 配坏了 → 401, 【不是】dev 模式。见 _key_map 的说明。
        # 2026-11-11: 回给调用方的是固定的一句 —— 哪里不对(第几条、JSON 错在第几个字符)只进日志和 /health 的 auth.note,
        # 401 里不说服务端的钥匙配置长什么样(和钥匙对不上那一句同一条线)。
        logger.error("%s", exc)
        raise AuthError(MISCONFIGURED_KEY_MAP, reason="misconfigured") from exc

    single = os.environ.get("DESKCORE_API_KEY")

    if not keys and not single:
        # ⚠️ 这里【曾经是无条件放行的】(审计 ROB-003)。deskcore 持
        # service_role 绕 RLS, 部署时漏配 key 就等于把全部租户的项目数据和
        # 全部工具(含写)匿名开放到公网。
        #
        # 而 /health 发现不了: 它虽然会把 auth.ok 报成 false, 但函数正常返回
        # → HTTP 200, 而 Railway 的 healthcheck 只看状态码 —— 一个彻底敞开的
        # 部署照样判定为健康、照样上线。"配错就全开"加上"健康检查看不见",
        # 两件事叠起来才是真正危险的地方。
        #
        # 现在改成 fail-closed: 没配就 401。免 key 跑必须【显式】声明意图,
        # 与 _key_map() 对"配了但解析失败"的处理同一口径 —— 配置错误绝不
        # 降级成放行。
        if not anonymous_allowed():
            raise AuthError(
                "server auth not configured: 未配 DESKCORE_KEYS / DESKCORE_API_KEY。"
                "deskcore 持 service_role 绕 RLS, 匿名放行等于开放全部租户数据, "
                "因此默认拒绝。本地开发请显式设 DESKCORE_ALLOW_ANONYMOUS=1。",
                reason="misconfigured",
            )
        logger.warning(
            "DESKCORE_ALLOW_ANONYMOUS=1 —— 匿名放行已开启, 仅限本地开发; "
            "生产环境请配 DESKCORE_KEYS 并去掉这个变量")
        return Caller(os.environ.get("DESKCORE_DEFAULT_USER_ID") or None,
                      name="dev", authenticated=False)

    if not provided:
        raise AuthError("missing X-Deskcore-Key", reason="missing")

    forms = _lookup_forms(provided)          # 原样, 再加去掉首尾空白的一份(理由见 _lookup_forms)
    if keys:
        entry = next((keys[f] for f in forms if f in keys), None)
        if entry is None:
            raise _invalid_key(provided)
        uid = (entry.get("user_id") or "").strip()
        if not uid:
            # 配置错误不能降级成"共享一切"。user_id 为空时, 下游按 user 过滤的
            # 查询(正负例/调校笔记)条件会失效 —— open_project 会把【所有人】的
            # 私有写作样本一起返回。宁可 401 让人当场发现。
            raise AuthError(
                f"DESKCORE_KEYS entry for {entry.get('name') or 'this key'} has no "
                "user_id; refusing to authenticate (a missing user_id would expose "
                "every user's private examples). Fix the key map.", reason="misconfigured")
        return Caller(uid, name=entry.get("name") or "", authenticated=True)

    if single not in forms:
        raise _invalid_key(provided)
    default_uid = (os.environ.get("DESKCORE_DEFAULT_USER_ID") or "").strip()
    if not default_uid:
        raise AuthError(
            "DESKCORE_API_KEY is set but DESKCORE_DEFAULT_USER_ID is empty; "
            "refusing to authenticate (see above).", reason="misconfigured")
    return Caller(default_uid, name="default", authenticated=True)
