#!/usr/bin/env node
// 发一版插件：把「这个提交上的 clients/dsh-plugin」登记进索引。
//
//   node clients/dsh-plugin-index/release.mjs <40 位提交号>
//
// 提交号要是默认分支上、已经包含这一版插件代码的那次提交（通常是合并出来的那个）。
// 版本号和 engines 从那次提交里的 package.json 读，不从工作区读，免得登记的和装到的对不上。
import { execFileSync } from 'node:child_process'
import { readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { addVersion } from './lib.mjs'

const PLUGIN_PATH = 'clients/dsh-plugin'
const REPOSITORY = 'https://github.com/TangVarie/autowriter'
const indexFile = fileURLToPath(new URL('./index.json', import.meta.url))

const commit = process.argv[2]
if (!/^[0-9a-f]{40}$/.test(commit ?? '')) {
  console.error('用法：node clients/dsh-plugin-index/release.mjs <小写的 40 位提交号>')
  process.exit(2)
}
const git = (...args) => execFileSync('git', args, { encoding: 'utf8' }).trim()
let manifest
try {
  manifest = JSON.parse(git('show', `${commit}:${PLUGIN_PATH}/package.json`))
} catch {
  console.error(`在提交 ${commit} 里读不到 ${PLUGIN_PATH}/package.json。先 git fetch，确认提交号对不对。`)
  process.exit(1)
}
// 加完这一条再整份校验（addVersion 里做）：刚建好、还没有任何版本的索引也要能登记第一条。
const index = JSON.parse(readFileSync(indexFile, 'utf8'))
try {
  addVersion(index, {
    name: manifest.name,
    version: manifest.version,
    commit,
    path: PLUGIN_PATH,
    repository: REPOSITORY,
    engines: manifest.engines?.dsh,
    publishTimestamp: Number(git('show', '-s', '--format=%ct', commit)),
  })
} catch (error) {
  console.error(error.message)
  process.exit(1)
}
writeFileSync(indexFile, `${JSON.stringify(index, null, 2)}\n`)
console.log(`已登记 ${manifest.name} ${manifest.version} -> ${commit}`)
