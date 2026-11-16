// 写作台插件的纯逻辑：地址和钥匙的解析、协议的拉取、系统提示怎么拼、
// 每个会话的流程状态。这里不 import 任何 DeepSeek Harness 的包，
// 所以 `node --test` 在没装 DSH 的机器上（CI）也能跑。

/** MCP 服务在 DSH 里的名字；工具名前缀由它定。 */
export const SERVER_NAME = 'deskcore'
export const TOOL_PREFIX = `mcp__${SERVER_NAME}__`

/** 凭据库里的两个引用名。值只存在运营自己的机器上。 */
export const URL_REF = 'BYWOOD_DESK_URL'
export const KEY_REF = 'BYWOOD_DESK_KEY'

/** 没开项目不许调的三个工具：发牌、查重、存档。 */
export const GATED_TOOLS = new Set(['draw_angles', 'check_drafts', 'score_drafts'])

const PROTOCOL_TIMEOUT_MS = 15_000

/**
 * 把运营粘进来的地址收成不带尾巴的基址。
 * `https://x/mcp`、`https://x/mcp/`、`https://x/` 都认。
 * @returns {{ ok: true, base: string } | { ok: false, reason: string }}
 */
export function normalizeBase(raw) {
  const text = String(raw ?? '').trim()
  let url
  try {
    url = new URL(text)
  } catch {
    return { ok: false, reason: '地址不是一个完整的网址（要以 https:// 开头）。' }
  }
  const local = url.hostname === 'localhost' || url.hostname === '127.0.0.1'
  if (url.protocol !== 'https:' && !(local && url.protocol === 'http:')) {
    return { ok: false, reason: '地址要以 https:// 开头。' }
  }
  if (url.username || url.password || url.search || url.hash) {
    return { ok: false, reason: '地址里不要带账号、问号参数或 # 。钥匙单独写在地址后面。' }
  }
  const path = url.pathname.replace(/\/+$/, '').replace(/\/mcp$/, '')
  return { ok: true, base: `${url.origin}${path}` }
}

/**
 * 解析 `/desk-setup <地址> <钥匙>` 后面那一串。
 * 钥匙两边的引号、全角空格、不间断空格都去掉——复制粘贴最容易带上这些。
 * @returns {{ ok: true, base: string, key: string } | { ok: false, reason: string }}
 */
export function parseSetup(rawInput) {
  const parts = String(rawInput ?? '')
    .replace(/[ 　]/g, ' ')
    .trim()
    .split(/\s+/)
    .filter(Boolean)
  if (parts.length !== 2) {
    return { ok: false, reason: '格式是：/desk-setup 地址 钥匙（中间一个空格，一共两段）。' }
  }
  const base = normalizeBase(parts[0])
  if (!base.ok) return base
  const key = parts[1].replace(/^["'“”‘’]+|["'“”‘’]+$/g, '')
  if (/^bearer$/i.test(key) || key.length < 8) {
    return { ok: false, reason: '钥匙看起来不完整。要贴整把，不带 Bearer。' }
  }
  return { ok: true, base: base.base, key }
}

/**
 * 从服务端取协议全文。走 deskcore 的纯 REST 通道，不经过模型。
 * @param {{ base: string, key: string, fetchImpl?: typeof fetch, timeoutMs?: number }} options
 * @returns {Promise<{ ok: true, version: string, protocol: string, header: string } | { ok: false, reason: string }>}
 */
export async function fetchProtocol({ base, key, fetchImpl = fetch, timeoutMs = PROTOCOL_TIMEOUT_MS }) {
  let response
  try {
    response = await fetchImpl(`${base}/tool/get_protocol`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'X-Deskcore-Key': key },
      body: '{}',
      signal: AbortSignal.timeout(timeoutMs),
    })
  } catch (error) {
    const timedOut = error?.name === 'TimeoutError' || error?.name === 'AbortError'
    return { ok: false, reason: timedOut ? '连接超时' : `连不上（${error?.message ?? error}）` }
  }
  if (response.status === 401 || response.status === 403) {
    return { ok: false, reason: `钥匙不对（服务端回了 ${response.status}）` }
  }
  if (!response.ok) return { ok: false, reason: `服务端回了 ${response.status}` }
  let body
  try {
    body = await response.json()
  } catch {
    return { ok: false, reason: '服务端回的不是 JSON，地址可能填错了' }
  }
  const result = body?.result
  if (typeof result?.protocol !== 'string' || typeof result?.version !== 'string' || !result.protocol.trim()) {
    return { ok: false, reason: '服务端没有回协议正文，地址可能填错了' }
  }
  return {
    ok: true,
    version: result.version,
    protocol: result.protocol,
    header: typeof result.skill_header === 'string' ? result.skill_header : '',
  }
}

const NOT_SET_UP = `# 写作台（还没配置）

这台电脑装了写作台插件，但还没填地址和钥匙，写作台现在用不了。

用户要你写、改给任何品牌发的小红书内容（笔记、标题、评论）时：**不要写稿**。原话告诉他：
「写作台还没配置。请在输入框里输入 /desk-setup，后面跟上负责人给你的地址和钥匙，中间用空格隔开，然后新开一个对话。」

别的事情照常做。`

function notConnected(reason) {
  return `# 写作台（没连上）

写作台插件配置过了，但现在取不到协议：${reason}。

用户要你写、改给任何品牌发的小红书内容（笔记、标题、评论）时：**不要写稿**——现在写出来的稿子不会查重，写作台也记不下来。原话告诉他：
「这个对话没连上写作台（${reason}）。请在输入框里输入 /desk-status 看一下状态，还不行就找负责人。」

不要说「服务中断了」「掉线了」「网络波动」。别的事情照常做。`
}

function connected({ version, protocol, header }) {
  return `# 写作台

下面是写作台协议，由写作台插件从服务端取来、放在系统提示里，整场对话都在。只要这场对话要产出或修改给任何品牌发的小红书内容（笔记、标题、评论），就照它做；用户没提写作台、只报了品牌名、只贴了 brief、同时点了别的 skill，都一样。

在这个客户端里有三点和正文写的不一样，以这三点为准：

1. **工具名带前缀。** 正文里写的 \`open_project\`、\`check_drafts\` 这些，在这里的名字是 \`${TOOL_PREFIX}open_project\`、\`${TOOL_PREFIX}check_drafts\`，其余工具同理。工具列表里找不到它们，才算没连上写作台。
2. **开场不用核版本。** 这份正文已经是服务端的最新版，不用调 \`get_protocol\`，也不用提「重新导入 skill」「更新技能」。\`open_project\` 和 \`comments_for\` 的 \`skill_version\` 填下面这一行的值。
3. **没开项目，发牌、查重、存档会被插件拦下。** 被拦时照提示先调 \`open_project\`，不要换别的办法绕。对话被压缩之后也要重新开一次项目。

protocol_version: ${version}

${header.trim()}

${protocol.trim()}`
}

/**
 * 当前该放进系统提示的那一段。
 * @param {{ configured: boolean, protocol?: { version: string, protocol: string, header: string }, lastError?: string }} status
 */
export function buildSection(status) {
  if (!status.configured) return NOT_SET_UP
  if (!status.protocol) return notConnected(status.lastError ?? '还在连接')
  return connected(status.protocol)
}

/** 去掉前缀后的写作台工具名；不是写作台的工具返回 undefined。 */
export function deskTool(name) {
  return typeof name === 'string' && name.startsWith(TOOL_PREFIX) ? name.slice(TOOL_PREFIX.length) : undefined
}

/**
 * 从一次工具调用的结果里取出 deskcore 回的那个对象。
 * 出错、解析不了、或者对象里带 `error`（deskcore 有些工具把失败包成带 error 的成功）都返回 undefined。
 */
export function payloadOf(result) {
  if (!result || result.isError) return undefined
  const value = result.value
  let payload
  if (value && typeof value === 'object' && value.structuredContent && typeof value.structuredContent === 'object') {
    payload = value.structuredContent
  } else {
    const blocks = Array.isArray(value?.content) ? value.content : Array.isArray(result.content) ? result.content : []
    const text = blocks.find(block => block?.type === 'text' && typeof block.text === 'string')?.text
    if (text === undefined) return undefined
    try {
      payload = JSON.parse(text)
    } catch {
      return undefined
    }
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return undefined
  if (payload.result && typeof payload.result === 'object' && !Array.isArray(payload.result) && Object.keys(payload).length === 1) {
    payload = payload.result
  }
  if (typeof payload.error === 'string' && payload.error) return undefined
  return payload
}

/** 一个会话的流程状态。 */
export function newState() {
  return {
    /** 这个会话里开过的项目。压缩之后清空。 */
    opened: new Set(),
    /** 查过重、还没存档。 */
    pendingArchive: false,
    /** 这一轮已经催过一次存档。 */
    nudged: false,
    /** 压缩之后要补一句提醒。 */
    reopenNotice: false,
  }
}

const projectOf = args => (args && typeof args === 'object' && typeof args.project_id === 'string' ? args.project_id.trim() : '')

/**
 * 工具执行前：没开项目就拦下发牌、查重、存档。
 * @returns {{ kind: 'allow' } | { kind: 'deny', reason: string }}
 */
export function gate(state, toolName, args) {
  const tool = deskTool(toolName)
  if (tool === undefined || !GATED_TOOLS.has(tool)) return { kind: 'allow' }
  const project = projectOf(args)
  if (project && state.opened.has(project)) return { kind: 'allow' }
  return {
    kind: 'deny',
    reason: `写作台插件拦下了这次 ${tool}：这个对话里还没有为这个项目调过 ${TOOL_PREFIX}open_project`
      + '（或者对话刚被压缩过，项目规则已经不在眼前）。'
      + `先调 ${TOOL_PREFIX}open_project 拿到简报和硬约束，再继续。不知道 project_id 就先调 ${TOOL_PREFIX}list_projects。`,
  }
}

/** 工具执行后：记下开了哪个项目、有没有欠着存档。 */
export function observe(state, toolName, args, result) {
  const tool = deskTool(toolName)
  if (tool === undefined) return
  const payload = payloadOf(result)
  if (payload === undefined) return
  if (tool === 'open_project') {
    const project = projectOf(args) || (typeof payload.project_id === 'string' ? payload.project_id.trim() : '')
    if (project) state.opened.add(project)
    state.reopenNotice = false
  } else if (tool === 'check_drafts') {
    state.pendingArchive = true
  } else if (tool === 'score_drafts') {
    state.pendingArchive = false
  }
}

/** 新的一轮开始。 */
export function turnStarted(state) {
  state.nudged = false
}

/** 对话压缩完成：项目规则不在眼前了，要求重新开项目。 */
export function compacted(state) {
  if (state.opened.size === 0) return
  state.opened.clear()
  state.reopenNotice = true
}

export const REOPEN_NOTICE = `对话刚被压缩过，之前 ${TOOL_PREFIX}open_project 拿到的项目规则和硬约束已经不在上下文里了。`
  + `接着写、改这个项目的稿子之前，先重新调一次 ${TOOL_PREFIX}open_project；这是你自己的事，不用跟用户提。`

/** 下一步开始前要不要补那句「重新开项目」。取走就清掉。 */
export function takeReopenNotice(state) {
  if (!state.reopenNotice) return undefined
  state.reopenNotice = false
  return REOPEN_NOTICE
}

export const ARCHIVE_NUDGE = `这一轮查过重，但还没调 ${TOOL_PREFIX}score_drafts。`
  + '查重通过、已经交给用户或正要交的稿子，现在就调它存档（一次最多 10 篇，带上各自的 angle_key），然后再结束这一轮。'
  + '没有交付的不用存：被你或用户毙掉的、用户坚持用撞车原稿的。这种情况直接结束，不用解释这条提醒。'

/** 这一轮要收尾了：欠着存档就催一次，一轮只催一次。 */
export function stopping(state) {
  if (!state.pendingArchive || state.nudged) return undefined
  state.nudged = true
  state.pendingArchive = false
  return ARCHIVE_NUDGE
}
