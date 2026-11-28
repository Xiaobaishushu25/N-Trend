import type { LiveTradeRecord, TradeFeeSummary } from '../types'
import { calcTradeRecordPnlAmount } from './multipliers'

// Dates supplied by the backend are natural Shanghai timestamps. Presentation
// must never infer a date from compact formatting, entry time, or warning time.
export function parseTradeTimestamp(ts?: string | null, _refTs?: string | null): number {
  if (!ts?.trim()) return 0
  let value = ts.trim().replace(/^(\d{4})(\d{2})(\d{2})(?=[ T])/, '$1-$2-$3').replace(' ', 'T')
  const wall = value.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/)
  if (wall) {
    const [, y, m, d, h, min, sec = '00'] = wall
    const check = new Date(Date.UTC(Number(y), Number(m) - 1, Number(d), Number(h), Number(min), Number(sec)))
    if (check.getUTCFullYear() !== Number(y) || check.getUTCMonth() + 1 !== Number(m) ||
        check.getUTCDate() !== Number(d) || check.getUTCHours() !== Number(h) ||
        check.getUTCMinutes() !== Number(min) || check.getUTCSeconds() !== Number(sec)) return 0
    value = `${y}-${m}-${d}T${h}:${min}:${sec}+08:00`
  } else if (!/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value)) {
    return 0
  }
  const timestamp = Date.parse(value)
  return Number.isFinite(timestamp) ? timestamp : 0
}

export function formatTradeDateTime(ts?: string | null, _refTs?: string | null): string {
  const timestamp = parseTradeTimestamp(ts)
  if (!timestamp) return ts?.trim() || '—'
  return new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).format(timestamp)
}

export function extractTradeDate(ts?: string | null, _refTs?: string | null): string | null {
  if (!parseTradeTimestamp(ts)) return null
  return formatTradeDateTime(ts).slice(0, 10)
}

/**
 * 计算单笔平仓记录的真实实现盈亏（元）
 */
export function getTradeRecordPnl(rec: LiveTradeRecord): number {
  if (rec.realized_pnl_amount != null && !Number.isNaN(rec.realized_pnl_amount) && rec.realized_pnl_amount !== 0) {
    return rec.realized_pnl_amount
  }
  return calcTradeRecordPnlAmount(rec.instrument_id || rec.symbol, rec.realized_pnl_pts, rec.volume)
}

export function addConfirmedTradeFee(summary: TradeFeeSummary, record: LiveTradeRecord): void {
  summary.total_count += 1
  const fee = record.fee_reconciliation
  if (fee?.status !== 'confirmed' || fee.commission_cents == null || fee.net_pnl_cents == null) return
  summary.confirmed_count += 1
  summary.commission_cents = (summary.commission_cents ?? 0) + fee.commission_cents
  summary.net_pnl_cents = (summary.net_pnl_cents ?? 0) + fee.net_pnl_cents
}

export function summarizeTradeFees(records: readonly LiveTradeRecord[]): TradeFeeSummary {
  const result: TradeFeeSummary = { confirmed_count: 0, total_count: 0, commission_cents: null, net_pnl_cents: null }
  for (const record of records) addConfirmedTradeFee(result, record)
  return result
}

export function dailyTradePnl(records: readonly LiveTradeRecord[]) {
  const days: Record<string, { totalPnl: number; count: number; winCount: number; lossCount: number; fees: TradeFeeSummary }> = {}
  for (const record of records) {
    const date = extractTradeDate(record.exit_ts)
    if (!date) continue
    const pnl = getTradeRecordPnl(record)
    const day = days[date] ??= { totalPnl: 0, count: 0, winCount: 0, lossCount: 0, fees: summarizeTradeFees([]) }
    day.totalPnl += pnl
    day.count += 1
    addConfirmedTradeFee(day.fees, record)
    if (pnl > 0) day.winCount += 1
    else if (pnl < 0) day.lossCount += 1
  }
  return days
}

/**
 * 格式化日历小格子内的紧凑金额展示
 */
export function formatCalendarPnl(amount: number): string {
  const sign = amount > 0 ? '+' : amount < 0 ? '-' : ''
  const abs = Math.abs(amount)
  if (abs >= 100000) {
    return `${sign}${(abs / 10000).toFixed(1)}w`
  }
  if (abs >= 10000) {
    return `${sign}${(abs / 10000).toFixed(1)}万`
  }
  return `${sign}¥${Math.round(abs).toLocaleString('zh-CN')}`
}
