"""
Semantic-duplication detection via Gemini text embeddings.

The text-only dedup pass in ``generator._build_dedup_instruction`` catches
verbatim and near-verbatim title overlap but is fooled by synonyms, paraphrases,
and stylistic rewrites (the model can rename "炫耀" → "展示" and slip past the
"前 20 字开头 + 核心名词重合" check).  This module adds a second pass that
operates on sentence embeddings: high cosine similarity between two titles
flags them as the same "angle" regardless of wording.

Design choices:
- Uses the project's existing ``google-genai`` dependency (no new package).
- Embeddings are 768-dim (Gemini ``text-embedding-004``); persisted to
  Supabase as ``vector(768)`` once the schema migration runs.
- The whole module is a no-op when ``GOOGLE_API_KEY`` is missing or the
  embedding call fails — callers must handle ``embeddings_available()``
  returning False and degrade to text-only dedup.
"""

from __future__ import annotations

import math
from typing import Optional

import clients

# 审计 SUP-003: 逐元素纯 Python 的余弦在这里是主要成本。
# 队列去重池上限 2000 条 × 768 维, 每批 10 条新标题 = 1536 万次乘加 + 同量级的
# 属性查找与 zip 迭代, 每批多出 10-20 秒 CPU —— 而且是在 worker 线程里占着
# GIL, 主线程的 Streamlit 渲染一起被拖慢。矩阵乘把同样的算术交给 BLAS。
#
# numpy 是 streamlit 的传递依赖(requirements.lock 里 numpy==2.4.6), 但本模块
# 是【查重硬闸】的一部分, 不能因为某个部署只装了 requirements.txt 就整个失效
# —— 所以 requirements.txt 里显式声明了它, 同时保留纯 Python 兜底: import 不到
# 就退回逐元素算法, 结果完全一致, 只是慢。
try:
    import numpy as _np
except Exception:      # pragma: no cover - 只有裁剪过依赖的部署会走到
    _np = None


def numpy_available() -> bool:
    """矩阵乘路径是否可用。回归用例靠它区分两条路径。"""
    return _np is not None


# Cosine similarity above this between two titles ⇒ same angle (hard repeat).
# Tuned conservatively so paraphrases trip it but legitimate angle variants
# (same topic, different framing) survive.
HARD_DUPLICATE_THRESHOLD: float = 0.92

# Threshold for the softer warning shown in the dedup-block prompt.  Hits in
# this band aren't auto-dropped; the model is told they're "high-risk angles
# already heavily covered" so it should pick something distinctly different.
RISK_THRESHOLD: float = 0.85

EMBEDDING_DIM: int = 768
EMBEDDING_MODEL: str = "text-embedding-004"


def _get_client():
    """Return a singleton Gemini client for embeddings, or None when the
    SDK is unavailable / unconfigured.

    转发到 ``clients.get_genai_client()`` — 模块级单例在 clients.py 集中维护，
    避免本模块与 generator.py 各自持一份 _client。
    """
    return clients.get_genai_client()


def embeddings_available() -> bool:
    """True if we can currently produce embeddings — used by callers that
    need to choose between the embedding path and the legacy text path."""
    return clients.genai_available()


def embed_texts(texts: list[str]) -> Optional[list[list[float]]]:
    """Return one embedding per input string, or ``None`` if the call fails.

    Inputs are silently truncated to 1024 chars (titles + openings fit well
    inside this) so a malformed body doesn't blow the per-request payload.
    """
    if not texts:
        return []
    client = _get_client()
    if client is None:
        return None
    safe = [(t or "")[:1024] for t in texts]
    try:
        # google-genai accepts a list of contents and returns parallel embeddings
        resp = client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=safe,
        )
    except Exception:
        return None

    # The SDK wraps the embeddings as a list of objects with a `.values`
    # attribute; handle both shapes defensively in case the SDK schema
    # shifts again.
    try:
        out: list[list[float]] = []
        for e in resp.embeddings:
            vals = getattr(e, "values", None) or getattr(e, "embedding", None)
            if vals is None:
                return None
            out.append(list(vals))
        return out
    except Exception:
        return None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Plain cosine similarity.  Returns 0.0 on degenerate input.

    单对比较仍走这里(``memory.filter_soft_by_relevance`` 之类的少量比较);
    成批比较请用下面的矩阵路径, 别在调用方写双重循环。
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / math.sqrt(na * nb)


# ── 成批余弦(审计 SUP-003) ────────────────────────────────────────────────

def _as_matrix(vecs: list[list[float]]):
    """把 list[list[float]] 转成二维数组; 不适合走矩阵路径时返回 None。

    返回 None 的三种情况都必须退回逐元素算法, 不能当成"没有相似项":
      · numpy 不可用
      · 维度参差(某条向量长度不同 / 为空)—— ``cosine_similarity`` 对这种对子
        定义成 0.0, 矩阵化会直接抛 ValueError 或造出错位的乘积
      · 含无法转成 float 的元素
    """
    if _np is None or not vecs:
        return None
    dim = len(vecs[0] or ())
    if dim == 0:
        return None
    for v in vecs:
        if not v or len(v) != dim:
            return None
    try:
        return _np.asarray(vecs, dtype=_np.float64)
    except Exception:
        return None


def _unit_rows(mat):
    """行归一化。零向量整行归零 —— 与 ``cosine_similarity`` 的"模为 0 返 0.0"
    一致; 不这么做会得到 nan, 而 nan 在 argmax 里会**赢过所有真分数**。"""
    norms = _np.linalg.norm(mat, axis=1)
    zero = norms == 0.0
    unit = mat / _np.where(zero, 1.0, norms)[:, None]
    unit[zero] = 0.0
    return unit


def similarity_matrix(new_vecs: list[list[float]], hist_vecs: list[list[float]]):
    """``sims[i][j]`` = new_vecs[i] 与 hist_vecs[j] 的余弦; 走不了矩阵就返回 None。

    nan / inf 归零: ``cosine_similarity`` 遇到它们返回 nan, 而 nan 在原来的
    ``if score > best_score`` 里永远为 False(永不当选)。numpy 的 argmax 相反 ——
    nan 会被当成最大值胜出, 于是一条脏向量就能把随便哪篇历史稿报成"撞车"。
    两条路径的结论必须一致。
    """
    a = _as_matrix(new_vecs)
    b = _as_matrix(hist_vecs)
    if a is None or b is None or a.shape[1] != b.shape[1]:
        return None
    sims = _unit_rows(a) @ _unit_rows(b).T
    return _np.nan_to_num(sims, nan=0.0, posinf=0.0, neginf=0.0)


def find_near_duplicates(
    new_vecs: list[list[float]],
    new_titles: list[str],
    historical_vecs: list[list[float]],
    historical_titles: list[str],
    threshold: float = HARD_DUPLICATE_THRESHOLD,
) -> list[dict]:
    """For each new title, return ``{"index", "title", "best_match", "score"}``
    when its closest historical neighbour has cosine ≥ ``threshold``.

    Items below the threshold are omitted.  The result is ordered by new-item
    index so callers can tag in place.
    """
    out: list[dict] = []
    if not new_vecs or not historical_vecs:
        return out
    if len(new_vecs) != len(new_titles):
        return out
    if len(historical_vecs) != len(historical_titles):
        return out

    # 审计 SUP-003: 一次矩阵乘代替 |new| × |hist| 次纯 Python 循环。
    # 并列时取【下标最小】的那条 —— 与旧的 `if score > best_score`(严格大于,
    # 后来者不顶替)一致; numpy 的 argmax 同样返回首个最大值。
    sims = similarity_matrix(new_vecs, historical_vecs)
    if sims is not None:
        for i in range(len(new_vecs)):
            j = int(sims[i].argmax())
            best_score = float(sims[i][j])
            # 旧路径的累加器从 0.0 起、且要求【严格大于】才顶替, 所以全为
            # 负分(或恰好 0)时报的是 score=0.0 / best_match=""。阈值默认远大于
            # 0 时两条路径本来就都不上报, 但阈值可由调用方传入, 口径要对齐。
            best_title = historical_titles[j] if best_score > 0.0 else ""
            if best_score <= 0.0:
                best_score = 0.0
            if best_score >= threshold:
                out.append({
                    "index":      i,
                    "title":      new_titles[i],
                    "best_match": best_title,
                    "score":      best_score,
                })
        return out

    for i, nv in enumerate(new_vecs):
        best_score = 0.0
        best_title = ""
        for hv, ht in zip(historical_vecs, historical_titles):
            score = cosine_similarity(nv, hv)
            if score > best_score:
                best_score = score
                best_title = ht
        if best_score >= threshold:
            out.append({
                "index":      i,
                "title":      new_titles[i],
                "best_match": best_title,
                "score":      best_score,
            })
    return out


def cross_batch_pairs(
    vecs: list[list[float]],
    titles: list[str],
    threshold: float = HARD_DUPLICATE_THRESHOLD,
) -> list[dict]:
    """Same as ``find_near_duplicates`` but within a single list — flags
    pairs where two items in the same batch are angles of each other."""
    out: list[dict] = []
    n = len(vecs)
    if n < 2 or n != len(titles):
        return out
    # 审计 SUP-003: 同上, 一次矩阵乘。只读上三角, 输出顺序(i 升序、同 i 内 j
    # 升序)与双重循环逐字一致 —— 调用方按顺序标记, 顺序变了标记也会变。
    sims = similarity_matrix(vecs, vecs)
    if sims is not None:
        for i in range(n):
            for j in range(i + 1, n):
                score = float(sims[i][j])
                if score >= threshold:
                    out.append({
                        "i":         i,
                        "j":         j,
                        "title_i":   titles[i],
                        "title_j":   titles[j],
                        "score":     score,
                    })
        return out
    for i in range(n):
        for j in range(i + 1, n):
            score = cosine_similarity(vecs[i], vecs[j])
            if score >= threshold:
                out.append({
                    "i":         i,
                    "j":         j,
                    "title_i":   titles[i],
                    "title_j":   titles[j],
                    "score":     score,
                })
    return out
