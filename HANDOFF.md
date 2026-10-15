# autowriter / deskcore交接 · 2026-10-15

本轮接管覆盖10-12～15；最后只读核对 **10-15 12:04，北京时间**。默认分支是 **`claude/xhs-content-workstation-BSEuH`**；功能基线 [PR96](https://github.com/TangVarie/autowriter/pull/96)，merge `487403b75fb06bf3001cf463a0a966171b45a2b0`。后续TV/Jev的参考评估文档没有再次改aw业务代码。

跨库：[总交接](https://github.com/TangVarie/truth-vault/blob/main/HANDOFF.md)、[统一待办](https://github.com/TangVarie/truth-vault/blob/main/OPEN-ITEMS.md)、[Jev交接](https://github.com/TangVarie/JevforCoentent/blob/main/HANDOFF.md)。旧runbook保留设计与历史过程，当前状态以本文注明的实查快照为准。

## 当前结论

MCP并发修复、评论转发和跨项目防护已经合并部署；现场5个隔离词已配生产。**评论服务仍关闭，J-03独立复验与报告验收未完成，W-02暂不统一群发重导skill。** 不能把工具列表出现评论接口当成评论检查已放行，也不能把线程池修复当成借卡客户端截止已验收。

## 本轮改动和验证

### MCP阻塞与身份

- 同步MCP工具通过async wrapper和`run_in_threadpool`执行，避免一个等待I/O的工具阻塞整个事件循环。固定`mcp==1.30.0`。
- 真MCP SDK并发回归验证：后台等待时另一个调用可继续；线程内拿到鉴权身份，模型传的`_user_id`不能覆盖它。REST原线程池路径保持原行为。
- 本地Python3.12全量820 passed、1既有xfail（PR96时点），CI另验Python3.11、SDK握手与REST拒写。未安装SDK的阶段会skip两项，正式SDK阶段已覆盖；不能只拿skip阶段作验收。

### 评论工具与截止

- 新工具`comment_repair_plan_for`、`judge_thread`先核所有权，再由服务端tv-map填项目/品类，不信任模型任意填项目。
- 同一个18秒墙钟截止覆盖上下文查询、连接、读取、JSON解析；最多4个共享线程/在途额度，超时不认迟到结果，满额如实返回未判。
- 只读接口拒绝写账本或伪装已发布参数。HTTP200但不是约定JSON、缺映射、服务超时/故障、`needs_review`、单条/整组结果形状异常均不得当“通过”。
- 对应Jev [PR9](https://github.com/TangVarie/JevforCoentent/pull/9)已部署，生产`comment_tools_enabled=false`；原100条700格与C1开发试跑不能替代新40条独立复验。人员40条已完整返回，原判固定为单人参考；C3获本次许可完整复验172/280、69歧义、0缺答，暂不推广。C4边界已固定并通过20条合成验证，真实旧40+新40的80条批次等待明确出境许可。

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
| J-03 → J-01 → W-02 | 人员40条/C3已完成，不推广；C4真实80条待明确出境许可，再评固定版本和具体报告；之后才开评论、真接口小批核查、统一导入skill | 暂不重做旧A/B，不用C1开发分数放行，不因工程已部署提前开关 |
| W-19 | 安排真实带鉴权、目标owner的拒写探针及正常对照；查`batch_metrics.meta.project_isolation`与`phase_ms.project_isolation`收集误报/漏报 | 证明拒绝且0写入；影子阶段不升阻断。测试探针应有明确归属，不冒充实际运营内容 |
| F-04 | 在WorkBuddy真实客户端测`open_project`/借卡等待、超时占比与p95 | 约22秒客户端截止尚未验收；MCP并发修复和评论18秒截止不能替代借卡验收，服务端60秒亦不能保证客户端完成 |
| W-06 | 查超线项目的发牌、会话中断、`commit_drafts`和`export_drafts`链路 | 本轮只读发现51%漏账，不自动补消费、删角度或改审核结果；先辨清真实流程断点 |
| J-08 / J-12 | 写手恢复后用`batch_metrics(meta.mode=deskcore_commit)`重估judge预算；核活跃项目tv-map | judge保持shadow；新超时值需真实样本，不凭旧runbook的“env未配”重复配置 |
| W-04 / W-05 / W-18 | 保留发牌结果加权、recent_angle_keys分页、旧文档逐段追平待办 | 加权未实施；当前交接补入口不表示全部旧文字已重写 |

Streamlit停服仍未执行；迁移011/012等更早状态不能从旧runbook“八个迁移/未执行”的段落推断，必要时用`doctor`只读核对。重新导skill由owner/运营在J-01就绪后统一安排，本轮不向写手发送消息。

## 下一位维护者的排查入口

```bash
curl -fsS https://autowriter-production.up.railway.app/health
python -m pytest tests/ -q
python -m deskcore.cli selftest
```

连库的`python -m deskcore.cli doctor`另按已有服务端凭据执行；本轮交接没有新跑doctor，不把健康接口当迁移证明。SQL/Python parity由CI独立PostgreSQL步骤验证，`tests/sql_parity_check.py`不是普通pytest。

现场配置材料在当前工作区`/workspace/rollout-audits/20261013/`，文件`w19-project-config-before.json`、`w19-project-config-after.json`、`w19-guard-validation.json`；换机器由owner安全转交，配置备份/owner或项目UUID/真实规则文本不追加上传GitHub。完整40条文件去Jev/TV交接所列本地目录获取。


## 评论线继续收尾 · 2026-10-15

用户委托整线完成再交接。已合并Jev14/TV185，C4候选及专用验证在[Jev15](https://github.com/TangVarie/JevforCoentent/pull/15)：231项测试通过，20条虚构用例131/140一致；不是生产验收。真实旧40开发/新40 AI参考输入已预检，因自动审批要求这80条新的C4出境批次明确许可而尚未调用。不能使用旧C3一次许可；也不能把新AI参考称真人金标准。

本库评论转发与协议不因候选准备而改变：服务端仍默认关闭，写手不得声称已通过。W-02统一重导skill应等最终具体报告接受及J-01小批真接口验证；本轮没有通知写手/群发。整体证据与受控材料索引见TV HANDOFF及Jev收尾报告，其他12:04生产快照未在本次重测。
