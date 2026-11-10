"""一批笔记的写法检查(2026-11-10, deskcore/poststyle.py, docs/deskcore.md §3.21) —— 纯函数这一层。

钉的是:
  · 重复说法(B): 同一小句里连着 8 个字一样、出现在 max(3, 两成)篇以上才算, 第 need 篇起点名; 一篇里出现几次只数一篇;
    话题标签、正文后面接着的评论段、【正文】这类标记、字面的 <br> / \\n 都不量(去掉的地方算断句), 品牌算一个字;
    项目规则原文里连着出现的说法不算;
  · 开头一样(C2): 第一小句的前 4 个字一样的有 max(5, 一半)篇以上才提示, 第一小句不到 2 个字的不算;
  · 版式残留: 五样各认得出, 正常的【】不算, 只有话题的正文算没有正文, 整个是空白的不报(存档那道闸管)。

所有示例文本都是合成的(示例牌 / 示例品牌)。
"""

from __future__ import annotations

import random

import pytest

from deskcore import poststyle as ps

A = ["示例牌"]
P = "周末带娃去公园晒太阳心情很好"              # 14 个字, 一个小句: 7 个 8 字串
Q = "带娃去公园晒太阳"                          # 正好 8 个字: 1 个 8 字串
_POOL = ("春夏秋冬山水花草风云雨雪日月星辰江河湖海松竹梅兰琴棋书画诗酒茶米油盐酱醋鱼虫鸟兽金木土石"
         "红黄蓝绿青紫黑白东南西北前后左右高低长短方圆轻重厚薄冷暖明暗快慢新旧早晚")


def _noise(i: int, k: int = 12, seed: int = 0) -> str:
    rng = random.Random(seed * 1000 + i)
    return "".join(rng.choice(_POOL) for _ in range(k))


def _body(i: int, *phrases: str) -> str:
    """第 i 篇: 开头、结尾各一句互不相同的话, 中间每个说法自成一个小句。"""
    mid = "".join(f"{p}。" for p in phrases)
    return f"第{i}篇{_noise(i)}，{mid}{_noise(i, seed=1)}。"


def _batch(n: int, carriers, *phrases: str) -> list[str]:
    phrases = phrases or (P,)
    return [_body(i, *phrases) if i in carriers else _body(i) for i in range(n)]


def _msg(quote: str, first: str, allowed: int, n: int) -> str:
    return (f"「{quote}」index 是 {first} 的 {allowed} 篇已经用过, 同一批 {n} 篇里同一个说法最多 {allowed} 篇用: "
            "这篇换个说法或换件事说(项目规则要求照原样写的话术不用改)")


# ══════════════════════════════════════════════════════════════════════
# 1 · 重复说法: 门槛
# ══════════════════════════════════════════════════════════════════════

def test_two_carriers_in_ten_are_allowed():
    r = ps.batch_repeats(_batch(10, {0, 1}), A)
    assert (r["need"], r["allowed"], r["grams"], r["flagged"], r["fix"]) == (3, 2, 0, [], {})


def test_the_third_carrier_is_flagged_and_told_which_two_used_it_first():
    r = ps.batch_repeats(_batch(10, {0, 1, 2}), A)
    assert (r["need"], r["grams"], r["carriers"], r["flagged"]) == (3, 7, [0, 1, 2], [2])
    assert r["fix"] == {2: [_msg(P, "0、1", 2, 10)]}


def test_labels_are_the_callers_indices():
    r = ps.batch_repeats(_batch(5, {0, 1, 2}), A, labels=["3", "5", "6", "8", "9"])
    assert r["fix"] == {2: [_msg(P, "3、5", 2, 5)]}


@pytest.mark.parametrize("n, carriers, flagged, allowed, first", [
    (15, {0, 1, 2}, [2], 2, "0、1"),               # 15 篇: 两成是 3, 照样 3 篇起
    (16, {0, 1, 2}, [], 3, None),                   # 16 篇: 两成向上取整是 4, 3 篇不算
    (16, {0, 1, 2, 3}, [3], 3, "0、1、2"),
    (20, {0, 1, 2, 3}, [3], 3, "0、1、2"),
    (21, {0, 1, 2, 3}, [], 4, None),                # 21 篇: 两成是 5
])
def test_the_share_takes_over_from_sixteen_drafts(n, carriers, flagged, allowed, first):
    r = ps.batch_repeats(_batch(n, carriers), A)
    assert (r["allowed"], r["flagged"]) == (allowed, flagged)
    if flagged:
        assert r["fix"][flagged[0]] == [_msg(P, first, allowed, n)]
        assert f"最多 {allowed} 篇" in r["fix"][flagged[0]][0]


def test_seven_tokens_are_not_a_phrase_eight_are():
    assert ps.batch_repeats(_batch(5, {0, 1, 2}, Q[1:]), A)["grams"] == 0
    r = ps.batch_repeats(_batch(5, {0, 1, 2}, Q), A)
    assert (r["grams"], r["flagged"]) == (1, [2])


def test_a_phrase_never_spans_two_clauses():
    assert ps.batch_repeats(_batch(5, {0, 1, 2}, "带娃去公园，晒太阳心情很好"), A)["grams"] == 0
    assert ps.batch_repeats(_batch(5, {0, 1, 2}, "带娃去公园晒太阳心情很好"), A)["grams"] == 5


# ══════════════════════════════════════════════════════════════════════
# 2 · 量之前去掉什么、什么不算
# ══════════════════════════════════════════════════════════════════════

def test_the_brand_counts_as_one_token_and_is_quoted_as_written():
    seven = "示例牌真的好用一二"                       # ＠ + 6 个字 = 7
    eight = "示例牌真的好用一二三"                     # ＠ + 7 个字 = 8
    assert ps.batch_repeats(_batch(5, {0, 1, 2}, seven), A)["grams"] == 0
    assert ps.batch_repeats(_batch(5, {0, 1, 2}, seven), [])["grams"] == 2, "不认品牌时 9 个字"
    r = ps.batch_repeats(_batch(5, {0, 1, 2}, eight), A)
    assert r["grams"] == 1 and r["fix"][2] == [_msg(eight, "0、1", 2, 5)], "引原话照原样写品牌, 不是 ＠"


@pytest.mark.parametrize("line", ["#周末带娃#去公园晒#太阳心情很好", "#周末带娃去公园晒太阳心情很好[话题]# #好心情[话题]#"])
def test_topic_tags_are_not_measured(line):
    assert ps.batch_repeats([_body(i) + line for i in range(5)], A)["grams"] == 0
    control = [_body(i) + line.replace("#", "").replace("[话题]", "") for i in range(5)]
    assert ps.batch_repeats(control, A)["grams"] > 0, "同样的字不是话题时照算"


@pytest.mark.parametrize("label", ps.COMMENT_BLOCKS)
def test_an_appended_comment_block_is_not_measured(label):
    assert ps.batch_repeats([_body(i) + label + P for i in range(5)], A)["grams"] == 0
    assert ps.batch_repeats([_body(i) + "。" + P for i in range(5)], A)["grams"] == 7


@pytest.mark.parametrize("cut", ["<br>", "<BR/>", "\\n", "【正文】", "【TAG】"])
def test_debris_inside_a_phrase_breaks_it(cut):
    assert ps.batch_repeats(_batch(5, {0, 1, 2}, f"带娃去公园{cut}晒太阳心情很好"), A)["grams"] == 0


def test_a_phrase_in_the_rule_text_is_exempt():
    texts = _batch(5, {0, 1, 2}, Q)
    r = ps.batch_repeats(texts, A, exempt=[f"售后统一这么说: {Q}, 不要改写"])
    assert (r["grams"], r["exempted"], r["flagged"], r["fix"]) == (0, 1, [], {})


@pytest.mark.parametrize("rule", [f"售后统一这么说: {Q[1:]}", "售后统一这么说: 带娃去公园，晒太阳", "", "  "])
def test_rule_text_with_part_of_the_phrase_does_not_exempt_it(rule):
    r = ps.batch_repeats(_batch(5, {0, 1, 2}, Q), A, exempt=[rule])
    assert (r["grams"], r["exempted"], r["flagged"]) == (1, 0, [2])


def test_rule_text_is_not_cut_at_a_comment_label():
    """规则常常在讲交付的样子(「正文后面接【素人评论】…」): 规则原文不截评论段, 后面那半句照样豁免。"""
    r = ps.batch_repeats(_batch(5, {0, 1, 2}, Q), A, exempt=[f"正文后面接【素人评论】, 评论里写「{Q}」"])
    assert (r["grams"], r["exempted"]) == (0, 1)


# ══════════════════════════════════════════════════════════════════════
# 3 · 怎么数、怎么说
# ══════════════════════════════════════════════════════════════════════

def test_a_phrase_repeated_inside_one_draft_counts_once():
    texts = [_body(0, P, P, P), _body(1, P)] + [_body(i) for i in range(2, 10)]
    assert ps.batch_repeats(texts, A)["grams"] == 0


def test_adjacent_phrases_used_by_different_drafts_each_name_their_own():
    """挨着的两段, 前面用过的不是同几篇: 各说各的, 不并成一句只报前一段的出处。"""
    a, b = "周末带娃去公园晒太阳", "示例牌防晒霜真的很好用"
    texts = [f"第一篇说{a}", f"第二篇讲{a}", f"另外{b}", f"还有{b}", f"最后{a}{b}"]
    r = ps.batch_repeats(texts, A)
    assert r["flagged"] == [4]
    assert r["fix"][4] == [_msg(a, "0、1", 2, 5), _msg(b, "2、3", 2, 5)]


def test_preferred_drafts_count_first_so_another_carrier_is_flagged():
    """``prefer``(这次改过重存的): 先算进不算的那几篇, 要改的落到别的篇上。"""
    texts = _batch(5, {0, 1, 2})
    r = ps.batch_repeats(texts, A, prefer={2})
    assert r["flagged"] == [1] and r["fix"] == {1: [_msg(P, "0、2", 2, 5)]}
    assert ps.batch_repeats(texts, A, prefer={0, 1, 2})["flagged"] == [2], "全是改过的: 照原来的顺序"
    assert ps.batch_repeats(texts, A, prefer={4})["flagged"] == [2], "不带这个说法的那篇不影响"


def test_at_most_two_phrases_are_named_per_draft():
    p2, p3 = "雨天在家拼积木拼到半夜", "早上煮了一锅小米南瓜粥"
    r = ps.batch_repeats(_batch(5, {0, 1, 2}, P, p2, p3), A)
    assert r["flagged"] == [2] and r["fix"][2] == [_msg(P, "0、1", 2, 5), _msg(p2, "0、1", 2, 5)]


def test_a_long_quote_is_clipped():
    long = _noise(99, k=40, seed=7)
    r = ps.batch_repeats(_batch(5, {0, 1, 2}, long), A)
    assert r["fix"][2] == [_msg(long[:ps.POST_QUOTE_MAX] + "…", "0、1", 2, 5)]


def test_too_few_drafts_cannot_trip():
    r = ps.batch_repeats([_body(0, P), _body(1, P)], A)
    assert (r["n"], r["need"], r["grams"], r["carriers"]) == (2, 3, 0, [])


# ══════════════════════════════════════════════════════════════════════
# 4 · 开头一样(只提示)
# ══════════════════════════════════════════════════════════════════════

def _openers(n: int, k: int, head: str = "姐妹们我真的想说") -> list[str]:
    return [f"{head}{_noise(i)}，{_noise(i, seed=2)}。" if i < k else _body(i) for i in range(n)]


def test_five_of_ten_with_the_same_opening_get_a_hint():
    r = ps.shared_openers(_openers(10, 5), A)
    assert (r["need"], r["top"], r["indices"]) == (5, 5, [0, 1, 2, 3, 4])
    assert r["hint"] == ("这批 10 篇里, 第 1、2、3、4、5 篇开头都是「姐妹们我」: 只是提示, 不算要改的地方; "
                         "想让这批读起来不那么像一个模子, 挑几篇换个开头")


def test_the_hint_counts_the_callers_drafts_from_one():
    """提示是要跟用户提的那一句: 不写 index, 第几篇从 1 数(调用方的 index + 1)。"""
    r = ps.shared_openers(_openers(6, 5), A, labels=["1", "2", "4", "5", "7", "9"])
    assert r["hint"].startswith("这批 6 篇里, 第 2、3、5、6、8 篇开头")
    assert "index" not in r["hint"]


@pytest.mark.parametrize("n, k, hinted", [(8, 4, False), (11, 5, False), (11, 6, True), (5, 5, True)])
def test_the_opener_floor_is_five_and_half(n, k, hinted):
    r = ps.shared_openers(_openers(n, k), A)
    assert bool(r["hint"]) is hinted and r["top"] == k
    assert r["indices"] == (list(range(k)) if hinted else [])


def test_openings_that_differ_at_the_fourth_token_are_not_the_same():
    texts = [f"姐妹们{c}{_noise(i)}，{_noise(i, seed=2)}。" for i, c in enumerate("甲乙丙丁戊己")]
    r = ps.shared_openers(texts, A)
    assert r["hint"] is None and r["top"] == 1


def test_a_one_token_first_clause_is_ignored():
    r = ps.shared_openers([f"嗯，{_noise(i)}。" for i in range(6)], A)
    assert (r["top"], r["hint"]) == (0, None)


def test_a_two_token_interjection_counts():
    r = ps.shared_openers([f"救命，{_noise(i)}。" for i in range(5)], A)
    assert r["hint"] and "开头都是「救命」" in r["hint"]


def test_labels_and_tags_before_the_opening_are_skipped_and_the_brand_is_one_token():
    r = ps.shared_openers([f"【正文】#话题 示例牌真的绝了{_noise(i)}。" for i in range(5)], A)
    assert r["hint"] and "开头都是「示例牌真的绝」" in r["hint"]


def test_time_words_alone_do_not_make_a_shared_opening():
    r = ps.shared_openers([f"周末{i}点{_noise(i)}，{_noise(i, seed=2)}。" for i in range(6)], A)
    assert r["hint"] is None


# ══════════════════════════════════════════════════════════════════════
# 5 · 版式残留
# ══════════════════════════════════════════════════════════════════════

def _kinds(title: str, body: str, **kw) -> list[str]:
    return [r["kind"] for r in ps.format_debris(title, body, **kw)]


@pytest.mark.parametrize("body, kinds", [
    ("正文内容。【素人评论】评论一", ["comment_block"]),
    ("正文内容。【作者回复】回复一", ["comment_block"]),
    ("【正文】正文内容", ["label"]),
    ("正文内容【话题】#话题甲", ["label"]),
    ("正文内容【Tag】#话题甲", ["label"]),
    ("正文<br>内容", ["br"]),
    ("正文<BR/>内容", ["br"]),
    ("正文<br />内容", ["br"]),
    ("正文\\n内容", ["literal_newline"]),
    ("正文\\r\\n内容", ["literal_newline"]),
    ("【正文】正文<br>内容\\n第二行【贴主回复】回复<br>", ["comment_block", "label", "br", "literal_newline"]),
    ("正常的正文【干货】分享", []),
    ("文件放在 C:\\new 下面", []),
    ("放在 folder\\new 下面", []),
    ("正文\n真的换行", []),
    ("第一段写完了\\nVlog今天带娃去公园", ["literal_newline"]),      # 后面接英文的照样认
    ("\\nOOTD今天的穿搭", ["literal_newline"]),
    ("写完了 \\nVlog", ["literal_newline"]),
    ("第一段写完了\\r\\nVlog今天", ["literal_newline"]),
])
def test_each_kind_of_debris_is_found(body, kinds):
    assert _kinds("标题", body) == kinds


@pytest.mark.parametrize("body", ["#话题甲 #话题乙", "  【正文】 #a#b ", "【素人评论】只有评论", "<br>\\n#话题甲"])
def test_a_body_with_nothing_left_is_empty_and_only_that(body):
    assert ps.format_debris("标题", body) == [
        {"kind": "empty", "what": "去掉话题标签、标记和评论之后这篇没有正文: 把正文补进 body"}]


@pytest.mark.parametrize("body", ["", "   ", "\n\t"])
def test_a_blank_body_is_left_to_the_empty_body_gate(body):
    assert ps.format_debris("标题", body) == []


@pytest.mark.parametrize("tag", ["#示例牌好物", "#OOTD"])
def test_a_literal_newline_right_after_a_topic_tag_does_not_swallow_the_body(tag):
    """「#话题\\nVlog…」: \\n 没认出来的话 \\ 算话题里的字, 话题一直吞到行尾 → 误报「没有正文」, 真问题反而不报。"""
    body = tag + "\\nVlog今天带娃去公园晒太阳心情很好"
    assert _kinds("标题", body) == ["literal_newline"]
    assert ps.measured_text(body).strip() == "Vlog今天带娃去公园晒太阳心情很好"


def test_a_brand_right_after_a_literal_newline_is_still_one_token():
    eight = "Example真的好用一二三"                     # ＠ + 7 个字 = 8
    texts = [_body(i) + "写完了\\n" + eight + "。" if i < 3 else _body(i) for i in range(5)]
    assert ps.batch_repeats(texts, ["Example"])["grams"] == 1


def test_debris_after_the_comment_block_is_not_reported_again():
    assert _kinds("标题", "正文内容。【素人评论】评论<br>【正文】") == ["comment_block"]


def test_labels_in_the_title_say_so_and_skip_labels_only_skips_those():
    rows = ps.format_debris("【标题】示例标题", "【正文】正文内容【话题】", skip_labels={"【正文】"})
    assert rows == [{"kind": "label", "what": "标题里有「【标题】」标记: body 只放正文, 标记删掉(交付时再按项目要的样子加)"},
                    {"kind": "label", "what": "正文里有「【话题】」标记: body 只放正文, 标记删掉(交付时再按项目要的样子加)"}]


def test_template_and_no_template_wordings_differ():
    body = "【正文】正文内容【话题】。【素人评论】评论一"
    assert ps.format_debris("标题", body) == [
        {"kind": "comment_block", "what": "正文里带了「【素人评论】」那一段: body 只放正文, 评论写在这篇的 comments 里, "
                                          "交付时再按项目要的样子排在正文后面; 正文里删掉这一段"},
        {"kind": "label", "what": "正文里有「【正文】」「【话题】」标记: body 只放正文, 标记删掉(交付时再按项目要的样子加)"}]
    assert ps.format_debris("标题", body, skip_labels={"【标题】", "【正文】"}, has_template=True) == [
        {"kind": "comment_block", "what": "正文里带了「【素人评论】」那一段: 评论不放进正文, 写在这篇的 comments 里, "
                                          "标签和位置由写作台按交付模板加; 正文里删掉这一段"},
        {"kind": "label", "what": "正文里有「【话题】」标记: 话题写在这篇的 tags 里, 交付稿里的话题行由写作台按交付模板加; "
                                  "稿子里删掉这个标记"}]
    assert ps.format_debris("【标题】标题", "【正文】正文内容【tag】", has_template=True) == [
        {"kind": "label", "what": "标题里有「【标题】」标记: 标记由写作台按交付模板加, 稿子里删掉"},
        {"kind": "label", "what": "正文里有「【正文】」标记: 标记由写作台按交付模板加, 稿子里删掉"},
        {"kind": "label", "what": "正文里有「【tag】」标记: 话题写在这篇的 tags 里, 交付稿里的话题行由写作台按交付模板加; "
                                  "稿子里删掉这个标记"}]


def test_a_template_without_comments_does_not_promise_to_add_them():
    """模板不拼评论(delivery.comments 是 null): 照「写在 comments 里」挪过去, 评论就从交付稿里没了。"""
    rows = ps.format_debris("标题", "正文内容。【素人评论】评论一", has_template=True, takes_comments=False)
    assert rows == [{"kind": "comment_block",
                     "what": "正文里带了「【素人评论】」那一段: 这个项目的交付模板不拼评论, 评论不放进正文; 正文里删掉这一段, "
                             "要给的评论放在交付稿后面单独给用户"}]
    assert "comments" not in rows[0]["what"] and "写作台" not in rows[0]["what"]


def test_a_template_without_topics_leaves_the_topic_label_alone():
    """模板不管话题: 话题行照项目规则写在正文末尾、交付稿原样带上, 【话题】是项目要的样子, 不报; 别的标记照报。"""
    body = "正文内容。\n【话题】#示例话题 #示例牌好物"
    assert ps.format_debris("标题", body, has_template=True, takes_tags=False) == []
    assert _kinds("标题", "【正文】" + body, has_template=True, takes_tags=False) == ["label"]
    assert _kinds("标题", body, has_template=True) == ["label"]
    assert _kinds("标题", body) == ["label"], "没模板: body 只放正文, 照报"


def test_br_and_newline_wordings():
    assert ps.format_debris("", "正文<br>内容\\n第二行") == [
        {"kind": "br", "what": "正文里有字面的「<br>」(多半是从交付表格里复制回来的): 换成真的换行"},
        {"kind": "literal_newline", "what": "正文里有字面的「\\n」(反斜杠加 n, 不是换行): 换成真的换行"}]


def test_measured_text_keeps_positions():
    body = "【正文】示例牌<br>好用\\n真的 #话题甲【素人评论】评论"
    m = ps.measured_text(body)
    assert len(m) == body.index("【素人评论】")
    assert m.strip().split() == ["示例牌", "好用", "真的"]
    for word in ("示例牌", "好用", "真的"):
        assert m.index(word) == body.index(word)
