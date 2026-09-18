-- ══════════════════════════════════════════════════════════════════════
-- 010 · 009 建的三张表开 RLS(2026-09-18)
--
-- 病灶
-- ────
-- 009 建了 tv_project_map / tv_note_links / ingest_locks, 发了 service_role
-- 的表级 GRANT, 但没有 ENABLE ROW LEVEL SECURITY —— 本 schema 其他每张表都开了
-- (000_baseline / 001 的惯例), 这三张是漏网的。Supabase 的 advisor 对着生产库
-- 报了出来(TV 2026-09-18 核对时转来的)。
--
-- 不是漏洞: anon / authenticated 对这三张表没有任何表级 GRANT, 拿 anon key 一行
-- 都读不到; 而 deskcore 与 CLI 持 service_role, service_role 绕过 RLS。开了对
-- 现有代码零影响, 只是把"没 GRANT"这一道栏杆之外再加 RLS 这一道, 与本 schema
-- 其他表同一口径 —— 以后谁给 anon 发了 GRANT 也不会一下子把整张表露出去。
--
-- versions_num_backup_20260826 不是本目录建的(003 落库那天手工留的备份表, 只在
-- 生产库里有), 同样没开 RLS。这里顺手开, 用 to_regclass 守着: 本地 harness 与
-- 新库没有这张表, 直接跳过。
--
-- 幂等: ENABLE ROW LEVEL SECURITY 重复执行是 no-op。doctor 不探它(探不到 RLS
-- 开没开, 且漏跑不影响任何功能); tests/sql_parity_check.py 守"autowriter 下每张
-- 表都开了 RLS"这条不变量, 以后再建表忘了开会在本地红。
-- ══════════════════════════════════════════════════════════════════════

BEGIN;

ALTER TABLE autowriter.tv_project_map ENABLE ROW LEVEL SECURITY;
ALTER TABLE autowriter.tv_note_links  ENABLE ROW LEVEL SECURITY;
ALTER TABLE autowriter.ingest_locks   ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF to_regclass('autowriter.versions_num_backup_20260826') IS NOT NULL THEN
        EXECUTE 'ALTER TABLE autowriter.versions_num_backup_20260826 ENABLE ROW LEVEL SECURITY';
    END IF;
END;
$$;

COMMIT;
