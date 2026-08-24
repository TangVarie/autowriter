# 全量代码审计 · 2026-08-23

范围: 仓库全部非第三方、非生成源码 (21,271 行 Python / 25 个文件)。
唯一 vendored 目录 `deskcore/vendor/` 只做校验和核对, 不审内容。

所有结论都带 `文件:行号`。无法靠静态阅读定死的, 标 **待人工确认** 并写明验证手段。

---

## 0. 修复进度

| 批次 | 条目 | 状态 |
|---|---|---|
| 第 1 批 · 止血 | ROB-003 · COR-003 · COR-007 · COR-010 · COR-005/006 | ✅ **已修**(见下) |
| 第 2 批 · 数据一致性 | COR-002 · COR-004 · COR-008 · COR-009 · COR-011 · COR-020 · COR-021 · COR-022 · ROB-009 · ROB-018 | ✅ **已修**(见 §0.2) |
| 第 3 批 · 性能与成本 | SUP-001 · SUP-002 · SUP-003 · SUP-004 · SUP-005 · SUP-007 · SUP-008 · ROB-004 · ROB-011 · ROB-013 | ✅ **已修**(见 §0.3; codex review 的 7 条修正见 §0.4) |
| 第 4 批 · 结构性 | ROB-001 · ROB-002 · SUP-011 · SUP-012 · SUP-024 · SUP-010 · COR-014 · COR-015 | 🚧 **进行中**(见 §0.5) —— SUP-024 ✅ / 其余 7 条待做 |

### 0.1 第 1 批(止血)的实现说明与**两处与本报告的偏差**

行号引用仍指审计当时的版本; 修复后的位置以 CI 的 `审计第一批止血回归` 一步为准。

| 条目 | 怎么修的 |
|---|---|
| ROB-003 | `deskcore/identity.py:resolve` 改 fail-closed: 未配 key 一律 401, 免 key 跑要显式 `DESKCORE_ALLOW_ANONYMOUS=1`。`/health` 增回显 `config.anonymous_allowed`, 并把 `auth_health()` 从两次调用收敛成一次(否则 `ok` 与 note 可能对不上) |
| COR-003 | 新增 `update_calibration_notes_cas` RPC(`migrations/002_calibration_cas.sql` + `db.py::CREATE_TABLES_SQL` 两边同步), witness 压成 md5 进 body。`memory.save_calibration_notes` 改走它; RPC 未部署时退回 `_legacy_cas_update` 并埋 `calibration_cas_rpc_missing` |
| COR-007 | `deskcore/store.shared_memories` 改调 `db.is_memory_muted_now` |
| COR-010 | 新增 `db.WriteReturnedNoRow` + `db._first_row()`, 覆盖全部写入点 |
| COR-005/006 | `deskcore/store._paged()` 助手(空页收工 + offset 按实收前进), 应用到 `existing_fingerprint_version_ids` / `legacy_versions` / `fingerprints_missing_vectors` |

**偏差 1 — ROB-003 没有把 `/health` 改成 503。** 报告 §9 第 1 条写的是"`/health` 在 `ok=False` 时返 503"。实施时否掉了这一半:

- CI 里有一条带完整理由的断言把 200 定为硬要求(`ci.yml` 的 app 冒烟步): 库瞬断这类**可恢复**故障不该把整个部署卡住, 而 Railway 的 healthcheck 只看状态码。翻转它会让一次 Supabase 抖动挡住正常发布。
- 更重要的是: 真正的危险来自 `resolve()` 的 fail-open, 不是状态码。根因堵死之后, 未配鉴权的部署会对每个工具调用返 401 —— 起得来但什么也不做、且当场可见, 不再泄露任何数据。再用状态码兜第二遍属于拿部署可用性换一个已经不存在的风险。

所以 ROB-003 按"修根因、留状态码"处理, 并在 `docs/deskcore.md` §3b 记下这个取舍。**若日后仍希望 `/health` 对配置类故障返 503**, 那是一个独立的、可讨论的运维决策, 不在本批。

**偏差 2 — COR-010 实际是 11 处, 不是 7 处。** 报告正文只列了 7 个代表位置。逐个核过之后另有 4 处同类:
`update_project`(`db.py:1114`)、`_upsert_memory_locked` 的 INSERT 返回(`:2217`)、
`increment_memory_frequency` 的兜底写(`:2500`)、`update_memory`(`:2508`)。全部一并修了。
CI 用 AST 遍历 `db.py` 钉死"不许再出现无守卫的 `return <x>.data[0]`"—— 按行文本匹配会把
`if res.data: return res.data[0]` 和 `return res.data[0] if res.data else None` 一起误报。

**过程中新发现的一处**: `memory.save_calibration_notes` 的 docstring 仍在描述已被替换的
`.eq("calibration_notes", ...)` 机制(注释与实现不一致), 由新加的 CI 断言当场抓到, 已一并改。

### 0.2 第 2 批(数据一致性与并发)的实现说明

回归在 CI 的 `审计第二批回归` 一步。每条都**先复现失败模式再证明修好** ——
只断言"现在是对的"挡不住回退。

| 条目 | 怎么修的 |
|---|---|
| COR-002 | `items.id` 改由客户端预生成并显式写进 INSERT(`uuid_generate_v4()` 只是 DEFAULT), slot ↔ item_id 由构造保证, 不再依赖任何返回顺序; 回执缺行时整批失败 + 回收已插的行 |
| COR-004 | `migrations/003` 加 `UNIQUE(item_id, version_num)`(先把历史重复对子按 `(version_num, created_at, id)` 稳定重编号, 否则建索引直接失败); `create_version` 捕获 23505 后重读 max 重试 |
| COR-008 | 新增 `db._paged_select()`(空页收工 + offset 按实收前进), 应用到 `get_session_committed_item_ids` / `list_approved_versions_for_sync` / `_collect_recent_canonical_versions` 的版本查询; `_collect_recent_canonical_versions` 删掉多余的短页判据; `deskcore/store.fingerprints` 同改 |
| COR-009 | `list_items.clear()` 移到 INSERT 之后 |
| COR-011 | 加 `marked_items` 集合去重, 警告仍逐条给用户 |
| COR-020 | `content = rule.get("content","")` 读一次, 四条上报路径统一用它 |
| COR-021 | 新增 `_strip_outer_code_fence()` 只剥最外层围栏, 替换三处全局 `re.sub` |
| COR-022 | `engines = list(dict.fromkeys(engines))` 去重保序 |
| ROB-009 | sweeper 增查 `heartbeat_at IS NULL` 的候选(按 `claimed_at` 判超时; 两者皆空视为僵尸), 分两次查而不是手拼 `or=` 过滤串 |
| ROB-018 | `seal_session` / `update_job_progress` 看受影响行数; 四个草稿写入收进 `_best_effort_item_patch`(仍不抛, 但两种失败都留痕) |

**顺带抽出的一处**: 新增 `db.parse_ts()` 统一 PG 时间戳解析(aware/naive/`Z`/7 位微秒),
`is_memory_muted_now` 改调它, ROB-009 也用它。COR-007 的根因就是"各写一份 ISO 解析",
本批修 ROB-009 时差点又用字符串字典序比时间戳 —— 带微秒与不带微秒混排会差一秒
(`.` 的码位大于 `+`)。判据收成一处。

**COR-008 的范围比报告写的多一处**: `_collect_recent_canonical_versions` 内部的
`_fetch_versions` 是按 **item 数**分块(每块 100 个 item)而不是按行数, 一块里只要平均
迭代过 10 版就能超过 max-rows —— 而那段注释声称已经处理了截断。一并改成翻页。

**CI 断言写法**: 本批有三条断言第一版用文本匹配, 全都被自己写的解释性注释误报
(注释里引用旧写法来说明为什么换掉它)。改成 AST 后才可靠 —— 这正是 round-5 §6
记过的那一课, 一个批次里又踩了三次, 说明"钉死某个写法不许回来"的断言天然应该走 AST。

### 0.3 第 3 批(性能与成本)的实现说明

回归在 CI 的 `审计第三批回归` 一步(16 条)。同样是**先复现失效再证明修好**。

| 条目 | 怎么修的 |
|---|---|
| SUP-001 | 新增 `config.ANTHROPIC_CACHE_TTL`(默认 `1h`), `generator._cache_control()` 成为全仓**唯一**构造 `cache_control` 的地方。中转站若不认 `ttl`, `_call_with_retry(..., rebuild=)` 当场关掉 ttl、重拼请求体再试一次, 并埋 `anthropic_cache_ttl_rejected` |
| SUP-002 | 新增 `deskcore_check_drafts` RPC(`migrations/004` + `db.py::CREATE_TABLES_SQL` 两边同步): 开头精确 / 四字串 Jaccard / 标题余弦三路全部在库里算完, 每篇只回一行。`core._history_probe` 把下推与 Python 两条路径收成同一个形状 |
| SUP-003 | `dedup.similarity_matrix()` 用 numpy 做成批余弦, `find_near_duplicates` / `cross_batch_pairs` 改走它; numpy 缺失时退回逐元素。numpy 补进 `requirements.txt` 显式声明 |
| SUP-004 | `list_projects` 由 `2N+1` 次查询变成固定 3 次: `rule_counts_bulk`(一次 `in_` 查全部项目的规则, 且**不再取 embedding 列**) + `deskcore_fingerprint_counts` RPC。RPC 未部署时退回逐项目 count |
| SUP-005 | 新增 `memory.prepare_soft_context()`, `filter_soft_by_relevance` 接受 `context=` 复用。app.py 两个调用点(队列 / 快速生成)各自从两次 embedding 降到一次 |
| SUP-007 | `_make_client_cached` 加 `ttl=7200` + `max_entries=64`; 登出**不再** `clear()` 全局 client 缓存 |
| SUP-008 | `_MEMORY_UPSERT_LOCKS` 改成带**引用计数**的 LRU(上限 512), 只淘汰 `users == 0` 的项 |
| ROB-004 | 根因随 SUP-002 消掉; 另给 `/health` 一个**私有** `anyio.CapacityLimiter` + 5 秒墙钟上限, 它不再和工具抢 starlette 的默认线程池 |
| ROB-011 | 新增 `store._paged_iter()` / `legacy_version_pages()`, `backfill_fingerprints` 改为逐页消费; 请求路径那一半随 SUP-002 消掉 |
| ROB-013 | 新增 `db.close_client()` + `auth._auth_client()` 上下文管理器, 五个一次性 auth client 用完即关 |

**SUP-001 的账**(base input 价记作 1×; 写入 5m=1.25× / 1h=2×, 命中均 0.1×):

| 一小时内的批数 | 现状(5m, 每批必 miss) | 1h TTL | 完全不开缓存 |
|---|---|---|---|
| 1 | 1.25 | 2.0 | 1.0 |
| 2 | 2.5 | 2.1 | 2.0 |
| 3 | 3.75 | 2.2 | 3.0 |
| 10 | 12.5 | 2.9 | 10.0 |

n≥2 起 1h 就比现状便宜, 只有"一小时里孤零零跑一批"更亏 —— 那种用法本来也无缓存可言。
**注意现状那一列**: 批间隔一旦超过 5 分钟, 开着缓存比不开还贵 25%, 这正是 SUP-001 的实质。
默认选 1h 是因为这个工作台的节奏(坐下来连排几批, 批间常常 >5min 但 ≪1h)正好落在 5m 最亏的区间。
落地前建议先按 §7.6 量一次 `cache_create / (cache_create + cache_read)`, 长期 >0.5 就是这个形状。

**SUP-002 刻意不走 ivfflat 索引**: `draft_fp_embedding_idx` 是近似最近邻(默认 `probes=1`
只扫一个聚类桶), 对"推荐相似内容"够用, 对**查重硬闸**是致命的 —— 漏掉的那条正是要拦下的
重复稿, 而且不报错; 叠加 `project_id` 过滤后更糟(先按向量取候选再过滤, 可能一条都不剩)。
RPC 里 `ORDER BY` 的是子查询算好的别名 `sim` 而不是 `title_embedding <=> v`, 规划器因此
必然走顺序扫描。索引留着不动, 将来做"找相似选题"这类容忍近似的功能仍然用得上。

**下推带来的一个口径变化**: `history_size` 现在是**全量**条数, `history_truncated` 恒为
`false` —— 原来的 4000 条上限和那条"更老的稿子没参与查重"的警告只在 `migrations/004`
没跑、退回 Python 路径时才会出现。这是修好了一个已知的谎, 不是丢了信息。

**SUP-008 的关键约束**: 加上限时**绝不能淘汰正在被持有的锁**。一旦把某个 key 的 Lock
换成新对象, 已经拿着旧锁的线程和随后拿到新锁的线程就不再互斥 —— 而这把锁存在的全部
意义就是互斥。引用计数在 `acquire` **之前**就 +1, 否则"取到锁对象但还没 acquire"那一瞬
仍可能被淘汰。回归里专门有一条守着这件事。

**又一次证明第 2 批那条教训**: 第 1 批留下的 `assert "_paged(" in body` 在本批把
`legacy_versions` 改成委托给 `legacy_version_pages` 之后当场误报"没走翻页" —— 翻页明明
还在, 只是深了一层。已改成 AST 且钉**被禁的形态**(常数 `.limit(N)`)而不是**当前的写法**。
钉写法的断言每次合理重构都会假报警; 钉禁忌的不会。

**deskcore 依赖增量**: `deskcore/requirements.txt` 不需要改 —— 部署本来就是
`pip install -r requirements.lock -r deskcore/requirements.txt`, numpy 在锁文件里。

---

### 0.4 codex review 的 7 条(2026-08-24)——**全部属实**

第三批推上去之后叫了一轮 codex review，7 条 findings 逐条核实之后**没有一条是误报**。
其中 3 条 P1，两条是我在这次审计里**自己引入**的回归。记在这里，因为它们暴露的是
我判断方式上的问题，不只是几个 bug。

| # | 严重度 | 是什么 | 修法 |
|---|---|---|---|
| 1 | P1 | `migrations/003` 的唯一索引会让**每一个多引擎批次**整批不落库 | `bulk_create_initial_versions` 改成同 item 内 1..N 编号 |
| 2 | P1 | `CREATE_TABLES_SQL` 里 4 个 RPC 的 `search_path` 够不着自己引用的表 | 加 `autowriter` 进 search_path + CI 守卫 |
| 3 | P1 | `/health` 那个"5 秒墙钟上限"**根本不生效** | `abandon_on_cancel=True` + 探测 client 配网络超时 |
| 4 | P2 | 硬约束标记去重记早了，一次瞬时错误会让 item 停在原状态 | 写成功之后再记 |
| 5 | P2 | 降级那一瞬间，除第一个之外的在途请求都白失败一次 | 判据与全局开关解耦 |
| 6 | P2 | `rule_counts_bulk` 把几百个 UUID 塞进一个 `.in_()`，几十 KB 查询串会被网关 414 | 走 `db._in_chunks` |
| 7 | P2 | 无记忆的项目每批白发一次 ctx embedding —— 本来要省的地方成了净增 | 两个记忆表都空时不算 |

**第 1 条是这次审计里我犯的最严重的错误。** COR-004 的诊断（重复 `version_num` 让挑
"代表版本"的 tie-break 静默选错）是对的，但我把成因锁死在"并发迭代"这个**罕见竞态**上，
没有去查"重复号平时是怎么产生的"。实际上 `bulk_create_initial_versions` 给同一 item 的
**每个引擎**都硬编码 `version_num=1` —— 多引擎批次是天天在跑的常规路径，重复号是**设计
产物而不是竞态残留**。于是加上唯一索引之后：首版 insert 必撞 23505 → `_save_batch_results`
的失败路径删掉已建 items → **整批生成完什么也不存**。

教训不是"再仔细点"。是：**给一列加唯一约束之前，必须先枚举出所有往这一列写值的地方**，
而不是只顺着"我怀疑的那条竞态路径"往下看。我当时只读了 `create_version`，
没读同样写 `version_num` 的 `bulk_create_initial_versions`。

**第 2 条也是同一类：只看了自己新加的东西，没看它所处的上下文。** 我确实注意到
`CREATE_TABLES_SQL` 里表名不带前缀而函数固定了 `search_path`，当时判断"这是既有写法，
不是我的问题"就放过了 —— 可我正在往这个块里**加两个同样写法的新函数**。而且失败方式极坏：
报错文本里带 `does not exist`，正好被 `store.rpc_missing` 的判据认成"迁移没跑"，
于是**静默**退回 Python 慢路径，永远不告警。顺带把旁边两个老函数一起修了。

**第 3 条是"我以为我修好了，其实没有"。** 实测（回归里钉死了这个事实）：

```
with anyio.fail_after(0.3):
    await to_thread.run_sync(sleep_1s2)   # 1.20s 后【返回了值】，没抛超时
```

`to_thread.run_sync` 默认 `abandon_on_cancel=False`，取消要等工作线程自己返回才生效。
也就是说改之前 `/health` 在"库卡住不回"——**本条修复唯一针对的那个场景**——下照样会一直挂着。
我写下"5 秒墙钟上限"时没有验证过它会不会触发。现在两层都上：`abandon_on_cancel=True`
让 `/health` 按时应答，探测 client 的 `postgrest_client_timeout` 让被放弃的线程能自己收场。

**关于回归**：7 条各补一条，且都先在 `git stash` 回到修复前的代码上跑过一遍、确认会红。
`/health` 那条把 anyio 的坑本身写成了可执行断言（先证明默认值确实不生效，再证明我们用的
写法生效）——下次有人"顺手"把参数去掉时，红的会是一条讲清楚了为什么的断言。

---

### 0.5 第 4 批(结构性)的实现说明 —— **进行中**

这批和前三批不一样：前三批是"改一处、证一处"，这批的主体（把生成编排从
`app.py`/`worker.py` 抽成 `generation_service.py`）是**搬家**。搬家的风险不在于某一行写错，
而在于搬完之后没人说得清"行为有没有变"。所以顺序被刻意打乱了：**SUP-024（测试地基）
提到最前面做**，其余七条排在它后面。

| 条目 | 状态 | 说明 |
|---|---|---|
| SUP-024 · 建 pytest 地基 | ✅ 已做 | 见下 |
| COR-015 · deskcore 归属校验 | ✅ 已做 | 按 `projects.owner_id` 隔离（读写两侧都校验），见下 |
| COR-014 · bottom-k 长短稿失真 | 待做 | 先量化现状漏检率，再改标准 bottom-k 估计 |
| ROB-001/002 + SUP-011/012 · 抽 `generation_service.py` | 待做 | 搬家本体，靠上面的地基兜底 |
| SUP-010 · DDL 挪出 `db.py` | 待做 | 侦察发现**没有任何代码执行 `CREATE_TABLES_SQL`**，比预想简单 |

#### SUP-024 —— 为什么现在才有第一个 `tests/` 目录

侦察时确认的事实：全仓在此之前**一个 pytest 文件都没有**。回归全部长在 `ci.yml` 的十几个
`python - <<'PY'` heredoc 块里，每块自带一份假件、一套 env 设置。那些块是随着一次次
review 长出来的，**它们仍然有效、也仍然在跑** —— `tests/` 不是去替换它们。

之所以现在必须建，是因为下一步要搬家：heredoc 那种形态可以钉住"某条 bug 不复发"，
但撑不住"同一批逻辑在两个地方要保持等价"这类需要反复加用例的场景。

新增四个文件：

| 文件 | 作用 |
|---|---|
| `tests/conftest.py` | 在 import 任何业务模块**之前**做两件事：`AW_DISABLE_ST_CACHE=1`（否则缓存会让"改了数据再读一次"的断言失真）、灌 Supabase 三个 env 占位值（`config.py` 是模块级读取，缺了在 import 期就抛） |
| `tests/fakes.py` | **一份**共享 PostgREST 假件。刻意只实现真的被用到的那部分；`count` 在 `range` **之前**算（PostgREST 的真实行为，也是 `db.py` 多处翻页逻辑的前提）；`max_rows` 模拟服务端 `db-max-rows` 静默钳位 —— 本仓踩得最多的坑（COR-005/006/008 三条都是它），假件必须能重现 |
| `tests/test_guardrails.py` | §7.1 的三组「护栏」：价档前缀匹配（R-042）、`validator._parse_rule` 的正负向优先级（R-041）、调校笔记的行级去重与软上限。共同点是**错了不报错** |
| `tests/test_known_gaps.py` | §7.1 点名、但**不在本批八条范围内**的两条，用 `xfail(strict=True)` 立案 |

CI 加一步 `pytest 用例目录`，位置在 `deskcore selftest` 之后、heredoc 群之前 ——
它需要 `requirements.lock`，但**不**需要 `deskcore/requirements.txt` 的 fastapi·mcp
（那些只在后面的 app 冒烟步里装）。第三批就在这个边界上栽过一次（`ModuleNotFoundError: fastapi`），
所以这次在步骤注释里写死了。`pytest` 单独放 `requirements-dev.txt`，不进部署闭包。

**关于 `xfail(strict=True)`**：`test_known_gaps.py` 里那两条现在是红的（SUP-006 的
per-model 输出上限、COR-014 的长短稿失真）。直接让 CI 红等于把两条计划外的修复硬塞进本批；
悄悄不写等于测试计划里最有价值的两条被静默跳过。`strict=True` 是关键 —— 一旦有人把它修好，
xfail 会变成 **XPASS 失败**，逼下一个人把标记摘掉、变成真正的回归。CI 那步带 `-ra`，
就是为了让这种 XPASS 在摘要里能一眼看到是哪条。

**一个自己撞上的坑，值得记下来。** SUP-006 那条第一版写的是"断言 `max_tokens ≤ 128000`"，
结果它 **XPASS 了** —— `count=50` 算出来是 125,000，恰好在阈值以下。也就是说那条断言从头到尾
没在验 SUP-006，只是碰巧成立。真正缺的东西是【按模型查输出上限】这件事本身：`config` 里
只有 `MODEL_CONTEXT_WINDOWS`（那是**输入**窗口），没有任何一张表记录 per-model 的 max output。
改成断言那个缺失的前提之后才真的红。这和 §0.4 学到的是同一件事的另一面：
**断言要钉住"被禁止的形态"，不是"我以为的当前数值"**。

#### COR-015 —— deskcore 按 `owner_id` 隔离

产品决策已定（2026-08-24）：**按 `projects.owner_id` 隔离，读写两侧都校验**。完整口径写在 `docs/deskcore.md` §2.2.1，这里只记实现上的判断。

**这条的严重度不在"能读到别人的项目名"，在于三个 `needs_user=False`。** 十一个工具里，`list_projects` / `borrow_lessons` / `check_drafts` 连"调用者是谁"这个参数都没有——不是拿到了不用，是根本不问。它们串起来是一条完整的越权链：

1. `list_projects` 返回**全库**项目台账，顺带给出一批可以喂给其它工具的 `project_id`；
2. `check_drafts` 的返回值回显撞车对象的**标题**——拿一篇随便什么稿子去撞，就是一个可反复调用的历史标题读取接口；
3. `borrow_lessons` 发给馆员的 brief 是拿项目行拼的（品牌 / 定位 / 战术）。

而写侧（`commit_drafts` / `record_rule` / `draw_angles`）虽然拿到了 `user_id`，却只用它读个人层，从不核对项目归谁。

**做法：判据只写一处。** `core.assert_project_access` 是唯一实现，九个项目级入口以它开头，`TOOLS` 里 `needs_user` 全部改成 True。收敛成一处不是洁癖——归属是**会变的产品决策**（今天按 owner，明天可能按团队成员表），散在十一个工具里意味着改口径要改十一处，而漏掉的那一处不会报错，只会继续放行。

四个具体判断，都不是显然的：

| 判断 | 为什么 |
|---|---|
| 校验函数**返回项目整行** | `build_writing_brief` / `draw_angles` / `my_style` / `borrow_lessons` 本来就要读项目行。只回 True/False 的话，这四条最热的路径每次多一个来回 |
| 拒绝映射成 **403 而不是 500** | 500 对调用方的意思是"服务端坏了，待会儿重试"，于是模型会拿同一个错 `project_id` 一直试；而且正常的权限拒绝会去污染错误监控 |
| `_safe` **不再吞** `PermissionError` | `_safe` 兜的是瞬时故障（少点参考不影响写稿）。权限拒绝重试一万次也一样，包成"只多一个 error 字段的正常结果"会让模型继续拿同一个错 id 试下一个工具，也让 403 只对一部分工具成立 |
| CLI 的 `backfill` / `reembed` **刻意不校验** | 它们是运维命令，跑的人手里握着 service_role key（等价于直连库）。加校验挡不住任何人，只会挡住"帮同事补一下指纹"，还会给人"运维路径也隔离了"的错觉。边界在 MCP/REST 那一面 |

顺带修掉一处死代码：`db.get_project` 用的是 `.single()`，PostgREST 在 0 行时回 406、postgrest-py 抛 APIError——也就是说它**从不返回 None**，四个调用点里那句 `if project is None: raise ValueError("project not found")` 从来没执行过，传错 `project_id` 拿到的是一条看不懂的 406。归属校验必须区分"项目不存在"（可能只是 id 抄错）和"项目不是你的"，所以换成 `store.project_row`（`limit(1)`）+ 独立的 `ProjectNotFound`（`ValueError` 的子类，老的 `except ValueError` 不会漏接）。

**回归写在 `tests/` 而不是新的 heredoc 块** —— 这是上一步建地基的第一次兑现。26 条用例分三组：判据本身、每个入口都过了闸、拒绝不会被吞掉。其中最有价值的一条是 AST 遍历：

> 归属校验最典型的失效方式不是判据写错，而是**新加了个工具忘了加校验**——而那不会报错，只会继续放行。

那条断言钉的是【被禁止的形态】（"一个项目级入口的 body 里找不到这个调用"），不是当前代码长什么样：加工具忘了校验会红，重构参数顺序不会误报。另外 403 的运行期断言放在 `ci.yml` 的 app 冒烟步——`app.py` 需要 fastapi，而跑 pytest 那一步只装 `requirements.lock`（§0.3 里那条边界）。

**修完之后回头改了 11 处旧断言。** 那些块里的假件不带 `projects` 表、调用不传 `user_id`，全部变红——不是回归，是它们验的东西（SUP-002 的下推、SUP-004 的查询次数、round-8 的蒸馏闭环）本来就不涉及归属。给假件兜一行项目、给调用补上身份即可。这件事本身值得记：**一道横切所有入口的校验，代价就是所有既有测试的构造都要跟着变**——如果那个代价大到让人想跳过，多半说明校验加在了错误的层。

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

> **落地位置**(SUP-024 已做, 见 §0.5)。下表的行分散在两处, 不是一处:
>
> - **COR-002/004/005/006/007/008/009/010/020/021/022** —— 已在第 1、2 批随修随补, 长在
>   `ci.yml` 的 `审计第一批止血回归` / `审计第二批回归` 两步里。**没有搬**: 它们每条都绑着
>   一段"先复现失败模式"的构造, 挪个地方只会丢掉上下文。
> - **三条护栏 + SUP-006 + COR-014** —— 新的 `tests/` 目录, 分别是
>   `tests/test_guardrails.py` 与 `tests/test_known_gaps.py`。共享假件在 `tests/fakes.py`。
>
> SUP-006 与 COR-014 两条**当前是红的**, 用 `xfail(strict=True)` 立案 —— 理由与
> "修好之后会自动变红逼人摘标记"的机制见 §0.5。

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

### 第 1 批 · 止血 (1-2 天, 收益/成本比最高) — ✅ 已完成, 实现说明见 §0.1

按顺序:

1. **ROB-003** — `/health` 在 `ok=False` 时返 503; `identity.resolve` 在未显式设 `DESKCORE_DEV=1` 时拒绝 dev 全放行。**这是唯一一个"配错就等于把库开放到公网"的问题。**
2. **COR-003 / ROB-006** — 调校笔记 CAS 改 hash witness。当前自动学习可能**已经在生产上静默停摆**, 且没有任何告警。顺手把 `memory.py:820-822` 的静默 `return None` 改成写一条 `status.warnings`。
3. **COR-007** — 一行改动: `deskcore/store.py:104-111` 改调 `db.is_memory_muted_now`。被静音的硬规则正在被注入。
4. **COR-010** — 统一 `res.data or []` 判空。7 处, 纯机械, 消掉一整类红屏。
5. **COR-005 / COR-006** — deskcore 三个无翻页查询补翻页。backfill 的幂等性和覆盖面都建立在它们之上。

### 第 2 批 · 数据一致性与并发 (3-5 天) — ✅ 已完成, 实现说明见 §0.2

6. **COR-002** — bulk insert 顺序假设改成显式匹配。这是唯一一条"正文永久丢失且零告警"的路径。
7. **COR-004** — `versions` 加 `UNIQUE(item_id, version_num)` + 撞键重试 (需要一条迁移, 两边都改)。
8. **COR-008** — 抽 `_paged_select` helper, 把 5 个"短页收工"点统一掉, 同时上 semgrep 规则②③防复发。
9. **COR-022 / COR-011 / COR-009 / COR-020 / COR-021** — 一批小改, 各自独立。
10. **ROB-009 / ROB-018** — sweeper 的 NULL 心跳; UPDATE 0 行的统一检查。

### 第 3 批 · 性能与成本 (1 周) — ✅ 已完成, 实现说明见 §0.3

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
