"""对不上的帖也记一个分数(2026-11-11, 审计 F4): tv_note_links.score 在 unmatched / ingested 行上原来一律是 NULL。

接住率不动的时候分不出两种情况: 帖是拿写作台的稿子改的(改的地方值得看), 还是根本在别处写的(版式、规则怎么调都动不了它)。
match_note / match_snapshot 对不上时把算过的最高包含度带出来, tv-sync 记进那一行的 score。只是读数: 哪一条走补录、
连到哪一版都不变。文字全是编的。
"""
from __future__ import annotations

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
