-- ══════════════════════════════════════════════════════════════════════
-- 019 · 查重取候选先走四字串的 GIN 索引、再筛项目(2026-11-11, W-21 / AU-F08)
--
-- 为什么
-- ──────
-- deskcore_check_drafts 和 deskcore_commit_fingerprints 取候选的那一步原来写的是
--     WHERE f.project_id = _project_id AND f.ngram_hashes && ng
-- 计划器对它(定制计划、通用计划都一样)选的是: 按项目索引取出整个项目的行, 再逐行比 &&。
-- draft_fp_ngram_gin_idx 从 001 起就在, 却从来轮不上。耗时跟项目的历史条数、稿子的 hash 数一起涨。
-- 11-11 生产库只读实测(全库 7,782 行, 最大的项目 1,163 行):
--   · 89 个 hash 的一篇: 这一步 366 毫秒(读 1,163 行, 留下 11 行); 400 个 hash 的一篇整条函数 1,560 毫秒;
--   · 最大的项目最近 10 篇一起查: 3,677 毫秒。同一组稿子在查重、入库前重查、入库写指纹三处各付一次。
--
-- 这次做什么
-- ──────────
-- 只换两条函数里取候选的那一步:
--     cand AS MATERIALIZED (... WHERE f.ngram_hashes && (SELECT ng))
-- 项目条件和 array_length 条件挪到读 cand 的那一层。还是那三个条件, 只是先后换了, 所以候选集不变、
-- 结论不变。判定公式、阈值、签名、返回列、STABLE、search_path、授权一个字没动; 不碰数据, 不建索引。
-- 两样缺一不可:
--   · MATERIALIZED —— 不让项目条件推回 CTE 里(推回去就又是按项目索引取全项目、逐行比)。
--   · (SELECT ng)  —— 把数组的值对计划器藏起来。plpgsql 里每条连接头 5 次执行用定制计划, 直接写 ng
--     的话计划器按实际数组估行数(生产库 89 个 hash 那篇估成 2,799 / 7,717 行会命中), 选全表顺序扫描:
--     那一篇 3,225 毫秒。**拿 OFFSET 0 当栅栏的写法同样走到顺序扫描(生产库实测 4,835 毫秒), 不能用。**
--
-- 生产库只读实测(11-11; 新写法是把这一步照抄成普通 SQL 跑的, 都是 SELECT):
--   · 候选集: 10 组(一篇的 sketch × 一个项目)新旧逐行相同 —— 1,163 行的项目(89 / 400 个 hash)、
--     有 117 行空数组的项目、只有 2 行和 3 行的项目、整个项目都是空数组的、sketch 自己是空数组的、
--     拿别的项目的稿子来查的。同 10 组里, 线上函数回的 best_j / j_title / best_c / c_title / c_sample
--     与新写法逐列相同。
--   · 耗时: 89 hash 那篇这一步 366 → 8 毫秒; 400 hash 那篇这一步 33 毫秒(整条旧函数 1,560);
--     最大的项目最近 10 篇整组 3,677 → 341 毫秒。
--   · 计划: Bitmap Index Scan on draft_fp_ngram_gin_idx(成本 206; 顺序扫描 627)。
--   · 代价: GIN 的钱跟全库命中的行数走, 不跟项目走。二三十行以下的小项目这一步原来是 1 毫秒级,
--     现在每篇 10~20 毫秒(2 行的项目: 整条旧函数 6 毫秒, 新写法这一步 21 毫秒)。全库涨到几万行时再看一次。
--
-- ⚠️ 计划器走不走 GIN 还看库的大小: 很小的库(本地 harness、新库)顺序扫描本来就更便宜, 它照旧顺序扫描,
-- 结论一样。所以 CI 只钉写法(tests/test_dedup_gin_first.py: 两个记号都在代码里)和结果
-- (tests/sql_parity_check.py 逐例对 Python), 不钉计划; 计划在生产库上跑完之后用文件末尾 ② 那一句核一次。
--
-- 先后: 代码不依赖它(Python 一行没改), 先于或后于部署都行。跑之前、跑之后查重的结论相同。
-- ⚠️ 用函数属主跑(Supabase 上 SQL Editor 的默认身份 postgres): CREATE OR REPLACE 要求属主。
-- doctor 探不到函数体(同 008 / 016), 报 unprobeable 并给出 SQL Editor 里查的那句。回滚见文件末尾。
--
-- 幂等: CREATE OR REPLACE + REVOKE/GRANT, 重复执行干净 no-op。⚠️ 同样两块也进了 000_baseline.sql
-- (逐字节相同, tests/test_baseline_parity.py 守着)。
-- ══════════════════════════════════════════════════════════════════════

BEGIN;

SET LOCAL search_path TO autowriter, extensions, public;

-- ── ① 查重(check_drafts 工具与入库前的重查共用) ── 008 那一版, 只换 ② 里取候选的那一步
CREATE OR REPLACE FUNCTION autowriter.deskcore_check_drafts(
    _project_id UUID,
    -- [{opening_hash, ngram_hashes, title_embedding}, ...]
    -- title_embedding 可以是 null(本批算不出向量), 那一路直接跳过。
    _rows       JSONB,
    -- 包含度那一路的有效样本量下限。真正生效的值由 Python 侧传下来
    -- (fingerprint.CONTAIN_MIN_SAMPLE), 这里的 DEFAULT 只是让手动在 SQL
    -- 控制台里调用时不至于报缺参 —— 两边写死两份就迟早对不上。
    _contain_min_sample INT DEFAULT 15
)
RETURNS TABLE(
    idx          INT,
    best_sim     NUMERIC,   -- 标题语义余弦的最大值; 没有可比向量时 0
    sim_title    TEXT,
    best_j       NUMERIC,   -- 正文四字串 Jaccard 的最大值(bottom-k 无偏估计)
    j_title      TEXT,
    open_exact   BOOLEAN,   -- 正文开头精确撞车
    open_title   TEXT,
    best_c       NUMERIC,   -- 正文四字串包含度的最大值(审计 COR-014)
    c_title      TEXT,
    c_sample     INT        -- 上面那次估计的有效样本量; 太小则调用方不采信
)
LANGUAGE plpgsql
STABLE
SET search_path = pg_catalog, extensions   -- ::vector 需要 extensions
AS $$
DECLARE
    r      JSONB;
    i      INT := -1;
    ng     TEXT[];
    ng_max TEXT;
    oh     TEXT;
    v      vector;
    -- 产出本批向量的模型名。NULL = 调用方没带(老客户端), 走兼容路径。
    _model TEXT;
BEGIN
    FOR r IN SELECT * FROM jsonb_array_elements(_rows) LOOP
        i := i + 1;
        idx := i;
        best_sim := 0; sim_title := NULL;
        best_j := 0;   j_title := NULL;
        best_c := 0;   c_title := NULL;  c_sample := 0;
        open_exact := FALSE; open_title := NULL;

        oh := NULLIF(r->>'opening_hash', '');
        SELECT COALESCE(array_agg(x), '{}') INTO ng
          FROM jsonb_array_elements_text(COALESCE(r->'ngram_hashes','[]'::jsonb)) x;
        ng_max := (SELECT max(x) FROM unnest(ng) x);
        v := CASE
                WHEN r->'title_embedding' IS NULL
                  OR jsonb_typeof(r->'title_embedding') = 'null'
                THEN NULL
                ELSE (r->>'title_embedding')::vector
             END;
        _model := NULLIF(r->>'embedding_model', '');

        -- ① 开头精确撞车。oh 为空 = 这篇没有正文开头, 空 == 空【不算撞车】
        --    (否则所有 title-only 的稿子会互相"精确撞车", 见 fp.opening_hash)。
        IF oh IS NOT NULL THEN
            SELECT f.title INTO open_title
              FROM autowriter.draft_fingerprints f
             WHERE f.project_id = _project_id AND f.opening_hash = oh
             LIMIT 1;
            open_exact := open_title IS NOT NULL;
        END IF;

        -- ② 正文四字串: Jaccard 与包含度**同一次扫描**算完, 各取各的最佳命中。
        --    GIN 的 && 先粗筛 —— 没有交集的行两个指标都是 0, 不可能成为最大值。
        IF ng_max IS NOT NULL THEN
            -- ⚠️ 逐行用 LATERAL 往下传, **不要**把中间结果拆成两个 CTE 再按
            --    title join 回去 —— title 不唯一(同一个项目里重名的历史稿很
            --    常见), 那样会扇出成笛卡尔积, 指标全错而且不报错。
            -- ⚠️ 候选先走 GIN(draft_fp_ngram_gin_idx)、再筛项目(migrations/019)。
            --    下面两样缺一不可, 别"简化"回去:
            --    · MATERIALIZED —— 不让项目条件推回这一层。推回去的话计划器按
            --      项目索引取出整个项目的行、再逐行比 &&(千行的项目一篇 0.4 秒)。
            --    · (SELECT ng) —— 把数组的值对计划器藏起来。直接写 ng 的话, 每条
            --      连接头几次执行按实际数组估行数、选全表顺序扫描(一篇要几秒);
            --      用 OFFSET 0 当栅栏同样会走到顺序扫描。
            --    候选集不变: 还是同一个 && 条件、同一个项目条件, 只是先后换了。
            WITH cand AS MATERIALIZED (
                SELECT f.title, f.ngram_hashes AS hs, f.project_id AS pid
                  FROM autowriter.draft_fingerprints f
                 WHERE f.ngram_hashes && (SELECT ng)
            ),
            metrics AS (
                SELECT c.title,
                       m.inter::numeric   AS inter,
                       m.uni::numeric     AS uni,
                       m.smaller::numeric AS smaller
                  FROM cand c
                  -- t = min(两个 sketch 各自的最大值)。定长十六进制,
                  -- 字典序即数值序。
                  CROSS JOIN LATERAL (
                      SELECT LEAST(ng_max,
                                   (SELECT max(x) FROM unnest(c.hs) x)) AS t
                  ) tt
                  CROSS JOIN LATERAL (
                      SELECT count(*) FILTER (WHERE g.in_a AND g.in_b) AS inter,
                             count(*)                                  AS uni,
                             LEAST(count(*) FILTER (WHERE g.in_a),
                                   count(*) FILTER (WHERE g.in_b))     AS smaller
                        FROM (
                            SELECT z.h,
                                   bool_or(z.src = 'a') AS in_a,
                                   bool_or(z.src = 'b') AS in_b
                              FROM (SELECT x AS h, 'a' AS src
                                      FROM unnest(ng) x WHERE x <= tt.t
                                    UNION ALL
                                    SELECT y, 'b'
                                      FROM unnest(c.hs) y WHERE y <= tt.t) z
                             GROUP BY z.h
                        ) g
                  ) m
                 WHERE c.pid = _project_id
                   AND array_length(c.hs, 1) IS NOT NULL
            ),
            -- ⚠️ 两路的最佳命中必须在【同一条语句】里取完。WITH 的作用域只有
            --    一条语句 —— 拆成两条 SELECT 的话第二条会报 relation "metrics"
            --    does not exist。(在真 PostgreSQL 上跑才发现的; 光看代码
            --    和 py_compile 都看不出来。)
            -- Jaccard 自己的样本量下限是**并集**大小(而不是包含度用的
            -- min(|a|,|b|)) —— 两者不能混用, 理由见 fingerprint.sketch_overlap
            -- 的注释: 短稿 vs 超长历史稿时 min 只有两三个但 union 有几百,
            -- 那是正常形态; 真正不可用的是**两边都塌到个位数**的时候。
            jbest AS (
                SELECT x.title, x.j FROM (
                    SELECT m.title, m.inter / NULLIF(m.uni, 0) AS j
                      FROM metrics m WHERE m.uni >= _contain_min_sample
                ) x WHERE x.j IS NOT NULL ORDER BY x.j DESC, x.title LIMIT 1
            ),
            -- ⚠️ 样本量的判据必须在 ORDER BY 之前。原来是先按 c 取冠军、把
            --    冠军的样本量一起返回给调用方事后判断 —— 一条毫不相关、只跟
            --    本稿撞上一个低位 hash 的历史稿能拿到 c=1.0 / 样本量=1, 压过
            --    真正的抄袭源(c=0.9 / 样本量>=15); 冠军随后因样本量不足被丢掉,
            --    真命中根本没进过决赛, 照搬长稿的稿子就这么放行了。
            --    Python 侧 core.py 的两处 `if s >= CONTAIN_MIN_SAMPLE` 同口径。
            cbest AS (
                SELECT x.title, x.c, x.smaller FROM (
                    SELECT m.title, m.inter / NULLIF(m.smaller, 0) AS c,
                           m.smaller::int AS smaller
                      FROM metrics m
                ) x WHERE x.c IS NOT NULL AND x.smaller >= _contain_min_sample
                  ORDER BY x.c DESC, x.title LIMIT 1
            )
            SELECT (SELECT b.j FROM jbest b), (SELECT b.title FROM jbest b),
                   (SELECT b.c FROM cbest b), (SELECT b.title FROM cbest b),
                   (SELECT b.smaller FROM cbest b)
              INTO best_j, j_title, best_c, c_title, c_sample;
            best_j := COALESCE(best_j, 0);
            best_c := COALESCE(best_c, 0);
            c_sample := COALESCE(c_sample, 0);
        END IF;

        -- ③ 标题语义余弦。见 migrations/004 的文件头: ORDER BY 的是别名 sim,
        --    不是距离算子 —— 刻意绕开 ivfflat 的近似最近邻。
        IF v IS NOT NULL THEN
            SELECT t.title, t.sim INTO sim_title, best_sim
              FROM (
                SELECT f.title, 1 - (f.title_embedding <=> v) AS sim
                  FROM autowriter.draft_fingerprints f
                 WHERE f.project_id = _project_id
                   AND f.title_embedding IS NOT NULL
                   -- ⚠️ 按模型隔离(migrations/008)。跨模型算余弦出来的数是
                   --    噪声, 而且【不报错】—— 既会放过真重复, 也会误杀无关稿,
                   --    同时 semantic_degraded 还报 false。
                   --    `=` 而不是 IS NOT DISTINCT FROM: embedding_model 为
                   --    NULL 的行是"来路不明", 必须一起排除, 而 NULL = X 恰好
                   --    就是 NULL(不匹配)。
                   --    _model 为 NULL(老调用方没带)时整句退化为 TRUE, 保持
                   --    008 之前的行为 —— 见文件头对这条兼容路径的说明。
                   AND (_model IS NULL OR f.embedding_model = _model)
              ) t
             ORDER BY t.sim DESC, t.title
             LIMIT 1;
            -- 全负分时报 0 —— 与 Python 侧"累加器从 0.0 起、严格大于才顶替"
            -- 的口径一致(dedup.find_near_duplicates 同款处理)。
            IF best_sim IS NULL OR best_sim <= 0 THEN
                best_sim := 0; sim_title := NULL;
            END IF;
        END IF;

        RETURN NEXT;
    END LOOP;
END;
$$;

REVOKE ALL ON FUNCTION autowriter.deskcore_check_drafts(UUID, JSONB, INT) FROM PUBLIC;
REVOKE ALL ON FUNCTION autowriter.deskcore_check_drafts(UUID, JSONB, INT) FROM anon;
REVOKE ALL ON FUNCTION autowriter.deskcore_check_drafts(UUID, JSONB, INT) FROM authenticated;
GRANT EXECUTE ON FUNCTION autowriter.deskcore_check_drafts(UUID, JSONB, INT) TO service_role;

-- ── ② 入库写指纹时的原子重查 ── 005 那一版, 同样只换取候选的那一步
CREATE OR REPLACE FUNCTION autowriter.deskcore_commit_fingerprints(
    _project_id         UUID,
    _rows               JSONB,
    _user_id            UUID,
    _ngram_hard         NUMERIC DEFAULT 0.35,
    _contain_hard       NUMERIC DEFAULT 0.60,
    _contain_min_sample INT     DEFAULT 15
)
RETURNS TABLE(idx INT, status TEXT, collided_with TEXT, detail TEXT)
LANGUAGE plpgsql
SET search_path = pg_catalog, extensions   -- ::vector 需要 extensions
AS $$
DECLARE
    r        JSONB;
    i        INT := -1;
    ng       TEXT[];
    ng_max   TEXT;
    oh       TEXT;
    hit      RECORD;
    best_j   NUMERIC;
    best_t   TEXT;
    best_c   NUMERIC;
    best_ct  TEXT;
    best_cs  INT;
    open_hit BOOLEAN;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('deskcore_draw:' || _project_id::text));

    FOR r IN SELECT * FROM jsonb_array_elements(_rows) LOOP
        i := i + 1;
        oh := r->>'opening_hash';
        SELECT COALESCE(array_agg(x), '{}') INTO ng
          FROM jsonb_array_elements_text(COALESCE(r->'ngram_hashes','[]'::jsonb)) x;
        ng_max := (SELECT max(x) FROM unnest(ng) x);

        -- ① 开头精确撞车
        -- ⚠️ 空开头(正文为空的 title-only 稿)不参与这一条。Python 侧
        -- fp.opening_hash 现在对空开头返回空串, 这里把空串和 NULL 一起排除 ——
        -- 否则所有 title-only 的行会互相"精确撞车", 而 opening_exact 是单独
        -- 就判死的强信号, 没有任何东西兜得住这个误伤。(codex review)
        open_hit := FALSE;
        IF oh IS NOT NULL AND oh <> '' THEN
            SELECT TRUE, f.title INTO open_hit, best_t
              FROM autowriter.draft_fingerprints f
             WHERE f.project_id = _project_id
               AND f.opening_hash = oh
               AND f.opening_hash <> ''
             LIMIT 1;
            -- 撞上的那行标题本身可能为空, 所以判据用 open_hit 而不是 best_t。
            open_hit := COALESCE(open_hit, FALSE);
        END IF;
        IF open_hit THEN
            idx := i; status := 'rejected';
            collided_with := best_t; detail := '正文开头与库中已有稿件完全一致';
            RETURN NEXT;
            CONTINUE;
        END IF;

        -- ② 四字串: Jaccard 与包含度同一次扫描算完, 口径与 deskcore_check_drafts
        --    完全一致(受限子域 + bottom-k 标准估计式)。
        best_j := 0; best_t := NULL;
        best_c := 0; best_ct := NULL; best_cs := 0;
        IF ng_max IS NOT NULL THEN
            FOR hit IN
                -- ⚠️ 候选先走 GIN、再筛项目(migrations/019): MATERIALIZED 和
                --    (SELECT ng) 缺一不可, 理由见 deskcore_check_drafts 的 cand。
                WITH cand AS MATERIALIZED (
                    SELECT f.title, f.ngram_hashes AS hs, f.project_id AS pid
                      FROM autowriter.draft_fingerprints f
                     WHERE f.ngram_hashes && (SELECT ng)
                )
                SELECT c.title,
                       m.inter::numeric / NULLIF(m.uni, 0)     AS j,
                       m.inter::numeric / NULLIF(m.smaller, 0) AS c,
                       m.smaller::int                          AS smaller,
                       m.uni::int                              AS uni
                  FROM cand c
                  CROSS JOIN LATERAL (
                      SELECT LEAST(ng_max, (SELECT max(x) FROM unnest(c.hs) x)) AS t
                  ) tt
                  CROSS JOIN LATERAL (
                      SELECT count(*) FILTER (WHERE g.in_a AND g.in_b) AS inter,
                             count(*)                                  AS uni,
                             LEAST(count(*) FILTER (WHERE g.in_a),
                                   count(*) FILTER (WHERE g.in_b))     AS smaller
                        FROM (
                            SELECT z.h, bool_or(z.src = 'a') AS in_a,
                                        bool_or(z.src = 'b') AS in_b
                              FROM (SELECT x AS h, 'a' AS src
                                      FROM unnest(ng) x WHERE x <= tt.t
                                    UNION ALL
                                    SELECT y, 'b'
                                      FROM unnest(c.hs) y WHERE y <= tt.t) z
                             GROUP BY z.h
                        ) g
                  ) m
                 WHERE c.pid = _project_id
                   AND array_length(c.hs, 1) IS NOT NULL
            LOOP
                -- Jaccard 看并集样本量, 包含度看 min —— 两个下限用同一个数,
                -- 但量的是不同的东西。见 check 侧 jbest 上面那段注释。
                IF hit.j IS NOT NULL AND hit.uni >= _contain_min_sample
                   AND hit.j > best_j THEN
                    best_j := hit.j; best_t := hit.title;
                END IF;
                -- 包含度记它自己的最佳命中: 抄袭源和"用词最像的那篇"经常不是
                -- 同一条, 报错时要指对人。
                -- ⚠️ 样本量先过闸再比大小(与 check 侧的 cbest、Python 侧
                --    core.py 同一口径)。否则一条只撞上一个低位 hash 的无关
                --    历史稿会以 c=1.0/样本量=1 当选, 把真正的抄袭源挤掉,
                --    随后又因样本量不足被下面那个 IF 放行。
                IF hit.c IS NOT NULL AND hit.smaller >= _contain_min_sample
                   AND hit.c > best_c THEN
                    best_c := hit.c; best_ct := hit.title; best_cs := hit.smaller;
                END IF;
            END LOOP;
        END IF;
        IF best_j >= _ngram_hard THEN
            idx := i; status := 'rejected'; collided_with := best_t;
            detail := format('正文与库中已有稿件大面积重合(四字串 Jaccard=%s)', round(best_j, 3));
            RETURN NEXT;
            CONTINUE;
        END IF;
        -- 样本量不够就不发言 —— 与 Python 侧 CONTAIN_MIN_SAMPLE 同一口径。
        -- 这一路刻意 fail-open: 样本量小的时候无关稿子也能撞出高包含度,
        -- 按硬闸处理就是误杀。理由见 deskcore/fingerprint.py 的注释。
        IF best_cs >= _contain_min_sample AND best_c >= _contain_hard THEN
            idx := i; status := 'rejected'; collided_with := best_ct;
            detail := format('正文有 %s%% 的四字串出现在库中已有稿件里(照搬长稿)',
                             round(best_c * 100, 1));
            RETURN NEXT;
            CONTINUE;
        END IF;

        INSERT INTO autowriter.draft_fingerprints
            (project_id, version_id, user_id, title, opening,
             title_embedding, embedding_model, opening_hash, ngram_hashes, angle_key)
        VALUES (
            _project_id,
            NULLIF(r->>'version_id','')::uuid,
            _user_id,
            COALESCE(r->>'title',''),
            COALESCE(r->>'opening',''),
            CASE WHEN r->'title_embedding' IS NULL OR jsonb_typeof(r->'title_embedding') = 'null'
                 THEN NULL ELSE (r->>'title_embedding')::vector END,
            NULLIF(r->>'embedding_model',''),
            oh,
            ng,
            NULLIF(r->>'angle_key','')
        );

        -- ⚠️ 这个字面量是**跨语言契约**: deskcore/core.py 按
        --    COMMIT_STATUS_INSERTED('inserted') 数 written、并据此给角度台账
        --    销账。曾经这里写成 'written', 后果不是报错 —— 是每次成功入库都
        --    报 written=0、一条角度都不销账(同一坐标可以被无限次抽到), 全程
        --    没有任何异常。改这个词之前先改 core.py 的那个常量。
        idx := i; status := 'inserted'; collided_with := NULL; detail := NULL;
        RETURN NEXT;
    END LOOP;
END;
$$;

REVOKE ALL ON FUNCTION autowriter.deskcore_commit_fingerprints(UUID, JSONB, UUID, NUMERIC, NUMERIC, INT) FROM PUBLIC;
REVOKE ALL ON FUNCTION autowriter.deskcore_commit_fingerprints(UUID, JSONB, UUID, NUMERIC, NUMERIC, INT) FROM anon;
REVOKE ALL ON FUNCTION autowriter.deskcore_commit_fingerprints(UUID, JSONB, UUID, NUMERIC, NUMERIC, INT) FROM authenticated;
GRANT EXECUTE ON FUNCTION autowriter.deskcore_commit_fingerprints(UUID, JSONB, UUID, NUMERIC, NUMERIC, INT) TO service_role;

COMMIT;

-- ══════════════════════════════════════════════════════════════════════
-- 校验(人工跑, 不在上面那个事务内; 三句都是只读的 SELECT)
-- ══════════════════════════════════════════════════════════════════════

-- ── ① 跑没跑: 两条函数的函数体里两个记号都在 ─────────────────────────
-- 先剥注释再查(理由同 008: 注释里也写着这两个词)。
--
--   SELECT p.proname,
--          regexp_replace(p.prosrc, '--[^\n]*', '', 'g') LIKE '%cand AS MATERIALIZED (%'        AS gin_first,
--          regexp_replace(p.prosrc, '--[^\n]*', '', 'g') LIKE '%f.ngram_hashes && (SELECT ng)%' AS array_hidden
--     FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
--    WHERE n.nspname = 'autowriter'
--      AND p.proname IN ('deskcore_check_drafts', 'deskcore_commit_fingerprints');
--   → 两行, 四个 t。

-- ── ② 真的快了: 拿最大的项目最近 10 篇的 sketch 调一次线上函数 ────────
-- deskcore_check_drafts 是 STABLE 的, 这一句一行都不写。
--
--   EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF)
--   SELECT r.idx, r.best_j
--     FROM (SELECT project_id FROM autowriter.draft_fingerprints
--            GROUP BY project_id ORDER BY count(*) DESC LIMIT 1) p
--    CROSS JOIN LATERAL (
--          SELECT jsonb_agg(jsonb_build_object('opening_hash', '',
--                                              'ngram_hashes', to_jsonb(t.ngram_hashes))) AS j
--            FROM (SELECT f.ngram_hashes FROM autowriter.draft_fingerprints f
--                   WHERE f.project_id = p.project_id AND array_length(f.ngram_hashes, 1) IS NOT NULL
--                   ORDER BY f.created_at DESC LIMIT 10) t
--          ) d
--    CROSS JOIN LATERAL autowriter.deskcore_check_drafts(p.project_id, d.j, 15) r;
--   → 看最后一行 Execution Time。11-11 跑本迁移之前是 3,677 毫秒; 跑完应在 1 秒以内
--     (同一组稿子照新写法跑是 341 毫秒)。还在 3 秒以上 = 计划器没走 GIN: 按下面回滚, 查重的结论不受影响。

-- ── ③ (可选) 看计划: 取候选的那一步照抄成普通 SQL ─────────────────────
--
--   EXPLAIN
--   WITH s AS MATERIALIZED (
--       SELECT f.ngram_hashes AS ng FROM autowriter.draft_fingerprints f
--        WHERE array_length(f.ngram_hashes, 1) IS NOT NULL ORDER BY f.created_at DESC LIMIT 1),
--   cand AS MATERIALIZED (
--       SELECT f.title, f.ngram_hashes AS hs, f.project_id AS pid
--         FROM autowriter.draft_fingerprints f
--        WHERE f.ngram_hashes && (SELECT ng FROM s))
--   SELECT count(*) FROM cand;
--   → 看 CTE cand 那一段: 是 Bitmap Index Scan on draft_fp_ngram_gin_idx, 不是 Seq Scan
--     (CTE s 那一段里的 Seq Scan 只是取一行样本, 不用管)。11-11 生产库: 成本 206, 同一句走顺序扫描是 627。

-- ── 回滚 ─────────────────────────────────────────────────────────────
-- 两条函数换回原来的写法(同样不碰数据, 查重的结论不变, 只是退回原来的速度), SQL Editor 里:
--   1. 整个重跑 migrations/008_embedding_model_isolation.sql —— deskcore_check_drafts 回到 008 那一版;
--   2. 只跑 migrations/005_deskcore_containment.sql 里从
--        CREATE OR REPLACE FUNCTION autowriter.deskcore_commit_fingerprints(
--      到它的 `$$;` 那一块。⚠️ 别整个重跑 005: 它会先 DROP deskcore_check_drafts、再建回没有按模型过滤的
--      那一版, 跑完还得再跑一遍 008。
