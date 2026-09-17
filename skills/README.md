# skills/

给 AI 编程/办公助手挂载的 skill（SKILL.md 格式）。本目录目前只有 `bywood-writing-desk` 一个。

## 怎么装

**首选从仓库装，别手工复制**：`TangVarie/autowriter → skills/bywood-writing-desk`（WorkBuddy 支持从 GitHub 装）。
**协议正文就在这份 SKILL.md 里**（由 `deskcore/protocol.md` 生成）。改了协议要让运营重新导入一次；忘了重新导入的话，模型开场核版本时会拿到新正文并提醒他。

下面这张表只在平台不支持从仓库装时用。**那种情况下这份拷贝要自己记着同步**，理由见下一节。

| 平台 | 放哪 |
|---|---|
| WorkBuddy | `~/.workbuddy/skills/<name>/SKILL.md` |
| Claude Code | `~/.claude/skills/<name>/` 或项目 `.claude/skills/` |
| CodeBuddy | `.codebuddy/skills/`（项目级）或 `~/.codebuddy/skills/`（用户级） |

## bywood-writing-desk

写作台协议：管**流程纪律**（先取规则、先发牌、成稿必过查重闸、反馈要分辨
「这一次」还是「以后都这样」），不管文笔。

**协议正文的源文件是 [`deskcore/protocol.md`](../deskcore/protocol.md)，SKILL.md 里那份由它生成。**
改了源文件就跑 `python -m deskcore.cli sync-skill`，把两个文件一起提交（测试
`test_skill_carries_the_full_protocol_and_is_in_sync` 盯着两边逐字一致）。SKILL.md 里
`<!-- protocol_version: … -->` 那一行以上是手写的引线头，以下是生成的。

为什么正文在 skill 里而不是每次从服务端取（2026-09-17）：09-10 曾把正文搬到服务端、
SKILL.md 只留一根引线让 `get_protocol` 每次下发——09-11 起定稿入库率从 110% 掉到 22%。
34 KB 的正文作为对话开头的一次工具返回值，在写完十几篇稿子之后已经离得太远或被平台
压缩掉，模型不记得还要查重和入库，写出去的稿子没有指纹，下一批查重看不见它们。技能
文件进系统提示，整场对话都在眼前。"改了没人知道"这个问题改由版本校验解决：开场
`get_protocol` 带上本地 `protocol_version`，不一致就下发新正文并提醒重新导入。

**先接服务，再装 skill。** 开场那一次 `get_protocol` 是版本校验，服务没接上时模型按本地
这份走、但会说一句"版本没核上"；写稿、查重、入库全部依赖服务，所以没接上不是"降级可用"。接入步骤（WorkBuddy 的项目级 `mcp.json`、Claude Code 的 `claude mcp add --transport http`、
key 一人一把）见 [`docs/deskcore.md`](../docs/deskcore.md) §4.3「挂到 WorkBuddy」；
连不上时的排查顺序见 [`docs/deskcore-runbook.md`](../docs/deskcore-runbook.md)「连不上时按这个顺序查」。

它配套的工具在 `deskcore/`（MCP 服务），设计见 [`docs/deskcore.md`](../docs/deskcore.md)。

**语言层不在本仓。** 去 AI 味、禁用句式、跨篇指纹走 `human-writing` skill；
提示词本身的迭代走 `seeding-prompt-refiner`——这两个都**不在** `skills/` 里，要另外装。三者分工：

- `human-writing` —— 这一篇读起来像不像人写的
- `bywood-writing-desk` —— 这一批守不守项目规则、跟历史重不重
- `seeding-prompt-refiner` —— 提示词本身怎么迭代
