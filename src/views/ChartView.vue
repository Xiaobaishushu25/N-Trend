<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  NButton,
  NEmpty,
  NIcon,
  NRadioButton,
  NRadioGroup,
  NScrollbar,
  NSpace,
  NText,
} from 'naive-ui'
import { ArrowLeft, Eye, EyeOff, List } from '@vicons/tabler'
import KLineChart from '../components/KLineChart.vue'
import { api, onDataUpdated, onScanCompleted } from '../services/api'
import { useSymbolsStore } from '../stores/symbols'
import { useKlinesStore } from '../stores/klines'
import { useScansStore } from '../stores/scans'
import type { MarketSnapshot, PatternDto, SignalOutcome, Timeframe } from '../types'

const route = useRoute()
const router = useRouter()
const symbolsStore = useSymbolsStore()
const klinesStore = useKlinesStore()
const scansStore = useScansStore()

const symbol = computed(() => String(route.params.symbol || ''))
const timeframe = ref<Timeframe>('15m')
const timeframes: Timeframe[] = ['5m', '15m', '30m', '60m', '120m', '240m', '1d']

const currentSymbol = computed(() => symbolsStore.symbols.find((s) => s.code === symbol.value))

/** 最近一次扫描识别出的该品种全部N形态（策略基于 15m/60m，与图表显示级别无关） */
const signals = computed<PatternDto[]>(() => {
  if (!scansStore.latest) return []
  return scansStore.latest.signals
    .filter((s) => s.symbol === symbol.value)
    .map((s) => s as unknown as PatternDto)
})

const latestClose = computed(() =>
  klinesStore.rows.length ? klinesStore.rows[klinesStore.rows.length - 1].close : null,
)

/** 被点击隐藏的形态编号（用于控制K线图上的展示） */
const hiddenNumbers = ref<Set<number>>(new Set())

/** 左侧品种列表开关与行情快照 */
const showList = ref(true)
const snapshots = ref<Record<string, MarketSnapshot>>({})

/** 行情快照：右侧信息卡的实时价与涨跌幅 */
const snapshot = computed(() => snapshots.value[symbol.value] ?? null)
/** 信息卡主价格：优先用实时快照，缺失时回退到K线最新收盘 */
const quotePrice = computed(() => snapshot.value?.latest ?? latestClose.value)
/** 涨跌颜色：与左侧品种列表一致，取自快照涨跌幅 */
const quoteColor = computed(() => trendColor(snapshot.value?.change_pct ?? null))
/** 涨跌点数：由最新价与涨跌幅反推上一根收盘价再相减，保证与涨跌幅口径一致 */
const quotePoints = computed(() => {
  const latest = snapshot.value?.latest
  const pct = snapshot.value?.change_pct
  if (latest == null || pct == null) return null
  return (latest * pct) / (100 + pct)
})
/** 涨跌胶囊的背景色（浅色tint，跟随涨跌方向） */
function quoteBg(v: number | null) {
  if (v == null || v === 0) return 'rgba(148, 163, 184, 0.12)'
  return v > 0 ? 'rgba(224, 49, 49, 0.12)' : 'rgba(15, 157, 88, 0.12)'
}
/** 带符号数字格式：+1.5 / -0.3 */
function fmtSigned(v: number | null) {
  if (v == null) return '—'
  return `${v >= 0 ? '+' : ''}${v.toFixed(1)}`
}

const listRows = computed(() =>
  symbolsStore.symbols.map((s) => {
    const signal = signalBySymbol.value[s.code] ?? null
    return {
      code: s.code,
      name: s.name !== s.code ? s.name : '',
      latest: snapshots.value[s.code]?.latest ?? null,
      changePct: snapshots.value[s.code]?.change_pct ?? null,
      signal,
      sigType: signal ? sigType(signal.state) : '',
    }
  }),
)

/** 每个品种取优先级最高的信号：即将触发 > 当前已触发 > 接近时效 > 过时；同级按触发/预警时间新的优先 */
const signalBySymbol = computed<Record<string, SignalOutcome | null>>(() => {
  const out: Record<string, SignalOutcome | null> = {}
  const latest = scansStore.latest
  if (!latest) return out
  const rankOf = (s: SignalOutcome): number => {
    if (s.state === '即将触发') return 0
    if (s.state === '当前已触发') return 1
    if (s.state === '已触发，接近时效边界') return 2
    return 3
  }
  const tsOf = (s: SignalOutcome): number => {
    const raw = s.trigger_ts || s.warning_ts
    return raw ? new Date(raw.replace(' ', 'T') + 'Z').getTime() : 0
  }
  for (const s of latest.signals) {
    const prev = out[s.symbol]
    if (!prev) {
      out[s.symbol] = s
      continue
    }
    const a = rankOf(s)
    const b = rankOf(prev)
    if (a < b) {
      out[s.symbol] = s
    } else if (a === b) {
      const ta = tsOf(s)
      const tb = tsOf(prev)
      if (ta > tb || (ta === tb && s.score > prev.score)) out[s.symbol] = s
    }
  }
  return out
})

/** 信号状态 → 列表里的短标签 */
function sigLabel(state: string) {
  switch (state) {
    case '即将触发':
      return '即将触发'
    case '当前已触发':
      return '已触发'
    case '已触发，接近时效边界':
      return '接近时效'
    case '已过时，仅复盘':
      return '过时'
    default:
      return state
  }
}

/** 信号状态 → 样式类型 */
function sigType(state: string) {
  if (state === '即将触发') return 'pending'
  if (state === '当前已触发') return 'triggered'
  if (state === '已触发，接近时效边界') return 'stale'
  return 'expired'
}

/** 悬停提示：形态编号、方向、级别、状态与触发/预警时间 */
function sigTitle(s: SignalOutcome) {
  const dir = s.direction === 'up' ? '做多' : s.direction === 'down' ? '做空' : s.direction
  const level = s.level === 'fine' ? '精细' : s.level === 'large' ? '较大' : s.level
  const t = s.trigger_ts || s.warning_ts || ''
  return `#${s.number} ${dir} ${level}N ${s.state}${t ? ` ${t}` : ''} 评分 ${s.score.toFixed(2)}`
}

function trendColor(v: number | null) {
  return v == null ? '#94a3b8' : v > 0 ? '#e03131' : v < 0 ? '#0f9d58' : '#94a3b8'
}
function fmtPrice(v: number | null) {
  return v == null ? '—' : v.toFixed(1)
}
function fmtChange(v: number | null) {
  return v == null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
}

async function loadSnapshots() {
  try {
    const list = await api.getMarketSnapshot()
    snapshots.value = Object.fromEntries(list.map((s) => [s.code, s]))
  } catch {
    // 快照加载失败不影响看图
  }
}

const visibleSignals = computed<PatternDto[]>(() =>
  signals.value.filter((s) => !hiddenNumbers.value.has(s.number)),
)

function isHidden(num: number) {
  return hiddenNumbers.value.has(num)
}

/** 普通滚轮在图表区域累积滚动量并切换上下品种（按住Ctrl时交给图表缩放） */
const wheelAcc = ref(0)
let lastSwitchAt = 0
const WHEEL_SWITCH_THRESHOLD = 20
const WHEEL_SWITCH_INTERVAL = 150

function switchSymbol(dir: number) {
  const list = symbolsStore.symbols
  if (!list.length || !symbol.value) return
  const idx = list.findIndex((s) => s.code === symbol.value)
  const next = list[(idx + dir + list.length) % list.length]
  if (next && next.code !== symbol.value) {
    router.push({ name: 'chart', params: { symbol: next.code } })
  }
}

function handleChartWheel(e: WheelEvent) {
  if (e.ctrlKey || e.shiftKey || e.altKey || e.metaKey) return
  const now = Date.now()
  if (now - lastSwitchAt < WHEEL_SWITCH_INTERVAL) return
  wheelAcc.value += e.deltaY
  if (Math.abs(wheelAcc.value) < WHEEL_SWITCH_THRESHOLD) return
  const dir = wheelAcc.value > 0 ? 1 : -1
  wheelAcc.value = 0
  lastSwitchAt = now
  switchSymbol(dir)
}

function togglePattern(num: number) {
  const next = new Set(hiddenNumbers.value)
  if (next.has(num)) {
    next.delete(num)
  } else {
    next.add(num)
  }
  hiddenNumbers.value = next
}

function dirText(d: string) {
  return d === 'up' ? '做多' : d === 'down' ? '做空' : d
}

function levelText(l: string) {
  return l === 'fine' ? '精细' : l === 'large' ? '较大' : l
}

function stateType(state: string): 'info' | 'success' | 'warning' | 'default' | 'error' {
  if (state === '即将触发') return 'info'
  if (state === '当前已触发') return 'success'
  if (state === '已触发，接近时效边界') return 'warning'
  return 'default'
}

/** 与入场的点差绝对值（单位：点），不区分多空方向 */
function fmtDelta(delta: number) {
  return Math.abs(delta).toFixed(1)
}

let unlisteners: (() => void)[] = []

watch([symbol, timeframe], async () => {
  hiddenNumbers.value = new Set()
  if (symbol.value) await klinesStore.load(symbol.value, timeframe.value, 1200)
})

onMounted(async () => {
  unlisteners.push(
    await onScanCompleted((result) => {
      scansStore.ingest(result)
      loadSnapshots()
    }),
  )
  unlisteners.push(await onDataUpdated(() => loadSnapshots()))
  await symbolsStore.load()
  // 进入页面立即拉一次行情快照，避免左侧价格/涨幅要等下一次刷新或扫描事件才显示
  loadSnapshots()
  if (!scansStore.latest) {
    try {
      await scansStore.runScan()
    } catch {
      // 无数据时扫描失败不影响看图
    }
  }
  if (symbol.value) await klinesStore.load(symbol.value, timeframe.value, 1200)
})

onBeforeUnmount(() => {
  for (const fn of unlisteners) fn()
})
</script>

<template>
  <div class="chart-page">
    <div class="topbar">
      <n-space align="center">
        <n-button quaternary size="small" @click="router.push({ name: 'dashboard' })">
          <template #icon>
            <n-icon :component="ArrowLeft" />
          </template>
          返回列表
        </n-button>
        <n-button quaternary size="small" @click="showList = !showList">
          <template #icon>
            <n-icon :component="List" />
          </template>
          {{ showList ? '收起列表' : '品种列表' }}
        </n-button>
        <n-text strong style="font-size: 17px">{{ symbol }}</n-text>
        <n-text depth="3">
          {{ currentSymbol?.name && currentSymbol.name !== symbol ? currentSymbol.name : '' }}
        </n-text>
        <n-text v-if="latestClose !== null" strong style="font-size: 16px">
          {{ latestClose.toFixed(1) }}
        </n-text>
      </n-space>
      <n-radio-group v-model:value="timeframe" size="small">
        <n-radio-button v-for="t in timeframes" :key="t" :value="t" :label="t" />
      </n-radio-group>
    </div>

    <div class="main">
      <div v-if="showList" class="symbol-list">
        <div class="sl-title">品种</div>
        <n-scrollbar style="flex: 1">
          <div
            v-for="row in listRows"
            :key="row.code"
            class="sl-row"
            :class="[
              { active: row.code === symbol },
              row.sigType === 'pending' ? 'has-pending' : '',
              row.sigType === 'triggered' ? 'has-triggered' : '',
              row.sigType === 'stale' ? 'has-stale' : '',
            ]"
            @click="router.push({ name: 'chart', params: { symbol: row.code } })"
          >
            <div class="sl-main">
              <span class="sl-name">{{ row.name || row.code }}</span>
              <span class="sl-code">{{ row.code }}</span>
            </div>
            <span
              v-if="row.signal"
              class="sl-sig"
              :class="'is-' + row.sigType"
              :title="sigTitle(row.signal)"
            >
              {{ sigLabel(row.signal.state) }}
            </span>
            <div class="sl-quote">
              <span class="sl-price" :style="{ color: trendColor(row.changePct) }">
                {{ fmtPrice(row.latest) }}
              </span>
              <span class="sl-change" :style="{ color: trendColor(row.changePct) }">
                {{ fmtChange(row.changePct) }}
              </span>
            </div>
          </div>
        </n-scrollbar>
      </div>

      <div class="chart-col" @wheel.prevent="handleChartWheel">
        <KLineChart
          v-if="symbol && klinesStore.rows.length"
          :symbol="symbol"
          :timeframe="timeframe"
          :rows="klinesStore.rows"
          :signals="visibleSignals"
          :loading="klinesStore.loading"
        />
        <n-empty
          v-else
          class="chart-empty"
          description="暂无K线数据，请先在列表页刷新数据"
        />
      </div>

      <div class="info-col">
        <div class="info-card">
          <div class="info-head">
            <div class="info-head-left">
              <span class="info-symbol">{{ symbol }}</span>
              <span v-if="currentSymbol?.name && currentSymbol.name !== symbol" class="info-name">
                {{ currentSymbol.name }}
              </span>
            </div>
            <span class="info-exchange">{{ currentSymbol?.exchange || '—' }}</span>
          </div>
          <div class="info-quote">
            <div class="info-quote-item">
              <span class="info-latest-label">最新价</span>
              <span class="info-latest-value" :style="{ color: quoteColor }">
                {{ fmtPrice(quotePrice) }}
              </span>
            </div>
            <div v-if="snapshot?.change_pct != null" class="info-quote-item">
              <span class="info-latest-label">涨跌点</span>
              <span class="info-points" :style="{ color: quoteColor }">
                {{ fmtSigned(quotePoints) }}点
              </span>
            </div>
            <div v-if="snapshot?.change_pct != null" class="info-quote-item">
              <span class="info-latest-label">涨跌幅</span>
              <span
                class="info-change"
                :style="{ color: quoteColor, background: quoteBg(snapshot.change_pct) }"
              >
                {{ fmtChange(snapshot.change_pct) }}
              </span>
            </div>
          </div>
        </div>

        <div class="patterns-card">
          <div class="patterns-title">全部 N 形态（{{ signals.length }}）</div>
          <n-scrollbar style="flex: 1">
            <div v-if="signals.length" class="patterns-list">
              <div
                v-for="s in signals"
                :key="s.number"
                class="pattern-card"
                :class="[
                  s.direction === 'up' ? 'is-up' : 'is-down',
                  { 'is-active': s.active, 'is-hidden': isHidden(s.number) },
                ]"
                :title="isHidden(s.number) ? '点击在K线图上显示该形态' : '点击在K线图上隐藏该形态'"
                @click="togglePattern(s.number)"
              >
                <div class="pc-head">
                  <div class="pc-badges">
                    <span class="pc-num">#{{ s.number }}</span>
                    <span class="pc-dir">{{ dirText(s.direction) }} {{ levelText(s.level) }}N</span>
                    <span class="pc-grade">{{ s.grade }}</span>
                  </div>
                  <n-icon
                  :component="isHidden(s.number) ? EyeOff : Eye"
                  size="17"
                  :color="isHidden(s.number) ? '#cbd5e1' : '#94a3b8'"
                  style="margin-top: 2px"
                />
                <div class="pc-score">
                    <span class="pc-score-num">{{ s.score.toFixed(2) }}</span>
                    <span class="pc-score-label">评分</span>
                  </div>
                </div>

                <div class="pc-state" :class="stateType(s.state)">
                  <span class="dot"></span>{{ s.state }}
                </div>

                <div class="pc-prices">
                  <div class="pc-price">
                    <span>入场</span>
                    <b>{{ s.entry.toFixed(1) }}</b>
                  </div>
                  <div class="pc-price">
                    <span>止损</span>
                    <b>{{ s.stop.toFixed(1) }}</b>
                    <em class="pc-delta is-stop">{{ fmtDelta(s.stop - s.entry) }}点</em>
                  </div>
                  <div class="pc-price">
                    <span>目标</span>
                    <b>{{ s.target.toFixed(1) }}</b>
                    <em class="pc-delta is-target">{{ fmtDelta(s.target - s.entry) }}点</em>
                  </div>
                  <div class="pc-price">
                    <span>RR</span>
                    <b>{{ s.rr.toFixed(2) }}</b>
                  </div>
                </div>

                <div class="pc-legs">
                  <span>a段 {{ s.a_bars }}根 / {{ s.a_move.toFixed(1) }}点</span>
                  <span>b段 {{ s.b_bars }}根 / {{ s.b_move.toFixed(1) }}点</span>
                  <span>回撤 {{ (s.retracement * 100).toFixed(1) }}%</span>
                </div>

                <div class="pc-note">{{ s.note }}</div>
              </div>
            </div>
            <div v-else class="patterns-empty">当前品种暂无识别出的N形态</div>
          </n-scrollbar>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.chart-page {
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: #f5f7fa;
}
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 16px;
  background: #fff;
  border-bottom: 1px solid #e5e7eb;
}
.main {
  flex: 1;
  min-height: 0;
  display: flex;
  gap: 10px;
  padding: 10px;
}
.symbol-list {
  flex: 0 0 210px;
  width: 210px;
  min-width: 0;
  background: #fff;
  border-radius: 10px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.06);
}
.sl-title {
  padding: 10px 12px 6px;
  font-size: 13px;
  font-weight: 600;
  color: #334155;
}
.sl-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 7px 12px;
  cursor: pointer;
  border-left: 3px solid transparent;
  transition: background 0.15s;
}
.sl-row:hover {
  background: #f6f8fa;
}
.sl-row.active {
  background: rgba(22, 119, 255, 0.06);
  border-left-color: #1677ff;
}
.sl-main {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-width: 0;
}
.sl-name {
  font-size: 12px;
  color: #1f2329;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.sl-code {
  font-size: 10px;
  color: #94a3b8;
}
.sl-quote {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  font-variant-numeric: tabular-nums;
}
.sl-price {
  font-size: 12px;
  font-weight: 600;
}
.sl-change {
  font-size: 10px;
}
.sl-row.has-pending {
  background: rgba(22, 119, 255, 0.05);
}
.sl-row.has-triggered {
  background: rgba(15, 157, 88, 0.06);
}
.sl-row.has-stale {
  background: rgba(249, 168, 37, 0.08);
}
.sl-sig {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: 10px;
  font-weight: 600;
  line-height: 1;
  padding: 2px 5px;
  border-radius: 999px;
  white-space: nowrap;
}
.sl-sig::before {
  content: '';
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
}
.sl-sig.is-pending {
  color: #1677ff;
  background: rgba(22, 119, 255, 0.12);
}
.sl-sig.is-triggered {
  color: #0f9d58;
  background: rgba(15, 157, 88, 0.12);
}
.sl-sig.is-stale {
  color: #b45309;
  background: rgba(249, 168, 37, 0.16);
}
.sl-sig.is-expired {
  color: #64748b;
  background: rgba(148, 163, 184, 0.14);
}
.chart-col {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.chart-empty {
  flex: 1;
  min-height: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}
.info-col {
  /* 右侧信息栏固定宽度（原 400px，收窄到 350px 试试） */
  flex: 0 0 350px;
  width: 350px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  overflow: hidden;
}
.info-card {
  background: linear-gradient(180deg, #ffffff 0%, #f8fafc 100%);
  border: 1px solid #eef0f3;
  border-radius: 10px;
  padding: 14px 16px;
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.06);
}
.info-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.info-head-left {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}
.info-symbol {
  font-size: 20px;
  font-weight: 700;
  letter-spacing: 0.5px;
  color: #1f2329;
  font-variant-numeric: tabular-nums;
}
.info-name {
  font-size: 15px;
  font-weight: 600;
  color: #475569;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.info-exchange {
  flex: 0 0 auto;
  font-size: 11px;
  font-weight: 600;
  color: #475569;
  background: #f1f5f9;
  border: 1px solid #eef0f3;
  border-radius: 999px;
  padding: 3px 10px;
}
.info-quote {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 24px;
  margin-top: 12px;
}
.info-quote-item {
  flex: 1 1 0;
  min-width: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 3px;
}
.info-latest-label {
  font-size: 11px;
  color: #94a3b8;
}
.info-latest-value {
  font-size: 30px;
  line-height: 1.15;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.5px;
  white-space: nowrap;
}
.info-points {
  font-size: 16px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  line-height: 1.15;
  white-space: nowrap;
}
.info-change {
  font-size: 14px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  border-radius: 999px;
  padding: 3px 12px;
  line-height: 1.4;
  white-space: nowrap;
}
.patterns-card {
  flex: 1;
  min-height: 0;
  background: #fff;
  border-radius: 10px;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.06);
}
.patterns-title {
  font-size: 13px;
  font-weight: 600;
  color: #334155;
  margin-bottom: 8px;
}
.patterns-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding-right: 4px;
}
.patterns-empty {
  padding: 24px 0;
  text-align: center;
  font-size: 13px;
  color: #94a3b8;
}

.pattern-card {
  position: relative;
  border: 1px solid #eef0f3;
  border-left-width: 4px;
  border-radius: 10px;
  padding: 10px 12px;
  background: #fff;
  cursor: pointer;
  transition:
    box-shadow 0.2s,
    opacity 0.2s;
}
.pattern-card:hover {
  box-shadow: 0 3px 12px rgba(15, 23, 42, 0.12);
}
.pattern-card.is-hidden {
  opacity: 0.45;
}
.pattern-card.is-hidden:hover {
  opacity: 0.7;
}
.pattern-card.is-up {
  border-left-color: #e03131;
}
.pattern-card.is-down {
  border-left-color: #0f9d58;
}
.pattern-card.is-active {
  box-shadow: 0 2px 10px rgba(15, 23, 42, 0.08);
}
.pc-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}
.pc-badges {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.pc-num {
  font-size: 13px;
  font-weight: 700;
  color: #1f2329;
}
.pc-dir {
  font-size: 12px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 999px;
}
.pattern-card.is-up .pc-dir {
  color: #e03131;
  background: rgba(224, 49, 49, 0.08);
}
.pattern-card.is-down .pc-dir {
  color: #0f9d58;
  background: rgba(15, 157, 88, 0.08);
}
.pc-grade {
  font-size: 11px;
  font-weight: 600;
  color: #7c5cff;
  background: rgba(124, 92, 255, 0.08);
  padding: 2px 7px;
  border-radius: 999px;
}
.pc-score {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  line-height: 1.1;
}
.pc-score-num {
  font-size: 16px;
  font-weight: 700;
  color: #1f2329;
}
.pattern-card.is-active .pc-score-num {
  color: #e03131;
}
.pc-score-label {
  font-size: 10px;
  color: #94a3b8;
}
.pc-state {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  margin-top: 8px;
  padding: 5px 12px;
  border-radius: 999px;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.5px;
  background: #f1f5f9;
  color: #475569;
}
.pc-state .dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: currentColor;
}
.pc-state.success {
  color: #0f9d58;
  background: rgba(15, 157, 88, 0.12);
}
.pc-state.info {
  color: #1677ff;
  background: rgba(22, 119, 255, 0.12);
}
.pc-state.warning {
  color: #b45309;
  background: rgba(249, 168, 37, 0.16);
}
.pc-state.error {
  color: #e03131;
  background: rgba(224, 49, 49, 0.1);
}
.pc-prices {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 6px;
  margin-top: 10px;
}
.pc-price {
  background: #f6f8fa;
  border-radius: 8px;
  padding: 6px 8px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.pc-price span {
  font-size: 10px;
  color: #94a3b8;
}
.pc-price b {
  font-size: 13px;
  color: #1f2329;
  font-variant-numeric: tabular-nums;
}
.pc-delta {
  font-style: normal;
  font-size: 10px;
  font-weight: 600;
  line-height: 1.2;
  font-variant-numeric: tabular-nums;
}
.pc-delta.is-stop {
  color: #0f9d58;
}
.pc-delta.is-target {
  color: #e03131;
}
.pc-legs {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  margin-top: 8px;
  font-size: 11px;
  color: #64748b;
}
.pc-note {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed #eef0f3;
  font-size: 11px;
  line-height: 1.6;
  color: #94a3b8;
}
</style>








