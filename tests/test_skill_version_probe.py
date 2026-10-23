"""服务端 skill 版本探针 (deskcore/skillcheck.py)。

写手手上的 skill 改了没人知道; 服务端每次都在, 所以由它抽查: 入口工具 (open_project / get_protocol) 报版本,
不是最新就在返回值里带 skill_update, message 末尾两行固定是升级入口 —— 仓库里 skill 的地址和 WorkBuddy 的
「更新技能/bywood-writing-desk」。记录在案是旧版的写手, 其它工具的返回值也捎上提醒 (30 分钟最多一次)。

这里钉的【被禁止的形态】:
  · 最新版也被提醒 (写手刚导完又被叫去导);
  · 旧版 / 没核到版本却没提醒, 或提醒里的链接、「更新技能」那行被改了;
  · 抽查刷屏 (每个工具都带), 或进程重启后给没报过版本的人误报;
  · 探针出错拖垮工具本身 (open_project 是合规读, 不能因为探针挂掉), 包括 MCP 层把 skill_version=null 当参数错误;
  · 计数放进不鉴权的 /health (app.py 的规矩: 那里不许放业务数据), 或计数里出现是谁。
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


def test_open_project_without_a_version_falls_back_to_a_recent_report():
    tools.get_protocol(local_version=CURRENT, _user_id=ME)
    assert "skill_update" not in tools.open_project("p", _user_id=ME), "12 小时内报过最新版, 这次没带也不提醒"
    other = "22222222-2222-2222-2222-222222222222"
    u = tools.open_project("p", _user_id=other)["skill_update"]
    assert u["status"] == "unknown" and u["message"].splitlines()[-2:] == LINK_LINES, \
        "从没报过版本 (更老的 skill 没有 protocol_version 那一行) → 也提醒升级"


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


def test_open_project_docs_tell_the_model_to_pass_the_version_and_relay_the_notice():
    doc = tools.open_project.__doc__ or ""
    assert "skill_version" in doc and "protocol_version" in doc and "skill_update" in doc and "原样" in doc


# ── 抽查: 其它工具 ───────────────────────────────────────────────────────────

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


def test_other_tools_remind_an_outdated_writer_at_most_every_30_minutes(rest, monkeypatch):
    t = [1_000_000.0]
    monkeypatch.setattr(skillcheck.time, "time", lambda: t[0])
    assert "skill_update" in tools.open_project("p", skill_version=OLD, _user_id=ME)
    assert "skill_update" not in rest(), "入口工具刚提醒过, 紧跟着的工具不再来一条"
    t[0] += skillcheck.REMIND_EVERY_S
    again = rest()
    assert again["skill_update"]["message"].splitlines()[-2:] == LINK_LINES, "过了 30 分钟抽查再提醒"
    t[0] += 60
    assert "skill_update" not in rest(), "30 分钟内不重复提醒"


def test_other_tools_leave_current_and_never_reported_writers_alone(rest):
    tools.open_project("p", skill_version=CURRENT, _user_id=ME)
    assert "skill_update" not in rest()
    assert "skill_update" not in rest(user="33333333-3333-3333-3333-333333333333"), \
        "进程重启后还没报过版本的人不提醒 —— 否则每次部署都误报一轮"


def test_a_failing_probe_never_changes_the_tool_result(rest, monkeypatch):
    tools.open_project("p", skill_version=OLD, _user_id=ME)

    def boom(*a, **k):
        raise RuntimeError("probe bug")
    monkeypatch.setattr(skillcheck, "annotate", boom)
    assert rest() == {"ok": True}


def test_both_call_paths_run_the_probe():
    import inspect
    src = inspect.getsource(A)
    assert "return _skill_probe(name, fn(**kwargs))" in src, "REST 路径"
    # MCP 路径(W-29 起先接住结果、失败留痕, 再过探针)
    assert "result = await run_in_threadpool(fn, *args, **kwargs)" in src \
        and "return _skill_probe(name, result)" in src, "MCP 路径"


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


def test_a_stale_report_stops_driving_reminders(rest, monkeypatch):
    t = [2_000_000.0]
    monkeypatch.setattr(skillcheck.time, "time", lambda: t[0])
    tools.open_project("p", skill_version=OLD, _user_id=ME)
    t[0] += skillcheck.RECORD_TTL_S + 1
    assert "skill_update" not in rest(), "12 小时前报的旧版不再算数 (那之后可能早升级了, 只是这场还没调 open_project)"


def test_calls_without_a_version_do_not_keep_an_old_report_alive(monkeypatch):
    t = [3_000_000.0]
    monkeypatch.setattr(skillcheck.time, "time", lambda: t[0])
    tools.open_project("p", skill_version=OLD, _user_id=ME)
    t[0] += 6 * 3600
    assert tools.open_project("p", _user_id=ME)["skill_update"]["status"] == "outdated"   # 12 小时内沿用
    t[0] += 7 * 3600                                                                       # 离那次报版本 13 小时
    u = tools.open_project("p", _user_id=ME)["skill_update"]
    assert u["status"] == "unknown" and u["local"] is None, "沿用不续命: 13 小时前那条旧版记录不再冒充现在的版本"


@pytest.mark.parametrize("raw", ["d59368a8", "无", "v1"])
def test_an_unreadable_version_is_outdated_in_both_paths(raw):
    gp = tools.get_protocol(local_version=raw, _user_id=ME)
    assert gp["up_to_date"] is False and gp["skill_update"]["status"] == "outdated", \
        "get_protocol 说旧版, 探针也说旧版, 同一个返回里对得上"
    assert gp["skill_update"]["message"].splitlines()[-2:] == LINK_LINES
    assert tools.open_project("p", skill_version=raw, _user_id=ME)["skill_update"]["status"] == "outdated"


@pytest.mark.parametrize("blank", [None, "", "  "])
def test_a_blank_version_means_not_reported(blank):
    u = tools.open_project("p", skill_version=blank, _user_id=ME)["skill_update"]
    assert u["status"] == "unknown"


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
    assert out["project_name"] == "测试" and out["skill_update"]["status"] == "unknown"
    out = _mcp_payload(_mcp_call("open_project", {"project_id": "p"}))
    assert out["project_name"] == "测试"


@pytest.mark.skipif(A._mcp is None, reason="mcp SDK not installed")
def test_over_mcp_other_tools_carry_the_spot_check(monkeypatch):
    monkeypatch.setattr(skillcheck, "REMIND_EVERY_S", 0)
    _mcp_call("open_project", {"project_id": "p", "skill_version": OLD})
    monkeypatch.setattr(core, "my_style", lambda client, pid, *, user_id=None: {"notes": ""})
    out = _mcp_payload(_mcp_call("my_style", {"project_id": "p"}))
    assert out.get("skill_update", {}).get("status") == "outdated", "MCP 这条路也做抽查"


def test_a_protocol_change_turns_earlier_current_reports_outdated(rest, monkeypatch):
    """状态不在报版本那一刻定死: 协议换了版 (codex review on #106), 之前报「最新」的人随之算旧版。"""
    tools.open_project("p", skill_version=CURRENT, _user_id=ME)
    newer = "fedcba987654"
    monkeypatch.setattr(A, "_protocol_version", lambda: newer)
    out = rest()
    assert out["skill_update"]["status"] == "outdated" and out["skill_update"]["local"] == CURRENT
    h = skillcheck.health(newer)
    assert h["writers_24h"]["outdated"] == 1 and h["outdated_versions"] == {CURRENT: 1}
