# 写作台接 DeepSeek Harness 试点 · 第 1 步：接入配置和验证清单

> **状态**：待验证。这份清单是照两边的源码和文档写的，**没有实际跑过**。
> **范围**：只挂现有的 MCP 服务和现有的 skill，不写插件。deskcore 服务端、`deskcore/protocol.md`、`skills/` 一个字都不改，WorkBuddy 那条路照旧。
> **对照的版本**：DeepSeek Harness（下称 DSH）`deepseek-ai/deepseek-harness` 提交 `39163dd3`（2026-11-13），npm `@deepseek-ai/dsh` 0.2.2-rc.1。DSH 还是公开预览版，升级后先重看「和 WorkBuddy 不一样的地方」那一节。

这一步要回答三个问题：

1. 现有的 MCP 服务能不能原样连上 DSH（鉴权头、协议版本、工具名）。
2. DeepSeek 模型照现有协议能不能把一批稿子从开项目走到入库。
3. 走不通的地方是哪几处，第 2 步的插件该补什么。

## 一、和 WorkBuddy 不一样的地方

下面每一条都是读 DSH 源码和它仓库里的文档得来的，括号里是出处（DSH 仓库内的路径）。

| 项 | WorkBuddy | DSH | 影响 |
|---|---|---|---|
| skill 正文放哪 | 进系统提示，整场都在 | 目录只进一条摘要；正文要模型调 `skill` 工具才加载，加载后留在**工具历史**里（`packages/skill/tool-skill/README.md`） | 长对话压缩时正文会被摘要掉。这正是 09-11 入库率从 110% 掉到 22% 的那种形状，**本步要专门测**（验证清单 D 组） |
| skill 描述长度 | 不截 | 目录里每条描述最多 500 字符（`catalogDescriptionMaxLength`） | 现在的描述 512 字符，截掉的只有结尾「pt-refiner）。」，品牌名都在。以后加品牌名会把尾巴继续往外挤 |
| 工具名 | `open_project` | `mcp__deskcore__open_project`（`packages/mcp/mcp-client/README.md`） | 协议里写的是不带前缀的名字，要看模型对不对得上（B3） |
| 服务端 `instructions` | 认的平台才注入 | 作为一段进系统提示，上限 32,768 字节，超了**拒绝连接** | 现在那段两千多字节，没问题。「写稿先开项目」这层兜底在 DSH 里一直在模型眼前 |
| 单次工具调用超时 | 实测约 22 秒 | 默认 60 秒（`toolCallTimeoutMs`） | 借卡最多等 18 秒，余量比 WorkBuddy 大（C 组顺带记耗时） |
| 自定义鉴权头 | 无权威文档，留了 Bearer 退路 | `headers` 是正式字段 | `X-Deskcore-Key` 直接用 |
| 断线重连 | — | 自动重连，500 毫秒起翻倍到 30 秒；连败 10 次后摘掉工具，要重载配置或重启 | 模型看到「工具没了」时不一定是没配 |
| 本地文件和命令 | 基本没有 | 自带读写文件、bash、终端工具 | 协议第 3 条「不读不写本地文件」只靠模型自觉，本步观察它守不守（E2） |
| 别的插件 | — | 装进来的插件和内置包同进程、无隔离 | 试点机器上**只装官方自带的**，不加第三方插件索引 |

## 二、接入配置

### 1. 装 DSH，配模型

桌面端从官网下，或者命令行 `npx @deepseek-ai/dsh web`。在 Settings → Models 里填 DeepSeek 的 API key。**用哪个模型、哪档推理强度，记在验证记录里**——换模型等于换了一组实验。

### 2. 给试点单独发一把写作台的 key

在 `DESKCORE_KEYS` 里新加一把，背后还是团队身份（`user_id` 抄库里已有的，见 `docs/deskcore.md` §5 第 4 条），名字写成能认出来的，比如 `dsh-pilot`。**不要拿写手正在用的那把**：后面要靠 key 把 DSH 的调用从调用日志里分出来。

### 3. 挂 MCP 服务

写一个覆盖文件，随便放哪，比如 `~/deskcore.cordis.yml`：

```yaml
- insert:
    - id: mcp-deskcore
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: deskcore
        transport: streamable-http
        url: https://<你的-deskcore>/mcp
        headers:
          X-Deskcore-Key: !!js process.env.DESKCORE_KEY
        startupTimeoutMs: 30000
```

启动：

```bash
DESKCORE_KEY='k-整把钥匙' npx @deepseek-ai/dsh web --patch "$HOME/deskcore.cordis.yml"
```

几点说明：

- `serverName` 就写 `deskcore`，工具名的前缀由它定。
- key 走环境变量，不写进文件。贴整把，不带引号里的空格，不加 `Bearer`。
- `toolCallTimeoutMs` 先不设，用默认的 60 秒，看 C 组实测耗时再定。
- 桌面端也可以在设置里的 MCP 页加同样的条目（那里的 key 用 `${credential:DESKCORE_KEY}` 引用凭据库）。**设置页的入口叫什么、长什么样我没核对过**，以实际界面为准；第一次验证建议走上面的覆盖文件，变量少。

### 4. 装 skill

把仓库里的 `skills/bywood-writing-desk/` 整个目录拷到 `~/.dsh/skills/bywood-writing-desk/`（里面是 `SKILL.md`）。DSH 只认 `<根>/<名字>/SKILL.md` 这一层，再深一层不扫。

这份是手工拷贝，协议改了要重拷。开场核版本那一步照样会提醒，但提醒里那句「在 WorkBuddy 里说更新技能」在 DSH 里不成立，试点期间由做验证的人自己重拷。

### 5. 开新会话

MCP 工具是异步发现的，等工具列表里出现 `mcp__deskcore__…` 再开始；skill 和 MCP 改完都要开**新会话**才生效。

## 三、验证清单

**在测试项目上跑**，不要用写手正在写的项目：这一步的稿子不该进真项目的指纹库。每一项记「过 / 不过 / 现象」，不过的把模型原话和 DSH 的 trace 截下来。

### A. 连接和鉴权

| # | 做什么 | 过的标准 |
|---|---|---|
| A1 | 启动后看工具列表 | 出现 `mcp__deskcore__` 开头的工具，数量和 WorkBuddy 里一致 |
| A2 | 看 trace 里的系统提示 | 有一段 deskcore 的 `instructions`（「写稿先开项目…」） |
| A3 | 故意把 key 改错一位再启动 | 连不上，DSH 日志里是 401，`detail` 说了钥匙哪里不对；不是静默成功 |
| A4 | 看 `/health` 的 `key_transport` | `header` 在涨，`query` 没动 |
| A5 | 查调用日志 | DSH 的调用都记在 `dsh-pilot` 那把 key 名下 |

A1 或 A3 不过就停：协议层不通，后面都不用测。

### B. skill 被调起来

每条开一个新会话，只说引号里的话：

| # | 说什么 | 过的标准 |
|---|---|---|
| B1 | 「帮我写几篇 <测试项目的品牌名>」 | 模型先加载 `bywood-writing-desk`，再 `get_protocol`、`list_projects` / `open_project`，然后才动笔 |
| B2 | 只贴一段 brief，不提品牌、不提写作台 | 同上 |
| B3 | 看 B1 的 trace | 协议里的 `open_project` 被正确对到 `mcp__deskcore__open_project`，没有「找不到工具」 |
| B4 | 开场 `get_protocol` | 带了 `local_version`，回 `up_to_date: true` |
| B5 | 「给 <品牌> 写 10 条评论」 | 先 `comments_for`，不是 `open_project` |

B1、B2 里模型没加载 skill 就直接写，记下来——这是第 2 步插件第一件要补的（协议常驻系统提示）。此时 A2 那段 `instructions` 有没有把它拉回来，也记一笔。

### C. 一批稿子走完全流程

一个会话，写 5 篇：

| # | 看什么 | 过的标准 |
|---|---|---|
| C1 | 开场 | 复述了项目名和 P0 硬约束 |
| C2 | 发牌 | 调了 `draw_angles`，每篇角度不同 |
| C3 | 查重 | 写完先调 `check_drafts`，reject 的重写后再查 |
| C4 | 存档和判稿 | 交付的同一轮里调了 `score_drafts`（存档和打分是这一个工具，`commit_drafts` 是它的旧名）；`rejected` 为空，库里多了 5 条、带指纹；合规问题自己改了再交 |
| C5 | 交付 | 交的是 `score_drafts` 回的交付稿，没有自己另排、没有生成文件 |
| C6 | 耗时 | 记 `open_project`（含借卡）、`check_drafts`、`score_drafts` 各自的耗时；有没有超过 60 秒被 DSH 掐掉的 |
| C7 | 改一句再说「以后都这样」 | 调了 `record_rule`，下一个新会话 `open_project` 能看到这条 |

### D. 长对话之后还记不记得（本步的重点）

接着 C 的会话继续，不开新的：

| # | 做什么 | 过的标准 |
|---|---|---|
| D1 | 再要三批、每批 5 篇，中间穿插几轮改稿 | 每一批都照样发牌、`check_drafts`、`score_drafts`，没有哪一批是「写完就交」 |
| D2 | 手动 `/compact` 一次，再要一批 | 压缩之后模型仍然走全流程；工具名没忘 |
| D3 | 查库 | 这个会话里交出去的篇数 = 入库的篇数 |

D2 不过是预期之内的结果，不算试点失败：它说明协议正文在 DSH 里必须由插件放进系统提示，第 2 步照做就是。要记的是**从第几批开始掉的、掉的是哪一步**。

### E. 边界

| # | 做什么 | 过的标准 |
|---|---|---|
| E1 | 断网或停掉 MCP 条目后说「写几篇」 | 模型不写稿，原话说没连上写作台；不编「服务中断了」 |
| E2 | 全程观察 | 没有读写本地文件、没有跑命令去「找上次的稿子」 |
| E3 | 同一会话里说「再写个别的项目」 | 模型让新开对话，不把上一个项目的规则带过去 |
| E4 | 同时点一个别的 skill（比如文案类的）再让它写 | 写作台流程照走，别的 skill 只当参考 |

## 四、怎么算这一步过了

- **A 全过、B 和 C 大体过**：DSH 能当写作台的客户端，进第 2 步写插件。B、D 里不过的项就是插件的需求单。
- **A 不过**：协议层不通。先看 DSH 日志里的错误码，对着 `docs/deskcore-runbook.md`「连不上时按这个顺序查」排；排不掉就停在这里。
- **C 大面积不过**（模型拿到协议也走不完流程）：问题在模型，不在挂载方式，插件救不了。换更强的档位再测一次，还不行就不往下做。

## 五、收尾

- 测试项目里写进去的稿子和规矩留着没关系；如果用了真项目，把这批的批次号记下来交给负责人。
- `dsh-pilot` 那把 key 第 2、3 步还要用，先别删。
- 验证记录建议追加在本文件末尾，写明日期、DSH 版本、模型和档位。
