# autowriter / deskcore 接手卡 · 2026-10-22

先读 [三仓总交接PR TV#215](https://github.com/TangVarie/truth-vault/pull/215) 中的 docs/handoff-2026-10-22-next-owner.md，包含授权原话、操作顺序、私有补充包和回滚。本文件只保留aw执行入口；下方历史HANDOFF中的“未改base”“仍审c709793”等不再是现状。

## 当前停点

| 项目 | 事实 |
|---|---|
| 默认分支 | claude/xhs-content-workstation-BSEuH，7042cebc1b723e29dd32ba1c17ad988d917a5cd8 |
| 已合并 | #121对#115同一最终功能提交875bfca有效过审/CI后合并；原#115自动标记merged，保留旧讨论 |
| 未合并 | #117，head fdeaa9f72cac193fa04201e8c466342e6fd569ba，base已改为默认分支；审查completed/false、0未解决线程、CI39718629002 success |
| 实际生产 | 北京10-22 11:57左右只读/health仍f0044905bda1，协议5ea3b14e1f37；不能称#121已经运行 |
| 015与私有操作 | rule_compiles尚不存在，模板/格式规则/代码检查备份及退路日志表均不存在；四项目未配置delivery |
| 本轮交接 | 只写文档，不合并#117、不继续部署、不执行生产写入 |

#121上线后的协议895ed01874d6；#117上线后的协议be4d668f5ab6。对应编译runbook、015与最终完整验证记录在 **#117分支claude/aw-e-check-rules**，不要只读当前默认分支就误判文件丢失。

## 接着做

1. 用接手人已有身份核Railway web及cron真实service、commit/config/分支/日志；我的环境缺身份不代表你的也缺。用户已明确授权既定生产上线与有备份的数据变更，无需再申请同一授权。
2. 先确认#121实际部署，再按总交接备份；015必须先于#117部署。四模板只选原包bjs/xiwu/hatherine/tuge.json，不用冲突备选；保留JSON string存储和其它参数，备份/CAS守卫不符就查原因。
3. #117已经完成改base/同步；核当前最终head门禁、015访问及RULE_COMPILE_AUTO=0后再合并部署。CLI先compile-rules --all --dry-run，再真编，禁止同时开服务自动补编或加--force；回读缓存后恢复自动补编，核真实鉴权接口与skill协议。
4. 四项目真实delivery.text/problems验收通过后，按W28先插3条退路、备份再降6条旧格式规则；只给ec0b9bd6加手设检查。总交接有脚本映射，客户原文不进仓库。
5. tv-sync先查10-21日志再补跑正确版本：BJS82/SPX65条真实可比较差异待补；排除ingested和lag<0，并核当前version。相同版本重算保留computed_at，不要求每行时间前进。

## 已验证，避免重做

#121修复限时引擎缺失回退、不同item历史样本、URL括号/fragment、Unicode禁词边界、异常tags和长空白性能；双方正则源长均300。#117已补promote/unmute/方向变化和global激活编译、排队共同截止、手设检查计数、坏题/重复题未查及完整接口CI。

#117最终功能集1905 passed、6既有xfailed、0 skipped，selftest与sync-skill通过；最终同步fdeaa9f只新增默认分支HANDOFF文档，CI再次成功。#121相关记录在docs/review-2026-10-21.md；148个合成正则与Jev对拍差异0。本地接口测试需允许本地通信，不能用缺FastAPI/MCP的skip冒充通过。纯文档交接不重复跑全量业务测试。

## 工作树与回滚

/workspace/worktrees/aw-117是待发功能树；本handoff分支只改文档。aw115-before、aw117-before为故意覆盖新测试的反证树，可能dirty，不要push或合并。/tmp依赖和日志不保证跨环境持久。

#117回退优先回已验收的#121，关闭自动补编并保留015/缓存；若连#121也回退，先核已降旧格式规则并按私有带守卫回滚恢复。模板仅移除本次仍匹配的delivery键，保留后来其它修改。完整操作/回滚、Supabase及Railway项目入口、私有包领取说明都在TV#215。
