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
    tactics      JSONB DEFAULT '[]'::jsonb,
    reference_files JSONB DEFAULT '[]'::jsonb,
    default_params  JSONB DEFAULT '{}'::jsonb,
    owner_id     UUID NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
CREATE POLICY IF NOT EXISTS projects_owner ON projects
    USING (owner_id = auth.uid());

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

-- Memories (project-level or global)
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
        "params": json.dumps(params),
        "ai_engines": json.dumps(ai_engines),
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


# ── Item CRUD ──────────────────────────────────────────────────────────────

def create_item(client: Client, user_id: str, batch_id: str) -> dict:
    data = {"batch_id": batch_id, "user_id": user_id}
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
        "keywords": json.dumps(keywords or []),
        "feedback": feedback,
        "images": json.dumps(images or []),
        "token_usage": json.dumps(token_usage or {}),
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
) -> dict:
    """
    Insert a new memory candidate or increment frequency of an existing one.
    Auto-promotes to 'confirmed' when frequency reaches the threshold.
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
        new_status = "confirmed" if new_freq >= auto_confirm_threshold else row["status"]
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
            "status": "candidate",
        }
        if project_id:
            data["project_id"] = project_id
        res = client.table("memories").insert(data).execute()
        return res.data[0]


def update_memory(client: Client, memory_id: str, updates: dict) -> dict:
    res = (
        client.table("memories").update(updates).eq("id", memory_id).execute()
    )
    return res.data[0]


def delete_memory(client: Client, memory_id: str) -> None:
    client.table("memories").delete().eq("id", memory_id).execute()


def get_confirmed_memories(
    client: Client,
    user_id: str,
    project_id: Optional[str] = None,
) -> tuple[list[dict], list[dict]]:
    """
    Returns (global_memories, project_memories) both filtered to 'confirmed'.
    """
    global_mems = list_memories(
        client, user_id, scope="global", status="confirmed"
    )
    project_mems: list[dict] = []
    if project_id:
        project_mems = list_memories(
            client, user_id, scope="project", project_id=project_id, status="confirmed"
        )
    return global_mems, project_mems
