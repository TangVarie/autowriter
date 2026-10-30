"""服务端 skill 版本探针 (deskcore/skillcheck.py)。

写手手上的 skill 改了没人知道; 服务端每次都在, 所以由它核: 入口工具 (open_project / get_protocol / comments_for)
报版本, 报的是旧版就在返回值里带 skill_update, message 末尾两行固定是升级入口 —— 仓库里 skill 的地址和 WorkBuddy 的
「更新技能/bywood-writing-desk」。

2026-10-30 发布收尾: 写作台是一个团队共用一个身份(一个 user_id)。原来按身份记版本, 一个人手上的旧 skill 让所有人
都收到升级提醒, 进程重启后没带版本的 open_project 又被提醒「没核到版本, 请升级」。现在提醒只看这一次调用报上来的版本。

这里钉的【被禁止的形态】:
  · 最新版也被提醒 (写手刚导完又被叫去导);
  · 报了旧版却没提醒, 或提醒里的链接、「更新技能」那行被改了;
  · 没报版本 (或报「无」) 也被提醒; 同一身份下别人报过的旧版、重启前的记录, 被算到这一次头上;
  · 非入口工具捎上提醒;
  · 探针出错拖垮工具本身 (open_project 是合规读, 不能因为探针挂掉), 包括 MCP 层把 skill_version=null 当参数错误;
  · 计数放进不鉴权的 /health (app.py 的规矩: 那里不许放业务数据), 或计数里出现是谁; 共用身份时计数只剩最后一个版本。
"""

from __future__ import annotations

import pytest

from deskcore import core, identity, skillcheck, tools

CURRENT = tools.protocol_text()[1]
OLD = "0123456789ab"
ME = "11111111-1111-1111-1111-111111111111"
LINK_LINES = [skillcheck.UPDATE_URL, skillcheck.UPDATE_COMMAND]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    skillcheck.reset()
    monkeypatch.setattr(core, "sb", lambda: object())
    monkeypatch.setattr(core, "build_writing_brief", lambda client, pid, *, user_id=None, brief=None: {"project_name": "测试"})
    yield
    skillcheck.reset()


def test_the_update_lines_are_exactly_what_the_owner_asked_for():
    assert skillcheck.UPDATE_URL == ("https://github.com/TangVarie/autowriter/tree/claude/xhs-content-workstation-BSEuH/"
                                     "skills/bywood-writing-desk")
    assert skillcheck.UPDATE_COMMAND == "更新技能/bywood-writing-desk"


@pytest.mark.parametrize("passed", [CURRENT, f"protocol_version: {CURRENT}", f"<!-- protocol_version: {CURRENT} -->"])
def test_open_project_with_the_current_skill_is_not_nagged(passed):
    out = tools.open_project("p", skill_version=passed, _user_id=ME)
    assert "skill_update" not in out, "最新版不许提醒 —— 写手刚导完又被叫去导"


def test_open_project_with_an_old_skill_returns_the_update_notice():
    out = tools.open_project("p", skill_version=f"protocol_version: {OLD}", _user_id=ME)
    u = out["skill_update"]
    assert u["status"] == "outdated" and u["local"] == OLD and u["current"] == CURRENT
    assert u["message"].splitlines()[-2:] == LINK_LINES, "末尾两行固定是升级入口, 一字不改"
    assert OLD in u["message"] and "原样" in u["action"]


def test_open_project_without_a_version_is_recorded_but_not_nagged():
    """进程重启后 (记录清零) 第一场没带版本: 原来回「没核到版本, 请升级」, 每次部署都给正在写稿的人报一轮。"""
    assert "skill_update" not in tools.open_project("p", _user_id=ME), "没报版本只记一笔, 不提醒"
    assert skillcheck.health(CURRENT)["writers_24h"] == {"up_to_date": 0, "outdated": 0, "unknown": 1}, "计数照记"


def test_an_old_report_under_the_shared_identity_does_not_follow_the_next_session():
    """一个团队共用一个身份: 甲手上是旧版、乙刚升级、丙的 skill 没带版本 —— 只有甲那一场被提醒。"""
    assert tools.open_project("p", skill_version=OLD, _user_id=ME)["skill_update"]["status"] == "outdated"
    assert "skill_update" not in tools.open_project("p", skill_version=CURRENT, _user_id=ME), "乙: 最新版"
    assert "skill_update" not in tools.open_project("p", _user_id=ME), "丙: 没带版本, 不沿用甲那条旧版"
    assert "skill_update" not in tools.get_protocol(_user_id=ME), "get_protocol 没带版本也一样"
    assert tools.open_project("p", skill_version=OLD, _user_id=ME)["skill_update"]["local"] == OLD, "甲再来还提醒"


def test_get_protocol_carries_the_same_notice():
    out = tools.get_protocol(local_version=OLD, _user_id=ME)
    assert out["up_to_date"] is False and "重新导入" in out["warning"] and skillcheck.UPDATE_URL in out["warning"]
    assert out["skill_update"]["message"].splitlines()[-2:] == LINK_LINES
    assert "skill_update" not in tools.get_protocol(local_version=CURRENT, _user_id=ME)


def test_open_project_survives_a_broken_protocol_file(monkeypatch):
    def boom():
        raise FileNotFoundError("protocol.md")
    monkeypatch.setattr(tools, "protocol_text", boom)
    assert tools.open_project("p", skill_version=OLD, _user_id=ME) == {"project_name": "测试"}, \
        "探针读不了协议就跳过, 不能把合规读弄坏"


def test_a_failing_probe_never_changes_the_entry_tool_result(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("probe bug")
    monkeypatch.setattr(skillcheck, "probe", boom)
    assert tools.open_project("p", skill_version=OLD, _user_id=ME) == {"project_name": "测试"}
    gp = tools.get_protocol(local_version=OLD, _user_id=ME)
    assert gp["up_to_date"] is False and "skill_update" not in gp, "版本照核, 只是这次没带提醒"


def test_open_project_docs_tell_the_model_to_pass_the_version_and_relay_the_notice():
    doc = tools.open_project.__doc__ or ""
    assert "skill_version" in doc and "protocol_version" in doc and "skill_update" in doc and "原样" in doc


# ── 其它工具: 不再抽查 (2026-10-30) ─────────────────────────────────────────────

A = pytest.importorskip("deskcore.app")


def _ping(_user_id=None):
    return {"ok": True}


@pytest.fixture
def rest(monkeypatch):
    monkeypatch.setitem(tools.TOOLS, "ping", (_ping, True))

    def call(user=ME):
        token = A._caller.set(identity.Caller(user, "t", True))
        try:
            return A._call_tool("ping", {})
        finally:
            A._caller.reset(token)
    return call


def test_other_tools_never_carry_the_reminder(rest):
    """原来记录在案是旧版的身份, 其它工具 30 分钟捎一条提醒。共用一个身份时那是别人的旧版 —— 不再抽查。
    REST 和 MCP 两条路都原样回工具结果(MCP 那条见 test_over_mcp_the_entry_tool_reminds_and_other_tools_do_not)。"""
    tools.open_project("p", skill_version=OLD, _user_id=ME)
    assert rest() == {"ok": True}, "同一身份刚报过旧版, 其它工具也原样回"
    tools.open_project("p", _user_id=ME)
    assert rest() == {"ok": True}
    assert rest(user="33333333-3333-3333-3333-333333333333") == {"ok": True}


def test_health_reports_counts_but_not_who(monkeypatch):
    tools.open_project("p", skill_version=CURRENT, _user_id=ME)
    tools.open_project("p", skill_version=OLD, _user_id="44444444-4444-4444-4444-444444444444")
    tools.open_project("p", _user_id="55555555-5555-5555-5555-555555555555")
    h = skillcheck.health(CURRENT)
    assert h == {"current": CURRENT, "writers_24h": {"up_to_date": 1, "outdated": 1, "unknown": 1},
                 "outdated_versions": {OLD: 1}}
    assert "4444" not in repr(h) and ME not in repr(h)


KEY = "k-skill-probe"


def test_counts_need_a_key_and_stay_off_health(monkeypatch):
    testclient = pytest.importorskip("fastapi.testclient")
    monkeypatch.setenv("DESKCORE_KEYS", '{"%s": {"user_id": "%s", "name": "t"}}' % (KEY, ME))
    tools.open_project("p", skill_version=OLD, _user_id=ME)
    c = testclient.TestClient(A.app, raise_server_exceptions=False)
    assert "skill_versions" not in c.get("/health").json() and "writers_24h" not in c.get("/health").text, \
        "/health 不鉴权, 按 app.py 的规矩不许放业务数据"
    assert c.get("/skill-versions").status_code == 401
    r = c.get("/skill-versions", headers={"X-Deskcore-Key": KEY})
    assert r.status_code == 200 and r.json()["writers_24h"]["outdated"] == 1 and ME not in r.text


def test_counts_cover_the_last_24_hours_only(monkeypatch):
    t = [2_000_000.0]
    monkeypatch.setattr(skillcheck.time, "time", lambda: t[0])
    tools.open_project("p", skill_version=OLD, _user_id=ME)
    t[0] += skillcheck.HEALTH_WINDOW_S - 60
    tools.open_project("p", skill_version=CURRENT, _user_id=ME)
    assert skillcheck.health(CURRENT)["writers_24h"] == {"up_to_date": 1, "outdated": 1, "unknown": 0}
    t[0] += 120
    tools.open_project("p", skill_version=CURRENT, _user_id=ME)
    assert skillcheck.health(CURRENT) == {"current": CURRENT, "outdated_versions": {},
                                          "writers_24h": {"up_to_date": 1, "outdated": 0, "unknown": 0}}
    assert len(skillcheck._seen) == 1, "过了窗口的记录删掉, 乱填的版本串不会越攒越多"


def test_the_reminder_follows_the_version_of_this_call_only():
    seq = [OLD, None, CURRENT, OLD, ""]
    got = ["skill_update" in tools.open_project("p", skill_version=v, _user_id=ME) for v in seq]
    assert got == [True, False, False, True, False]


@pytest.mark.parametrize("raw", ["d59368a8", "v1"])
def test_an_unreadable_version_is_outdated_in_both_paths(raw):
    gp = tools.get_protocol(local_version=raw, _user_id=ME)
    assert gp["up_to_date"] is False and gp["skill_update"]["status"] == "outdated", \
        "get_protocol 说旧版, 探针也说旧版, 同一个返回里对得上"
    assert gp["skill_update"]["message"].splitlines()[-2:] == LINK_LINES
    assert tools.open_project("p", skill_version=raw, _user_id=ME)["skill_update"]["status"] == "outdated"


@pytest.mark.parametrize("blank", [None, "", "  ", "无", "没有", "None", "NULL", "n/a"])
def test_a_blank_or_placeholder_version_means_not_reported(blank):
    """skill 里没有 protocol_version 那一行时, 模型传空或传一个「无」—— 都是没报, 不提醒、不当成一个版本。"""
    assert "skill_update" not in tools.open_project("p", skill_version=blank, _user_id=ME)
    assert skillcheck.health(CURRENT)["writers_24h"]["unknown"] == 1
    assert skillcheck.health(CURRENT)["outdated_versions"] == {}
    if blank is not None:
        gp = tools.get_protocol(local_version=blank, _user_id=ME)
        assert gp["up_to_date"] is None and gp["protocol"] and "skill_update" not in gp, \
            "get_protocol 同一个口径: 当没传, 全文照发"


def _mcp_call(name, args, user=ME):
    import asyncio
    token = A._caller.set(identity.Caller(user, "t", True))
    try:
        return asyncio.run(A._mcp.call_tool(name, args))
    finally:
        A._caller.reset(token)


def _mcp_payload(res):
    import json
    blocks = res[0] if isinstance(res, tuple) else res
    return json.loads(blocks[0].text)


@pytest.mark.skipif(A._mcp is None, reason="mcp SDK not installed")
def test_over_mcp_a_null_skill_version_does_not_break_open_project():
    """老 skill 的模型最可能给 skill_version 传 null; 参数校验把它当错误的话, 合规读整个失败 (独立评审指出)。"""
    out = _mcp_payload(_mcp_call("open_project", {"project_id": "p", "skill_version": None}))
    assert out["project_name"] == "测试" and "skill_update" not in out
    out = _mcp_payload(_mcp_call("open_project", {"project_id": "p"}))
    assert out["project_name"] == "测试"


@pytest.mark.skipif(A._mcp is None, reason="mcp SDK not installed")
def test_over_mcp_the_entry_tool_reminds_and_other_tools_do_not(monkeypatch):
    out = _mcp_payload(_mcp_call("open_project", {"project_id": "p", "skill_version": OLD}))
    assert out["skill_update"]["status"] == "outdated", "MCP 这条路入口工具照样提醒"
    monkeypatch.setattr(core, "my_style", lambda client, pid, *, user_id=None: {"notes": ""})
    out = _mcp_payload(_mcp_call("my_style", {"project_id": "p"}))
    assert out == {"notes": ""}, "其它工具原样回"


def test_a_protocol_change_turns_earlier_current_reports_outdated(monkeypatch):
    """计数不在报版本那一刻定死: 协议换了版 (codex review on #106), 之前报「最新」的那一笔随之算旧版;
    同一个版本再来报就提醒。"""
    tools.open_project("p", skill_version=CURRENT, _user_id=ME)
    newer = "fedcba987654"
    h = skillcheck.health(newer)
    assert h["writers_24h"]["outdated"] == 1 and h["outdated_versions"] == {CURRENT: 1}
    monkeypatch.setattr(tools, "protocol_text", lambda: ("正文", newer))
    u = tools.open_project("p", skill_version=CURRENT, _user_id=ME)["skill_update"]
    assert u["status"] == "outdated" and u["local"] == CURRENT and u["current"] == newer
