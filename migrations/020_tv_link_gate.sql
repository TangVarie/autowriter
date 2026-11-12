-- ══════════════════════════════════════════════════════════════════════
-- 020 · 对照上记每篇已发帖的来源和回流闸结论(2026-11-12, 回流闸 v1)
--
-- 为什么
-- ──────
-- 发出去的帖大多不是在写作台里写的(11-12 只读实测: 近 45 天对照表里的 TV 项目发了 912 篇, 775 篇是别处写的)。
-- tv-sync 当晚就把它们抄进指纹库(查重、「最近发过」的标题用), 这没问题; 可同一批帖也会被当成「可以学」的样子
-- 摆给模型(简报的 working 标题、发牌的起量帖参照、借来的教训卡), 而这三处的筛法各不相同:
-- 承诺功效的、后来在飞书里删掉的、还没答完特征题的, 哪一处都没筛。
-- 所以每篇已发的帖在对照上记一个结论, 三处读同一个。发出去的帖改不了, 闸只分拣、不拦:
-- 查重和「最近发过」的标题照旧全收; 配比、起量计数照旧按 priors.is_clean; 只有「可以学」那一路看这个结论。
--
-- 这次做什么
-- ──────────
-- 只给 autowriter.tv_note_links 加四个可空的列, 别的什么都不动(不建表、不建函数、不发 GRANT、不碰一行数据):
--   · origin        帖是哪来的(只是标签, 不参与结论): desk / desk_edited / copied_back / elsewhere / unclear
--   · gate          结论: pass / fail / unsure / hold / withdrawn; NULL = 还没判过
--   · gate_reasons  结论的原因, 只有名字和数字, 没有帖的文字
--   · gate_at       最近一次判它的时刻(结论没变也刷新)
-- 取值和判法都在 deskcore/postgate.py 一处; 这里不加 CHECK(以后多一档不用再跑迁移)。
-- 谁写: tv-sync 每晚最后一步(core._tv_gate), 只 UPDATE 这四列, 不碰对照本身的列。
--
-- 新列不用另发 GRANT: 007/009 给 service_role 的是表级权限, 覆盖新列(同 017)。
--
-- 失效方向: 没跑本迁移时, tv-sync 的那一步读不到这四列就整步跳过(对照、补录照常), 读结论的几处当作「没判过」、
-- 行为和以前完全一样。所以先于或后于部署代码都行; 跑了之后当晚的 tv-sync 起开始记。
--
-- 幂等: ADD COLUMN IF NOT EXISTS + COMMENT, 重复执行干净 no-op。⚠️ 同样的几行也进了 000_baseline.sql。
-- 回滚: 用不着。四个空列留着没有任何影响; 真要拿掉就是 ALTER TABLE … DROP COLUMN 这四列。
-- ══════════════════════════════════════════════════════════════════════

BEGIN;

SET LOCAL search_path TO autowriter, extensions, public;

ALTER TABLE autowriter.tv_note_links
    ADD COLUMN IF NOT EXISTS origin       TEXT,
    ADD COLUMN IF NOT EXISTS gate         TEXT,
    ADD COLUMN IF NOT EXISTS gate_reasons JSONB,
    ADD COLUMN IF NOT EXISTS gate_at      TIMESTAMPTZ;
COMMENT ON COLUMN autowriter.tv_note_links.origin IS
    '回流闸 v1: 帖的来源(deskcore/postgate.py)。desk = 写作台的稿发出去的; desk_edited = 写作台的稿改得多; copied_back = 发了之后才存进写作台的那版; elsewhere = 别处写的(和发布前写作台里的文字重合不到 0.15); unclear = 判不出。只是标签, 不参与 gate。NULL = 还没判过';
COMMENT ON COLUMN autowriter.tv_note_links.gate IS
    '回流闸 v1: 这篇已发的帖能不能拿来学(deskcore/postgate.py)。pass / fail / unsure / hold / withdrawn。NULL = 还没判过, 读的一方照没有闸时的口径';
COMMENT ON COLUMN autowriter.tv_note_links.gate_reasons IS
    '回流闸 v1: {"reasons": [原因名], "hold": [暂缓的细项名], "overlap": 和发布前写作台里的文字的重合(只在对不上的行)}。原因名: withdrawn / hold / not_clean / risk / efficacy / unsure。只有名字和数字, 没有帖的文字';
COMMENT ON COLUMN autowriter.tv_note_links.gate_at IS
    '回流闸 v1: 最近一次判这一行的时刻(tv-sync 每晚; 结论没变也刷新)';

COMMIT;
