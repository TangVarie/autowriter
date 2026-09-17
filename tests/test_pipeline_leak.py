"""「发了角度没入库」要看得见 —— doctor 和 /health 都报它。

09-11 起 78% 的角度发出去了、稿子没走 commit, 这个数字在角度台账里躺了一周,
而 /health 一直 ok —— 服务确实没坏, 坏的是流程, 而流程没有任何仪表。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from deskcore import core, store
from tests.fakes import FakeClient

A = "aaaaaaaa-0000-0000-0000-00000000000a"
B = "aaaaaaaa-0000-0000-0000-00000000000b"


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _ledger(project, drawn_days_ago, consumed=False, n=1):
    return [{"id": f"{project[-1]}-{drawn_days_ago}-{i}", "project_id": project,
             "angle_key": f"k{i}", "drawn_at": _ago(drawn_days_ago),
             "consumed_version_id": "v" if consumed else None}
            for i in range(n)]


def test_angle_leak_counts_per_project_inside_the_window_and_names_them():
    c = FakeClient(rows={
        "projects": [{"id": A, "name": "途鸽"}, {"id": B, "name": "sportsix"}],
        "angle_ledger": (
            _ledger(A, 1, consumed=True, n=2) + _ledger(A, 2, n=3)     # 5 发 2 销
            + _ledger(B, 3, n=4)                                       # 4 发 0 销
            + _ledger(B, 30, n=10)                                     # 窗口外, 不算
        ),
    })
    out = store.angle_leak(c, days=7)
    by = {p["name"]: p for p in out}
    assert by["途鸽"] == {"project_id": A, "name": "途鸽", "drawn": 5, "consumed": 2}
    assert by["sportsix"] == {"project_id": B, "name": "sportsix", "drawn": 4, "consumed": 0}
    assert [p["name"] for p in out] == ["sportsix", "途鸽"], "漏得最多的排前面"


def test_angle_leak_is_empty_when_nothing_was_drawn():
    assert store.angle_leak(FakeClient(rows={"angle_ledger": []}), days=7) == []


@pytest.mark.parametrize("per, ok, pct", [
    # 09-15 的真实数字: 发 104 销 12 → 88% —— 必须报
    ([{"project_id": A, "name": "x", "drawn": 104, "consumed": 12}], False, 88),
    # 09-10 的真实数字: 18% —— 健康
    ([{"project_id": A, "name": "x", "drawn": 122, "consumed": 100}], True, 18),
    # 样本太小: 一个人试写 5 条没入库不是事故
    ([{"project_id": A, "name": "x", "drawn": 5, "consumed": 0}], True, 100),
    ([], True, 0),
])
def test_pipeline_leak_verdict(monkeypatch, per, ok, pct):
    monkeypatch.setattr(core.store, "angle_leak", lambda sb, days: per)
    out = core.pipeline_leak(object())
    assert out["ok"] is ok
    assert out["leak_pct"] == pct
    assert out["window_days"] == core.LEAK_WINDOW_DAYS
    for p in out["projects"]:
        assert "leak_pct" in p


def test_health_carries_the_pipeline_block_but_the_top_level_ok_ignores_it(monkeypatch):
    """⚠️ 漏斗不是服务健康: 顶层 ok 不看它, 否则 Railway 会因为运营的用法重启容器。
    但它必须在回显里, 不然又是"服务显示健康、流程整个在漏"。"""
    testclient = pytest.importorskip("fastapi.testclient")
    import deskcore.app as A_

    monkeypatch.setenv("DESKCORE_KEYS",
                       '{"k-test": {"user_id": "22222222-2222-2222-2222-222222222222", "name": "t"}}')
    monkeypatch.setattr(A_, "_health_probe_client", lambda: object())
    monkeypatch.setattr(core, "pipeline_leak", lambda sb, days=7: {
        "ok": False, "window_days": 7, "drawn": 104, "consumed": 12,
        "leak_pct": 88, "projects": [], "note": "漏了"})
    client = testclient.TestClient(A_.app, raise_server_exceptions=False)
    body = client.get("/health").json()
    pipe = body["config"]["pipeline"]
    assert pipe["ok"] is False and pipe["leak_pct"] == 88
    cfg = body["config"]
    assert body["ok"] == (cfg["supabase"]["ok"] and cfg["vendored_vocab"]["ok"]
                          and cfg["auth"]["ok"]), "漏斗不许进顶层 ok"


def test_health_reports_an_unprobeable_pipeline_instead_of_hiding_it(monkeypatch):
    testclient = pytest.importorskip("fastapi.testclient")
    import deskcore.app as A_

    monkeypatch.setenv("DESKCORE_KEYS",
                       '{"k-test": {"user_id": "22222222-2222-2222-2222-222222222222", "name": "t"}}')
    monkeypatch.setattr(A_, "_health_probe_client", lambda: object())
    A_._leak_cache.update(at=0.0, value=None)     # 别让上一条测试的缓存替它回答

    def _boom(sb, days=7):
        raise RuntimeError("库挂了")
    monkeypatch.setattr(core, "pipeline_leak", _boom)
    client = testclient.TestClient(A_.app, raise_server_exceptions=False)
    pipe = client.get("/health").json()["config"]["pipeline"]
    assert pipe["ok"] is None
    assert "探不到" in pipe["note"]


def test_health_caches_the_leak_probe_between_pings(monkeypatch):
    """/health 是 Railway 的存活探针, 几十秒一次; 7 天的漏斗一分钟内不会变。
    每次 ping 翻一遍台账是白花的, 而且把存活探针拖慢。"""
    testclient = pytest.importorskip("fastapi.testclient")
    import deskcore.app as A_

    monkeypatch.setenv("DESKCORE_KEYS",
                       '{"k-test": {"user_id": "22222222-2222-2222-2222-222222222222", "name": "t"}}')
    monkeypatch.setattr(A_, "_health_probe_client", lambda: object())
    A_._leak_cache.update(at=0.0, value=None)
    calls = {"n": 0}

    def _leak(sb, days=7):
        calls["n"] += 1
        return {"ok": True, "window_days": 7, "drawn": 1, "consumed": 1,
                "leak_pct": 0, "projects": [], "note": ""}
    monkeypatch.setattr(core, "pipeline_leak", _leak)
    client = testclient.TestClient(A_.app, raise_server_exceptions=False)
    for _ in range(3):
        assert client.get("/health").json()["config"]["pipeline"]["ok"] is True
    assert calls["n"] == 1, "TTL 内只该查一次"
