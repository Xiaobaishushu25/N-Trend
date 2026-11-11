import type { IChartApi, ISeriesApi, ISeriesPrimitive, Time, UTCTimestamp } from 'lightweight-charts'
import type { KlineRow } from '../types'
import { researchChartTime } from './researchBoxes'

type Bounds = { low: number; high: number }

export interface ReplayStructure {
  id: string
  kind: 'zone' | 'box3' | 'box4'
  contract: string
  formed_ns: string
  confirmed_ns: string
  end_ns: string
  zones: Bounds[]
}

export interface ReplayVisit extends Bounds {
  id: string
  structure_id: string
  contract: string
  touch_ns: string
  classified_ns: string | null
  reaction: 'reversal' | 'breakout' | 'chop' | 'interrupted' | 'incomplete'
  delay_bars: number | null
}

export interface ReplayTrade {
  event_id: string
  variant_id: string
  policy: string
  scenario: string
  entry_ns: string
  exit_ns: string
  entry: number
  exit: number
  stop: number
  net: number
  reason: string
  signal_end?: string
  initial_stop?: number
  side?: number
  distance?: number
  marks?: { time_ns: string; kind: 'activate' | 'stop' | 'no_follow'; price?: number; effective_ns?: string }[]
}

export interface ReplayBar extends KlineRow {
  start: string
  actualEnd: string
}

export interface ZoneReplayPackage {
  version: 1
  product: string
  fold: number
  timeframe: '15m' | '30m'
  bars: ReplayBar[]
  executionBars?: ReplayBar[]
  fiveMinuteBars?: ReplayBar[]
  structures: ReplayStructure[]
  visits: ReplayVisit[]
  trades: ReplayTrade[]
  recent?: {
    first: string
    last: string
    variant: string
    statuses: { event_id: string; policy: string; reason: string }[]
  }
}

export interface ZoneReplayOverlay {
  zones: Bounds[]
  formed: string
  confirmed: string
  end: string
  visit: string
  visitLabel?: string
  confirmedLabel?: string
  viewStart?: string
  viewEnd?: string
  landmarks?: { ts: string; label: string }[]
  reaction: { ts: string; label: string } | null
  trade: {
    entry: string; exit: string; entryPrice: number; exitPrice: number; stop: number
    target?: number; side?: number; exitLabel?: string
    landmarks?: { ts: string; label: string }[]
    trailing?: { ts: string; price: number }[]
  } | null
}

function isTime(value: unknown): value is string {
  return typeof value === 'string' && /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(value) &&
    Number.isFinite(researchChartTime(value))
}

export function parseZoneReplay(json: string): ZoneReplayPackage {
  const p: ZoneReplayPackage = JSON.parse(json)
  if (!p || p.version !== 1 || !['15m', '30m'].includes(p.timeframe) ||
    typeof p.product !== 'string' || !Number.isInteger(p.fold) ||
    !Array.isArray(p.bars) || !p.bars.length || !Array.isArray(p.structures) ||
    !Array.isArray(p.visits) || !Array.isArray(p.trades)) {
    throw new Error('请选择有效的第1版研究复盘包')
  }
  const structures = new Map<string, ReplayStructure>()
  const visits = new Set<string>()
  for (const s of p.structures) {
    if (!s || typeof s.id !== 'string' || typeof s.contract !== 'string' ||
      !['zone', 'box3', 'box4'].includes(s.kind) || !isTime(s.formed_ns) ||
      !isTime(s.confirmed_ns) || !isTime(s.end_ns) || s.formed_ns > s.confirmed_ns ||
      s.confirmed_ns > s.end_ns || !Array.isArray(s.zones) || s.zones.length !== (s.kind === 'zone' ? 1 : 2) ||
      s.zones.some(z => !z || !Number.isFinite(z.low) || !Number.isFinite(z.high) || z.low >= z.high) ||
      structures.has(s.id)) throw new Error('复盘包区域数据无效')
    structures.set(s.id, s)
  }
  const contracts = new Set<string>()
  function validBar(b: ReplayBar, timeframe: string = p.timeframe) {
    if (!b || typeof b.symbol !== 'string' || b.timeframe !== timeframe ||
      !isTime(b.start) || !isTime(b.actualEnd) || !isTime(b.ts) ||
      b.start >= b.actualEnd || b.actualEnd > b.ts ||
      ![b.open, b.high, b.low, b.close, b.volume].every(Number.isFinite) ||
      b.low > Math.min(b.open, b.close) || b.high < Math.max(b.open, b.close)) {
      throw new Error('复盘包K线数据或时间顺序无效')
    }
    contracts.add(b.symbol)
  }
  for (const [i,b] of p.bars.entries()) {
    validBar(b)
    if (i && b.ts <= p.bars[i - 1].ts) throw new Error('复盘包K线时间顺序无效')
  }
  if (p.executionBars !== undefined) {
    if (!Array.isArray(p.executionBars)) throw new Error('复盘包补充K线无效')
    const keys = new Set(p.bars.map(b => `${b.symbol}:${b.ts}`))
    for (const b of p.executionBars) {
      validBar(b)
      const key = `${b.symbol}:${b.ts}`
      if (keys.has(key)) throw new Error('复盘包补充K线时间重复')
      keys.add(key)
    }
  }
  if (p.fiveMinuteBars !== undefined) {
    if (!Array.isArray(p.fiveMinuteBars)) throw new Error('复盘包5分钟K线无效')
    for (const [i, b] of p.fiveMinuteBars.entries()) {
      validBar(b, '5m')
      if (i && b.ts <= p.fiveMinuteBars[i - 1].ts) throw new Error('复盘包5分钟K线时间顺序无效')
    }
  }
  for (const v of p.visits) {
    const s = v && structures.get(v.structure_id)
    if (!v || typeof v.id !== 'string' || !s || v.contract !== s.contract || !contracts.has(v.contract) ||
      !isTime(v.touch_ns) || v.touch_ns <= s.confirmed_ns ||
      (v.classified_ns !== null && (!isTime(v.classified_ns) || v.classified_ns < v.touch_ns)) ||
      !['reversal', 'breakout', 'chop', 'interrupted', 'incomplete'].includes(v.reaction) || visits.has(v.id)) {
      throw new Error('复盘包到访记录无效')
    }
    visits.add(v.id)
  }
  for (const t of p.trades) {
    if (!t || !visits.has(t.event_id) || !isTime(t.entry_ns) || !isTime(t.exit_ns) ||
      t.entry_ns > t.exit_ns || ![t.entry, t.stop, t.exit, t.net].every(Number.isFinite) ||
      ![t.variant_id, t.policy, t.scenario, t.reason].every(s => typeof s === 'string') ||
      (t.signal_end !== undefined && !isTime(t.signal_end)) ||
      (t.marks !== undefined && (!Array.isArray(t.marks) || t.marks.some(m => !m || !isTime(m.time_ns) ||
        !['activate', 'stop', 'no_follow'].includes(m.kind) || (m.kind === 'stop' && !Number.isFinite(m.price)) ||
        (m.effective_ns !== undefined && (!isTime(m.effective_ns) || m.effective_ns < m.time_ns)))))) {
      throw new Error('复盘包成交记录无效')
    }
  }
  if (p.recent && (!/^\d{4}-\d{2}-\d{2}$/.test(p.recent.first) || !isTime(p.recent.last) ||
    typeof p.recent.variant !== 'string' || !Array.isArray(p.recent.statuses) ||
    p.recent.statuses.some(s => !s || !visits.has(s.event_id) || typeof s.policy !== 'string' ||
      typeof s.reason !== 'string'))) throw new Error('复盘包行情区间或拒单记录无效')
  return p
}

/** Marker coordinates use chart bucket labels. The detail panel keeps execution times. */
export function replayBarLabel(rows: ReplayBar[], time: string, atOpen = false): string | null {
  const row = rows.find(b => atOpen ? b.start <= time && time < b.actualEnd : b.start < time && time <= b.actualEnd)
  return row?.ts ?? null
}

export function zoneReplayOverlay(chart: IChartApi, candles: ISeriesApi<'Candlestick'>, data: ZoneReplayOverlay): ISeriesPrimitive<Time> {
  return {
    autoscaleInfo: (from, to) => {
      const t = data.trade
      if (!t || t.target === undefined) return null
      const scale = chart.timeScale()
      const first = scale.timeToIndex(researchChartTime(t.entry) as UTCTimestamp)
      const last = scale.timeToIndex(researchChartTime(t.exit) as UTCTimestamp)
      if (first === null || last === null || last < from || first > to) return null
      const prices = [t.entryPrice, t.exitPrice, t.stop, t.target, ...(t.trailing ?? []).map(s => s.price)]
      return { priceRange: { minValue: Math.min(...prices), maxValue: Math.max(...prices) } }
    },
    paneViews: () => [{
      zOrder: () => 'top',
      renderer: () => ({
        draw(target) {
          target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
            const x = (ts: string) => chart.timeScale().timeToCoordinate(researchChartTime(ts) as UTCTimestamp)
            const y = (price: number) => candles.priceToCoordinate(price)
            const a = x(data.formed), b = x(data.confirmed), c = x(data.end)
            ctx.save()
            ctx.beginPath()
            ctx.rect(0, 0, mediaSize.width, mediaSize.height)
            ctx.clip()
            ctx.font = '12px "Segoe UI", sans-serif'
            function line(x1: number, y1: number, x2: number, y2: number, color: string, dashed = false) {
              ctx.strokeStyle = color
              ctx.setLineDash(dashed ? [4, 4] : [])
              ctx.beginPath()
              ctx.moveTo(x1, y1)
              ctx.lineTo(x2, y2)
              ctx.stroke()
            }
            if (a !== null && b !== null && c !== null) {
              for (const zone of data.zones) {
                const top = y(zone.high), bottom = y(zone.low)
                if (top === null || bottom === null) continue
                ctx.fillStyle = 'rgba(124, 58, 237, .12)'
                ctx.fillRect(b, top, Math.max(1, c-b), bottom-top)
                for (const rail of [top, bottom]) {
                  line(a, rail, b, rail, '#a78bfa', true)
                  line(b, rail, Math.max(c,b+1), rail, '#7c3aed')
                }
              }
            }
            const landmarks = [{ ts: data.formed, label: '起始拐点', color: '#7c3aed' },
              { ts: data.confirmed, label: data.confirmedLabel ?? '区域确认', color: '#7c3aed' },
              { ts: data.visit, label: data.visitLabel ?? '到访', color: '#0284c7' }]
            if (data.reaction) landmarks.push({ ...data.reaction, color: '#b45309' })
            for (const mark of data.landmarks ?? []) landmarks.push({ ...mark, color: '#b45309' })
            for (const mark of data.trade?.landmarks ?? []) landmarks.push({ ...mark, color: '#7c3aed' })
            for (const [i, point] of landmarks.entries()) {
              const px = x(point.ts)
              if (px === null || px < 0 || px > mediaSize.width) continue
              line(px, 0, px, mediaSize.height, point.color, true)
              ctx.fillStyle = point.color
              const rightEdge = mediaSize.width - ctx.measureText(point.label).width - 3
              ctx.fillText(point.label, Math.max(3, Math.min(px + 3, rightEdge)), 40 + i*15)
            }
            if (data.trade) {
              const t = data.trade, start = x(t.entry), end = x(t.exit), stop = y(t.stop)
              const trailing = t.trailing ?? []
              if (start !== null && end !== null && stop !== null) {
                const moved = trailing[0] ? x(trailing[0].ts) : null
                line(start, stop, Math.max(start+1,moved ?? end), stop, '#dc2626', true)
                ctx.fillStyle = '#dc2626'
                ctx.fillText(`初始止损 ${t.stop}`, start+4, stop+13)
              }
              if (t.target !== undefined && start !== null && end !== null) {
                const target = y(t.target)
                if (target !== null) {
                  line(start, target, Math.max(start+1,end), target, '#2563eb', true)
                  ctx.fillStyle = '#2563eb'
                  ctx.fillText(`2R ${t.target}`, start+4, target-5)
                }
              }
              for (const [i, step] of trailing.entries()) {
                const px = x(step.ts), py = y(step.price)
                const next = trailing[i+1], until = next ? x(next.ts) : end
                if (px === null || py === null || until === null) continue
                line(px, py, Math.max(px+1,until), py, '#db2777')
                if (next) {
                  const nextPrice = y(next.price)
                  if (nextPrice !== null) line(until, py, until, nextPrice, '#db2777')
                } else {
                  ctx.fillStyle = '#db2777'
                  ctx.fillText('跟踪止损', px+3, py+13)
                }
              }
              const entryLabel = t.side === 1 ? '做多入场' : t.side === -1 ? '做空入场' : '入场'
              const exitLabel = t.exitLabel ? `出场 · ${t.exitLabel}` : '出场'
              for (const [ts,price,name] of [[t.entry,t.entryPrice,entryLabel],[t.exit,t.exitPrice,exitLabel]] as const) {
                const px = x(ts), py = y(price)
                if (px === null || py === null) continue
                ctx.fillStyle = '#0f766e'
                ctx.beginPath()
                ctx.arc(px, py, 4, 0, Math.PI*2)
                ctx.fill()
                const label = `${name} ${price}`
                const xLabel = Math.min(px+6, mediaSize.width - ctx.measureText(label).width - 3)
                ctx.fillText(label, Math.max(3, xLabel), py-5)
              }
            }
            ctx.restore()
          })
        },
      }),
    }],
  }
}
