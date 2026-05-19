"""
Supabase database operations for XHS Content Workstation.

Tables:
  projects  – project configuration per user
  batches   – generation batches linked to a project
  items     – individual copy items within a batch
  versions  – text versions of each item (with AI engine + feedback)
  memories  – project-level or global feedback memories
"""

from __future__ import annotations

import json
from typing import Any, Optional
from datetime import datetime

from supabase import create_client, Client
import config

try:
    import streamlit as st
    _HAS_ST = True
except Exception:
    _HAS_ST = False


def _cache_data(**kwargs):
    """Streamlit cache_data shim — no-op decorator when Streamlit isn't loaded
    (e.g. unit-test imports), and otherwise delegate to ``st.cache_data``.

    Callers pass the underlying Client positionally as a ``_client`` parameter
    so Streamlit's hasher skips it (leading underscore == unhashable).  Cache
    invalidation is done by writers calling ``<reader>.clear()`` after mutating
    the underlying row.
    """
    if _HAS_ST:
        return st.cache_data(**kwargs)
    def passthrough(fn):
        fn.clear = lambda: None  # match cache_data API for unconditional callers
        return fn
    return passthrough


def _cache_resource(**kwargs):
    """Same shim, for objects whose identity matters (e.g. Supabase Client)."""
    if _HAS_ST:
        return st.cache_resource(**kwargs)
    def passthrough(fn):
        fn.clear = lambda: None
        return fn
    return passthrough


@_cache_resource(show_spinner=False)
def _make_client_cached(supabase_url: str, anon_key: str, access_token: str) -> Client:
    """Per-token Supabase client singleton.  Keyed on the token so each
    authenticated user gets their own client; ``access_token=""`` returns the
    anonymous client.  Cleared on sign-out via ``_make_client_cached.clear()``.
    """
    client = create_client(supabase_url, anon_key)
    if access_token:
        client.postgrest.auth(access_token)
    return client


def get_client(access_token: Optional[str] = None) -> Client:
    """Return a Supabase client, optionally authenticated with the user JWT.

    Cached per-token via ``_make_client_cached`` so each Streamlit rerun
    reuses the same Client (and underlying httpx connection pool) instead of
    rebuilding it.  Falls back to a fresh client when Streamlit isn't loaded.
    """
    return _make_client_cached(
        config.SUPABASE_URL, config.SUPABASE_ANON_KEY, access_token or ""
    )


# ── DDL helpers (run once during setup) ───────────────────────────────────

CREATE_TABLES_SQL = """
-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Projects
CREATE TABLE IF NOT EXISTS projects (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name         TEXT NOT NULL,
    brand        TEXT,
    system_prompt TEXT,
    system_prompt_tone TEXT,
    system_prompt_exec TEXT,
    tactics      JSONB DEFAULT '[]'::jsonb,
    reference_files JSONB DEFAULT '[]'::jsonb,
    default_params  JSONB DEFAULT '{}'::jsonb,
    owner_id     UUID NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
CREATE POLICY IF NOT EXISTS projects_owner ON projects
    USING (owner_id = auth.uid());
-- Migration: add dual-prompt columns if upgrading from older schema
ALTER TABLE projects ADD COLUMN IF NOT EXISTS system_prompt_tone TEXT;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS system_prompt_exec TEXT;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS calibration_notes TEXT;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS custom_roles JSONB DEFAULT '[]'::jsonb;
-- Migration: add example_label to items for positive/negative example marking
ALTER TABLE items ADD COLUMN IF NOT EXISTS example_label TEXT CHECK (example_label IN ('positive', 'negative'));

-- Batches
CREATE TABLE IF NOT EXISTS batches (
    id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id     UUID REFERENCES projects(id) ON DELETE CASCADE,
    tactic         TEXT,
    params         JSONB DEFAULT '{}'::jsonb,
    ai_engines     JSONB DEFAULT '["claude"]'::jsonb,
    user_id        UUID NOT NULL,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE batches ENABLE ROW LEVEL SECURITY;
CREATE POLICY IF NOT EXISTS batches_owner ON batches
    USING (user_id = auth.uid());

-- Items (one per generated copy slot)
CREATE TABLE IF NOT EXISTS items (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    batch_id        UUID REFERENCES batches(id) ON DELETE CASCADE,
    status          TEXT DEFAULT 'pending' CHECK (status IN ('pending','approved','needs_revision')),
    best_version_id UUID,
    user_id         UUID NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE items ADD COLUMN IF NOT EXISTS ai_review_notes TEXT;
-- Persist a user's typed iteration feedback BEFORE running the AI call so
-- it survives crashes (token expiry, network drop, server error).  Cleared
-- on successful iteration; restored into the textarea on next render.
ALTER TABLE items ADD COLUMN IF NOT EXISTS feedback_draft TEXT;
-- Same idea for the "✏️ 手动精修" form: {title, body, keywords_raw,
-- base_version_id} so we can restore precisely against the version the
-- user was editing and not against an unrelated newly-selected best.
ALTER TABLE items ADD COLUMN IF NOT EXISTS manual_edit_draft JSONB;
ALTER TABLE items ENABLE ROW LEVEL SECURITY;
CREATE POLICY IF NOT EXISTS items_owner ON items
    USING (user_id = auth.uid());

-- Versions (each AI generation or iteration)
CREATE TABLE IF NOT EXISTS versions (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    item_id     UUID REFERENCES items(id) ON DELETE CASCADE,
    version_num INTEGER NOT NULL DEFAULT 1,
    ai_engine   TEXT NOT NULL,
    title       TEXT,
    body        TEXT,
    keywords    JSONB DEFAULT '[]'::jsonb,
    feedback    TEXT,
    images      JSONB DEFAULT '[]'::jsonb,
    token_usage JSONB DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE versions ENABLE ROW LEVEL SECURITY;
CREATE POLICY IF NOT EXISTS versions_owner ON versions
    USING (
        item_id IN (SELECT id FROM items WHERE user_id = auth.uid())
    );
-- Semantic-similarity embedding for cross-batch duplicate detection.
-- Requires the pgvector extension (Supabase: Database → Extensions → enable
-- "vector" once).  Nullable so legacy rows stay readable; a backfill helper
-- populates them lazily.
CREATE EXTENSION IF NOT EXISTS vector;
ALTER TABLE versions ADD COLUMN IF NOT EXISTS embedding vector(768);
CREATE INDEX IF NOT EXISTS versions_embedding_idx
    ON versions USING ivfflat (embedding vector_cosine_ops);

-- Memories (project-level or account-level — 'global' scope is per-user across projects)
CREATE TABLE IF NOT EXISTS memories (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    scope           TEXT NOT NULL CHECK (scope IN ('project','global')),
    project_id      UUID REFERENCES projects(id) ON DELETE CASCADE,
    content         TEXT NOT NULL,
    source_feedback TEXT,
    frequency       INTEGER DEFAULT 1,
    status          TEXT DEFAULT 'candidate' CHECK (status IN ('candidate','confirmed')),
    user_id         UUID NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
-- Additive schema upgrades for session-level instructions + memory typing.
-- Idempotent so existing deployments can re-run this block safely.
ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS memory_type TEXT NOT NULL DEFAULT 'rule'
        CHECK (memory_type IN ('rule','note','session'));
ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS source_batch_id UUID REFERENCES batches(id) ON DELETE SET NULL;
ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ NULL;
-- Severity: hard rules feed the P0 tier of the system prompt (100% must
-- satisfy: compliance / brand lines), soft rules feed P1 (apply when
-- relevant).  Legacy rows default to 'soft' so they keep being injected
-- but no longer drown the P0 tier.
ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS severity TEXT NOT NULL DEFAULT 'soft'
        CHECK (severity IN ('hard','soft'));
-- Applicability: short free-text hint about where the rule applies
-- ("标题" / "正文开头" / "全局" / specific tactic name).  Used by the
-- ranker to skip rules unrelated to the current generation.
ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS applicability TEXT NULL;
-- Optional silence flag: user-driven temporary mute (24h tag).  NULL
-- means no mute; future-dated value means "skip injection until then".
ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS muted_until TIMESTAMPTZ NULL;
CREATE INDEX IF NOT EXISTS memories_session_idx
    ON memories(user_id, memory_type, expires_at)
    WHERE memory_type = 'session';
-- Embedding for soft-rule relevance filtering.  The injection ranker
-- keeps every hard rule and only filters soft rules whose embedding is
-- semantically far from the current generation context; rows without an
-- embedding (legacy / backfill-pending) pass through unchanged.
ALTER TABLE memories ADD COLUMN IF NOT EXISTS embedding vector(768);
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
CREATE POLICY IF NOT EXISTS memories_owner ON memories
    USING (user_id = auth.uid());

-- ── Data API grants ──────────────────────────────────────────────────────
-- Forward-compat for Supabase's May/Oct 2026 change: new tables in "public"
-- will no longer be auto-exposed to PostgREST/supabase-js/GraphQL without an
-- explicit GRANT.  Existing tables keep their pre-change grants forever, so
-- running this block on the current project is a harmless idempotent
-- re-grant; a fresh provisioning of a new Supabase project after the cutoff
-- gets working grants out of the box.
--
-- We grant nothing to ``anon`` — every row in every table is user-scoped and
-- gated by RLS, and there's no public-read use case in this app.  ``service_
-- role`` keeps full access for any admin scripts; ``authenticated`` gets the
-- standard CRUD set and RLS does the per-user filtering.
GRANT SELECT, INSERT, UPDATE, DELETE ON
    projects, batches, items, versions, memories
    TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON
    projects, batches, items, versions, memories
    TO service_role;
"""


# ── Project CRUD ──────────────────────────────────────────────────────────

@_cache_data(ttl=60, show_spinner=False)
def list_projects(_client: Client, user_id: str) -> list[dict]:
    res = (
        _client.table("projects")
        .select("*")
        .eq("owner_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    return res.data or []


def get_project(client: Client, project_id: str) -> Optional[dict]:
    res = (
        client.table("projects")
        .select("*")
        .eq("id", project_id)
        .single()
        .execute()
    )
    return res.data


def create_project(
    client: Client,
    user_id: str,
    name: str,
    brand: str = "",
    system_prompt: str = "",
    tactics: Optional[list] = None,
    default_params: Optional[dict] = None,
) -> dict:
    data = {
        "name": name,
        "brand": brand,
        "system_prompt": system_prompt,
        "tactics": json.dumps(tactics or []),
        "default_params": json.dumps(default_params or {}),
        "owner_id": user_id,
    }
    res = client.table("projects").insert(data).execute()
    list_projects.clear()
    return res.data[0]


def update_project(client: Client, project_id: str, updates: dict) -> dict:
    # Serialise JSON fields if passed as Python objects
    for key in ("tactics", "default_params", "reference_files"):
        if key in updates and not isinstance(updates[key], str):
            updates[key] = json.dumps(updates[key])
    res = (
        client.table("projects")
        .update(updates)
        .eq("id", project_id)
        .execute()
    )
    list_projects.clear()
    return res.data[0]


def delete_project(client: Client, project_id: str) -> None:
    client.table("projects").delete().eq("id", project_id).execute()
    list_projects.clear()


# ── Batch CRUD ─────────────────────────────────────────────────────────────

def create_batch(
    client: Client,
    user_id: str,
    project_id: str,
    tactic: str,
    params: dict,
    ai_engines: list[str],
) -> dict:
    data = {
        "project_id": project_id,
        "tactic": tactic,
        "params": params,
        "ai_engines": ai_engines,
        "user_id": user_id,
    }
    res = client.table("batches").insert(data).execute()
    list_batches.clear()
    return res.data[0]


@_cache_data(ttl=30, show_spinner=False)
def list_batches(_client: Client, project_id: str, limit: int = 20) -> list[dict]:
    res = (
        _client.table("batches")
        .select("*")
        .eq("project_id", project_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data or []


def delete_batch(client: Client, batch_id: str) -> None:
    """Delete a batch and all its items/versions (cascade order)."""
    # 1. Collect item ids
    items_res = (
        client.table("items")
        .select("id")
        .eq("batch_id", batch_id)
        .execute()
    )
    item_ids = [r["id"] for r in (items_res.data or [])]

    # 2. Delete versions
    if item_ids:
        client.table("versions").delete().in_("item_id", item_ids).execute()

    # 3. Delete items
    client.table("items").delete().eq("batch_id", batch_id).execute()

    # 4. Delete batch
    client.table("batches").delete().eq("id", batch_id).execute()
    list_batches.clear()


# ── Item CRUD ──────────────────────────────────────────────────────────────

def create_item(
    client: Client,
    user_id: str,
    batch_id: str,
    ai_review_notes: Optional[str] = None,
) -> dict:
    data: dict[str, Any] = {"batch_id": batch_id, "user_id": user_id}
    if ai_review_notes:
        data["ai_review_notes"] = ai_review_notes
    res = client.table("items").insert(data).execute()
    return res.data[0]


def bulk_create_items(client: Client, rows: list[dict]) -> list[dict]:
    """Insert N item rows in a single round trip; returns the inserted rows
    with their generated ``id`` columns in input order.  Each row should
    carry ``batch_id``, ``user_id``, and optionally ``ai_review_notes``."""
    if not rows:
        return []
    res = client.table("items").insert(rows).execute()
    return res.data or []


def update_version_embedding(
    client: Client, version_id: str, vec: list[float]
) -> None:
    """Persist a 768-dim embedding for a single version row.  Tolerant of
    older deployments where the pgvector column hasn't been added yet —
    fails silently so the generation path doesn't blow up on a missing
    column."""
    if not vec:
        return
    try:
        client.table("versions").update({"embedding": vec}).eq("id", version_id).execute()
    except Exception:
        pass


def bulk_update_version_embeddings(
    client: Client, rows: list[dict]
) -> None:
    """Persist many version embeddings in one round trip.  Each row needs
    ``{"id": <version_id>, "embedding": [..]}``.  Falls back to per-row
    UPDATE if upsert isn't allowed by RLS for this user.

    Best-effort: errors are swallowed so an embedding failure never blocks
    the user's batch.
    """
    if not rows:
        return
    try:
        client.table("versions").upsert(rows).execute()
        return
    except Exception:
        pass
    # Per-row fallback
    for r in rows:
        update_version_embedding(client, r.get("id", ""), r.get("embedding") or [])


def get_recent_titles_openings_with_embeddings(
    client: Client, project_id: str, limit: int = 150
) -> list[dict]:
    """Like ``get_recent_titles_and_openings`` but also returns the stored
    embedding when present.  Used by the embedding-based dedup path; if a
    historical version doesn't have an embedding yet, the entry's
    ``embedding`` key is None and the caller can decide to skip it or
    backfill on demand.

    Returns ``{"version_id", "title", "opening", "embedding"}`` per item.
    """
    batches = list_batches(client, project_id, limit=40)
    if not batches:
        return []
    batch_ids = [b["id"] for b in batches]

    items_res = (
        client.table("items")
        .select("id, best_version_id")
        .in_("batch_id", batch_ids)
        .execute()
    )
    items = items_res.data or []
    if not items:
        return []
    item_ids = [it["id"] for it in items]

    try:
        versions_res = (
            client.table("versions")
            .select("id, item_id, title, body, version_num, embedding")
            .in_("item_id", item_ids)
            .execute()
        )
    except Exception:
        # embedding column not yet migrated — degrade gracefully
        versions_res = (
            client.table("versions")
            .select("id, item_id, title, body, version_num")
            .in_("item_id", item_ids)
            .execute()
        )
    versions_by_item: dict[str, list[dict]] = {}
    for v in (versions_res.data or []):
        versions_by_item.setdefault(v["item_id"], []).append(v)

    out: list[dict] = []
    for item in items:
        versions = versions_by_item.get(item["id"], [])
        if not versions:
            continue
        chosen = None
        best_vid = item.get("best_version_id")
        if best_vid:
            for v in versions:
                if v.get("id") == best_vid:
                    chosen = v
                    break
        if not chosen:
            chosen = max(versions, key=lambda v: v.get("version_num", 0))
        title = (chosen.get("title") or "").strip()
        if not title or title == "（解析失败）":
            continue
        body = (chosen.get("body") or "").strip()
        first_line = next((ln for ln in body.splitlines() if ln.strip()), "")
        opening = first_line.strip()[:25]
        out.append({
            "version_id": chosen.get("id"),
            "title":      title,
            "opening":    opening,
            "embedding":  chosen.get("embedding"),
        })
    return out[:limit]


def bulk_create_initial_versions(client: Client, rows: list[dict]) -> list[dict]:
    """Insert a batch of first-version rows (``version_num=1``) in one
    round trip.  Each row should carry ``item_id``, ``ai_engine``, ``title``,
    ``body``, and optionally ``keywords`` / ``token_usage``.

    Used by the batch-generation save path; iteration / manual-edit paths
    still go through ``create_version`` because they need the next available
    version_num for an existing item."""
    if not rows:
        return []
    payload = []
    for r in rows:
        payload.append({
            "item_id":     r["item_id"],
            "version_num": 1,
            "ai_engine":   r["ai_engine"],
            "title":       r.get("title", ""),
            "body":        r.get("body", ""),
            "keywords":    r.get("keywords") or [],
            "feedback":    r.get("feedback"),
            "images":      r.get("images") or [],
            "token_usage": r.get("token_usage") or {},
        })
    res = client.table("versions").insert(payload).execute()
    return res.data or []


def list_items(client: Client, batch_id: str) -> list[dict]:
    res = (
        client.table("items")
        .select("*, versions(*)")
        .eq("batch_id", batch_id)
        .order("created_at")
        .execute()
    )
    return res.data or []


def update_item_status(
    client: Client, item_id: str, status: str, best_version_id: Optional[str] = None
) -> dict:
    updates: dict[str, Any] = {"status": status}
    if best_version_id:
        updates["best_version_id"] = best_version_id
    res = client.table("items").update(updates).eq("id", item_id).execute()
    return res.data[0]


def save_feedback_draft(client: Client, item_id: str, draft: str) -> None:
    """Persist a user's in-progress iteration feedback before the AI call.
    Used to recover the typed text if anything goes wrong mid-iteration."""
    if not item_id:
        return
    try:
        (
            client.table("items")
            .update({"feedback_draft": (draft or "")})
            .eq("id", item_id)
            .execute()
        )
    except Exception:
        # Best-effort save — never block the iteration on a draft write
        pass


def clear_feedback_draft(client: Client, item_id: str) -> None:
    """Clear a previously-saved iteration feedback draft (call on success)."""
    if not item_id:
        return
    try:
        (
            client.table("items")
            .update({"feedback_draft": None})
            .eq("id", item_id)
            .execute()
        )
    except Exception:
        pass


def save_manual_edit_draft(client: Client, item_id: str, payload: dict) -> None:
    """Persist a user's in-progress manual-refine edits (title / body /
    keywords + the version they were editing).  Stored as JSONB so we can
    overwrite atomically; restoration only applies when ``base_version_id``
    matches the currently-displayed version, so switching the "best" pick
    doesn't surface a stale draft against the wrong baseline."""
    if not item_id or not isinstance(payload, dict):
        return
    try:
        (
            client.table("items")
            .update({"manual_edit_draft": payload})
            .eq("id", item_id)
            .execute()
        )
    except Exception:
        pass


def clear_manual_edit_draft(client: Client, item_id: str) -> None:
    """Drop the manual-refine draft, e.g. after a successful save."""
    if not item_id:
        return
    try:
        (
            client.table("items")
            .update({"manual_edit_draft": None})
            .eq("id", item_id)
            .execute()
        )
    except Exception:
        pass


# ── Version CRUD ───────────────────────────────────────────────────────────

def create_version(
    client: Client,
    item_id: str,
    ai_engine: str,
    title: str,
    body: str,
    keywords: Optional[list] = None,
    feedback: Optional[str] = None,
    images: Optional[list] = None,
    token_usage: Optional[dict] = None,
) -> dict:
    # Determine next version number
    existing = (
        client.table("versions")
        .select("version_num")
        .eq("item_id", item_id)
        .order("version_num", desc=True)
        .limit(1)
        .execute()
    )
    next_num = (existing.data[0]["version_num"] + 1) if existing.data else 1

    data = {
        "item_id": item_id,
        "version_num": next_num,
        "ai_engine": ai_engine,
        "title": title,
        "body": body,
        "keywords": keywords or [],
        "feedback": feedback,
        "images": images or [],
        "token_usage": token_usage or {},
    }
    res = client.table("versions").insert(data).execute()
    return res.data[0]


def list_versions(client: Client, item_id: str) -> list[dict]:
    res = (
        client.table("versions")
        .select("*")
        .eq("item_id", item_id)
        .order("version_num")
        .execute()
    )
    return res.data or []


def get_latest_version(client: Client, item_id: str) -> Optional[dict]:
    res = (
        client.table("versions")
        .select("*")
        .eq("item_id", item_id)
        .order("version_num", desc=True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def get_batch_item_counts(client: Client, batch_ids: list[str]) -> dict:
    """
    Fetch item counts by status for multiple batches in a single query.
    Returns {batch_id: {"total": N, "approved": N, "pending": N, "needs_revision": N}}
    """
    if not batch_ids:
        return {}
    res = (
        client.table("items")
        .select("batch_id, status")
        .in_("batch_id", batch_ids)
        .execute()
    )
    counts: dict[str, dict] = {}
    for row in (res.data or []):
        bid = row["batch_id"]
        if bid not in counts:
            counts[bid] = {"total": 0, "approved": 0, "pending": 0, "needs_revision": 0}
        counts[bid]["total"] += 1
        status = row.get("status", "pending")
        if status in counts[bid]:
            counts[bid][status] += 1
    return counts


def get_recent_titles_and_openings(
    client: Client, project_id: str, limit: int = 150
) -> list[dict]:
    """
    Fetch title + first-line opening of each content item across a wide recent
    window, for cross-batch deduplication.

    Covers the last 40 batches and all items regardless of status — rejected/
    pending items still pollute future output if we let the model re-invent the
    same angles.  Each entry is ``{"title": str, "opening": str}`` where opening
    is the first non-empty line of the body, truncated to 25 characters.

    Two-query implementation: first pull items (id + best_version_id), then
    pull their versions in one batched ``in_`` call.  Replaces the previous
    nested ``select("..., versions(*)")`` which was N+1-ish on a wide window
    (postgrest expanded the embedded select per item server-side) and
    returned an order of magnitude more data than needed.
    """
    batches = list_batches(client, project_id, limit=40)
    if not batches:
        return []
    batch_ids = [b["id"] for b in batches]

    items_res = (
        client.table("items")
        .select("id, status, best_version_id")
        .in_("batch_id", batch_ids)
        .execute()
    )
    items = items_res.data or []
    if not items:
        return []
    item_ids = [it["id"] for it in items]

    # Pull every version for these items in one shot; we only need the three
    # columns used for picking the canonical version.
    versions_res = (
        client.table("versions")
        .select("id, item_id, title, body, version_num")
        .in_("item_id", item_ids)
        .execute()
    )
    versions_by_item: dict[str, list[dict]] = {}
    for v in (versions_res.data or []):
        versions_by_item.setdefault(v["item_id"], []).append(v)

    out: list[dict] = []
    for item in items:
        versions = versions_by_item.get(item["id"], [])
        if not versions:
            continue

        chosen = None
        best_vid = item.get("best_version_id")
        if best_vid:
            for v in versions:
                if v.get("id") == best_vid:
                    chosen = v
                    break
        if not chosen:
            chosen = max(versions, key=lambda v: v.get("version_num", 0))

        title = (chosen.get("title") or "").strip()
        if not title or title == "（解析失败）":
            continue

        body = (chosen.get("body") or "").strip()
        first_line = next((ln for ln in body.splitlines() if ln.strip()), "")
        opening = first_line.strip()[:25]

        out.append({"title": title, "opening": opening})

    return out[:limit]


def get_recent_titles(client: Client, project_id: str, limit: int = 100) -> list[str]:
    """Backwards-compatible wrapper returning just titles."""
    return [t["title"] for t in get_recent_titles_and_openings(client, project_id, limit=limit)]


# ── Memory CRUD ────────────────────────────────────────────────────────────

def list_memories(
    client: Client,
    user_id: str,
    scope: Optional[str] = None,
    project_id: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict]:
    q = client.table("memories").select("*").eq("user_id", user_id)
    if scope:
        q = q.eq("scope", scope)
    if project_id:
        q = q.eq("project_id", project_id)
    if status:
        q = q.eq("status", status)
    res = q.order("frequency", desc=True).execute()
    return res.data or []


def _invalidate_memory_caches() -> None:
    """Drop every memory-related cache after a write so the next read pulls fresh
    rows.  Called from every memory mutator."""
    for fn in (get_confirmed_memories, get_session_instructions, list_example_items):
        try:
            fn.clear()
        except Exception:
            pass


def upsert_memory(
    client: Client,
    user_id: str,
    scope: str,
    content: str,
    source_feedback: str,
    project_id: Optional[str] = None,
    auto_confirm_threshold: int = 3,
    force_confirmed: bool = False,
    severity: str = "soft",
    applicability: Optional[str] = None,
) -> dict:
    """
    Insert a new memory candidate or increment frequency of an existing one.

    ``force_confirmed`` (used by the AI merger) creates the row already in the
    ``confirmed`` state, skipping the frequency threshold — callers that set
    this flag have already decided the rule is intentional.
    """
    # Try to find an existing memory with the same content
    q = (
        client.table("memories")
        .select("*")
        .eq("user_id", user_id)
        .eq("scope", scope)
        .eq("content", content)
    )
    if project_id:
        q = q.eq("project_id", project_id)
    existing = q.execute()

    if existing.data:
        row = existing.data[0]
        new_freq = row["frequency"] + 1
        if force_confirmed or new_freq >= auto_confirm_threshold:
            new_status = "confirmed"
        else:
            new_status = row["status"]
        res = (
            client.table("memories")
            .update({"frequency": new_freq, "status": new_status})
            .eq("id", row["id"])
            .execute()
        )
        _invalidate_memory_caches()
        return res.data[0]
    else:
        data: dict[str, Any] = {
            "scope": scope,
            "content": content,
            "source_feedback": source_feedback,
            "user_id": user_id,
            "frequency": 1,
            "status": "confirmed" if force_confirmed else "candidate",
        }
        if project_id:
            data["project_id"] = project_id
        # Severity / applicability are new columns added by the additive
        # migration block in CREATE_TABLES_SQL.  Try with them first; if the
        # column doesn't exist yet (older deployment), retry without so the
        # write still succeeds and the row degrades to "soft / global".
        if severity and severity.lower() in ("hard", "soft"):
            data["severity"] = severity.lower()
        if applicability:
            data["applicability"] = applicability[:32]

        # Compute the embedding once at write time so the relevance ranker
        # can use it without paying an API call per generation.  Only soft
        # rules are filtered; hard rules always inject, but we still embed
        # so the data is uniform.  Embedding failure is non-fatal.
        try:
            import dedup as _dedup
            if _dedup.embeddings_available():
                vecs = _dedup.embed_texts([content])
                if vecs and vecs[0]:
                    data["embedding"] = vecs[0]
        except Exception:
            pass

        try:
            res = client.table("memories").insert(data).execute()
        except Exception:
            # New columns missing from older deployments — strip and retry
            data.pop("severity", None)
            data.pop("applicability", None)
            data.pop("embedding", None)
            res = client.table("memories").insert(data).execute()
        _invalidate_memory_caches()
        return res.data[0]


def backfill_memory_embeddings(
    client: Client, user_id: str, max_rows: int = 50
) -> int:
    """Best-effort backfill: find up to ``max_rows`` memories owned by
    ``user_id`` that have no embedding stored, compute them, and write back.
    Returns the number of rows successfully embedded.

    Called from the memory manager UI button.  Bounded per call to keep the
    user's click responsive and to amortise embedding API spend across
    sessions.
    """
    try:
        import dedup as _dedup
    except Exception:
        return 0
    if not _dedup.embeddings_available():
        return 0
    try:
        res = (
            client.table("memories")
            .select("id, content, embedding")
            .eq("user_id", user_id)
            .is_("embedding", "null")
            .limit(max_rows)
            .execute()
        )
    except Exception:
        return 0
    rows = res.data or []
    if not rows:
        return 0
    texts = [r.get("content", "") for r in rows]
    vecs = _dedup.embed_texts(texts)
    if not vecs or len(vecs) != len(rows):
        return 0
    updated = 0
    for r, v in zip(rows, vecs):
        if not v:
            continue
        try:
            client.table("memories").update({"embedding": v}).eq("id", r["id"]).execute()
            updated += 1
        except Exception:
            pass
    if updated:
        _invalidate_memory_caches()
    return updated


def increment_memory_frequency(client: Client, memory_id: str) -> dict:
    """
    Bump an existing memory's frequency counter and mark it confirmed.  Used
    by the AI merger's ``merge`` path when a new feedback is deemed semantically
    equivalent to an existing rule.
    """
    row = (
        client.table("memories").select("*").eq("id", memory_id).execute()
    )
    if not row.data:
        raise ValueError(f"memory {memory_id} not found")
    cur = row.data[0]
    res = (
        client.table("memories")
        .update({"frequency": cur.get("frequency", 1) + 1, "status": "confirmed"})
        .eq("id", memory_id)
        .execute()
    )
    _invalidate_memory_caches()
    return res.data[0]


def update_memory(client: Client, memory_id: str, updates: dict) -> dict:
    res = (
        client.table("memories").update(updates).eq("id", memory_id).execute()
    )
    _invalidate_memory_caches()
    return res.data[0]


def delete_memory(client: Client, memory_id: str) -> None:
    client.table("memories").delete().eq("id", memory_id).execute()
    _invalidate_memory_caches()


def set_item_example_label(
    client: Client, item_id: str, label: Optional[str]
) -> dict:
    """Set or clear the example_label on an item ('positive', 'negative', or None)."""
    res = (
        client.table("items")
        .update({"example_label": label})
        .eq("id", item_id)
        .execute()
    )
    try:
        list_example_items.clear()
    except Exception:
        pass
    return res.data[0]


@_cache_data(ttl=120, show_spinner=False)
def list_example_items(
    _client: Client, project_id: str, label: str, limit: int = 5
) -> list[dict]:
    """
    Return recent items marked with the given label ('positive' or 'negative').
    Each dict has {title, body} from the item's best or latest version.
    """
    batches = list_batches(_client, project_id, limit=50)
    if not batches:
        return []
    batch_ids = [b["id"] for b in batches]

    res = (
        _client.table("items")
        .select("id, best_version_id, versions(id, title, body, version_num)")
        .in_("batch_id", batch_ids)
        .eq("example_label", label)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )

    examples: list[dict] = []
    for item in (res.data or []):
        item_versions = item.get("versions", [])
        if not item_versions:
            continue
        best_vid = item.get("best_version_id")
        chosen = None
        if best_vid:
            for v in item_versions:
                if v.get("id") == best_vid:
                    chosen = v
                    break
        if not chosen:
            chosen = max(item_versions, key=lambda v: v.get("version_num", 0))
        title = (chosen.get("title") or "").strip()
        body = (chosen.get("body") or "").strip()
        if title or body:
            examples.append({"title": title, "body": body})
    return examples


def _is_rule_memory(row: dict) -> bool:
    """True if a memory row should be treated as a durable rule (default for
    rows predating the ``memory_type`` column)."""
    mt = row.get("memory_type")
    return mt is None or mt == "rule"


def _rank_memories_for_injection(mems: list[dict], cap: int) -> list[dict]:
    """Rank rule memories for System Prompt injection.

    Two-tier selection so that fresh captures — especially the frequency=1
    rules created by the merger's ``rule`` path — are never starved by a
    backlog of older high-frequency rules:

      1. Rules created within the last 7 days are always included (newest
         first).  This guarantees "I just said it → it took effect".
      2. Remaining slots (cap - len(recent)) are filled from older rules by
         ``frequency DESC`` then ``created_at DESC``.

    Hard ceiling at ``cap``: in the pathological case where the user creates
    more than ``cap`` rules in a week, we still truncate (newest kept).
    """
    if not mems:
        return mems
    if not cap or cap <= 0:
        return sorted(
            mems,
            key=lambda m: (int(m.get("frequency") or 0), str(m.get("created_at") or "")),
            reverse=True,
        )

    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    recent: list[dict] = []
    older: list[dict] = []
    for m in mems:
        created = str(m.get("created_at") or "")
        (recent if created >= cutoff else older).append(m)

    recent.sort(key=lambda m: str(m.get("created_at") or ""), reverse=True)
    older.sort(
        key=lambda m: (int(m.get("frequency") or 0), str(m.get("created_at") or "")),
        reverse=True,
    )

    recent = recent[:cap]
    remaining = max(0, cap - len(recent))
    return recent + older[:remaining]


@_cache_data(ttl=60, show_spinner=False)
def get_confirmed_memories(
    _client: Client,
    user_id: str,
    project_id: Optional[str] = None,
    cap_per_scope: Optional[int] = None,
) -> tuple[list[dict], list[dict]]:
    """
    Returns (global_memories, project_memories), both filtered to 'confirmed'.

    Hard rules (``severity='hard'``) bypass ``cap_per_scope`` and are always
    injected in full — they're compliance / brand lines.  Soft rules are
    ranked (recent first, then frequency) and capped per scope.

    Session-typed memories are excluded — they load through
    :func:`get_session_instructions` into a separate high-priority prompt slot.

    Rows with a future ``muted_until`` are filtered out so the user can
    temporarily silence a rule without deleting it.
    """
    if cap_per_scope is None:
        cap_per_scope = int(getattr(config, "MAX_INJECTED_MEMORIES_PER_SCOPE", 12) or 12)

    def _split_and_rank(mems: list[dict]) -> list[dict]:
        rule_mems = [m for m in mems if _is_rule_memory(m) and not _is_muted(m)]
        hard = [m for m in rule_mems if (m.get("severity") or "soft").lower() == "hard"]
        soft = [m for m in rule_mems if (m.get("severity") or "soft").lower() != "hard"]
        return hard + _rank_memories_for_injection(soft, cap_per_scope)

    global_mems = _split_and_rank(
        list_memories(_client, user_id, scope="global", status="confirmed")
    )
    project_mems: list[dict] = []
    if project_id:
        project_mems = _split_and_rank(
            list_memories(
                _client, user_id, scope="project",
                project_id=project_id, status="confirmed",
            )
        )
    return global_mems, project_mems


def _is_muted(memory_row: dict) -> bool:
    """True if the memory has a ``muted_until`` timestamp in the future."""
    muted = memory_row.get("muted_until")
    if not muted:
        return False
    try:
        # Both ISO strings and datetime objects show up here depending on the
        # Supabase driver; compare as strings since they're all UTC-ISO.
        now_iso = datetime.utcnow().isoformat()
        return str(muted) > now_iso
    except Exception:
        return False


@_cache_data(ttl=30, show_spinner=False)
def get_session_instructions(
    _client: Client,
    user_id: str,
    project_id: Optional[str] = None,
) -> list[dict]:
    """
    Fetch unexpired session-level instructions for the current user/project.

    Session memories carry ad-hoc instructions that the user gave mid-flow
    ("from now on avoid numbers in titles") — they live outside the frequency/
    candidate/confirmed pipeline and inject at the highest priority of the
    system prompt.  Returns [] if the ``memory_type`` column isn't present yet
    (i.e. schema migration hasn't been run), so the feature degrades cleanly.
    """
    try:
        q = (
            _client.table("memories")
            .select("*")
            .eq("user_id", user_id)
            .eq("memory_type", "session")
        )
        if project_id:
            q = q.or_(f"project_id.eq.{project_id},project_id.is.null")
        res = q.order("created_at", desc=True).execute()
    except Exception:
        return []

    rows = res.data or []
    now_iso = datetime.utcnow().isoformat()
    fresh: list[dict] = []
    for row in rows:
        expires = row.get("expires_at")
        if expires and expires < now_iso:
            continue
        fresh.append(row)
    return fresh


def insert_session_instruction(
    client: Client,
    user_id: str,
    content: str,
    source_feedback: str = "",
    project_id: Optional[str] = None,
    source_batch_id: Optional[str] = None,
    ttl_hours: int = 24,
) -> Optional[dict]:
    """
    Insert a session-level memory row.  Returns None if the schema migration
    hasn't run yet (so callers can silently skip the feature).
    """
    from datetime import timedelta
    expires_at = (datetime.utcnow() + timedelta(hours=max(1, ttl_hours))).isoformat()
    payload: dict[str, Any] = {
        "scope": "project" if project_id else "global",
        "content": content.strip(),
        "source_feedback": source_feedback or "会话指令",
        "user_id": user_id,
        "frequency": 1,
        "status": "confirmed",
        "memory_type": "session",
        "expires_at": expires_at,
    }
    if project_id:
        payload["project_id"] = project_id
    if source_batch_id:
        payload["source_batch_id"] = source_batch_id
    try:
        res = client.table("memories").insert(payload).execute()
        _invalidate_memory_caches()
        return (res.data or [None])[0]
    except Exception:
        return None
