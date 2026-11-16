import test from 'node:test'
import assert from 'node:assert/strict'
import { install } from '../lib/plugin.js'
import { ARCHIVE_NUDGE, KEY_REF, REOPEN_NOTICE, TOOL_PREFIX, URL_REF } from '../lib/desk.js'

const tick = () => new Promise(resolve => setImmediate(resolve))
const settle = async () => { for (let i = 0; i < 5; i++) await tick() }
const ok = payload => ({ isError: false, value: { structuredContent: payload } })
const t = name => `${TOOL_PREFIX}${name}`

/** 一个够用的假 ctx：记下插件注册了什么，能手动触发事件。 */
function fakeCtx({ creds = {}, protocol = { version: 'v1', protocol: '# 协议正文', skill_header: '头' }, status = 200 } = {}) {
  const store = new Map(Object.entries(creds))
  const listeners = new Map()
  const disposers = []
  const record = { servers: [], unregistered: 0, sections: [], commands: new Map(), fetches: [], warnings: [] }
  const ctx = {
    logger: { warn: message => record.warnings.push(message) },
    effect(callback) {
      const dispose = callback()
      disposers.push(dispose)
      return dispose
    },
    on(name, listener) { listeners.set(name, listener) },
    credentials: {
      resolve: async ref => (store.has(ref) ? { value: store.get(ref), source: 'file' } : undefined),
      set: async (ref, value) => { store.set(ref, value); listeners.get('credentials/reference-updated')?.(ref) },
    },
    mcpManager: {
      register(name, entry) {
        record.servers.push({ name, entry })
        return () => { record.unregistered += 1 }
      },
    },
    systemPrompt: {
      getSectionOrder: () => 500,
      section(section) { record.sections.push(section); return () => {} },
    },
    commands: {
      register(definition) { record.commands.set(definition.name, definition); return () => {} },
    },
  }
  const fetchImpl = async (url, init) => {
    record.fetches.push({ url, key: init.headers['X-Deskcore-Key'] })
    return { status, ok: status === 200, json: async () => ({ result: protocol }) }
  }
  const createUserMessage = input => ({ ...input, role: 'user', id: 'm' })
  const emit = (name, ...args) => listeners.get(name)(...args)
  const dispose = () => { for (const d of disposers.reverse()) d?.() }
  return { ctx, record, store, emit, dispose, deps: { createUserMessage, fetchImpl, refreshMs: 3_600_000 } }
}

const sectionText = record => record.sections[0].text()

test('没配置：不挂 MCP，系统提示说先配置', async () => {
  const f = fakeCtx()
  install(f.ctx, f.deps)
  await settle()
  assert.equal(f.record.servers.length, 0)
  assert.match(sectionText(f.record), /还没配置/)
  assert.equal(f.record.sections[0].interpolate, false)
  assert.equal(f.record.sections[0].order, 501)
  f.dispose()
})

test('配置过：挂上 MCP（钥匙走凭据模板，不落进配置），协议进系统提示', async () => {
  const f = fakeCtx({ creds: { [URL_REF]: 'https://d.example.com', [KEY_REF]: 'k-secret-123456' } })
  install(f.ctx, f.deps)
  await settle()
  assert.deepEqual(f.record.servers, [{
    name: 'deskcore',
    entry: {
      transport: 'streamable-http',
      url: 'https://d.example.com/mcp',
      headers: { 'X-Deskcore-Key': '${credential:BYWOOD_DESK_KEY}' },
    },
  }])
  assert.ok(!JSON.stringify(f.record.servers).includes('k-secret-123456'))
  assert.deepEqual(f.record.fetches, [{ url: 'https://d.example.com/tool/get_protocol', key: 'k-secret-123456' }])
  const text = sectionText(f.record)
  assert.match(text, /protocol_version: v1/)
  assert.match(text, /# 协议正文/)
  f.dispose()
  assert.equal(f.record.unregistered, 1) // 卸载插件时把 MCP 服务摘掉
})

test('配置过但服务端不通：系统提示说没连上、不写稿', async () => {
  const f = fakeCtx({ creds: { [URL_REF]: 'https://d.example.com', [KEY_REF]: 'k-secret-123456' }, status: 401 })
  install(f.ctx, f.deps)
  await settle()
  const text = sectionText(f.record)
  assert.match(text, /没连上/)
  assert.match(text, /钥匙不对/)
  assert.match(text, /不要写稿/)
  f.dispose()
})

test('/desk-setup：先验证再保存；验证不过不动原配置；输入不进日志', async () => {
  const f = fakeCtx()
  install(f.ctx, f.deps)
  await settle()
  const setup = f.record.commands.get('desk-setup')
  assert.equal(setup.recordInput, false)

  const bad = await setup.handler({ rawInput: ' 只有一段' })
  assert.equal(bad.kind, 'error')
  assert.equal(f.store.size, 0)

  const done = await setup.handler({ rawInput: ' https://d.example.com/mcp k-secret-123456' })
  assert.equal(done.kind, 'success')
  assert.equal(f.store.get(URL_REF), 'https://d.example.com')
  assert.equal(f.store.get(KEY_REF), 'k-secret-123456')
  await settle()
  assert.equal(f.record.servers.length, 1) // 两个凭据先后更新，只挂一次
  assert.match(sectionText(f.record), /protocol_version: v1/)
  f.dispose()
})

test('/desk-setup：钥匙不对时报错，什么都不存', async () => {
  const f = fakeCtx({ status: 401 })
  install(f.ctx, f.deps)
  await settle()
  const result = await f.record.commands.get('desk-setup').handler({ rawInput: ' https://d.example.com k-wrong-123456' })
  assert.equal(result.kind, 'error')
  assert.match(result.text, /钥匙不对/)
  assert.equal(f.store.size, 0)
  assert.equal(f.record.servers.length, 0)
  f.dispose()
})

test('/desk-status 三种回答', async () => {
  const none = fakeCtx()
  install(none.ctx, none.deps)
  assert.match((await none.record.commands.get('desk-status').handler({})).text, /还没配置/)
  none.dispose()

  const up = fakeCtx({ creds: { [URL_REF]: 'https://d.example.com', [KEY_REF]: 'k-secret-123456' } })
  install(up.ctx, up.deps)
  assert.match((await up.record.commands.get('desk-status').handler({})).text, /连着.*v1/)
  up.dispose()

  const down = fakeCtx({ creds: { [URL_REF]: 'https://d.example.com', [KEY_REF]: 'k-secret-123456' }, status: 500 })
  install(down.ctx, down.deps)
  const result = await down.record.commands.get('desk-status').handler({})
  assert.equal(result.kind, 'error')
  assert.match(result.text, /500/)
  down.dispose()
})

test('事件接线：拦截、放行、催存档、压缩后提醒', async () => {
  const f = fakeCtx({ creds: { [URL_REF]: 'https://d.example.com', [KEY_REF]: 'k-secret-123456' } })
  install(f.ctx, f.deps)
  await settle()
  const session = {}
  const steered = []
  const agent = { session, steer: message => steered.push(message) }
  const allow = async () => ({ kind: 'allow' })
  const exec = (name, args) => ({ name, arguments: args, agent })

  // 没开项目：查重被拦，下游不会被调用。
  let reached = false
  const denied = await f.emit('tools/pre-execute', exec(t('check_drafts'), { project_id: 'p1' }), async () => { reached = true; return { kind: 'allow' } })
  assert.equal(denied.kind, 'deny')
  assert.equal(reached, false)

  // 没有 agent 的直接调用、别的工具：原样交给下游。
  assert.equal((await f.emit('tools/pre-execute', { name: t('check_drafts'), arguments: {} }, allow)).kind, 'allow')
  assert.equal((await f.emit('tools/pre-execute', exec('bash', {}), allow)).kind, 'allow')

  // 开项目 → 查重放行 → 收尾时催存档。
  f.emit('session/event', session, { type: 'turn/start', data: { turn: 1 } })
  f.emit('tools/result', exec(t('open_project'), { project_id: 'p1' }), ok({ project_name: '途鸽' }))
  assert.equal((await f.emit('tools/pre-execute', exec(t('check_drafts'), { project_id: 'p1' }), allow)).kind, 'allow')
  f.emit('tools/result', exec(t('check_drafts'), { project_id: 'p1' }), ok({}))
  f.emit('agent/turn-stopping', { agent, turn: 1 })
  assert.equal(steered.length, 1)
  assert.equal(steered[0].content[0].text, ARCHIVE_NUDGE)
  assert.deepEqual(steered[0].source, { kind: 'bywood-writing-desk', form: 'notice', summary: '写作台：这一轮的稿子还没存档' })
  f.emit('agent/turn-stopping', { agent, turn: 1 })
  assert.equal(steered.length, 1)

  // 压缩 → 下一步补一句提醒，保留下游给的消息；查重又被拦。
  f.emit('session/event', session, { type: 'compaction/end', data: {} })
  const entered = await f.emit('agent/pre-step', { agent, messages: [] }, async () => ({ kind: 'enter', messages: [{ id: 'u1' }], startsRequestSeries: true }))
  assert.equal(entered.messages.length, 2)
  assert.equal(entered.messages[0].id, 'u1')
  assert.equal(entered.messages[1].content[0].text, REOPEN_NOTICE)
  assert.equal(entered.startsRequestSeries, true)
  assert.equal((await f.emit('tools/pre-execute', exec(t('check_drafts'), { project_id: 'p1' }), allow)).kind, 'deny')

  // 压缩失败的不算；被拒的一步原样返回。
  const other = { session: {}, steer() {} }
  f.emit('tools/result', { name: t('open_project'), arguments: { project_id: 'p9' }, agent: other }, ok({}))
  f.emit('session/event', other.session, { type: 'compaction/end', data: { error: 'no model' } })
  assert.equal((await f.emit('tools/pre-execute', { name: t('draw_angles'), arguments: { project_id: 'p9' }, agent: other }, allow)).kind, 'allow')
  assert.deepEqual(await f.emit('agent/pre-step', { agent, messages: [] }, async () => ({ kind: 'reject' })), { kind: 'reject' })
  f.dispose()
})
