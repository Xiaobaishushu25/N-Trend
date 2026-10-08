<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NSpin } from 'naive-ui'
import {
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  createChart,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesPrimitive,
  type IPrimitivePaneView,
  type IPrimitivePaneRenderer,
  type PrimitivePaneViewZOrder,
  type SeriesAttachedParameter,
  type Time,
  type UTCTimestamp,
  type WhitespaceData,
} from 'lightweight-charts'
import type { CanvasRenderingTarget2D, MediaCoordinatesRenderingScope } from 'fancy-canvas'
import { useIntradayStore } from '../stores/intraday'
import type { IntradayPoint } from '../types'

const props = defineProps<{
  symbol: string
}>()

const store = useIntradayStore()
const container = ref<HTMLDivElement | null>(null)

let chart: IChartApi | null = null
let priceSeries: ISeriesApi<'Line'> | null = null
let avgSeries: ISeriesApi<'Line'> | null = null
let volumeSeries: ISeriesApi<'Histogram'> | null = null
let prevSettleLine: IPriceLine | null = null
let sessionPrimitive: SessionBandPrimitive | null = null

// 悬停十字光标数据，未悬停时默认展示最新一条
const activePoint = ref<IntradayPoint | null>(null)

const latestPoint = computed(() => {
  if (store.points.length === 0) return null
  return store.points[store.points.length - 1]
})

const currentDisplay = computed(() => {
  return activePoint.value || latestPoint.value
})

const changeInfo = computed(() => {
  const p = currentDisplay.value
  if (!p || !store.prevSettle || store.prevSettle <= 0) {
    return { diff: 0, pct: '0.00%', isUp: false, isDown: false }
  }
  const diff = p.price - store.prevSettle
  const pct = ((diff / store.prevSettle) * 100).toFixed(2) + '%'
  return {
    diff,
    pct: diff > 0 ? `+${pct}` : pct,
    isUp: diff > 0,
    isDown: diff < 0,
  }
})

function parseTs(ts: string): UTCTimestamp {
  return Math.floor(new Date(ts.replace(' ', 'T') + 'Z').getTime() / 1000) as UTCTimestamp
}

let currentFutureWhitespace: WhitespaceData<UTCTimestamp>[] = []

function getSessionBoundaryTimes(
  points: IntradayPoint[],
  futureWhitespace: WhitespaceData<UTCTimestamp>[] = []
): { nightStartTime: Time | null; dayStartTime: Time | null } {
  if (!points || points.length === 0) return { nightStartTime: null, dayStartTime: null }

  let nightStart: Time | null = null
  let dayStart: Time | null = null

  for (const p of points) {
    const hour = parseInt(p.ts.substring(11, 13), 10)
    const isNight = hour >= 20 || hour < 8
    const isDay = hour >= 8 && hour <= 16

    if (isNight && nightStart === null) {
      nightStart = parseTs(p.ts) as Time
    }
    if (isDay && dayStart === null) {
      dayStart = parseTs(p.ts) as Time
    }
  }

  // 若处于夜盘且尚未进入日盘，从 futureWhitespace 中寻找次日日盘 09:01 的时间戳
  if (nightStart !== null && dayStart === null && futureWhitespace.length > 0) {
    for (const ws of futureWhitespace) {
      const d = new Date((ws.time as number) * 1000)
      const h = d.getUTCHours()
      if (h >= 8 && h <= 16) {
        dayStart = ws.time
        break
      }
    }
  }

  return { nightStartTime: nightStart, dayStartTime: dayStart }
}

class SessionBandPaneRenderer implements IPrimitivePaneRenderer {
  constructor(
    private chart: IChartApi,
    private getBoundary: () => { nightStartTime: Time | null; dayStartTime: Time | null },
  ) {}

  draw(target: CanvasRenderingTarget2D) {
    const { nightStartTime, dayStartTime } = this.getBoundary()
    // 若无夜盘数据（如纯日盘品种），不进行任何绘制，全屏保留纯白底色
    if (!nightStartTime) return

    target.useMediaCoordinateSpace((scope: MediaCoordinatesRenderingScope) => {
      const { context, mediaSize } = scope
      const timeScale = this.chart.timeScale()

      if (dayStartTime) {
        const dayStartX = timeScale.timeToCoordinate(dayStartTime)
        if (dayStartX != null) {
          let startX = 0
          const nx = timeScale.timeToCoordinate(nightStartTime)
          if (nx != null && nx > 0) {
            startX = nx
          }

          const nightWidth = dayStartX - startX
          if (nightWidth > 0) {
            // 1. 夜盘浅灰底色（精致微调对比日盘区域）
            context.fillStyle = 'rgba(0, 0, 0, 0.032)'
            context.fillRect(startX, 0, nightWidth, mediaSize.height)

            // 2. 垂直分界线（夜盘与日盘分割线）
            context.strokeStyle = '#cbd5e1'
            context.lineWidth = 1
            context.beginPath()
            context.moveTo(dayStartX, 0)
            context.lineTo(dayStartX, mediaSize.height)
            context.stroke()

            // 3. 底部分时标签 [ 夜盘 | 日盘 ] 胶囊徽章
            const badgeY = Math.round(mediaSize.height * 0.77)
            const badgeW = 28
            const badgeH = 16

            // 夜盘标签（位于分界线左侧）
            context.fillStyle = '#e2e8f0'
            context.fillRect(dayStartX - badgeW - 2, badgeY, badgeW, badgeH)
            context.fillStyle = '#64748b'
            context.font = '10px -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif'
            context.textAlign = 'center'
            context.textBaseline = 'middle'
            context.fillText('夜盘', dayStartX - badgeW / 2 - 2, badgeY + badgeH / 2)

            // 日盘标签（位于分界线右侧）
            context.fillStyle = '#f1f5f9'
            context.fillRect(dayStartX + 2, badgeY, badgeW, badgeH)
            context.fillStyle = '#64748b'
            context.fillText('日盘', dayStartX + badgeW / 2 + 2, badgeY + badgeH / 2)
          }
        }
      } else {
        // 只有夜盘数据（当前处于夜盘交易时段）
        context.fillStyle = 'rgba(0, 0, 0, 0.032)'
        context.fillRect(0, 0, mediaSize.width, mediaSize.height)
      }
    })
  }
}

class SessionBandPaneView implements IPrimitivePaneView {
  private paneRenderer: SessionBandPaneRenderer
  constructor(chart: IChartApi, getBoundary: () => { nightStartTime: Time | null; dayStartTime: Time | null }) {
    this.paneRenderer = new SessionBandPaneRenderer(chart, getBoundary)
  }
  renderer(): IPrimitivePaneRenderer | null {
    return this.paneRenderer
  }
  zOrder(): PrimitivePaneViewZOrder {
    return 'bottom'
  }
}

class SessionBandPrimitive implements ISeriesPrimitive<Time> {
  private view: SessionBandPaneView
  private _requestUpdate?: () => void

  constructor(chart: IChartApi, getBoundary: () => { nightStartTime: Time | null; dayStartTime: Time | null }) {
    this.view = new SessionBandPaneView(chart, getBoundary)
  }

  attached(param: SeriesAttachedParameter<Time>) {
    this._requestUpdate = param.requestUpdate
  }

  detached() {
    this._requestUpdate = undefined
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return [this.view]
  }

  update() {
    this._requestUpdate?.()
  }
}

function getNightCloseTime(symbol: string): { hour: number; minute: number } | null {
  const prefix = symbol.replace(/\d+$/, '').toUpperCase()
  // 02:30 贵金属与原油
  if (['AU', 'AG', 'SC'].includes(prefix)) {
    return { hour: 2, minute: 30 }
  }
  // 01:00 有色金属与不锈钢
  if (['CU', 'AL', 'ZN', 'PB', 'NI', 'SN', 'BC', 'SS'].includes(prefix)) {
    return { hour: 1, minute: 0 }
  }
  // 23:30 纯碱与玻璃
  if (['SA', 'FG'].includes(prefix)) {
    return { hour: 23, minute: 30 }
  }
  // 无夜盘品种（农产品、部分能化、中金所股指国债）
  if ([
    'AP', 'CJ', 'JD', 'LH', 'PK', 'SI', 'LC', 'UR', 'WH', 'PM', 'RI', 'JR',
    'LR', 'BB', 'FB', 'IF', 'IH', 'IC', 'IM', 'TF', 'T', 'TS', 'TL'
  ].includes(prefix)) {
    return null
  }
  // 其余黑色、化工、油脂等默认为 23:00
  return { hour: 23, minute: 0 }
}

function generateFutureWhitespace(symbol: string, lastTs: string): WhitespaceData<UTCTimestamp>[] {
  if (!lastTs || lastTs.length < 16) return []

  const dateStr = lastTs.substring(0, 10)
  const hour = parseInt(lastTs.substring(11, 13), 10)
  const minute = parseInt(lastTs.substring(14, 16), 10)
  const timeVal = hour * 60 + minute

  const prefix = symbol.replace(/\d+$/, '').toUpperCase()
  const isCffex = ['IF', 'IH', 'IC', 'IM', 'TF', 'T', 'TS', 'TL'].includes(prefix)
  const isBond = ['TF', 'T', 'TS', 'TL'].includes(prefix)

  const pad = (n: number) => String(n).padStart(2, '0')
  const toTsStr = (d: string, h: number, m: number) => `${d} ${pad(h)}:${pad(m)}:00`

  const whitespaceList: WhitespaceData<UTCTimestamp>[] = []

  // 情况 1: 当前处于日盘交易时段（08:50 ~ 15:15 之间）
  // 补齐从当前时间直至当天收盘（普通商品 15:00，国债 15:15）的所有交易分钟
  if (timeVal >= 8 * 60 && timeVal < 15 * 60 + 15) {
    const dayDate = dateStr

    if (isCffex) {
      // 中金所时段: 09:30-11:30, 13:00-15:00 (国债至 15:15)
      for (let m = 9 * 60 + 31; m <= 11 * 60 + 30; m++) {
        if (m > timeVal) {
          const h = Math.floor(m / 60)
          const min = m % 60
          whitespaceList.push({ time: parseTs(toTsStr(dayDate, h, min)) })
        }
      }
      const closeMin = isBond ? 15 * 60 + 15 : 15 * 60
      for (let m = 13 * 60 + 1; m <= closeMin; m++) {
        if (m > timeVal) {
          const h = Math.floor(m / 60)
          const min = m % 60
          whitespaceList.push({ time: parseTs(toTsStr(dayDate, h, min)) })
        }
      }
    } else {
      // 普通商品期货: 09:00-10:15, 10:30-11:30, 13:30-15:00
      for (let m = 9 * 60 + 1; m <= 10 * 60 + 15; m++) {
        if (m > timeVal) {
          const h = Math.floor(m / 60)
          const min = m % 60
          whitespaceList.push({ time: parseTs(toTsStr(dayDate, h, min)) })
        }
      }
      for (let m = 10 * 60 + 31; m <= 11 * 60 + 30; m++) {
        if (m > timeVal) {
          const h = Math.floor(m / 60)
          const min = m % 60
          whitespaceList.push({ time: parseTs(toTsStr(dayDate, h, min)) })
        }
      }
      for (let m = 13 * 60 + 31; m <= 15 * 60; m++) {
        if (m > timeVal) {
          const h = Math.floor(m / 60)
          const min = m % 60
          whitespaceList.push({ time: parseTs(toTsStr(dayDate, h, min)) })
        }
      }
    }

    return whitespaceList
  }

  // 情况 2: 当前处于夜盘交易时段（20:50 至 24:00，或者跨日 00:00 至 02:30）
  // 补齐：剩余夜盘分钟 + 次日完整日盘时段直至 15:00
  const nightClose = getNightCloseTime(symbol)
  if (nightClose && (hour >= 20 || hour < 8)) {
    let dayDate = dateStr
    if (hour >= 20) {
      const parts = dateStr.split('-').map(Number)
      const curDate = new Date(parts[0], parts[1] - 1, parts[2])
      if (curDate.getDay() === 5) {
        // 周五夜盘归属下周一交易日
        curDate.setDate(curDate.getDate() + 3)
      } else {
        curDate.setDate(curDate.getDate() + 1)
      }
      dayDate = `${curDate.getFullYear()}-${pad(curDate.getMonth() + 1)}-${pad(curDate.getDate())}`
    }

    // (a) 剩余夜盘前半夜 (21:01 - 24:00)
    if (hour >= 20) {
      const nightCloseVal = nightClose.hour < 8 ? 24 * 60 : nightClose.hour * 60 + nightClose.minute
      for (let m = 21 * 60 + 1; m <= nightCloseVal; m++) {
        if (m > timeVal && m <= 24 * 60) {
          const h = Math.floor(m / 60)
          const min = m % 60
          whitespaceList.push({ time: parseTs(toTsStr(dateStr, h, min)) })
        }
      }
      // 跨日到凌晨
      if (nightClose.hour < 8) {
        const morningCloseVal = nightClose.hour * 60 + nightClose.minute
        for (let m = 1; m <= morningCloseVal; m++) {
          const h = Math.floor(m / 60)
          const min = m % 60
          whitespaceList.push({ time: parseTs(toTsStr(dayDate, h, min)) })
        }
      }
    } else if (hour < 8) {
      // (b) 已经进入凌晨
      const morningCloseVal = nightClose.hour * 60 + nightClose.minute
      for (let m = 1; m <= morningCloseVal; m++) {
        if (m > timeVal) {
          const h = Math.floor(m / 60)
          const min = m % 60
          whitespaceList.push({ time: parseTs(toTsStr(dayDate, h, min)) })
        }
      }
    }

    // (c) 次日日盘全天预留（09:00 - 15:00）
    for (let m = 9 * 60 + 1; m <= 10 * 60 + 15; m++) {
      const h = Math.floor(m / 60)
      const min = m % 60
      whitespaceList.push({ time: parseTs(toTsStr(dayDate, h, min)) })
    }
    for (let m = 10 * 60 + 31; m <= 11 * 60 + 30; m++) {
      const h = Math.floor(m / 60)
      const min = m % 60
      whitespaceList.push({ time: parseTs(toTsStr(dayDate, h, min)) })
    }
    for (let m = 13 * 60 + 31; m <= 15 * 60; m++) {
      const h = Math.floor(m / 60)
      const min = m % 60
      whitespaceList.push({ time: parseTs(toTsStr(dayDate, h, min)) })
    }

    return whitespaceList
  }

  return []
}

function initChart() {
  if (!container.value) return

  chart = createChart(container.value, {
    layout: {
      background: { type: ColorType.Solid, color: '#ffffff' },
      textColor: '#475569',
    },
    grid: {
      vertLines: { color: 'rgba(0, 0, 0, 0.05)' },
      horzLines: { color: 'rgba(0, 0, 0, 0.05)' },
    },
    crosshair: {
      mode: CrosshairMode.Normal,
      vertLine: {
        color: 'rgba(0, 0, 0, 0.25)',
        width: 1,
        style: LineStyle.Dashed,
      },
      horzLine: {
        color: 'rgba(0, 0, 0, 0.25)',
        width: 1,
        style: LineStyle.Dashed,
      },
    },
    rightPriceScale: {
      borderColor: 'rgba(0, 0, 0, 0.08)',
      scaleMargins: {
        top: 0.08,
        bottom: 0.25, // 预留底部给成交量
      },
    },
    timeScale: {
      borderColor: 'rgba(0, 0, 0, 0.08)',
      timeVisible: true,
      secondsVisible: false,
      fixLeftEdge: true,
      fixRightEdge: false,
      lockVisibleTimeRangeOnResize: true,
      minBarSpacing: 0.1,
      shiftVisibleRangeOnNewBar: false,
    },
  })

  // 1. 现价折线（纯黑/深灰细线，无面积阴影，上下全为纯白）
  priceSeries = chart.addSeries(LineSeries, {
    color: '#0f172a',
    lineWidth: 1,
    lineStyle: LineStyle.Solid,
    priceFormat: { type: 'price', precision: 1, minMove: 1 },
  })

  // 挂载夜盘浅灰区域与日夜分界线 Primitive
  sessionPrimitive = new SessionBandPrimitive(chart, () =>
    getSessionBoundaryTimes(store.points, currentFutureWhitespace),
  )
  priceSeries.attachPrimitive(sessionPrimitive)

  // 2. 均价线（明亮宝蓝色，光滑醒目）
  avgSeries = chart.addSeries(LineSeries, {
    color: '#2563eb',
    lineWidth: 1,
    lineStyle: LineStyle.Solid,
    priceFormat: { type: 'price', precision: 1, minMove: 1 },
  })

  // 3. 底部成交量柱图（分配在底部 20% 高度）
  volumeSeries = chart.addSeries(HistogramSeries, {
    priceFormat: { type: 'volume' },
    priceScaleId: 'volume_scale',
  })

  chart.priceScale('volume_scale').applyOptions({
    scaleMargins: {
      top: 0.82,
      bottom: 0,
    },
  })

  // 十字光标订阅
  chart.subscribeCrosshairMove((param) => {
    if (!param.time || !param.seriesData || store.points.length === 0) {
      activePoint.value = null
      return
    }

    const t = param.time as number
    const found = store.points.find((p) => parseTs(p.ts) === t)
    if (found) {
      activePoint.value = found
    } else {
      activePoint.value = null
    }
  })

  updateChartData()
}

function applyVisibleRange(totalBars: number) {
  if (!chart || totalBars <= 0) return
  chart.timeScale().setVisibleLogicalRange({
    from: 0,
    to: Math.max(1, totalBars - 1),
  })
}

function updateChartData() {
  if (!chart || !priceSeries || !avgSeries || !volumeSeries) return

  const pts = store.points
  if (pts.length === 0) {
    currentFutureWhitespace = []
    priceSeries.setData([])
    avgSeries.setData([])
    volumeSeries.setData([])
    if (prevSettleLine) {
      priceSeries.removePriceLine(prevSettleLine)
      prevSettleLine = null
    }
    sessionPrimitive?.update()
    return
  }

  // 排序与去重时间戳
  const sorted = [...pts].sort((a, b) => parseTs(a.ts) - parseTs(b.ts))

  // 1. 过滤隔离跨交易日数据：
  // 交易日以晚间 20:30+ 夜盘为开端，次日下午 15:00 收盘为终点。
  // 若 points 内部包含旧交易日的日盘 (<=16:00) 与新交易日的夜盘 (>=20:00)，夜盘为新交易日起点，剥离旧日盘点。
  let sessionStartIndex = 0
  for (let i = 1; i < sorted.length; i++) {
    const prevHour = parseInt(sorted[i - 1].ts.substring(11, 13), 10)
    const currHour = parseInt(sorted[i].ts.substring(11, 13), 10)
    if (prevHour <= 16 && currHour >= 20) {
      sessionStartIndex = i
    }
  }
  const currentSessionPts = sessionStartIndex > 0 ? sorted.slice(sessionStartIndex) : sorted

  const uniquePts: IntradayPoint[] = []
  let lastT = -1
  for (const p of currentSessionPts) {
    const t = parseTs(p.ts)
    if (t !== lastT) {
      uniquePts.push(p)
      lastT = t
    } else if (uniquePts.length > 0) {
      uniquePts[uniquePts.length - 1] = p
    }
  }

  const priceData = uniquePts.map((p) => ({
    time: parseTs(p.ts),
    value: p.price,
  }))

  const avgData = uniquePts.map((p) => ({
    time: parseTs(p.ts),
    value: p.avg_price,
  }))

  const volData = uniquePts.map((p) => ({
    time: parseTs(p.ts),
    value: p.volume,
    color: p.price >= store.prevSettle ? 'rgba(239, 68, 68, 0.55)' : 'rgba(34, 197, 94, 0.55)',
  }))

  const lastPointTs = uniquePts[uniquePts.length - 1].ts
  currentFutureWhitespace = generateFutureWhitespace(props.symbol, lastPointTs)

  priceSeries.setData([...priceData, ...currentFutureWhitespace])
  avgSeries.setData([...avgData, ...currentFutureWhitespace])
  volumeSeries.setData([...volData, ...currentFutureWhitespace])

  // 昨结算基准线 (0.00% 轴)
  if (store.prevSettle > 0) {
    if (!prevSettleLine) {
      prevSettleLine = priceSeries.createPriceLine({
        price: store.prevSettle,
        color: 'rgba(148, 163, 184, 0.65)',
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: `昨结 ${store.prevSettle.toFixed(1)}`,
      })
    } else {
      prevSettleLine.applyOptions({
        price: store.prevSettle,
        title: `昨结 ${store.prevSettle.toFixed(1)}`,
      })
    }
  }

  const totalBars = uniquePts.length + currentFutureWhitespace.length
  applyVisibleRange(totalBars)
  requestAnimationFrame(() => applyVisibleRange(totalBars))
  sessionPrimitive?.update()
}

// 监听窗口大小缩放
function handleResize() {
  if (chart && container.value) {
    chart.resize(container.value.clientWidth, container.value.clientHeight)
    const pts = store.points
    if (pts.length > 0) {
      const totalBars = pts.length + currentFutureWhitespace.length
      applyVisibleRange(totalBars)
    }
  }
}

// 防抖切换品种
let debounceTimer: any = null
watch(
  () => props.symbol,
  (sym) => {
    clearTimeout(debounceTimer)
    debounceTimer = setTimeout(() => {
      store.load(sym)
    }, 150)
  },
  { immediate: true }
)

// 监听 points 数据变化
watch(
  () => store.points,
  () => {
    updateChartData()
  },
  { deep: true }
)

onMounted(() => {
  nextTick(() => {
    initChart()
    window.addEventListener('resize', handleResize)
    if (props.symbol && store.points.length === 0) {
      store.load(props.symbol)
    }
  })
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  clearTimeout(debounceTimer)
  chart?.remove()
  chart = null
})
</script>

<template>
  <div class="intraday-wrapper">
    <!-- 顶部状态信息横条（价格、均价、昨结、涨跌幅） -->
    <div class="intraday-header">
      <div v-if="currentDisplay" class="header-items">
        <span class="info-item">
          <span class="label">时间:</span>
          <span class="val time-val">{{ currentDisplay.ts.substring(11, 16) }}</span>
        </span>
        <span class="info-item">
          <span class="label">价格:</span>
          <span
            class="val font-semibold"
            :class="{ 'text-red': changeInfo.isUp, 'text-green': changeInfo.isDown }"
          >
            {{ currentDisplay.price.toFixed(1) }}
          </span>
        </span>
        <span class="info-item">
          <span class="label">均价:</span>
          <span class="val text-blue font-semibold">
            {{ currentDisplay.avg_price.toFixed(1) }}
          </span>
        </span>
        <span class="info-item">
          <span class="label">涨跌:</span>
          <span
            class="val"
            :class="{ 'text-red': changeInfo.isUp, 'text-green': changeInfo.isDown }"
          >
            {{ changeInfo.pct }}
          </span>
        </span>
        <span v-if="currentDisplay.volume > 0" class="info-item">
          <span class="label">量:</span>
          <span class="val text-muted">{{ currentDisplay.volume }}</span>
        </span>
      </div>
      <div v-else-if="store.error" class="header-items text-red">
        <span>加载分时失败: {{ store.error }}</span>
        <button class="retry-btn" @click="store.load(props.symbol, true)">重试</button>
      </div>
      <div v-else-if="!store.loading && store.points.length === 0" class="header-items text-muted">
        今日暂无分时数据
        <button class="retry-btn" @click="store.load(props.symbol, true)">刷新</button>
      </div>
      <div v-else class="header-items text-muted">
        正在载入分时走势...
      </div>
    </div>

    <!-- 图表容器 -->
    <div ref="container" class="chart-canvas-box" />

    <!-- 加载中遮罩 -->
    <div v-if="store.loading" class="loading-overlay">
      <n-spin size="medium" />
    </div>
  </div>
</template>

<style scoped>
.intraday-wrapper {
  position: relative;
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  background: #ffffff;
  overflow: hidden;
}

.intraday-header {
  height: 32px;
  min-height: 32px;
  display: flex;
  align-items: center;
  padding: 0 16px;
  background: #f8fafc;
  border-bottom: 1px solid #e2e8f0;
  font-size: 13px;
  user-select: none;
  z-index: 10;
}

.header-items {
  display: flex;
  align-items: center;
  gap: 18px;
}

.info-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.label {
  color: #64748b;
  font-size: 12px;
}

.val {
  color: #0f172a;
  font-weight: 500;
}

.time-val {
  color: #475569;
  font-family: monospace;
}

.text-red {
  color: #dc2626 !important;
}

.text-green {
  color: #16a34a !important;
}

.text-blue {
  color: #2563eb !important;
}

.text-muted {
  color: #64748b !important;
}

.retry-btn {
  margin-left: 8px;
  padding: 1px 8px;
  font-size: 11px;
  border-radius: 4px;
  background: #eff6ff;
  color: #2563eb;
  border: 1px solid #bfdbfe;
  cursor: pointer;
}
.retry-btn:hover {
  background: #dbeafe;
}

.chart-canvas-box {
  flex: 1;
  width: 100%;
  min-height: 0;
  background: #ffffff;
}

.loading-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(255, 255, 255, 0.75);
  backdrop-filter: blur(2px);
  z-index: 20;
}
</style>
