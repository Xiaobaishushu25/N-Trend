// 全局局内通知：单例管理模式，不依赖任何 Provider，任何模块/回调里都能直接调用。
// 首次调用时自动创建并挂载通知宿主组件（AppNotificationHost）。

import { createApp, reactive } from 'vue'
import AppNotificationHost from '../components/AppNotificationHost.vue'

export type NotifyType = 'success' | 'info' | 'warning' | 'error'

export interface NotifyOptions {
  /** 可选标题，默认只显示内容 */
  title?: string
  /** 自动关闭时长（毫秒）；error 默认 0 = 不自动关闭 */
  duration?: number
  /** 是否显示关闭按钮，默认 true */
  closable?: boolean
  /** 悬停时暂停自动关闭，默认 true */
  keepAliveOnHover?: boolean
}

export interface NotifyItem {
  id: number
  type: NotifyType
  title?: string
  content: string
  closable: boolean
  keepAliveOnHover: boolean
  /** 剩余展示时长（毫秒），0 表示不自动关闭 */
  remaining: number
  /** 本轮计时起点（用于悬停暂停时计算已流逝时间） */
  startedAt: number
}

export const notifyItems = reactive<NotifyItem[]>([])

const timers = new Map<number, ReturnType<typeof setTimeout>>()
let seed = 0
let hostApp: ReturnType<typeof createApp> | null = null

function ensureHost() {
  if (hostApp) return
  const container = document.createElement('div')
  container.className = 'app-notify-host-root'
  document.body.appendChild(container)
  hostApp = createApp(AppNotificationHost)
  hostApp.mount(container)
}

function schedule(item: NotifyItem) {
  if (item.remaining <= 0) return
  timers.set(
    item.id,
    setTimeout(() => dismiss(item.id), item.remaining),
  )
}

function push(type: NotifyType, content: string, options?: NotifyOptions): number {
  ensureHost()
  const duration = options?.duration ?? (type === 'error' ? 0 : 4000)
  const item: NotifyItem = {
    id: ++seed,
    type,
    title: options?.title,
    content,
    closable: options?.closable ?? true,
    keepAliveOnHover: options?.keepAliveOnHover ?? true,
    remaining: duration,
    startedAt: Date.now(),
  }
  notifyItems.push(item)
  schedule(item)
  return item.id
}

/** 手动关闭某条通知 */
export function dismiss(id: number) {
  const timer = timers.get(id)
  if (timer) clearTimeout(timer)
  timers.delete(id)
  const idx = notifyItems.findIndex((i) => i.id === id)
  if (idx >= 0) notifyItems.splice(idx, 1)
}

/** 悬停暂停：记住剩余时长并取消定时器 */
export function suspend(id: number) {
  const item = notifyItems.find((i) => i.id === id)
  if (!item) return
  const timer = timers.get(id)
  if (timer) clearTimeout(timer)
  timers.delete(id)
  item.remaining = Math.max(item.remaining - (Date.now() - item.startedAt), 0)
}

/** 移出悬停后恢复倒计时 */
export function resume(id: number) {
  const item = notifyItems.find((i) => i.id === id)
  if (!item) return
  item.startedAt = Date.now()
  if (item.remaining <= 0) {
    dismiss(id)
    return
  }
  schedule(item)
}

export const notify = {
  success: (content: string, options?: NotifyOptions) => push('success', content, options),
  info: (content: string, options?: NotifyOptions) => push('info', content, options),
  warning: (content: string, options?: NotifyOptions) => push('warning', content, options),
  error: (content: string, options?: NotifyOptions) => push('error', content, options),
}
