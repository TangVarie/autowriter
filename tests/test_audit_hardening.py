"""Regression tests for autowriter audit hardening fixes."""

from __future__ import annotations

import asyncio
import json

import pytest


def test_librarian_configured_requires_url_and_key(monkeypatch):
    import config
    import librarian_client

    monkeypatch.setattr(config, "LIBRARIAN_URL", "https://librarian.example.com")
    monkeypatch.setattr(config, "LIBRARIAN_API_KEY", "")
    assert librarian_client.configured() is False

    monkeypatch.setattr(config, "LIBRARIAN_URL", "")
    monkeypatch.setattr(config, "LIBRARIAN_API_KEY", "secret-key")
    assert librarian_client.configured() is False

    monkeypatch.setattr(config, "LIBRARIAN_URL", "https://librarian.example.com")
    monkeypatch.setattr(config, "LIBRARIAN_API_KEY", "secret-key")
    assert librarian_client.configured() is True


def test_librarian_post_uses_connect_and_read_timeout_tuple(monkeypatch):
    import config
    import librarian_client

    seen = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"selected": [], "patterns": []}

    def _fake_post(url, *, json, headers, timeout):
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(config, "LIBRARIAN_URL", "https://librarian.example.com")
    monkeypatch.setattr(config, "LIBRARIAN_API_KEY", "secret-key")
    monkeypatch.setattr(config, "LIBRARIAN_TIMEOUT_SEC", 10.0)
    monkeypatch.setattr(librarian_client.requests, "post", _fake_post)

    st = {}
    out = librarian_client.fetch_flywheel_lessons({"project_id": "p1"}, status=st)
    assert out == [] and st["state"] == librarian_client.BORROW_EMPTY
    assert seen["timeout"] == (3.0, 10.0)


def test_logger_masks_deskcore_secrets(monkeypatch):
    import logger_utils

    monkeypatch.setattr(logger_utils, "_VALUE_SECRETS", None)
    monkeypatch.setenv("DESKCORE_API_KEY", "dc-single-secret-99999")
    monkeypatch.setenv("DESKCORE_VIEW_SECRET", "dc-view-secret-88888")
    monkeypatch.setenv(
        "DESKCORE_KEYS",
        json.dumps({"dc-multi-secret-77777": {"user_id": "u1", "name": "alice"}}),
    )
    msg = (
        "k1=dc-single-secret-99999 k2=dc-view-secret-88888 "
        "k3=dc-multi-secret-77777"
    )
    masked = logger_utils.mask_secrets(msg)
    assert "dc-single-secret-99999" not in masked
    assert "dc-view-secret-88888" not in masked
    assert "dc-multi-secret-77777" not in masked


def test_feature_answers_compares_mixed_timezones_chronologically(monkeypatch):
    from deskcore import store

    # 2026-11-10T09:00:00+08:00 == 01:00Z (earlier)
    # 2026-11-10T02:00:00Z      == 02:00Z (later, even though string '02:00:00Z' < '09:00:00+08:00')
    rows = [
        {
            "subject_id": "v1",
            "question_id": "q1",
            "question_version": 1,
            "answer": "否",
            "extractor": "jev:1.0",
            "extracted_at": "2026-11-10T09:00:00+08:00",
        },
        {
            "subject_id": "v1",
            "question_id": "q1",
            "question_version": 1,
            "answer": "是",
            "extractor": "jev:1.0",
            "extracted_at": "2026-11-10T02:00:00Z",
        },
    ]
    monkeypatch.setattr(store, "_paged", lambda _fn: rows)
    out = store._feature_answers(
        None,
        ["v1"],
        {"q1": (1, frozenset({"是", "否"}))},
        "v0.1",
    )
    assert out == {"v1": {"q1": "是"}}


def test_vocab_vendor_checksum_ok():
    from deskcore import vocab

    ok, detail = vocab.vendor_checksum_ok()
    assert ok is True and isinstance(detail, str) and len(detail) > 0


def test_mcp_allowed_origins_derives_scheme_qualified_origins():
    pytest.importorskip("fastapi")
    from deskcore import app as A

    # Default wildcard CORS -> derive https:// and http:// from allowed hosts
    assert A._mcp_allowed_origins(["desk.example.com", "localhost:*"], ["*"]) == [
        "https://desk.example.com",
        "http://desk.example.com",
        "https://localhost:*",
        "http://localhost:*",
    ]
    # Explicit DESKCORE_ALLOWED_ORIGINS -> honor explicit origins directly
    assert A._mcp_allowed_origins(
        ["desk.example.com"], ["https://app.example.com"]
    ) == ["https://app.example.com"]


def test_health_librarian_configured_checks_both_url_and_key(monkeypatch):
    pytest.importorskip("fastapi")
    import config
    from deskcore import app as A

    class _FakeTable:
        def select(self, *_a, **_k):
            return self

        def limit(self, *_a, **_k):
            return self

        def execute(self):
            return None

    class _FakeSB:
        def table(self, _name):
            return _FakeTable()

    monkeypatch.setattr(config, "LIBRARIAN_URL", "https://librarian.example.com")
    monkeypatch.setattr(config, "LIBRARIAN_API_KEY", "")
    monkeypatch.setattr(A, "_health_probe_client", lambda: _FakeSB())
    monkeypatch.setattr(A, "_leak_cached", lambda: {"ok": True})

    health = asyncio.run(A._collect_health())
    assert health["config"]["librarian"]["configured"] is False

    monkeypatch.setattr(config, "LIBRARIAN_API_KEY", "secret-key")
    health2 = asyncio.run(A._collect_health())
    assert health2["config"]["librarian"]["configured"] is True
