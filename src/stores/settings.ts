import { defineStore } from 'pinia'
import { api } from '../services/api'
import type { Settings, SchedulerStatus } from '../types'

const defaultSettings = (): Settings => ({
  refresh_interval_secs: 300,
  scan_interval_secs: 900,
  trading_only: true,
  request_interval_ms: 400,
  minutely_budget: 60,
  backfill_count: 1000,
  incremental_count: 10,
  auto_start_scheduler: true,
  log_level: 'info',
  email: {
    enabled: true,
    to: '2055761346@qq.com',
    from: '',
    smtp_host: 'smtp.qq.com',
    smtp_port: 465,
    smtp_user: '',
    smtp_password: '',
  },
})

export const useSettingsStore = defineStore('settings', {
  state: () => ({
    settings: defaultSettings() as Settings,
    status: { running: false, last_refresh: null, last_scan: null } as SchedulerStatus,
  }),
  actions: {
    async load() {
      this.settings = await api.getSettings()
      this.status = await api.schedulerStatus()
    },
    async save(next: Settings) {
      this.settings = await api.updateSettings(next)
      this.status = await api.schedulerStatus()
    },
    async setRunning(running: boolean) {
      this.status = await api.setSchedulerRunning(running)
    },
    async refreshStatus() {
      this.status = await api.schedulerStatus()
    },
  },
})
