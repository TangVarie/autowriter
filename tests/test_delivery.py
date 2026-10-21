"""交付版式由代码拼(2026-10-20)。

10-20 生产诊断: 版式交给写手那边的模型照规则排, 时灵时不灵; 老的独立写作台在代码里拼, 从没出过这个问题。现在模板存在
``projects.default_params.delivery``, score_drafts 存完档按模板拼整批交付稿。这里钉的是:

  · 拼出来的样子(表格 / 一篇一段)、话题的顺序与去重、条件话题;
  · 只报代码判得准的问题, 下标是写手传的位置;
  · 模板校验是严的(不认识的键、冒号标记都拒), CLI 写之前先校验、写回不丢别的键、仍存成 JSON 字符串;
  · 打分路径: 有模板才带 delivery, 没模板一个字不变, 模板坏了不拦存档, 发给 judge 的东西不变。

所有示例文本都是合成的。
"""

from __future__ import annotations

import json
import random
import threading
import time

import pytest

import config
import judge_client
import validator
from deskcore import cli, core, delivery, rulecheck
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
PROJ = "aaaaaaaa-0000-0000-0000-0000000000d1"

TABLE = {"layout": "table",
         "columns": [{"name": "方向", "field": "direction"}, {"name": "笔记", "field": "post"},
                     {"name": "评论", "field": "comments"}],
         "tags": {"content": 2, "fixed": ["#固定甲"], "conditional": [{"when_any": ["Alpha"], "add": "条件乙"}]},
         "comments": {"min": 1, "max": 2, "label_reader": "读者", "label_author": "作者",
                      "must_mention_any": ["示例品"]}}
BLOCKS = {"layout": "blocks", "tags": {"content": 1}}


# ══════════════════════════════════════════════════════════════════════
# 拼
# ══════════════════════════════════════════════════════════════════════

def test_table_is_a_markdown_table_with_one_row_per_draft():
    out = delivery.assemble(TABLE, [
        {"title": "标题一", "body": "第一段 a|b\n\n第二段", "tags": ["话题一", "话题二"], "direction": "方向一",
         "comments": [{"role": "reader", "text": "示例品怎么样"}, {"role": "author", "text": "示例品还行"}]},
        {"title": "标题二", "body": "只有一段", "tags": ["话题三", "话题四"], "direction": "方向二",
         "comments": [{"role": "reader", "text": "示例品贵吗"}]},
    ])
    lines = out["text"].split("\n")
    assert out["format"] == "table" and out["problems"] == []
    assert lines[0] == "| 方向 | 笔记 | 评论 |" and lines[1] == "| --- | --- | --- |"
    assert len(lines) == 4, "一篇一行, 正文里的换行要变成 <br>, 不能把表格撑破"
    assert lines[2] == ("| 方向一 | 【标题】标题一<br>【正文】第一段 a\\|b<br><br>第二段<br>#话题一 #话题二 #固定甲 | "
                        "读者示例品怎么样<br>作者示例品还行 |")
    assert lines[3].startswith("| 方向二 | 【标题】标题二<br>【正文】只有一段<br>#话题三 #话题四 #固定甲 |")


def _cells(row: str) -> list[str]:
    """按表格的规矩拆一行: 竖线前面连着奇数个反斜杠的是字面的竖线, 偶数个(含 0 个)的才是分隔符。"""
    out, cur, slashes = [], "", 0
    for ch in row.strip()[1:-1]:
        if ch == "|" and slashes % 2 == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
        slashes = slashes + 1 if ch == "\\" else 0
    return out + [cur]


def test_cells_with_a_lone_carriage_return_or_a_long_backslash_run_stay_on_one_row():
    """单独的 \\r 也是换行: 不换成 <br> 的话这一行在 GFM / markdown-it 里拆成两行; 一长串反斜杠后面跟竖线, 原来的写法每个起点
    各扫一遍, 平方级(独立复审 on aw#115 第八轮)。"""
    out = delivery.assemble(TABLE, [{"title": "标题", "body": "第一行\r第二行", "tags": ["话题一", "话题二"], "direction": "方向",
                                     "comments": [{"role": "reader", "text": "示例品"}]}])
    assert "\r" not in out["text"] and len(out["text"].splitlines()) == 3
    t0 = time.perf_counter()
    delivery.assemble(TABLE, [{"title": "标题", "body": "\\" * 50_000 + "|", "tags": ["话题一", "话题二"], "direction": "方向",
                               "comments": [{"role": "reader", "text": "示例品"}]}])
    assert time.perf_counter() - t0 < 0.5


@pytest.mark.parametrize("text", ["a|b", "a\\|b", "a\\\\|b", "\\|", "|", "末尾一个反斜杠\\"])
def test_a_backslash_before_a_pipe_does_not_split_the_cell(text):
    """正文里原本就有 \\| 时, 原来照抄成 \\\\|: 两个反斜杠是一个字面的反斜杠, 竖线成了分隔符, 这一行多出一格、整张表错位
    (codex review on aw#115)。"""
    out = delivery.assemble(TABLE, [{"title": "标题", "body": text, "tags": ["话题一", "话题二"], "direction": "方向",
                                     "comments": [{"role": "reader", "text": "示例品" + text}]}])
    row = out["text"].split("\n")[2]
    assert len(_cells(row)) == 3, row


def test_blocks_are_separated_by_a_blank_line_and_comments_get_their_labels():
    tpl = {"layout": "blocks", "tags": {"content": 1},
           "comments": {"min": 0, "max": 3, "label_reader": "【评论】", "label_author": "作者回复："}}
    out = delivery.assemble(tpl, [
        {"title": "甲", "body": "正文甲", "tags": ["话题"],
         "comments": [{"role": "reader", "text": "问一句"}, {"role": "author", "text": "答一句"}]},
        {"title": "乙", "body": "正文乙", "tags": "#话题"},
    ])
    assert out["text"] == ("【标题】甲\n【正文】正文甲\n#话题\n【评论】问一句\n作者回复：答一句"
                           "\n\n"
                           "【标题】乙\n【正文】正文乙\n#话题")
    assert out["problems"] == []


def test_tags_are_normalised_deduped_and_ordered_content_fixed_conditional():
    tpl = {"layout": "blocks",
           "tags": {"content": 3, "fixed": ["固定甲", "#固定乙"],
                    "conditional": [{"when_any": ["alpha", "贝塔"], "add": "#条件丙"},
                                    {"when_any": ["不会出现"], "add": "条件丁"}]}}
    out = delivery.assemble(tpl, [{"title": "说说 ALPHA", "body": "正文",
                                   "tags": ["#话题A", "话题a", "#话题B[话题]#", "#固定甲", "#话题C #话题D"]}])
    tag_line = out["text"].split("\n")[-1]
    # 写手的去重(不分大小写)、和固定话题重名的不占名额、截到 3 个; 然后固定的, 然后命中的条件话题
    assert tag_line == "#话题A #话题B #话题C #固定甲 #固定乙 #条件丙"
    assert out["problems"] == []


def test_a_trailing_tag_line_in_the_body_is_lifted_not_duplicated():
    """10-20 查生产库: 约六分之一的稿子末尾带一行 #话题。模板也拼一行的话, 交付稿里就是两行。"""
    out = delivery.assemble(BLOCKS, [{"title": "甲", "body": "正文第一句。\n\n#话题甲 #话题乙\n"}])
    assert out["text"] == "【标题】甲\n【正文】正文第一句。\n#话题甲"
    given = delivery.assemble(BLOCKS, [{"title": "甲", "body": "正文第一句。\n#旧话题", "tags": ["新话题"]}])
    assert given["text"] == "【标题】甲\n【正文】正文第一句。\n#新话题", "单独传了 tags 就以它为准"


def test_an_explicitly_empty_tags_list_is_taken_at_its_word():
    """传了 ``tags: []`` 就是"这篇不带写手的话题", 不能再退回正文末尾摘下来的那几个(codex review on aw#115): 写手把话题从
    tags 里删光了, 交付稿里却还是那几个。没传(或传了个空字符串, 模型常这么填缺省)才用摘下来的。末尾那行照摘, 不留在正文里。"""
    body = "正文第一句。\n#旧话题"
    empty = delivery.assemble(BLOCKS, [{"title": "甲", "body": body, "tags": []}])
    assert empty["text"] == "【标题】甲\n【正文】正文第一句。"
    assert empty["problems"] == [{"index": 0, "what": "话题标签 0 个, 这个项目要 1 个; 在 tags 里补齐"}]
    for missing in ({}, {"tags": None}, {"tags": ""}):
        out = delivery.assemble(BLOCKS, [{"title": "甲", "body": body, **missing}])
        assert out["text"] == "【标题】甲\n【正文】正文第一句。\n#旧话题", missing
    assert delivery.delivered_post(BLOCKS, {"title": "甲", "body": body, "tags": []}) == ("正文第一句。", [])


def test_tags_trailing_the_last_sentence_are_lifted_too():
    """「冲鸭！#秋招 #求职」: 话题跟在最后一句后面、不单独成行, 不摘的话交付稿里还是两份话题, 而且不报任何问题。"""
    tpl = {"layout": "blocks", "tags": {"content": 2, "fixed": ["固定甲"]}}
    out = delivery.assemble(tpl, [{"title": "甲", "body": "正文一段。冲鸭！#秋招 #求职", "tags": ["秋招", "求职"]}])
    assert out["text"] == "【标题】甲\n【正文】正文一段。冲鸭！\n#秋招 #求职 #固定甲"
    lifted = delivery.assemble(tpl, [{"title": "甲", "body": "正文。冲鸭！#秋招[话题]# #求职"}])
    assert lifted["text"].endswith("【正文】正文。冲鸭！\n#秋招 #求职 #固定甲") and lifted["problems"] == []
    # 正文中间的话题、和英文连写的 # 不动
    assert delivery.lift_trailing_tags("先说#中间话题 再说结尾") == ("先说#中间话题 再说结尾", [])
    assert delivery.lift_trailing_tags("在学C#入门") == ("在学C#入门", [])


def test_a_url_fragment_at_the_end_of_the_body_is_not_a_tag():
    """「详情见 https://example.com/#faq」: # 前面是 /, 原来当话题摘, 交付稿里网址断成 https://example.com/、faq 跑进话题行
    (codex review on aw#115)。网址里的 #片段 留在正文里; 它后面隔开跟着的话题照摘。"""
    out = delivery.assemble(BLOCKS, [{"title": "甲", "body": "详情见 https://example.com/#faq", "tags": ["话题"]}])
    assert out["text"] == "【标题】甲\n【正文】详情见 https://example.com/#faq\n#话题"
    for body in ("详情见 https://example.com/#faq", "详情见https://example.com/#faq", "看 www.example.com/#/a",
                 "详情见 example.com/#faq"):
        assert delivery.lift_trailing_tags(body) == (body, []), body
    assert delivery.lift_trailing_tags("详情见 https://example.com/#faq #秋招 #求职") == (
        "详情见 https://example.com/#faq", ["秋招", "求职"])
    assert delivery.lift_trailing_tags("地址 https://example.com/ #秋招") == ("地址 https://example.com/", ["秋招"])
    assert delivery.delivered_post(BLOCKS, {"title": "甲", "body": "详情见 https://example.com/#faq"})[0] == (
        "详情见 https://example.com/#faq")
    # 网址里的 #片段 也不算正文里的话题(codex review on aw#115): 交付稿的话题行里没有 faq, 查话题的规则也不该看见它
    assert delivery.delivered_post(BLOCKS, {"title": "甲", "body": "详情见 https://example.com/#faq", "tags": ["话题"]}) == (
        "详情见 https://example.com/#faq", ["话题"])


@pytest.mark.parametrize("line, tags", [
    ("学习C#入门", []), ("F#语言 和 1#楼", []), ("C#abc#def", []), ("学习C#入门 #编程", ["编程"]),
    ("#abc#def", ["abc", "def"]), ("#秋招[话题]#求职[话题]#", ["秋招", "求职"]), ("#学C#入门", ["学C", "入门"]),
    ("冲鸭！#秋招 #求职", ["秋招", "求职"]), ("冲鸭🔥#秋招", ["秋招"]), ("全角Ａ#秋招", ["秋招"]),
    ("详情见 https://example.com/#faq #秋招", ["秋招"]),
    # 收尾的那个 # 不开一串(codex review on aw#115): "#秋招[话题]#" 的第二个 # 是收尾, 后面隔着一段正文的 "C#入门" 不接在
    # 这串话题后面; 中文句子里没有空白, 往回找最近的 # 会一路找到它
    ("#秋招[话题]#今天学C#入门", ["秋招"]), ("上岸了#秋招[话题]#，学C#入门", ["秋招"]), ("看了#租房[话题]#1#楼的房子", ["租房"]),
    # 收尾的 # 后面是正文, 不是又一个话题: 整句留在正文里, 不摘(原来 "开心" 被当成话题摘走)
    ("今天拿到offer了#秋招[话题]#开心", ["秋招"]), ("#秋招[话题]#今天就到这里", ["秋招"]),
    # 带 [话题] 的是小红书的话题写法, 前一个字是英文字母数字也算(它不会是 "C#入门" 这种词的一部分)
    ("拿到offer#秋招[话题]#", ["秋招"]), ("冲刺2026#秋招[话题]# #求职[话题]#", ["秋招", "求职"]),
    # 话题本体到句读为止(codex review on aw#115): 中文句子里没有空白, 原来 "秋招，记得准备简历" 整个算话题, 后半句被摘走
    ("今天聊聊#秋招，记得准备简历", ["秋招"]), ("看这里#秋招（附模板）", ["秋招"]), ("冲鸭！#秋招！", ["秋招"]),
    ("#秋招、#求职", ["秋招", "求职"]), ("别错过#秋招…", ["秋招"]), ("#Node.js 和 #C++ #A&B", ["Node.js", "C++", "A&B"]),
    # 只停在真的标点上(独立复审 on aw#115 第八轮): 夹在英文字母中间的撇号、〇 々、全角的 ＆ ／ ＋、不断行的连字符都是话题的一部分;
    # 带 [话题] 的照旧到 [话题] 为止; ASCII 的 ~ 和 ⋯ ※ ‼ 也是标点
    ("#Kiehl's #护肤", ["Kiehl's", "护肤"]), ("#Levi\u2019s #穿搭", ["Levi\u2019s", "穿搭"]), ("#二〇二六考研", ["二〇二六考研"]),
    ("#Q＆A #A／B测试", ["Q＆A", "A／B测试"]), ("#Wi\u2011Fi", ["Wi\u2011Fi"]), ("#打工人！[话题]#", ["打工人！"]),
    ("#秋招~记得准备", ["秋招"]), ("#秋招⋯⋯记得准备", ["秋招"]), ("#秋招※记得准备", ["秋招"]), ("#秋招‼记得准备", ["秋招"]),
    ("#秋招 #求职。", ["秋招", "求职"]), ("#秋招、#求职、#offer", ["秋招", "求职", "offer"]), ("#ootd, #fashion", ["ootd", "fashion"]),
])
def test_finding_tags_lifting_tags_and_counting_agree_on_what_a_tag_is(line, tags):
    """哪个 # 算话题只在 validator.topic_tag_matches 认一处(codex review on aw#115): 正文里找话题(查话题的规则)、摘末尾话题
    (交付稿)、数字数三处一个口径。"C#入门" 前一个字是英文字母数字, 不是话题; 连写的 "#abc#def"、"#a[话题]#b[话题]#" 后一个
    # 紧接着前一个话题, 是; 全角字、emoji、标点后面的 # 也是; 带 [话题] 的不管前一个字是什么都是。"""
    assert delivery.body_tags(line) == tags
    kept, lifted = delivery.lift_trailing_tags(line)
    assert delivery.body_tags(kept) + lifted == tags
    if kept.strip():
        assert validator.count_chars(line) == validator.count_chars(kept)
    else:       # 整行话题连同话题之间的标点一起摘掉(独立复审 on aw#115 第八轮): 少掉的只是那几个标点
        assert validator.TAG_GAP.fullmatch(validator.HASHTAG.sub("", line))
    assert [m.start() for m in validator.topic_tag_matches(line)] == \
        [i for i, ch in enumerate(line) if ch == "#" and validator.is_topic_tag(line, i)]


@pytest.mark.parametrize("body, tags, kept, line", [
    ("第一段写点经验。\n最后祝大家顺利！\n#秋招 #求职。", ["秋招", "求职"], "第一段写点经验。\n最后祝大家顺利！", "#秋招 #求职 #固定甲"),
    ("第一段写点经验。\n#秋招、#求职、#offer", None, "第一段写点经验。", "#秋招 #求职 #固定甲"),
    ("第一段写点经验。\n#ootd, #fashion", ["ootd", "fashion"], "第一段写点经验。", "#ootd #fashion #固定甲"),
])
def test_a_tag_line_with_punctuation_between_or_after_the_tags_is_lifted_once(body, tags, kept, line):
    """话题本体到句读为止之后, 标点不再算进话题: 整行话题里夹着的 、 , 。 只认空白的话这一行摘不下来, 交付稿里两行话题, 还误报
    「话题标签 1 个」(独立复审 on aw#115 第八轮)。整行都是话题的, 夹着的标点跟着这一行一起摘掉。"""
    tpl = {"layout": "blocks", "tags": {"content": 2, "fixed": ["固定甲"]}}
    out = delivery.assemble(tpl, [{"title": "甲", "body": body, "tags": tags}])
    assert out["text"] == f"【标题】甲\n【正文】{kept}\n{line}" and out["problems"] == []


def test_a_tag_scoped_rule_sees_a_tag_with_an_apostrophe_whole():
    """「#Kiehl's」截成 Kiehl 的话, 「话题里要带 Kiehl's」误报必须改、「话题里不许带 Levi's」漏报(独立复审 on aw#115 第八轮)。"""
    rules = [{"id": "m-1", "severity": "hard", "rule_kind": "required_phrase", "rule_payload": {"any": ["Kiehl's"], "where": ["tags"]}},
             {"id": "m-2", "severity": "hard", "rule_kind": "forbidden_word", "rule_payload": {"words": ["Levi's"], "where": ["tags"]}}]
    hits, _stats = rulecheck.check(rules[:1], [{"title": "甲", "body": "换季护肤分享。\n#Kiehl's #护肤"}])
    assert hits == [], "Kiehl's 是一个话题, 必须带的话题带了"
    hits, _stats = rulecheck.check(rules[1:], [{"title": "乙", "body": "通勤穿搭分享。\n#Levi's #穿搭"}])
    assert [h["rule_id"] for h in hits] == ["m-2"], "不许带的话题查得到"


@pytest.mark.parametrize("tpl", [{"layout": "blocks", "tags": {"fixed": ["固定甲"]}}, {"layout": "blocks", "tags": {"content": 1}}])
def test_prose_after_a_plain_tag_is_never_lifted_out_of_the_body(tpl):
    """「今天聊聊#秋招，记得准备简历」原来整个 "秋招，记得准备简历" 算话题、被当成末尾话题摘走; 模板只有固定话题(content 0)时
    摘下来的又不用, 交付稿里这半句就没了, 一句不报(codex review on aw#115)。"""
    body = "第一段。\n今天聊聊#秋招，记得准备简历"
    assert delivery.lift_trailing_tags(body) == (body, [])
    out = delivery.assemble(tpl, [{"title": "甲", "body": body, "tags": ["话题"]}])
    assert "，记得准备简历" in out["text"]
    assert validator.count_chars(body) == validator.count_chars("第一段。\n今天聊聊，记得准备简历")


@pytest.mark.parametrize("body, kept, tags", [
    ("学了Node.js/Express，面试稳了！#秋招", "学了Node.js/Express，面试稳了！", ["秋招"]),        # 根本没有网址
    ("技术栈：Vue.js/React/Node.js，冲！#秋招 #前端", "技术栈：Vue.js/React/Node.js，冲！", ["秋招", "前端"]),
    ("详情见https://example.com/faq，冲鸭！#秋招 #求职", "详情见https://example.com/faq，冲鸭！", ["秋招", "求职"]),
    ("关注www.example.com，每天更新！#秋招", "关注www.example.com，每天更新！", ["秋招"]),
])
def test_a_url_earlier_in_the_sentence_does_not_keep_the_trailing_tags(body, kept, tags):
    """中文句子里没有空白, 「往前到最近一个空白」就是整句(codex review on aw#115): 句子前面随便有个网址、甚至 Node.js/ 这种
    技术栈写法, 末尾的话题就不摘了, 交付稿里两份话题。只有 # 前面紧挨着、一直是网址字符(ASCII、不带空白)的那一段像网址才算。"""
    assert delivery.lift_trailing_tags(body) == (kept, tags)
    out = delivery.assemble({"layout": "blocks", "tags": {"content": 2}}, [{"title": "甲", "body": body, "tags": tags}])
    assert out["text"] == f"【标题】甲\n【正文】{kept}\n" + " ".join("#" + t for t in tags)


def test_prose_after_an_inline_topic_stays_in_the_delivered_body():
    """最后一句中间夹着一个 "#秋招[话题]#"、后面还有话(codex review on aw#115): 原来摘末尾话题的正则在 [话题]# 那个可有可无的
    # 上回溯, 把收尾的 # 当成下一个话题的开头, 后面那半句被当成话题摘走 —— 传了 tags 的话那半句直接从交付稿里没了。"""
    tpl = {"layout": "blocks", "tags": {"content": 2}}
    body = "第一段。\n最后祝大家#秋招[话题]#顺利拿到offer！"
    out = delivery.assemble(tpl, [{"title": "甲", "body": body, "tags": ["秋招", "求职"]}])
    assert out["text"] == f"【标题】甲\n【正文】{body}\n#秋招 #求职"
    out = delivery.assemble(tpl, [{"title": "甲", "body": body}])
    assert out["text"] == f"【标题】甲\n【正文】{body}", "没传 tags 也不能把后半句拼成话题"
    assert delivery.delivered_post(tpl, {"title": "甲", "body": body}) == (body, ["秋招"])
    assert delivery.lift_trailing_tags("正文。\n#秋招[话题]#今天就到这里") == ("正文。\n#秋招[话题]#今天就到这里", [])


@pytest.mark.parametrize("body", ["#a" * 10_000 + "[", "#汉" * 10_000 + "[", "冲鸭#a" * 5_000 + "。"],
                         ids=["ascii", "cjk", "prose"])
def test_lifting_tags_is_linear(body):
    """摘末尾话题不用会回溯的正则(codex review on aw#115): 原来 "#a#a…#a[" 两万字要几秒, 一直握着 GIL。"""
    t0 = time.monotonic()
    delivery.lift_trailing_tags(body)
    delivery.body_tags(body)
    validator.count_chars(body)
    assert time.monotonic() - t0 < 0.3


def test_a_template_without_tags_leaves_the_body_alone():
    out = delivery.assemble({"layout": "blocks"}, [{"title": "甲", "body": "正文。\n#话题甲", "tags": ["别的"]}])
    assert out["text"] == "【标题】甲\n【正文】正文。\n#话题甲"


# ══════════════════════════════════════════════════════════════════════
# 问题: 只报代码判得准的, 下标按写手传的位置
# ══════════════════════════════════════════════════════════════════════

def test_problems_are_reported_against_the_callers_index():
    out = delivery.assemble(TABLE, [
        {"title": "【标题】甲", "body": "【正文】正文", "tags": ["只有一个"], "direction": "",
         "comments": [{"role": "reader", "text": "没提到品名"}, {"role": "author", "text": "示例品"},
                      {"role": "reader", "text": "示例品"}]},
        {"title": "乙", "body": "正文", "tags": ["一", "二"], "direction": "方向",
         "comments": [{"role": "boss", "text": "示例品"}]},
    ], indices=[4, 7])
    by = {}
    for p in out["problems"]:
        by.setdefault(p["index"], []).append(p["what"])
    first = " / ".join(by[4])
    assert "标题里已经有「【标题】」" in first and "正文里已经有「【正文】」" in first, "模型自己又包了一层标记"
    assert "话题标签 1 个, 这个项目要 2 个" in first
    assert "评论 3 条, 这个项目要 1~2 条" in first
    assert "第 1 条评论没提到「示例品」" in first and "第 2 条评论" not in first
    assert "表格有「方向」列, 这篇没带 direction" in first
    assert by[7] == ["第 1 条评论的 role 是「boss」, 只认 reader / author"]


def test_a_single_comment_not_wrapped_in_a_list_is_kept_and_a_bad_container_is_reported():
    """comments 不是列表(codex review on aw#115): 原来一律当 [], 项目 min 是 0 时一句不报, 写手给了的评论就从交付稿里没了。
    一条字符串 / 一条 {role, text} 认成一条(列表里每一项本来就认这两种写法); 认不出的外壳报出来; 没传 / null / "" 照旧是没有。"""
    tpl = {"layout": "blocks", "comments": {"min": 0, "max": 3, "label_reader": "读者：", "label_author": "作者："}}

    def one(comments):
        return delivery.assemble(tpl, [{"title": "甲", "body": "正文", "comments": comments}])

    assert one("示例评论")["text"] == "【标题】甲\n【正文】正文\n读者：示例评论"
    assert one({"role": "author", "text": "示例回复"})["text"] == "【标题】甲\n【正文】正文\n作者：示例回复"
    for empty in (None, "", "  "):
        assert one(empty) == {"format": "blocks", "text": "【标题】甲\n【正文】正文", "problems": []}, empty
    for bad in (5, True):
        out = one(bad)
        assert out["text"] == "【标题】甲\n【正文】正文"
        assert len(out["problems"]) == 1 and "comments 应该是列表" in out["problems"][0]["what"], bad


def test_comments_sent_as_a_json_string_are_unpacked():
    """模型常把嵌套的列表整个序列化成一串 JSON 传进来(codex review on aw#115)。当成一条评论的话, 原样的 JSON 带着「读者：」
    进了交付稿、一句不报; 解得开、解出来是评论列表 / 一条评论的, 照列表拼。解不开的、像 [捂脸R] 这种表情开头的照旧是一条。"""
    tpl = {"layout": "blocks", "comments": {"min": 1, "max": 3, "label_reader": "读者：", "label_author": "作者："}}

    def one(comments):
        return delivery.assemble(tpl, [{"title": "甲", "body": "正文", "comments": comments}])

    two = [{"role": "reader", "text": "示例评论"}, {"role": "author", "text": "示例回复"}]
    assert one(json.dumps(two, ensure_ascii=False)) == {
        "format": "blocks", "text": "【标题】甲\n【正文】正文\n读者：示例评论\n作者：示例回复", "problems": []}
    assert one(" " + json.dumps(two[1], ensure_ascii=False))["text"] == "【标题】甲\n【正文】正文\n作者：示例回复"
    for plain in ("[捂脸R]求链接", "[1, 2] 选哪个", "{不是 JSON}"):
        assert one(plain)["text"] == f"【标题】甲\n【正文】正文\n读者：{plain}", plain


def test_a_comment_without_a_string_text_is_reported_not_dropped_or_stringified():
    """没有 text 的({role, content} 这种)原来一句不报就没了; text 是列表 / 数字 / dict 的原来拿 str() 拼进交付稿(Python 的
    ['…']), 而查规则只认字符串、看不见它, 两边对不上(codex review on aw#115)。都算这一条形状不对: 报出来, 不拼。
    同样的东西序列化成一串 JSON 传进来也一样: 解出来是一个对象 / 一个列表的, 照对象 / 列表认, 每项再查形状。原来没有 text 的
    对象、夹着 null 的列表整段 JSON 带着「读者：」拼进去(作者的回复成了读者评论), 一句不报, 还算够条数。"""
    tpl = {"layout": "blocks", "comments": {"min": 0, "max": 9, "label_reader": "读者：", "label_author": "作者："}}
    comments = [{"role": "author", "content": "示例回复"}, {"role": "reader", "text": ["示例甲", "示例乙"]},
                {"role": "reader", "text": 12}, {"text": {"示例": 1}}, {"role": "reader", "text": None},
                {"role": "reader", "text": "示例评论"}, {"role": "reader", "text": "  "}]
    out = delivery.assemble(tpl, [{"title": "甲", "body": "正文", "comments": comments}])
    assert out["text"] == "【标题】甲\n【正文】正文\n读者：示例评论"
    assert [p["what"] for p in out["problems"]] == [
        f'第 {k} 条评论应该是 {{"role": "reader", "text": "..."}}' for k in (1, 2, 3, 4, 5)], "空白的 text 照旧不算一条、不报"
    one = delivery.assemble(tpl, [{"title": "甲", "body": "正文", "comments": {"role": "author", "content": "示例回复"}}])
    assert one["text"] == "【标题】甲\n【正文】正文" and len(one["problems"]) == 1
    shape = '第 {} 条评论应该是 {{"role": "reader", "text": "..."}}'
    for raw, delivered, bad in [
        (json.dumps({"role": "author", "content": "示例回复"}, ensure_ascii=False), "", [1]),
        (json.dumps({"comments": [{"role": "reader", "text": "示例评论"}]}, ensure_ascii=False), "", [1]),  # 外面再包一层: 不往里解
        (json.dumps([{"role": "author", "text": "示例回复"}, None, 3], ensure_ascii=False), "\n作者：示例回复", [2, 3]),
        ("[1, 2]", "", [1, 2]),
    ]:
        out = delivery.assemble(tpl, [{"title": "甲", "body": "正文", "comments": raw}])
        assert out["text"] == "【标题】甲\n【正文】正文" + delivered, raw
        assert [p["what"] for p in out["problems"]] == [shape.format(k) for k in bad], raw


def test_tags_sent_as_a_json_string_are_unpacked():
    """模型常把 tags 列表 json.dumps 成一串传进来(默认分隔是 ", "): 原来按空白切成 '["a",' 和 '"b"]', 交付稿里是
    #["a", #"b"], 一句不报, 话题数还算够(codex review on aw#115)。一项去掉空白后以 [ 开头、解得开是列表的, 列表里的字符串
    照常再切(# 和 [话题] 照摘)。"""
    tpl = {"layout": "blocks", "tags": {"content": 2}}
    for raw in (json.dumps(["话题甲", "话题乙"], ensure_ascii=False), json.dumps(["#话题甲", "话题乙[话题]#"]),
                [" " + json.dumps(["话题甲", "#话题乙"], ensure_ascii=False)], json.dumps(["#话题甲 #话题乙", 3])):
        out = delivery.assemble(tpl, [{"title": "甲", "body": "正文", "tags": raw}])
        assert out == {"format": "blocks", "text": "【标题】甲\n【正文】正文\n#话题甲 #话题乙", "problems": []}, raw
    assert delivery.delivered_post(tpl, {"title": "甲", "body": "正文", "tags": json.dumps(["话题甲"])}) == ("正文", ["话题甲"])
    assert delivery.split_tags("#话题甲 [话题]") == ["话题甲"], "不是 JSON 的照旧按空白切"


def test_deeply_nested_brackets_never_raise_out_of_the_shared_splitters():
    """tags / comments 是一长串 [ 的: json.loads 抛的是 RecursionError, 不是 ValueError(3.11 上 1000 层、3.13 上一万层就到)。
    原来直接抛出去, 拼交付稿和代码查规则都在 core 那边的 except Exception 里 —— 一篇坏的, 整批的 delivery 都没了、
    rule_checks_status 记成 error, 写得好好的那几篇也一起(codex review on aw#115)。照解不开的字符串处理。"""
    deep = "[" * 20_000
    tpl = {"layout": "blocks", "tags": {"content": 2},
           "comments": {"min": 0, "max": 3, "label_reader": "读者：", "label_author": "作者："}}
    for field in ("tags", "comments"):
        drafts = [{"title": "甲", "body": "正文", field: deep}, {"title": "乙", "body": "正文", "tags": ["话题甲"]}]
        out = delivery.assemble(tpl, drafts)
        assert "【标题】乙\n【正文】正文\n#话题甲" in out["text"], field
        assert delivery.delivered_post(tpl, drafts[0])[0] == "正文"
        assert rulecheck.check([], drafts) == ([], {"rules": 0, "skipped": 0})
        rule = {"id": "m-1", "content": "示例规则", "severity": "hard", "rule_kind": "forbidden_word",
                "rule_payload": {"words": ["示例词"], "where": [field]}}
        assert rulecheck.check([rule], drafts) == ([], {"rules": 1, "skipped": 0})


def test_comments_and_direction_are_not_checked_when_the_template_does_not_ask():
    out = delivery.assemble(BLOCKS, [{"title": "甲", "body": "正文", "tags": ["话题"]}])
    assert out["problems"] == []


# ══════════════════════════════════════════════════════════════════════
# 模板校验
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("bad, words", [
    ({**TABLE, "colour": "red"}, "不认识的键"),
    ({**TABLE, "tags": {**TABLE["tags"], "order": 1}}, "不认识的键"),
    ({**TABLE, "columns": [{"name": "笔记", "field": "post", "width": 3}]}, "不认识的键"),
    ({**TABLE, "comments": {**TABLE["comments"], "tone": "x"}}, "不认识的键"),
    ({**TABLE, "tags": {"conditional": [{"when_any": ["a"], "add": "b", "x": 1}]}}, "不认识的键"),
    ({**TABLE, "markers": {"title": "标题：", "body": "正文："}}, "冒号"),
    ({**TABLE, "layout": "list"}, "layout"),
    ({"layout": "table"}, "columns"),
    ({"layout": "table", "columns": [{"name": "方向", "field": "direction"}]}, "field=post"),
    ({"layout": "table", "columns": [{"name": "笔记", "field": "post"}, {"name": "评", "field": "comments"}]},
     "comments 没配"),
    ({"layout": "table", "columns": [{"name": "a", "field": "post"}, {"name": "b", "field": "post"}]}, "两次"),
    ({"layout": "table", "columns": [{"name": "a|b", "field": "post"}]}, "表头"),
    ({"layout": "blocks", "columns": [{"name": "笔记", "field": "post"}]}, "只给 table 用"),
    ({"layout": "blocks", "tags": {"content": 11}}, "0~10"),
    ({"layout": "blocks", "tags": {"content": True}}, "0~10"),
    ({"layout": "blocks", "tags": {"fixed": ["两个 词"]}}, "不是一个话题"),
    ({"layout": "blocks", "comments": {"min": 3, "max": 1}}, "min ≤ max"),
    ({"layout": "blocks", "comments": {"min": 0, "max": 1, "label_reader": "a\nb"}}, "label_reader"),
    ({"layout": "blocks", "comments": {"min": 1, "max": 2}}, "都要非空"),
    ({"layout": "blocks", "comments": {"min": 1, "max": 2, "label_reader": "读者：", "label_author": ""}}, "都要非空"),
    ({"layout": "table", "columns": [{"name": "内容", "field": "post"}], "comments": {"min": 2, "max": 3}},
     "没有 field=comments 的列"),
    ("table", "JSON 对象"),
])
def test_bad_templates_are_rejected_with_a_reason(bad, words):
    with pytest.raises(delivery.TemplateError) as exc:
        delivery.validate(bad)
    assert words in str(exc.value)


def test_validate_fills_defaults_and_is_idempotent():
    norm = delivery.validate(BLOCKS)
    assert norm["markers"] == {"title": "【标题】", "body": "【正文】"}
    assert norm["columns"] == [] and norm["comments"] is None
    assert delivery.validate(norm) == norm
    assert delivery.validate(delivery.validate(TABLE)) == delivery.validate(TABLE)


def test_default_params_is_read_as_a_json_string_or_a_dict():
    as_str = {"default_params": json.dumps({"isolation_terms": ["词"], "delivery": BLOCKS}, ensure_ascii=False)}
    assert delivery.template_of(as_str)["layout"] == "blocks"
    assert delivery.template_of({"default_params": {"delivery": BLOCKS}})["layout"] == "blocks"
    assert delivery.template_of({"default_params": json.dumps({"isolation_terms": ["词"]})}) is None
    assert delivery.template_of({"default_params": None}) is None
    with pytest.raises(delivery.TemplateError):
        delivery.template_of({"default_params": "{not json"})


# ══════════════════════════════════════════════════════════════════════
# CLI: 写之前先校验, 写回不丢别的键, 仍存成 JSON 字符串
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture
def cli_client(monkeypatch):
    c = FakeClient(rows={"projects": [{"id": PROJ, "name": "示例项目", "brand": "示例品牌", "owner_id": ME,
                                       "default_params": json.dumps({"isolation_terms": ["示例词"]},
                                                                    ensure_ascii=False)}]})
    monkeypatch.setattr(core, "sb", lambda: c)
    return c


def _params(c) -> dict:
    raw = c.rows["projects"][0]["default_params"]
    assert isinstance(raw, str), "生产库这一列全是 JSON 字符串, 别写成 dict 混进去"
    return json.loads(raw)


def test_cli_set_show_clear_round_trip(cli_client, tmp_path, capsys):
    f = tmp_path / "tpl.json"
    f.write_text(json.dumps(TABLE, ensure_ascii=False), encoding="utf-8")
    assert cli.main(["delivery-format", "--project", PROJ, "--set", str(f)]) == 0
    stored = _params(cli_client)
    assert stored["isolation_terms"] == ["示例词"], "写模板把隔离词抹掉了"
    assert stored["delivery"] == delivery.validate(TABLE)
    printed = capsys.readouterr().out
    assert "| 方向 | 笔记 | 评论 |" in printed, "设完要让运维看得见拼出来的样子"

    assert cli.main(["delivery-format", "--project", PROJ, "--show"]) == 0
    assert "【标题】示例标题" in capsys.readouterr().out

    assert cli.main(["delivery-format", "--project", PROJ, "--clear"]) == 0
    assert _params(cli_client) == {"isolation_terms": ["示例词"]}


def test_cli_refuses_a_bad_template_and_writes_nothing(cli_client, tmp_path, capsys):
    before = cli_client.rows["projects"][0]["default_params"]
    f = tmp_path / "bad.json"
    f.write_text(json.dumps({**BLOCKS, "markers": {"title": "标题："}}, ensure_ascii=False), encoding="utf-8")
    assert cli.main(["delivery-format", "--project", PROJ, "--set", str(f)]) == 1
    assert "模板没写进去" in capsys.readouterr().out
    assert cli_client.rows["projects"][0]["default_params"] == before
    assert not [x for x in cli_client.calls if x["op"] == "update"]


def test_cli_refuses_a_null_template_instead_of_treating_it_as_show(cli_client, tmp_path, capsys):
    """--set 的文件里是 JSON null: 原来和 --show 用的是同一个 None, 一个字不写、退出码 0, 运维以为设上了(codex review on
    aw#115)。要报错、退出码 1、不写; 删模板走 --clear。core 那边「只看」也换成了单独的记号, 直接传 None 一样要过校验。"""
    before = cli_client.rows["projects"][0]["default_params"]
    f = tmp_path / "null.json"
    f.write_text("null", encoding="utf-8")
    assert cli.main(["delivery-format", "--project", PROJ, "--set", str(f)]) == 1
    assert "--clear" in capsys.readouterr().out
    assert cli_client.rows["projects"][0]["default_params"] == before
    assert not [x for x in cli_client.calls if x["op"] == "update"]
    with pytest.raises(delivery.TemplateError):
        core.delivery_format(cli_client, PROJ, template=None)
    assert not [x for x in cli_client.calls if x["op"] == "update"]
    assert "written" not in core.delivery_format(cli_client, PROJ), "不传 template 仍是只看"
    assert not [x for x in cli_client.calls if x["op"] == "update"]


def test_cli_refuses_an_empty_set_path_instead_of_treating_it_as_show(cli_client, capsys):
    """--set "$TPL" 而 TPL 没设(部署脚本里常见): 原来按真假判, 空字符串当成没给 --set, 打印现有模板、退出码 0, 什么都没写
    (codex review on aw#115 第五轮)。要去读、报读不了、退出码 1、不写。"""
    before = cli_client.rows["projects"][0]["default_params"]
    assert cli.main(["delivery-format", "--project", PROJ, "--set", ""]) == 1
    assert "读不了模板文件" in capsys.readouterr().out
    assert cli_client.rows["projects"][0]["default_params"] == before
    assert not [x for x in cli_client.calls if x["op"] == "update"]


def test_expected_parts_are_what_the_template_asks_every_post_to_carry():
    """rulecheck 的 expects: 这些部位空着就是没交(必须带的话术照没带报)。"""
    assert delivery.expected_parts(TABLE) == ("tags", "comments")
    assert delivery.expected_parts(BLOCKS) == ("tags",)
    assert delivery.expected_parts({"layout": "blocks", "tags": {"fixed": ["固定甲"]}}) == ("tags",)
    assert delivery.expected_parts({"layout": "blocks", "tags": {"conditional": [{"when_any": ["x"], "add": "y"}]},
                                    "comments": {"min": 0, "max": 2, "label_reader": "读者：",
                                                 "label_author": "作者："}}) == ()


def test_cli_refuses_to_overwrite_default_params_it_cannot_parse(cli_client, tmp_path):
    cli_client.rows["projects"][0]["default_params"] = "{broken"
    f = tmp_path / "tpl.json"
    f.write_text(json.dumps(BLOCKS), encoding="utf-8")
    assert cli.main(["delivery-format", "--project", PROJ, "--set", str(f)]) == 1
    assert cli_client.rows["projects"][0]["default_params"] == "{broken", "拿 {} 去合并会把别的键一起抹掉"


# ══════════════════════════════════════════════════════════════════════
# 打分路径: score_drafts / commit_drafts
# ══════════════════════════════════════════════════════════════════════

_POOL = ("的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种面而方"
         "后多定行学法所民得经十三之进着等部度家电力里如水化高自二理起小物现实加量都两体制机当使点从业本去把性好应")


def distinct_drafts(n: int, seed: int = 0) -> list[dict]:
    """n 篇互不撞车的合成稿(同 test_deskcore_commit_judge 的造法), 免得查重闸先拦掉几篇。"""
    out = []
    for i in range(n):
        rng = random.Random(seed * 100 + i)
        body = "".join(f"这一段说的是{''.join(rng.choice(_POOL) for _ in range(18))}。" for _ in range(10))
        out.append({"title": f"第{i}篇{''.join(rng.choice(_POOL) for _ in range(6))}", "body": body})
    return out


def desk_client(*, default_params=None, memories=(), maps=()) -> FakeClient:
    proj = {"id": PROJ, "name": "示例项目", "brand": "示例品牌", "owner_id": ME, "calibration_notes": "",
            "tactics": "[]", "custom_roles": []}
    if default_params is not None:
        proj["default_params"] = default_params
    c = FakeClient(rows={"projects": [proj], "memories": list(memories), "tv_project_map": list(maps)})

    def _rpc(args):
        rows = []
        for k, r in enumerate(args.get("_rows") or []):
            c.rows.setdefault("draft_fingerprints", []).append({
                "id": f"fp-{r['version_id']}", "project_id": PROJ, "user_id": ME, "version_id": r["version_id"],
                "title": r["title"], "opening": r["opening"], "opening_hash": r["opening_hash"],
                "ngram_hashes": r["ngram_hashes"], "title_embedding": None, "embedding_model": None,
                "angle_key": r.get("angle_key"), "created_at": "2026-10-20T00:00:00+00:00"})
            rows.append({"idx": k, "status": core.COMMIT_STATUS_INSERTED, "collided_with": None, "detail": None})
        return rows
    c.rpc_impl["deskcore_commit_fingerprints"] = _rpc
    return c


@pytest.fixture(autouse=True)
def _no_embeddings(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    monkeypatch.setattr(config, "JUDGE_URL", "")


def _params_with(tpl) -> str:
    return json.dumps({"isolation_terms": ["示例词"], "delivery": tpl}, ensure_ascii=False)


def test_score_drafts_carries_the_assembled_batch_when_the_project_has_a_template():
    c = desk_client(default_params=_params_with(BLOCKS))
    drafts = distinct_drafts(2)
    drafts[0]["tags"] = ["话题甲"]
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert out["written"] == 2
    d = out["delivery"]
    assert d["format"] == "blocks"
    assert d["text"].count("【标题】") == 2 and "#话题甲" in d["text"]
    assert d["problems"] == [{"index": 1, "what": "话题标签 0 个, 这个项目要 1 个; 在 tags 里补齐"}]
    assert "delivery.text" in out["next_step"], "下一步要说清: 原样交 delivery.text"
    meta = c.rows["batch_metrics"][0]["meta"]
    assert meta["delivery"] == {"layout": "blocks", "drafts": 2, "problems": 1}


def test_rejected_drafts_are_not_in_the_delivery_and_problem_indices_follow_the_caller():
    c = desk_client(default_params=_params_with(BLOCKS))
    old = distinct_drafts(1, seed=9)[0]
    core.commit_drafts(c, PROJ, [old], user_id=ME)
    fresh = distinct_drafts(1, seed=3)[0]
    out = core.commit_drafts(c, PROJ, [{**old, "title": "换了个标题"}, fresh], user_id=ME)
    assert [r["index"] for r in out["rejected"]] == [0]
    assert out["delivery"]["text"].count("【标题】") == 1 and "换了个标题" not in out["delivery"]["text"]
    assert [p["index"] for p in out["delivery"]["problems"]] == [1]


def test_no_template_means_the_response_is_unchanged():
    """老项目、老调用方: 没模板就不带 delivery, 多传的字段也不改变任何别的返回值。"""
    plain = core.commit_drafts(desk_client(), PROJ, distinct_drafts(2), user_id=ME)
    extra = distinct_drafts(2)
    extra[0].update(tags=["话题"], comments=[{"role": "reader", "text": "评论"}], direction="方向")
    rich = core.commit_drafts(desk_client(), PROJ, extra, user_id=ME)
    for out in (plain, rich):
        assert "delivery" not in out and "rule_checks" not in out
    volatile = {"version_ids", "batch_id", "elapsed_ms", "phase_ms", "judge", "project_isolation"}
    assert {k: v for k, v in plain.items() if k not in volatile} == \
           {k: v for k, v in rich.items() if k not in volatile}


def test_the_brief_tells_the_writer_what_each_draft_must_carry():
    """不进简报的话, 第一次 score_drafts 才知道要几个话题 / 几条评论 / 要不要方向, 整批带着 problems 再存一次。"""
    c = desk_client(default_params=_params_with(TABLE))
    req = core.build_writing_brief(c, PROJ, user_id=ME)["delivery_requirements"]
    assert req == {"layout": "table", "tags": 2, "fixed_tags": ["固定甲"],
                   "comments": {"min": 1, "max": 2, "must_mention_any": ["示例品"]}, "direction": True,
                   "task": req["task"]}
    for words in ("tags 给 2 个话题", "#固定甲", "comments 给 1~2 条", "示例品", "direction", "不是 产品向 / 流量向"):
        assert words in req["task"]
    assert "delivery_requirements" not in core.build_writing_brief(desk_client(), PROJ, user_id=ME)
    broken = desk_client(default_params=json.dumps({"delivery": {"layout": "table"}}))
    assert "delivery_requirements" not in core.build_writing_brief(broken, PROJ, user_id=ME), "模板坏了不拦简报"


@pytest.mark.parametrize("tpl", [{"layout": "blocks"},
                                 {"layout": "blocks", "comments": {"min": 1, "max": 2, "label_reader": "读者：",
                                                                   "label_author": "作者："}}])
def test_a_template_that_does_not_manage_tags_keeps_them_in_the_body(tpl):
    """模板不管话题时 assemble 不读 tags 字段, 正文原样进交付稿。原来简报照样叫写手「不放话题行」, 照项目规则要的话题传在
    tags 里就从交付稿里没了, 一句不报(codex review on aw#115)。现在简报叫它写在正文末尾那一行; tags 里正文没有的报出来。"""
    assert not delivery.wants_tags(delivery.validate(tpl)), "前提: 这个模板不管话题"
    task = delivery.requirements(tpl)["task"]
    assert "不放话题行" not in task and "写在正文末尾那一行" in task and "tags 字段不会拼进交付稿" in task
    draft = {"title": "示例标题", "body": "示例正文。\n#话题甲", "comments": [{"role": "reader", "text": "示例评论"}]}
    kept = delivery.assemble(tpl, [draft])
    assert "#话题甲" in kept["text"] and kept["problems"] == [], "照简报写在正文里的话题原样带上"
    lost = delivery.assemble(tpl, [{**draft, "body": "示例正文。", "tags": ["话题甲", "#话题乙"]}], indices=[3])
    assert "话题甲" not in lost["text"]
    assert lost["problems"] == [{"index": 3, "what": "这个项目的交付模板不管话题, tags 里的「话题甲」「话题乙」没拼进交付稿: "
                                                     "要带话题就写在正文末尾那一行, tags 留空"}]
    both = delivery.assemble(tpl, [{**draft, "tags": ["话题甲"]}])
    assert both["problems"] == [], "正文里已经有的不算丢"


def test_a_template_that_manages_tags_still_keeps_the_tag_line_out_of_the_body():
    assert "不放话题行" in delivery.requirements(BLOCKS)["task"]


def test_a_broken_template_never_breaks_scoring():
    c = desk_client(default_params=json.dumps({"delivery": {"layout": "table"}}))
    out = core.commit_drafts(c, PROJ, distinct_drafts(1), user_id=ME)
    assert out["written"] == 1 and "delivery" not in out
    assert "columns" in c.rows["batch_metrics"][0]["meta"]["delivery"]["error"]


def test_tags_comments_and_direction_are_not_sent_to_the_judge(monkeypatch):
    monkeypatch.setattr(config, "JUDGE_URL", "https://judge.example.invalid")
    monkeypatch.setattr(config, "JUDGE_API_KEY", "jk")
    payloads, lock = [], threading.Lock()

    def fake(payload, *, timeout=None):
        with lock:
            payloads.append(payload)
        return judge_client.result(judge_client.JUDGE_OK, http_status=200, passed=True, calls=1, written=1)
    monkeypatch.setattr(judge_client, "judge_draft", fake)
    maps = [{"tv_project_id": "DEMO_phase1", "project_id": PROJ, "ingest_target": True}]

    plain = distinct_drafts(1)
    core.commit_drafts(desk_client(maps=maps), PROJ, plain, user_id=ME)
    rich = [{**plain[0], "tags": ["话题甲"], "direction": "方向",
             "comments": [{"role": "reader", "text": "示例评论"}]}]
    core.commit_drafts(desk_client(maps=maps, default_params=_params_with(TABLE)), PROJ, rich, user_id=ME)

    assert len(payloads) == 2
    a, b = ({k: v for k, v in p.items() if k != "subject_id"} for p in payloads)
    assert a == b, "加了话题 / 评论 / 方向之后发给 judge 的东西变了"
    assert b["body"] == plain[0]["body"]
    assert not {"tags", "comments", "direction"} & set(b)
