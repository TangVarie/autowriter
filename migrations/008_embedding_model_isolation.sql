-- ══════════════════════════════════════════════════════════════════════
-- 008 · 标题语义比对必须按 embedding 模型隔离
--
-- 病灶: `draft_fingerprints.embedding_model` 这一列从 migrations/001 起就存在,
-- 它的 COMMENT 写着"换 embedding 供应商时唯一的救命稻草"——
-- 而**从来没有任何代码真的用过它**。四路查重里最贵的那一路(标题语义余弦)
-- 一直是拿 `title_embedding IS NOT NULL` 当"可比", 不问是谁产的。
--
-- 2026-08-26 把 text-embedding-004(已下线)换成 gemini-embedding-001 时兑现:
-- 两个模型都能出 768 维, 于是**维度守卫拦不住**, 写库也不报错; 但两套向量
-- 空间毫不相干, 跨模型算出来的余弦是噪声。后果是双向的 ——
--
--   · 真重复的稿子算出来的相似度很低  → 硬闸放行, 重复稿进库;
--   · 完全无关的稿子算出来的相似度很高 → 硬闸误杀, 而理由是编的。
--
-- 而 `semantic_degraded` 会报 **false**: 历史行确实"有向量"。也就是说这一路
-- 不但失灵, 还在报告里说自己跑过了。(codex review · aw#65 P1)
--
-- ── 改了什么 ──────────────────────────────────────────────────────────
-- 只改标题语义那一段(③): 加一句按模型过滤。签名与返回列**一个字都没动**,
-- 所以是干净的 CREATE OR REPLACE, 不需要 DROP, 重复执行是 no-op。
--
-- 模型名走 `_rows` 里每条自带的 `embedding_model` 字段, **不加函数参数** ——
-- 加参数就要改签名, 而改签名意味着 DROP + 两版并存 + 调用方降级判断, 为了
-- 一个字符串不值得。同一批稿子本来就只可能出自同一个模型。
--
-- ⚠️ **没带 `embedding_model` 时保持旧行为**(比全部非空向量)。这是给"新库 +
-- 老调用方"留的兼容路径, 不是推荐用法 —— deskcore 的 store.check_drafts_sql
-- 从本迁移起一律带上它。之所以不选"没带就一条都不比": 那会让一次漏传变成
-- 静默的全量失灵, 比保持现状更坏。
--
-- ⚠️ 存量行怎么办: 老模型产的向量从此**不参与比对**(它们的 embedding_model
-- 不等于当前模型), 于是 `semantic_degraded` 会如实报 true。用
-- `python -m deskcore.cli reembed --project <id>` 拿当前模型重算, 重算完自动
-- 重新参与。**"少比并说出来" 好过 "混着比不说话"。**
--
-- 幂等: CREATE OR REPLACE + REVOKE/GRANT, 重复执行干净 no-op。
-- ══════════════════════════════════════════════════════════════════════

BEGIN;

SET LOCAL search_path TO autowriter, extensions, public;

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
            WITH cand AS (
                SELECT f.title, f.ngram_hashes AS hs
                  FROM autowriter.draft_fingerprints f
                 WHERE f.project_id = _project_id
                   AND f.ngram_hashes && ng
                   AND array_length(f.ngram_hashes, 1) IS NOT NULL
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

COMMIT;

-- ── 校验 (人工跑, 不在事务内) ────────────────────────────────────────
-- 函数体里必须出现按模型过滤那一句:
--   SELECT prosrc LIKE '%f.embedding_model = _model%'
--     FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
--    WHERE n.nspname='autowriter' AND p.proname='deskcore_check_drafts';
--   → 应为 t
--
-- 这个项目里有多少行的向量已经作废(需要 reembed):
--   SELECT embedding_model, count(*) FROM autowriter.draft_fingerprints
--    WHERE project_id = '<uuid>' AND title_embedding IS NOT NULL
--    GROUP BY 1;
--   → 除当前模型之外的每一组都要重算; NULL 那组是"来路不明", 同样要重算。
