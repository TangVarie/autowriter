"""评论的写法底线和一组评论的配比、重复(2026-11-10, deskcore/commentstyle.py)。只测纯函数, 不碰库、不调模型。

钉住:
  1. 提到帖子(2026-11-11 放宽, 原来只查「帖子里」): 帖子、贴子、这帖、本帖、原帖、帖里、发帖……开头、句中、结尾都查,
     后面的里 / 中 / 内一起引出来; 补贴里、请帖、妥帖、服帖、贴片、这帖药不算;
  2. 含糊指代「X那栏 / X那几个字 / X那条路 / X那次 / X那阵」: owner 举的五个(换成假的上下文)全中; 普通说法(那么、那样、
     那时、那天、那一刻、句首的那次、说自己的「我…那次」、动词后面的「走那条路」……)一个不中;
  3. 重复的说法: 规则 A(6 字在前面出现过)、B(5 字出现在 3 条以上)、C(开头 2 字一样的超过 2 条); 品牌长短叫法归成一个,
     不跨标点, 英文一个词算一个字; 帖子下已有的、同一批前几次判过的也算参照;
  4. 提品牌上限 floor、中性 / 短评下限 ceil, 都按整数百分比; 按给的顺序留前面的; 中性要 Jev 的主要行为, 没给就不数;
  5. 只有一个字的品牌叫法不用。
全部是合成文字和合成品牌, 不引用真实评论。
"""
from __future__ import annotations

import pytest

from deskcore import commentstyle as cs

BRAND = "示例牌"
ALIASES = cs.brand_aliases(BRAND, "", ["示例牌老师"])


# ── 1. 提到帖子 ─────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, word", [
    ("帖子里说的排期表能分享一下吗", "帖子里"),           # 开头
    ("我照着帖子里的清单准备了一周", "帖子里"),           # 句中
    ("这个方法我也想试试，就是帖子里", "帖子里"),         # 结尾
    ("贴子里那张表格是自己做的吗", "贴子里"),
    ("帖子中提到的课程还有吗", "帖子中"),
    ("帖里的第三步没看懂", "帖里"),
    # 2026-11-11: 一提帖子就算, 不只「帖子里」
    ("帖子说的排期表能分享吗", "帖子"),
    ("看完帖子我也想试试", "帖子"),
    ("这个帖子挺实在的", "帖子"),
    ("这帖子绝了", "帖子"),
    ("帖子内容很实在", "帖子"),                          # 「内容」的内不引进来
    ("这帖太实用了", "这帖"),
    ("本帖的清单收藏了", "本帖"),
    ("原帖那张表还在吗", "原帖"),
    ("此帖已收藏", "此帖"),
    ("该帖提到的课还有吗", "该帖"),
    ("上帖说的那家店", "上帖"),
    ("楼主这帖写得清楚", "这帖"),                        # 楼主不引: 光一个楼主不算
    ("楼主帖里的第三步", "楼主帖里"),
    ("你的帖我反复看了", "你的帖"),
    ("这篇帖我收藏了", "这篇帖"),
    ("这帖里的清单", "这帖里"),
    ("这条帖文写得真细", "帖文"),
    ("帖下面有人问过", "帖下"),
    ("帖内第二张图", "帖内"),
    ("看你发帖说三天见效", "发帖"),
])
def test_post_ref_is_flagged_anywhere(text, word):
    issues = cs.issues(text)
    assert len(issues) == 1 and f"去掉「{word}」: " in issues[0], issues
    assert issues[0] == f"去掉「{word}」: 评论里不提帖子, 直接说那件事(「帖子说的 X」「这帖的 X」→ 直接说 X)"


@pytest.mark.parametrize("text", [
    "补贴里有一部分是交通费", "请帖里写的时间是周六", "字帖里的字太好看了", "碑帖临了一年", "喜帖已经发了",
    "庚帖", "名帖递上去", "贴了两晚就好多了", "这贴两晚就消了", "示例贴贴上一整天", "贴片这个思路好",
    "老师做事妥帖内敛", "说话妥帖文雅", "粉底很服帖", "服帖下来了", "熨帖", "一帖药喝了三天", "这帖药苦得很",
    "这帖膏药贴了三天", "补贴子女教育", "津贴子女也能领", "贴子母扣的那种包", "楼主好厉害", "楼主用的哪款",
    "帖主回得好快",
])
def test_post_ref_leaves_other_words_alone(text):
    assert cs.issues(text) == [], text


def test_post_ref_quotes_every_distinct_mention_once():
    (line,) = cs.issues("看完帖子又翻了原帖, 帖子里那张表还在")
    assert line.startswith("去掉「帖子」「原帖」「帖子里」: ")


# ── 2. 含糊指代 ─────────────────────────────────────────────────────────────────────────────────

# owner 11-10 举的五个, 换成假的上下文(结构一样: <一个词>那<量词>)
OWNER_LIKE = [
    ("社团那栏写的什么呀", "社团那栏"),
    ("摸鱼那几个字太扎心了", "摸鱼那几个字"),
    ("海外仓那条路好走吗", "海外仓那条路"),
    ("三小时那次是怎么准备的", "三小时那次"),
    ("卡住那阵怎么熬过来的", "卡住那阵"),
]


@pytest.mark.parametrize("text, phrase", OWNER_LIKE + [
    ("你卡住那阵后来怎么办的", "你卡住那阵"),
    ("专业那一栏填的什么", "专业那一栏"),
    ("面试模拟那几次有用吗", "面试模拟那几次"),
    ("你说的四十分钟那次后来过了吗", "四十分钟那次"),
    ("“摸鱼”那几个字太扎心了", "“摸鱼”那几个字"),          # 引号括起来的词: 一样是指着帖子里的那几个字
    ("「社团」那栏写的什么呀", "「社团」那栏"),
    ("摸鱼那俩字太扎心了", "摸鱼那俩字"),
    ("[赞R]社团那栏写的什么呀", "社团那栏"),                # 贴纸不算进那个词
])
def test_vague_pointers_are_flagged(text, phrase):
    issues = cs.issues(text)
    assert len(issues) == 1, issues
    assert f"「{phrase}」" in issues[0] and "含糊的指代" in issues[0] and "直接把那件事说出来" in issues[0]


@pytest.mark.parametrize("text", [
    "那么多课程到底选哪个", "这么晚还在改简历", "那样写会不会太长", "这样就清楚多了", "那就先投小公司练手",
    "这就去试试", "那是去年的事了", "这是我第一次投", "那些模板都差不多", "这些问题面试都会问", "那时候还没毕业",
    "这时才想起来", "那里离地铁远吗", "这里的老师挺负责", "那儿有自习室吗", "这儿能停车吗", "刚回国那会儿啥也不懂",
    "这会儿才有空看", "交完材料那天我睡得很香", "老师那边回得挺快", "那种感觉我懂", "拿到通知那一刻真的想哭",
    "按下付款那一下手是抖的", "等结果那段时间最难熬", "那阵真慌", "那次以后我就不熬夜了", "我面挂那次哭了一晚上",
    "我转行那阵天天刷题", "我不想走那条路", "还记得那次分享会吗", "我们讲过的那次直播", "人家那格局就是不一样",
    "刮过那阵风就好了", "这个栏目每周更新", "这句话太对了", "不打鸡血这点挺好", "流量这块确实难做",
    "“内卷”这两个字太准了", "最后那句太真实了", "开头那段我看了三遍",
])
def test_ordinary_demonstratives_are_left_alone(text):
    assert cs.issues(text) == [], text


def test_both_new_lines_are_in_the_lines_and_the_short_task():
    assert any("含糊的指代" in x for x in cs.LINES) and "含糊指代" in cs.SHORT
    post = next(x for x in cs.LINES if "帖子" in x)
    assert post == "评论里不提帖子(帖子、这帖、本帖、原帖、帖里都不写, 换成「这篇」也一样): 直接说那件事"
    assert "帖子里" not in post, "2026-11-11 起不只「帖子里」"
    assert "不提帖子(帖子、这帖、本帖都不写)" in cs.SHORT and "「帖子里」" not in cs.SHORT


def test_padding_and_common_knowledge_lines_are_prompt_only():
    """owner 11-11: 不为凑字数加话(短也行)、常识不写成新发现。只给模型看, 代码不查: 不在 LINES(LINES 是代码查的那几样),
    issues 不因为短、也不因为「原来」「才知道」报。"""
    assert cs.PAD_LINE == "不为凑字数加话: 一句能说完就一句, 短也行; 不复述前面说过的, 不加总结句、感叹句撑长度"
    assert cs.COMMON_LINE == ("大家都知道的事不写成新发现: 常识别用「原来…」「才知道…」「没想到…」「涨知识了」去接, "
                              "直接说或者不说; 自己真碰上的意外照常写")
    assert cs.PAD_LINE not in cs.LINES and cs.COMMON_LINE not in cs.LINES
    assert "不为凑字数加话(短也行)" in cs.SHORT and "常识不写成新发现" in cs.SHORT
    for text in ("同问", "蹲", "原来防晒要每两小时补一次", "才知道熬夜伤皮肤", "没想到这么快就到了"):
        assert cs.issues(text) == [], text


# ── 3. 重复的说法 ───────────────────────────────────────────────────────────────────────────────

def test_rule_a_flags_the_later_comment_and_names_the_earlier_one():
    texts = ["上周三晚上八点开的直播我也蹲了", "同问价格", "我也是上周三晚上八点开始看的"]
    res = cs.repeated_clauses(texts, ALIASES)
    assert res["flagged"] == [2]
    (line,) = res["fix"][2]
    assert "「上周三晚上八点开」和第 1 条重复" in line and "整组同一个说法只用一次" in line
    assert res["segment"] and "「上周三晚上八点开」×2" in res["segment"]


def test_rule_a_needs_six_in_a_row():
    assert cs.repeated_clauses(["我也想知道多少钱一节", "求问多少钱一节课"], ALIASES)["fix"] == {}


def test_long_and_short_brand_names_count_as_the_same_word():
    res = cs.repeated_clauses(["示例牌老师帮我改了三版", "示例牌帮我改了三版"], ALIASES)
    assert res["flagged"] == [1] and "「示例牌帮我改了三版」" in res["fix"][1][0]


def test_a_run_does_not_cross_punctuation():
    assert cs.repeated_clauses(["简历改到第三版，周末投了五家", "改到第三版周末投"], ALIASES)["fix"] == {}


def test_an_english_word_is_one_token():
    assert cs.repeated_clauses(["拿到offer那周真睡不着", "我拿到offer那周也是"], ALIASES)["fix"] == {}, "5 个字, 不是 9 个"
    assert cs.repeated_clauses(["拿到offer那周真睡不着", "我拿到offer那周真的"], ALIASES)["flagged"] == [1]


def test_existing_and_earlier_comments_are_references():
    res = cs.repeated_clauses(["我是上周三晚上八点看到的"], ALIASES, existing=["上周三晚上八点开播的"])
    assert "和帖子下已有的评论重复" in res["fix"][0][0]
    res = cs.repeated_clauses(["我是上周三晚上八点看到的"], ALIASES, earlier=["别的", "上周三晚上八点开播的"])
    assert "和之前送判的第 2 条重复" in res["fix"][0][0]
    res = cs.repeated_clauses(["上周三晚上八点开播的"], ALIASES, existing=["上周三晚上八点开播的"], earlier=["同问"])
    assert res["flagged"] == [0], "只改这组里的; 参照自己不算"


def test_the_same_phrase_within_one_comment_is_not_a_repeat():
    assert cs.repeated_clauses(["上周三晚上八点开始，上周三晚上八点结束"], ALIASES)["fix"] == {}


def test_rule_b_flags_the_third_use_of_a_five_char_run():
    texts = ["多少钱一节呀", "想问多少钱一节", "求多少钱一节", "到底多少钱一节"]
    res = cs.repeated_clauses(texts, ALIASES, labels=["位 a", "位 b", "位 c", "位 d"])
    assert res["flagged"] == [2, 3]
    assert "「多少钱一节」位 a、位 b已经用过" in res["fix"][2][0]


def test_rule_b_counts_earlier_comments():
    res = cs.repeated_clauses(["到底多少钱一节"], ALIASES, earlier=["多少钱一节呀", "想问多少钱一节"])
    assert res["flagged"] == [0] and "之前送判的第 1 条、之前送判的第 2 条" in res["fix"][0][0]


def test_rule_c_brand_opener_three_times():
    two = ["示例牌把简历拆开讲了", "示例牌把时间排满了"]
    assert cs.repeated_clauses(two, ALIASES)["fix"] == {}
    res = cs.repeated_clauses(two + ["示例牌老师把面试题过了一遍"], ALIASES)
    assert res["flagged"] == [2]
    assert res["fix"][2] == ["开头「示例牌老师把」已经有 2 条(第 1 条、第 2 条), 换个开头"]
    assert "开头「示例牌老师把」×3" in res["segment"]


def test_stickers_and_emoji_are_ignored():
    res = cs.repeated_clauses(["[赞R]上周三晚上八点开的直播", "上周三晚上👍八点开的直播"], ALIASES)
    assert res["flagged"] == [1]


@pytest.mark.parametrize("unit", ["[👍]", "[赞R]", "[[👍]]", "[", "👍]"])
def test_long_runs_of_stickers_do_not_stall(unit):
    import time
    t = time.perf_counter()
    cs.repeated_clauses([unit * 200 + "示例牌真好", "示例牌真好" + unit * 200] * 10, ALIASES)
    assert time.perf_counter() - t < 2


# ── 4. 配比 ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("n, allowed, need", [(1, 0, 1), (4, 2, 2), (7, 3, 3), (20, 10, 6), (50, 25, 15)])
def test_brand_cap_floors_and_neutral_floor_ceils(n, allowed, need):
    assert cs.share_floor(n, 0.5) == allowed and cs.share_ceil(n, 0.3) == need


@pytest.mark.parametrize("pct", [0, 1, 20, 25, 28, 30, 33, 50, 58, 67, 99, 100])
def test_shares_match_exact_fractions_for_every_set_size(pct):
    from fractions import Fraction
    import math
    for n in range(1, 51):                          # 一批最多 50 条, 一次最多 20 条: 每个大小都对
        exact = Fraction(n * pct, 100)
        assert cs.share_floor(n, pct / 100) == math.floor(exact), (n, pct)
        assert cs.share_ceil(n, pct / 100) == math.ceil(exact), (n, pct)


def test_integer_percent_not_float():
    import math
    assert math.ceil(25 * 0.28) == 8 and math.floor(50 * 0.58) == 28, "浮点: 7.000000000000001 / 28.999999999999996"
    assert cs.share_ceil(25, 0.28) == 7 and cs.share_floor(50, 0.58) == 29
    assert cs.share_ceil(20, 0.3) == 6 and cs.share_ceil(20, 0.2) == 4 and cs.share_ceil(4, 0.2) == 1


def test_brand_share_keeps_the_first_ones():
    texts = ["今天没事", "示例牌挺好", "示例牌老师很细", "我在看", "示例牌第三次了", "示例牌又来了"]
    res = cs.brand_share(texts, ALIASES, brand_max=0.5)
    assert res["named"] == [1, 2, 4, 5] and res["allowed"] == 3 and res["excess"] == [5]
    assert "去掉品牌" in res["fix"][5] and "不换成代称" in res["fix"][5] and "最多 3 条" in res["fix"][5]
    assert "第 6 条 去掉品牌" in res["segment"]
    assert cs.brand_share(texts[:4], ALIASES, brand_max=0.5)["segment"] is None


def test_a_single_comment_may_not_name_the_brand_and_the_wording_says_so():
    res = cs.brand_share(["示例牌挺好"], ALIASES, brand_max=0.5)
    assert res["allowed"] == 0 and res["excess"] == [0]
    assert "一条都不能提品牌" in res["fix"][0] and "已经有 0 条" not in res["fix"][0]
    assert "留前 0 条" not in res["segment"] and "第 1 条 去掉品牌" in res["segment"]


def test_neutral_needs_no_brand_and_a_neutral_speech_act():
    texts = ["示例牌怎么收费", "多少钱一节", "这个老师很好", "我自己先投了小公司"]
    acts = ["提问", "提问", "旁观推荐", "补充经验"]
    res = cs.neutral_share(texts, ALIASES, neutral_min=0.3, speech_acts=acts)
    assert res["checked"] and res["neutral"] == [1, 3] and res["need"] == 2 and res["short_by"] == 0
    res = cs.neutral_share(texts, ALIASES, neutral_min=0.75, speech_acts=acts, prefer=[0])
    assert res["short_by"] == 1 and res["candidates"] == [0], "提品牌超出的那几条先改"
    res = cs.neutral_share(texts, ALIASES, neutral_min=0.75, speech_acts=["说不清", None, "提问", "提问"])
    assert res["neutral"] == [2, 3], "说不清、没判出来的不算中性"


def test_neutral_is_not_counted_without_speech_acts():
    res = cs.neutral_share(["a", "b"], ALIASES, neutral_min=0.3, speech_acts=None)
    assert res["checked"] is False and res["short_by"] == 0 and "没数" in res["segment"]


def test_short_share_strips_stickers_and_punctuation():
    assert cs.visible_len("蹲一个[赞R]！！ 👍") == 3
    assert cs.visible_len("示例牌 yyds") == 7, "品牌按字面的长度算"
    texts = ["蹲一个后续[赞R]", "这个课程到底适不适合完全零基础转行的人", "同问价格", "我自己是先投了几家小公司练手的后来才敢投大厂"]
    res = cs.short_share(texts, short_chars=15, short_min=0.75)
    assert res["short"] == [0, 2] and res["need"] == 3 and res["short_by"] == 1
    assert res["candidates"] == [1], "问句先改成一句话"
    assert cs.short_share(texts, short_chars=15, short_min=0.2)["segment"] is None


# ── 5. 只有一个字的品牌叫法 ─────────────────────────────────────────────────────────────────────

def test_single_char_cjk_alias_is_ignored():
    al = cs.brand_aliases("甲")
    assert al == ["甲"]
    assert cs.brand_share(["甲乙丙都试过", "甲方说的"], al, brand_max=0.0)["named"] == []
    assert cs.brand_endings(["选来选去还是甲"] * 4, al)["hits"] == []
