import threading
import concurrent.futures as cf
import json

import anyio
import pytest

from deskcore import core, isolation, store, tools
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
TARGET = {"id": "west", "name": "西屋911gt", "brand": "西屋", "owner_id": ME}
OTHER = {"id": "patch", "name": "Hatherine痘痘贴", "brand": "Hatherine", "owner_id": ME,
         "default_params": '{"isolation_terms":["祛痘","76片"]}'}


def client():
    return FakeClient(rows={"projects": [TARGET, OTHER], "memories": []})


def test_rule_contamination_is_refused_before_any_write(monkeypatch):
    c = client()
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    for text in ["禁止说祛痘", "只能说HATHERINE", "痘痘贴有76片"]:
        with pytest.raises(ValueError, match="未写入"):
            core.record_rule(c, "west", text, user_id=ME)
    assert c.rows["memories"] == []
    out = core.record_rule(c, "west", "西屋标题控制在20字", user_id=ME)
    assert out["memory_id"] and len(c.rows["memories"]) == 1


def test_other_owner_and_same_brand_are_not_false_conflicts():
    target = {**TARGET, "brand": "RIO", "name": "RIO-main"}
    same = {"id": "same", "brand": "RIO", "name": "RIO-second"}
    assert isolation.foreign_markers("RIO适量饮用", target, [target, same]) == []
    assert isolation.foreign_markers("priority matters", TARGET, [{"id": "rio", "brand": "RIO"}]) == []
    c = client()
    c.rows["projects"].append({"id": "private", "owner_id": "other", "brand": "不公开"})
    assert {p["id"] for p in store.isolation_projects(c, owner_id=ME)} == {"west", "patch"}


def test_global_rule_cannot_spread_current_project_markers(monkeypatch):
    c = client()
    with pytest.raises(ValueError, match="全局规则"):
        core.record_rule(c, "west", "西屋的规则全局使用", scope="global", user_id=ME)
    assert c.rows["memories"] == []


def test_shadow_uses_same_model_and_excludes_just_committed_vectors(monkeypatch):
    c = client()
    model = core.dedup.EMBEDDING_MODEL
    c.rows["draft_fingerprints"] = [
        {"id": "1", "project_id": "west", "version_id": "old", "title_embedding": [1., 0.], "embedding_model": model},
        {"id": "2", "project_id": "west", "version_id": "new", "title_embedding": [0., 1.], "embedding_model": model},
        {"id": "3", "project_id": "patch", "version_id": "other", "title_embedding": [0., 1.], "embedding_model": model},
        {"id": "4", "project_id": "west", "version_id": "wrong-model", "title_embedding": [0., 1.], "embedding_model": "old-model"}]
    out = core._cross_project_shadow(c, "west", ME, [{"title": "痘痘贴", "body": ""}], [[0., 1.]], {"new"})
    assert out["status"] == "shadow" and len(out["flags"]) == 1
    assert out["flags"][0]["title_vector"]["other_project_id"] == "patch"
    assert out["flags"][0]["title_vector"]["own_similarity"] == 0
    assert len(c.rows["draft_fingerprints"]) == 4, "影子检查不能修改任何指纹"


def test_real_mcp_wrapper_keeps_identity_and_does_not_block_loop(monkeypatch):
    pytest.importorskip("mcp")
    pytest.importorskip("fastapi")
    import deskcore.app as A
    from deskcore.identity import Caller
    entered, release = threading.Event(), threading.Event()
    seen = []
    def slow(_user_id=None):
        seen.append(_user_id)
        entered.set()
        assert release.wait(2), "MCP 同步函数阻塞了事件循环"
        return {"user": _user_id}
    monkeypatch.setattr(tools, "TOOLS", {"slow": (slow, True)})
    mcp = A._register_mcp()
    async def run():
        token = A._caller.set(Caller(ME))
        try:
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(mcp._tool_manager.call_tool, "slow", {"_user_id": "forged"})
                with anyio.fail_after(1):
                    while not entered.is_set():
                        await anyio.sleep(0.01)
                release.set()
        finally:
            release.set()
            A._caller.reset(token)
    anyio.run(run)
    assert seen == [ME]


def test_rest_rule_rejection_is_400_and_preserves_message(monkeypatch):
    pytest.importorskip("mcp")
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from deskcore.app import app
    c = client()
    monkeypatch.setenv("DESKCORE_KEYS", json.dumps({"test-key": {"user_id": ME, "name": "测试"}}))
    monkeypatch.setattr(core, "sb", lambda: c)
    # REST adapter does not need to restart the one-shot MCP lifespan used by other tests.
    http = TestClient(app)
    r = http.post("/tool/record_rule", headers={"X-Deskcore-Key": "test-key"},
                  json={"project_id": "west", "content": "不许说祛痘", "severity": "hard"})
    assert r.status_code == 400 and "未写入" in r.json()["detail"]
    assert c.rows["memories"] == []


def test_isolation_timeout_cannot_accumulate_workers(monkeypatch):
    release = threading.Event()
    pool, slots = cf.ThreadPoolExecutor(max_workers=1), threading.BoundedSemaphore(1)
    monkeypatch.setattr(core, "_ISOLATION_POOL", pool)
    monkeypatch.setattr(core, "_ISOLATION_SLOTS", slots)
    def slow(*a):
        assert release.wait(4)
        return {"status": "shadow", "flags": []}
    monkeypatch.setattr(core, "_cross_project_shadow", slow)
    try:
        assert core._cross_project_shadow_bounded(None, "west", ME, [], [], set())["status"] == "timeout"
        for _ in range(20):
            assert core._cross_project_shadow_bounded(None, "west", ME, [], [], set())["status"] == "busy"
        assert len(pool._threads) == 1
    finally:
        release.set()
        pool.shutdown(wait=True)


def test_markers_survive_when_the_vector_half_times_out(monkeypatch):
    """10-14 / 10-19 五批 15~20 篇全超时, 以前连品牌词标记一起丢 (W-19 等于没开)。"""
    c = client()
    release = threading.Event()
    pool, slots = cf.ThreadPoolExecutor(max_workers=1), threading.BoundedSemaphore(1)
    monkeypatch.setattr(core, "_ISOLATION_POOL", pool)
    monkeypatch.setattr(core, "_ISOLATION_SLOTS", slots)
    monkeypatch.setattr(core, "_ISOLATION_TIMEOUT_SEC", 0.3)
    monkeypatch.setattr(core, "_ISOLATION_COMPARE_RESERVE_SEC", 0)
    monkeypatch.setattr(store, "isolation_fingerprints", lambda *a: (release.wait(4), [])[1])   # 拉样本卡住
    drafts = [{"title": "西屋好用", "body": ""}, {"title": "痘痘贴76片", "body": ""}]
    try:
        out = core._cross_project_shadow_bounded(c, "west", ME, drafts, [[1., 0.], [0., 1.]], set())
    finally:
        release.set()
        pool.shutdown(wait=True)
    assert out["status"] == "timeout" and out["markers_checked"] is True
    assert [f["index"] for f in out["flags"]] == [1] and out["flags"][0]["markers"]
    assert "不能声称已完整排除" in out["note"]


def test_vector_half_stops_at_its_own_deadline_instead_of_timing_out(monkeypatch):
    c = client()
    model = core.dedup.EMBEDDING_MODEL
    c.rows["draft_fingerprints"] = [
        {"id": "1", "project_id": "west", "version_id": "a", "title_embedding": [1., 0.], "embedding_model": model},
        {"id": "2", "project_id": "patch", "version_id": "b", "title_embedding": [0., 1.], "embedding_model": model}]
    real, seen = store.isolation_fingerprints, []

    def slow(sb, pid, m):
        seen.append(pid)
        core.time.sleep(0.15)
        return real(sb, pid, m)

    monkeypatch.setattr(store, "isolation_fingerprints", slow)
    monkeypatch.setattr(core, "_ISOLATION_COMPARE_RESERVE_SEC", 0)
    out = core._cross_project_shadow(c, "west", ME, [{"title": "痘痘贴", "body": ""}], [[0., 1.]], set(),
                                     None, core.time.monotonic() + 0.1)
    assert seen == ["west"] and out["vector_partial"] is True
    assert out["vector_projects_sampled"] == 1 and out["projects_total"] == 2
    assert out["flags"] and out["flags"][0]["markers"], "向量没比完, 标记照报"


def test_matrix_and_pairwise_scores_agree(monkeypatch):
    vecs = {0: [1., 0., 0.], 2: [0.6, 0.8, 0.]}
    vs = [[0., 1., 0.], [1., 1., 0.], [0., 0., 0.], [1., 2.]]
    fast = core._vector_scores(vecs, vs)
    monkeypatch.setattr(core.dedup, "similarity_matrix", lambda *a: None)
    slow = core._vector_scores(vecs, vs)
    assert fast.keys() == slow.keys() == {0, 2}
    assert all(abs(fast[i] - slow[i]) < 1e-9 for i in fast)
    assert core._vector_scores({0: [1., 0.]}, [[1., 0., 0.]]) == {}, "维度不同的样本不算这个项目"
