"""一组评论里以品牌评价收尾的最多四分之一(2026-11-09, deskcore/commentstyle.brand_endings)。

钉住:
  1. 上限 ceil(25%·n): 4 → 1, 7 → 2, 20 → 5; 按给的顺序留前面几条, 后面的才要改;
  2. 只看收尾: 品牌在最后一小句里才算; 最后一小句很短时, 品牌在前一小句末尾的也算(「…<品牌>，真的稳」); 品牌在开头、
     后面接一段事实的不算(含空格切开的); 整条只有一小句时提到品牌就算; 问句收尾不算(「挺好的呢」「挺靠谱的嘛」不是问句);
  3. 品牌叫法: projects.brand + 交付模板 must_mention_any + 品牌里的英文段, 不分大小写; 都没有退回项目名;
  4. 品牌收尾的句式重复(3 条起)只提示, 不算没过; 不落在品牌上的收尾不数;
  5. score_comments: 超出的那几条 style_fix、passed=false, thread_fix 第一句点名; 判定服务没判成也照样回;
  6. 跟着笔记交付的评论不数(不进 delivery.problems), requirements 的 task 里只带一句写法提示。
全部是合成文字和合成品牌, 不引用真实评论。
"""
from __future__ import annotations

import pytest

from deskcore import commentstyle as cs
from deskcore import core, delivery
from tests.test_deskcore_judge_tools import _Resp, wired  # noqa: F401 — fixture
from tests.test_deskcore_score_and_comments import LINK, ME, _tv_client

BRAND = "示例牌"
ALIASES = cs.brand_aliases(BRAND)

# 以品牌评价收尾的几种样子(合成)
BRAND_TAILS = [
    "改了三版简历才约到面试，示例牌这波挺懂行",
    "第二周就收到回复了，示例牌这步挺实在",
    "老师每次都回得很快，示例牌确实让人安心",
    "从选校到文书一路跟下来，说实话示例牌不虚",
    "去年也是这么过来的，示例牌，真的稳",
]
# 不以品牌收尾的几种样子(合成)
PLAIN_TAILS = [
    "我当时也卡在第一轮，后来把项目经历挪到最前面",
    "看到第六十份那里我笑出声了",
    "简历第一行写的是什么呀",
    "示例牌的老师让我把数字写具体，第二周就约到了两场面试",     # 品牌在句中, 收尾是事实
    "同问，投了一个月还没回音",
    "我是用表格记每一家的进度，不然真记不住",
]


def _set(n_brand: int, n: int) -> list[str]:
    out = [BRAND_TAILS[i % len(BRAND_TAILS)] + "~" * (i // len(BRAND_TAILS)) for i in range(n_brand)]
    out += [PLAIN_TAILS[i % len(PLAIN_TAILS)] for i in range(n - n_brand)]
    return out


# ── 1. 上限 ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("n, allowed", [(1, 1), (2, 1), (3, 1), (4, 1), (5, 2), (7, 2), (8, 2), (12, 3), (20, 5)])
def test_cap_is_a_quarter_rounded_up(n, allowed):
    assert cs.brand_ending_allowed(n) == allowed


@pytest.mark.parametrize("n, n_brand", [(4, 1), (7, 2), (20, 5)])
def test_at_the_cap_nothing_to_fix(n, n_brand):
    res = cs.brand_endings(_set(n_brand, n), ALIASES)
    assert len(res["hits"]) == n_brand and res["excess"] == [] and res["fix"] == {} and res["thread_fix"] is None


@pytest.mark.parametrize("n, n_brand", [(4, 2), (7, 3), (20, 6), (20, 20)])
def test_over_the_cap_the_later_ones_are_flagged(n, n_brand):
    texts = _set(n_brand, n)
    res = cs.brand_endings(texts, ALIASES)
    allowed = cs.brand_ending_allowed(n)
    assert res["hits"] == list(range(n_brand))
    assert res["excess"] == list(range(allowed, n_brand)), "按给的顺序留前面几条, 后面的才要改"
    assert sorted(res["fix"]) == res["excess"]
    for line in res["fix"].values():
        assert f"最多 {allowed} 条" in line and "事实、感受、细节或提问" in line and "把品牌挪到前面的句子里" in line


def test_order_decides_which_ones_stay():
    texts = [PLAIN_TAILS[0], BRAND_TAILS[0], PLAIN_TAILS[1], BRAND_TAILS[1]]
    res = cs.brand_endings(texts, ALIASES)
    assert res["hits"] == [1, 3] and res["excess"] == [3]


def test_thread_fix_names_kept_and_rewritten_comments():
    texts = _set(3, 4)
    res = cs.brand_endings(texts, ALIASES, labels=["位 q1", "位 q2", "位 exp", "位 reply"])
    assert "整组 4 条里 3 条以品牌评价收尾, 最多 1 条" in res["thread_fix"]
    assert "留前 1 条(位 q1)" in res["thread_fix"] and "位 q2、位 exp 改成" in res["thread_fix"]
    assert "位 reply" not in res["thread_fix"]


# ── 2. 只看收尾 ─────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "示例牌的老师让我把数字写具体，第二周就约到了两场面试",
    "当时在示例牌和别家之间犹豫了很久，最后是看课程表定的",
    "我也是找示例牌改的简历，现在每天投十份",
])
def test_brand_mid_sentence_is_not_a_brand_ending(text):
    assert cs.brand_endings([text, text], ALIASES)["hits"] == []


@pytest.mark.parametrize("text", PLAIN_TAILS)
def test_plain_endings_are_fine(text):
    assert cs.brand_endings([text] * 4, ALIASES)["hits"] == []


@pytest.mark.parametrize("text", [
    "示例牌靠谱吗？", "示例牌这个课贵吗", "那示例牌呢", "示例牌怎么样", "示例牌靠不靠谱", "后来选的示例牌吗？！[笑哭]",
])
def test_question_endings_are_exempt(text):
    assert cs.brand_endings([text] * 4, ALIASES)["hits"] == []


@pytest.mark.parametrize("text", [
    "去年也是这么过来的，示例牌，真的稳",            # 最后一小句很短, 连上前一小句
    "最后选了示例牌，挺好",
    "示例牌这波挺懂行。",                             # 句末标点、表情、贴纸去掉再看
    "示例牌这波挺懂行[赞R]",
    "示例牌这波挺懂行 👍",
])
def test_short_tails_and_trailing_decoration(text):
    assert cs.brand_endings([text] * 2, ALIASES)["hits"] == [0, 1]


def test_a_long_last_clause_does_not_reach_back():
    assert cs.brand_endings(["选的示例牌，后来第三周拿到了第一个面试"] * 2, ALIASES)["hits"] == []


@pytest.mark.parametrize("text", [
    "示例牌帮我改了简历，三天就过了",                   # 品牌在开头, 后面一小句短但是事实
    "示例牌贴了两晚，红肿退了点",
    "示例牌的贴用了一周，闭口少了",
    "去年在示例牌报的班 简历改了三版",                  # 空格切开的也一样
    "示例牌老师帮我改了第3版，2天就过了",
])
def test_brand_at_the_start_with_a_short_factual_tail_is_not_a_brand_ending(text):
    assert cs.brand_endings([text] * 20, ALIASES)["hits"] == []


@pytest.mark.parametrize("text", ["示例牌的课我上过", "示例牌这波挺懂行"])
def test_a_one_clause_comment_naming_the_brand_counts(text):
    """整条只有一小句时那一句就是收尾: 光看字分不出事实和评价, 都算(上限留了四分之一的余地)。"""
    assert cs.brand_endings([text] * 2, ALIASES)["hits"] == [0, 1]


def test_fix_line_names_the_clause_that_matched():
    res = cs.brand_endings(["去年也是这么过来的，示例牌，真的稳"] * 2 + ["改了三版，示例牌这波挺懂行"] * 2, ALIASES)
    assert res["excess"] == [1, 2, 3]
    assert "收尾「示例牌，真的稳」落在「示例牌」上" in res["fix"][1]
    assert "收尾「示例牌这波挺懂行」落在「示例牌」上" in res["fix"][2]
    assert "最后一句" not in res["fix"][1]


@pytest.mark.parametrize("text", ["示例牌还是挺靠谱的嘛", "示例牌真的挺好的呢", "示例牌这种服务真的没有", "选示例牌就对了嘛"])
def test_declarative_ne_ma_meiyou_are_not_questions(text):
    assert cs.brand_endings([text] * 2, ALIASES)["hits"] == [0, 1]


@pytest.mark.parametrize("text", [
    "示例牌能退款嘛", "示例牌你们觉得呢", "其他家呢，示例牌怎么收费呢", "示例牌有没有", "示例牌用过没有", "那示例牌呢",
])
def test_real_questions_without_a_question_mark_stay_exempt(text):
    assert cs.brand_endings([text] * 2, ALIASES)["hits"] == []


def test_two_char_brand_can_match_across_words():
    """已知的小毛病: 两个字的中文品牌按字面找, 会跨词撞上(合成品牌「天好」撞「今天好累」)。少见, 不另做排除; 钉住现状。"""
    assert cs.brand_endings(["跑了一整天，今天好累"] * 2, cs.brand_aliases("天好"))["hits"] == [0, 1]


# ── 3. 品牌叫法 ─────────────────────────────────────────────────────────────────────────────────

def test_aliases_from_brand_template_terms_and_english_part():
    assert cs.brand_aliases("示例牌", "示例牌留学", ["示例牌老师"]) == ["示例牌老师", "示例牌"]
    assert set(cs.brand_aliases("ABC轻享", "ABC轻享-秋天")) == {"ABC轻享", "ABC"}
    assert set(cs.brand_aliases("", "示例项目-秋招方向")) == {"秋招方向", "示例项目"}, "没有品牌就退回项目名"
    assert cs.brand_aliases(None, "", []) == []


def test_aliases_match_case_insensitively_and_not_inside_english_words():
    al = cs.brand_aliases("Abcd")
    assert cs.brand_endings(["用了一个月，abcd真的好用"] * 2, al)["hits"] == [0, 1]
    assert cs.brand_endings(["这个 xabcdy 的设计挺好"] * 2, al)["hits"] == []


def test_sub_brand_names_contain_the_brand():
    assert cs.brand_endings(["跟了半年，示例牌老师这点很踏实"] * 2, ALIASES)["hits"] == [0, 1]


def test_no_aliases_means_nothing_to_count():
    assert cs.brand_endings(BRAND_TAILS, [])["hits"] == []


# ── 4. 收尾句式重复: 只提示 ─────────────────────────────────────────────────────────────────────

def test_three_same_template_endings_get_a_hint_even_under_the_cap():
    texts = ["改了三版，示例牌这波挺懂行", "回得快，示例牌这步挺实在", "一路跟下来，示例牌这个挺难得"] + _set(0, 9)
    res = cs.brand_endings(texts, ALIASES)
    assert res["excess"] == [] and res["fix"] == {}, "12 条最多 3 条, 没超"
    assert res["thread_hint"] and "<品牌>这…" in res["thread_hint"] and "第 1 条、第 2 条、第 3 条" in res["thread_hint"]


def test_same_last_three_chars_is_a_template_too():
    texts = ["第一周就约到了，示例牌太省心了", "材料一次过，选示例牌真省心了", "换了方法以后，示例牌是真省心了",
             "看到第六十份那里我笑出声了"] + _set(0, 8)
    res = cs.brand_endings(texts, ALIASES)
    assert res["hits"] == [0, 1, 2] and res["excess"] == []
    assert "「…省心了」" in res["thread_hint"]


def test_two_alike_endings_are_not_a_template():
    res = cs.brand_endings(["材料一次过，示例牌太省心了", "第一周就约到了，示例牌太省心了", "简历第一行写的是什么呀"], ALIASES)
    assert res["thread_hint"] is None


@pytest.mark.parametrize("texts", [
    ["这个班怎么样", "那个老师怎么样", "线上课怎么样", "示例牌这波挺懂行"],        # 问句收尾, 不劝「换成提问」
    ["第一周就约到了，太省心了", "材料一次过，太省心了", "换了方法以后真是太省心了"],  # 没落在品牌上
])
def test_only_brand_endings_are_grouped_into_templates(texts):
    assert cs.brand_endings(texts, ALIASES)["thread_hint"] is None


# ── 5. score_comments ───────────────────────────────────────────────────────────────────────────

_PASS = {"passed": True, "needs_review": False, "profile": {}, "flags": [], "hard_fails": [], "ambiguous": [],
         "plan": [], "hints": [], "advisory": [], "calls": 2}


def _client_with_brand():
    c = _tv_client()
    c.rows["projects"][0].update(name=BRAND, brand=BRAND)
    return c


def _judge_reply(n, plan=None):
    return lambda m, u, kw: _Resp(200, {"passed": True, "needs_review": False, "hard_fails": [], "unjudged": [],
                                        "profile": {}, "code": {}, "ambiguous": [], "advisory": [], "calls": 9,
                                        "policy": {}, "plan": plan or [], "hints": [],
                                        "comments": [dict(_PASS) for _ in range(n)]})


FOUR = [{"slot": "q1", "text": "简历第一行写的是什么呀"},
        {"slot": "q2", "text": "改了三版简历才约到面试，示例牌这波挺懂行"},
        {"slot": "exp", "text": "老师每次都回得很快，示例牌确实让人安心"},
        {"slot": "reply", "text": "写的是最近一段实习做成了什么"}]


def test_score_comments_fails_the_excess_brand_endings(wired):
    calls, reply = wired
    plan = [{"slot": "q1", "why": f"判定甲{i}"} for i in range(3)]
    reply["fn"] = _judge_reply(4, plan)
    out = core.score_comments(_client_with_brand(), user_id=ME, link=LINK, comments=FOUR)
    rows = {r["slot"]: r for r in out["comments"]}
    assert rows["q2"]["passed"] is True and "style_fix" not in rows["q2"], "前 1 条留着"
    assert rows["exp"]["passed"] is False and "以品牌评价收尾" in rows["exp"]["style_fix"][0]
    assert out["passed"] is False and out["summary"]["passed"] == 3
    assert out["thread_fix"][0].startswith("整组 4 条里 2 条以品牌评价收尾") and "位 exp 改成" in out["thread_fix"][0]
    assert len(out["thread_fix"]) == core.SCORE_MAX_FIXES, "代码那一句在最前面, 判定服务的几条挤不掉它"
    assert out["style_next_step"] == core.COMMENT_STYLE_NEXT_STEP
    (_m, _u, _h, body, _t), = calls
    assert [c["text"] for c in body["comments"]] == [c["text"] for c in FOUR], "照原样送去判"


def test_score_comments_under_the_cap_is_untouched(wired):
    _, reply = wired
    reply["fn"] = _judge_reply(4)
    comments = [dict(FOUR[0]), dict(FOUR[1]), {"slot": "exp", "text": "示例牌的老师让我把数字写具体，第二周就约到了"},
                dict(FOUR[3])]
    out = core.score_comments(_client_with_brand(), user_id=ME, link=LINK, comments=comments)
    assert out["passed"] is True and "thread_fix" not in out and "style_next_step" not in out


def test_score_comments_reports_brand_endings_when_the_judge_is_down(wired):
    _, reply = wired
    reply["fn"] = lambda m, u, kw: _Resp(503, {"detail": "评论判定服务未开启"})
    out = core.score_comments(_client_with_brand(), user_id=ME, link=LINK, comments=FOUR)
    assert out["judge_status"] != "ok" and "passed" not in out
    assert [r["slot"] for r in out["style_issues"]] == ["exp"]
    assert "位 exp" in out["thread_fix"][0]


def test_template_aliases_count_for_score_comments(wired):
    _, reply = wired
    reply["fn"] = _judge_reply(4)
    c = _client_with_brand()
    c.rows["projects"][0].update(brand="", name="示例项目",
                                 default_params='{"delivery": {"layout": "blocks", "comments": '
                                                '{"min": 1, "max": 3, "label_reader": "读者：", "label_author": "作者：", '
                                                '"must_mention_any": ["示例牌"]}}}')
    out = core.score_comments(c, user_id=ME, link=LINK, comments=FOUR)
    assert [r["slot"] for r in out["comments"] if not r["passed"]] == ["exp"]


def test_round_two_is_told_to_resend_the_whole_set_in_order():
    """上限按每次送来的那一组算: 第二轮只送改过的几条, 上限就重算了; 回包和工具说明都要说整组照原顺序再送。"""
    from deskcore import tools
    assert "整组全部送, 顺序和这次一样" in core.COMMENT_STYLE_NEXT_STEP
    assert "整组全部送, 顺序和第一次一样" in tools.score_comments.__doc__


def test_comments_for_hands_out_the_brand_ending_line():
    out = core.comments_for(_client_with_brand(), user_id=ME, link=LINK)
    assert out["style"][-1] == cs.BRAND_ENDING_LINE and out["style"][:3] == list(cs.LINES)


# ── 6. 跟着笔记交付的评论 ───────────────────────────────────────────────────────────────────────

TPL = {"layout": "blocks", "comments": {"min": 1, "max": 3, "label_reader": "读者", "label_author": "作者"}}


def test_delivery_does_not_count_brand_endings():
    """交付那条路不数(一篇 2~3 条, 量过本来就合规; must_mention_any 的模板每条都要点名品牌, 数了只会误报)。"""
    tpl = {"layout": "blocks", "comments": {"min": 1, "max": 3, "label_reader": "读者", "label_author": "作者",
                                            "must_mention_any": [BRAND]}}
    factual = {"title": "甲", "body": "正文", "comments": [
        {"role": "reader", "text": "示例牌的贴用了一周，闭口少了"},
        {"role": "reader", "text": "示例牌贴了两晚，红肿退了点"},
        {"role": "author", "text": "示例牌这个贴片薄，出门也能贴"}]}
    verdict = {"title": "乙", "body": "正文", "comments": [
        {"role": "reader", "text": "用了一周，示例牌这波挺懂行"},
        {"role": "author", "text": "贴了两晚，示例牌确实让人安心"}]}
    assert delivery.assemble(tpl, [factual, verdict])["problems"] == []


def test_delivery_requirements_mention_the_brand_ending_cap():
    task = delivery.requirements(TPL)["task"]
    assert cs.BRAND_ENDING_SHORT in task
