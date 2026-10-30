"""2026-10-30 发布收尾那一次换版的协议口径 (aw#136, desk_synthesis B1-B3 / S1-S12)。

写手说写作台"像没做完": 开场一串通知、每场被问「接着写还是放弃」、「查重没跑全」一直出现、改一句就被自己的旧版打回、
改稿后拿到半张表、AI 找不到项目就自己建一个。服务端在 aw#136 里改了, 协议这一次跟上, 之后冻结两周。这里钉的是
协议里【不许再出现的旧说法】和【这次定下的几条口径】—— 冻结期里有人顺手改协议, 改回旧说法就会红。
"""

from __future__ import annotations

import pytest

from deskcore import tools as T

BODY, _ = T.protocol_text()
HEAD = T.skill_head()


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i + len(start))]


SEC_FIND = _between(BODY, "### 零、", "### 一、")
SEC_OPEN = _between(BODY, "**1. `open_project`**", "**2. `draw_angles`**")
SEC_DRAW = _between(BODY, "**2. `draw_angles`**", "**3. `borrow_lessons`**")
SEC_CHECK = _between(BODY, "**4. `check_drafts`**", "**5. `score_drafts`**")
SEC_SCORE = _between(BODY, "**5. `score_drafts`**", "**5a. `check_rules`**")


@pytest.mark.parametrize("stale", [
    "每周最多一次", "desk_gap", "只换那几行", "别再抽一批新的", "导出", "审核", "人审",
    "只能给自己建", "那把 key", "停下来问用户要对的", "要告诉用户这件事", "工程侧", "backfill",
    "存完把每篇的分数", "先照它的 `task` 做完再写稿", "`commit_drafts`（",
])
def test_stale_wording_is_gone(stale):
    assert stale not in BODY, f"协议里还留着旧说法: {stale}"


def test_header_has_one_upgrade_wording_and_no_export():
    assert "导出" not in HEAD
    assert "版本没核上，按本地这份走" not in HEAD, "版本没核上不是提醒用户的理由"
    assert "重新导入一遍」" not in HEAD, "升级提醒只有一种说法: skill_update.message 原样转"
    assert "skill_update.message" in HEAD and "`skill_version`" in HEAD


def test_header_routes_open_project_failures_like_the_body():
    """复审(on aw#137): 引线头那条原来是「调了、重试一次还是失败 → 没连上」, 一个 403 / 500 也会叫写手去查 mcp.json。
    现在照正文「一、动笔前」第 1 条分三种: 没连上才说那句原话; 403 / 身份认不出不重试、找负责人; 别的报错读不到规则。"""
    bullet = next(ln for ln in HEAD.splitlines() if ln.startswith("- `open_project` 调不了"))
    for must in ("「一、动笔前」", "鉴权失败", "重试一次", "403", "不属于当前调用者", "无法识别调用者身份", "不重试",
                 "负责人", "读不到规则"):
        assert must in bullet, must
    assert "调了、重试一次还是失败" not in HEAD, "所有失败一律当没连上的旧说法"
    assert bullet.index("没连上") < bullet.index("403") < bullet.index("读不到规则")


def test_project_choice_never_creates_on_a_miss():
    """B1: 找不到 → 带 include_archived 再查 → 还没有就找负责人。不建、不问 project_id。"""
    assert "include_archived=true" in SEC_FIND and "负责人" in SEC_FIND
    assert "不要新建" in SEC_FIND and "不要问用户要 `project_id`" in SEC_FIND
    # 共用一个身份, 分不出谁是负责人: 用户说「那你先建一个」也不建; 只有开新品牌 / 新方向才建, 建之前念同品牌的项目名
    assert "也不建" in SEC_FIND and "新品牌 / 新方向开项目" in SEC_FIND and "同品牌的项目名" in SEC_FIND
    assert "开好再写" not in SEC_FIND, "找不到多半是在别的 owner 名下, 要挪不要建"
    forbidden = _between(BODY, "**例外：403", "### 三、")
    assert "负责人" in forbidden and "不要问用户要 `project_id`" in forbidden


def test_opening_block_has_a_fixed_order():
    """S5: 项目名 → skill_update → edit_digest, 然后才是正事。"""
    block = _between(SEC_OPEN, "**开场。**", "开场里不再")
    assert block.index("`project_name`") < block.index("`skill_update`") < block.index("`edit_digest`")
    assert "不挡写稿" in SEC_OPEN and "不要调 `ack_edit_digest`" in SEC_OPEN


def test_angle_debt_is_for_the_model_not_a_question_to_the_writer():
    """B2: 不问「接着写还是放弃」—— 这句只能以禁令出现。"""
    assert "angle_debt" in SEC_OPEN and "一小时以内" in SEC_OPEN
    for chunk in BODY.split("\n\n"):
        if "接着写还是放弃" in chunk:
            assert "不要问" in chunk, chunk


def test_lessons_timeout_borrows_once_before_drafting():
    """S8"""
    assert "`timeout`" in SEC_OPEN and "`borrow_lessons`" in SEC_OPEN and "只调这一次" in SEC_OPEN


def test_angle_keys_are_copied_from_the_prompt_block():
    """S1"""
    assert "angle_key=" in SEC_DRAW and "原样复制" in SEC_DRAW and "绝不自己编" in SEC_DRAW


def test_revisions_go_through_check_drafts_with_replaces_version_id():
    """S2"""
    assert "`replaces_version_id`" in SEC_CHECK and "照样先过 `check_drafts`" in SEC_CHECK
    assert "`replaces_version_id`" in SEC_SCORE


def test_batches_are_at_most_ten():
    """S9"""
    assert "最多 10 篇" in SEC_CHECK and "最多 10 篇" in SEC_SCORE


def test_decided_by_is_a_list_of_four_signals():
    """S12"""
    assert "`decided_by` 是一个列表" in SEC_CHECK
    for sig in ("opening", "title", "ngram", "contain"):
        assert f"`{sig}`" in SEC_CHECK


def test_system_states_are_one_sentence_and_no_writer_action():
    """B3"""
    for field in ("degraded_note", "empty_history_warning", "history_truncated_warning", "containment_skipped_warning"):
        assert f"`{field}`" in SEC_CHECK
    assert "用户不用做什么" in SEC_CHECK


def test_delivery_is_always_the_whole_text_and_scores_are_not_read_out():
    """S3 / S6 / S4"""
    assert "原样整段" in SEC_SCORE and "原来那一整批" in SEC_SCORE
    assert "「合规检查都过了」" in SEC_SCORE and "都不能说成过了" in SEC_SCORE
    assert "不会进写作台的历史稿" in SEC_CHECK and "不争辩" in SEC_CHECK


# ══════════════════════════════════════════════════════════════════════
# 复审(2026-10-30): 协议和服务端的话说成一样
# ══════════════════════════════════════════════════════════════════════
#
# 模型在运行时同时读协议、工具说明、返回里的 task / next_step / note 和 MCP instructions。原来协议说「两边对不上照工具
# 说明做」, 而好几处服务端的话还停在旧口径(「放在最前面 …… 然后才开始写稿」「找不到就 create_project」「global 规则是
# 私有的」…), 于是这次换版的修法在运行时被旧话盖掉。现在协议管「什么时候调、开场怎么排、跟用户说什么、要不要问用户」,
# 服务端的话也改成同一个口径; 下面钉死服务端不许再说回那些旧话 —— 协议冻结的两周里服务端照样能上线, 这里是那道闸。

import ast
from pathlib import Path

from deskcore import core, skillcheck, store

SEC_REJECT = _between(BODY, "- **reject**", "- **pass**")


def test_the_protocol_decides_when_and_what_to_say_tools_decide_fields():
    head = BODY.split("**写手只做三件事", 1)[0]
    assert "以各工具自己的说明为准" in head
    assert "两边看起来对不上时，照工具说明做" not in BODY
    for what in ("什么时候调哪个工具", "开场怎么排", "跟用户说什么", "要不要问用户", "照本协议做"):
        assert what in head, what


def test_reject_has_three_exceptions_that_are_not_rewrites():
    for must in ("`replace_lookup`", "`empty_body`", "改好参数再调一次", "自己交付过的旧版", "补上 `replaces_version_id`",
                 "`delivery.text` 后面单独给", "`delivery_requirements`"):
        assert must in SEC_REJECT, must
    assert "用户坚持也可以放行" not in SEC_CHECK, "warn 不交给写手判"
    assert "还是 warn 就照常交、照常 `score_drafts`" in SEC_CHECK


def test_batches_go_in_order_and_are_not_mentioned():
    assert "前一组存完再查下一组" in SEC_CHECK and "不用跟用户提" in SEC_CHECK


def test_digest_ack_happens_before_the_reply_that_lists_the_edits():
    for must in ("交付那条回复的末尾", "发出这条回复之前", "`rules_recorded=0`", "不等用户回话", "`through` 不变"):
        assert must in SEC_OPEN, must


def test_a_lost_digest_through_is_fetched_again_not_guessed():
    """复审(on aw#137): ack 挪到交付那条回复之前, 开场那次 open_project 的结果多半已经离得很远。through 不许猜, 拿不到就
    用同样的参数再调一次 open_project(没 ack 之前每次都带)。服务端也只认摘要给出去的那个(core.ack_edit_digest)。"""
    for must in ("原样抄", "不要猜", "再调一次 `open_project`", "没 ack 之前每次打开都带"):
        assert must in SEC_OPEN, must
    doc = T.ack_edit_digest.__doc__
    assert "不要猜" in doc and "open_project" in doc and "400" in doc
    msg = core.ACK_THROUGH_UNKNOWN
    assert "open_project" in msg and "edit_digest.through" in msg and "不要自己猜" in msg


def test_open_project_failures_are_routed_by_kind():
    for must in ("没连上", "项目打不开", "别的报错", "无法识别调用者身份"):
        assert must in SEC_OPEN, must


def test_lessons_retry_carries_the_arm_and_not_configured_is_not_said():
    assert "`flywheel_arm` 传简报里的" in SEC_OPEN
    assert "`not_configured` 跟用户说一句" not in BODY and "跟用户说一句）" not in BODY


def test_next_step_is_an_instruction_and_partial_delivery_is_named():
    assert "照返回的 `next_step` 说" not in SEC_SCORE and "不是要念给用户的原话" in SEC_SCORE
    assert "`this_call`" in SEC_SCORE and "这是改过的那几篇" in SEC_SCORE


def test_reimport_of_a_delivered_final_is_not_checked_again():
    row = next(ln for ln in BODY.splitlines() if ln.startswith("| 「入库」，给的是这场交付过的"))
    assert "不再查重" in row and "`check_drafts` →" not in row


def test_resend_of_the_table_gives_every_group():
    """复审(on aw#137): 「给我个表」那行原来只说「最新的 delivery.text」—— 分两组交的, 照字面只会再给第二组。"""
    row = next(ln for ln in BODY.splitlines() if ln.startswith("| 「给我个表」"))
    assert "每组各给它最新的那份" in row and "按组的顺序" in row


def test_archive_pending_is_only_about_this_conversations_cards():
    """复审(on aw#137): 共用一个身份, archive_pending 数的是整个团队的牌 —— 只管这场自己用的, 没用牌就当没看到、不提。"""
    para = next(c for c in BODY.split("\n\n") if "`archive_pending`" in c)
    for must in ("团队共用一个身份", "这场对话自己用的牌", "别人手上的", "就当没看到", "不跟用户提"):
        assert must in para, must
    assert "record_rule" not in para


def _app_instructions() -> str:
    src = Path(T.__file__).with_name("app.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.keyword) and node.arg == "instructions":
            return ast.literal_eval(node.value)
    raise AssertionError("app.py 里找不到 MCP instructions")


def _server_texts() -> dict[str, str]:
    texts = {f"tools.{name}": fn.__doc__ or "" for name, (fn, _) in T.TOOLS.items()}
    for const in ("EDIT_DIGEST_TASK", "EDIT_DIGEST_MESSAGE", "ARCHIVE_PENDING_NEXT_STEP", "SCORE_NOTE",
                  "ARCHIVED_STEP_HEAD", "IDENTITY_REFUSED", "PROJECT_REFUSED_NEXT",
                  "DELIVERY_NEXT_STEP", "RULE_CHECKS_NEXT_STEP", "CHECK_RULES_NOTE", "ACK_THROUGH_UNKNOWN",
                  "_CALIB_SYSTEM"):
        texts[f"core.{const}"] = getattr(core, const)
    texts["core._check_rules_next_step(must_fix)"] = core._check_rules_next_step({"must_fix": 1}, 0)
    notice = skillcheck.notice("aaaaaaaaaaaa", "bbbbbbbbbbbb")
    texts["skillcheck.notice.action"] = notice["action"]
    texts["skillcheck.notice.message"] = notice["message"]
    texts["app.instructions"] = _app_instructions()
    return texts


# 协议这次改掉的旧口径。服务端的话里再出现, 就是又和协议打架了。
SERVER_STALE = (
    "才开始写稿", "说完再开始写稿", "开写之前", "回复的最前面", "回复最前面", "放在最前面", "重新导入一下", "提醒用户重新导入",
    "恒为你自己", "那把 key", "别急着 create_project", "global 规则是私有", "个人技艺库", "只对本人生效", "不影响队友",
    "只能标【自己的】", "我能管的", "牌也还挂着没销账", "project_id 是不是传错", "不用导出", "没有导出这一步", "人审",
    "你 check 之后", "要先过 check_drafts 的话", "改完带 replaces_version_id 再调 score_drafts",
    "带 ``replaces_version_id`` 再调一次本工具", "用 score_drafts 带 ``replaces_version_id`` 再存一次",
    # 复审(on aw#137): 改稿重存跳过 check_drafts 的运行时说法(比对时不看 ``)
    "用 score_drafts 带 replaces_version_id 再存一次", "带 replaces_version_id 再调一次",
    # 调校笔记是这个项目大家共用的一份, 不是某一个人的
    "个人写作调校笔记", "同一个人对 AI 初稿", "这个人的语感偏好", "人的偏好会变", "个人风格清掉",
    # ack 就在交付那条回复发出之前, 那时还什么都没说出去
    "说给用户就别调",
)


@pytest.mark.parametrize("stale", SERVER_STALE)
def test_server_text_does_not_contradict_the_protocol(stale):
    # 文档串里是 ``x``、运行时的话里是 x: 去掉 `` 再比, 两种写法都拦得住
    stale = stale.replace("``", "")
    hits = [where for where, text in _server_texts().items() if stale in text.replace("``", "")]
    assert not hits, f"服务端的话里还有和协议打架的旧说法「{stale}」: {hits}"


def test_server_revision_path_goes_through_check_drafts_first():
    for text in (core.ARCHIVE_PENDING_NEXT_STEP, core.SCORE_NOTE, T.score_drafts.__doc__, T.check_rules.__doc__):
        assert "先过 check_drafts" in text
    # 复审(on aw#137): 离动作最近的那几句(score_drafts 的 next_step 后缀、check_rules 的 note / next_step)也一样
    for text in (core.DELIVERY_NEXT_STEP, core.RULE_CHECKS_NEXT_STEP, core.CHECK_RULES_NOTE,
                 core._check_rules_next_step({"must_fix": 1}, 0)):
        assert "replaces_version_id 先过 check_drafts" in text, text
        assert text.index("check_drafts") < text.index("score_drafts"), text


def test_calibration_notes_are_the_projects_not_one_persons(monkeypatch):
    """复审(on aw#137): 精修来自写这个项目的几个人, 笔记是共用的一份 —— 蒸馏口径、改稿备注的标签、空笔记的报错都这么说。"""
    assert "写这个项目的人共用" in core._CALIB_SYSTEM and "以新观察为准" in core._CALIB_SYSTEM
    assert "以动词开头" in core._CALIB_SYSTEM, "其余铁律照旧"
    edit = {"id": "e1", "ai_title": "甲", "ai_body": "原", "my_title": "甲", "my_body": "改", "note": "太长"}
    monkeypatch.setattr(store, "recent_style_edits", lambda *a, **k: [edit])
    monkeypatch.setattr(store, "get_user_calibration", lambda *a, **k: ("", None))
    monkeypatch.setattr(store, "count_pending_distillation", lambda *a, **k: 1)
    task = core.build_distillation_task(None, "p", user_id="u")
    assert task["instruction"] == core._CALIB_SYSTEM and "本人" not in task["edits"] and "太长" in task["edits"]
    monkeypatch.setattr(core, "assert_project_access", lambda *a, **k: None)
    with pytest.raises(ValueError) as exc:
        core.save_my_style(None, "p", "  ", user_id="u")
    assert "这个项目已有的调校笔记" in str(exc.value) and "个人" not in str(exc.value)


def test_upgrade_notice_goes_after_the_project_name():
    for where, text in (("skillcheck", skillcheck.notice("a" * 12, "b" * 12)["action"]),
                        ("app", _app_instructions()), ("open_project", T.open_project.__doc__),
                        ("get_protocol", T.get_protocol.__doc__)):
        assert "项目名之后" in text, where


def test_global_rules_are_described_as_team_wide():
    doc = T.record_rule.__doc__
    assert "所有品都这样" in doc and "所有写手" in doc


def test_identity_refusals_send_the_writer_to_the_owner_without_config_names(monkeypatch):
    calls = [
        lambda: core.assert_project_access(None, "p", user_id=None),
        lambda: core.list_projects(None, user_id=None),
        lambda: core.create_project(None, "x", user_id=None),
        lambda: core.label_example(None, "i", "positive", user_id=None),
    ]
    for call in calls:
        with pytest.raises(PermissionError) as exc:
            call()
        msg = str(exc.value)
        assert "负责人" in msg and "DESKCORE" not in msg, msg


def test_a_403_says_ask_the_owner_and_not_to_try_another_id(monkeypatch):
    monkeypatch.setattr(store, "project_row", lambda client, pid: {"id": pid, "owner_id": "someone-else"})
    with pytest.raises(PermissionError) as exc:
        core.assert_project_access(None, "p", user_id="me")
    msg = str(exc.value)
    assert "不属于当前调用者" in msg and "负责人" in msg and "不要换别的 project_id 试" in msg
    assert "传错" not in msg and "owner_id" not in msg
