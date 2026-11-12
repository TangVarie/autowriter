"""一批稿子排好版的页面, 纯函数那一层(2026-11-12, deskcore/viewpage.py)。

钉住:
  1. 令牌: 带得回 1~4 个 batch; 改一个字符、过期、形状不对、太长、版本不认识、钥匙不对都是 None, 不分原因;
  2. 钥匙和地址: 配了 DESKCORE_VIEW_SECRET 用它, 没配从服务本来就有的那把派生; 地址按 配的 → Railway 的域名 → 这次请求
     的顺序; 总开关关了什么都不发; /health 那一小块里没有钥匙、没有地址;
  3. 页面: 库里来的每一个字(标题、正文、话题、评论、标签、项目名、方向、贴型)都转义; 只有自己那一段脚本; 不向外部发请求;
  4. 复制出去的字和 delivery 拼的那一格同一段代码: 模板项目是 delivery.text 里那一格, 没模板的是【标题】…换行【正文】…,
     tv-sync 拆得回标题和正文。
路由那一层在 tests/test_view_page_route.py。所有文本都是合成的, 钥匙是测试里现编的。
"""
from __future__ import annotations

import ast
import base64
import pathlib
import uuid
from html.parser import HTMLParser

import pytest

from deskcore import delivery, tvlink, viewpage

KEY = viewpage.derive_key("xianbian-de-ceshi-miyao-0001")
OTHER_KEY = viewpage.derive_key("xianbian-de-ceshi-miyao-0002")
NOW = 1_800_000_000
EXP = NOW + 3600
IDS = [str(uuid.UUID(int=n)) for n in (11, 22, 33, 44, 55)]

TABLE = {"layout": "table",
         "columns": [{"name": "方向", "field": "direction"}, {"name": "笔记", "field": "post"},
                     {"name": "评论", "field": "comments"}],
         "tags": {"content": 2, "fixed": ["固定话题"]},
         "comments": {"min": 1, "max": 3, "label_reader": "读者：", "label_author": "作者回复："}}
BLOCKS = {"layout": "blocks", "tags": {"content": 1},
          "comments": {"min": 0, "max": 3, "label_reader": "读者：", "label_author": "作者回复："}}


def _drafts(n: int = 2, *, comments: bool = True) -> list[dict]:
    out = []
    for i in range(n):
        d = {"title": f"合成标题第{i}篇", "body": f"第{i}篇的第一段。\n\n第{i}篇的第二段。[捂脸R]",
             "tags": [f"话题{i}甲", f"#话题{i}乙"], "direction": f"第{i}篇的方向"}
        if comments:
            d["comments"] = [{"role": "reader", "text": f"第{i}篇的读者评论"}, {"role": "author", "text": "作者的回复"}]
        out.append(d)
    return out


def _page(tpl=TABLE, drafts=None, **kw) -> dict:
    tpl = delivery.validate(tpl) if tpl is not None else None
    return viewpage.build_page(kw.pop("project", "示例项目"), tpl,
                               [{"created_at": "2026-11-12T06:30:00+00:00", "drafts": _drafts() if drafts is None else drafts}],
                               expires_at=kw.pop("expires_at", 1_795_000_000))


class _Doc(HTMLParser):
    """把页面解析一遍: 每个标签和它的属性、每段文字归在哪个标签下面。"""

    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict]] = []
        self.text: list[tuple[str, str]] = []
        self._stack: list[str] = []
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag not in ("meta", "br", "link", "img", "input"):
            self._stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self._stack:
            while self._stack and self._stack.pop() != tag:
                pass

    def handle_data(self, data):
        self.text.append((self._stack[-1] if self._stack else "", data))

    def named(self, tag: str) -> list[dict]:
        return [a for t, a in self.tags if t == tag]

    def buttons(self, kind: str) -> list[dict]:
        return [a for a in self.named("button") if a.get("data-kind") == kind]

    def drawn(self) -> list[str]:
        """页面自己的小字(字数、「评论 N 条」、「这篇没有评论」): 放在 data-t 里由样式画出来, 不是元素里的字。"""
        return [a["data-t"] for _t, a in self.tags if "data-t" in a]


# ── 1. 令牌 ──────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_token_round_trips_one_to_four_batches_in_order(n):
    token = viewpage.make_token(IDS[:n], EXP, KEY)
    assert viewpage.read_token(token, KEY, NOW) == IDS[:n]
    assert viewpage.open_token(token, KEY, NOW) == (IDS[:n], EXP)
    assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"), "放进链接不用再转义"
    assert len(token) <= 114


def test_token_drops_repeated_batches_and_accepts_uuid_objects():
    token = viewpage.make_token([uuid.UUID(IDS[0]), IDS[1].upper(), IDS[0]], EXP, KEY)
    assert viewpage.read_token(token, KEY, NOW) == IDS[:2]


def test_changing_any_single_character_of_the_token_voids_it():
    token = viewpage.make_token(IDS[:2], EXP, KEY)
    for at in range(len(token)):
        for swap in ("A", "B", "-"):
            if token[at] == swap:
                continue
            forged = token[:at] + swap + token[at + 1:]
            assert viewpage.read_token(forged, KEY, NOW) is None, at


def test_token_expires_at_the_stated_second():
    token = viewpage.make_token(IDS[:1], EXP, KEY)
    assert viewpage.read_token(token, KEY, EXP - 1) == IDS[:1]
    assert viewpage.read_token(token, KEY, EXP) is None
    assert viewpage.read_token(token, KEY, EXP + 86400) is None


@pytest.mark.parametrize("bad", ["", " ", None, 123, b"abc", "a", "abc.def", "a/b", "中文", "A" * 50, "A" * 115, "A" * 5000,
                                 "%2e%2e", "abc def", "abc\n"])
def test_malformed_tokens_are_none_not_an_exception(bad):
    assert viewpage.read_token(bad, KEY, NOW) is None


def test_token_with_a_trailing_or_missing_character_is_none():
    token = viewpage.make_token(IDS[:1], EXP, KEY)
    for forged in (token[:-1], token + "A", token + "=", token + "==", " " + token, token + "。"):
        assert viewpage.read_token(forged, KEY, NOW) is None, forged


def test_a_validly_signed_token_of_an_unknown_version_is_none():
    payload = bytes([viewpage.TOKEN_VERSION + 1]) + EXP.to_bytes(4, "big") + uuid.UUID(IDS[0]).bytes
    token = base64.urlsafe_b64encode(payload + viewpage._sign(payload, KEY)).rstrip(b"=").decode()
    assert viewpage.read_token(token, KEY, NOW) is None


def test_only_one_spelling_of_a_token_is_accepted():
    """base64 末尾多出来的那几位改了也解得开同一串字节: 不认, 一个令牌只有一种写法。"""
    token = viewpage.make_token(IDS[:1], EXP, KEY)            # 37 字节 → 50 个字符, 最后一个字符只有 4 位有效
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    twin = token[:-1] + alphabet[alphabet.index(token[-1]) ^ 1]
    assert base64.urlsafe_b64decode(twin + "==") == base64.urlsafe_b64decode(token + "=="), "两种写法解出来一样"
    assert viewpage.read_token(twin, KEY, NOW) is None


@pytest.mark.parametrize("ids", [[], IDS[:5], ["not-a-uuid"], [None], ["11111111-1111-1111-1111-11111111111"]])
def test_make_token_refuses_no_batch_too_many_or_a_non_uuid(ids):
    with pytest.raises((ValueError, TypeError, AttributeError)):
        viewpage.make_token(ids, EXP, KEY)


def test_make_token_refuses_a_short_key_or_an_expiry_that_does_not_fit():
    with pytest.raises(ValueError):
        viewpage.make_token(IDS[:1], EXP, b"short")
    for exp in (0, -1, 2 ** 32):
        with pytest.raises(ValueError):
            viewpage.make_token(IDS[:1], exp, KEY)


def test_wrong_or_missing_key_is_none():
    token = viewpage.make_token(IDS[:1], EXP, KEY)
    assert viewpage.read_token(token, OTHER_KEY, NOW) is None
    assert viewpage.read_token(token, None, NOW) is None
    assert viewpage.read_token(token, b"", NOW) is None


def test_validity_is_fourteen_days_in_one_constant():
    assert viewpage.VALID_DAYS == 14 and viewpage.MAX_BATCHES == 4 and viewpage.SIG_BYTES >= 16


# ── 2. 钥匙、地址、总开关 ────────────────────────────────────────────────────────────────────────

SERVER_SECRET = "fuwuduan-benlai-jiuyou-de-nayiba"


@pytest.fixture()
def server_secret(monkeypatch):
    monkeypatch.setattr(viewpage, "_server_secret", lambda: SERVER_SECRET)


def test_own_secret_wins_over_the_derived_one(server_secret):
    own = viewpage.signing_key({"DESKCORE_VIEW_SECRET": "zhuanmen-pei-de-yiba-miyao"})
    assert own == viewpage.derive_key("zhuanmen-pei-de-yiba-miyao")
    derived = viewpage.signing_key({})
    assert derived == viewpage.derive_key(SERVER_SECRET) and derived != own
    assert len(derived) == 32 and SERVER_SECRET.encode() not in derived


def test_a_too_short_own_secret_turns_the_feature_off_rather_than_falling_back(server_secret):
    env = {"DESKCORE_VIEW_SECRET": "duan", "DESKCORE_PUBLIC_URL": "https://desk.example.com"}
    assert viewpage.signing_key(env) is None
    assert viewpage.status(env) == {"enabled": False, "reason": "secret_too_short"}
    assert viewpage.mint(IDS[:1], now=NOW, env=env) is None


def test_no_usable_secret_means_off(monkeypatch):
    monkeypatch.setattr(viewpage, "_server_secret", lambda: "")
    env = {"DESKCORE_PUBLIC_URL": "https://desk.example.com"}
    assert viewpage.signing_key(env) is None
    assert viewpage.status(env) == {"enabled": False, "reason": "no_secret"}
    assert viewpage.mint(IDS[:1], now=NOW, env=env) is None


@pytest.mark.parametrize("off", ["0", "false", "OFF", " no "])
def test_kill_switch_stops_minting_and_reading(server_secret, off):
    env = {"DESKCORE_VIEW_PAGE": off, "DESKCORE_PUBLIC_URL": "https://desk.example.com"}
    assert viewpage.signing_key(env) is None, "已经发出去的链接也验不过"
    assert viewpage.mint(IDS[:1], now=NOW, env=env) is None
    assert viewpage.status(env) == {"enabled": False, "reason": "switched_off"}


@pytest.mark.parametrize("on", ["", "1", "true", "anything"])
def test_switch_is_on_unless_explicitly_off(server_secret, on):
    assert viewpage.status({"DESKCORE_VIEW_PAGE": on, "DESKCORE_PUBLIC_URL": "https://desk.example.com"}) == {"enabled": True}


def test_public_base_prefers_the_configured_url_then_railway_then_the_request(server_secret):
    token = viewpage.REQUEST_BASE.set("https://from-request.example.com")
    try:
        env = {"DESKCORE_PUBLIC_URL": "https://desk.example.com/", "RAILWAY_PUBLIC_DOMAIN": "desk.up.example.app"}
        assert viewpage.public_base(env) == "https://desk.example.com"
        assert viewpage.public_base({"DESKCORE_PUBLIC_URL": "https://desk.example.com/base/"}) == "https://desk.example.com/base"
        assert viewpage.public_base({"RAILWAY_PUBLIC_DOMAIN": "desk.up.example.app"}) == "https://desk.up.example.app"
        assert viewpage.public_base({}) == "https://from-request.example.com"
    finally:
        viewpage.REQUEST_BASE.reset(token)
    assert viewpage.public_base({}) is None
    assert viewpage.status({}) == {"enabled": False, "reason": "no_public_url"}


@pytest.mark.parametrize("bad", ["http://desk.example.com", "desk.example.com", "ftp://desk.example.com",
                                 "https://desk.example.com/?a=1", "https://user:pw@desk.example.com",
                                 "https://desk.example.com/a b", "javascript:alert(1)", "https://"])
def test_a_configured_but_unusable_public_url_turns_it_off_instead_of_guessing(server_secret, bad):
    env = {"DESKCORE_PUBLIC_URL": bad, "RAILWAY_PUBLIC_DOMAIN": "desk.up.example.app"}
    assert viewpage.public_base(env) is None
    assert viewpage.status(env) == {"enabled": False, "reason": "bad_public_url"}
    assert viewpage.mint(IDS[:1], now=NOW, env=env) is None


def test_configured_http_is_fine_for_localhost_only(server_secret):
    assert viewpage.public_base({"DESKCORE_PUBLIC_URL": "http://localhost:8000"}) == "http://localhost:8000"
    assert viewpage.public_base({"DESKCORE_PUBLIC_URL": "http://127.0.0.1:8000/"}) == "http://127.0.0.1:8000"


@pytest.mark.parametrize("scheme, host, fwd, want", [
    ("https", "desk.example.com", "", "https://desk.example.com"),
    ("http", "desk.example.com", "https", "https://desk.example.com"),
    ("http", "desk.example.com", "https, http", "https://desk.example.com"),
    ("http", "desk.example.com", "", None),
    ("http", "desk.example.com", "http", None),
    ("http", "localhost:8000", "", "http://localhost:8000"),
    ("http", "127.0.0.1:8765", "", "http://127.0.0.1:8765"),
    ("http", "[::1]:8000", "", "http://[::1]:8000"),
    ("http", "localhost.evil.example.com", "", None),
    ("https", "", "", None),
    ("https", "desk.example.com/path", "", None),
    ("https", "desk.example.com@evil.example.com", "", None),
    ("https", "desk example.com", "", None),
    ("https", "desk.example.com:99999999", "", None),
    ("https", "<script>", "", None),
])
def test_request_base_trusts_only_https_or_localhost_with_a_plain_host(scheme, host, fwd, want):
    assert viewpage.request_base(scheme, host, fwd) == want


def test_mint_gives_url_expiry_and_the_note_and_the_token_reads_back(server_secret):
    env = {"DESKCORE_PUBLIC_URL": "https://desk.example.com"}
    got = viewpage.mint(IDS[:2], now=NOW, env=env)
    assert set(got) == {"url", "expires", "note"}
    assert got["url"].startswith("https://desk.example.com/v/")
    token = got["url"].rsplit("/", 1)[1]
    assert viewpage.read_token(token, viewpage.signing_key(env), NOW + 14 * 86400 - 1) == IDS[:2]
    assert viewpage.read_token(token, viewpage.signing_key(env), NOW + 14 * 86400) is None, "14 天整到期"
    assert got["note"] == f"排好版的页面(逐篇可复制, 改稿后刷新就是最新): {got['url']}"
    assert got["expires"].endswith("+08:00") and got["expires"].startswith("2027-01-29T"), got["expires"]


@pytest.mark.parametrize("ids", [[], None, IDS[:5], ["not-a-uuid"], [None]])
def test_mint_is_none_for_no_batches_too_many_or_bad_ids(server_secret, ids):
    assert viewpage.mint(ids, now=NOW, env={"DESKCORE_PUBLIC_URL": "https://desk.example.com"}) is None


def test_status_never_carries_the_secret_or_the_address(server_secret):
    for env in ({"DESKCORE_PUBLIC_URL": "https://desk.example.com", "DESKCORE_VIEW_SECRET": "zhuanmen-pei-de-yiba-miyao"},
                {"DESKCORE_VIEW_PAGE": "0"}, {}):
        text = repr(viewpage.status(env))
        assert set(viewpage.status(env)) <= {"enabled", "reason"}
        for leak in (SERVER_SECRET, "zhuanmen", "desk.example.com"):
            assert leak not in text


# ── 3. 页面: 转义、结构 ──────────────────────────────────────────────────────────────────────────

EVIL = ['<script>alert(1)</script>', '"><img src=x onerror=alert(1)>', "' onmouseover='alert(1)", "</style><script>x</script>",
        "</script><script>alert(2)</script>", "&lt;b&gt;", "<a href=\"https://evil.example.com\">点我</a>",
        "<link rel=stylesheet href=//evil.example.com/x.css>", "javascript:alert(1)", "\"><svg/onload=alert(1)>"]


def _evil_page(payload: str) -> dict:
    drafts = [{"title": f"标题{payload}", "body": f"正文第一段{payload}\n第二段{payload}", "tags": [f"话题{payload}", "正常话题"],
               "comments": [{"role": "reader", "text": f"评论{payload}"}, {"role": "author", "text": f"回复{payload}"}],
               "direction": f"方向{payload}", "kind": f"贴型{payload}"}]
    page = _page(BLOCKS, drafts, project=f"项目{payload}")
    page["posts"][0]["comments"][0]["label"] = f"标签{payload}"       # 模板的标签有长度和字符限制; 这里直接塞, 页面自己也得挡住
    return page


@pytest.mark.parametrize("payload", EVIL)
def test_nothing_from_the_database_becomes_markup(payload):
    out = viewpage.render(_evil_page(payload), nonce="n0nce")
    doc = _Doc(out)
    scripts = doc.named("script")
    assert len(scripts) == 1 and scripts[0] == {"nonce": "n0nce"}, "只有页面自己那一段脚本, 没有 src"
    assert {t for t, _a in doc.tags} <= {"html", "head", "meta", "link", "title", "style", "body", "main", "header", "span",
                                         "h1", "p", "div", "button", "section", "article", "h2", "b", "script"}
    assert doc.named("link") == [{"rel": "icon", "href": "data:,"}], "只有那个空图标(不发请求)"
    for tag, attrs in doc.tags:
        assert not [k for k in attrs if k.startswith("on")], attrs
        assert not {"src", "style", "srcset", "action", "formaction"} & set(attrs), attrs
        assert tag == "link" or "href" not in attrs, attrs
    visible = "".join(text for tag, text in doc.text if tag not in ("script", "style"))
    assert visible.count(payload) >= 8, "原样显示成字: 项目名、标题、两段正文、话题、两条评论、标签、方向、贴型"
    script_text = "".join(text for tag, text in doc.text if tag == "script")
    assert payload not in script_text and "alert" not in script_text


@pytest.mark.parametrize("payload", EVIL + ["一行\n两行", "带\t制表", "回车\r\n换行", "尾巴有空格 ", "&amp;amp;", "引号\"和'"])
def test_copy_text_survives_the_attribute_byte_for_byte(payload):
    page = _page(None, [{"title": "标题", "body": "正文"}])
    page["posts"][0]["copy_post"] = payload
    page["posts"][0]["comments"] = [{"role": "reader", "label": "读者：", "text": "评论"}]
    page["posts"][0]["copy_comments"] = payload
    doc = _Doc(viewpage.render(page))
    want = payload.replace("\r\n", "\n")             # 浏览器读属性时回车换行是一个换行; 存进库的稿子本来也没有回车
    assert [b["data-copy"].replace("\r\n", "\n") for b in doc.buttons("post")] == [want]
    assert [b["data-copy"].replace("\r\n", "\n") for b in doc.buttons("comments")] == [want]


def test_page_makes_no_request_to_anywhere():
    out = viewpage.render(_page())
    lowered = out.lower()
    assert out.count("href=") == 1 and out.count("<link") == 1 and '<link rel="icon" href="data:,">' in out, \
        "一个空图标: 不然浏览器自己去要 /favicon.ico(那条路要钥匙, 每开一次页面记一次被拒)"
    for needle in ("http://", "https://", "url(", "@import", "<img", "<iframe", "<form", "src=", "fetch(",
                   "xmlhttprequest", "sendbeacon", "websocket", "eval(", "innerhtml", "document.write", "localstorage",
                   "document.cookie"):
        assert needle not in lowered, needle
    assert 'name="robots" content="noindex, nofollow"' in out and 'name="referrer" content="no-referrer"' in out


def test_two_columns_when_any_post_has_comments():
    drafts = _drafts(3)
    drafts[2].pop("comments")
    doc = _Doc(viewpage.render(_page(BLOCKS, drafts)))
    classes = [a.get("class") for _t, a in doc.tags]
    assert "wrap" in classes and "cols" in classes and classes.count("side") == 3
    assert len(doc.buttons("post")) == 3 and len(doc.buttons("comments")) == 2 and len(doc.buttons("all")) == 1
    assert "data-copy" not in doc.buttons("all")[0], "「复制全部」不另存一份: 由每篇那一份连起来"
    texts = [text for _tag, text in doc.text]
    assert doc.drawn().count("这篇没有评论") == 1 and doc.drawn().count("评论 2 条") == 2
    assert "这篇没有评论" not in texts and "评论 2 条" not in texts, "页面自己的小字拖选 / 全选时带不走"
    assert [a["class"] for a in doc.named("div") if (a.get("class") or "").startswith("cmt")] == ["cmt reader", "cmt author"] * 2


def test_single_centred_column_when_no_post_has_comments():
    doc = _Doc(viewpage.render(_page(None, _drafts(4, comments=False))))
    classes = [a.get("class") for _t, a in doc.tags]
    assert "wrap single" in classes and "cols" not in classes and "side" not in classes
    assert not doc.buttons("comments") and len(doc.buttons("post")) == 4


def test_header_badge_title_tags_and_counts():
    page = _page(TABLE, _drafts(2))
    page["posts"][1]["kind"] = "产品贴"
    out = viewpage.render(page)
    doc = _Doc(out)
    texts = [text for _tag, text in doc.text]
    assert "示例项目" in texts and "共 2 篇笔记" in texts
    assert "2026 年 11 月 12 日 14:30 写好(北京时间)" in texts, "库里是 UTC 06:30, 页面上是北京时间"
    assert "链接到 11 月 18 日前有效" in texts, "1795000000 是北京时间 2026-11-18 19:06: 说得保守一点, 那天之前都打得开"
    assert "第1篇 · 第0篇的方向" in texts and "第2篇 · 第1篇的方向 · 产品贴" in texts
    assert texts.count("【标题】") == 2 and "【正文】" not in texts, "页面上只显示【标题】; 【正文】只在复制出去的那一份里"
    assert "#话题0甲 #话题0乙 #固定话题" in texts
    assert "标题 7 字 · 正文 21 字" in doc.drawn() and not any("字 · 正文" in t for t in texts)
    paragraphs = [text for tag, text in doc.text if tag == "p"]
    assert "第0篇的第一段。" in paragraphs and "第0篇的第二段。[捂脸R]" in paragraphs, "按换行分段, 空行不出空段"
    assert out.count("<p></p>") == 0
    assert doc.named("html")[0] == {"lang": "zh-CN"} and "prefers-color-scheme:dark" in out and "max-width:820px" in out


def test_no_direction_gives_a_plain_badge_and_missing_dates_are_left_out():
    page = _page(None, [{"title": "标题", "body": "正文"}])
    page["created_at"] = "不是时间"
    texts = [text for _tag, text in _Doc(viewpage.render(page)).text]
    assert "第1篇" in texts and not any("写好" in t or "北京时间" in t or " 年 " in t for t in texts)


def test_error_pages_are_static_and_scriptless():
    for fn in (viewpage.error_page, viewpage.busy_page):
        doc = _Doc(fn("n0nce"))
        assert not doc.named("script") and doc.named("style") == [{"nonce": "n0nce"}]
        assert fn("n0nce") == fn("n0nce")
    assert viewpage.ERROR_STATUS == 404 and viewpage.BUSY_STATUS == 503
    # 页面上叫写手做的事得是做得到的(WF-4): 写作台没有「给旧稿子补发一个链接」这一步, 链接只跟着存稿的回包发
    error, busy = viewpage.error_page("n"), viewpage.busy_page("n")
    assert "重新给一个页面链接" not in error + busy
    assert "完整地再复制一次" in error and "改其中任意一篇" in error and "新的链接" in error
    assert "过一会儿刷新一下" in busy and "链接" not in busy.split("<main", 1)[1], "读不出来时换一个链接没有用"


def test_the_page_does_not_use_the_words_the_desk_never_says_to_a_writer():
    """页面直接到写手手里, 中间没有模型: 协议叫模型别跟用户提「存档」, 页面自己也不说(WF-8)。"""
    pages = [viewpage.render(_page()), viewpage.render(_page(None, _drafts(2, comments=False))),
             viewpage.error_page("n"), viewpage.busy_page("n")]
    for out in pages:
        visible = " ".join(text for _tag, text in _Doc(out).text if _tag not in ("style", "script"))
        visible += " ".join(_Doc(out).drawn())
        assert "存档" not in visible and "重存" not in visible, visible[:200]
    assert "改过稿子之后刷新这个页面" in pages[0]


def test_a_hand_selection_takes_only_the_post():
    """写手习惯拖选一篇直接贴进飞书(参考页没有按钮; 复制失败时页面也叫她手动选)。按钮上的字、字数那一行、「评论 N 条」跟着
    进了格子, tv-sync 会把它们当成正文(PAGE-3)。按钮不许选中; 那几行小字是样式画出来的, 不在元素的文字里。"""
    out = viewpage.render(_page(BLOCKS, _drafts(2)))
    css = out.split("<style", 1)[1].split("</style>", 1)[0]
    copy_rule = next(r for r in css.split("}") if r.strip().startswith(".copy{"))
    assert "user-select:none" in copy_rule and "-webkit-user-select:none" in copy_rule
    assert ".count::before" in css and "content:attr(data-t)" in css
    for rule in css.split("}"):
        if "user-select:none" in rule:
            assert rule.strip().startswith(".copy{"), "小标签和那几行小字不用 user-select:none: 从它们上面起手拖会什么都选不中"
    doc = _Doc(out)
    words = [text for tag, text in doc.text if tag not in ("style", "script", "button", "title")]
    assert not any("字 · 正文" in w or "评论 2 条" in w or "复制" in w for w in words), words
    assert sorted(set(doc.drawn())) == ["标题 7 字 · 正文 21 字", "评论 2 条"]
    count = next(a for t, a in doc.tags if t == "p" and a.get("class") == "count")
    assert count["data-t"] == "标题 7 字 · 正文 21 字"


def test_the_badge_is_spelled_like_the_default_layout_line():
    """对话里那一行是「第N篇 · 场景」(core.PLAIN_LAYOUT_NEXT_STEP), 页面的小标签写法一样: 拿「第2篇」在页面上查找查得到(PAGE-4)。"""
    from deskcore import core
    assert "「第N篇 · 这篇的场景" in core.PLAIN_LAYOUT_NEXT_STEP
    texts = [text for _tag, text in _Doc(viewpage.render(_page(None, _drafts(12, comments=False)))).text]
    assert "第2篇 · 第1篇的方向" in texts and "第12篇 · 第11篇的方向" in texts
    assert not any(t.startswith("第 ") for t in texts)


def test_headers_lock_the_page_down():
    h = viewpage.headers("n0nce")
    csp = h["Content-Security-Policy"]
    for part in ("default-src 'none'", "style-src 'nonce-n0nce'", "script-src 'nonce-n0nce'", "base-uri 'none'",
                 "form-action 'none'", "frame-ancestors 'none'"):
        assert part in csp
    assert "unsafe" not in csp and "*" not in csp and "http" not in csp
    assert "img-src data:;" in csp, "只为那个空图标; 别的图片照样加载不了"
    assert h["X-Robots-Tag"] == "noindex, nofollow" and h["Referrer-Policy"] == "no-referrer"
    assert h["Cache-Control"] == "private, no-store" and h["X-Content-Type-Options"] == "nosniff"


def test_generated_nonce_differs_per_render():
    a, b = viewpage.render(_page()), viewpage.render(_page())
    assert a != b and _Doc(a).named("script")[0]["nonce"] != _Doc(b).named("script")[0]["nonce"]


# ── 4. 复制出去的字和 delivery 是同一段代码 ──────────────────────────────────────────────────────

def test_template_project_copies_exactly_the_cells_delivery_assembles():
    tpl = delivery.validate(BLOCKS)
    drafts = _drafts(3)
    page = viewpage.build_page("示例项目", tpl, [{"created_at": None, "drafts": drafts}], expires_at=EXP)
    whole = delivery.assemble(tpl, drafts)["text"]
    assert "\n\n".join(p["copy_post"] + "\n" + p["copy_comments"] for p in page["posts"]) == whole, \
        "blocks 版式: 一篇一段就是这两份接起来"
    first = page["posts"][0]
    assert first["copy_post"] == "【标题】合成标题第0篇\n【正文】第0篇的第一段。\n\n第0篇的第二段。[捂脸R]\n#话题0甲"
    assert first["copy_comments"] == "读者：第0篇的读者评论\n作者回复：作者的回复"


def test_table_template_copies_the_same_text_the_table_cells_hold():
    tpl = delivery.validate(TABLE)
    drafts = _drafts(2)
    page = viewpage.build_page("示例项目", tpl, [{"created_at": None, "drafts": drafts}], expires_at=EXP)
    table_rows = delivery.assemble(tpl, drafts)["text"].split("\n")[2:]
    for post, row in zip(page["posts"], table_rows):
        _direction, post_cell, comments_cell = [c.strip() for c in row.strip("|").split(" | ")]
        assert post["copy_post"].replace("\n", "<br>") == post_cell
        assert post["copy_comments"].replace("\n", "<br>") == comments_cell
    assert page["posts"][0]["tags"] == ["话题0甲", "话题0乙", "固定话题"]


def test_project_without_a_template_copies_title_newline_body_and_tvlink_reads_it_back():
    drafts = [{"title": " 没有模板的标题 ", "body": "第一段。\n第二段。\n#话题甲 #话题乙\n", "tags": ["#话题甲"],
               "comments": [{"role": "reader", "text": "写手传了的评论"}], "direction": "一个方向"}]
    page = viewpage.build_page("示例项目", None, [{"created_at": None, "drafts": drafts}], expires_at=EXP)
    post = page["posts"][0]
    assert post["copy_post"] == "【标题】没有模板的标题\n【正文】第一段。\n第二段。\n#话题甲 #话题乙", "正文里已有的话题不再拼一遍"
    assert tvlink.parse_raw(post["copy_post"]) == ("没有模板的标题", "第一段。\n第二段。\n#话题甲 #话题乙")
    assert post["paragraphs"] == ["第一段。", "第二段。"] and post["tags"] == ["话题甲", "话题乙"], "显示时话题单独一行"
    assert post["copy_comments"] == "读者：写手传了的评论" and post["direction"] == "一个方向"
    assert post["comments"] == [{"role": "reader", "label": "读者：", "text": "写手传了的评论"}]


def test_project_without_a_template_keeps_the_tags_the_writer_passed_separately():
    """工具说明叫模型把话题放在 tags 字段里、不塞进正文; 没有模板时模型交稿自己会排上去 —— 页面和复制出去的字不能少这一行。
    正文末尾另有话题的, 两处的都显示, 不重复。"""
    drafts = [{"title": "话题单独传的一篇", "body": "第一段。\n第二段。", "tags": ["#话题甲", "话题乙[话题]#", "话题甲"]},
              {"title": "两处都有话题的一篇", "body": "只有一段。\n#话题丙", "tags": ["话题丁", "话题丙"]},
              {"title": "没带话题的一篇", "body": "只有一段。", "tags": []}]
    page = viewpage.build_page("示例项目", None, [{"created_at": None, "drafts": drafts}], expires_at=EXP)
    one, two, three = page["posts"]
    assert one["copy_post"] == "【标题】话题单独传的一篇\n【正文】第一段。\n第二段。\n#话题甲 #话题乙"
    assert one["tags"] == ["话题甲", "话题乙"] and one["paragraphs"] == ["第一段。", "第二段。"]
    assert two["copy_post"] == "【标题】两处都有话题的一篇\n【正文】只有一段。\n#话题丙\n#话题丁"
    assert two["tags"] == ["话题丙", "话题丁"] and two["paragraphs"] == ["只有一段。"]
    assert three["copy_post"] == "【标题】没带话题的一篇\n【正文】只有一段。" and three["tags"] == []
    html_text = viewpage.render(page)
    assert "#话题甲 #话题乙" in html_text and "#话题丙 #话题丁" in html_text


def test_a_label_line_above_the_title_marker_is_ignored_by_tvlink():
    """没有模板的项目默认的交付写法(core.PLAIN_LAYOUT_NEXT_STEP)在【标题】上面多一行「第N篇 · 场景」: tv-sync 拆的时候不认那一行。"""
    raw = "第1篇 · 合成的场景甲\n【标题】没有模板的标题\n【正文】第一段。\n第二段。"
    assert tvlink.parse_raw(raw) == ("没有模板的标题", "第一段。\n第二段。")


def test_template_without_comments_shows_none_even_if_the_writer_passed_some():
    page = _page({"layout": "blocks", "tags": {"content": 1}}, _drafts(2))
    assert all(p["comments"] == [] and p["copy_comments"] == "" for p in page["posts"])
    assert "wrap single" in viewpage.render(page)


def test_posts_are_numbered_across_batches_and_capped():
    tpl = None
    one = [{"title": f"甲{i}", "body": "正文"} for i in range(3)]
    two = [{"title": f"乙{i}", "body": "正文"} for i in range(2)]
    page = viewpage.build_page("示例项目", tpl, [{"created_at": "2026-11-12T02:00:00+00:00", "drafts": one},
                                               {"created_at": "2026-11-11T02:00:00+00:00", "drafts": two}], expires_at=EXP)
    assert [(p["n"], p["title"]) for p in page["posts"]] == [(1, "甲0"), (2, "甲1"), (3, "甲2"), (4, "乙0"), (5, "乙1")]
    assert page["created_at"] == "2026-11-11T02:00:00+00:00", "页面上的时间是最早那一批的"
    many = [{"title": f"第{i}篇", "body": "正文"} for i in range(viewpage.MAX_POSTS + 3)]
    big = viewpage.build_page("示例项目", None, [{"created_at": None, "drafts": many}], expires_at=EXP)
    assert len(big["posts"]) == viewpage.MAX_POSTS and big["more"] == 3
    out = viewpage.render(big)
    assert f"共 {viewpage.MAX_POSTS + 3} 篇笔记" in out and "还有 3 篇没排上" in out


def test_odd_draft_shapes_do_not_break_the_page():
    drafts = [None, {"title": None, "body": None}, {"title": 123, "body": ["不是字符串"], "tags": 5, "comments": 7, "kind": 9}]
    for tpl in (None, delivery.validate(BLOCKS)):
        page = viewpage.build_page(None, tpl, [{"created_at": 12345, "drafts": drafts}, {"drafts": None}], expires_at=EXP)
        assert len(page["posts"]) == 3
        assert "第3篇" in viewpage.render(page)


def test_delivery_rows_is_what_assemble_lays_out():
    """assemble 现在只管排版, 每篇的那几格由 rows 拼: 两边的 problems 也是同一份。"""
    tpl = delivery.validate(TABLE)
    drafts = _drafts(2)
    drafts[1]["tags"] = []
    rows, problems = delivery.rows(tpl, drafts, indices=[4, 7])
    assert problems == delivery.assemble(tpl, drafts, indices=[4, 7])["problems"] != []
    assert [set(r) for r in rows] == [{"post", "comments", "direction", "title", "body", "tags", "comment_items"}] * 2
    assert rows[0]["comments"].split("\n") == [c["label"] + c["text"] for c in rows[0]["comment_items"]]


# ── 这个模块不连库、不联网 ───────────────────────────────────────────────────────────────────────

def test_module_imports_nothing_that_talks_to_a_database_or_the_network():
    tree = ast.parse(pathlib.Path(viewpage.__file__).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names |= {(node.module or "").split(".")[0]} | {a.name for a in node.names}
    assert not names & {"db", "store", "core", "supabase", "httpx", "requests", "urllib", "socket", "dedup", "logging"}, names
