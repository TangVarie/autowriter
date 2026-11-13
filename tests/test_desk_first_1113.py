"""写作台排第一, 别的 skill 只当参考 (2026-11-13, docs/deskcore.md §3.32)。

写手常把写作台和平台自带的 skill(小红书运营、文案、排版)一起点。11 月上旬的用量里: 有的项目日查了重没发牌, 有的查了重
没存, 还有一批交出去的是模型自己生成的 HTML 而不是写作台排好的页面。这一版把「谁排第一」写进四个地方: 协议正文靠前的一节、
skill 头(描述 + 什么时候用)、MCP instructions、入口工具回包里的 desk_first; 查重时看得出没发牌的, 回包里多一句。

这里钉的【被禁止的形态】:
  · 协议里没有这一节, 或者它排在流程后面(模型读到时已经照别的 skill 走了);
  · 协议里和 human-writing 的分工另立一节, 和这一节并排成两套说法;
  · skill 描述的开头没说排第一 / 没提别的 skill(有的客户端会截断描述);
  · 只连着工具、没加载 skill 的对话拿不到这条规矩(instructions 和入口回包都要有);
  · desk_first 把简报的键弄丢、弄乱, 或者探针出错时跟着丢;
  · 没发牌的那一句拦了查重、改了判定, 或者在不该说的时候说(单篇、改稿、带了牌的)。
"""

from __future__ import annotations

import pytest

from deskcore import core, skillcheck, tools

ME = "11111111-1111-1111-1111-111111111111"
BODY, _VERSION = tools.protocol_text()
HEAD = tools.skill_head()
SECTION = "## 写作台排第一，别的 skill 只当参考"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    skillcheck.reset()
    monkeypatch.setattr(core, "sb", lambda: object())
    yield
    skillcheck.reset()


# ── A. 协议 ──────────────────────────────────────────────────────────

def _section() -> str:
    start = BODY.index(SECTION)
    return BODY[start:BODY.index("\n## ", start + 1)]


def test_the_dispatch_section_sits_before_the_flow():
    assert BODY.index(SECTION) < BODY.index("## 标准流程"), "这一节要在流程前面"
    assert BODY.index(SECTION) < BODY.index("## 一个对话只写一个项目")


def test_the_dispatch_section_says_who_owns_what_and_who_wins():
    s = _section()
    for owned in ("稿子", "评论", "规矩", "正反例", "补入库"):
        assert owned in s, f"归写作台的那几样里少了「{owned}」"
    assert "不调工具" in s, "别的 skill 说「不要调用工具」也不算数"
    assert "不顶替发牌" in s and "不另走一套流程" in s, "别的 skill 给的是输入, 不是另一套流程"


def test_p0_is_outside_the_ranking_and_nobody_overrides_it():
    """复审 F1/R2: 原来 P0 排在「用户明说的」后面, 读成用户一句话能压过硬约束。"""
    line = next(ln for ln in _section().splitlines() if ln.startswith("- **听谁的**"))
    assert line.index("项目硬约束（P0）谁说都得守") < line.index("→"), "P0 先说死, 不放进先后顺序里"
    chain = line[line.index("其余说法不一样时"):]
    assert "P0" not in chain and "硬约束" not in chain
    order = ["用户自己在对话里打出来的", "本协议", "写作台的简报和卡", "别的 skill"]
    at = [chain.index(x) for x in order]
    assert at == sorted(at), "先后顺序: 用户自己说的 → 协议 → 简报和卡 → 别的 skill"


def test_choosing_another_skill_is_not_the_users_explicit_word():
    """复审 F3: 两个 skill 一起点是最常见的用法; 别的 skill 里的「用户要求…」不能借用户的位置。"""
    s = _section()
    assert "点了别的 skill" in s and "都不算用户说的" in s
    assert "「用户说了也不做」" not in BODY, "别引一句协议里没有的话(复审 R5)"
    assert "开项目、查重、存档" in s and "用户说了也不跳过" in s


def test_delivery_follows_the_tool_reply_and_a_requested_file_copies_it():
    """复审 F2/F7/R4: 不把「链接」和「在对话里交」对着说; 评论也在内; 用户明说要文件时有一条中间的路。"""
    line = next(ln for ln in _section().splitlines() if ln.startswith("- **交付照工具回包**"))
    assert "score_drafts" in line and "score_comments" in line
    assert "页面链接" not in BODY and "没回链接" not in BODY, "链接在 delivery.text 最后一行, 怎么交归工具的 next_step"
    assert "不自己生成 HTML" in line
    assert "用户明说要文件" in line and "不改字、不重排" in line


def test_the_dispatch_section_stays_short():
    """复审 F8/R6: 按字数卡, 不按行数; 「没有工具」那一条删了(「一、动笔前」第 1 条已经有原话), 也不逐次交代用了谁的什么。"""
    s = _section()
    assert len(s) <= 580, f"这一节整场对话都在系统提示里, 不许写长(现在 {len(s)} 字)"
    assert len([ln for ln in s.splitlines() if ln.startswith("- ")]) == 4
    assert "不装作用过写作台" not in s and "用了什么" not in s


def test_the_division_with_writing_skills_is_under_the_same_rule():
    assert "## 语言层交给谁" not in BODY, "和 human-writing 的分工并进这一节, 不另立一节"
    assert "human-writing" in _section() and "seeding-prompt-refiner" in _section()
    assert "## 分工" not in HEAD, "skill 头里那一段分工也并进来了"


# ── B. skill 头 ──────────────────────────────────────────────────────

def test_the_skill_description_puts_priority_first_and_stays_compact():
    desc = next(ln for ln in HEAD.splitlines() if ln.startswith("description:"))
    early = desc[:200]
    assert "排第一" in early and "别的 skill" in early, "有的客户端会截断描述: 排第一要在最前面"
    assert "写笔记" in early and "来几篇" in early, "触发的说法也要落在前 200 个字符里(复审 R6)"
    assert len(desc) <= 530, "描述不许越写越长(改之前是 515)"


def test_the_skill_head_points_to_the_section_instead_of_repeating_it():
    para = next(ln for ln in HEAD.splitlines() if ln.startswith("**和别的 skill 一起"))
    assert "本 skill 排第一" in para and "写作台排第一，别的 skill 只当参考" in para
    assert len(para) <= 90, "正文那一节就在后面, 头里只指过去(复审 F8)"
    assert len(HEAD) <= 2781, "skill 头不比改之前长"


# ── C. MCP instructions ──────────────────────────────────────────────

def test_mcp_instructions_carry_the_priority_rule():
    import deskcore.app as A_
    if A_._mcp is None:
        pytest.skip("mcp SDK 没装")
    text = A_._mcp.instructions or ""
    assert "写作台排第一" in text and "别的 skill" in text and "只当参考" in text
    assert "不自己生成 HTML" in text and "protocol_version" in text
    assert "用户没明说要" in text, "用户明说要文件是可以的(复审 F2)"
    assert "照旧优先" not in text, "不留一句没有边界的「用户优先」(复审 R3)"
    assert "human-writing" not in text and "只点了别的 skill" in text


# ── D. 入口工具的回包 ────────────────────────────────────────────────

def test_open_project_reply_starts_with_desk_first(monkeypatch):
    monkeypatch.setattr(core, "build_writing_brief",
                        lambda client, pid, *, user_id=None, brief=None: {"project_name": "测试", "p0": ["x"], "counts": {}})
    monkeypatch.setattr(core, "edit_digest", lambda *a, **k: None)
    out = tools.open_project("p", skill_version="0123456789ab", _user_id=ME)
    assert list(out)[0] == "desk_first", "简报很长, 这一句要在最前面"
    assert list(out)[1:4] == ["project_name", "p0", "counts"], "简报的键和顺序不动"
    assert "skill_update" in out, "旧版的升级提醒照旧带"
    text = out["desk_first"]
    assert text == tools.DESK_FIRST_POSTS
    for step in ("open_project", "draw_angles", "check_drafts", "score_drafts"):
        assert step in text
    assert "只当参考" in text and "用户没明说要就不自己生成 HTML" in text and "不用念给用户" in text
    assert len(text) < 260, "一两句话, 每次都带"
    again = tools.open_project("p", skill_version=tools.protocol_text()[1], _user_id=ME)
    assert again["desk_first"] == text, "每次都带: 共用一个身份, 分不出是不是同一场对话"


def test_comments_for_reply_starts_with_desk_first(monkeypatch):
    monkeypatch.setattr(core, "comments_for", lambda client, **kw: {"resolved": False, "need": ["post_body"]})
    out = tools.comments_for(link="https://example.invalid/x", _user_id=ME)
    assert list(out) == ["desk_first", "resolved", "need"]
    assert out["desk_first"] == tools.DESK_FIRST_COMMENTS
    assert "comments_for" in out["desk_first"] and "score_comments" in out["desk_first"]
    assert "用户没明说要就不自己生成 HTML" in out["desk_first"]


def test_the_rest_route_keeps_desk_first_as_the_first_key(monkeypatch):
    """复审 REL-3: 上面几条都是直接调函数; 这一条走一次真的 REST 路由, 包装层重排或滤掉键就会红。"""
    pytest.importorskip("fastapi")
    import json
    from fastapi.testclient import TestClient
    import deskcore.app as A_
    monkeypatch.setenv("DESKCORE_KEYS", json.dumps({"test-key": {"user_id": ME, "name": "测试"}}))
    monkeypatch.setattr(core, "comments_for", lambda client, **kw: {"resolved": False, "need": ["post_body"]})
    r = TestClient(A_.app, raise_server_exceptions=False).post(
        "/tool/comments_for", headers={"X-Deskcore-Key": "test-key"}, json={"link": "https://example.invalid/x"})
    assert r.status_code == 200, r.text[:300]
    assert list(r.json()["result"])[:1] == ["desk_first"]


def test_desk_first_never_breaks_a_non_dict_reply():
    assert tools._with_desk_first(None, "x") is None
    assert tools._with_desk_first({"desk_first": "旧", "a": 1}, "新") == {"desk_first": "新", "a": 1}


def test_tool_docs_mention_the_new_fields():
    assert "desk_first" in tools.open_project.__doc__ and "desk_first" in tools.comments_for.__doc__
    assert "只点了别的 skill" in tools.list_projects.__doc__ and "human-writing" not in tools.list_projects.__doc__
    assert "angles_note" in tools.check_drafts.__doc__


# ── E. 查重时看得出没发牌 ────────────────────────────────────────────

def _check(monkeypatch, drafts):
    verdict = {"results": [{"status": "pass"} for _ in drafts], "summary": {"pass": len(drafts), "warn": 0, "reject": 0}}
    monkeypatch.setattr(core, "check_drafts", lambda client, pid, ds, *, user_id=None, record_snapshot=False: dict(verdict))
    monkeypatch.setattr(core, "archive_pending", lambda *a, **k: None)
    return tools.check_drafts("p", drafts, _user_id=ME), verdict


def _draft(i, **extra):
    return {"title": f"标题{i}", "body": f"正文{i}" * 20, **extra}


def test_a_batch_without_any_angle_key_gets_one_sentence_and_is_not_blocked(monkeypatch):
    out, verdict = _check(monkeypatch, [_draft(i) for i in range(3)])
    assert out["angles_note"] == tools.ANGLES_NOT_DRAWN_NOTE.format(n=3)
    note = out["angles_note"]
    assert "draw_angles" in note and "不用跟用户提" in note
    assert "不用因为没发牌重写" in note and "results" in note, "reject / warn 照 results 办, 这一句不给不重写撑腰(复审 F10)"
    assert "照上面" not in note and "多半" not in note, "JSON 里没有「上面」; 先说事实, 不先猜(复审 R8)"
    assert "照常 score_drafts" not in note and "别的对话交付的稿子" in note, \
        "改的是别的对话交付的稿子(拿不到 version_id)也会收到这一句: 不能叫它去存(复审 REL-2)"
    assert len(note) <= 241, "不比原来长"
    assert out["results"] == verdict["results"] and out["summary"] == verdict["summary"], "只多一句, 判定不动"
    assert "next_step" in out, "过了的照常指向 score_drafts"


@pytest.mark.parametrize("drafts", [
    [_draft(0)],                                                             # 用户点题的单篇: 本来就没有牌
    [_draft(0), _draft(1)],                                                  # 不到 3 篇不算批量
    [_draft(0, angle_key="k1"), _draft(1), _draft(2)],                       # 发了牌(漏带的由 score_drafts 的 unattributed 管)
    [_draft(i, replaces_version_id=f"v{i}") for i in range(4)],              # 全是改稿
    [_draft(0, replaces_version_id="v0"), _draft(1), _draft(2)],             # 新稿不到 3 篇
])
def test_the_sentence_stays_quiet_when_it_is_not_certain(monkeypatch, drafts):
    out, _ = _check(monkeypatch, drafts)
    assert "angles_note" not in out


def test_revisions_do_not_hide_an_undrawn_batch(monkeypatch):
    out, _ = _check(monkeypatch, [_draft(0, replaces_version_id="v0")] + [_draft(i) for i in range(1, 4)])
    assert out["angles_note"] == tools.ANGLES_NOT_DRAWN_NOTE.format(n=3)


# ── F. 升级提醒 ──────────────────────────────────────────────────────

def test_the_update_notice_is_three_plain_lines_and_blames_nobody():
    msg = skillcheck.notice("0123456789ab", _VERSION)["message"]
    lines = msg.splitlines()
    assert len(lines) == 3 and lines[1:] == [skillcheck.UPDATE_URL, skillcheck.UPDATE_COMMAND]
    assert "排第一" in lines[0] and "重新导入一次" in lines[0], "这一版的那半句按版本号挂着"
    assert "0123456789ab" in lines[0] and _VERSION in lines[0]
    for blame in ("不是最新版", "请先升级", "你没", "忘了"):
        assert blame not in msg


def test_a_version_without_its_own_sentence_gets_the_neutral_notice():
    """复审 R7: 「变了什么」按版本号记。下一版忘了写, 提醒就只说有新版, 不把这一版的话再说一遍。"""
    assert isinstance(skillcheck.WHATS_NEW, dict) and skillcheck.WHATS_NEW.get(_VERSION)
    first = skillcheck.notice("0123456789ab", "ba9876543210")["message"].splitlines()[0]
    assert first == "写作台 skill 有新版（你手上的是 0123456789ab，最新是 ba9876543210）。重新导入一次就行："


def test_an_old_skill_keeps_working_against_the_new_server(monkeypatch):
    """旧版 skill 对新服务端: 入口工具照常回简报(多的键只是多), get_protocol 发新正文, 不拦任何调用。"""
    monkeypatch.setattr(core, "build_writing_brief",
                        lambda client, pid, *, user_id=None, brief=None: {"project_name": "测试"})
    monkeypatch.setattr(core, "edit_digest", lambda *a, **k: None)
    out = tools.open_project("p", skill_version="2d357c4b6075", _user_id=ME)
    assert out["project_name"] == "测试" and out["skill_update"]["local"] == "2d357c4b6075"
    gp = tools.get_protocol(local_version="2d357c4b6075", _user_id=ME)
    assert gp["up_to_date"] is False and SECTION in gp["protocol"]
    assert _VERSION != "2d357c4b6075", "协议改了, 版本号要跟着变"
