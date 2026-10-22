# autowriter / deskcore 交接 · 2026-10-21（最新，先看这一节）

快照 **2026-10-21 08:25 UTC**。上一位接手的 Claude 会话到这里停手，owner 换人接手，不再有人替你盯 PR 或按时检查。三库总交接、接手要按时做的事，看 [truth-vault HANDOFF.md 最上面一节](https://github.com/TangVarie/truth-vault/blob/main/HANDOFF.md)；未完成清单看 [OPEN-ITEMS.md](https://github.com/TangVarie/truth-vault/blob/main/OPEN-ITEMS.md)。这一节只写 aw 自己的部分。下面 10-15 那一版里没过时的照旧有效。

### 线上

- 默认分支 `claude/xhs-content-workstation-BSEuH` @ `2b96b36`。aw#118 只改文档，上一个改代码的是 `f004490`（aw#116：tv-sync 写 `tv_edit_diffs` 时 `computed_at` 有的带有的不带，PostgREST 补 NULL 撞 23502）。
- `/health`（08:20 UTC）：`build.commit` = `f0044905bda1`，`protocol_version` = `5ea3b14e1f37`。Railway 按 `2b96b36` 重新部署后 build 会变，协议不变。
- 生产迁移到 014。015 是 aw#117 的，还没跑。

### 在路上的 PR

1. **aw#115 写作台第二批 A–C**：TangVarie/autowriter#115，分支 `claude/zen-johnson-m2f14s`。
   - **状态**：head `c709793`，CI 绿，没有冲突，已是 ready。Codex 第 12 轮 08:09 UTC 开始审，交接时还没出结果。
   - **内容**：口径写在 `docs/deskcore.md` §3.9、§3.10。
     - 交付模板由代码拼交付稿（`deskcore/delivery.py`）。
     - 规则的代码检查（`deskcore/rulecheck.py`，打分之后报「必须改 / 建议改」）。
     - 限时正则引擎（`validator.search_user_regex`；`config.REGEX_SEARCH_TIMEOUT_SEC` 每条一份，`config.REGEX_CHECK_TOTAL_SEC` 一次检查的总上限）。
     - 结构指纹。
   - 不带迁移，改了 `deskcore/protocol.md` 和 skill：合并后协议变成 `895ed01874d6`。
   - **10-21 这几轮**：
     - 第 9 轮：「必须带的话术」只管标题或正文时，那一处空着不报。
     - 第 10 轮：模板不交付的话题 / 评论，查规则还拿原稿查。
     - 第 11 轮：`^广告$` 这种锚定的话题正则，话题多于一个就漏；现在整行和每个话题都查。
     - 三轮都已修推。第 9–11 轮没跑独立复审（上一会话的子代理额度用完），只跑了全量测试（`c709793` 上 1674 passed）和 smoke。
   - **合并条件**：truth-vault 的 `python scripts/codex_gate.py TangVarie/autowriter 115` 输出 PASS、CI 绿、没有未解决的 thread。有意见就修、在 thread 上回、推、再 `@codex review`。
   - **合并后**：
     1. Railway 自动部署，核 `/health` 的 `build.commit` 是合并提交前 12 位、协议是 `895ed01874d6`。
     2. 写手重导 skill。服务端探针会在工具返回里提醒，计数看 `/skill-versions`。
2. **aw#117 aw E：`check_rules` 写手工具 + Jev 编题**：TangVarie/autowriter#117，分支 `claude/aw-e-check-rules`。
   - **状态**：head `52eca1b`，CI 绿，没有冲突。08:16 UTC 被改成 ready，Codex 自动开始审 `52eca1b`。
   - `52eca1b` 把 aw#115 的 `c709793` 合了进来。冲突处理：
     - 两条查规则的路径（`score_drafts` 的 rule_checks 和 `check_rules`）共用 `core._as_delivered`，现在一律走 `delivery.delivered_draft`；
     - skill 用 `python -m deskcore.cli sync-skill` 重新生成，协议 `be4d668f5ab6`；
     - 本地全量 1827 passed。
   - ⚠️ **base 还是 aw#115 的分支 `claude/zen-johnson-m2f14s`，aw#115 合并之前不要合它**，否则 aw E 的代码会混进 aw#115。
   - 带迁移 `migrations/015_rule_compiles.sql`，**上线前先在生产跑**。
   - `49a3684` 是 WIP：独立复审提的 12 条只修了一部分，标题和严重程度在 PR 描述里。
   - **aw#115 合并后**：
     1. base 改成 `claude/xhs-content-workstation-BSEuH`，把默认分支合进来。
     2. 确认 `49a3684` 里的回归测试在修复前确实失败。
     3. 修完剩下的复审意见，再过 Codex。
   - Jev 侧 `/compile_rules` 已在线（Jev#23），编题 key 已配。
3. **aw#119**：TangVarie/autowriter#119，CI 装完 deskcore 依赖后重跑 `tests/`，让依赖 fastapi / mcp 的测试真跑起来。owner 另开会话在做。合了以后，下面「怎么测」里那条已知缺口就补上了。

### aw#115 部署之后的数据操作

一步一步来，每步先建备份表：

1. 4 个项目设交付模板（`projects.default_params` 的 `delivery`）：百健士、西屋911gt、Hatherine、途鸽。途鸽用「照规则原样」那版。
2. 看一批真实稿子。
3. W-28：插 3 条硬的退路规则，撤 6 条交付格式规则。
4. 只给规则 `ec0b9bd6` 加代码检查。

另外两点：

- Hatherine 有两条要等 D1–D7 重录。
- 计划和 SQL 里有客户的规则原文，**不进 GitHub**，10-21 已作为文件包发给 owner。

### 怎么测

- 本地：`python -m pytest tests/ -q`，用装全依赖（含 `deskcore/requirements.txt`）的环境。数字随提交变：10-21 aw#115 `c709793` 上 1674 passed，aw#117 `69f6037` 上 1772 passed。
- CI 的 smoke 是 Python 3.11。跑整个 `tests/` 的那一步只装了 `requirements.lock` 和 `requirements-dev.txt`，没有 fastapi / mcp。
  - 要这两个的测试得 `pytest.importorskip`，不然 CI 直接红（aw#117 10-21 踩过一次）。
  - 但跳过就等于 CI 没覆盖。后面装完 `deskcore/requirements.txt` 的那一步（`.github/workflows/ci.yml` 里 `pip install -r deskcore/requirements.txt httpx` 下面一行）只重跑点名的文件：默认分支上只有 `tests/test_project_isolation.py`，aw#117 加了 `tests/test_rule_compile.py`。新加这类测试要把文件名加到那一行。
  - 现有十来个依赖 fastapi 的测试文件在 CI 里一直是跳过的，是已知的缺口，只在本地全依赖环境里跑。
- 计时类测试的「例子本身得慢」前置条件用 `tests/test_regex_safety.py` 的 `_assert_slow_with_re`（aw#115 带进来），别写死毫秒数。CI 机器快，写死的会偶发红。

---

# autowriter / deskcore交接 · 2026-10-15

> **2026-10-19 · D-120 写手侧改版**：`score_drafts`（存档 + Jev 参考分，不拦；`commit_drafts` 为过渡期旧名）取代"commit → review → export"；`review_drafts` / `export_drafts` 撤出 MCP，定稿由写手贴飞书、发布后 tv-sync 对回；评论改为 `comments_for(link)` → 写 → `score_comments`。下文 10-15 的快照里涉及这几个工具的描述按此读。

> **评论线云端交接**：[写作台接手说明及完整材料入口](docs/comment-handoff-online-2026-10-15.md)，可直接在GitHub阅读。

本轮接管覆盖10-12～15；最后只读核对 **10-15 12:04，北京时间**。默认分支是 **`claude/xhs-content-workstation-BSEuH`**；功能基线 [PR96](https://github.com/TangVarie/autowriter/pull/96)，merge `487403b75fb06bf3001cf463a0a966171b45a2b0`。后续TV/Jev的参考评估文档没有再次改aw业务代码。

跨库：[总交接](https://github.com/TangVarie/truth-vault/blob/main/HANDOFF.md)、[统一待办](https://github.com/TangVarie/truth-vault/blob/main/OPEN-ITEMS.md)、[Jev交接](https://github.com/TangVarie/JevforCoentent/blob/main/HANDOFF.md)。旧runbook保留设计与历史过程，当前状态以本文注明的实查快照为准。

## 当前结论

MCP并发修复、评论转发和跨项目防护已经合并部署；现场5个隔离词已配生产。**评论服务仍关闭，J-03的C3/C4固定验证已完成但未过线、上线报告尚未获接受，W-02暂不统一群发重导skill。** 不能把工具列表出现评论接口当成评论检查已放行，也不能把线程池修复当成借卡客户端截止已验收。

## 本轮改动和验证

### MCP阻塞与身份

- 同步MCP工具通过async wrapper和`run_in_threadpool`执行，避免一个等待I/O的工具阻塞整个事件循环。固定`mcp==1.30.0`。
- 真MCP SDK并发回归验证：后台等待时另一个调用可继续；线程内拿到鉴权身份，模型传的`_user_id`不能覆盖它。REST原线程池路径保持原行为。
- 本地Python3.12全量820 passed、1既有xfail（PR96时点），CI另验Python3.11、SDK握手与REST拒写。未安装SDK的阶段会skip两项，正式SDK阶段已覆盖；不能只拿skip阶段作验收。

### 评论工具与截止

- 新工具`comment_repair_plan_for`、`judge_thread`先核所有权，再由服务端tv-map填项目/品类，不信任模型任意填项目。
- 同一个18秒墙钟截止覆盖上下文查询、连接、读取、JSON解析；最多4个共享线程/在途额度，超时不认迟到结果，满额如实返回未判。
- 只读接口拒绝写账本或伪装已发布参数。HTTP200但不是约定JSON、缺映射、服务超时/故障、`needs_review`、单条/整组结果形状异常均不得当“通过”。
- 对应Jev [PR9](https://github.com/TangVarie/JevforCoentent/pull/9)已部署，生产`comment_tools_enabled=false`；原100条700格与C1开发试跑不能替代新40条独立复验。人员40条已完整返回，原判固定为单人参考；C3获本次许可完整复验172/280、69歧义、0缺答，暂不推广。C4获此次明确许可完成真实80条，新40与AI参考225/280未达标，本轮不推广。

### 跨项目串词与影子提示

- `record_rule`读取同owner项目的品牌、项目名、可识别中文产品后缀及`default_params.isolation_terms`。命中其他项目专属词直接拒绝、0写入；含品牌/项目词的global规则也拒。NFKC/大小写/ASCII边界均处理，不把RIO错匹配到priority。
- 协议规定同一对话只写一个项目、切项目新开对话；首次回复复述`open_project`项目名。规则被拒后核对项目，不能改写措辞绕过检查。
- Hatherine根项目5词已在10-13按项目/owner/品牌精确匹配、CAS更新生产并回读：**痘痘贴、祛痘、去痘、抑痘、76片**。保留其他参数、兼容原jsonb字符串，其他项目未改。旧值和读回值仅本地审计目录保存。
- 使用回读配置在本地已合代码/fake库验证：5词全部拒绝且0写入，正常西屋规则允许；**尚未做真实带鉴权线上拒写探针**。早期发布说明“本PR未执行配置”是当时事实，不能再当成今天仍待配。
- `commit_drafts.project_isolation`只作影子信号：本项目与同owner其他项目的近期同模型标题向量作比较；预算1.5秒，每项目最近40条，排除本批全部成功写入指纹，包括身份失败后未回报version_id的行。相似度>=0.72且跨项目优势>=0.08只标记，不改审核结果。
- 无同模型本项目历史/维度不匹配时不作向量归属；正文词检查仍可用。最多2个共享线程/在途额度，超时后额度直到I/O结束才释放；不能把标题对比说成全量正文语义隔离。

详见 [PR96发布说明](docs/comment-isolation-rollout-2026-10-12.md)。本轮未改变`human_via_agent/user_words`的人工审核含义，协议只是补齐既有定义与实际流程。

## 10-15生产只读快照

服务为`https://autowriter-production.up.railway.app`。`/health`实查：

| 项 | 结果与限制 |
| --- | --- |
| build/protocol | `487403b75fb0` / `3fdff2c0b985`；24工具，包含两个评论转发工具 |
| 鉴权 | 3把key、匿名关闭；未读取或公开key值 |
| 依赖 | supabase/embeddings/vendored vocab正常；librarian configured |
| judge | configured=true，mode=shadow，入库timeout=8秒、draft tool timeout=18秒 |
| 业务管线 | **pipeline.ok=false**：7日drawn149/consumed73，leak_pct51，2项目超线（W-06） |

顶层`ok=true`不表示角度链路健康；configured不代表已做真实带鉴权业务请求。以上是时点快照，后续部署先核build与协议摘要，再核行为，不照抄工具数或历史数据库行数。

## 接下来做什么

| 待办 | 下一动作 | 验收边界 |
| --- | --- | --- |
| J-03 → J-01 → W-02 | 人员40条/C3已完成，不推广；C4真实80条已完成但诊断不达标，本轮不推广；下一轮新设计/新参考/新样本获验收；之后才开评论、真接口小批核查、统一导入skill | 暂不重做旧A/B，不用C1开发分数放行，不因工程已部署提前开关 |
| W-19 | 安排真实带鉴权、目标owner的拒写探针及正常对照；查`batch_metrics.meta.project_isolation`与`phase_ms.project_isolation`收集误报/漏报 | 证明拒绝且0写入；影子阶段不升阻断。测试探针应有明确归属，不冒充实际运营内容 |
| F-04 | 在WorkBuddy真实客户端测`open_project`/借卡等待、超时占比与p95 | 约22秒客户端截止尚未验收；MCP并发修复和评论18秒截止不能替代借卡验收，服务端60秒亦不能保证客户端完成 |
| W-06 | 查超线项目的发牌、会话中断、`commit_drafts`和`export_drafts`链路 | 本轮只读发现51%漏账，不自动补消费、删角度或改审核结果；先辨清真实流程断点 |
| J-08 / J-12 | 写手恢复后用`batch_metrics(meta.mode=deskcore_commit)`重估judge预算；核活跃项目tv-map | judge保持shadow；新超时值需真实样本，不凭旧runbook的“env未配”重复配置 |
| W-04 / W-05 / W-18 | 保留发牌结果加权、recent_angle_keys分页、旧文档逐段追平待办 | 加权未实施；当前交接补入口不表示全部旧文字已重写 |

Streamlit工作台2026-10-16已停服（owner在Railway停掉，按D-072不删仓、Supabase不动）；迁移011/012等更早状态不能从旧runbook“八个迁移/未执行”的段落推断，必要时用`doctor`只读核对。重新导skill由owner/运营在J-01就绪后统一安排，本轮不向写手发送消息。

## 下一位维护者的排查入口

```bash
curl -fsS https://autowriter-production.up.railway.app/health
python -m pytest tests/ -q
python -m deskcore.cli selftest
```

连库的`python -m deskcore.cli doctor`另按已有服务端凭据执行；本轮交接没有新跑doctor，不把健康接口当迁移证明。SQL/Python parity由CI独立PostgreSQL步骤验证，`tests/sql_parity_check.py`不是普通pytest。

现场配置材料在当前工作区`/workspace/rollout-audits/20261013/`，文件`w19-project-config-before.json`、`w19-project-config-after.json`、`w19-guard-validation.json`；换机器由owner安全转交，配置备份/owner或项目UUID/真实规则文本不追加上传GitHub。完整40条文件去Jev/TV交接所列本地目录获取。


## 评论线本轮收尾 · 2026-10-15 20:28（北京时间）

用户委托整线完成再交接。Jev14/TV185已合并；[Jev15](https://github.com/TangVarie/JevforCoentent/pull/15)固定C4，初次231项工程测试通过，审阅后242项通过。合成20条131/140；另有本地HTTP联真实Jev的纯虚构单条/整组7次请求，未修改部署环境。

用户明确许可本次C4真实80条后已全部完成，原40人填原判不改，一致187/280；新40为调用前固定的AI参考，一致225/280、57歧义、0缺答。新40总分、语气/安排及背书边界未达事先标准，按停止规则**本轮不推广C4**，不调低标准或继续加候选追分。详细来源/风险/摘要在Jev收尾报告及TV交接。

本库评论转发和协议工程已交付，但J-01关闭、J-02 shadow/primary未跑、W-02未群发；不把合成/AI参考当真人验收，也不把本地HTTP当生产鉴权端到端验证。下一轮应从主观读感/主行为定义与新独立参考设计继续，不让接手人重做已完成标注/接口/续跑。其他12:04生产快照未在本次重测，W-06/F-04等独立事项仍按原待办。

**C4审阅补丁说明：** 当前评估器v2已补原判独立摘要、20/40/40范围、调用前阈值类型/范围和全选项概率检查。初次v1批次未保存完整概率向量，仅作为历史诊断，不能追认严格v2验收；无追加模型调用。三组v2零调用预检与242项工程回归通过，见Jev收尾报告。
