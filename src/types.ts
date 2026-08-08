// 与 Rust 侧 DTO/实体一一对应的类型定义

export interface SymbolRow {
  code: string
  name: string
  variety: string
  exchange: string
  node: string
  watchlist: boolean
  enabled: boolean
  /** 最小变动价位（tick）；0 表示未显式设置，扫描时用内置默认表兜底 */
  tick_size: number
  created_at: string
  updated_at: string
}

/** 新浪期货合约搜索结果（标题栏搜索提示用），可能包含不同月份的合约 */
export interface ContractSuggestion {
  code: string
  name: string
  variety: string
  exchange: string
  node: string
}

export interface GroupRow {
  id: number
  name: string
  sort_index: number
}

export interface KlineRow {
  symbol: string
  timeframe: string
  ts: string
  open: number
  high: number
  low: number
  close: number
  volume: number
  hold: number
  source: string
}

export interface SwingDto {
  index: number
  price: number
  is_high: boolean
  ts: string
}

export interface PatternDto {
  number: number
  level: string
  direction: string
  grade: string
  s0: SwingDto
  s1: SwingDto
  s2: SwingDto
  a_bars: number
  b_bars: number
  a_move: number
  b_move: number
  retracement: number
  state: string
  category: string
  entry: number
  stop: number
  target: number
  risk: number
  space: number
  rr: number
  score: number
  dims: number[]
  warning_ts: string | null
  trigger_ts: string | null
  note: string
  active: boolean
}

export interface TrendDto {
  direction: string
  direction_label: string
  ma20: number
  slope: number
  price_vs_ma: number
  higher_highs: boolean
  higher_lows: boolean
  lower_highs: boolean
  lower_lows: boolean
}

export interface AnalysisDetail {
  symbol: string
  trend60: TrendDto
  signals: PatternDto[]
  full_report: string
}

export interface SignalOutcome {
  symbol: string
  number: number
  level: string
  direction: string
  grade: string
  s0: SwingDto
  s1: SwingDto
  s2: SwingDto
  a_bars: number
  b_bars: number
  a_move: number
  b_move: number
  retracement: number
  state: string
  category: string
  entry: number
  stop: number
  target: number
  risk: number
  space: number
  rr: number
  score: number
  dims: number[]
  warning_ts: string | null
  trigger_ts: string | null
  note: string
  active: boolean
}

export interface SignalRow {
  id: number
  scan_id: number
  symbol: string
  level: string
  direction: string
  grade: string
  state: string
  category: string
  entry: number
  stop: number
  target: number
  rr: number
  score: number
  note: string
  detail: string
  created_at: string
}

export interface ScanRow {
  id: number
  started_at: string
  finished_at: string
  status: string
  scanned: number
  active_count: number
  summary: string
}

export interface SymbolFailure {
  symbol: string
  reason: string
}

export interface ScanResult {
  scan_id: number
  scanned: number
  active_count: number
  summary: string
  signals: SignalOutcome[]
  failed: SymbolFailure[]
}

export interface RefreshStats {
  succeeded: number
  failures: number
}

export interface EmailSettings {
  enabled: boolean
  to: string
  from: string
  smtp_host: string
  smtp_port: number
  smtp_user: string
  smtp_password: string
}

export interface AppConfig {
  auto_start_scheduler: boolean
}

export interface SchedulerConfig {
  refresh_interval_secs: number
  scan_interval_secs: number
  trading_only: boolean
}

export interface FetchConfig {
  request_interval_ms: number
  minutely_budget: number
  backfill_count: number
  incremental_count: number
}

export interface QuoteConfig {
  poll_interval_ms: number
  request_interval_ms: number
  minutely_budget: number
}

export interface NotifyConfig {
  /** 局内新形态通知：扫描发现新的即将触发形态时弹卡片通知 */
  in_app_new_pattern: boolean
  /** 局内触发价通知：实时行情触及形态入场价时弹右下角通知（持久，需手动关闭） */
  in_app_entry_trigger: boolean
  /** 系统级触发价通知：入场价提醒同时发送系统通知 */
  system_entry_trigger: boolean
}

export interface LogConfig {
  level: string
}

export interface UiConfig {
  flash_ms: number
  breathe_hold_ms: number
  min_bar_spacing: number
  /** 启用的K线周期，K线页切换栏只显示勾选的周期 */
  timeframes: string[]
  /** 上次打开的分组表格（null=全部品种），应用启动后恢复 */
  last_group_id: number | null
}

export interface Config {
  app_config: AppConfig
  scheduler: SchedulerConfig
  fetch: FetchConfig
  quote: QuoteConfig
  email: EmailSettings
  notify: NotifyConfig
  log: LogConfig
  ui: UiConfig
}

/** 入场价触发命中：最新价已触及某形态入场点（做空=跌破，做多=突破） */
export interface EntryTriggerHit {
  signal_id: number
  symbol: string
  name: string
  direction: string
  level: string
  grade: string
  entry: number
  latest: number
}

export interface SchedulerStatus {
  running: boolean
  last_refresh: string | null
  last_scan: string | null
}

export interface MarketSnapshot {
  code: string
  latest: number | null
  change_pct: number | null
}

export interface AppInfo {
  name: string
  version: string
}

export const TIMEFRAMES = ['5m', '15m', '30m', '60m', '120m', '240m', '1d'] as const
export type Timeframe = (typeof TIMEFRAMES)[number]

