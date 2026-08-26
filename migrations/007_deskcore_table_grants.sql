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
-- ══════════════════════════════════════════════════════════════════════

BEGIN;

GRANT SELECT, INSERT, UPDATE, DELETE ON
    autowriter.angle_ledger,
    autowriter.draft_fingerprints,
    autowriter.user_calibration_notes,
    autowriter.style_edits
    TO service_role;

-- ── 顺带修 calibration_note_audit ────────────────────────────────────
-- 同一次排查翻出来的第二例: 那张表建在基线 Data API grants 块【之前】, 于是
-- 从来不在授权名单里。
--
-- ⚠️ 一开始我判断"现存库无需修, 只补基线就行" —— **那是从一个库推出来的结论,
-- 错了**。当前生产库看不出问题, 是因为它建于 Supabase 授权口径变更之前、带着
-- 历史 GRANT; 而任何在那之后用**旧版基线**开出来的库(branch 库 / 灾备重建 /
-- 别人的 dev 项目)都缺这份授权, 并且它们升级走的是增量而不是重跑基线 ——
-- 只修基线等于没修到它们。(codex review · aw#65)
--
-- 症状同样是静默的: db.log_calibration_audit 的写入是 ``except Exception: pass``,
-- 于是调教笔记的审计流水一条都不留, 而没有任何东西报错。
--
-- authenticated 只给 SELECT + INSERT: 审计表是 append-only。它存的就是"这条
-- 观察是什么时候、被什么改成什么样的", 能被改写或删除的流水回答不了这个问题。
-- 与旁边的 user_logins 同一个待遇。
GRANT SELECT, INSERT, UPDATE, DELETE ON autowriter.calibration_note_audit
    TO service_role;
GRANT SELECT, INSERT                  ON autowriter.calibration_note_audit
    TO authenticated;

-- ⚠️ 这两句会**收回**权限, 是本文件里唯一不是纯增量的动作 —— 单独说清楚。
--
-- 现存生产库上 authenticated 对这张表是全套 CRUD(历史 GRANT 带来的), 而按上面
-- 那条口径它不该有 UPDATE / DELETE。核对过代码再收的: db.py 对这张表只有
-- insert / select / count, **没有任何 update 或 delete**; 删项目时的清理走
-- projects 的 ON DELETE CASCADE, 而外键的级联动作以**表 owner** 权限执行,
-- 不需要调用者持有 DELETE。唯一的 authenticated 客户端(Streamlit 界面)已经停用。
-- 所以这两句在任何现有路径上都是 no-op, 只是把面收掉。
REVOKE UPDATE, DELETE ON autowriter.calibration_note_audit FROM authenticated;

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
