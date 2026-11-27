"""2026-11-26: 底稿不只在查重通过的那一刻记。

生产库(11-26 只读查): 11-09 以来四个活跃项目 255 篇对不上的帖, 和写作台任何文字的四字串重合都不到 15%(平均 2-5%),
对上的 84-96%, 中间没有。写手在对话里一轮轮改、仿写、去 AI 味, 最后那一轮贴进飞书, 从没到过服务端; 11-18..11-23
check_drafts 57014 超时 22 次, 那几批稿子也一行没留(底稿只在判定算完之后才记)。这里钉的是
  · (A) 查重本身抛了: 先记底稿(status = gate_error), 再原样上抛同一个异常; 没权限 / 不存在的项目、cli check 不记;
  · (B) record_edit 把写手手改后的那一版记成底稿(status = writer_edit): 不带 angle_key、不记飞轮臂 / 看到的卡,
    记不上绝不影响 record_edit, 日志里没有稿子的字;
  · 读底稿的几处按「有判定的行说了算」: 转正(_snapshot_groups)、牌算没算写过(snapshotted_angle_keys)、draw-report 的 D1;
  · 只由 writer_edit 转正的 batch 记 via = record_edit, 不销牌、不记臂和卡, 连着它的帖不算改稿差异(当晚、以后几晚都不算)。
"""
from __future__ import annotations

import logging
import uuid

import pytest

from deskcore import calllog, core, store, tools, tvlink
from deskcore import drawreport as dr
from tests.fakes import FakeClient
from tests.test_draft_snapshots import (BODY_A, BODY_OTHER, BODY_S, KEY, KEY2, ME, P1, TV, WRITER, _client, _desk,
                                        _iso, _links, _note, _snap)

SECRET = "这一句只在稿子里出现绝不能进日志"


@pytest.fixture(autouse=True)
def _no_embeddings(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)


def _vid(snap):
    return core._snapshot_version_id(P1, snap["text_fp"])


def _batch_of(c, link):
    item = next(i for i in c.rows["items"] if i["id"] == link["item_id"])
    return item, next(b for b in c.rows["batches"] if b["id"] == item["batch_id"])


def _diff_rows(c, nid="n1"):
    return [d for d in c.rows.get("tv_edit_diffs", []) if d["note_id"] == nid]


def _wire_into_index(c, vid, params):
    """假库不做 embedded join: 把转正建的那一版手动嵌进对照索引(versions_for_linking 读 items × versions × batches)。"""
    item = next(i for i in c.rows["items"] if i["id"] == _links(c)["n1"]["item_id"])
    ver = next(v for v in c.rows["versions"] if v["id"] == vid)
    item.update(best_version_id=vid, versions=[dict(ver)], batches={"project_id": P1, "params": dict(params)})
    ver["items"] = {"id": item["id"], "batches": {"project_id": P1}}
    return item


# ══════════════════════════════════════════════════════════════════════
# A · 查重本身抛了: 先记底稿(gate_error), 再原样上抛
# ══════════════════════════════════════════════════════════════════════

def _timeout():
    return RuntimeError('{"code":"57014","message":"canceling statement due to statement timeout"}')


def test_the_tool_records_gate_error_rows_then_reraises_the_same_exception(monkeypatch):
    c = _desk()
    monkeypatch.setattr(core, "sb", lambda: c)
    boom = _timeout()
    monkeypatch.setattr(core, "_gate", lambda *a, **k: (_ for _ in ()).throw(boom))
    sink: dict = {}
    drafts = [{"title": "骑了一百公里", "body": BODY_S + " #骑行", "angle_key": KEY},
              {"title": "只有标题", "body": " #骑行 "},
              {"title": "秋招", "body": BODY_A}]
    with pytest.raises(RuntimeError) as ei:
        calllog.collecting(sink, tools.check_drafts, P1, drafts, _user_id=WRITER)
    assert ei.value is boom, "原样上抛查重的那个异常(工具照旧 isError / 500, 模型照旧重查)"
    rows = c.rows["draft_snapshots"]
    assert [r["title"] for r in rows] == ["骑了一百公里", "秋招"], "没正文的照旧不记"
    assert {r["status"] for r in rows} == {core.SNAPSHOT_GATE_ERROR}
    assert rows[0]["angle_key"] == KEY and rows[1]["angle_key"] is None
    assert rows[0]["body"] == BODY_S + " #骑行" and rows[0]["user_id"] == WRITER
    assert rows[0]["flywheel_arm"] in core.FLYWHEEL_ARMS, "AI 写的稿: 臂照记"
    assert len({r["check_id"] for r in rows}) == 1
    assert sink.get("check8") == rows[0]["check_id"][:8], "失败的那一行调用留痕也对得回这批底稿"
    assert c.rows["draft_fingerprints"] == []


def test_a_failing_replacement_lookup_also_records_before_raising(monkeypatch):
    c = _desk()
    boom = TimeoutError("versions read timed out")
    monkeypatch.setattr(core, "_lookup_replacements", lambda *a, **k: (_ for _ in ()).throw(boom))
    with pytest.raises(TimeoutError) as ei:
        core.check_drafts(c, P1, [{"title": "t", "body": BODY_S, "replaces_version_id": str(uuid.uuid4())}],
                          user_id=WRITER, record_snapshot=True)
    assert ei.value is boom
    assert [r["status"] for r in c.rows["draft_snapshots"]] == ["gate_error"]


def test_a_failed_snapshot_write_never_replaces_the_gate_exception(monkeypatch, caplog):
    c = _desk()
    boom = _timeout()
    monkeypatch.setattr(core, "_gate", lambda *a, **k: (_ for _ in ()).throw(boom))

    def insert_fails(*a, **k):
        raise RuntimeError("connection reset while inserting")
    monkeypatch.setattr(store, "insert_draft_snapshots", insert_fails)
    with pytest.raises(RuntimeError) as ei:
        core.check_drafts(c, P1, [{"title": "t", "body": BODY_S}], user_id=WRITER, record_snapshot=True)
    assert ei.value is boom, "记底稿自己出的错不能顶掉查重原来那个"
    assert "记底稿也失败" in caplog.text and c.rows["draft_snapshots"] == []

    # 记底稿那一层自己抛(本来不会): 照样上抛原来那个
    def record_raises(*a, **k):
        raise ValueError("should never escape")
    monkeypatch.setattr(core, "_record_snapshots", record_raises)
    with pytest.raises(RuntimeError) as ei:
        core.check_drafts(c, P1, [{"title": "t", "body": BODY_S}], user_id=WRITER, record_snapshot=True)
    assert ei.value is boom


def test_a_refused_or_unknown_project_and_cli_check_record_nothing(monkeypatch):
    c = _desk()
    monkeypatch.setattr(core, "_gate", lambda *a, **k: (_ for _ in ()).throw(_timeout()))
    drafts = [{"title": "t", "body": BODY_S}]
    with pytest.raises(PermissionError):
        core.check_drafts(c, P1, drafts, user_id=ME, record_snapshot=True)              # 别人的项目
    with pytest.raises(core.ProjectNotFound):
        core.check_drafts(c, str(uuid.uuid4()), drafts, user_id=WRITER, record_snapshot=True)
    with pytest.raises(PermissionError):
        core.check_drafts(c, P1, drafts, user_id=None, record_snapshot=True)            # 认不出是谁
    with pytest.raises(RuntimeError):
        core.check_drafts(c, P1, drafts, user_id=WRITER)                                 # cli check: 不记
    assert c.rows["draft_snapshots"] == []
    assert core.check_drafts(c, P1, [], user_id=WRITER, record_snapshot=True)["results"] == []
    assert c.rows["draft_snapshots"] == []


# ── tv-sync / 读底稿的几处 ─────────────────────────────────────────────

def test_a_gate_error_only_snapshot_is_promoted_when_a_post_matches():
    snap = _snap("s1", "骑了一百公里", BODY_S, status="gate_error")
    c = _client([_note("n1", "骑了一百公里", BODY_S)], [snap])
    out = core.tv_sync(c, TV)
    l1 = _links(c)["n1"]
    assert l1["match_kind"] == "body_exact" and l1["version_id"] == _vid(snap)
    assert out["snapshots"]["promoted"] == 1 and out["snapshots"]["angles_consumed"] == 1, "AI 写的稿: 照常销牌"
    _, batch = _batch_of(c, l1)
    assert batch["params"]["via"] == "snapshot" and batch["params"]["flywheel_arm"] == "patterns"
    assert _diff_rows(c), "照常算改稿差异"


@pytest.mark.parametrize("statuses", [("gate_error", "reject"), ("reject", "gate_error"),
                                      ("writer_edit", "reject"), ("reject", "writer_edit"),
                                      ("gate_error", "writer_edit", "reject")])
def test_a_real_reject_vetoes_promotion_of_rows_without_a_verdict(statuses):
    snaps = [_snap(f"s{i}", "骑了一百公里", BODY_S, days=i * 0.1, status=st, check=f"c{i}")
             for i, st in enumerate(statuses)]
    c = _client([_note("n1", "骑了一百公里", BODY_S)], snaps)
    out = core.tv_sync(c, TV)
    assert out["snapshots"]["promoted"] == 0 and _links(c)["n1"]["match_kind"] == "ingested"


@pytest.mark.parametrize("statuses", [("gate_error", "pass"), ("gate_error", "reject", "warn"),
                                      ("writer_edit", "pass"), ("gate_error", "writer_edit")])
def test_a_real_pass_or_rows_without_a_verdict_promote(statuses):
    snaps = [_snap(f"s{i}", "骑了一百公里", BODY_S, days=i * 0.1, status=st, check=f"c{i}")
             for i, st in enumerate(statuses)]
    c = _client([_note("n1", "骑了一百公里", BODY_S)], snaps)
    out = core.tv_sync(c, TV)
    assert out["snapshots"]["promoted"] == 1
    _, batch = _batch_of(c, _links(c)["n1"])
    assert batch["params"]["via"] == "snapshot", "混了别的行的不算写手手改版"
    assert _diff_rows(c), "混了别的行的照常算改稿差异"


def test_snapshot_groups_let_verdict_rows_decide():
    def ok(*statuses):
        rows = [_snap(f"s{i}", "t", BODY_S, days=i, status=st) for i, st in enumerate(statuses)]
        (g,) = core._snapshot_groups(rows).values()
        return g["ok"], g["writer_edit_only"]
    assert ok("pass") == (True, False) and ok("reject") == (False, False)
    assert ok(None) == (True, False) and ok("gate_error") == (True, False)
    assert ok("writer_edit") == (True, True) and ok("writer_edit", "writer_edit") == (True, True)
    assert ok("gate_error", "writer_edit") == (True, False)
    assert ok("gate_error", "reject") == (False, False) and ok("writer_edit", "pass") == (True, False)
    assert ok("reject", "pass") == (True, False), "以前的规矩不变: 打回后改过、通过了的照样转正"
    assert ok("something_new") == (False, False), "认不出的 status 不转正"
    (g,) = core._snapshot_groups([_snap("s1", "t", BODY_S)]).values()
    assert not [k for k in g if k.startswith("_")], "内部记账的键不留在组里"


def test_gate_error_only_rows_count_as_desk_text_for_the_origin_gate():
    rows = [_snap("s1", "t", BODY_S, status="gate_error"), _snap("s2", "t2", BODY_A, status="writer_edit"),
            _snap("s3", "t3", BODY_OTHER, status="gate_error"), _snap("s4", "t3", BODY_OTHER, days=1, status="reject")]
    snaps = core._gate_snapshots(core._snapshot_groups(rows), set())
    assert sorted(v.title for v in snaps) == ["t", "t2"], "查重出错、写手手改的算写作台见过; 判定打回的不算"


def test_a_gate_error_retry_does_not_undo_an_earlier_pass():
    now, before = store.iso_now(), store.iso_ago(1)
    snaps = [dict(_snap("s1", "t", BODY_S), checked_at=before, status="pass"),
             dict(_snap("s2", "t", BODY_S, check="c2"), checked_at=now, status="gate_error"),     # 重查时超时了
             dict(_snap("s3", "t2", BODY_A, key=KEY2), checked_at=now, status="gate_error")]       # 只出过错
    c = _desk({"draft_snapshots": snaps})
    keys = store.snapshotted_angle_keys(c, P1, WRITER, core.ANGLE_DEBT_DAYS)
    assert keys == {KEY: before}, "出错的那次不是判定: KEY 照旧算写过了, 时刻是通过的那次; KEY2 照旧欠着"
    # reject 之后出错: 照旧欠着(最近一次判定是 reject)
    c = _desk({"draft_snapshots": [dict(_snap("s1", "t", BODY_S), checked_at=before, status="reject"),
                                   dict(_snap("s2", "t", BODY_S), checked_at=now, status="gate_error")]})
    assert store.snapshotted_angle_keys(c, P1, WRITER, core.ANGLE_DEBT_DAYS) == {}


def test_draw_report_d1_ignores_rows_without_a_verdict():
    from tests.test_draw_report import NOW, _card
    from tests.test_draw_report import _iso as at
    from tests.test_draw_report import P1 as DP1
    rows = [_card(1), _card(2), _card(3), _card(4)]

    def s(i, status):
        return {"project_id": DP1, "angle_key": f"{i:020x}", "status": status, "checked_at": at(9)}
    snaps = [s(1, "gate_error"), s(2, "pass"), s(2, "gate_error"), s(2, "writer_edit"), s(3, None),
             s(4, "gate_error"), s(4, "warn")]
    assert [c["d1"] for c in dr.card_rows(rows, snapshots=snaps, now=NOW)] == [None, 0, None, 1], \
        "出错 / 手改 / 没有 status 的不是「查过、没撞」, 不进 D1 的分母"


# ══════════════════════════════════════════════════════════════════════
# B · record_edit 记写手手改版(writer_edit)
# ══════════════════════════════════════════════════════════════════════

class _EditClient(FakeClient):
    """style_edits 的 distilled / created_at 是库里的默认值, 假件不补(同 test_style_distillation_safety)。"""

    def _execute(self, q):
        if q.table_name == "style_edits" and q.op == "insert":
            q.payload.setdefault("distilled", False)
            q.payload.setdefault("created_at", store.iso_now())
        return super()._execute(q)


def _rec_client():
    return _EditClient(rows={
        "projects": [{"id": P1, "name": "途鸽", "owner_id": WRITER, "calibration_notes": "",
                      "tactics": "[]", "custom_roles": []}],
        "draft_snapshots": [], "style_edits": [], "user_calibration_notes": []})


def _edit(c, *, distill=False, user=WRITER, **kw):
    args = {"ai_title": "AI 写的标题", "ai_body": BODY_A, "my_title": "我改的标题", "my_body": BODY_S + " #骑行"}
    args.update(kw)
    return core.record_edit(c, P1, user_id=user, distill=distill, **args)


def test_record_edit_records_one_writer_edit_row_with_the_writers_text(monkeypatch):
    called = []
    monkeypatch.setattr(core, "batch_arm", lambda *a, **k: called.append("arm") or ("patterns", "borrow", None, None))
    monkeypatch.setattr(core, "lesson_exposure", lambda *a, **k: called.append("exposure") or {"cards": ["x"]})
    c = _rec_client()
    sink: dict = {}
    out = calllog.collecting(sink, _edit, c)
    (row,) = c.rows["draft_snapshots"]
    assert row["status"] == core.SNAPSHOT_WRITER_EDIT == "writer_edit"
    assert (row["title"], row["body"]) == ("我改的标题", BODY_S + " #骑行")
    assert row["text_fp"] == core._draft_text_fp("我改的标题", BODY_S)
    assert row["angle_key"] is None and row["user_id"] == WRITER and row["project_id"] == P1
    assert uuid.UUID(row["check_id"]) and sink.get("check8") == row["check_id"][:8]
    assert row["flywheel_arm"] is None and row["flywheel_arm_source"] is None
    assert core.LESSON_EXPOSURE_KEY not in row and called == [], "人写的字: 不取臂、不取看到的卡"
    assert len(c.rows["style_edits"]) == 1 and c.rows["style_edits"][0]["my_body"] == BODY_S + " #骑行"
    assert c.rows.get("draft_fingerprints", []) == [], "不进指纹库"

    # 回给模型的和以前一模一样
    monkeypatch.setattr(core, "_record_snapshots", lambda *a, **k: 0)
    assert out == _edit(_rec_client())
    plain = _rec_client()
    assert set(_edit(plain, distill=True)) == {"stored", "distillation_task", "next_step"}


def test_an_identical_edit_records_nothing():
    c = _rec_client()
    out = _edit(c, my_title="AI 写的标题", my_body=BODY_A + "  ")
    assert out["stored"] is False and c.rows["draft_snapshots"] == [] and c.rows["style_edits"] == []


def test_a_one_sided_edit_takes_the_other_side_from_the_ai_version():
    c = _rec_client()
    _edit(c, my_body="")                                   # 只改了标题: 发出去的正文是 AI 那一版
    _edit(c, my_title="")                                  # 只改了正文: 标题是 AI 那一版
    _edit(c, ai_body="", my_body="", my_title="只有标题")  # 两边都没正文: 对不上任何帖, 不记
    assert [(r["title"], r["body"]) for r in c.rows["draft_snapshots"]] == [
        ("我改的标题", BODY_A), ("AI 写的标题", BODY_S + " #骑行")]
    assert len(c.rows["style_edits"]) == 3, "记不记底稿不影响 style_edits"


@pytest.mark.parametrize("exc", [
    RuntimeError(f'{{"code":"23502","message":"null value","details":"Failing row contains (x, {SECRET})."}}'),
    TimeoutError(f"57014 canceling statement {SECRET}"),
    RuntimeError(f"PGRST205 Could not find the table 'autowriter.draft_snapshots' {SECRET}")])
def test_a_failed_snapshot_never_breaks_record_edit_and_logs_no_text(monkeypatch, caplog, exc):
    caplog.set_level(logging.WARNING)

    def insert_fails(*a, **k):
        raise exc
    monkeypatch.setattr(store, "insert_draft_snapshots", insert_fails)
    c = _rec_client()
    out = _edit(c, my_body=BODY_S + SECRET, distill=True)
    assert out["stored"] is True and "distillation_task" in out
    assert len(c.rows["style_edits"]) == 1
    assert "record_edit" in caplog.text and type(exc).__name__ in caplog.text
    assert SECRET not in caplog.text and "Traceback" not in caplog.text, "日志里只有异常的类型, 没有稿子的字"
    # 工具那一层: 照常回 stored, 不变成 InvalidInput
    monkeypatch.setattr(core, "sb", lambda: c)
    got = tools.record_edit(P1, "AI 写的标题", BODY_A, "我改的标题", BODY_S + SECRET, _user_id=WRITER)
    assert got["stored"] is True and "distillation_task" in got
    assert c.rows["draft_snapshots"] == [] and len(c.rows["style_edits"]) == 2
    assert SECRET not in caplog.text


def test_record_edit_survives_even_if_the_snapshot_helper_itself_raises(monkeypatch):
    def record_raises(*a, **k):
        raise ValueError("should never escape")
    monkeypatch.setattr(core, "_record_snapshots", record_raises)
    c = _rec_client()
    assert _edit(c)["stored"] is True and len(c.rows["style_edits"]) == 1


def test_someone_elses_project_records_nothing(monkeypatch):
    c = _rec_client()
    with pytest.raises(PermissionError):
        _edit(c, user=ME)
    monkeypatch.setattr(core, "sb", lambda: c)
    with pytest.raises(PermissionError):
        tools.record_edit(P1, "AI 写的标题", BODY_A, "我改的标题", BODY_S, _user_id=ME)
    assert c.rows["draft_snapshots"] == [] and c.rows["style_edits"] == []


# ── tv-sync: 只由 writer_edit 转正 ─────────────────────────────────────

def _writer_edit_snap(**extra):
    # 022 跑过的库: 读底稿时 lesson_exposure 这一列在(null)。臂故意填上: 写手手改版本来没有, 有也不能带进 batch
    return _snap("s1", "骑了一百公里", BODY_S, key=None, status="writer_edit", check="e1",
                 flywheel_arm="patterns", **{core.LESSON_EXPOSURE_KEY: None, **extra})


def test_a_writer_edit_only_group_is_promoted_with_via_record_edit_and_no_edit_diff(monkeypatch):
    snap = _writer_edit_snap()
    c = _client([_note("n1", "骑了一百公里", BODY_S)], [snap])
    out = core.tv_sync(c, TV)
    l1 = _links(c)["n1"]
    vid = _vid(snap)
    assert l1["match_kind"] == "body_exact" and l1["version_id"] == vid and l1["item_id"]
    sn = out["snapshots"]
    assert (sn["matched_notes"], sn["promoted"], sn["minted"], sn["angles_consumed"]) == (1, 1, 1, 0)
    item, batch = _batch_of(c, l1)
    assert batch["params"]["via"] == core.SNAPSHOT_VIA_RECORD_EDIT == "record_edit"
    assert batch["params"]["source"] == "deskcore" and batch["params"]["check_id"] == "e1"
    assert "flywheel_arm" not in batch["params"] and core.LESSON_EXPOSURE_KEY not in batch["params"]
    assert item["user_id"] == WRITER
    assert c.rows["angle_ledger"][0]["consumed_version_id"] is None, "没有 angle_key, 不销牌"
    assert c.rows["draft_snapshots"][0]["promoted_at"] and c.rows["draft_snapshots"][0]["version_id"] == vid
    assert _diff_rows(c) == [], "写手自己的字: 当晚不算改稿差异(AI → 人的差异已经在 style_edits 里)"
    assert [f["version_id"] for f in c.rows["draft_fingerprints"]] == [vid], "指纹照写: 查重要认得这篇"

    # 以后几晚: 那一版进了对照索引(via 从 batch 的 params 读), 照旧不补算
    _wire_into_index(c, vid, batch["params"])
    assert tvlink.Version.from_row(store.versions_for_linking(c, P1)[0]).via == "record_edit"
    core.tv_sync(c, TV)
    assert _diff_rows(c) == [], "以后几晚也不补算"

    # 对照组: 同一篇换成普通底稿转正的, 当晚的补算就会算出一行 —— 上面没有行是 via 管的, 不是别的原因
    _wire_into_index(c, vid, dict(batch["params"], via="snapshot"))
    core.tv_sync(c, TV)
    (row,) = _diff_rows(c)
    assert row["diff_rev"] == tvlink.DIFF_REV and row["changed"] is False
    # 已经有行(按当前口径)的, 那一版是写手手改版就降回旧口径: TV 的未改动只数当前口径的行, 分母里就没有它
    _wire_into_index(c, vid, batch["params"])
    core.tv_sync(c, TV)
    (row,) = _diff_rows(c)
    assert row["diff_rev"] == 0


def test_a_post_matched_through_record_edit_gets_no_diff_but_others_that_night_still_do():
    snaps = [_writer_edit_snap(), _snap("s2", "外面那篇", BODY_OTHER, check="c2", key=KEY2)]
    notes = [_note("n1", "骑了一百公里", BODY_S), _note("n2", "外面那篇", BODY_OTHER)]
    c = _client(notes, snaps)
    out = core.tv_sync(c, TV)
    assert out["snapshots"]["promoted"] == 2
    assert _diff_rows(c, "n1") == [] and len(_diff_rows(c, "n2")) == 1
    vias = {_links(c)[n]["note_id"]: _batch_of(c, _links(c)[n])[1]["params"]["via"] for n in ("n1", "n2")}
    assert vias == {"n1": "record_edit", "n2": "snapshot"}, "一次查重一个 batch, 两种不混"


def test_versions_for_linking_carries_the_batch_via_as_its_last_key():
    def item(i, params):
        return {"id": f"i{i}", "best_version_id": f"v{i}", "created_at": _iso(-i),
                "versions": [{"id": f"v{i}", "title": "t", "body": BODY_S, "version_num": 1, "created_at": _iso(-i)}],
                "batches": {"project_id": P1, "params": params}}
    c = FakeClient(rows={"items": [item(1, {"source": "deskcore", "via": "record_edit"}),
                                   item(2, '{"source": "deskcore", "via": "snapshot"}'),
                                   item(3, {"source": "deskcore"}), item(4, None)]})
    got = {r["version_id"]: r for r in store.versions_for_linking(c, P1)}
    assert {v: r["via"] for v, r in got.items()} == {"v1": "record_edit", "v2": "snapshot", "v3": None, "v4": None}
    assert all(list(r)[-1] == "via" for r in got.values())
    assert tvlink.Version.from_row(got["v1"]).via == "record_edit"
    assert tvlink.Version.from_row({"version_id": "x", "title": "t", "body": "b"}).via is None


# ══════════════════════════════════════════════════════════════════════
# 复审(2026-11-26): 写手手改版只兜底; 混着的组; 空白; 日志; 对照上的说明; 报表; 改稿版不沿用 via
# ══════════════════════════════════════════════════════════════════════

# 写手改了两句的正文(和 BODY_S 大半一样): 帖贴的是这一版
EDITED_S = BODY_S.replace("补给站的香蕉早就吃完了", "补给站的香蕉早被前面的人拿光了").replace("撑到终点", "一路咬牙撑到终点")
# AI 那一版被写手整篇换掉了: 和帖几乎没有一样的字
AI_FAR = ("这一版是模型最早给的稿子, 讲的是城市夜跑的装备清单和配速表, 和最后发出去的那篇骑行几乎没有一个四字串一样。"
          "写手看完觉得不对味, 自己从头写了一篇骑行的, 再用 record_edit 报给写作台。") * 2


def _ai_snap(title, body):
    return _snap("s1", title, body, days=0, check="c1")           # 查重通过、带 KEY、臂 patterns


def _we_snap(title, body, days=0.5):
    return _snap("s2", title, body, days=days, key=None, status="writer_edit", check="e1",
                 flywheel_arm=None, flywheel_arm_source=None)


def _outcome(c, out):
    l1 = _links(c)["n1"]
    _, batch = _batch_of(c, l1)
    led = c.rows["angle_ledger"][0]["consumed_version_id"]
    diffs = _diff_rows(c)
    return {"kind": l1["match_kind"], "version": l1["version_id"], "via": batch["params"]["via"],
            "arm": batch["params"].get("flywheel_arm"), "consumed": out["snapshots"]["angles_consumed"],
            "ledger": led, "diff": [(d["changed"], d["diff_rev"]) for d in diffs],
            "note": [e["note"] for e in l1["candidates"] or () if str(e.get("note", "")).startswith("W-38: ")]}


@pytest.mark.parametrize("ai_title, ai_body, my_title, my_body", [
    ("AI 写的标题", BODY_S, "我改的标题", BODY_S),          # 只改了标题: record_edit 记 (my_title, ai_body)
    ("AI 写的标题", BODY_S, "AI 写的标题", EDITED_S)],      # 正文改了两句
    ids=["title_only", "light_body_edit"])
def test_a_writer_edit_never_steals_the_post_from_the_keyed_ai_snapshot(ai_title, ai_body, my_title, my_body):
    """AI 那一版查过重(带 KEY)、写手改完 record_edit、帖贴的是手改版: 和 B 之前一模一样 —— 连 AI 那份底稿、销牌、记臂、
    算 AI → 发出去的改稿差异; 写手手改版那一行留着不转正(复审 11-26: 以前它离帖最近, 总是它赢)。"""
    ai, we = _ai_snap(ai_title, ai_body), _we_snap(my_title, my_body)
    post = [_note("n1", my_title, my_body)]
    c0 = _client(post, [dict(ai)])
    before = _outcome(c0, core.tv_sync(c0, TV))
    c = _client(post, [dict(ai), dict(we)])
    out = core.tv_sync(c, TV)
    after = _outcome(c, out)
    assert after == before, "多了一行写手手改版, 结果不变"
    assert after["version"] == _vid(ai) and after["via"] == "snapshot" and after["arm"] == "patterns"
    assert after["consumed"] == 1 and after["ledger"] == _vid(ai), "牌照销: 连的是带 KEY 的 AI 底稿"
    assert after["diff"] and after["diff"][0][1] == tvlink.DIFF_REV, "AI → 发出去的改稿差异照算"
    assert after["note"] == [core.SNAPSHOT_NOTE]
    state = {r["id"]: r["promoted_at"] for r in c.rows["draft_snapshots"]}
    assert state["s1"] and state["s2"] is None, "手改版那一行留着(过 45 天不再参与、60 天删)"
    assert out["snapshots"]["writer_edit"] == 0


def test_a_post_only_the_writer_edit_matches_still_falls_back_to_it():
    """AI 那一版被写手整篇换掉(对不上帖), 只有手改版对得上: 照旧兜底转正, via = record_edit, 不销牌、不算改稿差异;
    对照上的说明写的是 record_edit, 不是「查重时记下的」。"""
    ai, we = _ai_snap("AI 写的标题", AI_FAR), _we_snap("骑了一百公里", BODY_S)
    c = _client([_note("n1", "骑了一百公里", BODY_S)], [ai, we])
    out = core.tv_sync(c, TV)
    got = _outcome(c, out)
    assert got["version"] == _vid(we) and got["via"] == "record_edit" and got["arm"] is None
    assert got["consumed"] == 0 and got["ledger"] is None and got["diff"] == []
    assert got["note"] == [core.SNAPSHOT_NOTE_WRITER_EDIT] and core.SNAPSHOT_NOTE not in got["note"]
    assert out["snapshots"]["writer_edit"] == 1 and out["snapshots"]["matched_notes"] == 1
    state = {r["id"]: r["promoted_at"] for r in c.rows["draft_snapshots"]}
    assert state["s1"] is None and state["s2"]


def test_an_undated_post_that_only_a_writer_edit_matches_is_held_not_ingested():
    we = _we_snap("骑了一百公里", BODY_S)
    c = _client([_note("n1", "骑了一百公里", BODY_S, publish_time=None)], [_ai_snap("AI 写的标题", AI_FAR), we])
    out = core.tv_sync(c, TV)
    assert out["snapshots"]["undated"] == 1 and _links(c)["n1"]["match_kind"] == "unmatched"
    assert len(c.rows["versions"]) == 1, "没补录成副本: 填了日期还认得回手改版"


def test_the_cli_says_how_many_posts_matched_a_writer_edit(capsys):
    c = _client([_note("n1", "骑了一百公里", BODY_S)], [_writer_edit_snap()])
    args = type("A", (), {"tv_project": TV, "all": False, "dry_run": False, "write_tv": False,
                          "since": None, "rematch": False})()
    from deskcore import cli
    assert cli._tv_sync(core, c, args) == 0
    assert "其中 1 篇对上的是 record_edit 记下的写手手改版" in capsys.readouterr().out


@pytest.mark.parametrize("writer_edit_first", [True, False])
def test_a_mixed_group_is_an_ordinary_snapshot_with_the_arm_of_its_first_checked_row(writer_edit_first):
    """手改版和查重的行 text_fp 一样(模型查过的正是写手改后的字): 照普通底稿转正(via = snapshot, 销牌, 算改稿差异);
    臂取第一次不是 writer_edit 的那一行, 先记了哪一行都一样(复审 11-26)。这是有意的口径, 钉在这里。"""
    we = _we_snap("我改的标题", BODY_S, days=0 if writer_edit_first else 0.4)
    chk = _snap("s3", "我改的标题", BODY_S, days=0.2, check="c3")
    c = _client([_note("n1", "我改的标题", BODY_S)], [we, chk])
    out = core.tv_sync(c, TV)
    got = _outcome(c, out)
    assert got["via"] == "snapshot" and got["arm"] == "patterns" and got["consumed"] == 1
    assert got["diff"] and got["note"] == [core.SNAPSHOT_NOTE] and out["snapshots"]["writer_edit"] == 0
    (g,) = core._snapshot_groups([we, chk]).values()
    assert (g["flywheel_arm"], g["flywheel_arm_source"]) == ("patterns", "borrow")
    (g,) = core._snapshot_groups([_writer_edit_snap()]).values()
    assert g["flywheel_arm"] is None, "只有手改版的组没有臂(就算那一行带着)"


def test_whitespace_only_my_fields_fall_back_to_the_ai_side():
    c = _rec_client()
    out = _edit(c, my_body="  \n")                         # 正文只给了空白: 当没给, 用 AI 那一版
    assert out["stored"] is True
    _edit(c, my_title="   ", my_body=BODY_S)               # 标题只给了空白
    assert [(r["title"], r["body"]) for r in c.rows["draft_snapshots"]] == [
        ("我改的标题", BODY_A), ("AI 写的标题", BODY_S)]


def test_a_failed_gate_error_snapshot_logs_only_the_exception_type(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    c = _desk()
    boom = RuntimeError(f"57014 canceling statement while scanning {SECRET}2")
    monkeypatch.setattr(core, "_gate", lambda *a, **k: (_ for _ in ()).throw(boom))

    def insert_fails(*a, **k):
        raise RuntimeError(f'{{"code":"23502","details":"Failing row contains (x, {SECRET})."}}')
    monkeypatch.setattr(store, "insert_draft_snapshots", insert_fails)
    with pytest.raises(RuntimeError) as ei:
        core.check_drafts(c, P1, [{"title": "t", "body": BODY_S + SECRET}], user_id=WRITER, record_snapshot=True)
    assert ei.value is boom
    assert "记底稿也失败" in caplog.text and "RuntimeError" in caplog.text
    assert SECRET not in caplog.text and "Traceback" not in caplog.text, \
        "只记异常的类型: 插入失败的那行字、查重原来那个异常都不进日志"


def test_a_later_revision_on_a_record_edit_item_does_not_inherit_the_via():
    """via 是 batch 的, 「这是写手自己的字」只对转正建的那一版(version_num 1)成立: 同一个 item 后来加的改稿版照常算差异。"""
    params = {"source": "deskcore", "via": "record_edit"}
    v1 = {"id": "v1", "title": "t", "body": BODY_S, "version_num": 1, "created_at": _iso(0)}
    v2 = {"id": "v2", "title": "t2", "body": BODY_A, "version_num": 2, "created_at": _iso(1)}
    c = FakeClient(rows={"items": [
        {"id": "i1", "best_version_id": "v2", "created_at": _iso(0), "versions": [v1, v2],
         "batches": {"project_id": P1, "params": params}},
        {"id": "i2", "best_version_id": "v3", "created_at": _iso(0),
         "versions": [{"id": "v3", "title": "t3", "body": BODY_OTHER, "version_num": 1, "created_at": _iso(0)}],
         "batches": {"project_id": P1, "params": params}}]})
    got = {r["version_id"]: r["via"] for r in store.versions_for_linking(c, P1)}
    assert got == {"v2": None, "v3": "record_edit"}
    assert store.version_via({"batches": {"params": params}}, {"id": "x"}) == "record_edit", "读不到 version_num 当 1"


def test_the_exposure_report_skips_record_edit_batches():
    from deskcore import exposurereport
    from tests.test_lesson_exposure import _RecordingBatches
    rows = [{"id": "old", "project_id": "p", "created_at": "2026-11-01T00:00:00", "params": {"source": "deskcore"}},
            {"id": "snap", "project_id": "p", "created_at": "2026-11-02T00:00:00",
             "params": {"source": "deskcore", "via": "snapshot", store.SNAPSHOT_EXPOSURE_COL: {"cards": ["c1"]}}},
            {"id": "edit", "project_id": "p", "created_at": "2026-11-03T00:00:00",
             "params": {"source": "deskcore", "via": "record_edit"}}]
    sb = type("SB", (), {"table": lambda self, name: _RecordingBatches(rows, [])})()
    out = store.report_exposure_batches(sb, 30)
    assert [b["id"] for b in out] == ["old", "snap"], "写手手改版转正的 batch 故意没有 lesson_exposure, 不进这张表"
    assert exposurereport.report(out)["batches"]["before_feature"] == 1, "「这个功能之前的批次」只剩真在之前的"
