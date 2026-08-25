-- ════════════════════════════════════════════════════════════════════
-- migrations/005_deskcore_containment.sql
-- ════════════════════════════════════════════════════════════════════
--
-- 修 check_drafts 正文那一路的两个问题。
-- 来源: 2026-08-23 全量审计 COR-014 (docs/audit-2026-08-23-full.md §0.5)
--
-- ⚠️ 本文件【替换】migrations/004 里的 deskcore_check_drafts —— 返回列多了三个
--    (best_c / c_title / c_sample), 所以 CREATE OR REPLACE 不够, 必须先 DROP。
--    004 仍然要先跑(它建的 deskcore_fingerprint_counts 本文件不动)。
--
-- ── 治的是什么 ────────────────────────────────────────────────────────
--
-- 问题一 · **估计有偏**。draft_fingerprints.ngram_hashes 存的是每篇各自最小的
-- 200 个 hash(bottom-k sketch), 不是完整的四字串集合。004 里直接对两个数组求
-- 交并比 —— 两篇长度差得多时, 它们的第 200 小值差着量级, 短稿 sketch 里那些
-- 落在长稿 sketch 之外的 hash 会被当成"长稿没有", 估计值系统性偏低。
-- 实测(3000 次合成对照)偏差最大 0.125, 而硬闸线就在 0.35。
--
-- 修法是 bottom-k 的标准估计式: 令 t = min(max(A), max(B)), **只在 [0,t] 上
-- 算**。因为 v ≤ t 时 "v ∈ 原集合" ⟺ "v ∈ sketch"(v 若在原集合里且 ≤ 该集合
-- 第 200 小值, 它必然在 sketch 里), 所以这个子域上两边的成员判定都是精确的;
-- 而 sha256 输出均匀, 子域是全域的均匀随机样本。误差降到最大 0.045。
-- **存量指纹不用重算** —— 只用已经存下来的两个数组。
--
-- ⚠️ 比较用的是**字符串序**。ngram_hashes 里是 sha256 前 16 位小写十六进制,
--    定长, 所以字典序 == 数值序。Python 侧 fingerprint.sketch_overlap 靠的是
--    同一条性质, 两边口径必须一致。
--
-- 问题二 · **指标选错了**。量化之后才看清: 估计改准了也抓不住"短稿逐字抄
-- 长稿"。因为那个形状下 Jaccard 的**真值**本来就小 —— 短稿整篇塞进长稿里
-- J = |A|/|B|, 280 字抄进 2800 字里真值就是 0.1, 再准也够不着 0.35。
-- Jaccard 天生被长度差稀释, 这不是精度问题。
--
-- 所以多回一路**包含度** |A∩B| / min(|A|,|B|) —— "短的那篇有多少比例出现在
-- 长的那篇里", 逐字抄袭时是 1.0, 与长度差无关。合成对照 7/7 全部拦下
-- (原来 1/7)。
--
-- ── c_sample 是干什么的 ───────────────────────────────────────────────
-- 子域里较小那边的元素个数, 也就是这次估计的**有效样本量**。调用方必须拿它
-- 当闸: 样本量太小时包含度会剧烈抖动(100 字草稿 vs 6000 字历史时子域只剩 4
-- 个元素, 完全无关的两篇也能撞出 1.0)。低于 ``CONTAIN_MIN_SAMPLE``(=15, 定义在
-- deskcore/fingerprint.py, 由 Python 侧作为 _contain_min_sample 传下来)就完全
-- 不发包含度信号 —— 这一路刻意 fail-open, 因为误杀正常稿子比漏检更难被发现。
-- 阈值本身是从零分布定的, 见 deskcore/fingerprint.py 里那张表。
--
-- ⚠️ 样本量是**候选资格**, 不是事后复核。够不着下限的命中根本不许参与
--    "取最大" —— 否则一条只撞上一个低位 hash 的无关稿以 c=1.0/样本量=1
--    当选, 把真正的抄袭源挤出决赛, 随后自己又因样本量不足被放行。
--
-- ── 为什么包含度也要 ORDER BY 自己那一路的最佳命中 ────────────────────
-- 抄袭源和"用词最像的那篇"经常不是同一条。共用一个 title 会让人对着一条根本
-- 没引发拒绝的稿子去改(codex review round-6 记过同款)。
--
-- 幂等: DROP ... IF EXISTS + CREATE, 重复执行是干净 no-op。
-- ⚠️ 与 db.py::CREATE_TABLES_SQL 的分工: 那边是 fresh install 的源头, 已同步
--    改成本文件这一版。migrations/README.md: 改函数必须两边都改。
-- ════════════════════════════════════════════════════════════════════

-- 返回列变了, CREATE OR REPLACE 会因签名冲突失败, 先 DROP。
-- OUT 参数不算函数标识的一部分, 所以这一句对 004 那版(7 列)和本文件这版(10 列)
-- 都有效 —— 重复执行时它把上一遍建的删掉, 下面再建一次, 干净 no-op。
DROP FUNCTION IF EXISTS autowriter.deskcore_check_drafts(UUID, JSONB);
-- 本文件自己那版(3 参)也要能被重复执行。加了 _contain_min_sample 之后
-- 参数表变了, CREATE OR REPLACE 顶不掉 2 参那版, 所以两个签名都 DROP。
DROP FUNCTION IF EXISTS autowriter.deskcore_check_drafts(UUID, JSONB, INT);

CREATE FUNCTION autowriter.deskcore_check_drafts(
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

COMMENT ON FUNCTION autowriter.deskcore_check_drafts(UUID, JSONB, INT) IS
    'check_drafts 的四路比对(开头精确 / 四字串 Jaccard / 四字串包含度 / 标题余弦), '
    '下推到库里一次算完。Jaccard 与包含度都走 bottom-k 的标准估计式(只在两个 '
    'sketch 都覆盖到的 hash 区间上算), 存量指纹不用重算。包含度专抓 Jaccard '
    '结构上抓不到的形状: 短稿整段照搬长稿。见 migrations/005 与审计 COR-014。';


-- ② 写入时的原子重查也要跟上 —— 否则两道闸的口径不一致
--
-- deskcore_commit_fingerprints 是 check 与 commit 之间那条竞态窗口的唯一防线:
-- 两个队友各自 check 时看到同一份旧指纹集、双双 pass, 然后各自 commit。
-- migrations/004 的文件头写着"两者的 Jaccard 口径必须一致", 而它用的正是被本
-- 文件换掉的那个有偏公式 —— 不一起改的话:
--   · 同一对稿子在 check 时判 reject、在 commit 时判 pass(反之亦然);
--   · 短稿照搬长稿这一类在写入侧【完全没有防线】。
--
-- 参数多了两个(阈值仍然由 Python 侧统一持有, 不在 SQL 里写死), 所以同样要先
-- DROP。旧的 4 参版本必须显式删掉 —— 留着会变成同名重载, 调用时报 ambiguous。
-- 旧的 4 参版要显式删掉 —— 留着会和新的 6 参版变成同名重载, 调用时报 ambiguous。
DROP FUNCTION IF EXISTS autowriter.deskcore_commit_fingerprints(UUID, JSONB, UUID, NUMERIC);
-- 而 6 参版这边必须是 CREATE **OR REPLACE**: 本文件重复执行时(或者在已经装了
-- 000_baseline 的新库上跑)那个函数已经在了, 裸 CREATE 会报 already exists。
-- migrations/README.md 承诺"重复执行必须是干净 no-op", CI 每次都会真跑两遍来验。
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
                SELECT c.title,
                       m.inter::numeric / NULLIF(m.uni, 0)     AS j,
                       m.inter::numeric / NULLIF(m.smaller, 0) AS c,
                       m.smaller::int                          AS smaller,
                       m.uni::int                              AS uni
                  FROM (
                      SELECT f.title, f.ngram_hashes AS hs
                        FROM autowriter.draft_fingerprints f
                       WHERE f.project_id = _project_id
                         AND f.ngram_hashes && ng
                         AND array_length(f.ngram_hashes, 1) IS NOT NULL
                  ) c
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

COMMENT ON FUNCTION autowriter.deskcore_commit_fingerprints(UUID, JSONB, UUID, NUMERIC, NUMERIC, INT) IS
    '定稿入库 + 同一事务内重查, 关掉 check 与 commit 之间的竞态窗口。四字串那一路 '
    '与 deskcore_check_drafts 口径一致(受限子域的 bottom-k 估计 + 包含度), '
    '否则同一对稿子会在两道闸上得到相反的结论。见 migrations/005 与审计 COR-014。';
