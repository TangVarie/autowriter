"""D-120 / W-23(2026-10-19): 人改得多的同一篇也认(edited), 记下人在发布前改了什么, 每周一份摘要。

钉住:
  1. tvlink: 包含度 0.30~0.60 且领先第二名 ≥0.15 → edited; 领先不够 → ambiguous(周检); 0.30 以下 → unmatched;
     ≥0.60 照旧 fuzzy;
  2. edit_diff: 标题改没改、删 / 加的句子、长度、品牌次数, 只改标点不算改;
  3. tv_sync: edited 写进对照、算真对上(回写 TV); 新对上的和以前漏算的都算差异; dry-run 不写; 差异挂了对照照写;
  4. edit_digest: 改过的 ≥3 篇才给; 看过之后一周内不给; 只看 through 之后的; 别人的项目 403;
  5. ack_edit_digest: 写一行, through 不对 / rules_recorded 不对 → 400; open_project 带上摘要、摘要查挂了简报照给。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from deskcore import core, store, tools, tvlink
from tests.fakes import FakeClient
from tests.test_tv_sync import BODY_A, ME, P1, TV, _client, _iso, _note

_POOL = ("春夏秋冬山水风云日月星辰江河湖海花草树木鸟兽虫鱼金木水火土天地人和东西南北上下左右前后"
         "红橙黄绿青蓝紫黑白灰酸甜苦辣咸香臭软硬冷热高低长短大小多少快慢轻重新旧好坏美丑真假")


def _sentence(seed: int, n: int = 16) -> str:
    """互相没有共同四字串的句子(线性同余取字), 免得"同模板句"互相对齐。"""
    x, out = seed * 7919 + 13, []
    for _ in range(n):
        x = (x * 1103515245 + 12345) % (2 ** 31)
        out.append(_POOL[x % len(_POOL)])
    return "".join(out)


SENTS = [_sentence(i) for i in range(20)]
FRESH = [_sentence(100 + i) for i in range(20)]


def _v(vid, body, days=0):
    return tvlink.Version(version_id=vid, project_id=P1, title=f"标题{vid}", body=body,
                          created_at=datetime(2026, 9, 10, tzinfo=timezone.utc) + timedelta(days=days))


def _n(body, title="发布标题"):
    return tvlink.Note(note_id="n", title=title, body=body,
                       publish_time=datetime(2026, 9, 12, tzinfo=timezone.utc))


def _body(keep: int, new: int, src=SENTS) -> str:
    """新加的放前面: 开头不同, 才不会被"开头 40 字相同"那一档先认走。"""
    return "。".join(FRESH[:new] + src[:keep]) + "。"


# ── 1. 匹配 ──────────────────────────────────────────────────────────────────────────────────

def test_heavily_edited_but_clearly_one_draft_is_edited():
    idx = tvlink.VersionIndex([_v("a", _body(20, 0)), _v("b", "。".join(FRESH[10:20]) + "。")])
    m = tvlink.match_note(_n(_body(9, 11)), idx)          # 包含度 ~0.45, 第二名 ~0
    assert m.kind == "edited" and m.version.version_id == "a" and 0.30 <= m.score < 0.60
    assert m.candidates and m.candidates[0]["version_id"] == "a"


def test_two_close_variants_go_to_review_not_a_guess():
    other = "。".join(SENTS[:8] + [s + "改" for s in SENTS[8:20]]) + "。"
    idx = tvlink.VersionIndex([_v("a", _body(20, 0)), _v("b", other)])
    m = tvlink.match_note(_n(_body(9, 11)), idx)
    assert m.kind == "ambiguous" and m.version is None
    assert {c["version_id"] for c in m.candidates} == {"a", "b"}


def test_below_the_floor_stays_unmatched_and_above_060_stays_fuzzy():
    idx = tvlink.VersionIndex([_v("a", _body(20, 0))])
    assert tvlink.match_note(_n(_body(4, 16)), idx).kind == "unmatched"     # ~0.2
    assert tvlink.match_note(_n(_body(15, 5)), idx).kind == "fuzzy"         # ~0.75


# ── 2. 改动差异 ──────────────────────────────────────────────────────────────────────────────

def test_edit_diff_reports_what_a_person_changed():
    d = tvlink.edit_diff("秋招投了六十份", _body(6, 0), "秋招投了60份", _body(4, 2), brand=SENTS[0][:2])
    assert d["title_changed"] and d["changed"]
    assert d["removed_count"] == 2 and d["added_count"] == 2
    assert d["removed_sentences"] == SENTS[4:6] and d["added_sentences"] == FRESH[:2]
    assert d["brand_draft"] == _body(6, 0).count(SENTS[0][:2]) and d["brand_published"] is not None
    assert d["published_chars"] > 0 and d["draft_chars"] > 0


def test_punctuation_only_is_not_a_change():
    d = tvlink.edit_diff("标题！", _body(5, 0), "标题", _body(5, 0).replace("。", "！"))
    assert not d["title_changed"] and not d["changed"] and d["removed_count"] == d["added_count"] == 0
    assert d["brand_draft"] is None


# ── 3. tv_sync 接线 ──────────────────────────────────────────────────────────────────────────

EDITED_BODY = BODY_A[:30] + "。" + "这一段是发布前人工改写的内容和原稿差很多但开头意思还在。" * 2


def test_tv_sync_writes_diffs_for_new_and_older_links():
    old = {"note_id": "n0", "tv_project_id": TV, "project_id": P1, "version_id": "v1", "item_id": "i1",
           "match_kind": "body_exact", "score": 1.0}
    notes = [_note("n0", "秋招投了六十份", BODY_A), _note("n1", "秋招投了六十份简历没回音", BODY_A)]
    c = _client(notes, links=[old])
    out = core.tv_sync(c, TV)
    rows = {r["note_id"]: r for r in c.rows["tv_edit_diffs"]}
    assert set(rows) == {"n0", "n1"}, "新对上的算, 以前对上还没算过的也补算"
    assert out["edit_diffs"]["backfilled"] == 1 and out["edit_diffs"]["written"] == 2
    assert rows["n1"]["title_changed"] is True and rows["n1"]["project_id"] == P1
    assert rows["n0"]["changed"] is False
    # 再跑一次: 都算过了, 不重算
    assert core.tv_sync(c, TV)["edit_diffs"]["computed"] == 0


def test_dry_run_writes_no_diffs():
    c = _client([_note("n1", "秋招投了六十份简历没回音", BODY_A)])
    out = core.tv_sync(c, TV, dry_run=True)
    assert out["edit_diffs"]["computed"] == 1 and out["edit_diffs"]["written"] == 0
    assert not c.rows.get("tv_edit_diffs")


def test_a_diff_failure_does_not_lose_the_links(monkeypatch):
    c = _client([_note("n1", "秋招投了六十份简历没回音", BODY_A)])
    monkeypatch.setattr(store, "tv_edit_diffs_upsert", lambda *a: (_ for _ in ()).throw(RuntimeError("库挂了")))
    out = core.tv_sync(c, TV)
    assert "库挂了" in out["edit_diffs"]["error"]
    assert [l["match_kind"] for l in c.rows["tv_note_links"]] == ["body_exact"]


def test_edited_counts_as_a_real_match_for_tv_backfill():
    assert "edited" in core._TV_BACKFILL_KINDS
    assert core._backfillable({"match_kind": "edited", "version_id": "v1"})


# ── 4/5. 每周摘要 + 看完 ─────────────────────────────────────────────────────────────────────

NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)


def _digest_client(n_changed=3, ack=None, owner=ME):
    rows = [{"note_id": f"n{i}", "tv_project_id": TV, "project_id": P1, "version_id": None,
             "match_kind": "fuzzy", "changed": i < n_changed, "title_changed": i == 0,
             "draft_title": f"原标题{i}", "published_title": f"新标题{i}", "draft_chars": 300, "published_chars": 200,
             "removed_count": 2, "added_count": 1, "removed_sentences": ["删掉的一句", "又删一句", "再删", "第四句"],
             "added_sentences": ["加的一句"], "brand_draft": 3, "brand_published": 1,
             "computed_at": (NOW - timedelta(days=1, minutes=i)).isoformat()} for i in range(5)]
    c = FakeClient(rows={"projects": [{"id": P1, "name": "sportsix", "brand": "sportsix", "owner_id": owner}],
                         "tv_edit_diffs": rows, "tv_edit_digest_acks": [ack] if ack else []})
    return c


def test_digest_appears_with_enough_changed_pairs():
    d = core.edit_digest(_digest_client(), P1, user_id=ME, now=NOW)
    assert d["changed"] == 3 and d["pairs"] == 5 and d["title_changed"] == 1
    assert d["chars"] == {"draft_avg": 300, "published_avg": 200}
    assert d["brand_mentions"] == {"draft_avg": 3, "published_avg": 1}
    assert len(d["examples"]) == 3 and d["examples"][0]["title_before"] == "原标题0"
    assert d["examples"][0]["removed"] == ["删掉的一句", "又删一句", "再删"], "每篇最多 3 句"
    assert "ack_edit_digest" in d["task"] and "record_rule" in d["task"]
    assert d["through"] == max(r["computed_at"] for r in _digest_client().rows["tv_edit_diffs"])


def test_no_digest_with_too_few_changes_or_within_a_week_of_the_last_one():
    assert core.edit_digest(_digest_client(n_changed=2), P1, user_id=ME, now=NOW) is None
    recent = {"project_id": P1, "acked_through": (NOW - timedelta(days=30)).isoformat(),
              "acked_at": (NOW - timedelta(days=3)).isoformat()}
    assert core.edit_digest(_digest_client(ack=recent), P1, user_id=ME, now=NOW) is None


def test_after_a_week_only_newer_diffs_count():
    old_ack = {"project_id": P1, "acked_through": (NOW - timedelta(days=1, minutes=2)).isoformat(),
               "acked_at": (NOW - timedelta(days=8)).isoformat()}
    c = _digest_client(ack=old_ack)
    # through 之后只剩 n0、n1 两条(n2 正好在 through 上, 不算) → 改过的不到 3 篇
    assert core.edit_digest(c, P1, user_id=ME, now=NOW) is None


def test_someone_elses_project_gets_no_digest():
    with pytest.raises(PermissionError):
        core.edit_digest(_digest_client(owner="someone-else"), P1, user_id=ME, now=NOW)


def test_ack_writes_one_row_and_rejects_bad_input(monkeypatch):
    c = _digest_client()
    monkeypatch.setattr(core, "sb", lambda: c)
    out = tools.ack_edit_digest(P1, "2026-10-18T11:59:00+00:00", rules_recorded=2, _user_id=ME)
    assert out["acked"] and out["rules_recorded"] == 2
    (row,) = c.rows["tv_edit_digest_acks"]
    assert row["project_id"] == P1 and row["acked_by"] == ME and row["rules_recorded"] == 2
    assert core.edit_digest(c, P1, user_id=ME) is None, "看过之后一周内不再给"
    for bad in ({"through": "不是时间"}, {"through": "2026-10-18", "rules_recorded": -1},
                {"through": "2026-10-18", "rules_recorded": True}):
        with pytest.raises(tools.InvalidInput):
            tools.ack_edit_digest(P1, bad["through"], rules_recorded=bad.get("rules_recorded", 0), _user_id=ME)
    assert tools.TOOLS["ack_edit_digest"][1] is True


def test_open_project_carries_the_digest_and_survives_its_failure(monkeypatch):
    monkeypatch.setattr(core, "sb", lambda: object())
    monkeypatch.setattr(core, "build_writing_brief", lambda *a, **k: {"p0": [], "counts": {}})
    monkeypatch.setattr(core, "edit_digest", lambda *a, **k: {"through": "t", "task": "x"})
    assert tools.open_project(P1, _user_id=ME)["edit_digest"]["through"] == "t"
    monkeypatch.setattr(core, "edit_digest", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("表没建")))
    out = tools.open_project(P1, _user_id=ME)
    assert "edit_digest" not in out and out["p0"] == []


# ── codex review on aw#107 第三轮 ─────────────────────────────────────────────────────────────

def test_a_few_words_changed_inside_a_long_sentence_is_a_change():
    before = "我当时投了六十份简历一直没有收到回音后来把第一行改成了具体的项目经历。" + SENTS[0] + "。"
    after = "我当时投了六十份简历一直没有收到回复后来把第一行改成了具体的项目经历。" + SENTS[0] + "。"
    d = tvlink.edit_diff("标题", before, "标题", after)
    assert d["removed_count"] == d["added_count"] == 0, "两边都还过半, 进不了删 / 加"
    assert d["changed"] and d["edited_count"] == 1
    assert d["edited_sentences"] == [[before.split("。")[0], after.split("。")[0]]]


def test_reordering_sentences_is_a_change():
    d = tvlink.edit_diff("标题", _body(6, 0), "标题", "。".join(reversed(SENTS[:6])) + "。")
    assert d["changed"] and d["removed_count"] == d["added_count"] == d["edited_count"] == 0


def _many_changed(n, start=None, tie_at=None):
    start = start or (NOW - timedelta(days=3))
    rows = []
    for i in range(n):
        t = start + timedelta(minutes=i if tie_at is None or i < tie_at else tie_at)
        rows.append({"note_id": f"m{i:04d}", "tv_project_id": TV, "project_id": P1, "match_kind": "fuzzy",
                     "changed": True, "title_changed": False, "draft_title": "a", "published_title": "a",
                     "draft_chars": 10, "published_chars": 9, "removed_count": 1, "added_count": 0,
                     "computed_at": t.isoformat()})
    return rows


def test_more_than_one_page_is_read_oldest_first_and_nothing_is_skipped():
    rows = _many_changed(store.EDIT_DIGEST_ROWS + 50)
    c = _digest_client(n_changed=0)
    c.rows["tv_edit_diffs"] = rows
    d = core.edit_digest(c, P1, user_id=ME, now=NOW)
    assert d["changed"] == store.EDIT_DIGEST_ROWS and d["more_after_through"] is True
    assert d["through"] == rows[store.EDIT_DIGEST_ROWS - 1]["computed_at"], "through 是真看到的最后一行"
    core.ack_edit_digest(c, P1, d["through"], user_id=ME)
    c.rows["tv_edit_digest_acks"][0]["acked_at"] = (NOW - timedelta(days=8)).isoformat()
    d2 = core.edit_digest(c, P1, user_id=ME, now=NOW)
    assert d2["changed"] == 50 and "more_after_through" not in d2, "剩下的 50 行下一份接着给, 一行不丢"


def test_unchanged_rows_do_not_block_later_changes():
    c = _digest_client(n_changed=0)
    quiet = [dict(r, changed=False) for r in _many_changed(500, start=NOW - timedelta(days=5))]
    c.rows["tv_edit_diffs"] = quiet + _many_changed(3, start=NOW - timedelta(days=1))
    for i, r in enumerate(c.rows["tv_edit_diffs"][-3:]):
        r["note_id"] = f"late{i}"
    d = core.edit_digest(c, P1, user_id=ME, now=NOW)
    assert d["changed"] == 3 and d["pairs"] == 503


def test_a_timestamp_tie_at_the_cut_is_left_whole_for_next_time():
    lim = store.EDIT_DIGEST_ROWS
    rows = _many_changed(lim + 5, tie_at=lim - 2)          # 第 lim-2 行起时间都一样, 跨过了截断点
    c = _digest_client(n_changed=0)
    c.rows["tv_edit_diffs"] = rows
    d = core.edit_digest(c, P1, user_id=ME, now=NOW)
    assert d["changed"] == lim - 2 and d["through"] < rows[lim - 2]["computed_at"]


def test_the_ack_watermark_never_moves_back():
    c = _digest_client()
    newer, older = "2026-10-18T12:00:00+00:00", "2026-10-17T12:00:00+00:00"
    assert core.ack_edit_digest(c, P1, newer, user_id=ME)["through"] == newer
    out = core.ack_edit_digest(c, P1, older, user_id=ME, rules_recorded=1)
    assert out["through"] == newer and c.rows["tv_edit_digest_acks"][0]["acked_through"] == newer
    assert c.rows["tv_edit_digest_acks"][0]["rules_recorded"] == 1, "看过的人和时间照记"


def test_cli_tv_sync_fails_loudly_when_diffs_cannot_be_read(monkeypatch, capsys):
    from deskcore import cli
    c = _client([_note("n1", "秋招投了六十份简历没回音", BODY_A)])
    monkeypatch.setattr(core, "sb", lambda: c)
    monkeypatch.setattr(store, "tv_edit_diff_note_ids",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("relation tv_edit_diffs does not exist")))
    assert cli.main(["tv-sync", "--tv-project", TV]) == 1
    out = capsys.readouterr().out
    assert "改动差异没写成" in out and "doctor" in out
    assert [l["match_kind"] for l in c.rows["tv_note_links"]] == ["body_exact"], "对照照写"
