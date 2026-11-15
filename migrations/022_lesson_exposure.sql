-- ══════════════════════════════════════════════════════════════════════
-- 022 · 查重底稿上记这一场看到了哪几张经验卡、哪几条规律(2026-11-15, 十三项第 3 项)
--
-- 为什么
-- ──────
-- 借来的经验卡和已验证规律, 写作台以前只记「带出去几张 / 几条」(batch_metrics.meta.flywheel_lessons、
-- batches.params.flywheel_patterns_shown), 不记是哪几张。于是「看到过这张卡的那几批稿子, 发出去以后怎么样」答不了,
-- 而且事后补不回来: 哪一场拿到了哪几张, 只有借卡那一刻知道。
-- 现在借卡时服务端在进程里记下卡的 source_note_id 和规律的标识, 存档时写进 batches.params.lesson_exposure
-- (params 是 JSONB, 不用迁移)。只查了重、没存档就发出去的稿子(W-38 的底稿)要靠这一列带着: tv-sync 把底稿转成
-- 版本时, 把它带进新建的 batch 的 params。
--
-- 这次做什么
-- ──────────
-- 只给 autowriter.draft_snapshots 加一个可空的列, 别的什么都不动(不建表、不建函数、不发 GRANT、不碰一行数据):
--   · lesson_exposure  JSONB: {"cards": [source_note_id…], "patterns": [规律标识…], "borrows": 几次借卡,
--                              "first_at", "last_at"}; NULL = 那次查重时服务端不知道(没借过卡 / 进程重启过 / 022 之前的行)
-- 只有 id、条数和时刻, 没有卡的文字。
-- 它是【曝光】记录, 不是「这篇稿子用了这张卡」的证明; 团队共用一个身份、12 小时里同一个项目开了几场时分不出是哪一场的
-- (docs/deskcore.md §3.33)。
--
-- 新列不用另发 GRANT: 018 给 service_role 的是表级权限, 覆盖新列。
--
-- 失效方向: 没跑本迁移时, check_drafts 记底稿那一步发现没有这一列就去掉它再写一次(服务日志里警告一次), 写进去的行和
-- 以前一模一样; tv-sync 读底稿退回原来那几列。查重的判定、转正、补录都不受影响, batches.params 照样记。所以先于或后于
-- 部署代码都行; 跑了之后最多十分钟自动用上, 不用重新部署。
--
-- 幂等: ADD COLUMN IF NOT EXISTS + COMMENT, 重复执行干净 no-op。⚠️ 同样的一行也进了 000_baseline.sql。
-- 回滚: 用不着。一个空列留着没有任何影响; 真要拿掉就是 ALTER TABLE … DROP COLUMN lesson_exposure。
-- ══════════════════════════════════════════════════════════════════════

BEGIN;

SET LOCAL search_path TO autowriter, extensions, public;

ALTER TABLE autowriter.draft_snapshots
    ADD COLUMN IF NOT EXISTS lesson_exposure JSONB;
COMMENT ON COLUMN autowriter.draft_snapshots.lesson_exposure IS
    '这次查重的那一场(项目, 身份)近 12 小时拿到过的经验卡和已验证规律: {"cards": [source_note_id], "patterns": [规律标识], "borrows", "first_at", "last_at"}。NULL = 不知道。是曝光记录, 不是用了这张卡的证明(deskcore/core.py lesson_exposure)';

COMMIT;
