import calendar from './researchBoxCalendar.json'
import type { KlineRow } from '../types'

export type ResearchBoxPeriod = 24 | 48 | 96
export const researchBoxTimeframes = ['5m', '15m', '30m'] as const
export type ResearchBoxTimeframe = (typeof researchBoxTimeframes)[number]
export type ResearchBoxEnd = 'active' | 'breakout' | 'expired' | 'gap' | 'rollover'
export type ResearchBar = Pick<KlineRow, 'ts' | 'open' | 'high' | 'low' | 'close' | 'volume'> & {
  rollover?: boolean
  valid?: boolean
  segmentReset?: boolean
}
export interface ResearchBox {
  startIndex: number
  confirmedIndex: number
  endIndex: number
  lower: number
  upper: number
  period: ResearchBoxPeriod
  endReason: ResearchBoxEnd
  touches: { index: number; side: 'upper' | 'lower' }[]
}

/** Chart timestamps are Beijing wall-clock strings, displayed as UTC by the chart. */
export const researchChartTime = (ts: string): number => Date.parse(`${ts.slice(0, 19).replace(' ', 'T')}Z`) / 1000
const daySeconds = 86400
const holidays = calendar.holidays as Record<string, string[][]>
const dayMinutes = [...range(545, 620), ...range(635, 695), ...range(815, 905)]

function range(start: number, end: number): number[] {
  return Array.from({ length: (end - start) / 5 }, (_, i) => start + i * 5)
}
function dateString(t: number): string { return new Date(t * 1000).toISOString().slice(0, 10) }
function trading(t: number): boolean {
  const date = dateString(t)
  const weekday = new Date(t * 1000).getUTCDay()
  return weekday !== 0 && weekday !== 6 && !!holidays[date.slice(0, 4)] &&
    !holidays[date.slice(0, 4)].some(([a, b]) => date >= `${date.slice(0, 4)}-${a}` && date <= `${date.slice(0, 4)}-${b}`)
}
function expectedSlots(rows: ResearchBar[], symbol: string, timeframe: ResearchBoxTimeframe): Map<number, number> {
  const product = symbol.split('.').pop()?.match(/^[a-z]+/i)?.[0].toUpperCase() ?? ''
  if (!calendar.products.includes(product) || !rows.length) return new Map()
  const first = researchChartTime(rows[0].ts)
  const last = researchChartTime(rows[rows.length - 1].ts)
  if (!Number.isFinite(first) || !Number.isFinite(last)) return new Map()
  const start = Math.floor(first / daySeconds) * daySeconds - daySeconds
  const finish = Math.floor(last / daySeconds) * daySeconds
  const slots: number[] = []
  for (let day = start; day <= finish; day += daySeconds) {
    if (!trading(day)) continue
    slots.push(...dayMinutes.map(m => day + m * 60))
    let nextWeekday = day + daySeconds
    while ([0, 6].includes(new Date(nextWeekday * 1000).getUTCDay())) nextWeekday += daySeconds
    if (!calendar.noNight.includes(product) && trading(nextWeekday)) {
      const end = calendar.nightUntil0100.includes(product) ? 1500 : 1380
      slots.push(...range(1265, end + 5).map(m => day + m * 60))
    }
  }
  const minutes = parseInt(timeframe, 10)
  const ends = minutes === 5 ? slots : slots.map(t => {
    // Match the chart history aggregator's calendar bucket labels, including
    // session tails (10:15 is labelled 10:30 on the 30-minute chart).
    const midnight = Math.floor(t / daySeconds) * daySeconds
    const bucket = Math.max(1, Math.ceil((t - midnight) / (minutes * 60)))
    return midnight + bucket * minutes * 60
  })
  return new Map([...new Set(ends)].sort((a, b) => a - b).map((t, i) => [t, i]))
}

function valid(row: ResearchBar): boolean {
  return row.valid !== false && [row.open, row.high, row.low, row.close, row.volume].every(Number.isFinite) &&
    row.low > 0 && row.low <= Math.min(row.open, row.close) && row.high >= Math.max(row.open, row.close) && row.volume >= 0
}

/** Mirrors frozen Python box_signals: no signal/profit parameters are changed. */
export function detectResearchBoxes(rows: ResearchBar[], period: ResearchBoxPeriod, symbol: string, timeframe: ResearchBoxTimeframe = '5m'): ResearchBox[] {
  const expected = expectedSlots(rows, symbol, timeframe)
  const result: ResearchBox[] = []
  let active: ResearchBox | null = null
  let run = 0
  let previousSlot: number | undefined
  let previousValid = false
  for (let i = 0; i < rows.length; i++) {
    const row = rows[i]
    const slot = expected.get(researchChartTime(row.ts))
    const good = valid(row) && slot !== undefined
    const reset = !good || !previousValid || row.rollover || row.segmentReset ||
      previousSlot === undefined || slot !== previousSlot + 1
    if (reset) {
      if (active) {
        active.endReason = row.rollover ? 'rollover' : 'gap'
        active.endIndex = Math.max(active.confirmedIndex, i - 1)
      }
      active = null
      run = 0
    }
    previousSlot = slot
    previousValid = good
    if (!good) continue
    run++
    if (active) {
      active.endIndex = i
      if (row.close < active.lower || row.close > active.upper) {
        active.endReason = 'breakout'
        active = null
      } else if (i - active.confirmedIndex >= period) {
        active.endReason = 'expired'
        active = null
      }
    }
    if (!active && run >= period) {
      const start = i - period + 1
      let upper = -Infinity; let lower = Infinity
      for (let j = start; j <= i; j++) {
        upper = Math.max(upper, rows[j].high)
        lower = Math.min(lower, rows[j].low)
      }
      const width = upper - lower
      if (!(width > 0)) continue
      const touches: ResearchBox['touches'] = []
      let ambiguous = false
      for (let j = start; j <= i; j++) {
        const up = rows[j].high >= upper - .1 * width
        const down = rows[j].low <= lower + .1 * width
        if (up && down) { ambiguous = true; break }
        const side = up ? 'upper' : down ? 'lower' : null
        if (side && touches[touches.length - 1]?.side !== side) touches.push({ index: j, side })
      }
      if (!ambiguous && touches.length >= 4 && row.close >= lower && row.close <= upper) {
        active = { startIndex: start, confirmedIndex: i, endIndex: i, lower, upper, period, endReason: 'active', touches }
        result.push(active)
      }
    }
  }
  return result
}
