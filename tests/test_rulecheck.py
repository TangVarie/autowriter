"""能用代码查的规则, 用代码查(2026-10-20)。

``memories.rule_kind`` / ``rule_payload`` 早就在库里(CHECK 只认 forbidden_word / required_phrase / max_len /
forbidden_regex / free_text), 只是以前只有退役的 validator.py 在读。这里钉的是:

  · record_rule 的 check 先校验再写, 写错整条不落库(工具层是 400 不是 500);
  · 存档时只查和简报同一份规则里带 check 的那几条 —— 静音的、降回试用的、别人的 global 都不查;
  · 命中只是参考: 有命中才带 rule_checks, 必须改 / 建议改分得清, 次数进 batch_metrics。

所有示例词和正文都是合成的。
"""

from __future__ import annotations

import json
import time

import pytest

import validator
from deskcore import core, delivery, rulecheck, store, tools
from tests.fakes import FakeClient
from tests.test_delivery import ME, PROJ, desk_client, distinct_drafts

OTHER = "99999999-9999-9999-9999-999999999999"


def _rule(i, kind=None, payload=None, **kw):
    row = {"id": f"m-{i}", "content": f"示例规则 {i}", "severity": "hard", "scope": "project", "project_id": PROJ,
           "user_id": ME, "status": "confirmed", "memory_type": "rule", "muted_until": None,
           "rule_kind": kind, "rule_payload": payload, "created_at": f"2026-10-{i:02d}T00:00:00+00:00",
           "frequency": 1, "applicability": None, "embedding": None}
    row.update(kw)
    return row


# ══════════════════════════════════════════════════════════════════════
# check 的形状
# ══════════════════════════════════════════════════════════════════════

def test_each_kind_normalises_with_defaults():
    assert rulecheck.normalize_check({"kind": "forbidden_word", "words": [" 甲 ", "甲", "乙"]}) == \
        ("forbidden_word", {"words": ["甲", "乙"], "where": ["title", "body"]})
    assert rulecheck.normalize_check({"kind": "required_phrase", "any": ["话术"], "where": ["comments", "body"]}) == \
        ("required_phrase", {"any": ["话术"], "where": ["body", "comments"]})
    assert rulecheck.normalize_check({"kind": "max_len", "min": 150}) == ("max_len", {"where": "body", "min": 150})
    assert rulecheck.normalize_check({"kind": "forbidden_regex", "pattern": r"\d{3,}", "where": "title"}) == \
        ("forbidden_regex", {"pattern": r"\d{3,}", "where": ["title"]})
    assert rulecheck.normalize_check({"kind": "free_text"}) == ("free_text", {"code_check": "off"})


@pytest.mark.parametrize("bad, words", [
    ({"kind": "style"}, "check.kind"),
    ("禁用词", "JSON 对象"),
    ({"kind": "forbidden_word", "words": []}, "非空"),
    ({"kind": "forbidden_word", "words": ["甲"], "scope": "title"}, "不认识的键"),
    ({"kind": "forbidden_word", "words": ["甲"], "where": ["footer"]}, "where"),
    ({"kind": "forbidden_word", "words": ["这是一整句话而不是一个词" * 5]}, "太长"),
    ({"kind": "forbidden_regex", "pattern": "(a+)+$"}, "灾难性回溯"),
    ({"kind": "forbidden_regex", "pattern": "("}, "无法编译"),
    ({"kind": "forbidden_regex", "pattern": "(哈|哈哈)+$"}, "灾难性回溯"),
    ({"kind": "forbidden_regex", "pattern": "(ab?)+$"}, "灾难性回溯"),
    ({"kind": "forbidden_regex", "pattern": "(?:x|yz){2,9}"}, "灾难性回溯"),
    ({"kind": "forbidden_regex", "pattern": "a*a*a*a*a*a*a*a*b"}, "挨在一起"),
    ({"kind": "forbidden_regex", "pattern": ".*.*x"}, "挨在一起"),
    ({"kind": "max_len"}, "至少给"),
    ({"kind": "max_len", "max": 10, "min": 20}, "不能比 max 大"),
    ({"kind": "max_len", "max": True}, "正整数"),
    ({"kind": "max_len", "max": 10, "where": "tags"}, "title 或 body"),
    ({"kind": "free_text", "words": ["甲"]}, "不带别的键"),
])
def test_bad_checks_are_rejected_with_a_reason(bad, words):
    with pytest.raises(rulecheck.CheckError) as exc:
        rulecheck.normalize_check(bad)
    assert words in str(exc.value)


@pytest.mark.parametrize("pattern", [r"哈{5,}", r"[!！]{3,}", r"(?:哈哈)+", r"(哈哈)+", r"\d+%", r"(最|第一|顶级)",
                                     r"(?:啊|呀)+", r".{0,5}私信"])
def test_ordinary_patterns_still_pass_the_shape_check(pattern):
    assert rulecheck.normalize_check({"kind": "forbidden_regex", "pattern": pattern})[1]["pattern"] == pattern


@pytest.mark.parametrize("pattern", [r"[!！][\s\S]*[!！][\s\S]*[!！][\s\S]*[!！]", r"最[\s\S]*最[\s\S]*最", r"\n\s*\n\s*\n",
                                     r"\d+\s*\d+", r"原价.*\d+"])
def test_counting_a_single_character_is_recordable(pattern):
    """「感叹号最多 3 个」照 Jev 规则编译器教的展开写(X[\\s\\S]*X…X), X 是单个字也要存得进、读得出(codex review on aw#115):
    这种写法只会因为 X 不够数而失败, 回溯被 X 的个数封住。以前 validator 把单个字当成隔不开, 整条规则连原话一起存不进。"""
    assert rulecheck.normalize_check({"kind": "forbidden_regex", "pattern": pattern, "where": ["body"]}) == \
        ("forbidden_regex", {"pattern": pattern, "where": ["body"]})
    row = _rule(1, "forbidden_regex", {"pattern": pattern, "where": ["body"]})
    assert rulecheck.spec_of(row) is not None


def test_an_ambiguous_quantified_alternation_is_refused_on_read_too():
    """validator.assert_regex_is_safe 放行 (哈|哈哈)+$: 30 个「哈」加一个句号就回溯近半秒、每多一个字乘 1.6, 而 re 匹配时
    握着 GIL —— 共享的服务进程整个停住。库里要是已经有这种形状(手写进去的), 读出来也跳过, 不跑。"""
    assert validator.regex_is_safe("(哈|哈哈)+$"), "前提: validator 那道闸确实抓不到这一类"
    assert rulecheck.spec_of(_rule(1, "forbidden_regex", {"pattern": "(哈|哈哈)+$", "where": ["body"]})) is None
    t0 = time.monotonic()
    hits, stats = rulecheck.check([_rule(1, "forbidden_regex", {"pattern": "(哈|哈哈)+$"})],
                                  [{"title": "", "body": "哈" * 40 + "。"}])
    assert hits == [] and stats == {"rules": 0, "skipped": 1}
    assert time.monotonic() - t0 < 1


def test_adjacent_overlapping_repeats_are_refused_on_read_too():
    """a*a*a*a*a*a*a*a*b 两道老闸都放行(没嵌套、没分支), 30 个 a 就回溯一秒多(codex review on aw#115)。库里要是已经有
    (手写进去的), 读出来也跳过, 不跑。"""
    row = _rule(1, "forbidden_regex", {"pattern": "a*a*a*a*a*a*a*a*b", "where": ["body"]})
    assert rulecheck.spec_of(row) is None
    t0 = time.monotonic()
    assert rulecheck.check([row], [{"title": "", "body": "a" * 40}]) == ([], {"rules": 0, "skipped": 1})
    assert time.monotonic() - t0 < 0.5


def test_validator_honours_where_on_a_desk_regex():
    """AW 生成链路(generation_service → validator.check_hard_rules)也读这两列: 只管评论的正则不能被它套到标题正文上。"""
    def hits(where):
        rule = {"content": "评论区：别引导私信", "rule_kind": "forbidden_regex",
                "rule_payload": {"pattern": "私信", **({"where": where} if where is not None else {})}}
        return validator.check_hard_rules([rule], "标题有私信", "正文也有私信")
    assert hits(["comments"]) == [] and hits(["tags", "comments"]) == []
    assert [h["match"] for h in hits(["title"])] == ["私信"]
    assert len(hits(None)) == 1 and len(hits(["title", "body"])) == 1
    only_body = {"content": "x", "rule_kind": "forbidden_regex", "rule_payload": {"pattern": "^标题", "where": ["body"]}}
    assert validator.check_hard_rules([only_body], "标题有私信", "正文") == []


def test_validator_never_reads_an_out_of_scope_desk_rule_as_text():
    """只管评论 / 话题的规则, validator 那边看不到那几处, 也不许退回从 content 里抽词(codex review on aw#115)。

    退回去的话「评论不要出现「私信」」会被抽成"标题正文不许出现私信", 生成的稿子一律标 needs_revision。"""
    def hits(kind, payload, content, title="标题有私信", body="正文也有私信"):
        return validator.check_hard_rules([{"content": content, "rule_kind": kind, "rule_payload": payload}],
                                          title, body)
    assert hits("forbidden_regex", {"pattern": "私信", "where": ["comments"]}, "评论不要出现「私信」") == []
    assert hits("forbidden_word", {"words": ["私信"], "where": ["tags", "comments"]}, "评论不要出现「私信」") == []
    assert hits("required_phrase", {"any": ["示例口播"], "where": ["comments"]}, "评论必须包含「示例口播」") == []
    # "正文或评论里有一个就行": 这里看不到评论, 正文里没有也判不了没守
    assert hits("required_phrase", {"any": ["示例口播"], "where": ["body", "comments"]}, "必须包含「示例口播」") == []
    # 只管标题的禁用词: 正文里有不算; 只管正文的字数下限: content 里没写数也照查
    assert hits("forbidden_word", {"words": ["私信"], "where": ["title"]}, "标题不要出现「私信」",
                title="标题", body="正文也有私信") == []
    assert [h["kind"] for h in hits("forbidden_word", {"words": ["私信"], "where": ["title"]},
                                    "标题不要出现「私信」")] == ["forbidden_word"]
    assert [h["kind"] for h in hits("max_len", {"min": 50, "where": "body"}, "正文写长一点")] == ["max_len"]
    # check 坏了(写手手改的库)也不退回文本抽取: 写作台那边同样是跳过
    assert hits("forbidden_word", {"words": [], "where": ["body"]}, "不要出现「私信」") == []


def test_a_withdrawn_check_is_not_read_back_from_the_text_by_the_generation_path(rule_client):
    """写作台撤掉代码检查(check={"kind": "free_text"})之后, AW 生成链路也不许从原文把检查抽回来(codex review on aw#115):
    「禁止出现「私信」」原来撤掉了照样被 _parse_rule 抽成禁用词。AW 录规则界面写的 free_text(不带标记)照旧从原文抽。"""
    core.record_rule(rule_client, PROJ, "禁止出现「私信」", severity="hard", user_id=ME,
                     check={"kind": "forbidden_word", "words": ["私信"]})
    core.record_rule(rule_client, PROJ, "禁止出现「私信」", severity="hard", user_id=ME, check={"kind": "free_text"})
    (row,) = rule_client.rows["memories"]
    assert validator.check_hard_rules([row], "标题", "正文有私信") == []
    assert rulecheck.spec_of(row) is None
    plain = {"content": "禁止出现「私信」", "rule_kind": "free_text", "rule_payload": None}
    assert [h["kind"] for h in validator.check_hard_rules([plain], "标题", "正文有私信")] == ["forbidden_word"]


# 两边同一行规则、同一篇稿子(只看标题和正文)必须给出同样的结论 —— 合成的示例
_PARITY_DRAFT = {"title": "Alpha 标题", "body": "禁止开头一句。\n正文提到甲 #话题乙\nmade 结尾禁止"}


@pytest.mark.parametrize("kind, payload", [
    ("forbidden_word", {"words": ["alpha"]}),
    ("forbidden_word", {"words": ["甲"], "where": ["title"]}),
    ("forbidden_word", {"words": ["ad", "话题乙"], "where": ["body"]}),
    ("required_phrase", {"any": ["不存在的话术"], "where": ["body"]}),
    ("required_phrase", {"any": ["ALPHA"], "where": ["title"]}),
    ("max_len", {"max": 5, "where": "title"}),
    ("max_len", {"max": 20}),
    ("max_len", {"min": 100}),
    ("forbidden_regex", {"pattern": "^禁止", "where": ["body"]}),
    ("forbidden_regex", {"pattern": "标题$"}),
    ("forbidden_regex", {"pattern": "禁止$", "where": ["title"]}),
    ("forbidden_regex", {"pattern": "^Alpha", "where": ["title", "body"]}),
])
def test_validator_and_rulecheck_agree_on_title_and_body(kind, payload):
    """同一行库里的规则, AW 生成链路(validator)和写作台(rulecheck)查标题正文的结论不能不一样(codex review on aw#115)。"""
    desk = bool(rulecheck.check([_rule(1, kind, payload)], [_PARITY_DRAFT])[0])
    aw = bool(validator.check_hard_rules([{"content": "示例规则", "rule_kind": kind, "rule_payload": payload}],
                                         _PARITY_DRAFT["title"], _PARITY_DRAFT["body"]))
    assert aw == desk, (kind, payload, desk)


def test_validator_searches_each_field_separately_for_anchored_patterns():
    """拼成"标题\n正文"再搜的话 ^ 永远碰不到正文开头、$ 永远碰不到标题结尾(codex review on aw#115)。"""
    def hits(pattern, where=None):
        payload = {"pattern": pattern, **({"where": where} if where is not None else {})}
        return validator.check_hard_rules([{"content": "x", "rule_kind": "forbidden_regex", "rule_payload": payload}],
                                          "标题以禁止结尾禁止", "禁止开头的正文")
    assert [h["match"] for h in hits("^禁止", ["title", "body"])] == ["禁止"]
    assert [h["match"] for h in hits("禁止$", ["title", "body"])] == ["禁止"]
    assert len(hits("^禁止")) == 1 and len(hits("禁止$")) == 1, "老形状(没有 where)也逐个部位查, 同写作台"
    assert hits("^禁止", ["title"]) == [] and hits("禁止$", ["body"]) == []


def test_validator_era_payloads_still_read():
    """validator.py(Day 3)存的老形状: 生产库里一条也没有, 但读的时候照样认, 别让哪天灌进来的老数据静默失效。"""
    assert rulecheck.spec_of(_rule(1, "forbidden_word", {"target": "甲"}))["words"] == ["甲"]
    assert rulecheck.spec_of(_rule(1, "required_phrase", {"target": "话术"}))["any"] == ["话术"]
    assert rulecheck.spec_of(_rule(1, "max_len", {"scope": "标题", "n": 20})) == \
        {"kind": "max_len", "where": "title", "max": 20}
    assert rulecheck.spec_of(_rule(1, "max_len", '{"scope": "正文", "n": 300}'))["where"] == "body"


@pytest.mark.parametrize("kind, payload", [
    ("forbidden_word", None), ("forbidden_word", "{broken"), ("max_len", {"scope": "开头", "n": 5}),
    ("forbidden_regex", {"pattern": "(a*)*"}), ("free_text", None), (None, {"words": ["甲"]}),
])
def test_unusable_checks_are_skipped_not_fatal(kind, payload):
    assert rulecheck.spec_of(_rule(1, kind, payload)) is None


# ══════════════════════════════════════════════════════════════════════
# 查
# ══════════════════════════════════════════════════════════════════════

DRAFT = {"title": "标题里有 Alpha", "body": "正文第一句。\n正文第二句提到甲。\n#话题乙 #话题丙",
         "tags": ["话题丁"], "comments": [{"role": "reader", "text": "评论里有乙"}]}


def test_forbidden_words_are_found_per_part_case_insensitively():
    rules = [_rule(1, "forbidden_word", {"words": ["alpha", "甲", "不存在"]}),
             _rule(2, "forbidden_word", {"words": ["乙"], "where": ["tags", "comments"]}, severity="soft")]
    hits, stats = rulecheck.check(rules, [DRAFT], indices=[3])
    assert stats == {"rules": 2, "skipped": 0}
    got = [(h["index"], h["rule_id"], h["where"], h["what"]) for h in hits]
    assert got == [(3, "m-1", "title", "必须改: 标题里有禁用词「alpha」"),
                   (3, "m-1", "body", "必须改: 正文里有禁用词「甲」"),
                   (3, "m-2", "tags", "建议改: 话题标签里有禁用词「乙」"),
                   (3, "m-2", "comments", "建议改: 评论里有禁用词「乙」")]


def test_a_single_comment_not_wrapped_in_a_list_is_still_checked():
    """交付稿把一条字符串 / 一条 dict 的 comments 也拼进去了(codex review on aw#115), 查规则也得看得见它。"""
    rule = _rule(1, "forbidden_word", {"words": ["私信"], "where": ["comments"]})
    for comments in ("想要的私信我", {"role": "reader", "text": "想要的私信我"}):
        hits = rulecheck.check([rule], [{"title": "t", "body": "b", "comments": comments}])[0]
        assert [h["where"] for h in hits] == ["comments"], comments


def test_comments_as_a_json_string_and_url_fragments_are_seen_as_delivered():
    """同交付稿(codex review on aw#115): 一串 JSON 的 comments 拆开查; 网址里的 #片段 不算话题(没有交付模板时也一样)。"""
    rule = _rule(1, "forbidden_word", {"words": ["私信"], "where": ["comments"]})
    comments = json.dumps([{"role": "reader", "text": "示例评论"}, {"role": "author", "text": "想要的私信我"}],
                          ensure_ascii=False)
    hits = rulecheck.check([rule], [{"title": "t", "body": "b", "comments": comments}])[0]
    assert [h["what"] for h in hits] == ["必须改: 评论里有禁用词「私信」"]
    ascii_tag = _rule(2, "forbidden_regex", {"pattern": "[A-Za-z]", "where": ["tags"]})
    assert rulecheck.check([ascii_tag], [{"title": "t", "body": "详情见 https://example.com/#faq #秋招"}])[0] == []


@pytest.mark.parametrize("comments", [
    [{"role": "author", "content": "想要的私信我"}],
    [{"role": "reader", "text": ["想要的私信我"]}, {"role": "reader", "text": 12}, {"text": {"示例": "私信"}}],
    json.dumps([{"role": "reader", "text": ["想要的私信我"]}, {"role": "author", "text": "示例回复"}], ensure_ascii=False),
    ["示例评论", {"role": "author", "text": " 想要的私信我 "}, {"role": "boss", "text": "示例三"}],
    {"role": "author", "text": "想要的私信我"},
    json.dumps({"role": "author", "content": "想要的私信我"}, ensure_ascii=False),
    json.dumps([{"role": "author", "text": "示例回复"}, None], ensure_ascii=False),
], ids=["no-text", "text-not-a-string", "json-string", "mixed-list", "single-dict", "json-dict-no-text", "json-list-with-null"])
def test_rules_check_exactly_the_comments_that_are_delivered(comments):
    """查评论和拼交付稿走同一处(delivery.comment_list, codex review on aw#115): text 是列表的原来交付稿里拼成 Python 的
    ['…'] 而规则看不见; 没有 text 的两边都不算(交付那边报一条形状不对)。序列化成一串 JSON 的同样按解出来的对象 / 列表认,
    原样的 JSON 既不进交付稿、也不拿去查。"""
    tpl = {"layout": "blocks", "comments": {"min": 0, "max": 9, "label_reader": "读者：", "label_author": "作者："}}
    text = delivery.assemble(tpl, [{"title": "t", "body": "b", "comments": comments}])["text"]
    delivered = [line.split("：", 1)[1] for line in text.split("\n")[2:]]
    assert not any(c.startswith(("{", "[")) for c in delivered), delivered
    assert rulecheck._parts({"comments": comments})["comments"] == delivered
    rule = _rule(1, "forbidden_word", {"words": ["私信"], "where": ["comments"]})
    hits = rulecheck.check([rule], [{"title": "t", "body": "b", "comments": comments}])[0]
    assert bool(hits) == any("私信" in c for c in delivered)


def test_tags_as_a_json_string_are_checked_as_delivered():
    """tags 是整个列表 json.dumps 出来的一串(codex review on aw#115): 交付稿拆开拼, 查话题的规则也拆开查, 不会看见 [ " , 。"""
    tags = json.dumps(["话题甲", "话题乙"], ensure_ascii=False)
    assert rulecheck._parts({"tags": tags})["tags"] == ["话题甲 话题乙"]
    punct = _rule(1, "forbidden_regex", {"pattern": "[\\[\\]\",]", "where": ["tags"]})
    assert rulecheck.check([punct], [{"title": "t", "body": "b", "tags": tags}])[0] == []


def test_url_fragments_and_language_tokens_count_as_characters():
    """数字数只扣真话题(codex review on aw#115): 「详情见 https://example.com/#faq」原来把 #faq 当话题扣掉, 交付出去它还在网址
    里 —— 上限放过超长的稿, 下限冤枉够长的稿。写作台(rulecheck)和 AW 生成链路(validator)数的是同一个数。"""
    body = "详情见 https://example.com/#faq"
    n = len(body.replace(" ", ""))
    assert validator.count_chars(body) == n
    assert validator.count_chars("今天开始学习C#入门 #编程") == len("今天开始学习C#入门")
    assert validator.count_chars("正文。#秋招[话题]#求职[话题]#") == len("正文。"), "连写的 [话题] 两个都是话题"
    over, enough = {"max": n - 1, "where": "body"}, {"min": n, "where": "body"}
    hits = rulecheck.check([_rule(1, "max_len", over)], [{"title": "t", "body": body}])[0]
    assert [h["where"] for h in hits] == ["body"]
    assert rulecheck.check([_rule(2, "max_len", enough)], [{"title": "t", "body": body}])[0] == []

    def aw(payload):
        return validator.check_hard_rules([{"content": "示例规则", "rule_kind": "max_len", "rule_payload": payload}],
                                          "t", body)
    assert [h["kind"] for h in aw(over)] == ["max_len"] and aw(enough) == []


def test_a_hash_after_an_ascii_letter_is_not_a_tag():
    """「学习C#入门」里的 #入门 不是话题(codex review on aw#115): 摘末尾话题时前一个字是英文字母数字的 # 本来就不摘, 正文里找
    话题却认它 —— 只管话题的禁用词会在一句普通的 C# 上报「必须改」。真话题照查。"""
    rule = _rule(1, "forbidden_word", {"words": ["入门"], "where": ["tags"]})
    body = "今天开始学习C#入门"
    assert rulecheck.check([rule], [{"title": "t", "body": body}])[0] == []
    assert [h["where"] for h in rulecheck.check([rule], [{"title": "t", "body": body + " #入门"}])[0]] == ["tags"]
    # 同一段前面有个 "#秋招[话题]#": 它收尾的那个 # 不开一串, C#入门 照样不是话题(codex review on aw#115)。中文句子里没有
    # 空白, 原来往回找最近的 # 一路找到它, 把 "#入门的经验" 当成接在话题后面的连写
    for inline in ("上岸了#秋招[话题]#，分享一下我学C#入门的经验", "#秋招[话题]#今天学C#入门"):
        assert rulecheck.check([rule], [{"title": "t", "body": inline}])[0] == [], inline


def test_a_huati_topic_after_an_ascii_letter_is_still_a_tag():
    """"拿到offer#秋招[话题]#": 带 [话题] 的是小红书的话题写法, 不会是 "C#入门" 这种词的一部分, 前一个字是英文字母数字也算话题
    (codex review on aw#115)。不算的话查话题的规则看不见它(要求带「秋招」的报没带), 字数还把它的 8 个字算进正文。"""
    body = "拿到offer#秋招[话题]#"
    need = _rule(1, "required_phrase", {"any": ["秋招"], "where": ["tags"]})
    assert rulecheck.check([need], [{"title": "t", "body": body, "tags": ["求职"]}])[0] == []
    assert validator.count_chars(body) == len("拿到offer")
    assert rulecheck.check([_rule(2, "max_len", {"max": len("拿到offer"), "where": "body"})],
                           [{"title": "t", "body": body}])[0] == []


def test_ascii_words_match_on_word_boundaries():
    rules = [_rule(1, "forbidden_word", {"words": ["ad"]})]
    assert rulecheck.check(rules, [{"title": "made in", "body": "reading"}])[0] == []
    assert len(rulecheck.check(rules, [{"title": "an AD here", "body": ""}])[0]) == 1


def test_required_phrase_hits_only_when_none_is_present():
    rule = _rule(1, "required_phrase", {"any": ["话术一", "话术二"], "where": ["body"]})
    assert rulecheck.check([rule], [{"title": "", "body": "带了话术二"}])[0] == []
    hits = rulecheck.check([rule], [{"title": "", "body": "什么都没带"}])[0]
    assert [h["what"] for h in hits] == ["必须改: 正文里没有「话术一」「话术二」中的任何一个"]
    on_comments = _rule(2, "required_phrase", {"any": ["话术一"], "where": ["comments"]})
    assert rulecheck.check([on_comments], [{"title": "t", "body": "b"}])[0] == [], "没写评论就不查评论"


def test_length_is_counted_without_whitespace_and_tags():
    rule = _rule(1, "max_len", {"min": 10, "max": 12, "where": "body"}, severity="soft")
    ok = {"title": "", "body": "一二三四五 六七八九十\n#话题一 #话题二"}
    assert rulecheck.check([rule], [ok])[0] == []
    short = rulecheck.check([rule], [{"title": "", "body": "一二三 #很长很长的话题"}])[0]
    assert [h["what"] for h in short] == ["建议改: 正文 3 字(不算空白和话题), 下限 10"]
    long = rulecheck.check([rule], [{"title": "", "body": "一" * 13}])[0]
    assert long[0]["what"].endswith("上限 12")


def test_truncation_keeps_every_hard_hit_before_any_soft_one():
    """按篇号直接截的话, 后面几篇的必须改会被前面几篇的建议改挤掉, 写手看不见, 就当没问题交了。"""
    hits = [{"index": i, "severity": "soft", "rule_id": f"s{k}"} for i in range(15) for k in range(3)]
    hits.append({"index": 14, "severity": "hard", "rule_id": "h"})
    hits.sort(key=lambda h: (h["index"], h["severity"] != "hard"))
    shown, more = rulecheck.top_hits(hits)
    assert len(shown) == rulecheck.MAX_HITS and more == 6
    assert {"index": 14, "severity": "hard", "rule_id": "h"} in shown
    assert [(h["index"], h["severity"]) for h in shown] == sorted(
        [(h["index"], h["severity"]) for h in shown], key=lambda x: (x[0], x[1] != "hard")), "列出来的照旧按篇排"
    assert [h["index"] for h in shown if h["severity"] == "soft"][-1] == 12, "建议改按篇号补到满"


def test_every_hard_hit_is_listed_even_past_the_cap():
    """必须改的一条都不截(codex review on aw#115): rule_checks_more 只是个数, 截掉的必须改写手看不见是哪篇哪条, 就当
    那几篇没问题交出去了。封顶只管建议改。"""
    hits = [{"index": i, "severity": "hard", "rule_id": f"h{k}"} for i in range(20) for k in range(3)]
    hits += [{"index": i, "severity": "soft", "rule_id": "s"} for i in range(20)]
    shown, more = rulecheck.top_hits(hits)
    assert sum(1 for h in shown if h["severity"] == "hard") == 60, "60 条必须改全列"
    assert not [h for h in shown if h["severity"] == "soft"] and more == 20, "没地方了, 建议改只报个数"
    assert [(h["index"], h["rule_id"]) for h in shown][-3:] == [(19, "h0"), (19, "h1"), (19, "h2")]


def test_hard_hits_sort_before_soft_within_a_draft():
    rules = [_rule(1, "forbidden_word", {"words": ["甲"]}, severity="soft"),
             _rule(2, "forbidden_word", {"words": ["甲"]}, severity="hard")]
    hits = rulecheck.check(rules, [{"title": "", "body": "甲"}, {"title": "", "body": "甲"}])[0]
    assert [(h["index"], h["severity"]) for h in hits] == [(0, "hard"), (0, "soft"), (1, "hard"), (1, "soft")]


# ══════════════════════════════════════════════════════════════════════
# record_rule 带 check
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture
def rule_client(monkeypatch):
    import dedup
    monkeypatch.setattr(dedup, "embeddings_available", lambda: False)
    return FakeClient(rows={"memories": [], "projects": [
        {"id": PROJ, "name": "示例项目", "brand": "示例品牌", "owner_id": ME}]})


def test_record_rule_stores_the_check(rule_client):
    out = core.record_rule(rule_client, PROJ, "不要用示例禁词", severity="hard", user_id=ME,
                           check={"kind": "forbidden_word", "words": ["禁词一", "禁词二"]})
    row = rule_client.rows["memories"][0]
    assert row["rule_kind"] == "forbidden_word"
    assert row["rule_payload"] == {"words": ["禁词一", "禁词二"], "where": ["title", "body"]}
    assert out["check"] == {"kind": "forbidden_word", "words": ["禁词一", "禁词二"], "where": ["title", "body"]}


def test_re_recording_an_existing_rule_can_add_change_and_drop_its_check(rule_client):
    """db.upsert_memory 只在新建时写这两列 —— 已有的规则再记一次带 check, 不能被静默丢掉。"""
    core.record_rule(rule_client, PROJ, "正文长度", user_id=ME)
    assert rule_client.rows["memories"][0].get("rule_kind") is None
    core.record_rule(rule_client, PROJ, "正文长度", user_id=ME, check={"kind": "max_len", "min": 200})
    assert len(rule_client.rows["memories"]) == 1
    assert rule_client.rows["memories"][0]["rule_payload"] == {"where": "body", "min": 200}
    out = core.record_rule(rule_client, PROJ, "正文长度", user_id=ME, check={"kind": "free_text"})
    assert rule_client.rows["memories"][0]["rule_kind"] == "free_text" and out["check"] is None
    assert rule_client.rows["memories"][0]["rule_payload"] == rulecheck.WITHDRAWN, "撤掉的要带标记, AW 链路才认得"


def test_a_bad_check_writes_nothing_and_is_a_400(rule_client, monkeypatch):
    with pytest.raises(rulecheck.CheckError):
        core.record_rule(rule_client, PROJ, "示例规则", user_id=ME, check={"kind": "max_len"})
    assert rule_client.rows["memories"] == [], "check 写错了规则却落了库: 用户以为会被代码查, 其实没有"
    monkeypatch.setattr(core, "sb", lambda: rule_client)
    with pytest.raises(tools.InvalidInput):
        tools.record_rule(PROJ, "示例规则", check={"kind": "forbidden_word", "words": []}, _user_id=ME)


def test_old_record_rule_callers_are_unaffected(rule_client):
    out = core.record_rule(rule_client, PROJ, "一条普通规则", user_id=ME)
    assert "check" not in out
    assert not [c for c in rule_client.calls if c["op"] == "update"]


def test_my_rules_shows_which_rules_are_code_checked(rule_client):
    core.record_rule(rule_client, PROJ, "示例禁词", user_id=ME, check={"kind": "forbidden_word", "words": ["甲"]})
    core.record_rule(rule_client, PROJ, "语气轻一点", user_id=ME)
    rows = {r["content"]: r for r in core.my_rules(rule_client, PROJ, user_id=ME)["rules"]}
    assert rows["示例禁词"]["check"]["words"] == ["甲"]
    assert "check" not in rows["语气轻一点"]


# ══════════════════════════════════════════════════════════════════════
# 存档时查: 和简报同一份规则
# ══════════════════════════════════════════════════════════════════════

def _with_word(word: str, n: int = 2) -> list[dict]:
    drafts = distinct_drafts(n)
    drafts[0]["body"] = drafts[0]["body"] + word
    return drafts


def test_score_drafts_reports_hits_and_records_counts():
    c = desk_client(memories=[_rule(1, "forbidden_word", {"words": ["示例禁词"]}),
                              _rule(2, "max_len", {"max": 5, "where": "title"}, severity="soft")])
    out = core.commit_drafts(c, PROJ, _with_word("示例禁词"), user_id=ME)
    got = [(h["index"], h["rule_id"], h["severity"]) for h in out["rule_checks"]]
    assert got == [(0, "m-1", "hard"), (0, "m-2", "soft"), (1, "m-2", "soft")]
    assert out["rule_checks"][0]["what"].startswith("必须改")
    assert "rule_checks" in out["next_step"]
    meta = c.rows["batch_metrics"][0]["meta"]["rule_checks"]
    assert (meta["rules"], meta["hits"], meta["hard"]) == (2, 3, 1)


def test_no_hits_means_no_rule_checks_key():
    c = desk_client(memories=[_rule(1, "forbidden_word", {"words": ["不会出现的词"]}), _rule(2)])
    out = core.commit_drafts(c, PROJ, distinct_drafts(2), user_id=ME)
    assert "rule_checks" not in out
    assert c.rows["batch_metrics"][0]["meta"]["rule_checks"]["rules"] == 1


@pytest.mark.parametrize("row", [
    _rule(1, "forbidden_word", {"words": ["示例禁词"]}, muted_until="2099-01-01T00:00:00+00:00"),
    _rule(1, "forbidden_word", {"words": ["示例禁词"]}, status="candidate"),
    _rule(1, "forbidden_word", {"words": ["示例禁词"]}, scope="global", project_id=None, user_id=OTHER),
    _rule(1, "forbidden_word", {"words": ["示例禁词"]}, memory_type="note"),
], ids=["muted", "retired", "someone-elses-global", "not-a-rule"])
def test_rules_the_brief_would_not_inject_are_not_checked(row):
    out = core.commit_drafts(desk_client(memories=[row]), PROJ, _with_word("示例禁词"), user_id=ME)
    assert "rule_checks" not in out


def test_my_own_global_rule_is_checked():
    row = _rule(1, "forbidden_word", {"words": ["示例禁词"]}, scope="global", project_id=None)
    out = core.commit_drafts(desk_client(memories=[row]), PROJ, _with_word("示例禁词"), user_id=ME)
    assert [h["rule_id"] for h in out["rule_checks"]] == ["m-1"]


def test_retire_and_mute_stop_the_code_check():
    """set_rule_state 不用改: 代码检查读的就是简报那一份(shared_memories), 停用 / 降档之后自然取不到。"""
    c = desk_client(memories=[_rule(1, "forbidden_word", {"words": ["示例禁词"]})])

    def commit(seed: int) -> dict:
        d = distinct_drafts(1, seed=seed)[0]          # 每次换一篇, 免得被自己上一次存的指纹拦下
        return core.commit_drafts(c, PROJ, [{**d, "body": d["body"] + "示例禁词"}], user_id=ME)

    assert "rule_checks" in commit(4)
    core.set_rule_state(c, "m-1", "mute", user_id=ME, days=30)
    assert "rule_checks" not in commit(5)
    core.set_rule_state(c, "m-1", "unmute", user_id=ME)
    assert "rule_checks" in commit(6)
    core.set_rule_state(c, "m-1", "retire", user_id=ME)
    assert "rule_checks" not in commit(7)


def test_a_failing_rule_lookup_never_breaks_scoring_but_says_it_did_not_run(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("规则表查不动")
    monkeypatch.setattr(store, "shared_memories", boom)
    c = desk_client()
    out = core.commit_drafts(c, PROJ, distinct_drafts(1), user_id=ME)
    assert out["written"] == 1 and "rule_checks" not in out
    assert out["rule_checks_status"] == "error", "没查成和查过了没命中长得一样的话, 写手会当整批都查干净了"
    assert "rule_checks_status=error" in out["next_step"]
    assert "规则表查不动" in c.rows["batch_metrics"][0]["meta"]["rule_checks"]["error"]


def test_a_clean_check_carries_no_status_marker():
    out = core.commit_drafts(desk_client(memories=[_rule(1, "forbidden_word", {"words": ["不会出现"]})]), PROJ,
                             distinct_drafts(1), user_id=ME)
    assert "rule_checks" not in out and "rule_checks_status" not in out


def test_soft_rules_for_the_other_direction_are_not_checked():
    """简报的方向闸(filter_by_applicability)挡掉的 soft 规则, my_rules 里是「方向不符」, 这里也不查 —— 不然产品向的
    "带上产品名"会在流量向项目上报「建议改」。hard 不过这道闸, 同简报。"""
    rules = [_rule(1, "required_phrase", {"any": ["示例产品名"]}, severity="soft", applicability="产品向"),
             _rule(2, "required_phrase", {"any": ["示例口播"]}, severity="soft", applicability="流量向"),
             _rule(3, "required_phrase", {"any": ["示例合规话术"]}, severity="hard", applicability="产品向")]
    c = desk_client(memories=rules)
    c.rows["projects"][0]["name"] = "示例项目 流量向"
    out = core.commit_drafts(c, PROJ, distinct_drafts(1), user_id=ME)
    assert sorted(h["rule_id"] for h in out["rule_checks"]) == ["m-2", "m-3"]
    ledger = {r["memory_id"]: r["state"] for r in core.my_rules(c, PROJ, user_id=ME)["rules"]}
    assert ledger["m-1"] == "方向不符"

    undecided = desk_client(memories=rules)            # 项目名判不出方向: 全部放行, 同简报
    out = core.commit_drafts(undecided, PROJ, distinct_drafts(1, seed=5), user_id=ME)
    assert sorted(h["rule_id"] for h in out["rule_checks"]) == ["m-1", "m-2", "m-3"]


def test_the_tactic_from_the_brief_gates_soft_rules_at_scoring_too(monkeypatch):
    """项目名判不出方向时, 简报按 open_project 传的 tactic 判(build_writing_brief); 存档这一步也要拿到同一个 tactic, 不然
    简报里挡掉的那一边的 soft 规则, 存档时又报成「建议改」(codex review on aw#115)。"""
    rules = [_rule(1, "required_phrase", {"any": ["示例产品名"]}, severity="soft", applicability="产品向"),
             _rule(2, "required_phrase", {"any": ["示例口播"]}, severity="soft", applicability="流量向"),
             _rule(3, "required_phrase", {"any": ["示例合规话术"]}, severity="hard", applicability="产品向")]
    c = desk_client(memories=rules)
    assert core.build_writing_brief(c, PROJ, user_id=ME, brief={"tactic": "流量向种草"})["counts"]["direction"] == "流量向"
    out = core.commit_drafts(c, PROJ, distinct_drafts(1), user_id=ME, tactic="流量向种草")
    assert sorted(h["rule_id"] for h in out["rule_checks"]) == ["m-2", "m-3"]

    from deskcore import tools as T                    # 工具层原样往下传, 不传就是空(按项目名判, 同以前)
    seen = {}
    monkeypatch.setattr(core, "sb", lambda: object())
    monkeypatch.setattr(core, "commit_drafts", lambda c, pid, drafts, **kw: seen.update(kw) or {})
    for tool in (T.score_drafts, T.commit_drafts):
        tool(PROJ, [], tactic="流量向种草", _user_id=ME)
        assert seen["tactic"] == "流量向种草"
        tool(PROJ, [], _user_id=ME)
        assert seen["tactic"] == ""
    assert "tactic" in (T.score_drafts.__doc__ or "") and "open_project" in (T.score_drafts.__doc__ or "")


def test_tag_checks_see_the_tags_the_template_adds():
    """固定话题是模板加的: 只看写手传的 tags 的话, delivery.text 末尾明明有「#示例固定」, rule_checks 却说没有。"""
    tpl = {"layout": "blocks", "tags": {"content": 2, "fixed": ["示例固定"]}}
    rules = [_rule(1, "required_phrase", {"any": ["示例固定"], "where": ["tags"]}),
             _rule(2, "forbidden_word", {"words": ["示例禁话题"], "where": ["tags"]})]
    c = desk_client(default_params=json.dumps({"delivery": tpl}, ensure_ascii=False), memories=rules)
    drafts = distinct_drafts(2)
    drafts[0]["tags"] = ["话题一", "话题二"]
    drafts[1]["tags"] = ["话题一", "示例禁话题"]
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert "#示例固定" in out["delivery"]["text"]
    assert [(h["index"], h["rule_id"]) for h in out["rule_checks"]] == [(1, "m-2")]


def test_body_checks_see_the_body_that_is_delivered():
    """模板管话题时, 正文末尾那行 #话题 被摘进话题行, 交付出去的正文里没有它(codex review on aw#115)。正文规则要查交付的
    那一份: 不然只在话题行里的词被报成「正文里有禁用词」, 必须带的话术靠被摘走的那行混过去。"""
    tpl = {"layout": "blocks", "tags": {"content": 2}}
    rules = [_rule(1, "forbidden_word", {"words": ["示例禁话题"], "where": ["body"]}),
             _rule(2, "required_phrase", {"any": ["示例必带"], "where": ["body"]})]
    c = desk_client(default_params=json.dumps({"delivery": tpl}, ensure_ascii=False), memories=rules)
    drafts = distinct_drafts(2)
    drafts[0]["body"] += "示例必带。\n#示例禁话题 #话题二"
    drafts[1]["body"] += "\n#示例必带 #话题二"
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert "#示例禁话题" in out["delivery"]["text"], "前提: 它在交付稿的话题行里, 不在正文里"
    assert [(h["index"], h["rule_id"]) for h in out["rule_checks"]] == [(1, "m-2")]


def test_the_rule_lookup_does_not_pull_embeddings():
    """每次存档都多拉一遍 768 维向量是白花的流量 —— 代码查规则只要带 check 的那几行。"""
    c = desk_client(memories=[_rule(1, "forbidden_word", {"words": ["甲"]})])
    core.commit_drafts(c, PROJ, distinct_drafts(1), user_id=ME)
    reads = [x for x in c.calls if x["table"] == "memories" and x["op"] == "select"]
    assert reads and all("embedding" not in x["cols"] for x in reads)
    assert all(("in", "rule_kind", rulecheck.CHECK_KINDS) in x["filters"] for x in reads)


def test_a_required_phrase_naming_another_project_is_refused():
    """必须带的话术会变成"正文里没有「X」"回给写手: X 是别的项目的品牌名, 等于叫模型往这个项目里写别家的名字。
    禁用词不受这道限制 —— 禁掉别家的名字恰恰是常见用法。"""
    sb = FakeClient(rows={"memories": [], "projects": [
        {"id": PROJ, "name": "示例项目", "brand": "示例品牌", "owner_id": ME},
        {"id": "p-other", "name": "另一个项目", "brand": "别家品牌", "owner_id": ME}]})
    with pytest.raises(core.ProjectIsolationRejected):
        core.record_rule(sb, PROJ, "必须带上指定话术", user_id=ME,
                         check={"kind": "required_phrase", "any": ["别家品牌"]})
    assert sb.rows["memories"] == []
    out = core.record_rule(sb, PROJ, "不许提竞品", user_id=ME,
                           check={"kind": "forbidden_word", "words": ["别家品牌"]})
    assert out["check"]["words"] == ["别家品牌"]


def test_a_tag_both_passed_and_in_the_body_counts_once():
    """写手传的 tags 和正文末尾的 #话题 是同一批时只算一次: 按个数数的检查不能把同一个话题数两遍。"""
    parts = rulecheck._parts({"title": "t", "body": "正文一段。\n#话题一 #话题二", "tags": ["话题一", "#话题二", "话题三"]})
    assert parts["tags"] == ["话题一 话题二 话题三"]


# ══════════════════════════════════════════════════════════════════════
# 正则限时: 没查完的不算命中, 也不能说成守住了(codex review on aw#115 第四轮)
# ══════════════════════════════════════════════════════════════════════

SLOW = "a*aaa*aaa*aaa*aaa*aaa*aaa*aaa*b"          # 闸放行(闸不改), 50 个 a 就 6 秒, 2000 个 a 永不返回


@pytest.fixture
def short_timeout(monkeypatch):
    import config
    monkeypatch.setattr(config, "REGEX_SEARCH_TIMEOUT_SEC", 0.1)
    validator.search_user_regex(SLOW, "x")       # 先编译好: 编译不算进时限
    return 0.1


def test_a_slow_regex_rule_is_reported_as_timed_out_not_as_a_hit(short_timeout):
    rules = [_rule(1, "forbidden_regex", {"pattern": SLOW}),
             _rule(2, "forbidden_regex", {"pattern": "示例禁词"}),
             _rule(3, "forbidden_word", {"words": ["示例禁词"]}, severity="soft")]
    drafts = [{"title": "a" * 2000, "body": "a" * 2000 + "示例禁词"} for _ in range(20)]
    t0 = time.perf_counter()
    hits, stats = rulecheck.check(rules, drafts)
    assert time.perf_counter() - t0 < 10 * short_timeout, "一条慢正则一次 check 只花一份时限, 不是每篇每个部位各一份"
    assert stats == {"rules": 3, "skipped": 0, "timed_out": ["m-1"], "timed_out_from": {"m-1": 0}}
    assert {h["rule_id"] for h in hits} == {"m-2", "m-3"} and len(hits) == 40, "别的规则照旧逐篇查"


def test_a_hit_found_before_the_time_ran_out_is_still_reported(short_timeout):
    """时限是在搜到之后才用完的: 搜到的那一处是真的, 照报; 这条同时报没查完(后面的稿子没搜)。"""
    rule = _rule(1, "forbidden_regex", {"pattern": "a*aaa*aaa*aaa*aaa*aaa*aaa*aaa*b|示例禁词", "where": ["title", "body"]})
    hits, stats = rulecheck.check([rule], [{"title": "示例禁词", "body": "a" * 2000}, {"title": "示例禁词", "body": "b"}])
    assert [(h["index"], h["where"]) for h in hits] == [(0, "title")]
    assert stats["timed_out"] == ["m-1"] and stats["timed_out_from"] == {"m-1": 0}


def test_score_drafts_says_from_which_post_a_rule_went_unchecked(short_timeout):
    """搜到一处之后才没查完: 前面的命中是真的, 照列在 rule_checks 里; rule_checks_unchecked 说清楚从哪篇起没查, 不再整条说
    "算没查、不是查出了违规"(同一条两头打架; 独立复审 on aw#115 第六轮)。"""
    c = desk_client(memories=[_rule(1, "forbidden_regex", {"pattern": SLOW + "|示例禁词"})])
    drafts = distinct_drafts(3)
    drafts[0]["body"] += "示例禁词"
    drafts[1]["body"] += "a" * 2000
    drafts[2]["body"] += "示例禁词"
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert [(h["index"], h["rule_id"]) for h in out["rule_checks"]] == [(0, "m-1")]
    [u] = out["rule_checks_unchecked"]
    assert u["from_index"] == 1 and "index=1 那篇起没查完" in u["what"] and "之前查出来、列在 rule_checks 里的照改" in u["what"]
    assert "from_index" in out["next_step"] and "照常算" in out["next_step"]
    assert c.rows["batch_metrics"][0]["meta"]["rule_checks"]["timed_out_from"] == {"m-1": 1}


def test_ordinary_regex_rules_hit_exactly_as_before():
    """换成限时的引擎之后, 正常的正则命中一模一样(emoji、①、组合符这些两个引擎原生字集不一样的字也一样)。"""
    rules = [_rule(1, "forbidden_regex", {"pattern": "^\\W", "where": ["title"]}),
             _rule(2, "forbidden_regex", {"pattern": "[^\\w\\s]{3,}", "where": ["body"]}, severity="soft"),
             _rule(3, "forbidden_regex", {"pattern": "\\bad\\b", "where": ["body"]}),
             _rule(4, "forbidden_regex", {"pattern": "\\d+(?:\\.\\d+)?元", "where": ["body"]})]
    drafts = [{"title": "①先说结论", "body": "好物❤️❤️分享 this ad 9.9元"},
              {"title": "❤️开头", "body": "made 九块九"},
              {"title": "普通标题", "body": "！！！ 三个感叹号"}]
    hits, stats = rulecheck.check(rules, drafts)
    assert "timed_out" not in stats
    assert [(h["index"], h["rule_id"], h["what"]) for h in hits] == [
        (0, "m-3", "必须改: 正文里有不该出现的写法「ad」"),
        (0, "m-4", "必须改: 正文里有不该出现的写法「9.9元」"),
        (0, "m-2", "建议改: 正文里有不该出现的写法「❤️❤️」"),
        (1, "m-1", "必须改: 标题里有不该出现的写法「❤」"),
        (2, "m-2", "建议改: 正文里有不该出现的写法「！！！」")]


def test_score_drafts_lists_the_unchecked_rule_and_never_calls_it_held(short_timeout):
    c = desk_client(memories=[_rule(1, "forbidden_regex", {"pattern": SLOW}),
                              _rule(2, "forbidden_word", {"words": ["不会出现的词"]}, severity="soft")])
    drafts = distinct_drafts(2)
    drafts[0]["body"] += "a" * 2000
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert out["written"] == 2 and "rule_checks" not in out
    assert out["rule_checks_unchecked"] == [{"rule_id": "m-1", "severity": "hard", "rule": "示例规则 1", "from_index": 0,
                                             "what": "硬约束·没查完: 这条规则的正则从 index=0 那篇起没查完(超时或者限时的引擎"
                                                     "跑不了), 那几篇算没查, 不是查出了违规"}]
    # 简报里只有规则原文、没有 id: 不带原文写手对不上是哪一条, 「照规则原文自己核」做不到(独立复审 on aw#115 第六轮)
    assert "照规则原文(rule)" in out["next_step"]
    # 「必须改」在 rule_checks 里是"这篇查出了违规": 没查完的不能用这个开头, 不然写手会跟用户说稿子违规了(codex review on aw#115)
    assert not any(u["what"].startswith(tuple(rulecheck.SEVERITY_LABEL.values())) for u in out["rule_checks_unchecked"])
    assert "rule_checks_unchecked" in out["next_step"] and "不能跟用户说这几条守住了" in out["next_step"]
    assert "不是查出了违规" in out["next_step"]
    assert c.rows["batch_metrics"][0]["meta"]["rule_checks"]["timed_out"] == ["m-1"]


def test_an_ordinary_boundary_rule_reports_its_hit_on_a_normal_batch():
    """20 篇各一千来字, 最后一篇里有一处: 照常报命中, 不能因为换了限时的引擎就慢到报成没查(codex review on aw#115 第五轮 ——
    早先 \\w 列成七百多段的显式字集, 这一批 0.1 秒就到点, 真命中只出现在 rule_checks_unchecked 里)。"""
    drafts = distinct_drafts(20)
    for d in drafts:
        d["body"] = d["body"] * 4
    drafts[-1]["body"] += " 示例联系 vx 详聊"
    c = desk_client(memories=[_rule(1, "forbidden_regex", {"pattern": r"\b(?:vx|wx|dd)\b"}),
                              _rule(2, "forbidden_regex", {"pattern": r"\bvx\b|\bwx\b|\bdd\b"})])
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    assert "rule_checks_unchecked" not in out
    assert [(h["index"], h["rule_id"], h["what"]) for h in out["rule_checks"]] == [
        (19, "m-1", "必须改: 正文里有不该出现的写法「vx」"), (19, "m-2", "必须改: 正文里有不该出现的写法「vx」")]


def test_a_check_with_nothing_slow_carries_no_unchecked_marker():
    out = core.commit_drafts(desk_client(memories=[_rule(1, "forbidden_regex", {"pattern": "不会出现"})]), PROJ,
                             distinct_drafts(1), user_id=ME)
    assert "rule_checks_unchecked" not in out and "rule_checks_unchecked" not in out["next_step"]


# ══════════════════════════════════════════════════════════════════════
# 必须带的话术: 模板要求的部位空着就是没带(codex review on aw#115)
# ══════════════════════════════════════════════════════════════════════

def test_an_expected_part_left_empty_counts_as_missing_the_phrase():
    rule = _rule(1, "required_phrase", {"any": ["示例口播"], "where": ["comments"]})
    draft = {"title": "t", "body": "b"}
    assert rulecheck.check([rule], [draft])[0] == [], "模板不要求评论: 评论常常是后面另走评论那一步写的, 不查"
    hits = rulecheck.check([rule], [draft], expects=("comments",))[0]
    assert [h["what"] for h in hits] == ["必须改: 评论里没有「示例口播」中的任何一个(这篇没带评论)"]
    tagged = _rule(2, "required_phrase", {"any": ["示例话题"], "where": ["tags"]})
    assert rulecheck.check([tagged], [draft], expects=("comments",))[0] == []
    assert len(rulecheck.check([tagged], [draft], expects=("tags",))[0]) == 1


def _tpl(**kw) -> str:
    return json.dumps({"delivery": {"layout": "blocks", **kw}}, ensure_ascii=False)


_COMMENTS = {"label_reader": "读者：", "label_author": "作者："}


@pytest.mark.parametrize("default_params, expect_hit", [
    (_tpl(comments={"min": 1, "max": 2, **_COMMENTS}), True),
    (None, False),
    (_tpl(comments={"min": 0, "max": 2, **_COMMENTS}), False),
    (_tpl(tags={"content": 1}), False),
], ids=["template-expects-comments", "no-template", "template-min-0", "template-without-comments"])
def test_a_comment_phrase_rule_at_scoring_follows_the_template(default_params, expect_hit):
    rule = _rule(1, "required_phrase", {"any": ["示例口播"], "where": ["comments"]})
    c = desk_client(default_params=default_params, memories=[rule])
    drafts = distinct_drafts(1)
    drafts[0]["tags"] = ["话题一"]
    out = core.commit_drafts(c, PROJ, drafts, user_id=ME)
    got = [(h["rule_id"], h["where"]) for h in out.get("rule_checks", [])]
    assert got == ([("m-1", "comments")] if expect_hit else [])
    with_phrase = distinct_drafts(1, seed=3)
    with_phrase[0].update(tags=["话题一"], comments=[{"role": "reader", "text": "示例口播在这里"}])
    assert "rule_checks" not in core.commit_drafts(c, PROJ, with_phrase, user_id=ME), "带了就不报"


@pytest.mark.parametrize("tags_tpl, expect_hit", [
    ({"content": 2}, True),
    ({"content": 0, "conditional": [{"when_any": ["不会出现"], "add": "条件话题"}]}, False),
    (None, False),
], ids=["template-asks-for-tags", "only-conditional-tags", "no-template"])
def test_a_tag_phrase_rule_at_scoring_follows_the_template(tags_tpl, expect_hit):
    rule = _rule(1, "required_phrase", {"any": ["示例话题"], "where": ["tags"]})
    c = desk_client(default_params=None if tags_tpl is None else _tpl(tags=tags_tpl), memories=[rule])
    out = core.commit_drafts(c, PROJ, distinct_drafts(1), user_id=ME)
    assert [h["rule_id"] for h in out.get("rule_checks", [])] == (["m-1"] if expect_hit else [])


def test_fixed_tags_from_the_template_count_as_tags_given():
    """有固定话题的模板: 话题行永远不空, 照常查(固定话题里有就算带了)。"""
    rules = [_rule(1, "required_phrase", {"any": ["示例固定"], "where": ["tags"]}),
             _rule(2, "required_phrase", {"any": ["别的话题"], "where": ["tags"]})]
    c = desk_client(default_params=_tpl(tags={"content": 0, "fixed": ["示例固定"]}), memories=rules)
    out = core.commit_drafts(c, PROJ, distinct_drafts(1), user_id=ME)
    assert [h["rule_id"] for h in out["rule_checks"]] == ["m-2"]


def test_the_generation_path_never_judges_a_comment_or_tag_phrase_rule():
    """AW 生成链路(validator.check_hard_rules)只看得到标题正文: 只管评论 / 话题的必须带的话术它一律跳过(_spec_from_desk),
    不管模板要不要 —— 生成出来的东西里本来就没有评论。"""
    for where in (["comments"], ["tags"], ["body", "comments"]):
        rule = _rule(1, "required_phrase", {"any": ["示例口播"], "where": where})
        assert validator.check_hard_rules([rule], title="t", body="b") == [], where


# ══════════════════════════════════════════════════════════════════════
# 查词的词边界只加在字母数字那一头(codex review on aw#115)
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text, term, found", [
    ("详情见 www.example.com", "www.", True),
    ("WWW.EXAMPLE.COM", "www.", True),
    ("wwww.example", "www.", False),
    ("官网 example.com 有", ".com", True),
    ("example.community", ".com", False),
    ("学C++入门", "c++", True),
    ("x a.b y", "a.b", True),
    ("a.b.c", "a.b", True),
    ("xa.b", "a.b", False),
    ("made in", "ad", False),
    ("an AD here", "ad", True),
    ("/path/", "/path/", True),
    # 不是纯英文的词也照两头的字算: 开头是字母的那一头要边界, 中文 / 标点那一头不要(codex review on aw#115 第五轮)
    ("我开了 Apple TV+ 会员", "v+", False),
    ("加v+", "v+", True),
    ("家里TV信号不好", "V信", False),
    ("加V信", "V信", True),
    ("abc++", "c++", False),
    ("学了 ObjC++", "c++", False),
    ("全民K歌", "K歌", True),
    ("OK歌", "K歌", False),
    ("这是示例词", "示例", True),
])
def test_word_boundaries_apply_only_at_alphanumeric_edges(text, term, found):
    assert validator.contains_term(text, term) is found


def test_rulecheck_and_the_generation_path_share_the_term_matcher():
    assert rulecheck._has is validator.contains_term
    forbid = _rule(1, "forbidden_word", {"words": ["www."]})
    need = _rule(2, "required_phrase", {"any": ["www."], "where": ["body"]})
    draft = {"title": "t", "body": "详情见 www.example.com"}
    assert [h["rule_id"] for h in rulecheck.check([forbid, need], [draft])[0]] == ["m-1"]
    assert [h["kind"] for h in validator.check_hard_rules([forbid, need], **draft)] == ["forbidden_word"]
    mixed = _rule(3, "forbidden_word", {"words": ["V信", "v+"]})
    clean = {"title": "t", "body": "家里TV信号不好, 开了 Apple TV+ 会员"}
    assert rulecheck.check([mixed], [clean])[0] == [] and validator.check_hard_rules([mixed], **clean) == []
    dirty = {"title": "t", "body": "加V信 详聊"}
    assert [h["what"] for h in rulecheck.check([mixed], [dirty])[0]] == ["必须改: 正文里有禁用词「V信」"]
    assert [h["match"] for h in validator.check_hard_rules([mixed], **dirty)] == ["V信"]


@pytest.mark.parametrize("kind, target, title, body, hit", [
    ("forbidden_word", "ad", "t", "made in", False),
    ("forbidden_word", "AD", "t", "an ad here", True),
    ("forbidden_word", "示例词", "t", "这里有示例词", True),
    ("required_phrase", "ad", "t", "made in", True),
    ("required_phrase", "AD", "t", "an ad here", False),
    ("required_phrase", "示例话术", "", "", False),
])
def test_legacy_target_rows_get_the_same_answer_on_both_paths(kind, target, title, body, hit):
    """AW 的 Streamlit 界面还在写老形状 {"target": X}: 写作台把它读成词表(rulecheck._from_legacy), 生成链路原来在"标题\\n正文"里
    分大小写找子串 —— "made" 算出现了 "ad"、"an ad" 又不算出现了 "AD", 两边结论相反(codex review on aw#115 第五轮)。"""
    rule = _rule(1, kind, {"target": target})
    desk = rulecheck.check([rule], [{"title": title, "body": body}])[0]
    gen = validator.check_hard_rules([rule], title=title, body=body)
    assert bool(desk) is hit and bool(gen) is hit
