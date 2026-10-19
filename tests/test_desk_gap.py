"""desk_gap (TV O-04, 2026-10-19): 你名下哪个项目最近发出去的帖写作台没接住, 在连着写作台的对话里告诉运营。

途鸽 9-16 之后写作台一次调用都没收到, 10-12 那周 22 篇全是补录副本, 同一位写手别的项目照常在写作台里写。
漏接的那些对话连不到写作台, 只能在【连着】的对话里提醒。

  1. 途鸽形状(7 天内补录 22 / 对上 0、60 天内发过牌) → 报; 窗口外的旧补录不算
  2. 下限: 补录差一篇、对上多一篇都不报; sportsix 那种"一部分在外面写"(12 / 18)不报
  3. 60 天没在写作台里写过的项目不唠叨
  4. 只看自己名下: 映射到同一个 TV 项目的别人的项目不算
  5. 当前正开着的就是它 → 话术换成"这次写完记得打分存档"; 名字取补录目标那个项目
  6. 缓存: 同一个库 10 分钟内不重算, 不同的库各算各的
  7. open_project / list_projects 带上它; 它查挂了清单和简报照给
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from deskcore import core, tools
from tests.fakes import FakeClient

ME, OTHER = "u-me", "u-other"
NOW = datetime.now(timezone.utc)


def _ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat()


def _client(*, ing=22, caught=0, drew_days=30, owner=ME):
    projects = [{"id": "p-tuge", "name": "途鸽", "brand": "途鸽", "owner_id": owner},
                {"id": "p-tuge-d1", "name": "途鸽D-1", "brand": "途鸽", "owner_id": owner},
                {"id": "p-spx", "name": "sportsix", "brand": "sportsix", "owner_id": ME}]
    tvmap = [{"tv_project_id": "TUGE", "project_id": "p-tuge-d1", "ingest_target": False, "note": ""},
             {"tv_project_id": "TUGE", "project_id": "p-tuge", "ingest_target": True, "note": ""},
             {"tv_project_id": "SPX", "project_id": "p-spx", "ingest_target": True, "note": ""}]
    link = lambda nid, tv, kind, **age: {"note_id": nid, "tv_project_id": tv, "match_kind": kind,  # noqa: E731
                                         "matched_at": _ago(**age)}
    links = ([link(f"t{i}", "TUGE", "ingested", days=2) for i in range(ing)]
             + [link(f"c{i}", "TUGE", "fuzzy", days=2) for i in range(caught)]
             + [link("amb", "TUGE", "ambiguous", days=2), link("un", "TUGE", "unmatched", days=2)]
             + [link(f"old{i}", "TUGE", "ingested", days=20) for i in range(30)]
             + [link(f"s{i}", "SPX", "ingested", days=1) for i in range(12)]
             + [link(f"sb{i}", "SPX", "body_exact", days=1) for i in range(18)])
    ledger = [{"id": "a1", "project_id": "p-tuge", "drawn_at": _ago(days=drew_days)},
              {"id": "a2", "project_id": "p-spx", "drawn_at": _ago(days=1)}]
    return FakeClient(rows={"projects": projects, "tv_project_map": tvmap,
                            "tv_note_links": links, "angle_ledger": ledger})


@pytest.fixture(autouse=True)
def _fresh_cache():
    core._desk_gap_cache.clear()
    yield
    core._desk_gap_cache.clear()


def test_the_tuge_shape_is_reported_and_old_copies_do_not_count():
    gap = core.desk_gaps(_client(), user_id=ME)
    assert [p["tv_project_id"] for p in gap["projects"]] == ["TUGE"], "sportsix 12 / 18 不该报"
    (p,) = gap["projects"]
    assert (p["project"], p["published"], p["caught"]) == ("途鸽", 22, 0), "20 天前的补录 / ambiguous 不算"
    assert "「途鸽」最近 7 天发出去 22 篇" in gap["message"] and "打开途鸽" in gap["message"]
    assert "project_ids" not in p, "给模型看的不带内部 id 列表"
    assert gap["task"] == core.DESK_GAP_TASK


def test_floors():
    assert core.desk_gaps(_client(ing=core.DESK_GAP_MIN_UNCAUGHT - 1), user_id=ME) is None
    core._desk_gap_cache.clear()
    assert core.desk_gaps(_client(ing=10, caught=core.DESK_GAP_MAX_CAUGHT + 1), user_id=ME) is None
    core._desk_gap_cache.clear()
    assert core.desk_gaps(_client(ing=10, caught=core.DESK_GAP_MAX_CAUGHT), user_id=ME) is not None


def test_a_project_not_written_in_the_desk_for_60_days_is_left_alone():
    assert core.desk_gaps(_client(drew_days=core.DESK_GAP_USED_WITHIN_DAYS + 1), user_id=ME) is None


def test_only_my_own_projects():
    assert core.desk_gaps(_client(owner=OTHER), user_id=ME) is None
    assert core.desk_gaps(_client(), user_id=None) is None


def test_the_project_being_opened_gets_the_write_and_score_wording():
    gap = core.desk_gaps(_client(), user_id=ME, current_project_id="p-tuge-d1")
    assert "这次就在写作台里写" in gap["message"] and "打开途鸽" not in gap["message"]


def test_cached_per_client_for_ten_minutes():
    c = _client()
    assert core.desk_gaps(c, user_id=ME) is not None
    c.rows["tv_note_links"].clear()
    assert core.desk_gaps(c, user_id=ME) is not None, "10 分钟内同一个库不重算"
    assert core.desk_gaps(_client(ing=0), user_id=ME) is None, "另一个库各算各的"
    core._desk_gap_cache.clear()
    assert core.desk_gaps(c, user_id=ME) is None


def test_open_project_and_list_projects_carry_it_and_survive_its_failure(monkeypatch):
    monkeypatch.setattr(core, "sb", lambda: object())
    monkeypatch.setattr(core, "build_writing_brief", lambda *a, **k: {"p0": [], "counts": {}})
    monkeypatch.setattr(core, "edit_digest", lambda *a, **k: None)
    monkeypatch.setattr(core, "list_projects", lambda *a, **k: [{"id": "p-tuge"}])
    seen = {}

    def fake_gaps(client, *, user_id, current_project_id=None):
        seen["current"] = current_project_id
        return {"projects": [], "message": "写作台提醒:\n…", "task": "x"}

    monkeypatch.setattr(core, "desk_gaps", fake_gaps)
    assert tools.open_project("p-tuge", _user_id=ME)["desk_gap"]["message"].startswith("写作台提醒")
    assert seen["current"] == "p-tuge", "open_project 要把正在开的项目传进去"
    assert tools.list_projects(_user_id=ME)["desk_gap"]["task"] == "x"

    monkeypatch.setattr(core, "desk_gaps", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("表没建")))
    out = tools.open_project("p-tuge", _user_id=ME)
    assert "desk_gap" not in out and out["p0"] == []
    listed = tools.list_projects(_user_id=ME)
    assert "desk_gap" not in listed and listed["projects"] == [{"id": "p-tuge"}]
