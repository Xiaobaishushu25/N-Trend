import type { SingleBarEvent, SingleBarKind } from '../types'

type RawSingleBar = Partial<SingleBarEvent> & {
  triggerTime?: unknown
  expireTime?: unknown
  trigger_bar_ts?: unknown
  expire_bar_ts?: unknown
  symbol?: unknown
  kind?: unknown
  label?: unknown
  price?: unknown
  high?: unknown
  low?: unknown
}

export const SINGLE_BAR_COLORS: Record<SingleBarKind, { border: string; text: string; bg: string; chart: string }> = {
  hammer: { border: '#f59e0b', text: '#f59e0b', bg: 'rgba(245,158,11,.10)', chart: '#f59e0b' }, // 下影锤 橙
  needle: { border: '#a78bfa', text: '#a78bfa', bg: 'rgba(167,139,250,.12)', chart: '#a78bfa' }, // 上影锤 紫
}

export function singleBarBadgeStyle(kind: SingleBarKind): string {
  const c = SINGLE_BAR_COLORS[kind]
  return `border:1px dashed ${c.border};color:${c.text};background:${c.bg};border-radius:999px;`
}

export function singleBarLabel(kind: SingleBarKind): string {
  return kind === 'needle' ? '上影锤' : '下影锤'
}

export function toMs(ts: string): number {
  return new Date(ts.replace(' ', 'T')).getTime()
}

function formatLocalTs(ms: number): string {
  const d = new Date(ms)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:00`
}

export function normalizeSingleBar(e: RawSingleBar): SingleBarEvent {
  const trigger_bar_ts = String((e as Record<string, unknown>).trigger_bar_ts ?? (e as Record<string, unknown>).triggerTime ?? '')
  const expire_bar_ts = String((e as Record<string, unknown>).expire_bar_ts ?? (e as Record<string, unknown>).expireTime ?? '')
  const triggerTime = typeof (e as Record<string, unknown>).triggerTime === 'number' ? ((e as Record<string, unknown>).triggerTime as number) : toMs(trigger_bar_ts)
  const expireTime = typeof (e as Record<string, unknown>).expireTime === 'number' ? ((e as Record<string, unknown>).expireTime as number) : toMs(expire_bar_ts)
  const tStr = typeof trigger_bar_ts === 'string' && trigger_bar_ts.length >= 16 ? trigger_bar_ts : formatLocalTs(triggerTime)
  const eStr = typeof expire_bar_ts === 'string' && expire_bar_ts.length >= 16 ? expire_bar_ts : formatLocalTs(expireTime)
  const kind: SingleBarKind = (e.kind === 'needle' ? 'needle' : 'hammer')
  return {
    symbol: String(e.symbol ?? ''),
    timeframe: '15m',
    kind,
    label: String(e.label ?? singleBarLabel(kind)),
    trigger_bar_ts: tStr,
    expire_bar_ts: eStr,
    triggerTime,
    expireTime,
    price: Number(e.price ?? 0),
    high: Number(e.high ?? 0),
    low: Number(e.low ?? 0),
  }
}

export function isExpired(e: SingleBarEvent, now = Date.now()): boolean {
  return now > e.expireTime
}

export function singleBarTitle(e: SingleBarEvent): string {
  return `${e.label} ${e.trigger_bar_ts} → ${e.expire_bar_ts}`
}
