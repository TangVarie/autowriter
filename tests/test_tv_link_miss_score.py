"""对不上的帖也记一个分数(2026-11-11, 审计 F4): tv_note_links.score 在 unmatched / ingested 行上原来一律是 NULL。

接住率不动的时候分不出两种情况: 帖是拿写作台的稿子改的(改的地方值得看), 还是根本在别处写的(版式、规则怎么调都动不了它)。
match_note / match_snapshot 对不上时把算过的最高包含度带出来, tv-sync 记进那一行的 score。只是读数: 哪一条走补录、
连到哪一版都不变。文字全是编的。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from deskcore import core, store, tvlink as L
from tests import test_draft_snapshots as S
from tests import test_tv_sync as T
from tests.test_tvlink import BODY_A, BODY_B, _n, _v


@pytest.fixture(autouse=True)
def _no_embeddings(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)


OUTSIDE = "这一篇是在别的地方写的, 和写作台里存着的哪一版都没有关系, 讲的是周末去郊外露营忘了带防潮垫。" * 2
# 够长的一段别处写的文字(四字串是集合, 重复不算数, 所以每句都不一样): 后面接一句写作台的话, 包含度落在 0 和 EDITED_FLOOR 之间
FILLER = ("周末去郊外露营忘了带防潮垫, 半夜被地上的潮气冻醒, 只好把背包里的雨衣铺在睡袋下面。"
          "第二天早上隔壁帐篷的大叔煮了一锅姜茶分给我们, 说他第一次露营也是这样熬过来的。"
          "回家以后列了一张清单贴在冰箱门上, 头灯、备用电池、防潮垫、驱蚊水, 一样一样打勾。"
          "下个月打算换一个离水源近一点的营地, 省得每次打水都要来回走上二十分钟的山路。")


# ── tvlink: 对不上也带分数 ─────────────────────────────────────────────────────────────

def test_an_unmatched_note_carries_its_best_containment():
    idx = L.VersionIndex([_v("v1", "秋招投了六十份", BODY_A), _v("v2", "群面被刷三次", BODY_B)])
    m = L.match_note(_n("n1", "别的标题", OUTSIDE), idx)
    assert m.kind == "unmatched" and m.version is None and m.score == 0.0
    # 借了一句写作台的话、别的全是自己写的: 还是对不上, 分数是最像那一版的包含度(和 overlap 同一个口径)
    sent = BODY_B.split("。")[0] + "。"
    note = _n("n2", "别的标题", FILLER + sent)
    m = L.match_note(note, idx)
    assert m.kind == "unmatched"
    assert m.score == pytest.approx(L.overlap(note, _v("v2", "群面被刷三次", BODY_B))) and 0 < m.score < L.EDITED_FLOOR


def test_a_same_title_hold_keeps_the_highest_containment_in_the_window():
    """2026-11-12 复审: 标题和某一版一样、正文却对不上的帖(同标题候选, 等人看), 分数原来只取同标题那几版里最高的 —— 它的字
    有一截出自另一篇不同标题的稿子时被记成接近 0, 读数上就成了「在别处写的」。记窗口里最高的那个。"""
    v1, v2 = _v("v1", "秋招投了六十份", BODY_A), _v("v2", "群面被刷三次", BODY_B)
    idx = L.VersionIndex([v1, v2])
    sent = BODY_B.split("。")[0] + "。"
    note = _n("n1", "秋招投了六十份", FILLER + sent)
    m = L.match_note(note, idx)
    assert m.kind == "ambiguous" and m.version is None
    assert L.overlap(note, v1) < L.overlap(note, v2) < L.EDITED_FLOOR
    assert m.score == pytest.approx(L.overlap(note, v2))


def test_too_short_to_compare_has_no_score_and_an_empty_window_scores_zero():
    idx = L.VersionIndex([_v("v1", "秋招投了六十份", BODY_A)])
    assert L.match_note(_n("n1", "短", "太短了"), idx).score is None, "没得比, 不是 0"
    far = L.match_note(_n("n1", "别的标题", BODY_A[3:] + "后面补了一句。", days=200), idx)
    assert far.kind == "unmatched" and far.score == 0.0, "时间窗里一版都没有"
    assert L.match_note(_n("n1", "别的标题", OUTSIDE), L.VersionIndex([])).score == 0.0


def test_a_borrowed_opening_still_counts_towards_the_score():
    """F02: 只借了开头的那一版不连(也不让内容那一路连回去), 可帖里有多少字出自写作台照实记 —— 这个数可以高过 EDITED_FLOOR。"""
    from tests.test_tvlink_prefix_tail import BODY_A as V1_BODY, SHORT, TITLE_V, _idx, _n as _pn, _v as _pv
    note = _pn("狗狗绝育之后", SHORT)
    m = L.match_note(note, _idx())
    assert m.kind == "unmatched" and m.version is None
    assert m.score == pytest.approx(L.overlap(note, _pv("v1", TITLE_V, V1_BODY))) and m.score >= L.EDITED_FLOOR


def test_an_unmatched_snapshot_result_carries_its_best_containment_too():
    # 约四成来自底稿: 不到转正的线(SNAPSHOT_EDITED_FLOOR), 对不上, 但分数说得出它是改出来的
    note_body = S.BODY_S.split("。")[0] + "。" + S.BODY_OTHER
    m = S._m(S._n("t", note_body), [S._v("s1", "t2", S.BODY_S, 0)])
    assert m.kind == "unmatched" and m.score == pytest.approx(S._contain(note_body, S.BODY_S))
    assert L.EDITED_FLOOR <= m.score < L.SNAPSHOT_EDITED_FLOOR
    assert S._m(S._n("t", S.BODY_OTHER), [S._v("s1", "t2", S.BODY_S, 0)]).score == 0.0
    assert S._m(S._n("t", S.BODY_OTHER), []).score == 0.0
    assert S._m(S._n("t", "太短了"), [S._v("s1", "t2", S.BODY_S, 0)]).score is None


# ── tv-sync: 记进 tv_note_links.score ──────────────────────────────────────────────────

def _links(c):
    return {l["note_id"]: l for l in c.rows["tv_note_links"]}


def test_ingested_and_unmatched_rows_store_the_score_and_matched_rows_are_unchanged():
    borrowed = FILLER + T.BODY_A.split("。")[0] + "。"
    notes = T.NOTES + [T._note("n4", "借了一句", borrowed),
                       {"note_id": "n5", "raw_content": "【标题】只有标题", "publish_time": T._iso(1), "tier": "趴",
                        "created_at": T._iso(1)}]
    c = T._client(notes)
    out = core.tv_sync(c, T.TV)
    links = _links(c)
    assert links["n2"]["match_kind"] == "ingested" and links["n2"]["score"] == 0.0
    assert links["n4"]["match_kind"] == "ingested" and 0 < links["n4"]["score"] < L.EDITED_FLOOR
    assert links["n4"]["score"] == round(links["n4"]["score"], 3)
    assert links["n1"]["match_kind"] == "body_exact" and links["n1"]["score"] == 1.0
    assert links["n3"]["match_kind"] == "tv_lineage" and links["n3"]["score"] is None
    assert links["n5"]["match_kind"] == "unmatched" and links["n5"]["score"] is None, "没正文: 没得比"
    assert out["counts"]["unmatched"] == 2 and out["ingest"]["minted"] == 2, "只是多记一个数, 判定和补录不变"

    c = T._client(notes, with_target=False)                # 没有补录目标: 留 unmatched, 分数照记
    core.tv_sync(c, T.TV)
    links = _links(c)
    assert links["n2"]["match_kind"] == "unmatched" and links["n2"]["score"] == 0.0
    assert links["n4"]["match_kind"] == "unmatched" and 0 < links["n4"]["score"] < L.EDITED_FLOOR


def test_an_old_unmatched_row_without_a_score_is_rewritten_once_then_left_alone():
    """W-10: 上次就是同样的 unmatched 的不每晚重写。以前记的没有分数 —— 头一晚重写一次把分数补上, 之后照旧不写
    (存回来的 REAL 带单精度的尾巴, 不能因此每晚都算「变了」)。"""
    old = {"note_id": "n2", "tv_project_id": T.TV, "project_id": None, "version_id": None, "item_id": None,
           "match_kind": "unmatched", "score": None, "lag_days": None, "candidates": None,
           "updated_at": "2026-11-01T14:02:00+00:00"}
    borrowed = FILLER + T.BODY_A.split("。")[0] + "。"
    c = T._client([T._note("n2", "借了一句", borrowed)], with_target=False, links=[dict(old)])
    first = core.tv_sync(c, T.TV)
    row = _links(c)["n2"]
    assert first["unmatched_kept"] == 0 and first["links_written"] == 1
    assert 0 < row["score"] < L.EDITED_FLOOR and row["updated_at"] != old["updated_at"]
    row["score"] = float(f"{row['score'] + 3e-8:.9f}")       # 库里是 REAL: 读回来不是原来那个数
    stamp = row["updated_at"]
    again = core.tv_sync(c, T.TV)
    assert again["unmatched_kept"] == 1 and again["links_written"] == 0
    assert _links(c)["n2"]["updated_at"] == stamp


def test_same_miss_score():
    assert core._same_miss_score(None, None)
    assert not core._same_miss_score(None, 0.0) and not core._same_miss_score(0.0, None)
    assert core._same_miss_score(0.123000003, 0.1234) and core._same_miss_score("0.123", 0.1226)
    assert not core._same_miss_score(0.123, 0.125) and not core._same_miss_score("x", 0.1)


def test_a_post_rewritten_from_an_unarchived_draft_scores_against_the_snapshot():
    """只查过重、没存档的稿子也是写作台的稿子: 帖里四成来自那份底稿(不到转正的线, 照常补录), 分数记的是这四成,
    不是和存过档的版本比出来的 0。"""
    note_body = S.BODY_S.split("。")[0] + "。" + S.BODY_OTHER
    c = S._client([S._note("n1", "换了标题", note_body)], [S._snap("s1", "骑了一百公里", S.BODY_S)])
    out = core.tv_sync(c, S.TV)
    l = _links(c)["n1"]
    assert l["match_kind"] == "ingested" and out["snapshots"]["promoted"] == 0
    assert L.EDITED_FLOOR <= l["score"] < L.SNAPSHOT_EDITED_FLOOR
    assert l["score"] == pytest.approx(S._contain(note_body, S.BODY_S), abs=1e-3)


def test_a_note_held_for_a_snapshot_keeps_the_snapshot_score(monkeypatch):
    # 对上了底稿、这晚没转成版本: 记 unmatched 等明晚, 分数是和那份底稿的包含度(接近 1), 不会被读成「在别处写的」
    monkeypatch.setattr(store, "mint_draft_identity",
                        lambda *a, **k: {"batch_id": None, "versions": {}, "error": "RuntimeError: db down"})
    c = S._client([S._note("n1", "骑了一百公里", S.BODY_S)], [S._snap("s1", "骑了一百公里", S.BODY_S)])
    core.tv_sync(c, S.TV)
    l = _links(c)["n1"]
    assert l["match_kind"] == "unmatched" and l["version_id"] is None and l["score"] == 1.0


# ── 一次性给对不上的旧对照补分数(2026-11-12, `tv-sync --rescore-misses`) ─────────────────────────────
# 11-11 之前补录的对照 score 都是空的, 而补录的对照是钉住的 —— 每晚的同步不碰它们(tests/test_tv_sync_drift.py 守着)。
# 所以另有一个手动跑的命令: 按同一个口径算, 只写 score 一列。

OLD_TS = "2026-09-01T00:00:00+00:00"


def _old(nid, kind="ingested", *, score=None, version="vcopy", **extra):
    return {"note_id": nid, "tv_project_id": T.TV, "project_id": T.P1, "version_id": version, "item_id": None,
            "match_kind": kind, "score": score, "lag_days": None, "candidates": [{"note": "D-129: 人定过的"}],
            "updated_at": OLD_TS, "matched_at": OLD_TS, "synced_to_tv_at": None, "tv_text_fp": "0" * 16, **extra}


def _rescore_fixture():
    borrowed = FILLER + T.BODY_A.split("。")[0] + "。"
    notes = [T.NOTES[0],                                                    # n1: 对上了写作台的稿
             T._note("n2", "上周发的那篇", T.BODY_NEW),                       # 别处写的
             T._note("n4", "借了一句", borrowed),                             # 借了写作台一句话
             {"note_id": "n5", "raw_content": "【标题】只有标题", "publish_time": T._iso(1), "tier": "趴",
              "created_at": T._iso(1)},                                     # 没正文
             T._note("n6", "太短", "太短了"),                                 # 太短没得比
             T._note("n7", "已经有分数的", borrowed + "又多写了一句别的。")]     # 11-11 之后记过分数的
    links = [_old("n1", "body_exact", score=1.0, version="v1", lag_days=2), _old("n2"), _old("n4", version="vcopy4"),
             _old("n5", "unmatched", version=None), _old("n6", version="vcopy6"),
             _old("n7", "unmatched", score=0.2, version=None)]
    return T._client(notes, links=[dict(l) for l in links]), links


def test_rescore_gives_a_pinned_ingested_row_a_score_and_changes_nothing_else():
    c, links = _rescore_fixture()
    out = core.tv_rescore_misses(c, T.TV)
    got = _links(c)
    assert got["n2"]["score"] == 0.0 and 0 < got["n4"]["score"] < L.EDITED_FLOOR
    assert got["n4"]["score"] == round(got["n4"]["score"], 3)
    assert got["n5"]["score"] is None, "没正文: 没得比, 照旧是空"
    assert got["n6"]["score"] is None, "太短没得比: 照旧是空"
    assert got["n1"]["score"] == 1.0 and got["n7"]["score"] == 0.2, "对上了的、已经记过的不动"
    for was in links:                                       # 别的列一个不动: match_kind / version_id / candidates / updated_at …
        assert {k: v for k, v in got[was["note_id"]].items() if k != "score"} == {k: v for k, v in was.items() if k != "score"}
    assert (out["candidates"], out["scored"], out["written"], out["error"]) == (4, 2, 2, None)
    writes = [call for call in c.calls if call["op"] != "select"]
    assert writes and all(call["table"] == "tv_note_links" and call["op"] == "update" and set(call["payload"]) == {"score"}
                          for call in writes), "只 UPDATE score 一列; 不 upsert, 不补录, 不写指纹"
    for call in writes:
        assert ("in", "match_kind", ("ingested", "unmatched")) in call["filters"]
        assert ("is", "score", "null") in call["filters"]
    assert [name for name, _ in c.rpc_calls] == ["deskcore_tv_notes"], "不回填 TV, 不拿补录锁"
    again = core.tv_rescore_misses(c, T.TV)
    assert (again["candidates"], again["scored"], again["written"]) == (2, 0, 0), "再跑一次: 只剩没得比的两条, 不重写"


def test_rescore_dry_run_and_days():
    c, links = _rescore_fixture()
    out = core.tv_rescore_misses(c, T.TV, dry_run=True)
    assert (out["scored"], out["written"], out["dry_run"]) == (2, 0, True)
    assert not [call for call in c.calls if call["op"] != "select"] and _links(c)["n2"]["score"] is None
    out = core.tv_rescore_misses(c, T.TV, 1)                  # 夹具里的帖是 2026-09 发的: 近 1 天里一条都没有
    assert (out["candidates"], out["written"]) == (0, 0) and _links(c)["n2"]["score"] is None
    out = core.tv_rescore_misses(c, T.TV, 36500)
    assert out["written"] == 2 and _links(c)["n2"]["score"] == 0.0
    with pytest.raises(ValueError):
        core.tv_rescore_misses(c, "NOPE_phase1")


def test_set_score_cannot_touch_a_matched_row_or_overwrite_a_score_even_when_asked():
    c, links = _rescore_fixture()
    n = store.tv_links_set_score(c, {"n1": 0.5, "n7": 0.9, "n2": 0.123456, "n4": 0.123449})
    got = _links(c)
    assert n == 2 and got["n1"]["score"] == 1.0 and got["n7"]["score"] == 0.2
    assert got["n2"]["score"] == got["n4"]["score"] == 0.123
    updates = [call for call in c.calls if call["op"] == "update"]
    assert len(updates) == 3, "按值分组, 一个值一次: 0.5 和 0.9 那两次被过滤挡住、一行没改; 0.123 那次改了两行"
    assert store.tv_links_set_score(c, {}) == 0


def test_rescore_counts_an_unarchived_snapshot_like_the_nightly_score_does():
    note_body = S.BODY_S.split("。")[0] + "。" + S.BODY_OTHER
    link = {"note_id": "n1", "tv_project_id": S.TV, "project_id": S.P1, "version_id": "vcopy", "item_id": None,
            "match_kind": "ingested", "score": None, "lag_days": None, "candidates": None, "tv_text_fp": None}
    c = S._client([S._note("n1", "换了标题", note_body)], [S._snap("s1", "骑了一百公里", S.BODY_S)], links=[link])
    core.tv_rescore_misses(c, S.TV)
    assert _links(c)["n1"]["score"] == pytest.approx(S._contain(note_body, S.BODY_S), abs=1e-3)
    assert _links(c)["n1"]["match_kind"] == "ingested" and _links(c)["n1"]["version_id"] == "vcopy"


def test_the_nightly_sync_never_rescores_a_pinned_row():
    c, links = _rescore_fixture()
    core.tv_sync(c, T.TV)
    assert _links(c)["n2"]["score"] is None and _links(c)["n4"]["score"] is None


def test_a_copy_back_raises_the_score_but_not_the_origin(monkeypatch):
    """score 说的是「帖的字有多少在写作台里」, 发了之后抄回来的那版也算(这里是 1.0); 来源只认发布前就在的文字
    (回流闸自己量, tests/test_tv_sync_gate.py), 所以还是「别处写的」。"""
    from tests import test_tv_sync_gate as GT
    monkeypatch.setattr(core, "_gate_now", lambda: T.T0 + timedelta(days=10))
    c = GT._tv([T._note("n2", "上周发的那篇", T.BODY_NEW)], links=[GT._copy_link()])
    GT._add_desk_version(c, "vback", T.BODY_NEW, days=5)      # 9-12 发的帖, 9-15 才存进写作台
    core.tv_rescore_misses(c, T.TV)
    assert _links(c)["n2"]["score"] == 1.0
    core.tv_sync(c, T.TV)
    row = _links(c)["n2"]
    assert row["match_kind"] == "ingested" and row["origin"] == "elsewhere" and row["gate_reasons"]["overlap"] == 0.0


def test_cli_rescore_misses_does_not_sync(monkeypatch, capsys):
    from deskcore import cli
    c, links = _rescore_fixture()
    monkeypatch.setattr(core, "sb", lambda: c)
    monkeypatch.setattr(core, "tv_sync", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该同步")))
    assert cli.main(["tv-sync", "--tv-project", T.TV, "--rescore-misses", "--dry-run"]) == 0
    assert "[dry-run]" in capsys.readouterr().out and _links(c)["n2"]["score"] is None
    assert cli.main(["tv-sync", "--all", "--rescore-misses"]) == 0
    out = capsys.readouterr().out
    assert "对不上、还没有分数的对照 4 条" in out and "算出分数 2" in out and "写入 2" in out
    assert _links(c)["n2"]["score"] == 0.0 and len(c.rows["tv_note_links"]) == len(links)
