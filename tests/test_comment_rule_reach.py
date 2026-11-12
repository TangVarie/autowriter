"""2026-11-11 审计 C1 / F5 / C7(docs/deskcore.md §3.24): 写手说过的评论规矩下一组评论拿得到; 评论位的说法不再招来被拒的调用。

钉住:
  规则
  1. ``_is_comment_rule``: 「评论区」开头(开头有一个开括号也认)、开头那一句(到第一个标点)的前 12 个字里有「评论」、
     或者存的检查的 where 带 comments, 三样有一样就算; 说交付版式、过了开头那一句才提到评论的不算;
  2. comments_for: 认出来的 soft 规则进 comment_rules; hard 的从 hard_rules 换到 comment_rules, 两个列表合起来一条不少、
     也不重复; 笔记那一侧的简报里这些规则照旧在(P0 / P1), 只是不算「说到字数」;
  3. record_rule 回 ``given_when_writing_comments``; 开头那一句只说评论的, check 的 where 存成只查评论、回一句;
     开头也点到正文 / 标题 / 笔记的不收窄;
  4. 库里已经存着的那种(where 带着 body)读的时候按同一个判据算: score_drafts 不拿它查正文, my_rules 给的是真用的那一份。
  评论位
  5. 定了配比的项目, comments_for 的 next_step 说四个位是四种评论、一个 id 最多用一次、其余的不带 slot、整组一次交;
     没定配比的项目那一句一个字不动;
  6. slot id 重复 / 不认识的两种报错(照真实的工具路径跑): 说去掉多出来那几条的 slot、整组再送一次, 不叫分几次判;
     照着做(四条带 id、其余不带)一次就判成;
  7. 整组参考里题目的 id 换成人话, 标明只是参考; 带 slot 的那几句不动。
规则原文、品牌、评论全是合成的: 只照生产里见过的开头形状造, 不引用真实规则。
"""
from __future__ import annotations

import pytest

from deskcore import core, rulecheck, tools
from tests.test_comment_rules_1111 import _with_rules
from tests.test_comment_set_score import BRANDED, CFG, COL, PLAIN, _acts, _client, _judge, _sent
from tests.test_delivery import PROJ, desk_client, distinct_drafts
from tests.test_deskcore_judge_tools import _Resp, wired  # noqa: F401 — fixture
from tests.test_deskcore_score_and_comments import LINK, ME, THREAD_OK
from tests.test_rulecheck import _rule, rule_client  # noqa: F401 — fixture

WORDS = {"kind": "forbidden_word", "words": ["示例禁词"]}

# 生产里见过的开头形状, 文字是造的
COMMENT_HEADS = [
    "评论区：示例牌不用代称",                               # 协议教的写法
    "  评论区和回评里不带链接",                             # 「评论区」开头, 没有冒号
    "【评论区示例纪律】1. 先答问题；2. 不带链接",           # 括号里的小标题
    "【评论区示例红线】不留联系方式。不带链接",
    "评论不堆形容词、不喊口号：写具体的事",                 # 「评论」开头, 不带「区」
    "示例牌X9 的评论不聊价格：不写几折、便宜多少",          # 品牌 + 空格 + 「的评论」
    "回评评论先后顺序（示例版）：先答问题",                 # 第一个标点是括号
    "这一组要发的评论先按时间排好，早的在前",               # 「评论」在第 7、8 个字
    "每篇稿子的评论区留两条【示例标签】（各一句）",
    "文案与评论区中都用全称（如示例牌），不用缩写",         # 两边都管: 写评论时照给
]
NOT_COMMENT_HEADS = [
    "【交付格式·示例版】每篇分两栏交，右边一栏放评论",      # 交付版式: 过了开头那一句才提到评论
    "【交付排版示例】成稿先放标题：再放正文，最后放评论",
    "交付时一篇一个文档：先正文，后评论",
    "标题先抛问题，评论区再给答案",
    "不管哪一部分、标题还是评论都不写「示例词」",
    "提问帖写法：提问帖的评论和正文一起交",
    "正文不放购买方式，放到评论区",
    "不能承诺效果",
]


def _c(content, **kw):
    return {"id": "r", "content": content, "rule_kind": None, "rule_payload": None, **kw}


# ── 1. 判据 ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("content", COMMENT_HEADS)
def test_heads_that_name_comments_are_comment_rules(content):
    assert core._is_comment_rule(_c(content)) is True


@pytest.mark.parametrize("content", NOT_COMMENT_HEADS)
def test_delivery_format_and_post_rules_are_not_comment_rules(content):
    assert core._is_comment_rule(_c(content)) is False, "过了开头那一句才提到评论的, 不给写评论的人"


def test_the_head_is_the_first_twelve_characters_before_any_punctuation():
    assert core._is_comment_rule(_c("一二三四五六七八九十评论要短")) is True, "「评论」在第 11、12 个字"
    assert core._is_comment_rule(_c("一二三四五六七八九十一评论要短")) is False, "「评论」跨过了第 12 个字"
    assert core._is_comment_rule(_c("示例牌 的 评论 别太长")) is True, "空格不算标点"
    assert core._is_comment_rule(_c("写法/评论都要短")) is False, "斜杠是标点"
    assert core._is_comment_rule(_c("写法\n评论要短")) is False, "换行也断句"
    assert core._is_comment_rule(_c("【【评论区】】要短")) is False, "只去一个开括号"
    assert core._is_comment_rule(_c(None)) is False and core._is_comment_rule({}) is False
    assert core._rule_head("（评论区）每条一句") == "评论区" and core._rule_head("  不承诺效果。别的") == "不承诺效果"


def test_a_stored_check_on_comments_makes_it_a_comment_rule():
    text = "称呼读者一律用「你」，不用敬称"
    assert core._is_comment_rule(_c(text)) is False
    for where in (["body", "comments"], ["comments"]):
        assert core._is_comment_rule(_c(text, rule_kind="forbidden_word",
                                        rule_payload={"words": ["您"], "where": where})) is True
    assert core._is_comment_rule(_c(text, rule_kind="forbidden_word", rule_payload={"words": ["您"]})) is False
    assert core._is_comment_rule(_c("正文别太短", rule_kind="max_len", rule_payload={"min": 300, "where": "body"})) is False
    assert core._is_comment_rule(_c(text, rule_kind="forbidden_word", rule_payload={"words": []})) is False, "坏的检查不算"
    assert core._is_comment_rule(_c(text, rule_kind="free_text", rule_payload={"where": ["comments"]})) is False, "撤掉了的检查不算"


# ── 2. comments_for / 简报 ──────────────────────────────────────────────────────────────────────

def _mem(i, content, severity="soft", **kw):
    return {"id": f"r-{i}", "project_id": None, "scope": "project", "status": "confirmed", "severity": severity,
            "content": content, "memory_type": "rule", "created_at": f"2026-10-{i + 1:02d}", **kw}


def test_what_a_writer_said_about_comments_reaches_the_next_comment_brief():
    """11-11: 写评论那场对话里记的三条都不是「评论区：」开头, 四分钟后的 comments_for 一条也没给回去。"""
    rules = [_mem(0, "评论不堆形容词、不喊口号：写具体的事"),
             _mem(1, "示例牌X9 的评论不聊价格：不写几折", rule_kind="forbidden_word",
                  rule_payload={"words": ["几折"], "where": ["body", "comments"]}),
             _mem(2, "称呼读者一律用「你」，不用敬称", rule_kind="forbidden_regex",
                  rule_payload={"pattern": "您们?", "where": ["body", "comments"]}),
             _mem(3, "【评论区示例红线】不留联系方式"),
             _mem(4, "【交付格式·示例版】每篇分两栏交，右边一栏放评论"),
             _mem(5, "正文结尾留白")]
    out = core.comments_for(_with_rules(_client(cfg=None), rules), user_id=ME, link=LINK)
    assert out["comment_rules"] == [r["content"] for r in rules[:4]]
    assert out["hard_rules"] == [] and out["rules_note"] == core.RULE_SELF_SERVICE


def test_hard_comment_rules_move_lists_and_nothing_is_lost_or_doubled():
    rules = [_mem(0, "【评论区示例纪律】先答问题", "hard"), _mem(1, "回评评论先后顺序（示例版）：先答问题", "hard"),
             _mem(2, "不能承诺效果", "hard"), _mem(3, "【交付格式·示例版】每篇分两栏交，右边一栏放评论", "hard"),
             _mem(4, "不管哪一部分、标题还是评论都不写「示例词」", "hard"), _mem(5, "评论区：示例牌不用代称"),
             _mem(6, "正文结尾留白")]
    out = core.comments_for(_with_rules(_client(cfg=None), rules), user_id=ME, link=LINK)
    assert out["comment_rules"] == [rules[0]["content"], rules[1]["content"], rules[5]["content"]]
    assert out["hard_rules"] == [rules[2]["content"], rules[3]["content"], rules[4]["content"]]
    both = out["comment_rules"] + out["hard_rules"]
    assert len(both) == len(set(both)) and set(both) == {r["content"] for r in rules[:6]}, \
        "生效的硬约束和管评论的软规则各在一个列表里, 一条不少; 正文偏好不带"


def test_the_post_brief_still_carries_comment_rules_but_not_the_length_tail():
    """笔记那一侧不动: 管评论的硬约束照旧在 P0(不从写笔记的人眼前拿走), 只是它说到的字数不给笔记补那一句。"""
    rows = [_rule(1, content="评论每条不超过三十字", severity="hard"),
            _rule(2, content="【评论区】评论 20 字以内", severity="soft"),
            _rule(3, content="称呼读者一律用「你」", severity="soft", rule_kind="forbidden_word",
                  rule_payload={"words": ["您"], "where": ["comments"]}),
            _rule(4, content="不能承诺效果", severity="hard")]
    rows[2]["content"] += "，字数不限"
    out = core.build_writing_brief(desk_client(memories=rows), PROJ, user_id=ME)
    assert "评论每条不超过三十字" in out["p0"] and "【评论区】评论 20 字以内" in out["p1"]
    for tail in (core.LENGTH_RULE_TAIL_HARD, core.LENGTH_RULE_TAIL):
        assert tail not in out["p0"] + out["p1"]


def test_a_post_rule_that_mentions_comments_later_still_gets_the_length_tail():
    rows = [_rule(1, content="正文不超过三百字，评论另算", severity="hard")]
    out = core.build_writing_brief(desk_client(memories=rows), PROJ, user_id=ME)
    assert core.LENGTH_RULE_TAIL_HARD in out["p0"]


# ── 3. record_rule ──────────────────────────────────────────────────────────────────────────────

def test_record_rule_says_whether_comment_writing_will_get_the_rule(rule_client):
    yes = core.record_rule(rule_client, PROJ, "评论不堆形容词、不喊口号：写具体的事", user_id=ME)
    no = core.record_rule(rule_client, PROJ, "结尾不要总结句", user_id=ME)
    by_check = core.record_rule(rule_client, PROJ, "称呼读者一律用「你」，不用敬称", user_id=ME,
                                check={"kind": "forbidden_regex", "pattern": "您们?", "where": ["body", "comments"]})
    assert (yes["given_when_writing_comments"], no["given_when_writing_comments"]) == (True, False)
    assert by_check["given_when_writing_comments"] is True and "check_where_note" not in by_check
    assert by_check["check"]["where"] == ["body", "comments"], "开头没说评论的, where 照传的存"
    doc = tools.record_rule.__doc__
    assert "``given_when_writing_comments``: 这条规则写评论时(comments_for)给不给" in doc and "「评论区：」" in doc


def test_the_field_agrees_with_what_comments_for_hands_out(rule_client):
    texts = ["评论区：示例牌不用代称", "【评论区示例红线】不留联系方式", "标题先抛问题，评论区再给答案", "不能承诺效果"]
    said = [core.record_rule(rule_client, PROJ, t, user_id=ME)["given_when_writing_comments"] for t in texts]
    hard, soft = core.store.shared_memories(rule_client, PROJ, ME)
    assert said == [core._is_comment_rule(r) for r in sorted(hard + soft, key=lambda r: texts.index(r["content"]))]
    assert said == [True, True, False, False]


@pytest.mark.parametrize("check", [
    {**WORDS, "where": ["body", "comments"]},       # 11-11 生产里那一条的形状
    dict(WORDS),                                    # 没写 where: 默认是标题 + 正文, 一条评论规矩查的全是笔记
    {**WORDS, "where": ["title", "body", "tags", "comments"]},
    {"kind": "required_phrase", "any": ["示例话术"], "where": ["body", "comments"]},
    {"kind": "forbidden_regex", "pattern": "示例{2,}"},
])
def test_a_comment_only_head_stores_the_check_on_comments_only(rule_client, check):
    out = core.record_rule(rule_client, PROJ, "示例牌X9 的评论不聊价格：不写几折", user_id=ME, check=check)
    row = rule_client.rows["memories"][0]
    assert row["rule_payload"]["where"] == ["comments"] and out["check"]["where"] == ["comments"]
    assert out["check_where_note"] == core.COMMENT_CHECK_NOTE and out["given_when_writing_comments"] is True
    assert core.COMMENT_CHECK_NOTE == (
        "这条规则开头说的是评论, 所以它的 check 只查评论(where 存成 [\"comments\"]), 存档时不拿它查标题和正文。"
        "标题、正文也要这样守的话, 另记一条说正文的规则。")


@pytest.mark.parametrize("content, check, where", [
    ("评论和正文里都不要出现示例禁词", {**WORDS, "where": ["body", "comments"]}, ["body", "comments"]),
    ("文案与评论区中都不出现示例禁词", dict(WORDS), ["title", "body"]),
    ("每篇笔记的评论里不出现示例禁词", {**WORDS, "where": ["body", "comments"]}, ["body", "comments"]),
    ("评论和话题里都不带示例禁词", {**WORDS, "where": ["tags", "comments"]}, ["tags", "comments"]),
    ("标题先抛问题，评论区再给答案", {**WORDS, "where": ["title", "comments"]}, ["title", "comments"]),
    ("结尾不要引导评论，不写「示例禁词」", {**WORDS, "where": ["body"]}, ["body"]),
    ("引导评论的那一句放文末，别放开头", dict(WORDS), ["title", "body"]),
    ("评论区：示例牌不用代称", {**WORDS, "where": ["comments"]}, ["comments"]),
    # 2026-11-12 复审 C1 ── 第一句(到句号 / 分号)里过了第一个逗号才点到正文 / 标题 / 笔记的: 说的是笔记, 不收窄
    ("示例禁词只在评论区出现，正文和标题不写", {**WORDS, "where": ["title", "body"]}, ["title", "body"]),
    ("示例禁词只放评论，正文不提", dict(WORDS), ["title", "body"]),
    ("示例禁词只放评论，笔记里不提", {**WORDS, "where": ["body", "comments"]}, ["body", "comments"]),
    # 明说了 where、里面没有 comments 的: 调用方定的, 照存(说的是笔记里求评论的那种句子)
    ("不写求评论求关注的句子", {**WORDS, "where": ["body"]}, ["body"]),
    ("别说评论区告诉你", {**WORDS, "where": ["body"]}, ["body"]),
    ("不要让读者去评论区找链接", {**WORDS, "where": ["title", "body"]}, ["title", "body"]),
    ("示例牌X9 的评论不聊价格：不写几折", {"kind": "forbidden_regex", "pattern": "示例{2,}", "where": ["body"]}, ["body"]),
    # codex review on aw#153 ── 逗号后面用「全文 / 文末 / 通篇」点到笔记的, 和写「正文」的一样不收窄
    ("评论不写示例禁词，全文也不写", {**WORDS, "where": ["body", "comments"]}, ["body", "comments"]),
    ("评论里不出现示例禁词，文末同样不出现", {**WORDS, "where": ["body", "comments"]}, ["body", "comments"]),
    ("评论不带示例禁词，通篇都不带", dict(WORDS), ["title", "body"]),
    # 2026-11-12 复审 X-1 ── 说的是这个词该待在哪儿(只能在评论区), 不是评论该怎么写: 没写 where 也不收窄, 检查照旧查标题和正文
    ("示例禁词只能出现在评论区", dict(WORDS), ["title", "body"]),
    ("示例禁词仅限评论区出现", dict(WORDS), ["title", "body"]),
    ("评论区才能提示例禁词", dict(WORDS), ["title", "body"]),
])
def test_heads_that_also_name_the_post_keep_the_where_they_were_given(rule_client, content, check, where):
    """两边都管的规则收窄了就悄悄少查正文: 开头那一句点到正文 / 标题 / 笔记 / 话题、或者说的是笔记里引导评论的,
    where 照传的存。"""
    out = core.record_rule(rule_client, PROJ, content, user_id=ME, check=check)
    assert rule_client.rows["memories"][0]["rule_payload"]["where"] == where
    assert "check_where_note" not in out


def test_a_post_rule_whose_first_clause_names_comments_checks_the_post(rule_client):
    """2026-11-12 复审 C1: 「只在评论区出现, 正文和标题不写」原来只看第一个逗号之前那半句, where 被改成只查评论 —— 正文
    里写了不报, 评论里写了(规则要它在那儿)反而报「必须改」; 带着 where 再记一次还是被改回去, 想存的检查存不进去。"""
    content, check = "示例禁词只在评论区出现，正文和标题不写", {**WORDS, "where": ["title", "body"]}
    core.record_rule(rule_client, PROJ, content, user_id=ME, severity="hard", check=check)
    row = rule_client.rows["memories"][0]
    drafts = [{"title": "甲", "body": "正文里写了示例禁词", "comments": ["评论里也有示例禁词"]}]
    assert [h["where"] for h in rulecheck.check([core._comment_check_rule(row)], drafts)[0]] == ["body"]
    # 没写 where 被收窄的(回包里说了), 带着 where 再记一次就照存
    first = core.record_rule(rule_client, PROJ, "不写求评论的句子", user_id=ME, check=dict(WORDS))
    assert first["check"]["where"] == ["comments"] and first["check_where_note"] == core.COMMENT_CHECK_NOTE
    again = core.record_rule(rule_client, PROJ, "不写求评论的句子", user_id=ME, check={**WORDS, "where": ["title", "body"]})
    assert again["check"]["where"] == ["title", "body"] and "check_where_note" not in again


def test_a_word_that_may_only_appear_in_comments_is_still_checked_in_the_post(rule_client):
    """2026-11-12 复审 X-1: 「示例禁词只能出现在评论区」不带 where 记下来, 原来被当成只说评论的规则、where 存成只查评论 ——
    正文里写了不报, 评论里写了(规则要它在那儿)反而报「必须改」。"""
    core.record_rule(rule_client, PROJ, "示例禁词只能出现在评论区", user_id=ME, severity="hard", check=dict(WORDS))
    row = rule_client.rows["memories"][0]
    drafts = [{"title": "甲", "body": "正文里写了示例禁词", "comments": ["评论里也有示例禁词"]}]
    assert [h["where"] for h in rulecheck.check([core._comment_check_rule(row)], drafts)[0]] == ["body"]
    # 库里已经存着、where 带着 comments 的同一种说法: 读的时候也不收窄
    stored = {"content": "示例禁词只能出现在评论区", "rule_kind": "forbidden_word",
              "rule_payload": {"words": ["示例禁词"], "where": ["body", "comments"]}}
    assert core._comment_check_rule(stored) is stored


def test_reading_narrows_only_stored_checks_that_include_comments():
    """库里已经存着的: where 里带 comments、第一句又只说评论的才按只查评论算(11-11 线上那一条的形状, 冒号后面带着
    「话题」也照收窄); where 里没有 comments 的是记的时候定下的, 读的时候不再改。"""
    stored = _rule(1, "forbidden_word", {"words": ["几折"], "where": ["body", "comments"]},
                   content="示例牌X9 的评论不聊价格：不写几折，回避优惠力度这个话题")
    assert core._comment_check_rule(stored)["rule_payload"]["where"] == ["comments"]
    post_only = _rule(2, "forbidden_word", {"words": ["几折"], "where": ["title", "body"]}, **STORED)
    assert core._comment_check_rule(post_only) is post_only
    tail = _rule(3, "forbidden_word", {"words": ["几折"], "where": ["body", "comments"]}, content="几折只放评论，正文不提")
    assert core._comment_check_rule(tail) is tail


def test_a_length_check_on_a_comment_only_rule_is_not_stored(rule_client):
    """2026-11-12 复审 C3: max_len 只数标题 / 正文。「评论每条不超过三十字」带着它存下去, 每篇笔记存档都报
    「正文 N 字, 上限 30」(硬约束就是「必须改」), 评论反而一条不查。规则原文照记, check 不存, 回一句。"""
    out = core.record_rule(rule_client, PROJ, "评论每条不超过三十字", user_id=ME, severity="hard",
                           check={"kind": "max_len", "max": 30})
    row = rule_client.rows["memories"][0]
    assert row["content"] == "评论每条不超过三十字" and row.get("rule_kind") is None and row.get("rule_payload") is None
    assert "check" not in out and out["given_when_writing_comments"] is True
    assert out["check_where_note"] == core.COMMENT_LEN_CHECK_NOTE == (
        "这条规则开头说的是评论, 字数检查(max_len)只数标题 / 正文, 所以没存 check; 规则原文记下了, 写评论时照给。")
    c = desk_client(memories=[_rule(1, row.get("rule_kind"), row.get("rule_payload"), content=row["content"])])
    assert "rule_checks" not in core.commit_drafts(c, PROJ, distinct_drafts(2), user_id=ME)
    assert "max_len" in tools.record_rule.__doc__ and "只说评论的规则带 max_len 时不存 check" in tools.record_rule.__doc__


@pytest.mark.parametrize("content, where", [("正文不超过三百字，评论另算", "body"), ("评论和正文都不超过三百字", "body"),
                                            ("评论每条不超过三十字，正文不超过三百字", "body"), ("标题不超过二十字", "title")])
def test_length_checks_on_post_rules_are_stored_as_before(rule_client, content, where):
    out = core.record_rule(rule_client, PROJ, content, user_id=ME, check={"kind": "max_len", "max": 300, "where": where})
    assert out["check"] == {"kind": "max_len", "where": where, "max": 300} and "check_where_note" not in out


def test_a_muted_rule_is_not_said_to_reach_comment_writing(rule_client):
    """2026-11-12 复审 C6: 停用着的规则再记一次(原文一样, 命中同一行, 停用不会被清掉), 回包原来说写评论时会给 —— 其实
    comments_for 不给。按库里那一行算, 停用着就回 false, 并说一句怎么恢复。"""
    first = core.record_rule(rule_client, PROJ, "评论区：不带链接", user_id=ME)
    assert first["given_when_writing_comments"] is True and "muted_note" not in first
    core.set_rule_state(rule_client, first["memory_id"], "mute", user_id=ME)
    again = core.record_rule(rule_client, PROJ, "评论区：不带链接", user_id=ME)
    assert again["memory_id"] == first["memory_id"] and core._comment_rules(rule_client, PROJ, ME) == ([], [])
    assert again["given_when_writing_comments"] is False
    assert again["muted_note"] == core.RULE_MUTED_NOTE and "unmute" in core.RULE_MUTED_NOTE
    core.set_rule_state(rule_client, first["memory_id"], "unmute", user_id=ME)
    back = core.record_rule(rule_client, PROJ, "评论区：不带链接", user_id=ME)
    assert back["given_when_writing_comments"] is True and "muted_note" not in back
    assert "``muted_note``" in tools.record_rule.__doc__


# ── 4. 库里已经存着的那种 ───────────────────────────────────────────────────────────────────────

STORED = dict(content="示例牌X9 的评论不聊价格：不写几折", severity="soft")


def _with_word(word: str) -> list[dict]:
    drafts = distinct_drafts(2)
    drafts[0]["body"] += word
    return drafts


def test_a_stored_comment_rule_is_not_run_against_post_bodies():
    """11-11: 一条说评论的规矩存的 where 是 body + comments, 一周里七十几篇笔记有十几篇因为它收到「建议改」。"""
    c = desk_client(memories=[_rule(1, "forbidden_word", {"words": ["几折"], "where": ["body", "comments"]}, **STORED)])
    out = core.commit_drafts(c, PROJ, _with_word("打几折"), user_id=ME)
    assert "rule_checks" not in out and "rule_checks_unchecked" not in out
    assert c.rows["memories"][0]["rule_payload"]["where"] == ["body", "comments"], "只在读的时候算, 不改库"


def test_it_still_runs_against_the_comments_delivered_with_a_post():
    rule = _rule(1, "forbidden_word", {"words": ["几折"], "where": ["body", "comments"]}, **STORED)
    drafts = [{"title": "甲", "body": "正文里写了打几折", "comments": ["评论里也说打几折"]}]
    (hit,) = rulecheck.check([core._comment_check_rule(rule)], drafts)[0]
    assert hit["where"] == "comments" and "评论里有禁用词「几折」" in hit["what"]
    assert len(rulecheck.check([rule], drafts)[0]) == 2, "不收窄的话正文也报"


@pytest.mark.parametrize("content", ["评论和正文里都不要说打几折", "正文别写打几折，评论也一样", "不能说打几折"])
def test_stored_rules_that_cover_the_post_are_still_run_against_it(content):
    c = desk_client(memories=[_rule(1, "forbidden_word", {"words": ["几折"], "where": ["body", "comments"]},
                                    content=content, severity="soft")])
    out = core.commit_drafts(c, PROJ, _with_word("打几折"), user_id=ME)
    assert [(h["index"], h["where"]) for h in out["rule_checks"]] == [(0, "body")]


def test_my_rules_shows_the_where_that_is_actually_used():
    rows = [_rule(1, "forbidden_word", {"words": ["几折"], "where": ["body", "comments"]}, **STORED),
            _rule(2, "forbidden_word", {"words": ["几折"], "where": ["body", "comments"]}, content="评论和正文里都不要说打几折"),
            _rule(3, "forbidden_word", {"words": ["几折"], "where": ["comments"]}, content="评论区：不说打几折"),
            _rule(4, content="评论不报参数")]
    got = {r["memory_id"]: r for r in core.my_rules(desk_client(memories=rows), PROJ, user_id=ME)["rules"]}
    assert got["m-1"]["check"] == {"kind": "forbidden_word", "words": ["几折"], "where": ["comments"]}
    assert got["m-1"]["check_note"] == core.MY_RULES_COMMENT_CHECK_NOTE
    assert got["m-2"]["check"]["where"] == ["body", "comments"] and "check_note" not in got["m-2"]
    assert got["m-3"]["check"]["where"] == ["comments"] and "check_note" not in got["m-3"], "库里本来就只查评论"
    assert "check" not in got["m-4"] and "check_note" not in got["m-4"]
    assert "``check_note``" in tools.my_rules.__doc__


def test_narrowing_never_touches_the_row_it_was_given():
    rule = _rule(1, "forbidden_word", {"words": ["几折"], "where": ["body", "comments"]}, **STORED)
    before = {**rule, "rule_payload": dict(rule["rule_payload"])}
    assert core._comment_check_rule(rule)["rule_payload"] == {"words": ["几折"], "where": ["comments"]}
    assert rule == before
    plain = _rule(2, "forbidden_word", {"words": ["几折"]}, content="不能说打几折")
    assert core._comment_check_rule(plain) is plain
    broken = _rule(3, "forbidden_word", {"words": []}, **STORED)
    assert core._comment_check_rule(broken) is broken, "坏的检查原样回, 照旧报成没查"


# ── 5. next_step ────────────────────────────────────────────────────────────────────────────────

OLD_SLOT_STEP = ("按 slots 每位写一条(贴主答疑那条回 q1), 守 hard_rules、comment_rules 和 style(所有项目都管), 别和 "
                 "existing_comments 里已经有的问题重复; 写完把整组交给 score_comments(帖子参数和这次一样: link, 或 "
                 "post_title / post_body + project_id; 每条评论带 slot 的 id)。")


def test_a_counted_set_is_told_slots_are_kinds_not_a_count():
    step = core.comments_for(_client(), user_id=ME, link=LINK)["next_step"]
    assert step.startswith(
        "slots 是四种评论(q1 / q2 提问、exp 经验或推荐、reply 贴主回 q1), 不是只写四条: 一组不止四条时, 每个 slot 的 id "
        "整组最多用一次, 其余的评论不带 slot; 守 hard_rules、comment_rules 和 style(所有项目都管), 别和 existing_comments "
        "里已经有的问题重复; 写完把整组放在一次 score_comments 里交(不按 slot 拆成几次; 一次最多 20 条, 超过的按顺序分段判、"
        "前面判过的放进 earlier_comments; 帖子参数和这次一样: link, 或 post_title / post_body + project_id)。"
        + core.COMMENTS_FOR_RULES_NOW + core.COMMENT_SET_NEXT_STEP)
    assert "每位写一条" not in step and "每条评论带 slot 的 id" not in step


@pytest.mark.parametrize("cfg", [None, {"column": COL}, {"brand_max": 1.0, "neutral_min": 0, "short_min": 0}])
def test_projects_without_counted_numbers_keep_the_old_sentence(cfg):
    step = core.comments_for(_client(cfg=cfg), user_id=ME, link=LINK)["next_step"]
    assert step.startswith(OLD_SLOT_STEP + core.COMMENTS_FOR_RULES_NOW)
    assert "不是只写四条" not in step


def test_the_tool_text_says_the_same_about_slots():
    brief, score = tools.comments_for.__doc__, tools.score_comments.__doc__
    assert "每个 id 整组最多用一次" in brief and "其余的评论不带 slot" in brief
    assert "一组不止四条时其余的不带 slot" in score and "不用分几次判" in score


# ── 6. 两种 slot 报错(真实的工具路径) ───────────────────────────────────────────────────────────

IDS = ["q1", "q2", "exp", "reply"]
TWENTY = BRANDED[:10] + PLAIN + ["我是看了三遍回放才搞懂排期的", "做设计的转产品难不难"]


def _tool(monkeypatch, c):
    monkeypatch.setattr(core, "sb", lambda: c)


def test_twenty_comments_cycling_four_slot_ids_is_refused_with_the_way_out(wired, monkeypatch):
    """改之前这里回的是「想多写几条同类的, 分几次判, 或者给它们不同的 id」: 分几次判整组的条数数不准, 换 id 又被下一道拒。"""
    calls, _ = wired
    _tool(monkeypatch, _client())
    with pytest.raises(tools.InvalidInput) as e:
        tools.score_comments([{"slot": IDS[i % 4], "text": t} for i, t in enumerate(TWENTY)], link=LINK, _user_id=ME)
    assert str(e.value) == ("同一个 slot id 一次只能用在一条评论上(q1 / q2 / exp / reply 重复了): 每个 id 留一条, "
                            "其余几条去掉 slot, 整组再送一次(不用分几次判, 也不用另编 id)")
    assert "分几次判, 或者" not in str(e.value) and "不同的 id" not in str(e.value)
    assert calls == []


def test_only_the_repeated_ids_are_named(wired, monkeypatch):
    _tool(monkeypatch, _client())
    with pytest.raises(tools.InvalidInput, match=r"\(exp 重复了\)"):
        tools.score_comments([{"slot": "q1", "text": "甲"}, {"slot": "exp", "text": "乙"}, {"slot": "exp", "text": "丙"}],
                             link=LINK, _user_id=ME)


def test_a_made_up_slot_id_is_refused_with_the_way_out(wired, monkeypatch):
    """改之前只说「自定义评论位要传整个对象」: 第五条起编 q5、q6 的模型拿不到「不带 slot 就行」这句。"""
    calls, _ = wired
    _tool(monkeypatch, _client())
    with pytest.raises(tools.InvalidInput) as e:
        tools.score_comments([{"slot": IDS[i] if i < 4 else f"q{i + 1}", "text": t} for i, t in enumerate(TWENTY)],
                             link=LINK, _user_id=ME)
    assert str(e.value) == ("第 5 条评论的 slot 'q5' 不是 comments_for 给的评论位 id(q1 / q2 / exp / reply); 不属于这几种的"
                            "评论不带 slot 就行: 整组里 slot 不是这几个 id 的都去掉 slot、再送一次(自定义评论位要传整个对象, "
                            "而且要带 id)")
    assert "去掉它的 slot" not in str(e.value), "2026-11-12 复审 C5: 只说「它」的话照字面做, 二十条里编了十六个 id 要被拒十六次"
    assert calls == []


def test_doing_what_the_errors_say_goes_through_in_one_call(wired, monkeypatch):
    calls, reply = wired
    reply["fn"] = _judge(_acts(TWENTY))
    _tool(monkeypatch, _client())
    out = tools.score_comments([{"slot": IDS[i], "text": t} if i < 4 else {"text": t} for i, t in enumerate(TWENTY)],
                               link=LINK, _user_id=ME)
    assert out["judge_status"] == "ok" and out["summary"]["comments"] == 20
    sent = _sent(calls)["comments"]
    assert [c.get("slot", {}).get("id") for c in sent] == IDS + [None] * 16, "一次调用, 四条带位、十六条不带"


# ── 7. 整组参考 ─────────────────────────────────────────────────────────────────────────────────

def test_whole_set_question_ids_become_plain_names_marked_as_reference():
    line = core.THREAD_HINT_PREFIX + "praise_share、same_template、has_friction、thread_arranged"
    assert core._thread_lines([{"slot": None, "why": line}]) == [
        "整组参考(只是参考, 不用为它重写): 夸品牌的评论占了多数；夸品牌的几条是同一个句式；"
        "没有质疑、追问价格或效果这类审慎的声音；整体读着像安排出来的"]
    # 2026-11-12 复审 C7: 原来用顿号连, 「没有质疑、追问价格或效果…」自己带着顿号, 读成三样、第三样成了「有人在追问价格」
    assert core._thread_lines([{"slot": None, "why": core.THREAD_HINT_PREFIX + "praise_share、has_friction"}]) == [
        core.THREAD_HINT_HEAD + "夸品牌的评论占了多数；没有质疑、追问价格或效果这类审慎的声音"]
    assert core._thread_lines([{"slot": None, "why": core.THREAD_HINT_PREFIX + "praise_share、new_question"}]) == [
        core.THREAD_HINT_HEAD + "夸品牌的评论占了多数；new_question"], "不认识的 id 原样留着"
    for name in core.THREAD_HINT_NAMES.values():
        assert "帖子" not in name and not any(w in name for w in ("改成", "要", "必须", "应该")), "只说看到了什么, 不说怎么改"


def test_other_whole_set_lines_are_untouched():
    entries = [{"slot": "q1", "why": "评论区没有摩擦：这一位改成审慎的追问"},
               {"slot": "q2", "why": core.THREAD_HINT_PREFIX + "praise_share"},
               {"slot": None, "why": "别和已有的评论重复"}, {"slot": None, "why": core.THREAD_HINT_PREFIX}]
    assert core._thread_lines(entries) == ["位 q1: 评论区没有摩擦：这一位改成审慎的追问",
                                           "位 q2: " + core.THREAD_HINT_PREFIX + "praise_share",
                                           "别和已有的评论重复", core.THREAD_HINT_PREFIX]


def test_score_comments_hands_back_the_plain_hint(wired):
    """判定服务那边的原话(tests/test_deskcore_score_and_comments.py 的 THREAD_OK)带着题目的 id。"""
    _, reply = wired
    reply["fn"] = lambda m, u, kw: _Resp(200, dict(THREAD_OK))
    out = core.score_comments(_client(cfg=None), user_id=ME, link=LINK,
                              comments=[{"slot": "q1", "text": "第一行写的什么呀"}, {"text": "蹲一个后续"}])
    assert out["thread_hints"] == ["整组参考(只是参考, 不用为它重写): 夸品牌的评论占了多数"]
    assert "praise_share" not in str(out["thread_hints"])
