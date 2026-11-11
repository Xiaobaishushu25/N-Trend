<script setup lang="ts">
import { computed, onMounted, ref, shallowRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import KLineChart from '../components/KLineChart.vue'
import { parseZoneReplay, replayBarLabel, type ZoneReplayOverlay, type ZoneReplayPackage } from '../utils/zoneReplay'

const router = useRouter()
const route = useRoute()
const packages = shallowRef<ZoneReplayPackage[]>([])
const timeframe = ref<'15m' | '30m'>('15m')
const packageId = ref('')
const structure = ref('')
const reaction = ref('')
const lossesOnly = ref(false)
const tradesOnly = ref(true)
const arm = ref('fixed_on')
const executionView = ref(false)
const activatedOnly = ref(false)
const visitId = ref('')
const tradeIndex = ref(0)
const errors = ref('')
const loading = ref(false)
const names = { zone: '单区', box3: '三点箱体', box4: '四点箱体' }
const reactions = { reversal: '收回', breakout: '突破', chop: '纠缠', interrupted: '中断', incomplete: '期末未完成' }
const products: Record<string, string> = { CF: '棉花', EG: '乙二醇' }
const arms = { fixed_on: '固定 2R · 无跟随开', fixed_off: '固定 2R · 无跟随关',
  trail_on: '2R 后跟踪 · 无跟随开', trail_off: '2R 后跟踪 · 无跟随关' }
const reasons: Record<string, string> = {
  stop: '初始止损', gap_stop: '跳空止损', deferred_stop: '止损延迟成交', target: '固定 2R 止盈',
  gap_target: '开盘已达止盈条件', trail_stop: '跟踪止损', trail_gap_stop: '跟踪止损（跳空）',
  trail_deferred_stop: '跟踪止损（延迟）', trail_close_breach: '收盘越过新止损，下一开盘退出',
  no_follow: '无跟随退出', max_hold: '达到持仓上限', roll: '换月退出',
  fold_boundary: '轮次边界', account_halt: '账户回撤停机', data_end: '行情截止时仍持仓',
  per_lot_risk: '单手计划风险超过 600 元', shared_risk: '总计划风险超过 900 元',
  position_active: '同品种已有持仓', margin: '保证金超限', contract_changed: '下一开盘已换月',
  missing_scheduled_entry: '预定入场柱缺失', locked_or_invalid_entry: '入场报价不可成交',
  invalid_entry_quote: '入场报价无效', gap_through_stop: '开盘已越过初始止损',
  gap_through_target: '开盘已越过目标',
}
const closeReasons = ['stop', 'target', 'trail_stop']
const id = (p: ZoneReplayPackage) => `${p.fold}:${p.product}:${p.timeframe}`
const available = computed(() => packages.value.filter(p => p.timeframe === timeframe.value))
const packet = computed(() => available.value.find(p => id(p) === packageId.value) ?? available.value[0])
const showFiveMinute = computed(() => executionView.value && !!packet.value?.fiveMinuteBars?.length)
const following = computed(() => !!packet.value?.recent && arm.value.startsWith('trail_'))
const zones = computed(() => new Map(packet.value?.structures.map(s => [s.id, s])))
const tradesByVisit = computed(() => {
  const byVisit = new Map<string, NonNullable<typeof packet.value>['trades']>()
  for (const trade of packet.value?.trades ?? []) {
    if (packet.value?.recent && trade.policy !== arm.value) continue
    const rows = byVisit.get(trade.event_id) ?? []
    rows.push(trade)
    byVisit.set(trade.event_id, rows)
  }
  return byVisit
})
const visits = computed(() => (packet.value?.visits ?? []).filter(v =>
  (!structure.value || zones.value.get(v.structure_id)?.kind === structure.value) &&
  (!reaction.value || v.reaction === reaction.value) &&
  (!packet.value?.recent || !tradesOnly.value || tradesByVisit.value.has(v.id)) &&
  (!following.value || !activatedOnly.value ||
    tradesByVisit.value.get(v.id)?.some(t => t.marks?.some(m => m.kind === 'activate'))) &&
  (!lossesOnly.value || tradesByVisit.value.get(v.id)?.some(t => t.net < 0)),
))
const visit = computed(() => visits.value.find(v => v.id === visitId.value) ?? visits.value[0])
const zone = computed(() => visit.value && zones.value.get(visit.value.structure_id))
const rows = computed(() => {
  const p = packet.value
  const main = showFiveMinute.value ? p?.fiveMinuteBars ?? [] : p?.bars ?? []
  const symbol = visit.value?.contract
  const chosen = new Map(main.filter(b => b.symbol === symbol).map(b => [b.ts, b]))
  for (const b of p?.executionBars ?? []) if (b.symbol === symbol && !chosen.has(b.ts)) {
    chosen.set(b.ts, showFiveMinute.value ? { ...b, timeframe: '5m' } : b)
  }
  return [...chosen.values()].sort((a,b) => a.ts.localeCompare(b.ts))
})
const trades = computed(() => {
  const items = tradesByVisit.value.get(visit.value?.id ?? '') ?? []
  return lossesOnly.value ? items.filter(t => t.net < 0) : items
})
const trade = computed(() => trades.value[tradeIndex.value] ?? trades.value[0])
const status = computed(() => packet.value?.recent?.statuses.find(s => s.event_id === visit.value?.id && s.policy === arm.value))
const reason = (key: string) => reasons[key] ?? key
const chartOverlay = computed<ZoneReplayOverlay | null>(() => {
  const v = visit.value, z = zone.value
  if (!v || !z) return null
  const label = (t: string, opening = false) => replayBarLabel(rows.value, t, opening)
  const formed = label(z.formed_ns), confirmed = label(z.confirmed_ns), end = label(z.end_ns), touch = label(v.touch_ns)
  if (!formed || !confirmed || !end || !touch) return null
  const classification = v.classified_ns && label(v.classified_ns)
  const t = trade.value
  const entry = t && label(t.entry_ns, true)
  const exit = t && label(t.exit_ns, !closeReasons.includes(t.reason))
  const landmarks: { ts: string; label: string }[] = []
  const trailing: { ts: string; price: number }[] = []
  const signal = t?.signal_end && label(t.signal_end)
  if (signal && signal !== classification) landmarks.push({ ts: signal, label: '入场信号' })
  for (const m of t?.marks ?? []) {
    const ts = label(m.time_ns)
    if (!ts) continue
    if (m.kind === 'stop' && m.price !== undefined) {
      const effective = m.effective_ns ? label(m.effective_ns, true) : null
      if (!exit || (effective ?? ts) <= exit) trailing.push({ ts: effective ?? ts, price: m.price })
    } else {
      landmarks.push({ ts, label: m.kind === 'activate' ? '达到 2R，启动跟踪' : '无跟随判定' })
    }
  }
  return {
    zones: z.zones, formed, confirmed, end, visit: touch,
    reaction: classification ? { ts: classification, label: reactions[v.reaction] } : null,
    trade: t && entry && exit ? { entry, exit, entryPrice: t.entry, exitPrice: t.exit,
      stop: t.initial_stop ?? t.stop, side: t.side, exitLabel: reason(t.reason), landmarks, trailing,
      target: packet.value?.recent && t.side && t.distance ? t.entry + t.side * 2 * t.distance : undefined } : null,
  }
})

watch(available, list => {
  if (!list.some(p => id(p) === packageId.value)) packageId.value = list[0] ? id(list[0]) : ''
}, { flush: 'sync' })
watch(visits, list => {
  if (!list.some(v => v.id === visitId.value)) visitId.value = list[0]?.id ?? ''
}, { flush: 'sync' })
watch(trades, () => { tradeIndex.value = 0 }, { flush: 'sync' })

async function loadRecent() {
  if (loading.value) return
  loading.value = true
  errors.value = ''
  try {
    const loaded = await Promise.all(Object.keys(products).map(async p => {
      const response = await fetch(`${import.meta.env.BASE_URL}research/zone-recent/${p}.json`)
      if (!response.ok) throw new Error(`无法读取 ${products[p]} 的本地复盘包`)
      return parseZoneReplay(await response.text())
    }))
    const next = new Map(packages.value.map(p => [id(p), p]))
    for (const p of loaded) next.set(id(p), p)
    packages.value = [...next.values()]
    const first = loaded.find(p => p.product === route.query.product) ?? loaded[0]
    timeframe.value = first.timeframe
    packageId.value = id(first)
    structure.value = reaction.value = ''
    lossesOnly.value = false
    tradesOnly.value = true
  } catch (error) {
    errors.value = error instanceof Error ? error.message : '无法载入复盘'
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  if (route.query.exit === 'trail_on' || route.query.exit === 'trail_off') {
    arm.value = route.query.exit
    activatedOnly.value = true
  }
  if (typeof route.query.product === 'string' && products[route.query.product]) void loadRecent()
})

async function importFiles(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  input.value = ''
  if (!files.length) return
  loading.value = true
  errors.value = ''
  const next = new Map(packages.value.map(p => [id(p), p]))
  const failures: string[] = []
  let first: ZoneReplayPackage | undefined
  for (const f of files) {
    try {
      if (f.size > 25 * 1024 * 1024) throw new Error('文件超过25 MB')
      const p = parseZoneReplay(await f.text())
      next.set(id(p), p)
      first ??= p
    } catch (error) {
      failures.push(`${f.name} · ${error instanceof Error ? error.message : '无法读取'}`)
    }
  }
  packages.value = [...next.values()].sort((a, b) => a.fold-b.fold || a.product.localeCompare(b.product))
  if (first) {
    timeframe.value = first.timeframe
    packageId.value = id(first)
  }
  errors.value = failures.join('，')
  loading.value = false
}

function move(step: number) {
  const i = visits.value.findIndex(v => v.id === visit.value?.id)
  const next = visits.value[i + step]
  if (next) visitId.value = next.id
}
</script>

<template>
  <main class="zone-replay">
    <div class="replay-controls">
      <button type="button" @click="router.back()">返回</button>
      <strong>区域研究复盘</strong>
      <button type="button" :disabled="loading" @click="loadRecent">棉花 / 乙二醇近期回放</button>
      <label>导入复盘包 <input type="file" accept=".json,application/json" multiple :disabled="loading" @change="importFiles"></label>
      <label>周期 <select v-model="timeframe"><option>15m</option><option>30m</option></select></label>
      <label>样本 <select v-model="packageId"><option v-for="p in available" :key="id(p)" :value="id(p)">{{ p.recent ? `${products[p.product]}（${p.product}）· 近期` : `第${p.fold}轮 · ${p.product}` }}</option></select></label>
      <label v-if="packet?.recent">退出方案 <select v-model="arm"><option v-for="(name, key) in arms" :key="key" :value="key">{{ name }}</option></select></label>
      <label v-if="following"><input v-model="activatedOnly" type="checkbox"> 仅已启动跟踪</label>
      <label v-if="packet?.recent"><input v-model="tradesOnly" type="checkbox"> 仅已平仓</label>
      <button v-if="packet?.fiveMinuteBars?.length" type="button" @click="executionView = !executionView">
        {{ showFiveMinute ? '查看 30 分钟全流程' : '查看 5 分钟执行' }}
      </button>
      <label>结构 <select v-model="structure"><option value="">全部</option><option v-for="(name, key) in names" :key="key" :value="key">{{ name }}</option></select></label>
      <label>反应 <select v-model="reaction"><option value="">全部</option><option v-for="(name, key) in reactions" :key="key" :value="key">{{ name }}</option></select></label>
      <label><input v-model="lossesOnly" type="checkbox"> 仅亏损交易</label>
    </div>
    <div v-if="errors" class="replay-error" role="alert">{{ errors }}</div>
    <div v-if="packet?.recent" class="replay-detail">
      {{ packet.recent.first }} 至 {{ packet.recent.last }}（北京时间） · 30 分钟识别 / 5 分钟模拟撮合
      · {{ packet.product === 'CF' ? '棉花借用乙二醇封存参数演示' : '沿用乙二醇最后一轮封存参数' }}
      · 本方案已平仓 {{ packet.trades.filter(t => t.policy === arm).length }} 笔
    </div>
    <div v-if="visit && zone && packet" class="replay-controls">
      <button type="button" :disabled="visits[0]?.id === visit.id" @click="move(-1)">上一例</button>
      <label>到访 <select v-model="visitId"><option v-for="v in visits" :key="v.id" :value="v.id">{{ v.touch_ns }} · {{ reactions[v.reaction] }}</option></select></label>
      <button type="button" :disabled="visits[visits.length-1]?.id === visit.id" @click="move(1)">下一例</button>
      <span>{{ visit.contract }} · {{ names[zone.kind] }} · {{ visits.length }}例</span>
      <label v-if="trades.length">成交
        <select v-model.number="tradeIndex">
          <option v-for="(t, i) in trades" :key="i" :value="i">{{ packet.recent ? reason(t.reason) : `${t.policy}/${t.scenario} · ${t.variant_id}` }} · {{ t.net.toFixed(2) }}元</option>
        </select>
      </label>
      <span v-else>{{ status ? reason(status.reason) : '该到访没有共享账户成交' }}</span>
    </div>
    <div v-if="visit && zone" class="replay-detail">
      区域 {{ zone.zones.map(z => `${z.low}～${z.high}`).join(' / ') }} · 起始拐点 {{ zone.formed_ns }} · 确认 {{ zone.confirmed_ns }} · 到访 {{ visit.touch_ns }}
      · {{ reactions[visit.reaction] }} {{ visit.classified_ns ?? '—' }}
      <span v-if="chartOverlay && chartOverlay.confirmed !== zone.confirmed_ns"> · 确认K线标签 {{ chartOverlay.confirmed }}</span>
      <span v-if="trade"> · {{ trade.side === 1 ? '做多' : trade.side === -1 ? '做空' : '' }}入场 {{ trade.entry_ns }} @ {{ trade.entry }}
        · 初始止损 {{ trade.initial_stop ?? trade.stop }} · 退出 {{ trade.exit_ns }} @ {{ trade.exit }} · {{ reason(trade.reason) }} · 净额 {{ trade.net.toFixed(2) }} 元</span>
      <span v-if="trade?.reason === 'roll' && packet?.executionBars?.length"> · 换月退出标在原合约5分钟成交柱</span>
      <details v-if="trade?.marks?.length" class="replay-marks">
        <summary>查看 {{ trade.marks.length }} 条退出决策（5 分钟）</summary>
        <ol>
          <li v-for="mark in trade.marks" :key="`${mark.time_ns}:${mark.kind}`">
            {{ mark.time_ns }} · {{ mark.kind === 'activate' ? '达到 2R 穿透条件，启动跟踪' : mark.kind === 'no_follow'
              ? '无跟随判定，下一可成交开盘退出' : `跟踪止损调至 ${mark.price}（生效：${mark.effective_ns ?? '下一根'}）` }}
          </li>
        </ol>
      </details>
    </div>
    <KLineChart v-if="packet && visit && chartOverlay"
      :key="`${packageId}:${visit.contract}:${showFiveMinute}`" :symbol="visit.contract" :timeframe="showFiveMinute ? '5m' : packet.timeframe"
      :rows="rows" :signals="[]" :zone-replay="chartOverlay" :focus-ts="chartOverlay.visit" :focus-key="`${visit.id}:${arm}:${tradeIndex}`" />
    <p v-else class="replay-detail">{{ packages.length ? '当前筛选没有到访记录。可切换周期或放宽筛选。' : '从研究输出的 replay 目录导入JSON，可一次选中15m和30m复盘包。' }}</p>
  </main>
</template>

<style scoped>
.zone-replay { height: 100%; min-height: 0; display: flex; flex-direction: column; }
.replay-controls { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; padding: 8px 12px; border-bottom: 1px solid #cbd5e1; }
.replay-controls label { display: inline-flex; align-items: center; gap: 4px; }
.replay-controls select, .replay-controls button { color: #334155; background: #fff; border: 1px solid #94a3b8; border-radius: 4px; padding: 3px 6px; }
.replay-controls button:disabled { opacity: .5; }
.replay-detail, .replay-error { padding: 6px 12px; font-size: 12px; }
.replay-marks { margin-top: 4px; }
.replay-marks ol { max-height: 120px; overflow-y: auto; margin: 4px 0; }
.replay-error { color: #dc2626; }
</style>
