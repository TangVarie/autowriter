"""归档: 不用的项目不进清单、不进跨项目检查(2026-10-30 发布收尾)。

主身份名下 41 个项目, 在用的六个左右。不用的那些照样排在 list_projects 里, 模型每场开头要在一长串里挑; score_drafts 的
跨项目检查也拿它们的品牌词和近期标题去比, 一个早就不写的项目的品牌词出现在正文里就报一次「可能串项目」。

不加迁移: 标记是 ``projects.default_params`` 里的 ``"archived": true``, 只由运维命令 ``cli project-archive`` 设 / 撤。
这里钉的是:

  · 判据: 只认 JSON 的 true; default_params 写坏了按没归档算(读是宽的), CLI 写之前是严的;
  · list_projects 默认不列归档的, 藏了几个照实说(archived_hidden), include_archived 列出来带 archived: true;
  · 跨项目检查(_cross_project_shadow)的候选集去掉归档的项目, 品牌词和标题向量都不比; 当前项目自己归档了照比;
  · open_project 打开归档的项目照常给整份简报, 只多 archived 和一句让模型跟用户确认的话(不锁人);
  · CLI 设 / 撤 / 重复跑 / 项目不存在 / default_params 解析不了。

所有项目名、品牌名都是合成的。
"""

from __future__ import annotations

import json
import threading

import pytest

import config
import dedup
from deskcore import cli, core, delivery, store, tools as T
from tests.fakes import FakeClient
from tests.test_rule_compile import ME, OTHER, PROJ, make_client, mem
from tests.test_writer_facing_text import _assert_writer_safe

IDLE = "aaaaaaaa-0000-4000-8000-0000000000e1"
LIVE = "aaaaaaaa-0000-4000-8000-0000000000e2"
ARCHIVED = json.dumps({"archived": True}, ensure_ascii=False)


def _project(pid: str, name: str, brand: str, *, params=None, owner: str = ME) -> dict:
    row = {"id": pid, "name": name, "brand": brand, "owner_id": owner, "calibration_notes": "",
           "tactics": "[]", "custom_roles": []}
    if params is not None:
        row["default_params"] = params
    return row


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    """不接判定、不接 embedding、不起后台补编(同 test_writer_facing_text)。"""
    monkeypatch.setattr(config, "JUDGE_URL", "")
    monkeypatch.setattr(config, "RULE_COMPILE_AUTO", False)
    monkeypatch.setattr(core, "_RULE_COMPILE_STATE", {})
    monkeypatch.setattr(core, "_RULE_COMPILE_FAILED", {})
    monkeypatch.setattr(core, "_RULE_COMPILE_SLOTS", threading.BoundedSemaphore(1))
    monkeypatch.setattr(core, "_RULE_COMPILE_JEV", {"slow_until": 0.0})
    monkeypatch.setattr(core, "_RULE_COMPILE_INFLIGHT", {})
    monkeypatch.setattr(dedup, "embeddings_available", lambda: False)


# ══════════════════════════════════════════════════════════════════════
# 1 · 判据
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("params, want", [
    (ARCHIVED, True),                                               # 生产库的形状: JSON 字符串
    ({"archived": True}, True),                                     # 也认 dict
    (json.dumps({"archived": True, "isolation_terms": ["示例词"]}, ensure_ascii=False), True),
    (json.dumps({"archived": False}), False),
    (json.dumps({"archived": "true"}), False),                      # 字符串 "true" 不算
    (json.dumps({"isolation_terms": ["示例词"]}, ensure_ascii=False), False),
    ("{broken", False),                                             # 写坏了按没归档算, 不能把在用的项目藏起来
    ("", False), ("   ", False), (None, False), ("[]", False),
])
def test_only_a_json_true_counts_as_archived(params, want):
    assert core.project_archived({"default_params": params}) is want
    assert core.project_archived(None) is False
    assert core.project_archived({}) is False


# ══════════════════════════════════════════════════════════════════════
# 2 · list_projects
# ══════════════════════════════════════════════════════════════════════

def _list_client() -> FakeClient:
    return FakeClient(rows={"projects": [
        _project(LIVE, "示例甲-在用", "示例甲", params=json.dumps({"isolation_terms": ["甲词"]}, ensure_ascii=False)),
        _project(IDLE, "示例乙-不用了", "示例乙", params=ARCHIVED),
        _project(PROJ, "示例丙-在用", "示例丙"),                        # 没有 default_params 这一列的老行
        _project("aaaaaaaa-0000-4000-8000-0000000000e9", "别人的不用了", "别家", params=ARCHIVED, owner=OTHER),
    ]})


def test_list_projects_hides_archived_projects_by_default():
    c = _list_client()
    seen: dict = {}
    got = core.list_projects(c, user_id=ME, report_sink=seen)
    assert {p["project_id"] for p in got} == {LIVE, PROJ}
    assert seen == {"archived_hidden": 1}, "别人名下的不算进我藏了几个"
    assert all("archived" not in p for p in got), "在用的行不加键, 清单形状和原来一样"
    # 藏掉的不去数规则 / 指纹(计数仍是固定几次查询, 只是 id 少了)
    counted = [x for x in c.calls if x["table"] != "projects"]
    assert counted and all(IDLE not in (f[2] if isinstance(f[2], tuple) else (f[2],))
                           for x in counted for f in x["filters"]), counted


def test_list_projects_include_archived_lists_them_flagged():
    seen: dict = {}
    got = core.list_projects(_list_client(), user_id=ME, include_archived=True, report_sink=seen)
    by_id = {p["project_id"]: p for p in got}
    assert set(by_id) == {LIVE, IDLE, PROJ}
    assert by_id[IDLE]["archived"] is True and by_id[IDLE]["name"] == "示例乙-不用了"
    assert "archived" not in by_id[LIVE] and "archived" not in by_id[PROJ]
    assert "archived_hidden" not in seen, "全列出来了就没有藏的"


def test_list_projects_with_everything_archived_is_an_empty_list():
    c = FakeClient(rows={"projects": [_project(IDLE, "示例乙-不用了", "示例乙", params=ARCHIVED)]})
    seen: dict = {}
    assert core.list_projects(c, user_id=ME, report_sink=seen) == []
    assert seen == {"archived_hidden": 1}
    assert [x["table"] for x in c.calls] == ["projects"] * len(c.calls), "一个都不列就不去数"


def test_list_projects_tool_reports_how_many_it_hid(monkeypatch):
    c = _list_client()
    monkeypatch.setattr(core, "sb", lambda: c)
    out = T.list_projects(_user_id=ME)
    assert {p["project_id"] for p in out["projects"]} == {LIVE, PROJ}
    assert out["archived_hidden"] == 1
    _assert_writer_safe(out, "list_projects")

    out = T.list_projects(include_archived=True, _user_id=ME)
    assert {p["project_id"] for p in out["projects"]} == {LIVE, IDLE, PROJ}
    assert "archived_hidden" not in out

    c.rows["projects"] = [p for p in c.rows["projects"] if p["id"] != IDLE]
    assert "archived_hidden" not in T.list_projects(_user_id=ME), "没藏东西就不带这个键"


def test_list_projects_tool_description_explains_the_parameter():
    doc = T.list_projects.__doc__ or ""
    assert "include_archived" in doc and "archived_hidden" in doc
    assert "create_project" in doc, "找不到先带 include_archived 再看, 别急着建新项目"
    assert "draft_fingerprints" not in doc, "工具说明里不写表名"


# ══════════════════════════════════════════════════════════════════════
# 3 · 跨项目检查不拿归档的项目比
# ══════════════════════════════════════════════════════════════════════

def _shadow_client(*, target_params=None, idle_params=ARCHIVED) -> FakeClient:
    model = dedup.EMBEDDING_MODEL
    return FakeClient(rows={
        "projects": [
            _project(LIVE, "示例甲-在用", "示例甲", params=target_params),
            _project(IDLE, "示例乙-不用了", "示例乙",
                     params=json.dumps({**json.loads(idle_params), "isolation_terms": ["乙专属词"]}, ensure_ascii=False)
                     if idle_params else json.dumps({"isolation_terms": ["乙专属词"]}, ensure_ascii=False)),
            _project(PROJ, "示例丙-在用", "示例丙"),
        ],
        "draft_fingerprints": [
            {"id": "1", "project_id": LIVE, "version_id": "a", "title_embedding": [1., 0.], "embedding_model": model},
            {"id": "2", "project_id": IDLE, "version_id": "b", "title_embedding": [0., 1.], "embedding_model": model},
            {"id": "3", "project_id": PROJ, "version_id": "c", "title_embedding": [1., 0.], "embedding_model": model},
        ]})


DRAFTS = [{"title": "示例乙的乙专属词也好用", "body": ""}, {"title": "示例丙也来了", "body": ""}]
VECS = [[0., 1.], [1., 0.]]


def test_shadow_flags_the_idle_project_while_it_is_not_archived():
    """对照组: 没归档时, 品牌词和标题向量都会指向那个项目 —— 下面那条去掉的就是这一种。"""
    out = core._cross_project_shadow(_shadow_client(idle_params=None), LIVE, ME, DRAFTS, VECS, set())
    flagged = {m["project_id"] for f in out["flags"] for m in f["markers"]}
    assert IDLE in flagged and PROJ in flagged
    assert out["flags"][0]["title_vector"]["other_project_id"] == IDLE
    assert out["projects_total"] == 3 and "archived_skipped" not in out


def test_shadow_skips_archived_projects_for_markers_and_vectors(monkeypatch):
    c = _shadow_client()
    real, sampled = store.isolation_fingerprints, []

    def spy(sb, pid, model):
        sampled.append(pid)
        return real(sb, pid, model)

    monkeypatch.setattr(store, "isolation_fingerprints", spy)
    out = core._cross_project_shadow(c, LIVE, ME, DRAFTS, VECS, set())
    assert out["status"] == "shadow"
    flagged = {m["project_id"] for f in out["flags"] for m in f["markers"]}
    assert IDLE not in flagged, "归档的项目的品牌词不再报「可能串项目」"
    assert PROJ in flagged, "在用的项目照比"
    assert not any((f.get("title_vector") or {}).get("other_project_id") == IDLE for f in out["flags"])
    assert IDLE not in sampled, "归档的项目不去拉标题样本"
    assert out["archived_skipped"] == 1 and out["projects_total"] == 2
    _assert_writer_safe(out, "project_isolation")


def test_shadow_still_runs_when_the_current_project_is_archived():
    """写手确认过要写一个归档的项目(open_project 照开), 存档时它自己不能因为归档就「标记未查到」。"""
    c = _shadow_client(target_params=ARCHIVED)
    out = core._cross_project_shadow(c, LIVE, ME, DRAFTS, VECS, set())
    assert out["status"] == "shadow"
    assert out["projects_total"] == 2 and out["archived_skipped"] == 1, "自己留着, 只去掉别的归档项目"
    flagged = {m["project_id"] for f in out["flags"] for m in f["markers"]}
    assert flagged == {PROJ}


def test_bounded_shadow_timeout_keeps_markers_without_archived_ones(monkeypatch):
    """外层超时只带回品牌词标记那条路, 带回来的也已经去掉了归档的项目。"""
    import concurrent.futures as cf
    release = threading.Event()
    pool, slots = cf.ThreadPoolExecutor(max_workers=1), threading.BoundedSemaphore(1)
    monkeypatch.setattr(core, "_ISOLATION_POOL", pool)
    monkeypatch.setattr(core, "_ISOLATION_SLOTS", slots)
    monkeypatch.setattr(core, "_ISOLATION_TIMEOUT_SEC", 0.3)
    monkeypatch.setattr(core, "_ISOLATION_COMPARE_RESERVE_SEC", 0)
    monkeypatch.setattr(store, "isolation_fingerprints", lambda *a: (release.wait(4), [])[1])
    try:
        out = core._cross_project_shadow_bounded(_shadow_client(), LIVE, ME, DRAFTS, VECS, set())
    finally:
        release.set()
        pool.shutdown(wait=True)
    assert out["status"] == "timeout" and out["markers_checked"] is True
    flagged = {m["project_id"] for f in out["flags"] for m in f["markers"]}
    assert flagged == {PROJ}


def test_record_rule_guard_is_unchanged_by_archiving():
    """记规则那道拒写的闸这次没动(这一组只改 score_drafts 的跨项目检查): 归档项目的专属词照样挡。
    要不要让它也跳过归档的项目是另一个决定 —— 那道闸拦的是写, 放宽之前得先想清楚 global 规则那一路。"""
    c = _shadow_client()
    c.rows["memories"] = []
    with pytest.raises(core.ProjectIsolationRejected):
        core.record_rule(c, LIVE, "不许写乙专属词", user_id=ME)
    assert c.rows["memories"] == []


# ══════════════════════════════════════════════════════════════════════
# 4 · open_project: 归档的项目照开, 多一句确认
# ══════════════════════════════════════════════════════════════════════

def _brief_client(params) -> FakeClient:
    c = make_client([mem(8, "示例硬规则: 标题不超过二十字", severity="hard")], [])
    c.rows["projects"][0]["default_params"] = params
    return c


def test_open_project_on_an_archived_project_still_gives_the_whole_brief(monkeypatch):
    c = _brief_client(json.dumps({"archived": True, "isolation_terms": ["示例词"]}, ensure_ascii=False))
    monkeypatch.setattr(core, "sb", lambda: c)
    out = T.open_project(PROJ, _user_id=ME)
    assert out["archived"] is True
    assert out["archived_note"] == core.ARCHIVED_NOTE.format(name="示例品牌-产品向")
    assert "示例品牌-产品向" in out["archived_note"] and "确认" in out["archived_note"]
    assert "\n" not in out["archived_note"], "一句话"
    # 简报一样不少: 不锁人
    assert "示例硬规则" in out["p0"] and out["project_id"] == PROJ and "lessons_status" in out
    _assert_writer_safe(out, "open_project[archived]")


@pytest.mark.parametrize("params", [None, "", json.dumps({"isolation_terms": ["示例词"]}, ensure_ascii=False),
                                    json.dumps({"archived": False}), "{broken"])
def test_open_project_on_a_live_project_does_not_mention_it(monkeypatch, params):
    c = _brief_client(params)
    monkeypatch.setattr(core, "sb", lambda: c)
    out = T.open_project(PROJ, _user_id=ME)
    assert "archived" not in out and "archived_note" not in out
    assert "示例硬规则" in out["p0"]


def test_open_project_tool_description_mentions_the_confirmation():
    doc = T.open_project.__doc__ or ""
    assert "archived_note" in doc and "确认" in doc


def test_archived_note_is_writer_safe():
    _assert_writer_safe({"note": core.ARCHIVED_NOTE.format(name="示例")}, "ARCHIVED_NOTE")
    assert "归档" not in core.ARCHIVED_NOTE and "运维" not in core.ARCHIVED_NOTE


# ══════════════════════════════════════════════════════════════════════
# 5 · CLI: project-archive / projects --include-archived
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture
def cli_client(monkeypatch):
    c = FakeClient(rows={"projects": [
        _project(IDLE, "示例乙-不用了", "示例乙",
                 params=json.dumps({"isolation_terms": ["乙专属词"], "delivery": {"layout": "blocks"}},
                                   ensure_ascii=False)),
        _project(LIVE, "示例甲-在用", "示例甲"),
    ]})
    monkeypatch.setattr(core, "sb", lambda: c)
    return c


def _params(c, pid=IDLE) -> dict:
    raw = next(p for p in c.rows["projects"] if p["id"] == pid)["default_params"]
    assert isinstance(raw, str), "生产库这一列全是 JSON 字符串, 别写成 dict 混进去"
    return json.loads(raw)


def _updates(c):
    return [x for x in c.calls if x["op"] == "update"]


def test_cli_archive_then_unarchive_round_trip(cli_client, capsys):
    assert cli.main(["project-archive", "--project", IDLE]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"project_id": IDLE, "project_name": "示例乙-不用了", "archived": True, "changed": True}
    stored = _params(cli_client)
    assert stored["archived"] is True
    assert stored["isolation_terms"] == ["乙专属词"] and stored["delivery"] == {"layout": "blocks"}, "别的键不能丢"
    assert core.project_archived(cli_client.rows["projects"][0])

    assert cli.main(["project-archive", "--project", IDLE, "--unarchive"]) == 0
    assert json.loads(capsys.readouterr().out)["archived"] is False
    assert _params(cli_client) == {"isolation_terms": ["乙专属词"], "delivery": {"layout": "blocks"}}, \
        "撤销是删键, 不留 archived: false"


def test_cli_archive_is_idempotent(cli_client, capsys):
    assert cli.main(["project-archive", "--project", IDLE]) == 0
    capsys.readouterr()
    n = len(_updates(cli_client))
    assert cli.main(["project-archive", "--project", IDLE]) == 0
    out = capsys.readouterr().out
    assert '"changed": false' in out and "没写" in out
    assert len(_updates(cli_client)) == n, "已经是这个状态就不写"
    # 没归档的项目撤销归档: 也不写
    assert cli.main(["project-archive", "--project", LIVE, "--unarchive"]) == 0
    assert len(_updates(cli_client)) == n


def test_cli_archive_on_a_project_without_default_params(cli_client):
    assert cli.main(["project-archive", "--project", LIVE]) == 0
    assert _params(cli_client, LIVE) == {"archived": True}


def test_cli_archive_refuses_unknown_project(cli_client, capsys):
    assert cli.main(["project-archive", "--project", "aaaaaaaa-0000-4000-8000-00000000dead"]) == 1
    assert "项目不存在" in capsys.readouterr().out
    assert not _updates(cli_client)


def test_cli_archive_refuses_default_params_it_cannot_parse(cli_client, capsys):
    cli_client.rows["projects"][0]["default_params"] = "{broken"
    assert cli.main(["project-archive", "--project", IDLE]) == 1
    assert "没写进去" in capsys.readouterr().out
    assert cli_client.rows["projects"][0]["default_params"] == "{broken", "拿 {} 去合并会把别的键一起抹掉"
    assert not _updates(cli_client)
    with pytest.raises(delivery.TemplateError):
        core.project_archive(cli_client, IDLE)


def test_cli_projects_hides_archived_unless_asked(cli_client, capsys):
    assert cli.main(["project-archive", "--project", IDLE]) == 0
    capsys.readouterr()
    cli_client.rpc_impl["deskcore_fingerprint_counts"] = lambda args: []
    assert cli.main(["projects", "--user", ME]) == 0
    assert [p["project_id"] for p in json.loads(capsys.readouterr().out)] == [LIVE]
    assert cli.main(["projects", "--user", ME, "--include-archived"]) == 0
    listed = {p["project_id"]: p for p in json.loads(capsys.readouterr().out)}
    assert set(listed) == {IDLE, LIVE} and listed[IDLE]["archived"] is True
