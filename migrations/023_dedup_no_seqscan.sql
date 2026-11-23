-- 023 · 查重的两个函数不许走全表顺序扫描（2026-11-23）
--
-- 现象：写作台 check_drafts / score_drafts 报 APIError code=57014（语句超时）。
--   PostgREST 每条请求上限 8 秒（authenticator 的 statement_timeout），
--   查重函数一批 5 篇要十几秒，超时被掐断；写手那边照协议停下、交付不了。
--   11-18、11-22、11-23 都出现过，涉及三个项目。
--
-- 原因：migrations/019 把取候选写成「先走四字串的 GIN 索引、再筛项目」：
--     cand AS MATERIALIZED (... WHERE f.ngram_hashes && (SELECT ng))
--   指纹表长到约 8,000 行（160 MB，大数组多在 TOAST 里）之后，计划器估
--   全表顺序扫描（成本约 647）比走 draft_fp_ngram_gin_idx（约 741）便宜，
--   可它没算逐行解压数组的开销：实际顺序扫描一篇约 3.5 秒，走索引约 0.16 秒。
--   11-23 用生产库同一项目最近 5 条指纹复现：19.3 秒 → 关掉顺序扫描后 0.54 秒，
--   四路结果逐项相同。
--
-- 做法：只给这两个函数加一个计划器开关 enable_seqscan = off（只在函数执行期间
--   生效）。函数体、签名、返回列、判定、阈值、授权、数据都不动；函数里别的几步
--   （开头精确撞车、标题语义）本来就走索引。真没有索引可用的地方计划器照样能
--   顺序扫描（只是估价变高），不会报错。
--
-- 用函数属主跑（SQL Editor 的 postgres）。可重跑。部署前后什么时候跑都行，Python 不改。
--
-- ⚠️ 以后谁再 CREATE OR REPLACE 这两个函数（照 019 / 基线整块复制、或照 019 文件末尾的回滚重跑 005 / 008），
--   PostgreSQL 会把函数的配置换成新语句里写的那几项，这个开关就悄悄没了：结论不变，查重又会超时。
--   重建之后要再跑一次本文件；新迁移里重建它们的，同一个文件里要再加这个开关
--   （tests/test_dedup_no_seqscan.py 和 tests/sql_parity_check.py 都会查）。
--
-- 核计划时注意：SQL Editor 自己的会话里顺序扫描是开着的，把取候选那一步单独拿出来 EXPLAIN 会看到
--   Seq Scan，那不代表函数里也是。要看就在 begin read only; set local enable_seqscan = off; … rollback; 里看，
--   或者直接拿真的稿子调一次函数看用时（019 文件末尾的 ②）。
--
-- 跑完核（应当两行都带 enable_seqscan=off）：
--   select p.proname, p.proconfig from pg_proc p join pg_namespace n on n.oid = p.pronamespace
--    where n.nspname = 'autowriter'
--      and p.proname in ('deskcore_check_drafts', 'deskcore_commit_fingerprints');
--
-- 回滚：
--   alter function autowriter.deskcore_check_drafts(uuid, jsonb, integer) reset enable_seqscan;
--   alter function autowriter.deskcore_commit_fingerprints(uuid, jsonb, uuid, numeric, numeric, integer) reset enable_seqscan;

begin;

alter function autowriter.deskcore_check_drafts(uuid, jsonb, integer)
    set enable_seqscan = off;

alter function autowriter.deskcore_commit_fingerprints(uuid, jsonb, uuid, numeric, numeric, integer)
    set enable_seqscan = off;

commit;
