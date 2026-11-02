"""angle cards v2 §6 item 1: 卡面上的大白话、去掉「标志词」的判据、标题句式的粗分类。

钉的是这几件事:
  · PLAIN_* 的键和 vendor 词表 / 标题句式池一个不差 —— 词表一漂, 卡上就会有值没有人话(或者人话对着一个不存在的值);
  · 判据去掉「标志词」那一截(卡上给的词会被模型原样抄进标题和正文), 但 vendor 的 JSON 和它的 sha 一个字不动;
  · classify_title 按顺序判, 先中先得; title_is_question 的答案能覆盖正则那一档; 空标题不算任何一类;
  · note_title 从 TV 的 raw_content 里取【标题】那一行。

用例里的标题全是编的, 不是任何客户的稿子。
"""
from __future__ import annotations

import hashlib
import json

import pytest

from deskcore import vocab

# ══════════════════════════════════════════════════════════════════════
# 大白话
# ══════════════════════════════════════════════════════════════════════


def test_plain_glosses_cover_exactly_the_vocab():
    assert set(vocab.PLAIN_FORMAT) == set(vocab.CONTENT_FORMATS)
    assert set(vocab.PLAIN_TITLE) == set(vocab.TITLE_STRUCTURES)
    assert set(vocab.PLAIN_LEVER) == set(vocab.EMOTIONAL_LEVERS)
    assert set(vocab.INTENSITY_PLAIN) == set(vocab.EMOTIONAL_INTENSITIES)
    for table in (vocab.PLAIN_FORMAT, vocab.PLAIN_TITLE, vocab.PLAIN_LEVER, vocab.OPENING_PLAIN):
        for k, v in table.items():
            assert isinstance(v, str) and v.strip(), k
            assert "标志词" not in v and k not in v, (k, v)    # 人话里不复述分类名本身


def test_plain_glosses_match_the_spec_wording_for_a_few_values():
    assert vocab.PLAIN_FORMAT["提问求助"] == "拿自己遇到的一件具体的事向大家讨主意"
    assert vocab.PLAIN_TITLE["疑问句"] == "标题就是一个问句"
    assert vocab.PLAIN_LEVER["焦虑撬动"] == "担心以后可能发生、还没发生的事"
    assert [vocab.INTENSITY_PLAIN[k] for k in ("low", "medium", "high")] == ["弱", "中", "强"]


def test_opening_plain_omits_other():
    assert "其他" not in vocab.OPENING_PLAIN
    assert set(vocab.OPENING_PLAIN) == {"具体事件", "身份自述", "观点断言", "提问", "数据事实", "感叹情绪"}
    assert vocab.OPENING_PLAIN.get("其他") is None


def test_title_alias_and_nonq_split():
    assert vocab.TITLE_ALIASES == {"问句": "疑问句"}
    assert all(v in vocab.TITLE_STRUCTURES for v in vocab.TITLE_ALIASES.values())
    assert set(vocab.NONQ_SPLIT) == set(vocab.TITLE_STRUCTURES) - {"疑问句"}
    assert sum(vocab.NONQ_SPLIT.values()) == pytest.approx(1.0)
    assert max(vocab.NONQ_SPLIT, key=vocab.NONQ_SPLIT.get) == "场景直述"


# ══════════════════════════════════════════════════════════════════════
# 判据去掉「标志词」
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("lever", vocab.EMOTIONAL_LEVERS)
def test_boundary_rule_text_never_carries_marker_words(lever):
    raw = vocab.boundary_rules_for(lever)
    got = vocab.boundary_rule_text(lever)
    assert "标志词" not in got
    if "标志词" not in raw:
        assert got == raw, "没有标志词的规则原样给"
    else:
        assert raw.startswith(got) and len(got) < len(raw)
        assert not got.endswith(("。", "，", ",", " ")), "前面那个句号一起吃掉, 不留孤零零的标点"


def test_boundary_rule_text_lengths_match_the_spec():
    """spec §2.8 实测: 焦虑 85→69 字, 恐惧 98→81 字, 其余三条不变。"""
    assert (len(vocab.boundary_rules_for("焦虑撬动")), len(vocab.boundary_rule_text("焦虑撬动"))) == (85, 69)
    assert (len(vocab.boundary_rules_for("恐惧撬动")), len(vocab.boundary_rule_text("恐惧撬动"))) == (98, 81)
    changed = [lv for lv in vocab.LEVER_BOUNDARY_RULES
               if vocab.boundary_rule_text(lv) != vocab.boundary_rules_for(lv)]
    assert sorted(changed) == sorted(["焦虑撬动", "恐惧撬动"])
    assert len(vocab.LEVER_BOUNDARY_RULES) == 5


def test_boundary_rule_text_unknown_lever_is_empty():
    assert vocab.boundary_rule_text("不存在的杠杆") == ""
    assert vocab.boundary_rule_text("") == ""


def test_boundary_rule_text_handles_fullwidth_colon_and_a_trailing_full_stop(monkeypatch):
    monkeypatch.setitem(vocab.LEVER_BOUNDARY_RULES, "焦虑撬动", "甲是乙。标志词：丙、丁。")
    assert vocab.boundary_rule_text("焦虑撬动") == "甲是乙"
    monkeypatch.setitem(vocab.LEVER_BOUNDARY_RULES, "焦虑撬动", "甲是乙, 标志词: 丙\n下一行照留")
    assert vocab.boundary_rule_text("焦虑撬动") == "甲是乙\n下一行照留"


def test_vendored_vocab_is_untouched():
    """去标志词只在给出去的那一份上做: vendor 的 JSON、它的 sha、读进来的原文都不变。"""
    ok, detail = vocab.vendor_checksum_ok()
    assert ok, detail
    raw = json.load(open(vocab._VOCAB_PATH, encoding="utf-8"))
    assert "标志词" in raw["boundary_rules"]["rules"]["焦虑撬动"]
    assert vocab.boundary_rules_for("焦虑撬动") == raw["boundary_rules"]["rules"]["焦虑撬动"]
    recorded = open(vocab._SHA_PATH, encoding="utf-8").read().split()[0]
    assert hashlib.sha256(open(vocab._VOCAB_PATH, "rb").read()).hexdigest() == recorded


# ══════════════════════════════════════════════════════════════════════
# classify_title
# ══════════════════════════════════════════════════════════════════════

CASES = [
    # 疑问句
    ("这样做到底有没有用", "疑问句"),
    ("晚上总睡不着怎么办", "疑问句"),
    ("为什么越补越累", "疑问句"),
    ("这个习惯是不是该改了", "疑问句"),
    ("有人试过吗", "疑问句"),
    ("下一步该选哪个呢", "疑问句"),
    ("下一步该选哪个呢～", "疑问句"),
    ("真的值得买？", "疑问句"),
    ("真的值得买?", "疑问句"),
    ("求推荐一款通勤包", "疑问句"),
    ("跪求一个靠谱的办法", "疑问句"),
    # 数字清单
    ("3个让早起变轻松的小习惯", "数字清单"),
    ("５种常见误区", "数字清单"),
    ("新手必看的10条建议", "数字清单"),
    ("两个动作搞定肩颈", "数字清单"),
    ("几招教你收纳小空间", "数字清单"),
    # 对白引语
    ("「你怎么又熬夜了」", "疑问句"),            # 问句那一档在前
    ("妈妈说「早点睡」", "对白引语"),
    ("同事一句“你变了”让我愣住", "对白引语"),
    # 第一人称自白
    ("我终于把作息调回来了", "第一人称自白"),
    ("我竟然坚持了一个月", "第一人称自白"),     # 第一人称在对比反转之前
    # 对比反转
    ("以为是小毛病结果拖了半年", "对比反转"),
    ("没想到最有用的是这一步", "对比反转"),
    ("贵的 vs 便宜的", "对比反转"),
    ("贵的 VS 便宜的", "对比反转"),
    ("看着简单却最难坚持", "对比反转"),
    # 比喻起手
    ("像在泥里跑步一样的周一", "比喻起手"),
    ("仿佛回到了学生时代", "比喻起手"),
    # 感叹共鸣
    ("终于等到这一天！", "感叹共鸣"),
    ("太真实了!", "感叹共鸣"),
    # 场景直述
    ("周末在家做了一顿早午餐", "场景直述"),
    ("通勤路上的二十分钟", "场景直述"),
]


@pytest.mark.parametrize("title,want", CASES)
def test_classify_title_cases(title, want):
    assert vocab.classify_title(title) == want


def test_classify_title_reaches_every_structure_and_only_those():
    got = {vocab.classify_title(t) for t, _ in CASES}
    assert got == set(vocab.TITLE_STRUCTURES)


@pytest.mark.parametrize("title,want", [
    ("求职季的第一份简历", "场景直述"),          # 「求职」不是求助
    ("对自己要求太高了", "场景直述"),            # 「要求」
    ("一直在追求的生活节奏", "场景直述"),          # 「追求」
    ("好像哪里不太对", "场景直述"),               # 「好像」不是比喻
    ("一个人的周末", "场景直述"),                # 「一个」不算清单
])
def test_classify_title_avoids_obvious_false_positives(title, want):
    assert vocab.classify_title(title) == want


def test_is_question_answer_overrides_the_regex():
    # 答「是」: 不管正则怎么看都是问句
    assert vocab.classify_title("周末在家做了一顿早午餐", is_question=True) == "疑问句"
    # 答「否」: 跳过问句那一档, 在其余七种里分
    assert vocab.classify_title("3个习惯有没有用", is_question=False) == "数字清单"
    assert vocab.classify_title("这样做到底有没有用", is_question=False) == "场景直述"
    assert vocab.classify_title("真的值得买？！", is_question=False) == "感叹共鸣"
    # 没答: 正则说了算
    assert vocab.classify_title("这样做到底有没有用", is_question=None) == "疑问句"


@pytest.mark.parametrize("title", [None, "", "   ", "\n\t"])
def test_empty_title_is_not_counted(title):
    assert vocab.classify_title(title) is None
    assert vocab.classify_title(title, is_question=True) is None


def test_classify_title_output_is_always_in_the_pool():
    for t in ("a", "！？", "123", "我", "「」", "vs", "像", "求", "……", "😀"):
        assert vocab.classify_title(t) in vocab.TITLE_STRUCTURES, t


# ══════════════════════════════════════════════════════════════════════
# note_title: TV raw_content 里的【标题】
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("title,raw,want", [
    ("表里有标题", "【标题】另一个\n【正文】正文", "表里有标题"),
    (None, "【标题】周末的早午餐\n【正文】第一段\n第二段", "周末的早午餐"),
    ("", "【标题】  前后有空格  \n【正文】x", "前后有空格"),
    (None, "【标题】\n标题在下一行\n【正文】x", "标题在下一行"),
    (None, "【标题】整段一行【正文】正文也在这一行", "整段一行"),
    (None, "【标题】【正文】标题是空的", ""),
    (None, "【标题】【干货】括号是标题的一部分\n【正文】x", "【干货】括号是标题的一部分"),
    (None, "没有标记的第一行\n第二行", "没有标记的第一行"),
    (None, "\n\n  \n第一个非空行", "第一个非空行"),
    (None, "前言\n【标题】标记不在开头\n【正文】x", "标记不在开头"),
    (None, None, ""),
    (None, "   ", ""),
])
def test_note_title(title, raw, want):
    assert vocab.note_title(title, raw) == want


def test_note_title_feeds_classify_title():
    assert vocab.classify_title(vocab.note_title(None, "【标题】晚上总睡不着怎么办\n【正文】x")) == "疑问句"
    assert vocab.classify_title(vocab.note_title(None, "【标题】【正文】x")) is None


def test_every_trend_value_but_the_exclusive_one_has_a_plain_gloss():
    """卡面的时效那一行跟一句大白话; 「通用」后面本来跟着排他说明, 不另加。"""
    assert set(vocab.PLAIN_TREND) == set(vocab.TREND_DEPENDENCIES) - {vocab.TREND_EXCLUSIVE}
    assert all(v and "标志词" not in v for v in vocab.PLAIN_TREND.values())
