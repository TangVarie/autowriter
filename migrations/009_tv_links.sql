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
--   ingest_locks + deskcore_ingest_lock / deskcore_ingest_unlock
--                                补录的**跨进程**互斥(codex review #81 P1): tv-sync 是
--                                CLI/cron 进程, ingest_published 工具跑在服务进程里,
--                                两边都会"先读库里有没有指纹、再写", 进程内的锁管不到
--                                对方。advisory lock 跨不过 PostgREST 的多次请求, 所以
--                                用一张锁表: 按项目一行, 带 TTL(持有方崩了也能被接管),
--                                拿锁/放锁各一个原子 RPC。
--
-- 两个跨 schema 的函数都用 EXECUTE 动态 SQL: plpgsql 在 CREATE 时不解析表名, 加上
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

-- ── 补录锁: 按项目一行, 到期可被接管 ──
CREATE TABLE IF NOT EXISTS autowriter.ingest_locks (
    project_id   UUID PRIMARY KEY REFERENCES autowriter.projects(id) ON DELETE CASCADE,
    holder       TEXT NOT NULL,
    acquired_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL
);
GRANT SELECT, INSERT, UPDATE, DELETE ON autowriter.ingest_locks TO service_role;

-- 拿锁: 没人持有 / 持有已过期 / 就是自己 → 拿到(TRUE); 别人还持有 → FALSE。
-- INSERT … ON CONFLICT DO UPDATE … WHERE 在同一条语句里判断并写入, 两个进程同时
-- 来只有一个能改到那一行(行锁), 不需要额外的事务控制。
CREATE OR REPLACE FUNCTION autowriter.deskcore_ingest_lock(
    _project_id  UUID,
    _holder      TEXT,
    _ttl_seconds INT DEFAULT 600
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    _got BOOLEAN := FALSE;
BEGIN
    -- clock_timestamp() 而不是 now(): now() 是事务开始时间, 在一个长事务里
    -- (或 harness 把几条语句塞进同一次 psql 调用时)会让"到期"永远判不出来。
    INSERT INTO autowriter.ingest_locks (project_id, holder, acquired_at, expires_at)
    VALUES (_project_id, _holder, clock_timestamp(),
            clock_timestamp() + make_interval(secs => GREATEST(_ttl_seconds, 1)))
    ON CONFLICT (project_id) DO UPDATE
       SET holder = EXCLUDED.holder, acquired_at = clock_timestamp(),
           expires_at = EXCLUDED.expires_at
     WHERE autowriter.ingest_locks.expires_at < clock_timestamp()
        OR autowriter.ingest_locks.holder = EXCLUDED.holder
    RETURNING TRUE INTO _got;
    RETURN COALESCE(_got, FALSE);
END;
$$;
REVOKE ALL ON FUNCTION autowriter.deskcore_ingest_lock(UUID, TEXT, INT) FROM PUBLIC;
REVOKE ALL ON FUNCTION autowriter.deskcore_ingest_lock(UUID, TEXT, INT) FROM anon;
REVOKE ALL ON FUNCTION autowriter.deskcore_ingest_lock(UUID, TEXT, INT) FROM authenticated;
GRANT EXECUTE ON FUNCTION autowriter.deskcore_ingest_lock(UUID, TEXT, INT) TO service_role;

-- 放锁: 只放自己持有的那一行。别人的锁(或已被接管的)不动, 返回 FALSE。
CREATE OR REPLACE FUNCTION autowriter.deskcore_ingest_unlock(_project_id UUID, _holder TEXT)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    _got BOOLEAN := FALSE;
BEGIN
    DELETE FROM autowriter.ingest_locks
     WHERE project_id = _project_id AND holder = _holder
    RETURNING TRUE INTO _got;
    RETURN COALESCE(_got, FALSE);
END;
$$;
REVOKE ALL ON FUNCTION autowriter.deskcore_ingest_unlock(UUID, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION autowriter.deskcore_ingest_unlock(UUID, TEXT) FROM anon;
REVOKE ALL ON FUNCTION autowriter.deskcore_ingest_unlock(UUID, TEXT) FROM authenticated;
GRANT EXECUTE ON FUNCTION autowriter.deskcore_ingest_unlock(UUID, TEXT) TO service_role;

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
