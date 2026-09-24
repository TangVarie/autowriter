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
> 空库 → `000` → 目录下**全部**增量（它 glob 整个目录，新增的自动进链）→ **再跑一遍全部**
> （验幂等）→ 抽查表与函数是否都在 → **`autowriter` 下每张表对 `service_role` 的四个权限一个不缺**
> → **`008` 的按模型过滤落在 `deskcore_check_drafts` 的最终函数体里**（问的是终态，不是「008 跑没跑」）
> → 逐例比对下推 SQL 与 Python 算出来的数。
> **这才是"消双写"真正的意思：不是只留一份，而是让两份必须对得上、对不上就报错。**
>
> 这个 harness 上线当天就抓到两个真问题：`004` 的 `CREATE OR REPLACE` 在
> 005-era 基线上会报 `cannot change return type`；`005` 里 6 参版的
> `deskcore_commit_fingerprints` 用了裸 `CREATE FUNCTION`，重跑会报
> `already exists`（也就是**不幂等**，而 README 这一节正好承诺了幂等）。

> ⚠️ **但它有个天生的盲区，2026-09-16 补上了。**
>
> 这个 harness 跑的是**完整链条**（`000` → 全部增量）。而增量里凡是 `DROP` 掉再
> 重建的函数，都会把基线那一版盖掉——于是**基线自己漂了也永远看不出来**，
> 因为终态总是对的。
>
> 实际漂的就是这两个：`deskcore_check_drafts` 停在 2 参（缺 `005` 的样本量前置闸、
> 缺 `008` 的按 `embedding_model` 过滤）；`deskcore_commit_fingerprints` 签名对但
> 函数体缺两处样本量闸，**而且 `INSERT` 的列里根本没有 `embedding_model`**。
> 后者最坏：一个只跑 `000` 的新库写进去的指纹全都没有模型名，而查重那一路按
> 模型名过滤——**指纹写了，却永远匹配不上**，不报错。
>
> 而「只跑 `000`」正是本文件第一张表承诺的 fresh install 路径。
>
> 现在 `tests/test_baseline_parity.py`（**纯文本比对，不需要数据库，每次 pytest 都跑**）
> 守四条：基线里的函数块与「最后改它的那个增量」**字节级相同**；`GRANT` 签名跟着
> 函数签名走；`FINAL_SOURCE` 登记的确实是目录里最后定义它的那个文件（防的是以后
> 加 `009` 又漂）；增量里 `DROP` 掉重建的函数都得登记。
>
> **所以改增量里那些函数的维护方式是：把整块原样复制进基线。** 刻意要求字节相同、
> 而不是"语义等价"——后者得先解析 SQL，而解析器一旦有 bug，失效方式恰好是
> "看起来绿、其实漂了"，正是这条要治的病。基线特有的说明写在 `CREATE` 上方，
> 不要写进函数块里面。

## 历史

`001` 之前的 schema 变更记在 **truth-vault 仓的 `autowriter-migrations/`**
（编号 001–008）——⚠️ 注意那是**另一套**编号，与本目录的 `001`–`008` 同号不同物，
本目录的 `001` 不是它的续号。那批存在于 TV，是因为当年 TV 为**集成**改 autowriter 的
schema（`external_source` / `example_label_proposal` / `tv_synced_user_id` 回填
等都是集成产物，007 只是 `db.py::CREATE_TABLES_SQL` 的一份快照）。

那不构成「TV 拥有 autowriter 的 schema」的通则。**autowriter 自己的能力，
schema 归自己**——本目录从 deskcore 开始（TV `DECISIONS.md` D-041 /
`docs/10-sister-repo-followups.md` R-034）。

## 怎么跑

Supabase SQL Editor 粘贴执行，或 MCP `apply_migration`。
**建议先在 branch 库跑一遍 + `get_advisors` 核验无回归再进 prod。**

**跑之前和跑之后各查一次库到底是什么状态**——别照着这份清单推断：

⚠️ `doctor` **直连库**读 schema，所以要先有 `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`。
没配时抛的是一句讲「worker 领不到 job」的 `RuntimeError`（`db.get_service_client` 的通用文案），
跟迁移无关——不是你命令敲错了。

```bash
python -m deskcore.cli doctor                    # 逐个探测, 只读; 缺哪个、缺了会怎样
python -m deskcore.cli doctor --project <uuid>   # 额外打印这个项目的指纹回填缺口
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
| `000_baseline.sql` | 全部 15 张表 + 8 个函数 + RLS policy + 索引 + 触发器 | 新环境什么都没有。**已有库不要跑它**——跑增量就够。它幂等、落在任何中途态上也不会留下重复重载（2026-09-16 起两条断言守着：函数体与增量字节相同、基线落在历史签名之上仍只剩一个重载，见下） |
| `001_deskcore.sql` | 发牌台账 / 成稿指纹 / 个人调校笔记 / 精修 diff 四张表（含它们的表级 `GRANT`，与 `007` 同一条语句）+ 两个 RPC（`deskcore_reserve_angles` / `deskcore_commit_fingerprints`）+ `items.updated_at` 与两个 `updated_at` 触发器 | deskcore 整个不可用；另外 `items.updated_at` 缺席会让 TV 的 `sync_autowriter_decisions_to_prepublish` 退回只按 `created_at` 增量同步，迟到的人工决策重新开始漏收 |
| `002_calibration_cas.sql` | `update_calibration_notes_cas`（审计 COR-003） | 退回旧 CAS 路径并埋 `calibration_cas_rpc_missing`；长笔记（>4000 字级）的自动学习仍然静默停摆 |
| `003_versions_unique_num.sql` | `UNIQUE(item_id, version_num)`（审计 COR-004） | 少了数据库层保护；应用层重试本身不依赖它。⚠️ 会先把历史重复对子重编号再建索引 |
| `004_deskcore_check_pushdown.sql` | `deskcore_check_drafts` + `deskcore_fingerprint_counts`（审计 SUP-002/SUP-004/ROB-004/ROB-011） | `check_drafts` 退回 Python 逐对比对并埋 `deskcore_rpc_missing`：结论一致但慢，且回到 4000 条上限；`list_projects` 退回逐项目 count |
| `005_deskcore_containment.sql` | **替换** `deskcore_check_drafts` 与 `deskcore_commit_fingerprints`（审计 COR-014）：正文四字串改走 bottom-k 的标准估计式，并多回一路**包含度** | 查重仍然跑，但**短稿整段照搬长稿抓不到**——那种形状下 Jaccard 的真值本来就够不着硬闸线。`check_drafts` 会在 `summary.containment_skipped_warning` 里明说这一路没生效；`commit` 侧自动回退 4 参旧签名（竞态窗口仍然关着，只是不做包含度重查） |
| `006_item_decision_provenance.sql` | `items` 加 `decision_source` / `reviewer_id` / `decided_at` 三列 + 一条部分索引（跨库审计 COR-004 / COR-007） | ⚠️ **这一个不跑是硬失败，不是降级**——见下 |
| `007_deskcore_table_grants.sql` | 把 `001` 建的那四张表授权给 `service_role`（2026-08-26 之前那版 `001` 只给函数发了 `EXECUTE`，表漏了；现在的 `001` 已含同一条 `GRANT`，所以这一半对新库是 no-op）+ 给 `calibration_note_audit` 补 `service_role` 全套 / `authenticated` 的 `SELECT,INSERT`，并**收回** `authenticated` 对它的 `UPDATE`/`DELETE`（审计表 append-only）——后面这三条只在这里和基线里有，所以**任何跑过 `001` 的库仍然都要跑 `007`** | ⚠️ **硬失败**：碰 `001` 那四张表的工具全挂在 `42501 permission denied`，而 `/health` 全绿——见下 |
| `008_embedding_model_isolation.sql` | `deskcore_check_drafts` 的标题语义那一路加**按 `embedding_model` 过滤**（`CREATE OR REPLACE`，签名不变） | 换过 embedding 模型的库上，标题语义比对会把**别的模型产的**向量也算进来。跨模型余弦是噪声：既放过真重复、也误杀无关稿，而 `semantic_degraded` 照报 `false`——见下 |
| `009_tv_links.sql` | 写作台 ↔ TV 的稿子对照：`tv_project_map`（TV 的 `project_id` ↔ 写作台项目，谁是补录目标）、`tv_note_links`（每条 TV 笔记对到哪一版、怎么对上的）+ 两个跨 schema 的 `SECURITY DEFINER` RPC：`deskcore_tv_notes`（读 `truth_vault.notes`）、`deskcore_tv_backfill_lineage`（把对照写回 TV 的 `source_autowriter_*` 两列，**只填 NULL**）。两个函数用 `to_regclass` 守着，库里没有 `truth_vault` 时返回空，本地 harness 与新库都建得出来。另有 `ingest_locks` + `deskcore_ingest_lock` / `deskcore_ingest_unlock`：补录的**跨进程**互斥（服务进程的 `ingest_published` 工具与 CLI/cron 的 `tv-sync` 都会「先读指纹再写」），按项目一行、带 TTL、到期可接管 | `tv-sync` 整个不可用：TV 的笔记认不回写作台的版本，已发未入库的稿子补不进指纹库，「爆没爆」永远回不到写作台（2026-09-17 之前的状态：5966 条笔记 lineage 全 NULL） |
| `010_tv_links_rls.sql` | `009` 建的三张表（`tv_project_map` / `tv_note_links` / `ingest_locks`）开 RLS，与本 schema 其他表同一口径；生产库里那张手工留的 `versions_num_backup_20260826` 也顺手开（`to_regclass` 守着，本地与新库跳过）。**幂等，doctor 不探它**（RLS 开没开从 PostgREST 探不到，且漏跑不影响功能）；`tests/sql_parity_check.py` 守「autowriter 下每张表都开了 RLS」这条不变量 | 什么功能都不坏：anon / authenticated 对这几张表本来就没有表级 GRANT，service_role 绕 RLS。差的是 Supabase advisor 一直报 `rls_disabled_in_public`，以及以后谁给 anon 发了 GRANT 会一下子把整张表露出去 |
| `011_angle_outcomes_view.sql` | 视图 `autowriter.v_angle_outcomes`：发牌台账用掉的坐标 → `tv_note_links` → `truth_vault.notes.tier`（2026-09-24，JevforCoentent docs/00「顺手查到 · 写作台」）。只列用掉了的坐标，还没对上笔记的 `note_id` / `tier` 为 NULL（算爆文率要分母）；只认真对上的对照（与 `core._TV_BACKFILL_KINDS` 同一份名单，`ingested` 不认）。⚠️ **要用底表属主跑**（Supabase 上 SQL Editor / `apply_migration` 的默认身份 `postgres`）：视图以属主身份读底表（`security_invoker = false`），三张底表 RLS 开着且无 policy，换一个普通角色建出来的视图会**永远 0 行且不报错**——所以它先验当前角色，不够格当场 `RAISE`。**只 GRANT 给 `service_role`**，显式收回 anon / authenticated（明细视图，看板要看就在 TV 那边建 public 聚合视图）。库里没有 `truth_vault.notes` 时整段 NOTICE 跳过，TV 落库之后重跑即可；基线里有逐字相同的一段 | **可降级**：今天没有任何代码读它，缺了只是发牌加权（docs/31 位置 ④）拿不到结果。`doctor` 探得到：视图不在报 missing；视图在但没授权也报 missing 并指回 011（GRANT 在它自己里面，不是 `007`） |

> ⚠️ **`008` 治的是"这一列存在了几个月却从来没有代码用过它"。**
>
> `draft_fingerprints.embedding_model` 从 `001` 起就在，它的 `COMMENT` 写着"换
> embedding 供应商时唯一的救命稻草"。2026-08-26 真换模型时才发现：**四路查重
> 里没有任何一路读过它**，SQL 侧和 Python 侧都是拿 `title_embedding IS NOT NULL`
> 当"可比"。
>
> 为什么维度守卫救不了：`text-embedding-004` 和 `gemini-embedding-001` 都能出
> 768 维，写库不报错、长度校验也过。但两套向量空间毫不相干。
>
> `008` 只改函数体（`CREATE OR REPLACE`，签名和返回列一个字没动），所以**跑没跑过
> 从调用侧完全看不出来**——`doctor` 因此把它报成 `unprobeable` 并交出查函数体的
> SQL，而不是蒙一个 applied。
>
> 存量的老模型向量从此不参与比对，`semantic_degraded` 会如实报 `true`。用
> `python -m deskcore.cli reembed --project <id>` 拿当前模型重算即可。
> **"少比并说出来"好过"混着比不说话"。**

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

> ⚠️ **`007` 和 `006` 一样是硬失败，不是降级。** 缺了它，凡是读写 `001` 那四张表的工具
> 全挂在 `42501`：`open_project`（调校笔记）、`draw_angles`（发牌台账）、
> `check_drafts` / `commit_drafts`（成稿指纹）、`my_style` / `save_my_style`、`record_edit`。
> `get_protocol` 不碰库，`create_project` 只碰 `projects`，另有一批只碰 `memories` / `items` 的
> （`record_rule` / `my_rules` / `set_rule_state` / `label_example` / `export_drafts` /
> `borrow_lessons`）同样不受影响。
>
> **`list_projects` 更坏一点**：指纹计数的 `42501` 被 `store.fingerprint_counts` 与 core 里的
> 逐项目兜底**两层**吞掉，于是它返回一份看起来正常、`fingerprint_count` 全是 0 的清单——
> 而 `/health` 仍然全绿（它探的是连得上、不是访问得了）。
>
> 任何跑过 `001` 的库都要补，排在 `006` 之后即可（它只发 `GRANT`，不依赖其它迁移的顺序）。

> ⚠️ **换签名必须自己 `DROP`，`CREATE OR REPLACE` 兜不住。** 这条值得单独记，
> 因为它的失效方式是无声的（PG 16.13 实测）：
>
> | 改了什么 | `CREATE OR REPLACE` 的反应 |
> |---|---|
> | 返回类型 | 报 `cannot change return type of existing function` |
> | **参数表** | **一句话都不说，安静地新建一个重载** |
>
> 第二行就是 2026-09-16 那个洞的成因：基线把 `deskcore_check_drafts` 从 2 参换成
> 3 参，只写 `CREATE OR REPLACE`，于是任何一个用旧基线起过、又没跑到 `005` 的库
> 重跑基线之后，库里同时有两个重载，两参调用当场
> `function autowriter.deskcore_check_drafts(uuid, jsonb) is not unique`。
> 现在基线在两处 `CREATE` 前面各自带了一句 `DROP FUNCTION IF EXISTS`（全新库上是
> 干净 no-op），`tests/sql_parity_check.py` 的「基线落在历史签名之上」那条守着它。
>
> ⚠️ `005` 里两个函数的建法**不一样**，别按一种记：
> `deskcore_check_drafts` 是 **DROP（两个签名都删）+ 裸 `CREATE`**——返回列变了，
> 光靠 `REPLACE` 会报 `cannot change return type`；`deskcore_commit_fingerprints`
> 是 **DROP 掉 4 参旧版**（留着会变同名重载，调用时报 ambiguous）
> **+ `CREATE OR REPLACE` 建 6 参版**——这一半必须是 `OR REPLACE`，因为基线里已经有
> 6 参那版了，裸 `CREATE` 会报 `already exists`
> （就是上面那条「harness 上线当天抓到的第二个真问题」）。
> **必须先跑 `004`**（它建的 `deskcore_fingerprint_counts` 本文件不动）。
>
> **存量指纹不用重算。** 新估计式只用已经存下来的两个 sketch。`ngram_hashes`
> 的 cap 同时从 200 提到 400，新旧混着比是正确的（`t = min(两边最大值)`，精度
> 退回旧的那一边，与今天持平）。想让老行也升到新分辨率就重跑一次
> `backfill`——**可选**，不跑不会坏。

**`002`–`005` 的共同点：都设计成"不跑也不会坏"** —— 应用侧检测到 RPC 不存在会
降级**并留痕**，而不是硬失败。这是刻意的：未迁移的库上硬失败会让整条功能停掉，比
降级更糟。但降级都是**有代价**的，别把"不会坏"读成"可以不跑"。

**三个不在这个共同点里的，各是各的坏法**：`001` 建的是表，不跑等于 deskcore 整个不可用；
`006` / `007` 是硬失败，缺了当场报错（见上面那两段）；`008` 最坏——它不跑**不报错也不留痕**，
标题语义那一路混着别的模型的向量算，而 `semantic_degraded` 照报 `false`。

加新迁移时先问一句它属于**硬失败 / 可降级 / 静默错答**哪一类，然后把答案写进上面那张表——
`deskcore.cli doctor` 的 `impact` 字段就是照着这张表写的，而 `tests/test_migration_doctor.py`
有两条守卫：`test_every_migration_is_listed_in_the_deployment_docs` 断言**每个增量 `.sql` 的
文件名都在这三份部署文档里各出现过**（本文件 + `docs/deskcore.md` + `docs/deskcore-runbook.md`；
`000_baseline.sql` 不在此列），`test_doctor_probes_every_incremental_migration` 断言
`core.migration_state` 里每个增量迁移都有一条探测。
两条都只保证"提到了 / 探到了"——**写进表格哪一格、`impact` 写得对不对，仍然要人自己看。
漏写不会静默通过，写错会。**
