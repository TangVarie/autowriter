# skills/

给 AI 编程/办公助手挂载的 skill（SKILL.md 格式）。本目录目前只有 `bywood-writing-desk` 一个。

## 怎么装

**首选从仓库装，别手工复制**：`TangVarie/autowriter → skills/bywood-writing-desk`（WorkBuddy 支持从 GitHub 装）。
装一次就够了——这份 SKILL.md 只是一根引线，协议正文在服务端，以后改协议运营端不做任何事。

下面这张表只在平台不支持从仓库装时用。**那种情况下这份拷贝要自己记着同步**，理由见下一节。

| 平台 | 放哪 |
|---|---|
| WorkBuddy | `~/.workbuddy/skills/<name>/SKILL.md` |
| Claude Code | `~/.claude/skills/<name>/` 或项目 `.claude/skills/` |
| CodeBuddy | `.codebuddy/skills/`（项目级）或 `~/.codebuddy/skills/`（用户级） |

## bywood-writing-desk

写作台协议：管**流程纪律**（先取规则、先发牌、成稿必过查重闸、反馈要分辨
「这一次」还是「以后都这样」），不管文笔。

**这里的 SKILL.md 只是一根引线。** 协议正文在 [`deskcore/protocol.md`](../deskcore/protocol.md)，
由 deskcore 的 `get_protocol` 工具在每次进入流程时下发。引线装一次就不用再动；
要改协议就改 `deskcore/protocol.md`，合并、部署，所有人同时换版。

为什么这样分：本地 skill 拷到运营机器上之后改了没人提醒，而协议管的是流程纪律，
过期了模型会按老规矩写而没人发现。已经出过一次事故（旧协议没有"评论"的落点，
模型跑去翻交付文档反复读同一个文件，被平台判定死循环强杀）。

**先接服务，再装 skill。** 引线唯一的动作是调 `get_protocol`，而这个工具只在 deskcore 的
MCP / REST 通道里。没接上服务不是"降级可用"，是模型照 SKILL.md 那条「协议读不到就停下来」
当场停住。接入步骤（WorkBuddy 的项目级 `mcp.json`、Claude Code 的 `claude mcp add --transport http`、
key 一人一把）见 [`docs/deskcore.md`](../docs/deskcore.md) §4.3「挂到 WorkBuddy」；
连不上时的排查顺序见 [`docs/deskcore-runbook.md`](../docs/deskcore-runbook.md)「连不上时按这个顺序查」。

它配套的工具在 `deskcore/`（MCP 服务），设计见 [`docs/deskcore.md`](../docs/deskcore.md)。

**语言层不在本仓。** 去 AI 味、禁用句式、跨篇指纹走 `human-writing` skill；
提示词本身的迭代走 `seeding-prompt-refiner`——这两个都**不在** `skills/` 里，要另外装。三者分工：

- `human-writing` —— 这一篇读起来像不像人写的
- `bywood-writing-desk` —— 这一批守不守项目规则、跟历史重不重
- `seeding-prompt-refiner` —— 提示词本身怎么迭代
