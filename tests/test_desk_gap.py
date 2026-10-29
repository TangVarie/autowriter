"""desk_gap (TV O-04, 2026-10-19): 你名下哪个项目最近发出去的帖写作台没接住, 在调到写作台的对话里告诉运营。

途鸽 9-16 之后写作台一次调用都没收到, 10-12 那周 22 篇全是补录副本, 同一位写手别的项目照常在写作台里写
(owner 确认写作台连接是全局的, 那一波是 skill 没被调起来)。漏接的那些对话里写作台没被调用, 只能在下一场说。

  1. 途鸽形状(7 天内发出去 22 篇全是补录、60 天内发过牌) → 报; 发布时间不在窗口里的不算
  2. 窗口按【发布时间】, 不按对照写进去的时间: 新接一张表 / tv-sync 补跑, 旧笔记同时拿到新 matched_at 也不报;
     排期还没发的(publish_time 在未来)也不算
  3. 下限: 补录差一篇、对上多一篇都不报; sportsix 那种"一部分在外面写"(12 / 18)不报;
     写作台那版晚于发布的对上(lag_days < 0, 抄回去的)不算接住
  4. 从没在写作台里写过的项目不唠叨; 用过的不管断了多久都照说(不设 60 天期限)
  5. 只数对到调用者自己项目上的对照: TV 项目映射到别人的项目也不算、不泄露
  6. 当前正开着的就是它 → 话术换成"这次写完记得打分存档"; 名字取补录目标那个项目
  7. 缓存按 user_id(store.client() 每次新建, 不能拿 client 当键), 过期的会被清掉
  8. open_project / list_projects 带上它; 它查挂了清单和简报照给
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


def _client(*, ing=22, caught=0, drew_days=30, owner=ME, stale_links=0, other_owner_links=0,
            late_copies=0, never_drew=False):
    projects = [{"id": "p-tuge", "name": "途鸽", "brand": "途鸽", "owner_id": owner},
                {"id": "p-tuge-d1", "name": "途鸽D-1", "brand": "途鸽", "owner_id": owner},
                {"id": "p-tuge-x", "name": "途鸽求职", "brand": "途鸽", "owner_id": OTHER},
                {"id": "p-spx", "name": "sportsix", "brand": "sportsix", "owner_id": ME}]
    tvmap = [{"tv_project_id": "TUGE", "project_id": "p-tuge-d1", "ingest_target": False, "note": ""},
             {"tv_project_id": "TUGE", "project_id": "p-tuge", "ingest_target": True, "note": ""},
             {"tv_project_id": "TUGE", "project_id": "p-tuge-x", "ingest_target": False, "note": ""},
             {"tv_project_id": "SPX", "project_id": "p-spx", "ingest_target": True, "note": ""}]
    notes: list[dict] = []
    links: list[dict] = []

    def add(nid, tv, kind, pid, published, created=None, lag=1):
        notes.append({"note_id": nid, "project_id": tv, "publish_time": published, "body": "（正文不该被取）"})
        links.append({"note_id": nid, "tv_project_id": tv, "project_id": pid, "match_kind": kind,
                      "lag_days": lag, "matched_at": _ago(hours=1)})   # 全都是刚连上的 —— 窗口不该按它切

    for i in range(ing):
        add(f"t{i:03d}", "TUGE", "ingested", "p-tuge", _ago(days=2))
    for i in range(caught):
        add(f"c{i:03d}", "TUGE", "fuzzy", "p-tuge-d1", _ago(days=2))
    add("amb", "TUGE", "ambiguous", None, _ago(days=2))
    for i in range(stale_links):                              # 20 天前发的, 今天才连上(新接表 / 补跑)
        add(f"old{i:03d}", "TUGE", "ingested", "p-tuge", _ago(days=20), created=_ago(days=1))
    for i in range(5):                                        # 排期: 已入库, 还没发
        add(f"sched{i}", "TUGE", "ingested", "p-tuge", (NOW + timedelta(days=2)).isoformat(), created=_ago(days=1))
    for i in range(late_copies):                              # 发了之后才进写作台的那版: 抄回去的
        add(f"late{i:03d}", "TUGE", "body_exact", "p-tuge", _ago(days=2), lag=-3)
    for i in range(other_owner_links):                        # 对到别人项目上的
        add(f"x{i:03d}", "TUGE", "ingested", "p-tuge-x", _ago(days=2))
    for i in range(12):
        add(f"s{i:03d}", "SPX", "ingested", "p-spx", _ago(days=1))
    for i in range(18):
        add(f"sb{i:03d}", "SPX", "body_exact", "p-spx", _ago(days=1))
    ledger = ([] if never_drew else [{"id": "a1", "project_id": "p-tuge", "drawn_at": _ago(days=drew_days)}]) + [
              {"id": "a2", "project_id": "p-spx", "drawn_at": _ago(days=1)},
              {"id": "a3", "project_id": "p-tuge-x", "drawn_at": _ago(days=1)}]
    # 不配 deskcore_tv_notes: 那个 RPC 带正文, desk_gap 不该走它(调了会抛 PGRST202)
    return FakeClient(rows={"projects": projects, "tv_project_map": tvmap, "tv_note_links": links,
                            "angle_ledger": ledger, "truth_vault.notes": notes})


@pytest.fixture(autouse=True)
def _fresh_cache():
    core._desk_gap_cache.clear()
    yield
    core._desk_gap_cache.clear()


def _gap(c, user=ME, **kw):
    core._desk_gap_cache.clear()
    return core.desk_gaps(c, user_id=user, **kw)


def test_the_tuge_shape_is_reported():
    gap = _gap(_client())
    assert [p["tv_project_id"] for p in gap["projects"]] == ["TUGE"], "sportsix 12 / 18 不该报"
    (p,) = gap["projects"]
    assert (p["project"], p["published"], p["caught"]) == ("途鸽", 22, 0), "ambiguous / 排期未发的不算"
    assert "「途鸽」最近 7 天发出去 22 篇" in gap["message"] and "打开途鸽" in gap["message"]
    # W-38(10-29, owner): 写手没有「存档」这一步 —— 提醒只叫写手在连着写作台的对话里写, 不叫打分 / 存档
    assert "连着写作台的对话里写" in gap["message"] and "写作台会自己对回来" in gap["message"]
    assert "存档" not in gap["message"] and "打分" not in gap["message"], gap["message"]
    assert "project_ids" not in p, "给模型看的不带内部 id 列表"
    assert gap["task"] == core.DESK_GAP_TASK


def test_reads_only_ids_from_truth_vault_notes_not_the_body_rpc():
    c = _client()
    assert _gap(c) is not None
    assert not c.rpc_calls, "不该走 deskcore_tv_notes(它把正文一起带回来)"
    sel = [q for q in c.calls if q["table"] == "truth_vault.notes"]
    assert sel and all("body" not in q["cols"] for q in sel), sel


def test_window_is_publish_time_not_link_time():
    """新接表 / 补跑: 30 篇 20 天前发的笔记今天才连上, 不该被说成最近 7 天发的。"""
    assert _gap(_client(ing=0, stale_links=30)) is None
    assert _gap(_client(ing=9, stale_links=30)) is None, "窗口里只有 9 篇, 旧的不能凑数"


def test_floors():
    assert _gap(_client(ing=core.DESK_GAP_MIN_UNCAUGHT - 1)) is None
    assert _gap(_client(ing=10, caught=core.DESK_GAP_MAX_CAUGHT + 1)) is None
    assert _gap(_client(ing=10, caught=core.DESK_GAP_MAX_CAUGHT)) is not None


def test_copies_made_after_publication_are_not_catches():
    """Hatherine 这周唯一一篇"对上"是发了之后才进写作台的那版(lag_days < 0): 那是抄回去的, 不算接住。"""
    g = _gap(_client(ing=10, late_copies=5))
    assert g is not None and g["projects"][0]["caught"] == 0


def test_never_used_is_left_alone_but_a_long_outage_keeps_being_reported():
    assert _gap(_client(never_drew=True)) is None
    assert _gap(_client(drew_days=200)) is not None, "断了半年也照说, 不能自己转绿"


def test_only_links_on_my_own_projects_count():
    assert _gap(_client(owner=OTHER)) is None
    assert _gap(_client(), user=None) is None
    # 别人(OTHER 名下的途鸽求职)那边补录了 15 篇: 我这边 9 篇不够门槛, 不能借别人的数凑够
    assert _gap(_client(ing=9, other_owner_links=15)) is None
    # OTHER 自己: 映射里有他的途鸽求职、60 天内发过牌, 但对到他项目上的只有他那 15 篇 → 报他自己的数
    g = _gap(_client(ing=22, other_owner_links=15), user=OTHER)
    assert [(p["project"], p["published"]) for p in g["projects"]] == [("途鸽求职", 15)], g


def test_the_project_being_opened_gets_the_write_and_score_wording():
    gap = _gap(_client(), current_project_id="p-tuge-d1")
    assert "这次就在写作台里写" in gap["message"] and "打开途鸽" not in gap["message"]


def test_cached_per_user_and_expired_entries_are_pruned(monkeypatch):
    c = _client()
    assert core.desk_gaps(c, user_id=ME) is not None
    c.rows["tv_note_links"].clear()
    # store.client() 每次新建: 换一个 client 对象也该命中缓存
    assert core.desk_gaps(_client(ing=0), user_id=ME) is not None, "10 分钟内按 user_id 命中"
    core._desk_gap_cache["stale-user"] = (core.time.monotonic() - core.DESK_GAP_CACHE_SEC - 1, [])
    core._desk_gap_cache.pop(ME)
    assert core.desk_gaps(c, user_id=ME) is None
    assert "stale-user" not in core._desk_gap_cache, "过期的要清掉, 不在进程里越攒越多"


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
