import type { OutcomeDetail, RecentOutcomeFilters, ReviewScoreMetric } from '../types'

export const reviewScoreOptions: { label: string; value: ReviewScoreMetric }[] = [
  { label: 'v2 综合 · 候选', value: 'quality_v2' },
  { label: 'v2 形态 · 候选', value: 'morphology_v2' },
  { label: 'v1 综合', value: 'quality_v1' },
  { label: 'v1 形态', value: 'morphology_v1' },
  { label: '旧入场分（0～5）', value: 'old' },
]
export function defaultReviewFilters(): RecentOutcomeFilters {
  return { symbol: '', symbols: [], version: null, direction: null, level: null, grade: null, outcome: null,
    scoreMin: null, scoreMax: null, qualityMetric: 'quality_v2', qualityMin: null, qualityMax: null,
    qualityStatus: 'all', timeField: 'warning', timeStart: null, timeEnd: null,
    hourStart: null, hourEnd: null, sortBy: 'time_desc' }
}
export function reviewFilterQuery(f: RecentOutcomeFilters = {}) {
  return {
    symbol: f.symbol || null, symbols: f.symbols?.length ? f.symbols.join(',') : null,
    version: f.version || null, direction: f.direction || null, level: f.level || null,
    grade: f.grade || null, outcome: f.outcome || null, score_min: f.scoreMin ?? null, score_max: f.scoreMax ?? null,
    quality_metric: f.qualityMetric || null, quality_min: f.qualityMin ?? null, quality_max: f.qualityMax ?? null,
    quality_status: f.qualityStatus || null, time_field: f.timeField || null,
    time_start: f.timeStart || null, time_end: f.timeEnd || null,
    hour_start: f.hourStart || null, hour_end: f.hourEnd || null, sort_by: f.sortBy || null,
  }
}
export function reviewScore(row: OutcomeDetail, metric: ReviewScoreMetric = 'quality_v2'): number | null {
  let value: number | null | undefined
  if (metric === 'old') value = row.entry_score
  else if (metric.endsWith('v2')) value = row.review_scores?.status === 'ok'
    ? metric === 'quality_v2' ? row.review_scores.quality_score : row.review_scores.morphology_score : null
  else value = row.quality?.status === 'ok'
    ? metric === 'quality_v1' ? row.quality.quality_score : row.quality.morphology_score : null
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}
export function reviewTime(row: OutcomeDetail, field: RecentOutcomeFilters['timeField'] = 'warning') {
  return field === 'trigger' ? row.trigger_ts || row.trigger_bar_ts : row.warning_ts
}
export function reviewScoreStatus(status?: string | null): string {
  const labels: Record<string, string> = { ok: '评分可用', insufficient_history: '历史K线不足', missing_endpoint: '缺少结构端点', missing_warning: '缺少预警K线', missing_data: '行情不连续', rollover: '评分窗口跨换月', partial_warning: '预警尚未收盘', invalid_geometry: '结构数据异常', invalid_bars: 'K线数据异常', invalid_atr: '均线或ATR预热不足', missing_tick: '缺少最小跳动', invalid_rules: '评分配置异常', diagnostic_unavailable: '过程指标不足' }
  return status ? labels[status] || '评分暂不可用' : '尚未计算'
}
export function validateReviewFilters(f: RecentOutcomeFilters): string | null {
  if (f.qualityMin != null && f.qualityMax != null && f.qualityMin > f.qualityMax) return '评分下限不能大于上限'
  if (f.timeStart && f.timeEnd && f.timeStart > f.timeEnd) return '开始时间不能晚于结束时间'
  return null
}
