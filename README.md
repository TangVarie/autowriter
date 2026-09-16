# autowriter

BYWOOD（芭梧文化传媒）小红书种草文案的生成与写作台系统。

一句话分工：**Streamlit 工作台**做人工审稿与运营界面，**worker** 跑后台生成队列，
**deskcore** 把写作台的能力外置成 MCP 工具服务，给 WorkBuddy / Claude Code / CodeBuddy 直接调。

> 方向上 deskcore 是重心：团队基本弃用 Streamlit 工作台转用 WorkBuddy，能力外置之后
> 推理归调用方模型，deskcore 只做轻量数据操作。**但 Streamlit 停服尚未执行**——
> 前置的归档链路取舍还没定，见 [`docs/deskcore-runbook.md`](docs/deskcore-runbook.md) §3 与待办 P1 #5。

## 三个可部署单元

| 单元 | 入口 | 部署配置 | 状态 |
|---|---|---|---|
| Streamlit 工作台 | 根目录 `app.py` | `Procfile` 的 `web:` | 在跑，计划停服（未执行） |
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
