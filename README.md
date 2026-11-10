# autowriter

> **评论线云端交接**：[写作台接手说明及完整材料入口](docs/comment-handoff-online-2026-10-15.md)，可直接在GitHub阅读。

> **接手当前进度（2026-10-15）先读 [HANDOFF.md](HANDOFF.md)**：本轮检查、已合并改动、生产验证、未完成事项和下一步执行顺序。

BYWOOD（芭梧文化传媒）小红书种草文案的生成与写作台系统。

一句话分工：**Streamlit 工作台**做人工审稿与运营界面，**worker** 跑后台生成队列，
**deskcore** 把写作台的能力外置成 MCP 工具服务，给 WorkBuddy / Claude Code / CodeBuddy 直接调。

> 方向上 deskcore 是重心：团队已弃用 Streamlit 工作台转用 WorkBuddy，能力外置之后
> 推理归调用方模型，deskcore 只做轻量数据操作。**Streamlit 工作台 2026-10-16 已停服**（owner 在 Railway 停掉；
> 按 TV D-072 停服不删仓，Supabase 一行不动），见 [`docs/deskcore-runbook.md`](docs/deskcore-runbook.md) §3 与待办 #5。

## 三个可部署单元

| 单元 | 入口 | 部署配置 | 状态 |
|---|---|---|---|
| Streamlit 工作台 | 根目录 `app.py` | `Procfile` 的 `web:` | 2026-10-16 已停服（Railway 上停掉；代码和 `Procfile` 留着，不删仓） |
| 后台 job worker | `worker.py` | `Procfile` 的 `worker:` | 独立 service，需单独配 `SUPABASE_SERVICE_ROLE_KEY`（R-018）。⚠️ Streamlit Cloud 忽略 `Procfile`，那种部署要另起常驻主机跑 `python worker.py` |
| deskcore MCP 服务 | `deskcore/app.py` | `deskcore/railway.json` | 已上线 Railway，`https://autowriter-production.up.railway.app` |

## 代码布局

业务模块**平铺在仓库根目录**，没有包结构、没有 `pyproject.toml`：
`app.py` / `db.py` / `generator.py` / `memory.py` / `generation_service.py` / `dedup.py` /
`exporter.py` / `validator.py` / `projects.py` / `auth.py` / `config.py` 等。
`tests/conftest.py` 自己把仓库根塞进 `sys.path`，测试才 import 得到它们。

**`deskcore/` 是唯一的包。** 注意它里面也有一个 `app.py`（FastAPI 服务），
和根目录那个 Streamlit `app.py` 同名、两回事。

## 本地跑

```bash
pip install -r requirements.lock -r requirements-dev.txt   # 跑测试
python -m pytest tests/ -q

pip install -r deskcore/requirements.txt                   # 再装这份才起得了 deskcore 服务
python -m deskcore.cli selftest                            # 不连库不联网, 验查重/发牌/词表
```

四个 requirements 文件各有各的场合，别混：

| 文件 | 干什么 |
|---|---|
| `requirements.txt` | 源清单，每个依赖锁到下一个 major 之前（R-017） |
| `requirements.lock` | `uv pip compile` 出的**部署**闭包，线上装这份 |
| `requirements-dev.txt` | 只有 pytest，**部署环境不要装** |
| `deskcore/requirements.txt` | 相对主清单的增量：fastapi / uvicorn / mcp |

⚠️ `tests/sql_parity_check.py` **不是 pytest 用例**，它需要一个活的 PostgreSQL，由 CI 单独起一步跑。
本地跑法写在该文件末尾。

## 备用中转站 (D-148, 可选)

aw 里直接调 Claude 的只有 **Streamlit 工作台**和 **worker.py**（两个现在都停着）。deskcore 不调 Claude，不需要下面这些变量。
机制在 `relay_failover.py`（和 truth-vault、Jev 是同一份文件），接线在 `clients.py`。

**启用**：给 Streamlit 工作台 / worker 加两个变量（三个仓同名）：`ANTHROPIC_BASE_URL_BACKUP`（备用站地址）和 `ANTHROPIC_API_KEY_BACKUP`（备用站 key）。两个都填了才算启用，缺一个等于没配，行为和现在完全一样。以后把它们重新开起来时，在 Railway 对应服务的 Variables 里加（改变量会自动重新部署）；跑在 Streamlit Cloud 上就加进 secrets。地址也当 secret 管，别写进代码、文档或公开日志。备用站必须认同样的模型名（claude-sonnet-5-5 / claude-haiku-5-5 / claude-opus-5-5，面板上选 Thinking 的话还有 claude-opus-5-5-thinking），不认会回 404/503。备用站和 truth-vault 用的是同一个的话，可以跑一次 truth-vault 的 Actions → gateway-probe，`target` 选 `backup`，看备用站 `http=200`。

**自动切换**：配好后每次调用先打主站，但主站只给一次机会且限时 60 秒（流式生成是 60 秒没有新字节）。主站超时（含流式写到一半卡住或断开）、连不上、429、5xx（含 520/522/524/529）、401/402/403（多半是余额或 key 失效）、404（多半是不认模型名）→ 这次立刻改打备用站，备用站照旧走原来的重试（`with_anthropic_retry`：最多 6 次，2→60 秒退避）。400 这类请求本身有问题的不切（中转站不认 cache ttl 的那个 400 照旧按 SUP-001 降级重拼）；中转站敏感词拦截（sensitive_words_detected）也不切。主站连续 2 次失败，或非流式调用连续 2 次慢过 30 秒 → 熔断 20 分钟：这期间所有调用直接走备用站；到点放一个调用去试主站，好了切回，不行再熔断 20 分钟。同一台机器上的进程共用熔断状态。

**手动切换**：变量 `RELAY_FORCE`：`backup` = 只走备用站；`primary` = 只走主站、不切换，和没配备用站时一模一样；留空或删掉 = 自动。在 Streamlit / worker 服务的 Variables（或 Streamlit secrets）里设；`config.py` 只在进程启动时读一次，改了要重启（Railway 改变量会自动重新部署）。备用站没配时设 `backup` 不生效，日志会提示一行。

**怎么看走的是哪条**：这两个进程没有 `/health`，看日志：第一次切换打一行 `relay: primary failed (HTTP 503); failing over to backup`，熔断打 `relay: breaker open for 1200s ...`，恢复打 `relay: breaker closed (primary ok again)`，进程结束打 `relay summary: {...}`（`mode`、`primary_ok` / `primary_slow` / `primary_fail`、`backup_ok` / `backup_fail`、`breaker_open_skips`（熔断期间直接走备用的次数）、`breaker`（closed 正常 / open 熔断中 / half_open 该试主站了））。代码里调 `clients.relay_status()` 拿同样的信息（`relay_mode` / `relay_backup_configured` / `relay_force` / `relay_breaker` / `relay_counts`）。这些日志和字段只出现 primary / backup、状态码、错误类型、耗时和次数，从不出现地址、key 或中转站返回的报错原文。

**代价**：备用站另外计费；主站超时那次主站可能已经扣了钱；两个站的 prompt cache 不通用，切过去后头几次没有缓存命中。已知不准：备用站的调用在 batch_metrics / token 面板里仍按主站的 `RELAY_PRICE_MULTIPLIER` 计价；备用站要是不认 cache ttl，本进程的 1h 缓存会对两个站一起关掉（退回 5 分钟，多花钱，不会挂）。老问题照旧：`GenerationResult.error` 和 `anthropic_cache_ttl_rejected` 遥测会带出当次那个站的报错原文，另行处理。可调（一般不用动）：`RELAY_PRIMARY_TIMEOUT_S`（默认 60）、`RELAY_SLOW_S`（默认 30，0 = 不按慢判）、`RELAY_BREAKER_FAILS`（默认 2）、`RELAY_BREAKER_COOLDOWN_S`（默认 1200）。

## 文档索引

| 在哪 | 是什么 |
|---|---|
| [`deskcore/README.md`](deskcore/README.md) | MCP 内核：结构、纪律、运维命令 |
| [`skills/README.md`](skills/README.md) | 挂给 AI 助手的 skill，以及运营端怎么装 |
| [`migrations/README.md`](migrations/README.md) | 增量 SQL：每个迁移不跑会怎样 |
| [`deskcore/vendor/README.md`](deskcore/vendor/README.md) | 从 truth-vault vendor 过来的词表怎么更新 |
| [`docs/deskcore.md`](docs/deskcore.md) | deskcore 的完整设计与对接参数 |
| [`docs/deskcore-runbook.md`](docs/deskcore-runbook.md) | 上线手册 / 跨仓对接 / 待办 / 已知不一致 |
| [`docs/audit-2026-08-23-full.md`](docs/audit-2026-08-23-full.md) | 全量代码审计 |
| [`docs/R-032-flywheel-librarian-consumer.md`](docs/R-032-flywheel-librarian-consumer.md) | 接 TV 飞轮馆员（pull 侧消费） |

跨仓决策记在 truth-vault：`DECISIONS.md` / `docs/10-sister-repo-followups.md`。

## 一条通用告诫

**这几份文档里的点位数字（表行数、工具个数、跑到第几个迁移）都会漂**，
历史上已经漂出过好几组互相矛盾的版本。要当前状态就去问系统，别照抄文档：

```bash
curl -s https://autowriter-production.up.railway.app/health   # 服务与依赖的实时状态
python -m deskcore.cli doctor                                 # 库跑到第几个迁移了(要连库)
```
