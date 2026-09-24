-- ══════════════════════════════════════════════════════════════════════
-- 011 · 发牌台账接上结果: autowriter.v_angle_outcomes(2026-09-24)
--
-- 病灶
-- ────
-- angle_ledger 只记 drawn_at / consumed_version_id, 不记结果(docs/deskcore.md §3.4
-- 末尾)。发牌要按规律加权(JevforCoentent docs/31 位置 ④), 前提是能算「哪些坐标出了
-- 爆文」—— 而链路其实早就齐了, 只差一个视图把它们连起来:
--
--   angle_ledger.consumed_version_id (UUID)
--     → tv_note_links.version_id (UUID) → tv_note_links.note_id (TEXT)   (009)
--     → truth_vault.notes.note_id (TEXT) → notes.tier
--
-- 两段 JOIN 的类型一一对上(UUID↔UUID、TEXT↔TEXT), 不需要任何转换。跨 schema 视图在
-- TV 有先例(notes_v1_2_cross_schema_views.sql 的 v_model_comparison 就 JOIN 在
-- autowriter.versions 上)。
--
-- 口径
-- ────
-- · 只列**用掉了**的坐标(consumed_version_id 非 NULL)。只抽了没写的占位行不是结果。
-- · LEFT JOIN: 用掉了但还没对上笔记(没发 / 还没被 tv-sync 认回来)的也在, note_id /
--   tier 为 NULL —— 算「这个坐标的爆文率」要分母, 丢掉它们就只剩成功样本。
-- · 只认真对上的 match_kind(body_exact / title_exact / fuzzy / tv_lineage), 与
--   core._TV_BACKFILL_KINDS 同一份名单(tests/test_angle_outcomes_view.py 盯着两边一致)。
--   ingested 不认: 那种版本是从笔记复制进来的, 因果倒置; ambiguous / unmatched 本来
--   就没有 version_id。
-- · 同一版发了两条笔记就是两行(一条笔记一行结果), 按坐标聚合时自己去重。
--
-- ⚠️ 权限: 为什么非得「属主建 + 显式 GRANT」
-- ──────────────────────────────────────────
-- 三张底表都开着 RLS 且**没有任何 policy**(angle_ledger: 001; tv_note_links: 010;
-- truth_vault.notes: TV notes_v1_2), tv_note_links 的表级 GRANT 也只发给了 service_role。
-- 视图用 security_invoker = false(以视图属主的权限读底表):
--   · 属主必须是底表的属主(或 BYPASSRLS / superuser)。换一个普通角色来建, RLS 会以
--     那个角色的身份生效 —— 视图建得出来、查得动、**永远 0 行, 不报错**。所以下面先验
--     当前角色, 不够格就当场 RAISE, 不留一个安静的空视图。Supabase 上用 SQL Editor /
--     MCP apply_migration 跑, 身份就是 postgres(三张底表的属主), 验得过。
--   · autowriter 是本仓自建 schema, **没有** default privileges(migrations/README
--     「建出来 ≠ 能访问」): 新视图出生对 service_role 也是零权限, 必须显式 GRANT。
--   · 只 GRANT 给 service_role(deskcore 发牌加权、TV 的脚本都走它)。**不**发给
--     anon / authenticated: 这是明细视图(坐标 × 笔记 × tier), 以属主身份读、绕过了
--     RLS, 发给 anon 等于把全部项目的台账连同笔记对照开放到公网 —— anon key 是按
--     「可公开」设计的。看板要看, 按 TV dashboard_views_v1.sql 的做法在 public 建
--     **只吐计数**的聚合视图, 由它(属主 postgres)去读本视图。
--
-- 没有 truth_vault 的库
-- ────────────────────
-- 本地 harness 与 fresh install 没有 truth_vault.notes。视图不像 009 的函数那样能靠
-- 动态 SQL 延迟解析表名, 所以整段包在 DO 里: 没有就 NOTICE 跳过, TV 落库之后**重跑
-- 本文件**即可。doctor 会把「视图不在」报成 missing。
--
-- 幂等: CREATE OR REPLACE VIEW(列只增不改)/ REVOKE / GRANT, 重复执行干净 no-op。
-- 基线 000 里有逐字相同的一段(tests/test_angle_outcomes_view.py 守着)。
-- 生产库尚未执行。
-- ══════════════════════════════════════════════════════════════════════

BEGIN;

DO $v011$
DECLARE
    _blind TEXT;
BEGIN
    IF to_regclass('autowriter.angle_ledger') IS NULL
       OR to_regclass('autowriter.tv_note_links') IS NULL THEN
        RAISE EXCEPTION '011: 缺 autowriter.angle_ledger(001)或 autowriter.tv_note_links(009) —— 先按编号跑完前面的迁移';
    END IF;
    IF to_regclass('truth_vault.notes') IS NULL THEN
        RAISE NOTICE '011: 库里没有 truth_vault.notes, 跳过 autowriter.v_angle_outcomes(本地 harness / 新库的正常形态); TV 落库之后重跑本文件即可';
        RETURN;
    END IF;

    -- 以当前角色为属主建视图时, 哪几张底表会被 RLS 挡成空集。
    -- 表属主(含继承其权限的成员)在没开 FORCE 时不受 RLS 约束; superuser / BYPASSRLS 全不受。
    SELECT string_agg(c.oid::regclass::text, ', ' ORDER BY c.oid::regclass::text)
      INTO _blind
      FROM pg_catalog.pg_class c
     WHERE c.oid IN ('autowriter.angle_ledger'::regclass,
                     'autowriter.tv_note_links'::regclass,
                     'truth_vault.notes'::regclass)
       AND c.relrowsecurity
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r
                        WHERE r.rolname = current_user AND (r.rolsuper OR r.rolbypassrls))
       AND NOT (pg_has_role(current_user, c.relowner, 'USAGE') AND NOT c.relforcerowsecurity);
    IF _blind IS NOT NULL THEN
        RAISE EXCEPTION '011: 当前角色 % 不是 % 的属主、也不能绕过 RLS —— 以它为属主建的 v_angle_outcomes 会永远查出 0 行且不报错。换底表属主(Supabase 上是 postgres, 即 SQL Editor / apply_migration 的默认身份)来跑', current_user, _blind;
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
                AND l.match_kind IN ('body_exact', 'title_exact', 'fuzzy', 'tv_lineage')
          LEFT JOIN truth_vault.notes n
                 ON n.note_id = l.note_id
         WHERE a.consumed_version_id IS NOT NULL
    $view$;

    EXECUTE 'COMMENT ON VIEW autowriter.v_angle_outcomes IS '
         || quote_literal('发牌台账 → 那一版 → TV 笔记 → tier(migrations/011)。只列用掉了的坐标; '
                          '还没对上笔记的 note_id / tier 为 NULL。明细视图, 只授权给 service_role。');

    -- 角色用 IF EXISTS 包着: Supabase 上三个都有, 裸 PostgreSQL(CI / 自托管)没有,
    -- 直接 GRANT 会报 role does not exist(同 TV dashboard_views_v1.sql 的写法)。
    REVOKE ALL ON autowriter.v_angle_outcomes FROM PUBLIC;
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON autowriter.v_angle_outcomes FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON autowriter.v_angle_outcomes FROM authenticated;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'service_role') THEN
        GRANT SELECT ON autowriter.v_angle_outcomes TO service_role;
    END IF;
END;
$v011$;

COMMIT;
