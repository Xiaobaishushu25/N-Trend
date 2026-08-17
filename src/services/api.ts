// 后端命令与事件的一层类型化封装

import { invoke } from '@tauri-apps/api/core'
import { listen } from '@tauri-apps/api/event'
import type {
  AppInfo,
  Config,
  ContractSuggestion,
  EntryTriggerHit,
  GroupRow,
  KlineRow,
  MarketSnapshot,
  NewNotificationHistoryItem,
  NotificationHistoryItem,
  OutcomeDetail,
  OutcomeRefresh,
  RecentOutcomeFilters,
  RefreshStats,
  ReviewSignalDetail,
  ReviewStats,
  ScanResult,
  SchedulerStatus,
  SymbolRow,
  TrendPointDto,
} from '../types'

export const api = {
  appInfo: () => invoke<AppInfo>('app_info'),
  recordNotification: (item: NewNotificationHistoryItem) =>
    invoke<NotificationHistoryItem[]>('record_notification', { item }),
  getNotificationHistory: () =>
    invoke<NotificationHistoryItem[]>('get_notification_history'),

  getSymbols: () => invoke<SymbolRow[]>('get_symbols'),
  listGroups: () => invoke<GroupRow[]>('list_groups'),
  createGroup: (name: string) => invoke<GroupRow>('create_group', { name }),
  renameGroup: (id: number, name: string) => invoke<void>('rename_group', { id, name }),
  deleteGroup: (id: number) => invoke<void>('delete_group', { id }),
  reorderGroups: (ids: number[], allPosition: number) =>
    invoke<void>('reorder_groups', { ids, allPosition }),
  getGroupAllPosition: () => invoke<number>('get_group_all_position'),
  getGroupSymbols: (groupId: number) =>
    invoke<SymbolRow[]>('get_group_symbols', { groupId }),
  listSymbolGroups: (symbol: string) =>
    invoke<GroupRow[]>('list_symbol_groups', { symbol }),
  addSymbolToGroup: (symbol: string, groupId: number) =>
    invoke<void>('add_symbol_to_group', { symbol, groupId }),
  removeSymbolFromGroup: (symbol: string, groupId: number) =>
    invoke<void>('remove_symbol_from_group', { symbol, groupId }),
  reorderGroupSymbols: (groupId: number, codes: string[]) =>
    invoke<void>('reorder_group_symbols', { groupId, codes }),
  reorderSymbols: (codes: string[]) => invoke<void>('reorder_symbols', { codes }),
  addSymbol: (code: string) => invoke<number>('add_symbol', { code }),
  searchContracts: (keyword: string) =>
    invoke<ContractSuggestion[]>('search_contracts', { keyword }),
  removeSymbol: (code: string) => invoke<void>('remove_symbol', { code }),
  setSymbolFlags: (code: string, watchlist: boolean, enabled: boolean) =>
    invoke<void>('set_symbol_flags', { code, watchlist, enabled }),
  setSymbolTick: (code: string, tick: number) =>
    invoke<void>('set_symbol_tick', { code, tick }),
  refreshSymbolList: () => invoke<number>('refresh_symbol_list'),
  enrichSymbolNames: () => invoke<number>('enrich_symbol_names'),

  getKlines: (symbol: string, timeframe: string, limit?: number) =>
    invoke<KlineRow[]>('get_klines', { symbol, timeframe, limit }),
  getTrendSeries: (symbol: string, timeframe: string, limit?: number) =>
    invoke<TrendPointDto[]>('get_trend_series', { symbol, timeframe, limit }),

  refreshDataNow: () => invoke<RefreshStats>('refresh_data_now'),
  getMarketSnapshot: () => invoke<MarketSnapshot[]>('get_market_snapshot'),
  runScanNow: () => invoke<ScanResult>('run_scan_now'),
  rebuildEventsNow: () => invoke<ScanResult>('rebuild_events_now'),

  refreshOutcomesNow: () => invoke<OutcomeRefresh>('refresh_outcomes_now'),
  getReviewStats: (
    dimension: string,
    scope: string,
    version?: string | null,
    scoreMin?: number | null,
    scoreMax?: number | null,
  ) =>
    invoke<ReviewStats>('get_review_stats', {
      dimension,
      scope,
      version: version || null,
      scoreMin: scoreMin ?? null,
      scoreMax: scoreMax ?? null,
    }),
  getRecentOutcomes: (limit?: number, filters?: RecentOutcomeFilters) =>
    invoke<OutcomeDetail[]>('get_recent_outcomes', {
      limit,
      symbol: filters?.symbol || null,
      version: filters?.version || null,
      direction: filters?.direction || null,
      level: filters?.level || null,
      grade: filters?.grade || null,
      scoreMin: filters?.scoreMin ?? null,
      scoreMax: filters?.scoreMax ?? null,
      outcome: filters?.outcome || null,
    }),
  getReviewSignal: (eventId: number) =>
    invoke<ReviewSignalDetail | null>('get_review_signal', { eventId }),

  getConfig: () => invoke<Config>('get_config'),
  updateConfig: (config: Config) => invoke<Config>('update_config', { config }),
  resetConfig: () => invoke<Config>('reset_config'),
  setLastGroup: (groupId: number | null) =>
    invoke<void>('set_last_group', { groupId }),
  setTimeframes: (timeframes: string[]) =>
    invoke<void>('set_timeframes', { timeframes }),
  openLogDirectory: () => invoke<void>('open_log_directory'),
  schedulerStatus: () => invoke<SchedulerStatus>('scheduler_status'),
  setSchedulerRunning: (running: boolean) =>
    invoke<SchedulerStatus>('set_scheduler_running', { running }),
}

export function onDataUpdated(cb: (stats: RefreshStats) => void) {
  return listen<RefreshStats>('data-updated', (e) => cb(e.payload))
}

export function onQuotesUpdated(cb: (snapshots: MarketSnapshot[]) => void) {
  return listen<MarketSnapshot[]>('quote-updated', (e) => cb(e.payload))
}

export function onScanCompleted(cb: (result: ScanResult) => void) {
  return listen<ScanResult>('scan-completed', (e) => cb(e.payload))
}

export function onEntryTrigger(cb: (hits: EntryTriggerHit[]) => void) {
  return listen<EntryTriggerHit[]>('entry-trigger', (e) => cb(e.payload))
}

export function onNotificationHistoryUpdated(
  cb: (items: NotificationHistoryItem[]) => void,
) {
  return listen<NotificationHistoryItem[]>('notification-history-updated', (e) =>
    cb(e.payload),
  )
}


