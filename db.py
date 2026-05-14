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


def get_client(access_token: Optional[str] = None) -> Client:
    """Return a Supabase client, optionally authenticated with the user JWT."""
    client = create_client(config.SUPABASE_URL, config.SUPABASE_ANON_KEY)
    if access_token:
        client.postgrest.auth(access_token)
    return client


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
CREATE INDEX IF NOT EXISTS memories_session_idx
    ON memories(user_id, memory_type, expires_at)
    WHERE memory_type = 'session';
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
CREATE POLICY IF NOT EXISTS memories_owner ON memories
    USING (user_id = auth.uid());
"""


# ── Project CRUD ──────────────────────────────────────────────────────────

def list_projects(client: Client, user_id: str) -> list[dict]:
    res = (
        client.table("projects")
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
    return res.data[0]


def delete_project(client: Client, project_id: str) -> None:
    client.table("projects").delete().eq("id", project_id).execute()


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
    return res.data[0]


def list_batches(client: Client, project_id: str, limit: int = 20) -> list[dict]:
    res = (
        client.table("batches")
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
    """
    batches = list_batches(client, project_id, limit=40)
    if not batches:
        return []
    batch_ids = [b["id"] for b in batches]

    res = (
        client.table("items")
        .select("id, status, best_version_id, versions(id, title, body, version_num)")
        .in_("batch_id", batch_ids)
        .execute()
    )

    out: list[dict] = []
    for item in (res.data or []):
        versions = item.get("versions", [])
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


def upsert_memory(
    client: Client,
    user_id: str,
    scope: str,
    content: str,
    source_feedback: str,
    project_id: Optional[str] = None,
    auto_confirm_threshold: int = 3,
    force_confirmed: bool = False,
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
        res = client.table("memories").insert(data).execute()
        return res.data[0]


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
    return res.data[0]


def update_memory(client: Client, memory_id: str, updates: dict) -> dict:
    res = (
        client.table("memories").update(updates).eq("id", memory_id).execute()
    )
    return res.data[0]


def delete_memory(client: Client, memory_id: str) -> None:
    client.table("memories").delete().eq("id", memory_id).execute()


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
    return res.data[0]


def list_example_items(
    client: Client, project_id: str, label: str, limit: int = 5
) -> list[dict]:
    """
    Return recent items marked with the given label ('positive' or 'negative').
    Each dict has {title, body} from the item's best or latest version.
    """
    batches = list_batches(client, project_id, limit=50)
    if not batches:
        return []
    batch_ids = [b["id"] for b in batches]

    res = (
        client.table("items")
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


def get_confirmed_memories(
    client: Client,
    user_id: str,
    project_id: Optional[str] = None,
    cap_per_scope: Optional[int] = None,
) -> tuple[list[dict], list[dict]]:
    """
    Returns (global_memories, project_memories), both filtered to 'confirmed'
    and capped at ``cap_per_scope`` items per scope (default pulled from
    ``config.MAX_INJECTED_MEMORIES_PER_SCOPE``).

    Session-typed memories are excluded — they load through
    :func:`get_session_instructions` into a separate high-priority prompt slot.
    """
    if cap_per_scope is None:
        cap_per_scope = int(getattr(config, "MAX_INJECTED_MEMORIES_PER_SCOPE", 40) or 40)

    global_mems = [
        m for m in list_memories(client, user_id, scope="global", status="confirmed")
        if _is_rule_memory(m)
    ]
    project_mems: list[dict] = []
    if project_id:
        project_mems = [
            m for m in list_memories(
                client, user_id, scope="project",
                project_id=project_id, status="confirmed",
            )
            if _is_rule_memory(m)
        ]
    return (
        _rank_memories_for_injection(global_mems, cap_per_scope),
        _rank_memories_for_injection(project_mems, cap_per_scope),
    )


def get_session_instructions(
    client: Client,
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
            client.table("memories")
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
        return (res.data or [None])[0]
    except Exception:
        return None
