import test from 'node:test'
import assert from 'node:assert/strict'
import {
  ARCHIVE_NUDGE, REOPEN_NOTICE, TOOL_PREFIX,
  buildSection, compacted, fetchProtocol, gate, newState, normalizeBase, observe,
  parseSetup, payloadOf, stopping, takeReopenNotice, turnStarted,
} from '../lib/desk.js'

const ok = payload => ({ isError: false, value: { content: [{ type: 'text', text: JSON.stringify(payload) }], structuredContent: payload } })
const t = name => `${TOOL_PREFIX}${name}`

test('normalizeBase 认带不带 /mcp 的地址，拒绝不安全的', () => {
  assert.deepEqual(normalizeBase('https://desk.example.com/mcp'), { ok: true, base: 'https://desk.example.com' })
  assert.deepEqual(normalizeBase(' https://desk.example.com/mcp/ '), { ok: true, base: 'https://desk.example.com' })
  assert.deepEqual(normalizeBase('https://desk.example.com/'), { ok: true, base: 'https://desk.example.com' })
  assert.deepEqual(normalizeBase('https://x.example.com/desk/mcp'), { ok: true, base: 'https://x.example.com/desk' })
  assert.deepEqual(normalizeBase('http://localhost:8000/mcp'), { ok: true, base: 'http://localhost:8000' })
  assert.equal(normalizeBase('http://desk.example.com/mcp').ok, false)
  assert.equal(normalizeBase('desk.example.com').ok, false)
  assert.equal(normalizeBase('https://desk.example.com/mcp?key=k-1').ok, false)
  assert.equal(normalizeBase('https://u:p@desk.example.com/mcp').ok, false)
})

test('parseSetup 取出地址和钥匙，去掉粘贴带进来的引号和怪空格', () => {
  assert.deepEqual(parseSetup(' https://d.example.com/mcp k-abc-12345678 '), { ok: true, base: 'https://d.example.com', key: 'k-abc-12345678' })
  assert.deepEqual(parseSetup('　https://d.example.com “k-abc-12345678”'), { ok: true, base: 'https://d.example.com', key: 'k-abc-12345678' })
  assert.equal(parseSetup('https://d.example.com').ok, false)
  assert.equal(parseSetup('https://d.example.com Bearer k-abc-12345678').ok, false)
  assert.equal(parseSetup('https://d.example.com short').ok, false)
  assert.equal(parseSetup('').ok, false)
})

test('fetchProtocol 打 REST 通道、带钥匙头，认得出各种失败', async () => {
  const calls = []
  const reply = (status, body) => async (url, init) => {
    calls.push({ url, init })
    return { status, ok: status >= 200 && status < 300, json: async () => body }
  }
  const good = await fetchProtocol({
    base: 'https://d.example.com', key: 'k-1',
    fetchImpl: reply(200, { result: { version: 'abc123', protocol: '# 协议', skill_header: '头' } }),
  })
  assert.deepEqual(good, { ok: true, version: 'abc123', protocol: '# 协议', header: '头' })
  assert.equal(calls[0].url, 'https://d.example.com/tool/get_protocol')
  assert.equal(calls[0].init.method, 'POST')
  assert.equal(calls[0].init.headers['X-Deskcore-Key'], 'k-1')
  assert.equal(calls[0].init.body, '{}')

  assert.match((await fetchProtocol({ base: 'b', key: 'k', fetchImpl: reply(401, {}) })).reason, /钥匙不对/)
  assert.match((await fetchProtocol({ base: 'b', key: 'k', fetchImpl: reply(502, {}) })).reason, /502/)
  assert.equal((await fetchProtocol({ base: 'b', key: 'k', fetchImpl: reply(200, { result: { version: 'v' } }) })).ok, false)
  assert.equal((await fetchProtocol({ base: 'b', key: 'k', fetchImpl: reply(200, { result: { version: 'v', protocol: '  ' } }) })).ok, false)
  const boom = async () => { throw new Error('ECONNREFUSED') }
  assert.match((await fetchProtocol({ base: 'b', key: 'k', fetchImpl: boom })).reason, /连不上/)
  const badJson = async () => ({ status: 200, ok: true, json: async () => { throw new Error('x') } })
  assert.match((await fetchProtocol({ base: 'b', key: 'k', fetchImpl: badJson })).reason, /JSON/)
})

test('buildSection 三种状态：没配置、没连上、连着', () => {
  const none = buildSection({ configured: false })
  assert.match(none, /不要写稿/)
  assert.match(none, /\/desk-setup/)

  const down = buildSection({ configured: true, lastError: '连接超时' })
  assert.match(down, /不要写稿/)
  assert.match(down, /连接超时/)
  assert.match(down, /\/desk-status/)

  const up = buildSection({ configured: true, protocol: { version: '6f23eba491fd', protocol: '# 写作台协议\n正文', header: '四条规矩' } })
  assert.match(up, /protocol_version: 6f23eba491fd/)
  assert.match(up, /四条规矩/)
  assert.match(up, /# 写作台协议\n正文/)
  assert.ok(up.includes(`${TOOL_PREFIX}open_project`))
  assert.doesNotMatch(up, /不要写稿/)

  // 取新协议失败但手上有旧的：继续用旧的，不让模型停笔。
  const stale = buildSection({ configured: true, lastError: '连接超时', protocol: { version: 'v1', protocol: '正文', header: '' } })
  assert.match(stale, /protocol_version: v1/)
})

test('payloadOf 认 structuredContent 和文本两种形状，带 error 的不算成功', () => {
  assert.deepEqual(payloadOf(ok({ a: 1 })), { a: 1 })
  assert.deepEqual(payloadOf({ isError: false, value: { content: [{ type: 'text', text: '{"a":2}' }] } }), { a: 2 })
  assert.deepEqual(payloadOf({ isError: false, value: { structuredContent: { result: { a: 3 } } } }), { a: 3 })
  assert.equal(payloadOf({ isError: true, error: 'x' }), undefined)
  assert.equal(payloadOf(ok({ error: 'project not found' })), undefined)
  assert.equal(payloadOf({ isError: false, value: { content: [{ type: 'text', text: 'not json' }] } }), undefined)
  assert.equal(payloadOf(undefined), undefined)
})

test('没开项目：发牌、查重、存档被拦；开了就放行；别的工具不管', () => {
  const state = newState()
  for (const tool of ['draw_angles', 'check_drafts', 'score_drafts']) {
    const decision = gate(state, t(tool), { project_id: 'p1' })
    assert.equal(decision.kind, 'deny')
    assert.ok(decision.reason.includes(t('open_project')))
  }
  assert.equal(gate(state, t('list_projects'), {}).kind, 'allow')
  assert.equal(gate(state, t('open_project'), { project_id: 'p1' }).kind, 'allow')
  assert.equal(gate(state, t('comments_for'), { link: 'x' }).kind, 'allow')
  assert.equal(gate(state, 'bash', { command: 'ls' }).kind, 'allow')
  assert.equal(gate(state, 'check_drafts', { project_id: 'p1' }).kind, 'allow') // 不带前缀的不是写作台的工具

  observe(state, t('open_project'), { project_id: 'p1' }, ok({ project_name: '途鸽' }))
  assert.equal(gate(state, t('check_drafts'), { project_id: 'p1' }).kind, 'allow')
  assert.equal(gate(state, t('check_drafts'), { project_id: 'p2' }).kind, 'deny') // 另一个项目没开
  assert.equal(gate(state, t('check_drafts'), {}).kind, 'deny')
})

test('open_project 失败不算开过', () => {
  const state = newState()
  observe(state, t('open_project'), { project_id: 'p1' }, { isError: true, error: '403' })
  observe(state, t('open_project'), { project_id: 'p1' }, ok({ error: 'project not found' }))
  assert.equal(gate(state, t('draw_angles'), { project_id: 'p1' }).kind, 'deny')
})

test('查过重没存档：收尾时催一次，一轮只催一次', () => {
  const state = newState()
  assert.equal(stopping(state), undefined) // 没查过重不催

  observe(state, t('open_project'), { project_id: 'p1' }, ok({}))
  observe(state, t('check_drafts'), { project_id: 'p1' }, ok({ summary: {} }))
  assert.equal(stopping(state), ARCHIVE_NUDGE)
  assert.equal(stopping(state), undefined)

  // 下一轮又查重又没存：再催一次。
  turnStarted(state)
  observe(state, t('check_drafts'), { project_id: 'p1' }, ok({}))
  assert.equal(stopping(state), ARCHIVE_NUDGE)
})

test('查重之后存了档就不催；查重失败也不催', () => {
  const state = newState()
  observe(state, t('check_drafts'), { project_id: 'p1' }, ok({}))
  observe(state, t('score_drafts'), { project_id: 'p1' }, ok({ delivery: {} }))
  assert.equal(stopping(state), undefined)

  const failed = newState()
  observe(failed, t('check_drafts'), { project_id: 'p1' }, { isError: true, error: 'boom' })
  assert.equal(stopping(failed), undefined)
})

test('压缩之后：要重新开项目，并补一句提醒，提醒只发一次', () => {
  const state = newState()
  compacted(state)
  assert.equal(takeReopenNotice(state), undefined) // 没开过项目的对话，压缩了也不提醒

  observe(state, t('open_project'), { project_id: 'p1' }, ok({}))
  compacted(state)
  assert.equal(gate(state, t('score_drafts'), { project_id: 'p1' }).kind, 'deny')
  assert.equal(takeReopenNotice(state), REOPEN_NOTICE)
  assert.equal(takeReopenNotice(state), undefined)

  compacted(state)
  observe(state, t('open_project'), { project_id: 'p1' }, ok({}))
  assert.equal(takeReopenNotice(state), undefined) // 模型自己先重开了，就不用再提醒
  assert.equal(gate(state, t('score_drafts'), { project_id: 'p1' }).kind, 'allow')
})
