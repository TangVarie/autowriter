-- ════════════════════════════════════════════════════════════════════
-- migrations/006_item_decision_provenance.sql
-- ════════════════════════════════════════════════════════════════════
--
-- 给 items 补"这个状态是**谁、什么时候、凭什么**定下来的"。
-- 来源: 2026-08-24 跨库审计 COR-004 / COR-007。
--
-- ── 治的是什么 ────────────────────────────────────────────────────────
--
-- `items.status` 一个字段同时承担两件完全不同的事:
--
--   · 人点了「通过」/「打回」            —— 这是**人工审稿意见**
--   · 硬规则违规、查重重生耗尽自动标记    —— 这是**机器检测结果**
--
-- 两者写进去之后**长得一模一样**。而 Truth Vault 的
-- `sync_autowriter_decisions_to_prepublish.py` 把它们**全部**当人工反馈
-- 灌进 `prepublish_evaluations`(evaluator_type='human'), 用来校准评估模型。
-- 也就是说: 机器自己的判定被当成人的判断喂回给模型去学 —— 训练标签污染,
-- 而且没有任何地方会报错。
--
-- 同一条 sync 还在推断另外两件事, 也都推错了:
--   · `evaluator_id` 取的是 **item 的 owner**, 不是真正点按钮的人;
--   · `created_at` 取的是**同步那一刻**, 不是决策发生的时刻。
-- 多人协作 + 延迟同步的场景下, 人员归因和时间序列一起失真。
--
-- ── 这次做什么, 不做什么 ──────────────────────────────────────────────
--
-- **做**: 把三个真值落到列上, 让消费方**不必再推断**。
--
-- **不做**: 审计原文建议的 append-only `review_events` + status 投影
-- (事件溯源)。那会改掉 AW 里每一处读 status 的地方, 换来的"决策历史"目前
-- 没有任何消费方在要。所以这次只补当前决策的出处 —— 它足以止住上面那个
-- 污染, 而且 TV 侧只要多 select 三列就能用上。
--
-- ⚠️ **因此仍然只保留"最新一次"决策**: `needs_revision → approved` 这段
--    历史依然是丢的(跨库审计 COR-005)。那条要修得两边一起动(TV 的
--    `idx_tv_evals_aw_item_evaluator_uniq` 现在也只容一条 human 行),
--    不是这个迁移能单独解决的。别把这里当成 COR-005 也修好了。
--
-- ── 存量行 ────────────────────────────────────────────────────────────
-- 三列全部留 NULL, **不做回填**。回填只能靠猜(现有数据里没有任何信息能
-- 区分人工与自动), 猜出来的 provenance 比 NULL 更坏 —— NULL 至少诚实地
-- 说"不知道", 消费方可以据此跳过。
--
-- 幂等: 全部 ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS。
-- ⚠️ 同样的列也加进了 000_baseline.sql(migrations/README.md 的规矩:
--    加列必须两边都改, 否则新环境缺列 / 老库升不上去)。
-- ════════════════════════════════════════════════════════════════════

-- 谁定的这个状态。取值是**闭集**, 加新值要同时更新 db.DecisionSource。
--   human           人点了通过/打回
--   auto_hard_rule  硬规则违规, 自动标 needs_revision
--   auto_dedup      查重重生耗尽, 自动标 needs_revision
--   system          既非审稿也非检测(例如迭代出新版后重置回 pending)
ALTER TABLE items ADD COLUMN IF NOT EXISTS decision_source TEXT
    CHECK (decision_source IN
           ('human', 'auto_hard_rule', 'auto_dedup', 'system'));

-- 真正做出这个决定的人。auto_* / system 恒为 NULL —— 那时候没有"人"。
-- ⚠️ 不要拿它去 fallback 到 items.user_id: "谁拥有"和"谁审的"是两件事,
--    混起来正是 COR-007 要治的那个失真。
ALTER TABLE items ADD COLUMN IF NOT EXISTS reviewer_id UUID;

-- 决策发生的时刻(不是同步时刻、也不是 updated_at —— 后者任何一次无关更新
-- 都会刷新)。
ALTER TABLE items ADD COLUMN IF NOT EXISTS decided_at TIMESTAMPTZ;

-- TV 的决策同步按"人工的、且还没归档过的"来捞。部分索引只覆盖 human,
-- 因为自动那些**本来就不该**进人工评估表。
CREATE INDEX IF NOT EXISTS items_human_decision_idx
    ON items (decided_at)
    WHERE decision_source = 'human';

COMMENT ON COLUMN items.decision_source IS
    '这个 status 是谁定的: human / auto_hard_rule / auto_dedup / system。'
    'NULL = 本列上线前的存量行, 无法判断(刻意不回填, 猜出来的比 NULL 更坏)。'
    '下游要把机器判定当人工反馈用之前, 必须先按这一列过滤。见审计 COR-004。';
COMMENT ON COLUMN items.reviewer_id IS
    '真正做出决定的人; 自动判定恒 NULL。**不是** items.user_id(那是归属)。';
COMMENT ON COLUMN items.decided_at IS
    '决策发生的时刻; 不是同步时刻, 也不是 updated_at。见审计 COR-007。';
