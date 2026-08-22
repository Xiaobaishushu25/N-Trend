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
  /** 连续合约换月后的第一根K线 */
  rollover: boolean
}

/** 当前周期 MA20 长期趋势线的一个数据点 */
export interface TrendPointDto {
  ts: string
  value: number
  direction: string
}

export interface SwingDto {
  index: number
  price: number
  is_high: boolean
  ts: string
}

/** 箱体信号元数据：上下轨价格、触碰次数与箱体首末时间 */
export interface BoxDto {
  upper: number
  lower: number
  upper_touches: number
  lower_touches: number
  first_ts: string
  last_ts: string
}

export interface PatternDto {
  number: number
  level: string
  /** 分析版本：1 = 原逻辑，2 = 严格N字 + 箱体；旧记录默认视为 1 */
  logic_version: string
  /** 2026-08-14：预警K线类型；质量分已计入 score */
  warning_kind?: string
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
  warning_ts: string | null
  trigger_ts: string | null
  /** 触发bar量能：成交量 / 前20根15m均量 */
  vol_ratio: number | null
  /** 触发bar之后还有K线，量能已走完可确认 */
  vol_confirmed: boolean
  /** 触发K线相对入场价的追价深度（按R归一化），触发K线收盘前只有实时值 */
  trigger_overshoot_r?: number | null
  /** 箱体信号元数据（仅 level="box" 时存在） */
  box?: BoxDto | null
  note: string
  active: boolean
  trend_state: string
  trend_bonus: number
  trend_label: string
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

/** 前向信号事件：预警K线收盘即创建，AB端点/预警K线/入场评分落库后永不回改 */
export interface PatternEvent {
  id: number
  symbol: string
  direction: string
  grade: string
  level: string
  s0_ts: string
  s0_price: number
  s1_ts: string
  s1_price: number
  s2_ts: string
  s2_price: number
  a_move: number
  b_move: number
  a_bars: number
  b_bars: number
  retracement: number
  warning_ts: string
  detected_at: string
  warning_kind: string
  entry_score: number
  entry_score_dims: string
  entry: number
  stop: number
  target: number
  risk: number
  rr: number
  state: string
  last_advance_ts: string | null
  trigger_ts: string | null
  trigger_bar_ts: string | null
  trigger_price: number | null
  trigger_score: number | null
  trigger_volume_ratio: number | null
  overshoot_r: number | null
  hold_score: number | null
  hold_score_history: string
  outcome: string | null
  exit_reason: string | null
  exit_ts: string | null
  exit_price: number | null
  r_multiple: number | null
  mfe_r: number | null
  mae_r: number | null
  created_at: string
  updated_at: string
}

export interface SymbolFailure {
  symbol: string
  reason: string
}

export interface ScanResult {
  scanned: number
  active_count: number
  summary: string
  signals: PatternEvent[]
  new_warnings: PatternEvent[]
  newly_triggered: PatternEvent[]
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
  /** 信号分析版本：1 = 原逻辑，2 = 严格N字 + 箱体 */
  logic_version: string
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
  /** 新形态通知的最低形态评分：低于该阈值的即将触发形态不提醒 */
  new_pattern_min_score: number
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
  /** 点击进入K线图时默认展示的K线根数 */
  chart_display_bars: number
  /** K线图默认向左移动距离（根） */
  chart_right_gap: number
  /** 进入K线图时默认显示首个信号形态 */
  chart_show_first_signal: boolean
  /** 列表页/表格状态胶囊完整显示的最低评分；每低 0.2 分缩小变浅一档 */
  score_pill_full_score: number
  /** 启用的K线周期，K线页切换栏只显示勾选的周期 */
  timeframes: string[]
  /** 上次打开的分组表格（null=全部品种），应用启动后恢复 */
  last_group_id: number | null
  chart_review_focus_right: boolean
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
  event_id: number
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

/** 复盘统计：单个分组（总体或某个维度分组） */
export interface GroupStat {
  key: string
  /** 实例数（结构键去重后） */
  n: number
  /** 未结算（open/数据不足） */
  pending: number
  no_trigger: number
  /** 已结算（win + loss，含 time_exit/no_follow 按 R 正负计入） */
  settled: number
  wins: number
  losses: number
  /** 窗口内跨过换月、不计入盈亏统计 */
  rollover: number
  /** 入场被跳空穿越的笔数 */
  gap_entry: number
  /** 止损被跳空穿越的笔数 */
  gap_exit: number
  win_rate: number | null
  avg_r: number | null
  avg_bars: number | null
  avg_win_r: number | null
  avg_loss_r: number | null
  payoff: number | null
  profit_factor: number | null
  r_ge1_rate: number | null
  r_ge2_rate: number | null
  mfe_ge1_rate: number | null
  mae_le_neg1_rate: number | null
  avg_r_mfe_ge1: number | null
  avg_r_mae_le_neg1: number | null
  avg_net_r: number | null
  ext_target_n: number
  tp1_exits: number
  tp2_exits: number
  tp2_conversion: number | null
  tp2_of_ext_rate: number | null
}

export interface ReviewStats {
  sim_version: number
  overall: GroupStat
  groups: GroupStat[]
}

/** 复盘页明细表一行 */
export interface OutcomeDetail {
  event_id: number
  symbol: string
  logic_version: string
  warning_kind: string
  warning_ts: string
  detected_at: string
  direction: string
  level: string
  grade: string
  entry_score: number
  entry_score_dims: string
  s0_ts: string
  s0_price: number
  s1_ts: string
  s1_price: number
  s2_ts: string
  s2_price: number
  entry: number
  stop: number
  target: number
  risk: number
  rr: number
  created_at: string
  state: string
  outcome: string
  exit_reason: string
  trigger_ts: string | null
  trigger_bar_ts: string | null
  trigger_price: number | null
  trigger_score: number | null
  trigger_volume_ratio: number | null
  overshoot_r: number | null
  hold_score: number | null
  exit_ts: string | null
  exit_price: number | null
  r_multiple: number | null
  mfe_r: number | null
  mae_r: number | null
  bars_held: number | null
  a_move: number | null
  b_move: number | null
  a_bars: number | null
  b_bars: number | null
  retracement: number | null
  a_q: number | null
  a_net_move: number | null
  a_gap_sum: number | null
  a_gap_count: number | null
  a_atr: number | null
  a_too_long: boolean | null
  b_too_long: boolean | null
  b_fast: boolean | null
  b_weakening: boolean | null
  b_weakening_ratio: number | null
  net_r: number | null
  rollover_crossed: boolean
  gap_crossed_entry: boolean
  gap_crossed_exit: boolean
  /** 用户批注，按创建时间正序 */
  annotations: SignalAnnotation[]
  /** 用户是否按建议开仓；未记录为 null */
  opened: boolean | null
}

export interface OutcomeRefresh {
  updated: number
}

/** 复盘明细跳转K线图：完整形态结构 + 结局 */
export interface ReviewSignalDetail {
  event: PatternEvent
  outcome: OutcomeDetail | null
  annotations: SignalAnnotation[]
  opened: boolean | null
}

/** 用户给信号写的批注 */
export interface SignalAnnotation {
  id: number
  event_id: number
  content: string
  created_at: string
}

/** 用户是否按建议开仓的记录 */
export interface SignalDecision {
  event_id: number
  opened: boolean
  updated_at: string
}

/** 单个信号的用户记录聚合 */
export interface SignalUserData {
  annotations: SignalAnnotation[]
  opened: boolean | null
}

/** K线图上重绘复盘点位所需的出场信息 */
export interface ReviewExitOverlay {
  price: number | null
  ts: string | null
  outcome: string
  r: number | null
}

/** 最近信号明细筛选条件（全部可选，空值不过滤） */
export interface RecentOutcomeFilters {
  symbol?: string | null
  version?: string | null
  direction?: string | null
  level?: string | null
  grade?: string | null
  outcome?: string | null
  scoreMin?: number | null
  scoreMax?: number | null
}

/** 复盘明细跳转K线图时从复盘窗口带往主窗口的上下文 */
export interface OpenReviewChartPayload {
  symbol: string
  eventId: number
  filters?: RecentOutcomeFilters
}

export type NotifyKind = 'success' | 'info' | 'warning' | 'error'

export interface NotificationSignal {
  code: string
  name: string
  direction: string
  level: string
  grade: string
  score: number
  entry: number
  stop: number
  target: number
  time?: string | null
}

export interface NotificationEntryTrigger {
  symbol: string
  name: string
  direction: string
  entry: number
  latest: number
}

export interface NewNotificationHistoryItem {
  kind: NotifyKind
  title?: string | null
  content: string
  signal?: NotificationSignal | null
  entry_trigger?: NotificationEntryTrigger | null
}

export interface NotificationHistoryItem {
  id: number
  created_at: string
  kind: NotifyKind
  title?: string | null
  content: string
  signal?: NotificationSignal | null
  entry_trigger?: NotificationEntryTrigger | null
}

export const TIMEFRAMES = ['5m', '15m', '30m', '60m', '120m', '240m', '1d'] as const
export type Timeframe = (typeof TIMEFRAMES)[number]

