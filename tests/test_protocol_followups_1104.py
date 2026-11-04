"""2026-11-04 那一次协议升版: W-42 / W-45 / W-50 一起改, 写手只重导一次 skill。

  · W-42: 查出合规问题不再等写手说「第 N 篇按这条改」, 模型自己照修改建议改一轮(和 check_rules 的「必须改」同一轮),
    交付时一句话说改了哪里; 改过一轮还查出来的照实说、不再接着改。
  · W-45: 发牌改版(aw#140)的协议跟进 —— 开场念 project_now.message; 外部参考卡不说成「数据验证过」;
    卡面的大白话、参考结构那一行、batch_note、pin / repeat_of。skill 头部随这次一起(描述里加了贴型的说法)。
  · W-50: 贴型(aw#141) —— 写手说了贴型才带 layer、照卡上那一行写, 没说就照旧、不问。

这里钉的是协议里【这次定下的口径】和服务端同一个口径的那几句; aw#144 写的评论口径一字不动。
"""

from __future__ import annotations

from deskcore import core, project_now, tools as T

BODY, VERSION = T.protocol_text()
HEAD = T.skill_head()


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i + len(start))]


SEC_OPEN = _between(BODY, "**1. `open_project`**", "**2. `draw_angles`**")
SEC_DRAW = _between(BODY, "**2. `draw_angles`**", "**3. `borrow_lessons`**")
SEC_LESSONS = _between(BODY, "**3. `borrow_lessons`**", "### 二、成稿后")
SEC_SCORE = _between(BODY, "**5. `score_drafts`**", "**5a. `check_rules`**")
SEC_RULES = _between(BODY, "**5a. `check_rules`**", "**5b.")


def test_the_version_moved_and_the_skill_is_in_sync():
    assert VERSION not in ("38270db04ea0", "30daa73b0535"), "这次改了协议, 版本号要跟着变"
    state = T.skill_sync_state()
    assert state["in_sync"] and state["skill_version"] == VERSION


# ── W-45 ① 开场念 project_now.message ─────────────────────────────────────────

def test_opening_reads_project_now_between_the_upgrade_notice_and_the_digest():
    block = _between(SEC_OPEN, "**开场。**", "开场里不再")
    order = [block.index(k) for k in ("`project_name`", "`skill_update`", "`project_now`", "`edit_digest`")]
    assert order == sorted(order), block
    line = next(ln for ln in block.splitlines() if "`project_now`" in ln)
    assert "`message` 原样" in line


def test_server_text_puts_project_now_in_the_opening_too():
    for text in (project_now.PROJECT_NOW_TASK, T.open_project.__doc__):
        assert "skill_update 之后、edit_digest 之前" in text and "不放进开场" not in text


# ── W-45 ② 外部参考卡不说成验证过 ─────────────────────────────────────────────

def test_external_cards_are_not_called_validated():
    assert "返回的是公司自己投放过、数据验证过的笔记提炼出的经验卡" not in BODY
    for must in ("`external: true`", "外部参考卡", "没经过我们的数据验证", "不要说成验证过的经验", "不提它里面的品牌",
                 "不管哪种卡，都只借钩子、结构、手法"):
        assert must in SEC_LESSONS, must
    for doc in (T.open_project.__doc__, T.borrow_lessons.__doc__):
        assert "external=true" in doc and "没" in doc and "验证" in doc


# ── W-45 ③ 卡面 / 参考结构 / batch_note / pin / repeat_of, W-50 贴型 ──────────

def test_draw_section_describes_the_plain_card_and_its_reference_line():
    for must in ("大白话", "内容形式和标题句式照卡写", "用自己的话写出来", "分类名、判据里的说法不搬进标题和正文",
                 "「参考结构」", "只借结构，不抄说法，不提参考帖里的品牌", "本项目的品牌照项目规则写"):
        assert must in SEC_DRAW, must
    # 卡面上协议点名的几样服务端照旧说
    assert "用自己的话" in core.ANGLES_FOOTER and "不要搬进标题和正文" in core.ANGLES_FOOTER
    assert "不提那篇帖子里的品牌" in core._reference_line(
        {"kind": "external", "title_structure": "数字清单", "desc": {"opening_type": "身份自述"}})


def test_layer_is_passed_only_when_the_writer_names_it():
    para = next(c for c in SEC_DRAW.split("\n\n") if "`layer`" in c)
    for must in ("产品贴", "流量贴", "提问贴", "精品贴", "「这篇是X贴」那一行照着写", "没说就不带", "不为这个去问用户"):
        assert must in para, must
    doc = T.draw_angles.__doc__
    assert "只在用户自己说了是哪种贴时传" in doc and "不要为它去问用户" in doc


def test_pin_and_repeat_of_only_on_request_and_batch_note_once_at_delivery():
    assert "`pin` / `repeat_of`" in SEC_DRAW and "平时不传" in SEC_DRAW
    para = next(c for c in SEC_DRAW.split("\n\n") if "`batch_note`" in c)
    assert "交付这一批的那条回复里" in para and "原样说一次" in para and "不放进开场" in para
    assert "`delivered` 少于 `requested`" in para and "别硬凑" in para
    doc = T.draw_angles.__doc__
    assert "batch_note" in doc and "不放进开场" in doc and "只在用户明确要求时用" in doc


def test_skill_head_triggers_on_layer_requests():
    desc = next(ln for ln in HEAD.splitlines() if ln.startswith("description:"))
    assert "写产品贴或流量贴" in desc


# ── W-42 合规问题自己改 ──────────────────────────────────────────────────────

def test_compliance_problems_are_fixed_without_asking_the_writer():
    para = next(c for c in SEC_SCORE.split("\n\n") if "查出合规问题的稿" in c)
    for must in ("自己改，不等用户说", "先照第 5a 条调完 `check_rules`", "拿不准的也改", "「必须改」放在同一轮里改掉",
                 "`replaces_version_id` 先过 `check_drafts`、再 `score_drafts`", "交给用户的是改好的那一版",
                 "一句话说清改了哪几篇的哪里", "改过一轮还查出来的，照实说一句，不再接着改"):
        assert must in para, must
    assert "按这条改" not in BODY
    assert "否则只说有要改的那几篇" not in BODY, "不再只是告诉写手、等他开口"
    must_fix = next(ln for ln in SEC_RULES.splitlines() if ln.startswith("- 以「必须改」开头的"))
    assert "同一轮" in must_fix and "一句话说清改了哪里" in must_fix


def test_server_text_says_the_same():
    for where, text in (("SCORE_NOTE", core.SCORE_NOTE), ("score_drafts", T.score_drafts.__doc__)):
        for must in ("不用等用户说", "拿不准", "也改", "replaces_version_id", "一句话说清改了哪篇的哪里", "不再接着改"):
            assert must in text, (where, must)
        assert "看一眼就行, 不一定要改" not in text and "用户要改哪篇: 按 fixes 改完" not in text, where
    tip = core._check_rules_next_step({"must_fix": 1}, 0)
    assert "参考分查出合规问题的那几篇也在这一轮一起改" in tip and "一句话说清改了哪里" in tip
    assert "参考分查出合规问题的那几篇也在这一轮一起改" in T.check_rules.__doc__


# ── 不动的 ───────────────────────────────────────────────────────────────────

def test_the_comment_policy_from_aw144_is_kept_word_for_word():
    owner = ("评论检查的结果怎么读（什么算通过、哪些必须改、哪些只是提示、哪些要人核实）以工具说明为准。没判成就如实说这次没完成"
             "检查，不能跳过后声称通过。评论可提问、表达感受、比较、推荐和答疑，也可写有素材支持的亲历背书、旁观推荐，不因表达"
             "类型本身禁用或转人工。具体经历、效果和数字依提供素材，不编造事实；没有素材时写提问或一般讨论。需要核实的具体疑点"
             "就照实交给用户，不要靠反复润色把疑点抹掉。")
    assert owner in BODY
