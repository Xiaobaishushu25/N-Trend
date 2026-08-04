<script setup lang="ts">
import { h, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  NButton,
  NDataTable,
  NInput,
  NPopconfirm,
  NSpace,
  NSwitch,
  NTag,
  NText,
  useMessage,
  type DataTableColumns,
} from 'naive-ui'
import dayjs from 'dayjs'
import { api, onDataUpdated, onScanCompleted } from '../services/api'
import { useSymbolsStore } from '../stores/symbols'
import { useScansStore } from '../stores/scans'
import type { KlineRow, SignalRow, SymbolRow } from '../types'

const router = useRouter()
const message = useMessage()
const symbolsStore = useSymbolsStore()
const scansStore = useScansStore()

interface WatchRow {
  symbol: SymbolRow
  latest: number | null
  changePct: number | null
  signal: SignalRow | null
}

const rows = ref<WatchRow[]>([])
const loading = ref(false)
const refreshing = ref(false)
const scanning = ref(false)
const enriching = ref(false)
const newCode = ref('')
let unlisteners: (() => void)[] = []

const ACTIVE_STATES = new Set(['即将触发', '当前已触发', '已触发，接近时效边界'])

function isActive(s: SignalRow) {
  return ACTIVE_STATES.has(s.state)
}

function bestSignal(signals: SignalRow[]): SignalRow | null {
  if (!signals.length) return null
  return [...signals].sort((a, b) => {
    const aa = isActive(a) ? 1 : 0
    const ba = isActive(b) ? 1 : 0
    if (aa !== ba) return ba - aa
    return b.score - a.score
  })[0]
}

function pad(n: number) {
  return n < 10 ? `0${n}` : String(n)
}

/** 交易日：夜盘（20:00 后）计入次日 */
function tradingDayKey(ts: string): string {
  const d = new Date(ts.replace(' ', 'T'))
  const date = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
  if (d.getHours() >= 20) return dayjs(date).add(1, 'day').format('YYYY-MM-DD')
  return date
}

function latestStats(klines: KlineRow[]): { latest: number | null; changePct: number | null } {
  if (!klines.length) return { latest: null, changePct: null }
  const latest = klines[klines.length - 1].close
  const byDay = new Map<string, number>()
  for (const k of klines) byDay.set(tradingDayKey(k.ts), k.close)
  const days = [...byDay.keys()].sort()
  if (days.length < 2) return { latest, changePct: null }
  const prev = byDay.get(days[days.length - 2])!
  return { latest, changePct: ((latest - prev) / prev) * 100 }
}

async function loadAll() {
  loading.value = true
  try {
    await symbolsStore.load()
    const signals = await api.getLatestSignals(500)
    const bySymbol = new Map<string, SignalRow[]>()
    for (const s of signals) {
      const arr = bySymbol.get(s.symbol) || []
      arr.push(s)
      bySymbol.set(s.symbol, arr)
    }
    const list = await Promise.all(
      symbolsStore.symbols.map(async (sym) => {
        let latest: number | null = null
        let changePct: number | null = null
        try {
          const ks = await api.getKlines(sym.code, '5m', 150)
          const stats = latestStats(ks)
          latest = stats.latest
          changePct = stats.changePct
        } catch {
          // 单品种数据异常不影响列表
        }
        return {
          symbol: sym,
          latest,
          changePct,
          signal: bestSignal(bySymbol.get(sym.code) || []),
        }
      }),
    )
    rows.value = list
  } finally {
    loading.value = false
  }
}

function openChart(row: WatchRow) {
  router.push({ name: 'chart', params: { symbol: row.symbol.code } })
}

function rowProps(row: WatchRow): Record<string, unknown> {
  return {
    style: 'cursor: pointer',
    onDblclick: () => openChart(row),
  }
}

const fmt = (v: number | null | undefined, digits = 1) =>
  v == null ? '—' : v.toFixed(digits)

const trendColor = (v: number | null) =>
  v == null ? '#8892a6' : v > 0 ? '#e03131' : v < 0 ? '#0f9d58' : '#8892a6'

function dirLabel(s: SignalRow) {
  return s.direction === 'up' ? '做多' : s.direction === 'down' ? '做空' : s.direction
}

function levelLabel(s: SignalRow) {
  return s.level === 'fine' ? '精细' : s.level === 'large' ? '较大' : s.level
}

function stateType(state: string): 'info' | 'success' | 'warning' | 'default' | 'error' {
  if (state === '即将触发') return 'info'
  if (state === '当前已触发') return 'success'
  if (state === '已触发，接近时效边界') return 'warning'
  return 'default'
}

const columns: DataTableColumns<WatchRow> = [
  {
    title: '代码',
    key: 'code',
    width: 90,
    render: (r) => h('span', { style: 'font-weight: 600' }, r.symbol.code),
  },
  {
    title: '名称',
    key: 'name',
    width: 110,
    render: (r) => (r.symbol.name && r.symbol.name !== r.symbol.code ? r.symbol.name : '—'),
  },
  {
    title: '最新价',
    key: 'latest',
    width: 100,
    align: 'right',
    render: (r) =>
      h(
        'span',
        { style: `color: ${trendColor(r.changePct)}; font-weight: 600` },
        fmt(r.latest, 1),
      ),
  },
  {
    title: '涨跌幅',
    key: 'change',
    width: 90,
    align: 'right',
    render: (r) =>
      h(
        'span',
        { style: `color: ${trendColor(r.changePct)}` },
        r.changePct == null ? '—' : `${r.changePct >= 0 ? '+' : ''}${r.changePct.toFixed(2)}%`,
      ),
  },
  {
    title: '最值得关注的形态',
    key: 'pattern',
    minWidth: 170,
    render: (r) =>
      r.signal ? `${dirLabel(r.signal)} ${levelLabel(r.signal)}N · ${r.signal.grade}` : '—',
  },
  {
    title: '状态',
    key: 'state',
    width: 150,
    render: (r) => {
      const sig = r.signal
      return sig
        ? h(NTag, { type: stateType(sig.state), size: 'small' }, { default: () => sig.state })
        : h(NText, { depth: 3 }, { default: () => '—' })
    },
  },
  {
    title: '入场价',
    key: 'entry',
    width: 90,
    align: 'right',
    render: (r) => fmt(r.signal?.entry, 1),
  },
  {
    title: '止损价',
    key: 'stop',
    width: 90,
    align: 'right',
    render: (r) => fmt(r.signal?.stop, 1),
  },
  {
    title: '目标价',
    key: 'target',
    width: 90,
    align: 'right',
    render: (r) => fmt(r.signal?.target, 1),
  },
  {
    title: '评分',
    key: 'score',
    width: 80,
    align: 'right',
    render: (r) => (r.signal ? r.signal.score.toFixed(2) : '—'),
  },
  {
    title: '启用',
    key: 'enabled',
    width: 70,
    render: (r) =>
      h(NSwitch, {
        size: 'small',
        value: r.symbol.enabled,
        onUpdateValue: (v: boolean) => symbolsStore.setFlags(r.symbol.code, r.symbol.watchlist, v),
      }),
  },
  {
    title: '操作',
    key: 'actions',
    width: 90,
    render: (r) =>
      h(
        NPopconfirm,
        { onPositiveClick: () => symbolsStore.remove(r.symbol.code) },
        {
          trigger: () =>
            h(NButton, { size: 'small', type: 'error', quaternary: true }, { default: () => '删除' }),
          default: () => `删除 ${r.symbol.code}？将同时删除其K线数据。`,
        },
      ),
  },
]

async function doRefresh() {
  refreshing.value = true
  try {
    const stats = await api.refreshDataNow()
    message.success(`数据刷新完成：成功 ${stats.succeeded}，失败 ${stats.failures}`)
    await loadAll()
  } catch (e) {
    message.error(String(e))
  } finally {
    refreshing.value = false
  }
}

async function doEnrich() {
  enriching.value = true
  try {
    const n = await symbolsStore.enrichNames()
    message.success(`已补齐 ${n} 个品种名称`)
    await loadAll()
  } catch (e) {
    message.error(String(e))
  } finally {
    enriching.value = false
  }
}

async function doScan() {
  scanning.value = true
  try {
    await scansStore.runScan()
    const result = scansStore.latest
    message.success(`扫描完成：${result?.scanned ?? 0} 个品种，${result?.active_count ?? 0} 个信号`)
    await loadAll()
  } catch (e) {
    message.error(String(e))
  } finally {
    scanning.value = false
  }
}

async function doAddSymbol() {
  const code = newCode.value.trim().toUpperCase()
  if (!code) return
  try {
    const count = await symbolsStore.add(code)
    message.success(`${code} 已添加，回填 ${count} 根K线`)
    newCode.value = ''
    await loadAll()
  } catch (e) {
    message.error(String(e))
  }
}

onMounted(async () => {
  unlisteners.push(await onDataUpdated(() => loadAll()))
  unlisteners.push(await onScanCompleted(() => loadAll()))
  await loadAll()
})

onBeforeUnmount(() => {
  for (const fn of unlisteners) fn()
})
</script>

<template>
  <div class="page">
    <div class="toolbar">
      <n-space align="center">
        <n-button type="primary" :loading="refreshing" @click="doRefresh">刷新数据</n-button>
        <n-button type="success" :loading="scanning" @click="doScan">立即扫描</n-button>
        <n-button :loading="enriching" @click="doEnrich">刷新名称</n-button>
        <n-input
          v-model:value="newCode"
          placeholder="输入品种代码，如 RB0"
          style="width: 180px"
          @keyup.enter="doAddSymbol"
        />
        <n-button type="primary" ghost @click="doAddSymbol">添加品种</n-button>
        <n-text depth="3" style="font-size: 12px">双击行打开K线图</n-text>
      </n-space>
    </div>
    <n-data-table
      :columns="columns"
      :data="rows"
      :loading="loading"
      :row-props="rowProps"
      size="small"
      :bordered="true"
      :single-line="false"
    />
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
</style>




