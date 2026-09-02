import { defineStore } from 'pinia'
import { listen } from '@tauri-apps/api/event'
import {
  isPermissionGranted,
  requestPermission,
  sendNotification,
} from '@tauri-apps/plugin-notification'
import { onDataUpdated, onScanCompleted, onEntryTrigger, api } from '../services/api'
import { useSettingsStore } from './settings'
import { useSymbolsStore } from './symbols'
import { isMainWindow, notify } from '../utils/notify'
import type { AppInfo, RecentOutcomeFilters } from '../types'

/** 通过 Tauri 官方通知插件发送系统级通知（自动申请权限；非 Tauri 环境忽略） */
async function sendSystemNotification(title: string, body: string) {
  try {
    let granted = await isPermissionGranted()
    if (!granted) {
      granted = (await requestPermission()) === 'granted'
    }
    if (granted) {
      sendNotification({ title, body })
    }
  } catch {
    // 浏览器预览或插件不可用时静默忽略
  }
}

/** 价格显示：整数不带小数，否则保留 1 位 */
function fmtPrice(v: number): string {
  return Number.isInteger(v) ? v.toFixed(0) : v.toFixed(1)
}

export const useAppStore = defineStore('app', {
  state: () => ({
    info: { name: 'ntrend', version: '0.1.0' } as AppInfo,
    listeners: [] as (() => void)[],
    /** 复盘窗口点击明细行时带入的筛选上下文；K线页复盘模式据此拉取信号列表 */
    reviewJumpFilters: null as RecentOutcomeFilters | null,
  }),
  actions: {
    async init() {
      try {
        this.info = await api.appInfo()
      } catch {
        // 浏览器预览环境下无后端命令，忽略
      }
      this.listeners.push(
        await onDataUpdated((stats) => {
          console.info('[data-updated]', stats)
        }),
      )
      this.listeners.push(
        await listen('symbols-updated', () => {
          useSymbolsStore().load()
        }),
      )
      this.listeners.push(
        await onScanCompleted((result) => {
          console.info('[scan-completed]', result)
        }),
      )
      this.listeners.push(
        await onEntryTrigger((hits) => {
          // 只由主窗口处理，避免设置窗口重复弹出/重复发送系统通知
          if (!isMainWindow()) return
          const cfg = useSettingsStore().settings.notify
          for (const hit of hits) {
            const dirLabel = hit.direction === 'up' ? '做多' : '做空'
            if (cfg.in_app_entry_trigger) {
              notify.entryTrigger({
                symbol: hit.symbol,
                name: hit.name || hit.symbol,
                direction: hit.direction,
                entry: hit.entry,
                latest: hit.latest,
              })
            }
            if (cfg.system_entry_trigger) {
              sendSystemNotification(
                `${hit.name || hit.symbol} ${dirLabel}`,
                `入场价 ${fmtPrice(hit.entry)} · 最新 ${fmtPrice(hit.latest)}`,
              )
            }
          }
        }),
      )
      this.listeners.push(
        await listen<{ from: string; to: string; reason: string }>('data-source-failover', (event) => {
          if (!isMainWindow()) return
          const settingsStore = useSettingsStore()
          if (event.payload.to === 'sina') {
            settingsStore.status.active_data_source = '新浪 (降级)'
            notify.warning(event.payload.reason, {
              title: '数据源已自动降级',
              duration: 8000,
            })
          } else {
            settingsStore.status.active_data_source = '天勤'
            notify.success(event.payload.reason, {
              title: '主力数据源已恢复',
              duration: 5000,
            })
          }
        }),
      )
    },
  },
})

