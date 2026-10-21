# deskcore · 写作台内核外置为 MCP

> 本文讲**为什么这么设计**。要看**现在是什么状态、还差什么、下一步做什么**，
> 去 [`deskcore-runbook.md`](deskcore-runbook.md)——上线手册、对接矩阵、待办清单，
> 以及一份「文档与实际不一致」的登记表。

> **给谁看**：要把写作能力挂到 WorkBuddy / Claude Code / CodeBuddy 的人；以及以后维护 deskcore 的人。
>
> **一句话**：Streamlit 界面【计划】停用，内核做成 MCP 工具继续用。库里几年的积累一条不迁。
> （停服**尚未执行**，前置取舍见 §3 与 runbook 待办 P1 #5。）
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

慢的根因是 Streamlit：写这段时 `app.py` 5560 行，每次交互全量重跑（2026-08-24 把 `generation_service` 抽出去之后降到三千八百多行，**但每次交互全量重跑这件事没变**）。能力外置后这个问题自动消失——推理归 WorkBuddy，deskcore 只做轻量数据操作。

### 1.1 重复率的四个根因

1. **硬闸默认关着。** `config.py` `ENABLE_DEDUP_REGEN` 默认 `"0"`。查重跑了、命中了，只写一条 UI 警告，**不重生也不拦**。
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
dedup / clients /                     judge (JevforCoentent)
librarian_client /                    (HTTP, 入库判定, 影子期, §3.8)
judge_client
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
| 入库判定（§3.8） | `judge_client.build_draft_request` / `judge_draft`（HTTP 拿事实，deskcore 仍然零 LLM） |
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
| 全部 MCP 工具 | `TOOLS` 里 `needs_user` **全部为 True**；`core.assert_project_access` 是唯一实现，每个项目级入口以它开头。`create_project` 不打开已有项目，所以不过这道闸——它的等价保证是**签名里没有 owner 参数**，归属只能是调用者。`get_protocol` 也不过闸——它不碰项目，只下发一份对所有人一样的流程文本（`needs_user` 仍是 True，不给那条机械保险开例外） |
| `label_example` | **按 `items.user_id` 校验**，比项目粒度更细——同一项目里 A 的正负例是 A 的个人资产 |
| REST/MCP 层 | `PermissionError` → **403**（不是 401，也不是 500）。401 = key 那一层没过；403 = key 过了但项目不是你的；500 = 服务端真的坏了 |
| CLI 的 `projects` / `open` / `draw` / `check` | 走同一个 `core` 函数，所以 `--user` 从可选变成**必填** |
| CLI 的 `backfill` / `reembed` / `delivery-format`（2026-10-20） | **刻意不校验**——它们是运维命令，跑它们的人手里握着 service_role key（等价于直连库），加校验挡不住任何人，只会挡住"帮同事补一下指纹"，还会给人"运维路径也隔离了"的错觉 |

**要改成团队共享时改哪儿**：`core.assert_project_access` 的函数体（加一张 `project_members` 表就是把那个 `!=` 换成一次成员查询），调用方一行不动。判据刻意收敛成一处，就是因为归属是会变的产品决策——散在每个工具里意味着改一次口径要改十几处，而漏掉的那处不会报错，只会继续放行。

回归在 `tests/test_deskcore_ownership.py`（含一条 AST 断言：每个项目级入口都必须调过这道闸——归属校验最典型的失效方式不是判据写错，而是**新加了个工具忘了加校验**）+ `ci.yml` app 冒烟步里的 403 运行期断言。

### 2.3 三处刻意的改动

**A. 正例按相关性选** → `core.select_positive_examples`。有 brief 且 embedding 可用时按余弦排序，然后跑一遍开头形态多样性约束。embedding 不可用时退化成 recency（与原行为一致，不会更差）。这一条直接断掉 §1.1 根因 4 的趋同回路。

> **2026-10-20 改：排不出相关性时轮换，不再退回 recency。** 写作台的版本从来不算向量（10-20 查生产库：09-01 以来 1,011 个写作台版本，0 个有 embedding），于是每条候选都记 -1.0，稳定排序原样交回最近的 5 条，`positive_selection_mode` 却报 `relevance`——每一场拿到的都是同几条最新正例，正是这一条要断的趋同回路。现在候选先按 (项目, 写手, 北京日期) 的 sha256 定种子洗牌（取法同 `flywheel_arm`，加了前缀不和分臂同一个哈希）：同一个人同一天开几场看到同一批，换一天换一批，60 条候选池都轮得到。没有 brief 文本、embedding 不可用、或候选一条向量都没有时（后者连 brief 的向量都不算了）就按洗好的顺序取，mode 报 `rotating`。有向量的候选存在时照旧按相关性排、mode 报 `relevance`，没向量的那些照洗好的顺序接在后面——洗牌在打分之前，所以只有一两条带向量（网页端那条生成路径会写 `versions.embedding`）时，剩下的空位也是按天轮换，不会落回最新那几条。

**B. 查重比全量、比三个信号** → 标题语义 + 开头精确指纹 + 正文四字串 Jaccard。后两个是纯字符串运算，**没有 `GOOGLE_API_KEY` 也能跑**——原来只比标题向量，embedding 一挂整个失效。

**C. 查重是硬闸** → `check_drafts` 出错必须抛，静默放行就是重演根因 1。

fail-open 的范围**只有四个工具**：`list_projects` / `borrow_lessons` / `my_style` / `my_rules`——读，且拿不到只是少点参考。这个集合被 `ci.yml` 的 `SAFE_OK` 钉死，增减都会红。判据是「失败之后调用方还会不会当作成功继续往下走」，会就不能吞：

| 工具 | 出错行为 | 为什么不能吞 |
|---|---|---|
| `create_project` | 抛 | 降级成带 `error` 的"成功"之后，调用方会拿着一个**不存在的 `project_id`** 往下走，后面每个工具都 404，而根因在三步之前且看不见 |
| `open_project` | 抛 | 拿不到 P0 就照常开写，产出违规内容，而调用方看到的是一份 `p0` 为空的正常简报 |
| `check_drafts` | 抛 | 硬闸静默放行 |
| `commit_drafts` | 抛 | 定稿缺席指纹库，同样的稿子以后能再过一次闸 |
| `draw_angles` | 抛 | 没落台账，同一个角度下批还能再抽 |
| `record_rule` | 抛 | 用户明确说"以后都这样"的 hard 合规规则静默缺席之后每一份简报 |
| `record_edit` | 抛 | "裂变"的入口，静默失败 = 文风永远长不出来 |
| `save_my_style` | 抛 | "裂变"闭环的最后一步，静默失败 = 前面全白做 |
| `label_example` | 抛 | 标记没落库却报成功，用户不会再标第二次 |
| `get_protocol` | 抛 | 协议拿不到就该停。降级成"成功"会让模型按记忆里的旧协议继续写——那正是把协议搬到服务端要消灭的失败 |

（codex review round-5：`open_project` 和 `record_rule` 原来都包了 `_safe`。前者尤其糟——`store.shared_memories()` 为此专门**故意不吞异常**，外面再包一层等于把那个设计原样抵消掉。）

**D. 两个并发点都交给数据库** → `check_drafts` 和 `commit_drafts` 是两次独立调用，两个队友各自 check 时看到同一份旧指纹集、双双 pass，然后各自 commit——撞车的稿子一起进库。发牌有同样的问题。两处都用 project 级事务 advisory lock 收进一个事务里解决：

| RPC | 关掉的竞态 |
|---|---|
| `deskcore_reserve_angles` | 两人同时发牌拿到同一组角度坐标 |
| `deskcore_commit_fingerprints` | 两人 check 后同时 commit 撞车的稿子 |

思路同本仓已有的 `claim_one_job`——并发正确性交给数据库，不靠应用层自觉。commit 侧只在锁内做**确定性**信号（开头精确 + 四字串 Jaccard）；标题语义那一路留在 Python 的 `check_drafts` 里，因为历史行可能没有向量。被判撞车的条目**不入库**，在 `rejected` 里返回。

两个 RPC 不存在时（迁移没跑）都会降级到非原子路径，并在返回值里显式标 `atomic=false` / `atomic_recheck=false` + warning——不会假装原子。

---

## 3. 工具面

（**个数不在这里钉死**——打一下 `/health` 或读 `tools.py` 的 `TOOLS`。这份表管的是每个工具干什么。）

| 阶段 | 工具 | 说明 |
|---|---|---|
| 核协议版本 | `get_protocol` | 对话开始时调一次，带上 skill 里的 `protocol_version`：一致只回版本；不一致或没带才下发正文（源文件 `deskcore/protocol.md`）。正文的主副本在 skill 文件里，见 §4.3 |
| 开项目 | `create_project` | 给新品牌/新方向建项目。**owner 恒为调用者**，签名里没有 owner 参数。同名不建、返回已有的；同品牌照建但列出兄弟项目（见 §3.0） |
| 写稿前 | `list_projects` | **我名下的**项目清单 + 各自的规则数/指纹数（按 `owner_id` 过滤，见 §2.2.1） |
| | `open_project` | **一次拿全**写作简报：stable / p0 / p1 / tactics；另带最近发出去 / 写过的标题 `recent_titles`，以及最近的稿子在哪几处都写成一个样子 `recent_structure`（都是 2026-10-20，见 §3.1） |
| | `draw_angles` | 发牌：n 组互不重复、避开台账的坐标，带可直接贴的 `prompt_block` |
| | `borrow_lessons` | 转调 TV 馆员，**再**借一批真实爆款经验卡（`open_project` 已随简报借过一次，见 `lessons` / `lessons_status`） |
| 写稿后 | `check_drafts` | **硬闸**：全量历史 + 本批内互比 |
| | `judge_draft` / `repair_plan_for` | **写后参考判定（可选，影子期不拦）**：deskcore 用服务端的管理 key 转发到 judge 的 `/judge_draft`，`write` / `return_rows` 钉死 false；项目号、品类按 `tv_project_map` 定（与 §3.8 的影子判定同一条路）。写手只有 deskcore 这一个 MCP，不再在自己机器上起 judge 仓的 `judge.mcp_server`（2026-10-09） |
| | `list_banks` | judge 上的题库清单（每家族最新版；暗题只报个数） |
| | `commit_drafts` | 入库：写指纹 + 建身份（batch/item/version）+ 给坐标销账。**交付的同一轮里调，不等用户说定稿**（2026-09-18 起；此前等人开口那道门漏掉了 91%）。入库是「待审」，不代表定稿；哪些真的发了由飞书 → TV 的 `tv-sync` 按内容对照回来。改稿带 `replaces_version_id`：先摘旧版指纹再过闸，成功后同一个 item 升一版（`replaced`），被拒或异常把指纹原样放回。摘指纹与原子写入不在一个事务里，所以 commit 全程持项目写锁（与 `ingest_published` 同一把，`009` 的 `ingest_locks`）：同一项目的入库与补录排队，等超时报 409 重试即可。锁放掉之后把真的建成了 versions 行的稿子发给 judge（影子期只记不拦，返回里的 `judge`，另带 `elapsed_ms` / `phase_ms`，见 §3.8）。2026-10-20 起项目有交付模板时带拼好的 `delivery`（§3.9），带 check 的规则有命中时带 `rule_checks`（§3.10） |
| | `check_rules` | **存档之后按规则逐篇查，只作建议**（2026-10-20，D-121）：规则事先由 Jev 编成是非题 / 代码检查、缓存在 `rule_compiles`；hard 没守住报「必须改」，soft 报「建议改」，Jev 拿不准的单列「拿不准」，到点没判完的报「没判完」。不拦存档、不改稿（见 §3.11） |
| 人审 | `review_drafts` | 把**用户真的给出的**审核结论落库：`approved` / `needs_revision`，`decision_source=human_via_agent`（结论来自用户、经模型转述写入；Streamlit 里亲手点按钮的才是 `human`）+ 真实 reviewer + 时间 + **用户原话 `user_words`（必填，落 `decision_note`）** + 距入库秒数 `decided_within_s`。审稿人恒为调用者；用户没表态**不许调**（见 §3.6） |
| 交付 | `export_drafts` | 导成可粘进飞书表的 Excel，带 TV 认的 lineage 列（见 §3.4） |
| 补历史 | `ingest_published` | 把**已经发出去、当时没入库**的稿子补进指纹库：建身份（出处记在 `batches.params`）+ 写指纹，**不过闸**、不销角度。运营在 WorkBuddy 里粘表即可，≤ 50 条一次；按全文幂等，重复调安全。CLI 的 `ingest --xlsx` 是同一个 core 函数的本地入口（2026-09-17） |
| 反馈 | `record_rule` | 沉淀规则（团队共享），hard 进 P0；2026-10-20 起可带 `check`，存档时用代码逐篇查（§3.10） |
| | `record_edit` | 喂手动精修 diff（信号 A），返回**交给调用方模型做**的蒸馏任务 |
| | `save_my_style` | 把模型蒸馏好的笔记写回 + 按 `edit_ids` 销账（`record_edit` 的第二步） |
| | `label_example` | 标正/负例 |
| | `my_style` | 查看个人风格资产；有积压时**直接带回可续做的蒸馏任务** |
| 规则台账 | `my_rules` | 列出这个项目上我能管的**全部**规则，含试用档与已停用的，每条带 `state`（见 §3.5） |
| | `set_rule_state` | 停用 / 恢复 / 升降档 / 设方向。**不改内容** |
| | `reembed_my_rules` | 给自己名下缺向量的规则补向量——没向量的规则照常注入但不参与相关性筛选（2026-08-28） |

### 3.0 `create_project` —— 补上"新品牌进不来"这个洞（2026-08-27）

**现场是这样炸的。** 团队第一次真用起来，要给六个新品（sportsix / 西屋 / 雷诺考特 / 百健士藻油 / 岸深冲牙器 / 途鸽）开项目，模型如实回答：

> 这六个我这边都建不了——不是没努力，是 deskcore 这套工具里压根没有「新建项目」这个动作。

它没说错。建项目的代码只在 `projects.py`（停用中的 Streamlit 页面）里，MCP 这侧一个入口都没有。而 skill 又强制「动笔前必须先 `open_project`」——**想建建不了，想跳过又不让跳，新品牌完全进不来**。这个缺口在此前任何一份文档里都没写。

三条设计决定，每条都对应一个已经踩过的坑：

**归属不接受参数。** 签名里没有 `owner_id`，owner 恒等于 `user_id`（这把 key 映射到的人）。加一个 owner 参数就等于允许往别人名下建项目——同一天刚踩过：一把标着某人名字的 key 实际指向同事账号，拿它写稿会把稿子记进别人的历史库。归属只能由服务端定。

**同名不建第二个**，把已有项目的 `project_id` 还回去（`created: false`）。建重了的代价不是多一行：两个同名项目 = 两套互不可见的历史库，`check_drafts` 按 `project_id` 比，从此对这个方向**永久失效且不报错**。

**同品牌照建，但列出兄弟项目**（`siblings` + `siblings_note`）。「途鸽」已经有 D-1..D-7，再建一个叫「途鸽」的顶层项目多半是误操作——但也可能真要开新方向，只有人能判断。所以给信息不拦，并明说新项目跟兄弟项目**不互相查重**。

`tests/test_create_project.py` 钉的是被禁止的形态：签名里出现任何含 `owner` 的参数即失败；撞名后库里行数不变；无身份必抛；`db.create_project` 返回空时必抛（不许降级成带 `error` 的"成功"——调用方会拿着不存在的 `project_id` 往下走，后面每个工具都 404 而根因看不见）。另有一条断言覆盖**全部写类工具**都不许被 `_safe` 包，而不只钉这一个。

### 3.5 规则的生命周期 —— 让写手自己管（2026-08-28）

**要解决的是一句产品要求：** 技艺库的判断、启用停用、迭代，要跟着写的人自己的
习惯和意志走，而不是靠一个不写稿的人在后端调。

在此之前这件事做不到。规则只进不出：生产库 305 条记忆里 `status='rejected'`
的是 **0 条**，`muted_until` 被用过的是 **0 条**。不是没人想关掉过规则，是
**关不掉** —— schema 里这两个字段都在、`store.shared_memories` 也一直尊重
`muted_until`，但**没有任何工具能写它们**。想改就只能直连数据库，也就是只能
找运维。

补的是两个工具和一个字段：

| 能力 | 之前 | 现在 |
|---|---|---|
| 新增一条规则 | ✅ `record_rule` | 同上，多了 `applicability` |
| 看见库里有什么 | ❌ 无（`my_style` 只回个人调校笔记） | ✅ `my_rules` |
| 停用 / 恢复 | ❌ 无 | ✅ `set_rule_state` mute/unmute |
| 升降档 | ❌ 无 | ✅ `set_rule_state` retire/promote |
| 设方向 | ❌ 无 | ✅ `set_rule_state` set_direction |

**四个 state，写手看得见。** `my_rules` 给每条规则算一个状态：生效中 / 方向不符
/ 试用 / 已停用。这四种在写手眼里以前长得一模一样 ——「我明明设过这条规矩怎么
没用」，而答案可能是任意一种。看不见就只能回来问运维，那正是这套东西要消灭的
环节。

**`retire` 是降档不是删除。** 降回 `candidate`，不再进简报，但行留着。删了就没法
回头看「这条当初为什么被记下来」，而技艺库最值钱的恰恰是那段演化史 ——
`calibration_note_audit` 已经攒了 99 条这样的历史。

**`mute` 默认 90 天，且会自己到期。** 所以「暂时别用」和「以后都不要」分不清时
用 `mute`：猜错的代价小。skill 里要求模型先问这一句。

#### 3.5.1 方向档（`applicability`）

同一条技艺在两种方向上经常是**相反**的要求。这不是推测 —— 把 27 份调校笔记
（约 25,000 字、381 条批注）聚成 37 簇之后，三路独立质疑反复撞到同一件事：

| 一边 | 另一边 | 质疑者的原话 |
|---|---|---|
| 结尾截断留白（22 次） | 结尾完整收口（8 次） | 「流量向要评论区互动，体验向要留白」 |
| 去品牌与渠道标签（11 次） | 正文直接嵌品牌名（3 次） | 「点不点品牌名是产品向与流量向的分界线，属方向定义」 |
| ①②③编号分点（7 次） | 时间线叙事（20+ 次） | 「按内容类型定，不按品类」 |

每一对的项目集几乎互补。它们不是互相矛盾的两条规则，是同一条规则的两个条件
分支 —— 没有方向这个维度，技艺库必然自己打架。

实现上有两个刻意的保守选择，都是为了避免**无声**的行为变更：

**判据只认「产品向」/「流量向」两个词。** `applicability` 是自由文本，生产库里
已经有 `正文` / `改简历部分` 这类**部位**标注在用（6 行）。把闸门做成「有
`applicability` 就按它过滤」会把这些老行全部静默挡掉。`filter_by_applicability`
只管带方向词的，其余原样放行。

**判不出方向就全放行。** `detect_direction` 从项目名和战术名里找方向词，两个词
同时出现（「产品直出-流量版」）算判不出来。保守方向必须是「多注入」而不是
「少注入」：漏注入一条写手明明设过的规则是无声的，多注入一条他至少看得见。

`counts` 里回显 `direction` 和 `soft_dropped_by_direction`，因为「被方向挡掉」和
「根本没存进去」在写手眼里同样是一片空白。

⚠️ 方向闸排在相关性过滤**前面**、封顶前面。相关性靠 embedding，而 110 条规则里
只有 2 条有向量 —— `filter_soft_by_relevance` 对无向量的一律放行，所以今天那一层
几乎拦不住东西，方向闸是真正起作用的那道。三者都必须在 cap 之前：把相关性挪到
cap 后面会让注入数少于 12，而池子里还有相关的排在第 13 位没能补上。

`tests/test_rule_lifecycle.py`（29 条）钉的是被禁止的形态，五个变异全部被抓：
`applicability` 从 select 列清单里掉出去、方向闸误伤部位标注、判不出方向时收紧、
改别人的 global 规则、状态入口改到归属字段上去。

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

> **一批之内也错开（2026-10-20）。** 10-20 生产诊断：稿子篇幅短、批批长得像，飞书里大半被人重写。台账只管跨批次不重复，同一批之内原来什么都不管——洗牌后直接取前 n 个，5 篇的内容形式全不撞只有约 1/5。现在 `_spread_order` 把洗好的候选贪心重排：优先内容形式和标题句式都还没用过的，其次有一个没用过的，再次随便；同档里情绪杠杆没用过的优先；某一维 8 个值用满一轮就清空重来。所以 n=8 时 8 种内容形式、8 种标题句式各一，n=10 时同一种最多两篇。仍是随机（先后来自洗牌，同 seed 结果不变）；选择仍在 RPC 的事务里做，这里只是排序——近期用得多的项目，被避重跳过的那一位由后面的补上，偶尔会多撞一个；降级路径先去掉避重的再排，不打折。
>
> 同一天的另外两处，都在 `open_project` 的简报里：
> - **`recent_titles`**（有才出现）：本项目近 30 天发出去的标题（`tv-sync` 对到本项目上的笔记，窗口按 `truth_vault.notes.publish_time`，只取 `note_id` / `publish_time` 两列；`ingested` 取版本标题，其余优先 `tv_edit_diffs.published_title`，人可能改过）和近 14 天在写作台写过的标题（补录副本不算），每条 ≤40 字、合计 ≤20 条，不带正文。两栏各有份额：写过的留 8 条、发出去的占其余，哪栏不够空位给另一栏——不能"发出去的先占满"，活跃项目 30 天对上的笔记远超 20 条，那样昨天写了还没发的几篇（下一批最容易撞的）永远进不来。`task` 让模型新写的稿子标题别和它们撞主干词和具体说法、开头别复述它们、人设和核心场景尽量错开；**标题句式照发牌坐标写**（句式只有 8 种，近 20 条标题几乎总把它们占全，"句式也要不一样"和发牌的硬坐标没法同时做到），并写明优先级：p0 / stable 定的人设、发牌分到的坐标和切入角度、用户点名的题目和改稿都在这一栏之上，改稿不受它约束。查不到就不带这一栏（两栏各自兜底），简报照给。
> - **`validated_patterns_usage`**（有规律才出现，`borrow_lessons` 同样带）：85% 的写手日简报都带着同样 4 条已验证规律，其中一条是"整篇围绕一个求助问题来组织"，模型把 4 条全用在每一篇上，批批同一副骨架。规律清单原样转交（馆员给的，写作台不改），旁边说清楚：每篇最多用一条、一批里按篇号轮换、有几篇一条都不用；它们是表现好过的选项，不是模板；和发牌坐标或 p0 冲突时以它们为准。轮换是 k 条规律加一个"不用"排成一圈，起点按 (项目, 写手, 北京日期) 的哈希定（`open_project` 和 `borrow_lessons` 同一天给同一句），两篇以上的批次没轮到"不用"的、最后一篇不用——固定从第 1 条起的话，每批第 1 篇都是同一条规律，3 篇一批、4 条规律时也轮不到"不用"。`draw_angles` 拿不到规律清单（不借卡），所以按篇号的轮换写在用法里，不在发牌回包里。
>
> **再加一处：`recent_structure`（有才出现，2026-10-20 第二批）。** `recent_titles` 管不到骨架。Jev 给每篇打分的稿子答过 TV 特征题库（fq-v0.1），答案在 `truth_vault.note_feature_answers`（`aw_version` × `versions.id` × `primary` × `jev:*`），回包里的 profile 原来是扔掉的。开项目时读回本项目最近 ≤30 篇打过分的稿子（近 60 天按版本自己的时间算、补录不算、一个 item 一票，版本 id 按篇摊名额；没判出答案的不占名额：最近 60 篇里有答案的凑不够 30 篇、更早的稿子或没问过的旧版本还有，就把还没找到答案的接着往下问一轮，最多 3 轮。judge 停过一阵是这样，judge 不覆盖的项目——没配 judge、`tv_project_map` 没映射（一篇都不判）、只判了一部分——也是这样，后者每次开项目都问满几轮、多几趟空请求，有界；补问那几轮失败只记 warning，第一轮找到的照用），某一处扎堆的那种答案占比 ≥ 0.6 就摆出来：`{window, concentrated: [{question, answer, share, of}], task}`（`of` 是这一题有有效答案的篇数、即 share 的分母，可能比 window 少，也要 ≥ 12 才报），让这一批每一处至少一半换个写法；p0 / stable、发牌坐标、用户点名的写法优先，改稿不受约束。少于 12 篇、不扎堆、账本读不到就不带。三步往返（批次 → 版本 → 答案；答案按主键排序翻页，Jev 换模型重判过几代、行数过了 1000 也不截；一轮是一页加一次空页，最近 60 篇凑够 30 篇有答案的就只问一轮，往下补问的每一轮再来一趟），只取答案列（不取 evidence、不取正文）。
>
> 问哪几题见 `deskcore/structure.py`（10-20 拿近 169 篇稿子对照 2,500 多篇已发笔记挑的）：只挑已发笔记本身不扎堆的结构题——有个项目近百篇稿子 86% 第一句是具体事件、82% 标题是问句、95% 写了已经发生的坏结果，它的已发笔记只有 45% / 43% / 35%。bool 题只报「是」扎堆；`product_role`（产品在文中的角色）不问——主角、顺带一提、只暗示、不出现，哪一种扎堆都是项目的露出要求（产品向项目九成稿子产品当主角正是要求本身），不是写法习惯；已发笔记本来就八成以上的（交代身份、亲身经历）、具体性几题、已验证规律用到的题（第一批让模型轮换着用，扎堆是有意的）都不问。篇幅 / 段落形状是 TV 的代码特征，稿子没有答案行，这里做不了。

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

> **2026-09-24 接上了一半**：`migrations/011` 的视图 `v_angle_outcomes` 把用掉的坐标经 `tv_note_links`（§3.7）连到了 `truth_vault.notes.tier`，「哪些坐标出了爆文」现在查得出来（见 §3.8 末尾）。**加权本身还没做**——`draw_angles` 仍是均匀随机，按规律加权要等闸二，且保留 15% 纯随机（D-065 续）。

### 3.6 人审 —— 定稿之后那一步（2026-09-16）

**缺的从来不是"把 pending 改成 approved"，是一个真的有人点过的动作。**

`commit_drafts` 建的 item 是 `pending` 且不盖任何决策戳，这是对的：定稿不等于审核。标成 approved 会让每条定稿变成一条伪造的人工评价去校准 TV 的评估模型，而且灌进去的是**清一色正例**——COR-004 治的正是这件事，换个门重犯一次一样糟。

但在此之前，写作台这条路**没有下一步**。只用它写稿、定稿、导出的团队永远产不出一条人工审核决定，于是 TV 按 `status ∈ (approved, needs_revision)` 捞行时一条也捞不到，**而且不报错**——查不到就是 0 条，跟"这几天没人审稿"长得一模一样。

`review_drafts` 就是那个动作。三条纪律写进了工具本身：

| 纪律 | 为什么 |
|---|---|
| 审稿人**恒为调用者**，签名里没有 reviewer 参数 | COR-004 治的正是"把 owner 当 reviewer"。留个口子等于把它请回来 |
| **只认 `approved` / `needs_revision`** | `pending` 也在拒绝之列——迭代后重置回待审是 `SYSTEM`，既不是审稿也不是检测，从人审入口进来就是伪造 |
| **打回和通过同等公民** | 只能点通过的入口产出的仍然是清一色正例，与不做无异 |

⚠️ **不设 `best_version_id`。**「选为最佳」在 UI 里是独立于审稿的动作（走 `db.set_best_version`，不盖决策戳）——把两件事合起来会污染出处数据。写作台的 item 每条只有一个版本，代表版本自然落在它身上。

⚠️ **用户没表态就不许调。** 这是给模型的纪律，写在协议里：他只说"写 20 条""入库""导出"，那都不是结论。替他点通过等于灌一条伪造的正例，比不记更糟。

⚠️ **2026-10-09 审计 A-01：这条路写的是 `human_via_agent`，不再是 `human`。** 实查生产库，`decision_source='human'` 的 32 行**全部**是 `review_drafts` 写的，而且全落在模型自己的工具链里（`commit_drafts` 之后 10~27 秒就"审完"）；deskcore 不记成功的工具调用，于是库里分不出「模型自己点了通过」和「用户说了全过、模型照实记」。服务端判不了用户到底说没说过，能做的是把证据留下来：

| 改了什么 | 为什么 |
|---|---|
| 工具只写 `decision_source=human_via_agent` | `human` 留给 Streamlit 里真的点了按钮的路（`app.py`，一个字没改）。下游一眼能分开，要不要把它当人工反馈由消费方定 |
| `user_words` **必填**（1~200 字，strip 后），落 `items.decision_note` | 用户给结论时的**原话**，给人复核"这条通过是不是真有人说过"。缺 / 空 / 超长 → 返回 `error`，**整批不写、库都不查** |
| `decided_within_s` 服务端算（距这批稿子 `batches.created_at` 的秒数，batch 无时间存 NULL） | 入库几秒后就"审完 20 篇"一眼就看得出 |
| `commit_drafts` 返回值多一句 `next_step` | 入库之后模型最常犯的下一步就是紧接着自己调 `review_drafts`，纪律要在最容易犯错的那一刻眼前 |

迁移是 `012_review_via_agent_provenance.sql`（两列 + CHECK 多一个值 + 一条只盖 `human_via_agent` 的部分索引；006 那条 `human` 索引不动）。存量 32 行**不回填**——数据里没有证据说它们是哪种。协议正文 `protocol.md` 这次**没动**：它的每个字都盖进 `protocol_version`，改一行就逼所有写手重新导入 skill，规则先放在工具 docstring 和 `next_step` 里，下次正文真要改版时一起补。`tests/test_review_provenance.py` 用 AST 守着 `deskcore/` 下不再有写 `'human'` 的代码。

⚠️ **TV 侧还没接**：`sync_autowriter_decisions_to_prepublish.py` 只认 `decision_source` 恰好等于 `human` 才算人工，`human_via_agent` 现在会被它归成 `unverified`。要不要算、怎么算，由 TV 的 DECISIONS 定，本仓不替它决定。

`commit_drafts` 那边仍然不盖决策戳，`tests/test_deskcore_review.py` 最后一条断言专门守着"入库路径不许写 `decision_source` / `reviewer_id`"——最容易犯的下一个错就是"既然现在能记人审了，那让 commit 顺手记一条"。

**剩下的在 TV 侧**：它现在仍然把捞到的全部写成 `evaluator_type='human'`、不读 `decision_source`。AW 这侧的字段已经齐了。

---

### 3.7 写作台 ↔ TV 的稿子对照（2026-09-17）

**病灶。** TV（`truth_vault.notes`）里 5966 条已发笔记，`source_autowriter_version_id` 全 NULL。
原设计（§3.4）让运营把 `export_drafts` 导出的六个 lineage 列手抄进飞书表，三周零匹配——
到 TV 手里的表**根本没有那六列**。反过来看更糟：08-28 建的项目里，岸深发了 144 篇一篇
都没进过库，西屋 301 篇库里连版本都没有，sportsix 上线第一周发了 54 篇入库 0——查重闸对
它们一无所知，而这不是 09-11 才开始的，是从上线第一天起的常态（#80 里"09-11 之前 110%"
那个数字是百健士 09-07 一次性补录 201 篇撑起来的，其中 101 篇发布时间早于入库时间）。

**改法：在库里按内容对，运营不做任何事。** 两边在同一个 Supabase 项目里，内容都是写作台
产的；TV 存的是全文（百健士对上的 143 篇里 115 篇正文逐字相同，其余只多了话题标签）。

| 步 | 做什么 | 口径 |
|---|---|---|
| 对照表 | `tv_project_map`：TV 的 `project_id`（`SPX_phase1`）↔ 写作台项目。一个 TV 项目可对多个写作台项目（WTG 15 个方向），**只有一行**是补录目标 | `tv-map add` 维护，`migrations/009` |
| 精确 | 正文前 40 字（去标点）相同 → `body_exact`；否则标题相同 → `title_exact` | 同标题多版按四字串包含度分，分不出再按时间窗内离发布最近的那版 |
| 模糊 | 时间窗内（发布 −30 ～ +30 天）四字串**包含度** ≥ 0.6 且与次佳差 ≥ 0.1 → `fuzzy` | 途鸽发之前改得很狠（305 字 vs 527 字），精确对不上，靠这一路 |
| 分不出 | `ambiguous`：候选写进 `tv_note_links.candidates`，**不硬猜**，报表里列给人看；人看完说「对不上」就 `tv-resolve --note … --ingest`，正文补录、对照改成 `ingested` | 两版同标题的评论稿都不是笔记正文（途鸽 2026-09-18）；不提供「指定某一版」，`match_kind` 没有 manual 一档 |
| 对不上 | `unmatched` → `ingest_published`（剥话题标签、不过闸、按全文幂等）补录进目标项目，建成的 `version_id` 记回 `tv_note_links` | 同项目稿子都以同一串标签结尾，不剥会互相误撞 |
| 回填 TV | `--write-tv` 才把对照写回 TV 的 `source_autowriter_*` 两列，**只填 NULL 的行**，且**只写真对上的**（`body_exact` / `title_exact` / `fuzzy` / `tv_lineage`）；`ingested` 不写 | 那是 TV 的列；补录的版本是从笔记复制来的，写回去因果倒置（TV 2026-09-18 核对时定的口径，见 runbook §1.4） |
| 互斥 | 补录走 `core.ingest_published`，它先拿库里的项目锁（`ingest_locks`，TTL 10 分钟、到期可接管），拿不到等 90 秒后报「另一个补录正在跑」（REST 409） | 服务进程的工具与 CLI/cron 的 `tv-sync` 是两个进程，进程内锁管不到对方；advisory lock 跨不过 PostgREST 的多次请求 |

实测（2026-09-17 的库，按全部 5000 多个版本对）：WTG 724 篇对上 229、百健士 182 对上 143、
唐小轻 127 对上 51、sportsix 492 对上 26、雷诺考特 374 对上 17、途鸽 127 对上 17；
对上的发布−入库间隔中位 1～3 天、九成 13 天内。对不上的那些绝大多数是**库里根本没有**
——`tv-sync` 会把它们补进去，之后每天跑一次。

拿真数据跑过一遍匹配器（途鸽 127 条 TV 笔记 × 166 个写作台版本，含 8 个子项目）：开头相同 15、标题相同 49、模糊 7、分不出 1、对不上 55——42 条爆款里 33 条认回了写作台的版本；对不上的 55 条最近的候选包含度都在 0.2 以下，确实不在库里，`tv-sync` 会把它们补进去。

跑法、验收、cron 见 runbook §1.5。匹配逻辑在 `deskcore/tvlink.py`，编排在 `core.tv_sync`，
用例 `tests/test_tvlink.py` / `tests/test_tv_sync.py`。**§3.4 的六列不再是回流的依赖**，
`export_drafts` 照样带着它们（粘了不坏），但没人需要再手抄。

### 3.8 入库判定 judge（2026-09-24，影子期；2026-10-19 起给写手看）

> **D-120（2026-10-19）**：判定结局不再是影子。`score_drafts`（`commit_drafts` 的新名）返回的 `judge.results` 每篇带
> 参考分 `score` 和最多 3 条 `fixes`（`core.draft_score`：Jev 有把握 p≥0.7 的硬伤 −25、拿不准的 −8、没判出的硬约束 −10，
> 正文太短不给分）。**仍然不拦**：written / rejected 口径不变，判不成照样存档。人审与导出两个工具撤了，评论按链接写
> （`comments_for` / `score_comments`，§3.6 的人审流程只剩 core 里的函数）。下文"影子期"的表述按此读。

**是什么。** JevforCoentent 仓 docs/00 #4 拍板：写作台的入库判定挂在 `commit_drafts` 内部，advisory，
8 秒没回就跳过，答案进账本（TV 的 `note_feature_answers`，`subject_type='aw_version'`、
`subject_id = versions.id`），影子期只记不拦。judge 是一个独立的 HTTP 服务（`POST /judge_draft`），
本仓只有一个薄客户端 `judge_client.py`，照 `librarian_client.py` 的形状写：env 配置、超时、
fail-open、绝不抛。**deskcore「一次 LLM 调用都没有」不破**——Jev 调用在 judge 那边，deskcore 拿到的
是事实，与借卡调馆员是同一种关系；`ci.yml` 那条 AST 守卫只禁 Anthropic 调用，HTTP 不在其列。

**决定一：覆盖面只到 `commit_drafts`，补录路径不判**（设计审查 2026-09-23 §4 #1）。
docs/31 §5.1 与 docs/00 #4 的理由是「写手抽完就散场（D-071），入库是每篇稿必经的唯一一步」——
那是把 D-071 读反了。D-071 的实查是写手**没散场**，继续用写作台发牌，但稿子在外面写、发布后由
tv-sync 倒灌回来，不过 `commit_drafts`（sportsix 438 条 ingested 里 114 条是 9 月发的）。所以两条路分开定：

| 路 | 进来的是什么 | 判不判 | 谁覆盖 |
|---|---|---|---|
| `commit_drafts` | 写作台交付、**未发布**的稿 | 判（本节） | judge → 账本 `aw_version` |
| `_ingest_published_unlocked`（`ingest_published` 工具 / `ingest --xlsx` / `tv-sync` 补录） | **已经发在小红书上**的笔记 | **不判** | TV 的事后特征抽取（docs/31 位置 ①，同一把 fq 尺子，`subject_type='note'`） |

补录路径不判的理由：已发布的稿子「入库前判一下」改变不了任何事；再以 `aw_version` 判一遍，是同一篇文字
在账本里记两份、挂两种主体；而它们本来就在 TV 的抽取队列里。**代价写明**：sportsix / Hatherine 这种
在外面写的项目，**写前**的判定也在 deskcore 里：`judge_draft` / `repair_plan_for` / `list_banks` 三个工具（2026-10-09 从写手机器上的 `judge.mcp_server` 并进来）——同一个 `/judge_draft`，但 `write=false`、不回账本行，项目号与品类走 `_judge_project_context`，超时 `JUDGE_DRAFT_TOOL_TIMEOUT_SEC`（默认 18 秒，Jev 实测一篇 4.5 秒，留在 MCP 客户端 ~22 秒容忍之内）。judge 仓的 `judge.mcp_server` 只剩内部 / 运维机器用。
`judge_draft` / `repair_plan_for`），不靠这里。⚠️ 「由 TV 覆盖」的前提是那条笔记**在 TV 里**：从飞书表
`ingest --xlsx` 补进来、而那张表没接 TV 同步的（Hatherine 目前就是），事后抽取也够不着——两边都判不到。
补救是把那张表接进 TV（接进去之后再加一行 `tv-map`），不是在补录路径上补一刀判定。这条决定写进了 `_ingest_published_unlocked` 的 docstring，
`tests/test_deskcore_commit_judge.py` 最后一条钉着「补录不调 judge」。（跨仓的决策记录归 truth-vault
`DECISIONS.md`，本仓不越界去写，见 runbook 待办。）

**决定二：判定在项目写锁之外、在恢复指纹的那个大 try 之外**（设计审查 §5）。`commit_drafts` 原来在锁和
try 里面 `return out`；现在改成落到 `with` 块外面，再判：

- 放在 try 里：判定一抛，那个 `except` 会把**已替换稿的旧指纹**无条件放回——新旧两版指纹并存，下一版判
  自己撞车；再 `raise` 出去，调用方重试又撞上自己刚写的指纹。一次增强项的故障变成一句「你的稿子重复了」。
- 放在锁里：就算自己包了 try，多等的 8 秒也全算进同项目其它 commit / 补录的 90 秒等锁预算。

判定自己逐篇兜底不抛，外面再兜一层；兜到的只记进 `out["judge"]`。`ci.yml` 钉在 `commit_drafts` 源码上的
那几条（`_gate(`、两行 `embedding_model`、`embed_failed` / `embedding_warning`）一条没动。

**判哪几篇。** subject_id 必须是一条**真实存在**的 `versions.id`，否则账本 JOIN 不回写作台、钱白花：

| 情形 | 判不判 |
|---|---|
| 新稿、身份建成（`version_ids` 里的） | 判 |
| 调用方自带 `version_id`（UI 生成的稿，库里本来就有那一版） | **判**。⚠️ 它们**不在** `version_ids` 里（那里只报这次新建的），照 `version_ids` 判会整批漏掉 |
| 调用方自带的 `version_id` 不是本项目的版本（库里没有 / 别的项目的 / 不是 UUID） | 跳过（`version_not_in_project`）。发之前按本项目核一遍（`store.items_for_versions`，与核 `replaces_version_id` 同一个查询）：`write = true` 会按 subject_id 写账本，过期的 id 留下孤儿答案，别的项目的 id 把这篇的判定记到那个项目名下（codex review P1 on #92）。发的是库里的规范写法。归属查询本身失败的这几篇不发、记 `error`，本次新建的照发 |
| 改稿替换、新版挂上了旧 item | 判，用新版的 id |
| `identity_error`：指纹入了库、versions 没建成 | 跳过（`skipped.reason = identity_error`） |
| 替换没挂上（`replace_warning`） | 跳过（`replace_failed`） |
| 正文为空 | 跳过（`empty_body`）：只有标题判不出东西，平台题却可能对着一个标题给硬伤 |
| 被闸拒掉的 | 不判：没交付，不该在账本里留答案 |

**项目号与品类（数据出境口径，docs/00 #7）。** 未发布稿默认只跑通用层 + 平台层，处方药项目的未发布稿
不跑，由 judge 执行；写作台每次都带上它判断所需的两样：

- `project` = **TV 的项目号**（`SPX_phase1` 这种），从 `tv_project_map`（`009`，tv-sync 用的同一张表）
  查：一个写作台项目对多个 TV 项目时取补录目标那一行，没有就按项目号排第一个。写作台的 UUID 在 TV 那边
  认不出来，所以**不拿它凑数**：认不出来的项目 judge 只能按「未清关」跑通用层，恰好漏掉「处方药不跑」——
  一个没接 tv-map 的处方药项目的草稿就这样发给了 Jev。因此**没有映射就一个请求都不发**，记
  `not_configured`，detail 里写着补救命令 `tv-map add`。
- `category` = `truth_vault.projects.category`（TV 的受控词表：处方药 / OTC药 / 保健品 / …），经
  PostgREST 的 `schema("truth_vault")` 读（本仓第一处这么读 TV 的地方，前提是 TV 的 Exposed schemas 与
  service_role 表级 GRANT，见 `store.tv_project_categories`）。几个映射里只要有一个是处方药就报处方药
  （出境上宁严勿松）。读不到就只发 `project`，不影响判定。⚠️ 查完**不关** `schema()` 回来的那个 client：
  锁文件的 supabase 2.30 里它自带 session，关不关都不碍事；但 `requirements.txt` 放行的老 2.x（如 2.10 +
  postgrest 0.18）里它就是父 client 自己，关掉就是关掉父 client 的连接，紧接着的 `batch_metrics` 静默写不进去
  （codex review P2 on #92）。连接的生命周期归持有它的一方。

不带 `brief`：写作台的规则是自由文本，编不成闭集题（那要模型）；未清关项目的项目层本来也会被 judge 按
出境口径丢掉。请求体其余几项：`judge_paras = "never"`（不判段，一篇的调用次数压到最少才放得进 8 秒）、
`write = true`（账本由 judge 写）、`run_tag = "primary"`、`return_rows = false`。

**结局（`judge_status`）。**

| 结局 | 来路 | 要不要人管 |
|---|---|---|
| `ok` | 200 | — `passed` 是判定结论，影子期不拿它拦任何东西 |
| `not_configured` | 没配 `JUDGE_URL` / `JUDGE_API_KEY`；或项目没接 tv-map | 部署 / `tv-map add` |
| `timeout` | 连接或读超时，或整批的共同截止到了（含发之前的查库没在截止内查完，那时一篇都没发，`judge.detail` 会说）。⚠️ judge 那边可能照样判完写了账本，只是写作台没等到 | 攒多了看 p95 |
| `policy_blocked` | 403 且 detail 以 `policy:` 开头 | 不用，**永不重试**（合同确认之后是 judge 那边的配置） |
| `bad_request` | 422 | 是写作台这边的 bug，**永不重试** |
| `unavailable` | 401 / 503 / 404 / 连不上 | 部署或配置 |
| `jev_failed` | 502 | judge 那边 |
| `error` | 其它状态码、200 但结构不对、意料之外的异常 | 看 detail |

**延迟。**「`commit_drafts` 现在约 1–2 秒」「一批 10 篇并行也是一两秒」在仓库里都没有依据，8 秒也没有。
所以：多篇**并行**发（`JUDGE_MAX_WORKERS`，默认 8），**整批共用一个截止**（`JUDGE_TIMEOUT_SEC`，默认 8，
夹在 1–15；`nan` / `inf` 与写坏的一样退回默认值），到点没回的记 `timeout`、还在排队的直接取消（不会再发
出去），不会串成 N × 8 秒。**截止从发之前的查库就开始算**：`tv_project_map`、跨 schema 的品类、自带
`version_id` 的归属这三次查库在工作线程里跑、算进同一个截止（PostgREST 客户端默认超时 120 秒，原来它们在
截止之外同步查，库一卡，一次已经入库成功的 commit 就挂着远远过了 8 秒——codex review P2 on #92）。到点没查完
就整批记 `timeout`、一篇都不发，commit 照常返回；查完了，判定只拿剩下的时间。上界是 MCP
客户端实测能容忍的 ~22 秒（`config.py` 里 `LIBRARIAN_TIMEOUT_SEC` 那段，D-074）。**2026-10-16（J-08）起判定只拿
整次回包里剩下的**：截止 = min(`JUDGE_TIMEOUT_SEC`, `COMMIT_RESPONSE_BUDGET_SEC`(20) − 入库已用 − 跨项目检查的
1.5 秒)，不到 1 秒就一篇不发、整批记 `timeout`。实测（10-12～10-15，有判定的 9 批 / 84 篇）判定段最长 4.3 秒、
单篇最长 3.4 秒，8 秒一次没碰到；但入库段 10 篇一批已到 11～12 秒，判定再拿满 8 秒就是 ~21.8 秒，贴着客户端
容忍——超了调用方会以为入库失败去重试、撞上自己刚写的指纹，判定超时只是影子期少一条账本记录。返回值带 `elapsed_ms` 与
`phase_ms = {lock_wait, commit, delivery_rules, judge, project_isolation, total}`（`lock_wait` 单列：补录正在跑时入库要
等锁，不拆开会把分位数带歪；`delivery_rules` 是存档之后、判定之前的交付版式 + 代码查规则（§3.9 / §3.10，规则那次是
翻页读的），判定的截止要扣掉它花的时间——2026-10-20 起单记，各段加起来约等于 `total`），**同一份数落进
`batch_metrics`**——它本来就是「每批一行的指标快照」（phase_ms / counters / meta 三个 jsonb），不需要新迁移。
`batch_id` 留 NULL（本次的 batch 记在 `meta.batch_id`），免得历史页给写作台批次挂上一块全是 0 的「性能指标」。攒够样本之后这样查：

```sql
select count(*),
       percentile_cont(0.5)  within group (order by (phase_ms->>'commit')::int) as commit_p50,
       percentile_cont(0.95) within group (order by (phase_ms->>'commit')::int) as commit_p95,
       percentile_cont(0.95) within group (order by (phase_ms->>'delivery_rules')::int) as delivery_rules_p95,
       percentile_cont(0.95) within group (order by (phase_ms->>'judge')::int)  as judge_p95,
       percentile_cont(0.95) within group (order by (phase_ms->>'total')::int)  as total_p95
  from autowriter.batch_metrics
 where meta->>'mode' = 'deskcore_commit' and created_at > now() - interval '14 days';

-- 判定结局的分布(影子期要回答的就是: 判了多少、超时多少、被出境口径挡了多少)
select j->>'status' as status, count(*)
  from autowriter.batch_metrics m, jsonb_array_elements(m.meta->'judge') j
 where m.meta->>'mode' = 'deskcore_commit'
 group by 1 order by 2 desc;
```

**回给调用方的。** `out["judge"] = {shadow, note, project, category, summary, results, skipped}`（整批一个都没发出去时
多一个 `detail` 说原因，只说一次）；`results` 每篇一行：`index / version_id / judge_status`，其余
`passed / hard_fails / calls / written / policy / elapsed_ms / http_status / detail` **有值才带**——这块东西每次 commit
都回给调用方模型，没接 judge 的部署一批 20 篇就是 20 行空壳，返回值越长越容易把协议挤出模型的注意力。
`hard_fails` 只带 `[题库, 题号, 答案, 概率]`，**不带证据句、不带修改单**：影子期的判定还没过闸二，返回里多一块
「不过的题」，模型最自然的反应就是去改稿——所以 `note` 与工具 docstring 都明说「只记录，不要据此改稿或重新提交，
判定失败也不用重试」。修改单留给写手侧的 MCP 工具。`written` / `rejected` / `version_ids` 的口径一个字不变，
判定怎么失败都一样（`tests/test_deskcore_commit_judge.py` 用真客户端 + 超时 / 403 / 整个挂掉三种情形比对过）。

**影子期之后**（不在这次范围）：等闸二有了验证过的特征，把「落在历史最低 20%」和「平台题库硬命中」标成
`revise`，仍然不拦（docs/31 §5.1）。到那时再决定修改单要不要回给写手。

**台账接上结果：`v_angle_outcomes`（`migrations/011`）。** 同一份拍板记录里「顺手查到 · 写作台」那条：
`angle_ledger` 不记结果，发牌加权之前要一个 `consumed_version_id → tv_note_links → notes.tier` 的视图。
两段 JOIN 的类型一一对上（UUID ↔ UUID、TEXT ↔ TEXT）。口径：只列用掉了的坐标；还没对上笔记的
`note_id` / `tier` 为 NULL（算爆文率要分母）；只认真对上的对照（与 `_TV_BACKFILL_KINDS` 同一份名单）；同一版发了
两条笔记就是两行。⚠️ 权限：三张底表 RLS 开着且无 policy，视图以属主身份读（`security_invoker = false`），所以
**必须由底表属主建**（换个普通角色建出来的视图永远 0 行且不报错，迁移里先验、不够格当场报错），且**显式
GRANT**（`autowriter` 没有 default privileges，新视图对 service_role 也是零权限）。只发给 `service_role`：这是
绕过 RLS 的明细视图，发给 anon 等于公网可读；看板要看，在 TV 那边照 `dashboard_views_v1.sql` 建只吐计数的聚合视图。

```sql
-- 按主维度看坐标的结果(只看已对上笔记的)
select dims->>'emotional_lever' as lever, count(*) as published,
       count(*) filter (where tier in ('爆', '大爆')) as bao
  from autowriter.v_angle_outcomes where note_id is not null
 group by 1 order by bao desc;
```

### 3.9 交付版式由代码拼（2026-10-20）

**为什么。** 10-20 生产诊断：写作台的稿子只有 23~45% 原样发出去（有一个项目是 0），其余在飞书里被人重写。其中一层是版式：
版式写成规则交给写手的模型（WorkBuddy）照着排，时灵时不灵——同一个项目攒了好几条互相打架的「交付格式」规则；老的独立
写作台是在代码里拼的（`exporter.py` / `app.py` 的复制块），从没出过这个问题。所以版式回到代码。

**模板在哪。** `projects.default_params` 的 `delivery` 键，不加迁移。这一列生产库 80 行都存成 JSON **字符串**（`isolation_terms`
也在里面），读写两侧都按字符串处理、也认 dict。只能用运维命令设：

```bash
python -m deskcore.cli delivery-format --project <uuid> --show          # 现在的模板 + 一篇合成示例稿拼出来的样子
python -m deskcore.cli delivery-format --project <uuid> --set 模板.json   # 先校验, 写回时保留别的键; 文件里是 null 报错不写
python -m deskcore.cli delivery-format --project <uuid> --clear
```

写手那边没有对应的工具：版式是项目定的，不该在对话里被模型改掉。模板形状（`deskcore/delivery.py`，校验是严的，不认识的键直接拒）：

| 键 | 说明 |
|---|---|
| `layout` | `table`（GitHub 风格的 markdown 表格，一篇一行，格内换行写成 `<br>`、`\|` 转义）或 `blocks`（一篇一段，空行隔开） |
| `columns` | 只给 `table`：`[{"name": 表头, "field": "direction" \| "post" \| "comments"}]`，顺序即列序，必须有一列 `post` |
| `markers` | 只能是 `【标题】` / `【正文】`（默认值）。tv-sync 的 `tvlink.parse_raw` 只认这两个标记，09-15 以来 6 个在跑项目发出去的 450 篇也全是这个写法；冒号写法会让整段被当成正文、对不回底稿，所以校验直接拒 |
| `tags` | `content`：写手给几个话题（0~10）；`fixed`：固定话题，接在后面；`conditional`：`[{"when_any": [词], "add": 话题}]`，标题或正文里出现任一词（不分大小写）就加 |
| `comments` | `null` 或 `{min, max, label_reader, label_author, must_mention_any}`；标签原样拼在每条评论前面。`table` 配了它就必须有一列 `comments`（反过来也是），不然评论被要求、被检查却拼不进交付稿；`blocks` 的两个标签都不能为空（评论紧跟在话题行后面，没标签分不出正文在哪结束），`table` 的可以为空 |

**怎么拼。** `score_drafts` 存完档（锁外、恢复指纹的 try 外，判定之前），只拿**存进去了的**那几篇按模板拼成一份，挂在返回值的
`delivery = {format, text, problems}`。一篇 = `【标题】标题` / `【正文】正文` / 话题行；话题顺序是写手的（去重、截到 `content` 个，
和固定 / 条件话题重名的不占名额）→ 固定的 → 命中的条件话题，统一成单个 `#`、不分大小写去重。写手把话题、评论、方向作为单独的
字段传（每篇 draft 里的 `tags` / `comments` / `direction`；`direction` 是这篇的切入角度，不是产品向 / 流量向），`body` 只放正文
（模板不管话题——`tags` 里 `content` / `fixed` / `conditional` 都没配——时例外：`assemble` 不读 `tags` 字段，正文原样进交付稿，
简报的 `task` 叫写手把项目要的话题照项目规则写在正文末尾那一行；原来照样叫写手「不放话题行」，传在 `tags` 里的话题一句不报就没了，codex review on aw#115）；
正文末尾要是还带着一行 `#话题`、或者最后一句后面直接跟着一串 `#话题`，模板管话题时会被摘出来（没单独传 `tags` 就用它们；传了 `tags: []` 算传了，摘下来的不用），
不会拼出两份。网址里的 `#片段`（`#` 前面紧挨着、一直是不带空白的 ASCII 的那一段像网址：带 `://`、`www.` 或 `域名/`）不算话题，留在正文里，查规则时也不算话题；
前一个字是英文字母数字的 `#`（`学习C#入门`、`1#楼`）同样不是话题，两种除外：紧接着上一个话题的连写（`#abc#def`，只看是不是紧挨着，
不往回找 `#`——前面 `#秋招[话题]#` 收尾的那个 `#` 不开一串，同一句后面的 `学C#入门` 照样不是话题），和带 `[话题]` 的（`拿到offer#秋招[话题]#`）。
`#秋招[话题]#` 后面接着正文的（`最后祝大家#秋招[话题]#顺利上岸`），收尾的 `#` 后面是正文，整句不摘。不带 `[话题]` 的话题本体到句读为止
（中文 / 全角标点、通用标点 U+2010~205E 和 `⋯`、ASCII 的 `, ; : ! ? " ' ( ) < > ~`；`.` `-` `_` `+` `&` `/` 和全角的 `＆ ＋ － ． ／ ＿`、
`〇` `々`、不断行的连字符、夹在英文字母中间的撇号都留在话题里：`#Node.js` `#C++` `#Kiehl's` `#二〇二六考研`；`validator.TAG_STOP`）：中文句子里
没有空白，原来 `今天聊聊#秋招，记得准备简历` 整个 `秋招，记得准备简历` 算话题，字数少算、后半句被当成末尾话题摘走（codex review on aw#115）。
带 `[话题]` 的照旧到 `[话题]` 为止（`#打工人！[话题]#` 是一个话题）。整行都是话题的，话题之间、末尾夹着的标点（`#秋招、#求职、#offer`、
`#秋招 #求职。`）跟着这一行一起摘掉；前面有正文的只认空白隔开，`冲鸭！#秋招！` 不摘、不丢字（独立复审 on aw#115 第八轮）。哪个 `#` 算话题只在
`validator.topic_tag_matches` 认一处：摘话题、查话题的规则、数字数（`max_len`）三处口径一样，摘末尾话题也只拿它切、不另写正则（codex review on aw#115）；
`comments` 只写一条（字符串或一个 `{role, text}`）也认；传成一串 JSON 的，解出来是列表就当评论列表、是对象就当一条，每一项照常查形状——
外面再包一层的 `{"comments": [...]}`、没有 `text` 的对象、列表里夹着的 `null` / 数字都算那一条形状不对（不往里多解一层），不会整段 JSON 带着「读者：」交出去；解不开的（`[捂脸R]求链接`；一长串 `[` 让 `json.loads` 抛 `RecursionError` 的也算）照旧是一条字符串（codex review on aw#115）。一条评论没有 `text`、或者 `text` 不是字符串（写成 `content`、给了列表 / 数字），
算这一条形状不对：报进 problems、不拼；查规则看的评论和交付稿是同一份（`delivery.comment_list`），交付稿里有哪几条就查哪几条。`tags` 传成整个列表 `json.dumps` 出来的一串（`'["a", "b"]'`）
也拆开认（`delivery.split_tags`；摘话题、话题行、查话题的规则都走它），不会拼出 `#["a", #"b"]`。`problems` 只报代码判得准的：话题不够、评论条数不对、某条评论形状不对、某条评论没提到 `must_mention_any` 里的词、
表格要方向而这篇没带、标题或正文里已经自带了标记、模板不管话题而 `tags` 里有正文里没有的话题（拼不进交付稿）；`index` 是写手传的位置。写手照协议先改 problems（带 `replaces_version_id`
再存一次），再把 `text` 原样交给用户。⚠️ 话题和评论不落库，服务端拼不回上一次的稿子，所以重新存的那一次只拼改过的那几篇，
写手用它按顺序替换原来那几篇（表格只换那几行，新返回的表头不再贴一遍）。

**写之前就让写手知道。** 有模板的项目，`open_project` 的简报带 `delivery_requirements`（`delivery.requirements`：要几个话题、
固定话题有哪些、评论几条和每条要提到的词、要不要方向，外加一句 `task`），模板就在已经取到的项目行里，不多查。不给的话
写手第一次 `score_drafts` 才知道要什么，整批带着 problems 回来再存一次（judge 也再跑一遍）。模板坏了不带这一栏。

**不变的。** 没模板的项目返回值一个字不变；发给 judge 的仍是原来的 `title` / `body`，话题、评论、方向一样都不发。模板坏了
（有人手改了库）只记日志、这次不拼，存档和打分照常；`batch_metrics.meta.delivery` 记拼了几篇、几条问题或出错原因。

**上线后要做的。** 设了模板的项目，原来那几条「交付格式」规则要 `retire`，不然简报里的文字规则和代码拼的版式会互相打架。

### 3.10 能用代码查的规则用代码查（2026-10-20）

**为什么。** 同一份诊断：规则越堆越多，全部写成文字塞进简报，守不守全凭模型的注意力。其中禁用词、某个词能不能出现在哪、必须带的
话术、字数范围这一类根本不用模型判。`memories.rule_kind` / `rule_payload` 本来就有（`000_baseline.sql` 的 CHECK 只认
forbidden_word / required_phrase / max_len / forbidden_regex / free_text），以前只有 AW 生成链路的 `validator.py`
（`generation_service` → `check_hard_rules`，只看 hard）在读，生产库 292 条规则一条没填。不加迁移。⚠️ `validator.py` 没退役，
同一批行它也读：`forbidden_regex` 的 `where` 它照认（只取 title / body 那部分；只管评论 / 话题的，它那边当没有结构化检查），
别的 kind 的新形状它读不出 `target`，退回按规则原文解析，和以前一样。

**怎么记。** `record_rule` 多一个可选参数 `check = {"kind": ..., ...}`，先校验再写（`rulecheck.normalize_check`），写错整条不落库、
回 400。`db.upsert_memory` 只在新建时写这两列，所以 check 和 `applicability` 一样单独写一次（`store.set_memory_check`），已有的
规则再记一次也能加上 / 换掉；`{"kind": "free_text"}` 撤掉。`my_rules` 每条带上它的 `check`。`required_phrase` 的词和规则原文一起过
项目隔离检查：命中会变成「正文里没有 X」回给写手，X 是别的项目的品牌名就等于叫模型往这个项目里写别家的名字；禁用词和正则不过这道
（禁掉别家的名字恰恰是常见用法）。

| kind | payload | 命中 |
|---|---|---|
| `forbidden_word` | `{"words": [...], "where": [...]}` | 出现任一词；词的开头 / 结尾是英文字母数字的，那一头按词边界认（`ad` 不算出现在 made 里，`V信` / `v+` 不算出现在「TV信号」「Apple TV+」里；中文、标点那一头不加，`www.` 照样出现在 `www.example.com` 里）。反过来 `Q群` 也不算出现在「QQ群」里、`A货` 不算出现在「AA货」「1:1A货」里，两种写法都要拦就都列上（record_rule 的说明里写了；独立复审 on aw#115 第六轮） |
| `forbidden_regex` | `{"pattern": "...", "where": [...]}` | 匹配上。**执行限时**（codex review on aw#115 第四轮）：下面两道静态闸只是尽力而为的过滤（四轮 review 每轮都找到新的漏网形状，`a*aaa*aaa*…b` 50 个 a 就 6 秒），放行 / 拒绝不再改（Jev 的 `/compile_rules` 一字不差地镜像它们）；执行一律走 `validator.search_user_regex`（第三方 `regex` 包的 `search(timeout=…)`），每条正则在一次检查里共用一份 `config.REGEX_SEARCH_TIMEOUT_SEC`（默认 0.1 秒，env 可调，夹在 0.01~1），漏过闸的慢正则最多花这一份，后面的稿子不再搜，报进 `rule_checks_unchecked`（这次没查），不算命中、也不能说守住了；所有正则规则加起来另有总上限 `config.REGEX_CHECK_TOTAL_SEC`（默认 2.5 秒，夹在 0.1~5；codex review on aw#115：不封顶的话是条数 × 每条那一份，每条调到 1 秒时 20 条就吃满 20 秒的回包目标），到点之后没查完的正则规则从那一篇起同样报没查；所以一次 `score_drafts` 最多多花约 min（正则规则条数 × 0.1 秒，2.5 秒）：到点之后 `regex` 隔一阵才看一次表，实测最多再拖两成多；这个进程第一次用到某条正则时的换写法、编译也从这一份里扣（编译打断不了，换出来的写法超过 3 万字就不编、报没查，编一次 0.1 秒以内；重复次数要展开超过 5 万遍的也不编、报没查——`regex` 编译时把最少次数展开，`x{10000000}` 十几个字就要 5 秒、2.5 GB，`re` 微秒级；Python 还不认识的新字母数字那份码位表一个进程扫一次、加锁、不扣这一份，那份写法另有 10 万字的上限，独立复审 on aw#115 第六轮）。20 条约 2.5 秒，在 20 秒的回包目标里占一成多；正常的规则一批 20 篇 × 1500 字实测几毫秒到十几毫秒（十个词的 `\b` 词表不到 1 毫秒），一份够用。`regex` 和 `re` 有几处不一样，执行前先换写法（`validator._for_regex`），结论和 `re` 一样：原生的 `\w` `\d` `\s` `\b` 字集不一样（`\w` 含 emoji 后面的 U+FE0F、不含 ①②③，`^\W` 碰上「①」两边结论相反），换成用 Unicode 属性写的字集（`\w` = `[\p{L}\p{N}_]`、`\d` = `\p{Nd}`，`\s` 写死 `str.isspace()` 那 29 个字；codex review on aw#115 第五轮：早先逐个码位列显式字集，`\w` 一个就 14 KB，普通的 `\bvx\b` 一批 20 篇就超时、真命中丢了，编译一条 `\b` 词表要几秒），`\b` 挨着英文字母数字的字面量时只看另一边；`regex` 的 Unicode 表比 Python 新，Python 还不认识的新字母数字只在正文里真有时才换一份把它们去掉的写法；`re` 不当量词的 `{`（`x{e}` 在 `regex` 里是模糊匹配，能配上 `y`）转义成字面的；`(?x)` 下全角空格等非 ASCII 空白转义成字面的；`(?i)` 下换出来的字集不按大小写扩；字集照 `re` 的解析树重写。字集里的字和取反的类别转义重叠时（`(?i)[！!\W]`）换成原子组，免得一个字落进好几个分支、指数级回溯；`(?x)` 下回引 / 八进制转义包一层组，免得 `regex` 隔着空白接着读数字（`\1 23` 读成 `\123`）。只剩几处不一样（土耳其语的 ı / İ、回引里 ſ 和 s、比 Python 新的 Unicode 才有的大小写对、组内的 `(?a:…)` 管不到 `regex` 的不分大小写、整条开了 `(?a)` 时 `(?u:…)` 里再套的组退回 ASCII）。`regex` 装不上就退回 `re`（没有时限）、记一行日志；装上了却跑不了的（换出来太长、编译不了）报没查，不退回没有时限的 `re`。静态闸：写入和读出都过 `validator.assert_regex_is_safe`，再加一道：量词只能套单个字符或定长、不含分支的组（`(哈\|哈哈)+$` 这种 validator 放行，30 个「哈」加一个句号就回溯半秒、每多一个字乘 1.6，`re` 没有超时还握着 GIL）。validator 那道自己也加了一条（codex review on aw#115）：能匹配同样字符的量词挨在一起、中间没有固定的字隔开（`a*a*…b` / `.*.*x` / `\w+\w+`）拒，30 个 a 配 8 个 `a*` 就一秒多；外层量词的每一次也算挨着（`(?:.{0,20}){5}x`）；前瞻 `(?=…)` `(?!…)` 里的量词和它前面的也算挨着（`\d+(?=\d*元)`），占有量词、原子组 `(?>…)` 也不算隔断（`.*a*+.*x`、`\d+(?>x?)\d+y`），只是自己不算开着的；后顾里套的前瞻一样；次数可变的组（`(?:…)?`、`(?:…){m,n}`）不是一整块，组里的量词和组前后的照样算挨着（`\d+(?:[,，]?\d+)?元` 2000 位 19 秒，改成 `\d+(?:[,，]\d+)?元` 就放行），组的字集照实算（`\d+(?:\.\d+)?(?=\s*元)` 放行）。两种放行：整条正则最后一个量词只当它的下限（`\d+\s*\d+`、`原价.*\d+`），「X 最多 N 次」展开写到结尾（`[!！][\s\S]*[!！][\s\S]*[!！]`、`\n\s*\n\s*\n`，最多 11 份；后面再接东西、连 `$` 都不行）。字集交不交叉交给 `re` 自己判（把单个字的节点带着组里的 `i` / `s` / `a` 旗单独编译再比，`(?i)[A-Z]` 认 `a`、`ẞ` 认 `ß`）；条件组 `(?(1)…)` 闸看不进去，一律拒 |
| `required_phrase` | `{"any": [...], "where": [...]}` | 列出的部位合起来一个都没有。那些部位全空时：标题 / 正文空着照没带报（codex review on aw#115：不然「正文里必须带 X」靠交一篇空正文就混过去了）；交付模板要求每篇都带的部位（评论 `min ≥ 1`；要写手给话题或有固定话题，`delivery.expected_parts`）空着就是没交，照没带报（codex review on aw#115：不然「评论里必须带 X」靠一条评论都不交就混过去了）；没模板、或者模板不要求的部位不查（评论常常是后面另走评论那一步写的）。AW 生成链路只看得到标题正文，只管评论 / 话题的这种它一律跳过 |
| `max_len` | `{"max": N, "min": M, "where": "body" \| "title"}` | 去掉空白和 #话题 之后的字数越界（网址里的 `#片段`、`C#` 这种不是话题，照样算字）；max / min 至少一个 |

`where` 可选 `title` / `body` / `tags` / `comments`，默认 `["title", "body"]`。`tags` 查两样：空格隔开的整行（「话题不超过 N 个」这种按个数数的正则写在它上面），两个以上时每个话题再单独查一遍（`^广告$` 这种锚定的写法按单个话题认；codex review on aw#115 第十一轮）；`comments` 一条一项。`validator.py` 的老形状（`{"target"}` /
`{"scope", "n"}`）读的时候照认；AW 生成链路读老形状的 `{"target"}` 也照这里的口径（标题正文分开、不分大小写、词边界；codex review on aw#115 第五轮，原来在「标题\n正文」里分大小写找子串，两边结论相反），只剩超过 40 字的词这里不收、那边照查。反过来 AW 生成链路（`validator.check_hard_rules`）也读这两列：标题正文照这里的口径查（查词、数字数共用
`validator.contains_term` / `count_chars`，正则逐个部位搜），只管话题 / 评论的、check 坏了的跳过，**不退回**从规则原文里抽词
（codex review on aw#115：「评论不要出现「私信」」会被抽成标题正文的禁用词）。

**怎么查。** `score_drafts` 存完档，用 `store.shared_memories(..., rule_kinds=...)` 取规则——和简报**同一个池子**（confirmed、
没静音、本项目 + 调用者自己的 global），所以 `mute` / `retire` 之后代码检查跟着停，`set_rule_state` 不用改；只是在服务端按
`rule_kind` 收窄、不取向量（每次存档多拉几百 KB 向量是白花的）。soft 的再过简报那道**方向闸**（`filter_by_applicability`，按项目名
加写手原样传回来的 `tactic` 判，同简报；codex review on aw#115）：标了另一个方向的 soft 规则简报里没有，这里也不查，不然产品向的"带上产品名"会报到流量向项目上；
hard 不过这道闸，同简报。简报的相关性筛选和每层 12 条的封顶**不做**：那两道是给注入的文字限量的，记了 check 的规则每次都查。
有交付模板时，查的是交付出去的那一份（`delivery.delivered_draft`），和 `delivery.text` 对得上：模板管话题的，话题含模板加的固定 / 条件话题，
正文是摘掉末尾那行话题之后的；不管话题的，`tags` 字段不拼、也不查，只认正文里的 `#话题`；`comments` 是 null 的，评论不拼、也不查
（codex review on aw#115：拿原稿查的话，不交付的话题 / 评论里的词照报违规，必须带的话术也能靠它们混过去）。没模板的照原稿查。
这一份由 `core._as_delivered` 给出，§3.11 的 `check_rules` 也只从这一处拿。
只查存进去了的稿子，有命中才带 `rule_checks`（每条 `index` / `rule_id` / `severity` / `where` / `what`，`what` 以「必须改」或
「建议改」开头，必须改的全列，建议改按篇号补到一共 40 条为止，多的报个数在 `rule_checks_more`）。不调模型、不出网、不拦存档；
查询失败记日志并带 `rule_checks_status = "error"`（不带的话和"查过了、没命中"一模一样）。正则规则这次没查完的（没在时限内跑完，或者限时的引擎跑不了），以及存的 check 用不了的（写坏了，或者是这里不查的老写法，比如 `max_len` 的「开头」；原来只在 batch_metrics 里记个数，回包和「查过了、没命中」一模一样，codex review on aw#115），列在
`rule_checks_unchecked`（每条 `rule_id` / `severity` / `rule`（规则原文：简报里没有 id，写手靠它对上是哪一条）/ `from_index`（从哪篇起没查完，之前的都查完了，查出来的命中照报）/ `what`；`what` 以「硬约束·没查完」/「建议·没查完」开头，不用「必须改」——那是"查出了违规"的意思），`next_step` 叫写手别说这几条守住了、也别说成违规，hard 的照原文自己核一遍；
已经搜到的命中照报。`batch_metrics.meta.rule_checks` 记能查的规则数、命中数、硬约束命中数、耗时，有没查完的再记 `timed_out`（规则 id）和 `timed_out_from`（规则 id → 从哪篇起），check 用不了的记 `skipped_ids`。

**上线后要做的。** 运营下次说到词表 / 字数类规矩时，写手会按协议带上 `check`；存量的这类规则要让写手在 `my_rules` 里挑出来、
原文一字不差地再 `record_rule` 一次并带上 `check`（或者运维照同一形状直接写那两列）。

### 3.11 写完按规则逐篇查：规则由 Jev 编译，`check_rules` 只作建议（2026-10-20，D-121）

**为什么。** 同一份诊断的另一半：§3.10 只管人手设了 `check` 的那几条（生产库一条没填），其余规则照旧是一段段文字塞进简报，
单个项目的软规则平均 400 字，守不守全凭模型注意力。要读懂内容才能判的（说法、语气、宣称、人设前后一致）交给 Jev。
owner 10-20 定了四件事（D-121）：Jev 的项目层对**所有**项目放行；规则原文可以发给 Jev 编译；结果从第一天就给写手看，但
**只作建议**——hard 没守住报「必须改」，soft 报「建议改」，永远不拦存档；软规则什么时候从简报里拿掉，等校准之后再定
（**简报这次一个字没改**）。

**编译在 Jev 那边（`POST /compile_rules`，JevforCoentent README「规则编译」）。** deskcore 仍然零 LLM。只认管理 key
（deskcore 的 `JUDGE_API_KEY` 就是）；项目号、品类按 `tv_project_map` 定，和 §3.8 的判定同一条路（`_judge_project_context`）
——没接 tv-map 就不发，数据出境口径按 TV 项目号认。一次 1–60 条 `{id, text, severity, applicability?}`，每条回一个结论：

| verdict | 编成什么 | 写作台怎么用 |
|---|---|---|
| `code` | 一份代码检查，形状和 §3.10 的 `check` 完全一样 | 交给 `rulecheck` 跑。**只跑没有人手设过能跑的 check 的规则**（`rule_kind` 是 `forbidden_word` / `forbidden_regex` / `required_phrase` / `max_len` 之一、且 `rulecheck.spec_of` 认得出 payload 的，`score_drafts` 已经按人设的那份查过）。rule_kind 对、payload 却跑不了的不算人设（10-20 第三轮复审）：AW 页存得进去、`rulecheck` 不收的那几种（`validator` 放过、量词形状那道拒掉的正则；`max_len` 的「开头」老形状）`score_drafts` 只在 batch_metrics 里记一笔 skipped，原来这里照样让编出来的 code 让路——这条规则两边都没查，`check_rules` 还说它守住了 / 是流程规则。`free_text` 不算人设（10-20 复审）：AW 记忆页存硬规则默认选「自由文本」，存进库就是 `free_text`，和没设分不开，当成「不用代码查」的话 AW 页上记的规则编出来的 code 永远不跑。只看话题 / 评论的（`where` 只有 `tags` / `comments`）不跑：版本行里只存了标题和正文，话题、评论、交付模板的固定话题都不在，查了不是误报就是空查；`where` 里混着标题 / 正文的只查那一半（那一半的命中是真的），数进 `rules.partial`，`uncovered` 写着哪几处没查，不说它整条守住了（独立复审 on aw E：原来照算 `by_code`、照说「都守住了」，「标题、正文和评论里都不许…」评论里违了也看不出来）——`required_phrase` 除外，它沾了话题 / 评论就整条不跑（「一个都没有才算没守」，看不见的那一处可能就有，只查正文会误报） |
| `questions` | 1–3 道「只看这一篇就能答」的是非题，每题带 `pass_if`（答哪个算守住）、`scope`、题号；分得开时 `code` 也一起给 | 拼成内联项目题库发 `/judge_draft` |
| `not_checkable` | 流程、交付、写作方向菜单 | 只计数 |

带 `error` 的那条不存，下次再编；`coverage` 是 `partial` 的（一条规则的要求没全装进题 / 代码，没装进去的写在 `uncovered`）照样用、照样查，但 `check_rules` 不说它整条守住了：这次真查了的（题进了题库 / 代码真跑了）里 partial 的数进 `rules.partial`，`rules.partial_rules` 列规则 id 和 `uncovered`（最多 5 条），`next_step` 说「有 N 条规则只查了一部分，不能说它们整条守住了」（10-20 第三轮复审：原来 `coverage` / `uncovered` 一处都不读，「标题不超过 20 字，正文要带某句话术」只编出了标题那一半，标题短就报「这次查的规则都守住了」）。原文本来就还在简报里。

Jev 回 200、但 `model` 是 `mock`（`JUDGE_MOCK=1` 的测试 / 本地 Jev，按关键词给假结果）的那一批**一条不存**，报 `jev_mock`（10-20 第三轮复审）：缓存键是 (`memory_id`, `sha`)，不带 model，存进去的话以后 `JUDGE_URL` 指回真的 Jev，这些假题、假代码照样一直命中，只有 `--force` 换得掉。和整批被挡同一个处理：后面几批不发、不按规则记失败，后台这个项目停 1 小时，`check_rules` 的 `uncompiled_reason` 报 `jev_mock`。

**缓存：`rule_compiles`（migrations/015）。** 一条规则的一个版本一行，键是 **（`memory_id`，`sha`）**。`sha` = 规则原文（UTF-8，
原样不去空白）的 sha256 前 12 位；`applicability` 去首尾空白后不为空时算「原文 + `\x00` + applicability」——和 Jev
`rules.rule_sha` 同一个算法，`deskcore/rulecompile.rule_sha` 是它的移植，`tests/test_rule_compile.py` 拿 Jev 对合成样例算出来的
值钉着（两边一漂，缓存永远命中不了、每次都重编白花钱；所以 Jev 回的 sha 和这边算的对不上时那一条不存、报出来）。为什么键要两样：
题号里带规则 id 的缩写，两条原文相同、id 不同的规则只按 sha 复用，会把前一条的题号带进后一条，同一份题库题号重复。原文或适用范围
一改就是新的一行；旧 sha 的行留着（改回去不再花钱），规则删了级联删。**不复用 `memories.rule_payload`**：那是人手设的 check，
写进去会盖掉人设的，也会被 `validator.py` 当成 AW 生成链路的硬检查。

**谁来编、什么时候编。** 编一批最坏约 6 分钟（Jev：2 次调用 × 2 次尝试 × 90 秒 + 退避），所以**写手的请求永远不等它**：

- `open_project`（简报的最后一步）、`record_rule`、`check_rules`（发现有没编的）各触发一次**后台补编**（`core._kick_rule_compile`）：
  每个项目同时最多一个线程（进程内锁），跑完 `RULE_COMPILE_COOLDOWN_SEC`（5 分钟）之内再触发不起新线程。`record_rule` 不等冷却
  ——刚记的规则要尽快编；遇到正在编的，让那个线程跑完再来一轮（它读池子时这条可能还没写进去），一次最多 3 轮。
  要不要编由线程自己查（按 (id, sha) 比 `rule_compiles`），触发的那一下不多查库。整个进程同时只编一个项目，别的线程排队
  （部署后第一轮各项目会同时触发；排着队编，同一个 owner 的 global 规则后一个项目读缓存时已经存下了，**这个进程里**不会编两遍）。
  ⚠️ 只管本进程：CLI 的 `compile-rules` 是另一个进程，和服务进程的后台补编不共用锁、互相看不见对方正在编什么，两边同时编同一个
  项目（或同一个 owner 的两个项目）时同一条规则会发两遍、付两遍钱（upsert 把后到的那份盖上去，数据里看不出来）。所以**后台补编开着
  的时候不要对线上跑 `compile-rules`，尤其是 `--force`**；上线补编的顺序见下面「上线要做的」。
  CLI 进程里不起后台补编（进程一退线程就被杀在半路，Jev 照样编完、花钱，结果没人存），CLI 要编就用 `compile-rules`。
  服务进程有同样的问题（Railway 重新部署——改一个变量就重新部署——时在路上的那一批丢了，新进程会把同一批再发一遍），躲不开，只能看得见
  （10-20 第三轮复审）：每发一批记一行 INFO（`compile_rules: 发一批 N 条去 Jev 编译(project=…)`）；`app` 的 lifespan 收尾时
  （`core.log_rule_compiles_cut_off`）对还有一批在路上的项目记 WARNING「后台补编被进程退出打断」，排队没发的记 INFO。不等它编完：
  一批最坏 6 分钟，远超平台给的退出时间。runbook §1.11 有怎么避开。
- 线程跑 `core.compile_rules`：池子和简报、§3.10 同一个（`store.shared_memories`：confirmed、没静音、本项目 + owner 自己的 global，
  hard 和 soft 都编、不过方向闸——global 规则跨项目共用，方向是查的时候按项目定的），已经有 (id, sha) 那一行的跳过，一批 ≤ 60 条
  （`RULE_COMPILE_BATCH`），读超时 `RULE_COMPILE_TIMEOUT_SEC`（默认 420 秒，Jev 最坏约 361 秒）。Jev 那边怎么失败都不抛，回一份报告。
- 不让失败变成花钱的循环：同一条规则的同一版编失败之后，后台 6 小时之内不再编它（进程内记账，重启清零；CLI 不受这条限制）；
  Jev 生成端的 HTTP 429 / 5xx（Jev 回「生成端调用失败：GeneratorHTTPError: HTTP 529 …」，整批失败时是 502）只停 30 分钟
  （`RULE_COMPILE_TRANSIENT_RETRY_SEC`）：上游没出输出、没计费，多半是部署后一阵过载（独立复审 on aw E：原来也扣 6 小时，一次几秒的过载让整个池子
  半天没检查）；504 / 524 网关超时、拒答、截断、输出不合格、读超时照旧 6 小时（可能已经计费）。
  整批被出境口径挡下 / 没接 tv-map / judge 不可用 / 请求形状不对时，这个项目的后台补编停 1 小时（`record_rule` 除外）——这几种是项目的事，
  **不按规则记失败**（10-20 复审：按规则记的话，owner 的 global 规则在他处方药项目里被挡一次，在他别的项目里也被扣 6 小时不编）。
  之后有一轮真编成了（`ok` / `partial`，比如 `record_rule` 不等 hold 编的那一轮），这个项目的 hold 就清掉（独立复审 on aw E：原来不清，
  Jev 恢复之后改过的规则照样被扣到 hold 到期，`check_rules` 还说「上次整批没编成(ok)」）；`check_rules` 说 hold 的原因时说起 hold 的那一轮。
- 一批**读超时**就不再发后面几批（报告里 `not_sent` / `stopped_by: timeout`），后台这个项目停 30 分钟（`RULE_COMPILE_TIMEOUT_HOLD_SEC`）：
  `/compile_rules` 没有整体截止，调用方断开之后 Jev 照样编完、照样计费，读超时过了它最坏的 ~361 秒说明 Jev 慢下来了，再发只是多付几批
  没人收的钱。Jev 恢复之后 CLI 再跑一次。Jev 回 502 之类（答了、几乎没花钱）的只算那一批，下一批照发。
  **停的是整个进程，不只是那个项目**（10-20 第二轮复审）：原来 hold 只记在超时的那个项目上，排着队等槽位的别的项目一拿到槽位就发，
  一个接一个读超时，每个 7 分钟、每批都在 Jev 那边计费。现在任何一次编译读超时都记一个进程级的停发期（`core._RULE_COMPILE_JEV`，
  同样 30 分钟）：停发期里 `_kick_rule_compile` 对哪个项目都不起线程（`record_rule` 也不例外，回 `jev_slow`），已经排着队的线程
  拿到槽位先看它、这一轮不发；`check_rules` 的 `rules.uncompiled_reason` 报 `jev_slow`。`compile_rules` 本身不看停发期——运维的 CLI
  要能在 Jev 恢复之后直接重跑。
  **连接超时不是读超时**（10-20 第三轮复审）：3 秒（`judge_client._CONNECT_TIMEOUT_CAP_SEC`）没连上，请求一个字节都没发出去，Jev 没在编、
  没计费——`judge_client.compile_rules` 把它记成 `unavailable`（详情「连接超时，请求没发出去」），按整批被挡处理：这个项目停 1 小时，
  不停整个进程、不按规则记失败、`compile-rules --all` 不就此停下。原来 `requests.ConnectTimeout` 先落进 `Timeout` 分支，报「420s 内没回」，
  一次 Jev 冷启动就让整个进程停发 30 分钟、整批规则扣 6 小时、`--all` 整个停下。写手侧的判定（§3.8）照旧把连接超时算 `timeout`。
- 编出来却写不进去：一批是一条 `INSERT … ON CONFLICT`，编的那几分钟里有规则被硬删（AW 页），外键会让整批失败——先查哪几条还在、只写还在的，
  删掉的报「规则编译期间被删」；还是写不进去才算写失败，那一批的规则按编失败记（后台不马上再付一遍钱）。
- 发不了的不发：原文超过 4000 字、`applicability` 超过 100 字（Jev 会让整批 60 条一起 422），记在报告的 `not_compilable`，原文留在简报里照旧。
- `RULE_COMPILE_AUTO=0` 关掉后台补编，只剩运维命令：

```bash
python -m deskcore.cli compile-rules --all --dry-run      # 每个项目一行: 池子几条、已编过几条、要编几条, 一条不发
python -m deskcore.cli compile-rules --all                # 真编; 已编过的不再发, 第二次跑基本秒回
python -m deskcore.cli compile-rules --project <uuid> --force   # 全部重编、覆盖(花钱; 只在 Jev 改了编译之后用)
```

`--all` 整次运行带一个 `seen`（10-20 复审）：owner 的 global 规则在他每个项目的池子里，同一条规则的同一版一次运行里最多发一次——
`--force` 也是（原来按项目数重编、重付钱），`--dry-run` 的「要编」合计就是真编要发的条数（原来按项目数重复计数）。`--dry-run` 和真编
同一个结局：没配 `JUDGE_*`、没接 tv-map 的项目照样报 ✗（原来 dry-run 在这两道之前就回了，打 ✓）。
`--all` 遇到一个项目读超时就停（10-20 第二轮复审）：后面的项目一个都不试，`⏸` 那一行列出它们，合计行写几个没试，退出码 1；读超时的那批
也记进 `seen`。原来接着发下一个项目——6 个项目就是 6 次 420 秒的读超时、6 批没人收的编译，owner 的 global 规则也每个项目各发一遍
（`seen` 原来只记写进库的）。

**查：`check_rules(project_id, version_ids, tactic?)`。** 协议里是 `score_drafts` 之后的一步（5a）：`score_drafts` 返回的
`check_rules_version_ids` 原样整串传，一次查（去重之后）前 20 个（`CHECK_RULES_MAX_VERSIONS`，一次的篇数都挤在同一个 18 秒截止里），
多出来的原样放进返回的 `remaining_version_ids`，`next_step` 让接着再调一次、传它们，直到返回里没有它。10-20 第二轮复审：`score_drafts`
一批不限篇数（22 篇的批次出现过），原来超过 20 个整次报错，协议又说「原样传、一批只调一次、报错就说规则没查成」——整批都没查。
存档那次的 `next_step` 在新稿超过 20 篇时也先说一句要分几次。每一次：

1. 归属：项目过 `assert_project_access`；版本只认本项目的（`store.versions_in_project`，和存档判定核自带 `version_id` 同一条
   `items!inner → batches!inner` 再 `.eq` project_id），别的项目的 / 不存在的 / 不是 UUID 的记进 `excluded`，不查、不发。
   查的是**交付出去的那一份**（`core._delivered_text`）：版本行存的是写手传的原稿，项目模板管话题时正文按 `delivery.delivered_post`
   摘掉末尾那行话题（和 `delivery.text` 里【正文】后面那一段一样），没有模板 / 模板不管话题时是原稿去掉首尾空白；模板坏了照原稿查。
   和 §3.10 的 `rule_checks` 走同一处（`core._as_delivered`）：标题、正文两边一字不差，和 `delivery.text` 里那一段也一字不差。话题和评论**不是**：
   版本行里没存写手单独传的 `tags`，这边拼得出来的话题只有末尾被摘走的那行 + 模板的固定 / 条件话题，写手传了 `tags` 的那篇和交付稿的话题行
   对不上（只有写手没传 `tags` 时一样）；评论版本行里没有，永远是空的（10-20 第三轮复审：原来这里写「两边一字不差」，只在写手没传 tags、
   没传评论时成立）。所以编出来的 code 里看话题 / 评论的照旧不跑（见步骤 4），拼出来的话题实际上没有检查会读。哪个 `#` 算话题只认
   `validator.topic_tag_matches`（网址里的 `#片段`、`C#入门`、`#秋招[话题]#` 后面还接着正文的都不摘，§3.9）。Jev 判的、代码查的都是它——
   拿原稿去判，只在话题行里的词会被当成「正文里有」，正文必须带的话术也能靠被摘走的那行混过去。
2. 池子：同简报，soft 过方向闸（`filter_by_applicability`，按项目名 + 写手原样传回来的 `tactic` 判，同简报和 §3.10；不传就只按项目名）；每条按 sha 找编译结果，找不到的数进
   `uncompiled_rules`（并触发后台补编），这次不查。`next_step` 说「还没有编译结果」并说清为什么（`rules.uncompiled_reason`）：
   Jev 没配 / 项目没接 tv-map（多查一次 `tv_project_map`）/ 后台补编关着 / 项目被出境口径挡下在 hold 里 / 本进程最近有编译读超时、
   在停发期里（`jev_slow`，不管超时的是不是这个项目）/ 上次编失败了
   （`rules.compile_failed` 条，6 小时内后台不重编）/ 真的在编。10-20 复审：原来一律说「还在后台编译」，编不了的项目每批都这么说。
3. 规则题：所有编出来的题拼成一份内联项目题库（`rulecompile.project_bank`，Jev `to_project_bank` 的移植：每题 `noul`、criteria 是 / 否、
   `scope` 原样；`pass_if` 为「否」的题 `evidence: on_true`——答「是」= 违反时 Jev 指出依据句，就是要改的那句）。每篇**单独**发一次
   `/judge_draft`：`banks: []`（存档那次已经判过默认三层，这里只花规则题的钱）、`project_bank_name: "desk_rules"`、
   `run_tag: "shadow-rules"`、`judge_paras: "never"`、`write: true`、`subject_id = versions.id`——答案按这一版落 TV 账本，和存档那次的
   `primary` 分开，以后拿来校准。题**不进** `hard_rules` / `brief`（进了就算进 `passed`）。最多 4 路并行，整次共用
   `JUDGE_DRAFT_TOOL_TIMEOUT_SEC`（默认 18 秒）的截止，发之前查 tv-map 的那两次库也算在里面；到点没回来的那篇报 `judge_status = timeout` +
   `judge_note`「没判完」，**绝不算守住**。正文是空的那篇不发（`judge_status: empty_body`），不算没判完，数进 `summary.empty_body`，
   `next_step` 说这几篇规则题没判、不能说守住了（独立复审 on aw E：原来什么都不数，标题上的题、「正文要带…」的题都没问，照说「都守住了」）。
4. 代码查：编出来的 `code` 交给 `rulecheck.check`（查词、数字数和 §3.10 同一份 `validator.contains_term` / `count_chars`），只跑没有
   人设能跑的 check 的规则（见上表 `code` 那行）。`where` 里的 `tags` / `comments` 先拿掉（版本行里没有），拿完一处不剩的不跑
   （`required_phrase` 沾了就不跑），数进 `rules.code_unavailable`，`next_step` 说这几条查不了、不能说守住了。
   payload 读的时候按人设 check 的同一套规矩再校一遍（正则过 `validator.assert_regex_is_safe` + 量词形状那道，§3.10）：不安全的不跑、
   记日志、数进 `rules.code_refused`，**不算进** `rules.by_code`（那是交给代码查的条数；正则没在时限内查完的算在里面，另数在
   `rules.code_timed_out`）。编不了的（原文超过 Jev 的字段上限之类，永远不发）
   数进 `rules.not_compilable`。这两种 `next_step` 都会说「有 N 条规则这次查不了，不能说它们守住了」（10-20 第二轮复审：原来
   `next_step` 只看没编译结果和话题评论两种，一条编不了的硬规则加一条正则不安全的硬规则，照说「这次查的规则都守住了」）；只有编不了的
   规则、别的都没有时，`nothing_to_check` 也照这样说，不说成「流程、交付、写作方向」那种查不了。
   写手用 `record_rule` 的 `{"kind": "free_text"}` 撤掉了代码检查的（`free_text` 加 `{"code_check": "off"}` 标记，
   `rulecompile.withdrawn`）编出来的 `code` 也不跑，规则题照判；AW 录规则界面写的 `free_text` 不带标记，不算撤掉。
   Jev 只编成了 `code`、没有规则题的那条撤掉之后就没有东西查它了：数进 `rules.code_withdrawn`，`next_step` 说这几条这次没查、
   不能说守住了（独立复审 on aw E：撤掉不改 sha，不会重编；原来这条哪个桶都不进，照说「都守住了」）。
   人设了能跑的 check 的规则编出来的 `code` 不跑，独立于 Jev 有没有编出 `code`，均数进 `rules.by_score_drafts`（`score_drafts` 存档时已经按人设的查过）。
   `nothing_to_check` 的「流程、交付、写作方向」那句只在每条编译结果都是 `not_checkable` 时说（独立复审 on aw E：原来池子不空就这么说，
   人设了 check 的、撤掉了代码检查的也被说成流程规则，还说「不用做什么」）：有人设的就说 `score_drafts` 已经按它查过、那边的「必须改」照改，
   有撤掉的就说这几条没查、不能说守住了。
   正则限时（§3.10，`validator.search_user_regex`）：没在时限内查完的那条，从停下的那篇起每篇列进 `code_unchecked`
   （`rule_id` / `severity` / `rule` 原文），数进 `summary.code_unchecked` 和 `rules.code_timed_out`；`next_step` 说没查完的那部分不是违规、
   也不能说守住了，同一篇里已经列在 `violations` 的照改（独立复审 on aw E：先在标题上命中、再在正文上超时时，同一条同一篇既是「必须改」
   又在 `code_unchecked` 里，原来只说「不是违规」，同 §3.10 `rule_checks_unchecked` 的第六轮复审）。
5. 账本：每篇的回包看 `written` / `write_error`（Jev 账本写失败照样回 200）：`write_error` 不为空、或 `written` 不是整数的，记一行 WARNING
   （带版本号）、`rules.ledger_write_errors` 计数、打一条 `deskcore_check_rules_ledger` 埋点；写手的 `next_step` 不提（判是判出来了，少的是
   以后校准用的那几行）。10-20 第三轮复审：原来只读 `detail.desk_rules`，`write=true` 要攒的影子期数据次次丢了也没人知道。

**编译结果读不出来**（`rule_compiles` 不在：015 没跑、或库从 015 之前的基线恢复）：`check_rules` 记 ERROR 日志（带项目号），回
`status: error`、`versions: []`、`reason: rule_compiles_unreadable` 和一句「这次规则没查成…不能说规则守住了；交付照常，不用重试」。
10-20 第三轮复审：原来直接抛——REST 是 500（模型读成「待会儿重试」），MCP 是工具错误，服务端 deskcore 一行日志都没有，而每次存档的
`next_step` 都让写手来调。

**答案怎么读**（`rulecompile.read_answers`，读 `detail.desk_rules[qid]`）：答案和 `pass_if` 不一样 = 没守住。Jev 标了 `ambiguous`、或
`p` < 0.7（同参考分的「有把握」线）的，不管偏向哪边都记「拿不准」——0.6 的守住和 0.6 的没守住一样不可信。`invalid_reason` 不为空的
记「没判出来」，不报违反也不报守住，**没有例外**——同 Jev README「规则编译 → 送回 /judge_draft」的契约。`evidence_not_found`
（答了「是」却指不出依据句，TV 账本里那一行 answer 为 NULL）也是没判出来（10-20 第二轮复审：原来它落在 `pass_if`「否」的题上时照算违反，
硬规则报「必须改」，写手照着一个账本里记成无效的答案去改稿，还不知道改哪一句）。没判出来的题数进那篇的 `counts.unjudged`（`unjudged` 列出规则 id
和原因，最多 5 条）、全部篇加起来进 `summary.unjudged`（整篇所有题都未判出来的也计入），`next_step` 说「另有 N 道规则题这次没判出来，不能说守住了」；一道都没判出来的
那篇（回包里没有 `desk_rules`，或每题都带 `invalid_reason`）记 `judge_status: not_judged`，和没判完一样算进 `summary.not_finished`。
10-20 复审：原来这里只在那篇上记个数，`summary` 不看它，`next_step` 照说「这次查的规则都守住了」——现在这句只在必须改 / 建议改 /
拿不准 / 没判完 / 没判出来 / 没编译结果 / 话题评论查不了 / 编不了 / 编出来的检查没跑 / 只查了一部分 / 代码没查完 / 撤掉了代码检查又没有题 /
正文是空的全是 0 时才说。

**返回值**（一篇一项 `versions[]`）：`violations`（每条 `rule_id` / `severity` / `what`（以「必须改」或「建议改」开头）/ `source`（jev | code）/
`p` / `evidence`，必须改的排前面、全列，建议改补到每篇一共 12 条为止，同 §3.10 的 `rule_checks`）、`unsure`（「拿不准」，最多 5 条）、`more`（这篇没列出来的条数）、`counts`、`judge_status` / `judge_note`、`code_unchecked`；
外加 `summary`（必须改 / 建议改 / 拿不准 / 没判完各几处、没判出来几道题；有才带：`more` 没列出来几条、`empty_body` 几篇正文是空的、`code_unchecked` 几处没查完）、`rules`（池子几条、Jev 查几条、代码查几条、查不了几条、
还没编几条和为什么、话题评论查不了几条、编不了几条、编出来没跑几条、只查了一部分几条、人设过 check 由 `score_drafts` 查的几条、撤掉了代码检查又没有题的几条、正则没查完几条）、`uncompiled_rules`、这次没查到的 `remaining_version_ids`（有才带）
和一句 `next_step`。`summary.more` 不是 0 时 `next_step` 说还有这么多条建议改 / 拿不准没列出来、如实告诉用户（独立复审 on aw E：原来
只在每篇的 `more` 里，`next_step` 和工具说明一字不提，写手照列出来的说完就算了；同 §3.10 的 `rule_checks_more`）。没有可查的规则时回
`status: nothing_to_check` 和一句话，什么都不发。

**只查新稿。** `score_drafts` 的返回多了 `check_rules_version_ids`：这批里的新稿，不含带 `replaces_version_id` 改过重存的；全是重存时
它是空的，`next_step` 说「不用再调 check_rules」。10-20 复审：原来每次存档的 `next_step` 都说「然后调一次 check_rules」，照
「必须改」改完重存那次也说——和协议 5a、`check_rules` 自己的「改一轮就够，不用再调」打架，最新的那句赢，多花一轮 Jev 的钱，
还会改 → 查 → 改地转下去。这批传了 `tactic` 的，`next_step` 再提醒一句把同一个 `tactic` 带给 `check_rules`（方向闸同一个判据）。
底稿没建成的新稿（`identity_warning`）没有版本行，不在 `check_rules_version_ids` 里：`next_step` 说这几篇 `check_rules` 查不了、
不能说守住了规则；「都是改过重存的，不用再调」只在没有这种新稿时说（独立复审 on aw E：原来 `check_rules_version_ids` 一空就这么说，
两篇新稿底稿都没建成时写手被告知它们是改稿、不用查）。

**不变的。** 简报、`score_drafts` 的参考分和 `rule_checks`、存档那次的判定都一个字没改；`check_rules` 不改库里的稿子，判不成也只是那篇
「没判完」。写手侧唯一多出来的动作是协议 5a 那一步。

**上线要做的。** ① 跑 `015`（先于部署）；② 部署，deskcore 服务上先设 `RULE_COMPILE_AUTO=0`；③ `compile-rules --all --dry-run` 看数，
再 `compile-rules --all` 跑完（runbook §1.11 有查 `rule_compiles` 的 SQL）；④ 去掉 `RULE_COMPILE_AUTO`（Railway 改变量就重新部署，不用发版），
后台补编打开时规则都已经在缓存里；⑤ 写手重新导入一次 skill（协议版本变了）。之后新记的 / 改过的规则由后台自己补编。为什么 ② 要先关：
CLI 和服务进程不共用锁（见上），补编那一阵写手一 `open_project` 就会在服务端编同一批规则，付两遍钱。

## 4. 接入

### 4.1 部署

独立 service（跟 `worker.py` 同级），config 指 `deskcore/railway.json`。

env：

| 变量 | 必需 | 说明 |
|---|---|---|
| `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` | ✅ | service_role，绕 RLS |
| `DESKCORE_KEYS` | 生产必需 | `{"k-xxx": {"user_id": "<uuid>", "name": "Ziao"}}`，一人一把 |
| `GOOGLE_API_KEY` | 强烈建议 | embedding。不设则查重降级为纯确定性 |
| `LIBRARIAN_URL` / `LIBRARIAN_API_KEY` | 可选 | 借爆款经验卡；不设则 `open_project` 的 `lessons` 与 `borrow_lessons` 都返回空，`status` 为 `not_configured`，服务日志记 WARN。馆员回 200 但 `status` 是 `degraded` / `error`（TV 侧选卡失败）记 `error` 不记 `empty` |
| `OPEN_PROJECT_BORROW_SEC` | 可选 | 默认 `10`，夹在 1–20。`open_project` 随简报借卡**只等这么久**，到点先交简报（`lessons_status.status=timeout`，`detail` 里告诉模型稍后调 `borrow_lessons` 命中缓存即回）；借阅线程继续跑完，TV 那边照样写缓存。MCP 客户端只容忍 ~22 秒，超过的是整份简报连 P0 一起丢。`borrow_lessons` 工具本身仍等满 `LIBRARIAN_TIMEOUT_SEC`（默认 60） |
| `JUDGE_URL` / `JUDGE_API_KEY` | 可选 | 入库判定（§3.8，影子期）。key 发在 `X-Judge-Key` 头里；它是自定义格式，登记在 `logger_utils` 的值级脱敏名单里（judge 的错误体会被抄进 detail / 遥测 / `batch_metrics`）。不设则 `commit_drafts` 照常，返回里每篇 `judge_status=not_configured`，一个请求都不发、一次库都不多查。⚠️ 设了也要项目接上 `tv_project_map` 才会发（出境口径要 TV 项目号） |
| `JUDGE_TIMEOUT_SEC` | 可选 | 默认 `8`，夹在 1–15（写坏了、`nan` / `inf` 都退回默认）。**整批判定的共同截止**，不是每篇各 8 秒；入库慢时实际截止更短（整次 commit 回包目标 20 秒，见 §3.8，J-08） |
| `JUDGE_MAX_WORKERS` | 可选 | 默认 `8`，夹在 1–16（同上，写坏了退回默认）。一次 commit 同时发几篇 |
| `RULE_COMPILE_TIMEOUT_SEC` | 可选 | 默认 `420`，夹在 60–900。规则编译（§3.11）一次请求的读超时；Jev 最坏约 361 秒，所以至少 400。只有后台线程和 CLI 用它 |
| `RULE_COMPILE_BATCH` | 可选 | 默认 `60`，夹在 1–60（Jev 的上限）。一次编几条 |
| `RULE_COMPILE_AUTO` | 可选 | 默认开。`0` 关掉 `open_project` / `record_rule` / `check_rules` 触发的后台补编，只剩 `cli compile-rules`。上线补编（`compile-rules --all`）那一阵要先设 `0`，跑完再去掉：CLI 和服务进程的后台补编不共用锁，同时编会付两遍钱（§3.11） |
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
`008_embedding_model_isolation.sql` / `009_tv_links.sql` / `010_tv_links_rls.sql` /
`011_angle_outcomes_view.sql` / `012_review_via_agent_provenance.sql` / `013_tv_edits.sql` / `014_tv_edit_diff_rev.sql` /
`015_rule_compiles.sql`
**十五个，按编号顺序跑，别跳号**（建议先在
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
- `009_tv_links.sql` 是写作台 ↔ TV 的稿子对照（2026-09-17）：两张表 + 两个跨 schema 的 RPC。为什么要它：TV 5966 条笔记里带写作台 lineage 的是 **0 条**——原设计让运营把 `export_drafts` 的六个 ID 列手抄进飞书，三周零匹配，到 TV 手里的表根本没有那六列。两边在同一个库里、内容都是写作台产的，`tv-sync` 按内容对（正文前 40 字 / 标题 / 时间窗内四字串包含度），对不上的直接从 TV 的全文补录进指纹库。**运营不用做任何事，飞书表不用加列。** 见 §3.7。

- `011_angle_outcomes_view.sql`：视图 `v_angle_outcomes`，发牌台账用掉的坐标 → 那一版 → TV 笔记 → `tier`（§3.8 末尾）。**要用底表属主（Supabase 上就是 SQL Editor / `apply_migration` 的默认身份 `postgres`）跑**：视图以属主身份读三张 RLS-on 无 policy 的底表，换个普通角色建出来会永远 0 行且不报错，所以迁移先验当前角色、不够格当场报错。只 GRANT 给 `service_role`。库里没有 `truth_vault.notes` 时它只打 NOTICE 跳过，TV 落库后重跑。今天没有代码读它，缺了不坏任何功能；`doctor` 探得到。

- `015_rule_compiles.sql`（2026-10-20，D-121）：`rule_compiles`，规则编译结果的缓存，键 (`memory_id`, `sha`)（§3.11）。只授 `service_role`、开 RLS。**可降级**：不跑的话 `compile-rules` 和后台补编在读缓存这一步就报 error（记 WARN，一条都不发给 Jev，不花编译的钱），`check_rules` 回 `status: error`、记 ERROR 日志（写手那边是这次规则没查成、不能说守住了，存档已成功、交付不受影响；不是 500，10-20 第三轮复审：原来直接抛，REST 是 500、MCP 是工具错误，服务端一行日志都没有），简报和存档不受影响。`doctor` 探这张表。
- `014_tv_edit_diff_rev.sql`（2026-10-20）：`tv_edit_diffs.diff_rev`，差异的口径版本。首晚 125 条"改过"只差稿子末尾的 #话题 行（TV 正文不带），`edit_diff` 改成两边先去话题、品牌只数正文；旧口径的行 tv-sync 重算（不推 `computed_at`），`edit_digest` 只读当前口径。以后改差异算法只需 `tvlink.DIFF_REV` +1。`DIFF_REV = 2`（10-20）：写作台那版晚于发布的（`lag_days < 0`，抄回来的）不算差异，同 desk_gap / 判据 C 的"接住"口径。
- `013_tv_edits.sql`（2026-10-19，D-120 / W-23）：`tv_note_links.match_kind` 多认 `edited`（包含度 0.30~0.60 且领先第二名 ≥0.15，人改得多但还是这一篇）；建 `tv_edit_diffs`（每条对上的笔记一行：标题改没改、删 / 加了哪些句子、长度、品牌提了几次）和 `tv_edit_digest_acks`（每个项目的每周摘要看到哪儿了）；`v_angle_outcomes` 把 `edited` 也算作对上。**硬失败只打 tv-sync 写 `edited` 那一块**（旧 CHECK 拒）；差异和摘要缺表时只记 WARN。`doctor` 探两张表。
- `012_review_via_agent_provenance.sql`（2026-10-09，审计 A-01，§3.6）：`items` 加 `decision_note`（用户原话，≤ 200 字）/ `decided_within_s`（距被审版本创建的秒数）两列，`decision_source` 的 CHECK 多认 `human_via_agent`，另建一条只盖它的部分索引。**只打 `review_drafts`**：它无条件写 `human_via_agent` + 两列，缺了每条都报 `failed`（`db.update_item_status` 会翻译成"跑 012"）；Streamlit 的通过 / 打回仍写 `human` + 三列，并把两列清空（每次决定整行重写，不许顶着上一次代理决定的原话）——所以 012 和 006 一样是硬前提。`doctor` 探两列（CHECK 从 PostgREST 探不到，用列当代理，两者同一事务）。

- `010_tv_links_rls.sql`：`009` 建的三张表补开 RLS（2026-09-18，Supabase advisor 对着生产库报出来的）。不是漏洞——anon / authenticated 对它们没有表级 GRANT，service_role 绕 RLS——只是与本 schema 其他表同一口径。`doctor` 探不到它（RLS 开没开从 PostgREST 读起来一样），报 `unprobeable` 并给出要在 SQL Editor 跑的那句；`tests/sql_parity_check.py` 守「每张表都开了 RLS」这条不变量。

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

WorkBuddy 里在「设置 → MCP」加，**全局生效**：随便开一个新对话都能调到写作台（owner 2026-10-19 确认）。配置形状：

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

skill 从本仓直接装：`TangVarie/autowriter → skills/bywood-writing-desk`（WorkBuddy 支持从 GitHub 装）。**协议正文就在那份 SKILL.md 里**（由 `deskcore/protocol.md` 经 `python -m deskcore.cli sync-skill` 生成）。改了协议要让运营重新导入一次；忘了的话，模型开场核版本时会拿到新正文并提醒他。

> **正文为什么在 skill 里，而不是每次从服务端取（2026-09-17）。** 09-10 曾反过来：SKILL.md 只留一根引线，正文由 `get_protocol` 每次下发，动机是"从 GitHub 装的 skill 是一次性拷贝，改了没人提醒"。09-11 起定稿入库率从 110% 掉到 22%（角度台账 vs 真入库的稿子）：34 KB 的正文作为对话开头的一次工具返回值，在模型写完十几篇稿子之后已经离得太远或被平台压缩掉，它不记得还要查重和入库，或者记得但找不到工具名——「找不到规则操作工具」就是那个状态的原话。写出去的稿子没有指纹，下一批再撞上它们时查重看不见；运营看到的"大量重复"就是这么来的。技能文件进系统提示，整场对话都在眼前，没有这个问题。
>
> "改了没人知道"改由版本校验解决：SKILL.md 里有一行 `<!-- protocol_version: … -->`，模型开场把它传给 `get_protocol`；一致只回版本（几十字节），不一致才下发正文并让模型提醒运营重新导入。MCP initialize 的 `instructions` 说的也是"先核版本"，不再是"先取全文"。
>
> `get_protocol` 报错说明服务端的协议文件坏了：模型按本地 skill 那份（同一份正文）继续，但要知道版本没核上；工具层故意不包 `_safe`，包成带 `error` 的"成功"会让它以为核过了。
>
> **让写作台在各种说法下都被调起来（2026-10-19）。** 操作的人不懂架构，不会说「用写作台」；10-19 查到同一位写手的写作台是连着的，写途鸽的那批对话却一次都没调到它。owner 确认 WorkBuddy 的写作台连接是**全局**的（随便开个新对话都能列出项目），那一波是 **skill 没被调起来**（写手点的是 human-writing），后面整套流程就没走。所以「被调起来」不能靠人记，三层各兜一层：
> ① **skill 的 `description`**（平台据它决定要不要加载 skill，常驻上下文）：写成「只要产出给品牌发的小红书内容就用」，列出只报品牌名 / 只贴 brief / 只点 human-writing 这些最容易漏的说法，并点了现有品牌名；
> ② **服务端**：MCP `instructions` 和 `open_project` / `list_projects` 的说明里写着「写稿先开项目，没提写作台也一样」——只要连着写作台，skill 没被加载也在模型眼前，部署即生效、不用重新导入；
> ③ **事后提醒**：`open_project` / `list_projects` 回包里的 `desk_gap`——你名下哪个项目近 7 天发出去的帖写作台没接住（口径同 TV 角度台账灯判据 C：按发布时间、对上只认写作台那版早于发布的、项目用过写作台就算），原话告诉运营下次在连着写作台的对话里写、先说「打开某某」。漏接的那些对话自己连不到写作台，只能在下一次连着的对话里说。
> 新品牌上线时记得把品牌名加进 `description`（改的是 skill 头，`sync-skill` 后版本号会变）。owner 10-19 说 WorkBuddy 现在会自动更新从仓库装的 skill；没更新到的那份，开场 `get_protocol` 核版本时模型会提醒运营重新导入——两条路都在，但描述里的新品牌名只有更新到了才生效，上新品牌时顺口跟运营确认一句。
>
> **"按本地那份继续"只在 `open_project` 通的时候成立（2026-10-19）。** 正文进了 skill 之后多了一种失效：写作台的 MCP 没连上（地址 / key 不对），模型照样拿到整份协议，原来那句「核不上版本就按本地这份继续、不要停下来不写」让它把整套流程走完，稿子写得和真用了写作台一模一样，服务端却一条记录都没有。（途鸽 9-16 之后写作台没收到任何调用，最初怀疑是这一种；后来确认连接是全局的、原因是 skill 没被调起来，见上一段。这条硬闸照样留着，堵的是真断线的那种。）现在 skill 头和协议「一、动笔前」第 1 条都写死：`open_project`（写评论时是 `comments_for`）找不到、或者重试一次还失败，就不写，原话告诉用户去查连接。判据是真去调，不是看眼前的工具列表——按需加载工具的客户端里，连上了列表里也可能一开始看不到它。服务端这边由 TV 的角度台账那盏灯补一种形状（项目在发帖、写作台却没接住），见 TV `scripts/check_angle_ledger_leak.py`。

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

**1. `AW_DISABLE_ST_CACHE`（R-042）。** deskcore 是 headless 进程，但 streamlit 在同一份 requirements 里装着，`db.py` 的缓存 shim 会走真 `st.cache_data` —— 跨进程缓存无法被 app 的 `.clear()` 失效，用户在 UI 改完记忆后 deskcore 会拿 30-60s 的旧数据。`deskcore/__init__.py` 已经在 import db 之前 setdefault 这个 env（同 `worker.py 顶部`）。**别在 `__init__` 之前 import db。**

**2. pgvector 反序列化（R-034）。** PostgREST 对 `vector(768)` 列的 JSON 序列化是**字符串** `"[0.1,...]"`，不是数组。不归一的话 `dedup.cosine_similarity` 因长度不等**静默返回 0.0** —— 查重变哑弹，一条都抓不到，而且不报错。所有读 embedding 的地方必须过 `db._parse_pgvector`（`store.py` 已经在读取边界统一处理）。

**3. 鉴权配坏了必须 fail closed。** `DESKCORE_KEYS` 的 JSON 写错时，早期实现会返回空 map → `resolve()` 判定为"没配鉴权" → **放行所有请求**。生产上一个逗号写错就等于把项目数据和全部写工具匿名开放。现在显式配了就必须当成"打算开鉴权"，解析失败一律 401，`/health` 的 `auth.ok` 会是 false。

**3b. "没配鉴权"同样 fail closed（2026-08-23 审计 ROB-003）。** 上面那条只堵了"配了但写错"，"根本没配"当时仍然走 dev 模式放行——而 deskcore 持 service_role 绕 RLS，漏配一个环境变量就等于把全部租户的数据和全部工具（含写）开放到公网。更糟的是**健康检查看不见**：`/health` 虽然会把 `auth.ok` 报成 false，但它返回的是 HTTP 200，而 Railway 的 healthcheck 只看状态码——一个彻底敞开的部署照样判定健康、照样上线。

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

**10-21 审查补充。** `set_rule_state` 把项目规则升成生效或修改生效规则的方向后，立即触发后台补编并绕过冷却。排队的判定任务在开始请求时重算整次剩余时间，截止后不再发送。缓存中形状不对、题号重复或缺少题目列表的规则列入 `rules.questions_unchecked` / `rules.questions_unchecked_rules`；幸存题目即使都通过，也不能声称整条规则守住。`doctor` 对规则缓存表的权限缺失指向迁移 015。CI 在安装全部接口依赖后重跑整个测试目录。
