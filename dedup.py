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
- Embeddings are 768-dim; persisted to Supabase as ``vector(768)``.
  模型是 ``gemini-embedding-001``, 它默认回 3072 维, 靠 ``output_dimensionality``
  截到 768 —— 见 EMBEDDING_MODEL 上面那段(前一个模型 text-embedding-004 已下线)。
- The whole module is a no-op when ``GOOGLE_API_KEY`` is missing or the
  embedding call fails — callers must handle ``embeddings_available()``
  returning False and degrade to text-only dedup.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import clients

logger = logging.getLogger("dedup")

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

# ⚠️ 改这个名字之前先读完这段。
#
# 2026-08-26 首次真部署时发现 ``text-embedding-004`` 已经**下线**了 ——
# API 回 404「is not found for API version v1beta, or is not supported for
# embedContent」。而这条路径每一层都是静默的(见 embed_texts 的注释), 所以
# 表现不是报错, 是 check_drafts 的 semantic_degraded 悄悄变 true, 四路信号
# 里最贵的那一路从此不发言。
#
# ``gemini-embedding-001`` **默认输出 3072 维**, 而库里三列都是 vector(768)
# (draft_fingerprints.title_embedding / versions.embedding / memories.embedding)。
# 靠 output_dimensionality 截到 768, 不动 schema —— 见 embed_texts。
#
# 余弦不受截断影响: 本模块的 cosine_similarity 除以模长, pgvector 的 `<=>`
# 也是余弦距离, 两条路径都归一化。**唯一的硬约束就是维度必须等于
# EMBEDDING_DIM**, 而那一条由 embed_texts 里的守卫盯着。
EMBEDDING_MODEL: str = "gemini-embedding-001"


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


def embed_texts(texts: list[str]) -> Optional[list[Optional[list[float]]]]:
    """每个输入回一个向量; **整批失败**时回 ``None``。

    返回的列表与输入**逐位对齐**, 但某一位可能是 ``None`` —— 那表示"这一条
    没有可嵌入的内容"(空标题), 不是失败。调用方一律要判 ``if v``; 现有调用方
    都已经这么写了, ``cosine_similarity`` 对 None 也返回 0.0。

    Inputs are silently truncated to 1024 chars (titles + openings fit well
    inside this) so a malformed body doesn't blow the per-request payload.

    ⚠️ **空串必须在送出去之前剔掉。** google-genai 对空内容直接抛
    ``ValueError: content is required.``, 而且是**整个请求**失败 —— 一条空标题
    能让同一批里另外 49 条正常稿子全都拿不到向量。

    2026-08-26 回填生产历史稿时现场撞上的: 第一个项目 67 条里有一批 17 条整批
    作废, 而其中标题真的为空的只有几条。这一批的失败**被日志记下来了**(那正是
    同一天补的留痕), 否则又是一次"跑完了、看着正常、向量少了一截"。
    """
    if not texts:
        return []
    client = _get_client()
    if client is None:
        logger.warning("embed_texts: 没有 genai client(GOOGLE_API_KEY 未配 "
                       "或 SDK 未安装) —— 本次退化为纯确定性查重")
        return None

    safe = [(t or "").strip()[:1024] for t in texts]
    keep = [i for i, s in enumerate(safe) if s]
    if not keep:
        # 全是空的: 不必打扰 API, 也**不是**失败 —— 如实回一排 None。
        logger.warning("embed_texts: %d 条输入全是空串, 这一批没有可嵌入的内容",
                       len(texts))
        return [None] * len(texts)
    if len(keep) != len(texts):
        logger.warning("embed_texts: %d/%d 条输入是空串, 已剔除后再送 —— "
                       "它们的向量位置回 None, 不影响同批其它条",
                       len(texts) - len(keep), len(texts))
    payload = [safe[i] for i in keep]
    try:
        # google-genai accepts a list of contents and returns parallel embeddings。
        #
        # ⚠️ output_dimensionality 不是可选的调参: gemini-embedding-001 默认回
        # 3072 维, 而库里是 vector(768)。不截的话写库直接报维度不符, 更糟的是
        # 在只比不写的路径上 cosine_similarity 因 len(a) != len(b) **静默返回
        # 0.0** —— 查重变哑弹且不报错(与 R-034 的 _parse_pgvector 同款形状)。
        resp = client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=payload,
            config={"output_dimensionality": EMBEDDING_DIM},
        )
    except Exception:
        # ⚠️ 这里原来是裸 ``except Exception: return None``, **一行日志都不打**。
        # 代价在 2026-08-26 首次部署时兑现了: text-embedding-004 下线之后, 表面
        # 现象只是 semantic_degraded 悄悄变 true, Railway 日志里干干净净, 最后
        # 是靠人肉 curl 才问出「模型 404」。降级可以静默, 但**降级的原因不行**。
        logger.exception("embed_texts: embed_content 调用失败 (model=%s, n=%d) "
                         "—— 本次退化为纯确定性查重", EMBEDDING_MODEL, len(payload))
        return None

    # The SDK wraps the embeddings as a list of objects with a `.values`
    # attribute; handle both shapes defensively in case the SDK schema
    # shifts again.
    try:
        out: list[list[float]] = []
        for e in resp.embeddings:
            vals = getattr(e, "values", None) or getattr(e, "embedding", None)
            if vals is None:
                logger.error("embed_texts: 返回里有一条没有 values/embedding 字段 "
                             "—— SDK 形状变了? (model=%s)", EMBEDDING_MODEL)
                return None
            out.append(list(vals))
    except Exception:
        logger.exception("embed_texts: 解析返回失败 (model=%s)", EMBEDDING_MODEL)
        return None

    # ── 维度守卫 ──────────────────────────────────────────────────────────
    # 换模型 / 换 SDK / output_dimensionality 被谁改掉, 都会从这里冒出来。
    # **必须整批作废而不是把不对的向量传下去**: 长度不符时 cosine_similarity
    # 返回 0.0, 于是"查重跑了、全 pass、看着一切正常", 而它一条都抓不到。
    # 宁可降级成纯确定性(那条路仍然有效且会在 summary 里报出来), 也不要一个
    # 假装在工作的语义信号。
    bad = next((len(v) for v in out if len(v) != EMBEDDING_DIM), None)
    if bad is not None:
        logger.error(
            "embed_texts: 维度不符 —— 期望 %d, 拿到 %d (model=%s)。整批作废, "
            "本次退化为纯确定性查重。库里三列都是 vector(%d), 换模型要么让它输出 "
            "%d 维, 要么连 schema 一起改。",
            EMBEDDING_DIM, bad, EMBEDDING_MODEL, EMBEDDING_DIM, EMBEDDING_DIM)
        return None

    # ── 条数守卫 ──────────────────────────────────────────────────────────
    # ⚠️ 调用方**一律按下标取**(vecs[i] 对应 drafts[i])。API 少回一条, 下面那
    # 个回填就会把 A 的向量安到 B 头上 —— 查重照跑, 比的却是别人的标题, 而且
    # 不报错。这是比"没有向量"坏得多的一种坏, 所以宁可整批作废。
    if len(out) != len(payload):
        logger.error("embed_texts: 送了 %d 条只回来 %d 条 (model=%s) —— "
                     "按下标对齐会张冠李戴, 整批作废",
                     len(payload), len(out), EMBEDDING_MODEL)
        return None

    # 映射回原位: 被剔掉的空串那几位留 None。
    aligned: list[Optional[list[float]]] = [None] * len(texts)
    for pos, i in enumerate(keep):
        aligned[i] = out[pos]
    return aligned


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
