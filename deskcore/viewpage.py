"""deskcore/viewpage.py — 一批稿子排好版的页面(2026-11-12)。

写手那边要的是一张排好版的页面: 一篇一行, 左边笔记(小标签、【标题】、分好段的正文、话题单独一行), 右边这篇的评论。
让写手的模型每次自己写 HTML 太慢; 版式又是每批都一样的东西 —— 所以页面由服务端拿已经存下的稿子拼, 回包里带一个链接
(``GET /v/{令牌}``, deskcore/app.py), 写手那边的模型一个字不用多写。改稿重存之后刷新页面, 看到的就是现在那一版。
一次交付分几组存的(一次最多存 10 篇), 后一组拿到的链接把前面几组一起带上, 一页上连着编号(core._view_session_batches)。

这个模块是纯函数, 不连库、不联网(同 delivery.py / isolation.py):

  · 令牌: 不落库, 自己带着 1~4 个 batch 的 id 和到期时间, HMAC-SHA256 签名(截到 16 字节), 有版本号。验签是常数时间比较。
    签名用的钥匙: 配了 ``DESKCORE_VIEW_SECRET`` 用它, 没配就从服务本来就必须有的 ``SUPABASE_SERVICE_ROLE_KEY`` 派生
    (HMAC-SHA256 + 固定标签; 派生是单向的, 令牌里看不出原来那把)。选它不选 ``DESKCORE_KEYS``: 后者每加减一个写手就变,
    发出去的链接会跟着全部失效。⚠️ 轮换 service_role 的那一次, 没到期的链接也一起失效(写手再存一次就有新的)。
  · 对外的地址: ``DESKCORE_PUBLIC_URL`` → ``https://`` + ``RAILWAY_PUBLIC_DOMAIN`` → 这次请求自己的地址(https 或本机,
    Host 过了 app 的白名单, 由 app.py 判好放进 ``REQUEST_BASE``) → 都没有就不发链接。
  · 总开关: ``DESKCORE_VIEW_PAGE=0`` —— 不发链接, 已经发出去的也打不开。
  · ``render``: 一份自带样式的 HTML。库里来的每一个字都转义; 样式和那一小段复制用的脚本都内联、带这次响应的 nonce;
    不向任何外部地址发请求; 不开 JavaScript 也能看(只是没有复制按钮)。

⚠️ 令牌和整条链接**不进日志、不进任何一张表**: 拿到链接就能看那一批稿子, 它和钥匙是一类东西。要留痕只留「发没发」和
batch id 的前 8 位。平台自己的访问日志会记请求路径, 那一层这里管不到(见 docs/deskcore.md §3.29)。
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import html
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import validator

from . import delivery

# ══════════════════════════════════════════════════════════════════════
# 开关、钥匙、对外地址
# ══════════════════════════════════════════════════════════════════════

VALID_DAYS = 14                 # 链接的有效期(只这一处)
MAX_BATCHES = 4                 # 一个链接最多带几个 batch(改稿挂的旧 batch + 这次新建的 + 同一次交付里前面几组存的)
MAX_POSTS = 200                 # 一页最多排几篇: 一次存最多 10 篇, 一次交付两三组二三十篇, 这只是不让一页无限长
TOKEN_VERSION = 1
SIG_BYTES = 16
MIN_SECRET_CHARS = 16

SWITCH_ENV = "DESKCORE_VIEW_PAGE"
SECRET_ENV = "DESKCORE_VIEW_SECRET"
PUBLIC_URL_ENV = "DESKCORE_PUBLIC_URL"
RAILWAY_DOMAIN_ENV = "RAILWAY_PUBLIC_DOMAIN"

_KEY_LABEL = b"deskcore.viewpage.key.v1"
_SIG_LABEL = b"deskcore.viewpage.token.v1"
_HOST = r"(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:]{2,39}\])(?::\d{1,5})?"
_HOST_RE = re.compile(_HOST)
_BASE_RE = re.compile(rf"(https?)://({_HOST})((?:/[A-Za-z0-9._~-]+)*)/?")
_LOCAL_NAMES = ("localhost", "127.0.0.1", "[::1]")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]+")
_TOKEN_SIZES = {1 + 4 + 16 * n + SIG_BYTES for n in range(1, MAX_BATCHES + 1)}
_TOKEN_MAX_CHARS = (max(_TOKEN_SIZES) * 4 + 2) // 3

# 这次请求自己的地址(``https://host`` 或本机的 ``http://localhost:端口``), app.py 的中间件判好了放进来; 不可信 / 不在
# 请求里是 None。工具在线程池里跑, starlette 会把 contextvars 一并带过去(同 app._caller)。
REQUEST_BASE: contextvars.ContextVar[str | None] = contextvars.ContextVar("deskcore_view_request_base", default=None)

# 回包里那一句(写手那边原样带给用户): score_drafts 把它放进 view.note, 有交付稿时接在 delivery.text 的最后一行
# (core._attach_view)。⚠️ 改写法要连 tvlink._VIEW_NOTE_RE 一起改: tv-sync 靠它认出这一行不是稿子(tests/test_view_reply.py 钉着)。
NOTE = "排好版的页面(逐篇可复制, 改稿后刷新就是最新): {url}"

_BEIJING = timezone(timedelta(hours=8))


def host_is_local(host: str) -> bool:
    """Host 头是不是本机(localhost / 127.0.0.1 / [::1], 带不带端口都算)。"""
    h = (host or "").strip().lower()
    name = h[:h.index("]") + 1] if h.startswith("[") and "]" in h else h.rsplit(":", 1)[0] if ":" in h else h
    return name in _LOCAL_NAMES


def request_base(scheme: str, host: str, forwarded_proto: str = "") -> str | None:
    """一次请求自己的地址能不能拿来拼链接: https(直连, 或者前面的代理说是 https)或者本机才行, Host 得是个正常的主机名。
    Host 在不在 app 的白名单里由调用方先判。回 ``https://host`` / ``http://localhost:端口`` / None。"""
    host = (host or "").strip()
    if not _HOST_RE.fullmatch(host):
        return None
    proto = (forwarded_proto or "").split(",")[0].strip().lower() or (scheme or "").lower()
    if proto == "https" or (scheme or "").lower() == "https":
        return f"https://{host}"
    if host_is_local(host):
        return f"http://{host}"
    return None


def _server_secret() -> str:
    """服务本来就必须有的那把(service_role), 同 app.py / db.py 读它的口径。读不到是空串。"""
    try:
        import config
        return getattr(config, "SUPABASE_SERVICE_ROLE_KEY", "") or ""
    except Exception:  # noqa: BLE001
        return ""


def _switched_off(env) -> bool:
    return str(env.get(SWITCH_ENV, "")).strip().lower() in ("0", "false", "off", "no")


def _key_and_why(env) -> tuple[bytes | None, str | None]:
    own = str(env.get(SECRET_ENV, "") or "").strip()
    if own:
        # 配了但太短: 不退回去用另一把(配了就是想用它), 直接算没法用, /health 上看得见
        return (derive_key(own), None) if len(own) >= MIN_SECRET_CHARS else (None, "secret_too_short")
    base = _server_secret()
    return (derive_key(base), None) if len(base) >= MIN_SECRET_CHARS else (None, "no_secret")


def _base_and_why(env) -> tuple[str | None, str | None]:
    configured = str(env.get(PUBLIC_URL_ENV, "") or "").strip()
    if configured:
        m = _BASE_RE.fullmatch(configured)
        if not m or (m.group(1) != "https" and not host_is_local(m.group(2))):
            return None, "bad_public_url"           # 配了但不是 https://主机(本机才许 http): 不猜, 不发
        return f"{m.group(1)}://{m.group(2)}{m.group(3)}", None
    domain = str(env.get(RAILWAY_DOMAIN_ENV, "") or "").strip()
    if domain and _HOST_RE.fullmatch(domain):
        return f"https://{domain}", None
    here = REQUEST_BASE.get()
    return (here, None) if here else (None, "no_public_url")


def derive_key(secret: str) -> bytes:
    """一把服务端的秘密 → 签令牌用的钥匙(HMAC-SHA256 + 固定标签)。"""
    return hmac.new(secret.encode("utf-8"), _KEY_LABEL, hashlib.sha256).digest()


def signing_key(env=None) -> bytes | None:
    """签 / 验令牌的钥匙; 总开关关了或者没有能用的秘密是 None。"""
    env = os.environ if env is None else env
    return None if _switched_off(env) else _key_and_why(env)[0]


def public_base(env=None) -> str | None:
    """链接的前半截(不带末尾的 /); 定不下来是 None。"""
    return _base_and_why(os.environ if env is None else env)[0]


def status(env=None) -> dict:
    """/health 回显的那一小块: 现在发不发得出链接; 发不出时一个固定的原因名。没有钥匙、没有地址。"""
    env = os.environ if env is None else env
    if _switched_off(env):
        return {"enabled": False, "reason": "switched_off"}
    for _value, why in (_key_and_why(env), _base_and_why(env)):
        if why:
            return {"enabled": False, "reason": why}
    return {"enabled": True}


# ══════════════════════════════════════════════════════════════════════
# 令牌
# ══════════════════════════════════════════════════════════════════════

def _sign(payload: bytes, key: bytes) -> bytes:
    return hmac.new(key, _SIG_LABEL + payload, hashlib.sha256).digest()[:SIG_BYTES]


def make_token(batch_ids, expires_at: int, key: bytes) -> str:
    """1~``MAX_BATCHES`` 个 batch id + 到期时间(unix 秒) → 令牌。id 不是 UUID、个数不对、时间装不下都抛 ValueError。"""
    ids = list(dict.fromkeys(str(uuid.UUID(str(b))) for b in batch_ids))
    if not 1 <= len(ids) <= MAX_BATCHES:
        raise ValueError(f"a view token carries 1..{MAX_BATCHES} batches, got {len(ids)}")
    exp = int(expires_at)
    if not 0 < exp < 2 ** 32:
        raise ValueError("expiry out of range")
    if not isinstance(key, (bytes, bytearray)) or len(key) < 16:
        raise ValueError("signing key too short")
    payload = bytes([TOKEN_VERSION]) + exp.to_bytes(4, "big") + b"".join(uuid.UUID(b).bytes for b in ids)
    return base64.urlsafe_b64encode(payload + _sign(payload, bytes(key))).rstrip(b"=").decode("ascii")


def open_token(token, key: bytes | None, now: float) -> tuple[list[str], int] | None:
    """令牌 → (batch id 列表, 到期时间); 形状不对、被改过、过期、太长、版本不认识、钥匙不对都是 None(不分原因)。"""
    if not key or not isinstance(token, str) or not 0 < len(token) <= _TOKEN_MAX_CHARS \
            or not _TOKEN_RE.fullmatch(token):
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, TypeError):
        return None
    # 同一串字节只认一种写法: base64 末尾多出来的那几位改了也解得开, 不认
    if len(raw) not in _TOKEN_SIZES or base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != token:
        return None
    payload, sig = raw[:-SIG_BYTES], raw[-SIG_BYTES:]
    if not hmac.compare_digest(sig, _sign(payload, bytes(key))):
        return None
    if payload[0] != TOKEN_VERSION:
        return None
    exp = int.from_bytes(payload[1:5], "big")
    if exp <= now:
        return None
    return [str(uuid.UUID(bytes=payload[i:i + 16])) for i in range(5, len(payload), 16)], exp


def read_token(token, key: bytes | None, now: float) -> list[str] | None:
    """令牌里的 batch id; 任何不对都是 None(见 open_token)。"""
    got = open_token(token, key, now)
    return None if got is None else got[0]


def mint(batch_ids, *, now: float | None = None, env=None) -> dict | None:
    """给这几个 batch 发一个页面链接: ``{"url", "expires", "note"}``; 发不了是 None(总开关关了、没有能用的秘密、
    对外地址定不下来、一个 batch 都没有、多于 ``MAX_BATCHES`` 个、id 不是 UUID)。

    ``expires`` 是北京时间的 ISO 时间; ``note`` 是给写手的那一句(带链接)。⚠️ 回来的 url / note 不要记日志、不要落库。"""
    env = os.environ if env is None else env
    key, base = signing_key(env), public_base(env)
    if not key or not base:
        return None
    import time
    exp = int(time.time() if now is None else now) + VALID_DAYS * 86400
    try:
        token = make_token(batch_ids or [], exp, key)
    except (ValueError, TypeError, AttributeError):
        return None
    url = f"{base}/v/{token}"
    return {"url": url, "expires": datetime.fromtimestamp(exp, _BEIJING).isoformat(timespec="seconds"),
            "note": NOTE.format(url=url)}


# ══════════════════════════════════════════════════════════════════════
# 页面的数据
# ══════════════════════════════════════════════════════════════════════

def build_page(project_name: str, template: dict | None, batches: list[dict], *, expires_at: int) -> dict:
    """库里读出来的几批稿子 → ``render`` 吃的那份数据。

    ``batches``: 按页面上的先后, 每批 ``{"created_at", "drafts": [{title, body, tags?, comments?, direction?, kind?}]}``
    (drafts 已经是页面上的顺序)。``template``: 项目的交付模板(校验过的)或 None。

    每篇复制出去的两段字不在这里另拼: 模板项目拿 ``delivery.rows``(和 delivery.text 里那一格同一段代码), 没模板的拿
    ``delivery.plain_rows``。页面上分开显示的正文 / 话题也从那一行来; 模板不管话题、或者没有模板时, 正文末尾那行话题只是
    显示上单独拎成一行(复制出去的还是原样留在正文里), 那一行给的话题(没模板时是写手单独传在 tags 里的)排在它后面。"""
    posts: list[dict] = []
    for b in batches:
        drafts = [d if isinstance(d, dict) else {} for d in (b.get("drafts") or [])]
        got = delivery.rows(template, drafts)[0] if template is not None else delivery.plain_rows(drafts)
        for d, r in zip(drafts, got):
            # 模板管话题的那一行: 正文已经摘过末尾的话题, 这里再摘是空的, 话题就是那一行给的
            body, lifted = delivery.lift_trailing_tags(r["body"])
            seen = {t.casefold() for t in lifted}
            tags = lifted + [t for t in r["tags"] if t.casefold() not in seen]
            kind = d.get("kind")
            posts.append({
                "n": len(posts) + 1,
                "direction": r["direction"],
                "kind": kind.strip() if isinstance(kind, str) else "",
                "title": r["title"],
                "paragraphs": [ln.strip() for ln in body.split("\n") if ln.strip()],
                "tags": tags,
                "title_chars": validator.count_chars(r["title"]),
                "body_chars": validator.count_chars(body),
                "comments": [dict(c) for c in r["comment_items"]],
                "copy_post": r["post"],
                "copy_comments": r["comments"],
            })
    more = max(0, len(posts) - MAX_POSTS)
    stamps = sorted(str(b.get("created_at")) for b in batches if b.get("created_at"))
    return {"project": str(project_name or "").strip(), "created_at": stamps[0] if stamps else None,
            "expires_at": int(expires_at), "posts": posts[:MAX_POSTS], "more": more}


# ══════════════════════════════════════════════════════════════════════
# HTML
# ══════════════════════════════════════════════════════════════════════

def _e(text) -> str:
    """库里来的字进 HTML(正文和属性值都用它)。换行 / 回车 / 制表写成字符引用: 放在属性里也一个不少地回得来。"""
    s = html.escape(str(text if text is not None else "").replace("\x00", ""), quote=True)
    return s.replace("\r", "&#13;").replace("\n", "&#10;").replace("\t", "&#9;")


def _beijing(value) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, _BEIJING)
    try:
        d = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(_BEIJING)


_CSS = """
:root{--bg:#f5f6f8;--card:#fff;--line:#e3e6ec;--text:#1f2329;--sub:#5f6672;--faint:#7a818d;
--pill-bg:#e1eeff;--pill:#1456c8;--badge-bg:#fde8e6;--badge:#b3261e;--mark:#cf2f27;--tag:#1565c0;
--head-bg:#eef1f6;--cmt-bg:#f3f5f8;--reader:#3370ff;--author:#98a0ad;--btn:#fff;--btn-line:#c9cfd9;--btn-text:#3c4350;--ok:#16713f}
@media (prefers-color-scheme:dark){:root{--bg:#131519;--card:#1c1f24;--line:#2f343d;--text:#e7e9ec;--sub:#a9b0bb;--faint:#8c94a1;
--pill-bg:#18305a;--pill:#a3c7ff;--badge-bg:#4a1f1c;--badge:#ffada6;--mark:#ff8a82;--tag:#7db8ff;
--head-bg:#23272e;--cmt-bg:#252930;--reader:#5b8dff;--author:#747c8a;--btn:#262a31;--btn-line:#444b57;--btn-text:#d8dce2;--ok:#62d59b}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);overflow-wrap:anywhere;
font:16px/1.75 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans CJK SC","Source Han Sans SC",sans-serif}
.wrap{max-width:1200px;margin:0 auto;padding:36px 20px 72px}
.wrap.single{max-width:780px}
.top{text-align:center;margin-bottom:26px}
.pill{display:inline-block;max-width:100%;padding:4px 14px;border-radius:999px;background:var(--pill-bg);color:var(--pill);font-size:14px;font-weight:600}
h1{margin:16px 0 6px;font-size:28px;line-height:1.3}
.sub{margin:0;color:var(--sub);font-size:14px}
.sub span{display:inline-block}
.sub span+span::before{content:"·";margin:0 6px}
.top .copy{margin-top:16px;padding:6px 16px;font-size:14px}
.sheet{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.cols,.row{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(0,1fr)}
.single .row{grid-template-columns:minmax(0,1fr)}
.cols{background:var(--head-bg);font-weight:600;font-size:15px}
.cols>div{padding:13px 24px}
.cols>div+div{border-left:1px solid var(--line)}
.row{border-top:1px solid var(--line)}
.sheet>.row:first-child{border-top:0}
.post,.side{padding:24px 24px 22px}
.side{border-left:1px solid var(--line)}
.meta,.side-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:10px}
.badge{display:inline-block;padding:2px 10px;border-radius:6px;background:var(--badge-bg);color:var(--badge);font-size:13px;font-weight:600;line-height:1.7}
h2{margin:0 0 12px;font-size:19px;line-height:1.5;font-weight:700}
.mk{color:var(--mark);margin-right:6px}
.body p{margin:0 0 14px}
.tags{margin:2px 0 0;color:var(--tag);font-size:15px}
.count{margin:12px 0 0;color:var(--faint);font-size:13px}
.count::before,.side-head span::before,.none::before{content:attr(data-t)}
.side-head{align-items:center;color:var(--sub);font-size:13px}
.cmt{margin:0 0 12px;padding:11px 14px;background:var(--cmt-bg);border-left:3px solid var(--reader);border-radius:0 8px 8px 0;font-size:15px}
.cmt.author{border-left-color:var(--author)}
.cmt b{margin-right:4px}
.none{margin:0;color:var(--faint);font-size:14px}
.copy{display:none;flex:none;align-items:center;-webkit-user-select:none;user-select:none;padding:3px 12px;border:1px solid var(--btn-line);border-radius:6px;background:var(--btn);color:var(--btn-text);font:inherit;font-size:13px;line-height:1.6;white-space:nowrap;cursor:pointer}
.js .copy{display:inline-flex}
.copy:hover{border-color:var(--pill);color:var(--pill)}
.copy.done{border-color:var(--ok);color:var(--ok)}
.copy:focus-visible{outline:2px solid var(--pill);outline-offset:2px}
.clip{position:fixed;top:0;left:-9999px;opacity:0}
.foot{margin:18px 0 0;text-align:center;color:var(--faint);font-size:13px}
.msg{max-width:460px;margin:18vh auto 0;padding:28px 24px;background:var(--card);border:1px solid var(--line);border-radius:12px;text-align:center}
.msg h1{margin:0 0 10px;font-size:20px}
.msg p{margin:0;color:var(--sub);font-size:15px}
@media (max-width:820px){.wrap{padding:22px 12px 56px}h1{font-size:23px}.cols{display:none}
.row{grid-template-columns:minmax(0,1fr)}.post,.side{padding:20px 16px 16px}
.side{border-left:0;border-top:1px dashed var(--line)}
.sub span{display:block}.sub span+span::before{content:none}}
""".strip()

# 复制按钮。要复制的字在按钮的 data-copy 里(服务端拼的), 不从页面上看得见的排版里抠; 「复制全部笔记」把每篇那一份
# 用一个空行连起来。先试 navigator.clipboard, 不行(老浏览器、没给权限)退回选中一个看不见的文本框再 execCommand。
_JS = """
(function(){var d=document;d.documentElement.className+=' js';
function old(t){return new Promise(function(ok,no){var a=d.createElement('textarea');a.value=t;a.className='clip';
a.setAttribute('readonly','');d.body.appendChild(a);a.select();var g=false;try{g=d.execCommand('copy')}catch(e){}
d.body.removeChild(a);g?ok():no()})}
function put(t){if(navigator.clipboard&&window.isSecureContext){return navigator.clipboard.writeText(t).catch(function(){return old(t)})}return old(t)}
function say(b,s,c){b.textContent=s;if(c)b.classList.add('done');clearTimeout(b._t);
b._t=setTimeout(function(){b.textContent=b.getAttribute('data-label');b.classList.remove('done')},1800)}
d.addEventListener('click',function(e){var b=e.target&&e.target.closest?e.target.closest('button.copy'):null;if(!b)return;
var t=b.getAttribute('data-copy');
if(t===null){t=[].map.call(d.querySelectorAll('button.copy[data-kind="post"]'),function(x){return x.getAttribute('data-copy')}).join('\\n\\n')}
put(t).then(function(){say(b,'已复制',1)},function(){say(b,'没复制上, 请手动选中',0)})})})();
""".strip()


def _doc(title: str, body: str, nonce: str, script: bool) -> str:
    n = _e(nonce)
    return ("<!doctype html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta name=\"robots\" content=\"noindex, nofollow\"><meta name=\"referrer\" content=\"no-referrer\">"
            "<meta name=\"color-scheme\" content=\"light dark\">"
            "<link rel=\"icon\" href=\"data:,\">"       # 不然浏览器会自己去要 /favicon.ico: 那条路要钥匙, 每开一次页面记一次被拒
            f"<title>{_e(title)}</title><style nonce=\"{n}\">{_CSS}</style></head>\n<body>\n{body}\n"
            + (f"<script nonce=\"{n}\">{_JS}</script>\n" if script else "") + "</body></html>\n")


def _button(label: str, kind: str, text: str | None = None) -> str:
    data = "" if text is None else f" data-copy=\"{_e(text)}\""
    return (f"<button type=\"button\" class=\"copy\" data-kind=\"{_e(kind)}\" data-label=\"{_e(label)}\"{data}>"
            f"{_e(label)}</button>")


def _chrome(tag: str, cls: str, text: str) -> str:
    """页面自己的小字(字数那一行、「评论 N 条」、「这篇没有评论」): 字放在属性里, 由样式画出来(``content:attr(data-t)``)。
    看得见, 但用鼠标拖选、全选复制时带不走 —— 写手习惯拖选一篇直接贴进飞书, 这几行不是稿子, 跟着进了格子会被 tv-sync 当成
    正文。不用 ``user-select:none``: 那样从这几个字上起手拖就什么都选不中(Chromium 实测)。按钮才用它。"""
    cls = f" class=\"{_e(cls)}\"" if cls else ""
    return f"<{tag}{cls} data-t=\"{_e(text)}\"></{tag}>"


def _post_html(p: dict, two_columns: bool) -> str:
    # 小标签写成「第N篇」, 中间不空格: 和没有模板的项目默认排法(core.PLAIN_LAYOUT_NEXT_STEP)里那一行是同一个写法,
    # 写手拿对话里看到的「第37篇」在页面上查找能查到
    badge = " · ".join(x for x in (f"第{int(p.get('n') or 0)}篇", str(p.get("direction") or "").strip(),
                                   str(p.get("kind") or "").strip()) if x)
    out = [f"<article class=\"row\" id=\"p{int(p.get('n') or 0)}\"><div class=\"post\">",
           f"<div class=\"meta\"><span class=\"badge\">{_e(badge)}</span>"
           f"{_button('复制笔记', 'post', str(p.get('copy_post') or ''))}</div>",
           f"<h2><span class=\"mk\">{_e(delivery.DEFAULT_MARKERS['title'])}</span>{_e(p.get('title'))}</h2>",
           "<div class=\"body\">" + "".join(f"<p>{_e(x)}</p>" for x in p.get("paragraphs") or []) + "</div>"]
    tags = [str(t) for t in p.get("tags") or [] if str(t).strip()]
    if tags:
        out.append("<p class=\"tags\">" + _e(" ".join("#" + t for t in tags)) + "</p>")
    out.append(_chrome("p", "count", f"标题 {int(p.get('title_chars') or 0)} 字 · 正文 {int(p.get('body_chars') or 0)} 字")
               + "</div>")
    if two_columns:
        comments = [c for c in p.get("comments") or [] if isinstance(c, dict)]
        out.append("<div class=\"side\">")
        if comments:
            out.append("<div class=\"side-head\">" + _chrome("span", "", f"评论 {len(comments)} 条")
                       + f"{_button('复制评论', 'comments', str(p.get('copy_comments') or ''))}</div>")
            for c in comments:
                role = "author" if c.get("role") == "author" else "reader"
                label = str(c.get("label") or "")
                out.append(f"<div class=\"cmt {role}\">" + (f"<b>{_e(label)}</b>" if label else "")
                           + f"{_e(c.get('text'))}</div>")
        else:
            out.append(_chrome("p", "none", "这篇没有评论"))
        out.append("</div>")
    out.append("</article>")
    return "".join(out)


def render(page: dict, nonce: str | None = None) -> str:
    """页面数据(``build_page`` 的结果) → 一整份 HTML。``nonce``: 这次响应的 nonce(和 ``headers`` 用同一个); 不给就现编一个
    (存成文件直接打开时用, 那时没有 CSP 头)。库里来的字全部转义。"""
    nonce = nonce or secrets.token_urlsafe(16)
    posts = [p for p in page.get("posts") or [] if isinstance(p, dict)]
    two = any(p.get("comments") for p in posts)
    project = str(page.get("project") or "").strip() or "写作台"
    made, until = _beijing(page.get("created_at")), _beijing(page.get("expires_at"))
    # 页面是直接给写手看的, 中间没有模型: 不说「存档」「重存」(协议叫模型别跟用户提这两个词, 她没有这一步)
    sub = [f"{made.year} 年 {made.month} 月 {made.day} 日 {made:%H:%M} 写好(北京时间)"] if made else []
    if until:
        sub.append(f"链接到 {until.month} 月 {until.day} 日前有效")
    more = int(page.get("more") or 0)
    body = [f"<main class=\"wrap{'' if two else ' single'}\">",
            f"<header class=\"top\"><span class=\"pill\">{_e(project)}</span><h1>共 {len(posts) + more} 篇笔记</h1>",
            "<p class=\"sub\">" + "".join(f"<span>{_e(x)}</span>" for x in sub) + "</p>",
            f"<div>{_button('复制全部笔记', 'all')}</div></header>",
            "<section class=\"sheet\">"]
    if two:
        body.append("<div class=\"cols\"><div>笔记</div><div>评论</div></div>")
    body += [_post_html(p, two) for p in posts]
    body.append("</section>")
    notes = ["改过稿子之后刷新这个页面, 看到的就是最新的一版。"]
    if more:
        notes.insert(0, f"这一页只排了前 {len(posts)} 篇, 还有 {more} 篇没排上。")
    body.append(f"<p class=\"foot\">{_e(''.join(notes))}</p></main>")
    return _doc(f"{project} · {len(posts) + more} 篇", "\n".join(body), nonce, script=True)


ERROR_STATUS = 404
BUSY_STATUS = 503


def error_page(nonce: str | None = None) -> str:
    """链接不对 / 过期 / 找不到那一批: 都是这一页、同一个状态码(``ERROR_STATUS``), 不说是哪一种。

    页面上叫写手做的两件事都是做得到的: 链接没贴全的, 对话里原来那一行还在, 重新复制; 过期了的, 写作台没有「给旧稿子
    补发一个链接」这一步(链接只在存稿的回包里发), 改任意一篇之后的回包里有新的。"""
    return _doc("链接打不开", "<main class=\"msg\"><h1>这个链接打不开了</h1>"
                "<p>链接可能不完整, 或者已经过期。先回到写作台, 把原来那一行链接完整地再复制一次; 还是打不开, 多半是"
                "过期了: 稿子都还在, 让写作台改其中任意一篇, 改好的那次会带一个新的链接。</p></main>",
                nonce or secrets.token_urlsafe(16), script=False)


def busy_page(nonce: str | None = None) -> str:
    """读库慢了 / 读不出来(``BUSY_STATUS``): 链接本身没问题, 过一会儿再开。"""
    return _doc("页面暂时打不开", "<main class=\"msg\"><h1>页面暂时打不开</h1>"
                "<p>稿子都还在, 只是这会儿没读出来。过一会儿刷新一下。</p></main>",
                nonce or secrets.token_urlsafe(16), script=False)


def headers(nonce: str) -> dict[str, str]:
    """页面(连同两张出错页)的响应头: 只许这一页自己带 nonce 的样式和脚本, 别的一概不许加载、不许被嵌、不许被收录、
    不许缓存、跳出去不带来源。``img-src data:`` 只为页面里那个空的图标(不发请求); 别的图片照样加载不了。"""
    return {
        "Content-Security-Policy": (f"default-src 'none'; style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; img-src data:; "
                                    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
        "X-Robots-Tag": "noindex, nofollow",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
