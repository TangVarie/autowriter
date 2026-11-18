// 「写作台」预设里那一行 MCP 客户端，地址和钥匙从哪儿来。
//
// 1.0 版靠 MCP 客户端自己的 ${credential:…} 模板去凭据库取值。DeepSeek Harness 0.2.1 的 MCP 客户端
// 没有这个本事（模板是 0.2.2 才加的），而 Intel Mac 上的桌面版停在 0.2.1。于是改成：
//
//   · 这个模块在宿主插件加载时往 globalThis 上挂一个只读的小对象（GLOBAL_NAME），
//   · 预设里那一行用 `!!js` 调它（Loader 在预设为某个对话激活时才求值，晚于宿主插件加载），
//   · 它每次现读桌面端自己的凭据文件 `<DSH 主目录>/.credentials.yaml` 里 refs 下面那两项。
//
// 读的就是 /desk-setup 写进凭据库的同一个文件，所以钥匙仍然只有那一处，不另存一份。
// 代价：预设激活之后再改地址或钥匙，要完全退出桌面端重开一次才生效（模板版会自己重连）。
//
// 这里只认 credentials-local 写出来的那种 YAML 的一个子集：顶格的 `refs:`，下面缩进的 `名字: 值`，
// 值可以不带引号、带单引号或带双引号。认不出来就当没配，绝不抛。

import { readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { KEY_REF, URL_REF, normalizeBase } from './desk.js'

export const GLOBAL_NAME = '__bywoodWritingDesk'
export const CREDENTIALS_FILE = '.credentials.yaml'

/**
 * 凭据文件的路径。优先用桌面端提供的 dshHomePath（和凭据库算法一致），
 * 拿不到就按同一规则自己算：$DSH_HOME，没有就 ~/.dsh。
 */
export function credentialsPath({ homePath, env = process.env, home = homedir } = {}) {
  if (typeof homePath === 'function') {
    try {
      const path = homePath(CREDENTIALS_FILE)
      if (typeof path === 'string' && path) return path
    } catch { /* 用下面的规则 */ }
  }
  const root = String(env?.DSH_HOME ?? '').trim() || join(home(), '.dsh')
  return join(root, CREDENTIALS_FILE)
}

function scalar(raw) {
  const text = raw.trim()
  if (!text) return ''
  if (text.startsWith('"')) {
    const end = text.lastIndexOf('"')
    if (end <= 0) return ''
    try {
      return JSON.parse(text.slice(0, end + 1))
    } catch {
      return text.slice(1, end)
    }
  }
  if (text.startsWith("'")) {
    const end = text.lastIndexOf("'")
    if (end <= 0) return ''
    return text.slice(1, end).replaceAll("''", "'")
  }
  // 不带引号的：` #` 之后是注释
  const comment = text.search(/\s#/)
  return (comment === -1 ? text : text.slice(0, comment)).trim()
}

/** 从凭据文件的文本里取出 refs 下面的 `名字 → 值`。只认上面说的那个子集。 */
export function parseRefs(text) {
  const refs = new Map()
  const lines = String(text ?? '').split(/\r?\n/)
  let inRefs = false
  let indent
  for (const line of lines) {
    if (!line.trim() || line.trimStart().startsWith('#')) continue
    if (!/^\s/.test(line)) {
      inRefs = /^refs\s*:\s*(#.*)?$/.test(line)
      indent = undefined
      continue
    }
    if (!inRefs) continue
    const match = /^(\s+)([A-Za-z_][A-Za-z0-9_]*)\s*:(?:\s+(.*))?$/.exec(line)
    if (!match) continue
    indent ??= match[1]
    if (match[1] !== indent) continue
    refs.set(match[2], scalar(match[3] ?? ''))
  }
  return refs
}

/** 现读一次凭据文件，回 `{ configured, url, key }`。读不到、解析不了都当没配。 */
export function readConnection(options = {}) {
  const read = options.readFile ?? (path => readFileSync(path, 'utf8'))
  let refs
  try {
    refs = parseRefs(read(credentialsPath(options)))
  } catch {
    return { configured: false, url: '', key: '' }
  }
  const base = normalizeBase(refs.get(URL_REF) ?? '')
  const key = (refs.get(KEY_REF) ?? '').trim()
  if (!base.ok || !key) return { configured: false, url: '', key: '' }
  return { configured: true, url: `${base.base}/mcp`, key }
}

/**
 * 把读取函数挂到 globalThis 上给预设里的 `!!js` 用。回一个摘掉它的函数（只摘自己挂的那个）。
 * 三个方法各自现读一次文件：预设激活时只调这三次，文件很小。
 */
export function installGlobal(options = {}) {
  const reader = Object.freeze({
    configured: () => readConnection(options).configured,
    url: () => readConnection(options).url,
    key: () => readConnection(options).key,
  })
  globalThis[GLOBAL_NAME] = reader
  return () => {
    if (globalThis[GLOBAL_NAME] === reader) delete globalThis[GLOBAL_NAME]
  }
}
