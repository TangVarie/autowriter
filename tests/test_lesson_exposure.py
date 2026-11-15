"""这一批稿子写之前看到了哪几张经验卡、哪几条规律(2026-11-15, 十三项第 3 项, migrations/022)。

钉的是:
  · 借卡那一刻服务端记下卡的 source_note_id 和规律的标识(馆员没给 id 就用规律文字的短哈希), 模型不用传回任何东西;
  · 存档时进新建的 batch 的 params.lesson_exposure(没借过卡 / 重启过 = null), 不进 batch_metrics(那里只有数);
  · 查重时进底稿那一行; 022 没跑(库里没有这一列)时去掉这一列再写一次, 行和 022 之前一模一样, 只警告一次,
    别的写入错误照旧算失败;
  · tv-sync 读底稿: 那一列读得到就带进转正的 batch, 读不到退回原来那几列;
  · doctor 认得 022; 读数只有 id 和计数。

所有稿子、卡、规律都是合成的示例。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

import librarian_client as lib
from deskcore import cli, core, exposurereport, store
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
PID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
BODY = ("周末骑了一百公里, 第六十公里的时候腿开始发抖, 补给站的香蕉早就吃完了。"
        "后来在路边小卖部买了两根能量胶, 撑到终点才发现袋子里还剩一根没开。") * 2
PATTERNS = [{"id": "ending_asks_reader", "label": "结尾问读者", "instruction": "结尾留一个读者能直接回答的问题。",
             "evidence": "示例"},
            {"label": "开头给数字", "instruction": "第一句带一个具体的数。", "evidence": "示例"}]


class _Librarian:
    def __init__(self, cards, patterns=()):
        self.cards, self.patterns = list(cards), list(patterns)

    def __call__(self, brief, *, status=None):
        if status is not None:
            status.update({"state": lib.BORROW_BORROWED, "count": len(self.cards), "elapsed_ms": 5, "detail": "",
                           "patterns": [] if brief.get("explore") else list(self.patterns)})
        return [{"source_note_id": c, "why_it_worked": "示例"} for c in self.cards]


def _client(**rows):
    c = FakeClient(rows={"projects": [{"id": PID, "name": "项目", "brand": "A", "owner_id": ME, "system_prompt": "",
                                       "calibration_notes": "", "tactics": "[]", "custom_roles": [],
                                       "default_params": "{}"}],
                         "memories": [], "user_calibration_notes": [], "items": [], "versions": [], "batches": [],
                         "draft_fingerprints": [], "draft_snapshots": [], "angle_ledger": [], "style_edits": [], **rows})
    c.rpc_impl["deskcore_commit_fingerprints"] = lambda args: [
        {"idx": i, "status": core.COMMIT_STATUS_INSERTED, "collided_with": None, "detail": None}
        for i in range(len(args.get("_rows") or []))]
    return c


@pytest.fixture(autouse=True)
def _plain(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    monkeypatch.setattr(core, "flywheel_arm", lambda pid, uid, **kw: "patterns")
    monkeypatch.setattr(core, "_borrowed_arms", {})
    monkeypatch.setattr(core, "_patterns_shown", {})


def _borrow(monkeypatch, cards, patterns=PATTERNS, *, client=None, user=ME):
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", _Librarian(cards, patterns))
    return core.borrow_lessons(client or _client(), PID, user_id=user)


# ── 借卡时记, 存档时进 batch ─────────────────────────────────────────────────────────────

def test_the_cards_and_patterns_handed_over_are_remembered_and_stored_on_the_batch(monkeypatch):
    c = _client()
    out = _borrow(monkeypatch, ["n1", "n2"], client=c)
    assert "lesson_exposure" not in out, "回包里没有: 模型不用知道, 也不用传回"

    got = core.lesson_exposure(PID, ME)
    assert got["cards"] == ["n1", "n2"] and got["borrows"] == 1
    assert got["patterns"][0] == "ending_asks_reader", "馆员给了 id 就用它"
    assert got["patterns"][1].startswith("h:") and len(got["patterns"][1]) == 14, "没给 id: 规律文字的短哈希"
    assert got["first_at"] == got["last_at"]
    assert got["latest"] == {"cards": 2, "patterns": 2}

    res = core.commit_drafts(c, PID, [{"title": "标题一", "body": BODY}], user_id=ME)
    params = c.rows["batches"][0]["params"]
    assert params["lesson_exposure"] == got
    assert "lesson_exposure" not in json.dumps(res, ensure_ascii=False), "存档的回包里也没有"
    meta = [r["meta"] for r in c.rows["batch_metrics"] if r["meta"].get("mode") == "deskcore_commit"][-1]
    assert "lesson_exposure" not in meta and meta["flywheel_lessons"]["shown"] == 2, "指标那一行照旧只有数"
    assert "n1" not in json.dumps(meta, ensure_ascii=False)


def test_several_borrows_are_a_union_with_the_latest_first_and_other_identities_are_separate(monkeypatch):
    t0 = datetime(2026, 11, 15, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(core, "_utcnow", lambda: t0)
    _borrow(monkeypatch, ["n1", "n2"], patterns=[])
    monkeypatch.setattr(core, "_utcnow", lambda: t0 + timedelta(hours=1))
    _borrow(monkeypatch, ["n3", "n1"], patterns=PATTERNS[:1])
    got = core.lesson_exposure(PID, ME)
    assert got["cards"] == ["n3", "n1", "n2"] and got["patterns"] == ["ending_asks_reader"]
    assert got["borrows"] == 2 and got["first_at"] < got["last_at"]
    assert got["latest"] == {"cards": 2, "patterns": 1}, "前 2 张、前 1 条正好是最近一次借的"
    assert got["cards"][:got["latest"]["cards"]] == ["n3", "n1"]
    assert core.lesson_exposure(PID, "22222222-2222-2222-2222-222222222222") is None
    assert core.lesson_exposure("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", ME) is None

    monkeypatch.setattr(core, "_utcnow", lambda: t0 + timedelta(hours=12, minutes=30))
    assert core.lesson_exposure(PID, ME)["cards"] == ["n3", "n1"], "超过 12 小时的那一次不算"
    monkeypatch.setattr(core, "_utcnow", lambda: t0 + timedelta(hours=14))
    assert core.lesson_exposure(PID, ME) is None


def test_a_borrow_that_brought_nothing_is_an_empty_list_and_no_borrow_is_null(monkeypatch):
    c = _client()
    core.commit_drafts(c, PID, [{"title": "标题一", "body": BODY}], user_id=ME)
    params = c.rows["batches"][0]["params"]
    assert "lesson_exposure" in params and params["lesson_exposure"] is None, "没借过卡 / 重启过: null = 不知道"

    _borrow(monkeypatch, [], patterns=[])
    assert core.lesson_exposure(PID, ME)["cards"] == [] and core.lesson_exposure(PID, ME)["borrows"] == 1


def test_the_lists_are_capped_and_say_so(monkeypatch):
    for i in range(12):
        _borrow(monkeypatch, [f"n{i}-{k}" for k in range(5)], patterns=[])
    got = core.lesson_exposure(PID, ME)
    assert len(got["cards"]) == core._EXPOSURE_MAX_CARDS and got["truncated"] is True and got["borrows"] == 12


def test_recording_never_breaks_a_borrow(monkeypatch):
    monkeypatch.setattr(core, "_pattern_key", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    out = _borrow(monkeypatch, ["n1"])
    assert out["count"] == 1 and core.lesson_exposure(PID, ME) is None


# ── 查重时进底稿; 022 没跑 ────────────────────────────────────────────────────────────────

def test_check_drafts_writes_the_exposure_on_every_snapshot_row(monkeypatch):
    c = _client()
    _borrow(monkeypatch, ["n1"], client=c)
    core.check_drafts(c, PID, [{"title": "骑了一百公里", "body": BODY}, {"title": "另一篇", "body": BODY[::-1]}],
                      user_id=ME, record_snapshot=True)
    rows = c.rows["draft_snapshots"]
    assert len(rows) == 2 and all(r["lesson_exposure"]["cards"] == ["n1"] for r in rows)


def test_without_a_borrow_the_snapshot_row_is_exactly_as_before(monkeypatch):
    c = _client()
    core.check_drafts(c, PID, [{"title": "骑了一百公里", "body": BODY}], user_id=ME, record_snapshot=True)
    assert "lesson_exposure" not in c.rows["draft_snapshots"][0], "不知道就不带这一列: 022 没跑也不会多一次失败的写入"


def _no_022(**kw):
    c = _client(**kw)
    c.missing_columns["draft_snapshots"] = {"lesson_exposure"}
    return c


def test_before_022_the_snapshot_is_written_as_before_and_warned_once(monkeypatch, caplog):
    c = _no_022()
    _borrow(monkeypatch, ["n1"], client=c)
    with caplog.at_level(logging.WARNING, logger="deskcore.store"):
        core.check_drafts(c, PID, [{"title": "骑了一百公里", "body": BODY}], user_id=ME, record_snapshot=True)
        core.check_drafts(c, PID, [{"title": "另一篇", "body": BODY[::-1]}], user_id=ME, record_snapshot=True)
    rows = c.rows["draft_snapshots"]
    assert len(rows) == 2 and all("lesson_exposure" not in r for r in rows)
    assert {"project_id", "user_id", "check_id", "title", "body", "angle_key", "text_fp", "status", "flywheel_arm",
            "flywheel_arm_source"} <= set(rows[0])
    assert caplog.text.count("022_lesson_exposure.sql") == 1, "只警告一次"
    writes = [x for x in c.calls if x["table"] == "draft_snapshots" and x["op"] == "insert"]
    assert len(writes) == 3, "第一次带着那一列失败、去掉再写; 之后十分钟内直接不带"
    # 存档不受 022 管: batches.params 照样记
    core.commit_drafts(c, PID, [{"title": "标题一", "body": BODY}], user_id=ME)
    assert c.rows["batches"][0]["params"]["lesson_exposure"]["cards"] == ["n1"]


def test_the_missing_column_is_probed_again_after_the_retry_window(monkeypatch):
    c = _no_022()
    _borrow(monkeypatch, ["n1"], client=c)
    core.check_drafts(c, PID, [{"title": "骑了一百公里", "body": BODY}], user_id=ME, record_snapshot=True)
    c.missing_columns.clear()                           # 负责人跑了 022
    store._snapshot_exposure_state["missing_until"] = 0.0   # 十分钟过去了
    core.check_drafts(c, PID, [{"title": "另一篇", "body": BODY[::-1]}], user_id=ME, record_snapshot=True)
    assert c.rows["draft_snapshots"][-1]["lesson_exposure"]["cards"] == ["n1"], "不用重新部署就用上了"


@pytest.mark.parametrize("message, missing", [
    ("PGRST204 Could not find the 'lesson_exposure' column of 'draft_snapshots' in the schema cache", True),
    ('42703 column draft_snapshots.lesson_exposure does not exist', True),
    ('column "lesson_exposure" of relation "draft_snapshots" does not exist', True),
    ("PGRST204 Could not find the 'flywheel_arm' column of 'draft_snapshots' in the schema cache", False),
    ("PGRST205 Could not find the table 'autowriter.draft_snapshots' in the schema cache", False),
    ("42501 permission denied for table draft_snapshots", False),
    ("connection reset while writing lesson_exposure", False),
])
def test_only_the_named_column_counts_as_022_missing(message, missing):
    assert store.snapshot_exposure_column_missing(RuntimeError(message)) is missing


def test_any_other_write_error_is_still_a_failed_snapshot(monkeypatch, caplog):
    c = _client()
    _borrow(monkeypatch, ["n1"], client=c)
    real = c._execute

    def boom(q):
        if q.table_name == "draft_snapshots" and q.op == "insert":
            raise RuntimeError("42501 permission denied for table draft_snapshots")
        return real(q)
    monkeypatch.setattr(c, "_execute", boom)
    out = core.check_drafts(c, PID, [{"title": "骑了一百公里", "body": BODY}], user_id=ME, record_snapshot=True)
    assert out["summary"]["total"] == 1 and "记底稿失败" in caplog.text
    assert store._snapshot_exposure_state["warned"] is False, "不是缺列: 不当成 022 没跑"


# ── tv-sync 读底稿, 转正的 batch ───────────────────────────────────────────────────────────

def _snap_row(sid, exposure="absent", **extra):
    row = {"id": sid, "project_id": PID, "user_id": ME, "check_id": "c1", "title": "标题", "body": BODY,
           "angle_key": None, "text_fp": "fp-" + sid, "status": "pass", "flywheel_arm": "patterns",
           "flywheel_arm_source": "borrow", "checked_at": datetime.now(timezone.utc).isoformat(),
           "version_id": None, "promoted_at": None, "note_id": None, **extra}
    if exposure != "absent":
        row["lesson_exposure"] = exposure
    return row


def test_pending_snapshots_read_the_column_when_it_is_there_and_fall_back_when_it_is_not(caplog):
    exp = {"cards": ["n1"], "patterns": [], "borrows": 1}
    c = _client(draft_snapshots=[_snap_row("s1", exp)])
    rows = store.pending_draft_snapshots(c, [PID], since=store.iso_ago(5))
    assert rows[0]["lesson_exposure"] == exp
    assert "lesson_exposure" in c.calls[-1]["cols"] or any("lesson_exposure" in x["cols"] for x in c.calls)

    c2 = _no_022(draft_snapshots=[_snap_row("s1")])
    with caplog.at_level(logging.WARNING, logger="deskcore.store"):
        rows = store.pending_draft_snapshots(c2, [PID], since=store.iso_ago(5))
    assert [r["id"] for r in rows] == ["s1"] and "lesson_exposure" not in rows[0]
    assert "022_lesson_exposure.sql" in caplog.text
    c2.calls.clear()
    store.pending_draft_snapshots(c2, [PID], since=store.iso_ago(5))
    assert all("lesson_exposure" not in x["cols"] for x in c2.calls), "十分钟内不再带着那一列试"


def test_a_missing_018_table_still_maps_to_none():
    c = _client()

    def boom(q):
        raise RuntimeError("PGRST205 Could not find the table 'autowriter.draft_snapshots' in the schema cache")
    c._execute = boom
    assert store.pending_draft_snapshots(c, [PID], since=store.iso_ago(5)) is None


def test_snapshot_groups_carry_the_first_known_exposure_into_the_promoted_batch(monkeypatch):
    exp = {"cards": ["n1"], "patterns": ["ending_asks_reader"], "borrows": 1}
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    groups = core._snapshot_groups([_snap_row("s1", None, checked_at=old, text_fp="same"),
                                    _snap_row("s2", exp, text_fp="same")])
    assert len(groups) == 1
    g = next(iter(groups.values()))
    assert g["lesson_exposure"] == exp, "同一篇查了两次: 取最早一次有记录的"
    assert "lesson_exposure" not in next(iter(core._snapshot_groups([_snap_row("s3")]).values())), \
        "那一列没读到(022 没跑)就没有这个键"

    seen = {}

    def fake_mint(client, project_id, user_id, tactic, entries, *, batch_params=None):
        seen["params"] = batch_params
        return {"batch_id": "b-new", "versions": {e["version_id"]: "i-new" for e in entries}, "error": None}
    monkeypatch.setattr(store, "mint_draft_identity", fake_mint)
    monkeypatch.setattr(store, "snapshot_states", lambda *a, **k: {})
    monkeypatch.setattr(store, "items_for_versions", lambda *a, **k: {})
    monkeypatch.setattr(store, "fingerprint_keys", lambda *a, **k: {})
    monkeypatch.setattr(store, "write_fingerprints", lambda *a, **k: 1)
    monkeypatch.setattr(store, "angle_consumed_by", lambda *a, **k: set())
    monkeypatch.setattr(store, "mark_snapshots_promoted", lambda *a, **k: 1)

    import contextlib
    monkeypatch.setattr(core, "_project_write_lock", lambda *a, **k: contextlib.nullcontext())
    g["writer"] = ME
    core._promote_snapshot_groups(_client(), PID, [g], owner=ME, holder="t")
    assert seen["params"]["via"] == "snapshot" and seen["params"]["lesson_exposure"] == exp

    bare = next(iter(core._snapshot_groups([_snap_row("s3")]).values()))
    core._promote_snapshot_groups(_client(), PID, [bare], owner=ME, holder="t")
    assert "lesson_exposure" not in seen["params"]

    unknown = next(iter(core._snapshot_groups([_snap_row("s4", None)]).values()))
    core._promote_snapshot_groups(_client(), PID, [unknown], owner=ME, holder="t")
    assert seen["params"]["lesson_exposure"] is None, "那一列在、那次查重时不知道: null"


# ── doctor ─────────────────────────────────────────────────────────────────────────────

def test_doctor_reports_022_missing_when_the_column_is_absent():
    from tests.test_migration_doctor import _TABLES
    sb = FakeClient(rows=dict(_TABLES), missing_columns={"draft_snapshots": {"lesson_exposure"}})
    report = core.migration_state(sb)
    checks = {c["probe"]: c for c in report["checks"]}
    assert checks["draft_snapshots 的 lesson_exposure 列"]["state"] == "missing"
    assert core.MIGRATION_LESSON_EXPOSURE in report["missing"]
    assert "可降级" in checks["draft_snapshots 的 lesson_exposure 列"]["impact"]


# ── 读数 ───────────────────────────────────────────────────────────────────────────────

def _batches():
    return [{"id": "b1", "exposure": {"cards": ["n1", "n2"], "patterns": ["p1"]}},
            {"id": "b2", "exposure": {"cards": ["n1"], "patterns": []}},
            {"id": "b3", "exposure": {"cards": [], "patterns": []}},
            {"id": "b4", "exposure": None},
            {"id": "b5", "exposure": "absent"}]


def test_report_counts_batches_posts_and_hits_per_card():
    versions = {"b1": ["v1", "v2"], "b2": ["v3"], "b3": ["v4"]}
    adoption = {"by_version": {"v1": {"note_id": "t1"}, "v2": {"note_id": "t2"}, "v4": {"note_id": "t4"}}}
    notes = {"t1": {"tier": "爆"}, "t2": {"tier": "趴"}, "t4": {"tier": "大爆"}}
    rep = exposurereport.report(_batches(), versions=versions, adoption=adoption, notes=notes, days=90)
    assert rep["batches"] == {"total": 5, "known": 3, "unknown": 1, "before_feature": 1, "no_cards": 1}
    cards = {r["id"]: r for r in rep["cards"]}
    assert cards["n1"] == {"id": "n1", "batches": 2, "batches_published": 1, "posts": 2, "hits": 1}
    assert cards["n2"] == {"id": "n2", "batches": 1, "batches_published": 1, "posts": 2, "hits": 1}
    assert rep["patterns"] == [{"id": "p1", "batches": 1, "batches_published": 1, "posts": 2, "hits": 1}]
    assert rep["no_cards"] == {"batches": 1, "batches_published": 1, "posts": 1, "hits": 1}
    assert [r["id"] for r in rep["cards"]] == ["n1", "n2"], "看到的批次多的在前"
    text = exposurereport.format_report(rep)
    assert "是曝光, 不是起作用的证明" in text and "n1" in text


def test_report_marks_unreadable_layers_as_unknown_not_zero():
    rep = exposurereport.report(_batches(), versions={"b1": ["v1"]}, adoption=None, notes={}, days=90)
    assert rep["cards"][0]["batches"] == 2 and rep["cards"][0]["posts"] is None and rep["cards"][0]["hits"] is None
    rep = exposurereport.report(_batches(), versions={"b1": ["v1"]}, adoption={"by_version": {"v1": {"note_id": "t1"}}},
                                notes=None, days=90)
    n1 = next(r for r in rep["cards"] if r["id"] == "n1")
    assert n1["posts"] == 1 and n1["hits"] is None
    assert "读不到" in exposurereport.format_report(rep)


def test_cli_reads_only_and_prints_counts(capsys):
    now = datetime.now(timezone.utc).isoformat()
    exp = {"cards": ["n1"], "patterns": ["ending_asks_reader"], "borrows": 1}
    c = FakeClient(rows={
        "batches": [{"id": "b1", "project_id": PID, "created_at": now,
                     "params": {"source": "deskcore", "lesson_exposure": exp}},
                    {"id": "b2", "project_id": PID, "created_at": now,
                     "params": {"source": "deskcore", "lesson_exposure": None}},
                    {"id": "b3", "project_id": PID, "created_at": now, "params": {"source": "deskcore"}},
                    {"id": "b4", "project_id": PID, "created_at": now,
                     "params": {"source": "ingest", "lesson_exposure": exp}}],
        "items": [{"id": "i1", "batch_id": "b1"}],
        "versions": [{"id": "v1", "item_id": "i1", "title": "不该被取", "body": "不该被取"}],
        "tv_note_links": [{"note_id": "t1", "version_id": "v1", "match_kind": "body_exact"}],
        "tv_edit_diffs": [],
        "truth_vault.notes": [{"note_id": "t1", "tier": "爆", "publish_time": now, "comments_count": 30,
                               "raw_content": "不该被取"}]})

    class Args:
        days, project, json = 90, None, True
    assert cli._lesson_exposure(c, Args) == 0
    out = capsys.readouterr().out
    rep = json.loads(out[out.index("{"):])
    assert rep["batches"] == {"total": 3, "known": 1, "unknown": 1, "before_feature": 1, "no_cards": 0}, "补录副本不算"
    assert rep["cards"] == [{"id": "n1", "batches": 1, "batches_published": 1, "posts": 1, "hits": 1}]
    assert not [x for x in c.calls if x["op"] != "select"], "只读"
    for call in c.calls:
        if call["table"] in ("versions", "truth_vault.notes"):
            assert "body" not in call["cols"] and "raw_content" not in call["cols"]
    assert "不该被取" not in out

    Args.json = False
    assert cli._lesson_exposure(c, Args) == 0
    assert "经验卡曝光" in capsys.readouterr().out


def test_cli_says_so_when_batches_cannot_be_read(monkeypatch, capsys):
    monkeypatch.setattr(store, "report_exposure_batches", lambda *a, **k: None)

    class Args:
        days, project, json = 90, None, False
    assert cli._lesson_exposure(FakeClient(rows={}), Args) == 1
    assert "什么都没算" in capsys.readouterr().out
