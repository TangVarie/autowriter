"""回流闸 v1 读的一方(写作台这一半): A4 学的那一路按结论筛, A8 同一个 TV 项目对着的几个写作台项目看得到彼此发出去的
标题, A5 补录副本不当正负例。判法在 tests/test_postgate.py, tv-sync 那一步在 tests/test_tv_sync_gate.py。

这里盯的几件事:
  · 开关 config.RETURN_GATE_ENFORCE 是代码里的默认值(关), 环境变量只是覆盖;
  · 关着、或者 020 没跑(读不到那几列)、或者那几列读挂了 —— 简报、项目块、发牌的回包同今天一字不差(整块比), 关着时连
    发出去的请求都一样;
  · 开着: working 标题和起量帖参照不用被挡的帖; published 那一栏、配比 / 计数 / 标签 / 提升一个数不变;
    被挡的帖照旧算进起量帖「前一成」的门槛(别的帖不会因为闸顶上来);
  · A8: 只看得到 tv_project_map 里对着的 TV 项目的帖, 没映射的 TV 项目的帖一篇都进不来; 同一个标题不列两遍;
  · A5: store.labeled_examples 永远不回补录副本。
文字全是编的。
"""
from __future__ import annotations

import copy
import json
import os
import re
import runpy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config
from deskcore import core, postgate, priors, store
from tests.fakes import FakeClient
from tests.test_desk_variety import _project

ME = "11111111-1111-1111-1111-111111111111"
PID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"       # 写手真在写的那个写作台项目(不是补录目标)
SIB = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"       # 同一个 TV 项目对着的另一个写作台项目(补录目标)
OTHER = "cccccccc-cccc-cccc-cccc-cccccccccccc"     # 对着别的 TV 项目的写作台项目
TV, TV_BRAND, TV_OTHER = "AAA_phase2", "AAA_phase1", "BBB_phase1"
NOW = datetime.now(timezone.utc)
PNOW = NOW                                         # 先验那一半的「现在」(纯函数的用例显式传它; 走缓存的用例走真时钟)
GATE_READ = "note_id, gate, gate_reasons"
ROOT = Path(__file__).resolve().parent.parent


def _ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat()


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setattr(config, "RETURN_GATE_ENFORCE", True)


@pytest.fixture(autouse=True)
def _gate_off_by_default(monkeypatch):
    """每条用例从关着开始(开发机的环境变量不该让「关着 = 今天」的用例跑在开着的口径上)。"""
    monkeypatch.setattr(config, "RETURN_GATE_ENFORCE", False)


def _verdict(gate, *reasons, hold=()):
    """对照上回流闸那两格的样子(postgate.pack_reasons 存进去的形状)。"""
    v = postgate.Verdict(gate, tuple(reasons), tuple(hold))
    return {"gate": gate, "gate_reasons": postgate.pack_reasons(v)}


FAIL_EFFICACY = _verdict("fail", "efficacy")
FAIL_NOT_CLEAN = _verdict("fail", "not_clean")
FAIL_RISK = _verdict("fail", "risk")
UNSURE = _verdict("unsure", "unsure")
HOLD = _verdict("hold", "hold", hold=("short_body",))
WITHDRAWN = _verdict("withdrawn", "withdrawn")
PASS = _verdict("pass")
BLOCKING = {"efficacy": FAIL_EFFICACY, "not_clean": FAIL_NOT_CLEAN, "risk": FAIL_RISK, "unsure": UNSURE, "hold": HOLD,
            "withdrawn": WITHDRAWN}


class _World:
    """TV 项目 AAA_phase2 对着两个写作台项目(PID 不是补录目标, SIB 是); BBB_phase1 对着 OTHER。笔记、对照、版本按需加。"""

    def __init__(self):
        self.notes: list[dict] = []
        self.links: list[dict] = []
        self.versions: list[dict] = []
        self.diffs: list[dict] = []
        self.batches: list[dict] = []
        self.items: list[dict] = []
        self.boosted: list[dict] = []
        self.answers: list[dict] = []

    def post(self, nid, title, *, days=5, comments=None, tier=None, kind="body_exact", pid=PID, tv=TV, verdict=None,
             flags=None, published_title=None):
        vid = f"v-{nid}"
        self.versions.append({"id": vid, "title": title})
        self.notes.append({"note_id": nid, "project_id": tv, "publish_time": _ago(days=days),
                           "comments_count": comments, "tier": tier, "data_quality_flags": flags,
                           "title": "(TV 的标题不该被取)", "raw_content": "(不该被取)"})
        link = {"note_id": nid, "tv_project_id": tv, "project_id": pid, "version_id": vid, "match_kind": kind,
                "matched_at": _ago(hours=len(self.links))}
        link.update(verdict or {})
        self.links.append(link)
        if published_title is not None:
            self.diffs.append({"note_id": nid, "project_id": pid, "published_title": published_title})
        return nid

    def drafted(self, bid, titles, *, days=1, pid=PID):
        self.batches.append({"id": bid, "project_id": pid, "params": {"source": "deskcore"}, "created_at": _ago(days=days)})
        for i, t in enumerate(titles):
            iid = f"{bid}-i{i}"
            self.items.append({"id": iid, "batch_id": bid, "best_version_id": f"{iid}-v1",
                               "created_at": _ago(days=days, seconds=i),
                               "versions": [{"id": f"{iid}-v1", "title": t, "version_num": 1}]})

    def client(self, *, gate_columns=True, cls=FakeClient, **kw):
        """``gate_columns=False``: 020 没跑 —— 对照上没有那四列, select 到它们按 PostgREST 的样子报错。"""
        links = copy.deepcopy(self.links)
        if not gate_columns:
            for l in links:
                for col in store.TV_GATE_COLS:
                    l.pop(col, None)
            kw["missing_columns"] = {**(kw.get("missing_columns") or {}), "tv_note_links": set(store.TV_GATE_COLS)}
        return cls(rows={
            "projects": [_project(PID), _project(SIB, "测试项目-方向二"), _project(OTHER, "另一个品")],
            "tv_project_map": [{"tv_project_id": TV, "project_id": PID, "ingest_target": False},
                               {"tv_project_id": TV, "project_id": SIB, "ingest_target": True},
                               {"tv_project_id": TV_OTHER, "project_id": OTHER, "ingest_target": True}],
            "truth_vault.notes": copy.deepcopy(self.notes), "tv_note_links": links,
            "truth_vault.v_paid_boost_events": list(self.boosted),
            "truth_vault.projects": [{"project_id": p, "category": "运动补给"} for p in (TV, TV_BRAND, TV_OTHER)],
            "truth_vault.note_feature_answers": list(self.answers),
            "tv_edit_diffs": list(self.diffs), "versions": list(self.versions), "batches": list(self.batches),
            "items": copy.deepcopy(self.items),
            "memories": [], "user_calibration_notes": [], "draft_fingerprints": [], "angle_ledger": [], "style_edits": [],
        }, **kw)


def _gate_reads(c) -> list[dict]:
    """专门去问结论的那种读(按 note_id 问 gate / gate_reasons)。"""
    return [q for q in c.calls if q["table"] == "tv_note_links" and q["op"] == "select" and q["cols"] == GATE_READ]


def _tv_link_reads(c) -> list[dict]:
    """A8 那次读: 按 TV 项目取对照, 连着回流闸那两格。"""
    return [q for q in c.calls if q["table"] == "tv_note_links" and q["op"] == "select"
            and any(f[1] == "tv_project_id" for f in q["filters"])]


def _unmap(c, pid=PID):
    """这个写作台项目不在对照表里(没接 TV 的项目 / 对照表被改过): A8 没东西可并, 结论要专门去问。"""
    c.rows["tv_project_map"] = [m for m in c.rows["tv_project_map"] if m["project_id"] != pid]
    return c


_STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(\+00:00|Z)?")


def _calls(c) -> list[tuple]:
    """假库收到的请求(表、动作、列、过滤、排序、范围), 用来比「发出去的请求一样不一样」。过滤里的时刻(窗口的起点, 跟着
    真时钟走)抹掉再比。"""
    return [(q["table"], q["op"], q["cols"],
             _STAMP.sub("<t>", json.dumps(q["filters"], sort_keys=True, default=str, ensure_ascii=False)),
             json.dumps(q["orders"], default=str), q["range"]) for q in c.calls]


def _fresh(fn):
    """从冷的进程缓存开始跑 ``fn``(先验、投流事件都是进程缓存: 不清的话第二个假库少收几次请求, 比不出一样不一样)。"""
    from tests.conftest import _InlinePool
    priors.reset_cache()
    priors._POOL[0] = _InlinePool()
    return fn()


class _Denied(FakeClient):
    """回流闸那几列在、但读不出来(权限 / 连接 —— 不是「020 没跑」): 带 gate 的 select 一律抛。"""

    def _execute(self, q):
        if q.table_name == "tv_note_links" and q.op == "select" and "gate" in (q.cols or ""):
            self.calls.append({"table": q.table_name, "op": q.op, "cols": q.cols, "filters": list(q.filters),
                               "range": q.rng, "count": q.count_mode, "orders": list(q.orders), "payload": q.payload})
            raise RuntimeError('{"code":"42501","message":"permission denied for table tv_note_links"}')
        return super()._execute(q)


# ══════════════════════════════════════════════════════════════════════
# 开关
# ══════════════════════════════════════════════════════════════════════

def _config_with(env: dict) -> dict:
    """换一套环境变量把 config.py 从头跑一遍(不动已经 import 的那个 config)。"""
    saved = dict(os.environ)
    try:
        os.environ.pop("RETURN_GATE_ENFORCE", None)
        os.environ.update(env)
        return runpy.run_path(str(ROOT / "config.py"))
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_the_flag_is_a_code_default_and_it_is_off():
    assert config.RETURN_GATE_ENFORCE_DEFAULT == "0"
    assert _config_with({})["RETURN_GATE_ENFORCE"] is False, "不设环境变量 = 关"
    src = (ROOT / "config.py").read_text(encoding="utf-8")
    assert src.count('RETURN_GATE_ENFORCE_DEFAULT = "0"') == 1, "打开是把这一行的 0 改成 1"


@pytest.mark.parametrize("value, on", [("1", True), ("true", True), ("on", True), ("TRUE", True),
                                       ("0", False), ("false", False), ("off", False), ("", False), ("yes", False)])
def test_an_environment_variable_can_override_the_default(value, on):
    assert _config_with({"RETURN_GATE_ENFORCE": value})["RETURN_GATE_ENFORCE"] is on


def test_the_flag_is_read_at_call_time(monkeypatch):
    assert store.return_gate_on() is False
    monkeypatch.setattr(config, "RETURN_GATE_ENFORCE", True)
    assert store.return_gate_on() is True


# ══════════════════════════════════════════════════════════════════════
# 读结论: store.tv_gate_blocked_note_ids / desk_gate_blocked
# ══════════════════════════════════════════════════════════════════════

def _verdict_world():
    w = _World()
    for name, v in BLOCKING.items():
        w.post(f"n-{name}", f"标题 {name}", verdict=v)
    w.post("n-pass", "放行的", verdict=PASS)
    w.post("n-never", "没判过的")
    w.post("n-odd", "不认识的结论", verdict={"gate": "later", "gate_reasons": {"reasons": ["efficacy"]}})
    w.post("n-noreason", "判过但原因读不出", verdict={"gate": "fail", "gate_reasons": None})
    w.post("n-newreason", "有不认识的原因", verdict={"gate": "fail", "gate_reasons": {"reasons": ["brand_new"]}})
    return w


def test_blocked_ids_are_decided_by_reason_for_the_desk_lane():
    w = _verdict_world()
    c = w.client()
    ids = [n["note_id"] for n in w.notes] + ["n-no-link-row"]
    got = store.tv_gate_blocked_note_ids(c, ids)
    assert got == {f"n-{k}" for k in BLOCKING} | {"n-noreason", "n-newreason"}
    assert all(q["cols"] == GATE_READ for q in _gate_reads(c)), "只取 note_id 和回流闸那两格, 不取对照别的列"
    # 教训卡那一路不挡 not_clean(同一个读法, 换一个常数)
    cards = store.tv_gate_blocked_note_ids(c, ids, blocking=postgate.BLOCKS_CARDS)
    assert cards == got - {"n-not_clean"}


def test_blocked_ids_are_read_in_chunks_and_none_means_020_is_missing():
    w = _World()
    for i in range(250):
        w.post(f"n{i:03d}", f"第{i}篇", verdict=FAIL_EFFICACY if i % 2 else PASS)
    c = w.client()
    got = store.tv_gate_blocked_note_ids(c, [n["note_id"] for n in w.notes])
    assert len(got) == 125
    reads = _gate_reads(c)
    assert len(reads) == 3 and all(len(f[2]) <= store.TV_GATE_READ_CHUNK for q in reads for f in q["filters"])
    assert store.tv_gate_blocked_note_ids(w.client(gate_columns=False), ["n001"]) is None
    with pytest.raises(RuntimeError):
        store.tv_gate_blocked_note_ids(w.client(cls=_Denied), ["n001"])
    assert store.tv_gate_blocked_note_ids(c, []) == set()


def test_desk_gate_blocked_never_filters_when_it_cannot_know(monkeypatch, caplog):
    w = _verdict_world()
    ids = [n["note_id"] for n in w.notes]
    c = w.client()
    assert store.desk_gate_blocked(c, ids) == frozenset() and not c.calls, "关着: 一个请求都不发"
    monkeypatch.setattr(config, "RETURN_GATE_ENFORCE", True)
    assert store.desk_gate_blocked(c, []) == frozenset() and not c.calls
    assert len(store.desk_gate_blocked(c, ids)) == len(BLOCKING) + 2
    assert store.desk_gate_blocked(w.client(gate_columns=False), ids) == frozenset(), "020 没跑 = 不筛"
    with caplog.at_level("WARNING"):
        assert store.desk_gate_blocked(w.client(cls=_Denied), ids, what="recent titles") == frozenset()
    assert "return gate verdicts unreadable" in caplog.text and "recent titles" in caplog.text


# ══════════════════════════════════════════════════════════════════════
# A4 · 简报的 working 标题
# ══════════════════════════════════════════════════════════════════════

def _working_world():
    w = _World()
    for i, (name, v) in enumerate(BLOCKING.items()):
        w.post(f"n-{name}", f"被挡的 {name}", comments=90 - i, verdict=v)
    w.post("n-pass", "放行的那篇", comments=60, verdict=PASS)
    w.post("n-never", "没判过的那篇", comments=50)
    w.post("n-odd", "结论不认识的那篇", comments=40, verdict={"gate": "later", "gate_reasons": {"reasons": ["efficacy"]}})
    w.post("n-noreason", "判过但原因读不出的", comments=35, verdict={"gate": "unsure", "gate_reasons": "not json"})
    w.post("n-cold", "没人评论的", comments=1, verdict=FAIL_EFFICACY)
    return w


def test_with_the_flag_off_the_working_column_is_todays(gate_on, monkeypatch):
    w = _working_world()
    monkeypatch.setattr(config, "RETURN_GATE_ENFORCE", False)
    c = w.client()
    got = _fresh(lambda: store.recent_titles(c, PID, working=5))
    assert got["working"] == ["被挡的 efficacy", "被挡的 not_clean", "被挡的 risk", "被挡的 unsure", "被挡的 hold"]
    assert not _gate_reads(c)
    # 同一个世界、对照上没有那四列(020 之前): 回的东西、发的请求都一样
    c0 = w.client(gate_columns=False)
    assert _fresh(lambda: store.recent_titles(c0, PID, working=5)) == got
    assert _calls(c0) == _calls(c)


def test_with_the_flag_on_working_drops_every_blocking_reason_and_keeps_no_verdict_rows(gate_on):
    w = _working_world()
    c = w.client()
    got = store.recent_titles(c, PID, working=5)
    assert got["working"] == ["放行的那篇", "没判过的那篇", "结论不认识的那篇"], got["working"]
    assert len(_tv_link_reads(c)) == 1 and not _gate_reads(c), "结论跟着这个 TV 项目的对照一次读回来, 不另问"
    # 没接对照表的项目(A8 没东西可并): 专门问一次, 只问够得上 working 的那几篇
    c2 = _unmap(w.client())
    assert store.recent_titles(c2, PID, working=5) == got
    reads = _gate_reads(c2)
    assert len(reads) == 1 and not _tv_link_reads(c2)
    asked = set(reads[0]["filters"][0][2])
    assert "n-cold" not in asked and "n-pass" in asked, "只问够得上 working 的那几篇"


def test_the_published_column_is_never_gated(gate_on, monkeypatch):
    w = _working_world()
    on = store.recent_titles(w.client(), PID, working=5)
    monkeypatch.setattr(config, "RETURN_GATE_ENFORCE", False)
    off = store.recent_titles(w.client(), PID, working=5)
    assert on["published"] == off["published"] and len(on["published"]) == len(w.notes)
    for name in BLOCKING:
        assert f"被挡的 {name}" in on["published"], "承诺功效的、删了的也是发过的标题, 照样要避开"
    assert on["drafted"] == off["drafted"]


def test_a_blocked_post_makes_room_for_the_next_qualifier(gate_on):
    w = _World()
    w.post("n-top", "评论最多、承诺了功效", comments=500, verdict=FAIL_EFFICACY)
    for i in range(12):
        w.post(f"n{i:02d}", f"够格的第{i:02d}篇", comments=100 - i, verdict=PASS)
    got = store.recent_titles(w.client(), PID, working=5)["working"]
    assert got == [f"够格的第{i:02d}篇" for i in range(5)], "挡掉一篇不让这一栏变短: 在截断之前筛"


def test_working_may_run_short_when_the_gate_leaves_fewer_than_five(gate_on):
    w = _World()
    w.post("n1", "删了的", comments=80, verdict=WITHDRAWN)
    w.post("n2", "留下的", comments=70, verdict=PASS)
    got = store.recent_titles(w.client(), PID, working=5)
    assert got["working"] == ["留下的"] and set(got["published"]) == {"删了的", "留下的"}


@pytest.mark.parametrize("how", ["missing", "denied"])
def test_with_the_flag_on_but_no_readable_verdicts_working_is_todays(gate_on, monkeypatch, how, caplog):
    w = _working_world()
    c = w.client(gate_columns=False) if how == "missing" else w.client(cls=_Denied)
    with caplog.at_level("WARNING"):
        got = store.recent_titles(c, PID, working=5)
    monkeypatch.setattr(config, "RETURN_GATE_ENFORCE", False)
    assert got == store.recent_titles(w.client(gate_columns=False), PID, working=5)
    assert ("return gate verdicts unreadable" in caplog.text) is (how == "denied"), "020 没跑是预期之内的, 不记 WARN"


def test_the_gate_is_read_only_when_some_post_qualifies(gate_on):
    w = _World()
    w.post("n1", "评论少的", comments=3, verdict=FAIL_EFFICACY)
    c = _unmap(w.client())
    assert store.recent_titles(c, PID, working=5)["working"] == []
    assert not _gate_reads(c)
    c2 = _unmap(w.client())
    assert "working" not in store.recent_titles(c2, PID) and not _gate_reads(c2), "不要 working 一栏就不读"


def test_gate_and_paid_boost_filters_stack(gate_on):
    """投流事件里的帖今天就不进 working(priors.is_clean); 闸在它之后, 两道各挡各的。"""
    w = _World()
    w.post("n-boost", "投了薯条的", comments=90, verdict=PASS)
    w.post("n-eff", "承诺功效的", comments=80, verdict=FAIL_EFFICACY)
    w.post("n-ok", "自己起来的", comments=70, verdict=PASS)
    w.boosted.append({"note_id": "n-boost", "project_id": TV})
    assert store.recent_titles(w.client(), PID, working=5)["working"] == ["自己起来的"]


# ══════════════════════════════════════════════════════════════════════
# A4 · 发牌的起量帖参照(priors._hit_pool / fetch_project)
# ══════════════════════════════════════════════════════════════════════

def _tv_note(nid, pid, *, age_days, comments, fmt="提问求助", lever="好奇驱动"):
    """先验读的那几列(同 tests/test_priors.note), id 是定死的: 同一个世界建两遍, 两遍比得出一样不一样。"""
    return {"note_id": nid, "project_id": pid, "publish_time": (PNOW - timedelta(days=age_days)).isoformat(),
            "content_format": fmt, "emotional_lever": lever, "human_truth_archetype": ["健康焦虑"],
            "emotional_intensity": "medium", "trend_dependencies": ["通用"], "tier": "趴", "comments_count": comments,
            "data_quality_flags": {}, "title": None, "raw_content": ""}


def _hit_rows(n_own: int = 30):
    """本期 30 篇(评论 0–29 条, 发出 3–17 天)、同品牌上一期 10 篇(评论 10–19 条, 发出 30–39 天)。"""
    rows = [_tv_note(f"own-{i:02d}", TV, age_days=3 + i % 15, comments=i) for i in range(n_own)]
    rows += [_tv_note(f"brand-{i:02d}", TV_BRAND, age_days=30 + i, comments=10 + i, fmt="场景植入") for i in range(10)]
    ans = {r["note_id"]: {"opening_type": "具体事件", "ending_asks_reader": "是"} for r in rows}
    return rows, ans


def _refs(agg) -> list[str]:
    return [h["ref"][len("note:"):] for h in agg["hits"]]


def test_a_blocked_post_is_not_a_hit_and_nothing_else_in_the_block_moves():
    rows, ans = _hit_rows()
    plain = priors.project_aggregate(rows, ans, tv_ids=[TV], brand_ids=[TV_BRAND], now=PNOW)
    assert len(plain["hits"]) == 4                                          # 本项目 3 篇 + 同品牌上一期 1 篇
    top, brand_top = _refs(plain)[0], _refs(plain)[-1]
    got = priors.project_aggregate(rows, ans, tv_ids=[TV], brand_ids=[TV_BRAND], now=PNOW,
                                   blocked=frozenset({top, brand_top}))
    assert _refs(got) == _refs(plain)[1:-1], "本项目的和同品牌其他期的被挡了都不当参照"
    rest = lambda a: {k: v for k, v in a.items() if k != "hits"}            # noqa: E731
    assert rest(got) == rest(plain), "配比、篇数、起量标签、杠杆 / 标题的计数、特征题比例一个数不变"
    assert got["working"] == plain["working"] and got["n_res"] == plain["n_res"]
    assert got["wmix"] == plain["wmix"] and got["raw"] == plain["raw"] and got["posts"] == plain["posts"]
    assert priors.project_aggregate(rows, ans, tv_ids=[TV], brand_ids=[TV_BRAND], now=PNOW,
                                    blocked=frozenset()) == plain, "不给 blocked 和原来一样"


def test_blocked_posts_stay_in_the_top_decile_floor():
    """门槛(这个项目这种形式里评论排到前一成要多少条)照旧把被挡的帖算在内: 30 篇的前一成是 3 篇, 门槛 27。
    挡掉第一名之后参照是剩下的两篇 —— 第四名(26 条)不会因为闸补进来。反过来看投流的帖: 它一直不进分母(今天的口径)。"""
    rows, ans = _hit_rows()
    plain = priors.project_aggregate(rows, ans, tv_ids=[TV], now=PNOW)
    assert [h["comments"] for h in plain["hits"]] == [29, 28, 27]
    top = _refs(plain)[0]
    got = priors.project_aggregate(rows, ans, tv_ids=[TV], now=PNOW, blocked=frozenset({top}))
    assert [h["comments"] for h in got["hits"]] == [28, 27], "少了一篇, 没有谁补进来"
    all_three = priors.project_aggregate(rows, ans, tv_ids=[TV], now=PNOW, blocked=frozenset(_refs(plain)))
    assert all_three["hits"] == [], "前一成全被挡了就没有参照, 第 4–6 名不顶上"
    boosted = priors.project_aggregate(rows, ans, tv_ids=[TV], now=PNOW, boosted=frozenset({top}))
    assert [h["comments"] for h in boosted["hits"]] == [28, 27, 26], "对照: 投流的帖不进分母, 门槛跟着降(原来的口径)"
    # 别的帖的条目一个字段不变(权重、年龄、结构答案)
    assert got["hits"] == plain["hits"][1:]


def _tv_world(links=(), *, gate_columns=True, cls=FakeClient):
    rows, _ = _hit_rows()
    link_rows = [dict({"note_id": nid, "tv_project_id": TV, "project_id": SIB, "match_kind": "ingested",
                       "version_id": f"v-{nid}"}, **v) for nid, v in links]
    missing = {}
    if not gate_columns:
        for l in link_rows:
            for col in store.TV_GATE_COLS:
                l.pop(col, None)
        missing = {"tv_note_links": set(store.TV_GATE_COLS)}
    c = cls(rows={"truth_vault.notes": rows, "truth_vault.v_paid_boost_events": [],
                  "truth_vault.projects": [{"project_id": TV, "category": "运动补给"},
                                           {"project_id": TV_BRAND, "category": "运动补给"}],
                  "tv_project_map": [{"tv_project_id": TV, "project_id": PID}],
                  "tv_note_links": link_rows,
                  "projects": [{"id": PID, "name": "示例项目", "brand": "示例品牌"}]},
            missing_columns=missing)
    return c, rows


def _top_refs():
    c, _rows = _tv_world()
    return _refs(priors.fetch_project(c, PID, now=PNOW))


def test_fetch_project_with_the_flag_off_is_todays_block_and_sends_todays_requests():
    top = _top_refs()
    assert len(top) == 4
    priors.reset_cache()
    c0, _ = _tv_world(gate_columns=False)                                   # 今天: 没有那四列
    base = priors.fetch_project(c0, PID, now=PNOW)
    priors.reset_cache()
    c1, _ = _tv_world([(top[0], FAIL_EFFICACY), (top[1], WITHDRAWN), (top[-1], UNSURE)])   # 判过了, 开关关着
    got = priors.fetch_project(c1, PID, now=PNOW)
    assert got == base
    assert _calls(c1) == _calls(c0) and not _gate_reads(c1)


def test_fetch_project_with_the_flag_on_drops_blocked_hits_only(gate_on):
    top = _top_refs()
    priors.reset_cache()
    c0, _ = _tv_world()
    base = priors.fetch_project(c0, PID, now=PNOW)
    priors.reset_cache()
    c1, _ = _tv_world([(top[0], FAIL_EFFICACY), (top[2], PASS), (top[-1], WITHDRAWN)])
    got = priors.fetch_project(c1, PID, now=PNOW)
    assert _refs(got) == [top[1], top[2]], "本项目第一名(承诺功效)和同品牌上一期那篇(删了)不当参照; 放行的、没判过的留着"
    assert {k: v for k, v in got.items() if k != "hits"} == {k: v for k, v in base.items() if k != "hits"}
    assert got["hits"] == [h for h in base["hits"] if h["ref"][5:] in (top[1], top[2])]
    reads = _gate_reads(c1)
    assert len(reads) == 1 and reads[0]["cols"] == GATE_READ
    assert sorted(reads[0]["filters"][0][2]) == sorted(top), "只问选出来的那几篇, 一次请求"


@pytest.mark.parametrize("how", ["missing", "denied", "no_rows"])
def test_fetch_project_with_the_flag_on_but_no_readable_verdicts_is_todays_block(gate_on, monkeypatch, how, caplog):
    top = _top_refs()
    priors.reset_cache()
    links = [(top[0], FAIL_EFFICACY)]
    if how == "missing":
        c, _ = _tv_world(links, gate_columns=False)
    elif how == "denied":
        c, _ = _tv_world(links, cls=_Denied)
    else:
        c, _ = _tv_world([])                                               # 这个 TV 项目的帖还没有对照行 / 没判过
    with caplog.at_level("WARNING"):
        got = priors.fetch_project(c, PID, now=PNOW)
    monkeypatch.setattr(config, "RETURN_GATE_ENFORCE", False)
    priors.reset_cache()
    c0, _ = _tv_world(gate_columns=False)
    assert got == priors.fetch_project(c0, PID, now=PNOW)
    assert ("return gate verdicts unreadable" in caplog.text) is (how == "denied")


def test_a_store_without_the_gate_reader_builds_the_block_as_before(gate_on):
    """回测 / 快照脚本换进来的 store 替身没有 desk_gate_blocked: 当没有闸。读的函数自己抛了也一样。"""
    class Old:
        pass
    assert priors._gate_blocked(Old(), None, ["n1"], PID) == frozenset()

    class Boom:
        @staticmethod
        def desk_gate_blocked(*a, **k):
            raise RuntimeError("boom")
    assert priors._gate_blocked(Boom(), None, ["n1"], PID) == frozenset()
    assert priors._gate_blocked(store, None, [], PID) == frozenset()


# ══════════════════════════════════════════════════════════════════════
# A8 · 同一个 TV 项目对着的几个写作台项目看得到彼此发出去的标题
# ══════════════════════════════════════════════════════════════════════

def _sibling_world():
    w = _World()
    w.post("n-own", "本项目的稿发出去的", days=2, comments=30)
    w.post("n-copy", "别处写的、补录在另一个项目上", days=4, comments=45, kind="ingested", pid=SIB, verdict=PASS)
    w.post("n-sib", "另一个方向的稿发出去的", days=6, comments=25, pid=SIB, published_title="人改过的标题", verdict=PASS)
    w.post("n-old", "窗口外的", days=40, comments=99, kind="ingested", pid=SIB)
    w.post("n-amb", "判不出的", days=3, kind="ambiguous", pid=SIB)
    w.post("n-foreign", "别的 TV 项目的帖", days=3, comments=88, kind="ingested", pid=OTHER, tv=TV_OTHER)
    w.links.append({"note_id": "n-none", "tv_project_id": TV, "project_id": None, "version_id": None,
                    "match_kind": "unmatched", "matched_at": _ago(hours=1)})
    return w


def test_with_the_flag_off_a_project_sees_only_its_own_links():
    w = _sibling_world()
    c = w.client()
    got = store.recent_titles(c, PID, working=5)
    assert got["published"] == ["本项目的稿发出去的"] and "working" in got and got["working"] == []
    assert not [q for q in c.calls if q["table"] == "tv_project_map"], "关着时不读对照表, 请求同今天"
    assert [q["cols"] for q in c.calls if q["table"] == "tv_note_links"] == ["note_id, version_id, match_kind"]


def test_with_the_flag_on_a_project_sees_its_tv_projects_published_titles(gate_on):
    w = _sibling_world()
    c = w.client()
    got = store.recent_titles(c, PID, working=5)
    assert got["published"] == ["本项目的稿发出去的", "别处写的、补录在另一个项目上", "人改过的标题"], got
    assert got["working"] == ["别处写的、补录在另一个项目上", "人改过的标题"], "working 也从整个 TV 项目里挑"
    # 另一个方向那边也一样看得到本项目发出去的
    back = store.recent_titles(w.client(), SIB, working=5)
    assert back["published"] == ["本项目的稿发出去的", "别处写的、补录在另一个项目上", "人改过的标题"]
    assert all(len(t) <= store.RECENT_TITLE_CHARS for t in got["published"])
    assert all("body" not in (q["cols"] or "") and "raw_content" not in (q["cols"] or "") for q in c.calls), "照旧不取正文"


def test_a_post_of_an_unmapped_tv_project_never_appears(gate_on):
    w = _sibling_world()
    for pid in (PID, SIB):
        c = w.client()
        got = store.recent_titles(c, pid, working=5)
        assert "别的 TV 项目的帖" not in got["published"] + got["working"] + got["drafted"]
        asked = [f for q in c.calls if q["table"] == "tv_note_links" for f in q["filters"] if f[1] == "tv_project_id"]
        assert asked and all(f[0] == "in" and list(f[2]) == [TV] for f in asked), "只按对照表里对着的 TV 项目问"
    # 反过来: 对着 BBB 的项目看不到 AAA 的任何一篇
    other = store.recent_titles(w.client(), OTHER, working=5)
    assert other["published"] == ["别的 TV 项目的帖"] and other["working"] == ["别的 TV 项目的帖"]


def test_a_row_of_another_tv_project_is_dropped_even_if_the_query_returned_it(gate_on):
    """回来的行再核一遍 TV 项目号: 就算过滤器漏了(假库这里故意不按 tv_project_id 筛), 没映射的 TV 项目的帖也进不来。"""
    class Leaky(FakeClient):
        def _match(self, row, kind, key, value):
            return True if key == "tv_project_id" else super()._match(row, kind, key, value)
    w = _sibling_world()
    got = store.recent_titles(w.client(cls=Leaky), PID, working=5)
    assert "别的 TV 项目的帖" not in got["published"] + got["working"]
    assert "别处写的、补录在另一个项目上" in got["published"]


def test_a_project_without_a_mapping_sees_what_it_sees_today(gate_on, monkeypatch):
    w = _sibling_world()
    c = _unmap(w.client())
    got = store.recent_titles(c, PID, working=5)
    monkeypatch.setattr(config, "RETURN_GATE_ENFORCE", False)
    assert got == store.recent_titles(w.client(), PID, working=5)
    assert got["published"] == ["本项目的稿发出去的"]


def test_a_title_already_shown_is_not_listed_twice(gate_on):
    w = _World()
    w.post("n-own", "同一个标题发了两遍", days=2, comments=30)
    w.post("n-dup", "同一个标题 发了两遍！", days=3, comments=40, kind="ingested", pid=SIB,   # 交叉发: 只差空白和标点
           verdict=PASS)
    w.post("n-third", "另一篇", days=4, comments=50, kind="ingested", pid=SIB, verdict=PASS)
    w.drafted("b1", ["另一篇", "还没发的新稿"])
    c = w.client()
    got = store.recent_titles(c, PID, working=5)
    assert got["published"] == ["同一个标题发了两遍", "另一篇"]
    assert got["working"] == ["另一篇", "同一个标题 发了两遍！"]
    assert got["drafted"] == ["还没发的新稿"], "写过的那栏不再列发出去的 / working 里有的"
    # 一篇帖只有一行对照; 两次查询都读到同一行时(本项目的行也在 TV 项目那次里)只算一次
    own = [q for q in c.calls if q["table"] == "tv_note_links" and any(f[1] == "project_id" for f in q["filters"])]
    assert len(own) == 1
    links, blocked, judged = store._sibling_links(c, PID, {"n-own"})
    assert sorted(r["note_id"] for r in links) == ["n-dup", "n-third"] and blocked == frozenset()
    assert judged == {"n-own", "n-dup", "n-third"}
    assert all(set(r) == {"note_id", "version_id", "match_kind"} for r in links)


def test_sibling_titles_follow_the_gate_in_the_working_column(gate_on):
    w = _World()
    w.post("n-own", "本项目的", days=5, comments=30, verdict=PASS)
    w.post("n-eff", "别处写的、承诺了功效", days=5, comments=90, kind="ingested", pid=SIB, verdict=FAIL_EFFICACY)
    w.post("n-ok", "别处写的、放行了", days=5, comments=60, kind="ingested", pid=SIB, verdict=PASS)
    w.post("n-own-del", "本项目的、后来删了", days=5, comments=70, verdict=WITHDRAWN)
    c = w.client()
    got = store.recent_titles(c, PID, working=5)
    assert got["working"] == ["别处写的、放行了", "本项目的"]
    assert {"别处写的、承诺了功效", "本项目的、后来删了"} <= set(got["published"]), "要避开的那一栏照旧有它们"
    # 结论和对照是同一次读回来的(本项目自己的行也在这个 TV 项目里): 不另问
    sib = _tv_link_reads(c)
    assert len(sib) == 1 and sib[0]["cols"] == "note_id, version_id, match_kind, tv_project_id, gate, gate_reasons"
    assert not _gate_reads(c)


def test_a_sibling_row_without_a_verdict_is_a_title_to_avoid_but_not_a_model(gate_on):
    """并进来的别的项目的行, 结论那一格是空的(闸那一步连着几晚没跑成、或者开关开在第一晚判之前)或者是不认识的值:
    算发出去的标题(要避开), 不进 working —— 这个项目以前从没见过它, 「没有结论 = 同以前」对它就是不摆出来。
    本项目自己的行照旧: 没有结论 = 没有闸时的口径, 照样进 working。"""
    w = _World()
    w.post("n-own", "本项目的、还没判过", days=5, comments=30)
    w.post("n-sib-null", "别处写的、还没判过", days=5, comments=90, kind="ingested", pid=SIB)
    w.post("n-sib-odd", "别处写的、结论不认识", days=5, comments=80, kind="ingested", pid=SIB,
           verdict={"gate": "later", "gate_reasons": {"reasons": []}})
    w.post("n-sib-ok", "别处写的、放行了", days=5, comments=60, kind="ingested", pid=SIB, verdict=PASS)
    c = w.client()
    got = store.recent_titles(c, PID, working=5)
    assert got["working"] == ["别处写的、放行了", "本项目的、还没判过"], got["working"]
    assert {"别处写的、还没判过", "别处写的、结论不认识"} <= set(got["published"]), "发出去的那一栏照旧有它们"
    assert len(_tv_link_reads(c)) == 1 and not _gate_reads(c), "不为这个多发请求"
    # 那一行在哪个项目名下, 哪个项目就照旧看得到它(那边它是自己的行)
    back = store.recent_titles(w.client(), SIB, working=5)
    assert back["working"] == ["别处写的、还没判过", "别处写的、结论不认识", "别处写的、放行了"], back["working"]
    assert "本项目的、还没判过" in back["published"]


def test_own_rows_the_tv_project_read_did_not_cover_are_asked_about_separately(gate_on, monkeypatch):
    """本项目的行排在按 TV 项目那次读的上限之外(TV 项目大、这个方向对上得早): 它们的结论专门再问一次。"""
    monkeypatch.setattr(store, "RECENT_LINKS_SCAN", 3)
    w = _World()
    w.post("s1", "别处写的 1", days=4, comments=30, kind="ingested", pid=SIB, verdict=PASS)       # matched_at 最新的三行
    w.post("s2", "别处写的 2", days=4, comments=31, kind="ingested", pid=SIB, verdict=FAIL_EFFICACY)
    w.post("s3", "别处写的 3", days=4, comments=32, kind="ingested", pid=SIB, verdict=PASS)
    w.post("o1", "本项目的、承诺了功效", days=5, comments=90, verdict=FAIL_EFFICACY)
    w.post("o2", "本项目的、放行了", days=5, comments=80, verdict=PASS)
    c = w.client()
    got = store.recent_titles(c, PID, working=5)
    assert got["working"] == ["本项目的、放行了", "别处写的 3", "别处写的 1"]
    reads = _gate_reads(c)
    assert len(reads) == 1 and sorted(reads[0]["filters"][0][2]) == ["o1", "o2"]


@pytest.mark.parametrize("how", ["missing", "denied"])
def test_without_readable_verdicts_no_sibling_title_is_added(gate_on, monkeypatch, how, caplog):
    """开关开着, 020 没跑 / 那两格读不出来: 别的项目的行一行不并 —— 没看过结论的别处的帖不会因为 A8 进 working,
    两栏同今天(「闸读不到 = 今天」对 A8 一样成立)。"""
    w = _sibling_world()
    c = w.client(gate_columns=False) if how == "missing" else w.client(cls=_Denied)
    with caplog.at_level("WARNING"):
        got = store.recent_titles(c, PID, working=5)
    assert got["published"] == ["本项目的稿发出去的"]
    assert ("sibling links unreadable" in caplog.text) is (how == "denied")
    monkeypatch.setattr(config, "RETURN_GATE_ENFORCE", False)
    assert got == store.recent_titles(w.client(gate_columns=False), PID, working=5)


def test_unreadable_sibling_links_fall_back_to_todays_columns(gate_on, monkeypatch, caplog):
    class NoMap(FakeClient):
        def _execute(self, q):
            if q.table_name == "tv_project_map":
                raise RuntimeError("connection reset")
            return super()._execute(q)
    w = _sibling_world()
    with caplog.at_level("WARNING"):
        got = store.recent_titles(w.client(cls=NoMap), PID, working=5)
    assert "sibling links unreadable" in caplog.text
    monkeypatch.setattr(config, "RETURN_GATE_ENFORCE", False)
    assert got == store.recent_titles(w.client(), PID, working=5)


def test_the_sibling_read_is_bounded(gate_on):
    w = _World()
    for i in range(30):
        w.post(f"s{i:02d}", f"另一个项目上的第{i:02d}篇", days=1 + i * 0.5, kind="ingested", pid=SIB)
    c = w.client()
    got = store.recent_titles(c, PID, limit=20, working=5)
    assert len(got["published"]) == 20 and got["published"][0] == "另一个项目上的第00篇"
    sib = _tv_link_reads(c)
    own = [q for q in c.calls if q["table"] == "tv_note_links" and any(f[1] == "project_id" for f in q["filters"])]
    assert len(sib) == 1 and len(own) == 1 and sib[0]["range"] == own[0]["range"] == (0, store.RECENT_LINKS_SCAN - 1)
    assert len([q for q in c.calls if q["table"] == "tv_project_map"]) == 1
    assert len([q for q in c.calls if q["table"] == "truth_vault.notes"]) == 1, "一页 id 装得下就一次"


# ══════════════════════════════════════════════════════════════════════
# 整块比: 简报、发牌
# ══════════════════════════════════════════════════════════════════════

def _brief_world():
    """一个写手能碰到的全套: 本项目对上的帖、补录在另一个项目上的帖、够格当起量帖的帖、被挡的帖。"""
    w = _World()
    rows, ans = _hit_rows(60)                                               # 前一成 6 篇 + 同品牌上一期 1 篇当起量帖
    for i, r in enumerate(rows):                                            # 发牌先验读的那些列并进 truth_vault.notes
        r = dict(r, title=None, raw_content=f"【标题】怎么选第{i}款？\n【正文】……")
        w.notes.append(r)
        w.answers += [{"subject_type": "note", "run_tag": "primary", "question_id": q, "subject_id": r["note_id"],
                       "answer": a, "extracted_at": _ago(days=1), "question_version": 1, "extractor": "jev"}
                      for q, a in ans[r["note_id"]].items()]
    top = sorted((r for r in rows if r["project_id"] == TV), key=lambda r: -r["comments_count"])
    verdicts = {top[0]["note_id"]: FAIL_EFFICACY, top[1]["note_id"]: WITHDRAWN, top[2]["note_id"]: PASS,
                top[3]["note_id"]: UNSURE}
    for i, r in enumerate(top[:12]):
        vid = f"v-hit{i}"
        w.versions.append({"id": vid, "title": f"起量帖的标题 {i:02d}"})
        link = {"note_id": r["note_id"], "tv_project_id": TV, "project_id": PID if i % 2 else SIB, "version_id": vid,
                "match_kind": "body_exact" if i % 2 else "ingested", "matched_at": _ago(hours=i)}
        link.update(verdicts.get(r["note_id"]) or {})
        w.links.append(link)
    w.post("n-foreign", "别的 TV 项目的帖", days=5, comments=77, kind="ingested", pid=OTHER, tv=TV_OTHER)
    w.drafted("b1", ["昨天写的一篇"])
    return w, [r["note_id"] for r in top]


def _stable(out):
    """简报里和时钟有关的两处(借卡耗时、先验的年龄)拿掉再比。"""
    out = copy.deepcopy(out)
    (out.get("lessons_status") or {}).pop("elapsed_ms", None)
    return json.loads(json.dumps(out, sort_keys=True, default=str, ensure_ascii=False))


def _brief(c):
    core.build_writing_brief(c, PID, user_id=ME)                            # 第一次只投递预热(就地跑完)
    return _stable(core.build_writing_brief(c, PID, user_id=ME))


def test_the_whole_brief_is_identical_with_the_flag_off(monkeypatch):
    w, _top = _brief_world()
    today = _fresh(lambda: _brief(w.client(gate_columns=False)))            # 今天: 020 没跑, 没有开关
    assert today["recent_titles"]["working"] and today.get("project_now"), "这份简报里确实有会被闸动到的两块"
    judged = _fresh(lambda: _brief(w.client()))                             # 判过了, 开关关着
    assert judged == today
    c0, c1 = w.client(gate_columns=False), w.client()
    _fresh(lambda: _brief(c0)), _fresh(lambda: _brief(c1))
    assert _calls(c1) == _calls(c0), "关着时发出去的请求也和今天一样"


@pytest.mark.parametrize("how", ["missing", "denied"])
def test_the_whole_brief_is_identical_when_the_verdicts_cannot_be_read(monkeypatch, how):
    """开关开着, 可 020 没跑 / 那几列读不出来: 整份简报同今天 —— 起量帖照旧, working 照旧, 别的项目的标题也不并进来。"""
    w, _top = _brief_world()
    today = _fresh(lambda: _brief(w.client(gate_columns=False)))
    monkeypatch.setattr(config, "RETURN_GATE_ENFORCE", True)
    c = w.client(gate_columns=False) if how == "missing" else w.client(cls=_Denied)
    assert _fresh(lambda: _brief(c)) == today


def test_with_the_flag_on_only_the_learn_lane_blocks_of_the_brief_change(monkeypatch):
    w, top = _brief_world()
    for l in w.links:
        l["project_id"] = PID
    w.links = [l for l in w.links if l["tv_project_id"] == TV]
    off = _fresh(lambda: _brief(w.client()))
    monkeypatch.setattr(config, "RETURN_GATE_ENFORCE", True)
    on = _fresh(lambda: _brief(w.client()))
    assert on["recent_titles"]["published"] == off["recent_titles"]["published"]
    assert on["recent_titles"]["drafted"] == off["recent_titles"]["drafted"]
    assert on["recent_titles"]["task"] == off["recent_titles"]["task"]
    assert off["recent_titles"]["working"][:4] == [f"起量帖的标题 {i:02d}" for i in range(4)]
    assert on["recent_titles"]["working"] == [f"起量帖的标题 {i:02d}" for i in (2, 4, 5, 6, 7)]
    assert on["project_now"] == off["project_now"], "「这个项目现在什么情况」是计数那一路, 不过闸"
    rest = lambda b: {k: v for k, v in b.items() if k != "recent_titles"}   # noqa: E731
    assert rest(on) == rest(off)


def test_the_brief_of_a_sibling_project_gains_titles_and_the_title_length_line_follows(monkeypatch):
    """A8 在简报里: 写手真在写的项目不是补录目标, 今天它的简报没有「最近发出去」; 开着之后看得到 TV 项目发出去的标题,
    标题字数那一句(看的是 published 那一栏)也跟着有了。别的 TV 项目的帖不在里面。"""
    w = _World()
    for i in range(12):
        w.post(f"s{i:02d}", f"别处写的短标题{i:02d}", days=1 + i, comments=30 + i, kind="ingested", pid=SIB, verdict=PASS)
    w.post("n-foreign", "别的 TV 项目的帖", days=5, comments=77, kind="ingested", pid=OTHER, tv=TV_OTHER)
    off = _fresh(lambda: _brief(w.client()))
    assert "recent_titles" not in off
    monkeypatch.setattr(config, "RETURN_GATE_ENFORCE", True)
    on = _fresh(lambda: _brief(w.client()))
    rt = on["recent_titles"]
    assert rt["published"] == [f"别处写的短标题{i:02d}" for i in range(12)]
    assert rt["working"] == [f"别处写的短标题{i:02d}" for i in (11, 10, 9, 8, 7)]
    assert rt["task"] == core.RECENT_TITLES_TASK + core.TITLE_LEN_TAIL
    assert "别的 TV 项目的帖" not in json.dumps(on, ensure_ascii=False)
    assert {k: v for k, v in on.items() if k != "recent_titles"} == {k: v for k, v in off.items()}


def _profile(c):
    return _stable(priors.get_profile(c, _project(PID), budget_s=10, now=PNOW))


def test_the_draw_profile_is_identical_with_the_flag_off_and_differs_only_in_hits_with_it_on(monkeypatch):
    w, top = _brief_world()
    today = _fresh(lambda: _profile(w.client(gate_columns=False)))
    assert len(today["hits"]) >= 3
    assert _fresh(lambda: _profile(w.client())) == today
    monkeypatch.setattr(config, "RETURN_GATE_ENFORCE", True)
    assert _fresh(lambda: _profile(w.client(gate_columns=False))) == today
    assert _fresh(lambda: _profile(w.client(cls=_Denied))) == today
    on = _fresh(lambda: _profile(w.client()))
    assert {k: v for k, v in on.items() if k != "hits"} == {k: v for k, v in today.items() if k != "hits"}, \
        "配比、提升、篇数、阶段、标签都不变"
    gone = {f"note:{top[i]}" for i in (0, 1, 3)}                            # 承诺功效 / 删了 / 功效题没答
    assert [h for h in today["hits"] if h["ref"] not in gone] == on["hits"], "留下的几篇一个字段不变, 也没有谁补进来"
    assert gone & {h["ref"] for h in today["hits"]} == gone and len(on["hits"]) == len(today["hits"]) - 3


def _reserve(args):
    return [{"reserved_key": c["angle_key"], "reserved_dims": c["dims"]} for c in args["_candidates"]][:args["_want"]]


def _draw(c):
    c.rpc_impl["deskcore_reserve_angles"] = _reserve
    out = core.draw_angles(c, PID, 10, user_id=ME, seed=7)
    refs = [((cand["dims"].get("_draw") or {}).get("ref")) for name, args in c.rpc_calls
            if name == "deskcore_reserve_angles" for cand in args["_candidates"]]
    return _stable(out), [r for r in refs if r]


def test_the_draw_reply_is_identical_with_the_flag_off(monkeypatch):
    w, top = _brief_world()
    today, refs_today = _fresh(lambda: _draw(w.client(gate_columns=False)))
    assert {f"note:{top[i]}" for i in (0, 1, 3)} & set(refs_today), "这一手牌里确实有以后会被挡的帖当参照"
    judged, refs_judged = _fresh(lambda: _draw(w.client()))
    assert judged == today and refs_judged == refs_today
    monkeypatch.setattr(config, "RETURN_GATE_ENFORCE", True)
    for c in (w.client(gate_columns=False), w.client(cls=_Denied)):
        out, refs = _fresh(lambda: _draw(c))
        assert out == today and refs == refs_today


def test_with_the_flag_on_no_card_is_drawn_from_a_blocked_post(monkeypatch):
    w, top = _brief_world()
    blocked = {f"note:{top[i]}" for i in (0, 1, 3)}
    seen_off, seen_on = set(), set()
    for seed in range(40):
        def draw(seed=seed):
            c = w.client()
            c.rpc_impl["deskcore_reserve_angles"] = _reserve
            core.draw_angles(c, PID, 10, user_id=ME, seed=seed)
            return {(cand["dims"].get("_draw") or {}).get("ref") for name, args in c.rpc_calls
                    if name == "deskcore_reserve_angles" for cand in args["_candidates"]}
        monkeypatch.setattr(config, "RETURN_GATE_ENFORCE", False)
        seen_off |= _fresh(draw)
        monkeypatch.setattr(config, "RETURN_GATE_ENFORCE", True)
        seen_on |= _fresh(draw)
    assert blocked <= seen_off, "关着时这三篇是会被发成参照的(不然这条用例什么都没证明)"
    assert not (blocked & seen_on)
    assert any(r and r.startswith("note:") for r in seen_on), "别的起量帖照样当参照"


# ══════════════════════════════════════════════════════════════════════
# A5 · 补录副本不当正负例
# ══════════════════════════════════════════════════════════════════════

def _example(iid, title, *, source="deskcore", label="positive", params=None, user=ME):
    return {"id": iid, "best_version_id": f"{iid}-v1", "created_at": _ago(days=1), "user_id": user,
            "example_label": label,
            "batches": {"project_id": PID, "params": params if params is not None else {"source": source}},
            "versions": [{"id": f"{iid}-v1", "title": title, "body": "正文" * 20, "version_num": 1, "embedding": None}]}


def test_an_ingested_copy_is_never_returned_as_a_labelled_example():
    rows = [_example("i-desk", "写作台写的正例"),
            _example("i-copy", "补录副本被标成了正例", source="ingest"),
            _example("i-copy-str", "老行: params 是字符串", params=json.dumps({"source": "ingest"})),
            _example("i-plain", "没有 params 的老批次", params={}),
            _example("i-neg-copy", "补录副本被标成了负例", source="ingest", label="negative"),
            _example("i-neg", "写作台写的负例", label="negative")]
    c = FakeClient(rows={"items": rows})
    pos = store.labeled_examples(c, PID, "positive", ME)
    assert [e["item_id"] for e in pos] == ["i-desk", "i-plain"]
    neg = store.labeled_examples(c, PID, "negative", ME)
    assert [e["item_id"] for e in neg] == ["i-neg"]
    sel = [q["cols"] for q in c.calls if q["table"] == "items"]
    assert all("batches!inner(project_id, params)" in s for s in sel), "认副本要 params; 照旧 inner join 到本项目"


def test_the_brief_never_carries_an_ingested_copy_as_an_example(monkeypatch):
    w = _World()
    c = w.client()
    c.rows["items"] = [_example("i-desk", "写作台写的正例"), _example("i-copy", "补录副本被标成了正例", source="ingest"),
                       _example("i-neg-copy", "补录副本被标成了负例", source="ingest", label="negative")]
    out = core.build_writing_brief(c, PID, user_id=ME)
    text = json.dumps(out, ensure_ascii=False)
    assert "写作台写的正例" in text
    assert "补录副本被标成了正例" not in text and "补录副本被标成了负例" not in text
    assert out["counts"]["positive_pool"] == 1 and out["counts"]["negative_examples"] == 0


def test_examples_without_any_copy_come_back_exactly_as_before():
    rows = [_example(f"i{i}", f"正例 {i}") for i in range(5)]
    got = store.labeled_examples(FakeClient(rows={"items": rows}), PID, "positive", ME)
    assert [e["item_id"] for e in got] == [f"i{i}" for i in range(5)]
    assert set(got[0]) == {"item_id", "version_id", "title", "body", "embedding"}
