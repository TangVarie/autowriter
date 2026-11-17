"""每次工具调用、每个新 batch 记下是从哪个客户端来的(2026-11-17, deskcore/clientmark.py, docs/deskcore.md §3.37)。

这一份只测纯函数(不要 fastapi / mcp, CI 只装主依赖的那一步也跑): 洗 UA、套规则、词表是封闭的、钥匙不出门、出了意外是
unknown 而且只记一行 WARNING、initialize 报的 clientInfo 那张小表(指纹含协议版本、到期、撞车、上限、指纹管不到的那一种)、
先后、解析请求体、请求头只取第一个且 UA 只看前 512 个字符、batch 只拿两个词、绑定和 CLI 的进程默认。
走真 REST / MCP 的在 tests/test_client_mark_http.py。
全部是现编的 UA、现编的 clientInfo、测试里现编的钥匙; 不含任何真客户端的原串。
"""
from __future__ import annotations

import json
import logging
import random
import re
import time

import pytest

from deskcore import calllog, clientmark as cm
from deskcore.identity import Caller

ME = "11111111-1111-1111-1111-111111111111"
FAKE_KEY = "k-clientmark-0123456789abcdefFAKE"           # 测试里现编的, 不是任何真钥匙
WRITER_A = Caller(ME, "示例写手甲", True)
WRITER_B = Caller(ME, "示例写手乙", True)                  # 同一个团队身份, 另一把钥匙
SAFE = re.compile(r"[a-z0-9_.]{1,52}")
SAN_KEY = re.sub(r"[^a-z0-9]+", "_", FAKE_KEY.lower()).strip("_")
MIXED_KEY = "k-AbCdEfGhIjKlMnOpQrStUvWxYz012345"          # 现编的、大小写混着的钥匙(token_urlsafe 那种样子)
PV = "2025-06-18"                                         # _init 报的协议版本; 之后的请求头上带的也是它

ELECTRON_WB = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) WorkBuddy/4.7.3 "
               "Chrome/128.0.6613.186 Electron/32.2.5 Safari/537.36")
ELECTRON = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.6613.186 Electron/32.2.5 Safari/537.36")
CHROME = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
ELECTRON_WB_AFTER = ("Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.6613.186 "
                     "Electron/32.2.5 Safari/537.36 WorkBuddy/4.7.3")


@pytest.fixture(autouse=True)
def _fresh():
    cm.reset()
    yield
    cm.reset()


def _init(name, version="1.0.0", *, batch=False) -> bytes:
    msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
           "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                      "clientInfo": {"name": name, "version": version}}}
    return json.dumps([msg, {"jsonrpc": "2.0", "method": "notifications/initialized"}] if batch else msg).encode()


def _mcp(ua="node", origin=False, caller=WRITER_A, key=FAKE_KEY, pv=PV) -> dict:
    return cm.classify(channel="mcp", ua=ua, origin=origin, key=key, caller=caller, pv=pv).as_dict()


def _hello(name, version="1.0.0", ua="node", origin=False, caller=WRITER_A, key=FAKE_KEY, pv=PV, **kw) -> None:
    body = json.loads(_init(name, version))
    body["params"]["protocolVersion"] = pv
    cm.note_hello(json.dumps(body).encode(), ua=ua, origin=origin, key=key, caller=caller, **kw)


# ── 1. 洗 UA ───────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ua, want", [
    ("node", "node"),
    ("curl/8.5.0", "curl_v8.5.0"),
    ("python-httpx/0.28.1", "python_httpx_v0.28.1"),
    ("testclient", "testclient"),
    (ELECTRON_WB, "workbuddy_v4.7.3"),
    (ELECTRON_WB_AFTER, "workbuddy_v4.7.3"),                       # 产品 token 写在 Electron 后面: Electron 排最后
    (ELECTRON, "electron_v32.2.5"),
    (CHROME, "browser"),
    ("Mozilla/5.0", "browser"),
    ("claude-code/2.0.14", "claude_code_v2.0.14"),
    ("DSH/0.2.2-rc.3 node", "dsh_v0.2.2_rc.3"),                 # -rc: 连字符换成下划线
    ("Example-App/1.0.0-beta.2+build.7", "example_app_v1.0.0_beta.2buil"),   # 版本至多 16 个字符
    ("示例 客户端/1.0 \"k-NOTAKEY\" …", "other"),                 # 没有一个像样的 token
    ("(only a comment)", "other"),
    ("", "none"),
    ("   ", "none"),
    (None, "none"),
    (12345, "none"),
    ("x" * 100_000, "x" * 32),
    ("Mozilla/5.0 " + " " * 600 + "WorkBuddy/1", "browser"),        # 至多看 512 个字符
    ("A" * 40 + "/" + "9" * 40, "a" * 32 + "_v" + "9" * 16),
])
def test_ua_descriptor_table(ua, want):
    got = cm.ua_descriptor(ua)
    assert got == want
    assert SAFE.fullmatch(got) and calllog._WORD.fullmatch(got)


# ── 2. 规则 ─────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name, app", [
    ("dsh", "dsh_plugin"), ("dsh_mcp_client", "dsh_plugin"), ("deepseek_ai_dsh_mcp_client", "dsh_plugin"),
    ("deepseek_harness", "dsh_plugin"), ("deepseekharness", "dsh_plugin"),
    ("workbuddy", "workbuddy_skill"), ("codebuddy", "workbuddy_skill"), ("tencent_workbuddy_desktop", "workbuddy_skill"),
    ("claude_code", "claude_code"), ("claudecode", "claude_code"),
    ("dashboard", None), ("fdsh_tool", None), ("dshx", None), ("deepseek_api_client", None), ("deepseek", None),
    ("node", None), ("browser", None), ("electron", None), ("ci", None), ("other", None), ("none", None),
    ("", None), (None, None),
])
def test_app_for_rules(name, app):
    assert cm.app_for(name) == app


def test_the_run_together_deepseekharness_token_is_dsh():
    # UA 的产品 token 一般连写(没有空格也没有连字符): DeepSeekHarness/0.2.2, 单独的或者夹在 Electron 的 UA 里
    assert _mcp(ua="DeepSeekHarness/0.2.2")["app"] == "dsh_plugin"
    assert _mcp(ua=ELECTRON.replace("Electron/", "DeepSeekHarness/0.2.2 Electron/")) == {
        "app": "dsh_plugin", "basis": "user_agent", "ua": "deepseekharness_v0.2.2", "origin": False}
    _hello("DeepSeekHarness", "0.2.2")
    assert _mcp() == {"app": "dsh_plugin", "basis": "hello", "ua": "node", "origin": False,
                      "hello": "deepseekharness_v0.2.2"}


def test_the_rule_looks_at_the_name_not_the_version_suffix():
    # 名字是 dsh_v2(一个叫这个的产品), 版本另算: 套规则只看洗过的名字
    assert cm.classify(channel="mcp", ua="dsh-v2/1.0", caller=WRITER_A).app == "dsh_plugin"
    assert cm.classify(channel="mcp", ua="tool/dsh", caller=WRITER_A).app == "other_mcp", "版本里有 dsh 不算"


# ── 3. 词表是封闭的: 乱七八糟的请求头进来, 出去的永远是那几个词和合规矩的短串 ──────────────────────────────

def _junk(rng: random.Random) -> str:
    pieces = ["Mozilla/5.0", "(Windows; x64)", "WorkBuddy/1.2", "示例客户端/1.0", "\"quoted text\"", "…", "\x00\x07",
              "dsh/0.1", "claude-code/9", "Electron/32", "a/b/c", "((nested (deep)))", ")(", "\t", "x" * rng.randint(0, 3000),
              "Chrome/1", "测试 标记", "k-" + "z" * rng.randint(0, 40), "%00%ff", "‮", FAKE_KEY[: rng.randint(0, 40)]]
    return " ".join(rng.choice(pieces) for _ in range(rng.randint(0, 12)))


def test_the_vocabulary_is_closed_for_any_input():
    rng = random.Random(20261117)
    huge = "Mozilla/5.0 " + "y" * 1_000_000
    for i in range(500):
        ua = huge if i % 97 == 0 else _junk(rng)
        hello_name = _junk(rng) or None
        t = time.perf_counter()
        cm.note_hello(_init(hello_name or "", _junk(rng)[:40]), ua=ua, origin=rng.random() < 0.5, key=FAKE_KEY,
                      caller=WRITER_A)
        out = cm.classify(channel=rng.choice(["mcp", "rest", "other"]), ua=ua, origin=rng.random() < 0.5,
                          key=FAKE_KEY, caller=rng.choice([WRITER_A, WRITER_B, None])).as_dict()
        assert (time.perf_counter() - t) < 0.05, "一次分类(含记小表)不许超过 50 ms"
        assert out["app"] in cm.APPS and out["basis"] in cm.BASES
        assert set(out) <= {"app", "basis", "ua", "origin", "hello"}
        for k in ("ua", "hello"):
            if k in out:
                assert SAFE.fullmatch(out[k]) and calllog._WORD.fullmatch(out[k]), out
        if "origin" in out:
            assert isinstance(out["origin"], bool)
        assert calllog._clean({cm.KEY: out}) == {cm.KEY: out}, "过调用留痕那一道筛一个键都不少"
        assert FAKE_KEY.lower() not in json.dumps(out).lower()
        washed = "".join(out.get(k, "") for k in ("ua", "hello"))
        assert not any(SAN_KEY[j:j + 8] in washed for j in range(len(SAN_KEY) - 7)), ("钥匙的一段洗过之后出了门", out)


def test_as_dict_rechecks_its_own_output():
    assert cm.Mark("made_up_app", "user_agent").as_dict() == {"app": "unknown", "basis": "none"}
    assert cm.Mark("rest", "made_up_basis").as_dict() == {"app": "unknown", "basis": "none"}
    bad = cm.Mark("rest", "transport", ua="Has Space", origin="yes", hello="a-b").as_dict()
    assert bad == {"app": "rest", "basis": "transport"}, "不合规矩的串和不是布尔的 origin 都丢掉"
    assert cm.Mark("rest", "transport", ua="x" * 53).as_dict() == {"app": "rest", "basis": "transport"}


# ── 4. 钥匙不出门 ───────────────────────────────────────────────────────────────────────────────────

def test_a_key_inside_the_user_agent_or_client_info_is_never_returned():
    for ua in (f"{FAKE_KEY}/1.0", f"node {FAKE_KEY}", f"Tool/1 ({FAKE_KEY})", f"{FAKE_KEY.upper()}/2",
               f"Mozilla/5.0 ({FAKE_KEY})", f'"{FAKE_KEY}"'):
        assert cm.ua_descriptor(ua, key=FAKE_KEY) == "other"
        out = cm.classify(channel="mcp", ua=ua, key=FAKE_KEY, caller=WRITER_A).as_dict()
        assert out == {"app": "other_mcp", "basis": "transport", "ua": "other", "origin": False}
    # 换了分隔符的钥匙、截短了的钥匙(洗完还有连续 8 个字符和钥匙洗完一样)也不出门
    assert cm.ua_descriptor(FAKE_KEY.replace("-", ".") + "/1", key=FAKE_KEY) == "other"
    assert cm.ua_descriptor("Tool/" + FAKE_KEY[10:20], key=FAKE_KEY) == "other"
    assert cm.ua_descriptor("x" + FAKE_KEY[2:12].upper() + "/1", key=FAKE_KEY) == "other"
    # Bearer 那种传法: 钥匙从 Authorization 里取
    ua, origin, keys, pv = cm.header_signals([(b"user-agent", f"x {FAKE_KEY}".encode()),
                                              (b"authorization", f"Bearer {FAKE_KEY}".encode())])
    assert keys == [FAKE_KEY] and cm.ua_descriptor(ua, key=keys) == "other" and pv is None
    assert cm.hello_descriptor(f"dsh {FAKE_KEY}", "1", key=FAKE_KEY) is None
    assert cm.hello_descriptor("dsh", FAKE_KEY, key=FAKE_KEY) is None
    cm.note_hello(_init(f"dsh-mcp-client {FAKE_KEY}"), ua="node", key=FAKE_KEY, caller=WRITER_A)
    assert _mcp() == {"app": "other_mcp", "basis": "transport", "ua": "node", "origin": False}, "带钥匙的 clientInfo 不记"


@pytest.mark.parametrize("frag", [MIXED_KEY[2:14], MIXED_KEY[2:14].upper(), MIXED_KEY[10:22].lower()])
def test_a_key_fragment_is_caught_whatever_its_letter_case(frag):
    # 钥匙大小写混着(token_urlsafe 那种): 原串里的一段和洗过(全小写)的输出比之前都要先转小写, 不然 12 个字符的一段会出门
    assert cm.ua_descriptor("Tool/" + frag, key=MIXED_KEY) == "other"
    assert cm.ua_descriptor(frag + "/1", key=MIXED_KEY) == "other"
    assert cm.hello_descriptor(frag, "1", key=MIXED_KEY) is None
    assert cm.hello_descriptor("dsh", frag, key=MIXED_KEY) is None
    assert cm.ua_descriptor("Tool/1.0", key=MIXED_KEY) == "tool_v1.0", "不相干的 UA 照常洗"


# ── 5. 出了意外: unknown, 不抛, 只一行 WARNING ─────────────────────────────────────────────────────

def test_a_failure_gives_unknown_and_one_rate_limited_warning(monkeypatch, caplog):
    def boom(*_a, **_k):
        raise RuntimeError(f"secret header text {FAKE_KEY}")
    monkeypatch.setattr(cm, "_ua_parts", boom)
    caplog.set_level(logging.DEBUG)
    for _ in range(3):
        mark = cm.classify(channel="mcp", ua="node WorkBuddy/1", origin=True, key=FAKE_KEY, caller=WRITER_A)
        assert mark is cm.UNKNOWN and mark.as_dict() == {"app": "unknown", "basis": "none"}
        cm.note_hello(_init("dsh"), ua="node", key=FAKE_KEY, caller=WRITER_A)         # 同样吞掉
    recs = [r for r in caplog.records if r.name == "deskcore.clientmark"]
    assert len(recs) == 1 and recs[0].levelno == logging.WARNING, "5 分钟里只记一行"
    assert recs[0].getMessage() == "client mark: classification failed (RuntimeError)"
    assert FAKE_KEY not in caplog.text and "secret header text" not in caplog.text and "WorkBuddy" not in caplog.text


def test_the_asgi_entry_points_never_raise(monkeypatch):
    monkeypatch.setattr(cm, "classify", lambda **_k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(cm, "note_hello", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")))
    assert cm.classify_headers("mcp", [(b"user-agent", b"node")], WRITER_A) is cm.UNKNOWN
    assert cm.note_hello_headers(_init("dsh"), [(b"user-agent", b"node")], WRITER_A) is None
    assert cm.classify_headers("mcp", object(), WRITER_A) is cm.UNKNOWN, "请求头不是一串 (bytes, bytes) 也不抛"


# ── 6. clientInfo 的小表 ─────────────────────────────────────────────────────────────────────────────

def test_hello_applies_to_the_same_connection_fingerprint_only():
    _hello("dsh-mcp-client", "0.2.2")
    assert _mcp() == {"app": "dsh_plugin", "basis": "hello", "ua": "node", "origin": False,
                      "hello": "dsh_mcp_client_v0.2.2"}
    other = {"app": "other_mcp", "basis": "transport"}
    assert _mcp(caller=WRITER_B) == {**other, "ua": "node", "origin": False}, "另一把钥匙"
    assert _mcp(origin=True) == {**other, "ua": "node", "origin": True}, "带 Origin 的是另一个客户端"
    assert _mcp(ua="undici") == {**other, "ua": "undici", "origin": False}, "另一个 UA"
    assert _mcp(caller=Caller("22222222-2222-2222-2222-222222222222", "示例写手甲", True))["app"] == "other_mcp"


def test_rest_never_reads_the_hello_table():
    _hello("dsh-mcp-client", "0.2.2")
    out = cm.classify(channel="rest", ua="node", key=FAKE_KEY, caller=WRITER_A).as_dict()
    assert out == {"app": "rest", "basis": "transport", "ua": "node", "origin": False}


def test_hello_expires_after_a_day(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(cm, "_clock", lambda: now[0])
    _hello("dsh-mcp-client")
    now[0] += cm.HELLO_TTL_SEC - 1
    assert _mcp()["app"] == "dsh_plugin"
    now[0] += 2
    assert _mcp() == {"app": "other_mcp", "basis": "transport", "ua": "node", "origin": False}
    assert not cm._HELLO, "过期的那一项读到时就删掉"


def test_two_different_clients_on_one_fingerprint_are_ambiguous_until_it_expires(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(cm, "_clock", lambda: now[0])
    _hello("dsh-mcp-client")
    _hello("WorkBuddy")
    want = {"app": "other_mcp", "basis": "transport", "ua": "node", "origin": False}
    assert _mcp() == want, "说不清: 哪个都不用, hello 也不报"
    now[0] += cm.HELLO_TTL_SEC / 2
    _hello("dsh-mcp-client")
    _hello("generic-client")
    assert _mcp() == want, "说不清的到期之前一直说不清(再来一个认不得的也不会把它洗成干净的一条)"
    now[0] += cm.HELLO_TTL_SEC / 2 + 1
    _hello("dsh-mcp-client")
    assert _mcp()["app"] == "dsh_plugin", "到期(从记成说不清那一刻算 24 小时)之后重新记"
    # 认得的和认不得的撞上也算说不清(不能把那个认不得的客户端记成 dsh)
    _hello("some-generic-mcp-client", caller=WRITER_B)
    _hello("dsh-mcp-client", caller=WRITER_B)
    assert _mcp(caller=WRITER_B) == want


def test_the_same_client_with_a_new_version_updates_the_entry():
    _hello("dsh-mcp-client", "0.2.2")
    _hello("dsh-mcp-client", "0.3.0")
    assert _mcp() == {"app": "dsh_plugin", "basis": "hello", "ua": "node", "origin": False,
                      "hello": "dsh_mcp_client_v0.3.0"}
    _hello("generic-client", "1", caller=WRITER_B)
    _hello("generic-client", "2", caller=WRITER_B)
    assert _mcp(caller=WRITER_B) == {"app": "other_mcp", "basis": "transport", "ua": "node", "origin": False,
                                     "hello": "generic_client_v2"}, "认不得的 clientInfo 也当证据带上"


def test_the_table_is_capped_and_drops_the_oldest_first():
    for i in range(cm.HELLO_MAX + 5):
        _hello("dsh-mcp-client", caller=Caller(ME, f"writer{i}", True))
    assert len(cm._HELLO) == cm.HELLO_MAX
    assert _mcp(caller=Caller(ME, "writer0", True))["app"] == "other_mcp", "最早的那几项丢了"
    assert _mcp(caller=Caller(ME, f"writer{cm.HELLO_MAX + 4}", True))["app"] == "dsh_plugin"


def test_two_unrecognised_clients_on_one_fingerprint_are_ambiguous_too():
    # 两个认不得的、名字不一样: 原来后 initialize 的那个顶掉前一个, a 的调用带着 b 的 hello。现在说不清, hello 不报
    _hello("client-a", "1")
    _hello("client-b", "1")
    assert _mcp() == {"app": "other_mcp", "basis": "transport", "ua": "node", "origin": False}
    _hello("client-a", "2")
    assert "hello" not in _mcp(), "说不清的到期之前一直说不清"


def test_the_protocol_version_is_part_of_the_fingerprint():
    # 部署之后(小表是空的)另一个客户端 X 一直在调、没重新 initialize; 它谈定的协议版本和 DSH 的不一样
    x = dict(ua="node", pv="2025-11-25")
    assert _mcp(**x) == {"app": "other_mcp", "basis": "transport", "ua": "node", "origin": False}
    _hello("dsh-mcp-client", "0.2.2", pv="2025-06-18")                # DSH 新开一场对话
    assert _mcp(**x) == {"app": "other_mcp", "basis": "transport", "ua": "node", "origin": False}, \
        "X 的调用不会算到 DSH 头上"
    assert _mcp(pv="2025-06-18")["app"] == "dsh_plugin", "DSH 自己的调用照常"
    # 2025-06-18 起的客户端每个请求都带头: 没带头的调用对不上它的 initialize
    assert _mcp(pv=None)["app"] == "other_mcp"
    assert _mcp(pv="not-a-date")["app"] == "other_mcp"


def test_old_protocol_versions_without_the_header_still_match():
    # 2025-06-18 之前的版本不要求带 MCP-Protocol-Version 头(SDK 当 2025-03-26): 没带头的调用和它们的 initialize 对得上
    _hello("dsh-mcp-client", pv="2025-03-26")
    assert _mcp(pv=None)["basis"] == "hello"
    assert _mcp(pv="2025-03-26")["basis"] == "hello"
    cm.reset()
    _hello("dsh-mcp-client", pv="2024-11-05")
    assert _mcp(pv=None)["app"] == "dsh_plugin"
    cm.reset()
    _hello("dsh-mcp-client", pv=None)                                 # 没有字符串的 protocolVersion: SDK 拒这个 initialize
    assert not cm._HELLO


def test_the_fingerprint_uses_the_negotiated_version():
    # 客户端要的版本服务端不支持: SDK 回最新的那一版, 客户端之后带的也是它 —— 指纹按谈定的那一版记
    sup, latest = ("2025-03-26", "2025-06-18", "2025-11-25"), "2025-11-25"
    _hello("dsh-mcp-client", pv="2099-01-01", supported=sup, latest=latest)
    assert _mcp(pv="2025-11-25")["basis"] == "hello"
    assert _mcp(pv="2099-01-01")["basis"] == "transport"
    assert cm.negotiated_version("2025-06-18", sup, latest) == "2025-06-18"
    assert cm.negotiated_version("2099-01-01", None, None) == "2099-01-01", "不给支持列表就当是要的那一版"


def test_the_documented_limit_a_client_that_never_initializes_inherits_the_label():
    # docs/deskcore.md §3.37「指纹管不到的那一种」: 同一个指纹(同一把钥匙、同一个 UA 词、同样有没有 Origin、同一版协议)上
    # 一个从不 initialize 的客户端服务端看不见, 它的调用算到这个指纹最近一次 initialize 的那个客户端头上。文档就是这么写的;
    # 这一条钉住「文档和代码说的是同一件事」, 改了行为要一起改文档
    before = _mcp()
    _hello("dsh-mcp-client")
    assert before["basis"] == "transport" and _mcp()["basis"] == "hello"
    assert _mcp(caller=WRITER_B)["basis"] == "transport", "单发一把钥匙(备注名在指纹里)就分开了"


# ── 7. 先后 ─────────────────────────────────────────────────────────────────────────────────────────

def test_a_specific_user_agent_beats_hello_and_hello_beats_transport():
    _hello("dsh-mcp-client", ua="WorkBuddy/4.7.3")
    out = _mcp(ua="WorkBuddy/4.7.3")
    assert out == {"app": "workbuddy_skill", "basis": "user_agent", "ua": "workbuddy_v4.7.3", "origin": False,
                   "hello": "dsh_mcp_client_v1.0.0"}, "UA 定的; 小表里那一条照样当证据带上"
    _hello("dsh-mcp-client", ua="node")
    assert _mcp(ua="node")["basis"] == "hello"
    assert _mcp(ua="curl/8")["basis"] == "transport"
    assert cm.classify(channel="mystery", ua="node", caller=WRITER_A).as_dict() == {
        "app": "unknown", "basis": "none", "ua": "node", "origin": False}


# ── 8. 解析请求体 ───────────────────────────────────────────────────────────────────────────────────

def test_note_hello_reads_single_and_batched_initialize():
    cm.note_hello(_init("dsh-mcp-client", batch=True), ua="node", key=FAKE_KEY, caller=WRITER_A)
    assert _mcp()["app"] == "dsh_plugin"


@pytest.mark.parametrize("body", [
    b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
    b'{"jsonrpc":"2.0","method":"notifications/initialized"}',
    b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"capabilities":{}}}',
    b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":"dsh"}}',
    b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":""}}}',
    b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":42}}}',
    b'{"method":"initialize", not json',
    b"[" * 50_000 + b'"initialize"',
    _init("dsh-mcp-client" + " " * cm.HELLO_BODY_MAX),
    "initialize".encode() + b"\xff\xfe",
    None,
    "a str, not bytes: initialize",
])
def test_note_hello_ignores_everything_else(body, caplog):
    caplog.set_level(logging.DEBUG)
    cm.note_hello(body, ua="node", key=FAKE_KEY, caller=WRITER_A)
    assert not cm._HELLO
    assert not [r for r in caplog.records if r.name == "deskcore.clientmark"], "解不出来是正常情况, 不记日志"


def test_an_initialize_without_a_protocol_version_is_ignored():
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"capabilities": {},
                                                                          "clientInfo": {"name": "dsh", "version": "1"}}}
    cm.note_hello(json.dumps(body).encode(), ua="node", key=FAKE_KEY, caller=WRITER_A)
    assert not cm._HELLO


def test_only_bodies_with_the_quoted_method_name_are_parsed(monkeypatch):
    # tools/call 的稿子里有 initialize 这个词不算: 只有带 "initialize"(JSON 字符串, 引号也在)的请求体才解析
    seen = []
    real = cm.json.loads
    monkeypatch.setattr(cm.json, "loads", lambda b, *a, **k: seen.append(len(b)) or real(b, *a, **k))
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "check_drafts", "arguments": {"drafts": [{"body": "how to initialize a habit"}]}}}
    cm.note_hello(json.dumps(call).encode(), ua="node", key=FAKE_KEY, caller=WRITER_A)
    cm.note_hello_headers(json.dumps(call).encode(), [(b"user-agent", b"node")], WRITER_A)
    assert seen == []
    cm.note_hello(_init("dsh"), ua="node", key=FAKE_KEY, caller=WRITER_A)
    assert len(seen) == 1


def test_note_hello_sanitizes_the_client_name():
    cm.note_hello(_init("示例 DSH 客户端 (测试)", "v1.0-测试"), ua="node", key=FAKE_KEY, caller=WRITER_A)
    out = _mcp()
    assert out["app"] == "dsh_plugin" and out["hello"] == "dsh_vv1.0"
    cm.note_hello(_init("示例客户端", "1"), ua="node", key=FAKE_KEY, caller=WRITER_B)
    assert _mcp(caller=WRITER_B)["hello"] == "other", "名字洗完是空的: other"


# ── 8b. 跑在事件循环上: 只看每种头的第一个、UA 的前 512 个字符 ─────────────────────────────────────────

def _flood(n_keys=1000, ua_len=30_000) -> list:
    return ([(b"user-agent", ("Tool/1.0 " + "a" * ua_len).encode()), (b"origin", b"https://example.invalid"),
             (b"mcp-protocol-version", PV.encode()), (b"mcp-protocol-version", b"2025-11-25")]
            + [(b"x-deskcore-key", f"zz{i:06d}synthetic".encode()) for i in range(n_keys)]
            + [(b"authorization", f"Bearer bb{i:06d}synthetic".encode()) for i in range(n_keys)])


def test_only_the_first_key_and_bearer_are_read_like_auth_does():
    ua, origin, keys, pv = cm.header_signals(_flood(n_keys=50, ua_len=10))
    assert keys == ["zz000000synthetic", "bb000000synthetic"], "鉴权只认第一个(Starlette 的 headers.get), 这里也是"
    assert origin is True and pv == PV and ua.startswith("Tool/1.0")
    _, _, keys, _ = cm.header_signals([(b"authorization", b"Basic abc"), (b"authorization", b"Bearer later")])
    assert keys == [], "第一个 Authorization 不是 Bearer: 鉴权不认后面的, 这里也不认"


def test_the_key_search_only_reads_the_first_512_characters(monkeypatch):
    # 输出只从前 512 个字符来: 钥匙在那之后出不了门, 也不用为它把整串转小写、扫一遍
    assert cm.ua_descriptor("Foo/1.0 " + "x" * 600 + " " + FAKE_KEY, key=FAKE_KEY) == "foo_v1.0"
    assert cm.ua_descriptor("Foo/1.0 " + FAKE_KEY, key=FAKE_KEY) == "other", "前 512 个字符里的照样挡"
    lens = []
    real = cm._leaks
    monkeypatch.setattr(cm, "_leaks", lambda raw, out, keys: lens.append((len(raw), len(keys))) or real(raw, out, keys))
    cm.classify_headers("mcp", _flood(), WRITER_A)
    assert lens and all(n <= cm.UA_MAX_IN and k <= 2 for n, k in lens), lens


def test_a_header_flood_costs_about_the_same_as_one_key():
    # 一个已经过了鉴权的客户端发一千个重复的钥匙头 + 30 KB 的 UA: 原来一次分类要二十几毫秒(都在事件循环上), 现在和一把钥匙差不多
    flood = _flood()
    best = min(_timed(lambda: cm.classify_headers("mcp", flood, WRITER_A)) for _ in range(5))
    assert best < 0.010, f"{best * 1000:.1f} ms"


def _timed(fn) -> float:
    t = time.perf_counter()
    fn()
    return time.perf_counter() - t


# ── 9. 绑定、CLI 的进程默认 ─────────────────────────────────────────────────────────────────────────

def test_current_order_and_process_default_restore():
    assert cm.current() is cm.UNKNOWN and cm.for_batch() == {"app": "unknown", "basis": "none"}
    with cm.process_default(cm.SERVER_JOB):
        assert cm.for_batch() == {"app": "server_job", "basis": "process"}
        token = cm.bind(cm.Mark("rest", "transport", ua="curl_v8", origin=False, hello="example_client_v1"))
        try:
            assert cm.for_batch() == {"app": "rest", "basis": "transport"}, \
                "绑着的那个先于进程默认; batch 只拿两个词, 洗过的证据只进 batch_metrics.meta"
            assert cm.current().as_dict() == {"app": "rest", "basis": "transport", "ua": "curl_v8", "origin": False,
                                              "hello": "example_client_v1"}, "调用留痕 / 存档那一行拿全的"
        finally:
            cm.unbind(token)
        assert cm.current().app == "server_job"
    assert cm.current() is cm.UNKNOWN, "退出时还原"
    with pytest.raises(ZeroDivisionError):
        with cm.process_default(cm.SERVER_JOB):
            1 / 0
    assert cm.current() is cm.UNKNOWN, "抛了也还原, 用例之间串不了"
    with cm.process_default("made_up"):
        assert cm.current() is cm.UNKNOWN
    cm.unbind(None)                                       # 没绑上(bind 回了 None)时 unbind 什么都不做


def test_cli_main_runs_its_command_as_a_server_job(monkeypatch):
    from deskcore import cli
    seen = []
    monkeypatch.setattr(cli, "selftest", lambda: seen.append(cm.for_batch()) or 0)
    assert cli.main(["selftest"]) == 0
    assert seen == [{"app": "server_job", "basis": "process"}]
    assert cm.current() is cm.UNKNOWN, "main 退出后还原"


def test_the_module_imports_no_web_framework():
    src = open(cm.__file__, encoding="utf-8").read()
    for mod in ("fastapi", "starlette", "mcp", "anyio"):
        assert not re.search(rf"^\s*(import|from)\s+{mod}\b", src, re.M), mod
