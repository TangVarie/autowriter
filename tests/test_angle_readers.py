"""angle cards v2 §6 item 2: store 里新加的几个窄读数(全是 SELECT, 翻页, 只取要的列)。

每个读数钉三件事:
  · 翻页读全 —— 服务端 db-max-rows 把每一页钳短时也不漏(本仓踩得最多的坑, 假件的 max_rows 模拟它);
  · 只取要的列 —— 结果表不取标题 / 正文; 外部帖不取正文; 项目表不取客户品牌名;
  · 读挂了回 None、不抛 —— 发牌和简报照常出(调用方退回缓存 / 快照 / 只靠 RPC 避重)。None 和 [] 分开: [] 是真没有。

数据全是编的; TV 项目号用 AAA_phase1 这种占位。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from deskcore import store
from deskcore.fingerprint import angle_key
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
PID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER = "cccccccc-cccc-cccc-cccc-cccccccccccc"
NOW = datetime.now(timezone.utc)


def _ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat()


def _naive_ago(**kw) -> str:
    """TV 的 publish_time 是 timestamp without time zone, PostgREST 回的串不带时区。"""
    return (NOW - timedelta(**kw)).replace(tzinfo=None).isoformat()


class Flaky(FakeClient):
    """读 ``fail_table`` 的第 ``fail_on`` 次(从 1 数)起抛错; ``fail_schema=True`` 时 schema() 本身就抛。"""

    def __init__(self, *a, fail_table=None, fail_on=1, fail_schema=False, **kw):
        super().__init__(*a, **kw)
        self.fail_table, self.fail_on, self.fail_schema, self.seen = fail_table, fail_on, fail_schema, 0

    def schema(self, name):
        if self.fail_schema:
            raise RuntimeError("PGRST106 The schema must be one of the following: autowriter")
        return super().schema(name)

    def _execute(self, q):
        if self.fail_table and q.table_name == self.fail_table:
            self.seen += 1
            if self.seen >= self.fail_on:
                raise RuntimeError('{"code":"57014","message":"canceling statement due to statement timeout"}')
        return super()._execute(q)


def _calls(c, table):
    return [q for q in c.calls if q["table"] == table]


# ══════════════════════════════════════════════════════════════════════
# ① 全库结果表
# ══════════════════════════════════════════════════════════════════════

def _notes(n, *, nulls=0):
    rows = []
    for i in range(n):
        rows.append({"note_id": f"n{i:05d}", "project_id": ("AAA_phase1", "AAA_phase2", "BBB_phase1")[i % 3],
                     "publish_time": None if i < nulls else _naive_ago(days=i % 90),
                     "content_format": "提问求助", "emotional_lever": "焦虑撬动",
                     "human_truth_archetype": ["健康焦虑"], "emotional_intensity": "medium",
                     "trend_dependencies": ["通用"], "tier": "参考", "comments_count": i % 40,
                     "data_quality_flags": {}, "title": "(不该取)", "body": "(不该取)", "raw_content": "(不该取)"})
    return rows


@pytest.mark.parametrize("max_rows", [None, 300])
def test_outcomes_read_everything_page_by_page(max_rows):
    rows = _notes(2350, nulls=17)
    c = FakeClient(rows={"truth_vault.notes": rows}, max_rows=max_rows)
    got = store.tv_notes_outcomes(c)
    assert [r["note_id"] for r in got] == [r["note_id"] for r in rows[17:]], "一行不漏、不重, 按 note_id 升序"
    sel = _calls(c, "truth_vault.notes")
    assert all(q["cols"] == store.TV_OUTCOME_COLS for q in sel)
    assert all(("isnot", "publish_time", "null") in q["filters"] for q in sel), "没有发布时间的不要"
    assert not any(k in r for r in got for k in ("title", "body", "raw_content"))
    # 游标翻页: 第一页不带 gt, 之后每页 gt 上一页最后一个 id; 读到空页才收工
    assert not any(f[0] == "gt" for f in sel[0]["filters"])
    assert all(any(f[:2] == ("gt", "note_id") for f in q["filters"]) for q in sel[1:])
    per = max_rows or store.TV_OUTCOMES_PAGE
    assert len(sel) == -(-len(got) // per) + 1


def test_outcomes_page_starts_after_the_cursor():
    c = FakeClient(rows={"truth_vault.notes": _notes(50)})
    page = store.tv_notes_outcomes_page(c, after="n00020", limit=10)
    assert [r["note_id"] for r in page] == [f"n{i:05d}" for i in range(21, 31)]


def test_outcomes_stop_at_the_page_cap_and_say_so(caplog):
    c = FakeClient(rows={"truth_vault.notes": _notes(500)})
    with caplog.at_level(logging.WARNING, logger="deskcore.store"):
        got = store.tv_notes_outcomes(c, max_pages=2, page=100)
    assert len(got) == 200
    assert "stopped at 2 pages" in caplog.text


@pytest.mark.parametrize("fail_on", [1, 2])
def test_outcomes_failure_is_none_not_a_partial_table(fail_on):
    c = Flaky(rows={"truth_vault.notes": _notes(2500)}, fail_table="truth_vault.notes", fail_on=fail_on)
    assert store.tv_notes_outcomes(c) is None
    assert store.tv_notes_outcomes_page(Flaky(fail_schema=True)) is None
    assert store.tv_notes_outcomes(Flaky(fail_schema=True)) is None


def test_outcomes_empty_table_is_empty_not_none():
    assert store.tv_notes_outcomes(FakeClient(rows={"truth_vault.notes": []})) == []


# ══════════════════════════════════════════════════════════════════════
# ② title_is_question
# ══════════════════════════════════════════════════════════════════════

def _answer(sid, ans, *, at_days=1, extractor="jev:a", qv=1, q="title_is_question", st="note", tag="primary"):
    return {"subject_type": st, "subject_id": sid, "question_id": q, "question_version": qv,
            "extractor": extractor, "run_tag": tag, "answer": ans, "extracted_at": _ago(days=at_days),
            "evidence": "(不该取)", "bank_version": "fq-v0.1"}


@pytest.mark.parametrize("max_rows", [None, 70])
def test_title_answers_take_the_latest_valid_answer_per_note(max_rows):
    rows = [_answer(f"n{i:04d}", "是" if i % 3 else "否") for i in range(450)]
    rows += [
        _answer("n0001", "否", at_days=0.5, extractor="jev:b"),      # 更晚抽的那一行赢
        _answer("n0002", "是", at_days=9, extractor="jev:b"),        # 更早的输
        _answer("n0004", None, at_days=0.1, extractor="jev:c"),      # 空答案不算
        _answer("n0005", "", at_days=0.1, extractor="jev:c"),
        _answer("n0007", "否", q="opening_type"),                    # 别的题
        _answer("n0008", "否", st="aw_version"),                     # 别的主体
        _answer("n0010", "否", tag="gate1-v2"),                      # 别的批次
        _answer("x-only-invalid", None),
    ]
    c = FakeClient(rows={"truth_vault.note_feature_answers": rows}, max_rows=max_rows)
    got = store.tv_title_answers(c, page=100)
    assert len(got) == 450 and "x-only-invalid" not in got
    assert got["n0001"] == "否" and got["n0002"] == "是" and got["n0004"] == "是" and got["n0005"] == "是"
    assert got["n0007"] == "是" and got["n0008"] == "是" and got["n0010"] == "是"
    sel = _calls(c, "truth_vault.note_feature_answers")
    assert all(q["cols"] == "subject_id, answer, extracted_at" for q in sel), "不取 evidence"
    assert all({("eq", "subject_type", "note"), ("eq", "run_tag", "primary"),
                ("eq", "question_id", "title_is_question")} <= set(q["filters"]) for q in sel)
    assert sel[0]["orders"] == ["subject_id", "question_version", "extractor"], "排序唯一, 翻页不漏不重"


def test_title_answers_failure_is_none():
    rows = [_answer(f"n{i:04d}", "是") for i in range(250)]
    assert store.tv_title_answers(Flaky(rows={"truth_vault.note_feature_answers": rows},
                                        fail_table="truth_vault.note_feature_answers", fail_on=2), page=100) is None
    assert store.tv_title_answers_page(Flaky(fail_schema=True)) is None


# ══════════════════════════════════════════════════════════════════════
# ③ 项目表
# ══════════════════════════════════════════════════════════════════════

def test_projects_meta_uses_the_id_prefix_as_brand_and_never_reads_the_brand_column():
    rows = [{"project_id": "AAA_phase1", "category": "保健品", "brand": "(客户品牌名, 不该取)"},
            {"project_id": "AAA_phase2", "category": "保健品", "brand": "(不该取)"},
            {"project_id": "BBB", "category": None, "brand": "(不该取)"}]
    c = FakeClient(rows={"truth_vault.projects": rows})
    got = store.tv_projects_meta(c)
    assert got == {"AAA_phase1": {"category": "保健品", "brand_key": "AAA"},
                   "AAA_phase2": {"category": "保健品", "brand_key": "AAA"},
                   "BBB": {"category": None, "brand_key": "BBB"}}
    assert [q["cols"] for q in c.calls] == ["project_id, category"]


def test_projects_meta_failure_is_none():
    assert store.tv_projects_meta(Flaky(fail_schema=True)) is None
    assert store.tv_projects_meta(Flaky(rows={"truth_vault.projects": []}, fail_table="truth_vault.projects")) is None


# ══════════════════════════════════════════════════════════════════════
# ④ 本项目画像用的笔记(带 raw_content)
# ══════════════════════════════════════════════════════════════════════

def test_profile_reads_only_the_mapped_projects_since_the_window_and_pages():
    rows = []
    for i in range(520):
        rows.append({"note_id": f"p{i:04d}", "project_id": "AAA_phase2" if i % 2 else "CCC_phase1",
                     "publish_time": _naive_ago(days=i % 80), "title": None, "raw_content": f"【标题】编的标题{i}\n【正文】x",
                     "content_format": "提问求助", "comments_count": 3, "tier": None, "data_quality_flags": {}})
    since = NOW - timedelta(days=60)
    c = FakeClient(rows={"truth_vault.notes": rows}, max_rows=150)
    got = store.tv_notes_profile(c, ["AAA_phase2", "AAA_phase2", ""], since)
    want = [r["note_id"] for r in rows if r["project_id"] == "AAA_phase2" and r["publish_time"] >= since.replace(tzinfo=None).isoformat()]
    assert sorted(r["note_id"] for r in got) == sorted(want) and len(got) == len(want)
    assert all("raw_content" in r and "title" in r for r in got)
    sel = _calls(c, "truth_vault.notes")
    assert all(q["cols"] == store.TV_OUTCOME_COLS + ", title, raw_content" for q in sel)
    assert all(("in", "project_id", ("AAA_phase2",)) in q["filters"] for q in sel)


def test_profile_without_tv_ids_reads_nothing():
    c = FakeClient(rows={})
    assert store.tv_notes_profile(c, [], NOW) == [] and not c.calls


def test_profile_failure_is_none():
    rows = [{"note_id": f"p{i:04d}", "project_id": "AAA_phase2", "publish_time": _naive_ago(days=1)} for i in range(300)]
    c = Flaky(rows={"truth_vault.notes": rows}, fail_table="truth_vault.notes", fail_on=2)
    assert store.tv_notes_profile(c, ["AAA_phase2"], NOW - timedelta(days=60)) is None
    assert store.tv_notes_profile(Flaky(fail_schema=True), ["AAA_phase2"], "2026-01-01") is None


# ══════════════════════════════════════════════════════════════════════
# ⑤ / ⑦ 特征题答案
# ══════════════════════════════════════════════════════════════════════

QIDS = ("title_is_question", "opening_type", "turning_point", "ending_asks_reader")


def test_note_answers_chunk_ids_page_within_a_chunk_and_keep_the_latest():
    ids = [f"n{i:04d}" for i in range(250)]
    rows = []
    for sid in ids:
        for q in QIDS:
            rows.append(_answer(sid, "是", q=q, extractor="jev:a", at_days=3))
            rows.append(_answer(sid, "否", q=q, extractor="jev:b", at_days=1))    # 后抽的赢
    rows.append(_answer("n0000", "具体事件", q="opening_type", extractor="jev:c", at_days=0.1, qv=2))
    rows.append(_answer("n0001", None, q="opening_type", extractor="jev:c", at_days=0.1))   # 空答案不算
    rows.append(_answer("n0002", "是", q="placebo_rand_1"))                                   # 没问的题
    rows.append(_answer("n0003", "是", q="title_is_question", st="external_note", tag="external", at_days=0))
    c = FakeClient(rows={"truth_vault.note_feature_answers": rows}, max_rows=300)
    got = store.note_answers(c, ids + ["n0000"], QIDS)
    assert set(got) == set(ids)
    assert got["n0000"]["opening_type"] == "具体事件"
    assert got["n0001"]["opening_type"] == "否"
    assert "placebo_rand_1" not in got["n0002"]
    assert got["n0003"]["title_is_question"] == "否", "外部帖的答案不混进来"
    assert all(set(v) == set(QIDS) for v in got.values())
    sel = _calls(c, "truth_vault.note_feature_answers")
    for q in sel:
        ids_in = next(f[2] for f in q["filters"] if f[:2] == ("in", "subject_id"))
        assert len(ids_in) <= store.NOTE_ANSWERS_CHUNK
        assert {("eq", "subject_type", "note"), ("eq", "run_tag", "primary")} <= set(q["filters"])
        assert "evidence" not in q["cols"]
    # 250 个 id → 3 批; 每批 800 行、每页钳到 300 → 3 页 + 1 次空页
    assert len(sel) > 3


def test_note_answers_for_external_notes():
    rows = [_answer("e1", "是", q="efficacy_promise", st="external_note", tag="external"),
            _answer("e1", "身份自述", q="opening_type", st="external_note", tag="external"),
            _answer("e1", "否", q="efficacy_promise")]                                   # TV 笔记那一路, 不算
    c = FakeClient(rows={"truth_vault.note_feature_answers": rows})
    got = store.note_answers(c, ["e1"], ["efficacy_promise", "opening_type"], subject_type="external_note",
                             run_tag="external")
    assert got == {"e1": {"efficacy_promise": "是", "opening_type": "身份自述"}}


def test_note_answers_empty_input_and_failure():
    c = FakeClient(rows={})
    assert store.note_answers(c, [], QIDS) == {} and store.note_answers(c, ["n1"], []) == {} and not c.calls
    rows = [_answer(f"n{i:04d}", "是") for i in range(150)]
    flaky = Flaky(rows={"truth_vault.note_feature_answers": rows}, fail_table="truth_vault.note_feature_answers",
                  fail_on=2)
    assert store.note_answers(flaky, [f"n{i:04d}" for i in range(150)], QIDS) is None
    assert store.note_answers(Flaky(fail_schema=True), ["n1"], QIDS) is None


# ══════════════════════════════════════════════════════════════════════
# ⑥ 外部帖
# ══════════════════════════════════════════════════════════════════════

def test_external_slice_reads_the_category_window_without_bodies():
    rows = []
    for i in range(640):
        rows.append({"note_id": f"{0x65000000 + i:08x}0000000000000000", "category": "运动补给" if i % 4 else "戒烟",
                     "title": f"编的外部标题{i}", "body": "(不该取)", "author": "(不该取)",
                     "comments": i, "liked": i * 3, "collected": i, "ad_like": False, "has_product": False,
                     "fetched_at": _ago(days=50 if i % 10 == 0 else 3)})
    c = FakeClient(rows={"truth_vault.external_notes": rows}, max_rows=200)
    got = store.external_slice(c, "运动补给")
    want = [r for i, r in enumerate(rows) if r["category"] == "运动补给" and i % 10]
    assert len(got) == len(want) and all(r["category"] == "运动补给" for r in got)
    assert not any(k in r for r in got for k in ("body", "author"))
    assert all(q["cols"] == "note_id, category, title, comments, liked, collected, fetched_at, ad_like, has_product"
               for q in _calls(c, "truth_vault.external_notes"))


def test_external_slice_without_category_and_on_failure():
    c = FakeClient(rows={})
    assert store.external_slice(c, None) == [] and store.external_slice(c, " ") == [] and not c.calls
    assert store.external_slice(Flaky(fail_schema=True), "运动补给") is None
    assert store.external_slice(Flaky(rows={"truth_vault.external_notes": []},
                                      fail_table="truth_vault.external_notes"), "运动补给") is None


# ══════════════════════════════════════════════════════════════════════
# ⑧ 台账窗口 / 按 key 取 dims
# ══════════════════════════════════════════════════════════════════════

FOUR = {"emotional_lever": "焦虑撬动", "human_truth_archetype": "健康焦虑",
        "content_format": "提问求助", "title_structure": "疑问句"}


def _row(rid, *, drawn_days, consumed_days=None, project=PID, key="k" * 20, dims=None):
    return {"id": rid, "project_id": project, "angle_key": key, "dims": dims or dict(FOUR),
            "drawn_by": ME, "drawn_at": _ago(days=drawn_days),
            "consumed_version_id": f"v-{rid}" if consumed_days is not None else None,
            "consumed_at": _ago(days=consumed_days) if consumed_days is not None else None}


def test_ledger_window_includes_draws_in_window_and_late_consumptions():
    rows = [_row("a", drawn_days=1), _row("b", drawn_days=29),
            _row("c", drawn_days=40, consumed_days=5),          # 抽得早、写得近: 要
            _row("d", drawn_days=40, consumed_days=35),         # 都在窗口外: 不要
            _row("e", drawn_days=31),                            # 窗口外: 不要
            _row("f", drawn_days=2, project=OTHER)]              # 别的项目: 不要
    c = FakeClient(rows={"angle_ledger": rows})
    got = store.ledger_window(c, PID, 30)
    assert [r["id"] for r in got] == ["a", "b", "c"], "新抽的在前, 晚销账的接在后面"
    assert set(got[0]) == {"id", "angle_key", "dims", "drawn_by", "drawn_at", "consumed_at", "consumed_version_id"}
    assert all(("eq", "project_id", PID) in q["filters"] for q in _calls(c, "angle_ledger"))


def test_ledger_window_caps_and_warns(caplog):
    rows = [_row(f"r{i:03d}", drawn_days=i / 10) for i in range(40)]
    with caplog.at_level(logging.WARNING, logger="deskcore.store"):
        got = store.ledger_window(FakeClient(rows={"angle_ledger": rows}, max_rows=7), PID, 30, cap=25)
    assert len(got) == 25 and [r["id"] for r in got] == [f"r{i:03d}" for i in range(25)]
    assert "hit the cap" in caplog.text


def test_ledger_window_dedupes_rows_seen_twice():
    """翻页途中有人发牌, offset 错开一行时同一行会出现两次: 按 id 去重。"""
    rows = [_row("a", drawn_days=1), _row("c", drawn_days=40, consumed_days=1)]
    c = FakeClient(rows={"angle_ledger": rows})
    real = c._execute

    def doubled(q):
        res = real(q)
        if q.table_name == "angle_ledger" and res.data:
            res.data = res.data + res.data[:1]
        return res
    c._execute = doubled
    assert [r["id"] for r in store.ledger_window(c, PID, 30)] == ["a", "c"]


def test_ledger_window_failure_is_none():
    assert store.ledger_window(Flaky(rows={"angle_ledger": []}, fail_table="angle_ledger"), PID, 30) is None
    assert store.ledger_window(Flaky(rows={"angle_ledger": [_row("a", drawn_days=1)]}, fail_table="angle_ledger",
                                     fail_on=3), PID, 30) is None, "第二次查询(晚销账那撮)挂了也是 None"


def test_ledger_window_empty_is_empty_not_none():
    assert store.ledger_window(FakeClient(rows={"angle_ledger": []}), PID, 30) == []


def test_ledger_dims_for():
    key = angle_key(FOUR)
    newer = {**FOUR, "_draw": {"src": "x"}}
    rows = [_row("a", drawn_days=20, key=key), _row("b", drawn_days=2, key=key, dims=newer),
            _row("c", drawn_days=1, key="f" * 20, project=OTHER)]
    c = FakeClient(rows={"angle_ledger": rows})
    assert store.ledger_dims_for(c, PID, key) == FOUR
    assert store.ledger_dims_for(c, PID, "f" * 20) is None, "别的项目的 key 不算"
    assert store.ledger_dims_for(c, PID, "0" * 20) is None
    n = len(c.calls)
    for bad in ("", None, "k,ey", 'x"y', "a" * 65, "带中文"):
        assert store.ledger_dims_for(c, PID, bad) is None
    assert len(c.calls) == n, "形状不对的 key 不查库"
    assert store.ledger_dims_for(Flaky(rows={"angle_ledger": rows}, fail_table="angle_ledger"), PID, key) is None


# ══════════════════════════════════════════════════════════════════════
# ⑨ 写作台的稿子发出去了没有
# ══════════════════════════════════════════════════════════════════════

def _adoption_rows():
    links = [{"note_id": "t1", "version_id": "v1", "match_kind": "body_exact", "project_id": PID},
             {"note_id": "t2", "version_id": "v2", "match_kind": "fuzzy", "project_id": PID},
             {"note_id": "t3", "version_id": "v3", "match_kind": "edited", "project_id": PID},
             {"note_id": "t3b", "version_id": "v3", "match_kind": "title_exact", "project_id": PID},   # 同一版两条: 算一篇
             {"note_id": "t4", "version_id": "v4", "match_kind": "ingested", "project_id": PID},       # 抄回来的, 不算
             {"note_id": "t5", "version_id": "v5", "match_kind": "ambiguous", "project_id": PID}]      # 不知道是哪篇
    diffs = [{"note_id": "t1", "title_changed": True, "draft_chars": 300, "published_chars": 280,
              "published_title": "(不该取)"},
             {"note_id": "t2", "title_changed": False, "draft_chars": 200, "published_chars": 199}]
    return {"tv_note_links": links, "tv_edit_diffs": diffs}


def test_desk_adoption_counts_published_and_retitled_versions():
    c = FakeClient(rows=_adoption_rows())
    got = store.desk_adoption(c, ["v1", "v2", "v3", "v4", "v5", "v6", "v1", None])
    assert got["versions"] == 6 and got["published"] == 3 and got["title_changed"] == 1
    assert set(got["by_version"]) == {"v1", "v2", "v3"}
    assert got["by_version"]["v1"] == {"note_id": "t1", "match_kind": "body_exact", "title_changed": True,
                                       "draft_chars": 300, "published_chars": 280}
    assert got["by_version"]["v3"]["title_changed"] is None, "还没算差异: 不知道, 不当成没改"
    assert not any(col in (q["cols"] or "") for q in c.calls for col in ("published_title", "draft_title")), \
        "只数, 不取标题"


def test_desk_adoption_survives_a_missing_diff_table():
    c = Flaky(rows=_adoption_rows(), fail_table="tv_edit_diffs")
    got = store.desk_adoption(c, ["v1", "v2", "v3"])
    assert got["published"] == 3 and got["title_changed"] == 0
    assert all(v["title_changed"] is None for v in got["by_version"].values())


def test_desk_adoption_links_failure_is_none_and_empty_is_zero():
    assert store.desk_adoption(Flaky(rows=_adoption_rows(), fail_table="tv_note_links"), ["v1"]) is None
    c = FakeClient(rows={})
    assert store.desk_adoption(c, []) == {"versions": 0, "published": 0, "title_changed": 0, "by_version": {}}
    assert not c.calls


def test_desk_adoption_chunks_version_ids():
    vids = [f"v{i:03d}" for i in range(250)]
    links = [{"note_id": f"t{i:03d}", "version_id": v, "match_kind": "body_exact"} for i, v in enumerate(vids)]
    c = FakeClient(rows={"tv_note_links": links, "tv_edit_diffs": []})
    got = store.desk_adoption(c, vids)
    assert got["published"] == 250
    for q in c.calls:
        for f in q["filters"]:
            if f[0] == "in" and f[1] in ("version_id", "note_id"):
                assert len(f[2]) <= store.NOTE_ANSWERS_CHUNK


# ══════════════════════════════════════════════════════════════════════
# recent_titles: 多取 comments_count / tier, 可选的 working 一栏
# ══════════════════════════════════════════════════════════════════════

class _Titles:
    def __init__(self):
        self.notes, self.links, self.versions, self.batches, self.items = [], [], [], [], []

    def post(self, nid, title, *, days, comments=None, tier=None, kind="body_exact"):
        vid = f"v-{nid}"
        self.versions.append({"id": vid, "title": title})
        self.notes.append({"note_id": nid, "project_id": "AAA_phase1", "publish_time": _ago(days=days),
                           "comments_count": comments, "tier": tier, "raw_content": "(不该取)"})
        self.links.append({"note_id": nid, "tv_project_id": "AAA_phase1", "project_id": PID, "version_id": vid,
                           "match_kind": kind, "matched_at": _ago(hours=len(self.links))})

    def draft(self, bid, titles, *, days=1):
        self.batches.append({"id": bid, "project_id": PID, "params": {"source": "deskcore"}, "created_at": _ago(days=days)})
        for i, t in enumerate(titles):
            iid = f"{bid}-i{i}"
            self.items.append({"id": iid, "batch_id": bid, "best_version_id": f"{iid}-v1",
                               "created_at": _ago(days=days, seconds=i),
                               "versions": [{"id": f"{iid}-v1", "title": t, "version_num": 1}]})

    def client(self, cls=FakeClient, **kw):
        return cls(rows={"truth_vault.notes": self.notes, "tv_note_links": self.links, "tv_edit_diffs": [],
                         "versions": self.versions, "batches": self.batches, "items": self.items}, **kw)


def test_recent_titles_shape_is_unchanged_without_working():
    w = _Titles()
    w.post("n1", "评论很多的老帖", days=10, comments=99, tier="爆")
    got = store.recent_titles(w.client(), PID)
    assert set(got) == {"published", "drafted"}


def test_working_titles_are_older_than_three_days_with_comments_or_a_hit():
    w = _Titles()
    w.post("fresh", "两天前发的评论很多", days=2, comments=80)                # 不到三天: 不算
    w.post("few", "评论十九条", days=6, comments=19)                         # 不够 20: 不算
    w.post("none", "评论数没有", days=6, comments=None)
    w.post("c20", "评论二十条", days=5, comments=20)
    w.post("c50", "评论五十条", days=8, comments=50)
    w.post("hit", "标了爆的那篇", days=12, comments=3, tier="爆")             # 爆排最前
    w.post("big", "标了大爆的那篇", days=20, comments=None, tier="大爆")
    w.post("old", "窗口外的爆款", days=40, comments=500, tier="大爆")         # 30 天外: 不算
    w.post("long", "这是一个特别长的标题" * 6, days=9, comments=30)
    got = store.recent_titles(w.client(), PID, working=5)
    assert got["working"] == ["标了爆的那篇", "标了大爆的那篇", "评论五十条",
                              ("这是一个特别长的标题" * 6)[:store.RECENT_TITLE_CHARS], "评论二十条"]
    assert all(len(t) <= store.RECENT_TITLE_CHARS for t in got["working"])
    assert "两天前发的评论很多" in got["published"], "published 那一栏照旧"


def test_working_reaches_past_the_published_cap():
    """活跃项目里 published 只装得下最近几天的; 三天以上、评论多的那几篇照样进 working。"""
    w = _Titles()
    for i in range(40):
        w.post(f"n{i:02d}", f"第{i}篇", days=0.2 + i * 0.5, comments=25 if i == 30 else 1)
    got = store.recent_titles(w.client(), PID, limit=20, working=5)
    assert "第30篇" not in got["published"] and got["working"] == ["第30篇"]


def test_drafted_does_not_repeat_a_working_title():
    w = _Titles()
    w.post("hit", "那篇评论多的", days=6, comments=60)
    for i in range(25):
        w.post(f"n{i:02d}", f"最近的第{i}篇", days=0.1 + i * 0.1)
    w.draft("b1", ["那篇评论多的", "昨天写的"])
    got = store.recent_titles(w.client(), PID, limit=20, working=5)
    assert got["working"] == ["那篇评论多的"]
    assert got["drafted"] == ["昨天写的"]


def test_working_is_empty_when_truth_vault_is_unreachable():
    w = _Titles()
    w.post("hit", "那篇评论多的", days=6, comments=60)
    w.draft("b1", ["写过的"])
    c = w.client(Flaky, fail_table="truth_vault.notes")
    assert store.recent_titles(c, PID, working=5) == {"published": [], "drafted": ["写过的"], "working": []}
