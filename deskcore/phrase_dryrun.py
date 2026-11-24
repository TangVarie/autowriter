"""deskcore/phrase_dryrun.py — 用词清单干跑核验 (D-166 / W-62 ①)。

纯计算模块，只用标准库 + deskcore.fingerprint + deskcore.tvlink（零数据库、零 LLM、零网络）。

为什么不能直接拿「带候选词的句子删改率 vs 其它句子删改率」下结论：
  1. 位置与句长混杂：开头句、结尾句、长句本来就更容易被运营删改。若某说法集中在开头/长句，
     不分层对比会把位置效应误判成词效应。这里按 ``(position, length_bucket)`` 做同层加权基线。
  2. 句级改动 vs 词级消失：运营改了句子的别处、但**保留了该说法**，不能算成该说法被删改。
     因此把 ``sentence_changed_rate``（整句删或改）与 ``phrase_removed_rate``（整句被删，或
     改后句里已不含该说法）分开统计，门槛看后者相对同层基线的净抬升 ``word_removal_lift``。
  3. 隐私边界：``evaluate_phrase_edits`` 返回的摘要只含计数、比例、品类代号、口径版本与过线标记，
     不含任何原始句子、标题、正文或候选词原文，可直接落公开日志。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping

from . import fingerprint as fp
from . import tvlink

DIFF_REV = tvlink.DIFF_REV
SHORT_MAX_CHARS = 14
MEDIUM_MAX_CHARS = 35
DEFAULT_MIN_HITS = 5
DEFAULT_MIN_LIFT = 0.10
DEFAULT_MIN_CATEGORIES_FOR_GO = 2


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


def extract_candidate_phrases(
    draft_texts: Iterable[str],
    published_texts: Iterable[str],
    *,
    min_draft_docs: int = 3,
    min_len: int = 2,
    max_len: int = 6,
    min_ratio: float = 2.0,
    top_k: int = 50,
) -> list[dict[str, Any]]:
    """从写作台稿 vs 发布帖里按文档频率筛出「稿子常用、发布少用」的候选短语。

    ⚠️ 仅在内存里使用并喂给 ``evaluate_phrase_edits``；不要把返回里的 ``phrase`` 原文
    直接打印到公开日志或写进公开仓库。
    """
    if min_len < 2 or max_len < min_len or top_k <= 0:
        return []
    draft_list = [fp.normalize(tvlink.strip_hashtags(t)) for t in draft_texts]
    draft_list = [t for t in draft_list if len(t) >= min_len]
    pub_list = [fp.normalize(tvlink.strip_hashtags(t)) for t in published_texts]
    pub_list = [t for t in pub_list if len(t) >= min_len]
    if not draft_list or not pub_list:
        return []

    def _doc_freq(docs: list[str]) -> Counter[str]:
        counts: Counter[str] = Counter()
        for doc in docs:
            seen_in_doc: set[str] = set()
            n_chars = len(doc)
            for size in range(min_len, min(max_len, n_chars) + 1):
                for i in range(n_chars - size + 1):
                    seen_in_doc.add(doc[i : i + size])
            counts.update(seen_in_doc)
        return counts

    draft_df = _doc_freq(draft_list)
    pub_df = _doc_freq(pub_list)
    n_draft = len(draft_list)
    n_pub = len(pub_list)

    candidates: list[dict[str, Any]] = []
    for phrase, d_cnt in draft_df.items():
        if d_cnt < min_draft_docs:
            continue
        p_cnt = pub_df.get(phrase, 0)
        d_rate = d_cnt / n_draft
        # Laplace smoothing avoids div-by-zero when a phrase never appears in published docs
        p_rate = (p_cnt + 0.5) / (n_pub + 1.0)
        ratio = d_rate / p_rate
        if ratio >= min_ratio:
            candidates.append(
                {
                    "phrase": phrase,
                    "draft_docs": d_cnt,
                    "published_docs": p_cnt,
                    "draft_rate": round(d_rate, 4),
                    "published_rate": round(p_cnt / n_pub, 4) if n_pub else 0.0,
                    "overuse_ratio": round(ratio, 3),
                }
            )
    candidates.sort(key=lambda c: (-c["overuse_ratio"], -c["draft_docs"], -len(c["phrase"]), c["phrase"]))
    return candidates[:top_k]


def classify_draft_sentences(draft_body: str, published_body: str) -> list[dict[str, Any]]:
    """把一篇稿子的每一句按 tvlink (DIFF_REV=2) 口径判成 unchanged / removed / edited。

    返回列表保留句序与分层键 ``(position, length_bucket)``，便于做同层对比。
    """
    clean_draft = tvlink.strip_hashtags(draft_body)
    clean_pub = tvlink.strip_hashtags(published_body)
    dsents = tvlink.sentences(clean_draft)
    if not dsents:
        return []
    psents = tvlink.sentences(clean_pub)
    pg = tvlink.grams(clean_pub)
    exact_pub = {fp.normalize(p) for p in psents}
    pub_norm_full = fp.normalize(clean_pub)
    pub_grams = [(p, fp.normalize(p), tvlink.grams(p)) for p in psents]

    total = len(dsents)
    out: list[dict[str, Any]] = []
    for idx, sent in enumerate(dsents):
        ns = fp.normalize(sent)
        stratum = (sentence_position(idx, total), length_bucket(len(ns)))
        if not tvlink._kept(sent, pg):
            outcome = "removed"
            target_norm = ""
        elif ns in exact_pub:
            outcome = "unchanged"
            target_norm = ns
        else:
            outcome = "edited"
            if pub_grams:
                g = tvlink.grams(sent)
                _, best_norm, best_overlap = max(
                    ((p, pn, len(g & q)) for p, pn, q in pub_grams),
                    key=lambda t: t[2],
                )
                # If a sentence was split/merged across boundaries, overlap with any single
                # published sentence may be 0 even though _kept(sent, pg) was True; fall back
                # to the normalized full published body before declaring a phrase removed.
                target_norm = best_norm if best_overlap > 0 else pub_norm_full
            else:
                target_norm = pub_norm_full
        out.append(
            {
                "index": idx,
                "stratum": stratum,
                "norm_sentence": ns,
                "outcome": outcome,
                "target_norm": target_norm,
            }
        )
    return out


def evaluate_phrase_edits(
    pairs: Iterable[Mapping[str, Any]],
    phrases: Iterable[str],
    *,
    min_hits: int = DEFAULT_MIN_HITS,
    min_lift: float = DEFAULT_MIN_LIFT,
    min_categories_for_go: int = DEFAULT_MIN_CATEGORIES_FOR_GO,
) -> dict[str, Any]:
    """按品类做分层对照干跑，返回**纯计数与比例**的评估摘要。

    ``pairs``: 每项含 ``category`` / ``draft_body`` / ``published_body``（已按 ``DIFF_REV=2``
    过滤掉 ``lag_days < 0`` 的对照对）。
    ``phrases``: 候选短语列表（会做 ``fp.normalize`` 去标点空白并去重）。
    """
    norm_phrases = sorted({p for raw in phrases if (p := fp.normalize(raw))})
    by_cat: dict[str, dict[str, Any]] = {}
    skipped_negative_lag = 0

    for pair in pairs:
        lag = pair.get("lag_days")
        if lag is not None:
            try:
                if float(lag) < 0:
                    skipped_negative_lag += 1
                    continue
            except (TypeError, ValueError):
                pass
        cat = str(pair.get("category") or "未分类").strip() or "未分类"
        bucket = by_cat.setdefault(
            cat,
            {
                "pairs": 0,
                "total_sentences": 0,
                "phrase_hits": 0,
                "phrase_sentence_changed": 0,
                "phrase_word_removed": 0,
                "phrase_word_kept_on_edit": 0,
                "non_phrase_sentences": 0,
                "non_phrase_changed": 0,
                "phrase_strata": Counter(),
                "non_phrase_strata_total": Counter(),
                "non_phrase_strata_changed": Counter(),
            },
        )
        bucket["pairs"] += 1
        classified = classify_draft_sentences(
            str(pair.get("draft_body") or ""),
            str(pair.get("published_body") or ""),
        )
        for item in classified:
            bucket["total_sentences"] += 1
            ns = item["norm_sentence"]
            stratum = item["stratum"]
            outcome = item["outcome"]
            changed = outcome != "unchanged"
            matched = [np for np in norm_phrases if np in ns]
            if matched:
                bucket["phrase_hits"] += 1
                bucket["phrase_strata"][stratum] += 1
                if changed:
                    bucket["phrase_sentence_changed"] += 1
                    if outcome == "removed":
                        bucket["phrase_word_removed"] += 1
                    else:
                        target_norm = item["target_norm"]
                        if any(np not in target_norm for np in matched):
                            bucket["phrase_word_removed"] += 1
                        else:
                            bucket["phrase_word_kept_on_edit"] += 1
            else:
                bucket["non_phrase_sentences"] += 1
                bucket["non_phrase_strata_total"][stratum] += 1
                if changed:
                    bucket["non_phrase_changed"] += 1
                    bucket["non_phrase_strata_changed"][stratum] += 1

    categories_out: dict[str, dict[str, Any]] = {}
    passing_categories = 0
    total_pairs = 0
    total_sentences = 0
    total_phrase_hits = 0

    for cat in sorted(by_cat):
        b = by_cat[cat]
        total_pairs += b["pairs"]
        total_sentences += b["total_sentences"]
        total_phrase_hits += b["phrase_hits"]

        hits = b["phrase_hits"]
        non_hits = b["non_phrase_sentences"]
        raw_baseline = (b["non_phrase_changed"] / non_hits) if non_hits else None

        # Stratum-matched baseline: re-weight non-phrase edit rates by the exact
        # (position, length_bucket) distribution of phrase-bearing sentences.
        matched_baseline: float | None = None
        fallback_hits = 0
        if hits > 0 and non_hits > 0:
            weighted_sum = 0.0
            for stratum, s_hits in b["phrase_strata"].items():
                base_n = b["non_phrase_strata_total"].get(stratum, 0)
                if base_n > 0:
                    rate = b["non_phrase_strata_changed"][stratum] / base_n
                else:
                    fallback_hits += s_hits
                    rate = raw_baseline if raw_baseline is not None else 0.0
                weighted_sum += s_hits * rate
            matched_baseline = weighted_sum / hits

        sent_rate = (b["phrase_sentence_changed"] / hits) if hits else None
        word_rate = (b["phrase_word_removed"] / hits) if hits else None
        sent_lift = (
            round(sent_rate - matched_baseline, 4)
            if sent_rate is not None and matched_baseline is not None
            else None
        )
        word_lift = (
            round(word_rate - matched_baseline, 4)
            if word_rate is not None and matched_baseline is not None
            else None
        )
        passed = bool(
            hits >= min_hits
            and non_hits >= min_hits
            and word_lift is not None
            and word_lift >= min_lift
        )
        if passed:
            passing_categories += 1

        categories_out[cat] = {
            "pairs": b["pairs"],
            "total_sentences": b["total_sentences"],
            "phrase_hits": hits,
            "non_phrase_sentences": non_hits,
            "phrase_sentence_changed": b["phrase_sentence_changed"],
            "phrase_word_removed": b["phrase_word_removed"],
            "phrase_word_kept_on_edit": b["phrase_word_kept_on_edit"],
            "non_phrase_changed": b["non_phrase_changed"],
            "sentence_changed_rate": round(sent_rate, 4) if sent_rate is not None else None,
            "phrase_removed_rate": round(word_rate, 4) if word_rate is not None else None,
            "raw_baseline_changed_rate": round(raw_baseline, 4) if raw_baseline is not None else None,
            "stratum_baseline_changed_rate": round(matched_baseline, 4) if matched_baseline is not None else None,
            "sentence_changed_lift": sent_lift,
            "word_removal_lift": word_lift,
            "stratum_fallback_hits": fallback_hits,
            "passed_category_gate": passed,
        }

    return {
        "diff_rev": DIFF_REV,
        "phrase_count": len(norm_phrases),
        "total_pairs": total_pairs,
        "skipped_negative_lag": skipped_negative_lag,
        "total_sentences": total_sentences,
        "total_phrase_hits": total_phrase_hits,
        "min_hits": min_hits,
        "min_lift": min_lift,
        "min_categories_for_go": min_categories_for_go,
        "passing_categories": passing_categories,
        "gate_passed": passing_categories >= min_categories_for_go,
        "categories": categories_out,
    }
