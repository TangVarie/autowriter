"""W-09: /health 报各种 key 传法的请求数 —— 只报传法, 不报 key。

?key= 会进访问日志和代理日志; 要决定什么时候不再收它, 得先知道还有多少
客户端在用。/health 不鉴权, 所以这里同时钉住: 计数里没有 key 本身。
"""

from __future__ import annotations

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

import deskcore.app as A                      # noqa: E402

KEY = "k-transport-secret"
KEYS = ('{"%s": {"user_id": "22222222-2222-2222-2222-222222222222", "name": "t"}}' % KEY)


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("DESKCORE_KEYS", KEYS)
    monkeypatch.setattr(A, "_KEY_TRANSPORT", A.collections.Counter())
    return fastapi_testclient.TestClient(A.app, raise_server_exceptions=False)


def test_each_transport_is_counted_once_per_request(client):
    client.get("/tools", headers={"X-Deskcore-Key": KEY})
    client.get("/tools", headers={"Authorization": f"Bearer {KEY}"})
    client.get("/tools", headers={"Authorization": f"Bearer {KEY}"})
    client.get(f"/tools?key={KEY}")
    client.get("/tools")                                   # 没带 key, 401 也算一次 none
    assert A.key_transport_counts() == {"header": 1, "bearer": 2, "query": 1, "none": 1}


def test_header_wins_over_query_and_counts_as_header(client):
    client.get(f"/tools?key={KEY}", headers={"X-Deskcore-Key": KEY})
    assert A.key_transport_counts() == {"header": 1, "bearer": 0, "query": 0, "none": 0}


def test_health_reports_counts_without_the_key(client, monkeypatch):
    class _Dead:                                           # 不连真库; 这里只看 key_transport
        def table(self, *_a, **_k):
            raise RuntimeError("no db in this test")
    monkeypatch.setattr(A, "_health_probe_client", lambda: _Dead())
    client.get(f"/tools?key={KEY}")
    r = client.get("/health")
    assert r.json()["key_transport"]["query"] == 1
    assert KEY not in r.text
    # /health 自己不鉴权, 不该被算进 none
    assert client.get("/health").json()["key_transport"]["none"] == 0
