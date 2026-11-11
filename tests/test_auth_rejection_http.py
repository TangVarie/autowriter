"""钥匙对不上时的 401 在 HTTP 那一层(2026-11-11, docs/deskcore.md §3.23): REST 和 MCP 两条路拿到同一句, /health 只有计数。

钉住:
  1. REST(/tool/…、/tools)和 MCP(/mcp、/mcp/)被拒时都是 401 + 同一份 JSON: ``detail``(那一句)、``reason``、``key_shape``;
  2. 回包里没有服务端配的钥匙、没有配了几把; 发来的那一串最多念回最后 4 位;
  3. 首尾的空白去掉再查(不间断空格这类服务器不会替你去的); 引号、多出来的 Bearer 不替对方剥;
  4. /health 的 ``auth_rejections``: 固定的几个原因 + 最后一次的时间, 没有钥匙的任何一部分(连最后 4 位也没有), /health 自己
     不算被拒; 每拒一次一行 WARNING, 里面有长度和几个是 / 否, 没有钥匙、没有最后 4 位。
钥匙全是测试里现编的。纯函数那一层在 tests/test_auth_rejection_diagnostics.py。
"""
from __future__ import annotations

import collections
import json
import logging
import re

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

import deskcore.app as A                                  # noqa: E402
from deskcore import identity                             # noqa: E402

UID = "22222222-2222-2222-2222-222222222222"
GOOD = "k-shili-0123456789abcdefFAKE"                      # 现编的
OTHER = "k-lingyi-zyxwvutsrq9876543210"                    # 现编的
KEYS = json.dumps({GOOD: {"user_id": UID, "name": "示例写手"}, OTHER: {"user_id": UID, "name": "另一位"}},
                  ensure_ascii=False)
WRONG = "k-cuode-0000000000000000"
WRONG_DETAIL = ("invalid X-Deskcore-Key: 服务器不认识收到的这把钥匙(长度 24, 最后 4 位「0000」; 没看出空格、引号、省略号这类问题)。"
                "请对照发给你的那把钥匙, 在客户端的写作台连接设置(mcp.json)里把整把钥匙重新粘贴一遍: 不带空格、不带省略号, "
                "钥匙外面只留一对引号; 然后重启客户端、开一个新对话。钥匙不要发到对话里。")
MCP_INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "0"}}}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("DESKCORE_KEYS", KEYS)
    monkeypatch.delenv("DESKCORE_API_KEY", raising=False)
    monkeypatch.delenv("DESKCORE_ALLOW_ANONYMOUS", raising=False)
    monkeypatch.setattr(identity, "_REJECTIONS", collections.Counter())
    monkeypatch.setattr(identity, "_LAST_REJECTION", {"at": None})
    monkeypatch.setattr(A, "_KEY_TRANSPORT", A.collections.Counter())

    class _Dead:                                           # /health 不连真库
        def table(self, *_a, **_k):
            raise RuntimeError("no db in this test")
    monkeypatch.setattr(A, "_health_probe_client", lambda: _Dead())
    return fastapi_testclient.TestClient(A.app, raise_server_exceptions=False)


def _no_server_keys(text: str) -> None:
    for leaked in (GOOD, OTHER, GOOD[:8], OTHER[:8], "FAKE", "3210", "示例写手", "另一位", "key(s)", "2 把"):
        assert leaked not in text, leaked


# ── 1. 两条路同一句 ──────────────────────────────────────────────────────────────────────────────

def test_rest_401_carries_the_sentence_the_reason_and_the_shape(client):
    r = client.post("/tool/list_projects", json={}, headers={"X-Deskcore-Key": WRONG})
    assert r.status_code == 401
    body = r.json()
    assert list(body) == ["detail", "reason", "key_shape"], "detail 在最前面: 只显示开头的客户端也看得到那一句"
    assert body["detail"] == WRONG_DETAIL and body["reason"] == "invalid"
    assert body["key_shape"] == {"length": 24, "last4": "0000", "whitespace_around": False, "whitespace_inside": False,
                                 "quotes": False, "ellipsis": False, "bearer_prefix": False, "non_ascii": False,
                                 "placeholder": False}
    _no_server_keys(r.text)
    assert "服务器不认识收到的这把钥匙" in r.text, "中文原样回, 不转成 \\u(MCP 客户端多半把响应体原样贴出来)"


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
def test_mcp_401_is_the_same_json_on_both_spellings_of_the_path(client, path):
    """鉴权中间件包着整个 app, 挂在 /mcp 下面的 MCP 子应用也在里面: 握手被拒时拿到的和 REST 是同一份。"""
    r = client.post(path, json=MCP_INIT, headers={**MCP_HEADERS, "X-Deskcore-Key": WRONG}, follow_redirects=False)
    assert r.status_code == 401, (r.status_code, r.text[:200])
    assert r.json() == client.post("/tool/list_projects", json={}, headers={"X-Deskcore-Key": WRONG}).json()
    assert r.json()["detail"] == WRONG_DETAIL
    _no_server_keys(r.text)
    assert client.get(path, headers={"X-Deskcore-Key": WRONG}, follow_redirects=False).json()["detail"] == WRONG_DETAIL


def test_mcp_with_the_right_key_gets_past_the_auth_layer(client):
    """这里不进 lifespan(MCP 的 session manager 每个进程只能起一次, 握手成不成由 test_deskcore_cors 的 live_client 和 CI
    那一步看): 只看带对了钥匙就不是 401、也不记成被拒。"""
    r = client.post("/mcp", json=MCP_INIT, headers={**MCP_HEADERS, "X-Deskcore-Key": GOOD})
    assert r.status_code != 401, (r.status_code, r.text[:200])
    assert identity.auth_rejections()["since_start"] == dict.fromkeys(identity.AUTH_REJECT_REASONS, 0)


def test_bearer_transport_gets_the_same_diagnosis(client):
    r = client.get("/tools", headers={"Authorization": f"Bearer {WRONG}"})
    assert r.status_code == 401 and r.json()["detail"] == WRONG_DETAIL


@pytest.mark.parametrize("headers, reason, starts", [
    ({}, "missing", "missing X-Deskcore-Key"),
    ({"X-Deskcore-Key": ""}, "missing", "missing X-Deskcore-Key"),
    ({"Authorization": "Basic abc"}, "missing", "missing X-Deskcore-Key"),
])
def test_missing_key_has_no_shape(client, headers, reason, starts):
    r = client.get("/tools", headers=headers)
    assert r.status_code == 401 and r.json() == {"detail": starts, "reason": reason}


def test_query_param_keeps_its_own_sentence_and_reason(client):
    r = client.get(f"/tools?key={GOOD}")
    assert r.status_code == 401 and r.json()["reason"] == "query_param" and "key_shape" not in r.json()
    assert "no longer accepted" in r.json()["detail"]
    _no_server_keys(r.text)


def test_misconfigured_server_says_so(client, monkeypatch):
    monkeypatch.setenv("DESKCORE_KEYS", "{not json")
    r = client.get("/tools", headers={"X-Deskcore-Key": GOOD})
    assert r.status_code == 401 and r.json()["reason"] == "misconfigured" and "key_shape" not in r.json()
    assert GOOD not in r.text


def test_a_broken_key_map_tells_an_unauthenticated_caller_nothing_about_the_keys(client, monkeypatch):
    """复审 11-11: 少写一层的条目(合法 JSON)—— 401 是固定的一句, /health 只报第几条; 两处都没有钥匙的前几位。"""
    monkeypatch.setenv("DESKCORE_KEYS", json.dumps({GOOD: {"user_id": UID}, OTHER: UID}))
    r = client.get("/tools", headers={"X-Deskcore-Key": "k-suibian-00000000"})
    assert r.status_code == 401 and r.json() == {"detail": identity.MISCONFIGURED_KEY_MAP, "reason": "misconfigured"}
    health = client.get("/health")
    assert "第 [2] 条的值不是对象" in health.json()["config"]["auth"]["note"]
    for text in (r.text, health.text):
        for leaked in (GOOD, OTHER, GOOD[:6], OTHER[:6], "k-shi", "k-lin"):
            assert leaked not in text, leaked


def test_an_elided_entry_in_the_key_map_cannot_be_sent_over_http(client, monkeypatch):
    """复审 11-11: DESKCORE_KEYS 里一把截短的示例(带「…」)—— 照着它发的请求一直 401, REST 和 MCP 都是。"""
    elided = "k-shili-…"
    monkeypatch.setenv("DESKCORE_KEYS", json.dumps({elided: {"user_id": UID, "name": "示例写手"}}, ensure_ascii=False))
    for sent in (elided.encode("utf-8"), (elided + "\u00a0").encode("utf-8"), ("\u3000" + elided).encode("utf-8")):
        r = client.get("/tools", headers={"X-Deskcore-Key": sent})
        assert r.status_code == 401 and r.json()["reason"].startswith("invalid"), r.text
        r = client.get("/tools", headers={"Authorization": b"Bearer " + sent})
        assert r.status_code == 401 and r.json()["reason"].startswith("invalid"), r.text
        r = client.post("/mcp", json=MCP_INIT, headers={**MCP_HEADERS, "X-Deskcore-Key": sent})
        assert r.status_code == 401 and r.json()["reason"].startswith("invalid"), r.text
    r = client.get("/tools", headers={"X-Deskcore-Key": elided.encode("utf-8")})
    assert r.json()["reason"] == "invalid_ellipsis" and r.json()["key_shape"]["length"] == len(elided)
    assert "会一直 401" in client.get("/health").json()["config"]["auth"]["note"]


# ── 2 / 3. 不替对方改, 只去首尾空白 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("sent, reason, flag_word, last4", [
    (f'"{GOOD}"', "invalid_quotes", "带引号", 'AKE"'),
    (f"Bearer {GOOD}", "invalid_bearer_prefix", "开头多了 Bearer(这一栏只放钥匙本身)", "FAKE"),
    (GOOD[:14] + "...", "invalid_ellipsis", "带省略号", "5..."),
    (GOOD[:10] + " " + GOOD[10:], "invalid_whitespace", "中间有空格", "FAKE"),
    ("k-xxx", "invalid", "像示例里的占位", None),
])
def test_a_wrapped_or_cut_key_is_refused_and_named(client, sent, reason, flag_word, last4):
    r = client.get("/tools", headers={"X-Deskcore-Key": sent})
    assert r.status_code == 401, "里面包着的就算是对的钥匙也不替对方剥"
    body = r.json()
    assert body["reason"] == reason and flag_word in body["detail"] and body["key_shape"]["last4"] == last4
    assert body["key_shape"]["length"] == len(sent)
    assert GOOD not in r.text, "整把钥匙不念回去"
    assert GOOD[4:12] not in r.text


def test_utf8_ellipsis_sent_as_bytes_is_measured_as_the_writer_typed_it(client):
    sent = "k-shili-…"
    r = client.get("/tools", headers={"X-Deskcore-Key": sent.encode("utf-8")})
    body = r.json()
    assert r.status_code == 401 and body["reason"] == "invalid_ellipsis"
    assert body["key_shape"]["length"] == 9 and body["key_shape"]["last4"] == "li-…"
    assert "长度 9, 最后 4 位「li-…」; 带省略号、有非英文字符" in body["detail"]


@pytest.mark.parametrize("raw", [GOOD.encode() + b"\xa0", b"\xa0" + GOOD.encode(), GOOD.encode() + b"  ",
                                 b"\t" + GOOD.encode()])
def test_surrounding_whitespace_on_the_header_value_does_not_cost_a_401(client, raw):
    """空格和 tab 多数服务器到这里之前就去掉了; 不间断空格(复制粘贴最容易带上)它们不管, 这里去。"""
    r = client.get("/tools", headers={"X-Deskcore-Key": raw})
    assert r.status_code == 200, r.text[:200]
    assert identity.auth_rejections()["since_start"] == dict.fromkeys(identity.AUTH_REJECT_REASONS, 0)


# ── 4. /health 的计数和日志 ──────────────────────────────────────────────────────────────────────

def test_health_counts_rejections_by_reason_and_nothing_else(client, caplog):
    sent = [({"X-Deskcore-Key": WRONG}, "invalid"),
            ({"X-Deskcore-Key": f'"{GOOD}"'}, "invalid_quotes"),
            ({"X-Deskcore-Key": f"Bearer {GOOD}"}, "invalid_bearer_prefix"),
            ({"X-Deskcore-Key": "k-shili-0123..."}, "invalid_ellipsis"),
            ({"X-Deskcore-Key": "k-shili-…".encode("utf-8")}, "invalid_ellipsis"),
            ({"X-Deskcore-Key": "k-shili 0123456789"}, "invalid_whitespace"),
            ({}, "missing"), ({}, "missing")]
    assert client.get("/health").json()["auth_rejections"] == {
        "since_start": dict.fromkeys(identity.AUTH_REJECT_REASONS, 0), "last_at": None}
    with caplog.at_level(logging.WARNING, logger="deskcore"):
        for headers, reason in sent:
            assert client.get("/tools", headers=headers).json()["reason"] == reason
        assert client.get(f"/tool/x?key={GOOD}").status_code == 401
        assert client.get("/tools", headers={"X-Deskcore-Key": GOOD}).status_code == 200   # 过了的不算
    r = client.get("/health")
    block = r.json()["auth_rejections"]
    assert block["since_start"] == {"missing": 2, "invalid": 1, "invalid_whitespace": 1, "invalid_quotes": 1,
                                    "invalid_ellipsis": 2, "invalid_bearer_prefix": 1, "query_param": 1,
                                    "misconfigured": 0, "other": 0}
    assert set(block) == {"since_start", "last_at"}
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", block["last_at"])
    assert client.get("/health").json()["auth_rejections"]["since_start"] == block["since_start"], "/health 自己不算"
    # /health 不带鉴权: 没有钥匙的任何一部分, 连最后 4 位也没有
    health_text = json.dumps({k: v for k, v in r.json().items() if k != "tools"}, ensure_ascii=False)
    for leaked in (GOOD, OTHER, WRONG, "FAKE", "0000", "AKE", "23...", "li-…", "示例写手", "另一位", "last4"):
        assert leaked not in health_text, leaked
    # 日志: 每拒一次一行, 有长度和几个是 / 否, 没有钥匙、没有最后 4 位
    lines = [rec.getMessage() for rec in caplog.records if rec.getMessage().startswith("auth rejected")]
    assert lines == [
        "auth rejected: reason=invalid path=/tools via=header len=24 flags=-",
        "auth rejected: reason=invalid_quotes path=/tools via=header len=30 flags=quotes",
        "auth rejected: reason=invalid_bearer_prefix path=/tools via=header len=35 flags=bearer_prefix",
        "auth rejected: reason=invalid_ellipsis path=/tools via=header len=15 flags=ellipsis",
        "auth rejected: reason=invalid_ellipsis path=/tools via=header len=9 flags=ellipsis,non_ascii",
        "auth rejected: reason=invalid_whitespace path=/tools via=header len=18 flags=whitespace_inside",
        "auth rejected: reason=missing path=/tools via=none len=- flags=-",
        "auth rejected: reason=missing path=/tools via=none len=- flags=-",
        "auth rejected: reason=query_param path=/tool/x via=query len=- flags=-",
    ]
    for leaked in (GOOD, WRONG, "FAKE", "0000", GOOD[:8], "k-shili", "key="):
        assert leaked not in "\n".join(lines), leaked


def test_the_transport_counter_still_counts_once_per_request(client):
    client.get("/tools", headers={"X-Deskcore-Key": WRONG})
    client.get("/tools", headers={"Authorization": f"Bearer {WRONG}"})
    client.get(f"/tools?key={WRONG}")
    client.get("/tools")
    assert A.key_transport_counts() == {"header": 1, "bearer": 1, "query": 1, "none": 1}


def test_a_401_still_carries_cors_headers(client):
    r = client.get("/tools", headers={"X-Deskcore-Key": WRONG, "Origin": "http://localhost:5173"})
    assert r.status_code == 401 and r.headers.get("access-control-allow-origin") == "*"
    assert r.json()["detail"] == WRONG_DETAIL
