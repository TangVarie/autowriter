"""服务端 skill 版本探针 (deskcore/skillcheck.py)。

写手手上的 skill 改了没人知道; 服务端每次都在, 所以由它抽查: 入口工具 (open_project / get_protocol) 报版本,
不是最新就在返回值里带 skill_update, message 末尾两行固定是升级入口 —— 仓库里 skill 的地址和 WorkBuddy 的
「更新技能/bywood-writing-desk」。记录在案是旧版的写手, 其它工具的返回值也捎上提醒 (30 分钟最多一次)。

这里钉的【被禁止的形态】:
  · 最新版也被提醒 (写手刚导完又被叫去导);
  · 旧版 / 没核到版本却没提醒, 或提醒里的链接、「更新技能」那行被改了;
  · 抽查刷屏 (每个工具都带), 或进程重启后给没报过版本的人误报;
  · 探针出错拖垮工具本身 (open_project 是合规读, 不能因为探针挂掉);
  · /health 里出现是谁。
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
    tools.open_project("p", skill_version=OLD, _user_id=ME)
    first = rest()
    assert first["skill_update"]["message"].splitlines()[-2:] == LINK_LINES
    t[0] += 60
    assert "skill_update" not in rest(), "30 分钟内不重复提醒"
    t[0] += skillcheck.REMIND_EVERY_S
    assert "skill_update" in rest(), "过了 30 分钟再提醒一次"


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
    assert "return _skill_probe(name, await run_in_threadpool(fn, *args, **kwargs))" in src, "MCP 路径"


def test_health_reports_counts_but_not_who(monkeypatch):
    tools.open_project("p", skill_version=CURRENT, _user_id=ME)
    tools.open_project("p", skill_version=OLD, _user_id="44444444-4444-4444-4444-444444444444")
    tools.open_project("p", _user_id="55555555-5555-5555-5555-555555555555")
    h = skillcheck.health(CURRENT)
    assert h == {"current": CURRENT, "writers_24h": {"up_to_date": 1, "outdated": 1, "unknown": 1},
                 "outdated_versions": {OLD: 1}}
    assert "4444" not in repr(h) and ME not in repr(h)


def test_health_endpoint_exposes_the_counts(monkeypatch):
    testclient = pytest.importorskip("fastapi.testclient")
    tools.open_project("p", skill_version=OLD, _user_id=ME)
    r = testclient.TestClient(A.app, raise_server_exceptions=False).get("/health")
    sv = r.json()["skill_versions"]
    assert sv["current"] == CURRENT and sv["writers_24h"]["outdated"] == 1 and ME not in r.text


def test_a_stale_report_stops_driving_reminders(rest, monkeypatch):
    t = [2_000_000.0]
    monkeypatch.setattr(skillcheck.time, "time", lambda: t[0])
    tools.open_project("p", skill_version=OLD, _user_id=ME)
    t[0] += skillcheck.RECORD_TTL_S + 1
    assert "skill_update" not in rest(), "12 小时前报的旧版不再算数 (那之后可能早升级了, 只是这场还没调 open_project)"
