# deskcore 上线手册 · 对接 · 待办

> **这份文档的定位**：`docs/deskcore.md` 讲的是**为什么这么设计**，这份讲的是
> **现在到底是什么状态、还差什么、你下一步做什么**。以后主要在本仓迭代，从这里开始读。
>
> 快照时间：**2026-08-26**。所有数字都是当天从生产库（`kduysqedr`）和线上服务实测的，
> 不是照文档抄的。**改动之后请回来更新这些数字**，否则它就变成下一份"写着已经有了、
> 实际没有"的文档——本仓在这上面栽过好几次，登记在 §5。

---

## 0. 一句话现状

**代码全写完了、离线自测通过（216 条 pytest + selftest + 真 PostgreSQL 上的迁移与
SQL/Python 逐例比对全绿）。但服务没部署，六个迁移里生产库只跑过 `001`，四张新表
全是 0 行——所以整条链路一次都没真跑过。**

> ⚠️ **这一段 2026-08-23 版写的是"schema 也上了生产"。那不对。** 08-26 实测：
> `002` / `003` / `004` / `005` / `006` 在生产库上一个都没有（见 §1.2）。修这条
> 的同时补了 `python -m deskcore.cli doctor`——以后不必再手写 SQL 去问这个问题。
>
> 其中 **`006` 不跑是硬失败**：`db.update_item_status` 无条件写
> `decision_source` / `reviewer_id` / `decided_at`，缺列会让现有工作台的
> 「通过 / 打回」当场报错。它排在所有步骤最前面。

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

### 1.2 本仓（autowriter）——代码齐、库是空的

| 部件 | 实测结果 |
|---|---|
| `deskcore/` 代码 | 齐。`selftest` → **PASS**；`pytest tests/` → **216 passed, 1 xfailed**；`tests/sql_parity_check.py` 在真 PostgreSQL 上 → 基线 + 六个迁移叠起来、重跑幂等、SQL 与 Python 逐例一致，**全绿** |
| ⚠️ **schema** | ❌ 生产库**只跑过 `001`**。`002` / `003` / `004` / `005` / `006` 一个都没有——见下面那张表 |
| **服务部署** | ❌ **没有**。两个仓里搜不到任何 deskcore 的线上地址 |
| `draft_fingerprints` | **0 行** ← 查重硬闸背后一条历史都没有 |
| `angle_ledger` / `user_calibration_notes` / `style_edits` | **全 0** ← 没人用过 |
| `memories` | 303 条 |
| ⚠️ `memories.severity` | **303 条全是 `soft`，`hard` = 0** |
| `items` | 4,574 条 / 61 个项目 / 6 个 owner |
| `versions` | 5,532 行 |
| ⚠️ `versions.embedding` | **非空 0 条** |
| ⚠️ `(item_id, version_num)` 重复 | **339 对 / 678 行** ← `003` 会先把它们重编号再建唯一索引 |
| 最后一次真实使用 | 2026-08-19（item / batch / memory 的最新 `created_at` 都停在这天） |

**迁移状态逐条实测**（2026-08-26，`information_schema` + `pg_proc` 查的）：

| 迁移 | 生产库 | 判据 |
|---|---|---|
| `001_deskcore.sql` | ✅ 在 | 四张表都在；`items.updated_at` 在；`deskcore_reserve_angles` 在 |
| `002_calibration_cas.sql` | ❌ 没跑 | `update_calibration_notes_cas` 查不到 |
| `003_versions_unique_num.sql` | ❌ 没跑 | `versions_item_version_uniq` 索引不存在（且库里正好有 339 对重复） |
| `004_deskcore_check_pushdown.sql` | ❌ 没跑 | `deskcore_check_drafts` / `deskcore_fingerprint_counts` 都查不到 |
| `005_deskcore_containment.sql` | ❌ 没跑 | `deskcore_commit_fingerprints` 还是 **4 参**那版 |
| `006_item_decision_provenance.sql` | ❌ 没跑 | `items` 没有 `decision_source` / `reviewer_id` / `decided_at` |

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

### 第 0 步 · 补齐迁移（`002`–`006`），**先跑 `006`**

生产库只跑过 `001`（§1.2）。**`006` 排在最前面，因为它是唯一一个"代码已经发了、
迁移没跑就当场坏"的**：`db.update_item_status` 无条件写
`decision_source` / `reviewer_id` / `decided_at`，缺列时现有工作台的
「通过 / 打回」按钮、硬规则自动标记、查重自动标记全部报错。

跑法：Supabase SQL Editor 粘贴执行，或 MCP `apply_migration`。建议先在 branch 库
跑一遍 + `get_advisors` 核验再进 prod。顺序：

```
006 → 002 → 003 → 004 → 005
```

`006` 之外按编号顺序即可。**`005` 必须在 `004` 之后**（它 DROP 掉 `004` 建的那版
`deskcore_check_drafts` 再重建，跳过 `004` 会缺 `deskcore_fingerprint_counts`）。

⚠️ **`003` 会改数据**，不只是建索引：库里现有 **339 对 / 678 行**重复的
`(item_id, version_num)`，它先按 `(version_num, created_at, id)` 稳定重编号，再建
唯一索引。这是 `bulk_create_initial_versions` 给多引擎批次每个引擎都写
`version_num=1` 留下的历史（审计 §0.4 第 1 条），不是数据损坏。跑之前先备份/开
branch 库过一遍。

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

### 第 1 步 · 部署 deskcore service

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
| `DESKCORE_ALLOWED_HOSTS` | 可选 | 逗号分隔；设了才开 MCP 的 Host 校验 |
| `DESKCORE_ALLOW_ANONYMOUS` | ⛔ 仅本地 | 设 `1` 才允许免 key 访问。**生产绝不能设** —— service_role 绕 RLS，匿名 = 全租户数据开放 |

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
> 判据是 `projects.owner_id == user_id`，十二个工具全部校验。所以给新人发一个全新 UUID
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

### 第 2 步 · 回填历史指纹（**不做等于没上查重**）

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
> COR-014 后续）。**只有在那之前回填过的项目才要补跑重算**；而 `draft_fingerprints`
> 现在是 0 行（§1.2），也就是说这次首次上线**一个项目都不需要跑**。留着这段是
> 因为以后再改 `normalize` 时还会用到：
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

skill：把本仓 `skills/bywood-writing-desk/SKILL.md` 复制到
`~/.workbuddy/skills/bywood-writing-desk/SKILL.md`。CodeBuddy 放 `.codebuddy/skills/`。

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
| 0 | **补齐迁移 `002`–`006`，`006` 最先** | 生产库只跑过 `001`。`006` 不跑是**硬失败**——现有工作台的「通过 / 打回」当场报错 | `python -m deskcore.cli doctor` 报「迁移: 全部到位」；`003` 那条 `unprobeable` 自己用 SQL 确认一次 |
| 1 | 部署 deskcore service | 现在根本没跑 | `curl $URL/health` 每项 ok |
| 2 | 回填指纹（至少主力项目） | 不做的话硬闸背后 0 行历史 | `python -m deskcore.cli doctor --project <uuid>` 的「还差」归零；且 `backfill` 返回的 `missing_embeddings` = 0。⚠️ **不能**拿全局 `count(*) > 0` 或「`empty_history_warning` 消失了」当验收——两个都会在主力项目还差几百条时报绿 |
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

`SKILL.md` 里写着「如果 `counts.hard_rules` 是 0 而用户以前明明定过规则，
说明可能传错了 project_id，问一句」——**不是传错，是真的一条都没有**。

P0 硬约束层是「规则不忘」的实现机制，在有 hard 规则之前它是空转的。
见待办 #4。

### 5.3 `draft_fingerprints` 是空表

**实测：0 行。**

`docs/deskcore.md` §4.1.1 已经把"必做回填"写清楚了，这里只是记录**截至快照时
仍然没做**。见待办 #2。

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
