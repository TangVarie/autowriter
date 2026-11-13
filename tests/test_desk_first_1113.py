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
    for owned in ("开项目", "发牌", "查重", "存档", "交付", "评论", "记规矩", "记改稿"):
        assert owned in s, f"归写作台的那几样里少了「{owned}」"
    assert "不顶替发牌" in s and "不另走一套流程" in s, "别的 skill 给的是输入, 不是另一套流程"
    order = ["用户在这场对话里明说的", "项目硬约束（P0", "本协议的流程", "写作台的简报和卡", "别的 skill。"]
    line = next(ln for ln in s.splitlines() if ln.startswith("- **说法不一样时听谁的**"))
    at = [line.index(x) for x in order]
    assert at == sorted(at), "先后顺序: 用户明说的 → P0 → 协议流程 → 简报和卡 → 别的 skill"
    assert "照旧每条必守" in s, "P0 还是硬的"
    assert "不自己生成 HTML" in s and "没回链接就在对话里交" in s
    assert "不装作用过写作台" in s
    assert len([ln for ln in s.splitlines() if ln.strip()]) <= 12, "这一节整场对话都在系统提示里, 不许写长"


def test_the_division_with_writing_skills_is_under_the_same_rule():
    assert "## 语言层交给谁" not in BODY, "和 human-writing 的分工并进这一节, 不另立一节"
    assert "human-writing" in _section() and "seeding-prompt-refiner" in _section()
    assert "## 分工" not in HEAD, "skill 头里那一段分工也并进来了"


# ── B. skill 头 ──────────────────────────────────────────────────────

def test_the_skill_description_puts_priority_first_and_stays_compact():
    desc = next(ln for ln in HEAD.splitlines() if ln.startswith("description:"))
    early = desc[:200]
    assert "排第一" in early and "别的 skill" in early, "有的客户端会截断描述: 排第一要在最前面"
    assert len(desc) <= 640, "描述不许越写越长"
    assert "和别的 skill 一起被点到" in HEAD and "本 skill 排第一" in HEAD


# ── C. MCP instructions ──────────────────────────────────────────────

def test_mcp_instructions_carry_the_priority_rule():
    import deskcore.app as A_
    if A_._mcp is None:
        pytest.skip("mcp SDK 没装")
    text = A_._mcp.instructions or ""
    assert "写作台排第一" in text and "别的 skill" in text and "只当参考" in text
    assert "不自己生成 HTML" in text and "protocol_version" in text


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
    assert "只当参考" in text and "不自己生成 HTML" in text and "不用念给用户" in text
    assert len(text) < 260, "一两句话, 每次都带"
    again = tools.open_project("p", skill_version=tools.protocol_text()[1], _user_id=ME)
    assert again["desk_first"] == text, "每次都带: 共用一个身份, 分不出是不是同一场对话"


def test_comments_for_reply_starts_with_desk_first(monkeypatch):
    monkeypatch.setattr(core, "comments_for", lambda client, **kw: {"resolved": False, "need": ["post_body"]})
    out = tools.comments_for(link="https://example.invalid/x", _user_id=ME)
    assert list(out) == ["desk_first", "resolved", "need"]
    assert out["desk_first"] == tools.DESK_FIRST_COMMENTS
    assert "comments_for" in out["desk_first"] and "score_comments" in out["desk_first"]


def test_desk_first_never_breaks_a_non_dict_reply():
    assert tools._with_desk_first(None, "x") is None
    assert tools._with_desk_first({"desk_first": "旧", "a": 1}, "新") == {"desk_first": "新", "a": 1}


def test_tool_docs_mention_the_new_fields():
    assert "desk_first" in tools.open_project.__doc__ and "desk_first" in tools.comments_for.__doc__
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
    assert "draw_angles" in out["angles_note"] and "不用重写" in out["angles_note"] and "不用跟用户提" in out["angles_note"]
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
    msg = skillcheck.notice("0123456789ab", "ba9876543210")["message"]
    lines = msg.splitlines()
    assert len(lines) == 3 and lines[1:] == [skillcheck.UPDATE_URL, skillcheck.UPDATE_COMMAND]
    assert skillcheck.WHATS_NEW in lines[0] and "排第一" in lines[0] and "重新导入一次" in lines[0]
    assert "0123456789ab" in lines[0] and "ba9876543210" in lines[0]
    for blame in ("不是最新版", "请先升级", "你没", "忘了"):
        assert blame not in msg


def test_the_notice_still_reads_well_without_a_whats_new_line(monkeypatch):
    monkeypatch.setattr(skillcheck, "WHATS_NEW", "")
    first = skillcheck.notice("0123456789ab", "ba9876543210")["message"].splitlines()[0]
    assert first.startswith("写作台 skill 有新版（你手上的是")


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
