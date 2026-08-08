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
  RefreshStats,
  ScanResult,
  ScanRow,
  SchedulerStatus,
  SignalRow,
  SymbolRow,
} from '../types'

export const api = {
  appInfo: () => invoke<AppInfo>('app_info'),

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

  refreshDataNow: () => invoke<RefreshStats>('refresh_data_now'),
  getMarketSnapshot: () => invoke<MarketSnapshot[]>('get_market_snapshot'),
  runScanNow: () => invoke<ScanResult>('run_scan_now'),
  getScanHistory: (limit?: number) => invoke<ScanRow[]>('get_scan_history', { limit }),
  getScanDetail: (scanId: number) => invoke<SignalRow[]>('get_scan_detail', { scanId }),
  getLatestSignals: (limit?: number) => invoke<SignalRow[]>('get_latest_signals', { limit }),

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


