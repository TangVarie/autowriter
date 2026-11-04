"""开头一样、后面是另一篇的不认开头(外部评审 11-04 F02, 2026-11-04)。

match_note 的开头档原来只看正文前 40 字: 一个候选就认 body_exact、分数写死 1.0, 不看时间窗; still_same(W-34)也是开头一样就
认 —— 借了一版开头的另一篇帖被连上之后永远纠不回来。这里钉住: 长帖、短帖(光开头就够 0.30 的)都不再按开头连; 标题一样、
后面重写的照旧认(人留了开头和标题把后面改了, 还是那一篇); 分数记真的包含度; 开头和后面都对得上的晚发也认; 连上之后 TV 把后面整篇
换掉, W-34 认得出来; 主索引的开头候选被拒之后, 查重底稿(W-38)能接住。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from deskcore import core, fingerprint as fp, tvlink as L

T0 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
BODY_A = ("投到第六十份的时候我开始怀疑是不是邮箱坏了, 后来发现坏的是简历第一行。"
          "改完之后一周里来了三个面试, 才知道之前的六十份全都倒在第一行上。")
# BODY_A 的开头(去标点 46 字, 盖住 PREFIX_CHARS)
OPEN = "投到第六十份的时候我开始怀疑是不是邮箱坏了, 后来发现坏的是简历第一行。改完之后一周里来了三个面试"
DOG = "家里的狗做完绝育以后胃口变得特别好, 每天早上都守着饭盆叫, 兽医说要控制零食。"
GEL = ("跑半马之前一定要试一次能量胶, 第一次吃是在第十五公里, 胃里翻了半个小时才缓过来。"
       "后来换成每五公里吃半支, 配着水一起咽下去, 到终点都没再难受过, 配速还快了二十秒。"
       "赛前一周别换新鞋, 我就是在比赛当天穿了刚买的跑鞋, 跑到第八公里脚后跟就磨出了水泡。"
       "补水站不要每个都停, 隔一个喝两口就够, 喝多了肚子晃得厉害, 反而跑不动。")
SHORT = OPEN + ", 不过这都是去年的事了。" + DOG           # 帖短: 光开头就有 0.49 的全文包含度
LONG = OPEN + "。" + GEL                                  # 帖长: 全文包含度很低
TITLE_V = "秋招投了六十份"


def _v(vid, title, body, days=0):
    return L.Version(version_id=vid, project_id="p1", title=title, body=body,
                     created_at=T0 + timedelta(days=days), item_id=f"item-{vid}")


def _n(title, body, days=2):
    return L.Note(note_id="n1", title=title, body=L.strip_hashtags(body), publish_time=T0 + timedelta(days=days))


def _idx(*vs):
    return L.VersionIndex(vs or [_v("v1", TITLE_V, BODY_A)])


def test_texts_share_the_opening_and_the_short_one_clears_the_full_text_floor():
    # 前提: 开头 40 字一样; 短帖光开头就过了 EDITED_FLOOR —— 只看全文包含度拦不住它, 要看开头之后那段
    assert L.prefix_key(SHORT) == L.prefix_key(BODY_A) == L.prefix_key(LONG)
    assert L.overlap(_n("x", SHORT), _v("v1", TITLE_V, BODY_A)) > L.EDITED_FLOOR
    assert L.overlap(_n("x", LONG), _v("v1", TITLE_V, BODY_A)) < L.EDITED_FLOOR


@pytest.mark.parametrize("body", [SHORT, LONG], ids=["short", "long"])
def test_same_opening_different_post_and_title_is_not_body_exact(body):
    m = L.match_note(_n("狗狗绝育之后", body), _idx())
    assert m.kind == "unmatched" and m.version is None, "只借了开头: 不按开头连, 也不让内容那一路换个名目连回去"


def test_same_opening_and_title_with_a_rewritten_tail_stays_linked_and_records_its_real_score():
    # 人留了开头和标题、把后面整段重写(生产库那条只剩开头一样的就是这样): 还是那一篇
    m = L.match_note(_n(TITLE_V, SHORT), _idx())
    assert m.kind == "body_exact" and m.version.version_id == "v1"
    assert m.score == pytest.approx(L.overlap(_n(TITLE_V, SHORT), _v("v1", TITLE_V, BODY_A)))
    assert m.score < 1.0, "分数记真的包含度, 不再写死 1.0"


def test_a_normal_trim_with_a_new_title_is_still_body_exact():
    # 改了标题、删了后半句: 开头之后那段大半还在, 照旧认
    trimmed = BODY_A.replace(", 才知道之前的六十份全都倒在第一行上", ", 原来六十份全倒在第一行上")
    m = L.match_note(_n("改了标题的发布稿", trimmed), _idx())
    assert m.kind == "body_exact" and m.version.version_id == "v1" and 0.5 < m.score < 1.0


def test_identical_text_still_scores_one():
    m = L.match_note(_n("别的标题", BODY_A), _idx())
    assert m.kind == "body_exact" and m.score == 1.0


@pytest.mark.parametrize("days", [29, 31, 60])
def test_a_late_publication_with_the_same_opening_and_tail_is_still_body_exact(days):
    # 复审 11-04: 开头和后面都对得上的就是那一篇, 晚发 31 / 60 天、改了标题也是 —— 开头档不按时间窗筛
    # (筛掉的话内容那一路也看窗口, 它会变成 unmatched 被补录成副本)。时间窗只在几版分不出时用
    m = L.match_note(_n("改了标题的发布稿", BODY_A, days=days), _idx())
    assert m.kind == "body_exact" and m.version.version_id == "v1" and m.score == 1.0


def test_a_borrowed_opening_stays_rejected_outside_the_window_too():
    m = L.match_note(_n("狗狗绝育之后", LONG, days=60), _idx())
    assert m.kind == "unmatched" and m.version is None


def test_tail_check_needs_enough_text_on_both_sides():
    # 开头之后太短(不够 FUZZY_MIN_GRAMS 个四字串)的不判, 照旧按开头认
    v = _v("v1", TITLE_V, OPEN + "。")
    m = L.match_note(_n("狗狗绝育之后", SHORT), _idx(v))
    assert m.kind == "body_exact" and m.version.version_id == "v1"
    assert not L.prefix_tail_differs(_n("别的", OPEN + "。就这样"), _v("v1", TITLE_V, BODY_A))


def test_still_same_catches_a_post_that_kept_only_the_opening():
    v = _v("v1", TITLE_V, BODY_A)
    assert not L.still_same(_n("狗狗绝育之后", SHORT), v), "开头一样、后面和标题都换了: 整篇换了"
    assert not L.still_same(_n("狗狗绝育之后", LONG), v)
    assert L.still_same(_n(TITLE_V, SHORT), v), "标题还在: 还是那一篇"
    assert L.still_same(_n("改了标题", BODY_A), v)


# ── tv-sync: W-34 认得出来; 底稿接得住 ───────────────────────────────────────────────────────────

@pytest.fixture
def _no_embeddings(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)


def test_w34_flags_a_linked_post_whose_tail_was_replaced(_no_embeddings):
    from tests.test_tv_sync_drift import _link, _replace, _tv
    from tests.test_tv_sync import _note
    notes = [_note("n1", "秋招投了六十份简历没回音", BODY_A)]
    c = _tv(notes)
    core.tv_sync(c, "SPX_phase1", write_tv=True)
    assert _link(c, "n1")["version_id"] == "v1"

    _replace(notes, "n1", SHORT, title="狗狗绝育之后")
    out = core.tv_sync(c, "SPX_phase1", write_tv=True)
    assert out["drift"]["replaced"] == 1 and out["drift"]["relinked"] == 1
    l = _link(c, "n1")
    assert l["version_id"] != "v1" and l["match_kind"] == "ingested", "按内容重对: 对不上就补录, 不再挂在 v1 上"


def test_a_snapshot_wins_when_the_main_index_opening_candidate_is_rejected(_no_embeddings):
    from tests.test_draft_snapshots import _client, _links, _note, _snap
    post = OPEN + "。" + GEL
    snaps = [_snap("s1", "半马能量胶怎么吃", post, days=0)]
    c = _client([_note("n1", "半马能量胶怎么吃", post, days=2)], snaps)
    out = core.tv_sync(c, "TUGE_phase1")
    l1 = _links(c)["n1"]
    vid = core._snapshot_version_id(c.rows["projects"][0]["id"], snaps[0]["text_fp"])
    assert l1["version_id"] == vid and l1["version_id"] != "v1", "存档那一版只是开头一样, 来源是底稿"
    assert out["snapshots"]["matched_notes"] == 1
    assert fp.opening_hash(post) == fp.opening_hash(BODY_A), "前提: 两篇的开头指纹也一样"
