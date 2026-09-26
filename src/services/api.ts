// 后端命令与事件的一层类型化封装

import { invoke } from '@tauri-apps/api/core'
import { listen } from '@tauri-apps/api/event'
import type {
  V2ModelRow,
  V2PredictionRow,
  V2ReportBundle,
  AppInfo,
  Config,
  ContractSuggestion,
  ChartKlineResponse,
  EntryTriggerHit,
  GroupRow,
  KlineRow,
  ManualLevelAlert,
  ManualLevelDto,
  ManualLevelEvent,
  ManualLevelInput,
  PatternEvent,
  PrecloseCandidate,
  PrecloseSignal,
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
  SignalAnnotation,
  SignalDecision,
  SignalUserData,
  SymbolRow,
  TrendPointDto,
  ConnectionStatus,
  ConnectionStateDto,
  AuthRecord,
  ClientLocalSettings,
  MetaDto,
  ServerStatusDto,
  ServerSettingsDto,
  ServerSettingsUpdate,
  ConfigApplyResult,
  DeviceItemDto,
  DeviceRegisterRequest,
  DeviceRegisterResponse,
  BackupStatus,
} from '../types'

export const api = {
  appInfo: () => invoke<AppInfo>('app_info'),
  setWindowSize: (width: number, height: number) =>
    invoke<void>('set_window_size', { width, height }),
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
  setSymbolFollowed: (code: string, followed: boolean) =>
    invoke<void>('set_symbol_followed', { code, followed }),
  refreshSymbolList: () => invoke<number>('refresh_symbol_list'),
  enrichSymbolNames: () => invoke<number>('enrich_symbol_names'),

  getKlines: (symbol: string, timeframe: string, limit?: number) =>
    invoke<KlineRow[]>('get_klines', { symbol, timeframe, limit }),
  getChartKlines: (symbol: string, timeframe: string, limit?: number) =>
    invoke<ChartKlineResponse>('get_chart_klines', { symbol, timeframe, limit }),
  getTrendSeries: (symbol: string, timeframe: string, limit?: number) =>
    invoke<TrendPointDto[]>('get_trend_series', { symbol, timeframe, limit }),

  refreshDataNow: () => invoke<RefreshStats>('refresh_data_now'),
  getActiveEvents: () => invoke<PatternEvent[]>('get_active_events'),
  getActivePrecloseSignals: () => invoke<PrecloseSignal[]>('get_active_preclose_signals'),
  getActivePrecloseCandidates: () => invoke<PrecloseCandidate[]>('get_active_preclose_candidates'),
  getPrecloseSignals: () => invoke<PrecloseSignal[]>('get_preclose_signals'),
  getPrecloseCandidates: () => invoke<PrecloseCandidate[]>('get_preclose_candidates'),
  getMarketSnapshot: () => invoke<MarketSnapshot[]>('get_market_snapshot'),
  listManualLevels: (symbol?: string, timeframe?: string, activeOnly = false) =>
    invoke<ManualLevelDto[]>('list_manual_levels', {
      symbol: symbol || null,
      timeframe: timeframe || null,
      activeOnly,
    }),
  createManualLevel: (input: ManualLevelInput) =>
    invoke<ManualLevelDto>('create_manual_level', { input }),
  updateManualLevel: (id: number, input: ManualLevelInput) =>
    invoke<ManualLevelDto>('update_manual_level', { id, input }),
  setManualLevelMonitoring: (id: number, enabled: boolean) =>
    invoke<ManualLevelDto>('set_manual_level_monitoring', { id, enabled }),
  archiveManualLevel: (id: number) => invoke<void>('archive_manual_level', { id }),
  deleteManualLevel: (id: number) => invoke<void>('delete_manual_level', { id }),
  getManualLevelEvents: (id: number) =>
    invoke<ManualLevelEvent[]>('get_manual_level_events', { id }),
  runScanNow: () => invoke<ScanResult>('run_scan_now'),
  runScanFastNow: () => invoke<ScanResult>('run_scan_fast_now'),
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
  getSignalUserData: (eventId: number) =>
    invoke<SignalUserData>('get_signal_user_data', { eventId }),
  addSignalAnnotation: (eventId: number, content: string) =>
    invoke<SignalAnnotation>('add_signal_annotation', { eventId, content }),
  deleteSignalAnnotation: (id: number) =>
    invoke<void>('delete_signal_annotation', { id }),
  setSignalDecision: (eventId: number, opened: boolean) =>
    invoke<SignalDecision>('set_signal_decision', { eventId, opened }),

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
  checkSymbolIntegrity: (symbol: string) => invoke<any>('check_symbol_integrity', { symbol }),
  checkAllSymbolsIntegrity: () => invoke<any[]>('check_all_symbols_integrity'),
  repairSymbolIntegrity: (symbol: string) => invoke<any>('repair_symbol_integrity', { symbol }),
  getV2Models: () => invoke<V2ModelRow[]>('get_v2_models'),
  getV2Predictions: (modelId?: string | null) => invoke<V2PredictionRow[]>('get_v2_predictions', { modelId: modelId || null }),
  backfillV2Predictions: () => invoke<{ models: number; events_seen: number; events_scored: number; predictions_written: number }>('backfill_v2_predictions'),
  getV2Report: () => invoke<V2ReportBundle>('get_v2_dataset_report'),

  // ---- Multi-terminal & Server Architecture ----
  getConnectionStatus: () => invoke<ConnectionStateDto>('get_connection_status'),
  getAuthRecord: () => invoke<AuthRecord>('get_auth_record'),
  updateAuthRecord: (record: AuthRecord) => invoke<void>('update_auth_record', { record }),
  getClientSettings: () => invoke<ClientLocalSettings>('get_client_settings'),
  updateClientSettings: (settings: ClientLocalSettings) => invoke<void>('update_client_settings', { settings }),
  getMeta: () => invoke<MetaDto>('get_meta'),
  getServerStatus: () => invoke<ServerStatusDto>('get_server_status'),
  getServerSettings: () => invoke<ServerSettingsDto>('get_server_settings'),
  updateServerSettings: (req: ServerSettingsUpdate) => invoke<ConfigApplyResult>('update_server_settings', { req }),
  listDevices: () => invoke<DeviceItemDto[]>('list_devices'),
  revokeDevice: (deviceId: string) => invoke<void>('revoke_device', { deviceId }),
  updateDeviceRole: (deviceId: string, role: string) => invoke<void>('update_device_role', { deviceId, role }),
  pairDevice: (req: DeviceRegisterRequest) => invoke<DeviceRegisterResponse>('pair_device', { req }),
  restartServer: () => invoke<void>('restart_server'),
  restartBridge: () => invoke<void>('restart_bridge'),
  getBackupStatus: () => invoke<BackupStatus>('get_backup_status'),
  triggerDatabaseBackup: () => invoke<string>('trigger_database_backup'),
  openBackupDirectory: () => invoke<void>('open_backup_directory'),
}

export function onConnectionStatusChanged(cb: (status: ConnectionStatus) => void) {
  return listen<ConnectionStatus>('connection-status-changed', (e) => cb(e.payload))
}

export function onBackupCompleted(cb: (filename: string) => void) {
  return listen<string>('backup-completed', (e) => cb(e.payload))
}

export function onServerStatus(cb: (status: ServerStatusDto) => void) {
  return listen<ServerStatusDto>('server-status', (e) => cb(e.payload))
}

export function onKlinePartial(cb: (payload: { symbol: string; timeframe: string; bar: any }) => void) {
  return listen<{ symbol: string; timeframe: string; bar: any }>('kline-partial', (e) => cb(e.payload))
}

export function onKlineClosed(cb: (payload: { symbol: string; timeframe: string; bar: any }) => void) {
  return listen<{ symbol: string; timeframe: string; bar: any }>('kline-closed', (e) => cb(e.payload))
}

export function onStateChanged(cb: (payload: { scope: string; revision: number }) => void) {
  return listen<{ scope: string; revision: number }>('state-changed', (e) => cb(e.payload))
}

export function onDataUpdated(cb: (stats: RefreshStats) => void) {
  return listen<RefreshStats>('data-updated', (e) => cb(e.payload))
}

export function onDataSourceFailover(
  cb: (event: { from: string; to: string; reason: string }) => void,
) {
  return listen<{ from: string; to: string; reason: string }>('data-source-failover', (e) => cb(e.payload))
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

export function onManualLevelAlert(cb: (alerts: ManualLevelAlert[]) => void) {
  return listen<ManualLevelAlert[]>('manual-level-alert', (e) => cb(e.payload))
}

export function onPrecloseSignal(cb: (signals: PrecloseSignal[]) => void) {
  return listen<PrecloseSignal[]>('preclose-signal', (e) => cb(e.payload))
}

export function onPrecloseCandidate(cb: (candidates: PrecloseCandidate[]) => void) {
  return listen<PrecloseCandidate[]>('preclose-candidate', (e) => cb(e.payload))
}

export function onNotificationHistoryUpdated(
  cb: (items: NotificationHistoryItem[]) => void,
) {
  return listen<NotificationHistoryItem[]>('notification-history-updated', (e) =>
    cb(e.payload),
  )
}
