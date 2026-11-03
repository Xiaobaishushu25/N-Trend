import type { RecentOutcomeFilters } from '../types'
import type { ReviewExcursion, ReviewItemsPage, ReviewQuery, ReviewResultDetail, ReviewScoreMetric, ReviewSummary } from '../types/review'

const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
export function publishedScore(row: ReviewSummary | null | undefined, metric: ReviewScoreMetric = 'quality_v2'): number | null {
  const value = row?.scores?.[metric]
  return finite(value) ? value : null
}
export function sameReview(row: ReviewSummary | null | undefined, detail: ReviewResultDetail | null | undefined): boolean {
  return !!row && !!detail && !!row.result_version && row.event_id === detail.summary.event_id && row.result_version === detail.summary.result_version
}
export function excursionText(value: ReviewExcursion | null | undefined, fallback?: number | null): string {
  const text = (r: number) => `${r >= 0 ? '+' : ''}${r.toFixed(2)}R`
  if (value?.precision === 'bounded') {
    return finite(value.lower_r) && finite(value.upper_r) ? `${text(value.lower_r)} ～ ${text(value.upper_r)}（区间）` : '未知（有界）'
  }
  if (value) return finite(value.r) ? text(value.r) : '未知'
  return finite(fallback) ? text(fallback) : '未知'
}
export function reviewState(row: ReviewSummary | null | undefined): {
  text: string
  type: 'default' | 'error' | 'success' | 'warning' | 'info'
  color?: { color: string; textColor: string; borderColor?: string }
} {
  if (!row) return { text: '尚未加载', type: 'default' }
  const reason = row.state === 'closed' ? row.exit_reason : row.terminal_reason || row.state
  const labels: Record<string, string> = {
    target_1r: '止盈 1R',
    target: '目标止盈',
    stop: '止损',
    no_follow: '无跟随',
    time_exit: '时间退出',
    pending: '等待触发',
    open: '持仓中',
    closed: '已平仓',
    expired: '已到期',
    untriggered: '未触发',
    invalidated: '入场前失效',
    filtered: '已过滤',
    unfilled: '未成交',
    ambiguous: '同根价格歧义',
    rollover: '换月终结',
    missing_data: '行情不足',
    missing_calendar: '缺交易日历',
    missing_parameters: '缺合约参数',
  }
  const text = labels[reason || ''] || reason || '状态未知'

  if (row.state === 'closed') {
    const netR = row.net_r
    if (netR != null) {
      if (netR > 0) {
        return {
          text,
          type: 'error',
          color: { color: '#fef2f2', textColor: '#dc2626', borderColor: '#fecaca' },
        }
      }
      if (netR < 0) {
        return {
          text,
          type: 'success',
          color: { color: '#ecfdf5', textColor: '#16a34a', borderColor: '#a7f3d0' },
        }
      }
      return {
        text,
        type: 'default',
        color: { color: '#f1f5f9', textColor: '#64748b', borderColor: '#e2e8f0' },
      }
    }
    if (reason === 'target_1r' || reason === 'target') {
      return {
        text,
        type: 'error',
        color: { color: '#fef2f2', textColor: '#dc2626', borderColor: '#fecaca' },
      }
    }
    if (reason === 'stop') {
      return {
        text,
        type: 'success',
        color: { color: '#ecfdf5', textColor: '#16a34a', borderColor: '#a7f3d0' },
      }
    }
    return {
      text,
      type: 'info',
      color: { color: '#eff6ff', textColor: '#2563eb', borderColor: '#bfdbfe' },
    }
  }

  if (row.state === 'open') {
    return {
      text,
      type: 'warning',
      color: { color: '#fffbeb', textColor: '#d97706', borderColor: '#fde68a' },
    }
  }

  return {
    text,
    type: 'default',
    color: { color: '#f8fafc', textColor: '#64748b', borderColor: '#e2e8f0' },
  }
}
export function costExplanation(detail: ReviewResultDetail | null | undefined) {
  if (!detail) return { type: 'info', title: '尚未取得成本明细', desc: '加载同一结果版本的详情后才能核对成本。' }
  const s = detail.summary
  if (s.state !== 'closed') return { type: 'warning', title: reviewState(s).text, desc: s.entry_ts ? '已有模拟入场，尚无确定的往返结算。请查看覆盖原因和生命周期。' : '尚无模拟往返成交，不生成结算收益。' }
  if (!detail.cost_version || detail.cost?.settled !== true || !finite(s.gross_r) || !finite(s.net_r)) return { type: 'warning', title: '结算记录不完整', desc: '平仓状态不能单独证明成本已结算，请核对成本版本和数据覆盖。' }
  if (!finite(detail.multiplier) || detail.multiplier <= 0 || s.coverage.includes('missing_multiplier') || !finite(s.gross_amount) || !finite(s.net_amount)) return { type: 'info', title: 'R 已结算，金额覆盖不足', desc: '缺少有效合约乘数或金额数据，不能计算完整人民币成本与净额。' }
  return { type: 'success', title: '已完成模拟交易与估算成本结算', desc: '开平各 1 tick 滑点已计入模拟成交价；净收益仅另扣往返 0.5 tick 估算费用。' }
}
export function publishedQuery(f: RecentOutcomeFilters, size: number): ReviewQuery {
  const outcome = f.outcome
  return {
    filters: {
      symbols: f.symbols?.length ? f.symbols : f.symbol ? [f.symbol] : [],
      directions: f.direction ? [f.direction as 'up' | 'down'] : [], levels: f.level ? [f.level] : [], grades: f.grade ? [f.grade] : [],
      outcomes: outcome === 'win' || outcome === 'loss' || outcome === 'flat' ? [outcome] : [],
      states: outcome && !['win', 'loss', 'flat', 'no_trigger'].includes(outcome) ? [outcome === 'insufficient_data' ? 'missing_data' : outcome] : [],
      terminal_reasons: outcome === 'no_trigger' ? ['untriggered'] : [],
      score_metric: f.qualityMetric || 'quality_v2', score_min: f.qualityMin ?? null, score_max: f.qualityMax ?? null, score_status: f.qualityStatus || 'all',
      time_field: f.timeField === 'trigger' ? 'entry' : 'warning', time_start: f.timeStart || null, time_end: f.timeEnd || null,
      sessions: f.hourStart || f.hourEnd ? [{ start: f.hourStart || '00:00', end: f.hourEnd || '23:59' }] : [], sample_scope: 'deduplicated',
    },
    sort_by: f.sortBy?.startsWith('score') ? 'score' : 'time', descending: !f.sortBy?.endsWith('asc'), page_size: size,
  }
}
export function validatePublishedPage(page: ReviewItemsPage): void {
  if (!page || !Array.isArray(page.items) || !page.snapshot_id || !Number.isSafeInteger(page.total) || page.total < 0 || !Number.isSafeInteger(page.revision)) {
    throw new Error('新复盘响应不完整，请将客户端和服务端升级到同一版本。')
  }
  for (const r of page.items) {
    if (!Number.isSafeInteger(r.event_id)) {
      throw new Error(`复盘信号存在无效或溢出的事件编号 (${r.event_id})，请重新触发复盘计算。`)
    }
    if (typeof r.result_version !== 'string' || !r.result_version || !r.scores || !r.plan || !Array.isArray(r.coverage)) {
      throw new Error('新复盘响应不完整，请将客户端和服务端升级到同一版本。')
    }
  }
}
