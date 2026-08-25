<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NSpin, NTooltip } from 'naive-ui'
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  createChart,
  createSeriesMarkers,
  type CandlestickData,
  type HistogramData,
  type IPrimitivePaneRenderer,
  type IPrimitivePaneView,
  type ISeriesPrimitive,
  type PrimitivePaneViewZOrder,
  type IChartApi,
  type IPriceLine,
  type ISeriesMarkersPluginApi,
  type ISeriesApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'
import type { CanvasRenderingTarget2D, MediaCoordinatesRenderingScope } from 'fancy-canvas'
import type { KlineRow, PatternDto, ReviewExitOverlay, TrendPointDto, SingleBarEvent } from '../types'
import { SINGLE_BAR_COLORS } from '../utils/singleBar'
import { useSettingsStore } from '../stores/settings'

const props = defineProps<{
  symbol: string
  timeframe: string
  rows: KlineRow[]
  signals: PatternDto[]
  singleBars?: SingleBarEvent[]
  /** 是否在当前可视区间标注最高价/最低价 */
  showExtremes?: boolean
  loading?: boolean
  /** 复盘跳转模式：额外绘制出场价位与出场标记 */
  reviewExit?: ReviewExitOverlay | null
  /** 复盘模式：需要自动定位到该K线时间（优先触发/预警） */
  focusTs?: string | null
  /** 复盘信号唯一标识：用于同时间戳重复聚焦时强制触发 */
  focusKey?: number | string | null
  /** 当前周期 MA20 长期趋势线数据点 */
  trendPoints?: TrendPointDto[]
}>()

const container = ref<HTMLDivElement | null>(null)
const legend = ref<HTMLDivElement | null>(null)
const timeLeft = ref<HTMLDivElement | null>(null)
const trendVisible = ref(true)
const settingsStore = useSettingsStore()
const minBarSpacing = computed(() => settingsStore.settings.ui.min_bar_spacing)

interface GapRect {
  from: Time
  to: Time
  top: number
  bottom: number
}

interface EventLabelData {
  time: Time
  text: string
  color: string
  price: number | null
  priority: number
  side: 'above' | 'below'
  marker?: 'dot' | 'square'
}

class EventLabelPaneRenderer implements IPrimitivePaneRenderer {
  private chart: IChartApi
  private source: ISeriesApi<'Candlestick'>
  private labels: EventLabelData[]
  constructor(chart: IChartApi, source: ISeriesApi<'Candlestick'>, labels: EventLabelData[]) {
    this.chart = chart
    this.source = source
    this.labels = labels
  }
  draw(target: CanvasRenderingTarget2D) {
    if (!this.labels.length) return
    target.useMediaCoordinateSpace((scope: MediaCoordinatesRenderingScope) => {
      const { context, mediaSize } = scope
      const timeScale = this.chart.timeScale()
      const boxH = 18
      const rowGap = 4
      const priceAxisWidth = this.chart.priceScale('right').width() || 64
      const rightEdge = Math.max(2, mediaSize.width - priceAxisWidth - 4)
      context.font = '600 12px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'
      context.textBaseline = 'middle'
      context.textAlign = 'center'

      const drawn: {
        label: EventLabelData
        x: number
        anchorY: number
        boxX: number
        boxY: number
        boxW: number
        boxH: number
      }[] = []
      let aboveRows = 0
      let belowRows = 0

      const sorted = [...this.labels].sort((a, b) => a.priority - b.priority)
      for (const label of sorted) {
        const x = timeScale.timeToCoordinate(label.time)
        if (x == null) continue
        const anchorY = label.price == null ? null : this.source.priceToCoordinate(label.price)
        if (anchorY == null) continue

        const textWidth = context.measureText(label.text).width
        const boxW = Math.min(Math.max(0, rightEdge - 2), textWidth + 12)
        if (boxW <= 0) continue
        const boxX = Math.min(Math.max(2, x - boxW / 2), Math.max(2, rightEdge - boxW))
        const boxY =
          label.side === 'below'
            ? mediaSize.height - 4 - boxH - belowRows * (boxH + rowGap)
            : 4 + aboveRows * (boxH + rowGap)
        if (label.side === 'below') belowRows += 1
        else aboveRows += 1

        drawn.push({
          label,
          x,
          anchorY,
          boxX,
          boxY,
          boxW,
          boxH,
        })
      }

      for (const d of drawn) {
        context.strokeStyle = d.label.color
        context.globalAlpha = 0.55
        context.lineWidth = 1
        context.beginPath()
        const lineFromY = d.boxY + d.boxH <= d.anchorY ? d.boxY + d.boxH : d.boxY
        context.moveTo(d.x, lineFromY)
        context.lineTo(d.x, d.anchorY)
        context.stroke()
        context.globalAlpha = 1

        context.fillStyle = d.label.color
        if (d.label.marker === 'square') {
          const halfSize = 2
          context.fillRect(d.x - halfSize, d.anchorY - halfSize, halfSize * 2, halfSize * 2)
        } else {
          context.beginPath()
          context.arc(d.x, d.anchorY, 3, 0, Math.PI * 2)
          context.fill()
        }

        context.fillStyle = 'rgba(255, 255, 255, 0.86)'
        context.strokeStyle = 'rgba(100, 116, 139, 0.55)'
        context.lineWidth = 1
        context.shadowColor = 'rgba(15, 23, 42, 0.22)'
        context.shadowBlur = 5
        context.fillRect(d.boxX, d.boxY, d.boxW, d.boxH)
        context.shadowBlur = 0
        context.strokeRect(d.boxX + 0.5, d.boxY + 0.5, d.boxW - 1, d.boxH - 1)
        context.fillStyle = d.label.color
        context.fillText(d.label.text, d.boxX + d.boxW / 2, d.boxY + d.boxH / 2 + 1)
      }
    })
  }
}

class EventLabelPaneView implements IPrimitivePaneView {
  private paneRenderer: EventLabelPaneRenderer
  constructor(chart: IChartApi, source: ISeriesApi<'Candlestick'>, labels: EventLabelData[]) {
    this.paneRenderer = new EventLabelPaneRenderer(chart, source, labels)
  }
  renderer(): IPrimitivePaneRenderer | null {
    return this.paneRenderer
  }
  zOrder(): PrimitivePaneViewZOrder {
    return 'top'
  }
}

class EventLabelPrimitive implements ISeriesPrimitive<Time> {
  private view: EventLabelPaneView
  constructor(chart: IChartApi, source: ISeriesApi<'Candlestick'>, labels: EventLabelData[]) {
    this.view = new EventLabelPaneView(chart, source, labels)
  }
  paneViews(): readonly IPrimitivePaneView[] {
    return [this.view]
  }
}

class GapPaneRenderer implements IPrimitivePaneRenderer {
  private chart: IChartApi
  private source: ISeriesApi<'Candlestick'>
  private gaps: GapRect[]
  constructor(chart: IChartApi, source: ISeriesApi<'Candlestick'>, gaps: GapRect[]) {
    this.chart = chart
    this.source = source
    this.gaps = gaps
  }
  draw(target: CanvasRenderingTarget2D) {
    if (!this.gaps.length) return
    target.useMediaCoordinateSpace((scope: MediaCoordinatesRenderingScope) => {
      const { context } = scope
      const timeScale = this.chart.timeScale()
      const priceScale = this.chart.priceScale('right')
      context.fillStyle = 'rgba(100, 116, 139, 0.16)'
      for (const gap of this.gaps) {
        const x1 = timeScale.timeToCoordinate(gap.from)
        const x2 = timeScale.timeToCoordinate(gap.to)
        const yTop = this.source.priceToCoordinate(gap.top)
        const yBottom = this.source.priceToCoordinate(gap.bottom)
        if (x1 == null || x2 == null || yTop == null || yBottom == null) continue
        const left = Math.min(x1, x2)
        const width = Math.abs(x2 - x1)
        const topY = Math.min(yTop, yBottom)
        const height = Math.abs(yTop - yBottom)
        if (height <= 0) continue
        context.fillRect(left, topY, width, height)
      }
    })
  }
}

class GapPaneView implements IPrimitivePaneView {
  private paneRenderer: GapPaneRenderer
  constructor(chart: IChartApi, source: ISeriesApi<'Candlestick'>, gaps: GapRect[]) {
    this.paneRenderer = new GapPaneRenderer(chart, source, gaps)
  }
  renderer(): IPrimitivePaneRenderer | null {
    return this.paneRenderer
  }
  zOrder(): PrimitivePaneViewZOrder {
    return 'bottom'
  }
}

class GapPrimitive implements ISeriesPrimitive<Time> {
  private view: GapPaneView
  constructor(chart: IChartApi, source: ISeriesApi<'Candlestick'>, gaps: GapRect[]) {
    this.view = new GapPaneView(chart, source, gaps)
  }
  paneViews(): readonly IPrimitivePaneView[] {
    return [this.view]
  }
}

class RolloverPaneRenderer implements IPrimitivePaneRenderer {
  private chart: IChartApi
  private times: Time[]
  constructor(chart: IChartApi, times: Time[]) {
    this.chart = chart
    this.times = times
  }
  draw(target: CanvasRenderingTarget2D) {
    if (!this.times.length) return
    target.useMediaCoordinateSpace((scope: MediaCoordinatesRenderingScope) => {
      const { context, mediaSize } = scope
      const timeScale = this.chart.timeScale()

      // 橙色粗虚线，压过白色背景和普通网格线
      context.strokeStyle = '#f97316'
      context.lineWidth = 3
      context.setLineDash([10, 6])
      context.beginPath()
      for (const t of this.times) {
        const x = timeScale.timeToCoordinate(t)
        if (x == null) continue
        context.moveTo(x, 0)
        context.lineTo(x, mediaSize.height)
      }
      context.stroke()

      context.setLineDash([])
    })
  }
}

class RolloverPaneView implements IPrimitivePaneView {
  private paneRenderer: RolloverPaneRenderer
  constructor(chart: IChartApi, times: Time[]) {
    this.paneRenderer = new RolloverPaneRenderer(chart, times)
  }
  renderer(): IPrimitivePaneRenderer | null {
    return this.paneRenderer
  }
  zOrder(): PrimitivePaneViewZOrder {
    return 'bottom'
  }
}

class RolloverPrimitive implements ISeriesPrimitive<Time> {
  private view: RolloverPaneView
  constructor(chart: IChartApi, times: Time[]) {
    this.view = new RolloverPaneView(chart, times)
  }
  paneViews(): readonly IPrimitivePaneView[] {
    return [this.view]
  }
}

/** 同一品种/级别内的缩放/平移状态：切换品种或级别时沿用同样的横向视图，避免跳变 */
let lastView: { from: number; to: number; totalAtCapture: number } | null = null
/** 最近一次写入图表的数据量：captureView 用它标记视图取自多长的数据，
 *  用于识别“数据不足被撑满”的短数据瞬态（详见 dropStaleView） */
let lastDataCount = 0

let chart: IChartApi | null = null
let candleSeries: ISeriesApi<'Candlestick'> | null = null
let volumeSeries: ISeriesApi<'Histogram'> | null = null
let resizeObserver: ResizeObserver | null = null
let countdownTimer: ReturnType<typeof setInterval> | null = null
let priceLines: IPriceLine[] = []
let extremeLines: IPriceLine[] = []
let markersApi: ISeriesMarkersPluginApi<Time> | null = null
let gapPrimitive: GapPrimitive | null = null
let rolloverPrimitive: RolloverPrimitive | null = null
let eventLabelPrimitive: EventLabelPrimitive | null = null
let focusIndex = -1
let focusFollowsLatest = true
/** 键盘或复盘定位后，鼠标移出图表时仍把十字光标留在焦点K线上 */
let focusPinnedByKeys = false
let pendingFocusTs: string | null = null
/** 鼠标悬停在历史K线时，实时行情刷新不应把焦点拉回最新K线 */
let hoveredTime: Time | null = null
let isHovering = false
let patternLines: ISeriesApi<'Line'>[] = []
let trendSeries: ISeriesApi<'Line'> | null = null
let priceExtent = 1

// 已修复：进入K线图被放大成巨大K线的根因是空图表上执行视图设置导致 barSpacing 污染（详见 git 历史）；保留修复逻辑，已移除调试面板代码

/** 进入图表时默认展示的K线根数（从最新一根往前数），由设置界面配置 */
const displayKNum = computed(() => Math.max(1, settingsStore.settings.ui.chart_display_bars))
/** 默认视图右侧留出的空白上限（以K线根数为单位），相当于把图表向左拖一段，由设置界面配置 */
const displayRightGap = computed(() => Math.max(0, settingsStore.settings.ui.chart_right_gap))
/** 右侧留白占可见K线根数的比例上限：数据量少的品种留白按此比例缩水，避免右侧出现大片空白 */
const right_gap_ratio = 0.1

/** 计算右侧留白根数：数据量足够时保持 displayRightGap 根；
 *  数据量少时按可见根数的 right_gap_ratio 缩到0，避免右侧出现大片空白 */
function rightGapBars(visible: number): number {
  if (visible <= 0) return displayRightGap.value
  return Math.min(displayRightGap.value, Math.max(0, Math.round(visible * right_gap_ratio)))
}
/** N形态连线/标记颜色：与K线自身的红绿区分开并带透明度，减少对K线的遮挡 */
const PATTERN_UP_COLOR = 'rgba(255, 135, 135, 0.9)' // 上涨：浅红
const PATTERN_DOWN_COLOR = 'rgba(32, 201, 151, 0.9)' // 下跌：青绿
const BOX_COLOR = '#ff6d00' // 箱体：亮橘色，与N形态红绿区分开
const ZOOM_SENSITIVITY = 0.0015
/** 价格轴上下留白：K线最高/最低点与图表边框之间的空隙比例 */
const PRICE_SCALE_TOP = 0.06
const PRICE_SCALE_BOTTOM = 0.06

const toTs = (s: string): UTCTimestamp =>
  Math.floor(new Date(s.replace(' ', 'T') + 'Z').getTime() / 1000) as UTCTimestamp

/** 时间浮标格式：2026-07-30 15:00 */
function formatTime(t: Time): string {
  if (typeof t === 'number') {
    const d = new Date(t * 1000)
    const p = (n: number) => String(n).padStart(2, '0')
    return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`
  }
  if (typeof t === 'string') return t
  const bd = t as { year: number; month: number; day: number }
  return `${bd.year}-${String(bd.month).padStart(2, '0')}-${String(bd.day).padStart(2, '0')}`
}

/** 周期字符串转毫秒：5m/15m/60m/1d 等 */
function timeframeMs(tf: string): number {
  if (tf === '1d') return 24 * 60 * 60 * 1000
  const m = /^(\d+)m$/.exec(tf)
  if (m) return Number(m[1]) * 60 * 1000
  const h = /^(\d+)h$/.exec(tf)
  if (h) return Number(h[1]) * 60 * 60 * 1000
  return 0
}

/** 分钟周期的当前K线结束时间：按自然日分钟网格取整，与后端桶末语义一致 */
function currentBucketEnd(date: Date, periodMs: number): Date | null {
  const minutes = periodMs / 60000
  if (!Number.isInteger(minutes) || minutes <= 0 || minutes >= 24 * 60) return null
  const elapsed = date.getHours() * 60 + date.getMinutes() + date.getSeconds() / 60
  const endMin = Math.max(1, Math.ceil(elapsed / minutes)) * minutes
  return new Date(date.getFullYear(), date.getMonth(), date.getDate(), 0, endMin, 0, 0)
}

/** 与后端一致：仅在国内期货日盘/夜盘窗口显示倒计时 */
function isTradingTime(date: Date): boolean {
  const weekday = (date.getDay() + 6) % 7 // 0=周一
  const t = date.getHours() * 60 + date.getMinutes() + date.getSeconds() / 60
  const friNight = weekday === 4 && t >= 21 * 60
  const earlySat = weekday === 5 && t < 2 * 60 + 31
  if (friNight || earlySat) return true
  if (weekday >= 5) return false
  return (
    (t >= 9 * 60 && t < 10 * 60 + 16) ||
    (t >= 10 * 60 + 30 && t < 11 * 60 + 31) ||
    (t >= 13 * 60 + 30 && t < 15 * 60 + 1) ||
    (t >= 21 * 60 && t < 23 * 60 + 31)
  )
}

/** 十字光标下的K线信息：时间 + 开高低收 */
function formatLegend(d: CandlestickData, time: Time): string {
  const up = d.close >= d.open
  const color = up ? '#e03131' : '#43BC7C'
  const item = (label: string, value: number) =>
    `<span class="lg-item"><span class="lg-label">${label}</span><span class="lg-value" style="color:${color}">${value}</span></span>`
  const trend = trendBadgeHtml()
  return `<span class="lg-time">${formatTime(time)}</span><span class="lg-sep"></span>${item('开', d.open)}${item('高', d.high)}${item('低', d.low)}${item('收', d.close)}${trend}`
}

/** 当前周期 MA20 趋势方向徽标；无数据时不显示 */
function trendBadgeHtml(): string {
  if (!trendVisible.value) return ''
  const dir = props.trendPoints?.length ? props.trendPoints[props.trendPoints.length - 1].direction : null
  if (!dir) return ''
  if (dir === 'up') {
    return `<span class="lg-sep"></span><span class="lg-trend trend-up">MA20多</span>`
  }
  if (dir === 'down') {
    return `<span class="lg-sep"></span><span class="lg-trend trend-down">MA20空</span>`
  }
  return `<span class="lg-sep"></span><span class="lg-trend trend-flat">MA20震荡</span>`
}

/** 把趋势点按时间落到当前周期每根K线，保证切换周期后仍有连续参考线 */
function buildTrendData(): { time: Time; value: number }[] {
  if (!props.trendPoints?.length) return []
  const out: { time: Time; value: number }[] = []
  let idx = 0
  for (const row of props.rows) {
    const rowTs = toTs(row.ts)
    while (idx + 1 < props.trendPoints.length && toTs(props.trendPoints[idx + 1].ts) <= rowTs) {
      idx += 1
    }
    if (idx >= props.trendPoints.length || toTs(props.trendPoints[idx].ts) > rowTs) continue
    out.push({ time: rowTs as Time, value: props.trendPoints[idx].value })
  }
  return out
}

/** 当前K线收盘倒计时：显示在时间轴上方、最新K线正下方 */
function updateCountdown() {
  const el = timeLeft.value
  if (!el) return
  if (!chart || !container.value || !props.rows.length) {
    el.style.display = 'none'
    return
  }
  const last = props.rows[props.rows.length - 1]
  const period = timeframeMs(last.timeframe || props.timeframe)
  if (period <= 0) {
    el.style.display = 'none'
    return
  }
  const now = new Date()
  const endDate = currentBucketEnd(now, period)
  // 时间戳为桶末（K线收盘时间），按当前自然分钟桶计算剩余时间；
  // 没有实时报价时也能显示，但仅限交易时段，避免休市时出现无效倒计时
  if (!endDate || !isTradingTime(now)) {
    el.style.display = 'none'
    return
  }
  const remaining = endDate.getTime() - now.getTime()
  if (remaining <= 0 || remaining > period) {
    el.style.display = 'none'
    return
  }
  const pad = (n: number) => String(n).padStart(2, '0')
  const totalSec = Math.ceil(remaining / 1000)
  const hh = Math.floor(totalSec / 3600)
  const mm = Math.floor((totalSec % 3600) / 60)
  const ss = totalSec % 60
  el.textContent = hh > 0 ? `剩余 ${pad(hh)}:${pad(mm)}:${pad(ss)}` : `剩余 ${pad(mm)}:${pad(ss)}`
  const pad2 = (n: number) => String(n).padStart(2, '0')
  const endLabel = `${endDate.getFullYear()}-${pad2(endDate.getMonth() + 1)}-${pad2(endDate.getDate())} ${pad2(endDate.getHours())}:${pad2(endDate.getMinutes())}:00`
  let x: number | null = chart.timeScale().timeToCoordinate(toTs(endLabel))
  if (x == null) {
    const lastX = chart.timeScale().timeToCoordinate(toTs(last.ts))
    if (lastX == null) {
      el.style.display = 'none'
      return
    }
    const barSpacing = chart.timeScale().options().barSpacing || 8
    x = lastX + barSpacing
  }
  if (x == null) {
    el.style.display = 'none'
    return
  }
  const width = container.value.clientWidth
  el.style.left = `${Math.min(Math.max(56, x), Math.max(56, width - 56))}px`
  el.style.bottom = `${chart.timeScale().height() + 6}px`
  el.style.display = 'block'
}

function buildCandles(): CandlestickData[] {
  return props.rows.map((r) => ({
    time: toTs(r.ts) as Time,
    open: r.open,
    high: r.high,
    low: r.low,
    close: r.close,
  }))
}

function buildVolumes(): HistogramData[] {
  return props.rows.map((r) => ({
    time: toTs(r.ts) as Time,
    value: r.volume,
    color: r.close >= r.open ? 'rgba(224, 49, 49, 0.45)' : 'rgba(15, 157, 88, 0.45)',
  }))
}

/** 当前可视区间内的最高价/最低价所在K线 */
function visibleExtremes(): { high: KlineRow | null; low: KlineRow | null } {
  if (!chart || !props.rows.length) return { high: null, low: null }
  const logical = chart.timeScale().getVisibleLogicalRange()
  const from = Math.max(0, Math.floor(logical?.from ?? 0))
  const to = Math.min(props.rows.length - 1, Math.ceil(logical?.to ?? props.rows.length - 1))
  if (to < from) return { high: null, low: null }
  let high = props.rows[from]
  let low = props.rows[from]
  for (let i = from + 1; i <= to; i++) {
    const r = props.rows[i]
    if (r.high > high.high) high = r
    if (r.low < low.low) low = r
  }
  return { high, low }
}

function buildMarkers(): SeriesMarker<Time>[] {
  const markers: SeriesMarker<Time>[] = []
  const ex = props.reviewExit
  if (props.showExtremes) {
    const { high, low } = visibleExtremes()
    if (high) {
      markers.push({
        time: toTs(high.ts),
        position: 'aboveBar',
        color: '#e03131',
        shape: 'arrowDown',
        text: '最高',
      })
    }
    if (low) {
      markers.push({
        time: toTs(low.ts),
        position: 'belowBar',
        color: '#0f9d58',
        shape: 'arrowUp',
        text: '最低',
      })
    }
  }
  for (const s of props.signals) {
    const color = s.direction === 'up' ? PATTERN_UP_COLOR : PATTERN_DOWN_COLOR
    if (s.level === 'box') {
      if (s.warning_ts) {
        markers.push({
          time: toTs(s.warning_ts),
          position: s.direction === 'up' ? 'belowBar' : 'aboveBar',
          color: BOX_COLOR,
          shape: 'circle',
          size: 0.5,
          text: 'BOX',
        })
      }
      if (s.trigger_ts) {
        markers.push({
          time: toTs(s.trigger_ts),
          position: s.direction === 'up' ? 'belowBar' : 'aboveBar',
          color: '#e53935',
          shape: 'arrowDown',
          text: '触发',
        })
      }
      continue
    }
    markers.push({
      time: toTs(s.s0.ts),
      position: 'belowBar',
      color,
      shape: 'arrowUp',
    })
    markers.push({
      time: toTs(s.s1.ts),
      position: s.direction === 'up' ? 'belowBar' : 'aboveBar',
      color,
      shape: 'circle',
      size: 0.5,
    })
    if (s.warning_ts) {
      markers.push({
        time: toTs(s.warning_ts),
        position: 'aboveBar',
        color: '#f9a825',
        shape: 'circle',
        size: 0.5,
      })
    }
    if (s.trigger_ts) {
      markers.push({
        time: toTs(s.trigger_ts),
        position: 'aboveBar',
        color: '#e53935',
        shape: 'arrowDown',
      })
    }
  }
  if (ex?.ts) {
    markers.push({
      time: toTs(ex.ts),
      position: 'aboveBar',
      color: '#7c3aed',
      shape: 'circle',
    })
  }
  return markers
}

function syncPriceLines() {
  if (!candleSeries) return
  for (const line of priceLines) candleSeries.removePriceLine(line)
  priceLines = []
  const lines: { price: number; color: string; title: string }[] = []
  for (const s of props.signals) {
    if (s.entry > 0) lines.push({ price: s.entry, color: '#1565c0', title: '入场' })
    if (s.stop > 0) lines.push({ price: s.stop, color: '#e53935', title: '止损' })
    if (s.target > 0) lines.push({ price: s.target, color: '#2e7d32', title: '目标' })
  }
  const ex = props.reviewExit
  if (ex?.price && ex.price > 0) {
    lines.push({ price: ex.price, color: '#7c3aed', title: '出场' })
  }
  priceLines = lines.map((l) =>
    candleSeries!.createPriceLine({
      price: l.price,
      color: l.color,
      lineWidth: 1,
      lineStyle: 2,
      axisLabelVisible: true,
      title: l.title,
    }),
  )
}

/** 在当前可视区间画出最高价/最低价虚线 */
function syncExtremes() {
  if (!candleSeries) return
  for (const line of extremeLines) candleSeries.removePriceLine(line)
  extremeLines = []
  if (!props.showExtremes) return
  const { high, low } = visibleExtremes()
  const lines: { price: number; color: string; title: string }[] = []
  if (high) lines.push({ price: high.high, color: '#e03131', title: '最高' })
  if (low) lines.push({ price: low.low, color: '#0f9d58', title: '最低' })
  extremeLines = lines.map((l) =>
    candleSeries!.createPriceLine({
      price: l.price,
      color: l.color,
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      axisLabelVisible: true,
      title: l.title,
    }),
  )
}

/** 可视区间变化时重算最高/最低点 */
function onVisibleRangeChange() {
  syncFocusFollowWithView()
  if (!props.showExtremes) return
  syncExtremes()
  markersApi?.setMarkers(buildMarkers())
}

/** 画出每个N形态的 S0→S1→S2 连线；箱体只画上下轨横线 */
function syncPatternLines() {
  if (!chart) return
  for (const line of patternLines) chart.removeSeries(line)
  patternLines = []
  const seenBoxRails = new Set<string>()
  for (const sig of props.signals) {
    if (sig.level === 'box') {
      if (!sig.box) continue
      const addRail = (price: number) => {
        const key = `${price}|${sig.box!.first_ts}|${sig.box!.last_ts}`
        if (seenBoxRails.has(key)) return
        seenBoxRails.add(key)
        const line = chart!.addSeries(LineSeries, {
          color: BOX_COLOR,
          lineWidth: 1,
          lineStyle: LineStyle.Solid,
          lastValueVisible: false,
          priceLineVisible: false,
          crosshairMarkerVisible: false,
        })
        line.setData([
          { time: toTs(sig.box!.first_ts) as Time, value: price },
          { time: toTs(sig.box!.last_ts) as Time, value: price },
        ])
        patternLines.push(line)
      }
      addRail(sig.box.upper)
      addRail(sig.box.lower)
      continue
    }
    const pts = [sig.s0, sig.s1, sig.s2]
    if (pts.some((p) => !p.ts || p.price <= 0)) continue
    const line = chart.addSeries(LineSeries, {
      color: sig.direction === 'up' ? PATTERN_UP_COLOR : PATTERN_DOWN_COLOR,
      lineWidth: 1,
      lineStyle: LineStyle.Solid,
      lastValueVisible: false,
      priceLineVisible: false,
      crosshairMarkerVisible: false,
    })
    line.setData(pts.map((p) => ({ time: toTs(p.ts) as Time, value: p.price })))
    patternLines.push(line)
  }
}

/** 重建当前周期 MA20 长期趋势线（独立 series，不混入 N 形态连线） */
function syncTrendSeries() {
  if (!chart) return
  if (trendSeries) {
    chart.removeSeries(trendSeries)
    trendSeries = null
  }
  if (!trendVisible.value) return
  const data = buildTrendData()
  if (!data.length) return
  trendSeries = chart.addSeries(LineSeries, {
    color: 'rgba(37, 99, 235, 0.85)',
    lineWidth: 1,
    lineStyle: LineStyle.Solid,
    lastValueVisible: true,
    priceLineVisible: false,
    crosshairMarkerVisible: false,
  })
  trendSeries.setData(data)
}

function applyDefaultView() {
  if (!chart) return
  // 空图表（数据尚未写入）上设置视图会污染时间轴的间距状态，等数据就位后再校准
  if (!candleSeries || candleSeries.data().length === 0) return
  const total = props.rows.length
  if (total === 0) return
  const visible = Math.min(displayKNum.value, total)
  // 右边界放在最后一根K线右侧留出空白（最后一根K线中心在逻辑坐标 total-1 处）
  const to = total - 0.5 + rightGapBars(visible)
  const from = Math.max(-0.5, to - visible)
  chart.timeScale().setVisibleLogicalRange({ from, to })
  clampMinBarSpacing({ from, to })
}

/** 兜底：若K线间距小于 MIN_BAR_SPACING，收窄可见范围直到间距达标（右边缘不动）。
 *  pendingRange：刚调用 setVisibleLogicalRange 请求、尚未生效的范围。
 *  必须基于“待应用的范围”而不是 setVisibleLogicalRange 之后立刻读取的旧范围，
 *  否则旧范围（无右侧留白）会覆盖掉刚请求的带留白范围，导致右侧留白失效。 */
function clampMinBarSpacing(pendingRange?: { from: number; to: number }) {
  if (!chart || !container.value) return
  if (!candleSeries || candleSeries.data().length === 0) return
  const width = container.value.clientWidth
  const total = props.rows.length
  if (!width || total <= 0) return
  const ts = chart.timeScale()
  const logical = pendingRange ?? ts.getVisibleLogicalRange()
  if (!logical || ts.options().barSpacing >= minBarSpacing.value) return
  const maxSpan = width / minBarSpacing.value
  const span = logical.to - logical.from
  if (span <= maxSpan) return
  const from = Math.max(-0.5, logical.to - maxSpan)
  ts.setVisibleLogicalRange({ from, to: logical.to })
}

function updatePriceExtent() {
  if (!props.rows.length) {
    priceExtent = 1
    return
  }
  let hi = -Infinity
  let lo = Infinity
  for (const r of props.rows) {
    if (r.high > hi) hi = r.high
    if (r.low < lo) lo = r.low
  }
  priceExtent = Math.max(1e-9, hi - lo)
}

/** 分配窗格高度：蜡烛图 78%，成交量 22% */
function applyPaneHeights() {
  if (!chart || !container.value) return
  const h = container.value.clientHeight
  if (h <= 0) return
  const panes = chart.panes()
  if (panes.length >= 2) {
    panes[0].setHeight(Math.round(h * 0.78))
    panes[1].setHeight(Math.max(40, Math.round(h * 0.22)))
  }
}

/** 保存当前视图作为全局缩放状态 */
function captureView() {
  if (!chart || !props.rows.length) return
  const logical = chart.timeScale().getVisibleLogicalRange()
  if (!logical) return
  lastView = { from: logical.from, to: logical.to, totalAtCapture: lastDataCount }
}

/** 丢弃短数据瞬态留下的缩放状态：
 *  保存视图时数据量不足默认显示根数、且视图覆盖了当时全部K线（被“撑满”），
 *  而现在数据明显变多——说明那是临时短数据（品种切换中间态/初始数据很少等）。
 *  若继续沿用该跨度，图表会卡在“几根巨大K线”的放大状态。此时回到默认视图。 */
function dropStaleView(total: number) {
  if (!lastView) return
  const span = lastView.to - lastView.from
  const capturedTotal = lastView.totalAtCapture
  if (
    capturedTotal > 0 &&
    capturedTotal < displayKNum.value &&
    span >= capturedTotal - 0.5 &&
    total > capturedTotal
  ) {
    lastView = null
  }
}

/** 把全局视图套用到当前数据：优先保持原窗口位置（含右侧空白），数据不足时贴右端显示同样数量的K线 */
function restoreView() {
  if (!chart) return
  if (!candleSeries || candleSeries.data().length === 0) return
  const priceApi = chart.priceScale('right')
  //   `== 恢复视图: 行${props.rows.length} 保存${lastView ? lastView.from.toFixed(2) + '~' + lastView.to.toFixed(2) : 'null'}`,
  // )
  dropStaleView(props.rows.length)
  if (!lastView) {
    priceApi.setAutoScale(true)
    applyDefaultView()
    return
  }
  const total = props.rows.length
  const span = lastView.to - lastView.from
  let from = lastView.from
  let to = lastView.to
  if (total > 0) {
    const maxTo = total - 0.5 + rightGapBars(Math.min(span, total))
    if (span >= total) {
      to = maxTo
      from = Math.max(-0.5, to - span)
    } else {
      from = Math.max(-0.5, Math.min(maxTo - span, from))
      to = from + span
    }
  }
  chart.timeScale().setVisibleLogicalRange({ from, to })
  clampMinBarSpacing({ from, to })
  // 纵轴自动适配新品种的价格区间，避免因价格水平不同导致画面空白
  priceApi.setAutoScale(true)
}

/** 切换品种/周期时：保留当前缩放级别（可见K线根数），但视图贴到新数据最右端并留出右侧空白 */
function applySwitchView(span: number) {
  if (!chart) return
  if (!candleSeries || candleSeries.data().length === 0) return
  const total = props.rows.length
  if (total === 0) return
  const visible = Math.min(span, total)
  const to = total - 0.5 + rightGapBars(visible)
  const from = Math.max(-0.5, to - span)
  chart.timeScale().setVisibleLogicalRange({ from, to })
  clampMinBarSpacing({ from, to })
  // 纵轴自动适配新品种的价格区间，避免因价格水平不同导致画面空白
  chart.priceScale('right').setAutoScale(true)
}

function focusRow(): KlineRow | null {
  return focusIndex >= 0 && focusIndex < props.rows.length ? props.rows[focusIndex] : null
}

/** 图例显示当前焦点K线的开高低收 */
function renderFocusLegend() {
  if (!legend.value) return
  const row = focusRow()
  if (!row) {
    legend.value.innerHTML = 'N趋势 K线'
    return
  }
  const time = toTs(row.ts) as Time
  legend.value.innerHTML = formatLegend(
    { time, open: row.open, high: row.high, low: row.low, close: row.close },
    time,
  )
}

/** 更新焦点K线的十字光标与图例；数据尚未就绪时清除光标 */
function syncFocus() {
  if (!chart || !candleSeries) return
  const row = focusRow()
  if (!row) {
    chart.clearCrosshairPosition()
    renderFocusLegend()
    return
  }
  chart.setCrosshairPosition(row.close, toTs(row.ts) as Time, candleSeries)
  renderFocusLegend()
}

/** 焦点到可视区边缘时，让画面自动跟随一根，保持焦点K线可见 */
function ensureFocusVisible() {
  if (!chart) return
  if (focusIndex < 0 || focusIndex >= props.rows.length) return
  const ts = chart.timeScale()
  const logical = ts.getVisibleLogicalRange()
  if (!logical) return
  const total = props.rows.length
  const span = logical.to - logical.from
  const maxTo = total - 0.5 + rightGapBars(Math.min(span, total))
  let from = Number(logical.from)
  let to = Number(logical.to)
  if (focusIndex < logical.from) {
    to = Math.min(maxTo, logical.to - (logical.from - focusIndex))
    from = Math.max(-0.5, to - span)
  } else if (focusIndex > logical.to) {
    from = Math.min(maxTo - span, logical.from + (focusIndex - logical.to))
    to = from + span
  } else {
    return
  }
  ts.setVisibleLogicalRange({ from, to })
}

/** 手动拖动/缩放离开最新K线后停止自动跟随；回到最新区域时再恢复跟随 */
function syncFocusFollowWithView() {
  if (!chart || props.rows.length === 0) return
  if (focusIndex !== props.rows.length - 1) return
  const logical = chart.timeScale().getVisibleLogicalRange()
  if (!logical) return
  focusFollowsLatest = Number(logical.to) >= props.rows.length - 1
}

/** 定位复盘形态：优先精确匹配，匹配不到时取时间上最接近的K线 */
function nearestRowIndex(ts: string): number {
  const target = toTs(ts)
  let best = -1
  let bestDiff = Number.POSITIVE_INFINITY
  for (let i = 0; i < props.rows.length; i++) {
    const diff = Math.abs(toTs(props.rows[i].ts) - target)
    if (diff < bestDiff) {
      bestDiff = diff
      best = i
    }
  }
  return best
}

/** 把目标K线放到可视区中央，保留当前缩放级别；开启“右侧聚焦”时则将目标置于最右侧（无后视） */
function centerFocusView(index: number) {
  if (!chart || props.rows.length === 0) return
  const ts = chart.timeScale()
  const logical = ts.getVisibleLogicalRange()
  const total = props.rows.length
  const span = Math.max(1, Math.min(logical ? Number(logical.to - logical.from) : displayKNum.value, total))
  const maxTo = total - 0.5 + rightGapBars(Math.min(span, total))
  let from: number
  let to: number
  if (settingsStore.settings.ui.chart_review_focus_right) {
    const gap = rightGapBars(Math.min(span, total))
    const rightMaxTo = total - 0.5 + gap + 2
    to = index + 0.5 + gap + 2
    from = to - span
    if (from < -0.5) {
      from = -0.5
      to = from + span
    }
    if (to > rightMaxTo) {
      to = rightMaxTo
      from = Math.max(-0.5, to - span)
    }
  } else {
    const half = span / 2
    from = Math.max(-0.5, index - half)
    to = Math.min(maxTo, index + half)
    if (to - from < span - 1e-6) {
      if (from <= -0.5 + 1e-6) to = from + span
      else from = to - span
    }
  }
  ts.setVisibleLogicalRange({ from, to })
  clampMinBarSpacing({ from, to })
  lastView = { from, to, totalAtCapture: total }
}

/** 复盘模式自动定位：数据未就绪时先记下，等K线到位后再聚焦；带重试避免时序竞态（50%不聚焦） */
let focusRetryTimer: ReturnType<typeof setTimeout> | null = null
let focusRetryCount = 0
function tryApplyPendingFocus(): boolean {
  if (!pendingFocusTs || !chart || props.rows.length === 0) return false
  if (!rowsMatchRequest()) {
    const t = toTs(pendingFocusTs)
    const firstTs = props.rows.length ? toTs(props.rows[0].ts) : t
    const lastTs = props.rows.length ? toTs(props.rows[props.rows.length-1].ts) : t
    if (t < firstTs - 7*24*3600 || t > lastTs + 7*24*3600) {
      return false
    }
  }
  const idx = nearestRowIndex(pendingFocusTs)
  if (idx < 0) return false
  pendingFocusTs = null
  focusRetryCount = 0
  focusIndex = idx
  focusFollowsLatest = false
  focusPinnedByKeys = true
  syncFocus()
  centerFocusView(focusIndex)
  nextTick(() => {
    if (focusIndex === idx && chart) {
      centerFocusView(focusIndex)
      const lr = chart!.timeScale().getVisibleLogicalRange()
      if (lr && (idx < lr.from || idx > lr.to)) {
        centerFocusView(focusIndex)
      }
    }
  })
  return true
}
function scheduleFocusRetry() {
  if (focusRetryTimer) clearTimeout(focusRetryTimer)
  if (focusRetryCount >= 50) {
    focusRetryCount = 0
    return
  }
  focusRetryCount++
  focusRetryTimer = setTimeout(() => {
    focusRetryTimer = null
    if (!pendingFocusTs) return
    if (tryApplyPendingFocus()) return
    scheduleFocusRetry()
  }, 60)
}
function focusAtTs(ts: string | null) {
  pendingFocusTs = ts || null
  focusRetryCount = 0
  if (!pendingFocusTs) {
    if (focusRetryTimer) { clearTimeout(focusRetryTimer); focusRetryTimer = null }
    return
  }
  if (tryApplyPendingFocus()) {
    if (focusRetryTimer) { clearTimeout(focusRetryTimer); focusRetryTimer = null }
    return
  }
  scheduleFocusRetry()
}
function stepCandles(dir: number) {
  if (!chart) return
  if (!candleSeries || candleSeries.data().length === 0) return
  if (props.rows.length <= 0) return
  if (focusIndex < 0 || focusIndex >= props.rows.length) focusIndex = props.rows.length - 1
  const next = focusIndex + (dir < 0 ? -1 : 1)
  if (next < 0 || next >= props.rows.length) return
  focusIndex = next
  focusFollowsLatest = false
  focusPinnedByKeys = true
  syncFocus()
  ensureFocusVisible()
}

/** 相邻K线时间间隔超过该分钟数视为交易时段断裂（午休、日盘/夜盘切换、周末），
 *  跨时段的“缺口”是正常停盘，不是价格缺口，不画矩形 */
const SESSION_BREAK_MIN = 60

/** 忽略的微小缺口阈值：缺口高度小于 max(2 点, 价格的 0.1%) 不画，避免噪声 */
function isSignificantGap(bottom: number, size: number): boolean {
  return size >= Math.max(2, bottom * 0.001)
}

/** 相邻两根K线的时间间隔（分钟），用于判断是否同一交易时段 */
function tsDiffMinutes(a: string, b: string): number {
  return (new Date(b.replace(' ', 'T') + 'Z').getTime() - new Date(a.replace(' ', 'T') + 'Z').getTime()) / 60000
}

/** 找出价格缺口（同一交易时段内相邻K线价格区间不衔接），矩形从缺口处向右延伸，
 *  直到有K线重新进入该价格带（缺口回补）为止；未回补的延伸到数据最右端 */
function computeGaps(): GapRect[] {
  const gaps: GapRect[] = []
  const n = props.rows.length
  for (let i = 1; i < n; i++) {
    const prev = props.rows[i - 1]
    const cur = props.rows[i]
    // 跨交易时段（午休/日盘夜盘切换/周末）的间隔不算缺口
    if (tsDiffMinutes(prev.ts, cur.ts) > SESSION_BREAK_MIN) continue
    let top: number
    let bottom: number
    if (cur.low > prev.high) {
      // 向上缺口：空隙为 前一根高点 ~ 当前低点
      top = cur.low
      bottom = prev.high
    } else if (cur.high < prev.low) {
      // 向下缺口：空隙为 当前高点 ~ 前一根低点
      top = prev.low
      bottom = cur.high
    } else {
      continue
    }
    if (!isSignificantGap(bottom, top - bottom)) continue
    // 向右延伸：直到某根K线的价格带重新进入缺口区间为止
    let end = i
    for (let j = i; j < n; j++) {
      const bar = props.rows[j]
      if (bar.low < top && bar.high > bottom) {
        break
      }
      end = j
    }
    gaps.push({
      from: toTs(prev.ts) as Time,
      to: toTs(props.rows[end].ts) as Time,
      top,
      bottom,
    })
  }
  return gaps
}

/** 重建跳空灰底图层 */
function syncGaps() {
  if (!chart || !candleSeries) return
  if (gapPrimitive) {
    candleSeries.detachPrimitive(gapPrimitive)
    gapPrimitive = null
  }
  const gaps = computeGaps()
  if (!gaps.length) return
  gapPrimitive = new GapPrimitive(chart, candleSeries, gaps)
  candleSeries.attachPrimitive(gapPrimitive)
}

function computeRollovers(): Time[] {
  return props.rows.filter((r) => !!r.rollover).map((r) => toTs(r.ts) as Time)
}

/** 重建换月竖线图层 */
function syncRollovers() {
  if (!chart || !candleSeries) return
  if (rolloverPrimitive) {
    candleSeries.detachPrimitive(rolloverPrimitive)
    rolloverPrimitive = null
  }
  const times = computeRollovers()
  if (!times.length) return
  rolloverPrimitive = new RolloverPrimitive(chart, times)
  candleSeries.attachPrimitive(rolloverPrimitive)
}

function rowAt(ts: string): KlineRow | undefined {
  const target = toTs(ts)
  return props.rows.find((r) => toTs(r.ts) === target)
}

/** 事件文字统一走带避让的标签层，避免S0/S1/S2与预警/触发/出场互相压住 */
function buildEventLabels(): EventLabelData[] {
  const labels: EventLabelData[] = []
  const ex = props.reviewExit
  const seenBoxLabels = new Set<string>()
  for (const s of props.signals) {
    const color = s.direction === 'up' ? '#d64545' : '#0e9f6e'
    const swingSide = (isHigh: boolean): 'above' | 'below' => (isHigh ? 'above' : 'below')
    if (s.level === 'box' && s.box) {
      const boxKey = `${s.box.upper}|${s.box.lower}|${s.box.first_ts}|${s.box.last_ts}`
      if (!seenBoxLabels.has(boxKey)) {
        seenBoxLabels.add(boxKey)
        labels.push({
          time: toTs(s.box.first_ts),
          text: '下轨',
          color: BOX_COLOR,
          price: s.box.lower,
          priority: 0,
          side: 'below',
        })
        labels.push({
          time: toTs(s.box.last_ts),
          text: '上轨',
          color: BOX_COLOR,
          price: s.box.upper,
          priority: 0,
          side: 'above',
        })
      }
    } else if (s.level !== 'box') {
      labels.push({
        time: toTs(s.s0.ts),
        text: 'S0',
        color,
        price: s.s0.price,
        priority: 0,
        side: swingSide(s.s0.is_high),
      })
      labels.push({
        time: toTs(s.s1.ts),
        text: 'S1',
        color,
        price: s.s1.price,
        priority: 0,
        side: swingSide(s.s1.is_high),
      })
      labels.push({
        time: toTs(s.s2.ts),
        text: 'S2',
        color,
        price: s.s2.price,
        priority: 0,
        side: swingSide(s.s2.is_high),
        marker: 'square',
      })
    }

    if (s.warning_ts) {
      const row = rowAt(s.warning_ts)
      if (row) {
        labels.push({
          time: toTs(s.warning_ts),
          text: s.level === 'box' ? '箱体预警' : '预警',
          color: s.level === 'box' ? BOX_COLOR : '#b45309',
          price: s.direction === 'up' ? row.low : row.high,
          priority: 1,
          side: s.direction === 'up' ? 'below' : 'above',
        })
      }
    }

    const triggerTs = s.trigger_ts
    if (triggerTs) {
      const row = rowAt(triggerTs)
      if (row) {
        labels.push({
          time: toTs(triggerTs),
          text: '触发',
          color: '#c0392b',
          price: s.direction === 'up' ? row.high : row.low,
          priority: 2,
          side: s.direction === 'up' ? 'above' : 'below',
        })
      }
    }

    if (s.trigger_ts && s.vol_ratio != null && s.vol_confirmed) {
      const row = rowAt(s.trigger_ts)
      if (row) {
        labels.push({
          time: toTs(s.trigger_ts),
          text: `量能${s.vol_ratio.toFixed(1)}×`,
          color: '#0f766e',
          price: row.high,
          priority: 3,
          side: 'above',
        })
      }
    }
  }

  // 单K裸K独立提醒：锤/针 在下一根K线上方/下方打点 priority 5
  const sbList = props.singleBars
  if (sbList?.length) {
    const now = Date.now()
    for (const sb of sbList) {
      if (!sb || sb.timeframe !== "15m") continue
      if (now > sb.expireTime) continue
      const nextMs = sb.triggerTime + 15 * 60 * 1000
      const row = rowAt(new Date(nextMs).toISOString().slice(0, 16).replace("T", " ") + ":00") || props.rows[props.rows.length - 1]
      const time = (nextMs / 1000) as Time
      const isHammer = sb.kind === "hammer"
      labels.push({
        time,
        text: isHammer ? "锤" : "针",
        color: SINGLE_BAR_COLORS[sb.kind].chart,
        price: isHammer ? (row ? row.low : null) : (row ? row.high : null),
        priority: 5,
        side: isHammer ? "below" : "above",
      })
    }
  }

  if (ex?.ts && ex.price != null && ex.price > 0) {
    const rText = ex.r == null ? '' : ` ${ex.r >= 0 ? '+' : ''}${ex.r.toFixed(2)}R`
    labels.push({
      time: toTs(ex.ts),
      text: `出场${rText}`,
      color: '#6d28d9',
      price: ex.price,
      priority: 4,
      side: 'above',
    })
  }
  return labels
}

watch(() => props.singleBars, () => { syncEventLabels() })

function syncEventLabels() {
  if (!chart || !candleSeries) return
  if (eventLabelPrimitive) {
    candleSeries.detachPrimitive(eventLabelPrimitive)
    eventLabelPrimitive = null
  }
  const labels = buildEventLabels()
  if (!labels.length) return
  const aboveCount = labels.filter((l) => l.side === 'above').length
  const belowCount = labels.filter((l) => l.side === 'below').length
  chart.priceScale('right').applyOptions({
    scaleMargins: {
      top: Math.min(0.2, Math.max(PRICE_SCALE_TOP, 0.04 + aboveCount * 0.02)),
      bottom: Math.min(0.2, Math.max(PRICE_SCALE_BOTTOM, 0.04 + belowCount * 0.02)),
    },
  })
  eventLabelPrimitive = new EventLabelPrimitive(chart, candleSeries, labels)
  candleSeries.attachPrimitive(eventLabelPrimitive)
}

let prevSymbol: string | null = null
let prevTimeframe: string | null = null

/** 当前 rows 是否真的属于 props 指定的品种/周期。
 *  切换品种/周期时后端尚未返回新数据前，组件会先收到「旧数据+新品种」的中间态，
 *  用它判断可以避免中间态提前消耗掉切换状态、导致新数据到达后被当成普通刷新 */
function rowsMatchRequest(): boolean {
  const first = props.rows[0]
  if (!first || first.timeframe !== props.timeframe) return false
  if (first.symbol === props.symbol) return true
  // 换月/连续合约：品种字母前缀相同即视为同一品种（如 MA001 vs MA、MA2409 vs MA001）
  // 避免因合约月份后缀变化导致 rows 被判为中间态而跳过渲染，进而导致换月后必不聚焦
  const a = first.symbol.replace(/[^A-Za-z]/g, '')
  const b = props.symbol.replace(/[^A-Za-z]/g, '')
  return !!a && !!b && a === b
}

/** 数据变化时：沿用当前缩放/平移状态（无记录则默认视图）；
 *  切换品种/周期则贴到新数据最右端并留出右侧空白，避免最新K线紧贴右边界。
 *  数据与请求的品种/周期不一致（切换中间态）时跳过视图处理，等新数据到达再切换 */
function renderData() {
  if (!chart || !candleSeries || !volumeSeries) return
  if (!rowsMatchRequest()) {
    //   `== 跳过渲染: 行${props.rows.length} 首行${props.rows[0] ? props.rows[0].symbol + '/' + props.rows[0].timeframe : '-'} 请求${props.symbol}/${props.timeframe}`,
    // )
    return
  }
  const isSwitch = prevSymbol !== props.symbol || prevTimeframe !== props.timeframe
  prevSymbol = props.symbol
  prevTimeframe = props.timeframe
  captureView()
  updatePriceExtent()
  candleSeries.setData(buildCandles())
  volumeSeries.setData(buildVolumes())
  lastDataCount = props.rows.length
  syncGaps()
  syncRollovers()
  if (isSwitch) {
    dropStaleView(props.rows.length)
    const span = lastView
      ? Math.min(lastView.to - lastView.from, props.rows.length)
      : Math.min(displayKNum.value, props.rows.length)
    applySwitchView(span)
  } else {
    restoreView()
  }
  const lastTsForHover = props.rows.length ? (toTs(props.rows[props.rows.length - 1].ts) as Time) : null
  const hoveringOnHistory = isHovering && hoveredTime != null && lastTsForHover != null && hoveredTime !== lastTsForHover
  const shouldAutoFollow = focusFollowsLatest && !hoveringOnHistory
  if (isSwitch) {
    focusIndex = props.rows.length - 1
    focusFollowsLatest = true
  } else if (shouldAutoFollow) {
    focusIndex = props.rows.length - 1
  } else if (focusIndex < 0 || focusIndex >= props.rows.length) {
    focusIndex = props.rows.length - 1
  }
  let focusNeedsCenter = false
  let focusIdxForCenter = -1
  if (pendingFocusTs) {
    const idx = nearestRowIndex(pendingFocusTs)
    if (idx >= 0) {
      pendingFocusTs = null
      focusRetryCount = 0
      if (focusRetryTimer) { clearTimeout(focusRetryTimer); focusRetryTimer = null }
      focusIndex = idx
      focusFollowsLatest = false
      focusPinnedByKeys = true
      focusNeedsCenter = true
      focusIdxForCenter = idx
    }
  }
  if (hoveringOnHistory) {
    const hoveredRow = hoveredTime != null ? props.rows.find((r) => (toTs(r.ts) as Time) === hoveredTime) : undefined
    if (hoveredRow && hoveredTime != null) {
      if (legend.value) {
        legend.value.innerHTML = formatLegend({ time: hoveredTime, open: hoveredRow.open, high: hoveredRow.high, low: hoveredRow.low, close: hoveredRow.close } as CandlestickData, hoveredTime)
      }
      chart.setCrosshairPosition(hoveredRow.close, hoveredTime, candleSeries)
    } else {
      syncFocus()
    }
  } else {
    syncFocus()
  }
  if (focusNeedsCenter) {
    centerFocusView(focusIdxForCenter)
    const savedIdx = focusIdxForCenter
    nextTick(() => {
      if (focusIndex === savedIdx && chart) centerFocusView(savedIdx)
    })
  } else if (shouldAutoFollow) ensureFocusVisible()
  else if (pendingFocusTs) {
    // 数据已就绪但仍有未消耗的 pending（极端时序），再约一次
    scheduleFocusRetry()
  }
}

/** 只刷新形态标注与价位线，保留当前缩放/平移状态 */
function renderOverlays() {
  if (!chart || !candleSeries) return
  markersApi?.setMarkers(buildMarkers())
  syncPriceLines()
  syncExtremes()
  syncPatternLines()
  syncTrendSeries()
  syncEventLabels()
}

/** Ctrl+滚轮缩放：向上(deltaY<0)放大、向下(deltaY>0)缩小；时间轴与价格轴按光标位置同步缩放。
 *  普通滚轮不在此处理，交给父容器切换品种。 */
function handleWheel(e: WheelEvent) {
  if (!e.ctrlKey) return
  if (!chart || !container.value) return
  e.preventDefault()
  const rect = container.value.getBoundingClientRect()
  const width = rect.width || 1
  const height = rect.height || 1
  const xRatio = Math.min(1, Math.max(0, (e.clientX - rect.left) / width))
  const yRatio = Math.min(1, Math.max(0, (e.clientY - rect.top) / height))

  let dy = e.deltaY
  if (e.deltaMode === 1) dy *= 16
  else if (e.deltaMode === 2) dy *= height
  // 向上滚：deltaY<0 -> factor<1 -> 可见范围变小 = 放大
  const factor = Math.exp(dy * ZOOM_SENSITIVITY)

  const timeScale = chart.timeScale()
  const logical = timeScale.getVisibleLogicalRange()
  const total = props.rows.length
  if (!logical || total <= 0) return

  const minSpan = 1
  const maxSpan = total
  const requested = (logical.to - logical.from) * factor
  const span = Math.min(maxSpan, Math.max(minSpan, requested))
  const clamped = requested < minSpan || requested > maxSpan
  const center = logical.from + (logical.to - logical.from) * xRatio
  let from = center - span * xRatio
  let to = from + span
  from = Math.max(-0.5, from)
  to = Math.min(total - 0.5, to)
  if (to - from < minSpan) {
    from = Math.max(-0.5, to - minSpan)
  }
  timeScale.setVisibleLogicalRange({ from, to })

  // 横轴已到极限时纵轴同步停止，避免无限缩放
  if (clamped) return

  const priceApi = chart.priceScale('right')
  const pr = priceApi.getVisibleRange()
  if (!pr || pr.to <= pr.from || priceExtent <= 0) return
  const minP = priceExtent * 0.002
  const maxP = priceExtent * 1.2
  const spanP = Math.min(maxP, Math.max(minP, (pr.to - pr.from) * factor))
  const centerP = pr.to - (pr.to - pr.from) * yRatio
  const fromP = centerP - spanP * (1 - yRatio)
  priceApi.setAutoScale(false)
  priceApi.setVisibleRange({ from: fromP, to: fromP + spanP })
}

onMounted(() => {
  if (!container.value) return
  // 每次进入图表页先回到默认视图（最新 displayKNum 根）；页内切换品种/级别沿用缩放
  //     `行${props.rows.length} 首行${props.rows[0] ? props.rows[0].symbol + '/' + props.rows[0].timeframe : '-'} ` +
  //     `请求${props.symbol}/${props.timeframe} 匹配${rowsMatchRequest() ? 'Y' : 'N'} 旧图${chart ? '有' : '无'}`,
  // )
  lastView = null
  lastDataCount = 0
  prevSymbol = null
  prevTimeframe = null
  focusIndex = -1
  focusFollowsLatest = true
  focusPinnedByKeys = false
  hoveredTime = null
  isHovering = false
  chart = createChart(container.value, {
    layout: {
      background: { type: ColorType.Solid, color: '#ffffff' },
      textColor: '#64748b',
    },
    grid: {
      vertLines: { color: 'rgba(226, 232, 240, 0.6)' },
      horzLines: { color: 'rgba(226, 232, 240, 0.6)' },
    },
    rightPriceScale: { borderColor: 'rgba(197, 203, 215, 0.4)' },
    timeScale: {
      borderColor: 'rgba(197, 203, 215, 0.4)',
      timeVisible: true,
      secondsVisible: false,
    },
    crosshair: { mode: CrosshairMode.Normal },
    localization: {
      timeFormatter: (time: Time) => formatTime(time),
    },
    handleScroll: {
      mouseWheel: false,
      pressedMouseMove: true,
      horzTouchDrag: true,
      vertTouchDrag: true,
    },
    handleScale: {
      mouseWheel: false,
      pinch: true,
      axisPressedMouseMove: { time: true, price: true },
      axisDoubleClickReset: true,
    },
  })
  candleSeries = chart.addSeries(CandlestickSeries, {
    // 红涨空心：实体透明只留红色描边；绿跌实心
    upColor: 'rgba(224, 49, 49, 0)',
    downColor: '#43BC7C',
    borderVisible: true,
    borderUpColor: '#e03131',
    borderDownColor: '#43BC7C',
    wickUpColor: '#e03131',
    wickDownColor: '#43BC7C',
  })
  markersApi = createSeriesMarkers(candleSeries, [])
  // 成交量柱状图放在独立窗格（pane 1），与K线物理分离，放大不会互相压到
  volumeSeries = chart.addSeries(
    HistogramSeries,
    { priceFormat: { type: 'volume' }, priceScaleId: 'vol' },
    1,
  )
  // 默认 6% 上下留白；事件文字标签较多时会由 syncEventLabels 动态放宽
  chart.priceScale('right').applyOptions({
    scaleMargins: { top: PRICE_SCALE_TOP, bottom: PRICE_SCALE_BOTTOM },
  })
  chart.priceScale('vol', 1).applyOptions({ scaleMargins: { top: 0.08, bottom: 0.04 } })
  applyPaneHeights()
  chart.timeScale().subscribeVisibleLogicalRangeChange(onVisibleRangeChange)

  chart.subscribeCrosshairMove((param) => {
    if (!legend.value || !candleSeries) return
    if (!param.time || !param.point) {
      isHovering = false
      hoveredTime = null
      if (focusPinnedByKeys) syncFocus()
      else renderFocusLegend()
      return
    }
    const d = param.seriesData.get(candleSeries) as CandlestickData | undefined
    if (!d) {
      isHovering = false
      hoveredTime = null
      renderFocusLegend()
      return
    }
    isHovering = true
    hoveredTime = param.time as Time
    focusPinnedByKeys = false
    legend.value.innerHTML = formatLegend(d, param.time as Time)
  })

  container.value.addEventListener('wheel', handleWheel, { passive: false })

  resizeObserver = new ResizeObserver((entries) => {
    const el = entries[0].target as HTMLElement
    chart?.applyOptions({ width: el.clientWidth, height: el.clientHeight })
    applyPaneHeights()
  })
  resizeObserver.observe(container.value)
  renderData()
  renderOverlays()
  // 兜底：确保图表拿到容器实际尺寸
  requestAnimationFrame(() => {
    if (container.value && chart) {
      //   `== 回退帧: 宽${container.value.clientWidth} 行${props.rows.length} 保存${lastView ? '有' : '无'}`,
      // )
      chart.applyOptions({
        width: container.value.clientWidth,
        height: container.value.clientHeight,
      })
      applyPaneHeights()
      // 尺寸定稿后再校准一次默认视图：防止初始化期间宽度变化把视图重置成全量
      // 图表还没有数据时不能设置视图：空图表上应用视图会污染时间轴的间距状态，
      // 等数据到达后 renderData 会用正确的数据长度校准视图
      if (!lastView && candleSeries && candleSeries.data().length > 0) applyDefaultView()
      // requestAnimationFrame(() => {
      //   if (!chart) return
      //   const lr = chart.timeScale().getVisibleLogicalRange()
      //     `== 终态: 视图${lr ? lr.from.toFixed(2) + '~' + lr.to.toFixed(2) : 'null'} ` +
      //       `间距${Number(chart.timeScale().options().barSpacing.toFixed(2))}`,
      //   )
      // })
    }
  })
  updateCountdown()
  countdownTimer = setInterval(updateCountdown, 1000)
})

watch(
  () => props.rows,
  () => {
    if (chart) {
      renderData()
      renderOverlays()
    }
  },
  { deep: true },
)
watch(
  () => props.signals,
  () => {
    if (chart) renderOverlays()
  },
  { deep: true },
)
watch(
  () => props.trendPoints,
  () => {
    if (!chart) return
    syncTrendSeries()
    renderFocusLegend()
  },
  { deep: true },
)
watch(trendVisible, () => {
  if (!chart) return
  syncTrendSeries()
  renderFocusLegend()
})
watch(
  () => props.reviewExit,
  () => {
    if (chart) renderOverlays()
  },
)
watch(
  () => props.focusTs,
  (ts) => focusAtTs(ts ?? null),
  { immediate: true },
)
watch(
  () => props.focusKey,
  () => focusAtTs(props.focusTs ?? null),
)
watch(
  () => props.showExtremes,
  () => {
    syncExtremes()
    markersApi?.setMarkers(buildMarkers())
  },
)

onBeforeUnmount(() => {
  container.value?.removeEventListener('wheel', handleWheel)
  resizeObserver?.disconnect()
  if (gapPrimitive && candleSeries) {
    candleSeries.detachPrimitive(gapPrimitive)
    gapPrimitive = null
  }
  if (rolloverPrimitive && candleSeries) {
    candleSeries.detachPrimitive(rolloverPrimitive)
    rolloverPrimitive = null
  }
  if (eventLabelPrimitive && candleSeries) {
    candleSeries.detachPrimitive(eventLabelPrimitive)
    eventLabelPrimitive = null
  }
  focusIndex = -1
  focusPinnedByKeys = false
  pendingFocusTs = null
  focusRetryCount = 0
  if (focusRetryTimer) { clearTimeout(focusRetryTimer); focusRetryTimer = null }
  hoveredTime = null
  isHovering = false
  chart?.timeScale().unsubscribeVisibleLogicalRangeChange(onVisibleRangeChange)
  if (countdownTimer) {
    clearInterval(countdownTimer)
    countdownTimer = null
  }
  markersApi?.detach()
  markersApi = null
  for (const line of extremeLines) candleSeries?.removePriceLine(line)
  extremeLines = []
  for (const line of patternLines) chart?.removeSeries(line)
  patternLines = []
  if (trendSeries) {
    chart?.removeSeries(trendSeries)
    trendSeries = null
  }
  chart?.remove()
  chart = null
  candleSeries = null
  volumeSeries = null
  priceLines = []
})

defineExpose({ stepCandles })
</script>

<template>
  <div class="kline-wrap">
    <div ref="legend" class="legend">N趋势 K线</div>
    <div ref="timeLeft" class="time-left"></div>
    <div ref="container" class="kline-canvas"></div>
    <!-- 临时调试面板（已注释，见文件顶部说明；取消注释可复测“巨大K线”问题） -->
    </div> -->
    <n-spin v-if="loading" class="spin-mask" />
    <button
      class="trend-toggle"
      :class="{ active: trendVisible }"
      type="button"
      @click="trendVisible = !trendVisible"
    >
      MA20
    </button>
    <div class="help-hint">
      <n-tooltip trigger="hover" placement="bottom-end">
        <template #trigger>
          <span class="help-icon">?</span>
        </template>
        ←/→ 切换焦点K线<br />Ctrl+滚轮 缩放<br />滚轮 切换品种<br />拖拽 平移<br />双击价格轴 复位
      </n-tooltip>
    </div>
  </div>
</template>

<style scoped>
.kline-wrap {
  position: relative;
  width: 100%;
  flex: 1 1 auto;
  min-height: 0;
  background: #fff;
  border-radius: 8px;
  overflow: hidden;
}
.kline-canvas {
  position: absolute;
  inset: 0;
}
.legend {
  position: absolute;
  top: 8px;
  left: 12px;
  z-index: 5;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: #334155;
  background: rgba(255, 255, 255, 0.88);
  border: 1px solid rgba(100, 116, 139, 0.28);
  box-shadow: 0 1px 4px rgba(15, 23, 42, 0.1);
  padding: 5px 10px;
  border-radius: 6px;
  pointer-events: none;
  white-space: nowrap;
}
.legend :deep(.lg-time) {
  color: #475569;
  font-variant-numeric: tabular-nums;
}
.legend :deep(.lg-sep) {
  width: 1px;
  height: 14px;
  background: rgba(148, 163, 184, 0.5);
}
.legend :deep(.lg-item) {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.legend :deep(.lg-label) {
  color: #94a3b8;
  font-size: 11px;
}
.legend :deep(.lg-value) {
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.legend :deep(.lg-trend) {
  font-weight: 600;
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
}
.legend :deep(.trend-up) {
  color: #e03131;
  background: rgba(224, 49, 49, 0.1);
}
.legend :deep(.trend-down) {
  color: #0f9d58;
  background: rgba(15, 157, 88, 0.1);
}
.legend :deep(.trend-flat) {
  color: #64748b;
  background: rgba(100, 116, 139, 0.12);
}
.time-left {
  position: absolute;
  z-index: 5;
  display: none;
  transform: translateX(-50%);
  font-size: 11px;
  font-weight: 600;
  color: #334155;
  background: rgba(255, 255, 255, 0.9);
  border: 1px solid rgba(100, 116, 139, 0.3);
  box-shadow: 0 1px 4px rgba(15, 23, 42, 0.12);
  padding: 2px 8px;
  border-radius: 5px;
  pointer-events: none;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.trend-toggle {
  position: absolute;
  top: 8px;
  right: 40px;
  z-index: 5;
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 8px;
  border: 1px solid rgba(100, 116, 139, 0.3);
  border-radius: 5px;
  background: rgba(255, 255, 255, 0.9);
  color: #94a3b8;
  font-size: 11px;
  font-weight: 600;
  line-height: 1;
  cursor: pointer;
  user-select: none;
  transition: background 0.15s, border-color 0.15s, color 0.15s;
}
.trend-toggle:hover {
  border-color: rgba(37, 99, 235, 0.4);
  color: #2563eb;
}
.trend-toggle.active {
  border-color: rgba(37, 99, 235, 0.35);
  background: rgba(37, 99, 235, 0.08);
  color: #2563eb;
}
.help-hint {
  position: absolute;
  top: 8px;
  right: 12px;
  z-index: 5;
}
.help-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: rgba(148, 163, 184, 0.2);
  color: #64748b;
  font-size: 11px;
  font-weight: 700;
  line-height: 1;
  cursor: help;
  user-select: none;
  transition: background 0.15s, color 0.15s;
}
.help-icon:hover {
  background: rgba(148, 163, 184, 0.35);
  color: #334155;
}
.spin-mask {
  position: absolute;
  inset: 0;
  background: rgba(255, 255, 255, 0.6);
  z-index: 6;
  display: flex;
  align-items: center;
  justify-content: center;
}
/* 临时调试面板样式（已注释，见文件顶部说明；取消注释可复测“巨大K线”问题） */
/* .debug-info {
  position: absolute;
  left: 8px;
  bottom: 30px;
  z-index: 9;
  max-width: calc(100% - 16px);
  padding: 4px 8px;
  background: rgba(30, 30, 30, 0.85);
  color: #ffd75e;
  font-size: 11px;
  line-height: 1.4;
  border-radius: 6px;
  white-space: pre-wrap;
  pointer-events: none;
  font-family: Consolas, monospace;
} */
</style>









