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

import hashlib
import json
import re
import threading
from typing import Any, Optional
from datetime import datetime, timezone, timedelta

from supabase import create_client, Client
from supabase.client import ClientOptions
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

    2026-05-21: schema='autowriter' 让所有 ``client.table("items")`` 等调用
    透明指向 ``autowriter.items``（共享 Supabase + schema 隔离，避免和
    sanshengliubu 在 public 里冲突）。前置条件：autowriter-migrations 已跑
    且 Supabase Dashboard → Settings → API → Exposed schemas 已包含
    ``autowriter``。
    """
    client = create_client(
        supabase_url, anon_key,
        options=ClientOptions(schema="autowriter"),
    )
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


def get_service_client() -> Client:
    """构造一个 service_role Supabase client（绕 RLS）。

    **仅供后台 worker 进程（worker.py）使用** —— service_role 能读写所有用户
    的数据, 绝不能在 Streamlit app 路径里调用。需要 ``SUPABASE_SERVICE_ROLE_KEY``
    环境变量（见 config.py）; 未配时抛错而不是静默退化, 避免 worker 拿 anon key
    走 RLS 永远领不到 job 还查不出原因。

    不走 ``_make_client_cached``（那个按 anon_key + access_token 缓存）: service
    client 是 worker 进程级单例, 由 worker.py 持有一份即可。
    """
    key = getattr(config, "SUPABASE_SERVICE_ROLE_KEY", "")
    if not key:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY 未配置 —— worker 无法绕 RLS 领取 job。"
            "请在 worker 主机的环境变量里设置（不要硬编码）。"
        )
    return create_client(
        config.SUPABASE_URL, key,
        options=ClientOptions(schema="autowriter"),
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
-- R-029 (2026-05-22 audit): 本文件所有 policy 里的 auth.uid() 都包成
-- (select auth.uid())。PG 对 auth.uid() 这种 STABLE 函数, 直接写在 policy 里
-- 会每行求值一次; 包成子查询后 planner 当 initplan 全表只算一次——消除
-- Supabase auth_rls_initplan 告警, 大表显著更快。返回值与裸 auth.uid()
-- 完全一致, 零行为改变。Supabase 上已即时修复; 这里同步源码防 bootstrap 覆盖回。
DROP POLICY IF EXISTS projects_owner ON projects;
CREATE POLICY projects_owner ON projects
    USING (owner_id = (select auth.uid()));
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
-- 2026-05 Day 14: 太子自动学习的批次维度幂等标记。NULL = 这批次从没被
-- "全部通过 → 自动调教笔记反思" 触发过；非 NULL = 已经反思过（带时间戳便于
-- 审计 / 调试）。审核页用它来决定要不要再跑一次，避免每次刷新都重复学习
-- 同一批次（之前只用 st.session_state，浏览器刷新 / 重登就丢，重新打开同一
-- 批次又会再跑一次 generate_calibration_notes，每次都是一次 Claude 调用）。
ALTER TABLE batches
    ADD COLUMN IF NOT EXISTS auto_calibrated_at TIMESTAMPTZ NULL;
ALTER TABLE batches ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS batches_owner ON batches;
CREATE POLICY batches_owner ON batches
    USING (user_id = (select auth.uid()));

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
-- 2026-05-21: TV 飞轮接入相关列。等价于 autowriter-migrations/002+003 的
-- bootstrap 路径——operator 在已部署 Supabase 上跑迁移即可；fresh 部署直接
-- 走这段 DDL 就有列。
--   external_source / external_source_id：标记从 TV 同步进来的 item
--     (external_source='truth_vault' + external_source_id=<TV uuid>)，
--     配合下面的 UNIQUE INDEX 防止重复 ingest。
--   example_label_proposal：TV 推荐的负例标签（3 个来源置信度等级），由人工在
--     autowriter Memory Manager 审核 → 升级写到正式的 example_label，避免污
--     染飞轮 prompt。build_system_prompt 只读 example_label，不读 proposal。
ALTER TABLE items ADD COLUMN IF NOT EXISTS external_source TEXT;
ALTER TABLE items ADD COLUMN IF NOT EXISTS external_source_id TEXT;
ALTER TABLE items ADD COLUMN IF NOT EXISTS example_label_proposal TEXT
    CHECK (example_label_proposal IS NULL OR example_label_proposal IN (
        'negative_manual_rewrite',   -- A: 用户手动重写过 AI 版（高置信）
        'negative_feedback_iter',    -- B: 用户给 feedback 后 AI 重生成过（中）
        'negative_batch_rejected'    -- C: 同 batch 有 approved，本 item 卡（低）
    ));
-- 唯一索引必须按 user_id 分租户：UNIQUE 是表级约束，RLS 不参与判定。如果
-- 索引键只有 (external_source, external_source_id)，两个用户同步同一条上游
-- 记录时第二个会直接撞键失败，TV → autowriter 的跨租户 sync 整条断掉。
-- 把 user_id 放在前缀既隔离了租户，又保留了 (user_id, external_source[, id])
-- 的覆盖查询能力。
-- 旧版（PR 初版）写过一个全局索引 items_external_source_uniq；这里显式
-- DROP，否则 CREATE INDEX IF NOT EXISTS 同名时会原地跳过、保留错误的全局
-- 约束。新名字 _per_user 让已部署环境的 operator 一眼看出迁移已生效。
DROP INDEX IF EXISTS items_external_source_uniq;
CREATE UNIQUE INDEX IF NOT EXISTS items_external_source_per_user_uniq
    ON items (user_id, external_source, external_source_id)
    WHERE external_source IS NOT NULL;
CREATE INDEX IF NOT EXISTS items_proposal_idx
    ON items (example_label_proposal)
    WHERE example_label_proposal IS NOT NULL;
ALTER TABLE items ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS items_owner ON items;
CREATE POLICY items_owner ON items
    USING (user_id = (select auth.uid()));

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
        item_id IN (SELECT id FROM items WHERE user_id = (select auth.uid()))
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
        project_id IN (SELECT id FROM projects WHERE owner_id = (select auth.uid()))
    );
CREATE INDEX IF NOT EXISTS calibration_note_audit_project_idx
    ON calibration_note_audit(project_id, created_at DESC);
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS memories_owner ON memories;
CREATE POLICY memories_owner ON memories
    USING (user_id = (select auth.uid()));

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
    USING (user_id = (select auth.uid()));
CREATE INDEX IF NOT EXISTS batch_metrics_project_idx
    ON batch_metrics(project_id, created_at DESC);

-- 2026-05 Phase 2: 跨批 prompt caching 的"对话会话"持久化
--
-- 设计动机：Phase 1 让 Claude 单批之内 / 5min 内连续生成命中 cache。Phase 2
-- 把"已审核通过"的历史生成内容作为 conversation history (messages prefix)
-- 喂给下一批,让模型基于自己的过往输出继续创作——比当前每批塞 20 条历史
-- 标题进 user prompt 末尾(每批都变破坏 cache)信号强 N 倍。
--
-- session 切分维度: (project_id, engine, model_id, base_prompt_hash)。
-- 同项目同模型在 base_prompt 不变时继续累积; 跨模型 / base 改了 / 撞窗口
-- 才开新 session。RLS 跟 batches/items 一致按 user_id 隔离。
CREATE TABLE IF NOT EXISTS generation_sessions (
    id                    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id            UUID REFERENCES projects(id) ON DELETE CASCADE,
    engine                TEXT NOT NULL,                    -- 'claude' / 'gemini'
    model_id              TEXT NOT NULL,                    -- e.g. 'claude-sonnet-4-6'
    base_prompt_hash      TEXT NOT NULL,                    -- sha256 of project.system_prompt
    status                TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','sealed','archived')),
    seal_reason           TEXT
        CHECK (seal_reason IS NULL OR seal_reason IN
            ('window_full','context_error','base_changed','manual')),
    -- 累积 input tokens(粗略,跨批 SUM): 每次 LLM 调用后追加 usage.input +
    -- cache_create + cache_read。Phase 2.3 起它只当"本会话累计消耗"统计——
    -- 因为是跨批累加,不能当"当前窗口占用"用(几批就虚高到接近上限)。
    running_input_tokens  BIGINT NOT NULL DEFAULT 0,
    -- Phase 2.3: 当前窗口占用 = 最近一批单次主调用的 prefix 大小(SET 非累加)。
    -- 这才是"这个 session 现在多满",用于 UI 进度条 + 自动封窗阈值判断。
    last_prefix_tokens    BIGINT NOT NULL DEFAULT 0,
    -- session 建立时 snapshot 一份 model 的 context window(避免后续 config
    -- 改了 window 数让本 session 进度条跳变)。
    window_limit          BIGINT NOT NULL,
    user_id               UUID NOT NULL,
    created_at            TIMESTAMPTZ DEFAULT NOW(),
    last_used_at          TIMESTAMPTZ DEFAULT NOW(),
    sealed_at             TIMESTAMPTZ NULL
);
-- Phase 2.3: 给已部署环境补 last_prefix_tokens 列(当前窗口占用)。
ALTER TABLE generation_sessions
    ADD COLUMN IF NOT EXISTS last_prefix_tokens BIGINT NOT NULL DEFAULT 0;
ALTER TABLE generation_sessions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS generation_sessions_owner ON generation_sessions;
CREATE POLICY generation_sessions_owner ON generation_sessions
    USING (user_id = (select auth.uid()));
-- 路由 UNIQUE partial index: 防止并发 worker 同时为同一 routing key 各自
-- insert 一条 active session(后续 get_or_create_active_session 用 .limit(1)
-- 无 order,可能交替绑到不同 session, 撕裂对话历史)。两个 insert race 时
-- 一条会撞 23505 unique violation, helper 内部 catch 后重 lookup 拿到先
-- 成功那条。已部署环境通过 ALTER 迁移过(旧版叫 generation_sessions_route
-- _idx,这里用新名 _uniq 表达约束变化,部署脚本 DROP 旧名 + CREATE 新名)。
CREATE UNIQUE INDEX IF NOT EXISTS generation_sessions_route_uniq
    ON generation_sessions(project_id, engine, model_id, base_prompt_hash)
    WHERE status = 'active';
-- 按项目列 session 的索引(UI 面板用)。Key 用 last_used_at 跟 list_project
-- _sessions 的 ORDER BY 一致, 避免 extra sort。
CREATE INDEX IF NOT EXISTS generation_sessions_project_idx
    ON generation_sessions(project_id, last_used_at DESC);

-- session_messages: 每条 turn 一行(user / assistant 交替)
--
-- ``content`` 用 JSONB 而不是 TEXT,为了承载 Claude/Gemini 的 multi-block
-- 格式(list[{type,text}]) + 未来扩展(image / tool_use)。当前只用 text 块。
-- user role: 本批的指令文本; assistant role: 本批通过审核的版本聚合。
--
-- ``batch_id`` ON DELETE SET NULL: 删 batch 不应级联删除已经入 session 的
-- 消息——它已经是"模型对话历史"的一部分,删了反而让前缀错位。SET NULL 后
-- 仍能从 session 走出"哪条 turn 来自哪个 batch"的回溯关系(NULL = 来源
-- batch 已被删除)。
CREATE TABLE IF NOT EXISTS session_messages (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id    UUID NOT NULL REFERENCES generation_sessions(id) ON DELETE CASCADE,
    turn_idx      INTEGER NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('user','assistant')),
    content       JSONB NOT NULL,
    batch_id      UUID REFERENCES batches(id) ON DELETE SET NULL,
    committed_at  TIMESTAMPTZ DEFAULT NOW()
);
-- Phase 2.2: item_id 标记"这条 turn 来自哪个已审核 item",懒同步时按
-- item_id 去重(同一 approved item 只进 session 一次)。ON DELETE SET NULL:
-- 删 item 不删历史(同 batch_id 哲学)。
ALTER TABLE session_messages
    ADD COLUMN IF NOT EXISTS item_id UUID REFERENCES items(id) ON DELETE SET NULL;
ALTER TABLE session_messages ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS session_messages_owner ON session_messages;
-- 跟 versions 一样,通过外键到 generation_sessions.user_id 来做 RLS,避免
-- 重复存 user_id(session 一改用户/被搬就乱套)。
CREATE POLICY session_messages_owner ON session_messages
    USING (
        session_id IN (SELECT id FROM generation_sessions WHERE user_id = (select auth.uid()))
    );
-- 按 (session, turn) 顺序读取是热路径(每批生成前要拉完整历史拼 prefix)。
CREATE UNIQUE INDEX IF NOT EXISTS session_messages_session_turn_uniq
    ON session_messages(session_id, turn_idx);
-- 懒同步幂等 + 并发防重: (session_id, item_id) 唯一(item_id 非空)。
-- 一个 approved item 在一个 session 只能有一条带 item_id 的 turn(assistant);
-- user 占位 turn 的 item_id 为 NULL 不受约束。并发同步时第二个 insert 撞约束
-- 失败 → 本次跳过, 不产生重复历史(Phase 2.2 review #4)。
CREATE UNIQUE INDEX IF NOT EXISTS session_messages_session_item_uniq
    ON session_messages(session_id, item_id)
    WHERE item_id IS NOT NULL;

-- 2026-05: 登录审计——识别"一号多人共享"
-- 每次成功 sign_in 落一行；token_refresh / sign_up 不入表（避免噪音）。
-- 客户端 IP / UA 由 Streamlit ``st.context.headers`` 抓取（X-Forwarded-For
-- 经过 Streamlit Cloud 代理后保留真实客户端 IP）。
-- 单账号短期出现多个 distinct IP/UA → 共享嫌疑。
CREATE TABLE IF NOT EXISTS user_logins (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id     UUID NOT NULL,
    ip          TEXT,
    user_agent  TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE user_logins ENABLE ROW LEVEL SECURITY;
-- 审计表 append-only：用户只能 SELECT/INSERT 自己的行，UPDATE/DELETE 一律
-- 禁止——否则被检测出"一号多人"的用户能直接 DELETE 掉自己的登录历史，
-- 整张表的可信度就没了。拆成两条独立 policy（FOR SELECT / FOR INSERT），
-- 不写 UPDATE / DELETE policy = PG 默认 deny。GRANT 块里也单独把 user_logins
-- 拎出来只授 SELECT, INSERT 给 authenticated（防 GRANT/RLS 双层失效）。
DROP POLICY IF EXISTS user_logins_owner ON user_logins;
DROP POLICY IF EXISTS user_logins_select_own ON user_logins;
DROP POLICY IF EXISTS user_logins_insert_own ON user_logins;
CREATE POLICY user_logins_select_own ON user_logins
    FOR SELECT USING (user_id = (select auth.uid()));
CREATE POLICY user_logins_insert_own ON user_logins
    FOR INSERT WITH CHECK (user_id = (select auth.uid()));
CREATE INDEX IF NOT EXISTS user_logins_user_idx
    ON user_logins(user_id, created_at DESC);

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
    projects, batches, items, versions, memories, batch_metrics,
    generation_sessions, session_messages
    TO authenticated;
-- user_logins 是审计表：only SELECT + INSERT for authenticated（append-only），
-- 防止用户改/删自己的登录历史。service_role 走 SQL Editor 看全部 / 必要时清理。
GRANT SELECT, INSERT ON user_logins TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON
    projects, batches, items, versions, memories, batch_metrics, user_logins,
    generation_sessions, session_messages
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

-- ─────────────────────────────────────────────────────────────────────────
-- R-029 (2026-05-22 audit · unindexed_foreign_keys): 给外键列补覆盖索引。
-- FK 列无索引时, 父表删除的级联清理 + 按 FK 的 JOIN/过滤要全表扫描, 大表会
-- 明显变慢。全部 IF NOT EXISTS 幂等。常用且基本非空的列用普通索引; 频繁为
-- NULL 且 ON DELETE SET NULL 的列用 partial(WHERE ... IS NOT NULL)缩小体积,
-- 仍覆盖"按该 FK 找引用行"的级联/JOIN 场景。
-- 当时数据量小(items~3.7k / versions~4.4k)收益不大, 提前建防患于未然。
CREATE INDEX IF NOT EXISTS batches_project_idx     ON batches(project_id);
CREATE INDEX IF NOT EXISTS items_batch_idx         ON items(batch_id);
CREATE INDEX IF NOT EXISTS versions_item_idx       ON versions(item_id);
CREATE INDEX IF NOT EXISTS batch_metrics_batch_idx ON batch_metrics(batch_id);
CREATE INDEX IF NOT EXISTS memories_project_idx
    ON memories(project_id) WHERE project_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS memories_source_batch_idx
    ON memories(source_batch_id) WHERE source_batch_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS session_messages_batch_idx
    ON session_messages(batch_id) WHERE batch_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS session_messages_item_idx
    ON session_messages(item_id) WHERE item_id IS NOT NULL;

-- ═════════════════════════════════════════════════════════════════════════
-- R-018 (2026-05-22 audit): DB-backed job 队列 —— 取代 Streamlit 进程内
-- daemon thread。daemon thread 在进程 reload / 容器滚动 / OOM 被 kill 时会把
-- 正在跑的 batch 丢掉（UI 显示 running 实际已死）。jobs 表 + 独立 worker 进程
-- 让任务跨进程重启存活: worker 用 claim_one_job()（FOR UPDATE SKIP LOCKED）
-- 原子领取, 心跳超时由 sweeper 退回重试。Phase 1 仅 'noop' handler 验证连通性,
-- 'generate_batch' / 'quick_gen' 留 Phase 2。详见 worker.py。
CREATE TABLE IF NOT EXISTS jobs (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    kind             TEXT NOT NULL,                  -- 'generate_batch' / 'quick_gen' / 'noop'
    payload          JSONB NOT NULL DEFAULT '{}'::jsonb,
    status           TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','claimed','running','success','failed','cancelled')),
    priority         INTEGER NOT NULL DEFAULT 0,     -- 高优先先领（用户触发 > 后台任务）
    attempts         INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 3,
    progress_pct     INTEGER NOT NULL DEFAULT 0,
    progress_message TEXT,
    result           JSONB,
    error_text       TEXT,
    claimed_by       TEXT,                           -- worker id
    claimed_at       TIMESTAMPTZ,
    started_at       TIMESTAMPTZ,
    heartbeat_at     TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    next_retry_at    TIMESTAMPTZ,                    -- 退避重试: 早于此不领
    -- user_id / project_id 是裸 UUID（不设 FK，同 R-012 跨表松耦合哲学）:
    -- jobs 是瞬时表, 不需要随项目级联删; 也省掉又一个 FK-without-index。
    user_id          UUID NOT NULL,
    project_id       UUID,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS jobs_owner ON jobs;
-- 用户只能看/插/改/删自己的 job（UI 取消 = UPDATE status='cancelled'）。
-- worker 用 service_role 绕 RLS, 能领取任意用户的 pending job。
-- auth.uid() 包成 (select ...) 同 R-029, 避免每行重算。
CREATE POLICY jobs_owner ON jobs
    USING (user_id = (select auth.uid()))
    WITH CHECK (user_id = (select auth.uid()));
-- 领取热路径: 只扫 pending, 按 priority 高→低、created_at 早→晚。
CREATE INDEX IF NOT EXISTS jobs_pending_idx
    ON jobs(priority DESC, created_at) WHERE status = 'pending';
-- sweeper 扫心跳超时的 claimed/running 行。
CREATE INDEX IF NOT EXISTS jobs_active_heartbeat_idx
    ON jobs(heartbeat_at) WHERE status IN ('claimed','running');
-- UI 列某用户的 job。
CREATE INDEX IF NOT EXISTS jobs_user_idx ON jobs(user_id, created_at DESC);
GRANT SELECT, INSERT, UPDATE, DELETE ON jobs TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON jobs TO service_role;

-- claim_one_job: 原子领取一个待处理 job。FOR UPDATE SKIP LOCKED 保证多 worker
-- 副本并发领取不会抢到同一行（撞锁的直接跳过该行往下找）。领到后置 running +
-- attempts+1 + 记 worker / 时间戳。无可领时返回 0 行。
-- 仅授予 service_role（worker 身份）; 普通用户不能领 job。
CREATE OR REPLACE FUNCTION claim_one_job(_worker_id TEXT, _kinds TEXT[] DEFAULT NULL)
RETURNS SETOF jobs
LANGUAGE plpgsql
AS $$
DECLARE
    v_job jobs;
BEGIN
    SELECT * INTO v_job
    FROM jobs
    WHERE status = 'pending'
      AND (next_retry_at IS NULL OR next_retry_at <= now())
      AND (_kinds IS NULL OR kind = ANY(_kinds))
    ORDER BY priority DESC, created_at ASC
    FOR UPDATE SKIP LOCKED
    LIMIT 1;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    UPDATE jobs
    SET status       = 'running',
        attempts     = attempts + 1,
        claimed_by   = _worker_id,
        claimed_at   = now(),
        started_at   = COALESCE(started_at, now()),
        heartbeat_at = now()
    WHERE id = v_job.id
    RETURNING * INTO v_job;

    RETURN NEXT v_job;
END;
$$;
REVOKE ALL ON FUNCTION claim_one_job(TEXT, TEXT[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_one_job(TEXT, TEXT[]) TO service_role;
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


def _record_schema_drift(missing_cols: list[str]) -> None:
    """R-027: 把列漂移记到 ``st.session_state`` 让主页面渲染一次可见告警。

    之前 ``update_project`` 撞"列不存在"只剥列 + telemetry，用户在 UI 改了
    值、DB 没生效却没有任何提示。这里在有 Streamlit ScriptRunContext 时（即
    UI 主线程）把缺失列塞进 session_state，``app.py`` 顶部统一 pop 出来
    ``st.warning``。worker 线程没有 context，写入会抛 → 被吞掉（那边本来就
    只能靠 telemetry）。
    """
    if not _HAS_ST or not missing_cols:
        return
    try:
        import streamlit as st
        prev = st.session_state.get("_schema_drift_cols") or []
        st.session_state["_schema_drift_cols"] = sorted(set(list(prev) + list(missing_cols)))
    except Exception:
        pass


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
        # R-027: 不再静默——把缺失列塞 session_state 让主页面显式告警一次。
        _record_schema_drift(hit_cols)
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


def mark_batch_auto_calibrated(client: Client, batch_id: str) -> bool:
    """把批次标记为"已被太子学习过"。

    审核页全部通过时触发自动调教笔记反思后调用，记一个时间戳到
    ``batches.auto_calibrated_at``。再次打开同一批次时，gate 检查这一列就
    能跳过重复学习，省一次 Claude 调用 + token。

    返回 True 表示写入成功，False 表示失败（列缺失 / 网络）—— 调用方据此
    决定要不要在 UI 上提示一次。
    """
    try:
        client.table("batches").update({
            "auto_calibrated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", batch_id).execute()
        try:
            list_batches.clear()
        except Exception:
            pass
        return True
    except Exception as exc:
        msg = str(exc)
        if "auto_calibrated_at" in msg:
            telemetry.log_event(
                "mark_batch_auto_calibrated_missing_column",
                hint="run additive ALTER on batches.auto_calibrated_at",
            )
        else:
            telemetry.log_event(
                "mark_batch_auto_calibrated_failed",
                batch_id=batch_id, error=msg[:200],
            )
        return False


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
) -> bool:
    """重写一条 version 的内容（用于自动重生场景）。返回主 UPDATE 是否成功。

    只更新传入的字段；id / item_id / version_num / ai_engine / created_at 不动。
    R-034: 此前主 UPDATE 也被 ``except: pass`` 吞掉(docstring 只声称 embedding
    静默)——调用方 ``_try_regen_one`` 按成功继续更新内存去重池, DB 瞬断时
    DB 里仍是旧重复文案而内存认为已替换, 永久分叉且零告警。现在主写失败
    返回 False + telemetry(仍不抛, daemon 线程里不让单条写失败炸整批);
    embedding 子写保持 best-effort, 但主写失败时跳过(不给旧内容配新向量)。
    """
    updates: dict[str, Any] = {"title": title, "body": body}
    if keywords is not None:
        updates["keywords"] = keywords
    if token_usage is not None:
        updates["token_usage"] = token_usage
    ok = True
    try:
        res = client.table("versions").update(updates).eq("id", version_id).execute()
        if not (getattr(res, "data", None) or []):
            # R-034 review #2: PostgREST 对 0 行匹配的 UPDATE 不抛错(行已被
            # 并发删除 / RLS 拦截), 返回空 data —— 同样必须按失败处理, 否则
            # 调用方仍会以"已替换"更新内存去重池, 恰好复现本函数要消灭的
            # DB/内存分叉。
            ok = False
            telemetry.log_event(
                "version_content_update_no_match", version_id=version_id,
            )
    except Exception as exc:
        ok = False
        telemetry.log_event(
            "version_content_update_failed",
            version_id=version_id, error=str(exc)[:200],
        )
    try:
        list_items.clear()
    except Exception:
        pass
    if ok and embedding is not None:
        try:
            client.table("versions").update({"embedding": embedding}).eq("id", version_id).execute()
        except Exception:
            # embedding 列没迁移的部署还能用 —— 保持静默(仅向量缺失, 内容已对)
            pass
    return ok


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


def _parse_pgvector(val) -> Optional[list[float]]:
    """把 PostgREST 读回的 pgvector 值归一成 ``list[float]``(R-034)。

    PostgREST 对 ``vector(768)`` 列的 JSON 序列化是**字符串** ``"[0.1,...]"``,
    不是数组。此前全库无任何反序列化,下游 ``dedup.cosine_similarity`` 对
    list-vs-str 因长度不等**静默返回 0.0** —— 后果是(a)已 backfill 向量的
    soft 规则在 ``memory.filter_soft_by_relevance`` 里 score=0 全部被丢弃不
    注入;(b)跨批语义查重的 DB 历史池(队列预热 + 重生避重)自上线起 0 命中。
    本函数在 DB 读取边界统一归一: 已是 list 原样返回; None/空/解析失败返回
    None(调用方按"无向量"处理, 与历史行为一致)。
    """
    if val is None or isinstance(val, list):
        return val
    if isinstance(val, str):
        s = val.strip()
        if s.startswith("[") and s.endswith("]"):
            inner = s[1:-1].strip()
            if not inner:
                return None
            try:
                return [float(x) for x in inner.split(",")]
            except ValueError:
                return None
    return None


def _in_chunks(seq: list, size: int = 100):
    """把 id 列表切成 ≤size 的块, 供 ``.in_()`` 分批查询(R-034)。

    一次塞几百个 UUID 进 ``.in_()`` 会生成 10KB+ 的查询串(414 风险), 且
    结果行数超过 PostgREST max-rows(默认 1000)时**静默截断**。同文件
    ``get_session_committed_item_ids`` 已用同样的分块套路。
    """
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


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
    pairs = _collect_recent_canonical_versions(
        client, project_id, limit, with_embedding=True,
    )
    out: list[dict] = []
    for _item, chosen in pairs:
        body = (chosen.get("body") or "").strip()
        first_line = next((ln for ln in body.splitlines() if ln.strip()), "")
        out.append({
            "version_id": chosen.get("id"),
            "title":      (chosen.get("title") or "").strip(),
            "opening":    first_line.strip()[:25],
            # R-034: pgvector 字符串归一成 list[float](见 _parse_pgvector)
            "embedding":  _parse_pgvector(chosen.get("embedding")),
        })
    # R-034: 收集器返回新→旧; 反转成旧→新。消费方(_build_dedup_instruction
    # 取 historical[-20:]、topup 的合并 dict 以尾部优先)都以"尾部=最新"为约定。
    out.reverse()
    return out


def _collect_recent_canonical_versions(
    client: Client,
    project_id: str,
    limit: int,
    with_embedding: bool,
) -> list[tuple[dict, dict]]:
    """分页遍历最近 40 个 batch 的 items(新→旧), 为每个 item 选 canonical
    version(best_version_id 优先, 否则最大 version_num), 跳过无版本/
    ``（解析失败）``占位, **集齐 limit 条有效记录即停**。

    R-034(review #1): 封顶必须发生在**过滤之后** —— 旧的 ``limit*2`` 预过滤
    截断在"最近一段恰是失败批次"(正是触发本轮修复的事故场景)时, 会把窗口
    内更早的有效标题永远挡在池外, 恰好削弱事故项目的去重。分页按需拉取,
    多数情况第一页即集齐; 极端情况也最多遍历完 40-batch 窗口。

    created_at 在 bulk insert 下大量并列(同一语句共享 NOW()), 以 id 作第二
    排序键保证 ``.range()`` 分页不丢行/不重复。返回 (item, chosen_version)
    列表, 顺序新→旧, 长度 ≤ limit。
    """
    batches = list_batches(client, project_id, limit=40)
    if not batches:
        return []
    batch_ids = [b["id"] for b in batches]

    base_fields = "id, item_id, title, body, version_num"
    out: list[tuple[dict, dict]] = []
    page_size = max(limit, 100)
    offset = 0
    embedding_ok = with_embedding
    while len(out) < limit:
        items_res = (
            client.table("items")
            .select("id, best_version_id")
            .in_("batch_id", batch_ids)
            .order("created_at", desc=True)
            .order("id", desc=True)
            .range(offset, offset + page_size - 1)
            .execute()
        )
        items = items_res.data or []
        if not items:
            break
        offset += len(items)
        item_ids = [it["id"] for it in items]

        def _fetch_versions(fields: str) -> list[dict]:
            rows: list[dict] = []
            for chunk in _in_chunks(item_ids):
                res = (
                    client.table("versions")
                    .select(fields)
                    .in_("item_id", chunk)
                    .execute()
                )
                rows.extend(res.data or [])
            return rows

        if embedding_ok:
            try:
                version_rows = _fetch_versions(base_fields + ", embedding")
            except Exception:
                # embedding column not yet migrated — 本页及后续页都降级
                embedding_ok = False
                version_rows = _fetch_versions(base_fields)
        else:
            version_rows = _fetch_versions(base_fields)

        versions_by_item: dict[str, list[dict]] = {}
        for v in version_rows:
            versions_by_item.setdefault(v["item_id"], []).append(v)

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
            out.append((item, chosen))
            if len(out) >= limit:
                break
        if len(items) < page_size:
            break  # 40-batch 窗口已遍历完
    return out


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
    client: Client, item_id: str, status: str, best_version_id: Optional[str] = None,
    clear_best_version: bool = False,
) -> dict:
    """更新 item 状态; ``best_version_id`` truthy 时一并写入。

    R-036: ``clear_best_version=True`` 显式把 best_version_id 置 NULL ——
    此前全代码库没有任何清除路径(``if best_version_id`` 只在 truthy 时写),
    迭代出新版本后旧的"最佳"指针仍指着老版本, 卡片/导出永远展示旧文。
    仅在未同时传入新 best_version_id 时生效。
    """
    updates: dict[str, Any] = {"status": status}
    if best_version_id:
        updates["best_version_id"] = best_version_id
    elif clear_best_version:
        updates["best_version_id"] = None
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

    R-034: 经 ``_collect_recent_canonical_versions`` 分页收集 —— 按 created_at
    desc 遍历、**过滤后**集齐 limit 条有效记录即停(预过滤封顶会在"最近一段
    恰是失败批次"时把更早的有效标题挡在池外), versions 分块查防 max-rows
    静默截断。输出旧→新(尾部=最新), 与 _build_dedup_instruction 取
    historical[-20:] 的约定对齐。
    """
    pairs = _collect_recent_canonical_versions(
        client, project_id, limit, with_embedding=False,
    )
    out: list[dict] = []
    for _item, chosen in pairs:
        body = (chosen.get("body") or "").strip()
        first_line = next((ln for ln in body.splitlines() if ln.strip()), "")
        out.append({
            "title":   (chosen.get("title") or "").strip(),
            "opening": first_line.strip()[:25],
        })
    out.reverse()
    return out


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
    rows = res.data or []
    # R-034: select("*") 会把 pgvector 的 embedding 列以字符串形态带回;
    # 下游 memory.filter_soft_by_relevance 拿它算 cosine 得 0 分, 已 backfill
    # 向量的 soft 规则会被全部静默过滤掉。在读取边界统一归一成 list[float]。
    for r in rows:
        if "embedding" in r:
            r["embedding"] = _parse_pgvector(r.get("embedding"))
    return rows


def _invalidate_memory_caches() -> None:
    """Drop every memory-related cache after a write so the next read pulls fresh
    rows.  Called from every memory mutator."""
    for fn in (get_confirmed_memories, get_session_instructions, list_example_items):
        try:
            fn.clear()
        except Exception:
            pass


# 进程内 per-content 锁：AI 合并器并发分类反馈时，多个 worker 线程会对同一条
# 规则 (user_id, scope, content_hash, project_id) 同时调 upsert_memory。原先的
# select→update 不原子：两边都读到 freq=5 后都写 freq=6，frequency 计数器丢一次
# 增量；select→insert 同样可双插重复行污染 dedup。schema 上没有 UNIQUE 约束
# （会和历史脏数据冲突无法添加），所以用 app 层的 keyed lock 兜底单进程部署。
# 多 worker / 多 instance 场景仍有残余竞态，但 UPDATE 走 CAS 至少能检测出冲突
# 并重试。
_MEMORY_UPSERT_LOCKS: dict[str, threading.Lock] = {}
_MEMORY_UPSERT_LOCKS_GUARD = threading.Lock()


def _get_memory_upsert_lock(key: str) -> threading.Lock:
    with _MEMORY_UPSERT_LOCKS_GUARD:
        lk = _MEMORY_UPSERT_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _MEMORY_UPSERT_LOCKS[key] = lk
        return lk


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

    并发安全（2026-05）：
      - 进程内 keyed lock 串行化同一 (user, scope, content, project_id) 的并发调用
      - UPDATE 路径用 CAS（match old frequency），冲突时重读重试最多 3 次
    """
    lock_key = hashlib.sha256(
        f"{user_id}|{scope}|{project_id or ''}|{content}".encode("utf-8")
    ).hexdigest()
    with _get_memory_upsert_lock(lock_key):
        return _upsert_memory_locked(
            client, user_id, scope, content, source_feedback,
            project_id=project_id,
            auto_confirm_threshold=auto_confirm_threshold,
            force_confirmed=force_confirmed,
            severity=severity,
            applicability=applicability,
            rule_kind=rule_kind,
            rule_payload=rule_payload,
        )


def _upsert_memory_locked(
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
    # Try to find an existing memory with the same content
    # scope 隔离很重要：``project_id=None`` 的 global 反馈必须只匹配 project_id IS NULL
    # 的行——之前漏了 IS NULL 过滤，导致 global 反馈会去匹配并 +1 某个项目级同
    # 文本规则的 frequency，scope 边界被打穿。
    def _read_existing() -> Optional[dict]:
        q = (
            client.table("memories")
            .select("*")
            .eq("user_id", user_id)
            .eq("scope", scope)
            .eq("content", content)
        )
        if project_id:
            q = q.eq("project_id", project_id)
        else:
            q = q.is_("project_id", "null")
        rows = q.execute().data or []
        return rows[0] if rows else None

    # CAS UPDATE 循环：每轮读最新 frequency，写时用 .eq("frequency", old) 兜底。
    # 0 行受影响 → 别的并发已经改了这一行，重读重试。
    for attempt in range(3):
        row = _read_existing()
        if row is None:
            break  # 转入下方 INSERT 路径
        old_freq = row["frequency"]
        new_freq = old_freq + 1
        if force_confirmed or new_freq >= auto_confirm_threshold:
            new_status = "confirmed"
        else:
            new_status = row["status"]
        res = (
            client.table("memories")
            .update({"frequency": new_freq, "status": new_status})
            .eq("id", row["id"])
            .eq("frequency", old_freq)  # CAS：仅当 frequency 未变时才写
            .execute()
        )
        if res.data:
            _invalidate_memory_caches()
            return res.data[0]
        # 冲突：别的 worker 抢先改了 frequency，下一轮重读
        telemetry.log_event(
            "upsert_memory_cas_retry",
            attempt=attempt + 1, scope=scope,
            content_preview=content[:60],
        )
    else:
        # 3 次 CAS 都冲突，best-effort 强写一次避免完全失败
        telemetry.log_event(
            "upsert_memory_cas_exhausted",
            scope=scope, content_preview=content[:60],
        )
        latest = _read_existing()
        if latest is not None:
            new_freq = latest["frequency"] + 1
            new_status = (
                "confirmed" if force_confirmed or new_freq >= auto_confirm_threshold
                else latest["status"]
            )
            res = (
                client.table("memories")
                .update({"frequency": new_freq, "status": new_status})
                .eq("id", latest["id"])
                .execute()
            )
            _invalidate_memory_caches()
            return res.data[0] if res.data else latest
        # 兜底落空（不该发生），继续走 INSERT

    # INSERT 路径：row 为 None，需要创建新规则
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


def record_user_login(
    client: Client,
    user_id: str,
    ip: Optional[str],
    user_agent: Optional[str],
) -> None:
    """登录成功后落一行到 user_logins，用于"一号多人共享"检测。

    失败静默（不能因为审计写入失败把登录流程拖死）。client 需要带新签发
    的 JWT —— RLS 用 ``auth.uid() = user_id`` 校验。
    """
    if not user_id:
        return
    try:
        client.table("user_logins").insert({
            "user_id":    user_id,
            "ip":         ip or None,
            "user_agent": (user_agent or "")[:500] or None,
        }).execute()
    except Exception as exc:
        telemetry.log_event(
            "user_login_persist_failed",
            user_id=user_id, error=str(exc)[:200],
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

    用乐观并发（CAS）替代原本的 read-then-write：UPDATE 时 WHERE 同时匹配旧
    frequency，0 行受影响说明被别人抢先 +1 了，重读重试。这样队列 worker / UI
    同时给同一规则反馈不会丢更新——之前两个连接都读到 freq=5、都写 6 会少
    一次增量，``MEMORY_AUTO_CONFIRM_THRESHOLD=3`` 这种小阈值下"自动确认"行为
    被悄悄拖慢。
    """
    MAX_CAS_RETRIES = 5
    for _attempt in range(MAX_CAS_RETRIES):
        row = (
            client.table("memories").select("*").eq("id", memory_id).execute()
        )
        if not row.data:
            raise ValueError(f"memory {memory_id} not found")
        cur = row.data[0]
        cur_freq = cur.get("frequency", 1)
        res = (
            client.table("memories")
            .update({"frequency": cur_freq + 1, "status": "confirmed"})
            .eq("id", memory_id)
            .eq("frequency", cur_freq)  # CAS：旧值变了 → 0 行受影响 → 重试
            .execute()
        )
        if res.data:
            _invalidate_memory_caches()
            return res.data[0]
        # CAS 失败：被别的事务抢先；下一次循环重读再试
    # 重试上限——极端并发或行被删时落到这里。回退到无 CAS 的最后一次写入，
    # 至少保证 frequency 不倒退（写入值取最新读到的 +1）。
    telemetry.log_event(
        "memory_increment_cas_exhausted",
        memory_id=memory_id, retries=MAX_CAS_RETRIES,
    )
    row = client.table("memories").select("*").eq("id", memory_id).execute()
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
    """Set or clear the example_label on an item ('positive', 'negative', or None).

    同时清掉 list_example_items / list_items / list_labeled_items 三处
    cache，让"审核与迭代"卡片、Memory Manager 的"已确认"列表、注入路径
    都立刻反映新状态。
    """
    res = (
        client.table("items")
        .update({"example_label": label})
        .eq("id", item_id)
        .execute()
    )
    for fn in (list_example_items, list_items, list_labeled_items):
        try:
            fn.clear()
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

    2026-05-21：原实现取最近 50 个 batch 再 ``in_(batch_ids)`` 过滤，TV
    同步进来的 special-batch 一旦滚出 50-batch 窗口就读不到——飞轮中断。
    改用 PostgREST embedded inner join：``batches!inner(project_id)`` 让
    外层 items 行按 batches.project_id 直接过滤，不依赖窗口位置。
    """
    res = (
        _client.table("items")
        .select(
            "id, best_version_id, created_at, "
            "versions(id, title, body, version_num), "
            "batches!inner(project_id)"
        )
        .eq("batches.project_id", project_id)
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


# ── 负例候选审核（example_label_proposal）─────────────────────────────────
# TV 飞轮把候选负例写到 items.example_label_proposal，用户在 Memory Manager
# 的"负例候选审核" tab 人工确认才升级为 example_label='negative'。这样
# build_system_prompt 只读 example_label，候选不会污染 prompt 池。

@_cache_data(ttl=30, show_spinner=False)
def list_negative_proposals(
    _client: Client, project_id: str, limit: int = 50,
) -> list[dict]:
    """List items with a pending example_label_proposal in this project.

    Returns list of dicts with: item_id, title, body, proposal (3 个负例来源
    label), batch_id, created_at. 用 batches!inner 跨所有 batch 取，不依赖
    最近 N batch 窗口（同 list_example_items 的设计）。
    """
    res = (
        _client.table("items")
        .select(
            "id, best_version_id, created_at, batch_id, "
            "example_label_proposal, "
            "versions(id, title, body, version_num), "
            "batches!inner(project_id, tactic)"
        )
        .eq("batches.project_id", project_id)
        .not_.is_("example_label_proposal", "null")
        .is_("example_label", "null")  # 已经确认为 example 的不再列在候选里
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )

    out: list[dict] = []
    for item in (res.data or []):
        versions = item.get("versions") or []
        if not versions:
            continue
        best_vid = item.get("best_version_id")
        chosen = next((v for v in versions if v.get("id") == best_vid), None) \
            or max(versions, key=lambda v: v.get("version_num", 0))
        out.append({
            "item_id":  item.get("id"),
            "batch_id": item.get("batch_id"),
            "tactic":   (item.get("batches") or {}).get("tactic"),
            "title":    (chosen.get("title") or "").strip(),
            "body":     (chosen.get("body") or "").strip(),
            "proposal": item.get("example_label_proposal"),
            "created_at": item.get("created_at"),
        })
    return out


def confirm_negative_proposal(client: Client, item_id: str) -> dict:
    """User 在 UI 上点"确认为负例"：写 example_label='negative'，清空 proposal。

    清四份 cache：
    - list_example_items     注入路径要立刻看到新增的负例
    - list_negative_proposals 审核 tab 要把这条移出"待审核"段
    - list_items             审核与迭代页的卡片要立刻显示新 label
    - list_labeled_items     "已确认 example 池"段要立刻看到新进池的这条
                              （30s TTL 不清会让用户 rerun 后看不到自己刚
                              确认的 item，flow 看起来不一致）
    """
    res = (
        client.table("items")
        .update({"example_label": "negative", "example_label_proposal": None})
        .eq("id", item_id)
        .execute()
    )
    for fn in (list_example_items, list_negative_proposals, list_items, list_labeled_items):
        try:
            fn.clear()
        except Exception:
            pass
    return (res.data or [{}])[0]


def dismiss_negative_proposal(client: Client, item_id: str) -> dict:
    """User 点"驳回"：只清空 proposal，不写 example_label。

    清 list_negative_proposals cache 让审核 tab 立刻把这条移出。
    """
    res = (
        client.table("items")
        .update({"example_label_proposal": None})
        .eq("id", item_id)
        .execute()
    )
    try:
        list_negative_proposals.clear()
    except Exception:
        pass
    return (res.data or [{}])[0]


@_cache_data(ttl=30, show_spinner=False)
def list_labeled_items(
    _client: Client, project_id: str, limit: int = 50,
) -> list[dict]:
    """List items already in the example pool (example_label IS NOT NULL).

    给 Memory Manager 的"已确认 example 管理"用，让用户能撤销 / 切换标签。
    返回 item_id 等管理需要的字段，跟 list_example_items 区分（后者只供
    prompt 注入用，没必要带 id）。
    """
    res = (
        _client.table("items")
        .select(
            "id, batch_id, best_version_id, created_at, example_label, "
            "versions(id, title, body, version_num), "
            "batches!inner(project_id, tactic)"
        )
        .eq("batches.project_id", project_id)
        .not_.is_("example_label", "null")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )

    out: list[dict] = []
    for item in (res.data or []):
        versions = item.get("versions") or []
        if not versions:
            continue
        best_vid = item.get("best_version_id")
        chosen = next((v for v in versions if v.get("id") == best_vid), None) \
            or max(versions, key=lambda v: v.get("version_num", 0))
        out.append({
            "item_id":  item.get("id"),
            "batch_id": item.get("batch_id"),
            "tactic":   (item.get("batches") or {}).get("tactic"),
            "title":    (chosen.get("title") or "").strip(),
            "body":     (chosen.get("body") or "").strip(),
            "label":    item.get("example_label"),
            "created_at": item.get("created_at"),
        })
    return out


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
    # 用 timezone-aware 比对：之前 ``datetime.utcnow()`` 返回 naive，而 PG 的
    # TIMESTAMPTZ 字符串带 ``+00:00`` 偏移，按字典序字符串比较在边界微秒/格式
    # 略差时不可靠（最坏会让已过期的 session_instruction 仍然注入 prompt，
    # 24h TTL 名存实亡）。改成解析为 aware datetime 后比较。
    now_aware = datetime.now(timezone.utc)
    fresh: list[dict] = []
    for row in rows:
        expires = row.get("expires_at")
        if not expires:
            fresh.append(row)
            continue
        try:
            # PG 常见格式：``2026-05-20T11:00:00+00:00`` 或带 ``.123456``。
            # fromisoformat 在 3.11+ 接受 ``Z`` 后缀，3.10 不支持 → 兜底替换。
            expires_aware = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
            if expires_aware.tzinfo is None:
                expires_aware = expires_aware.replace(tzinfo=timezone.utc)
        except Exception:
            # 解析失败时保守保留——比误删用户当前会话的临时指令体感好
            fresh.append(row)
            continue
        if expires_aware < now_aware:
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
    # timezone-aware：写入端与 ``get_session_instructions`` 的过期判定保持同口径
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=max(1, ttl_hours))).isoformat()
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


# ── Phase 2: Generation Session CRUD ──────────────────────────────────────
# 跨批 prompt caching 的"对话会话"层。每个 session 按 (project, engine,
# model, base_prompt_hash) 唯一; 工作流是:
#
#   batch 开始前 → get_or_create_active_session() 拿 session.id
#                → list_session_messages(session.id) 拿历史 prefix
#                → 把 prefix 拼到 LLM 调用的 messages 数组前面
#                → 调用 + 累加 metrics + token tally 到 session
#   batch 通过审核后 → append_session_messages(...) 把"本批 user 指令 +
#                通过的 assistant 输出"写入 session_messages
#   batch 撞窗口 / context error → seal_session(session.id, reason)
#                → 下一批触发新 session 自动创建
#
# 本 PR 仅提供数据层 helpers,worker / app 接入留给后续 PR。完整 happy path
# 测试在 Phase 2.1 接入业务时一起跑;现阶段 helpers 不被任何业务调用。


def get_or_create_active_session(
    client: Client,
    project_id: str,
    engine: str,
    model_id: str,
    base_prompt_hash: str,
    user_id: str,
    window_limit: int,
) -> Optional[dict]:
    """Find existing ``status='active'`` session matching the 4 routing keys,
    or insert one with ``window_limit`` snapshotted at creation time.

    Returns the session row (含 id) or None **only when both lookup and
    insert paths exhaust without success**:

      lookup OK + found      → return existing
      lookup OK + not found  → insert; OK → return new; conflict → re-lookup
      lookup FAILED          → still try insert (transient PostgREST GET
                               errors shouldn't kill the routing); insert OK
                               → return new; insert FAILED → re-lookup once
                               more to recover if race partner just won

    The route_uniq partial index (UNIQUE on project/engine/model/hash WHERE
    status='active') makes the insert deterministic under concurrency —
    second worker hits 23505, we catch and re-lookup to bind to the winner.
    """
    def _lookup() -> Optional[dict]:
        res = (
            client.table("generation_sessions")
            .select("*")
            .eq("project_id", project_id)
            .eq("engine", engine)
            .eq("model_id", model_id)
            .eq("base_prompt_hash", base_prompt_hash)
            .eq("status", "active")
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None

    # 1. Initial lookup — failure here doesn't short-circuit; insert path may
    #    still succeed (or fail and we re-lookup as race recovery).
    try:
        existing = _lookup()
        if existing is not None:
            return existing
    except Exception as exc:
        telemetry.log_event(
            "generation_session_lookup_failed",
            project_id=project_id, engine=engine, model_id=model_id,
            error=str(exc)[:200],
        )
        # Continue to insert path

    # 2. Insert. UNIQUE partial index makes this race-safe.
    try:
        payload = {
            "project_id":        project_id,
            "engine":            engine,
            "model_id":          model_id,
            "base_prompt_hash":  base_prompt_hash,
            "status":            "active",
            "window_limit":      int(window_limit),
            "user_id":           user_id,
        }
        res = client.table("generation_sessions").insert(payload).execute()
        if res.data:
            return res.data[0]
    except Exception as exc:
        telemetry.log_event(
            "generation_session_create_failed",
            project_id=project_id, engine=engine, model_id=model_id,
            error=str(exc)[:200],
        )
        # Insert failure most commonly = 23505 (another worker just won
        # the race for this routing key). Re-lookup to bind to the winner.
        try:
            recovered = _lookup()
            if recovered is not None:
                return recovered
        except Exception:
            pass

    return None


def list_session_messages(client: Client, session_id: str) -> list[dict]:
    """按 ``turn_idx`` 升序返回该 session 全部 messages。返回空 list 表示
    新 session 或读取失败(看 stdout 日志区分)。
    """
    try:
        res = (
            client.table("session_messages")
            .select("turn_idx, role, content, batch_id")
            .eq("session_id", session_id)
            .order("turn_idx", desc=False)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        telemetry.log_event(
            "session_messages_list_failed",
            session_id=session_id, error=str(exc)[:200],
        )
        return []


def get_session_committed_item_ids(client: Client, session_id: str) -> set:
    """返回该 session 已经 commit 过的 item_id 集合(Phase 2.2 懒同步幂等用)。

    分页拉全(Phase 2.2 review #2): PostgREST 单次响应被 project max-rows
    (常见 1000)截断, session 超过那么多 committed turn 后单次 select 会漏,
    导致已同步的 item 被当成新的重复 append。这里用 .range() 翻页直到拉完。

    **必须 .order() 才能安全分页**(review): 不指定排序时 PostgREST 跨页
    返回顺序不确定, 可能某些 item_id 跨页被跳过 / 重复, committed set 不全 →
    懒同步把已同步的当新 append, 重新引入重复。按主键 ``id``(绝对唯一稳定)
    排序保证分页确定。

    读取失败返回空 set(调用方可能重复 commit, 但 DB 唯一约束
    session_messages_session_item_uniq 兜底防重复行)。
    """
    out: set = set()
    page = 1000
    offset = 0
    try:
        while True:
            res = (
                client.table("session_messages")
                .select("item_id")
                .eq("session_id", session_id)
                .not_.is_("item_id", "null")
                .order("id")
                .range(offset, offset + page - 1)
                .execute()
            )
            rows = res.data or []
            for r in rows:
                if r.get("item_id"):
                    out.add(r["item_id"])
            if len(rows) < page:
                break
            offset += page
        return out
    except Exception as exc:
        telemetry.log_event(
            "session_committed_items_failed",
            session_id=session_id, error=str(exc)[:200],
        )
        return out


def list_approved_versions_for_sync(
    client: Client,
    project_id: str,
    limit: int = 50,
) -> list[dict]:
    """Phase 2.2 懒同步: 查该 project 下 status='approved' 的 items 的代表
    版本 —— **不分引擎/来源**(claude / gemini / manual 手动精修 全要)。

    为什么不按 engine 过滤: 避重应该针对项目所有已产出内容。如果 claude
    session 只放 claude 写的, 就看不到 gemini 写的、更看不到用户手动精修
    (ai_engine='manual')的最终稿 —— 而 manual 恰恰是最该避免重复的采纳内容。
    每个 engine 的 session 都补"项目全部 approved", 内容相同、分别发给各自
    模型, cache 各自命中(prefix 字节一致即可, 与内容由谁产生无关)。

    版本选择(Phase 2.3 修): 优先 ``best_version_id`` 指向的版本; **为空时
    回退到该 item 最新(version_num 最大)的版本**。普通"通过"按钮不写
    best_version_id(只有手动精修 / 显式"选为最佳"才写), 旧实现要求
    best_version_id 非空, 项目级实测漏掉 ~46% 已通过内容。回退后所有
    approved item 都进避重历史。

    返回 ``[{item_id, batch_id, title, body, keywords, created_at}, ...]``,
    按 item 创建时间升序(老的在前, 符合对话历史顺序), 最多 ``limit`` 条。

    Review #3 (known limitation): 用 ``items.created_at`` 近似审核时间排序。
    items 表没有 approved_at 字段, 延迟审核的老 item(创建早、审核晚)会按
    创建时间排序; over-fetch 1000 缓解。要精确需加 approved_at 列(后续 PR)。
    """
    try:
        # 该 project 下所有 status='approved' 的 items, 按 created_at desc 取最近
        # limit 条。不再要求 best_version_id 非空(否则漏掉近一半已通过内容)。
        items_res = (
            client.table("items")
            .select("id, batch_id, best_version_id, created_at, batches!inner(project_id)")
            .eq("batches.project_id", project_id)
            .eq("status", "approved")
            .order("created_at", desc=True)
            .limit(max(limit, 1))
            .execute()
        )
        items = items_res.data or []
        if not items:
            return []

        item_ids = [it["id"] for it in items if it.get("id")]
        if not item_ids:
            return []

        # 批量取这些 item 的全部 version, 按 item 分组。
        # item_id 列表分批(避免 in_ 列表过长); 每批内部再 .range() 翻页拉全——
        # 否则单次 in_ 命中的 version 行数超过 project max-rows(常见 1000)会被
        # 静默截断, _pick_version 可能漏掉 best_version_id 指向的行 / 回退到非
        # 最新版本(同 get_session_committed_item_ids 的分页理由)。必须 .order
        # ("id") 才能安全翻页(主键唯一稳定, 跨页不跳不重)。
        versions_by_item: dict = {}
        id_chunk = 200
        page = 1000
        for i in range(0, len(item_ids), id_chunk):
            sub = item_ids[i:i + id_chunk]
            offset = 0
            while True:
                vres = (
                    client.table("versions")
                    .select("id, item_id, version_num, title, body, keywords, created_at")
                    .in_("item_id", sub)
                    .order("id")
                    .range(offset, offset + page - 1)
                    .execute()
                )
                rows = vres.data or []
                for v in rows:
                    versions_by_item.setdefault(v.get("item_id"), []).append(v)
                if len(rows) < page:
                    break
                offset += page

        def _pick_version(it: dict) -> Optional[dict]:
            """优先 best_version_id 指向的版本; 没有(或指向的版本已不存在)则
            回退到该 item 最新(version_num 最大 → created_at 最新)的版本。"""
            cand = versions_by_item.get(it.get("id")) or []
            if not cand:
                return None
            bvid = it.get("best_version_id")
            if bvid:
                for v in cand:
                    if v.get("id") == bvid:
                        return v
            return max(
                cand,
                key=lambda v: (
                    int(v.get("version_num") or 0),
                    str(v.get("created_at") or ""),
                    str(v.get("id") or ""),
                ),
            )

        matched = []
        for it in items:  # items 已按 created_at desc, 最近 limit 条
            v = _pick_version(it)
            if not v:
                continue
            matched.append({
                "item_id":    it["id"],
                "batch_id":   it.get("batch_id"),
                "title":      v.get("title", ""),
                "body":       v.get("body", ""),
                "keywords":   v.get("keywords", []),
                "created_at": it.get("created_at"),
            })
        # matched 按 created_at desc; 翻成升序让对话历史从老到新。
        matched.reverse()
        return matched
    except Exception as exc:
        telemetry.log_event(
            "approved_versions_sync_query_failed",
            project_id=project_id,
            error=str(exc)[:200],
        )
        return []


_APPEND_RETRY_MAX = 3
_APPEND_RETRY_BASE_DELAY = 0.05  # seconds


def _is_unique_violation(exc: BaseException) -> bool:
    """Heuristic detection of PostgreSQL 23505 unique_violation in Supabase
    Python client errors. The SDK wraps everything in generic exceptions,
    so we fall back to substring match on the error message — concrete
    forms vary by client version but always contain '23505', 'duplicate',
    or 'unique' somewhere.
    """
    s = str(exc).lower()
    return "23505" in s or "duplicate key" in s or "unique constraint" in s


def append_session_messages(
    client: Client,
    session_id: str,
    messages: list[dict],
    batch_id: Optional[str] = None,
) -> int:
    """批量追加 messages 到 session。

    ``messages`` 形如 ``[{"role": "user", "content": {...}, "item_id": ...}, ...]``;
    ``turn_idx`` 由本函数自动计算(从当前 max + 1 开始递增,bulk insert)。
    ``batch_id`` 是触发本次 commit 的 batch(可空,但通常都有)。
    每条 message 可带可选 ``item_id`` / ``batch_id``(覆盖整批默认值),
    Phase 2.2 懒同步用 item_id 标记 turn 来源 + 幂等去重。

    Returns: 成功插入的行数;失败返回 0。

    并发安全: read-max-then-insert 是 race-prone 的(两个 worker 同时往
    一个 session 写会同时读到一样的 max → insert 时撞
    ``session_messages_session_turn_uniq`` 23505),所以包了 retry loop。
    碰到 unique violation 退避一下重新算 max + 重试,最多 N 次。其它
    错误立即返回 0(transient 错误另外的层级会自然恢复;持久错误重试也
    没用)。

    实际使用: Phase 2.1+ 的 worker 是队列串行单线程,正常情况下不会有
    并发追加同 session 的场景;此处加 retry 是防御性 + 给"未来允许并行
    生成"留余地。
    """
    if not messages:
        return 0

    last_exc: Optional[BaseException] = None
    for attempt in range(_APPEND_RETRY_MAX):
        try:
            # 拿当前最大 turn_idx, 新插入从 next_idx 起累加
            cur = (
                client.table("session_messages")
                .select("turn_idx")
                .eq("session_id", session_id)
                .order("turn_idx", desc=True)
                .limit(1)
                .execute()
            )
            next_idx = ((cur.data[0]["turn_idx"] + 1) if cur.data else 0)

            rows = []
            for i, m in enumerate(messages):
                role = (m.get("role") or "").lower()
                if role not in ("user", "assistant"):
                    continue
                content = m.get("content")
                # content 必须是 JSON-serializable 的 dict/list — Supabase 会
                # 拒绝裸字符串往 JSONB 列写。统一包成 {"text": str} 形式以兼容
                # 调用方传 str 的便利写法。
                if isinstance(content, str):
                    content = {"text": content}
                rows.append({
                    "session_id":   session_id,
                    "turn_idx":     next_idx + i,
                    "role":         role,
                    "content":      content,
                    # per-message item_id/batch_id 覆盖整批默认(懒同步一次写多
                    # 个 item 的 turn, 各自带自己的 item_id/batch_id)
                    "batch_id":     m.get("batch_id", batch_id),
                    "item_id":      m.get("item_id"),
                })
            if not rows:
                return 0
            res = client.table("session_messages").insert(rows).execute()
            return len(res.data or [])
        except Exception as exc:
            last_exc = exc
            if _is_unique_violation(exc) and attempt < _APPEND_RETRY_MAX - 1:
                # 并发 race: 让对方先 commit 完,我们重读 max + 重试
                import time as _time
                _time.sleep(_APPEND_RETRY_BASE_DELAY * (attempt + 1))
                continue
            break

    telemetry.log_event(
        "session_messages_append_failed",
        session_id=session_id,
        error=str(last_exc)[:200] if last_exc else "unknown",
        retries=_APPEND_RETRY_MAX,
    )
    return 0


def add_session_running_tokens(client: Client, session_id: str, delta: int) -> None:
    """累加 running_input_tokens + 更新 last_used_at。

    Supabase Python client 不支持服务端 increment 表达式,所以 read-modify-
    write。并发场景下偶尔丢更新可以接受——这个值只用于软警告 UI,精度不
    关键(实际撞窗口靠 API 返回 context_length_exceeded 兜底)。
    """
    if delta <= 0:
        return
    try:
        cur = (
            client.table("generation_sessions")
            .select("running_input_tokens")
            .eq("id", session_id)
            .single()
            .execute()
        )
        cur_value = int((cur.data or {}).get("running_input_tokens") or 0)
        client.table("generation_sessions").update({
            "running_input_tokens": cur_value + int(delta),
            "last_used_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", session_id).execute()
    except Exception as exc:
        telemetry.log_event(
            "session_tokens_update_failed",
            session_id=session_id, error=str(exc)[:200],
        )


def set_session_prefix_tokens(
    client: Client, session_id: str, prefix_tokens: int,
) -> int:
    """SET ``last_prefix_tokens``(当前窗口占用,非累加)+ 更新 last_used_at。

    跟 ``add_session_running_tokens`` 不同: 这里是 SET 覆盖, 因为它表示"本
    session 当前 prefix 多大"——每批单次主调用的 prefix 大小, 不该累加。

    返回该 session 的 ``window_limit``(供调用方做封窗阈值判断), 失败返回 0。
    PostgREST update 默认回传被改的行, 顺便把 window_limit 带回来省一次 GET。
    """
    if prefix_tokens < 0:
        return 0
    try:
        res = (
            client.table("generation_sessions")
            .update({
                "last_prefix_tokens": int(prefix_tokens),
                "last_used_at": datetime.now(timezone.utc).isoformat(),
            })
            .eq("id", session_id)
            .execute()
        )
        if res.data:
            return int(res.data[0].get("window_limit") or 0)
    except Exception as exc:
        telemetry.log_event(
            "session_prefix_tokens_update_failed",
            session_id=session_id, error=str(exc)[:200],
        )
    return 0


def seal_session(client: Client, session_id: str, reason: str) -> bool:
    """标记 session 为 sealed,记录 seal_reason + sealed_at。

    幂等: 已 sealed 的 session 再调一次只是覆写 sealed_at 时间(无害),
    不报错。reason 必须在 schema CHECK 列表内
    (window_full / context_error / base_changed / manual),
    否则 PG 拒绝。
    """
    try:
        client.table("generation_sessions").update({
            "status":      "sealed",
            "seal_reason": reason,
            "sealed_at":   datetime.now(timezone.utc).isoformat(),
        }).eq("id", session_id).execute()
        return True
    except Exception as exc:
        telemetry.log_event(
            "session_seal_failed",
            session_id=session_id, reason=reason, error=str(exc)[:200],
        )
        return False


def list_project_sessions(
    client: Client,
    project_id: str,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """列出某项目的 session(UI session 进度面板用)。``status`` None 时
    返回所有状态;给 'active' / 'sealed' 时过滤。
    """
    try:
        q = (
            client.table("generation_sessions")
            .select("*")
            .eq("project_id", project_id)
            .order("last_used_at", desc=True)
            .limit(limit)
        )
        if status:
            q = q.eq("status", status)
        res = q.execute()
        return res.data or []
    except Exception as exc:
        telemetry.log_event(
            "project_sessions_list_failed",
            project_id=project_id, error=str(exc)[:200],
        )
        return []


def count_session_messages(client: Client, session_id: str) -> int:
    """返回该 session 的 message 行数(UI 面板显示"历史轮数"用)。

    用 PostgREST 的 ``count='exact'`` 只取计数, 不拉 content(JSONB 可能很大),
    比 ``list_session_messages`` 轻得多。失败返回 0。
    """
    try:
        res = (
            client.table("session_messages")
            .select("id", count="exact")
            .eq("session_id", session_id)
            .limit(1)
            .execute()
        )
        return int(getattr(res, "count", 0) or 0)
    except Exception as exc:
        telemetry.log_event(
            "session_messages_count_failed",
            session_id=session_id, error=str(exc)[:200],
        )
        return 0


# ── Job queue (R-018) ──────────────────────────────────────────────────────
# DB-backed job 队列的数据层。UI 侧用 insert_job / get_job / cancel_job /
# list_user_jobs（走 authed client + RLS）; worker 侧用 claim_one_job /
# heartbeat / progress / finish / sweep（走 service client 绕 RLS）。
# 状态机: pending →(claim)→ running →(handler)→ success | failed;
# 失败且未超 max_attempts → 回 pending + next_retry_at 退避; sweeper 把心跳
# 超时的 running 行也退回。详见 worker.py。

def _now_iso() -> str:
    """UTC ISO 字符串(秒精度), 给 jobs 的时间戳列用。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def insert_job(
    client: Client,
    kind: str,
    payload: dict,
    user_id: str,
    project_id: Optional[str] = None,
    priority: int = 0,
    max_attempts: int = 3,
) -> dict:
    """入队一个 job（UI 侧用 authed client; RLS 要求 user_id = auth.uid()）。"""
    row: dict[str, Any] = {
        "kind": kind,
        "payload": payload or {},
        "user_id": user_id,
        "priority": int(priority),
        "max_attempts": int(max_attempts),
    }
    if project_id:
        row["project_id"] = project_id
    res = client.table("jobs").insert(row).execute()
    return res.data[0]


def get_job(client: Client, job_id: str) -> Optional[dict]:
    """读单个 job 行（UI 轮询进度用）。不存在 / 无权限时返回 None。"""
    try:
        res = client.table("jobs").select("*").eq("id", job_id).single().execute()
        return res.data
    except Exception:
        return None


def list_user_jobs(client: Client, user_id: str, limit: int = 20) -> list[dict]:
    """列某用户最近的 job（UI 历史 / 队列面板用）。"""
    res = (
        client.table("jobs").select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data or []


def cancel_job(client: Client, job_id: str) -> None:
    """UI 取消: 仅当还在 pending/claimed/running 时置 cancelled（终态的不动）。"""
    try:
        (
            client.table("jobs")
            .update({"status": "cancelled", "finished_at": _now_iso()})
            .eq("id", job_id)
            .in_("status", ["pending", "claimed", "running"])
            .execute()
        )
    except Exception as exc:
        telemetry.log_event("job_cancel_failed", job_id=str(job_id), error=str(exc)[:200])


def claim_one_job(
    client: Client, worker_id: str, kinds: Optional[list[str]] = None
) -> Optional[dict]:
    """worker 领取一个待处理 job（走 claim_one_job RPC, service client）。

    返回领到的 job 行（已置 running）或 None（队列空）。RPC 内部用
    FOR UPDATE SKIP LOCKED, 多 worker 并发安全。
    """
    res = client.rpc(
        "claim_one_job", {"_worker_id": worker_id, "_kinds": kinds}
    ).execute()
    rows = res.data or []
    return rows[0] if rows else None


def update_job_progress(
    client: Client, job_id: str, pct: int, message: Optional[str] = None
) -> None:
    """handler 更新进度; UI 轮询同一行即可看到。失败不抛（埋点）。"""
    patch: dict[str, Any] = {"progress_pct": int(pct)}
    if message is not None:
        patch["progress_message"] = message
    try:
        client.table("jobs").update(patch).eq("id", job_id).execute()
    except Exception as exc:
        telemetry.log_event("job_progress_failed", job_id=str(job_id), error=str(exc)[:200])


def heartbeat_job(client: Client, job_id: str, worker_id: Optional[str] = None) -> None:
    """worker 心跳线程刷 heartbeat_at; sweeper 据此判断 worker 是否还活着。

    CAS: 传 worker_id 时只在 claimed_by 仍是本 worker 时刷新。否则一个心跳曾
    失联、job 已被 sweeper 退回 + 他人重领的 stale worker, 其心跳线程会把别人的
    行"续命", 让那行永远不被 sweeper 回收（僵尸保活）。
    """
    try:
        q = client.table("jobs").update({"heartbeat_at": _now_iso()}).eq("id", job_id)
        if worker_id is not None:
            q = q.eq("claimed_by", worker_id)
        q.execute()
    except Exception as exc:
        telemetry.log_event("job_heartbeat_failed", job_id=str(job_id), error=str(exc)[:200])


def finish_job_success(
    client: Client, job_id: str, result: Optional[dict] = None,
    worker_id: Optional[str] = None,
) -> bool:
    """handler 成功返回 → 置 success + 100% + 写 result。

    CAS: 只在仍 ``status='running'`` 且（传了 worker_id 时）``claimed_by=worker_id``
    时写。防止本 worker 心跳曾失联 → sweeper 退回 → 他人重领后, 这个 stale worker
    迟到的成功覆盖掉新 attempt 的状态/结果, 或盖掉用户已取消的 job。
    返回是否真的写入（False = 行已不归本 worker, 跳过）。
    """
    q = (
        client.table("jobs")
        .update({"status": "success", "finished_at": _now_iso(),
                 "progress_pct": 100, "result": result or {}})
        .eq("id", job_id).eq("status", "running")
    )
    if worker_id is not None:
        q = q.eq("claimed_by", worker_id)
    res = q.execute()
    if not res.data:
        telemetry.log_event("job_finish_skipped_not_owner", job_id=str(job_id), worker=worker_id)
    return bool(res.data)


def mark_job_failed(
    client: Client, job_id: str, error_text: str, worker_id: Optional[str] = None,
) -> bool:
    """直接置 failed（无重试语义）。用于"无 handler"等不该重试的情形。

    CAS: 同 finish_job_success —— 只在 running +（可选）claimed_by 命中时写,
    不覆盖已被重领/取消/完成的行。返回是否真的写入。
    """
    q = (
        client.table("jobs")
        .update({"status": "failed", "finished_at": _now_iso(), "error_text": error_text})
        .eq("id", job_id).eq("status", "running")
    )
    if worker_id is not None:
        q = q.eq("claimed_by", worker_id)
    res = q.execute()
    if not res.data:
        telemetry.log_event("job_fail_skipped_not_owner", job_id=str(job_id), worker=worker_id)
    return bool(res.data)


def fail_or_requeue_job(
    client: Client, job: dict, error_text: str,
    expected_claimed_by: Optional[str] = None,
) -> str:
    """handler 抛错 / sweeper 回收后的处理: 还有重试次数 → 回 pending + 指数退避;
    否则 failed。退避 = 30 × 2^(attempts-1) 秒（30 / 60 / 120 …）。attempts 已在
    claim 时 +1, 所以这里直接比 ``attempts >= max_attempts``。

    CAS（防并发互踩, review #1/#3）: 只在行仍 active（claimed/running）且（传了
    ``expected_claimed_by`` 时）claimed_by 仍是该值才写。
      - worker 异常路径: ``expected_claimed_by`` = 本 WORKER_ID
      - sweeper 路径: ``expected_claimed_by`` = 候选行的 claimed_by（那个疑似死掉
        的 worker）
    这样 sweeper 读候选后、写之前若行已被另一 worker 重领（claimed_by 变了）或已
    完成/取消（status 变了）, CAS 落空跳过, 不会把别人的 running 打回 pending 造成
    重复执行 / 丢进度。返回新 status: ``'failed'`` / ``'pending'`` / ``'skipped'``。
    """
    attempts = int(job.get("attempts") or 0)
    max_attempts = int(job.get("max_attempts") or 1)
    if attempts >= max_attempts:
        patch: dict[str, Any] = {
            "status": "failed", "finished_at": _now_iso(), "error_text": error_text,
        }
        target = "failed"
    else:
        backoff = 30 * (2 ** max(0, attempts - 1))
        next_retry = (datetime.now(timezone.utc) + timedelta(seconds=backoff)).isoformat(timespec="seconds")
        patch = {
            "status": "pending", "error_text": error_text, "next_retry_at": next_retry,
            # 清掉 claim 痕迹, 让它能被重新领取
            "claimed_by": None, "claimed_at": None, "heartbeat_at": None, "started_at": None,
        }
        target = "pending"
    q = (
        client.table("jobs").update(patch)
        .eq("id", job["id"]).in_("status", ["claimed", "running"])
    )
    if expected_claimed_by is not None:
        q = q.eq("claimed_by", expected_claimed_by)
    res = q.execute()
    if not res.data:
        telemetry.log_event(
            "job_requeue_skipped",
            job_id=str(job.get("id")), expected_claimed_by=expected_claimed_by,
        )
        return "skipped"
    return target


def sweep_dead_jobs(client: Client, timeout_seconds: int) -> int:
    """把心跳超时的 claimed/running job 退回重试 / 置 failed（超次数）。

    worker 主循环定期调一次。返回回收的 job 数。失败安全（埋点不抛）。
    每条退回都带 ``expected_claimed_by=候选行的 claimed_by`` 做 CAS, 防止读候选
    后、写之前该行已被另一 worker 重领 —— 那种情况跳过, 不打断新 worker（review #3）。
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)).isoformat(timespec="seconds")
    try:
        res = (
            client.table("jobs").select("*")
            .in_("status", ["claimed", "running"])
            .lt("heartbeat_at", cutoff)
            .execute()
        )
        dead = res.data or []
    except Exception as exc:
        telemetry.log_event("job_sweep_query_failed", error=str(exc)[:200])
        return 0
    recovered = 0
    for job in dead:
        try:
            new_status = fail_or_requeue_job(
                client, job,
                f"heartbeat stale (cutoff={cutoff}); worker likely died, recovered by sweeper",
                expected_claimed_by=job.get("claimed_by"),
            )
            if new_status != "skipped":
                recovered += 1
        except Exception as exc:
            telemetry.log_event(
                "job_sweep_recover_failed",
                job_id=str(job.get("id")), error=str(exc)[:200],
            )
    return recovered
