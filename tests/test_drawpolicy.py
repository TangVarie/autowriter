"""发牌 v2 的策略(deskcore/drawpolicy.py, angle cards v2 §2.4–§2.9 / §5.1 / §6 Tests)。

数据全是合成的: Profile 从 priors.uniform_profile 改 π 表; 台账行手写; 外部参考 / 起量帖的 ref 是 ext:fx* / note:fx*,
没有任何标题或正文。西屋形状的模拟用 vendored 快照里的聚合数(drawsim.fixture_profile)。

反证(证明别的用例真能失败):
  · v1 在 84/16 配额上抽出配额外的形式(fit 用例对它不成立);
  · 关掉 L1(PARAMS 的测试副本), 14 天内就会重发同一个精确 key;
  · 把相对形状惩罚换成绝对的 0.4^T, 每种形状都在目标份额上时, 主原型的概率掉到 0.7·π 以下(相对的保持 π);
  · 同 seed 同候选表、同 _draw; n = 30 一次发牌 300 ms 以内。
"""
from __future__ import annotations

import json
import math
import random
import statistics
import time
from datetime import datetime, timedelta, timezone

import pytest

from deskcore import core, drawpolicy as dp, drawsim as ds, priors, vocab
from deskcore.fingerprint import angle_key

L, A, F, T = dp.L, dp.A, dp.F, dp.T
Q = dp.QUESTION
NOW = datetime(2026, 11, 9, 2, 0, tzinfo=timezone.utc)
LEVS, ARCS, FMTS, TTLS = (dp.VALUES[d] for d in (L, A, F, T))


# ── 合成 Profile / 台账 ─────────────────────────────────────────────────────

def make_profile(*, fmt=None, q=None, lev=None, arc=None, stage="run", ext=None, hits=None):
    p = priors.uniform_profile("fx-project")
    p.update(stage=stage, n_res={"none": None, "cold": 5, "warm": 30, "run": 200}[stage], ps="live")
    if fmt:
        p["pi"][F] = dict(fmt)
    if q is not None:
        t = {Q: q}
        t.update({k: (1 - q) * s for k, s in vocab.NONQ_SPLIT.items()})
        p["pi"][T] = t
    if lev:
        p["pi"][L] = dict(lev)
    if arc:
        p["pi"][A] = dict(arc)
    if ext is not None:
        p["ext"] = ext
    if hits is not None:
        p["hits"] = hits
    return p


FMT_8416 = {"提问求助": 0.84, "场景植入": 0.16}


def concentrated(**kw):
    """一个偏得很厉害的项目: 84/16 的形式配额, 问句 0.8, 杠杆 / 原型各有一个大头。"""
    lev = {v: (0.40 if v == "愤怒撬动" else 0.6 / 11) for v in LEVS}
    arc = {v: (0.35 if v == "健康焦虑" else 0.65 / 18) for v in ARCS}
    return make_profile(fmt=FMT_8416, q=0.8, lev=lev, arc=arc, **kw)


def ext_entry(i, *, title=None):
    return {"ref": f"ext:fx{i:04d}", "w": 1.0 + i / 10, "velocity": 3.0 + i, "comments": 60 + i,
            "posted_days_ago": 10 + i, "title_structure": title or TTLS[1 + i % 7],
            "desc": {"opening_type": "身份自述", "turning_point": True, "ending_asks_reader": True,
                     "asks_for_help": None, "own_experience": True}}


def ext_block(n, **kw):
    return {"category": "运动补给", "state": "ok", "ok": n >= 5, "eligible": n,
            "pool": [ext_entry(i, **kw) for i in range(n)]}


def hit_entry(i, *, own=True, fmt="提问求助", lev="愤怒撬动", arc="健康焦虑", ttl=Q):
    return {"ref": f"note:fx{i:04d}", "own": own, "w": 3.0 if own else 1.0, "tv_project": "FX_phase2",
            F: fmt, L: lev, A: arc, T: ttl, "age_days": 5, "comments": 40,
            "desc": {"opening_type": "具体事件", "ending_asks_reader": True, "asks_for_help": None,
                     "turning_point": None}}


def cell(lev="愤怒撬动", arc="健康焦虑", fmt="提问求助", ttl=Q):
    return (lev, arc, fmt, ttl)


def row(c, days_ago, *, consumed_days_ago=None, ref=None, rid=None):
    dims = dp.cell_dims(c)
    if ref:
        dims["_draw"] = {"v": 2, "pol": "v2", "src": "e", "ref": ref}
    drawn = NOW - timedelta(days=days_ago)
    out = {"id": rid or f"r{days_ago}-{hash(c) & 0xffff}", "angle_key": angle_key(dp.cell_dims(c)), "dims": dims,
           "drawn_by": "u", "drawn_at": drawn.isoformat(), "consumed_at": None, "consumed_version_id": None}
    if consumed_days_ago is not None:
        out["consumed_at"] = (NOW - timedelta(days=consumed_days_ago)).isoformat()
        out["consumed_version_id"] = "v-fx"
    return out


EMPTY = None


def empty_state():
    return dp.ledger_state([], NOW)


NO_TAIL = {**dp.PARAMS, "tail_min": 0, "tail_mult": 0}


def one_slot_plan(src, f, *, n=1, sp=0.7):
    return {"n": n, "stage": "run", "shares": {src: sp}, "counts": {src: n}, "formats": {f: n}, "pin": None,
            "slots": [{"i": i + 1, "src": src, "f": f, "sp": sp, "g": 0} for i in range(n)]}


def draw(profile, n, seed, *, rows=(), pin=None, params=dp.PARAMS, state=None):
    rng = random.Random(seed)
    st = state if state is not None else dp.ledger_state(list(rows), NOW, params=params)
    plan = dp.slot_plan(n, profile, rng, pin=pin, params=params)
    cands, meta = dp.build_candidates(profile, st, plan, rng, params=params)
    return plan, cands, meta


# ══════════════════════════════════════════════════════════════════════
# 张数取整与槽位(§2.4)
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("n,shares", [
    (5, {"w": 0.45, "x": 0.20, "e": 0.25, "h": 0.10}),
    (3, {"w": 0.50, "x": 0.20, "e": 0.20, "h": 0.10}),
    (10, {"提问求助": 0.84, "场景植入": 0.16}),
    (7, {v: w for v, w in zip(FMTS, (0.31, 0.22, 0.13, 0.11, 0.09, 0.07, 0.05, 0.02))}),
])
def test_systematic_round_expected_counts_are_n_times_share(n, shares):
    rng = random.Random(1)
    runs = 10_000
    tot = {k: 0 for k in shares}
    for _ in range(runs):
        c = dp.systematic_round(n, shares, rng)
        assert sum(c.values()) == n
        for k, s in shares.items():
            assert math.floor(n * s + 1e-9) <= c[k] <= math.ceil(n * s - 1e-9)   # 只在 floor / ceil 之间
            tot[k] += c[k]
    for k, s in shares.items():
        assert abs(tot[k] / runs - n * s) <= 0.03 * n, (k, tot[k] / runs, n * s)


def test_external_guarantee_pool_of_five_gives_at_least_one_and_pool_of_four_gives_none():
    five, four = make_profile(stage="warm", ext=ext_block(5)), make_profile(stage="warm", ext=ext_block(4))
    for seed in range(200):
        for n in (3, 4, 5):
            p5 = dp.slot_plan(n, five, random.Random(seed))
            assert p5["ext_ok"] and p5["counts"]["e"] >= 1
            gs = [s for s in p5["slots"] if s["g"]]
            assert len(gs) == (1 if p5["guaranteed_ext"] else 0) and all(s["src"] == "e" for s in gs)
            p4 = dp.slot_plan(n, four, random.Random(seed))
            assert not p4["ext_ok"] and p4["counts"]["e"] == 0
        assert dp.slot_plan(2, five, random.Random(seed))["counts"]["e"] == 0          # 一批不到 3 张不保底
        assert dp.slot_plan(30, five, random.Random(seed))["counts"]["e"] == 4         # 0.2 × 30 = 6 → 封顶 4
    # 保底那一张从 w 拿: n = 3、warm(e 0.2)时 c_e 的期望 0.6 → 有 40% 的批次靠保底
    guaranteed = sum(dp.slot_plan(3, five, random.Random(s))["guaranteed_ext"] for s in range(400))
    assert 100 < guaranteed < 220


def test_shares_follow_the_stage_and_unavailable_shares_go_to_w():
    def mean_counts(profile, n=12, seeds=300):              # n = 12: 外部 0.25 × 12 = 3, 碰不到 4 张的封顶
        tot = {k: 0 for k in dp.SOURCES}
        for s in range(seeds):
            for k, v in dp.slot_plan(n, profile, random.Random(s))["counts"].items():
                tot[k] += v
        return {k: v / seeds for k, v in tot.items()}
    anchors = [hit_entry(i) for i in range(3)] + [hit_entry(10 + i, own=False) for i in range(3)]
    warm = mean_counts(make_profile(stage="warm", ext=ext_block(8), hits=anchors))
    assert warm == pytest.approx({"w": 6.0, "x": 2.4, "e": 2.4, "h": 1.2}, abs=0.15)
    cold = mean_counts(make_profile(stage="cold", ext=ext_block(8), hits=anchors))
    assert cold == pytest.approx({"w": 5.4, "x": 2.4, "e": 3.0, "h": 1.2}, abs=0.15)
    bare = mean_counts(make_profile(stage="run"))                              # 没有外部、没有起量帖
    assert bare == pytest.approx({"w": 9.6, "x": 2.4, "e": 0.0, "h": 0.0}, abs=0.15)
    # 冷启动只用同品牌上一期的起量帖: 本项目的 3 篇不算, 不到 3 篇就没有 h
    cold_own_only = make_profile(stage="cold", hits=[hit_entry(i) for i in range(5)])
    assert dp.hit_anchors(cold_own_only) == [] and dp.slot_plan(10, cold_own_only, random.Random(0))["counts"]["h"] == 0
    assert len(dp.hit_anchors(make_profile(stage="run", hits=[hit_entry(i) for i in range(5)]))) == 5


def test_formats_follow_the_quota_and_are_independent_of_source():
    prof = make_profile(fmt=FMT_8416)
    pair = {}
    for s in range(400):
        plan = dp.slot_plan(10, prof, random.Random(s))
        assert sum(plan["formats"].values()) == 10
        assert set(plan["formats"]) <= set(FMT_8416)
        for sl in plan["slots"]:
            pair.setdefault(sl["src"], []).append(sl["f"] == "提问求助")
    for src, xs in pair.items():                    # 每种来源里的形式比例都 ≈ 0.84
        assert sum(xs) / len(xs) == pytest.approx(0.84, abs=0.04), src


# ══════════════════════════════════════════════════════════════════════
# 贴项目(§6 test_desk_variety 的替换用例在策略层的版本)
# ══════════════════════════════════════════════════════════════════════

def test_uniform_profile_eight_card_batch_has_eight_formats():
    prof = priors.uniform_profile("fx")
    for seed in range(20):
        _plan, _c, meta = draw(prof, 8, seed)
        assert sorted(p["dims"][F] for p in meta["primaries"]) == sorted(FMTS)


def test_8416_fixture_n10_averages_84_question_help_with_zero_off_support():
    prof = make_profile(fmt=FMT_8416)
    counts, off = [], 0
    for seed in range(200):
        _plan, _c, meta = draw(prof, 10, seed, params=NO_TAIL)
        fs = [p["dims"][F] for p in meta["primaries"]]
        assert len(fs) == 10
        counts.append(fs.count("提问求助"))
        off += sum(1 for f in fs if f not in FMT_8416)
    assert off == 0
    assert statistics.mean(counts) == pytest.approx(8.4, abs=0.3)


def test_v1_fails_the_fit_test_on_the_8416_fixture():
    """反证: 今天的发牌(全空间洗牌 + _spread_order)不管配额, 84/16 的项目每批都会发到配额外的形式。"""
    off_batches = 0
    for seed in range(50):
        top = core._v1_candidates(10, random.Random(seed))[:10]
        off_batches += any(c["dims"][F] not in FMT_8416 for c in top)
    assert off_batches >= 45


@pytest.mark.parametrize("n", [5, 10, 30])
def test_no_in_batch_shape_or_pair_repeat_and_lever_archetype_caps_hold(n):
    prof = concentrated(ext=ext_block(8), hits=[hit_entry(i, arc=ARCS[i]) for i in range(4)])
    for seed in range(25):
        _plan, _c, meta = draw(prof, n, seed, params=NO_TAIL)
        cells = [dp.cell_of(p["dims"]) for p in meta["primaries"]]
        assert len(cells) == n
        sig = [(c[2], c[3], c[1]) for c in cells]
        pairs = [(c[0], c[1]) for c in cells]
        assert len(set(sig)) == n and len(set(pairs)) == n
        lev_cap, arc_cap = max(2, math.ceil(0.4 * n)), max(2, math.ceil(0.35 * n))
        assert max(sum(1 for c in cells if c[0] == v) for v in LEVS) <= lev_cap
        assert max(sum(1 for c in cells if c[1] == v) for v in ARCS) <= arc_cap


def test_title_count_among_non_explore_cards_is_capped():
    prof = concentrated()
    for seed in range(40):
        plan, _c, meta = draw(prof, 10, seed, params=NO_TAIL)
        n_nonx = sum(1 for s in plan["slots"] if s["src"] != "x")
        pi_t = dp.targets(prof)[T]
        for t in TTLS:
            k = sum(1 for p in meta["primaries"] if p["src"] in ("w", "h") and p["dims"][T] == t)
            assert k <= math.ceil(pi_t[t] * n_nonx) + 1


def test_explore_titles_are_uniform_and_explore_formats_stay_in_support():
    """x: 杠杆 / 原型 / 标题均匀(不看 π), 形式照配额。2,000 张试新牌的标题过 χ²(df = 7, p > 0.01 ⇔ χ² < 18.475)。"""
    prof = concentrated()
    titles = {t: 0 for t in TTLS}
    picks = 0
    for seed in range(200):
        plan = {"n": 10, "stage": "run", "shares": {"x": 1.0}, "counts": {"x": 10}, "formats": {}, "pin": None,
                "slots": [{"i": i + 1, "src": "x", "f": ("提问求助" if i % 5 else "场景植入"), "sp": 0.2, "g": 0}
                          for i in range(10)]}
        _c, meta = dp.build_candidates(prof, empty_state(), plan, random.Random(seed), params=NO_TAIL)
        for p in meta["primaries"]:
            titles[p["dims"][T]] += 1
            picks += 1
    assert picks == 2000
    chi2 = sum((c - picks / 8) ** 2 / (picks / 8) for c in titles.values())
    assert chi2 < 18.475, titles
    for seed in range(100):                        # 正常的计划里, 试新牌的形式也在配额里
        _plan, _c, meta = draw(prof, 10, seed, params=NO_TAIL)
        assert all(p["dims"][F] in FMT_8416 for p in meta["primaries"] if p["src"] == "x")


# ══════════════════════════════════════════════════════════════════════
# 跨批次去重(§2.6)
# ══════════════════════════════════════════════════════════════════════

def test_ledger_state_reads_windows_decay_pairs_and_references():
    rows = [row(cell(), 0.0), row(cell(), 7.0), row(cell(lev="焦虑撬动"), 20.0, ref="ext:fx0001"),
            row(cell(lev="好奇驱动"), 40.0, consumed_days_ago=10), row(cell(lev="共鸣释放"), 40.0),
            {"angle_key": "deadbeefdeadbeefdead", "dims": {"x": 1}, "drawn_at": (NOW - timedelta(days=2)).isoformat()}]
    st = dp.ledger_state(rows, NOW)
    assert st["ok"] and st["dd"] == "py" and st["window_days"] == 30
    assert st["blocked"][cell()] == [0.0, False]
    assert st["blocked"][cell(lev="焦虑撬动")] == [20.0, False]
    assert st["blocked"][cell(lev="好奇驱动")][1] is True                  # 写过、10 天前写的: 硬挡
    assert cell(lev="共鸣释放") not in st["blocked"]                        # 40 天前抽的、没写
    assert st["unmapped_keys"] == {"deadbeefdeadbeefdead": [2.0, False]}   # 解不出格子的行按 key 挡
    sigma = ("提问求助", Q, "健康焦虑")
    assert st["shape_T"][sigma] == pytest.approx(1 + math.exp(-1) + math.exp(-20 / 7))
    assert st["W"] == pytest.approx(st["shape_T"][sigma])
    assert st["shape_n14"][sigma] == 2 and st["M14"] == 2
    assert st["pairs7"] == {("愤怒撬动", "健康焦虑")}
    assert st["ref_age"] == {"ext:fx0001": 20.0}
    bad = dp.ledger_state(None, NOW)
    assert not bad["ok"] and bad["dd"] == "rpc_only"
    assert dp.ledger_state([], NOW, avoid_days=45)["window_days"] == 45


def test_l1_key_drawn_20_days_ago_is_never_a_primary_while_31_days_ago_is_allowed():
    prof = concentrated()
    pin = {F: "提问求助", T: Q, A: "健康焦虑"}                              # 只剩杠杆可变: 12 个格子
    k = cell()                                                             # 愤怒撬动是大头(0.40)
    hit_20 = hit_31 = 0
    for seed in range(120):
        for days, rows in ((20, [row(k, 20)]), (31, [row(k, 31)])):
            _plan, cands, meta = draw(prof, 1, seed, rows=rows, pin=pin, params=NO_TAIL)
            prim = dp.cell_of(meta["primaries"][0]["dims"])
            if days == 20:
                hit_20 += prim == k
                assert angle_key(dp.cell_dims(k)) not in {c["angle_key"] for c in cands}
            else:
                hit_31 += prim == k
    assert hit_20 == 0 and hit_31 >= 30
    # 不 pin 也一样: 最可能的那张 20 天前抽过 → 永远不是主牌
    for seed in range(60):
        _plan, _c, meta = draw(prof, 3, seed, rows=[row(k, 20)], params=NO_TAIL)
        assert k not in [dp.cell_of(p["dims"]) for p in meta["primaries"]]
    # 更早抽、近 avoid_days 天写掉的也挡
    _plan, cands, _m = draw(prof, 1, 0, rows=[row(k, 45, consumed_days_ago=3)], pin=pin)
    assert angle_key(dp.cell_dims(k)) not in {c["angle_key"] for c in cands}


def test_l1_relaxes_to_seven_days_only_when_a_heavy_pin_leaves_fewer_than_three_cells():
    prof = concentrated()
    pin = {F: "提问求助", T: Q, A: "健康焦虑"}
    rows = [row(cell(lev=v), 10) for v in LEVS[:10]] + [row(cell(lev=LEVS[10]), 3)]   # 只剩 1 个没抽过的
    seen = set()
    for seed in range(40):
        _plan, _c, meta = draw(prof, 1, seed, rows=rows, pin=pin, params=NO_TAIL)
        p = meta["primaries"][0]
        assert p["l1"] == "relaxed"
        assert p["dims"][L] != LEVS[10]                                    # 3 天前抽的照样挡
        seen.add(p["dims"][L])
    assert LEVS[11] in seen and len(seen) > 2                               # 10 天前抽的放回来了
    _plan, cands, _m = draw(prof, 1, 0, rows=rows, pin=pin, params=NO_TAIL)
    assert cands[0]["dims"]["_draw"]["l1"] == "relaxed"


def test_relative_shape_penalty_only_bites_above_target_and_the_hard_cap_holds():
    prof = make_profile(fmt={"提问求助": 1.0}, q=0.5)
    pi = dp.targets(prof)
    sigma = ("提问求助", Q, "健康焦虑")
    st = empty_state()
    st["W"] = 40.0
    e = pi[F]["提问求助"] * pi[T][Q] * pi[A]["健康焦虑"] * 40.0             # 目标下的 T
    st["shape_T"][sigma] = e
    assert dp.l2_factor(sigma, st, pi) == pytest.approx(1.0)
    st["shape_T"][sigma] = e + 2.0
    assert dp.l2_factor(sigma, st, pi) == pytest.approx(0.4 ** 2)
    st["shape_T"][sigma] = e
    st["M14"], st["shape_n14"][sigma] = 30, 3                             # max(3, ⌈1.5·π_σ·30⌉) = 3
    assert dp.l2_factor(sigma, st, pi) == pytest.approx(0.01)
    st["pairs7"].add(("愤怒撬动", "健康焦虑"))
    assert dp.l2b_factor("愤怒撬动", "健康焦虑", st) == 0.6
    assert dp.d_factor(cell(), st, pi) == pytest.approx(0.01 * 0.6)


def test_absolute_shape_penalty_drops_the_dominant_archetype_below_seventy_percent_of_target():
    """反证: 台账里每种形状都正好在目标份额上。相对惩罚不动它们 —— w 槽里主原型的概率还是 π; 换成绝对的 0.4^T,
    主原型自己的形状最常用、被压得最狠, 概率掉到 0.7·π 以下(v2 的「健康焦虑 14% vs 已发 28%」就是这么来的)。"""
    prof = ds.fixture_profile("XIWU_phase2")
    pi = dp.targets(prof)
    top = max(ARCS, key=lambda a: pi[A][a])
    st = empty_state()
    st["W"] = 34.0                                                         # 5 张/天 × 21 天的衰减和
    for f in FMTS:
        for t in TTLS:
            for a in ARCS:
                st["shape_T"][(f, t, a)] = pi[F][f] * pi[T][t] * pi[A][a] * st["W"]
    slot = {"f": "提问求助", "src": "w"}

    def share(params):
        d = dp.slot_distribution(prof, st, slot, params=params)
        return sum(p for c, (p, _pu) in d.items() if c[1] == top)
    assert share(dp.PARAMS) == pytest.approx(pi[A][top], abs=1e-6)
    assert share({**dp.PARAMS, "l2_mode": "absolute"}) < 0.7 * pi[A][top]


def test_rebalance_pulls_toward_the_target_only_after_twenty_recent_cards():
    prof = make_profile(q=0.5)
    pi = dp.targets(prof)
    st = empty_state()
    st["M14"], st["m14"][T] = 19, {Q: 0}
    assert dp.r_value(T, Q, st, pi) == 1.0
    st["M14"] = 40
    assert dp.r_value(T, Q, st, pi) == pytest.approx(1.5 ** 0.5)          # 少发了 → 往上拉, 截在 1.5
    st["m14"][T] = {Q: 40}
    assert dp.r_value(T, Q, st, pi) == pytest.approx(((0.5 * 40 + 2) / 42) ** 0.5)
    assert dp.r_factor(cell(), st, pi, skip=(T,)) == pytest.approx(
        dp.r_value(L, "愤怒撬动", st, pi) * dp.r_value(A, "健康焦虑", st, pi))


def test_disabling_l1_yields_exact_repeats_within_fourteen_simulated_days():
    """反证: 把 L1 关掉(PARAMS 的测试副本), 只剩 RPC 的 L0, 14 天里就会重发同一组坐标; 开着时一次都没有。"""
    prof = ds.fixture_profile("XIWU_phase2")
    off = {**dp.PARAMS, "l1": False}
    rep_off = sum(ds.metrics(ds.simulate(prof, days=14, per_day=5, seed=s, params=off), prof)["repeats14"]
                  for s in range(2))
    rep_on = sum(ds.metrics(ds.simulate(prof, days=14, per_day=5, seed=s), prof)["repeats14"] for s in range(2))
    assert rep_off > 0 and rep_on == 0


def test_ledger_read_failure_skips_python_dedup_and_marks_rpc_only():
    prof = concentrated()
    _plan, cands, meta = draw(prof, 5, 0, state=dp.ledger_state(None, NOW))
    assert meta["dd"] == "rpc_only"
    assert {c["dims"]["_draw"]["dd"] for c in cands} == {"rpc_only"}


# ══════════════════════════════════════════════════════════════════════
# 参考帖(§2.5 e / h, L5)
# ══════════════════════════════════════════════════════════════════════

def test_pick_reference_cooldown_batch_exclusion_and_least_recently_used_reuse():
    pool = [ext_entry(i) for i in range(3)]
    rng = random.Random(0)
    e, p, reuse = dp.pick_reference(pool, rng, ref_age={"ext:fx0000": 5, "ext:fx0001": 30}, cooldown_days=21,
                                    in_batch=["ext:fx0002"])
    assert e["ref"] == "ext:fx0001" and p == 1.0 and reuse == 0
    e, p, reuse = dp.pick_reference(pool, rng, ref_age={"ext:fx0000": 5, "ext:fx0001": 2, "ext:fx0002": 9},
                                    cooldown_days=21)
    assert e["ref"] == "ext:fx0002" and reuse == 1                          # 全在冷却: 最久没用的那篇
    assert dp.pick_reference(pool, rng, cooldown_days=21, in_batch=[x["ref"] for x in pool]) == (None, 0.0, 0)
    hits = [hit_entry(0, fmt="场景植入"), hit_entry(1, fmt="提问求助")]
    for s in range(20):
        h, _p, _r = dp.pick_reference(hits, random.Random(s), cooldown_days=14,
                                      prefer=lambda x: x[F] == "提问求助")
        assert h["ref"] == "note:fx0001"


def test_external_cards_take_the_reference_title_structure_and_record_it():
    prof = concentrated(stage="warm", ext=ext_block(6, title="数字清单"))
    for seed in range(30):
        plan, cands, meta = draw(prof, 5, seed)
        es = [p for p in meta["primaries"] if p["src"] == "e"]
        assert len(es) >= 1
        refs = [p["ref"] for p in es]
        assert len(set(refs)) == len(refs)                                 # 一批之内一篇参考帖至多一张
        for p in es:
            assert p["dims"][T] == "数字清单" and p["ref"].startswith("ext:fx")
            assert p["reference"]["kind"] == "external" and p["reference"]["comments_per_day"] is not None
            assert p["dims"][F] in FMT_8416
            d = next(c for c in cands if c["angle_key"] == p["angle_key"])["dims"]["_draw"]
            assert d["src"] == "e" and d["ref"] == p["ref"]
            assert d.get("g", 0) == (1 if any(s["g"] and s["i"] == p["i"] for s in plan["slots"]) else 0)
    # 21 天内用过的参考帖不再挑(全都用过就重用最久没用的, reuse:1)
    rows = [row(cell(lev=LEVS[i]), 3 + i, ref=f"ext:fx{i:04d}") for i in range(6)]
    _plan, cands, meta = draw(prof, 5, 1, rows=rows)
    es = [c["dims"]["_draw"] for c in cands if c["dims"]["_draw"]["src"] == "e" and c["dims"]["_draw"]["i"]]
    assert es and all(d.get("reuse") == 1 for d in es)
    assert es[0]["ref"] == "ext:fx0005"                                    # 8 天前: 最久没用


def test_hit_cards_take_the_anchor_cell_or_back_off_archetype_first():
    anchors = [hit_entry(i, lev="愤怒撬动", arc="健康焦虑", ttl=Q) for i in range(3)]
    prof = concentrated(hits=anchors)
    plan = one_slot_plan("h", "提问求助", sp=0.1)
    _c, meta = dp.build_candidates(prof, empty_state(), plan, random.Random(0), params=NO_TAIL)
    p = meta["primaries"][0]
    assert dp.cell_of(p["dims"]) == cell() and p["src"] == "h" and p["ref"].startswith("note:fx")
    assert p["reference"]["kind"] == "hit" and p["reference"]["own"] is True
    _c, meta = dp.build_candidates(prof, dp.ledger_state([row(cell(), 5)], NOW), plan, random.Random(0),
                                   params=NO_TAIL)
    p = meta["primaries"][0]
    lev, arc, fmt, ttl = dp.cell_of(p["dims"])
    assert (lev, fmt, ttl) == ("愤怒撬动", "提问求助", Q) and arc != "健康焦虑" and p["src"] == "h"
    # 14 天内用过的起量帖不挑; 都用过 → 重用最久没用的
    rows = [row(cell(lev=LEVS[i]), 2 + i, ref=f"note:fx{i:04d}") for i in range(3)]
    _c, meta = dp.build_candidates(prof, dp.ledger_state(rows, NOW), plan, random.Random(0), params=NO_TAIL)
    assert meta["primaries"][0]["ref"] == "note:fx0002" and meta["primaries"][0]["reuse"] == 1


# ══════════════════════════════════════════════════════════════════════
# 候选表与 _draw(§2.7 / §5.1)
# ══════════════════════════════════════════════════════════════════════

DRAW_KEYS = {"v", "pol", "b", "i", "src", "sp", "p", "pu", "pv", "st", "ps", "dd"}


def test_candidate_list_caps_at_2000_keys():
    # 生产里 n 封顶 30; 这里只看 2,000 的上限。备牌 / 尾巴要过整批的 B(复审), 一批 100 张时只剩 128 个 (杠杆, 原型)
    # —— 只有两种形式的项目凑不出 1,900 张合规的备牌, 所以用八种形式都在配额里的项目
    prof = make_profile(stage="warm", ext=ext_block(6), q=0.5)
    _plan, cands, meta = draw(prof, 100, 3)
    keys = [c["angle_key"] for c in cands]
    assert len(keys) == len(set(keys)) == 2000
    idx = [c["dims"]["_draw"]["i"] for c in cands if c["dims"]["_draw"]["i"]]
    assert idx == sorted(idx) and len(idx) == len(meta["primaries"]) == 100 - meta["short"]


@pytest.mark.parametrize("n", [1, 5, 30])
def test_candidate_list_is_primaries_then_round_robin_alternates_then_tail(n):
    prof = concentrated(stage="warm", ext=ext_block(6), hits=[hit_entry(i, arc=ARCS[i]) for i in range(4)])
    plan, cands, meta = draw(prof, n, 3)
    keys = [c["angle_key"] for c in cands]
    assert len(keys) == len(set(keys)) == min(max(20 * n, 500), 2000)
    assert meta["short"] == 0
    assert [c["dims"]["_draw"]["i"] for c in cands[:n]] == list(range(1, n + 1))
    assert all(c["dims"]["_draw"]["i"] == 0 for c in cands[n:])
    assert [c["angle_key"] for c in cands[:n]] == [p["angle_key"] for p in meta["primaries"]]
    tail = [c for c in cands if c["dims"]["_draw"]["i"] == 0 and c["dims"]["_draw"]["src"] == "w"]
    assert tail and all(c["dims"][F] in FMT_8416 for c in cands)
    b = {c["dims"]["_draw"]["b"] for c in cands}
    assert len(b) == 1 and len(next(iter(b))) == 12
    for c in cands:
        d = c["dims"]
        assert set(d) == {L, A, F, T, "_draw"}
        assert DRAW_KEYS <= set(d["_draw"]) and d["_draw"]["v"] == 2 and d["_draw"]["pol"] == "v2"
        assert d["_draw"]["src"] in ("w", "x", "e", "h") and len(d["_draw"]["pv"]) == 4
        assert angle_key(d) == angle_key(dp.cell_dims(dp.cell_of(d))) == c["angle_key"]
        assert len(json.dumps(d["_draw"], ensure_ascii=False)) <= 300
    # 备牌: 每个槽位第 2 名在前、第 3 名在后(轮流排)
    alts = cands[n:n + 2 * n]
    assert all(c["dims"]["_draw"]["i"] == 0 for c in alts)


def test_angle_key_ignores_the_draw_block():
    _plan, cands, _m = draw(concentrated(), 5, 0)
    for c in cands[:50]:
        four = {k: v for k, v in c["dims"].items() if k != "_draw"}
        assert angle_key(c["dims"]) == angle_key(four) == c["angle_key"]


def test_same_seed_same_draw():
    prof = concentrated(stage="warm", ext=ext_block(6), hits=[hit_entry(i, arc=ARCS[i]) for i in range(4)])
    rows = [row(cell(lev=v), 1 + i) for i, v in enumerate(LEVS)]
    a = draw(prof, 10, 42, rows=rows)
    b = draw(prof, 10, 42, rows=rows)
    assert a[1] == b[1] and a[2] == b[2] and a[0] == b[0]
    assert draw(prof, 10, 43, rows=rows)[1] != a[1]


def test_p_sums_to_one_per_slot_and_pu_is_one_over_eligible_when_db_is_flat():
    prof = concentrated()
    st = empty_state()
    for slot in ({"f": "提问求助", "src": "w"}, {"f": "场景植入", "src": "x"},
                 {"f": "提问求助", "src": "e", "t_only": "数字清单"}):
        d = dp.slot_distribution(prof, st, slot)
        assert sum(p for p, _ in d.values()) == pytest.approx(1.0)
        assert sum(pu for _, pu in d.values()) == pytest.approx(1.0)
    d = dp.slot_distribution(prof, st, {"f": "提问求助", "src": "x"})
    assert len(d) == 12 * 19 * 8 and all(p == pytest.approx(1 / 1824) and pu == pytest.approx(1 / 1824)
                                         for p, pu in d.values())
    batch = [cell(), cell(lev="焦虑撬动", arc="经济焦虑", ttl="场景直述")]
    d = dp.slot_distribution(prof, st, {"f": "提问求助", "src": "w"}, batch_cells=batch, n=5, n_nonx=4)
    elig = [c for c, (_p, pu) in d.items() if pu > 0]
    assert all(pu == pytest.approx(1 / len(elig)) for _p, pu in d.values())
    assert sum(p for p, _ in d.values()) == pytest.approx(1.0)
    # 存进 _draw 的 p 就是这个槽位分布里的概率(3 位有效数字)
    _c, meta = dp.build_candidates(prof, st, one_slot_plan("w", "提问求助"), random.Random(5), params=NO_TAIL)
    c0 = _c[0]
    full = dp.slot_distribution(prof, st, {"f": "提问求助", "src": "w"})
    p, pu = full[dp.cell_of(c0["dims"])]
    assert c0["dims"]["_draw"]["p"] == float(f"{p:.3g}") and c0["dims"]["_draw"]["pu"] == float(f"{pu:.3g}")


def test_draw_for_thirty_cards_is_under_300_ms():
    prof = ds.fixture_profile("XIWU_phase2")
    rows = [row((LEVS[i % 12], ARCS[i % 19], FMTS[6 if i % 8 else 5], TTLS[i % 8]), (i % 30) + 0.5,
                rid=f"r{i}") for i in range(600)]
    for profile in (prof, priors.uniform_profile("fx")):
        times = []
        for seed in range(3):
            t0 = time.perf_counter()
            rng = random.Random(seed)
            st = dp.ledger_state(rows, NOW)
            plan = dp.slot_plan(30, profile, rng)
            cands, meta = dp.build_candidates(profile, st, plan, rng)
            times.append(time.perf_counter() - t0)
            assert len(meta["primaries"]) == 30 and len(cands) == 600
        assert statistics.median(times) < 0.300, times


# ══════════════════════════════════════════════════════════════════════
# 写手点名(§2.9)
# ══════════════════════════════════════════════════════════════════════

def test_pin_restricts_every_card_while_l1_still_applies():
    prof = concentrated()
    pin, err = dp.normalize_pin({"标题句式": "问句", "content_format": "场景植入"}, prof)
    assert err is None and pin == {T: Q, F: "场景植入"}
    blocked = cell(fmt="场景植入")
    for seed in range(20):
        plan, cands, meta = draw(prof, 5, seed, rows=[row(blocked, 12)], pin=pin)
        assert {s["src"] for s in plan["slots"]} == {"pin"}
        assert all(c["dims"][T] == Q and c["dims"][F] == "场景植入" for c in cands)
        assert all(c["dims"]["_draw"]["src"] == "pin" and c["dims"]["_draw"]["pin"] == [F, T] for c in cands)
        assert angle_key(dp.cell_dims(blocked)) not in {c["angle_key"] for c in cands}
        cells = [dp.cell_of(p["dims"]) for p in meta["primaries"]]
        assert len(cells) == 5 and len({(c[0], c[1]) for c in cells}) == 5     # B 照样管没 pin 的维度


def test_bad_pin_returns_the_allowed_values_and_the_format_shares():
    prof = make_profile(fmt=FMT_8416)
    pin, err = dp.normalize_pin({"标题句式": "反问句"}, prof)
    assert pin is None and err["error"] == "pin_invalid" and err["dim"] == T
    assert err["allowed"] == {"标题句式": list(TTLS)}
    assert err["format_shares"] == {"提问求助": 0.84, "场景植入": 0.16}
    pin, err = dp.normalize_pin({"颜色": "红"}, prof)
    assert pin is None and set(err["allowed"]) == {"内容形式", "标题句式", "情绪杠杆", "人性原型"}
    assert dp.normalize_pin(None) == (None, None)
    assert dp.normalize_pin("疑问句")[1]["error"] == "pin_invalid"


def test_repeat_candidates_carry_rep_and_the_original_key():
    dims = dp.cell_dims(cell())
    key = angle_key(dims)
    out = dp.repeat_candidates([(key, dims), ("0" * 20, dims)], batch_id="abcdef012345",
                               profile=concentrated(), extra={"fa": "patterns"})
    assert len(out) == 1                                                   # key 和四个维度对不上的不发
    d = out[0]["dims"]["_draw"]
    assert out[0]["angle_key"] == key and angle_key(out[0]["dims"]) == key
    assert d["src"] == "rep" and d["of"] == key and d["b"] == "abcdef012345" and d["fa"] == "patterns"
    # 重发的格子进了这一批的 B: 后面正常抽的牌不撞它的形状和 (杠杆, 原型)
    for seed in range(20):
        plan = dp.slot_plan(4, concentrated(), random.Random(seed))
        _c, meta = dp.build_candidates(concentrated(), empty_state(), plan, random.Random(seed),
                                       seed_cells=[cell()], params=NO_TAIL)
        for p in meta["primaries"]:
            c = dp.cell_of(p["dims"])
            assert (c[2], c[3], c[1]) != ("提问求助", Q, "健康焦虑") and (c[0], c[1]) != ("愤怒撬动", "健康焦虑")


# ══════════════════════════════════════════════════════════════════════
# 贴合度、模拟闸、CLI
# ══════════════════════════════════════════════════════════════════════

def test_fit_score_is_zero_for_uniform_and_higher_for_the_projects_main_card():
    prof = concentrated()
    main = dp.fit_score(dp.cell_dims(cell()), prof)
    odd = dp.fit_score(dp.cell_dims(cell(lev="虚荣撬动", arc="自由意志", fmt="反差破圈", ttl="对白引语")), prof)
    assert main > 0 > odd
    assert dp.fit_score([1 / 8, 1 / 8, 1 / 12, 1 / 19]) == pytest.approx(0.0, abs=0.01)
    pv = dp._pv(cell(), dp.targets(prof))
    assert dp.fit_score(pv) == pytest.approx(main)
    assert dp.fit_score([0.5]) is None and dp.fit_score({"x": 1}, prof) is None


@pytest.mark.slow
def test_sim_gates_pass_at_five_per_day_on_a_xiwu_like_profile():
    """§5.6 的上线闸: 西屋形状的聚合 Profile(vendored 快照), 28 天 × 5 张/天, 5 个 seed 平均。"""
    prof = ds.fixture_profile("XIWU_phase2")
    assert prof["stage"] == "run" and prof["pi"][F]["提问求助"] > 0.8
    rep = ds.run(prof, days=28, per_day=5, seeds=range(5))
    assert rep["gated"] and rep["pass"], ds.format_report(rep)
    m = rep["mean"]
    assert m["distinct30"] == 1.0 and m["off_support"] == 0 and m["in_batch_repeats"] == 0
    assert m["delivered"] == 140


def test_cli_angle_sim_runs_offline_from_the_snapshot(capsys):
    from deskcore import cli
    rc = cli.main(["angle-sim", "--project", "XIWU_phase2", "--days", "3", "--per-day", "10", "--seeds", "1"])
    out = capsys.readouterr().out
    assert rc == 0 and "只报数" in out and "fixture:XIWU_phase2" in out
    rc = cli.main(["angle-sim", "--project", "XIWU_phase2", "--days", "3", "--per-day", "10", "--seeds", "1",
                   "--json"])
    rep = json.loads(capsys.readouterr().out)
    assert rc == 0 and rep["gated"] is False and rep["mean"]["delivered"] == 30


# ══════════════════════════════════════════════════════════════════════
# 复审回合: RPC 跳过主牌、起量帖槽的 L1、e / h 的 p、说不出结构的参考帖
# ══════════════════════════════════════════════════════════════════════

def _reserve(cands, n, blocked=frozenset()):
    """仿 deskcore_reserve_angles: 按候选表的先后取前 n 个没被挡的(被挡 = 并发发牌刚抢走 / 写过而台账没读到)。"""
    out = []
    for c in cands:
        if len(out) >= n:
            break
        if c["angle_key"] not in blocked:
            out.append(c)
    return out


@pytest.mark.parametrize("rpc_only", [False, True])
def test_backups_keep_the_batch_free_of_repeats_when_the_rpc_skips_primaries(rpc_only):
    """RPC 跳过某张主牌时顶上来的是候选表里接下来的备牌 / 尾巴: 它们也不能和批里的形状、(杠杆, 原型)、参考帖撞
    (§2.6 B、L5)。跳几张都一样(备牌之间也互不相撞); 只跳一张时杠杆 / 原型封顶也守住。台账读不到(rpc_only)时 L1 关着,
    主牌常常是写过的 key、被 RPC 挡下 —— 这条路最常见。"""
    prof = concentrated(ext=ext_block(8), hits=[hit_entry(i, arc=ARCS[i]) for i in range(4)])
    n = 10
    lev_cap, arc_cap = max(2, math.ceil(0.4 * n)), max(2, math.ceil(0.35 * n))
    for seed in range(30):
        st = dp.ledger_state(None, NOW) if rpc_only else empty_state()
        rng = random.Random(seed)
        plan = dp.slot_plan(n, prof, rng)
        cands, meta = dp.build_candidates(prof, st, plan, rng)
        prim = [p["angle_key"] for p in meta["primaries"]]
        assert len(prim) == n
        pick = random.Random(1000 + seed)
        skips = [{k} for k in prim] + [set(pick.sample(prim, j)) for j in (2, 3, 5)]
        noise = {c["angle_key"] for c in cands[n:] if pick.random() < 0.3}    # 备牌里也有一批写过的
        for blocked in skips + [s | noise for s in skips[-3:]]:
            got = _reserve(cands, n, blocked)
            cells = [dp.cell_of(c["dims"]) for c in got]
            assert len(got) == n
            assert len({(c[2], c[3], c[1]) for c in cells}) == n, (seed, len(blocked), "形状重复")
            assert len({(c[0], c[1]) for c in cells}) == n, (seed, len(blocked), "(杠杆, 原型) 重复")
            refs = [c["dims"]["_draw"]["ref"] for c in got if c["dims"]["_draw"].get("ref")]
            assert len(refs) == len(set(refs)), (seed, "同一篇参考帖一批出了两张")
            if len(blocked) == 1:
                assert max(sum(1 for c in cells if c[0] == v) for v in LEVS) <= lev_cap
                assert max(sum(1 for c in cells if c[1] == v) for v in ARCS) <= arc_cap


def test_hit_slot_tries_other_anchors_then_w_before_relaxing_l1():
    """起量帖自己那一圈(本格 + 换原型 / 标题 / 杠杆, 共 37 格)全在 8–30 天前抽过: 不放宽 L1 去重发(§2.6 只在重度 pin
    时放宽), 先换别的起量帖, 都不行就按 w 发 —— w 还有上千个格子。"""
    neigh = ({cell()} | {cell(arc=x) for x in ARCS} | {cell(ttl=t) for t in TTLS} | {cell(lev=v) for v in LEVS})
    rows = [row(c, 8 + i % 20, rid=f"n{i}") for i, c in enumerate(sorted(neigh))]
    st = dp.ledger_state(rows, NOW)
    plan = one_slot_plan("h", "提问求助", sp=0.1)
    same = [hit_entry(i) for i in range(3)]                              # 三篇起量帖是同一个格子
    for seed in range(20):
        _c, meta = dp.build_candidates(concentrated(hits=same), st, plan, random.Random(seed), params=NO_TAIL)
        p = meta["primaries"][0]
        assert p["l1"] is None and p["src"] == "w" and dp.cell_of(p["dims"]) not in neigh
    other = hit_entry(9, lev="好奇驱动", arc="经济焦虑", ttl="场景直述")
    for seed in range(20):
        _c, meta = dp.build_candidates(concentrated(hits=same + [other]), st, plan, random.Random(seed),
                                       params=NO_TAIL)
        p = meta["primaries"][0]
        assert p["l1"] is None and p["src"] == "h" and p["ref"] == other["ref"]
        assert dp.cell_of(p["dims"]) == ("好奇驱动", "经济焦虑", "提问求助", "场景直述")


def test_external_slot_p_is_the_cells_probability_over_all_references():
    """e 槽存的 p 是这个格子在这个槽位里的概率 Σ_r p_r·P(格子 | r)(§2.7), 不是「挑中这篇参考帖 × 格子」的联合概率 ——
    40 篇参考帖里一大半是同一种标题句式, 联合概率会小几十倍。"""
    pool = [dict(ext_entry(i, title="数字清单" if i < 30 else "场景直述"), w=1.0 + (i % 7) / 3) for i in range(40)]
    prof = concentrated(ext={"category": "运动补给", "state": "ok", "ok": True, "eligible": 40, "pool": pool})
    st = empty_state()
    plan = one_slot_plan("e", "提问求助", sp=0.2)
    total = sum(e["w"] for e in pool)
    cond = {t: dp.slot_distribution(prof, st, {"f": "提问求助", "src": "e", "t_only": t}) for t in ("数字清单", "场景直述")}
    for seed in range(8):
        cands, meta = dp.build_candidates(prof, st, plan, random.Random(seed), params=NO_TAIL)
        c0 = cands[0]
        assert c0["dims"]["_draw"]["src"] == "e"
        c = dp.cell_of(c0["dims"])
        want = sum(e["w"] / total * cond[e[T]].get(c, (0.0, 0.0))[0] for e in pool)
        assert c0["dims"]["_draw"]["p"] == pytest.approx(float(f"{want:.3g}"), rel=1e-6)


def test_hit_slot_p_is_the_cells_probability_over_all_anchors():
    """h 槽: 三篇起量帖是同一个格子、一篇是另一个格子, 权重一样 → 这两个格子在槽位里的概率是 3/4 和 1/4。"""
    other = hit_entry(9, lev="好奇驱动", arc="经济焦虑", ttl="场景直述")
    prof = concentrated(hits=[hit_entry(i) for i in range(3)] + [other])
    plan = one_slot_plan("h", "提问求助", sp=0.1)
    got = {}
    for seed in range(30):
        cands, _meta = dp.build_candidates(prof, empty_state(), plan, random.Random(seed), params=NO_TAIL)
        got[dp.cell_of(cands[0]["dims"])] = cands[0]["dims"]["_draw"]["p"]
    assert got == {cell(): 0.75, ("好奇驱动", "经济焦虑", "提问求助", "场景直述"): 0.25}


def test_references_without_a_describable_structure_are_not_used():
    """卡上的参考行只说结构; 一样结构都说不出来的起量帖 / 外部帖(特征题没读到、开头是「其他」、几个是非题都是否)不当
    参考 —— 不然卡上没有参考行, batch_note 却说「参照了…的结构」。"""
    blank = {"opening_type": "其他", "ending_asks_reader": False, "asks_for_help": None, "turning_point": False}
    hits = [dict(hit_entry(i, arc=ARCS[i]), desc=dict(blank)) for i in range(5)]
    ext = ext_block(6)
    for e in ext["pool"]:
        e.update(title_structure=None, desc=dict(blank, own_experience=None))
    prof = concentrated(hits=hits, ext=ext)
    assert dp.hit_anchors(prof) == [] and dp.ext_refs(prof) == []
    for seed in range(10):
        plan, _c, meta = draw(prof, 10, seed, params=NO_TAIL)
        assert plan["counts"].get("h", 0) == 0 and plan["counts"].get("e", 0) == 0
        assert all(p["reference"] is None for p in meta["primaries"])
    # 说得出一样就够: 开头类型、或者任何一个「是」
    ok = dict(hit_entry(0), desc=dict(blank, ending_asks_reader=True))
    assert dp.hit_anchors(concentrated(hits=[ok])) == [ok]


@pytest.mark.slow
def test_sim_keeps_batches_clean_when_the_ledger_is_unreadable_or_teammates_draw_at_once():
    """模拟里台账读不到(Python 去重全关, 只靠 RPC 挡写过的)、或者队友同一时刻抢走一部分主牌: 一批之内照样没有
    重复的形状 / (杠杆, 原型); 没 pin 的牌一张都不放宽 L1。"""
    prof = ds.fixture_profile("XIWU_phase2")
    for kw in ({"rpc_only": True}, {"steal": 0.3}):
        rep = ds.run(prof, days=21, per_day=10, seeds=range(2), **kw)
        m = rep["mean"]
        assert m["in_batch_repeats"] == 0 and m["l1_relaxed"] == 0, (kw, ds.format_report(rep))
        assert m["delivered"] == 210
