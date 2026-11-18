// 写作台插件 · DeepSeek Harness 入口。逻辑在 lib/，这里只把 DSH 的东西递进去。
//
// 这个插件挂在 profile 根上，但写作台只对「写作台」预设开的对话生效：
//  - 系统提示那一段是按对话（agent.ctx）注册的，普通对话没有；
//  - deskcore 的工具由预设里的 dsh-mcp-client 挂，普通对话看不见；
//  - 拦住发牌 / 查重 / 存档只看 deskcore 的工具，普通对话没有这些工具。
import { install } from './lib/plugin.js'

export const name = 'bywood-writing-desk'
export const inject = ['commands', 'credentials', 'systemPrompt', 'tools']

export function apply(ctx) {
  install(ctx)
}
