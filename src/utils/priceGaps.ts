import type { KlineRow } from '../types'

export interface PriceGapSegment {
  fromIndex: number
  top: number
  bottom: number
  direction: 'up' | 'down'
}

/** 价格缺口的横向边界位于相邻两根 K 线中心之间。用时间查坐标，避免其他 series 改变全图逻辑下标。 */
export function gapBoundaryCoordinate<T extends { time: unknown }>(
  bars: readonly T[],
  index: number,
  coordinate: (time: T['time']) => number | null,
): number | null {
  if (index <= 0 || index >= bars.length) return null
  const before = coordinate(bars[index - 1].time)
  const after = coordinate(bars[index].time)
  return before === null || after === null ? null : (before + after) / 2
}

function sessionKey(ts: string): string {
  const minute = Number(ts.slice(11, 13)) * 60 + Number(ts.slice(14, 16))
  const day = ts.slice(0, 10)
  if (minute <= 2 * 60 + 30) return `${day}:night-late`
  if (minute <= 10 * 60 + 30) return `${day}:morning-1`
  if (minute <= 11 * 60 + 30) return `${day}:morning-2`
  if (minute <= 15 * 60) return `${day}:afternoon`
  return `${day}:night-early`
}

function isAdjacentTradingBar(prev: KlineRow, cur: KlineRow, timeframe: string): boolean {
  if (cur.rollover || prev.symbol !== cur.symbol || cur.source === 'live_quote') return false
  const periodMatch = /^(\d+)m$/.exec(timeframe)
  const minutes = periodMatch ? Number(periodMatch[1]) : 0
  const diff = (Date.parse(cur.ts.replace(' ', 'T') + 'Z') - Date.parse(prev.ts.replace(' ', 'T') + 'Z')) / 60000
  if (!Number.isFinite(diff) || diff <= 0) return false
  if (!minutes) return true
  // 同一交易小节内缺 K 线时不把数据断档误判成价格缺口；休市前后仍可比较。
  return sessionKey(prev.ts) !== sessionKey(cur.ts) || diff <= minutes * 1.5
}

export function computePriceGaps(rows: KlineRow[], timeframe: string): PriceGapSegment[] {
  const segments: PriceGapSegment[] = []
  for (let i = 1; i < rows.length; i++) {
    const prev = rows[i - 1]
    const cur = rows[i]
    if (!isAdjacentTradingBar(prev, cur, timeframe)) continue

    const direction = cur.low > prev.high ? 'up' : cur.high < prev.low ? 'down' : null
    if (!direction) continue
    let top = direction === 'up' ? cur.low : prev.low
    let bottom = direction === 'up' ? prev.high : cur.high
    const recent = rows.slice(Math.max(0, i - 14), i)
    const averageRange = recent.reduce((sum, bar) => sum + bar.high - bar.low, 0) / recent.length
    if (top - bottom < Math.max(2, averageRange * 0.3)) continue

    let filled = false
    for (let j = i + 1; j < rows.length; j++) {
      const bar = rows[j]
      if (direction === 'up') {
        if (bar.low <= bottom) {
          filled = true
          break
        }
        top = Math.min(top, bar.low)
      } else {
        if (bar.high >= top) {
          filled = true
          break
        }
        bottom = Math.max(bottom, bar.high)
      }
    }
    // 只展示当前仍未回补的价格带；完全回补后不保留历史灰块。
    if (!filled) segments.push({ fromIndex: i, top, bottom, direction })
  }
  return segments
}
