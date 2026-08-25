# migrations/

给**已存在的库**打的增量 SQL。

## 基线与增量

| | 作用 | 何时跑 |
|---|---|---|
| `000_baseline.sql` | schema 的**源头**，fresh install 一把建全 | 新环境初始化 |
| `001+.sql` | 增量，把已有的库升到和源头一致 | 已有库升级 |

**加表/加列/改函数必须两边都改。** 只改增量 → 新环境缺东西；只改基线 →
生产库升不上去。两边都写成幂等（`IF NOT EXISTS` / `CREATE OR REPLACE` /
`DROP … IF EXISTS`），重复执行必须是干净 no-op。

> ⚠️ **基线原来是 `db.py` 里一个 1162 行的 Python 字符串（`CREATE_TABLES_SQL`）。**
> 审计 SUP-010 把它搬成了文件。搬的理由不是"db.py 太大"（虽然确实从 4438 行降到
> 3280），而是**没有任何代码执行过它，所以也没有任何东西验证过它**。
>
> 代价是真实发生过的：2026-08-24 修 COR-014 时发现，`db.py` 里那份
> `deskcore_commit_fingerprints` 还带着"空开头的 title-only 稿会互相精确撞车"这个
> bug——而 `001` 里同一个函数早就修好了。也就是说**任何一个新环境都会带着一个老
> bug 出生**，而没人会发现。
>
> 现在 `tests/sql_parity_check.py`（CI 每次跑）会在一个空库上真的执行：
> 空库 → `000` → `001..005` → **再跑一遍全部**（验幂等）→ 抽查表与函数是否都在 →
> 逐例比对下推 SQL 与 Python 算出来的数。**这才是"消双写"真正的意思：不是只留
> 一份，而是让两份必须对得上、对不上就报错。**
>
> 这个 harness 上线当天就抓到两个真问题：`004` 的 `CREATE OR REPLACE` 在
> 005-era 基线上会报 `cannot change return type`；`005` 里 6 参版的
> `deskcore_commit_fingerprints` 用了裸 `CREATE FUNCTION`，重跑会报
> `already exists`（也就是**不幂等**，而 README 这一节正好承诺了幂等）。

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
| `000_baseline.sql` | 全部 15 张表 + 8 个函数 + RLS policy + 索引 + 触发器 | 新环境什么都没有。**已有库不要跑它**——跑增量就够（它幂等，跑了也不坏） |
| `001_deskcore.sql` | 发牌台账 / 成稿指纹 / 个人调校笔记 / 精修 diff 四张表 + 两个 RPC | deskcore 整个不可用 |
| `002_calibration_cas.sql` | `update_calibration_notes_cas`（审计 COR-003） | 退回旧 CAS 路径并埋 `calibration_cas_rpc_missing`；长笔记（>4000 字级）的自动学习仍然静默停摆 |
| `003_versions_unique_num.sql` | `UNIQUE(item_id, version_num)`（审计 COR-004） | 少了数据库层保护；应用层重试本身不依赖它。⚠️ 会先把历史重复对子重编号再建索引 |
| `004_deskcore_check_pushdown.sql` | `deskcore_check_drafts` + `deskcore_fingerprint_counts`（审计 SUP-002/SUP-004/ROB-004/ROB-011） | `check_drafts` 退回 Python 逐对比对并埋 `deskcore_rpc_missing`：结论一致但慢，且回到 4000 条上限；`list_projects` 退回逐项目 count |
| `005_deskcore_containment.sql` | **替换** `deskcore_check_drafts` 与 `deskcore_commit_fingerprints`（审计 COR-014）：正文四字串改走 bottom-k 的标准估计式，并多回一路**包含度** | 查重仍然跑，但**短稿整段照搬长稿抓不到**——那种形状下 Jaccard 的真值本来就够不着硬闸线。`check_drafts` 会在 `summary.containment_skipped_warning` 里明说这一路没生效；`commit` 侧自动回退 4 参旧签名（竞态窗口仍然关着，只是不做包含度重查） |

> ⚠️ `005` 里两个函数都是 **DROP + CREATE** 而不是 `CREATE OR REPLACE`：返回列 /
> 参数变了，`REPLACE` 会因签名冲突失败。**必须先跑 `004`**（它建的
> `deskcore_fingerprint_counts` 本文件不动）。
>
> **存量指纹不用重算。** 新估计式只用已经存下来的两个 sketch。`ngram_hashes`
> 的 cap 同时从 200 提到 400，新旧混着比是正确的（`t = min(两边最大值)`，精度
> 退回旧的那一边，与今天持平）。想让老行也升到新分辨率就重跑一次
> `backfill`——**可选**，不跑不会坏。

**共同点：五个迁移都设计成"不跑也不会坏"** —— 应用侧检测到 RPC 不存在会降级并留痕，
而不是硬失败。这是刻意的：未迁移的库上硬失败会让整条功能停掉，比降级更糟。
但降级都是**有代价**的，别把"不会坏"读成"可以不跑"。
