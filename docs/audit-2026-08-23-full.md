# 全量代码审计 · 2026-08-23

范围: 仓库全部非第三方、非生成源码 (21,271 行 Python / 25 个文件)。
唯一 vendored 目录 `deskcore/vendor/` 只做校验和核对, 不审内容。

所有结论都带 `文件:行号`。无法靠静态阅读定死的, 标 **待人工确认** 并写明验证手段。

---

## 1. 执行摘要

| 项 | 数 |
|---|---|
| 源码文件 / 行数 | 25 个 `.py` / 21,271 行 |
| 巨型文件 (>2000 行) | 4 个: `app.py` 5560 · `db.py` 3747 · `generator.py` 2530 · `memory.py` 2343 |
| 模块数 (顶层 + deskcore) | 17 + 9 |
| 入口 | 9 (Streamlit / worker / uvicorn / 4 个 HTTP 路由 / CLI / CI) |
| 后台静默逻辑 | 23 项 (见 §3) |
| 测试文件 | **0**。唯一验证 = `deskcore/cli.py selftest` + `ci.yml` 内联脚本 |
| 正确性问题 (COR) | 22 |
| 优越性问题 (SUP) | 24 |
| 健壮性风险 (ROB) | 23 |
| 待人工确认 | 4 |

**严重程度分布**

| 级别 | 数 | 编号 |
|---|---|---|
| P0 (数据丢失 / 越权 / 静默失效) | 8 | COR-001 COR-003 COR-005 COR-015 ROB-001 ROB-002 ROB-003 ROB-006 |
| P1 (正确性错误 / 显著成本或性能) | 17 | COR-002 COR-004 COR-006 COR-007 COR-008 COR-012 COR-014 COR-018 SUP-001 SUP-002 SUP-003 SUP-009 SUP-013 ROB-004 ROB-005 ROB-007 ROB-020 |
| P2 (可维护性 / 局部缺陷) | 30 | 其余 |
| P3 (文档/死代码) | 14 | COR-016 COR-019 SUP-018 SUP-021 等 |

**一句话结论**: 这份代码库在**单点防御**上做得非常扎实 —— 几乎每个已知坑都有注释、有 review 编号、有兜底。
它真正的风险集中在三处**结构性**位置:

1. 生成主链路仍活在 Streamlit daemon 线程里, 而为它准备的 DB job 队列已建好却只挂着 `noop` handler (`worker.py:102-121`)。R-018 想解决的"进程重启丢批次"原样存在, 并额外带来 JWT 中途过期这个新故障。
2. 大量"上限 + 无翻页"的查询散落各处, 而本仓自己在 `db.py:1417` / `deskcore/store.py:292` 反复声明 PostgREST `max-rows=1000`。同一个坑在 `list_items_for_batches` 被明确修过 (`db.py:1657-1665`), 但 5 个同类点没跟上。
3. 相似度比对全是纯 Python 逐元素循环, 而 pgvector 索引早已建好 (`db.py:742-744`)。查重硬闸的延迟随历史线性增长, 且同步占住 uvicorn 线程。

---

## 2. 模块与架构总结

### 2.1 模块清单

| 路径 | 行 | 职责 | 性质 |
|---|---|---|---|
| `app.py` | 5560 | Streamlit 6 个页面 + 两条后台生成线程 | 源码 · 巨型 |
| `db.py` | 3747 | 数据访问 + 全量 DDL 字符串 + job 队列数据层 | 源码 · 巨型 |
| `generator.py` | 2530 | Claude/Gemini 引擎、JSON 修复、批量生成、合规复审、多角色 | 源码 · 巨型 |
| `memory.py` | 2343 | 分层 system prompt、记忆合并器、调校笔记、记忆管理 UI | 源码 · 巨型 |
| `projects.py` | 768 | 项目 CRUD UI + 战术/角色解析 | 源码 |
| `auth.py` | 584 | Supabase Auth + cookie 持久化 | 源码 |
| `exporter.py` | 425 | xlsx/docx + 飞书 webhook | 源码 |
| `validator.py` | 325 | 硬规则确定性校验 | 源码 |
| `clients.py` | 286 | SDK 单例 + 重试中间件 | 公共基础设施 |
| `telemetry.py` | 288 | BatchMetrics 埋点 | 公共基础设施 |
| `image_handler.py` | 260 | 图片压缩/编码/上传 | 源码 |
| `worker.py` | 223 | DB job 队列后台进程 | 后台静默 |
| `dedup.py` | 171 | Gemini embedding + 余弦 | 源码 |
| `librarian_client.py` | 112 | TV 飞轮 HTTP 客户端 | 源码 |
| `logger_utils.py` | 103 | secret 脱敏 | 公共基础设施 |
| `config.py` | 416 | 配置 / 价格表 / 上下文窗口 | 公共基础设施 |
| `deskcore/core.py` | 1093 | 写作台纯逻辑层 | 源码 |
| `deskcore/store.py` | 557 | deskcore 独有查询形状 | 源码 |
| `deskcore/tools.py` | 342 | 11 个 MCP 工具面 | 源码 |
| `deskcore/app.py` | 315 | FastAPI + MCP mount + 鉴权中间件 | 源码 · 入口 |
| `deskcore/cli.py` | 264 | 本地 CLI + selftest | 源码 |
| `deskcore/fingerprint.py` | 204 | 指纹与判定 (零依赖) | 源码 |
| `deskcore/vocab.py` | 189 | 受控词表加载 | 源码 |
| `deskcore/identity.py` | 151 | key → user_id | 源码 · 安全边界 |
| `deskcore/vendor/` | — | TV 受控词表副本 + sha256 | **vendored** |

### 2.2 依赖方向

```
app ──> db, memory, generator, projects, auth, exporter,
        dedup, validator, telemetry, image_handler, librarian_client, config
memory ──> db, clients, telemetry, config
generator ──> clients, config, telemetry, memory
db ──> config, telemetry
dedup ──> clients ──> config
telemetry ──> logger_utils
worker ──> db, config, telemetry
deskcore/app ──> identity, tools, vocab
deskcore/tools ──> deskcore/core
deskcore/core ──> db, dedup, memory, librarian_client, projects, config,
                  deskcore/{store,fingerprint,vocab}
deskcore/store ──> db
```

**循环依赖 2 处, 均刻意打断**:
- `clients.py:123-129` 用 `sys.modules.get("generator")` 清 `_ENGINE_CACHE`, 避开 `clients ↔ generator` 静态环。
- `deskcore/vocab.py:88-101` / `:113-134` 用 `try: import generator` + 硬编码 fallback。**代价见 SUP-018**。

### 2.3 外部依赖

Supabase (Postgres/PostgREST/GoTrue/Storage/pgvector) · Anthropic (可经中转站) ·
Google GenAI (embedding + Gemini) · TV 飞轮馆员 (Railway HTTP) · 飞书 Webhook ·
Railway/Render/Heroku (worker + deskcore 独立 service)。

### 2.4 入口

| # | 入口 | 位置 |
|---|---|---|
| 1 | `streamlit run app.py` | `Procfile:1` → `app.py:5535` |
| 2 | `python worker.py` | `worker.py:222` |
| 3 | `uvicorn deskcore.app:app` | `deskcore/railway.json:8` |
| 4 | `GET /health` (**免鉴权**) | `deskcore/app.py:140` |
| 5 | `GET /tools` | `deskcore/app.py:208` |
| 6 | `POST /tool/{name}` | `deskcore/app.py:217` |
| 7 | `/mcp` streamable HTTP | `deskcore/app.py:312-315` |
| 8 | `python -m deskcore.cli` | `deskcore/cli.py:180` |
| 9 | GitHub Actions `ci` | `.github/workflows/ci.yml` |

---

## 3. 后台静默逻辑台账

| # | 名称 | 触发 | 周期/并发 | 副作用 | 幂等 | 失败处理 | 位置 |
|---|---|---|---|---|---|---|---|
| B1 | 生成队列 worker | UI 按钮 | daemon 线程, 1/会话, 串行 N plan | 写 6 张表 + LLM 计费 | 否 | 每 plan try + 外层 BaseException shim | `app.py:923-1408`, 启动 `app.py:3722-3727` |
| B2 | 快速生成 worker | UI 按钮 | daemon 线程, 1/项目/会话 | 同 B1 (单批) | 否 | try + finally 拨 done | `app.py:3076-3443`, 启动 `app.py:4083-4087` |
| B3 | 自动重生 | 语义去重命中 + 策略开 | 每重复项 ≤max_retries 次 LLM | UPDATE versions; 耗尽标 needs_revision | 否 | 写失败返 False 续试 | `app.py:296-449` |
| B4 | 失败位补量 | 引擎调用有缺口 | 每次调用 ≤1 刀, 不递归 | 额外 LLM | 否 | 异常吞掉 | `generator.py:1423-1520` |
| B5 | 合规复审 | 有 P0/P1/P2 或飞轮块 | 每批 1 次 Claude | 只打 token_usage 标 | 是 | 全吞 + telemetry | `generator.py:1836-1980` |
| B6 | 硬约束校验 | 每批落库后 | version × hard rule | items.status=needs_revision | 是 | telemetry | `app.py:237-293` |
| B7 | 记忆合并器 | 填了 extra_instructions | 每 plan 1 次 Claude | 写 memories / calibration | 部分 (CAS) | — | `app.py:1165-1171` → `memory.py:564` |
| B8 | 太子自动调校反思 | 审核页全部通过 | 每 batch 1 次 | 追加 calibration_notes | **是** (`auto_calibrated_at`) | 返 False | `app.py:5020-5072`, `db.py:1157-1188` |
| B9 | session 懒同步 | 每批生成前 | 每 engine 1 次 | 写 session_messages | **是** (唯一索引) | 静默返 0 | `app.py:674-725` |
| B10 | session 自动封窗 | 每批生成后 | 每 engine | status=sealed | 是 | telemetry | `app.py:861-920` |
| B11 | embedding 预热 | 项目首次入队 | 每 project 1 次 | 只读 | 是 | status.warnings | `app.py:1176-1198` |
| B12 | job worker 主循环 | `python worker.py` | 2s 轮询 | 领 job 跑 handler | 依 handler | 全吞 + 退避 | `worker.py:196-219` |
| B13 | job 心跳线程 | job 处理期间 | 15s, 1 线程/job | UPDATE heartbeat_at (CAS) | 是 | `db.heartbeat_job` 内吞 | `worker.py:130-139`, `db.py:3602-3615` |
| B14 | 死 job sweeper | 主循环每轮, 60s 节流 | 单 worker 串行 | 退回 pending / failed | 是 (CAS) | telemetry | `worker.py:185-192`, `db.py:3713-3747` |
| B15 | SIGTERM/SIGINT | 容器滚动 | — | 置 `_shutdown`, **不中断 handler** | — | 处理器里直接做 stdout I/O | `worker.py:83-85,197-198` |
| B16 | PG 触发器 items.updated_at | status/example_label 变更 | 每行 | 写 updated_at | 是 | — | `db.py:803-809` |
| B17 | PG 触发器 user_calib.updated_at | UPDATE | 每行 | 写 updated_at | 是 | — | `db.py:794-797` |
| B18 | `deskcore_reserve_angles` | draw_angles | advisory lock 按 project 串行 | INSERT angle_ledger | 否 | RPC 缺失 → 降级非原子 | `db.py:823-881` |
| B19 | `deskcore_commit_fingerprints` | commit_drafts | 同上 | INSERT draft_fingerprints | 否 | RPC 缺失 → 降级直插 | `db.py:901-982` |
| B20 | LLM 重试退避 | 调用失败 | ≤6/5 次, `time.sleep` 阻塞 | 重复调用 | **待人工确认** | 耗尽抛原异常 | `clients.py:137-183` |
| B21 | token 主动刷新 | 每 rerun, ttl<60s | ≤1/rerun | 换 token + 写 cookie | 否 (轮换) | 双路失败 → 登出 | `auth.py:344-358` |
| B22 | cookie 写入等待 | 登录成功后 | `time.sleep(0.6)` | — | 是 | 无 | `auth.py:110-115` |
| B23 | 队列横幅 fragment | UI 渲染 | 2s | 只读 | 是 | — | `app.py:1416` |

**未发现**: cron / @Scheduled / Quartz / Celery / APScheduler / 系统 crontab、
MQ 消费者 (Kafka/RabbitMQ/SQS/RocketMQ/Pulsar)、asyncio 后台 task、事件调度器。

---

## 4. 正确性问题清单 (COR)

按严重程度排序。完整表格见下。

### P0

**COR-001 · 后台线程持有的 JWT 会中途过期, 且永不刷新**
- 位置: `app.py:3724` (传 `db_client` 进线程) · `db.py:69-99` (按 token 缓存 client) · `auth.py:344-358` (刷新只改 session_state)
- 触发: 队列总时长 > access token 剩余寿命 (Supabase 默认 1h)
- 影响: 超时后每个 DB 调用 401。LLM 的钱已经花了, 内容全丢。用户只看到一串 `计划 N: ...`
- 复现: 排 15+ plan, 或把 Supabase JWT TTL 调到 5 分钟
- 修复: 线程内每次 DB 操作前取最新 token; 或把生成搬进 job 队列走 service_role (正是 R-018 的初衷)

**COR-003 · 调校笔记 CAS witness 走 URL query, 笔记变长后自动学习静默停写**
- 位置: `memory.py:904` (`.eq("calibration_notes", expected_before_text)`) · 上限 `memory.py:657` (4000 字) · 失败被吞 `memory.py:820-822`
- 触发: `calibration_notes` 长度超过网关 URL/header 上限。中文 URL-encode 后 ×9, 4000 字 ≈ 36KB, 远超 nginx 8k
- 影响: 迭代/手改/整批反思/taste 四条自动学习路径**全部静默停止**。用户以为在学, 其实早断了
- 复现: 把某项目 `calibration_notes` 填到 3000+ 中文字, 触发一次迭代沉淀
- 修复: 改成带 CAS 的 RPC, witness 传 `md5(calibration_notes)` 而非全文
- 待人工确认: 目标 Supabase (Kong/nginx) 的确切 URL 上限

**COR-005 · backfill 幂等依据无分页, 超过 max-rows 后重复插指纹**
- 位置: `deskcore/store.py:386-395` (`existing_fingerprint_version_ids` 无 limit 无翻页)
- 触发: 项目指纹数 > PostgREST `db-max-rows` (本仓自己在 `db.py:1417`/`store.py:292` 声明为 1000)
- 影响: 重跑 backfill 给已存在 version 重复插行; 重复行拉高 Jaccard/相似度, 误杀正常选题
- 复现: 造 1200 条指纹后跑两次 `deskcore.cli backfill`
- 修复: 照 `store.fingerprints:295-338` 的翻页写法改

**COR-015 · deskcore 对 project_id 无任何归属校验**
- 位置: `deskcore/tools.py:330-342` (`check_drafts`/`borrow_lessons`/`list_projects` 均 `needs_user=False`) · 对比 `deskcore/core.py:1010-1016` (`label_example` 是唯一做归属校验的)
- 触发: 任一持有效 key 的调用方传入他人 `project_id`
- 影响: 可读他人项目全部指纹标题 (`check_drafts` 返回 `collided_with`); 可往他人项目写指纹与角度台账
- 复现: 用 key A 调 `commit_drafts(project_id=<B 的项目>)`
- 修复: 若"项目团队共享"是有意的, 至少给**写**操作加成员校验; 否则按 `projects.owner_id` 校验
- 说明: `deskcore/store.py:47-58` 明确写了"不按 owner 过滤"是设计选择。本条指出的是: 这个选择让整套隔离只剩 key 这一层, 而 key 这一层有 ROB-003

### P1

| 编号 | 问题 | 位置 | 触发 | 影响 | 修复 |
|---|---|---|---|---|---|
| COR-002 | `zip(valid_slots, inserted_items)` 依赖 bulk insert 返回顺序, 返回数少于入参时静默丢尾部 | `app.py:197`; `db.py:1251-1262` 只在 docstring 断言 | 顺序变化或部分插入 | version 挂错 item; 尾部正文永久丢失且无报错 | 带序号列后按键匹配; `len` 不等显式报错 |
| COR-004 | `create_version` 是 SELECT max + 1 + INSERT, `(item_id, version_num)` 无唯一约束 | `db.py:1785-1806`; schema `db.py:252-264` | 同 item 并发迭代 | 重复 version_num; best 指针可能指向被覆盖版本 | 加 UNIQUE + 撞键重试 |
| COR-006 | `legacy_versions(limit=5000)` / `fingerprints_missing_vectors(limit=2000)` 单次 limit, 被 max-rows 截断 | `deskcore/store.py:358`, `:410` | 历史 > 1000 条 | backfill 只覆盖最近 1000 条却报告像是全量; 老稿重复原样放行 | 翻页 + 用 `count='exact'` 对账 |
| COR-007 | deskcore 自写 muted 判定, 不走已修好 bug 的 `db.is_memory_muted_now` | `deskcore/store.py:104-111` vs `db.py:2819-2856` | naive ISO 或 7 位微秒 | TypeError → except 返 True → **被静音的规则照常注入 P0/P1** | 直接调 `db.is_memory_muted_now` |
| COR-008 | "短页即收工"分页终止; 同文件已把这写法定性为 bug 并改过 | `db.py:1544-1545` vs `db.py:1657-1665`; 同类 `db.py:3132`, `db.py:3215`, `store.py:326` | `db-max-rows` < page_size | 去重历史池腰斩, 避重覆盖静默下降 | 统一"空页收工 + offset 按实拿行数前进", 抽 `_paged_select` |
| COR-012 | `items.updated_at` 被定义为"人工决策时间"(TV 增量同步据此拉取), 但自动路径也写 status 触发它 | 定义 `db.py:780-809`; 程序写入 `app.py:277`, `app.py:439` | 每次自动校验/重生失败 | TV 把机器标记当人工决策收走, 下游归因失真 | 触发器加来源判据, 或自动路径写独立列 |
| COR-014 | bottom-k 采样后直接算两采样集 Jaccard, 长度差距大时系统性偏低 | `deskcore/fingerprint.py:59-90` (cap=200), `:87-90` | 正文 >~250 字且新旧稿长度差 2 倍以上 | 短稿逐字抄长稿一段时估计远低于 0.35, 硬闸放行 | 改标准 bottom-k (对 A∪B 取 bottom-k 后算命中率) |
| COR-018 | 注释断言 session "最多带最近 50 条", 实际只增不减 | `app.py:648` vs `app.py:696-719` | 持续审核通过 | prefix 无上界增长到 80% 窗口才封窗, 期间每批可达 160K token | 真滑窗, 或改注释 + 前移封窗阈值 |

### P2 / P3

| 编号 | 问题 | 位置 | 影响 | 修复 |
|---|---|---|---|---|
| COR-009 | `list_items.clear()` 在 INSERT **之前** | `db.py:1560-1563` vs `:1577` | 并发读回填旧快照, 30s 内看不到新版本 | clear 移到 insert 之后 |
| COR-010 | `res.data[0]` 直取, 0 行匹配 IndexError | `db.py:1702`, `2535`, `1035`, `1141`, `1232`, `1811`, `3536` | 红屏 IndexError 无可操作信息; `db.py:1291-1299` 已为另一函数处理过同问题却没推广 | 统一 `rows = res.data or []` 后判空 |
| COR-011 | 注释说"不重复标记同一 item", 代码无去重 | `app.py:274-277` | 同 item 被打 N 次, 每次全局 `list_items.clear()` | 加 `seen_item_ids` |
| COR-013 | `_caller` contextvar 有匿名默认值 | `deskcore/app.py:62-63`, `:76-85` | 中间件被绕过时 `user_id=None`, `open_project` 返回所有人的私有正负例 (`store.py:142` 的 `if user_id` 失效) | 默认值改抛异常的哨兵 |
| COR-016 | docstring 说"十个工具", 实际 11 个 | `deskcore/tools.py:6` vs `:330-342` | 模型也读这份 docstring | 改数字 |
| COR-017 | docstring 声称"over-fetch 1000 缓解", 实际 `.limit(50)` | `db.py:3169` vs `db.py:3180`; 调用方 `app.py:698` | 延迟审核的老 item 永远进不了 session 历史, 而文档说已缓解 | 真 over-fetch, 或加 `approved_at` 列 |
| COR-019 | `by_idx` 赋值后未使用 | `deskcore/core.py:566` | 死代码 | 删 |
| COR-020 | 解析用 `.get("content","")`, 报告用 `rule["content"]` | `validator.py:245` vs `:253/261/283/294` | 缺 content 键时 KeyError 冒泡到生成主流程 | 统一 `.get` |
| COR-021 | 全局剥反引号会改写正文 | `generator.py:485`, `:618` | 正文含代码块时被就地破坏 | 只剥首尾围栏 |
| COR-022 | `engine_results` 以 engine 名为键, 重复名字互相覆盖 | `generator.py:1747`, `:1763-1766` | 同一对象两份引用, 原地打标互相污染 (本文件 `:896-898` 已识破此陷阱) | `list(dict.fromkeys(engines))` |

---

## 5. 优越性问题清单 (SUP)

按 严重程度 + 频率 + 修复成本 排序。

| 编号 | 问题 | 位置 | 影响 | 修复 |
|---|---|---|---|---|
| SUP-001 | prompt cache 全用默认 5 分钟 TTL, 而 cache write 是 base input 的 **1.25 倍** | `generator.py:829`, `:835`, `:149` 均 `{"type":"ephemeral"}` 无 `ttl` | 批次间隔 >5min (正常节奏) 时每批都是 miss + 全量 write: prefix 20K 时**比不开缓存多花 25%**; session 长到 160K 时单批 cache_create ≈ $1.8 纯亏 | 加 `"ttl":"1h"` (写 2× / 命中 0.1×, 对 >5min 场景大幅更优); 或间隔久时不打断点 |
| SUP-002 | 余弦相似度纯 Python 逐元素循环 (768 维), `check_drafts` D×H 全量两两算, H 上限 4000 | `dedup.py:94-107`, `deskcore/core.py:398-441`, `store.py:295-338` (一次拉 4000×768 并逐个 `float()`) | 10 篇 × 4000 条 = 30.7M 乘加 + 3M `float()`, 单次数十秒, 同时占 uvicorn 线程池一槽 | 下推 pgvector (`draft_fp_embedding_idx` 已建於 `db.py:742-744`); Jaccard 的 SQL 版已存在 (`db.py:940-955`) |
| SUP-003 | 队列内存去重池同样 O(n·m) 纯 Python, 上限 2000 | `app.py:111`, `:551-561`, `dedup.py:110-171` | 每批额外 10-20s CPU, 全程占 GIL 拖慢主线程渲染 | 同上; 至少改 numpy 矩阵乘 |
| SUP-004 | `deskcore.list_projects` 每项目 3 次查询 = 3N+1 往返 | `deskcore/core.py:1078-1093` | 40 项目 = 121 次往返, 而这是模型最常调的第一个工具 | 一次查全部 memories 内存分桶; count 用 group by RPC |
| SUP-005 | `filter_soft_by_relevance` 每次调用发一次 embedding; 队列 worker 用同一 context 连调两次 | `memory.py:89`; 调用点 `app.py:1096` 与 `app.py:1099` | embedding 调用数与延迟翻倍 | 上层算一次 ctx_vec 传入 |
| SUP-006 | `max_tokens = max(2048, count*2500)`, `MAX_GENERATION_COUNT=50` → 125,000 | `generator.py:771`, `config.py:175` | 走官方端点时 count>26 直接 400, 整批 0 产出; 现在只在中转站不校验的前提下能跑 | 按 model clamp + 超限自动分片 |
| SUP-007 | `_make_client_cached` 是无上限 `st.cache_resource`, key 含每小时轮换的 token | `db.py:69-87` | 每用户每小时新增一个 Client + httpx 池永不回收; 且 `auth.py:234` 一人登出清掉**所有人** | 加 `max_entries`/`ttl`; 登出只清自己 |
| SUP-008 | `_MEMORY_UPSERT_LOCKS` 每个规则文本永久留一把 Lock | `db.py:1988-1998` | 无界字典增长 | weakref / LRU 上限 |
| SUP-009 | 5 个 `@_cache_data` 读函数的 key **不含租户维度**, 而 cache 是进程级共享、绕过 RLS | `db.py:1144`, `1581`, `2539`, `2592`, `2681` (对比 `2776`/`2868` 正确带了 user_id) | 若同一 project 下存在多用户 items (deskcore 用 service_role 写入时 `items.user_id` 与 `projects.owner_id` 无强制一致, 可构造), 缓存把 A 的数据端给 B | 一律把 user_id 加进 key |
| SUP-010 | `db.py` 3747 行含一个 862 行 DDL 字符串 (`:127-988`); `app.py` 5560 / `generator.py` 2530 / `memory.py` 2343 | 见左 | schema 与 `migrations/` 双写, README 自己承认"两边都要改", 漂移是时间问题 | DDL 挪进 migrations; app 按 page 拆包 |
| SUP-011 | job 队列全套已部署但只挂 `noop` handler | `worker.py:102-121` | 运维成本 + 认知负担, 且 R-018 的问题原样存在 | 落地 Phase 2 或明确下线 |
| SUP-012 | `_quick_gen_worker` 与 `_queue_worker_impl` 是两份重复编排 | `app.py:3076-3443` vs `app.py:962-1408`; `app.py:3134-3139` 自陈已漂移过一次 | 下次还会漂 | 抽 `generation_service.py` |
| SUP-013 | 用户可写正则被无超时 `re.search` | `validator.py:290`; 写入侧只 `re.compile` 校验 | `(a+)+$` 类灾难性回溯挂死线程 | `re2`/带 timeout 的 regex, 或禁嵌套量词 |
| SUP-014 | `url_to_b64` 对任意 URL 发请求, 无 scheme/host 白名单, 跟随重定向, 先全量读入再判 20MB | `image_handler.py:249-260` | SSRF (云元数据端点); 内存峰值 | https + 固定域名白名单; `stream=True` 边读边计数 |
| SUP-015 | `push_items_as_text` **无条件返回 True** | `exporter.py:420-425` (对比 `:386` 检查了 code) | 飞书返 200 + 非 0 code 时仍报"推送成功" | 复用 code 检查 + 分片 |
| SUP-016 | `claim_one_job` / `batch_item_counts` 无 `SET search_path`, 而两个 deskcore 函数专门加了 | `db.py:542-563`, `:641-673` vs `:836`, `:909` | advisor 告警; 名字解析可被调用方 search_path 影响 | 两处补上并同步 migrations |
| SUP-017 | `versions_embedding_idx` 无 `lists` 且在空表上建 ivfflat | `db.py:277-278` (对比 `:742-744` 是对的) | 召回极差且不自愈 | 数据到位后 REINDEX, 或改 HNSW |
| SUP-018 | `deskcore/vocab.py` 硬编码复制了 generator 三个常量池当 fallback | `vocab.py:94-98` vs `generator.py:1259-1279`; `vocab.py:121-134` vs `generator.py:1985+` | 改 generator 后, 裸环境 (CLI/CI) 与线上抽到不同组合空间 | fallback 改为报错退出, 或常量下沉零依赖模块 |
| SUP-019 | 信号处理器里做 stdout I/O; 退避用不可中断 `time.sleep` | `worker.py:83-85`, `clients.py:179` | SIGTERM 宽限期内无法响应 | 处理器只 set event; 退避改 `stop_event.wait` |
| SUP-020 | UI 标签写进 `versions.token_usage` 计费列 | `generator.py:1977-1980` | 任何按 token_usage 聚合成本的查询都要额外排除 | 单独列/表 |
| SUP-021 | `delete_batch` 手动 4 步删, 而外键本就是 CASCADE | `db.py:1191-1217` vs `db.py:172`, `:193`, `:254` | 3 次多余往返; 中途失败留半删状态 | 只删 batch |
| SUP-022 | `queue_embeddings`/`queue_titles` 是线程内存字典, 重启即空 | `app.py:1018-1025` (`db.py:710-713` 的注释已点名) | 跨批避重从头开始; deskcore 建了 `draft_fingerprints` 但 Streamlit 路径没接 | Streamlit 路径也读 `draft_fingerprints` |
| SUP-023 | `push_to_feishu(webhook_url=...)` 参数化 URL | `exporter.py:344` | 若有 UI 入口即 SSRF | **待人工确认** 是否存在 UI 传参路径 |
| SUP-024 | 全仓 0 个测试文件 | 全仓 | 本次 22 条正确性问题里大部分都是"一个单测就能钉死"的类型 | 见 §7 |

---

## 6. 健壮性风险清单 (ROB)

| 编号 | 风险 | 触发 | 影响范围 | 位置 | 可重试/幂等/降级 | 修复 | 验证 |
|---|---|---|---|---|---|---|---|
| ROB-001 | 进程重启 → 正在跑的队列整批蒸发, DB 留半成品 batch, UI 连"曾经在跑"都看不到 | 重启/发布/OOM/闲置回收 | 全部生成 | `app.py:3722-3727`, `app.py:3720` | 均否 | 落地 `worker.py:114-121` 的 Phase 2 | 生成中途 kill -9 后重启 |
| ROB-002 | JWT 中途过期 (= COR-001) | 队列 >1h | 剩余全部 plan | `app.py:3724`, `db.py:69-99` | 否 (错误已被 catch 成字符串) | 同 COR-001 | 缩短 JWT TTL |
| ROB-003 | deskcore 未配鉴权 = 全放行, 而 `/health` 无论 ok 真假都返 **HTTP 200**, Railway healthcheck 照样判健康 | 部署漏配 key | 公网任何人可读写全部租户数据 (服务持 service_role 绕 RLS) | `deskcore/identity.py:122-124`, `deskcore/app.py:140-205`, `railway.json:9` | 无 | `ok=False` 时返 503; 或生产环境 `resolve()` 直接抛 | 不设 key 起服务后 curl |
| ROB-004 | `check_drafts` 同步跑数十秒占满线程池 → `/health` 超时 → 平台重启容器 → 正在跑的调用全断 | 大项目 | 全服务 | `deskcore/core.py:371-441`, `store.py:295-338`; `deskcore/app.py:232-236` 的注释描述过同款事故 | 无 | 下推 pgvector | 5000 条指纹并发 5 个 check + 打 /health |
| ROB-005 | MCP 工具以**同步函数**注册进 FastMCP; REST 路由专门用 `run_in_threadpool` 避开了这一点, MCP 路径没有 | 任一慢工具经 `/mcp` 调用 | 全服务假死 | `deskcore/app.py:288-308` (`def wrapper`) vs `:237` | 无 | wrapper 改 `async def` + `await run_in_threadpool` | **待人工确认**: 查装机 MCP SDK 是否在事件循环里直接调同步工具; 或起服务后 /mcp 调 sleep 工具同时打 /health |
| ROB-006 | 自动学习静默停写 (= COR-003) | 笔记 >~2-4KB | 全部自动学习 | `memory.py:904`, `:820-822` | 无告警 | 见 COR-003 | 造长笔记 |
| ROB-007 | RPC 不存在时自动降级为非原子, 只在返回值塞一行 warning 字符串 | migrations/001 未跑 | 并发发牌撞车 / 并发 commit 双双入库, 用户看到"成功" | `store.py:240-244`, `:458-461`, `core.py:313-315`, `:633-636` | 幂等性丢失 | `/health` 检测 RPC 存在性并置 ok=False; 或降级路径拒写 | 未跑迁移的库上调 draw_angles |
| ROB-008 | 停止只在 plan 边界生效, 不中断进行中的 LLM 与重生循环 | 用户点停止 | 已发出调用继续计费 | `app.py:1028-1030` | — | `stop_event` 传进 `generate_batch`/`_try_regen_one` | 第 2 个 plan 刚开始就停 |
| ROB-009 | `sweep_dead_jobs` 用 `.lt("heartbeat_at", cutoff)`, NULL 行永不被扫到 | heartbeat_at 为 NULL 的 claimed/running 行 | 僵尸 job 永久占位 | `db.py:3720-3727` | 无 | `.or_(is.null, lt)` + NULL 时用 claimed_at | 手工置 heartbeat_at=NULL |
| ROB-010 | UI 取消不中断 handler | 取消长任务 | 算力照花, 结果丢弃 | `db.py:3560-3571`, `worker.py:161-168` | — | handler 周期性 re-read status | 入队长任务后取消 |
| ROB-011 | `store.fingerprints` 一次 4000×768 float + ngram 数组; `legacy_versions` 一次 1000 条全文 | 大项目 | 百 MB 级峰值 → 容器 OOM → 重启 | `store.py:295-338`, `:341-383`, `core.py:705` | 无 | 流式/分块或下推 SQL | memory_profiler 跑 backfill |
| ROB-012 | 日志无限增长: 每 LLM 调用一条诊断, `BATCH_METRICS` 含完整 meta | 常态 | 平台日志额度 | `telemetry.py:278-288`, `generator.py:203-261` | — | 日志级别开关; meta 只打摘要 | 跑 100 批看体积 |
| ROB-013 | 连接池/FD 泄漏: SUP-007 + `_fresh_auth_client` 每次新建且从不关闭 | 长期运行 | Too many open files | `db.py:69-87`, `auth.py:136-155` | — | client TTL/上限; auth client 用完关闭 | lsof 观察 |
| ROB-014 | 双标签页并发刷 refresh token, 轮换模式下互踢 | 用户开两页 | 其中一个被踢回登录 | `auth.py:344-358`, `:290-305` | 有 fallback 但同时刷仍互踢 | 加轻量互斥或容忍 Already Used 后重读 cookie | 两页同时在 ttl<60s 刷新 |
| ROB-015 | Supabase 免费版闲置暂停 → 后台线程把每个 plan 变成一行错误继续往下跑, 把整队列烧掉 | Supabase 暂停 | 整队列失败 + LLM 费用照花 | `app.py:1392-1398` | 无熔断 | 连续 N 个同类错误 fail-fast | 暂停 Supabase 后启动队列 |
| ROB-016 | 馆员超时 8s × 每 plan 串行阻塞 | 馆员慢/挂 | 10 plan = 最多 80s 纯等待 | `librarian_client.py:99`, `config.py:87` | fail-open ✓ | 熔断: 连续 2 次超时后本队列剩余跳过 | toxiproxy 加 30s 延迟 |
| ROB-017 | `append_session_messages` 读 max + 1 + insert, 撞唯一键重试 3 次后返 0 | 并发 append 同 session | 本批 approved 内容静默不进 session | `db.py:3310-3363` | 幂等 ✓ 但失败无补偿 | 用 RPC 在事务里算 turn_idx | 两线程同时同步 |
| ROB-018 | UPDATE 0 行被当成功: `consume_angle` 已修, `seal_session`/`update_job_progress`/`save_*_draft` 仍只看"没抛异常" | 行不存在/RLS 拦截 | 封窗失败 → session 继续膨胀 | `db.py:3428-3448`, `:3589-3599`, `:1705-1768`; 对比已修的 `store.py:282-287` | — | 统一检查 `res.data` | 传不存在的 session_id 调 seal |
| ROB-019 | 环境变量缺失的可见性不一致: service_role 缺失会抛 (好), GOOGLE_API_KEY 缺失只静默降级 | 部署漏配 | 查重从三信号退成两信号, Streamlit 侧用户不知道 | `clients.py:92`, `dedup.py:52-55`; deskcore 侧 `/health` 有回显 `deskcore/app.py:187-192` | 有降级, 可见性弱 | Streamlit 侧加启动自检面板 | 清掉 GOOGLE_API_KEY 跑一批 |
| ROB-020 | 连接错误重试时请求可能已被服务端处理 | 响应丢失 | 同一批被生成并计费多次; `_topup_failed_slots` 还会再补一刀 | `clients.py:186-223`, `generator.py:1486` | 不幂等 | 传 idempotency_key; 区分 5xx 与连接错误 | toxiproxy 在响应阶段切断 |
| ROB-021 | 图片下载无流式上限 | 恶意/超大 URL | 内存暴涨 | `image_handler.py:256-258` | — | stream + 分块累计 | 指向 1GB 文件 |
| ROB-022 | MCP SDK 缺失只打一行 warning, 服务照常起, `/health` 全绿 | 依赖装漏 | `/mcp` 404 而健康检查通过, WorkBuddy 侧表现为"连不上" | `deskcore/app.py:114-118`, `:252-258` | — | `/health` 回显 `mcp_mounted` 并计入 ok | 卸载 mcp 后打 /health |
| ROB-023 | `generation_sessions` 的 `archived` 状态全仓无写入路径; `session_messages` 只增不删 | 长期使用 | 表无界增长 | `db.py:395-399` CHECK 有 archived, grep 无写入 | — | 加归档/清理 | 看 sealed session 计数 |

---

## 7. 测试与验证方案

### 7.1 单元测试 (无网络, 全假件)

工具: `pytest` + 一个 `FakePostgrest` (链式 `.table().select().eq()...execute()` 返回预设 data)。
`AW_DISABLE_ST_CACHE=1` 让 `db.py` 缓存 shim 退化为透传 (`db.py:37-39` 已支持)。

| 目标 | 函数 | 场景 | 预期 |
|---|---|---|---|
| COR-002 | `app._save_batch_results` | bulk insert 返回逆序 / 少 1 行 | 逆序仍正确对应; 少行时抛错不静默丢 |
| COR-004 | `db.create_version` | 两线程并发同 item | version_num 无重复 |
| COR-005/006 | `store.existing_fingerprint_version_ids`, `legacy_versions` | 假 client 每页 1000 行共 2500 行 | 返回 2500 |
| COR-007 | muted 判定 | naive / +00:00 / 7 位微秒 | 三者都判"已静音" |
| COR-008 | `db._collect_recent_canonical_versions` | max-rows=100, limit=150 | 拿满 150 |
| COR-009 | `db.bulk_create_initial_versions` | clear 与 insert 之间插一次读 | 读不到旧快照 |
| COR-010 | `update_item_status` / `set_item_example_label` | `res.data == []` | 返 None 或业务异常, 不 IndexError |
| COR-014 | `fingerprint.jaccard` + `ngram_hashes` | 600 字新稿 ⊂ 3000 字旧稿 | Jaccard ≥ 0.35 (当前 <0.1) |
| COR-020 | `validator.check_hard_rules` | 规则 dict 无 content 键 | 不抛 KeyError |
| COR-021 | `generator._parse_copy_json` | body 内含代码块 | 代码块原样保留 |
| COR-022 | `generator.generate_batch` | `engines=["claude","claude"]` | slot 内两个 version 是独立对象 |
| SUP-006 | `ClaudeEngine._make_params` | count=50 | max_tokens ≤ 模型上限 |
| 护栏 | `config.get_pricing` / `get_context_window` | `claude-opus-4-10`, `-thinking`, `claude/` 前缀, 未知 id | 不误配 4-1 档 (R-042 已修, 加测试钉死) |
| 护栏 | `memory._dedup_calibration_lines` | 超 4000 字 | 丢最旧 + dropped_sink 非空 |
| 护栏 | `validator._parse_rule` | `必须包含"不要熬夜"` / `禁止使用"必须包含"话术` / `标题不要太长` / `控制在20字以上` | required / forbidden / None / None |

### 7.2 集成 / 端到端

| 场景 | 断言点 |
|---|---|
| 队列全链路 (1 项目 × 2 plan × 3 篇, 双引擎) | items=3; 每 item 2 version; batch_metrics 1 行/批; session_messages 不重复; `token_totals.by_source_model.main` 存在 |
| deskcore 闭环 | `open_project.counts.hard_rules>0` → `draw_angles.atomic_reservation=true` → 抄袭稿必须 reject 且 `decided_by` 正确 → `commit_drafts.rejected` 非空 |
| deskcore 冷启动 | 未 backfill 时 `summary.empty_history_warning` 必须出现 |
| 记忆 → prompt | hard 规则进 p0; 静音后不进 |
| 迭代 → 调校笔记 | calibration_notes 增行 + audit 表有 before/after |
| **CI 扩充** | 现有 `ci.yml` 已起 TestClient 打 `/health`; **再加** 真发一次 MCP initialize 握手 + 一次慢工具与 `/health` 的并发 (覆盖 ROB-005) |

### 7.3 并发 / 压力

| 目标 | 手法 |
|---|---|
| COR-004 | 8 线程同 item 调 `create_version`, 断言 version_num 无重复 |
| ROB-017 | 8 线程并发 append 同 session, 断言总行数正确且 turn_idx 连续 |
| COR-015 / RPC 串行化 | 两进程同时 `draw_angles(P, n=5)`, 断言 10 个 angle_key 互不相同 |
| SUP-002/003 | `pytest-benchmark`: H=1000/2000/4000 的 `check_drafts` 曲线, 目标 P95 < 2s |
| ROB-004 | locust 并发 20 个 check_drafts + 每秒打 /health, 断言 /health P99 < 1s |
| upsert_memory | 16 线程同 content upsert, 断言 frequency==16 |

### 7.4 故障注入 / 混沌

| 风险 | 手法 |
|---|---|
| ROB-001 | 队列第 2 个 plan 时 kill -9 → 重启 → 断言无孤儿 item, 且有机制看出"批次中断" |
| ROB-002 | JWT expiry 调到 120s 跑 5 分钟队列 |
| ROB-003 | 不设任何 `DESKCORE_*` 起服务, `curl -XPOST /tool/commit_drafts` 应 401 |
| ROB-007 | 在未跑 migrations/001 的库上跑全套 deskcore |
| ROB-015/016 | toxiproxy 对 Supabase 注 100% timeout / 对馆员注 30s 延迟, 断言熔断而非烧完 |
| ROB-020 | toxiproxy 在响应阶段切断 Anthropic, 观察是否重复计费 |
| ROB-013 | 循环登录/登出 500 次, lsof 断言 FD 不单调增长 |
| SUP-013 | 存 `(a+)+$` 后生成 5000 字正文, 断言有超时保护 |

### 7.5 静态分析

| 工具 | 规则 | 门禁 |
|---|---|---|
| `ruff` | `E,F,B,S,ASYNC,SIM,RUF`; 重点 `BLE001`/`S113`/`S310` | CI 阻断 |
| `mypy --strict` | 先只开 `config/clients/dedup/validator/logger_utils/deskcore/fingerprint` (零/轻依赖) | 渐进 |
| `bandit` | `B310` URL scheme · `B113` requests 无 timeout | 补 SUP-014 |
| **`semgrep` 自定义 4 条 (本仓最高价值)** | ① `res.data[0]` 无空检查 (COR-010) ② `.limit(` 后无翻页 (COR-005/006) ③ `len(page) < page_size` 终止 (COR-008) ④ `unsafe_allow_html=True` 附近未 escape 的 f-string | CI 阻断 |
| `sqlfluff` (postgres) | `migrations/*.sql` | 提交前 |
| Supabase `get_advisors` | `function_search_path_mutable` (SUP-016) · `unindexed_foreign_keys` · `auth_rls_initplan` | 每次迁移后 |
| `pip-audit` | 依赖 CVE | 周任务 |
| `vulture` | 死代码 (COR-019, `generator.py:1301-1364` 的 `_assign_slot_coordinates` 家族) | 参考 |

### 7.6 监控与告警

| 指标 | 来源 | 阈值 |
|---|---|---|
| `queue_worker_unexpected_exit` | `app.py:943` | 任意 1 次 |
| 单 plan `total_ms` | BATCH_METRICS | P95 > 180s |
| `phase_ms.embedding / total_ms` | 同上 | > 0.35 (SUP-002/003 恶化) |
| `cache_create / (cache_create + cache_read)` | 同上 | > 0.5 持续 1h (SUP-001) |
| 单批 `cost_usd` | 同上 | > $1.0 |
| `hard_rule_violations / count` | 同上 | > 0.2 |
| `regen_attempts / regen_success` | 同上 | > 3 (重生空转) |
| `embedding_missing` | status/metrics | 日均 > 0 |
| `upsert_memory_cas_exhausted` / `calibration_cas_exhausted` | telemetry | 任意 1 次 |
| `calibration_save_error` | telemetry | 任意 1 次 (COR-003 哨兵) |
| `flywheel_librarian_unavailable` 比例 | telemetry | > 30% / 1h |
| `job_sweep_recovered` | telemetry | > 0 |
| jobs pending 深度 / 最老 pending 年龄 | DB | 年龄 > 10 min |
| deskcore `/health` 的 ok / vendored_vocab.ok / auth.ok / embeddings.ok | HTTP 探针 | 任一 false 立即 (**并把 ok=false 改返 503**, ROB-003) |
| `check_drafts` P95 / `history_truncated` 出现率 | 日志 | P95 > 3s; truncated > 0 |
| `draft_fingerprints` 行数/项目 | DB | > 3500 (逼近 4000 上限) |
| Supabase 连接数 / FD | 平台 | 单调增长 (ROB-013) |

---

## 8. 待人工确认项

| # | 事项 | 为什么静态定不了 | 验证手段 |
|---|---|---|---|
| A1 | ROB-005: MCP SDK 对**同步**工具函数是在事件循环里直接调, 还是丢线程池 | 本环境未装 `mcp` 包, 且行为随 SDK 版本变 | `python -c "import inspect, mcp.server.fastmcp.utilities.func_metadata as m; print(inspect.getsource(m))"` 看 `call_fn_with_arg_validation` 的同步分支; 或起服务后 `/mcp` 调一个 sleep 工具同时打 `/health` |
| A2 | COR-003 / ROB-006: 目标 Supabase (Kong/nginx) 的 URL 与 header 上限具体是多少字节 | 取决于部署侧网关配置 | 直接构造一个 3000 中文字的 `.eq()` 过滤发一次, 看是否 414/400 |
| A3 | COR-005/006/008: 该 Supabase 项目 Dashboard → API → **Max rows** 的实际值 | 全仓多处按 1000 假设, 但可被改 | Dashboard 查看; 或对一张 >1500 行的表做一次无 range 的 select 数行数 |
| A4 | SUP-023: `push_to_feishu(webhook_url=...)` 是否有 UI 传参路径 | 需要人工确认产品意图 (是否允许用户自填 webhook) | grep 调用点 + 确认产品设计 |
| A5 | ROB-020 / B20: LLM 重试在"请求已到达但响应丢失"时是否真的重复计费 | 取决于中转站行为 | 对账一次: toxiproxy 在响应阶段切断, 对比中转站账单条数 |

---

## 9. 下一步建议 (修复路线图)

### 第 1 批 · 止血 (1-2 天, 收益/成本比最高)

按顺序:

1. **ROB-003** — `/health` 在 `ok=False` 时返 503; `identity.resolve` 在未显式设 `DESKCORE_DEV=1` 时拒绝 dev 全放行。**这是唯一一个"配错就等于把库开放到公网"的问题。**
2. **COR-003 / ROB-006** — 调校笔记 CAS 改 hash witness。当前自动学习可能**已经在生产上静默停摆**, 且没有任何告警。顺手把 `memory.py:820-822` 的静默 `return None` 改成写一条 `status.warnings`。
3. **COR-007** — 一行改动: `deskcore/store.py:104-111` 改调 `db.is_memory_muted_now`。被静音的硬规则正在被注入。
4. **COR-010** — 统一 `res.data or []` 判空。7 处, 纯机械, 消掉一整类红屏。
5. **COR-005 / COR-006** — deskcore 三个无翻页查询补翻页。backfill 的幂等性和覆盖面都建立在它们之上。

### 第 2 批 · 数据一致性与并发 (3-5 天)

6. **COR-002** — bulk insert 顺序假设改成显式匹配。这是唯一一条"正文永久丢失且零告警"的路径。
7. **COR-004** — `versions` 加 `UNIQUE(item_id, version_num)` + 撞键重试 (需要一条迁移, 两边都改)。
8. **COR-008** — 抽 `_paged_select` helper, 把 5 个"短页收工"点统一掉, 同时上 semgrep 规则②③防复发。
9. **COR-022 / COR-011 / COR-009 / COR-020 / COR-021** — 一批小改, 各自独立。
10. **ROB-009 / ROB-018** — sweeper 的 NULL 心跳; UPDATE 0 行的统一检查。

### 第 3 批 · 性能与成本 (1 周)

11. **SUP-001** — cache TTL 改 1h。**这是纯配置改动, 但按当前 session 规模, 单项就能显著降本。** 改之前先用监控指标 `cache_create/(cache_create+cache_read)` 量出基线。
12. **SUP-002 / SUP-003 / ROB-004 / ROB-011** — 相似度比对下推 pgvector。索引 (`db.py:742-744`) 和 SQL 版 Jaccard (`db.py:940-955`) 都已经在库里了, 主要是把 Python 侧换掉。同时解决 deskcore 的线程池饥饿与 OOM 风险。
13. **SUP-004 / SUP-005** — 两个 N+1 / 重复调用, 各半天。
14. **SUP-007 / SUP-008 / ROB-013** — 缓存与锁池加上限, 消掉长跑泄漏。

### 第 4 批 · 结构性 (2-4 周, 但决定长期上限)

15. **ROB-001 / ROB-002 / SUP-011 / SUP-012** 是**同一件事**: 把 `_queue_worker_impl` 与 `_quick_gen_worker` 抽成不依赖 streamlit 的 `generation_service.py`, 注册成 `worker.py` 的 `generate_batch` / `quick_gen` handler。这一步同时:
    - 消掉进程重启丢批次 (ROB-001)
    - 消掉 JWT 中途过期 (ROB-002, worker 走 service_role)
    - 让已经建好的 job 队列真正派上用场 (SUP-011)
    - 消掉两份重复编排 (SUP-012)
    `worker.py:114-121` 已经写好了接入点和前置条件。
16. **SUP-024** — 在第 15 步之前先把 §7.1 的单测建起来, 否则这么大的搬迁没有安全网。
17. **SUP-010** — DDL 从 `db.py` 挪进 `migrations/`, 消掉双写。
18. **COR-014** — bottom-k 估计改标准形式。需要先用真实语料量化当前漏检率, 再决定是改算法还是调阈值。
19. **COR-015** — 确定 deskcore 的多租户模型 (团队共享 vs 按 owner 隔离), 然后给写操作补校验。这是产品决策, 不是纯技术问题。

### 不建议现在做

- 大规模重命名 / 格式化: 这份代码的注释密度极高且带 review 编号追溯, 大改会毁掉这条线索。
- 把 `app.py` 一次性拆完: 先按第 15 步抽出生成服务, 剩下的 UI 部分等有测试后再动。
