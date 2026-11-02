"""deskcore/drawsim.py — 发牌 v2 的模拟(angle cards v2 §2.12 / §5.6 的 angle-sim): 纯函数, 不碰库。

一个 Profile + 一本假台账, 按天发牌 N 天: 每天现算台账状态(ledger_state)、排槽位、抽候选, 再用一个照抄
deskcore_reserve_angles 判据的假 RPC 按顺序预留(写过且在 avoid_days 内的跳过、一天内抽过没写的跳过)。预留到的牌按
consume_rate 当天写掉(写掉的 L0 挡 avoid_days 天)。发牌和「写不写」用两个独立的 rng, 同一个 seed 结果不变。

两种 RPC 会跳过主牌的情形(复审): rpc_only=True —— 台账读不到, 策略看到的是空状态(Python 去重全关), 假 RPC 照样按真
台账挡写过的; steal=s —— 队友同一时刻在同一个项目上发牌, 每张主牌有 s 的概率在我们预留之前被抢走(抢走的记进台账)。

上线闸(§5.6, 5 张/天、5 个 seed 平均):
  · 30 天内精确 key 全不重复              · 配额外的内容形式 0 张
  · 每种形式的占比离 π 不超过 5 个点        · 非试新牌的疑问句占比 ≥ 目标 − 0.08
  · 非试新牌里 π 最大的原型占比 ≥ 0.7 × π    · 任意 14 天里最多的杠杆占比 ≤ 0.40
  · 一批之内没有重复的形状 / (杠杆, 原型) / 参考帖     · 没 pin 的牌不放宽 L1(复审: 只在重度 pin 时放宽)
10 张/天只报数, 不设闸。

fixture_profile 用 vendored 快照(只有聚合数)拼一个项目的 Profile: 本项目近 60 天配比 + 快照里的标签计数; 标题句式只有
问句 / 非问句两类, 非问句按 vocab.NONQ_SPLIT 摊开; 起量帖按本项目配比合成几篇(ref 是 note:fixture-*, 不是真笔记)。
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from . import drawpolicy as dp
from . import priors, vocab

L, A, F, T = dp.L, dp.A, dp.F, dp.T
QUESTION = dp.QUESTION
SIM_START = datetime(2026, 11, 9, 2, 0, tzinfo=timezone.utc)      # 固定起点: 结果不随跑的那天变

GATES = {
    "distinct30": ("30 天内精确 key 不重复", lambda m: m["distinct30"] >= 1.0 - 1e-9),
    "off_support": ("配额外的内容形式 0 张", lambda m: m["off_support"] == 0),
    "fmt_dev": ("形式占比离 π ≤ 5 个点", lambda m: m["fmt_dev_max"] <= 0.05 + 1e-9),
    "question": ("非试新疑问句 ≥ 目标 − 0.08", lambda m: m["q_nonx"] >= m["q_target"] - 0.08 - 1e-9),
    "top_arc": ("非试新最大原型 ≥ 0.7 × π", lambda m: m["arc_nonx"] >= 0.7 * m["arc_target"] - 1e-9),
    "lever14": ("任意 14 天最多的杠杆 ≤ 0.40", lambda m: m["lever14_max"] <= 0.40 + 1e-9),
    "in_batch": ("一批之内形状 / (杠杆, 原型) / 参考帖不重复", lambda m: m["in_batch_repeats"] == 0),
    "l1_relaxed": ("没 pin 的牌不放宽 L1", lambda m: m["l1_relaxed"] == 0),
}


# ══════════════════════════════════════════════════════════════════════
# 快照 → Profile
# ══════════════════════════════════════════════════════════════════════

def synthetic_hits(mix: dict, *, n: int, seed: int = 7) -> list[dict]:
    """按配比合成 n 篇「起量帖」(只有四个维度和结构描述, 没有文本; ref 是 note:fixture-*)。"""
    rng = random.Random(seed)

    def draw(dim):
        m = {v: float(c) for v, c in (mix.get(dim) or {}).items() if v in dp.VALUES[dim] and float(c) > 0}
        if not m:
            return rng.choice(dp.VALUES[dim])
        return dp.es_pick(list(m.items()), rng)[0]

    out = []
    for i in range(max(int(n), 0)):
        out.append({"ref": f"note:fixture-{i + 1}", "own": True, "w": 3.0, "tv_project": "fixture",
                    F: draw(F), L: draw(L), A: draw(A), T: draw(T), "age_days": 3 + i % 10, "comments": 25 + i,
                    "desc": {"opening_type": "具体事件", "ending_asks_reader": True, "asks_for_help": None,
                             "turning_point": None}})
    return out


def fixture_profile(tv_id: str, *, gt: dict | None = None, now: datetime | None = None, n_hits: int = 6,
                    params: dict = priors.PARAMS) -> dict | None:
    """vendored 快照里一个 TV 项目 → Profile(只用聚合数)。快照里没有这个项目回 None。"""
    gt = gt if gt is not None else priors.load_snapshot()
    entry = ((gt or {}).get("projects") or {}).get(tv_id)
    if not entry:
        return None
    mix = entry.get("mix_recent") or entry.get("mix") or {}
    wmix = {dim: {k: float(v) for k, v in (mix.get(dim) or {}).items()} for dim in priors.MIX_DIMS}
    tq = mix.get("title_q") or {}
    ts: dict = {}
    if float(tq.get("Q") or 0) > 0:
        ts[QUESTION] = float(tq["Q"])
    if float(tq.get("NQ") or 0) > 0:
        for k, s in vocab.NONQ_SPLIT.items():
            ts[k] = float(tq["NQ"]) * s
    wmix["title_structure"] = ts
    hit_mix = dict(wmix)
    proj = {"project_id": f"fixture:{tv_id}", "tv_ids": [tv_id], "brand_ids": [],
            "last_by_id": {tv_id: entry.get("last") or ""}, "posts": int(float(mix.get("n") or 0)),
            "window_days": params["mix_window_days"], "wmix": wmix, "raw": wmix,
            "n_res": int(((entry.get("label") or {}).get("n")) or 0), "working": None, "fq_shares": {},
            "hits": synthetic_hits(hit_mix, n=n_hits)}
    return priors.compose(gt, proj, None, project_id=f"fixture:{tv_id}", now=now or SIM_START, ps="snap",
                          pp="live", params=params)


# ══════════════════════════════════════════════════════════════════════
# 模拟
# ══════════════════════════════════════════════════════════════════════

def _iso(t: datetime) -> str:
    return t.isoformat()


def _l0_blocked(rows: list[dict], now: datetime, avoid_days: int) -> bool:
    """deskcore_reserve_angles 的两条判据(照抄 migrations/001): 写过且 consumed_at 在 avoid_days 内; 没写、一天内抽过。"""
    for r in rows:
        if r.get("consumed_version_id") is not None:
            if r["_c"] is not None and r["_c"] >= now - timedelta(days=avoid_days):
                return True
        elif r["_t"] >= now - timedelta(days=1):
            return True
    return False


def simulate(profile: dict, *, days: int = 28, per_day: int = 5, seed: int = 0, avoid_days: int = 30,
             consume_rate: float = 0.6, params: dict = dp.PARAMS, start: datetime | None = None,
             rpc_only: bool = False, steal: float = 0.0) -> dict:
    """按天发牌 days 天, 每天一批 per_day 张。回 {"cards": [...], "batches": [...], "requested", "delivered"}。
    rpc_only / steal 见模块说明。"""
    rng = random.Random(seed)
    crng = random.Random(10_007 * seed + 1)
    srng = random.Random(20_011 * seed + 3)
    start = start or SIM_START
    ledger: list[dict] = []
    by_key: dict[str, list[dict]] = {}
    cards: list[dict] = []
    batches: list[dict] = []
    win = max(30, int(avoid_days))
    for day in range(int(days)):
        now = start + timedelta(days=day)
        rows = [r for r in reversed(ledger)
                if r["_t"] >= now - timedelta(days=win)
                or (r["_c"] is not None and r["_c"] >= now - timedelta(days=win))]
        state = dp.ledger_state(None if rpc_only else rows, now, avoid_days, params=params)
        plan = dp.slot_plan(per_day, profile, rng, params=params)
        cands, meta = dp.build_candidates(profile, state, plan, rng, params=params)
        if steal > 0:                                     # 队友同一时刻抢走一部分主牌(记进台账, 一天内抽过 → L0 挡)
            for c in cands[:len(meta["primaries"])]:
                if srng.random() < steal:
                    row = {"id": len(ledger) + 1, "angle_key": c["angle_key"], "dims": c["dims"], "drawn_by": "mate",
                           "drawn_at": _iso(now), "_t": now, "consumed_at": None, "_c": None,
                           "consumed_version_id": None}
                    ledger.append(row)
                    by_key.setdefault(c["angle_key"], []).append(row)
        taken = []
        for c in cands:                                   # 假 RPC: 按顺序取前 per_day 个可用的
            if len(taken) >= per_day:
                break
            if _l0_blocked(by_key.get(c["angle_key"], []), now, avoid_days):
                continue
            taken.append(c)
        bcards = []
        for c in taken:
            consumed = crng.random() < consume_rate
            row = {"id": len(ledger) + 1, "angle_key": c["angle_key"], "dims": c["dims"], "drawn_by": "sim",
                   "drawn_at": _iso(now), "_t": now,
                   "consumed_at": _iso(now + timedelta(hours=20)) if consumed else None,
                   "_c": now + timedelta(hours=20) if consumed else None,
                   "consumed_version_id": f"v{len(ledger) + 1}" if consumed else None}
            ledger.append(row)
            by_key.setdefault(c["angle_key"], []).append(row)
            d = c["dims"]["_draw"]
            card = {"day": day, "src": d.get("src"), "i": d.get("i"), "cell": dp.cell_of(c["dims"]),
                    "key": c["angle_key"], "batch": meta["batch"], "ref": d.get("ref"), "g": d.get("g", 0),
                    "l1": d.get("l1")}
            cards.append(card)
            bcards.append(card)
        batches.append({"day": day, "batch": meta["batch"], "cards": bcards, "plan": meta["plan"],
                        "short": meta["short"], "n_candidates": meta["n_candidates"]})
    return {"cards": cards, "batches": batches, "requested": int(days) * int(per_day), "delivered": len(cards),
            "days": int(days), "per_day": int(per_day), "seed": seed}


# ══════════════════════════════════════════════════════════════════════
# 指标与闸
# ══════════════════════════════════════════════════════════════════════

def _share(cards, idx, v) -> float:
    return sum(1 for c in cards if c["cell"][idx] == v) / len(cards) if cards else 0.0


def metrics(sim: dict, profile: dict, *, params: dict = dp.PARAMS) -> dict:
    pi = dp.targets(profile)
    cards = [c for c in sim["cards"] if c["cell"]]
    nonx = [c for c in cards if c["src"] != "x"]
    days = sim["days"]
    support = [v for v in dp.VALUES[F] if pi[F][v] > 0]
    fmt_share = {v: _share(cards, 2, v) for v in dp.VALUES[F]}
    off = sum(1 for c in cards if pi[F][c["cell"][2]] <= 0)
    top_fmt = max(support, key=lambda v: pi[F][v]) if support else None
    top_arc = max(dp.VALUES[A], key=lambda v: pi[A][v])

    def window(lo, hi):
        return [c for c in cards if lo <= c["day"] < hi]

    # 任意 14 天(完整窗口; 不满 14 天就整段)里最多的杠杆占比
    lev_max = 0.0
    for lo in range(0, max(days - 13, 1)):
        w = window(lo, lo + 14)
        if w:
            counts: dict = {}
            for c in w:
                counts[c["cell"][0]] = counts.get(c["cell"][0], 0) + 1
            lev_max = max(lev_max, max(counts.values()) / len(w))
    last14 = window(days - 14, days)
    l14_lev: dict = {}
    shapes14: dict = {}
    for c in last14:
        l14_lev[c["cell"][0]] = l14_lev.get(c["cell"][0], 0) + 1
        s = (c["cell"][2], c["cell"][3], c["cell"][1])
        shapes14[s] = shapes14.get(s, 0) + 1
    top_shape = max(shapes14, key=lambda s: (shapes14[s], s)) if shapes14 else None
    shape_cap = None
    if top_shape:
        pis = pi[F][top_shape[0]] * pi[T][top_shape[1]] * pi[A][top_shape[2]]
        shape_cap = max(params["l2_cap_min"], math.ceil(params["l2_cap_mult"] * pis * len(last14) - 1e-9))
    # 30 天内精确 key 不重复(按 30 天滑窗, 不满 30 天就整段)
    distinct30 = 1.0
    for lo in range(0, max(days - 29, 1)):
        w = window(lo, lo + 30)
        if w:
            distinct30 = min(distinct30, len({c["key"] for c in w}) / len(w))
    # 14 天内重发的精确 key(反证: 关掉 L1 时应该出现)
    last_seen: dict = {}
    repeats14 = 0
    for c in cards:
        d0 = last_seen.get(c["key"])
        if d0 is not None and c["day"] - d0 <= 14:
            repeats14 += 1
        last_seen[c["key"]] = c["day"]
    in_batch = 0
    for b in sim["batches"]:
        cs = [c for c in b["cards"] if c["cell"]]
        sig = [(c["cell"][2], c["cell"][3], c["cell"][1]) for c in cs]
        pairs = [(c["cell"][0], c["cell"][1]) for c in cs]
        refs = [c["ref"] for c in cs if c.get("ref")]
        if len(set(sig)) < len(sig) or len(set(pairs)) < len(pairs) or len(set(refs)) < len(refs):
            in_batch += 1
    src: dict = {}
    for c in cards:
        src[c["src"]] = src.get(c["src"], 0) + 1
    return {
        "cards": len(cards), "requested": sim["requested"], "delivered": sim["delivered"], "sources": src,
        "top_format": top_fmt, "fmt_target": pi[F].get(top_fmt, 0.0) if top_fmt else 0.0,
        "fmt_nonx": _share(nonx, 2, top_fmt) if top_fmt else 0.0,
        "fmt_share": fmt_share, "off_support": off,
        "fmt_dev_max": max((abs(fmt_share[v] - pi[F][v]) for v in dp.VALUES[F]), default=0.0),
        "q_target": pi[T][QUESTION], "q_nonx": _share(nonx, 3, QUESTION), "q_x": _share(
            [c for c in cards if c["src"] == "x"], 3, QUESTION),
        "top_arc": top_arc, "arc_target": pi[A][top_arc], "arc_nonx": _share(nonx, 1, top_arc),
        "lever14_max": lev_max,
        "lever14_last": (max(l14_lev.values()) / len(last14)) if last14 else 0.0,
        "distinct14": [len({c["key"] for c in last14}), len(last14)],
        "distinct30": distinct30, "repeats14": repeats14,
        "shape14_max": shapes14.get(top_shape, 0) if top_shape else 0, "shape14_cap": shape_cap,
        "in_batch_repeats": in_batch,
        "l1_relaxed": sum(1 for c in cards if c.get("l1") == "relaxed" and c["src"] != "pin"),
    }


def _mean(ms: list[dict], key: str) -> float:
    return sum(float(m[key]) for m in ms) / len(ms) if ms else 0.0


def run(profile: dict, *, days: int = 28, per_day: int = 5, seeds=range(5), params: dict = dp.PARAMS,
        avoid_days: int = 30, consume_rate: float = 0.6, rpc_only: bool = False, steal: float = 0.0) -> dict:
    """几个 seed 各跑一遍, 指标取平均; per_day ≤ 5 时判闸(10 张/天只报数)。"""
    per_seed = []
    for s in seeds:
        sim = simulate(profile, days=days, per_day=per_day, seed=s, params=params, avoid_days=avoid_days,
                       consume_rate=consume_rate, rpc_only=rpc_only, steal=steal)
        per_seed.append(metrics(sim, profile, params=params))
    avg_keys = ("fmt_nonx", "fmt_dev_max", "q_nonx", "q_x", "arc_nonx", "lever14_max", "lever14_last", "distinct30",
                "off_support", "in_batch_repeats", "shape14_max", "repeats14", "delivered", "l1_relaxed")
    avg = {k: _mean(per_seed, k) for k in avg_keys}
    first = per_seed[0] if per_seed else {}
    for k in ("top_format", "fmt_target", "q_target", "top_arc", "arc_target", "requested"):
        avg[k] = first.get(k)
    avg["distinct14"] = [sum(m["distinct14"][0] for m in per_seed), sum(m["distinct14"][1] for m in per_seed)]
    avg["shape14_cap_min"] = min((m["shape14_cap"] for m in per_seed if m["shape14_cap"] is not None), default=None)
    avg["shape14_max_max"] = max((m["shape14_max"] for m in per_seed), default=0)
    avg["sources"] = {}
    for m in per_seed:
        for k, v in m["sources"].items():
            avg["sources"][k] = avg["sources"].get(k, 0) + v / len(per_seed)
    gated = per_day <= 5
    gates = {}
    if gated:
        for name, (label, fn) in GATES.items():
            gates[name] = {"label": label, "pass": bool(fn(avg))}
    return {"project": profile.get("project_id"), "stage": profile.get("stage"), "days": days, "per_day": per_day,
            "seeds": list(seeds), "gated": gated, "pass": all(g["pass"] for g in gates.values()) if gated else None,
            "gates": gates, "mean": avg, "per_seed": per_seed}


def format_report(rep: dict) -> str:
    m = rep["mean"]
    lines = [f"angle-sim {rep['project']} · 阶段 {rep['stage']} · {rep['days']} 天 × {rep['per_day']} 张/天 · "
             f"seed {rep['seeds']}(取平均)",
             f"  发到 {m['delivered']:.1f} / 要 {m['requested']} · 来源(每个 seed 平均) "
             + " ".join(f"{k} {v:.1f}" for k, v in sorted(m["sources"].items())),
             f"  {m['top_format']}(非试新): {m['fmt_nonx']:.3f}  目标 {m['fmt_target']:.3f} · 各形式离 π 最远 "
             f"{m['fmt_dev_max']:.3f} · 配额外 {m['off_support']:.0f} 张",
             f"  疑问句(非试新): {m['q_nonx']:.3f}  目标 {m['q_target']:.3f} · 试新牌 {m['q_x']:.3f}",
             f"  {m['top_arc']}(非试新): {m['arc_nonx']:.3f}  目标 {m['arc_target']:.3f}",
             f"  最近 14 天精确 key 不重复 {m['distinct14'][0]}/{m['distinct14'][1]} · 30 天滑窗最低 "
             f"{m['distinct30']:.3f} · 14 天内重发 {m['repeats14']:.1f}",
             f"  最近 14 天同一形状最多 {m['shape14_max']:.1f} 次(最多的一个 seed {m['shape14_max_max']}, "
             f"封顶 {m['shape14_cap_min']}) · 杠杆最多占比: 最近 14 天 {m['lever14_last']:.3f}、"
             f"任意 14 天 {m['lever14_max']:.3f}",
             f"  一批之内撞形状 / 撞 (杠杆, 原型) / 撞参考帖的批数 {m['in_batch_repeats']:.1f} · "
             f"没 pin 而放宽 L1 的牌 {m['l1_relaxed']:.1f}"]
    if rep["gated"]:
        lines.append("闸(§5.6): " + ("全过" if rep["pass"] else "没过"))
        for g in rep["gates"].values():
            lines.append(f"  {'✓' if g['pass'] else '✗'} {g['label']}")
    else:
        lines.append("10 张/天只报数, 不判闸。")
    return "\n".join(lines)
