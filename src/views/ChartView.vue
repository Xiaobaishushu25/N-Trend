<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import draggable from 'vuedraggable'
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
import { api, onDataUpdated, onQuotesUpdated, onScanCompleted } from '../services/api'
import OverflowText from '../components/OverflowText.vue'
import { useGroupsStore } from '../stores/groups'
import { useSymbolsStore } from '../stores/symbols'
import { useKlinesStore } from '../stores/klines'
import { useScansStore } from '../stores/scans'
import { confirmAction } from '../utils/confirm'
import { notify } from '../utils/notify'
import { openSymbolContextMenu } from '../utils/symbolMenu'
import type {
  GroupRow,
  KlineRow,
  MarketSnapshot,
  PatternDto,
  SignalOutcome,
  SymbolRow,
  Timeframe,
} from '../types'

const route = useRoute()
const router = useRouter()
const symbolsStore = useSymbolsStore()
const klinesStore = useKlinesStore()
const scansStore = useScansStore()
const groupsStore = useGroupsStore()

const VueDraggable = draggable

const symbol = computed(() => String(route.params.symbol || ''))
const timeframe = ref<Timeframe>('15m')
const timeframes: Timeframe[] = ['5m', '15m', '30m', '60m', '120m', '240m', '1d']

const currentSymbol = computed(() => symbolsStore.symbols.find((s) => s.code === symbol.value))

/** 最近一次扫描识别出的该品种全部N形态（策略基于 15m/60m，与图表显示级别无关） */
const signals = computed<PatternDto[]>(() => {
  return scansStore.latestSignals
    .filter((s) => s.symbol === symbol.value)
    .map((s) => s as unknown as PatternDto)
})

/**
 * 形态身份键：方向+级别+s1/s2 索引，用于把「最近活跃信号」历史挂到对应的形态卡片上
 */
function historyKey(s: PatternDto) {
  return `${s.direction}|${s.level}|${s.s1.index}|${s.s2.index}`
}

/**
 * 每个形态的状态演变历史：来自最近若干次扫描的实时记录（不是事后回算），
 * 同一形态连续几轮状态不变时只保留一次。挂在对应形态卡片下展示。
 */
const patternHistory = computed(() => {
  const map = new Map<string, { time: string; state: string }[]>()
  const rows = [...scansStore.recentSignals].sort((a, b) =>
    a.created_at.localeCompare(b.created_at),
  )
  for (const r of rows) {
    let d: PatternDto | null = null
    try {
      d = JSON.parse(r.detail) as PatternDto
    } catch {
      d = null
    }
    if (!d) continue
    const key = `${r.direction}|${r.level}|${d.s1.index}|${d.s2.index}`
    const arr = map.get(key) ?? []
    const last = arr[arr.length - 1]
    if (!last || last.state !== r.state) {
      arr.push({ time: r.created_at, state: r.state })
    }
    map.set(key, arr)
  }
  return map
})

/** 被点击隐藏的形态编号（用于控制K线图上的展示） */
const hiddenNumbers = ref<Set<number>>(new Set())

/** 记录“仅显示首个形态”的默认隐藏是否已应用到当前 品种+周期 */
const hiddenApplied = ref('')

/**
 * 默认只显示第一个形态：其余全部在K线图上隐藏，避免画线/标点堆叠看不清。
 * 用户手动点开/隐藏后不再自动重置；切换品种或周期时重新按默认应用。
 */
function applyDefaultHidden() {
  const key = `${symbol.value}|${timeframe.value}`
  if (hiddenApplied.value === key) return
  const nums = signals.value.map((s) => s.number)
  if (!nums.length) return
  hiddenApplied.value = key
  hiddenNumbers.value = new Set(nums.slice(1))
}

/** 左侧品种列表开关与行情快照 */
const showList = ref(true)
const snapshots = ref<Record<string, MarketSnapshot>>({})
/** 品种行闪烁方向：up=上涨(红) / down=下跌(绿)，由实时行情跳动驱动 */
const rowFlash = ref<Record<string, 'up' | 'down'>>({})
const flashTimers = new Map<string, ReturnType<typeof setTimeout>>()
const FLASH_MS = 1000
/** 分钟级周期的长度（分钟），用于把实时报价对齐到当前正在形成的K线桶 */
const TIMEFRAME_MINUTES: Record<string, number> = {
  '5m': 5,
  '15m': 15,
  '30m': 30,
  '60m': 60,
  '120m': 120,
  '240m': 240,
}
/**
 * 实时拼出的K线：最后一个元素是「当前正在形成」的一根，
 * 之前的元素是已收盘但库内还没落地的闭环；与后端分桶规则保持一致
 * （按自然日分钟网格取整、桶末时间戳），切换品种/周期时清空。
 */
const liveBars = ref<KlineRow[]>([])

/** 按后端相同规则计算当前正在形成的桶的结束时间（桶末时间戳） */
function liveBucketLabel(date: Date, timeframe: string): string | null {
  const minutes = TIMEFRAME_MINUTES[timeframe]
  if (!minutes) return null
  const elapsed = date.getHours() * 60 + date.getMinutes() + date.getSeconds() / 60
  const endMin = Math.max(1, Math.ceil(elapsed / minutes)) * minutes
  const d = new Date(date.getFullYear(), date.getMonth(), date.getDate(), 0, endMin, 0, 0)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:00`
}

/** 用实时报价刷新当前正在形成的K线；进入新桶时把前一根自动转为“已收未落地” */
function updateLiveBar(latest: number) {
  if (!symbol.value) return
  const label = liveBucketLabel(new Date(), timeframe.value)
  if (!label) return
  const arr = [...liveBars.value]
  const last = arr[arr.length - 1]
  if (last && last.ts === label) {
    if (last.close === latest) return
    arr[arr.length - 1] = {
      ...last,
      high: Math.max(last.high, latest),
      low: Math.min(last.low, latest),
      close: latest,
    }
  } else {
    arr.push({
      symbol: symbol.value,
      timeframe: timeframe.value,
      ts: label,
      open: latest,
      high: latest,
      low: latest,
      close: latest,
      volume: 0,
      hold: 0,
      source: 'live',
    })
    // 只保留最近一小段，等库内每 5 分钟刷新后自然由历史序列接管
    if (arr.length > 12) arr.splice(0, arr.length - 12)
  }
  liveBars.value = arr
}

/** 展示序列：历史完整K线 + 实时拼出的K线（时间戳重叠时以后者更新、保留库内成交量） */
const displayRows = computed<KlineRow[]>(() => {
  const rows = klinesStore.rows
  const live = liveBars.value
  if (!live.length) return rows
  const liveByTs = new Map(live.map((b) => [b.ts, b]))
  const rowByTs = new Map(rows.map((r) => [r.ts, r]))
  const out: KlineRow[] = []
  for (const r of rows) {
    const bar = liveByTs.get(r.ts)
    out.push(bar ? { ...bar, volume: r.volume, hold: r.hold } : r)
  }
  for (const bar of live) {
    if (!rowByTs.has(bar.ts) && (!out.length || bar.ts > out[out.length - 1].ts)) {
      out.push(bar)
    }
  }
  return out
})

/** 顶栏/信息卡回退用的最新收盘价，优先取实时拼出的正在形成的K线 */
const latestClose = computed(() =>
  displayRows.value.length ? displayRows.value[displayRows.value.length - 1].close : null,
)

/** 当前分组的成员（按组内 sort_index 顺序）；全部视图下为全部品种 */
const groupSymbols = ref<SymbolRow[]>([])
/** 拖拽结束后浏览器可能补发一次 click，用它临时抑制行点击跳转 */
let symbolSuppressClick = false

/** 拉取当前分组的成员（按组内 sort_index 顺序） */
async function loadGroupSymbols() {
  groupSymbols.value =
    groupsStore.selectedId == null
      ? [...symbolsStore.symbols]
      : await api.getGroupSymbols(groupsStore.selectedId)
}

/** 左侧列表：分组视图按组内顺序，全部视图按代码序 */
const visibleSymbols = computed(() => groupSymbols.value)

/**
 * 拖拽结束落库：vuedraggable 已把 groupSymbols 调整为新顺序，
 * 这里把顺序持久化并广播，让列表页表格同步重拉。
 */
async function persistListOrder() {
  // 拖拽结束后浏览器可能补发一次 click，这里临时抑制行点击跳转
  symbolSuppressClick = true
  setTimeout(() => {
    symbolSuppressClick = false
  }, 0)
  const groupId = groupsStore.selectedId
  if (groupId == null) return
  try {
    await api.reorderGroupSymbols(
      groupId,
      groupSymbols.value.map((s) => s.code),
    )
    groupsStore.bumpRevision()
  } catch (err) {
    notify.error(String(err))
    await loadGroupSymbols() // 落库失败则回滚为服务端顺序
  }
}

/** 行点击进入K线图；拖拽结束后紧接着的 click 不触发跳转 */
function onSymbolRowClick(code: string) {
  if (symbolSuppressClick) return
  router.push({ name: 'chart', params: { symbol: code } })
}

/** 左侧品种行右键菜单：与表格行一致（分组操作 + 彻底删除） */
async function onSymbolContextMenu(row: { code: string }, e: MouseEvent) {
  // 先同步阻止浏览器默认菜单，再异步查分组归属，避免原生菜单闪现
  e.preventDefault()
  let memberGroups: GroupRow[] = []
  try {
    memberGroups = await api.listSymbolGroups(row.code)
  } catch {
    // 查询失败不影响菜单弹出，仅少了对勾标识
  }
  openSymbolContextMenu(e, {
    groups: groupsStore.groups,
    selectedGroupId: groupsStore.selectedId,
    symbol: row.code,
    memberGroupIds: new Set(memberGroups.map((g) => g.id)),
    onRemoveFromGroup: () => handleRemoveFromGroup(row.code),
    onCopyToGroup: (g) => handleCopyToGroup(row.code, g),
    onMoveToGroup: (g) => handleMoveToGroup(row.code, g),
    onDeleteSymbol: () => handleDeleteSymbol(row.code),
  })
}

async function reloadSymbolList() {
  await symbolsStore.load()
  await loadGroupSymbols()
}

async function handleRemoveFromGroup(code: string) {
  const groupId = groupsStore.selectedId
  if (groupId == null) return
  try {
    await api.removeSymbolFromGroup(code, groupId)
    notify.success(`${code} 已从该组删除`)
    await reloadSymbolList()
  } catch (err) {
    notify.error(String(err))
  }
}

async function handleCopyToGroup(code: string, group: GroupRow) {
  try {
    await api.addSymbolToGroup(code, group.id)
    notify.success(`${code} 已复制到「${group.name}」`)
  } catch (err) {
    notify.error(String(err))
  }
}

async function handleMoveToGroup(code: string, group: GroupRow) {
  const fromId = groupsStore.selectedId
  if (fromId == null) return
  try {
    // 先加入目标组，再从原组移除：即使第二步失败，品种也不会丢失
    await api.addSymbolToGroup(code, group.id)
    await api.removeSymbolFromGroup(code, fromId)
    notify.success(`${code} 已移动到「${group.name}」`)
    await reloadSymbolList()
  } catch (err) {
    notify.error(String(err))
  }
}

async function handleDeleteSymbol(code: string) {
  const ok = await confirmAction({
    title: '删除品种',
    content: `确定删除 ${code} 吗？将同时删除其K线数据。`,
    positiveText: '删除',
    negativeText: '取消',
    type: 'warning',
  })
  if (!ok) return
  try {
    await symbolsStore.remove(code)
    notify.success(`${code} 已删除`)
    await reloadSymbolList()
    // 删除的正是当前查看的品种时，跳到组内第一个品种；组空了回列表页
    if (symbol.value === code) {
      const first = visibleSymbols.value[0]
      if (first) router.push({ name: 'chart', params: { symbol: first.code } })
      else router.push({ name: 'dashboard' })
    }
  } catch (err) {
    notify.error(String(err))
  }
}

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

/** 让某个品种行闪烁一次，动画结束后自动熄灭，保证下一次跳动可重新触发 */
function setRowFlash(code: string, dir: 'up' | 'down') {
  rowFlash.value = { ...rowFlash.value, [code]: dir }
  const prev = flashTimers.get(code)
  if (prev) clearTimeout(prev)
  flashTimers.set(
    code,
    setTimeout(() => {
      const next = { ...rowFlash.value }
      delete next[code]
      rowFlash.value = next
      flashTimers.delete(code)
    }, FLASH_MS),
  )
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
  const list = visibleSymbols.value
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

/** 最近活跃信号时间显示：MM-DD HH:mm */
function fmtRecentTime(t: string) {
  return t.length >= 16 ? t.slice(5, 16) : t
}

let unlisteners: (() => void)[] = []

// 形态列表就绪后（含进入页面、扫描完成刷新）按默认规则隐藏非首个形态
watch(signals, applyDefaultHidden, { immediate: true })

watch([symbol, timeframe], async () => {
  hiddenApplied.value = ''
  applyDefaultHidden()
  liveBars.value = []
  if (symbol.value) {
    await klinesStore.load(symbol.value, timeframe.value, 1200)
    scansStore.loadRecentSignals(symbol.value)
    scansStore.refreshLatestSignals()
  }
})

// 分组/组内顺序在别处被改动（如列表页表格拖拽）时，重拉本页列表
watch(() => groupsStore.revision, () => loadGroupSymbols())

onMounted(async () => {
  unlisteners.push(
    await onScanCompleted((result) => {
      scansStore.ingest(result)
      loadSnapshots()
      scansStore.loadRecentSignals(symbol.value)
      scansStore.refreshLatestSignals()
    }),
  )
  unlisteners.push(
    await onDataUpdated(() => {
      loadSnapshots()
      scansStore.refreshLatestSignals()
      // 定时入库后静默重载完整K线，让刚收盘的实时桶转正为历史K线
      if (symbol.value) klinesStore.load(symbol.value, timeframe.value, 1200, true)
    }),
  )
  // 实时现价：合并进快照表，缺失品种保留旧值，避免盘口跳动时整表闪空
  unlisteners.push(
    await onQuotesUpdated((list) => {
      if (!list.length) return
      const next: Record<string, MarketSnapshot> = { ...snapshots.value }
      for (const s of list) {
        const prev = snapshots.value[s.code]
        // 价格实际跳动才闪烁：上涨红、下跌绿；首笔报价不闪
        if (s.latest != null && prev?.latest != null && s.latest !== prev.latest) {
          setRowFlash(s.code, s.latest > prev.latest ? 'up' : 'down')
        }
        // 用当前品种的实时报价拼出正在形成的K线
        if (s.latest != null && s.code === symbol.value) updateLiveBar(s.latest)
        next[s.code] = s
      }
      snapshots.value = next
    }),
  )
  await symbolsStore.load()
  await groupsStore.load()
  await loadGroupSymbols()
  // 进入页面立即拉一次行情快照，避免左侧价格/涨幅要等下一次刷新或扫描事件才显示
  loadSnapshots()
  if (!scansStore.latest) {
    try {
      await scansStore.runScan()
      scansStore.loadRecentSignals(symbol.value)
      scansStore.refreshLatestSignals()
    } catch {
      // 无数据时扫描失败不影响看图
    }
  }
  if (symbol.value) {
    await klinesStore.load(symbol.value, timeframe.value, 1200)
    scansStore.loadRecentSignals(symbol.value)
    scansStore.refreshLatestSignals()
  }
})

onBeforeUnmount(() => {
  for (const timer of flashTimers.values()) clearTimeout(timer)
  flashTimers.clear()
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
      <div
        v-if="showList"
        class="symbol-list"
        :class="{ 'can-reorder': groupsStore.selectedId != null }"
      >
        <div class="sl-title">品种</div>
        <n-scrollbar style="flex: 1">
          <VueDraggable
            v-model="groupSymbols"
            item-key="code"
            class="sl-list"
            :animation="150"
            :disabled="groupsStore.selectedId == null"
            ghost-class="sl-row-ghost"
            chosen-class="sl-row-chosen"
            @end="persistListOrder"
          >
            <template #item="{ element }">
              <div
                class="sl-row"
                :class="[
                  { active: element.code === symbol },
                  { 'is-flash-up': rowFlash[element.code] === 'up' },
                  { 'is-flash-down': rowFlash[element.code] === 'down' },
                  signalBySymbol[element.code]?.state === '即将触发' ? 'has-pending' : '',
                  signalBySymbol[element.code]?.state === '当前已触发' ? 'has-triggered' : '',
                  signalBySymbol[element.code]?.state === '已触发，接近时效边界' ? 'has-stale' : '',
                ]"
                @click="onSymbolRowClick(element.code)"
                @contextmenu="onSymbolContextMenu(element, $event)"
              >
                <div class="sl-main">
                  <OverflowText
                    class="sl-name"
                    :text="element.name !== element.code ? element.name : element.code"
                  />
                  <span class="sl-code">{{ element.code }}</span>
                </div>
                <span
                  v-if="signalBySymbol[element.code]"
                  class="sl-sig"
                  :class="'is-' + sigType(signalBySymbol[element.code]?.state ?? '')"
                  :title="sigTitle(signalBySymbol[element.code]!)"
                >
                  {{ sigLabel(signalBySymbol[element.code]?.state ?? '') }}
                </span>
                <div class="sl-quote">
                  <span
                    class="sl-price"
                    :style="{ color: trendColor(snapshots[element.code]?.change_pct ?? null) }"
                  >
                    {{ fmtPrice(snapshots[element.code]?.latest ?? null) }}
                  </span>
                  <span
                    class="sl-change"
                    :style="{ color: trendColor(snapshots[element.code]?.change_pct ?? null) }"
                  >
                    {{ fmtChange(snapshots[element.code]?.change_pct ?? null) }}
                  </span>
                </div>
              </div>
            </template>
          </VueDraggable>
        </n-scrollbar>
      </div>

      <div class="chart-col" @wheel.prevent="handleChartWheel">
        <KLineChart
          v-if="symbol && klinesStore.rows.length"
          :symbol="symbol"
          :timeframe="timeframe"
          :rows="displayRows"
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

                <div v-if="patternHistory.get(historyKey(s))?.length" class="pc-history">
                  <span class="pc-history-label">状态演变</span>
                  <span v-for="(h, i) in patternHistory.get(historyKey(s))" :key="i">
                    {{ i > 0 ? ' → ' : '' }}{{ fmtRecentTime(h.time) }} {{ h.state }}
                  </span>
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
  /* 字号整体放大一档后，容器同步加宽，避免文字挤在一起 */
  flex: 0 0 230px;
  width: 230px;
  min-width: 0;
  background: #fff;
  border-radius: 10px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.06);
}
.sl-title {
  padding: 12px 14px 8px;
  font-size: 14px;
  font-weight: 600;
  color: #334155;
}
.sl-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 8px 14px;
  cursor: pointer;
  user-select: none;
  border-left: 3px solid transparent;
  transition: background 0.15s;
}
.sl-list {
  min-height: 100%;
}
.symbol-list.can-reorder .sl-row {
  cursor: grab;
}
.sl-row-ghost {
  opacity: 0.45;
  background: #eaf2ff;
}
.sl-row-chosen {
  background: #dbe9ff;
}
.sl-row:hover {
  background: #f6f8fa;
}
.sl-row.is-flash-up {
  animation: sl-row-flash-up 0.9s ease-out;
}
.sl-row.is-flash-down {
  animation: sl-row-flash-down 0.9s ease-out;
}
@keyframes sl-row-flash-up {
  0% {
    background-color: rgba(224, 49, 49, 0.14);
  }
  100% {
    background-color: #fff;
  }
}
@keyframes sl-row-flash-down {
  0% {
    background-color: rgba(15, 157, 88, 0.14);
  }
  100% {
    background-color: #fff;
  }
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
  font-size: 14px;
  color: #1f2329;
}
.sl-code {
  font-size: 12px;
  color: #94a3b8;
}
.sl-quote {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  font-variant-numeric: tabular-nums;
}
.sl-price {
  font-size: 14px;
  font-weight: 600;
}
.sl-change {
  font-size: 12px;
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
  gap: 4px;
  font-size: 12px;
  font-weight: 600;
  line-height: 1;
  padding: 3px 8px;
  border-radius: 999px;
  white-space: nowrap;
}
.sl-sig::before {
  content: '';
  width: 6px;
  height: 6px;
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
.pc-history {
  margin-top: 8px;
  font-size: 11px;
  line-height: 1.6;
  color: #94a3b8;
}
.pc-history-label {
  margin-right: 6px;
  font-weight: 600;
  color: #64748b;
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
