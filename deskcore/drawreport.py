"""deskcore/drawreport.py — 发牌 v2 的读数(angle cards v2 §5.2 / §5.3, ``python -m deskcore.cli draw-report``)。

纯函数: 台账行(dims._draw 是每张牌的记账块)、写作台版本对上 TV 笔记的情况(store.desk_adoption)、查重底稿的结论、
发出去那几篇的评论数 → 一份报表。只有计数和比率, 没有标题和正文。

每张牌的结果(能读到的那天才算进分母, 没到日子的不算 0):
  M1 写了    抽出后 3 天内销账(score_drafts 或底稿转正)                         D+3
  M2 发了    销账的那一版在 tv_note_links 按内容对上了 TV 的笔记                   D+8
  M3 留着    M2, 而且标题没改、发出去的字数 ≥ 写作台那版的 0.85(tv_edit_diffs)       D+8
             (差异没算出来、标题改没改或两边字数缺一样的读不到, 不进分母)
  D1 撞车    带这张牌的 key 查过重的稿子里, 有 reject / warn 的比例                  D+1
             (查重只归它之前最近的那一次发牌: 同一个 key 重发之后的查重算重发那张的)
  M5 评论    发出去三天以上、评论 ≥ 20 的比例(样本小, 只给计数)                      D+10
  M6 爆      tier 爆 / 大爆 的篇数                                                  按月

比较(§5.2): w 对 x(盲、批内随机)是主比较, Mantel–Haenszel 按项目分层的风险差和风险比; e / h 对 w 是次要比较(参考行
本身就是处理的一部分, 不盲)。配额内均匀乱抽(ipw_uniform): 只用试新牌估它会写出 / 发出多少 —— 试新牌就是这个策略的
直接样本; w 牌不并进来(w 抽不到的格子试新抽得到, 并进来会偏, 见 ipw_uniform)。x-only 杠杆提升: 只用试新牌(不受策略
本身的偏向影响)重估各杠杆的 M1 / M2。pin / rep(写手点名的)不进比较, 单独计数(D3)。

健康(§5.3 末): 配额外比例、各维度实际 vs 目标、30 天里不同 key ÷ 牌数、14 天里最大杠杆份额、来源实际 vs 计划、
fb 比例、写手点名比例。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from . import drawpolicy as dp
from . import priors

_CN_TZ = timezone(timedelta(hours=8))
M1_DAYS, M2_DAYS, M5_DAYS = 3, 8, 3
KEEP_RATIO = 0.85
HIT_TIERS = priors.HIT_TIERS
COMPARE_SOURCES = ("w", "x", "e", "h")
WRITER_SOURCES = ("pin", "rep")
LIFT_PSEUDO = 2.0


def _ts(v) -> datetime | None:
    return priors._ts(v)


def _week(t: datetime) -> str:
    y, w, _d = t.astimezone(_CN_TZ).isocalendar()
    return f"{y}-W{w:02d}"


def card_rows(ledger_rows, *, adoption: dict | None = None, snapshots=None, notes: dict | None = None,
              now: datetime | None = None) -> list[dict]:
    """台账行 → 每张牌一行: {project, key, src, week, drawn_at, fit, sp, p, pu, lever, fmt, pv, m1, m2, m3, d1, m5, m6}。
    结果是 None 表示「还读不到 / 没有数据」, 不是 0。"""
    now = now or datetime.now(timezone.utc)
    by_version = ((adoption or {}).get("by_version") or {}) if adoption is not None else None
    snaps: dict[tuple[str, str], list[tuple[datetime, str]]] = defaultdict(list)
    for s in snapshots or ():
        t = _ts(s.get("checked_at"))
        if s.get("angle_key") and t is not None:
            snaps[(str(s.get("project_id")), str(s["angle_key"]))].append((t, str(s.get("status") or "")))
    # 同一个 key 在同一个项目里每次发出去的时刻(repeat_of 重发、过了避重窗口又抽到): 一次查重只归它之前最近的那一次发牌,
    # 不归更早的几次(review on aw#140)
    issued: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    for r in ledger_rows or ():
        t = _ts(r.get("drawn_at"))
        if t is not None and r.get("angle_key"):
            issued[(str(r.get("project_id")), str(r["angle_key"]))].append(t)
    out = []
    for r in ledger_rows or ():
        dims = r.get("dims") if isinstance(r.get("dims"), dict) else {}
        d = dims.get("_draw") if isinstance(dims.get("_draw"), dict) else {}
        drawn = _ts(r.get("drawn_at"))
        if drawn is None:
            continue
        age = (now - drawn).total_seconds() / 86400.0
        consumed = _ts(r.get("consumed_at")) if r.get("consumed_version_id") else None
        vid = str(r.get("consumed_version_id") or "")
        m1 = None
        if age >= M1_DAYS:
            m1 = int(consumed is not None and (consumed - drawn).total_seconds() <= M1_DAYS * 86400)
        m2 = m3 = m5 = m6 = None
        if age >= M2_DAYS and by_version is not None:
            link = by_version.get(vid) if vid else None
            m2 = int(link is not None)
            if link is None:
                m3 = 0
            else:
                # 改稿差异还没算出来 / 读不到(title_changed None)、字数缺一样: 读不到, 记 None(不进分母), 不记 0、
                # 也不当字数够了(review on aw#140)。正文为空的稿(draft_chars 0)发出去多少字都算够。
                tc, dc, pc = link.get("title_changed"), link.get("draft_chars"), link.get("published_chars")
                if tc is not None and dc is not None and pc is not None:
                    m3 = int(tc is False and float(pc) >= KEEP_RATIO * float(dc))
                note = (notes or {}).get(str(link.get("note_id"))) if notes is not None else None
                if note is not None:
                    pt = _ts(note.get("publish_time"))
                    if pt is not None and (now - pt).total_seconds() >= M5_DAYS * 86400 \
                            and note.get("comments_count") is not None:
                        m5 = int(int(note["comments_count"]) >= priors.PARAMS["label_min_comments"])
                    m6 = int(str(note.get("tier") or "") in HIT_TIERS)
        pk = (str(r.get("project_id")), str(r.get("angle_key")))
        nxt = min((t for t in issued.get(pk, ()) if t > drawn), default=None)
        verdicts = [st for t, st in snaps.get(pk, ()) if t >= drawn and (nxt is None or t < nxt)]
        d1 = int(any(v in ("reject", "warn") for v in verdicts)) if verdicts else None
        pv = d.get("pv")
        out.append({
            "project": str(r.get("project_id") or ""), "key": str(r.get("angle_key") or ""),
            "src": str(d.get("src") or ("legacy" if not d else "?")), "pol": d.get("pol"),
            "week": _week(drawn), "drawn_at": drawn, "age": age,
            "sp": d.get("sp"), "p": d.get("p"), "pu": d.get("pu"), "pv": pv,
            "fit": dp.fit_score(pv) if isinstance(pv, list) else None,
            "lever": dims.get(dp.L), "fmt": dims.get(dp.F), "arc": dims.get(dp.A), "ttl": dims.get(dp.T),
            "dd": d.get("dd"), "ps": d.get("ps"), "b": d.get("b"), "pn": d.get("pn"), "pl": d.get("pl"), "pk": d.get("pk"),
            "m1": m1, "m2": m2, "m3": m3, "d1": d1, "m5": m5, "m6": m6,
        })
    return out


def _rate(cards, m: str) -> dict:
    ys = [c[m] for c in cards if c.get(m) is not None]
    return {"n": len(ys), "y": sum(ys), "rate": round(sum(ys) / len(ys), 3) if ys else None}


METRICS = ("m1", "m2", "m3", "d1", "m5", "m6")


def table(cards) -> list[dict]:
    """来源 × 项目 × 周: 张数和每个指标的 n / y / rate。"""
    groups: dict[tuple, list] = defaultdict(list)
    for c in cards:
        groups[(c["src"], c["project"], c["week"])].append(c)
    rows = []
    for (src, proj, week), cs in sorted(groups.items()):
        row = {"src": src, "project": proj, "week": week, "cards": len(cs)}
        for m in METRICS:
            row[m] = _rate(cs, m)
        rows.append(row)
    return rows


def mantel_haenszel(cards, treat: str, ctrl: str, m: str) -> dict:
    """treat 对 ctrl 在指标 m 上的 Mantel–Haenszel 风险差 / 风险比(按项目分层; 只用两边都有、能读到 m 的层)。"""
    strata: dict[str, list[list[int]]] = defaultdict(lambda: [[0, 0], [0, 0]])
    for c in cards:
        if c.get(m) is None or c["src"] not in (treat, ctrl):
            continue
        cell = strata[c["project"]][0 if c["src"] == treat else 1]
        cell[0] += c[m]
        cell[1] += 1
    num_rd = den_rd = num_rr = den_rr = 0.0
    n_t = n_c = y_t = y_c = 0
    used = 0
    for (a, n1), (b, n0) in strata.values():
        if not n1 or not n0:
            continue
        used += 1
        N = n1 + n0
        num_rd += (a * n0 - b * n1) / N
        den_rd += n1 * n0 / N
        num_rr += a * n0 / N
        den_rr += b * n1 / N
        n_t, n_c, y_t, y_c = n_t + n1, n_c + n0, y_t + a, y_c + b
    return {"metric": m, "treat": treat, "ctrl": ctrl, "strata": used, "n_treat": n_t, "n_ctrl": n_c,
            "y_treat": y_t, "y_ctrl": y_c,
            "rd_mh": round(num_rd / den_rd, 4) if den_rd else None,
            "rr_mh": round(num_rr / den_rr, 4) if den_rr else None}


def ipw_uniform(cards, m: str) -> dict:
    """「配额内均匀乱抽」的指标 m: 每个槽位的内容形式照项目配额, (杠杆, 原型, 标题) 在这个槽位过得了跨批次去重和一批之内
    约束的格子里按 D·B 抽 —— 也就是试新牌(x)自己的抽法。只用试新牌估, 估计 = 能读到 m 的试新牌里 m 的比例; 它们就是这个
    策略的直接样本(x 牌的 p 和 pu 是同一个分布, 要加权的话每张都是 1)。

    名字沿用原来的 IPW。原来把 w 牌按 pu / p 加权和 x 牌并在一起估, 有偏(review round 2 on aw#140): 有的格子 w 抽不到
    (p = 0, 比如这一批某个标题句式到了封顶)、试新分布却抽得到(pu > 0, 试新不设标题封顶), 这些格子只有 x 牌代表, 并进来的
    w 牌只把它们摊薄 —— 一个占均匀分布 11.4% 的标题句式会被估成 2.5% 上下。所以 w 牌一张都不进这个估计。

    回 {metric, estimate, explore_cards(进估计的试新牌张数), observed(w + x 实际的比例, 对照用), observed_cards}。
    试新牌只占两成, 张数少的时候估计很粗, 看 explore_cards。"""
    xs = [c[m] for c in cards if c["src"] == "x" and c.get(m) is not None]
    seen = _rate([c for c in cards if c["src"] in ("w", "x")], m)
    return {"metric": m, "estimate": round(sum(xs) / len(xs), 4) if xs else None, "explore_cards": len(xs),
            "observed": seen["rate"], "observed_cards": seen["n"]}


def x_only_lever_lift(cards, m: str) -> dict:
    """只用试新牌重估各杠杆在 m 上的提升: ((y_l + k·r̄) / (n_l + k)) / r̄, k = 2(决策规则 5 对它和策略的提升)。"""
    xs = [c for c in cards if c["src"] == "x" and c.get(m) is not None and c.get("lever")]
    if not xs:
        return {"metric": m, "cards": 0, "lift": {}}
    rbar = sum(c[m] for c in xs) / len(xs)
    per: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for c in xs:
        per[c["lever"]][0] += c[m]
        per[c["lever"]][1] += 1
    lift = {}
    for lev, (y, n) in sorted(per.items()):
        lift[lev] = {"n": n, "y": y,
                     "lift": round(((y + LIFT_PSEUDO * rbar) / (n + LIFT_PSEUDO)) / rbar, 3) if rbar > 0 else None}
    return {"metric": m, "cards": len(xs), "base_rate": round(rbar, 4), "lift": lift}


def _planned_sources(cards) -> dict:
    """来源计划: 每一批按它记下的整张计划加一次(review on aw#140): _draw 的 pk 是这一批实际排出来的各来源槽位数
    (外部保底 / 封顶之后的); 只有 pn(槽位数)+ pl(份额)的就按 n·份额。按每张实际落下的来源加 sp 会把计划缩成
    「实际 × 份额」, 一张都没落下的来源整个不见。没记计划的批(重写的几张 rep 自己不带计划)不算进来。"""
    plan: dict[str, float] = defaultdict(float)
    seen: set = set()
    for c in cards:
        b, pn, pl, pk = c.get("b"), c.get("pn"), c.get("pl"), c.get("pk")
        if not b or b in seen:
            continue
        # 优先用这一批实际排出来的槽位数(pk: 外部保底 / 封顶之后的); 没记 pk 的老牌退回 n·份额
        if isinstance(pk, dict) and pk:
            seen.add(b)
            for s, k in pk.items():
                if isinstance(k, (int, float)) and k > 0:
                    plan[str(s)] += float(k)
            continue
        if not isinstance(pl, dict) or not isinstance(pn, (int, float)):
            continue
        seen.add(b)
        for s, share in pl.items():
            if isinstance(share, (int, float)) and share > 0:
                plan[str(s)] += float(pn) * float(share)
    return plan


def health(cards, *, now: datetime | None = None) -> dict:
    """每个项目: 配额外比例(pin / rep 不算)、30 天不同 key ÷ 牌数(rep 不算)、14 天最大杠杆份额、来源实际 vs 计划、
    fb / stale / 快照比例、写手点名比例、贴合度均值。"""
    now = now or datetime.now(timezone.utc)
    by_p: dict[str, list] = defaultdict(list)
    for c in cards:
        by_p[c["project"]].append(c)
    out = {}
    for proj, cs in sorted(by_p.items()):
        v2 = [c for c in cs if c.get("pol") == "v2"]
        normal = [c for c in v2 if c["src"] in COMPARE_SOURCES]
        off = [c for c in normal if isinstance(c.get("pv"), list) and c["pv"] and float(c["pv"][0]) <= 0]
        last30 = [c for c in cs if c["src"] != "rep" and (now - c["drawn_at"]).days < 30]
        last14 = [c for c in cs if (now - c["drawn_at"]).days < 14 and c.get("lever")]
        lev14 = Counter(c["lever"] for c in last14)
        src = Counter(c["src"] for c in v2)
        plan = _planned_sources(v2)
        fits = [c["fit"] for c in normal if c.get("fit") is not None]
        out[proj] = {
            "cards": len(cs), "v2_cards": len(v2),
            "off_quota_share": round(len(off) / len(normal), 3) if normal else None,
            "distinct_keys_per_card_30d": round(len({c["key"] for c in last30}) / len(last30), 3) if last30 else None,
            "max_lever_share_14d": round(max(lev14.values()) / len(last14), 3) if last14 else None,
            "sources": dict(src),
            "sources_planned": {k: round(v, 1) for k, v in plan.items()},
            "fb_share": round(sum(1 for c in cs if c["src"] == "fb") / len(cs), 3) if cs else None,
            "rpc_only_share": round(sum(1 for c in v2 if c.get("dd") == "rpc_only") / len(v2), 3) if v2 else None,
            "stale_or_snapshot_share": (round(sum(1 for c in v2 if c.get("ps") in ("stale", "snap", "uniform"))
                                              / len(v2), 3) if v2 else None),
            "writer_share": round(sum(1 for c in v2 if c["src"] in WRITER_SOURCES) / len(v2), 3) if v2 else None,
            "fit_mean": round(sum(fits) / len(fits), 3) if fits else None,
        }
    return out


def report(ledger_rows, *, adoption: dict | None = None, snapshots=None, notes: dict | None = None,
           now: datetime | None = None, days: int = 28) -> dict:
    cards = card_rows(ledger_rows, adoption=adoption, snapshots=snapshots, notes=notes, now=now)
    comp = []
    for treat, ctrl in (("w", "x"), ("e", "w"), ("h", "w")):
        for m in ("m1", "m2", "m3", "d1"):
            comp.append(mantel_haenszel(cards, treat, ctrl, m))
    return {
        "days": days, "cards": len(cards),
        "by_source": {s: {"cards": n, **{m: _rate([c for c in cards if c["src"] == s], m) for m in METRICS}}
                      for s, n in sorted(Counter(c["src"] for c in cards).items())},
        "table": table(cards),
        "compare": comp,
        "ipw_uniform": [ipw_uniform(cards, m) for m in ("m1", "m2")],
        "x_only_lever_lift": [x_only_lever_lift(cards, m) for m in ("m1", "m2")],
        "health": health(cards, now=now),
        "notes": ["M2 只看销账的那一版有没有按内容对上 TV 的笔记, 不卡「7 天内」(对照表不带对上的时间)",
                  "pin / rep 是写手点名的, 不进比较; legacy 是 angle cards v2 之前的牌(没有 _draw)"],
    }


def format_report(rep: dict) -> str:
    lines = [f"draw-report: 近 {rep['days']} 天 {rep['cards']} 张牌"]
    lines.append("来源        张数   M1 写了        M2 发了        M3 留着        D1 撞车")
    for s, row in rep["by_source"].items():
        cells = []
        for m in ("m1", "m2", "m3", "d1"):
            r = row[m]
            cells.append(f"{r['y']}/{r['n']}" + (f" ({r['rate']:.0%})" if r["rate"] is not None else ""))
        lines.append(f"  {s:<8} {row['cards']:>5}   " + "   ".join(f"{c:<12}" for c in cells))
    lines.append("比较(Mantel–Haenszel, 按项目分层):")
    for c in rep["compare"]:
        if c["strata"]:
            lines.append(f"  {c['treat']} vs {c['ctrl']} {c['metric']}: 风险差 {c['rd_mh']} · 风险比 {c['rr_mh']} "
                         f"({c['y_treat']}/{c['n_treat']} vs {c['y_ctrl']}/{c['n_ctrl']}, {c['strata']} 个项目)")
    for r in rep["ipw_uniform"]:
        lines.append(f"  配额内均匀乱抽 {r['metric']}: {r['estimate']} (只用试新牌 {r['explore_cards']} 张; "
                     f"w + x 实际 {r['observed']}, {r['observed_cards']} 张)")
    lines.append("健康:")
    for proj, h in rep["health"].items():
        lines.append(f"  {proj}: 配额外 {h['off_quota_share']} · 不同 key/牌 {h['distinct_keys_per_card_30d']} · "
                     f"14 天最大杠杆份额 {h['max_lever_share_14d']} · fb {h['fb_share']} · 写手点名 {h['writer_share']} · "
                     f"来源 {h['sources']}")
    for n in rep.get("notes") or ():
        lines.append(f"  注: {n}")
    return "\n".join(lines)
