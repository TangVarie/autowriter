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
| REST/MCP 层 | `PermissionError` → **403**（不是 401，也不是 500）。401 = key 那一层没过；403 = key 过了但项目不是你的；500 = 服务端真的坏了。**500 / MCP 的真故障只回 `<异常类型> (ref <8 位>)`**（外部评审 11-04 X1a，2026-11-04）：异常原文（PostgREST 的 `Failing row contains (…)` 就是整行稿子）不回给调用方、也不进日志，服务端那行日志记类型、错误码、堆栈帧和同一个 ref，拿 ref 对日志；400 / 403 / 404 / 409 那几类正常拒绝的消息照旧原样回。core 里给调用方看的参数提示是普通 `ValueError`（`set_rule_state` 的 action / 天数 / direction、`save_my_style` 的空笔记、`record_edit` 两边都空、`label_example` 的 label、`create_project` 的空名字），在 tools 那层收成 `InvalidInput`（400，消息原样回），不然模型只看到 `ValueError (ref …)`、不知道该改哪个参数（复审 11-04）。根 logger 的 handler 启动时装上 `logger_utils.SecretMaskingFormatter`（key / JWT 形状的串打码） |
| CLI 的 `projects` / `open` / `draw` / `check` | 走同一个 `core` 函数，所以 `--user` 从可选变成**必填** |
| CLI 的 `backfill` / `reembed` / `delivery-format`（2026-10-20）/ `project-archive`（2026-10-30） | **刻意不校验**——它们是运维命令，跑它们的人手里握着 service_role key（等价于直连库），加校验挡不住任何人，只会挡住"帮同事补一下指纹"，还会给人"运维路径也隔离了"的错觉 |

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
| 开项目 | `create_project` | 给新品牌/新方向建项目，**只在用户要开一个新方向时调**——找不到要写的项目不建，叫写手找负责人（2026-10-30，协议「零」）。**owner 恒为调用者**，签名里没有 owner 参数。同名不建、返回已有的；同品牌照建但列出兄弟项目（见 §3.0） |
| 写稿前 | `list_projects` | **我名下的**项目清单 + 各自的规则数/指纹数（按 `owner_id` 过滤，见 §2.2.1）。2026-10-30 起归档的项目默认不列，`include_archived=true` 才列（见 §3.13） |
| | `open_project` | **一次拿全**写作简报：stable / p0 / p1 / tactics；另带最近发出去 / 写过的标题 `recent_titles`，以及最近的稿子在哪几处都写成一个样子 `recent_structure`（都是 2026-10-20，见 §3.1） |
| | `draw_angles` | 发牌：n 组互不重复、避开台账的坐标，带可直接贴的 `prompt_block`（2026-10-30 起每篇那行带完整的 `angle_key`：「第1篇 [angle_key=…]」，模型照着贴进 `check_drafts` / `score_drafts`，不再自己编） |
| | `borrow_lessons` | 转调 TV 馆员，**再**借一批真实爆款经验卡（`open_project` 已随简报借过一次，见 `lessons` / `lessons_status`）。最多等 `BORROW_LESSONS_WAIT_SEC`（默认 18 秒），`open_project` 起的那次选卡还在跑就等它，不再发第二次（§3.26） |
| 写稿后 | `check_drafts` | **硬闸**：全量历史 + 本批内互比。改的是已经存过的那篇时带 `replaces_version_id`（2026-10-30，同 `score_drafts` 的字段、同一套核对 `_lookup_replacements`）：旧 item 全部版本的指纹不算它的对手，别的稿子照常比；**仍然只读**，不摘指纹。库里那一路（`deskcore_check_drafts`）每路只回最像的一条的标题、排除不了哪几行，不加迁移的做法是在 Python 里补（`_without_own_versions`）：先拿旧版那几行逐路比，库里回的命中比得过它们就是别的稿子、照用；比不过的那一路在去掉旧版之后的历史上重比——开头按哈希点查，标题语义 / 四字串把项目指纹拉回来再比（只拉要比的列，只给改稿那几篇）。被旧版挡在后面的别的稿子照样拦。id 对不上判 reject、`gate="replace_lookup"`。原来不认这个字段，改稿一查就被自己的旧版拦下（「正文开头与历史稿完全一致」），`archive_pending` 那句才叫模型改稿时跳过查重 |
| | `judge_draft` / `repair_plan_for` | **写后参考判定（可选，影子期不拦）**：deskcore 用服务端的管理 key 转发到 judge 的 `/judge_draft`，`write` / `return_rows` 钉死 false；项目号、品类按 `tv_project_map` 定（与 §3.8 的影子判定同一条路）。写手只有 deskcore 这一个 MCP，不再在自己机器上起 judge 仓的 `judge.mcp_server`（2026-10-09） |
| | `list_banks` | judge 上的题库清单（每家族最新版；暗题只报个数） |
| | `score_drafts`（旧名 `commit_drafts`，同一个函数） | 存档 + 参考分：写指纹 + 建身份（batch/item/version）+ 给坐标销账。**交付的同一轮里调，不等用户说定稿**（2026-09-18 起；此前等人开口那道门漏掉了 91%）。入库是「待审」，不代表定稿；哪些真的发了由飞书 → TV 的 `tv-sync` 按内容对照回来。改稿带 `replaces_version_id`：先摘旧版指纹再过闸，成功后同一个 item 升一版（`replaced`），被拒或异常把指纹原样放回。进程在这几秒里被杀（SIGKILL / OOM / 重启）放不回：item 的 best 还是旧版、旧版一行指纹都没有——每晚的 `tv-sync` 顺手查一遍接着的写作台项目里写作台自己的（`ai_engine = deskcore`）定稿版本有没有指纹行，没有的拿项目写锁按 `backfill` 那一套补回、打 WARNING、报 `fingerprint_gap`（外部评审 11-04 F05，2026-11-04；补回的行没有 `angle_key`）。同一个 item 别的版本还有指纹行的不补、只数 `skipped`：那是改稿的新指纹入了库、挂到旧 item 上失败（`replace_warning`），旧版的行是故意撤的，补回去新旧两版就都在查重里（复审 11-04）；新版连 versions 行都没建成的认不出来，照补。改成一个事务（RPC 里删旧写新）是后续项，这次没动 `score_drafts` 的顺序。摘指纹与原子写入不在一个事务里，所以 commit 全程持项目写锁（与 `ingest_published` 同一把，`009` 的 `ingest_locks`）：同一项目的入库与补录排队，等超时报 409 重试即可。锁放掉之后把真的建成了 versions 行的稿子发给 judge（影子期只记不拦，返回里的 `judge`，另带 `elapsed_ms` / `phase_ms`，见 §3.8）。2026-10-20 起项目有交付模板时带拼好的 `delivery`（§3.9），带 check 的规则有命中时带 `rule_checks`（§3.10）。2026-10-27 起带了 key 却对不上台账的另数（`unknown_angle_keys` / `used_angle_keys`，按形状分编的、别的项目的牌、台账没写进去），传回的飞轮臂对不上服务端发的时以服务端为准、重启过核对不了的另标（§3.12）。2026-11-10 起同一批里的重复说法和正文里混进来的版式残留带 `draft_checks`、开头一样的提示带 `batch_hints`（§3.21） |
| | `check_rules` | **存档之后按规则逐篇查，只作建议**（2026-10-20，D-121）：规则事先由 Jev 编成是非题 / 代码检查、缓存在 `rule_compiles`；hard 没守住报「必须改」，soft 报「建议改」，Jev 拿不准的每篇只报条数、偏向没守住的最多列 3 条规则（`unsure_rules`），到点没判完的报「没判完」。只问评论 / 话题的规则题不发。不拦存档、不改稿（见 §3.11） |
| （已撤） | `review_drafts` / `export_drafts` | 2026-10-19 撤掉（D-120）：写手没有人审、导出、存档这几步，定稿贴进飞书，`tv-sync` 按内容对回来。§3.4 / §3.6 留作历史 |
| 补历史 | `ingest_published` | 把**已经发出去、当时没入库**的稿子补进指纹库：建身份（出处记在 `batches.params`）+ 写指纹，**不过闸**、不销角度。运营在 WorkBuddy 里粘表即可，≤ 50 条一次；按全文幂等，重复调安全。CLI 的 `ingest --xlsx` 是同一个 core 函数的本地入口（2026-09-17） |
| 写评论 | `comments_for` / `score_comments` | 按链接（或贴来的正文）一次拿齐帖子、规则、评论位和项目定的配比 → 整组判一次、不存库（§3.14 起的各节，配比见 §3.20；哪些规则算管评论的、评论位怎么用见 §3.24；每次调用只留一行计数，见 §3.8 末尾） |
| | `set_comment_mix` | **用户明说要改项目的评论配比时**改 `comment_set` 里的四个数（提品牌上限、中性下限、短评下限、短评字数），回改之前 → 改之后；列改不了；留最近 20 次的记录（2026-11-11，§3.23） |
| 反馈 | `record_rule` | 沉淀规则（团队共享），hard 进 P0；2026-10-20 起可带 `check`，存档时用代码逐篇查（§3.10）；2026-11-11 起回包带 `given_when_writing_comments`（这条写评论时给不给），开头只说评论的规则 `check` 只查评论（§3.24） |
| | `record_edit` | 喂手动精修 diff（信号 A），返回**交给调用方模型做**的蒸馏任务 |
| | `save_my_style` | 把模型蒸馏好的笔记写回 + 按 `edit_ids` 销账（`record_edit` 的第二步） |
| | `label_example` | 标正/负例 |
| | `my_style` | 查看个人风格资产；有积压时**直接带回可续做的蒸馏任务** |
| 规则台账 | `my_rules` | 列出这个项目上我能管的**全部**规则，含试用档与已停用的，每条带 `state`（见 §3.5）；带检查的那几条给的是查的时候真用的那一份（§3.24） |
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

> **"抽了没写"只数 W-38 之后的牌（2026-10-30 发布收尾）。** 简报的 `angle_debt`、存档后的 `angle_debt`、`archive_pending` 都只数 `core.ANGLE_DEBT_SINCE`（2026-10-29 08:26 UTC，W-38 上线）之后抽的牌：那之前只有 `score_drafts` 记稿子、写手又没有存档这一步（D-120），那时抽的牌大多写完发出去了，只是对不回来。简报那句 `angle_debt_note` 也改成说给模型听的——这个项目发牌分到、还没有稿子的坐标，用户这次要写的对得上就优先按它们写（查重时带上各自的 `angle_key`）；不再叫模型把数字说给用户、问「接着写完还是放弃」，不说「你抽了」（一个团队共用一个身份），不提存档。返回的键不变。
>
> **复审（同日）：刚抽的牌不算。** 一个团队共用一个身份，分不出牌是谁抽的：队友几分钟前刚抽、正在写的牌原来会出现在另一个写手新开那场的简报里，被叫「优先按它们写」。简报的 `angle_debt` 和存档后的 `angle_debt` 现在只数抽了一小时以上的（`core.ANGLE_DEBT_MIN_AGE`；抽牌到查重通常只隔几分钟，查过重的已按 W-38 的底稿排除；断掉的那场过一小时再开，它的牌照样在里面），`archive_pending` 不截（它只挂在这一场发牌、带 `angle_key` 查重的回包上，刚抽的牌正是它要提醒存档的；同日复审起不再挂 `record_rule` 和不带牌的 `check_drafts`——共用一个身份，它数到的是整个团队这一天没存的牌，协议叫模型只管这场自己用的那几张）。存档后那份也按查重时带过的 key 排除，那句话改成和简报同一个写法：说「这个项目」，对得上就优先按这些坐标写、查重时带上 key，不再叫「别再抽一批新的」，不提存档。

> **一批之内也错开（2026-10-20）。** 10-20 生产诊断：稿子篇幅短、批批长得像，飞书里大半被人重写。台账只管跨批次不重复，同一批之内原来什么都不管——洗牌后直接取前 n 个，5 篇的内容形式全不撞只有约 1/5。现在 `_spread_order` 把洗好的候选贪心重排：优先内容形式和标题句式都还没用过的，其次有一个没用过的，再次随便；同档里情绪杠杆没用过的优先；某一维 8 个值用满一轮就清空重来。所以 n=8 时 8 种内容形式、8 种标题句式各一，n=10 时同一种最多两篇。仍是随机（先后来自洗牌，同 seed 结果不变）；选择仍在 RPC 的事务里做，这里只是排序——近期用得多的项目，被避重跳过的那一位由后面的补上，偶尔会多撞一个；降级路径先去掉避重的再排，不打折。
>
> 同一天的另外两处，都在 `open_project` 的简报里：
> - **`recent_titles`**（有才出现）：本项目近 30 天发出去的标题（`tv-sync` 对到本项目上的笔记，窗口按 `truth_vault.notes.publish_time`，只取 `note_id` / `publish_time` 两列；`ingested` 取版本标题，其余优先 `tv_edit_diffs.published_title`，人可能改过）和近 14 天在写作台写过的标题（补录副本不算），每条 ≤40 字、合计 ≤20 条，不带正文。两栏各有份额：写过的留 8 条、发出去的占其余，哪栏不够空位给另一栏——不能"发出去的先占满"，活跃项目 30 天对上的笔记远超 20 条，那样昨天写了还没发的几篇（下一批最容易撞的）永远进不来。`task` 让模型新写的稿子标题别和它们撞主干词和具体说法、开头别复述它们、人设和核心场景尽量错开；**标题句式照发牌坐标写**（句式只有 8 种，近 20 条标题几乎总把它们占全，"句式也要不一样"和发牌的硬坐标没法同时做到），并写明优先级：p0 / stable 定的人设、发牌分到的坐标和切入角度、用户点名的题目和改稿都在这一栏之上，改稿不受它约束。查不到就不带这一栏（两栏各自兜底），简报照给。2026-11-11 起，发出去的标题大多不超过 20 字的项目，`task` 末尾多一句「新写的标题也写在 20 字以内」（§3.25）。
> - **`validated_patterns_usage`**（有规律才出现，`borrow_lessons` 同样带）：85% 的写手日简报都带着同样 4 条已验证规律，其中一条是"整篇围绕一个求助问题来组织"，模型把 4 条全用在每一篇上，批批同一副骨架。规律清单原样转交（馆员给的，写作台不改），旁边说清楚：每篇最多用一条、一批里按篇号轮换、有几篇一条都不用；它们是表现好过的选项，不是模板；和发牌坐标或 p0 冲突时以它们为准。轮换是 k 条规律加一个"不用"排成一圈，起点按 (项目, 写手, 北京日期) 的哈希定（`open_project` 和 `borrow_lessons` 同一天给同一句），两篇以上的批次没轮到"不用"的、最后一篇不用——固定从第 1 条起的话，每批第 1 篇都是同一条规律，3 篇一批、4 条规律时也轮不到"不用"。`draw_angles` 拿不到规律清单（不借卡），所以按篇号的轮换写在用法里，不在发牌回包里。
>
> **再加一处：`recent_structure`（有才出现，2026-10-20 第二批）。** `recent_titles` 管不到骨架。Jev 给每篇打分的稿子答过 TV 特征题库（fq-v0.1），答案在 `truth_vault.note_feature_answers`（`aw_version` × `versions.id` × `primary` × `jev:*`），回包里的 profile 原来是扔掉的。开项目时读回本项目最近 ≤30 篇打过分的稿子（近 60 天按版本自己的时间算，批次年龄和批次数量不截断候选、补录不算、一个 item 一票，版本 id 按篇摊名额；没判出答案的不占名额：最近 60 篇里有答案的凑不够 30 篇、更早的稿子或没问过的旧版本还有，就把还没找到答案的接着往下问一轮，最多 3 轮。judge 停过一阵是这样，judge 不覆盖的项目——没配 judge、`tv_project_map` 没映射（一篇都不判）、只判了一部分——也是这样，后者每次开项目都问满几轮、多几趟空请求，有界；补问那几轮失败只记 warning，第一轮找到的照用），某一处扎堆的那种答案占比 ≥ 0.6 就摆出来：`{window, concentrated: [{question, answer, share, of}], task}`（`of` 是这一题有有效答案的篇数、即 share 的分母，可能比 window 少，也要 ≥ 12 才报），让这一批每一处至少一半换个写法；p0 / stable、发牌坐标、用户点名的写法优先，改稿不受约束。少于 12 篇、不扎堆、账本读不到就不带。三步往返（批次 → 版本 → 答案；答案按主键排序翻页，Jev 换模型重判过几代、行数过了 1000 也不截；一轮是一页加一次空页，最近 60 篇凑够 30 篇有答案的就只问一轮，往下补问的每一轮再来一趟），只取答案列（不取 evidence、不取正文）。
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
| 精确 | 正文前 40 字（去标点）相同 → `body_exact`；否则标题相同 → `title_exact`。**开头一样、后面是另一篇的不算**（外部评审 11-04 F02，2026-11-04）：开头之后那段（第 40 个字起的四字串）两边都 ≥ 15 个、笔记那段只有不到 0.15 在那一版里、标题也不一样的，那一版不进开头档，内容那一路也不再连它（帖短时光开头就够 0.30）；标题一样的照旧认（人留了开头和标题、把后面重写，还是那一篇）。开头档**不**按时间窗筛（复审 11-04）：开头和后面都对得上的就是那一篇，晚发 31 天、改了标题也是（筛掉的话内容那一路也看窗口，它会被补录成副本；11-04 生产库 372 条 `body_exact` 里 4 条在窗口外：晚发 38 天的 1 条，另 3 条发布早于那一版）；时间窗只在几版分不出时用。分数记真的包含度（原来写死 1.0）。**标题一样、正文不像的不算**（外部审计 11-02 F04，2026-11-02）：笔记和那一版的正文都有 ≥ 15 个四字串、包含度却不到 0.30 的，那一版不进标题档，走下面的模糊 / 改过；内容也对不上 → `ambiguous`，那一版列进候选、标 `weak_title`。时间窗（−30 ～ +30 天）外的同标题连候选都不算（半年后拿旧标题写的另一篇 → `unmatched`）。只有标题的稿、正文太短的笔记照旧认标题 | 同标题多版按四字串包含度分，分不出再按时间窗内离发布最近的那版。F04：同一个标题、正文毫不相干的两篇以前被连上（包含度 0.0），TV 血缘、发布原文指纹、改动差异、接住率一路都认它；生产库 124 条 `title_exact` 里 2 条是这种（SPX 0.063、途鸽 0.076，写作台那版都有完整正文） |
| 模糊 | 时间窗内（发布 −30 ～ +30 天）四字串**包含度** ≥ 0.6 且与次佳差 ≥ 0.1 → `fuzzy` | 途鸽发之前改得很狠（305 字 vs 527 字），精确对不上，靠这一路 |
| 分不出 | `ambiguous`：候选写进 `tv_note_links.candidates`，**不硬猜**，报表里列给人看；人看完说「对不上」就 `tv-resolve --note … --ingest`，正文补录、对照改成 `ingested`。**只因为标题一样**才分不出的（候选全标 `weak_title`，内容那一路什么都没对上，F04 复审）不一直挂着：先当 `unmatched` 拿查重底稿对一遍（下一行），底稿认领就转正；没认领的记 `ambiguous` 等人看 `TV_WEAK_TITLE_GRACE_DAYS`（7）天——从头一次这样判出来那晚算，记在 `candidates` 里一条带 `weak_title_since` 的备注、每晚带过去——满了没人判就照常补录（没有补录目标的记 `unmatched`，同样带着日期）。等的这几天它的文字不在指纹库里（认了的代价：记在那一版名下是说它来自那一版，补录又是终态）；内容那一路分不出、另有一版只是标题一样的，照普通的分不出处理 | 两版同标题的评论稿都不是笔记正文（途鸽 2026-09-18）；不提供「指定某一版」，`match_kind` 没有 manual 一档。F04 复审：`ambiguous` 不进补录、W-40 也不记它的文字（它没有版本），W-38 只给补录名单里的帖找底稿——拿旧标题写了一篇新的、只查了重没存档的帖，真正的来源认不回来，发出去的文字查重永远看不见；而人看完能做的也只有补录 |
| 查重底稿 | 对不上写作台版本的帖，补录之前先拿 `draft_snapshots`（`018`：写手那边的 AI 走工具调 `check_drafts` 时每查一篇记一行——标题、正文、带来的 `angle_key`、查重结论、当时的飞轮臂；运维 `cli check` 试跑不记；**不进指纹库**，不挡后面的稿子；`score_drafts` 存成版本的同样文字的底稿当场记成已转正；留 60 天）再对一次（`tvlink.match_snapshot`，近 45 天、还没转正、至少有一次查重没被打回的）。比上面几步严：帖要有发布日期（还没填日期、按内容对得上底稿的先不补录，记 `unmatched` 等日期，等到底稿过了 45 天为止；同一晚它的改写版也一起等），底稿要早于帖（发布那天 − 第一次查重那天，**北京日历日**，0～30 天）；开头 / 标题和帖一样的底稿是一档（有就走这一档，分数打平时它排前面），四字串包含度 ≥ 0.30 就行，只跟同样开头 / 标题一样的底稿（和存档版本）比，分数更高、开头标题不一样的得是它的前后几稿，否则 `ambiguous`；都不一样的那一档（`fuzzy` / `edited`）要 ≥ 0.45（W-34 判「整篇换了」的线是 0.30，擦线转正的对照运营再改几句就会被挪走）；分不出时只在打平的几份是同一篇的前后几稿（互相重合 ≥ 0.6）才挑一份——帖在稿里 + 稿在帖里加起来最像的那一稿，一样像取标题 / 开头和帖一样的，再一样取最晚查的；打平的里有别的稿子、或时间窗里有存过档的版本离得差不多近，记 `ambiguous`、**不补录**，每晚重判（底稿过了 45 天就照常补录）。帖的文字已经在指纹库里（以前补录的副本）的不转正。同一晚剩下对不上的帖，再按主索引的规矩对一次今晚要转正的几版（对手里带上存过档的版本；同一篇改写给另一个号发的，明晚主索引本来也会连上）：对上的跟着这一版一起转正、帖的文字一起记指纹，分不出的不补录、明晚重判。对上了就把底稿**转成真的版本**，和存过档的一样：建 batch / item / version（`params.via = "snapshot"`，同一晚同一次查重的几篇一个 batch——一轮的帖分几晚发就分几个，`params.check_id` 一样；版本时刻 = 第一次查重，不是转正那一刻）、写指纹（不过闸，带 `angle_key`；发出去的帖和底稿不一样的，帖的文字也记一行，同一个版本）、销牌（只销写这篇的人自己在查重之前抽的那张，找不到就不销——不像存档那样退到别人的牌）、底稿行记 `promoted_at` / `version_id` / `note_id`；对照记 `body_exact` / `title_exact` / `fuzzy` / `edited`（候选里一句 W-38 备注，不钉死），同一晚算改稿差异、`--write-tv` 回填 TV。版本 id 由（项目，正文指纹）定出来（uuid5），半途断了之后只补没做完的、不再建一份：指纹按文字补（底稿那一行写了、帖那一行没写的照样补），补的那晚这个 TV 项目里没有连着它的帖（帖在接同一个写作台项目的另一个 TV 项目里）就只补底稿那一行、销牌，不记转正，等连着帖的那个 TV 项目补上帖的文字再记。转正拿项目写锁之后再看一眼底稿：`score_drafts` 刚把同样文字的稿子存了档（底稿已经记到那一版）的，这晚不转、不补录，明晚主索引对存了档的那一版。一时读不出底稿、或这晚没转成的，**不补录**（补录是终态）：记 `unmatched` + 原因、明晚再对，退出码 1；底稿表缺权限 / 缺列（配置问题）照旧补录、退出码 1；`018` 没跑：跳过这一步、照旧补录 | W-38（2026-10-29，owner）：写手没有「存档」这一步，写作台的稿只在模型调了 `score_drafts` 时才进库。10-28 途鸽一轮 10 张牌，查重查了、`score_drafts` 没调，发出去的帖只能被补录成副本（牌不销、没有改稿差异）。`check_drafts` 是每篇必过的硬闸，在那里留底稿；记底稿失败只记 WARN，不碰查重的判定。同一次把 `lag_days` 改成按北京日历日算：TV 的 `publish_time` 是北京那天的 0 点（存成前一天 16:00 UTC），按 UTC 日期减，北京当天写、当天发的记成 −1（10-29 生产库 264 条里 26 条）；存着 −1、按北京日是 0 的对照 tv-sync 当晚改回来（改稿差异的 `lag_fill`） |
| 对不上 | `unmatched` → `ingest_published`（剥话题标签、不过闸、按全文幂等）补录进目标项目，建成的 `version_id` 记回 `tv_note_links`。对不上的那一行（`unmatched` / `ingested`）的 `score` 记这篇帖和写作台的稿子最像到什么程度（2026-11-11，§3.28），只是读数，不参与判定 | 同项目稿子都以同一串标签结尾，不剥会互相误撞 |
| 帖的文字进指纹库 | 对上了写作台某一版的帖（`body_exact` / `title_exact` / `fuzzy` / `edited` / `tv_lineage`，以及补录之后正文又改过的 `ingested`（F07，见「正文换了」），按现在的正文核过的：今晚写的对照、今晚记上正文指纹的、之前核过且字没变的），发出去的文字记一行指纹在那一版下：按文字幂等，正文和标题一起算（那一版下已经有正文、标题都一样的行就不记；帖和那一版只差话题标签的算一样；**只改了标题的照样记**——查重单看标题也会判死，运营改过的标题不在库里就拦不住照着它起的标题；帖没标题的只比正文）；行的时刻、写手、`angle_key` 跟那一版最早那行走；那一版一行指纹都没有的（改稿 `replaces_version_id` 把整个 item 的指纹撤了重写、老的没指纹的版本、W-38 转了一半的），时刻取那一版自己建的时刻、写手记项目 owner，照样记——帖还在外面。拿项目写锁、锁里重读再写，按 100 行一批插（同补录）。每晚把所有这样的对照核一遍，所以第一晚会把以前对上的一起补上，之后只有新对上和改过字的。写失败不挡对照，退出码 1、下一晚再补 | W-40（2026-10-30，owner 让我定）：查重比的是写过的稿，写手拿去改过再发的那一版原来不在库里——补录的帖、W-38 转正的帖早就把发出去的文字记进来了，只有对上存档版本的漏了。10-30 生产：`title_exact` 124 条、`fuzzy` 28 条、`edited` 1 条对照，那一版下都只有一行（底稿 / 存档那行）。时刻跟着那一版走，不记成今晚：TV 的发帖对账按时间窗数稿子，记成今晚会把老稿当成新写的 |
| 回填 TV | `--write-tv` 才把对照写回 TV 的 `source_autowriter_*` 两列，**只填 NULL 的行**，且**只写真对上的**（`body_exact` / `title_exact` / `fuzzy` / `tv_lineage`）；`ingested` 不写 | 那是 TV 的列；补录的版本是从笔记复制来的，写回去因果倒置（TV 2026-09-18 核对时定的口径，见 runbook §1.4） |
| 飞书已删 | TV 的 `v_notes_vanished` 里的篇（飞书里删掉或挪去别的表、TV 那一行还留着）`deskcore_tv_notes` 不返回（`016`）：不重对、不补录、不计数，已有的对照一行不动。缺席满一天半的（视图里本项目最近一次完整同步比这篇最后被看到晚 ≥ 36 小时，`TV_VANISHED_GRACE`），按当前口径算过的改动差异降回旧口径（W-37，10-28），摘要不再读。只查这晚没读到的那几条：`--since` 窗口外、视图里没有的不降；TV 两次完整同步重叠、或某次写库写一半，活着的篇也会短暂进视图（不到一天），不降。降了之后那篇又读到了，按「口径过时」重算（那一版还在对照索引里才算得了），`computed_at` 留旧值（同一版不推），中间若确认过摘要，它不会再进摘要。视图查不了这晚不降、退出码 1；视图不在（新库，`PGRST205`）不降、不报。评论按链接找帖（`comments_for` / `score_comments`）不走这个 RPC，由 `core._note_from_link` 自己查视图：同一条链接有活的一行就不用已删那行，只剩已删那行时回包带 `tv_note_vanished`、给的项目对不上时走贴正文那条路（代码，不依赖 016） | 2026-10-26：79 篇成人藻油从 BJS_phase1 挪去新表（TV 记为 BJS_phase2），其中 14 篇是 unmatched，补录目标一打开就会被补录进儿童藻油的项目；视图不在（新库）时照旧全给 |
| 正文换了 | 有版本的对照（`ingested` 和人定过的除外；`ingested` 见本格末尾）每晚比一次 TV 正文的指纹（`tv_note_links.tv_text_fp`，`017`；16 位，前 8 位是标题、后 8 位是剥话题的正文，各自去标点后 sha1，只改标点、空格、话题不算）。指纹变了才核：只改了标题（正文那一半没变），或还是那一篇（开头 40 字相同、开头之后也沾边（F02：开头一样、后面和标题都换了的算整篇换了） / 四字串 ≥ 0.30 在原版里 / 原版只有标题而标题相同或四字串过半还在）→ 只记新指纹、改动差异按现在的正文重算（同一版不推 `computed_at`；017 之前的对照第一次记指纹时也重算一遍）。指纹等差异算好了才记，差异这晚没算成的明晚再核。**整篇换了** → 按内容重对，TV 里那条 lineage 若正是我们写回去的旧版就不当证据。挪之前先把这几条对照标成「没回填过」，再用 `deskcore_tv_clear_lineage` 撤回 TV 里我们写的那一对（两列都还等于才清），撤成了才挪，同一晚的回填写新版本。撤回失败、TV 里已是别的值、或一晚超过 `TV_SYNC_RELINK_MAX`（默认 5，按 TV 项目）的都不挪，差异先隔离（降回旧口径），退出码 1 等人看；没开 `--write-tv` 的也不挪、差异隔离，但只报（退出码 0），下次带 `--write-tv` 跑时挪。`tv_lineage` 而 TV 还指着这一版的，正文换了不挪、只报（退出码 0），TV 改了 lineage 之后再按 TV / 内容走。`--rematch` 同样按内容重对、受同一套限制；连着的那版已不是 item 的 best 时，正文还是那一篇就留着。**以前按标题连上、正文其实不像的**（`title_exact`，两边正文都够长、包含度不到 0.30，F04）指纹没变也每晚核：走上面「整篇换了」那条路（上限、先撤回 TV 里我们写的血缘、差异降回旧口径），重对多半是只有标题一样的分不出（见「分不出」那一行），原来那版只作为 `weak_title` 候选列着、不算还对回原来那版；候选前面记一条 `F04 <日期>: 原来只是标题一样…` 的备注（不钉死）。存着的包含度不到 0.30 的 `title_exact`，连着的那一版后来不是 best 了也去库里取它的正文来核（F04 复审，否则核不了、错的对照一直挂着）。**补录的帖（`ingested`）正文改了**（外部评审 11-04 F07，2026-11-04）：身份那条线不动（不重对、不回填血缘，对照和版本都不改），但 TV 正文的指纹变了就记 `drift.ingested_text_changed` 和一条样本（`why` 以 `ingested_drift` 开头，不带正文），改后的文字按「帖的文字进指纹库」那一套记一行在补录的那一版下（按文字幂等），记上了才更新 `tv_text_fp`（没记上的明晚再补）。017 之前没记过指纹的补录对照拿那一版的正文比一次：正文一样只记指纹（`drift.ingested_seeded`，上线首晚约两千条、之后为 0），不一样算改过 | W-34（2026-10-27）。D-129 那 14 条：10-20 对上儿童稿，飞书正文后来整篇换成成人帖（重叠 ≤ 0.06），对照、TV 血缘和改动差异一直挂着，10 条差异进了儿童藻油项目的改稿摘要。10-27 生产库 501 条读得到的对照里，没换过的最低 0.481，中间 0.078~0.481 一条都没有。成批的换（运营把一张表挪作他用）由人定，所以有上限 |
| 互斥 | 补录走 `core.ingest_published`，它先拿库里的项目锁（`ingest_locks`，TTL 10 分钟、到期可接管），拿不到等 90 秒后报「另一个补录正在跑」（REST 409） | 服务进程的工具与 CLI/cron 的 `tv-sync` 是两个进程，进程内锁管不到对方；advisory lock 跨不过 PostgREST 的多次请求 |
| 补标题向量 | `--all` 且不是 `--dry-run` 时，所有 TV 项目同步完（删过期底稿之后）跑一次 `core.reembed_after_sync`：有标题、没有当前模型向量的指纹行（缺向量 / 来路不明 / 换模型作废三种，`store.fingerprints_needing_vectors`，**不分项目**，不只是 `tv_project_map` 里的），缺向量的排前面，每晚最多 `TV_SYNC_REEMBED_MAX`（200）行、最多跑 `TV_SYNC_REEMBED_BUDGET_SEC`（90）秒（embedding 客户端没设请求超时，到点在块与块之间停，剩下的不算失败），没轮到的明晚接着补；CLI 报补了几行、涉及几个项目。尽力而为：embedding 调用失败或读库出错报 ⚠️、退出码 1，这晚写进去的对照一行不动；没配 embedding 只说一句、退出码不变 | 2026-10-30 复审：sportsix 有一条有标题、没向量的指纹，之后每一批查重都报 `semantic_degraded`；补它的 `reembed` 是运维命令，没有任何东西排着跑。cron 本来就带着 `GOOGLE_API_KEY`（`railway.cron.json`） |

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
>
> **2026-10-30 发布收尾：分数不再逐篇报。** 10 月存档的 234 篇里 231 篇是 100 分，「每篇多少分」写手听到的几乎全是一串 100。
> `judge.note`（`core.SCORE_NOTE`）和 `next_step` 开头那句（`core._score_report_step`，判定跑完才接在「这批已存档。」后面）
> 改成：有 `fixes` / `hard_fails` 的那几篇点出 `judge.results` 里的 index，只说这几篇要改哪一句、犯了哪条；其余整批一句
> 「合规检查都过了」；没打成分的一句带过（不看是哪种失败，回包除 `judge` 外一个字不变）。`score` / `fixes` 照旧都在 `judge.results` 里。
>
> **复审（同日）：「合规检查都过了」只在每篇都打成了分、都没有 `fixes` / `hard_fails` / `unjudged` 时说。** 只有 `unjudged`
> 的（有几道硬约束 Jev 这次没判出来）单独一句「有几道硬约束这次没判出来，不能说都过了；不影响交付，不用重试」；混着别的
> 情形时其余几篇只说「没查出要改的地方」；没打成分的从来不算过。整批没发出去时的工程原因（没配哪个变量、没接 tv-map、
> 查库卡在哪）只进 `batch_metrics.meta.judge_detail`，回包里的 `judge.detail` 是念给写手的那一句。

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
`ingest --xlsx` 补进来、而那张表没接 TV 同步的（写这段时 Hatherine 就是；现在它已接进 TV，写作台的 Hatherine 项目 9 个里 8 个映射上了，2026-11-04 核过），事后抽取也够不着——两边都判不到。
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
| 调用方自带的 `version_id` 不是本项目的版本（库里没有 / 别的项目的 / 不是 UUID） | 走不到这里（外部审计 11-02 F05，2026-11-02）：`score_drafts` 拿锁之前就按本项目核一遍（`_caller_version_problems`，同一个查询，`_source_autowriter_version_id` 也核），核不上的按 `gate="version_lookup"` 拒掉，不写指纹、不销角度、不算 `written`。以前照单全收：指纹挂在不存在的版本名下、角度销给了它，回包一切正常。判定这边照旧跳过（`version_not_in_project`），只防万一。发之前按本项目核一遍（`store.items_for_versions`，与核 `replaces_version_id` 同一个查询）：`write = true` 会按 subject_id 写账本，过期的 id 留下孤儿答案，别的项目的 id 把这篇的判定记到那个项目名下（codex review P1 on #92）。发的是库里的规范写法。归属查询本身失败的这几篇不发、记 `error`，本次新建的照发 |
| 改稿替换、新版挂上了旧 item | 判，用新版的 id |
| `identity_error`：指纹入了库、versions 没建成 | 跳过（`skipped.reason = identity_error`） |
| 替换没挂上（`replace_warning`） | 跳过（`replace_failed`） |
| 正文为空 | 走不到这里：`score_drafts` 入库前就按 `gate="empty_body"` 拒掉，不存档、不销角度（W-30，10-26：Hatherine 两批 15 篇只有标题的稿子照样存了档）。判定这边的跳过（`empty_body`）只防万一：只有标题判不出东西，平台题却可能对着一个标题给硬伤 |
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
`batch_id` 留 NULL（本次的 batch 记在 `meta.batch_id`），免得历史页给写作台批次挂上一块全是 0 的「性能指标」。
**判出来了、账本没写进去的**（外部评审 11-04 F04，2026-11-04）：Jev 写 TV 账本失败照样回 200（答案照给，`written` 是 0、
`write_error` 里说一句），原来记成 `judge_status=ok`、不计数不报警。现在 `judge_status` 仍是 `ok`（判定本身成功了，写手看到
的分数不受影响），但 `write_error`（脱敏、截到 300 字）或 `written` 不是整数的那几篇记进 `meta.judge[].ledger_error`、
`counters.judge_ledger_errors`，打一条 WARNING 和一条 `deskcore_commit_judge_ledger` 埋点（同 `check_rules` 的
`rules.ledger_write_errors`）；不进回包。AW 补不了账本（`/judge_draft` 不回账本行），要补只能重判那几篇。
**2026-10-30（W-21）起 commit 段再按步拆开**，记在 `meta.commit_parts_ms`（不进 `phase_ms`，那里各段加起来要等于 `total`；也不进回包）：
`access`（查项目）、`replace_prep`（改稿摘旧指纹）、`embed`（标题向量）、`gate`（查重闸）、`fingerprints`（原子写指纹）、
`restore`（被拒的改稿放回旧指纹）、`replace_versions`（改稿挂旧 item）、`arm`（飞轮臂）、`mint`（建 batch / item / version，
每篇三次写）、`consume`（角度销账，每篇一查一写）、`snapshots`（查重底稿转正，W-38）、`key_check`（key 对不上台账时多查一次）、
`angle_debt`（存档后查欠账，只在有没带 key / key 对不上时）、`other`（拼回包、提示语、放锁）。按这个顺序一段接一段量，
加起来等于 `phase_ms.commit`（存进 jsonb 后键会按名字重排，要按执行顺序看就按 `core.COMMIT_PARTS` 排）。
**2026-11-11 读了**：`gate` + `fingerprints` 占 commit 的六到七成，都花在查重 RPC 取候选的那一步上，`019` 换了那一步的写法（§3.27）；
owner 跑完 `019` 之后拿下面第二条查询再看一次这两步。攒够样本之后这样查：

```sql
select count(*),
       percentile_cont(0.5)  within group (order by (phase_ms->>'commit')::int) as commit_p50,
       percentile_cont(0.95) within group (order by (phase_ms->>'commit')::int) as commit_p95,
       percentile_cont(0.95) within group (order by (phase_ms->>'delivery_rules')::int) as delivery_rules_p95,
       percentile_cont(0.95) within group (order by (phase_ms->>'judge')::int)  as judge_p95,
       percentile_cont(0.95) within group (order by (phase_ms->>'total')::int)  as total_p95
  from autowriter.batch_metrics
 where meta->>'mode' = 'deskcore_commit' and created_at > now() - interval '14 days';

-- commit 段哪一步占大头(W-21, 2026-10-30 起才有 commit_parts_ms)。p50 / p95 是每批的绝对毫秒。
-- 摊到篇数用加权比(总毫秒 / 总篇数, 不是每批比值再平均: 那样 1 篇的小批和 20 篇的大批一样重), 分母看这一步跟谁走:
--   per_submitted 按交上来的篇数(counters.drafts): gate 逐篇比对, 被拒的也照算; embed 是一批一次调用, 更像固定成本
--   per_written   按存进去的篇数(counters.written): mint / consume / snapshots / replace_versions 只碰存进去的;
--                 全拒的批(written = 0)不进这一列, 否则它的整批耗时会被当成「一篇」
select e.k as step, count(*),
       percentile_cont(0.5)  within group (order by e.v::int) as p50,
       percentile_cont(0.95) within group (order by e.v::int) as p95,
       round(sum(e.v::numeric) / nullif(sum((m.counters->>'drafts')::int), 0)) as per_submitted,
       round(sum(e.v::numeric) filter (where (m.counters->>'written')::int > 0)
             / nullif(sum((m.counters->>'written')::int), 0)) as per_written
  from autowriter.batch_metrics m, jsonb_each_text(m.meta->'commit_parts_ms') as e(k, v)
 where m.meta->>'mode' = 'deskcore_commit' and m.created_at > now() - interval '14 days'
 group by 1 order by p95 desc;

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

**每次工具调用留一行，只有数（2026-11-11，`deskcore/calllog.py`）。** 原来只有 `score_drafts` 在 `batch_metrics` 里留一行。
开项目、查重、查规则各用了多久、怎么收场的，写评论那一路判了几条、哪一样检查打回了几条，库里一个数都没有，只能翻只留
一周左右的网关日志（W-33 / W-38 / W-41 / W-42 / W-51 的周检都靠它）。现在 REST 和 MCP 给工具计时的那两处
（`app.py` 的 `rest_tool` 和 `_wrap`），回包定下来之后把这一次交给后台，写一行 `autowriter.batch_metrics`：
`meta.mode = 'tool_call'`，`batch_id` 空着，**不加迁移**（`batch_id` / `project_id` 可空、`user_id` 非空，
`migrations/000_baseline.sql`，2026-11-11 在生产库 `information_schema` 核过）。

- **不改、不拖慢、不弄坏回包。** 交接只是往一个有上限的队列里放一下（最多等 200 项，满了新的直接丢）；摘数和写库都在后台
  一个线程里。写不进去只记一行 WARNING（`deskcore.calllog`，5 分钟最多一行，只带异常类型和错误码），队列满了同样。
  进程退出时最多等 2 秒把剩下的写完，写不完就算了：这是给复盘攒数的，丢几行不影响任何人。
- **只留数、固定的词和 8 位代号。** 稿子、评论、帖子、规则、回包里的任何一段文字都不进这一行（`calllog.build` 只从固定
  的位置取条数、布尔和固定的词，最后 `_clean` 再筛一遍，不是整数 / 布尔 / 认得的词的一律丢掉；
  `tests/test_tool_call_log.py` 拿现编的标记串逐行查）。评论照旧不存库，存的只是这一行计数。
- **不记的**：`get_protocol`、`/health` 这类不是工具的端点、认不出调用者的调用（`user_id` 是非空列，不编一个填进去）、
  参数没过 MCP 校验的调用（到不了计时的那一处，服务日志里有一行）、开关关着时的全部调用。存档之后在后台跑的那两样
  `check_rules`（改稿复查、规则补查，都在 §3.19）不是工具调用，也不留这一行：`tool = 'check_rules'` 的行数就是模型自己
  调了几次。
- **开关**：`DESKCORE_TOOL_CALL_LOG=0` 关掉，默认开。
- **读这张表的地方都要按 `meta.mode` 或 `batch_id` 筛。** 历史页的 `db.list_batch_metrics` 改成只读 `batch_id` 不为空的行
  （不筛的话一天几十上百行的调用记录会把批次指标挤出最近 N 条）；本文档里的查询和 TV `verify_supabase_state.sql` #104 都带
  `meta->>'mode' = 'deskcore_commit'`，不受影响。以后新写的查询照这个来。

`project_id` 那一列只在调用成了的时候填（评论工具填服务端对上的那个项目）；没成的调用带来的 project_id 可能是抄错的、别人的，
只在 `meta.project8` 留前 8 位。`phase_ms = {"total": 毫秒}`，其余都在 `meta` 里：

| 键 | 哪些调用有 | 是什么 |
|---|---|---|
| `tool` / `channel` / `elapsed_ms` | 全部 | 工具名、`rest` / `mcp`、服务端这一次用了多久 |
| `outcome` / `error_class` | 全部 / 没成的 | `ok`；`error_result`（没抛、但回的是带 `error` 的降级结果）；`refused`（400 / 403 / 404 / 409 那几类正常拒绝）；`failed`（真故障）；`interrupted`（客户端断了）。后三种带异常类型（同服务日志那一行） |
| `items` / `reply_chars` | 有才带 | 这次带了几篇稿 / 几条评论 / 几个版本（发牌是要几张）；回包的字符数（紧凑 JSON，中文一个字算一个） |
| `skill` | `open_project` / `comments_for` | 这次报上来的 skill 版本：`up_to_date` / `outdated` / `unknown`（没报） |
| `check` | `check_drafts` | `total` / `pass` / `warn` / `reject`；`decided_by` 是可疑和拒的各由哪个信号定的（`opening` / `title` / `ngram` / `contain`，一篇可以有几个）；`gates` 是不是撞车的拒（`replace_lookup` 这类）；`semantic_degraded` |
| `rules` | `check_rules` | 回包里的数（§3.11 的 2026-11-11 那一版回包）：`status`（`checked` / `nothing_to_check` / `no_versions` / `error`）；`versions`、`must_fix`、`suggest`、`unsure`、`unjudged`、`evidence_not_found`（Jev 怀疑有问题、指不出是哪一句的，算在 `unjudged` 里）、`not_finished`、`empty_body`、`code_unchecked`、`more`，都是整次的合计（同回包的 `summary`，有才带）；`flagged`（有要动手的那几篇）、`judged`（规则题判回来了的那几篇）；`unsure_rules` / `unsure_rules_hard`（最外层列出来的偏向没守住的规则几条、其中硬约束几条，最多 3）；`excluded` / `remaining`（不是这个项目的、留到下一次的各几版）；`pool` 是规则池里各几条（同回包的 `rules`，只取数：`pool` / `by_jev` / `by_code` / `uncompiled` / `partial` / `code_unavailable` / `code_withdrawn` / `not_compilable` …）。规则 id、版本 id、题面和 Jev 指出的句子都不取 |
| `comments` | `comments_for` / `score_comments` | `resolved`（帖子对上没有；没对上带 `need` = 还缺几样）、`source`（`tv` / `tv_by_text` / `pasted`）、`post8`（这篇帖子的 8 位代号：对上 TV 笔记的按笔记号、没对上的按贴来的正文算的 sha256 前 8 位，同一篇帖子的几次调用是同一个，还原不出原文）。`comments_for` 另有 `existing` / `comment_rules` / `hard_rules`（各几条）、`has_set`。`score_comments` 另有 `n`（这一组几条）、`earlier`（带了几条前面判过的）、`no_slot`（几条没带评论位）、`existing`、`judge_status`、`passed`（整组）、`passed_n`、`needs_review`、`delivery_lines`（那一格拼了几行）和 `checks` |
| `comments.checks` | `score_comments` | `style` 是写法底线每样打回几条（`period` 句号 / `dialect` 方言语气词 / `contact` 像要联系方式 / `post_ref` 提了帖子 / `vague` 含糊指代）；`brand_over`（提品牌超出几条）、`repeats`（说法或开头重复的几条）、`endings_over`（品牌收尾超出几条）、`short_by` / `neutral_by`（短评、中性还差几条；项目没定这一项、或中性没判成时没有这个键）；`fix` / `style_fix`（带这两样的各几条）、`thread_fix` / `thread_hints`（整组的各几句） |

它说得出一场走到了哪一步、每一步多久、评论判了几轮，说不出一场为什么停、是谁在打字。

**存档那一行多三小块（同日，审计 A3 / A8 / A5）。** 都只有数和固定的词，只进 `meta`、不进回包，哪一块算不出来就不带哪一块：

- `flywheel_lessons`：这个（项目，身份）近 12 小时借卡的情况。`borrows` 借了几次（0 = 没借过或进程重启过，不知道）；
  `shown` / `status` 是最近一次带出去几张经验卡、什么结局（`borrowed` / `empty` / `timeout` / `error` / `not_configured`，
  另有只在这一行出现的 `followup_timeout`：`borrow_lessons` 自己等到点卡还没选完，§3.26；回包里那次的 `status` 还是 `timeout`）；
  `shown_max` 是最多的一次几张；`statuses` 是各种结局各几次。记的是简报实际带出去的那一份（超时 = 0，后台之后选完的不算）。
  团队共用一个身份时分不出是哪一场借的：`shown_max = 0` 才能确定这一批没参照过卡。`statuses` 里同时有 `timeout` 和
  `followup_timeout` = 简报没等到、补借也没等到。
- `skill_versions`：近 24 小时报上来的 skill 版本构成（`skillcheck.health`，同 `/skill-versions`）：`current`、
  `up_to_date` / `outdated` / `unknown` 各几个、`outdated_versions`（旧版各是哪一版；认不出 12 位十六进制的并进 `other`）。
  数的是「身份 × 版本」，读作有几种版本在用，不是人数。原来只在进程内存里、要带 key 读、重新部署就清零。
- `rule_compile`：这个项目最近一次规则编译（后台补编 / CLI）的收场：`status`、`uncompiled`（还有几条没编出来；规则池读不出来时
  没有这个键）、`not_compilable`、`error_kinds`（没编出来的按种类各几条：`refusal` 拒答 / `truncated` 截断 /
  `generator_http_529` 这类生成端状态码 / `generator_timeout` / `bad_output` / `batch_timeout` 这类整批的结局 …）、
  `age_s`（这是多久之前的数）。种类按**拆小重发之后**的结局数（§3.11）：重发编出来的不算，第一次整批没成、重发了的按重发那次
  每条的结局算（原因认不出来的记 `batch_jev_failed` 这类），只有整批被挡 / 读超时没重发的那一批按 `batch_<结局>` 记条数。这一轮没发、沿用上一轮种类的另带 `error_kinds_age_s`。本进程还没编过这个项目（刚重启）就不带。

```sql
-- 每个工具调了几次、多久、多长(近 7 天)
select meta->>'tool' as tool, meta->>'channel' as channel, count(*),
       round(avg((meta->>'elapsed_ms')::int)) as avg_ms,
       percentile_cont(0.95) within group (order by (meta->>'elapsed_ms')::int) as p95_ms,
       round(avg((meta->>'reply_chars')::int)) as avg_reply_chars
  from autowriter.batch_metrics
 where meta->>'mode' = 'tool_call' and created_at > now() - interval '7 days'
 group by 1, 2 order by 3 desc;

-- 没成的调用: 哪个工具、哪种结局、哪类异常
select meta->>'tool' as tool, meta->>'outcome' as outcome, meta->>'error_class' as error_class, count(*)
  from autowriter.batch_metrics
 where meta->>'mode' = 'tool_call' and meta->>'outcome' <> 'ok' and created_at > now() - interval '7 days'
 group by 1, 2, 3 order by 4 desc;

-- 一场走到了哪一步: 每个项目每天各工具调了几次(发了牌、查了重却没有 score_drafts 的那几天一眼看得出)
select day, p, jsonb_object_agg(tool, n order by tool) as calls
  from (select (created_at at time zone 'Asia/Shanghai')::date as day,
               coalesce(left(project_id::text, 8), meta->>'project8') as p,
               meta->>'tool' as tool, count(*) as n
          from autowriter.batch_metrics
         where meta->>'mode' = 'tool_call' and created_at > now() - interval '7 days'
         group by 1, 2, 3) t
 group by 1, 2 order by 1 desc, 2;

-- 查重: 可疑和拒的是哪个信号定的
select s.status, sig.key as signal, sum(sig.value::int) as drafts
  from autowriter.batch_metrics m,
       jsonb_each(coalesce(m.meta->'check'->'decided_by', '{}'::jsonb)) as s(status, signals),
       jsonb_each_text(s.signals) as sig
 where m.meta->>'mode' = 'tool_call' and m.meta->>'tool' = 'check_drafts'
   and m.created_at > now() - interval '14 days'
 group by 1, 2 order by 3 desc;

-- 查规则: 模型调了几次、查了几版、必须改 / 建议改 / 拿不准各几条、几次带了 unsure_rules、几次有规则查不了
select count(*) as calls, meta->'rules'->>'status' as status,
       sum((meta->'rules'->>'versions')::int) as versions,
       sum((meta->'rules'->>'must_fix')::int) as must_fix, sum((meta->'rules'->>'suggest')::int) as suggest,
       sum((meta->'rules'->>'unsure')::int) as unsure, sum((meta->'rules'->>'evidence_not_found')::int) as unlocated,
       count(*) filter (where (meta->'rules'->>'unsure_rules')::int > 0) as with_unsure_rules,
       count(*) filter (where (meta->'rules'->'pool'->>'code_unavailable')::int > 0) as with_comment_only_rules,
       round(avg((meta->>'reply_chars')::int)) as avg_reply_chars
  from autowriter.batch_metrics
 where meta->>'mode' = 'tool_call' and meta->>'tool' = 'check_rules'
   and created_at > now() - interval '7 days'
 group by 2 order by 1 desc;

-- 评论(W-51): 判了几次、几篇帖子、一篇帖子判几轮、整组过了几次、被拒的调用几次
select count(*) as score_calls,
       count(distinct meta->'comments'->>'post8') as posts,
       round(count(*) filter (where meta->>'outcome' = 'ok')::numeric
             / nullif(count(distinct meta->'comments'->>'post8'), 0), 1) as calls_per_post,
       count(*) filter (where meta->'comments'->>'passed' = 'true') as sets_passed,
       count(*) filter (where meta->>'outcome' = 'refused') as refused,
       sum((meta->'comments'->>'n')::int) as comments_judged,
       sum((meta->'comments'->>'no_slot')::int) as without_slot
  from autowriter.batch_metrics
 where meta->>'mode' = 'tool_call' and meta->>'tool' = 'score_comments'
   and created_at > now() - interval '7 days';

-- 评论: 每样检查打回了几条(配比 / 重复 / 短评 / 品牌收尾 / 写法底线各样)
select k.key as check_name, sum(k.value::int) as comments, count(*) filter (where k.value::int > 0) as calls
  from autowriter.batch_metrics m,
       jsonb_each_text((m.meta->'comments'->'checks') - 'style' || coalesce(m.meta->'comments'->'checks'->'style', '{}'::jsonb)) as k
 where m.meta->>'mode' = 'tool_call' and m.meta->>'tool' = 'score_comments'
   and m.created_at > now() - interval '7 days'
 group by 1 order by 2 desc;

-- 写手手上的 skill: 入口工具每次报上来的版本(按天)
select (created_at at time zone 'Asia/Shanghai')::date as day, meta->>'tool' as tool, meta->>'skill' as skill, count(*)
  from autowriter.batch_metrics
 where meta->>'mode' = 'tool_call' and meta ? 'skill' and created_at > now() - interval '14 days'
 group by 1, 2, 3 order by 1 desc, 4 desc;

-- 存档那一行的三小块: 没参照过卡的批次占多少、skill 版本构成、规则还有几条没编出来
select count(*) as commits,
       count(*) filter (where (meta->'flywheel_lessons'->>'borrows')::int > 0) as borrow_known,
       count(*) filter (where (meta->'flywheel_lessons'->>'shown_max')::int = 0) as no_cards,
       count(*) filter (where (meta->'flywheel_lessons'->'statuses'->>'followup_timeout')::int > 0) as followup_timed_out,
       count(*) filter (where (meta->'skill_versions'->>'outdated')::int > 0) as with_outdated_skill,
       count(*) filter (where (meta->'rule_compile'->>'uncompiled')::int > 0) as with_uncompiled_rules
  from autowriter.batch_metrics
 where meta->>'mode' = 'deskcore_commit' and created_at > now() - interval '14 days';

select created_at, left(project_id::text, 8) p, meta->'rule_compile' as rule_compile
  from autowriter.batch_metrics
 where meta->>'mode' = 'deskcore_commit' and (meta->'rule_compile'->>'uncompiled')::int > 0
 order by created_at desc limit 20;
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
也拆开认（`delivery.split_tags`；摘话题、话题行、查话题的规则都走它），不会拼出 `#["a", #"b"]`。`problems` 只报代码判得准的：`tags` 外壳或列表成员形状不对（固定话题模板也报）、话题不够、评论条数不对、某条评论形状不对、某条评论没提到 `must_mention_any` 里的词、
表格要方向而这篇没带、标题或正文里已经自带了标记、模板不管话题而 `tags` 里有正文里没有的话题（拼不进交付稿）；`index` 是写手传的位置。正文里混进来的评论段、别的标记（【话题】这类；模板不管话题时【话题】是项目要的样子，不报）、字面的 `<br>` 和 `\n` 在 `draft_checks` 里报（§3.21）；【标题】/【正文】只在这里报，那边不再报一次。写手照协议先改 problems（带 `replaces_version_id`
再存一次），再把 `text` 原样整段交给用户。**改稿重存时拼的是原来那一整批**（2026-10-30）：话题、评论、方向不进 `versions`，所以存档时把这一批的顺序和每篇的这三样记进 `batches.params.delivery_parts`（`{"order": [每篇第一版的 version_id], "parts": {version_id: {写手传了的那几样}}}`，只在项目有模板时记，不加迁移）；改稿挂上旧 item 时新的那一版的三样并进去。重存时按记下的顺序，没改的几篇用它们现在那一版（标题正文从 `versions` 取），改过的换在原位，`delivery.scope = "whole_batch"`（另带 `batches` / `untouched`）；改的稿子分属几批的一批一段、按建的先后排，同一次另有新稿的拼在最后；`problems` 只报这次传的那几篇。拼不回来的（这之前存的batch、补录 / 底稿转正的、批里的稿子和记下的对不上、查库出错）照旧只拼改过的几篇，`scope = "this_call"`，`scope_fallback` 说原因（`batch_not_recorded` / `batch_changed` / `batch_unknown` / `lookup_failed`），`next_step` 多一句「照样原样交、说明是改过的那几篇」。原来是只拼改过的几篇、叫写手的模型自己按顺序替换原来那几行，和「原样交」打架。

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

**正则源码最长 300 字**（和 Jev `/compile_rules` 一致），在解析和编译之前拒绝过长输入，防止校验阶段绕过执行时限。已存的超长正则列为没查，不视为守住。模板项目的标题按实际交付的转换和去空白结果查；中文域名和路径后的 URL fragment 留在正文，不当成话题摘走。

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
- 没编出来的拆小重发一次（2026-11-11，审计 A5 / JP-5）。Jev 把一次请求按 10 条一批交给模型，一批里有一条让模型拒答 / 输出不合格、
  或者那一次调用没通，这 10 条就一起带 error 回来。原来整批记失败，6 小时后（还得有人开这个项目）原样 10 条再发、再一起失败——11-11
  一个项目最早的 10 条硬规则就这样一条都没编出来。现在一次请求回来之后，带 error 的（和回包里漏掉的）马上拆成每次最多 3 条
  （`RULE_COMPILE_RESEND_CHUNK`）各重发一次：是某一条的事，别的几条这次就编出来了；是那一下没通，重发多半也过了。Jev 一条都没编出来
  回 502（`jev_failed`）或回包不成形（`error`）的那一批同样重发。**不重发的**：整批被挡的那几种和读超时（照旧停整次运行）、sha 对不上的
  （两边算法漂了，再发也一样）。重发的小请求自己撞上被挡 / 读超时，同主循环一样停，没重发成的照第一次的失败记。一次 `compile_rules` 里
  重发最多花 10 分钟（`RULE_COMPILE_RESEND_BUDGET_SEC`），到点剩下的照第一次的失败记、下次再来（Jev 生成端慢的时候每个小请求都要等到它
  自己超时）。重发就在这一次 `compile_rules` 里，后台路径照旧握着那一个编译槽位：一批 10 条最多多发 4 次请求，别的项目多等这一会儿。
  报告里多两个数：`resent`（重发了几条）、`recovered`（其中又编出几条）；重发都编出来了这次就是 `ok`（`batch_errors` 留着第一次那笔）。
  重发之后还没编出来的记一行 WARNING `compile_rules: 拆小重发 N 条, 编出 M 条, 还有 K 条没编出来(project=…): <归类>: <规则 id 前 8 位>, …`
  ——归类是一个固定的短词（`refusal` / `truncated` / `generator_http_<状态码>` / `generator_timeout` / `bad_output` / `no_result` /
  `missing_in_response` …，认不出来的记这一小批的结局 `batch_<结局>`；同一套词也进存档指标的 `rule_compile.error_kinds`，§3.8 末尾），
  不带规则原文，也不带 Jev 的错误原文（「输出不合格」后面跟的是模型那段输出的毛病，里面可能有规则的内容）。CLI 的 `compile-rules`
  同样重发，多打一行。
- 不让失败变成花钱的循环：同一条规则的同一版**重发之后还是**编失败的，后台 6 小时之内不再编它（进程内记账，重启清零；CLI 不受这条限制）；
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
   **只问评论 / 话题的题不发**（2026-11-11，审计 S2 / JP-1；`rulecompile.unseen_questions`）：发给 Jev 的只有标题和正文，这种题是对着一篇
   没有评论的稿子答的——「评论里有没有带上…」（`pass_if` 是）篇篇答「否」，硬规则篇篇报「必须改」，怎么改正文都消不掉；
   「评论里有没有…」（`pass_if` 否）篇篇算守住，其实没看。11-11 线上一个项目的一道题让 15 篇里 15 篇报了「必须改」，改过重存的 5 篇照报，
   历史上记下的 19 条硬规则违反里 15 条是它。编出来的 `code` 早就按 `where` 拦了（步骤 4），题这边原来没有同样的一道。
   只看题面的字：`ask` 提到 评论 / 话题 / 标签、又一处没提 正文 / 标题 / 全文 的才算（「正文或评论里有没有…」是正文的题，照问）；
   `scope` 标了 `title` / `body` 的不算（编译时就指明只看标题 / 正文的题，「标题有没有话题钩子」里的「话题」不是话题行；线上这种题的
   `scope` 都是 `full`）。在拼题库**之前**拿掉——`project_bank` 是 Jev 那边的移植、逐字节钉着，不在里面筛。一条规则的题全被拿掉、
   别的也没查它的，和步骤 4 里只看话题 / 评论的 `code` 并成一个集合数进 `rules.code_unavailable`（两边都沾的只数一次）；拿掉一部分、
   或者题全拿掉但编出来的 `code` 照查了标题 / 正文的，数进 `rules.partial`（`partial_rules[].uncovered` 写「只问评论 / 话题的规则题没问」）。
   两种 `next_step` 都不说它（整条）守住了。拿掉的题号记一行 INFO 日志（只有题号）。11-11 当天的编译结果里这一刀拿掉 12 道题
   （三个项目 8 + 2 + 2，五条规则的题全在里面），没有一道提到标题或正文的题被拿掉。按字认会漏掉不带这几个词的问法（比如只说「楼中楼」，
   当天没有），漏掉的照旧发。**复盘注意**：拿掉之前那 15 条假的硬规则违反已经在规则题账本里（`run_tag=shadow-rules`），校准时要排除这
   几条规则的题；拿掉之后这些题不再写账本。真要按评论查这些规则得把评论交给 Jev，是另一件事，这次没做。
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
硬规则报「必须改」，写手照着一个账本里记成无效的答案去改稿，还不知道改哪一句）。没判出来的题数进那篇的 `counts.unjudged`（只有题数，
11-11 起不逐条列）、全部篇加起来进 `summary.unjudged`（整篇所有题都未判出来的也计入），`next_step` 说「另有 N 道规则题这次没判出来，不能说守住了」；
其中 `evidence_not_found` 的另数进 `summary.evidence_not_found`（有才带），`next_step` 单说一句「Jev 怀疑有问题、但指不出是哪一句：不算没守住
（也不说成守住了），不用改」（2026-11-11，审计 S5 / JP-4：线上没判出来的全是这一种，没有一句可改，也不是回包缺了题，原来和缺题混成
一句「没判出来…不用重试」；数照旧都在 `counts.unjudged` 里，协议 5a 认的是它）。一道都没判出来的
那篇（回包里没有 `desk_rules`，或每题都带 `invalid_reason`）记 `judge_status: not_judged`，和没判完一样算进 `summary.not_finished`。
10-20 复审：原来这里只在那篇上记个数，`summary` 不看它，`next_step` 照说「这次查的规则都守住了」——现在这句只在必须改 / 建议改 /
拿不准 / 没判完 / 没判出来 / 没编译结果 / 话题评论查不了 / 编不了 / 编出来的检查没跑 / 只查了一部分 / 代码没查完 / 撤掉了代码检查又没有题 /
正文是空的全是 0 时才说。

**返回值**（一篇一项 `versions[]`）：`violations`（每条 `rule_id` / `severity` / `what`（以「必须改」或「建议改」开头）/ `source`（jev | code）/
`p` / `evidence`，必须改的排前面、全列，建议改补到每篇一共 12 条为止，同 §3.10 的 `rule_checks`；同一条规则在一篇里只留一条，见下）、`more`（这篇没列出来的建议改条数）、`counts`（`must_fix` / `suggest` / `unsure` / `unjudged`）、`judge_status` / `judge_note`、`code_unchecked`；
外加 `summary`（必须改 / 建议改 / 拿不准 / 没判完各几处、没判出来几道题；有才带：`more` 没列出来几条、`evidence_not_found` 几道题指不出依据句、`empty_body` 几篇正文是空的、`code_unchecked` 几处没查完）、`unsure_rules`（有才带，见下）、`rules`（池子几条、Jev 查几条、代码查几条、查不了几条、
还没编几条和为什么、话题评论查不了几条、编不了几条、编出来没跑几条、只查了一部分几条、人设过 check 由 `score_drafts` 查的几条、撤掉了代码检查又没有题的几条、正则没查完几条）、`uncompiled_rules`、这次没查到的 `remaining_version_ids`（有才带）
和一句 `next_step`。`summary.more` 不是 0 时 `next_step` 说还有这么多条建议改没列出来、如实告诉用户（独立复审 on aw E：原来
只在每篇的 `more` 里，`next_step` 和工具说明一字不提，写手照列出来的说完就算了；同 §3.10 的 `rule_checks_more`）。没有可查的规则时回
`status: nothing_to_check` 和一句话，什么都不发。

**只逐条列要动手的**（2026-11-11，审计 F1 / JP-6）。11-11 线上判过规则题的 43 版，一篇平均 5.0 条拿不准（上限就是 5，所以 `more`
几乎次次不是 0）、2.9 道没判出来，都是工具说明和 `next_step` 说「不用改」的；拿不准的答案里 76% 偏向守住。10 篇的回包一万两三千字，
大半是这些行。现在：
- 每篇不再带 `unsure` / `unjudged` 两个列表，只留 `counts` 里的条数；`more` 只数没列出来的建议改。
- 最外层多一个 `unsure_rules`（最多 3 条，`CHECK_RULES_MAX_UNSURE_RULES`）：只收**偏向没守住**的拿不准（答案和 `pass_if` 不一样、
  把握不到 0.7 或 Jev 标了歧义），一条规则一行 `{rule_id, severity, what, drafts}`——`what` 是「拿不准: 要点」，不带每篇各自的答案和把握，
  `drafts` 是这次有几篇在它上面拿不准、偏向没守住；硬约束的排前面，再按篇数。偏向守住的只计数（照旧不算守住）。
- `next_step` 的拿不准那句带上数：「有 N 处「拿不准」(涉及 M 条规则), 提一句就行, 不用改」。
- 同一条规则在一篇上只报一条（`rulecompile.one_per_rule`）：一条规则编成两三道相近的题、几道都没守住时原来各报一条（线上 128 条
  里只有 114 个不同的「规则 × 篇」），现在留带依据句的那条，都带或都不带留先出现的；`counts` / `summary` 按留下的数。只并 Jev 判的；
  代码查的命中照旧另列。
- 代价：回包里看不出是**哪一篇**拿不准、哪条规则没判出来（数都在）；一条规则的两道题问的是两件事、又都没守住时，只报其中一件。
  后台的改稿复查（§3.19 的 AU-F07，`_recheck_digest`）只读 `counts`、`summary` 和 `violations[].rule_id`，不受影响。

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

### 3.12 存档时核对 angle_key 和飞轮臂，全拒也留原因（2026-10-27，W-31）

10-26 sportsix 一场：08:06 UTC 发牌 20 张，08:10～08:13 存了 5 批 19 篇，每篇都带 `angle_key`，可一条都不在台账里
（台账的 key 是 `fingerprint.angle_key` 出来的 20 位十六进制；这 19 条是 7～8 位带下划线的编号、53 位的长串，模型
自己编的），20 张牌一张没销、一天后回了池子。`consume_angle` 找不到行只记一行 warning，`unattributed` 只数没带 key
的，回包只有 `consumed_angles` 少于 `written`——写手那边的 AI 从头到尾不知道 key 是错的。同一场 5 批里 4 批记了
`flywheel_arm=patterns`（模型传回的），而那场借卡是 explore（TV 缓存键 `:explore`，没给规律）；08:09 还有一次 20 篇
全拒（written 0），`batch_metrics` 只记了总数，拒的原因事后查不出。三处一起改，都是纯代码，**不加迁移**
（`batch_metrics` 的 `counters` / `meta` 本来就是 jsonb）。

**key 对不上台账。** `score_drafts` 销账没销成的那几篇，再按 key 查一次本项目台账（`store.angle_key_states`，只在
有没销成的时候查、一次查询）：有、但每一行都已经销给了别的版本（上一批用过、同一批两篇带了同一个 key）算 `used`；
还有没销账的行却没销成的（同一个 key 一行已销、一行没销也算这种）是并发 / 出错，不算 key 的问题，照旧进
`angle_ledger_warning`。这个项目台账里没有的再按形状分三种（复审补的：10-27 查生产库近 60 天 48 条对不上的里 15 条是
20 位十六进制，7 条是同一个人在别的项目抽的牌、8 条哪个台账都没有，说成「自己编的」写手会去改一个本来没错的 key）：

| `problem` | 判据 | 回包怎么说 |
|---|---|---|
| `unknown` | 形状不是 20 位小写十六进制（`fingerprint.looks_like_angle_key`） | 自己编的、缩写的或抄错了；**原样复制 `draw_angles` 给的那个**，不要自己编号、缩写或改写 |
| `other_project` | 形状对；`angle_ledger` 里有调用者自己（`drawn_by`）在**别的项目**抽的这一行（`store.angle_keys_drawn_elsewhere`，只在有这种 key 时多查一次，走不上 `(project_id, angle_key)` 索引，台账几千行不加索引） | 是你在别的项目抽的牌，稿子是不是存错了项目，跟用户确认；条目多带那个项目的 `project_id`。别人在别的项目抽过同一个 key 不算 |
| `not_in_ledger` | 形状对，哪个台账都没有（或者查别的项目失败了） | 多半是发牌时台账没写进去（非原子路径 `record_draw` 失败只记日志），不是写手编的或抄错的 |

送去查的 key 只收 `[0-9A-Za-z_-]{1,64}`：postgrest-py 的 `in_()` 不转义 `"` 和 `\`，一个坏 key 会让整条查询报错，
那种形状本来也不可能在台账里，直接算 `unknown`。回包多几个字段：

| 字段 | 什么时候有 | 内容 |
|---|---|---|
| `unknown_angle_keys` / `used_angle_keys` | 每次（同 `unattributed`） | 篇数，和 `unattributed`（没带 key）分开数；`unknown_angle_keys` 是这个项目台账里没有的三种合计 |
| `angle_key_mismatch` | 有对不上的 | 每篇 `{index, angle_key, length, problem}`（`other_project` 另带 `project_id`），`angle_key` 只露前 24 字（模型编的长串可能是一句描述） |
| `angle_key_warning` | 同上 | 一段话：按上表每种几篇、怎么回事；「原样复制、不要自己编号」只在有 `unknown` / `used` 时说；已经存了的不用重存；挂着的牌在 `angle_debt` 里一并回去，但不说「按它们写」——那几张牌对应的稿子可能刚存过，再写一遍就是同一个角度两篇。同一批里还有没带 key 的，`unattributed_warning` 这时也不带「按它们写」那一截（复审：两句话叫模型做相反的事） |

`angle_ledger_warning` 只报剩下的（key 本身没问题的那几个）；按 key 查也失败了，就笼统报「没查清」，不猜。查询失败
不抛：指纹这时已经写进库了。

**下一次提醒也带上。** 只靠存档那次的回包不够：牌还挂着，之后 `draw_angles` / `check_drafts` / `record_rule` 的
`archive_pending`（W-29）只会说「还有 N 张没存档」，模型明明存过了，只会当提醒有误。所以存档时对不上的记一笔，
`archive_pending` 多带 `angle_key_mismatch = {drafts, unknown, used, by_problem, keys, last_at, note}`（`keys` 最多 10 个、
截断；`unknown` 同回包的三种合计，`by_problem` 分开数）。`note` 按出现的那几种拼：「这些牌其实写完存了、别按它们再写」只对
`unknown` / `used` 说（别的项目的牌、台账没有这一行的，跟这个项目挂着的牌无关），「不要自己编号」只对 `unknown` 说。
记在进程内存里，同 `_borrowed_arms` 的取舍（deskcore 单进程）：重启丢了只少这一段提醒，`archive_pending` 照旧只多一次
查询；窗口和 `archive_pending` 一样 1 天，**每篇对不上的各按自己存档的时刻过期**（codex review on aw#130：原来整段跟着最近一笔
续命，0、23、46 小时各一篇的，46 小时时三篇都还在，旧 key 还占着 `keys` 的位置），记和读的时候都先剪掉过了窗口的，`last_at` 是
还在窗口里最近那一篇的时刻；按 (项目, 存档的人) 记，别人的不给看；只在还挂着牌时带。

**模型传回的臂以服务端为准。** 进程里原来只记 (项目, 写手) 最近一次借卡的臂，模型传回的臂认得出就照收。现在记的是近
36 小时**每个臂最后一次发出去的时刻**，传回的臂按顺序过（`core._reported_arm_check`）：

1. 进程里什么都没有（重启后没借过 / 36 小时没借过）：无从核对，照旧采用、来源 `brief`；
2. 是最近一次发的臂，或者 **12 小时内**（`_ARM_REPORT_TTL`）发过：采用、来源 `brief`（同时开着两场、跨北京 0 点各拿一个臂的，
   两个都在，各记各的，codex review on #104 那条不退）。复审前放的是整个 36 小时：同一个写手昨天开过一场别的臂，今天这场传错
   就核不出来——生产库近 30 天 21 个 (项目, 写手, 北京日) 里 9 个前一天 36 小时内也有一场，其中 4 个是另一个臂；
3. 进程起来不到 12 小时（`_ARMS_SINCE`），传回的臂正好是起来之前那一段（往回 12 小时到起来那一刻）某个北京日哈希出来的臂：
   可能是重启前那一场拿到的，**核对不了**。记服务端最近一次发的臂、来源 `borrow`，传回的那个另存 `flywheel_arm_reported`，
   另标 `flywheel_arm_unverified: true`（`batches.params` 和 `batch_metrics.meta` 都有，**分析对照组时剔掉**）；回包带
   `flywheel_arm_unverified = {reported, recorded, note}`，只说重启过核对不了，不说「服务端分的是 X」（部署落在一场写作中间时
   那样说是错的，10-26 当天 04:26 UTC 就部署过一次）。改稿（`replaces_version_id`）不建新 batch，挂的旧 batch 也标
   `flywheel_arm_unverified`（codex review on aw#130）：旧 batch 正好是记下的那个臂时，拿它比不出 `flywheel_arm_mixed`，可改稿
   可能是在传回的那个臂下写的；和记下的臂不同的照旧另标 mixed。改稿没传臂、重启后（或 36 小时内）也没借过卡、按入库时重算的
   （来源 `recomputed`）同样标：重算出来的臂是猜的，新 batch 靠来源剔，旧 batch 的来源还是它自己的，只能靠这个标。没标上的同
   mixed 一样进 `arm_mixed_unmarked_item_ids`；
4. 其余：服务端没给过这场这个臂。以最近一次发的为准，来源 `borrow`，传回的那个另存 `flywheel_arm_reported`，回包带
   `flywheel_arm_corrected = {reported, recorded, note}`。

`borrow_lessons` 的 `flywheel_arm`（沿用这场的臂）走同一道：1～3 沿用（核对不了的也沿用，不把重启前那一场改成另一个臂），
4 不沿用、按当天重新分——不然一句传错的臂先被借卡记下来（还真按它借了规律），入库时就核不出来了。没选「标 mixed」：被纠正
的那批成稿只受过一种处理（服务端发的那个臂），剔掉反而白丢样本。仍然核不出的：① 重启后什么都没借过就入库的（第 1 条，来源
`brief`）；② 一场写作开了 12 小时以上、中间同一个写手又借到另一个臂，这一场后面的批次会被「纠正」成新的臂；③ 前一天晚上
（12 小时内）那一场的臂，第二天一早新开的一场传错成它，照收。分析对照组时剔掉 `flywheel_arm_unverified` 和
`flywheel_arm_mixed` 的批次。

**分到的臂 ≠ 看到的规律（外部审计 11-02 F13，2026-11-02）。** patterns 臂的那一场，借卡超时、馆员降级（`status=degraded`）、
馆员这次没给规律时，模型手里一条规律都没有，这批却照样记 patterns。降级那一种是写作台自己丢的：TV 先算规律、后选卡，选卡
失败时规律照样回来，`librarian_client` 原来在看 `patterns` 之前就按降级返回了。现在降级 / `error` 时卡照旧是空的、结局照旧
`error`，规律照样交给模型。另外借卡交出去之后把**实际给出去几条规律**记在发出去的那个臂旁边（进程内存 `_patterns_shown`，
同 `_borrowed_arms` 的取舍）：`open_project` 记简报里带的条数（超时 / 借阅线程起不来 = 0，后台线程之后选完了也不算），
`borrow_lessons` 记这次给的条数（2026-11-11 起它也限时，到点没等到 = 这次 0 条，§3.26），原样传回简报里的臂（同一场接着借）
就和前面那次取大的。入库时 `batch_arm` 第四项取出来，
写进 `batches.params.flywheel_patterns_shown` 和 `batch_metrics.meta.flywheel_patterns_shown`：整数 = 给出去几条（explore 臂
是 0）；`null` = 服务端不知道（重启过、按入库时重算的臂、记下的那次借卡和最近一次发臂对不上）；没有这个键 = F13 之前的批次。
臂怎么分一点没变。分析 L-07 / G-05 时 patterns 臂只算 `flywheel_patterns_shown > 0` 的，0 和 `null` 单列一组。W-38 查重底稿
转正建的 batch 不带这个键（`draft_snapshots` 没有这一列）。

复审（同日）：团队共用一个身份（同一个 `user_id`），同一个项目同一天的几场拿到的是同一个臂、记在同一个键下。原来只留最后一次
借卡的条数，A 场入库记的就是 B 场的（A 简报 1 条、B 降级 0 条 → A 的批次记 0；反过来 A 超时 0 条、B 3 条 → A 没看到规律的批次
记 3，进了处理组）。现在每次发臂各记一条（时刻，条数），留最近一次之前 12 小时（`_ARM_REPORT_TTL`）以内的：条数都一样才记这个
数，对不上就记 `null`——服务端分不出这一批是哪一场的。`borrow_lessons` 原样传回臂的那次和前面最近那条取大的，那条若是别的场的，
别的场自己那条还在、条数对不上，照样 `null`。所以 L-07 / G-05 读到的 `null` 也可能是**几场叠在一起**，不只是重启 / 重算；
整数是这 12 小时里每一场都给了这么多条。

**全拒也留原因。** `batch_metrics.meta` 有被拒的才多几个键，只留数和字段名，不留标题、正文、撞上的是哪篇：
`rejected_by`（gate → 篇数：`pre_commit` / `atomic_recheck` / `replace_lookup` / `empty_body` / `version_lookup`）、`rejected_signals`
（依据的信号 → 篇数：`opening` / `title` / `ngram` / `contain`，一篇可以有几个）、`rejected_scope`（`in_batch` / `history`）、
`empty_body_fields`（正文空的那几篇带了哪些字段名，W-30：多半是正文放错了字段）。`counters` 另记 `unattributed` /
`unknown_angle_keys` / `used_angle_keys`，`meta.angle_key_mismatch` 记 `{index, problem, length}`（不记 key 本身）。

> **正文放错了字段先对上（2026-10-30 发布收尾）。** `check_drafts` / `score_drafts`（和旧名 `commit_drafts`）在工具层（`tools._draft_fields`）
> 先看每篇：`body` / `title` 空着、而别名键（`正文` / `内容` / `content` / `text`，标题是 `标题`）里**恰好一个**有字，就拿它当正文 / 标题；
> 本名有字的不覆盖，两个别名都有字的不猜。对上的那份就是查重比的、底稿记的、存进 `versions` 的（之后 `check_rules` 查的也是它）。
> 哪儿都没有正文的照旧按 `empty_body` 拒。对上了几篇只进 `deskcore.tools` 的 WARN 日志（键名 → 篇数，不带内容），回包不多字段。

```sql
-- 全拒的批次为什么拒(W-31)
select created_at, left(project_id::text, 8) p, counters->>'drafts' d,
       meta->'rejected_by' by_gate, meta->'rejected_signals' signals, meta->'rejected_scope' scope, meta->'empty_body_fields' fields
  from autowriter.batch_metrics
 where meta->>'mode' = 'deskcore_commit' and (counters->>'written')::int = 0 and (counters->>'rejected')::int > 0
 order by created_at desc limit 20;

-- key 对不上台账 / 臂被纠正的批次
select created_at, left(project_id::text, 8) p, counters->>'written' w,
       counters->>'unknown_angle_keys' unknown, counters->>'used_angle_keys' used,
       meta->>'flywheel_arm' arm, meta->>'flywheel_arm_source' src, meta->>'flywheel_arm_reported' reported,
       meta->>'flywheel_arm_unverified' unverified, meta->>'flywheel_patterns_shown' shown, meta->'angle_key_mismatch' mismatch
  from autowriter.batch_metrics
 where meta->>'mode' = 'deskcore_commit'
   and (coalesce((counters->>'unknown_angle_keys')::int, 0) + coalesce((counters->>'used_angle_keys')::int, 0) > 0
        or meta ? 'flywheel_arm_reported')
 order by created_at desc limit 20;
```

协议没改（版本不变，写手不用重导 skill）：`score_drafts` 的工具说明补了这几个字段，回包和提醒自己把话说清楚。

### 3.13 归档：不用的项目不进清单、不进跨项目检查（2026-10-30，发布收尾）

**为什么。** 主身份名下 41 个项目，在用的六个左右。不用的那些照样排在 `list_projects` 里，模型每场开头要在一长串里挑；`score_drafts` 的跨项目检查（`project_isolation`）也拿它们的品牌词和近期标题去比——一个早就不写的项目的品牌词出现在正文里，就报一次「可能串项目」。

**标记在哪。** `projects.default_params` 的 `"archived": true`，不加迁移（同 `delivery` / `isolation_terms`，整列存成 JSON 字符串）。只认 JSON 的 `true`；`default_params` 解析不了按**没归档**算（读是宽的：写坏一行不能把在用的项目藏起来）。**没有任何路径自动写它**，写手那边也没有对应的工具，只能用运维命令：

```bash
python -m deskcore.cli project-archive --project <uuid>               # 标成归档; 写回时保留别的键, 已经归档就不写
python -m deskcore.cli project-archive --project <uuid> --unarchive   # 撤销: 删掉这个键(不写 false)
python -m deskcore.cli projects --user <uuid> --include-archived      # 全列出来, 归档的带 archived: true
```

`default_params` 本身解析不了就拒绝写、退出码 1（同 `delivery-format`：拿 `{}` 去合并会把别的键一起抹掉）。

| 哪里 | 归档之后 |
|---|---|
| `list_projects` | 默认不列；回包带 `archived_hidden`（藏了几个，没有就不带）和一句 `archived_hidden_note`（怎么看它们）。工具说明叫模型：要写的品找不到、而 `archived_hidden` 大于 0 时先带 `include_archived=true` 再看，别急着 `create_project` 建重。列出来的归档项目带 `archived: true`，在用的行形状不变 |
| `create_project` | 建之前再找一遍（复审）：同名（不分大小写、首尾空格）的照旧不建，归档的那个品牌对得上（或没给品牌）就回它、带 `archived: true` 和一句「用户要写的是它就照常用」，品牌对不上也不建、但叫模型先跟用户确认。名字只差空格、分隔符（`-` `_` `·` `/` `、` 等，夹在两个数字中间的不算）、全半角、大小写的（给了品牌时品牌也一样）同样不建：**在用的优先**，回它并叫模型先跟用户确认一句（写法不一样，不能闷头用）。「D1-2」和「D12」、「2.0」和「20」、「¥99套餐」和「99套餐」不算同名。兄弟项目里归档的带 `archived: true`。工具说明的触发条件改成「带 `include_archived=true` 再查也没有」 |
| 跨项目检查（`_cross_project_shadow`） | 候选集里去掉归档的项目，品牌词和标题向量都不拿它们比；当前项目自己归档了照比。`projects_total` 是去掉之后的个数，去掉了几个记在 `archived_skipped`（随 `project_isolation` 进 `batch_metrics.meta`） |
| `open_project` | **照开，不锁人**——一个团队共用一个身份，某个写手要接着写一个旧项目是正当的。简报一样不少，只多 `archived: true` 和一句 `archived_note`：用户点名了这个项目或品牌就照常写、不提它暂时不用；只有模型自己挑的项目才先跟用户确认（复审：原来每次都确认，点名要的项目再被问一遍是多余的一问）。打开归档项目记一行 INFO 日志 |
| `record_rule` 的串项目闸 | **没动**：归档项目的专属词照样挡。那道闸拦的是写，要不要放宽是另一个决定 |

协议没改（版本不变，写手不用重导 skill）：`list_projects` / `open_project` 的工具说明补了这两个字段。回归在 `tests/test_project_archive.py`。

### 3.14 写评论：没有链接时按贴来的正文在 TV 里找帖（2026-11-02）

**为什么。** `comments_for` 按链接找帖（`core._note_from_link`）时能把帖子下已有的评论一起带回来，模型写的时候避开已经问过的事。写手常常不给链接，只贴正文（飞书那一行的正文格整段复制，带不带标题行、末尾带不带话题都有）+ `project_id`；链接在 TV 里没找到、改贴正文的也一样。原来这条路不找 TV，`existing_comments` 永远是空的。

**怎么找（`core._note_from_text`，只有 `comments_for` 走）。** 先过归属闸（`assert_project_access`），再只在这个写作台项目对着的 TV 项目里找（`store.tv_projects_of`，`tv_project_map`），回来的行再核一遍项目号——别的项目的笔记一条不回。

| 步 | 做什么 | 口径 |
|---|---|---|
| 拆 | 带「【标题】…【正文】…」标记的按标记拆（`tvlink.parse_raw`），不带的整段当正文；剥话题（`tvlink.strip_hashtags`）。去标点后不到 20 字不查 | 同 `tvlink.MIN_PREFIX`：太短的到处撞 |
| 粗筛 | 正文里最长的两段连续汉字 / 字母 / 数字（`store.TV_TEXT_RUN_CHARS`，≥ 8 字，截前 16 字），逐段 `ilike` 一次 `raw_content`，新发布的在前、一页 30 条（`store.tv_notes_by_text_run`）。两段都查、候选合在一起排，不提前停（ilike 逐字比，某一行在某一段里标点不一样就会漏掉；codex review on aw#138 两轮），最多两次 | 进 PostgREST 过滤串的只有这几类字符（没有 `%` `_` `,` `(` `)` 空格），照样过 `_like_escape`；段不合规的 store 层直接回空、不发请求。只比 `raw_content`：生产库 6838 行 `body` 列全空、`raw_content` 每行都有 |
| 细比 | 去标点、剥话题之后的四字串，交集占**较大那一边**的比例 ≥ 0.90（`core.TEXT_MATCH_SIM`；完全一样直接算 1；两边都要有 15 个四字串，太短的只认完全一样）。只比正文对正文（`core._text_score`）：贴来的开头就是这条笔记的标题、笔记正文自己又不以标题开头时，那一截是正文格里的标题行，剥掉再比 | 两个方向的包含度都要够、长度也要差不多：一句共用的话术过不了，一篇整段包含另一篇、多出一成以上的也过不了。标题不进分：阈值是按正文对正文量的，同标题的另一版带上标题会被推过线（见下） |
| 多条都像 | 先按帖子（发布链接）比谁更像；同一条链接的几行里活的优先（`v_notes_vanished` 里的让位，同 `_note_from_link`），再取更像的；一样像的再看活不活、再取发布最近的；视图读不到就当都活着 | 同一段文案改几个字又发一次：贴来的是哪一版就取哪一版。别的帖子活着、只是够像，不压过这篇只剩已删那行的原文 |
| 回包 | `source = "tv_by_text"`，带 `note_id` / `tier` / `existing_comments`（截断说明照旧）和一句 `tv_by_text_note`（是按正文对上的；同一段正文发过不止一条时取最近的；用户说不是这一篇就别用 `existing_comments` / `note_id`，照贴来的正文写、不用再调——链接在 TV 里没找到时带链接再调还是走这条路、对上同一篇）；只剩已删那行时带 `tv_note_vanished` 和按正文的说法 | 标题正文照旧用贴来的，TV 那份只用来取评论 |

**尽力而为。** 拆不出能查的段、项目没映射、查库出错（`tv_projects_of` / 粗筛 / 读评论任何一步）、没有一篇到阈值，都和原来一样：`source = "pasted"`、`existing_comments` 为空，出错的记一行 ERROR 日志，不抛、不拦写评论。按链接那条路一个字没动（读评论出错照旧报错）。`score_comments` 原来不走这一步（只用标题正文判，多两次查询换不来什么）；11-10 起也走（Codex on aw#149）：判的标题正文照旧是贴来的，对上的笔记只拿来取已有评论查重复（§3.20 P3 / P5），不然 `comments_for` 给写手看过的已有评论、判的时候一条都不比。多的两次查询（各约 10 ms）算在同一个判定截止里；对上的笔记评论读不到时当没对上、不提示，对上的那行已删也不在回包里说。

**阈值的依据（11-02 查生产库 `truth_vault.notes` 全部 19 个项目，同项目两两比，正文去标点后不完全相同的 52.8 万对）。** 链接不同的两篇到 0.90 的只有 2 对（0.951 / 0.937），都是同一段文案隔几天又发了一次；紧挨着的 7 对在 0.875～0.897：同标题同开头的另一版、同一个故事写了两三版、一篇整段包含另一篇再加一句——阈值放到 0.85 就会把它们认成同一篇。同一条链接的两行（挪表、改过的旧行）9 对里 4 对 ≥ 0.90、3 对 0.80～0.90、2 对不到 0.6——改得多的认不出，只是退回贴正文。这些数都是正文对正文量的，所以标题不进分（复审 on a3d12e6）：原来笔记那边还拿「标题 + 正文」比，飞书正文格带着标题行贴过来时比的就成了标题 + 正文对标题 + 正文，RIO 两组同标题不同链接的帖子正文 0.893 / 0.897，带上标题是 0.905 / 0.902，过了线；现在按笔记的标题剥掉标题行再比，这两组回到 0.893 / 0.897。映射的 10 个 TV 项目 3295 篇带标题的里，正文以自己标题开头的只有 14 篇，这种不剥。宁可认不出，也不把别的帖子的评论安到这篇头上。正文完全一样的两行有 261 对：153 对是同一条链接（挪表留下的旧行等，活的优先），69 对链接不同（同一段文案发了两次）分不出是哪一篇，按上面的顺序取，回包那句话说清楚。粗筛那一段的依据：同一天抽查五个项目 1489 篇，每篇最长那段（截 16 字）在本项目里最多出现在 27 篇里（94% 只有它自己），一页 30 条装得下；第二长的那段最多 82 篇，只作后备。粗筛查询在最大的项目（657 篇）上执行约 10 ms（按 `project_id` 走索引再过滤）。

协议没改（版本不变，写手不用重导 skill）：`comments_for` 的工具说明补了这条路和 `source` 的三个取值。回归在 `tests/test_deskcore_comment_tools.py`。

### 3.15 贴型：写手说了才标，带方向的规则跟着卡走（2026-11-03）

**为什么。** 老板的内容层：产品贴 P（产品在正文里出场）、流量贴 T（冲着切口写，产品多半靠评论区种）、提问贴 Q（和 T 同一族）、精品贴 J（产品融进切口）。飞书「方向」列只有一部分项目有，项目整体的配比也不等于写作台这一批要写的那一片，所以**不给默认配比**，只在写手 / 模型说了「写 N 篇 X 贴」「这批都是产品贴」时给卡标。效果学习这一轮不动（按贴型分开学留到以后）。

| 环节 | 做什么 | 没标贴型时 |
|---|---|---|
| `draw_angles(layer=…)` | 一个贴型（每篇都是）或 `{贴型: 篇数}`（其余不标）；别名见 `vocab.LAYER_ALIASES`；`pin` 里的「方向」/`layer` 归进这里（`drawpolicy.split_pin_layer`），不当维度。写错、篇数超过 n、和 pin 里的方向说的不一样 → `layer_invalid`、什么都不写。按槽位先后标（`slot_layers`，不用 rng）；点名重写的带原来那张卡的贴型（写手这次说「都是 X」时按 X），各几篇的说法先拿它们抵账 | 一字不差：`tests/golden/draw_v2_c5ca36c.json` 钉着候选表、台账 `_draw`、重发的行、牌和卡面（rng 消耗也一样）；v1 总开关照旧 |
| 台账 | `_draw.ly` = P / T / Q / J；不进 `angle_key`、不进去重；备牌跟着它那个槽位，尾巴只在整批同一个贴型时带。`ledger_dims_for` 带出 `_draw: {ly}`（`public_dims` 照样剥掉） | 没有这个键 |
| 卡面 | 「第i篇」下面一行 `vocab.LAYER_LINE`；流量贴 / 提问贴的「直给推荐」「场景植入」换一句不叫人硬插产品的大白话（`PLAIN_FORMAT_TRAFFIC`）。渲染只看卡上的值 | 不加行 |
| 规则 | `core.rule_applies_to_layer`：流量向只管 T / Q，产品向只管 P / J（hard、soft 一样），通用的照旧每篇都管。简报里带方向的规则后面注【只管流量贴/提问贴的稿】/【只管产品贴/精品贴的稿】，那一层末尾一句说清卡上没写贴型的照常守（root 的 `memory.py` 不动，改的是传进去的副本）；这场方向闸挡掉的另一个方向的 soft 规则单独一截接在 P1 最后（`OTHER_DIRECTION_BLOCK`，过同一道相关性、同样封顶，`counts.soft_rules_for_other_layers`），写明只管卡上写着那一族贴型的稿、没写贴型的不用管 —— 复审：不然标了另一族贴型的稿会被报一条简报里从没出现过的「建议改」，原来那一截一个字不变；`score_drafts` 的代码查规则和 `check_rules`（Jev 规则题按贴型分组发各自的题库，编出来的 code 逐篇过滤）按每篇的贴型查，这一批哪一篇都不管的规则先拿掉（不报「没查完」、不数进 stats / pool / uncompiled，也不为它补编）。贴型从台账来，存档和 `check_rules`（`store.resolve_version_layers`）认同一张卡：销给这一版的那张卡（存档时销账先做完，新稿就用销的那一行：`consume_angle` 销账时把那一行的贴型一并回给 `commit_drafts`，存档不再按版本读一次 —— 同一个 key 台账里有两行没销的时候，`consume_angle` 销的是调用者最早那一行，按 key 找到的是最近抽的那一行，Codex review on aw#141；多读那一次的话，它一失败就只能退回按 key 找）；改稿重存的那一版自己不销牌，找同一篇稿子别的版本销的那张（这一步读失败照旧按 key 找）；找到了卡、卡没标贴型也算定了。两条线上都没有卡的才按 `angle_key` 找最近抽的那一行（存档时按稿子带的 key，`check_rules` 按指纹行记的 key，`store.ledger_layers`） | 原来的口径：soft 过这场的方向闸，hard 都管；读不到台账也是这样 |
| 参考帖 | 外部帖多读 Jev 的 `product_role`（`EXT_QIDS`），池子条目带 `prod`（1 出场 / 0 没出场，答了才有）；产品贴 / 精品贴的 e 槽优先 `prod=1`，流量贴 / 提问贴优先 `prod=0`。起量帖读 TV `notes.content_layer`（单独一次可选的查询，不进 `TV_OUTCOME_COLS`；列不在就不读），条目带 `ly`，h 槽分三档：形式一样且同一族 → 形式一样 → 同一族。只是优先、不筛（外部帖进池的门槛按族算，见表下第一段），√日均评论的权重不变，记的 p 按同一套分档算（`_e_marginal` / `_h_marginal`） | 不分档 |
| 判定提示 | `score_drafts` 那次判定本来就回 `profile.product_role`：产品贴 / 精品贴判出「未出现」、流量贴 / 提问贴判出「主角」时，那一篇的 `judge.results` 带一句 `layer_hint`，next_step 叫模型提一句；不扣分、不拦存档、不多调 Jev | 不带 |

**外部帖按族比日均评论（同一轮，老板 11-03 定的）。** 正文里产品当主角 / 解决方案的外部帖（Jev `product_role`）评论本来就少（近 45 天日均评论中位数 0.71，其余 2.84），按全品类一个中位数切，P/J 卡想优先的 `prod=1` 在进池之前就被切掉大半。`priors.external_pool` 改成按族切：P 族 = `product_role` 是主角 / 解决方案，O 族 = 其余（包括没答的），各自在帖龄合格的帖上取中位数，留 ≥ 自己那一族中位数的；一族不满 `ext_family_min_n`（5）篇就用全品类的中位数；整个品类一篇 `product_role` 都没答（读挂了 / 没抓）就和原来一样只有一个中位数（回包也不带 `family_median_velocity`）。权重照样 √日均评论、不打折，`ext_ok` 照样数切完的池子。按族切只管哪些帖进池：`title_shares`（`compose` 按 `ext_title_blend` 0.25 掺进非问句的标题句式 π）、`top_structures`、`recent_14d` 和掺外部那一步的篇数门槛（回包新加的 `summary_eligible`，`ext_title_min` 数它；没有这个键的外部块照旧数 `eligible`）仍按全品类一个中位数切出来的帖算 —— 复审：不然没标贴型的卡的标题句式 π、`cli` / `project_now` 的摘要行在生产上也跟着变，超出这次定的范围；要让它们也跟新池子走，得另外定。金标不受影响：两份发牌金标的外部池是合成的，不经过 `external_pool`。

**投流事件（同一轮）。** `priors.is_clean` 另外排除 `truth_vault.v_paid_boost_events` 里的帖（投了薯条、没标 `paid_promoted` 的也在里面；service_role 读得到，有 `note_id` 列）：经 `priors.paid_boosted` 那份进程缓存读（复审：视图每读一次要算 ~0.5 秒，原来每次建全局表 / 项目块都读、还要多读一页空的，压在发牌冷缓存那 2.5 秒里）：全局表、项目块、简报的 working 一栏（`store._recent_published`，有候选时才读）共用一份，6 小时一刷，同一时刻只一个线程读、手里有旧的先用旧的；读不到时有旧的用旧的，没有就记 WARN、照原来的口径算，5 分钟内不再去读。`store.tv_paid_boost_note_ids` 不满一页就停。生产上起量标签会少几篇（11-03 视图里 38 篇，其中 28 篇没标 `paid_promoted`，是这次新排除的）；快照脚本的离线模式（读导出文件）不读这个视图。

**`_draw` 的预算。** 预算那条用例（`test_drawpolicy` 的夹具）标了贴型照样 ≤ 300 字。最坏情形本来就超过 300（点名四个维度、L1 放宽、旧先验的牌 359 字；带参考帖、重用、保底、旧先验的牌 333 字），贴型只多 11 字；没标贴型的牌一个字不多（金标钉着），所以没动已有的键。回归在 `tests/test_draw_layers.py`、`tests/test_layer_rules.py`。

### 3.16 写评论：按链接找到帖子时，暂时不用的项目不拿来问（2026-11-03）

**为什么。** 一张飞书表（一个 TV 项目）可以对着好几个写作台项目。夜里没对上这条笔记写的是哪一个时，`comments_for` 把调用者名下的全列出来让写手挑（`choices`）。按团队身份数，近 60 天途鸽 144 条链接里 126 条要在 8 个名字里挑，其中 7 个是早就归档的方向（途鸽D-1…D-7）。

**怎么去（`core._narrow_note_projects`，只在候选不止一个时走）。** 去掉归档的（`project_archived`，`list_projects` 默认也不列它们，§3.13），剩一个就直接用。两条不去：

- 这张表自己的补录目标（`ingest_target`）归档了也留着。它是这张表的本家，归档的项目照样能写；去掉它可能只剩一个挂在这张表上的别的品，不问就定成那个，规则全是别的品的。
- 一个在用的都没有就照旧全给。

**没做的：按「哪张表的本家」替他定。** 第一版（同一个 PR 的头一个提交）还去掉「在别的表当补录目标的」候选，理由是「这张表里的帖子写的就是这张表的品」。复审查生产库推翻了：成人藻油那张表（BJS_phase2）里 10-07～10-19 的帖是青少年款（D-129 更正：对照指回 百健士-藻油 的 42 篇里 41 篇；夜里没对上、只靠 `ingested` 指回的 25 篇里 24 篇，另 1 篇 10-21 是成人帖），按那条规则会定成成人项目，评论就丢了 百健士-藻油 那几条青少年款的硬规则。所以几个都在用就照旧问；`ingested` 那一行指着哪个项目也不拿来定（同 `test_an_ingested_link_is_not_a_direction`）。成人藻油那张表的链接照旧要问一句。

不动的：夜里真对上了（`tv_note_links` 的匹配档）就用对上的那个，归档的也用；`score_comments` 走同一条路。

协议没改（版本不变，写手不用重导 skill）。回归在 `tests/test_deskcore_score_and_comments.py`。

### 3.17 评论的写法底线：不用句号、不用方言语气词、不写像要联系方式的字（2026-11-03）

**为什么。** owner 11-03 拿一批途鸽维护评论来问：「写作台加一条，把句号去掉」「修正一下方言的语气，或者多审核一下，最好不需要人工再来改」「这个v是个啥」。查下来，「v」「伐」「真滴」「pls」是途鸽项目一条 08-28 的硬规则要求的（「每批评论中必须按比例混入网络缩写（如 v、pls、HC）……方言词（"伐"、"真滴"）」），模型照着按比例往句尾塞；句号是另一条 10-14 的硬规则（「所有文案必须含有正常的中文标点符号（包括逗号、句号……）」），评论也拿到了。「v」在小红书评论里多半是「加 v / vx = 微信」，像在要联系方式（引流），平台会判违规，不是网感。

**怎么做（只管评论，不管笔记标题和正文；所有项目都管，项目规则说要也不行；代码在 `deskcore/commentstyle.py`，两条路共用）。**

| 环节 | 做什么 |
|---|---|
| `comments_for` | 回包多一个 `style`（`core.COMMENT_STYLE_LINES` 三句），next_step 叫模型和 hard_rules、comment_rules 一起守，先别写出来 |
| `score_comments` | 判之前先用代码查（`core.comment_style_issues`，不调模型）：全角「。」任何位置、半角句末单个「.」（「...」不算；3.5 的点不在句末本来不算，「今年28.」算）；方言语气词「伐」（只认句末 / 标点前，步伐、讨伐、砍伐不算）、「侬」、「阿拉」（阿拉伯、阿拉斯加不算）；像要联系方式的：单独一个 v（后面是标点、空格或结尾；「V计划」这种后面接汉字的不算）、加微信 / 加v / 加vx / 加个微信、光一个「加微」时后面得是标点、结尾或语气字（「添加微量元素」「增加微调」不算）、vx、wx、薇信、v我（「威信」是常用词，不查）。有一样那条 `passed=false`、带 `style_fix`，整组 `passed=false`，`style_next_step` 叫模型照改、不用问用户；判定服务没判成时这几样照样在 `style_issues` 里回 |
| 跟着笔记交付的评论（交付模板有评论栏：西屋911gt、Hatherine痘痘贴、百健士-藻油） | `delivery.requirements` 的 task 里那句「comments 给 N 条」后面接上 `commentstyle.SHORT`；`delivery.assemble` 逐条查，查出来的进 `delivery.problems`（「第 k 条评论: …」），照现有「先改 problems 再带 replaces_version_id 重存」那条路改。复审 aw#143 补的：原来只查单独写评论那条路，而这三个项目按写作台稿子发出去的评论正是带句号的那一批（百健士 14/14） |

送去 Jev 判的文字、拼进交付稿的评论都照原样，不替模型改（改了模型就不知道自己写的哪里不对，下一批照样写）。按协议「没过的按修改建议改一轮再调一次」，模型自己改掉，写手不用动手。

**途鸽那两条规则**另给 owner 一份 SQL（备份 + 改写，不删）：混入缩写那条改成「评论区：」开头的建议级（常见缩写、同音梗偶尔用，不按比例硬塞，不用方言语气词，不写 v / vx / 加微），句号那条写明只管标题和笔记正文。

**检查范围。** 这里的代码只查三条写法底线，不核验经历、效果或数字的来源。具体事实依提供素材，不编造；亲历背书和旁观推荐本身可以写，不因表达类型自动转人工，当前评论位及判定口径见下一节。

协议没改（版本不变，写手不用重导 skill）：`comments_for` / `score_comments` 的工具说明补了 `style` / `style_fix`。回归在 `tests/test_deskcore_score_and_comments.py`。

**一组评论里以品牌评价收尾的最多四分之一（2026-11-09）。** owner 11-09：一组维护评论几乎每条都在最后一句给品牌下个结论（「……，<品牌>这波挺懂行」「……，<品牌>确实让人安心」），「20 条里有个 3-5 条够多了」。同一套代码（`commentstyle.brand_endings`，纯函数，不调模型）补了第四条底线：

- **怎么数。** 去掉末尾表情、贴纸、标点后按标点和空格切小句。品牌叫法在**最后一小句**里才算；最后一小句很短（≤6 字，「真的稳」）时，品牌在**前一小句末尾**（后面最多两个字：「最后选了<品牌>，挺好」「<品牌>这波，真的稳」）的也算。品牌在开头、后面接一段事实的不算（「<品牌>帮我改了简历，三天就过了」「…报的班 简历改了三版」）。整条只有一小句时那一句就是收尾，提到品牌就算（光看字分不出「<品牌>的课我上过」和「<品牌>这波挺懂行」，四分之一的余地留给它们）。问句收尾不算：问号、吗 / 么、怎么样 / 如何 / 啥…、「A不A」「有没有」「…过没有」；「呢」要有别的疑问的样子（疑问词、「那…呢」「你们呢」「觉得呢」），「的嘛 / 了嘛」是陈述，「挺好的呢」「挺靠谱的嘛」照样算品牌收尾。品牌叫法＝`projects.brand` ＋ 交付模板评论栏的 `must_mention_any` ＋ 品牌里的英文段，不分大小写；都没有退回项目名。两个字的中文品牌按字面找，偶尔会跨词撞上，少见，不另做排除。上限 `ceil(25%·n)`：20 → 5、7 → 2、4 条以内 → 1。按给的顺序留前面几条，后面的才要改；`style_fix` 点名落在品牌上的那一两小句。§3.18 的口径（自然提、有素材的亲历和推荐）不动，只管收尾。
- **单独写评论。** `comments_for` 的 `style` 多第四句（`commentstyle.BRAND_ENDING_LINE`）；`score_comments` 拿到项目之后数，超出的那几条 `style_fix` 加一句改法、`passed=false`，`thread_fix` **第一句**点名留哪几条、改哪几条（`thread_fix` 只留 3 条，代码这一句放最前面不会被判定服务的几条挤掉）。品牌收尾的句式重复（末三个字一样，或「<品牌>这…」起头一样；不落在品牌上的收尾不数）够 3 条时进 `thread_hints`，只提示、不算没过。判定服务没判成时照样在 `style_issues` / `thread_fix` 里回；线程占满、查帖子超时那两种还没拿到项目，没有这一项。上限按每次送来的那一组算，所以 `style_next_step` 和 `score_comments` 的工具说明都写明：改完再判时整组全部送、顺序不变（只送改过的几条，上限会重算）。
- **跟着笔记交付的评论。** 不数，不进 `delivery.problems`：一篇只有 2~3 条，11-09 量过本来就合规（18 组里 4 组有品牌收尾，每组最多 1 条）；有 `must_mention_any` 的模板每条都要点名品牌，数了只会误报，而「先改 problems 再重存」那条路没有一轮的上限。`delivery.requirements` 的 task 接上 `commentstyle.BRAND_ENDING_SHORT`，只是写法提示。
- **笔记正文不管。** 11-09 量过桌面稿（近 30 天 1,068 版，去掉话题行和表情）：最后一句提到品牌的 0~6.9%、最后一小句 ≤1.7%；发出去的笔记最后一句 0~4.1%。问题只在多条一组的评论上。
- **根子在项目数据。** 有的项目已确认的硬规则要求大部分维护评论带品牌并夸，代码这条上限每一轮都会和它顶着；要不要改项目规则由 owner 定，工程不擅自改。（11-10 owner 改了口径，这两条规则不再原样留着：见 §3.20。）

协议没改（`2d357c4b6075`，写手不用重导 skill）：只改了工具回包和 `comments_for` / `score_comments` 的工具说明。回归在 `tests/test_comment_brand_endings.py`。

### 3.18 评论可自然提品牌，可写有素材支持的亲历与推荐（2026-11-03）

**当前口径。** `comments_for` 的四个默认评论位都允许自然点名品牌，不要求每条点名；提问位仍是提问，经验位允许补充经验、亲历背书或旁观推荐，贴主回复位允许答疑、亲历背书或旁观推荐，照旧接住它回复的 q1。具体经历、效果和数字依提供素材，不编造事实；没有素材时写提问或一般讨论。表达类型本身不触发禁用或人工复核，有具体疑点才按返回结果核实。

**不设默认配额。** `score_comments` 和 core 中保留的 `judge_thread` 默认 `max_named=None`，向 Jev 传 JSON `null`，整组不再固定最多一条点名。只有用户明确要限制时才传整数，`0` 仍表示整组不点名；自定义评论位的品牌要求和其他检查照旧。（11-10 起：项目在 `comment_set` 里配了提品牌上限时由代码数，`max_named` 不转给 Jev，见 §3.20。）Jev 的「允许点名」表示可提可不提，不是必须出现品牌。

三条写法底线、项目规则、不得重复已有问题、改一轮后交付、失败不能说通过，以及单独评论不入稿件库等流程保持原有要求。协议与 skill 同步更新；写手需要重新导入 skill，旧版本在 `get_protocol` / `comments_for` 的版本核对时会收到更新正文与提醒。**部署先 Jev 后 autowriter**：旧 Jev 的 `max_named` 不接收 `null`，先部署 autowriter 会导致评论检查请求报错。回归覆盖评论位、默认无配额、显式整数转发和写手侧说明。

### 3.19 协议升版：合规问题 AI 自己改，发牌改版和贴型的协议跟进，改稿在后台按规则复查（2026-11-04，W-42 / W-45 / W-50 / AU-F07）

**为什么现在动协议。** 换版（aw#137，11-02）之后原定两周不动协议，最早 11-16 之后。§3.18（aw#144，11-03）已经把协议改到 `30daa73b0535`，写手本来就要重导一次 skill，而那之后写作台还没有人用过。三处等着下一次协议窗口的跟进（W-42 / W-45 / W-50）放进同一版，写手只重导这一次：协议 `30daa73b0535` → `2d357c4b6075`（以 `python -m deskcore.cli sync-skill --check` 为准；部署后 `/health` 的 `build.protocol_version` 应是它）。§3.18 的评论口径一字不动。

**W-42 合规问题自己改。** 换版后写手唯一还要开口的一步：AI 说出合规问题，写手回「第 N 篇按这条改」。现在 `score_drafts` 判出 `fixes` / `hard_fails` 的那几篇，模型不等写手开口：先把 `check_rules` 调完，把合规问题（Jev 拿不准的也改）和「必须改」放在同一轮改掉，改过的带 `replaces_version_id` 先过 `check_drafts` 再 `score_drafts`，交付改好的那一版，交付那条回复里一句话说清改了哪几篇的哪里。

- 改一轮就够。服务端按这次带 `replaces_version_id` 的那几篇分开说（`_score_report_step` 的 `revised`）：改过重存之后还查出来的，只叫模型照实说一句、不再接着改，免得改、判、改地转下去。写手当场要求改的那篇改完又查出合规问题，也是这样只说一句，写手要改再说。
- 拿不准的也改：10-19 线上实查，影子期判出的 7 条硬伤 `p` 全在 0.54～0.69，没有一条够得上「有把握」（`p ≥ 0.7`；出处是 `deskcore/core.py` 里 `SCORE_SURE_P` 上面那段注释）。只改有把握的，那一次就一条都不会自己改，照旧全要写手开口；合规上宁严勿松，改一句话的代价小。
- 「建议改」照旧告诉写手、由写手定，不在这一条里。
- 服务端同一个口径：`SCORE_NOTE`、`_score_report_step`、`score_drafts` / `check_rules` 的工具说明、`check_rules` 必须改那句 next_step。

**W-45 发牌改版的协议跟进（aw#140 冻结期只改了工具说明）。**

- 开场多一项：有 `project_now` 就把它的 `message` 原样放进开场，排在 `skill_update` 之后、`edit_digest` 之前。`PROJECT_NOW_TASK` 和 `open_project` 的说明原来写的是「不放进开场」，一起改了。`warming` / `unavailable` 时简报里没有 `project_now`，开场就不放。
- 借卡那一段原来对所有经验卡都说「数据验证过」。D-138 起每次借卡至少 1 张外部参考卡，现在分开说：`external: true` 的是别家同品类最近起量的帖，没经过我们的数据验证，不说成验证过的经验，也不提它里面的品牌。
- 发牌那一段跟上新卡面：每个坐标后面跟着一句大白话，内容形式和标题句式照卡写，情绪杠杆和人性原型用自己的话写，卡上的分类名和判据说法不搬进标题正文（同 `ANGLES_FOOTER`）；「参考结构」那一行只借结构、不提参考帖里的品牌，本项目的品牌照写；`batch_note` 交付时原样说一次、不放进开场；`pin` / `repeat_of` 只在写手点名时用。
- skill 头部：描述里加了「写产品贴或流量贴」，写手只说贴型、没报品牌时也能调起来。

**W-50 贴型。** 协议加一句：写手说了要写哪种贴（或者各几篇）才带 `layer`，卡上「这篇是X贴」那一行照着写；没说就不带，也不问。怎么传、写错报什么照 `draw_angles` 的说明（§3.15）。

**AU-F07 改稿在后台按规则复查一次，只给复盘看。** 改过重存的那几版不进 `check_rules_version_ids`（改一轮就够，协议和 `check_rules` 都这么说），编译好的语义规则就一直没查过它们（审计 11-02 F07）。W-42 之后改稿会变多，所以 `score_drafts` 存完档、指标那一行插进去之后，这次真有改过重存的版本，就在后台对这几版跑一次 `check_rules`（同一个调用者、同一个 `tactic`，一次查不完的接着查），结果摘成一小块并进那一行的 `batch_metrics.meta.revision_rule_recheck`：

```
{status, must_fix, suggest, unsure, [not_finished, unjudged, code_unchecked, empty_body],
 uncompiled_rules, versions: [{version_id, judge_status, must_fix, suggest, unsure, rule_ids}],
 revised, elapsed_ms, at}
```

- 只留数和规则 id，不留稿子、规则原文和 Jev 指出的句子。不进回包、不转给写手、不叫模型再改。
- 和 `check_rules` 是同一个函数：规则题照样发 Jev、写进规则题账本（`run_tag=shadow-rules`），缺编译结果照样触发后台补编。一版多一次 Jev 调用，11-02 审计时 30 天是 6 次改稿的量。
- 一个工作线程，名额 4 个、从投进去一直占到跑完：同时最多 4 次（1 次在跑、3 次排着），第 5 次就跳过、记 WARN；进程重启丢掉还在排的。那一行就没有这个键：**没有这个键 = 没复查**，不是复查过了。`check_rules` 抛了记 `status: error`；写回失败只记日志。
- 测试里默认关掉（`tests/conftest.py` 把池子换成 None），要测它的用例换成就地跑的池子（`tests/test_revision_recheck.py`）。

**规则检查的后台补查（2026-11-11，审计 S3 / JP-3）。** 新稿的规则检查靠模型在 `score_drafts` 之后多调一次 `check_rules`。10-23 上线到 11-11，136 版新稿里只有 36 版在账本里有规则题的答案，有五个写稿日是 0 版；「人在发布前改了什么」那 326 对里一对都没有规则题的答案，而 11-20 要拿这两样对着校准规则题。所以每次存完档（真存进去了才排），在改稿复查的同一个工作线程上、排在它后面，再排**一个**补查（`core._rule_sweep`）：

- 列这个项目近 21 天（`RULE_SWEEP_DAYS`）、建了超过 3 分钟（`RULE_SWEEP_MIN_AGE_SEC`）的稿子——每篇只看交出去的那一版（`items.best_version_id`；改动对只挂在这一版上），补录副本不算——去掉账本里已经有规则题答案的（`truth_vault.note_feature_answers`，`subject_type = 'aw_version'`、`run_tag = 'shadow-rules'`，就是 `check_rules` 自己写的那些行；读法 `store.versions_with_answers`，只取 `subject_id`），剩下的一次最多查 20 版（`RULE_SWEEP_MAX_VERSIONS`），多的留给下一次存档。
- 刚存的这一批不到 3 分钟，不在里面：模型自己那次 `check_rules` 通常二十来秒就到，等它先查。它查了的账本里有行，这里一次 Jev 都不多花；它没查的，下一次存档补上。
- **旧的在前。** 改动对只有发出去的稿子才有，发布比存档晚好几天，越早的稿子越可能已经有了那一对；最早的那几版也最先滑出 21 天的窗口。新的在前的话每次的名额都先给刚写的（模型多半会自己查），等着校准的旧稿一直轮不到、过期。
- 用的就是 `check_rules`：同一个调用者、同一个 client（和改稿复查一样是存档那次的），答案由 Jev 照常落账本，缺编译结果照样触发后台补编。`tactic` 传空——旧稿那一场的 `tactic` 哪儿都没存（批次行上的一直是空的），拿这一场的去判旧稿的方向会把该问的软规则挡掉；传空就是只按项目名判方向，判不出来全放行，卡上标了贴型的稿子本来就逐篇跟着贴型走。查的是**现在**的规则池，不是写那篇时的。
- **不写 `batch_metrics`（存档那一行不多一块，也不留「每次工具调用一行」，§3.8 末尾）、不进回包、`next_step` 和 `check_rules_version_ids` 一字不动**：写手和模型都看不到它。只有一行 INFO：`规则补查(project=…): 近 21 天 N 版, 已有答案 A 版, 这次查了 C 版(判回来 J 版), 跳过 S 版, …ms`（跳过 = 还没有答案、这次没查的）。没跑完记一行 WARNING，绝不冒泡到存档。
- 账本就是记录，进程重启不丢进度。进程里另记两样省读数的：已经知道有答案的版本不再问账本；查过一次的版本 6 小时内（`RULE_SWEEP_RETRY_AFTER_SEC`）不再查——正文是空的、这一组没有规则题、Jev 没判完的那些查了也没有行，不记的话「旧的在前」会一直卡在它们上面。
- 同一个项目同时只有一个在排 / 在跑（只管本进程）。没配判定服务、`RULE_SWEEP_AUTO=0`、池子关着（测试里默认，`tests/conftest.py`）时什么都不做。不占改稿复查那 4 个名额。

看补到哪儿了（近 21 天写作台的稿子里，交出去的那版有规则题答案的占多少）：

```sql
select b.project_id, count(*) as delivered,
       count(*) filter (where exists (
           select 1 from truth_vault.note_feature_answers a
            where a.subject_type = 'aw_version' and a.run_tag = 'shadow-rules' and a.subject_id = v.id::text)) as with_rule_answers
  from autowriter.versions v
  join autowriter.items i on i.id = v.item_id and i.best_version_id = v.id
  join autowriter.batches b on b.id = i.batch_id
 where v.created_at > now() - interval '21 days' and b.params->>'source' = 'deskcore'
 group by 1 order by 2 desc;
```

改稿复查复盘时这样查：

```sql
select created_at,
       jsonb_array_length(meta->'revised_item_ids')             as revised_items,
       meta->'revision_rule_recheck'->>'status'                 as recheck,
       (meta->'revision_rule_recheck'->>'must_fix')::int        as must_fix,
       (meta->'revision_rule_recheck'->>'suggest')::int         as suggest
  from autowriter.batch_metrics
 where meta->>'mode' = 'deskcore_commit'
   and jsonb_array_length(coalesce(meta->'revised_item_ids', '[]'::jsonb)) > 0
 order by created_at desc limit 50;
```

`revised_items` 大于 0 而 `recheck` 是空的，就是那一行还没有复查结果：要么还在排、还在跑（`check_rules` 一页查 20 篇、整页共用 `JUDGE_DRAFT_TOOL_TIMEOUT_SEC` 的截止，默认 18 秒，多出来的接着查下一页），要么被跳过了（名额满了，或进程重启丢掉了排着的）。指标那一行没插成的不会在这里出现——没有那一行，也就不会排复查。

回归：`tests/test_protocol_followups_1104.py`（协议和服务端同一个口径、aw#144 的评论口径原样在）、`tests/test_angle_block_score_report.py`（自己改一轮、改过的不再改）、`tests/test_project_now.py`、`tests/test_revision_recheck.py`。

### 3.20 一组评论：项目定的配比、不写「帖子里」、不重复说法、交付那一格、人工核实列全文、不用含糊指代（2026-11-10）

**为什么。** owner 11-10 看了写作台交出去的一组维护评论，定了七件事（下面 P1～P7）。这一次**取代 11-09 「项目里那两条评论配比规则先留着」的决定**：那时只加了「品牌收尾最多四分之一」（§3.17 末段），配比规则原样留着；现在 owner 改了口径，配比由代码按项目配置数，项目规则的改写由 owner 自己跑 SQL（备份在先，同 11-03 的做法），工程不改规则数据。全部只在工具回包、工具说明、`commentstyle` 的几句里，协议没改（`2d357c4b6075`，写手不用重导 skill）。

| | owner 的要求 | 怎么做 | 范围 |
|---|---|---|---|
| P1 | 一组里提品牌的最多一半，至少三成中性 | `commentstyle.brand_share` / `neutral_share`。提品牌＝品牌叫法在这条里任何地方出现；上限 `floor(brand_max·n)`（20 → 10、7 → 3），按给的顺序留前面的，后面的 `style_fix`「去掉品牌，也不换成代称」。中性＝不提品牌，而且 Jev 判的主要行为（`comments[i].profile.speech_act`，原来 aw 丢掉了）是提问 / 质疑 / 补充经验 / 回复答疑 / 闲聊表情；说不清、没判出来的不算。下限 `ceil(neutral_min·n)`（20 → 6），不够时整组 `passed=false`，代码那一句点名先改哪几条（提品牌超出的先，再从后往前挑） | 只有配了 `comment_set` 的项目 |
| P2 | 不写「帖子里」 | （2026-11-11 起放宽成评论里一提帖子就算，见 §3.22。）`commentstyle.issues` 多一样：帖子里 / 帖子中 / 帖子内 / 帖里 / 帖中 / 贴子里，开头、句中、结尾都查（请帖、字帖、妥帖、补贴里不算）。Jev 冻结题干里的「可见帖子里具体说到的事」会被修改单原样引用、等于叫模型写「帖子里」，转给写手之前换成「帖子」 | 所有项目，两条路都查 |
| P3 | 一组里不重复小句 | `commentstyle.repeated_clauses`：去掉贴纸、表情、标点，品牌叫法（不管长短）归成一个记号，按标点和空格切小句、一段重复不跨小句，汉字一个字一个、英文一个词一个。规则 A：连着 6 个字在前面哪条里出现过；规则 B：连着 5 个字出现在 3 条以上；规则 C：开头 2 个字一样的超过 2 条。后面那条 `style_fix` 引原话、说和哪条重复。参照里还有帖子下已有的评论（P5）和同一批前几次判过的（`earlier_comments`） | 所有项目，只在 `score_comments` |
| P4 | 要有一些短句 | `commentstyle.short_share`：去掉空白、标点、表情、贴纸后 `short_chars`（15）字以内的算一句话短评，至少 `ceil(short_min·n)`（20 → 4），不够整组没过，先建议把问句压成一句。另加一句只提示、不查的「长评论也多用短句」 | 门槛按项目；提示所有项目 |
| P5 | 交付放进飞书那一列 | 项目配了列名时 `score_comments` 回 `delivery_column = {column, text, lines}`：一行一条、条内换行换成空格、不编号不加标签；`delivery_column_next_step` 叫模型原样交、说清贴进这条帖子那一行的那一格、格子里有内容就接在最后另起一行（不覆盖）、列名不贴进去；**贴几行就算发了几条**，只交真要发的那几条；一批分几次判的，每一段用它最后一次判回的那一份，按判的顺序接起来交。读的一侧：`comments_for` 和 `score_comments` 也读那一格（`truth_vault.notes.raw_extra[列名]`，没声明的列在 `_undeclared` 下），按行切、去编号，最多 50 行 × 200 字，`comments_for` 放进 `existing_comments`（带 `source`），`score_comments` 拿来查 P3 | 只有配了列名的项目 |
| P6 | 要人核实的评论列全文 | 原来 `score_comments` 每条只回下标和评论位，写手那边的模型把要核实的评论缩写成半句给用户看。现在 `needs_review=true` 的那几条带 `text`（全文）和 `review_why`（Jev 拿不准的门槛题和它现在判成什么、有题没答出来），另加 `review_list = [{label, text, why}]` 和 `review_next_step`：逐条照原文写全、不缩写不省略、不用「…」代替，后面跟疑点 | 所有项目 |
| P7 | 不用含糊指代 | `commentstyle.issues` 多一样，见下 | 所有项目，两条路都查 |

**项目配置。** `projects.default_params` 里的 `comment_set`（和 `delivery` / `archived` 同一个 JSON 字符串，不加迁移）：`{"brand_max": 0.5, "neutral_min": 0.3, "short_min": 0.2, "short_chars": 15, "column": "<列名>"}`。每一项都可以不配，不配的不数；份额在 [0, 1]、`brand_max + neutral_min ≤ 1`，`short_chars` 6～40，列名 1～20 字、不带 `|` 和换行。列名只在库里，代码和测试里不写。配错了不报错：记日志，回包 `comment_set_note` 一句「按没配处理」。`comments_for` 回包多一个 `comment_set`（按配的数写好的几句 + 列名），说明和 style 一样要守、项目规则说法不同的以它为准。配了 `brand_max` 的项目 `score_comments` 不把 `max_named` 转给 Jev（回 `max_named_note`）：Jev 的点名数是模型判的（雇主、平台名也算），修改单从前往后删，和代码按顺序留前面的正好反着。

**整组怎么报。** 份额一律按整数百分比算（`n × pct // 100`），浮点在个别 n 上会多一条（25 × 0.28 在浮点里向上取整是 8）。顺序是：提品牌超出 → 重复 → 品牌收尾（提品牌超出、要去掉品牌的那几条按空的算，免得同一条既叫它删品牌又叫它把品牌往前挪）→ 短评；判完再数中性。「太多」的进那几条的 `style_fix`，「太少」的整组没过。所有代码数出来的整组那几样并成 `thread_fix` 的**第一句**（「整组代码查: …」；2026-11-11 起这几个字后面带一句是谁定的、怎么改，见 §3.22），后面才是 Jev 的，`SCORE_MAX_FIXES=3` 照旧、代码那一句挤不掉。整组 `passed = Jev 过了 and 每条都没有 style_fix and 中性、短评都够`。判定没判成时，提品牌、重复、收尾、短评照样在 `style_issues` / `thread_fix` 里，中性那一句说这次没数；线程占满、查帖子超时那两种还没拿到项目，只有每条的底线。帖子下已有的评论在查帖子的同一个截止里读，读不到只提示「只查了组内重复」。一批超过 20 条分几次判时，后面几次带上 `earlier_comments`（最多 60 条，只查重复，不送 Jev）。跟着笔记交付的评论只多 P2、P7（进 `delivery.problems`），整组的几样不数（一篇 2～3 条，有的模板每条都要点名品牌）。

**P7 含糊指代怎么认。** owner 举的五个都是「<一个词>那<量词>」，指帖子里的某一处，读者不知道是哪一处（这里换成假的上下文：「社团那栏」「摸鱼那几个字」「海外仓那条路」「三小时那次」「卡住那阵」）。只认这个结构，不列禁用词：

- 「那」前面得是一个字或者右引号，不是句首、标点后（句首的「那阵真慌」「那次以后」是普通说法；引号括起来的词接「那几个字」「那栏」，如「“摸鱼”那几个字」，一样是指着帖子里的那几个字，人写的评论里一条都没有）；前面那个字不是代词、助词、介词或「走 / 选 / 换」（我那次、的那次、在那栏、走那条路）；同一小句里「那」前面四个字以内有「我」的不算（「我面挂那次」是在说自己的事）。
- 量词只认五样：栏 / 格、（几 / 两 / 俩…个）字、条路、次、阵；量词后面接它量的东西时不算（那格局、那字幕、那阵风）。那么、那样、那时、那里、那儿、那会儿、那天、那些、那种、那边、那就、那是、那一刻、那一下、那段时间都不在里面，一概不查。
- 「这」不查：人写的「这」几乎都指同一条评论里刚说过的那句（「……，这句话太对了」「X这点挺好」），不是指帖子。
- 11-10 拿人写的评论量过（只读 SQL）：`truth_vault.comments` 10,588 条里 7 条（0.07%）、别的项目的评论区快照 3,817 条里 1 条，都是「X那阵子 / X那次」说自己的事；运营写的维护评论 4,708 行里 30 行（0.6%），是同一个结构（「X那几次」「X那栏」）；那么 / 那样这类普通说法 0 条。owner 举的五个全中。

**数据侧（owner 做，工程不改）。** 三个相关项目的 `comment_set` 由 owner 用 SQL 加；一个项目原来要求「每条都带品牌」的评论配比规则，由 owner 按新口径改写（备份在先）。只有一个汉字的品牌叫法不再用（`_alias_re` 跳过一个字的叫法：一个已归档的子项目品牌只填了一个字，按字面找会撞上一堆普通词）。

回归：`tests/test_commentstyle_set_checks.py`（纯函数：P2、P7 正反例、规则 A/B/C、份额取整、单字叫法）、`tests/test_comment_set_score.py`（`score_comments` / `comments_for`：配了 / 没配 / 配错、整组没过、`max_named` 不转、`delivery_column`、判定没判成、那一格的读法、`earlier_comments`、`review_list` 全文、交付那条路）；`tests/test_comment_brand_endings.py` 跟着改成合并后的那一句。

### 3.21 一批笔记：同一批里的重复说法、开头一样的提示、正文里混进来的版式残留（2026-11-10）

**为什么。** #149 之后 owner 问：一组评论的那几样检查（§3.20）要不要也管笔记。只搬了三样：重复说法（评论规则 B）、开头一样（评论规则 C，笔记这边只提示）、正文里混进来的版式残留；别的留在评论那一侧，理由见「不做的」。都在 `score_drafts` 存完档之后、只查存进去了的那几篇（`deskcore/poststyle.py`，纯函数，不调模型；只有一处按需读规则原文，见下）。全部只在工具回包、工具说明、`poststyle` 里，协议没改（`2d357c4b6075`，写手不用重导 skill）：协议 §5 已经说了一次最多 10 篇、`body` 只放正文（不放标记、不放话题行）、先改 `delivery.problems`、`next_step` 是给模型的指示。

| | 怎么认 | 门槛 | 回包 | 算不算要改 |
|---|---|---|---|---|
| 重复说法 | 同一小句里连着 8 个「字」一样（汉字一个字一个，英文 / 数字连着的一段一个，品牌叫法整个算一个）；一篇里出现几次只数一篇 | 至少 max(3, ⌈20%·n⌉) 篇（15 篇以内 3 篇，16～20 篇 4 篇）。按写手传的顺序，前 need−1 篇不算，第 need 篇起点名（改掉它们就不再超，和评论规则 B 一样）；这次带 `replaces_version_id` 改过重存的那几篇先算进不算的那几篇，要改的落到新稿上（改过的改一轮就够，见下）。每篇最多点名两处，引原话（最多 30 字）；挨着的两段前面用过的不是同几篇时各说各的，不并成一句 | `draft_checks` 里 `kind = "repeat"` | 要改 |
| 开头一样 | 第一小句的前 4 个字（不够 4 个就整句；第一小句不到 2 个字的那篇不算，「嗯，」这种） | 开头一样的最大那一组至少 max(5, ⌈50%·n⌉) 篇 | `batch_hints`（一句，点名哪几篇、引第一篇的原话；这句是要跟用户提的，所以写「第 1、2… 篇」、从 1 数，不写 `index`） | 只提示，不进 `draft_checks` |
| 版式残留 | `comment_block`：正文里带了【素人评论】【贴主回复】【作者回复】那一段；`label`：标题 / 正文里有【正文】【标题】【话题】【tag】（【tag】不分大小写）；`br`：字面的 `<br>`（不分大小写，`<br/>` `<br />` 也算）；`literal_newline`：字面的 `\n`（反斜杠加 n；反斜杠前面是英文 / 数字 / 半角标点、后面又紧跟英文字母的不算，`C:\new` `folder\new` 这种路径；前面是汉字、全角标点、emoji、空白或者在开头的一律算，「写完了\nVlog」也算；夹在话题里的也算，话题在那儿收住——不然 `\` 被当成话题里的字，话题一直吞到行尾，后面接英文的正文会被误报成没有正文）；`empty`：去掉话题、标记、评论段之后一个字都没有（有它就只报这一条） | 有就报；标记、`<br>`、`\n` 只看评论段前面那部分（评论段整段要删） | `draft_checks` | 要改 |

**量之前先去掉什么。** 话题标签（`validator.topic_tag_matches`，哪个 `#` 算话题只在那里认：连写、`[话题]`、网址里的 `#片段`、`C#` 和别处一样）、正文后面接着的评论段（从第一个评论标记起整段不量）、上面那几种标记、字面的 `<br>` 和 `\n`，都换成同样长的空格：位置不变，引原话照样从原文里取（品牌照原样写，不是记号），去掉的地方算断句。品牌叫法和评论那一侧同一套（`core._brand_aliases`：`projects.brand` + 交付模板的 `must_mention_any`，都没有退回项目名，一个字的叫法不用）。注意「字」是记号：一个英文词、一个品牌叫法各算一个，所以「8 个字」可能比 8 个汉字长。**项目规则原文里的说法不算重复**：规则要求照原样写的话术、售后和规格的说法，改了反而可能违规。原文是这个项目确认过的规则（hard + soft、本项目 + 通用，静音的不算，不过方向闸；`store.shared_memories(..., vectors=False)`），规则里连着出现的同一串就豁免；存了「必须带的话术」检查（`required_phrase`）的，检查里列的每一句也算原文（照 `rulecheck.spec_of` 读，老形状 `target` 也认）——规则原文常常只写「结尾带上售后那一句」，话术本身只在检查里，不算的话这边叫第 3 篇起换个说法，照改了 `rule_checks` 又报「必须改」；规则原文不截评论段（规则常常在讲交付的样子）。只在头一遍查出重复说法时才读（大多数批次一句都不重复，不为它多查一次库）；读失败这一次**不报重复说法**、原因记进指标——把规则要求的话术当重复叫人改，比漏报一句更糟。整个是空白的正文照旧由存档那道闸拒（W-30，`empty_body`，不存档、不进这一步），这里不再报一次；只有话题的正文这次没改成拒（那会改 `written` / `rejected` 和 CI 钉住的存档那一块），存档照常、这里报 `empty`。

**回包和下一步。** `draft_checks = [{index, kind, what}]`（有才带，按 `index` 排，同一篇的版式残留在前），`index` 是写手这次传的位置，被闸拒掉的那几篇不在里面、也不算进篇数。有交付模板、拼成了 `delivery` 的，【标题】【正文】已经在 `delivery.problems` 里报了，这里不再报。措辞跟着模板管什么走——只说写作台真会加的：

- 没模板：`body` 只放正文；评论写进这篇的 `comments`、标记删掉，交付时再按项目要的样子排；
- 模板拼评论（`delivery.comments` 不是 null）：评论写进 `comments`，标签和位置由写作台按模板加；模板不拼评论：只说这个模板不拼评论，正文里删掉这一段、要给的评论放在交付稿后面单独给用户——不说「写进 `comments`」，照那样挪过去评论就从 `delivery.text` 里没了，`problems` 也不会说；
- 【标题】【正文】由写作台按模板加；【话题】【tag】没有哪个模板会加：模板管话题（`delivery.wants_tags`）时说话题写在 `tags` 里、话题行由写作台加，标记删掉；不管话题时不报——那时话题行本来就照项目规则写在正文末尾（`delivery_requirements.task` 这么说），交付稿原样带上。`next_step` 接在规则检查那几句后面：

- `DRAFT_CHECKS_NEXT_STEP`：点名哪几篇，和合规检查、参考分分开算，不用等用户说、先改再交，和 `delivery.problems`、「必须改」放在同一轮，改过的带 `replaces_version_id` 先过 `check_drafts`、再调 `score_drafts`；
- `DRAFT_CHECKS_ONCE_NEXT_STEP`：这次带 `replaces_version_id` 改过重存的那几篇还查出来的（重复说法、版式残留都算），改一轮就够、照实说一句（W-42 同一个道理：几篇照同一个新说法改，下一次又撞上，来回改不完；本来就要那样写的字面 `\n` / `<br>`——讲代码的笔记——或者写手那边每次都转义坏了的换行，叫改也改不掉；和协议 §5「改过一轮还查出来的，照实说一句，不再接着改」、参考分那边的 `fixed_once` 同一个口径）。同一篇不会既在上一句里叫改、又在这一句里叫别改；
- `BATCH_HINTS_NEXT_STEP`：`batch_hints` 照那一句提一下，只是参考。

不拦存档，不碰 `judge`、`score`、`fixes`，「合规检查都过了」那一句照旧只看判定。这一块出任何错都不抛：两个键都不带（写了一半的摘掉），原因只进指标。`batch_metrics.meta.draft_checks` 只有数、没有稿子里的字：`{"drafts", "repeat": {"need", "grams", "exempted", "carriers", "flagged", "once"}（读规则原文失败时是 {"need", "grams", "error"}）, "format": {kind: 篇数}（一篇标题、正文里都有标记是两条，算一篇）, "once": 改过重存还查出来的篇数, "opener": {"need", "top", "hinted"}, "elapsed_ms"}`，整块出错时是 `{"error", "elapsed_ms"}`。时间算在 `phase_ms.delivery_rules` 里，`phase_ms` 的键不变；原型量过 20 篇 × 4,000 字约 47 毫秒，10 篇 × 800 字几毫秒。

**量过（11-10，只读 SQL，只看数）。**

- 门槛的取舍：跨度 6 会把免责声明、规格和用量的说法、常用短语都算进来，跨度 10 召回减半，取 8；协议一次最多 10 篇，两成只在超量的调用上起作用（87 个够格的批次里 14 个是 15～20 篇）。开头取 2 个字会撞上「周末」「今天」这种时间词，取 3 个字以上一批都没触发；「前 4 个字、不够就整句、至少 2 个字」照样认得出「救命」「姐妹们」这种开头。
- 重复说法：近 60 天 87 个够格的批次、679 篇里，10 批 / 73 篇用了同一个说法 / 第 3 篇起 45 篇；加上规则原文豁免是 9 / 51 / 27（约 4%）。10-21 之后没有。已发笔记的基线（按 10 篇一组算）是 2.8%。
- 开头一样：写作台 163 批、更早的 22 批，没有一批到门槛（只提示，本来就不常见）。
- 版式残留：近期 697 篇里 77 篇（11%），已发的 7,155 篇笔记里 0 篇；最后一次见到是 10-26。原因：有的项目的规则里写了交付的样子，写手的模型照着抄进了正文。

**不做的。** 评论那一侧的这几样不搬：品牌 / 中性 / 短评的配比（那是一组评论在帖子下面的样子，一篇笔记里提品牌是正常的）；不写「帖子里」、不用含糊的「那」指代（评论指着帖子里某一处说话才有的毛病，笔记正文自己就是帖子）；评论规则 A（6 个字和前面任何一条重复：笔记长，跨度 6 会把免责、规格说法都算进来）；评论规则 C 的「开头 2 个字一样最多 2 条」（会撞时间词，笔记这边改成 4 个字、只提示）。

**后续。**

- 产品 / 型号名当一个字：要项目在 `default_params` 里明确配一个列表。现成的 `isolation_terms` 放的是品类词（近 60 天有写作台批次的项目里只有 1 个配了，是几个两字左右的品类通用词），拿来遮会把普通词也遮掉，所以没用。
- 同一项目同一天的参照：写手把一批拆成几次调时，后面几次也和前面几次比（量过：用了重复说法的篇数按同一项目同一天算是 90，按单次调用算是 73）。
- 只有话题的正文要不要在存档那道闸（W-30）里直接拒。
- 在 `check_drafts`（存档前）就查重复说法。

回归：`tests/test_poststyle.py`（纯函数：8 字和 3 篇 / 两成的边界、一篇只数一次、品牌、话题、评论段、标记 / `<br>` / `\n` 断句、后面接英文的 `\n` 和话题里夹着的 `\n`、规则原文豁免、挨着的两段各说各的出处、改过的先算、开头的门槛和从 1 数的篇号、五样残留和模板拼不拼评论 / 管不管话题的几种措辞）、`tests/test_score_drafts_batch_checks.py`（从 `commit_drafts` 走：下标照写手传的、被拒的不算、规则原文只在要时读、`required_phrase` 检查里的那一句也豁免、读失败不报重复、有模板时标记只报一次、不拼评论 / 不管话题的模板、`format` 数篇数、只有话题的正文、改一轮就够（重复说法和版式残留、新旧混着的一次）、`batch_hints`、出错不带半截的键、时间算在 `delivery_rules`）；`tests/test_protocol_release_cleanup.py`（三句新话进旧说法扫描、改稿先过 `check_drafts`）、`tests/test_angle_block_score_report.py`（工具说明）、`tests/test_deskcore_ownership.py`（两个新的私有函数收 `project_id`，登记为只被已过闸的 `commit_drafts` 调）跟着加了几行。

### 3.22 写评论：不提帖子、不凑字数、常识不写成新发现，整组那一句说清是负责人定的（2026-11-11）

**为什么。** owner 11-11 对写评论又定了四件事，所有项目都管：

1. 整句里都不要说「帖子」怎么样，直接说那件事，「帖子」两个字不提（11-10 只管「帖子里」）；
2. 不为凑字数加话，字数不够可以；
3. 大家都知道的事不当成新发现来写；
4. 写手那边的模型遇到「代码检查」和项目规则说法不一样时，不该回来问负责人，写手在对话里就该能改。

第 4 件的起因：11-10 晚上一位写手的模型写了一组 20 条评论，把提品牌的上限读成「代码检查的看法」，和它记得的一条要每条都带品牌的旧规则对着放，按「硬约束优先」留了品牌；整组短评一条都没有，它说加短评会改变整批的口气，没有改；最后问用户要不要报给负责人。那条旧规则 11-10 已经由 owner 改掉，不在生效的规则里；它的老说法只剩在几条试用档的规则里（台账 `my_rules` 照列、标「试用」，不进简报，也不给写评论），或者是同一场对话前面拿到的旧回包。回包里没有一句说这些数是谁定的、比别的说法新，「不用问用户」也只在 `style_next_step` 里，只缺短评时整组那一句后面什么都不带。

全部只在工具回包、工具说明、`commentstyle` 的几句、简报末尾一句和规则检查的一句话里，协议没改（`2d357c4b6075`，写手不用重导 skill）。

| | owner 的要求 | 怎么做 | 范围 |
|---|---|---|---|
| Q1 | 评论里不提帖子 | `commentstyle._POST_REF` 放宽：帖子 / 帖文 / 贴子哪里都算；「这 / 本 / 此 / 原 / 该 / 上 / 前 / 那 / 楼主 / 博主 / 你 / 您」（中间可有 的 / 个 / 篇 / 条）接一个「帖」（这帖、本帖、原帖、楼主帖、你的帖、这篇帖，后面是药、膏、汤、剂、字、方子、中药的不算，「这本帖」的本不算）；帖里 / 帖中 / 帖内 / 帖下面（下方、下边、下评论）；发帖（子）；去掉「帖子」以后最可能换成的几样：这篇 / 本篇（后面接 里、中、说、写、提、讲、的）、这篇笔记 / 这条笔记、笔记里说 / 提 / 讲（「我笔记里写了」「的笔记里」不算）。引出来的那一段去掉后不剩半截：后面紧跟的「里 / 中 / 内」一起引（「帖子内容」的内不算），前面的「这个 / 你的 / 楼主的」一起引（「之前帖子」的前、「应该帖子」的该不引），发帖子整个引。请帖、字帖、碑帖、庚帖、名帖、喜帖、临帖、法帖、范帖、拓帖、妥帖、熨帖、服帖、贴片（「这贴」「贴了两晚」）、补贴子女、贴子母扣不算；帖当一剂药讲的（一帖下去、一帖中药）不算；楼主、帖主是称呼，不查。原贴 / 本贴 / 主贴 / 发贴这几个错字写法不查：贴片项目里「根本贴不住」「基本贴合」「按原贴的位置」都是常话。改法：「评论里不提帖子，直接说那件事（「帖子说的 X」「这帖的 X」→ 直接说 X）」。Jev 的修改单是在说评论（「接住帖子说到的事」），本身可以提帖子，可模型照着改容易写成「帖子说的 X」：转给写手时，fix / hints / thread 那几句里第一句提到帖子的后面补「（评论里不提帖子，直接说那件事）」（`POST_REF_NOTE`，Jev 题库不动）。`LINES`、`SHORT`、`COMMENT_STYLE_NEXT_STEP`、两个工具说明跟着改 | 所有项目，单独写评论和跟着笔记交付两条路都查 |
| Q2 | 不为凑字数加话 | 代码不查（短本来就行）。`commentstyle.PAD_LINE`「不为凑字数加话：一句能说完就一句，短也行；不复述前面说过的，不加总结句、感叹句撑长度」进 `comments_for.style`；`SHORT`（交付模板的 task）加「不为凑字数加话（短也行）」。把评论往长里推的几处压回去：`COMMENT_SCORE_NOTE` 加「照 fix、hints 改都不是要加长：补一件具体的事就够」；Jev 细节那道题没过时（经验位要「至少模糊」）那一句后面补「（点到一个情境或做法就够，不为这个加长）」（`DETAIL_NOTE`）；经验位的 note 加「一两句就够，不凑字数」；`comment_set` 短评那句原来的「经验位要接住帖子、太短过不了」改成「经验位那条接住帖子说的事，一两句就够」 | 所有项目 |
| Q3 | 常识不写成新发现 | 代码不查。`commentstyle.COMMON_LINE`「大家都知道的事不写成新发现：常识别用「原来…」「才知道…」「没想到…」「涨知识了」去接，直接说或者不说；自己真碰上的意外照常写」进 `comments_for.style`，`SHORT` 加「常识不写成新发现」。不做成检查的原因见下 | 所有项目 |
| Q4 | 写手在对话里就能改 | 见下 | 所有项目；配比那几句只有配了 `comment_set` 的项目 |

> **11-11 晚更新（§3.23）。** owner 把配比也开放了：写手在对话里可以用 `set_comment_mix` 改项目的四个数。下面引的几句里「是负责人给这个项目定的」现在是「这个项目定的配比」，「配比本身对话里改不了」换成「用户明说这个项目以后都按新的配比，就调 set_comment_mix」；别的一个字没动。这一节其余部分留着当时的原话，现在的原文以 §3.23 和 `tests/test_comment_rules_1111.py` 为准。

**Q4 怎么说。** 口径只有一个：提品牌、中性、短评的条数是负责人给这个项目定的；项目规则和之前见过的旧规则原文说法不一样的，以条数为准，不算冲突，直接改，不问用户，也不报给负责人；用户这次在对话里明说要别的配比（比如每条都带品牌），这一组就照用户的写，交付时一句话说明和项目定的配比不一样（`USER_MIX_NOTE`；协议「用户另有要求就照用户的」，owner 也问「她自己就能在对话中更改吗」），配比本身对话里改不了；用户觉得哪条项目规则过时了，当场用 `set_rule_state` 停掉。不说「最新」「比…都新」：配比没有日期，比不了谁新（复审 11-11：原来那几句压的是「前面对话里的说法」，连用户自己这次说的也压掉了）。放在模型一定会读到的几处：

- `score_comments`：整组那一句里有配比的数（提品牌、中性、短评，只有配了 `comment_set` 的项目才有）时，开头从「整组代码查: 」换成 `SET_LINE_HEAD`「整组代码查(条数是负责人给这个项目定的, 项目规则里说法不一样时以这里为准; 直接改, 不用问用户): 」；只有重复、品牌收尾这些所有项目都管的时是 `SET_LINE_HEAD_ALL`「整组代码查(所有项目都管; 直接改, 不用问用户): 」（都还是「整组代码查」四个字打头，工具说明和回包都按它认）。有配比的数才带 `set_next_step`，按这次真报了的几样拼（`_set_next_step`）：`SET_NEXT_STEP`（条数是负责人给这个项目定的，照点名的几条直接改，改完整组再判，不问用户、不报负责人）+ 提品牌或中性报了才接 `SET_BRAND_STEP`（规则里要每条带品牌、要几条夸或背书的以条数为准，不算冲突）+ 短评真不够才接 `SET_SHORT_STEP`（压短或换成一句话短评，这不算改口气）+ `USER_MIX_NOTE` + `RULE_SELF_SERVICE`。判成、没判成两条路都带；没配 `comment_set` 的项目不带（复审 11-11：原来只要有那一句就带，没配配比的项目也说「负责人给这个项目定的」、叫模型拿它压过要每条带品牌的规则，那几条的 `style_fix` 本来就有 `style_next_step`）。`COMMENT_SCORE_NOTE`（每个回包都有）加「style_fix 和 thread_fix 第一句也是必须改的，直接改，不用问用户；summary.passed 只数每条，整组 passed=false 时照 thread_fix 第一句改」：原来那句只把 Jev 的四样算必须改，`summary.passed` 可以是 20/20 而整组没过。
- `comments_for`：`comment_set.lines` 第一句是 `COMMENT_SET_HEAD`（这些数是负责人给这个项目定的配比，项目规则或之前见过的规则原文里要每条带品牌、要几条夸或背书的照这里写，品牌怎么称呼照项目规则，不问用户、不报负责人；后面接 `USER_MIX_NOTE`）；原来只有提品牌那句带「以这一句为准」，并进了第一句。`next_step` 末尾加 `COMMENTS_FOR_RULES_NOW`「规则以这次回的为准，前面对话里见过的规则原文和这次不一样的不算（可能已经停掉了）」（所有项目；规则每次现取，只取生效的；只说规则原文，不压用户这次说的），配了配比的再加 `COMMENT_SET_NEXT_STEP`（同一个口径，也带用户明说就照用户的）；那一格里已有的维护评论多一句「是按以前的规矩写的（提品牌多、短评少），只拿来避开重复，配比照 comment_set」——量过那一格里提品牌的行过半、15 字以内的不到一成，模型会照着学。有 `comment_rules` 或 `hard_rules` 时回 `rules_note`（`RULE_SELF_SERVICE`：用户说哪条项目规则过时了，当场用 `set_rule_state` 停掉，以后都不要用 retire、先停一阵用 mute，memory_id 在 `my_rules` 里，不用等负责人；要换新说法用 `record_rule`）。
- `my_rules`：回包加 `state_note`（试用、已停用的现在不生效，写稿、写评论都不用守，和生效中的规则或 `comment_set` 说法不一样也不算冲突；用户说哪条过时了当场停掉）；`actions` 里 retire 那句加「写评论也不再给」。
- 工具说明（不进协议的哈希）：`comments_for` 的 comment_rules / hard_rules / comment_set 三条和新加的一条（规则以这次回的为准、`rules_note`）；`score_comments` 的 `summary.passed`、`fix`、`thread_fix`、改一轮那几条；`my_rules` 的「试用」；`set_rule_state` 加一句写评论时也是当场改。

`comment_set` 的数本身是项目配置（`projects.default_params`，owner 用 SQL 配）；写这一节时写手改不了，11-11 晚起可以在对话里改（`set_comment_mix`，§3.23）。写手能自己改的还有项目规则（`record_rule` / `set_rule_state`，早就有，这次只是在写评论的回包里说出来）。

**笔记那一侧的字数（同一句「不为凑字数凑废话」）。** 两处服务端的话，不在协议里：

- 简报：显示出来的规则里有说到字数的（原文有「N 字」「字数」「篇幅」——阿拉伯数字或两个字以上的中文数；一个中文数字（「十个字」「两个字」）要挨着说长短的词才算（不超过 / 最多 / 至少 / 控制在…，以内 / 以上 / 左右 / 内…），「一个字都不能错」「品牌名四个字不拆开」「slogan 八个字原样用」是在说原样用，不算；「十字路口」「一字不改」「八字」不算——或者存了 `max_len` 检查；管评论的规则不算：原来只认「评论区」开头，2026-11-11 起按 `_is_comment_rule`，见 §3.24），那一层末尾补一句。硬约束说到就接在 P0：`LENGTH_RULE_TAIL_HARD`「规则里说到的字数照守; 差字数时只拿具体内容(经过、细节、数字)补, 不加空话、重复的解释或总结句」——P0 每一条都要守（协议），硬约束的下限 `check_rules` 照旧报「必须改」，不能在 P0 里说可以写短（复审 11-11）。只有软规则说到就接在 P1：`LENGTH_RULE_TAIL`「规则里说到的字数, 上限照守; 但字数不是要凑够的数: 不为凑字数加空话、重复的解释或总结句, 素材不够就写短一点, 交付时跟用户说一句差多少」。只接一次，放在贴型那句（`DIRECTION_RULE_TAIL`）前面。「上限照守」是故意的：最常见的是「标题不超过二十字」，这句不能读成上限也能松。
- 规则检查：字数没到下限的那一句后面加 `rulecheck.BELOW_MIN_HOW`「能补的是具体内容(经过、细节、数字), 不加空话凑字; 没有素材就照实告诉用户差多少」。级别不变：硬约束的下限照旧标「必须改」。

**量过（11-11，只读 SQL，只看数）。**

- Q1：原来的写法在 `truth_vault.comments` 10,605 条、运营写的维护评论 4,629 行里一条都不中；放宽之后中 11 条（0.10%）和 21 行（0.45%），都是真在说帖子（这条帖子，或泛泛的刷帖子）；「服帖」18 条一条不中；光一个「楼主」的 176 条不查。「贴子」「这贴」「帖药 / 帖膏」在评论里一次没出现。
- Q3：拿「原来 / 才知道 / 没想到 / 居然 / 竟然 / 涨知识 / 学到了」去找，人写的评论里中 1.9%、维护评论里中 2.7%；抽样看，真是「常识当新发现」的只有一成多，大半是说自己碰上的事、产品的效果出乎意料，或者「原来」是「以前」的意思。做成检查三条里错两条，只做提示也是叫人白改，所以只写进给模型看的那句。最干净的「涨知识」这一类 15,234 行里只有 4 行。

**不做的 / 留给 owner 的。**

- 用户在对话里明说要别的配比时：现在照协议「用户另有要求就照用户的」，这一组照用户的写、交付时说一句（`USER_MIX_NOTE`）。要不要让写手在对话里长期改配比（现在是 owner 的项目配置），由 owner 定。**已定（11-11 晚）：开放，见 §3.23。**
- 笔记要不要也「可以支持字数不够」：owner 那句是对评论说的；现在只在软规则（P1）那一句里说素材不够可以写短，硬约束的字数照守。
- 品牌名本身以「帖」结尾的项目（「示例帖」这种），「示例帖里」「示例帖中」还会被当成提帖子；要的话 `issues()` 先把项目的品牌叫法遮掉再查（两条路都要传叫法进去），现在没做。
- 写手停不掉试用档的规则：retire 是降回试用档，对试用档是空操作；`my_rules` 照列它们，`state_note` 说明不生效。要从台账里清掉，由 owner 跑 SQL。
- 硬约束里的字数下限要不要降成「建议改」：现在照旧「必须改」，只是改法里说清补具体内容、没有素材就照实说。
- 协议里写规则的例句有一句「…占八成」（说的是品牌叫法的比例），容易被读成「八成评论要带品牌」；协议冻结，只在 `comment_set` 第一句里说「品牌怎么称呼照项目规则」。
- Jev 的 `reader_value` 提示把品牌名算作可行动信息，会把模型往加品牌上推；那是只提示的题，没动。

回归：`tests/test_comment_rules_1111.py`（复审 11-11：没配 `comment_set` 时那一句的开头和不带 `set_next_step`、`set_next_step` 按报了的几样拼、新加的几句里没有「最新」「前面对话里的说法」、用户明说的配比照用户的、P0 用 `LENGTH_RULE_TAIL_HARD`、「一个字都不能错」这类不算字数、管评论的规则不补那一句；Q1 两条路、Jev 那几句只补一次、补的那句在截断之后；Q2 / Q3 两句在 `style` 里紧跟代码查的那几句、只给细节那道题补；整组那一句的原文、`set_next_step` 判成 / 没判成 / 没有那一句三种、`comment_set` 第一句和 next_step、`rules_note` 只在有规则时、停掉的规则下一次不给、`my_rules.state_note`、工具说明、新加的话里没有工程指令和「归档」；笔记那一侧：字数规则的认法、P0 / P1 只接一次、存了 `max_len` 也算、没有就不接、在贴型那句前面、下限那句的原文）；`tests/test_commentstyle_set_checks.py`（Q1 正反例各几十条，11-11 变异测试后每个前缀、中间字和排除字都有单独一条；复审加了这篇 / 笔记里、字帖子、去掉后不剩半截的引法，和帖当药、当字帖讲的反例、一条里几处都引出来、两句只给模型看）；跟着改的：`tests/test_comment_set_score.py`、`tests/test_comment_brand_endings.py`、`tests/test_deskcore_score_and_comments.py`、`tests/test_rulecheck.py`。

### 3.23 写手在对话里改项目的评论配比；钥匙对不上时 401 自己说清为什么（2026-11-11）

owner 11-11 晚又定了两件事。都不动协议（`2d357c4b6075`，写手不用重导 skill）：新工具的用法全在它自己的说明和别的工具的回包里。

#### (I) 配比也开放：`set_comment_mix`

**为什么。** §3.22 只做到「用户这次明说要别的配比，这一组照用户的写」，项目的数还得 owner 跑 SQL。owner：「配比也开放吧。」

**两条路分开说。** 写手那边的模型每次写评论都会读到（`USER_MIX_NOTE`，在 `comment_set.lines` 第一句、`set_next_step` 里；`COMMENT_SET_NEXT_STEP`；`comments_for` / `score_comments` / `my_rules` 的工具说明）：

- 用户只是**这一组**想要别的配比 → 照用户的写，交付时一句话说明和项目定的配比不一样，项目的数不动（和 §3.22 一样）；
- 用户明说**这个项目以后**都按新的配比 → 调 `set_comment_mix` 把项目的数改掉，不用找负责人；
- **只有用户明说才改。** 模型不为了让这一组通过自己去改配比，也不替用户决定要改；拿不准用户说的是哪一种，先问一句。这三句在工具说明的开头，`USER_MIX_NOTE` 的结尾也是这一句。
- **「以后都按新的配比」不走 `record_rule`**（复审 11-11）。协议正文（冻着）说「以后…」必须 `record_rule`；配比是例外：记成「评论区：」规则不生效，后面每次 `comments_for` / `score_comments` 都说「项目规则里说法不一样的以 comment_set 为准」，用户的话就被旧的数悄悄压掉了。工具说明不在协议哈希里，所以两头各说一句：`record_rule` 的说明在「什么时候调」后面加了「例外」（那几个数不记成规则，调 `set_comment_mix`），`set_comment_mix` 的「什么时候调」里加了「这一类不用 record_rule 记」。没调过 `comments_for` 的对话里（用户直接说「规则更新：以后提品牌最多七成」），模型能看到的只有这两段说明。

顺带改了口径里的一个词：原来几处说条数「是负责人给这个项目定的」，现在是「这个项目定的配比」（`SET_LINE_HEAD`、`SET_NEXT_STEP`、`COMMENT_SET_HEAD`、`COMMENT_SET_NEXT_STEP` 和两个工具说明）——数可能是写手照用户说的改过的，再说是负责人定的就不对了。「项目规则里说法不一样时以这里为准、不用问用户、不用报给负责人」没动。复审 11-11：`SET_LINE_HEAD` 里的「直接改」带上了宾语——「整组代码查(条数是这个项目定的配比, 项目规则里说法不一样时以这里为准; 照点名的几条直接改评论, 不用问用户): 」。前面的名词成了「配比」、又有了一个改配比的工具之后，光一句「直接改, 不用问用户」会被读成可以直接改配比，和「只有用户明说才改」正好相反；这是整组没过时模型看到的那一句。

**工具。** `set_comment_mix(project_id, mix)`，`mix` 里只放要改的键：

| 键 | 是什么 | 怎么写 |
|---|---|---|
| `brand_max` | 一组里提到品牌的最多几成 | 小数（0.5）、百分数（50）、带百分号的字（"50%"）都认；`1` 是 100%（要 1% 写 0.01 或 "1%"）。存的是整数百分比对应的小数（33.3 → 0.33）：代码本来就按整数百分比数，存的就是数的那个值 |
| `neutral_min` | 至少几成是中性评论 | 同上 |
| `short_min` | 至少几成是一句话短评 | 同上 |
| `short_chars` | 多少字以内算一句话短评 | 6～40 的整数（和 `_validate_comment_set` 同一个范围） |

- 七成写 0.7 或 70，**单写 7 是 7%**（5、10 这些本来就是合法的百分数，不拦；工具说明和报错里的 `MIX_KEYS_HELP` 都点了这一句，回的 `summary` 里是「→ 7%」）。
- 值写 `null` 是把这一项去掉（不再数）；`brand_max` 写 1、下限写 0 是这一样不限了。`brand_max + neutral_min` 不能超过 100%（中性评论是不提品牌的，这是读的那一侧本来就有的校验）：要把提品牌的上限放到 100%，同一次把 `neutral_min` 写 0 或 null，报错那一句也这么说。
- **列改不了。** `mix` 里带 `column`（哪怕和现在的一样、哪怕是 null）就报一句、什么都不写：那一列和 TV 数预埋评论的是同一列，换了那边就数不到了，仍由 owner 配。库里原来的列一个字不动。
- 项目还没有 `comment_set` 的，按这次给的数新建一份，不带列，也不替用户补别的数。四个数都去掉、项目也没配列时，`comment_set` 整个拿掉（空对象过不了校验，不留空壳）；配了列的留着列。
- **等于不数的那一项不出句子**（复审 11-11）。`_comment_set_lines` 里提品牌的上限 100%、下限 0% 的那一项不再拼「最多 100%(20 条最多 20 条)」「至少 0%…20 条至少 0 条」这种句子；一个在数的数都没有时（都去掉了，或都调成了不限）不带第一句「下面几个数是这个项目定的配比」，只剩列那一句，列也没配就没有 `comment_set`。这时 `set_comment_mix` 回一句 `comment_set_note`（这个项目现在不数配比了，所有项目都管的写法底线照旧），`next_step` 换成 `MIX_CLEARED_NEXT_STEP`（不往 `comment_set.lines` 指）；`comments_for` 不接 `COMMENT_SET_NEXT_STEP`。数照存，只是不出句子。
- **校验是读的时候那同一个**（`core._validate_comment_set`）：合并完整个过一遍，不过就不写。所以工具收下的数，下一次 `comments_for` / `score_comments` 不会回「配置不对，按没配处理」。这次给的四个数先各自查过（报哪个数不对、要怎么写）；合并之后还不过，就是库里原来就有、这里又改不了的那一项不对（列名、不认识的键），回一句请用户找负责人，不写（`MIX_EXISTING_BAD`）。
- **库里原来就不认的那个数**（复审 11-11）。这次带了它：重新设就修好了。「和现在一样不一样」按数比、不按念出来的样子比——库里写的是 `"12"`（读的时候不认）念出来也是「12 字」，原来被当成一样、留着没动，结果回的是「有一项不是这里能改的」；现在库里那个值自己过得了读的校验（`_mix_stored_ok`，就是拿它单独过 `_validate_comment_set`）而且数相等才算一样。这次没带它：点出是哪一个、现在是什么、要什么样的数，说清它这里能改（`MIX_EXISTING_NUMBER_BAD`：用户同意的话一起放进 `mix` 改掉，不然找负责人），不再说「不是这里能改的」。几成那三项库里写着 `null` 和没有这一项是一回事：去掉它不算改，不写、不留痕（原来会写一次、回一句空的「改了: 。」）。
- **字数那一项什么输入都只回那一句**（复审 11-11）。`"²"`、`"①"`（`str.isdigit()` 是真、`int()` 不认）、`inf` / `nan`（`int()` 直接抛）原来漏成 500，模型读到的是「服务坏了，重试」；现在只认 ASCII 数字写的字，先挡 nan / inf 再 `int()`。报错里念回的那个值最多 40 个字符。
- **MCP 那条路上不是有限的数**（复审 11-11）。mcp SDK 收到请求后会按 JSON 模式再过一遍，`NaN` / `Infinity` / `1e400` 在那一步变成 null，而 null 在这个工具里是「去掉这一项」：一个写坏的数会把一项检查关掉，REST 上同一个请求却是 400。到工具那一层已经分不出来，所以在 SDK 之前接住（`app._McpFiniteNumbers`，纯 ASGI，包在 `/mcp` 子应用外面）：只对 `tools/call set_comment_mix`，把请求体里不是有限的数换成它原来的字面（字符串），工具照「要是一个数, 现在是 '1e400'」拒掉；别的请求一个字节不动。真的 `null` 两条路都还是去掉那一项。客户端自己先把 NaN 序列化成了 null 的（JS 的 `JSON.stringify` 就是这样），服务端看到的就是 null，分不出来——回的 `summary` 会照实写「→ 没定(不数)」。
- **`default_params` 的形状不变**：仍是 JSON 字符串，别的键（`delivery` / `archived` / `isolation_terms` …）原样、顺序不变；本身解析不了就不写（同 `delivery_format`）。写完再读一次，以库里实际的值回报；对不上（刚好有人也在改）回 `changed=false` 和一句「请再调一次」。
- **读、改、写不在一个事务里**（`delivery-format` / `project-archive` 也一样）。这一列现在是写手的模型在对话里写，不只是负责人手敲的命令，所以多做了三件事（复审 11-11）：① 同一个项目的 `set_comment_mix` 在进程里排队（`core._mix_lock`，一个项目一把锁；线上是单进程，`railway.json` 的 uvicorn 没开 `--workers`）；② 排到了再读一次项目行，在最新的那份上合并，不拿归属校验时读的那份——原来甲读完还没写、乙改完了，甲拿旧的一份盖回去，乙的改动和留痕都没了，两边还都报改成了；③ 写完再读，配比要对得上、留痕最前面那条也要是这次的，有一样不对就照实说没改成（别的键不比：这次写完之后别人接着改了别的键，这次的改动还在）。**还剩的口子**：这次读和写之间（一次库往返）另一个**进程**写了这一列——负责人正好在跑 `delivery-format` / `project-archive`——那一次会被这次盖掉，两边都看不出来。要关上得让库按「读到的值没变才写」来写（带条件的 UPDATE 或一个 RPC），这一版没做；以后开多个 worker 之前必须先做。
- **留痕**在同一个 JSON 里：`comment_set_log`，新的在前，最多 20 条，每条 `{at, by, before, after}`。`at` 是 UTC 时间；`by` 是那把钥匙在 `DESKCORE_KEYS` 里的备注名（`name`），不是钥匙，也不是 `user_id`（D-136 之后大家都是团队身份那一个，分不出人）。时间和名字由工具那一层取、app 注入（`_caller_name`，和 `_user_id` 一样调用方传不进来），`core.set_comment_mix` 自己不看钟、不认人。和现在一样的（0.5 和 50 是同一个数）不写、不留痕。服务端日志里只有项目 id、`user_id` 前 8 位和改了哪几个键，没有名字。
- **回包**：`changed`、`before` / `after`（四个数，没定的是 null）、`summary`（「「示例项目」一组评论的配比改了: 提品牌的上限 50% → 60%; 一句话短评的下限 20% → 没定(不数)。」，叫模型原样告诉用户）、`comment_set`（改完之后的 `lines` 和 `column`，和 `comments_for` 回的是同一个函数拼的；没有句子也没配列时是 null）、`next_step`。
- 归属闸、认不出调用者、不包 `_safe`（写，失败要冒泡）、报的那几句收成 `InvalidInput`（REST 400 / MCP 工具错误原样回）都和别的写工具一样。`tests/test_protocol_tool.py` 原来要求每个工具都在协议里有名字，协议冻结期间加的工具进 `OUTSIDE_PROTOCOL`，条件是说明里自己写清什么时候调 / 不调，而且每次写评论都会拿到的那几句里点了它的名。

回归：`tests/test_set_comment_mix.py`（数怎么写、边界、列、同一个校验、`default_params` 的形状、留痕、说明里的那几句；复审 11-11：字数的怪输入、按数比、库里不认的数点名、排队和再读、写完核对留痕、等于不数的不出句子、`record_rule` 说明里的例外）、`tests/test_set_comment_mix_http.py`（REST / MCP / `/tools` / `/health`，署名是钥匙的备注名、调用方传的不算；复审 11-11：不是有限的数两条路都是那一句、真的 null 照旧、只改写 `set_comment_mix` 的请求体）、`tests/test_comment_rules_1111.py`（改过的几句原文）、`tests/test_deskcore_ownership.py`。

#### (II) 钥匙对不上：401 只说收到的那一串长什么样

**为什么。** 写内容的写手连不上时，客户端里只有一句 `401 invalid X-Deskcore-Key`，谁也说不出错在哪：贴了截短的示例（11-03 那次，§runbook「key 轮换」）、多了一层引号、把 `Bearer` 的写法填进了 `X-Deskcore-Key`，还是拿的是旧钥匙。

**现在 401 回什么。** REST（`/tool/…`、`/tools`）和 MCP（`/mcp`、`/mcp/`）是同一个中间件、同一份 JSON：

```json
{"detail": "invalid X-Deskcore-Key: 服务器不认识收到的这把钥匙(长度 9, 最后 4 位「li-…」; 带省略号、有非英文字符)。请对照发给你的那把钥匙, 在客户端的写作台连接设置(mcp.json)里把整把钥匙重新粘贴一遍: 不带空格、不带省略号, 钥匙外面只留一对引号; 然后重启客户端、开一个新对话。钥匙不要发到对话里。",
 "reason": "invalid_ellipsis",
 "key_shape": {"length": 9, "last4": "li-…", "whitespace_around": false, "whitespace_inside": false, "quotes": false,
               "ellipsis": true, "bearer_prefix": false, "non_ascii": true, "placeholder": false}}
```

- `detail` 开头照旧是 `invalid X-Deskcore-Key`（runbook 和排查的人按它认）。括号里：长度；最后 4 位（整串不到 8 位时不带，那等于把它原样念回去）；有哪几样就列哪几样——首尾带空格、中间有空格、带引号、带省略号（`…` / `...` / `⋯`）、开头多了 Bearer、有非英文字符、像示例里的占位（带尖括号、`your`、`xxx`、「你的」「示例」）；一样都没有就说「没看出空格、引号、省略号这类问题」。后面是固定的一句怎么办：先说贴在哪儿（客户端的写作台连接设置，`mcp.json`）再说怎么贴，最后一句「钥匙不要发到对话里」——这一句也会被对话里的模型转述，光说「重新粘贴一遍」容易变成把钥匙贴进对话（复审 11-11）。没带钥匙（`missing`）、`?key=` 的 `detail` 和原来一样，多一个 `reason`，没有 `key_shape`。
- **服务端配置写坏时**（`misconfigured`，复审 11-11）：`DESKCORE_KEYS` 解析不了时 401 回的是固定的一句（`identity.MISCONFIGURED_KEY_MAP`：配置解析不了、所有请求都会 401、哪里不对看 `/health` 的 `auth.note` 或服务端日志）。原来这一句把配置错在哪原样带出来，其中「值不是对象」那一种带着坏条目钥匙的前 6 位和个数——是 401 里唯一还在描述服务端钥匙的地方。`/health` 的 `auth.note` 现在只报第几条（「第 [2] 条的值不是对象」「第 [2, 3] 条缺 user_id」），不带钥匙的前几位。
- **只看发来的那一串。** `identity.key_shape(provided)` 的入参只有那一串：不说服务端配了几把，不和哪一把比像不像，不提示「前几位对了」。同一串发过来，服务端配的是哪几把，回的那一句一个字不变（有回归钉着）。发来的那一串最多念回最后 4 位。
- 请求头到服务器是按 latin-1 解的，客户端按 UTF-8 发的「…」会变成三个怪字符：量之前先还原，长度和最后 4 位才和写手配置里看到的对得上。
- MCP 客户端怎么显示这份响应体各家不一样：TypeScript SDK 那一类把响应体原样贴在报错里（所以中文不转成 `\u`、`detail` 放在最前面）；带 OAuth 的客户端（Claude Code）收到 401 会先去走登录流程、不显示响应体，那时用 runbook 里那条 `curl` 看。

**只去首尾空白。** 钥匙走 HTTP 头，头的值首尾的空白按规范不算值的一部分（空格和 tab 多数服务器到这里之前就去掉了；不间断空格、全角空格它们不管，复制粘贴时最容易带上）。`resolve` 先按原样查，查不到再按去掉首尾空白的查（直接是那个字符的、按 UTF-8 发过来被解成怪字符的，各一份）：去掉之后**整把**对上了，说明对方手里就是这把，放行不多给任何人东西；原样的先查，原来能过的一个不少；去完是空的不查。**去完之后整串是可见 ASCII 才拿去查**（`identity._sendable`，复审 11-11）：还原 UTF-8 那一步只为去掉首尾的空白，本身不能多放行任何东西。不加这一条，`DESKCORE_KEYS` 里一把带「…」的钥匙（11-03 那种把截短的示例原样贴进去的）就从「客户端发不出来、一直 401」变成「谁照着示例发谁就过」——那一串不是秘密，而 `/health` 还在说「这几个人会一直 401」。UTF-8 里 0x80 以上的字节还原不出 ASCII，所以加了这一条之后多出来的两份和原样相比只可能少了首尾的空白；原样那一份的行为和改之前一模一样。中间的空格、引号、省略号、多出来的 `Bearer` 一律不替对方改——那是在替对方猜钥匙——照实 401 并说出来。

**`/health` 多一块 `auth_rejections`**（不带鉴权，所以只有个数和时间）：

```json
"auth_rejections": {"since_start": {"missing": 0, "invalid": 2, "invalid_whitespace": 0, "invalid_quotes": 1,
                                    "invalid_ellipsis": 3, "invalid_bearer_prefix": 0, "query_param": 0,
                                    "misconfigured": 0, "other": 0},
                    "last_at": "2026-11-11T08:00:00+00:00"}
```

原因是固定的九个（`identity.AUTH_REJECT_REASONS`），对不上的钥匙有几样毛病时记最可能是病根的那个（多出来的 Bearer → 引号 → 省略号 → 空格 → 都没有记 `invalid`）。里面没有钥匙的任何一部分（连最后 4 位也没有）、没有路径、没有是谁；`/health` / `/ready` 自己不算。进程重启清零。带 OAuth 的客户端被拒之后会去探 `/.well-known/…`，那几次没带钥匙，记在 `missing` 里。

**日志**：每拒一次一行 WARNING，Railway 日志里搜 `auth rejected`：

```
auth rejected: reason=invalid_ellipsis path=/mcp/ via=header len=9 flags=ellipsis,non_ascii
```

有原因、路径（不带查询串）、钥匙是从哪儿传的（header / bearer / query / none）、长度和那几个是 / 否；**没有钥匙，也没有最后 4 位**（日志留得比一次 401 久，看得到的人也多）。

**没动的。** `/health` 的 `auth.note` 配好时原来就回「N key(s)」（runbook 的轮换步骤按它核对条数），这次没动；配坏时回的是坏条目的位置（原来有两种回的是钥匙的前 6 位，复审 11-11 改成了位置）。新加的那一块和 401 里都没有这些。`DESKCORE_KEYS` 里一把以 ASCII 的 `...` 结尾的钥匙，客户端是发得出来的，照着发原来就能过、现在也能过（`/health` 一直把它标成不可用）——这次没动，要拦得另定「被标成不可用的条目一律不放行」。

回归：`tests/test_auth_rejection_diagnostics.py`（每一项怎么判、那一句原文、只看发来的那一串、去首尾空白和不替对方改的几样、原因和计数、日志那一行；复审 11-11：配置里带「…」的钥匙哪种写法都对不上、配置写坏时固定的一句和只报位置）、`tests/test_auth_rejection_http.py`（REST 和 MCP 两条路同一份、`/health` 的计数里没有钥匙、日志；复审 11-11 的两条也各走了一遍 HTTP）。运维怎么用见 runbook「连不上时按这个顺序查」。

### 3.24 写手说过的评论规矩，下一组评论拿得到；评论位的说法不再招来被拒的调用（2026-11-11，审计 C1 / F5 / C7）

不动协议（`2d357c4b6075`，写手不用重导 skill），不加迁移，不改库里任何一行。改的是 `core.py` 里几个判据和给模型看的几句话。

#### (I) 哪些规则算管评论的

**为什么。** 协议叫模型把管评论的规矩记成「评论区：」开头，`comments_for` 也只认这个开头。可 11-03 以来记下的 3 条规则全是 11-11
在写评论的对话里记的，没有一条带这个开头：四分钟后的 `comments_for` 回的 `comment_rules` 是空的，写手刚说过的话下一组评论照样犯。
另有一条以「【评论区…】」开头的建议级规则，因为多了一个括号，也一直没给过写评论的人。

**现在的判据**（`core._is_comment_rule`，三样有一样就算）：

1. 原文去掉开头的空白和**一个**开括号之后以「评论区」开头（原来就认的那一种）；
2. 开头那一句——到第一个标点或换行为止，空格不算标点——的前 12 个字里有「评论」；
3. 存的代码检查（`rule_kind` / `rule_payload`）的 `where` 里有 `comments`。

说交付版式、过了开头那一句才提到评论的规则（「【交付格式…】…」这一类）不算，写评论时照旧不给。

用在两处：

- **`comments_for`**（`_comment_rules`）：认出来的进 `comment_rules`，其余的硬约束进 `hard_rules`。一条硬的评论规则原来在
  `hard_rules`，现在在 `comment_rules`——两个列表都是「必须守」，合起来一条不少、也不重复，写评论的人拿到的东西只多不少。
  `score_comments` 不读规则（整组那几样只看品牌叫法和 `comment_set`，判定服务也不收规则原文），不受影响。
- **简报的字数那一句**（`_mentions_length`，§3.22）：管评论的规则说到字数，不给笔记补那一句——判据从「评论区」开头换成同一个
  `_is_comment_rule`。

**笔记那一侧不动。** 这些规则照旧在简报的 P0 / P1 里：硬约束不能因为「看起来是说评论的」就从写笔记的人眼前拿走（跟着笔记
一起交付的评论也归它管）。

**核对（11-11 对生产库只读查了一遍）。** 生效的规则 233 条，原来按开头认出 4 条。新判据多认出 10 条：

| 项目（id 前 8 位） | 多给写评论的建议级规则 | 从 `hard_rules` 换到 `comment_rules` 的硬约束 |
|---|---|---|
| `64366f9a` | 3（11-11 记的那三条；其中一条是靠第 3 样认出来的） | 0 |
| `a14f15a4` | 1（开头多一个括号的那条） | 2 |
| `0d6f50df` | 0 | 3 |
| `89d3e7aa` | 0 | 1 |

多认出来的每一条说的都是评论（其中一条硬约束说的是评论文本交出去时的样子，一条是笔记和评论两边都管的）。原文里提到评论、
没被认出来的 35 条里，33 条是过了第 13 个字才提到的，2 条是「评论」前面先有一个标点（先说了别的部位、后面才提到评论）：
开头标着交付格式 / 交付排版的 8 条一条没进，其余是说笔记正文怎么写、顺带提到评论的。

#### (II) `record_rule`：当场说这条写评论时给不给；说评论的检查只查评论

- 回包多一个 **`given_when_writing_comments`**（true / false）：按库里实际那一行算，判据就是上面那一个。模型在写评论的对话里
  记完规矩，当场就知道下一组评论拿不拿得到，不用等用户下次发现没生效。工具说明里一句：管评论的规矩 content 开头写「评论区：」就给。
- **开头那一句只说评论的规则，`check` 的 `where` 存成 `["comments"]`**（`_comment_only_where`），回包带一句 `check_where_note`
  （`COMMENT_CHECK_NOTE`：只查评论、不拿它查标题和正文；标题、正文也要这样守就另记一条说正文的规则）。11-11 那三条里有一条的
  检查存的是 `["body", "comments"]`：一条说评论的规矩，每次存档都拿去查笔记正文，近 7 天那个项目 74 版笔记里 13 版会因为它
  收到一条和笔记无关的「建议改」。没写 `where`（默认标题 + 正文）的也一样收窄。
- **不收窄的**：开头那一句里也点到正文 / 标题 / 笔记 / 文案 / 帖 / 话题 / 标签 / 全文 / 开头 / 结尾 / 文末 / 封面 / 配图 / 引导的
  （`_POST_WORDS`）——那是两边都管的规则，或者说的是笔记里怎么引导评论，收窄会悄悄少查正文；靠第 3 样才算评论规则的（原文开头没说评论）；`max_len`（只认标题 / 正文）；撤掉检查的 `free_text`。
- **库里已经存着的那一条不改库**，读的时候按同一个判据算（`_comment_check_rule`）：`score_drafts` 的 `rule_checks` 不再拿它查正文，
  跟着笔记一起交付的评论照查；`my_rules` 的 `check` 给的是查的时候真用的那一份，和库里存的不一样时多一句 `check_note`。
  AW 生成链路的 `validator` 读的还是库里那一行（它只看硬约束，那一条是建议级，碰不到）。

#### (III) 评论位：四种评论，不是四条

**为什么。** 定了配比的项目一组是 20 到 50 条，评论位只有四个。`comments_for` 的 `next_step` 原来不管哪个项目都说「按 slots 每位
写一条…每条评论带 slot 的 id」。模型给 20 条各挂一个 id → 同一个 id 重复，`score_comments` 整个调用 400，报错还叫它「分几次判，
或者给它们不同的 id」：分几次判，整组的条数（提品牌、中性、短评）就数不准；另编一个 id 又是一个 400。能用的那条路（多出来的
不带 slot）只在短评那一句里带了半句。这两种 400 都在读库之前，日志里看不见，所以次数没法数；改之前在测试里照真实的工具路径
把两种都跑了出来（`tests/test_comment_rule_reach.py` 第 6 段）。

**改了三处给模型看的话**（判定逻辑一行没动）：

- `comments_for.next_step`，**只在项目有在数的配比时**（`_comment_set_counts`；审计时是 3 个项目）：开头换成 `COMMENTS_FOR_SET_SLOTS`
  「slots 是四种评论…不是只写四条: 一组不止四条时, 每个 slot 的 id 整组最多用一次, 其余的评论不带 slot」，结尾换成
  `COMMENTS_FOR_SET_SCORE`「写完把整组放在一次 score_comments 里交(不按 slot 拆成几次; 一次最多 20 条, 超过的按顺序分段判、
  前面判过的放进 earlier_comments…)」。没定配比的项目那一句一个字不动。
- 两种报错：slot id 重复的说「每个 id 留一条, 其余几条去掉 slot, 整组再送一次(不用分几次判, 也不用另编 id)」，并点出是哪几个
  id 重复；id 不认识的在原来那句后面加「不属于这几种的评论不带 slot 就行, 去掉它的 slot、整组再送一次」。
- 整组参考（`thread_hints`）：一组里有评论不带 slot 时，判定服务回的是一句固定的话后面跟题目的 id。转给写手之前换成人话
  （`THREAD_HINT_NAMES`），开头标「整组参考(只是参考, 不用为它重写)」。只说看到了什么（夸品牌的占了多数、几条是同一个句式、
  没有审慎的声音、整体像安排出来的），不说该怎么改：这四道题没验收过，说成改法模型会照着重写、多出几轮。不认识的 id 原样留着。

工具说明（不进协议的哈希）跟着改了 `comments_for` 的 comment_rules / slots 两条、`score_comments` 的 `comments` 那一段、
`record_rule` 和 `my_rules` 各一两句。

**没做的。** 不给重复的 id 另造评论位（那样每条都带位，判定服务会按位出修改单，改的东西更多）；不改 `COMMENT_SCORE_NOTE`、
每条一行的回包和配比那几句提醒（11-11 白天刚按 owner 的话定下来）。

回归：`tests/test_comment_rule_reach.py`（判据的正反例——照生产里见过的开头形状造的合成规则，交付版式的几种都是反例；
`comments_for` 两个列表不少不重；简报里规则还在、字数那一句不补；`record_rule` 的字段和收窄、不收窄的几种；库里存着的那种
`score_drafts` 不查正文、`my_rules` 给真用的那一份；`next_step` 两种项目；两种 slot 报错和照着做一次判成；整组参考换成人话）。
`tests/test_deskcore_score_and_comments.py` 里钉着整组参考原话的那一条跟着改了。

### 3.25 简报里的标题字数：发出去的标题大多不超过 20 字的项目，多说一句（2026-11-11，审计 A2 / A4 / 标题字数那一条）

不动协议（`2d357c4b6075`，写手不用重导 skill），不加迁移，不多一次查库，不多一轮改稿。只是 `open_project` 简报里
`recent_titles.task` 末尾在够条件时多一句话。

**为什么。** `tv_edit_diffs`（`diff_rev ≥ 2`，326 对）里，写作台稿标题超过 20 字的 98 篇有 65 篇发出去之前标题被改，20 字以内的
228 篇只有 48 篇。另一个口径（09-15 起发布的 142 对）里，超长标题被改的 55 篇有 52 篇改完落在 20 字以内，改过的 89 篇里
24 篇只改了一个过长的标题。这件事原来只在存档之后才说得出来：存了 `max_len` 标题检查的规则由 `check_rules` 报（全局那条是软规则，协议叫模型转告用户、不自己改），
写之前没人说。

**怎么做。** `core.build_writing_brief` 挂 `recent_titles` 的地方：这一栏里 `published` 的标题够 10 条
（`TITLE_LEN_MIN_TITLES`）、其中至少 80%（`TITLE_LEN_SHARE_PCT`，整数比较，不四舍五入）不超过 20 字（`TITLE_LEN_CAP`）时，
`task` 后面接 `TITLE_LEN_TAIL`：「published 里的标题大多不超过 20 字（不算空白和话题）：新写的稿子标题也写在 20 字以内。
这一句只管标题，正文篇幅不受它影响；规则或用户对标题另有要求的，照规则和用户的。」

- **固定 20，不按项目算分位数。** 分位数（有的项目 p90 是 22）会把 21–22 字那一档放过去，被改得最多的正是那一档。
- **按哪些标题算。** 就是这一栏已经拿到的 `published`：近 30 天发出去的里最近的 12–20 条（写过的那栏占掉几条，它就少几条），
  去重、每条截到 40 字之后的那一份——模型自己也看得见这几条。不是近 30 天的全部，所以卡在八成上下的项目，这一句可能这一场有、
  下一场没有；它只是一句参照，有没有都不拦任何东西。`drafted`、`working` 不算。
- **数法**和 `max_len` 标题检查同一个（`rulecheck._count`，即 `validator.count_chars`：去掉空白和 #话题 再数），同一条标题两边
  不会一个说超了一个说没超。平台自己怎么数（英文、数字、表情）没核实过，先跟现有的检查一致。
- **不说的时候一个字不多**：发出去的标题不够 10 条（新项目、`tv-sync` 还没对上、TV 读不到），或者这个项目发出去的标题本来就
  常超过 20 字。
- **只是参照，不是检查。** 不进 `draft_checks`、不进 `check_rules`，不会为它多一轮改稿重存；`recent_titles.task` 原有的优先级
  照旧（p0 / stable、发牌坐标、用户点名的都在这一栏之上，改稿不受它约束）。和简报里字数那一句（§3.22，`LENGTH_RULE_TAIL`
  「上限照守」）同一个方向；项目规则对标题另有上限的（不管是不是 20），这一句自己写明照规则。只说标题：10-20 撤过几条
  「写短一点」的全局规则（正文被写得太短），这一句写明正文篇幅不受它影响。

**没做的。** 不在 `score_drafts` 的 `draft_checks` 里加「标题超长」一行：那条路每篇被点到的稿都要多走一轮改稿重存，而且
同一轮里 `check_rules` 会把同一个标题按软规则报成「转告用户、别自己改」，两句话打架。也不动改稿摘要（`edit_digest`）。

**怎么看有没有用（两周后，只读）。** 两个数：写作台稿每周标题超过 20 字的占比（上线前约 15%）；`tv_edit_diffs` 里按稿子标题
长度分的标题被改率。超过 20 字的占比还在 15% 上下的话，再考虑加检查，那时要和已有的 `max_len` 标题规则去重。注意 §3.22 的
字数那一句同一天上线，前后对比不干净。

回归：`tests/test_desk_variety.py`（够 10 条且八成才说、正好八成算、19 条里 15 条不算；写过的标题不凑数；数法和 `max_len`
标题检查逐条对得上；说和不说发给库的请求一条不差；措辞不叫模型去查、去改存过的稿、去问用户；和存了标题字数检查的硬规则并存）。
`tests/test_protocol_release_cleanup.py` 把 `TITLE_LEN_TAIL` 收进「服务端的话不说回旧口径」那一组。

### 3.26 借卡：`borrow_lessons` 也限时，并到 `open_project` 起的那次选卡上（2026-11-11，审计 A3；F-04 / AU-F09 写作台这一半）

**为什么。** `open_project` 随简报借卡最多等 15 秒，到点先交简报，叫模型动笔之前用同样的参数调一次 `borrow_lessons`。
可是 `borrow_lessons` 原来直接调借阅体，等满 `LIBRARIAN_TIMEOUT_SEC`（60 秒），客户端约 22 秒就断；它也不知道
`open_project` 那次选卡还在后台跑，又向馆员发一次，馆员那头第一次还没写缓存，第二次就再跑一遍模型选卡。
`truth_vault.flywheel_librarian_cache.select_ms`：10-20 以来 31 次冷选卡，14 次超过 15 秒；11-05 以来 5 次超过 22 秒
（28.7 / 60.4 / 46.1 / 45.2 / 22.7 秒）。F-04 / AU-F09 写的触发条件是「两周内第二次冷借超过 22 秒」，早就到了。
能确认没拿到卡的只有 11-09 一批（3 篇）；更常见的是补借那一次被客户端掐断，或者白跑第二次选卡。

**现在。** 协议没动（`timeout` 时「只调这一次，还没有就照常写」原来就写着），写手和 owner 都不用做事。

1. **`borrow_lessons` 最多等 `BORROW_LESSONS_WAIT_SEC`**（默认 18 秒，夹在 1–20，§4.1 的表）。走的是 `open_project` 用的那一层
   `core._borrow_for_brief_bounded`，多了三个参数：`arm=`（`borrow_lessons` 已经定好的臂，沿用模型传回的那个；这一层不再发
   一次臂）、`carry=`（同一场接着借，记规律条数时和前面那次取大的，§3.12）、`followup=`（到点时回哪一句、指标里记哪个名字）。
   分臂和规律条数那套账还是原来那两笔、原来的顺序；只多一种情形：到点没等到 = 这次交出去 0 条 / 0 张（后台之后选完的不算，
   模型没看到）。原来等满 60 秒时，客户端早断了，选完的规律条数却照样记成「给出去了」。
2. **到点回一句新的话**（`core.BORROW_STILL_SELECTING_NOTE`）：`status` 还是 `timeout`，`detail` 是「卡还在选，这次没等到。
   照常写就行，不用再调 borrow_lessons，也别跟用户说没有可借的经验。」简报超时那句（`BORROW_TIMEOUT_NOTE`）是叫模型去调
   `borrow_lessons` 的，放在 `borrow_lessons` 自己的回包里模型会再调一轮，所以不能原样用。
3. **正在跑的选卡记在进程里**（`core._borrows_running`）：键是（项目，发给馆员那份 brief 的 sha256，臂），值是那次选卡的
   future。同一个键再来的（`borrow_lessons` 跟在 `open_project` 后面、同一份简报开了两次）不新起，等同一个 future，
   各拿一份拷贝。
   - 键里有臂：探索臂那次选卡不带规律，两个臂不能并到一起。键里没有身份：同一个项目、同一份 brief、同一个臂选出来的是同
     一批卡（TV 的缓存键也不分人）；谁并上去，账都记在他自己的（项目，身份）下，不动别人的。
   - 选卡跑完（成了、抛了都算）在它自己线程的 `finally` 里摘掉，先摘、后交结果。等的人到点只是不等了，不取消、不摘：
     选卡照样跑完，TV 照样写缓存，这期间再来的人还并得上。线程起不来的不登记；brief 拼不出来的不合并。
   - 借阅体承诺不抛；真抛了，等它的每个人各自按 `error` 回，不往外抛。
   - 起跑超过 `LIBRARIAN_TIMEOUT_SEC` 再加 10 秒还没回来的那一条当成卡死的线程，不再往上并，另起一次、把表里那条换掉。
4. **存档那一行指标**（§3.8 的 `flywheel_lessons`）：`borrow_lessons` 到点没等到的那次记成 `followup_timeout`（只在这一行；
   回包里是 `timeout`）。`statuses` 里同时有 `timeout` 和 `followup_timeout` 的批次 = 简报没等到、补借也没等到。

**没做的、要知道的。**
- 合并只在这一个进程里有效（deskcore 单进程）。重启就没了，退回原来的样子：再发一次，多半命中 TV 的缓存。TV 那头的同款合并、
  重试和超时是 AU-F09 的另一半，在 TV 仓库做。
- 并上去的那次不向馆员发请求，TV 的 `flywheel_librarian_cache.last_hit_at` 也就不动。拿那一列等于 `created_at` 来推
  「卡没被再读过」会更不准；看卡到没到模型手里，用 `flywheel_lessons`。
- 选卡要 45 秒以上的那几场，补借照样等不到（18 秒）。区别是模型拿到一句正常的「照常写」，不是一次被掐断的调用，也不再多跑
  一次选卡。没卡的场数不会因此变少，§3.8 那条查询的 `no_cards` / `followup_timed_out` 看得出有多少。
- 预热（夜里或定时按项目先选一遍）没做：预热的行要带 `consumer = deskcore` 才命中，通道 2 那盏灯就再也看不出
  `open_project` 的借卡断没断；走现成的借阅体还会往发臂记录里写一条没人写稿的臂。

回归：`tests/test_borrow_lessons_deadline.py`（到点回那句话、选卡不取消；上限的默认值和夹子；并到 `open_project` 那次上只发
一次请求；两个人同时等各拿各的拷贝；臂 / brief / 项目不同的不并；等的人走了之后选完会摘、不改任何记录；选卡抛了每个人按
`error` 回并且摘掉；线程起不来不登记；晚到的收尾只摘自己那条；卡死的不并；显式的臂不再发一次；到点的补借和前面取大的；
并到别人那次上不动别人的账；存档那一行的 `followup_timeout`）。`tests/test_open_project_borrow_budget.py` 里原来钉
「`borrow_lessons` 等满 `LIBRARIAN_TIMEOUT_SEC`」的那条改成了「不被 `open_project` 的预算截断」。`tests/conftest.py`
每条用例从一张空表开始。

### 3.27 查重取候选先走四字串的索引（2026-11-11，W-21 / AU-F08，`019`）

**为什么。** 一组 10 篇在历史多的项目里存档要等十秒上下，其中六到七成花在两条查重 RPC 上（§3.8 的 `commit_parts_ms`：
`gate` 和 `fingerprints`），而同一条 RPC 在 `check_drafts` 里还要先跑一遍。慢在取候选的那一步：
`WHERE f.project_id = _project_id AND f.ngram_hashes && ng`，计划器按项目索引取出整个项目的行，再逐行比 `&&`，
四字串的 GIN 索引（`draft_fp_ngram_gin_idx`，`001` 起就在）从来轮不上。耗时跟项目的历史条数和稿子的 hash 数一起涨。

**现在。** `migrations/019_dedup_gin_first.sql` 只换 `deskcore_check_drafts` 和 `deskcore_commit_fingerprints` 里的这一步：

```sql
WITH cand AS MATERIALIZED (
    SELECT f.title, f.ngram_hashes AS hs, f.project_id AS pid
      FROM autowriter.draft_fingerprints f
     WHERE f.ngram_hashes && (SELECT ng)
)
...  FROM cand c ...  WHERE c.pid = _project_id AND array_length(c.hs, 1) IS NOT NULL
```

还是那三个条件，只是先按四字串从全库取命中的行、再筛项目，所以候选集不变，查重的结论不变。判定公式、阈值、签名、
返回列、授权都没动，Python 一行没改；跑之前和跑之后写作台都照常用，写手和模型看不出区别，只是等得短。两个记号缺一不可：

- `MATERIALIZED`：不让项目条件推回 CTE 里。推回去就又是按项目索引取全项目、逐行比。
- `(SELECT ng)`：把数组的值对计划器藏起来。plpgsql 里每条连接头 5 次执行用定制计划，直接写 `ng` 的话计划器按实际数组
  估行数（估成三分之一以上的行会命中），选全表顺序扫描，一篇要 3 秒以上。拿 `OFFSET 0` 当栅栏的写法同样走到顺序扫描，不能用。

**量过的（11-11，生产库只读；新写法是把这一步照抄成普通 SQL 跑的）。** 全库 7,782 条指纹，最大的项目 1,163 条。

| | 原来 | 新写法 |
|---|---|---|
| 89 个 hash 的一篇，取候选这一步（最大的项目） | 366 毫秒，读 1,163 行留 11 行 | 8 毫秒，同样 11 行 |
| 400 个 hash 的一篇（最大的项目） | 整条函数 1,560 毫秒 | 取候选 33 毫秒 |
| 最大的项目最近 10 篇一起查 | 3,677 毫秒 | 341 毫秒 |
| 只有 2 条指纹的项目，217 个 hash 的一篇 | 整条函数 6 毫秒 | 取候选 21 毫秒 |
| 数组直接写 `ng`（没有 `(SELECT …)`），89 个 hash | — | 顺序扫描，3,225 毫秒 |

10 组（一篇的 sketch × 一个项目）逐行比过候选集，也比过线上函数的输出和新写法的输出（`best_j` / `j_title` / `best_c` /
`c_title` / `c_sample`），全部相同：大项目、有一百多行空数组的项目、只有两三行的项目、整个项目都是空数组的、sketch 自己
是空数组的、拿别的项目的稿子来查的。本地 PostgreSQL 16 上另用 9,162 行合成指纹把新旧两版函数各调了 200 篇
（check 全列、commit 的 status / collided_with / detail），逐行相同；同一条连接里连调 8 次（头 5 次是定制计划，之后是
通用计划）取候选都走 GIN，新连接的第 1 次调用和后面一样快。

**没做的、要知道的。**
- **要 owner 在 SQL Editor 跑一次 `019`**（两条 `CREATE OR REPLACE FUNCTION`，不碰数据）。没跑之前一切照旧，只是没变快。
  跑完怎么核、怎么退都写在迁移文件末尾，runbook §1.15 也有。
- **小项目会慢一点。** GIN 的钱跟全库命中的行数走，不跟项目走：二三十条指纹以下的项目这一步原来是 1 毫秒级，现在每篇
  10~20 毫秒（10 篇一组三次 RPC 合计多不到 1 秒）。全库涨到几万行时要再看一次这一步。
- **计划不在 CI 里断言。** 计划器走不走 GIN 还看表的大小：CI 的夹具只有几行，它对小表本来就选顺序扫描，写法对不对都
  一样。CI 钉的是写法（两个记号都在代码里、019 相对 `008` / `005` 只换了这一步）和结果（逐例对 Python，外加「别的
  项目里的同一篇不算」）；计划在生产库上跑完之后核一次。生产库今天的成本是 GIN 206 对顺序扫描 627。
- 入库那条 RPC 里逐条循环取最大值时，分数完全相同的两条历史稿报哪一条的标题取决于行的读出顺序，原来也没有排序；
  判拦不拦不受影响。`check_drafts` 那条有明确的排序，不受影响。
- commit 段里每篇六次串行读写（`mint` / `consume` / `snapshots`）没动，那是另一件事。

回归：`tests/test_dedup_gin_first.py`（两个记号在 `019` 和基线的两条函数里各出现一次、在同一个 CTE 里、项目条件在读
`cand` 的那一层；没有 `OFFSET 0`、没有直接写 `ng` 的 `&&`；`CREATE` 到 `AS $$` 与 `008` / `005` 逐字节相同；把 `019` 的
那一步换回去等于上一版的代码；文件里除了两条函数和它们的授权没有别的语句；校验和回滚都在文件里）。
`tests/test_baseline_parity.py` 的登记改成 `019`。`tests/test_migration_doctor.py`：`019` 报 `unprobeable`、交出查两个
记号的 SQL、不让报告变红、不为它多调一次 RPC。`tests/sql_parity_check.py`（CI 的真 PostgreSQL）：两个记号在最终的
函数体里；别的项目里的同一篇 check 不算、commit 不拦。

### 3.28 两个读数：牌面到没到稿子和帖子里，对不上的帖是不是在别处写的（2026-11-11，审计 F1 / F4）

都是给负责人和工程看的读数。写手那边一个字没变：工具回包、`next_step`、协议都没动，判定、补录、发牌也都没动。

**一、`draw-report` 多三项：牌面对不对得上。** 发牌靠牌面的坐标（内容形式、情绪杠杆、标题句式）改变写出来的东西，
效果数据也是通过抽牌的权重回到写作里的。可原来的报表只有写了 / 发了 / 留着 / 撞车，没有一个数说牌面的坐标到没到
稿子和帖子里。11-11 手工连了一次（牌 → 销账的版本 → 对上的 TV 笔记 → TV 的标签）：112 篇发出去的帖里，牌面的内容
形式和标签一样的 16 篇（14%，碰巧一样是 17%），杠杆一样的 24 篇（21%，碰巧 8%）。这个数以后报表自己出。

| 项 | 比的是什么 | 什么时候读得到 |
|---|---|---|
| `a_form` | 牌面的内容形式 = TV 给发出去那一篇打的 `content_format` | 销账的那一版对上了 TV 的笔记（同 M2 的对照），标签在词表里 |
| `a_lever` | 牌面的情绪杠杆 = TV 给发出去那一篇打的 `emotional_lever` | 同上 |
| `a_title` | 牌面的标题句式是不是「疑问句」= Jev 对销账那一版答的「标题是不是问句」 | 销了账、Jev 答过就有，**不用等发出去** |

- 读不到的记 `None`、不进分母（词表外的值、TV 还没打标签、Jev 没答、没销账），不是 0。这三项不等 D+8：发出去才有标签、
  销了账才有答案，没有的本来就不在分母里。
- **chance（碰巧）怎么算。** 每个比例旁边带一个 chance：同一组牌里，假如牌面和结果互不相干，两边碰巧一样的比例。
  只看这一组里这一项读得到的牌，数出牌面各取值的占比 p(v) 和结果各取值的占比 q(v)，chance = Σ p(v)·q(v)。
  牌面六种形式各占 1/6 时，不管结果怎么分布都是 1/6；结果九成是同一种、牌面三成是那一种时接近三成。比例比 chance
  高出来的那一截才是牌面起的作用。报表末尾有一句同样的说明（`drawreport.CHANCE_NOTE`）。
- **怎么读。** 标签和答案都是模型跑一遍给的，重跑一致不一致没测过：看比例比 chance 高多少、看几周的走向，别看单周；
  一组只有几张牌时 chance 自己也不稳。杠杆本来就只是个松的引子（W-45），高出一截就算到了；内容形式和标题句式是硬坐标。
  发牌 v2 发出去的样本今天只有 6 篇，`a_form` / `a_lever` 还要几周才读得出；`a_title` 现在就能用（v2 销了账的 81 张）。
- **在哪儿。** `table`（来源 × 项目 × 周）每行多这三项；`by_source` 每个来源多这三项；新的
  `adherence.by_source_week` 是来源 × 周（各项目并在一起，三项都读不到的组不列）。文字版在来源那张表下面多一块，
  每个来源一行「全部」、下面每周一行，格子是「对上 / 读得到的（比例，碰巧 chance）」；一项都读不到就不印这一块。
- **多读了什么。** `store.report_note_outcomes` 多取 `emotional_lever`（`content_format` 本来就取了）；
  `store.report_title_answers` 读销账版本在 `title_is_question` 上的答案，和简报的 `recent_structure` 同一个读法
  （只认 Jev 的行、题目版本要对得上、同一题几行取最后抽的），只取答案列、不取 `evidence`。两样读挂了报表照出，
  CLI 说一句是哪几列这次不算。
- 没做的：审计里提过「标题句式和牌面不一样时在 `score_drafts` 里提一句」。那会让稿子多改一轮，等这个读数出来再说。

**二、对不上的帖也记一个分数（`tv_note_links.score`）。** 接住率低有两种原因，以前分不出：帖是拿写作台的稿子改的
（改的地方值得看），还是根本在别处写的（版式、规则怎么调都动不了它）。11-11 的只读核对：三个项目 545 篇没接住的帖里
542 篇和写作台任何一稿的四字串重合不到 15%，是后一种。可 `unmatched` / `ingested` 行的 `score` 一律是 NULL（生产库
3129 行），每次要回答这个问题都得手工重算。

- `tvlink.match_note` 对不上时把它本来就算过的最高包含度带出来（`Match.score`）：帖的四字串有多少在时间窗里最像的
  那一版里。时间窗里一版都没有是 0.0；帖太短（不到 15 个四字串）没得比，留 `None`。只是借了开头的那一版（F02）
  照样不连，但算进这个分数——它说的是帖的文字有多少出自写作台，不是能不能连上，所以可以高过 0.30。
- 只查过重、没存档的稿子也是写作台的稿子：`tvlink.match_snapshot` 对不上时同样带上最高包含度，`tv-sync` 取两边
  看过的最大值（`miss_score`）。对上了底稿、这晚没转成版本而记 `unmatched` 的，分数是和那份底稿的包含度。
- `tv-sync` 把它记进这晚写的 `unmatched` / `ingested` 行的 `score`（保留三位小数）。没正文的行照旧 NULL。
  对上了的几档、`ambiguous` 的 `score` 不变；`tv-resolve --ingest` 人工补录的那一行照旧 NULL。
- **只是读数。** 哪一条走补录、连到哪一版、报表里的计数都不变；没有任何地方按这个分数做判断（读 `score` 的只有
  F04 那一处，它只看 `title_exact`）。
- **没有迁移。** `score` 列 `009` 就有，`REAL`、可空（11-11 在生产库的 `information_schema` 核过）。
- **老行。** `ingested` 是终态、不重对，以前补录的行不会回头补分数，NULL 读作「没算过」。没有补录目标的项目
  （W-10）以前记的 `unmatched` 没有分数：头一晚各重写一次把分数补上，之后分数没变的照旧不重写（`_same_miss_score`
  按三位小数比，库里读回来的单精度尾巴不算变了）。有补录目标的项目里还挂着的 `unmatched` 本来就每晚重写。
- **怎么读**（TV 那边的灯按这个口径读，写作台不读它）：不到 0.15 是在别处写的，0.30 以上是拿写作台的稿子改的，
  NULL 是没算过或没得比。查数见 runbook §1.5。

回归：`tests/test_draw_report.py`（三项各自什么时候是 `None`；chance 等于两边占比的乘积之和、只用读得到的牌；
`table` / `by_source` / `adherence.by_source_week` 和文字版都带比例和 chance；CLI 多读的两样不取正文和 `evidence`、
读挂了说哪几列不算；`report_title_answers` 的读法）。`tests/test_tv_link_miss_score.py`（对不上带最高包含度、太短是
`None`、空窗口是 0.0、借了开头的也算；底稿那一路同样；`ingested` / `unmatched` 行记分数而判定和补录不变；W-10 的老行
重写一次之后不再写；从没存档的底稿改出来的帖按底稿记分）。原有用例一条没改期望。

## 4. 接入

### 4.1 部署

独立 service（跟 `worker.py` 同级），config 指 `deskcore/railway.json`。

env：

| 变量 | 必需 | 说明 |
|---|---|---|
| `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` | ✅ | service_role，绕 RLS |
| `DESKCORE_KEYS` | 生产必需 | `{"k-xxx": {"user_id": "<团队身份的 uuid>", "name": "Ziao"}}`。钥匙一人一把，`user_id` 全填团队身份（2026-11-02 D-136）；加写手就加一行，见 runbook「加一个写手」 |
| `GOOGLE_API_KEY` | 强烈建议 | embedding。不设则查重降级为纯确定性 |
| `LIBRARIAN_URL` / `LIBRARIAN_API_KEY` | 可选 | 借爆款经验卡；不设则 `open_project` 的 `lessons` 与 `borrow_lessons` 都返回空，`status` 为 `not_configured`，服务日志记 WARN。馆员回 200 但 `status` 是 `degraded` / `error`（TV 侧选卡失败）记 `error` 不记 `empty` |
| `OPEN_PROJECT_BORROW_SEC` | 可选 | 默认 `15`（2026-10-30 起，原 10；最近冷借 14–18 秒），夹在 1–20。`open_project` 随简报借卡**最多等这么久**，而且不超过整次回包目标 `OPEN_PROJECT_RESPONSE_BUDGET_SEC`（20 秒，不开 env）扣掉借阅之前已用的时间和借阅之后几步的预留（`core.OPEN_PROJECT_TAIL_RESERVE_SEC`，2.5 秒）剩下的那段；到点先交简报（`lessons_status.status=timeout`，`detail` 里叫模型动笔之前用同样的参数调一次 `borrow_lessons`，命中缓存即回；只调一次，还没有就照常写）；借阅线程继续跑完，TV 那边照样写缓存。MCP 客户端只容忍 ~22 秒，超过的是整份简报连 P0 一起丢。`borrow_lessons` 工具有自己的等待上限（下一行；2026-11-11 之前等满 `LIBRARIAN_TIMEOUT_SEC`），2026-10-30 起也收 `extra_instructions`（它在馆员的缓存键里） |
| `BORROW_LESSONS_WAIT_SEC` | 可选 | 默认 `18`（2026-11-11），夹在 1–20。`borrow_lessons` **最多等这么久**：同一个（项目、brief、臂）的选卡还在跑（多半是 `open_project` 起的那次）就等它，不向馆员发第二次；到点回 `status=timeout`，`detail` 是「卡还在选，照常写，不用再调」，选卡不取消、选完照样进 TV 的缓存。馆员那次请求自己的超时还是 `LIBRARIAN_TIMEOUT_SEC`（默认 60），不跟着改（§3.26） |
| `JUDGE_URL` / `JUDGE_API_KEY` | 可选 | 入库判定（§3.8，影子期）。key 发在 `X-Judge-Key` 头里；它是自定义格式，登记在 `logger_utils` 的值级脱敏名单里（judge 的错误体会被抄进 detail / 遥测 / `batch_metrics`）。不设则 `commit_drafts` 照常，返回里每篇 `judge_status=not_configured`，一个请求都不发、一次库都不多查。⚠️ 设了也要项目接上 `tv_project_map` 才会发（出境口径要 TV 项目号） |
| `JUDGE_TIMEOUT_SEC` | 可选 | 默认 `8`，夹在 1–15（写坏了、`nan` / `inf` 都退回默认）。**整批判定的共同截止**，不是每篇各 8 秒；入库慢时实际截止更短（整次 commit 回包目标 20 秒，见 §3.8，J-08） |
| `JUDGE_MAX_WORKERS` | 可选 | 默认 `8`，夹在 1–16（同上，写坏了退回默认）。一次 commit 同时发几篇 |
| `RULE_COMPILE_TIMEOUT_SEC` | 可选 | 默认 `420`，夹在 60–900。规则编译（§3.11）一次请求的读超时；Jev 最坏约 361 秒，所以至少 400。只有后台线程和 CLI 用它 |
| `RULE_COMPILE_BATCH` | 可选 | 默认 `60`，夹在 1–60（Jev 的上限）。一次编几条 |
| `RULE_COMPILE_AUTO` | 可选 | 默认开。`0` 关掉 `open_project` / `record_rule` / `check_rules` 触发的后台补编，只剩 `cli compile-rules`。上线补编（`compile-rules --all`）那一阵要先设 `0`，跑完再去掉：CLI 和服务进程的后台补编不共用锁，同时编会付两遍钱（§3.11） |
| `RULE_SWEEP_AUTO` | 可选 | 默认开。`0` 关掉存档之后的规则补查（给这个项目还没有规则题答案的旧稿在后台补一轮 `check_rules`，每次存档最多 20 版，只花 Jev 规则题的钱，写手看不到；见「规则检查的后台补查」）。改稿的后台复查不受它管 |
| `DESKCORE_TOOL_CALL_LOG` | 可选 | 默认开。`0` 关掉「每次工具调用留一行只有数的记录」（§3.8 末尾）：一行不写，别的都不变 |
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
`015_rule_compiles.sql` / `016_tv_notes_skip_vanished.sql` / `017_tv_link_text_fp.sql` /
`018_draft_snapshots.sql` / `019_dedup_gin_first.sql`
**十九个，按编号顺序跑，别跳号**（建议先在
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

- `016_tv_notes_skip_vanished.sql`（2026-10-26，I-22）：`deskcore_tv_notes` 不再返回飞书里已删的篇（TV 的 `truth_vault.v_notes_vanished`，D-103），§3.7 表里「飞书已删」那一行。只换函数体，签名不变；视图不在就照旧全给。**静默错答**，和 `008` 同类：不跑不报错，已删的篇每晚照样被读回来重对，项目有补录目标时对不上的会被补录成副本。所以**先跑 016，再打开有已删篇的 TV 项目的补录目标**（10-26：BJS_phase1 挪走了 79 篇成人藻油，其中 14 篇是 unmatched）。`doctor` 报 `unprobeable` 并给出查函数体和视图的 SQL。
- `019_dedup_gin_first.sql`（2026-11-11，W-21 / AU-F08）：`deskcore_check_drafts` 和 `deskcore_commit_fingerprints` 取候选先走四字串的 GIN 索引、再筛项目（§3.27）。只换函数体里这一步，签名、返回列、结论都不变，不碰数据。**不跑不坏，只是慢**（千行的项目每篇约 0.4 秒，10 篇一组三处各等 3~5 秒）；代码不依赖它，先于或后于部署都行。`doctor` 探不到函数体，报 `unprobeable` 并给出查两个记号的 SQL；跑完用文件末尾那句 `EXPLAIN` 核一次。
- `018_draft_snapshots.sql`（2026-10-29，W-38）：`draft_snapshots`，`check_drafts` 查过的每篇稿子（标题、正文、`angle_key`、查重结论、飞轮臂），§3.7 表里「查重底稿」那一行。只授 `service_role`、开 RLS、不建 policy（存的是稿子全文），留 60 天。**可降级，但先跑 018 再部署**：代码先上线时 `check_drafts` 照常判（记底稿那一步失败只记 WARN），tv-sync 跳过底稿那一步、照旧补录；可同一次部署已经不再催模型存档，那段时间只查了重的稿子发出去会被补录成副本（终态）。`doctor` 探这张表。
- `017_tv_link_text_fp.sql`（2026-10-27，W-34）：`tv_note_links.tv_text_fp`（TV 正文的指纹）+ `deskcore_tv_clear_lineage`（撤回写作台自己写进 TV 的血缘：两列都还等于传进来那一对才清，返回清了的 `note_id`），§3.7 表里「正文换了」那一行。**硬失败、不写错数据**：代码先上线的话 tv-sync 读对照选不到新列，每个 TV 项目都失败、退出码 1、一行不写——所以**先跑 017 再部署**。`doctor` 探列和 RPC。
- `015_rule_compiles.sql`（2026-10-20，D-121）：`rule_compiles`，规则编译结果的缓存，键 (`memory_id`, `sha`)（§3.11）。只授 `service_role`、开 RLS。**可降级**：不跑的话 `compile-rules` 和后台补编在读缓存这一步就报 error（记 WARN，一条都不发给 Jev，不花编译的钱），`check_rules` 回 `status: error`、记 ERROR 日志（写手那边是这次规则没查成、不能说守住了，存档已成功、交付不受影响；不是 500，10-20 第三轮复审：原来直接抛，REST 是 500、MCP 是工具错误，服务端一行日志都没有），简报和存档不受影响。`doctor` 探这张表。
- `014_tv_edit_diff_rev.sql`（2026-10-20）：`tv_edit_diffs.diff_rev`，差异的口径版本。首晚 125 条"改过"只差稿子末尾的 #话题 行（TV 正文不带），`edit_diff` 改成两边先去话题、品牌只数正文；旧口径的行 tv-sync 重算（不推 `computed_at`），`edit_digest` 只读当前口径。以后改差异算法只需 `tvlink.DIFF_REV` +1。`DIFF_REV = 2`（10-20）：写作台那版晚于发布的（`lag_days < 0`，抄回来的）不算差异，同 desk_gap / 判据 C 的"接住"口径。写作台那版只有标题、没有正文的（W-36，10-27：HATHERINE 59 条 title_exact 连的就是这种稿）也不算差异——没有正文可比，算出来是"整篇都是新加的"；以前按当前口径算过的 13 条，tv-sync 下一晚降回旧口径。
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
> ③ **事后提醒**：`open_project` / `list_projects` 回包里的 `desk_gap`——你名下哪个项目近 7 天发出去的帖写作台没接住（口径同 TV 角度台账灯判据 C：按发布时间、对上只认写作台那版早于发布的、项目用过写作台就算），原话告诉运营下次在连着写作台的对话里写、先说「打开某某」。漏接的那些对话自己连不到写作台，只能在下一次连着的对话里说。**2026-10-30 发布收尾起不再带**：一个团队共用一个身份，每个写手每场开头都听到别人项目的这段提醒，还推断「写的时候没调起写作台」；可对不回来的多半是 W-38 之前写的（那时查重不记稿子），正在写作台里写的人还被叫「这次就在写作台里写」。计算 `core.desk_gaps` 留着、不进回包；看漏接走 TV 角度台账灯判据 C。
> 新品牌上线时记得把品牌名加进 `description`（改的是 skill 头，`sync-skill` 后版本号会变）。owner 10-19 说 WorkBuddy 现在会自动更新从仓库装的 skill；没更新到的那份，开场 `get_protocol` 核版本时模型会提醒运营重新导入——两条路都在，但描述里的新品牌名只有更新到了才生效，上新品牌时顺口跟运营确认一句。
>
> **"按本地那份继续"只在 `open_project` 通的时候成立（2026-10-19）。** 正文进了 skill 之后多了一种失效：写作台的 MCP 没连上（地址 / key 不对），模型照样拿到整份协议，原来那句「核不上版本就按本地这份继续、不要停下来不写」让它把整套流程走完，稿子写得和真用了写作台一模一样，服务端却一条记录都没有。（途鸽 9-16 之后写作台没收到任何调用，最初怀疑是这一种；后来确认连接是全局的、原因是 skill 没被调起来，见上一段。这条硬闸照样留着，堵的是真断线的那种。）现在 skill 头和协议「一、动笔前」第 1 条都写死：`open_project`（写评论时是 `comments_for`）找不到、或者重试一次还失败，就不写，原话告诉用户去查连接。判据是真去调，不是看眼前的工具列表——按需加载工具的客户端里，连上了列表里也可能一开始看不到它。服务端这边由 TV 的角度台账那盏灯补一种形状（项目在发帖、写作台却没接住），见 TV `scripts/check_angle_ledger_leak.py`。

> ⚠️ **鉴权头的退路**：WorkBuddy 的 HTTP MCP 能不能配自定义 header，官方更新日志只说了支持 HTTP MCP 和 OAuth（v4.7.3），没有权威文档。所以 key 收两种传法，**优先级不同**：
>
> 1. `X-Deskcore-Key` header —— 首选
> 2. `Authorization: Bearer` —— 同样安全，平台不认自定义头时用
>
> 原来还收第三种 `?key=` 查询参数，**2026-10-23 起不收了（W-09）**：查询串会进日志。CI 里那次冒烟测试的日志就原样打出了 `POST /tool/list_projects?key=k-good`——换成真 key，它会落进 Railway 的访问日志、中间代理日志、以及任何转发 URL 的地方，而且事后删不掉。`/health.key_transport` 从 10-16 起计数，两个部署里 `query` 都是 0（10-18 header 2,291；10-20 部署到 10-23 header 2,904），没有客户端在用。现在带 `?key=`、又没有 header 的请求直接 401，`detail` 写明要改用 header；`key_transport.query` 仍然计数，数的是被拒的请求，不是 0 就说明有人还在试，找到是谁、帮他改成 header。
>
> **这一步必须最先验**——协议层不通的话整个形态要换。

Claude Code：`claude mcp add --transport http deskcore <url>/mcp --header "X-Deskcore-Key: k-xxx"`。CodeBuddy 同理。

钥匙要贴**整把**：上面两处的 `k-xxx` 是占位，不带省略号、不带多余的引号和空格，`X-Deskcore-Key` 里不写 `Bearer`。贴错了服务器回的 401 会说收到的那一串长什么样（§3.23 (II)）；改完要重启客户端、开一个新对话。

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

**10-21 审查补充。** `set_rule_state` 把项目规则升成生效、恢复停用规则或修改生效规则的方向后，立即触发后台补编并绕过冷却。排队的判定任务在开始请求时重算整次剩余时间，截止后不再发送。缓存中形状不对、题号重复或缺少题目列表的规则列入 `rules.questions_unchecked` / `rules.questions_unchecked_rules`；幸存题目即使都通过，也不能声称整条规则守住。`doctor` 对规则缓存表的权限缺失指向迁移 015。CI 在安装全部接口依赖后重跑整个测试目录。
