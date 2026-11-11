"""钥匙对不上时 401 自己说清为什么(2026-11-11, docs/deskcore.md §3.23; deskcore/identity.py)。

写内容的写手连不上, 看到的只有 ``401 invalid X-Deskcore-Key``, 谁也说不出错在哪。现在那一句只说**对方发来的那一串**长什么样
(长度、最后 4 位、有没有空格 / 引号 / 省略号 / 多出来的 Bearer / 非英文字符 / 示例占位)和怎么办。

钉住:
  1. ``key_shape`` 每一项怎么判, 以及拼出来的那一句原文;
  2. 它只看发来的那一串: 服务端配了哪几把、配了几把, 那一句一个字不变; 发来的那一串最多念回最后 4 位(不到 8 位时不念);
  3. 首尾的空白去掉再查(原样的先查); 中间的空格、引号、省略号、多出来的 Bearer 不替对方改, 照实 401;
  4. 原因是固定的一组; 计数只有个数和最后一次的时间; 日志那一行有长度和几个是 / 否, 没有钥匙、没有最后 4 位。
钥匙全是测试里现编的。HTTP 那一层在 tests/test_auth_rejection_http.py(要 fastapi)。
"""
from __future__ import annotations

import collections
import inspect
import json
import logging
import re

import pytest

from deskcore import identity

UID = "00000000-0000-4000-8000-000000000001"
GOOD = "k-shili-0123456789abcdefFAKE"                      # 现编的
OTHER = "k-lingyi-zyxwvutsrq9876543210"                    # 现编的

HOW = ("请对照发给你的那把钥匙, 把整把钥匙重新粘贴一遍: 不带空格、不带省略号, mcp.json 里钥匙外面只留一对引号; "
       "然后重启客户端、开一个新对话。")
NO_FLAGS = dict.fromkeys(("whitespace_around", "whitespace_inside", "quotes", "ellipsis", "bearer_prefix", "non_ascii",
                          "placeholder"), False)


def _keys(monkeypatch, keys: dict) -> None:
    monkeypatch.delenv("DESKCORE_API_KEY", raising=False)
    monkeypatch.delenv("DESKCORE_ALLOW_ANONYMOUS", raising=False)
    monkeypatch.setenv("DESKCORE_KEYS", json.dumps(keys, ensure_ascii=False))


@pytest.fixture()
def one_key(monkeypatch):
    _keys(monkeypatch, {GOOD: {"user_id": UID, "name": "示例写手"}})


@pytest.fixture()
def fresh_counts(monkeypatch):
    monkeypatch.setattr(identity, "_REJECTIONS", collections.Counter())
    monkeypatch.setattr(identity, "_LAST_REJECTION", {"at": None})


def _refused(provided) -> identity.AuthError:
    with pytest.raises(identity.AuthError) as exc:
        identity.resolve(provided)
    return exc.value


# ── 1. 形状 ──────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("provided, length, last4, flags, reason", [
    ("k-cuode-0000000000000000", 24, "0000", [], "invalid"),
    ("k-duan", 6, None, [], "invalid"),
    ("1234567", 7, None, [], "invalid"),
    ("12345678", 8, "5678", [], "invalid"),
    ("k-shili-0123...", 15, "3...", ["ellipsis"], "invalid_ellipsis"),
    ("k-shili-0123…", 13, "123…", ["ellipsis", "non_ascii"], "invalid_ellipsis"),
    ("k-shili-0123⋯", 13, "123⋯", ["ellipsis", "non_ascii"], "invalid_ellipsis"),
    ("k-shili-0123..", 14, "23..", [], "invalid"),
    ('"k-shili-0123456789"', 20, '789"', ["quotes"], "invalid_quotes"),
    ("'k-shili-0123456789", 19, "6789", ["quotes"], "invalid_quotes"),
    ("k-shili-0123456789”", 19, "789”", ["quotes", "non_ascii"], "invalid_quotes"),
    ("k-shi\"li-01234", 14, "1234", [], "invalid"),        # 引号在中间不算包着
    ("Bearer k-shili-0123456789", 25, "6789", ["bearer_prefix"], "invalid_bearer_prefix"),
    ("bearer\tk-shili-0123456789", 25, "6789", ["bearer_prefix"], "invalid_bearer_prefix"),
    ("BEARER k-shili 0123", 19, "0123", ["whitespace_inside", "bearer_prefix"], "invalid_bearer_prefix"),
    ("Bearer", 6, None, ["bearer_prefix"], "invalid_bearer_prefix"),
    ("Bearerk-shili-0123", 18, "0123", [], "invalid"),     # 没有空格隔开: 不是 Bearer 的写法
    (" k-shili-0123456789", 19, "6789", ["whitespace_around"], "invalid_whitespace"),
    ("k-shili-0123456789\u00a0", 19, "789␣", ["whitespace_around", "non_ascii"], "invalid_whitespace"),
    ("k-shili-0123456789\u3000", 19, "789␣", ["whitespace_around", "non_ascii"], "invalid_whitespace"),
    ("k-shili 0123456789", 18, "6789", ["whitespace_inside"], "invalid_whitespace"),
    ("k-shili-0123\n4567", 17, "4567", ["whitespace_inside"], "invalid_whitespace"),
    ("k-写手-0123456789", 15, "6789", ["non_ascii"], "invalid"),
    ("k-示例-0123456789", 15, "6789", ["non_ascii", "placeholder"], "invalid"),
    ("k-xxx", 5, None, ["placeholder"], "invalid"),
    ("k-XXX-0000", 10, "0000", ["placeholder"], "invalid"),
    ("<your-key>", 10, "key>", ["placeholder"], "invalid"),
    ("YOUR_KEY_HERE", 13, "HERE", ["placeholder"], "invalid"),
    ("<你的 key>", 8, "key>", ["whitespace_inside", "non_ascii", "placeholder"], "invalid_whitespace"),
])
def test_key_shape(provided, length, last4, flags, reason):
    shape = identity.key_shape(provided)
    assert shape["length"] == length
    assert shape["last4"] == last4
    assert identity.shape_flags(shape) == flags
    assert {k: v for k, v in shape.items() if k not in ("length", "last4")} == {**NO_FLAGS, **dict.fromkeys(flags, True)}
    assert identity._invalid_reason(shape) == reason


def test_last4_is_exactly_the_last_four_characters_and_only_from_eight_up():
    assert identity.key_shape("k-shili-…")["last4"] == "li-…"
    assert identity.key_shape('"Bearer k-xxx…"')["last4"] == 'xx…"'
    assert identity.key_shape("abcdefg")["last4"] is None and identity.key_shape("abcdefgh")["last4"] == "efgh"
    assert identity._LAST4_MIN_LENGTH == 8
    assert identity.key_shape("abcdefgh\x00\x7f\t ")["last4"] == "??␣␣", "看不见的字符不原样念回去"


def test_several_problems_at_once_pick_the_most_telling_reason():
    shape = identity.key_shape('"Bearer k-xxx…"')
    assert identity.shape_flags(shape) == ["whitespace_inside", "quotes", "ellipsis", "non_ascii", "placeholder"]
    assert identity._invalid_reason(shape) == "invalid_quotes", "引号包着的时候开头不是 Bearer"
    both = identity.key_shape("Bearer \"k-shili-…\" ")
    assert identity.shape_flags(both) == ["whitespace_around", "quotes", "ellipsis", "bearer_prefix", "non_ascii"]
    assert identity._invalid_reason(both) == "invalid_bearer_prefix"
    assert identity._invalid_reason(identity.key_shape("'k-shili-0123...' ")) == "invalid_quotes"
    assert identity._invalid_reason(identity.key_shape("k-shili 0123...")) == "invalid_ellipsis"


def test_utf8_bytes_read_as_latin1_are_put_back_before_measuring():
    """请求头到服务器是按 latin-1 解的: 客户端按 UTF-8 发的「…」变成三个怪字符。还原之后长度和最后 4 位才和写手配置里的对得上。"""
    as_received = "k-shili-…".encode("utf-8").decode("latin-1")
    assert len(as_received) == 11
    shape = identity.key_shape(as_received)
    assert shape["length"] == 9 and shape["ellipsis"] is True and shape["non_ascii"] is True
    assert identity.key_shape("k-caf\u00e9-0123")["length"] == 11, "本来就是 latin-1 的(还原不了)原样用"


@pytest.mark.parametrize("provided, detail", [
    ("k-cuode-0000000000000000",
     "invalid X-Deskcore-Key: 服务器不认识收到的这把钥匙(长度 24, 最后 4 位「0000」; 没看出空格、引号、省略号这类问题)。" + HOW),
    ("k-shili-…", "invalid X-Deskcore-Key: 服务器不认识收到的这把钥匙(长度 9, 最后 4 位「li-…」; 带省略号、有非英文字符)。" + HOW),
    ("k-xxx", "invalid X-Deskcore-Key: 服务器不认识收到的这把钥匙(长度 5; 像示例里的占位)。" + HOW),
    ('"' + GOOD + '"',
     "invalid X-Deskcore-Key: 服务器不认识收到的这把钥匙(长度 30, 最后 4 位「AKE\"」; 带引号)。" + HOW),
    ("Bearer " + GOOD,
     "invalid X-Deskcore-Key: 服务器不认识收到的这把钥匙(长度 35, 最后 4 位「FAKE」; 开头多了 Bearer(这一栏只放钥匙本身))。" + HOW),
    (GOOD[:10] + " " + GOOD[10:],
     "invalid X-Deskcore-Key: 服务器不认识收到的这把钥匙(长度 29, 最后 4 位「FAKE」; 中间有空格)。" + HOW),
    ("\u3000k-cuode-00000000 ",
     "invalid X-Deskcore-Key: 服务器不认识收到的这把钥匙(长度 18, 最后 4 位「000␣」; 首尾带空格、有非英文字符)。" + HOW),
])
def test_the_401_sentence(one_key, provided, detail):
    exc = _refused(provided)
    assert str(exc) == detail
    assert exc.shape == identity.key_shape(provided) and exc.reason == identity._invalid_reason(exc.shape)


def test_the_how_to_fix_sentence():
    assert identity.INVALID_KEY_HOW == HOW
    for must in ("对照发给你的那把钥匙", "整把钥匙重新粘贴一遍", "不带空格", "不带省略号", "只留一对引号", "重启客户端",
                 "开一个新对话"):
        assert must in HOW
    for bad in ("你写错", "你的错", "归档", "DESKCORE_KEYS", "Railway", "运维"):
        assert bad not in HOW and bad not in "".join(identity._FLAG_WORDS.values())


# ── 2. 只看发来的那一串 ──────────────────────────────────────────────────────────────────────────

def test_the_sentence_does_not_depend_on_which_keys_the_server_has(monkeypatch):
    """同一串发过来, 服务端配的是一把、三把、还是只差最后一位的一把, 回的那一句一模一样: 不比、不数、不提示。"""
    sent = GOOD[:-1] + "X"                                # 和 GOOD 只差最后一位
    details = set()
    for keys in ({GOOD: {"user_id": UID, "name": "示例写手"}},
                 {OTHER: {"user_id": UID, "name": "另一位"}},
                 {GOOD: {"user_id": UID, "name": "示例写手"}, OTHER: {"user_id": UID, "name": "另一位"},
                  "k-disan-00000000000000000000": {"user_id": UID, "name": "第三位"}}):
        _keys(monkeypatch, keys)
        exc = _refused(sent)
        details.add((str(exc), exc.reason, json.dumps(exc.shape, sort_keys=True)))
    assert len(details) == 1
    (detail, reason, _shape), = details
    assert reason == "invalid"
    for leaked in (GOOD, OTHER, GOOD[:6], "示例写手", "另一位", "key(s)", "3 把", "1 把", "接近", "差一位", "前几位"):
        assert leaked not in detail, leaked


def test_single_key_mode_says_the_same_sentence(monkeypatch):
    monkeypatch.delenv("DESKCORE_KEYS", raising=False)
    monkeypatch.setenv("DESKCORE_API_KEY", GOOD)
    monkeypatch.setenv("DESKCORE_DEFAULT_USER_ID", UID)
    sent = "k-cuode-0000000000000000"
    exc = _refused(sent)
    assert str(exc) == identity.invalid_key_detail(identity.key_shape(sent)) and exc.reason == "invalid"
    assert GOOD not in str(exc)
    assert identity.resolve(GOOD).user_id == UID
    assert identity.resolve(GOOD + " \t").user_id == UID, "单钥匙模式也去首尾空白"


def test_key_shape_takes_nothing_but_the_string_it_was_sent():
    """界线靠签名守着: 往这几个函数里传 key map 就是开始比对了。"""
    assert list(inspect.signature(identity.key_shape).parameters) == ["provided"]
    assert list(inspect.signature(identity.invalid_key_detail).parameters) == ["shape"]
    assert list(inspect.signature(identity._invalid_key).parameters) == ["provided"]
    for fn in (identity.key_shape, identity.invalid_key_detail, identity._invalid_key, identity._invalid_reason,
               identity.shape_flags, identity._as_typed):
        src = inspect.getsource(fn)
        assert "_key_map" not in src and "environ" not in src and "DESKCORE_KEYS" not in src, fn.__name__


@pytest.mark.parametrize("sent", [
    "k-cuode-abcdefghijklmnopqrstuvwxyz", "Bearer k-cuode-abcdefghijklmnopqrstuvwxyz",
    '"k-cuode-abcdefghijklmnopqrstuvwxyz"', "k-cuode-abcdefghijklmnop…", "k cuode abcdefghijklmnopqrstuvwxyz", "abcdefg",
])
def test_no_more_than_the_last_four_characters_are_echoed(one_key, sent):
    detail = str(_refused(sent))
    longest = max((n for n in range(1, len(sent) + 1)
                   for i in range(len(sent) - n + 1) if sent[i:i + n] in detail), default=0)
    # 单个常见字符(字母、引号)在那句话里本来就有; 钥匙里连着 5 个字出现在回的话里才算念回去了
    assert longest <= 4 or sent[:6] == "Bearer" and longest <= 6, (longest, detail)
    if sent.startswith("Bearer"):
        assert sent[7:12] not in detail
    if len(sent) < 8:
        assert "最后 4 位" not in detail and sent[-4:] not in detail


# ── 3. 去首尾空白, 别的不动 ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("wrap", ["{} ", " {}", "\t{}\t", "{}\u00a0", "\u3000{}", "{}\r\n", " \u00a0{}\u2003 "])
def test_surrounding_whitespace_is_stripped_before_lookup(one_key, wrap):
    caller = identity.resolve(wrap.format(GOOD))
    assert caller.user_id == UID and caller.name == "示例写手" and caller.authenticated is True


@pytest.mark.parametrize("wrap", ["{}\u00a0", "\u3000{}", "\u00a0{}\u3000 "])
def test_whitespace_that_arrived_as_utf8_bytes_is_stripped_too(one_key, wrap):
    """客户端按 UTF-8 发的不间断空格、全角空格, 到服务器是两三个 latin-1 怪字符: 还原之后再去。"""
    as_received = wrap.format(GOOD).encode("utf-8").decode("latin-1")
    assert as_received.strip() != GOOD, "直接去空白去不干净"
    assert identity.resolve(as_received).user_id == UID
    assert identity._lookup_forms(as_received)[-1] == GOOD
    garbled = (GOOD + "é").encode("utf-8").decode("latin-1")      # 还原出来多了一个字: 不是这把钥匙
    assert _refused(garbled).reason == "invalid"


@pytest.mark.parametrize("wrap, reason", [
    ('"{}"', "invalid_quotes"), ("'{}'", "invalid_quotes"), ("“{}”", "invalid_quotes"),
    ("Bearer {}", "invalid_bearer_prefix"), ("bearer {}", "invalid_bearer_prefix"),
    ("{}…", "invalid_ellipsis"), ("{}...", "invalid_ellipsis"),
    ("{}x", "invalid"), ("x{}", "invalid"), ("{},", "invalid"),
])
def test_nothing_else_is_normalised(one_key, wrap, reason):
    """引号、多出来的 Bearer、省略号: 里面包着的就算是对的钥匙也不替对方剥 —— 照实 401, 说出来。"""
    assert _refused(wrap.format(GOOD)).reason == reason


def test_inner_whitespace_and_case_are_not_normalised(one_key):
    assert _refused(GOOD[:8] + " " + GOOD[8:]).reason == "invalid_whitespace"
    assert _refused(GOOD.upper()).reason == "invalid" and _refused(GOOD.lower()).reason == "invalid"


def test_the_exact_form_is_tried_first_and_an_empty_string_is_never_looked_up(monkeypatch):
    spaced = GOOD + " "
    _keys(monkeypatch, {spaced: {"user_id": UID, "name": "带空格的"}, "": {"user_id": UID, "name": "空的"}})
    assert identity.resolve(spaced).name == "带空格的", "原样对得上的照旧过(原来能过的一个不少)"
    assert identity._lookup_forms(spaced) == [spaced, GOOD] and identity._lookup_forms(GOOD) == [GOOD]
    assert identity._lookup_forms(" \u00a0 ") == [" \u00a0 "], "去完是空的不查"
    exc = _refused(" \u00a0 ")
    assert exc.reason == "invalid_whitespace" and exc.shape["length"] == 3
    assert _refused("").reason == "missing" and _refused(None).reason == "missing"


# ── 4. 原因、计数、日志 ──────────────────────────────────────────────────────────────────────────

def test_the_reasons_are_a_fixed_set():
    assert identity.AUTH_REJECT_REASONS == (
        "missing", "invalid", "invalid_whitespace", "invalid_quotes", "invalid_ellipsis", "invalid_bearer_prefix",
        "query_param", "misconfigured", "other")
    assert identity.AuthError("x").reason == "other" and identity.AuthError("x").shape is None
    assert identity.AuthError("x", reason="现编的原因").reason == "other", "不在那一组里的一律记 other"


@pytest.mark.parametrize("setup, provided, reason, starts", [
    ({"DESKCORE_KEYS": "{not json"}, GOOD, "misconfigured", "server auth misconfigured"),
    ({"DESKCORE_KEYS": json.dumps({GOOD: UID})}, GOOD, "misconfigured", "server auth misconfigured"),
    ({}, GOOD, "misconfigured", "server auth not configured"),
    ({"DESKCORE_KEYS": json.dumps({GOOD: {"user_id": "", "name": "n"}})}, GOOD, "misconfigured", "DESKCORE_KEYS entry"),
    ({"DESKCORE_API_KEY": GOOD}, GOOD, "misconfigured", "DESKCORE_API_KEY is set"),
    ({"DESKCORE_KEYS": json.dumps({GOOD: {"user_id": UID}})}, None, "missing", "missing X-Deskcore-Key"),
    ({"DESKCORE_KEYS": json.dumps({GOOD: {"user_id": UID}})}, "", "missing", "missing X-Deskcore-Key"),
])
def test_every_other_refusal_has_its_reason_and_no_shape(monkeypatch, setup, provided, reason, starts):
    for var in ("DESKCORE_KEYS", "DESKCORE_API_KEY", "DESKCORE_DEFAULT_USER_ID", "DESKCORE_ALLOW_ANONYMOUS"):
        monkeypatch.delenv(var, raising=False)
    for k, v in setup.items():
        monkeypatch.setenv(k, v)
    exc = _refused(provided)
    assert exc.reason == reason and exc.shape is None and str(exc).startswith(starts)


def test_rejections_are_counted_by_reason_with_the_time_of_the_last_one(one_key, fresh_counts):
    assert identity.auth_rejections() == {"since_start": dict.fromkeys(identity.AUTH_REJECT_REASONS, 0), "last_at": None}
    for sent in ("k-cuode-0000000000000000", "k-shili-…", "k-shili-...", '"' + GOOD + '"', "Bearer " + GOOD,
                 GOOD + " x", None):
        identity.record_rejection(_refused(sent), path="/tool/list_projects", via="header")
    identity.record_rejection(identity.AuthError("?key= …", reason="query_param"))
    identity.record_rejection(identity.AuthError("x"))
    identity.record_rejection(RuntimeError("不是 AuthError 也不许抛"))
    out = identity.auth_rejections()
    assert out["since_start"] == {"missing": 1, "invalid": 1, "invalid_whitespace": 1, "invalid_quotes": 1,
                                  "invalid_ellipsis": 2, "invalid_bearer_prefix": 1, "query_param": 1, "misconfigured": 0,
                                  "other": 2}
    assert list(out["since_start"]) == list(identity.AUTH_REJECT_REASONS)
    assert set(out) == {"since_start", "last_at"}
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", out["last_at"])
    assert all(type(v) is int for v in out["since_start"].values())
    text = json.dumps(out, ensure_ascii=False)
    for leaked in (GOOD, "FAKE", "0000", "示例写手", "/tool/"):
        assert leaked not in text, leaked


def test_one_warning_line_per_rejection_with_length_and_flags_but_no_key_material(one_key, fresh_counts, caplog):
    sent = '"k-shili-0123456789abcdefFAK…"'
    with caplog.at_level(logging.WARNING, logger="deskcore"):
        identity.record_rejection(_refused(sent), path="/mcp/", via="header")
    (rec,) = [r for r in caplog.records if r.getMessage().startswith("auth rejected")]
    assert rec.levelno == logging.WARNING
    assert rec.getMessage() == ("auth rejected: reason=invalid_quotes path=/mcp/ via=header len=30 "
                                "flags=quotes,ellipsis,non_ascii")
    shape = identity.key_shape(sent)
    assert shape["last4"] == 'AK…"'
    for leaked in (sent, shape["last4"], "AK…", "k-shili", GOOD, "示例写手"):
        assert leaked not in caplog.text, leaked


def test_the_log_line_for_a_missing_key_and_a_hostile_path(one_key, fresh_counts, caplog):
    with caplog.at_level(logging.WARNING, logger="deskcore"):
        identity.record_rejection(_refused(None), path="/tool/x\ninjected line " + "a" * 200, via="none")
    (rec,) = [r for r in caplog.records if r.getMessage().startswith("auth rejected")]
    msg = rec.getMessage()
    assert msg.startswith("auth rejected: reason=missing path=/tool/xinjectedline") and msg.endswith(" via=none len=- flags=-")
    assert "\n" not in msg and len(msg) < 200
