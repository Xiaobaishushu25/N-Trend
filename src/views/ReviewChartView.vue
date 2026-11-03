<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { isTauri } from '@tauri-apps/api/core'
import { listen } from '@tauri-apps/api/event'
import { getCurrentWindow } from '@tauri-apps/api/window'
import { NAlert, NButton, NIcon, NSpin, NTag } from 'naive-ui'
import { ArrowDown, ArrowLeft, ArrowRight, ArrowUp, Eye, EyeOff, Focus2, Refresh } from '@vicons/tabler'
import KLineChart from '../components/KLineChart.vue'
import ReviewSignalPool from '../components/ReviewSignalPool.vue'
import ReviewSignalDetailDrawer from '../components/ReviewSignalDetailDrawer.vue'
import { useKlinesStore } from '../stores/klines'
import { useSymbolsStore } from '../stores/symbols'
import { api } from '../services/api'
import type { PatternDto, ReviewExitOverlay, SymbolRow } from '../types'
import type { ReviewQuery, ReviewScoreMetric, ReviewSummary } from '../types/review'
import type { OpenReviewChartParams } from '../utils/openReviewChartWindow'
import { validatePublishedPage } from '../utils/reviewPresentation'
import { useReviewDetail } from '../utils/useReviewDetail'

const route = useRoute()
const router = useRouter()
const klinesStore = useKlinesStore()
const symbolsStore = useSymbolsStore()
const chartRef = ref<InstanceType<typeof KLineChart> | null>(null)
const currentSymbol = ref('')
const currentEventId = ref<number | null>(null)
const currentVersion = ref('')
const currentSnapshotId = ref('')
const signalList = ref<ReviewSummary[]>([])
const detailRequest = useReviewDetail()
const { detail: activeDetail, loading: detailLoading, error: detailError } = detailRequest
const poolLoading = ref(false)
const poolError = ref('')
const total = ref(0)
const cursor = ref(0)
const query = ref<ReviewQuery>({ page_size: 120 })
const metric = ref<ReviewScoreMetric>('quality_v2')
const pageSize = computed(() => query.value.page_size || 120)
let poolRequest = 0
let contextRequest = 0
const focusTs = ref<string | null>(null)
const focusKey = ref<number | null>(null)
const isSignalHidden = ref(false)
function toggleSignalVisibility() { isSignalHidden.value = !isSignalHidden.value }
const symbolInfo = computed<SymbolRow | undefined>(() => symbolsStore.symbols.find(s => s.code === currentSymbol.value))
const activeSignal = computed(() => activeDetail.value?.summary ?? signalList.value.find(s => s.event_id === currentEventId.value && s.result_version === currentVersion.value))
const currentIndex = computed(() => signalList.value.findIndex(s => s.event_id === currentEventId.value))
const hasPrev = computed(() => currentIndex.value > 0)
const hasNext = computed(() => currentIndex.value >= 0 && currentIndex.value < signalList.value.length - 1)
const activeOutsidePage = computed(() => !!activeSignal.value && currentIndex.value < 0)

const activePatternDto = computed<PatternDto[]>(() => {
  const d = activeDetail.value
  if (isSignalHidden.value || !d) return []
  const s = d.summary, orig = d.original_event
  const num = (key: string) => typeof orig[key] === 'number' ? orig[key] as number : 0
  const str = (key: string) => typeof orig[key] === 'string' ? orig[key] as string : ''
  const down = s.direction === 'down'
  // Structural geometry is drawn only after the matching immutable detail is loaded.
  return [{ number: s.event_id, quality: d.quality, level: s.level, logic_version: str('logic_version'), warning_kind: str('warning_kind'), direction: s.direction, grade: s.grade,
    s0: { index: 0, price: num('s0_price'), is_high: down, ts: str('s0_ts') },
    s1: { index: 0, price: num('s1_price'), is_high: !down, ts: str('s1_ts') },
    s2: { index: 0, price: num('s2_price'), is_high: down, ts: str('s2_ts') },
    a_bars: num('a_bars'), b_bars: num('b_bars'), a_move: num('a_move'), b_move: num('b_move'), retracement: num('retracement'),
    state: s.state, category: '', entry: num('entry'), stop: num('stop'), target: num('target'), risk: num('risk'), space: 0, rr: num('rr'),
    score: s.scores.old ?? 0, warning_ts: s.warning_ts, trigger_ts: s.entry_ts,
    vol_ratio: s.diagnostics.vol_ratio ?? null, vol_confirmed: (s.diagnostics.vol_ratio ?? 0) >= 2,
    trigger_overshoot_r: s.diagnostics.overshoot ?? null, box: null, note: '', active: true, trend_state: '', trend_bonus: 0, trend_label: '',
    outcome: s.state, exit_reason: s.exit_reason ?? undefined, r_multiple: s.net_r,
  }]
})
const reviewExit = computed<ReviewExitOverlay | null>(() => {
  const d = activeDetail.value
  if (isSignalHidden.value || !d || d.exit_price == null || !d.summary.exit_ts) return null
  return { price: d.exit_price, ts: d.summary.exit_ts, outcome: d.summary.exit_reason || d.summary.state, r: d.summary.net_r }
})
async function loadKlines(sym: string) { if (sym) await klinesStore.load(sym, '15m', 800, false) }
async function loadDetail(eventId: number, version?: string) {
  const result = await detailRequest.load(eventId, version)
  if (!result || currentEventId.value !== eventId) return
  currentVersion.value = result.summary.result_version
  if (result.summary.symbol !== currentSymbol.value) {
    currentSymbol.value = result.summary.symbol
    await loadKlines(result.summary.symbol)
  }
  if (currentEventId.value === eventId && activeDetail.value?.summary.result_version === result.summary.result_version) focusCurrentSignal()
}
async function selectSignal(sig: ReviewSummary) {
  currentEventId.value = sig.event_id
  currentVersion.value = sig.result_version
  isSignalHidden.value = false
  if (currentSymbol.value !== sig.symbol) { currentSymbol.value = sig.symbol; void loadKlines(sig.symbol) }
  await loadDetail(sig.event_id, sig.result_version)
}
async function loadSignalPool(snapshotId = '', offset = cursor.value) {
  const ticket = ++poolRequest
  poolLoading.value = true; poolError.value = ''; signalList.value = []
  try {
    let snapshot = snapshotId
    let page
    if (snapshot) {
      try {
        page = await api.reviewItems(snapshot, { cursor: offset, limit: pageSize.value })
      } catch (err: any) {
        const msg = String(err?.message || err)
        if (msg.includes('SNAPSHOT_EXPIRED') || msg.includes('expired') || msg.includes('410')) {
          console.warn('复盘快照已失效，正在按原筛选条件自动续期重建快照...')
          snapshot = ''
        } else {
          throw err
        }
      }
    }
    if (!snapshot) {
      const result = await api.reviewQuery(query.value)
      if (ticket !== poolRequest) return
      validatePublishedPage(result)
      snapshot = result.snapshot_id
      if (offset >= result.total) {
        offset = Math.max(0, Math.floor(Math.max(0, result.total - 1) / pageSize.value) * pageSize.value)
      }
      page = offset ? await api.reviewItems(snapshot, { cursor: offset, limit: pageSize.value }) : result
    }
    if (ticket !== poolRequest || !page) return
    validatePublishedPage(page)
    if (page.snapshot_id !== snapshot) throw new Error('复盘快照不一致')
    signalList.value = page.items; total.value = page.total; cursor.value = offset; currentSnapshotId.value = snapshot
    if (!page.total) {
      const meta = await api.reviewMetadata()
      if (ticket !== poolRequest) return
      const job = meta.jobs.find(j => j.status === 'running' || j.status === 'queued')
      poolError.value = job ? `复盘计算中：${job.completed_symbols}/${job.total_symbols} 个品种，尚无匹配的已发布结果。` : meta.jobs[0]?.status === 'failed' ? `复盘任务失败：${meta.jobs[0].errors.join('；')}` : '当前筛选下暂无已发布复盘结果。'
    }
  } catch (e) {
    if (ticket === poolRequest) { poolError.value = `${String(e)}；可按原筛选刷新快照。`; total.value = 0 }
  } finally { if (ticket === poolRequest) poolLoading.value = false }
}
async function changePoolPage(offset: number) {
  const context = contextRequest
  const ticket = poolRequest + 1
  detailRequest.cancel(); currentEventId.value = null; currentVersion.value = ''
  await loadSignalPool(currentSnapshotId.value, offset)
  if (context !== contextRequest || ticket !== poolRequest) return
  if (signalList.value[0]) await selectSignal(signalList.value[0])
}
async function navigateSignal(offset: number) {
  if (poolLoading.value) return
  const nextIdx = currentIndex.value + offset
  if (nextIdx >= 0 && nextIdx < signalList.value.length) {
    const row = signalList.value[nextIdx]
    if (row) void selectSignal(row)
    return
  }
  // 滚动跨越当前分页末尾，自动加载下一页
  if (offset > 0 && cursor.value + pageSize.value < total.value) {
    await changePoolPage(cursor.value + pageSize.value)
    return
  }
  // 滚动跨越当前分页起始，自动返回上一页并聚焦上一页尾条
  if (offset < 0 && cursor.value > 0) {
    const prevCursor = Math.max(0, cursor.value - pageSize.value)
    await changePoolPage(prevCursor)
    if (signalList.value.length > 0) {
      await selectSignal(signalList.value[signalList.value.length - 1])
    }
    return
  }
}

/** 鼠标滚轮在K线图区域平滑累积滚动量切换信号池信号（按住Ctrl时交给图表缩放） */
const wheelAcc = ref(0)
let lastSwitchAt = 0
const WHEEL_SWITCH_THRESHOLD = 20
const WHEEL_SWITCH_INTERVAL = 120

function handleChartWheel(e: WheelEvent) {
  if (e.ctrlKey || e.shiftKey || e.altKey || e.metaKey) return
  const now = Date.now()
  if (now - lastSwitchAt < WHEEL_SWITCH_INTERVAL) return
  wheelAcc.value += e.deltaY
  if (Math.abs(wheelAcc.value) < WHEEL_SWITCH_THRESHOLD) return
  const dir = wheelAcc.value > 0 ? 1 : -1
  wheelAcc.value = 0
  lastSwitchAt = now
  void navigateSignal(dir)
}
function focusCurrentSignal() {
  const s = activeDetail.value?.summary
  if (s && s.event_id === currentEventId.value) { focusTs.value = s.entry_ts || s.warning_ts; focusKey.value = Date.now() }
}
function handleKeydown(e: KeyboardEvent) {
  const target = e.target as HTMLElement | null
  if (target?.closest('input,textarea,[contenteditable="true"]')) return
  if (['p', 'P', 'ArrowUp'].includes(e.key)) { e.preventDefault(); void navigateSignal(-1) }
  else if (['n', 'N', 'ArrowDown'].includes(e.key)) { e.preventDefault(); void navigateSignal(1) }
  else if (e.key === 'Escape' && isTauri()) void getCurrentWindow().close()
}
async function applyContext(params: OpenReviewChartParams) {
  const ticket = ++contextRequest
  detailRequest.cancel(); ++poolRequest
  currentEventId.value = params.eventId; currentVersion.value = params.version || ''; currentSnapshotId.value = params.snapshotId || ''
  currentSymbol.value = params.symbol; query.value = params.query || { page_size: 120 }; cursor.value = params.cursor || 0
  metric.value = params.metric || params.query?.filters?.score_metric || 'quality_v2'
  await Promise.all([loadKlines(params.symbol), loadSignalPool(params.snapshotId, cursor.value), loadDetail(params.eventId, params.version)])
  if (ticket !== contextRequest) return
}
function routeContext(): OpenReviewChartParams | null {
  const id = Number(route.query.eventId || route.query.review)
  if (!Number.isSafeInteger(id) || id <= 0) { poolError.value = '缺少有效信号编号'; return null }
  let savedQuery: ReviewQuery | undefined
  try { savedQuery = typeof route.query.context === 'string' ? JSON.parse(route.query.context) : undefined } catch { poolError.value = '复盘筛选上下文无效'; return null }
  return { symbol: String(route.params.symbol || route.query.symbol || ''), eventId: id, version: typeof route.query.version === 'string' ? route.query.version : undefined,
    snapshotId: typeof route.query.snapshotId === 'string' ? route.query.snapshotId : undefined, query: savedQuery, cursor: Number(route.query.cursor) || 0, metric: (route.query.metric || undefined) as ReviewScoreMetric | undefined }
}
const unlisteners: (() => void)[] = []
let stopRoute: (() => void) | undefined
onMounted(async () => {
  window.addEventListener('keydown', handleKeydown)
  void symbolsStore.load().catch(() => {})
  stopRoute = watch(() => route.fullPath, () => { const params = routeContext(); if (params) void applyContext(params) }, { immediate: true })
  if (isTauri()) {
    try { unlisteners.push(await listen<OpenReviewChartParams>('load-review-signal', e => { void applyContext(e.payload) })) }
    catch (e) { poolError.value = `无法监听跨窗口定位：${String(e)}` }
  }
})
onBeforeUnmount(() => { ++contextRequest; ++poolRequest; detailRequest.cancel(); stopRoute?.(); window.removeEventListener('keydown', handleKeydown); unlisteners.forEach(fn => fn()) })
</script>

<template>
  <div class="review-chart-page">
    <!-- 顶部极简操作工具条（注意：不再渲染 TitleBar，避免与 AppLayout 原生窗口控制栏产生双套按钮） -->
    <header class="chart-header-subbar" @wheel.prevent="handleChartWheel">
      <div class="ch-left">
        <!-- 信号前后切换漫游 -->
        <div class="tb-signal-nav">
          <n-button
            secondary
            size="tiny"
            :disabled="!hasPrev"
            title="快捷键 P 或 ↑"
            @click="navigateSignal(-1)"
          >
            <template #icon>
              <n-icon :component="ArrowLeft" />
            </template>
            上一条
          </n-button>

          <span v-if="currentIndex >= 0" class="tb-nav-counter font-num">
            {{ currentIndex + 1 }} / {{ signalList.length }}
          </span>

          <n-button
            secondary
            size="tiny"
            :disabled="!hasNext"
            title="快捷键 N 或 ↓"
            @click="navigateSignal(1)"
          >
            下一条
            <template #icon>
              <n-icon :component="ArrowRight" />
            </template>
          </n-button>
        </div>

        <div class="ch-divider"></div>

        <!-- 当前信号身份标识 -->
        <div v-if="activeSignal" class="ch-signal-badge">
          <span class="sym-code font-bold font-num">{{ currentSymbol }}</span>
          <span class="sym-name text-muted">{{ symbolInfo?.name || currentSymbol }}</span>
          <span class="dir-badge" :class="activeSignal.direction === 'up' ? 'is-up' : 'is-down'">
            <n-icon :component="activeSignal.direction === 'up' ? ArrowUp : ArrowDown" :size="10" />
            {{ activeSignal.direction === 'up' ? '做多' : '做空' }}
          </span>
          <span class="spec-tag">{{ activeSignal.level === 'fine' ? '精细' : activeSignal.level }}N · {{ activeSignal.grade }}</span>
          <span class="event-id-tag font-num">#{{ activeSignal.event_id }}</span>
          <span class="tf-tag">15m 形态 · 5m 回放</span>
        </div>
      </div>

      <div class="ch-right">
        <!-- 形态打点显隐开关 -->
        <n-button
          size="tiny"
          secondary
          :type="isSignalHidden ? 'warning' : 'default'"
          :title="isSignalHidden ? '点击在K线图上显示该形态打点' : '点击在K线图上隐藏该形态打点'"
          @click="toggleSignalVisibility"
        >
          <template #icon>
            <n-icon :component="isSignalHidden ? EyeOff : Eye" />
          </template>
          {{ isSignalHidden ? '形态打点已隐藏' : '隐藏形态打点' }}
        </n-button>

        <n-button size="tiny" secondary type="primary" @click="focusCurrentSignal">
          <template #icon>
            <n-icon :component="Focus2" />
          </template>
          居中聚焦形态
        </n-button>

        <n-button quaternary size="tiny" title="重新加载K线" @click="loadKlines(currentSymbol)">
          <template #icon>
            <n-icon :component="Refresh" />
          </template>
          刷新K线
        </n-button>

        <span class="shortcut-tip text-muted">快捷键: P 上一条 · N 下一条 · 滚轮 切换 · Esc 关闭</span>
      </div>
    </header>

    <!-- 两栏核心主体：左侧满屏K线（无左侧品种树） + 右侧复盘信号池 -->
    <div class="chart-content-row">
      <!-- K线视口区域 -->
      <section class="chart-viewport-box" @wheel.prevent="handleChartWheel">
        <div class="chart-canvas-wrap">
          <KLineChart
            v-if="currentSymbol && (klinesStore.rows.length || klinesStore.loading)"
            ref="chartRef"
            :symbol="currentSymbol"
            timeframe="15m"
            :rows="klinesStore.rows"
            :signals="activePatternDto"
            :review-exit="reviewExit"
            :focus-ts="focusTs"
            :focus-key="focusKey"
            :loading="klinesStore.loading"
          />

          <div v-else class="chart-empty-state">
            <n-spin v-if="klinesStore.loading" size="medium" />
            <span v-else class="text-muted">暂无 [{{ currentSymbol }}] 15m K线数据</span>
          </div>
        </div>
      </section>

      <!-- 右侧信号池与平仓流水 / A/B段评分细则 -->
      <aside class="signal-pool-sidebar">
        <n-alert v-if="detailError" type="warning" :show-icon="false">{{ detailError }}<n-button size="tiny" @click="currentEventId && loadDetail(currentEventId, currentVersion || undefined)">重试详情</n-button></n-alert>
        <n-alert v-if="poolError" type="warning" :show-icon="false">{{ poolError }}<n-button size="tiny" @click="loadSignalPool('', cursor)">按原筛选刷新</n-button></n-alert>
        <div class="pool-pager"><n-button size="tiny" :disabled="poolLoading || cursor === 0" @click="changePoolPage(Math.max(0, cursor - pageSize))">上一页</n-button><span>{{ total ? cursor + 1 : 0 }}–{{ Math.min(cursor + signalList.length, total) }} / {{ total }}</span><n-button size="tiny" :disabled="poolLoading || cursor + pageSize >= total" @click="changePoolPage(cursor + pageSize)">下一页</n-button></div>
        <div v-if="activeOutsidePage"><span>当前定位信号（不属于本页）</span><ReviewSignalDetailDrawer :summary="activeSignal" :detail="activeDetail" :loading="detailLoading" :error="detailError" @retry="currentEventId && loadDetail(currentEventId, currentVersion || undefined)" /></div>
        <ReviewSignalPool
          :signals="signalList"
          :selected-id="currentEventId"
          :detail="activeDetail"
          :detail-loading="detailLoading"
          :detail-error="detailError"
          :metric="metric"
          :pool-loading="poolLoading"
          :is-signal-hidden="isSignalHidden"
          @select-signal="selectSignal"
          @retry="currentEventId && loadDetail(currentEventId, currentVersion || undefined)"
          @toggle-visibility="toggleSignalVisibility"
        />
      </aside>
    </div>
  </div>
</template>

<style scoped>
.pool-pager { display:flex; align-items:center; justify-content:space-between; gap:8px; padding:6px; font-size:11px; flex:none }
.review-chart-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  width: 100%;
  background: #f8fafc;
  color: #1e293b;
  overflow: hidden;
  box-sizing: border-box;
}

/* 顶部操作子工具栏 */
.chart-header-subbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 14px;
  background: #ffffff;
  border-bottom: 1px solid #e2e8f0;
  flex: none;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.02);
}

.ch-left {
  display: flex;
  align-items: center;
  gap: 10px;
}

.tb-signal-nav {
  display: flex;
  align-items: center;
  gap: 4px;
}
.tb-nav-counter {
  font-size: 11px;
  color: #475569;
  padding: 0 6px;
  font-weight: 600;
}

.ch-divider {
  width: 1px;
  height: 16px;
  background: #e2e8f0;
}

.ch-signal-badge {
  display: flex;
  align-items: center;
  gap: 6px;
}
.sym-code {
  font-size: 13px;
  color: #0f172a;
}
.sym-name {
  font-size: 12px;
}

.dir-badge {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  font-size: 10px;
  font-weight: 700;
  padding: 1px 5px;
  border-radius: 3px;
}
.dir-badge.is-up {
  background: #fef2f2;
  color: #ef4444;
}
.dir-badge.is-down {
  background: #ecfdf5;
  color: #10b981;
}

.spec-tag {
  font-size: 10px;
  background: #f1f5f9;
  color: #475569;
  padding: 1px 6px;
  border-radius: 3px;
}
.event-id-tag {
  font-size: 10px;
  color: #64748b;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  padding: 1px 5px;
  border-radius: 3px;
}
.tf-tag {
  font-size: 10px;
  background: #eff6ff;
  color: #2563eb;
  padding: 1px 5px;
  border-radius: 3px;
}

.ch-right {
  display: flex;
  align-items: center;
  gap: 10px;
}
.shortcut-tip {
  font-size: 11px;
  color: #94a3b8;
}

/* 两栏核心主体 */
.chart-content-row {
  display: flex;
  flex: 1;
  min-height: 0;
  width: 100%;
  height: 100%;
  overflow: hidden;
}

/* 左侧K线容器 */
.chart-viewport-box {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
  height: 100%;
  background: #ffffff;
  overflow: hidden;
}

.chart-canvas-wrap {
  position: relative;
  flex: 1 1 0;
  min-height: 0;
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
}

.chart-empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  gap: 10px;
}

/* 右侧信号池侧栏 */
.signal-pool-sidebar {
  width: 440px;
  height: 100%;
  flex: none;
  border-left: 1px solid #e2e8f0;
  background: #ffffff;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* 通用字体样式 */
.font-num {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  font-variant-numeric: tabular-nums;
  font-feature-settings: "tnum";
  font-weight: 500;
}
.font-bold {
  font-weight: 700;
}
.text-muted {
  color: #64748b;
}
</style>
