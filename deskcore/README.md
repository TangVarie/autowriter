# deskcore · 写作台内核

内容工作台的能力内核，做成 MCP 工具服务。**Streamlit 界面停用，库里的积累一条不迁。**

完整设计见 [`docs/deskcore.md`](../docs/deskcore.md)；**上线手册 / 对接 / 待办 / 已知不一致**见 [`docs/deskcore-runbook.md`](../docs/deskcore-runbook.md)。决策在 truth-vault：`DECISIONS.md` D-041 / `docs/10-sister-repo-followups.md` R-034。

> ⚠️ 截至 2026-08-23，**本服务尚未部署**，`draft_fingerprints` / `angle_ledger` / `user_calibration_notes` / `style_edits` 四张表都是 0 行。上线步骤看 runbook §2。

## 它解决什么

| 症状 | 机制 |
|---|---|
| WorkBuddy 老忘记之前定的规则 | `open_project` 每次生成前重新注入 P0 硬约束层，不依赖对话记忆 |
| 写出来像 AI 不像本人 | `record_edit` 收手动精修 diff（最高权重信号），蒸馏成个人调校笔记 |
| 跨批次越写越像 | `draw_angles` 发牌台账 + `check_drafts` 全量成稿指纹硬闸 |
| 一个项目 5 个提示词要点 5 次 | `open_project` 一次拿全 |

## 结构

```
deskcore/
├── core.py         逻辑层, 不 import FastAPI/MCP —— 简报/发牌/查重/学习
├── store.py        db.py 里【没有的】查询形状(共享层规则/带向量的正负例/四张新表)
├── fingerprint.py  指纹与判定, 【只用标准库】
├── vocab.py        闭集: essence 来自 vendor JSON, surface 引用 generator.py
├── identity.py     API key → user_id, 「个人风格私有」的前提
├── tools.py        10 个 MCP 工具, docstring 是给模型看的
├── app.py          FastAPI + MCP(streamable HTTP) + REST 兜底 + /health 配置回显
├── cli.py          本地 adapter, 含【不连库不联网】的 selftest
└── vendor/         从 truth-vault 原样复制的词表 + sha256(见 vendor/README.md)
```

**薄是刻意的。** 分层 prompt 用 `memory.build_layered_system_prompt`，DB 客户端用
`db.get_service_client`，embedding 用 `dedup`，借经验卡用 `librarian_client` ——
本仓已有的一律直接调，不重写。两份"怎么拼分层 prompt"的代码分居两处是最容易
烂掉的那种结构。

## 快速自测

```bash
python -m deskcore.cli selftest    # 不装 supabase/anthropic 也能跑
```

它验四件事：vendor 的词表副本没被手改、开头精确撞车能拦、换皮改写能拦、
真正不同的稿子不误伤。前两条查重是 autowriter 原来抓不到的（它只比标题）。

## 三条纪律

**fail-open 只有 `list_projects` / `borrow_lessons` / `my_style` 三个。**
`open_project`（拿不到 P0 硬约束就照常开写 = 产出违规内容）和全部写类工具都**刻意
没包** `_safe` —— 判据见 `tools.py:25` 的 docstring：失败之后调用方还会不会当作成功
继续往下走，会就不能包。`check_drafts` 更不能，查重出错必须抛：静默放行就是重演
`config.py:132` 那个 `ENABLE_DEDUP_REGEN` 默认关着、查重跑了但不拦的老问题。

**别在 `deskcore/__init__` 之前 import db。** 它要先设 `AW_DISABLE_ST_CACHE=1`
（R-042，同 `worker.py:56`），否则 headless 进程会拿 `st.cache_data` 的 30-60s 旧数据。

**读 embedding 必须过 `db._parse_pgvector`。** PostgREST 把 `vector(768)` 序列化成
字符串，不归一的话 `cosine_similarity` 静默返回 0.0 —— 查重变哑弹且不报错（R-034）。
