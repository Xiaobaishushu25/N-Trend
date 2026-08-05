// 后端命令与事件的一层类型化封装

import { invoke } from '@tauri-apps/api/core'
import { listen } from '@tauri-apps/api/event'
import type {
  AppInfo,
  GroupRow,
  KlineRow,
  MarketSnapshot,
  RefreshStats,
  ScanResult,
  ScanRow,
  SchedulerStatus,
  Settings,
  SignalOutcome,
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
  getGroupSymbols: (groupId: number) =>
    invoke<SymbolRow[]>('get_group_symbols', { groupId }),
  addSymbolToGroup: (symbol: string, groupId: number) =>
    invoke<void>('add_symbol_to_group', { symbol, groupId }),
  removeSymbolFromGroup: (symbol: string, groupId: number) =>
    invoke<void>('remove_symbol_from_group', { symbol, groupId }),
  addSymbol: (code: string) => invoke<number>('add_symbol', { code }),
  removeSymbol: (code: string) => invoke<void>('remove_symbol', { code }),
  setSymbolFlags: (code: string, watchlist: boolean, enabled: boolean) =>
    invoke<void>('set_symbol_flags', { code, watchlist, enabled }),
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

  getSettings: () => invoke<Settings>('get_settings'),
  updateSettings: (settings: Settings) => invoke<Settings>('update_settings', { settings }),
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

export function onSignalFound(cb: (signals: SignalOutcome[]) => void) {
  return listen<SignalOutcome[]>('signal-found', (e) => cb(e.payload))
}


