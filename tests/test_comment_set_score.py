"""单独写评论那条路上的一组配比、重复、交付那一格和人工核实全文(2026-11-10, core.score_comments / comments_for)。

钉住:
  1. 项目配了 comment_set(假列名「测试列」): 提品牌超出的按顺序留前面、后面的 style_fix; 中性不够、短评不够整组 passed=false,
     thread_fix 第一句是「整组代码查」; max_named 不转给 Jev; delivery_column 一行一条、贴几行就是几条;
  2. 判定没判成时品牌 / 重复 / 短评照样回, 中性那一句说这次没数;
  3. 没配 comment_set 的项目只多「帖子里」、含糊指代和重复检查, 别的照旧; 配错了按没配处理、回一句;
  4. 帖子那一格里已有的维护评论: comments_for 放进 existing_comments(带 source), score_comments 拿来查重复; 读不到只提示;
  5. earlier_comments 只拿来查重复, 不送 Jev, 参数不对 400;
  6. 要人核实的那几条带全文和疑点, 并列进 review_list, 不截断;
  7. 跟着笔记交付的评论也查「帖子里」和含糊指代(delivery.problems)。
全部是合成文字、合成品牌和假列名, 不引用真实评论、真实规则或真实列名。
"""
from __future__ import annotations

import json

import pytest

from deskcore import commentstyle as cs
from deskcore import core, delivery, store, tools
from tests.test_deskcore_judge_tools import _Resp, wired  # noqa: F401 — fixture
from tests.test_deskcore_score_and_comments import LINK, ME, NOTE, _tv_client

BRAND = "示例牌"
COL = "测试列"
CFG = {"brand_max": 0.5, "neutral_min": 0.3, "short_min": 0.2, "short_chars": 15, "column": COL}

# 12 条提品牌(品牌在前面、收尾是事实, 不算品牌收尾) + 8 条不提品牌; 互相不重复, 开头各不相同, 没有句号、指代
BRANDED = [
    "示例牌的老师看过以后，第二周就约到了两场面试",
    "在示例牌上了三节课，作品集终于不像流水账",
    "朋友推荐的示例牌，我先试听了一节再决定",
    "跟示例牌聊完心里有底了，至少知道先改哪一页",
    "示例牌排的计划挺细，每周三交一次作业",
    "当时犹豫要不要报示例牌，后来还是先自己试了两个月",
    "听同事说示例牌收费不低，想问问具体怎么算",
    "示例牌给的面试题库我刷了一半，剩下的周末补",
    "上个月刚在示例牌结课，现在每天投五份",
    "示例牌带课的老师讲行业很透，三个小时没看手机",
    "表姐也报过示例牌，她说重点是自己肯练",
    "问了示例牌的顾问，她让我先把简历发过去",
]
PLAIN = [
    "蹲一个后续", "同问价格", "求一份书单", "作品集一般要放几个项目呀",
    "我自己是先投小公司练手的，面了七八家才找到感觉", "有没有人试过零基础转行的",
    "我们组去年招人主要看实习经历", "周末的线下分享会还有名额吗",
]


def _client(cfg=CFG, cell=None, brand=BRAND, comments=None):
    note = dict(NOTE, raw_extra=({COL: cell} if cell is not None else {}))
    c = _tv_client(notes=[note], comments=comments)
    c.rows["projects"][0].update(name="示例项目", brand=brand,
                                 default_params=json.dumps({"comment_set": cfg}) if cfg is not None else "{}")
    return c


def _view(act, *, review=False, ambiguous=(), flags=()):
    return {"passed": not review, "needs_review": review, "profile": {"speech_act": act, "echoes_post": "否"},
            "flags": list(flags), "hard_fails": [], "ambiguous": list(ambiguous), "plan": [], "hints": [],
            "advisory": ["register", "reader_value", "arranged", "comment_intent", "is_scripted"], "calls": 2}


def _judge(views, **thread):
    body = {"passed": all(v["passed"] for v in views), "needs_review": any(v["needs_review"] for v in views),
            "hard_fails": [], "unjudged": [], "profile": {}, "code": {}, "ambiguous": [], "advisory": [], "calls": 9,
            "policy": {}, "plan": [], "hints": [], "comments": views}
    body.update(thread)
    return lambda m, u, kw: _Resp(200, body)


def _acts(texts, act_for_plain="提问"):
    return [_view("旁观推荐" if BRAND in t else act_for_plain) for t in texts]


def _sent(calls):
    (_m, _url, _h, body, _t), = calls
    return body


def _score(c, texts, **kw):
    return core.score_comments(c, user_id=ME, link=LINK, comments=[{"text": t} for t in texts], **kw)


# ── 1. 配了 comment_set ─────────────────────────────────────────────────────────────────────────

def test_twelve_of_twenty_branded_fails_the_last_two(wired):
    calls, reply = wired
    texts = BRANDED + PLAIN
    reply["fn"] = _judge(_acts(texts))
    out = _score(_client(), texts)
    rows = out["comments"]
    flagged = [r["index"] for r in rows if r.get("style_fix")]
    assert flagged == [10, 11], "按顺序留前 10 条"
    for i in flagged:
        assert rows[i]["passed"] is False and "去掉品牌" in rows[i]["style_fix"][0]
        assert len(rows[i]["style_fix"]) == 1, "要去掉品牌的那条不再叫它把品牌往前挪"
    assert out["passed"] is False and out["summary"]["passed"] == 18
    assert out["thread_fix"][0].startswith(core.SET_LINE_HEAD + "提品牌的 12 条, 最多 10 条")
    assert "第 11 条、第 12 条" in out["thread_fix"][0]
    assert _sent(calls)["comments"][0]["text"] == texts[0] and len(_sent(calls)["comments"]) == 20


def test_ten_of_twenty_branded_and_enough_neutral_passes(wired):
    _, reply = wired
    texts = BRANDED[:10] + PLAIN + ["我是看了三遍回放才搞懂排期的", "做设计的转产品难不难"]
    reply["fn"] = _judge(_acts(texts))
    out = _score(_client(), texts)
    assert out["passed"] is True and "thread_fix" not in out and "style_next_step" not in out


def test_too_few_neutral_fails_the_set_and_the_code_line_comes_first(wired):
    _, reply = wired
    texts = BRANDED[:10] + PLAIN + ["我是看了三遍回放才搞懂排期的", "做设计的转产品难不难"]
    views = _acts(texts)
    for i in range(10, 17):                       # 10 条不提品牌的里 7 条判成旁观推荐: 只剩 3 条中性, 至少要 6 条
        views[i] = _view("旁观推荐")
    reply["fn"] = _judge(views, plan=[{"slot": None, "why": f"判定甲{k}"} for k in range(3)])
    out = _score(_client(), texts)
    assert all(r["passed"] for r in out["comments"]), "每条都过了, 是整组不够"
    assert out["passed"] is False
    assert out["thread_fix"][0].startswith(core.SET_LINE_HEAD + "中性的") and "3 条, 至少 6 条" in out["thread_fix"][0]
    assert "第 17 条、第 16 条、第 15 条" in out["thread_fix"][0], "从后往前挑不中性的"
    assert len(out["thread_fix"]) == core.SCORE_MAX_FIXES, "代码那一句挤不掉"


def test_too_few_short_comments_fails_the_set(wired):
    _, reply = wired
    texts = [BRANDED[0], BRANDED[1], "我自己是先投小公司练手的，面了七八家才找到感觉",
             "我们组去年招人主要看实习经历和作品", "作品集一般要放几个项目才够看大家知道吗"]
    reply["fn"] = _judge(_acts(texts, act_for_plain="补充经验"))
    out = _score(_client(), texts)
    assert out["passed"] is False and all(r["passed"] for r in out["comments"])
    line = out["thread_fix"][0]
    assert "15 字以内的一句话短评 0 条, 至少 1 条" in line and "第 5 条" in line, "问句先改成一句话"


def test_max_named_is_not_forwarded_when_the_project_counts_brands(wired):
    calls, reply = wired
    texts = PLAIN[:4]
    reply["fn"] = _judge(_acts(texts))
    out = _score(_client(), texts, max_named=1)
    assert _sent(calls)["max_named"] is None and "max_named" in out["max_named_note"]


def test_delivery_column_is_one_comment_per_line(wired):
    _, reply = wired
    texts = [PLAIN[0], "第一行\n第二行 接着说", PLAIN[1]]
    reply["fn"] = _judge(_acts(texts))
    out = _score(_client(), texts)
    dc = out["delivery_column"]
    assert dc["column"] == COL and dc["text"] == "蹲一个后续\n第一行 第二行 接着说\n同问价格" and dc["lines"] == 3
    assert dc["text"].count("\n") + 1 == len(texts), "贴几行就是发了几条"
    step = out["delivery_column_next_step"]
    assert f"「{COL}」" in step and "不要覆盖" in step and "另起一行" in step and "列名不要贴进格子" in step
    assert "贴几行就算发了几条" in step
    assert "一段接一段" in step and "earlier_comments" in step, "一批分几次判的, 每段接起来交, 不是只交最后一段"


def test_a_brand_excess_comment_is_not_also_told_to_move_its_brand_ending(wired):
    _, reply = wired
    texts = ["示例牌的课我上过两次，作业量挺大", "朋友推荐的，最后选了示例牌", "蹲一个后续", "比来比去还是示例牌"]
    assert cs.brand_endings(texts, cs.brand_aliases(BRAND))["excess"] == [3], "不先去掉的话, 第 4 条两样都要改"
    reply["fn"] = _judge(_acts(texts))
    out = _score(_client(cfg={"brand_max": 0.5}), texts)
    rows = out["comments"]
    assert [r["index"] for r in rows if r.get("style_fix")] == [3]
    assert len(rows[3]["style_fix"]) == 1 and "去掉品牌" in rows[3]["style_fix"][0]
    assert "以品牌评价收尾" not in out["thread_fix"][0], "去掉品牌的那条不算进品牌收尾, 剩下 1 条没超"


def test_judge_down_still_reports_brand_repeats_and_short_and_says_neutral_was_not_counted(wired):
    _, reply = wired
    reply["fn"] = lambda m, u, kw: _Resp(503, {"detail": "评论判定服务未开启"})
    texts = BRANDED + ["我也是上个月刚在示例牌结课的"] + PLAIN[4:7]
    out = _score(_client(), texts)
    assert out["judge_status"] != "ok" and "passed" not in out
    flagged = {r["index"]: r["style_fix"] for r in out["style_issues"]}
    assert set(flagged) == {8, 9, 10, 11, 12}, "16 条里 13 条提品牌, 最多 8 条"
    assert any("和第 9 条重复" in x for x in flagged[12])
    line = out["thread_fix"][0]
    assert line.startswith("整组代码查") and "中性的条数这次没数" in line and "一句话短评 3 条, 至少 4 条" in line
    assert out["delivery_column"]["lines"] == 16


# ── 3. 没配 / 配错 ──────────────────────────────────────────────────────────────────────────────

def test_without_comment_set_only_the_global_checks_apply(wired):
    calls, reply = wired
    texts = BRANDED[:6] + ["帖子里说的书单能发一下吗", "社团那栏写的什么呀"]
    reply["fn"] = _judge(_acts(texts))
    out = _score(_client(cfg=None), texts, max_named=2)
    assert _sent(calls)["max_named"] == 2, "没配就照旧转给 Jev"
    assert "delivery_column" not in out and "comment_set_note" not in out
    rows = {r["index"]: r for r in out["comments"]}
    assert [i for i, r in rows.items() if r.get("style_fix")] == [6, 7], "6/8 提品牌不数"
    assert "「帖子里」" in rows[6]["style_fix"][0] and "「社团那栏」" in rows[7]["style_fix"][0]


@pytest.mark.parametrize("cfg, why", [
    ({"brand_max": 1.5}, "brand_max"), ({"brand_max": 0.8, "neutral_min": 0.3}, "不能超过 1"),
    ({"short_chars": 3, "short_min": 0.2}, "short_chars"), ({"column": "a|b"}, "column"),
    ({"colum": "x"}, "不认识的键"), ({}, "非空"), ("half", "非空"), ({"brand_max": True}, "brand_max"),
])
def test_a_bad_comment_set_is_ignored_with_a_note(wired, cfg, why):
    calls, reply = wired
    texts = BRANDED[:4]
    reply["fn"] = _judge(_acts(texts))
    out = _score(_client(cfg=cfg), texts, max_named=1)
    assert why in out["comment_set_note"] and "按没配处理" in out["comment_set_note"]
    assert _sent(calls)["max_named"] == 1 and "delivery_column" not in out
    assert not any(r.get("style_fix") for r in out["comments"]), "4/4 提品牌, 没配就不数"


def test_short_chars_defaults_and_shares_are_optional():
    cfg = core._validate_comment_set({"short_min": 0.2})
    assert cfg == {"brand_max": None, "neutral_min": None, "short_min": 0.2, "short_chars": 15, "column": None}
    assert core._comment_set({"default_params": "not json"}) == (None, None)


# ── 4. 帖子那一格里已有的维护评论 ───────────────────────────────────────────────────────────────

CELL = "1. 上周三晚上八点开的直播我也蹲了\n\n2、同问价格\n  第三条在这里  \n"


def test_comments_for_reads_the_cell_as_existing_comments():
    out = core.comments_for(_client(cell=CELL), user_id=ME, link=LINK)
    cell = [x for x in out["existing_comments"] if x.get("source") == COL]
    assert [x["text"] for x in cell] == ["上周三晚上八点开的直播我也蹲了", "同问价格", "第三条在这里"], "编号、空行去掉"
    assert [x["text"] for x in out["existing_comments"] if "source" not in x] == ["那后来怎么改的", "可以看看简历第一行"]
    assert out["comment_set"]["column"] == COL and len(out["comment_set"]["lines"]) == 5
    assert out["comment_set"]["lines"][0] == core.COMMENT_SET_HEAD, "第一句说这些数是负责人给这个项目定的"
    assert "20 条最多 10 条" in out["comment_set"]["lines"][1] and "20 条至少 6 条" in out["comment_set"]["lines"][2]
    assert "20 条至少 4 条" in out["comment_set"]["lines"][3]
    assert f"source 是「{COL}」" in out["next_step"] and "comment_set.lines" in out["next_step"]
    assert out["style"] == list(core.COMMENT_STYLE_LINES)
    assert "existing_comments_note" not in out, "那一格的行不算进 TV 评论数的比较"


def test_cell_lines_are_capped_and_only_real_numbering_is_stripped():
    lines = core._cell_lines("\n".join(f"第{k}条随便写写" for k in range(60)))
    assert len(lines) == core.CELL_LINES_MAX == 50 and lines[0] == "第0条随便写写"
    assert core._cell_lines("长" * 300) == ["长" * core.CELL_LINE_CHARS]
    assert core._cell_lines("1. 甲\n（2）乙\n3、丙\n10:30 那场直播\n3.5 分也算高了") == [
        "甲", "乙", "丙", "10:30 那场直播", "3.5 分也算高了"], "后面紧跟数字的是时间、分数, 不是编号"


def test_the_cell_is_read_when_raw_extra_is_a_json_string():
    c = _client()
    c._tv.rows["notes"][0]["raw_extra"] = json.dumps({COL: "存成字符串的那一格"}, ensure_ascii=False)
    assert store.tv_note_extra_text(c, NOTE["note_id"], COL) == "存成字符串的那一格"


def test_the_cell_is_read_under_undeclared_too():
    c = _client()
    c._tv.rows["notes"][0]["raw_extra"] = {"_undeclared": {COL: "藏在没声明的列里"}}
    assert store.tv_note_extra_text(c, NOTE["note_id"], COL) == "藏在没声明的列里"
    assert store.tv_note_extra_text(c, NOTE["note_id"], "别的列") == ""


def test_comments_for_without_a_column_does_not_read_the_cell():
    c = _client(cfg=None, cell=CELL)
    out = core.comments_for(c, user_id=ME, link=LINK)
    assert all("source" not in x for x in out["existing_comments"]) and "comment_set" not in out
    assert not any("raw_extra" in (x["cols"] or "") for x in c._tv.calls)


def test_an_unreadable_cell_does_not_block_comments_for(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("db down")
    monkeypatch.setattr(store, "tv_note_extra_text", boom)
    out = core.comments_for(_client(cell=CELL), user_id=ME, link=LINK)
    assert out["resolved"] is True and "没读到" in out["existing_comments_unread_note"]


def test_score_comments_checks_repeats_against_the_cell(wired):
    _, reply = wired
    texts = ["我是上周三晚上八点开始看的", PLAIN[0]]
    reply["fn"] = _judge(_acts(texts))
    out = _score(_client(cell=CELL), texts)
    assert "和帖子下已有的评论重复" in out["comments"][0]["style_fix"][0]


def test_score_comments_says_when_existing_comments_were_not_read(wired, monkeypatch):
    _, reply = wired
    monkeypatch.setattr(store, "tv_note_comments", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    reply["fn"] = _judge(_acts(PLAIN[:2]))
    out = _score(_client(), PLAIN[:2])
    assert out["judge_status"] == "ok" and core.EXISTING_UNREAD_HINT in out["thread_hints"]


# ── 5. earlier_comments ─────────────────────────────────────────────────────────────────────────

def test_earlier_comments_are_only_a_reference(wired):
    calls, reply = wired
    texts = ["我也是上周三晚上八点开始看的", PLAIN[1]]
    reply["fn"] = _judge(_acts(texts))
    out = _score(_client(), texts, earlier_comments=["上周三晚上八点开的直播我也蹲了", {"text": "求一份书单"}])
    assert "和之前送判的第 1 条重复" in out["comments"][0]["style_fix"][0]
    assert [c["text"] for c in _sent(calls)["comments"]] == texts, "前几次的不送去判"


@pytest.mark.parametrize("earlier, why", [("abc", "一组"), (["x"] * 61, "最多 60"), ([3], "第 1 条")])
def test_bad_earlier_comments_are_a_400(wired, monkeypatch, earlier, why):
    calls, _ = wired
    c = _client()
    monkeypatch.setattr(core, "sb", lambda: c)
    with pytest.raises(tools.InvalidInput) as e:
        tools.score_comments([{"text": "同问"}], link=LINK, earlier_comments=earlier, _user_id=ME)
    assert why in str(e.value) and calls == []


# ── 6. 人工核实的全文 ───────────────────────────────────────────────────────────────────────────

LONG = ("去年秋招我从九月投到十二月，一共投了八十多家，前两个月几乎全挂在简历关，"
        "后来把实习经历按项目重新拆开写，每一段都补上了具体做了什么、带来了什么变化，第三个月才陆续有面试")


def test_needs_review_rows_carry_the_full_text_and_the_doubt(wired):
    _, reply = wired
    texts = [PLAIN[0], LONG, PLAIN[1]]
    views = [_view("提问"), _view("亲历背书", review=True, ambiguous=["speech_act", "register"]),
             _view("提问", review=True, flags=["漏答"])]
    reply["fn"] = _judge(views)
    out = _score(_client(cfg=None), texts)
    rows = out["comments"]
    assert "text" not in rows[0], "不用核实的那条不带全文"
    assert rows[1]["text"] == LONG and len(rows[1]["text"]) == len(LONG), "不截断"
    assert rows[1]["review_why"] == "判定拿不准: 主要行为(现在判「亲历背书」)", "只提示的题不算疑点"
    assert rows[2]["review_why"] == "有门槛题判定没答出来"
    assert out["review_list"] == [{"label": "第 2 条", "text": LONG, "why": rows[1]["review_why"]},
                                  {"label": "第 3 条", "text": PLAIN[1], "why": rows[2]["review_why"]}]
    assert "全文" in out["review_next_step"] and "不缩写" in out["review_next_step"]
    assert "…" not in json.dumps(out["review_list"], ensure_ascii=False)


def test_review_labels_use_slots(wired):
    _, reply = wired
    reply["fn"] = _judge([_view("提问"), _view("回复答疑", review=True)])
    out = core.score_comments(_client(cfg=None), user_id=ME, link=LINK, comments=[
        {"slot": "q1", "text": "作品集一般放几个项目"}, {"slot": "reply", "text": "三到五个就够"}])
    assert out["review_list"] == [{"label": "位 reply", "text": "三到五个就够", "why": ""}]


def test_the_tool_docs_ask_for_full_text():
    doc = tools.score_comments.__doc__
    assert "review_list" in doc and "全文" in doc and "不缩写" in doc and "delivery_column" in doc
    assert "earlier_comments" in doc and "整组代码查" in doc
    assert "全文" in core.COMMENT_SCORE_NOTE and "不缩写" in core.COMMENT_SCORE_NOTE
    assert "comment_set" in tools.comments_for.__doc__ and "不提帖子" in tools.comments_for.__doc__


def test_jev_thread_lines_drop_the_post_ref(wired):
    _, reply = wired
    reply["fn"] = _judge([_view("提问"), _view("提问")], plan=[{"slot": "q1", "why": "要接住帖子里具体说到的事"}])
    out = _score(_client(cfg=None), PLAIN[:2])
    assert out["thread_fix"] == ["位 q1: 要接住帖子具体说到的事" + core.POST_REF_NOTE]


# ── 7. 跟着笔记交付的评论 ───────────────────────────────────────────────────────────────────────

def test_delivery_flags_post_ref_and_vague_pointers():
    tpl = {"layout": "blocks", "comments": {"min": 1, "max": 3, "label_reader": "读者", "label_author": "作者"}}
    draft = {"title": "甲", "body": "正文", "comments": [{"role": "reader", "text": "帖子里那张表能分享吗"},
                                                       {"role": "author", "text": "社团那栏写的是兴趣爱好"}]}
    problems = [p["what"] for p in delivery.assemble(tpl, [draft])["problems"]]
    assert any(p.startswith("第 1 条评论: 去掉「帖子里」") for p in problems)
    assert any(p.startswith("第 2 条评论: 「社团那栏」") for p in problems)
    assert cs.SHORT in delivery.requirements(tpl)["task"]
