"""deskcore/clientmark.py — 每次工具调用、每个新 batch 记下是从哪个客户端来的(2026-11-17, docs/deskcore.md §3.37)。

为什么: 写手现在从两条路进写作台 —— WorkBuddy 里的 bywood-writing-desk skill、DeepSeek Harness 桌面的写作台插件
(clients/dsh-plugin, 经 MCP 挂写作台)。原来哪儿都没记一次调用、一批稿子是从哪条路来的: 校准、存档率、借卡次数、曝光、
改稿率、人感这些读数里两边的稿子混在一起分不开。

怎么做: 只看服务端本来就收得到的东西, 插件不用发版、skill 不用重装 ——
  · 这一次请求的 User-Agent(只取第一个像样的产品名 + 版本, 洗成 ``[a-z0-9_.]``, 最长 52 个字符)、有没有 Origin 头;
  · 走的是 MCP 还是 REST;
  · MCP ``initialize`` 里客户端自报的 ``clientInfo``。服务端是无状态挂的 MCP(app.py 的 ``stateless_http=True``): 每个请求
    一个新会话, ``clientInfo`` 只在 initialize 那一个请求里有, ``tools/call`` 上没有, 也没有会话 id 可对。所以 initialize 那次
    把它记进进程内存里的一个小表(``_HELLO``, 24 小时、最多 512 项), 键(指纹)是「这个调用者 + 这把钥匙的备注名 + UA 那个词 +
    有没有 Origin + 协议版本」(协议版本: initialize 时谈定的那一版 ↔ 之后每个请求的 ``MCP-Protocol-Version`` 头), 之后同一个
    指纹的工具调用拿来用。同一个指纹上先后来了两个名字不一样的 clientInfo(认得不认得都算)就记成说不清, 哪个都不用。
    ⚠️ 指纹只是「像同一个连接」: 同一个指纹上一个**从不 initialize** 的客户端(部署之后一直在调、没重连的)服务端看不见,
       它的调用会算到这个指纹最近一次 initialize 的那个客户端头上。``basis = hello`` 读作「这个指纹最近一次 initialize 报的是它」,
       不是「这一次一定是它」; 要分干净就给另一个客户端单发一把钥匙(备注名在指纹里)。见 docs/deskcore.md §3.37。
算出来的是一个固定词表里的词(``APPS``)加一个「凭什么这么定」(``BASES``)。调用留痕那一行和存档那一行(都是 ``batch_metrics.meta``,
只有负责人的 SQL 读)的 ``meta.client_mark`` 另带洗过的证据(``ua`` / ``origin`` / ``hello``); 新 batch 的 ``params.client_mark`` 只有
``app`` / ``basis`` 两个词(``for_batch``)—— batches 是很多地方读的长命行, 证据不进那里。

⚠️ 原始的请求头、钥匙、客户端报上来的原串一律不存、不进日志、不上 /health。这里只出固定的词和洗过的短串; ``Mark.as_dict``
   出门前再自己核一遍。
⚠️ 绝不抛、绝不等: 认不出就是 ``unknown``。没有 I/O: 请求头过一遍(各种头只取第一个, 钥匙也只认第一个 ——
   同 app._extract_key), UA 只看前 512 个字符, 一次字典查找; MCP 请求体里有 ``"initialize"`` 字样、不超过 64 KiB 的才解析一次
   JSON(SDK 自己也要解析一遍)。真出了意外只记一行 WARNING(只带异常类型, 5 分钟最多一行)。
⚠️ 只用标准库: CI 只装主依赖就 import ``deskcore.store`` / ``deskcore.core``, 这里不许 import fastapi / starlette / mcp。

DSH 和 WorkBuddy 实际发什么 UA、clientInfo, 离线核不了(第三方闭源)。规则是假设, 上线后按 §3.37 的核对步骤看一次;
不对就改 ``app_for`` 一行, 历史按存下的 ``ua`` / ``hello`` 用 SQL 的 CASE 重读, 不回写库。
"""

from __future__ import annotations

import collections
import contextlib
import contextvars
import json
import logging
import re
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger("deskcore.clientmark")

KEY = "client_mark"                         # params / meta 里的那个键

# 存的词只有这几个(固定词表)。
DSH_PLUGIN = "dsh_plugin"                   # DeepSeek Harness 桌面 + 写作台插件
WORKBUDDY_SKILL = "workbuddy_skill"         # WorkBuddy / CodeBuddy(都装 bywood-writing-desk skill)
CLAUDE_CODE = "claude_code"                 # Claude Code(负责人自测)
OTHER_MCP = "other_mcp"                     # 走 MCP、哪条规则都没认出来的
REST = "rest"                               # 走 REST、哪条规则都没认出来的(curl、脚本、CI)
SERVER_JOB = "server_job"                   # 没有 HTTP 调用方: python -m deskcore.cli 的命令(含每晚 tv-sync)
UNKNOWN_APP = "unknown"                     # 认不出(出了意外、请求和 CLI 之外的服务端调用、底稿转正的 batch)
APPS = (DSH_PLUGIN, WORKBUDDY_SKILL, CLAUDE_CODE, OTHER_MCP, REST, SERVER_JOB, UNKNOWN_APP)

# 凭什么这么定
BASES = ("user_agent", "hello", "transport", "process", "none")

UA_MAX_IN = 512                             # UA / clientInfo 最多看这么多个字符(找钥匙也只在这一段里找)
NAME_MAX, VER_MAX = 32, 16                  # 洗过的产品名 / 版本的上限; 拼起来 name_v<ver> 最长 50
HELLO_BODY_MAX = 64 * 1024                  # initialize 的请求体超过这么大就不解析
HELLO_TTL_SEC = 24 * 3600.0
HELLO_MAX = 512
WARN_EVERY_SEC = 300.0

# 浏览器 UA 里那些人人都带的词: 跳过它们找真正的产品名
_GENERIC = frozenset({"mozilla", "applewebkit", "khtml", "like", "gecko", "chrome", "chromium", "safari", "version",
                      "mobile", "firefox", "edg", "edge", "opr", "headlesschrome"})
_TCHAR = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")     # RFC 9110 的 token 字符
_NAME_BAD = re.compile(r"[^a-z0-9]+")
_VER_BAD = re.compile(r"[^0-9a-z._]+")
_SAFE = re.compile(r"[a-z0-9_.]{1,52}")                  # 出门的串只许这样(calllog._WORD 的子集: 不带连字符)
_DSH = re.compile(r"(^|_)dsh(_|$)")
# 协议版本: 2025-06-18 起客户端在 initialize 之后的每个请求上都要带 MCP-Protocol-Version 头; 更早的版本不要求, 不带时
# SDK 当它是 2025-03-26(mcp 的 DEFAULT_NEGOTIATED_VERSION)。指纹里那一格见 _pv_slot。
PV_HEADER_SINCE = "2025-06-18"
_PV_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


# ── 洗串 ────────────────────────────────────────────────────────────────────────────────────────────
def _san_name(raw: str) -> str:
    """产品名: 小写, 不是 ``[a-z0-9]`` 的一段换成一个 ``_``, 去掉两头的 ``_``, 至多 32 个字符。"""
    return _NAME_BAD.sub("_", raw.lower()).strip("_")[:NAME_MAX].strip("_")


def _san_ver(raw: str | None) -> str:
    """版本: 小写, ``-`` 换成 ``_``, 只留 ``[0-9a-z._]``, 至多 16 个字符, 去掉两头的点和下划线。"""
    if not isinstance(raw, str):
        return ""
    return _VER_BAD.sub("", raw.lower().replace("-", "_"))[:VER_MAX].strip("._")


def _compose(name: str, ver: str) -> str:
    return f"{name}_v{ver}" if ver else name


def _keys(key) -> list[str]:
    """这次请求带的钥匙(可能是一个串、几个串或没有), 只留非空的, 只在内存里用来核「输出里有没有它」。"""
    if isinstance(key, str):
        key = [key]
    if not isinstance(key, (list, tuple)):
        return []
    return [k.strip() for k in key if isinstance(k, str) and k.strip()]


def _leaks(raw: str, out: str, keys: list[str]) -> bool:
    """原串里有钥匙(不分大小写), 或者洗出来的串里有洗过的钥匙的任意连续 8 个字符(钥匙不到 8 个字符就是整把; 产品名和版本
    两种洗法都比): 都算漏。调用方给的原串至多 512 个字符(输出只从这一段来), 钥匙至多两把(header_signals 只取第一个),
    洗出来的串至多 52 个字符: 微秒级。"""
    low = raw.lower()
    for k in keys:
        kl = k.lower()
        if kl in low:
            return True
        for sk in {_NAME_BAD.sub("_", kl).strip("_"), _VER_BAD.sub("", kl.replace("-", "_")).strip("._")}:
            if not sk:
                continue
            w = min(len(sk), 8)
            if any(sk[i:i + w] in out for i in range(len(sk) - w + 1)):
                return True
    return False


def _strip_comments(s: str) -> str:
    """去掉 UA 里括号括着的注释(数着层数)。"""
    out, depth = [], 0
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def _ua_parts(user_agent, keys=None) -> tuple[str, str | None]:
    """UA → (洗过的那个词, 定这个词的产品名 —— 套规则用的; 不是某个产品时是 None)。见 ``ua_descriptor``。"""
    if not isinstance(user_agent, str) or not user_agent.strip():
        return "none", None
    keys = _keys(keys)
    head = user_agent[:UA_MAX_IN]               # 输出只从这一段来, 找钥匙也只在这一段里找(只转一次小写)
    if keys:
        low = head.lower()
        if any(k.lower() in low for k in keys):
            return "other", None                 # 钥匙进了 UA: 什么都不认(防御用, 不该发生)
    text = _strip_comments(head)
    tokens: list[tuple[str, str]] = []
    for part in text.split():
        name, _, ver = part.partition("/")
        if not _TCHAR.fullmatch(name) or (ver and not _TCHAR.fullmatch(ver)):
            continue                             # 引号、非 ASCII、省略号 …… 不是 token 的整段丢掉
        n = _san_name(name)
        if n:
            tokens.append((n, _san_ver(ver)))
    if not tokens:
        return "other", None
    pick = next(((n, v) for n, v in tokens if n not in _GENERIC and n != "electron"), None) \
        or next(((n, v) for n, v in tokens if n == "electron"), None)
    if pick is None:
        return "browser", None
    desc = _compose(*pick)
    if keys and _leaks(head, desc, keys):
        return "other", None
    return desc, pick[0]


def ua_descriptor(user_agent, *, key=None) -> str:
    """User-Agent → 一个洗过的词: 没有 / 空 → ``none``; 第一个不是浏览器常见词(也不是 ``electron``)的产品 token →
    ``name`` 或 ``name_v<版本>``(Electron 排在最后: 产品 token 写在 ``Electron/…`` 后面也认产品); 只有 Electron →
    ``electron_v…``; 只有浏览器常见词 → ``browser``; 一个像样的 token 都没有, 或者这次请求的钥匙在里面 → ``other``。
    至多看 512 个字符(钥匙也只在这一段里找); 结果只含 ``[a-z0-9_.]``、至多 52 个字符。"""
    return _ua_parts(user_agent, key)[0]


def hello_descriptor(name, version=None, *, key=None) -> str | None:
    """MCP initialize 的 ``clientInfo.name`` / ``version`` → 同一种洗法的词; 名字洗完是空的 → ``other``。名字或版本里有这次
    请求的钥匙 → None(不记)。"""
    if not isinstance(name, str) or not name.strip():
        return None
    name = name[:UA_MAX_IN]
    version = version[:UA_MAX_IN] if isinstance(version, str) else None
    keys = _keys(key)
    n = _san_name(name)
    out = _compose(n, _san_ver(version)) if n else "other"
    if keys and (_leaks(name, out, keys) or (version and _leaks(version, out, keys))):
        return None
    return out


def app_for(name) -> str | None:
    """洗过的产品名(不是拼了版本的那个词)→ 认得的客户端; 认不出 None。
      · ``dsh`` 作为一个完整的段(``dsh``、``dsh_mcp_client``、``deepseek_ai_dsh_mcp_client``), 或含 ``deepseek_harness`` /
        ``deepseekharness``(UA 的产品 token 常连写: ``DeepSeekHarness/0.2``) → dsh_plugin(``dashboard``、``fdsh_tool`` 不算;
        光是 ``deepseek`` 也不算: 那也是模型家族和 API 客户端的名字);
      · 含 ``workbuddy`` / ``codebuddy`` → workbuddy_skill;
      · 含 ``claude_code`` / ``claudecode`` → claude_code。"""
    if not isinstance(name, str) or not name:
        return None
    if _DSH.search(name) or "deepseek_harness" in name or "deepseekharness" in name:
        return DSH_PLUGIN
    if "workbuddy" in name or "codebuddy" in name:
        return WORKBUDDY_SKILL
    if "claude_code" in name or "claudecode" in name:
        return CLAUDE_CODE
    return None


# ── 结果 ────────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Mark:
    app: str = UNKNOWN_APP
    basis: str = "none"
    ua: str | None = None                       # 洗过的 UA 词(HTTP 请求才有)
    origin: bool | None = None                  # 有没有 Origin 头(HTTP 请求才有)
    hello: str | None = None                    # 洗过的 clientInfo(这个指纹最近一次 initialize 报的、而且没撞车才有)

    def as_dict(self) -> dict:
        """要存的那一小块。出门前自己再核一遍: 词不在词表里就整块当 unknown, 串不合规矩就丢掉那个键。绝不抛。"""
        try:
            if self.app not in APPS or self.basis not in BASES:
                return {"app": UNKNOWN_APP, "basis": "none"}
            out: dict = {"app": self.app, "basis": self.basis}
            if isinstance(self.ua, str) and _SAFE.fullmatch(self.ua):
                out["ua"] = self.ua
            if isinstance(self.origin, bool):
                out["origin"] = self.origin
            if isinstance(self.hello, str) and _SAFE.fullmatch(self.hello):
                out["hello"] = self.hello
            return out
        except Exception:                       # noqa: BLE001
            return {"app": UNKNOWN_APP, "basis": "none"}


UNKNOWN = Mark()


# ── 出了意外: 一行 WARNING, 5 分钟最多一行, 只带异常类型 ──────────────────────────────────────────────
_WARN_LOCK = threading.Lock()
_last_warn: list = [None]


def _clock() -> float:
    return time.monotonic()


def _warn(exc: BaseException) -> None:
    try:
        now = _clock()
        with _WARN_LOCK:
            last = _last_warn[0]
            if last is not None and now - last < WARN_EVERY_SEC:
                return
            _last_warn[0] = now
        logger.warning("client mark: classification failed (%s)", type(exc).__name__)
    except Exception:                           # noqa: BLE001
        pass


# ── clientInfo 的小表(一个连接 initialize 一次, 之后的工具调用拿来用) ───────────────────────────────────
@dataclass(frozen=True)
class _Hello:
    app: str | None
    hello: str | None
    name: str                                   # 洗过的 clientInfo 名字(不带版本): 撞车按它比
    expires: float
    ambiguous: bool = False


_HELLO: "collections.OrderedDict[tuple, _Hello]" = collections.OrderedDict()
_HELLO_LOCK = threading.Lock()


def _pv_slot(v) -> str:
    """协议版本 → 指纹里的那一格。2025-06-18 起的版本是它自己(这些版本的客户端每个请求都带头, 两边对得上); 更早的、没带头的
    都是 ``legacy``(那几版不要求带头, 不带时 SDK 当 2025-03-26); 不像日期的是 ``other``。只在进程内存里。"""
    if v is None:
        return "legacy"
    if not isinstance(v, str) or not _PV_DATE.fullmatch(v):
        return "other"
    return v if v >= PV_HEADER_SINCE else "legacy"


def negotiated_version(requested, supported=None, latest=None):
    """initialize 时客户端要的协议版本 → 服务端回给它、它之后每个请求头上带的那一版。mcp SDK 的规矩(server/session.py):
    要的在支持列表里就是它, 不在就是最新的那一版。``supported`` / ``latest`` 由 app 从 mcp 取了传进来(这里不 import mcp);
    不给(单测)就当是要的那一版。"""
    if isinstance(requested, str) and supported and isinstance(latest, str) and requested not in supported:
        return latest
    return requested


def _fp(caller, ua_desc: str, origin, pv_slot: str) -> tuple:
    """「像同一个客户端连接」的指纹: (调用者 user_id, 钥匙的备注名, UA 那个词, 有没有 Origin, 协议版本那一格)。只在进程内存里,
    不存不记。⚠️ 只是像: 同一个指纹上从不 initialize 的客户端看不见(见模块说明)。"""
    return (getattr(caller, "user_id", None), getattr(caller, "name", None), ua_desc, bool(origin), pv_slot)


def _hello_get(fp: tuple) -> _Hello | None:
    now = _clock()
    with _HELLO_LOCK:
        e = _HELLO.get(fp)
        if e is None:
            return None
        if e.expires <= now:
            del _HELLO[fp]
            return None
        return e


def _hello_put(fp: tuple, app: str | None, hello: str, name: str) -> None:
    """记一次 initialize。同一个指纹先前记着另一个名字的客户端(认得不认得都算: 两个认不得的也不能互相顶替 ``hello`` 这份证据)
    → 记成说不清, 到期之前哪个都不用; 同一个名字换了版本 → 换成新的那个词。最多 512 项, 满了先丢最早的。"""
    now = _clock()
    with _HELLO_LOCK:
        e = _HELLO.get(fp)
        if e is not None and e.expires > now:
            if e.ambiguous:
                return                          # 说不清的到期之前一直说不清
            if e.name != name:
                _HELLO[fp] = _Hello(None, None, "", now + HELLO_TTL_SEC, True)
                _HELLO.move_to_end(fp)
                return
        _HELLO[fp] = _Hello(app, hello, name, now + HELLO_TTL_SEC)
        _HELLO.move_to_end(fp)
        while len(_HELLO) > HELLO_MAX:
            _HELLO.popitem(last=False)


def note_hello(body, *, ua=None, origin=False, key=None, caller=None, supported=None, latest=None) -> None:
    """MCP 的请求体: 是 initialize(单条或一批里的一条)就把 ``params.clientInfo`` 记进小表, 指纹里的协议版本按
    ``params.protocolVersion`` 谈定的那一版算(``negotiated_version``)。只看带 ``"initialize"`` 字样、不超过 64 KiB 的请求体;
    解不出来、不是 initialize、没有 clientInfo、没有字符串的 protocolVersion(SDK 会拒这个 initialize)都什么也不做。绝不抛。"""
    try:
        if not isinstance(body, (bytes, bytearray)) or len(body) > HELLO_BODY_MAX or b'"initialize"' not in body:
            return
        try:
            msg = json.loads(bytes(body))
        except (ValueError, RecursionError):
            return
        for m in (msg if isinstance(msg, list) else [msg])[:16]:
            if not isinstance(m, dict) or m.get("method") != "initialize":
                continue
            params = m.get("params")
            info = params.get("clientInfo") if isinstance(params, dict) else None
            if not isinstance(info, dict) or not isinstance(params.get("protocolVersion"), str):
                continue
            name, version = info.get("name"), info.get("version")
            keys = _keys(key)
            hello = hello_descriptor(name, version if isinstance(version, str) else None, key=keys)
            if hello is None:
                return
            ua_desc, _ = _ua_parts(ua, keys)
            pv = _pv_slot(negotiated_version(params["protocolVersion"], supported, latest))
            n = _san_name(name[:UA_MAX_IN]) or "other"
            _hello_put(_fp(caller, ua_desc, origin, pv), app_for(n), hello, n)
            return
    except Exception as exc:                    # noqa: BLE001
        _warn(exc)


# ── 定这一次 ────────────────────────────────────────────────────────────────────────────────────────
def classify(*, channel: str, ua=None, origin=False, key=None, caller=None, pv=None) -> Mark:
    """这一次 HTTP 请求是哪个客户端。先后:
      1. UA 的产品名认得 → 那个客户端(``user_agent``);
      2. 走 MCP, 这个指纹(``pv`` 是这次请求的 ``MCP-Protocol-Version`` 头)最近一次 initialize 报过、没撞车、认得 → 那个客户端
         (``hello``);
      3. 走 MCP → ``other_mcp``; 走 REST → ``rest``(``transport``);
    出了意外 → ``unknown`` / ``none``。``ua`` / ``origin`` 总是带上; ``hello`` 只要小表里有(没撞车)就带上, UA 定的也带。
    REST 不查小表。绝不抛。"""
    try:
        keys = _keys(key)
        origin = bool(origin)
        ua_desc, ua_name = _ua_parts(ua, keys)
        entry = _hello_get(_fp(caller, ua_desc, origin, _pv_slot(pv))) if channel == "mcp" else None
        if entry is not None and entry.ambiguous:
            entry = None
        hello = entry.hello if entry is not None else None
        app = app_for(ua_name)
        if app:
            return Mark(app, "user_agent", ua_desc, origin, hello)
        if entry is not None and entry.app:
            return Mark(entry.app, "hello", ua_desc, origin, hello)
        if channel == "mcp":
            return Mark(OTHER_MCP, "transport", ua_desc, origin, hello)
        if channel == "rest":
            return Mark(REST, "transport", ua_desc, origin, None)
        return Mark(UNKNOWN_APP, "none", ua_desc, origin, None)
    except Exception as exc:                    # noqa: BLE001
        _warn(exc)
        return UNKNOWN


def header_signals(headers) -> tuple[str | None, bool, list[str], str | None]:
    """ASGI 的 ``scope["headers"]``(一串 (bytes, bytes))→ (User-Agent, 有没有 Origin, 这次带的钥匙, MCP-Protocol-Version)。
    每种头只取第一个: 鉴权也只认第一个 ``X-Deskcore-Key`` / 第一个 ``Authorization``(Starlette 的 ``headers.get``), 后面重复的
    不碰 —— 一千个重复的钥匙头不会让这里多干一千遍活。钥匙只在内存里拿来核输出, 不存不记; 这里不数钥匙的传法
    (那是 app._extract_key 的事)。"""
    ua = key = auth = pv = None
    origin = False
    for k, v in headers or ():
        name = bytes(k).lower()
        if name == b"user-agent":
            if ua is None:
                ua = bytes(v).decode("latin-1")
        elif name == b"origin":
            origin = True
        elif name == b"x-deskcore-key":
            if key is None:
                key = bytes(v).decode("latin-1")
        elif name == b"authorization":
            if auth is None:
                auth = bytes(v).decode("latin-1")
        elif name == b"mcp-protocol-version":
            if pv is None:
                pv = bytes(v).decode("latin-1")
    keys = [key] if key is not None else []
    if auth is not None and auth[:7].lower() == "bearer ":
        keys.append(auth[7:])
    return ua, origin, _keys(keys), pv


def classify_headers(channel: str, headers, caller) -> Mark:
    """``classify`` 的 ASGI 版本: 从 ``scope["headers"]`` 取信号。绝不抛。"""
    try:
        ua, origin, keys, pv = header_signals(headers)
        return classify(channel=channel, ua=ua, origin=origin, key=keys, caller=caller, pv=pv)
    except Exception as exc:                    # noqa: BLE001
        _warn(exc)
        return UNKNOWN


def note_hello_headers(body, headers, caller, *, supported=None, latest=None) -> None:
    """``note_hello`` 的 ASGI 版本(``supported`` / ``latest``: 服务端支持的协议版本和最新的那一版, 见 ``negotiated_version``)。
    绝不抛。"""
    try:
        if not isinstance(body, (bytes, bytearray)) or len(body) > HELLO_BODY_MAX or b'"initialize"' not in body:
            return
        ua, origin, keys, _ = header_signals(headers)
        note_hello(body, ua=ua, origin=origin, key=keys, caller=caller, supported=supported, latest=latest)
    except Exception as exc:                    # noqa: BLE001
        _warn(exc)


# ── 这一次调用的那个词: 请求里绑在 contextvar 上, CLI 里是进程级的默认 ───────────────────────────────────
# contextvar: REST 的 run_in_threadpool、MCP SDK 起的处理任务都会把它带过去(同 app._caller)。
# 进程级默认(不是 contextvar): CLI 的 tv-sync 起的普通线程 / 线程池拿不到 contextvar; ``process_default`` 退出时还原,
# 用例之间串不了。
_BOUND: contextvars.ContextVar = contextvars.ContextVar("deskcore_client_mark", default=None)
_process_default: list = [None]


def bind(mark) -> contextvars.Token | None:
    try:
        return _BOUND.set(mark if isinstance(mark, Mark) else UNKNOWN)
    except Exception:                           # noqa: BLE001
        return None


def unbind(token) -> None:
    if token is None:
        return
    try:
        _BOUND.reset(token)
    except Exception:                           # noqa: BLE001
        pass


@contextlib.contextmanager
def process_default(app: str = SERVER_JOB):
    """这一段里没有绑定的地方都算 ``app``(CLI 用 ``server_job`` / ``process``); 退出时还原成原来的。"""
    prev = _process_default[0]
    _process_default[0] = Mark(app, "process") if app in APPS else UNKNOWN
    try:
        yield
    finally:
        _process_default[0] = prev


def current() -> Mark:
    """这会儿算哪个客户端: 绑着的那个 → 进程级默认 → unknown。绝不抛。"""
    try:
        m = _BOUND.get()
        if isinstance(m, Mark):
            return m
        d = _process_default[0]
        if isinstance(d, Mark):
            return d
    except Exception:                           # noqa: BLE001
        pass
    return UNKNOWN


def for_batch() -> dict:
    """新 batch 的 ``params.client_mark``: 只有 ``app`` / ``basis`` 两个固定的词。洗过的证据(``ua`` / ``origin`` / ``hello``)
    只进 ``batch_metrics.meta``(调用留痕那一行、存档那一行): batches 是很多地方读的长命行, 不放客户端报上来的串。
    绝不抛: 出了意外是 ``{"app": "unknown", "basis": "none"}``。"""
    try:
        d = current().as_dict()
        return {"app": d["app"], "basis": d["basis"]}
    except Exception:                           # noqa: BLE001
        return {"app": UNKNOWN_APP, "basis": "none"}


def reset() -> None:
    """测试用: 清空 clientInfo 小表和日志限频。"""
    with _HELLO_LOCK:
        _HELLO.clear()
    with _WARN_LOCK:
        _last_warn[0] = None
