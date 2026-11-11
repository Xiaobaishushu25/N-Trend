<script setup lang="ts">
import { computed, onMounted, ref, shallowRef, watch } from 'vue'
import { useRouter } from 'vue-router'
import KLineChart from '../components/KLineChart.vue'
import { replayBarLabel, type ReplayBar, type ZoneReplayOverlay } from '../utils/zoneReplay'

type Arm = 'baseline' | 'extension'
type Timeframe = '60m' | '30m' | '5m'
interface Trade {
  signal_end: string
  entry_ns: string
  exit_ns: string
  entry: number
  exit: number
  initial_stop: number
  distance: number
  planned_risk: number
  net: number
  reason: string
  marks: { kind: 'activate' | 'stop'; time_ns: string; price?: number; effective_ns?: string }[]
}
interface ReplayCase {
  id: string
  label: string
  contract: string
  variant: 'direct' | 'retest'
  side: 1 | -1
  clearance: number
  closeLocation: number | null
  formed: string
  confirmed: string
  firstClose: string
  breakout: string
  zones: ZoneReplayOverlay['zones']
  rows: Record<Timeframe, ReplayBar[]>
  outcomes: Record<Arm, { status: string; trade: Trade | null }>
}

const router = useRouter()
const cases = shallowRef<ReplayCase[]>([])
const selected = ref('')
const arm = ref<Arm>('baseline')
const timeframe = ref<Timeframe>('60m')
const error = ref('')
const arms = { baseline: '原滚动版', extension: '超过 1 ATR 跳过' }
const timeframes = { '60m': '60 分钟 · 箱体', '30m': '30 分钟 · 入场', '5m': '5 分钟 · 撮合' }
const reasons: Record<string, string> = {
  stop: '初始止损', gap_stop: '跳空止损', trail_stop: '跟踪止损',
  trail_gap_stop: '跟踪止损（跳空）', trail_close_breach: '收盘越过新跟踪止损',
  max_hold: '达到持仓时限', box_reentry: '收回箱体', roll: '换月退出',
  filtered: '信号被过滤', position_active: '同品种已有持仓', shared_risk: '总计划风险超限',
  margin: '保证金超限', no_fill: '未成交',
}
const intrabarExit = new Set(['stop', 'trail_stop'])
const reason = (value: string) => reasons[value] ?? value
const current = computed(() => cases.value.find(item => item.id === selected.value))
const rows = computed(() => current.value?.rows[timeframe.value] ?? [])
const outcome = computed(() => current.value?.outcomes[arm.value])
const trade = computed(() => outcome.value?.trade)
const otherTrade = computed(() => current.value?.outcomes[arm.value === 'baseline' ? 'extension' : 'baseline'].trade)

function result(outcome: ReplayCase['outcomes'][Arm]) {
  if (!outcome.trade) return reason(outcome.status)
  const net = outcome.trade.net
  return `${net > 0 ? '+' : ''}${net.toFixed(2)} 元 · ${reason(outcome.trade.reason)}`
}

const overlay = computed<ZoneReplayOverlay | null>(() => {
  const item = current.value
  if (!item) return null
  const label = (time: string, open = false) => replayBarLabel(rows.value, time, open)
  const formed = label(item.formed), confirmed = label(item.confirmed)
  const first = label(item.firstClose), breakout = label(item.breakout)
  if (!formed || !confirmed || !first || !breakout) return null
  const t = trade.value
  const entry = t && label(t.entry_ns, true)
  const exit = t && label(t.exit_ns, !intrabarExit.has(t.reason))
  const comparison = otherTrade.value
  const comparisonExit = comparison && label(comparison.exit_ns, !intrabarExit.has(comparison.reason))
  const landmarks: { ts: string; label: string }[] = []
  if (t && item.variant === 'retest') {
    const retest = label(t.signal_end)
    if (retest) landmarks.push({ ts: retest, label: '30m 回踩确认' })
  }
  const trailing = new Map<string, number>()
  for (const mark of t?.marks ?? []) {
    if (mark.kind === 'activate') {
      const ts = label(mark.time_ns)
      if (ts) landmarks.push({ ts, label: '触发 2R 跟踪' })
    } else if (mark.effective_ns && mark.price !== undefined) {
      const ts = label(mark.effective_ns, true)
      if (ts && (!exit || ts <= exit)) trailing.set(ts, mark.price)
    }
  }
  const status = outcome.value?.status
  return {
    formed, confirmed, end: breakout, zones: item.zones,
    confirmedLabel: '箱体确认', visit: first, visitLabel: '第 1 根收在箱外',
    reaction: { ts: breakout, label: status === 'filtered'
      ? '第 2 根确认 · 过滤跳过'
      : status && !t ? `第 2 根确认 · ${reason(status)}` : '第 2 根收在箱外 · 突破确认' },
    landmarks,
    viewStart: timeframe.value === '30m' ? first : undefined,
    viewEnd: !t ? comparisonExit ?? undefined : undefined,
    trade: t && entry && exit ? {
      entry, exit, entryPrice: t.entry, exitPrice: t.exit, stop: t.initial_stop, side: item.side,
      target: t.entry + item.side * 2 * t.distance, exitLabel: reason(t.reason),
      trailing: [...trailing].map(([ts, price]) => ({ ts, price })),
    } : null,
  }
})

watch(current, item => {
  if (!item) return
  arm.value = item.outcomes.baseline.trade ? 'baseline' : 'extension'
  timeframe.value = '60m'
})

onMounted(async () => {
  try {
    const response = await fetch(`${import.meta.env.BASE_URL}research/rolling-breakout/cases.json`)
    if (!response.ok) throw new Error('无法读取箱体突破复盘样例')
    const data: ReplayCase[] = await response.json()
    if (!Array.isArray(data) || !data.length) throw new Error('箱体突破复盘样例为空')
    cases.value = data
    selected.value = data[0].id
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '载入箱体突破复盘失败'
  }
})
</script>

<template>
  <main class="breakout-replay">
    <header class="replay-toolbar">
      <button type="button" @click="router.back()">返回</button>
      <strong>箱体突破复盘</strong>
      <label>案例
        <select v-model="selected">
          <option v-for="item in cases" :key="item.id" :value="item.id">{{ item.label }} · {{ item.breakout.slice(0, 10) }}</option>
        </select>
      </label>
      <label>图表
        <select v-model="timeframe">
          <option v-for="(name, key) in timeframes" :key="key" :value="key">{{ name }}</option>
        </select>
      </label>
      <span class="muted">共享账户回测 · 压力成本 · 北京时间</span>
    </header>
    <section v-if="current" class="case-info">
      <div class="case-line">
        <strong>{{ current.contract }} · {{ current.side === 1 ? '做多' : '做空' }}</strong>
        <span>{{ current.variant === 'direct' ? '60 分钟确认后直接进场' : '60 分钟确认方向，30 分钟回踩后进场' }}</span>
        <span>突破 {{ current.breakout.slice(0, 16) }} · 收盘离箱 {{ current.clearance.toFixed(2) }} ATR</span>
        <span v-if="current.closeLocation !== null">顺势收盘位置 {{ Math.round(current.closeLocation * 100) }}%</span>
      </div>
      <div class="case-line comparison">
        <button v-for="(name, key) in arms" :key="key" type="button" :aria-pressed="arm === key"
          @click="arm = key">{{ name }} · {{ result(current.outcomes[key]) }}</button>
      </div>
      <div v-if="trade" class="case-line">
        <span v-if="current.variant === 'retest'">回踩确认 {{ trade.signal_end.slice(0, 16) }}</span>
        <span>入场 {{ trade.entry_ns.slice(0, 16) }} @ {{ trade.entry }}</span>
        <span>初始止损 {{ trade.initial_stop }} · 计划风险 {{ trade.planned_risk.toFixed(0) }} 元</span>
        <span>退出 {{ trade.exit_ns.slice(0, 16) }} @ {{ trade.exit }}</span>
        <details v-if="trade.marks.length">
          <summary>查看 {{ trade.marks.length }} 条跟踪记录</summary>
          <ol>
            <li v-for="mark in trade.marks" :key="`${mark.time_ns}:${mark.kind}`">
              {{ mark.time_ns }} · {{ mark.kind === 'activate' ? '启动 2R 跟踪' : `止损调至 ${mark.price}，${mark.effective_ns} 生效` }}
            </li>
          </ol>
        </details>
      </div>
      <div v-else-if="outcome" class="case-line">本方案 · {{ reason(outcome.status) }}</div>
      <div class="case-line muted">紫色为箱体上下轨，确认前用虚线标示。红色为初始止损，蓝线为 2R 参考位，粉色为跟踪止损。</div>
    </section>
    <KLineChart v-if="current && overlay" class="replay-chart"
      :key="`${current.id}:${timeframe}`" :symbol="current.contract" :timeframe="timeframe"
      :rows="rows" :signals="[]" :zone-replay="overlay" :focus-ts="overlay.reaction?.ts"
      :focus-key="`${current.id}:${timeframe}:${arm}`" />
    <p v-else class="replay-message" :role="error ? 'alert' : 'status'">{{ error || (current ? '该周期缺少标注所需的行情' : '正在加载复盘样例…') }}</p>
  </main>
</template>

<style scoped>
.breakout-replay { height: 100%; min-height: 0; display: flex; flex-direction: column; }
.replay-toolbar, .case-line { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 14px; }
.replay-toolbar, .case-info { padding: 8px 12px; border-bottom: 1px solid #cbd5e1; }
.case-info { display: grid; gap: 6px; font-size: 12px; }
.replay-toolbar label { display: inline-flex; align-items: center; gap: 5px; }
button, select { color: #334155; background: #fff; border: 1px solid #94a3b8; border-radius: 4px; padding: 4px 8px; }
button[aria-pressed="true"] { color: #1e3a8a; border-color: #2563eb; background: #eff6ff; }
.muted { color: #64748b; font-size: 12px; }
details ol { max-height: 100px; overflow-y: auto; margin: 4px 0; }
.replay-chart { flex: 1; min-height: 0; }
.replay-message { margin: 12px; }
</style>
