<script setup lang="ts">
import { computed, h, nextTick, onActivated, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import Sortable from 'sortablejs'
import {
  NButton,
  NDataTable,
  NIcon,
  NInput,
  NInputGroup,
  NModal,
  NSpace,
  NSwitch,
  NTab,
  NTabs,
  NText,
  type DataTableColumns,
} from 'naive-ui'
import {
  DeviceFloppy,
  FolderPlus,
  GripVertical,
  Lock,
  Plus,
  Refresh,
  Scan,
  Settings,
  Tag,
  Trash,
} from '@vicons/tabler'
import { api, onDataUpdated, onQuotesUpdated, onScanCompleted } from '../services/api'
import { useGroupsStore } from '../stores/groups'
import { useSettingsStore } from '../stores/settings'
import { useSymbolsStore } from '../stores/symbols'
import { useScansStore } from '../stores/scans'
import { confirmAction } from '../utils/confirm'
import { notify } from '../utils/notify'
import { openSymbolContextMenu } from '../utils/symbolMenu'
import type { GroupRow, MarketSnapshot, SignalOutcome, SymbolRow } from '../types'

// 显式声明组件名：配合 AppLayout 里的 keep-alive include 缓存本页面
defineOptions({ name: 'DashboardView' })

const router = useRouter()
const symbolsStore = useSymbolsStore()
const scansStore = useScansStore()
const settingsStore = useSettingsStore()
const groupsStore = useGroupsStore()

interface WatchRow {
  symbol: SymbolRow
  latest: number | null
  changePct: number | null
  changePts: number | null
  signal: SignalOutcome | null
  /** 最近一次行情跳动的方向：up=上涨(红) / down=下跌(绿)，用于行呼吸闪烁 */
  flash: 'up' | 'down' | null
}

const rows = ref<WatchRow[]>([])
const loading = ref(false)
const refreshing = ref(false)
const scanning = ref(false)
const enriching = ref(false)
const newCode = ref('')
const adding = ref(false)
const groupModal = ref<'create' | 'manage' | null>(null)
const newGroupName = ref('')
const groupNameDrafts = ref<Record<number, string>>({})
/** 管理分组弹窗里的分组列表容器（Sortable 挂载到它上面） */
const groupListEl = ref<HTMLElement | null>(null)
let groupSortable: Sortable | null = null
/** 拖拽中：显示插入线提示 */
const groupDragging = ref(false)
/** 插入线：将要插入到该行（'all'=全部品种 / 分组 id）之前；null 表示插入到列表末尾 */
const insertBeforeKey = ref<string | null>(null)
/** 标签页/管理列表的统一顺序：「全部品种」插在 allPosition 处，与真实分组一起排序 */
const groupTabs = computed<Array<{ kind: 'all' } | { kind: 'group'; g: GroupRow }>>(() => {
  const groups = groupsStore.groups
  const pos = Math.min(Math.max(groupsStore.allPosition, 0), groups.length)
  const items: Array<{ kind: 'all' } | { kind: 'group'; g: GroupRow }> = []
  for (let i = 0; i < groups.length; i++) {
    if (i === pos) items.push({ kind: 'all' })
    items.push({ kind: 'group', g: groups[i] })
  }
  if (pos >= groups.length) items.push({ kind: 'all' })
  return items
})
/** 分组头像取色板：按 id 稳定取色 */
const GROUP_COLORS = ['#1677ff', '#0f9d58', '#f5a623', '#7c5cff', '#e03131', '#0ca5e9', '#f272b0', '#18a058']
function groupColor(g: GroupRow) {
  return GROUP_COLORS[g.id % GROUP_COLORS.length]
}
/** 表格外层容器（Sortable 挂载到其内部 tbody 上） */
const tableWrapEl = ref<HTMLElement | null>(null)
let tableSortable: Sortable | null = null
/** 拖拽中：暂停行情/数据刷新，避免表格重渲染打断正在进行的拖拽 */
const listDragging = ref(false)
/** 插入线：将要插入到该代码行之前；null 表示插入到表格末尾 */
const insertBeforeCode = ref<string | null>(null)
/** 拖拽结束后短暂抑制双击跳转，避免松手时误开K线图 */
let suppressOpenChart = false
let unlisteners: (() => void)[] = []
/** 行闪烁清除计时器：动画结束后把 flash 归零，保证下一次跳动能重新触发动画 */
const flashTimers = new Map<string, ReturnType<typeof setTimeout>>()

/** 信号状态优先级：即将触发 > 当前已触发 > 接近时效边界 > 过时 > 失效/异常 */
function signalStateRank(state: string): number {
  if (state === '即将触发') return 0
  if (state === '当前已触发') return 1
  if (state === '已触发，接近时效边界') return 2
  if (state === '已过时，仅复盘') return 3
  return 4
}

/** 每个品种取优先级最高的形态：先按状态（即将触发 > 已触发 > 接近时效），同状态按评分从高到低 */
function bestSignal(signals: SignalOutcome[]): SignalOutcome | null {
  if (!signals.length) return null
  return [...signals].sort((a, b) => {
    const rankA = signalStateRank(a.state)
    const rankB = signalStateRank(b.state)
    if (rankA !== rankB) return rankA - rankB
    if (b.score !== a.score) return b.score - a.score
    return a.number - b.number
  })[0]
}

async function loadAll() {
  loading.value = true
  try {
    await symbolsStore.load()
    // 分组视图只加载该组内的品种；全部视图加载全部品种
    let symbols = symbolsStore.symbols
    if (groupsStore.selectedId != null) {
      symbols = await api.getGroupSymbols(groupsStore.selectedId)
    }
    // 与K线页共用同一份信号数据（scansStore.latestSignals），避免两处各自拉取不同步
    await scansStore.refreshLatestSignals()
    const signals = scansStore.latestSignals
    const bySymbol = new Map<string, SignalOutcome[]>()
    for (const s of signals) {
      const arr = bySymbol.get(s.symbol) || []
      arr.push(s)
      bySymbol.set(s.symbol, arr)
    }
    // 一次快照拿全部品种的最新价/涨跌幅，替代逐个品种拉整段K线
    const snapshots = await api.getMarketSnapshot()
    const byCode = new Map(snapshots.map((s) => [s.code, s]))
    const list = symbols.map((sym) => {
      const snap = byCode.get(sym.code)
      const latest = snap?.latest ?? null
      const changePct = snap?.change_pct ?? null
      // 涨跌点数由最新价与涨跌幅反推，保证与涨跌幅口径一致
      const changePts =
        latest != null && changePct != null ? (latest * changePct) / (100 + changePct) : null
      return {
        symbol: sym,
        latest,
        changePct,
        changePts,
        signal: bestSignal(bySymbol.get(sym.code) || []),
        flash: null,
      }
    })
    rows.value = list
  } finally {
    loading.value = false
  }
}

/** 实时现价事件：只更新行情列（最新价/涨跌幅/涨跌点数），不重拉数据库 */
function applyQuotes(snapshots: MarketSnapshot[]) {
  if (!snapshots.length || !rows.value.length || listDragging.value) return
  const byCode = new Map(snapshots.map((s) => [s.code, s]))
  rows.value = rows.value.map((r) => {
    const snap = byCode.get(r.symbol.code)
    if (!snap || snap.latest == null) return r
    const oldLatest = r.latest
    const changePts =
      snap.latest != null && snap.change_pct != null
        ? (snap.latest * snap.change_pct) / (100 + snap.change_pct)
        : null
    // 只有价格实际跳动才闪烁：上涨红、下跌绿；首笔报价（无旧值）不闪
    const flash =
      oldLatest != null && snap.latest !== oldLatest
        ? snap.latest > oldLatest
          ? 'up'
          : 'down'
        : r.flash
    if (flash && flash !== r.flash) {
      const prev = flashTimers.get(r.symbol.code)
      if (prev) clearTimeout(prev)
      flashTimers.set(
        r.symbol.code,
        setTimeout(() => {
          if (listDragging.value) return
          rows.value = rows.value.map((x) =>
            x.symbol.code === r.symbol.code && x.flash === flash ? { ...x, flash: null } : x,
          )
          flashTimers.delete(r.symbol.code)
        }, settingsStore.settings.ui.flash_ms),
      )
    }
    return { ...r, latest: snap.latest, changePct: snap.change_pct, changePts, flash }
  })
}

function openChart(row: WatchRow) {
  router.push({ name: 'chart', params: { symbol: row.symbol.code } })
}

function rowProps(row: WatchRow): Record<string, unknown> {
  return {
    style: `cursor: ${listDragging.value ? 'grabbing' : 'grab'}`,
    class: [
      row.flash ? (row.flash === 'up' ? 'row-flash-up' : 'row-flash-down') : '',
      { 'insert-before': listDragging.value && insertBeforeCode.value === row.symbol.code },
    ],
    'data-code': row.symbol.code,
    onDblclick: () => {
      if (suppressOpenChart) return
      openChart(row)
    },
    onContextmenu: (e: MouseEvent) => onRowContextMenu(row, e),
  }
}

/**
 * Sortable 直接挂载到表格 tbody：行顺序变化后读取 DOM 新顺序落库并广播。
 * 分组视图写组内顺序，全部品种视图写全局顺序，与K线页左侧列表一致。
 */
function setupTableSortable() {
  const tbody = tableWrapEl.value?.querySelector<HTMLElement>('.watch-table tbody')
  if (!tbody) return
  // 数据重载导致 tbody 被重建时，先销毁旧实例再挂到新 tbody 上
  if (tableSortable?.el === tbody) return
  tableSortable?.destroy()
  tableSortable = Sortable.create(tbody, {
    animation: 150,
    draggable: 'tr[data-code]',
    // 排除启用开关等交互控件：点击开关正常切换，不触发拖拽
    filter: '.n-switch, input, button, textarea, select, a',
    preventOnFilter: false,
    forceFallback: true,
    fallbackClass: 'sl-table-fallback',
    fallbackOnBody: true,
    ghostClass: 'sl-table-ghost',
    chosenClass: 'sl-table-chosen',
    onStart: () => {
      listDragging.value = true
      insertBeforeCode.value = null
    },
    onMove: onTableMove,
    onEnd: () => persistTableOrder(),
  })
}

/**
 * 拖拽移动判定：与K线页左侧列表同一套「光标在目标行上半 → 插到该行之前；
 * 下半 → 插到该行之后」规则，蓝线提示与实际落点完全一致。
 */
function onTableMove(evt: {
  related: HTMLElement | null
  relatedRect: { top: number; bottom: number; height: number } | null
  willInsertAfter?: boolean
  originalEvent?: Event | null
}): boolean | 1 | -1 {
  const related = evt.related as HTMLElement | null
  const isRow = !!related?.closest?.('tr[data-code]')
  if (!isRow || !related) {
    // 目标是表格体本身（末尾空区域）：按默认逻辑插到末尾
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
  // 插入 related 之后时，边界是 related 的下一行；插入之前时，边界就是 related
  const boundary = after ? related.nextElementSibling : related
  insertBeforeCode.value =
    (boundary as HTMLElement | null)?.getAttribute('data-code') ?? null
  return after ? 1 : -1
}

async function persistTableOrder() {
  listDragging.value = false
  insertBeforeCode.value = null
  // 松手后短暂抑制双击跳转，避免误开K线图
  suppressOpenChart = true
  setTimeout(() => {
    suppressOpenChart = false
  }, 200)
  const tbody = tableWrapEl.value?.querySelector<HTMLElement>('.watch-table tbody')
  if (!tbody) return
  const codes = [...tbody.querySelectorAll<HTMLElement>('tr[data-code]')]
    .map((tr) => tr.dataset.code)
    .filter((c): c is string => c != null)
  const currentCodes = rows.value.map((r) => r.symbol.code)
  // 关键：Sortable 已直接移动了 DOM，Vue 并不知道。先把 tbody 恢复成 Vue 当前
  // 渲染的顺序，随后 Vue 再基于新的数据顺序自己完成重排；否则 Vue 的 keyed diff
  // 会拿“被手动移动过的 DOM”对比，产生重复/丢失的行。顺带做防御性去重。
  const trsByCode = new Map<string, HTMLElement>()
  for (const tr of [...tbody.querySelectorAll<HTMLElement>('tr[data-code]')]) {
    const code = tr.dataset.code ?? ''
    if (trsByCode.has(code)) {
      tr.remove()
    } else {
      trsByCode.set(code, tr)
    }
  }
  for (const code of currentCodes) {
    const tr = trsByCode.get(code)
    if (tr) tbody.appendChild(tr)
  }
  // 防御：codes 必须是当前行的完整排列（无重复、无缺失），否则回滚为服务端顺序
  if (!codes.length || codes.length !== rows.value.length) {
    await loadAll()
    return
  }
  const uniqueCodes = new Set(codes)
  if (!currentCodes.every((c) => uniqueCodes.has(c))) {
    await loadAll()
    return
  }
  // 让 Vue 数据与 DOM 新顺序保持一致
  const byCode = new Map(rows.value.map((r) => [r.symbol.code, r]))
  const next = codes.map((c) => byCode.get(c)).filter((r): r is WatchRow => r != null)
  if (next.length !== rows.value.length) return
  rows.value = next
  const groupId = groupsStore.selectedId
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
    await loadAll() // 落库失败则回滚为服务端顺序
  }
}

/** 行右键菜单：分组操作 + 彻底删除品种 */
async function onRowContextMenu(row: WatchRow, e: MouseEvent) {
  // 先同步阻止浏览器默认菜单，再异步查分组归属，避免原生菜单闪现
  e.preventDefault()
  let memberGroups: GroupRow[] = []
  try {
    memberGroups = await api.listSymbolGroups(row.symbol.code)
  } catch {
    // 查询失败不影响菜单弹出，仅少了对勾标识
  }
  openSymbolContextMenu(e, {
    groups: groupsStore.groups,
    selectedGroupId: groupsStore.selectedId,
    symbol: row.symbol.code,
    memberGroupIds: new Set(memberGroups.map((g) => g.id)),
    onRemoveFromGroup: () => handleRemoveFromGroup(row),
    onCopyToGroup: (g) => handleCopyToGroup(row, g),
    onMoveToGroup: (g) => handleMoveToGroup(row, g),
    onDeleteSymbol: () => handleDelete(row),
  })
}

/** 删除品种：先弹确认框，确认后再删除并刷新表格 */
async function handleDelete(row: WatchRow) {
  const ok = await confirmAction({
    title: '删除品种',
    content: `确定删除 ${row.symbol.code} 吗？将同时删除其K线数据。`,
    positiveText: '删除',
    negativeText: '取消',
    type: 'warning',
  })
  if (!ok) return
  try {
    await symbolsStore.remove(row.symbol.code)
    notify.success(`${row.symbol.code} 已删除`)
    await loadAll()
  } catch (err) {
    notify.error(String(err))
  }
}

async function handleRemoveFromGroup(row: WatchRow) {
  const groupId = groupsStore.selectedId
  if (groupId == null) return
  try {
    await api.removeSymbolFromGroup(row.symbol.code, groupId)
    notify.success(`${row.symbol.code} 已从该组删除`)
    await loadAll()
  } catch (err) {
    notify.error(String(err))
  }
}

async function handleCopyToGroup(row: WatchRow, group: GroupRow) {
  try {
    await api.addSymbolToGroup(row.symbol.code, group.id)
    notify.success(`${row.symbol.code} 已复制到「${group.name}」`)
  } catch (err) {
    notify.error(String(err))
  }
}

async function handleMoveToGroup(row: WatchRow, group: GroupRow) {
  const fromId = groupsStore.selectedId
  if (fromId == null) return
  try {
    // 先加入目标组，再从原组移除：即使第二步失败，品种也不会丢失
    await api.addSymbolToGroup(row.symbol.code, group.id)
    await api.removeSymbolFromGroup(row.symbol.code, fromId)
    notify.success(`${row.symbol.code} 已移动到「${group.name}」`)
    await loadAll()
  } catch (err) {
    notify.error(String(err))
  }
}

async function onSelectGroup(name: string) {
  await groupsStore.select(name === 'all' ? null : Number(name))
  await loadAll()
}

function openCreateGroup() {
  newGroupName.value = ''
  groupModal.value = 'create'
}

async function doCreateGroup() {
  const name = newGroupName.value.trim()
  if (!name) return
  try {
    await groupsStore.create(name)
    notify.success(`已创建分组「${name}」`)
    groupModal.value = null
    await loadAll()
  } catch (err) {
    notify.error(String(err))
  }
}

function openManageGroup() {
  groupNameDrafts.value = Object.fromEntries(
    groupsStore.groups.map((g) => [g.id, g.name]),
  ) as Record<number, string>
  groupModal.value = 'manage'
  // 等弹窗内容渲染完成后，再给分组列表挂载拖拽排序
  nextTick(() => setupGroupSortable())
}

async function doRenameGroup(g: GroupRow) {
  const name = (groupNameDrafts.value[g.id] ?? '').trim()
  // 以 store 里的最新名称为准，避免失焦保存与点击保存图标重复提交
  const current = groupsStore.groups.find((x) => x.id === g.id)?.name ?? g.name
  if (!name || name === current) {
    // 未修改或名称为空：还原为当前名称，避免输入框显示空白
    groupNameDrafts.value[g.id] = current
    return
  }
  try {
    await groupsStore.rename(g.id, name)
    notify.success(`已重命名为「${name}」`)
  } catch (err) {
    notify.error(String(err))
    groupNameDrafts.value[g.id] = current
  }
}

async function doDeleteGroup(g: GroupRow) {
  const ok = await confirmAction({
    title: '删除分组',
    content: `确定删除分组「${g.name}」吗？分组内的品种不会被删除。`,
    positiveText: '删除',
    negativeText: '取消',
    type: 'warning',
  })
  if (!ok) return
  try {
    await groupsStore.remove(g.id)
    notify.success(`已删除分组「${g.name}」`)
    await loadAll()
  } catch (err) {
    notify.error(String(err))
  }
}

/**
 * Sortable 挂载到管理弹窗的分组列表：拖动手柄重排分组。
 * 顺序变化后先本地更新（标签页立即跟随），再落库并广播。
 */
function setupGroupSortable() {
  const el = groupListEl.value
  if (!el) return
  if (groupSortable?.el === el) return
  groupSortable?.destroy()
  groupSortable = Sortable.create(el, {
    animation: 150,
    draggable: '.group-manage-row',
    // 只允许拖动手柄触发拖拽，输入框/按钮不受影响
    handle: '.group-drag-handle',
    filter: 'input, button, textarea, select',
    preventOnFilter: false,
    forceFallback: true,
    fallbackClass: 'group-row-fallback',
    fallbackOnBody: true,
    ghostClass: 'group-row-ghost',
    chosenClass: 'group-row-chosen',
    onStart: () => {
      groupDragging.value = true
      insertBeforeKey.value = null
    },
    onMove: onGroupMove,
    onEnd: () => persistGroupOrder(),
  })
}

/** 拖拽移动判定：与品种列表/表格同一套「上半插前、下半插后」规则 */
function onGroupMove(evt: {
  related: HTMLElement | null
  relatedRect: { top: number; bottom: number; height: number } | null
  willInsertAfter?: boolean
  originalEvent?: Event | null
}): boolean | 1 | -1 {
  const related = evt.related as HTMLElement | null
  const isRow = !!related?.closest?.('.group-manage-row')
  if (!isRow || !related) {
    // 目标是列表容器本身（末尾空区域）：按默认逻辑插到末尾
    const boundary = evt.willInsertAfter ? related?.nextElementSibling : related
    insertBeforeKey.value =
      (boundary as HTMLElement | null)?.getAttribute('data-key') ?? null
    return true
  }
  const rect =
    evt.relatedRect ??
    (related.getBoundingClientRect() as { top: number; bottom: number; height: number })
  const mouseY = (evt.originalEvent as MouseEvent | null)?.clientY ?? rect.top + rect.height / 2
  const after = mouseY > rect.top + rect.height / 2
  // 插入 related 之后时，边界是 related 的下一行；插入之前时，边界就是 related
  const boundary = after ? related.nextElementSibling : related
  insertBeforeKey.value =
    (boundary as HTMLElement | null)?.getAttribute('data-key') ?? null
  return after ? 1 : -1
}

async function persistGroupOrder() {
  groupDragging.value = false
  insertBeforeKey.value = null
  const el = groupListEl.value
  if (!el) return
  // 读取拖拽后的 DOM 新顺序：'all' 代表「全部品种」虚拟行，其余为分组 id
  const order: Array<number | 'all'> = []
  for (const row of el.querySelectorAll<HTMLElement>('.group-manage-row')) {
    const key = row.getAttribute('data-key')
    if (key === 'all') {
      order.push('all')
    } else if (key != null && /^\d+$/.test(key)) {
      order.push(Number(key))
    }
  }
  // 关键：Sortable 已直接移动了 DOM，Vue 并不知道。先把行恢复成 Vue 当前渲染的顺序
  // （groupTabs 即 store 顺序），随后 Vue 再基于新的 store 顺序自己完成重排；
  // 否则 Vue 的 keyed diff 会拿“被手动移动过的 DOM”对比，产生重复/丢失的行。
  // 顺带做防御性去重，清理历史脏状态。
  const rowsByKey = new Map<string, HTMLElement>()
  for (const row of [...el.querySelectorAll<HTMLElement>('.group-manage-row')]) {
    const key = row.getAttribute('data-key') ?? ''
    if (rowsByKey.has(key)) {
      row.remove()
    } else {
      rowsByKey.set(key, row)
    }
  }
  const expectedKeys = groupTabs.value.map((t) =>
    t.kind === 'all' ? 'all' : String(t.g.id),
  )
  for (const key of expectedKeys) {
    const row = rowsByKey.get(key)
    if (row) el.appendChild(row)
  }
  for (const row of [...el.querySelectorAll<HTMLElement>('.group-manage-row')]) {
    if (!expectedKeys.includes(row.getAttribute('data-key') ?? '')) {
      row.remove()
    }
  }
  if (order.length !== groupsStore.groups.length + 1) return
  const allPosition = order.indexOf('all')
  if (allPosition < 0) return
  const ids = order.filter((x): x is number => x !== 'all')
  if (!ids.length) return
  try {
    await groupsStore.reorder(ids, allPosition)
  } catch (err) {
    notify.error(String(err))
  }
}

const fmt = (v: number | null | undefined, digits = 1) =>
  v == null ? '—' : v.toFixed(digits)

const trendColor = (v: number | null) =>
  v == null || v === 0 ? '#94a3b8' : v > 0 ? '#e03131' : '#0f9d58'

/** 涨跌点数：按量级自适应小数位（几百点 1 位、几点 2 位、零点几 3 位） */
const fmtPoints = (v: number | null | undefined) => {
  if (v == null) return '—'
  const a = Math.abs(v)
  const digits = a >= 100 ? 1 : a >= 1 ? 2 : 3
  return `${v >= 0 ? '+' : ''}${v.toFixed(digits)}`
}

function dirLabel(s: SignalOutcome) {
  return s.direction === 'up' ? '做多' : s.direction === 'down' ? '做空' : s.direction
}

function levelLabel(s: SignalOutcome) {
  return s.level === 'fine' ? '精细' : s.level === 'large' ? '较大' : s.level
}

/** 状态胶囊的短标签（与 K 线图左侧品种列表一致） */
function stateLabel(state: string) {
  switch (state) {
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

/** 状态胶囊配色：与 K 线图左侧品种列表一致 */
function stateCls(state: string): string {
  if (state === '即将触发') return 'pending'
  if (state === '当前已触发') return 'triggered'
  if (state === '已触发，接近时效边界') return 'stale'
  if (state === '已过时，仅复盘') return 'expired'
  if (state === '结构失效' || state === '空间异常') return 'error'
  return 'muted'
}

const columns: DataTableColumns<WatchRow> = [
  {
    title: '代码',
    key: 'code',
    width: 84,
    render: (r) => h('span', { class: 'cell-code' }, r.symbol.code),
  },
  {
    title: '名称',
    key: 'name',
    width: 170,
    ellipsis: { tooltip: true },
    render: (r) =>
      h(
        'span',
        { class: 'cell-name' },
        r.symbol.name && r.symbol.name !== r.symbol.code ? r.symbol.name : '—',
      ),
  },
  {
    title: '最新价',
    key: 'latest',
    width: 92,
    align: 'right',
    render: (r) =>
      h(
        'span',
        { class: 'cell-price', style: { color: trendColor(r.changePct) } },
        fmt(r.latest, 1),
      ),
  },
  {
    title: '涨跌幅',
    key: 'change',
    width: 92,
    align: 'right',
    render: (r) =>
      r.changePct == null
        ? h('span', { class: 'cell-empty' }, '—')
        : h(
            'span',
            { class: 'cell-pct', style: { color: trendColor(r.changePct) } },
            `${r.changePct >= 0 ? '+' : ''}${r.changePct.toFixed(2)}%`,
          ),
  },
  {
    title: '涨跌点数',
    key: 'change_pts',
    width: 92,
    align: 'right',
    render: (r) =>
      r.changePts == null
        ? h('span', { class: 'cell-empty' }, '—')
        : h(
            'span',
            { class: 'cell-pts', style: { color: trendColor(r.changePct) } },
            fmtPoints(r.changePts),
          ),
  },
  {
    title: '形态',
    key: 'pattern',
    width: 132,
    maxWidth: 150,
    render: (r) => {
      const s = r.signal
      if (!s) return h('span', { class: 'cell-empty' }, '—')
      const dir = dirLabel(s)
      const tip = `${dir} ${levelLabel(s)}N · ${s.grade} · 评分 ${s.score.toFixed(2)}`
      return h(
        'div',
        { class: 'pattern-pills', title: tip },
        [
          h(
            'span',
            { class: `pill pill-dir ${s.direction === 'up' ? 'is-up' : 'is-down'}` },
            `${dir} ${levelLabel(s)}N`,
          ),
          h('span', { class: 'pill pill-grade' }, s.grade),
        ],
      )
    },
  },
  {
    title: '状态',
    key: 'state',
    width: 104,
    render: (r) => {
      const sig = r.signal
      return sig
        ? h(
            'span',
            { class: `state-pill is-${stateCls(sig.state)}`, title: sig.state },
            [h('span', { class: 'state-dot' }), stateLabel(sig.state)],
          )
        : h('span', { class: 'cell-empty' }, '—')
    },
  },
  {
    title: '入场价',
    key: 'entry',
    width: 84,
    align: 'right',
    render: (r) => h('span', { class: 'cell-num' }, fmt(r.signal?.entry, 1)),
  },
  {
    title: '止损价',
    key: 'stop',
    width: 84,
    align: 'right',
    render: (r) => h('span', { class: 'cell-num' }, fmt(r.signal?.stop, 1)),
  },
  {
    title: '目标价',
    key: 'target',
    width: 84,
    align: 'right',
    render: (r) => h('span', { class: 'cell-num' }, fmt(r.signal?.target, 1)),
  },
  {
    title: '评分',
    key: 'score',
    width: 72,
    align: 'right',
    render: (r) =>
      r.signal
        ? h('span', { class: 'cell-score' }, r.signal.score.toFixed(2))
        : h('span', { class: 'cell-empty' }, '—'),
  },
  {
    title: '启用',
    key: 'enabled',
    width: 64,
    render: (r) =>
      h(NSwitch, {
        size: 'small',
        value: r.symbol.enabled,
        onUpdateValue: (v: boolean) => symbolsStore.setFlags(r.symbol.code, r.symbol.watchlist, v),
      }),
  },
]

async function doRefresh() {
  refreshing.value = true
  try {
    const stats = await api.refreshDataNow()
    notify.success(`数据刷新完成：成功 ${stats.succeeded}，失败 ${stats.failures}`)
    try {
      await settingsStore.refreshStatus()
    } catch {
      // 顶部时间同步失败不影响本次操作
    }
    await loadAll()
  } catch (e) {
    notify.error(String(e))
  } finally {
    refreshing.value = false
  }
}

async function doEnrich() {
  enriching.value = true
  try {
    const n = await symbolsStore.enrichNames()
    notify.success(`已补齐 ${n} 个品种名称`)
    await loadAll()
  } catch (e) {
    notify.error(String(e))
  } finally {
    enriching.value = false
  }
}

async function doScan() {
  scanning.value = true
  try {
    await scansStore.runScan()
    const result = scansStore.latest
    notify.success(`扫描完成：${result?.scanned ?? 0} 个品种，${result?.active_count ?? 0} 个信号`)
    try {
      await settingsStore.refreshStatus()
    } catch {
      // 顶部时间同步失败不影响本次操作
    }
    await loadAll()
  } catch (e) {
    notify.error(String(e))
  } finally {
    scanning.value = false
  }
}

async function doAddSymbol() {
  const code = newCode.value.trim().toUpperCase()
  if (!code) return
  adding.value = true
  try {
    const count = await symbolsStore.add(code)
    notify.success(`${code} 已添加，回填 ${count} 根K线`)
    newCode.value = ''
    await loadAll()
  } catch (e) {
    notify.error(String(e))
  } finally {
    adding.value = false
  }
}

onMounted(async () => {
  try {
    await settingsStore.load()
  } catch {
    // 浏览器预览环境下无后端命令，保持默认值
  }
  await groupsStore.load()
  // 恢复上次打开的分组表格；分组已不存在时回退到“全部品种”
  const last = settingsStore.settings.ui.last_group_id
  groupsStore.selectedId =
    last != null && groupsStore.groups.some((g) => g.id === last) ? last : null
  unlisteners.push(
    await onDataUpdated(() => {
      if (listDragging.value) return
      loadAll()
    }),
  )
  unlisteners.push(await onQuotesUpdated(applyQuotes))
  // 扫描完成时同步更新内存里的最新扫描结果，避免图表页「全部N形态」停留在旧扫描
  unlisteners.push(
    await onScanCompleted((result) => {
      scansStore.ingest(result)
      if (!listDragging.value) loadAll()
    }),
  )
  await loadAll()
  await nextTick()
  setupTableSortable()
})

// 组内顺序在别处被改动（如K线页左侧拖拽）时，重拉表格
watch(() => groupsStore.revision, () => {
  if (listDragging.value) return
  loadAll()
})
// 数据从空到有、或 keep-alive 重新激活时 tbody 可能被重建，重建 Sortable
watch(
  () => rows.value.length,
  async (len) => {
    if (!len) return
    await nextTick()
    setupTableSortable()
  },
)
// 关闭管理分组弹窗时销毁拖拽实例，避免占用已卸载的 DOM
watch(groupModal, (v) => {
  if (v !== 'manage') {
    groupSortable?.destroy()
    groupSortable = null
  }
})
// keep-alive 缓存页面：从K线图返回时重拉数据，避免表格停留在旧扫描结果
onActivated(() => {
  setupTableSortable()
  loadAll()
})

onBeforeUnmount(() => {
  tableSortable?.destroy()
  tableSortable = null
  for (const timer of flashTimers.values()) clearTimeout(timer)
  flashTimers.clear()
  for (const fn of unlisteners) fn()
})
</script>

<template>
  <div class="page">
    <div class="toolbar">
      <n-space align="center" :size="8" class="toolbar-actions">
        <n-button type="primary" :loading="refreshing" @click="doRefresh">
          <template #icon>
            <n-icon :component="Refresh" />
          </template>
          刷新数据
        </n-button>
        <n-button type="success" :loading="scanning" @click="doScan">
          <template #icon>
            <n-icon :component="Scan" />
          </template>
          立即扫描
        </n-button>
        <n-button :loading="enriching" @click="doEnrich">
          <template #icon>
            <n-icon :component="Tag" />
          </template>
          刷新名称
        </n-button>
      </n-space>
      <div class="toolbar-right">
        <n-input-group>
          <n-input
            v-model:value="newCode"
            placeholder="输入品种代码，如 RB0"
            style="width: 180px"
            @keyup.enter="doAddSymbol"
          />
          <n-button type="primary" :loading="adding" @click="doAddSymbol">
            <template #icon>
              <n-icon :component="Plus" />
            </template>
            添加品种
          </n-button>
        </n-input-group>
        <n-text depth="3" style="font-size: 12px">双击行打开K线图，拖动行可排序</n-text>
      </div>
    </div>
    <div class="group-bar">
      <n-tabs
        :value="groupsStore.selectedId == null ? 'all' : String(groupsStore.selectedId)"
        type="line"
        size="small"
        class="group-tabs"
        @update:value="onSelectGroup"
      >
        <template v-for="t in groupTabs" :key="t.kind === 'all' ? 'all' : t.g.id">
          <n-tab v-if="t.kind === 'all'" name="all">全部品种</n-tab>
          <n-tab v-else :name="String(t.g.id)">{{ t.g.name }}</n-tab>
        </template>
      </n-tabs>
      <n-space align="center" :size="8">
        <n-button size="small" type="primary" ghost @click="openCreateGroup">
          <template #icon>
            <n-icon :component="FolderPlus" />
          </template>
          新建分组
        </n-button>
        <n-button
          size="small"
          :disabled="!groupsStore.groups.length"
          @click="openManageGroup"
        >
          <template #icon>
            <n-icon :component="Settings" />
          </template>
          管理分组
        </n-button>
      </n-space>
    </div>
    <div
      ref="tableWrapEl"
      class="watch-table-wrap"
      :class="{ 'insert-at-end': listDragging && insertBeforeCode === null }"
    >
      <n-data-table
        class="watch-table"
        :columns="columns"
        :data="rows"
        :loading="loading"
        :row-props="rowProps"
        :row-key="(r: WatchRow) => r.symbol.code"
        size="small"
        :bordered="false"
        flex-height
      />
    </div>

    <n-modal
      :show="groupModal === 'create'"
      preset="card"
      title="新建分组"
      style="width: 360px"
      @update:show="(v: boolean) => { if (!v) groupModal = null }"
    >
      <n-input
        v-model:value="newGroupName"
        placeholder="分组名称，如：黑色系"
        @keyup.enter="doCreateGroup"
      />
      <template #footer>
        <n-space justify="end">
          <n-button size="small" @click="groupModal = null">取消</n-button>
          <n-button
            size="small"
            type="primary"
            :disabled="!newGroupName.trim()"
            @click="doCreateGroup"
          >
            创建
          </n-button>
        </n-space>
      </template>
    </n-modal>

    <n-modal
      :show="groupModal === 'manage'"
      preset="card"
      title="管理分组"
      style="width: 480px"
      @update:show="(v: boolean) => { if (!v) groupModal = null }"
    >
      <div class="group-manage-wrap">
        <div class="group-manage-hint">
          <n-icon :component="GripVertical" />
          <n-text depth="3" style="font-size: 12px">
            拖动手柄调整分组顺序；输入名称后回车或点击保存
          </n-text>
        </div>
        <div
          ref="groupListEl"
          class="group-manage-list"
          :class="{ 'insert-at-end': groupDragging && insertBeforeKey === null }"
        >
          <template v-for="t in groupTabs" :key="t.kind === 'all' ? 'all' : t.g.id">
            <div
              v-if="t.kind === 'all'"
              class="group-manage-row group-manage-row-default"
              :data-key="'all'"
              :class="{ 'insert-before': groupDragging && insertBeforeKey === 'all' }"
            >
              <span class="group-drag-handle" title="拖动「全部品种」调整顺序">
                <n-icon :component="GripVertical" />
              </span>
              <span class="group-avatar group-avatar-all">全</span>
              <span class="group-name-static">全部品种</span>
              <span class="group-default-badge">
                <n-icon :component="Lock" />
                默认分组
              </span>
            </div>
            <div
              v-else
              class="group-manage-row"
              :data-key="String(t.g.id)"
              :class="{ 'insert-before': groupDragging && insertBeforeKey === String(t.g.id) }"
            >
              <span class="group-drag-handle" :title="`拖动「${t.g.name}」调整顺序`">
                <n-icon :component="GripVertical" />
              </span>
              <span class="group-avatar" :style="{ background: groupColor(t.g) }">
                {{ t.g.name.charAt(0) }}
              </span>
              <n-input
                v-model:value="groupNameDrafts[t.g.id]"
                size="small"
                class="group-name-input"
                :placeholder="t.g.name"
                @keyup.enter="doRenameGroup(t.g)"
                @blur="doRenameGroup(t.g)"
              />
              <n-button
                size="small"
                text
                type="primary"
                class="group-save-btn"
                title="保存名称"
                @click="doRenameGroup(t.g)"
              >
                <template #icon>
                  <n-icon :component="DeviceFloppy" />
                </template>
              </n-button>
              <n-button
                size="small"
                text
                type="error"
                class="group-del-btn"
                title="删除分组"
                @click="doDeleteGroup(t.g)"
              >
                <template #icon>
                  <n-icon :component="Trash" />
                </template>
              </n-button>
            </div>
          </template>
        </div>
        <div v-if="!groupsStore.groups.length" class="group-manage-empty">
          <n-text depth="3">暂无分组，先在上方新建一个吧</n-text>
        </div>
      </div>
      <template #footer>
        <n-space justify="end">
          <n-button size="small" type="primary" ghost @click="groupModal = null">
            完成
          </n-button>
        </n-space>
      </template>
    </n-modal>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 12px;
  height: 100%;
  min-height: 0;
}
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 8px 12px;
  border: 1px solid #eef1f5;
  border-radius: 10px;
  background: #fbfcfe;
}
.toolbar-actions {
  flex: none;
}
.toolbar-right {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}
.group-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 6px 12px;
  border: 1px solid #eef1f5;
  border-radius: 10px;
  background: #fbfcfe;
}
.group-tabs {
  flex: 1;
  min-width: 0;
}
.group-manage-wrap {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.group-manage-hint {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #94a3b8;
}
.group-manage-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 360px;
  overflow-y: auto;
  padding: 2px;
}
.group-manage-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border: 1px solid #eef1f5;
  border-radius: 8px;
  background: #fff;
  user-select: none;
  transition:
    background 0.15s,
    border-color 0.15s,
    box-shadow 0.15s;
}
.group-manage-row:hover {
  background: #f8fafc;
  border-color: #dbe4ee;
}
.group-manage-row :deep(.n-input) {
  user-select: text;
}
.group-drag-handle {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 4px;
  border-radius: 6px;
  color: #94a3b8;
  cursor: grab;
}
.group-drag-handle:hover {
  color: #1677ff;
  background: #eaf2ff;
}
.group-drag-handle:active {
  cursor: grabbing;
}
.group-avatar {
  flex: none;
  width: 28px;
  height: 28px;
  border-radius: 8px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 700;
  color: #fff;
}
.group-avatar-all {
  background: linear-gradient(135deg, #1677ff, #69b1ff);
}
.group-name-input {
  flex: 1;
  min-width: 0;
}
.group-name-static {
  flex: 1;
  min-width: 0;
  font-size: 14px;
  font-weight: 600;
  color: #1f2329;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.group-default-badge {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  font-weight: 600;
  line-height: 1;
  padding: 4px 8px;
  border-radius: 999px;
  color: #64748b;
  background: #f1f5f9;
}
.group-manage-row-default {
  background: #f8faff;
  border-color: #dbe9ff;
}
.group-save-btn,
.group-del-btn {
  flex: none;
}
.group-manage-empty {
  display: flex;
  justify-content: center;
  padding: 18px 0;
}
/* 拖拽插入线：提示将要插入到该行之前（或列表末尾） */
.group-manage-row.insert-before {
  box-shadow: inset 0 2px 0 #1677ff;
}
.group-manage-list.insert-at-end::after {
  content: '';
  display: block;
  height: 2px;
  margin: 0 8px;
  border-radius: 1px;
  background: #1677ff;
}
.group-row-ghost {
  opacity: 0.45;
  background: #eaf2ff;
}
.group-row-chosen {
  background: #dbe9ff;
}
.watch-table-wrap {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  position: relative;
}
.watch-table {
  flex: 1;
  min-height: 0;
  /* 只保留极淡的横向分隔线，去掉列间竖线 */
  --n-border-color: #eef1f5;
  --n-th-text-color: #475569;
  --n-td-text-color: #334155;
  --n-td-color-hover: #f7f9fc;
}
.watch-table :deep(.n-data-table-th) {
  background: #fafbfc;
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.3px;
}
.watch-table :deep(.n-data-table-td) {
  font-size: 14px;
}
/* 表格行拖拽：与K线页左侧列表一致的视觉反馈 */
.watch-table :deep(tbody tr) {
  user-select: none;
}
.watch-table :deep(tbody tr.sl-table-ghost td) {
  opacity: 0.45;
  background: #eaf2ff;
}
.watch-table :deep(tbody tr.sl-table-chosen td) {
  background: #dbe9ff;
}
/* 拖拽插入线：提示将要插入到该行之前（或表格末尾） */
.watch-table :deep(tbody tr.insert-before td) {
  box-shadow: inset 0 2px 0 #1677ff;
}
.watch-table-wrap.insert-at-end :deep(.watch-table tbody tr:last-child td) {
  box-shadow: inset 0 -2px 0 #1677ff;
}
</style>

<!--
  表格单元格（形态胶囊、状态胶囊等）由 naive 表格在运行时渲染，
  作用域样式（scoped）无法匹配这些动态节点，因此这里用全局样式，
  并以 .watch-table 限定作用范围避免影响其他页面。
-->
<style>
.watch-table .cell-code {
  color: #64748b;
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.4px;
}
.watch-table .cell-name {
  color: #1f2329;
  font-weight: 500;
}
.watch-table .cell-price {
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.watch-table .cell-pct {
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.watch-table .cell-pts {
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.watch-table .cell-num {
  color: #334155;
  font-variant-numeric: tabular-nums;
}
.watch-table .cell-score {
  color: #7c5cff;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.watch-table .cell-empty {
  color: #c2c9d4;
}
.watch-table .pattern-pills {
  display: flex;
  align-items: center;
  gap: 4px;
  overflow: hidden;
}
.watch-table .pill {
  flex: none;
  display: inline-flex;
  align-items: center;
  font-size: 12px;
  font-weight: 600;
  line-height: 1;
  padding: 4px 8px;
  border-radius: 999px;
  white-space: nowrap;
}
.watch-table .pill-dir.is-up {
  color: #e03131;
  background: rgba(224, 49, 49, 0.1);
}
.watch-table .pill-dir.is-down {
  color: #0f9d58;
  background: rgba(15, 157, 88, 0.1);
}
.watch-table .pill-grade {
  color: #7c5cff;
  background: rgba(124, 92, 255, 0.1);
}
.watch-table .state-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 13px;
  font-weight: 600;
  line-height: 1;
  padding: 4px 10px;
  border-radius: 999px;
  white-space: nowrap;
}
.watch-table .state-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  flex: none;
}
.watch-table .state-pill.is-pending {
  color: #1677ff;
  background: rgba(22, 119, 255, 0.12);
}
.watch-table .state-pill.is-triggered {
  color: #0f9d58;
  background: rgba(15, 157, 88, 0.12);
}
.watch-table .state-pill.is-stale {
  color: #b45309;
  background: rgba(249, 168, 37, 0.16);
}
.watch-table .state-pill.is-expired {
  color: #64748b;
  background: rgba(148, 163, 184, 0.14);
}
.watch-table .state-pill.is-error {
  color: #e03131;
  background: rgba(224, 49, 49, 0.1);
}
.watch-table .state-pill.is-muted {
  color: #64748b;
  background: rgba(148, 163, 184, 0.1);
}

/* 行情跳动时的行闪烁：上涨红、下跌绿，透明背景淡入淡出 */
.watch-table tr.row-flash-up td {
  animation: row-flash-up 0.9s ease-out;
}
.watch-table tr.row-flash-down td {
  animation: row-flash-down 0.9s ease-out;
}
@keyframes row-flash-up {
  0% {
    background-color: rgba(224, 49, 49, 0.16);
  }
  100% {
    background-color: transparent;
  }
}
@keyframes row-flash-down {
  0% {
    background-color: rgba(15, 157, 88, 0.16);
  }
  100% {
    background-color: transparent;
  }
}

</style>




