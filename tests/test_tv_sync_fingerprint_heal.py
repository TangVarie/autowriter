"""改稿时进程被杀, 旧稿从查重里消失 —— tv-sync 夜里补回来(外部评审 11-04 F05, 2026-11-04)。

score_drafts 改稿(replaces_version_id)先摘掉旧 item 的全部指纹、查重、再写新的; 普通异常有 _restore_stash 放回, 进程被杀
(SIGKILL / OOM, 这里用 BaseException 模拟: ``except Exception`` 接不住它, 和进程没了一样什么都不跑)就放不回了: item 的 best
还是旧版, 旧版一行指纹都没有。原来只有手动的 ``cli backfill`` / ``doctor --gap`` 能补。这里钉住: tv-sync 认出写作台自己的
定稿版本没有指纹行, 按 backfill 那一套补回来并报数; 正常改稿之后(best 是新版)不误报; 别的引擎的老稿不碰; 预演只报不写。
"""

from __future__ import annotations

import copy
import logging

import pytest

import config
from deskcore import core, fingerprint as fp, store
from tests.test_deskcore_commit_judge import BODY_A, BODY_A2, ME, PROJ, _client, _embed, _rpc_that_writes

TV = "TUGE_phase1"


class HardKill(BaseException):
    pass


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    monkeypatch.setattr(config, "JUDGE_URL", "")


def _joinable(c):
    """假库没有真 join: versions_for_linking / legacy_version_pages 要的 items → versions、items → batches 嵌套摆好。"""
    batches = {b["id"]: b for b in c.rows.get("batches", [])}
    for it in c.rows.get("items", []):
        it["versions"] = [v for v in c.rows.get("versions", []) if v.get("item_id") == it["id"]]
        b = batches.get(it.get("batch_id")) or {}
        it["batches"] = {"project_id": b.get("project_id"), "params": b.get("params")}
    c.rpc_impl["deskcore_tv_notes"] = lambda a: []
    return c


def _rows(c, vid):
    return [f for f in c.rows.get("draft_fingerprints", []) if f.get("version_id") == vid]


def _crashed(monkeypatch):
    """存一版, 再改稿; 摘掉旧指纹之后、写新指纹之前进程没了。回 (库停在那一刻的样子, 旧版 id)。"""
    c = _client()
    v1 = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A}], user_id=ME)["version_ids"][0]
    assert len(_rows(c, v1)) == 1
    _embed(c)
    frozen = {}

    def die():
        frozen["rows"] = copy.deepcopy(c.rows)
        raise HardKill("进程被杀")
    monkeypatch.setattr(core.dedup, "embeddings_available", die)
    with pytest.raises(HardKill):
        core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A2, "replaces_version_id": v1}], user_id=ME)
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    c.rows = frozen["rows"]
    c.rpc_impl["deskcore_commit_fingerprints"] = _rpc_that_writes(c)
    item = next(i for i in c.rows["items"] if i.get("best_version_id") == v1)
    assert item and not _rows(c, v1), "前提: best 还是旧版, 旧版一行指纹都没有"
    return _joinable(c), v1


def test_tv_sync_rebuilds_the_missing_fingerprint_and_warns(monkeypatch, caplog):
    c, v1 = _crashed(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=core.logger.name):
        out = core.tv_sync(c, TV)
    gap = out["fingerprint_gap"]
    assert (gap["missing"], gap["rebuilt"], gap["error"]) == (1, 1, None)
    assert gap["samples"] == [{"project_id": PROJ, "version_id": v1}]
    (row,) = _rows(c, v1)
    assert row["opening_hash"] == fp.opening_hash(BODY_A) and row["ngram_hashes"] == fp.ngram_hashes(BODY_A)
    assert any("没有指纹行" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)

    again = core.tv_sync(c, TV)
    assert again["fingerprint_gap"]["missing"] == 0 and len(_rows(c, v1)) == 1, "补过就不再补"


def test_dry_run_reports_the_gap_and_writes_nothing(monkeypatch):
    c, v1 = _crashed(monkeypatch)
    out = core.tv_sync(c, TV, dry_run=True)
    assert out["fingerprint_gap"]["missing"] == 1 and out["fingerprint_gap"]["rebuilt"] == 0
    assert not _rows(c, v1)


def test_a_completed_revision_is_not_a_gap():
    c = _client()
    v1 = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A}], user_id=ME)["version_ids"][0]
    _embed(c)
    out = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A2, "replaces_version_id": v1}], user_id=ME)
    v2 = out["version_ids"][0]
    assert not _rows(c, v1) and _rows(c, v2), "正常改稿: 旧版的行撤了, 新版有行"
    res = core.tv_sync(_joinable(c), TV)
    assert res["fingerprint_gap"]["missing"] == 0 and not _rows(c, v1), "best 是新版, 旧版没有行是对的"


def test_other_engines_are_left_to_the_manual_backfill(monkeypatch):
    c, v1 = _crashed(monkeypatch)
    for v in c.rows["versions"]:
        v["ai_engine"] = "manual"
    _joinable(c)
    out = core.tv_sync(c, TV)
    assert out["fingerprint_gap"]["missing"] == 0 and not _rows(c, v1)


def test_a_failure_is_reported_and_does_not_stop_the_sync(monkeypatch):
    c, v1 = _crashed(monkeypatch)
    monkeypatch.setattr(store, "write_fingerprints", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    out = core.tv_sync(c, TV)
    assert out["fingerprint_gap"]["missing"] == 1 and "db down" in out["fingerprint_gap"]["error"]


def test_a_revision_that_did_not_attach_to_the_item_is_not_backfilled(monkeypatch, caplog):
    # 复审 11-04: 新指纹入了库、新版也建了, 只是 best 没挪过去(commit_drafts 的 replace_error)。旧版没有行是故意的, 新的
    # 文字在同一个 item 的新版下 —— 补回旧稿的文字, 一篇帖的新旧两版就都在查重里了
    c = _client()
    v1 = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A}], user_id=ME)["version_ids"][0]
    _embed(c)

    def _fail(*a, **k):
        raise RuntimeError("best 挪不过去")
    monkeypatch.setattr(store, "point_item_at_version", _fail)
    out = core.commit_drafts(c, PROJ, [{"title": "秋招等回音", "body": BODY_A2, "replaces_version_id": v1}], user_id=ME)
    assert "replace_warning" in out
    monkeypatch.undo()
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    monkeypatch.setattr(config, "JUDGE_URL", "")
    item = next(i for i in c.rows["items"] if i.get("best_version_id") == v1)
    v2 = next(v["id"] for v in c.rows["versions"] if v.get("item_id") == item["id"] and v["id"] != v1)
    assert not _rows(c, v1) and _rows(c, v2), "前提: best 还是旧版、旧版没有行, 新版在同一个 item 下、有行"

    with caplog.at_level(logging.INFO, logger=core.logger.name):
        res = core.tv_sync(_joinable(c), TV)
    gap = res["fingerprint_gap"]
    assert not _rows(c, v1), "旧稿的文字不补回去"
    assert (gap["missing"], gap["rebuilt"], gap.get("skipped")) == (0, 0, 1)
    assert not any(r.levelno >= logging.WARNING and "没有指纹行" in r.getMessage() for r in caplog.records)
