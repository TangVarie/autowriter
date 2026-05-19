"""
Semantic-duplication detection via Gemini text embeddings.

The text-only dedup pass in ``generator._build_dedup_instruction`` catches
verbatim and near-verbatim title overlap but is fooled by synonyms, paraphrases,
and stylistic rewrites (the model can rename "炫耀" → "展示" and slip past the
"15-char first-line" check).  This module adds a second pass that operates on
sentence embeddings: high cosine similarity between two titles flags them as
the same "angle" regardless of wording.

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

import config


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


try:
    from google import genai as _google_genai
    _GOOGLE_AVAILABLE = True
except Exception:
    _GOOGLE_AVAILABLE = False


_client = None  # lazily built


def _get_client():
    """Return a singleton Gemini client for embeddings, or None when the
    SDK is unavailable / unconfigured."""
    global _client
    if _client is not None:
        return _client
    if not _GOOGLE_AVAILABLE or not config.GOOGLE_API_KEY:
        return None
    kwargs = {"api_key": config.GOOGLE_API_KEY}
    if getattr(config, "GOOGLE_BASE_URL", ""):
        kwargs["http_options"] = {"base_url": config.GOOGLE_BASE_URL}
    try:
        _client = _google_genai.Client(**kwargs)
    except Exception:
        _client = None
    return _client


def embeddings_available() -> bool:
    """True if we can currently produce embeddings — used by callers that
    need to choose between the embedding path and the legacy text path."""
    return _get_client() is not None


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
    """Plain cosine similarity.  Returns 0.0 on degenerate input."""
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
