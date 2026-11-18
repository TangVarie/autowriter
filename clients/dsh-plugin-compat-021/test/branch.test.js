// 兼容版和正常版的边界：正常版不许被兼容改动带歪，该一样的地方要一样。
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'

const here = new URL('../', import.meta.url)
const main = new URL('../../dsh-plugin/', import.meta.url)
const read = (base, path) => readFileSync(new URL(path, base), 'utf8')

test('正常版没被兼容改动带歪', () => {
  const manifest = JSON.parse(read(main, 'package.json'))
  assert.match(manifest.engines.dsh, /^>=0\.2\.2/, '正常版的版本要求还是 0.2.2 起')
  assert.doesNotMatch(manifest.version, /compat/)
  const preset = read(main, 'presets/writing-desk.patch.yml')
  assert.match(preset, /\$\{credential:BYWOOD_DESK_KEY\}/, '正常版照旧用凭据模板')
  assert.match(preset, /name: '@deepseek-ai\/dsh-tool-office'/, '正常版照旧有 Office 工具')
  assert.doesNotMatch(preset, /__bywoodWritingDesk/)
  assert.throws(() => read(main, 'lib/credfile.js'), /ENOENT/, '读凭据文件那一套只在兼容版里')
})

test('兼容版自己标明是兼容版', () => {
  const manifest = JSON.parse(read(here, 'package.json'))
  assert.equal(manifest.name, JSON.parse(read(main, 'package.json')).name, '同一个包名：和正常版二选一')
  assert.match(manifest.version, /-compat021\./)
  assert.match(JSON.parse(read(here, 'locale/zh.json')).meta.title, /兼容版/)
})

test('入口、浏览器端、图标、文档和正常版逐字节相同', () => {
  const same = ['index.js', 'client.js', 'cordis.patch.yml', 'icon.svg', 'test/desk.test.js']
  for (const name of readdirSync(new URL('docs/', main))) same.push(`docs/${name}`)
  for (const path of same) assert.equal(read(here, path), read(main, path), `${path} 和正常版不一样了`)
})

test('lib/desk.js 只差两句「配完要重启」', () => {
  const restart = [
    ['中间用空格隔开；配好之后完全退出 DeepSeek Harness 再打开（Mac 按 ⌘Q），再点左侧栏的「写作台」。」', '中间用空格隔开，然后新开一个对话。」'],
    ['要重配就 /desk-setup 地址 钥匙，配完完全退出 DeepSeek Harness 再打开（Mac 按 ⌘Q）。」', '要重配就 /desk-setup 地址 钥匙，配完新开一个对话。」'],
  ]
  let compat = read(here, 'lib/desk.js')
  for (const [ours, theirs] of restart) {
    assert.ok(compat.includes(ours), `兼容版里找不到：${ours.slice(0, 20)}…`)
    compat = compat.replace(ours, theirs)
  }
  assert.equal(compat, read(main, 'lib/desk.js'), '除了那两句，desk.js 要和正常版一样：正常版改了逻辑，这里也要跟')
})
