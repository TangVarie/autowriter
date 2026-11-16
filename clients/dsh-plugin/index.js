// 写作台插件 · DeepSeek Harness 入口。逻辑在 lib/，这里只把 DSH 的东西递进去。
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import { install } from './lib/plugin.js'

export const name = 'bywood-writing-desk'
export const inject = ['commands', 'credentials', 'mcpManager', 'systemPrompt']

export function apply(ctx) {
  install(ctx, { createUserMessage })
}
