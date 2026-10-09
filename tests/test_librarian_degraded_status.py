"""馆员回 200 + selected=[] + status=degraded/error 时, 借阅结局是 error 不是 empty
(TV 2026-10-08 审计 B-04)。

馆员的契约(TV-06)在响应里带 status: ok / no_match / degraded / error。前两个是结论,
后两个是故障 —— 配错的 FLYWHEEL_LIBRARIAN_MODEL、中转站断了、cache_control 被拒, 馆员
都回 200 + []。以前客户端只看 selected, 这些全变成 "empty": 写手听到"这个项目没有可借
的经验", TV 的通道 2 交通灯(降级不写缓存行)则把锅甩给 autowriter。

这里钉的【被禁止的形态】: degraded / error 被记成 empty。
"""

from __future__ import annotations

import pytest

import config
import librarian_client as lib


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(config, "LIBRARIAN_URL", "https://example.invalid")
    monkeypatch.setattr(config, "LIBRARIAN_API_KEY", "k")


def _borrow_with(monkeypatch, payload):
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(payload))
    st: dict = {}
    got = lib.fetch_flywheel_lessons({"project_id": "p", "consumer": "deskcore"}, status=st)
    return got, st


@pytest.mark.parametrize("tv_status", ["degraded", "error"])
def test_degraded_or_error_from_the_librarian_is_an_error_not_empty(monkeypatch, tv_status):
    got, st = _borrow_with(monkeypatch, {"selected": [], "status": tv_status})
    assert got == [], "仍然 fail-open"
    assert st["state"] == lib.BORROW_ERROR, f"status={tv_status} 被记成了 {st['state']!r}"
    assert tv_status in st["detail"] and "不是这次没匹配上" in st["detail"]


def test_no_match_is_still_a_normal_empty(monkeypatch):
    got, st = _borrow_with(monkeypatch, {"selected": [], "status": "no_match"})
    assert got == [] and st["state"] == lib.BORROW_EMPTY


def test_old_contract_without_status_is_unchanged(monkeypatch):
    """老馆员 / 没带 status 的响应: 行为与改动前完全一样。"""
    got, st = _borrow_with(monkeypatch, {"selected": []})
    assert got == [] and st["state"] == lib.BORROW_EMPTY
    got, st = _borrow_with(monkeypatch, {"selected": [{"source_note_id": "n1"}], "status": "ok"})
    assert len(got) == 1 and st["state"] == lib.BORROW_BORROWED
