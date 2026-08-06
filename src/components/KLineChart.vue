<script setup lang="ts">
// reactive 仅被下方被注释的调试面板使用；如取消注释调试面板，需把 reactive 加回 import
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
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
import type { KlineRow, PatternDto } from '../types'
import { useSettingsStore } from '../stores/settings'

const props = defineProps<{
  symbol: string
  timeframe: string
  rows: KlineRow[]
  signals: PatternDto[]
  loading?: boolean
}>()

const container = ref<HTMLDivElement | null>(null)
const legend = ref<HTMLDivElement | null>(null)
const settingsStore = useSettingsStore()
const minBarSpacing = computed(() => settingsStore.settings.ui.min_bar_spacing)

interface GapRect {
  from: Time
  to: Time
  top: number
  bottom: number
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

/** 同一品种/级别内的缩放/平移状态：切换品种或级别时沿用同样的横向视图，避免跳变 */
let lastView: { from: number; to: number; totalAtCapture: number } | null = null
/** 最近一次写入图表的数据量：captureView 用它标记视图取自多长的数据，
 *  用于识别“数据不足被撑满”的短数据瞬态（详见 dropStaleView） */
let lastDataCount = 0

let chart: IChartApi | null = null
let candleSeries: ISeriesApi<'Candlestick'> | null = null
let volumeSeries: ISeriesApi<'Histogram'> | null = null
let resizeObserver: ResizeObserver | null = null
let priceLines: IPriceLine[] = []
let markersApi: ISeriesMarkersPluginApi<Time> | null = null
let gapPrimitive: GapPrimitive | null = null
let patternLines: ISeriesApi<'Line'>[] = []
let priceExtent = 1

// ============================================================================
// 【临时调试面板 — 已注释保留，供后续复测“进入K线图被放大成两根巨大K线”问题】
// 问题现象：从列表/表格页点进K线图时，偶尔只显示最后两三根巨大的K线，
//           退出重进才恢复正常。
// 问题原因（已通过真实运行记录定位）：
//   1) 挂载时全局K线数据里往往还留着上一品种的数据（与当前请求品种不匹配），
//      首次渲染被跳过，图表处于“空图表”状态；
//   2) 挂载后的“回退校准帧”在空图表上执行了默认视图设置（setVisibleLogicalRange），
//      图表库会把“每根K线的像素间距”记为 width/跨度；若此时数据只有寥寥几根
//      （或视图跨度极小），间距会被污染成 300~400px；
//   3) 真正数据到达后，图表直接按被污染的间距渲染，只剩最后两三根巨大K线；
//   4) 该极小视图又被保存为“用户缩放状态”，此后每次数据刷新都沿用，形成卡死。
// 修复方法：
//   1) 空图表（series 数据为空）时禁止任何视图设置，杜绝间距被污染；
//   2) 保存缩放状态时记录当时的数据量，若“数据不足140根、视图被全部数据撑满、
//      且现在数据明显变多”，判定为短数据瞬态，丢弃该状态回到默认视图。
// 取消注释即可再次显示调试面板（需同时把上方 import 中的 reactive 加回）。
// ============================================================================
// const dbg = reactive({
//   visible: true,
//   renderCount: 0,
//   mountCount: 0,
//   unmountCount: 0,
//   rowsLen: 0,
//   candleLen: 0,
//   inChartBefore: -1,
//   lastView: '',
//   logical: '',
//   span: 0,
//   barSpacing: 0,
//   isSwitch: false,
//   width: 0,
//   history: [] as string[],
// })
// function updateDbg() {
//   const ts = chart?.timeScale()
//   const logical = ts?.getVisibleLogicalRange() ?? null
//   dbg.renderCount += 1
//   dbg.rowsLen = props.rows.length
//   dbg.candleLen = buildCandles().length
//   dbg.lastView = lastView ? `${lastView.from.toFixed(2)}~${lastView.to.toFixed(2)}` : 'null'
//   dbg.logical = logical ? `${logical.from.toFixed(2)}~${logical.to.toFixed(2)}` : 'null'
//   dbg.span = logical ? Number((logical.to - logical.from).toFixed(2)) : 0
//   dbg.barSpacing = ts ? Number(ts.options().barSpacing.toFixed(2)) : 0
//   dbg.width = container.value?.clientWidth ?? 0
//   dbg.history.push(
//     `#${dbg.renderCount} 行${dbg.rowsLen} 切换${dbg.isSwitch ? 'Y' : 'N'} ` +
//       `图内${dbg.inChartBefore} 捕获${dbg.lastView} 视图${dbg.logical} 跨度${dbg.span} 间距${dbg.barSpacing}`,
//   )
//   if (dbg.history.length > 8) dbg.history.splice(0, dbg.history.length - 8)
// }

/** 进入图表时默认展示的K线根数（从最新一根往前数）。根数越少单根K线越宽；
 *  想再宽就调小，想多看历史就调大。先写死便于手工调整，后续由配置传入 */
const display_k_num = 140
/** 默认视图右侧留出的空白上限（以K线根数为单位），相当于把图表向左拖一段，让最新K线不贴右边缘 */
const display_right_gap = 10
/** 右侧留白占可见K线根数的比例上限：数据量少的品种留白按此比例缩水，避免右侧出现大片空白 */
const right_gap_ratio = 0.1

/** 计算右侧留白根数：数据量足够时保持 display_right_gap 根；
 *  数据量少时按可见根数的 right_gap_ratio 缩到最小1根，保证不同品种留白占屏比例一致 */
function rightGapBars(visible: number): number {
  if (visible <= 0) return display_right_gap
  return Math.min(display_right_gap, Math.max(1, Math.round(visible * right_gap_ratio)))
}
/** N形态连线/标记颜色：与K线自身的红绿区分开并带透明度，减少对K线的遮挡 */
const PATTERN_UP_COLOR = 'rgba(255, 135, 135, 0.9)' // 上涨：浅红
const PATTERN_DOWN_COLOR = 'rgba(32, 201, 151, 0.9)' // 下跌：青绿
const ZOOM_SENSITIVITY = 0.0015

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

function buildMarkers(): SeriesMarker<Time>[] {
  const markers: SeriesMarker<Time>[] = []
  for (const s of props.signals) {
    const color = s.direction === 'up' ? PATTERN_UP_COLOR : PATTERN_DOWN_COLOR
    markers.push({
      time: toTs(s.s0.ts),
      position: 'belowBar',
      color,
      shape: 'arrowUp',
      text: 'S0',
    })
    markers.push({
      time: toTs(s.s1.ts),
      position: s.direction === 'up' ? 'belowBar' : 'aboveBar',
      color,
      shape: 'circle',
      text: 'S1',
    })
    markers.push({
      time: toTs(s.s2.ts),
      position: s.direction === 'up' ? 'belowBar' : 'aboveBar',
      color,
      shape: 'square',
      text: 'S2',
    })
    if (s.warning_ts) {
      markers.push({
        time: toTs(s.warning_ts),
        position: 'aboveBar',
        color: '#f9a825',
        shape: 'circle',
        text: '预警',
      })
    }
    if (s.trigger_ts) {
      markers.push({
        time: toTs(s.trigger_ts),
        position: 'aboveBar',
        color: '#e53935',
        shape: 'arrowDown',
        text: '触发',
      })
    }
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

/** 画出每个N形态的 S0→S1→S2 连线 */
function syncPatternLines() {
  if (!chart) return
  for (const line of patternLines) chart.removeSeries(line)
  patternLines = []
  for (const sig of props.signals) {
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

function applyDefaultView() {
  if (!chart) return
  // 空图表（数据尚未写入）上设置视图会污染时间轴的间距状态，等数据就位后再校准
  if (!candleSeries || candleSeries.data().length === 0) return
  const total = props.rows.length
  if (total === 0) return
  const visible = Math.min(display_k_num, total)
  // 右边界放在最后一根K线右侧留出空白（最后一根K线中心在逻辑坐标 total-1 处）
  const to = total - 0.5 + rightGapBars(visible)
  const from = Math.max(-0.5, to - visible)
  // dbg.history.push(`== 默认视图: 行${total} 请求${from.toFixed(2)}~${to.toFixed(2)}`)
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
    capturedTotal < display_k_num &&
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
  // dbg.history.push(
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
  // dbg.history.push(`== 切换视图: 行${total} 跨度请求${span} 请求${from.toFixed(2)}~${to.toFixed(2)}`)
  chart.timeScale().setVisibleLogicalRange({ from, to })
  clampMinBarSpacing({ from, to })
  // 纵轴自动适配新品种的价格区间，避免因价格水平不同导致画面空白
  chart.priceScale('right').setAutoScale(true)
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

let prevSymbol: string | null = null
let prevTimeframe: string | null = null

/** 当前 rows 是否真的属于 props 指定的品种/周期。
 *  切换品种/周期时后端尚未返回新数据前，组件会先收到「旧数据+新品种」的中间态，
 *  用它判断可以避免中间态提前消耗掉切换状态、导致新数据到达后被当成普通刷新 */
function rowsMatchRequest(): boolean {
  const first = props.rows[0]
  return !!first && first.symbol === props.symbol && first.timeframe === props.timeframe
}

/** 数据变化时：沿用当前缩放/平移状态（无记录则默认视图）；
 *  切换品种/周期则贴到新数据最右端并留出右侧空白，避免最新K线紧贴右边界。
 *  数据与请求的品种/周期不一致（切换中间态）时跳过视图处理，等新数据到达再切换 */
function renderData() {
  if (!chart || !candleSeries || !volumeSeries) return
  if (!rowsMatchRequest()) {
    // dbg.history.push(
    //   `== 跳过渲染: 行${props.rows.length} 首行${props.rows[0] ? props.rows[0].symbol + '/' + props.rows[0].timeframe : '-'} 请求${props.symbol}/${props.timeframe}`,
    // )
    return
  }
  const isSwitch = prevSymbol !== props.symbol || prevTimeframe !== props.timeframe
  // dbg.isSwitch = isSwitch
  prevSymbol = props.symbol
  prevTimeframe = props.timeframe
  // dbg.inChartBefore = candleSeries.data().length
  captureView()
  updatePriceExtent()
  candleSeries.setData(buildCandles())
  volumeSeries.setData(buildVolumes())
  lastDataCount = props.rows.length
  syncGaps()
  // updateDbg()
  if (isSwitch) {
    dropStaleView(props.rows.length)
    const span = lastView
      ? Math.min(lastView.to - lastView.from, props.rows.length)
      : Math.min(display_k_num, props.rows.length)
    applySwitchView(span)
  } else {
    restoreView()
  }
}

/** 只刷新形态标注与价位线，保留当前缩放/平移状态 */
function renderOverlays() {
  if (!chart || !candleSeries) return
  markersApi?.setMarkers(buildMarkers())
  syncPriceLines()
  syncPatternLines()
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
  // 每次进入图表页先回到默认视图（最新 display_k_num 根）；页内切换品种/级别沿用缩放
  // dbg.mountCount += 1
  // dbg.history.push(
  //   `== 挂载#${dbg.mountCount} 上次保存${lastView ? lastView.from.toFixed(2) + '~' + lastView.to.toFixed(2) : 'null'} ` +
  //     `行${props.rows.length} 首行${props.rows[0] ? props.rows[0].symbol + '/' + props.rows[0].timeframe : '-'} ` +
  //     `请求${props.symbol}/${props.timeframe} 匹配${rowsMatchRequest() ? 'Y' : 'N'} 旧图${chart ? '有' : '无'}`,
  // )
  lastView = null
  lastDataCount = 0
  prevSymbol = null
  prevTimeframe = null
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
    downColor: '#0f9d58',
    borderVisible: true,
    borderUpColor: '#e03131',
    borderDownColor: '#0f9d58',
    wickUpColor: '#e03131',
    wickDownColor: '#0f9d58',
  })
  markersApi = createSeriesMarkers(candleSeries, [])
  // 成交量柱状图放在独立窗格（pane 1），与K线物理分离，放大不会互相压到
  volumeSeries = chart.addSeries(
    HistogramSeries,
    { priceFormat: { type: 'volume' }, priceScaleId: 'vol' },
    1,
  )
  // 0.08 就是 8%（小数形式），top 控制上边距、bottom 控制下边距
  chart.priceScale('right').applyOptions({ scaleMargins: { top: 0.08, bottom: 0.08 } })
  chart.priceScale('vol', 1).applyOptions({ scaleMargins: { top: 0.08, bottom: 0.04 } })
  applyPaneHeights()

  chart.subscribeCrosshairMove((param) => {
    if (!legend.value || !param.time || !candleSeries) return
    const d = param.seriesData.get(candleSeries) as CandlestickData | undefined
    if (!d) return
    legend.value.innerHTML = `${formatTime(param.time as Time)}　开 ${d.open}　高 ${d.high}　低 ${d.low}　收 ${d.close}`
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
      // dbg.history.push(
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
      //   dbg.history.push(
      //     `== 终态: 视图${lr ? lr.from.toFixed(2) + '~' + lr.to.toFixed(2) : 'null'} ` +
      //       `间距${Number(chart.timeScale().options().barSpacing.toFixed(2))}`,
      //   )
      // })
    }
  })
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

onBeforeUnmount(() => {
  // dbg.unmountCount += 1
  // dbg.history.push(`== 卸载#${dbg.unmountCount} 行${props.rows.length} 保存${lastView ? '有' : '无'}`)
  container.value?.removeEventListener('wheel', handleWheel)
  resizeObserver?.disconnect()
  if (gapPrimitive && candleSeries) {
    candleSeries.detachPrimitive(gapPrimitive)
    gapPrimitive = null
  }
  markersApi?.detach()
  markersApi = null
  for (const line of patternLines) chart?.removeSeries(line)
  patternLines = []
  chart?.remove()
  chart = null
  candleSeries = null
  volumeSeries = null
  priceLines = []
})
</script>

<template>
  <div class="kline-wrap">
    <div ref="legend" class="legend">N趋势 K线</div>
    <div ref="container" class="kline-canvas"></div>
    <!-- 临时调试面板（已注释，见文件顶部说明；取消注释可复测“巨大K线”问题） -->
    <!-- <div v-if="dbg.visible" class="debug-info">
      [挂载{{ dbg.mountCount }}] 渲染{{ dbg.renderCount }}
      行{{ dbg.rowsLen }} 蜡烛{{ dbg.candleLen }} 图内{{ dbg.inChartBefore }} 视图{{ dbg.logical }} 跨度{{ dbg.span }}
      间距{{ dbg.barSpacing }} 保存{{ dbg.lastView }} 切换{{ dbg.isSwitch ? 'Y' : 'N' }} 宽{{ dbg.width }}
      <template v-for="(h, i) in dbg.history" :key="i">{{ h }}<br /></template>
    </div> -->
    <n-spin v-if="loading" class="spin-mask" />
    <div class="help-hint">
      <n-tooltip trigger="hover" placement="bottom-end">
        <template #trigger>
          <span class="help-icon">?</span>
        </template>
        Ctrl+滚轮 缩放<br />滚轮 切换品种<br />拖拽 平移<br />双击价格轴 复位
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
  font-size: 12px;
  color: #475569;
  background: rgba(255, 255, 255, 0.85);
  padding: 2px 8px;
  border-radius: 4px;
  pointer-events: none;
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










