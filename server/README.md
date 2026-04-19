# AutoWriter · Feishu Backend

轻量 FastAPI 服务,让**飞书多维表格**直接触发 Claude / Gemini 生成小红书文案,生成结果写回表格。完全取代原有 Streamlit + Supabase 架构。

## 架构

```
飞书多维表格 (UI + 主库)
   │  状态=待生成 → 飞书自动化 → POST /generate
   │  反馈填好后   → 飞书自动化 → POST /iterate
   ▼
本服务 (部署在 Railway,FastAPI)
   │
   ├ /generate ── 读 Batches → 项目 + 参数
   │              读 Projects → system_prompt + 调教笔记 + 战术后缀 + 正/负例 + 参考图片
   │              读 Items → 最近标题(跨批次去重) + 已通过/标正例的作为 live examples
   │              读 Memories (启用 + 作用域过滤)
   │              并发调 Claude / Gemini → 批量写入 Items + 更新 Batches 状态
   │
   └ /iterate ── 读 Items[record_id] → 反馈 + 上一版
                 读 Project / Batches → 组装同一套 system prompt
                 多轮对话让 LLM 基于反馈重写 → 更新该行 + 版本+1
```

---

## 多维表格设计

### 1. Projects

| 字段名 | 类型 | 必填 | 说明 |
|---|---|---|---|
| 名称 | 文本 | ✔ | 项目名(主字段) |
| 品牌 | 文本 | | |
| `system_prompt` | 多行文本 | ✔ | 基础系统提示词 |
| 调教笔记 | 多行文本 | | 审美偏好描述,注入 prompt(纯手动,不再 AI 生成) |
| 战术配置 | 多行文本 | | JSON,按战术名配 suffix(见下方格式) |
| 正面示例 | 多行文本 | | 见下方"示例字段格式" |
| 负面示例 | 多行文本 | | 同上 |
| 参考图片 | 附件 | | 上传的图片会作为 vision 输入传给 Claude/Gemini(最多 4 张) |

**示例字段格式**(服务端自动识别,两种任选):

```
好物分享 | 这支口红真的巨好用,显白显气质...
通勤穿搭 | 西装+奶白短靴绝配...
```

或:

```
好物分享
这支口红真的巨好用...

通勤穿搭
西装+奶白短靴...
```

**战术配置 JSON 格式**(两种任选):

```json
{"种草": "\n额外要求:开篇情境,结尾留白。", "测评": "\n额外要求:先讲优缺点再讲总结。"}
```

或:

```json
[
  {"name": "种草", "suffix": "\n额外要求:开篇情境,结尾留白。"},
  {"name": "测评", "suffix": "\n额外要求:先讲优缺点再讲总结。"}
]
```

### 2. Batches

| 字段名 | 类型 | 必填 | 说明 |
|---|---|---|---|
| 批次名 | 文本 | ✔ | 主字段 |
| 项目 | 关联 → Projects | ✔ | |
| tactic | 单选或文本 | | 匹配 Projects.战术配置 里的 name/key |
| 数量 | 数字 | | 每个引擎生成多少条(1–50,默认 1) |
| 引擎 | 多选 | | 选项:`claude` / `gemini`,默认 claude |
| 目标人群 / 核心卖点 / 语气 / 补充说明 | 多行文本 | | 可选 |
| Claude模型 | 单选 | | 如 `claude-opus-4-6` / `claude-sonnet-4-6`,不填用默认 |
| Gemini模型 | 单选 | | 如 `gemini-3.1-pro-preview` / `gemini-2.5-pro` |
| Claude深度思考 | 复选框 | | 开启 extended thinking |
| Gemini深度思考 | 复选框 | | 开启 Gemini thinking |
| 状态 | 单选 | | 选项:待生成 / 生成中 / 完成 / 失败 |
| 错误信息 | 多行文本 | | 服务写入 |

### 3. Items

| 字段名 | 类型 | 必填 | 说明 |
|---|---|---|---|
| 标题 | 文本 | ✔ | 主字段,服务写入,人工可改 |
| 正文 | 多行文本 | | 服务写入,人工可改 |
| 关键词 | 文本 | | 逗号分隔 |
| 引擎 | 文本 | | 如 `claude/claude-sonnet-4-6` |
| 版本 | 数字 | | 服务写入,每次 `/iterate` 后 +1 |
| 状态 | 单选 | | 选项:待审核 / 生成中 / 已通过 / 需修改 |
| 反馈 | 多行文本 | | **审核时填写**,触发 `/iterate` 重写;重写完会被清空 |
| 示例标记 | 单选 | | 选项:正例 / 负例(留空即不作示例) |
| 审核人 | 人员 | | 多维表格原生 |
| 图片 | 附件 | | 可选,方便素材协同 |
| 批次 | 关联 → Batches | | |
| 项目 | 关联 → Projects | | |
| 错误信息 | 多行文本 | | 服务写入 |

### 4. Memories

| 字段名 | 类型 | 必填 | 说明 |
|---|---|---|---|
| 内容 | 文本 | ✔ | 主字段,即记忆文本 |
| 范围 | 单选 | ✔ | 选项:全局 / 项目 |
| 项目 | 关联 → Projects | | 范围=项目 时必填 |
| 启用 | 复选框 | | 只有打勾的记忆才会注入 prompt |

> 没有 LLM 反馈分类 —— 用户审核时把反馈写进 Items.反馈,自己决定要不要新增到 Memories。

---

## 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `ANTHROPIC_API_KEY` | ✔ | |
| `ANTHROPIC_BASE_URL` | | 可选代理 |
| `CLAUDE_MODEL` | | 默认 `claude-sonnet-4-6` |
| `GOOGLE_API_KEY` | | 用 Gemini 时必填 |
| `GOOGLE_BASE_URL` | | 可选代理 |
| `GEMINI_MODEL` | | 默认 `gemini-3.1-pro-preview` |
| `FEISHU_APP_ID` | ✔ | 飞书自建应用 |
| `FEISHU_APP_SECRET` | ✔ | 同上 |
| `FEISHU_BITABLE_APP_TOKEN` | ✔ | 多维表格 URL 里 `base/xxxxx` 段 |
| `FEISHU_TABLE_PROJECTS` | ✔ | 对应 URL `?table=xxx` 的 table_id |
| `FEISHU_TABLE_BATCHES` | ✔ | |
| `FEISHU_TABLE_ITEMS` | ✔ | |
| `FEISHU_TABLE_MEMORIES` | | 不配置则跳过 memory 注入 |
| `WEBHOOK_SHARED_SECRET` | | 建议配置;飞书自动化请求头 `X-Autowriter-Token` 要带这个值 |
| `PORT` | | 由 Railway 自动注入 |

---

## 本地运行

```bash
pip install -r requirements-server.txt
export $(cat .env | xargs)   # 或手动 export
uvicorn server.main:app --reload --port 8000
```

健康检查:`curl localhost:8000/healthz` 返回 `{"ok": true, "missing_env": []}` 即可。

手动触发一次生成(需要先在 Bitable 里建好一条 Batches 行):

```bash
curl -X POST localhost:8000/generate \
  -H 'Content-Type: application/json' \
  -H "X-Autowriter-Token: $WEBHOOK_SHARED_SECRET" \
  -d '{"batch_record_id": "recXXXXXXXX"}'
```

---

## 部署到 Railway

1. 在 Railway 新建项目,连接 GitHub 仓库,分支选 `claude/migrate-to-feishu-tables-JX9Ou`(或后续合入后的 main)
2. Railway 会自动读取 `railway.toml`,用 `requirements-server.txt` 装依赖,启动 uvicorn
3. 在 Variables 里配置上文所有环境变量
4. 部署后拿到公网域名,例如 `https://autowriter-xxxx.up.railway.app`
5. 健康检查:浏览器访问 `/healthz`

---

## 飞书自动化配置

1. **创建自建应用**(open.feishu.cn 后台)
   - 权限:`bitable:app`(或最小化 `bitable:app:readwrite`)
   - 应用发布后,记下 `App ID` / `App Secret`
   - 在多维表格右上角"…" → "添加协作者" → 添加你的应用(否则调 API 会 403)

2. **获取 token**
   - `FEISHU_BITABLE_APP_TOKEN`:表格 URL `https://xxx.feishu.cn/base/BASCxxxxxx?table=tblxxx` 里的 `BASCxxxxxx`
   - `FEISHU_TABLE_*`:URL 参数 `table=tblxxx` 的 `tblxxx`

3. **Batches 表自动化(首次生成)**
   - 表格右上角"自动化" → 新建流程
   - 触发条件:**当记录满足条件时**,字段"状态" = "待生成"
   - 执行动作:**发送 HTTP 请求**
     - URL:`https://<你的railway域名>/generate`
     - 方法:POST
     - Header 加 `Content-Type: application/json` 和 `X-Autowriter-Token: <你的 secret>`
     - Body:`{"batch_record_id": "{{记录ID}}"}`(用字段引用语法)
   - 保存并启用

4. **Items 表自动化(基于反馈重写)**
   - 触发条件:**当字段更新时**,字段"状态" 变为 "需修改"
     (也可以改成:字段"反馈" 不为空且 状态 = "需修改")
   - 执行动作:**发送 HTTP 请求**
     - URL:`https://<你的railway域名>/iterate`
     - 方法 / header 同上
     - Body:`{"item_record_id": "{{记录ID}}"}`
   - 保存并启用

> 飞书暂不支持多维表格的"按钮字段"直接触发 webhook —— 统一用"状态字段变化"作为触发条件:新建/重跑 Batches 行时改状态=待生成,Items 行要重写时填写反馈并改状态=需修改。

---

## 功能对照(与原 Streamlit 系统)

| 原系统功能 | 新系统实现方式 |
|---|---|
| 项目 system_prompt / 品牌 / 示例 | Projects 表字段 |
| 调教笔记(品味描述) | Projects.调教笔记,手动编辑,拼入 prompt(AI 自动生成被取消) |
| 战术方向 + 独立 suffix | Projects.战术配置(JSON),Batches.tactic 字段引用 |
| Claude / Gemini 引擎选择 + 模型选择 + 深度思考 | Batches 对应字段 |
| 目标人群 / 卖点 / 语气 / 补充说明 | Batches 对应字段 |
| 跨批次去重(避免重复角度) | `/generate` 自动拉取该项目最近 30 条 Items 标题作为 dedup 上下文 |
| 通过的文案 → 自动成为未来示例 | `/generate` 自动把该项目里 **示例标记=正例** 或 **状态=已通过** 的 Items 当作正例;手动填写的正面示例优先 |
| 负例标记 | Items.示例标记=负例 |
| 反馈 → 重新生成 | 在 Items 填反馈 + 状态=需修改 → 触发 `/iterate`,该行就地更新,版本+1 |
| 审核状态 / 批次历史 / 过滤 | Bitable 原生视图 + 筛选 |
| 记忆管理(项目/全局) | Memories 表,手动维护(LLM 分类被取消);"启用"复选框控制是否注入 |
| 多用户权限隔离 | Bitable 协作者 + 应用权限 |
| 参考图片(vision) | Projects.参考图片 附件,`/generate` 自动下载 base64 传给 Claude/Gemini |
| Excel / Word 导出 | Bitable 原生"导出"按钮即可 |
| Feishu webhook 推送 | 数据已经在飞书,不需要再推 |
| 三省法 / 六部精炼 / AI 自动生成调教笔记 / LLM 反馈分类 | **明确取消**,按用户决定通过"改 prompt + 反馈"替代 |

## 常见问题

**Q: 飞书自动化超时了**
A: `/generate` 立即返回 202,真正的生成在后台线程里跑。如果飞书仍然报超时,说明请求根本没到服务。检查 Railway 日志 + 自动化 URL 是否正确。

**Q: 生成失败了但我找不到错误信息**
A: 去对应 Batches 行看"错误信息"字段。Railway 日志有完整堆栈。

**Q: 为什么要把 memories 做成「启用」开关而不是自动合并?**
A: 用户诉求:"有问题我改进提示词或者写进反馈"。记忆应该用户有意识地管理,不是 AI 自动吸收。启用开关让用户能快速开关某条规则验证效果。

**Q: 需要保留 Streamlit 吗?**
A: 短期保留(现有 `app.py`、`requirements.txt`、Heroku `Procfile` 都不动)。验证飞书流程跑通后,再清理旧文件。
