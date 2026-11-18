// 把 lib/desk.js 的逻辑接到 DeepSeek Harness 的扩展点上。
//
// 这个插件挂在 profile 根上，但**只对「写作台」预设开的对话起作用**：
//  - 系统提示那一段是按对话注册的（agent.ctx），普通对话里根本没有这一段；
//  - deskcore 的工具由预设里的 dsh-mcp-client 挂，只在预设的 scope 里，普通对话看不见；
//  - 拦发牌 / 查重 / 存档只看带 deskcore 前缀的工具，别的对话没有这些工具，所以拦不到；
//  - 压缩提醒、收尾催存档两类提醒只对开过项目的对话有状态，别的对话永远不触发。
//
// 不能和 0.1.0 同时启用：0.1.0 把 deskcore 注册成全局服务，下面「看得见 deskcore 的
// 工具就算写作台对话」那条兜底判据会把每一场对话都认成写作台对话。
//
// DSH 的东西（ctx）都从参数进来，这个文件自己不 import DSH 的包，
// 所以能用假的 ctx 做单元测试。真正的入口在 ../index.js。

import { randomUUID } from 'node:crypto'
import {
  KEY_REF, SIGNPOST, TOOL_PREFIX, URL_REF,
  buildSection, compacted, fetchProtocol, gate, newState, observe, parseSetup,
  stopping, takeReopenNotice, turnStarted,
} from './desk.js'
import { installGlobal } from './credfile.js'

const REFRESH_MS = 30 * 60 * 1000
// 预设里那一行 MCP 客户端是在预设激活时读地址和钥匙的（见 credfile.js），配完要重开一次才连得上。
const RESTART_AFTER_SETUP = '写作台配好了。请完全退出 DeepSeek Harness 再重新打开（Mac 按 ⌘Q，不是只关窗口），'
  + '然后点左侧栏的「写作台」开一个对话，输入 /desk-status 看一下是不是「写作台连着」。'
const SECTION_NAME = 'bywood-writing-desk'
const SIGNPOST_NAME = 'bywood-writing-desk-signpost'
const SOURCE_KIND = 'bywood-writing-desk'
/** 写作台页签开对话时选的智能体预设 id（见 presets/writing-desk.patch.yml）。 */
export const PRESET_ID = 'writing-desk'

/**
 * 一条挂在本对话里的提醒。等价于 `@deepseek-ai/dsh-llm` 的 createUserMessage：
 * 同一个形状（role / id / content / source），但不用 import DSH 的包——
 * 这样这个包没有任何运行时依赖，从 Git 地址装、本地链接装都不用管模块解析。
 */
function userMessage(text, summary) {
  const message = {
    role: 'user',
    id: randomUUID(),
    content: [{ type: 'text', text }],
    source: { kind: SOURCE_KIND, form: 'notice', summary },
  }
  return Object.freeze({ ...message, content: Object.freeze(message.content), source: Object.freeze(message.source) })
}

/**
 * @param {object} ctx - profile 根上的 Cordis 上下文。
 * @param {{ fetchImpl?: typeof fetch, refreshMs?: number, presetId?: string }} deps
 */
/** 桌面端在根上提供的 dshHomePath（0.2.1 和 0.2.2 都有）；拿不到就让 credfile 自己按 $DSH_HOME / ~/.dsh 算。 */
function dshHomePathOf(ctx) {
  try {
    return ctx.get?.('dshHomePath') ?? undefined
  } catch {
    return undefined
  }
}

export function install(ctx, { fetchImpl, refreshMs = REFRESH_MS, presetId = PRESET_ID } = {}) {
  const status = {
    configured: false,
    base: undefined,
    protocol: undefined,
    lastError: undefined,
    fetchedAt: undefined,
  }
  const states = new WeakMap()
  /**
   * 一个会话当前挂着那一段的那个 agent：sessionId -> { agent, dispose }。
   *
   * 按会话记而不是按 agent 记，是因为同一场对话会被 resume 成一个新 agent；
   * 按 sessionId 记住「是哪一位挂的」，换人时把旧的那一段摘掉再挂新的。
   * 插件卸载时一起摘。
   */
  const sections = new Map()
  let disposed = false
  let active
  let pending

  const stateOf = (session) => {
    let state = states.get(session)
    if (state === undefined) {
      state = newState()
      states.set(session, state)
    }
    return state
  }

  const warn = message => { ctx.logger?.warn?.(`bywood-writing-desk: ${message}`) }

  const resolveRef = async (ref) => {
    try {
      return (await ctx.credentials.resolve(ref))?.value
    } catch (error) {
      warn(`读不到 ${ref}：${error?.message ?? error}`)
      return undefined
    }
  }

  /**
   * 这个对话用的是哪个预设。读不到就返回 null。
   *
   * 0.2.2 有 presetForSession(session)；0.2.1（Intel Mac 上的桌面版停在这一版）没有，
   * 但有 composedPreset(agent.ctx)：「这个 agent 正在用哪个预设」。两个都试。
   */
  const presetOf = (agent) => {
    const presets = ctx.agentPresets
    try {
      const bySession = presets?.presetForSession?.(agent.session)
      if (bySession != null) return bySession
    } catch { /* 试下一个 */ }
    try {
      return presets?.composedPreset?.(agent.ctx) ?? null
    } catch {
      return null
    }
  }

  /**
   * 这个对话能不能看见 deskcore 的工具。
   *
   * 工具是预设里的 dsh-mcp-client 挂的，只在预设的 scope 里；用这个 agent 的
   * 视角去看才看得见。scoped 注册不会被 restrict 挡掉，所以这是「MCP 客户端
   * 连上了、工具同步完了」的现场判据。
   */
  const toolsVisibleTo = (agent) => {
    if (!agent) return false
    try {
      // 0.2.2 是 definitions(scope)，0.2.1 只有 schemas(scope)；两个都带 name。
      const tools = ctx.tools
      const definitions = tools?.definitions?.(agent) ?? tools?.schemas?.(agent) ?? []
      for (const definition of definitions) {
        if (typeof definition?.name === 'string' && definition.name.startsWith(TOOL_PREFIX)) return true
      }
    } catch (error) {
      warn(`看不到工具列表：${error?.message ?? error}`)
    }
    return false
  }

  /** 是不是写作台对话：预设对得上，或者（预设读不到时）这个对话真看得见 deskcore 的工具。 */
  const isDeskAgent = (agent) => presetOf(agent) === presetId || toolsVisibleTo(agent)

  const anyDeskAgent = () => {
    let agents = []
    try {
      agents = ctx.agents?.list?.() ?? []
    } catch {
      agents = []
    }
    for (const agent of agents) if (toolsVisibleTo(agent)) return true
    return false
  }

  /**
   * 给一个对话挂上它该有的那一段系统提示。
   *
   * 写作台对话挂协议；别的对话挂一句指路（10-19 那次「写手没走到写作台、模型直接
   * 开写」的口子）。两段都注册在 agent.ctx 上：只对这个 agent 生效，agent 销毁就
   * 没了。同一个会话的同一个 agent 只挂一次、同一种只挂一次；同一场对话被 resume
   * 成新 agent、或者中途换了预设，就把旧的摘掉再挂新的（同层同名的段落重复注册
   * 会打架）。
   */
  const attachSection = (agent) => {
    const sessionId = String(agent?.id ?? agent?.session?.id ?? '')
    if (!sessionId) return
    const desk = isDeskAgent(agent)
    const kind = desk ? 'desk' : 'signpost'
    const current = sections.get(sessionId)
    if (current && current.agent === agent && current.kind === kind) return
    if (current) {
      try {
        current.dispose?.()
      } catch { /* 老 agent 已经自己收掉了 */ }
      sections.delete(sessionId)
    }
    try {
      const dispose = agent.ctx.effect(() => agent.ctx.systemPrompt.section(desk
        ? {
          name: SECTION_NAME,
          order: sectionOrder(),
          // 系统提示里那句「连没连上」要现场看，不能是启动时的一张快照；
          // 看的是这个对话自己看不看得见工具，别的写作台对话连着不算数。
          text: () => buildSection(status, toolsVisibleTo(agent)),
          interpolate: false,
        }
        : {
          name: SIGNPOST_NAME,
          order: sectionOrder(),
          text: SIGNPOST,
          interpolate: false,
        }), kind === 'desk' ? 'bywood-writing-desk section' : 'bywood-writing-desk signpost')
      sections.set(sessionId, { agent, kind, dispose })
    } catch (error) {
      warn(`挂系统提示失败：${error?.message ?? error}`)
    }
  }

  const sectionOrder = () => {
    try {
      return ctx.systemPrompt.getSectionOrder('MCP_SERVERS') + 1
    } catch {
      return 9000
    }
  }

  /** 读凭据、取协议。一次跑完整的一遍，不会抛错。 */
  const run = async () => {
    const [base, key] = await Promise.all([resolveRef(URL_REF), resolveRef(KEY_REF)])
    if (disposed) return
    status.configured = Boolean(base && key)
    status.base = base
    if (!status.configured) {
      status.protocol = undefined
      status.lastError = undefined
      return
    }
    const fetched = await fetchProtocol({ base, key, ...(fetchImpl ? { fetchImpl } : {}) })
    if (disposed) return
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

  const start = () => {
    active = run()
      .catch(error => warn(`刷新失败：${error?.message ?? error}`))
      .finally(() => { active = undefined })
    return active
  }

  /**
   * 一次只跑一遍。正在跑的时候再叫，就排一遍在后面，后来的人都等这同一遍：
   * 它读到的一定是他们叫的时候已经存好的凭据，等它回来 status 也一定是跑完的样子。
   */
  const reload = () => {
    if (pending) return pending
    if (!active) return start()
    pending = active.then(() => {
      pending = undefined
      return start()
    })
    return pending
  }

  const reloadQuietly = () => { void reload() }

  /** 新对话开始前把协议准备好：第一轮就该看见它，不能等第二轮的刷新。 */
  const settled = async () => {
    try {
      if (active) await active
      if (!status.protocol && status.configured && !disposed) await start()
    } catch (error) {
      warn(`准备协议失败：${error?.message ?? error}`)
    }
  }

  // 给预设里那一行 MCP 客户端读地址和钥匙用（credfile.js）。插件卸载时摘掉。
  ctx.effect(() => installGlobal({ homePath: dshHomePathOf(ctx) }), 'bywood-writing-desk credential reader')

  ctx.effect(() => {
    const timer = setInterval(reloadQuietly, refreshMs)
    timer.unref?.()
    return () => {
      disposed = true
      clearInterval(timer)
      for (const entry of sections.values()) {
        try {
          entry.dispose?.()
        } catch { /* agent 已经自己收掉了 */ }
      }
      sections.clear()
    }
  }, 'bywood-writing-desk lifecycle')

  ctx.on('credentials/reference-updated', (ref) => {
    if (ref === URL_REF || ref === KEY_REF) reloadQuietly()
  })

  // 对话一建好就挂上它该有的那一段：写作台对话挂协议，别的对话挂一句指路。
  // 写作台对话还要把协议准备好，否则第一个模型步骤会显示「还在连接」。
  ctx.on('agent/created', async ({ agent }) => {
    attachSection(agent)
    if (isDeskAgent(agent) && status.configured && !status.protocol) await settled()
  })

  // agent 走了，它那份段落由 agent.ctx 自己收；这里把「谁挂着」记掉，
  // 免得同一场对话 resume 出来的新 agent 被当成「已经挂过」。
  ctx.on('agent/disposed', ({ agent }) => {
    const sessionId = String(agent?.id ?? agent?.session?.id ?? '')
    const current = sections.get(sessionId)
    if (!current || current.agent !== agent) return
    try {
      current.dispose?.()
    } catch { /* 已经收过了 */ }
    sections.delete(sessionId)
  })

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
      return { kind: 'success', text: RESTART_AFTER_SETUP }
    },
  }), 'bywood-writing-desk /desk-setup')

  ctx.effect(() => ctx.commands.register({
    name: 'desk-status',
    description: '看写作台现在连没连上',
    handler: async ({ agent } = {}) => {
      await reload()
      if (!status.configured) return { kind: 'success', text: '写作台还没配置。输入 /desk-setup 地址 钥匙 填上负责人给的那两样。' }
      if (status.lastError) {
        const stale = status.protocol ? '（手上还有上一次取到的协议，能继续用）' : ''
        return { kind: 'error', text: `写作台没连上：${status.lastError}${stale}。` }
      }
      if (!status.protocol) return { kind: 'error', text: '写作台还在连接，过几秒再输入一次 /desk-status。' }
      const version = status.protocol.version
      // 工具只挂在写作台对话里。在写作台对话里问，就看这个对话自己；
      // 在别的对话里问，没有写作台对话开着时工具本来就不在，不能说成「没连上」。
      if (agent && isDeskAgent(agent)) {
        if (toolsVisibleTo(agent)) return { kind: 'success', text: `写作台连着。协议版本 ${version}。` }
        return { kind: 'error', text: '写作台没连上：这个对话里写作台的工具还没挂上。刚配过地址和钥匙的话，要先完全退出 DeepSeek Harness 再打开（Mac 按 ⌘Q）；已经重开过就等几秒再输入一次 /desk-status，还不行找负责人。' }
      }
      if (anyDeskAgent()) return { kind: 'success', text: `写作台连着。协议版本 ${version}。` }
      return { kind: 'success', text: `地址和钥匙没问题，协议版本 ${version}。写作台的工具只挂在写作台对话里：点左侧栏的「写作台」开一个对话，在那里再输入一次 /desk-status 确认。` }
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
    // 从别处切到写作台预设（或切出去）的对话：下一个模型步骤起就换成对应的那一段。
    attachSection(agent)
    const decision = await next()
    if (decision.kind !== 'enter') return decision
    const text = takeReopenNotice(stateOf(agent.session))
    if (text === undefined) return decision
    return { ...decision, messages: [...decision.messages, userMessage(text, '写作台：压缩后要重新开项目')] }
  })

  ctx.on('agent/turn-stopping', ({ agent }) => {
    const text = stopping(stateOf(agent.session))
    if (text !== undefined) agent.steer(userMessage(text, '写作台：这一轮的稿子还没存档'))
  })

  reloadQuietly()
  return { status, reload, isDeskAgent }
}
