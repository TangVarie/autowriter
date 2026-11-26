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
