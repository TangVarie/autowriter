"""端到端: 用真实的 FastAPI app, 按协议的顺序把整条流程走一遍。

单测各盯各的函数; 这里盯的是**接线**: REST 层的参数透传(尤其 get_protocol 新加
的 local_version)、返回值里模型要看的那几个键真的在、跳过 check 直接 commit
真的进不了库、/health 真的带 pipeline 块、MCP 层的工具 schema 没把内部参数
漏出去。2026-09-17 那次塌方就是"每个零件都对、接起来不对"。
"""

from __future__ import annotations

import base64

import anyio
import pytest

from deskcore import core
from tests.fakes import FakeClient

fastapi_testclient = pytest.importorskip("fastapi.testclient")

ME = "22222222-2222-2222-2222-222222222222"
KEYS = '{"k-e2e": {"user_id": "%s", "name": "e2e"}}' % ME
H = {"X-Deskcore-Key": "k-e2e"}

BODIES = [
    ("秋招投了六十份简历没回音", "投到第六十份的时候我开始怀疑是不是邮箱坏了, 后来发现坏的是简历第一行。" * 4),
    ("绩点二点八也拿到了终面", "绩点这东西在群面之后就没人再提了, 面试官只问我上一段实习做成了什么。" * 4),
    ("签机构前我问了三个问题", "签之前我问了退费条款、导师是谁、上一届去了哪, 三个问题把销售问沉默了。" * 4),
]


@pytest.fixture
def env(monkeypatch):
    import deskcore.app as A_
    fake = FakeClient(rows={"projects": []})
    monkeypatch.setenv("DESKCORE_KEYS", KEYS)
    monkeypatch.setattr(core, "sb", lambda: fake)
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    monkeypatch.setattr(A_, "_health_probe_client", lambda: fake)
    A_._leak_cache.update(at=0.0, value=None)
    client = fastapi_testclient.TestClient(A_.app, raise_server_exceptions=False)

    def call(_tool, **args):
        r = client.post(f"/tool/{_tool}", json=args, headers=H)
        assert r.status_code == 200, f"{_tool}: {r.status_code} {r.text[:300]}"
        return r.json()["result"]
    return client, call, fake


def test_the_whole_protocol_end_to_end(env):
    client, call, fake = env
    from deskcore import tools as T

    # ── 0. 开场核版本: 三种分支都要在 REST 上打得通 ──────────────────────
    _, current = T.protocol_text()
    r = call("get_protocol", local_version=current)
    assert r["up_to_date"] is True and "protocol" not in r, "版本一致时不许再发全文"
    r = call("get_protocol", local_version="<!-- protocol_version: deadbeefcafe -->")
    assert r["up_to_date"] is False and r["protocol"] and "重新导入" in r["warning"]
    r = call("get_protocol")
    assert r["up_to_date"] is None and r["protocol"]

    # ── 1. 建项目 → 打开 ────────────────────────────────────────────────
    r = call("create_project", name="e2e-途鸽-D9", brand="途鸽")
    assert r["created"] is True
    pid = r["project_id"]
    brief = call("open_project", project_id=pid)
    assert "counts" in brief and "p0" in brief

    # ── 2. 发牌 ─────────────────────────────────────────────────────────
    r = call("draw_angles", project_id=pid, n=3)
    assert r["delivered"] == 3 and r["prompt_block"]
    keys = [a["angle_key"] for a in r["angles"]]
    assert len(set(keys)) == 3

    # ── 3. 查重 → 定稿入库(带坐标) ──────────────────────────────────────
    drafts = [{"title": t, "body": b, "angle_key": k} for (t, b), k in zip(BODIES, keys)]
    r = call("check_drafts", project_id=pid, drafts=drafts)
    assert r["summary"]["total"] == 3 and r["summary"]["reject"] == 0
    r = call("commit_drafts", project_id=pid, drafts=drafts)
    assert r["written"] == 3 and r["rejected"] == []
    assert r["unattributed"] == 0 and "unattributed_warning" not in r
    assert r["consumed_angles"] == 3, r
    assert len(r["version_ids"]) == 3 and r["batch_id"]
    assert "gate_summary" in r
    batch_id, vids = r["batch_id"], r["version_ids"]

    # ── 4. 跳过 check 直接再提交同一批: 一条都进不去 ─────────────────────
    r = call("commit_drafts", project_id=pid, drafts=[{**d, "angle_key": None} for d in drafts])
    assert r["written"] == 0
    assert [x["index"] for x in r["rejected"]] == [0, 1, 2]
    assert {x["gate"] for x in r["rejected"]} == {"pre_commit"}
    assert "check_drafts" in r["note"]
    assert len(fake.rows["draft_fingerprints"]) == 3, "指纹库不许多出一条"

    # ── 5. 导出 → 人审 ───────────────────────────────────────────────────
    # 导出走 PostgREST 的 embedded join(items → versions / batches); 假库不会
    # 自己嵌, 这里手动补上那一层(同 test_deskcore_export 的回环测试)。
    for row in fake.rows["items"]:
        row["versions"] = [v for v in fake.rows["versions"] if v["item_id"] == row["id"]]
        row["batches"] = {"project_id": pid}
    r = call("export_drafts", project_id=pid, batch_id=batch_id)
    assert r["filename"].endswith(".xlsx")
    assert base64.b64decode(r["xlsx_base64"])[:2] == b"PK"
    # review 走 versions → items!inner → batches!inner 的嵌套查询, 假库同样要手动嵌。
    items_by_id = {i["id"]: i for i in fake.rows["items"]}
    for v in fake.rows["versions"]:
        it = items_by_id[v["item_id"]]
        v["items"] = {"id": it["id"], "status": it["status"],
                      "decision_source": it.get("decision_source"),
                      "batch_id": it["batch_id"], "batches": {"project_id": pid}}
    r = call("review_drafts", project_id=pid,
             decisions=[{"version_id": vids[0], "decision": "approved"},
                        {"version_id": vids[1], "decision": "needs_revision"}])
    assert r["reviewed"] == 2
    statuses = {i["status"] for i in fake.rows["items"]}
    assert statuses == {"approved", "needs_revision", "pending"}

    # ── 6. /health 带漏斗块, 且这一批全销账 → 不红 ───────────────────────
    h = client.get("/health").json()
    pipe = h["config"]["pipeline"]
    assert pipe["ok"] is True and pipe["drawn"] == 3 and pipe["consumed"] == 3, pipe
    assert "projects" not in pipe and "e2e-途鸽-D9" not in client.get("/health").text, \
        "/health 不鉴权, 项目名单不许出现在里面"
    assert "pipeline" not in h or h["ok"] == (h["config"]["supabase"]["ok"]
                                             and h["config"]["vendored_vocab"]["ok"]
                                             and h["config"]["auth"]["ok"])


def test_mcp_tool_surface_matches_the_protocol():
    """MCP 层暴露给模型的 schema: get_protocol 有 local_version, 内部参数一个
    不许漏出去; instructions 说的是"核版本"而不是"取全文"。"""
    import deskcore.app as A_
    assert A_._mcp is not None, "mcp SDK 没装, /mcp 不存在"
    tools_ = anyio.run(A_._mcp.list_tools)
    by = {t.name: t for t in tools_}
    from deskcore import tools as T
    assert set(by) == set(T.TOOLS)
    gp = by["get_protocol"].inputSchema["properties"]
    assert "local_version" in gp and "_user_id" not in gp
    for t in tools_:
        assert not any(k.startswith("_") for k in t.inputSchema.get("properties", {})), t.name
    assert "protocol_version" in (A_._mcp.instructions or "")
    assert "取完整协议" not in (A_._mcp.instructions or "")
    # commit_drafts 的说明要提到新键 —— 模型只看这个
    assert "unattributed" in by["commit_drafts"].description
    assert "pre_commit" in by["commit_drafts"].description
