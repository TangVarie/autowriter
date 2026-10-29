-- ══════════════════════════════════════════════════════════════════════
-- 018 · 查重时记下的底稿, tv-sync 拿飞书的帖来对(2026-10-29, W-38)
--
-- 为什么
-- ──────
-- 写手没有「存档」这一步(D-120): 稿子写完贴进飞书, 写作台每晚按内容把飞书的帖对回写作台的稿。可写作台的稿只在
-- 模型调了 score_drafts 时才进库(batch / item / version + 指纹 + 销牌); 查重 check_drafts 是每篇都必过的硬闸,
-- 却什么都不留。模型没调 score_drafts 的那几轮(10-28 途鸽一轮 10 张牌, 一张没销), 发出去的帖只能被当成外面写的
-- 补录成副本(ingested, 永远不再重对), 牌销不掉, 改稿差异也没有。
--
-- 这次做什么
-- ──────────
-- autowriter.draft_snapshots: check_drafts 每查一篇记一行(标题、正文、带来的 angle_key、查重结论、当时的飞轮臂)。
-- **不进 draft_fingerprints**: 底稿不挡后面的稿子查重。tv-sync 对不上写作台版本的帖, 先拿这些底稿再对一次
-- (deskcore/tvlink.match_snapshot), 对上了就把底稿转成真的版本(建 item / version、写指纹、销牌、算改稿差异),
-- 和存过档的一样; promoted_at / version_id / note_id 记转正的结果。对不上的照旧补录。
-- 一次查重的几篇共用一个 check_id(转正时一轮一个 batch)。同一篇查了几次就有几行, 按 (project_id, text_fp) 在
-- Python 里归成一篇。
-- 存的是稿子全文: 只授 service_role, 开 RLS、不建 policy(anon / authenticated 一行都读不到)。
--
-- 可降级, 但**先跑本迁移再部署代码**: 代码先上线的话 check_drafts 照常判(记底稿那一步失败只记 WARN, 不碰判定)、
-- tv-sync 跳过底稿那一遍、照旧补录 —— 可同一次部署改了给模型的话(没存上的稿子发出去以后写作台会自己对回来), 那段时间
-- 只查了重的稿子发出去会被补录成副本(终态, 再也认不回来)。留 60 天(tv-sync --all 每晚删更早的, SNAPSHOT_RETAIN_DAYS)。
--
-- 幂等: 表 / 索引 IF NOT EXISTS; GRANT / RLS 重复执行是 no-op。⚠️ 同样的表也进了 000_baseline.sql。
-- ══════════════════════════════════════════════════════════════════════

BEGIN;

CREATE TABLE IF NOT EXISTS autowriter.draft_snapshots (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id           UUID NOT NULL REFERENCES autowriter.projects(id) ON DELETE CASCADE,
    user_id              UUID,
    check_id             UUID NOT NULL,
    title                TEXT NOT NULL DEFAULT '',
    body                 TEXT NOT NULL DEFAULT '',
    angle_key            TEXT,
    text_fp              TEXT NOT NULL,
    status               TEXT,
    flywheel_arm         TEXT,
    flywheel_arm_source  TEXT,
    checked_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    version_id           UUID,
    promoted_at          TIMESTAMPTZ,
    note_id              TEXT
);
-- tv-sync 每晚读还没转正的(按项目、近 45 天); 简报 / 发牌提醒按 (项目, 写手) 取查过的 angle_key
CREATE INDEX IF NOT EXISTS draft_snapshots_pending_idx
    ON autowriter.draft_snapshots (project_id, checked_at DESC) WHERE promoted_at IS NULL;
CREATE INDEX IF NOT EXISTS draft_snapshots_user_key_idx
    ON autowriter.draft_snapshots (project_id, user_id, checked_at DESC) WHERE angle_key IS NOT NULL;
-- 建出来 ≠ 能访问(migrations/README): service_role 不绕表级 GRANT。只给 service_role —— 读写都只有 deskcore。
GRANT SELECT, INSERT, UPDATE, DELETE ON autowriter.draft_snapshots TO service_role;
ALTER TABLE autowriter.draft_snapshots ENABLE ROW LEVEL SECURITY;

COMMIT;
