"""deskcore/drawpolicy.py — 发牌 v2 的策略(angle cards v2 §2.4–§2.9): 纯函数, 不碰库、不碰网。

输入三样: Profile(priors.get_profile 给的普通 dict)、台账状态(ledger_state, 由 store.ledger_window 的行算出来)、
一个 random.Random。输出给 deskcore_reserve_angles RPC 的有序、过量供给的候选表 —— RPC 的契约一个字不改(按顺序取前
n 个可用的、原子预留、避重在 RPC 里), 这里只决定【候选的先后】和每张牌带进台账的记账块 ``dims._draw``。

一批怎么发(§2.4–§2.7):
  1. slot_plan: n 个槽位分四种来源 —— w 按效果加权、x 试新(格式仍在项目配额里)、e 照外部同品类涨得快的帖的结构、
     h 照本项目(冷启动时同品牌上一期)评论排前一成的帖。份额按阶段定, 张数用系统抽样取整(期望正好 n·s); 有外部
     参考时一批至少 1 张、至多 4 张。内容形式同样按 π_fmt 系统取整成 n 个标签, 来源表和形式表各洗一次再配对。
  2. 每个槽位在 12 × 19 × 8 个 (杠杆, 原型, 标题) 格子里按权重抽一个(Efraimidis–Spirakis: key = ln u / w, 取最大):
        w: π_lev·π_arc·π_ttl · D · R · B        x: D · B(三维均匀)
        e: π_lev·π_arc·1[t = 参考帖的标题句式] · D · R · B′(B 去掉标题封顶)
        h: 起量帖自己的 (杠杆, 原型, 标题); D·B 为 0 时依次换原型 / 标题 / 杠杆
     D 跨批次去重(每种来源一样: 两条臂面对同一套去重), R 向目标回拉(只 w/e/h), B 一批之内(见 §2.6 的表)。
  3. 候选表 = n 张主牌 + 每个槽位 2 张备牌(轮流排) + 按 w 分布在全部配额形式上接着抽的尾巴, 凑到
     max(20n, 500) 个不同的 key(封顶 2000)。按 angle_key 去重。RPC 跳过一张主牌时顶上来的是表里接下来的那张, 不一定
     是那个槽位自己的备牌, 所以备牌和尾巴都要过【整批】的 B、不带主牌已经用了的参考帖, 彼此撞形状 / (杠杆, 原型) 的
     排到后一档(复审; 见 build_candidates)。

每张候选: {"angle_key": fp.angle_key(四个维度), "dims": {四个维度, "_draw": {...}}}。fp.angle_key 只认四个主维度,
_draw 不进 key; RPC 把 dims 原样存进台账、原样回来 —— 预留到哪一张, 那一张就带着自己真实的来源(§2.7)。
_draw 的键(§5.1): v pol b i src sp pn pl g p pu pv st ps dd [fa ref reuse pin of l1 pp]。pp 只在项目块不是新鲜的时候带
(stale 旧的 / table 只读到对照表、配比从全局表或快照里拼 / none 都没有)。pn / pl 是这一批的槽位数和各来源份额(整张计划,
每张都带; draw-report 的来源计划按批算 n·份额), sp 是这一张实际落下的来源的份额。

去重为什么是相对的(P1 / P2 的平衡): 绝对的形状惩罚会把项目自己的主形状压下去(v2 的健康焦虑 14% vs 已发 28%);
这里一个形状只在跑到目标份额以上时才降温, 另有硬封顶。精确 key 30 天不重发(L1)本身就把任何一种
(形式, 标题, 原型) 形状限制在 30 天 12 次(每个杠杆一次)。

常数都在 PARAMS(以 priors.PARAMS 为底), §5.5 的预注册决策规则每条都是改一个常数。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from . import priors, vocab
from .fingerprint import angle_key

# ── 维度 ──────────────────────────────────────────────────────────────────
L, A, F, T = "emotional_lever", "human_truth_archetype", "content_format", "title_structure"
DIMS4 = (L, A, F, T)                    # 格子 cell = (杠杆, 原型, 形式, 标题), 同 fp.angle_key 的顺序
SHAPE = (F, T, A)                       # 形状 σ = (形式, 标题, 原型)
VALUES = {d: tuple(priors.VALUES[d]) for d in DIMS4}
QUESTION = priors.QUESTION

# ── 常数(§2.11) ──────────────────────────────────────────────────────────
PARAMS: dict = {
    **priors.PARAMS,
    # 槽位份额(§2.4): 阶段 cold / none 时外部 0.25, 其余 0.20; 用不上的份额归 w(从不给跨品类的起量帖)
    "share_x": 0.20, "share_e": 0.20, "share_e_cold": 0.25, "share_h": 0.10,
    "share_e_floor": 0.10,              # 老板定的外部下限: 决策规则 3 往下调也不低于它
    "ext_min_n": 3, "ext_max": 4,       # 一批 ≥3 张才保底 1 张外部; 一批至多 4 张外部(多出来的归 w)
    "hit_min_n": 3, "hit_min_anchors": 3,
    # 跨批次去重(§2.6)
    "l1": True,                         # 精确 key: 抽过(任何一行)max(30, avoid_days) 天内 / 写过 avoid_days 天内 → 0
    "l1_days": 30, "l1_relax_days": 7, "l1_min_cells": 3,
    "l2_mode": "relative",              # "absolute" 只给反证测试用: 0.4^T, 不减目标份额
    "l2_base": 0.4, "l2_tau_days": 7.0, "l2_window_days": 21,
    "l2_cap_days": 14, "l2_cap_min": 3, "l2_cap_mult": 1.5, "l2_cap_factor": 0.01,
    "l2b_days": 7, "l2b_factor": 0.6,
    "r_min_m14": 20, "r_days": 14, "r_pseudo": 2.0, "r_clip": (0.5, 1.5), "r_exp": 0.5,
    # 一批之内(B)
    "b_lev_frac": 0.40, "b_arc_frac": 0.35, "b_min": 2,
    # 参考帖冷却(L5)
    "ext_cooldown_days": 21, "hit_cooldown_days": 14,
    # 候选表(§2.7)
    "alternates": 2, "tail_mult": 20, "tail_min": 500, "tail_cap": 2000,
}

SOURCES = ("w", "x", "e", "h")
MAX_REPEAT = 3
PIN_DIM_ALIASES = {"内容形式": F, "标题句式": T, "情绪杠杆": L, "人性原型": A,
                   "format": F, "title": T, "lever": L, "archetype": A}
DIM_LABEL = {F: "内容形式", T: "标题句式", L: "情绪杠杆", A: "人性原型"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sig3(x: float) -> float:
    return float(f"{x:.3g}") if x else 0.0


def cell_dims(cell) -> dict:
    return {L: cell[0], A: cell[1], F: cell[2], T: cell[3]}


def cell_of(dims) -> tuple | None:
    """dims → (杠杆, 原型, 形式, 标题); 有一维不在词表里回 None(老行 / 坏行不进形状统计)。"""
    if not isinstance(dims, dict):
        return None
    c = tuple(dims.get(d) for d in DIMS4)
    return c if all(v in VALUES[d] for v, d in zip(c, DIMS4)) else None


def targets(profile) -> dict:
    """Profile 的四张 π 表, 只留词表里的值、归一; 一维缺了 / 全零就均匀。内容形式 π 为 0 的就是配额外(不发)。"""
    pi = (profile or {}).get("pi") or {}
    out = {}
    for d in DIMS4:
        src = pi.get(d) or {}
        raw = {v: max(float(src.get(v) or 0.0), 0.0) for v in VALUES[d]}
        s = sum(raw.values())
        out[d] = {v: raw[v] / s for v in VALUES[d]} if s > 0 else {v: 1.0 / len(VALUES[d]) for v in VALUES[d]}
    return out


# ══════════════════════════════════════════════════════════════════════
# 张数取整(§2.4)
# ══════════════════════════════════════════════════════════════════════

def systematic_round(n: int, shares: dict, rng) -> dict:
    """把 n 按 shares 分成整数张: 各取 floor(n·s_k), 余下 R 个名额用系统抽样(Madow)落到 R 个不同的 k 上 ——
    k 多分到一张的概率正好是 n·s_k 的小数部分, 所以每个 k 的期望张数正好是 n·s_k, 也不会有哪个 k 多分两张。
    先用 rng 洗一次 k 的次序(联合分布不依赖字典的先后)。shares 不必归一; 回的字典和 shares 同序、同键。"""
    keys = [k for k in shares if float(shares[k] or 0.0) > 0]
    out = {k: 0 for k in shares}
    tot = sum(float(shares[k]) for k in keys)
    if n <= 0 or tot <= 0:
        return out
    ex = {k: n * float(shares[k]) / tot for k in keys}
    for k in keys:
        out[k] = int(math.floor(ex[k] + 1e-9))
    rest = n - sum(out[k] for k in keys)
    if rest <= 0:
        return out
    fr = [(k, max(ex[k] - out[k], 0.0)) for k in keys]
    rng.shuffle(fr)
    s = sum(f for _, f in fr)
    if s <= 0:                                  # 浮点意外: 按份额大小补
        for k in sorted(keys, key=lambda k: -ex[k])[:rest]:
            out[k] += 1
        return out
    scale = rest / s                            # 小数部分之和就是 rest(浮点误差内), 拉齐
    u = rng.random()
    acc, j = 0.0, 0
    given = set()
    for k, f in fr:
        acc += f * scale
        while j < rest and u + j < acc - 1e-12:
            out[k] += 1
            given.add(k)
            j += 1
    if j < rest:                                # 浮点收尾: 没分完的给小数部分最大、还没多分过的
        for k, _f in sorted(fr, key=lambda kv: -kv[1]):
            if j >= rest:
                break
            if k not in given:
                out[k] += 1
                given.add(k)
                j += 1
    return out


# ══════════════════════════════════════════════════════════════════════
# 槽位计划(§2.4)
# ══════════════════════════════════════════════════════════════════════

def describable(entry: dict, *, external: bool) -> bool:
    """这篇参考帖 / 起量帖在卡上说得出至少一样结构吗(和渲染的参考行同一份口径, vocab.structure_bits; 外部帖的标题句式
    也算一样)。说不出来的不当参考(复审): 特征题没读到(项目块里 answers 是空的)、开头是「其他」而几个是非题都不是「是」
    —— 用了它, 卡上没有参考行, batch_note 却说「参照了…的结构」。"""
    if external and entry.get(T) in VALUES[T]:
        return True
    return bool(vocab.structure_bits(entry.get("desc"), external=external))


def hit_anchors(profile) -> list[dict]:
    """能用的起量帖: 杠杆 / 原型 / 标题句式三样都在词表里、卡上说得出结构的。冷启动(cold / none)只用同品牌上一期的
    (§2.4 表)。"""
    stage = (profile or {}).get("stage") or "none"
    out = []
    for h in (profile or {}).get("hits") or []:
        if not isinstance(h, dict) or not h.get("ref"):
            continue
        if h.get(L) not in VALUES[L] or h.get(A) not in VALUES[A] or h.get(T) not in VALUES[T]:
            continue
        if stage in ("cold", "none") and h.get("own"):
            continue
        if not describable(h, external=False):
            continue
        out.append(h)
    return out


def ext_refs(profile) -> list[dict]:
    """能用的外部参考帖: 有 ref、卡上说得出结构的(ext_ok 的「至少 5 篇」也按这个数)。"""
    ext = (profile or {}).get("ext") or {}
    return [e for e in ext.get("pool") or []
            if isinstance(e, dict) and e.get("ref") and describable(e, external=True)]


def slot_plan(n: int, profile, rng, *, pin: dict | None = None, params: dict = PARAMS) -> dict:
    """一批 n 个槽位的来源和内容形式(§2.4)。

    · ext_ok: n ≥ 3、项目有外部品类、参考池至少 5 篇; hit_ok: n ≥ 3、至少 3 篇可用的起量帖。
    · 份额 x .20 / e .20(cold、none .25)/ h .10 / w 其余; 用不上的归 w。系统抽样取整, 期望正好 n·s。
    · 有外部参考而取整后 e = 0: 从 w(没有就 h, 再没有就 x)拿一个槽给 e, 标 g:1; e 至多 4 张, 多的归 w。
    · 形式按 π_fmt 同样系统取整(pin 了形式就全是它); 来源表和形式表各自用 rng 洗一次再配对 —— 形式和来源独立。
    · pin 时每个槽位都是 src "pin"(写手点名的, 不进 w / x 的对照)。"""
    n = max(int(n or 0), 0)
    stage = (profile or {}).get("stage") or "none"
    ext = (profile or {}).get("ext") or {}
    ext_ok = (n >= params["ext_min_n"] and bool(ext.get("category"))
              and len(ext_refs(profile)) >= params["ext_ok_min"])
    hit_ok = n >= params["hit_min_n"] and len(hit_anchors(profile)) >= params["hit_min_anchors"]
    pin = dict(pin or {})
    guaranteed = False
    if pin:
        shares = {"pin": 1.0}
        counts = {"pin": n}
        srcs = ["pin"] * n
    else:
        s_x = float(params["share_x"])
        s_e = 0.0
        if ext_ok:
            s_e = float(params["share_e_cold"] if stage in ("cold", "none") else params["share_e"])
            s_e = max(s_e, float(params["share_e_floor"]))
        s_h = float(params["share_h"]) if hit_ok else 0.0
        shares = {"w": max(1.0 - s_x - s_e - s_h, 0.0), "x": s_x, "e": s_e, "h": s_h}
        counts = systematic_round(n, shares, rng)
        if ext_ok and counts["e"] == 0:
            for k in ("w", "h", "x"):
                if counts[k] > 0:
                    counts[k] -= 1
                    counts["e"] = 1
                    guaranteed = True
                    break
        if counts["e"] > params["ext_max"]:
            counts["w"] += counts["e"] - params["ext_max"]
            counts["e"] = params["ext_max"]
        srcs = [k for k in SOURCES for _ in range(counts[k])]
        rng.shuffle(srcs)
    pi_f = targets(profile)[F]
    if F in pin:
        fmts = [pin[F]] * n
        fmt_counts = {pin[F]: n}
    else:
        fc = systematic_round(n, {v: pi_f[v] for v in VALUES[F] if pi_f[v] > 0}, rng)
        fmts = [v for v in VALUES[F] for _ in range(fc.get(v, 0))]
        rng.shuffle(fmts)
        fmt_counts = {v: c for v, c in fc.items() if c}
    slots = []
    g_left = guaranteed
    for i, (s, f) in enumerate(zip(srcs, fmts), start=1):
        g = 0
        if s == "e" and g_left:
            g, g_left = 1, False
        slots.append({"i": i, "src": s, "f": f, "sp": round(float(shares.get(s, 0.0)), 3), "g": g})
    return {"n": n, "stage": stage, "ext_ok": ext_ok, "hit_ok": hit_ok, "guaranteed_ext": guaranteed,
            "shares": {k: round(v, 3) for k, v in shares.items()}, "counts": dict(counts),
            "formats": fmt_counts, "pin": pin or None, "slots": slots}


# ══════════════════════════════════════════════════════════════════════
# 台账状态(§2.6 L1–L5 的原料)
# ══════════════════════════════════════════════════════════════════════

def _empty_state(now: datetime, avoid_days: int, *, ok: bool) -> dict:
    return {"ok": ok, "dd": "py" if ok else "rpc_only", "now": now, "avoid_days": int(avoid_days),
            "window_days": max(30, int(avoid_days)), "rows": 0,
            "blocked": {}, "blocked_keys": {}, "unmapped_keys": {},
            "shape_T": {}, "W": 0.0, "shape_n14": {}, "M14": 0,
            "m14": {L: {}, A: {}, T: {}, F: {}}, "pairs7": set(), "ref_age": {}}


def ledger_state(rows, now: datetime | None = None, avoid_days: int = 30, *, params: dict = PARAMS) -> dict:
    """台账行(store.ledger_window 的, dims 带 _draw) → 发牌要的状态。rows 是 None(读失败)时 ok=False、dd="rpc_only":
    Python 这几层(L1、L2、R、参考帖冷却)全跳过, 精确 key 只靠 RPC 挡。

      blocked[cell] = [最近一次抽的天数, 硬挡]       抽过(任何一行)max(30, avoid_days) 天内; 写过 avoid_days 天内是硬挡
      blocked_keys / unmapped_keys                  同上按 angle_key; dims 解不出格子的行只能按 key 挡
      shape_T[σ] = Σ e^(−age/7)(21 天内), W = 所有行的同一个和; shape_n14[σ], M14, m14[维][值](14 天内的计数)
      pairs7 = 7 天内抽过的 (杠杆, 原型); ref_age[ref] = 这篇参考帖最近一次被用是几天前"""
    now = now or _utcnow()
    if rows is None:
        return _empty_state(now, avoid_days, ok=False)
    st = _empty_state(now, avoid_days, ok=True)
    win = st["window_days"]
    tau = float(params["l2_tau_days"])
    for r in rows:
        if not isinstance(r, dict):
            continue
        st["rows"] += 1
        key = str(r.get("angle_key") or "")
        dims = r.get("dims") if isinstance(r.get("dims"), dict) else {}
        cell = cell_of(dims)
        drawn = priors._ts(r.get("drawn_at")) if r.get("drawn_at") else None
        consumed = priors._ts(r.get("consumed_at")) if r.get("consumed_at") else None
        age = max(priors._days(now, drawn), 0.0) if drawn is not None else None
        hard = (r.get("consumed_version_id") is not None and consumed is not None
                and priors._days(now, consumed) <= avoid_days)
        soft = age is not None and age <= win
        if hard or soft:
            a = age if age is not None else float(win)
            for table, k in ((st["blocked"], cell), (st["blocked_keys"], key or None),
                             (st["unmapped_keys"], key if (cell is None and key) else None)):
                if k is None:
                    continue
                prev = table.get(k)
                table[k] = [min(a, prev[0]) if prev else a, bool(hard or (prev and prev[1]))]
        d = dims.get("_draw") if isinstance(dims.get("_draw"), dict) else {}
        ref = d.get("ref")
        if ref and age is not None:
            ref = str(ref)
            st["ref_age"][ref] = min(age, st["ref_age"].get(ref, age))
        if cell is None or age is None:
            continue
        lev, arc, fmt, ttl = cell
        sigma = (fmt, ttl, arc)
        if age <= params["l2_window_days"]:
            e = math.exp(-age / tau)
            st["shape_T"][sigma] = st["shape_T"].get(sigma, 0.0) + e
            st["W"] += e
        if age <= params["l2_cap_days"]:
            st["shape_n14"][sigma] = st["shape_n14"].get(sigma, 0) + 1
        if age <= params["r_days"]:
            st["M14"] += 1
            for dim, v in ((L, lev), (A, arc), (T, ttl), (F, fmt)):
                st["m14"][dim][v] = st["m14"][dim].get(v, 0) + 1
        if age <= params["l2b_days"]:
            st["pairs7"].add((lev, arc))
    return st


# ══════════════════════════════════════════════════════════════════════
# 三个因子(§2.6): D 跨批次、R 回拉、B 一批之内
# ══════════════════════════════════════════════════════════════════════

def l1_factor(cell, state, *, relaxed: bool = False, params: dict = PARAMS) -> float:
    """L1 精确 key: 抽过 max(30, avoid_days) 天内 / 写过 avoid_days 天内 → 0。relaxed(槽位里正权重的格子不到 3 个,
    只在重度 pin 时出现)只挡 7 天内抽过的和写过的。台账读失败(rpc_only)或 PARAMS["l1"] 关掉时恒为 1。"""
    if not state.get("ok") or not params["l1"]:
        return 1.0
    b = state["blocked"].get(tuple(cell))
    if not b:
        return 1.0
    age, hard = b
    if hard or not relaxed or age <= params["l1_relax_days"]:
        return 0.0
    return 1.0


def l2_factor(sigma, state, pi, *, params: dict = PARAMS) -> float:
    """L2 近似形状 σ = (形式, 标题, 原型): 0.4^max(0, T(σ) − E(σ)), E(σ) = π_f·π_t·π_a·W —— 只在跑到目标份额以上时
    降温(相对); 14 天内用了 ≥ max(3, ⌈1.5·π_σ·M₁₄⌉) 次再 ×0.01(硬封顶)。"""
    if not state.get("ok"):
        return 1.0
    fmt, ttl, arc = sigma
    tsum = state["shape_T"].get(tuple(sigma), 0.0)
    pis = pi[F][fmt] * pi[T][ttl] * pi[A][arc]
    fac = 1.0
    if tsum > 0:
        if params["l2_mode"] == "absolute":
            excess = tsum
        else:
            excess = max(0.0, tsum - pis * state["W"])
        fac = params["l2_base"] ** excess
    n14 = state["shape_n14"].get(tuple(sigma), 0)
    if n14:
        cap = max(params["l2_cap_min"], math.ceil(params["l2_cap_mult"] * pis * state["M14"] - 1e-9))
        if n14 >= cap:
            fac *= params["l2_cap_factor"]
    return fac


def l2b_factor(lev, arc, state, *, params: dict = PARAMS) -> float:
    """L2b: (杠杆, 原型) 这一对 7 天内抽过 → ×0.6。"""
    if state.get("ok") and (lev, arc) in state["pairs7"]:
        return params["l2b_factor"]
    return 1.0


def d_factor(cell, state, pi, *, relaxed: bool = False, l2: bool = True, params: dict = PARAMS) -> float:
    """D = L1 · L2 · L2b。每种来源都一样(两条臂面对同一套去重)。l2=False: 两个以上形状维度被 pin 了(§2.9)。"""
    lev, arc, fmt, ttl = cell
    f1 = l1_factor(cell, state, relaxed=relaxed, params=params)
    if f1 <= 0:
        return 0.0
    f2 = l2_factor((fmt, ttl, arc), state, pi, params=params) if l2 else 1.0
    return f1 * f2 * l2b_factor(lev, arc, state, params=params)


def r_value(dim: str, v: str, state, pi, *, params: dict = PARAMS) -> float:
    """R 的一维: clip((π_d(v)·M₁₄ + 2) / (m₁₄,d(v) + 2), 0.5, 1.5)^0.5; 14 天内不到 20 张时恒为 1。"""
    if not state.get("ok") or state.get("M14", 0) < params["r_min_m14"]:
        return 1.0
    m = state["m14"].get(dim, {}).get(v, 0)
    x = (pi[dim][v] * state["M14"] + params["r_pseudo"]) / (m + params["r_pseudo"])
    lo, hi = params["r_clip"]
    return min(hi, max(lo, x)) ** params["r_exp"]


def r_factor(cell, state, pi, *, skip=(), params: dict = PARAMS) -> float:
    """R = Π over 杠杆 / 原型 / 标题 的 r_value(只给 w / e / h; pin 了的维度跳过)。"""
    lev, arc, _fmt, ttl = cell
    out = 1.0
    for dim, v in ((L, lev), (A, arc), (T, ttl)):
        if dim not in skip:
            out *= r_value(dim, v, state, pi, params=params)
    return out


class Batch:
    """一批之内已经发了什么(B 因子用)。σ 已在批里 → 0; (杠杆, 原型) 已在批里 → 0; 某个杠杆到 max(2, ⌈0.4n⌉)、某个原型到
    max(2, ⌈0.35n⌉) → 0; w / h(和 pin)另有: 非试新牌里某个标题句式到 ⌈π_ttl(t)·n_nonx⌉ + 1 → 0。
    同一个格子不能在一批里出现两次。pin 了的维度不设封顶; 每一维都被 pin 了的那条唯一性约束也不设(否则一批只能发一张)。"""

    def __init__(self, n: int, n_nonx: int, pi, *, pinned=(), params: dict = PARAMS):
        self.cells: set = set()
        self.sigmas: set = set()
        self.pairs: set = set()
        self.lev: dict = {}
        self.arc: dict = {}
        self.ttl: dict = {}
        pinned = set(pinned or ())
        self.lev_cap = None if L in pinned else max(params["b_min"], math.ceil(params["b_lev_frac"] * n - 1e-9))
        self.arc_cap = None if A in pinned else max(params["b_min"], math.ceil(params["b_arc_frac"] * n - 1e-9))
        self.ttl_cap = None if T in pinned else {t: math.ceil(pi[T][t] * n_nonx - 1e-9) + 1 for t in VALUES[T]}
        self.sigma_ban = not all(d in pinned for d in SHAPE)
        self.pair_ban = not (L in pinned and A in pinned)

    def lev_full(self, lev) -> bool:
        return self.lev_cap is not None and self.lev.get(lev, 0) >= self.lev_cap

    def arc_full(self, arc) -> bool:
        return self.arc_cap is not None and self.arc.get(arc, 0) >= self.arc_cap

    def ttl_full(self, ttl) -> bool:
        return self.ttl_cap is not None and self.ttl.get(ttl, 0) >= self.ttl_cap[ttl]

    def add(self, cell, src: str) -> None:
        lev, arc, fmt, ttl = cell
        self.cells.add(tuple(cell))
        self.sigmas.add((fmt, ttl, arc))
        self.pairs.add((lev, arc))
        self.lev[lev] = self.lev.get(lev, 0) + 1
        self.arc[arc] = self.arc.get(arc, 0) + 1
        if src != "x":
            self.ttl[ttl] = self.ttl.get(ttl, 0) + 1


def b_factor(cell, batch: Batch, src: str) -> float:
    """B: 一批之内的禁令(0 或 1)。标题封顶只对 w / h / pin; e 用 B′(不设标题封顶), x 也不设。"""
    lev, arc, fmt, ttl = cell
    if tuple(cell) in batch.cells:
        return 0.0
    if batch.sigma_ban and (fmt, ttl, arc) in batch.sigmas:
        return 0.0
    if batch.pair_ban and (lev, arc) in batch.pairs:
        return 0.0
    if batch.lev_full(lev) or batch.arc_full(arc):
        return 0.0
    if src in ("w", "h", "pin") and batch.ttl_full(ttl):
        return 0.0
    return 1.0


# ══════════════════════════════════════════════════════════════════════
# 抽(§2.7)
# ══════════════════════════════════════════════════════════════════════

def es_key(w: float, rng) -> float:
    """Efraimidis–Spirakis 的 key: ln(u)/w, u 来自 rng; key 最大的中签。w ≤ 0 的格子不参加(也不消耗随机数)。"""
    u = rng.random()
    while u <= 0.0:
        u = rng.random()
    return math.log(u) / w


def es_pick(weights, rng, k: int = 1) -> list:
    """按权重不放回抽 k 个(key 最大的 k 个): weights 是 [(项, 权重)] 或 {项: 权重}, 次序决定随机数的消耗次序。"""
    items = weights.items() if isinstance(weights, dict) else weights
    top: list = []
    for item, w in items:
        if not w or w <= 0:
            continue
        key = es_key(w, rng)
        if len(top) < k:
            top.append((key, item))
            top.sort(key=lambda kv: -kv[0])
        elif key > top[-1][0]:
            top[-1] = (key, item)
            top.sort(key=lambda kv: -kv[0])
    return [item for _, item in top]


def _ref_w(e: dict) -> float:
    return max(float(e.get("w") or 0.0), 0.0) or 1e-9


def _fresh_refs(pool, *, ref_age, cooldown_days: float, in_batch=()) -> list:
    """没在冷却期里、这一批也还没用过的参考帖(pick_reference 和 p 的边际概率用同一份)。"""
    ref_age = ref_age or {}
    used = set(in_batch or ())
    return [e for e in pool or () if e.get("ref") and e["ref"] not in used
            and (ref_age.get(e["ref"]) is None or ref_age[e["ref"]] > cooldown_days)]


def pick_reference(pool, rng, *, ref_age=None, cooldown_days: float, in_batch=(), prefer=None):
    """从参考池里挑一篇(§2.5 / L5): 权重是条目的 w(外部 √日均评论, 起量帖 本项目 ×3 / 同品牌 ×1)。

    冷却期(这个写作台项目 cooldown_days 天内用过的)和这一批已经用过的不挑; prefer(起量帖: 形式和槽位一样的)在没冷却的
    里面优先。全在冷却里就重用最久没用的那篇(reuse=1)。回 (条目, 被挑中的概率, reuse) 或 (None, 0.0, 0)。"""
    ref_age = ref_age or {}
    used = set(in_batch or ())
    avail = [e for e in pool or () if e.get("ref") and e["ref"] not in used]
    if not avail:
        return None, 0.0, 0
    fresh = _fresh_refs(avail, ref_age=ref_age, cooldown_days=cooldown_days)
    if fresh:
        if prefer is not None:
            pref = [e for e in fresh if prefer(e)]
            fresh = pref or fresh
        ws = [(i, _ref_w(e)) for i, e in enumerate(fresh)]
        tot = sum(w for _, w in ws)
        i = es_pick(ws, rng)[0]
        return fresh[i], ws[i][1] / tot, 0
    if prefer is not None:
        avail = [e for e in avail if prefer(e)] or avail
    lru = max(avail, key=lambda e: (ref_age.get(e["ref"], 0.0), e["ref"]))
    return lru, 1.0, 1


class _Ctx:
    """一次发牌的不变部分: π、pin、各因子的表。每种形式算一张表(D 和不带 B 的权重, 只留 D > 0 的格子), 热循环里只剩
    集合查找、一次随机数和一次 log。"""

    def __init__(self, profile, state, *, pin=None, params: dict = PARAMS):
        self.params = params
        self.pi = targets(profile)
        self.pin = dict(pin or {})
        self.state = state
        self.vals = {d: ((self.pin[d],) if d in self.pin else VALUES[d]) for d in DIMS4}
        ok = bool(state.get("ok"))
        # L2 在两个以上形状维度被 pin 时跳过; R 跳过被 pin 的维度(§2.9)
        self.l2_on = ok and sum(1 for d in SHAPE if d in self.pin) < 2
        self.l1_on = ok and bool(params["l1"])
        self.blocked = (state.get("blocked") or {}) if self.l1_on else {}
        skip = set(self.pin)
        self.r = {d: {v: (1.0 if d in skip else r_value(d, v, state, self.pi, params=params)) for v in VALUES[d]}
                  for d in (L, A, T)}
        self.pairs7 = (state.get("pairs7") or set()) if ok else set()
        self._l2: dict = {}
        self._tabs: dict = {}
        self.relax_days = params["l1_relax_days"]
        self.has_soft = any(not b[1] for b in self.blocked.values())

    def l2(self, sigma) -> float:
        v = self._l2.get(sigma)
        if v is None:
            v = l2_factor(sigma, self.state, self.pi, params=self.params) if self.l2_on else 1.0
            self._l2[sigma] = v
        return v

    def d(self, cell, relaxed: bool = False) -> float:
        if self.blocked:
            b = self.blocked.get(cell)
            if b and (b[1] or not relaxed or b[0] <= self.relax_days):
                return 0.0
        lev, arc, fmt, ttl = cell
        f = self.l2((fmt, ttl, arc))
        if (lev, arc) in self.pairs7:
            f *= self.params["l2b_factor"]
        return f

    def table(self, f: str, relaxed: bool = False) -> list:
        """形式 f 的表: [(杠杆, [(原型, [(标题, D, π_l·π_a·π_t·R·D, π_l·π_a·R·D)])])], 只留 D > 0 的格子。"""
        key = (f, relaxed)
        tab = self._tabs.get(key)
        if tab is not None:
            return tab
        p_l, p_a, p_t = self.pi[L], self.pi[A], self.pi[T]
        r_l, r_a, r_t = self.r[L], self.r[A], self.r[T]
        tab = []
        for lev in self.vals[L]:
            row = []
            for arc in self.vals[A]:
                base = p_l[lev] * p_a[arc] * r_l[lev] * r_a[arc]
                cells = []
                for ttl in self.vals[T]:
                    dd = self.d((lev, arc, f, ttl), relaxed)
                    if dd > 0:
                        bwt = base * r_t[ttl] * dd
                        cells.append((ttl, dd, bwt * p_t[ttl], bwt))
                if cells:
                    row.append((arc, cells))
            if row:
                tab.append((lev, row))
        self._tabs[key] = tab
        return tab


_TINY = 1e-300


def _scan(ctx: _Ctx, batch: Batch, f: str, src: str, rng, *, t_only=None, relaxed=False, k: int = 3,
          es: bool = True, collect: dict | None = None, by_title: dict | None = None) -> dict:
    """一个槽位(形式 f 定了)在全部 (杠杆, 原型, 标题) 格子上的一遍: 权重 w(按来源)、试新分布 DB(= D·B_x, 给 pu)、
    ES 的前 k 名。t_only: e 槽只在参考帖的标题句式上有权重(DB 仍按全部标题算)。

    w: π_l·π_a·π_t·R·D · B    e: π_l·π_a·R·D · 1[t = t_only] · B′    x: D · B    pin: 同 w(限在 pin 的值上)
    by_title(给 e 槽算边际概率): 按标题句式累加 π_l·π_a·R·D(过了 B′ 的格子) —— 就是参考帖是这一类时那一遍的 sw。"""
    lev_ban = {v for v in ctx.vals[L] if batch.lev_full(v)}
    arc_ban = {v for v in ctx.vals[A] if batch.arc_full(v)}
    pair_ban = batch.pairs if batch.pair_ban else ()
    sig_ban = {(t, a) for (ff, t, a) in batch.sigmas if ff == f} if batch.sigma_ban else set()
    cell_ban = {(lv, a, t) for (lv, a, ff, t) in batch.cells if ff == f}
    ttl_ban = ({v for v in ctx.vals[T] if batch.ttl_full(v)} if src in ("w", "h", "pin") else set())
    if t_only is not None:
        ttl_ban = ttl_ban | {v for v in VALUES[T] if v != t_only}
    mode = 0 if src == "x" else (2 if t_only is not None else 1)
    rnd = rng.random if es else None
    log = math.log
    top: list = []
    kth = -math.inf
    sw = sdb = 0.0
    npos = 0
    for lev, row in ctx.table(f, relaxed):
        if lev in lev_ban:
            continue
        for arc, cells in row:
            if arc in arc_ban or (lev, arc) in pair_ban:
                continue
            for ttl, dd, bw, bwt in cells:
                if (ttl, arc) in sig_ban or (lev, arc, ttl) in cell_ban:
                    continue
                # pu 的分母不看标题封顶(试新牌本来就不设): 封顶的标题在 w / h / pin 槽里 p = 0、pu 却 > 0, 所以 draw-report 的
                # 「配额内均匀乱抽」只用试新牌估, 不拿 w 牌按 pu / p 加权(review round 2 on aw#140, drawreport.ipw_uniform)
                sdb += dd
                if by_title is not None and bwt > 0:
                    by_title[ttl] = by_title.get(ttl, 0.0) + bwt
                if ttl in ttl_ban:
                    if collect is not None:
                        collect[(lev, arc, f, ttl)] = (0.0, dd)
                    continue
                w = dd if mode == 0 else (bw if mode == 1 else bwt)
                if w <= 0:
                    continue
                sw += w
                npos += 1
                if collect is not None:
                    collect[(lev, arc, f, ttl)] = (w, dd)
                if rnd is not None:
                    key = log(rnd() or _TINY) / w
                    if key > kth:
                        top.append((key, (lev, arc, f, ttl), w, dd))
                        if len(top) > 1:
                            top.sort(key=lambda x: -x[0])
                        if len(top) > k:
                            top.pop()
                        if len(top) == k:
                            kth = top[-1][0]
    return {"top": [(c, w, dd) for _, c, w, dd in top], "sw": sw, "sdb": sdb, "npos": npos}


def _scan_relaxing(ctx, batch, f, src, rng, **kw):
    """L1 放宽(§2.6): 槽位里正权重的格子不到 3 个, 而且有格子只是「抽过、没写过」被挡 → 改成只挡 7 天内抽过的再来一遍。"""
    res = _scan(ctx, batch, f, src, rng, **kw)
    if res["npos"] < ctx.params["l1_min_cells"] and ctx.has_soft:
        res2 = _scan(ctx, batch, f, src, rng, relaxed=True, **kw)
        if res2["npos"] > res["npos"]:
            res2["l1"] = "relaxed"
            return res2
    return res


def _h_steps(ctx: _Ctx, batch: Batch, f: str, anchor: dict) -> tuple:
    """起量帖槽(形式 f)的候选, 不用随机数: (本格, 它的 D 或 0) 和三步退路 [(格子, 权重, D)](换原型 → 标题 → 杠杆;
    一步之内的权重 = 换掉的那一维的 π · D · R)。只看严格的 L1(§2.6 只在重度 pin 时放宽; pin 时没有 h 槽)。"""
    la, aa, ta = anchor[L], anchor[A], anchor[T]
    pi = ctx.pi
    c0 = (la, aa, f, ta)
    d0 = ctx.d(c0)
    d0 = d0 if d0 > 0 and b_factor(c0, batch, "h") > 0 else 0.0
    steps = []
    for dim, cells in ((A, [(la, a, f, ta) for a in VALUES[A] if a != aa]),
                       (T, [(la, aa, f, t) for t in VALUES[T] if t != ta]),
                       (L, [(lv, aa, f, ta) for lv in VALUES[L] if lv != la])):
        ws = []
        for c in cells:
            dd = ctx.d(c)
            if dd <= 0 or b_factor(c, batch, "h") <= 0:
                continue
            w = pi[dim][c[DIMS4.index(dim)]] * dd * ctx.r[L][c[0]] * ctx.r[A][c[1]] * ctx.r[T][c[3]]
            if w > 0:
                ws.append((c, w, dd))
        steps.append(ws)
    return (c0, d0), steps


def _h_chain(ctx: _Ctx, batch: Batch, f: str, anchor: dict, rng) -> list:
    """起量帖槽: 先试起量帖自己的格子(D·B > 0 就用它), 不行依次换原型(按 π_arc)、标题(按 π_ttl)、杠杆(按 π_lev),
    每一步内按 ES 排好。回 [(格子, 条件概率, DB)], 第一个是主牌, 后面的是备牌。"""
    (c0, d0), steps = _h_steps(ctx, batch, f, anchor)
    out = [(c0, 1.0, d0)] if d0 > 0 else []
    for ws in steps:
        if not ws:
            continue
        tot = sum(w for _, w, _ in ws)
        order = es_pick([((c, w, dd), w) for c, w, dd in ws], rng, k=len(ws))
        out.extend((c, w / tot, dd) for c, w, dd in order)
    return out


def _h_first(ctx: _Ctx, batch: Batch, f: str, anchor: dict) -> dict:
    """这篇起量帖定下之后, 槽位那一张(链上第一个)的分布: 本格能用就是它(概率 1), 否则第一步非空的退路里按权重。"""
    (c0, d0), steps = _h_steps(ctx, batch, f, anchor)
    if d0 > 0:
        return {c0: 1.0}
    for ws in steps:
        if ws:
            tot = sum(w for _, w, _ in ws)
            return {c: w / tot for c, w, _ in ws}
    return {}


def _h_marginal(ctx: _Ctx, batch: Batch, f: str, anchors, *, ref_age, used_refs) -> dict:
    """h 槽里每个格子的概率(§2.7 的 p = 这个格子在这个槽位里的概率, 复审): Σ_a P(挑中起量帖 a)·P(格子 | a)。

    挑起量帖的过程(pick_reference + 用不上就换下一篇): 没冷却的里面先挑形式和槽位一样的, 链是空的就排除再挑。等价于
    在「能用的」那一组里按 w 抽 —— 形式一样而能用的有就是它们, 没有就是全部能用的。"""
    fresh = _fresh_refs(anchors, ref_age=ref_age, cooldown_days=ctx.params["hit_cooldown_days"], in_batch=used_refs)
    firsts = [(a, _h_first(ctx, batch, f, a)) for a in fresh]
    ok = [(a, d) for a, d in firsts if d]
    pref = [(a, d) for a, d in ok if a.get(F) == f]
    group = pref or ok
    tot = sum(_ref_w(a) for a, _ in group)
    out: dict = {}
    for a, d in group:
        pa = _ref_w(a) / tot
        for c, pc in d.items():
            out[c] = out.get(c, 0.0) + pa * pc
    return out


def _e_title(ctx: _Ctx, ref: dict):
    t = ref.get(T) if ref.get(T) in VALUES[T] else None
    if t is not None and T in ctx.pin and ctx.pin[T] != t:
        t = None
    return t


def _e_marginal(ctx: _Ctx, batch: Batch, f: str, pool, cells, *, ref_age, used_refs, rng) -> dict:
    """e 槽里这几个格子的概率(复审): Σ_r P(挑中参考帖 r)·P(格子 | r)。P(格子 | r) 只看 r 的标题句式 t_r ——
    同一类的参考帖条件分布一样, 所以 = Σ_t P(挑中的是 t 类)·P(格子 | t)。挑的过程同 h(用不上的排除再挑), 等价于在
    「这一类在槽位里有正权重」的参考帖里按 w 抽; 没有标题句式的参考帖按 w 的分布(含 π_t)。"""
    sums: dict = {}
    s_none = _scan(ctx, batch, f, "e", rng, es=False, by_title=sums)["sw"]
    fresh = _fresh_refs(pool, ref_age=ref_age, cooldown_days=ctx.params["ext_cooldown_days"], in_batch=used_refs)
    cls: dict = {}
    for e in fresh:
        t = _e_title(ctx, e)
        if (sums.get(t, 0.0) if t is not None else s_none) > 0:
            cls[t] = cls.get(t, 0.0) + _ref_w(e)
    tot = sum(cls.values())
    out = {}
    for c in cells:
        lev, arc, fmt, ttl = c
        if tot <= 0:
            out[c] = 0.0
            continue
        bwt = (ctx.pi[L][lev] * ctx.pi[A][arc] * ctx.r[L][lev] * ctx.r[A][arc] * ctx.r[T][ttl] * ctx.d(c))
        p = 0.0
        if cls.get(ttl) and sums.get(ttl, 0.0) > 0:
            p += cls[ttl] / tot * bwt / sums[ttl]
        if cls.get(None) and s_none > 0:
            p += cls[None] / tot * bwt * ctx.pi[T][ttl] / s_none
        out[c] = p
    return out


def _pick_slot(ctx: _Ctx, batch: Batch, slot: dict, rng, *, profile, used_refs: list, k: int) -> dict:
    """一个槽位抽主牌和它的备牌。h / e 槽: 一篇参考帖用不上(它那一圈的格子全被去重挡了)就换下一篇, 都用不上才按 w 发
    —— 不为了用上某一篇参考帖去放宽 L1(复审: L1 只在 w / x 槽正权重的格子不到 3 个时放宽, 那只在重度 pin 时出现;
    pin 时整批都是 pin 槽, 没有 h / e)。h / e 的 p 是格子在槽位里的边际概率(对所有可能挑中的参考帖求和)。"""
    f, src = slot["f"], slot["src"]
    st = ctx.state
    res: dict = {"slot": slot, "src": src, "picks": [], "ref": None, "reuse": 0, "l1": None, "reference": None}
    if src == "h":
        anchors = hit_anchors(profile)
        tried: list = []
        while True:
            anchor, _p_ref, reuse = pick_reference(
                anchors, rng, ref_age=st.get("ref_age"), cooldown_days=ctx.params["hit_cooldown_days"],
                in_batch=list(used_refs) + tried, prefer=lambda e: e.get(F) == f)
            if anchor is None:
                break
            chain = _h_chain(ctx, batch, f, anchor, rng)
            if chain:
                base = _scan(ctx, batch, f, "x", rng, es=False)           # pu 的分母: 这个槽位的试新分布
                sdb = base["sdb"] or 1.0
                if reuse:                                                    # 全在冷却里, 重用最久没用的: 定死的那一篇
                    first = _h_first(ctx, batch, f, anchor)
                    probs = {c: first.get(c, 0.0) for c, _pc, _dd in chain[:k]}
                else:
                    probs = _h_marginal(ctx, batch, f, anchors, ref_age=st.get("ref_age"), used_refs=used_refs)
                res["picks"] = [(c, probs.get(c, 0.0), dd / sdb) for c, _pc, dd in chain[:k]]
                res.update(ref=anchor["ref"], reuse=reuse, reference={
                    "kind": "hit", "own": bool(anchor.get("own")), "age_days": anchor.get("age_days"),
                    "title_structure": anchor.get(T), "desc": dict(anchor.get("desc") or {})})
                return res
            tried.append(anchor["ref"])
        src = res["src"] = "w"                                               # 没有能用的起量帖: 按 w 发
    if src == "e":
        pool = ext_refs(profile)
        tried = []
        while True:
            ref, _p_ref, reuse = pick_reference(
                pool, rng, ref_age=st.get("ref_age"), cooldown_days=ctx.params["ext_cooldown_days"],
                in_batch=list(used_refs) + tried)
            if ref is None:
                break
            t_ref = _e_title(ctx, ref)
            out = _scan(ctx, batch, f, "e", rng, t_only=t_ref, k=k)
            if out["sw"] > 0:
                sdb = out["sdb"] or 1.0
                cells = [c for c, _w, _dd in out["top"]]
                if reuse:
                    probs = {c: w / out["sw"] for c, w, _dd in out["top"]}
                else:
                    probs = _e_marginal(ctx, batch, f, pool, cells, ref_age=st.get("ref_age"), used_refs=used_refs,
                                        rng=rng)
                res["picks"] = [(c, probs.get(c, 0.0), dd / sdb) for c, _w, dd in out["top"]]
                res.update(ref=ref["ref"], reuse=reuse, reference={
                    "kind": "external", "title_structure": t_ref, "posted_days_ago": ref.get("posted_days_ago"),
                    "comments_per_day": ref.get("velocity"), "desc": dict(ref.get("desc") or {})})
                return res
            tried.append(ref["ref"])
        src = res["src"] = "w"                                               # 参考帖都用不上: 按 w 发
    out = _scan_relaxing(ctx, batch, f, src, rng, k=k)
    if out["sw"] > 0:
        sdb = out["sdb"] or 1.0
        res["picks"] = [(c, w / out["sw"], dd / sdb) for c, w, dd in out["top"]]
        res["l1"] = out.get("l1")
    return res


def new_batch_id(rng) -> str:
    """批次号: 12 位十六进制, 从这次发牌的 rng 里取(同 seed 同批次号, 生产里 rng 不设 seed 就是随机的)。"""
    return f"{rng.getrandbits(48):012x}"


def _pv(cell, pi) -> list:
    lev, arc, fmt, ttl = cell
    return [round(pi[F][fmt], 3), round(pi[T][ttl], 3), round(pi[L][lev], 3), round(pi[A][arc], 3)]


def build_candidates(profile, state, plan: dict, rng, *, pin: dict | None = None, seed_cells=(),
                     batch_id: str | None = None, extra: dict | None = None,
                     params: dict = PARAMS) -> tuple[list[dict], dict]:
    """§2.5–§2.7: 按 plan 逐个槽位抽主牌(B 每抽一张更新一次), 再排备牌和尾巴。

    pin: 写手点名的维度(已经过 normalize_pin); 不给就用 plan["pin"]。seed_cells: 这一批里已经有的格子(repeat_of 重发
    的那几张), 只进 B, 不进候选。extra: 每张 _draw 都带的额外键(core 传 {"fa": flywheel_arm})。

    回 (candidates, meta)。candidates 按 RPC 的先后排好、按 angle_key 去重; meta 有每个槽位抽到什么、每个 key 的参考帖
    描述(by_key, 给渲染用 —— 描述不进台账)。同一个 seed、profile、台账给出完全一样的候选表和 _draw。"""
    pin = dict(plan.get("pin") or {}) if pin is None else dict(pin or {})
    b = batch_id or new_batch_id(rng)
    slots = list(plan.get("slots") or [])
    seeds = [tuple(c) for c in seed_cells or ()]
    n = len(slots) + len(seeds)
    n_nonx = sum(1 for s in slots if s["src"] != "x") + len(seeds)
    ctx = _Ctx(profile, state, pin=pin, params=params)
    batch = Batch(n, n_nonx, ctx.pi, pinned=pin, params=params)
    for c in seeds:
        batch.add(c, "rep")
    k = 1 + max(int(params["alternates"]), 0)
    stage = (profile or {}).get("stage") or plan.get("stage") or "none"
    ps = (profile or {}).get("ps") or "uniform"
    dd_mode = state.get("dd") or ("py" if state.get("ok") else "rpc_only")
    base = {"v": 2, "pol": "v2", "b": b}
    # 这一批的整张计划(槽位数和各来源份额)每张都带: draw-report 的「来源计划」按批算 n·份额, 一批只算一次 —— 光凭每张
    # 实际落下来的来源和它的 sp 拼不出整张计划(一张都没落下的来源就不见了, review on aw#140)
    # 记的是这一批实际排出来的各来源槽位数(外部保底那一张、外部封顶之后的), 不是份额: 大批(封顶)和小批(保底)的
    # 计划不等于 n·份额, 拿 n·份额去比会报出本来就该有的差(复审 on aw#140)。_draw 有 300 字的预算, 所以只记槽位数。
    counts = {s_: int(c) for s_, c in (plan.get("counts") or {}).items() if int(c or 0) > 0}
    if counts:
        base.update(pn=int(plan.get("n") or len(slots)), pk=counts)
    extra_keys = dict(extra or {})

    used_refs: list = []
    results = []
    for slot in slots:
        r = _pick_slot(ctx, batch, slot, rng, profile=profile, used_refs=used_refs, k=k)
        if r["picks"]:
            batch.add(r["picks"][0][0], r["src"])
            if r["ref"]:
                used_refs.append(r["ref"])
        results.append(r)

    seen: set = set()
    by_key: dict = {}
    unmapped = (state.get("unmapped_keys") or {}) if (state.get("ok") and params["l1"]) else {}
    pp = (profile or {}).get("pp")

    def make(cell, *, i, src, sp, p, pu, g=0, ref=None, reuse=0, l1=None, reference=None) -> dict | None:
        dims = cell_dims(cell)
        key = angle_key(dims)
        if key in seen or key in unmapped:
            return None
        seen.add(key)
        d = dict(base)
        d.update(i=i, src=src, sp=sp, p=_sig3(p), pu=_sig3(pu), pv=_pv(cell, ctx.pi), st=stage, ps=ps, dd=dd_mode)
        if pp and pp != "live":
            d["pp"] = pp                                    # 项目块是旧的 / 只有对照表 / 没有(复审: 分析时分得出来)
        if g:
            d["g"] = 1
        if ref:
            d["ref"] = ref
        if reuse:
            d["reuse"] = 1
        if pin:
            d["pin"] = [x for x in DIMS4 if x in pin]
        if l1:
            d["l1"] = l1
        d.update(extra_keys)
        dims["_draw"] = d
        by_key[key] = {"src": src, "i": i, "ref": ref, "reference": reference}
        return {"angle_key": key, "dims": dims}

    prim_cands: list[dict] = []
    primaries = []
    final = Batch(n, n_nonx, ctx.pi, pinned=pin, params=params)      # 整批定下来之后的 B: 重发的几张 + 全部主牌
    for c in seeds:
        final.add(c, "rep")
    prim_refs: set = set()
    for r in results:
        if not r["picks"]:
            continue
        s = r["slot"]
        sp = s["sp"] if r["src"] == s["src"] else (plan.get("shares") or {}).get("w", 0.0)
        c, p, pu = r["picks"][0]
        cand = make(c, i=s["i"], src=r["src"], sp=sp, p=p, pu=pu, g=s.get("g", 0) if r["src"] == "e" else 0,
                    ref=r["ref"], reuse=r["reuse"], l1=r["l1"], reference=r["reference"])
        if cand:
            prim_cands.append(cand)
            final.add(c, r["src"])
            if r["ref"]:
                prim_refs.add(r["ref"])
            primaries.append({"i": s["i"], "src": r["src"], "planned": s["src"], "angle_key": cand["angle_key"],
                              "dims": cell_dims(c), "ref": r["ref"], "reuse": r["reuse"], "l1": r["l1"],
                              "reference": r["reference"]})

    # ── 备牌和尾巴(复审): RPC 按表的先后取前 n 个能用的 —— 跳过一张主牌(并发发牌刚抢走; 台账读不到时撞上写过的 key),
    # 顶上来的就是接下来的备牌 / 尾巴, 它不一定是那个槽位自己的备牌。所以每一张备牌都要和【整批】比:
    #   · 过不了整批的 B(形状、(杠杆, 原型)、同一个格子、杠杆 / 原型封顶, w / h 还有标题封顶)的不进表;
    #   · 带着某张主牌的参考帖的不进表(L5: 一篇参考帖一批只出一张 —— e / h 槽自己的备牌因此都不进表);
    #   · 和前面的备牌撞形状 / (杠杆, 原型) / 参考帖的排到后面一档。
    # RPC 顶上来的几张都在第一档里时(第一档有上百张, 一次发牌至多 30 张), 不管跳过了几张, 一批之内形状和 (杠杆, 原型)
    # 都不重复; 只跳一张时封顶也守得住。
    tier1: list[dict] = []
    tier2: list[dict] = []
    b_sig: set = set()
    b_pair: set = set()
    b_ref: set = set()

    def push_backup(cell, *, src, ref=None, **kw) -> bool:
        if ref and ref in prim_refs:
            return False
        if b_factor(cell, final, src) <= 0:
            return False
        lev, arc, fmt, ttl = cell
        sig, pair = (fmt, ttl, arc), (lev, arc)
        clash = ((final.sigma_ban and sig in b_sig) or (final.pair_ban and pair in b_pair)
                 or (ref is not None and ref in b_ref))
        cand = make(cell, i=0, src=src, ref=ref, **kw)
        if cand is None:
            return False
        if clash:
            tier2.append(cand)
        else:
            tier1.append(cand)
            b_sig.add(sig)
            b_pair.add(pair)
            if ref:
                b_ref.add(ref)
        return True

    for j in range(1, k):                                   # 备牌: 每个槽位第 j 名, 轮流排
        for r in results:
            if len(r["picks"]) > j:
                s = r["slot"]
                sp = s["sp"] if r["src"] == s["src"] else (plan.get("shares") or {}).get("w", 0.0)
                c, p, pu = r["picks"][j]
                push_backup(c, src=r["src"], sp=sp, p=p, pu=pu, ref=r["ref"], reuse=r["reuse"], l1=r["l1"],
                            reference=r["reference"])

    n_req = max(int(plan.get("n") or len(slots)), 1)
    want = min(max(params["tail_mult"] * n_req, params["tail_min"]), params["tail_cap"])
    have = len(prim_cands) + len(tier1) + len(tier2)
    if slots and have < want:
        _tail(ctx, rng, want - have, push_backup, pin=pin,
              sp=(plan.get("shares") or {}).get("pin" if pin else "w", 1.0))
    cands = prim_cands + tier1 + tier2

    meta = {"batch": b, "dd": dd_mode, "stage": stage, "ps": ps, "plan": {k_: plan.get(k_) for k_ in (
        "n", "stage", "ext_ok", "hit_ok", "guaranteed_ext", "shares", "counts", "formats")},
        "primaries": primaries, "short": sum(1 for r in results if not r["picks"]),
        "by_key": by_key, "n_candidates": len(cands), "backups": [len(tier1), len(tier2)]}
    return cands, meta


def _tail(ctx: _Ctx, rng, need: int, push, *, pin, sp) -> None:
    """尾巴(§2.7 第 3 步): w 分布在全部配额形式上的接续 —— 权重 π_f·π_lev·π_arc·π_ttl·D·R, ES 排序; push 跳过已在表里的、
    过不了整批 B 的(见 build_candidates 的备牌说明)。只给 RPC 在主牌 / 备牌都被并发发牌抢走时兜底, 很少被预留到。"""
    pi = ctx.pi
    fmts = ctx.vals[F] if F in ctx.pin else tuple(v for v in VALUES[F] if pi[F][v] > 0)
    rnd, log = rng.random, math.log
    items = []
    sw = sdb = 0.0
    for fmt in fmts:
        pf = pi[F][fmt]
        for lev, row in ctx.table(fmt):
            for arc, cells in row:
                for ttl, dd, bw, _bwt in cells:
                    w = pf * bw
                    if w <= 0:
                        continue
                    sw += w
                    sdb += pf * dd
                    items.append((log(rnd() or _TINY) / w, (lev, arc, fmt, ttl), w, pf * dd))
    items.sort(key=lambda x: -x[0])
    src = "pin" if pin else "w"
    added = 0
    for _key, cell, w, db in items:
        if added >= need:
            break
        if push(cell, src=src, sp=sp, p=w / sw, pu=db / sdb):
            added += 1


def slot_distribution(profile, state, slot: dict, *, batch_cells=(), n: int | None = None, n_nonx: int | None = None,
                      pin: dict | None = None, params: dict = PARAMS) -> dict:
    """一个 w / x / e(给 t_only)/ pin 槽位的完整分布(测试和 draw-report 用): {格子: (p, pu)}。和发牌走同一个 _scan。"""
    ctx = _Ctx(profile, state, pin=pin, params=params)
    n = n if n is not None else max(len(batch_cells) + 1, 1)
    n_nonx = n_nonx if n_nonx is not None else n
    batch = Batch(n, n_nonx, ctx.pi, pinned=pin or {}, params=params)
    for c in batch_cells:
        batch.add(tuple(c), "w")
    coll: dict = {}
    res = _scan(ctx, batch, slot["f"], slot["src"], None, t_only=slot.get("t_only"), es=False, collect=coll)
    sw, sdb = res["sw"] or 1.0, res["sdb"] or 1.0
    return {c: (w / sw, dd / sdb) for c, (w, dd) in coll.items()}


# ══════════════════════════════════════════════════════════════════════
# 写手点名(§2.9): pin / repeat_of
# ══════════════════════════════════════════════════════════════════════

def _pin_error(dim, value, profile, *, why: str) -> dict:
    pi_f = targets(profile)[F] if profile else {}
    shares = {v: round(s, 2) for v, s in sorted(pi_f.items(), key=lambda kv: -kv[1]) if s > 0}
    allowed = {DIM_LABEL[d]: list(VALUES[d]) for d in DIMS4} if dim is None else {DIM_LABEL[dim]: list(VALUES[dim])}
    return {"error": "pin_invalid", "message": why, "dim": dim, "value": value, "allowed": allowed,
            "format_shares": shares}


def normalize_pin(pin, profile=None) -> tuple[dict | None, dict | None]:
    """校验写手点名固定的维度: 键是四个维度名(或 内容形式 / 标题句式 / 情绪杠杆 / 人性原型), 值必须在词表里(问句 → 疑问句)。
    回 (pin, None) 或 (None, 错误)。错误里列出可选值和项目现在的内容形式占比 —— 调用方原样回给模型、什么都不写。"""
    if not pin:
        return None, None
    if not isinstance(pin, dict):
        return None, _pin_error(None, pin, profile, why="pin 要写成 {维度: 值}, 比如 {\"标题句式\": \"疑问句\"}。")
    out: dict = {}
    for k, v in pin.items():
        dim = k if k in DIMS4 else PIN_DIM_ALIASES.get(str(k).strip())
        if dim is None:
            return None, _pin_error(None, k, profile, why=f"pin 里的「{k}」不是可以固定的维度。")
        val = str(v or "").strip()
        if dim == T:
            val = vocab.TITLE_ALIASES.get(val, val)
        if val not in VALUES[dim]:
            return None, _pin_error(dim, v, profile, why=f"「{v}」不是{DIM_LABEL[dim]}的可选值。")
        out[dim] = val
    return out, None


def repeat_candidates(items, *, batch_id: str, profile=None, extra: dict | None = None) -> list[dict]:
    """repeat_of 重发的那几张(§2.9): items = [(angle_key, 四个维度)](store.ledger_dims_for 查到的)。每张原样四个维度 +
    _draw={src:"rep", of:key}, 由调用方走非原子的 store.record_draw 写进台账 —— 故意绕过 L0 / L1。
    四个维度算出来的 key 和给的 key 对不上的不发(台账那一行坏了)。"""
    stage = (profile or {}).get("stage") or "none"
    ps = (profile or {}).get("ps") or "uniform"
    out = []
    for i, (key, dims) in enumerate(items or (), start=1):
        cell = cell_of(dims)
        if cell is None:
            continue
        d4 = cell_dims(cell)
        if angle_key(d4) != key:
            continue
        d = {"v": 2, "pol": "v2", "b": batch_id, "i": i, "src": "rep", "of": key, "st": stage, "ps": ps}
        d.update(extra or {})
        d4["_draw"] = d
        out.append({"angle_key": key, "dims": d4})
    return out


# ══════════════════════════════════════════════════════════════════════
# 贴合度(§5.2 的剂量-反应)
# ══════════════════════════════════════════════════════════════════════

def fit_score(x, profile=None) -> float | None:
    """一张牌有多像这个项目平时发的: 四个维度 ln(|V_d|·π_d(v)) 的平均。0 = 和均匀乱抽一样, 正的 = 比乱抽更贴项目。

    x 是 _draw 里的 pv([π_fmt, π_ttl, π_lev, π_arc], 台账上就能算, 不用再读 profile), 或者四个维度的 dims(这时要给
    profile)。π 为 0(配额外的形式、pin 出来的)按 0.001 算。"""
    sizes = (len(VALUES[F]), len(VALUES[T]), len(VALUES[L]), len(VALUES[A]))
    if isinstance(x, dict):
        cell = cell_of(x)
        if cell is None or profile is None:
            return None
        pv = _pv(cell, targets(profile))
    else:
        try:
            pv = [float(v) for v in x]
        except (TypeError, ValueError):
            return None
        if len(pv) != 4:
            return None
    return sum(math.log(max(p, 1e-3) * s) for p, s in zip(pv, sizes)) / 4.0
