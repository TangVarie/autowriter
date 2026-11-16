import test from 'node:test'
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { addVersion, compareVersions, validateIndex } from '../lib.mjs'

const index = JSON.parse(readFileSync(new URL('../index.json', import.meta.url), 'utf8'))
const plugin = () => structuredClone(index).plugins[0]
const entry = (over = {}) => ({
  name: 'bywood-writing-desk-dsh', version: '9.0.0', commit: 'a'.repeat(40), path: 'clients/dsh-plugin',
  repository: 'https://github.com/TangVarie/autowriter', engines: '>=0.2.2-rc.1 <0.3.0', publishTimestamp: 1_800_000_000, ...over,
})

test('仓库里那份索引合乎格式', () => {
  validateIndex(structuredClone(index))
  assert.equal(index.plugins.length, 1)
  assert.equal(index.plugins[0].name, 'bywood-writing-desk-dsh')
})

const git = (...args) => execFileSync('git', args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim()

test('索引里每个版本指向的提交，装出来的就是那个版本', (t) => {
  // 只有浅克隆里「历史不全」才算查不了。完整历史里提交不在、目录不在、package.json 读不出来，
  // 都是这一版装不上，必须红。
  const shallow = git('rev-parse', '--is-shallow-repository') === 'true'
  let skipped = 0
  for (const version of plugin().versions) {
    const { commit, path } = version.source
    let present = true
    try {
      git('cat-file', '-e', `${commit}^{commit}`)
    } catch {
      present = false
    }
    if (!present) {
      assert.ok(shallow, `${version.version} 指向的提交 ${commit} 不在仓库历史里`)
      skipped += 1
      continue
    }
    let raw
    try {
      raw = git('show', `${commit}:${path}/package.json`)
    } catch {
      assert.fail(`提交 ${commit} 里没有 ${path}/package.json（${version.version}）`)
    }
    const manifest = JSON.parse(raw)
    assert.equal(manifest.name, 'bywood-writing-desk-dsh', version.version)
    assert.equal(manifest.version, version.version, `提交 ${commit} 里的版本号`)
    assert.equal(manifest.engines?.dsh, version.engines?.dsh, `${version.version} 的 engines.dsh`)
    assert.ok(manifest.dsh?.bundle, `${version.version} 要是一个 DSH bundle`)
  }
  if (skipped > 0) t.diagnostic(`浅克隆：有 ${skipped} 个版本的提交不在本地历史里，没法核对`)
})

test('版本从新到旧排，没有重复', () => {
  const versions = plugin().versions.map(version => version.version)
  assert.equal(new Set(versions).size, versions.length)
  assert.deepEqual(versions, [...versions].sort((a, b) => compareVersions(b, a)))
})

test('addVersion：新的排最前；重复的、不比现有新的都不收', () => {
  const copy = structuredClone(index)
  addVersion(copy, entry())
  assert.equal(copy.plugins[0].versions[0].version, '9.0.0')
  assert.deepEqual(copy.plugins[0].versions[0].source, {
    type: 'git', url: 'https://github.com/TangVarie/autowriter', commit: 'a'.repeat(40), path: 'clients/dsh-plugin',
  })
  assert.throws(() => addVersion(copy, entry()), /已经在索引里/)
  assert.throws(() => addVersion(structuredClone(index), entry({ version: '0.0.1' })), /不比索引里最高的/)
  assert.throws(() => addVersion(structuredClone(index), entry({ name: 'someone-else' })), /没有 someone-else/)
})

test('validateIndex 拦得住写坏的索引', () => {
  const broken = (mutate, pattern) => {
    const copy = structuredClone(index)
    mutate(copy)
    assert.throws(() => validateIndex(copy), pattern)
  }
  broken((i) => { i.format = 2 }, /format/)
  broken((i) => { i.title = { zh: '只有中文' } }, /必须有 en/)
  broken((i) => { i.plugins[0].versions = [] }, /至少要有一个版本/)
  broken((i) => { i.plugins[0].versions[0].source.commit = 'D37C103' }, /40 位提交号/)
  broken((i) => { i.plugins[0].versions[0].source.url = 'https://github.com/TangVarie/autowriter.git' }, /不带 \.git/)
  broken((i) => { i.plugins[0].versions[0].source.url = 'http://github.com/TangVarie/autowriter' }, /https/)
  broken((i) => { i.plugins[0].versions[0].source.path = '../elsewhere' }, /相对目录/)
  broken((i) => { i.plugins[0].versions[0].version = '1.0' }, /确切的版本号/)
  broken((i) => { i.plugins[0].versions.push(structuredClone(i.plugins[0].versions[0])) }, /版本号重复/)
  broken((i) => { i.plugins[0].icon = 'icons/desk.svg' }, /data:/)
})

test('版本先后按 SemVer 算：预发布版排在同号正式版之前', () => {
  const order = ['1.0.0', '1.0.1', '1.1.0-alpha', '1.1.0-alpha.1', '1.1.0-rc.1', '1.1.0-rc.2', '1.1.0-rc.10', '1.1.0', '1.10.0', '2.0.0']
  for (let i = 0; i < order.length - 1; i++) {
    assert.ok(compareVersions(order[i], order[i + 1]) < 0, `${order[i]} < ${order[i + 1]}`)
    assert.ok(compareVersions(order[i + 1], order[i]) > 0, `${order[i + 1]} > ${order[i]}`)
  }
  assert.equal(compareVersions('1.2.3', '1.2.3'), 0)
  assert.equal(compareVersions('1.2.3+build.5', '1.2.3'), 0)
})

test('发过预发布版之后，下一个预发布版和正式版都登记得上', () => {
  const copy = structuredClone(index)
  addVersion(copy, entry({ version: '9.1.0-rc.1' }))
  addVersion(copy, entry({ version: '9.1.0-rc.2', commit: 'b'.repeat(40) }))
  addVersion(copy, entry({ version: '9.1.0', commit: 'c'.repeat(40) }))
  assert.deepEqual(copy.plugins[0].versions.slice(0, 3).map(version => version.version), ['9.1.0', '9.1.0-rc.2', '9.1.0-rc.1'])
  assert.throws(() => addVersion(copy, entry({ version: '9.1.0-rc.3', commit: 'd'.repeat(40) })), /不比索引里最高的/)
})

test('核对用的两个判据：不存在的提交、提交里不存在的路径，git 都会报错', () => {
  assert.throws(() => git('cat-file', '-e', `${'f'.repeat(40)}^{commit}`))
  const head = git('rev-parse', 'HEAD')
  assert.throws(() => git('show', `${head}:clients/no-such-plugin/package.json`))
})
