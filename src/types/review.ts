/** New fixed-one-lot review API; mirrors n-protocol/src/review.rs. */
import type { SignalQualityDto, ReviewScoreDto } from '../types'

export type ReviewScoreMetric = 'old' | 'quality_v1' | 'morphology_v1' | 'quality_v2' | 'morphology_v2' | 'a_score' | 'b_score'
export interface ReviewSession { start: string; end: string }
export interface ReviewFiltersV2 {
  symbols?: string[]; directions?: ('up' | 'down')[]; levels?: string[]; grades?: string[]
  states?: string[]; outcomes?: ('win' | 'loss' | 'flat')[]; exit_reasons?: string[]; terminal_reasons?: string[]
  score_metric?: ReviewScoreMetric; score_min?: number | null; score_max?: number | null
  score_status?: 'all' | 'scored' | 'unscored'; time_field?: 'warning' | 'entry' | 'exit'
  time_start?: string | null; time_end?: string | null; sessions?: ReviewSession[]
  sample_scope?: 'deduplicated' | 'all'
}
export interface ReviewQuery {
  filters?: ReviewFiltersV2
  sort_by?: 'score' | 'time' | 'gross_amount' | 'net_amount' | 'gross_r' | 'net_r' | 'mfe_r' | 'mae_r' | 'bars_held'
  descending?: boolean; page_size?: number
}
export interface ReviewPageRequest { cursor?: number | null; limit?: number }
export interface ReviewBucket { label: string; min: number; max: number }
export interface ReviewTimeBucket { label: string; start: string; end: string }
export interface ReviewAggregateRequest { snapshot_id: string; dimensions: string[]; score_buckets?: ReviewBucket[]; time_buckets?: ReviewTimeBucket[] }
export interface ReviewSummary {
  event_id: number; result_version: string; family_id: number; family_size: number
  symbol: string; direction: string; level: string; grade: string; warning_ts: string
  entry_ts: string | null; exit_ts: string | null; state: string; exit_reason: string | null; terminal_reason: string | null
  scores: Record<ReviewScoreMetric, number | null>; score_status: string
  score_explanations: string[]
  plan: { entry: number | null; stop: number | null; reference_target: number | null; risk: number | null; rr: number | null }
  entry_price: number | null; exit_price: number | null; mfe: ReviewExcursion | null; mae: ReviewExcursion | null
  risk_points: number | null; risk_amount: number | null; gross_amount: number | null; net_amount: number | null
  gross_r: number | null; net_r: number | null; mfe_r: number | null; mae_r: number | null
  mfe_amount: number | null; mae_amount: number | null; bars_held: number; wait_bars: number
  coverage: string[]; diagnostics: Record<string, number | null>
}
export interface ReviewMetric { value: number | null; n: number }
export interface ReviewStatistics {
  count: number; counts: Record<string, number>; metrics: Record<string, ReviewMetric>
  gross_r_histogram: Record<string, number>; mfe_r_histogram: Record<string, number>; mae_r_histogram: Record<string, number>
}
export interface ReviewGroup { key: string[]; statistics: ReviewStatistics }
export interface ReviewEquityPoint { event_id: number; exit_ts: string; gross_amount: number | null; net_amount: number | null; gross_r: number | null; net_r: number | null }
export interface ReviewItemsPage { snapshot_id: string; revision: number; total: number; items: ReviewSummary[]; next_cursor: number | null }
export interface ReviewQueryResponse extends ReviewItemsPage {
  expires_at: string; raw_count: number; deduplicated_count: number; scored: number; unscored: number; overview: ReviewStatistics
  groups: Record<string, ReviewGroup[]>; equity: ReviewEquityPoint[]
}
export interface ReviewAggregateResponse { snapshot_id: string; groups: ReviewGroup[] }
export interface ReviewLifecycleNode {
  sequence: number; stage: string; occurred_at: string; known_at: string; recorded_at: string
  origin: string; comment: string; evidence: Record<string, unknown>
}
export interface ReviewExcursion {
  r: number | null; amount: number | null; price: number | null; interval_start: string | null; interval_end: string | null
  lower_r: number | null; upper_r: number | null; precision: string
}
export interface ReviewHoldingPoint {
  sequence: number; interval_start: string; interval_end: string; price: number
  floating_r: number | null; floating_amount: number | null; mfe: ReviewExcursion; mae: ReviewExcursion
  bars_held: number; hold_score: number | null; checks: Record<string, unknown>
}
export interface ReviewResultDetail {
  summary: ReviewSummary; profile: string; cost_version: string; source_minutes: number; origin: string
  input_hash: string; calendar_version: string; parameter_hash: string; computed_at: string
  multiplier: number | null; multiplier_source: string; entry_price: number | null; exit_price: number | null
  cost: ReviewCost; original_event: Record<string, unknown>; quality: SignalQualityDto | null
  review_scores: ReviewScoreDto | null; lifecycle: ReviewLifecycleNode[]; mfe: ReviewExcursion; mae: ReviewExcursion; holding_count: number
}
export interface ReviewCost {
  estimated?: boolean; settled?: boolean; currency?: string; volume?: number
  tick_size?: number | null; entry_slippage_ticks?: number; exit_slippage_ticks?: number
  additional_roundtrip_fee_ticks?: number; total_friction_ticks?: number
  entry_slippage_amount?: number | null; exit_slippage_amount?: number | null; additional_fee_amount?: number | null
}
export interface ReviewHoldingPage { event_id: number; result_version: string; total: number; items: ReviewHoldingPoint[]; next_cursor: number | null }
export interface ReviewRebuildRequest { symbols?: string[] }
export interface ReviewJob {
  id: string; status: 'queued' | 'running' | 'completed' | 'failed'; origin: string; total_symbols: number
  completed_symbols: number; processed_signals: number; coverage: Record<string, number>; errors: string[]
  created_at: string; updated_at: string; revision: number | null
}
export interface ReviewMetadata {
  profile: string; cost_version: string; timezone: string; revision: number; symbols: string[]; states: string[]
  exit_reasons: Record<string, string>; terminal_reasons: Record<string, string>; score_ranges: Record<ReviewScoreMetric, [number, number]>
  dimensions: string[]; coverage: Record<string, number>; jobs: ReviewJob[]
}
export interface ReviewChanged { job_id: string; status: ReviewJob['status']; completed_symbols: number; total_symbols: number; revision: number | null }
