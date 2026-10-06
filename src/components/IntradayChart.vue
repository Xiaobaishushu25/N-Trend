<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NSpin } from 'naive-ui'
import {
  AreaSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  createChart,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type UTCTimestamp,
} from 'lightweight-charts'
import { useIntradayStore } from '../stores/intraday'
import type { IntradayPoint } from '../types'

const props = defineProps<{
  symbol: string
}>()

const store = useIntradayStore()
const container = ref<HTMLDivElement | null>(null)

let chart: IChartApi | null = null
let priceSeries: ISeriesApi<'Area'> | null = null
let avgSeries: ISeriesApi<'Line'> | null = null
let volumeSeries: ISeriesApi<'Histogram'> | null = null
let prevSettleLine: IPriceLine | null = null

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

function initChart() {
  if (!container.value) return

  chart = createChart(container.value, {
    layout: {
      background: { type: ColorType.Solid, color: '#131722' },
      textColor: '#94a3b8',
    },
    grid: {
      vertLines: { color: 'rgba(255, 255, 255, 0.04)' },
      horzLines: { color: 'rgba(255, 255, 255, 0.04)' },
    },
    crosshair: {
      mode: CrosshairMode.Normal,
    },
    rightPriceScale: {
      borderColor: 'rgba(255, 255, 255, 0.1)',
      scaleMargins: {
        top: 0.1,
        bottom: 0.25, // 预留底部给成交量
      },
    },
    timeScale: {
      borderColor: 'rgba(255, 255, 255, 0.1)',
      timeVisible: true,
      secondsVisible: false,
    },
  })

  // 1. 现价折线（带平滑半透明面积阴影）
  priceSeries = chart.addSeries(AreaSeries, {
    lineColor: '#e2e8f0',
    topColor: 'rgba(56, 189, 248, 0.2)',
    bottomColor: 'rgba(56, 189, 248, 0.0)',
    lineWidth: 2,
    priceFormat: { type: 'price', precision: 1, minMove: 1 },
  })

  // 2. 均价线（明亮蓝色，对比鲜明）
  avgSeries = chart.addSeries(LineSeries, {
    color: '#38bdf8',
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

function updateChartData() {
  if (!chart || !priceSeries || !avgSeries || !volumeSeries) return

  const pts = store.points
  if (pts.length === 0) {
    priceSeries.setData([])
    avgSeries.setData([])
    volumeSeries.setData([])
    if (prevSettleLine) {
      priceSeries.removePriceLine(prevSettleLine)
      prevSettleLine = null
    }
    return
  }

  // 排序与去重时间戳
  const sorted = [...pts].sort((a, b) => parseTs(a.ts) - parseTs(b.ts))
  const uniquePts: IntradayPoint[] = []
  let lastT = -1
  for (const p of sorted) {
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

  priceSeries.setData(priceData)
  avgSeries.setData(avgData)
  volumeSeries.setData(volData)

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

  chart.timeScale().fitContent()
}

// 监听窗口大小缩放
function handleResize() {
  if (chart && container.value) {
    chart.resize(container.value.clientWidth, container.value.clientHeight)
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
        <button class="retry-btn" @click="store.load(props.symbol)">重试</button>
      </div>
      <div v-else-if="!store.loading && store.points.length === 0" class="header-items text-muted">
        今日暂无分时数据
        <button class="retry-btn" @click="store.load(props.symbol)">刷新</button>
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
  background: #131722;
  overflow: hidden;
}

.intraday-header {
  height: 32px;
  min-height: 32px;
  display: flex;
  align-items: center;
  padding: 0 16px;
  background: rgba(15, 23, 42, 0.7);
  border-bottom: 1px solid rgba(255, 255, 255, 0.05);
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
  color: #f1f5f9;
}

.time-val {
  color: #94a3b8;
  font-family: monospace;
}

.text-red {
  color: #ef4444 !important;
}

.text-green {
  color: #22c55e !important;
}

.text-blue {
  color: #38bdf8 !important;
}

.text-muted {
  color: #94a3b8 !important;
}

.retry-btn {
  margin-left: 8px;
  padding: 1px 8px;
  font-size: 11px;
  border-radius: 4px;
  background: rgba(56, 189, 248, 0.2);
  color: #38bdf8;
  border: 1px solid rgba(56, 189, 248, 0.4);
  cursor: pointer;
}
.retry-btn:hover {
  background: rgba(56, 189, 248, 0.35);
}

.chart-canvas-box {
  flex: 1;
  width: 100%;
  min-height: 0;
}

.loading-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(19, 23, 34, 0.45);
  backdrop-filter: blur(2px);
  z-index: 20;
}
</style>
