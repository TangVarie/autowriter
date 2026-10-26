-- ══════════════════════════════════════════════════════════════════════
-- 016 · tv-sync 不再读飞书里已删的篇(2026-10-26, I-22)
--
-- 为什么
-- ──────
-- TV 的 truth_vault.notes 只增不删: 运营在飞书里删掉或挪走的篇, TV 那一行留着, 进 truth_vault.v_notes_vanished
-- (TV D-103: 盖过 last_seen 戳、但不在本项目最近一次完整同步里)。009 的 deskcore_tv_notes 不看这张视图, 于是
-- tv-sync 每晚把这些篇照样读回来重对:
--   · 已对上 / 已补录的: 跳过, 无害;
--   · 对不上的: 项目有补录目标时会被当成新稿补录成副本 —— 进了那个项目的指纹库, 查重会拿它拦新稿;
--     没有补录目标时(W-10)每晚重写一遍 unmatched。
-- 10-26 实况: 运营把 79 篇成人藻油从 BJS_phase1 的飞书表挪到新表(TV 记为 BJS_phase2), TV 里 BJS_phase1 那 79 行
-- 进了 v_notes_vanished, 其中 14 篇是 unmatched 且有正文。BJS_phase1 的补录目标(10-23 暂时改成 false)一改回 true,
-- 下一晚这 14 篇就会被补录进儿童藻油的写作台项目。HATHERINE_phase1 已经在每晚重写 6 条这样的 unmatched。
--
-- 这次做什么
-- ──────────
-- deskcore_tv_notes 多一个条件: 视图在时, 不返回本项目里 v_notes_vanished 列着的篇。签名、返回列、
-- SECURITY DEFINER、search_path 都与 009 一样, 所以 CREATE OR REPLACE 就够(不用 DROP), 授权也不丢; 下面照样
-- 重发一遍。tv-sync 和 tv-resolve 都只经这个 RPC 读 TV, Python 不用改: 删了的篇不进对照, 它已有的
-- tv_note_links 一行不动(已对上的血缘、已补录的副本、unmatched 都留着, 删不删由人定)。
--
-- `v.project_id = $1` 不是多余的: 它让项目过滤下推进视图里那个 DISTINCT ON(按项目取最近一次完整同步), 否则每次
-- 调用都要把全库每个项目算一遍(10-26 生产库 EXPLAIN ANALYZE: BJS_phase1 182 行, 2.3 ms)。
--
-- 失效方向都是安全的: 视图不在(TV 还没建 / 本地 harness / 新库)就照旧全给; TV 两次完整同步重叠时会有篇被短暂
-- 判成消失, 那一晚它只是不被读到, 什么都不写。
--
-- ⚠️ 用函数属主跑(Supabase 上 SQL Editor / apply_migration 的默认身份 postgres): CREATE OR REPLACE 要求属主。
-- 函数以属主身份读 truth_vault.notes(RLS 开着、无 policy)和视图, 生产库的 postgres 有 BYPASSRLS。
-- doctor 探不到函数体(同 008), 报 unprobeable 并给出 SQL Editor 里查的那句。
--
-- 幂等: CREATE OR REPLACE + REVOKE/GRANT, 重复执行干净 no-op。⚠️ 同一块也进了 000_baseline.sql(逐字节相同,
-- tests/test_baseline_parity.py 守着)。
-- ══════════════════════════════════════════════════════════════════════

BEGIN;

SET LOCAL search_path TO autowriter, extensions, public;

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
DECLARE
    _skip_vanished TEXT := '';
BEGIN
    IF to_regclass('truth_vault.notes') IS NULL THEN
        RETURN;
    END IF;
    -- 016: 飞书里已删的篇(TV 的 v_notes_vanished)不返回; 视图不在就照旧全给。
    -- v.project_id = $1 让项目过滤下推进视图的 DISTINCT ON, 不能省。
    IF to_regclass('truth_vault.v_notes_vanished') IS NOT NULL THEN
        _skip_vanished :=
            '   AND NOT EXISTS (SELECT 1 FROM truth_vault.v_notes_vanished v '
            '                    WHERE v.project_id = $1 AND v.note_id = n.note_id) ';
    END IF;
    RETURN QUERY EXECUTE
        'SELECT n.note_id, n.publish_time::timestamptz, n.tier, n.raw_content, '
        '       n.title, n.body, n.source_autowriter_version_id, '
        '       n.created_at::timestamptz '
        '  FROM truth_vault.notes n '
        ' WHERE n.project_id = $1 '
        '   AND ($2 IS NULL OR n.created_at >= $2) '
        '   AND ($3 IS NULL OR n.note_id > $3) '
        || _skip_vanished ||
        ' ORDER BY n.note_id '
        ' LIMIT $4'
        USING _tv_project_id, _since, _after, GREATEST(_limit, 1);
END;
$$;
REVOKE ALL ON FUNCTION autowriter.deskcore_tv_notes(TEXT, TIMESTAMPTZ, TEXT, INT) FROM PUBLIC;
REVOKE ALL ON FUNCTION autowriter.deskcore_tv_notes(TEXT, TIMESTAMPTZ, TEXT, INT) FROM anon;
REVOKE ALL ON FUNCTION autowriter.deskcore_tv_notes(TEXT, TIMESTAMPTZ, TEXT, INT) FROM authenticated;
GRANT EXECUTE ON FUNCTION autowriter.deskcore_tv_notes(TEXT, TIMESTAMPTZ, TEXT, INT) TO service_role;

COMMIT;
