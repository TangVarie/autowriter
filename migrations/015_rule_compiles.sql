-- ══════════════════════════════════════════════════════════════════════
-- 015 · 规则编译结果的缓存(2026-10-20, D-121)
--
-- 为什么
-- ──────
-- 10-20 生产诊断: 规则全部写成文字塞进写手模型的简报, 守不守全凭注意力。改成「写完再查」: 每条规则先由
-- Jev 的 POST /compile_rules 编译一次 —— 能用代码查的编成 code(形状同 deskcore/rulecheck.py), 要读懂内容的编成
-- 1–3 道是非题, 查不了的记 not_checkable。写手写完调 check_rules, 用编好的结果逐篇查, 只作建议(hard → 必须改,
-- soft → 建议改), 不拦存档(owner 10-20 拍板, D-121)。
--
-- 编译一次最坏要 6 分钟、还花钱, 所以结果按 (规则 id, sha) 缓存: sha 是规则原文(+ applicability)的 sha256 前 12 位,
-- 和 Jev 的 rules.rule_sha 同一个算法。原文或适用范围一改 sha 就变, 后台重编; 没改的永远不再花钱。
-- 缓存键是两样一起, 不是只按 sha: 题号里带规则 id 的缩写, 两条原文相同、id 不同的规则只按 sha 复用会把前一条的
-- 题号带进后一条(同一份题库里题号重复, /judge_draft 422)。
--
-- 这次做什么
-- ──────────
-- autowriter.rule_compiles: 一条规则的一个版本一行。result 是 /compile_rules 回的那一条(verdict / code /
-- questions / coverage / uncovered / why), 带 error 的不存(下次再编)。规则删了跟着删(ON DELETE CASCADE)。
-- ⚠️ 不复用 memories.rule_payload: 那两列是人手设的代码检查(record_rule 的 check, 10-20 第二批 B), 编译结果
-- 写进去会盖掉人设的、也会被 validator.py 当成 AW 生成链路的硬检查。
--
-- 幂等: 表 IF NOT EXISTS; GRANT / RLS 重复执行是 no-op。⚠️ 同样的表也进了 000_baseline.sql。
-- ══════════════════════════════════════════════════════════════════════

BEGIN;

CREATE TABLE IF NOT EXISTS autowriter.rule_compiles (
    memory_id    UUID NOT NULL REFERENCES autowriter.memories(id) ON DELETE CASCADE,
    sha          TEXT NOT NULL,
    result       JSONB NOT NULL,
    model        TEXT,
    compiled_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (memory_id, sha)
);
-- 建出来 ≠ 能访问(migrations/README): service_role 不绕表级 GRANT。只给 service_role —— 读写都只有 deskcore。
GRANT SELECT, INSERT, UPDATE, DELETE ON autowriter.rule_compiles TO service_role;
ALTER TABLE autowriter.rule_compiles ENABLE ROW LEVEL SECURITY;

COMMIT;
