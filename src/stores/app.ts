import { defineStore } from 'pinia'
import { listen } from '@tauri-apps/api/event'
import { onDataUpdated, onScanCompleted, onSignalFound, api } from '../services/api'
import { useSymbolsStore } from './symbols'
import type { AppInfo } from '../types'

export const useAppStore = defineStore('app', {
  state: () => ({
    info: { name: 'ntrend', version: '0.1.0' } as AppInfo,
    listeners: [] as (() => void)[],
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
        await onSignalFound((signals) => {
          console.info('[signal-found]', signals)
          if (typeof window !== 'undefined' && 'Notification' in window) {
            const active = signals[0]
            if (active && Notification.permission === 'granted') {
              new Notification(`N趋势 新信号 ${active.symbol}`, {
                body: `${active.direction === 'up' ? '做多' : '做空'} ${active.level} N | 评分 ${active.score.toFixed(2)} | ${active.state}`,
              })
            }
          }
        }),
      )
      if (typeof window !== 'undefined' && 'Notification' in window && Notification.permission === 'default') {
        Notification.requestPermission()
      }
    },
  },
})

