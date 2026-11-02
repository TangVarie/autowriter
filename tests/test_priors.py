"""angle cards v2 §2.2 / §2.3 / §4 / §6 item 3: deskcore/priors.py(先验、缓存、外部参考池)和 priors_backtest.py(回测闸)。

数据全是合成的。西屋的金标准用例按 spec §2.2 算例里的生产聚合数(问句 3/137、非问句 8/66, 上一期 8/101 vs 1/142,
近 60 天 144 问句 / 70 非问句)造行, 没有一条真标题、真笔记 id。
"""

from __future__ import annotations

import json
import os
import random
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from deskcore import priors, priors_backtest as bt, vocab

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _fresh_cache():
    priors.reset_cache()
    yield
    priors._POOL[0].wait_idle(10)
    priors.reset_cache()
    priors._NOW[0] = time.time


# ── 合成数据 ──────────────────────────────────────────────────────────────

_SEQ = [0]


def _nid(t: datetime | None = None) -> str:
    _SEQ[0] += 1
    t = t or NOW
    return f"{int(t.timestamp()):08x}{_SEQ[0]:016x}"


def note(pid, *, age_days, fmt="提问求助", lever="共鸣释放", arcs=("健康焦虑",), tier="趴", comments=None,
         flags=None, title="", intensity="medium", trends=("通用",), nid=None):
    t = NOW - timedelta(days=age_days)
    return {"note_id": nid or _nid(t), "project_id": pid, "publish_time": t.isoformat(),
            "content_format": fmt, "emotional_lever": lever, "human_truth_archetype": list(arcs),
            "emotional_intensity": intensity, "trend_dependencies": list(trends), "tier": tier,
            "comments_count": comments, "data_quality_flags": flags or {},
            "title": None, "raw_content": f"【标题】{title}\n【正文】……" if title else ""}


def xiwu_fixture(*, with_brand=True):
    """西屋两期(spec §2.2 的算例口径): 本期有评论数, 上一期只有 tier。全是同一种内容形式(算例没有按形式拆)。"""
    rows, answers = [], {}

    def add(r, ans=None):
        rows.append(r)
        if ans:
            answers[r["note_id"]] = ans

    # 本期: 已出标签的问句 137 篇(起量 3)、非问句 66 篇(起量 8)、没答案 1 篇; 另有发出不到三天的 7 问句 / 4 非问句 / 9 没答案
    for i in range(137):
        age = 20 + i % 40 if i < 50 else 3 + i % 11
        add(note("XIWU_phase2", age_days=age, comments=25 if i < 3 else 2, title=f"怎么选第{i}款？"), "是")
    for i in range(66):
        age = 20 + i % 40 if i < 24 else 3 + i % 11
        add(note("XIWU_phase2", age_days=age, comments=30 if i < 8 else 1, title=f"今天收拾厨房{i}"), "否")
    add(note("XIWU_phase2", age_days=5, comments=0, title="有没有人用过这个？"))
    for i in range(7):
        add(note("XIWU_phase2", age_days=1, comments=0, title=f"第{i}个问题怎么办？"), "是")
    for i in range(4):
        add(note("XIWU_phase2", age_days=1, comments=0, title=f"周末大扫除{i}"), "否")
    for i in range(9):
        add(note("XIWU_phase2", age_days=1, comments=0,
                 title=(f"有没有人试过{i}？" if i < 6 else f"我家的第{i}台")))
    if with_brand:
        # 上一期: 只有 tier(没有评论数), 问句 101 篇里 8 爆、非问句 142 篇里 1 爆; 另 58 篇还在评估(只进配比)
        for i in range(101):
            add(note("XIWU_phase1", age_days=70 + i % 25, tier="爆" if i < 8 else "趴"), "是")
        for i in range(142):
            add(note("XIWU_phase1", age_days=70 + i % 25, tier="爆" if i < 1 else "趴"), "否")
        for i in range(58):
            add(note("XIWU_phase1", age_days=70 + i % 25, tier="评估中"), "是" if i < 29 else "否")
    meta = {"XIWU_phase2": {"category": "家居家电"}}
    if with_brand:
        meta["XIWU_phase1"] = {"category": "家居家电"}
    return rows, answers, meta


def xiwu_profile(*, with_brand=True):
    rows, answers, meta = xiwu_fixture(with_brand=with_brand)
    title_answers = {k: v for k, v in answers.items()}
    gt = priors.build_global(rows, title_answers, meta, now=NOW)
    own = [r for r in rows if r["project_id"] == "XIWU_phase2"]
    fq = {k: {"title_is_question": v} for k, v in answers.items()}
    proj = priors.project_aggregate(own, fq, tv_ids=["XIWU_phase2"],
                                    brand_ids=["XIWU_phase1"] if with_brand else [], now=NOW)
    proj.update(project_id="desk-xiwu", tv_ids=["XIWU_phase2"])
    return priors.compose(gt, proj, None, project_id="desk-xiwu", now=NOW), gt, proj


# ── §2.2 金标准: 西屋 P(疑问句) ─────────────────────────────────────────────

def test_xiwu_golden_question_share_is_060():
    prof, gt, _ = xiwu_profile()
    lbl = gt["projects"]["XIWU_phase2"]["label"]
    assert (lbl["n"], lbl["y"]) == (204, 11)
    assert (gt["projects"]["XIWU_phase1"]["label"]["n"], gt["projects"]["XIWU_phase1"]["label"]["y"]) == (243, 9)
    lift = prof["lift"]
    assert prof["tv"]["current"] == "XIWU_phase2" and prof["tv"]["brand_ids"] == ["XIWU_phase1"]
    assert prof["ref"]["source"] == "brand"
    # spec 的算例: B_T(Q)=1.37 / B_T(NQ)=0.68, L_T(Q)=0.91 / L_T(NQ)=1.16, q≈0.66, P=0.60
    assert lift["title_brand"]["Q"] == pytest.approx(1.37, abs=0.02)
    assert lift["title_brand"]["NQ"] == pytest.approx(0.68, abs=0.02)
    assert lift["title"]["Q"] == pytest.approx(0.91, abs=0.02)
    assert lift["title"]["NQ"] == pytest.approx(1.16, abs=0.02)
    assert prof["mix"]["question_share"] == pytest.approx(0.66, abs=0.01)
    assert lift["band"][0] == pytest.approx(prof["mix"]["question_share"] - 0.15)
    assert lift["p_question"] == pytest.approx(0.60, abs=0.01)
    assert prof["pi"]["title_structure"]["疑问句"] == pytest.approx(lift["p_question"], abs=0.02)


def test_without_the_brand_layer_thin_data_sits_at_the_band_floor():
    prof, _gt, _ = xiwu_profile(with_brand=False)
    q = prof["mix"]["question_share"]
    assert prof["lift"]["title_successes"] < 30
    assert prof["lift"]["p_star"] < q - 0.15
    assert prof["lift"]["p_question"] == pytest.approx(q - 0.15)


def test_band_widens_after_30_title_successes_and_bounds_hold():
    lifts = {"Q": 1.6, "NQ": 0.6}
    p_thin, _, band_thin = priors.p_question(0.43, lifts, 29)
    p_rich, star, band_rich = priors.p_question(0.43, lifts, 72)
    assert band_thin == pytest.approx((0.28, 0.53)) and p_thin == pytest.approx(0.53)
    assert band_rich == pytest.approx((0.18, 0.63)) and star > 0.63 and p_rich == pytest.approx(0.63)
    assert priors.p_question(0.78, lifts, 72)[0] == pytest.approx(0.80)
    assert priors.p_question(0.15, {"Q": 0.6, "NQ": 1.6}, 72)[0] == pytest.approx(0.10)


# ── 提升: 截断、品类层、分层 ────────────────────────────────────────────────

def _lbl(cells):
    lbl = priors._new_label()
    for fmt, lever, q, y, n in cells:
        priors.label_add(lbl, fmt=fmt, lever=lever, qclass=q, y=y, n=n)
    return lbl


def test_lever_and_title_lifts_are_clipped():
    gt = {"schema": 1, "projects": {
        "AAA_phase1": {"label": _lbl([("提问求助", "愤怒撬动", "Q", 1, 60), ("提问求助", "信息差利用", "NQ", 0, 400),
                                      ("提问求助", "共鸣释放", "NQ", 0, 200)])},
        "BBB_phase1": {"label": _lbl([("提问求助", "共鸣释放", "NQ", 0, 300), ("提问求助", "愤怒撬动", "NQ", 1, 2)])}}}
    ll = priors.lever_lift(gt, "AAA_phase1")["lift"]
    assert ll["愤怒撬动"] == pytest.approx(1.6)
    assert ll["信息差利用"] == pytest.approx(0.5)
    assert all(0.5 <= v <= 1.6 for v in ll.values())
    tl = priors.title_lift(gt, "AAA_phase1")["lift"]
    assert tl["Q"] == pytest.approx(1.6) and tl["NQ"] == pytest.approx(0.6)


def test_category_layer_is_off_by_default():
    assert priors.PARAMS["lift_category"] is False
    peers = _lbl([("情感叙事", "焦虑撬动", None, 1, 40), ("情感叙事", "焦虑撬动", None, 0, 10),
                  ("情感叙事", "共鸣释放", None, 0, 200)])
    other = _lbl([("情感叙事", "焦虑撬动", None, 0, 100), ("情感叙事", "共鸣释放", None, 1, 30),
                   ("情感叙事", "共鸣释放", None, 0, 70)])
    me = _lbl([("情感叙事", "焦虑撬动", None, 0, 5), ("情感叙事", "共鸣释放", None, 0, 5)])
    gt = {"schema": 1, "projects": {"ME_phase1": {"category": "个护", "label": me},
                                    "PEER_phase1": {"category": "个护", "label": peers},
                                    "FAR_phase1": {"category": "酒类", "label": other}}}
    off = priors.lever_lift(gt, "ME_phase1")
    assert off["category"] is None
    # 三层(全局 → 品牌 → 本项目)手算
    p = priors.PARAMS
    GO, GE = priors._sum_oe(gt, ["PEER_phase1", "FAR_phase1"], "lev", p, "binary")
    rp, O, E = priors.oe(me, "lev")
    for v in ("焦虑撬动", "共鸣释放"):
        G = (GO.get(v, 0) + 5) / (GE.get(v, 0) + 5)
        want = (O.get(v, 0) + 60 * rp * G) / (E.get(v, 0) + 60 * rp)
        assert off["lift"][v] == pytest.approx(min(1.6, max(0.5, want)))
    on = priors.lever_lift(gt, "ME_phase1", params={**p, "lift_category": True})
    assert on["category"] is not None and on["lift"]["焦虑撬动"] != pytest.approx(off["lift"]["焦虑撬动"])


def test_stratified_expectation_removes_a_format_effect_and_the_unstratified_one_does_not():
    # 流量贴(情感叙事)起量率 30%, 产品贴(场景植入)5%; 共鸣释放多在流量贴、信息差利用多在产品贴; 同一形式里两种杠杆的起量率一样
    cells = []
    for fmt, rate in (("情感叙事", 0.30), ("场景植入", 0.05)):
        for lever, n in (("共鸣释放", 400 if fmt == "情感叙事" else 40), ("信息差利用", 40 if fmt == "情感叙事" else 400)):
            y = round(n * rate)
            cells += [(fmt, lever, None, 1, y), (fmt, lever, None, 0, n - y)]
    lbl = _lbl(cells)
    _r, O, E = priors.oe(lbl, "lev")
    for v in ("共鸣释放", "信息差利用"):
        assert O[v] / E[v] == pytest.approx(1.0, abs=0.03)
    _r, O, E = priors.oe(lbl, "lev", stratified=False)
    assert O["共鸣释放"] / E["共鸣释放"] > 1.3 and O["信息差利用"] / E["信息差利用"] < 0.6


def test_rank_signal_uses_mid_ranks_and_half_per_post():
    lbl = priors._new_label()
    lbl["rk"] = True
    priors.label_add(lbl, fmt="提问求助", lever="愤怒撬动", qclass="Q", y=0, rank=0.9, n=1)
    priors.label_add(lbl, fmt="提问求助", lever="共鸣释放", qclass="NQ", y=0, rank=0.1, n=1)
    r, O, E = priors.oe(lbl, "lev", signal="rank")
    assert r == 0.5 and O == pytest.approx({"愤怒撬动": 0.9, "共鸣释放": 0.1}) and E == {"愤怒撬动": 0.5, "共鸣释放": 0.5}
    assert priors._mid_ranks([1, 2, 2, 3]) == pytest.approx([0.125, 0.5, 0.5, 0.875])


# ── 标签 ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("flags", [{"synthetic": True}, {"paid_promoted": True}, {"paid_promoted": "true"},
                                   {"comment_maintained_routes": ["起量后干预", "铺评工单"]},
                                   json.dumps({"synthetic": True}, ensure_ascii=False)])
def test_label_excludes_synthetic_paid_and_paved_notes(flags):
    r = note("SPX_phase1", age_days=10, comments=50, tier="大爆")
    r["data_quality_flags"] = flags
    assert priors.label(r, has_cc=True, now=NOW) is None


def test_label_rules():
    lab = lambda r, cc: priors.label(r, has_cc=cc, now=NOW)  # noqa: E731
    assert lab(note("SPX_phase1", age_days=10, comments=20), True) == 1
    assert lab(note("SPX_phase1", age_days=10, comments=19), True) == 0
    assert lab(note("SPX_phase1", age_days=10, comments=3, tier="大爆"), True) == 1
    assert lab(note("SPX_phase1", age_days=10, comments=None, tier="爆"), True) is None      # 有评论数的项目: NULL 不参与
    assert lab(note("SPX_phase1", age_days=2.5, comments=99), True) is None                  # 不满三天
    assert lab(note("TGV_phase1", age_days=400, comments=99), True) is None                  # 整个项目不参与
    # 没有评论数的项目只认 tier
    assert lab(note("NRT_phase2", age_days=30, tier="爆"), False) == 1
    assert lab(note("NRT_phase2", age_days=30, tier="趴"), False) == 0
    assert lab(note("NRT_phase2", age_days=30, tier="评估中", comments=99), False) is None
    assert lab(note("NRT_phase2", age_days=30, tier=None), False) is None
    assert lab(note("NRT_phase2", age_days=30, tier="趴", flags={"comment_maintained_routes": "铺评工单"}), False) == 0


def test_build_global_uses_tier_only_for_projects_without_comments():
    rows = [note("AAA_phase1", age_days=10, tier="爆"), note("AAA_phase1", age_days=10, tier="趴"),
            note("AAA_phase1", age_days=10, tier="评估中"),
            note("BBB_phase1", age_days=10, comments=21), note("BBB_phase1", age_days=10, comments=None, tier="爆"),
            note("BBB_phase1", age_days=1, comments=99)]
    gt = priors.build_global(rows, {}, {}, now=NOW)
    a, b = gt["projects"]["AAA_phase1"]["label"], gt["projects"]["BBB_phase1"]["label"]
    assert (a["n"], a["y"], a["rk"]) == (2, 1, False)
    assert (b["n"], b["y"], b["rk"]) == (1, 1, True)
    assert gt["projects"]["AAA_phase1"]["mix"]["n"] == 3 and gt["projects"]["BBB_phase1"]["mix"]["n"] == 3
    assert priors.snapshot_problems(gt) == []


# ── 配比、π、阶段 ──────────────────────────────────────────────────────────

def test_floor_cap_water_fills_and_keeps_ratios():
    p = priors.floor_cap({"a": 10, "b": 5, "c": 1, "d": 0}, 0.05, 0.5)
    assert sum(p.values()) == pytest.approx(1.0)
    assert p["a"] == pytest.approx(0.5) and p["d"] == pytest.approx(0.05)
    assert p["b"] / p["c"] == pytest.approx(5.0)
    assert priors.floor_cap({"a": 0, "b": 0}, 0.1, 0.4) == {"a": 0.5, "b": 0.5}
    q = priors.floor_cap({"a": 1, "b": 1}, 0.0, 0.3)                       # 封顶不可行: 放宽到 1/n
    assert q == pytest.approx({"a": 0.5, "b": 0.5})


def test_pi_respects_support_floors_and_caps():
    prof, _gt, _ = xiwu_profile()
    pi = prof["pi"]
    assert set(pi["content_format"]) <= {"提问求助"}           # 合成数据只有一种形式: 别的都不在支撑集里
    assert max(pi["emotional_lever"].values()) <= 0.40 + 1e-9
    assert min(pi["emotional_lever"].values()) >= 0.02 - 1e-9
    assert max(pi["human_truth_archetype"].values()) <= 0.35 + 1e-9
    assert min(pi["human_truth_archetype"].values()) >= 0.01 - 1e-9
    assert min(pi["title_structure"].values()) >= 0.02 - 1e-9
    for d in priors.DIMS:
        assert sum(pi[d].values()) == pytest.approx(1.0)


def test_format_support_drops_values_under_three_percent():
    gt = {"schema": 1, "projects": {}}
    proj = {"tv_ids": ["AAA_phase1"], "n_res": 80, "posts": 100,
            "wmix": {"content_format": {"提问求助": 900, "场景植入": 150, "认知重构": 5}}}
    prof = priors.compose(gt, proj, None, now=NOW)
    assert set(prof["pi"]["content_format"]) == {"提问求助", "场景植入"}
    assert prof["stage"] == "run"


def test_stage_thresholds_and_no_mapping():
    assert priors.stage_of(14, True) == "cold"
    assert priors.stage_of(15, True) == "warm"
    assert priors.stage_of(59, True) == "warm"
    assert priors.stage_of(60, True) == "run"
    assert priors.stage_of(100, False) == "none"
    prof = priors.compose(priors.load_snapshot(), {"tv_ids": [], "ext_category": None}, None, now=NOW)
    assert prof["stage"] == "none"
    assert prof["pi"]["content_format"] == pytest.approx({v: 1 / 8 for v in vocab.CONTENT_FORMATS})


def test_reference_chain_falls_back_brand_category_global():
    def mix(n, fmt):
        m = priors._new_mix()
        m["n"] = n
        m["content_format"] = {fmt: n}
        return m
    gt = {"schema": 1, "projects": {
        "AAA_phase2": {"category": "个护", "mix": mix(10, "提问求助"), "mix_recent": mix(10, "提问求助")},
        "AAA_phase1": {"category": "个护", "mix": mix(49, "情感叙事")},
        "BBB_phase1": {"category": "个护", "mix": mix(120, "直给推荐"), "mix_recent": mix(120, "直给推荐")},
        "CCC_phase1": {"category": "酒类", "mix": mix(500, "反差破圈"), "mix_recent": mix(500, "反差破圈")}}}
    r = priors.reference(gt, current="AAA_phase2", brand_ids=["AAA_phase1"], category="个护", exclude=["AAA_phase2"])
    assert r["source"] == "category" and r["shares"]["content_format"]["直给推荐"] == pytest.approx(1.0)
    gt["projects"]["AAA_phase1"]["mix"] = mix(50, "情感叙事")
    r = priors.reference(gt, current="AAA_phase2", brand_ids=["AAA_phase1"], category="个护", exclude=["AAA_phase2"])
    assert r["source"] == "brand" and r["shares"]["content_format"]["情感叙事"] == pytest.approx(1.0)
    # 品牌那层没有标题答案: 标题这一维接着往下退
    assert r["sources"]["title_structure"] in ("category", "global", "uniform")
    r = priors.reference(gt, current="ZZZ_phase1", category="教育", exclude=["ZZZ_phase1"])
    assert r["source"] == "global"


def test_own_mix_weights_the_last_two_weeks_double_and_reads_titles_from_raw_content():
    rows = [note("AAA_phase1", age_days=20, fmt="情感叙事", title="今天去爬山"),
            note("AAA_phase1", age_days=5, fmt="提问求助", title="这个怎么办？"),
            note("AAA_phase1", age_days=5, fmt="提问求助", title="我的第一次", flags={"synthetic": True}),
            note("ZZZ_phase1", age_days=5, fmt="横评对比", title="别家的帖")]
    agg = priors.project_aggregate(rows, {}, tv_ids=["AAA_phase1"], brand_ids=["AAA_phase0"], now=NOW)
    assert agg["posts"] == 2
    assert agg["wmix"]["content_format"] == {"情感叙事": 1, "提问求助": 2}
    assert agg["wmix"]["title_structure"] == {"场景直述": 1, "疑问句": 2}
    assert agg["raw"]["content_format"] == {"情感叙事": 1, "提问求助": 1}


def test_working_counts_and_published_feature_shares():
    rows = [note("AAA_phase1", age_days=10, comments=30, lever="愤怒撬动", title="怎么办？"),
            note("AAA_phase1", age_days=10, comments=1, lever="愤怒撬动", title="今天"),
            note("AAA_phase1", age_days=1, comments=99, lever="愤怒撬动", title="昨天")]
    ans = {rows[0]["note_id"]: {"ending_asks_reader": "是", "opening_type": "具体事件"},
           rows[1]["note_id"]: {"ending_asks_reader": "否", "opening_type": "提问"}}
    agg = priors.project_aggregate(rows, ans, tv_ids=["AAA_phase1"], now=NOW)
    w = agg["working"]
    assert (w["resolved"], w["successes"]) == (2, 1)
    assert w["by_title"] == {"Q": [1, 1], "NQ": [1, 0]} and w["by_lever"] == {"愤怒撬动": [2, 1]}
    assert agg["fq_shares"]["ending_asks_reader"] == pytest.approx(0.5)
    assert agg["fq_shares"]["opening_type"] == pytest.approx({"具体事件": 0.5, "提问": 0.5})


def test_hit_pool_top_decile_own_and_brand_windows_never_cross_brand():
    rows = []
    for i in range(30):
        rows.append(note("AAA_phase2", age_days=3 + i % 15, comments=i, lever="好奇驱动"))
    rows.append(note("AAA_phase2", age_days=1, comments=500))                 # 太新
    rows.append(note("AAA_phase2", age_days=10, comments=400, flags={"paid_promoted": True}))  # 不干净
    for i in range(10):
        rows.append(note("AAA_phase1", age_days=30 + i, comments=10 + i, fmt="场景植入"))
    rows.append(note("AAA_phase1", age_days=50, comments=999, fmt="场景植入"))  # 品牌期超过 45 天
    rows.append(note("BBB_phase1", age_days=10, comments=999))                 # 别的品牌
    ans = {r["note_id"]: {"opening_type": "具体事件", "ending_asks_reader": "是"} for r in rows}
    agg = priors.project_aggregate(rows, ans, tv_ids=["AAA_phase2"], brand_ids=["AAA_phase1"], now=NOW)
    hits = agg["hits"]
    own = [h for h in hits if h["own"]]
    brand = [h for h in hits if not h["own"]]
    assert [h["comments"] for h in own] == [29, 28, 27]                       # 31 篇里前一成(含不干净的不算进分母)
    assert all(h["w"] == 3.0 and h["emotional_lever"] == "好奇驱动" for h in own)
    assert [h["comments"] for h in brand] == [19] and brand[0]["w"] == 1.0
    assert all(h["ref"].startswith("note:") for h in hits)
    assert not any(h["tv_project"] == "BBB_phase1" for h in hits)
    assert own[0]["desc"] == {"opening_type": "具体事件", "ending_asks_reader": True, "asks_for_help": None,
                              "turning_point": None}


# ── 外部参考池 ────────────────────────────────────────────────────────────

def ext_row(*, posted_days, fetched_days=1, comments=100, title="今天练完腿", ad_like=None):
    posted = NOW - timedelta(days=posted_days)
    return {"note_id": _nid(posted), "category": "运动补给", "title": title, "comments": comments,
            "liked": 0, "collected": 0, "fetched_at": (NOW - timedelta(days=fetched_days)).isoformat(),
            "ad_like": ad_like, "has_product": False}


def test_posted_at_decodes_the_first_eight_hex_digits():
    t = datetime(2026, 10, 1, 8, 30, tzinfo=timezone.utc)
    assert priors.posted_at(f"{int(t.timestamp()):08x}0123456789abcdef", now=NOW) == t
    assert priors.posted_at("zzzzzzzz0000", now=NOW) is None
    assert priors.posted_at("short", now=NOW) is None
    assert priors.posted_at("00000001" + "0" * 16, now=NOW) is None                      # 1970 年
    future = NOW + timedelta(days=5)
    assert priors.posted_at(f"{int(future.timestamp()):08x}" + "0" * 16, now=NOW) is None


def test_external_pool_filters_and_orders_by_comment_velocity():
    rows = [ext_row(posted_days=11, comments=300),                 # 抓取时 10 天 → 30/天
            ext_row(posted_days=6, comments=100),                  # 5 天 → 20/天
            ext_row(posted_days=4, comments=60),                   # 3 天 → 20/天(并列, 按 ref 排)
            ext_row(posted_days=31, comments=30),                  # 1/天: 三篇都低于中位数(20/天)
            ext_row(posted_days=31, comments=30),
            ext_row(posted_days=31, comments=30),
            ext_row(posted_days=2, comments=500),                  # 抓取时才 1 天
            ext_row(posted_days=120, comments=9000),               # 抓取时 119 天
            ext_row(posted_days=21, fetched_days=50, comments=900),  # 50 天前抓的
            ext_row(posted_days=8, comments=400, ad_like=True),
            ext_row(posted_days=8, comments=400, title="sportsix 新口味"),
            ext_row(posted_days=8, comments=400)]                   # Jev 说它承诺功效
    efficacy = rows[-1]["note_id"]
    answers = {efficacy: {"efficacy_promise": "是"},
               rows[0]["note_id"]: {"title_is_question": "是", "opening_type": "身份自述", "turning_point": "是"}}
    out = priors.external_pool(rows, answers, category="运动补给", now=NOW)
    assert out["age_ok"] == 9 and out["eligible"] == 3 and out["median_velocity"] == pytest.approx(20.0)
    vels = [e["velocity"] for e in out["pool"]]
    assert vels == sorted(vels, reverse=True) and vels == pytest.approx([30.0, 20.0, 20.0])
    first = next(e for e in out["pool"] if e["ref"] == f"ext:{rows[0]['note_id']}")
    assert first["title_structure"] == "疑问句"                     # Jev 的问句答案压过正则
    assert first["desc"]["opening_type"] == "身份自述" and first["desc"]["turning_point"] is True
    assert first["w"] == pytest.approx(30 ** 0.5)
    assert first["posted_days_ago"] == 11
    assert all("title" not in e for e in out["pool"])
    assert out["recent_14d"] == 3 and out["updated_days_ago"] == 1


def test_external_category_mirrors_the_tv_brand_table():
    assert priors.EXT_CATEGORY_BY_PROJECT == {
        "戒烟": ("力克雷", "nicorette"), "鼻炎": ("雷诺考特",), "运动补给": ("sportsix",),
        "术后营养": ("大象集团", "nucare"), "求职留学": ("途鸽",)}
    assert priors.external_category({"brand": "SportSix", "name": "x"}) == "运动补给"
    assert priors.external_category({"brand": "", "name": "途鸽留学 11 月"}) == "求职留学"
    assert priors.external_category({"brand": "某家电", "name": "x"}) is None
    assert priors.external_category(None) is None
    assert "sportsix" in priors.registered_brands()


def test_ref_last_used_reads_the_draw_block():
    rows = [{"dims": {"_draw": {"ref": "ext:a"}}, "drawn_at": (NOW - timedelta(days=3)).isoformat()},
            {"dims": {"_draw": {"ref": "ext:a"}}, "drawn_at": (NOW - timedelta(days=1)).isoformat()},
            {"dims": {"emotional_lever": "x"}, "drawn_at": NOW.isoformat()}]
    assert priors.ref_last_used(rows) == {"ext:a": NOW - timedelta(days=1)}


# ── 快照 ──────────────────────────────────────────────────────────────────

def test_vendored_snapshot_is_aggregate_only_and_usable():
    raw = open(priors.SNAPSHOT_PATH, encoding="utf-8").read()
    assert len(raw.encode("utf-8")) <= priors.SNAPSHOT_MAX_BYTES
    gt = json.loads(raw)
    assert priors.snapshot_problems(gt) == []
    assert len(gt["projects"]) >= 10
    assert sum((e.get("label") or {}).get("n", 0) for e in gt["projects"].values()) > 1000
    for banned in ('"note_id"', '"title"', '"raw_content"', '"body"', "【标题】"):
        assert banned not in raw
    prof = priors.compose(gt, None, None, now=NOW, ps="snap")
    assert prof["stage"] == "none" and prof["lift"]["emotional_lever"]


def test_snapshot_problems_flags_text_and_unknown_values():
    gt = {"schema": 1, "built_at": "x", "projects": {"AAA_phase1": {"category": None, "brand": "AAA",
                                                                     "titles": ["a"], "mix": {"n": 1, "content_format": {"不存在": 1}}}}}
    probs = priors.snapshot_problems(gt)
    assert any("titles" in p for p in probs) and any("不存在" in p for p in probs)
    assert priors.snapshot_problems({"schema": 2}) != []


def test_build_script_from_export_matches_the_live_label_path(tmp_path):
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    try:
        import build_angle_priors as bap
    finally:
        sys.path.pop(0)
    cells = [{"p": "AAA_phase1", "c": "0,6,3,1,1,2,1.80;2,6,8,0,0,5,1.20;2,-1,-1,-1,0,1,0.50"}]
    mix = [{"p": "AAA_phase1", "c": "个护", "span": "2026-09-01/2026-10-20", "built": "2026-11-02 12:00:00+00",
            "m": "Af6:7.000;Al3:2.000;Al8:5.000;An:8.000;AqQ:2.000;AqN:5.000;Ba11:3.500;Bn:8.000"}]
    gt = bap.from_export(cells, mix)
    assert priors.snapshot_problems(gt) == []
    lbl = gt["projects"]["AAA_phase1"]["label"]
    assert (lbl["n"], lbl["y"], lbl["rk"]) == (8, 2, True)
    assert lbl["lev"]["提问求助"]["愤怒撬动"] == [2, 2, 1.8]
    assert lbl["ttl"]["提问求助"] == {"Q": [2, 2, 1.8], "NQ": [5, 0, 1.2]}
    assert lbl["fmt"][""] == [1, 0, 0.5]
    assert gt["projects"]["AAA_phase1"]["mix"]["title_q"] == {"Q": 2, "NQ": 5}
    assert gt["projects"]["AAA_phase1"]["mix_recent"]["human_truth_archetype"] == {"健康焦虑": 3.5}
    assert gt["built_at"].startswith("2026-11-02T12:00")
    out = tmp_path / "snap.json"
    assert bap.write(gt, str(out)) == 0 and json.loads(out.read_text(encoding="utf-8")) == gt
    gt["projects"]["AAA_phase1"]["title"] = "不该在这里"
    assert bap.write(gt, str(tmp_path / "bad.json")) == 1 and not (tmp_path / "bad.json").exists()


# ── 缓存: 永不抛、按时回、单飞 ─────────────────────────────────────────────

class Boom:
    def __getattr__(self, name):
        raise RuntimeError("store is down")


def test_get_profile_with_a_raising_store_returns_the_snapshot_profile():
    t0 = time.monotonic()
    prof = priors.get_profile(Boom(), {"id": "desk-1", "brand": "sportsix", "name": "x"}, budget_s=2.5)
    assert time.monotonic() - t0 < 2.0
    assert prof["ps"] == "snap" and prof["pp"] == "none" and prof["stage"] == "none"
    assert prof["ext"]["category"] == "运动补给" and prof["ext"]["ok"] is False
    assert priors.cache_state("desk-1") == "unavailable"
    assert priors.cached_profile("desk-1") is None


def test_get_profile_never_raises_even_when_composing_fails(monkeypatch):
    monkeypatch.setattr(priors, "compose", lambda *a, **k: (_ for _ in ()).throw(ValueError("bug")))
    prof = priors.get_profile(Boom(), "desk-1", budget_s=0.2)
    assert prof["ps"] == "uniform" and "bug" in prof["error"]


def test_snapshot_corrupt_falls_back_to_uniform(monkeypatch, tmp_path):
    bad = tmp_path / "snap.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(priors, "SNAPSHOT_PATH", str(bad))
    prof = priors.get_profile(Boom(), "desk-1", budget_s=0.2)
    assert prof["ps"] == "uniform" and prof["stage"] == "none"


@pytest.fixture
def fake_store(monkeypatch):
    """store 的读数函数换成假的: delay 秒之后回 xiwu 的合成数据(gate 可以提前放行)。"""
    from deskcore import store
    rows, answers, meta = xiwu_fixture()
    gate = threading.Event()
    state = {"delay": 0.0, "calls": {}}

    def slow(name, value):
        def f(*a, **k):
            state["calls"][name] = state["calls"].get(name, 0) + 1
            if state["delay"]:
                gate.wait(state["delay"])
            return value() if callable(value) else value
        return f

    monkeypatch.setattr(store, "tv_notes_outcomes", slow("outcomes", rows))
    monkeypatch.setattr(store, "tv_title_answers", slow("answers", answers))
    monkeypatch.setattr(store, "tv_projects_meta", slow("meta", {k: {"category": v["category"], "brand_key": k.split("_")[0]}
                                                               for k, v in meta.items()}))
    monkeypatch.setattr(store, "project_row", slow("row", {"id": "desk-x", "name": "x", "brand": "某家电"}))
    monkeypatch.setattr(store, "tv_projects_of", slow("map", [{"tv_project_id": "XIWU_phase2"}]))
    monkeypatch.setattr(store, "tv_notes_profile", slow("profile", lambda: [r for r in rows]))
    monkeypatch.setattr(store, "note_answers", slow("fq", {k: {"title_is_question": v} for k, v in answers.items()}))
    monkeypatch.setattr(store, "external_slice", slow("ext", []))
    yield state
    gate.set()
    assert priors._POOL[0].wait_idle(10)          # 卡住的构建跑完再撤假件, 不然它醒来会去调下一个用例的


def test_a_slow_store_returns_within_budget_with_the_snapshot(fake_store):
    fake_store["delay"] = 5.0
    t0 = time.monotonic()
    prof = priors.get_profile(object(), "desk-x", budget_s=2.5)
    assert time.monotonic() - t0 < 2.6
    assert prof["ps"] == "snap" and prof["pp"] == "none"
    assert priors.cache_state("desk-x") == "warming"


def test_a_slow_store_serves_stale_data_up_to_72_hours(fake_store):
    real = time.time
    prof = priors.get_profile(object(), "desk-x", budget_s=10)
    assert prof["ps"] == "live" and prof["pp"] == "live" and prof["stage"] == "run"
    fake_store["delay"] = 5.0
    priors._NOW[0] = lambda: real() + 13 * 3600                 # 全局表(12 h)和项目块(2 h)都过期了
    t0 = time.monotonic()
    prof = priors.get_profile(object(), "desk-x", budget_s=0.3)
    assert time.monotonic() - t0 < 0.5
    assert prof["ps"] == "stale" and prof["pp"] == "stale" and prof["stage"] == "run"
    assert prof["prior_age_min"] >= 13 * 60
    priors._NOW[0] = lambda: real() + 73 * 3600                 # 超过 72 h: 不再用旧的
    prof = priors.get_profile(object(), "desk-x", budget_s=0.3)
    assert prof["ps"] == "snap" and prof["pp"] == "none"


def test_single_flight_builds_each_piece_once(fake_store):
    fake_store["delay"] = 0.3
    out = []
    ths = [threading.Thread(target=lambda: out.append(priors.get_profile(object(), "desk-x", budget_s=5)))
           for _ in range(4)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    assert len(out) == 4 and all(p["ps"] == "live" for p in out)
    assert fake_store["calls"]["outcomes"] == 1 and fake_store["calls"]["map"] == 1


def test_warm_is_fire_and_forget_and_fills_the_cache(fake_store):
    fake_store["delay"] = 0.2
    t0 = time.monotonic()
    priors.warm(object(), "desk-x")
    assert time.monotonic() - t0 < 0.1
    assert priors.cached_profile("desk-x") is None
    assert priors.cache_state("desk-x") == "warming"
    deadline = time.monotonic() + 5
    while priors.cache_state("desk-x") != "ok" and time.monotonic() < deadline:
        time.sleep(0.02)
    prof = priors.cached_profile("desk-x")
    assert prof is not None and prof["pp"] == "live" and prof["tv"]["current"] == "XIWU_phase2"
    assert prof["lift"]["p_question"] == pytest.approx(0.60, abs=0.01)


# ── 回测 ──────────────────────────────────────────────────────────────────

def test_auc_counts_ties_as_half():
    assert bt.auc([(2, 1, 1), (1, 0, 1)]) == 1.0
    assert bt.auc([(1, 1, 1), (1, 0, 3)]) == 0.5
    assert bt.auc([(1, 1, 2), (2, 0, 1), (0, 0, 1)]) == pytest.approx(0.5)
    assert bt.auc([(1, 0, 1)]) is None


def test_recommend_beta_follows_the_gate_two_fallback():
    assert bt.recommend_beta(0.61, 0.50) == 1.0
    assert bt.recommend_beta(0.59, 0.585) == 0.5
    assert bt.recommend_beta(0.59, 0.57) == 0.0
    assert bt.recommend_beta(None, None) == 0.0


def _synthetic_cells(seed=7):
    """六个项目; 好奇驱动的起量率是共鸣释放的四倍, 问句标题再翻一倍。"""
    rng = random.Random(seed)
    cells = {}
    for p in ("AAA_phase1", "BBB_phase1", "CCC_phase1", "DDD_phase1", "EEE_phase1", "AAA_phase2"):
        cs = []
        for bucket, n in (("train", 60), ("gap", 20), ("test", 120)):
            for _ in range(n):
                lever = rng.choice(["好奇驱动", "共鸣释放"])
                q = rng.choice(["Q", "NQ"])
                rate = (0.20 if lever == "好奇驱动" else 0.05) * (1.6 if q == "Q" else 0.8)
                y = int(rng.random() < rate)
                cs.append({"bucket": bucket, "content_format": "提问求助", "emotional_lever": lever, "q": q,
                           "y": y, "n": 1, "r": rng.random()})
        cells[p] = cs
    return cells


def test_backtest_finds_signal_and_reports_both_gates():
    cells = _synthetic_cells()
    spans = {"AAA_phase1": ("2026-01-01", "2026-02-01"), "AAA_phase2": ("2026-03-01", "2026-04-01")}
    res = bt.backtest(cells, spans)
    assert res["n_targets"] == 6
    assert res["models"]["shipped_beta_1"]["mean_auc"] > 0.65
    assert res["models"]["shipped_beta_0"]["mean_auc"] < res["models"]["shipped_beta_1"]["mean_auc"]
    assert res["gates"]["gate2_shipped"]["pass"] is True and res["recommend"]["beta"] == 1.0
    assert set(res["gates"]["gate1_reference"]) == {"value", "target", "pass"}
    assert bt._earlier_brand("AAA_phase2", spans, cells) == ["AAA_phase1"]
    assert bt._earlier_brand("AAA_phase1", spans, cells) == []          # 后一期不给前一期当品牌层
    assert "gate 1" in bt.format_report(res)


def test_backtest_from_notes_buckets_by_beijing_day():
    rows = [note("AAA_phase1", age_days=40, comments=30), note("AAA_phase1", age_days=31, comments=1),
            note("AAA_phase1", age_days=25, comments=5), note("AAA_phase1", age_days=10, comments=40)]
    cells, spans = bt.cells_from_notes(rows, {}, now=NOW)
    assert [c["bucket"] for c in cells["AAA_phase1"]] == ["train", "train", "test", "test"]
    assert [c["y"] for c in cells["AAA_phase1"]] == [1, 0, 0, 1]
    assert spans["AAA_phase1"][0] < spans["AAA_phase1"][1]


def test_cli_offline_backtest_runs_without_a_database(tmp_path, capsys):
    from deskcore import cli
    path = tmp_path / "cells.json"
    rows = []
    rng = random.Random(3)
    for p in ("AAA_phase1", "BBB_phase1", "CCC_phase1"):
        toks = []
        for b in (0, 2):
            for li in (8, 10):
                for y in (0, 1):
                    toks.append(f"{b},6,{li},{rng.choice([0, 1])},{y},{rng.randint(1, 9) if y else 30},0")
        rows.append({"p": p, "c": ";".join(toks)})
    path.write_text(json.dumps(rows), encoding="utf-8")
    rc = cli.main(["angle-prior", "--backtest", "--cells", str(path)])
    out = capsys.readouterr().out
    assert "gate 1" in out and "gate 2" in out and rc in (0, 1)
    rc = cli.main(["angle-prior", "--backtest", "--cells", str(path), "--json"])
    assert json.loads(capsys.readouterr().out)["n_targets"] == 3


# ── 复审回合: 项目块读不到但对照表读得到; 同品牌只认上一期 ─────────────────────────────

def _tv_down(monkeypatch, *, profile_delay=None):
    """TV 那几张表读不到(或者 tv_notes_profile 卡住 profile_delay 秒再失败), 写作台这边的对照表照常读得到。"""
    from deskcore import store
    gate = threading.Event()

    def none(*a, **k):
        return None

    def notes_profile(*a, **k):
        if profile_delay:
            gate.wait(profile_delay)
        return None

    for fn in ("tv_notes_outcomes", "tv_title_answers", "tv_projects_meta", "note_answers", "external_slice"):
        monkeypatch.setattr(store, fn, none)
    monkeypatch.setattr(store, "tv_notes_profile", notes_profile)
    monkeypatch.setattr(store, "project_row", lambda *a, **k: {"id": "desk-x", "name": "x", "brand": "某家电"})
    monkeypatch.setattr(store, "tv_projects_of", lambda *a, **k: [{"tv_project_id": "XIWU_phase2"}])
    return gate


def _snapshot_support(tv_id: str) -> set:
    m = priors.load_snapshot()["projects"][tv_id]["mix_recent"]["content_format"]
    tot = sum(m.values())
    return {v for v, c in m.items() if c / tot >= priors.PARAMS["fmt_support_min"]}


def test_tv_down_with_a_readable_mapping_keeps_formats_inside_the_snapshot_quota(monkeypatch):
    """重启后缓存是空的、TV 读不到: 对照表已经读到了, 就不能把项目当成「没对上」按 8 种形式平均发 —— 配比从快照里
    本项目那一条拼, 形式只在它的配额里; 没有近 60 天的原始数, 卡上不给括号, 简报的 project_now 照旧只认项目块。"""
    _tv_down(monkeypatch)
    for _ in range(2):                                   # 第二次: 项目块 60 秒内不重试, 对照表照样认得
        prof = priors.get_profile(object(), "desk-x", budget_s=2.5)
        assert prof["ps"] == "snap" and prof["pp"] == "table" and prof["stage"] in ("warm", "run")
        assert prof["tv"]["ids"] == ["XIWU_phase2"] and prof["tv"]["current"] == "XIWU_phase2"
        sup = {v for v, s in prof["pi"]["content_format"].items() if s > 0}
        assert sup and sup <= _snapshot_support("XIWU_phase2")
        assert prof["evidence"]["content_format"] == {} and prof["hits"] == []
        assert prof["lift"]["brand"] is not None, "提升照样按本项目对上的 TV 项目算(全局表 / 快照里有它的计数)"
    assert priors.cache_state("desk-x") == "unavailable" and priors.cached_profile("desk-x") is None


def test_a_slow_tv_with_a_readable_mapping_keeps_formats_inside_the_quota(monkeypatch):
    """冷缓存 + TV 慢(项目块超过发牌的等待预算): 对照表一读到就先记下, 这一次发牌照样按本项目的配额发。"""
    gate = _tv_down(monkeypatch, profile_delay=5.0)
    try:
        t0 = time.monotonic()
        prof = priors.get_profile(object(), "desk-x", budget_s=0.5)
        assert time.monotonic() - t0 < 0.8
        assert prof["pp"] == "table" and prof["stage"] != "none"
        assert {v for v, s in prof["pi"]["content_format"].items() if s > 0} <= _snapshot_support("XIWU_phase2")
        assert priors.cache_state("desk-x") == "warming"
    finally:
        gate.set()
        assert priors._POOL[0].wait_idle(10)


def test_brand_layer_for_reference_and_anchors_is_the_earlier_phase_only():
    """「同品牌上一期」= 最后一篇发在本期第一篇之前的那一期(§2.3 §2.5, 同 priors_backtest._earlier_brand)。只对上
    前一期的写作台项目不拿后一期当配比参照、不拿后一期的帖当起量帖; 只对上后一期的照常用前一期。"""
    rows = []
    for i in range(80):
        rows.append(note("AAA_phase1", age_days=30 + i % 15, comments=5 + i, fmt="场景植入"))     # 前一期: 30–44 天前
    for i in range(80):
        rows.append(note("AAA_phase2", age_days=2 + i % 24, comments=5 + i))                     # 后一期: 2–25 天前
    ans = {r["note_id"]: {"opening_type": "具体事件"} for r in rows}
    meta = {"AAA_phase1": {"category": "家居家电"}, "AAA_phase2": {"category": "家居家电"}}
    gt = priors.build_global(rows, {}, meta, now=NOW)

    def prof(own, other):
        proj = priors.project_aggregate(rows, ans, tv_ids=[own], brand_ids=[other], now=NOW)
        proj.update(project_id="desk-a", tv_ids=[own])
        return priors.compose(gt, proj, None, project_id="desk-a", now=NOW)

    old = prof("AAA_phase1", "AAA_phase2")
    assert old["ref"]["source"] != "brand" and old["ref"]["by_dim"]["content_format"] != "brand"
    assert [h for h in old["hits"] if not h["own"]] == []
    assert old["tv"]["earlier_ids"] == []
    new = prof("AAA_phase2", "AAA_phase1")
    assert new["ref"]["source"] == "brand" and new["ref"]["ids"] == ["AAA_phase1"]
    assert new["tv"]["earlier_ids"] == ["AAA_phase1"]
    assert any(not h["own"] and h["tv_project"] == "AAA_phase1" for h in new["hits"])
