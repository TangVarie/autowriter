# migrations/

给**已存在的库**打的增量 SQL。

## 和 `db.py::CREATE_TABLES_SQL` 的关系

| | 作用 | 何时跑 |
|---|---|---|
| `db.py::CREATE_TABLES_SQL` | schema 的**源头**，fresh install 一把建全 | 新环境初始化 |
| `migrations/*.sql` | 增量，把已有的库升到和源头一致 | 已有库升级 |

**加表/加列必须两边都改。** 只改迁移 → 新环境缺东西；只改 `CREATE_TABLES_SQL`
→ 生产库升不上去。两边都写成幂等（`IF NOT EXISTS` / `CREATE OR REPLACE` /
`DROP TRIGGER IF EXISTS`），重复执行必须是干净 no-op。

## 历史

`001` 之前的 schema 变更记在 **truth-vault 仓的 `autowriter-migrations/`**
（编号 001–008）。那批存在于 TV，是因为当年 TV 为**集成**改 autowriter 的
schema（`external_source` / `example_label_proposal` / `tv_synced_user_id` 回填
等都是集成产物，007 只是 `db.py::CREATE_TABLES_SQL` 的一份快照）。

那不构成「TV 拥有 autowriter 的 schema」的通则。**autowriter 自己的能力，
schema 归自己**——本目录从 deskcore 开始（TV `DECISIONS.md` D-041 /
`docs/10-sister-repo-followups.md` R-034）。

## 怎么跑

Supabase SQL Editor 粘贴执行，或 MCP `apply_migration`。
**建议先在 branch 库跑一遍 + `get_advisors` 核验无回归再进 prod。**

## 清单

| | 内容 | 不跑会怎样 |
|---|---|---|
| `001_deskcore.sql` | 发牌台账 / 成稿指纹 / 个人调校笔记 / 精修 diff 四张表 + 两个 RPC | deskcore 整个不可用 |
| `002_calibration_cas.sql` | `update_calibration_notes_cas`（审计 COR-003） | 退回旧 CAS 路径并埋 `calibration_cas_rpc_missing`；长笔记（>4000 字级）的自动学习仍然静默停摆 |
| `003_versions_unique_num.sql` | `UNIQUE(item_id, version_num)`（审计 COR-004） | 少了数据库层保护；应用层重试本身不依赖它。⚠️ 会先把历史重复对子重编号再建索引 |
| `004_deskcore_check_pushdown.sql` | `deskcore_check_drafts` + `deskcore_fingerprint_counts`（审计 SUP-002/SUP-004/ROB-004/ROB-011） | `check_drafts` 退回 Python 逐对比对并埋 `deskcore_rpc_missing`：结论一致但慢，且回到 4000 条上限；`list_projects` 退回逐项目 count |

**共同点：四个迁移都设计成"不跑也不会坏"** —— 应用侧检测到 RPC 不存在会降级并留痕，
而不是硬失败。这是刻意的：未迁移的库上硬失败会让整条功能停掉，比降级更糟。
但降级都是**有代价**的，别把"不会坏"读成"可以不跑"。
