# R-032 · autowriter 接入 TV 飞轮馆员(pull)— 消费侧对接说明

> 面向:autowriter 维护者(你)+ TV 馆员服务维护者(对接对象)。
> 配套 PR:`claude/vigilant-brown-3vYdm` → 默认分支。
> 对应规范:TV 侧 docs/15(R-032)。本文是 **autowriter 实际落地后的消费侧契约**,
> 用来跟 TV 侧核对"我发什么、我读什么、配在哪、怎么联调"。

---

## 1. 一句话

通道2 从 push 改 pull 后的消费侧:autowriter 每次写稿(batch 生成)前,按本次
brief 向 TV 馆员服务 `POST /librarian` 借阅匹配的"真实爆款经验",把返回的
`selected` 注入分层 system prompt 的 **P2 会话层(不缓存)**。借阅失败/超时/未配
一律降级成"没有飞轮料",写稿照常 —— **飞轮永远不是写稿的前置依赖**。

---

## 2. 需要配置的环境变量(对接第一步)

| 变量 | 必填 | 说明 |
|---|---|---|
| `LIBRARIAN_URL` | 是(接飞轮时) | TV 馆员服务地址,例 `https://truth-vault-production.up.railway.app`。**TV 侧提供** |
| `LIBRARIAN_API_KEY` | 是(接飞轮时) | 内部鉴权 key,作为 `X-Librarian-Key` 头发出。**TV 侧提供** |
| `LIBRARIAN_TIMEOUT_SEC` | 否 | 借阅超时秒数,默认 `8` |

**两个值留空 = 不接飞轮**(写稿照常,只是少了"真实爆款参照"这一节)。

### ⚠️ 配在哪个 service(重要部署细节)

当前 autowriter 的**生成逻辑跑在 Streamlit app 进程内的后台 daemon 线程**里
(`app.py` 的 `_queue_worker_impl` / `_quick_gen_worker`),**不是** `worker.py`
——后者(R-018)目前只有 `noop` handler,生成还没搬过去。

> 所以这两个环境变量现在要配在**跑 Streamlit 的那个 service / secrets** 上
> (Streamlit Cloud → App settings → Secrets;Render/Railway → web service 的 env)。
> 等 R-018 Phase 2 把生成搬进 `worker.py` 后,worker service 也要补配同样的值。

---

## 3. autowriter 实际发出的 brief(请 TV 侧核对解析)

`POST {LIBRARIAN_URL}/librarian`,头 `X-Librarian-Key: {LIBRARIAN_API_KEY}`,
body 由 `librarian_client.build_brief()` 组装:

```jsonc
{
  "consumer": "autowriter",
  "project_id": "<项目 uuid>",

  // —— 项目级稳定字段(馆员可 prompt-cache 这部分)——
  "brand":              "<project.brand>",
  "project_name":       "<project.name>",
  "system_prompt":      "<project.system_prompt>",
  "system_prompt_tone": "<project.system_prompt_tone>",
  "system_prompt_exec": "<project.system_prompt_exec>",
  "tactics":            "<project.tactics 原始形态>",   // 见下方 §6 待确认 4
  "calibration_notes":  "<project.calibration_notes>",

  // —— 本次 batch 的 delta ——
  "tactic":             "<本批战术名>",
  "key_messages":       "<本批核心卖点>",   // R-032 回执:已加入 delta(同卖点优先匹配)
  "target_audience":    "<本批受众>",
  "tone":               "<本批语气>",
  "extra_instructions": "<本批额外指令>"

  // "draft_topic": <可选, 当前未填 —— 与 key_messages 语义不同, aw 暂无真选题字段>
}
```

- 项目某列缺失 → 对应值为 `null`(契约约定馆员按缺失处理,不 500)。
- **未发送的字段**:`draft_topic`(选题)—— 与 `key_messages` 语义不同,aw 暂无真选题字段(见 §6)。

---

## 4. autowriter 实际消费的响应字段(请 TV 侧核对产出)

期望 `200 {"selected": [ {...}, ... 0–5 条 ]}`;空库/馆员内部错 → `{"selected": []}`。

autowriter 当前**读取并注入这 7 个字段**(`memory.build_layered_system_prompt`
渲染进 P2):

| 字段 | 用途 | 处理 |
|---|---|---|
| `hook_type` | 钩子类型 | 空则显示 `?` |
| `structure` | 结构 | 空则显示 `?` |
| `why_it_worked` | 为何有效 | 原样 |
| `transferable_tactic` | 可迁移手法 | 原样 |
| `borrow_what` | 让模型借鉴什么 | 原样 |
| `why_relevant` | 与本次相关性 | 原样 |
| `excerpt` | 原文片段 | **截断到 200 字** |

> **仍不注入 prompt 的字段**(R-032 回执 §8.3):`tier`(轻量权重信号、价值低)、
> `source_note_id`(内部 id,对模型是噪音)。它们能正常收到、但不进 system prompt。

渲染出的 P2 节(有料时)形如:

```
[真实爆款参照 · 系统按本次选题从帆谷飞轮库匹配]
下面是现实中真爆过 / 运营确认值得参考的帆谷笔记的提炼经验。借鉴其钩子 / 结构 /
手法与角度，**严禁照抄原文的标题主干或具体句子**。

· 钩子：反差｜结构：痛点→反转→产品｜为何有效：前3秒制造意外
  可迁移手法：用"翻车经历"做钩子再带出解决方案
  借这条的：开场反差结构（相关性：同为经期场景）
  原文片段：姐妹些，痛经那天我直接……
```

与 owner 在 Memory Manager 手标的 `[优质正案例]`(P1 层)**并列、不替代**
(docs/14 §1:owner 主观判断 ⊕ 飞轮客观经验)。

---

## 5. 注入位置与降级语义

- **注入到 P2(会话层,`cache_control` 不缓存)**:`selected` 随 brief(tactic/选题)
  每批变,放进缓存的 P1 会把 Anthropic/Gemini 的 prompt cache **每批打穿**;P2 本
  就是 ephemeral 层,放这里不影响稳定前缀的缓存命中。
- **降级(fail-open)**:未配 URL/KEY、超时、网络错、4xx/5xx、返回非预期结构 →
  `fetch_flywheel_lessons` 返回 `[]` → P2 那节不出现 → 写稿照常用 owner 自有正例。
  失败会打一条 telemetry:`flywheel_librarian_unavailable`(带 `project_id` +
  脱敏后的 error),不抛异常、不阻塞、不重试。
- **空库期**:TV 书架还没有真·非 synthetic 爆款前,`selected` 基本都是 `[]`,
  接好了也"看不到效果"是正常的 —— 先把管子接通。

---

## 6. 与规范(docs/15)的差异 & 待 TV 侧确认项

落地时发现规范对 autowriter 内部结构的几处描述与实际不符,已据实调整(不影响契约):

| # | 规范说法 | 实际 / 我的处理 |
|---|---|---|
| A | §3:在 `generate_batch` 里调馆员、调 `build_layered_system_prompt` 之前 | `generate_batch` **不构建** system prompt(收的是已建好的)。真正构建点在 `app.py` 三处。已把借阅接在两个 batch 生成点(`_queue_worker_impl` / `_quick_gen_worker`),迭代点不接(scope 是新批生成) |
| B | §4:P2 已有 `p2_sections` 列表 | 实际 P2 原是单字符串。已重构成 `p2_sections` 列表;**无飞轮料时 P2 与旧实现 byte-identical**,不破坏缓存前缀 |
| C | §2:用 `httpx.post` | 改用 `requests`(仓库已直接依赖、飞书 webhook 同款;httpx 仅 supabase 传递依赖)。**零新增依赖** |

**4 个待确认项 → 已拍板(R-032 回执, 2026-06-02)+ 双方已落地:**

1. **`key_messages` 要发** ✅ —— 同卖点优先匹配。TV 已把它加进馆员 brief delta +
   结果缓存 key;aw 已在 brief 发送(`build_brief` 加 delta + 两个 batch 生成点透传)。
2. **`draft_topic` 不用 `key_messages` 顶替** ✅ —— 语义不同(核心卖点 ≠ 选题)。
   `key_messages` 走自己的槽(见 1);`draft_topic` 保持可选,aw 暂无真选题字段就不发。
3. **`structure` + `transferable_tactic` 注入 prompt** ✅ —— 经验卡核心,已加进 §4 的
   P2 渲染。`tier` 不注入(轻量信号、价值低),`source_note_id` 不注入(内部 id 噪音)。
4. **`tactics` 形态** ✅ —— 发 `project["tactics"]` 原始值即可;TV 已用
   `json.dumps(sort_keys=True)` 稳定化缓存块,aw 无需特殊处理。

> 契约本身**未变**(brief 多一个可选 `key_messages`、响应字段早就返回 `structure`/
> `transferable_tactic`)。TV 侧改动仅 `librarian/core.py` 两处,无 schema/接口变更。

---

## 7. 联调步骤

```bash
# ① 健康检查
curl -s "$LIBRARIAN_URL/health"            # 期望 {"ok":true,...}

# ② 带 key 发一个 brief(空库期)
curl -s -X POST "$LIBRARIAN_URL/librarian" \
  -H "X-Librarian-Key: $LIBRARIAN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"consumer":"autowriter","project_id":"x","brand":"waytogo",
       "tactic":"经期场景","target_audience":"经期人群"}'
# 期望 {"selected":[]}(空库)

# ③ 鉴权:不带/带错 key → 期望 401
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$LIBRARIAN_URL/librarian" -d '{}'
```

④ 把 `LIBRARIAN_URL/KEY` 配到 Streamlit service 后,在 app 里跑一个真 batch:
   - 看「注入可视化」里 `flywheel_lessons` 计数;
   - 有料时,生成所用 system prompt 的 P2 出现 `[真实爆款参照]` 节;
   - 借阅失败时,日志出现 `flywheel_librarian_unavailable` 事件,但生成不受影响。

---

## 8. 本次变更文件清单

| 文件 | 改动 |
|---|---|
| `librarian_client.py`(新) | `build_brief()` 组装 brief + `fetch_flywheel_lessons()` 发 HTTP 借阅、全异常 fail-open 成 `[]` |
| `config.py` | 加 `LIBRARIAN_URL` / `LIBRARIAN_API_KEY` / `LIBRARIAN_TIMEOUT_SEC` |
| `memory.py` | `build_layered_system_prompt` 加 `flywheel_lessons` 形参 → P2 注入 `[真实爆款参照]` 节 + `report_sink` 计数 |
| `app.py` | import `librarian_client`;`_queue_worker_impl` / `_quick_gen_worker` 两处借阅 + 透传 |
| `.github/workflows/ci.yml` | import 图纳入 `librarian_client` |

**不动**(符合 docs/15 §5):`items.example_label`(owner 手标正/负例)及其消费、
negative proposal 通道、旧 TV push 进 items 那条。
