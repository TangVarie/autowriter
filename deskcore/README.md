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
| 运营端那份 SKILL.md 改了没人知道，模型照旧协议写 | `get_protocol` 每次从服务端下发 `protocol.md` 正文，改协议只改服务端并重部署，全员同时换版 |
| 稿子发出去就断线了，TV 那边 `v_model_comparison` 长期查出空集还不报错 | `export_drafts` 导的 xlsx 带六列 `_source_autowriter_*`，是笔记回到写作台的唯一线索；列名写错会让 TV 整行 quarantine，判据见 `tests/test_lineage_contract.py`（六个名字手抄在用例里，**不**从 `exporter` 读） |

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
├── protocol.md     写作台协议正文, get_protocol 每次下发; skills/ 里的 SKILL.md 只是引线
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
`get_protocol` 同理不包——协议拿不到就该停，不能让模型按记忆里的旧版本继续。

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
