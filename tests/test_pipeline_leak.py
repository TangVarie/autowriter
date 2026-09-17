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


@pytest.fixture(autouse=True)
def _fresh_leak_cache():
    """/health 的漏斗块带 60s TTL 缓存 —— 不清掉的话, 上一条测试(或端到端)的
    结果会替这一条回答, 测试顺序一换就红。

    ⚠️ CI 跑 pytest 的环境没装 fastapi(它在 deskcore/requirements.txt 里, 不在
    主锁文件), 所以 deskcore.app 可能 import 不了 —— 那时只让 /health 那几条自己
    importorskip, 别把 store/core 的纯逻辑测试一起拖死(2026-09-17 CI 真红过)。
    """
    try:
        import deskcore.app as A_
    except ImportError:
        yield
        return
    A_._leak_cache.update(at=0.0, value=None)
    yield
    A_._leak_cache.update(at=0.0, value=None)


def _live_probe_client():
    """库探测能过的假客户端 —— /health 现在库探测没过就不扫漏斗(codex P1),
    所以要测漏斗块的那几条得先让 supabase 那格是 ok。"""
    return FakeClient(rows={"projects": []})


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
        assert "leak_pct" in p and "ok" in p


def test_a_fully_broken_project_is_not_masked_by_a_healthy_big_one(monkeypatch):
    """codex review P1: 30/30 全漏 + 100/100 全入库 = 总量 23%, 判"健康" ——
    而那个小项目一条指纹都没进。逐项目也要判。"""
    monkeypatch.setattr(core.store, "angle_leak", lambda sb, days: [
        {"project_id": A, "name": "大而健康", "drawn": 100, "consumed": 100},
        {"project_id": B, "name": "小而全漏", "drawn": 30, "consumed": 0},
    ])
    out = core.pipeline_leak(object())
    assert out["leak_pct"] == 23
    assert out["ok"] is False
    assert out["projects_over_threshold"] == 1
    by = {p["name"]: p for p in out["projects"]}
    assert by["小而全漏"]["ok"] is False and by["大而健康"]["ok"] is True


def test_health_carries_the_pipeline_block_but_the_top_level_ok_ignores_it(monkeypatch):
    """⚠️ 漏斗不是服务健康: 顶层 ok 不看它, 否则 Railway 会因为运营的用法重启容器。
    但它必须在回显里, 不然又是"服务显示健康、流程整个在漏"。"""
    testclient = pytest.importorskip("fastapi.testclient")
    import deskcore.app as A_

    monkeypatch.setenv("DESKCORE_KEYS",
                       '{"k-test": {"user_id": "22222222-2222-2222-2222-222222222222", "name": "t"}}')
    monkeypatch.setattr(A_, "_health_probe_client", _live_probe_client)
    monkeypatch.setattr(core, "pipeline_leak", lambda sb, days=7: {
        "ok": False, "window_days": 7, "drawn": 104, "consumed": 12,
        "leak_pct": 88, "projects_over_threshold": 1,
        "projects": [{"project_id": A, "name": "途鸽", "drawn": 104, "consumed": 12,
                      "leak_pct": 88, "ok": False}],
        "note": "漏了"})
    client = testclient.TestClient(A_.app, raise_server_exceptions=False)
    body = client.get("/health").json()
    pipe = body["config"]["pipeline"]
    assert pipe["ok"] is False and pipe["leak_pct"] == 88
    # ⚠️ /health 不鉴权 —— 项目名单不许出现在里面(codex review P1)
    assert "projects" not in pipe
    assert "途鸽" not in client.get("/health").text
    cfg = body["config"]
    assert body["ok"] == (cfg["supabase"]["ok"] and cfg["vendored_vocab"]["ok"]
                          and cfg["auth"]["ok"]), "漏斗不许进顶层 ok"


def test_health_reports_an_unprobeable_pipeline_instead_of_hiding_it(monkeypatch):
    testclient = pytest.importorskip("fastapi.testclient")
    import deskcore.app as A_

    monkeypatch.setenv("DESKCORE_KEYS",
                       '{"k-test": {"user_id": "22222222-2222-2222-2222-222222222222", "name": "t"}}')
    monkeypatch.setattr(A_, "_health_probe_client", _live_probe_client)

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
    monkeypatch.setattr(A_, "_health_probe_client", _live_probe_client)
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


def test_health_skips_the_leak_scan_when_the_db_probe_already_failed(monkeypatch):
    """codex review P1: 库卡住时, 存活探针等完一次超时不该再为漏斗等第二次 ——
    Railway 会在库故障期间把服务重启掉, 那正是存活探针设计要避免的。"""
    testclient = pytest.importorskip("fastapi.testclient")
    import deskcore.app as A_

    monkeypatch.setenv("DESKCORE_KEYS",
                       '{"k-test": {"user_id": "22222222-2222-2222-2222-222222222222", "name": "t"}}')

    class _Dead:
        def table(self, *_a, **_k):
            raise RuntimeError("库挂了")
    monkeypatch.setattr(A_, "_health_probe_client", lambda: _Dead())
    calls = {"n": 0}

    def _leak(sb, days=7):
        calls["n"] += 1
        raise AssertionError("库探测没过就不该来扫台账")
    monkeypatch.setattr(core, "pipeline_leak", _leak)
    client = testclient.TestClient(A_.app, raise_server_exceptions=False)
    body = client.get("/health").json()
    assert body["config"]["supabase"]["ok"] is False
    assert body["config"]["pipeline"]["ok"] is None
    assert "跳过" in body["config"]["pipeline"]["note"]
    assert calls["n"] == 0


def test_a_failed_leak_scan_is_cached_briefly(monkeypatch):
    """失败也缓存: 不然库慢的时候每次 ping 都再等一次。"""
    testclient = pytest.importorskip("fastapi.testclient")
    import deskcore.app as A_

    monkeypatch.setenv("DESKCORE_KEYS",
                       '{"k-test": {"user_id": "22222222-2222-2222-2222-222222222222", "name": "t"}}')
    monkeypatch.setattr(A_, "_health_probe_client", _live_probe_client)
    calls = {"n": 0}

    def _boom(sb, days=7):
        calls["n"] += 1
        raise RuntimeError("慢")
    monkeypatch.setattr(core, "pipeline_leak", _boom)
    client = testclient.TestClient(A_.app, raise_server_exceptions=False)
    for _ in range(3):
        pipe = client.get("/health").json()["config"]["pipeline"]
        assert pipe["ok"] is None and "不再重试" in pipe["note"]
    assert calls["n"] == 1
