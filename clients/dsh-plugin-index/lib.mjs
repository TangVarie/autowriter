// 插件索引的读写和校验。规则照 DeepSeek Harness 的索引格式 1 写的
// （它自己的校验器在 packages/util/plugin-market-index），这里不依赖 DSH。

const SEMVER = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/
const COMMIT = /^[0-9a-f]{40}$/
const DATA_ICON = /^data:(?:image\/svg\+xml|image\/png|image\/jpeg|image\/webp);base64,([A-Za-z0-9+/]*={0,2})$/
const PACKAGE_PATH = /^[A-Za-z0-9._@+-]+(?:\/[A-Za-z0-9._@+-]+)*$/
const MAX_ICON_BYTES = 32 * 1024

const fail = (path, reason) => { throw new Error(`${path}: ${reason}`) }

function localized(value, path) {
  if (typeof value === 'string') return
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail(path, '要是字符串，或按语言分的对象')
  if (typeof value.en !== 'string') fail(path, '按语言分时必须有 en')
  for (const [lang, text] of Object.entries(value)) if (typeof text !== 'string') fail(`${path}.${lang}`, '要是字符串')
}

/** 校验整份索引；有问题就抛错，错误信息里带出问题的位置。 */
export function validateIndex(index) {
  if (!index || typeof index !== 'object') fail('$', '要是一个对象')
  if (index.format !== 1) fail('$.format', '只认 1')
  localized(index.title, '$.title')
  if (!Array.isArray(index.plugins)) fail('$.plugins', '要是数组')
  const names = new Set()
  index.plugins.forEach((plugin, p) => {
    const at = `$.plugins[${p}]`
    if (typeof plugin.name !== 'string' || !plugin.name) fail(`${at}.name`, '要是包名')
    if (names.has(plugin.name)) fail(`${at}.name`, '包名重复')
    names.add(plugin.name)
    if (plugin.title !== undefined) localized(plugin.title, `${at}.title`)
    if (plugin.description !== undefined) localized(plugin.description, `${at}.description`)
    if (plugin.icon !== undefined) {
      const match = DATA_ICON.exec(plugin.icon)
      if (!match) fail(`${at}.icon`, '这份索引只用内联的 data: 图标')
      if (Buffer.from(match[1], 'base64').length > MAX_ICON_BYTES) fail(`${at}.icon`, '解码后不能超过 32 KiB')
    }
    if (!Array.isArray(plugin.versions) || plugin.versions.length === 0) fail(`${at}.versions`, '至少要有一个版本')
    const seen = new Set()
    plugin.versions.forEach((version, v) => {
      const vat = `${at}.versions[${v}]`
      if (typeof version.version !== 'string' || !SEMVER.test(version.version)) fail(`${vat}.version`, '要是确切的版本号')
      if (seen.has(version.version)) fail(`${vat}.version`, '版本号重复')
      seen.add(version.version)
      if (!Number.isInteger(version.publishTimestamp) || version.publishTimestamp < 0) fail(`${vat}.publishTimestamp`, '要是 Unix 秒')
      const source = version.source
      if (!source || source.type !== 'git') fail(`${vat}.source.type`, '这份索引只发 git 来源')
      let url
      try {
        url = new URL(source.url)
      } catch {
        fail(`${vat}.source.url`, '要是网址')
      }
      if (url.protocol !== 'https:') fail(`${vat}.source.url`, '要是 https')
      if (!/^(?:\/[^/]+){2,}$/.test(url.pathname) || /\.git$/.test(url.pathname)) fail(`${vat}.source.url`, '写仓库页面的地址，不带 .git')
      if (typeof source.commit !== 'string' || !COMMIT.test(source.commit)) fail(`${vat}.source.commit`, '要是小写的 40 位提交号')
      if (source.path !== undefined && (typeof source.path !== 'string' || !PACKAGE_PATH.test(source.path) || /(^|\/)\.\.?(\/|$)/.test(source.path))) {
        fail(`${vat}.source.path`, '要是仓库里的相对目录')
      }
      if (version.engines !== undefined && typeof version.engines?.dsh !== 'string') fail(`${vat}.engines.dsh`, '要是版本范围')
    })
  })
  return index
}

/**
 * 按 SemVer 的先后比两个版本号：负数是 a 旧，正数是 a 新，0 是一样。
 * 预发布版排在同号正式版之前（1.1.0-rc.1 < 1.1.0-rc.2 < 1.1.0）；`+构建号` 不参与比较。
 */
export function compareVersions(a, b) {
  const split = (version) => {
    const [core, ...pre] = version.split('+')[0].split('-')
    return { core: core.split('.').map(Number), pre: pre.length ? pre.join('-').split('.') : [] }
  }
  const [x, y] = [split(a), split(b)]
  for (let i = 0; i < 3; i++) if (x.core[i] !== y.core[i]) return x.core[i] - y.core[i]
  if (!x.pre.length || !y.pre.length) return (x.pre.length ? 0 : 1) - (y.pre.length ? 0 : 1)
  for (let i = 0; i < Math.max(x.pre.length, y.pre.length); i++) {
    const [p, q] = [x.pre[i], y.pre[i]]
    if (p === undefined) return -1
    if (q === undefined) return 1
    const [pn, qn] = [/^\d+$/.test(p), /^\d+$/.test(q)]
    if (pn && qn) {
      if (Number(p) !== Number(q)) return Number(p) - Number(q)
    } else if (pn !== qn) {
      return pn ? -1 : 1 // 纯数字的标识排在带字母的前面
    } else if (p !== q) {
      return p < q ? -1 : 1
    }
  }
  return 0
}

/**
 * 给一个插件加一条版本记录，新的排在最前面。
 * 同一个版本号已经在了就报错：发过的版本不能改指向。
 */
export function addVersion(index, { name, version, commit, path, repository, engines, publishTimestamp }) {
  const plugin = index.plugins.find(entry => entry.name === name)
  if (!plugin) throw new Error(`索引里没有 ${name} 这个插件`)
  if (plugin.versions.some(entry => entry.version === version)) throw new Error(`${name} 的 ${version} 已经在索引里了；要发新版先改 package.json 的版本号`)
  const highest = plugin.versions.map(entry => entry.version).sort((a, b) => compareVersions(b, a))[0]
  if (highest && compareVersions(version, highest) <= 0) throw new Error(`${version} 不比索引里最高的 ${highest} 新`)
  plugin.versions.unshift({
    version,
    publishTimestamp,
    source: { type: 'git', url: repository, commit, path },
    ...(engines ? { engines: { dsh: engines } } : {}),
  })
  return validateIndex(index)
}
