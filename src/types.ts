// 与 Rust 侧 DTO/实体一一对应的类型定义

export interface SymbolRow {
  code: string
  name: string
  variety: string
  exchange: string
  node: string
  watchlist: boolean
  enabled: boolean
  created_at: string
  updated_at: string
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

export interface Settings {
  refresh_interval_secs: number
  scan_interval_secs: number
  trading_only: boolean
  request_interval_ms: number
  minutely_budget: number
  backfill_count: number
  incremental_count: number
  auto_start_scheduler: boolean
  log_level: string
  email: EmailSettings
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

