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

**这里的 SKILL.md 只是一根引线。** 协议正文在 [`deskcore/protocol.md`](../deskcore/protocol.md)，
由 deskcore 的 `get_protocol` 工具在每次进入流程时下发。引线装一次就不用再动；
要改协议就改 `deskcore/protocol.md`，合并、部署，所有人同时换版。

为什么这样分：本地 skill 拷到运营机器上之后改了没人提醒，而协议管的是流程纪律，
过期了模型会按老规矩写而没人发现。已经出过一次事故（旧协议没有"评论"的落点，
模型跑去翻交付文档反复读同一个文件，被平台判定死循环强杀）。

它配套的工具在 `deskcore/`（MCP 服务），设计见 [`docs/deskcore.md`](../docs/deskcore.md)。

**语言层不在这里。** 去 AI 味、禁用句式、跨篇指纹走 `human-writing` skill；
提示词本身的迭代走 `seeding-prompt-refiner`。三者分工：

- `human-writing` —— 这一篇读起来像不像人写的
- `bywood-writing-desk` —— 这一批守不守项目规则、跟历史重不重
- `seeding-prompt-refiner` —— 提示词本身怎么迭代
