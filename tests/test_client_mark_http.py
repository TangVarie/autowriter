"""每次工具调用、每个新 batch 记下是从哪个客户端来的(2026-11-17, deskcore/clientmark.py, docs/deskcore.md §3.37) —— 接线。

走真的 REST(``A.app``: 鉴权中间件 + ``rest_tool``)和真的 MCP(线上那一串 ``_McpFiniteNumbers(_McpClientMark(...))`` 包着
SDK), 工具也是真的。钉住:
  1. 调用留痕那一行有 ``meta.client_mark``: REST 按 UA 定 / 认不出是 ``rest``; MCP 先 initialize 报了 clientInfo, 同一个指纹
     之后的 tools/call 按它定, 换了 Origin、请求头上的协议版本对不上 initialize 谈定的那一版就不算同一个指纹;
  2. /mcp 挂的还是 ``_McpFiniteNumbers``, 里面一层是 ``_McpClientMark``;
  3. 写作台建的 batch 有 ``params.client_mark``(只有 ``app`` / ``basis``), ``source`` / ``ai_engines`` /
     ``versions.ai_engine`` 照旧; 存档那一行有全的(带证据);
  4. ``ingest_published`` 没给出处时 ``file`` 照旧是 ``workbuddy``, 客户端另记;
  5. 查重那一行有 ``check8``; 底稿转正的 batch 是 ``unknown``(CLI 的进程默认也盖不了它), 拿 check_id 前 8 位对得回去;
  6. 原始的请求头、clientInfo 原串、钥匙不进库、不进日志、不上 /health;
  7. 分类出了意外, 回包一个字节都不变, 记的是 unknown;
  8. 只多 JSONB 里的键, 不加列。
全部是合成项目、现编的 UA、现编的 clientInfo、测试里现编的钥匙。
"""
from __future__ import annotations

import json
import logging

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
pytest.importorskip("mcp")

import deskcore.app as A                                  # noqa: E402
from deskcore import clientmark, core, store              # noqa: E402
from deskcore.identity import Caller                      # noqa: E402
from tests.test_deskcore_commit_judge import BODY_A, BODY_B  # noqa: E402
from tests.test_deskcore_commit_judge import PROJ as COMMIT_PROJ  # noqa: E402
from tests.test_deskcore_commit_judge import _client as _commit_client  # noqa: E402
from tests.test_draft_snapshots import BODY_S, P1, TV, _note, _snap  # noqa: E402
from tests.test_draft_snapshots import _client as _tv_client  # noqa: E402
from tests.test_tool_call_log import (DRAFTS, FAKE_KEY, H, ME, PID, _drafts_client, _rest,  # noqa: E402,F401
                                      _rows, log)

# 2025-06-18 起客户端在 initialize 之后的每个请求上都带 MCP-Protocol-Version(真客户端的样子); initialize 自己带不带都行
MCP_H = {**H, "Accept": "application/json, text/event-stream", "Content-Type": "application/json",
         "MCP-Protocol-Version": "2025-06-18"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "dsh-mcp-client", "version": "0.2.2"}}}
MARK_PHRASE, MARK_CJK = "MARKER phrase with spaces", "示例标记丙"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    clientmark.reset()
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    yield
    clientmark.reset()


def _check_args() -> dict:
    return {"project_id": PID, "drafts": DRAFTS}


def _call(tool: str, args: dict, rid: int = 2) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": tool, "arguments": args}}


@pytest.fixture()
def mcp(log, monkeypatch):
    """真走一遍 SDK, 外面包的是线上那一串(同 tests/test_set_comment_mix_http.py 的 live): 另起一份 MCP 子应用, 身份照
    auth_middleware 的做法放进 ``_caller``。"""
    c = _drafts_client()
    monkeypatch.setattr(core, "sb", lambda: c)
    inner = A._McpFiniteNumbers(A._McpClientMark(A._register_mcp().streamable_http_app()))

    async def as_caller(scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        token = A._caller.set(Caller(ME, "示例写手甲", True))
        try:
            await inner(scope, receive, send)
        finally:
            A._caller.reset(token)
    with fastapi_testclient.TestClient(as_caller, raise_server_exceptions=False) as client:
        yield client, c


def _post(client, body: dict, **headers) -> dict:
    r = client.post("/", headers={**MCP_H, **headers}, content=json.dumps(body, ensure_ascii=False).encode())
    assert r.status_code == 200, r.text
    return r.json()


def _commit_rows(c) -> list[dict]:
    return [r for r in c.rows.get("batch_metrics", []) if (r.get("meta") or {}).get("mode") == "deskcore_commit"]


# ── 1. 调用留痕那一行 ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("headers, want", [
    ({"User-Agent": "claude-code/9.9.9"},
     {"app": "claude_code", "basis": "user_agent", "ua": "claude_code_v9.9.9", "origin": False}),
    ({}, {"app": "rest", "basis": "transport", "ua": "testclient", "origin": False}),
    ({"Origin": "https://example.invalid"}, {"app": "rest", "basis": "transport", "ua": "testclient", "origin": True}),
])
def test_a_rest_call_row_carries_the_client_mark(log, monkeypatch, headers, want):
    c = _drafts_client()
    monkeypatch.setattr(core, "sb", lambda: c)
    r = _rest().post("/tool/check_drafts", headers={**H, **headers}, json=_check_args())
    assert r.status_code == 200, r.text
    (row,) = _rows(c)
    assert row["meta"]["client_mark"] == want
    assert row["meta"]["channel"] == "rest" and row["meta"]["tool"] == "check_drafts"


def test_an_mcp_call_uses_the_client_info_its_connection_sent_on_initialize(mcp):
    client, c = mcp
    init = _post(client, INIT, **{"User-Agent": "node"})
    assert init["result"]["serverInfo"]["name"] == "deskcore"
    out = _post(client, _call("check_drafts", _check_args()), **{"User-Agent": "node"})
    assert out["result"]["isError"] is False
    out = _post(client, _call("check_drafts", _check_args(), 3), **{"User-Agent": "node", "Origin": "https://example.invalid"})
    assert out["result"]["isError"] is False
    first, second = _rows(c)
    assert first["meta"]["channel"] == "mcp"
    assert first["meta"]["client_mark"] == {"app": "dsh_plugin", "basis": "hello", "ua": "node", "origin": False,
                                            "hello": "dsh_mcp_client_v0.2.2"}
    assert second["meta"]["client_mark"] == {"app": "other_mcp", "basis": "transport", "ua": "node", "origin": True}, \
        "带 Origin 的不是 initialize 的那个连接"


def test_a_call_whose_protocol_header_differs_from_the_initialize_is_another_fingerprint(mcp):
    # 部署之后一直在调、没重新 initialize 的另一个客户端(谈定的是 2025-11-25): DSH initialize 之后它的调用也不算 DSH 的
    # 用中性的 UA: 2026-11-23 起 MCP 上 node、不带 Origin、没自报名字的按 UA 算 DSH(见下面 test_node_without_origin_*),
    # 再用 node 就测不到「另一个指纹、没认出来」那一支了
    client, c = mcp
    _post(client, INIT, **{"User-Agent": "undici"})
    _post(client, _call("check_drafts", _check_args()), **{"User-Agent": "undici", "MCP-Protocol-Version": "2025-11-25"})
    _post(client, _call("check_drafts", _check_args(), 3), **{"User-Agent": "undici"})
    other, dsh = _rows(c)
    assert other["meta"]["client_mark"] == {"app": "other_mcp", "basis": "transport", "ua": "undici", "origin": False}
    assert dsh["meta"]["client_mark"]["basis"] == "hello"
    # 同样的场景用 Node 默认的 UA(node)、不带 Origin: 2026-11-23 起 X 的调用按 UA 算 DSH(§3.37「11-23 核对」的前提: 没有这样的别的客户端)
    _post(client, _call("check_drafts", _check_args(), 4), **{"User-Agent": "node", "MCP-Protocol-Version": "2025-11-25"})
    assert _rows(c)[-1]["meta"]["client_mark"] == {"app": "dsh_plugin", "basis": "user_agent", "ua": "node", "origin": False}


def test_node_without_origin_and_without_initialize_is_dsh(mcp):
    # 2026-11-23 核对(docs/deskcore.md §3.37): 写手在 DSH 里用写作台, 请求只有 UA node、不带 Origin、没有 initialize 记录
    client, c = mcp
    out = _post(client, _call("check_drafts", _check_args()), **{"User-Agent": "node"})
    assert out["result"]["isError"] is False
    out = _post(client, _call("check_drafts", _check_args(), 3), **{"User-Agent": "node", "Origin": "https://example.invalid"})
    assert out["result"]["isError"] is False
    first, second = _rows(c)
    assert first["meta"]["client_mark"] == {"app": "dsh_plugin", "basis": "user_agent", "ua": "node", "origin": False}
    assert second["meta"]["client_mark"] == {"app": "other_mcp", "basis": "transport", "ua": "node", "origin": True}


def test_the_fingerprint_uses_the_version_the_server_negotiated(mcp):
    # 客户端要了一个服务端不支持的版本: SDK 回最新的那一版, 客户端之后带的也是它 —— 指纹按谈定的那一版记(app 从 mcp 取支持列表)
    client, c = mcp
    supported, latest = A._MCP_PROTOCOL_VERSIONS
    assert latest in supported and "2099-01-01" not in supported
    init = _post(client, {**INIT, "params": {**INIT["params"], "protocolVersion": "2099-01-01"}}, **{"User-Agent": "node"})
    assert init["result"]["protocolVersion"] == latest
    _post(client, _call("check_drafts", _check_args()), **{"User-Agent": "node", "MCP-Protocol-Version": latest})
    (row,) = _rows(c)
    assert row["meta"]["client_mark"]["basis"] == "hello"


def test_an_mcp_call_without_initialize_falls_back_to_the_user_agent_or_transport(mcp):
    client, c = mcp
    _post(client, _call("check_drafts", _check_args()), **{"User-Agent": "WorkBuddy/9.9.9"})
    _post(client, _call("check_drafts", _check_args(), 3))
    first, second = _rows(c)
    assert first["meta"]["client_mark"] == {"app": "workbuddy_skill", "basis": "user_agent", "ua": "workbuddy_v9.9.9",
                                            "origin": False}
    assert second["meta"]["client_mark"] == {"app": "other_mcp", "basis": "transport", "ua": "testclient",
                                             "origin": False}


def test_the_mounted_mcp_app_is_wrapped_by_both_layers():
    (mount,) = [r for r in A.app.routes if getattr(r, "path", None) == "/mcp"]
    assert isinstance(mount.app, A._McpFiniteNumbers), "外层还是那一层(tests/test_set_comment_mix_http.py 也钉它)"
    assert isinstance(mount.app.app, A._McpClientMark)


# ── 2. batch 和存档那一行 ─────────────────────────────────────────────────────────────────────────────

def test_a_desk_batch_records_the_client_without_changing_source_or_engine(log, monkeypatch):
    c = _commit_client()
    monkeypatch.setattr(core, "sb", lambda: c)
    r = _rest().post("/tool/score_drafts", headers={**H, "User-Agent": "DSH/9.9.9"},
                     json={"project_id": COMMIT_PROJ, "drafts": [{"title": "秋招等回音", "body": BODY_A}]})
    assert r.status_code == 200 and r.json()["result"]["written"] == 1, r.text
    bid = r.json()["result"]["batch_id"]
    batch = next(b for b in c.rows["batches"] if b["id"] == bid)
    want = {"app": "dsh_plugin", "basis": "user_agent", "ua": "dsh_v9.9.9", "origin": False}
    assert batch["params"][clientmark.KEY] == {"app": "dsh_plugin", "basis": "user_agent"}, \
        "batch 只拿两个词; 洗过的证据只在 batch_metrics.meta(下面两行)"
    assert batch["params"]["source"] == "deskcore" and batch["ai_engines"] == ["deskcore"]
    items = {i["id"] for i in c.rows["items"] if i.get("batch_id") == bid}
    versions = [v for v in c.rows["versions"] if v.get("item_id") in items]
    assert versions and all(v["ai_engine"] == "deskcore" for v in versions)
    assert not store.is_ingest_batch(batch)
    batch.setdefault("created_at", "2099-01-01T00:00:00+00:00")
    assert bid in [b["id"] for b in store.desk_batches_since(c, COMMIT_PROJ, "2000-01-01T00:00:00+00:00")], \
        "按 params->>source = deskcore 找 batch 的地方照样找得到"
    (commit,) = _commit_rows(c)
    assert commit["meta"][clientmark.KEY] == want, "存档那一行也记: 改稿落在原来的 batch 里, 是谁改的看这一行"
    (row,) = _rows(c)
    assert row["meta"]["client_mark"] == want


def test_ingest_published_keeps_the_workbuddy_default_and_records_the_client_alongside(log, monkeypatch):
    c = _commit_client()
    monkeypatch.setattr(core, "sb", lambda: c)
    r = _rest().post("/tool/ingest_published", headers={**H, "User-Agent": "WorkBuddy/9.9.9"},
                     json={"project_id": COMMIT_PROJ, "drafts": [{"title": "发过的一篇", "body": BODY_B}]})
    assert r.status_code == 200 and r.json()["result"]["minted"] == 1, r.text
    batch = next(b for b in c.rows["batches"] if b["id"] == r.json()["result"]["batch_id"])
    assert batch["params"]["file"] == "workbuddy", "没给出处时照旧是 workbuddy(读 params.file 的 SQL 不变)"
    assert batch["params"]["source"] == "ingest" and store.is_ingest_batch(batch)
    assert batch["params"][clientmark.KEY] == {"app": "workbuddy_skill", "basis": "user_agent"}


def test_a_snapshot_promoted_batch_is_unknown_and_joins_back_through_check8(log, monkeypatch):
    # 查重经 REST: 记了底稿, 那一行带 check8
    c = _drafts_client()
    monkeypatch.setattr(core, "sb", lambda: c)
    r = _rest().post("/tool/check_drafts", headers={**H, "User-Agent": "DSH/9.9.9"}, json=_check_args())
    assert r.status_code == 200, r.text
    snaps = c.rows["draft_snapshots"]
    assert snaps and len({s["check_id"] for s in snaps}) == 1
    (row,) = _rows(c)
    assert row["meta"]["check8"] == snaps[0]["check_id"][:8]
    assert row["meta"]["client_mark"]["app"] == "dsh_plugin"
    # cli check(不是工具调用)不记 check8, 也不出错
    assert "check8" not in json.dumps(core.check_drafts(c, PID, DRAFTS[:1], user_id=ME, record_snapshot=True))

    # tv-sync 转正: 哪怕在 CLI 的进程默认里跑, 也是 unknown; source / via / check_id 照旧
    snap = _snap("s1", "骑了一百公里", BODY_S + "\n#骑行", days=0, check=snaps[0]["check_id"])
    tv = _tv_client([_note("n1", "骑了一百公里", BODY_S, days=2)], [snap])
    with clientmark.process_default(clientmark.SERVER_JOB):
        out = core.tv_sync(tv, TV)
    assert out["snapshots"]["promoted"] == 1
    (promoted,) = [b for b in tv.rows["batches"] if (b.get("params") or {}).get("via") == "snapshot"]
    assert promoted["params"][clientmark.KEY] == {"app": "unknown", "basis": "none"}
    assert promoted["params"]["source"] == "deskcore" and promoted["params"]["check_id"] == snaps[0]["check_id"]
    assert promoted["params"]["check_id"][:8] == row["meta"]["check8"], "对得回查重那一行"
    assert promoted["project_id"] == P1


def test_cli_ingest_and_tv_sync_ingest_are_server_jobs(monkeypatch):
    tv = _tv_client([_note("n9", "外面写的一篇", BODY_B, days=2)], [])
    with clientmark.process_default(clientmark.SERVER_JOB):
        out = core.tv_sync(tv, TV)
    assert out["ingest"]["minted"] == 1
    batch = next(b for b in tv.rows["batches"] if b["id"] == out["ingest"]["batch_id"])
    assert batch["params"] == {"source": "ingest", "file": f"tv:{TV}",
                               clientmark.KEY: {"app": "server_job", "basis": "process"}}


# ── 3. 原始的串哪儿都不进 ───────────────────────────────────────────────────────────────────────────

def test_no_raw_header_client_info_or_key_is_stored_logged_or_shown(log, monkeypatch, caplog, capsys, mcp):
    caplog.set_level(logging.DEBUG)
    marker_ua = f'node ({MARK_CJK} {MARK_PHRASE}) "{MARK_PHRASE}" {MARK_CJK}/1'
    as_sent = marker_ua.encode("utf-8")                   # 请求头只能是字节; 服务端按 latin-1 解(ASGI 的规矩)
    as_seen = as_sent.decode("latin-1")
    # REST: 存档(batch + 存档那一行 + 调用那一行), UA 里有标记; 再来一次 UA 里有钥匙
    cc = _commit_client()
    monkeypatch.setattr(core, "sb", lambda: cc)
    r = _rest().post("/tool/score_drafts", headers={**H, "User-Agent": as_sent},
                     json={"project_id": COMMIT_PROJ, "drafts": [{"title": "秋招等回音", "body": BODY_A}]})
    assert r.status_code == 200, r.text
    r = _rest().post("/tool/score_drafts", headers={**H, "User-Agent": f"Tool/1 {FAKE_KEY}"},
                     json={"project_id": COMMIT_PROJ, "drafts": [{"title": "绩点二点八", "body": BODY_B}]})
    assert r.status_code == 200, r.text
    rest_rows = _rows(cc)
    assert [row["meta"]["client_mark"]["ua"] for row in rest_rows] == ["node", "other"]
    # MCP: initialize 的 clientInfo 里有标记和钥匙
    client, c = mcp
    monkeypatch.setattr(core, "sb", lambda: c)
    hello = {**INIT, "params": {**INIT["params"], "clientInfo": {"name": f"dsh-mcp-client {MARK_PHRASE} {MARK_CJK}",
                                                                 "version": f"0.2.2 {MARK_PHRASE}"}}}
    _post(client, hello, **{"User-Agent": as_sent})
    _post(client, _call("check_drafts", _check_args()), **{"User-Agent": as_sent})
    (mcp_row,) = _rows(c)
    assert mcp_row["meta"]["client_mark"]["app"] == "dsh_plugin" and mcp_row["meta"]["client_mark"]["basis"] == "hello"
    # /health 不连真库
    monkeypatch.setattr(A, "_health_probe_client", lambda: type("_Dead", (), {"table": lambda *a, **k: 1 / 0})())
    health = _rest().get("/health").text

    stored = [json.dumps(x, ensure_ascii=False) for x in
              rest_rows + [mcp_row] + _commit_rows(cc) + [b.get("params") for b in cc.rows["batches"]]]
    assert sum(clientmark.KEY in blob for blob in stored) == 7, "三行调用留痕 + 两行存档 + 两个新 batch 里各有一份(老 batch 没有)"
    for blob in stored + [health, caplog.text, capsys.readouterr().out]:          # 服务日志 + 打到 stdout 的埋点
        for leaked in (MARK_PHRASE, MARK_CJK, FAKE_KEY, marker_ua, as_seen, MARK_CJK.encode().decode("latin-1"),
                       FAKE_KEY[2:12]):
            assert leaked not in blob, (leaked, blob[:300])
    assert "client_mark" not in health


# ── 4. 分类出了意外: 回包不变, 记 unknown ─────────────────────────────────────────────────────────────

def _boom(*_a, **_k):
    raise RuntimeError("classification exploded")


def test_a_classification_failure_never_changes_a_rest_reply(log, monkeypatch):
    def run():
        c = _drafts_client()
        monkeypatch.setattr(core, "sb", lambda: c)
        r = _rest().post("/tool/check_drafts", headers={**H, "User-Agent": "DSH/9.9.9"}, json=_check_args())
        (row,) = _rows(c)
        return r.status_code, r.json(), row
    status, body, row = run()
    assert row["meta"]["client_mark"]["app"] == "dsh_plugin"
    monkeypatch.setattr(clientmark, "classify", _boom)
    got_status, got_body, got_row = run()
    assert (got_status, got_body) == (status, body)
    assert got_row["meta"]["client_mark"] == {"app": "unknown", "basis": "none"}

    cc = _commit_client()
    monkeypatch.setattr(core, "sb", lambda: cc)
    r = _rest().post("/tool/score_drafts", headers={**H, "User-Agent": "DSH/9.9.9"},
                     json={"project_id": COMMIT_PROJ, "drafts": [{"title": "秋招等回音", "body": BODY_A}]})
    assert r.status_code == 200 and r.json()["result"]["written"] == 1, r.text
    assert cc.rows["batches"][-1]["params"][clientmark.KEY] == {"app": "unknown", "basis": "none"}


@pytest.mark.parametrize("broken", ["classify", "note_hello"])
def test_a_classification_failure_never_changes_an_mcp_reply(mcp, monkeypatch, broken):
    client, c = mcp
    ua = {"User-Agent": "undici"}               # 中性的 UA(node 在 MCP 上会按 UA 认成 DSH, 测不到小表坏掉那一支)
    want_init = _post(client, INIT, **ua)
    want_call = _post(client, _call("check_drafts", _check_args()), **ua)
    clientmark.reset()
    monkeypatch.setattr(clientmark, broken, _boom)
    assert _post(client, INIT, **ua) == want_init
    assert _post(client, _call("check_drafts", _check_args()), **ua) == want_call
    before, after = _rows(c)
    assert before["meta"]["client_mark"]["app"] == "dsh_plugin"
    assert after["meta"]["client_mark"] == ({"app": "unknown", "basis": "none"} if broken == "classify" else
                                            {"app": "other_mcp", "basis": "transport", "ua": "undici", "origin": False})


# ── 5. 不加列 ───────────────────────────────────────────────────────────────────────────────────────

def test_only_jsonb_keys_are_added_no_new_columns(log, monkeypatch):
    cc = _commit_client()
    monkeypatch.setattr(core, "sb", lambda: cc)
    seen: list[tuple[str, set]] = []
    real = cc._execute

    def spy(q):
        if q.op == "insert" and q.table_name in ("batches", "batch_metrics"):
            for row in q.payload if isinstance(q.payload, list) else [q.payload]:
                seen.append((q.table_name, set(row)))
        return real(q)
    cc._execute = spy
    r = _rest().post("/tool/score_drafts", headers={**H, "User-Agent": "DSH/9.9.9"},
                     json={"project_id": COMMIT_PROJ, "drafts": [{"title": "秋招等回音", "body": BODY_A}]})
    assert r.status_code == 200, r.text
    _rows(cc)                                            # 等后台把调用那一行写完
    tables = [t for t, _ in seen]
    assert tables.count("batches") == 1 and tables.count("batch_metrics") == 2
    for table, cols in seen:
        allowed = ({"project_id", "tactic", "params", "ai_engines", "user_id"} if table == "batches" else
                   {"batch_id", "project_id", "user_id", "phase_ms", "counters", "meta", "injection"})
        assert cols <= allowed, (table, cols - allowed)
