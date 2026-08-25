"""deskcore 的 REST 适配层: 异常 → HTTP 状态码的映射。

这一层的价值全在**分类**。core 特意把三件事分成三个异常:

  · ``ProjectNotFound`` —— 项目 ID 打错了 / 项目已删。调用方该换个 ID。
  · ``PermissionError`` —— 项目在, 但不是你的。调用方该停手。
  · 其它 —— 查重真的挂了。调用方该重试, 我们该被叫醒。

适配层不映射就等于白分: 前两个一起掉进 500, 调用方(通常是个模型)被告知
"服务端故障、待会儿重试", 于是拿同一个错 ID 一直试; 而错误监控里堆满了
其实完全正常的"查无此项目"。

⚠️ ``raise_server_exceptions=False`` 是必需的 —— 否则 TestClient 会把 500
那一路的原始异常直接抛出来, 而这里要断言的正是**响应码**。
"""

from __future__ import annotations

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

from deskcore import core                     # noqa: E402
import deskcore.app as A                      # noqa: E402


KEYS = ('{"k-test": {"user_id": "22222222-2222-2222-2222-222222222222",'
        ' "name": "t"}}')
AUTH = {"Authorization": "Bearer k-test"}


@pytest.fixture()
def client(monkeypatch):
    # 不配 key 时服务会一律 401(它宁可全拒也不匿名放行, 因为它持 service_role)。
    monkeypatch.setenv("DESKCORE_KEYS", KEYS)
    return fastapi_testclient.TestClient(A.app, raise_server_exceptions=False)


def _raises(exc):
    def _boom(name, args):
        raise exc
    return _boom


@pytest.mark.parametrize("exc, expected, why", [
    (core.ProjectNotFound("project not found: nope"), 404, "ID 打错/已删"),
    (PermissionError("project x 不属于当前调用者"), 403, "越权"),
    (RuntimeError("查重真的挂了"), 500, "真故障"),
])
def test_exception_maps_to_the_right_status(client, monkeypatch, exc, expected, why):
    monkeypatch.setattr(A, "_call_tool", _raises(exc))
    r = client.post("/tool/check_drafts", json={"project_id": "x"}, headers=AUTH)
    assert r.status_code == expected, f"{why}: 期望 {expected}, 实际 {r.status_code}"


def test_not_found_and_denied_are_not_the_same_status(client, monkeypatch):
    """两者不许塌成同一个码 —— 那就是 core 分两个异常之前的状态。"""
    monkeypatch.setattr(A, "_call_tool", _raises(core.ProjectNotFound("x")))
    a = client.post("/tool/check_drafts", json={}, headers=AUTH).status_code
    monkeypatch.setattr(A, "_call_tool", _raises(PermissionError("x")))
    b = client.post("/tool/check_drafts", json={}, headers=AUTH).status_code
    assert a != b, f"两者都是 {a} —— 分了异常却没分状态码"
    assert 400 <= a < 500 and 400 <= b < 500, (a, b)


# ══════════════════════════════════════════════════════════════════════
# live / ready 必须分开（跨库审计 2026-08-24 ROB-005）
# ══════════════════════════════════════════════════════════════════════

def _force_health(monkeypatch, ok: bool):
    """把健康计算按住, 只留下 ok 这一个变量。

    不去真的打库 —— 这几条测的是"状态码怎么跟着 ok 走", 不是"探测准不准"。
    """
    async def _fake():
        return {"ok": ok, "service": "deskcore", "config": {}}
    monkeypatch.setattr(A, "_collect_health", _fake)


def test_health_is_liveness_and_stays_200_even_when_degraded(monkeypatch, client):
    """``/health`` 是 liveness, 残废时也必须 200。

    它是 railway.json 的 healthcheckPath, 而 Railway 对健康检查失败的反应是
    **重启容器**。库瞬断重启一遍既治不好也救不回来, 只会变成重启风暴。
    这条断言的是**被禁止的形态**（跟着 ok 变码），不是当前写法。
    """
    _force_health(monkeypatch, ok=False)
    r = client.get("/health")
    assert r.status_code == 200, "liveness 跟着 ok 变码了 —— 会引发重启风暴"
    assert r.json()["ok"] is False, "200 不代表可以谎报 ok"


def test_ready_is_503_when_not_ok(monkeypatch, client):
    """``/ready`` 不 ready 就必须是 503, 否则编排摘不掉流量。"""
    _force_health(monkeypatch, ok=False)
    assert client.get("/ready").status_code == 503


def test_ready_is_200_when_ok(monkeypatch, client):
    _force_health(monkeypatch, ok=True)
    r = client.get("/ready")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_both_endpoints_report_the_same_ok(monkeypatch, client):
    """两个端点的 body 必须来自同一份计算 —— 各算一次迟早判据写岔。"""
    for ok in (True, False):
        _force_health(monkeypatch, ok=ok)
        assert client.get("/health").json()["ok"] is ok
        assert client.get("/ready").json()["ok"] is ok
