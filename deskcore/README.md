# deskcore · 写作台内核

内容工作台的能力内核，做成 MCP 工具服务。**Streamlit 界面【计划】停用（TV D-041），库里的积累一条不迁。**
停服**尚未执行**——前置的归档链路取舍见 runbook §3「停 Streamlit 会把 aw → TV 这条链路断掉」与待办 P1 #5，选定之前不要停。

完整设计见 [`docs/deskcore.md`](../docs/deskcore.md)；**上线手册 / 对接 / 待办 / 已知不一致**见 [`docs/deskcore-runbook.md`](../docs/deskcore-runbook.md)。决策在 truth-vault：`DECISIONS.md` D-041 / `docs/10-sister-repo-followups.md` R-034。

> **本服务已部署**：Railway，`https://autowriter-production.up.railway.app`（2026-08-26 首次上线）。
> **库里各表的实时行数不要照抄本文件或 runbook 的快照**——那些是写下那天的点位，会漂。要当前状态就跑
> `python -m deskcore.cli doctor [--project <uuid>]` 查库。上线 / 回填 / 对接步骤看 runbook §2。

## 它解决什么

| 症状 | 机制 |
|---|---|
| WorkBuddy 老忘记之前定的规则 | `open_project` 每次生成前重新注入 P0 硬约束层，不依赖对话记忆 |
| 写出来像 AI 不像本人 | `record_edit` 收手动精修 diff（最高权重信号），蒸馏成个人调校笔记 |
| 跨批次越写越像 | `draw_angles` 发牌台账 + `check_drafts` 全量成稿指纹硬闸 |
| 正例池是 recency top-5，模仿最近 5 条 → 被标 positive → 窗口滚动，语感越收越窄 | 正例改按**相关性**选取（`core.py` 新增四样之三，断掉趋同回路）；`borrow_lessons` 另从 TV 飞轮图书馆借真实爆款经验卡 |
| 一个项目 5 个提示词要点 5 次 | `open_project` 一次拿全 |
| 运营端那份 SKILL.md 改了没人知道，模型照旧协议写 | 协议正文进技能文件（系统提示，整场对话都在模型眼前）；`get_protocol` 只核对版本，本地是旧版时才下发新正文并提醒重新导入。09-10 曾只留引线、正文每次由工具下发——09-11 起入库率从 110% 掉到 22%，34 KB 的返回值在写完十几篇之后已经离得太远，模型不记得还要查重入库 |
| 只用写作台写稿、定稿、导出的团队，永远产不出一条人工审核决定 | `review_drafts` 把**用户真的给出的**结论落库（`decision_source=human` + 真实 reviewer + 时间）。定稿仍然只建 `pending`——定稿不是审核，`commit_drafts` 一个字没改 |
| 稿子发出去就断线了，TV 那边 `v_model_comparison` 长期查出空集还不报错 | `export_drafts` 导的 xlsx 带六列 `_source_autowriter_*`，是笔记回到写作台的唯一线索；列名写错会让 TV 整行 quarantine，判据见 `tests/test_lineage_contract.py`（六个名字手抄在用例里，**不**从 `exporter` 读） |
| 经验卡是可选工具，模型 95% 的对话不调（2026-09-01~16：71 批成稿、3 次借阅），通道 2 一直黑着 | `open_project` 随简报自动借（`lessons` / `lessons_status`），借阅不再取决于模型想不想多调一个工具；三种失败结局各记一条 WARN |
| 借阅失败和"这次没匹配"长得一模一样，用不上经验也没人知道 | `borrow_lessons` 回 `status`：`borrowed` / `empty` / `not_configured` / `timeout` / `error` 五种结局分开，带耗时 |
| 模型跳过 `check_drafts` 直接 `commit_drafts`，同题重写的稿子整批进库（途鸽 09-10 四个标题各入库两次，两两 Jaccard 只有 0.33） | **入库自带闸**：`commit_drafts` 先跑和 `check_drafts` 同一套判定（标题语义、开头、四字串、本批内互比），判 reject 的不进 RPC；没带 `angle_key` 的数出来（`unattributed`），台账销不了账的角度下一批会再被抽到 |
| 协议让 `commit_drafts` 等「用户确认定稿」再调，运营拿了稿子就走，近一周 214 个角度只有 20 条走到入库；改稿时新稿又撞上自己旧版的指纹 | 协议改成**交付即入库**（待审，不代表定稿；定稿与否由飞书 → TV 的 `tv-sync` 对照回来），改稿用 `replaces_version_id` 原地升版：先摘旧指纹再过闸，成功同一个 item 加一版，被拒或异常把指纹放回（2026-09-18） |
| 09-11 起 78% 的角度发出去了、稿子没入库，指纹库不知道它们，下一批查重看不见——而 `/health` 一直 ok | `doctor` 和 `/health` 的 `config.pipeline` 报近 7 天「发了角度没入库」的比例，超过 50% 标红（不进顶层 `ok`：那是流程在漏，不是服务坏了） |
| 已经发出去、没走 commit 的稿子永远补不回指纹库 | 工具 `ingest_published`（运营在 WorkBuddy 里把表粘给模型，≤ 50 条一次，重复调安全）或 `python -m deskcore.cli ingest --xlsx 飞书表` 从导出格式或「标题/正文」两列读回来，建身份（出处记在 `batches.params`，**不碰** `items.external_source`——那列是 TV 同步的标记）+ 写指纹，**不过闸**——已发生的事实拦它没有意义 |
| TV 里 5966 条已发笔记一条都认不回写作台（lineage 全 NULL）；让运营手抄六个 ID 列进飞书三周零匹配 | `tv-sync`（`migrations/009`）在库里按内容对：正文前 40 字 / 标题 / 时间窗内四字串包含度；对不上的直接从 TV 的全文补录进指纹库；`--write-tv` 才回填 TV 的两列。**运营不做任何事，飞书表不加列**。见 `docs/deskcore.md` §3.7 |

> 另有两条改在**常规生成那条路**（根目录的 `memory.py` / `generation_service.py`），
> 不经过 deskcore，列在这里只是免得两边打架：未验证的经验卡现在会在提示词里
> 显形（deskcore 这侧一直是对的——原样回卡字段，协议里解释了 `synthetic`）；
> 生成时会记下**真进了提示词的那几张卡**的 id。deskcore 这条路记不了后者：
> 借阅是模型单独调的一次工具，入库时它已经不知道当时借了什么。

## 结构

```
deskcore/
├── __init__.py     先设 AW_DISABLE_ST_CACHE=1 再 import db(R-042), 见下面第三条纪律
├── core.py         逻辑层, 不 import FastAPI/MCP —— 简报/发牌/查重/学习
├── store.py        db.py 里【没有的】查询形状(共享层规则/带向量的正负例/四张新表)
├── fingerprint.py  指纹与判定, 【只用标准库】
├── vocab.py        闭集: essence 来自 vendor JSON, surface 引用 generator.py
├── identity.py     API key → user_id, 「个人风格私有」的前提
├── tools.py        MCP 工具面, docstring 是给模型看的
├── protocol.md     写作台协议正文的【源文件】; skills/…/SKILL.md 里那份由 cli sync-skill 生成, 测试盯着两边一致
├── app.py          FastAPI + MCP(streamable HTTP) + REST 兜底 + /health 配置回显
├── cli.py          本地 adapter, 含【不连库不联网】的 selftest
├── vendor/         从 truth-vault 原样复制的词表 + sha256(见 vendor/README.md)
├── requirements.txt 相对主 lockfile 的增量依赖: fastapi / uvicorn / mcp
└── railway.json    Railway service 配置(startCommand / healthcheckPath=/health)
```

> ⚠️ 这里的 `app.py` 是 deskcore 的 FastAPI 服务，**不是**仓库根目录那个 Streamlit 工作台的 `app.py`。同名，两回事。

**薄是刻意的。** 分层 prompt 用 `memory.build_layered_system_prompt`，DB 客户端用
`db.get_service_client`，embedding 用 `dedup`，借经验卡用 `librarian_client` ——
本仓已有的一律直接调，不重写。两份"怎么拼分层 prompt"的代码分居两处是最容易
烂掉的那种结构。

**工具数量不写在这里。** 它变过好几次（文档里至今留着 12 / 13 / 16 三个版本的残影）。
要准确数字就问服务：`curl -s <base>/health | jq '.tools | length'`，或读 `tools.py` 的 `TOOLS`。

## 快速自测

```bash
python -m deskcore.cli selftest    # 不装 supabase/anthropic 也能跑, 不连库不联网
```

它验五组：vendor 的词表副本没被手改（连带 lever→valence 派生表和「通用」排他规则）、
三条查重用例（开头精确撞车能拦 / 换皮改写能拦 / 真正不同的稿子不误伤）、
n-gram bottom-k 截断的内容稳定性、正例多样性上限、`angle_key` 不受叠加项影响。
查重那三条是 autowriter 原来抓不到的（它只比标题）。

## 运维命令

和 selftest 不是一类：**这些全都要连库**（`SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`），
补向量的还要 `GOOGLE_API_KEY`。没配时抛的是一句讲「worker 领不到 job」的 `RuntimeError`
（`db.get_service_client` 的通用文案），跟你敲的命令无关——不是命令错了。

| 命令 | 干什么 |
|---|---|
| `doctor [--project <uuid>]` | 这个库跑到第几个迁移了、缺的那些各自会怎样；带 `--project` 还打印该项目的指纹回填缺口 |
| `backfill --project <uuid>` | 把历史成稿补进指纹库。**部署时必跑一次**，否则号称"比对全量历史"的硬闸手里一条历史都没有 |
| `reembed --project <uuid>` | 给指纹库里缺标题向量的行补向量（欠费恢复、换 embedding 模型之后跑） |
| `recompute-fingerprints --project <uuid>` | 按当前 normalize 口径重算确定性指纹。**只在改了 `normalize` 之后跑** |
| `reembed-rules --user <uuid>` | 给**规则**补向量——soft 规则的相关性过滤靠它才有意义 |
| `ingest --project <uuid> --user <uuid> --xlsx 表.xlsx [--dry-run]` | 把**已经发出去、没走 commit** 的稿子从飞书表补进库（身份 + 指纹，不过闸）。认 `export_drafts` 的导出格式（带 `version_id` 的行跳过）或「标题」「正文」两列（`--title-col/--body-col` 可指定）。先 `--dry-run` 看它认出几条 |
| `tv-map add --tv-project SPX_phase1 --project <uuid> --ingest-target` / `tv-map list` | TV 项目 ↔ 写作台项目的对照表；每个 TV 项目只能有一个补录目标 |
| `tv-sync --all [--dry-run] [--write-tv] [--since …] [--rematch]` | 把 TV 的笔记按内容对到写作台的版本，对不上的补进指纹库（不过闸、按全文幂等）。先 `--dry-run` 看数；`--write-tv` 才回填 TV 的 `source_autowriter_*`。**每天跑一次** |
| `tv-resolve --note <note_id> --ingest` | `tv-sync` 报「分不出」的那几条，人看过之后落判定：目前只有一种，「对不上，按 TV 的正文补录」。对照改成 `ingested`，原候选保留。**不硬猜**、也**不提供「指定某一版」**（`match_kind` 没有 manual 一档，硬记成 `body_exact` 是撒谎） |
| `sync-skill [--check]` | **不连库。** 改了 `protocol.md` 之后跑，把正文接进 `skills/…/SKILL.md`；`--check` 只比对，给 CI 用。两边不一致时 `test_protocol_tool.py` 会红 |

另有 `health` / `projects` / `open` / `draw` / `check` 几个只读的手动验证入口。完整参数与验收判据见 runbook §2。

运行期 env（`DESKCORE_KEYS` 一人一把、`SUPABASE_*`、`GOOGLE_API_KEY`、`DESKCORE_ALLOWED_*`）
见 [`docs/deskcore.md`](../docs/deskcore.md) §4.1。**两侧都 fail-closed**：key 一个都没配时所有请求 401
（本服务持 `service_role` 绕 RLS，匿名放行等于开放全部租户数据）。

## 四条纪律

**fail-open 只有 `list_projects` / `borrow_lessons` / `my_style` / `my_rules` 四个。**
这个集合被 `.github/workflows/ci.yml` 的 `SAFE_OK` 钉死，增减都会红。
`open_project`（拿不到 P0 硬约束就照常开写 = 产出违规内容）和全部写类工具都**刻意
没包** `_safe` —— 判据见 `tools.py` 里 `_safe` 的 docstring：失败之后调用方还会不会当作成功
继续往下走，会就不能包。`check_drafts` 更不能，查重出错必须抛：静默放行就是重演
`config.py` 里 `ENABLE_DEDUP_REGEN` 默认关着、查重跑了但不拦的老问题。
`get_protocol` 同理不包——它报错说明服务端的协议文件坏了；模型该按本地 skill 那份（同一份正文）继续、但知道版本没核上，包成带 `error` 的"成功"会让它以为核过了。

**归属拒绝不在 fail-open 范围内**（审计 COR-015）。`_safe` 兜的是瞬时故障；
`PermissionError` 重试一万次也一样，包成"看起来成功"会让调用方模型继续拿同一个错
`project_id` 去试下一个工具。REST 层的状态码口径是「这是谁的错」：
**401** = key 没过，**403** = key 过了但项目不是你的，**404** = 工具名不存在、或项目查无此 ID
（`core.ProjectNotFound`，与 403 同理刻意不返 500），**500** = 服务端真的坏了。

**别在 `deskcore/__init__` 之前 import db。** 它要先设 `AW_DISABLE_ST_CACHE=1`
（R-042，同 `worker.py` 顶部那行 `os.environ.setdefault`），否则 headless 进程会拿
`st.cache_data` 的 30-60s 旧数据。

**读 embedding 必须过 `db._parse_pgvector`。** PostgREST 把 `vector(768)` 序列化成
字符串，不归一的话 `cosine_similarity` 静默返回 0.0 —— 查重变哑弹且不报错（R-034）。
