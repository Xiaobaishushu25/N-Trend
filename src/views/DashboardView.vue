<script setup lang="ts">
import { h, nextTick, onActivated, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import Sortable from 'sortablejs'
import {
  NButton,
  NDataTable,
  NInput,
  NModal,
  NSpace,
  NSwitch,
  NTab,
  NTabs,
  NText,
  type DataTableColumns,
} from 'naive-ui'
import { api, onDataUpdated, onQuotesUpdated, onScanCompleted } from '../services/api'
import { useGroupsStore } from '../stores/groups'
import { useSettingsStore } from '../stores/settings'
import { useSymbolsStore } from '../stores/symbols'
import { useScansStore } from '../stores/scans'
import { confirmAction } from '../utils/confirm'
import { notify } from '../utils/notify'
import { openSymbolContextMenu } from '../utils/symbolMenu'
import type { GroupRow, MarketSnapshot, SignalRow, SymbolRow } from '../types'

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
  signal: SignalRow | null
  /** 最近一次行情跳动的方向：up=上涨(红) / down=下跌(绿)，用于行呼吸闪烁 */
  flash: 'up' | 'down' | null
}

const rows = ref<WatchRow[]>([])
const loading = ref(false)
const refreshing = ref(false)
const scanning = ref(false)
const enriching = ref(false)
const newCode = ref('')
const groupModal = ref<'create' | 'manage' | null>(null)
const newGroupName = ref('')
const groupNameDrafts = ref<Record<number, string>>({})
/** 表格外层容器（Sortable 挂载到其内部 tbody 上） */
const tableWrapEl = ref<HTMLElement | null>(null)
let tableSortable: Sortable | null = null
let unlisteners: (() => void)[] = []
/** 行闪烁清除计时器：动画结束后把 flash 归零，保证下一次跳动能重新触发动画 */
const flashTimers = new Map<string, ReturnType<typeof setTimeout>>()
const FLASH_MS = 900

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

async function loadAll() {
  loading.value = true
  try {
    await symbolsStore.load()
    // 分组视图只加载该组内的品种；全部视图加载全部品种
    let symbols = symbolsStore.symbols
    if (groupsStore.selectedId != null) {
      symbols = await api.getGroupSymbols(groupsStore.selectedId)
    }
    const signals = await api.getLatestSignals(500)
    const bySymbol = new Map<string, SignalRow[]>()
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
  if (!snapshots.length || !rows.value.length) return
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
          rows.value = rows.value.map((x) =>
            x.symbol.code === r.symbol.code && x.flash === flash ? { ...x, flash: null } : x,
          )
          flashTimers.delete(r.symbol.code)
        }, FLASH_MS),
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
    style: `cursor: ${groupsStore.selectedId != null ? 'grab' : 'pointer'}`,
    class: [
      row.flash ? (row.flash === 'up' ? 'row-flash-up' : 'row-flash-down') : '',
    ],
    'data-code': row.symbol.code,
    onDblclick: () => openChart(row),
    onContextmenu: (e: MouseEvent) => onRowContextMenu(row, e),
  }
}

/**
 * Sortable 直接挂载到表格 tbody：行顺序变化后读取 DOM 新顺序落库并广播。
 * 只在分组视图启用（全部视图没有全局顺序字段）。
 */
function setupTableSortable() {
  if (tableSortable) return
  const tbody = tableWrapEl.value?.querySelector<HTMLElement>('.watch-table tbody')
  if (!tbody) return
  tableSortable = Sortable.create(tbody, {
    animation: 150,
    draggable: 'tr[data-code]',
    ghostClass: 'sl-table-ghost',
    chosenClass: 'sl-table-chosen',
    disabled: groupsStore.selectedId == null,
    onEnd: () => persistTableOrder(tbody),
  })
}

async function persistTableOrder(tbody: HTMLElement) {
  const groupId = groupsStore.selectedId
  if (groupId == null) return
  const codes = [...tbody.querySelectorAll<HTMLElement>('tr[data-code]')]
    .map((tr) => tr.dataset.code)
    .filter((c): c is string => c != null)
  if (!codes.length) return
  // 让 Vue 数据与 DOM 新顺序保持一致
  const byCode = new Map(rows.value.map((r) => [r.symbol.code, r]))
  const next = codes.map((c) => byCode.get(c)).filter((r): r is WatchRow => r != null)
  if (!next.length) return
  rows.value = next
  try {
    await api.reorderGroupSymbols(groupId, codes)
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
}

async function doRenameGroup(g: GroupRow) {
  const name = (groupNameDrafts.value[g.id] ?? '').trim()
  if (!name || name === g.name) return
  try {
    await groupsStore.rename(g.id, name)
    notify.success(`已重命名为「${name}」`)
  } catch (err) {
    notify.error(String(err))
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

function dirLabel(s: SignalRow) {
  return s.direction === 'up' ? '做多' : s.direction === 'down' ? '做空' : s.direction
}

function levelLabel(s: SignalRow) {
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
  try {
    const count = await symbolsStore.add(code)
    notify.success(`${code} 已添加，回填 ${count} 根K线`)
    newCode.value = ''
    await loadAll()
  } catch (e) {
    notify.error(String(e))
  }
}

onMounted(async () => {
  await groupsStore.load()
  unlisteners.push(await onDataUpdated(() => loadAll()))
  unlisteners.push(await onQuotesUpdated(applyQuotes))
  // 扫描完成时同步更新内存里的最新扫描结果，避免图表页「全部N形态」停留在旧扫描
  unlisteners.push(
    await onScanCompleted((result) => {
      scansStore.ingest(result)
      loadAll()
    }),
  )
  await loadAll()
  await nextTick()
  setupTableSortable()
})

// 组内顺序在别处被改动（如K线页左侧拖拽）时，重拉表格
watch(() => groupsStore.revision, () => loadAll())
// 切换分组时同步开关表格拖拽；keep-alive 回到页面时重建 Sortable（tbody 可能被重建）
watch(
  () => groupsStore.selectedId,
  () => {
    tableSortable?.option('disabled', groupsStore.selectedId == null)
  },
)
onActivated(() => {
  tableSortable?.destroy()
  tableSortable = null
  setupTableSortable()
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
    <div class="group-bar">
      <n-tabs
        :value="groupsStore.selectedId == null ? 'all' : String(groupsStore.selectedId)"
        type="line"
        size="small"
        class="group-tabs"
        @update:value="onSelectGroup"
      >
        <n-tab name="all">全部品种</n-tab>
        <n-tab v-for="g in groupsStore.groups" :key="g.id" :name="String(g.id)">
          {{ g.name }}
        </n-tab>
      </n-tabs>
      <n-space align="center">
        <n-button size="small" @click="openCreateGroup">新建分组</n-button>
        <n-button
          size="small"
          ghost
          :disabled="!groupsStore.groups.length"
          @click="openManageGroup"
        >
          管理分组
        </n-button>
      </n-space>
    </div>
    <div ref="tableWrapEl" class="watch-table-wrap">
      <n-data-table
        class="watch-table"
        :columns="columns"
        :data="rows"
        :loading="loading"
        :row-props="rowProps"
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
      style="width: 440px"
      @update:show="(v: boolean) => { if (!v) groupModal = null }"
    >
      <n-space vertical size="small">
        <div v-for="g in groupsStore.groups" :key="g.id" class="group-manage-row">
          <n-input v-model:value="groupNameDrafts[g.id]" size="small" style="flex: 1" />
          <n-button size="small" @click="doRenameGroup(g)">保存</n-button>
          <n-button size="small" type="error" ghost @click="doDeleteGroup(g)">删除</n-button>
        </div>
        <n-text v-if="!groupsStore.groups.length" depth="3" style="text-align: center">
          暂无分组
        </n-text>
      </n-space>
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
}
.group-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.group-tabs {
  flex: 1;
  min-width: 0;
}
.group-manage-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.watch-table-wrap {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
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

/* 分组视图下表格行支持拖拽排序 */
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
</style>




