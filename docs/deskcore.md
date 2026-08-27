# deskcore · 写作台内核外置为 MCP

> 本文讲**为什么这么设计**。要看**现在是什么状态、还差什么、下一步做什么**，
> 去 [`deskcore-runbook.md`](deskcore-runbook.md)——上线手册、对接矩阵、待办清单，
> 以及一份「文档与实际不一致」的登记表。

> **给谁看**：要把写作能力挂到 WorkBuddy / Claude Code / CodeBuddy 的人；以及以后维护 deskcore 的人。
>
> **一句话**：Streamlit 界面停用，内核做成 MCP 工具继续用。库里几年的积累一条不迁。
>
> 决策在 truth-vault：`DECISIONS.md` D-041 · `docs/10-sister-repo-followups.md` R-034

---

## 1. 为什么

团队基本弃用工作台转用 WorkBuddy。两边缺陷互补：

| | 工作台 | WorkBuddy |
|---|---|---|
| 规则执行 | ✅ 提示词规则输入后严格执行 | ❌ 经常忘记之前定的规则 |
| 像不像本人 | ✅ 喂自己写的内容会慢慢变成"像我在说话" | ❌ 感觉持续在跟 AI 对话 |
| 操作成本 | ❌ 一个项目 5 个提示词要点 5 次，几十个提示词散在各项目 | ✅ 流畅 |
| 速度 | ❌ 太慢太卡 | ✅ 快 |
| 重复率 | ❌ 高 | ✅ 相对低 |

工作台的优势是四个**具体机制**（可搬），劣势是四个**具体工程缺陷**（可修）。deskcore 把两件事在同一个改动里做完。

慢的根因是 Streamlit：`app.py` 5560 行，每次交互全量重跑。能力外置后这个问题自动消失——推理归 WorkBuddy，deskcore 只做轻量数据操作。

### 1.1 重复率的四个根因

1. **硬闸默认关着。** `config.py:132` `ENABLE_DEDUP_REGEN` 默认 `"0"`。查重跑了、命中了，只写一条 UI 警告，**不重生也不拦**。
2. **只比标题，阈值过高。** `app.py:520` 只对 `title` 取向量；阈值 0.92（`config.py:140`）对标题 embedding 极高，换说法的同角度标题普遍落在 0.85–0.90 全部溜过。正文开头、场景、结尾完全不在查重范围。
3. **提示词侧只看最近 20 条。** `db.py:1556` 从库里捞 150 条 / 最近 40 批，`generator.py:1384` 一句 `historical[-20:]` 扔掉 130 条；剩下的还只是软指令（"宁可少出一条"），靠模型自觉。
4. **正例池是 recency top-5，构成趋同回路。** `db.py:2169` 是 `created_at DESC` limit 5。模型模仿最近 5 条正例 → 新稿被标 positive → 窗口滚动 → 语感越收越窄。

第 4 条此前**完全不可见**：TV 那边监控它的 `check_positive_saturation.py` 只统计 `external_source='truth_vault'` 的行，而 push 通道从没真跑过（那列生产库里全 NULL），所以它从上线起永远打印"没有正例"。这个回路跑了多久没人知道。TV 侧已修（`schemas/notes_v1_8`）。

另外 `_assign_slot_coordinates`（`generator.py:1301`，给每篇分配不同的角度×句式×词感）被移除成死代码。注释（`:1576-1584`）说理由是跟项目自己的 role 设定打架，并留了话："future iteration wants them back behind a per-project opt-in flag"。deskcore 的 `draw_angles` 就是按那条路复活的——**切入角度优先用项目自己的 `custom_roles`**。

---

## 2. 架构

```
WorkBuddy 项目 (mcp.json)      Claude Code / CodeBuddy
        │                              │
        └────── MCP over HTTP ─────────┘
                     │
        deskcore/ (独立 service, 同 worker.py)
                     │
   ┌─────────────────┼──────────────────┐
   │                 │                  │
本仓现有模块      migrations/001      TV librarian
db / memory /     四张新表            (HTTP, 借爆款经验卡)
dedup / clients /
librarian_client
```

### 2.1 复用而不是重写

deskcore 是**薄的**。本仓已有的一律直接调：

| deskcore 用到 | 来自 |
|---|---|
| 分层 prompt（stable/tactic/p0/p1/p2 + hard/soft） | `memory.build_layered_system_prompt` |
| service_role client（已带 `schema="autowriter"`） | `db.get_service_client` |
| 项目读取 / 标正负例 / 写规则 | `db.get_project` / `set_item_example_label` / `upsert_memory` |
| embedding 与余弦 | `dedup.embed_texts` / `cosine_similarity` |
| 借飞轮经验卡 | `librarian_client.build_brief` / `fetch_flywheel_lessons` |
| pgvector 反序列化 | `db._parse_pgvector` ⚠️ 见 §5 |

deskcore 自己只有六个模块：

| 模块 | 干什么 |
|---|---|
| `core.py` | 逻辑层（简报 / 发牌 / 查重 / 学习），不 import FastAPI/MCP |
| `store.py` | db.py 里**没有的查询形状**（共享层规则、带向量的正负例、四张新表） |
| `fingerprint.py` | 指纹与判定，**只用标准库** |
| `vocab.py` | 闭集：essence 来自 vendor 的 JSON，surface 引用 `generator.py` |
| `identity.py` | API key → user_id |
| `tools.py` / `app.py` / `cli.py` | MCP / HTTP / CLI 三个 adapter |

为什么新查询不塞进 `db.py`：R-020 已经把 `db.py`（3376 行）/ `memory.py` / `app.py` 标为"改动成本高的巨型文件"，再往里加只会更糟。

### 2.2 隔离口径

**项目规则团队共享 + 个人风格私有**（用户 2026-08-22 拍板）。

| 层 | 内容 | 存放 |
|---|---|---|
| **共享**（按 project_id） | 硬/软规则、项目 prompt 包、发牌台账、成稿指纹库 | `memories` / `projects` / `angle_ledger` / `draft_fingerprints` |
| **私有**（按 user_id） | 个人调校笔记、手动精修 diff、个人正负例 | `user_calibration_notes` / `style_edits` / `items.example_label` |

deskcore 持 service_role 绕 RLS，**由服务端自己执行口径**——`db.get_confirmed_memories` 是按 `user_id` 过滤的（Streamlit 单用户视角），共享规则读不到，所以 `store.shared_memories` 按 `project_id` 读全量。

#### 2.2.1 项目归属：按 `owner_id` 隔离（2026-08-24 拍板，审计 COR-015）

上面那张表回答的是"一个项目的数据分几层"，**不回答"谁能打开这个项目"**。这两件事被混过一次：deskcore 原来对 `project_id` 没有任何归属校验，当时的十一个工具里 `list_projects` / `borrow_lessons` / `check_drafts` 三个连调用者是谁都不问。于是任一持有效 key 的调用方传入他人 `project_id` 就能读他人成稿标题（`check_drafts` 的 `collided_with` 会回显）、往他人项目写规则和指纹。

现在的口径：**`projects.owner_id == 调用者 user_id`**，读写两侧都校验。这与 `db.py` 里那条 RLS policy `owner_id = (select auth.uid())` 是同一条谓词——deskcore 绕过 RLS，就得自己把它执行一遍。

| 谁 | 怎么执行 |
|---|---|
| 十二个 MCP 工具 | `TOOLS` 里 `needs_user` **全部为 True**；`core.assert_project_access` 是唯一实现，每个项目级入口以它开头 |
| `label_example` | **按 `items.user_id` 校验**，比项目粒度更细——同一项目里 A 的正负例是 A 的个人资产 |
| REST/MCP 层 | `PermissionError` → **403**（不是 401，也不是 500）。401 = key 那一层没过；403 = key 过了但项目不是你的；500 = 服务端真的坏了 |
| CLI 的 `projects` / `open` / `draw` / `check` | 走同一个 `core` 函数，所以 `--user` 从可选变成**必填** |
| CLI 的 `backfill` / `reembed` | **刻意不校验**——它们是运维命令，跑它们的人手里握着 service_role key（等价于直连库），加校验挡不住任何人，只会挡住"帮同事补一下指纹"，还会给人"运维路径也隔离了"的错觉 |

**要改成团队共享时改哪儿**：`core.assert_project_access` 的函数体（加一张 `project_members` 表就是把那个 `!=` 换成一次成员查询），调用方一行不动。判据刻意收敛成一处，就是因为归属是会变的产品决策——散在十二个工具里意味着改口径要改十二处，而漏掉的那处不会报错，只会继续放行。

回归在 `tests/test_deskcore_ownership.py`（含一条 AST 断言：每个项目级入口都必须调过这道闸——归属校验最典型的失效方式不是判据写错，而是**新加了个工具忘了加校验**）+ `ci.yml` app 冒烟步里的 403 运行期断言。

### 2.3 三处刻意的改动

**A. 正例按相关性选** → `core.select_positive_examples`。有 brief 且 embedding 可用时按余弦排序，然后跑一遍开头形态多样性约束。embedding 不可用时退化成 recency（与原行为一致，不会更差）。这一条直接断掉 §1.1 根因 4 的趋同回路。

**B. 查重比全量、比三个信号** → 标题语义 + 开头精确指纹 + 正文四字串 Jaccard。后两个是纯字符串运算，**没有 `GOOGLE_API_KEY` 也能跑**——原来只比标题向量，embedding 一挂整个失效。

**C. 查重是硬闸** → `check_drafts` 出错必须抛，静默放行就是重演根因 1。

fail-open 的范围**只有三个工具**：`list_projects` / `borrow_lessons` / `my_style`——读，且拿不到只是少点参考。判据是「失败之后调用方还会不会当作成功继续往下走」，会就不能吞：

| 工具 | 出错行为 | 为什么不能吞 |
|---|---|---|
| `open_project` | 抛 | 拿不到 P0 就照常开写，产出违规内容，而调用方看到的是一份 `p0` 为空的正常简报 |
| `check_drafts` | 抛 | 硬闸静默放行 |
| `commit_drafts` | 抛 | 定稿缺席指纹库，同样的稿子以后能再过一次闸 |
| `draw_angles` | 抛 | 没落台账，同一个角度下批还能再抽 |
| `record_rule` | 抛 | 用户明确说"以后都这样"的 hard 合规规则静默缺席之后每一份简报 |
| `record_edit` | 抛 | "裂变"的入口，静默失败 = 文风永远长不出来 |
| `save_my_style` | 抛 | "裂变"闭环的最后一步，静默失败 = 前面全白做 |
| `label_example` | 抛 | 标记没落库却报成功，用户不会再标第二次 |

（codex review round-5：`open_project` 和 `record_rule` 原来都包了 `_safe`。前者尤其糟——`store.shared_memories()` 为此专门**故意不吞异常**，外面再包一层等于把那个设计原样抵消掉。）

**D. 两个并发点都交给数据库** → `check_drafts` 和 `commit_drafts` 是两次独立调用，两个队友各自 check 时看到同一份旧指纹集、双双 pass，然后各自 commit——撞车的稿子一起进库。发牌有同样的问题。两处都用 project 级事务 advisory lock 收进一个事务里解决：

| RPC | 关掉的竞态 |
|---|---|
| `deskcore_reserve_angles` | 两人同时发牌拿到同一组角度坐标 |
| `deskcore_commit_fingerprints` | 两人 check 后同时 commit 撞车的稿子 |

思路同本仓已有的 `claim_one_job`——并发正确性交给数据库，不靠应用层自觉。commit 侧只在锁内做**确定性**信号（开头精确 + 四字串 Jaccard）；标题语义那一路留在 Python 的 `check_drafts` 里，因为历史行可能没有向量。被判撞车的条目**不入库**，在 `rejected` 里返回。

两个 RPC 不存在时（迁移没跑）都会降级到非原子路径，并在返回值里显式标 `atomic=false` / `atomic_recheck=false` + warning——不会假装原子。

---

## 3. 工具面（12 个）

| 阶段 | 工具 | 说明 |
|---|---|---|
| 写稿前 | `list_projects` | **我名下的**项目清单 + 各自的规则数/指纹数（按 `owner_id` 过滤，见 §2.2.1） |
| | `open_project` | **一次拿全**写作简报：stable / p0 / p1 / tactics |
| | `draw_angles` | 发牌：n 组互不重复、避开台账的坐标，带可直接贴的 `prompt_block` |
| | `borrow_lessons` | 转调 TV 馆员，借真实爆款经验卡 |
| 写稿后 | `check_drafts` | **硬闸**：全量历史 + 本批内互比 |
| | `commit_drafts` | 定稿入库：写指纹 + 建身份（batch/item/version）+ 给坐标销账 |
| 交付 | `export_drafts` | 导成可粘进飞书表的 Excel，带 TV 认的 lineage 列（见 §3.4） |
| 反馈 | `record_rule` | 沉淀规则（团队共享），hard 进 P0 |
| | `record_edit` | 喂手动精修 diff（信号 A），返回**交给调用方模型做**的蒸馏任务 |
| | `save_my_style` | 把模型蒸馏好的笔记写回 + 按 `edit_ids` 销账（`record_edit` 的第二步） |
| | `label_example` | 标正/负例 |
| | `my_style` | 查看个人风格资产；有积压时**直接带回可续做的蒸馏任务** |

### 3.3 蒸馏的两步与它的失败模式

`record_edit`（存 diff + 出任务）→ 调用方模型提炼 → `save_my_style`（写回 + 销账）。服务端**不调 LLM**。

这个拆分引入了一个新的无声失败：两步之间断掉，diff 存着而笔记不变——"喂了稿子却没变得像我"。三道处理：

| 风险 | 处理 |
|---|---|
| 模型拿到任务就忘了写回 | `record_edit` 的 `next_step` 明写"这一步还没做完"，指名 `save_my_style` |
| 断点无声 | `my_style` 报 `pending_distillation` |
| **会话没了，任务也丢了** | `pending > 0` 时 `my_style` 直接带回完整的 `pending_distillation_task`——材料、口径、`edit_ids` 都在，换个会话也能接着做，不必让用户重喂稿子 |

⚠️ **销账必须按 `edit_ids` 精确销，不能按 user+project 全量销。** 蒸馏任务是一份快照（一次最多 8 条），笔记只覆盖快照里那些。全量销账会吃掉两类不在快照里的行——第 9 条往后的、以及拿到任务之后新 `record_edit` 进来的。它们会从 `pending_distillation` 里消失却从没影响过笔记：用户喂了稿子，计数归零看着正常，那几条等于白喂。同理，`save_my_style` 不传 `edit_ids`（手动改笔记）时**一条都不销**。

工具的 docstring 就是模型看到的说明。

### 3.1 发牌

主维度笛卡尔积 = `emotional_lever(12) × human_truth_archetype(19) × content_format(8) × title_structure(8)` = **14,592 组**。

`angle_key` 只取这四个主维度；情绪强度 / 时效依赖 / 词感是叠加项**不进 key**——否则同一个核心组合换个词感就被当成"没用过"，台账白记。

台账避重两档时效：真出了稿的按 `avoid_days`（默认 30 天）；只抽了没写的（占位）按 1 天——抽了不写不该长期占坑。

### 3.2 词表

`deskcore/vocab.py` 分两层，来源不同，**故意不混**：

- **essence 层** ← `vendor/controlled_vocab_v0_2.json`，从 truth-vault 原样复制。权威源是 TV 的 `docs/05-controlled-vocab.md`。**不要手写这些值**，改词表走 `deskcore/vendor/README.md` 的流程。
- **surface 层** ← 本仓 `generator.py` 的 `TITLE_STRUCTURE_MATRIX` / `WORD_TILTS` / `CREATIVE_ROLES_POOL`。半衰期 6-12 个月，本来就该独立演进。

这个分法对应 TV README 原则 2 的 Surface/Essence 分层：两层衰减速度不同，混进一个字段就锁死了跨时间跨产品的迁移性。

vendor 的副本带 sha256，CI 和 `/health` 都校验——手改会被抓出来。

### 3.4 交付与 lineage（2026-08-25）

写作台以前**只到 `commit_drafts` 为止**：稿子进了指纹库，然后就没有下文了。用户自己从对话里把文案复制出去，粘进飞书表——粘过去的只有正文，没有任何"这是谁写的哪一版"。

后果不在 autowriter 这边，在回程上。飞书表是 Truth Vault 的数据源，TV 靠 `notes.source_autowriter_version_id` 这个跨 schema FK 把笔记接回 `autowriter.versions`，`v_model_comparison` 就 JOIN 在它上面。那一列一直是空的，所以那个 view **长期查出空集，而且不报错**。

三处一起改才通：

**① `commit_drafts` 建身份。** 入了库的稿子现在有 `batches` / `items` / `versions` 行，返回值带 `batch_id` 和 `version_ids`。

- 建的是 `status='pending'`，决策三列全空。**不能标 `approved`**——TV 的 `sync_autowriter_decisions_to_prepublish.py` 按 `status in ('approved','needs_revision')` 捞行、把捞到的全部当 `evaluator_type='human'` 灌进 `prepublish_evaluations`，而它**还没接** COR-004 那三列。标 approved 等于让每条定稿变成一条伪造的人工评价去校准 TV 的评估模型，且写作台没有"打回"这个动作，灌进去的会是清一色正例。
- 只给**判定 inserted** 的那几条建。撞车被拒的不建——否则审核页会多出没人交付过的孤儿。
- 顺序是**先造 id 写进指纹、事后建行**。反过来会留孤儿；这个方向的失败只是 lineage 断掉（退回改之前的状态），可逆，且会报 `identity_warning`。

**② `export_drafts` 出表。** 按 `batch_id`（或 `version_ids`）点名导出，**不看 status**——写作台的稿子是 pending 且永远不会变 approved，走导出中心那条路（`_collect_approved_items` 第一行就是 status 判断）只会导出空。

返回 `xlsx_base64`，调用方自己落盘。不给下载 URL 是因为那要配一套单独的签名鉴权，而本仓审计史上一半的坑都是"半套鉴权"。

**③ lineage 列改成命名可见列。** 原来 `build_combined_excel` 把 lineage 塞在**隐藏的无名 B 列**（整张表连表头都没有）。两条独立原因让它在飞书那侧结构上就走不通：飞书按**列名**匹配字段，无名列进不去；隐藏列只有"整表导入"才跟着走，而运营的实际动作是选中可见列复制粘贴，一粘就没了。TV 的 `docs/11-feishu-table-setup.md` 把后一条单独列为待解决的坑。

现在是 6 个命名可见列，列名**由 TV 定**（`exporter.LINEAGE_COLUMNS` 是唯一真源）：

| 列名 | 去处 |
|---|---|
| `_source_autowriter_item_id` | → `notes.source_autowriter_item_id`（FK） |
| `_source_autowriter_version_id` | → `notes.source_autowriter_version_id`（FK，`v_model_comparison` JOIN 它） |
| `_source_autowriter_project_id` / `_batch_id` | → `notes.raw_extra` |
| `_ai_engine` | → `raw_extra`；TV 按它 GROUP BY 出模型胜率，所以**不能 `.upper()`** |
| `_exported_at` | → `raw_extra`；飞书那边是**日期**列 |

⚠️ 名字写错的代价**不是丢一列**：未声明的列会让 TV 的 D-021 把**整行** quarantine，那条笔记连正文带指标一起进不了库。跨库审计 COR-002 抓的就是 `build_excel_document` 里那两个错名字（`_source_autowriter_ai_engine` / `_source_autowriter_version_num`）——那个函数没有调用方，所以这个错从写下那天起没被任何一次真实导出暴露过。`tests/test_lineage_contract.py` 把六个名字**手抄**在用例里当判据，不从 `exporter` 读（那样就是自己和自己比，永远绿）。

**运营侧前置**：飞书表要先按上表建好这六列，列名逐字相同。`export_drafts` 的返回值里直接带 `columns`，不必翻文档。

**还没接的一段**：指标回流。TV 那边有了归因数据之后，"这个角度产出的稿子后来爆没爆"才能反过来喂 `draw_angles` 和正例池——`angle_ledger` 现在只记 `drawn_at` / `consumed_version_id`，不记结果。那是下一步，不在这次范围里。

---

## 4. 接入

### 4.1 部署

独立 service（跟 `worker.py` 同级），config 指 `deskcore/railway.json`。

env：

| 变量 | 必需 | 说明 |
|---|---|---|
| `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` | ✅ | service_role，绕 RLS |
| `DESKCORE_KEYS` | 生产必需 | `{"k-xxx": {"user_id": "<uuid>", "name": "Ziao"}}`，一人一把 |
| `GOOGLE_API_KEY` | 强烈建议 | embedding。不设则查重降级为纯确定性 |
| `LIBRARIAN_URL` / `LIBRARIAN_API_KEY` | 可选 | 借爆款经验卡；不设则 `borrow_lessons` 返回空 |
| `DESKCORE_ALLOWED_HOSTS` | 可选 | 逗号分隔。设了才开 MCP 的 Host 校验；不设=不校验（见 §5） |
| `DESKCORE_ALLOWED_ORIGINS` | 可选 | 逗号分隔的完整 origin。不设=允许全部——**这是安全的**，身份靠显式传的 key 而非 cookie，浏览器不会自动附上（见 §5.5） |

> ⚠️ **不需要 `ANTHROPIC_API_KEY` / `DESKCORE_MODEL`。** deskcore 一次 LLM 调用都没有——调校笔记的蒸馏交给调用方模型做（`record_edit` 出材料 → 模型提炼 → `save_my_style` 写回）。
>
> 这不只是省一个 key。方案自己的原则就是「推理归 WorkBuddy，MCP 只做轻量数据操作」，而服务端自己调 LLM 违反了它，还多一个中转站故障点。顺带消掉了一整类故障：上一轮刚踩过「`/health` 回显的模型名和实际调用的不同源，于是配错永远看不见」——**没有模型可配，就没有配错的余地**。

### 4.1.1 部署两步，缺一不可

**① 跑迁移。** 不是只有 `001` —— 到今天是 `001_deskcore.sql` /
`002_calibration_cas.sql` / `003_versions_unique_num.sql` /
`004_deskcore_check_pushdown.sql` / `005_deskcore_containment.sql` /
`006_item_decision_provenance.sql` / `007_deskcore_table_grants.sql` /
`008_embedding_model_isolation.sql`
**八个，按编号顺序跑，别跳号**（建议先在
Supabase branch 库跑 + `get_advisors` 核验再进 prod）。每个各自不跑会怎样，看
`migrations/README.md` 的清单表，那份是唯一真源。

> ⚠️ **这一段曾经写着"`001`，以及后续的 `002` / `003` / `004`"**，`005` 和 `006`
> 从来没被写进去。照着它部署的人会漏掉 `005`（短稿整段照搬长稿从此抓不到）和
> `006`（现有工作台的审稿按钮当场报错），而两边都不会有任何东西提醒他漏了。
> 现在 `tests/test_migration_doctor.py` 断言 `migrations/*.sql` 每个文件名都要
> 出现在本文档、`migrations/README.md` 和 runbook 里——漏写会红。

**先查一次库，别照文档推断**：

```bash
python -m deskcore.cli doctor        # 只读; 逐个探测缺哪个、缺了会怎样
```

判据与真正消费这些迁移的代码同源（`store.rpc_missing`），所以不会出现"文档说
跑过了、代码认为没跑"。2026-08-26 实测生产库 `kduysqedr` **只跑过 `001`**，而
runbook §0 当时写的是"schema 也上了生产"——这条命令就是为了不再有下一次。

两个要单独记住的：

- `004_deskcore_check_pushdown.sql` 是 2026-08-23 审计 SUP-002/SUP-004/ROB-004/ROB-011 的落地：把 `check_drafts` 的三路比对和 `list_projects` 的指纹计数下推到库里。**不跑也不会坏** —— `check_drafts` 检测到 RPC 不存在会退回 Python 逐对比对（结论一致，只是慢，且回到 4000 条上限），并埋一行 `deskcore_rpc_missing`。但大项目上不跑就仍然会撞线程池饥饿和 OOM，所以别拖。
- `006_item_decision_provenance.sql` **不跑就当场坏**：`db.update_item_status` 无条件写 `decision_source` / `reviewer_id` / `decided_at`，缺列会让现有工作台的「通过 / 打回」、硬规则自动标记、查重自动标记全部报错。刻意不做"去掉三列重试"的降级——那等于让机器判定继续伪装成人工反馈去污染 TV 的评估模型（COR-004 治的正是这件事）。**升级顺序上它排最前面。**
- `007_deskcore_table_grants.sql` **同样不跑就当场坏**，而且它的失败形态最难猜：`001` 建的那四张表**一行 `GRANT` 都没有**（`001` 只给两个*函数*发了 `EXECUTE`），于是 deskcore 除 `list_projects` 外每个工具都挂在 `42501 permission denied for table draft_fingerprints`——**而 `/health` 全绿**（它探的是连得上，不是访问得了）。

  > 坑在于 `service_role` **绕过 RLS，但不绕过表级 `GRANT`**——两套独立机制。它在 `public` schema 下看着无所不能，靠的是 Supabase 给 `public` 配的 default privileges；`autowriter` 是本仓自建 schema，**没有**这份默认授权，新表出生就是零权限。
  >
  > 这是 2026-08-26 首次真部署当天靠人肉 `curl` 打线上才发现的。现在有两道守卫：`tests/sql_parity_check.py` 断言 **`autowriter` 下每一张表都必须对 `service_role` 有 `SELECT/INSERT/UPDATE/DELETE`**（断不变量而不是名单，以后加表忘了发 GRANT 会自己红）；`doctor` 把 `42501` 单独报成 `denied` 而不是混进 `error`，并直接指向 `migrations/007`——而且**四个权限一个个探**，因为"读得到"证明不了"写得进"。
- `008_embedding_model_isolation.sql` 治的是另一种"写着已经有了、实际没有"：`draft_fingerprints.embedding_model` 从 `001` 起就在，`COMMENT` 写着它是"换 embedding 供应商时唯一的救命稻草"，而**四路查重里没有任何一路读过它**。

  > 后果在换模型那天兑现：两个模型都出 768 维，所以维度守卫拦不住、写库也不报错，但两套向量空间毫不相干。跨模型算出来的余弦是噪声，**双向出错**——真重复的算出来很低（放行），无关的算出来很高（误杀），而 `semantic_degraded` 报的是 `false`：这一路不但失灵，还在报告里说自己跑过了。
  >
  > `008` 只加一句按模型过滤，`CREATE OR REPLACE`、签名不变。因为签名不变，**跑没跑过从调用侧完全看不出来**，所以 `doctor` 把它报成 `unprobeable` 并交出查函数体的 SQL，不蒙。
  >
  > 存量的老模型向量从此不参与比对（`semantic_degraded` 会如实报 `true`），跑 `python -m deskcore.cli reembed --project <id>` 用当前模型重算即可。**"少比并说出来"好过"混着比不说话"。**

**② 回填历史指纹**（**必做**）：

```bash
python -m deskcore.cli backfill --project <uuid>    # 每个项目跑一次
```

⚠️ 迁移建的是**空表**，而 `check_drafts` 只读这张表、只有 `commit_drafts` 会往里写。不回填的话，**刚上线那天号称"比对全量历史"的硬闸背后一条历史都没有**，老稿子的重复会原样放行。

回填是幂等的（按 `version_id` 跳过已有的），可以反复跑。

**`backfill` 和 `reembed` 是两件事，别混：**

| | 干什么 | 够得着哪些行 |
|---|---|---|
| `backfill` | 把 `autowriter.versions` 里的历史成稿**搬进**指纹库 | 只有 `items × versions` 里的 |
| `reembed` | 给**已经在指纹库里、但当时没取到向量**的行补向量 | 指纹表里所有缺向量的 |

典型场景：embedding 的 key 欠费那几天照常 commit 了稿子，它们只有确定性指纹。那些行 **`backfill` 永远够不着**——它扫的是 `items × versions`，而 WorkBuddy 写的稿子 `version_id` 是空的、根本不在那张表里。补好 key 之后跑 `python -m deskcore.cli reembed --project <uuid>`。

`commit_drafts` 现在会在「配了 embedding 却没取到向量」时返回 `embedding_warning`，不再只给一个 `embedded: false` 让人自己猜。没回填时 `check_drafts` 的 summary 会带 `empty_history_warning`。

向量的取法：`versions.embedding` 存的就是**标题**向量（`app.py:520` 只对 title 取向量），与 `draft_fingerprints.title_embedding` 同义，所以历史行有向量就**直接复用**，只有缺的才补算。返回值里 `reused_embeddings` / `computed_embeddings` / `missing_embeddings` 分开报。

> ⚠️ **但生产库里一条都复用不到。** 2026-08-23 实测：`autowriter.versions` 5,532 行，`embedding` 非空的是 **0 条**。所以 backfill 时 `reused_embeddings` 会是 0，标题向量全部现算（4,574 条，几分钟、几毛钱）。
>
> 一条都没有的原因是这条路径每一层都静默：`dedup.py:58` `embed_texts` 失败返回 `None` 不抛异常 → `app.py:522` `if not new_vecs: return` 整段跳过 → `db.py:1338` 落库「errors are swallowed」→ `db.py:1310` 直接 `except Exception: pass`。详见 [`deskcore-runbook.md`](deskcore-runbook.md) §5.1。

⚠️ **没配 `GOOGLE_API_KEY` 时也能回填**，但那批行没有标题向量，只参与确定性查重。补配之后重跑**不会**给已写入的行补向量（幂等是按 `version_id` 跳过的）——要补得先把这些行删掉再跑。所以顺序上**先配好 embedding 再回填**。

> 这个函数曾经**根本不存在**。CLI 子命令、本文档、PR 描述、连"部署必跑一次"的措辞都写好了，唯独没写函数体，`deskcore.cli backfill` 每次都 `AttributeError` —— 也就是回填这件事从头到尾没发生过，而所有文字都写着它已经有了。`py_compile` 抓不到（属性错误是运行期），所以 CI 现在有一步反射 `cli.py` 里所有 `core.xxx(` 调用、逐个断言存在。

### 4.2 自测

```bash
python -m deskcore.cli selftest      # 不连库不联网, 只用标准库
python -m deskcore.cli projects --user <uuid>          # 需要 SUPABASE_* env
python -m deskcore.cli draw --project <uuid> --user <uuid> -n 20 --block
# ⚠️ --user 是必填(审计 COR-015): 归属校验在 core 层, CLI 与 MCP 同一个函数。
#    传 projects.owner_id 里【已有的】那个 UUID, 与 DESKCORE_KEYS 里配的同源。
#    backfill / reembed 是运维命令, 刻意不需要 --user。

curl -sS "$DESKCORE_URL/health" | jq        # 每个依赖的 ok
curl -sS -X POST "$DESKCORE_URL/tool/list_projects" \
  -H "X-Deskcore-Key: $KEY" -H 'content-type: application/json' -d '{}'
```

`/health` 会回显实际解析到的模型名、embedding 可用性、vendor 词表校验和、鉴权是否配置——**配错当场可见**。这是刻意的：TV `docs/19:180-200` 记过一次事故，librarian 的模型 env 变量名配错，每次 LLM 调用失败降级成 `[]`，外面看永远 200，查了很久。

⚠️ **回显只有和真正用它的地方同源才叫回显。** 这里翻过一次车：`/health` 读 `os.environ["DESKCORE_MODEL"]`，而 `distill_calibration` 读 `getattr(config, "DESKCORE_MODEL", "")`——`config.py` 里根本没这个属性，永远落回 `config.CLAUDE_MODEL`。于是只配了 `DESKCORE_MODEL` 的部署里，`/health` 信心满满地回显着一个从未被调用过的模型名，配错依然当场看不见。

模型那一项现在已经不存在了（蒸馏搬走，deskcore 不调 LLM），但这条教训对**剩下每一个回显项**都成立：加新回显时，取值必须走真正消费它的那个函数，不要在 `/health` 里另读一遍 env。

### 4.3 挂到 WorkBuddy

项目级 `mcp.json`：

```json
{
  "mcpServers": {
    "deskcore": {
      "url": "https://<your-deskcore>/mcp",
      "headers": { "X-Deskcore-Key": "k-xxx" }
    }
  }
}
```

skill 从本仓直接装：`TangVarie/autowriter → skills/bywood-writing-desk`（WorkBuddy 支持从 GitHub 装）。**别手工复制**——拷贝出去的那份改了没人提醒，而它管的是流程纪律，过期了模型会按老规矩写而没人发现。

> ⚠️ **鉴权头的退路**：WorkBuddy 的 HTTP MCP 能不能配自定义 header，官方更新日志只说了支持 HTTP MCP 和 OAuth（v4.7.3），没有权威文档。所以 key **三种传法都收**，但**优先级不同**：
>
> 1. `X-Deskcore-Key` header —— 首选
> 2. `Authorization: Bearer` —— 同样安全，平台不认自定义头时用
> 3. `?key=` 查询参数 —— **最后的退路，能不用就不用**
>
> 第三种为什么排最后：**查询串会进日志。** CI 里那次冒烟测试的日志就原样打出了 `POST /tool/list_projects?key=k-good`——换成真 key，它会落进 Railway 的访问日志、中间代理日志、以及任何转发 URL 的地方，而且事后删不掉。真要用它，就当这个 key 已经半公开了：单独发一把、只给那一个平台、发现平台支持 header 之后立刻换掉并从 `DESKCORE_KEYS` 里删掉旧的。
>
> **这一步必须最先验**——协议层不通的话整个形态要换。

Claude Code：`claude mcp add --transport http deskcore <url>/mcp --header "X-Deskcore-Key: k-xxx"`。CodeBuddy 同理。

---

## 5. ⚠️ 已经踩过的坑，别再踩

**1. `AW_DISABLE_ST_CACHE`（R-042）。** deskcore 是 headless 进程，但 streamlit 在同一份 requirements 里装着，`db.py` 的缓存 shim 会走真 `st.cache_data` —— 跨进程缓存无法被 app 的 `.clear()` 失效，用户在 UI 改完记忆后 deskcore 会拿 30-60s 的旧数据。`deskcore/__init__.py` 已经在 import db 之前 setdefault 这个 env（同 `worker.py:56`）。**别在 `__init__` 之前 import db。**

**2. pgvector 反序列化（R-034）。** PostgREST 对 `vector(768)` 列的 JSON 序列化是**字符串** `"[0.1,...]"`，不是数组。不归一的话 `dedup.cosine_similarity` 因长度不等**静默返回 0.0** —— 查重变哑弹，一条都抓不到，而且不报错。所有读 embedding 的地方必须过 `db._parse_pgvector`（`store.py` 已经在读取边界统一处理）。

**3. 鉴权配坏了必须 fail closed。** `DESKCORE_KEYS` 的 JSON 写错时，早期实现会返回空 map → `resolve()` 判定为"没配鉴权" → **放行所有请求**。生产上一个逗号写错就等于把项目数据和全部写工具匿名开放。现在显式配了就必须当成"打算开鉴权"，解析失败一律 401，`/health` 的 `auth.ok` 会是 false。

**3b. "没配鉴权"同样 fail closed（2026-08-23 审计 ROB-003）。** 上面那条只堵了"配了但写错"，"根本没配"当时仍然走 dev 模式放行——而 deskcore 持 service_role 绕 RLS，漏配一个环境变量就等于把全部租户的数据和十二个工具（含写）开放到公网。更糟的是**健康检查看不见**：`/health` 虽然会把 `auth.ok` 报成 false，但它返回的是 HTTP 200，而 Railway 的 healthcheck 只看状态码——一个彻底敞开的部署照样判定健康、照样上线。

现在默认拒绝：没配 key 时每个请求都 401。本地开发要免 key 跑，显式设 `DESKCORE_ALLOW_ANONYMOUS=1`（`/health` 会把它回显在 `config.anonymous_allowed`，并在 `auth.note` 里写明是 dev 模式）。

`/health` 仍然返 200 —— **这是刻意的**。Railway 只看状态码，而库瞬断这类可恢复故障不该把整个部署卡住（CI 里那条 "200 是硬要求" 的断言就是为此存在的）。越权风险已经在 `identity.resolve` 那一层堵死了，不该再用状态码兜第二遍。

**3c. `/health` 不和工具抢线程池（2026-08-23 审计 ROB-004）。** 工具调用走 `run_in_threadpool`，而 `def health()` 这种**同步路由也走同一个 starlette 默认 limiter**（40 个额度）。`check_drafts` 那种几十秒的调用一多，`/health` 就排在它们后面 → 平台健康检查超时 → 重启容器 → 正在跑的调用全断。

根因已经被 `migrations/004` 的下推堵掉了（工具调用不再是几十秒），但"健康检查和业务抢同一个池子"本身是个雷，换个慢查询就复发。所以 `/health` 现在是 `async def`，它唯一那次打网络的探测（库连通）走**私有** `anyio.CapacityLimiter(2)` + 5 秒墙钟上限（`DESKCORE_HEALTH_PROBE_TIMEOUT` 可调）。

超时按不健康报，但 note 里会写明**是超时而不是连不上** —— 这是两种完全不同的排查方向：前者是连接池耗尽 / 网络黑洞，后者是配置或凭据。

**4. `user_id` 必须用库里已有的 UUID。** 不要新造。TV `autowriter-migrations/RUNBOOK.md:150-153` 记过：写了 service account 的 UUID 导致 RLS 屏蔽、`list_example_items` 永远 0 行、飞轮静默断开，查了很久。配 `DESKCORE_KEYS` 时从 `projects.owner_id` / `items.user_id` 里查出来抄。

**5. 浏览器侧的客户端要 CORS，而所有手工验证都绕开了它（2026-08-27）。** 第一次真的往 WorkBuddy 上挂，报的是 `streamableHttp connect failed: fetch failed`。两个独立的毛病：

**5a. 一行 CORS 头都没发。** 浏览器/Electron 渲染层在发带自定义头的请求前会先发 `OPTIONS` 预检，而预检**按规范就不带任何自定义头**，也就不带 `X-Deskcore-Key` → 一头撞进鉴权中间件 → 401。浏览器对「预检没通过」的报法是 `TypeError: fetch failed`，**不是 401**，客户端那头完全看不出是鉴权的事。

之所以一直没发现：curl 不做预检、浏览器地址栏打 `/health` 是同源导航也不做预检、CI 的冒烟测试用 TestClient 直接发 `initialize` 同样不做预检——**每一条验证路径都是绿的，而真实客户端一个都连不上**。又是那个老病的新形态。

`CORSMiddleware` **必须注册在鉴权中间件之后**（Starlette 里最后注册的跑在最外层）。装在里面等于没装：唯一会被拦的请求（401）恰恰是最需要被客户端读到的那个。`allow_credentials` 必须是 `False`——开了它浏览器就**拒绝**通配的 `Access-Control-Allow-Origin`，两者不能同时要。

**5b. `/mcp` 不带斜杠每次先吃一个 307。** `Mount("/mcp")` 的正则是 `^/mcp(?P<path>/.*)$`，光秃秃的 `/mcp` 匹配不上，落到 `redirect_slashes` 兜底跳 `/mcp/`。而文档和给客户端的地址写的全是不带斜杠的那个。平时看不出来是因为验证用的东西**都自动跟随重定向**；跨源时这一跳要重新预检，各家实现处理不一致，失败报法同样是 `fetch failed`。已用一个纯 ASGI 中间件就地改写掉（**不用** `BaseHTTPMiddleware`——它会把响应体收进内存，对流式响应有害）。

`/health` 现在回显 `mcp_allowed_origins` / `mcp_allowed_hosts`。代码注释里原本就写着"会回显"，**而实际上没有**——写了但没实现，一并补上。

---

## 6. 与三个语言类 skill 的分工

**不要再立第四套语言规则**——已经有三套并列口径了（`human-writing` 全禁 / `bywood-proposal` 散段 / `seeding-prompt-refiner`）。

| skill | 管什么 |
|---|---|
| `human-writing` | 这一篇读起来像不像人写的（去 AI 味、禁用句式、跨篇指纹） |
| `bywood-writing-desk` | 这一批守不守项目规则、跟历史重不重 |
| `seeding-prompt-refiner` | 提示词本身怎么迭代 |

`seeding-prompt-refiner/SKILL.md:496` 自己写过「LLM 是无状态函数……提示词再好也解决不了跨批次去重这个根本问题」，并把这条归给工作台。它诊断对了，缺的就是一个能持久记账的外部工具。它 `references/cross-batch-diversity.md` 里那四套**用户手动维护**的补偿机制（角度编号库 / 历史回避清单 / 跨批次分布档案 / 账号差异化种子），deskcore 落地后可以全部退役——它自己就标注了那是"过渡方案，不是长期方案"。

---

## 7. 未决点

1. **WorkBuddy 的 HTTP MCP 自定义鉴权头无权威文档。** 见 §4.3，已留两条退路，但必须最先验。
2. **馆员选卡质量从未在真实规模验证过。** TV 书架现有 118 张卡 / 可借 201，但这个规模下的选卡准确率没人测过。
3. **embedding 依赖 `GOOGLE_API_KEY`。** 换模型会让存量向量全部作废需重算。没有它时查重降级为纯确定性——仍能抓开头撞车和四字串重合（`selftest` 证明了这点），但同角度换说法的标题会漏。

   ⚠️ **模型是会被下线的**（2026-08-26 实测踩到）。原来写死的 `text-embedding-004` 已经不存在，API 回 404；现在是 `gemini-embedding-001`，它**默认输出 3072 维**，靠 `output_dimensionality` 截到库里那三列要求的 768。

   真正的教训不是"模型换了名字"，是**这条路径的失败当时完全没有痕迹**：`embed_texts` 是裸 `except Exception: return None`，一行日志都不打，于是现象只是 `check_drafts` 的 `semantic_degraded` 悄悄变 `true`，Railway 日志干干净净，最后靠人肉 curl 才问出「模型 404」。现在那一层加了 `logger.exception`，并且多了一道**维度守卫**——长度不等于 `EMBEDDING_DIM` 时整批作废，因为 `cosine_similarity` 对长度不符**返回 0.0**（与 R-034 的 `_parse_pgvector` 同款形状：查重变哑弹且不报错）。
4. ~~**查重目前在 Python 里逐对比。**~~ **已解决（2026-08-23 审计 SUP-002）**：`migrations/004` 的 `deskcore_check_drafts` 把三路信号全部下推，`check_drafts` 不再把指纹拉进内存，`history_size` 也从"最近 4000 条"变成全量。RPC 没部署时仍会退回老路径（带原来的上限和截断警告）。

   ⚠️ **余弦那一路刻意不走 ivfflat 索引**。ivfflat 是近似最近邻（默认 `probes=1` 只扫一个聚类桶），对"推荐相似内容"够用，对**查重硬闸**是致命的：漏掉的那条正是要拦下的重复稿，而且不报错；叠加 `project_id` 过滤后更糟（先按向量取候选再过滤，命中本项目的可能一条都不剩）。RPC 里 `ORDER BY` 的是子查询算好的别名 `sim`，不是 `title_embedding <=> v` —— pgvector 的索引只认后一种形态，换成前者规划器必然走顺序扫描，精确且可预期。索引留着不动，将来做"找相似选题"这类容忍近似的功能仍然用得上。

4b. ~~**正文那一路只有 Jaccard。**~~ **已解决（2026-08-24 审计 COR-014）**：`migrations/005` 把 Jaccard 换成 bottom-k 的标准估计式（只在两个 sketch 都覆盖到的 hash 区间上算），并多回一路**包含度** `|A∩B|/min(|A|,|B|)`。

   ⚠️ **两件事，别只记住一件。** 估计确实有偏（每篇各自取 bottom-k，长短稿的第 k 小值差着量级，估计系统性偏低，最大 0.125）；但**改准了也抓不住"短稿逐字抄长稿"**——那个形状下 Jaccard 的真值就等于两篇的长度比（280 字抄进 2800 字里真值是 0.1），够不着 0.35 的硬闸线。Jaccard 天生被长度差稀释，这是定义决定的。所以必须加包含度。合成对照 7/7 全部拦下（原来 1/7）。阈值 0.60 / 样本量下限 15 都是从零分布定的，见 `deskcore/fingerprint.py` 里那两张表。**存量指纹不用重算。**

4c. **`normalize` 的口径变了（2026-08-24）——改 `normalize` 就必须重算存量指纹。**

   原来那行手写的字符类里 **中文弯引号 `“ ” ‘ ’` 从来就没写进去过**，`「」『』【】〈〉〔〕` 也没有（当时数出 37 个常见中文标点没被覆盖）。实测同一篇稿子把 `“”` 换成 `""`，Jaccard 掉到 **0.333**——正好在 0.35 硬闸线之下，开头指纹也对不上。**那是一条能绕过查重的路子**，而 `【】` 在小红书文案里遍地都是。

   现在改成按 Unicode 类别判：去掉 `Cc/Cf · Zs/Zl/Zp · Pc/Pd/Ps/Pe/Pi/Pf/Po · Sm/Sc/Sk`，**保留 `So`（emoji）**——"换个 emoji 算不算同一篇"是产品问题，不该由一个规范化函数顺手决定。逐个往字符类里补是补不完的。

   ⚠️ **`normalize` 是存量指纹的计算口径。** 它一变，库里的 `opening_hash` / `ngram_hashes` 就全部作废：新稿算出来的四字串和历史对不上，查重在过渡期反而**更弱**，而且不报错。`backfill` 补不了——它按 `version_id` 幂等跳过。所以配了一个重算命令：

   ```bash
   python -m deskcore.cli recompute-fingerprints --project <uuid>
   ```

   **能修到什么程度是由指纹表存了什么决定的**（它只存 `title` 和 `opening` 前 25 字，**不存正文**）：

   | 字段 | 能修吗 |
   |---|---|
   | `opening_hash` | **每一行都能修**——它本来就是 `sha16(normalize(opening))`，而 `opening` 原样存着。这一路是"单独就判死"的最强信号 |
   | `ngram_hashes` | 只有 `version_id` 非空的行能修（正文在 `versions` 里）。~~WorkBuddy 经 `commit_drafts` 写进来的行**正文已经不存在了**~~ ——  2026-08-25 起 `commit_drafts` 会建 `versions` 行，正文留着了，所以这些行也修得回来（只有那之前写进去的旧行还是修不了） |

   返回值里的 `ngram_unrecoverable` 就是修不回来的行数，不为 0 时会带 `warning`。要彻底修只能把那些稿子重新 commit 一遍。

   **上线顺序**：先发代码 → 立刻对每个项目跑一次重算。两者之间的窗口里查重是偏弱的，所以别隔夜。

5. **commit 的原子重查只覆盖确定性信号。** 开头精确 + 四字串 Jaccard 在锁内查；标题语义相似度没查（要 pgvector 距离算子，且历史行可能没向量）。也就是说竞态窗口里"标题换个说法的同角度稿"仍可能两条都进。要覆盖它得把向量比对也搬进 RPC——等 backfill 把历史向量补齐之后再做更合适。
6. **"个人风格私有"与团队协作的张力。** 同一项目两个人各自驯化，风格会分叉。指纹库共享（互相避重），调校笔记不共享。跑一段时间如果分叉太严重，可能需要"把我的调校笔记提升为项目基线"的操作。第一期不做。

   ⚠️ **按 `owner_id` 隔离之后这条张力换了形态**（见 §2.2.1）。原来的设想是"两个人打开同一个项目"，而现在一个项目只有一个 owner，跨人协作要么共用一个 `user_id`（那样个人层就退化没了），要么等真正的团队模型。真要做团队共享时，改的是 `core.assert_project_access` 一处——但那是个产品决策，不是顺手改。
7. **native 正例的 essence 不可测。** 运营手标的正例没有 `external_source_id`，join 不到 `truth_vault.notes`，拿不到 `emotional_lever`，所以 TV 的饱和度监控对它们只能报"无法评估"。想让它可测需要给这些正例补 essence 标注。
