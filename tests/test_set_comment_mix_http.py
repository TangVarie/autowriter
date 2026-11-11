"""set_comment_mix 在 HTTP 那一层(2026-11-11, docs/deskcore.md §3.23): REST、MCP、/tools、/health。

钉住: 留痕里的 by 是服务端那把钥匙的备注名(app 注入), 调用方自己传的 ``_caller_name`` / ``_user_id`` 不算, 钥匙本身不进库、
不进回包; 报的那几句是 400、别人的项目是 403; MCP 的入参只有 project_id 和 mix。
全部是合成项目、合成品牌、假列名和测试里现编的钥匙。纯函数那一层在 tests/test_set_comment_mix.py。
"""
from __future__ import annotations

import json

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

import deskcore.app as A                                  # noqa: E402
from deskcore import core, tools                          # noqa: E402
from tests.test_comment_set_score import _client          # noqa: E402
from tests.test_deskcore_score_and_comments import ME     # noqa: E402
from tests.test_set_comment_mix import _params, _pid, _writes  # noqa: E402

FAKE_KEY = "k-mixtest-0123456789abcdefFAKE"               # 测试里现编的, 不是任何真钥匙
KEYS = json.dumps({FAKE_KEY: {"user_id": ME, "name": "示例写手甲"}}, ensure_ascii=False)
H = {"X-Deskcore-Key": FAKE_KEY}


@pytest.fixture()
def http(monkeypatch):
    c = _client()
    monkeypatch.setenv("DESKCORE_KEYS", KEYS)
    monkeypatch.setattr(core, "sb", lambda: c)
    return fastapi_testclient.TestClient(A.app, raise_server_exceptions=False), c


def test_rest_call_changes_the_mix_and_signs_the_log_with_the_key_name_not_the_key(http):
    client, c = http
    r = client.post("/tool/set_comment_mix", headers=H, json={
        "project_id": _pid(c), "mix": {"brand_max": 60},
        "_caller_name": "冒名", "_user_id": "22222222-2222-2222-2222-222222222222"})
    assert r.status_code == 200, r.text
    out = r.json()["result"]
    assert out["changed"] is True and out["summary"] == "「示例项目」一组评论的配比改了: 提品牌的上限 50% → 60%。"
    (entry,) = _params(c)[core.COMMENT_SET_LOG_KEY]
    assert entry["by"] == "示例写手甲", "署名是服务端那把钥匙的备注名, 调用方传的不算"
    assert FAKE_KEY not in c.rows["projects"][0]["default_params"] and FAKE_KEY not in r.text


@pytest.mark.parametrize("body, status, part", [
    ({"mix": {"column": "另一列"}}, 400, "评论贴进飞书哪一列在这里改不了"),
    ({"mix": {"brand_max": 0.71}}, 400, "超过了 100%"),
    ({"mix": {}}, 400, "mix 里没有要改的"),
])
def test_rest_refusals_are_400_with_the_sentence(http, body, status, part):
    client, c = http
    r = client.post("/tool/set_comment_mix", headers=H, json=dict(body, project_id=_pid(c)))
    assert r.status_code == status and part in r.json()["detail"], r.text
    assert _writes(c) == []


def test_rest_someone_elses_project_is_403(http, monkeypatch):
    client, c = http
    c.rows["projects"][0]["owner_id"] = "22222222-2222-2222-2222-222222222222"
    r = client.post("/tool/set_comment_mix", headers=H, json={"project_id": _pid(c), "mix": {"brand_max": 0.6}})
    assert r.status_code == 403 and _writes(c) == []


def test_listed_in_tools_health_and_mcp(http, monkeypatch):
    anyio = pytest.importorskip("anyio")
    client, _c = http

    class _Dead:
        def table(self, *_a, **_k):
            raise RuntimeError("no db in this test")
    monkeypatch.setattr(A, "_health_probe_client", lambda: _Dead())
    assert "set_comment_mix" in client.get("/health").json()["tools"]
    listed = {t["name"]: t for t in client.get("/tools", headers=H).json()["tools"]}
    assert listed["set_comment_mix"]["needs_identity"] is True
    assert listed["set_comment_mix"]["description"] == tools.set_comment_mix.__doc__.strip()
    assert A._mcp is not None
    by = {t.name: t for t in anyio.run(A._mcp.list_tools)}
    schema = by["set_comment_mix"].inputSchema
    assert set(schema["properties"]) == {"project_id", "mix"} and sorted(schema["required"]) == ["mix", "project_id"]
    assert by["set_comment_mix"].description.startswith("改这个项目一组评论的配比")


def test_mcp_call_injects_the_name_and_hides_internal_params(http):
    anyio = pytest.importorskip("anyio")
    client, c = http
    token = A._caller.set(A.identity.Caller(ME, "示例写手乙", True))
    try:
        anyio.run(lambda: A._mcp.call_tool("set_comment_mix", {"project_id": _pid(c), "mix": {"short_min": None},
                                                               "_caller_name": "冒名"}))
    finally:
        A._caller.reset(token)
    (entry,) = _params(c)[core.COMMENT_SET_LOG_KEY]
    assert entry["by"] == "示例写手乙" and "short_min" not in entry["after"]
