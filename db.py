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
import re
from typing import Any, Optional
from datetime import datetime, timezone

from supabase import create_client, Client
import config
import telemetry

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
-- PostgreSQL 不支持 CREATE POLICY IF NOT EXISTS；用 DROP + CREATE 实现幂等
DROP POLICY IF EXISTS projects_owner ON projects;
CREATE POLICY projects_owner ON projects
    USING (owner_id = auth.uid());
-- Migration: add dual-prompt columns if upgrading from older schema
ALTER TABLE projects ADD COLUMN IF NOT EXISTS system_prompt_tone TEXT;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS system_prompt_exec TEXT;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS calibration_notes TEXT;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS custom_roles JSONB DEFAULT '[]'::jsonb;
-- 2026-05 Day 2: 项目级语义去重阈值与队列策略（NULL = 用 config 全局默认）
ALTER TABLE projects ADD COLUMN IF NOT EXISTS
    semantic_dedup_threshold REAL NULL
    CHECK (semantic_dedup_threshold IS NULL
           OR (semantic_dedup_threshold >= 0.80 AND semantic_dedup_threshold <= 0.99));
ALTER TABLE projects ADD COLUMN IF NOT EXISTS
    queue_strategy TEXT NULL
    CHECK (queue_strategy IS NULL OR queue_strategy IN ('stable','throughput'));

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
DROP POLICY IF EXISTS batches_owner ON batches;
CREATE POLICY batches_owner ON batches
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
-- Marks an item as a positive / negative example for the project. Must be
-- declared AFTER items CREATE TABLE, otherwise冷启动新库会跑到这里时表还
-- 不存在 → "relation items does not exist" → 整段 DDL 中断。
ALTER TABLE items ADD COLUMN IF NOT EXISTS example_label TEXT
    CHECK (example_label IN ('positive', 'negative'));
ALTER TABLE items ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS items_owner ON items;
CREATE POLICY items_owner ON items
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
DROP POLICY IF EXISTS versions_owner ON versions;
CREATE POLICY versions_owner ON versions
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

-- 2026-05 Day 3: 硬规则结构化字段（rule_kind 决定 rule_payload 的解释）
ALTER TABLE memories ADD COLUMN IF NOT EXISTS
    rule_kind TEXT NULL
    CHECK (rule_kind IS NULL OR rule_kind IN
        ('forbidden_word','required_phrase','max_len','forbidden_regex','free_text'));
ALTER TABLE memories ADD COLUMN IF NOT EXISTS rule_payload JSONB NULL;
CREATE INDEX IF NOT EXISTS memories_rule_kind_idx
    ON memories(user_id, rule_kind) WHERE rule_kind IS NOT NULL;

-- 调教笔记审计：每次写入都留底（before / append_lines / after），方便
-- 排查"为什么这条观察突然出现/消失了"。RLS 按 project_id 关联到用户。
CREATE TABLE IF NOT EXISTS calibration_note_audit (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id    UUID REFERENCES projects(id) ON DELETE CASCADE,
    source        TEXT,                    -- iteration / manual_edit / batch_reflection / merger_taste / user_manual / unknown
    before_text   TEXT,                    -- 写入前的全文
    append_lines  JSONB DEFAULT '[]'::jsonb, -- 本次新增的观察行列表
    after_text    TEXT,                    -- 写入后的全文
    created_at    TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE calibration_note_audit ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS calibration_note_audit_owner ON calibration_note_audit;
CREATE POLICY calibration_note_audit_owner ON calibration_note_audit
    USING (
        project_id IN (SELECT id FROM projects WHERE owner_id = auth.uid())
    );
CREATE INDEX IF NOT EXISTS calibration_note_audit_project_idx
    ON calibration_note_audit(project_id, created_at DESC);
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS memories_owner ON memories;
CREATE POLICY memories_owner ON memories
    USING (user_id = auth.uid());

-- 2026-05 Day 5: 批次指标持久化（phase_ms / counters / injection summary）
-- 历史页查"哪一批慢/重/违规多"，免去每次都翻 stdout 日志。
CREATE TABLE IF NOT EXISTS batch_metrics (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    batch_id     UUID REFERENCES batches(id) ON DELETE CASCADE,
    project_id   UUID REFERENCES projects(id) ON DELETE CASCADE,
    user_id      UUID NOT NULL,
    phase_ms     JSONB DEFAULT '{}'::jsonb,
    counters     JSONB DEFAULT '{}'::jsonb,
    meta         JSONB DEFAULT '{}'::jsonb,
    injection    JSONB DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE batch_metrics ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS batch_metrics_owner ON batch_metrics;
CREATE POLICY batch_metrics_owner ON batch_metrics
    USING (user_id = auth.uid());
CREATE INDEX IF NOT EXISTS batch_metrics_project_idx
    ON batch_metrics(project_id, created_at DESC);

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
    projects, batches, items, versions, memories, batch_metrics
    TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON
    projects, batches, items, versions, memories, batch_metrics
    TO service_role;

-- ─────────────────────────────────────────────────────────────────────────
-- 服务端聚合 RPC：batch_item_counts
--
-- 历史上 ``get_batch_item_counts`` 走 PostgREST 的 ``select("batch_id, status")
-- .in_("batch_id", batch_ids)``，把全部行拉到 client 在 Python 里 GROUP BY。
-- N 个 batch × 平均 5 个 item = 5N 行 payload；进入历史页时每次 rerun 都拉一
-- 次，浪费明显。改走 RPC 让 PG 服务端做 GROUP BY，只回 N 行聚合结果。
--
-- SECURITY INVOKER（默认）：函数以调用者身份执行，RLS 自动按 user_id 过滤；
-- 不需要在 SQL 里显式写 ``WHERE user_id = auth.uid()``。
-- ``STABLE``：同一事务内多次调用同参返回相同结果，PG 优化器可以缓存。
CREATE OR REPLACE FUNCTION batch_item_counts(batch_ids UUID[])
RETURNS TABLE(
    batch_id        UUID,
    total           BIGINT,
    approved        BIGINT,
    pending         BIGINT,
    needs_revision  BIGINT
)
LANGUAGE sql
STABLE
SECURITY INVOKER
AS $$
    SELECT
        i.batch_id,
        COUNT(*)                                                AS total,
        COUNT(*) FILTER (WHERE i.status = 'approved')           AS approved,
        COUNT(*) FILTER (WHERE i.status = 'pending')            AS pending,
        COUNT(*) FILTER (WHERE i.status = 'needs_revision')     AS needs_revision
    FROM items i
    WHERE i.batch_id = ANY(batch_ids)
    GROUP BY i.batch_id;
$$;
GRANT EXECUTE ON FUNCTION batch_item_counts(UUID[]) TO authenticated, service_role;
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

    # 列缺失精确兜底：用户的 Supabase 部署可能没运行 Day 2 / Day 3 / Day 5 等
    # ALTER TABLE 迁移；这时往 ``queue_strategy`` / ``semantic_dedup_threshold``
    # / ``system_prompt_tone`` 等"新列"里写值会撞 PGRST204
    # "Could not find the 'X' column of 'projects' in the schema cache"。
    # 之前是直接红屏让用户保存不了项目设置；现在剥掉缺失列后重试一次，让
    # name / brand 等核心字段照常保存，新列只是不生效，并埋一行 telemetry
    # 提醒运维去跑迁移。
    _NEW_COLUMNS = (
        "semantic_dedup_threshold", "queue_strategy",
        "system_prompt_tone", "system_prompt_exec",
        "custom_roles", "calibration_notes",
    )
    try:
        res = (
            client.table("projects")
            .update(updates)
            .eq("id", project_id)
            .execute()
        )
    except Exception as exc:
        msg = str(exc)
        # 只在错误明确指向"列缺失"且涉及已知新列时才剥列重试
        hit_cols = [c for c in _NEW_COLUMNS if c in msg and c in updates]
        if not hit_cols:
            raise
        telemetry.log_event(
            "update_project_schema_fallback",
            project_id=project_id,
            missing_columns=hit_cols,
            error=msg[:200],
        )
        stripped = {k: v for k, v in updates.items() if k not in hit_cols}
        if not stripped:
            # 这次写入的全部字段都是"新列"，剥完什么都没了，直接返回当前行
            cur = client.table("projects").select("*").eq("id", project_id).execute()
            return (cur.data or [{}])[0]
        res = (
            client.table("projects")
            .update(stripped)
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
    # list_items 也要 invalidate（cache key 含 batch_id；这里宽口径全 clear，
    # cache 体积小成本可忽略）
    try:
        list_items.clear()
    except Exception:
        pass


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
    try:
        list_items.clear()
    except Exception:
        pass
    return res.data or []


def update_version_content(
    client: Client,
    version_id: str,
    title: str,
    body: str,
    keywords: Optional[list] = None,
    token_usage: Optional[dict] = None,
    embedding: Optional[list[float]] = None,
) -> None:
    """重写一条 version 的内容（用于自动重生场景）。

    只更新传入的字段；id / item_id / version_num / ai_engine / created_at 不动。
    embedding 单独可选，失败时静默——pgvector 列没迁移的部署还能用。
    """
    updates: dict[str, Any] = {"title": title, "body": body}
    if keywords is not None:
        updates["keywords"] = keywords
    if token_usage is not None:
        updates["token_usage"] = token_usage
    try:
        client.table("versions").update(updates).eq("id", version_id).execute()
    except Exception:
        pass
    try:
        list_items.clear()
    except Exception:
        pass
    if embedding is not None:
        try:
            client.table("versions").update({"embedding": embedding}).eq("id", version_id).execute()
        except Exception:
            pass


def update_version_embedding(
    client: Client, version_id: str, vec: list[float]
) -> bool:
    """Persist a 768-dim embedding for a single version row.  Tolerant of
    older deployments where the pgvector column hasn't been added yet —
    fails silently so the generation path doesn't blow up on a missing
    column.  Returns True on success, False on failure."""
    if not vec:
        return False
    try:
        client.table("versions").update({"embedding": vec}).eq("id", version_id).execute()
        return True
    except Exception as exc:
        telemetry.log_event(
            "embedding_row_update_failed",
            version_id=version_id, error=str(exc)[:200],
        )
        return False


def bulk_update_version_embeddings(
    client: Client,
    rows: list[dict],
    failed_sink: Optional[list[str]] = None,
) -> None:
    """Persist many version embeddings in one round trip.  Each row needs
    ``{"id": <version_id>, "embedding": [..]}``.  Falls back to per-row
    UPDATE if upsert isn't allowed by RLS for this user.

    Best-effort: errors are swallowed so an embedding failure never blocks
    the user's batch — but with full telemetry so累积衰减可以被发现.
    ``failed_sink`` 给调用方一个直接拿到本次失败 version_id 列表的入口，
    UI 可以在批次完成后展示"⚠ N 条版本缺少向量"提示。
    """
    if not rows:
        return
    try:
        client.table("versions").upsert(rows).execute()
        return
    except Exception as exc:
        err_msg = str(exc)
        telemetry.log_event(
            "embedding_upsert_fallback",
            count=len(rows), error=err_msg[:200],
        )
        # 如果错误明确指向"列不存在"（pgvector 迁移没跑），不再逐行重试 N 次：
        # 直接把所有 id 标记 failed，省 N-1 次必败的 round trip + 日志风暴。
        low = err_msg.lower()
        if (
            "embedding" in low
            and ("column" in low or "does not exist" in low or "schema" in low)
        ):
            telemetry.log_event(
                "embedding_column_missing",
                hint="run versions.embedding pgvector migration",
            )
            if failed_sink is not None:
                for r in rows:
                    failed_sink.append(r.get("id", ""))
            return
    # Per-row fallback
    for r in rows:
        ok = update_version_embedding(client, r.get("id", ""), r.get("embedding") or [])
        if not ok and failed_sink is not None:
            failed_sink.append(r.get("id", ""))


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
    # 写入新 version 后 list_items 的 versions(*) 嵌套结果就过时了
    try:
        list_items.clear()
    except Exception:
        pass
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


@_cache_data(ttl=30, show_spinner=False)
def list_items(_client: Client, batch_id: str) -> list[dict]:
    """List items + their versions for one batch.

    30s cache：审核页用户连续点"通过/打回/迭代"按钮时不重复查 DB。所有写
    items / versions 的路径都要在写入后调用 ``list_items.clear()``——这种
    write-through invalidation 是手动的，遗漏点会让 UI 显示旧状态。已知调
    用点（必须 clear）：
      - update_item_status                — items.status
      - set_item_example_label            — items.example_label
      - delete_batch                      — cascade 删 items
      - bulk_create_initial_versions      — 新建 versions（影响 list_items 的 versions(*) 嵌套）
      - bulk_create_items                  — 新建 items
      - update_version_content            — versions.title/body/keywords
    cache 失败回原始查询（_cache_data shim 在 no-streamlit 环境是 no-op）。
    """
    res = (
        _client.table("items")
        .select("*, versions(*)")
        .eq("batch_id", batch_id)
        .order("created_at")
        .execute()
    )
    return res.data or []


def list_items_for_batches(
    client: Client, batch_ids: list[str]
) -> dict[str, list[dict]]:
    """Bulk-fetch items for many batches in one round trip.

    Returns ``{batch_id: [items]}``. 给 page_export 这种需要遍历多个 batch
    的入口用，消除 N+1 — 之前每个 batch 一次 query，50 个 batch 就要 50
    次 RT。现在一次 ``in_(batch_ids)`` 拿回全部，client 侧按 batch_id 分桶。

    Streamlit cache 不做这里：export 页交互低频，但 batch_ids 集合频繁
    变化（用户勾选），cache_data 反而命中率低。
    """
    if not batch_ids:
        return {}
    try:
        res = (
            client.table("items")
            .select("*, versions(*)")
            .in_("batch_id", batch_ids)
            .order("created_at")
            .execute()
        )
    except Exception as exc:
        telemetry.log_event(
            "list_items_for_batches_failed",
            n_batches=len(batch_ids), error=str(exc)[:200],
        )
        return {bid: [] for bid in batch_ids}
    grouped: dict[str, list[dict]] = {bid: [] for bid in batch_ids}
    for item in (res.data or []):
        bid = item.get("batch_id")
        if bid in grouped:
            grouped[bid].append(item)
    return grouped


def update_item_status(
    client: Client, item_id: str, status: str, best_version_id: Optional[str] = None
) -> dict:
    updates: dict[str, Any] = {"status": status}
    if best_version_id:
        updates["best_version_id"] = best_version_id
    res = client.table("items").update(updates).eq("id", item_id).execute()
    try:
        list_items.clear()
    except Exception:
        pass
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
    try:
        list_items.clear()
    except Exception:
        pass
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

    优先走 PG RPC ``batch_item_counts``（服务端 GROUP BY，只回 N 行聚合结果）；
    RPC 不存在（老部署没跑新迁移）或网络错误时 fallback 到 client-side 聚合，
    保证可用性不退化。
    """
    if not batch_ids:
        return {}
    # 路径 A：RPC 服务端聚合
    try:
        res = client.rpc(
            "batch_item_counts", {"batch_ids": batch_ids}
        ).execute()
        rows = res.data or []
        if rows:
            counts: dict[str, dict] = {}
            for row in rows:
                bid = row.get("batch_id")
                if not bid:
                    continue
                counts[bid] = {
                    "total":          int(row.get("total") or 0),
                    "approved":       int(row.get("approved") or 0),
                    "pending":        int(row.get("pending") or 0),
                    "needs_revision": int(row.get("needs_revision") or 0),
                }
            # 即使某些 batch_id 在 items 里完全没行（边界情况），RPC 也不会回
            # 那一行；用 0 填回去保持 caller 看到的语义不变。
            for bid in batch_ids:
                counts.setdefault(bid, {
                    "total": 0, "approved": 0,
                    "pending": 0, "needs_revision": 0,
                })
            return counts
        # rows 为空也是合法结果（所有 batch 都没 item），直接返回 0-填充
        return {
            bid: {"total": 0, "approved": 0, "pending": 0, "needs_revision": 0}
            for bid in batch_ids
        }
    except Exception as exc:
        # 路径 B：fallback —— 老部署 / RPC 函数不存在 / 网络错误
        msg = str(exc)
        telemetry.log_event(
            "batch_item_counts_rpc_fallback", error=msg[:200],
        )
        try:
            res = (
                client.table("items")
                .select("batch_id, status")
                .in_("batch_id", batch_ids)
                .execute()
            )
        except Exception:
            return {}
        counts = {}
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
    rule_kind: Optional[str] = None,
    rule_payload: Optional[dict] = None,
) -> dict:
    """
    Insert a new memory candidate or increment frequency of an existing one.

    ``force_confirmed`` (used by the AI merger) creates the row already in the
    ``confirmed`` state, skipping the frequency threshold — callers that set
    this flag have already decided the rule is intentional.

    Day 3 新增：``rule_kind`` + ``rule_payload`` 用于结构化硬规则
    （``forbidden_word`` / ``required_phrase`` / ``max_len`` / ``forbidden_regex``）。
    迁移未跑的老部署 insert 失败后会自动 strip 这两列重试，保持向后兼容。
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
        if rule_kind and rule_kind in (
            "forbidden_word", "required_phrase", "max_len",
            "forbidden_regex", "free_text",
        ):
            data["rule_kind"] = rule_kind
        if rule_payload is not None:
            data["rule_payload"] = rule_payload

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
        except Exception as exc:
            # 之前 silent pass。本路径非致命（规则没向量也能注入），但持续
            # 失败会导致 soft-rule 相关性筛选完全降级为"全部注入"——用户
            # 看到 system_prompt 暴涨却不知所以。埋一行让运维能查。
            telemetry.log_event(
                "memory_embedding_compute_failed",
                content_preview=content[:60],
                error=str(exc)[:200],
            )

        try:
            res = client.table("memories").insert(data).execute()
        except Exception as exc:
            # 仅在错误明确指向"新列缺失"（未跑迁移）时才剥列重试；其它错误
            # 抛回去让调用方/UI 看见。之前裸 except 会把 RLS 拒绝、唯一冲突、
            # 网络中断都当成"老部署"，导致 rule_kind / rule_payload 静默丢失。
            msg = str(exc)
            new_cols = ("severity", "applicability", "embedding",
                        "rule_kind", "rule_payload")
            if not any(col in msg for col in new_cols):
                raise
            telemetry.log_event(
                "upsert_memory_schema_fallback",
                error=msg[:200],
            )
            for col in new_cols:
                data.pop(col, None)
            res = client.table("memories").insert(data).execute()
        _invalidate_memory_caches()
        return res.data[0]


def insert_calibration_audit(
    client: Client,
    project_id: str,
    source: str,
    before_text: str,
    append_lines: list[str],
    after_text: str,
) -> None:
    """记录一次调教笔记的写入。失败时静默——审计不能拖死主流程。

    旧部署没运行新表迁移时，会被 ``except`` 接住静默丢弃，调用方无感。
    """
    if not project_id:
        return
    try:
        client.table("calibration_note_audit").insert({
            "project_id":   project_id,
            "source":       (source or "unknown")[:32],
            "before_text":  before_text or "",
            "append_lines": append_lines or [],
            "after_text":   after_text or "",
        }).execute()
    except Exception:
        pass


def list_calibration_audit(
    client: Client,
    project_id: str,
    limit: int = 30,
    before_ts: Optional[str] = None,
) -> list[dict]:
    """查看某个项目调教笔记的写入历史（UI 排障用）。

    ``before_ts`` 给分页用：传入上一页最旧一条的 ``created_at`` 字符串，
    返回结果会严格早于该时间戳。不传则返回最新 ``limit`` 条。
    """
    if not project_id:
        return []
    try:
        q = (
            client.table("calibration_note_audit")
            .select("*")
            .eq("project_id", project_id)
            .order("created_at", desc=True)
            .limit(limit)
        )
        if before_ts:
            q = q.lt("created_at", before_ts)
        res = q.execute()
        return res.data or []
    except Exception:
        return []


def insert_batch_metrics(
    client: Client,
    batch_id: str,
    project_id: str,
    user_id: str,
    phase_ms: dict,
    counters: dict,
    meta: dict,
    injection: dict,
) -> None:
    """落一条本批次的指标快照到 ``batch_metrics`` 表。

    Day 5：批次完成后调用，让历史页可以离线查"哪一批慢/重/违规多"，
    不依赖刷 stdout 日志。失败不抛——埋点掉链子不能拖死生成主流程。
    """
    if not batch_id:
        return
    try:
        client.table("batch_metrics").insert({
            "batch_id":   batch_id,
            "project_id": project_id,
            "user_id":    user_id,
            "phase_ms":   phase_ms or {},
            "counters":   counters or {},
            "meta":       meta or {},
            "injection":  injection or {},
        }).execute()
    except Exception as exc:
        telemetry.log_event(
            "batch_metrics_persist_failed",
            batch_id=batch_id, error=str(exc)[:200],
        )


@_cache_data(ttl=60, show_spinner=False)
def list_batch_metrics(
    _client: Client, project_id: str, limit: int = 50,
    user_id: Optional[str] = None,
) -> list[dict]:
    """读最近 N 条批次指标。历史页用，60s 缓存避免重复查询。

    ``user_id`` 仅作为缓存 key 用（RLS 已经按行过滤）。不传也能用，但同
    一会话内多账户切换时可能拿到上一个账户缓存的结果——所以建议传。
    """
    if not project_id:
        return []
    try:
        res = (
            _client.table("batch_metrics")
            .select("*")
            .eq("project_id", project_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []
    except Exception:
        return []


def count_calibration_audit(client: Client, project_id: str) -> int:
    """项目调教笔记历史的总条数。UI 用于显示 "X / Y 条" 标签。"""
    if not project_id:
        return 0
    try:
        res = (
            client.table("calibration_note_audit")
            .select("id", count="exact")
            .eq("project_id", project_id)
            .limit(1)
            .execute()
        )
        return int(getattr(res, "count", 0) or 0)
    except Exception:
        return 0


def backfill_memory_embeddings(
    client: Client, user_id: str, max_rows: int = 50
) -> dict:
    """Best-effort backfill: find up to ``max_rows`` memories owned by
    ``user_id`` that have no embedding stored, compute them, and write back.

    Returns a status dict instead of a bare int so UI can tell apart these
    scenarios that all used to collapse to "0":
      - ``{"status": "ok", "updated": N}``        — 正常补算了 N 条
      - ``{"status": "noop"}``                    — 没有缺向量的记忆需要补
      - ``{"status": "no_embedding_sdk"}``        — 未配 GOOGLE_API_KEY
      - ``{"status": "schema_missing", "hint": …}`` — pgvector 列没建 / 迁移未跑
      - ``{"status": "query_failed", "error": …}`` — 网络 / RLS / 其它

    之前一律返回 int 0，按钮点了显示"没有需要补算的记忆"既包含真正无需补的
    情况，也包含 schema 缺失等真错误，用户无从判断为什么"没反应"。
    """
    try:
        import dedup as _dedup
    except Exception as exc:
        return {"status": "no_embedding_sdk", "error": str(exc)[:200]}
    if not _dedup.embeddings_available():
        return {"status": "no_embedding_sdk"}
    try:
        res = (
            client.table("memories")
            .select("id, content, embedding")
            .eq("user_id", user_id)
            .is_("embedding", "null")
            .limit(max_rows)
            .execute()
        )
    except Exception as exc:
        msg = str(exc)
        telemetry.log_event(
            "backfill_memory_embeddings_query_failed",
            user_id=user_id, error=msg[:200],
        )
        low = msg.lower()
        if (
            "embedding" in low
            and ("column" in low or "does not exist" in low or "schema" in low)
        ):
            return {
                "status": "schema_missing",
                "hint": "memories.embedding 列不存在，需要先跑 pgvector 迁移",
                "error": msg[:200],
            }
        return {"status": "query_failed", "error": msg[:200]}
    rows = res.data or []
    if not rows:
        return {"status": "noop"}
    texts = [r.get("content", "") for r in rows]
    vecs = _dedup.embed_texts(texts)
    if not vecs or len(vecs) != len(rows):
        return {"status": "query_failed", "error": "embed_texts 返回长度不匹配"}
    updated = 0
    for r, v in zip(rows, vecs):
        if not v:
            continue
        try:
            client.table("memories").update({"embedding": v}).eq("id", r["id"]).execute()
            updated += 1
        except Exception as exc:
            telemetry.log_event(
                "backfill_memory_row_failed",
                memory_id=r.get("id"), error=str(exc)[:200],
            )
    if updated:
        _invalidate_memory_caches()
    return {"status": "ok", "updated": updated}


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
    try:
        list_items.clear()
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


def is_memory_muted_now(muted_until) -> bool:
    """True iff ``muted_until`` (raw column value) is in the future, UTC.

    历史上 db._is_muted 和 memory.py 的 UI 各持一份字符串字典序比较，且写入
    端用 aware ISO（``...+00:00``）而读取端用 naive ISO（无 tz 后缀）。当
    两个字符串前缀相同时 ``+`` (43) < 任何数字 → aware 字符串恒大于 naive，
    边界条件下静音的"刚到期"瞬间会判错。

    本函数把 ``muted_until`` 统一解析为 aware UTC datetime 后用 datetime
    比较，对以下输入都鲁棒：
      - datetime 对象（aware 或 naive，naive 默认按 UTC 解释）
      - ISO 字符串带 ``+00:00`` 或 ``Z``
      - ISO 字符串无 tz 后缀（兼容老数据）

    Failure-safe：解析失败一律返回 False（"未静音"）—— 用户看到一条规则
    生效，比"明明设置静音但不生效"的反向 bug 影响小。
    """
    if not muted_until:
        return False
    try:
        if isinstance(muted_until, datetime):
            mu = muted_until
        else:
            s = str(muted_until).strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            try:
                mu = datetime.fromisoformat(s)
            except ValueError:
                # 进一步兜底：截掉小数秒后再试（某些 PG client 把 7 位微秒
                # 返回成字符串，fromisoformat 只接受最多 6 位）
                s2 = re.sub(r"\.(\d{6})\d+", r".\1", s)
                mu = datetime.fromisoformat(s2)
        if mu.tzinfo is None:
            mu = mu.replace(tzinfo=timezone.utc)
        return mu > datetime.now(timezone.utc)
    except Exception:
        return False


def _is_muted(memory_row: dict) -> bool:
    """Backcompat shim — delegate to ``is_memory_muted_now``.

    保留旧名字让 db.py 内部其它调用点（如 ``get_confirmed_memories``）继续
    工作，新代码应直接用 ``is_memory_muted_now``。
    """
    return is_memory_muted_now(memory_row.get("muted_until"))


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
    except Exception as exc:
        telemetry.log_event(
            "session_instruction_insert_failed",
            project_id=project_id, error=str(exc)[:200],
        )
        return None
