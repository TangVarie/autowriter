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

> ⚠️ **加表还有第三步：发 `GRANT`。建出来 ≠ 能访问。**
>
> `service_role` **绕过 RLS，但不绕过表级 `GRANT`**——两套独立机制。它在
> `public` 下看着无所不能，靠的是 Supabase 给 `public` 配的 default privileges；
> `autowriter` 是本仓自建的 schema，**没有**这份默认授权，新表出生就是零权限。
>
> 代价在 2026-08-26 首次真部署当天兑现：`001` 建的四张表一行 `GRANT` 都没有
> （它只给两个**函数**发了 `EXECUTE`，于是"权限这块齐了"的错觉很完整——函数
> 能跑是因为 `SECURITY DEFINER` 走 owner 权限，和表权限无关）。deskcore 除
> `list_projects` 外每个工具都挂在 `42501 permission denied for table
> draft_fingerprints`，而 `/health` 全绿。补丁是 `007_deskcore_table_grants.sql`。
>
> 同一次排查还翻出 `calibration_note_audit` 也不在基线的授权名单里。现存库
> 看不出来（那张表建于 Supabase 授权口径变更之前，带着历史 `GRANT`），坏的只有
> **新开的库**——而 `db.log_calibration_audit` 的写入是 `except: pass`，表现是
> 调教笔记的审计流水**静默地一条都不留**。
>
> 现在 `tests/sql_parity_check.py` 常驻一条断言：**`autowriter` 下每一张表都必须
> 对 `service_role` 有 `SELECT/INSERT/UPDATE/DELETE`**。断的是不变量而不是名单，
> 以后加表忘了发 `GRANT`，CI 自己会红，不需要谁想起来更新清单。

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
> 空库 → `000` → `001..006`（它 glob 整个目录，新增的自动进链）→ **再跑一遍全部**
> （验幂等）→ 抽查表与函数是否都在 → 逐例比对下推 SQL 与 Python 算出来的数。
> **这才是"消双写"真正的意思：不是只留一份，而是让两份必须对得上、对不上就报错。**
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

**跑之前和跑之后各查一次库到底是什么状态**——别照着这份清单推断：

```bash
python -m deskcore.cli doctor          # 逐个探测, 只读; 缺哪个、缺了会怎样
```

它用的判据与真正消费这些迁移的代码同源（`store.rpc_missing`），所以不会出现
"文档说跑过了、代码认为没跑"这种分歧。`003` 建的是唯一索引，PostgREST 看不见
索引，它会诚实地报 `unprobeable` 并把该跑的 SQL 交出来。

> 这条命令是补上去的，因为**没跑迁移这件事在运行期完全看不见**：2026-08-26
> 实测生产库 `kduysqedr` 只跑过 `001`，而 runbook §0 当时写着"schema 也上了
> 生产"。查一次库就知道，但在这之前没有任何一条命令能查。

## 清单

| | 内容 | 不跑会怎样 |
|---|---|---|
| `000_baseline.sql` | 全部 15 张表 + 8 个函数 + RLS policy + 索引 + 触发器 | 新环境什么都没有。**已有库不要跑它**——跑增量就够（它幂等，跑了也不坏） |
| `001_deskcore.sql` | 发牌台账 / 成稿指纹 / 个人调校笔记 / 精修 diff 四张表 + 两个 RPC | deskcore 整个不可用 |
| `002_calibration_cas.sql` | `update_calibration_notes_cas`（审计 COR-003） | 退回旧 CAS 路径并埋 `calibration_cas_rpc_missing`；长笔记（>4000 字级）的自动学习仍然静默停摆 |
| `003_versions_unique_num.sql` | `UNIQUE(item_id, version_num)`（审计 COR-004） | 少了数据库层保护；应用层重试本身不依赖它。⚠️ 会先把历史重复对子重编号再建索引 |
| `004_deskcore_check_pushdown.sql` | `deskcore_check_drafts` + `deskcore_fingerprint_counts`（审计 SUP-002/SUP-004/ROB-004/ROB-011） | `check_drafts` 退回 Python 逐对比对并埋 `deskcore_rpc_missing`：结论一致但慢，且回到 4000 条上限；`list_projects` 退回逐项目 count |
| `005_deskcore_containment.sql` | **替换** `deskcore_check_drafts` 与 `deskcore_commit_fingerprints`（审计 COR-014）：正文四字串改走 bottom-k 的标准估计式，并多回一路**包含度** | 查重仍然跑，但**短稿整段照搬长稿抓不到**——那种形状下 Jaccard 的真值本来就够不着硬闸线。`check_drafts` 会在 `summary.containment_skipped_warning` 里明说这一路没生效；`commit` 侧自动回退 4 参旧签名（竞态窗口仍然关着，只是不做包含度重查） |
| `006_item_decision_provenance.sql` | `items` 加 `decision_source` / `reviewer_id` / `decided_at` 三列 + 一条部分索引（跨库审计 COR-004 / COR-007） | ⚠️ **这一个不跑是硬失败，不是降级**——见下 |
| `007_deskcore_table_grants.sql` | 把 `001` 建的那四张表授权给 `service_role`（`001` 只给函数发了 `EXECUTE`，表漏了） | ⚠️ **硬失败**：deskcore 除 `list_projects` 外全挂在 `42501 permission denied`，而 `/health` 全绿——见下 |

> ⚠️ **`006` 与上面五个不是一类，别把"不跑也不会坏"套到它头上。**
>
> `db.update_item_status` **无条件**写这三列（`source` 做成了必填关键字参数，
> 理由见该函数的 docstring）。缺列时 PostgREST 直接报
> `column "decision_source" ... does not exist`，于是现有工作台的**「通过 / 打回」
> 按钮、硬规则自动标记、查重重生耗尽的自动标记全部报错**。
>
> 刻意**不做**"去掉三列重试一次"的降级：那等于让机器判定继续伪装成人工反馈
> 去污染 TV 的评估模型，正是 COR-004 要治的那件事。只把报错翻译成人话
> （`db.py` 里那段 `except`），告诉人该跑哪个迁移。
>
> **所以升级顺序上，`006` 应该排在最前面**——它是唯一一个"代码已经发了、迁移
> 没跑就当场坏"的。

> ⚠️ **`007` 和 `006` 一样是硬失败，不是降级。** 缺了它，deskcore 除
> `list_projects` 外每个工具都在 `42501 permission denied` 上挂——而
> `/health` 仍然全绿（它探的是连得上、不是访问得了）。任何跑过 `001` 的库都要补，
> 排在 `006` 之后即可（它只发 `GRANT`，不依赖其它迁移的顺序）。

> ⚠️ `005` 里两个函数都是 **DROP + CREATE** 而不是 `CREATE OR REPLACE`：返回列 /
> 参数变了，`REPLACE` 会因签名冲突失败。**必须先跑 `004`**（它建的
> `deskcore_fingerprint_counts` 本文件不动）。
>
> **存量指纹不用重算。** 新估计式只用已经存下来的两个 sketch。`ngram_hashes`
> 的 cap 同时从 200 提到 400，新旧混着比是正确的（`t = min(两边最大值)`，精度
> 退回旧的那一边，与今天持平）。想让老行也升到新分辨率就重跑一次
> `backfill`——**可选**，不跑不会坏。

**`001`–`005` 的共同点：都设计成"不跑也不会坏"** —— 应用侧检测到 RPC 不存在会
降级并留痕，而不是硬失败。这是刻意的：未迁移的库上硬失败会让整条功能停掉，比
降级更糟。但降级都是**有代价**的，别把"不会坏"读成"可以不跑"。

**`006` 不在这个共同点里**（见上面那段）。加新迁移时先问一句它属于哪一类，然后
把答案写进上面那张表——`deskcore.cli doctor` 的 `impact` 字段就是照着这张表写的，
而 `tests/test_migration_doctor.py` 会断言**每个 `.sql` 都出现在这份清单里**。
漏写不会静默通过。
