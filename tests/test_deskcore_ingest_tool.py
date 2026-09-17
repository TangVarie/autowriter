"""`ingest_published` 作为**写作台工具**(MCP/REST)—— 运营手里只有 WorkBuddy。

2026-09-17: 234 条已发未入库的稿子要补指纹, 起初做成了 CLI 的 ``ingest --xlsx``,
而运营没有终端。所以同一个 core 函数再挂成工具: 运营把飞书表粘进对话, 模型
分批调它。这里盯的是工具层那几件事: 注册了且带身份; 走过 commit 的行跳过;
一次太多要拒; 半途失败的两种情形给出的 ``note`` 方向相反(重传 vs 不许重传);
协议正文和工具说明都讲清了它跟 commit_drafts 的区别。
"""

from __future__ import annotations

import pytest

from deskcore import core
from deskcore import tools as T
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
PROJ = "aaaaaaaa-0000-0000-0000-000000000001"


@pytest.fixture
def fake(monkeypatch):
    c = FakeClient(rows={"projects": [
        {"id": PROJ, "name": "途鸽", "brand": "途鸽", "owner_id": ME,
         "calibration_notes": "", "tactics": "[]", "custom_roles": []}]})
    monkeypatch.setattr(core, "sb", lambda: c)
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    return c


def _rows(n, prefix="发过的"):
    return [{"title": f"{prefix}{i}", "body": f"第 {i} 篇的正文, 已经在小红书上了, 各不相同。" * 5}
            for i in range(n)]


def test_registered_as_a_tool_that_needs_the_caller_identity():
    assert "ingest_published" in T.TOOLS
    assert T.TOOLS["ingest_published"][1] is True, "COR-015: 新工具默认要身份"


def test_rows_that_already_went_through_commit_are_skipped(fake):
    drafts = _rows(3)
    drafts[0]["version_id"] = "v-已入库"
    drafts[1]["_source_autowriter_version_id"] = "v-也入库了"
    out = T.ingest_published(PROJ, drafts, _user_id=ME)
    assert out["received"] == 3
    assert out["skipped_already_committed"] == 2
    assert (out["minted"], out["fingerprinted"]) == (1, 1)
    assert len(fake.rows["draft_fingerprints"]) == 1
    assert "补完" in out["note"]


def test_too_many_in_one_call_is_refused_before_touching_the_db(fake):
    with pytest.raises(ValueError, match="最多"):
        T.ingest_published(PROJ, _rows(T.INGEST_MAX_PER_CALL + 1), _user_id=ME)
    assert "draft_fingerprints" not in fake.rows or not fake.rows["draft_fingerprints"]
    with pytest.raises(ValueError):
        T.ingest_published(PROJ, [], _user_id=ME)


def test_dry_run_counts_but_writes_nothing_and_says_so(fake):
    out = T.ingest_published(PROJ, _rows(4), dry_run=True, _user_id=ME)
    assert out["dry_run"] is True and out["to_write"] == 4 and out["minted"] == 0
    assert not fake.rows.get("draft_fingerprints") and not fake.rows.get("versions")
    assert "没动库" in out["note"] and "去掉 dry_run" in out["note"]


def test_repeat_call_is_idempotent(fake):
    first = T.ingest_published(PROJ, _rows(3), _user_id=ME)
    again = T.ingest_published(PROJ, _rows(3), _user_id=ME)
    assert first["minted"] == 3
    assert again["minted"] == 0 and again["skipped_already_fingerprinted"] == 3
    assert len(fake.rows["draft_fingerprints"]) == 3, "重复调不许翻倍"


def test_fingerprint_failure_says_do_not_resend(fake, monkeypatch):
    """身份建了、指纹没写: 重传会再建一份身份。note 必须是"不要重传"。"""
    def _boom(sb, rows):
        raise RuntimeError("PostgREST 502")
    monkeypatch.setattr(core.store, "write_fingerprints", _boom)
    out = T.ingest_published(PROJ, _rows(2), _user_id=ME)      # 不抛
    assert out["minted"] == 2 and out["fingerprinted"] == 0
    assert out["fingerprint_error"]
    assert "不要重传" in out["note"] and "backfill" in out["note"]


def test_identity_failure_says_resend_is_fine(fake, monkeypatch):
    monkeypatch.setattr(core.store, "mint_draft_identity",
                        lambda *a, **k: {"batch_id": None, "versions": {}, "error": "库抖了"})
    out = T.ingest_published(PROJ, _rows(2), _user_id=ME)
    assert out["minted"] == 0 and out["identity_error"]
    assert "重传即可" in out["note"] and "不要重传" not in out["note"]


def test_someone_elses_project_is_refused(fake):
    with pytest.raises(PermissionError):
        T.ingest_published(PROJ, _rows(1), _user_id="99999999-9999-9999-9999-999999999999")


# ── 协议与说明: 模型只看这两处 ────────────────────────────────────────────

def test_protocol_tells_the_model_when_this_is_not_commit_drafts():
    body, _ = T.protocol_text()
    assert "`ingest_published`" in body
    sec = body.split("`ingest_published`", 1)[1][:1200]
    assert "不过闸" in sec and "50" in sec and "dry_run" in sec
    assert "commit_drafts" in sec or "commit" in sec, "要讲清和 commit 的区别"
    # 请求→工具 那张表里也要有一行
    table_rows = [l for l in body.splitlines() if l.startswith("|") and "ingest_published" in l]
    assert table_rows and "已经发出去" in table_rows[0]
    # 写类工具报错清单里要有它
    assert "`ingest_published` / `record_rule`" in body


def test_tool_docstring_covers_the_operating_rules():
    doc = T.ingest_published.__doc__
    for needle in ("不过闸", "50", "dry_run", "重复调是安全的", "不要重传", "commit_drafts"):
        assert needle in doc, needle


def test_it_is_a_write_and_therefore_not_wrapped_in_safe():
    import inspect
    assert "_safe(" not in inspect.getsource(T.ingest_published)
