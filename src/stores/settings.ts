import { defineStore } from 'pinia'
import { api } from '../services/api'
import type { Config, SchedulerStatus, ClientLocalSettings } from '../types'

export const defaultClientSettings = (): ClientLocalSettings => ({
  serverUrl: 'http://127.0.0.1:8081',
  deviceId: '',
  deviceName: 'Desktop PC',
  theme: 'dark',
  chartDisplayBars: 200,
  mobileChartFullscreenBars: 180,
  chartRightGap: 15,
  minBarSpacing: 6,
  timeframes: ['5m', '15m', '30m', '1h', '2h', '4h', '1d'],
  lastGroupId: null,
  desktopNotificationEnabled: true,
  inAppNotificationEnabled: true,
  notificationMinScore: 0.0,
  entryTriggerNotificationEnabled: true,
  manualLevelEvents: ['approach', 'testing', 'breakout', 'rejection', 'retest'],
  logLevel: 'info',
})

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
  preclose: {
    schema_version: 2,
    enabled: true,
    lead_secs: 180,
    horizon_minutes: 60,
    in_app_notify: true,
  },
  log: {
    level: 'info',
  },
  ui: {
    flash_ms: 900,
    breathe_hold_ms: 5000,
    min_bar_spacing: 8,
    chart_display_bars: 140,
    mobile_chart_fullscreen_bars: 180,
    chart_right_gap: 10,
    chart_show_first_signal: true,
    score_pill_full_score: 3.5,
    timeframes: ['5m', '15m', '30m', '60m', '120m', '240m', '1d'],
    last_group_id: null,
    chart_review_focus_right: false,
  },
  data_source: {
    primary_source: 'tqsdk',
    fallback_enabled: true,
    tq_account: '',
    tq_password: '',
    bridge_port: 8765,
    auto_spawn_bridge: true,
    python_path: null,
  },
})

export const useSettingsStore = defineStore('settings', {
  state: () => ({
    settings: defaultConfig() as Config,
    clientSettings: defaultClientSettings(),
    status: { running: false, last_refresh: null, last_scan: null, active_data_source: '天勤' } as SchedulerStatus,
  }),
  actions: {
    async load() {
      this.settings = await api.getConfig()
      this.status = await api.schedulerStatus()
      try {
        const client = await api.getClientSettings()
        if (client) {
          this.setClientSettings(client)
        }
      } catch {
        // 静默降级
      }
    },
    setClientSettings(client: Partial<ClientLocalSettings>) {
      this.clientSettings = {
        ...this.clientSettings,
        ...client,
        inAppNotificationEnabled:
          client.inAppNotificationEnabled ??
          (client as any).in_app_notification_enabled ??
          this.clientSettings.inAppNotificationEnabled ??
          true,
        notificationMinScore: Number(
          client.notificationMinScore ??
          (client as any).notification_min_score ??
          this.clientSettings.notificationMinScore ??
          0,
        ),
        entryTriggerNotificationEnabled:
          client.entryTriggerNotificationEnabled ??
          (client as any).entry_trigger_notification_enabled ??
          this.clientSettings.entryTriggerNotificationEnabled ??
          true,
        desktopNotificationEnabled:
          client.desktopNotificationEnabled ??
          (client as any).desktop_notification_enabled ??
          this.clientSettings.desktopNotificationEnabled ??
          true,
        manualLevelEvents: Array.isArray(client.manualLevelEvents)
          ? client.manualLevelEvents
          : Array.isArray((client as any).manual_level_events)
            ? (client as any).manual_level_events
            : (this.clientSettings.manualLevelEvents || ['approach', 'testing', 'breakout', 'rejection', 'retest']),
      }
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
