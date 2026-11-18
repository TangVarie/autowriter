import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { GLOBAL_NAME, credentialsPath, installGlobal, parseRefs, readConnection } from '../lib/credfile.js'

// credentials-local（yaml 库）写出来的样子：顶格 version、refs，下面两格缩进。
const WRITTEN = `version: 1
refs:
  DEEPSEEK_API_KEY: sk-abc
  BYWOOD_DESK_URL: https://autowriter-production.up.railway.app
  BYWOOD_DESK_KEY: k-b-l7Ojmz9ZlEMPcB9X_FfGhhS9
records: {}
`

const fileOf = text => () => text

test('credentialsPath：优先用桌面端的 dshHomePath，否则 $DSH_HOME，再否则 ~/.dsh', () => {
  assert.equal(credentialsPath({ homePath: name => `/h/${name}` }), '/h/.credentials.yaml')
  assert.equal(credentialsPath({ env: { DSH_HOME: '/custom' }, home: () => '/Users/a' }), join('/custom', '.credentials.yaml'))
  assert.equal(credentialsPath({ env: {}, home: () => '/Users/a' }), join('/Users/a', '.dsh', '.credentials.yaml'))
  assert.equal(credentialsPath({ homePath: () => { throw new Error('x') }, env: {}, home: () => '/u' }), join('/u', '.dsh', '.credentials.yaml'))
})

test('parseRefs：只取 refs 下面那一层，认三种写法和注释', () => {
  const refs = parseRefs(`# comment
version: 1
refs:
  PLAIN: https://d.example.com # trailing
  SINGLE: 'k-it''s'
  DOUBLE: "k-\\"q\\""
    NESTED: ignored
  EMPTY:
records:
  NOT_A_REF: nope
`)
  assert.equal(refs.get('PLAIN'), 'https://d.example.com')
  assert.equal(refs.get('SINGLE'), "k-it's")
  assert.equal(refs.get('DOUBLE'), 'k-"q"')
  assert.equal(refs.get('EMPTY'), '')
  assert.equal(refs.has('NESTED'), false)
  assert.equal(refs.has('NOT_A_REF'), false)
  assert.equal(parseRefs('refs: {}\n').size, 0)
  assert.equal(parseRefs(undefined).size, 0)
  assert.equal(parseRefs('refs:\r\n  A: b\r\n').get('A'), 'b')
})

test('readConnection：两项都在才算配好，地址补上 /mcp', () => {
  assert.deepEqual(readConnection({ readFile: fileOf(WRITTEN) }), {
    configured: true, url: 'https://autowriter-production.up.railway.app/mcp', key: 'k-b-l7Ojmz9ZlEMPcB9X_FfGhhS9',
  })
  const missingKey = WRITTEN.replace(/\n  BYWOOD_DESK_KEY:.*\n/, '\n')
  assert.equal(readConnection({ readFile: fileOf(missingKey) }).configured, false)
  const badUrl = WRITTEN.replace('https://autowriter-production.up.railway.app', 'http://desk.example.com')
  assert.equal(readConnection({ readFile: fileOf(badUrl) }).configured, false)
  const missing = () => { throw Object.assign(new Error('ENOENT'), { code: 'ENOENT' }) }
  assert.deepEqual(readConnection({ readFile: missing }), { configured: false, url: '', key: '' })
  // 手工改过、带了 /mcp 的地址也认
  const withMcp = WRITTEN.replace('railway.app\n', 'railway.app/mcp\n')
  assert.equal(readConnection({ readFile: fileOf(withMcp) }).url, 'https://autowriter-production.up.railway.app/mcp')
})

test('installGlobal：挂上的对象每次现读文件；摘的时候只摘自己挂的', () => {
  let text = ''
  const uninstall = installGlobal({ readFile: () => text })
  const reader = globalThis[GLOBAL_NAME]
  assert.equal(reader.configured(), false)
  text = WRITTEN
  assert.equal(reader.configured(), true)
  assert.equal(reader.url(), 'https://autowriter-production.up.railway.app/mcp')
  assert.equal(reader.key(), 'k-b-l7Ojmz9ZlEMPcB9X_FfGhhS9')
  assert.throws(() => { reader.key = () => 'x' }, TypeError)

  const other = installGlobal({ readFile: () => '' })
  uninstall()
  assert.ok(globalThis[GLOBAL_NAME], '别人后挂的那个不能被前一个摘掉')
  other()
  assert.equal(globalThis[GLOBAL_NAME], undefined)
})

test('预设里那三个 !!js 表达式：照桌面端 Loader 的求值方式跑一遍', () => {
  // vendor/loader/src/config/utils.ts：new Function('ctx','expr','with (ctx) { return eval(expr) }')
  const evaluate = new Function('ctx', 'expr', 'with (ctx) { return eval(expr) }')
  const preset = readFileSync(new URL('../presets/writing-desk.patch.yml', import.meta.url), 'utf8')
  const block = preset.slice(preset.indexOf('- id: desk-mcp'), preset.indexOf('reconnect:', preset.indexOf('- id: desk-mcp')))
  const exprs = Object.fromEntries([...block.matchAll(/^\s*([A-Za-z-]+): !!js "(.*)"$/gm)].map(m => [m[1], m[2].replace(/\\"/g, '"')]))
  assert.deepEqual(Object.keys(exprs).sort(), ['X-Deskcore-Key', 'disabled', 'url'])
  assert.doesNotMatch(block, /\$\{credential:/, '0.2.1 的 MCP 客户端不认模板')
  assert.doesNotMatch(block, /inject: \[credentials\]/)

  const ctx = {}
  delete globalThis[GLOBAL_NAME]
  assert.equal(evaluate(ctx, exprs.disabled), true, '插件没加载：整行不加载')
  assert.equal(evaluate(ctx, exprs.url), '')

  let text = ''
  const uninstall = installGlobal({ readFile: () => text })
  assert.equal(evaluate(ctx, exprs.disabled), true, '没配：整行不加载')
  text = WRITTEN
  assert.equal(evaluate(ctx, exprs.disabled), false)
  assert.equal(evaluate(ctx, exprs.url), 'https://autowriter-production.up.railway.app/mcp')
  assert.equal(evaluate(ctx, exprs['X-Deskcore-Key']), 'k-b-l7Ojmz9ZlEMPcB9X_FfGhhS9')
  uninstall()
})

test('预设里不再列 0.2.1 没有的包；版本要求放到 0.2.1', () => {
  const preset = readFileSync(new URL('../presets/writing-desk.patch.yml', import.meta.url), 'utf8')
  assert.doesNotMatch(preset, /name: '@deepseek-ai\/dsh-tool-office'/)
  const manifest = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'))
  assert.equal(manifest.engines.dsh, '>=0.2.1-rc.1 <0.3.0')
})
