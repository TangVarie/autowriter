-- ══════════════════════════════════════════════════════════════════════
-- 012 · 经代理转述的人审要能和「人亲手点的」分开(审计 A-01, 2026-10-09)
--
-- 病灶
-- ────
-- 生产库 autowriter.items 里 decision_source='human' 的 32 行**全部**是 deskcore 的
-- review_drafts 工具写的, 而且全部落在模型自己的工具链里:
--   check_drafts → commit_drafts → review_drafts → export_drafts, 距 commit 10~27 秒,
-- 中间没有一个人读得完稿子的窗口。deskcore 不记成功的工具调用, 于是库里**分不出**
-- 「模型自己点了通过」和「老板在客户端说了"全过"、模型照实记」—— 两者写进去一模一样,
-- 都是 human + 调用者的 reviewer_id。协议和工具 docstring 都写着「用户没表态不许调」,
-- 但服务端没有任何一道闸, 而 TV 的决策同步只认 decision_source='human' 当人工反馈。
--
-- 这次做什么
-- ──────────
-- · decision_source 多一个值 **human_via_agent**: 结论来自用户、但经模型(代理)之手
--   写进来。从此 MCP 工具只写这个值, 'human' 留给 Streamlit 里真的点了按钮的那条路
--   (app.py)。下游要不要把它当人工反馈, 由消费方自己决定 —— 这里只负责**说实话**。
-- · items.decision_note  TEXT   ≤ 200 字: 用户给结论时的**原话**(工具的 user_words
--   参数), 空着不让写(应用层校验), 给人复核「这是不是真有人说过」用。
-- · items.decided_within_s INTEGER: 决策时刻距这批稿子 created_at 的秒数。10 秒内
--   「审完」20 篇, 一眼就能看出不对; 没有 batch 行时留 NULL, 不硬算。
--
-- 不做什么
-- ────────
-- · **不回填**存量那 32 行。它们到底是哪种, 数据里没有证据; 猜出来的比 NULL 更坏
--   (006 的存量行也是这么处理的)。要改口径, 由 truth-vault 那边的 DECISIONS 定。
-- · 不动 items_human_decision_idx(TV 的决策同步按它捞 'human')。另建一条只盖
--   human_via_agent 的部分索引, 消费方要分开捞、合着捞都用得上。
--
-- 幂等: ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS; CHECK 用 DO 块先看
-- 现有定义里有没有新值, 有就跳过。
-- ⚠️ 同样的改动也进了 000_baseline.sql(migrations/README.md 的规矩: 加列必须两边都改)。
-- ⚠️ 与 006 一样, 取值是闭集, 加值要同时更新 db.DecisionSource。
-- ══════════════════════════════════════════════════════════════════════

BEGIN;

-- ── ① CHECK 多一个值 ──────────────────────────────────────────────────
-- 006 用的是列内联 CHECK, 名字由 PG 自动起(items_decision_source_check); 这里不押
-- 名字, 按「items 上、引用 decision_source 的 CHECK」找, 找到就换成带名字的新定义。
DO $$
DECLARE
    _con  record;
    _done boolean := false;
BEGIN
    FOR _con IN
        SELECT conname, pg_get_constraintdef(oid) AS def
          FROM pg_constraint
         WHERE conrelid = 'autowriter.items'::regclass
           AND contype  = 'c'
           AND pg_get_constraintdef(oid) LIKE '%decision_source%'
    LOOP
        IF _con.def LIKE '%human_via_agent%' THEN
            _done := true;                                   -- 已经跑过, 幂等退出
        ELSE
            EXECUTE format('ALTER TABLE autowriter.items DROP CONSTRAINT %I', _con.conname);
        END IF;
    END LOOP;
    IF NOT _done THEN
        ALTER TABLE autowriter.items
            ADD CONSTRAINT items_decision_source_check
            CHECK (decision_source IN
                   ('human', 'human_via_agent', 'auto_hard_rule', 'auto_dedup', 'system'));
    END IF;
END;
$$;

-- ── ② 两列: 原话 + 距批次创建的秒数 ─────────────────────────────────
ALTER TABLE autowriter.items ADD COLUMN IF NOT EXISTS decision_note TEXT
    CONSTRAINT items_decision_note_len_check CHECK (char_length(decision_note) <= 200);
ALTER TABLE autowriter.items ADD COLUMN IF NOT EXISTS decided_within_s INTEGER;

-- ── ③ 只盖 human_via_agent 的部分索引; 006 那条 'human' 的原样不动 ──
CREATE INDEX IF NOT EXISTS items_human_via_agent_decision_idx
    ON autowriter.items (decided_at)
    WHERE decision_source = 'human_via_agent';

COMMENT ON COLUMN autowriter.items.decision_source IS
    '这个 status 是谁定的: human(Streamlit 里真的点了按钮) / human_via_agent(用户给了'
    '结论、经 deskcore review_drafts 工具写入, 原话在 decision_note) / auto_hard_rule / '
    'auto_dedup / system。NULL = 本列上线前的存量行(刻意不回填)。'
    '下游要把机器判定当人工反馈用之前, 必须先按这一列过滤。见审计 COR-004 / A-01。';
COMMENT ON COLUMN autowriter.items.decision_note IS
    '用户给出结论时的原话(review_drafts 的 user_words), ≤ 200 字。只有 human_via_agent '
    '会写。它存在的意义是让人能复核「这条通过是不是真有人说过」。';
COMMENT ON COLUMN autowriter.items.decided_within_s IS
    'decided_at 距这批稿子 batches.created_at 的秒数, 服务端算。几秒内审完一批就是'
    '模型替用户点的信号。没有 batch 行时 NULL。';

COMMIT;
