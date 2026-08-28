# deskcore 上线手册 · 对接 · 待办

> **这份文档的定位**：`docs/deskcore.md` 讲的是**为什么这么设计**，这份讲的是
> **现在到底是什么状态、还差什么、你下一步做什么**。以后主要在本仓迭代，从这里开始读。
>
> 快照时间：**2026-08-26**。所有数字都是当天从生产库（`kduysqedr`）和线上服务实测的，
> 不是照文档抄的。**改动之后请回来更新这些数字**，否则它就变成下一份"写着已经有了、
> 实际没有"的文档——本仓在这上面栽过好几次，登记在 §5。

---

## 0. 一句话现状

**整条链路已经跑通并验证过**：八个迁移全部落库、服务在 Railway 上线、指纹库
回填完 **3,678 行 / 44 个项目**、四路查重信号全部到齐。剩下的只有一件是**你要做**
的——把 deskcore 挂到客户端（见 §2 第 3 步），不挂上去这一整套没人用。

离线自测：全套 pytest（条数以实跑为准，别在文档里钉死——前几次提交都在 commit message 里报了新数却没回来改这里）+ selftest + 真 PostgreSQL 上的迁移叠加与 SQL/Python
逐例比对，全绿。线上实测：`/health` 全绿、13 个工具都在、`semantic_degraded`
为 `false`；四路查重信号**各自都当过一次判定信号**（`opening` / `title` /
`ngram` / `contain` 各有一个专属探针，见 §1.5）。

> ⚠️ **这一段的历史值得留着看**，它是本仓那条老毛病的活标本：
>
> - 2026-08-23 版写的是"schema 也上了生产"。**那是错的**——08-26 实测时只有
>   `001`，另外五个一个都没跑。修这条的同时补了 `deskcore.cli doctor`。
> - 补齐六个之后服务仍然除 `list_projects` 外全挂在 `42501`：`001` 建的四张表
>   **一行 `GRANT` 都没有**，而 `/health` 照样全绿。→ `007`（见 §1.3）
> - `007` 之后查重能跑了，但最贵的那一路是**哑的**：换掉下线的
>   `text-embedding-004` 时才发现 `embedding_model` 这一列存在了几个月、
>   **从来没有任何代码读过它**。→ `008`（见 §1.4）
> - 换模型的修复本身又带出两个：回填时**一条空标题让整批 embedding 作废**
>   （靠前一步刚补的日志才抓住）；修那条又把 `check_drafts` 的下推路径
>   **打崩成 500**（判据只问下标越没越界、不问那一位有没有东西）。
>
> **五次都是同一个形态：写着已经有了，实际没有，而且不报错。**
> 没有一次是读代码读出来的——全靠真打一次。最后两个尤其说明问题：一个是被
> 前一个的修复抓住的，另一个是被**验证部署的探针**抓住的。
> `/health` 全绿在这五次里**一次都没帮上忙**。

---

## 1. 实测状态快照（2026-08-26）

### 1.1 TV 侧（truth-vault）——在跑，且是自动的

| 部件 | 实测结果 |
|---|---|
| `daily-sync` cron | 129 次 run，最近 20+ 次全绿，每天 02:00 UTC（北京 10:00）自动触发 |
| librarian 服务 | `https://truth-vault-production.up.railway.app/health` → `{"ok":true,"service":"flywheel-librarian"}` |
| worker 服务 | `https://tv-worker-production.up.railway.app/health` → `ok`，`auth.ok=true`、`mode=X-Worker-Key` |
| 数据 | `truth_vault.notes` 4,223 · 经验卡标注 347 · 馆员缓存 5 · `prepublish_evaluations` 598（全 human） |

TV 每天自己干的事：飞书 → `truth_vault.notes` → LLM 标 essence → 策展成经验卡 → 进书架；
同时把本仓的人工审稿决定倒灌回 `prepublish_evaluations` 存档。**这条线不需要本仓管。**

### 1.2 本仓（autowriter）——全部到位，只差挂到客户端

| 部件 | 实测结果 |
|---|---|
| `deskcore/` 代码 | 齐。`selftest` → **PASS**；`pytest tests/` → **全绿**（条数每次提交都在涨，以实跑为准）；`tests/sql_parity_check.py` 在真 PostgreSQL 上 → 基线 + 八个迁移叠起来、重跑幂等、SQL 与 Python 逐例一致，**全绿** |
| **schema** | ✅ 八个迁移**已全部跑进生产**（2026-08-26，见 §1.3 / §1.4） |
| **服务部署** | ✅ Railway，`https://autowriter-production.up.railway.app`。`/health` 全绿、**16 个工具**都在（2026-08-28 加了 `my_rules` / `set_rule_state` / `reembed_my_rules`）、`DESKCORE_ALLOW_ANONYMOUS` **未设**（`anonymous_allowed: false`） |
| **四路查重信号** | ✅ 全部到齐——`check_drafts` 实测 `semantic_degraded: false`（2026-08-26 换 `gemini-embedding-001` + 换 key 之后） |
| `draft_fingerprints` | ✅ **3,678 行 / 44 个项目**（2026-08-26 回填，见 §1.5）。当前模型 3,671 / 来路不明 0 / 别的模型 0 / 维度 min=max=768 / 重复 `version_id` 0 |
| `angle_ledger` / `user_calibration_notes` / `style_edits` | **全 0** ← 没人用过 |
| `memories` | 303 条 |
| ⚠️ `memories.severity` | **303 条全是 `soft`，`hard` = 0** |
| `items` | 4,574 条 / 61 个项目 / 6 个 owner |
| `versions` | 5,532 行（跑 `003` 前后一致——重编号只改号，不增删行） |
| ⚠️ `versions.embedding` | **非空 0 条** |
| `(item_id, version_num)` 重复 | **0**（`003` 之前是 339 对） |
| 最后一次真实使用 | 2026-08-19（item / batch / memory 的最新 `created_at` 都停在这天） |

### 1.3 迁移落库记录（2026-08-26）

跑之前实测只有 `001`。按 `006 → 002 → 003 → 004 → 005` 补齐，每跑一个验一个；
`007` 是**服务真跑起来之后**才被逼出来的第七个，见表下那段：

| 迁移 | 结果 | 怎么验的 |
|---|---|---|
| `006_item_decision_provenance` | ✅ | 三列 + CHECK + 部分索引都在；用**与 `db.update_item_status` 完全相同的 SET 子句**、`where` 匹配 0 行跑了一次 UPDATE，不再报缺列 |
| `002_calibration_cas` | ✅ | `SECURITY INVOKER`、`search_path` 固定、`anon` 已 REVOKE、`authenticated`+`service_role` 已 GRANT；witness 不匹配返 0 行 |
| `003_versions_unique_num` | ✅ | 见下 |
| `004_deskcore_check_pushdown` | ✅ | 两个 RPC 都在 |
| `005_deskcore_containment` | ✅ | `check_drafts` 只剩 **3 参**那版、`commit_fingerprints` 只剩 **6 参**那版（**没有重载残留**，留着旧签名调用时会报 ambiguous）；拿一条真数据跑通四路比对，10 列都回来了 |
| `007_deskcore_table_grants` | ✅ | 四张表对 `service_role` 的 `SELECT/INSERT/UPDATE/DELETE` 都在；更值钱的是反过来问的那条——`autowriter` 下**没有一张表**缺 `service_role` 权限 |

**`007` 是服务打通之后才发现的**，值得单独记，因为它的失败形态骗过了前面每一道检查：

- 症状：`doctor` 全绿、`/health` 全绿、六个迁移全部核验过，而 deskcore 除
  `list_projects` 外**每个工具**都回 `42501 permission denied for table
  draft_fingerprints`。
- 根因：`001` 建了四张表，却只给两个**函数**发了 `EXECUTE`，表本身一行 `GRANT`
  都没有。`service_role` **绕过 RLS，但不绕过表级 `GRANT`**——两套独立机制。它在
  `public` 下看着无所不能，靠的是 Supabase 给 `public` 配的 default privileges；
  `autowriter` 是本仓自建 schema，**没有**这份默认授权。
- 同一次排查还翻出 `calibration_note_audit` 也不在基线的授权名单里——而
  `db.log_calibration_audit` 的写入是 `except: pass`，表现是调教笔记的审计流水
  **静默地一条都不留**。
  > ⚠️ 我当时判断"现存库带着历史 `GRANT`，所以只补基线就行"。**那是从一个库
  > 推出来的结论，错了**（codex 在 aw#65 上指出）：任何在 Supabase 授权口径变更
  > **之后**用旧版基线开出来的库都缺这份授权，而它们升级走的是增量、不重跑基线。
  > 现在 `000_baseline.sql` 和 `007` 两边都补了。
  >
  > 顺带按 `user_logins` 的先例收成 append-only（`authenticated` 只给
  > `SELECT + INSERT`）——能被改写或删除的流水，回答不了"这条观察为什么变了"。
- 补了两道守卫，免得靠人肉 `curl` 才发现下一个：`tests/sql_parity_check.py` 断言
  **`autowriter` 下每一张表都必须对 `service_role` 有 `SELECT/INSERT/UPDATE/DELETE`**
  （断不变量而不是名单）；`doctor` 把 `42501` 单独报成 `denied` 而不是混进
  `error`，并直接指向 `migrations/007`，而且**四个权限一个个探**——"读得到"
  证明不了"写得进"，只探 `SELECT` 的话一个部分授权的库会报全绿。

### 1.4 `008_embedding_model_isolation` —— 换模型逼出来的第八个

换掉下线的 `text-embedding-004` 之后才发现的，形态和 `007` 是同一类：
**一样东西写着已经有了，实际从来没生效过。**

`draft_fingerprints.embedding_model` 从 `001` 起就在，它的 `COMMENT` 写着是"换
embedding 供应商时唯一的救命稻草"——而**四路查重里没有任何一路读过它**，SQL 侧和
Python 侧都是拿 `title_embedding IS NOT NULL` 当"可比"。

为什么没被任何检查拦住：`text-embedding-004` 和 `gemini-embedding-001` 都能出
768 维，维度守卫过、写库不报错。但两套向量空间毫不相干，跨模型的余弦是噪声，
而且**双向出错**——真重复的算出来很低（放行），无关的算出来很高（误杀）。
最坏的一层是 `semantic_degraded` 报 `false`：这一路不但失灵，还在报告里说自己
跑过了。

改了四处：`008` 给 SQL 侧加按模型过滤（`CREATE OR REPLACE`，签名不变）；Python
兜底路径同一口径；`backfill` 不再把来路不明的 `versions.embedding` 贴上当前模型
的标签（那张表没有模型标记，来路无法证明）；`reembed` 从只补 `NULL` 扩到也能重算
**换模型作废**和**来路不明**的行——否则老行既进不了比对、又永远不会被重算，
卡在一个没有出口的状态里。

> ⚠️ **写 `008` 的当时生产库不受影响**：`draft_fingerprints` 还是 **0 行**、
> `versions.embedding` 非空 **0 条**，没有任何存量向量可污染。**同日晚些时候
> 回填了 3,678 行**（§1.5），全部是 `gemini-embedding-001` 现算的、来路不明 0 条
> ——这套隔离是**在库里还空着的时候**装好的，正好赶在第一批向量写进去之前。
> 下一次换模型时它才真正开始干活。

**落库记录（2026-08-26，记为 `aw_008_embedding_model_isolation`）**——`008` 是
`CREATE OR REPLACE`，签名与返回列一个字都没动，所以**跑没跑过从调用侧完全看不出来**，
验证只能查函数体：

| 验什么 | 结果 |
|---|---|
| 函数体里真有那句过滤（**剥掉注释再查**） | ✅ `(_model IS NULL OR f.embedding_model = _model)` |
| 用的不是 `IS NOT DISTINCT FROM` | ✅ 代码里没有 |
| 会从 `_rows` 里读模型名 | ✅ `_model := NULLIF(r->>'embedding_model', '')` |
| 签名没变、无重载残留 | ✅ 只有一个 `(uuid,jsonb,integer)` |
| `STABLE` / `search_path` 固定 / 只授 `service_role` | ✅ 三项都对（`anon` / `authenticated` 均 false） |

> ⚠️ **"剥掉注释再查"不是讲究，是踩过的坑。** 第一遍直接 `prosrc LIKE
> '%IS NOT DISTINCT FROM%'` 报了 `true`，吓一跳——其实是**解释"为什么不用这个
> 操作符"的注释**里出现了那句话。本地那条同名断言也在同一处被自己打红过。
> 查函数体的断言一律先 `regexp_replace(ln, '--.*$', '')`。

**还在真 pgvector 上做了一次功能验证**，因为这一块**所有自动化测试都覆盖不到**——
`tests/sql_parity_check.py` 把 pgvector shim 成了 text 域（见该文件头），单元测试
用的是假件。做法：造三条**向量完全相同**、只有 `embedding_model` 不同的历史指纹，
所以余弦都是 `1.0`，**命中谁只可能由模型过滤决定**：

| 送什么模型 | 命中 | `best_sim` |
|---|---|---|
| `gemini-embedding-001` | AAA 本模型 | 1.0000 |
| `text-embedding-004` | BBB 老模型 | 1.0000 |
| `no-such-model-xyz` | **（无命中）** | **0.0000** |
| 不带模型（兼容路径） | AAA 本模型 | 1.0000 |

> ⚠️ **只跑第一行是不够的**，我差点就收工了：三条余弦都是 1.0，"命中本模型的行"
> 也可能只是标题排序碰巧。第 2 行（换模型就换命中）证明它是**按模型选**而不是
> 按标题选；第 3 行（送一个不存在的模型 → 一条都不命中）是决定性的那条。
>
> 整个夹具包在 `BEGIN … ROLLBACK` 里，生产库一行都不留；跑完复查过
> 项目数 61 不变、`draft_fingerprints` 仍是 0 行。**完整的可重跑查询写在
> `migrations/008_embedding_model_isolation.sql` 末尾**——它只存在于这次对话里
> 的话，下一个环境等于没有。

### 1.5 指纹库回填记录（2026-08-26）

只回填了**有实质调教痕迹的两个 owner**——按"调教笔记 + 记忆规则 + 人工决策 + 反馈"
四个维度筛，不按 items 总量：

| owner | 项目 | 有笔记的项目 | 笔记字数 | 记忆 | items | 已标注 | 人工决策 | 反馈 | 跑了吗 |
|---|---|---|---|---|---|---|---|---|---|
| `85f5f888` | 29 | **15** | **16,591** | **152** | 3,385 | 73 | 343 | 17 | ✅ |
| `afbaf84e` | 20 | 4 | 4,989 | **139** | 667 | 42 | **206** | **40** | ✅ |
| `b907ec9d` | 6 | 3 | 1,630 | 11 | 504 | 2 | 34 | 2 | ❌ 太薄 |
| 另外三个 | 6 | ≤1 | ≤460 | ≤1 | 18 | ≤1 | ≤3 | 0 | ❌ 全零 |

> ⚠️ **`afbaf84e` 只看 items 总量会被漏掉**：它的 items 只有 `85f5f888` 的五分之一，
> 但记忆 139 条几乎持平、人工决策 206 条、**反馈 40 条比对方的 17 条还多**。
> 按密度算它是调教最扎实的一个。筛选口径用错维度，就会把最该回填的那个丢掉。

**库那侧核过的形状**（不是看脚本自报的数）：

```
3,678 行 / 44 个项目
当前模型 3,671   来路不明 0   别的模型 0
向量维度 min = max = 768
重复 version_id 0        ← 中途掐断重跑过, 幂等没破
没有向量 7 条            ← 全部是标题为空的历史脏数据
```

**端到端（在 385 条历史的项目上，1.3–2 秒返回）**：

⚠️ **`fingerprint.deciding_signals` 是短路的**：`opening → title → ngram → contain`，
前面命中就直接返回。所以要证明"四路各自都能独立判死"，**必须让每一路都当过一次
`decided_by`**——只看信号值都算出来了是不够的，那四个数就算全是噪声，探针照样
全过。（codex review · aw#68 指出我第一版只证了两路就写了"四路各自在发言"。）

| 场景 | `decided_by` | 关键信号 |
|---|---|---|
| 原样照搬 | `opening` | 开头精确 ✓ |
| 近义改写标题 + **完全无关的正文** | **`title`** | **cos=0.9797**，J=0.0075，开头 ✗ |
| 只搬正文后半段（开头也不同） | `ngram` | J=0.498 |
| **从长稿中段截 110 字**（不含开头） | **`contain`** | **J=0.272 够不着 0.35 硬闸，包含度 100%（样本 97）** |
| 无关新稿（对照组） | — `pass` | J=0.003 / 包含度 0.01 |

最后一行就是 `migrations/005` 存在的全部理由：短稿整段照搬长稿时，**Jaccard 会被
长度差稀释**（0.272 < 0.35 的硬闸），只有包含度抓得住。这一路要是坏了，前三个探针
一个都不会红。

⚠️ **回填过程中撞出两个 bug，都已修**（见 §0 那条时间线的最后一项）：一条空标题
让整批 embedding 作废；修那条又把 `check_drafts` 的下推路径打崩成 500。前者靠日志
抓住，后者靠"验证部署有没有生效"的探针抓住。

**`003` 是唯一改数据的一个**，所以单独记：

- 跑之前存了快照表 `autowriter.versions_num_backup_20260826`（5,532 行，回滚 SQL 写在表注释里）。**确认无误之后可以 drop 它。**
- 先干跑（只 SELECT）：339 个 item 受影响 / 715 个版本行 / **实际会改 376 行** / 改完仍重复 **0** 对 / 未触碰却重复 **0** 对。
- 跑完实测：唯一索引已建、重复清零、行数 5,532 → 5,532、与快照相比改了 **376** 行（与干跑一致）。
- ⚠️ 单独验了一件事：**受影响的 339 个 item 里，"代表版本"（最大号那一行）被换掉的是 0 个**。重编号的排序键是 `(version_num, created_at, id)`，最大号仍是最大号——这正是 COR-004 要保住的东西。

跑完 `get_advisors(security)`：没有一条与本次迁移相关。报出来的 ERROR 全在 `public`
schema（dashboard 视图 / pipeline 表，既有问题）；`autowriter` 四张表的
`rls_enabled_no_policy` 是 INFO 且**设计如此**（deskcore 走 service_role 绕 RLS，
隔离由 `store.py` 显式执行，启用 RLS 无策略 = 对其它角色一律拒绝）。
**没有 `function_search_path_mutable`** —— 五个函数的 `search_path` 都固定了。

> ⚠️ 一个跑之前没料到的坑：`006` 里的 `ALTER TABLE items` **不带 schema 前缀**
> （它假定在 SQL Editor 里 `search_path` 已含 `autowriter`），而 Supabase 的
> `apply_migration` 用的是 `"$user", public, extensions`。好在 `public.items`
> **不存在**，所以那句会当场报错而不是改错表——但下次谁在别的环境跑，先确认
> `search_path`，或者在前面加一句 `SET search_path = autowriter, public, extensions;`。

以后不用手写这些 SQL：

```bash
python -m deskcore.cli doctor        # 只读; 逐个探测缺哪个、缺了会怎样、下一步跑什么
```

单项目体量（`backfill` 口径 = 每个 item 的**有版本的**那些，所以略小于 item 数）：

| 项目 | project_id | owner_id | backfill 应有 |
|---|---|---|---|
| WTG-No.3产品直给体验型 | `e4171a1f-f66f-4f6f-84f8-ab4782e67fed` | `85f5f888…` | 385 |
| WTG-No.1佳琦背书直给型 | `ff28bf9e-696f-499b-8273-9d1d4e847d22` | `85f5f888…` | 375 |
| WTG-No.2佳琦关键词占位型 | `8c3d654e-bb4d-4445-9258-fec17f515d2e` | `85f5f888…` | 359 |
| WTG-No.4贴全棉时代 | `5caa74c8-62e1-4dd2-960a-f5d269489470` | `85f5f888…` | 315 |
| 唐小轻成分党 | `bcc3d155-201d-4920-9fde-64933a8948dc` | `b907ec9d…` | 229 |
| RIO便利店调酒 | `b3ac2a5f-320b-49e5-9f11-15d6e517ec41` | `b907ec9d…` | 104 |

owner 分布（配 `DESKCORE_KEYS` 时从这里抄 UUID，**别新造**）：

| owner_id | 项目数 | items |
|---|---|---|
| `85f5f888-a649-4843-8358-9c83c91282e6` | 29 | 3,385 |
| `afbaf84e-e5a3-4429-9490-72718d2a9019` | 20 | 667 |
| `b907ec9d-9e49-4dac-89ad-220eb53afc38` | 6 | 504 |
| 另外三个 | 6 | 18 |

---

## 2. 上线五步（顺序不能换）

### 第 0 步 · 补齐迁移 ✅ **已完成（2026-08-26，见 §1.3 / §1.4）**

> 这一步在当前生产库上已经做完了。**下面这段留着不是历史记录，是给下一套库
> （branch 库、新环境、灾备重建）用的** —— 顺序和坑都在这儿。

**`006` 排在最前面，因为它是唯一一个"代码已经发了、迁移没跑就当场坏"的**：
`db.update_item_status` 无条件写 `decision_source` / `reviewer_id` /
`decided_at`，缺列时现有工作台的「通过 / 打回」按钮、硬规则自动标记、查重自动
标记全部报错。

跑法：Supabase SQL Editor 粘贴执行，或 MCP `apply_migration`。建议先在 branch 库
跑一遍 + `get_advisors` 核验再进 prod。顺序：

```
006 → 002 → 003 → 004 → 005 → 007 → 008
```

`006` 之外按编号顺序即可。**`005` 必须在 `004` 之后**（它 DROP 掉 `004` 建的那版
`deskcore_check_drafts` 再重建，跳过 `004` 会缺 `deskcore_fingerprint_counts`）。
**`007` 只发 `GRANT`**，不依赖顺序，但**不能省**——省了它服务能起、`/health`
全绿、而每个工具都挂在 `42501`（见 §1.3 那段）。全新库跑 `000_baseline.sql`
的话这条已经含在里面了。

**`008` 必须在 `005` 之后**（它 `CREATE OR REPLACE` 的正是 `005` 建的那版 3 参
`deskcore_check_drafts`；`005` 没跑的话 `REPLACE` 会顶到 `000` 里那版 2 参的、
签名对不上而失败）。见 §1.4。

⚠️ **`003` 会改数据**，不只是建索引：它按 `(version_num, created_at, id)` 稳定
重编号，再建唯一索引。重复号是 `bulk_create_initial_versions` 给多引擎批次每个
引擎都写 `version_num=1` 留下的历史（审计 §0.4 第 1 条），不是数据损坏。

08-26 那次的做法，下次照抄即可：

1. **先存快照**（`create table ... as select id, item_id, version_num from versions`），
   回滚 SQL 写进表注释。
2. **干跑**：把 `003` 的两个 CTE 原样跑成 SELECT，看四个数——受影响 item / 版本行 /
   实际会改的行 / **改完仍重复的对子（必须是 0，否则唯一索引建不起来）**。顺带查
   一次"未被触碰却重复的对子"也必须是 0。
3. 跑迁移。
4. **验代表版本没被换掉**：每个受影响 item 的最大号那一行，改前改后必须是同一行
   （拿快照 join 一次）。这条才是 COR-004 真正要保住的东西，只看"索引建起来了"
   是不够的。

跑完立刻自检——**别照文档推断，去查库**：

```bash
python -m deskcore.cli doctor
```

期望输出是「迁移: 全部到位」。`003` 会报 `unprobeable`（PostgREST 看不见索引），
它会把该跑的 SQL 一起交出来，自己跑一次确认。

> 这条命令是 2026-08-26 补的。此前**没有任何办法**一眼看出库跑到第几个迁移，
> 于是这份 runbook 的 §0 写了三天"schema 也上了生产"，而实际只有 `001`。
> 判据与真正消费这些迁移的代码同源（`store.rpc_missing`），不会出现"文档说跑过
> 了、代码认为没跑"。

### 第 0.5 步 · 先想清楚「协议不通怎么办」

WorkBuddy 的 HTTP MCP 到底认不认自定义 header，官方只说了支持 HTTP MCP 和 OAuth
（v4.7.3），**没有权威文档**。**这一步不通的话整个形态要换**，所以它是排在最前面
的风险——但它**验不了**，因为那时候还没有可连的地址。

所以实际顺序是：**先部署（第 1 步，很便宜），部好立刻验协议（第 3 步的前半段），
通了再往下走**。别把回填和铺开放在协议验证之前。

key 支持三种传法，优先级见 `docs/deskcore.md` §4.3——`?key=` 是最后的退路，
因为查询串会进访问日志且删不掉。

> 想在部署之前先摸一遍工具面：`uvicorn deskcore.app:app --port 8000` 本地起，
> 打 `POST /tool/{name}`。但**这验不了 WorkBuddy 的协议**——那需要一个
> WorkBuddy 够得着的公网地址。

### 第 1 步 · 部署 deskcore service ✅ **已完成（2026-08-26，Railway）**

新建一个 Railway service（与 `worker.py` 同级的独立 service），config 指
`deskcore/railway.json`。

| env | 必需 | 说明 |
|---|---|---|
| `SUPABASE_URL` | ✅ | 与主服务同一套 |
| `SUPABASE_SERVICE_ROLE_KEY` | ✅ | service_role，绕 RLS |
| `DESKCORE_KEYS` | ✅ **必需** | 见下方格式。不配 = 所有请求 401（ROB-003 起 fail-closed，不再匿名放行） |
| `GOOGLE_API_KEY` | 强烈建议 | 没有的话查重降级成纯确定性，同角度换说法的标题会漏 |
| `LIBRARIAN_URL` | 建议 | `https://truth-vault-production.up.railway.app`（已验活） |
| `LIBRARIAN_API_KEY` | 建议 | = TV librarian 那把 key |
| `DESKCORE_ALLOWED_HOSTS` | 可选 | 逗号分隔；设了才开 MCP 的 Host 校验。配错的表现是 **421**，看着像客户端的问题 |
| `DESKCORE_ALLOWED_ORIGINS` | 可选 | 逗号分隔的完整 origin。**不设 = 允许全部，这是安全的**：身份靠显式传的 key、从不靠 cookie，浏览器不会自动附上，所以没有 CSRF 面。配错的表现是客户端 **`fetch failed`**，服务端零痕迹 |
| `DESKCORE_ALLOW_ANONYMOUS` | ⛔ 仅本地 | 设 `1` 才允许免 key 访问。**生产绝不能设** —— service_role 绕 RLS，匿名 = 全租户数据开放 |

> 上面两条口径 `/health` 的 `config` 里会回显（`mcp_allowed_origins` /
> `mcp_allowed_hosts`）。这不是可有可无的排查便利：它俩配错时**服务端一行日志
> 都不会有**，报错全在客户端那头，而且报的是传输层错误。回显出来才对得上。

`DESKCORE_KEYS` 格式（**一人一把**，key 映射成 `user_id`，这是"个人风格私有"的前提）：

```json
{"k-ziao-xxxx": {"user_id": "<uuid>", "name": "Ziao"},
 "k-xiaoyu-yyy": {"user_id": "<uuid>", "name": "小鱼"}}
```

> ⚠️ **判据是「这把 key 背后是不是同一个人」，不是「UUID 在不在库里」。**
>
> - **老人**（库里已有他的项目/稿子）→ **必须用他自己那个已有 UUID**，否则
>   `open_project` 里的个人正负例、`my_style` 的调校笔记全都看不到，个人层等于关着。
> - **新人**（库里没有任何历史）→ **可以给一个全新 UUID**，他从空的个人层开始积累，
>   这是对的。
> - ❌ **绝不能把两个人塞进同一个 UUID** —— 那等于把 A 的调校笔记和正负例交给 B 看，
>   而且 B 能改 A 的标注（`label_example` 的归属校验是按 `user_id` 判的）。
>
> ⚠️ **2026-08-24 起还多一层**（审计 COR-015）：`user_id` 现在还决定**能打开哪些项目**——
> 判据是 `projects.owner_id == user_id`，十五个工具全部校验（`create_project` 不校验已有项目，它**恒以调用者为 owner** 建新的）。所以给新人发一个全新 UUID
> 意味着他**一个项目都打不开**，得先让他自己建项目（或者把他要用的项目的 `owner_id`
> 改成他）。口径与"改成团队共享要改哪儿"见 `docs/deskcore.md` §2.2.1。
>
> **为什么不是 RLS 的事**：deskcore 持 `service_role`，**绕过 RLS**，个人层是靠
> `store.py` 里一串显式 `.eq("user_id", user_id)` 隔离的（:143 / :471 / :499 / :524 /
> :542 / :553），`label_example` 还另做了一次归属校验（`core.py:990` 的 docstring 写了
> 为什么）。TV `autowriter-migrations/RUNBOOK.md:150-153` 那次 RLS 屏蔽事故是
> **TV sync + Streamlit** 那条路径上的，机制不同，别把结论搬过来。
>
> 查已有身份：
> ```sql
> select user_id, count(*) from autowriter.items group by user_id order by 2 desc;
> select distinct owner_id from autowriter.projects;
> ```
> 当前 items 最多的三个 `user_id`：`85f5f888…`(3,385) · `afbaf84e…`(667) · `b907ec9d…`(504)。

#### key 轮换（怎么换、为什么这么换）

key 一旦在聊天窗口、截图、工单里出现过，就当它已经泄露 —— deskcore 持
`service_role` 绕过 RLS，一把有效 key 等于那个人名下**全部项目数据 + 十五个
工具（含写）**。轮换是唯一的补救。

**第一步：本地生成，不要让任何人替你生成。**

```bash
python - <<'EOF'
import secrets
for who, uid in [
    ("quanquan", "85f5f888-a649-4843-8358-9c83c91282e6"),
    ("jiayi",    "afbaf84e-e5a3-4429-9490-72718d2a9019"),
    ("ziao",     "b907ec9d-9e49-4dac-89ad-220eb53afc38"),
]:
    print(f'  "k-{who}-{secrets.token_urlsafe(24)}": '
          f'{{"user_id": "{uid}", "name": "{who}"}},')
EOF
```

**为什么必须是你自己在本地跑**：让别人（包括 AI 助手）生成，新 key 就又一次
经过了聊天记录、日志和上下文窗口 —— 那是刚刚要修的那个洞。这段脚本只吐字符串，
不联网、不落盘。

**第二步**：把输出拼成 JSON（去掉最后一个逗号），整体替换 Railway 上的
`DESKCORE_KEYS`，等服务重启。

**第三步**：`curl -s <服务地址>/health | grep auth` 确认 `note` 还是 `3 key(s)`。
数目对不上就是 JSON 写坏了 —— 注意 `_key_map()` 是 **fail-closed** 的：
解析失败一律 401，不会退化成"没配"（那曾经等于匿名全开，见 ROB-003）。

**第四步**：把新 key 分别发给本人，**一人一把，不要群发**。旧 key 在第二步
替换的瞬间就失效了，不需要额外撤销动作。

⚠️ **`name` 字段只是给人看的备注，不参与鉴权**。2026-08-27 踩过一次：一把叫
`k-ziao-…` 的 key，`user_id` 指向的其实是同事的账号 —— 拿它写稿会把稿子记进
**别人**的历史库。轮换时对着上面 §「owner 分布」那张表逐个核 UUID，别照抄
旧配置里的名字。

> ⚠️ 少写一层（`{"k": "<uuid>"}`）是**合法 JSON**，`identity.py` 会当成配置错误抛
> 可读的 401——不会漏到运行期变成 500。JSON 整个写错也一律 401，**不会**退化成
> "没配鉴权"然后放行（这条曾经是 fail-open 的）。

部好之后第一件事：

```bash
curl -sS "$DESKCORE_URL/health" | jq
```

⚠️ **别只看顶层 `ok`。** 它只由三项决定（`app.py:176`）：

```python
"ok": db_ok and vocab_ok and auth_ok
```

**embeddings 和 librarian 都不在里面**，而且这两项的回显只是「配没配」，不是「通不通」：

| 字段 | 它真正说明的 | 它**不**说明的 |
|---|---|---|
| `config.embeddings.ok` | google-genai SDK 装了、client **构造得起来**（`clients.py:107` `get_genai_client() is not None`） | key 有没有效、有没有欠费、配额用没用尽。**这三种情况它照样 `true`** |
| `config.librarian.configured` | `LIBRARIAN_URL` 这个环境变量非空 | 地址对不对、key 对不对、服务活没活 |

也就是说：跟着 `/health` 全绿走下去，`borrow_lessons` 可能一直静默返回空列表，
`backfill` 可能一条向量都没取到。所以这两项要**各自实打实探一次**：

```bash
# librarian 真的通不通 —— 看返回里有没有卡, 不是看 configured
curl -sS -X POST "$DESKCORE_URL/tool/borrow_lessons" \
  -H "X-Deskcore-Key: $KEY" -H 'content-type: application/json' \
  -d '{"project_id":"<uuid>","draft_topic":"测试"}' | jq

# embedding 真的取得到 —— 看 backfill 的返回, 见第 2 步
```

### 第 2 步 · 回填历史指纹 ✅ **已完成（2026-08-26，见 §1.5）**

> 当前生产库这一步已经做完了。下面这段留着是给**下一套库**用的——口径、坑和验证方式都在这儿。

```bash
python -m deskcore.cli backfill --project <uuid>    # 每个项目跑一次
```

迁移建的是**空表**，`check_drafts` 只读这张表、只有 `commit_drafts` 会往里写。
不回填的话，上线第一天号称"比对全量历史"的硬闸手里**一条历史都没有**——
几年老稿子对它全是新的，重复原样放行。

**建议只回填你真要用的那两三个项目**，61 个全跑没必要。

幂等（按 `version_id` 跳过），可以反复跑。

**怎么算回填完了**——⚠️ **不能**看 `select count(*) from autowriter.draft_fingerprints > 0`，
那是**全局**的；也不能看 `empty_history_warning` 消失，它的条件是
`if not history`（`core.py:488`），**这个项目有第 1 条指纹它就不见了**。
两个都会在「主力项目还差几百条」的时候报绿。

按项目逐个比对**该项目应有的条数**：

```bash
python -m deskcore.cli doctor --project <uuid>
```

`还差` 归零才算完。

> 这里原来给的是一段手写的三表 join SQL。换掉它是因为那是把回填的口径**抄了
> 第二份**——`backfill` 取的是 `store.legacy_version_pages` 那一套，抄出来的
> SQL 一旦和它漂开，验收标准就会在"主力项目还差几百条"的时候报绿。而验收标准
> 报绿正是本仓最怕的那类失败（§5 整节）。`doctor --project` 直接调回填自己用的
> 那两个函数（`existing_fingerprint_version_ids` + `legacy_version_pages`），
> 两者不可能漂；`tests/test_migration_doctor.py` 有一条 AST 断言钉着这件事。

另外看 `backfill` 的返回值：`missing_embeddings` 不为 0
就是**该有向量却没取到**（key 欠费/配额用尽——`embeddings_available()` 那时仍是
`true`，见第 1 步），那批行只有确定性指纹，补好 key 之后要跑 `reembed`。

> ⚠️ **先配好 `GOOGLE_API_KEY` 再回填。** 没配也能回填，但那批行没有标题向量、
> 只参与确定性查重；补配之后重跑**不会**给已写入的行补向量（幂等是按 `version_id`
> 跳过的），得先把这些行删掉再跑。
>
> ⚠️ **2026-08-24 起还多一步——但这次上线用不上。** `fingerprint.normalize` 的
> 口径改了（原来不去中文弯引号 `“ ”` 和 `【】`，是一条能绕过查重的路子，审计
> COR-014 后续）。**只有在那之前回填过的项目才要补跑重算**——而本库的 3,678 行
> 指纹全部是 **2026-08-26 回填的**（§1.5），已经按新口径算，所以**一个项目都不用
> 跑**。留着这段是因为以后再改 `normalize` 时还会用到：
> ```bash
> python -m deskcore.cli recompute-fingerprints --project <uuid>
> ```
> 从没回填过的项目不用管——`backfill` 本来就按当前口径算。
> 返回值里 `ngram_unrecoverable` 不为 0 说明有些行的正文已经不在库里
> （WorkBuddy 经 `commit_drafts` 写的，`version_id` 为空），那部分的四字串修不
> 回来；开头指纹是全部修好的。
>
> 事后补救用 `reembed`（给**已在指纹库里、但当时没取到向量**的行补向量）：
> ```bash
> python -m deskcore.cli reembed --project <uuid>
> ```
> `backfill` 和 `reembed` 够得着的行不一样：`backfill` 扫的是 `items × versions`，
> 而 WorkBuddy 写进来的稿子 `version_id` 是空的、根本不在那张表里——那些行
> **`backfill` 永远够不着**，只能靠 `reembed`。

### 第 3 步 · 挂上去

Claude Code（先在这儿验）：

```bash
claude mcp add --transport http deskcore <url>/mcp --header "X-Deskcore-Key: k-xxx"
```

WorkBuddy，项目级 `mcp.json`：

```json
{"mcpServers": {"deskcore": {"url": "https://<你的>/mcp",
                             "headers": {"X-Deskcore-Key": "k-xxx"}}}}
```

**skill：从本仓直接装，别手工复制。**

```
TangVarie/autowriter → skills/bywood-writing-desk
```

WorkBuddy 支持从 GitHub 装 skill，用这条。手工复制那份**会悄悄过期**——skill
改了之后没有任何东西提醒你去同步，而它管的是流程纪律（必须先 `open_project`、
`p0` 原样带进上下文、`check_drafts` 判 reject 的不许交付），过期的代价是模型
按老规矩写而没人发现。**又是同一种病，这次不给它机会。**

（CodeBuddy 的 skill 目录是 `.codebuddy/skills/`；它要是不支持从仓库装，那份
拷贝就要自己记着同步。）

#### 连不上时按这个顺序查

客户端报的错基本分两类，**先分清是哪一类再动手**：

| 客户端报什么 | 说明什么 | 怎么办 |
|---|---|---|
| `401 missing X-Deskcore-Key` | 请求到了服务器，只是没带 key | header 没生效 → 换 `Authorization: Bearer k-xxx`，再不行用 `<url>/mcp?key=k-xxx` |
| `401 invalid X-Deskcore-Key` | key 抄错，或不在 `DESKCORE_KEYS` 里 | 对着 `/health` 的 `auth.note` 数一下几把 key |
| 返回空列表（不报错） | key 通了但 `user_id` 配错 | 必须是库里已有的 `projects.owner_id`，编的 UUID **不报错只返空** |
| `fetch failed` / `TypeError` | **请求根本没到服务器**，或到了但浏览器不让读 | 见下 |
| `421 Misdirected Request` | `DESKCORE_ALLOWED_HOSTS` 配了但没包含线上域名 | 对着 `/health` 的 `mcp_allowed_hosts` 改 |

`fetch failed` 这一类要先把「机器出不去」和「CORS」分开，一条命令就够：

```bash
curl -i https://<你的>/mcp        # 期望: 401 + {"detail":"missing X-Deskcore-Key"}
```

- **拿到 401** → 机器到服务器是通的，问题在 CORS 或客户端配置
- **超时 / 证书错 / 连不上** → 是本地网络或代理，跟本服务无关

确认是 CORS 那一路的话，再打一次**预检**（浏览器在真请求之前发的那个）：

```bash
curl -i -X OPTIONS https://<你的>/mcp \
  -H "Origin: http://localhost" \
  -H "Access-Control-Request-Method: POST" \
  -H "Access-Control-Request-Headers: content-type,x-deskcore-key"
```

必须看到 `access-control-allow-origin`。**看不到就是服务端的问题，不是客户端的**
——预检按规范就不带自定义头，所以它不带 key；这一层要是被鉴权拦掉，客户端
只会看到一个传输层错误，完全看不出是鉴权的事。详见 §5.7。

### 第 3.5 步 · 飞书表先建好那六个 lineage 列（**只影响交付那一段**）

`export_drafts` 导出的 Excel 带 6 个**命名可见列**，列名由 TV 定
（`exporter.LINEAGE_COLUMNS` 是唯一真源，与 TV
`scripts/sync_feishu_notes_to_truth_vault.py` 的映射表对得上）：

| 列名 | 飞书列类型 | 去处 |
|---|---|---|
| `_source_autowriter_project_id` | 文本 | `notes.raw_extra` |
| `_source_autowriter_batch_id` | 文本 | `notes.raw_extra` |
| `_source_autowriter_item_id` | 文本 | `notes.source_autowriter_item_id`（FK） |
| `_source_autowriter_version_id` | 文本 | `notes.source_autowriter_version_id`（FK，`v_model_comparison` JOIN 它） |
| `_ai_engine` | 文本 | `raw_extra`；TV 按它 GROUP BY 出模型胜率 |
| `_exported_at` | **日期** | `raw_extra` |

⚠️ **列名逐字相同，写错的代价不是丢一列**：未声明的列会让 TV 的 D-021 把**整行**
quarantine，那条笔记连正文带指标一起进不了库。`export_drafts` 的返回值里直接带
`columns`，不必翻文档。

这一步不做不影响写稿，只影响"发出去之后爆没爆"能不能回流——可以等第 4 步验完
再补，但**别忘了**，`v_model_comparison` 长期查出空集就是因为这一段一直没通。

### 第 4 步 · 端到端验一轮

挑一个真实项目（建议 `RIO便利店调酒` / `b3ac2a5f-320b-49e5-9f11-15d6e517ec41`，
104 条，体量适中；owner 是 `b907ec9d…`，`DESKCORE_KEYS` 里要有这把 key），完整跑：

```
open_project → draw_angles(n=20) → borrow_lessons → 生成 20 篇
             → check_drafts → commit_drafts → export_drafts
```

然后验六件事：

1. 给一条"以后都这样"的反馈 → `record_rule(severity='hard')` → **下一轮生成时它出现在 P0 里**
2. 手改一篇 → `record_edit` → 提炼 → `save_my_style` → **`my_style` 能读到变化，换个 key 看不到**
3. 故意塞两篇近义改写进 `check_drafts` → **被 reject 且指出撞的是哪一条**
4. 拿历史上被投诉重复的那批稿子回灌 → **新查重能抓出来**（这是唯一能证明"确实修好了"的测试）
5. `commit_drafts` 的返回值里 **`batch_id` 和 `version_ids` 都不为空**，且**没有** `identity_warning`
   —— 有 warning 说明指纹写进去了但 `batches`/`items`/`versions` 没建成，lineage 会断
   （可逆，重跑即可，但别当没看见）
6. `export_drafts` 出的表**粘进飞书之后**，去 TV 那边查一次
   `select count(*) from truth_vault.notes where source_autowriter_version_id is not null`
   —— **这一列长期是 0**，它第一次不为 0 才说明回程真的通了

⚠️ 第 5、6 条是 2026-08-25 新增的那一段（`commit_drafts` 建身份 + lineage 命名列）
的验收，2026-08-23 版的清单里没有它们。

---

## 3. TV ↔ aw 对接矩阵

| 方向 | 通道 | 谁调谁 | 配置 | 失败时 |
|---|---|---|---|---|
| TV → aw | `borrow_lessons` 借爆款经验卡 | deskcore 调 TV librarian `POST /librarian` | `LIBRARIAN_URL` + `LIBRARIAN_API_KEY` | 返回空列表，**照常写稿**。飞轮永远不是写稿的前置依赖 |
| aw → TV | 人工审稿决定归档 | TV 的 `sync_autowriter_decisions_to_prepublish.py` 每天读 `autowriter.items` 的 `status` | TV 侧 secrets，本仓不用管 | TV 侧 daily-sync 报红发邮件 |
| ⚠️ 同上 | **仅对 Streamlit 时期的存量成立** | deskcore **不写** `items.status` | — | 停了 Streamlit 就没有新决策进 TV，见下 |
| 共库 | 同一个 Supabase 项目 `kduysqedr` | `truth_vault` / `autowriter` 两个 schema | — | — |

**跨仓约定（重要）**：本仓如果改 `items` / `batches` / `versions` 的列，
**必须通知 TV**——TV 那边 `sync_truth_vault_baokuan_to_autowriter_items.py`、
`extract_negative_examples_from_autowriter.py`、`sync_autowriter_decisions_to_prepublish.py`
和跨 schema 视图都依赖这些列。症状是 TV 的 CI SQL apply 步骤变红，或运行时
PostgREST 报 `column does not exist`。

**TV 侧刚做的一件相关改动**（TV PR #105，D-043）：`sync_autowriter_decisions_to_prepublish.py`
的时间窗已经改成 `created_at >= since OR updated_at >= since`，用的就是本仓
`migrations/001_deskcore.sql` 加的 `items.updated_at` + 触发器。所以**触发器的
`WHEN` 子句不能随便改**——它现在是 TV 那条归档链路的依据：

```sql
WHEN (old.status IS DISTINCT FROM new.status
   OR old.example_label IS DISTINCT FROM new.example_label)
```

### ⚠️ 停 Streamlit 会把 aw → TV 这条链路断掉

把 deskcore 的**全部写操作**列出来（`store.py` 里所有 `.insert/.upsert/.update/.rpc`）：

```
angle_ledger · draft_fingerprints · user_calibration_notes · style_edits
deskcore_reserve_angles · deskcore_commit_fingerprints
items.example_label（只有 label_example 这一个，走 db.set_item_example_label）
```

**没有任何一个工具会写 `items.status`。** 而 TV 那条归档链路读的正是
`status ∈ (approved, needs_revision)`。

> **2026-08-25 起这条只对了一半。** `commit_drafts` 现在**会**建
> `batches` / `items` / `versions`（§ `docs/deskcore.md` 3.4），所以"新写的稿子
> 不进 `autowriter.items`"已经不成立了。但它建的是 `status='pending'`、决策三列
> 全空，**刻意不标 approved**——标了就等于让每条定稿变成一条伪造的人工评价去
> 校准 TV 的评估模型（而写作台没有"打回"这个动作，灌进去的会是清一色正例）。
>
> 也就是说：**下面这三条路的选择题仍然没解，只是选项 A 做了一半。**

所以 Streamlit 一停，新写的稿子虽然进了 `autowriter.items`，却永远停在 pending，
不会产生 approved/needs_revision —— `prepublish_evaluations` 从此不再有新行，
**而 TV 那边不会报错**（查不到就是 0 条，跟"这几天没人审稿"长得一模一样）。

顺带一个连带效应：停服之后 `items.updated_at` 唯一还会被刷的来源就是
`label_example` 改正负例标注，于是 TV 打印的「创建后被动过」会**全部**是标注活动。

**三条路，第一期之前必须选一条**（列进待办 #5 的前置）：

| 选项 | 做什么 | 现在到哪一步了 |
|---|---|---|
| A · deskcore 补写回 | `commit_drafts` 时建 item/version（**✅ 已做**），再加一个能落 `status` 的动作（❌ 没做——写作台没有"审稿"这个动作，要新增一个工具或换个语义） | 半做 |
| B · 换一条归档源 | 让 TV 改读 `draft_fingerprints` + 一个新的决策表 | 没动，跨仓 |
| C · 明确接受断掉 | 把这一行标成 legacy-only，`prepublish_evaluations` 冻结在存量 598 条 | 没动 |

**在选定之前不要停 Streamlit。**

⚠️ **另有一条跨仓待办卡在 TV 那边**（本仓改不动）：`migrations/006` 加的
`decision_source` / `reviewer_id` / `decided_at` 三列，**TV 至今一处都没读**
（2026-08-26 在 truth-vault 全仓 grep 过，零命中）。也就是说 COR-004 的修复只做了
生产侧：本仓现在诚实地记下了"这个状态是机器判的还是人判的"，而
`sync_autowriter_decisions_to_prepublish.py` 仍然把两者一起当人工反馈灌进
`prepublish_evaluations`。**训练标签污染还在，只是现在有办法分辨了。**
TV 侧要做的是在那条 sync 上加一句 `.eq("decision_source", "human")`（外加对存量
NULL 行的口径决定）。

---

**反向依赖**：`items.updated_at` 只有跑过本仓 `migrations/001_deskcore.sql` 的库才有——
TV 自己那份建库脚本 `autowriter-migrations/007_fresh_install_autowriter_schema.sql`
里的 `items` 只有 `created_at`。TV 那个脚本对【且仅对】"没有这一列"降级回旧口径
（只按 `created_at`）并大声告警，不会把归档链路打死；但降级之后迟到的人工决策
会重新开始漏收。**所以新装一套库的时候，001_deskcore 要记得跑。**

> 顺带记一笔口径：TV 那边打印的「创建后被动过」是按 `updated_at > created_at` 数的，
> 因为上面那个 `WHEN` 对 `status` 和 `example_label` 都刷时间戳，**它不等于
> 「迟到的审稿决定」有多少条**。库里没有 `status` 专属的时间戳，想要更细的口径
> 得先加一个。

---

## 4. 待办

### P0 · 挡着上线的

| # | 事 | 为什么 | 验收标准 |
|---|---|---|---|
| 0 | ~~补齐迁移 `002`–`006`~~ | ✅ **2026-08-26 已完成**（见 §1.3）。六个全跑进生产，逐条验过；`003` 的快照表 `versions_num_backup_20260826` 还留着，确认无误后可 drop | — |
| 1 | ~~部署 deskcore service~~ | ✅ **2026-08-26 已完成**。Railway，`/health` 每项 ok、13 个工具都在 | — |
| 2 | ~~回填指纹~~ | ✅ **2026-08-26 已完成**（§1.5）：3,678 行 / 44 个项目。⚠️ **只覆盖 `85f5f888` 和 `afbaf84e`**——见下一行 | — |
| 2b | ⚠️ 给 `b907ec9d` 发 key **之前**必须先回填它 | 它那 504 条历史稿现在对查重**不可见**：一接上来，老稿重发会被当成新的放行 | `doctor --project <uuid>` 的「还差」归零；且 `backfill` 返回的 `missing_embeddings` = 0。⚠️ **不能**拿全局 `count(*) > 0` 或「`empty_history_warning` 消失了」当验收——两个都会在主力项目还差几百条时报绿 |
| 3 | 验 WorkBuddy 的鉴权头 | 不通就要换形态 | MCP 握手成功、错 key 返 401 |

### P1 · 上线后第一周

| # | 事 | 为什么 |
|---|---|---|
| 4 | **把真正的硬规则设成 `hard`** | 现在 303 条记忆**全是 soft**，P0 硬约束层是空的。"规则不忘"这个卖点在有 hard 规则之前等于没生效。先从禁词、合规话术这类开始 |
| 5 | 老 Streamlit 工作台停服 | 决策是"停服但不删仓，Supabase 一行不动"。两套同时开着会让指纹库漏记（Streamlit 写的稿子不走 `commit_drafts`）。**⚠️ 前置**：先在 §3「停 Streamlit 会把 aw → TV 这条链路断掉」的 A/B/C 三条里选一条 —— 否则人工审稿决定从此不再进 `prepublish_evaluations`，而且不报错 |
| 6 | 观察 `borrow_lessons` 选卡质量 | TV 书架规模下的选卡准确率**从来没人测过**。不行就在 `librarian/core.py:33` 那个 `CANDIDATE_CAP=50` 的口子加 embedding 预筛 |
| 7 | 让 `check_drafts` 的降级信号被人看见 | `semantic_degraded` / `empty_history_warning` 这两个字段没人盯的话，表现就是"查重跑了、全 pass、看着一切正常" |
| 7b | **推 TV 那边读 `decision_source`** | 跨仓。`migrations/006` 的三列 TV 至今一处都没读（2026-08-26 全仓 grep 零命中），所以机器判定仍在被当人工反馈灌进 `prepublish_evaluations`。本仓这一侧已经做完了，卡在 TV |
| 7c | 打通 lineage 回程 | 飞书表建好那六列 + 真导一次 + 在 TV 查 `notes.source_autowriter_version_id` 非空条数。这一列长期是 0，`v_model_comparison` 因此长期查出空集且不报错 |

### P2 · 攒够再做

| # | 事 | 触发条件 |
|---|---|---|
| 8 | 查重搬到 pgvector 服务端 | **单项目逼近 4,000 条指纹时**（不是十万）。`store.fingerprints` 的 `limit` 就是 4,000（`store.py:295`），超过就只比最近的这些、返回里带 `history_truncated_warning`——也就是说**第 4,001 条起，「比对全量历史」这个说法就不成立了**。`draft_fingerprints` 已建 ivfflat 索引，改起来不难；来不及就先把 cap 调高 |
| 9 | commit 的原子重查覆盖语义信号 | 现在锁内只查确定性信号（开头精确 + 四字串 Jaccard），标题语义相似度没查——竞态窗口里"换个说法的同角度稿"仍可能两条都进。等 backfill 把历史向量补齐之后再做 |
| 10 | "把我的调校笔记提升为项目基线" | 同一项目两人各自驯化会让风格分叉。指纹库共享、笔记不共享。跑一段时间看分叉严重程度 |
| 11 | 拆 `db.py` / `memory.py` / `app.py` | TV R-020，触发式延后。真在这些文件里频繁改动时才做 |
| 12 | 给 native 正例补 essence 标注 | 运营手标的正例没有 `external_source_id`，join 不到 `truth_vault.notes`，TV 的饱和度监控对它们只能报"无法评估" |
| 13 | `projects` 加 `UNIQUE (owner_id, lower(btrim(name)))` | `create_project` 的撞名保护是**应用层 check-then-insert**，两次并发调用会各自查空、各自建成。判据（大小写与首尾空格都不算差异）已经和这个索引对齐，加索引就是把它下推给数据库——与 `docs/deskcore.md` §2.3-D「并发正确性交给数据库」同一口径。**做之前要先处理存量可能已有的重名行**，所以单独一次迁移。现网并发建项目的概率极低，不阻塞 |
| 14 | `store.recent_angle_keys` 也走 `_paged` | 它读 `angle_ledger` 仍是裸 `.execute()`，同 COR-005/006/008 那一族的静默截断。台账被钳短的后果是**已经用过的角度组合会被当成没用过再抽一次**，跨批次去重悄悄退化。现网台账还很小，等它长起来之前做掉 |
| 15 | `/health` 回显注入封顶 | **纯便利, 不是唯一手段** —— `open_project` 的返回里已经有 `counts.soft_rules_cap_per_scope`(`deskcore/core.py:330`, 直接取自 `MAX_INJECTED_MEMORIES_PER_SCOPE`), 拿一把 key 和一个 project_id 就能确认线上生效值。放进 `/health` 的好处是**不需要 key、不需要 project_id**, 改完 env 立刻能验。(初稿把这条写成「线上无法验证」—— 错的, codex review 指出, 已改) |
| ~~16~~ | ~~`memories.embedding` 回填~~ | ✅ **2026-08-28 两个入口都就位**：① MCP 工具 `reembed_my_rules` —— 写手/运维在 WorkBuddy 里一句话触发, 一次 50 条, 只补调用者自己名下的, `remaining` 归零为止；② CLI `python -m deskcore.cli reembed-rules --user <uuid>`。⚠️ **只管存量, 不是给「新规则没向量」兜底** —— 新规则本来就有向量：`db.upsert_memory` 在写入时就算(`db.py:1748`)。缺向量的 174 条来自：① 2026-08-28 用**裸 SQL** 灌的 66 条技艺库种子(绕过了 upsert_memory, 见 §4.5)；② 更早那批建于这段代码之前、或当时没配 GOOGLE_API_KEY 的。按人分布：623346512 → 111 条、1796631194 → 44 条、tangziao1997 → 18 条、738443677 → 1 条(无孤儿行)。`my_rules` 的 counts 里会显示「缺向量：N」提醒 |

---

## 4.5 个人技艺库种子（2026-08-28 已投放）

把 27 份项目调校笔记（约 25,000 字、381 条工艺批注）聚成 37 簇、经三路独立
质疑分档之后，按人写进各自的 `scope='global'` 库。

**写法上的三个决定**，每条都对应一句产品要求（「技艺库要跟着写的人自己长，
不能靠一个不写稿的人在后端调」）：

| 决定 | 为什么 |
|---|---|
| `scope='global'`，不是项目规则 | 读路径里 global 是**私有**的（`.eq("user_id", user_id)`），队友互相看不见。这是个人技艺，不是团队规范 |
| **每条只写进真正记过它的那个人** | 簇的 `owners` 字段有记录。只有一个人记过的就只进那个人的库——写进别人库里就是「后端下发」 |
| `applicability` 一律留空 | 方向档的初值**推不出来**：37 簇是在没有方向维度的前提下聚的，连质疑者点名为「方向分支」的三对（结尾截断 vs 收口、去品牌标签 vs 嵌品牌名、编号分点 vs 叙事）项目集都是交叠的。填上去就是猜。第一个方向标签留给写手用 `set_rule_state set_direction` 自己打 |

**分档与落库形态**

| 档 | 判据 | 条数 | `status` |
|---|---|---|---|
| A | 三路质疑零异议 | 9 | `confirmed`（进简报） |
| B | 2/3 通过，一票保留 | 8 | `candidate` |
| C | 无多数 | 14 | `candidate` |
| D | ≥2 票判品牌方向特有 | 6 | **不入库**，留项目层 |

B 档没有直接设成 `confirmed`，是因为每条都带一票保留意见——让写手自己
`promote` 比替她转正更符合上面那句要求，也顺带避开了下面这个坑。

**实际落库**（`source_feedback='craft-seed-2026-08-28'`）

| 人 | 种子 | 其中生效 | 其中试用 | 她原有 | global 合计 |
|---|---|---|---|---|---|
| 623346512（圈圈） | 31 | 9 | 22 | 18 | 49 |
| 1796631194（佳怡） | 22 | 8 | 14 | 0 | 22 |
| tangziao1997 | 13 | 7 | 6 | 1 | 14 |

另有 2 簇的记录人是 `other`（匹配不上花名册），**没写**。

### 4.5.1 灌之前必须先调 cap —— 差点造成的一次事故

`db._rank_memories_for_injection` 对 **7 天内新建的规则一律优先**，然后砍到
`MAX_INJECTED_MEMORIES_PER_SCOPE`。圈圈有 18 条自己设了几个月的 global 规则，
cap 当时是 12：

| 灌入量 | 她的旧规则还剩几条进简报 |
|---|---|
| 9 条 | 3 条 |
| 31 条（原计划） | **0 条，持续 7 天，无提示** |

所以顺序是**先把 cap 调到 18（PR #72）并等部署落地，再灌种子**。调完之后
实测她的注入池是「9 条技艺种子 + 她自己票数最高的 9 条」，正好 18。

⚠️ 这也是为什么 B+C 档全进 `candidate`：22 条如果一起 `confirmed`，即使
cap=18 也会把她的旧规则重新挤空。

### 4.5.2 怎么撤

全部种子带同一个标记，撤销是一条语句：

```sql
delete from autowriter.memories
where source_feedback = 'craft-seed-2026-08-28';
```

只撤某一个人的，加 `and user_id = '<uuid>'`。**原始的 27 份笔记在
`projects.calibration_notes` 里一个字没动**——种子是 `memories` 的新增行，
两者互不影响。

---

## 5. ⚠️ 已知的「文档与实际不一致」

这一节单独存在，因为本仓反复栽在同一种坑上：**文字写着已经有了，实际没有，
而且不报错**。发现新的请追加到这里。

### 5.1 `versions.embedding` 生产库里一条都没有 —— 所以"复用历史向量"复用不到

`docs/deskcore.md` §4.1.1 原本写着「历史行有向量就**直接复用**，只有缺的才补算——
几千条重算既慢又费钱」。

**实测：`autowriter.versions` 5,532 行，`embedding` 非空的是 0 条。**

所以 backfill 时 `reused_embeddings` 会是 0，4,574 条标题向量**全部现算**。
量级不大（几分钟、几毛钱），但你要知道它会真的去调 API，而不是像文档暗示的那样大部分白拿。

**为什么会一条都没有**——这条路径上每一层都是静默的：

| 层 | 位置 | 失败时的行为 |
|---|---|---|
| 取向量 | `dedup.py:58` `embed_texts` | 没 client / API 报错 / SDK 形状变了 → **返回 `None`**，不抛异常、不打日志 |
| 调用方 | `app.py:522` | `if not new_vecs: return` → **整个 embedding + 查重段直接跳过** |
| 落库（批量） | `db.py:1339` `bulk_update_version_embeddings` | docstring 明写「errors are swallowed so an embedding failure never blocks the user's batch」 |
| 落库（单条） | `db.py:1313`（在 `update_version_content` 里） | `except Exception: pass`，注释写着「保持静默」 |

叠加上 `config.py:132` 的 `ENABLE_DEDUP_REGEN` **默认是 `"0"`**（查重命中了也不重生、
不拦，只写一条 UI 警告），以及 R-034 记过的 `_parse_pgvector` 缺失（PostgREST 把
`vector(768)` 序列化成字符串，不归一则 `cosine_similarity` 因长度不等**静默返回 0.0**，
"跨批语义查重的 DB 历史池自上线起 0 命中"）——

**结论（可查证的部分）**：老工作台的语义查重在生产里**没有历史向量可比**，
而且这条路径的每一层失败都不可见。这足以解释"跨批次越写越像"这个症状，
但不宜断言它是唯一原因——重复率还受提示词侧 `historical[-20:]` 截断、
正例池 recency top-5 趋同回路等因素影响（`docs/deskcore.md` §1.1 列了四个根因）。

**给 deskcore 的直接启示**：`check_drafts` **绝不 fail-open**（`deskcore/README.md`
三条纪律之一）就是冲着这个来的。查重挂了必须抛，不能静默放行。

### 5.2 `memories` 里没有任何 `hard` 规则

**实测：303 条记忆，`severity` 全是 `soft`，`hard` = 0。**

`SKILL.md` 里**曾经**写着「如果 `counts.hard_rules` 是 0 而用户以前明明定过规则，
说明可能传错了 project_id，问一句」——**不是传错，是真的一条都没有**。照那句执行，
模型每次开工都会怀疑 project_id 传错并问一遍。

> **2026-08-28 已改**（见 §5.8）。`SKILL.md` 和 `deskcore/tools.py` 的
> `open_project` docstring 两处都改成了「`hard_rules=0` 属于正常」，真正该起疑的
> 判据是 `soft_rules` 与 `soft_rules_pool` **同时**为 0。上面那句引文保留在这里
> 是为了记住这个坑长什么样，**不要照它去改代码**。

P0 硬约束层是「规则不忘」的实现机制，在有 hard 规则之前它是空转的。
见待办 #4。

### 5.3 `draft_fingerprints` 是空表 ✅ **已解决（2026-08-26）**

原文记的是"实测 0 行"。**已回填 3,678 行 / 44 个项目**，见 §1.5。

⚠️ **但只覆盖两个 owner，这条约束现在仍然成立。** `b907ec9d`（6 个项目 /
504 items）按"调教痕迹"的口径被跳过了，它的历史稿**至今没有指纹**：

> `check_drafts` 比的是**项目指纹**，跟这个 owner 有没有调教笔记毫无关系。
> 筛选口径当初是按"值不值得投入回填成本"定的，而**保护范围不是按那个口径
> 划的**——两件事被混过一次。所以只要 `b907ec9d` 拿到 `DESKCORE_KEYS` 里的
> 一个条目接上来，它那 504 条老稿子对查重就是隐形的：老稿重发会被当成新的
> 放行，而 `check_drafts` 只在 summary 里报一句 `empty_history_warning`，
> **不会拦**。
>
> **要么先给它跑回填，要么别给它发 key。**（codex review · aw#68）

### 5.4 历史教训：`backfill_fingerprints` 曾经根本不存在

CLI 子命令、设计文档、PR 描述、连"部署必跑一次"的措辞都写好了，唯独没写函数体，
`deskcore.cli backfill` 每次都 `AttributeError`——**回填这件事从头到尾没发生过，
而所有文字都写着它已经有了**。`py_compile` 抓不到（属性错误是运行期）。

现在 CI 有一步反射 `cli.py` 里所有 `core.xxx(` 调用、逐个断言存在。

留在这里是因为它是本节存在的理由：**别信文档，去查库、去 curl**。

### 5.5 `db.py` 的注释引了一个不存在的函数名（本次已修）

`db.py:1625` 的 docstring 写着「分页写法照搬本文件 `_fetch_recent_titles(:1462)` 那套」。

**全仓搜不到 `_fetch_recent_titles`。** `:1454` 上的真名是
`_collect_recent_canonical_versions`。

值得记一笔的是这条错误**已经传播过一次**：autowriter#57 的 PR 描述里照抄了这个名字和行号，
而那份描述是照着注释写的、没去查函数是否存在。这正是 truth-vault `DECISIONS.md` D-042
第 5 节记的那个错法——**按注释判断代码，而不是按代码**。

本次已把注释改成正确的名字和行号。**引用 `file:line` 之前先 grep 一下真的有没有。**

### 5.6 部署文档漏掉了一半迁移，而生产库其实只跑过 `001`（2026-08-26，已修）

三处同时不一致，任何一处单独看都不显眼：

| 哪儿 | 写的 | 实际 |
|---|---|---|
| 本文 §0 | 「schema 也上了生产」 | 只有 `001`。`002`/`003`/`004`/`005`/`006` 一个都没有 |
| `docs/deskcore.md` §4.1.1 | 「跑 `001`，以及后续的 `002` / `003` / `004`」 | 少写了 `005` 和 `006` |
| `migrations/README.md` 清单 | 只列到 `005`，并写「五个迁移都不跑也不会坏」 | `006` 没进清单，**而且它不跑就是硬失败** |

照着这三份文档部署的人会漏掉 `005`（短稿整段照搬长稿从此抓不到，而 `check_drafts`
只在 `summary` 里埋一句没人看的 warning）和 `006`（现有工作台的审稿按钮直接抛
PostgREST 原话）。**两边都不会有任何东西提醒他漏了**——又一次同样的形状。

三道处理，一道比一道靠前：

1. **文档改对**（三份都改了）；
2. **`python -m deskcore.cli doctor`** —— 此前没有任何办法一眼看出库跑到第几个
   迁移，只能手写 `information_schema` / `pg_proc` 查询。判据与真正消费这些迁移
   的代码同源（`store.rpc_missing`）；
3. **`tests/test_migration_doctor.py` 钉死**：`migrations/*.sql` 的每个文件名必须
   出现在本文、`docs/deskcore.md`、`migrations/README.md` 三份里，且必须被 `doctor`
   探到。钉的是【被禁止的形态】（新增迁移漏进文档 / 漏进 doctor），不是"代码现在
   长这样"——所以以后加迁移忘了写文档会**当场红**，而不是等下一次部署踩到。

顺带把 `db.update_item_status` 缺列时的报错翻译成人话（原来抛的是
`column "decision_source" ... does not exist`，点「通过」的人看到那句完全不知道
该做什么）。**刻意不降级**：去掉三列重试一次就等于让机器判定继续伪装成人工反馈
去污染 TV 的评估模型，正是 COR-004 要治的那件事。

### 5.7 浏览器侧的 MCP 客户端一个都连不上（2026-08-27，已修）

第一次真的往 WorkBuddy 上挂就炸了，报的是：

```
streamableHttp connect failed: fetch failed
sse connect failed: sse connect timed out after 12000ms
```

**SSE 那条可以忽略**——服务端是 `stateless_http=True`，压根没有老的 SSE 端点，
客户端两种传输都试是正常行为。真正的问题是 `fetch failed`。查出来是**两个**
独立的毛病，都属于本仓那个老病：*部署显示健康、curl 全绿、功能整个不可用*。

**一、整个服务一行 CORS 头都不发。**

浏览器/Electron 渲染层在发带自定义头的请求前，会先发一个 `OPTIONS` 预检。
预检**按规范就不带任何自定义头**，也就不带 `X-Deskcore-Key`——于是它一头撞进
鉴权中间件，拿到 401。而浏览器对「预检没通过」的报法是 `TypeError: fetch
failed`，**不是 401**。客户端那头看到的是一个传输层错误，怎么查都查不到鉴权
上去。

为什么一直没发现：**所有手工验证都绕开了 CORS**。curl 不做预检；浏览器地址栏
直接打 `/health` 是同源导航，也不做预检；REST 的 `/tool/{name}` 自测同样是
curl；CI 的冒烟测试用 TestClient 直接发 `initialize`，也不做预检。于是每一条
验证路径都是绿的，而真实客户端一个都连不上。

修法是加 `CORSMiddleware`，**必须注册在鉴权中间件之后**——Starlette 里最后注册
的跑在最外层，装在里面等于没装：唯一会被拦的请求（401）恰恰是最需要被客户端
读到的那个。`allow_credentials` 必须是 `False`：本服务的身份靠显式传的 key、
从不靠 cookie，浏览器不会自动附上，所以放开 origin 没有 CSRF 面；而且开了
credentials 时浏览器**拒绝**通配的 `Access-Control-Allow-Origin`，两者不能同时要。

**二、`/mcp` 不带斜杠时每次都先吃一个 307。**

`app.mount("/mcp", ...)` 建的 `Mount` 正则是 `^/mcp(?P<path>/.*)$`，光秃秃的
`/mcp` **匹配不上**，落到 Starlette 的 `redirect_slashes` 兜底：307 跳到 `/mcp/`。
而文档、CI、给客户端的地址写的全都是不带斜杠的那个。

平时看不出来，是因为验证用的东西**都自动跟随重定向**（`curl -L`、TestClient、
httpx 默认）。跨源时这一跳是独立的失败路径：预检是针对 `/mcp` 做的，跳到
`/mcp/` 后按 Fetch 规范要重新预检，各家实现对「预检过的请求能不能跟重定向」
处理并不一致——失败的报法同样是 `fetch failed`，同样零痕迹。

修法是一个纯 ASGI 中间件把 `/mcp` 就地改写成 `/mcp/`。**刻意不用**
`@app.middleware("http")`：那是 `BaseHTTPMiddleware`，会把响应体收进内存再吐
出去，对 MCP 的流式响应有害。

**三道闸，都钉的是被禁止的形态：**

1. `tests/test_deskcore_cors.py`（19 条）——预检不许 401、401 本身必须带 CORS 头、
   CORS 必须在 `user_middleware[0]`、通配 origin 与 credentials 不许同时开、
   `/mcp` 不许 307（`follow_redirects=False` 是这条的全部意义，默认跟随的话 307
   和 200 看起来一模一样）。另有两条防回归：带 `Origin` 不等于免鉴权、
   `/mcpfoo` 这种前缀撞名的路径不许被顺手改写。
2. **CI 的 deskcore 冒烟测试加了预检那一段**——就接在原来那两条
   （lifespan / 421）后面，因为这是同族的第三个。
3. `/health` 现在回显 `mcp_allowed_origins` / `mcp_allowed_hosts`。
   `_register_mcp` 的注释里原本就写着「`/health` 会回显当前生效的口径」，
   **而实际上没有**——写了但没实现，又是一次"说有其实没有"，一并补上。

### 5.8 新品牌根本进不来，而任何一份文档都没写（2026-08-27，已修）

团队第一次真用起来就撞上了。要给六个新品（sportsix / 西屋 / 雷诺考特 / 百健士藻油 / 岸深冲牙器 / 途鸽）开项目，模型如实回答：

> 这六个我这边都建不了——不是没努力，是 deskcore 这套工具里压根没有「新建项目」这个动作。

它没说错。建项目的代码只在 `projects.py`（Streamlit 页面）里，MCP 这侧一个入口都没有；而 skill 又强制「动笔前必须先 `open_project`」。**想建建不了，想跳过又不让跳。**

这个缺口的性质值得记一笔：它不是 bug，是**整条路径从没被走过**。上线检查表里每一项都绿——迁移跑完了、`/health` 全绿、12 个工具都在、端到端验过一轮——因为那轮验证用的是**已经存在的项目**。「新项目怎么来」这一步在任何一份文档、任何一条 CI 断言里都不存在，于是它既没被实现也没被发现，直到有人真要开新品。

修法与三条设计决定见 [`docs/deskcore.md` §3.0](deskcore.md)。CI 的 deskcore 冒烟现在会断言 `create_project` 在 MCP 工具表里、且 schema 里**没有任何归属参数**。

顺带修了 `SKILL.md` 里一句会**每次都误报**的话：原文写着「如果 `hard_rules` 是 0 而用户以前明明定过规则，说明可能传错了 project_id，问一句」——而 §5.2 早就实测出 `hard` 全局为 0。照那句话执行，模型每次开工都会怀疑 project_id 传错并问一遍。现在改成明说 `hard_rules=0` 是正常的，真正该起疑的是 `soft_rules` 也为 0。

---

## 6. 在本仓迭代时要知道的

### 6.1 CI 里已有的闸（改代码前先看一眼会被哪条拦）

| 步骤 | 拦什么 |
|---|---|
| `deskcore selftest` | 查重硬闸 + 发牌 + vendor 词表完整性 |
| 迁移在真 PostgreSQL 上跑 | 空库 → `000` → `001..005` → 再跑一遍验幂等 → 抽查表/函数 → 下推 SQL 与 Python 逐例比对（审计 SUP-010/COR-014） |
| `pytest tests/` | 护栏（价档前缀 / 规则解析 / 笔记去重）+ **归属校验**（含一条 AST 断言：新加工具忘了加校验会红）+ 两条 `xfail(strict)` 立案（SUP-006 / COR-014） |
| round-5 回归 | fail-closed / 规则口径 / 触发器条件 |
| round-6 回归 | **回填函数存在** / 撞车归因 / 坐标块完整 |
| round-7 回归 | 空开头 / key map / 翻页 / 角度洗牌 |
| round-8 回归 | 蒸馏搬给调用方模型的两步闭环 |
| app 真的能起 | `/health` 不炸 + MCP 能握手 + 越权拒绝真的返 403 |
| 真实 import 图 | 抓依赖 API 漂移（不 import `app.py`，它会跑 streamlit 脚本） |
| supabase schema-aware client 契约 | TV 通道 2 sync 依赖 |
| `list_items_for_batches` 必须分页 | 假件带 `server_cap`，模拟 PostgREST 把请求钳短 |

### 6.2 三条不能破的纪律

1. **fail-open 只有三个工具，别扩大。** `_safe()` 包装会把异常变成一个**看起来成功**、
   只多一个 `error` 字段的结果，`hint` 里还写着"写稿可以继续"。目前只包了
   `list_projects` / `borrow_lessons` / `my_style` —— 读，且拿不到只是少点参考。

   **`open_project` 刻意没包**（`tools.py:25` 的 docstring 专门讲了原因）：拿不到 P0
   硬约束就照常开写，产出的是违规内容，而调用方看到的是一份 `p0` 为空的**正常简报**。
   `store.shared_memories()` 为此故意不吞异常，外面再包一层 `_safe` 等于把那个设计
   原样抵消掉。**写类工具**（`draw_angles` / `commit_drafts` / `record_rule` /
   `record_edit` / `label_example`）同理，全都不包。

   ⚠️ **`PermissionError` 是 `_safe` 的显式例外**（审计 COR-015）：归属校验的拒绝
   原样上抛，不包成降级结果。理由和上面同一条判据——权限拒绝不是"这次没拿到"，
   重试也一样，而包住它会让调用方模型继续拿同一个错 `project_id` 试下一个工具。
   REST 层映射成 **403**（401 = key 没过；403 = key 过了但项目不是你的；500 = 真故障）。

   `check_drafts` 更不能包 —— 查重出错必须抛，理由见 §5.1。

   判据一句话：**失败之后调用方还会不会当作成功继续往下走。会 → 不能包。**
2. **别在 `deskcore/__init__` 之前 import `db`。** 它要先设 `AW_DISABLE_ST_CACHE=1`
   （R-042，同 `worker.py:56`），否则 headless 进程会拿 `st.cache_data` 的 30-60s 旧数据。
3. **读 embedding 必须过 `db._parse_pgvector`。** 不归一则 `cosine_similarity` 静默返回
   `0.0`——查重变哑弹且不报错（R-034）。`store.py` 已在读取边界统一处理。

### 6.3 分页（2026-08-23 审计）

`fetch`/翻页类查询必须带**唯一且稳定**的排序键。`created_at` 在 bulk insert 下大量并列
（同一语句共享 `NOW()`），无序 OFFSET 跨页会漏行/重行，**而且不报错**。

终止判据是**空页**不是**短页**——PostgREST 的 `max-rows`（本库实测 **1000**）会把请求
钳短，按短页收工会静默截断。offset 按**实拿到的行数**前进。

`_collect_recent_canonical_versions`（`db.py:1454`）和 `list_items_for_batches` 是两个正确范例。

### 6.4 不要再立第四套语言规则

已经有三套并列口径了。分工：

| skill | 管什么 |
|---|---|
| `human-writing` | 这一篇读起来像不像人写的（去 AI 味、禁用句式、跨篇指纹） |
| `bywood-writing-desk`（本仓） | 这一批守不守项目规则、跟历史重不重 |
| `seeding-prompt-refiner` | 提示词本身怎么迭代 |

deskcore 落地后，`seeding-prompt-refiner/references/cross-batch-diversity.md` 里那四套
**用户手动维护**的补偿机制（角度编号库 / 历史回避清单 / 跨批次分布档案 / 账号差异化种子）
可以全部退役——它自己就标注了那是"过渡方案，不是长期方案"。

---

## 7. 相关文档

| 文档 | 在哪 | 讲什么 |
|---|---|---|
| `docs/deskcore.md` | 本仓 | 设计原理、工具面、为什么这么拆 |
| `skills/bywood-writing-desk/SKILL.md` | 本仓 | 写作台协议（给模型看的） |
| `deskcore/README.md` | 本仓 | 目录结构 + 三条纪律 + 快速自测 |
| `migrations/README.md` | 本仓 | ⚠️ 没有自动执行机制——SQL Editor 粘贴或 MCP `apply_migration`，建议先过 branch 库 + `get_advisors`。**加表/加列必须 `migrations/` 和 `db.py::CREATE_TABLES_SQL` 两边都改** |
| `DECISIONS.md` D-041 / D-042 / D-043 | truth-vault | 外置为 MCP 的决策 / 分页与互斥锁 / 迟到决策时间窗 |
| `docs/10-sister-repo-followups.md` R-034 | truth-vault | 跨仓待办总账 |
| `docs/15` / `docs/19` | truth-vault | librarian 接入说明与 quickstart |
