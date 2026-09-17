-- ══════════════════════════════════════════════════════════════════════
-- 009 · 写作台 ↔ TV 的稿子对照(2026-09-17)
--
-- 病灶
-- ────
-- 写作台发出去的稿子, 在 TV(truth_vault.notes)里一条都认不回来:
-- 5966 条笔记, source_autowriter_version_id 全 NULL。原设计让运营把
-- export_drafts 导出的六个 lineage 列手抄进飞书表, 三周零匹配 —— 到 TV 手里
-- 的表根本没有那六列。于是 TV 的模型对比视图长期空集, 「爆没爆」永远回不到
-- 写作台; 而反过来, 08-28 才建的项目里, 岸深发了 144 篇一篇都没进过库,
-- 西屋 301 篇库里连版本都没有 —— 查重闸对它们一无所知。
--
-- 改法
-- ────
-- 两边在同一个 Supabase 项目里, 内容都是写作台产的: **在库里按内容对**, 不靠
-- 人抄 ID。TV 存的是全文(百健士对上的 143 篇里 115 篇正文逐字相同, 其余只
-- 多了话题标签), 发布时间与入库时间的间隔中位数 1~3 天。
--
--   tv_project_map   TV 的 project_id('SPX_phase1') ↔ 写作台项目。一个 TV 项目
--                    可以对应多个写作台项目(WTG 有 15 个方向), 但**只有一行**
--                    是 ingest_target —— 对不上的笔记补录进这一个。
--   tv_note_links    每条 TV 笔记 → 写作台版本。match_kind 记的是怎么对上的
--                    (body_exact / title_exact / fuzzy / ingested / ambiguous /
--                    tv_lineage), 判不出的留 candidates 给人看, 不硬猜。
--   deskcore_tv_notes            读 truth_vault.notes(跨 schema, SECURITY
--                                DEFINER; 库里没有 truth_vault 时返回空集,
--                                本地 harness 与新库都跑得过)。
--   deskcore_tv_backfill_lineage 把对照写回 TV 的 source_autowriter_*
--                                两列 —— **只填 NULL 的行**, 从不覆盖 TV 自己
--                                写的值。Python 侧默认不调它(--write-tv 才调),
--                                这是 TV 的列, 回填之前要跟 TV 打招呼。
--
-- 两个函数都用 EXECUTE 动态 SQL: plpgsql 在 CREATE 时不解析表名, 加上
-- to_regclass 守卫, 让没有 truth_vault 的库(本地 harness、fresh install)也能
-- 建出来并干净地返回空 —— 而不是在 CREATE 或第一次调用时炸掉。
--
-- 幂等: IF NOT EXISTS / CREATE OR REPLACE / GRANT, 重复执行干净 no-op。
-- 生产库尚未执行(合并后按 runbook §2 跑, doctor 会探)。
-- ══════════════════════════════════════════════════════════════════════

BEGIN;

SET LOCAL search_path TO autowriter, extensions, public;

CREATE TABLE IF NOT EXISTS autowriter.tv_project_map (
    tv_project_id  TEXT NOT NULL,
    project_id     UUID NOT NULL REFERENCES autowriter.projects(id) ON DELETE CASCADE,
    ingest_target  BOOLEAN NOT NULL DEFAULT FALSE,
    note           TEXT NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tv_project_id, project_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS tv_project_map_one_target
    ON autowriter.tv_project_map (tv_project_id) WHERE ingest_target;
CREATE INDEX IF NOT EXISTS tv_project_map_project_idx
    ON autowriter.tv_project_map (project_id);

CREATE TABLE IF NOT EXISTS autowriter.tv_note_links (
    note_id        TEXT PRIMARY KEY,
    tv_project_id  TEXT NOT NULL,
    project_id     UUID REFERENCES autowriter.projects(id) ON DELETE CASCADE,
    version_id     UUID REFERENCES autowriter.versions(id) ON DELETE SET NULL,
    item_id        UUID,
    match_kind     TEXT NOT NULL CHECK (match_kind IN (
                       'body_exact', 'title_exact', 'fuzzy', 'ingested',
                       'ambiguous', 'unmatched', 'tv_lineage')),
    score          REAL,
    lag_days       INTEGER,
    candidates     JSONB,
    synced_to_tv_at TIMESTAMPTZ,
    matched_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tv_note_links_project_kind_idx
    ON autowriter.tv_note_links (project_id, match_kind);
CREATE INDEX IF NOT EXISTS tv_note_links_version_idx
    ON autowriter.tv_note_links (version_id);

-- 建出来 ≠ 能访问(migrations/README): service_role 不绕表级 GRANT。
GRANT SELECT, INSERT, UPDATE, DELETE ON
    autowriter.tv_project_map,
    autowriter.tv_note_links
    TO service_role;

-- ── 读 TV 的笔记(keyset 翻页; PostgREST 的 db-max-rows 对 RPC 同样生效) ──
CREATE OR REPLACE FUNCTION autowriter.deskcore_tv_notes(
    _tv_project_id TEXT,
    _since         TIMESTAMPTZ DEFAULT NULL,
    _after         TEXT DEFAULT NULL,
    _limit         INT DEFAULT 500
)
RETURNS TABLE(
    note_id TEXT, publish_time TIMESTAMPTZ, tier TEXT, raw_content TEXT,
    title TEXT, body TEXT, source_autowriter_version_id UUID,
    created_at TIMESTAMPTZ
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF to_regclass('truth_vault.notes') IS NULL THEN
        RETURN;
    END IF;
    RETURN QUERY EXECUTE
        'SELECT n.note_id, n.publish_time::timestamptz, n.tier, n.raw_content, '
        '       n.title, n.body, n.source_autowriter_version_id, '
        '       n.created_at::timestamptz '
        '  FROM truth_vault.notes n '
        ' WHERE n.project_id = $1 '
        '   AND ($2 IS NULL OR n.created_at >= $2) '
        '   AND ($3 IS NULL OR n.note_id > $3) '
        ' ORDER BY n.note_id '
        ' LIMIT $4'
        USING _tv_project_id, _since, _after, GREATEST(_limit, 1);
END;
$$;
REVOKE ALL ON FUNCTION autowriter.deskcore_tv_notes(TEXT, TIMESTAMPTZ, TEXT, INT) FROM PUBLIC;
REVOKE ALL ON FUNCTION autowriter.deskcore_tv_notes(TEXT, TIMESTAMPTZ, TEXT, INT) FROM anon;
REVOKE ALL ON FUNCTION autowriter.deskcore_tv_notes(TEXT, TIMESTAMPTZ, TEXT, INT) FROM authenticated;
GRANT EXECUTE ON FUNCTION autowriter.deskcore_tv_notes(TEXT, TIMESTAMPTZ, TEXT, INT) TO service_role;

-- ── 把对照写回 TV: 只填 NULL 的行, 返回实际更新的行数 ──
CREATE OR REPLACE FUNCTION autowriter.deskcore_tv_backfill_lineage(_links JSONB)
RETURNS INT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    _n INT := 0;
BEGIN
    IF to_regclass('truth_vault.notes') IS NULL THEN
        RETURN 0;
    END IF;
    EXECUTE
        'UPDATE truth_vault.notes t '
        '   SET source_autowriter_version_id = l.version_id, '
        '       source_autowriter_item_id    = COALESCE(t.source_autowriter_item_id, l.item_id) '
        '  FROM jsonb_to_recordset($1) AS l(note_id TEXT, version_id UUID, item_id UUID) '
        ' WHERE t.note_id = l.note_id '
        '   AND l.version_id IS NOT NULL '
        '   AND t.source_autowriter_version_id IS NULL'
        USING _links;
    GET DIAGNOSTICS _n = ROW_COUNT;
    RETURN _n;
END;
$$;
REVOKE ALL ON FUNCTION autowriter.deskcore_tv_backfill_lineage(JSONB) FROM PUBLIC;
REVOKE ALL ON FUNCTION autowriter.deskcore_tv_backfill_lineage(JSONB) FROM anon;
REVOKE ALL ON FUNCTION autowriter.deskcore_tv_backfill_lineage(JSONB) FROM authenticated;
GRANT EXECUTE ON FUNCTION autowriter.deskcore_tv_backfill_lineage(JSONB) TO service_role;

COMMIT;
