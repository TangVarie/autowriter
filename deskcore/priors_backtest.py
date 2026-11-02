"""deskcore/priors_backtest.py — 发牌先验的回测与上线闸(angle cards v2 §5.6)。纯函数, 不连库。

做法: 每个目标项目只拿它【开张头 10 天】(北京时间, 从它第一篇算起)的干净标签当本项目数据, 别的项目用全部数据当
全局 / 品牌层, 去预测它【第 14 天起】哪些帖起量; 每个项目算一个 AUC(并列记 0.5), 取目标项目的平均。目标项目 = 第
14 天起既有起量也有没起量的项目(现在是 13 个)。品牌层只用比目标项目【更早结束】的同品牌其他期(用后一期去预测前一期
是偷看答案)。

两个闸:
  1. 参照模型 —— 本项目向全局收缩(+60 篇 × r_P, 全局 +5), 内容形式 + 情绪杠杆 + 标题问句三项提升相乘, 期望不分层、
     不截断 —— 要落在 0.62 ± 0.02。落不进说明数据口径和当初那次分析对不上, 先查口径再谈上线。
  2. 上线用的打分(PARAMS 原样: 期望按形式分层, 杠杆 全局 → 品牌 → 本项目、截 [0.5, 1.6]、^β; 标题 品牌 → 本项目、
     截 [0.6, 1.6]; 两者相乘)要 ≥ 0.60。不到就 β = 0.5 再跑; 还低于 0.58 就 β = 0(只按配比发)。
另外打印 rank 信号(评论数中位秩代替 0/1)的同一个打分, 只有它比 binary 高出 0.02 才值得换。

输入是「格」: ``{项目: [{bucket: train|gap|test, content_format, emotional_lever, q: Q|NQ|None, y, n, r}]}``,
n 是这一格的篇数、r 是中位秩之和。线上从 ①② 读出来的笔记用 ``cells_from_notes`` 转; 离线用导出的聚合
(scripts/sql/angle_priors_export.sql 第一段, scripts/build_angle_priors.parse_cells)。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from . import priors

TRAIN_DAYS = 10
TEST_FROM_DAY = 14
GATE1_TARGET, GATE1_TOL = 0.62, 0.02
GATE2_MIN, GATE2_FALLBACK_MIN = 0.60, 0.58


def _bj_date(t: datetime):
    return (t + timedelta(hours=8)).date()


def cells_from_notes(notes, title_answers, *, now: datetime | None = None,
                     params: dict = priors.PARAMS) -> tuple[dict, dict]:
    """①(全库笔记) + ②(title_is_question 答案) → (格, 每个项目的 (第一篇, 最后一篇) 日期)。一篇一格。"""
    now = now or priors._utcnow()
    answers = title_answers or {}
    by_p: dict[str, list[dict]] = {}
    for r in notes or ():
        if r.get("project_id") and priors._ts(r.get("publish_time")) is not None:
            by_p.setdefault(str(r["project_id"]), []).append(r)
    cells: dict[str, list[dict]] = {}
    spans: dict[str, tuple[str, str]] = {}
    for pid, rows in by_p.items():
        times = [priors._ts(r["publish_time"]) for r in rows]
        spans[pid] = (min(times).date().isoformat(), max(times).date().isoformat())
        d0 = min(_bj_date(t) for t in times)
        has_cc = any(r.get("comments_count") is not None for r in rows)
        elig = []
        for r, t in zip(rows, times):
            y = priors.label(r, has_cc=has_cc, now=now, params=params)
            if y is not None:
                elig.append((r, t, y))
        ranks: dict[int, float] = {}
        if has_cc:
            groups: dict[str, list[int]] = {}
            for i, (r, _t, _y) in enumerate(elig):
                groups.setdefault(priors._valid("content_format", r.get("content_format")) or "", []).append(i)
            for idxs in groups.values():
                vals = [float(elig[i][0].get("comments_count") or 0) for i in idxs]
                for i, rk in zip(idxs, priors._mid_ranks(vals)):
                    ranks[i] = rk
        out = []
        for i, (r, t, y) in enumerate(elig):
            day = (_bj_date(t) - d0).days
            out.append({"bucket": "train" if day < TRAIN_DAYS else "test" if day >= TEST_FROM_DAY else "gap",
                        "content_format": r.get("content_format"), "emotional_lever": r.get("emotional_lever"),
                        "q": priors._qclass(answers.get(str(r.get("note_id")))), "y": y, "n": 1,
                        "r": ranks.get(i, 0.0)})
        cells[pid] = out
    return cells, spans


def _label(cells) -> dict | None:
    lbl = None
    for c in cells:
        lbl = lbl or priors._new_label()
        priors.label_add(lbl, fmt=c.get("content_format"), lever=c.get("emotional_lever"), qclass=c.get("q"),
                         y=int(c["y"]), rank=float(c.get("r") or 0.0), n=int(c.get("n") or 1))
        if float(c.get("r") or 0.0) > 0:
            lbl["rk"] = True
    return lbl


def auc(scored) -> float | None:
    """[(分数, y, 篇数)] → AUC, 并列记 0.5; 没有正例或没有负例回 None。"""
    pos = [(s, n) for s, y, n in scored if y == 1 and n > 0]
    neg = [(s, n) for s, y, n in scored if y == 0 and n > 0]
    P, N = sum(n for _, n in pos), sum(n for _, n in neg)
    if not P or not N:
        return None
    acc = 0.0
    for s1, n1 in pos:
        for s0, n0 in neg:
            acc += n1 * n0 * (1.0 if s1 > s0 else 0.5 if s1 == s0 else 0.0)
    return acc / (P * N)


def targets(cells: dict) -> list[str]:
    out = []
    for pid, cs in sorted(cells.items()):
        if pid in priors.PARAMS["label_exclude_projects"]:
            continue
        test = [c for c in cs if c["bucket"] == "test"]
        if any(c["y"] == 1 for c in test) and any(c["y"] == 0 for c in test):
            out.append(pid)
    return out


def _earlier_brand(pid: str, spans: dict | None, universe) -> list[str]:
    if not spans or pid not in spans:
        return []
    start = spans[pid][0]
    b = priors.brand_of(pid)
    return sorted(p for p in universe if p != pid and priors.brand_of(p) == b and p in spans and spans[p][1] < start)


def _unstrat(lbl: dict | None, dim: str) -> tuple[float, dict, dict]:
    """参照模型用的 (r_P, O, E), E = N·r_P(不分层)。dim: content_format / emotional_lever / q。"""
    if not lbl:
        return 0.0, {}, {}
    rp = (lbl["y"] + 1.0) / (lbl["n"] + 20.0)
    O: dict = {}
    N: dict = {}
    if dim == "content_format":
        for f, c in lbl["fmt"].items():
            if f:
                O[f] = O.get(f, 0.0) + c[1]
                N[f] = N.get(f, 0.0) + c[0]
    else:
        for _f, cells in lbl["lev" if dim == "emotional_lever" else "ttl"].items():
            for v, c in cells.items():
                O[v] = O.get(v, 0.0) + c[1]
                N[v] = N.get(v, 0.0) + c[0]
    return rp, O, {v: n * rp for v, n in N.items()}


def _reference_scores(T, train_lbl, full: dict, test_cells, *, k: float = 60.0, kg: float = 5.0):
    b = priors.brand_of(T)
    others = [p for p in full if priors.brand_of(p) != b]
    lifts = {}
    for dim in ("content_format", "emotional_lever", "q"):
        GO: dict = {}
        GE: dict = {}
        for p in others:
            _r, o, e = _unstrat(full[p], dim)
            for v, x in o.items():
                GO[v] = GO.get(v, 0.0) + x
            for v, x in e.items():
                GE[v] = GE.get(v, 0.0) + x
        rT, O, E = _unstrat(train_lbl, dim)
        kp = k * rT
        lifts[dim] = {v: (O.get(v, 0.0) + kp * (GO.get(v, 0.0) + kg) / (GE.get(v, 0.0) + kg)) / (E.get(v, 0.0) + kp)
                      for v in set(GO) | set(GE) | set(E) if (E.get(v, 0.0) + kp) > 0}
    out = []
    for c in test_cells:
        s = 1.0
        for dim, key in (("content_format", "content_format"), ("emotional_lever", "emotional_lever"), ("q", "q")):
            v = c.get(key)
            if v:
                s *= lifts[dim].get(v, 1.0)
        out.append((s, int(c["y"]), int(c.get("n") or 1)))
    return out


def _shipped_scores(T, train_lbl, full: dict, test_cells, *, brand_ids, params, beta, signal):
    gt = {"schema": priors.SNAPSHOT_SCHEMA,
          "projects": {p: {"label": lbl} for p, lbl in full.items() if lbl and p != T}}
    if train_lbl:
        gt["projects"][T] = {"label": train_lbl}
    ll = priors.lever_lift(gt, T, brand_ids=brand_ids, params=params, signal=signal)["lift"]
    tl = priors.title_lift(gt, T, brand_ids=brand_ids, params=params, signal=signal)["lift"]
    out = []
    for c in test_cells:
        lev = priors._valid("emotional_lever", c.get("emotional_lever"))
        s = (ll.get(lev, 1.0) ** beta if lev else 1.0) * (tl.get(c.get("q"), 1.0) if c.get("q") else 1.0)
        out.append((s, int(c["y"]), int(c.get("n") or 1)))
    return out


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def recommend_beta(auc_beta1: float | None, auc_beta05: float | None) -> float:
    """§5.6 闸 2 的退路: β=1 的打分到 0.60 就留 1; 不到就看 β=0.5, 到 0.58 用 0.5; 还不到就 β=0(只按配比发)。"""
    if auc_beta1 is not None and auc_beta1 >= GATE2_MIN:
        return 1.0
    if auc_beta05 is not None and auc_beta05 >= GATE2_FALLBACK_MIN:
        return 0.5
    return 0.0


def _gate2_min(beta: float) -> float:
    return GATE2_MIN if beta >= 1.0 else GATE2_FALLBACK_MIN if beta > 0 else 0.0


def backtest(cells: dict, spans: dict | None = None, *, params: dict = priors.PARAMS) -> dict:
    """§5.6 的回测。回 ``{"targets", "models": {名字: {"mean_auc", "by_project"}}, "gates", "recommend"}``。"""
    tg = targets(cells)
    full = {p: _label(cs) for p, cs in cells.items() if p not in params["label_exclude_projects"]}
    full = {p: lbl for p, lbl in full.items() if lbl}
    models: dict[str, dict] = {}

    def run(name, fn):
        by = {}
        for T in tg:
            train = [c for c in cells[T] if c["bucket"] == "train"]
            test = [c for c in cells[T] if c["bucket"] == "test"]
            by[T] = auc(fn(T, _label(train), test))
        m = _mean(by.values())
        models[name] = {"mean_auc": None if m is None else round(m, 4),
                        "by_project": {k: (None if v is None else round(v, 4)) for k, v in by.items()}}

    run("reference", lambda T, tr, te: _reference_scores(T, tr, full, te))
    base_beta = float(params["beta"])
    for beta in sorted({base_beta, 1.0, 0.5, 0.0}, reverse=True):
        run(f"shipped_beta_{beta:g}", lambda T, tr, te, b=beta: _shipped_scores(
            T, tr, full, te, brand_ids=_earlier_brand(T, spans, full), params=params, beta=b, signal="binary"))
    run(f"shipped_rank_beta_{base_beta:g}", lambda T, tr, te: _shipped_scores(
        T, tr, full, te, brand_ids=_earlier_brand(T, spans, full), params=params, beta=base_beta, signal="rank"))

    ref = models["reference"]["mean_auc"]
    g1 = ref is not None and abs(ref - GATE1_TARGET) <= GATE1_TOL
    shipped = models[f"shipped_beta_{base_beta:g}"]["mean_auc"]
    rec = recommend_beta(models["shipped_beta_1"]["mean_auc"], models["shipped_beta_0.5"]["mean_auc"])
    rank = models[f"shipped_rank_beta_{base_beta:g}"]["mean_auc"]
    return {
        "targets": tg,
        "n_targets": len(tg),
        "models": models,
        "gates": {
            "gate1_reference": {"value": ref, "target": [GATE1_TARGET - GATE1_TOL, GATE1_TARGET + GATE1_TOL],
                                "pass": bool(g1)},
            "gate2_shipped": {"value": shipped, "beta": base_beta, "min": _gate2_min(base_beta),
                              "pass": shipped is not None and shipped >= _gate2_min(base_beta)},
        },
        "recommend": {"beta": rec,
                      "lift_signal": "rank" if (rank is not None and shipped is not None and rank >= shipped + 0.02)
                      else "binary"},
    }


def format_report(res: dict) -> str:
    lines = [f"targets ({res['n_targets']}): " + ", ".join(res["targets"])]
    names = list(res["models"])
    lines.append(f"{'project':<18}" + "".join(f"{n[:22]:>24}" for n in names))
    for T in res["targets"]:
        row = f"{T:<18}"
        for n in names:
            v = res["models"][n]["by_project"].get(T)
            row += f"{'-' if v is None else f'{v:.3f}':>24}"
        lines.append(row)
    lines.append(f"{'MEAN':<18}" + "".join(
        f"{'-' if res['models'][n]['mean_auc'] is None else format(res['models'][n]['mean_auc'], '.4f'):>24}"
        for n in names))
    g1, g2 = res["gates"]["gate1_reference"], res["gates"]["gate2_shipped"]
    lines.append(f"gate 1 (reference {g1['target'][0]:.2f}–{g1['target'][1]:.2f}): {g1['value']} → "
                 f"{'PASS' if g1['pass'] else 'FAIL'}")
    lines.append(f"gate 2 (shipped β={g2['beta']:g} ≥ {g2['min']:.2f}): {g2['value']} → "
                 f"{'PASS' if g2['pass'] else 'FAIL'}")
    lines.append(f"recommend: β = {res['recommend']['beta']:g}, lift signal = {res['recommend']['lift_signal']}")
    return "\n".join(lines)


__all__ = ["cells_from_notes", "backtest", "auc", "targets", "format_report"]
