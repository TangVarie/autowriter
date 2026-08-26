-- ════════════════════════════════════════════════════════════════════
-- migrations/000_baseline.sql — schema 的**源头**, fresh install 一把建全
-- ════════════════════════════════════════════════════════════════════
--
-- 这份 DDL 原来是 db.py 里一个 1162 行的 Python 字符串(``CREATE_TABLES_SQL``)。
-- 挪出来的理由不是"文件太大"(虽然 db.py 确实因此从 4438 行降到 3276), 而是
-- 一件更具体的事: **没有任何代码执行过它, 所以也没有任何东西验证过它**。
-- 审计 SUP-010。
--
-- 代价是真实发生过的。2026-08-24 修 COR-014 时发现: db.py 里那份
-- ``deskcore_commit_fingerprints`` 还带着"空开头的 title-only 稿会互相精确
-- 撞车"这个 bug —— 而 ``migrations/001`` 里同一个函数早就修好了。也就是说
-- 任何一个**新环境**都会带着一个老 bug 出生, 而没人会发现, 因为那份 SQL
-- 从来没被跑过。
--
-- 变成 .sql 文件之后:
--   · ``psql -f`` 跑得动, CI 里真的在一个空库上跑一遍(见 tests/sql_parity_check.py);
--   · 跑完再叠 001..005, 两边不一致会当场红 —— 这才是"消双写"真正的意思:
--     不是只留一份, 而是**让两份必须对得上, 对不上就报错**。
--
-- ⚠️ 加表 / 加列 / 改函数仍然要两边都改(本文件 + 对应的增量迁移)。区别在于
--    现在漏改会被 CI 抓到, 而不是等到某次 fresh install。
--
-- ⚠️ 这份是 Supabase 上的 schema: 用到 ``auth.uid()``、``extensions`` schema、
--    ``anon`` / ``authenticated`` / ``service_role`` 三个角色, 以及 pgvector。
--    在裸 PostgreSQL 上跑要先建这些(CI 的 harness 里有最小 shim)。
--
-- 幂等: 全文都是 IF NOT EXISTS / CREATE OR REPLACE / DROP+CREATE, 重复执行
-- 是干净 no-op。
-- ════════════════════════════════════════════════════════════════════
-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA extensions;

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
-- 决策出处(审计 COR-004 / COR-007)。status 一个字段同时装"人工审稿意见"和
-- "机器检测结果", 而 TV 的决策同步把两者**全部**当人工反馈灌进
-- prepublish_evaluations 去校准模型 —— 机器自己的判定被当成人的判断喂回给
-- 模型学。这三列让消费方不必再推断。完整说明见 migrations/006。
-- ⚠️ 与 006 保持一致: 两边都要有(README: 加列必须两边都改)。
ALTER TABLE items ADD COLUMN IF NOT EXISTS decision_source TEXT
    CHECK (decision_source IN
           ('human', 'auto_hard_rule', 'auto_dedup', 'system'));
ALTER TABLE items ADD COLUMN IF NOT EXISTS reviewer_id UUID;
ALTER TABLE items ADD COLUMN IF NOT EXISTS decided_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS items_human_decision_idx
    ON items (decided_at)
    WHERE decision_source = 'human';
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
-- 审计 COR-004 / migrations/003: 版本号在同一个 item 内唯一。
-- db.create_version 是「读 max → +1 → INSERT」, 并发迭代同一个 item 会双双
-- 写入同一个号。那不会报错, 但挑"代表版本"的地方(list_approved_versions_for
-- _sync 的 _pick_version、deskcore 的 labeled_examples / legacy_versions)都按
-- max(version_num, created_at, id) 排序 —— 并列时选中哪条要看 tie-break,
-- best 指针可能指向被覆盖的那一版, 导出与避重参照拿到的都是旧文, 全程无声。
-- 有了这条约束, 并发写入必有一方撞 23505, create_version 捕获后重读 max 重试。
-- (已有库走 migrations/003, 那边会先把历史重复对子重编号再建索引。)
CREATE UNIQUE INDEX IF NOT EXISTS versions_item_version_uniq
    ON versions (item_id, version_num);
-- Semantic-similarity embedding for cross-batch duplicate detection.
-- Requires the pgvector extension (Supabase: Database → Extensions → enable
-- "vector" once).  Nullable so legacy rows stay readable; a backfill helper
-- populates them lazily.
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;
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
--
-- ⚠️ 这个块只覆盖【它上面已经建出来的表】。文件后面还有 jobs 和 deskcore 那
-- 四张表, 它们各自在自己那一段末尾发 GRANT —— 加新表时别忘了那一步。
-- CI 会替你记着: tests/sql_parity_check.py 里有一条断言, autowriter schema 下
-- 【每一张】表都必须至少对 service_role 有 SELECT/INSERT/UPDATE/DELETE。
GRANT SELECT, INSERT, UPDATE, DELETE ON
    projects, batches, items, versions, memories, batch_metrics,
    generation_sessions, session_messages
    TO authenticated;
-- ── 两张审计表：authenticated 只给 append + 读 ─────────────────────────
-- user_logins 是审计表：only SELECT + INSERT for authenticated（append-only），
-- 防止用户改/删自己的登录历史。service_role 走 SQL Editor 看全部 / 必要时清理。
--
-- calibration_note_audit 是同一类东西、同一个待遇。它存的是调教笔记每次写入的
-- before / append / after 三份全文，用途就是回答"为什么这条观察突然出现/消失
-- 了"——**能被改写或删除的审计流水回答不了这个问题**。它的 RLS 只有一条不分
-- 命令的 owner policy，把用户限制在自己的项目里，但拦不住他改写自己项目的历史；
-- 真正该拦住这件事的是这里少发两个权限。(codex review · aw#65)
--
-- ⚠️ 代码侧核对过：db.py 对这张表只有 insert / select / count，
-- **没有任何 update 或 delete**。删项目时的清理走 projects 的
-- ON DELETE CASCADE，而外键的级联动作是以**表 owner** 的权限执行的，
-- 不需要调用者持有 DELETE。所以收紧它不影响任何现有路径。
GRANT SELECT, INSERT ON user_logins            TO authenticated;
GRANT SELECT, INSERT ON calibration_note_audit TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON
    projects, batches, items, versions, memories, batch_metrics, user_logins,
    calibration_note_audit, generation_sessions, session_messages
    TO service_role;
-- ⚠️ ``calibration_note_audit`` 是 2026-08-26 补进来的 —— 它建于本块之前却一直
-- 不在名单里。现存的生产库看不出来(那张表建于 Supabase 授权口径变更前, 带着
-- 历史 GRANT), 坏的只有【新开的库】: db.py::log_calibration_audit 的写入是
-- ``except Exception: pass``, 于是新库上调教笔记的审计流水会**静默地一条都
-- 不留**, 没有任何东西报错。增量那一路见 migrations/007。

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

-- ══════════════════════════════════════════════════════════════════════
-- deskcore(写作台内核 MCP 服务)的四张表 —— TV D-041 / R-034
--
-- Streamlit 界面停用后, 写作能力经 deskcore 挂到 WorkBuddy / Claude Code /
-- CodeBuddy。这四张表是外置后【新增】的能力, 现有 8 张表一行不动。
-- 增量迁移见 migrations/001_deskcore.sql(给已存在的库)。
-- ══════════════════════════════════════════════════════════════════════

-- 发牌台账: 记录每个项目抽过哪些创作坐标组合, 供跨批次避重。
-- 根因: generator._assign_slot_coordinates(:1301) 只在单批内去重, 且已被移除
-- 成死代码(:1576-1584)。跨批次没有任何"用过没有"的持久记录 —— 模型是无状态的,
-- 光靠提示词让它"注意不要重复"做不到。
CREATE TABLE IF NOT EXISTS angle_ledger (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id          UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    angle_key           TEXT NOT NULL,
    dims                JSONB NOT NULL DEFAULT '{}'::jsonb,
    drawn_by            UUID,
    drawn_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- NULL = 抽了但没写成稿(占位, 按时间自然过期);
    -- 非 NULL = 真产出了稿子, 是"用掉了"的强证据。两者避重时效不同。
    consumed_version_id UUID,
    consumed_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS angle_ledger_project_key_idx
    ON angle_ledger (project_id, angle_key);
CREATE INDEX IF NOT EXISTS angle_ledger_project_drawn_idx
    ON angle_ledger (project_id, drawn_at DESC);
ALTER TABLE angle_ledger ENABLE ROW LEVEL SECURITY;

-- 成稿指纹库: 持久 / 全量 / 跨人 / 跨批次的查重底座。
-- 根因: app.py:1024 的 queue_embeddings 是 worker 进程内的内存字典, 进程一重启
-- 就空了(jobs 表那条迁移的存在本身就说明进程重启很频繁); 而且只比标题。
CREATE TABLE IF NOT EXISTS draft_fingerprints (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id      UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version_id      UUID,
    user_id         UUID,
    title           TEXT NOT NULL DEFAULT '',
    opening         TEXT NOT NULL DEFAULT '',
    -- 与 versions.embedding 同一个模型(见 dedup.EMBEDDING_MODEL), 可互比
    title_embedding vector(768),
    -- 产出上面那个向量的模型名。NULL = 这行没有向量。
    -- ⚠️ 换 embedding 供应商时唯一的救命稻草: 跨模型算余弦是垃圾且【不报错】,
    -- 没有这一列, 迁移期新旧向量混在一张表里, 查重会安静地失灵。
    -- 完整理由见 migrations/001_deskcore.sql 里这一列的 COMMENT。
    embedding_model TEXT,
    -- 正文首个非空行前 25 字规范化后的 sha256 前 16 位。标题换了也能抓。
    -- 空开头存空串而不是 sha16("") —— 否则所有 title-only 的行会互相"精确撞车"。
    opening_hash    TEXT,
    -- 正文四字串 shingle 的 hash 采样, 抓"换了词还是同一篇"的换皮改写
    ngram_hashes    TEXT[] NOT NULL DEFAULT '{}',
    angle_key       TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE draft_fingerprints ADD COLUMN IF NOT EXISTS embedding_model TEXT;
CREATE INDEX IF NOT EXISTS draft_fp_project_created_idx
    ON draft_fingerprints (project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS draft_fp_embedding_model_idx
    ON draft_fingerprints (project_id, embedding_model)
    WHERE title_embedding IS NOT NULL;
CREATE INDEX IF NOT EXISTS draft_fp_opening_hash_idx
    ON draft_fingerprints (project_id, opening_hash) WHERE opening_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS draft_fp_ngram_gin_idx
    ON draft_fingerprints USING GIN (ngram_hashes);
CREATE INDEX IF NOT EXISTS draft_fp_embedding_idx
    ON draft_fingerprints USING ivfflat (title_embedding vector_cosine_ops)
    WITH (lists = 100);
ALTER TABLE draft_fingerprints ENABLE ROW LEVEL SECURITY;

-- 个人调校笔记(私有层)。projects.calibration_notes 保留为【项目级共享基线】,
-- 本表是【个人叠加层】—— 隔离口径: 项目规则团队共享 + 个人风格私有。
CREATE TABLE IF NOT EXISTS user_calibration_notes (
    project_id  UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id     UUID NOT NULL,
    notes       TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project_id, user_id)
);
ALTER TABLE user_calibration_notes ENABLE ROW LEVEL SECURITY;

-- 手动精修 diff 的原始存放处(私有层)。
-- 为什么存原始 diff 而不只存蒸馏后的笔记: memory.generate_calibration_notes(:1154)
-- 的【信号 A · 手动精修差异】是最高权重信号。只留蒸馏结果的话, 换了蒸馏 prompt
-- 或想重算就没有料了 —— 笔记是导出物, diff 才是事实。
-- 只收【人真的动手改了】的对子; 未改就通过的稿子不算教学材料(memory.py:1191-1194
-- 明确拒绝从那里学, 否则模型会从偶然选择里编造风格规则)。
CREATE TABLE IF NOT EXISTS style_edits (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id  UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id     UUID NOT NULL,
    ai_title    TEXT NOT NULL DEFAULT '',
    ai_body     TEXT NOT NULL DEFAULT '',
    my_title    TEXT NOT NULL DEFAULT '',
    my_body     TEXT NOT NULL DEFAULT '',
    note        TEXT,
    distilled   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS style_edits_owner_idx
    ON style_edits (project_id, user_id, created_at DESC);
ALTER TABLE style_edits ENABLE ROW LEVEL SECURITY;

-- ── 上面四张表的表级 GRANT ───────────────────────────────────────────
-- 2026-08-26 补, 与 migrations/001_deskcore.sql:4.5 / migrations/007 同一条。
-- 原来这四张表**一行 GRANT 都没有**, 而生产库上的症状是 deskcore 除
-- list_projects 外每个工具都挂在 42501: permission denied for table
-- draft_fingerprints。
--
-- 为什么会漏: ``service_role`` 绕过 RLS 但**不绕过表级 GRANT**, 两套独立机制。
-- 它在 public schema 下看着无所不能, 是因为 Supabase 给 public 配了 default
-- privileges; ``autowriter`` 是本仓自建 schema, 没有这份默认授权。
--
-- 只给 service_role: 这四张表唯一的调用方是 deskcore(service key 连库, 归属
-- 隔离在 deskcore/core.py::assert_project_access 做)。Streamlit 界面已停用,
-- authenticated 一行都不需要。
GRANT SELECT, INSERT, UPDATE, DELETE ON
    angle_ledger, draft_fingerprints, user_calibration_notes, style_edits
    TO service_role;

-- items.updated_at: 人工决策(status / example_label)的最后变更时间。
-- 补 TV scripts/sync_autowriter_decisions_to_prepublish.py:36-40 记的缺陷 ——
-- 没这列只能按 created_at 过滤, 迟到的人工决策会漏收。
ALTER TABLE items ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
CREATE INDEX IF NOT EXISTS items_updated_at_idx ON items (updated_at DESC);

CREATE OR REPLACE FUNCTION _deskcore_touch_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS deskcore_user_calib_updated_at ON user_calibration_notes;
CREATE TRIGGER deskcore_user_calib_updated_at
    BEFORE UPDATE ON user_calibration_notes
    FOR EACH ROW EXECUTE FUNCTION _deskcore_touch_updated_at();

-- WHEN 条件必须和 migrations/001_deskcore.sql 保持一致(那边有完整理由):
-- items 上还有 save_feedback_draft/save_manual_edit_draft 这类【打字即写】的
-- 路径, 无条件触发会让 updated_at 变成"最后一次自动存草稿", 而不是这一列
-- 定义的"人工决策最后变更时间"。
DROP TRIGGER IF EXISTS deskcore_items_updated_at ON items;
CREATE TRIGGER deskcore_items_updated_at
    BEFORE UPDATE ON items
    FOR EACH ROW
    WHEN (OLD.status IS DISTINCT FROM NEW.status
       OR OLD.example_label IS DISTINCT FROM NEW.example_label)
    EXECUTE FUNCTION _deskcore_touch_updated_at();

-- ── deskcore 发牌的原子预留 ──────────────────────────────────────────
-- 为什么需要它: draw_angles 原本是"读避重集 → Python 里挑 → 插入"三步。
-- 两个队友同时给同一项目发牌, 会各自读到"这个组合没用过"、各自插入成功,
-- 同一个角度被两批同时用掉 —— 而两边都报告成功, 跨批次唯一性的承诺破了。
-- 台账上只有非唯一索引, 拦不住。
--
-- 这里把三步收进一个事务, 用事务级 advisory lock 把【同一项目】的发牌串行化
-- (不同项目互不阻塞, 事务结束自动释放)。与 claim_one_job 的
-- FOR UPDATE SKIP LOCKED 是同一思路: 并发正确性交给数据库, 不靠应用层自觉。
--
-- _candidates: [{"angle_key": "...", "dims": {...}}, ...] 按优先级排好序,
--              过量供给(调用方给远多于 _want 的候选), 函数取前 _want 个可用的。
CREATE OR REPLACE FUNCTION deskcore_reserve_angles(
    _project_id UUID,
    _candidates JSONB,
    _drawn_by   UUID,
    _want       INT,
    _avoid_days INT DEFAULT 30
)
RETURNS TABLE(reserved_key TEXT, reserved_dims JSONB)
LANGUAGE plpgsql
-- 与 migrations/001_deskcore.sql 保持一致。fresh install 走本文件、已有库走
-- migrations/ —— 只改一边的话, 新建的库照旧 function_search_path_mutable
-- 且保留可变 search_path。(codex aw#57 review; migrations/README 也写了
-- "加表/加列必须两边都改", 函数同理)
SET search_path = pg_catalog, autowriter, extensions
-- ⚠️ autowriter 必须在里面(codex review 2026-08-24)。本文件里的表名一律**不带
-- schema 前缀**(整段 SQL 靠 search_path 解析), 而函数自己的 SET search_path
-- 会覆盖调用方的 —— 只写 pg_catalog+extensions 的话, 函数体里的 draft_fingerprints
-- 在【运行时】解析不到, 报 relation ... does not exist。
-- 最坏的是它怎么失败: 那句报错里带 "does not exist", 而 store.rpc_missing 的判据
-- 正好认这个词 —— 于是 check_drafts 会把"函数在、只是找不到表"当成"迁移没跑",
-- 静默退回 Python 慢路径, 永远不报警。
-- (migrations/*.sql 走的是另一条路: 那边表名全限定成 autowriter.*, 所以只需
--  pg_catalog+extensions。两边各自自洽即可, 但不能混。)
AS $$
DECLARE
    cand  JSONB;
    k     TEXT;
    taken INT := 0;
BEGIN
    IF _want <= 0 THEN
        RETURN;
    END IF;

    PERFORM pg_advisory_xact_lock(hashtext('deskcore_draw:' || _project_id::text));

    FOR cand IN SELECT * FROM jsonb_array_elements(_candidates) LOOP
        EXIT WHEN taken >= _want;
        k := cand->>'angle_key';
        CONTINUE WHEN k IS NULL OR k = '';

        -- 已产出成稿且仍在避重窗内 → 跳过。按 consumed_at 而不是 drawn_at:
        -- 审稿定稿常拖几天, 按 drawn_at 会让刚定稿的角度立刻可被重用。
        CONTINUE WHEN EXISTS (
            SELECT 1 FROM angle_ledger al
             WHERE al.project_id = _project_id
               AND al.angle_key  = k
               AND al.consumed_version_id IS NOT NULL
               AND al.consumed_at >= now() - make_interval(days => _avoid_days));

        -- 抽了还没写的占位, 1 天内 → 跳过。占位不该长期占坑, 否则连点几次
        -- 发牌就把组合空间锁死。
        CONTINUE WHEN EXISTS (
            SELECT 1 FROM angle_ledger al
             WHERE al.project_id = _project_id
               AND al.angle_key  = k
               AND al.consumed_version_id IS NULL
               AND al.drawn_at >= now() - interval '1 day');

        INSERT INTO angle_ledger (project_id, angle_key, dims, drawn_by)
        VALUES (_project_id, k, COALESCE(cand->'dims', '{}'::jsonb), _drawn_by);

        taken         := taken + 1;
        reserved_key  := k;
        reserved_dims := COALESCE(cand->'dims', '{}'::jsonb);
        RETURN NEXT;
    END LOOP;
END;
$$;

-- 只给 service_role。deskcore 是唯一调用方; 不加约束的话任何能访问 PostgREST
-- RPC 的角色都能往别人项目的台账里塞行(同 claim_one_job 的权限处理)。
REVOKE ALL ON FUNCTION deskcore_reserve_angles(UUID, JSONB, UUID, INT, INT) FROM PUBLIC;
REVOKE ALL ON FUNCTION deskcore_reserve_angles(UUID, JSONB, UUID, INT, INT) FROM anon;
REVOKE ALL ON FUNCTION deskcore_reserve_angles(UUID, JSONB, UUID, INT, INT) FROM authenticated;
GRANT EXECUTE ON FUNCTION deskcore_reserve_angles(UUID, JSONB, UUID, INT, INT) TO service_role;

-- ── deskcore 定稿入库的原子查重 ────────────────────────────────────────
-- 为什么需要: check_drafts 和 commit_drafts 是两次独立调用。两个队友各自 check
-- 时都看到同一份旧指纹集、双双 pass, 然后各自 commit —— 两篇撞车的稿子都进了库,
-- 跨人硬闸形同虚设。发牌那边已经用 advisory lock 串行化了, 这边不能留着。
--
-- 本函数在同一个事务里【重新查一遍 + 插入】, 用与 draw 相同的 project 级
-- advisory lock 串行化。只做【确定性】信号(开头精确 + 四字串 Jaccard)——
-- 标题语义那一路要 pgvector 距离算子, 且历史行可能没有向量, 留在 Python 侧的
-- check_drafts 里做; 这里挡住的是竞态窗口里最可能撞的那两类。
--
-- 返回每条的结果: inserted / rejected + 撞了谁。调用方据此告诉用户哪几条要重写。
CREATE OR REPLACE FUNCTION deskcore_commit_fingerprints(
    _project_id UUID,
    _rows       JSONB,        -- [{title,opening,opening_hash,ngram_hashes,title_embedding,version_id,angle_key}, ...]
    _user_id    UUID,
    _ngram_hard NUMERIC DEFAULT 0.35,
    -- 审计 COR-014: 写入侧的重查必须和 deskcore_check_drafts 同口径, 否则同一对
    -- 稿子会在两道闸上得到相反的结论。阈值仍由 Python 侧统一持有, 不在 SQL 写死。
    _contain_hard       NUMERIC DEFAULT 0.60,
    _contain_min_sample INT     DEFAULT 15
)
RETURNS TABLE(idx INT, status TEXT, collided_with TEXT, detail TEXT)
LANGUAGE plpgsql
SET search_path = pg_catalog, autowriter, extensions
-- 固定 search_path 的三段各自的作用见上面 deskcore_reserve_angles 那段说明;
-- extensions 是给 ::vector 用的, autowriter 是给不带前缀的表名用的。
AS $$
DECLARE
    r        JSONB;
    i        INT := -1;
    ng       TEXT[];
    ng_max   TEXT;
    oh       TEXT;
    hit      RECORD;
    best_j   NUMERIC;
    best_t   TEXT;
    best_c   NUMERIC;
    best_ct  TEXT;
    best_cs  INT;
    open_hit BOOLEAN;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('deskcore_draw:' || _project_id::text));

    FOR r IN SELECT * FROM jsonb_array_elements(_rows) LOOP
        i := i + 1;
        oh := r->>'opening_hash';
        SELECT COALESCE(array_agg(x), '{}') INTO ng
          FROM jsonb_array_elements_text(COALESCE(r->'ngram_hashes','[]'::jsonb)) x;
        ng_max := (SELECT max(x) FROM unnest(ng) x);

        -- ① 开头精确撞车
        -- ⚠️ 空开头(正文为空的 title-only 稿)不参与。Python 侧 fp.opening_hash
        -- 对空开头返回空串, 这里把空串和 NULL 一起排除 —— 否则所有 title-only
        -- 的行会互相"精确撞车", 而这是单独就判死的强信号, 没东西兜得住。
        -- (这段本来只改在 migrations/001 里, 本文件漏了 —— 两份 DDL 双写的
        --  典型代价, 见审计 SUP-010。)
        open_hit := FALSE;
        IF oh IS NOT NULL AND oh <> '' THEN
            SELECT TRUE, f.title INTO open_hit, best_t
              FROM draft_fingerprints f
             WHERE f.project_id = _project_id
               AND f.opening_hash = oh
               AND f.opening_hash <> ''
             LIMIT 1;
            open_hit := COALESCE(open_hit, FALSE);
        END IF;
        IF open_hit THEN
            idx := i; status := 'rejected';
            collided_with := best_t; detail := '正文开头与库中已有稿件完全一致';
            RETURN NEXT;
            CONTINUE;
        END IF;

        -- ② 四字串: Jaccard 与包含度同一次扫描算完, 口径与 deskcore_check_drafts
        --    完全一致(受限子域 + bottom-k 标准估计式)。见 migrations/005。
        best_j := 0; best_t := NULL;
        best_c := 0; best_ct := NULL; best_cs := 0;
        IF ng_max IS NOT NULL THEN
            FOR hit IN
                SELECT c.title,
                       m.inter::numeric / NULLIF(m.uni, 0)     AS j,
                       m.inter::numeric / NULLIF(m.smaller, 0) AS c,
                       m.smaller::int                          AS smaller
                  FROM (
                      SELECT f.title, f.ngram_hashes AS hs
                        FROM draft_fingerprints f
                       WHERE f.project_id = _project_id
                         AND f.ngram_hashes && ng
                         AND array_length(f.ngram_hashes, 1) IS NOT NULL
                  ) c
                  CROSS JOIN LATERAL (
                      SELECT LEAST(ng_max, (SELECT max(x) FROM unnest(c.hs) x)) AS t
                  ) tt
                  CROSS JOIN LATERAL (
                      SELECT count(*) FILTER (WHERE g.in_a AND g.in_b) AS inter,
                             count(*)                                  AS uni,
                             LEAST(count(*) FILTER (WHERE g.in_a),
                                   count(*) FILTER (WHERE g.in_b))     AS smaller
                        FROM (
                            SELECT z.h, bool_or(z.src = 'a') AS in_a,
                                        bool_or(z.src = 'b') AS in_b
                              FROM (SELECT x AS h, 'a' AS src
                                      FROM unnest(ng) x WHERE x <= tt.t
                                    UNION ALL
                                    SELECT y, 'b'
                                      FROM unnest(c.hs) y WHERE y <= tt.t) z
                             GROUP BY z.h
                        ) g
                  ) m
            LOOP
                IF hit.j IS NOT NULL AND hit.j > best_j THEN
                    best_j := hit.j; best_t := hit.title;
                END IF;
                IF hit.c IS NOT NULL AND hit.c > best_c THEN
                    best_c := hit.c; best_ct := hit.title; best_cs := hit.smaller;
                END IF;
            END LOOP;
        END IF;
        IF best_j >= _ngram_hard THEN
            idx := i; status := 'rejected'; collided_with := best_t;
            detail := format('正文与库中已有稿件大面积重合(四字串 Jaccard=%s)', round(best_j, 3));
            RETURN NEXT;
            CONTINUE;
        END IF;
        -- 样本量不够就不发言 —— 与 Python 侧 CONTAIN_MIN_SAMPLE 同一口径, 刻意
        -- fail-open(样本量小时无关稿子也能撞出高包含度, 按硬闸处理就是误杀)。
        IF best_cs >= _contain_min_sample AND best_c >= _contain_hard THEN
            idx := i; status := 'rejected'; collided_with := best_ct;
            detail := format('正文有 %s%% 的四字串出现在库中已有稿件里(照搬长稿)',
                             round(best_c * 100, 1));
            RETURN NEXT;
            CONTINUE;
        END IF;

        INSERT INTO draft_fingerprints
            (project_id, version_id, user_id, title, opening,
             title_embedding, opening_hash, ngram_hashes, angle_key)
        VALUES (
            _project_id,
            NULLIF(r->>'version_id','')::uuid,
            _user_id,
            COALESCE(r->>'title',''),
            COALESCE(r->>'opening',''),
            CASE WHEN r->'title_embedding' IS NULL OR jsonb_typeof(r->'title_embedding') = 'null'
                 THEN NULL ELSE (r->>'title_embedding')::vector END,
            oh,
            ng,
            NULLIF(r->>'angle_key','')
        );
        idx := i; status := 'inserted'; collided_with := NULL; detail := NULL;
        RETURN NEXT;
    END LOOP;
END;
$$;

REVOKE ALL ON FUNCTION deskcore_commit_fingerprints(UUID, JSONB, UUID, NUMERIC, NUMERIC, INT) FROM PUBLIC;
REVOKE ALL ON FUNCTION deskcore_commit_fingerprints(UUID, JSONB, UUID, NUMERIC, NUMERIC, INT) FROM anon;
REVOKE ALL ON FUNCTION deskcore_commit_fingerprints(UUID, JSONB, UUID, NUMERIC, NUMERIC, INT) FROM authenticated;
GRANT EXECUTE ON FUNCTION deskcore_commit_fingerprints(UUID, JSONB, UUID, NUMERIC, NUMERIC, INT) TO service_role;

-- ── deskcore 查重比对下推(审计 SUP-002 / ROB-004 / ROB-011) ─────────────
-- 原来是把整个项目的指纹(4000 行 × 768 维)拉进 Python 再逐对算余弦: 百 MB 级
-- 传输 + 三千万次乘加, 单次数十秒, 而且占着 uvicorn 线程池的一个槽 ——
-- 池子占满 /health 就跟着排队, 平台健康检查超时重启容器, 正在跑的调用全断。
-- 三条审计发现是同一个根因。
--
-- ⚠️ 余弦这一路刻意【不走 draft_fp_embedding_idx】(上面那个 ivfflat)。
-- ivfflat 是近似最近邻(默认 probes=1 只扫一个桶), 对查重硬闸是致命的 ——
-- 漏掉的那条正是要拦下的重复稿, 且不报错。这里 ORDER BY 的是子查询算好的
-- 别名 sim, 不是 `title_embedding <=> v` —— pgvector 的索引只认后一种形态,
-- 换成前者规划器必然走顺序扫描, 精确且可预期。完整理由见 migrations/004。
CREATE OR REPLACE FUNCTION deskcore_check_drafts(
    _project_id UUID,
    _rows       JSONB        -- [{opening_hash, ngram_hashes, title_embedding}, ...]
)
RETURNS TABLE(
    idx        INT,
    best_sim   NUMERIC,
    sim_title  TEXT,
    best_j     NUMERIC,
    j_title    TEXT,
    open_exact BOOLEAN,
    open_title TEXT,
    best_c     NUMERIC,      -- 四字串包含度(审计 COR-014)
    c_title    TEXT,
    c_sample   INT           -- 该次估计的有效样本量; 太小则调用方不采信
)
LANGUAGE plpgsql
STABLE
SET search_path = pg_catalog, autowriter, extensions
-- 固定 search_path 的三段各自的作用见上面 deskcore_reserve_angles 那段说明;
-- extensions 是给 ::vector 用的, autowriter 是给不带前缀的表名用的。
AS $$
DECLARE
    r      JSONB;
    i      INT := -1;
    ng     TEXT[];
    ng_max TEXT;
    oh     TEXT;
    v      vector;
BEGIN
    FOR r IN SELECT * FROM jsonb_array_elements(_rows) LOOP
        i := i + 1;
        idx := i;
        best_sim := 0; sim_title := NULL;
        best_j := 0;   j_title := NULL;
        best_c := 0;   c_title := NULL;  c_sample := 0;
        open_exact := FALSE; open_title := NULL;

        oh := NULLIF(r->>'opening_hash', '');
        SELECT COALESCE(array_agg(x), '{}') INTO ng
          FROM jsonb_array_elements_text(COALESCE(r->'ngram_hashes','[]'::jsonb)) x;
        ng_max := (SELECT max(x) FROM unnest(ng) x);
        v := CASE WHEN r->'title_embedding' IS NULL
                    OR jsonb_typeof(r->'title_embedding') = 'null'
                  THEN NULL ELSE (r->>'title_embedding')::vector END;

        -- ① 开头精确撞车。空开头不参与(否则 title-only 的稿子会互相撞车)。
        IF oh IS NOT NULL THEN
            SELECT f.title INTO open_title
              FROM draft_fingerprints f
             WHERE f.project_id = _project_id AND f.opening_hash = oh
             LIMIT 1;
            open_exact := open_title IS NOT NULL;
        END IF;

        -- ② 四字串: Jaccard 与包含度同一次扫描算完, 各取各的最佳命中。
        --    ngram_hashes 存的是 bottom-k **sketch** 不是完整集合, 所以两个
        --    指标都只在【两个 sketch 都覆盖到的 hash 区间】上算:
        --    t = min(两边最大值), 因为 v ≤ t 时"v ∈ 原集合" ⟺ "v ∈ sketch"。
        --    直接对整个数组求交并比会系统性偏低(实测最大 0.125, 而硬闸线 0.35)。
        --    包含度 |A∩B|/min(|A|,|B|) 专抓 Jaccard 结构上抓不到的形状:
        --    短稿整段照搬长稿(那时 J 的真值就等于长度比, 再准也够不着阈值)。
        --    完整推导与阈值的零分布见 migrations/005 + deskcore/fingerprint.py。
        --    ⚠️ 逐行走 LATERAL, 不要拆成两个 CTE 再按 title join —— title 不唯一。
        IF ng_max IS NOT NULL THEN
            WITH cand AS (
                SELECT f.title, f.ngram_hashes AS hs
                  FROM draft_fingerprints f
                 WHERE f.project_id = _project_id
                   AND f.ngram_hashes && ng
                   AND array_length(f.ngram_hashes, 1) IS NOT NULL
            ),
            metrics AS (
                SELECT c.title, m.inter::numeric AS inter,
                       m.uni::numeric AS uni, m.smaller::numeric AS smaller
                  FROM cand c
                  CROSS JOIN LATERAL (
                      SELECT LEAST(ng_max, (SELECT max(x) FROM unnest(c.hs) x)) AS t
                  ) tt
                  CROSS JOIN LATERAL (
                      SELECT count(*) FILTER (WHERE g.in_a AND g.in_b) AS inter,
                             count(*)                                  AS uni,
                             LEAST(count(*) FILTER (WHERE g.in_a),
                                   count(*) FILTER (WHERE g.in_b))     AS smaller
                        FROM (
                            SELECT z.h, bool_or(z.src = 'a') AS in_a,
                                        bool_or(z.src = 'b') AS in_b
                              FROM (SELECT x AS h, 'a' AS src
                                      FROM unnest(ng) x WHERE x <= tt.t
                                    UNION ALL
                                    SELECT y, 'b'
                                      FROM unnest(c.hs) y WHERE y <= tt.t) z
                             GROUP BY z.h
                        ) g
                  ) m
            ),
            -- ⚠️ 两路的最佳命中必须在【同一条语句】里取完。WITH 的作用域只有
            --    一条语句 —— 拆成两条 SELECT 的话第二条会报 relation "metrics"
            --    does not exist。(在真 PostgreSQL 上跑才发现的; 光看代码
            --    和 py_compile 都看不出来。)
            jbest AS (
                SELECT x.title, x.j FROM (
                    SELECT m.title, m.inter / NULLIF(m.uni, 0) AS j FROM metrics m
                ) x WHERE x.j IS NOT NULL ORDER BY x.j DESC, x.title LIMIT 1
            ),
            cbest AS (
                SELECT x.title, x.c, x.smaller FROM (
                    SELECT m.title, m.inter / NULLIF(m.smaller, 0) AS c,
                           m.smaller::int AS smaller
                      FROM metrics m
                ) x WHERE x.c IS NOT NULL ORDER BY x.c DESC, x.title LIMIT 1
            )
            SELECT (SELECT b.j FROM jbest b), (SELECT b.title FROM jbest b),
                   (SELECT b.c FROM cbest b), (SELECT b.title FROM cbest b),
                   (SELECT b.smaller FROM cbest b)
              INTO best_j, j_title, best_c, c_title, c_sample;
            best_j := COALESCE(best_j, 0);
            best_c := COALESCE(best_c, 0);
            c_sample := COALESCE(c_sample, 0);
        END IF;

        -- ③ 标题语义余弦(见上: 顺序扫描, 精确)。
        IF v IS NOT NULL THEN
            SELECT t.title, t.sim INTO sim_title, best_sim
              FROM (
                SELECT f.title, 1 - (f.title_embedding <=> v) AS sim
                  FROM draft_fingerprints f
                 WHERE f.project_id = _project_id
                   AND f.title_embedding IS NOT NULL
              ) t
             ORDER BY t.sim DESC, t.title
             LIMIT 1;
            IF best_sim IS NULL OR best_sim <= 0 THEN
                best_sim := 0; sim_title := NULL;
            END IF;
        END IF;

        RETURN NEXT;
    END LOOP;
END;
$$;

REVOKE ALL ON FUNCTION deskcore_check_drafts(UUID, JSONB) FROM PUBLIC;
REVOKE ALL ON FUNCTION deskcore_check_drafts(UUID, JSONB) FROM anon;
REVOKE ALL ON FUNCTION deskcore_check_drafts(UUID, JSONB) FROM authenticated;
GRANT EXECUTE ON FUNCTION deskcore_check_drafts(UUID, JSONB) TO service_role;

-- ── list_projects 的指纹批量计数(审计 SUP-004) ──────────────────────────
-- PostgREST 不会 GROUP BY, 没有这个函数就只能每个项目发一次 count=exact:
-- 40 个项目 41 次往返, 而 list_projects 是模型最常调的第一个工具。
CREATE OR REPLACE FUNCTION deskcore_fingerprint_counts(_project_ids UUID[])
RETURNS TABLE(project_id UUID, n BIGINT)
LANGUAGE sql
STABLE
SET search_path = pg_catalog, autowriter, extensions
-- 固定 search_path 的三段各自的作用见上面 deskcore_reserve_angles 那段说明;
-- extensions 是给 ::vector 用的, autowriter 是给不带前缀的表名用的。
AS $$
    SELECT f.project_id, count(*)::bigint
      FROM draft_fingerprints f
     WHERE f.project_id = ANY(_project_ids)
     GROUP BY f.project_id;
$$;

REVOKE ALL ON FUNCTION deskcore_fingerprint_counts(UUID[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION deskcore_fingerprint_counts(UUID[]) FROM anon;
REVOKE ALL ON FUNCTION deskcore_fingerprint_counts(UUID[]) FROM authenticated;
GRANT EXECUTE ON FUNCTION deskcore_fingerprint_counts(UUID[]) TO service_role;

-- ── 调教笔记的 CAS 写入（审计 COR-003 / migrations/002）────────────────────
-- 为什么必须是 RPC: memory.save_calibration_notes 的乐观并发原本写成
--   .eq("calibration_notes", expected_before_text)
-- PostgREST 把过滤条件放在 **URL query string** 里, 而 calibration_notes 的软
-- 上限是 4000 字(memory.py:_dedup_calibration_lines)。中文 URL-encode 后一个字
-- 变 9 个字符 —— 4000 字 ≈ 36KB 的查询串, 远超网关(nginx/Kong)对单行请求头的
-- 上限。越线之后每一次 CAS 写入都 414/400, 而 memory._append_new_observations
-- 的 `except Exception: return None` 把它吞掉:
--   迭代 / 手动精修 / 整批反思 / merger_taste 四条自动学习路径【全部静默停写】,
--   用户以为在学、其实笔记早就不动了, 且没有任何告警。
--
-- 改成把 witness 压成 md5 送进 body: 请求大小与笔记长度解耦, 32 字节定长。
-- md5 在这里【只做变更检测】, 不是安全用途 —— 冲突的后果是多重试一轮。
--
-- 空 witness 的语义是"我读到的是没有笔记": COALESCE 让 NULL 与 '' 都落到
-- md5('') 上, 顺带把 R-036 那个 `.eq("", ...)` 匹配不到 NULL 行的特例消掉了
-- (从未写过笔记的项目该列是 NULL 而不是 '')。
--
-- SECURITY INVOKER(默认): 以调用者身份跑, projects_owner 那条 RLS 照常生效,
-- 用户改不了别人的项目。search_path 固定, 同 deskcore 两个 RPC 的处理。
CREATE OR REPLACE FUNCTION update_calibration_notes_cas(
    _project_id   UUID,
    _expected_md5 TEXT,
    _notes        TEXT
)
RETURNS TABLE(id UUID, calibration_notes TEXT)
LANGUAGE sql
SECURITY INVOKER
SET search_path = pg_catalog, autowriter
AS $$
    UPDATE autowriter.projects p
       SET calibration_notes = _notes
     WHERE p.id = _project_id
       AND md5(COALESCE(p.calibration_notes, '')) = _expected_md5
    RETURNING p.id, p.calibration_notes;
$$;
REVOKE ALL ON FUNCTION update_calibration_notes_cas(UUID, TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION update_calibration_notes_cas(UUID, TEXT, TEXT) FROM anon;
GRANT EXECUTE ON FUNCTION update_calibration_notes_cas(UUID, TEXT, TEXT)
    TO authenticated, service_role;
