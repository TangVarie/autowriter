"""入库判定客户端(judge_client) —— 每一种结局都要走到, 且绝不抛。

照 ``tests/test_librarian_timeout.py`` 的教训写: 判"fail-open"之前必须先把 URL/KEY 配上,
否则函数在"未配置"那一支就 return 了, **根本没走到 HTTP**, 把 fail-open 改成往外抛都照样
绿。所以除了未配置那一条, 其余每条都配上并断言"真的发出去了"。

状态映射是契约(JevforCoentent ``judge/api.py``):
  200 → ok · 403 "policy:…" → policy_blocked · 422 → bad_request · 401/503 → unavailable ·
  502 → jev_failed · 超时 → timeout · 其它 → error
"""

from __future__ import annotations

import importlib
import os
import pathlib
import re

import pytest
import requests

import config
import judge_client as J


class _Resp:
    def __init__(self, status: int, body=None, *, text: str = "", bad_json: bool = False):
        self.status_code = status
        self._body = body
        self.text = text
        self._bad = bad_json

    def json(self):
        if self._bad:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._body


@pytest.fixture
def wired(monkeypatch):
    """配上 URL/KEY, 并把 requests.post 换成录音机。返回 (calls, set_reply)。"""
    monkeypatch.setattr(config, "JUDGE_URL", "https://judge.example.invalid/")
    monkeypatch.setattr(config, "JUDGE_API_KEY", "jk-secret")
    monkeypatch.setattr(config, "JUDGE_TIMEOUT_SEC", 8.0)
    calls: list[dict] = []
    reply: dict = {"fn": lambda: _Resp(200, _OK_BODY)}

    def fake_post(url, *, headers=None, json=None, timeout=None, **kw):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return reply["fn"]()

    monkeypatch.setattr(requests, "post", fake_post)

    def set_reply(fn):
        reply["fn"] = fn
    return calls, set_reply


_OK_BODY = {
    "subject_id": "v1", "passed": False,
    "hard_fails": [["platform_health_v0.1", "efficacy_claim", "是", 0.91, "效果是真的绝, 立马就压住了"]],
    "plan": [{"qid": "efficacy_claim"}], "calls": 3, "written": 24,
    "policy": {"published": False, "project": "SPX_phase1", "dropped_banks": ["project"],
               "dropped_hard_rules": [], "extra": "x"},
    "profile": {"opening_type": "具体事件"},
}

PAYLOAD = J.build_draft_request(version_id="v1", title="标题", body="正文", project="SPX_phase1")


# ══════════════════════════════════════════════════════════════════════
# 1 · 未配置: 一个请求都不发
# ══════════════════════════════════════════════════════════════════════

def test_not_configured_sends_nothing(monkeypatch):
    monkeypatch.setattr(config, "JUDGE_URL", "")
    monkeypatch.setattr(config, "JUDGE_API_KEY", "")

    def boom(*a, **k):
        raise AssertionError("没配置却发了请求")
    monkeypatch.setattr(requests, "post", boom)

    out = J.judge_draft(PAYLOAD)
    assert out["judge_status"] == J.JUDGE_NOT_CONFIGURED
    assert J.configured() is False


@pytest.mark.parametrize("url,key", [("https://j", ""), ("", "k")])
def test_half_configured_is_not_configured(monkeypatch, url, key):
    monkeypatch.setattr(config, "JUDGE_URL", url)
    monkeypatch.setattr(config, "JUDGE_API_KEY", key)
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("不该发请求"))
    assert J.judge_draft(PAYLOAD)["judge_status"] == J.JUDGE_NOT_CONFIGURED


# ══════════════════════════════════════════════════════════════════════
# 2 · 请求本身: 地址、鉴权头、超时、请求体
# ══════════════════════════════════════════════════════════════════════

def test_request_shape(wired):
    calls, _ = wired
    J.judge_draft(PAYLOAD)
    assert len(calls) == 1
    c = calls[0]
    assert c["url"] == "https://judge.example.invalid/judge_draft", "尾斜杠要剥掉"
    assert c["headers"] == {"X-Judge-Key": "jk-secret"}
    connect, read = c["timeout"]
    assert connect <= 3.0 and read == 8.0, c["timeout"]
    body = c["json"]
    assert body["subject_id"] == "v1" and body["subject_type"] == "aw_version"
    assert body["project"] == "SPX_phase1"
    assert body["judge_paras"] == "never", "判段会把一篇的调用次数翻倍, 放不进 8 秒"
    assert body["write"] is True and body["run_tag"] == "primary"
    assert body["return_rows"] is False
    assert "category" not in body, "不知道品类就不带这个键(不是带一个空串)"
    assert "brief" not in body


def test_category_is_sent_when_known():
    body = J.build_draft_request(version_id="v", title="t", body="b",
                                 project="NUC_phase1", category="处方药")
    assert body["category"] == "处方药" and body["project"] == "NUC_phase1"


def test_explicit_timeout_wins(wired):
    calls, _ = wired
    J.judge_draft(PAYLOAD, timeout=2.5)
    assert calls[0]["timeout"] == (2.5, 2.5)


# ══════════════════════════════════════════════════════════════════════
# 3 · 每一种结局
# ══════════════════════════════════════════════════════════════════════

def test_ok_keeps_the_verdict_and_drops_the_evidence(wired):
    out = J.judge_draft(PAYLOAD)
    assert out["judge_status"] == J.JUDGE_OK and out["http_status"] == 200
    assert out["passed"] is False
    # 证据句不带回来(账本里有; 影子期也不想让写手模型对着它改稿)
    assert out["hard_fails"] == [["platform_health_v0.1", "efficacy_claim", "是", 0.91]]
    assert out["calls"] == 3 and out["written"] == 24 and out["plan_size"] == 1
    assert out["policy"] == {"published": False, "project": "SPX_phase1",
                             "dropped_banks": ["project"], "dropped_hard_rules": []}


@pytest.mark.parametrize("exc", [requests.Timeout("read timed out"),
                                 requests.ReadTimeout("read timed out"),
                                 requests.ConnectTimeout("connect timed out")])
def test_timeouts_are_timeout(wired, exc):
    """ConnectTimeout 同时是 ConnectionError 的子类 —— 分支顺序写反, 它会被报成
    unavailable, 而"这次慢了"和"部署坏了"要人做的事完全不同。"""
    calls, set_reply = wired

    def raise_():
        raise exc
    set_reply(raise_)
    out = J.judge_draft(PAYLOAD)
    assert calls, "没走到 HTTP —— 这条用例又变成空跑了"
    assert out["judge_status"] == J.JUDGE_TIMEOUT, out


def test_connection_refused_is_unavailable(wired):
    _, set_reply = wired

    def raise_():
        raise requests.ConnectionError("Connection refused")
    set_reply(raise_)
    assert J.judge_draft(PAYLOAD)["judge_status"] == J.JUDGE_UNAVAILABLE


def test_unexpected_exception_is_error_not_raised(wired):
    _, set_reply = wired

    def raise_():
        raise RuntimeError("意料之外")
    set_reply(raise_)
    out = J.judge_draft(PAYLOAD)
    assert out["judge_status"] == J.JUDGE_ERROR and "意料之外" in out["detail"]


@pytest.mark.parametrize("status,body,want", [
    (403, {"detail": "policy: 处方药项目的未发布稿不跑(docs/00 #7)"}, J.JUDGE_POLICY_BLOCKED),
    (403, {"detail": "Forbidden"}, J.JUDGE_ERROR),            # 不是出境口径的 403 不认
    (422, {"detail": "write=true 时 subject_id 必须是这篇稿子自己的 id"}, J.JUDGE_BAD_REQUEST),
    (401, {"detail": "X-Judge-Key 不对"}, J.JUDGE_UNAVAILABLE),
    (503, {"detail": "JUDGE_API_KEY 未配置"}, J.JUDGE_UNAVAILABLE),
    (404, {"detail": "Not Found"}, J.JUDGE_UNAVAILABLE),
    (502, {"detail": "Jev 调用失败：HTTP 529"}, J.JUDGE_JEV_FAILED),
    (500, {"detail": "Internal Server Error"}, J.JUDGE_ERROR),
    (429, {"detail": "slow down"}, J.JUDGE_ERROR),
])
def test_http_status_mapping(wired, status, body, want):
    calls, set_reply = wired
    set_reply(lambda: _Resp(status, body))
    out = J.judge_draft(PAYLOAD)
    assert out["judge_status"] == want, (status, out)
    assert out["http_status"] == status
    assert body["detail"][:20] in out["detail"]
    assert len(calls) == 1, "一律不重试 —— policy_blocked / bad_request 重试也是同一个结果"


def test_error_body_that_is_not_json_is_still_mapped(wired):
    _, set_reply = wired
    set_reply(lambda: _Resp(502, text="<html>Bad Gateway</html>", bad_json=True))
    out = J.judge_draft(PAYLOAD)
    assert out["judge_status"] == J.JUDGE_JEV_FAILED and "Bad Gateway" in out["detail"]


@pytest.mark.parametrize("body", [
    {"selected": []},                       # 别的服务的形状
    {"passed": "yes"},                      # passed 不是布尔
    ["passed"],                             # 不是对象
])
def test_200_with_the_wrong_shape_is_error_not_ok(wired, body):
    """与馆员那条同一个道理: 200 但结构不对**不是**成功, 记成 ok 这类故障就永远没人看了。"""
    _, set_reply = wired
    set_reply(lambda: _Resp(200, body))
    assert J.judge_draft(PAYLOAD)["judge_status"] == J.JUDGE_ERROR


def test_200_that_is_not_json_is_error(wired):
    _, set_reply = wired
    set_reply(lambda: _Resp(200, bad_json=True))
    out = J.judge_draft(PAYLOAD)
    assert out["judge_status"] == J.JUDGE_ERROR and "解析" in out["detail"]


def test_secrets_do_not_leak_into_detail(wired):
    _, set_reply = wired

    def raise_():
        raise requests.ConnectionError(
            "HTTPSConnectionPool: Max retries exceeded, Authorization: Bearer sk-ant-api03-abcdefghijklmnopqrstuvwxyz")
    set_reply(raise_)
    out = J.judge_draft(PAYLOAD)
    assert "sk-ant-api03-abcdefghijklmnopqrstuvwxyz" not in out["detail"]


def test_every_status_is_in_the_documented_set():
    assert set(J.STATUSES) == {"ok", "not_configured", "timeout", "policy_blocked",
                               "bad_request", "unavailable", "jev_failed", "error"}


# ══════════════════════════════════════════════════════════════════════
# 4 · 超时配置: 默认 8 秒、env 可改、写坏了不拖垮 import、上限夹住
# ══════════════════════════════════════════════════════════════════════

def test_code_default_timeout_is_eight_seconds():
    """钉**源码里的默认值**, 不钉 config.JUDGE_TIMEOUT_SEC —— 后者是 env 解析之后的值,
    环境里恰好配了 8 时, 默认值改成别的也照样绿(test_librarian_timeout 踩过)。"""
    src = pathlib.Path(config.__file__).read_text(encoding="utf-8")
    m = re.search(r'_bounded_number\("JUDGE_TIMEOUT_SEC",\s*([0-9.]+),\s*([0-9.]+),\s*([0-9.]+)\)', src)
    assert m, "config.py 里那行的写法变了 —— 同步改这条守卫"
    default, lo, hi = map(float, m.groups())
    assert default == 8.0, "docs/00 #4 定的是 8 秒即跳过"
    # 上限要留在 MCP 客户端实测能容忍的 ~22 秒之内, commit 自己还要 1~3 秒
    assert hi <= 15.0 and lo >= 1.0


@pytest.mark.parametrize("raw,want", [("5", 5.0), ("8s", 8.0), ("", 8.0), ("99", 15.0), ("0", 1.0)])
def test_timeout_env_is_parsed_defensively(raw, want):
    """写坏了(``8s``)退回默认、越界夹住 —— 增强项的配置错了不许让三个进程一起起不来。"""
    saved = os.environ.get("JUDGE_TIMEOUT_SEC")
    try:
        os.environ["JUDGE_TIMEOUT_SEC"] = raw
        assert importlib.reload(config).JUDGE_TIMEOUT_SEC == want
    finally:
        if saved is None:
            os.environ.pop("JUDGE_TIMEOUT_SEC", None)
        else:
            os.environ["JUDGE_TIMEOUT_SEC"] = saved
        importlib.reload(config)
