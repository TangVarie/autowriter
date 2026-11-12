"""deskcore/postgate.py — 回流闸 v1: 一篇已发的帖能不能拿来学(纯函数, 不碰库、不发请求)。

为什么要有它(2026-11-12)
────────────────────────
发出去的帖大多不是在写作台里写的(近 45 天 912 篇里 775 篇)。tv-sync 当晚把它们抄进指纹库, 之后同一批帖在三处被当成
「可以学」的样子摆给模型: 简报的 working 标题、发牌的起量帖参照、借来的教训卡。三处的筛法各不相同, 承诺功效的、后来在
飞书里删掉的、特征题还没答完的哪一处都没筛。这里把「一篇已发的帖怎么看」收成一处:

  · 一篇帖的用处分三路。**避开**(查重、「最近发过」的标题)只要知道这段文字存在, 不过闸; **计数**(配比、起量、标签)
    照旧按 priors.is_clean, 不过闸; **学**(上面那三处)才看这里的结论。
  · 发出去的帖改不了, 所以闸只分拣、不拦。写作台写的和别处写的走同一个函数: 来源(origin)只是记在旁边的标签,
    【不】参与结论。
  · 没有结论(列还没加、这一行还没判过、读不出来)= 照没有闸时的口径。判过、没放行的才不学 —— 闸坏了不会把简报掏空。

本模块是下面这些的【唯一】出处: 来源 / 结论 / 原因的名字, 检查的先后, 哪个原因挡哪一路。谁写、什么时候写在
core._tv_gate(tv-sync 每晚最后一步); 存在 tv_note_links 的 origin / gate / gate_reasons / gate_at 四列
(migrations/020)。读的一方(写作台的两处、TV 的馆员和灯)只认这里的名字, 不自己再判一遍。

检查的先后(第一个成立的定结论):
  1. withdrawn  飞书里删了 / 挪走了, 而且缺席满 36 小时(core.TV_VANISHED_GRACE; 不满的这晚不判, 留着上一晚的结论)
  2. hold       没有发布时间 / 发布时间在未来 / 正文还是空的(不到 MIN_BODY_CHARS 个字) / 合成数据。都是「还没到齐」;
                发出去的短帖有字就不在此列, 照走下面三步
  3. fail       not_clean(priors.is_clean 说不干净: 投流、铺评工单、投流事件) / risk(tier 风控) / efficacy(TV 存的
                efficacy_promise 答案是「是」)
  4. unsure     efficacy_promise 还没有答案(每晚重判, 答案到了就定)
  5. pass       上面都不成立。意思是「查过的几项里没发现什么」, 不是「值得学」—— 值不值得学(发出三天以上、起量)各处
                自己还有自己的线。

原因是给各路分别开关用的(decisions 第 5 条): 结论一篇一个, 原因可以有几个, 第 3、4 步里成立的全都列上 —— 投了流、
功效题又还没答的帖是 fail, 原因是 not_clean + unsure, 所以教训卡(不看 not_clean)在答案到之前照样不借它。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Iterable, Mapping, NamedTuple

from . import priors

# ── 来源(只是标签) ───────────────────────────────────────────────────────
ORIGIN_DESK = "desk"                  # 写作台的稿发出去的(开头 / 内容 / TV 血缘对上; 标题对上的要正文也像)
ORIGIN_DESK_EDITED = "desk_edited"    # 写作台的稿, 人改得多(对照是 edited)
ORIGIN_COPIED_BACK = "copied_back"    # 对上的那一版是发了之后才存进写作台的(lag_days < 0)
ORIGIN_ELSEWHERE = "elsewhere"        # 别处写的: 对不上, 和发布前写作台里的文字重合不到 ELSEWHERE_MAX
ORIGIN_UNCLEAR = "unclear"            # 判不出: 重合 ≥ ELSEWHERE_MAX 的对不上、还没有重合数的、分不出的、只有标题对上的
ORIGINS = (ORIGIN_DESK, ORIGIN_DESK_EDITED, ORIGIN_COPIED_BACK, ORIGIN_ELSEWHERE, ORIGIN_UNCLEAR)

# 对照的几种(tv_note_links.match_kind): 对上了写作台版本的 / 对不上的。ambiguous 两边都不是, 来源记 unclear。
MATCHED_KINDS = frozenset({"body_exact", "title_exact", "fuzzy", "edited", "tv_lineage"})
MISS_KINDS = frozenset({"ingested", "unmatched"})
# 两条线都是 tvlink 里已经在用的: 0.30 = EDITED_FLOOR(「还是那一篇」的下限), 0.15 = EDITED_FLOOR − EDITED_MARGIN
# (不到它的只是共用产品话术的不同稿子)。写死在这里, 不 import tvlink: 这是存进库里的标签的口径, 不该跟着匹配器的常数漂。
ELSEWHERE_MAX = 0.15
DESK_TITLE_MIN = 0.30

# ── 结论 ─────────────────────────────────────────────────────────────────
GATE_PASS = "pass"
GATE_FAIL = "fail"
GATE_UNSURE = "unsure"
GATE_HOLD = "hold"
GATE_WITHDRAWN = "withdrawn"
GATES = (GATE_PASS, GATE_FAIL, GATE_UNSURE, GATE_HOLD, GATE_WITHDRAWN)
# 判过、没放行的四档(只按结论看; 各路该用下面按原因的 learnable, 这个集合留给只要数个数的地方)
NOT_LEARNABLE = frozenset({GATE_FAIL, GATE_UNSURE, GATE_HOLD, GATE_WITHDRAWN})
# 还会变的两档: 等发布时间 / 正文 / 特征题答案。不管帖多老, 每晚都重判。fail 而原因里还带着 unsure 的也在等答案 ——
# 那是按原因看的, 在 core._tv_gate 定范围的地方。
PENDING_GATES = frozenset({GATE_HOLD, GATE_UNSURE})

# ── 原因(存进 gate_reasons.reasons 的名字) ─────────────────────────────────
REASON_WITHDRAWN = "withdrawn"
REASON_HOLD = "hold"
REASON_NOT_CLEAN = "not_clean"
REASON_RISK = "risk"
REASON_EFFICACY = "efficacy"
REASON_UNSURE = "unsure"
REASONS = (REASON_WITHDRAWN, REASON_HOLD, REASON_NOT_CLEAN, REASON_RISK, REASON_EFFICACY, REASON_UNSURE)
# hold 的细项(存进 gate_reasons.hold; 只给人看, 各路不按它分)
HOLD_NO_PUBLISH_TIME = "no_publish_time"
HOLD_SCHEDULED = "scheduled"
HOLD_SHORT_BODY = "short_body"        # 正文还是空的(名字是最初按「不到 50 个字」起的, 存进库了, 不改)
HOLD_SYNTHETIC = "synthetic"
HOLD_DETAILS = (HOLD_NO_PUBLISH_TIME, HOLD_SCHEDULED, HOLD_SHORT_BODY, HOLD_SYNTHETIC)

# ── 哪个原因挡哪一路(decisions 第 5 条; 一路一个常数, 改口径只改这里一行) ──────────────
# 写作台的两处(简报的 working 标题、发牌的起量帖参照): 六个原因都挡。not_clean 这两处今天就按 priors.is_clean 挡着,
# 这里只是把同一把尺记成一个原因, 行为不变。
BLOCKS_DESK = frozenset(REASONS)
# 教训卡(TV 馆员): 【不】含 not_clean —— 老板的决定 D-133 第 1 条: 投流证据链不动 tier、书架和 L2; 书架借不借投过流的
# 帖是老板的事, 还没定。哪天定了「也不借」, 把 REASON_NOT_CLEAN 加进来就是全部改动。
BLOCKS_CARDS = frozenset(REASONS) - {REASON_NOT_CLEAN}

# 正文(tv-sync 从 raw_content 拆出来、剥了话题的那段)不到这么多字的先放着。1 = 只有空的才放着: hold 是「文字还没到」,
# 每晚重判, 等它到。发出去的短帖(一句问话加话题)正文不会再变长, 按字数放着等于永远放着, 而 hold 两路都挡 —— 最初定的
# 是 50, 生产上两百多篇有字的短帖(几乎都答了「否」, 起量的比例不比长帖低)和它们的十来张教训卡会因此一直学不了
# (2026-11-12 复审)。值不值得学不归这里管: pass 不是质量线, 各处自己有线。
MIN_BODY_CHARS = 1
RISK_TIER = "风控"
EFFICACY_QID = "efficacy_promise"


class Verdict(NamedTuple):
    """一篇帖这一晚的结论。``reasons`` / ``hold`` 都是上面登记的名字, 按登记的顺序。"""
    gate: str
    reasons: tuple[str, ...] = ()
    hold: tuple[str, ...] = ()


def _num(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def origin_overlap(score, prior) -> float | None:
    """对不上的帖拿哪个数定来源: 存着的 score 和「发布前写作台里的文字」的重合(prior)里【小的那个】; 只有一个就用那个,
    都没有回 None。

    两个数量的不是一回事: score(tv-sync 判它对不上那晚记的)把时间窗里的版本都算上, 发了之后才抄回写作台的那版也在里面;
    prior(tvlink.prior_overlap)只算发布那天及之前就在的。来源问的是「帖是不是拿写作台的稿改的」, 抄回来的不算 —— 所以
    prior 小就用 prior。反过来 prior 比 score 大只有一种情形: 帖排期在后面、先被抄进了指纹库, 发布前又有人把它存进了
    写作台 —— 那也是抄回来的, 用 score。"""
    s, p = _num(score), _num(prior)
    if s is None or p is None:
        return s if p is None else p
    return min(s, p)


def origin_of(link: Mapping, prior=None) -> str:
    """一行对照 → 来源。``link`` 要 match_kind / version_id / score / lag_days 四个键(tv_note_links 的行);
    ``prior`` 是这篇帖和发布前写作台里的文字的重合(见 origin_overlap), 没有就只看存着的 score。

    对上了的: 那一版晚于发布(lag_days < 0)是抄回来的; edited 是改得多; 标题对上的要包含度到 DESK_TITLE_MIN 才算
    (只有标题一样、正文不像或没得比的记 unclear); TV 带的血缘指到认不出的版本(没有 version_id)记 unclear。lag 没有的
    (那一版不在对照索引里 / 帖没有发布时间)按没晚于发布算, 同 core._diffable 和 TV 判「接住」的口径。
    对不上的(ingested / unmatched): 重合不到 ELSEWHERE_MAX 是别处写的, 其余(包括还没有重合数的)unclear。"""
    kind = (link or {}).get("match_kind")
    if kind in MATCHED_KINDS:
        if not link.get("version_id"):
            return ORIGIN_UNCLEAR
        lag = _num(link.get("lag_days"))
        if lag is not None and lag < 0:
            return ORIGIN_COPIED_BACK
        if kind == "edited":
            return ORIGIN_DESK_EDITED
        if kind == "title_exact":
            s = _num(link.get("score"))
            return ORIGIN_DESK if s is not None and s >= DESK_TITLE_MIN else ORIGIN_UNCLEAR
        return ORIGIN_DESK
    if kind in MISS_KINDS:
        ov = origin_overlap(link.get("score"), prior)
        return ORIGIN_ELSEWHERE if ov is not None and ov < ELSEWHERE_MAX else ORIGIN_UNCLEAR
    return ORIGIN_UNCLEAR


def classify(note: Mapping, *, vanished: Iterable[str] = frozenset(), boosted: Iterable[str] = frozenset(),
             efficacy: Mapping[str, str] | None = None, now: datetime) -> Verdict:
    """一篇已发的帖 → 结论和原因(本模块开头那五步, 第一个成立的定结论)。

    ``note``: note_id / publish_time / tier / data_quality_flags(truth_vault.notes 的那几列, 没有文字)再加
    ``body_chars`` —— tv-sync 拆出来的正文有几个字。不给(None)就不查正文长短: 调用方读不到正文的那晚不该因此判 hold。
    ``vanished``: 缺席满宽限的 note_id(store.tv_long_vanished_note_ids)。还在宽限里的调用方不该来问 —— 留着上一晚的。
    ``boosted``: 投流事件里的 note_id(priors.paid_boosted), 原样交给 priors.is_clean; 干不干净只问它, 这里不另写一遍。
    ``efficacy``: ``{note_id: "是" | "否"}``(store.note_answers 里 efficacy_promise 那一题); 没有这一篇 = 还没答。
    """
    row = dict(note)
    nid = str(row.get("note_id") or "")
    if nid and nid in (vanished if isinstance(vanished, (set, frozenset)) else frozenset(vanished)):
        return Verdict(GATE_WITHDRAWN, (REASON_WITHDRAWN,))

    hold: list[str] = []
    if priors._ts(row.get("publish_time")) is None:
        hold.append(HOLD_NO_PUBLISH_TIME)
    elif priors.published_at(row, now) is None:
        hold.append(HOLD_SCHEDULED)
    chars = _num(row.get("body_chars"))
    if chars is not None and chars < MIN_BODY_CHARS:
        hold.append(HOLD_SHORT_BODY)
    if priors.is_synthetic(row):
        hold.append(HOLD_SYNTHETIC)
    if hold:
        return Verdict(GATE_HOLD, (REASON_HOLD,), tuple(hold))

    reasons: list[str] = []
    if not priors.is_clean(row, boosted if isinstance(boosted, (set, frozenset)) else frozenset(boosted)):
        reasons.append(REASON_NOT_CLEAN)
    if note.get("tier") == RISK_TIER:
        reasons.append(REASON_RISK)
    answer = priors.answer_bool((efficacy or {}).get(nid))
    if answer is True:
        reasons.append(REASON_EFFICACY)
    failed = bool(reasons)
    if answer is None:
        reasons.append(REASON_UNSURE)
    if failed:
        return Verdict(GATE_FAIL, tuple(reasons))
    if reasons:
        return Verdict(GATE_UNSURE, tuple(reasons))
    return Verdict(GATE_PASS)


# ── 存进库的样子, 和读它的办法 ─────────────────────────────────────────────

def pack_reasons(verdict: Verdict, *, overlap=None, overlap_pending: bool = False, overlap_fp: str | None = None) -> dict:
    """结论 → gate_reasons 那一格: ``{"reasons": [...]}``; hold 的带 ``"hold": [细项]``; 对不上的行量过重合的带
    ``"overlap"``(三位小数)和 ``"overlap_fp"``(量的那一刻帖的文字指纹, tvlink.text_fp 的 16 位: 帖的正文后来改了就认得
    出来, 这个数该重量); 这晚时间用完、重合还没量的带 ``"overlap_pending": true``(量上之后这个键就没了)。
    只有登记过的名字、一个数、一个指纹和一个记号, 没有帖的文字。"""
    out: dict = {"reasons": [r for r in REASONS if r in verdict.reasons]}
    if verdict.hold:
        out["hold"] = [h for h in HOLD_DETAILS if h in verdict.hold]
    ov = _num(overlap)
    if ov is not None:
        out["overlap"] = round(ov, 3)
        if isinstance(overlap_fp, str) and overlap_fp:
            out[OVERLAP_FP] = overlap_fp
    elif overlap_pending:
        out[OVERLAP_PENDING] = True
    return out


def _as_obj(stored):
    if isinstance(stored, str):
        try:
            stored = json.loads(stored)
        except ValueError:
            return None
    return stored


def reasons_of(stored) -> tuple[str, ...]:
    """gate_reasons 那一格 → 原因名。认 dict(``{"reasons": [...]}``)、JSON 字符串、直接一个列表; 读不出回空。"""
    obj = _as_obj(stored)
    if isinstance(obj, Mapping):
        obj = obj.get("reasons")
    if not isinstance(obj, (list, tuple)):
        return ()
    return tuple(str(r) for r in obj if isinstance(r, str) and r)


def stored_overlap(stored) -> float | None:
    """gate_reasons 那一格里记着的重合(没有回 None)。"""
    obj = _as_obj(stored)
    return _num(obj.get("overlap")) if isinstance(obj, Mapping) else None


# 这晚量重合的时间用完了、这一行还没量(codex review on aw#155): 结论照判照存, 来源先记 unclear; 带着这个记号的行
# 不管帖多老、结论定没定, 第二晚都还在范围里 —— 不然 60 天以前的帖第一晚没量上, 之后永远不在范围里, 来源一直是 unclear。
OVERLAP_PENDING = "overlap_pending"


# 记下的重合是对着帖的哪一份文字量的(codex review on aw#155): 补录的帖正文后来在飞书里改了(对照是钉住的, 不重对),
# 沿用旧文字量出来的数就不对了 —— 记着量的那一刻的文字指纹, 正文变了就重量; 只是写作台这边的索引变了不重量。
OVERLAP_FP = "overlap_fp"


def stored_overlap_fp(stored) -> str | None:
    """gate_reasons 里记着的「量重合时帖的文字指纹」(没有回 None)。"""
    obj = _as_obj(stored)
    fp = obj.get(OVERLAP_FP) if isinstance(obj, Mapping) else None
    return fp if isinstance(fp, str) and fp else None


def overlap_pending(stored) -> bool:
    """这一行的重合是不是还欠着没量(gate_reasons 里带着 OVERLAP_PENDING)。"""
    obj = _as_obj(stored)
    return isinstance(obj, Mapping) and obj.get(OVERLAP_PENDING) is True


def learnable(gate, gate_reasons=None, *, blocking: frozenset = BLOCKS_DESK) -> bool:
    """这篇帖这一路能不能学。``gate`` / ``gate_reasons`` 是 tv_note_links 上那两格; ``blocking`` 是这一路挡的原因
    (BLOCKS_DESK / BLOCKS_CARDS)。

    没有结论(None、不认识的值)→ 能(照没有闸时的口径); pass → 能; 其余看原因: 有一个在 ``blocking`` 里就不能。
    判过、没放行, 原因却读不出来或者有不认识的名字 → 不能(宁可少学一篇)。"""
    if gate not in GATES or gate == GATE_PASS:
        return True
    reasons = set(reasons_of(gate_reasons))
    if not reasons or reasons - set(REASONS):
        return False
    return not (reasons & blocking)


def blocked(row: Mapping | None, blocking: frozenset = BLOCKS_DESK) -> bool:
    """一行对照(带 gate / gate_reasons 两个键)在这一路是不是被挡: ``not learnable(...)``。没有这一行 = 没挡。"""
    return bool(row) and not learnable(row.get("gate"), row.get("gate_reasons"), blocking=blocking)
