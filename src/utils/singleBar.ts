import type { SingleBarEvent, SingleBarKind } from '../types'

export const SINGLE_BAR_COLORS: Record<SingleBarKind, { border: string; text: string; bg: string; chart: string }> = {
  hammer: { border: '#f59e0b', text: '#f59e0b', bg: 'rgba(245,158,11,.10)', chart: '#f59e0b' },
  needle: { border: '#a78bfa', text: '#a78bfa', bg: 'rgba(167,139,250,.12)', chart: '#a78bfa' },
}

export function isHammer(kind: SingleBarKind): boolean {
  return kind === 'hammer'
}

export function singleBarBadgeStyle(kind: SingleBarKind): string {
  const c = SINGLE_BAR_COLORS[kind]
  return `border:1px dashed ${c.border};color:${c.text};background:${c.bg};border-radius:999px;`
}

export function toMs(ts: string): number {
  return new Date(ts.replace(' ', 'T')).getTime()
}

export function normalizeSingleBar(e: any): SingleBarEvent {
  const trigger_bar_ts: string = e.trigger_bar_ts ?? e.triggerTime ?? ''
  const expire_bar_ts: string = e.expire_bar_ts ?? e.expireTime ?? ''
  const triggerTime = typeof e.triggerTime === 'number' ? e.triggerTime : toMs(String(trigger_bar_ts))
  const expireTime = typeof e.expireTime === 'number' ? e.expireTime : toMs(String(expire_bar_ts))
  const tStr = typeof trigger_bar_ts === 'string' && trigger_bar_ts.length >= 16 ? trigger_bar_ts : new Date(triggerTime).toISOString().slice(0, 16).replace('T', ' ') + ':00'
  const eStr = typeof expire_bar_ts === 'string' && expire_bar_ts.length >= 16 ? expire_bar_ts : new Date(expireTime).toISOString().slice(0, 16).replace('T', ' ') + ':00'
  return {
    symbol: String(e.symbol ?? ''),
    timeframe: '15m',
    kind: (e.kind === 'needle' ? 'needle' : 'hammer') as SingleBarKind,
    label: String(e.label ?? (e.kind === 'needle' ? '针·15m' : '锤·15m')),
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