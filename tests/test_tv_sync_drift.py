"""tv-sync 核对已对上的对照的 TV 正文(W-34, 2026-10-27)。

以前有版本的对照每晚记 already_linked、不再看 TV 的正文: 运营在飞书里把一篇整篇换成另一篇(note_id 不变), 对照、写回 TV 的
血缘和改动差异就一直挂在原来那版上(D-129: BJS_phase1 14 条, 其中 10 条的差异进了儿童藻油项目的改稿摘要)。--rematch 也救不了:
写回过 TV 的对照一重对就走 TV 的 lineage 分支、对回同一版。

这里的假 TV 是活的: 回填 RPC 真把 lineage 写进笔记(只填 NULL, item 走 COALESCE), 撤回 RPC 做 compare-and-swap ——
test_tv_sync 的假回填只记调用、不写回, 那个回环在那里看不见。
"""

from __future__ import annotations

import pytest

from deskcore import cli, core, store, tvlink
from tests.test_tv_sync import BODY_A, BODY_NEW, ME, P1, TV, _client, _iso, _note

BODY_C = ("第二篇稿子讲的是另一件事: 面试前一晚把自我介绍念给室友听, 室友听到第三句就睡着了。"
          "第二天我把前三句删了, 面试官第一次在我说完之前就开始点头。")
BODY_TENNIS = ("周末第一次去打网球, 教练让我先对着墙打两百个正手, 打到一半手腕就酸得抬不起来。"
               "回家查了才知道握拍太紧, 第二周换了松一点的握法, 墙打到三百个也不累。")
OLD_TS = "2026-09-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _no_embeddings(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)


def _add_version(c, vid, iid, title, body):
    c.rows["items"].append({"id": iid, "batch_id": "b1", "best_version_id": vid, "created_at": _iso(0),
                            "user_id": ME, "status": "pending",
                            "versions": [{"id": vid, "title": title, "body": body,
                                          "version_num": 1, "created_at": _iso(0)}],
                            "batches": {"project_id": P1}})
    c.rows["versions"].append({"id": vid, "item_id": iid, "title": title, "body": body,
                               "version_num": 1, "created_at": _iso(0)})


def _tv(notes, *, links=None, with_target=True):
    """``notes`` 就是 TV: tv-sync 读它, 回填 / 撤回写它。"""
    c = _client(notes, links=links, with_target=with_target)
    _add_version(c, "v2", "i2", "自我介绍删了前三句", BODY_C)
    calls = {"backfill": [], "clear": []}

    def _row(nid):
        return next((r for r in notes if r["note_id"] == nid), None)

    def _tv_notes(a):
        rows = sorted((r for r in notes if a.get("_after") is None or r["note_id"] > a["_after"]),
                      key=lambda r: r["note_id"])
        return [dict(r) for r in rows[: a["_limit"]]]

    def _backfill(a):                                   # 009: 只填 NULL, item 走 COALESCE
        calls["backfill"].append(a["_links"])
        n = 0
        for l in a["_links"]:
            r = _row(l["note_id"])
            if r is not None and l.get("version_id") and r.get("source_autowriter_version_id") is None:
                r["source_autowriter_version_id"] = l["version_id"]
                r["source_autowriter_item_id"] = r.get("source_autowriter_item_id") or l.get("item_id")
                n += 1
        return n

    def _clear(a):                                      # 017: 两列都还等于才清, 回清了的 note_id
        calls["clear"].append(a["_links"])
        out = []
        for l in a["_links"]:
            r = _row(l["note_id"])
            if (r is not None and l.get("version_id") and r.get("source_autowriter_version_id") == l["version_id"]
                    and r.get("source_autowriter_item_id") == l.get("item_id")):
                r["source_autowriter_version_id"] = r["source_autowriter_item_id"] = None
                out.append(l["note_id"])
        return sorted(out)

    c.rpc_impl = {"deskcore_tv_notes": _tv_notes, "deskcore_tv_backfill_lineage": _backfill,
                  "deskcore_tv_clear_lineage": _clear}
    c.tv_calls = calls
    return c


def _replace(notes, nid, body, title=None):
    r = next(r for r in notes if r["note_id"] == nid)
    t = title or tvlink.parse_raw(r["raw_content"])[0]
    r["raw_content"] = f"【标题】{t} 【正文】{body} #跑步 #能量胶"


def _link(c, nid):
    return next(l for l in c.rows["tv_note_links"] if l["note_id"] == nid)


def _diff(c, nid):
    return next((d for d in c.rows.get("tv_edit_diffs", []) if d["note_id"] == nid), None)


def _lineage(notes, nid):
    r = next(r for r in notes if r["note_id"] == nid)
    return r.get("source_autowriter_version_id"), r.get("source_autowriter_item_id")


# ── tvlink: 指纹和"还是不是那一篇" ───────────────────────────────────────────────────────────────

def _n(title, body):
    return tvlink.Note(note_id="n", title=title, body=tvlink.strip_hashtags(body), publish_time=None)


def _v(title, body):
    return tvlink.Version(version_id="v", project_id=P1, title=title, body=body, created_at=None)


def test_text_fp_ignores_hashtags_punctuation_and_spacing_but_not_words():
    base = tvlink.text_fp(_n("秋招投了六十份", BODY_A))
    assert tvlink.text_fp(_n("秋招投了六十份!", BODY_A.replace(", ", "，") + " #秋招 #求职")) == base
    assert tvlink.text_fp(_n("秋招投了六十份", BODY_A.replace("三个面试", "四个面试"))) != base
    assert tvlink.text_fp(_n("秋招投了七十份", BODY_A)) != base, "标题改了也算改过"


def test_still_same_keeps_edits_and_catches_wholesale_replacement():
    v = _v("秋招投了六十份", BODY_A)
    assert tvlink.still_same(_n("x", BODY_A), v), "同样的开头"
    assert tvlink.still_same(_n("x", "加了一句开场白, 和原文无关的几个字。" + BODY_A), v), "开头改了, 四字串大半还在"
    assert not tvlink.still_same(_n("x", BODY_TENNIS), v), "整篇换了"
    title_only = _v("我的跑步第一课", "")
    assert tvlink.still_same(_n("我的跑步第一课", BODY_TENNIS), title_only), "原版只有标题: 标题一样就是那篇"
    assert not tvlink.still_same(_n("别的标题", BODY_TENNIS), title_only)


# ── 整篇换了: 挪对照、撤回我们写进 TV 的旧血缘 ─────────────────────────────────────────────────────

def test_replaced_text_moves_the_link_clears_our_lineage_and_quarantines_the_old_diff():
    notes = [_note("n1", "秋招投了六十份简历没回音", BODY_A)]
    c = _tv(notes)
    core.tv_sync(c, TV, write_tv=True)
    assert _lineage(notes, "n1") == ("v1", "i1") and _diff(c, "n1")["diff_rev"] == tvlink.DIFF_REV

    _replace(notes, "n1", BODY_TENNIS)
    out = core.tv_sync(c, TV, write_tv=True)
    dr = out["drift"]
    assert dr["replaced"] == 1 and dr["relinked"] == 1 and dr["tv_cleared"] == 1
    assert c.tv_calls["clear"][-1] == [{"note_id": "n1", "version_id": "v1", "item_id": "i1"}]
    l = _link(c, "n1")
    assert l["match_kind"] == "ingested" and l["version_id"] not in (None, "v1")
    assert "W-34" in l["candidates"][0]["note"] and "v1" in l["candidates"][0]["note"]
    assert _lineage(notes, "n1") == (None, None), "ingested 不回填: TV 留空, 不再说它来自 v1"
    assert _diff(c, "n1")["diff_rev"] == 0, "旧差异降回旧口径, 摘要读不到"
    assert sum(out["counts"].values()) == out["notes"]

    again = core.tv_sync(c, TV, write_tv=True)
    assert again["counts"]["already_linked"] == 1 and again["drift"]["replaced"] == 0
    assert _link(c, "n1")["match_kind"] == "ingested", "第二晚不会被 TV 的旧血缘拽回去"


def test_replaced_by_another_drafts_text_relinks_and_writes_the_new_lineage():
    notes = [_note("n1", "秋招投了六十份简历没回音", BODY_A)]
    c = _tv(notes)
    core.tv_sync(c, TV, write_tv=True)
    _diff(c, "n1")["computed_at"] = OLD_TS

    _replace(notes, "n1", BODY_C, title="自我介绍删了前三句")
    out = core.tv_sync(c, TV, write_tv=True)
    l = _link(c, "n1")
    assert (l["match_kind"], l["version_id"], l["item_id"]) == ("body_exact", "v2", "i2")
    assert l["synced_to_tv_at"], "新的一版写回过 TV"
    assert _lineage(notes, "n1") == ("v2", "i2"), "先撤回 v1/i1, 同一晚回填 v2/i2"
    d = _diff(c, "n1")
    assert d["version_id"] == "v2" and d["diff_rev"] == tvlink.DIFF_REV and d["computed_at"] > OLD_TS, \
        "换了一版: 新差异记现在, 进下一份摘要"
    assert out["drift"]["relinked"] == 1


def test_rematch_after_write_back_matches_by_content_not_our_own_lineage():
    """以前: 写回过 TV 的对照一 --rematch 就走 tv_lineage 分支, 对回同一版、改名 tv_lineage, 内容一次都没比。"""
    notes = [_note("n1", "秋招投了六十份简历没回音", BODY_A)]
    c = _tv(notes)
    core.tv_sync(c, TV, write_tv=True)
    out = core.tv_sync(c, TV, rematch=True, write_tv=True)
    assert out["counts"]["tv_lineage"] == 0 and out["counts"]["body_exact"] == 1
    assert _link(c, "n1")["match_kind"] == "body_exact" and _link(c, "n1")["version_id"] == "v1"

    _replace(notes, "n1", BODY_C, title="自我介绍删了前三句")
    out = core.tv_sync(c, TV, rematch=True, write_tv=True)
    assert _link(c, "n1")["version_id"] == "v2" and _lineage(notes, "n1") == ("v2", "i2")


def test_without_write_tv_a_replaced_link_waits_and_its_diff_is_quarantined():
    notes = [_note("n1", "秋招投了六十份简历没回音", BODY_A)]
    c = _tv(notes)
    core.tv_sync(c, TV, write_tv=True)
    before = dict(_link(c, "n1"))

    _replace(notes, "n1", BODY_TENNIS)
    out = core.tv_sync(c, TV)
    assert out["drift"]["deferred"] == 1 and out["drift"]["relinked"] == 0
    assert _link(c, "n1") == before, "对照和指纹都不动"
    assert _lineage(notes, "n1") == ("v1", "i1") and not c.tv_calls["clear"]
    assert _diff(c, "n1")["diff_rev"] == 0, "差异先隔离"

    out = core.tv_sync(c, TV, write_tv=True)
    assert out["drift"]["relinked"] == 1 and _link(c, "n1")["match_kind"] == "ingested"


def test_a_failed_clear_moves_nothing_fails_the_cli_and_is_retried(monkeypatch, capsys):
    notes = [_note("n1", "秋招投了六十份简历没回音", BODY_A)]
    c = _tv(notes)
    core.tv_sync(c, TV, write_tv=True)
    _replace(notes, "n1", BODY_TENNIS)
    real = store.tv_clear_lineage
    monkeypatch.setattr(store, "tv_clear_lineage",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("PGRST202 Could not find the function")))
    monkeypatch.setattr(core, "sb", lambda: c)
    assert cli.main(["tv-sync", "--tv-project", TV, "--write-tv"]) == 1
    out = capsys.readouterr().out
    assert "撤回 TV 旧血缘失败" in out and "doctor" in out
    assert _link(c, "n1")["version_id"] == "v1" and _diff(c, "n1")["diff_rev"] == 0

    monkeypatch.setattr(store, "tv_clear_lineage", real)
    assert cli.main(["tv-sync", "--tv-project", TV, "--write-tv"]) == 0
    assert _link(c, "n1")["match_kind"] == "ingested" and _lineage(notes, "n1") == (None, None)
    assert cli.main(["tv-sync", "--tv-project", TV, "--write-tv"]) == 0
    assert _link(c, "n1")["match_kind"] == "ingested"


def test_only_the_notes_whose_lineage_was_actually_cleared_are_moved(capsys, monkeypatch):
    notes = [_note("n1", "秋招投了六十份简历没回音", BODY_A), _note("n2", "自我介绍删了前三句", BODY_C)]
    c = _tv(notes)
    core.tv_sync(c, TV, write_tv=True)
    assert _lineage(notes, "n2") == ("v2", "i2")
    notes[1]["source_autowriter_item_id"] = "i-someone-else"      # TV 那边有人改过 n2 的 item
    _replace(notes, "n1", BODY_TENNIS, title="第一次打网球")
    _replace(notes, "n2", BODY_NEW, title="上周发的那篇")
    monkeypatch.setattr(core, "sb", lambda: c)
    assert cli.main(["tv-sync", "--tv-project", TV, "--write-tv"]) == 1
    assert "已经不是我们写的那一对" in capsys.readouterr().out
    assert _link(c, "n1")["match_kind"] == "ingested"
    assert _link(c, "n2")["version_id"] == "v2", "没清掉的不挪"
    assert _lineage(notes, "n2") == ("v2", "i-someone-else") and _diff(c, "n2")["diff_rev"] == 0


def test_over_the_nightly_cap_nothing_moves_and_the_cli_says_how_to_release(monkeypatch, capsys):
    notes = [_note("n1", "秋招投了六十份简历没回音", BODY_A), _note("n2", "自我介绍删了前三句", BODY_C)]
    c = _tv(notes)
    core.tv_sync(c, TV, write_tv=True)
    _replace(notes, "n1", BODY_TENNIS, title="第一次打网球")
    _replace(notes, "n2", BODY_NEW, title="上周发的那篇")
    monkeypatch.setenv("TV_SYNC_RELINK_MAX", "1")
    monkeypatch.setattr(core, "sb", lambda: c)
    assert cli.main(["tv-sync", "--tv-project", TV, "--write-tv"]) == 1
    out = capsys.readouterr().out
    assert "超过每晚上限 1" in out and "TV_SYNC_RELINK_MAX=2" in out
    assert [_link(c, n)["version_id"] for n in ("n1", "n2")] == ["v1", "v2"] and not c.tv_calls["clear"]
    assert [_diff(c, n)["diff_rev"] for n in ("n1", "n2")] == [0, 0]
    monkeypatch.setenv("TV_SYNC_RELINK_MAX", "2")
    assert cli.main(["tv-sync", "--tv-project", TV, "--write-tv"]) == 0
    assert {_link(c, n)["match_kind"] for n in ("n1", "n2")} == {"ingested"}


def test_dry_run_reports_the_moves_and_writes_nothing():
    notes = [_note("n1", "秋招投了六十份简历没回音", BODY_A)]
    c = _tv(notes)
    core.tv_sync(c, TV, write_tv=True)
    _replace(notes, "n1", BODY_C, title="自我介绍删了前三句")
    before = [dict(l) for l in c.rows["tv_note_links"]]
    out = core.tv_sync(c, TV, write_tv=True, dry_run=True)
    dr = out["drift"]
    assert dr["relinked"] == 1 and dr["samples"][0]["new_version"] == "v2" and dr["samples"][0]["overlap"] < 0.1
    assert c.rows["tv_note_links"] == before and not c.tv_calls["clear"] and _lineage(notes, "n1") == ("v1", "i1")


# ── 改过字、还是那一篇: 只记指纹, 差异按现在的正文重算 ─────────────────────────────────────────────

def test_an_added_sentence_recomputes_the_diff_in_place_and_touches_only_the_fingerprint():
    notes = [_note("n1", "秋招投了六十份简历没回音", BODY_A)]
    c = _tv(notes)
    core.tv_sync(c, TV, write_tv=True)
    d = _diff(c, "n1")
    assert d["added_count"] == 0
    d["computed_at"] = OLD_TS
    before = dict(_link(c, "n1"))

    _replace(notes, "n1", BODY_A + "最后补一句: 简历第一行写你最硬的那段经历。")
    out = core.tv_sync(c, TV, write_tv=True)
    assert out["drift"]["text_changed"] == 1 and out["drift"]["relinked"] == 0 and not c.tv_calls["clear"]
    after = _link(c, "n1")
    assert after["tv_text_fp"] != before["tv_text_fp"]
    assert {k: v for k, v in after.items() if k != "tv_text_fp"} == {k: v for k, v in before.items() if k != "tv_text_fp"}
    d = _diff(c, "n1")
    assert d["changed"] is True and d["added_count"] == 1 and d["computed_at"] == OLD_TS, \
        "同一版重算不推 computed_at: 没看过的照样进摘要, 看过的不重放"

    quiet = core.tv_sync(c, TV, write_tv=True)
    assert quiet["drift"]["text_changed"] == 0 and quiet["links_written"] == 0


def test_hashtag_or_punctuation_edits_write_nothing():
    notes = [_note("n1", "秋招投了六十份简历没回音", BODY_A)]
    c = _tv(notes)
    core.tv_sync(c, TV, write_tv=True)
    calls = len(c.calls)
    notes[0]["raw_content"] = notes[0]["raw_content"].replace(", ", "，") + " #秋招"
    out = core.tv_sync(c, TV, write_tv=True)
    assert not any(v for k, v in out["drift"].items() if k not in ("samples", "relink_max"))
    writes = [x for x in c.calls[calls:] if x["op"] in ("update", "upsert", "insert", "delete")]
    assert writes == [], writes


def test_a_legacy_link_gets_its_fingerprint_or_moves_on_the_first_night():
    legacy = {"note_id": "n1", "tv_project_id": TV, "project_id": P1, "version_id": "v1", "item_id": "i1",
              "match_kind": "body_exact", "score": 1.0, "lag_days": 2, "updated_at": OLD_TS,
              "synced_to_tv_at": OLD_TS}
    notes = [_note("n1", "秋招投了六十份简历没回音", BODY_A)]
    c = _tv(notes, links=[dict(legacy)])
    out = core.tv_sync(c, TV)
    l = _link(c, "n1")
    assert out["drift"]["seeded"] == 1 and l["tv_text_fp"] == tvlink.text_fp(tvlink.Note.from_row(notes[0]))
    assert l["updated_at"] == OLD_TS and l["synced_to_tv_at"] == OLD_TS, "只记指纹, 对照本身没动"

    notes = [_note("n1", "秋招投了六十份简历没回音", BODY_TENNIS)]
    c = _tv(notes, links=[dict(legacy)])
    out = core.tv_sync(c, TV)
    assert out["drift"]["relinked"] == 1 and _link(c, "n1")["match_kind"] == "ingested", \
        "TV 里没有我们的血缘(没回填过): 不用撤回, 第一晚就挪"


# ── 不挪的 ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rematch", [False, True])
def test_links_a_person_decided_are_never_touched(rematch):
    pinned = {"note_id": "n1", "tv_project_id": TV, "project_id": P1, "version_id": "v1", "item_id": "i1",
              "match_kind": "body_exact", "score": 1.0, "lag_days": 2, "updated_at": OLD_TS,
              "synced_to_tv_at": OLD_TS, "candidates": [{"note": "D-129 2026-10-26: 人定的"}]}
    copy = {"note_id": "n2", "tv_project_id": TV, "project_id": P1, "version_id": "v2", "item_id": "i2",
            "match_kind": "ingested", "score": None, "lag_days": None, "updated_at": OLD_TS,
            "candidates": [{"note": "D-129 2026-10-26: 改指到补录副本"}]}
    notes = [_note("n1", "x", BODY_TENNIS, source_autowriter_version_id="v1"), _note("n2", "y", BODY_NEW)]
    c = _tv(notes, links=[dict(pinned), dict(copy)])
    out = core.tv_sync(c, TV, rematch=rematch, write_tv=True)
    assert out["counts"]["already_linked"] == 2
    assert [_link(c, "n1"), _link(c, "n2")] == [pinned, copy] and not c.tv_calls["clear"]


def test_a_tv_lineage_link_whose_text_was_replaced_is_reported_not_moved():
    notes = [_note("n1", "秋招投了六十份简历没回音", BODY_A, source_autowriter_version_id="v1")]
    c = _tv(notes)
    core.tv_sync(c, TV, write_tv=True)
    assert _link(c, "n1")["match_kind"] == "tv_lineage" and _diff(c, "n1")["diff_rev"] == tvlink.DIFF_REV
    _replace(notes, "n1", BODY_TENNIS)
    out = core.tv_sync(c, TV, write_tv=True)
    assert out["drift"]["blocked"] == 1 and out["drift"]["samples"][0]["old_kind"] == "tv_lineage"
    assert _link(c, "n1")["version_id"] == "v1" and _diff(c, "n1")["diff_rev"] == 0 and not c.tv_calls["clear"]


@pytest.mark.parametrize("rematch", [False, True])
def test_an_emptied_tv_body_leaves_the_link_alone(rematch):
    notes = [_note("n1", "秋招投了六十份简历没回音", BODY_A)]
    c = _tv(notes)
    core.tv_sync(c, TV, write_tv=True)
    before = dict(_link(c, "n1"))
    notes[0]["raw_content"] = "【标题】秋招投了六十份简历没回音 【正文】 #跑步"
    out = core.tv_sync(c, TV, rematch=rematch, write_tv=True)
    assert out["drift"]["no_body_kept"] == 1 and _link(c, "n1") == before and not c.tv_calls["clear"]


def test_a_weak_title_match_that_rematches_to_the_same_version_is_kept_then_quiet():
    """10-27 生产库 TUGE 那条: title_exact、当初就只有 0.076 的包含度。重对还是同一版 —— 留着、记指纹, 第二晚不再动。"""
    legacy = {"note_id": "n1", "tv_project_id": TV, "project_id": P1, "version_id": "v1", "item_id": "i1",
              "match_kind": "title_exact", "score": 0.076, "lag_days": 2, "updated_at": OLD_TS}
    notes = [_note("n1", "秋招投了六十份", BODY_TENNIS)]
    c = _tv(notes, links=[dict(legacy)])
    out = core.tv_sync(c, TV)
    assert out["drift"]["kept_after_rematch"] == 1 and out["drift"]["relinked"] == 0
    assert _link(c, "n1")["version_id"] == "v1" and _link(c, "n1")["tv_text_fp"]
    again = core.tv_sync(c, TV)
    assert again["links_written"] == 0 and again["drift"]["kept_after_rematch"] == 0


def test_rematch_that_cannot_tell_two_versions_apart_keeps_the_old_one():
    notes = [_note("n1", "秋招投了六十份简历没回音", BODY_A)]
    c = _tv(notes)
    core.tv_sync(c, TV)
    _add_version(c, "v1b", "i1b", "秋招投了六十份(副本)", BODY_A)    # 同正文的第二版: 分不出
    out = core.tv_sync(c, TV, rematch=True)
    assert out["drift"]["kept_after_rematch"] == 1 and _link(c, "n1")["version_id"] == "v1", \
        "以前: 重对成 ambiguous, 已经对上的版本丢了"


def test_a_version_outside_the_index_is_fetched_and_an_unfetchable_one_waits(monkeypatch):
    old = {"note_id": "n1", "tv_project_id": TV, "project_id": P1, "version_id": "v-old", "item_id": "i1",
           "match_kind": "fuzzy", "score": 0.7, "lag_days": 2, "updated_at": OLD_TS}
    notes = [_note("n1", "秋招投了六十份简历没回音", BODY_TENNIS)]
    c = _tv(notes, links=[dict(old)])
    monkeypatch.setattr(store, "versions_in_project", lambda sb, pid, ids: (_ for _ in ()).throw(RuntimeError("502")))
    out = core.tv_sync(c, TV)
    assert out["drift"]["unverifiable"] == 1 and _link(c, "n1")["version_id"] == "v-old"
    assert not _link(c, "n1").get("tv_text_fp"), "取不到就不记指纹, 明晚再核"

    monkeypatch.setattr(store, "versions_in_project",
                        lambda sb, pid, ids: {"v-old": {"title": "秋招投了六十份", "body": BODY_A}})
    out = core.tv_sync(c, TV)
    assert out["drift"]["relinked"] == 1 and _link(c, "n1")["match_kind"] == "ingested"


# ── store / 迁移 ──────────────────────────────────────────────────────────────────────────────

class _FailOnEq:
    def __init__(self, fail_at):
        self.calls, self.fail_at = 0, fail_at

    def table(self, _name):
        return self

    def update(self, _payload):
        return self

    def eq(self, _col, _val):
        return self

    def execute(self):
        self.calls += 1
        if self.calls == self.fail_at:
            raise RuntimeError("timeout")

        class _R:
            data = [{}]
        return _R()


def test_set_fp_reports_what_was_written_before_a_failure():
    fps = {f"n{i}": "f" for i in range(5)}
    with pytest.raises(store.PartialWriteError) as got:
        store.tv_links_set_fp(_FailOnEq(fail_at=3), fps)
    assert got.value.done == 2
    with pytest.raises(RuntimeError) as first:
        store.tv_links_set_fp(_FailOnEq(fail_at=1), fps)
    assert not isinstance(first.value, store.PartialWriteError)


def test_an_fp_write_failure_is_reported_and_fails_the_cli(monkeypatch, capsys):
    legacy = {"note_id": "n1", "tv_project_id": TV, "project_id": P1, "version_id": "v1", "item_id": "i1",
              "match_kind": "body_exact", "score": 1.0, "lag_days": 2}
    c = _tv([_note("n1", "秋招投了六十份简历没回音", BODY_A)], links=[legacy])
    monkeypatch.setattr(store, "tv_links_set_fp",
                        lambda *a: (_ for _ in ()).throw(store.PartialWriteError(0, RuntimeError("timeout"))))
    monkeypatch.setattr(core, "sb", lambda: c)
    assert cli.main(["tv-sync", "--tv-project", TV]) == 1
    assert "正文指纹没记上" in capsys.readouterr().out


def test_clear_lineage_returns_the_cleared_note_ids():
    c = _tv([_note("n1", "t", BODY_A, source_autowriter_version_id="v1", source_autowriter_item_id="i1")])
    assert store.tv_clear_lineage(c, []) == set() and not c.tv_calls["clear"]
    got = store.tv_clear_lineage(c, [{"note_id": "n1", "version_id": "v1", "item_id": "i1"},
                                     {"note_id": "n2", "version_id": None}])
    assert got == {"n1"} and c.tv_calls["clear"][0] == [{"note_id": "n1", "version_id": "v1", "item_id": "i1"}]


def test_migration_017_adds_the_column_and_the_rpc_in_both_places():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for f in ("migrations/017_tv_link_text_fp.sql", "migrations/000_baseline.sql"):
        sql = (root / f).read_text(encoding="utf-8")
        assert "ADD COLUMN IF NOT EXISTS tv_text_fp TEXT" in sql, f
        assert "FUNCTION autowriter.deskcore_tv_clear_lineage(_links JSONB)" in sql, f
        assert "IS NOT DISTINCT FROM l.item_id" in sql, f
        assert "GRANT EXECUTE ON FUNCTION autowriter.deskcore_tv_clear_lineage(JSONB) TO service_role" in sql, f


def test_doctor_reports_017_missing_when_the_column_is_absent():
    from tests.test_migration_doctor import _ALL_RPCS, _TABLES
    from tests.fakes import FakeClient
    sb = FakeClient(rows=_TABLES, rpc_impl=_ALL_RPCS, missing_columns={"tv_note_links": {"tv_text_fp"}})
    rep = core.migration_state(sb)
    assert "017_tv_link_text_fp.sql" in rep["missing"]
    st = {c["probe"]: c["state"] for c in rep["checks"]}
    assert st["tv_note_links 的 tv_text_fp 列"] == "missing" and st["deskcore_tv_clear_lineage"] == "applied"


# ── review on aw#128 ──────────────────────────────────────────────────────────────────────────

def test_a_title_only_edit_is_never_a_replacement_even_for_a_title_only_draft():
    """HATHERINE 59 条 title_exact 连的是只有标题的稿: 指纹里标题一改, 以前 still_same 只能比标题、判成换了一篇,
    挪成 ingested / unmatched, 对的那条对照就丢了。现在: 指纹的正文那一半没变 = 只改了标题; 标题改一两个字也还是那一篇。"""
    notes = [_note("n3", "我的跑步第一课", BODY_TENNIS)]
    c = _tv(notes)
    _add_version(c, "v3", "i3", "我的跑步第一课", "")
    core.tv_sync(c, TV, write_tv=True)
    assert (_link(c, "n3")["match_kind"], _link(c, "n3")["version_id"]) == ("title_exact", "v3")

    _replace(notes, "n3", BODY_TENNIS, title="我的跑步第一节课")             # 只改标题
    out = core.tv_sync(c, TV, write_tv=True)
    assert out["drift"]["text_changed"] == 1 and out["drift"]["replaced"] == 0 and not c.tv_calls["clear"]

    _replace(notes, "n3", BODY_TENNIS + "补一句: 第二节课学反手。", title="我的跑步第一课")   # 标题改回、正文也改
    out = core.tv_sync(c, TV, write_tv=True)
    assert out["drift"]["text_changed"] == 1 and out["drift"]["replaced"] == 0
    assert _link(c, "n3")["version_id"] == "v3" and _lineage(notes, "n3") == ("v3", "i3")


def test_still_same_accepts_a_lightly_edited_title_for_a_title_only_draft():
    title_only = _v("我的跑步第一课", "")
    assert tvlink.still_same(_n("我的跑步第一节课", BODY_TENNIS), title_only)
    assert not tvlink.still_same(_n("周末去打网球", BODY_TENNIS), title_only)
    assert tvlink.body_unchanged(tvlink.text_fp(_n("甲标题", BODY_A)), tvlink.text_fp(_n("乙标题", BODY_A)))
    assert not tvlink.body_unchanged(None, tvlink.text_fp(_n("甲标题", BODY_A)))


@pytest.mark.parametrize("rematch", [False, True])
def test_a_tv_lineage_link_stays_blocked_until_tv_changes_its_lineage(rematch):
    notes = [_note("n1", "秋招投了六十份简历没回音", BODY_A, source_autowriter_version_id="v1")]
    c = _tv(notes)
    core.tv_sync(c, TV, write_tv=True)
    _replace(notes, "n1", BODY_C, title="自我介绍删了前三句")
    for _ in range(2):
        out = core.tv_sync(c, TV, rematch=rematch, write_tv=True)
        assert out["drift"]["blocked"] == 1 and _link(c, "n1")["version_id"] == "v1"
        assert _diff(c, "n1")["diff_rev"] == 0 and _link(c, "n1")["tv_text_fp"] != tvlink.text_fp(
            tvlink.Note.from_row(notes[0])), "不记指纹: 每晚都报"
    notes[0]["source_autowriter_version_id"] = None                          # TV 那边把 lineage 清了
    out = core.tv_sync(c, TV, rematch=rematch, write_tv=True)
    assert (_link(c, "n1")["match_kind"], _link(c, "n1")["version_id"]) == ("body_exact", "v2")
    assert _lineage(notes, "n1") == ("v2", "i2") and not c.tv_calls["clear"]


def test_an_empty_body_night_does_not_bring_a_quarantined_diff_back():
    notes = [_note("n1", "秋招投了六十份简历没回音", BODY_A)]
    c = _tv(notes)
    core.tv_sync(c, TV, write_tv=True)
    _replace(notes, "n1", BODY_TENNIS)
    core.tv_sync(c, TV)                                                      # 没开 --write-tv: 不挪, 差异隔离
    assert _diff(c, "n1")["diff_rev"] == 0
    notes[0]["raw_content"] = "【标题】秋招投了六十份简历没回音 【正文】 #跑步"
    core.tv_sync(c, TV)
    d = _diff(c, "n1")
    assert d["diff_rev"] == 0 and d.get("published_chars") != 0, "不拿空正文算差异"


def test_a_link_held_over_the_cap_is_not_backfilled_with_its_known_stale_version(monkeypatch):
    notes = [_note("n1", "秋招投了六十份简历没回音", BODY_A), _note("n2", "自我介绍删了前三句", BODY_C)]
    c = _tv(notes)
    core.tv_sync(c, TV)                                                      # 没开 --write-tv: TV 空着, 对照没回填
    _replace(notes, "n1", BODY_TENNIS, title="第一次打网球")
    _replace(notes, "n2", BODY_NEW, title="上周发的那篇")
    monkeypatch.setenv("TV_SYNC_RELINK_MAX", "1")
    out = core.tv_sync(c, TV, write_tv=True)
    assert out["drift"]["over_cap"] == 2 and out["tv_backfilled"] == 0
    assert _lineage(notes, "n1") == (None, None) and _lineage(notes, "n2") == (None, None)


def test_the_first_fingerprint_also_recomputes_a_diff_made_before_the_text_was_edited():
    legacy = {"note_id": "n1", "tv_project_id": TV, "project_id": P1, "version_id": "v1", "item_id": "i1",
              "match_kind": "body_exact", "score": 1.0, "lag_days": 2, "updated_at": OLD_TS}
    notes = [_note("n1", "秋招投了六十份简历没回音", BODY_A)]
    c = _tv(notes, links=[dict(legacy)])
    core.tv_sync(c, TV, rematch=True)                                        # 造一条按旧正文算的差异
    c.rows["tv_note_links"] = [dict(legacy)]                                 # 回到 017 之前: 没指纹
    _diff(c, "n1")["computed_at"] = OLD_TS
    assert _diff(c, "n1")["added_count"] == 0
    _replace(notes, "n1", BODY_A + "最后补一句: 简历第一行写你最硬的那段经历。")    # 017 上线前运营加了一句
    out = core.tv_sync(c, TV)
    assert out["drift"]["seeded"] == 1
    d = _diff(c, "n1")
    assert d["added_count"] == 1 and d["computed_at"] == OLD_TS


def test_rematch_keeps_a_link_whose_version_is_no_longer_the_items_best(monkeypatch):
    notes = [_note("n1", "秋招投了六十份简历没回音", BODY_A)]
    c = _tv(notes)
    core.tv_sync(c, TV, write_tv=True)
    item = next(i for i in c.rows["items"] if i["id"] == "i1")
    item["best_version_id"] = "v1new"                                       # 写手后来又存了一版当 best
    item["versions"].append({"id": "v1new", "title": "秋招投了六十份", "body": BODY_TENNIS,
                             "version_num": 2, "created_at": _iso(5)})
    monkeypatch.setattr(store, "versions_in_project",
                        lambda sb, pid, ids: {"v1": {"title": "秋招投了六十份", "body": BODY_A}} if "v1" in ids else {})
    out = core.tv_sync(c, TV, rematch=True, write_tv=True)
    assert out["drift"]["kept_after_rematch"] == 1 and out["drift"]["relinked"] == 0
    assert _link(c, "n1")["version_id"] == "v1" and _lineage(notes, "n1") == ("v1", "i1") and not c.tv_calls["clear"]


def test_a_failed_diff_write_holds_the_fingerprint_so_the_recompute_is_retried(monkeypatch):
    notes = [_note("n1", "秋招投了六十份简历没回音", BODY_A)]
    c = _tv(notes)
    core.tv_sync(c, TV, write_tv=True)
    _diff(c, "n1")["computed_at"] = OLD_TS
    old_fp = _link(c, "n1")["tv_text_fp"]
    _replace(notes, "n1", BODY_A + "最后补一句: 简历第一行写你最硬的那段经历。")
    real = store.tv_edit_diffs_upsert
    monkeypatch.setattr(store, "tv_edit_diffs_upsert", lambda *a: (_ for _ in ()).throw(RuntimeError("23502")))
    out = core.tv_sync(c, TV, write_tv=True)
    assert out["edit_diffs"]["error"] and out["drift"]["fp_held"] == 1
    assert _link(c, "n1")["tv_text_fp"] == old_fp, "差异没算成: 指纹先不记"
    monkeypatch.setattr(store, "tv_edit_diffs_upsert", real)
    out = core.tv_sync(c, TV, write_tv=True)
    assert out["drift"]["text_changed"] == 1 and _diff(c, "n1")["added_count"] == 1
    assert _diff(c, "n1")["computed_at"] == OLD_TS and _link(c, "n1")["tv_text_fp"] != old_fp


def test_a_cleared_lineage_comes_back_if_the_move_never_lands_and_the_text_is_restored(monkeypatch):
    notes = [_note("n1", "秋招投了六十份简历没回音", BODY_A)]
    c = _tv(notes)
    core.tv_sync(c, TV, write_tv=True)
    original = notes[0]["raw_content"]
    _replace(notes, "n1", BODY_TENNIS)
    real = store.tv_links_upsert
    monkeypatch.setattr(store, "tv_links_upsert", lambda *a: (_ for _ in ()).throw(RuntimeError("502")))
    with pytest.raises(RuntimeError):
        core.tv_sync(c, TV, write_tv=True)                                   # 撤回提交了, 挪对照没落地
    assert _lineage(notes, "n1") == (None, None) and _link(c, "n1")["version_id"] == "v1"
    monkeypatch.setattr(store, "tv_links_upsert", real)
    notes[0]["raw_content"] = original                                       # 第二天运营又改回来了
    core.tv_sync(c, TV, write_tv=True)
    assert _link(c, "n1")["version_id"] == "v1" and _lineage(notes, "n1") == ("v1", "i1"), "回填补回去"


def test_dry_run_over_the_cap_lists_every_move(monkeypatch, capsys):
    notes = [_note(f"m{i:02d}", f"标题{i:02d}", BODY_A) for i in range(12)]
    c = _tv(notes)
    core.tv_sync(c, TV, write_tv=True)
    for i, n in enumerate(notes):
        n["raw_content"] = f"【标题】网球第{i}课 【正文】{BODY_TENNIS}第{i}篇。"
    monkeypatch.setattr(core, "sb", lambda: c)
    assert cli.main(["tv-sync", "--tv-project", TV, "--write-tv", "--dry-run"]) == 1
    out = capsys.readouterr().out
    assert out.count("↻ m") == 12 and "另有" not in out
