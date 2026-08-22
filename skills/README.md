# skills/

给 AI 编程/办公助手挂载的 skill（SKILL.md 格式）。

同一份文件三个平台通用，格式同构：

| 平台 | 放哪 |
|---|---|
| WorkBuddy | `~/.workbuddy/skills/<name>/SKILL.md` |
| Claude Code | `~/.claude/skills/<name>/` 或项目 `.claude/skills/` |
| CodeBuddy | `.codebuddy/skills/`（项目级）或 `~/.codebuddy/skills/`（用户级） |

## bywood-writing-desk

写作台协议：管**流程纪律**（先取规则、先发牌、成稿必过查重闸、反馈要分辨
「这一次」还是「以后都这样」），不管文笔。

它配套的工具在 `deskcore/`（MCP 服务），设计见 [`docs/deskcore.md`](../docs/deskcore.md)。

**语言层不在这里。** 去 AI 味、禁用句式、跨篇指纹走 `human-writing` skill；
提示词本身的迭代走 `seeding-prompt-refiner`。三者分工：

- `human-writing` —— 这一篇读起来像不像人写的
- `bywood-writing-desk` —— 这一批守不守项目规则、跟历史重不重
- `seeding-prompt-refiner` —— 提示词本身怎么迭代
