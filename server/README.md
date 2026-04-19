# AutoWriter · Feishu Backend

轻量 FastAPI 服务,让**飞书多维表格**直接触发 Claude / Gemini 生成小红书文案,生成结果写回表格。完全取代原有 Streamlit + Supabase 架构。

## 架构

```
飞书多维表格 (UI + 主库)
   │  "生成" 按钮 → 飞书自动化
   ▼
本服务 /generate (部署在 Railway)
   │
   ├─ 读 Batches[record_id]         → 项目 + 参数
   ├─ 读 Projects[project_id]       → system_prompt + 正/负例
   ├─ 读 Memories (启用 + 作用域过滤)
   ├─ 拼装完整 system prompt
   ├─ 并发调 Claude / Gemini
   └─ 批量写入 Items 表 + 更新 Batches 状态
```

---

## 多维表格设计

### 1. Projects

| 字段名 | 类型 | 说明 |
|---|---|---|
| 名称 | 文本 | 项目名(主字段) |
| 品牌 | 文本 | |
| `system_prompt` | 多行文本 | 基础系统提示词(必填) |
| tactics | 多选 | 可选策略列表,供 Batches 引用 |
| 正面示例 | 多行文本 | 格式见下文 |
| 负面示例 | 多行文本 | 同上 |
| 参考文件 | 附件 | 可选 |

**示例字段格式**(服务端解析,两种任选):

```
好物分享 | 这支口红真的巨好用,显白显气质...
通勤穿搭 | 西装+奶白短靴绝配...
```

或:

```
好物分享
这支口红真的巨好用,显白显气质...

通勤穿搭
西装+奶白短靴绝配...
```

### 2. Batches

| 字段名 | 类型 | 说明 |
|---|---|---|
| 批次名 | 文本 | 主字段(自己命名或用公式) |
| 项目 | 关联 → Projects | 必填 |
| tactic | 单选或文本 | 战术方向,如"种草""测评" |
| 数量 | 数字 | 每个引擎生成多少条(1–50) |
| 引擎 | 多选 | 选项:`claude` / `gemini` |
| 目标人群 | 文本 | 可选 |
| 核心卖点 | 多行文本 | 可选 |
| 语气 | 文本 | 可选 |
| 补充说明 | 多行文本 | 可选 |
| 状态 | 单选 | 选项:待生成 / 生成中 / 完成 / 失败 |
| 错误信息 | 多行文本 | 失败时由服务写入 |

### 3. Items

| 字段名 | 类型 | 说明 |
|---|---|---|
| 标题 | 文本 | 主字段 |
| 正文 | 多行文本 | |
| 关键词 | 文本 | 逗号分隔 |
| 引擎 | 文本 | 如 `claude/claude-sonnet-4-6` |
| 状态 | 单选 | 选项:待审核 / 已通过 / 需修改 |
| 反馈 | 多行文本 | 审核时填写 |
| 审核人 | 人员 | |
| 图片 | 附件 | |
| 批次 | 关联 → Batches | |
| 项目 | 关联 → Projects | |
| 错误信息 | 多行文本 | 如解析失败会写入 |

### 4. Memories

| 字段名 | 类型 | 说明 |
|---|---|---|
| 内容 | 文本 | 主字段,即记忆文本 |
| 范围 | 单选 | 选项:全局 / 项目 |
| 项目 | 关联 → Projects | 范围=项目 时必填,全局 留空 |
| 启用 | 复选框 | 只有打勾的记忆才会注入 prompt |

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

3. **在 Batches 表配置自动化**
   - 表格右上角"自动化" → 新建流程
   - 触发条件:**当记录满足条件时**,字段"状态" = "待生成"(或选"当字段更新"触发)
   - 执行动作:**发送 HTTP 请求**
     - URL:`https://<你的railway域名>/generate`
     - 方法:POST
     - Header 加 `Content-Type: application/json` 和 `X-Autowriter-Token: <你的 secret>`
     - Body:`{"batch_record_id": "{{记录ID}}"}`(用字段引用语法)
   - 保存并启用

> 飞书暂不支持多维表格的"按钮字段"直接触发 webhook —— 用"状态字段变化"作为触发条件即可:新建 Batches 行时状态=待生成,保存就自动跑。重跑改状态即可。

---

## 常见问题

**Q: 飞书自动化超时了**
A: `/generate` 立即返回 202,真正的生成在后台线程里跑。如果飞书仍然报超时,说明请求根本没到服务。检查 Railway 日志 + 自动化 URL 是否正确。

**Q: 生成失败了但我找不到错误信息**
A: 去对应 Batches 行看"错误信息"字段。Railway 日志有完整堆栈。

**Q: 为什么要把 memories 做成「启用」开关而不是自动合并?**
A: 用户诉求:"有问题我改进提示词或者写进反馈"。记忆应该用户有意识地管理,不是 AI 自动吸收。启用开关让用户能快速开关某条规则验证效果。

**Q: 需要保留 Streamlit 吗?**
A: 短期保留(现有 `app.py`、`requirements.txt`、Heroku `Procfile` 都不动)。验证飞书流程跑通后,再清理旧文件。
