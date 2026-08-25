-- ════════════════════════════════════════════════════════════════════
-- migrations/004_deskcore_check_pushdown.sql
-- ════════════════════════════════════════════════════════════════════
--
-- 把 check_drafts 的三路比对下推到数据库, 外加 list_projects 的指纹计数。
-- 来源: 2026-08-23 全量审计 SUP-002 / SUP-004 / ROB-004 / ROB-011
--       (docs/audit-2026-08-23-full.md)
--
-- ── 治的是什么 ────────────────────────────────────────────────────────
-- deskcore.check_drafts 原来把【整个项目的指纹】拉进 Python 再逐对算:
--   · store.fingerprints 一次取回 4000 行 × 768 维 float + ngram 数组
--     —— 光是 PostgREST 回的 JSON 就是百 MB 级, 再被 _parse_pgvector 逐个
--     float() 成 307 万个 Python 对象(ROB-011: 容器 OOM 就是这么来的);
--   · 然后 D 篇草稿 × H 条历史逐对算余弦, 768 维纯 Python 循环
--     —— 10 × 4000 = 3070 万次乘加(SUP-002), 单次调用数十秒;
--   · 这数十秒是在 uvicorn 的线程池里同步跑的。池子占满, /health 跟着排队,
--     平台健康检查超时 → 重启容器 → 正在跑的调用全断(ROB-004)。
--     deskcore/app.py:242-246 的注释描述过同款事故。
--
-- 这三条是同一个根因的三种表现: 该在数据库里做的事搬到了 Python 里。
--
-- ── 为什么余弦这一路刻意【不走 ivfflat 索引】 ──────────────────────────
-- db.py:753-755 建了 draft_fp_embedding_idx (ivfflat, lists=100)。它是
-- **近似**最近邻: 默认 probes=1 只扫一个聚类桶, 真正的最近邻很可能不在里面。
-- 对"推荐相似内容"那种场景无所谓, 对**查重硬闸**是致命的 —— 漏掉的那条正是
-- 要拦下的重复稿, 而且不报错。叠加 project_id 过滤之后更糟: 索引先按向量取
-- 若干候选、再过滤项目, 命中项目的候选可能一条都不剩。
--
-- 所以这里 ORDER BY 的是【子查询算好的别名 sim DESC】而不是
-- `title_embedding <=> v`。pgvector 的索引只认后一种形态, 换成前者规划器
-- 必然走顺序扫描 —— 精确、可预期。代价是每个项目全扫一遍指纹, 但那是
-- 4000 行的 C 循环, 与原来 Python 侧的 3070 万次乘加不在一个量级。
-- (索引留着不动: 将来做"找相似选题"这类容忍近似的功能仍然用得上。)
--
-- ── 与 deskcore_commit_fingerprints 的关系 ────────────────────────────
-- 那个是【写入时】在同一事务里重查一遍关竞态窗口, 只做确定性信号。
-- 本函数是【写入前】的完整比对, 三路信号都做, 只读不写、不加锁。
-- 两者的 Jaccard 口径必须一致 —— 都是 GIN 的 && 粗筛 + 交并比精算。
--
-- 幂等: CREATE OR REPLACE FUNCTION 重复执行是干净 no-op。
-- ⚠️ 与 db.py::CREATE_TABLES_SQL 的分工: 那边是 fresh install 的源头, 已同步
--    加了这两个函数。migrations/README.md: 加函数必须两边都改。
-- ════════════════════════════════════════════════════════════════════

-- ① 查重比对: 每篇草稿回一行三路信号的最佳命中
--
-- ⚠️ 先 DROP 再 CREATE, 不能只写 CREATE OR REPLACE。migrations/005 把这个函数的
--    返回列从 7 个加到 10 个, 而 000_baseline.sql 里存的是 005 之后那一版 ——
--    于是"空库 → 000 → 001..005 依序跑一遍"这个流程走到这里会报
--    `cannot change return type of existing function`。
--    (OUT 参数不算函数标识的一部分, 所以这个 DROP 对新旧两版都有效。)
--    这个流程不是假想: migrations/README.md 承诺过"重复执行必须是干净 no-op",
--    而 tests/sql_parity_check.py 每次 CI 都会真跑一遍来验这句话。
DROP FUNCTION IF EXISTS autowriter.deskcore_check_drafts(UUID, JSONB);
CREATE OR REPLACE FUNCTION autowriter.deskcore_check_drafts(
    _project_id UUID,
    -- [{opening_hash, ngram_hashes, title_embedding}, ...]
    -- title_embedding 可以是 null(本批算不出向量), 那一路直接跳过。
    _rows       JSONB
)
RETURNS TABLE(
    idx          INT,
    best_sim     NUMERIC,   -- 标题语义余弦的最大值; 没有可比向量时 0
    sim_title    TEXT,
    best_j       NUMERIC,   -- 正文四字串 Jaccard 的最大值
    j_title      TEXT,
    open_exact   BOOLEAN,   -- 正文开头精确撞车
    open_title   TEXT
)
LANGUAGE plpgsql
STABLE
SET search_path = pg_catalog, extensions   -- ::vector 需要 extensions
AS $$
DECLARE
    r    JSONB;
    i    INT := -1;
    ng   TEXT[];
    oh   TEXT;
    v    vector;
BEGIN
    FOR r IN SELECT * FROM jsonb_array_elements(_rows) LOOP
        i := i + 1;
        idx := i;
        best_sim := 0; sim_title := NULL;
        best_j := 0;   j_title := NULL;
        open_exact := FALSE; open_title := NULL;

        oh := NULLIF(r->>'opening_hash', '');
        SELECT COALESCE(array_agg(x), '{}') INTO ng
          FROM jsonb_array_elements_text(COALESCE(r->'ngram_hashes','[]'::jsonb)) x;
        v := CASE
                WHEN r->'title_embedding' IS NULL
                  OR jsonb_typeof(r->'title_embedding') = 'null'
                THEN NULL
                ELSE (r->>'title_embedding')::vector
             END;

        -- ① 开头精确撞车。oh 为空 = 这篇没有正文开头, 空 == 空【不算撞车】
        --    (否则所有 title-only 的稿子会互相"精确撞车", 见 fp.opening_hash)。
        IF oh IS NOT NULL THEN
            SELECT f.title INTO open_title
              FROM autowriter.draft_fingerprints f
             WHERE f.project_id = _project_id AND f.opening_hash = oh
             LIMIT 1;
            open_exact := open_title IS NOT NULL;
        END IF;

        -- ② 四字串 Jaccard。GIN 的 && 先粗筛, 只对有交集的行精算 ——
        --    没有交集的行 Jaccard 必为 0, 不可能成为最大值。
        IF array_length(ng, 1) IS NOT NULL THEN
            SELECT t.title, t.j INTO j_title, best_j
              FROM (
                SELECT f.title,
                       (SELECT count(*) FROM (SELECT unnest(ng) INTERSECT SELECT unnest(f.ngram_hashes)) s)::numeric
                       / NULLIF((SELECT count(*) FROM (SELECT unnest(ng) UNION SELECT unnest(f.ngram_hashes)) u), 0) AS j
                  FROM autowriter.draft_fingerprints f
                 WHERE f.project_id = _project_id
                   AND f.ngram_hashes && ng
              ) t
             WHERE t.j IS NOT NULL
             ORDER BY t.j DESC, t.title
             LIMIT 1;
            best_j := COALESCE(best_j, 0);
        END IF;

        -- ③ 标题语义余弦。见文件头: ORDER BY 的是别名 sim, 不是距离算子 ——
        --    刻意绕开 ivfflat 的近似最近邻。
        IF v IS NOT NULL THEN
            SELECT t.title, t.sim INTO sim_title, best_sim
              FROM (
                SELECT f.title, 1 - (f.title_embedding <=> v) AS sim
                  FROM autowriter.draft_fingerprints f
                 WHERE f.project_id = _project_id
                   AND f.title_embedding IS NOT NULL
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

REVOKE ALL ON FUNCTION autowriter.deskcore_check_drafts(UUID, JSONB) FROM PUBLIC;
REVOKE ALL ON FUNCTION autowriter.deskcore_check_drafts(UUID, JSONB) FROM anon;
REVOKE ALL ON FUNCTION autowriter.deskcore_check_drafts(UUID, JSONB) FROM authenticated;
GRANT EXECUTE ON FUNCTION autowriter.deskcore_check_drafts(UUID, JSONB) TO service_role;

COMMENT ON FUNCTION autowriter.deskcore_check_drafts(UUID, JSONB) IS
    'check_drafts 的三路比对(开头精确 / 四字串 Jaccard / 标题余弦), 下推到库里 '
    '一次算完。余弦刻意走顺序扫描而不是 ivfflat —— 近似最近邻会静默漏掉真正的 '
    '重复稿。见 migrations/004。';


-- ② 指纹计数: 一次拿一批项目的条数, 替掉 list_projects 的每项目一次 count
--    (审计 SUP-004: 40 个项目 = 41 次往返, 而这是模型最常调的第一个工具)。
CREATE OR REPLACE FUNCTION autowriter.deskcore_fingerprint_counts(_project_ids UUID[])
RETURNS TABLE(project_id UUID, n BIGINT)
LANGUAGE sql
STABLE
SET search_path = pg_catalog, extensions
AS $$
    SELECT f.project_id, count(*)::bigint
      FROM autowriter.draft_fingerprints f
     WHERE f.project_id = ANY(_project_ids)
     GROUP BY f.project_id;
$$;

REVOKE ALL ON FUNCTION autowriter.deskcore_fingerprint_counts(UUID[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION autowriter.deskcore_fingerprint_counts(UUID[]) FROM anon;
REVOKE ALL ON FUNCTION autowriter.deskcore_fingerprint_counts(UUID[]) FROM authenticated;
GRANT EXECUTE ON FUNCTION autowriter.deskcore_fingerprint_counts(UUID[]) TO service_role;

COMMENT ON FUNCTION autowriter.deskcore_fingerprint_counts(UUID[]) IS
    '一次返回多个项目的指纹条数。没有它 list_projects 是 N+1 —— PostgREST '
    '不会 GROUP BY, 只能每个项目发一次 count=exact。见 migrations/004。';
