import test from 'node:test'
import assert from 'node:assert/strict'
import {
  ARCHIVE_NUDGE, KEY_REF, REOPEN_NOTICE, SIGNPOST, TOOL_PREFIX, URL_REF,
  buildSection, compacted, gate, newState, normalizeBase, observe, parseSetup, stopping, takeReopenNotice, turnStarted,
} from '../lib/desk.js'

const t = name => `${TOOL_PREFIX}${name}`

test('地址收成基址：三种写法都认，尾巴去掉', () => {
  assert.deepEqual(normalizeBase('https://d.example.com'), { ok: true, base: 'https://d.example.com' })
  assert.deepEqual(normalizeBase('https://d.example.com/'), { ok: true, base: 'https://d.example.com' })
  assert.deepEqual(normalizeBase('https://d.example.com/mcp'), { ok: true, base: 'https://d.example.com' })
  assert.deepEqual(normalizeBase('https://d.example.com/mcp/'), { ok: true, base: 'https://d.example.com' })
  assert.deepEqual(normalizeBase('https://d.example.com/api/mcp'), { ok: true, base: 'https://d.example.com/api' })
  assert.deepEqual(normalizeBase('http://127.0.0.1:8000/mcp'), { ok: true, base: 'http://127.0.0.1:8000' })
  assert.equal(normalizeBase('http://d.example.com').ok, false)
  assert.equal(normalizeBase('d.example.com').ok, false)
  assert.equal(normalizeBase('https://d.example.com?x=1').ok, false)
})

test('解析 /desk-setup：两段、带不带引号都认；钥匙太短不认', () => {
  assert.deepEqual(parseSetup(' https://d.example.com/mcp  k-secret-123456 '), {
    ok: true, base: 'https://d.example.com', key: 'k-secret-123456',
  })
  assert.deepEqual(parseSetup('https://d.example.com "k-secret-123456"'), {
    ok: true, base: 'https://d.example.com', key: 'k-secret-123456',
  })
  // 粘进来带 Bearer 前缀的，收下并只留钥匙本身。
  assert.deepEqual(parseSetup('https://d.example.com Bearer k-secret-123456'), {
    ok: true, base: 'https://d.example.com', key: 'k-secret-123456',
  })
  assert.equal(parseSetup(' 只有一段').ok, false)
  assert.equal(parseSetup('https://d.example.com short').ok, false)
  assert.equal(parseSetup('https://d.example.com Bearer').ok, false)
})

test('系统提示三种状态：没配置 / 没连上 / 连着', () => {
  const notSetUp = buildSection({ configured: false }, false)
  assert.match(notSetUp, /还没配置/)
  assert.match(notSetUp, /\/desk-setup/)
  assert.match(notSetUp, /不要写稿/)

  const down = buildSection({ configured: true, lastError: '钥匙不对（服务端回了 401）' }, false)
  assert.match(down, /没连上/)
  assert.match(down, /钥匙不对/)
  assert.match(down, /不要写稿/)

  const connecting = buildSection({ configured: true }, false)
  assert.match(connecting, /没连上/)

  const up = buildSection({ configured: true, protocol: { version: 'v1', protocol: '# 协议正文', header: '头' } }, true)
  assert.match(up, /protocol_version: v1/)
  assert.match(up, /# 协议正文/)
  assert.match(up, new RegExp(TOOL_PREFIX + 'open_project'))
  // 服务端说明里的老说法（skill / 核版本 / WorkBuddy / mcp.json）在这一段里被覆盖。
  assert.match(up, /五点/)
  // 交付前逐字自查：错字和对不上的数字、标注，流程里没有别的环节查得出来。
  assert.match(up, /逐字读一遍/)
  assert.match(up, /数字、金额、时间前后要对得上/)
  assert.match(up, /不适用/)
  assert.match(up, /WorkBuddy/)
  assert.match(up, /\/desk-status/)

  // 工具还没挂上：协议取到了也不算连着。
  assert.match(buildSection({ configured: true, protocol: { version: 'v1', protocol: 'x', header: '' } }, false), /没连上/)
})

test('普通对话的指路：一句，指向写作台，明确不要直接写稿', () => {
  assert.match(SIGNPOST, /写作台/)
  assert.match(SIGNPOST, /查重/)
  assert.match(SIGNPOST, /不要直接写稿/)
})

test('拦截：没开项目就拦发牌 / 查重 / 存档，旧名字也拦', () => {
  const state = newState()
  for (const name of ['draw_angles', 'check_drafts', 'score_drafts', 'commit_drafts']) {
    const decision = gate(state, t(name), { project_id: 'p1' })
    assert.equal(decision.kind, 'deny', name)
    assert.match(decision.reason, new RegExp(TOOL_PREFIX + 'open_project'))
  }
  assert.equal(gate(state, t('list_projects'), {}).kind, 'allow')
  assert.equal(gate(state, 'bash', {}).kind, 'allow')

  observe(state, t('open_project'), { project_id: 'p1' }, { isError: false, value: { structuredContent: { project_name: '途鸽' } } })
  assert.equal(gate(state, t('check_drafts'), { project_id: 'p1' }).kind, 'allow')
  // 别的项目还没开过，照样拦。
  assert.equal(gate(state, t('check_drafts'), { project_id: 'p2' }).kind, 'deny')
})

test('记账：查过重欠着存档，收尾催一次，一轮只催一次', () => {
  const state = newState()
  observe(state, t('open_project'), { project_id: 'p1' }, { isError: false, value: { structuredContent: {} } })
  observe(state, t('check_drafts'), { project_id: 'p1' }, { isError: false, value: { structuredContent: {} } })
  assert.equal(stopping(state), ARCHIVE_NUDGE)
  assert.equal(stopping(state), undefined)
  turnStarted(state)
  assert.equal(stopping(state), undefined) // pendingArchive 已经清掉了

  observe(state, t('check_drafts'), { project_id: 'p1' }, { isError: false, value: { structuredContent: {} } })
  observe(state, t('commit_drafts'), { project_id: 'p1' }, { isError: false, value: { structuredContent: {} } })
  assert.equal(stopping(state), undefined)
})

test('压缩：要重新开项目；没开过项目的对话不提醒', () => {
  const opened = newState()
  observe(opened, t('open_project'), { project_id: 'p1' }, { isError: false, value: { structuredContent: {} } })
  compacted(opened)
  assert.equal(takeReopenNotice(opened), REOPEN_NOTICE)
  assert.equal(takeReopenNotice(opened), undefined)

  const idle = newState()
  compacted(idle)
  assert.equal(takeReopenNotice(idle), undefined)
})

test('工具结果里的错误不算开成功', () => {
  const state = newState()
  observe(state, t('open_project'), { project_id: 'p1' }, { isError: true, value: { structuredContent: { project_name: 'x' } } })
  assert.equal(gate(state, t('check_drafts'), { project_id: 'p1' }).kind, 'deny')
})

// ---- 下面几条是 0.1.0 就有的口径，页签化之后照样成立 ----
import { fetchProtocol as fetchProtocolFn, payloadOf as payloadOfFn, gate as gateFn, observe as observeFn, newState as newStateFn, stopping as stoppingFn, compacted as compactedFn, parseSetup as parseSetupFn, TOOL_PREFIX as PREFIX } from '../lib/desk.js'

const okResult = payload => ({ isError: false, value: { content: [{ type: 'text', text: JSON.stringify(payload) }], structuredContent: payload } })
const tool = name => `${PREFIX}${name}`

test('fetchProtocol 打 REST 通道、带钥匙头，认得出各种失败', async () => {
  const calls = []
  const reply = (status, body) => async (url, init) => {
    calls.push({ url, init })
    return { status, ok: status >= 200 && status < 300, json: async () => body }
  }
  const good = await fetchProtocolFn({
    base: 'https://d.example.com', key: 'k-1',
    fetchImpl: reply(200, { result: { version: 'abc123', protocol: '# 协议', skill_header: '头' } }),
  })
  assert.deepEqual(good, { ok: true, version: 'abc123', protocol: '# 协议', header: '头' })
  assert.equal(calls[0].url, 'https://d.example.com/tool/get_protocol')
  assert.equal(calls[0].init.method, 'POST')
  assert.equal(calls[0].init.headers['X-Deskcore-Key'], 'k-1')
  assert.equal(calls[0].init.body, '{}')

  assert.match((await fetchProtocolFn({ base: 'b', key: 'k', fetchImpl: reply(401, {}) })).reason, /钥匙不对/)
  assert.match((await fetchProtocolFn({ base: 'b', key: 'k', fetchImpl: reply(502, {}) })).reason, /502/)
  assert.equal((await fetchProtocolFn({ base: 'b', key: 'k', fetchImpl: reply(200, { result: { version: 'v' } }) })).ok, false)
  assert.equal((await fetchProtocolFn({ base: 'b', key: 'k', fetchImpl: reply(200, { result: { version: 'v', protocol: '  ' } }) })).ok, false)
  const boom = async () => { throw new Error('ECONNREFUSED') }
  assert.match((await fetchProtocolFn({ base: 'b', key: 'k', fetchImpl: boom })).reason, /连不上/)
  const badJson = async () => ({ status: 200, ok: true, json: async () => { throw new Error('x') } })
  assert.match((await fetchProtocolFn({ base: 'b', key: 'k', fetchImpl: badJson })).reason, /JSON/)
})

test('payloadOf 认 structuredContent 和文本两种形状，带 error 的不算成功', () => {
  assert.deepEqual(payloadOfFn(okResult({ a: 1 })), { a: 1 })
  assert.deepEqual(payloadOfFn({ isError: false, value: { content: [{ type: 'text', text: '{"a":2}' }] } }), { a: 2 })
  assert.deepEqual(payloadOfFn({ isError: false, value: { structuredContent: { result: { a: 3 } } } }), { a: 3 })
  assert.equal(payloadOfFn({ isError: true, error: 'x' }), undefined)
  assert.equal(payloadOfFn(okResult({ error: 'project not found' })), undefined)
  assert.equal(payloadOfFn({ isError: false, value: { content: [{ type: 'text', text: 'not json' }] } }), undefined)
  assert.equal(payloadOfFn(undefined), undefined)
})

test('查重之后存了档就不催，用旧名 commit_drafts 存的也算；压缩后旧名一样被拦', () => {
  const state = newStateFn()
  observeFn(state, tool('open_project'), { project_id: 'p1' }, okResult({}))
  observeFn(state, tool('check_drafts'), { project_id: 'p1' }, okResult({}))
  observeFn(state, tool('score_drafts'), { project_id: 'p1' }, okResult({ delivery: {} }))
  assert.equal(stoppingFn(state), undefined)

  observeFn(state, tool('check_drafts'), { project_id: 'p1' }, okResult({}))
  observeFn(state, tool('commit_drafts'), { project_id: 'p1' }, okResult({ delivery: {} }))
  assert.equal(stoppingFn(state), undefined)

  compactedFn(state)
  assert.equal(gateFn(state, tool('commit_drafts'), { project_id: 'p1' }).kind, 'deny')
  assert.equal(gateFn(state, tool('check_drafts'), { project_id: 'p2' }).kind, 'deny') // 另一个项目没开
})

test('/desk-setup 的「地址 Bearer 钥匙」三段写法也收；别的三段不收；怪空格不碍事', () => {
  assert.deepEqual(parseSetupFn(' https://d.example.com/mcp Bearer k-abc-12345678'), { ok: true, base: 'https://d.example.com', key: 'k-abc-12345678' })
  assert.deepEqual(parseSetupFn('\u3000https://d.example.com\u00a0`k-abc-12345678`'), { ok: true, base: 'https://d.example.com', key: 'k-abc-12345678' })
  assert.equal(parseSetupFn('https://d.example.com k-abc-12345678 多一段').ok, false)
  assert.equal(parseSetupFn('https://d.example.com Bearer').ok, false)
})
