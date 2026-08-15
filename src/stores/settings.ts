import { defineStore } from 'pinia'
import { api } from '../services/api'
import type { Config, SchedulerStatus } from '../types'

const defaultConfig = (): Config => ({
  app_config: {
    auto_start_scheduler: true,
    logic_version: '1',
  },
  scheduler: {
    refresh_interval_secs: 300,
    scan_interval_secs: 900,
    trading_only: true,
  },
  fetch: {
    request_interval_ms: 400,
    minutely_budget: 60,
    backfill_count: 1000,
    incremental_count: 10,
  },
  quote: {
    poll_interval_ms: 3000,
    request_interval_ms: 200,
    minutely_budget: 120,
  },
  email: {
    enabled: true,
    to: '2055761346@qq.com',
    from: '',
    smtp_host: 'smtp.qq.com',
    smtp_port: 465,
    smtp_user: '',
    smtp_password: '',
  },
  notify: {
    in_app_new_pattern: true,
    new_pattern_min_score: 0,
    in_app_entry_trigger: true,
    system_entry_trigger: false,
  },
  log: {
    level: 'info',
  },
  ui: {
    flash_ms: 900,
    breathe_hold_ms: 5000,
    min_bar_spacing: 8,
    chart_display_bars: 140,
    chart_right_gap: 10,
    chart_show_first_signal: true,
    timeframes: ['5m', '15m', '30m', '60m', '120m', '240m', '1d'],
    last_group_id: null,
  },
})

export const useSettingsStore = defineStore('settings', {
  state: () => ({
    settings: defaultConfig() as Config,
    status: { running: false, last_refresh: null, last_scan: null } as SchedulerStatus,
  }),
  actions: {
    async load() {
      this.settings = await api.getConfig()
      this.status = await api.schedulerStatus()
    },
    async save(next: Config) {
      this.settings = await api.updateConfig(next)
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
