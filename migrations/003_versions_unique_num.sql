-- ════════════════════════════════════════════════════════════════════
-- migrations/003_versions_unique_num.sql
-- ════════════════════════════════════════════════════════════════════
--
-- versions 加 UNIQUE(item_id, version_num)。
-- 来源: 2026-08-23 全量审计 COR-004 (docs/audit-2026-08-23-full.md)
--
-- ── 治的是什么 ────────────────────────────────────────────────────────
-- db.create_version 是「读 max(version_num) → +1 → INSERT」三步, 而这两列上
-- 一直没有唯一约束。同一个 item 并发迭代(两个标签页; 或"AI 迭代"与"手动精修"
-- 同时提交)会各自读到同一个 max, 双双写入同一个号。
--
-- ⚠️ 但重复号【绝大多数不是竞态来的】(codex review 2026-08-24 指出, 属实):
-- db.bulk_create_initial_versions 原来给同一 item 的**每个引擎**都写
-- version_num=1 —— 多引擎批次是天天在跑的常规路径, 一个 item 有几个引擎就有
-- 几条并列的 1。所以:
--   · 那个函数已改成同 item 内按 1..N 编号(否则装上本索引之后, 每一个多引擎
--     批次的首版 insert 都会撞 23505, 而失败路径会删掉已建 items ——
--     整批生成完什么也不存);
--   · 下面 ① 的重编号不是"清理罕见脏数据", 而是**几乎每个多引擎 item 都要走
--     一遍**。它按 (version_num, created_at, id) 稳定重排, 与新代码同一套口径。
--
-- 后果不是报错, 是**排序不确定**:
--   db.list_approved_versions_for_sync 的 _pick_version 和
--   deskcore/store.labeled_examples / legacy_versions 都按
--   max(version_num, created_at, id) 挑"代表版本"。并列时挑中哪一条要看
--   tie-break —— 于是 best 指针可能指向被覆盖的那一版, 导出的、送模型做
--   避重参照的, 都可能是旧文。而且从头到尾没有任何报错。
--
-- ── 为什么要先重编号 ──────────────────────────────────────────────────
-- 库里若已经有并发写入留下的重复对子, 直接 CREATE UNIQUE INDEX 会失败,
-- 整条迁移中断。所以先做一次确定性的重编号, 再建索引。
--
-- 重编号安全性: version_num 只是"第几版"的展示/排序值, **没有任何外键指向
-- 它**。items.best_version_id 指的是 versions.id(不是 version_num),
-- session_messages 关联的是 item_id —— 改号不会打断任何引用。
--
-- 重编号口径: 同一 item 内按 (version_num, created_at, id) 稳定排序后
-- 从 1 起连续重排。这样"谁在前"与现有代码的 tie-break 口径一致,
-- 不会把用户认知里的版本顺序打乱。
--
-- ⚠️ 与 db.py::CREATE_TABLES_SQL 的分工: 那边是 fresh install 的源头, 已经
--    同步加了同一条唯一索引(新库天然没有重复, 不需要重编号那一段)。
--    migrations/README.md: 加表/加列必须两边都改。
--
-- 幂等: 重编号只动"确实重复"的行(没有重复时是 no-op);
--       CREATE UNIQUE INDEX IF NOT EXISTS 重复执行是干净 no-op。
-- ════════════════════════════════════════════════════════════════════

-- ① 把重复的 (item_id, version_num) 对子重编号
--    只重排【存在重复的那些 item】, 其余一行不动。
WITH dup_items AS (
    SELECT item_id
      FROM autowriter.versions
     GROUP BY item_id, version_num
    HAVING count(*) > 1
),
renumbered AS (
    SELECT v.id,
           row_number() OVER (
               PARTITION BY v.item_id
               ORDER BY v.version_num, v.created_at, v.id
           ) AS new_num
      FROM autowriter.versions v
     WHERE v.item_id IN (SELECT item_id FROM dup_items)
)
UPDATE autowriter.versions v
   SET version_num = r.new_num
  FROM renumbered r
 WHERE v.id = r.id
   AND v.version_num IS DISTINCT FROM r.new_num;

-- ② 建唯一索引
CREATE UNIQUE INDEX IF NOT EXISTS versions_item_version_uniq
    ON autowriter.versions (item_id, version_num);

COMMENT ON INDEX autowriter.versions_item_version_uniq IS
    '并发迭代同一个 item 时, 让数据库来保证版本号不重复(必有一方撞 23505, '
    'db.create_version 捕获后重读 max 重试)。没有它, 两条同号版本都会写进去, '
    '而挑"代表版本"的 tie-break 会静默选错。见 migrations/003。';
