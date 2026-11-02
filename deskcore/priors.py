"""deskcore/priors.py — 发牌 v2 的先验: 项目现在发什么、什么在起量、外部同品类什么在涨(angle cards v2 §2.2 §2.3 §4)。

发牌(drawpolicy, 纯函数)只认一个 Profile(普通 dict); 这里负责把它从 TV 的数据里算出来, 并且【永远不抛、永远按时回】:

  ① 全库结果表(truth_vault.notes 的打标签列 + title_is_question 答案 + 项目品类) → 全局表 ``build_global``:
     每个 TV 项目的干净标签计数(按 内容形式 × 情绪杠杆 / 内容形式 × 问句与否 分格)和发帖配比。只有计数, 没有标题、
     正文、笔记 id —— 所以它能原样落成 vendored 快照(vendor/angle_priors_snapshot.json), TV 读不到时顶上。
  ② 写作台项目自己的近 60 天(对照表里的 TV 项目) → 项目块 ``_build_project``: 自己的配比(近两周双倍权)、已发出去
     三天以上的计数、标题句式、已发的特征题比例、本项目 / 同品牌上一期评论排前一成的帖(只留结构)。
  ③ 外部同品类近 45 天抓到的帖 → 外部块 ``external_pool``: 发帖时间从 note_id 解出来, 按日均评论排, 只留结构描述。

三块各自缓存(进程内, 单飞: 同一块同一时刻只有一个线程在建, 两个工作线程), ``get_profile`` 至多等 budget_s 秒,
等不到就用过期的(72 小时内)、再不行用快照、再不行用均匀分布, 后台接着建。``warm`` 只投递不等待(open_project 用)。
``cached_profile`` 只读缓存, 一次网络都不发(简报的 project_now 用)。

公式(§2.2): 干净标签 y; 期望成功数按内容形式分层(流量贴 / 产品贴的起量率差五倍, 不分层的话格式差异会冒充成情绪
杠杆的差异); 情绪杠杆提升 = 全局(去掉本品牌) → 同品牌 → 本项目三层收缩, 截在 [0.5, 1.6]; 标题只分问句 / 非问句,
同品牌 → 本项目两层, 再按已发比例夹在一个带子里。品类层默认关(回测里它让 AUC 变差)。常数都在 PARAMS, 一处改。

Profile 的键(drawpolicy / project_now 读这些):
  stage none|cold|warm|run · n_res · ps live|stale|snap|uniform(全局先验从哪来)
  pp live|stale|table|none(项目块; table = 项目块读不到、对照表读到了, 配比从全局表 / 快照里本项目那几条拼)
  prior_age_min · tv{ids,current,brand,brand_ids,earlier_ids,category} · ref{source,ids}
  mix{posts,per_day,window_days,question_share,<四个维度>: 收缩后的配比} · evidence{<维度>: 本项目近 60 天原始占比}
  pi{content_format,title_structure,emotional_lever,human_truth_archetype} · lift{emotional_lever,beta,title,p_question,…}
  extras{emotional_intensity,trend_dependencies} · working{resolved,successes,by_title,by_lever} · fq_shares
  ext{category,ok,eligible,pool,title_shares,updated_days_ago,recent_14d,top_structures,state} · hits[…]
笔记 id 只出现在 ext.pool[].ref / hits[].ref(给台账记冷却用, 存进私有库的 _draw.ref), 标题和正文只在进程里用来分句式。
"""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import threading
import time
from concurrent.futures import Future, wait as _fwait
from datetime import datetime, timedelta, timezone

from . import vocab

logger = logging.getLogger("deskcore.priors")

# ── 常数(§2.11) ──────────────────────────────────────────────────────────
# drawpolicy 的 PARAMS 以这一份为底再加发牌自己的常数; 回测(priors_backtest)也读这一份 —— 预注册的决策规则(§5.5)
# 每条都是改一个常数。
PARAMS: dict = {
    # 标签(§2.2)
    "label_min_age_days": 3,
    "label_min_comments": 20,
    "label_exclude_projects": ("TGV_phase1",),
    # 分层期望: r_p = (Y+1)/(N+20), r_pf = (Y_pf + 2·r_p)/(N_pf + 2)
    "rate_prior_y": 1.0, "rate_prior_n": 20.0, "fmt_prior_n": 2.0,
    # 情绪杠杆提升: 全局(+5) → [品类(+5, 默认关)] → 品牌(+5) → 本项目(+60 篇 × r_P)
    "lev_global_k": 5.0, "lev_category_k": 5.0, "lev_brand_k": 5.0, "lev_project_k": 60.0,
    "lev_clip": (0.5, 1.6),
    "beta": 1.0,
    "lift_category": False,
    # 标题提升: 品牌(+8) → 本项目(+8 个期望成功数, 见 title_lift 的说明) → 截 [0.6, 1.6] → 带子
    "ttl_brand_k": 8.0, "ttl_project_k": 8.0,
    "ttl_clip": (0.6, 1.6),
    "ttl_band_thin": (0.15, 0.10), "ttl_band_rich": (0.25, 0.20), "ttl_rich_successes": 30,
    "ttl_bounds": (0.10, 0.80),
    "lift_signal": "binary",            # "rank": 用 (项目, 形式) 内评论数的中位秩代替 y(回测高出 0.02 才换)
    # 配比(§2.3)
    "mix_kappa": 30.0, "mix_window_days": 60, "mix_recent_days": 14, "mix_recent_weight": 2.0,
    "ref_window_days": 180, "ref_brand_min": 50, "ref_category_min": 100,
    # 目标分布 π
    "fmt_support_min": 0.03,
    "ttl_floor": 0.02, "ext_title_blend": 0.25, "ext_title_min": 15,
    "lev_mix_exp": 0.5, "lev_floor": 0.02, "lev_cap": 0.40,
    "arc_mix_exp": 1.0, "arc_floor": 0.01, "arc_cap": 0.35,
    "extras_kappa": 30.0,
    # 阶段(按 60 天窗口里已出标签的篇数)
    "stage_warm": 15, "stage_run": 60,
    # 外部参考(§2.5)
    "ext_min_age_days": 3, "ext_max_age_days": 90, "ext_scrape_days": 45, "ext_ok_min": 5,
    "ext_velocity_floor_days": 3,
    # 本项目起量帖(§2.5)
    "hit_min_comments": 10, "hit_top_frac": 0.10,
    "hit_own_age": (2, 21), "hit_brand_age": (2, 45), "hit_own_weight": 3.0, "hit_brand_weight": 1.0,
    # 缓存(§4)
    "ttl_global_s": 12 * 3600, "ttl_project_s": 2 * 3600, "ttl_ext_s": 6 * 3600,
    "stale_max_s": 72 * 3600, "budget_s": 2.5, "retry_after_fail_s": 60,
}

HIT_TIERS = frozenset({"爆", "大爆"})
RESOLVED_TIERS = frozenset({"爆", "大爆", "趴"})
PAVED_ROUTE = "铺评工单"
Q, NQ = "Q", "NQ"
QUESTION = vocab.TITLE_STRUCTURES[0]          # 「疑问句」

DIMS = ("content_format", "title_structure", "emotional_lever", "human_truth_archetype")
VALUES: dict[str, tuple[str, ...]] = {
    "content_format": vocab.CONTENT_FORMATS,
    "title_structure": vocab.TITLE_STRUCTURES,
    "emotional_lever": vocab.EMOTIONAL_LEVERS,
    "human_truth_archetype": vocab.HUMAN_TRUTH_ARCHETYPES,
    "emotional_intensity": vocab.EMOTIONAL_INTENSITIES,
    "trend_dependencies": vocab.TREND_DEPENDENCIES,
}
MIX_DIMS = ("content_format", "emotional_lever", "human_truth_archetype", "emotional_intensity",
            "trend_dependencies")

# ⑤ 本项目已发帖的特征题(TV subject_type=note, run_tag=primary); ⑦ 外部帖的(Jev, external_note / external)
FQ_QIDS = ("title_is_question", "opening_type", "turning_point", "negative_outcome_happened", "comparison_group",
           "calls_out_reader_group", "ending_asks_reader", "asks_for_help")
EXT_QIDS = ("title_is_question", "opening_type", "turning_point", "ending_asks_reader", "asks_for_help",
            "own_experience", "efficacy_promise")
HIT_DESC_QIDS = ("opening_type", "ending_asks_reader", "asks_for_help", "turning_point")
EXT_DESC_QIDS = ("opening_type", "turning_point", "ending_asks_reader", "asks_for_help", "own_experience")

# ── 外部品类(§4 末) ────────────────────────────────────────────────────────
# 照抄 truth-vault prompts/external_shelf_v1.json 的 categories(「外部品类 → 我们哪些品牌算同品类」), 匹配口径也照
# librarian/external.py:categories_for —— 项目的品牌 + 项目名, 不分大小写, 子串命中。那边加一行, 这里跟着加一行
# (两边各有一个钉表的用例; OPEN-ITEMS 记着这份重复)。品类名必须和外部表的 category 列一字不差。
# 只抄「我们的品牌」这张表; 那份配置里的别家品牌词(mask_terms)不抄 —— 发牌从不把外部帖的标题给出去, 用不着。
EXT_CATEGORY_BY_PROJECT: dict[str, tuple[str, ...]] = {
    "戒烟": ("力克雷", "nicorette"),
    "鼻炎": ("雷诺考特",),
    "运动补给": ("sportsix",),
    "术后营养": ("大象集团", "nucare"),
    "求职留学": ("途鸽",),
    # 2026-11-02 起 Jev 加抓的三个品类(TV D-138 / TangVarie/truth-vault#261 的书架表, 一字不差地对齐)
    "家居家电": ("西屋",),
    "个护": ("hatherine",),
    "藻油": ("百健士",),
}

SNAPSHOT_PATH = os.path.join(os.path.dirname(__file__), "vendor", "angle_priors_snapshot.json")
SNAPSHOT_SCHEMA = 1


def external_categories(project) -> list[str]:
    """项目(整行 dict)命中的外部品类, 按表的顺序。同 TV 的 categories_for: 品牌 + 项目名, 小写子串。"""
    if not isinstance(project, dict):
        return []
    text = " ".join(str(project.get(k) or "") for k in ("brand", "name", "project_name")).lower()
    return [cat for cat, brands in EXT_CATEGORY_BY_PROJECT.items()
            if any(b and b.lower() in text for b in brands)]


def external_category(project) -> str | None:
    """发牌只用一个外部品类: 命中多个时取表里靠前的那个(TV 的书架会都给, 这里一张卡只能对一个品类)。"""
    cats = external_categories(project)
    return cats[0] if cats else None


def registered_brands() -> tuple[str, ...]:
    """我们登记的全部品牌(小写)。外部帖标题里点到其中任何一个的不当参考 —— 多半是我们自己铺的帖。"""
    return tuple(sorted({b.lower() for bs in EXT_CATEGORY_BY_PROJECT.values() for b in bs if b}))


# ══════════════════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════════════════

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ts(v) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _days(a: datetime, b: datetime) -> float:
    return (a - b).total_seconds() / 86400.0


def published_at(row: dict, now: datetime) -> datetime | None:
    """这篇发出去的时刻: publish_time 读得出、而且不晚于 now。排期还没发的(publish_time 在未来)回 None —— 不算发了,
    不进任何「已发」的聚合(配比、篇数、近两周加倍、最后发帖日、起量帖池、全局表、回测), 同 store._recent_published
    的上界(review on aw#140)。"""
    t = _ts(row.get("publish_time"))
    return t if t is not None and t <= now else None


def _truthy(v) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() == "true")


def _flags(row: dict) -> dict:
    f = row.get("data_quality_flags")
    if isinstance(f, str):
        try:
            f = json.loads(f)
        except ValueError:
            f = None
    return f if isinstance(f, dict) else {}


def is_synthetic(row: dict) -> bool:
    return _truthy(_flags(row).get("synthetic"))


def is_clean(row: dict) -> bool:
    """干净: 不是合成的、没投流、评论区没走过铺评工单(§2.2)。"""
    f = _flags(row)
    routes = f.get("comment_maintained_routes")
    paved = isinstance(routes, (list, tuple)) and PAVED_ROUTE in routes
    return not (_truthy(f.get("synthetic")) or _truthy(f.get("paid_promoted")) or paved)


def answer_bool(a) -> bool | None:
    s = str(a or "").strip()
    return True if s == "是" else False if s == "否" else None


def _norm(d: dict) -> dict:
    s = sum(v for v in d.values() if v > 0)
    return {k: (max(v, 0.0) / s if s > 0 else 0.0) for k, v in d.items()}


def floor_cap(weights: dict, floor: float = 0.0, cap: float = 1.0) -> dict:
    """按比例归一, 同时每个值落在 [floor, cap] 里: p_i = clip(λ·w_i, floor, cap), λ 二分到 Σp = 1。

    这就是「地板 + 封顶, 超出的部分按比例灌给其余的」(water-filling): 没碰到边的值之间比例不变。地板 / 封顶不可行时
    (n·floor > 1 或 n·cap < 1)各自放宽到 1/n。全零权重回均匀。"""
    keys = list(weights)
    n = len(keys)
    if not n:
        return {}
    w = {k: max(0.0, float(weights[k] or 0.0)) for k in keys}
    floor = min(max(float(floor), 0.0), 1.0 / n)
    cap = max(float(cap), 1.0 / n)
    top = max(w.values())
    if top <= 0:
        return {k: 1.0 / n for k in keys}
    # 零权重的值给一个极小的正权重: 封顶 + 地板凑不满 1 时, 多出来的那一截平分给它们(而不是最后整体归一把封顶顶破)
    w = {k: max(v, top * 1e-12) for k, v in w.items()}

    def total(lam: float) -> float:
        return sum(min(cap, max(floor, lam * w[k])) for k in keys)

    lo, hi = 0.0, 1.0
    for _ in range(200):
        if total(hi) >= 1.0:
            break
        hi *= 2.0
    for _ in range(100):
        mid = (lo + hi) / 2.0
        if total(mid) < 1.0:
            lo = mid
        else:
            hi = mid
    p = {k: min(cap, max(floor, hi * w[k])) for k in keys}
    s = sum(p.values())
    return {k: v / s for k, v in p.items()}


def _clip(x: float, lohi) -> float:
    return min(lohi[1], max(lohi[0], x))


# ══════════════════════════════════════════════════════════════════════
# 标签(§2.2)
# ══════════════════════════════════════════════════════════════════════

def label(row: dict, *, has_cc: bool, now: datetime, params: dict = PARAMS) -> int | None:
    """一篇 TV 笔记的干净标签: 1 起量 / 0 没起 / None 不参与。

    参与: 发出满 3 天(publish_time 只到天), 不是 TGV_phase1, 干净(``is_clean``)。
    项目有评论数的(``has_cc``): 评论数为 NULL 的不参与; 爆 / 大爆 或评论 ≥ 20 记 1。
    项目整个没有评论数的: 只认 tier 已判的(爆 / 大爆 / 趴), 爆 / 大爆 记 1。"""
    if str(row.get("project_id") or "") in params["label_exclude_projects"]:
        return None
    pt = _ts(row.get("publish_time"))
    if pt is None or _days(now, pt) < params["label_min_age_days"]:
        return None
    if not is_clean(row):
        return None
    tier = row.get("tier")
    if has_cc:
        cc = row.get("comments_count")
        if cc is None:
            return None
        try:
            return int(tier in HIT_TIERS or int(cc) >= params["label_min_comments"])
        except (TypeError, ValueError):
            return None
    if tier not in RESOLVED_TIERS:
        return None
    return int(tier in HIT_TIERS)


def _valid(dim: str, v) -> str | None:
    s = str(v or "").strip()
    return s if s and s in VALUES[dim] else None


def _qclass(answer) -> str | None:
    b = answer_bool(answer)
    return Q if b is True else NQ if b is False else None


def _new_label() -> dict:
    return {"n": 0, "y": 0, "rk": False, "fmt": {}, "lev": {}, "ttl": {}}


def _cell(table: dict, f: str, v: str) -> list:
    return table.setdefault(f, {}).setdefault(v, [0, 0, 0.0])


def label_add(lbl: dict, *, fmt, lever, qclass, y: int, rank: float | None = None, n: int = 1) -> None:
    """往一个项目的标签表里记 n 篇(同一格)。fmt 为空记在 "" 那一层(它也有自己的起量率)。rank: 这几篇的中位秩之和。"""
    f = _valid("content_format", fmt) or ""
    r = float(rank if rank is not None else y * n)
    lbl["n"] += n
    lbl["y"] += y * n
    fc = lbl["fmt"].setdefault(f, [0, 0, 0.0])
    fc[0] += n
    fc[1] += y * n
    fc[2] += r
    lev = _valid("emotional_lever", lever)
    if lev:
        c = _cell(lbl["lev"], f, lev)
        c[0] += n
        c[1] += y * n
        c[2] += r
    if qclass in (Q, NQ):
        c = _cell(lbl["ttl"], f, qclass)
        c[0] += n
        c[1] += y * n
        c[2] += r


def _mid_ranks(values: list[float]) -> list[float]:
    """每个值在这一组里的中位秩百分位: (比它小的个数 + 0.5·和它相等的个数) / n。"""
    n = len(values)
    order = sorted(values)
    out = []
    import bisect
    for v in values:
        lo = bisect.bisect_left(order, v)
        hi = bisect.bisect_right(order, v)
        out.append((lo + 0.5 * (hi - lo)) / n)
    return out


# ══════════════════════════════════════════════════════════════════════
# 全局表 ①②③ → 快照形状
# ══════════════════════════════════════════════════════════════════════

def _new_mix() -> dict:
    m: dict = {d: {} for d in MIX_DIMS}
    m["title_q"] = {}
    m["n"] = 0.0
    return m


def _mix_add(m: dict, row: dict, w: float = 1.0, *, title_key: str | None = None, title_dim: str = "title_q") -> None:
    m["n"] = m.get("n", 0.0) + w
    for dim in ("content_format", "emotional_lever", "emotional_intensity"):
        v = _valid(dim, row.get(dim))
        if v:
            m[dim][v] = m[dim].get(v, 0.0) + w
    for dim in ("human_truth_archetype", "trend_dependencies"):
        arr = row.get(dim)
        vals = [x for x in (_valid(dim, a) for a in (arr if isinstance(arr, (list, tuple)) else [])) if x]
        for v in vals:
            m[dim][v] = m[dim].get(v, 0.0) + w / len(vals)
    if title_key:
        t = m.setdefault(title_dim, {})
        t[title_key] = t.get(title_key, 0.0) + w


def _round_tree(x, nd: int = 3):
    if isinstance(x, float):
        r = round(x, nd)
        return int(r) if r == int(r) else r
    if isinstance(x, dict):
        return {k: _round_tree(v, nd) for k, v in x.items()}
    if isinstance(x, list):
        return [_round_tree(v, nd) for v in x]
    return x


def build_global(notes, title_answers, projects_meta, *, now: datetime | None = None,
                 params: dict = PARAMS) -> dict:
    """① 全库笔记 + ② title_is_question 答案 + ③ 项目品类 → 全局表(和快照同一个形状, 只有计数)。

    ``projects.<id>``:
      category / brand(项目号 "_" 前那一截) / first / last(发帖日期) / cc(有没有评论数)
      label: {n, y, rk, fmt{形式: [N, Y, R]}, lev{形式: {杠杆: [N, Y, R]}}, ttl{形式: {Q|NQ: [N, Y, R]}}}
             R = 中位秩之和(rank 信号用; 没有评论数的项目 rk=false)
      mix:        它自己最后 180 天数据的配比(同品牌上一期当参照时用)
      mix_recent: 现在往回 180 天的配比(品类 / 全局参照用)
    配比各维是计数(人性原型、时效依赖一篇多值按 1/len 记), title_q 是 {Q, NQ} 篇数, n 是篇数。"""
    now = now or _utcnow()
    answers = title_answers or {}
    meta = projects_meta or {}
    rows_by_p: dict[str, list[dict]] = {}
    for r in notes or ():
        pid = str(r.get("project_id") or "")
        if pid and published_at(r, now) is not None:            # 排期还没发的不算(first / last / 配比 / 有没有评论数)
            rows_by_p.setdefault(pid, []).append(r)
    win = timedelta(days=params["ref_window_days"])
    out: dict = {"schema": SNAPSHOT_SCHEMA, "built_at": now.isoformat(timespec="seconds"), "projects": {}}
    for pid in sorted(set(rows_by_p) | set(meta)):
        rows = rows_by_p.get(pid, [])
        info = meta.get(pid) or {}
        entry: dict = {"category": info.get("category") or None, "brand": pid.split("_", 1)[0]}
        if rows:
            times = [_ts(r.get("publish_time")) for r in rows]
            last = max(times)
            entry["first"] = min(times).date().isoformat()
            entry["last"] = last.date().isoformat()
            has_cc = any(r.get("comments_count") is not None for r in rows)
            lbl = _new_label()
            elig = []
            for r in rows:
                y = label(r, has_cc=has_cc, now=now, params=params)
                if y is not None:
                    elig.append((r, y))
            ranks: dict[int, float] = {}
            if has_cc:
                lbl["rk"] = True
                by_f: dict[str, list[int]] = {}
                for i, (r, _y) in enumerate(elig):
                    by_f.setdefault(_valid("content_format", r.get("content_format")) or "", []).append(i)
                for idxs in by_f.values():
                    vals = [float(elig[i][0].get("comments_count") or 0) for i in idxs]
                    for i, rk in zip(idxs, _mid_ranks(vals)):
                        ranks[i] = rk
            for i, (r, y) in enumerate(elig):
                label_add(lbl, fmt=r.get("content_format"), lever=r.get("emotional_lever"),
                          qclass=_qclass(answers.get(str(r.get("note_id")))), y=y, rank=ranks.get(i))
            entry["label"] = lbl
            mix_a, mix_b = _new_mix(), _new_mix()
            for r, t in zip(rows, times):
                if is_synthetic(r):
                    continue
                tq = _qclass(answers.get(str(r.get("note_id"))))
                if t >= last - win:
                    _mix_add(mix_a, r, title_key=tq)
                if t >= now - win:
                    _mix_add(mix_b, r, title_key=tq)
            entry["mix"] = mix_a
            if mix_b["n"] > 0:
                entry["mix_recent"] = mix_b
        out["projects"][pid] = entry
    return _round_tree(out)


def snapshot_ok(gt) -> bool:
    return (isinstance(gt, dict) and gt.get("schema") == SNAPSHOT_SCHEMA
            and isinstance(gt.get("projects"), dict))


SNAPSHOT_MAX_BYTES = 120_000
_SNAPSHOT_PROJECT_KEYS = frozenset({"category", "brand", "first", "last", "label", "mix", "mix_recent"})
_SNAPSHOT_LABEL_KEYS = frozenset({"n", "y", "rk", "fmt", "lev", "ttl"})
_SNAPSHOT_MIX_KEYS = frozenset(MIX_DIMS) | {"title_q", "n"}


def snapshot_problems(gt) -> list[str]:
    """快照(或全局表)里不该有的东西: 形状不对、词表外的值、白名单外的键(标题 / 正文 / 笔记 id 一个都不许进 —— 仓库是公开的)。
    空列表 = 干净。快照脚本写盘前、测试里都跑这个。"""
    if not snapshot_ok(gt):
        return ["not a schema-%d angle priors table" % SNAPSHOT_SCHEMA]
    bad: list[str] = []
    extra = set(gt) - {"schema", "built_at", "source", "projects"}
    if extra:
        bad.append(f"top-level keys {sorted(extra)}")
    for pid, e in gt["projects"].items():
        if not isinstance(e, dict):
            bad.append(f"{pid}: not an object")
            continue
        if set(e) - _SNAPSHOT_PROJECT_KEYS:
            bad.append(f"{pid}: keys {sorted(set(e) - _SNAPSHOT_PROJECT_KEYS)}")
        lbl = e.get("label")
        if lbl is not None:
            if set(lbl) - _SNAPSHOT_LABEL_KEYS:
                bad.append(f"{pid}: label keys {sorted(set(lbl) - _SNAPSHOT_LABEL_KEYS)}")
            for f in (lbl.get("fmt") or {}):
                if f and f not in vocab.CONTENT_FORMATS:
                    bad.append(f"{pid}: format {f!r}")
            for f, cells in (lbl.get("lev") or {}).items():
                bad += [f"{pid}: lever {v!r}" for v in cells if v not in vocab.EMOTIONAL_LEVERS]
            for f, cells in (lbl.get("ttl") or {}).items():
                bad += [f"{pid}: title class {v!r}" for v in cells if v not in (Q, NQ)]
        for key in ("mix", "mix_recent"):
            m = e.get(key)
            if m is None:
                continue
            if set(m) - _SNAPSHOT_MIX_KEYS:
                bad.append(f"{pid}.{key}: keys {sorted(set(m) - _SNAPSHOT_MIX_KEYS)}")
            for dim in MIX_DIMS:
                bad += [f"{pid}.{key}: {dim} {v!r}" for v in (m.get(dim) or {}) if v not in VALUES[dim]]
            bad += [f"{pid}.{key}: title {v!r}" for v in (m.get("title_q") or {}) if v not in (Q, NQ)]
    return bad


_SNAPSHOT: dict = {}


def load_snapshot(path: str | None = None) -> dict | None:
    """vendored 快照; 读不了 / 形状不对回 None(调用方退到均匀)。默认路径只读一次。"""
    p = path or SNAPSHOT_PATH
    if p in _SNAPSHOT:
        return _SNAPSHOT[p]
    try:
        with open(p, encoding="utf-8") as fh:
            gt = json.load(fh)
        if not snapshot_ok(gt):
            raise ValueError("unexpected snapshot shape")
    except Exception as exc:                            # noqa: BLE001 — 快照坏了就均匀分布, 不拦发牌
        logger.warning("angle priors snapshot unusable (%s): %s", p, f"{type(exc).__name__}: {exc}"[:200])
        gt = None
    _SNAPSHOT[p] = gt
    return gt


# ══════════════════════════════════════════════════════════════════════
# 提升(§2.2)
# ══════════════════════════════════════════════════════════════════════

def _projects(gt) -> dict:
    return (gt or {}).get("projects") or {}


def _label_of(gt, pid) -> dict | None:
    lbl = (_projects(gt).get(pid) or {}).get("label")
    return lbl if isinstance(lbl, dict) and lbl.get("n") else None


def rates(lbl: dict, params: dict = PARAMS) -> tuple[float, dict]:
    """r_p = (Y+1)/(N+20); r_pf = (Y_pf + 2·r_p)/(N_pf + 2)。"""
    rp = (lbl["y"] + params["rate_prior_y"]) / (lbl["n"] + params["rate_prior_n"])
    k = params["fmt_prior_n"]
    rpf = {f: (c[1] + k * rp) / (c[0] + k) for f, c in (lbl.get("fmt") or {}).items()}
    return rp, rpf


def oe(lbl: dict | None, table: str, *, params: dict = PARAMS, signal: str | None = None,
       stratified: bool = True) -> tuple[float, dict, dict]:
    """一个项目在 ``table``("lev" 或 "ttl")上的 (r_P, O, E): O_v = 起量篇数, E_v = Σ 这几篇按形式分层的期望。

    signal="rank": O 是中位秩之和、E 是每篇 0.5, r 记 0.5(没有评论数的项目退回 binary)。
    stratified=False 只给回测的参照模型和「分层能失败」的用例: E = N·r_p。"""
    if not lbl:
        return 0.0, {}, {}
    signal = signal or params["lift_signal"]
    rank = signal == "rank" and lbl.get("rk")
    rp, rpf = rates(lbl, params)
    O: dict[str, float] = {}
    E: dict[str, float] = {}
    for f, cells in (lbl.get(table) or {}).items():
        for v, c in cells.items():
            n, y, r = c[0], c[1], (c[2] if len(c) > 2 else c[1])
            if rank:
                O[v] = O.get(v, 0.0) + r
                E[v] = E.get(v, 0.0) + 0.5 * n
            else:
                O[v] = O.get(v, 0.0) + y
                E[v] = E.get(v, 0.0) + n * (rpf.get(f, rp) if stratified else rp)
    return (0.5 if rank else rp), O, E


def _sum_oe(gt, pids, table, params, signal) -> tuple[dict, dict]:
    O: dict[str, float] = {}
    E: dict[str, float] = {}
    for pid in pids:
        _r, o, e = oe(_label_of(gt, pid), table, params=params, signal=signal)
        for v, x in o.items():
            O[v] = O.get(v, 0.0) + x
        for v, x in e.items():
            E[v] = E.get(v, 0.0) + x
    return O, E


def brand_of(pid: str | None) -> str:
    return str(pid or "").split("_", 1)[0]


def earlier_phases(gt, current: str | None, candidates=(), *, now: datetime | None = None) -> list[str]:
    """同品牌【上一期】(§2.3 配比参照、§2.5 起量帖; 同 priors_backtest._earlier_brand): 品牌前缀和本期一样、最后一篇
    发在本期第一篇之前的那几期。后一期、同时在跑的那一期都不算(复审: 只对上前一期的写作台项目, 卡上不能把后一期的帖
    说成「同品牌上一期」)。本期还不在全局表 / 快照里(刚开的新一期)按「今天才开始」算。日期是 YYYY-MM-DD 字符串。"""
    if not current:
        return []
    projs = _projects(gt)
    start = (projs.get(current) or {}).get("first") or (now or _utcnow()).date().isoformat()
    b = brand_of(current)
    return sorted({p for p in candidates or () if p != current and brand_of(p) == b
                   and (projs.get(p) or {}).get("last") and str(projs[p]["last"]) < str(start)})


def lever_lift(gt, current: str | None, *, brand_ids=(), params: dict = PARAMS, signal: str | None = None) -> dict:
    """情绪杠杆提升(clip 后、^β 之前)和各层: ``{"lift", "global", "brand", "category"}``。

    G(v) = (Σ_{p∉本品牌} O + 5)/(Σ E + 5); [C(v) = (Σ_{同品类, 非本品牌} O + 5·G)/(Σ E + 5), 默认关];
    B(v) = (Σ_{同品牌其他期} O + 5·G)/(Σ E + 5); L(v) = (O_P + 60·r_P·B)/(E_P + 60·r_P) → 截 [0.5, 1.6]。
    current=None(还没对上 TV 项目): L = G(全部项目) 截一下。"""
    signal = signal or params["lift_signal"]
    projs = _projects(gt)
    own_brand = brand_of(current) if current else None
    g_ids = [p for p in projs if brand_of(p) != own_brand]
    GO, GE = _sum_oe(gt, g_ids, "lev", params, signal)
    G = {v: (GO.get(v, 0.0) + params["lev_global_k"]) / (GE.get(v, 0.0) + params["lev_global_k"])
         for v in vocab.EMOTIONAL_LEVERS}
    out = {"global": G, "category": None, "brand": None}
    if not current:
        out["lift"] = {v: _clip(G[v], params["lev_clip"]) for v in G}
        return out
    base = G
    if params.get("lift_category"):
        cat = (projs.get(current) or {}).get("category")
        c_ids = [p for p in projs if cat and (projs[p] or {}).get("category") == cat and brand_of(p) != own_brand]
        CO, CE = _sum_oe(gt, c_ids, "lev", params, signal)
        k = params["lev_category_k"]
        base = {v: (CO.get(v, 0.0) + k * G[v]) / (CE.get(v, 0.0) + k) for v in G}
        out["category"] = base
    br = [p for p in brand_ids if p != current]
    BO, BE = _sum_oe(gt, br, "lev", params, signal)
    kb = params["lev_brand_k"]
    B = {v: (BO.get(v, 0.0) + kb * base[v]) / (BE.get(v, 0.0) + kb) for v in G}
    out["brand"] = B
    rp, O, E = oe(_label_of(gt, current), "lev", params=params, signal=signal)
    kp = params["lev_project_k"] * rp
    out["lift"] = {v: _clip((O.get(v, 0.0) + kp * B[v]) / (E.get(v, 0.0) + kp), params["lev_clip"])
                   if (E.get(v, 0.0) + kp) > 0 else _clip(B[v], params["lev_clip"]) for v in G}
    return out


def title_lift(gt, current: str | None, *, brand_ids=(), params: dict = PARAMS, signal: str | None = None) -> dict:
    """标题两类(问句 Q / 非问句 NQ)的提升: ``{"lift": {Q, NQ}, "brand": {Q, NQ}, "successes": S_T}``。

    B_T(c) = (O_Br,c + 8)/(E_Br,c + 8); L_T(c) = (O_Pc + k·B_T(c))/(E_Pc + k) → 截 [0.6, 1.6]。没有全局 / 品类层:
    跨项目方向不一致(6 个项目问句占优, 8 个相反), 品类层把回测 AUC 拉到 0.45。

    ⚠️ k 取 8 个期望成功数(PARAMS["ttl_project_k"]), 不是 §2.2 公式字面上的 60·r_P: 按 60·r_P(西屋 r≈5.4% 时
    才 3.2)算, 西屋会是 L_T(Q)=0.67 / L_T(NQ)=1.54、P* 0.46、被带子托在 0.51; spec 自己的算例(L_T 0.91 / 1.16,
    P(疑问句)=0.60)和 §0、§6 的金标准用例都只有 k=8 才对得上。"""
    signal = signal or params["lift_signal"]
    br = [p for p in brand_ids if p != current]
    BO, BE = _sum_oe(gt, br, "ttl", params, signal)
    kb = params["ttl_brand_k"]
    B = {c: (BO.get(c, 0.0) + kb) / (BE.get(c, 0.0) + kb) for c in (Q, NQ)}
    out: dict = {"brand": B, "successes": 0.0}
    if not current:
        out["lift"] = {Q: 1.0, NQ: 1.0}
        return out
    _rp, O, E = oe(_label_of(gt, current), "ttl", params=params, signal=signal)
    k = params["ttl_project_k"]
    out["lift"] = {c: _clip((O.get(c, 0.0) + k * B[c]) / (E.get(c, 0.0) + k), params["ttl_clip"]) for c in (Q, NQ)}
    out["successes"] = O.get(Q, 0.0) + O.get(NQ, 0.0)
    return out


def p_question(q: float, lift_t: dict, successes: float, params: dict = PARAMS) -> tuple[float, float, tuple]:
    """P(疑问句): P* = q·L_Q / (q·L_Q + (1−q)·L_NQ), 夹进 [q−.15, q+.10](本项目标题起量 < 30 篇)或 [q−.25, q+.20],
    再夹进 [0.10, 0.80]。回 (P, P*, 带子)。"""
    lq, lnq = float(lift_t.get(Q, 1.0)), float(lift_t.get(NQ, 1.0))
    den = q * lq + (1 - q) * lnq
    p_star = q * lq / den if den > 0 else q
    lo_d, hi_d = params["ttl_band_rich"] if successes >= params["ttl_rich_successes"] else params["ttl_band_thin"]
    band = (q - lo_d, q + hi_d)
    p = _clip(_clip(p_star, band), params["ttl_bounds"])
    return p, p_star, band


# ══════════════════════════════════════════════════════════════════════
# 配比与参照(§2.3)
# ══════════════════════════════════════════════════════════════════════

def title_shares_from_q(tq: dict) -> dict:
    """参照项目只有 title_is_question 两类: 问句那一份给「疑问句」, 非问句按 NONQ_SPLIT 摊。没有答案回 {}。"""
    nq, nn = float((tq or {}).get(Q, 0.0)), float((tq or {}).get(NQ, 0.0))
    if nq + nn <= 0:
        return {}
    q = nq / (nq + nn)
    out = {QUESTION: q}
    for k, s in vocab.NONQ_SPLIT.items():
        out[k] = (1 - q) * s
    return out


def _sum_mix(gt, pids, key: str) -> dict:
    acc = _new_mix()
    for pid in pids:
        m = (_projects(gt).get(pid) or {}).get(key)
        if not isinstance(m, dict):
            continue
        acc["n"] += float(m.get("n") or 0.0)
        for dim in MIX_DIMS + ("title_q",):
            for v, x in (m.get(dim) or {}).items():
                acc[dim][v] = acc[dim].get(v, 0.0) + float(x)
    return acc


def _mix_shares(m: dict, dim: str) -> dict:
    if dim == "title_structure":
        return title_shares_from_q(m.get("title_q") or {})
    raw = {v: float((m.get(dim) or {}).get(v, 0.0)) for v in VALUES[dim]}
    return _norm(raw) if sum(raw.values()) > 0 else {}


def reference(gt, *, current: str | None, brand_ids=(), category: str | None = None, exclude=(),
              params: dict = PARAMS) -> dict:
    """配比的参照(ref_d), 按顺序退: 同品牌上一期(它最后 180 天 ≥ 50 篇) → TV 同品类(近 180 天 ≥ 100 篇) → 全库近 180 天
    → 均匀。某一维在选中的那一层没有数(比如没有标题答案)就这一维接着往下退。

    回 ``{"source", "ids", "shares": {维度: {值: 占比}}, "sources": {维度: 层}}``。"""
    projs = _projects(gt)
    excl = set(exclude) | ({current} if current else set())
    levels = []
    br = [p for p in brand_ids if p in projs and p != current]
    if br:
        levels.append(("brand", br, _sum_mix(gt, br, "mix"), params["ref_brand_min"]))
    recent = [p for p in projs if p not in excl and isinstance((projs[p] or {}).get("mix_recent"), dict)]
    if category:
        cat_ids = [p for p in recent if (projs[p] or {}).get("category") == category]
        if cat_ids:
            levels.append(("category", cat_ids, _sum_mix(gt, cat_ids, "mix_recent"), params["ref_category_min"]))
    levels.append(("global", recent, _sum_mix(gt, recent, "mix_recent"), 1))
    levels = [lv for lv in levels if lv[2]["n"] >= lv[3]]
    shares: dict = {}
    sources: dict = {}
    for dim in MIX_DIMS + ("title_structure",):
        for name, _ids, m, _min in levels:
            s = _mix_shares(m, dim)
            if s:
                shares[dim], sources[dim] = s, name
                break
        else:
            shares[dim] = {v: 1.0 / len(VALUES[dim]) for v in VALUES[dim]}
            sources[dim] = "uniform"
    head = levels[0] if levels else ("uniform", [], None, 0)
    return {"source": head[0], "ids": sorted(head[1]), "shares": shares, "sources": sources}


def shrink(own: dict, own_total: float, ref: dict, kappa: float, values) -> dict:
    """m(v) = (Σ ω·x_v + κ·ref(v)) / (Σ ω + κ)。"""
    den = float(own_total) + float(kappa)
    if den <= 0:
        return dict(ref)
    return {v: (float(own.get(v, 0.0)) + kappa * float(ref.get(v, 0.0))) / den for v in values}


# ══════════════════════════════════════════════════════════════════════
# 外部参考池(§2.5 e)
# ══════════════════════════════════════════════════════════════════════

_NOTE_EPOCH_MIN = datetime(2014, 1, 1, tzinfo=timezone.utc)


def posted_at(note_id, *, now: datetime | None = None) -> datetime | None:
    """小红书 note_id 的前 8 位十六进制是发帖的 unix 秒。解不出来 / 落在 2014 年之前或比现在还晚一天以上的回 None。"""
    s = str(note_id or "").strip()
    if len(s) < 8:
        return None
    try:
        t = datetime.fromtimestamp(int(s[:8], 16), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    now = now or _utcnow()
    if t < _NOTE_EPOCH_MIN or t > now + timedelta(days=1):
        return None
    return t


def _desc(answers: dict, qids) -> dict:
    out = {}
    for q in qids:
        a = (answers or {}).get(q)
        if q == "opening_type":
            s = str(a or "").strip()
            out[q] = s or None
        else:
            out[q] = answer_bool(a)
    return out


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def external_pool(rows, answers, *, category: str | None = None, now: datetime | None = None,
                  registered=None, params: dict = PARAMS) -> dict:
    """外部同品类帖 → 参考池(只留结构)。

    · 发帖时间从 note_id 解; 抓取时帖龄 3–90 天、近 45 天抓的;
    · 不像广告(ad_like 不是 true)、Jev 的 efficacy_promise 不是「是」、标题里没点到我们登记的品牌;
    · 日均评论 = 评论数 / max(抓取时帖龄, 3), 不低于这个品类 3–90 天帖的中位数; 不设前 N 名的上限。
    每条: ref("ext:<id>") / w(√日均评论, 抽签权重) / velocity / posted_days_ago / title_structure(Jev 的问句答案优先)
    / desc(开头类型、转折、结尾问读者、求助、亲历)。标题本身不出这个函数。"""
    now = now or _utcnow()
    reg = tuple(registered if registered is not None else registered_brands())
    ans = answers or {}
    rows = list(rows or ())
    cands = []
    latest_fetch = None
    for r in rows:
        nid = str(r.get("note_id") or "")
        posted = posted_at(nid, now=now)
        fetched = _ts(r.get("fetched_at"))
        if fetched is not None and (latest_fetch is None or fetched > latest_fetch):
            latest_fetch = fetched
        if posted is None or fetched is None:
            continue
        if _days(now, fetched) > params["ext_scrape_days"]:
            continue
        age = _days(fetched, posted)
        if not (params["ext_min_age_days"] <= age <= params["ext_max_age_days"]):
            continue
        try:
            comments = max(int(r.get("comments") or 0), 0)
        except (TypeError, ValueError):
            comments = 0
        vel = comments / max(age, float(params["ext_velocity_floor_days"]))
        cands.append((r, nid, posted, age, comments, vel))
    med = _median([c[5] for c in cands])
    pool = []
    for r, nid, posted, age, comments, vel in cands:
        a = ans.get(nid) or {}
        if _truthy(r.get("ad_like")):
            continue
        if answer_bool(a.get("efficacy_promise")) is True:
            continue
        title = str(r.get("title") or "")
        if any(b and b in title.lower() for b in reg):
            continue
        if med is not None and vel < med:
            continue
        isq = answer_bool(a.get("title_is_question"))
        ts_class = vocab.classify_title(title, isq) or (QUESTION if isq is True else None)
        pool.append({
            "ref": f"ext:{nid}",
            "w": math.sqrt(vel),
            "velocity": round(vel, 2),
            "comments": comments,
            "posted_days_ago": max(int(_days(now, posted)), 0),
            "age_at_scrape": round(age, 1),
            "title_structure": ts_class,
            "desc": _desc(a, EXT_DESC_QIDS),
        })
    pool.sort(key=lambda e: (-e["velocity"], e["ref"]))
    counts: dict[str, int] = {}
    for e in pool:
        if e["title_structure"]:
            counts[e["title_structure"]] = counts.get(e["title_structure"], 0) + 1
    tot = sum(counts.values())
    title_shares = {k: v / tot for k, v in counts.items()} if tot else {}
    opening: dict[str, int] = {}
    for e in pool:
        o = e["desc"].get("opening_type")
        if o in vocab.OPENING_PLAIN:
            opening[o] = opening.get(o, 0) + 1
    top = []
    if counts:
        top.append(f"标题是{max(sorted(counts), key=counts.get)}")
    if opening:
        top.append(vocab.OPENING_PLAIN[max(sorted(opening), key=opening.get)])
    return {
        "category": category,
        "scraped": len(rows),
        "age_ok": len(cands),
        "median_velocity": None if med is None else round(med, 3),
        "eligible": len(pool),
        "pool": pool,
        "title_shares": title_shares,
        "updated_days_ago": None if latest_fetch is None else max(int(_days(now, latest_fetch)), 0),
        "recent_14d": sum(1 for e in pool if e["posted_days_ago"] <= 14),
        "top_structures": top,
    }


def ref_last_used(ledger_rows) -> dict[str, datetime]:
    """台账里每个参考帖(_draw.ref)最近一次被发出去的时间 —— 外部参考 21 天、起量帖 14 天的冷却靠它(drawpolicy 用)。"""
    out: dict[str, datetime] = {}
    for r in ledger_rows or ():
        d = (r.get("dims") or {}).get("_draw") if isinstance(r.get("dims"), dict) else None
        ref = (d or {}).get("ref") if isinstance(d, dict) else None
        t = _ts(r.get("drawn_at"))
        if ref and t is not None and (ref not in out or t > out[ref]):
            out[str(ref)] = t
    return out


# ══════════════════════════════════════════════════════════════════════
# 项目块 ④⑤: 本项目近 60 天
# ══════════════════════════════════════════════════════════════════════

def _top_decile_floor(values: list[int], frac: float) -> int | None:
    if not values:
        return None
    s = sorted(values, reverse=True)
    k = max(1, math.ceil(frac * len(s)))
    return s[k - 1]


def project_aggregate(rows, answers, *, tv_ids, brand_ids=(), now: datetime | None = None,
                      params: dict = PARAMS) -> dict:
    """本项目(对照表里的 TV 项目)近 60 天的笔记 + 特征题答案 → 项目块(聚合数 + 起量帖池)。纯函数。

    rows 里同品牌其他期(brand_ids, 没对照到本项目的)的帖只用来找起量帖(45 天内), 不进本项目的配比和计数。"""
    now = now or _utcnow()
    tv = set(tv_ids or ())
    brand = set(brand_ids or ()) - tv
    ans = answers or {}
    win = params["mix_window_days"]
    recent = params["mix_recent_days"]
    # 排期还没发的(publish_time 在未来)一篇都不进: 年龄是负的会过窗口、拿近两周加倍、顶到最后发帖日, 还会改起量帖的门槛
    rows = [r for r in rows or () if published_at(r, now) is not None]
    own = [r for r in rows if str(r.get("project_id")) in tv]
    brows = [r for r in rows if str(r.get("project_id")) in brand]
    has_cc = {p: any(r.get("comments_count") is not None for r in own + brows if str(r.get("project_id")) == p)
              for p in tv | brand}
    wmix = _new_mix()
    wmix["title_structure"] = {}
    raw = _new_mix()
    raw["title_structure"] = {}
    last_by_id: dict[str, str] = {}
    posts = 0
    working = {"resolved": 0, "successes": 0, "by_title": {}, "by_lever": {}, "by_format": {}}
    fq: dict[str, dict[str, int]] = {}
    for r in own:
        t = _ts(r.get("publish_time"))
        if t is None or _days(now, t) > win:
            continue
        pid = str(r.get("project_id"))
        d = t.date().isoformat()
        if d > last_by_id.get(pid, ""):
            last_by_id[pid] = d
        if is_synthetic(r):
            continue
        posts += 1
        a = ans.get(str(r.get("note_id"))) or {}
        isq = answer_bool(a.get("title_is_question"))
        title = vocab.note_title(r.get("title"), r.get("raw_content"))
        ts_class = vocab.classify_title(title, isq)
        if ts_class is None and isq is True:
            ts_class = QUESTION
        w = params["mix_recent_weight"] if _days(now, t) <= recent else 1.0
        _mix_add(wmix, r, w)
        _mix_add(raw, r, 1.0)
        if ts_class:
            wmix["title_structure"][ts_class] = wmix["title_structure"].get(ts_class, 0.0) + w
            raw["title_structure"][ts_class] = raw["title_structure"].get(ts_class, 0.0) + 1.0
        elif isq is False:                    # 有答案「不是问句」、没有标题文本: 按 NONQ_SPLIT 摊
            for k, s in vocab.NONQ_SPLIT.items():
                wmix["title_structure"][k] = wmix["title_structure"].get(k, 0.0) + w * s
                raw["title_structure"][k] = raw["title_structure"].get(k, 0.0) + s
        for q in FQ_QIDS:
            v = a.get(q)
            if v is None or str(v).strip() == "":
                continue
            fq.setdefault(q, {})
            fq[q][str(v).strip()] = fq[q].get(str(v).strip(), 0) + 1
        y = label(r, has_cc=has_cc.get(pid, False), now=now, params=params)
        if y is None:
            continue
        working["resolved"] += 1
        working["successes"] += y
        qc = Q if (isq is True or ts_class == QUESTION) else NQ if (isq is False or ts_class) else None
        if qc:
            c = working["by_title"].setdefault(qc, [0, 0])
            c[0] += 1
            c[1] += y
        lev = _valid("emotional_lever", r.get("emotional_lever"))
        if lev:
            c = working["by_lever"].setdefault(lev, [0, 0])
            c[0] += 1
            c[1] += y
        fmt = _valid("content_format", r.get("content_format"))
        if fmt:
            c = working["by_format"].setdefault(fmt, [0, 0])
            c[0] += 1
            c[1] += y
    fq_shares: dict = {}
    for q, counts in fq.items():
        tot = sum(counts.values())
        if not tot:
            continue
        if q == "opening_type":
            fq_shares[q] = {k: v / tot for k, v in counts.items()}
        else:
            fq_shares[q] = counts.get("是", 0) / tot
    hits = _hit_pool(own, brows, ans, tv=tv, brand=brand, now=now, params=params)
    return {
        "tv_ids": sorted(tv), "brand_ids": sorted(brand), "last_by_id": last_by_id,
        "posts": posts, "window_days": win,
        "wmix": _round_tree(wmix, 4), "raw": _round_tree(raw, 4),
        "n_res": working["resolved"], "working": working,
        "fq_shares": _round_tree(fq_shares, 4), "hits": hits,
    }


def _hit_pool(own, brows, ans, *, tv, brand, now, params) -> list[dict]:
    """起量帖: 干净、评论 ≥ 10、在(TV 项目, 内容形式)里评论数排前一成。本项目 2–21 天权重 ×3, 同品牌其他期 2–45 天 ×1。
    不跨品牌。卡上只拿它的 杠杆 / 第一个人性原型 / 标题句式 和 开头、结尾、求助、转折 四个结构答案。"""
    groups: dict[tuple, list[int]] = {}
    for r in own + brows:
        if not is_clean(r) or r.get("comments_count") is None:
            continue
        key = (str(r.get("project_id")), _valid("content_format", r.get("content_format")) or "")
        try:
            groups.setdefault(key, []).append(int(r.get("comments_count")))
        except (TypeError, ValueError):
            continue
    floors = {k: _top_decile_floor(v, params["hit_top_frac"]) for k, v in groups.items()}
    out = []
    for r in own + brows:
        pid = str(r.get("project_id"))
        is_own = pid in tv
        lo, hi = params["hit_own_age"] if is_own else params["hit_brand_age"]
        t = _ts(r.get("publish_time"))
        if t is None or not is_clean(r) or r.get("comments_count") is None:
            continue
        age = _days(now, t)
        if not (lo <= age <= hi):
            continue
        try:
            cc = int(r.get("comments_count"))
        except (TypeError, ValueError):
            continue
        fmt = _valid("content_format", r.get("content_format"))
        floor = floors.get((pid, fmt or ""))
        if cc < params["hit_min_comments"] or floor is None or cc < floor:
            continue
        a = ans.get(str(r.get("note_id"))) or {}
        arcs = r.get("human_truth_archetype") if isinstance(r.get("human_truth_archetype"), (list, tuple)) else []
        arc = next((x for x in (_valid("human_truth_archetype", v) for v in arcs) if x), None)
        isq = answer_bool(a.get("title_is_question"))
        ts_class = vocab.classify_title(vocab.note_title(r.get("title"), r.get("raw_content")), isq)
        if ts_class is None and isq is True:
            ts_class = QUESTION
        out.append({
            "ref": f"note:{r.get('note_id')}",
            "own": is_own,
            "w": params["hit_own_weight"] if is_own else params["hit_brand_weight"],
            "tv_project": pid,
            "content_format": fmt,
            "emotional_lever": _valid("emotional_lever", r.get("emotional_lever")),
            "human_truth_archetype": arc,
            "title_structure": ts_class,
            "age_days": int(age),
            "comments": cc,
            "desc": _desc(a, HIT_DESC_QIDS),
        })
    out.sort(key=lambda e: (-e["w"], -e["comments"], e["ref"]))
    return out


# ══════════════════════════════════════════════════════════════════════
# 拼 Profile(纯函数, 没有 IO)
# ══════════════════════════════════════════════════════════════════════

def _uniform(dim: str) -> dict:
    return {v: 1.0 / len(VALUES[dim]) for v in VALUES[dim]}


def table_own(gt, tv_ids, *, now: datetime, params: dict = PARAMS) -> tuple[dict, int]:
    """项目块读不到、对照表读到了(pp="table", 复审)时本项目「自己的配比」和已出标签的篇数, 从全局表 / 快照里它那几条
    拼(只有聚合数): 配比用 mix_recent(现在往回 180 天; 没有就是 180 天没发过, 自己那份留空, 全靠参照); 标题只有问句 /
    非问句两类, 非问句按 NONQ_SPLIT 摊(同 drawsim.fixture_profile); 篇数用标签的 n, 最后一篇在 60 天窗口外的记 0
    (项目块的口径: 窗口里没有已出标签的就是冷启动)。"""
    projs = _projects(gt)
    own: dict = {dim: {} for dim in MIX_DIMS}
    own["title_structure"] = {}
    n_res = 0
    floor = (now - timedelta(days=params["mix_window_days"])).date().isoformat()
    for pid in tv_ids or ():
        e = projs.get(pid) or {}
        m = e.get("mix_recent")
        if isinstance(m, dict):
            for dim in MIX_DIMS:
                for v, x in (m.get(dim) or {}).items():
                    if v in VALUES[dim]:
                        own[dim][v] = own[dim].get(v, 0.0) + float(x or 0.0)
            tq = m.get("title_q") or {}
            q, nq = float(tq.get(Q) or 0.0), float(tq.get(NQ) or 0.0)
            ts = own["title_structure"]
            if q > 0:
                ts[QUESTION] = ts.get(QUESTION, 0.0) + q
            if nq > 0:
                for k, sh in vocab.NONQ_SPLIT.items():
                    ts[k] = ts.get(k, 0.0) + nq * sh
        if str(e.get("last") or "") >= floor:
            n_res += int((e.get("label") or {}).get("n") or 0)
    return own, n_res


def stage_of(n_res: int | None, mapped: bool, params: dict = PARAMS) -> str:
    if not mapped or n_res is None:
        return "none"
    if n_res < params["stage_warm"]:
        return "cold"
    return "warm" if n_res < params["stage_run"] else "run"


def compose(gt, proj: dict | None, ext: dict | None, *, project_id: str | None = None,
            now: datetime | None = None, ps: str = "live", pp: str = "live", prior_age_min: int | None = None,
            ext_state: str | None = None, ext_category: str | None = None, params: dict = PARAMS) -> dict:
    """全局表 + 项目块 + 外部块 → Profile(见模块说明)。纯函数: 回测、CLI、缓存读都走这一个。"""
    now = now or _utcnow()
    projs = _projects(gt)
    tv_ids = list((proj or {}).get("tv_ids") or [])
    mapped = bool(proj) and bool(tv_ids)
    current = None
    brand_ids: list[str] = []
    category = None
    earlier: list[str] = []
    table = mapped and bool((proj or {}).get("mapping_only"))
    if mapped:
        last = dict((proj or {}).get("last_by_id") or {})
        for pid in tv_ids:
            last.setdefault(pid, (projs.get(pid) or {}).get("last") or "")
        current = max(tv_ids, key=lambda p: (last.get(p, ""), p))
        b = brand_of(current)
        # 同品牌其他期: 情绪 / 标题提升的品牌层(§2.2 的 Br, 不论先后); 配比参照和起量帖只认上一期(§2.3 §2.5, 复审)
        brand_ids = sorted({p for p in set(projs) | set((proj or {}).get("brand_ids") or []) | set(tv_ids)
                            if brand_of(p) == b and p != current})
        earlier = earlier_phases(gt, current, brand_ids, now=now)
        category = (projs.get(current) or {}).get("category") or ((proj or {}).get("categories") or {}).get(current)
    n_res = (proj or {}).get("n_res") if mapped else None
    wmix = (proj or {}).get("wmix") or {}
    if table:
        # 项目块读不到、只有对照表(复审): 不能当成「没对上」去按 8 种形式平均发 —— 自己的配比从全局表 / 快照里拼
        wmix, n_res = table_own(gt, tv_ids, now=now, params=params)
    stage = stage_of(n_res, mapped, params)

    ref = reference(gt, current=current, brand_ids=earlier, category=category, exclude=tv_ids, params=params)
    kappa = params["mix_kappa"]
    mix: dict = {}
    for dim in ("content_format", "emotional_lever", "human_truth_archetype", "title_structure"):
        own = (wmix.get(dim) or {}) if mapped else {}
        mix[dim] = shrink(own, sum(own.values()), ref["shares"][dim], kappa, VALUES[dim])
    q = mix["title_structure"].get(QUESTION, 0.0)

    # 提升
    ll = lever_lift(gt, current if mapped else None, brand_ids=brand_ids, params=params)
    tl = title_lift(gt, current if mapped else None, brand_ids=brand_ids, params=params)
    if mapped:
        pq, p_star, band = p_question(q, tl["lift"], tl["successes"], params)
    else:
        pq, p_star, band = _clip(q, params["ttl_bounds"]), q, (q, q)
    beta = float(params["beta"])

    # π
    if mapped:
        sup = {v: s for v, s in mix["content_format"].items() if s >= params["fmt_support_min"]}
        pi_fmt = _norm(sup) if sup else _uniform("content_format")
    else:
        pi_fmt = _uniform("content_format")
    nonq = {v: mix["title_structure"].get(v, 0.0) for v in vocab.TITLE_STRUCTURES if v != QUESTION}
    nonq = _norm(nonq) if sum(nonq.values()) > 0 else dict(vocab.NONQ_SPLIT)
    ext_ok_titles = ext and ext.get("eligible", 0) >= params["ext_title_min"]
    if ext_ok_titles:
        en = {v: (ext.get("title_shares") or {}).get(v, 0.0) for v in nonq}
        if sum(en.values()) > 0:
            en = _norm(en)
            b = params["ext_title_blend"]
            nonq = {v: (1 - b) * nonq[v] + b * en[v] for v in nonq}
    # 地板只从非问句那一份里出: 疑问句就是上面夹过带子的 P, 先归一再托底会把它挤到带子外面(非问句集中在一两种
    # 句式的项目, 七个 0.02 的地板要吃掉 0.1 以上)。
    rest = max(1.0 - pq, 0.0)
    nonq_pi = floor_cap(nonq, min(params["ttl_floor"] / rest, 1.0 / len(nonq)) if rest > 0 else 0.0, 1.0)
    pi_ttl = {QUESTION: pq}
    pi_ttl.update({v: rest * s for v, s in nonq_pi.items()})
    lev_w = {v: (mix["emotional_lever"].get(v, 0.0) ** params["lev_mix_exp"]) * (ll["lift"][v] ** beta)
             for v in vocab.EMOTIONAL_LEVERS}
    pi_lev = floor_cap(lev_w, params["lev_floor"], params["lev_cap"])
    arc_w = {v: mix["human_truth_archetype"].get(v, 0.0) ** params["arc_mix_exp"] for v in vocab.HUMAN_TRUTH_ARCHETYPES}
    pi_arc = floor_cap(arc_w, params["arc_floor"], params["arc_cap"])

    # 每张卡的附加项(§2.8): 情绪浓度 / 时效依赖 向全局收缩
    glob = reference(gt, current=None, exclude=tv_ids, params=params)["shares"]
    extras = {}
    for dim in ("emotional_intensity", "trend_dependencies"):
        own = (wmix.get(dim) or {}) if mapped else {}
        extras[dim] = shrink(own, sum(own.values()), glob[dim], params["extras_kappa"], VALUES[dim])

    raw = (proj or {}).get("raw") or {}                 # table: 没有近 60 天的原始数 → evidence 空, 卡上不给括号
    posts = int((proj or {}).get("posts") or 0) if mapped else 0
    evidence = {}
    for dim in ("content_format", "emotional_lever", "human_truth_archetype", "title_structure"):
        counts = (raw.get(dim) or {}) if mapped else {}
        tot = sum(counts.values())
        evidence[dim] = {v: c / tot for v, c in counts.items()} if tot else {}

    ext_info = {"category": (ext or {}).get("category") or ext_category or (proj or {}).get("ext_category"),
                "state": ext_state or ("ok" if ext else "none")}
    if ext:
        ext_info.update({k: ext.get(k) for k in ("eligible", "pool", "title_shares", "updated_days_ago",
                                                  "recent_14d", "top_structures", "median_velocity")})
    ext_info["ok"] = bool(ext) and int(ext.get("eligible") or 0) >= params["ext_ok_min"]

    return {
        "v": 1,
        "project_id": project_id or (proj or {}).get("project_id"),
        "built_at": now.isoformat(timespec="seconds"),
        "stage": stage,
        "n_res": n_res,
        "ps": ps, "pp": pp if proj else "none", "prior_age_min": prior_age_min,
        "tv": {"ids": tv_ids, "current": current, "brand": brand_of(current) if current else None,
               "brand_ids": brand_ids, "earlier_ids": earlier, "category": category},
        "ref": {"source": ref["source"], "ids": ref["ids"], "by_dim": ref["sources"]},
        "mix": {"posts": posts, "per_day": round(posts / params["mix_window_days"], 2),
                "window_days": params["mix_window_days"], "question_share": q, **mix},
        "evidence": evidence,
        "pi": {"content_format": pi_fmt, "title_structure": pi_ttl, "emotional_lever": pi_lev,
               "human_truth_archetype": pi_arc},
        "lift": {"emotional_lever": ll["lift"], "beta": beta, "global": ll["global"], "brand": ll["brand"],
                 "title": tl["lift"], "title_brand": tl["brand"], "title_successes": tl["successes"],
                 "p_question": pq, "p_star": p_star, "band": list(band)},
        "extras": extras,
        "working": (proj or {}).get("working") if mapped else None,
        "fq_shares": (proj or {}).get("fq_shares") if mapped else {},
        "ext": ext_info,
        # 起量帖: 本项目自己的 + 同品牌上一期的(后一期 / 同时在跑的那一期不算, 复审)
        "hits": [h for h in (proj or {}).get("hits") or [] if isinstance(h, dict)
                 and (h.get("own") or h.get("tv_project") in set(earlier))] if mapped else [],
    }


def uniform_profile(project_id: str | None = None, *, reason: str = "") -> dict:
    """什么都没有时的 Profile(快照也坏了, 或者拼的时候出了错): 全均匀、没有提升。自己不依赖 compose —— compose
    出错时也要能回这一份。"""
    uni = {d: _uniform(d) for d in VALUES}
    q = uni["title_structure"][QUESTION]
    p = {
        "v": 1, "project_id": project_id, "built_at": _utcnow().isoformat(timespec="seconds"),
        "stage": "none", "n_res": None, "ps": "uniform", "pp": "none", "prior_age_min": None,
        "tv": {"ids": [], "current": None, "brand": None, "brand_ids": [], "earlier_ids": [], "category": None},
        "ref": {"source": "uniform", "ids": [], "by_dim": {}},
        "mix": {"posts": 0, "per_day": 0.0, "window_days": PARAMS["mix_window_days"], "question_share": q,
                **{d: dict(uni[d]) for d in DIMS}},
        "evidence": {d: {} for d in DIMS},
        "pi": {d: dict(uni[d]) for d in DIMS},
        "lift": {"emotional_lever": {v: 1.0 for v in vocab.EMOTIONAL_LEVERS}, "beta": float(PARAMS["beta"]),
                 "global": {}, "brand": None, "title": {Q: 1.0, NQ: 1.0}, "title_brand": {Q: 1.0, NQ: 1.0},
                 "title_successes": 0.0, "p_question": q, "p_star": q, "band": [q, q]},
        "extras": {"emotional_intensity": dict(uni["emotional_intensity"]),
                   "trend_dependencies": dict(uni["trend_dependencies"])},
        "working": None, "fq_shares": {}, "ext": {"category": None, "state": "none", "ok": False}, "hits": [],
    }
    if reason:
        p["error"] = reason[:200]
    return p


# ══════════════════════════════════════════════════════════════════════
# IO: 三块各自怎么建(读失败一律回 None = 这一次没建成)
# ══════════════════════════════════════════════════════════════════════

def _store():
    from . import store                       # 晚 import: 纯函数(回测、快照脚本的离线模式)不需要 db / supabase
    return store


def fetch_global(sb, *, now: datetime | None = None, params: dict = PARAMS) -> dict | None:
    st = _store()
    notes = st.tv_notes_outcomes(sb)
    if notes is None:
        return None
    answers = st.tv_title_answers(sb)
    if answers is None:
        return None
    meta = st.tv_projects_meta(sb)
    if meta is None:
        return None
    return build_global(notes, answers, meta, now=now, params=params)


def fetch_project(sb, project_id: str, row: dict | None = None, *, now: datetime | None = None,
                  params: dict = PARAMS, on_mapping=None) -> dict | None:
    """项目块 ④⑤。读对照表(写作台这边的库, 便宜)→ 读 TV 的近 60 天笔记和特征题(慢, 可能读不到)。

    on_mapping(复审): 对照表一读到就先交出去({project_id, ext_category, tv_ids, built_at}), 调用方记进缓存的 m:<id> ——
    后面 TV 读失败(回 None)或者慢过发牌的等待预算, 发牌照样知道这个项目对上了哪几个 TV 项目, 按全局表 / 快照里它自己的
    配比发, 不当成「没对上」去按 8 种形式平均发。"""
    st = _store()
    now = now or _utcnow()
    if row is None:
        try:
            row = st.project_row(sb, project_id)
        except Exception as exc:                        # noqa: BLE001 — 只少了外部品类
            logger.warning("priors: project row unreadable (%s): %s", project_id, f"{type(exc).__name__}: {exc}"[:200])
            row = None
    try:
        maps = st.tv_projects_of(sb, project_id)
    except Exception as exc:                            # noqa: BLE001
        logger.warning("priors: tv mapping unreadable (%s): %s", project_id, f"{type(exc).__name__}: {exc}"[:200])
        return None
    tv_ids = sorted({str(m.get("tv_project_id")) for m in maps or () if m.get("tv_project_id")})
    base = {"project_id": project_id, "ext_category": external_category(row), "tv_ids": tv_ids,
            "built_at": now.isoformat(timespec="seconds")}
    if on_mapping is not None:
        try:
            on_mapping(dict(base, tv_ids=list(tv_ids)))
        except Exception:                               # noqa: BLE001 — 记不下对照表不影响建项目块
            logger.exception("priors: remembering the tv mapping failed (%s)", project_id)
    if not tv_ids:
        return base
    meta = st.tv_projects_meta(sb) or {}
    prefixes = {brand_of(p) for p in tv_ids}
    brand_ids = sorted(p for p in meta if p not in tv_ids and brand_of(p) in prefixes)
    rows = st.tv_notes_profile(sb, tv_ids + brand_ids, now - timedelta(days=params["mix_window_days"]))
    if rows is None:
        return None
    tv = set(tv_ids)
    own_ids = [str(r.get("note_id")) for r in rows if str(r.get("project_id")) in tv and r.get("note_id")]
    hit_from = now - timedelta(days=params["hit_brand_age"][1])

    def _brand_recent(r) -> bool:                       # 起量帖候选: 发出去了(排期还没发的不算)、在品牌期的窗口里
        t = published_at(r, now)
        return t is not None and t >= hit_from

    brand_cand = [str(r.get("note_id")) for r in rows
                  if str(r.get("project_id")) not in tv and r.get("note_id") and r.get("comments_count") is not None
                  and _brand_recent(r) and int(r.get("comments_count") or 0) >= params["hit_min_comments"]]
    answers = st.note_answers(sb, own_ids + brand_cand, FQ_QIDS)
    if answers is None:
        logger.warning("priors: fq answers unreadable (%s); title classes fall back to the regex", project_id)
        answers = {}
    agg = project_aggregate(rows, answers, tv_ids=tv_ids, brand_ids=brand_ids, now=now, params=params)
    agg["categories"] = {p: (meta.get(p) or {}).get("category") for p in tv_ids if (meta.get(p) or {}).get("category")}
    base.update(agg)
    return base


def fetch_ext(sb, category: str, *, now: datetime | None = None, params: dict = PARAMS) -> dict | None:
    st = _store()
    now = now or _utcnow()
    rows = st.external_slice(sb, category, days=params["ext_scrape_days"])
    if rows is None:
        return None
    ids = []
    for r in rows:
        posted, fetched = posted_at(r.get("note_id"), now=now), _ts(r.get("fetched_at"))
        if posted and fetched and (params["ext_min_age_days"] <= _days(fetched, posted) <= params["ext_max_age_days"]):
            ids.append(str(r.get("note_id")))
    answers = st.note_answers(sb, ids, EXT_QIDS, subject_type="external_note", run_tag="external")
    if answers is None:
        return None                                     # 读不到 efficacy_promise 就不给外部卡(受监管品类)
    return external_pool(rows, answers, category=category, now=now, params=params)


# ══════════════════════════════════════════════════════════════════════
# 进程内缓存: 单飞 + 两个工作线程
# ══════════════════════════════════════════════════════════════════════

WORKERS = 2


class _Pool:
    """两个 daemon 工作线程 + 一个队列。不用 ThreadPoolExecutor: 它的线程在解释器退出时要被 join, TV 卡住的那次构建会把
    CLI 的退出也拖住; 这里的线程是 daemon, 进程说走就走。"""

    def __init__(self, n: int = WORKERS):
        self.q: queue.Queue = queue.Queue()
        self.threads = []
        self.n = n
        self.lock = threading.Lock()
        self.busy = 0

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """等队列空、手上的活干完(测试收尾用, 免得上一个用例卡住的构建醒来后去调下一个用例的假件)。"""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                if self.busy == 0 and self.q.empty():
                    return True
            time.sleep(0.01)
        return False

    def submit(self, fn, fut: Future) -> None:
        self.q.put((fn, fut))
        with self.lock:
            self.threads = [t for t in self.threads if t.is_alive()]
            while len(self.threads) < self.n:
                t = threading.Thread(target=self._run, name="deskcore-priors", daemon=True)
                t.start()
                self.threads.append(t)

    def _run(self) -> None:
        while True:
            fn, fut = self.q.get()
            with self.lock:
                self.busy += 1
            try:
                if not fut.set_running_or_notify_cancel():
                    continue
                try:
                    fut.set_result(fn())
                except BaseException as exc:            # noqa: BLE001 — 交给 Future, 等的人自己看
                    fut.set_exception(exc)
            finally:
                with self.lock:
                    self.busy -= 1


_LOCK = threading.Lock()
# 键 → {"value", "at"(time.time()), "err_at"}。g 全局表 · p:<id> 项目块 · m:<id> 对照表(项目块构建途中记下的) ·
# x:<品类> 外部块 · 别的模块借用的(project_now 的 d:<id>)
_CACHE: dict[str, dict] = {}
_INFLIGHT: dict[str, Future] = {}
_GEN = [0]
_POOL = [_Pool()]
_NOW = [time.time]                 # 测试改这个, 不碰真时钟


def reset_cache() -> None:
    """清缓存、换一个新的线程池(测试用; 旧池里还在跑的构建写不回来 —— 代号对不上)。"""
    with _LOCK:
        _CACHE.clear()
        _INFLIGHT.clear()
        _GEN[0] += 1
        _POOL[0] = _Pool()
    _SNAPSHOT.clear()


def _ttl(key: str, params: dict) -> float:
    return params["ttl_global_s"] if key == "g" else params["ttl_ext_s"] if key.startswith("x:") else params["ttl_project_s"]


def _entry(key: str) -> dict | None:
    with _LOCK:
        e = _CACHE.get(key)
        return dict(e) if e else None


def _fresh(e: dict | None, key: str, params: dict) -> bool:
    return bool(e and e.get("value") is not None and _NOW[0]() - e["at"] <= _ttl(key, params))


def _usable(e: dict | None, params: dict) -> bool:
    return bool(e and e.get("value") is not None and _NOW[0]() - e["at"] <= params["stale_max_s"])


def _recently_failed(e: dict | None, params: dict) -> bool:
    return bool(e and e.get("err_at") and _NOW[0]() - e["err_at"] < params["retry_after_fail_s"])


def _submit(key: str, fn) -> Future:
    """同一个键同一时刻只建一次: 已经在建就把那个 Future 交回去。建成写缓存, 建不成(回 None / 抛)记失败时间。"""
    with _LOCK:
        f = _INFLIGHT.get(key)
        if f is not None and not f.done():
            return f
        f = Future()
        _INFLIGHT[key] = f
        gen = _GEN[0]
        pool = _POOL[0]

    def run():
        try:
            v = fn()
        except Exception as exc:                        # noqa: BLE001
            logger.warning("priors: building %s failed: %s", key, f"{type(exc).__name__}: {exc}"[:200])
            v = None
        with _LOCK:
            if _GEN[0] == gen:
                e = _CACHE.setdefault(key, {"value": None, "at": 0.0, "err_at": None})
                if v is None:
                    e["err_at"] = _NOW[0]()
                else:
                    e.update(value=v, at=_NOW[0](), err_at=None)
                if _INFLIGHT.get(key) is f:
                    _INFLIGHT.pop(key, None)
        return v

    pool.submit(run, f)
    return f


def _remember(key: str, value, gen: int) -> None:
    """构建途中先记下已经拿到的一块(对照表 m:<id>)。只在缓存代号没变时写: reset_cache 之后, 旧池子里还在跑的构建写不回来。"""
    with _LOCK:
        if _GEN[0] == gen:
            _CACHE[key] = {"value": value, "at": _NOW[0](), "err_at": None}


def _project_builder(sb, pid: str, row, params: dict):
    """p:<pid> 的构建函数: 建项目块, 对照表一读到就记进 m:<pid>(复审, 见 fetch_project)。"""
    gen = _GEN[0]
    return lambda: fetch_project(sb, pid, row, params=params,
                                 on_mapping=lambda v: _remember("m:" + pid, v, gen))


def _need(key: str, fn, params: dict) -> Future | None:
    """这一块不新鲜就投递构建(刚失败过的、手里又没有能用的旧值, 一分钟内不重试), 回要等的 Future。"""
    e = _entry(key)
    if _fresh(e, key, params):
        return None
    if _recently_failed(e, params):
        with _LOCK:
            f = _INFLIGHT.get(key)
        return f if f is not None and not f.done() else None
    return _submit(key, fn)


def _project_ref(project) -> tuple[str, dict | None]:
    if isinstance(project, dict):
        return str(project.get("id") or project.get("project_id") or ""), project
    return str(project or ""), None


def _assemble(pid: str, *, now: datetime, params: dict, ext_cat: str | None = None) -> dict:
    """从缓存(新鲜 / 72 小时内的旧值)拼 Profile; 全局表没有就用快照, 快照也没有就均匀。不发任何请求。"""
    g = _entry("g")
    if _usable(g, params):
        gt = g["value"]
        ps = "live" if _fresh(g, "g", params) else "stale"
        age = int((_NOW[0]() - g["at"]) / 60)
    else:
        gt = load_snapshot()
        if gt is not None:
            ps = "snap"
            b = _ts(gt.get("built_at"))
            age = int(_days(now, b) * 1440) if b else None
        else:
            gt, ps, age = {"schema": SNAPSHOT_SCHEMA, "projects": {}}, "uniform", None
    p = _entry("p:" + pid)
    proj = p["value"] if _usable(p, params) else None
    pp = ("live" if _fresh(p, "p:" + pid, params) else "stale") if proj else "none"
    if proj is None:
        # 项目块没有(冷缓存 + TV 慢 / 读失败, 一分钟内不重试), 对照表在(复审): 按对上的 TV 项目发, 配比从全局表 / 快照拼
        m = _entry("m:" + pid)
        if _usable(m, params) and isinstance(m.get("value"), dict):
            proj, pp = dict(m["value"], mapping_only=True), "table"
    ext = None
    ext_state = None
    cat = ext_cat or (proj or {}).get("ext_category")
    if cat:
        x = _entry("x:" + cat)
        if _usable(x, params):
            ext, ext_state = x["value"], "ok"
        else:
            ext_state = "unavailable" if x and x.get("err_at") else "warming"
    return compose(gt, proj, ext, project_id=pid, now=now, ps=ps, pp=pp, prior_age_min=age,
                   ext_state=ext_state if cat else "none", ext_category=cat, params=params)


def get_profile(sb, project, *, budget_s: float | None = None, now: datetime | None = None,
                params: dict = PARAMS) -> dict:
    """发牌用的 Profile。最多等 ``budget_s`` 秒(默认 2.5)把缺的块建出来, 等不到就用旧的 / 快照 / 均匀, 后台接着建。
    ``project`` 是项目整行(draw_angles 手里那个)或项目 id。**从不抛**。"""
    pid = ""
    try:
        pid, row = _project_ref(project)
        now = now or _utcnow()
        budget = params["budget_s"] if budget_s is None else float(budget_s)
        deadline = time.monotonic() + max(budget, 0.0)
        ext_cat = external_category(row) if row else None
        # 项目块排在最前: 两个工作线程, 它的第一步就是读对照表(便宜), 读到就记下(复审) —— 全局表慢的时候也不耽误
        futs = [f for f in (
            _need("p:" + pid, _project_builder(sb, pid, row, params), params) if pid else None,
            _need("g", lambda: fetch_global(sb, params=params), params),
            _need("x:" + ext_cat, lambda: fetch_ext(sb, ext_cat, params=params), params) if ext_cat else None,
        ) if f is not None]
        if futs:
            _fwait(futs, timeout=max(deadline - time.monotonic(), 0.0))
        if not ext_cat and pid:
            p = _entry("p:" + pid)
            if not _usable(p, params):
                p = _entry("m:" + pid)
            cat = ((p or {}).get("value") or {}).get("ext_category")
            if cat:
                f = _need("x:" + cat, lambda: fetch_ext(sb, cat, params=params), params)
                if f is not None:
                    _fwait([f], timeout=max(deadline - time.monotonic(), 0.0))
                ext_cat = cat
        return _assemble(pid, now=now, params=params, ext_cat=ext_cat)
    except Exception as exc:                            # noqa: BLE001 — 先验出错不能拦发牌
        logger.exception("priors: get_profile failed (project=%s)", pid)
        return uniform_profile(pid or None, reason=f"{type(exc).__name__}: {exc}")


def warm(sb, project_id: str, project: dict | None = None, *, params: dict = PARAMS) -> None:
    """只投递、不等待(build_writing_brief / open_project 用): 接下来那次发牌多半就能直接读缓存。从不抛。"""
    try:
        pid = str(project_id or "")
        if not pid:
            _need("g", lambda: fetch_global(sb, params=params), params)
            return
        row = project if isinstance(project, dict) else None
        cat = external_category(row) if row else None
        fetch_p = _project_builder(sb, pid, row, params)

        def build_p():
            v = fetch_p()
            c = (v or {}).get("ext_category")
            if c:
                _need("x:" + c, lambda: fetch_ext(sb, c, params=params), params)
            return v

        _need("p:" + pid, build_p, params)                  # 项目块先投(同 get_profile: 对照表早一点记下)
        _need("g", lambda: fetch_global(sb, params=params), params)
        if cat:
            _need("x:" + cat, lambda: fetch_ext(sb, cat, params=params), params)
    except Exception:                                   # noqa: BLE001
        logger.exception("priors: warm failed (project=%s)", project_id)


def cached_profile(project_id: str, *, now: datetime | None = None, params: dict = PARAMS) -> dict | None:
    """只读缓存的 Profile: 项目块不在缓存里(或超过 72 小时)回 None。一个请求都不发、不等。从不抛。"""
    try:
        pid = str(project_id or "")
        if not pid or not _usable(_entry("p:" + pid), params):
            return None
        return _assemble(pid, now=now or _utcnow(), params=params)
    except Exception:                                   # noqa: BLE001
        logger.exception("priors: cached_profile failed (project=%s)", project_id)
        return None


def cache_state(project_id: str, *, params: dict = PARAMS) -> str:
    """项目块的状态(简报的 counts.project_now 用): ok 有可用的 / warming 正在建或还没建过 / unavailable 上一次建失败了。"""
    try:
        key = "p:" + str(project_id or "")
        e = _entry(key)
        if _usable(e, params):
            return "ok"
        with _LOCK:
            f = _INFLIGHT.get(key)
        if f is not None and not f.done():
            return "warming"
        return "unavailable" if e and e.get("err_at") else "warming"
    except Exception:                                   # noqa: BLE001
        return "unavailable"


# ── 别的模块借这套缓存(project_now 的写作台计数用) ─────────────────────────────────────
# 同一套单飞 + 两个工作线程 + TTL(键不是 g / x: 开头的按项目块算, 2 小时)+ 72 小时旧值。键别和 g / p: / m: / x: 撞。
def warm_value(key: str, fn, *, params: dict = PARAMS) -> None:
    """只投递、不等: ``key`` 不新鲜就在后台跑 ``fn``(回 None / 抛 = 这一次没建成)。从不抛。"""
    try:
        if key and not key.startswith(("g", "p:", "m:", "x:")):
            _need(key, fn, params)
    except Exception:                                   # noqa: BLE001
        logger.exception("priors: warm_value failed (%s)", key)


def cached_value(key: str, *, params: dict = PARAMS):
    """只读缓存: 72 小时内建成过的值, 没有回 None。不发请求、不等。从不抛。"""
    try:
        e = _entry(key)
        return e["value"] if _usable(e, params) else None
    except Exception:                                   # noqa: BLE001
        return None
