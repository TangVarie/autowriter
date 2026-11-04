"""/health 要当场看得见被截短或占位的 DESKCORE_KEYS 条目(2026-11-04 写手连不上那次)。"""

import json

from deskcore import identity

UID = "00000000-0000-4000-8000-000000000001"


def _set(monkeypatch, keys: dict) -> None:
    monkeypatch.delenv("DESKCORE_API_KEY", raising=False)
    monkeypatch.setenv("DESKCORE_KEYS", json.dumps(keys, ensure_ascii=False))


def test_full_keys_are_ok(monkeypatch):
    _set(monkeypatch, {"k-a-0123456789abcdefABCDEF": {"user_id": UID, "name": "A"},
                       "k-b-0123456789abcdefABCDEF": {"user_id": UID, "name": "B"}})
    ok, note = identity.auth_health()
    assert ok, note
    assert note == "2 key(s)"


def test_elided_keys_turn_health_red_by_position_only(monkeypatch):
    _set(monkeypatch, {"k-ziao-…": {"user_id": UID, "name": "Ziao"},
                       "k-b-...": {"user_id": UID, "name": "B"},
                       "k-c-0123456789abcdefABCDEF": {"user_id": UID, "name": "C"},
                       "k-d with space": {"user_id": UID, "name": "D"}})
    ok, note = identity.auth_health()
    assert not ok
    assert "[1, 2, 4]" in note
    # /health 不带鉴权: 不回显 key 本身, 也不回显名字
    for leaked in ("k-ziao", "k-b-", "Ziao", "k-d with"):
        assert leaked not in note


def test_elided_key_never_authenticates(monkeypatch):
    _set(monkeypatch, {"k-ziao-…": {"user_id": UID, "name": "Ziao"}})
    try:
        identity.resolve("k-ziao-fullvalue0123456789")
    except identity.AuthError:
        pass
    else:  # pragma: no cover
        raise AssertionError("a full key must not match an elided entry")
