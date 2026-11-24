"""deskcore/phrase_dryrun.py — 用词清单干跑核验 (D-166 / W-62 ①)。

⚠️ 这是一个**只出计数的干跑**, 不是功能:
  · 纯计算: 只用标准库 + deskcore.fingerprint + deskcore.tvlink。不连库、不写库、不调模型、不联网。
  · 不给写手任何东西: 没有 CLI 入口, 没接进 tools.py / open_project / comments_for / app / 定时任务。
    接线(W-62 后面几步)另开 PR。
  · 以后写的跑数脚本只许打印 ``evaluate_phrase_edits`` 返回的摘要(计数、比例、区间、品类代号、口径版本、过线标记)。
    稿和帖的原文只留在内存里。``extract_candidate_phrases`` / ``select_candidate_phrases`` 挑出来的说法、
    ``classify_draft_sentences`` 返回的句子, 都是客户稿和帖里的原文: 不打印、不落日志、不进仓。
    品类只传代号(CAT_A 这种), 不传项目名、品牌名。

口径(aw#179 review 之后定的):
  1. **挑词和核验用两批不重叠的数据。** ``split_pairs_by_time`` 按发布时间切成早一段(挑词)和晚一段(核验);
     ``select_candidate_phrases`` 记下挑词那批每一对的稿 / 帖指纹和最晚的发布时间; ``evaluate_phrase_edits`` 发现
     核验那批里有挑词用过的稿或帖, 或者有发布时间不晚于挑词那批的对, 直接拒绝(``SplitViolation``), 不出读数。
     同一批挑、同一批核, 用词和删改毫无关系时闸也几乎必过(review 的模拟: 同一批核 30 次里过 14~30 次, 换一批 3~5 次)。
  2. **位置和句长混杂。** 开头、结尾、长句本来就更容易被改。按 ``(position, length_bucket)`` 分层, 每一层**自己的**
     对照句要有 ``MIN_STRATUM_CONTROLS`` 句、来自 ``MIN_STRATUM_CONTROL_DRAFTS`` 篇以上才算对上。对不上的层不拿别层的
     对照凑数、也不退回总体基线, 只记进覆盖率。
  3. **放行看区间, 不看固定差值。** 同一篇稿的几句是同一个人改的, 彼此相关; 所以按篇整篇重抽样(cluster bootstrap)
     给净抬升算区间, 下界要高过 ``MIN_LIFT_CI_LOW``; 有命中的稿要够 ``MIN_HIT_DRAFTS`` 篇(按篇数, 不按句数)。
  4. **两边同一个口径。** 带说法的句子和对照句都看「被删, 或者内容被改」(D-166 原话「带这些说法的句子被删改的比例」)。
     只动了标点的(合句、拆句、换标点, 字一个没变)两边都不算改(``punct_only``)。「说法本身不见了」只作诊断输出, 不放行。
     切句和 tvlink 同一套(``tvlink.sentences``, DIFF_REV=2); tvlink.edit_diff 那边合句照旧算改过, 这里不动它。
  5. **候选说法。** 只算整段都是「稿里多出来的」那几个字(跨到发布帖里还留着的字上的碎片不算); 同一处删改的重叠碎片
     只留排在最前的一个; 跟调用方给的品牌 / 产品叫法(``exclude_terms``)连着重合两个字以上的不要。
"""

from __future__ import annotations

import hashlib
import random
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping

from . import fingerprint as fp
from . import tvlink

DIFF_REV = tvlink.DIFF_REV
SHORT_MAX_CHARS = 14
MEDIUM_MAX_CHARS = 35

# ── 放行门槛。evaluate_phrase_edits 的同名小写参数可以覆盖; 摘要的 thresholds 里回显实际用的值 ──────────────
MIN_STRATUM_CONTROLS = 5          # 一层的对照句(不带说法的句子)至少这么多句才算对上……
MIN_STRATUM_CONTROL_DRAFTS = 3    # ……而且要来自至少这么多篇稿
MIN_HIT_DRAFTS = 5                # 对上的层里有命中的稿至少这么多篇(按篇, 不按句)
MIN_MATCHED_COVERAGE = 0.80       # 命中句里落在对上的层的比例
MIN_LIFT_CI_LOW = 0.0             # 净抬升区间的下界要高过这个数
LIFT_CI_ALPHA = 0.05              # 区间取 [5%, 95%] 分位; 下界就是单侧 95%
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 166              # 固定种子: 同样的输入, 读数可复现
MIN_CATEGORIES_FOR_GO = 2         # 至少这么多个品类过线才算整体过闸

# ── 候选说法 ─────────────────────────────────────────────────────────────────────────────────
MIN_CANDIDATE_DRAFT_DOCS = 5      # 一个说法至少在这么多篇稿里「整段多出来」才进候选; 3 篇时只出现三四次的碎串会靠平滑
                                  # 后的比值排到真说法前面(合成模拟里 10 次挑出的前 10 个几乎全是这种)
CONTEXT_GRAM = 4                  # 判「这几个字在发布帖里还留着」用四字串, 与 tvlink.grams 同长
DEDUP_OVERLAP_SHARE = 0.5         # 超过这个比例的稿里, 和排在前面的说法出自同一处(位置重叠) → 同一处删改的碎片
EXCLUDE_OVERLAP_CHARS = 2         # 跟品牌 / 产品叫法连着重合这么多个字就不要

DEFAULT_TIME_KEY = "publish_time"  # 切时间窗看的字段: TV 笔记的发布时间(tvlink.Note.publish_time)
CHANGED_OUTCOMES = frozenset({"removed", "edited"})
_TERM_SEP = re.compile(r"[\s\-_·/|（）()【】\[\]+&、，,]+")


class SplitViolation(ValueError):
    """核验那批碰到了挑词那批(同一篇稿或帖, 或者发布时间不晚于挑词那批)。消息里只有计数, 没有原文。"""


@dataclass(frozen=True)
class CandidateSet:
    """候选说法 + 挑词那批的来历。``phrases`` 是原文, repr 里不带, 也不要打印。"""

    phrases: tuple[str, ...] = field(repr=False)
    source: str = "manual"                       # selected: select_candidate_phrases 挑的; manual: 人给的清单
    selection_pairs: int = 0
    selected_through: datetime | None = None     # 挑词那批里最晚的发布时间
    draft_keys: frozenset[str] = field(default=frozenset(), repr=False)
    published_keys: frozenset[str] = field(default=frozenset(), repr=False)

    @classmethod
    def manual(cls, phrases: Iterable[str]) -> "CandidateSet":
        """人给的清单(不是从要核验的这批数据里挑的)。从数据里挑的一律走 ``select_candidate_phrases``, 不要绕到这里。"""
        return cls(phrases=_norm_phrases(phrases))


@dataclass(frozen=True)
class PairSplit:
    """``split_pairs_by_time`` 的结果。两批对照对都是原文, repr 里不带。"""

    select: tuple[Mapping[str, Any], ...] = field(repr=False)
    verify: tuple[Mapping[str, Any], ...] = field(repr=False)
    cutoff: datetime | None = None
    without_time: int = 0


def _norm_phrases(phrases: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({p for raw in phrases if (p := fp.normalize(raw))}))


def _negative_lag(pair: Mapping[str, Any]) -> bool:
    lag = pair.get("lag_days")
    if lag is None:
        return False
    try:
        return float(lag) < 0
    except (TypeError, ValueError):
        return False


def _text_key(text: Any) -> str:
    """一篇稿或帖的指纹(剥话题、去标点后的 sha256 前 16 位), 只在内存里比对用; 空文本没有指纹。"""
    n = fp.normalize(tvlink.strip_hashtags(str(text or "")))
    return hashlib.sha256(n.encode("utf-8")).hexdigest()[:16] if n else ""


def _pair_time(pair: Mapping[str, Any], time_key: str) -> datetime | None:
    return tvlink._as_dt(pair.get(time_key))


def sentence_position(idx: int, total: int) -> str:
    """按句子在稿子里的位置分三档：opening / middle / closing。"""
    if idx <= 0:
        return "opening"
    if total > 1 and idx >= total - 1:
        return "closing"
    return "middle"


def length_bucket(norm_len: int) -> str:
    """按归一化字数分三档：short (<15) / medium (15..35) / long (>35)。"""
    if norm_len <= SHORT_MAX_CHARS:
        return "short"
    if norm_len <= MEDIUM_MAX_CHARS:
        return "medium"
    return "long"


def split_pairs_by_time(
    pairs: Iterable[Mapping[str, Any]],
    *,
    cutoff: Any = None,
    select_share: float = 0.5,
    time_key: str = DEFAULT_TIME_KEY,
) -> PairSplit:
    """按发布时间把对照对切成两批: 早于 ``cutoff`` 的拿去挑词, 不早于的拿去核验。

    没给 ``cutoff`` 时取让挑词那批约占 ``select_share`` 的那个时间点。没有时间的对两边都不进、只计数
    (``without_time``) —— 证明不了它在哪一边。
    """
    cut = None
    if cutoff is not None:
        cut = tvlink._as_dt(cutoff)
        if cut is None:
            raise ValueError("cutoff 不是一个时间")
    timed: list[tuple[datetime, Mapping[str, Any]]] = []
    without = 0
    for pair in pairs:
        t = _pair_time(pair, time_key)
        if t is None:
            without += 1
        else:
            timed.append((t, pair))
    if not timed:
        return PairSplit(select=(), verify=(), cutoff=cut, without_time=without)
    if cut is None:
        times = sorted(t for t, _ in timed)
        share = min(max(float(select_share), 0.0), 1.0)
        cut = times[min(len(times) - 1, int(len(times) * share))]
    return PairSplit(
        select=tuple(p for t, p in timed if t < cut),
        verify=tuple(p for t, p in timed if t >= cut),
        cutoff=cut,
        without_time=without,
    )


def _doc_sentences(texts: Iterable[str], min_len: int) -> list[list[str]]:
    docs: list[list[str]] = []
    for raw in texts:
        clean = tvlink.strip_hashtags(raw)
        sents = [ns for s in tvlink.sentences(clean) if len(ns := fp.normalize(s)) >= min_len]
        if sents:
            docs.append(sents)
    return docs


def _doc_freq(docs: list[list[str]], sizes: Iterable[int]) -> Counter[str]:
    sizes = tuple(sizes)
    counts: Counter[str] = Counter()
    for sents in docs:
        seen_in_doc: set[str] = set()
        for sent in sents:
            n_chars = len(sent)
            for size in sizes:
                for i in range(n_chars - size + 1):
                    seen_in_doc.add(sent[i : i + size])
        counts.update(seen_in_doc)
    return counts


def _exclusion_runs(terms: Iterable[str]) -> set[str]:
    """品牌 / 产品叫法 → 它们里面所有连着的 ``EXCLUDE_OVERLAP_CHARS`` 个字(去标点、不分大小写)。一个字的叫法不用。"""
    k = EXCLUDE_OVERLAP_CHARS
    runs: set[str] = set()
    for raw in terms or ():
        for part in _TERM_SEP.split(str(raw or "")):
            t = fp.normalize(part).casefold()
            runs.update(t[i : i + k] for i in range(len(t) - k + 1))
    return runs


def _touches_excluded(phrase: str, runs: set[str]) -> bool:
    k = EXCLUDE_OVERLAP_CHARS
    p = phrase.casefold()
    return any(p[i : i + k] in runs for i in range(len(p) - k + 1))


def extract_candidate_phrases(
    draft_texts: Iterable[str],
    published_texts: Iterable[str],
    *,
    exclude_terms: Iterable[str] = (),
    min_draft_docs: int = MIN_CANDIDATE_DRAFT_DOCS,
    min_len: int = 2,
    max_len: int = 6,
    min_ratio: float = 2.0,
    top_k: int = 50,
) -> list[dict[str, Any]]:
    """从写作台稿 vs 发布帖里按文档频率筛出「稿子常用、发布少用」的候选说法。

    - n-gram 严格在 ``tvlink.sentences`` 句边界内抽取(绝不跨句拼接), 与 ``evaluate_phrase_edits`` 的句内检索同口径。
    - 只认整段都是「稿里多出来的」那几个字: 一处出现里只要碰到一个在发布帖里还留着的四字串(``CONTEXT_GRAM``,
      稿和帖里出现得差不多多), 这一处就不算。删掉「说白了」时, 跨到后面正文上的「白了这款…」这种碎片因此不会进来。
    - 同一处删改的重叠碎片只留一个: 按过度使用比、篇数、长度排好, 超过 ``DEDUP_OVERLAP_SHARE`` 的稿里和排在前面的
      说法位置重叠的, 算同一处删改的碎片, 不要。
    - ``exclude_terms``: 调用方给的品牌 / 产品叫法(例如 ``commentstyle.brand_aliases(项目品牌, 项目名, must_mention_any)``
      再加上产品名)。跟其中任何一个连着重合 ``EXCLUDE_OVERLAP_CHARS`` 个字以上的说法不要。本模块自己拿不到这些名单。

    ⚠️ 返回里的 ``phrase`` 是客户稿里的原文: 只在内存里用、喂给 ``evaluate_phrase_edits``; 不打印、不落日志、不进仓。
    挑词这批不能再拿去核验 —— 走 ``select_candidate_phrases``, 它会把来历记下来, 核验时拦住重叠。
    """
    if min_len < 2 or max_len < min_len or top_k <= 0:
        return []

    draft_docs = _doc_sentences(draft_texts, min_len)
    pub_docs = _doc_sentences(published_texts, min_len)
    if not draft_docs or not pub_docs:
        return []

    sizes = sorted(set(range(min_len, max_len + 1)) | {CONTEXT_GRAM})
    draft_df = _doc_freq(draft_docs, sizes)
    pub_df = _doc_freq(pub_docs, sizes)
    n_draft = len(draft_docs)
    n_pub = len(pub_docs)

    def _ratio(g: str) -> float:
        # Laplace smoothing avoids div-by-zero when a phrase never appears in published docs
        return (draft_df.get(g, 0) / n_draft) / ((pub_df.get(g, 0) + 0.5) / (n_pub + 1.0))

    def _core_windows(sent: str) -> Iterable[tuple[int, int]]:
        n = len(sent)
        kept = [False] * n
        for i in range(n - CONTEXT_GRAM + 1):
            if _ratio(sent[i : i + CONTEXT_GRAM]) < min_ratio:
                for k in range(i, i + CONTEXT_GRAM):
                    kept[k] = True
        pre = [0]
        for flag in kept:
            pre.append(pre[-1] + flag)
        for size in range(min_len, min(max_len, n) + 1):
            for i in range(n - size + 1):
                if pre[i + size] == pre[i]:
                    yield i, i + size

    core_df: Counter[str] = Counter()
    for sents in draft_docs:
        seen_in_doc: set[str] = set()
        for sent in sents:
            seen_in_doc.update(sent[i:j] for i, j in _core_windows(sent))
        core_df.update(seen_in_doc)

    runs = _exclusion_runs(exclude_terms)
    pool: list[tuple[float, dict[str, Any]]] = []
    for phrase, c_cnt in core_df.items():
        if c_cnt < min_draft_docs:
            continue
        ratio = _ratio(phrase)
        if ratio < min_ratio:
            continue
        p_cnt = pub_df.get(phrase, 0)
        pool.append(
            (
                ratio,
                bool(runs) and _touches_excluded(phrase, runs),
                {
                    "phrase": phrase,
                    "draft_docs": draft_df[phrase],
                    "core_docs": c_cnt,
                    "published_docs": p_cnt,
                    "draft_rate": round(draft_df[phrase] / n_draft, 4),
                    "published_rate": round(p_cnt / n_pub, 4),
                    "overuse_ratio": round(ratio, 3),
                },
            )
        )
    pool.sort(key=lambda rc: (-rc[0], -rc[2]["core_docs"], -len(rc[2]["phrase"]), rc[2]["phrase"]))

    # 每个候选说法在每篇稿里的出现位置(只算整段多出来的那几处), 用来认「同一处删改的碎片」。
    wanted = {c["phrase"] for _, _, c in pool}
    spans: dict[str, dict[int, list[tuple[int, int, int]]]] = {p: {} for p in wanted}
    for di, sents in enumerate(draft_docs):
        for si, sent in enumerate(sents):
            for i, j in _core_windows(sent):
                if sent[i:j] in wanted:
                    spans[sent[i:j]].setdefault(di, []).append((si, i, j))

    # 碰到品牌 / 产品叫法的说法不出, 但照样占住它的位置: 同一处删改里只差一个字没碰到叫法的碎片(「我用S」这种)
    # 也跟着当成同一处的碎片去掉。
    claimed: dict[int, list[tuple[int, int, int]]] = {}
    out: list[dict[str, Any]] = []
    for _, excluded, cand in pool:
        occ = spans[cand["phrase"]]
        overlapped = sum(
            1
            for di, mine in occ.items()
            if any(a[0] == b[0] and a[1] < b[2] and b[1] < a[2] for a in mine for b in claimed.get(di, ()))
        )
        if occ and overlapped / len(occ) > DEDUP_OVERLAP_SHARE:
            continue
        for di, mine in occ.items():
            claimed.setdefault(di, []).extend(mine)
        if excluded:
            continue
        out.append(cand)
        if len(out) >= top_k:
            break
    return out


def select_candidate_phrases(
    select_pairs: Iterable[Mapping[str, Any]],
    *,
    exclude_terms: Iterable[str] = (),
    time_key: str = DEFAULT_TIME_KEY,
    **kwargs: Any,
) -> CandidateSet:
    """在挑词那批(``split_pairs_by_time(...).select``)上挑候选说法, 并记下这批的来历。

    返回的 ``CandidateSet`` 带着这批每一对的稿 / 帖指纹和最晚发布时间; 拿它去 ``evaluate_phrase_edits`` 时,
    核验那批碰到这批的会被拒绝。其余参数原样传给 ``extract_candidate_phrases``。``lag_days < 0`` 的对不用。
    """
    rows = [p for p in select_pairs if not _negative_lag(p)]
    details = extract_candidate_phrases(
        [str(p.get("draft_body") or "") for p in rows],
        [str(p.get("published_body") or "") for p in rows],
        exclude_terms=exclude_terms,
        **kwargs,
    )
    times = [t for p in rows if (t := _pair_time(p, time_key)) is not None]
    return CandidateSet(
        phrases=tuple(d["phrase"] for d in details),
        source="selected",
        selection_pairs=len(rows),
        selected_through=max(times) if times else None,
        draft_keys=frozenset(k for p in rows if (k := _text_key(p.get("draft_body")))),
        published_keys=frozenset(k for p in rows if (k := _text_key(p.get("published_body")))),
    )


def _anchor_span(ns: str, frag_grams: set[str]) -> tuple[int, int] | None:
    """Return the (start, end) character span of shared 4-grams of a fragment inside ``ns``."""
    lo_vals: list[int] = []
    hi_vals: list[int] = []
    for gm in frag_grams:
        lo = ns.find(gm)
        if lo != -1:
            lo_vals.append(lo)
            hi_vals.append(ns.rfind(gm) + 4)
    if not lo_vals:
        return None
    return min(lo_vals), max(hi_vals)


def _matched_chars(a: str, b: str) -> int:
    if not a or not b:
        return 0
    return sum(blk.size for blk in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks())


def _partition_merged_fragment(
    pn: str,
    q: set[str],
    owners: set[int],
    d_norms: list[str],
    d_grams: list[set[str]],
) -> dict[int, tuple[str, bool, bool]]:
    """Slice a merged published fragment across its distinct draft-sentence owners so
    a phrase retained in one merged sentence cannot mask its removal from another.
    Returns ``{owner_idx: (slice_text, at_left_edge, at_right_edge)}``."""
    # Group by distinct normalized draft sentence (duplicate draft sentences share a rep).
    by_norm: dict[str, list[int]] = {}
    for k in sorted(owners):
        by_norm.setdefault(d_norms[k], []).append(k)
    if len(by_norm) <= 1:
        return {k: (pn, True, True) for k in owners}

    reps = [idxs[0] for idxs in by_norm.values()]
    positioned: list[tuple[int, int, int, int, int]] = []
    unpositioned: list[int] = []
    for k in reps:
        other_grams = set().union(*(d_grams[o] for o in reps if o != k))
        shared = d_grams[k] & q
        anchor_g = (shared - other_grams) or shared
        c_span = _anchor_span(pn, anchor_g)
        d_span = _anchor_span(d_norms[k], anchor_g)
        if c_span is None or d_span is None:
            unpositioned.append(k)
        else:
            positioned.append((c_span[0], c_span[1], d_span[0], d_span[1], k))

    if len(positioned) <= 1:
        return {k: (pn, True, True) for k in owners}

    positioned.sort(key=lambda t: (t[0], t[1]))
    cuts: list[int] = [0]
    for i in range(len(positioned) - 1):
        p_clo, p_chi, _p_dlo, p_dhi, p_k = positioned[i]
        n_clo, n_chi, n_dlo, _n_dhi, n_k = positioned[i + 1]
        if p_chi <= n_clo:
            prev_tail = d_norms[p_k][p_dhi:]
            nxt_head = d_norms[n_k][:n_dlo]
            best_cut = p_chi
            best_score = -1
            for cand in range(p_chi, n_clo + 1):
                score = _matched_chars(prev_tail, pn[p_chi:cand]) + _matched_chars(
                    nxt_head, pn[cand:n_clo]
                )
                if score > best_score:
                    best_score = score
                    best_cut = cand
            cut = best_cut
        else:
            # Overlapping unique cores (e.g., shared phrase collapsed once at merge boundary):
            # cut at an owner's full match edge rather than chopping the shared phrase in half.
            prev_full_end = min(len(pn), p_chi + max(0, len(d_norms[p_k]) - p_dhi))
            nxt_full_start = max(0, n_clo - n_dlo)
            if prev_full_end <= n_chi:
                cut = prev_full_end
            else:
                cut = max(p_clo, nxt_full_start)
        cuts.append(max(cuts[-1], min(len(pn), cut)))
    cuts.append(len(pn))

    out: dict[int, tuple[str, bool, bool]] = {}
    last_pos = len(positioned) - 1
    for pos_idx, (_, _, _, _, rep_k) in enumerate(positioned):
        piece = pn[cuts[pos_idx] : cuts[pos_idx + 1]]
        entry = (piece, pos_idx == 0, pos_idx == last_pos)
        for k in by_norm[d_norms[rep_k]]:
            out[k] = entry
    for rep_k in unpositioned:
        for k in by_norm[d_norms[rep_k]]:
            out[k] = (pn, True, True)
    return out


def _join_contiguous_runs(
    indices: set[int], frag_texts: list[str]
) -> tuple[str, ...]:
    """Join only contiguous published fragment indices into runs so phrases spanning a
    punctuation split boundary stay matchable without concatenating non-adjacent fragments."""
    if not indices:
        return ()
    ordered = sorted(indices)
    runs: list[str] = []
    buf = [frag_texts[ordered[0]]]
    prev = ordered[0]
    for idx in ordered[1:]:
        if idx == prev + 1:
            buf.append(frag_texts[idx])
        else:
            runs.append("".join(buf))
            buf = [frag_texts[idx]]
        prev = idx
    runs.append("".join(buf))
    return tuple(runs)


def _normalize_with_cuts(text: str) -> tuple[str, set[int]]:
    """去标点后的串, 以及原文里在哪些位置(按去标点后的下标)有过标点 / 空白; 头尾也算。"""
    chars: list[str] = []
    cuts = {0}
    for ch in text or "":
        if fp._is_droppable(ch):
            cuts.add(len(chars))
        else:
            chars.append(ch)
    cuts.add(len(chars))
    return "".join(chars), cuts


def _punct_only_flags(d_norms: list[str], clean_pub: str) -> list[bool]:
    """每一句是不是「只动了标点」: 把稿里各句(去标点)按序接起来, 和去标点的发布帖整篇对齐(SequenceMatcher)。

    - 一句里有字被删或被换 → 不是。同一个说法在合句处只留了一份时, 对齐把删掉的那几个字记到哪一句, 哪一句就算改过。
    - 发布帖多出来的字落在一句中间 → 这一句不是。落在两句交界处时看发布帖的标点: 后面紧跟标点(「…放风筝呢。」)
      算前一句加的; 前面紧跟标点算后一句加的; 前后都有标点是单独加的一句, 两边都不算; 前后都没有, 两边都算改过。
    只调了句序的句子(不是原样一句、又被合 / 拆过)会被算成改过 —— 宁可多算改, 不少算。
    """
    n = len(d_norms)
    flags = [True] * n
    if not n:
        return flags
    norm_pub, cuts = _normalize_with_cuts(clean_pub)
    starts: list[int] = []
    pos = 0
    for ns in d_norms:
        starts.append(pos)
        pos += len(ns)
    joined = "".join(d_norms)

    def _owner(i: int) -> int:
        lo, hi = 0, n - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts[mid] <= i:
                lo = mid
            else:
                hi = mid - 1
        return lo

    for tag, i1, i2, j1, j2 in SequenceMatcher(None, joined, norm_pub, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if i2 > i1:
            for k in range(_owner(i1), _owner(i2 - 1) + 1):
                flags[k] = False
            continue
        nxt = _owner(i1) if i1 < len(joined) else None
        if nxt is not None and starts[nxt] != i1:
            flags[nxt] = False
            continue
        prev = (nxt - 1 if nxt is not None else n - 1) if i1 > 0 else None
        left_cut, right_cut = j1 in cuts, j2 in cuts
        if left_cut and right_cut:
            continue
        if prev is not None and not left_cut:
            flags[prev] = False
        if nxt is not None and not right_cut:
            flags[nxt] = False
    return flags


def classify_draft_sentences(draft_body: str, published_body: str) -> list[dict[str, Any]]:
    """把一篇稿子的每一句判成 unchanged / punct_only / removed / edited。

    切句、「还在不在」都按 tvlink (DIFF_REV=2) 口径。``punct_only`` 是本模块多出来的一档: 只合了句、拆了句或换了
    标点, 这一句的字一个没变, 不算改(见 ``_punct_only_flags``)。

    返回列表保留句序、分层键 ``(position, length_bucket)``，以及改写句在发布稿里对应的
    全部重合片段 ``target_norms``：
      - 按 ``tvlink.SENT_ALIGN`` 做排他性归属，防止两句共用同一短语时互相借词掩盖删除；
      - 多句合并成一句时按各原句在发布句里的位置切分归属区段，防止合并后另一句保留的词掩盖本句删词；
      - 紧邻且方向一致的短片段（≤4 字、无法构成多个四字串）归回原句；
      - 相邻归属片段按原序合并为连续片段，避免标点正好拆在候选词中间时误判为删词。

    ⚠️ 返回的 ``norm_sentence`` / ``target_norms`` 就是稿和帖的原文(去了标点): 只在内存里用, 不打印、不落日志、不进仓。
    """
    clean_draft = tvlink.strip_hashtags(draft_body)
    clean_pub = tvlink.strip_hashtags(published_body)
    dsents = tvlink.sentences(clean_draft)
    if not dsents:
        return []
    psents = tvlink.sentences(clean_pub)
    pg = tvlink.grams(clean_pub)
    d_norms = [fp.normalize(s) for s in dsents]
    d_grams = [tvlink.grams(s) for s in dsents]
    exact_draft = set(d_norms)
    exact_pub = {fp.normalize(p) for p in psents}
    punct_only = _punct_only_flags(d_norms, clean_pub)
    # Retain all punctuation-delimited published fragments (including short <6-char splits
    # such as a standalone 3-char opener) so split-sentence retention is detected accurately.
    pub_frags = [
        (pn, tvlink.grams(raw))
        for raw in tvlink._SENT_SPLIT.split(clean_pub)
        if (pn := fp.normalize(raw))
    ]

    # Precompute exclusive fragment ownership across all draft sentences so an unrelated
    # published sentence sharing only a 4-char candidate phrase cannot be borrowed.
    n_draft = len(dsents)
    n_pub = len(pub_frags)
    ov_by_frag: list[dict[int, int]] = []
    best_owners: list[set[int]] = []
    strong_owners: list[set[int]] = [set() for _ in range(n_pub)]

    for j, (pn, q) in enumerate(pub_frags):
        ov: dict[int, int] = {}
        if q:
            for k in range(n_draft):
                cnt = len(d_grams[k] & q)
                if cnt:
                    ov[k] = cnt
        ov_by_frag.append(ov)
        max_ov = max(ov.values(), default=0)
        owners = {k for k, v in ov.items() if v == max_ov} if max_ov > 0 else set()
        best_owners.append(owners)
        distinct_owner_norms = {d_norms[k] for k in owners}
        # Split / 1-to-1 fragment ownership: fragment has >=2 4-grams (>=5 chars), at least
        # SENT_ALIGN of its 4-grams come from its unambiguous strongest draft owner.
        if len(q) >= 2 and len(distinct_owner_norms) == 1 and max_ov / len(q) >= tvlink.SENT_ALIGN:
            for k in owners:
                strong_owners[j].add(k)
        # Merge / 1-to-1 with additions: at least SENT_ALIGN of draft sentence k lives in frag j.
        for k, cnt in ov.items():
            if d_grams[k] and cnt / len(d_grams[k]) >= tvlink.SENT_ALIGN:
                strong_owners[j].add(k)

    # For any published fragment owned by multiple distinct draft sentences (a merged sentence),
    # partition the fragment text so each owner only checks its own corresponding slice.
    merged_slices: dict[int, dict[int, tuple[str, bool, bool]]] = {}
    for j, (pn, q) in enumerate(pub_frags):
        if len({d_norms[k] for k in strong_owners[j]}) > 1:
            merged_slices[j] = _partition_merged_fragment(pn, q, strong_owners[j], d_norms, d_grams)

    out: list[dict[str, Any]] = []
    for idx, sent in enumerate(dsents):
        ns = d_norms[idx]
        g = d_grams[idx]
        stratum = (sentence_position(idx, n_draft), length_bucket(len(ns)))
        if not tvlink._kept(sent, pg):
            outcome = "removed"
            target_norms: tuple[str, ...] = ()
        elif ns in exact_pub:
            outcome = "unchanged"
            target_norms = (ns,)
        elif punct_only[idx]:
            outcome = "punct_only"
            target_norms = (ns,)
        else:
            outcome = "edited"
            if pub_frags:
                matched: set[int] = set()
                spans: dict[int, tuple[int, int]] = {}
                for j in range(n_pub):
                    if idx not in strong_owners[j]:
                        continue
                    pn, q = pub_frags[j]
                    # Skip a fragment that is a verbatim copy of another draft sentence unless
                    # this sentence itself has >= SENT_ALIGN of its 4-grams inside it.
                    if pn in exact_draft and pn != ns:
                        if not (g and ov_by_frag[j].get(idx, 0) / len(g) >= tvlink.SENT_ALIGN):
                            continue
                    matched.add(j)
                    slice_text = merged_slices[j][idx][0] if j in merged_slices else pn
                    slice_q = tvlink.grams(slice_text) if j in merged_slices else q
                    sp = _anchor_span(ns, g & slice_q)
                    if sp is not None:
                        spans[j] = sp

                selected = set(matched)
                if selected:
                    # Expand directionally across contiguous short (<=4 chars) or tied/expanded
                    # split fragments not strongly owned by a different draft sentence.
                    frontier = list(selected)
                    while frontier:
                        cur = frontier.pop()
                        cur_span = spans.get(cur)
                        if cur_span is None:
                            continue
                        cur_lo, cur_hi = cur_span
                        at_left, at_right = (
                            (merged_slices[cur][idx][1], merged_slices[cur][idx][2])
                            if cur in merged_slices and idx in merged_slices[cur]
                            else (True, True)
                        )
                        for direction, nb in ((-1, cur - 1), (1, cur + 1)):
                            if (direction < 0 and not at_left) or (direction > 0 and not at_right):
                                continue
                            if not (0 <= nb < n_pub) or nb in selected:
                                continue
                            if any(d_norms[o] != ns for o in strong_owners[nb]):
                                continue
                            nb_norm, nb_q = pub_frags[nb]
                            if len(nb_norm) <= 4:
                                if direction < 0:
                                    pos = ns.rfind(nb_norm, 0, cur_lo)
                                else:
                                    pos = ns.find(nb_norm, cur_hi)
                                if pos != -1:
                                    selected.add(nb)
                                    spans[nb] = (pos, pos + len(nb_norm))
                                    frontier.append(nb)
                            elif idx in best_owners[nb] and ov_by_frag[nb].get(idx, 0) >= 2:
                                nb_span = _anchor_span(ns, g & nb_q)
                                if nb_span is not None and (
                                    (direction < 0 and nb_span[1] <= cur_lo + 3)
                                    or (direction > 0 and nb_span[0] >= cur_hi - 3)
                                ):
                                    selected.add(nb)
                                    spans[nb] = nb_span
                                    frontier.append(nb)
                    owner_texts = [
                        merged_slices[j][idx][0] if j in merged_slices and idx in merged_slices[j] else pub_frags[j][0]
                        for j in range(n_pub)
                    ]
                    target_norms = _join_contiguous_runs(selected, owner_texts)
                else:
                    # Fallback when a sentence was split into tiny (<=4-char) pieces so no
                    # individual fragment met SENT_ALIGN: among contiguous runs of fragments not
                    # strongly owned by a different draft sentence, pick the qualifying window
                    # maximizing order-preserving matched characters (>=2-char blocks) and
                    # breaking ties by shortest length so trailing unrelated fragments are not
                    # pulled in.
                    best_window: tuple[str, ...] = ()
                    best_score: tuple[int, int] = (-1, 0)
                    max_win = min(n_pub, max(12, len(ns)))
                    for start in range(n_pub):
                        if any(d_norms[o] != ns for o in strong_owners[start]):
                            continue
                        parts: list[str] = []
                        for end in range(start, min(n_pub, start + max_win)):
                            if any(d_norms[o] != ns for o in strong_owners[end]):
                                break
                            parts.append(pub_frags[end][0])
                            win_len = end - start + 1
                            if win_len < 2:
                                continue
                            joined = "".join(parts)
                            if not tvlink._kept(sent, tvlink.grams(joined)):
                                continue
                            matched_len = sum(
                                blk.size
                                for blk in SequenceMatcher(None, ns, joined, autojunk=False).get_matching_blocks()
                                if blk.size >= 2
                            )
                            score = (matched_len, -win_len)
                            if score > best_score:
                                best_score = score
                                best_window = (joined,)
                    target_norms = best_window
            else:
                target_norms = ()
        out.append(
            {
                "index": idx,
                "stratum": stratum,
                "norm_sentence": ns,
                "outcome": outcome,
                "target_norms": target_norms,
            }
        )
    return out


def _new_bucket() -> dict[str, Any]:
    return {
        "pairs": 0,
        "total_sentences": 0,
        "phrase_hits": 0,
        "phrase_sentence_changed": 0,
        "phrase_punct_only": 0,
        "phrase_word_removed": 0,
        "phrase_word_kept_on_edit": 0,
        "non_phrase_sentences": 0,
        "non_phrase_changed": 0,
        "non_phrase_punct_only": 0,
        # 稿的指纹 → {分层键: [命中句, 命中句被删改, 命中句里说法不见了, 对照句, 对照句被删改]}
        "clusters": {},
    }


def _lift(per_stratum: Iterable[list[int]], idx: int) -> tuple[float, float] | None:
    """同层加权: 命中句的比率 − 按命中句数加权的同层对照被删改率。回 (净抬升, 加权基线); 没有可比的层回 None。"""
    hits = num = base = 0.0
    for t in per_stratum:
        if t[0] and t[3]:
            hits += t[0]
            num += t[idx]
            base += t[0] * t[4] / t[3]
    if not hits:
        return None
    return (num - base) / hits, base / hits


def _quantile(sorted_vals: list[float], q: float) -> float:
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _cluster_bootstrap(
    clusters: list[list[list[int]]], *, resamples: int, alpha: float, seed: int
) -> tuple[float, float] | None:
    """按篇整篇重抽样: 每次有放回地抽同样多篇稿, 篇里的句子一起进出, 算一次同层净抬升。回 (alpha, 1−alpha) 分位。

    一篇稿里的几句是同一个人改的, 按句子独立抽会把样本量算多; 只有一篇稿时没有区间。"""
    n = len(clusters)
    if n < 2 or resamples <= 0:
        return None
    n_strata = len(clusters[0])
    rng = random.Random(seed)
    idx = range(n)
    stats: list[float] = []
    for _ in range(resamples):
        tot = [[0, 0, 0, 0, 0] for _ in range(n_strata)]
        for k in rng.choices(idx, k=n):
            for s, t in enumerate(clusters[k]):
                acc = tot[s]
                acc[0] += t[0]
                acc[1] += t[1]
                acc[3] += t[3]
                acc[4] += t[4]
        res = _lift(tot, 1)
        if res is not None:
            stats.append(res[0])
    if len(stats) * 2 < resamples:
        return None
    stats.sort()
    return _quantile(stats, alpha), _quantile(stats, 1.0 - alpha)


def _r4(v: float | None) -> float | None:
    return round(v, 4) if v is not None else None


def evaluate_phrase_edits(
    pairs: Iterable[Mapping[str, Any]],
    candidates: CandidateSet,
    *,
    time_key: str = DEFAULT_TIME_KEY,
    min_stratum_controls: int = MIN_STRATUM_CONTROLS,
    min_stratum_control_drafts: int = MIN_STRATUM_CONTROL_DRAFTS,
    min_hit_drafts: int = MIN_HIT_DRAFTS,
    min_matched_coverage: float = MIN_MATCHED_COVERAGE,
    min_lift_ci_low: float = MIN_LIFT_CI_LOW,
    ci_alpha: float = LIFT_CI_ALPHA,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
    min_categories_for_go: int = MIN_CATEGORIES_FOR_GO,
) -> dict[str, Any]:
    """在核验那批上按品类做分层对照干跑, 返回**纯计数、比例与区间**的摘要(不含任何原句、标题、正文或候选说法原文)。

    ``pairs``: 核验那批(``split_pairs_by_time(...).verify``), 每项含 ``category``(品类代号) / ``draft_body`` /
    ``published_body`` / ``time_key`` 指的发布时间; ``lag_days < 0`` 的对跳过(DIFF_REV=2)。
    ``candidates``: ``select_candidate_phrases`` 在挑词那批上挑的, 或 ``CandidateSet.manual`` 的人工清单。
    核验那批里有挑词用过的稿或帖, 或者(挑词那批有时间时)有没有时间、不晚于挑词那批的对: 抛 ``SplitViolation``。

    放行只看 ``sentence_changed_lift``(带说法的句子被删或内容被改的比例 − 同层对照的同一比例), 要同时满足:
    对上的层里有命中的稿 ≥ ``min_hit_drafts`` 篇、匹配覆盖率 ≥ ``min_matched_coverage``、按篇重抽样的净抬升区间下界
    > ``min_lift_ci_low``。``word_removal_lift`` 一类「说法本身不见了」的数只作诊断。
    """
    if not isinstance(candidates, CandidateSet):
        raise TypeError(
            "candidates 要是 CandidateSet: 从数据里挑的走 select_candidate_phrases, 人给的清单用 CandidateSet.manual"
        )
    rows: list[Mapping[str, Any]] = []
    skipped_negative_lag = 0
    for pair in pairs:
        if _negative_lag(pair):
            skipped_negative_lag += 1
        else:
            rows.append(pair)

    reused = sum(
        1
        for p in rows
        if _text_key(p.get("draft_body")) in candidates.draft_keys
        or _text_key(p.get("published_body")) in candidates.published_keys
    )
    if reused:
        raise SplitViolation(
            f"核验那批里有 {reused} 对的稿或帖在挑词那批里用过: 挑词和核验要用两批不重叠的数据(split_pairs_by_time)"
        )
    if candidates.selected_through is not None:
        not_later = sum(
            1 for p in rows if (t := _pair_time(p, time_key)) is None or t <= candidates.selected_through
        )
        if not_later:
            raise SplitViolation(
                f"核验那批里有 {not_later} 对没有发布时间、或不晚于挑词那批: 核验要用挑词窗口之后的数据"
            )

    eff_min_controls = max(1, int(min_stratum_controls))
    eff_min_control_drafts = max(1, int(min_stratum_control_drafts))
    eff_min_hit_drafts = max(1, int(min_hit_drafts))
    eff_min_cov = max(0.0, min(1.0, float(min_matched_coverage)))
    eff_alpha = max(0.0, min(0.5, float(ci_alpha)))
    eff_min_cats = max(1, int(min_categories_for_go))
    phrases = _norm_phrases(candidates.phrases)
    by_cat: dict[str, dict[str, Any]] = {}

    for pair in rows:
        cat = str(pair.get("category") or "未分类").strip() or "未分类"
        b = by_cat.setdefault(cat, _new_bucket())
        b["pairs"] += 1
        draft = str(pair.get("draft_body") or "")
        classified = classify_draft_sentences(draft, str(pair.get("published_body") or ""))
        if not classified:
            continue
        tallies = b["clusters"].setdefault(_text_key(draft), {})
        for item in classified:
            b["total_sentences"] += 1
            ns = item["norm_sentence"]
            outcome = item["outcome"]
            changed = outcome in CHANGED_OUTCOMES
            t = tallies.setdefault(item["stratum"], [0, 0, 0, 0, 0])
            matched = [np for np in phrases if np in ns]
            if matched:
                b["phrase_hits"] += 1
                t[0] += 1
                if outcome == "punct_only":
                    b["phrase_punct_only"] += 1
                if changed:
                    b["phrase_sentence_changed"] += 1
                    t[1] += 1
                    if not all(any(np in frag for frag in item["target_norms"]) for np in matched):
                        b["phrase_word_removed"] += 1
                        t[2] += 1
                    else:
                        b["phrase_word_kept_on_edit"] += 1
            else:
                b["non_phrase_sentences"] += 1
                t[3] += 1
                if outcome == "punct_only":
                    b["non_phrase_punct_only"] += 1
                if changed:
                    b["non_phrase_changed"] += 1
                    t[4] += 1

    categories_out: dict[str, dict[str, Any]] = {}
    passing_categories = 0
    for cat in sorted(by_cat):
        b = by_cat[cat]
        strata: dict[tuple[str, str], list[int]] = {}
        control_drafts: Counter[tuple[str, str]] = Counter()
        for tallies in b["clusters"].values():
            for s, t in tallies.items():
                acc = strata.setdefault(s, [0, 0, 0, 0, 0])
                for i in range(5):
                    acc[i] += t[i]
                if t[3]:
                    control_drafts[s] += 1
        # 每一层自己的对照要够句数、够篇数才算对上; 不够的层不拿别层凑, 也不退回总体基线。
        matched_strata = sorted(
            s
            for s, acc in strata.items()
            if acc[0] and acc[3] >= eff_min_controls and control_drafts[s] >= eff_min_control_drafts
        )
        hits = b["phrase_hits"]
        non_hits = b["non_phrase_sentences"]
        matched_tot = [strata[s] for s in matched_strata]
        matched_hits = sum(a[0] for a in matched_tot)
        matched_controls = sum(a[3] for a in matched_tot)
        clusters = []
        for tallies in b["clusters"].values():
            vec = [tallies.get(s, [0, 0, 0, 0, 0]) for s in matched_strata]
            if any(any(t) for t in vec):
                clusters.append(vec)
        matched_hit_drafts = sum(1 for vec in clusters if any(t[0] for t in vec))

        changed_res = _lift(matched_tot, 1)
        word_res = _lift(matched_tot, 2)
        ci = (
            _cluster_bootstrap(clusters, resamples=int(resamples), alpha=eff_alpha, seed=int(seed))
            if changed_res is not None
            else None
        )
        coverage = (matched_hits / hits) if hits else None
        passed = bool(
            matched_hit_drafts >= eff_min_hit_drafts
            and coverage is not None
            and coverage >= eff_min_cov
            and ci is not None
            and ci[0] > min_lift_ci_low
        )
        if passed:
            passing_categories += 1

        categories_out[cat] = {
            "pairs": b["pairs"],
            "drafts": len(b["clusters"]),
            "total_sentences": b["total_sentences"],
            "phrase_hits": hits,
            "matched_strata": len(matched_strata),
            "matched_phrase_hits": matched_hits,
            "unmatched_stratum_hits": hits - matched_hits,
            "matched_coverage": _r4(coverage),
            "matched_hit_drafts": matched_hit_drafts,
            "non_phrase_sentences": non_hits,
            "matched_non_phrase_sentences": matched_controls,
            "phrase_sentence_changed": b["phrase_sentence_changed"],
            "phrase_punct_only": b["phrase_punct_only"],
            "non_phrase_changed": b["non_phrase_changed"],
            "non_phrase_punct_only": b["non_phrase_punct_only"],
            "sentence_changed_rate": _r4(b["phrase_sentence_changed"] / hits if hits else None),
            "raw_baseline_changed_rate": _r4(b["non_phrase_changed"] / non_hits if non_hits else None),
            "matched_sentence_changed_rate": _r4(sum(a[1] for a in matched_tot) / matched_hits if matched_hits else None),
            "stratum_baseline_changed_rate": _r4(changed_res[1] if changed_res else None),
            "sentence_changed_lift": _r4(changed_res[0] if changed_res else None),
            "lift_ci_low": _r4(ci[0] if ci else None),
            "lift_ci_high": _r4(ci[1] if ci else None),
            # 诊断: 「说法本身不见了」, 不用来放行(见模块说明第 4 条)
            "phrase_word_removed": b["phrase_word_removed"],
            "phrase_word_kept_on_edit": b["phrase_word_kept_on_edit"],
            "phrase_removed_rate": _r4(b["phrase_word_removed"] / hits if hits else None),
            "matched_phrase_removed_rate": _r4(sum(a[2] for a in matched_tot) / matched_hits if matched_hits else None),
            "word_removal_lift": _r4(word_res[0] if word_res else None),
            "passed_category_gate": passed,
        }

    return {
        "diff_rev": DIFF_REV,
        "gate_metric": "sentence_changed_lift",
        "candidate_source": candidates.source,
        "selection_pairs": candidates.selection_pairs,
        "phrase_count": len(phrases),
        "total_pairs": sum(c["pairs"] for c in categories_out.values()),
        "skipped_negative_lag": skipped_negative_lag,
        "total_sentences": sum(c["total_sentences"] for c in categories_out.values()),
        "total_phrase_hits": sum(c["phrase_hits"] for c in categories_out.values()),
        "thresholds": {
            "min_stratum_controls": eff_min_controls,
            "min_stratum_control_drafts": eff_min_control_drafts,
            "min_hit_drafts": eff_min_hit_drafts,
            "min_matched_coverage": eff_min_cov,
            "min_lift_ci_low": min_lift_ci_low,
            "ci_alpha": eff_alpha,
            "resamples": int(resamples),
            "min_categories_for_go": eff_min_cats,
        },
        "passing_categories": passing_categories,
        "gate_passed": passing_categories >= eff_min_cats,
        "categories": categories_out,
    }
