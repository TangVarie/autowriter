-- ══════════════════════════════════════════════════════════════════════
-- 013 · 改得多的同一篇也认 + 记下人在发布前改了什么(2026-10-19, D-120 / W-23)
--
-- 为什么
-- ──────
-- 写手不再"提交 / 导出"(D-120): 定稿贴进飞书, 发布后 tv-sync 按内容对回写作台的那一版。
-- 对得上靠四档(开头 / 标题 / 包含度 ≥0.6 / 时间窗), 人改得多一些的(包含度 0.30~0.60)
-- 以前直接当新稿补录。10-19 实测这一档里领先第二名 ≥0.15 的人一看就是同一篇改出来的,
-- 多了一个 match_kind = 'edited'。
--
-- 而"人改了什么"本身是信号(owner 10-19:「改了什么, 哪怕就是几个字, 也是很有意思的可以抽象
-- 出来的点」)。每一对对上的稿子记一行差异(tv_edit_diffs), 按项目攒着; open_project 每周给
-- 写手的模型一份摘要, 由模型归纳成规则建议、问过用户再 record_rule, 看完记一笔(tv_edit_digest_acks)。
--
-- 这次做什么
-- ──────────
-- ① tv_note_links.match_kind 的 CHECK 多一个 'edited'
-- ② autowriter.tv_edit_diffs        每条对上的笔记一行(主键 note_id, 重对时覆盖)
-- ③ autowriter.tv_edit_digest_acks  每个写作台项目一行: 摘要看到哪儿了
-- ④ v_angle_outcomes 把 'edited' 也算作对上(视图在才改; 没有 TV 表的库 011 本来就跳过)
--
-- 幂等: CHECK 先删后加(同名); 表 IF NOT EXISTS; GRANT / RLS 重复执行是 no-op。
-- ⚠️ 同样的改动也进了 000_baseline.sql(README 的规矩: 加表加列两边都改)。
-- ══════════════════════════════════════════════════════════════════════

BEGIN;

-- ── ① match_kind 多一个 edited ─────────────────────────────────────────
ALTER TABLE autowriter.tv_note_links DROP CONSTRAINT IF EXISTS tv_note_links_match_kind_check;
ALTER TABLE autowriter.tv_note_links ADD CONSTRAINT tv_note_links_match_kind_check
    CHECK (match_kind IN ('body_exact', 'title_exact', 'fuzzy', 'edited', 'ingested',
                          'ambiguous', 'unmatched', 'tv_lineage'));

-- ── ② 改动差异 ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS autowriter.tv_edit_diffs (
    note_id            TEXT PRIMARY KEY,
    tv_project_id      TEXT NOT NULL,
    project_id         UUID REFERENCES autowriter.projects(id) ON DELETE CASCADE,
    version_id         UUID REFERENCES autowriter.versions(id) ON DELETE CASCADE,
    match_kind         TEXT NOT NULL,
    changed            BOOLEAN NOT NULL,
    title_changed      BOOLEAN NOT NULL,
    draft_title        TEXT NOT NULL DEFAULT '',
    published_title    TEXT NOT NULL DEFAULT '',
    draft_chars        INTEGER NOT NULL,
    published_chars    INTEGER NOT NULL,
    removed_count      INTEGER NOT NULL,
    added_count        INTEGER NOT NULL,
    edited_count       INTEGER NOT NULL DEFAULT 0,      -- 还在、但里面改了字的句子
    removed_sentences  TEXT[] NOT NULL DEFAULT '{}',
    added_sentences    TEXT[] NOT NULL DEFAULT '{}',
    edited_sentences   JSONB NOT NULL DEFAULT '[]'::jsonb,   -- [[原句, 改后], ...] 最多 8 对
    brand_draft        INTEGER,
    brand_published    INTEGER,
    publish_time       TIMESTAMPTZ,
    computed_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tv_edit_diffs_project_idx
    ON autowriter.tv_edit_diffs (project_id, computed_at);
GRANT SELECT, INSERT, UPDATE, DELETE ON autowriter.tv_edit_diffs TO service_role;
ALTER TABLE autowriter.tv_edit_diffs ENABLE ROW LEVEL SECURITY;

-- ── ③ 摘要看到哪儿了 ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS autowriter.tv_edit_digest_acks (
    project_id      UUID PRIMARY KEY REFERENCES autowriter.projects(id) ON DELETE CASCADE,
    acked_through   TIMESTAMPTZ NOT NULL,
    acked_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    acked_by        UUID,
    rules_recorded  INTEGER NOT NULL DEFAULT 0
);
GRANT SELECT, INSERT, UPDATE, DELETE ON autowriter.tv_edit_digest_acks TO service_role;
ALTER TABLE autowriter.tv_edit_digest_acks ENABLE ROW LEVEL SECURITY;

-- ── ④ 视图把 edited 也算作对上 ─────────────────────────────────────────
-- 只换 JOIN 条件里的那一串, 列不变(CREATE OR REPLACE VIEW 不许改列)。视图不在(本地 harness /
-- 没有 TV 表的新库, 011 本来就跳过)就不建 —— 那是 011 的事。
DO $v013$
BEGIN
    IF to_regclass('autowriter.v_angle_outcomes') IS NULL THEN
        RAISE NOTICE '013: 没有 autowriter.v_angle_outcomes(011 跳过了), 不改视图';
        RETURN;
    END IF;
    EXECUTE $view$
        CREATE OR REPLACE VIEW autowriter.v_angle_outcomes
        WITH (security_invoker = false) AS
        SELECT a.id                  AS ledger_id,
               a.project_id,
               a.angle_key,
               a.dims,
               a.drawn_by,
               a.drawn_at,
               a.consumed_version_id,
               a.consumed_at,
               l.note_id,
               l.tv_project_id,
               l.match_kind,
               l.lag_days,
               n.tier,
               n.publish_time
          FROM autowriter.angle_ledger a
          LEFT JOIN autowriter.tv_note_links l
                 ON l.version_id = a.consumed_version_id
                AND l.match_kind IN ('body_exact', 'title_exact', 'fuzzy', 'edited', 'tv_lineage')
          LEFT JOIN truth_vault.notes n
                 ON n.note_id = l.note_id
         WHERE a.consumed_version_id IS NOT NULL
    $view$;
END;
$v013$;

COMMIT;
