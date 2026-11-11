"""2026-11-11 owner 对写评论的四件事, 所有项目都管(docs/deskcore.md §3.22)。

钉住:
  1. 不提帖子: 一提帖子就报(不只「帖子里」), 单独写评论和跟着笔记交付的两条路都查; Jev 的修改单里提到帖子的那一句后面
     补一句「评论里不提帖子」, 细节没过的那一句补一句「点到就够, 不加长」;
  2. 不为凑字数加话、常识不写成新发现: 只给模型看(comments_for.style、交付模板的 task), 代码不查;
     score_comments 的 note 说照 fix / hints 改不是要加长;
  3. 整组代码查那一句: 里面有配比的数时说清条数是这个项目定的配比、项目规则(和之前见过的规则原文)说法不一样的以它为准,
     直接改, 不问用户、不报负责人; 用户这次明说要别的配比就照用户的(不压用户, 不说「最新」), 明说这个项目以后都按新的就调
     set_comment_mix(11-11 晚 owner 开放了配比, 见 tests/test_set_comment_mix.py; 「是负责人给这个项目定的」跟着改成
     「这个项目定的配比」: 数可能是写手照用户说的改过的); 只有所有项目都管的几样时
     不说是项目定的配比、不带 set_next_step; set_next_step 按报了的几样拼(判成和没判成两条路都带);
     comments_for 的 comment_set 第一句、next_step 同一个口径;
  4. 规则过时了写手自己停: comments_for 有规则时带 rules_note, my_rules 带 state_note(试用、已停用的不用守、不算冲突);
  5. 笔记那一侧: 简报里有规则说到字数时补一句(硬约束在 P0: 字数照守、只拿具体内容补; 只有软规则在 P1: 不是要凑够的数,
     上限照守), 管评论的规则和「一个字都不能错」这类不算; 下限没到的那句说补具体内容、不加空话。
全部是合成文字、合成品牌和合成规则, 不引用真实评论、真实规则或真实列名。
"""
from __future__ import annotations

import pytest

from deskcore import commentstyle as cs
from deskcore import core, delivery, rulecheck, tools
from tests.fakes import FakeClient
from tests.test_comment_set_score import BRANDED, PLAIN, _acts, _client, _judge, _score
from tests.test_deskcore_judge_tools import _Resp, wired  # noqa: F401 — fixture
from tests.test_deskcore_score_and_comments import LINK, ME
from tests.test_delivery import PROJ, desk_client
from tests.test_rulecheck import _rule
from tests.test_writer_facing_text import FORBIDDEN

RULE_A = {"id": "r-a", "project_id": None, "scope": "project", "status": "confirmed", "severity": "soft",
          "content": "评论区：示例牌不用代称", "memory_type": "rule", "created_at": "2026-10-01"}


def _with_rules(c, rules):
    pid = c.rows["projects"][0]["id"]
    c.rows["memories"] = [dict(r, project_id=pid) for r in rules]
    return c


# ── 1. 不提帖子 ─────────────────────────────────────────────────────────────────────────────────

def test_score_comments_flags_any_post_mention_without_a_comment_set(wired):
    _, reply = wired
    texts = ["看完帖子我也想试试", "这帖的清单收藏了", "同问价格"]
    reply["fn"] = _judge(_acts(texts))
    out = _score(_client(cfg=None), texts)
    rows = out["comments"]
    assert rows[0]["style_fix"] == ["去掉「帖子」: 评论里不提帖子, 直接说那件事(「帖子说的 X」「这帖的 X」→ 直接说 X)"]
    assert rows[1]["style_fix"][0].startswith("去掉「这帖」: ")
    assert "style_fix" not in rows[2] and out["passed"] is False
    assert "提到帖子" in out["style_next_step"] and "「帖子里」" not in out["style_next_step"]


def test_delivery_flags_any_post_mention_too():
    tpl = {"layout": "blocks", "comments": {"min": 1, "max": 3, "label_reader": "读者", "label_author": "作者"}}
    draft = {"title": "甲", "body": "正文", "comments": [{"role": "reader", "text": "这个帖子挺实在的"},
                                                       {"role": "author", "text": "粉底很服帖"}]}
    problems = [p["what"] for p in delivery.assemble(tpl, [draft])["problems"]]
    assert problems == ["第 1 条评论: 去掉「这个帖子」: 评论里不提帖子, 直接说那件事(「帖子说的 X」「这帖的 X」→ 直接说 X)"]
    assert cs.SHORT in delivery.requirements(tpl)["task"]


def test_jev_lines_that_mention_the_post_get_one_reminder():
    """Jev 的话是在说评论, 本身可以提帖子; 第一句提到帖子的后面补一句, 只补一次, 不提帖子的不动。"""
    entries = [{"qid": "speech_act", "instruction": "主要行为要改成提问"},
               {"qid": "echoes_post", "instruction": "要接住可见帖子里具体说到的事"},
               {"qid": "names_brand", "instruction": "不要从帖子借品牌"}]
    assert core._instructions(entries, 3) == [
        "主要行为要改成提问", "要接住可见帖子具体说到的事" + core.POST_REF_NOTE, "不要从帖子借品牌"]
    assert core._instructions(entries[:1], 3) == ["主要行为要改成提问"], "没提帖子就不补"
    assert core.POST_REF_NOTE == "(评论里不提帖子, 直接说那件事)"
    assert core._thread_lines([{"slot": "q1", "why": "评论区没有摩擦"}, {"slot": "exp", "why": "接住帖子里的事"},
                               {"slot": None, "why": "别和帖子重复"}]) == [
        "位 q1: 评论区没有摩擦", "位 exp: 接住帖子的事" + core.POST_REF_NOTE, "别和帖子重复"]


def test_the_reminder_survives_the_length_cap():
    long = "要接住帖子里具体说到的事" + "，" * (core._COMMENT_FIX_CHARS + 50)
    (line,) = core._instructions([{"qid": "echoes_post", "instruction": long}], 1)
    assert line.endswith(core.POST_REF_NOTE) and len(line) == core._COMMENT_FIX_CHARS - 1 + len(core.POST_REF_NOTE)


# ── 2. 不凑字数、常识不写成新发现 ─────────────────────────────────────────────────────────────────

def test_detail_fixes_say_a_situation_is_enough():
    (line,) = core._instructions([{"qid": "detail_level", "instruction": "细节现在判「无」，要改成「至少模糊」"}], 3)
    assert line == "细节现在判「无」，要改成「至少模糊」" + core.DETAIL_NOTE
    assert core.DETAIL_NOTE == "(点到一个情境或做法就够, 不为这个加长)"
    (other,) = core._instructions([{"qid": "register", "instruction": "口语一点"}], 3)
    assert other == "口语一点", "只给细节那道题补"


def test_comments_for_hands_out_the_two_new_lines_right_after_the_checked_ones():
    out = core.comments_for(_client(cfg=None), user_id=ME, link=LINK)
    n = len(cs.LINES)
    assert out["style"][:n] == list(cs.LINES)
    assert out["style"][n:n + 2] == [cs.PAD_LINE, cs.COMMON_LINE]
    assert out["style"] == list(core.COMMENT_STYLE_LINES)


def test_exp_slot_says_one_or_two_sentences_is_enough():
    exp = next(s for s in core.DEFAULT_COMMENT_SLOTS if s["id"] == "exp")
    assert exp["note"].endswith("一两句就够, 不凑字数") and "依提供素材" in exp["note"]


def test_score_note_says_fixing_is_not_lengthening():
    note = core.COMMENT_SCORE_NOTE
    assert "照 fix、hints 改都不是要加长: 补一件具体的事就够, 不为凑字数加话。" in note
    assert "style_fix 和 thread_fix 第一句(整组代码查)也是必须改的, 直接改, 不用问用户" in note
    assert "summary.passed 只数每条, 整组 passed=false 时照 thread_fix 第一句改" in note
    assert "全文" in note and "不缩写" in note, "11-10 那几句照旧在"


# ── 3. 整组代码查那一句 ─────────────────────────────────────────────────────────────────────────

SHORT_FAIL = [BRANDED[0], BRANDED[1], "我自己是先投小公司练手的，面了七八家才找到感觉",
              "我们组去年招人主要看实习经历和作品", "作品集一般要放几个项目才够看大家知道吗"]


def test_set_line_head_says_whose_numbers_and_what_to_do():
    assert core.SET_LINE_HEAD == "整组代码查(条数是这个项目定的配比, 项目规则里说法不一样时以这里为准; 直接改, 不用问用户): "
    assert core.SET_LINE_HEAD_ALL == "整组代码查(所有项目都管; 直接改, 不用问用户): "
    for head in (core.SET_LINE_HEAD, core.SET_LINE_HEAD_ALL):
        assert head.startswith("整组代码查"), "工具说明和回包都按这四个字认那一句"


def test_short_floor_failure_carries_the_head_and_the_set_next_step(wired):
    _, reply = wired
    reply["fn"] = _judge(_acts(SHORT_FAIL, act_for_plain="补充经验"))
    out = _score(_client(), SHORT_FAIL)
    assert out["passed"] is False and out["summary"]["passed"] == len(SHORT_FAIL), "每条都过了, 是整组不够"
    line = out["thread_fix"][0]
    assert line == core.SET_LINE_HEAD + "15 字以内的一句话短评 0 条, 至少 1 条: 把 第 5 条 改成一句话短评(比如蹲一个、同问)"
    assert out["set_next_step"] == (core.SET_NEXT_STEP + core.SET_SHORT_STEP + core.USER_MIX_NOTE
                                    + core.RULE_SELF_SERVICE), "只报了短评: 不说规则里要每条带品牌的那句"
    assert "style_next_step" not in out, "没有哪一条要改写法, 只有整组那一句"


def test_set_next_step_text():
    assert core.SET_NEXT_STEP == ("thread_fix 第一句里提品牌、中性、短评的条数是这个项目定的配比: 照它点名的几条直接改, "
                                  "改完整组再判一次; 不用问用户, 也不用报给负责人。")
    assert core.SET_BRAND_STEP == ("项目规则里或之前见过的规则原文说要每条带品牌、要几条夸或背书的, 以这里的条数为准, "
                                   "不算冲突。")
    assert core.SET_SHORT_STEP == "短评不够: 把点名的几条压短, 或换成一句话短评(比如「蹲一个」「同问」), 这不算改口气。"
    assert core.USER_MIX_NOTE == ("用户这次明说要别的配比(比如每条都带品牌), 这一组就照用户的写, 交付时一句话说明和项目定的"
                                  "配比不一样; 用户明说这个项目以后都按新的配比, 就调 set_comment_mix 把项目的数改掉, "
                                  "不用找负责人。只有用户明说才改: 不为了让这一组通过自己去改配比。")
    assert core.RULE_SELF_SERVICE == ("用户说哪条项目规则过时了: 当场用 set_rule_state 停掉(以后都不要用 retire, 先停一阵用 "
                                      "mute; memory_id 在 my_rules 里), 不用等负责人; 要换成新说法用 record_rule。")


def test_brand_excess_also_carries_the_set_next_step(wired):
    _, reply = wired
    texts = BRANDED + PLAIN
    reply["fn"] = _judge(_acts(texts))
    out = _score(_client(), texts)
    assert out["thread_fix"][0].startswith(core.SET_LINE_HEAD + "提品牌的 12 条, 最多 10 条")
    assert out["set_next_step"] == (core.SET_NEXT_STEP + core.SET_BRAND_STEP + core.USER_MIX_NOTE
                                    + core.RULE_SELF_SERVICE), "短评够了: 不说怎么补短评"
    assert out["style_next_step"] == core.COMMENT_STYLE_NEXT_STEP


def test_no_set_line_means_no_set_next_step(wired):
    _, reply = wired
    texts = BRANDED[:10] + PLAIN + ["我是看了三遍回放才搞懂排期的", "做设计的转产品难不难"]
    reply["fn"] = _judge(_acts(texts))
    out = _score(_client(), texts)
    assert out["passed"] is True and "thread_fix" not in out and "set_next_step" not in out


def test_judge_down_still_carries_the_set_next_step(wired):
    _, reply = wired
    reply["fn"] = lambda m, u, kw: _Resp(503, {"detail": "评论判定服务未开启"})
    out = _score(_client(), SHORT_FAIL)
    assert out["judge_status"] != "ok"
    assert out["thread_fix"][0].startswith(core.SET_LINE_HEAD)
    assert out["set_next_step"] == (core.SET_NEXT_STEP + core.SET_BRAND_STEP + core.SET_SHORT_STEP + core.USER_MIX_NOTE
                                    + core.RULE_SELF_SERVICE), "中性这次没数也是配比那几样"


def test_without_a_comment_set_the_line_is_not_called_the_owners_numbers(wired):
    """复审 11-11: 没配 comment_set 的项目, 整组那一句只有重复、品牌收尾(所有项目都管): 不说是这个项目定的配比,
    不带 set_next_step(不叫模型拿它压过「每条都带品牌」的规则; 那几条的 style_fix 有 style_next_step)。"""
    _, reply = wired
    texts = ["示例牌的老师很耐心", "示例牌的课排得紧", "示例牌的顾问回得快", "同问价格"]
    reply["fn"] = _judge(_acts(texts))
    out = _score(_client(cfg=None), texts)
    assert out["thread_fix"][0].startswith(core.SET_LINE_HEAD_ALL + "重复的说法")
    assert "负责人" not in out["thread_fix"][0] and "项目定的" not in out["thread_fix"][0]
    assert "set_next_step" not in out and out["style_next_step"] == core.COMMENT_STYLE_NEXT_STEP


def test_judge_down_without_a_comment_set_has_no_set_next_step(wired):
    _, reply = wired
    reply["fn"] = lambda m, u, kw: _Resp(503, {"detail": "评论判定服务未开启"})
    texts = ["示例牌的老师很耐心", "示例牌的课排得紧", "示例牌的顾问回得快", "同问价格"]
    out = _score(_client(cfg=None), texts)
    assert out["thread_fix"][0].startswith(core.SET_LINE_HEAD_ALL) and "set_next_step" not in out


def test_only_rule_text_gives_way_not_what_the_user_says_now():
    """复审 11-11: 配比压的是规则原文(项目规则、之前见过的旧规则), 不压用户这次在对话里明说的(协议: 用户另有要求就照用户的);
    也不说「最新」「比…都新」: 配比没有日期。"""
    texts = [core.SET_LINE_HEAD, core.SET_NEXT_STEP, core.SET_BRAND_STEP, core.COMMENT_SET_HEAD,
             core.COMMENT_SET_NEXT_STEP, core.COMMENTS_FOR_RULES_NOW,
             tools.comments_for.__doc__, tools.score_comments.__doc__]
    for t in texts:
        assert "最新" not in t and "都新" not in t and "前面对话里的说法" not in t, t[:40]
    for t in (core.COMMENT_SET_HEAD, core.COMMENT_SET_NEXT_STEP):
        assert "用户这次明说要别的配比" in t and "就照用户的写" in t
    assert core.COMMENT_SET_HEAD.endswith(core.USER_MIX_NOTE)


def test_judge_down_without_a_set_line_has_no_set_next_step(wired):
    _, reply = wired
    reply["fn"] = lambda m, u, kw: _Resp(503, {"detail": "评论判定服务未开启"})
    out = _score(_client(cfg=None), PLAIN[:3])
    assert "thread_fix" not in out and "set_next_step" not in out


def test_comments_for_comment_set_block_says_whose_numbers_they_are():
    out = core.comments_for(_client(), user_id=ME, link=LINK)
    lines = out["comment_set"]["lines"]
    assert lines[0] == core.COMMENT_SET_HEAD
    assert core.COMMENT_SET_HEAD == ("下面几个数是这个项目定的配比: 项目规则里或之前见过的规则原文要每条带品牌、"
                                     "要几条夸或背书的, 照这里写, 不算冲突; 品牌怎么称呼(全称、简称)照项目规则; "
                                     "不用问用户, 也不用报给负责人。" + core.USER_MIX_NOTE)
    assert lines[1].endswith("不提品牌时也不用代称(这家、它家、缩写)"), "「以这一句为准」并进第一句了"
    assert lines[3].endswith("短评放在提问位或不带评论位; 经验位那条接住帖子说的事, 一两句就够")
    assert not any("太短过不了" in x for x in lines)
    assert out["next_step"].count(core.COMMENTS_FOR_RULES_NOW) == 1
    assert core.COMMENT_SET_NEXT_STEP in out["next_step"] and "不算冲突, 不用问用户" in core.COMMENT_SET_NEXT_STEP
    assert core.COMMENT_SET_NEXT_STEP == (
        "这个项目定了一组的配比(comment_set.lines), 和 style 一样要守; 项目规则里说法不一样的以它为准, 不算冲突, 不用问用户; "
        "用户这次明说要别的配比, 这一组就照用户的写, 交付时一句话说明; 用户明说这个项目以后都按新的配比, 调 set_comment_mix 改, "
        "不用找负责人。")
    assert "只拿来避开重复, 配比照 comment_set" not in out["next_step"], "那一格是空的, 不说"


def test_comments_for_says_the_cell_comments_follow_the_old_mix():
    out = core.comments_for(_client(cell="1. 示例牌的课我上过\n2. 同问"), user_id=ME, link=LINK)
    assert "按以前的规矩写的(提品牌多、短评少), 只拿来避开重复, 配比照 comment_set" in out["next_step"]


def test_comments_for_without_a_comment_set_still_says_rules_are_as_of_now():
    out = core.comments_for(_client(cfg=None), user_id=ME, link=LINK)
    assert "comment_set" not in out and out["next_step"].endswith(core.COMMENTS_FOR_RULES_NOW)
    assert core.COMMENTS_FOR_RULES_NOW == "规则以这次回的为准, 前面对话里见过的规则原文和这次不一样的不算(可能已经停掉了)。"


# ── 4. 规则过时了写手自己停 ─────────────────────────────────────────────────────────────────────

def test_rules_note_only_when_there_are_rules_to_stop():
    with_rules = core.comments_for(_with_rules(_client(cfg=None), [RULE_A]), user_id=ME, link=LINK)
    assert with_rules["comment_rules"] == ["评论区：示例牌不用代称"] and with_rules["rules_note"] == core.RULE_SELF_SERVICE
    hard_only = core.comments_for(_with_rules(_client(cfg=None), [dict(RULE_A, content="不承诺效果", severity="hard")]),
                                  user_id=ME, link=LINK)
    assert hard_only["hard_rules"] == ["不承诺效果"] and hard_only["rules_note"] == core.RULE_SELF_SERVICE
    none = core.comments_for(_with_rules(_client(cfg=None), []), user_id=ME, link=LINK)
    assert none["comment_rules"] == none["hard_rules"] == [] and "rules_note" not in none


def test_retired_rules_are_not_handed_out_next_time():
    """retire 之后下一次 comments_for 就不给了: 规则是每次现取的, 只取生效的。"""
    c = _with_rules(_client(cfg=None), [RULE_A, dict(RULE_A, id="r-b", content="评论区：示例旧规则", status="candidate"),
                                        dict(RULE_A, id="r-c", content="评论区：示例停用规则",
                                             muted_until="2099-01-01T00:00:00+00:00")])
    assert core.comments_for(c, user_id=ME, link=LINK)["comment_rules"] == ["评论区：示例牌不用代称"]


def test_my_rules_says_inactive_rules_are_not_to_be_kept():
    pid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    rows = [{"id": f"m-{i}", "content": f"示例规则 {i}", "severity": "soft", "scope": "project", "project_id": pid,
             "user_id": ME, "status": st, "memory_type": "rule", "muted_until": None, "rule_kind": None,
             "rule_payload": None, "created_at": "2026-10-01T00:00:00+00:00", "frequency": 1, "applicability": None,
             "embedding": None} for i, st in enumerate(("confirmed", "candidate"))]
    c = FakeClient(rows={"memories": rows, "projects": [{"id": pid, "name": "示例项目", "brand": "示例牌", "owner_id": ME}]})
    out = core.my_rules(c, pid, user_id=ME)
    assert {r["state"] for r in out["rules"]} == {"生效中", "试用"}
    assert out["state_note"] == core.MY_RULES_STATE_NOTE == (
        "state 是「试用」「已停用」的现在不生效: 写稿、写评论都不用守, 和生效中的规则或 comment_set 说法不一样也不算冲突。"
        "用户说哪条过时了, 当场用 set_rule_state 停掉, 不用等负责人。")
    assert "retire(降回试用档, 不再进简报、写评论也不再给)" in out["actions"]


def test_tool_docs_carry_the_same_wording():
    cf, sc = tools.comments_for.__doc__, tools.score_comments.__doc__
    assert "和 style、comment_set 说法不一样的以那两样为准" in cf
    assert "说每篇都要带品牌的, 评论里几条提品牌照 comment_set" in cf
    assert "``rules_note``" in cf and "不用等负责人" in cf
    assert "不提帖子(帖子、这帖、本帖、原帖、帖里都不写, 直接说那件事)" in cf and "不写「帖子里」" not in cf
    assert "不为凑字数加话" in cf and "常识别用「原来」「才知道」「没想到」去接" in cf
    assert "不算冲突, 不用问用户, 也不用报给负责人" in cf
    assert "``set_next_step``" in sc and "不用问用户, 也不用报给负责人" in sc and "是这个项目定的配比" in sc
    assert "用户这次明说要别的配比, 这一组就照用户的写" in sc and "前面对话里见过的规则原文" in sc
    assert "用户这次明说要别的配比(比如每条都带品牌)" in cf and "项目的数不动" in cf
    for doc in (cf, sc):                                  # 11-11 晚: 配比开放了, 两条路分开说; 原来那句「改不了」不留
        assert "改不了。" not in doc and "对话里改不了" not in doc
        assert "用户明说这个项目以后都按新的配比, 调" in doc and "set_comment_mix" in doc
        assert "只有用户明说才改" in doc
    assert "整组 ``passed=false``(中性、短评不够)时这组还没过" in sc and "这就是整组的分数" not in sc
    assert "不为凑字数加话" in sc and "「帖子里」" not in sc
    assert "写评论也不给" in tools.my_rules.__doc__ and "不用等负责人" in tools.my_rules.__doc__
    assert "不用等负责人" in tools.set_rule_state.__doc__


NEW_TEXTS = {
    "SET_LINE_HEAD": core.SET_LINE_HEAD, "SET_NEXT_STEP": core.SET_NEXT_STEP, "RULE_SELF_SERVICE": core.RULE_SELF_SERVICE,
    "SET_LINE_HEAD_ALL": core.SET_LINE_HEAD_ALL, "SET_BRAND_STEP": core.SET_BRAND_STEP, "SET_SHORT_STEP": core.SET_SHORT_STEP,
    "USER_MIX_NOTE": core.USER_MIX_NOTE, "LENGTH_RULE_TAIL_HARD": core.LENGTH_RULE_TAIL_HARD,
    "COMMENT_SET_HEAD": core.COMMENT_SET_HEAD, "COMMENTS_FOR_RULES_NOW": core.COMMENTS_FOR_RULES_NOW,
    "COMMENT_SET_NEXT_STEP": core.COMMENT_SET_NEXT_STEP, "MY_RULES_STATE_NOTE": core.MY_RULES_STATE_NOTE,
    "COMMENT_SCORE_NOTE": core.COMMENT_SCORE_NOTE, "POST_REF_NOTE": core.POST_REF_NOTE, "DETAIL_NOTE": core.DETAIL_NOTE,
    "LENGTH_RULE_TAIL": core.LENGTH_RULE_TAIL, "BELOW_MIN_HOW": rulecheck.BELOW_MIN_HOW,
    "PAD_LINE": cs.PAD_LINE, "COMMON_LINE": cs.COMMON_LINE, "SHORT": cs.SHORT,
}


@pytest.mark.parametrize("name", sorted(NEW_TEXTS))
def test_new_texts_are_writer_safe(name):
    """念给模型、可能转给写手的话: 没有工程指令, 不叫写手归档, 不说是写手的错, 不叫写手去反馈。"""
    text = NEW_TEXTS[name]
    bad = [f for f in FORBIDDEN + ("归档", "archive", "写错", "你的错", "反馈给", "报告负责人") if f in text]
    assert not bad, (name, bad)


# ── 5. 笔记那一侧的字数 ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, hit", [
    ("正文300-500字", True), ("标题不超过20字", True), ("正文三百到五百字", True), ("五十字以内", True),
    ("字数控制在八百以内", True), ("篇幅别太长", True), ("２０字", True),
    ("品牌名一字不改", False), ("别写十字路口", False), ("不提八字", False), ("标题用英文字母", False),
    ("不用繁体字", False), ("20字幕", False), ("结尾带上售后那一句", False),
    # 复审 11-11: 一个中文数字 + 个字 是在说原样用, 挨着说长短的词才算字数
    ("开头两个字别一样", False), ("标题里品牌名一个字都不能错", False), ("一个字也别改", False),
    ("品牌名四个字不拆开", False), ("这十个字要原样保留", False), ("slogan 八个字原样用", False), ("不提八字内容", False),
    ("不超过八个字", True), ("标题最多十个字", True), ("十字以内", True), ("标题十个字内", True),
])
def test_length_rule_detection(text, hit):
    assert bool(core._LENGTH_RULE.search(text)) is hit


def test_a_hard_length_rule_gets_the_tail_once_in_p0():
    rows = [_rule(1, content="正文不少于三百字", severity="hard"), _rule(2, content="篇幅别太长", severity="soft")]
    out = core.build_writing_brief(desk_client(memories=rows), PROJ, user_id=ME)
    assert out["p0"].endswith("\n" + core.LENGTH_RULE_TAIL_HARD) and core.LENGTH_RULE_TAIL_HARD not in out["p1"]
    assert core.LENGTH_RULE_TAIL not in out["p0"] + out["p1"], "P0 是硬约束: 不说「素材不够就写短一点」"
    assert core.LENGTH_RULE_TAIL_HARD == ("（规则里说到的字数照守; 差字数时只拿具体内容(经过、细节、数字)补, "
                                          "不加空话、重复的解释或总结句。）")
    assert "写短" not in core.LENGTH_RULE_TAIL_HARD and "差多少" not in core.LENGTH_RULE_TAIL_HARD
    assert core.LENGTH_RULE_TAIL == ("（规则里说到的字数, 上限照守; 但字数不是要凑够的数: 不为凑字数加空话、重复的解释或总结句, "
                                     "素材不够就写短一点, 交付时跟用户说一句差多少。）")


def test_a_soft_length_rule_gets_the_tail_in_p1():
    rows = [_rule(1, content="不能承诺效果", severity="hard"), _rule(2, content="正文三百到五百字", severity="soft")]
    out = core.build_writing_brief(desk_client(memories=rows), PROJ, user_id=ME)
    assert out["p1"].count(core.LENGTH_RULE_TAIL) == 1 and core.LENGTH_RULE_TAIL not in out["p0"]


def test_a_stored_length_check_counts_even_without_a_number_in_the_text():
    rows = [_rule(1, "max_len", {"min": 300, "where": "body"}, content="正文别太短", severity="hard")]
    out = core.build_writing_brief(desk_client(memories=rows), PROJ, user_id=ME)
    assert core.LENGTH_RULE_TAIL_HARD in out["p0"]


def test_comment_rules_do_not_add_the_note_side_length_tail():
    rows = [_rule(1, content="评论区：每条不超过三十字", severity="hard"),
            _rule(2, content="评论区：评论 20 字以内", severity="soft"), _rule(3, content="不能承诺效果", severity="hard")]
    out = core.build_writing_brief(desk_client(memories=rows), PROJ, user_id=ME)
    for tail in (core.LENGTH_RULE_TAIL_HARD, core.LENGTH_RULE_TAIL):
        assert tail not in out["p0"] + out["p1"], "管评论的规则说到字数, 不给笔记补那一句"


def test_no_length_rule_no_tail():
    rows = [_rule(1, content="不能承诺效果", severity="hard"), _rule(2, content="结尾留白", severity="soft")]
    out = core.build_writing_brief(desk_client(memories=rows), PROJ, user_id=ME)
    assert core.LENGTH_RULE_TAIL not in out["p0"] + out["p1"]


def test_the_length_tail_goes_before_the_direction_tail():
    rows = [_rule(1, content="标题不超过二十字", severity="hard"),
            _rule(2, content="品牌名不进正文", severity="hard", applicability="流量向")]
    p0 = core.build_writing_brief(desk_client(memories=rows), PROJ, user_id=ME)["p0"]
    assert p0.endswith(core.LENGTH_RULE_TAIL_HARD + "\n" + core.DIRECTION_RULE_TAIL)


def test_below_min_hit_says_to_add_substance_not_filler():
    rule = _rule(1, "max_len", {"min": 10, "max": 40, "where": "body"}, severity="hard")
    (hit,) = rulecheck.check([rule], [{"title": "", "body": "一二三"}])[0]
    assert hit["what"] == ("必须改: 正文 3 字(不算空白和话题), 下限 10: 能补的是具体内容(经过、细节、数字), "
                           "不加空话凑字; 没有素材就照实告诉用户差多少")
    (over,) = rulecheck.check([rule], [{"title": "", "body": "一" * 41}])[0]
    assert over["what"] == "必须改: 正文 41 字(不算空白和话题), 上限 40", "上限那句不动"
