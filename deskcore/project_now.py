"""deskcore/project_now.py — 「这个项目现在什么情况」的三处说法(angle cards v2 §3.3 §3.4)。

  · ``build``       简报里的 project_now 一栏(给模型定写法用): 近 60 天发了什么、什么在起量(只是计数)、写作台近两周
                    写的稿子发出去了几篇、外部同品类有没有参考。只读缓存(priors.cached_profile + 这里的写作台计数),
                    open_project 一秒都不等 —— 缓存是冷的就由 build_writing_brief 投递一次后台预热, 这一次说 warming。
  · ``status_line`` 发牌回包的 project_status(也是 prompt_block 的第二行): 一句话, ≤120 字, 只说项目的配比和这批照什么发。
  · ``batch_note``  发牌回包的 batch_note: 从这一批实际发出去的牌数出来的一句话, 交付时原样说一次。从不提「试新」——
                    试新牌和普通牌对写手、对模型都长得一模一样(§0 的盲法), 这句话只看牌面上的值和参考行。

再加一个 ``evidence_text``: 卡面上「（本项目近60天约九成是这一类）」那个括号。只看这个值在本项目近 60 天里的原始占比
(profile["evidence"]), 不看这张牌是哪种来源抽出来的 —— 同样的值, 括号一字不差。

纯函数(读 Profile 这个普通 dict), 只有 ``desk_counts`` 读库(后台跑, 走 priors 的缓存和工作线程)。说给写手的话里
不出现表名、命令、来源代号。
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import priors, vocab

logger = logging.getLogger("deskcore.project_now")

_CN_TZ = timezone(timedelta(hours=8))
_CN_NUM = {1: "一", 2: "两", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九"}

QUESTION = vocab.TITLE_STRUCTURES[0]            # 「疑问句」
EVIDENCE_MIN = 0.15                             # 卡面括号: 这个值在本项目近 60 天的占比到这么多才说
STATUS_MAX_CHARS = 120
LIFT_CLAUSE_MIN = 1.2                           # 「情绪偏向这个项目里评论多的几类」: 最高的杠杆提升 ≥ 1.2 …
LIFT_CLAUSE_SUCCESSES = 3                       # … 而且那个杠杆本项目自己起量过 ≥ 3 篇
WORKING_MIN_RESOLVED = 20                       # project_now.working: 发出三天以上的够 20 篇才给
WORKING_MIN_CELL = 8                            # 每一格至少 8 篇
WORKING_LEVERS_MAX = 6
DESK_DAYS = 14
DESK_KEY = "d:"                                 # priors 缓存里写作台计数的键前缀

STAGE_NAME = {"run": "running", "warm": "warm", "cold": "cold", "none": "none"}
REF_LABEL = {"brand": "同品牌上一期", "category": "同品类项目", "global": "全部项目", "uniform": "通用配比"}

PROJECT_NOW_TASK = (
    "这是这个项目现在的情况，给你定写法用。默认贴着 mix 写（draw_angles 的卡已经按这个比例发了）；"
    "working 只用来在两种写法之间拿主意，是计数不是结论；用户点名的写法优先于这里的一切。"
    "用户问这个项目现在怎么样时，用一句话说 message；不放进开场。")
WORKING_NOTE = "起量 = 发出三天以上、评论≥20 或标了爆；只是计数"
NO_EXTERNAL_NOTE = "本品类还没有外部参考"
EXTERNAL_UNREAD_NOTE = "同品类的外部参考这次没读到"

STATUS_DEGRADED = "暂时读不到已发数据，这批按通用配比发。"
STATUS_V1 = "这批按通用配比发。"
SHORT_NOTE = "近期这个项目用掉的组合太多，凑不满；分两批写，或固定某一项（pin）再发。"


# ══════════════════════════════════════════════════════════════════════
# 占比怎么说
# ══════════════════════════════════════════════════════════════════════

def share_phrase(s: float, *, approx: bool = True) -> str:
    """0.88 → 「约九成」, 0.66 → 「约三分之二」, 0.5 → 「约一半」, ≥0.95 → 「几乎都」。approx=False 不带「约」。"""
    s = max(float(s or 0.0), 0.0)
    a = "约" if approx else ""
    if s >= 0.95:
        return "几乎都"
    if abs(s - 2 / 3) <= 0.035:
        return f"{a}三分之二"
    if abs(s - 0.5) <= 0.03:
        return f"{a}一半"
    if abs(s - 1 / 3) <= 0.025:
        return f"{a}三分之一"
    n = min(max(int(round(s * 10)), 1), 9)
    return f"{a}{_CN_NUM[n]}成"


def _window(profile) -> int:
    return int(((profile or {}).get("mix") or {}).get("window_days") or priors.PARAMS["mix_window_days"])


def _evidence(profile, dim: str) -> dict:
    ev = ((profile or {}).get("evidence") or {}).get(dim) or {}
    return {k: float(v) for k, v in ev.items() if isinstance(v, (int, float)) and v > 0}


def evidence_text(profile, dim: str, value: str) -> str | None:
    """卡面括号里的那句(§3.1): 只在这个值占本项目近 60 天的 15% 以上时给; 原型里占比最高的那个说「最常写的一种」。
    只看值, 不看来源。情绪杠杆不给(它的括号是浓度; 杠杆是按效果倾斜的, 「本项目常写」会误导)。"""
    ev = _evidence(profile, dim)
    s = ev.get(value, 0.0)
    if s < EVIDENCE_MIN:
        return None
    w = _window(profile)
    if dim == "content_format":
        return f"本项目近{w}天{share_phrase(s)}是这一类"
    if dim == "title_structure":
        return (f"本项目近{w}天{share_phrase(s)}是问句" if value == QUESTION
                else f"本项目近{w}天{share_phrase(s)}用这种")
    if dim == "human_truth_archetype":
        top = max(sorted(ev), key=ev.get)
        return "本项目最常写的一种" if value == top else "本项目常写"
    return None


def card_evidence(profile, dims: dict) -> dict:
    """一张牌三个维度的括号(没有的不放键)。"""
    out = {}
    for dim in ("content_format", "title_structure", "human_truth_archetype"):
        t = evidence_text(profile, dim, (dims or {}).get(dim))
        if t:
            out[dim] = t
    return out


# ══════════════════════════════════════════════════════════════════════
# project_status(发牌回包, prompt_block 第二行)
# ══════════════════════════════════════════════════════════════════════

def _top(d: dict, k: int, *, min_share: float = 0.0) -> list[str]:
    return [v for v in sorted(d, key=lambda v: (-d[v], v)) if d[v] >= min_share][:k]


def _ref_label(profile) -> str:
    return REF_LABEL.get(((profile or {}).get("ref") or {}).get("source") or "uniform", "通用配比")


def _lift_clause(profile) -> bool:
    lift = (((profile or {}).get("lift") or {}).get("emotional_lever") or {})
    by_lever = (((profile or {}).get("working") or {}).get("by_lever") or {})
    if not lift:
        return False
    top = max(sorted(lift), key=lambda v: lift[v])
    cell = by_lever.get(top) or [0, 0]
    try:
        return float(lift[top]) >= LIFT_CLAUSE_MIN and int(cell[1]) >= LIFT_CLAUSE_SUCCESSES
    except (TypeError, ValueError, IndexError):
        return False


def _ext_unread(profile, plan) -> bool:
    ext = (profile or {}).get("ext") or {}
    if not ext.get("category") or (plan or {}).get("ext_ok"):
        return False
    return ext.get("state") in ("unavailable", "warming")


def status_line(profile, plan: dict | None = None) -> str:
    """一句话: 这个项目近 60 天发了什么、这批照什么发(§3.3)。三种: 跑起来了 / 冷启动 / 读不到。≤120 字。"""
    p = profile or {}
    if not p or p.get("error") or p.get("pp") == "none":
        return STATUS_DEGRADED
    stage = p.get("stage") or "none"
    tail = "同品类的外部参考这次没读到。" if _ext_unread(p, plan) else ""
    if stage == "none":
        return _fit("这个项目还没对上已发的笔记，这批先按通用配比发；对上之后按它自己的比例发。", tail)
    if stage == "cold":
        n = int(p.get("n_res") or 0)
        return _fit(f"本项目发出三天以上的还少（{n}篇），这批先按{_ref_label(p)}的比例发；"
                    f"发出去三天后开始用本项目自己的数据。", tail)
    mix = p.get("mix") or {}
    posts = int(mix.get("posts") or 0)
    w = _window(p)
    fmt = _evidence(p, "content_format")
    head = f"近{w}天发了{posts}篇"
    parts = [f"{share_phrase(fmt[v], approx=False)}是{v}" for v in _top(fmt, 2, min_share=0.1)]
    first = head + ("，" + "、".join(parts) if parts else "")
    second = []
    q = _evidence(p, "title_structure").get(QUESTION)
    if q:
        second.append(f"标题{share_phrase(q)}是问句")
    arc = _top(_evidence(p, "human_truth_archetype"), 1)
    if arc:
        second.append(f"最常写{arc[0]}")
    body = first + ("；" + "，".join(second) if second else "")
    lift = "；情绪偏向这个项目里评论多的几类" if _lift_clause(p) else ""
    return _fit(body + lift + "。这批照这个比例发。", tail,
                fallback=first + "。这批照这个比例发。")


def _fit(main: str, tail: str, *, fallback: str | None = None) -> str:
    for cand in (main + tail, main, fallback or main):
        if len(cand) <= STATUS_MAX_CHARS:
            return cand
    return (fallback or main)[:STATUS_MAX_CHARS]


# ══════════════════════════════════════════════════════════════════════
# batch_note(发牌回包; 交付时原样说一次)
# ══════════════════════════════════════════════════════════════════════

_PIN_LABEL = {"content_format": "内容形式", "title_structure": "标题句式", "emotional_lever": "情绪杠杆",
              "human_truth_archetype": "人性原型"}


def batch_note(picked: list[dict], *, pin: dict | None = None) -> str:
    """「这批10篇：9篇提问求助、1篇场景植入，6篇问句标题；1篇参照了本项目近期评论多的一篇的结构。」
    只数牌面上的值和参考行(外部 / 起量帖 / 点名重写), 不提试新, 也不提每张牌是怎么抽出来的。"""
    cards = [a for a in picked or () if isinstance(a, dict) and isinstance(a.get("dims"), dict)]
    if not cards:
        return ""
    order = {v: i for i, v in enumerate(vocab.CONTENT_FORMATS)}
    fmts = Counter(a["dims"].get("content_format") for a in cards)
    ranked = sorted(fmts, key=lambda v: (-fmts[v], order.get(v, 99), str(v)))
    fparts = [f"{fmts[v]}篇{v}" for v in ranked[:3]]
    head = f"这批{len(cards)}篇：" + "、".join(fparts) + ("等" if len(ranked) > 3 else "")
    q = sum(1 for a in cards if a["dims"].get("title_structure") == QUESTION)
    if q:
        head += f"，{q}篇问句标题"
    refs = Counter()
    for a in cards:
        ref = a.get("reference") or {}
        kind = ref.get("kind")
        if kind == "external":
            refs["ext"] += 1
        elif kind == "hit":
            refs["own" if ref.get("own") else "brand"] += 1
        elif kind == "repeat":
            refs["rep"] += 1
    extra = []
    if refs["own"]:
        extra.append(f"{refs['own']}篇参照了本项目近期评论多的帖的结构")
    if refs["brand"]:
        extra.append(f"{refs['brand']}篇参照了同品牌上一期评论多的帖的结构")
    if refs["ext"]:
        extra.append(f"{refs['ext']}篇参照了同品类别家最近涨得快的帖的结构")
    if refs["rep"]:
        extra.append(f"{refs['rep']}篇是点名要再写一次的角度")
    for dim, v in (pin or {}).items():
        if dim in _PIN_LABEL:
            extra.append(f"{_PIN_LABEL[dim]}都按点名的「{v}」")
    return head + ("；" + "；".join(extra) if extra else "") + "。"


# ══════════════════════════════════════════════════════════════════════
# 简报的 project_now
# ══════════════════════════════════════════════════════════════════════

def _as_of(profile) -> str | None:
    t = priors._ts((profile or {}).get("built_at"))
    return t.astimezone(_CN_TZ).strftime("%Y-%m-%d %H:%M") if t else None


def _prior_text(profile) -> str:
    stage = (profile or {}).get("stage") or "none"
    ref = _ref_label(profile)
    if (profile or {}).get("pp") == "none":
        return "暂时读不到本项目已发的数据，按通用配比"
    if stage == "run":
        return f"本项目自己的数据为主，{ref}补充"
    if stage == "warm":
        return f"本项目自己的数据和{ref}各占一部分"
    if stage == "cold":
        return f"本项目数据还少，主要按{ref}"
    return "还没对上已发的笔记，按通用配比"


def _mix_block(profile) -> dict | None:
    mix = (profile or {}).get("mix") or {}
    posts = int(mix.get("posts") or 0)
    if posts <= 0:
        return None
    fmt = _evidence(profile, "content_format")
    ttl = _evidence(profile, "title_structure")
    out = {"posts": posts, "per_day": mix.get("per_day"),
           "content_format": {v: round(fmt[v], 2) for v in _top(fmt, 8, min_share=0.03)}}
    if ttl:
        out["question_title_share"] = round(ttl.get(QUESTION, 0.0), 2)
        out["title_structures_top"] = {v: round(ttl[v], 2) for v in _top(ttl, 3)}
    out["archetypes_top"] = _top(_evidence(profile, "human_truth_archetype"), 2)
    out["levers_top"] = _top(_evidence(profile, "emotional_lever"), 2)
    return out


def _working_block(profile) -> dict | None:
    """起量计数(§3.4): 发出三天以上的够 20 篇才给, 每一格至少 8 篇; 只有篇数和起量数, 不给比率、不给结论。"""
    wk = (profile or {}).get("working") or {}
    resolved = int(wk.get("resolved") or 0)
    if resolved < WORKING_MIN_RESOLVED:
        return None
    by_title = []
    for key, label in ((priors.Q, "问句"), (priors.NQ, "非问句")):
        n, y = (list(wk.get("by_title", {}).get(key) or [0, 0]) + [0, 0])[:2]
        if int(n) >= WORKING_MIN_CELL:
            by_title.append({"title": label, "posts": int(n), "起量": int(y)})
    levers = [(lev, int(c[0]), int(c[1])) for lev, c in (wk.get("by_lever") or {}).items()
              if isinstance(c, (list, tuple)) and len(c) >= 2 and int(c[0]) >= WORKING_MIN_CELL]
    levers.sort(key=lambda t: (-t[1], t[0]))
    by_lever = [{"lever": lev, "posts": n, "起量": y} for lev, n, y in levers[:WORKING_LEVERS_MAX]]
    if not by_title and not by_lever:
        return None
    out = {"resolved": resolved, "note": WORKING_NOTE}
    if by_title:
        out["by_title"] = by_title
    if by_lever:
        out["by_lever"] = by_lever
    return out


def _external_block(ext) -> dict:
    ext = ext or {}
    cat = ext.get("category")
    if not cat:
        return {"category": None, "note": NO_EXTERNAL_NOTE}
    if ext.get("state") in ("unavailable", "warming") or ext.get("eligible") is None:
        return {"category": cat, "note": EXTERNAL_UNREAD_NOTE}
    out = {"category": cat}
    for k in ("updated_days_ago", "recent_14d", "top_structures"):
        if ext.get(k) is not None:
            out[k] = ext[k]
    return out


def _message(profile, desk, *, name: str = "") -> str:
    p = profile or {}
    who = name or "这个项目"
    if p.get("pp") == "none":
        first = f"{who}已发的数据这次没读到"
    elif (p.get("stage") or "none") == "none":
        first = f"{who}还没对上已发的笔记"
    else:
        posts = int((p.get("mix") or {}).get("posts") or 0)
        bits = []
        fmt = _evidence(p, "content_format")
        if fmt:
            top = _top(fmt, 1)[0]
            bits.append(f"{share_phrase(fmt[top], approx=False)}是{top}")
        q = _evidence(p, "title_structure").get(QUESTION)
        if q:
            bits.append(f"{share_phrase(q, approx=False)}是问句标题")
        first = f"{who}近两个月发了{posts}篇" + ("，" + "、".join(bits) if bits else "")
    second = ""
    if desk:
        written, published = int(desk.get("written") or 0), int(desk.get("published") or 0)
        if not int(desk.get("cards_drawn") or 0) and not written:
            second = "写作台近两周还没有发过牌"
        elif not written:
            second = "写作台近两周发了牌、还没有写成的稿子"
        elif not published:
            second = f"写作台近两周写的{written}篇还没有一篇发出去"
        else:
            second = f"写作台近两周写的{written}篇发出去了{published}篇"
    return first + ("；" + second if second else "") + "。"


def build(profile, desk: dict | None = None, external: dict | None = None, *, name: str = "") -> dict | None:
    """简报的 project_now(§3.4)。profile 是 priors.cached_profile 给的(只读缓存); desk 是 cached_desk 给的写作台计数
    (没有就不带 desk 这一键)。external 默认取 profile["ext"]。profile 是 None 回 None。"""
    if not profile:
        return None
    out: dict = {"as_of": _as_of(profile), "stage": STAGE_NAME.get(profile.get("stage") or "none", "none"),
                 "window_days": _window(profile), "prior": _prior_text(profile)}
    mix = _mix_block(profile)
    if mix:
        out["mix"] = mix
    work = _working_block(profile)
    if work:
        out["working"] = work
    if desk:
        out["desk"] = {k: desk.get(k) for k in ("days", "cards_drawn", "written", "published", "title_changed")}
    out["external"] = _external_block(external if external is not None else profile.get("ext"))
    out["message"] = _message(profile, desk, name=name)
    out["task"] = PROJECT_NOW_TASK
    return out


# ══════════════════════════════════════════════════════════════════════
# 写作台近两周的计数(读库; 后台跑, 走 priors 的缓存)
# ══════════════════════════════════════════════════════════════════════

def desk_counts(sb, project_id: str, *, days: int = DESK_DAYS, now: datetime | None = None) -> dict | None:
    """近 ``days`` 天发了几张牌、写成了几篇(销了账的版本)、发出去几篇(tv_note_links 按内容对上的)、几篇标题被改了。
    读不到台账回 None(这一次没建成); 对照表读不到就只给前两个数。只数, 不带标题和正文。"""
    from . import store                                  # 晚 import: 纯函数部分不需要 db
    now = now or datetime.now(timezone.utc)
    rows = store.ledger_window(sb, project_id, days)
    if rows is None:
        return None
    floor = now - timedelta(days=days)
    drawn = 0
    vids: list[str] = []
    for r in rows:
        t = priors._ts(r.get("drawn_at"))
        if t is not None and t >= floor:
            drawn += 1
        c = priors._ts(r.get("consumed_at"))
        if r.get("consumed_version_id") and c is not None and c >= floor:
            vids.append(str(r["consumed_version_id"]))
    vids = list(dict.fromkeys(vids))
    out = {"days": days, "cards_drawn": drawn, "written": len(vids), "published": None, "title_changed": None}
    adoption = store.desk_adoption(sb, vids) if vids else {"published": 0, "title_changed": 0}
    if adoption is not None:
        out["published"] = int(adoption.get("published") or 0)
        out["title_changed"] = int(adoption.get("title_changed") or 0)
    return out


def warm_desk(sb, project_id: str) -> None:
    """只投递不等(build_writing_brief 用)。"""
    pid = str(project_id or "")
    if pid:
        priors.warm_value(DESK_KEY + pid, lambda: desk_counts(sb, pid))


def cached_desk(project_id: str) -> dict | None:
    return priors.cached_value(DESK_KEY + str(project_id or ""))
