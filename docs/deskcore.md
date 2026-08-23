# deskcore · 写作台内核外置为 MCP

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

## 3. 工具面（11 个）

| 阶段 | 工具 | 说明 |
|---|---|---|
| 写稿前 | `list_projects` | 项目清单 + 各自的规则数/指纹数 |
| | `open_project` | **一次拿全**写作简报：stable / p0 / p1 / tactics |
| | `draw_angles` | 发牌：n 组互不重复、避开台账的坐标，带可直接贴的 `prompt_block` |
| | `borrow_lessons` | 转调 TV 馆员，借真实爆款经验卡 |
| 写稿后 | `check_drafts` | **硬闸**：全量历史 + 本批内互比 |
| | `commit_drafts` | 定稿入库：写指纹 + 给坐标销账 |
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

> ⚠️ **不需要 `ANTHROPIC_API_KEY` / `DESKCORE_MODEL`。** deskcore 一次 LLM 调用都没有——调校笔记的蒸馏交给调用方模型做（`record_edit` 出材料 → 模型提炼 → `save_my_style` 写回）。
>
> 这不只是省一个 key。方案自己的原则就是「推理归 WorkBuddy，MCP 只做轻量数据操作」，而服务端自己调 LLM 违反了它，还多一个中转站故障点。顺带消掉了一整类故障：上一轮刚踩过「`/health` 回显的模型名和实际调用的不同源，于是配错永远看不见」——**没有模型可配，就没有配错的余地**。

### 4.1.1 部署两步，缺一不可

**① 跑迁移** `migrations/001_deskcore.sql`（建议先在 Supabase branch 库跑 + `get_advisors` 核验再进 prod）。

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

向量的取法：`versions.embedding` 存的就是**标题**向量（`app.py:520` 只对 title 取向量），与 `draft_fingerprints.title_embedding` 同义，所以历史行有向量就**直接复用**，只有缺的才补算——几千条重算既慢又费钱。返回值里 `reused_embeddings` / `computed_embeddings` / `missing_embeddings` 分开报。

⚠️ **没配 `GOOGLE_API_KEY` 时也能回填**，但那批行没有标题向量，只参与确定性查重。补配之后重跑**不会**给已写入的行补向量（幂等是按 `version_id` 跳过的）——要补得先把这些行删掉再跑。所以顺序上**先配好 embedding 再回填**。

> 这个函数曾经**根本不存在**。CLI 子命令、本文档、PR 描述、连"部署必跑一次"的措辞都写好了，唯独没写函数体，`deskcore.cli backfill` 每次都 `AttributeError` —— 也就是回填这件事从头到尾没发生过，而所有文字都写着它已经有了。`py_compile` 抓不到（属性错误是运行期），所以 CI 现在有一步反射 `cli.py` 里所有 `core.xxx(` 调用、逐个断言存在。

### 4.2 自测

```bash
python -m deskcore.cli selftest      # 不连库不联网, 只用标准库
python -m deskcore.cli projects      # 需要 SUPABASE_* env
python -m deskcore.cli draw --project <uuid> -n 20 --block

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

skill 放 `~/.workbuddy/skills/bywood-writing-desk/SKILL.md`（本仓 `skills/` 下直接复制）。

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

## 5. ⚠️ 三个已经踩过的坑，别再踩

**1. `AW_DISABLE_ST_CACHE`（R-042）。** deskcore 是 headless 进程，但 streamlit 在同一份 requirements 里装着，`db.py` 的缓存 shim 会走真 `st.cache_data` —— 跨进程缓存无法被 app 的 `.clear()` 失效，用户在 UI 改完记忆后 deskcore 会拿 30-60s 的旧数据。`deskcore/__init__.py` 已经在 import db 之前 setdefault 这个 env（同 `worker.py:56`）。**别在 `__init__` 之前 import db。**

**2. pgvector 反序列化（R-034）。** PostgREST 对 `vector(768)` 列的 JSON 序列化是**字符串** `"[0.1,...]"`，不是数组。不归一的话 `dedup.cosine_similarity` 因长度不等**静默返回 0.0** —— 查重变哑弹，一条都抓不到，而且不报错。所有读 embedding 的地方必须过 `db._parse_pgvector`（`store.py` 已经在读取边界统一处理）。

**3. 鉴权配坏了必须 fail closed。** `DESKCORE_KEYS` 的 JSON 写错时，早期实现会返回空 map → `resolve()` 判定为"没配鉴权" → **放行所有请求**。生产上一个逗号写错就等于把项目数据和全部写工具匿名开放。现在显式配了就必须当成"打算开鉴权"，解析失败一律 401，`/health` 的 `auth.ok` 会是 false。

**4. `user_id` 必须用库里已有的 UUID。** 不要新造。TV `autowriter-migrations/RUNBOOK.md:150-153` 记过：写了 service account 的 UUID 导致 RLS 屏蔽、`list_example_items` 永远 0 行、飞轮静默断开，查了很久。配 `DESKCORE_KEYS` 时从 `projects.owner_id` / `items.user_id` 里查出来抄。

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
3. **embedding 依赖 `GOOGLE_API_KEY`。** 存量 768 维向量都是 Gemini `text-embedding-004` 产的，换模型会让历史向量全部作废需重算。没有它时查重降级为纯确定性——仍能抓开头撞车和四字串重合（`selftest` 证明了这点），但同角度换说法的标题会漏。
4. **查重目前在 Python 里逐对比。** `store.fingerprints` 有 4000 行上限。单项目到十万行量级时该改成 pgvector 服务端检索（`draft_fingerprints` 已建 ivfflat 索引，改起来不难）。commit 侧的原子重查已经在 SQL 里了，可以参照。

5. **commit 的原子重查只覆盖确定性信号。** 开头精确 + 四字串 Jaccard 在锁内查；标题语义相似度没查（要 pgvector 距离算子，且历史行可能没向量）。也就是说竞态窗口里"标题换个说法的同角度稿"仍可能两条都进。要覆盖它得把向量比对也搬进 RPC——等 backfill 把历史向量补齐之后再做更合适。
6. **"个人风格私有"与团队协作的张力。** 同一项目两个人各自驯化，风格会分叉。指纹库共享（互相避重），调校笔记不共享。跑一段时间如果分叉太严重，可能需要"把我的调校笔记提升为项目基线"的操作。第一期不做。
7. **native 正例的 essence 不可测。** 运营手标的正例没有 `external_source_id`，join 不到 `truth_vault.notes`，拿不到 `emotional_lever`，所以 TV 的饱和度监控对它们只能报"无法评估"。想让它可测需要给这些正例补 essence 标注。
