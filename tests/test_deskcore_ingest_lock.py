"""补录的跨进程互斥(migrations/009 的 ingest_locks + core.ingest_published)。

codex review #81 P1: ingest_published 的幂等靠"先读库里有没有指纹、再写"; 服务进程
(WorkBuddy 工具)和 CLI/cron(tv-sync)重叠时都过完那一读再各写一份, 进程内的锁管不到
对方。所以主锁是库里的一行(TTL、可接管), 进程内锁只是省轮询。
"""

from __future__ import annotations

import threading
import time

import pytest

from deskcore import core, store
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
PROJ = "aaaaaaaa-0000-0000-0000-000000000001"


class _LockTable:
    """把 009 那两个 RPC 的语义搬进假库: 一行/项目, 带 TTL, 同 holder 可重入。"""

    def __init__(self):
        self.rows: dict[str, tuple[str, float]] = {}
        self.calls: list[tuple[str, dict]] = []

    def lock(self, a):
        self.calls.append(("lock", a))
        pid, holder, ttl = a["_project_id"], a["_holder"], a["_ttl_seconds"]
        cur = self.rows.get(pid)
        if cur is None or cur[1] < time.monotonic() or cur[0] == holder:
            self.rows[pid] = (holder, time.monotonic() + ttl)
            return True
        return False

    def unlock(self, a):
        self.calls.append(("unlock", a))
        cur = self.rows.get(a["_project_id"])
        if cur and cur[0] == a["_holder"]:
            del self.rows[a["_project_id"]]
            return True
        return False


def _client(with_lock=True):
    c = FakeClient(rows={"projects": [
        {"id": PROJ, "name": "途鸽", "brand": "途鸽", "owner_id": ME,
         "calibration_notes": "", "tactics": "[]", "custom_roles": []}]})
    lt = _LockTable()
    if with_lock:
        c.rpc_impl = {"deskcore_ingest_lock": lt.lock, "deskcore_ingest_unlock": lt.unlock}
    c.lock_table = lt
    return c


def _rows(n):
    return [{"title": f"t{i}", "body": f"第 {i} 篇的正文各不相同, 长度都够二十个字以上。" * 3}
            for i in range(n)]


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    monkeypatch.setattr(core, "INGEST_LOCK_WAIT_SEC", 1.0)
    monkeypatch.setattr(core, "INGEST_LOCK_POLL_SEC", 0.02)


def test_lock_is_taken_and_released_around_a_real_ingest():
    c = _client()
    out = core.ingest_published(c, PROJ, _rows(2), user_id=ME, source="x")
    assert out["minted"] == 2
    kinds = [k for k, _ in c.lock_table.calls]
    assert kinds == ["lock", "unlock"] and c.lock_table.rows == {}
    holder = c.lock_table.calls[0][1]["_holder"]
    assert holder.startswith("x:") and c.lock_table.calls[1][1]["_holder"] == holder


def test_lock_is_released_even_when_the_ingest_blows_up(monkeypatch):
    c = _client()
    monkeypatch.setattr(store, "mint_draft_identity",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("库炸了")))
    with pytest.raises(RuntimeError):
        core.ingest_published(c, PROJ, _rows(1), user_id=ME, source="x")
    assert c.lock_table.rows == {}, "失败也要放锁, 否则别人要等 TTL"


def test_dry_run_never_touches_the_lock():
    c = _client()
    core.ingest_published(c, PROJ, _rows(2), user_id=ME, source="x", dry_run=True)
    assert c.lock_table.calls == []


def test_a_lock_held_by_another_process_makes_us_busy_not_duplicate():
    c = _client()
    c.lock_table.rows[PROJ] = ("someone-else", time.monotonic() + 600)
    with pytest.raises(core.IngestBusy, match="另一个写操作"):
        core.ingest_published(c, PROJ, _rows(1), user_id=ME, source="x")
    assert not c.rows.get("versions"), "拿不到锁一行都不能写"


def test_a_stale_lock_is_taken_over():
    c = _client()
    c.lock_table.rows[PROJ] = ("crashed", time.monotonic() - 1)     # 已过期
    out = core.ingest_published(c, PROJ, _rows(1), user_id=ME, source="x")
    assert out["minted"] == 1


def test_missing_lock_rpc_degrades_to_the_process_lock_with_a_warning(caplog):
    """迁移没跑: 不能把"没部署"当成"拿到"; 也不能因此拒绝补录 —— 降级 + 记 warning,
    doctor 会把 009 报成 missing。"""
    c = _client(with_lock=False)
    with caplog.at_level("WARNING"):
        out = core.ingest_published(c, PROJ, _rows(1), user_id=ME, source="x")
    assert out["minted"] == 1
    assert any("lock RPC 没部署" in r.getMessage() for r in caplog.records)


def test_two_processes_worth_of_overlap_write_one_copy(monkeypatch):
    """两个线程各自一把"进程内锁"(模拟两个进程), 只靠库锁串行: 后到的等前一个
    放锁, 再读到它写的指纹, 全部跳过。"""
    c = _client()
    real_write = store.write_fingerprints

    def _slow(sb, rows):
        time.sleep(0.15)
        return real_write(sb, rows)
    monkeypatch.setattr(store, "write_fingerprints", _slow)
    # 每次调用换一把进程内锁 → 进程内锁形同虚设, 只剩库锁在管
    monkeypatch.setattr(core, "_ingest_proc_lock", lambda pid: threading.Lock())
    rows = _rows(3)
    results = []
    ts = [threading.Thread(target=lambda: results.append(
              core.ingest_published(c, PROJ, rows, user_id=ME, source="x"))) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(c.rows["draft_fingerprints"]) == 3
    assert sorted(r["minted"] for r in results) == [0, 3]
