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
