-- ════════════════════════════════════════════════════════════════════
-- migrations/002_calibration_cas.sql
-- ════════════════════════════════════════════════════════════════════
--
-- 调教笔记的 CAS 写入改走 RPC —— witness 压成 md5, 不再进 URL query。
-- 来源: 2026-08-23 全量审计 COR-003 / ROB-006 (docs/audit-2026-08-23-full.md)
--
-- ── 治的是什么 ────────────────────────────────────────────────────────
-- memory.save_calibration_notes 的乐观并发原本写成
--
--     .eq("calibration_notes", expected_before_text)
--
-- PostgREST 把过滤条件放在 **URL query string** 里, 而 calibration_notes 的
-- 软上限是 4000 字(memory._dedup_calibration_lines 的 max_chars)。中文经
-- URL-encode 后一个字变 9 个字符 —— 4000 字 ≈ 36KB 的查询串, 远超 nginx /
-- Kong 对单行请求头的默认上限(常见 8KB)。
--
-- 越线之后, **每一次** CAS 写入都被网关拒掉, 而
-- memory._append_new_observations 的 `except Exception: return None` 把它吞了:
--
--     迭代 / 手动精修 / 整批反思 / merger_taste 四条自动学习路径全部静默停写,
--     用户以为在学、其实笔记早就不动了, 且没有任何告警。
--
-- 笔记越有价值(越长)越先坏 —— 这是本次审计里最隐蔽的一条。
--
-- ── 怎么修 ────────────────────────────────────────────────────────────
-- witness 压成 32 字节的 md5 放进 request body: 请求大小与笔记长度彻底解耦。
-- md5 在这里【只做变更检测】, 不是安全用途 —— 碰撞的后果仅仅是多重试一轮。
--
-- 顺带消掉一个特例: 空 witness 的语义是"我读到的是没有笔记", 而该列 nullable
-- 无默认, 从未写过笔记的项目是 NULL 而不是 ''。旧路径要用
-- `or_(calibration_notes.is.null, calibration_notes.eq.)` 专门绕(R-036);
-- 这里 COALESCE(...,'') 让 NULL 与 '' 都落到 md5('') 上, 天然覆盖两态。
--
-- ── 权限与隔离 ────────────────────────────────────────────────────────
-- SECURITY INVOKER(默认): 以调用者身份执行, projects_owner 那条 RLS 照常生效
-- —— 用户改不了别人项目的笔记。Streamlit 走 authenticated, 所以这个 RPC 与
-- deskcore 那两个(service_role only)不同, 必须授给 authenticated。
-- 仍显式 REVOKE PUBLIC/anon: 匿名角色没有任何理由能改笔记。
--
-- search_path 固定, 同 deskcore_reserve_angles / deskcore_commit_fingerprints
-- 的处理(Supabase advisor 的 function_search_path_mutable)。
--
-- ⚠️ 与 db.py::CREATE_TABLES_SQL 里的同名函数【语义必须一致】。
--    fresh install 走 db.py, 已有库走本文件 —— 只改一边, 两种环境行为就分叉。
--    (两份的**限定写法**本来就不同: CREATE_TABLES_SQL 依赖 SQL Editor 的
--     search_path 建对象、本文件全部写成 autowriter.*, 这是 001 起的既有风格,
--     不是笔误。所以对齐的是 WHERE 判据 / RETURNING / 授权这三处语义, 不是文本。)
--    CI 的 round-9 回归逐条盯着它们。
--
-- 幂等: CREATE OR REPLACE + REVOKE/GRANT, 重复执行是干净 no-op。
-- ════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION autowriter.update_calibration_notes_cas(
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

REVOKE ALL ON FUNCTION autowriter.update_calibration_notes_cas(UUID, TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION autowriter.update_calibration_notes_cas(UUID, TEXT, TEXT) FROM anon;
GRANT EXECUTE ON FUNCTION autowriter.update_calibration_notes_cas(UUID, TEXT, TEXT)
    TO authenticated, service_role;

COMMENT ON FUNCTION autowriter.update_calibration_notes_cas(UUID, TEXT, TEXT) IS
    '调教笔记的乐观并发写入。witness 是 md5(COALESCE(calibration_notes,'''')), '
    '不走 URL query —— 见 migrations/002_calibration_cas.sql 的完整理由。'
    '返回 0 行 = CAS 冲突, 调用方应重读重试。';
