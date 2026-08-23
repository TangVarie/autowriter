-- ════════════════════════════════════════════════════════════════════
-- migrations/001_deskcore.sql
-- ════════════════════════════════════════════════════════════════════
--
-- deskcore(写作台内核 MCP 服务)的四张表 + items.updated_at
-- 决策: truth-vault DECISIONS.md D-041 / docs/10-sister-repo-followups.md R-034
--
-- 背景:
--   Streamlit 界面停用(app.py 5560 行、每次交互全量重跑 = 慢的根源), 但库里
--   几年的积累(40 项目 / 269 条记忆 / 103 条已确认负例 / 调校笔记)原样保留,
--   写作能力经 deskcore 挂到 WorkBuddy / Claude Code / CodeBuddy。
--
--   本迁移只【补齐】外置后新增的能力, 现有 8 张表一行不动。
--
-- 每张表对应一个已确诊的根因:
--
--   angle_ledger       ← 跨批次没有"哪些角度组合用过"的持久记录。
--                        generator._assign_slot_coordinates(:1301) 只在单批内
--                        去重, 且已被移除成死代码(:1576-1584)。模型是无状态的,
--                        光靠提示词让它"注意不要重复"做不到。
--
--   draft_fingerprints ← 查重池 queue_embeddings 是 worker 进程内的内存字典
--                        (app.py:1024), 进程一重启就空; 且只比标题。
--                        本表是持久 / 全量 / 跨人 / 跨批次的查重底座。
--
--   user_calibration_notes ← projects.calibration_notes 是项目级单份, 承载不了
--                        「项目规则团队共享 + 个人风格私有」的分层。项目级那份
--                        保留为共享基线, 本表是个人叠加层。
--
--   style_edits        ← 手动精修 diff 的原始存放处。笔记是导出物、diff 才是
--                        事实, 换蒸馏 prompt 时要能重算。
--
--   items.updated_at   ← TV scripts/sync_autowriter_decisions_to_prepublish.py:36-40
--                        记的缺陷: 没这列只能按 created_at 过滤, 迟到的人工
--                        决策会漏收(--since-days 从 90 改 365 是补丁)。
--
-- RLS: 全部 enable 不建 policy = service_role only。deskcore 持 service_role,
--      隔离口径由服务端自己执行(共享层按 project_id 读全量、个人层带 user_id)。
--      这是刻意的 —— 「项目规则团队共享」要求跨 owner 读规则, RLS 的
--      user_id = auth.uid() 做不到。
--
-- 幂等: 重复执行不报错。
--
-- 回滚:
--   DROP TABLE IF EXISTS draft_fingerprints CASCADE;
--   DROP TABLE IF EXISTS angle_ledger CASCADE;
--   DROP TABLE IF EXISTS user_calibration_notes CASCADE;
--   DROP TABLE IF EXISTS style_edits CASCADE;
--   ALTER TABLE items DROP COLUMN IF EXISTS updated_at;
--   DROP FUNCTION IF EXISTS _deskcore_touch_updated_at();
--   DROP FUNCTION IF EXISTS autowriter.deskcore_reserve_angles(UUID,JSONB,UUID,INT,INT);
--   DROP FUNCTION IF EXISTS autowriter.deskcore_commit_fingerprints(UUID,JSONB,UUID,NUMERIC);
-- ════════════════════════════════════════════════════════════════════

BEGIN;

SET LOCAL search_path TO autowriter, extensions, public;

-- ⚠️ WITH SCHEMA extensions 不能省。上面 SET LOCAL search_path 把 autowriter 放在
-- 首位, 而 CREATE EXTENSION 不带 SCHEMA 时【装进 search_path 的第一个 schema】——
-- 实测(PG 16): search_path = autowriter, extensions, public → pg_trgm 装进了 autowriter。
-- 真装错了的后果不是这里报错, 而是下面 deskcore_commit_fingerprints 里那句
-- ::vector 在【运行时】解析不到类型 —— 定稿入库整条路挂掉, 而且要等到真有人
-- 提交稿子才发现。Supabase 预装在 extensions, IF NOT EXISTS 会跳过, 所以现有库
-- 看不出问题; 全新库才踩。(codex aw#57 review)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA extensions;
CREATE EXTENSION IF NOT EXISTS vector      WITH SCHEMA extensions;

-- 兜底: 万一这个库早就把 vector 装在别处(IF NOT EXISTS 会静默跳过上面那句),
-- 在【迁移时】就炸掉并说清怎么办, 而不是留给运行时。
DO $guard$
DECLARE ns TEXT;
BEGIN
    SELECT n.nspname INTO ns
      FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
     WHERE e.extname = 'vector';
    IF ns IS DISTINCT FROM 'extensions' THEN
        RAISE EXCEPTION
            'vector 扩展装在 %, 但两个 deskcore RPC 的 search_path 固定为 '
            '(pg_catalog, extensions) —— ::vector 会在运行时解析不到。'
            '请先 ALTER EXTENSION vector SET SCHEMA extensions; 再重跑本迁移。', ns;
    END IF;
END
$guard$;

-- ── 1. 发牌台账 (共享层) ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS autowriter.angle_ledger (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id          UUID NOT NULL REFERENCES autowriter.projects(id) ON DELETE CASCADE,
    angle_key           TEXT NOT NULL,
    dims                JSONB NOT NULL DEFAULT '{}'::jsonb,
    drawn_by            UUID,
    drawn_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    consumed_version_id UUID,
    consumed_at         TIMESTAMPTZ
);
COMMENT ON COLUMN autowriter.angle_ledger.angle_key IS
    'dims 的规范化指纹(只取四个主维度; 情绪强度/时效/词感是叠加项不进 key, 否则换个词感就被当成没用过)';
COMMENT ON COLUMN autowriter.angle_ledger.consumed_version_id IS
    'NULL=抽了未用(占位, 1 天后自然过期); 非 NULL=已产出成稿, 按 avoid_days 避重';
CREATE INDEX IF NOT EXISTS angle_ledger_project_key_idx
    ON autowriter.angle_ledger (project_id, angle_key);
CREATE INDEX IF NOT EXISTS angle_ledger_project_drawn_idx
    ON autowriter.angle_ledger (project_id, drawn_at DESC);
ALTER TABLE autowriter.angle_ledger ENABLE ROW LEVEL SECURITY;

-- ── 2. 成稿指纹库 (共享层) ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS autowriter.draft_fingerprints (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id      UUID NOT NULL REFERENCES autowriter.projects(id) ON DELETE CASCADE,
    version_id      UUID,
    user_id         UUID,
    title           TEXT NOT NULL DEFAULT '',
    opening         TEXT NOT NULL DEFAULT '',
    title_embedding vector(768),
    embedding_model TEXT,
    opening_hash    TEXT,
    ngram_hashes    TEXT[] NOT NULL DEFAULT '{}',
    angle_key       TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- 已有库补列(本迁移可能在表已存在时重跑)
ALTER TABLE autowriter.draft_fingerprints
    ADD COLUMN IF NOT EXISTS embedding_model TEXT;
COMMENT ON COLUMN autowriter.draft_fingerprints.title_embedding IS
    'Gemini text-embedding-004 768d, 与 versions.embedding 同模型可互比';
COMMENT ON COLUMN autowriter.draft_fingerprints.embedding_model IS
    '产出 title_embedding 的模型名(如 text-embedding-004)。NULL = 这行没有向量。'
    '⚠️ 换 embedding 供应商时唯一的救命稻草: 跨模型算余弦相似度出来的数是垃圾, '
    '而且【不报错】—— 没有这一列, 迁移期新旧向量混在一张表里, 查重会安静地失灵, '
    '只能靠"写入时间早于某某"去猜哪些是旧的。有了它, 换模型从停机重算变成增量迁移: '
    '比对时只在同模型内比, 两套并存互不污染, 新的补齐了再退役旧的。'
    '这一列必须在写第一条向量之前就存在, 补写的成本高得多。';
CREATE INDEX IF NOT EXISTS draft_fp_embedding_model_idx
    ON autowriter.draft_fingerprints (project_id, embedding_model)
    WHERE title_embedding IS NOT NULL;
COMMENT ON COLUMN autowriter.draft_fingerprints.opening_hash IS
    '正文首个非空行前 25 字规范化后 sha256 前 16 位 —— 标题换了也能抓';
COMMENT ON COLUMN autowriter.draft_fingerprints.ngram_hashes IS
    '正文四字串 shingle 的 hash 采样 —— 抓换了词还是同一篇的换皮改写';
CREATE INDEX IF NOT EXISTS draft_fp_project_created_idx
    ON autowriter.draft_fingerprints (project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS draft_fp_opening_hash_idx
    ON autowriter.draft_fingerprints (project_id, opening_hash)
    WHERE opening_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS draft_fp_ngram_gin_idx
    ON autowriter.draft_fingerprints USING GIN (ngram_hashes);
CREATE INDEX IF NOT EXISTS draft_fp_embedding_idx
    ON autowriter.draft_fingerprints USING ivfflat (title_embedding vector_cosine_ops)
    WITH (lists = 100);
ALTER TABLE autowriter.draft_fingerprints ENABLE ROW LEVEL SECURITY;

-- ── 3. 个人调校笔记 (私有层) ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS autowriter.user_calibration_notes (
    project_id  UUID NOT NULL REFERENCES autowriter.projects(id) ON DELETE CASCADE,
    user_id     UUID NOT NULL,
    notes       TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project_id, user_id)
);
ALTER TABLE autowriter.user_calibration_notes ENABLE ROW LEVEL SECURITY;

-- ── 4. 手动精修 diff (私有层) ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS autowriter.style_edits (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id  UUID NOT NULL REFERENCES autowriter.projects(id) ON DELETE CASCADE,
    user_id     UUID NOT NULL,
    ai_title    TEXT NOT NULL DEFAULT '',
    ai_body     TEXT NOT NULL DEFAULT '',
    my_title    TEXT NOT NULL DEFAULT '',
    my_body     TEXT NOT NULL DEFAULT '',
    note        TEXT,
    distilled   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
COMMENT ON COLUMN autowriter.style_edits.distilled IS
    '是否已蒸馏进 user_calibration_notes。想整体重算就把这列置回 FALSE';
CREATE INDEX IF NOT EXISTS style_edits_owner_idx
    ON autowriter.style_edits (project_id, user_id, created_at DESC);
ALTER TABLE autowriter.style_edits ENABLE ROW LEVEL SECURITY;

-- ── 5. items.updated_at ──────────────────────────────────────────────
-- 回填成 created_at 而不是 NOW(), 免得历史行全部看起来"刚改过"。
ALTER TABLE autowriter.items ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
UPDATE autowriter.items SET updated_at = created_at WHERE updated_at IS NULL;
ALTER TABLE autowriter.items ALTER COLUMN updated_at SET DEFAULT NOW();
COMMENT ON COLUMN autowriter.items.updated_at IS
    '人工决策(status/example_label)最后变更时间。补"只能按 created_at 过滤导致漏收迟到决策"的缺陷';
CREATE INDEX IF NOT EXISTS items_updated_at_idx ON autowriter.items (updated_at DESC);

-- ── 6. updated_at 触发器 ─────────────────────────────────────────────
-- DEFAULT 只在 INSERT 生效; upsert 的 DO UPDATE 分支必须靠触发器刷新。
CREATE OR REPLACE FUNCTION autowriter._deskcore_touch_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS deskcore_user_calib_updated_at ON autowriter.user_calibration_notes;
CREATE TRIGGER deskcore_user_calib_updated_at
    BEFORE UPDATE ON autowriter.user_calibration_notes
    FOR EACH ROW EXECUTE FUNCTION autowriter._deskcore_touch_updated_at();

-- ⚠️ items 的触发器带 WHEN 条件, 只在【人工决策列】真的变了时才刷时间戳。
-- 这一列的定义就是"人工决策(status/example_label)最后变更时间", 而 items 上
-- 还有别的高频写入路径: save_feedback_draft() / save_manual_edit_draft()
-- (db.py:1644/1676) 在用户【打字过程中】就往同一张表写草稿。无条件触发的话,
-- 每敲一次键都会把 updated_at 推到现在, TV 的增量同步
-- (sync_autowriter_decisions_to_prepublish.py) 会把一堆决策没变的行当成"刚
-- 改过"反复捞回去 —— 而真正迟到的那些决策依旧混在噪声里, 这一列就白加了。
-- IS DISTINCT FROM 而不是 <>: 决策列可以是 NULL(未决策/未标注), <> 遇 NULL
-- 得 NULL 即不触发, 于是"第一次标正例"(NULL → 'positive')反而不刷时间戳。
-- (codex review round-5 P2)
DROP TRIGGER IF EXISTS deskcore_items_updated_at ON autowriter.items;
CREATE TRIGGER deskcore_items_updated_at
    BEFORE UPDATE ON autowriter.items
    FOR EACH ROW
    WHEN (OLD.status IS DISTINCT FROM NEW.status
       OR OLD.example_label IS DISTINCT FROM NEW.example_label)
    EXECUTE FUNCTION autowriter._deskcore_touch_updated_at();

-- ── deskcore 发牌的原子预留 ──────────────────────────────────────────
-- 为什么需要它: draw_angles 原本是"读避重集 → Python 里挑 → 插入"三步。
-- 两个队友同时给同一项目发牌, 会各自读到"这个组合没用过"、各自插入成功,
-- 同一个角度被两批同时用掉 —— 而两边都报告成功, 跨批次唯一性的承诺破了。
-- 台账上只有非唯一索引, 拦不住。
--
-- 这里把三步收进一个事务, 用事务级 advisory lock 把【同一项目】的发牌串行化
-- (不同项目互不阻塞, 事务结束自动释放)。与 claim_one_job 的
-- FOR UPDATE SKIP LOCKED 是同一思路: 并发正确性交给数据库, 不靠应用层自觉。
--
-- _candidates: [{"angle_key": "...", "dims": {...}}, ...] 按优先级排好序,
--              过量供给(调用方给远多于 _want 的候选), 函数取前 _want 个可用的。
CREATE OR REPLACE FUNCTION autowriter.deskcore_reserve_angles(
    _project_id UUID,
    _candidates JSONB,
    _drawn_by   UUID,
    _want       INT,
    _avoid_days INT DEFAULT 30
)
RETURNS TABLE(reserved_key TEXT, reserved_dims JSONB)
LANGUAGE plpgsql
-- 固定 search_path: 不设的话 Supabase advisor 报 function_search_path_mutable(WARN),
-- 且调用方能通过改 search_path 影响函数内未限定名字的解析。
-- 不能用 '' —— 下面 commit_fingerprints 有 ::vector 转型, vector 类型在 extensions;
-- 表名本来就全限定成 autowriter.*, 所以 pg_catalog + extensions 就够。
-- (2026-08-23 分支库验证: 加之前 2 条 WARN, 加之后归零, 两个 RPC 功能实测不受影响)
SET search_path = pg_catalog, extensions
AS $$
DECLARE
    cand  JSONB;
    k     TEXT;
    taken INT := 0;
BEGIN
    IF _want <= 0 THEN
        RETURN;
    END IF;

    PERFORM pg_advisory_xact_lock(hashtext('deskcore_draw:' || _project_id::text));

    FOR cand IN SELECT * FROM jsonb_array_elements(_candidates) LOOP
        EXIT WHEN taken >= _want;
        k := cand->>'angle_key';
        CONTINUE WHEN k IS NULL OR k = '';

        -- 已产出成稿且仍在避重窗内 → 跳过。按 consumed_at 而不是 drawn_at:
        -- 审稿定稿常拖几天, 按 drawn_at 会让刚定稿的角度立刻可被重用。
        CONTINUE WHEN EXISTS (
            SELECT 1 FROM autowriter.angle_ledger al
             WHERE al.project_id = _project_id
               AND al.angle_key  = k
               AND al.consumed_version_id IS NOT NULL
               AND al.consumed_at >= now() - make_interval(days => _avoid_days));

        -- 抽了还没写的占位, 1 天内 → 跳过。占位不该长期占坑, 否则连点几次
        -- 发牌就把组合空间锁死。
        CONTINUE WHEN EXISTS (
            SELECT 1 FROM autowriter.angle_ledger al
             WHERE al.project_id = _project_id
               AND al.angle_key  = k
               AND al.consumed_version_id IS NULL
               AND al.drawn_at >= now() - interval '1 day');

        INSERT INTO autowriter.angle_ledger (project_id, angle_key, dims, drawn_by)
        VALUES (_project_id, k, COALESCE(cand->'dims', '{}'::jsonb), _drawn_by);

        taken         := taken + 1;
        reserved_key  := k;
        reserved_dims := COALESCE(cand->'dims', '{}'::jsonb);
        RETURN NEXT;
    END LOOP;
END;
$$;

-- 只给 service_role。deskcore 是唯一调用方; 不加约束的话任何能访问 PostgREST
-- RPC 的角色都能往别人项目的台账里塞行(同 claim_one_job 的权限处理)。
REVOKE ALL ON FUNCTION autowriter.deskcore_reserve_angles(UUID, JSONB, UUID, INT, INT) FROM PUBLIC;
REVOKE ALL ON FUNCTION autowriter.deskcore_reserve_angles(UUID, JSONB, UUID, INT, INT) FROM anon;
REVOKE ALL ON FUNCTION autowriter.deskcore_reserve_angles(UUID, JSONB, UUID, INT, INT) FROM authenticated;
GRANT EXECUTE ON FUNCTION autowriter.deskcore_reserve_angles(UUID, JSONB, UUID, INT, INT) TO service_role;
-- ── deskcore 定稿入库的原子查重 ────────────────────────────────────────
-- 为什么需要: check_drafts 和 commit_drafts 是两次独立调用。两个队友各自 check
-- 时都看到同一份旧指纹集、双双 pass, 然后各自 commit —— 两篇撞车的稿子都进了库,
-- 跨人硬闸形同虚设。发牌那边已经用 advisory lock 串行化了, 这边不能留着。
--
-- 本函数在同一个事务里【重新查一遍 + 插入】, 用与 draw 相同的 project 级
-- advisory lock 串行化。只做【确定性】信号(开头精确 + 四字串 Jaccard)——
-- 标题语义那一路要 pgvector 距离算子, 且历史行可能没有向量, 留在 Python 侧的
-- check_drafts 里做; 这里挡住的是竞态窗口里最可能撞的那两类。
--
-- 返回每条的结果: inserted / rejected + 撞了谁。调用方据此告诉用户哪几条要重写。
CREATE OR REPLACE FUNCTION autowriter.deskcore_commit_fingerprints(
    _project_id UUID,
    _rows       JSONB,        -- [{title,opening,opening_hash,ngram_hashes,title_embedding,version_id,angle_key}, ...]
    _user_id    UUID,
    _ngram_hard NUMERIC DEFAULT 0.35
)
RETURNS TABLE(idx INT, status TEXT, collided_with TEXT, detail TEXT)
LANGUAGE plpgsql
SET search_path = pg_catalog, extensions   -- 见上; ::vector 需要 extensions
AS $$
DECLARE
    r        JSONB;
    i        INT := -1;
    ng       TEXT[];
    oh       TEXT;
    hit      RECORD;
    best_j   NUMERIC;
    best_t   TEXT;
    open_hit BOOLEAN;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('deskcore_draw:' || _project_id::text));

    FOR r IN SELECT * FROM jsonb_array_elements(_rows) LOOP
        i := i + 1;
        oh := r->>'opening_hash';
        SELECT COALESCE(array_agg(x), '{}') INTO ng
          FROM jsonb_array_elements_text(COALESCE(r->'ngram_hashes','[]'::jsonb)) x;

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

        -- ② 四字串重合。先用 GIN 的 && 粗筛, 只对有交集的行算精确 Jaccard。
        best_j := 0; best_t := NULL;
        IF array_length(ng, 1) IS NOT NULL THEN
            FOR hit IN
                SELECT f.title,
                       (SELECT count(*) FROM (SELECT unnest(ng) INTERSECT SELECT unnest(f.ngram_hashes)) s)::numeric
                       / NULLIF((SELECT count(*) FROM (SELECT unnest(ng) UNION SELECT unnest(f.ngram_hashes)) u), 0) AS j
                  FROM autowriter.draft_fingerprints f
                 WHERE f.project_id = _project_id
                   AND f.ngram_hashes && ng
            LOOP
                IF hit.j IS NOT NULL AND hit.j > best_j THEN
                    best_j := hit.j; best_t := hit.title;
                END IF;
            END LOOP;
        END IF;
        IF best_j >= _ngram_hard THEN
            idx := i; status := 'rejected'; collided_with := best_t;
            detail := format('正文与库中已有稿件大面积重合(四字串 Jaccard=%s)', round(best_j, 3));
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
        idx := i; status := 'inserted'; collided_with := NULL; detail := NULL;
        RETURN NEXT;
    END LOOP;
END;
$$;

REVOKE ALL ON FUNCTION autowriter.deskcore_commit_fingerprints(UUID, JSONB, UUID, NUMERIC) FROM PUBLIC;
REVOKE ALL ON FUNCTION autowriter.deskcore_commit_fingerprints(UUID, JSONB, UUID, NUMERIC) FROM anon;
REVOKE ALL ON FUNCTION autowriter.deskcore_commit_fingerprints(UUID, JSONB, UUID, NUMERIC) FROM authenticated;
GRANT EXECUTE ON FUNCTION autowriter.deskcore_commit_fingerprints(UUID, JSONB, UUID, NUMERIC) TO service_role;

COMMIT;

-- ── 校验 (人工跑, 不在事务内) ────────────────────────────────────────
-- SELECT table_name FROM information_schema.tables WHERE table_schema='autowriter'
--   AND table_name IN ('angle_ledger','draft_fingerprints','user_calibration_notes','style_edits');
--   → 应返回 4 行
-- SELECT count(*) FROM autowriter.items WHERE updated_at IS NULL;   → 应为 0
