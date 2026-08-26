"""会话同步报的数必须是**真的写进去了几条**（跨库审计 2026-08-24 ROB-015）。

``db.append_session_messages`` 内部是 read-max-then-insert + 唯一约束重试。
重试耗尽时它**返回 0**（并自己埋了一行 telemetry），但调用方原来把返回值
整个丢掉、转头 ``return len(new_msgs) // 2`` —— 报的是"我打算写几条", 不是
"实际写进去几条"。

这个假成功比"写失败"更糟: 会话历史是避重用的**上下文**, 少了几轮没有任何
地方会报错, 表现只是后面生成的稿子开始跟历史撞车 —— 而排查的人看到的
telemetry 是"同步成功 N 对"。

所以这里盯的是**调用方有没有采信被调用方的回执**, 而不是"能不能写成功"。
"""

from __future__ import annotations

import pytest

import generation_service as gs


SESSION = "sess-1"
PROJECT = "proj-1"


class _FakeDB:
    """只实现 _sync_approved_to_session 用到的那几个。

    ``append_returns`` 就是这条用例的全部变量: 让 append 回一个与入参不符的
    数字, 看调用方是照抄意图还是采信回执。
    """

    def __init__(self, *, approved: int, append_returns):
        self._approved = approved
        self._append_returns = append_returns
        self.appended: list[dict] | None = None

    def get_session_committed_item_ids(self, client, session_id):
        return set()

    def list_approved_versions_for_sync(self, client, project_id, limit=None):
        return [{"item_id": f"item-{i}", "batch_id": "b1", "title": f"T{i}",
                 "content": f"正文{i}"} for i in range(self._approved)]

    def append_session_messages(self, client, session_id, messages,
                                batch_id=None):
        self.appended = messages
        return self._append_returns


@pytest.fixture()
def patched(monkeypatch):
    def _apply(db):
        monkeypatch.setattr(gs, "db", db)
        monkeypatch.setattr(gs, "_format_version_for_session",
                            lambda v: v.get("content") or "")
        return db
    return _apply


def test_reports_what_was_actually_written(patched):
    """全部写成功 → 报实际对数。"""
    db = patched(_FakeDB(approved=3, append_returns=6))   # 3 对 = 6 条
    n = gs._sync_approved_to_session(object(), SESSION, PROJECT)
    assert n == 3, n


def test_a_partial_write_is_not_reported_as_full(patched):
    """只写进去一部分 → **不许**报成全部。

    这是原来那行 ``return len(new_msgs) // 2`` 的正面反例。
    """
    db = patched(_FakeDB(approved=3, append_returns=2))   # 只落了 1 对
    n = gs._sync_approved_to_session(object(), SESSION, PROJECT)
    assert n == 1, f"打算写 3 对、实际只落了 1 对, 却报了 {n}"


def test_cas_exhausted_is_not_reported_as_success(patched):
    """CAS 重试耗尽(append 返回 0) → 必须报 0。

    append_session_messages 在这条路径上不抛异常, 只返回 0 —— 所以
    ``except`` 兜不住它, 只能靠调用方看返回值。
    """
    db = patched(_FakeDB(approved=4, append_returns=0))
    n = gs._sync_approved_to_session(object(), SESSION, PROJECT)
    assert n == 0, f"一条都没写进去, 却报了 {n} 对"


def test_nothing_to_do_is_zero_not_an_error(patched):
    """没有新内容要同步是**正常**情况, 不该走进失败分支。"""
    db = patched(_FakeDB(approved=0, append_returns=0))
    assert gs._sync_approved_to_session(object(), SESSION, PROJECT) == 0
    assert db.appended is None, "没东西可写却还是调了 append"
