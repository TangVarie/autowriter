-- ══════════════════════════════════════════════════════════════════════
-- 017 · 对照记下 TV 正文的指纹; 写作台可以撤回自己写进 TV 的旧血缘(2026-10-27, W-34)
--
-- 为什么
-- ──────
-- tv-sync 对已经有版本的对照每晚记 already_linked、不再看 TV 的正文。运营在飞书里把一篇整篇换成另一篇
-- (同一个 note_id), 对照、写回 TV 的 source_autowriter_* 和改动差异就一直挂在原来那版上:
--   · D-129(10-26): BJS_phase1 14 条对上儿童稿的, 飞书正文后来整篇换成成人帖(和所连版本重叠 ≤ 0.06),
--     其中 10 条的差异进了儿童藻油项目的改稿摘要;
--   · 10-27 生产库还有 2 条: SPX_phase1 一条(10-16 对上跑步稿, 当天被换成网球帖)、TUGE_phase1 一条。
-- --rematch 也救不了: 519 条有版本的对照全被 --write-tv 写回了 TV, 重对时 TV 的 lineage 分支先走, 又对回旧版。
--
-- 这次做什么
-- ──────────
-- 1. tv_note_links.tv_text_fp: 最近一次核对时 TV 正文的指纹(tvlink.text_fp)。tv-sync 每晚只核指纹变了的。
-- 2. deskcore_tv_clear_lineage(_links): 把 TV 的 source_autowriter_version_id / _item_id 清成 NULL, **只清**
--    两列都还等于传进来那一对的行(compare-and-swap) —— 也就是写作台自己当初写回去的那一对。返回实际清了的
--    note_id(JSONB 数组): 调用方只挪清掉的那几条, 没清掉的(TV 里已经是别的值)不挪、报出来给人看。
--    清完之后同一晚 009 的回填(只填 NULL)把新版本写回去; 009 的"只填 NULL"不变。
--
-- 为什么不用 RETURN QUERY EXECUTE 'UPDATE … RETURNING': PG 14 之前它报 "cannot open UPDATE query as cursor";
-- 这里用数据修改 CTE + EXECUTE … INTO, 各版本都行。
--
-- ⚠️ 用函数属主跑(Supabase 上 SQL Editor / apply_migration 的默认身份 postgres)。函数以属主身份写
-- truth_vault.notes(RLS 开着、无 policy), 生产库的 postgres 有 BYPASSRLS —— 与 009 的回填同一个前提。
-- 新列不用另发 GRANT: 007/009 给 service_role 的是表级权限, 覆盖新列。
--
-- 失效方向: 库里没有 truth_vault(本地 harness / fresh install)时函数返回 '[]', 什么都不写。
-- 代码先于本迁移上线时, tv-sync 读 tv_note_links 选不到 tv_text_fp, 每个 TV 项目都失败、退出码 1、一行不写 ——
-- 吵, 但不写错数据。所以先跑本迁移, 再合并代码。
--
-- 幂等: ADD COLUMN IF NOT EXISTS + CREATE OR REPLACE + REVOKE/GRANT, 重复执行干净 no-op。⚠️ 同样的改动也进了
-- 000_baseline.sql(函数块逐字节相同, tests/test_baseline_parity.py 守着)。
-- ══════════════════════════════════════════════════════════════════════

BEGIN;

SET LOCAL search_path TO autowriter, extensions, public;

ALTER TABLE autowriter.tv_note_links
    ADD COLUMN IF NOT EXISTS tv_text_fp TEXT;
COMMENT ON COLUMN autowriter.tv_note_links.tv_text_fp IS
    'W-34: 最近一次核对时 TV 正文的指纹(tvlink.text_fp: 标题 + 剥话题正文, fp.normalize 后 sha1 前 16 位)。NULL = 017 之前的对照, 或还没核过';

-- ── 撤回写作台自己写进 TV 的血缘: 两列都还等于传进来的那一对才清, 返回清了的 note_id ──
CREATE OR REPLACE FUNCTION autowriter.deskcore_tv_clear_lineage(_links JSONB)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    _ids JSONB;
BEGIN
    IF to_regclass('truth_vault.notes') IS NULL THEN
        RETURN '[]'::jsonb;
    END IF;
    EXECUTE
        'WITH u AS ( '
        '  UPDATE truth_vault.notes t '
        '     SET source_autowriter_version_id = NULL, '
        '         source_autowriter_item_id    = NULL '
        '    FROM jsonb_to_recordset($1) AS l(note_id TEXT, version_id UUID, item_id UUID) '
        '   WHERE t.note_id = l.note_id '
        '     AND l.version_id IS NOT NULL '
        '     AND t.source_autowriter_version_id = l.version_id '
        '     AND t.source_autowriter_item_id IS NOT DISTINCT FROM l.item_id '
        '  RETURNING t.note_id) '
        'SELECT COALESCE(jsonb_agg(note_id ORDER BY note_id), ''[]''::jsonb) FROM u'
        INTO _ids USING _links;
    RETURN _ids;
END;
$$;
REVOKE ALL ON FUNCTION autowriter.deskcore_tv_clear_lineage(JSONB) FROM PUBLIC;
REVOKE ALL ON FUNCTION autowriter.deskcore_tv_clear_lineage(JSONB) FROM anon;
REVOKE ALL ON FUNCTION autowriter.deskcore_tv_clear_lineage(JSONB) FROM authenticated;
GRANT EXECUTE ON FUNCTION autowriter.deskcore_tv_clear_lineage(JSONB) TO service_role;

COMMIT;
