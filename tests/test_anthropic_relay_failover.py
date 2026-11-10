"""备用中转站 (D-148): ``clients.py`` 的接线 + ``relay_failover.py`` 共享核心。

全部 mock, 不联网: ``anthropic.Anthropic`` 换成记录构造参数的假件, 每个"站"怎么回按脚本出;
地址只用 ``*.invalid``、key 只用 ``sk-test-*``、中转站报错正文里带一个哨兵串 ——
断言日志 / 汇总行 / ``relay_status()`` 里一个都不出现。

覆盖:
  核心 C1-C12(设计稿 §16; 经 ``relay_failover.configure`` 读 config 上的属性)
  没配备用站 = 老路径一个字节不差(构造参数、尝试次数、没有 timeout、不碰熔断文件、不打日志)
  主站超时 / 5xx / 401-404 / 429 → 同一次尝试改打备用站; 400 / 422 / 敏感词拦截 → 原样上抛
  流式(``ClaudeEngine.iterate``)整段在备用站重跑; 流式不按"慢"判
  SUP-001 的 ttl 降级重拼在自动模式下照旧只打主站
  熔断: 连续 2 次失败打开 → 直接走备用 → 冷却后放一个试探
  RELAY_FORCE 两个方向; 多线程各走各的; 备用 key 进值级脱敏名单
"""

from __future__ import annotations

import http.client
import itertools
import json
import logging
import os
import ssl
import subprocess
import sys
import tempfile
import threading
import types
import urllib.error
from pathlib import Path

import anthropic
import httpx
import pytest

import clients
import config
import logger_utils
import relay_failover as rf

REPO_ROOT = Path(__file__).resolve().parent.parent

P = "https://primary-host.invalid"
BU = "https://backup-host.invalid"
K = "sk-test-primary-0123456789"
BK = "sk-test-backup-0123456789"
SENTINEL = "BALANCE-SENTINEL-余额"
FORBIDDEN = ("primary-host.invalid", "backup-host.invalid", K, BK, "BALANCE-SENTINEL")

RELAY_ATTRS = ("ANTHROPIC_BASE_URL_BACKUP", "ANTHROPIC_API_KEY_BACKUP", "RELAY_FORCE",
               "RELAY_PRIMARY_TIMEOUT_S", "RELAY_SLOW_S", "RELAY_BREAKER_FAILS", "RELAY_BREAKER_COOLDOWN_S")

LEGACY_KW = {"api_key": K, "base_url": P, "max_retries": 0}
PRIMARY_KW = {"api_key": K, "base_url": P, "max_retries": 0, "timeout": 60.0}
BACKUP_KW = {"api_key": BK, "base_url": BU, "max_retries": 0}
ZERO = dict.fromkeys(rf.COUNTER_KEYS, 0)

FAILOVER_STATUSES = [401, 402, 403, 404, 408, 409, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529]
NO_FAILOVER_STATUSES = [400, 405, 413, 422]


# ── SDK 异常: 不走 __init__(要真 httpx Request / Response, 各 SDK 版本不一样), 同 ci.yml SUP-001 的写法 ──

_STATUS_CLS = {
    400: anthropic.BadRequestError, 401: anthropic.AuthenticationError, 403: anthropic.PermissionDeniedError,
    404: anthropic.NotFoundError, 409: anthropic.ConflictError, 422: anthropic.UnprocessableEntityError,
    429: anthropic.RateLimitError, 500: anthropic.InternalServerError,
}


def _sdk_exc(cls, msg, *, body=None, **attrs):
    e = cls.__new__(cls)
    Exception.__init__(e, msg)
    e.message = msg
    e.body = body
    for k, v in attrs.items():
        setattr(e, k, v)
    return e


def status_exc(status, msg=None, body=None):
    """中转站的报错: 正文里带地址和余额哨兵, 只有状态码 / 类名可以进日志。"""
    cls = _STATUS_CLS.get(status, anthropic.APIStatusError)
    msg = msg or f"Error code: {status} - {{'error': {{'message': '{SENTINEL}', 'at': '{P}'}}}}"
    return _sdk_exc(cls, msg, body=body, status_code=status)


def timeout_exc():
    return _sdk_exc(anthropic.APITimeoutError, f"Request timed out ({SENTINEL} {P})")


def conn_exc():
    return _sdk_exc(anthropic.APIConnectionError, f"Connection error ({SENTINEL} {BU})")


# ── 假 SDK: 记下每次 Anthropic(**kw) 和每个请求; 每个站的回应按脚本出 ─────────────────────────────

class _Final:
    def __init__(self, text):
        self.content = [types.SimpleNamespace(type="text", text=text)]
        self.usage = types.SimpleNamespace(input_tokens=3, output_tokens=5,
                                           cache_read_input_tokens=0, cache_creation_input_tokens=0)
        self.stop_reason = "end_turn"


class FakeSDK:
    def __init__(self, tmp):
        self.tmp = tmp
        self.built: list = []        # 每次 Anthropic(**kw) 的 kw, 按构造顺序
        self.instances: list = []
        self.calls: list = []        # (relay, "create" | "stream", kwargs, thread name)
        self.sleeps: list = []
        self.script = {"primary": [], "backup": []}
        self.thread_script: dict = {}   # thread name → {"primary": [...], "backup": [...]}
        self.lock = threading.Lock()

    @staticmethod
    def relay_of(kw):
        return "backup" if kw.get("base_url") == BU else "primary"

    def relays_built(self):
        return [self.relay_of(kw) for kw in self.built]

    def relays_called(self, thread=None):
        return [c[0] for c in self.calls if thread is None or c[3] == thread]

    def record(self, relay, kind, kw):
        with self.lock:
            self.calls.append((relay, kind, kw, threading.current_thread().name))

    def outcome(self, relay):
        with self.lock:
            per = self.thread_script.get(threading.current_thread().name)
            q = (per if per is not None else self.script)[relay]
            out = q.pop(0) if q else f"ok-{relay}"
        if callable(out) and not isinstance(out, BaseException):
            out = out()
        if isinstance(out, BaseException):
            raise out
        return _Final(out)


class _Stream:
    def __init__(self, fake, relay):
        self._fake, self._relay = fake, relay

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._fake.outcome(self._relay)


class _Messages:
    def __init__(self, fake, relay):
        self._fake, self._relay = fake, relay

    def create(self, **kw):
        self._fake.record(self._relay, "create", kw)
        return self._fake.outcome(self._relay)

    def stream(self, **kw):
        self._fake.record(self._relay, "stream", kw)
        return _Stream(self._fake, self._relay)


def _recorder(fake):
    class Recorder:
        def __init__(self, **kw):
            self.kw = kw
            self.relay = fake.relay_of(kw)
            self.auth_token = "bearer-from-env"     # 备用 client 必须被清掉
            self.messages = _Messages(fake, self.relay)
            with fake.lock:
                fake.built.append(dict(kw))
                fake.instances.append(self)
    return Recorder


@pytest.fixture(autouse=True)
def fake(monkeypatch, tmp_path, caplog):
    sdk = FakeSDK(tmp_path)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", K)
    monkeypatch.setattr(config, "ANTHROPIC_BASE_URL", P)
    for name in RELAY_ATTRS:                     # raising=True: config 必须定义了这几个名字
        monkeypatch.setattr(config, name, "")
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(clients.time, "sleep", sdk.sleeps.append)
    monkeypatch.setattr(clients.anthropic, "Anthropic", _recorder(sdk))
    caplog.set_level(logging.DEBUG)
    rf._reset_for_tests()
    clients.reset_clients()
    yield sdk
    clients.reset_clients()
    rf._reset_for_tests()


def backup_on(monkeypatch, force=""):
    monkeypatch.setattr(config, "ANTHROPIC_BASE_URL_BACKUP", BU)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY_BACKUP", BK)
    monkeypatch.setattr(config, "RELAY_FORCE", force)


def relay_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "relay"]


def breaker_files(tmp):
    return sorted(p.name for p in Path(tmp).glob("relay_breaker_*"))


def breaker_file(primary=P):
    with open(rf.state_path(primary), encoding="utf-8") as f:
        return json.load(f)


def assert_clean(caplog, capsys, *extra):
    out, err = capsys.readouterr()
    blob = "\n".join([caplog.text, out, err, *extra])
    leaked = [s for s in FORBIDDEN if s in blob]
    assert not leaked, f"地址 / key / 中转站报错原文进了日志: {len(leaked)} 处"


def counts(**nonzero):
    return {**ZERO, **nonzero}


def memory_style_call():
    """memory.py 的写法: 先拿 client, 再把闭包交给 with_anthropic_retry。"""
    c = clients.get_anthropic_client()
    return clients.with_anthropic_retry(lambda: c.messages.create(
        model="claude-sonnet-5-5", max_tokens=400, messages=[{"role": "user", "content": "hi"}]))


def text_of(resp):
    return resp.content[0].text


# ══════════════════════════════════════════════════════════════════════════════
# 共享核心 C1-C12
# ══════════════════════════════════════════════════════════════════════════════

_MODE_TABLE = [
    ("none", "", "", "", rf.MODE_PRIMARY_ONLY),
    ("url_only", BU, "", "", rf.MODE_PRIMARY_ONLY),
    ("key_only", "", BK, "", rf.MODE_PRIMARY_ONLY),
    ("url_blank", "   ", BK, "", rf.MODE_PRIMARY_ONLY),
    ("key_blank", BU, " \t", "", rf.MODE_PRIMARY_ONLY),
    ("both", BU, BK, "", rf.MODE_AUTO),
    ("both_AUTO", BU, BK, "AUTO ", rf.MODE_AUTO),
    ("both_primary", BU, BK, "primary", rf.MODE_PRIMARY_ONLY),
    ("both_Backup", BU, BK, " Backup", rf.MODE_BACKUP_ONLY),
]


@pytest.mark.parametrize("url,key,force,want", [r[1:] for r in _MODE_TABLE], ids=[r[0] for r in _MODE_TABLE])
def test_c1_mode_table(monkeypatch, caplog, url, key, force, want):
    monkeypatch.setattr(config, "ANTHROPIC_BASE_URL_BACKUP", url)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY_BACKUP", key)
    monkeypatch.setattr(config, "RELAY_FORCE", force)
    assert rf.mode() == want
    assert rf.backup_configured() is (want != rf.MODE_PRIMARY_ONLY or force == "primary")
    assert relay_lines(caplog) == []


def test_c1_force_backup_without_backup_warns_once(monkeypatch, caplog):
    monkeypatch.setattr(config, "RELAY_FORCE", "backup")
    assert rf.mode() == rf.MODE_PRIMARY_ONLY
    assert rf.mode() == rf.MODE_PRIMARY_ONLY
    assert relay_lines(caplog) == ["relay: RELAY_FORCE=backup but no backup relay is configured; using primary"]


def test_c1_unknown_force_is_auto_and_warns_once(monkeypatch, caplog):
    backup_on(monkeypatch, force="zzz")
    assert rf.mode() == rf.MODE_AUTO
    assert rf.mode() == rf.MODE_AUTO
    assert rf.force_setting() == "auto"
    assert relay_lines(caplog) == [
        "relay: RELAY_FORCE not recognized (expected primary, backup or empty); using auto"]


@pytest.mark.parametrize("raw,want", [("", 60.0), ("20", 20.0), ("1", 5.0), ("9999", 600.0),
                                      ("abc", 60.0), ("nan", 60.0)])
def test_c2_primary_timeout_parsing(monkeypatch, raw, want):
    monkeypatch.setattr(config, "RELAY_PRIMARY_TIMEOUT_S", raw)
    assert rf.primary_timeout_s(60.0) == want


def test_c2_other_knobs(monkeypatch):
    for raw, want in (("", 2), ("2.5", 2), ("0", 1), ("50", 20), ("3", 3), ("x", 2)):
        monkeypatch.setattr(config, "RELAY_BREAKER_FAILS", raw)
        assert rf.breaker_fails() == want, raw
    for raw, want in (("", 1200.0), ("10", 30.0), ("100000", 86400.0), ("600", 600.0)):
        monkeypatch.setattr(config, "RELAY_BREAKER_COOLDOWN_S", raw)
        assert rf.breaker_cooldown_s() == want, raw
    for raw, want in (("", 30.0), ("0", 0.0), ("-5", 0.0), ("700", 600.0), ("12.5", 12.5)):
        monkeypatch.setattr(config, "RELAY_SLOW_S", raw)
        assert rf.slow_s(30.0) == want, raw


def test_c3_classification():
    for st in FAILOVER_STATUSES:
        assert rf.is_failover_worthy(status_exc(st)), st
    for exc in (timeout_exc(), conn_exc(), TimeoutError(), ConnectionResetError(),
                urllib.error.URLError(ConnectionRefusedError()), http.client.RemoteDisconnected("x"),
                http.client.IncompleteRead(b""), ssl.SSLError(), json.JSONDecodeError("x", "doc", 0)):
        assert rf.is_failover_worthy(exc), type(exc).__name__
    # SDK 流里的 error 事件: HTTP 200, 但 body 里是 overloaded_error
    assert rf.is_failover_worthy(status_exc(200, body={"type": "error", "error": {"type": "overloaded_error"}}))
    for st in NO_FAILOVER_STATUSES:
        assert not rf.is_failover_worthy(status_exc(st)), st
    for exc in (ValueError("x"), KeyError("x"), RuntimeError("connection timeout")):   # 不按消息子串判
        assert not rf.is_failover_worthy(exc), exc
    # D-125 敏感词拦截是"内容判决", 不是站挂了: 状态码 / 类名再像故障也不切
    for exc in (status_exc(500, "Error code: 500 - sensitive_words_detected"),
                status_exc(529, "sensitive_words_detected"),
                _sdk_exc(anthropic.APITimeoutError, "sensitive_words_detected")):
        assert not rf.is_failover_worthy(exc)


def test_c4_describe_never_carries_the_message():
    assert rf.describe(status_exc(529, body={"error": {"type": "overloaded_error"}})) == "HTTP 529 overloaded_error"
    assert rf.describe(timeout_exc()) == "APITimeoutError"
    assert rf.describe(status_exc(401)) == "HTTP 401"
    assert rf.describe(status_exc(404, body={"error": {"type": "not_found_error"}})) == "HTTP 404"


def test_c5_state_path(tmp_path):
    a = rf.state_path("https://h/")
    assert a == rf.state_path("https://h/v1") == rf.state_path(" https://h/v1/ ")
    assert a != rf.state_path("https://g/")
    assert os.path.dirname(a) == str(tmp_path)
    name = os.path.basename(rf.state_path(P))
    assert "primary-host" not in name and "http" not in name and "invalid" not in name
    assert rf.state_path("") == rf.state_path(None) == rf.state_path("https://api.anthropic.com")


def test_c6_breaker_phases(monkeypatch, caplog, fake):
    backup_on(monkeypatch)
    now = [1_000_000.0]
    monkeypatch.setattr(rf, "_now", lambda: now[0])
    path, cd = rf.state_path(P), rf.breaker_cooldown_s()

    rf._record(path, "fail", False, "HTTP 503")
    assert rf.breaker_state(P) == "closed" and rf._decide(path, cd) == "primary"
    rf._record(path, "fail", False, "HTTP 503")
    assert rf.breaker_state(P) == "open"
    assert [x for x in relay_lines(caplog) if "breaker open" in x] == [
        "relay: breaker open for 1200s after 2 consecutive primary failures (last: HTTP 503); "
        "calls go straight to backup"]
    assert rf._decide(path, cd) == "skip"

    now[0] += cd                                  # opened_at + cooldown → 半开: 第一个试探, 第二个照旧走备用
    assert rf.breaker_state(P) == "half_open"
    assert rf._decide(path, cd) == "probe"
    assert rf._decide(path, cd) == "skip"
    now[0] += rf.PROBE_TTL_S + 1                  # 试探名额过期
    assert rf._decide(path, cd) == "probe"
    rf._record(path, "ok", True, "")
    assert rf.breaker_state(P) == "closed"
    assert relay_lines(caplog)[-1] == "relay: breaker closed (primary ok again)"
    assert breaker_file() == {"v": 1, "fails": 0, "opened_at": None, "probe_at": None}

    rf._record(path, "fail", False, "x")
    rf._record(path, "fail", False, "x")
    now[0] += cd
    assert rf._decide(path, cd) == "probe"
    rf._record(path, "fail", True, "APITimeoutError")   # 试探失败 → 以现在为起点重新熔断
    st = breaker_file()
    assert st["opened_at"] == now[0] and st["probe_at"] is None
    assert rf._decide(path, cd) == "skip"

    now[0] += cd
    assert rf._decide(path, cd) == "probe"
    rf._record(path, "neutral", True, "")               # 试探撞上非故障错误 → 只归还名额
    assert breaker_file()["probe_at"] is None
    assert rf._decide(path, cd) == "probe"

    Path(path).write_text(json.dumps({"v": 1, "fails": 2, "opened_at": now[0] + 10_000, "probe_at": None}),
                          encoding="utf-8")
    assert rf.breaker_state(P) == "half_open"          # 来自未来的时间戳(时钟漂移 / 损坏)放一个试探


@pytest.mark.parametrize("content", ["{x", "[]", '{"v":2}', '{"v":1,"fails":"x","opened_at":"y"}'],
                         ids=["broken_json", "list", "wrong_version", "wrong_types"])
def test_c7_corrupt_state_file_reads_closed(monkeypatch, content):
    backup_on(monkeypatch)
    path = rf.state_path(P)
    Path(path).write_text(content, encoding="utf-8")
    rf._reset_for_tests()
    assert rf.breaker_state(P) == "closed"
    assert rf._decide(path, 1200.0) == "primary"
    rf._record(path, "fail", False, "HTTP 503")
    assert breaker_file() == {"v": 1, "fails": 1, "opened_at": None, "probe_at": None}


def test_c8_cross_process_read_modify_write(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "RELAY_"))}
    env["TMPDIR"] = str(tmp_path)
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import relay_failover as r; "
            "p = r.state_path(sys.argv[2]); [r._record(p, 'fail', False, 'HTTP 503') for _ in range(50)]")
    procs = [subprocess.Popen([sys.executable, "-c", code, str(REPO_ROOT), P], env=env, cwd=str(tmp_path),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
    outs = [p.communicate(timeout=120) for p in procs]
    assert [p.returncode for p in procs] == [0, 0, 0, 0], [e[-500:] for _, e in outs]
    st = breaker_file()
    assert st["fails"] == 200 and st["opened_at"] is not None
    assert sum(e.count("relay: breaker open") for _, e in outs) == 1
    blob = "".join(o + e for o, e in outs)
    assert not [s for s in FORBIDDEN if s in blob]


def test_c9_call_fast_success(monkeypatch):
    backup_on(monkeypatch)
    monkeypatch.setattr(rf, "_mono", lambda: 0.0)
    seen = []

    def unit(t):
        seen.append(t)
        return f"r-{t}"

    assert rf.call(unit, relay_mode=rf.MODE_AUTO, primary_url=P, slow_after_s=25.0) == "r-primary"
    assert seen == ["primary"]
    assert rf.counts() == counts(primary_ok=1)


def test_c9_call_slow_success_counts_as_breaker_failure(monkeypatch, caplog):
    backup_on(monkeypatch)
    ticks = iter([0.0, 26.0, 100.0, 126.0])
    monkeypatch.setattr(rf, "_mono", lambda: next(ticks))
    seen = []

    def unit(t):
        seen.append(t)
        return f"r-{t}"

    assert rf.call(unit, relay_mode=rf.MODE_AUTO, primary_url=P, slow_after_s=25.0) == "r-primary"
    assert rf.breaker_state(P) == "closed"
    assert rf.call(unit, relay_mode=rf.MODE_AUTO, primary_url=P, slow_after_s=lambda: 25.0) == "r-primary"
    assert rf.breaker_state(P) == "open"
    assert any("(last: slow 26.0s)" in x for x in relay_lines(caplog))
    assert rf.call(unit, relay_mode=rf.MODE_AUTO, primary_url=P, slow_after_s=25.0) == "r-backup"
    assert seen == ["primary", "primary", "backup"]
    assert rf.counts() == counts(primary_ok=2, primary_slow=2, backup_ok=1, breaker_open_skips=1)


def test_c9_call_failover_and_backup_exception_has_no_primary_context(monkeypatch):
    backup_on(monkeypatch)

    def unit(t):
        if t == "primary":
            raise timeout_exc()
        raise status_exc(503)

    with pytest.raises(anthropic.APIStatusError) as ei:
        rf.call(unit, relay_mode=rf.MODE_AUTO, primary_url=P)
    assert ei.value.status_code == 503 and ei.value.__context__ is None
    assert rf.counts() == counts(primary_fail=1, backup_fail=1)

    def unit2(t):
        if t == "primary":
            raise status_exc(529)
        return "r-backup"

    assert rf.call(unit2, relay_mode=rf.MODE_AUTO, primary_url=P) == "r-backup"
    assert rf.counts() == counts(primary_fail=2, backup_ok=1, backup_fail=1)


def test_c9_call_non_failover_error_is_the_same_object(monkeypatch):
    backup_on(monkeypatch)
    err = status_exc(400, "max_tokens: must be positive")
    seen = []

    def unit(t):
        seen.append(t)
        raise err

    with pytest.raises(anthropic.BadRequestError) as ei:
        rf.call(unit, relay_mode=rf.MODE_AUTO, primary_url=P)
    assert ei.value is err and seen == ["primary"]
    assert rf.breaker_state(P) == "closed"
    assert rf.counts() == ZERO


def test_c9_call_backup_only_and_primary_only(monkeypatch, fake):
    backup_on(monkeypatch, force="backup")
    seen = []
    assert rf.call(lambda t: seen.append(t) or f"r-{t}", relay_mode=rf.MODE_BACKUP_ONLY, primary_url=P) == "r-backup"
    assert seen == ["backup"] and breaker_files(fake.tmp) == []
    with pytest.raises(ValueError):
        rf.call(lambda t: "x", relay_mode=rf.MODE_PRIMARY_ONLY, primary_url=P)


def test_c11_summary_keys(monkeypatch):
    backup_on(monkeypatch)
    s = rf.summary()
    assert set(s) == {"mode", *rf.COUNTER_KEYS, "breaker"}
    assert "ok" not in s and "failed" not in s
    assert s["mode"] == "auto" and s["breaker"] == "closed"


def test_c12_health_keys_and_types(monkeypatch, fake):
    h = rf.health()
    assert h == {"relay_mode": "primary_only", "relay_backup_configured": False, "relay_force": "auto",
                 "relay_breaker": "off", "relay_counts": ZERO}
    assert breaker_files(fake.tmp) == []                 # primary_only 连熔断文件都不建
    backup_on(monkeypatch)
    h = rf.health(include_counts=False)
    assert h == {"relay_mode": "auto", "relay_backup_configured": True, "relay_force": "auto",
                 "relay_breaker": "closed"}


# ══════════════════════════════════════════════════════════════════════════════
# 没配备用站: 老路径一个字节不差
# ══════════════════════════════════════════════════════════════════════════════

_LEGACY_CONFIGS = {
    "no_backup": {},
    "url_only": {"ANTHROPIC_BASE_URL_BACKUP": BU},
    "key_only": {"ANTHROPIC_API_KEY_BACKUP": BK},
    "url_blank": {"ANTHROPIC_BASE_URL_BACKUP": "  ", "ANTHROPIC_API_KEY_BACKUP": BK},
    "forced_primary": {"ANTHROPIC_BASE_URL_BACKUP": BU, "ANTHROPIC_API_KEY_BACKUP": BK, "RELAY_FORCE": "primary"},
}


@pytest.mark.parametrize("cfg", sorted(_LEGACY_CONFIGS))
def test_legacy_parity_without_backup(monkeypatch, caplog, capsys, fake, cfg):
    for k, v in _LEGACY_CONFIGS[cfg].items():
        monkeypatch.setattr(config, k, v)

    def _no_relay(*a, **kw):
        raise AssertionError("primary_only 不该进 relay_failover.call")
    monkeypatch.setattr(rf, "call", _no_relay)

    c = clients.get_anthropic_client()
    assert not isinstance(c, clients.RelayAnthropicClient)
    assert c is fake.instances[0]
    assert fake.built == [LEGACY_KW]
    assert clients.get_anthropic_client() is c

    n = {"i": 0}

    def _down():
        n["i"] += 1
        raise conn_exc()

    with pytest.raises(anthropic.APIConnectionError):
        clients.with_anthropic_retry(_down)
    assert n["i"] == 6 and fake.sleeps == [2.0, 4.0, 8.0, 16.0, 32.0]

    m = {"i": 0}

    def _bad():
        m["i"] += 1
        raise status_exc(400, "max_tokens invalid")

    with pytest.raises(anthropic.BadRequestError):
        clients.with_anthropic_retry(_bad)
    assert m["i"] == 1

    assert text_of(memory_style_call()) == "ok-primary"
    assert fake.calls[-1][2] == {"model": "claude-sonnet-5-5", "max_tokens": 400,
                                 "messages": [{"role": "user", "content": "hi"}]}
    assert fake.built == [LEGACY_KW]                     # 仍是那一个 client, 没有 timeout
    assert breaker_files(fake.tmp) == []
    assert rf.counts() == ZERO
    assert relay_lines(caplog) == []

    monkeypatch.setattr(config, "ANTHROPIC_BASE_URL", "")
    clients.reset_clients()
    clients.get_anthropic_client()
    assert fake.built[-1] == {"api_key": K, "max_retries": 0}
    assert_clean(caplog, capsys)


# ══════════════════════════════════════════════════════════════════════════════
# 自动模式
# ══════════════════════════════════════════════════════════════════════════════

def test_auto_primary_timeout_fails_over_to_backup(monkeypatch, caplog, capsys, fake):
    backup_on(monkeypatch)
    fake.script["primary"] = [timeout_exc()]
    c = clients.get_anthropic_client()
    assert isinstance(c, clients.RelayAnthropicClient) and clients.get_anthropic_client() is c
    assert fake.built == []                               # 懒建: 用到哪个才建哪个

    assert text_of(memory_style_call()) == "ok-backup"
    assert fake.built == [PRIMARY_KW, BACKUP_KW]
    assert fake.instances[1].auth_token is None           # 不带主站 env 里的 bearer
    assert fake.relays_called() == ["primary", "backup"]
    assert fake.calls[0][2] == fake.calls[1][2]           # 同一个请求体, 模型名不变
    assert getattr(clients._relay_tls, "target", None) is None
    assert fake.sleeps == []                              # 切换发生在同一次尝试里, 不退避
    assert rf.counts() == counts(primary_fail=1, backup_ok=1)
    assert relay_lines(caplog) == ["relay: primary failed (APITimeoutError); failing over to backup"]

    fake.script["primary"] = [timeout_exc()]
    assert text_of(memory_style_call()) == "ok-backup"
    assert len(fake.built) == 2                           # client 复用
    lines = relay_lines(caplog)
    assert sum("failing over" in x for x in lines) == 1   # 第一次切换那行每进程只打一次
    assert any(x.startswith("relay: breaker open for 1200s after 2 consecutive") for x in lines)
    assert_clean(caplog, capsys)


@pytest.mark.parametrize("raw,want", [("", 60.0), ("20", 20.0), ("abc", 60.0)])
def test_auto_primary_timeout_knob(monkeypatch, fake, raw, want):
    backup_on(monkeypatch)
    monkeypatch.setattr(config, "RELAY_PRIMARY_TIMEOUT_S", raw)
    memory_style_call()
    assert fake.built == [{**LEGACY_KW, "timeout": want}]


def test_auto_outside_retry_uses_plain_primary(monkeypatch, fake):
    """不经 with_anthropic_retry 的直接调用: 原样的主站 client(不限时), 不切换。"""
    backup_on(monkeypatch)
    c = clients.get_anthropic_client()
    assert text_of(c.messages.create(model="m", max_tokens=1, messages=[])) == "ok-primary"
    assert c.kw == LEGACY_KW
    assert fake.built == [LEGACY_KW]


@pytest.mark.parametrize("status", [401, 402, 403, 404, 408, 409, 429, 500, 502, 503, 504, 520, 522, 524, 529])
def test_auto_failover_statuses(monkeypatch, caplog, capsys, fake, status):
    backup_on(monkeypatch)
    fake.script["primary"] = [status_exc(status)]
    assert text_of(memory_style_call()) == "ok-backup"
    assert fake.relays_called() == ["primary", "backup"]
    assert relay_lines(caplog) == [f"relay: primary failed (HTTP {status}); failing over to backup"]
    assert_clean(caplog, capsys)


def test_auto_stream_error_event_with_http_200_fails_over(monkeypatch, fake):
    backup_on(monkeypatch)
    fake.script["primary"] = [status_exc(200, body={"type": "error", "error": {"type": "overloaded_error"}})]
    assert text_of(memory_style_call()) == "ok-backup"


@pytest.mark.parametrize("status", NO_FAILOVER_STATUSES)
def test_auto_request_errors_do_not_fail_over(monkeypatch, caplog, capsys, fake, status):
    backup_on(monkeypatch)
    err = status_exc(status)
    fake.script["primary"] = [err]
    with pytest.raises(anthropic.APIStatusError) as ei:
        memory_style_call()
    assert ei.value is err
    assert fake.relays_built() == ["primary"] and fake.relays_called() == ["primary"]
    assert fake.sleeps == []
    assert rf.breaker_state(P) == "closed" and rf.counts() == ZERO
    assert_clean(caplog, capsys)


def test_auto_sensitive_words_block_passes_through(monkeypatch, caplog, capsys, fake):
    """D-125: 中转站的敏感词拦截原样上抛, 不切备用站, 也不算熔断失败(重试次数同没配备用站时)。"""
    backup_on(monkeypatch)
    err = status_exc(500, f"Error code: 500 - {{'error': {{'code': 'sensitive_words_detected', 'm': '{SENTINEL}'}}}}")
    fake.script["primary"] = [err] * 6
    with pytest.raises(anthropic.InternalServerError) as ei:
        memory_style_call()
    assert ei.value is err
    assert fake.relays_called() == ["primary"] * 6
    assert fake.relays_built() == ["primary"]
    assert rf.breaker_state(P) == "closed"
    if os.path.exists(rf.state_path(P)):
        assert breaker_file()["fails"] == 0
    assert rf.counts() == ZERO
    assert_clean(caplog, capsys)


def test_auto_backup_failure_keeps_legacy_retry(monkeypatch, fake):
    """切过去之后备用站也挂: 重试判据作用在备用站的异常上, 仍是 6 次、同样的退避。"""
    backup_on(monkeypatch)
    now = [5_000_000.0]
    monkeypatch.setattr(rf, "_now", lambda: now[0])
    fake.script["primary"] = [timeout_exc()] * 6
    last = status_exc(503)
    fake.script["backup"] = [status_exc(503)] * 5 + [last]
    with pytest.raises(anthropic.APIStatusError) as ei:
        memory_style_call()
    assert ei.value is last and ei.value.__context__ is None
    # 前两次尝试各打一次主站, 熔断打开后只打备用
    assert fake.relays_called() == ["primary", "backup", "primary", "backup", "backup", "backup", "backup", "backup"]
    assert fake.sleeps == [2.0, 4.0, 8.0, 16.0, 32.0]
    assert rf.counts() == counts(primary_fail=2, backup_fail=6, breaker_open_skips=4)


def test_memory_call_site_fails_over(monkeypatch, caplog, capsys, fake):
    import memory
    backup_on(monkeypatch)
    fake.script["primary"] = [status_exc(503)]
    fake.script["backup"] = ["整理后的笔记"]
    assert memory.simplify_calibration_notes("旧笔记") == "整理后的笔记"
    assert fake.relays_called() == ["primary", "backup"]
    assert_clean(caplog, capsys)


# ── 流式: ClaudeEngine 缓存了 client, 整段 stream + get_final_message 在备用站重跑 ──────────────────

def test_stream_engine_iterate_fails_over(monkeypatch, caplog, capsys, fake):
    import generator as G
    backup_on(monkeypatch)
    fake.script["primary"] = [timeout_exc()]              # 流开起来了, 在 get_final_message 里超时
    fake.script["backup"] = [json.dumps({"title": "标题", "body": "正文", "keywords": ["词"]}, ensure_ascii=False)]
    eng = G.ClaudeEngine()
    assert isinstance(eng._client, clients.RelayAnthropicClient)
    res = eng.iterate([{"role": "user", "content": "再改改"}], "系统提示")
    assert [c[:2] for c in fake.calls] == [("primary", "stream"), ("backup", "stream")]
    assert fake.calls[0][2] == fake.calls[1][2]
    assert not res.error and res.title == "标题"
    assert fake.built == [PRIMARY_KW, BACKUP_KW]
    assert_clean(caplog, capsys)


@pytest.mark.parametrize("exc_factory,label", [
    (lambda: httpx.ReadTimeout(f"timed out {SENTINEL} {P}"), "StreamIdleTimeout"),
    (lambda: httpx.RemoteProtocolError(f"peer closed connection {SENTINEL} {P}"), "StreamDisconnected"),
    (lambda: httpx.ReadError(f"reset {SENTINEL} {P}"), "StreamDisconnected"),
], ids=["read_timeout", "remote_protocol", "read_error"])
def test_stream_midway_stall_on_primary_fails_over(monkeypatch, caplog, capsys, fake, exc_factory, label):
    """流开起来之后卡住 / 断开: SDK 不包装, get_final_message 里冒出来的是原样的 httpx 异常。
    主站限时 60 秒正是为了它 —— 必须切备用站, 而不是直接抛给 ClaudeEngine(它只接 anthropic.APIError)。"""
    import generator as G
    backup_on(monkeypatch)
    fake.script["primary"] = [exc_factory()]
    fake.script["backup"] = [json.dumps({"title": "标题", "body": "正文", "keywords": ["词"]}, ensure_ascii=False)]
    res = G.ClaudeEngine().iterate([{"role": "user", "content": "再改改"}], "系统提示")
    assert not res.error and res.title == "标题"
    assert [c[:2] for c in fake.calls] == [("primary", "stream"), ("backup", "stream")]
    assert rf.counts() == counts(primary_fail=1, backup_ok=1)
    assert relay_lines(caplog) == [f"relay: primary failed ({label}); failing over to backup"]
    assert_clean(caplog, capsys)


def test_stream_midway_stall_on_backup_is_raised_unchanged(monkeypatch, fake):
    """备用站那次尝试不改写: 原样的 httpx 异常对象抛出去(= 老行为, 老重试判据也不重试它)。"""
    backup_on(monkeypatch)
    err = httpx.ReadTimeout("timed out")
    fake.script["primary"] = [httpx.ReadTimeout("timed out")]
    fake.script["backup"] = [err]
    c = clients.get_anthropic_client()

    def _streamed():
        with c.messages.stream(model="claude-sonnet-5-5", max_tokens=100, messages=[]) as s:
            return s.get_final_message()

    with pytest.raises(httpx.ReadTimeout) as ei:
        clients.with_anthropic_retry(_streamed)
    assert ei.value is err and ei.value.__context__ is None
    assert fake.relays_called() == ["primary", "backup"] and fake.sleeps == []

    backup_on(monkeypatch, force="backup")                # 只走备用: 同样原样抛, 主站一次不碰
    err2 = httpx.RemoteProtocolError("dropped")
    fake.script["backup"] = [err2]
    fake.calls.clear()
    with pytest.raises(httpx.RemoteProtocolError) as ei:
        clients.with_anthropic_retry(_streamed)
    assert ei.value is err2 and fake.relays_called() == ["backup"]


# ── 真 SDK + httpx.MockTransport(不联网、不开端口): 流在半路卡住时 SDK 实际抛的是什么, 由 SDK 自己说了算 ──

_REAL_ANTHROPIC = anthropic.Anthropic       # 模块 import 时还没被 fixture 换掉


def _sse(text):
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5", "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 3, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 5}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return [f"event: {e}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n".encode("utf-8") for e, d in events]


class _SSEBody(httpx.SyncByteStream):
    def __init__(self, chunks, then_raise=None):
        self._chunks, self._then_raise = chunks, then_raise

    def __iter__(self):
        yield from self._chunks
        if self._then_raise is not None:
            raise self._then_raise


def test_real_sdk_stream_stall_fails_over(monkeypatch, caplog, capsys, fake):
    import generator as G
    backup_on(monkeypatch)
    hosts, built = [], []
    good = json.dumps({"title": "备用标题", "body": "正文", "keywords": ["词"]}, ensure_ascii=False)

    def handler(request):
        hosts.append(request.url.host)
        if request.url.host == "primary-host.invalid":     # 头和第一个事件到了, 然后 60 秒没新字节
            body = _SSEBody(_sse("x")[:1], then_raise=httpx.ReadTimeout("timed out", request=request))
        else:
            body = _SSEBody(_sse(good))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)

    def make(**kw):
        built.append(dict(kw))
        return _REAL_ANTHROPIC(**kw, http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(clients.anthropic, "Anthropic", make)
    for noisy in ("httpx", "httpcore", "anthropic"):     # SDK 自己的 DEBUG 行带请求 URL(与本改动无关)
        caplog.set_level(logging.WARNING, logger=noisy)
    res = G.ClaudeEngine().iterate([{"role": "user", "content": "再改改"}], "系统提示")
    assert not res.error and res.title == "备用标题"
    assert hosts == ["primary-host.invalid", "backup-host.invalid"]
    assert built == [PRIMARY_KW, BACKUP_KW]
    assert rf.counts() == counts(primary_fail=1, backup_ok=1)
    assert relay_lines(caplog)[0].startswith("relay: primary failed (")
    assert_clean(caplog, capsys)


def test_stream_slowness_is_not_a_breaker_failure(monkeypatch, fake):
    backup_on(monkeypatch)
    clock = itertools.count(0.0, 100.0)                   # 每次调用都"花了" 100 秒
    monkeypatch.setattr(rf, "_mono", lambda: next(clock))
    c = clients.get_anthropic_client()

    def _streamed():
        with c.messages.stream(model="claude-sonnet-5-5", max_tokens=25000, messages=[]) as s:
            return s.get_final_message()

    for _ in range(3):
        assert text_of(clients.with_anthropic_retry(_streamed)) == "ok-primary"
    assert rf.counts() == counts(primary_ok=3)
    assert rf.breaker_state(P) == "closed"

    memory_style_call()                                   # 非流式慢 100 秒 → 按失败计
    memory_style_call()
    assert rf.counts() == counts(primary_ok=5, primary_slow=2)
    assert rf.breaker_state(P) == "open"
    assert fake.relays_called() == ["primary"] * 5


# ── SUP-001: ttl 被拒是 400, 不切; 降级重拼之后仍打主站 ─────────────────────────────────────────────

def test_ttl_rebuild_still_works_in_auto_mode(monkeypatch, caplog, fake):
    import generator as G
    backup_on(monkeypatch)
    monkeypatch.setattr(G, "_cache_ttl_disabled", False)
    fake.script["primary"] = [status_exc(400, "cache_control.ttl is not supported by this endpoint")]
    c = clients.get_anthropic_client()
    rebuilt = []
    resp = G._call_with_retry(
        lambda: c.messages.create(model="claude-sonnet-5-5", max_tokens=800, messages=[]),
        rebuild=lambda: rebuilt.append(1))
    assert text_of(resp) == "ok-primary"
    assert rebuilt == [1] and G._cache_ttl_disabled is True
    assert fake.relays_built() == ["primary"] and fake.relays_called() == ["primary", "primary"]
    assert rf.counts() == counts(primary_ok=1)
    assert rf.breaker_state(P) == "closed"
    # anthropic_cache_ttl_rejected 遥测本来就带 base_url 和报错原文(老问题, 设计稿 §11 另行处理);
    # 这里只管 relay 自己的日志
    assert not [s for s in FORBIDDEN if s in "\n".join(relay_lines(caplog))]


# ── 熔断跨调用 ───────────────────────────────────────────────────────────────────────────────────────

def test_breaker_opens_skips_primary_then_probe_closes(monkeypatch, caplog, capsys, fake):
    backup_on(monkeypatch)
    now = [2_000_000.0]
    monkeypatch.setattr(rf, "_now", lambda: now[0])
    fake.script["primary"] = [timeout_exc(), timeout_exc()]
    assert text_of(memory_style_call()) == "ok-backup"
    assert rf.breaker_state(P) == "closed"
    assert text_of(memory_style_call()) == "ok-backup"
    assert rf.breaker_state(P) == "open"
    assert clients.relay_status()["relay_breaker"] == "open"

    fake.calls.clear()
    assert text_of(memory_style_call()) == "ok-backup"
    assert fake.relays_called() == ["backup"]
    assert rf.counts()["breaker_open_skips"] == 1

    now[0] += 1200
    assert rf.breaker_state(P) == "half_open"
    fake.calls.clear()
    assert text_of(memory_style_call()) == "ok-primary"   # 试探成功 → 切回
    assert fake.relays_called() == ["primary"]
    assert rf.breaker_state(P) == "closed"
    assert relay_lines(caplog)[-1] == "relay: breaker closed (primary ok again)"
    assert_clean(caplog, capsys)


def test_breaker_failed_probe_reopens(monkeypatch, fake):
    backup_on(monkeypatch)
    now = [3_000_000.0]
    monkeypatch.setattr(rf, "_now", lambda: now[0])
    fake.script["primary"] = [timeout_exc(), timeout_exc(), status_exc(503)]
    memory_style_call()
    memory_style_call()
    now[0] += 1200
    fake.calls.clear()
    assert text_of(memory_style_call()) == "ok-backup"
    assert fake.relays_called() == ["primary", "backup"]
    st = breaker_file()
    assert st["opened_at"] == now[0] and st["probe_at"] is None
    assert rf.breaker_state(P) == "open"


def test_breaker_state_shared_across_proxy_rebuilds(monkeypatch, fake):
    """熔断状态在临时目录的文件里: 换一个 client(或另一个进程)也看得见。"""
    backup_on(monkeypatch)
    fake.script["primary"] = [timeout_exc(), timeout_exc()]
    memory_style_call()
    memory_style_call()
    clients.reset_clients()
    rf._reset_for_tests()
    fake.calls.clear()
    memory_style_call()
    assert fake.relays_called() == ["backup"]


# ── RELAY_FORCE ─────────────────────────────────────────────────────────────────────────────────────

def test_force_backup_never_touches_primary(monkeypatch, caplog, capsys, fake):
    backup_on(monkeypatch, force="backup")
    c = clients.get_anthropic_client()
    assert isinstance(c, clients.RelayAnthropicClient)
    assert text_of(memory_style_call()) == "ok-backup"
    assert text_of(c.messages.create(model="m", max_tokens=1, messages=[])) == "ok-backup"
    assert fake.built == [BACKUP_KW]
    assert breaker_files(fake.tmp) == []
    assert rf.counts() == counts(backup_ok=1)
    assert clients.relay_status()["relay_breaker"] == "off"

    fake.script["backup"] = [conn_exc() for _ in range(6)]   # 只走备用 = 老行为指向备用站: 6 次、同样退避
    with pytest.raises(anthropic.APIConnectionError):
        memory_style_call()
    assert fake.relays_called() == ["backup"] * 8
    assert fake.sleeps == [2.0, 4.0, 8.0, 16.0, 32.0]
    assert fake.relays_built() == ["backup"]
    assert_clean(caplog, capsys)


def test_force_backup_without_backup_is_legacy(monkeypatch, caplog, fake):
    monkeypatch.setattr(config, "RELAY_FORCE", "backup")
    c = clients.get_anthropic_client()
    assert not isinstance(c, clients.RelayAnthropicClient)
    assert text_of(memory_style_call()) == "ok-primary"
    assert fake.built == [LEGACY_KW]
    assert breaker_files(fake.tmp) == []
    assert relay_lines(caplog) == ["relay: RELAY_FORCE=backup but no backup relay is configured; using primary"]


# ── 多线程: 每个线程按自己那次尝试取 client ────────────────────────────────────────────────────────────

def test_threads_route_independently(monkeypatch, fake):
    backup_on(monkeypatch)
    barrier = threading.Barrier(2, timeout=10)

    def _primary_a():
        barrier.wait()          # 两个线程同时停在各自的主站尝试里
        raise timeout_exc()

    def _primary_b():
        barrier.wait()
        return "ok-primary-b"

    fake.thread_script = {"A": {"primary": [_primary_a], "backup": ["ok-backup-a"]},
                          "B": {"primary": [_primary_b], "backup": []}}
    c = clients.get_anthropic_client()
    results, tls_after, errors = {}, {}, []

    def _run(name):
        try:
            results[name] = text_of(clients.with_anthropic_retry(
                lambda: c.messages.create(model="m", max_tokens=1, messages=[])))
            tls_after[name] = getattr(clients._relay_tls, "target", None)
        except BaseException as exc:     # noqa: BLE001 — 交回主线程断言
            errors.append(exc)

    ts = [threading.Thread(target=_run, args=(n,), name=n) for n in "AB"]
    for t in ts:
        t.start()
    for t in ts:
        t.join(15)
    assert not errors, [type(e).__name__ for e in errors]
    assert results == {"A": "ok-backup-a", "B": "ok-primary-b"}
    assert tls_after == {"A": None, "B": None}
    assert fake.relays_called("A") == ["primary", "backup"]
    assert fake.relays_called("B") == ["primary"]
    assert sorted(fake.relays_built()) == ["backup", "primary"]   # 一个限时主站 client 两个线程共用


# ── 杂项 ──────────────────────────────────────────────────────────────────────────────────────────────

def test_backup_key_is_value_masked(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY_BACKUP", "custom-backup-secret-XYZ12345")
    monkeypatch.setattr(logger_utils, "_VALUE_SECRETS", None)
    out = logger_utils.mask_secrets("relay said: custom-backup-secret-XYZ12345 is out of balance")
    assert "custom-backup-secret" not in out and "***REDACTED***" in out


def test_custom_headers_warns_once(monkeypatch, caplog, fake):
    backup_on(monkeypatch, force="backup")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "X-Example: 1")
    memory_style_call()
    clients.reset_clients()
    memory_style_call()
    assert relay_lines(caplog) == [
        "relay: ANTHROPIC_CUSTOM_HEADERS is set; some SDK versions also send it to the backup relay"]


def test_reset_clients_drops_relay_clients(monkeypatch, fake):
    backup_on(monkeypatch)
    proxy = clients.get_anthropic_client()
    memory_style_call()
    assert clients._relay_clients
    clients.reset_clients()
    assert clients._relay_clients == {} and clients._relay_proxy is None
    assert clients.get_anthropic_client() is not proxy


def test_relay_status_fields_have_no_host_or_key(monkeypatch, fake):
    backup_on(monkeypatch)
    fake.script["primary"] = [timeout_exc()]
    memory_style_call()
    st = clients.relay_status()
    assert set(st) == {"relay_mode", "relay_backup_configured", "relay_force", "relay_breaker", "relay_counts"}
    assert st["relay_mode"] == "auto" and st["relay_backup_configured"] is True
    assert st["relay_force"] == "auto" and st["relay_breaker"] == "closed"
    assert set(st["relay_counts"]) == set(rf.COUNTER_KEYS)
    assert all(type(v) is int for v in st["relay_counts"].values())
    blob = json.dumps(st, ensure_ascii=False)
    assert not [s for s in FORBIDDEN if s in blob]


def test_log_hygiene_end_to_end_with_exit_summary(monkeypatch, caplog, capsys, fake):
    backup_on(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "X-Example: 1")
    fake.script["primary"] = [status_exc(402), timeout_exc(), status_exc(400)]
    fake.script["backup"] = [conn_exc(), "ok"]
    memory_style_call()                                   # 402 → 备用连不上 → 重试 → 主站超时 → 备用 ok(熔断打开)
    with pytest.raises(anthropic.BadRequestError):
        fake.script["backup"] = [status_exc(400)]
        memory_style_call()                               # 熔断中直接走备用, 备用 400 原样上抛
    rf._emit_summary()
    lines = relay_lines(caplog)
    summary = [x for x in lines if x.startswith("relay summary: ")]
    assert len(summary) == 1
    payload = summary[0][len("relay summary: "):]
    data = json.loads(payload)
    assert payload == json.dumps(data, sort_keys=True)
    assert data["mode"] == "auto" and data["breaker"] == "open"
    assert data["primary_fail"] == 2 and data["backup_ok"] == 1 and data["backup_fail"] == 2
    assert_clean(caplog, capsys)
