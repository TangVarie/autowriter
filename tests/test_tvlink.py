"""写作台 ↔ TV 的稿子对照 —— 匹配逻辑(deskcore/tvlink.py), 不碰库。

2026-09-17: TV 5966 条笔记里带写作台 lineage 的是 0 条; 让运营抄六个 ID 列进
飞书三周零匹配。按内容对的口径来自那天的库: 百健士 143 篇里 115 篇逐字相同、
其余多了话题标签; 发布−入库中位 1~3 天, 也有先发后补录的负值; 途鸽发之前
改得很狠(305 字 vs 527 字)。这里每条用例对应一种真实形态。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from deskcore import tvlink as L

T0 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
BODY_A = ("投到第六十份的时候我开始怀疑是不是邮箱坏了, 后来发现坏的是简历第一行。"
          "改完之后一周里来了三个面试, 才知道之前的六十份全都倒在第一行上。")
BODY_B = ("绩点这东西在群面之后就没人再提了, 面试官只问我上一段实习做成了什么。"
          "所以别再为二点八焦虑了, 把实习那段写成能量化的结果比什么都强。")


def _v(vid, title, body, days=0, project="p1"):
    return L.Version(version_id=vid, project_id=project, title=title, body=body,
                     created_at=T0 + timedelta(days=days), item_id=f"item-{vid}")


def _n(nid, title, body, days=2):
    return L.Note(note_id=nid, title=title, body=L.strip_hashtags(body),
                  publish_time=T0 + timedelta(days=days))


# ── 解析 ──────────────────────────────────────────────────────────────

def test_parse_raw_reads_tv_markers_and_falls_back_to_body_only():
    assert L.parse_raw("【标题】 这笔学费花值了 【正文】 谁懂文科生找工作的痛！\n第二行") == \
        ("这笔学费花值了", "谁懂文科生找工作的痛！\n第二行")
    assert L.parse_raw("没有标记的一段") == ("", "没有标记的一段")
    assert L.parse_raw("【标题】只有标题") == ("只有标题", "")
    # 途鸽 127 条里 7 条是这种: 有【标题】没【正文】, 标题后空一行就是正文
    assert L.parse_raw("【标题】是求职机构让我完成了社会化\n\n想说的都在图上了。\n第二段 #tag") == \
        ("是求职机构让我完成了社会化", "想说的都在图上了。\n第二段 #tag")
    assert L.parse_raw("【标题】 前面有空格的标题\n正文紧跟着") == ("前面有空格的标题", "正文紧跟着")
    assert L.parse_raw(None) == ("", "")


def test_strip_hashtags_removes_xhs_topics_but_keeps_prose():
    raw = "正文第一句。\n第二句 #秋招 #留学生求职#上岸 #途鸽求职[话题]# \n\n#尾巴\n"
    assert L.strip_hashtags(raw) == "正文第一句。\n第二句"
    # 剥完之后同项目的稿子不再共享一串标签四字串
    assert "秋招" not in L.strip_hashtags(raw)


def test_note_from_row_prefers_columns_and_strips_tags():
    n = L.Note.from_row({"note_id": "n1", "raw_content": "【标题】甲 【正文】正文 #tag",
                         "publish_time": "2026-09-12 08:00:00", "tier": "爆"})
    assert (n.title, n.body, n.tier) == ("甲", "正文", "爆")
    assert n.publish_time.tzinfo is not None
    n2 = L.Note.from_row({"note_id": "n2", "title": "列里的标题", "body": "列里的正文",
                          "raw_content": "【标题】x 【正文】y", "publish_time": None})
    assert (n2.title, n2.body) == ("列里的标题", "列里的正文")


# ── 精确 ──────────────────────────────────────────────────────────────

def test_identical_body_matches_even_with_tags_and_punctuation_changes():
    idx = L.VersionIndex([_v("v1", "秋招投了六十份", BODY_A), _v("v2", "绩点二点八", BODY_B)])
    note = _n("n1", "秋招投了六十份简历没回音", BODY_A.replace(", ", "，") + " #秋招 #留学生")
    m = L.match_note(note, idx)
    assert m.kind == "body_exact" and m.version.version_id == "v1"
    assert m.lag_days == 2 and m.score == 1.0


def test_published_before_committed_still_matches_by_body():
    """百健士: 101 篇先发后补录, 发布时间早于入库 4~20 天。开头相同就认, 窗口只
    用来分歧义。"""
    idx = L.VersionIndex([_v("v1", "t", BODY_A, days=0)])
    m = L.match_note(_n("n1", "t", BODY_A, days=-15), idx)
    assert m.kind == "body_exact" and m.lag_days == -15


def test_same_title_different_bodies_is_resolved_by_containment():
    """途鸽 09-10: 同一个标题各入库两次(同题重写)。发布稿是从其中一版改出来的,
    按四字串包含度分。"""
    idx = L.VersionIndex([_v("v1", "被外企群面刷掉三次", BODY_A),
                          _v("v2", "被外企群面刷掉三次", BODY_B)])
    # 开场改了(所以开头对不上), 中段照搬 v2
    edited = "发之前把开场重写了一遍, " + BODY_B[20:] + "后面这一段也是发布前改写的。"
    m = L.match_note(_n("n1", "被外企群面刷掉三次", edited), idx)
    assert m.kind == "title_exact" and m.version.version_id == "v2"
    assert m.score > 0.5 and len(m.candidates) == 2


def test_same_title_and_indistinguishable_bodies_is_ambiguous_not_guessed():
    idx = L.VersionIndex([_v("v1", "同一个标题两版", BODY_A, days=0),
                          _v("v2", "同一个标题两版", BODY_A, days=0)])
    m = L.match_note(_n("n1", "同一个标题两版", "完全不相干的正文, 只有标题对得上, 长度凑够二十个字。"), idx)
    assert m.kind == "ambiguous" and m.version is None
    assert {c["version_id"] for c in m.candidates} == {"v1", "v2"}


def test_tie_is_broken_by_the_closest_version_inside_the_window():
    idx = L.VersionIndex([_v("v1", "同题", BODY_A, days=-20), _v("v2", "同题", BODY_A, days=1)])
    m = L.match_note(_n("n1", "同题", BODY_A, days=2), idx)
    assert m.kind == "body_exact" and m.version.version_id == "v2"


# ── 模糊 ──────────────────────────────────────────────────────────────

def test_heavily_edited_publication_matches_by_containment_inside_the_window():
    """途鸽: 发布稿 305 字、原稿 527 字 —— 发布稿的四字串大多来自原稿。"""
    idx = L.VersionIndex([_v("v1", "原标题", BODY_A + BODY_B), _v("v2", "另一篇", BODY_B[::-1])])
    published = "改了个新标题以后, " + BODY_A[10:] + " 结尾也换了一句。"
    m = L.match_note(_n("n1", "换了标题", published, days=3), idx)
    assert m.kind == "fuzzy" and m.version.version_id == "v1"
    assert m.score >= L.FUZZY_CONTAIN


def test_fuzzy_never_reaches_outside_the_time_window():
    idx = L.VersionIndex([_v("v1", "原标题", BODY_A + BODY_B, days=0)])
    published = "改了个新标题以后, " + BODY_A[10:]
    far = _n("n1", "换了标题", published, days=L.WINDOW_AFTER_DAYS + 5)
    assert L.match_note(far, idx).kind == "unmatched"
    near = _n("n2", "换了标题", published, days=L.WINDOW_AFTER_DAYS - 1)
    assert L.match_note(near, idx).kind == "fuzzy"


def test_unrelated_note_is_unmatched_and_short_notes_do_not_fuzzy_match():
    idx = L.VersionIndex([_v("v1", "a", BODY_A), _v("v2", "b", BODY_B)])
    assert L.match_note(_n("n1", "别的", "今天天气很好, 出去走了走, 和这个项目毫无关系。" * 2), idx).kind == "unmatched"
    assert L.match_note(_n("n2", "短", "太短了"), idx).kind == "unmatched"


def test_two_equally_good_fuzzy_candidates_are_ambiguous():
    idx = L.VersionIndex([_v("v1", "a", BODY_A + "甲版结尾。"), _v("v2", "b", BODY_A + "乙版结尾。")])
    m = L.match_note(_n("n1", "x", "开场换了一句, " + BODY_A[5:]), idx)
    assert m.kind == "ambiguous" and len(m.candidates) == 2


def test_match_all_keys_by_note_id():
    idx = L.VersionIndex([_v("v1", "a", BODY_A)])
    out = L.match_all([_n("n1", "a", BODY_A), _n("n2", "b", "无关" * 30)], idx)
    assert out["n1"].kind == "body_exact" and out["n2"].kind == "unmatched"
