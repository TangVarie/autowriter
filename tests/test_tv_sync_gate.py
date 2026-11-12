"""回流闸 v1 的接线: tv-sync 的最后一步(core._tv_gate_step)怎么读、怎么判、怎么写。

判法本身在 tests/test_postgate.py。这里盯的是这一步的四条纪律:
  · 排在最后、出什么错都不改 tv_sync 的结果;
  · migrations/020 没跑(读不到那四列)整步跳过, dry-run 不跑;
  · 只 UPDATE 回流闸那四列, 值一样的行一次写, 结论没变的只刷新 gate_at; 不 upsert, 不碰对照本身的列;
  · 飞书里删了的缺席满宽限才记 withdrawn, 不满的留着上一晚的结论。
假库同 tests/test_tv_sync.py, 再加 TV 那边闸要读的四样: truth_vault.notes 的状态列、note_feature_answers、
v_paid_boost_events、v_notes_vanished。文字全是编的。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from deskcore import cli, core, postgate, priors, store, tvlink
from tests import test_tv_sync as T
from tests.fakes import FakeClient

TV, P1, ME = T.TV, T.P1, T.ME
GATE_COLS = set(store.TV_GATE_COLS)
NIGHT = [T.T0 + timedelta(days=10)]                     # 这一晚的「现在」; 用例里往后拨就是下一晚
BODY_C = ("第二篇稿子讲的是另一件事: 面试前一晚把自我介绍念给室友听, 室友听到第三句就睡着了。"
          "第二天我把前三句删了, 面试官第一次在我说完之前就开始点头。")
BODY_D = ("周末第一次去打网球, 教练让我先对着墙打两百个正手, 打到一半手腕就酸得抬不起来。"
          "回家查了才知道握拍太紧, 第二周换了松一点的握法, 墙打到三百个也不累。")
# 同 tests/test_tv_sync.py 的三条(对上写作台的稿 / 库里没有 → 补录 / TV 自己带了认不出的血缘), 第三条的正文长一些
NOTES = [T.NOTES[0], T.NOTES[1],
         T._note("n3", "TV 自己带了 lineage", "随便什么正文, 反正 TV 已经知道它是哪一版。" * 3,
                 source_autowriter_version_id="vvvvvvvv-0000-0000-0000-00000000000v")]
BODY_E = ("搬家那天才发现书比衣服多三倍, 纸箱在楼道里排到了电梯口, 邻居以为我要开书店。"
          "最后留下的不到一半, 剩下的送给了楼下的旧书摊, 摊主回了我一袋橘子。")


@pytest.fixture(autouse=True)
def _fixed(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    NIGHT[0] = T.T0 + timedelta(days=10)
    monkeypatch.setattr(core, "_gate_now", lambda: NIGHT[0])


def _fp(note_row: dict) -> str:
    """这篇帖现在的文字指纹(量重合时记在 gate_reasons.overlap_fp 里的那个)。"""
    return tvlink.text_fp(tvlink.Note.from_row(note_row))


def _next_night(days: float = 1.0) -> None:
    NIGHT[0] = NIGHT[0] + timedelta(days=days)


def _tv_row(n: dict, **extra) -> dict:
    """TV 里的那一行, 只有闸读的几列(没有标题和正文)。"""
    return {"note_id": n["note_id"], "project_id": TV, "publish_time": n.get("publish_time"),
            "tier": n.get("tier"), "data_quality_flags": n.get("flags"), **extra}


def _answer(nid, ans="否", at="2026-09-13T20:00:00+00:00"):
    return {"subject_type": "note", "run_tag": "primary", "question_id": "efficacy_promise", "subject_id": nid,
            "answer": ans, "extracted_at": at, "question_version": 1, "extractor": "jev"}


def _tv(notes, *, links=None, with_target=True, unanswered=(), promise=(), boosted=(), missing_columns=None):
    """``notes`` 是 tv-sync 读到的 TV(list, 用例里可以改); truth_vault.notes 另放一份状态列。"""
    c = T._client(notes, with_target=with_target, links=links)
    if missing_columns:
        c.missing_columns = {"tv_note_links": set(missing_columns)}
    c.rows["truth_vault.notes"] = [_tv_row(n) for n in notes]
    c.rows["truth_vault.note_feature_answers"] = [
        _answer(n["note_id"], "是" if n["note_id"] in promise else "否")
        for n in notes if n["note_id"] not in unanswered]
    c.rows["truth_vault.v_paid_boost_events"] = [{"note_id": nid} for nid in boosted]
    c.rows["truth_vault.v_notes_vanished"] = []
    return c


def _links(c) -> dict[str, dict]:
    return {l["note_id"]: l for l in c.rows["tv_note_links"]}


def _gate(c) -> dict[str, tuple]:
    return {nid: (l.get("origin"), l.get("gate"), tuple((l.get("gate_reasons") or {}).get("reasons", ())))
            for nid, l in _links(c).items()}


def _gate_writes(c, since: int = 0) -> list[dict]:
    """这一步对 tv_note_links 发的写(负载里带回流闸那几列的)。"""
    out = []
    for call in c.calls[since:]:
        if call["table"] != "tv_note_links" or call["op"] == "select":
            continue
        rows = call["payload"] if isinstance(call["payload"], list) else [call["payload"]]
        if any(GATE_COLS & set(r) for r in rows if isinstance(r, dict)):
            out.append(call)
    return out


def _vanish(c, notes, nid, *, hours):
    """飞书里删了 nid: tv-sync 这晚读不到它(016), TV 那一行还在, 进了 v_notes_vanished, 缺席 ``hours`` 小时。"""
    notes[:] = [n for n in notes if n["note_id"] != nid]
    seen = NIGHT[0] - timedelta(hours=hours)
    c.rows["truth_vault.v_notes_vanished"] = [r for r in c.rows["truth_vault.v_notes_vanished"] if r["note_id"] != nid]
    c.rows["truth_vault.v_notes_vanished"].append(
        {"note_id": nid, "project_id": TV, "last_seen_at": seen.isoformat(), "latest_seen_at": NIGHT[0].isoformat()})


# ── 第一晚 / 第二晚 ─────────────────────────────────────────────────────────────────────

def test_the_first_night_judges_every_link_and_the_result_carries_counts_only():
    c = _tv(list(NOTES))
    out = core.tv_sync(c, TV)
    assert _gate(c) == {"n1": ("desk", "pass", ()),          # 开头对上写作台的稿, 写在发布之前
                        "n2": ("elsewhere", "pass", ()),     # 对不上, 补录的: 和发布前写作台里的文字没有重合
                        "n3": ("unclear", "pass", ())}       # TV 带的血缘指到认不出的版本
    assert _links(c)["n2"]["gate_reasons"] == {"reasons": [], "overlap": 0.0, "overlap_fp": _fp(NOTES[1])}
    assert _links(c)["n1"]["gate_reasons"] == {"reasons": []}, "对上了的行不记重合"
    at = {l["gate_at"] for l in _links(c).values()}
    assert at == {NIGHT[0].isoformat()}
    g = out["gate"]
    assert g["applied"] is True and g["error"] is None and g["partial"] is False
    assert (g["scope"], g["judged"], g["changed"], g["touched"]) == (3, 3, 3, 0)
    assert g["counts"] == {"desk": {"pass": 1}, "elsewhere": {"pass": 1}, "unclear": {"pass": 1}}
    assert g["overlap_measured"] == 1
    # 结果里只有数: 没有 note_id, 没有文字
    text = json.dumps(g, ensure_ascii=False)
    assert not re.search(r"n[123]\b", text) and "秋招" not in text and "上周" not in text


def test_the_second_night_writes_nothing_but_gate_at_moves_on_every_row_in_scope():
    c = _tv(list(NOTES))
    core.tv_sync(c, TV)
    before = {nid: dict(l) for nid, l in _links(c).items()}
    mark = len(c.calls)
    _next_night()
    out = core.tv_sync(c, TV)
    g = out["gate"]
    assert (g["scope"], g["judged"], g["changed"], g["touched"]) == (3, 3, 0, 3)
    writes = _gate_writes(c, mark)
    assert [w["payload"] for w in writes] == [{"gate_at": NIGHT[0].isoformat()}], "三行结论都没变: 一次 update, 只带 gate_at"
    assert writes[0]["op"] == "update"
    after = _links(c)
    for nid in ("n1", "n2", "n3"):
        assert after[nid]["gate_at"] == NIGHT[0].isoformat() != before[nid]["gate_at"]
        # n3 没有版本, tv-sync 自己每晚重对、重写一遍(updated_at 是它刷的, 不是闸); 别的列和另两行整行都没动
        skip = {"gate_at"} | ({"updated_at"} if nid == "n3" else set())
        assert {k: v for k, v in after[nid].items() if k not in skip} == \
               {k: v for k, v in before[nid].items() if k not in skip}


def test_only_the_rows_whose_verdict_changed_are_rewritten():
    c = _tv(list(NOTES))
    core.tv_sync(c, TV)
    mark = len(c.calls)
    _next_night()
    c.rows["truth_vault.note_feature_answers"].append(_answer("n1", "是", at="2026-09-21T20:00:00+00:00"))
    out = core.tv_sync(c, TV)
    assert (out["gate"]["changed"], out["gate"]["touched"]) == (1, 2)
    assert _gate(c)["n1"] == ("desk", "fail", ("efficacy",))
    writes = _gate_writes(c, mark)
    full = [w for w in writes if set(w["payload"]) == GATE_COLS]
    touch = [w for w in writes if set(w["payload"]) == {"gate_at"}]
    assert len(full) == 1 and len(touch) == 1 and len(writes) == 2
    assert full[0]["filters"] == [("in", "note_id", ("n1",))]
    assert set(touch[0]["filters"][0][2]) == {"n2", "n3"}


def test_unchanged_rows_are_refreshed_in_one_update_and_measured_rows_carry_their_own_fingerprint():
    notes = [T._note(f"m{i}", f"别处写的第 {i} 篇", BODY_D + f"第 {i} 篇多写的一句话, 各不相同的第 {i * 7} 个细节。")
             for i in range(5)]
    c = _tv(notes, with_target=False)
    core.tv_sync(c, TV)
    writes = _gate_writes(c)
    # 对不上的行各带着量重合那一刻的文字指纹(codex review on aw#155), 第一晚一行一次; 之后结论没变的只刷新时间, 合成一次
    assert len(writes) == 5 and sorted(w["filters"][0][2][0] for w in writes) == [f"m{i}" for i in range(5)]
    mark = len(c.calls)
    _next_night()
    core.tv_sync(c, TV)
    again = _gate_writes(c, mark)
    assert len(again) == 1 and set(again[0]["payload"]) == {"gate_at"} and len(again[0]["filters"][0][2]) == 5
    first_at = (NIGHT[0] - timedelta(days=1)).isoformat()
    by_note = {w["filters"][0][2][0]: w["payload"] for w in writes}
    assert by_note["m0"] == {"origin": "elsewhere", "gate": "pass", "gate_at": first_at,
                             "gate_reasons": {"reasons": [], "overlap": 0.0, "overlap_fp": _fp(notes[0])}}


# ── 只写那四列 ──────────────────────────────────────────────────────────────────────────

def test_gate_writes_are_targeted_updates_of_the_gate_columns_and_never_an_upsert():
    c = _tv(list(NOTES))
    core.tv_sync(c, TV)
    _next_night()
    c.rows["truth_vault.notes"][0]["tier"] = "风控"
    core.tv_sync(c, TV)
    writes = _gate_writes(c)
    assert writes and all(w["op"] == "update" for w in writes), "从不 upsert / insert"
    for w in writes:
        assert set(w["payload"]) <= GATE_COLS, w["payload"]
        assert [f[:2] for f in w["filters"]] == [("in", "note_id")], "只按 note_id 圈行"
    # 反过来: tv-sync 自己写对照的那几笔里不带回流闸的列(upsert 只带对照的列, 不会把结论冲成 NULL)
    for call in c.calls:
        if call["table"] == "tv_note_links" and call["op"] == "upsert":
            assert not any(GATE_COLS & set(r) for r in call["payload"])


def test_pinned_rows_keep_every_match_field():
    """补录的(ingested)、人定过的(candidates 里有 D-xxx)对照是钉住的: 闸给它们记结论, 对照本身的列一个不动。"""
    old = "2026-09-01T00:00:00+00:00"
    pinned = {"note_id": "n1", "tv_project_id": TV, "project_id": P1, "version_id": "v1", "item_id": "i1",
              "match_kind": "body_exact", "score": 1.0, "lag_days": 2, "updated_at": old, "matched_at": old,
              "synced_to_tv_at": old, "tv_text_fp": None, "candidates": [{"note": "D-129 2026-10-26: 人定的"}]}
    copy = {"note_id": "n2", "tv_project_id": TV, "project_id": P1, "version_id": "vcopy", "item_id": "icopy",
            "match_kind": "ingested", "score": None, "lag_days": None, "updated_at": old, "matched_at": old,
            "synced_to_tv_at": None, "tv_text_fp": None, "candidates": None}
    notes = [T._note("n1", "秋招投了六十份简历没回音", T.BODY_A), T._note("n2", "上周发的那篇", T.BODY_NEW)]
    c = _tv(notes, links=[dict(pinned), dict(copy)])
    out = core.tv_sync(c, TV)
    assert out["counts"]["already_linked"] == 2 and out["gate"]["changed"] == 2
    got = _links(c)
    for nid, was in (("n1", pinned), ("n2", copy)):
        assert {k: v for k, v in got[nid].items() if k not in GATE_COLS} == was, nid
    assert _gate(c) == {"n1": ("desk", "pass", ()), "n2": ("elsewhere", "pass", ())}
    assert got["n2"]["score"] is None, "每晚的同步不给钉住的对照补分数(那是 --rescore-misses 的事)"


# ── 每一档 ──────────────────────────────────────────────────────────────────────────────

def test_every_class_from_the_inputs_the_step_reads():
    notes = [
        T._note("a_pass", "普通的一篇", BODY_C),
        T._note("b_paid", "投了流的一篇", BODY_D, flags={"paid_promoted": True}),
        T._note("c_boost", "投流事件里的一篇", BODY_E),
        T._note("d_risk", "风控的一篇", BODY_C + "多一句让它和上面那篇不一样的话, 这样指纹不会撞。", tier="风控"),
        T._note("e_promise", "承诺功效的一篇", BODY_D + "多一句让它和上面那篇不一样的话, 这样指纹不会撞。"),
        T._note("f_unanswered", "还没答题的一篇", BODY_E + "多一句让它和上面那篇不一样的话, 这样指纹不会撞。"),
        T._note("g_empty", "正文还是空的一篇", ""),
        T._note("h_future", "排期在后面的一篇", BODY_C + "这一篇排在十天以后才发, 现在只是先填进了表里。", days=20),
        T._note("i_undated", "还没填发布日期的一篇", BODY_D + "这一篇还没有填发布日期, 表里那一格是空的。", publish_time=None),
        T._note("j_synthetic", "合成的一篇", BODY_E + "这一篇是合成的测试数据, 不是真发出去的帖。", flags={"synthetic": True}),
        T._note("k_short", "正文只有一句的一篇", "就这么一句。"),          # 发出去的短帖: 有字, 走后面三步
    ]
    c = _tv(notes, with_target=False, unanswered={"f_unanswered"}, promise={"e_promise"}, boosted={"c_boost"})
    out = core.tv_sync(c, TV)
    got = {nid: v[1:] for nid, v in _gate(c).items()}
    assert got == {"a_pass": ("pass", ()), "b_paid": ("fail", ("not_clean",)), "c_boost": ("fail", ("not_clean",)),
                   "d_risk": ("fail", ("risk",)), "e_promise": ("fail", ("efficacy",)),
                   "f_unanswered": ("unsure", ("unsure",)), "g_empty": ("hold", ("hold",)),
                   "h_future": ("hold", ("hold",)), "i_undated": ("hold", ("hold",)), "j_synthetic": ("hold", ("hold",)),
                   "k_short": ("pass", ())}
    hold = {nid: l["gate_reasons"].get("hold") for nid, l in _links(c).items() if l["gate"] == "hold"}
    assert hold == {"g_empty": ["short_body"], "h_future": ["scheduled"], "i_undated": ["no_publish_time"],
                    "j_synthetic": ["synthetic"]}
    assert sum(n for per in out["gate"]["counts"].values() for n in per.values()) == 11
    # 前两步就定了的(hold)不去读功效题的答案
    asked = {nid for call in c.calls if call["table"] == "truth_vault.note_feature_answers"
             for f in call["filters"] if f[:2] == ("in", "subject_id") for nid in f[2]}
    assert asked == {"a_pass", "b_paid", "c_boost", "d_risk", "e_promise", "f_unanswered", "k_short"}
    # 干不干净和 priors.is_clean 是同一把尺(同一行、同一个投流事件集合)
    boosted = priors.paid_boosted(c)
    for row in c.rows["truth_vault.notes"]:
        if _links(c)[row["note_id"]]["gate"] in ("pass", "fail", "unsure"):
            assert ("not_clean" in _gate(c)[row["note_id"]][2]) == (not priors.is_clean(row, boosted)), row["note_id"]


def test_body_length_comes_from_the_text_tv_sync_parsed_and_the_gate_reads_no_text_from_tv():
    """decisions 第 2 条: truth_vault.notes.body 在近期的帖上全是 NULL; 正文长短看 tv-sync 从 raw_content 拆出来的那段。"""
    notes = [T._note("n1", "正文在 raw_content 里", BODY_D), T._note("n2", "只有话题", "#跑步 #能量胶 #周末")]
    c = _tv(notes, with_target=False)
    for r in c.rows["truth_vault.notes"]:
        r["body"] = None                                 # 生产库的样子
    core.tv_sync(c, TV)
    assert _gate(c)["n1"][1] == "pass" and _gate(c)["n2"][1] == "hold"
    reads = [call for call in c.calls if call["table"] == "truth_vault.notes" and call["op"] == "select"]
    assert reads and all(call["cols"] == store.TV_GATE_FLAG_COLS for call in reads), "不取标题、正文、raw_content"


def test_reasons_are_stored_as_names_only():
    notes = [T._note("n1", "承诺功效还投了流的一篇", BODY_C, flags={"paid_promoted": True}),
             T._note("n2", "正文还是空的一篇", ""), T._note("n3", "普通的一篇", BODY_D)]
    c = _tv(notes, with_target=False, promise={"n1"})
    core.tv_sync(c, TV)
    names = set(postgate.REASONS) | set(postgate.HOLD_DETAILS)
    for nid, l in _links(c).items():
        stored = l["gate_reasons"]
        assert set(stored) <= {"reasons", "hold", "overlap", "overlap_fp", "overlap_pending", "score_stale"}, stored
        assert "overlap_fp" not in stored or re.fullmatch(r"[0-9a-f]{16}", stored["overlap_fp"]), "指纹, 不是文字"
        assert all(isinstance(r, str) and r in names for r in stored["reasons"] + stored.get("hold", []))
        assert "overlap" not in stored or isinstance(stored["overlap"], float)
        assert l["origin"] in postgate.ORIGINS and l["gate"] in postgate.GATES
        text = json.dumps({k: l[k] for k in GATE_COLS}, ensure_ascii=False)
        for n in notes:
            title = re.search(r"【标题】(.*?) 【正文】", n["raw_content"]).group(1)
            assert title not in text and n["raw_content"][-30:] not in text
    assert _gate(c)["n1"][1:] == ("fail", ("not_clean", "efficacy"))
    assert _links(c)["n2"]["gate_reasons"]["hold"] == ["short_body"]


def test_duplicate_texts_are_judged_row_by_row():
    """decisions 第 6 条: 同一段文字挂在几条笔记上(父记录继承正文、交叉发), 每一行按它自己那篇笔记判。"""
    notes = [T._note("n1", "同一段正文的第一条", BODY_D), T._note("n2", "同一段正文的第二条", BODY_D)]
    c = _tv(notes, promise={"n2"})
    core.tv_sync(c, TV)
    links = _links(c)
    assert links["n1"]["version_id"] == links["n2"]["version_id"], "两条笔记连着同一份补录副本"
    assert _gate(c)["n1"][1:] == ("pass", ()) and _gate(c)["n2"][1:] == ("fail", ("efficacy",))


# ── 飞书里删了的 ────────────────────────────────────────────────────────────────────────

def test_a_note_deleted_from_the_sheet_becomes_withdrawn_after_the_grace():
    notes = list(NOTES)
    c = _tv(notes)
    core.tv_sync(c, TV)
    assert _gate(c)["n2"] == ("elsewhere", "pass", ())
    _next_night(2)
    _vanish(c, notes, "n2", hours=48)
    out = core.tv_sync(c, TV)
    assert _gate(c)["n2"] == ("elsewhere", "withdrawn", ("withdrawn",)), "来源留着, 结论改成 withdrawn"
    assert _links(c)["n2"]["gate_reasons"] == {"reasons": ["withdrawn"], "overlap": 0.0, "overlap_fp": _fp(NOTES[1])}
    assert _links(c)["n2"]["match_kind"] == "ingested" and _links(c)["n2"]["version_id"], "对照(和查重里的副本)不动"
    assert out["gate"]["counts"]["elsewhere"] == {"withdrawn": 1} and out["gate"]["kept_in_grace"] == 0


def test_a_note_that_vanished_inside_the_grace_keeps_its_class():
    """decisions 第 1 条: 缺席不满 36 小时的(TV 一次同步写坏, 活着的帖也会短暂进视图)留着上一晚的结论, 只刷新时间。"""
    notes = list(NOTES)
    c = _tv(notes)
    core.tv_sync(c, TV)
    first = dict(_links(c)["n2"])
    _next_night()
    _vanish(c, notes, "n2", hours=10)
    assert timedelta(hours=10) < core.TV_VANISHED_GRACE
    out = core.tv_sync(c, TV)
    row = _links(c)["n2"]
    assert (row["origin"], row["gate"], row["gate_reasons"]) == (first["origin"], "pass", first["gate_reasons"])
    assert row["gate_at"] == NIGHT[0].isoformat() != first["gate_at"], "这晚看过它"
    assert out["gate"]["kept_in_grace"] == 1 and out["gate"]["judged"] == 2 and out["gate"]["changed"] == 0
    # 再过一天还没回来: 缺口过了宽限, 这才 withdrawn
    _next_night()
    _vanish(c, notes, "n2", hours=34 + 3)
    core.tv_sync(c, TV)
    assert _gate(c)["n2"][1] == "withdrawn"
    # 帖又回来了(运营把它挪回来): 这晚读到了, 照常重判 —— 哪怕它已经发出去很久
    _next_night(90)
    notes.append(NOTES[1])
    c.rows["truth_vault.v_notes_vanished"] = []
    core.tv_sync(c, TV)
    assert _gate(c)["n2"] == ("elsewhere", "pass", ())


def test_a_whole_project_vanishing_for_one_night_marks_nothing_withdrawn():
    """TV 一次同步坏了, 整个项目的帖这晚都读不到、都进了视图(缺口不到一天): 没有一行变 withdrawn。"""
    notes = list(NOTES)
    c = _tv(notes)
    core.tv_sync(c, TV)
    before = _gate(c)
    _next_night()
    for nid in ("n1", "n2", "n3"):
        _vanish(c, notes, nid, hours=20)
    out = core.tv_sync(c, TV)
    assert _gate(c) == before and out["gate"]["kept_in_grace"] == 3 and out["gate"]["judged"] == 0


def test_a_never_judged_note_inside_the_grace_stays_without_a_verdict():
    link = {"note_id": "n2", "tv_project_id": TV, "project_id": P1, "version_id": "vcopy", "item_id": None,
            "match_kind": "ingested", "score": None, "lag_days": None, "candidates": None, "tv_text_fp": None}
    notes = [T._note("n2", "上周发的那篇", T.BODY_NEW)]
    c = _tv(notes, links=[dict(link)])
    _vanish(c, notes, "n2", hours=5)
    out = core.tv_sync(c, TV)
    assert _links(c)["n2"].get("gate") is None and _links(c)["n2"].get("gate_at") is None
    assert out["gate"]["unread"] == 1 and out["gate"]["changed"] == out["gate"]["touched"] == 0


def test_when_the_vanished_view_cannot_be_read_unread_rows_are_left_alone(monkeypatch):
    notes = list(NOTES)
    c = _tv(notes)
    core.tv_sync(c, TV)
    before = dict(_links(c)["n2"])
    _next_night(3)
    _vanish(c, notes, "n2", hours=72)
    monkeypatch.setattr(store, "tv_long_vanished_note_ids",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("view down")))
    out = core.tv_sync(c, TV)
    assert _links(c)["n2"] == before, "查不了删没删: 不动, 不猜"
    assert "view down" in out["gate"]["vanished_error"] and out["gate"]["judged"] == 2 and out["gate"]["unread"] == 1


def test_a_database_without_the_vanished_view_is_not_an_error(monkeypatch):
    """新库 / TV 还没有那张视图(PGRST205): 没有「飞书里删了」这回事, 没读到的行不动, 这一步不报错。"""
    notes = list(NOTES)
    c = _tv(notes)
    core.tv_sync(c, TV)
    before = dict(_links(c)["n2"])
    _next_night()
    notes[:] = [n for n in notes if n["note_id"] != "n2"]
    gone = RuntimeError("{'code': 'PGRST205', 'message': \"Could not find the table 'truth_vault.v_notes_vanished' "
                        "in the schema cache\"}")
    real = c._execute

    def execute(q):
        if q.table_name == "truth_vault.v_notes_vanished":
            raise gone
        return real(q)
    monkeypatch.setattr(c, "_execute", execute)
    out = core.tv_sync(c, TV)
    assert out["gate"]["vanished_error"] is None and out["gate"]["error"] is None
    assert out["gate"]["unread"] == 1 and _links(c)["n2"] == before


def test_a_note_outside_the_since_window_is_left_alone():
    """--since 只读新进来的笔记: 没读到、又不在已删视图里的行不判也不动(没有正文可看)。"""
    c = _tv(list(NOTES))
    core.tv_sync(c, TV)
    before = {nid: dict(l) for nid, l in _links(c).items()}
    _next_night()
    c.rpc_impl["deskcore_tv_notes"] = lambda a: []        # 这晚 TV 一条都没读回来
    out = core.tv_sync(c, TV, since="2026-09-20T00:00:00+00:00")
    assert _links(c) == before and out["gate"]["unread"] == 3 and out["gate"]["judged"] == 0


# ── 范围 ────────────────────────────────────────────────────────────────────────────────

def test_scope_is_recent_posts_plus_never_judged_plus_pending_ones():
    notes = [T._note("old_pass", "很久以前的一篇", BODY_C, days=-100),
             T._note("old_unsure", "很久以前、还没答题的一篇", BODY_D, days=-100),
             T._note("new", "刚发的一篇", BODY_E, days=5)]
    c = _tv(notes, with_target=False, unanswered={"old_unsure"})
    out = core.tv_sync(c, TV)
    assert out["gate"]["scope"] == 3, "从没判过的不管多老都判(跑完 020 的第一晚就是全部对照)"
    first = {nid: l["gate_at"] for nid, l in _links(c).items()}
    assert _gate(c)["old_unsure"][1] == "unsure"

    _next_night()
    c.rows["truth_vault.note_feature_answers"].append(_answer("old_unsure"))
    out = core.tv_sync(c, TV)
    assert out["gate"]["scope"] == 2
    assert _links(c)["old_pass"]["gate_at"] == first["old_pass"], "发布超过 60 天、已经有定论的不再看"
    assert _gate(c)["old_unsure"][1] == "pass", "等答案的不管多老每晚重判, 答案到了就定"
    assert _links(c)["new"]["gate_at"] == NIGHT[0].isoformat()

    _next_night()
    out = core.tv_sync(c, TV)
    assert out["gate"]["scope"] == 1, "old_unsure 定了以后也出了范围"
    out = core.tv_sync(c, TV, gate_all=True)
    assert out["gate"]["scope"] == 3 and out["gate"]["touched"] == 3
    assert _links(c)["old_pass"]["gate_at"] == NIGHT[0].isoformat()


def test_an_old_settled_post_deleted_from_the_sheet_still_becomes_withdrawn():
    """删了的帖不管多老都要记 withdrawn: 教训卡不看帖龄(书架上多数卡的帖早过了 60 天), 一篇有定论的老帖哪天从飞书里
    删了, 这晚没读到它 —— 没读到的行不看发布时间, 一律进范围去问 v_notes_vanished。"""
    notes = [T._note("old", "很久以前的一篇", BODY_C, days=-80), T._note("new", "刚发的一篇", BODY_E, days=8),
             T._note("old_kept", "很久以前、一直在的一篇", BODY_D, days=-80)]
    c = _tv(notes, with_target=False)
    core.tv_sync(c, TV)
    assert {nid: v[1] for nid, v in _gate(c).items()} == {"old": "pass", "new": "pass", "old_kept": "pass"}
    first = {nid: l["gate_at"] for nid, l in _links(c).items()}

    # 刚不见(还在宽限里): 留着上一晚的结论, 只刷新时间
    _next_night()
    _vanish(c, notes, "old", hours=10)
    out = core.tv_sync(c, TV)
    assert out["gate"]["scope"] == 2 and out["gate"]["kept_in_grace"] == 1
    assert _gate(c)["old"][1] == "pass" and _links(c)["old"]["gate_at"] == NIGHT[0].isoformat()

    # 缺席满了宽限: withdrawn, 和刚发的帖删了一样
    _next_night(3)
    _vanish(c, notes, "old", hours=72)
    _vanish(c, notes, "new", hours=72)
    out = core.tv_sync(c, TV)
    assert _gate(c)["old"] == ("elsewhere", "withdrawn", ("withdrawn",))
    assert _gate(c)["new"] == ("elsewhere", "withdrawn", ("withdrawn",))
    assert postgate.blocked(_links(c)["old"], postgate.BLOCKS_CARDS)
    assert out["gate"]["scope"] == 2 and out["gate"]["changed"] == 2
    assert _links(c)["old_kept"]["gate_at"] == first["old_kept"], "这晚读到了的老帖、有定论的照旧不看"

    # 记过 withdrawn 的老帖之后不再每晚去问
    _next_night()
    mark = len(c.calls)
    out = core.tv_sync(c, TV)
    assert out["gate"]["scope"] == 1, "只剩那篇近 60 天里发的(withdrawn 了也还在窗口里)"
    asked = {nid for call in c.calls[mark:] if call["table"] == "truth_vault.v_notes_vanished"
             for f in call["filters"] if f[:2] == ("in", "note_id") for nid in f[2]}
    assert "old" not in asked


def test_an_old_settled_post_that_was_not_read_and_is_not_deleted_is_left_alone():
    """--since 只读新进来的笔记: 没读到的老帖也进范围去问删没删, 不在已删视图里的一行不动(只是数进「没读到」)。"""
    notes = [T._note("old", "很久以前的一篇", BODY_C, days=-80)]
    c = _tv(notes, with_target=False)
    core.tv_sync(c, TV)
    before = dict(_links(c)["old"])
    mark = len(c.calls)
    _next_night()
    c.rpc_impl["deskcore_tv_notes"] = lambda a: []
    out = core.tv_sync(c, TV, since="2026-09-20T00:00:00+00:00")
    assert _links(c)["old"] == before and not _gate_writes(c, mark)
    assert (out["gate"]["scope"], out["gate"]["unread"], out["gate"]["judged"]) == (1, 1, 0)


def test_a_fail_row_still_waiting_for_the_efficacy_answer_is_rejudged_at_any_age():
    """投了流、功效题又还没答的帖是 fail, 原因 not_clean + unsure。教训卡不看 not_clean, 靠 unsure 在答案到之前不借它 ——
    所以这一行也是「还在等」的: 不管帖多老, 每晚重判, 答案到了 unsure 就摘掉。只按结论(fail)看的话, 过了 60 天的
    这种帖 unsure 永远摘不掉, 教训卡永远借不了它。"""
    notes = [T._note("old_paid", "很久以前、投了流、还没答题的一篇", BODY_C, days=-100, flags={"paid_promoted": True}),
             T._note("old_pass", "很久以前的一篇", BODY_D, days=-100)]
    c = _tv(notes, with_target=False, unanswered={"old_paid"})
    core.tv_sync(c, TV)
    assert _gate(c)["old_paid"][1:] == ("fail", ("not_clean", "unsure"))
    assert postgate.blocked(_links(c)["old_paid"], postgate.BLOCKS_CARDS)

    _next_night()
    out = core.tv_sync(c, TV)
    assert out["gate"]["scope"] == 1 and out["gate"]["touched"] == 1, "答案还没到: 每晚都看它一眼"

    _next_night()
    c.rows["truth_vault.note_feature_answers"].append(_answer("old_paid"))
    out = core.tv_sync(c, TV)
    assert out["gate"]["scope"] == 1 and out["gate"]["changed"] == 1
    assert _gate(c)["old_paid"][1:] == ("fail", ("not_clean",))
    assert not postgate.blocked(_links(c)["old_paid"], postgate.BLOCKS_CARDS), "教训卡不看 not_clean(D-133 第 1 条)"
    assert postgate.blocked(_links(c)["old_paid"], postgate.BLOCKS_DESK)
    settled = _links(c)["old_paid"]["gate_at"]

    _next_night()
    out = core.tv_sync(c, TV)
    assert out["gate"]["scope"] == 0 and _links(c)["old_paid"]["gate_at"] == settled, "定了以后出了范围"


def test_a_link_whose_note_is_gone_from_tv_is_counted_and_left_alone():
    c = _tv(list(NOTES))
    c.rows["truth_vault.notes"] = [r for r in c.rows["truth_vault.notes"] if r["note_id"] != "n3"]
    out = core.tv_sync(c, TV)
    assert out["gate"]["no_note"] == 1 and out["gate"]["scope"] == 2 and _links(c)["n3"].get("gate") is None


# ── 来源: 发了之后抄回写作台的不算 ────────────────────────────────────────────────────────

def _add_desk_version(c, vid, body, *, days):
    c.rows["items"].append({"id": f"i-{vid}", "batch_id": "b1", "best_version_id": vid, "created_at": T._iso(days),
                            "user_id": ME, "status": "pending",
                            "versions": [{"id": vid, "title": "后来存的一版", "body": body, "version_num": 1,
                                          "created_at": T._iso(days)}],
                            "batches": {"project_id": P1}})
    c.rows["versions"].append({"id": vid, "item_id": f"i-{vid}", "title": "后来存的一版", "body": body,
                               "version_num": 1, "created_at": T._iso(days)})


def _copy_link():
    return {"note_id": "n2", "tv_project_id": TV, "project_id": P1, "version_id": "vcopy", "item_id": None,
            "match_kind": "ingested", "score": None, "lag_days": None, "candidates": None, "tv_text_fp": None}


def test_a_copy_back_does_not_read_as_rewritten_from_a_desk_draft():
    """decisions 第 4 条: 帖 9-12 发, 早就补录成了副本; 9-15 有人把发出去的文字存进了写作台。写作台里这一版和帖逐字相同,
    可它是发了之后才有的 —— 来源还是「别处写的」, 不是「拿写作台的稿改的」。"""
    c = _tv([T._note("n2", "上周发的那篇", T.BODY_NEW)], links=[_copy_link()])
    _add_desk_version(c, "vback", T.BODY_NEW, days=5)
    core.tv_sync(c, TV)
    assert _gate(c)["n2"][0] == "elsewhere" and _links(c)["n2"]["gate_reasons"]["overlap"] == 0.0
    assert _links(c)["n2"]["match_kind"] == "ingested" and _links(c)["n2"]["version_id"] == "vcopy"

    # 对照: 同一版要是发布之前就在写作台里, 那就说不清了(unclear), 不记成别处写的
    c = _tv([T._note("n2", "上周发的那篇", T.BODY_NEW)], links=[_copy_link()])
    _add_desk_version(c, "vprior", T.BODY_NEW, days=1)
    core.tv_sync(c, TV)
    assert _gate(c)["n2"][0] == "unclear" and _links(c)["n2"]["gate_reasons"]["overlap"] == 1.0


def test_the_overlap_is_measured_once_and_kept():
    """发布之前写作台里有什么不会再变, 而对照索引会变(稿子改了一版、底稿过期): 量过一次就记着, 之后不重量。"""
    c = _tv([T._note("n2", "上周发的那篇", T.BODY_NEW)], links=[_copy_link()])
    _add_desk_version(c, "vprior", T.BODY_NEW, days=1)
    out = core.tv_sync(c, TV)
    assert out["gate"]["overlap_measured"] == 1 and _gate(c)["n2"][0] == "unclear"
    _next_night()
    c.rows["items"] = [i for i in c.rows["items"] if i["id"] != "i-vprior"]     # 那一版后来不在索引里了
    out = core.tv_sync(c, TV)
    assert out["gate"]["overlap_measured"] == 0 and out["gate"]["touched"] == 1
    assert _gate(c)["n2"][0] == "unclear" and _links(c)["n2"]["gate_reasons"]["overlap"] == 1.0


def test_a_snapshot_checked_before_publication_counts_as_desk_text():
    from tests import test_draft_snapshots as S
    body = S.BODY_S.split("。")[0] + "。" + S.BODY_OTHER      # 约四成来自底稿: 不到转正的线, 照常补录
    notes = [S._note("n1", "换了标题", body)]
    c = S._client(notes, [S._snap("s1", "骑了一百公里", S.BODY_S)])
    c.rows["truth_vault.notes"] = [{"note_id": "n1", "project_id": S.TV, "publish_time": notes[0]["publish_time"],
                                    "tier": "趴", "data_quality_flags": None}]
    c.rows["truth_vault.note_feature_answers"] = [_answer("n1")]
    NIGHT[0] = S.T0 + timedelta(days=5)
    core.tv_sync(c, S.TV)
    row = _links(c)["n1"]
    assert row["match_kind"] == "ingested" and row["origin"] == "unclear"
    assert postgate.ELSEWHERE_MAX <= row["gate_reasons"]["overlap"] < tvlink.SNAPSHOT_EDITED_FLOOR


def test_a_night_the_snapshots_could_not_be_read_leaves_the_overlap_pending(monkeypatch):
    """codex review on aw#155: 这晚查重底稿没读出来(一时的故障), tv_sync 手里的底稿是空的, 不是真没有。原来照量 ——
    约四成来自底稿的帖量出 0、记成「别处写的」, 这个 0 一直沿用, 底稿读得出来之后也不再量。现在这晚不量、记成欠着的,
    读得出来的那晚量上。"""
    from tests import test_draft_snapshots as S
    body = S.BODY_S.split("。")[0] + "。" + S.BODY_OTHER
    notes = [S._note("n1", "换了标题", body)]
    c = S._client(notes, [S._snap("s1", "骑了一百公里", S.BODY_S)])
    c.rows["truth_vault.notes"] = [{"note_id": "n1", "project_id": S.TV, "publish_time": notes[0]["publish_time"],
                                    "tier": "趴", "data_quality_flags": None}]
    c.rows["truth_vault.note_feature_answers"] = [_answer("n1")]
    NIGHT[0] = S.T0 + timedelta(days=5)
    real = store.pending_draft_snapshots

    def boom(*a, **k):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(store, "pending_draft_snapshots", boom)
    out = core.tv_sync(c, S.TV)
    row = _links(c)["n1"]
    assert out["snapshots"]["error"] and out["gate"]["overlap_left"] == 1 and out["gate"]["overlap_measured"] == 0
    assert row["origin"] == "unclear" and "overlap" not in row["gate_reasons"]
    assert row["gate_reasons"]["overlap_pending"] is True

    monkeypatch.setattr(store, "pending_draft_snapshots", real)
    _next_night()
    out = core.tv_sync(c, S.TV)
    row = _links(c)["n1"]
    assert out["gate"]["overlap_measured"] == 1 and "overlap_pending" not in row["gate_reasons"]
    assert row["gate_reasons"]["overlap"] >= postgate.ELSEWHERE_MAX and row["origin"] == "unclear", "不是「别处写的」"


# ── 跳过 / 出错: tv-sync 的结果不动 ──────────────────────────────────────────────────────

def _comparable(out: dict) -> str:
    """tv_sync 的结果去掉 gate, 随机的 id 抹平, 好拿两次运行来比。"""
    rest = {k: v for k, v in out.items() if k != "gate"}
    return re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<id>",
                  json.dumps(rest, sort_keys=True, ensure_ascii=False, default=str))


def _plain_rows(c) -> list[dict]:
    return sorted(({k: v for k, v in l.items() if k not in GATE_COLS | {"id", "version_id", "item_id", "updated_at"}}
                   for l in c.rows["tv_note_links"]), key=lambda r: r["note_id"])


def test_without_migration_020_the_step_skips_itself_and_nothing_else_changes():
    ref = _tv(list(NOTES))
    ref_out = core.tv_sync(ref, TV)
    c = _tv(list(NOTES), missing_columns=GATE_COLS)
    out = core.tv_sync(c, TV)
    assert out["gate"] == {"applied": False, "skipped": "020"}
    assert _comparable(out) == _comparable(ref_out) and _plain_rows(c) == _plain_rows(ref)
    assert not _gate_writes(c) and not any(GATE_COLS & set(l) for l in c.rows["tv_note_links"])
    assert not any(call["table"].startswith("truth_vault.") for call in c.calls), "列不在: TV 那几样一次都不读"
    assert core.tv_sync(c, TV)["gate"] == {"applied": False, "skipped": "020"}, "每晚都只是跳过"


def test_a_dry_run_does_not_run_the_step():
    c = _tv(list(NOTES))
    out = core.tv_sync(c, TV, dry_run=True)
    assert out["gate"] == {"applied": None, "skipped": "dry_run"}
    assert c.rows["tv_note_links"] == [] and not _gate_writes(c)
    assert not any("gate" in (call["cols"] or "") for call in c.calls if call["table"] == "tv_note_links")
    # 已经有对照的库上 dry-run: 也一行不写
    core.tv_sync(c, TV)
    before = {nid: dict(l) for nid, l in _links(c).items()}
    mark = len(c.calls)
    _next_night()
    core.tv_sync(c, TV, dry_run=True)
    assert _links(c) == before and not _gate_writes(c, mark)


def _break(monkeypatch, what: str) -> None:
    boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gate boom"))        # noqa: E731
    if what == "read_links":
        monkeypatch.setattr(store, "tv_links_gate", boom)
    elif what == "flags":
        monkeypatch.setattr(store, "tv_note_gate_flags", lambda *a, **k: None)
    elif what == "answers":
        real = store.note_answers
        monkeypatch.setattr(store, "note_answers", lambda sb, ids, qids, **k: (
            None if list(qids) == [postgate.EFFICACY_QID] else real(sb, ids, qids, **k)))
    elif what == "boost":                               # 视图整晚读不到: 缓存那一份没有, 这一步自己再读一次也没有
        monkeypatch.setattr(priors, "paid_boosted", lambda *a, **k: None)
        monkeypatch.setattr(store, "tv_paid_boost_note_ids", lambda *a, **k: None)
    elif what == "classify":
        monkeypatch.setattr(postgate, "classify", boom)
    elif what == "write":
        monkeypatch.setattr(store, "tv_links_set_gate", boom)
    elif what == "snapshots":
        monkeypatch.setattr(core, "_gate_snapshots", boom)
    else:
        raise AssertionError(what)


@pytest.mark.parametrize("what", ["read_links", "flags", "answers", "boost", "classify", "write", "snapshots"])
def test_a_failing_gate_step_leaves_the_tv_sync_result_intact(monkeypatch, what):
    ref = _tv(list(NOTES))
    ref_out = core.tv_sync(ref, TV)
    c = _tv(list(NOTES))
    _break(monkeypatch, what)
    out = core.tv_sync(c, TV)                           # 不抛
    assert out["gate"]["applied"] is True and out["gate"]["error"]
    assert _comparable(out) == _comparable(ref_out), "别的键和闸没出错时一模一样"
    assert _plain_rows(c) == _plain_rows(ref), "对照、补录照常落库"
    assert out["links_written"] == 3 and out["ingest"]["minted"] == 1
    assert all(l.get("gate") is None for l in c.rows["tv_note_links"]), "没判成: 一行结论都没写"


@pytest.mark.parametrize("what", ["flags", "answers", "boost", "write"])
def test_a_failing_night_keeps_yesterdays_verdicts(monkeypatch, what):
    c = _tv(list(NOTES))
    core.tv_sync(c, TV)
    before = {nid: {k: l[k] for k in GATE_COLS} for nid, l in _links(c).items()}
    _next_night()
    c.rows["truth_vault.notes"][0]["tier"] = "风控"      # 这晚本来该有一行变 fail
    _break(monkeypatch, what)
    out = core.tv_sync(c, TV)
    assert out["gate"]["error"]
    assert {nid: {k: l[k] for k in GATE_COLS} for nid, l in _links(c).items()} == before, "连 gate_at 都留着上一晚的"


def test_one_failed_read_of_the_boost_view_does_not_cancel_the_projects_after_it(monkeypatch):
    """`tv-sync --all` 在一个进程里几秒钟一个项目地跑。投流事件那份缓存(priors.paid_boosted)读挂一次之后 5 分钟不再读,
    新起的 cron 进程里又没有旧的可用 —— 只靠它的话, 第一个项目碰上一次读挂, 这一晚后面每个项目的闸都跟着不判。
    闸这一步缓存里拿不到时自己直接再读一次: 一次读挂最多赔上它碰上的那一次读。"""
    real = store.tv_paid_boost_note_ids
    reads = []

    def flaky(sb):
        reads.append(1)
        return None if len(reads) == 1 else real(sb)
    monkeypatch.setattr(store, "tv_paid_boost_note_ids", flaky)
    c = _tv(list(NOTES), boosted={"n2"})
    for night in range(3):                              # 同一个进程里连着三次(相当于三个 TV 项目), 都在那 5 分钟里
        out = core.tv_sync(c, TV)
        assert out["gate"]["error"] is None and out["gate"]["judged"] == 3, night
        assert _gate(c)["n2"][1:] == ("fail", ("not_clean",)), "判的时候手里是真的投流事件集合"
        assert _links(c)["n1"]["gate_at"] == NIGHT[0].isoformat()
        _next_night()
    assert len(reads) == 4, "缓存读挂的那一次 + 三个项目各自直接读的一次; 缓存的退避没有动"
    assert priors.paid_boosted(c) is None and len(reads) == 4


def test_a_write_that_fails_part_way_reports_the_rows_that_did_land():
    """值不一样的两组结论, 第二组的 UPDATE 挂了: 第一组已经提交。这一晚的计数是这一步唯一的记录(decisions 第 3 条:
    不另记一行), 「改了几行」得是真写进去的行数; 没变的行这晚不刷新 gate_at(灯靠它看出这一晚没判全)。"""
    c = _tv(list(NOTES))
    core.tv_sync(c, TV)
    first = {nid: l["gate_at"] for nid, l in _links(c).items()}
    _next_night()
    c.rows["truth_vault.notes"][0]["tier"] = "风控"                                   # n1 → fail [risk]
    c.rows["truth_vault.note_feature_answers"].append(_answer("n2", "是", at="2026-09-21T20:00:00+00:00"))  # n2 → fail [efficacy]
    real, seen = c._execute, []

    def execute(q):
        if q.table_name == "tv_note_links" and q.op == "update" and set(q.payload) == GATE_COLS:
            seen.append(1)
            if len(seen) == 2:
                raise RuntimeError("timeout")
        return real(q)
    c._execute = execute
    out = core.tv_sync(c, TV)
    g = out["gate"]
    assert g["changed"] == 1 and g["touched"] == 0
    assert "PartialWriteError" in g["error"] and "写了 1 行之后失败" in g["error"]
    got = _gate(c)
    assert sorted(v[1] for v in (got["n1"], got["n2"])) == ["fail", "pass"], "第一组写进去了, 第二组留着上一晚的"
    assert got["n3"][1] == "pass" and _links(c)["n3"]["gate_at"] == first["n3"], "没变的那一行这晚没刷新时间"


def test_a_partial_write_reports_what_landed(monkeypatch):
    c = _tv(list(NOTES))
    monkeypatch.setattr(store, "tv_links_touch_gate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("502")))
    core.tv_sync(c, TV)
    _next_night()
    c.rows["truth_vault.notes"][0]["tier"] = "风控"
    out = core.tv_sync(c, TV)
    assert out["gate"]["changed"] == 1 and out["gate"]["touched"] == 0 and "502" in out["gate"]["error"]
    assert _gate(c)["n1"][1] == "fail", "写进去的那一行是对的"


def test_the_step_writes_nothing_when_its_time_ran_out_while_reading(monkeypatch):
    c = _tv(list(NOTES))
    monkeypatch.setattr(core, "TV_GATE_BUDGET_S", -1.0)
    out = core.tv_sync(c, TV)
    assert "时间上限" in out["gate"]["error"] and not _gate_writes(c)
    assert all(l.get("gate") is None for l in c.rows["tv_note_links"]) and out["links_written"] == 3
    monkeypatch.setattr(core, "TV_GATE_BUDGET_S", 120.0)
    _next_night()
    out = core.tv_sync(c, TV)
    assert out["gate"]["changed"] == 3 and out["gate"]["error"] is None, "没判成的第二晚照常判"


def test_the_step_stops_between_write_chunks_and_the_rest_goes_first_tomorrow(monkeypatch):
    """写到一半时间用完: 写进去的是对的, 没写的留着(没有结论 / 上一晚的), 明晚排在最前。"""
    c = _tv(list(NOTES))
    real = store.tv_links_set_gate

    def slow_set(sb, groups, *, at, stop=None):
        n = real(sb, groups[:1], at=at, stop=stop)        # 写完第一组, 时间就用完了
        monkeypatch.setattr(core, "TV_GATE_BUDGET_S", -1.0)
        return n + real(sb, groups[1:], at=at, stop=stop)
    monkeypatch.setattr(store, "tv_links_set_gate", slow_set)
    out = core.tv_sync(c, TV)
    g = out["gate"]
    assert g["partial"] is True and g["changed"] == 1 and g["error"] is None
    assert sorted(l.get("gate") is not None for l in c.rows["tv_note_links"]) == [False, False, True]
    monkeypatch.setattr(store, "tv_links_set_gate", real)
    monkeypatch.setattr(core, "TV_GATE_BUDGET_S", 120.0)
    _next_night()
    out = core.tv_sync(c, TV)
    assert (out["gate"]["changed"], out["gate"]["touched"], out["gate"]["partial"]) == (2, 1, False)
    assert all(l["gate"] == "pass" and l["gate_at"] == NIGHT[0].isoformat() for l in c.rows["tv_note_links"])


def test_an_unmeasured_overlap_is_unclear_tonight_and_measured_the_next_night(monkeypatch):
    c = _tv([T._note("n2", "上周发的那篇", T.BODY_NEW)], links=[_copy_link()])
    monkeypatch.setattr(core, "TV_GATE_OVERLAP_BUDGET_S", -1.0)
    out = core.tv_sync(c, TV)
    assert out["gate"]["overlap_left"] == 1 and _gate(c)["n2"] == ("unclear", "pass", ())
    assert "overlap" not in _links(c)["n2"]["gate_reasons"]
    assert _links(c)["n2"]["gate_reasons"]["overlap_pending"] is True
    monkeypatch.setattr(core, "TV_GATE_OVERLAP_BUDGET_S", 30.0)
    _next_night()
    out = core.tv_sync(c, TV)
    assert out["gate"]["overlap_measured"] == 1 and _gate(c)["n2"] == ("elsewhere", "pass", ())


def test_an_old_post_whose_overlap_was_not_measured_stays_in_scope_until_it_is(monkeypatch):
    """codex review on aw#155: 60 天以前的帖第一晚(第一次全量)量重合的时间用完了, 结论照存、来源记 unclear —— 原来它
    结论定了、又过了窗口, 第二晚就不在范围里, 来源永远是 unclear(除非有人手跑 --gate-all)。欠着没量的行带一个记号,
    不管多老都留在范围里, 量上了记号就没了、也不再进范围。"""
    c = _tv([T._note("n2", "很久以前发的那篇", T.BODY_NEW, days=-80)], links=[_copy_link()])
    monkeypatch.setattr(core, "TV_GATE_OVERLAP_BUDGET_S", -1.0)
    out = core.tv_sync(c, TV)
    assert out["gate"]["overlap_left"] == 1 and _gate(c)["n2"] == ("unclear", "pass", ())
    assert _links(c)["n2"]["gate_reasons"].get("overlap_pending") is True
    monkeypatch.setattr(core, "TV_GATE_OVERLAP_BUDGET_S", 30.0)
    _next_night()
    out = core.tv_sync(c, TV)
    assert out["gate"]["scope"] == 1 and out["gate"]["overlap_measured"] == 1
    assert _gate(c)["n2"] == ("elsewhere", "pass", ())
    assert "overlap_pending" not in _links(c)["n2"]["gate_reasons"]
    _next_night()
    assert core.tv_sync(c, TV)["gate"]["scope"] == 0, "量上了、结论定了、过了窗口: 不再进范围"


def test_a_copy_whose_published_body_was_replaced_later_is_measured_again():
    """codex review on aw#155: 补录的帖后来在飞书里整篇换了(对照是钉住的, 不重对)。原来记着的重合是对着旧文字量的,
    一直沿用 —— 旧文字和发布前的稿子一模一样(1.0), 换成不相干的文字之后还是 1.0。量的时候把帖的文字指纹一起记下, 正文
    变了就重量; 只是写作台这边的索引变了、或者只改了标题, 不重量。"""
    notes = [T._note("n2", "上周发的那篇", T.BODY_NEW)]
    c = _tv(notes, links=[_copy_link()])
    _add_desk_version(c, "vprior", T.BODY_NEW, days=-3)                      # 发布之前写作台里就有一模一样的
    core.tv_sync(c, TV)
    first = _links(c)["n2"]["gate_reasons"]
    assert first["overlap"] == 1.0 and _links(c)["n2"]["origin"] == "unclear"

    _next_night()
    notes[0] = T._note("n2", "只改了标题", T.BODY_NEW)
    out = core.tv_sync(c, TV)
    assert out["gate"]["overlap_measured"] == 0 and _links(c)["n2"]["gate_reasons"]["overlap"] == 1.0, "只改标题不重量"

    _next_night()
    notes[0] = T._note("n2", "只改了标题", BODY_E)                            # 整篇换成不相干的
    out = core.tv_sync(c, TV)
    row = _links(c)["n2"]
    assert out["gate"]["overlap_measured"] == 1
    assert row["gate_reasons"]["overlap"] == 0.0 and row["origin"] == "elsewhere"
    assert row["gate_reasons"]["overlap_fp"] != first["overlap_fp"]
    assert row["match_kind"] == "ingested" and row["version_id"] == "vcopy", "对照照旧钉着"


def test_a_score_computed_for_the_old_body_no_longer_decides_the_origin_once_the_body_changed():
    """codex review on aw#155: 补录的对照是钉住的, score(0.0, 对着当初那篇不相干的文字算的)一个字不改。帖的正文后来换成了
    发布前写作台里就有的那篇: 重合重量成 1.0, 可来源在 score 和重合里取小的 —— 旧文字的 0 压着, 还是「别处写的」。
    正文变过就记下 score 已经对不上, 之后定来源只看发布前的重合; 对照上的 score 不动。"""
    notes = [T._note("n2", "上周发的那篇", BODY_E)]
    link = {**_copy_link(), "score": 0.0}
    c = _tv(notes, links=[link])
    _add_desk_version(c, "vprior", T.BODY_NEW, days=-3)                      # 发布之前写作台里就有的另一篇
    core.tv_sync(c, TV)
    assert _gate(c)["n2"][0] == "elsewhere" and "score_stale" not in _links(c)["n2"]["gate_reasons"]

    _next_night()
    notes[0] = T._note("n2", "上周发的那篇", T.BODY_NEW)                      # 正文换成了那篇
    core.tv_sync(c, TV)
    row = _links(c)["n2"]
    assert row["gate_reasons"]["overlap"] == 1.0 and row["gate_reasons"]["score_stale"] is True
    assert row["origin"] == "unclear", "不再是「别处写的」"
    assert row["score"] == 0.0 and row["match_kind"] == "ingested", "对照上的 score 和 kind 不动"

    _next_night()
    core.tv_sync(c, TV)                                                      # 记号一直带着: 第三晚照旧不看 score
    assert _links(c)["n2"]["origin"] == "unclear" and _links(c)["n2"]["gate_reasons"]["score_stale"] is True


# ── store: 读写的形状 ───────────────────────────────────────────────────────────────────

def test_tv_links_gate_is_none_only_when_the_gate_columns_are_missing():
    rows = [{"note_id": "n1", "tv_project_id": TV, "match_kind": "ingested", "version_id": "v", "score": None,
             "lag_days": None, "candidates": [{"note": "x"}]}]
    assert store.tv_links_gate(FakeClient(rows={"tv_note_links": rows}, missing_columns={"tv_note_links": {"gate"}}),
                               TV) is None
    got = store.tv_links_gate(FakeClient(rows={"tv_note_links": rows}), TV)
    assert got == {"n1": {"note_id": "n1", "match_kind": "ingested", "version_id": "v", "score": None, "lag_days": None}}
    # 别的缺列 / 别的错不是「020 没跑」: 原样上抛(调用方当故障记)
    with pytest.raises(RuntimeError):
        store.tv_links_gate(FakeClient(rows={"tv_note_links": rows}, missing_columns={"tv_note_links": {"lag_days"}}), TV)


@pytest.mark.parametrize("msg, missing", [
    ('column "origin" of relation "tv_note_links" does not exist', True),
    ("{'code': '42703', 'message': 'column tv_note_links.gate does not exist'}", True),
    ("{'code': '42703', 'message': 'column tv_note_links.gate_reasons does not exist'}", True),
    ("{'code': 'PGRST204', 'message': \"Could not find the 'gate_at' column of 'tv_note_links' in the schema cache\"}", True),
    ("{'code': '42703', 'message': 'column tv_note_links.tv_text_fp does not exist'}", False),
    ("{'code': '42501', 'message': 'permission denied for table tv_note_links'}", False),
    ("{'code': 'PGRST205', 'message': \"Could not find the table 'autowriter.tv_note_links' in the schema cache\"}", False),
    ("connection reset by peer", False),
    ("column tv_note_links.navigate does not exist", False),
])
def test_tv_gate_columns_missing_only_recognises_the_four_columns(msg, missing):
    assert store.tv_gate_columns_missing(RuntimeError(msg)) is missing


def test_set_gate_chunks_and_stops_and_cannot_carry_other_columns():
    rows = [{"note_id": f"n{i:03d}", "tv_project_id": TV, "match_kind": "ingested", "score": 0.5} for i in range(450)]
    c = FakeClient(rows={"tv_note_links": [dict(r) for r in rows]})
    ids = [r["note_id"] for r in rows]
    n = store.tv_links_set_gate(c, [("elsewhere", "pass", {"reasons": []}, ids)], at="T1")
    assert n == 450 and [len(call["filters"][0][2]) for call in c.calls] == [200, 200, 50]
    assert all(set(call["payload"]) == GATE_COLS for call in c.calls)
    assert all(r["match_kind"] == "ingested" and r["score"] == 0.5 and r["gate_at"] == "T1" for r in c.rows["tv_note_links"])
    # 时间用完: 不再发下一块, 回已经写了的行数
    left = [2]

    def stop():
        left[0] -= 1
        return left[0] < 0
    assert store.tv_links_touch_gate(c, ids, at="T2", stop=stop) == 400
    assert sum(r["gate_at"] == "T2" for r in c.rows["tv_note_links"]) == 400
    assert store.tv_links_set_gate(c, [("desk", "fail", {"reasons": ["risk"]}, ids)], at="T3", stop=lambda: True) == 0
    assert store.tv_links_set_gate(c, [], at="T3") == 0 and store.tv_links_touch_gate(c, [], at="T3") == 0


# ── CLI ───────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def fake(monkeypatch):
    c = _tv(list(NOTES))
    monkeypatch.setattr(core, "sb", lambda: c)
    return c


def test_cli_prints_one_line_for_the_gate(fake, capsys):
    assert cli.main(["tv-sync", "--tv-project", TV]) == 0
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if "回流闸" in l)
    assert "范围 3 行, 判了 3(改了 3, 只刷新时间 0)" in line
    assert "pass 3 / fail 0 / unsure 0 / hold 0 / withdrawn 0" in line
    assert "来源 desk 1 / elsewhere 1 / unclear 1" in line
    assert cli.main(["tv-sync", "--tv-project", TV, "--dry-run"]) == 0
    assert "回流闸" not in capsys.readouterr().out, "dry-run 不跑这一步, 也不提它"


def test_cli_says_the_step_was_skipped_when_020_is_not_applied(fake, capsys):
    fake.missing_columns = {"tv_note_links": set(GATE_COLS)}
    assert cli.main(["tv-sync", "--all"]) == 0, "没跑 020 不是错: 退出码不变"
    assert "回流闸: 020 没跑, 这一步跳过" in capsys.readouterr().out


def test_cli_exits_nonzero_when_the_step_itself_failed_but_still_prints_the_sync(fake, capsys, monkeypatch):
    monkeypatch.setattr(store, "tv_note_gate_flags", lambda *a, **k: None)
    assert cli.main(["tv-sync", "--tv-project", TV]) == 1
    out = capsys.readouterr().out
    assert "回流闸这一步没跑成" in out and "对照照常写了" in out and "补录: 收到 1" in out
    assert len(fake.rows["tv_note_links"]) == 3


def test_cli_gate_all_reaches_the_step(fake, capsys):
    notes = [T._note("old", "很久以前的一篇", BODY_C, days=-100)]
    c = _tv(notes, with_target=False)
    fake.rows, fake.rpc_impl = c.rows, c.rpc_impl
    assert cli.main(["tv-sync", "--tv-project", TV]) == 0
    _next_night()
    assert cli.main(["tv-sync", "--tv-project", TV]) == 0
    assert "回流闸: 范围 0 行" in capsys.readouterr().out
    assert cli.main(["tv-sync", "--tv-project", TV, "--gate-all"]) == 0
    assert "回流闸: 范围 1 行, 判了 1(改了 0, 只刷新时间 1)" in capsys.readouterr().out


# ── 迁移 / doctor ───────────────────────────────────────────────────────────────────────

def test_migration_020_adds_the_same_four_columns_in_both_places_and_nothing_else():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    inc = (root / "migrations/020_tv_link_gate.sql").read_text(encoding="utf-8")
    base = (root / "migrations/000_baseline.sql").read_text(encoding="utf-8")
    block = inc[inc.index("ALTER TABLE autowriter.tv_note_links"):inc.index("\nCOMMIT;")].strip()
    assert block in base, "基线里是逐字相同的那几行"
    for col, typ in (("origin", "TEXT"), ("gate", "TEXT"), ("gate_reasons", "JSONB"), ("gate_at", "TIMESTAMPTZ")):
        assert re.search(rf"ADD COLUMN IF NOT EXISTS {col}\s+{typ}[,;]", block), col
        assert f"COMMENT ON COLUMN autowriter.tv_note_links.{col} IS" in block
    code = "\n".join(l for l in inc.splitlines() if not l.strip().startswith("--"))
    statements = re.findall(r"^(BEGIN|COMMIT|[A-Z]+ [A-Z]+)\b", code, re.M)      # 顶格起头的每一句
    assert sorted(set(statements)) == ["ALTER TABLE", "BEGIN", "COMMENT ON", "COMMIT", "SET LOCAL"], \
        "只加列和注释: 不建表、不建函数、不发 GRANT、不碰数据"
    assert statements.count("ALTER TABLE") == 1 and statements.count("COMMENT ON") == 4
    for word in ("NOT NULL", "DEFAULT", "CHECK", "UPDATE ", "INSERT ", "DELETE ", "DROP ", "GRANT ", "CREATE "):
        assert word not in code, word
    # 注释里登记的名字和 postgate 里的是同一套
    for name in postgate.ORIGINS + postgate.GATES + postgate.REASONS:
        assert name in block, name


def test_doctor_reports_020_missing_when_the_columns_are_absent_and_applied_when_present():
    from tests.test_migration_doctor import _ALL_RPCS, _TABLES
    probe = "tv_note_links 的回流闸四列(origin / gate / gate_reasons / gate_at)"
    rep = core.migration_state(FakeClient(rows=_TABLES, rpc_impl=_ALL_RPCS,
                                          missing_columns={"tv_note_links": {"gate_at"}}))
    st = {c["probe"]: c for c in rep["checks"]}
    assert "020_tv_link_gate.sql" in rep["missing"] and st[probe]["state"] == "missing"
    assert "可降级" in st[probe]["impact"]
    assert st["tv_note_links 的 tv_text_fp 列"]["state"] == "applied", "017 的探测不受影响"
    rep = core.migration_state(FakeClient(rows=_TABLES, rpc_impl=_ALL_RPCS))
    assert {c["probe"]: c["state"] for c in rep["checks"]}[probe] == "applied" and rep["ok"] is True
    assert core.MIGRATION_TV_LINK_GATE == "020_tv_link_gate.sql"
