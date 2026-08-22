-- ════════════════════════════════════════════════════════════════════
-- migrations/001_deskcore.sql
-- ════════════════════════════════════════════════════════════════════
--
-- deskcore(写作台内核 MCP 服务)的四张表 + items.updated_at
-- 决策: truth-vault DECISIONS.md D-041 / docs/10-sister-repo-followups.md R-034
--
-- 背景:
--   Streamlit 界面停用(app.py 5560 行、每次交互全量重跑 = 慢的根源), 但库里
--   几年的积累(40 项目 / 269 条记忆 / 103 条已确认负例 / 调校笔记)原样保留,
--   写作能力经 deskcore 挂到 WorkBuddy / Claude Code / CodeBuddy。
--
--   本迁移只【补齐】外置后新增的能力, 现有 8 张表一行不动。
--
-- 每张表对应一个已确诊的根因:
--
--   angle_ledger       ← 跨批次没有"哪些角度组合用过"的持久记录。
--                        generator._assign_slot_coordinates(:1301) 只在单批内
--                        去重, 且已被移除成死代码(:1576-1584)。模型是无状态的,
--                        光靠提示词让它"注意不要重复"做不到。
--
--   draft_fingerprints ← 查重池 queue_embeddings 是 worker 进程内的内存字典
--                        (app.py:1024), 进程一重启就空; 且只比标题。
--                        本表是持久 / 全量 / 跨人 / 跨批次的查重底座。
--
--   user_calibration_notes ← projects.calibration_notes 是项目级单份, 承载不了
--                        「项目规则团队共享 + 个人风格私有」的分层。项目级那份
--                        保留为共享基线, 本表是个人叠加层。
--
--   style_edits        ← 手动精修 diff 的原始存放处。笔记是导出物、diff 才是
--                        事实, 换蒸馏 prompt 时要能重算。
--
--   items.updated_at   ← TV scripts/sync_autowriter_decisions_to_prepublish.py:36-40
--                        记的缺陷: 没这列只能按 created_at 过滤, 迟到的人工
--                        决策会漏收(--since-days 从 90 改 365 是补丁)。
--
-- RLS: 全部 enable 不建 policy = service_role only。deskcore 持 service_role,
--      隔离口径由服务端自己执行(共享层按 project_id 读全量、个人层带 user_id)。
--      这是刻意的 —— 「项目规则团队共享」要求跨 owner 读规则, RLS 的
--      user_id = auth.uid() 做不到。
--
-- 幂等: 重复执行不报错。
--
-- 回滚:
--   DROP TABLE IF EXISTS draft_fingerprints CASCADE;
--   DROP TABLE IF EXISTS angle_ledger CASCADE;
--   DROP TABLE IF EXISTS user_calibration_notes CASCADE;
--   DROP TABLE IF EXISTS style_edits CASCADE;
--   ALTER TABLE items DROP COLUMN IF EXISTS updated_at;
--   DROP FUNCTION IF EXISTS _deskcore_touch_updated_at();
-- ════════════════════════════════════════════════════════════════════

BEGIN;

SET LOCAL search_path TO autowriter, extensions, public;

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;

-- ── 1. 发牌台账 (共享层) ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS autowriter.angle_ledger (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id          UUID NOT NULL REFERENCES autowriter.projects(id) ON DELETE CASCADE,
    angle_key           TEXT NOT NULL,
    dims                JSONB NOT NULL DEFAULT '{}'::jsonb,
    drawn_by            UUID,
    drawn_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    consumed_version_id UUID,
    consumed_at         TIMESTAMPTZ
);
COMMENT ON COLUMN autowriter.angle_ledger.angle_key IS
    'dims 的规范化指纹(只取四个主维度; 情绪强度/时效/词感是叠加项不进 key, 否则换个词感就被当成没用过)';
COMMENT ON COLUMN autowriter.angle_ledger.consumed_version_id IS
    'NULL=抽了未用(占位, 1 天后自然过期); 非 NULL=已产出成稿, 按 avoid_days 避重';
CREATE INDEX IF NOT EXISTS angle_ledger_project_key_idx
    ON autowriter.angle_ledger (project_id, angle_key);
CREATE INDEX IF NOT EXISTS angle_ledger_project_drawn_idx
    ON autowriter.angle_ledger (project_id, drawn_at DESC);
ALTER TABLE autowriter.angle_ledger ENABLE ROW LEVEL SECURITY;

-- ── 2. 成稿指纹库 (共享层) ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS autowriter.draft_fingerprints (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id      UUID NOT NULL REFERENCES autowriter.projects(id) ON DELETE CASCADE,
    version_id      UUID,
    user_id         UUID,
    title           TEXT NOT NULL DEFAULT '',
    opening         TEXT NOT NULL DEFAULT '',
    title_embedding vector(768),
    opening_hash    TEXT,
    ngram_hashes    TEXT[] NOT NULL DEFAULT '{}',
    angle_key       TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
COMMENT ON COLUMN autowriter.draft_fingerprints.title_embedding IS
    'Gemini text-embedding-004 768d, 与 versions.embedding 同模型可互比';
COMMENT ON COLUMN autowriter.draft_fingerprints.opening_hash IS
    '正文首个非空行前 25 字规范化后 sha256 前 16 位 —— 标题换了也能抓';
COMMENT ON COLUMN autowriter.draft_fingerprints.ngram_hashes IS
    '正文四字串 shingle 的 hash 采样 —— 抓换了词还是同一篇的换皮改写';
CREATE INDEX IF NOT EXISTS draft_fp_project_created_idx
    ON autowriter.draft_fingerprints (project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS draft_fp_opening_hash_idx
    ON autowriter.draft_fingerprints (project_id, opening_hash)
    WHERE opening_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS draft_fp_ngram_gin_idx
    ON autowriter.draft_fingerprints USING GIN (ngram_hashes);
CREATE INDEX IF NOT EXISTS draft_fp_embedding_idx
    ON autowriter.draft_fingerprints USING ivfflat (title_embedding vector_cosine_ops)
    WITH (lists = 100);
ALTER TABLE autowriter.draft_fingerprints ENABLE ROW LEVEL SECURITY;

-- ── 3. 个人调校笔记 (私有层) ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS autowriter.user_calibration_notes (
    project_id  UUID NOT NULL REFERENCES autowriter.projects(id) ON DELETE CASCADE,
    user_id     UUID NOT NULL,
    notes       TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project_id, user_id)
);
ALTER TABLE autowriter.user_calibration_notes ENABLE ROW LEVEL SECURITY;

-- ── 4. 手动精修 diff (私有层) ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS autowriter.style_edits (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id  UUID NOT NULL REFERENCES autowriter.projects(id) ON DELETE CASCADE,
    user_id     UUID NOT NULL,
    ai_title    TEXT NOT NULL DEFAULT '',
    ai_body     TEXT NOT NULL DEFAULT '',
    my_title    TEXT NOT NULL DEFAULT '',
    my_body     TEXT NOT NULL DEFAULT '',
    note        TEXT,
    distilled   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
COMMENT ON COLUMN autowriter.style_edits.distilled IS
    '是否已蒸馏进 user_calibration_notes。想整体重算就把这列置回 FALSE';
CREATE INDEX IF NOT EXISTS style_edits_owner_idx
    ON autowriter.style_edits (project_id, user_id, created_at DESC);
ALTER TABLE autowriter.style_edits ENABLE ROW LEVEL SECURITY;

-- ── 5. items.updated_at ──────────────────────────────────────────────
-- 回填成 created_at 而不是 NOW(), 免得历史行全部看起来"刚改过"。
ALTER TABLE autowriter.items ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
UPDATE autowriter.items SET updated_at = created_at WHERE updated_at IS NULL;
ALTER TABLE autowriter.items ALTER COLUMN updated_at SET DEFAULT NOW();
COMMENT ON COLUMN autowriter.items.updated_at IS
    '人工决策(status/example_label)最后变更时间。补"只能按 created_at 过滤导致漏收迟到决策"的缺陷';
CREATE INDEX IF NOT EXISTS items_updated_at_idx ON autowriter.items (updated_at DESC);

-- ── 6. updated_at 触发器 ─────────────────────────────────────────────
-- DEFAULT 只在 INSERT 生效; upsert 的 DO UPDATE 分支必须靠触发器刷新。
CREATE OR REPLACE FUNCTION autowriter._deskcore_touch_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS deskcore_user_calib_updated_at ON autowriter.user_calibration_notes;
CREATE TRIGGER deskcore_user_calib_updated_at
    BEFORE UPDATE ON autowriter.user_calibration_notes
    FOR EACH ROW EXECUTE FUNCTION autowriter._deskcore_touch_updated_at();

DROP TRIGGER IF EXISTS deskcore_items_updated_at ON autowriter.items;
CREATE TRIGGER deskcore_items_updated_at
    BEFORE UPDATE ON autowriter.items
    FOR EACH ROW EXECUTE FUNCTION autowriter._deskcore_touch_updated_at();

COMMIT;

-- ── 校验 (人工跑, 不在事务内) ────────────────────────────────────────
-- SELECT table_name FROM information_schema.tables WHERE table_schema='autowriter'
--   AND table_name IN ('angle_ledger','draft_fingerprints','user_calibration_notes','style_edits');
--   → 应返回 4 行
-- SELECT count(*) FROM autowriter.items WHERE updated_at IS NULL;   → 应为 0
