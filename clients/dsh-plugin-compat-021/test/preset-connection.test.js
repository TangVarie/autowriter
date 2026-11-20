// 预设里连写作台的那一行（desk-mcp）：地址和钥匙怎么来，以及为什么「第一次读到什么就一直是什么」。
//
// 照 DSH 0.2.1 Loader 的求值方式跑预设里的三个 !!js 表达式
// （vendor/loader/src/config/utils.ts：new Function('ctx','expr','with (ctx) { return eval(expr) }')）。
import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync, writeFileSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const evaluate = new Function('ctx', 'expr', 'with (ctx) { return eval(expr) }')
const SNAPSHOT = '__bywoodWritingDeskConnection'
const PRESET = readFileSync(new URL('../presets/writing-desk.patch.yml', import.meta.url), 'utf8')

/** desk-mcp 那一行：从 `- id: desk-mcp` 到下一行 `- id:`。 */
function deskRow() {
  const start = PRESET.indexOf('          - id: desk-mcp')
  const end = PRESET.indexOf('\n          - id: ', start + 1)
  return PRESET.slice(start, end)
}

/** 取出三个表达式：disabled 是 `!!js |-` 块，url 和钥匙是一行带引号的。 */
function expressions() {
  const lines = deskRow().split('\n')
  const at = lines.findIndex(line => line === '            disabled: !!js |-')
  assert.ok(at > 0, 'disabled 要写成 !!js |- 块')
  const body = []
  for (const line of lines.slice(at + 1)) {
    if (line.trim() && !line.startsWith('              ')) break
    body.push(line.slice(14))
  }
  const quoted = Object.fromEntries([...deskRow().matchAll(/^\s*([A-Za-z-]+): !!js "(.*)"$/gm)]
    .map(m => [m[1], m[2].replace(/\\"/g, '"')]))
  assert.deepEqual(Object.keys(quoted).sort(), ['X-Deskcore-Key', 'url'])
  return { disabled: body.join('\n').trimEnd(), url: quoted.url, key: quoted['X-Deskcore-Key'] }
}

const EXPR = expressions()

// credentials-local（yaml 库）写出来的样子：顶格 version、refs，下面两格缩进。
const WRITTEN = `version: 1
refs:
  DEEPSEEK_API_KEY: sk-abc
  BYWOOD_DESK_URL: https://autowriter-production.up.railway.app
  BYWOOD_DESK_KEY: k-test-not-a-real-key-000
records: {}
`

/** 一个临时的 DSH 主目录；text 为 undefined 时不写凭据文件。 */
function home(text) {
  const dir = mkdtempSync(join(tmpdir(), 'desk-home-'))
  if (text !== undefined) writeFileSync(join(dir, '.credentials.yaml'), text)
  return dir
}

/** 像桌面端那样给 ctx：dshHomePath 是根上提供的函数，按 ctx.get 拿。 */
function ctxFor(dir) {
  return { get: name => (name === 'dshHomePath' ? (...segments) => join(dir, ...segments) : undefined) }
}

/** 一次「桌面端进程」：清掉上一个进程留下的快照，再按 Loader 的方式求值。 */
function boot(ctx) {
  delete globalThis[SNAPSHOT]
  return {
    disabled: () => evaluate(ctx, EXPR.disabled),
    url: () => evaluate(ctx, EXPR.url),
    key: () => evaluate(ctx, EXPR.key),
  }
}

test.afterEach(() => { delete globalThis[SNAPSHOT] })

test('配好了：整行加载，地址补上 /mcp，钥匙原样', () => {
  const dir = home(WRITTEN)
  const run = boot(ctxFor(dir))
  assert.equal(run.disabled(), false)
  assert.equal(run.url(), 'https://autowriter-production.up.railway.app/mcp')
  assert.equal(run.key(), 'k-test-not-a-real-key-000')
  rmSync(dir, { recursive: true })
})

test('没配、配了一半、地址不对、文件读不了：都当没配，整行不加载，不抛', () => {
  const cases = {
    没有文件: undefined,
    空文件: '',
    缺钥匙: WRITTEN.replace(/\n {2}BYWOOD_DESK_KEY:.*\n/, '\n'),
    缺地址: WRITTEN.replace(/\n {2}BYWOOD_DESK_URL:.*\n/, '\n'),
    不是https: WRITTEN.replace('https://autowriter-production.up.railway.app', 'http://desk.example.com'),
    带问号参数: WRITTEN.replace('railway.app\n', 'railway.app/?k=1\n'),
    带账号: WRITTEN.replace('https://', 'https://u:p@'),
    不是网址: WRITTEN.replace('https://autowriter-production.up.railway.app', 'autowriter'),
    钥匙在别处: 'version: 1\nrecords:\n  BYWOOD_DESK_URL: https://d.example.com\n  BYWOOD_DESK_KEY: k-xxxxxxxx\n',
  }
  for (const [name, text] of Object.entries(cases)) {
    const dir = home(text)
    const run = boot(ctxFor(dir))
    assert.equal(run.disabled(), true, name)
    assert.equal(run.url(), '', name)
    assert.equal(run.key(), '', name)
    rmSync(dir, { recursive: true })
  }
  const run = boot({ get: () => (() => '/') })
  assert.equal(run.disabled(), true, '路径是个目录也只当没配')
})

test('凭据文件的写法：三种引号、注释、更深的缩进、CRLF、手工带上的 /mcp、本机 http', () => {
  const variants = [
    [`# comment\nversion: 1\nrefs: # managed\n  BYWOOD_DESK_URL: https://d.example.com/ # trailing\n  BYWOOD_DESK_KEY: 'k-it''s-a-key'\n    NESTED: x\nrecords:\n  BYWOOD_DESK_KEY: nope\n`,
      'https://d.example.com/mcp', "k-it's-a-key"],
    ['refs:\n  BYWOOD_DESK_URL: "https://d.example.com/base/mcp"\n  BYWOOD_DESK_KEY: "k-\\"q\\"-key"\n',
      'https://d.example.com/base/mcp', 'k-"q"-key'],
    ['refs:\r\n  BYWOOD_DESK_URL: https://d.example.com\r\n  BYWOOD_DESK_KEY: k-crlf-key-1\r\n',
      'https://d.example.com/mcp', 'k-crlf-key-1'],
    ['refs:\n  BYWOOD_DESK_URL: http://127.0.0.1:8000\n  BYWOOD_DESK_KEY: k-local-key\n',
      'http://127.0.0.1:8000/mcp', 'k-local-key'],
  ]
  for (const [text, url, key] of variants) {
    const dir = home(text)
    const run = boot(ctxFor(dir))
    assert.equal(run.disabled(), false, text)
    assert.equal(run.url(), url)
    assert.equal(run.key(), key)
    rmSync(dir, { recursive: true })
  }
})

test('文件在哪：先用桌面端的 dshHomePath，拿不到就 $DSH_HOME，再拿不到就 ~/.dsh', () => {
  const viaService = home(WRITTEN)
  const viaEnv = home(WRITTEN.replace('k-test-not-a-real-key-000', 'k-from-dsh-home'))
  const fakeHome = mkdtempSync(join(tmpdir(), 'desk-user-'))
  mkdirSync(join(fakeHome, '.dsh'))
  writeFileSync(join(fakeHome, '.dsh', '.credentials.yaml'), WRITTEN.replace('k-test-not-a-real-key-000', 'k-from-user-home'))
  const saved = { DSH_HOME: process.env.DSH_HOME, HOME: process.env.HOME }
  try {
    process.env.DSH_HOME = viaEnv
    process.env.HOME = fakeHome
    assert.equal((r => (r.disabled(), r.key()))(boot(ctxFor(viaService))), 'k-test-not-a-real-key-000')
    assert.equal((r => (r.disabled(), r.key()))(boot({ get: () => undefined })), 'k-from-dsh-home')
    assert.equal((r => (r.disabled(), r.key()))(boot({ get: () => { throw new Error('without inject') } })), 'k-from-dsh-home')
    process.env.DSH_HOME = '  '
    assert.equal((r => (r.disabled(), r.key()))(boot({ get: () => undefined })), 'k-from-user-home', '空白的 DSH_HOME 当没设')
  } finally {
    for (const [name, value] of Object.entries(saved)) {
      if (value === undefined) delete process.env[name]
      else process.env[name] = value
    }
    for (const dir of [viaService, viaEnv, fakeHome]) rmSync(dir, { recursive: true })
  }
})

test('0.2.1 的「never started」：一个进程里 disabled 算几次都是同一个答案', () => {
  // 0.2.1 在桌面端启动时就装预设（agent-preset-registry 的 register → activate → mountPreset），
  // 之后每次进对话 diagnostic() → auditRows() 再按 entry.disabled 复查一遍。
  // 两次答案不一样（装的时候 true 所以没起，复查时 false），就是「desk-mcp … never started」。
  const dir = home(undefined)
  const run = boot(ctxFor(dir))
  assert.equal(run.disabled(), true, '启动时还没配')
  writeFileSync(join(dir, '.credentials.yaml'), WRITTEN) // 然后 /desk-setup 写了凭据文件
  assert.equal(run.disabled(), true, '复查时还得是 true：这一行压根没起，改口就报 never started')
  assert.equal(run.url(), '')

  const again = boot(ctxFor(dir)) // 完全退出、重开
  assert.equal(again.disabled(), false, '重开之后读到新配的')
  rmSync(join(dir, '.credentials.yaml'))
  assert.equal(again.disabled(), false, '反过来也一样：起了的那一行，复查时不能改说「不该在」')
  assert.equal(again.url(), 'https://autowriter-production.up.railway.app/mcp', '地址和钥匙取的是同一份')
  rmSync(dir, { recursive: true })
})

test('不靠宿主插件：宿主插件还没加载、旧版留下的读取函数在，都照样读文件', () => {
  const dir = home(WRITTEN)
  globalThis.__bywoodWritingDesk = Object.freeze({ configured: () => false, url: () => 'x', key: () => 'y' })
  try {
    const run = boot(ctxFor(dir))
    assert.equal(run.disabled(), false)
    assert.equal(run.key(), 'k-test-not-a-real-key-000')
  } finally {
    delete globalThis.__bywoodWritingDesk
    rmSync(dir, { recursive: true })
  }
})

test('在 0.2.1 那种「没声明就不许读」的 ctx 里求值也不抛', () => {
  // with (ctx) 只对 ctx 身上真有的名字取值；0.2.1 的 ctx 读没声明的服务会抛。表达式只经 ctx.get 读服务。
  const dir = home(WRITTEN)
  const base = ctxFor(dir)
  const strict = new Proxy(base, {
    get(target, prop) {
      if (typeof prop === 'symbol' || prop in target) return Reflect.get(target, prop)
      throw new Error(`cannot get property "${String(prop)}" without inject`)
    },
  })
  const run = boot(strict)
  assert.equal(run.disabled(), false)
  assert.equal(run.url(), 'https://autowriter-production.up.railway.app/mcp')
  rmSync(dir, { recursive: true })
})

test('这一行不用模板、不靠 inject 凭据库；预设里不列 0.2.1 没有的包；版本要求放到 0.2.1', () => {
  const row = deskRow()
  assert.doesNotMatch(row.slice(row.indexOf('- id: desk-mcp')), /\$\{/, '0.2.1 的 MCP 客户端不认模板，表达式里也不写 ${}')
  assert.doesNotMatch(row, /inject: \[credentials\]/)
  assert.doesNotMatch(PRESET, /name: '@deepseek-ai\/dsh-tool-office'/)
  const manifest = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'))
  assert.equal(manifest.engines.dsh, '>=0.2.1-rc.1 <0.3.0')
})
