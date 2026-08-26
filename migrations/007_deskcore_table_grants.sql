-- ══════════════════════════════════════════════════════════════════════
-- 007 · 把 deskcore 那四张表授权给 service_role
--
-- 现场发现于 2026-08-26 首次真部署。deskcore 打到线上库时,
-- ``list_projects`` 之外的工具全在 42501 上失败:
--
--     permission denied for table draft_fingerprints
--
-- 为什么 service_role 也会被挡
-- ────────────────────────────
-- ``service_role`` **绕过 RLS**, 但**不绕过表级 GRANT** —— 这是两套独立的
-- 机制, 而"service_role 是超级权限"这个直觉只对了一半。它在 public schema
-- 下之所以看着无所不能, 是因为 Supabase 给 public 配了 default privileges;
-- ``autowriter`` 是本仓自己建的 schema, **没有**这份默认授权。
--
-- 于是 ``migrations/001_deskcore.sql`` 建的四张表从出生起就一行 GRANT 都没有:
-- 001 只给两个函数发了 EXECUTE(:303 / :414), 表本身漏了。函数能跑是因为它们是
-- SECURITY DEFINER, owner 权限执行; 而 deskcore 直接 ``client.table(...)`` 的
-- 那些路径(指纹读写 / 台账 / 个人笔记 / 精修 diff)全走 PostgREST 的调用者权限,
-- 全挂。
--
-- 为什么只给 service_role
-- ──────────────────────
-- 这四张表**只有 deskcore 一个调用方**, 而 deskcore 用 service key 连库并在
-- 应用层自己做归属隔离(见 ``deskcore/core.py::assert_project_access``)。
-- ``authenticated`` 一行都不需要 —— Streamlit 界面已经停用, 给了只是多一个面。
-- ``anon`` 更不用说。
--
-- 幂等: GRANT 重复执行是干净 no-op。
-- 生产库已于 2026-08-26 执行(记为 aw_007_deskcore_table_grants)。
--
-- ⚠️ 注意这条**不包含** ``calibration_note_audit``: 那张表建于 Supabase 那次
-- 授权口径变更之前, 现存库里带着历史 GRANT 是好的; 它漏的是**新库**那一路,
-- 修在 ``000_baseline.sql`` 的 Data API grants 块里。
-- ══════════════════════════════════════════════════════════════════════

BEGIN;

GRANT SELECT, INSERT, UPDATE, DELETE ON
    autowriter.angle_ledger,
    autowriter.draft_fingerprints,
    autowriter.user_calibration_notes,
    autowriter.style_edits
    TO service_role;

COMMIT;

-- ── 校验 (人工跑, 不在事务内) ────────────────────────────────────────
-- 四张表都应回 t:
--   SELECT c.relname,
--          has_table_privilege('service_role', c.oid, 'SELECT') AS sel,
--          has_table_privilege('service_role', c.oid, 'INSERT') AS ins
--     FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
--    WHERE n.nspname = 'autowriter' AND c.relkind = 'r'
--      AND c.relname IN ('angle_ledger','draft_fingerprints',
--                        'user_calibration_notes','style_edits');
--
-- 更值钱的是这条 —— 问"还有谁漏了", 而不是"我列的这四张对不对":
--   SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
--    WHERE n.nspname='autowriter' AND c.relkind='r'
--      AND NOT has_table_privilege('service_role', c.oid, 'SELECT');
--   → 应返回 0 行。CI 里由 tests/sql_parity_check.py 常驻断言同一件事。
