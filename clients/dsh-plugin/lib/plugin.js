// 把 lib/desk.js 的逻辑接到 DeepSeek Harness 的扩展点上。
// DSH 的东西（ctx、createUserMessage）都从参数进来，这个文件自己不 import DSH，
// 所以能用假的 ctx 做单元测试。真正的入口在 ../index.js。

import {
  KEY_REF, SERVER_NAME, URL_REF,
  buildSection, compacted, fetchProtocol, gate, newState, observe, parseSetup,
  stopping, takeReopenNotice, turnStarted,
} from './desk.js'

const REFRESH_MS = 30 * 60 * 1000
const SECTION_NAME = 'bywood-writing-desk'
const SOURCE_KIND = 'bywood-writing-desk'

/**
 * @param {object} ctx - 插件的 Cordis 上下文。
 * @param {{ createUserMessage: Function, fetchImpl?: typeof fetch, refreshMs?: number }} deps
 */
export function install(ctx, { createUserMessage, fetchImpl, refreshMs = REFRESH_MS }) {
  const status = { configured: false, base: undefined, protocol: undefined, lastError: undefined, fetchedAt: undefined }
  const states = new WeakMap()
  let unregisterServer
  let registeredBase
  let disposed = false
  let generation = 0

  const stateOf = (session) => {
    let state = states.get(session)
    if (state === undefined) {
      state = newState()
      states.set(session, state)
    }
    return state
  }

  const notice = (text, summary) => createUserMessage({
    content: [{ type: 'text', text }],
    source: { kind: SOURCE_KIND, form: 'notice', summary },
  })

  const warn = (message) => { ctx.logger?.warn?.(`bywood-writing-desk: ${message}`) }

  const resolveRef = async (ref) => {
    try {
      return (await ctx.credentials.resolve(ref))?.value
    } catch (error) {
      warn(`读不到 ${ref}：${error?.message ?? error}`)
      return undefined
    }
  }

  const dropServer = () => {
    if (unregisterServer === undefined) return
    try {
      unregisterServer()
    } catch (error) {
      warn(`摘掉 MCP 服务失败：${error?.message ?? error}`)
    }
    unregisterServer = undefined
    registeredBase = undefined
  }

  // 地址写死在注册里，钥匙走凭据模板：钥匙不进任何配置文件，换钥匙由 MCP 客户端自己重连。
  const ensureServer = (base) => {
    if (registeredBase === base) return
    dropServer()
    try {
      unregisterServer = ctx.mcpManager.register(SERVER_NAME, {
        transport: 'streamable-http',
        url: `${base}/mcp`,
        headers: { 'X-Deskcore-Key': `\${credential:${KEY_REF}}` },
      })
      registeredBase = base
    } catch (error) {
      status.lastError = `MCP 服务挂不上（${error?.message ?? error}）`
      warn(status.lastError)
    }
  }

  /** 读凭据、挂 MCP、取协议。并发调用时只有最后一次的结果生效。 */
  const reload = async () => {
    const mine = ++generation
    const [base, key] = await Promise.all([resolveRef(URL_REF), resolveRef(KEY_REF)])
    if (disposed || mine !== generation) return
    status.configured = Boolean(base && key)
    status.base = base
    if (!status.configured) {
      dropServer()
      status.protocol = undefined
      status.lastError = undefined
      return
    }
    ensureServer(base)
    const fetched = await fetchProtocol({ base, key, ...(fetchImpl ? { fetchImpl } : {}) })
    if (disposed || mine !== generation) return
    if (fetched.ok) {
      status.protocol = { version: fetched.version, protocol: fetched.protocol, header: fetched.header }
      status.lastError = undefined
      status.fetchedAt = Date.now()
    } else {
      // 取不到新的就留着上一次的：旧协议比没有协议强。
      status.lastError = fetched.reason
      warn(`取协议失败：${fetched.reason}`)
    }
  }

  const reloadQuietly = () => { void reload().catch(error => warn(`刷新失败：${error?.message ?? error}`)) }

  ctx.effect(() => {
    const timer = setInterval(reloadQuietly, refreshMs)
    timer.unref?.()
    return () => {
      disposed = true
      clearInterval(timer)
      dropServer()
    }
  }, 'bywood-writing-desk lifecycle')

  ctx.on('credentials/reference-updated', (ref) => {
    if (ref === URL_REF || ref === KEY_REF) reloadQuietly()
  })

  let order = 9000
  try {
    order = ctx.systemPrompt.getSectionOrder('MCP_SERVERS') + 1
  } catch { /* 用默认位置 */ }
  ctx.effect(() => ctx.systemPrompt.section({
    name: SECTION_NAME,
    order,
    text: () => buildSection(status),
    interpolate: false,
  }), 'bywood-writing-desk section')

  ctx.effect(() => ctx.commands.register({
    name: 'desk-setup',
    description: '配置写作台：/desk-setup 地址 钥匙',
    input: { hint: '<地址> <钥匙>' },
    // 钥匙不进会话日志。
    recordInput: false,
    handler: async ({ rawInput }) => {
      const parsed = parseSetup(rawInput)
      if (!parsed.ok) return { kind: 'error', text: parsed.reason }
      const probe = await fetchProtocol({ base: parsed.base, key: parsed.key, ...(fetchImpl ? { fetchImpl } : {}) })
      if (!probe.ok) return { kind: 'error', text: `没配上：${probe.reason}。检查地址和钥匙后再试一次，之前的配置没有动。` }
      await ctx.credentials.set(URL_REF, parsed.base)
      await ctx.credentials.set(KEY_REF, parsed.key)
      await reload()
      return { kind: 'success', text: '写作台配好了。新开一个对话就能用。' }
    },
  }), 'bywood-writing-desk /desk-setup')

  ctx.effect(() => ctx.commands.register({
    name: 'desk-status',
    description: '看写作台现在连没连上',
    handler: async () => {
      await reload()
      if (!status.configured) return { kind: 'success', text: '写作台还没配置。输入 /desk-setup 地址 钥匙。' }
      if (status.lastError) {
        const stale = status.protocol ? '（手上还有上一次取到的协议，能继续用）' : ''
        return { kind: 'error', text: `写作台没连上：${status.lastError}${stale}。` }
      }
      return { kind: 'success', text: `写作台连着。协议版本 ${status.protocol.version}。` }
    },
  }), 'bywood-writing-desk /desk-status')

  ctx.on('tools/pre-execute', async (exec, next) => {
    if (!exec.agent) return next()
    const decision = gate(stateOf(exec.agent.session), exec.name, exec.arguments)
    return decision.kind === 'deny' ? decision : next()
  })

  ctx.on('tools/result', (exec, result) => {
    if (exec.agent) observe(stateOf(exec.agent.session), exec.name, exec.arguments, result)
  })

  ctx.on('session/event', (session, event) => {
    if (event.type === 'turn/start') turnStarted(stateOf(session))
    else if (event.type === 'compaction/end' && !event.data?.error) compacted(stateOf(session))
  })

  ctx.on('agent/pre-step', async ({ agent }, next) => {
    const decision = await next()
    if (decision.kind !== 'enter') return decision
    const text = takeReopenNotice(stateOf(agent.session))
    if (text === undefined) return decision
    return { ...decision, messages: [...decision.messages, notice(text, '写作台：压缩后要重新开项目')] }
  })

  ctx.on('agent/turn-stopping', ({ agent }) => {
    const text = stopping(stateOf(agent.session))
    if (text !== undefined) agent.steer(notice(text, '写作台：这一轮的稿子还没存档'))
  })

  reloadQuietly()
  return { status, reload }
}
