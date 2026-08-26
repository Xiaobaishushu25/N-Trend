<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import draggable from 'vuedraggable'
import {
  NButton,
  NCheckbox,
  NSwitch,
  NEmpty,
  NIcon,
  NPopover,
  NScrollbar,
} from 'naive-ui'
import { Adjustments, ArrowLeft, Eye, EyeOff, GripVertical, List, X } from '@vicons/tabler'
import KLineChart from '../components/KLineChart.vue'
import { api, onDataUpdated, onQuotesUpdated, onScanCompleted } from '../services/api'
import OverflowText from '../components/OverflowText.vue'
import SignalNotes from '../components/SignalNotes.vue'
import { useGroupsStore } from '../stores/groups'
import { useSymbolsStore } from '../stores/symbols'
import { useKlinesStore } from '../stores/klines'
import { useScansStore } from '../stores/scans'
import { singleBarBadgeStyle, singleBarTitle } from '../utils/singleBar'
import { useSettingsStore } from '../stores/settings'
import { useAppStore } from '../stores/app'
import { fmtR } from '../stores/review'
import { confirmAction } from '../utils/confirm'
import { notify } from '../utils/notify'
import { openSymbolContextMenu } from '../utils/symbolMenu'
import type {
  GroupRow,
  KlineRow,
  MarketSnapshot,
  OutcomeDetail,
  PatternEvent,
  PatternDto,
  ReviewExitOverlay,
  ReviewSignalDetail,
  SymbolRow,
  Timeframe,
  TrendPointDto,
} from '../types'

const route = useRoute()
const router = useRouter()
const appStore = useAppStore()
const symbolsStore = useSymbolsStore()
const klinesStore = useKlinesStore()
const scansStore = useScansStore()
const groupsStore = useGroupsStore()

const VueDraggable = draggable

const symbol = computed(() => String(route.params.symbol || ''))
const getSingleBar = (code: string) => scansStore.singleBars.get(code) ?? null
const chartSingleBars = computed(() => { const sb = scansStore.singleBars.get(symbol.value); return sb ? [sb] : [] })
const timeframe = ref<Timeframe>('15m')
const allTimeframes: Timeframe[] = ['5m', '15m', '30m', '60m', '120m', '240m', '1d']
const settingsStore = useSettingsStore()
/** 图表加载的历史K线根数：至少保留现有 1200 根窗口，展示根数调大时同步扩容 */
const chartLoadLimit = computed(() => Math.max(1200, settingsStore.settings.ui.chart_display_bars))
/** 按配置勾选过滤后显示的周期；全部未勾选时回退为全部 */
const visibleTimeframes = computed<Timeframe[]>(() => {
  const enabled = settingsStore.settings.ui.timeframes
  const list = allTimeframes.filter((t) => enabled.includes(t))
  return list.length ? list : allTimeframes
})

/** 弹层勾选周期：立即生效并落盘，至少保留一个 */
function toggleTimeframe(t: Timeframe, checked: boolean) {
  const cur = settingsStore.settings.ui.timeframes
  const next = checked
    ? [...new Set([...cur, t])]
    : cur.filter((x) => x !== t)
  if (!next.length) return
  settingsStore.settings.ui.timeframes = next
  api.setTimeframes(next).catch(() => {})
}

const currentSymbol = computed(() => symbolsStore.symbols.find((s) => s.code === symbol.value))

/** 复盘跳转模式：从 /chart/:symbol?review=<eventId> 进入时重绘该事件形态与进出场点位 */
const reviewOverlay = ref<ReviewSignalDetail | null>(null)
/** 复盘模式下被点击隐藏绘制的信号ID */
const reviewHidden = ref<Set<number>>(new Set())
const reviewSignalId = computed(() => {
  const q = route.query.review
  return q ? Number(q) : null
})
const reviewMode = computed(() => reviewSignalId.value != null)
const reviewRows = ref<OutcomeDetail[]>([])
const reviewLoading = ref(false)
const reviewListKey = ref('')
const reviewIndex = computed(() =>
  reviewSignalId.value == null
    ? -1
    : reviewRows.value.findIndex((r) => r.event_id === reviewSignalId.value),
)
const reviewExit = computed<ReviewExitOverlay | null>(() => {
  const o = reviewOverlay.value?.outcome
  if (!o || reviewHidden.value.has(reviewSignalId.value ?? -1)) return null
  return {
    price: o.exit_price,
    ts: o.exit_ts,
    outcome: o.outcome,
    r: o.r_multiple,
  }
})
const activeReviewRow = computed(() =>
  reviewRows.value.find((r) => r.event_id === reviewSignalId.value) ?? null,
)
/** 复盘模式自动定位点：优先触发K线，未触发时用预警K线 */
const reviewFocusTs = computed<string | null>(() => {
  const row = activeReviewRow.value
  if (row?.trigger_ts || row?.warning_ts) return row.trigger_ts || row.warning_ts
  const ev = reviewOverlay.value?.event
  return ev?.trigger_ts || ev?.warning_ts || null
})
const reviewFocusKey = computed<number | null>(() => reviewSignalId.value)

let reviewOverlaySeq = 0
async function loadReviewOverlay() {
  const id = reviewSignalId.value
  const seq = ++reviewOverlaySeq
  if (!id || !symbol.value) {
    reviewOverlay.value = null
    return
  }
  try {
    const detail = await api.getReviewSignal(id)
    if (seq !== reviewOverlaySeq) return
    reviewOverlay.value = detail ?? null
    // 形态基于 15m，复盘视图固定到 15m
    if (detail) timeframe.value = '15m'
  } catch {
    if (seq === reviewOverlaySeq) reviewOverlay.value = null
  }
}

let reviewListSeq = 0
/** 进入复盘模式后按复盘窗口的筛选条件拉取一次明细列表，切换信号时复用该列表 */
async function loadReviewList(force = false) {
  const id = reviewSignalId.value
  if (id == null) return
  const filters = appStore.reviewJumpFilters
  const key = filters ? JSON.stringify(filters) : 'default'
  if (!force && reviewListKey.value === key) return
  const seq = ++reviewListSeq
  reviewLoading.value = true
  try {
    let rows = await api.getRecentOutcomes(2000, filters ?? undefined)
    // 目标信号不在筛选结果里时退化为全量明细，保证滚轮列表能覆盖当前信号
    if (!rows.some((r) => r.event_id === id)) {
      rows = await api.getRecentOutcomes(2000)
    }
    if (seq !== reviewListSeq || reviewSignalId.value == null) return
    reviewRows.value = rows
    reviewListKey.value = key
  } catch {
    if (seq === reviewListSeq) {
      reviewRows.value = []
      reviewListKey.value = ''
    }
  } finally {
    if (seq === reviewListSeq) reviewLoading.value = false
  }
}

function selectReviewSignal(row: OutcomeDetail) {
  if (!row) return
  reviewHidden.value.delete(row.event_id)
  if (row.event_id === reviewSignalId.value && row.symbol === symbol.value) return
  void router.replace({
    name: 'chart',
    params: { symbol: row.symbol },
    query: { review: String(row.event_id) },
  })
}

/** 复盘卡片：点击当前信号切换隐藏/显示，点击其他信号则先切过去并显示 */
function toggleReviewSignal(row: OutcomeDetail) {
  if (!row) return
  if (row.event_id === reviewSignalId.value && row.symbol === symbol.value) {
    const next = new Set(reviewHidden.value)
    if (next.has(row.event_id)) {
      next.delete(row.event_id)
    } else {
      next.add(row.event_id)
    }
    reviewHidden.value = next
    return
  }
  selectReviewSignal(row)
}

function isReviewHidden(id: number) {
  return reviewHidden.value.has(id)
}

/** 复盘模式：在筛选列表内循环切换上/下一个信号 */
function switchReviewSignal(dir: number) {
  const rows = reviewRows.value
  if (!rows.length) return
  const cur = reviewIndex.value
  const base = cur >= 0 ? cur : dir > 0 ? -1 : 0
  const next = rows[(base + dir + rows.length) % rows.length]
  selectReviewSignal(next)
}

/** 退出复盘模式：移除 review 参数，回到当前品种的普通图表 */
function exitReviewMode() {
  appStore.reviewJumpFilters = null
  reviewRows.value = []
  reviewListKey.value = ''
  const query = { ...route.query }
  delete query.review
  if (symbol.value) {
    void router.replace({
      name: 'chart',
      params: { symbol: symbol.value },
      query,
    })
  }
}

function onReviewKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && reviewMode.value) {
    exitReviewMode()
    return
  }
  if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
  if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return
  const target = e.target as HTMLElement | null
  if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)) {
    return
  }
  e.preventDefault()
  chartRef.value?.stepCandles(e.key === 'ArrowLeft' ? -1 : 1)
}

/** 形态状态优先级：等待触发 > 已触发 > 已了结 > 已失效 */
function patternStateRank(state: string): number {
  if (state === 'pending') return 0
  if (state === 'triggered') return 1
  if (state === 'closed') return 2
  if (state === 'expired') return 3
  return 4
}

/** 把新事件表记录转换成图表组件使用的旧形态结构 */
function parseTrendDims(dims: string): {state: string; bonus: number} { try{ const o=JSON.parse(dims); return {state: o.trend_state || "", bonus: Number(o.trend_bonus)||0 } }catch{ return {state:"",bonus:0} } }
function toChartSignal(e: PatternEvent): PatternDto {
  const s0High = e.direction === 'down'
  return {
    number: e.id,
    level: e.level,
    logic_version: '4',
    warning_kind: e.warning_kind,
    direction: e.direction,
    grade: e.grade,
    s0: { index: 0, price: e.s0_price, is_high: s0High, ts: e.s0_ts },
    s1: { index: 0, price: e.s1_price, is_high: !s0High, ts: e.s1_ts },
    s2: { index: 0, price: e.s2_price, is_high: s0High, ts: e.s2_ts },
    a_bars: e.a_bars,
    b_bars: e.b_bars,
    a_move: e.a_move,
    b_move: e.b_move,
    retracement: e.retracement,
    state: e.state,
    category: '',
    entry: e.entry,
    stop: e.stop,
    target: e.target,
    risk: e.risk,
    space: 0,
    rr: e.rr,
    score: e.entry_score,
    warning_ts: e.warning_ts,
    trigger_ts: e.trigger_ts,
    vol_ratio: e.trigger_volume_ratio,
    vol_confirmed: e.trigger_ts != null,
    trigger_overshoot_r: e.overshoot_r,
    box: null,
    note: '',
    trend_state: parseTrendDims(e.entry_score_dims).state,
    trend_bonus: parseTrendDims(e.entry_score_dims).bonus,
    trend_label: trendText(parseTrendDims(e.entry_score_dims).state),
    active: true,
  }
}

/** 把复盘/历史明细行转换成图表组件使用的形态结构 */
function toChartSignalFromOutcome(row: OutcomeDetail): PatternDto {
  const s0High = row.direction === 'down'
  return {
    number: row.event_id,
    level: row.level,
    logic_version: row.logic_version,
    warning_kind: row.warning_kind,
    direction: row.direction,
    grade: row.grade,
    s0: { index: 0, price: row.s0_price, is_high: s0High, ts: row.s0_ts },
    s1: { index: 0, price: row.s1_price, is_high: !s0High, ts: row.s1_ts },
    s2: { index: 0, price: row.s2_price, is_high: s0High, ts: row.s2_ts },
    a_bars: row.a_bars ?? 0,
    b_bars: row.b_bars ?? 0,
    a_move: row.a_move ?? 0,
    b_move: row.b_move ?? 0,
    retracement: row.retracement ?? 0,
    state: row.state,
    category: '',
    entry: row.entry,
    stop: row.stop,
    target: row.target,
    risk: row.risk,
    space: 0,
    rr: row.rr,
    score: row.entry_score,
    warning_ts: row.warning_ts,
    trigger_ts: row.trigger_ts,
    vol_ratio: row.trigger_volume_ratio,
    vol_confirmed: row.trigger_ts != null,
    trigger_overshoot_r: row.overshoot_r,
    box: null,
    note: '',
    trend_state: parseTrendDims(row.entry_score_dims).state,
    trend_bonus: parseTrendDims(row.entry_score_dims).bonus,
    trend_label: trendText(parseTrendDims(row.entry_score_dims).state),
    active: false,
  }
}

/**
 * 最近一次扫描识别出的该品种仍在途N形态（策略基于 15m，与图表显示级别无关）。
 * 已了结、已失效、未知状态由信号源统一过滤，不进入右侧列表。
 * 排序规则：先按状态（等待触发 > 已触发），
 * 同一状态内按评分从高到低，评分相同按编号小的优先。
 */
const signals = computed<PatternDto[]>(() => {
  return scansStore.latestSignals
    .filter((s) => s.symbol === symbol.value)
    .map(toChartSignal)
    .sort((a, b) => {
      const rankA = patternStateRank(a.state)
      const rankB = patternStateRank(b.state)
      if (rankA !== rankB) return rankA - rankB
      if (b.score !== a.score) return b.score - a.score
      return a.number - b.number
    })
})

const recentSignals = ref<PatternDto[]>([])
const recentLoading = ref(false)
let recentSeq = 0

/** 该品种最近的历史形态：排除仍在途的当前信号，按预警时间倒序保留 5 个。 */
const recentHistorySignals = computed<PatternDto[]>(() => {
  const activeNumbers = new Set(signals.value.map((s) => s.number))
  return recentSignals.value
    .filter((s) => !activeNumbers.has(s.number))
    .slice()
    .sort((a, b) => {
      const ta = a.warning_ts ?? ''
      const tb = b.warning_ts ?? ''
      if (ta !== tb) return tb.localeCompare(ta)
      return b.number - a.number
    })
    .slice(0, 5)
})

async function loadRecentPatterns() {
  const sym = symbol.value
  const seq = ++recentSeq
  if (!sym) {
    recentSignals.value = []
    recentLoading.value = false
    return
  }
  recentLoading.value = true
  try {
    const rows = await api.getRecentOutcomes(20, { symbol: sym })
    if (seq !== recentSeq) return
    recentSignals.value = rows.map(toChartSignalFromOutcome).sort((a, b) => {
      const ta = a.warning_ts ?? ''
      const tb = b.warning_ts ?? ''
      if (ta !== tb) return tb.localeCompare(ta)
      return b.number - a.number
    })
  } catch {
    if (seq === recentSeq) recentSignals.value = []
  } finally {
    if (seq === recentSeq) recentLoading.value = false
  }
}

/** 被点击隐藏的形态编号（用于控制K线图上的展示） */
const hiddenNumbers = ref<Set<number>>(new Set())

/** 最近历史形态中用户主动点开绘制到K线图上的编号 */
const shownRecentNumbers = ref<Set<number>>(new Set())

/** 是否在K线图上标记当前视图的最高价/最低价 */
const showExtremes = ref(true)

/** 当前周期 MA20 长期趋势线（叠加在当前周期K线图上） */
const trendPoints = ref<TrendPointDto[]>([])
let trendSeq = 0
async function loadTrendLine() {
  const sym = symbol.value
  const tf = timeframe.value
  const seq = ++trendSeq
  if (!sym) {
    trendPoints.value = []
    return
  }
  try {
    const points = await api.getTrendSeries(sym, tf, chartLoadLimit.value)
    if (seq === trendSeq) trendPoints.value = points
  } catch {
    if (seq === trendSeq) trendPoints.value = []
  }
}

/** 记录“默认隐藏规则”是否已应用到当前 品种+周期+设置 */
const hiddenApplied = ref('')

/**
 * 默认隐藏规则：设置开启时保留排序最靠前的首个信号，其余隐藏；
 * 设置关闭时全部隐藏，避免画线/标点堆叠看不清。
 * 用户手动点开/隐藏后不再自动重置；切换品种、周期或修改设置时重新按默认应用。
 */
function applyDefaultHidden() {
  const showFirst = settingsStore.settings.ui.chart_show_first_signal ?? true
  const key = `${symbol.value}|${timeframe.value}|${showFirst}`
  if (hiddenApplied.value === key) return
  const nums = signals.value.map((s) => s.number)
  if (!nums.length) return
  hiddenApplied.value = key
  hiddenNumbers.value = showFirst ? new Set(nums.slice(1)) : new Set(nums)
}

/** 左侧品种列表开关与行情快照 */
const showList = ref(true)
const chartRef = ref<{ stepCandles: (dir: number) => void } | null>(null)
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
    // 桶起点基准：优先用库内同桶成形K线（定时/手动刷新已落库，带真实开盘与成交量），
    // 否则用前一根收盘价保证连续——避免首笔实时报价来晚了导致
    // “开盘价离前收很远、看起来像跳空”的假缺口
    const prev = displayRows.value[displayRows.value.length - 1]
    const seed = prev && prev.ts === label ? prev : null
    const open = seed ? seed.open : prev ? prev.close : latest
    arr.push({
      symbol: symbol.value,
      timeframe: timeframe.value,
      ts: label,
      open,
      high: seed ? Math.max(seed.high, latest) : Math.max(open, latest),
      low: seed ? Math.min(seed.low, latest) : Math.min(open, latest),
      close: latest,
      volume: seed?.volume ?? 0,
      hold: seed?.hold ?? 0,
      source: 'live',
      rollover: false,
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
  // 只有最后一根实时桶还在形成中，允许它更新历史行；
  // 更早的“已收未落地”桶一旦库里已有成形K线，就以库内为准，避免旧实时收盘覆盖落库结果。
  const currentLiveTs = live.length ? live[live.length - 1].ts : null
  const out: KlineRow[] = []
  for (const r of rows) {
    const bar = liveByTs.get(r.ts)
    if (bar && bar.ts === currentLiveTs) {
      // 库内已有同桶K线（刷新落库的成形桶）：保留真实开盘/成交量，
      // 实时值只负责把高低扩展到已观测到的范围、并更新收盘
      out.push({
        ...r,
        high: Math.max(r.high, bar.high),
        low: Math.min(r.low, bar.low),
        close: bar.close,
        source: 'live',
      })
    } else {
      out.push(r)
    }
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
/** 插入线：将要插入到该代码行之前；null 表示插入到列表末尾 */
const insertBeforeCode = ref<string | null>(null)
const LIST_REORDER_KEY = 'ntrend_chart_list_reorder_enabled'
const reorderEnabled = ref(localStorage.getItem(LIST_REORDER_KEY) === '1')
watch(reorderEnabled, (v) => {
  try { localStorage.setItem(LIST_REORDER_KEY, v ? '1' : '0') } catch {}
})
const isListDragDisabled = computed(() => reviewMode.value || !reorderEnabled.value)
const listDragging = ref(false)

/** 拉取当前分组的成员（按组内 sort_index 顺序） */
async function loadGroupSymbols() {
  if (groupsStore.selectedId == null) {
    // 全部品种视图：以服务端全局顺序为准（拖拽/别处重排后重拉）
    await symbolsStore.load()
    groupSymbols.value = [...symbolsStore.symbols]
    return
  }
  groupSymbols.value = await api.getGroupSymbols(groupsStore.selectedId)
}

/** 左侧列表：分组视图按组内顺序，全部视图按代码序 */
const visibleSymbols = computed(() => groupSymbols.value)

/**
 * 拖拽结束落库：vuedraggable 已把 groupSymbols 调整为新顺序，
 * 这里把顺序持久化并广播，让列表页表格同步重拉。
 */
async function persistListOrder() {
  listDragging.value = false
  insertBeforeCode.value = null
  // 拖拽结束后浏览器可能补发一次 click，这里临时抑制行点击跳转
  symbolSuppressClick = true
  setTimeout(() => {
    symbolSuppressClick = false
  }, 0)
  const groupId = groupsStore.selectedId
  const codes = groupSymbols.value.map((s) => s.code)
  try {
    // 分组视图写组内顺序；全部品种视图写全局顺序
    if (groupId != null) {
      await api.reorderGroupSymbols(groupId, codes)
    } else {
      await api.reorderSymbols(codes)
    }
    groupsStore.bumpRevision()
  } catch (err) {
    notify.error(String(err))
    await loadGroupSymbols() // 落库失败则回滚为服务端顺序
  }
}

/** 拖拽开始：清掉上一次的插入线 */
function onListDragStart() {
  listDragging.value = true
  insertBeforeCode.value = null
}

/**
 * SortableJS 的移动判定：按「光标在目标行上半 → 插到该行之前；下半 → 插到该行之后」
 * 主动决定插入方向，并让蓝线与该规则完全一致，实现所见即所得。
 * 不能依赖 Sortable 默认的 willInsertAfter：它默认是交换语义（把拖拽项插到目标行所在的
 * 位置），与按行边界理解的直觉差一行；返回 1/-1 会覆盖 Sortable 内部的插入方向。
 */
function onListMove(evt: {
  related: HTMLElement | null
  relatedRect: { top: number; bottom: number; height: number } | null
  willInsertAfter?: boolean
  originalEvent?: Event | null
}): boolean | 1 | -1 {
  const related = evt.related as HTMLElement | null
  const isRow = !!related?.closest?.('.sl-row')
  if (!isRow || !related) {
    // 目标是容器本身（列表末尾的空区域）：按默认逻辑插到末尾
    const boundary = evt.willInsertAfter ? related?.nextElementSibling : related
    insertBeforeCode.value =
      (boundary as HTMLElement | null)?.getAttribute('data-code') ?? null
    return true
  }
  const rect =
    evt.relatedRect ??
    (related.getBoundingClientRect() as { top: number; bottom: number; height: number })
  const mouseY = (evt.originalEvent as MouseEvent | null)?.clientY ?? rect.top + rect.height / 2
  const after = mouseY > rect.top + rect.height / 2
  // 插入 related 之后时，边界是 related 的下一行；插入 related 之前时，边界就是 related
  const boundary = after ? related.nextElementSibling : related
  insertBeforeCode.value =
    (boundary as HTMLElement | null)?.getAttribute('data-code') ?? null
  return after ? 1 : -1
}

/** 行点击进入K线图；拖拽结束后紧接着的 click 不触发跳转 */
function onSymbolRowClick(code: string) {
  if (reviewMode.value) return
  if (symbolSuppressClick) return
  router.push({ name: 'chart', params: { symbol: code } })
}

/** 左侧品种行右键菜单：与表格行一致（分组操作 + 彻底删除） */
async function onSymbolContextMenu(row: { code: string }, e: MouseEvent) {
  if (reviewMode.value) {
    e.preventDefault()
    return
  }
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

/** 每个品种取优先级最高的信号：与表格页同源同规则 */
const signalBySymbol = computed<Record<string, PatternEvent | null>>(() => {
  const out: Record<string, PatternEvent | null> = {}
  const latest = scansStore.latestSignals
  if (!latest.length) return out
  const rankOf = (s: PatternEvent): number => {
    if (s.state === 'pending') return 0
    if (s.state === 'triggered') return 1
    if (s.state === 'closed') return 2
    if (s.state === 'expired') return 3
    return 4
  }
  for (const s of latest) {
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
      if (s.entry_score > prev.entry_score || (s.entry_score === prev.entry_score && s.id < prev.id)) {
        out[s.symbol] = s
      }
    }
  }
  return out
})

/** 信号状态 → 列表里的短标签 */
function sigLabel(state: string) {
  switch (state) {
    case 'pending':
      return '等待触发'
    case 'triggered':
      return '已触发'
    case 'closed':
      return '已了结'
    case 'expired':
      return '已失效'
    default:
      return state
  }
}

/** 信号状态 → 样式类型 */
function sigType(state: string) {
  if (state === 'pending') return 'pending'
  if (state === 'triggered') return 'triggered'
  if (state === 'closed') return 'stale'
  return 'expired'
}

/** 评分分档：达到配置门槛按完整样式显示，每低 0.2 分缩小变浅一档 */
function scoreTier(score: number | null | undefined) {
  const fullScore = settingsStore.settings.ui.score_pill_full_score ?? 3.5
  if (score == null) return 'score-0'
  if (score >= fullScore) return 'score-5'
  if (score >= fullScore - 0.2) return 'score-4'
  if (score >= fullScore - 0.4) return 'score-3'
  if (score >= fullScore - 0.6) return 'score-2'
  if (score >= fullScore - 0.8) return 'score-1'
  return 'score-0'
}

/** 悬停提示：形态编号、方向、级别、状态与触发/预警时间 */
function sigTitle(s: PatternEvent) {
  const dir = s.direction === 'up' ? '做多' : s.direction === 'down' ? '做空' : s.direction
  const t = s.trigger_ts || s.warning_ts || ''
  return `#${s.id} ${dir} ${levelSuffix(s.level)} ${sigLabel(s.state)}${t ? ` ${t}` : ''} 评分 ${s.entry_score.toFixed(2)}`
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
  // 拖拽中暂停快照刷新，避免列表重渲染打断正在进行的拖拽排序
  if (listDragging.value) return
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
      if (listDragging.value) return
      const next = { ...rowFlash.value }
      delete next[code]
      rowFlash.value = next
      flashTimers.delete(code)
    }, FLASH_MS),
  )
}

const visibleSignals = computed<PatternDto[]>(() => {
  if (reviewOverlay.value) {
    return reviewHidden.value.has(reviewSignalId.value ?? -1)
      ? []
      : [toChartSignal(reviewOverlay.value.event)]
  }
  if (reviewMode.value) return []
  const active = signals.value.filter((s) => !hiddenNumbers.value.has(s.number))
  const recent = recentHistorySignals.value.filter((s) => shownRecentNumbers.value.has(s.number))
  return [...active, ...recent]
})

function isHidden(num: number) {
  return hiddenNumbers.value.has(num)
}

function isRecentShown(num: number) {
  return shownRecentNumbers.value.has(num)
}

function toggleRecentPattern(num: number) {
  const next = new Set(shownRecentNumbers.value)
  if (next.has(num)) {
    next.delete(num)
  } else {
    next.add(num)
  }
  shownRecentNumbers.value = next
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
  if (reviewMode.value) {
    switchReviewSignal(dir)
  } else {
    switchSymbol(dir)
  }
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
  return l === 'fine' ? '精细' : l === 'large' ? '较大' : l === 'box' ? '箱体' : l
}

function levelSuffix(l: string) {
  return `${levelText(l)}${l === 'box' ? '' : 'N'}`
}

function trendText(state?: string) {
  switch (state) {
    case 'STRONG_UP': return '强多'
    case 'WEAK_UP': return '弱多'
    case 'STRONG_DOWN': return '强空'
    case 'WEAK_DOWN': return '弱空'
    case 'RANGE':
    case 'NEUTRAL': return '震荡'
    default: return state || '—'
  }
}
function trendClass(state?: string) {
  switch (state) {
    case 'STRONG_UP': return 'trend-strong-up'
    case 'WEAK_UP': return 'trend-weak-up'
    case 'STRONG_DOWN': return 'trend-strong-down'
    case 'WEAK_DOWN': return 'trend-weak-down'
    case 'RANGE':
    case 'NEUTRAL': return 'trend-range'
    default: return 'trend-unknown'
  }
}
function trendBonusText(s: PatternDto) {
  if (!s.trend_bonus || s.trend_bonus === 0) return ''
  return `+${s.trend_bonus.toFixed(1)}`
}

// 2026-08-14：预警质量分已计入 score，这里只显示类型标签，不再叠加显示加分。
function warningKindText(kind?: string) {
  switch (kind) {
    case 'strong':
      return '强反转'
    case 'engulf':
      return '强反转'
    case 'wick':
      return '长影线'
    case 'fast':
      // 历史记录兼容；新扫描不再产生 fast。
      return '快速路径'
    case 'cumulative':
      return '累计覆盖'
    default:
      return '—'
  }
}

const reviewOutcomeLabel: Record<string, { text: string; cls: 'win' | 'loss' | 'plain' | 'warn' }> = {
  win: { text: '盈利', cls: 'win' },
  loss: { text: '亏损', cls: 'loss' },
  no_trigger: { text: '未触发', cls: 'plain' },
  open: { text: '持仓中', cls: 'warn' },
  insufficient_data: { text: '数据不足', cls: 'plain' },
  rollover: { text: '换月', cls: 'warn' },
}

const reviewExitLabel: Record<string, string> = {
  stop: '止损',
  target: '止盈',
  no_follow: '无跟随退出',
  time_exit: '时间退出',
  rollover: '换月',
  '': '—',
}

function reviewOutcome(row: OutcomeDetail) {
  return reviewOutcomeLabel[row.outcome] ?? { text: row.outcome, cls: 'plain' as const }
}

function rvNum(v: number | null | undefined, digits = 2) {
  return v == null ? '—' : v.toFixed(digits)
}

function rvMult(v: number | null | undefined) {
  return v == null ? '—' : `${v.toFixed(2)}×`
}

function rvBool(v: boolean | null) {
  return v == null ? '—' : v ? '是' : '否'
}

function rvRClass(v: number | null | undefined) {
  return v == null ? 'is-neutral' : v >= 0 ? 'is-pos' : 'is-neg'
}

function rvGap(row: OutcomeDetail) {
  const parts: string[] = []
  if (row.gap_crossed_entry) parts.push('入')
  if (row.gap_crossed_exit) parts.push('出')
  return parts.length ? parts.join('/') : '—'
}

function rvSpeed(row: OutcomeDetail) {
  return row.a_move == null || row.b_move == null || row.a_move === 0
    ? '—'
    : (row.b_move / row.a_move).toFixed(2)
}

function rvBarRatio(row: OutcomeDetail) {
  return row.a_bars == null || row.b_bars == null || row.a_bars === 0
    ? '—'
    : (row.b_bars / row.a_bars).toFixed(2)
}

type RvDims = {
  dimA: number | null
  dimB: number | null
  dimWarning: number | null
}

const rvDimsCache = new WeakMap<OutcomeDetail, RvDims>()

function rvDims(row: OutcomeDetail): RvDims {
  const cached = rvDimsCache.get(row)
  if (cached) return cached
  const dims: RvDims = { dimA: null, dimB: null, dimWarning: null }
  try {
    const v = JSON.parse(row.entry_score_dims) as Record<string, unknown>
    if (typeof v.dim_a === 'number') dims.dimA = v.dim_a
    if (typeof v.dim_b === 'number') dims.dimB = v.dim_b
    if (typeof v.dim_warning === 'number') dims.dimWarning = v.dim_warning
  } catch {
    // 历史脏数据按缺失处理，显示为 —
  }
  rvDimsCache.set(row, dims)
  return dims
}

function rvDimClass(v: number | null) {
  if (v == null) return 'is-neutral'
  if (v >= 3.5) return 'is-good'
  if (v >= 3.0) return 'is-mid'
  return 'is-weak'
}

function rvLegPerBar(move: number | null | undefined, bars: number | null | undefined) {
  return move == null || bars == null || bars <= 0 ? '—' : `${(move / bars).toFixed(1)}点/根`
}

function rvQClass(v: number | null | undefined) {
  if (v == null) return 'is-neutral'
  if (v >= 0.5) return 'is-good'
  if (v >= 0.35) return 'is-mid'
  return 'is-weak'
}

function rvNetMove(row: OutcomeDetail) {
  return row.a_net_move == null ? '—' : `${row.a_net_move.toFixed(1)}点`
}

function rvNetTitle(row: OutcomeDetail) {
  if (row.a_move == null || row.a_net_move == null) return ''
  const gap = row.a_gap_sum ?? 0
  return gap > 0
    ? `账面 ${row.a_move.toFixed(1)}点 - 大跳空 ${gap.toFixed(1)}点`
    : `账面 ${row.a_move.toFixed(1)}点`
}

function rvGapDetail(row: OutcomeDetail) {
  if (row.a_gap_count == null) return '—'
  if (row.a_gap_count === 0) return '无'
  return `${row.a_gap_count}根/${rvNum(row.a_gap_sum)}点`
}

function rvAtrRatio(row: OutcomeDetail) {
  return row.a_net_move == null || row.a_atr == null || row.a_atr <= 0
    ? '—'
    : `${(row.a_net_move / row.a_atr).toFixed(2)}×`
}

function rvLegAtr(row: OutcomeDetail) {
  return row.a_net_move == null ||
    row.a_bars == null ||
    row.a_atr == null ||
    row.a_bars <= 0 ||
    row.a_atr <= 0
    ? '—'
    : `${((row.a_net_move / row.a_bars) / row.a_atr).toFixed(2)}×`
}

function rvWeakening(row: OutcomeDetail) {
  if (row.b_weakening == null) return '—'
  if (!row.b_weakening) return '否'
  return row.b_weakening_ratio == null ? '是' : `是(${row.b_weakening_ratio.toFixed(2)})`
}

function stateType(state: string): 'info' | 'success' | 'warning' | 'default' | 'error' {
  if (state === 'pending') return 'info'
  if (state === 'triggered') return 'success'
  if (state === 'closed') return 'warning'
  return 'default'
}

const VOL_CONFIRM_RATIO = 2.0

function volStatusText(s: PatternDto): string {
  if (!s.vol_confirmed) return '量能待收盘'
  if (s.vol_ratio == null) return '量能缺失'
  return s.vol_ratio >= VOL_CONFIRM_RATIO ? '放量确认' : '未放量'
}

function volStatusClass(s: PatternDto): 'pending' | 'missing' | 'confirmed' | 'plain' {
  if (!s.vol_confirmed) return 'pending'
  if (s.vol_ratio == null) return 'missing'
  return s.vol_ratio >= VOL_CONFIRM_RATIO ? 'confirmed' : 'plain'
}

function overshootStatusText(s: PatternDto): string {
  if (!s.vol_confirmed) return '待收盘'
  if (s.trigger_overshoot_r == null) return '数据缺失'
  return '已确认'
}

function overshootStatusClass(s: PatternDto): 'pending' | 'missing' | 'done' {
  if (!s.vol_confirmed) return 'pending'
  if (s.trigger_overshoot_r == null) return 'missing'
  return 'done'
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

// 修改“默认显示首个信号”设置后，重新应用K线图上的默认隐藏规则
watch(() => settingsStore.settings.ui.chart_show_first_signal, () => {
  hiddenApplied.value = ''
  applyDefaultHidden()
})

// 复盘模式：query.review 变化（或切换品种）时重新加载复盘点位
watch([symbol, reviewSignalId], () => {
  reviewHidden.value = new Set()
  void loadReviewOverlay()
}, { immediate: true })

// 复盘模式：首次进入或筛选上下文变化时拉取一次明细列表，后续切换信号直接复用
watch(
  [reviewSignalId, () => appStore.reviewJumpFilters],
  () => {
    if (reviewMode.value) void loadReviewList()
  },
  { immediate: true },
)

// 复盘模式：当前行变化时把高亮行滚进可视区
watch([reviewIndex, reviewRows], async () => {
  if (!reviewMode.value) return
  await nextTick()
  document.querySelector('.review-row.is-active')?.scrollIntoView({ block: 'nearest' })
})

watch([symbol, timeframe], async () => {
  hiddenApplied.value = ''
  applyDefaultHidden()
  liveBars.value = []
  shownRecentNumbers.value = new Set()
  trendPoints.value = []
  loadRecentPatterns()
  if (symbol.value) {
    await klinesStore.load(symbol.value, timeframe.value, chartLoadLimit.value)
    await loadTrendLine()
    scansStore.refreshLatestSignals()
  }
}, { immediate: true })

// 分组/组内顺序在别处被改动（如列表页表格拖拽）时，重拉本页列表
watch(() => groupsStore.revision, () => loadGroupSymbols())

onMounted(async () => {
  window.addEventListener('keydown', onReviewKeydown)
  unlisteners.push(
    await onScanCompleted((result) => {
      scansStore.ingest(result)
      loadSnapshots()
      scansStore.refreshLatestSignals()
      loadRecentPatterns()
    }),
  )
  unlisteners.push(
    await onDataUpdated(() => {
      // 数据库刷新后实时临时桶已经转正，清掉残留，避免旧收盘继续覆盖历史K线
      liveBars.value = []
      loadSnapshots()
      scansStore.refreshLatestSignals()
      loadRecentPatterns()
      // 定时入库后静默重载完整K线，让刚收盘的实时桶转正为历史K线
      if (symbol.value) {
        klinesStore.load(symbol.value, timeframe.value, chartLoadLimit.value, true)
        loadTrendLine()
      }
    }),
  )
  // 实时现价：合并进快照表，缺失品种保留旧值，避免盘口跳动时整表闪空
  unlisteners.push(
    await onQuotesUpdated((list) => {
      if (!list.length) return
      // 拖拽中暂停快照/闪烁/实时K线更新，避免列表重渲染打乱正在拖拽的 DOM
      if (listDragging.value) return
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
      scansStore.refreshLatestSignals()
    } catch {
      // 无数据时扫描失败不影响看图
    }
  }
  // 首屏K线已由 watch([symbol,timeframe], immediate:true) 加载，
  // 这里仅做兜底：若因时序/空路由未加载到则补拉一次，避免显示无数据
  if (symbol.value && (!klinesStore.rows.length || klinesStore.rows[0]?.symbol !== symbol.value || klinesStore.rows[0]?.timeframe !== timeframe.value)) {
    await klinesStore.load(symbol.value, timeframe.value, chartLoadLimit.value)
    await loadTrendLine()
    scansStore.refreshLatestSignals()
    loadRecentPatterns()
  }
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onReviewKeydown)
  for (const timer of flashTimers.values()) clearTimeout(timer)
  flashTimers.clear()
  for (const fn of unlisteners) fn()
})
</script>

<template>
  <div class="chart-page">
    <div class="topbar">
      <div class="topbar-left">
        <n-button quaternary size="small" class="nav-btn" @click="router.push({ name: 'dashboard' })">
          <template #icon>
            <n-icon :component="ArrowLeft" />
          </template>
          返回
        </n-button>
        <n-button quaternary size="small" class="nav-btn" @click="showList = !showList">
          <template #icon>
            <n-icon :component="List" />
          </template>
          {{ showList ? '收起列表' : '品种列表' }}
        </n-button>
        <n-button
          v-if="reviewMode"
          secondary
          size="small"
          class="nav-btn review-exit-btn"
          @click="exitReviewMode"
        >
          <template #icon>
            <n-icon :component="X" />
          </template>
          退出复盘
        </n-button>
      </div>

      <div class="topbar-symbol">
        <span class="sym-code">{{ symbol }}</span>
        <span v-if="currentSymbol?.name && currentSymbol.name !== symbol" class="sym-name">
          {{ currentSymbol.name }}
        </span>
        <span class="sym-divider" />
        <span v-if="quotePrice !== null" class="sym-price" :style="{ color: quoteColor }">
          {{ quotePrice.toFixed(1) }}
        </span>
        <span
          v-if="snapshot?.change_pct != null"
          class="sym-change"
          :style="{ color: quoteColor, background: quoteBg(snapshot.change_pct) }"
        >
          {{ fmtChange(snapshot.change_pct) }}
        </span>
      </div>

            <div class="reorder-control" :class="{ 'is-enabled': reorderEnabled && !reviewMode, disabled: reviewMode }" :title="reviewMode ? '复盘模式下不可排序' : (reorderEnabled ? '已开启拖拽排序 · 拖动左侧品种可重排' : '已关闭拖拽 · 开启后可拖动左侧品种排序')">
              <n-icon :component="GripVertical" class="reorder-icon" :size="14" />
              <span class="reorder-label">拖拽排序</span>
              <n-switch v-model:value="reorderEnabled" size="small" :disabled="reviewMode" :rail-style="() => ({ background: reorderEnabled && !reviewMode ? '#3b82f6' : undefined })">
                <template #checked>开</template>
                <template #unchecked>关</template>
              </n-switch>
              <span class="reorder-state" :class="{ on: reorderEnabled && !reviewMode }">{{ reorderEnabled && !reviewMode ? '已开启' : '已关闭' }}</span>
            </div>

      <div class="topbar-timeframes">
        <div class="tf-group">
          <button
            v-for="t in visibleTimeframes"
            :key="t"
            type="button"
            class="tf-btn"
            :class="{ active: timeframe === t, 'is-disabled': reviewMode }"
            :disabled="reviewMode"
            :title="reviewMode ? '复盘模式固定 15m' : t"
            @click="timeframe = t"
          >
            {{ t }}
          </button>
        </div>
        <button
          type="button"
          class="tf-btn hl-btn"
          :class="{ active: showExtremes }"
          :title="showExtremes ? '隐藏最高/最低点标记' : '标记当前视图最高/最低点'"
          @click="showExtremes = !showExtremes"
        >
          高低
        </button>
        <n-popover
          placement="bottom-end"
          trigger="click"
          :show-arrow="false"
          style="padding: 0"
        >
          <template #trigger>
            <button
              type="button"
              class="tf-more"
              :disabled="reviewMode"
              title="周期显示设置"
            >
              <n-icon :component="Adjustments" />
            </button>
          </template>
          <div class="tf-settings">
            <div class="tf-settings-title">更多周期选择</div>
            <div class="tf-settings-list">
              <label v-for="t in allTimeframes" :key="t" class="tf-check">
                <n-checkbox
                  :checked="settingsStore.settings.ui.timeframes.includes(t)"
                  @update:checked="(v: boolean) => toggleTimeframe(t, v)"
                />
                <span>{{ t }}</span>
              </label>
            </div>
            <div class="tf-settings-hint">勾选后立即生效，切换栏只显示勾选的周期</div>
          </div>
        </n-popover>
      </div>
    </div>

    <div class="main">
      <div v-if="showList" class="symbol-list" :class="{ 'can-reorder': reorderEnabled && !reviewMode }">
        <div class="sl-title">品种</div>
        <n-scrollbar style="flex: 1">
          <VueDraggable
            v-model="groupSymbols"
            item-key="code"
            :disabled="isListDragDisabled"
            class="sl-list"
            :class="{ 'insert-at-end': listDragging && insertBeforeCode === null }"
            :animation="150"
            :force-fallback="true"
            fallback-on-body
            fallback-class="sl-row-fallback"
            ghost-class="sl-row-ghost"
            chosen-class="sl-row-chosen"
            :move="onListMove"
            @start="onListDragStart"
            @end="persistListOrder"
          >
            <template #item="{ element }">
              <div
                class="sl-row"
                :data-code="element.code"
                :class="[
                  { active: element.code === symbol },
                  { 'insert-before': insertBeforeCode === element.code },
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
                  :class="[
                    'is-' + sigType(signalBySymbol[element.code]?.state ?? ''),
                    'is-' + scoreTier(signalBySymbol[element.code]?.entry_score),
                  ]"
                  :title="sigTitle(signalBySymbol[element.code]!)"
                >
                  {{ sigLabel(signalBySymbol[element.code]?.state ?? '') }}
                </span>
                <span v-if="getSingleBar(element.code)" :style="singleBarBadgeStyle(getSingleBar(element.code)!.kind) + 'margin-left:6px;padding:0 6px;font-size:10px;line-height:16px;display:inline-flex;align-items:center'" :title="singleBarTitle(getSingleBar(element.code)!)" >{{ getSingleBar(element.code)!.label }}</span>
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
          ref="chartRef"
          :symbol="symbol"
          :timeframe="timeframe"
          :rows="displayRows"
          :signals="visibleSignals"
          :show-extremes="showExtremes"
          :review-exit="reviewExit"
          :focus-ts="reviewFocusTs" :focus-key="reviewFocusKey"
           :trend-points="trendPoints"
          :single-bars="chartSingleBars"
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

        <div v-if="reviewMode" class="patterns-card review-card">
          <div class="patterns-title">复盘信号明细（{{ reviewRows.length }}）</div>
          <n-scrollbar style="flex: 1">
            <div v-if="reviewLoading" class="patterns-empty">正在加载信号明细...</div>
            <div v-else-if="reviewRows.length" class="review-list">
              <div
                v-for="row in reviewRows"
                :key="row.event_id"
                class="review-row"
                :class="[
                  row.direction === 'up' ? 'is-up' : 'is-down',
                  {
                    'is-active': row.event_id === reviewSignalId,
                    'is-hidden': isReviewHidden(row.event_id),
                  },
                ]"
                :title="
                  row.event_id === reviewSignalId
                    ? isReviewHidden(row.event_id)
                      ? '点击显示该信号绘制'
                      : '点击隐藏该信号绘制'
                    : '点击查看该信号'
                "
                @click="toggleReviewSignal(row)"
              >
                <div class="rv-head">
                  <span class="rv-id">#{{ row.event_id }}</span>
                  <span class="rv-symbol">{{ row.symbol }}</span>
                  <span class="rv-badge">
                    {{ dirText(row.direction) }} {{ levelSuffix(row.level) }}
                  </span>
                  <span class="rv-grade">{{ row.grade }}</span>
                  <span class="rv-warning">{{ warningKindText(row.warning_kind) }}</span>
                  <span class="rv-score">
                    <b>{{ row.entry_score.toFixed(2) }}</b>
                    <em>评分</em>
                  </span>
                  <span
                    v-if="row.event_id === reviewSignalId"
                    class="rv-eye"
                    :title="isReviewHidden(row.event_id) ? '点击显示该信号绘制' : '点击隐藏该信号绘制'"
                  >
                    <n-icon
                      :component="isReviewHidden(row.event_id) ? EyeOff : Eye"
                      :size="14"
                      :color="isReviewHidden(row.event_id) ? '#94a3b8' : '#f97316'"
                    />
                  </span>
                </div>

                <div class="rv-meta">
                  <span class="rv-time" :title="row.created_at">{{ fmtRecentTime(row.created_at) }}</span>
                  <span class="rv-outcome" :class="reviewOutcome(row).cls">
                    {{ reviewOutcome(row).text }}
                  </span>
                  <span class="rv-exit">{{ reviewExitLabel[row.exit_reason] ?? row.exit_reason }}</span>
                </div>
                <div class="rv-user">
                  <!-- 第一行：开仓状态 + 批注数量 -->
                  <div class="rv-ann-header">
    <span
        class="rv-opened"
        :class="{
        'is-open': row.opened === true,
        'is-skip': row.opened === false
      }"
    >
      {{ row.opened == null ? '开仓未记录' : row.opened ? '已按建议开仓' : '未开仓' }}
    </span>

                    <span
                        v-if="row.annotations?.length"
                        class="rv-ann-count"
                    >
      共 {{ row.annotations.length }} 条批注
    </span>
                  </div>

                  <!-- 第二行开始：具体批注 -->
                  <div
                      v-if="row.annotations?.length"
                      class="rv-ann"
                  >
                    <div
                        v-for="(annotation, index) in row.annotations"
                        :key="index"
                        class="rv-ann-item"
                    >
      <span class="rv-ann-index">
        {{ index + 1 }}.
      </span>

                      <span class="rv-ann-content">
        {{ annotation.content }}
      </span>

                      <span class="rv-ann-time">
        {{ annotation.created_at }}
      </span>
                    </div>
                  </div>
                </div>
                <div class="rv-grid">
                  <div class="rv-item">
                    <span>预警时间</span>
                    <b>{{ fmtRecentTime(row.warning_ts) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>60m趋势</span>
                    <b :class="trendClass(parseTrendDims(row.entry_score_dims).state)">{{ trendText(parseTrendDims(row.entry_score_dims).state) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>触发时间</span>
                    <b>{{ row.trigger_ts ? fmtRecentTime(row.trigger_ts) : '—' }}</b>
                  </div>
                  <div class="rv-item">
                    <span>入场</span>
                    <b>{{ row.entry.toFixed(1) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>止损</span>
                    <b>{{ row.stop.toFixed(1) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>目标</span>
                    <b>{{ row.target.toFixed(1) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>RR</span>
                    <b>{{ row.rr.toFixed(2) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>触发价</span>
                    <b>{{ row.trigger_price == null ? '—' : row.trigger_price.toFixed(1) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>追价深度</span>
                    <b>{{ row.overshoot_r == null ? '—' : `${row.overshoot_r.toFixed(2)}R` }}</b>
                  </div>
                  <div class="rv-item">
                    <span>触发量能</span>
                    <b>{{ rvMult(row.trigger_volume_ratio) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>持仓评分</span>
                    <b>{{ rvNum(row.hold_score) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>出场价</span>
                    <b>{{ row.exit_price == null ? '—' : row.exit_price.toFixed(1) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>R</span>
                    <b :class="rvRClass(row.r_multiple)">{{ fmtR(row.r_multiple) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>MFE</span>
                    <b :class="rvRClass(row.mfe_r)">{{ rvNum(row.mfe_r) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>MAE</span>
                    <b :class="rvRClass(row.mae_r)">{{ rvNum(row.mae_r) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>K线</span>
                    <b>{{ row.bars_held ?? '—' }}</b>
                  </div>
                  <div class="rv-item">
                    <span>净R</span>
                    <b :class="rvRClass(row.net_r)">{{ fmtR(row.net_r) }}</b>
                  </div>
                  <div class="rv-section">ABC结构</div>
                  <div class="rv-item">
                    <span>A的q</span>
                    <b :class="rvQClass(row.a_q)">{{ rvNum(row.a_q, 3) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>A净幅度</span>
                    <b :title="rvNetTitle(row)">{{ rvNetMove(row) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>A跳空</span>
                    <b>{{ rvGapDetail(row) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>A幅度ATR</span>
                    <b>{{ rvAtrRatio(row) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>A速度</span>
                    <b>{{ rvLegPerBar(row.a_net_move, row.a_bars) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>A速度ATR</span>
                    <b>{{ rvLegAtr(row) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>B速度</span>
                    <b>{{ rvLegPerBar(row.b_move, row.b_bars) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>A过长</span>
                    <b>{{ rvBool(row.a_too_long) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>B过长</span>
                    <b>{{ rvBool(row.b_too_long) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>B过快</span>
                    <b>{{ rvBool(row.b_fast) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>B弱化</span>
                    <b>{{ rvWeakening(row) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>A质量</span>
                    <b :class="rvDimClass(rvDims(row).dimA)">{{ rvNum(rvDims(row).dimA) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>B质量</span>
                    <b :class="rvDimClass(rvDims(row).dimB)">{{ rvNum(rvDims(row).dimB) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>预警质量</span>
                    <b :class="rvDimClass(rvDims(row).dimWarning)">{{ rvNum(rvDims(row).dimWarning) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>A段</span>
                    <b>{{ row.a_move == null ? '—' : `${row.a_move.toFixed(1)}点/${row.a_bars ?? '?'}根` }}</b>
                  </div>
                  <div class="rv-item">
                    <span>B段</span>
                    <b>{{ row.b_move == null ? '—' : `${row.b_move.toFixed(1)}点/${row.b_bars ?? '?'}根` }}</b>
                  </div>
                  <div class="rv-item">
                    <span>回撤</span>
                    <b>{{ row.retracement == null ? '—' : `${(row.retracement * 100).toFixed(1)}%` }}</b>
                  </div>
                  <div class="rv-item">
                    <span>b/a速度</span>
                    <b>{{ rvSpeed(row) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>根数比</span>
                    <b>{{ rvBarRatio(row) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>换月</span>
                    <b>{{ rvBool(row.rollover_crossed) }}</b>
                  </div>
                  <div class="rv-item">
                    <span>缺口</span>
                    <b>{{ rvGap(row) }}</b>
                  </div>
                </div>
              </div>
            </div>
            <div v-else class="patterns-empty">当前筛选下暂无信号</div>
          </n-scrollbar>
        </div>

        <div v-else class="patterns-card">
          <div class="patterns-title">全部信号（{{ signals.length }}）</div>
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
                    <span class="pc-dir">{{ dirText(s.direction) }} {{ levelSuffix(s.level) }}</span>
                    <span class="pc-grade">{{ s.grade }}</span>
                    <span class="pc-warning">{{ warningKindText(s.warning_kind) }}</span>
                    <span v-if="s.trend_state" class="pc-trend" :class="trendClass(s.trend_state)" :title="s.trend_state || ''">{{ trendText(s.trend_state) }}<em v-if="s.trend_bonus"> {{ trendBonusText(s) }}</em></span>
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
                  <span class="dot"></span>{{ sigLabel(s.state) }}
                </div>

                <div class="pc-history">
                  <span class="pc-history-label">预警</span>
                  <span>{{ s.warning_ts ? fmtRecentTime(s.warning_ts) : '—' }}</span>
                  <span v-if="s.trigger_ts" class="pc-history-label"> / 触发</span>
                  <span v-if="s.trigger_ts">{{ fmtRecentTime(s.trigger_ts) }}</span>
                </div>

                <div v-if="s.trigger_ts" class="pc-vol" :class="volStatusClass(s)">
                  <span>触发量能</span>
                  <b v-if="s.vol_ratio != null">{{ s.vol_ratio.toFixed(2) }}×</b>
                  <em>{{ volStatusText(s) }}</em>
                </div>

                <div v-if="s.trigger_ts" class="pc-vol pc-overshoot" :class="overshootStatusClass(s)">
                  <span>追价深度</span>
                  <b v-if="s.trigger_overshoot_r != null">{{ s.trigger_overshoot_r.toFixed(2) }}R</b>
                  <em>{{ overshootStatusText(s) }}</em>
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
                <SignalNotes :event-id="s.number" />
              </div>
            </div>
            <div v-else class="patterns-empty">当前品种暂无识别出的信号</div>

            <div class="patterns-title recent-title">最近 5 个形态（{{ recentHistorySignals.length }}）</div>
            <div v-if="recentLoading" class="patterns-empty">正在加载历史形态...</div>
            <div v-else-if="recentHistorySignals.length" class="patterns-list recent-list">
              <div
                v-for="r in recentHistorySignals"
                :key="`recent-${r.number}`"
                class="pattern-card is-recent"
                :class="[
                  r.direction === 'up' ? 'is-up' : 'is-down',
                  { 'is-hidden': !isRecentShown(r.number) },
                ]"
                :title="
                  isRecentShown(r.number)
                    ? '点击在K线图上隐藏该形态'
                    : '点击在K线图上显示该形态'
                "
                @click="toggleRecentPattern(r.number)"
              >
                <div class="pc-head">
                  <div class="pc-badges">
                    <span class="pc-num">#{{ r.number }}</span>
                    <span class="pc-dir">{{ dirText(r.direction) }} {{ levelSuffix(r.level) }}</span>
                    <span class="pc-grade">{{ r.grade }}</span>
                    <span class="pc-warning">{{ warningKindText(r.warning_kind) }}</span>
                    <span v-if="r.trend_state" class="pc-trend" :class="trendClass(r.trend_state)" :title="r.trend_state || ''">{{ trendText(r.trend_state) }}<em v-if="r.trend_bonus"> {{ trendBonusText(r) }}</em></span>
                  </div>
                  <n-icon
                    :component="isRecentShown(r.number) ? Eye : EyeOff"
                    size="17"
                    :color="isRecentShown(r.number) ? '#1677ff' : '#94a3b8'"
                    style="margin-top: 2px"
                  />
                  <div class="pc-score">
                    <span class="pc-score-num">{{ r.score.toFixed(2) }}</span>
                    <span class="pc-score-label">评分</span>
                  </div>
                </div>

                <div class="pc-state" :class="stateType(r.state)">
                  <span class="dot"></span>{{ sigLabel(r.state) }}
                  <span v-if="r.trigger_ts" class="pc-state-time">{{ fmtRecentTime(r.trigger_ts) }}</span>
                </div>

                <div class="pc-history">
                  <span class="pc-history-label">预警</span>
                  <span>{{ r.warning_ts ? fmtRecentTime(r.warning_ts) : '—' }}</span>
                  <span v-if="r.trigger_ts" class="pc-history-label"> / 触发</span>
                  <span v-if="r.trigger_ts">{{ fmtRecentTime(r.trigger_ts) }}</span>
                </div>

                <div class="pc-prices">
                  <div class="pc-price">
                    <span>入场</span>
                    <b>{{ r.entry.toFixed(1) }}</b>
                  </div>
                  <div class="pc-price">
                    <span>止损</span>
                    <b>{{ r.stop.toFixed(1) }}</b>
                    <em class="pc-delta is-stop">{{ fmtDelta(r.stop - r.entry) }}点</em>
                  </div>
                  <div class="pc-price">
                    <span>目标</span>
                    <b>{{ r.target.toFixed(1) }}</b>
                    <em class="pc-delta is-target">{{ fmtDelta(r.target - r.entry) }}点</em>
                  </div>
                  <div class="pc-price">
                    <span>RR</span>
                    <b>{{ r.rr.toFixed(2) }}</b>
                  </div>
                </div>

                <div class="pc-legs">
                  <span>a段 {{ r.a_bars }}根 / {{ r.a_move.toFixed(1) }}点</span>
                  <span>b段 {{ r.b_bars }}根 / {{ r.b_move.toFixed(1) }}点</span>
                  <span>回撤 {{ (r.retracement * 100).toFixed(1) }}%</span>
                </div>
                <SignalNotes :event-id="r.number" />
              </div>
            </div>
            <div v-else class="patterns-empty">该品种暂无历史形态</div>
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
  height: 100%;
  background: #f5f7fa;
}
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 8px 14px;
  background: #fff;
  border-bottom: 1px solid #e5e7eb;
}
.topbar-left {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: none;
}
.nav-btn {
  border-radius: 8px;
}
.topbar-symbol {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  margin: 0 auto;
}
.sym-code {
  flex: none;
  font-size: 13px;
  font-weight: 500;
  color: #94a3b8;
  font-variant-numeric: tabular-nums;
}
.sym-name {
  flex: none;
  font-size: 18px;
  font-weight: 800;
  color: #1f2329;
  letter-spacing: 0.5px;
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sym-divider {
  flex: none;
  width: 1px;
  height: 18px;
  background: #e5e9ef;
}
.sym-price {
  font-size: 17px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.sym-change {
  font-size: 12px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  padding: 2px 8px;
  border-radius: 999px;
}
.reorder-control {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  flex: none;
  padding: 5px 10px 5px 9px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 999px;
  transition: all 0.2s ease;
  box-shadow: 0 1px 2px rgba(15,23,42,0.04);
}
.reorder-control:hover {
  border-color: #cbd5e1;
  background: #f1f5f9;
}
.reorder-control.is-enabled {
  background: linear-gradient(135deg, #eff6ff 0%, #dbeafe 100%);
  border-color: #93c5fd;
  box-shadow: 0 1px 6px rgba(59,130,246,0.18);
}
.reorder-control.disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.reorder-control .reorder-icon {
  color: #94a3b8;
  transition: color 0.2s;
  flex: none;
}
.reorder-control.is-enabled .reorder-icon {
  color: #3b82f6;
}
.reorder-label {
  font-size: 12.5px;
  font-weight: 600;
  color: #475569;
  white-space: nowrap;
  letter-spacing: 0.2px;
}
.reorder-control.is-enabled .reorder-label {
  color: #1e40af;
}
.reorder-state {
  font-size: 11px;
  font-weight: 600;
  color: #94a3b8;
  white-space: nowrap;
  min-width: 36px;
}
.reorder-state.on {
  color: #2563eb;
}
.topbar-timeframes {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: none;
}
.tf-group {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 3px;
  background: #f1f5f9;
  border-radius: 8px;
}
.tf-btn {
  border: none;
  background: transparent;
  padding: 4px 11px;
  border-radius: 6px;
  font-size: 13px;
  font-weight: 500;
  color: #64748b;
  font-variant-numeric: tabular-nums;
  cursor: pointer;
  transition:
    background 0.15s,
    color 0.15s,
    box-shadow 0.15s;
}
.tf-btn:hover {
  color: #1f2329;
  background: rgba(255, 255, 255, 0.7);
}
.tf-btn.active {
  background: #fff;
  color: #1677ff;
  font-weight: 600;
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.1);
}
.hl-btn {
  background: #f1f5f9;
}
.tf-btn:disabled,
.tf-more:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.review-exit-btn {
  color: #b45309;
  border-color: rgba(180, 83, 9, 0.3);
  background: rgba(249, 168, 37, 0.1);
}
.tf-more {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border: none;
  border-radius: 8px;
  background: #f1f5f9;
  color: #64748b;
  cursor: pointer;
  transition:
    background 0.15s,
    color 0.15s;
}
.tf-more:hover {
  background: #e8edf3;
  color: #1f2329;
}
.tf-settings {
  width: 240px;
  padding: 12px 14px;
}
.tf-settings-title {
  font-size: 14px;
  font-weight: 700;
  color: #1f2329;
  padding: 0 2px 8px;
}
.tf-settings-list {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 2px 8px;
}
.tf-check {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 6px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  color: #3d4757;
}
.tf-check:hover {
  background: #f6f8fa;
}
.tf-settings-hint {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid #eef1f5;
  font-size: 12px;
  color: #94a3b8;
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
/* 拖拽插入线：提示将要插入到该行之前（或列表末尾） */
.sl-row.insert-before {
  box-shadow: inset 0 2px 0 #1677ff;
}
.sl-list.insert-at-end::after {
  content: '';
  display: block;
  height: 2px;
  margin: 0 14px;
  background: #1677ff;
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
  --sl-font: 12px;
  --sl-font-weight: 600;
  --sl-gap: 4px;
  --sl-pad-y: 3px;
  --sl-pad-x: 8px;
  --sl-dot: 6px;
  --sl-opacity: 1;
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: var(--sl-gap);
  font-size: var(--sl-font);
  font-weight: var(--sl-font-weight);
  line-height: 1;
  padding: var(--sl-pad-y) var(--sl-pad-x);
  border-radius: 999px;
  white-space: nowrap;
  opacity: var(--sl-opacity);
}
.sl-sig::before {
  content: '';
  width: var(--sl-dot);
  height: var(--sl-dot);
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
.sl-sig.is-score-0 {
  --sl-font: 6.5px;
  --sl-font-weight: 500;
  --sl-gap: 2.5px;
  --sl-pad-y: 2px;
  --sl-pad-x: 5px;
  --sl-dot: 3px;
  --sl-opacity: 0.5;
}
.sl-sig.is-score-1 {
  --sl-font: 7.8px;
  --sl-font-weight: 550;
  --sl-gap: 3px;
  --sl-pad-y: 2.4px;
  --sl-pad-x: 6px;
  --sl-dot: 3.6px;
  --sl-opacity: 0.6;
}
.sl-sig.is-score-2 {
  --sl-font: 9.1px;
  --sl-font-weight: 600;
  --sl-gap: 3.5px;
  --sl-pad-y: 2.8px;
  --sl-pad-x: 7px;
  --sl-dot: 4.2px;
  --sl-opacity: 0.7;
}
.sl-sig.is-score-3 {
  --sl-font: 10.4px;
  --sl-font-weight: 650;
  --sl-gap: 4px;
  --sl-pad-y: 3.2px;
  --sl-pad-x: 8px;
  --sl-dot: 4.8px;
  --sl-opacity: 0.8;
}
.sl-sig.is-score-4 {
  --sl-font: 11.7px;
  --sl-font-weight: 700;
  --sl-gap: 4.5px;
  --sl-pad-y: 3.6px;
  --sl-pad-x: 9px;
  --sl-dot: 5.4px;
  --sl-opacity: 0.9;
}
.sl-sig.is-score-5 {
  --sl-font: 13px;
  --sl-font-weight: 600;
  --sl-gap: 5px;
  --sl-pad-y: 4px;
  --sl-pad-x: 10px;
  --sl-dot: 6px;
  --sl-opacity: 1;
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
.recent-title {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid #f1f5f9;
}
.review-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-right: 4px;
}
.review-row {
  position: relative;
  border: 1px solid #eef0f3;
  border-left-width: 4px;
  border-radius: 8px;
  padding: 8px 10px;
  background: #fff;
  cursor: pointer;
  transition:
    box-shadow 0.15s,
    border-color 0.15s,
    background 0.15s;
}
.review-row:hover {
  box-shadow: 0 2px 8px rgba(15, 23, 42, 0.1);
}
.review-row.is-up {
  border-left-color: #e03131;
}
.review-row.is-down {
  border-left-color: #0f9d58;
}
.review-row.is-active {
  border-color: #bfdbfe;
  background: #f5faff;
  box-shadow: 0 2px 8px rgba(22, 119, 255, 0.14);
}
.review-row.is-hidden {
  opacity: 0.45;
}
.rv-eye {
  display: inline-flex;
  align-items: center;
  margin-left: 6px;
}
.rv-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 5px 7px;
}
.rv-id {
  font-size: 13px;
  font-weight: 800;
  color: #1f2329;
  font-variant-numeric: tabular-nums;
}
.rv-symbol {
  font-size: 12px;
  font-weight: 700;
  color: #475569;
}
.rv-badge {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 7px;
  border-radius: 999px;
  background: #f1f5f9;
  color: #64748b;
}
.review-row.is-up .rv-badge {
  color: #e03131;
  background: rgba(224, 49, 49, 0.08);
}
.review-row.is-down .rv-badge {
  color: #0f9d58;
  background: rgba(15, 157, 88, 0.08);
}
.rv-grade {
  font-size: 11px;
  font-weight: 600;
  color: #7c5cff;
  background: rgba(124, 92, 255, 0.08);
  padding: 2px 7px;
  border-radius: 999px;
}
.rv-warning {
  font-size: 10px;
  font-weight: 700;
  color: #c2410c;
  background: rgba(249, 115, 22, 0.12);
  padding: 2px 7px;
  border-radius: 999px;
}
.rv-score {
  margin-left: auto;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  line-height: 1.1;
}
.rv-score b {
  font-size: 14px;
  font-weight: 800;
  color: #1f2329;
  font-variant-numeric: tabular-nums;
}
.rv-score em {
  font-style: normal;
  font-size: 9px;
  color: #94a3b8;
}
.rv-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
  font-size: 11px;
  color: #64748b;
}
.rv-time {
  font-variant-numeric: tabular-nums;
  color: #94a3b8;
}
.rv-outcome {
  font-weight: 700;
  padding: 2px 7px;
  border-radius: 999px;
}
.rv-outcome.win {
  color: #e03131;
  background: rgba(224, 49, 49, 0.1);
}
.rv-outcome.loss {
  color: #0f9d58;
  background: rgba(15, 157, 88, 0.1);
}
.rv-outcome.warn {
  color: #b45309;
  background: rgba(249, 168, 37, 0.16);
}
.rv-outcome.plain {
  color: #64748b;
  background: #f1f5f9;
}
.rv-exit {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #64748b;
}
.rv-user {
  display: block;
  margin-top: 6px;
  font-size: 11px;
  min-width: 0;
}

.rv-ann-header {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.rv-opened {
  flex: 0 0 auto;
  color: #94a3b8;
  padding: 2px 7px;
  border-radius: 999px;
  background: #f1f5f9;
}

.rv-opened.is-open {
  color: #e03131;
  background: rgba(224, 49, 49, 0.1);
  font-weight: 700;
}

.rv-opened.is-skip {
  color: #64748b;
}

.rv-ann-count {
  flex: 0 0 auto;
  color: #64748b;
  font-size: 12px;
}

.rv-ann {
  min-width: 0;
  margin-top: 6px;
  padding: 0;
  color: #64748b;
  font-size: 12px;
  line-height: 1.5;
  white-space: normal;
  word-break: break-word;
  overflow: visible;
}

.rv-ann-item {
  display: block;
  margin: 0 0 4px 0;
  padding: 0;
}

.rv-ann-item:last-child {
  margin-bottom: 0;
}

.rv-ann-index {
  margin-right: 4px;
  color: #94a3b8;
}
.rv-ann-time {
  flex: 0 0 auto;
  margin-left: 8px;
  color: #94a3b8;
  font-size: 11px;
}
.rv-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 3px 10px;
  margin-top: 7px;
  padding-top: 7px;
  border-top: 1px dashed #eef0f3;
}
.rv-section {
  grid-column: 1 / -1;
  margin-top: 4px;
  padding-top: 5px;
  border-top: 1px solid #f1f5f9;
  font-size: 10px;
  font-weight: 800;
  color: #7c5cff;
  letter-spacing: 0;
}
.rv-item {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 6px;
  min-width: 0;
  font-size: 11px;
}
.rv-item span {
  flex: 0 0 auto;
  color: #94a3b8;
}
.rv-item b {
  min-width: 0;
  text-align: right;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 700;
  color: #1f2329;
  font-variant-numeric: tabular-nums;
}
.rv-item b.is-pos {
  color: #e03131;
}
.rv-item b.is-neg {
  color: #0f9d58;
}
.rv-item b.is-good {
  color: #0f766e;
}
.rv-item b.is-mid {
  color: #2563eb;
}
.rv-item b.is-weak {
  color: #94a3b8;
}
.rv-item b.is-neutral {
  color: #94a3b8;
}
.patterns-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding-right: 4px;
}
.recent-list {
  gap: 8px;
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
.pattern-card.is-recent {
  cursor: pointer;
}
.pattern-card.is-recent:hover {
  box-shadow: 0 3px 12px rgba(15, 23, 42, 0.12);
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
.pc-trend { font-size: 11px; padding: 2px 6px; border-radius: 999px; font-weight: 600; border: 1px solid transparent; }
.pc-trend.trend-strong-up { background:#fee2e2; color:#dc2626; border-color:#fecaca; }
.pc-trend.trend-weak-up { background:#ffedd5; color:#ea580c; border-color:#fed7aa; }
.pc-trend.trend-range { background:#f1f5f9; color:#64748b; border-color:#e2e8f0; }
.pc-trend.trend-weak-down { background:#e0f2fe; color:#0284c7; border-color:#bae6fd; }
.pc-trend.trend-strong-down { background:#dbeafe; color:#1d4ed8; border-color:#bfdbfe; }
.pc-trend em { font-style:normal; margin-left:2px; font-weight:700; }
.pc-warning {
  font-size: 10px;
  font-weight: 700;
  color: #c2410c;
  background: rgba(249, 115, 22, 0.12);
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
.pc-state-time {
  margin-left: auto;
  font-size: 11px;
  font-weight: 500;
  color: #94a3b8;
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
.pc-vol {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-top: 8px;
  font-size: 12px;
  color: #64748b;
}
.pc-vol span {
  color: #94a3b8;
}
.pc-vol b {
  font-weight: 700;
  color: #1f2329;
  font-variant-numeric: tabular-nums;
}
.pc-vol em {
  margin-left: auto;
  font-style: normal;
  font-weight: 600;
}
.pc-vol.confirmed em {
  color: #e03131;
}
.pc-vol.plain em {
  color: #64748b;
}
.pc-vol.pending em {
  color: #b45309;
}
.pc-vol.missing em {
  color: #94a3b8;
}
.pc-overshoot {
  margin-top: 4px;
}
.pc-vol.done em {
  color: #1677ff;
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

.rv-ann {
  color: #334155;
  font-size: 12px;
  line-height: 1.5;
  white-space: normal;
  word-break: break-word;
}

.rv-ann-item {
  margin-bottom: 6px;
}

.rv-ann-item:last-child {
  margin-bottom: 0;
}

.rv-ann-index {
  margin-right: 4px;
  color: #94a3b8;
}
</style>









