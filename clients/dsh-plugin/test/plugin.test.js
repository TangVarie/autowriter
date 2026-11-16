import test from 'node:test'
import assert from 'node:assert/strict'
import { install, PRESET_ID } from '../lib/plugin.js'
import { ARCHIVE_NUDGE, KEY_REF, REOPEN_NOTICE, TOOL_PREFIX, URL_REF } from '../lib/desk.js'

const tick = () => new Promise(resolve => setImmediate(resolve))
const settle = async () => { for (let i = 0; i < 6; i++) await tick() }
const ok = payload => ({ isError: false, value: { structuredContent: payload } })
const t = name => `${TOOL_PREFIX}${name}`

/**
 * 一个够用的假 ctx：
 *  - `record.sections` 记下按对话注册的系统提示段落；
 *  - `preset` 写在会话上，决定哪个对话是写作台对话；
 *  - `tools.definitions(agent)` 决定哪个 agent 看得见 deskcore 的工具；
 *  - `emit` 手动触发事件，async 的监听器会被等；
 *  - `record.steered` 记下 steer 出去的提醒。
 */
function fakeCtx({
  creds = {},
  protocol = { version: 'v1', protocol: '# 协议正文', skill_header: '头' },
  status = 200,
  gateFetch,
} = {}) {
  const store = new Map(Object.entries(creds))
  const listeners = new Map()
  const disposers = []
  const record = { sections: [], commands: new Map(), fetches: [], warnings: [], steered: [], disposals: 0 }
  const toolUsers = new Set()

  const ctx = {
    logger: { warn: message => record.warnings.push(message) },
    effect(callback) {
      const dispose = callback()
      disposers.push(dispose)
      return dispose
    },
    on(name, listener) {
      if (!listeners.has(name)) listeners.set(name, [])
      listeners.get(name).push(listener)
    },
    credentials: {
      resolve: async ref => (store.has(ref) ? { value: store.get(ref), source: 'file' } : undefined),
      set: async (ref, value) => {
        store.set(ref, value)
        for (const listener of listeners.get('credentials/reference-updated') ?? []) listener(ref)
      },
    },
    systemPrompt: {
      getSectionOrder: () => 500,
      // 根上注册的段落不算数：写作台只按对话注册。
      section() { throw new Error('global sections are not used') },
    },
    commands: {
      register(definition) { record.commands.set(definition.name, definition); return () => {} },
    },
    tools: {
      definitions(agent) {
        return toolUsers.has(agent) ? [{ name: t('list_projects') }, { name: t('open_project') }] : [{ name: 'bash' }]
      },
    },
    agents: { list: () => [...toolUsers] },
    agentPresets: { presetForSession: session => session?.preset ?? null },
  }

  const fakeAgent = (name, { preset = null, sessionId = name } = {}) => {
    const session = { id: sessionId, preset }
    return {
      id: name,
      session,
      steer: message => record.steered.push(message),
      ctx: {
        effect: callback => {
          const dispose = callback()
          const wrapped = () => { record.disposals += 1; return dispose?.() }
          disposers.push(wrapped)
          return wrapped
        },
        systemPrompt: {
          section(section) { record.sections.push({ session: sessionId, section }); return () => {} },
        },
      },
    }
  }

  const fetchImpl = async (url, init) => {
    record.fetches.push({ url, key: init.headers['X-Deskcore-Key'] })
    if (gateFetch) await gateFetch
    return { status, ok: status === 200, json: async () => ({ result: protocol }) }
  }

  const emit = async (name, ...args) => {
    const list = listeners.get(name) ?? []
    let last
    for (const listener of list) last = await listener(...args)
    return last
  }

  const dispose = () => { for (const d of disposers.reverse()) { try { d?.() } catch { /* ignore */ } } }

  return { ctx, record, store, emit, dispose, fakeAgent, toolUsers, deps: { fetchImpl, refreshMs: 3_600_000 } }
}

test('普通对话不挂写作台：只挂一句指路，拦的规则也不触发', async () => {
  const f = fakeCtx()
  install(f.ctx, f.deps)
  await settle()
  const plain = f.fakeAgent('s-plain')
  await f.emit('agent/created', { agent: plain })
  assert.equal(f.record.sections.length, 1)
  const section = f.record.sections[0].section
  assert.equal(section.name, 'bywood-writing-desk-signpost')
  assert.equal(section.interpolate, false)
  assert.match(section.text, /写作台/)
  assert.match(section.text, /这里不要直接写稿/)

  const decision = await f.emit('tools/pre-execute', { name: 'bash', arguments: {}, agent: plain }, async () => ({ kind: 'allow' }))
  assert.equal(decision.kind, 'allow')
  f.dispose()
})

test('中途从普通对话切到写作台预设：旧指路摘掉，换成协议', async () => {
  const f = fakeCtx({ creds: { [URL_REF]: 'https://d.example.com', [KEY_REF]: 'k-secret-123456' } })
  const agent = f.fakeAgent('s-switch')
  install(f.ctx, f.deps)
  await settle()
  await f.emit('agent/created', { agent })
  assert.equal(f.record.sections[0].section.name, 'bywood-writing-desk-signpost')

  // 切到写作台预设；工具这时也挂上了（真机上由预设里的 MCP 客户端带来）。
  f.toolUsers.add(agent)
  agent.session.preset = PRESET_ID
  await f.emit('agent/pre-step', { agent, messages: [] }, async () => ({ kind: 'enter', messages: [] }))
  assert.equal(f.record.sections.length, 2)
  assert.equal(f.record.sections[1].section.name, 'bywood-writing-desk')
  assert.equal(f.record.disposals, 1)
  assert.match(f.record.sections[1].section.text(), /protocol_version: v1/)
  f.dispose()
})

test('写作台对话：没配置时挂「还没配置」那一段，告诉模型不要写稿', async () => {
  const f = fakeCtx()
  install(f.ctx, f.deps)
  await settle()
  const desk = f.fakeAgent('s-desk', { preset: PRESET_ID })
  await f.emit('agent/created', { agent: desk })
  assert.equal(f.record.sections.length, 1)
  assert.equal(f.record.sections[0].session, 's-desk')
  const section = f.record.sections[0].section
  assert.equal(section.interpolate, false)
  assert.equal(section.order, 501)
  assert.match(section.text(), /还没配置/)
  assert.match(section.text(), /不要写稿/)
  f.dispose()
})

test('写作台对话 + 配好了：协议进那一段；工具没挂上就不说连着', async () => {
  const f = fakeCtx({ creds: { [URL_REF]: 'https://d.example.com', [KEY_REF]: 'k-secret-123456' } })
  install(f.ctx, f.deps)
  await settle()
  assert.deepEqual(f.record.fetches, [{ url: 'https://d.example.com/tool/get_protocol', key: 'k-secret-123456' }])

  const desk = f.fakeAgent('s-desk', { preset: PRESET_ID })
  await f.emit('agent/created', { agent: desk })
  // 这个 agent 还没看见 deskcore 的工具（MCP 客户端没连上）：协议取到了也不说连着。
  assert.match(f.record.sections[0].section.text(), /没连上/)
  f.dispose()
})

test('工具可见的写作台对话：文本是完整协议', async () => {
  const f = fakeCtx({ creds: { [URL_REF]: 'https://d.example.com', [KEY_REF]: 'k-secret-123456' } })
  const desk = f.fakeAgent('s-desk', { preset: PRESET_ID })
  f.toolUsers.add(desk)
  install(f.ctx, f.deps)
  await settle()
  await f.emit('agent/created', { agent: desk })
  const text = f.record.sections[0].section.text()
  assert.match(text, /protocol_version: v1/)
  assert.match(text, /# 协议正文/)
  assert.match(text, new RegExp(TOOL_PREFIX + 'open_project'))
  f.dispose()
})

test('预设读不到时，靠「看得见 deskcore 的工具」认出写作台对话', async () => {
  const f = fakeCtx({ creds: { [URL_REF]: 'https://d.example.com', [KEY_REF]: 'k-secret-123456' } })
  f.ctx.agentPresets.presetForSession = () => null
  const desk = f.fakeAgent('s-desk')
  f.toolUsers.add(desk)
  install(f.ctx, f.deps)
  await settle()
  await f.emit('agent/created', { agent: desk })
  assert.equal(f.record.sections.length, 1)
  assert.match(f.record.sections[0].section.text(), /protocol_version: v1/)
  f.dispose()
})

test('同一场对话 resume 成新 agent：旧的段落摘掉，新的挂上', async () => {
  const f = fakeCtx({ creds: { [URL_REF]: 'https://d.example.com', [KEY_REF]: 'k-secret-123456' } })
  const first = f.fakeAgent('a1', { preset: PRESET_ID, sessionId: 's-desk' })
  const second = f.fakeAgent('a2', { preset: PRESET_ID, sessionId: 's-desk' })
  f.toolUsers.add(first)
  f.toolUsers.add(second)
  install(f.ctx, f.deps)
  await settle()
  await f.emit('agent/created', { agent: first })
  assert.equal(f.record.sections.length, 1)
  await f.emit('agent/disposed', { agent: first })
  assert.equal(f.record.disposals, 1)
  await f.emit('agent/created', { agent: second })
  assert.equal(f.record.sections.length, 2)
  assert.equal(f.record.sections[1].session, 's-desk')
  assert.match(f.record.sections[1].section.text(), /protocol_version: v1/)
  f.dispose()
})

test('拦截、催存档、压缩提醒都按会话算', async () => {
  const f = fakeCtx({ creds: { [URL_REF]: 'https://d.example.com', [KEY_REF]: 'k-secret-123456' } })
  const desk = f.fakeAgent('s-desk', { preset: PRESET_ID })
  f.toolUsers.add(desk)
  install(f.ctx, f.deps)
  await settle()
  await f.emit('agent/created', { agent: desk })

  const allow = async () => ({ kind: 'allow' })
  const exec = (name, args) => ({ name, arguments: args, agent: desk })

  let reached = false
  const denied = await f.emit('tools/pre-execute', exec(t('check_drafts'), { project_id: 'p1' }), async () => {
    reached = true
    return { kind: 'allow' }
  })
  assert.equal(denied.kind, 'deny')
  assert.equal(reached, false)

  await f.emit('session/event', desk.session, { type: 'turn/start', data: { turn: 1 } })
  await f.emit('tools/result', exec(t('open_project'), { project_id: 'p1' }), ok({ project_name: '途鸽' }))
  assert.equal((await f.emit('tools/pre-execute', exec(t('check_drafts'), { project_id: 'p1' }), allow)).kind, 'allow')

  await f.emit('tools/result', exec(t('check_drafts'), { project_id: 'p1' }), ok({}))
  await f.emit('agent/turn-stopping', { agent: desk, turn: 1 })
  assert.equal(f.record.steered.length, 1)
  assert.equal(f.record.steered[0].content[0].text, ARCHIVE_NUDGE)
  assert.equal(f.record.steered[0].role, 'user')
  assert.equal(f.record.steered[0].source.form, 'notice')

  await f.emit('agent/turn-stopping', { agent: desk, turn: 1 })
  assert.equal(f.record.steered.length, 1)

  // 压缩 → 下一步补一句提醒，下游给的消息保留。
  await f.emit('session/event', desk.session, { type: 'compaction/end', data: {} })
  const entered = await f.emit('agent/pre-step', { agent: desk, messages: [] }, async () => ({
    kind: 'enter', messages: [{ id: 'u1' }], startsRequestSeries: true,
  }))
  assert.equal(entered.messages.length, 2)
  assert.equal(entered.messages[0].id, 'u1')
  assert.equal(entered.messages[1].content[0].text, REOPEN_NOTICE)
  assert.equal(entered.startsRequestSeries, true)
  assert.equal((await f.emit('tools/pre-execute', exec(t('check_drafts'), { project_id: 'p1' }), allow)).kind, 'deny')

  // 被拒的一步原样返回。
  assert.deepEqual(await f.emit('agent/pre-step', { agent: desk, messages: [] }, async () => ({ kind: 'reject' })), { kind: 'reject' })
  f.dispose()
})

test('/desk-setup 先验证再保存；/desk-status 没配置时照实说', async () => {
  const f = fakeCtx()
  install(f.ctx, f.deps)
  await settle()
  const setup = f.record.commands.get('desk-setup')
  assert.equal(setup.recordInput, false)

  const bad = await setup.handler({ rawInput: ' 只有一段' })
  assert.equal(bad.kind, 'error')
  assert.equal(f.store.size, 0)
  f.dispose()

  const none = fakeCtx()
  install(none.ctx, none.deps)
  await settle()
  assert.match((await none.record.commands.get('desk-status').handler({})).text, /还没配置/)
  none.dispose()
})

test('/desk-setup 验证不过时什么都不存', async () => {
  const f = fakeCtx({ status: 401 })
  install(f.ctx, f.deps)
  await settle()
  const done = await f.record.commands.get('desk-setup').handler({ rawInput: ' https://d.example.com k-wrong-123456' })
  assert.equal(done.kind, 'error')
  assert.match(done.text, /钥匙不对/)
  assert.equal(f.store.size, 0)
  f.dispose()
})

test('卸载插件把按对话注册的段落摘掉，不抛错', async () => {
  const f = fakeCtx({ creds: { [URL_REF]: 'https://d.example.com', [KEY_REF]: 'k-secret-123456' } })
  const desk = f.fakeAgent('s-desk', { preset: PRESET_ID })
  f.toolUsers.add(desk)
  install(f.ctx, f.deps)
  await settle()
  await f.emit('agent/created', { agent: desk })
  assert.equal(f.record.sections.length, 1)
  f.dispose()
  assert.ok(true)
})

const CREDS = { [URL_REF]: 'https://d.example.com', [KEY_REF]: 'k-secret-123456' }

test('/desk-status 在写作台对话里：看这个对话自己有没有工具', async () => {
  const f = fakeCtx({ creds: CREDS })
  install(f.ctx, f.deps)
  await settle()
  const status = f.record.commands.get('desk-status')
  const desk = f.fakeAgent('d1', { preset: PRESET_ID })

  const waiting = await status.handler({ agent: desk })
  assert.equal(waiting.kind, 'error')
  assert.match(waiting.text, /工具还没挂上/)

  f.toolUsers.add(desk)
  const up = await status.handler({ agent: desk })
  assert.equal(up.kind, 'success')
  assert.match(up.text, /写作台连着。协议版本 v1/)
  f.dispose()
})

test('/desk-status 在普通对话里：没有写作台对话开着不算没连上，说去写作台里确认', async () => {
  const f = fakeCtx({ creds: CREDS })
  install(f.ctx, f.deps)
  await settle()
  const status = f.record.commands.get('desk-status')
  const plain = f.fakeAgent('p1')

  const alone = await status.handler({ agent: plain })
  assert.equal(alone.kind, 'success')
  assert.match(alone.text, /地址和钥匙没问题，协议版本 v1/)
  assert.match(alone.text, /左侧栏的「写作台」/)
  assert.doesNotMatch(alone.text, /没连上/)

  f.toolUsers.add(f.fakeAgent('d1', { preset: PRESET_ID }))
  assert.match((await status.handler({ agent: plain })).text, /写作台连着/)
  f.dispose()
})

test('/desk-status：钥匙不对照实报错；取协议还没回来时连问两次都等到结果', async () => {
  const bad = fakeCtx({ creds: CREDS, status: 401 })
  install(bad.ctx, bad.deps)
  await settle()
  const denied = await bad.record.commands.get('desk-status').handler({ agent: bad.fakeAgent('p1') })
  assert.equal(denied.kind, 'error')
  assert.match(denied.text, /钥匙不对/)
  bad.dispose()

  let release
  const gateFetch = new Promise((resolve) => { release = resolve })
  const f = fakeCtx({ creds: CREDS, gateFetch })
  install(f.ctx, f.deps)
  await tick()
  const status = f.record.commands.get('desk-status')
  const plain = f.fakeAgent('p1')
  const first = status.handler({ agent: plain })
  const second = status.handler({ agent: plain })
  await tick()
  release()
  for (const result of await Promise.all([first, second])) {
    assert.equal(result.kind, 'success')
    assert.match(result.text, /协议版本 v1/)
  }
  assert.equal(f.record.fetches.length, 2) // 开机那一遍 + 两次询问合成的一遍
  f.dispose()
})

test('写作台对话的那一段只看这个对话自己的工具，别的写作台对话连着不算', async () => {
  const f = fakeCtx({ creds: CREDS })
  install(f.ctx, f.deps)
  await settle()
  const other = f.fakeAgent('d-other', { preset: PRESET_ID })
  f.toolUsers.add(other)
  const mine = f.fakeAgent('d-mine', { preset: PRESET_ID })
  await f.emit('agent/created', { agent: mine })
  const section = f.record.sections.find(entry => entry.session === 'd-mine').section
  assert.match(section.text(), /不要写稿/)
  f.toolUsers.add(mine)
  assert.match(section.text(), /protocol_version: v1/)
  f.dispose()
})
