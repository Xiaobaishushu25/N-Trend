// 全局局内通知：单例管理模式，不依赖任何 Provider，任何模块/回调里都能直接调用。
// 首次调用时自动创建并挂载通知宿主组件（AppNotificationHost）。

import { createApp, reactive } from 'vue'
import AppNotificationHost from '../components/AppNotificationHost.vue'
import { getCurrentWebviewWindow } from '@tauri-apps/api/webviewWindow'
import { api } from '../services/api'

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
  /** 可选：结构化信号卡片内容（展示比纯文本更美观的信号通知） */
  signal?: NotifyItem['signal']
  /** 可选：结构化入场价提醒内容 */
  entryTrigger?: NotifyItem['entryTrigger']
  /** 单K锤形态结构化通知 */
  singleBar?: NotifyItem['singleBar']
  /** 仅写入历史通知，不弹出应用内卡片 */
  recordOnly?: boolean
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
  /** 结构化信号卡片内容；存在时优先以卡片形式展示 */
  signal?: {
    code: string
    name: string
    direction: string
    level: string
    grade: string
    score: number
    entry: number
    stop: number
    target: number
    /** 通知时间（HH:mm）；未传入时自动取当前时间 */
    time?: string
  }
  /** 结构化入场价提醒内容：突出品种名称、方向与价格 */
  entryTrigger?: {
    symbol: string
    name: string
    direction: string
    entry: number
    latest: number
  }
  /** 单K形态：上影锤/下影锤 卡片内容 */
  singleBar?: {
    symbol: string
    name: string
    label: string // 上影锤 / 下影锤
    kind: 'hammer' | 'needle'
    time: string // HH:mm
    price: number
  }
}

export const notifyItems = reactive<NotifyItem[]>([])

const timers = new Map<number, ReturnType<typeof setTimeout>>()
let seed = 0
let hostApp: ReturnType<typeof createApp> | null = null

function ensureHost() {
  if (hostApp) return
  // 通知只在主窗口展示，设置窗口等子窗口不弹右下角通知
  if (!isMainWindow()) return
  const container = document.createElement('div')
  container.className = 'app-notify-host-root'
  document.body.appendChild(container)
  hostApp = createApp(AppNotificationHost)
  hostApp.mount(container)
}

export function isMainWindow(): boolean {
  try {
    return getCurrentWebviewWindow().label === 'main'
  } catch {
    // 浏览器预览等非 Tauri 环境，保持原有行为
    return true
  }
}

function schedule(item: NotifyItem) {
  if (item.remaining <= 0) return
  timers.set(
    item.id,
    setTimeout(() => dismiss(item.id), item.remaining),
  )
}

function push(type: NotifyType, content: string, options?: NotifyOptions): number {
  const recordOnly = options?.recordOnly ?? false
  if (!recordOnly) ensureHost()
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
    signal: options?.signal,
    entryTrigger: options?.entryTrigger,
    singleBar: options?.singleBar,
  }
  if (!recordOnly) {
    notifyItems.push(item)
    schedule(item)
  }
  if (isMainWindow()) {
    void api.recordNotification({
      kind: type,
      title: item.title ?? null,
      content: item.content,
      signal: item.signal
        ? { ...item.signal, time: item.signal.time ?? null }
        : null,
      entry_trigger: item.entryTrigger ?? null,
      single_bar: item.singleBar ?? null,
    }).catch(() => {
      // 浏览器预览或后端未包含新命令时，历史功能静默降级
    })
  }
  return item.id
}

function withSignalTime(data: NonNullable<NotifyItem['signal']>) {
  return {
    ...data,
    time: data.time ?? new Date().toLocaleTimeString('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
    }),
  }
}

/** 手动关闭某条通知 */
export function dismiss(id: number) {
  const timer = timers.get(id)
  if (timer) clearTimeout(timer)
  timers.delete(id)
  const idx = notifyItems.findIndex((i) => i.id === id)
  if (idx >= 0) notifyItems.splice(idx, 1)
}

/** 一键清空当前所有应用内通知 */
export function dismissAll() {
  for (const timer of timers.values()) clearTimeout(timer)
  timers.clear()
  notifyItems.splice(0, notifyItems.length)
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
    // 持久通知（remaining=0，如信号卡片/入场价提醒）：悬停移开也不消失，只能手动关闭
    return
  }
  schedule(item)
}

export const notify = {
  success: (content: string, options?: NotifyOptions) => push('success', content, options),
  info: (content: string, options?: NotifyOptions) => push('info', content, options),
  warning: (content: string, options?: NotifyOptions) => push('warning', content, options),
  error: (content: string, options?: NotifyOptions) => push('error', content, options),
  /** 持久化的信号卡片通知（不自动关闭，可手动关闭） */
  signal: (data: NonNullable<NotifyItem['signal']>) =>
    push('info', '', { duration: 0, signal: withSignalTime(data) }),
  /** 仅写入历史通知，不弹出应用内卡片 */
  recordSignal: (data: NonNullable<NotifyItem['signal']>) =>
    push('info', '', { duration: 0, recordOnly: true, signal: withSignalTime(data) }),
  /** 持久化的入场价提醒（不自动关闭，可手动关闭） */
  singleBar: (data: NonNullable<NotifyItem['singleBar']>) =>
    push('info', '', { duration: 4000, singleBar: data }),
  entryTrigger: (data: NonNullable<NotifyItem['entryTrigger']>) =>
    push('info', '', { duration: 0, entryTrigger: data }),
}



