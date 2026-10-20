"""一批之内、批与批之间写得不一样(2026-10-20 生产诊断: 稿子篇幅短、批批长得像, 飞书里大半被人重写)。

第一批四处小改, 都是"给模型的材料本身就在把它往同一个方向推":

  1. 简报带【最近发出去 / 写过的标题】(recent_titles): 模型是无状态的, 不知道这个项目上周发了什么。
     · 发出去的 = tv-sync 对到本项目上的笔记, 窗口按 truth_vault.notes.publish_time(只取 id 和时间两列);
       ingested 取版本标题(那就是发出去的), 其余优先 tv_edit_diffs.published_title(人可能改过标题);
       ambiguous / 没版本的不算; 对到别的写作台项目上的不算; 排期还没发的不算
     · 写过的 = 本项目近 14 天的写作台批次, 补录副本不算, 和发出去的重复的不再列
     · 每条 ≤40 字、合计 ≤20 条, 写过的留 8 条份额(活跃项目发出去的远超 20 条, 不留的话昨天写的永远进不来),
       哪栏不够空位给另一栏; 不带正文; 查不到就不带这一栏, 简报照给
     · task 不要求"标题句式不一样"(那是发牌的硬坐标), 只管说法 / 人设 / 场景, 并写明谁优先
  2. draw_angles 一批之内错开内容形式和标题句式: n=8 时 8 种各一, n=10 时同一种最多两篇; 避重照旧
  3. 正例排不出相关性时【轮换】, 不再是"最近 N 条": 写作台版本从来没有向量(09-01 起 1,011 个, 0 个有),
     原来每条都是 -1.0、稳定排序原样交回最近的 N 条, mode 还报 relevance; 只有几条有向量时, 没向量的那一截
     也按天轮换(洗牌在打分之前)
  4. 已验证规律旁边带用法(validated_patterns_usage): 每篇最多一条、轮换、有几篇不用 —— 原来 4 条规律
     被模型全用在每一篇上, 批批同一副骨架; 轮换起点按天换, 两篇以上的批次一定有一篇不用
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

import librarian_client as lib
from deskcore import core, store, tools
from tests.fakes import FakeClient

ME = "11111111-1111-1111-1111-111111111111"
MATE = "22222222-2222-2222-2222-222222222222"
PID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SIB = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"      # 同一个 TV 项目对着的另一个写作台项目
NOW = datetime.now(timezone.utc)


def _ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat()


def _project(pid=PID, name="测试项目"):
    return {"id": pid, "name": name, "brand": "测试品", "owner_id": ME, "system_prompt": "你是文案",
            "calibration_notes": "", "tactics": "[]", "default_params": "{}"}


# ══════════════════════════════════════════════════════════════════════
# 1. 最近的标题
# ══════════════════════════════════════════════════════════════════════

class _World:
    """一个 TV 项目(SPX)对着两个写作台项目; 笔记、对照、差异、版本、批次按需往里加。"""

    def __init__(self):
        self.notes: list[dict] = []
        self.links: list[dict] = []
        self.diffs: list[dict] = []
        self.versions: list[dict] = []
        self.batches: list[dict] = []
        self.items: list[dict] = []

    def published(self, nid, title, *, kind="body_exact", days=1, pid=PID, published_title=None,
                  publish_time=None):
        vid = f"v-{nid}"
        self.versions.append({"id": vid, "title": title})
        self.notes.append({"note_id": nid, "project_id": "SPX", "publish_time": publish_time or _ago(days=days),
                           "title": "(TV 的标题不该被取)", "body": "(正文不该被取)", "raw_content": "(不该被取)"})
        self.links.append({"note_id": nid, "tv_project_id": "SPX", "project_id": pid,
                           "version_id": None if kind in ("ambiguous", "unmatched") else vid, "match_kind": kind})
        if published_title is not None:
            self.diffs.append({"note_id": nid, "project_id": pid, "published_title": published_title})

    def drafted(self, bid, titles, *, days=1, source="deskcore", pid=PID):
        self.batches.append({"id": bid, "project_id": pid, "params": {"source": source}, "created_at": _ago(days=days)})
        for i, t in enumerate(titles):
            iid = f"{bid}-i{i}"
            self.items.append({"id": iid, "batch_id": bid, "best_version_id": f"{iid}-v2",
                               "created_at": _ago(days=days, seconds=i),
                               "versions": [{"id": f"{iid}-v1", "title": "改稿前的旧标题", "version_num": 1},
                                            {"id": f"{iid}-v2", "title": t, "version_num": 2}]})

    def client(self, **kw):
        return FakeClient(rows={
            "projects": [_project(), _project(SIB, "测试项目-方向二")],
            "tv_project_map": [{"tv_project_id": "SPX", "project_id": PID, "ingest_target": True},
                               {"tv_project_id": "SPX", "project_id": SIB, "ingest_target": False}],
            "truth_vault.notes": self.notes, "tv_note_links": self.links, "tv_edit_diffs": self.diffs,
            "versions": self.versions, "batches": self.batches, "items": self.items,
            "memories": [], "user_calibration_notes": [], "draft_fingerprints": [], "angle_ledger": [],
            "style_edits": [],
        }, **kw)


def test_published_titles_come_from_links_to_this_project_newest_first():
    w = _World()
    w.published("n-old", "一个月前发的", days=31)                           # 窗口外
    w.published("n-ing", "补录的那篇就是发出去的标题", kind="ingested", days=5)
    w.published("n-edit", "写作台原标题", kind="edited", days=3, published_title="人改过的标题")
    w.published("n-body", "没算过差异的那篇", kind="body_exact", days=2)     # 没有差异行 → 退回版本标题
    w.published("n-amb", "判不出是哪篇", kind="ambiguous", days=1)
    w.published("n-sib", "对到另一个方向上的", days=1, pid=SIB)
    w.published("n-sched", "排期还没发", publish_time=(NOW + timedelta(days=2)).isoformat())
    w.published("n-new", "昨天发的", kind="tv_lineage", days=0.5, published_title="")
    got = store.recent_published_titles(w.client(), PID, days=30, limit=20)
    assert got == ["昨天发的", "没算过差异的那篇", "人改过的标题", "补录的那篇就是发出去的标题"], got


def test_truth_vault_notes_are_read_by_id_and_time_only():
    w = _World()
    w.published("n1", "标题一")
    c = w.client()
    assert store.recent_titles(c, PID)["published"] == ["标题一"]
    sel = [q for q in c.calls if q["table"] == "truth_vault.notes"]
    assert sel and all(q["cols"] == "note_id, publish_time" for q in sel), sel
    assert not c.rpc_calls, "deskcore_tv_notes 带正文, 不该走它"
    assert all("body" not in (q["cols"] or "") for q in c.calls), "全程不取正文"


def test_drafted_titles_skip_ingest_copies_old_batches_and_published_duplicates():
    w = _World()
    w.published("n1", "已经发出去的这一篇！", kind="ingested")
    w.drafted("b-new", ["今天写的第一篇", "已经发出去的 这一篇"], days=0.1)   # 第二篇和发出去的只差标点空格
    w.drafted("b-ing", ["补录副本的标题"], days=0.2, source="ingest")
    w.drafted("b-mid", ["上周写的"], days=7)
    w.drafted("b-old", ["半个月前写的"], days=15)
    w.drafted("b-sib", ["别的方向写的"], days=1, pid=SIB)
    got = store.recent_titles(w.client(), PID)
    assert got == {"published": ["已经发出去的这一篇！"], "drafted": ["今天写的第一篇", "上周写的"]}, got


def test_titles_are_truncated_deduped_and_capped_with_a_share_for_drafted():
    w = _World()
    long = "这是一个特别特别长的标题" * 6
    for i in range(15):
        w.published(f"n{i:02d}", f"发出去的第{i}篇", days=1 + i / 10)
    w.published("n-long", long, days=0.5)
    w.published("n-dup", "发出去的第3篇", days=3)                              # 同标题发了两次
    w.drafted("b1", [f"写过的第{i}篇" for i in range(12)], days=1)
    got = store.recent_titles(w.client(), PID, limit=20)
    assert got["published"][0] == long[:store.RECENT_TITLE_CHARS]
    assert all(len(t) <= store.RECENT_TITLE_CHARS for t in got["published"] + got["drafted"])
    assert got["published"].count("发出去的第3篇") == 1
    assert len(got["published"]) == 12 and got["drafted"] == [f"写过的第{i}篇" for i in range(8)], \
        "合计封顶 20: 写过的留 8 条, 发出去的占其余"


def test_an_active_project_still_shows_yesterdays_drafts():
    """10-20 review: 30 天里对上的笔记远超 20 条时, 发出去的不能把 20 条占满 —— 昨天写了还没发的几篇才是
    下一批最容易撞的。"""
    w = _World()
    for i in range(30):
        w.published(f"n{i:02d}", f"发出去的第{i}篇", days=1 + i / 2)
    w.drafted("b-yday", ["昨天写的一", "昨天写的二", "昨天写的三"], days=1)
    got = store.recent_titles(w.client(), PID, limit=20)
    assert got["drafted"] == ["昨天写的一", "昨天写的二", "昨天写的三"]
    assert len(got["published"]) == 17, "写过的不够 8 条, 空位还给发出去的"

    only_published = _World()
    for i in range(30):
        only_published.published(f"n{i:02d}", f"发出去的第{i}篇", days=1 + i / 2)
    got = store.recent_titles(only_published.client(), PID, limit=20)
    assert len(got["published"]) == 20 and got["drafted"] == []

    only_drafted = _World()
    only_drafted.published("n1", "就发了这一篇")
    only_drafted.drafted("b1", [f"写过的第{i}篇" for i in range(30)], days=1)
    got = store.recent_titles(only_drafted.client(), PID, limit=20)
    assert got["published"] == ["就发了这一篇"] and len(got["drafted"]) == 19


def test_a_page_of_notes_for_other_directions_does_not_hide_this_projects_titles():
    """同一个 TV 项目对着好几个方向(WTG 15 个): 最新的一整页都是别的方向的, 本项目的在后面也要找得到。"""
    w = _World()
    for i in range(store.RECENT_NOTES_PAGE + 5):
        w.published(f"s{i:03d}", f"方向二的第{i}篇", days=0.5, pid=SIB)
    w.published("mine", "本项目的那篇", days=3)
    assert store.recent_titles(w.client(), PID)["published"] == ["本项目的那篇"]


def test_one_column_failing_does_not_take_the_other_down():
    """truth_vault 读不到(没暴露 / 本地库)时, 写作台自己写过的那栏照给。"""
    w = _World()
    w.published("n1", "发出去的")
    w.drafted("b1", ["写过的"])
    c = w.client()
    real = c.schema

    def broken(name):
        if name == "truth_vault":
            raise RuntimeError("PGRST106 schema must be one of the exposed schemas")
        return real(name)
    c.schema = broken
    assert store.recent_titles(c, PID) == {"published": [], "drafted": ["写过的"]}


def test_missing_edit_diffs_table_falls_back_to_the_desk_title():
    w = _World()
    w.published("n1", "写作台那版标题", kind="fuzzy", published_title="人改过的")
    c = w.client(missing_columns={"tv_edit_diffs": {"published_title"}})
    assert store.recent_titles(c, PID)["published"] == ["写作台那版标题"]


def _brief(client, **kw):
    return core.build_writing_brief(client, PID, user_id=ME, **kw)


def test_brief_carries_recent_titles_with_a_task():
    w = _World()
    w.published("n1", "发出去的标题", kind="ingested")
    w.drafted("b1", ["写过的标题"])
    out = _brief(w.client())
    rt = out["recent_titles"]
    assert rt["published"] == ["发出去的标题"] and rt["drafted"] == ["写过的标题"]
    assert rt["task"] == core.RECENT_TITLES_TASK
    for word in ("主干词", "开头", "人设", "场景", "同一批", "不要模仿"):
        assert word in rt["task"], word
    assert set(rt) == {"published", "drafted", "task"}, "只给标题, 不带正文和内部 id"


def test_recent_titles_task_does_not_fight_the_drawn_title_structure():
    """10-20 review: 标题句式是 draw_angles 发的硬坐标(8 种), 近 20 条标题几乎总把它们占全 —— task 要是还说
    "标题句式要和它们不一样", 两句话就没法同时做到。句式照发牌写, 并且写明发牌坐标 / p0 / 改稿优先。"""
    task = core.RECENT_TITLES_TASK
    assert "标题句式照 draw_angles 分到的写" in task
    assert "句式、开头写法" not in task and "标题句式都要" not in task
    for word in ("优先级", "p0", "stable", "切入角度", "改稿", "尽量"):
        assert word in task, word
    doc = tools.open_project.__doc__ or ""
    assert "标题句式照" in doc and "优先于它" in doc


def test_brief_omits_recent_titles_when_there_are_none():
    assert "recent_titles" not in _brief(_World().client())


def test_brief_survives_recent_titles_blowing_up(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(store, "recent_titles", boom)
    out = _brief(_World().client())
    assert "recent_titles" not in out and out["project_id"] == PID


def test_open_project_doc_tells_the_model_about_recent_titles():
    doc = tools.open_project.__doc__ or ""
    assert "recent_titles" in doc and "不要模仿" in doc


# ══════════════════════════════════════════════════════════════════════
# 2. 发牌: 一批之内错开
# ══════════════════════════════════════════════════════════════════════

AVOID: set[str] = set()


def _reserve(args):
    """仿 deskcore_reserve_angles: 按候选先后, 跳过避重集, 取前 _want 个。"""
    out = []
    for c in args["_candidates"]:
        if c["angle_key"] in AVOID:
            continue
        out.append({"reserved_key": c["angle_key"], "reserved_dims": c["dims"]})
        if len(out) >= args["_want"]:
            break
    return out


def _draw_client(*, rpc: bool, ledger=()):
    return FakeClient(rows={"projects": [_project()], "angle_ledger": list(ledger)},
                      rpc_impl={"deskcore_reserve_angles": _reserve} if rpc else None)


@pytest.mark.parametrize("rpc", [True, False], ids=["rpc", "fallback"])
@pytest.mark.parametrize("seed", range(12))
def test_eight_angles_cover_eight_formats_and_eight_title_structures(rpc, seed):
    out = core.draw_angles(_draw_client(rpc=rpc), PID, 8, user_id=ME, seed=seed)
    assert out["delivered"] == 8
    assert len({a["dims"]["content_format"] for a in out["angles"]}) == 8
    assert len({a["dims"]["title_structure"] for a in out["angles"]}) == 8


@pytest.mark.parametrize("rpc", [True, False], ids=["rpc", "fallback"])
@pytest.mark.parametrize("seed", range(12))
def test_ten_angles_use_no_format_more_than_twice(rpc, seed):
    out = core.draw_angles(_draw_client(rpc=rpc), PID, 10, user_id=ME, seed=seed)
    fmts = Counter(a["dims"]["content_format"] for a in out["angles"])
    sts = Counter(a["dims"]["title_structure"] for a in out["angles"])
    assert max(fmts.values()) <= 2 and max(sts.values()) <= 2, (fmts, sts)
    assert len({a["angle_key"] for a in out["angles"]}) == 10


def test_small_batches_never_repeat_a_format():
    for seed in range(30):
        out = core.draw_angles(_draw_client(rpc=True), PID, 5, user_id=ME, seed=seed)
        assert len({a["dims"]["content_format"] for a in out["angles"]}) == 5


@pytest.mark.parametrize("rpc", [True, False], ids=["rpc", "fallback"])
def test_avoided_keys_are_still_never_returned(rpc):
    """先看同一个 seed 本来会发哪几张, 把它们(和再往后的十几张)记成近期用过的, 再发一次: 一张都不能回来。"""
    first = core.draw_angles(_draw_client(rpc=True), PID, 8, user_id=ME, seed=3)
    avoid = {a["angle_key"] for a in first["angles"]}
    second = core.draw_angles(_draw_client(rpc=True, ledger=()), PID, 24, user_id=ME, seed=3)
    avoid |= {a["angle_key"] for a in second["angles"]}
    AVOID.clear()
    AVOID.update(avoid)
    try:
        ledger = [{"id": f"l{i}", "project_id": PID, "angle_key": k, "dims": {}, "drawn_by": ME,
                   "drawn_at": _ago(days=3), "consumed_version_id": f"v{i}", "consumed_at": _ago(days=2)}
                  for i, k in enumerate(sorted(avoid))]
        out = core.draw_angles(_draw_client(rpc=rpc, ledger=ledger), PID, 8, user_id=ME, seed=3)
    finally:
        AVOID.clear()
    got = {a["angle_key"] for a in out["angles"]}
    assert out["delivered"] == 8 and not (got & avoid)
    if not rpc:   # 降级路径先去掉避重的再排, 错开不打折
        assert len({a["dims"]["content_format"] for a in out["angles"]}) == 8


def test_same_seed_same_draw():
    a = core.draw_angles(_draw_client(rpc=True), PID, 6, user_id=ME, seed=42)
    b = core.draw_angles(_draw_client(rpc=True), PID, 6, user_id=ME, seed=42)
    assert [x["angle_key"] for x in a["angles"]] == [x["angle_key"] for x in b["angles"]]
    assert [x["angle_name"] for x in a["angles"]] == [x["angle_name"] for x in b["angles"]]


# ══════════════════════════════════════════════════════════════════════
# 3. 正例轮换
# ══════════════════════════════════════════════════════════════════════

def _pool(n=30, *, embedded=()):
    # 正文开头各不相同: cap_by_shape 按开头形态限额, 开头一样的只会给两条
    return [{"item_id": f"it{i:02d}", "title": f"正例{i}", "body": f"第{i:02d}号正例的开头各不相同, 后面是正文。",
             "embedding": [1.0, 0.0] if i in embedded else None} for i in range(n)]


def _bj(y, m, d, h):
    return datetime(y, m, d, h, tzinfo=timezone(timedelta(hours=8)))


def test_without_any_vectors_it_rotates_instead_of_returning_the_newest(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: True)

    def no_call(*a, **k):
        raise AssertionError("候选一条向量都没有, 不该去算 brief 的向量")
    monkeypatch.setattr(core.dedup, "embed_texts", no_call)
    pool = _pool()
    picked, mode = core.select_positive_examples(pool, {"draft_topic": "通勤"}, project_id=PID, user_id=ME,
                                                 now=_bj(2026, 10, 20, 9))
    assert mode == "rotating" and len(picked) == core.MAX_POSITIVE
    assert [p["item_id"] for p in picked] != [p["item_id"] for p in pool[:core.MAX_POSITIVE]]


def test_rotation_is_stable_within_a_beijing_day_and_moves_across_days():
    pool = _pool()

    def ids(**kw):
        return [p["item_id"] for p in core.select_positive_examples(pool, {}, project_id=PID, **kw)[0]]
    morning = ids(user_id=ME, now=_bj(2026, 10, 20, 1))
    assert morning == ids(user_id=ME, now=_bj(2026, 10, 20, 23)), "同一个北京日里几场看到的是同一批"
    assert morning != ids(user_id=ME, now=_bj(2026, 10, 21, 1))
    assert morning != ids(user_id=MATE, now=_bj(2026, 10, 20, 1)), "各人各轮"
    seen = set()
    for d in range(14):
        seen.update(ids(user_id=ME, now=_bj(2026, 10, 1, 9) + timedelta(days=d)))
    assert len(seen) >= 20, f"两周下来应该轮过大半个池子, 只见到 {len(seen)} 条"


def test_no_brief_text_also_rotates(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: True)
    _picked, mode = core.select_positive_examples(_pool(embedded={1, 2}), {}, project_id=PID, user_id=ME)
    assert mode == "rotating"


def test_relevance_is_kept_when_some_candidates_have_vectors(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: True)
    monkeypatch.setattr(core.dedup, "embed_texts", lambda texts: [[1.0, 0.0]])
    picked, mode = core.select_positive_examples(_pool(embedded={17}), {"draft_topic": "通勤"},
                                                 project_id=PID, user_id=ME)
    assert mode == "relevance" and picked[0]["item_id"] == "it17"


def test_the_unembedded_tail_rotates_too(monkeypatch):
    """10-20 review: 只有一两条有向量(网页端生成路径写过 versions.embedding)时, 其余仍都是 -1.0 —— 洗牌要在打分
    之前, 不然剩下的空位每天都是最新那几条。"""
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: True)
    monkeypatch.setattr(core.dedup, "embed_texts", lambda texts: [[1.0, 0.0]])
    pool = _pool(60, embedded={40, 55})
    tails, seen = set(), set()
    for d in range(10):
        picked, mode = core.select_positive_examples(pool, {"draft_topic": "通勤"}, project_id=PID, user_id=ME,
                                                     now=_bj(2026, 10, 10, 9) + timedelta(days=d))
        ids = [p["item_id"] for p in picked]
        assert mode == "relevance" and set(ids[:2]) == {"it40", "it55"}, ids
        tails.add(tuple(ids[2:]))
        seen.update(ids[2:])
    assert len(tails) >= 8, "没向量的那一截要按天换"
    assert not {"it00", "it01", "it02"} >= seen, "不能每天都是最新那几条"
    assert len(seen) >= 15


def test_the_brief_reports_rotating(monkeypatch):
    monkeypatch.setattr(core.dedup, "embeddings_available", lambda: False)
    items = [{"id": f"it{i}", "batch_id": "b1", "best_version_id": f"v{i}", "created_at": _ago(days=i),
              "user_id": ME, "example_label": "positive", "batches": {"project_id": PID},
              "versions": [{"id": f"v{i}", "title": f"正例{i}", "body": f"第{i:02d}号正例的开头各不相同。",
                            "version_num": 1, "embedding": None}]} for i in range(12)]
    c = FakeClient(rows={"projects": [_project()], "items": items, "memories": [],
                         "user_calibration_notes": [], "angle_ledger": []})
    out = core.build_writing_brief(c, PID, user_id=ME, brief={"draft_topic": "通勤"})
    assert out["positive_selection_mode"] == "rotating"
    assert out["counts"]["positive_examples"] == core.MAX_POSITIVE


# ══════════════════════════════════════════════════════════════════════
# 4. 已验证规律的用法
# ══════════════════════════════════════════════════════════════════════

PATTERNS = [{"id": f"p{i}", "label": f"规律{i}", "instruction": f"写法{i}", "evidence": "OR 2.0"} for i in range(4)]


class _Librarian:
    def __call__(self, brief, *, status=None):
        if status is not None:
            status.update({"state": lib.BORROW_BORROWED, "count": 0, "elapsed_ms": 1, "detail": "",
                           "patterns": [] if brief.get("explore") else list(PATTERNS)})
        return []


@pytest.fixture
def librarian(monkeypatch):
    monkeypatch.setattr(lib, "fetch_flywheel_lessons", _Librarian())
    with core._borrowed_arms_lock:
        core._borrowed_arms.clear()
    yield
    with core._borrowed_arms_lock:
        core._borrowed_arms.clear()


def test_brief_and_borrow_say_one_pattern_per_draft_and_rotate(monkeypatch, librarian):
    monkeypatch.setattr(core, "flywheel_arm", lambda pid, uid, **kw: "patterns")
    out = _brief(_World().client())
    assert out["validated_patterns"] == PATTERNS, "清单原样转交, 不改"
    usage = out["validated_patterns_usage"]
    for phrase in ("每篇最多用其中一条", "轮换", "一条都不用", "不是模板", "第 5 篇", "最后一篇改成一条都不用",
                   "以坐标和 p0 为准"):
        assert phrase in usage, phrase
    got = core.borrow_lessons(_World().client(), PID, user_id=ME)
    assert got["validated_patterns_usage"] == usage


def test_no_usage_note_without_patterns(monkeypatch, librarian):
    monkeypatch.setattr(core, "flywheel_arm", lambda pid, uid, **kw: "explore")
    out = _brief(_World().client())
    assert "validated_patterns" not in out and "validated_patterns_usage" not in out
    assert "validated_patterns_usage" not in core.borrow_lessons(_World().client(), PID, user_id=ME)


def test_single_pattern_alternates():
    texts = {core.validated_patterns_usage(1, seed) for seed in range(4)}
    assert texts == {t for t in texts if "单数篇用它, 双数篇不用" in t or "单数篇不用, 双数篇用它" in t}
    assert len(texts) == 2, "两种起点都会出现"


def test_pattern_rotation_starts_somewhere_new_each_day_and_always_has_a_skip():
    """10-20 review: 固定从第 1 条起, 每批第 1 篇都是同一条规律; 3 篇一批、4 条规律时也轮不到"不用"。"""
    for seed in range(10):
        rota = core.pattern_rotation(4, seed)
        assert sorted(rota, key=lambda p: p or 0) == [None, 1, 2, 3, 4], rota
    assert {core.pattern_rotation(4, seed)[0] for seed in range(10)} == {None, 1, 2, 3, 4}
    starts = {core._daily_rotation_seed(PID, ME, purpose="patterns", now=_bj(2026, 10, 1, 9) + timedelta(days=d)) % 5
              for d in range(14)}
    assert len(starts) >= 3, "起点按天换"
    usage = core.validated_patterns_usage(4, 2)
    assert usage.index("第 1 篇用第 3 条") < usage.index("第 3 篇一条都不用") < usage.index("第 5 篇用第 2 条")
    assert "两篇以上" in usage and "最后一篇改成一条都不用" in usage


def test_open_project_and_borrow_give_the_same_rotation_on_the_same_day(monkeypatch, librarian):
    monkeypatch.setattr(core, "flywheel_arm", lambda pid, uid, **kw: "patterns")
    a = _brief(_World().client())["validated_patterns_usage"]
    b = core.borrow_lessons(_World().client(), PID, user_id=ME)["validated_patterns_usage"]
    assert a == b == core._patterns_usage_for(PID, ME, len(PATTERNS))


def test_tool_docs_say_at_most_one_pattern_per_draft():
    for fn in (tools.open_project, tools.borrow_lessons):
        doc = fn.__doc__ or ""
        assert "每篇最多用" in doc and "不是模板" in doc and "轮换" in doc, fn.__name__
