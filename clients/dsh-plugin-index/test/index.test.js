import test from 'node:test'
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { addVersion, validateIndex } from '../lib.mjs'

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

test('索引里每个版本指向的提交，装出来的就是那个版本', (t) => {
  let checked = 0
  for (const version of plugin().versions) {
    let manifest
    try {
      manifest = JSON.parse(execFileSync('git', ['show', `${version.source.commit}:${version.source.path}/package.json`], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }))
    } catch {
      continue // 浅克隆里没有这个提交：查不了，不算错
    }
    checked += 1
    assert.equal(manifest.name, 'bywood-writing-desk-dsh', version.version)
    assert.equal(manifest.version, version.version, `提交 ${version.source.commit} 里的版本号`)
    assert.equal(manifest.engines?.dsh, version.engines?.dsh, `${version.version} 的 engines.dsh`)
    assert.ok(manifest.dsh?.bundle, `${version.version} 要是一个 DSH bundle`)
  }
  if (checked === 0) t.skip('这份克隆里没有索引指向的提交')
})

test('版本从新到旧排，没有重复', () => {
  const versions = plugin().versions.map(version => version.version)
  assert.equal(new Set(versions).size, versions.length)
  const key = version => version.split('.').map(part => part.padStart(6, '0')).join('.')
  assert.deepEqual(versions, [...versions].sort((a, b) => (key(a) < key(b) ? 1 : -1)))
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
