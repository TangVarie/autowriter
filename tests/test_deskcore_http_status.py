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
