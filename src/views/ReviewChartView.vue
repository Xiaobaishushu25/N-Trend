<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { isTauri } from '@tauri-apps/api/core'
import { listen } from '@tauri-apps/api/event'
import { getCurrentWindow } from '@tauri-apps/api/window'
import {
  NButton,
  NEmpty,
  NIcon,
  NSpin,
  NTag,
} from 'naive-ui'
import {
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  Focus2,
  Refresh,
} from '@vicons/tabler'
import TitleBar from '../components/TitleBar.vue'
import KLineChart from '../components/KLineChart.vue'
import ReviewSignalPool from '../components/ReviewSignalPool.vue'
import { useKlinesStore } from '../stores/klines'
import { useSymbolsStore } from '../stores/symbols'
import { api } from '../services/api'
import type { PatternDto, ReviewExitOverlay, SymbolRow } from '../types'
import type { ReviewResultDetail, ReviewSummary } from '../types/review'
import type { OpenReviewChartParams } from '../utils/openReviewChartWindow'
import { notify } from '../utils/notify'
import { fmtR } from '../stores/review'

const route = useRoute()
const router = useRouter()
const klinesStore = useKlinesStore()
const symbolsStore = useSymbolsStore()

const currentSymbol = ref<string>('')
const currentEventId = ref<number | null>(null)
const currentVersion = ref<string>('')
const currentSnapshotId = ref<string>('')

const signalList = ref<(ReviewSummary | any)[]>([])
const activeDetail = ref<ReviewResultDetail | null>(null)
const detailLoading = ref(false)
const poolLoading = ref(false)

const focusTs = ref<string | null>(null)
const focusKey = ref<number | null>(null)

// 查找当前品种详情
const symbolInfo = computed<SymbolRow | undefined>(() => {
  return symbolsStore.symbols.find((s) => s.code === currentSymbol.value)
})

// 当前选中的信号概要
const activeSignal = computed(() => {
  return signalList.value.find((s) => s.event_id === currentEventId.value) || activeDetail.value?.summary
})

// 信号池索引与翻页
const currentIndex = computed(() => {
  return signalList.value.findIndex((s) => s.event_id === currentEventId.value)
})

const hasPrev = computed(() => currentIndex.value > 0)
const hasNext = computed(() => currentIndex.value >= 0 && currentIndex.value < signalList.value.length - 1)

function buildPatternDto(
  orig: any,
  summary: any,
  quality: any,
  detail: any,
): PatternDto {
  const s0High = (summary?.direction || orig?.direction) === 'down'
  return {
    number: summary?.event_id || orig?.id || orig?.event_id || 0,
    quality: quality ?? null,
    level: summary?.level || orig?.level || 'fine',
    logic_version: orig?.logic_version || '4',
    warning_kind: orig?.warning_kind || '',
    direction: summary?.direction || orig?.direction || 'up',
    grade: summary?.grade || orig?.grade || 'A',
    s0: {
      index: 0,
      price: orig?.s0_price || orig?.planned_entry || detail?.entry_price || 0,
      is_high: s0High,
      ts: orig?.s0_ts || summary?.warning_ts || '',
    },
    s1: {
      index: 0,
      price: orig?.s1_price || orig?.planned_entry || detail?.entry_price || 0,
      is_high: !s0High,
      ts: orig?.s1_ts || summary?.entry_ts || summary?.warning_ts || '',
    },
    s2: {
      index: 0,
      price: orig?.s2_price || orig?.planned_stop || 0,
      is_high: s0High,
      ts: orig?.s2_ts || summary?.entry_ts || '',
    },
    a_bars: orig?.a_bars || 0,
    b_bars: orig?.b_bars || 0,
    a_move: orig?.a_move || 0,
    b_move: orig?.b_move || 0,
    retracement: orig?.retracement || 0,
    state: summary?.state || orig?.state || 'triggered',
    category: '',
    entry: orig?.entry || orig?.planned_entry || detail?.entry_price || 0,
    stop: orig?.stop || orig?.planned_stop || 0,
    target: orig?.target || orig?.planned_target || 0,
    risk: summary?.risk_points || orig?.risk || 0,
    space: 0,
    rr: orig?.rr || summary?.rr || 1.0,
    score: orig?.entry_score || summary?.scores?.quality_v2 || 0,
    warning_ts: orig?.warning_ts || summary?.warning_ts || null,
    trigger_ts: orig?.trigger_ts || summary?.entry_ts || null,
    vol_ratio: orig?.trigger_volume_ratio || summary?.diagnostics?.vol_ratio || null,
    vol_confirmed: Boolean(orig?.trigger_volume_ratio != null || summary?.diagnostics?.vol_ratio != null),
    trigger_overshoot_r: orig?.overshoot_r || summary?.diagnostics?.overshoot_r || null,
    box: null,
    note: '',
    active: true,
    trend_state: '',
    trend_bonus: 0,
    trend_label: '',
    outcome: summary?.exit_reason || summary?.outcome || orig?.outcome,
    exit_reason: summary?.exit_reason || orig?.exit_reason,
    r_multiple: summary?.net_r ?? summary?.r_multiple ?? null,
  }
}

// 将 detail.original_event 转化为 KLineChart 需要的 PatternDto
const activePatternDto = computed<PatternDto[]>(() => {
  if (activeDetail.value) {
    const d = activeDetail.value
    return [buildPatternDto(d.original_event, d.summary, d.quality, d)]
  }
  const s = activeSignal.value
  if (!s) return []
  return [buildPatternDto(s, s, (s as any).quality, null)]
})

// 复盘出场标记覆盖层
const reviewExit = computed<ReviewExitOverlay | null>(() => {
  const d = activeDetail.value
  if (!d) {
    const s = activeSignal.value
    if (!s || !s.exit_ts) return null
    return {
      price: s.exit_price ?? null,
      ts: s.exit_ts,
      outcome: s.exit_reason || s.outcome || 'closed',
      r: (s as any).net_r ?? (s as any).r_multiple ?? null,
    }
  }

  const exitPrice = d.exit_price ?? (d.summary as any).exit_price
  const exitTs = d.summary.exit_ts
  if (exitPrice == null || !exitTs) return null

  return {
    price: exitPrice,
    ts: exitTs,
    outcome: d.summary.exit_reason || d.summary.state || 'closed',
    r: (d.summary as any).net_r ?? (d.summary as any).r_multiple ?? null,
  }
})

/** 加载当前选中的信号详情 */
async function loadDetail(eventId: number, version?: string) {
  detailLoading.value = true
  try {
    const v = version || currentVersion.value || 'review-fixed-one-r-5m-v1'
    const res = await api.reviewDetail(eventId, v)
    activeDetail.value = res

    // 聚焦时间更新
    const ts = res.summary.entry_ts || res.summary.warning_ts
    if (ts) {
      focusTs.value = ts
      focusKey.value = eventId
    }
  } catch (err) {
    console.warn(`加载信号详情失败 #${eventId}:`, err)
    // 降级使用旧接口读取详情
    try {
      const legacy = await api.getReviewSignal(eventId)
      if (legacy) {
        activeDetail.value = {
          summary: {
            ...legacy.event,
            event_id: legacy.event.id,
            result_version: 'legacy',
            symbol: legacy.event.symbol,
            direction: legacy.event.direction,
            level: legacy.event.level,
            grade: legacy.event.grade,
            warning_ts: legacy.event.warning_ts || '',
            entry_ts: legacy.event.trigger_ts || legacy.event.warning_ts,
            exit_ts: legacy.outcome?.exit_ts ?? null,
            state: legacy.event.state,
            exit_reason: legacy.outcome?.exit_reason ?? null,
            terminal_reason: null,
            family_id: legacy.event.id,
            family_size: 1,
            scores: { old: legacy.event.entry_score, quality_v1: null, morphology_v1: null, quality_v2: null, morphology_v2: null, a_score: null, b_score: null },
            score_status: 'scored',
            risk_points: legacy.event.risk,
            risk_amount: null,
            gross_amount: null,
            net_amount: (legacy.outcome as any)?.pnl_amount ?? null,
            gross_r: null,
            net_r: legacy.outcome?.r_multiple ?? null,
            mfe_r: legacy.outcome?.mfe_r ?? null,
            mae_r: legacy.outcome?.mae_r ?? null,
            mfe_amount: null,
            mae_amount: null,
            bars_held: legacy.outcome?.bars_held ?? 0,
            wait_bars: 0,
            coverage: [],
            diagnostics: {},
          },
          profile: 'legacy',
          cost_version: '',
          source_minutes: 15,
          origin: 'legacy',
          input_hash: '',
          calendar_version: '',
          parameter_hash: '',
          computed_at: '',
          multiplier: null,
          multiplier_source: '',
          entry_price: legacy.event?.entry ?? null,
          exit_price: legacy.outcome?.exit_price ?? null,
          cost: {},
          original_event: legacy.event as any,
          quality: legacy.outcome?.quality ?? null,
          review_scores: legacy.outcome?.review_scores ?? null,
          lifecycle: [],
          mfe: { r: legacy.outcome?.mfe_r ?? null, amount: null, price: null, interval_start: null, interval_end: null, lower_r: null, upper_r: null, precision: '' },
          mae: { r: legacy.outcome?.mae_r ?? null, amount: null, price: null, interval_start: null, interval_end: null, lower_r: null, upper_r: null, precision: '' },
          holding_count: legacy.outcome?.bars_held ?? 0,
        }
        const ts = legacy.outcome?.trigger_ts || legacy.event?.trigger_ts || legacy.event?.warning_ts
        if (ts) {
          focusTs.value = ts
          focusKey.value = eventId
        }
      }
    } catch {}
  } finally {
    detailLoading.value = false
  }
}

/** 加载图表 K 线 */
async function loadKlines(sym: string) {
  if (!sym) return
  await klinesStore.load(sym, '15m', 800, false)
}

/** 加载快照或候选信号池列表 */
async function loadSignalPool(snapshotId?: string) {
  poolLoading.value = true
  try {
    if (snapshotId) {
      const page = await api.reviewItems(snapshotId, { cursor: 0, limit: 100 })
      if (page.items && page.items.length) {
        signalList.value = page.items
        return
      }
    }
    // 若无快照，通过 query 请求首屏或旧接口兜底
    const queryRes = await api.reviewQuery({ page_size: 100 })
    if (queryRes.items && queryRes.items.length) {
      signalList.value = queryRes.items
      currentSnapshotId.value = queryRes.snapshot_id
      return
    }
    // 旧接口兜底
    const legacyPage = await api.getReviewPage(100, 0, {})
    if (legacyPage.items) {
      signalList.value = legacyPage.items
    }
  } catch (err) {
    console.warn('加载复盘信号池失败:', err)
  } finally {
    poolLoading.value = false
  }
}

/** 切换到指定信号 */
async function selectSignal(sig: ReviewSummary | any) {
  if (!sig) return
  currentEventId.value = sig.event_id
  if (sig.result_version) currentVersion.value = sig.result_version

  // 若品种变动，重新拉取 K 线
  if (sig.symbol && sig.symbol !== currentSymbol.value) {
    currentSymbol.value = sig.symbol
    await loadKlines(sig.symbol)
  }

  await loadDetail(sig.event_id, sig.result_version)
}

/** 上一条 / 下一条信号快捷漫游 */
function navigateSignal(offset: number) {
  const nextIdx = currentIndex.value + offset
  if (nextIdx >= 0 && nextIdx < signalList.value.length) {
    void selectSignal(signalList.value[nextIdx])
  }
}

function handleKeydown(e: KeyboardEvent) {
  const target = e.target as HTMLElement | null
  if (target?.tagName === 'INPUT' || target?.tagName === 'TEXTAREA') return

  if (e.key === 'p' || e.key === 'P' || e.key === 'ArrowUp') {
    e.preventDefault()
    navigateSignal(-1)
  } else if (e.key === 'n' || e.key === 'N' || e.key === 'ArrowDown') {
    e.preventDefault()
    navigateSignal(1)
  } else if (e.key === 'Escape') {
    if (isTauri()) {
      void getCurrentWindow().close()
    }
  }
}

function focusCurrentSignal() {
  const ts = activeDetail.value?.summary?.entry_ts || activeDetail.value?.summary?.warning_ts || activeSignal.value?.entry_ts || activeSignal.value?.warning_ts
  if (ts && currentEventId.value) {
    focusTs.value = ts
    focusKey.value = currentEventId.value + Date.now()
    notify.success(`已居中聚焦 Event #${currentEventId.value}`)
  }
}

let unlisteners: (() => void)[] = []

onMounted(async () => {
  window.addEventListener('keydown', handleKeydown)

  // 1. 读取路由传参
  const symParam = (route.params.symbol as string) || (route.query.symbol as string) || 'FG0'
  const eventIdParam = route.query.eventId ? Number(route.query.eventId) : (route.query.review ? Number(route.query.review) : null)
  const verParam = (route.query.version as string) || ''
  const snapParam = (route.query.snapshotId as string) || ''

  currentSymbol.value = symParam
  currentEventId.value = eventIdParam
  currentVersion.value = verParam
  currentSnapshotId.value = snapParam

  // 并发加载 K 线、信号池和当前详情
  await Promise.all([
    loadKlines(symParam),
    loadSignalPool(snapParam),
    eventIdParam ? loadDetail(eventIdParam, verParam) : Promise.resolve(),
  ])

  // 2. 监听外部唤起通知（若独立窗口保持打开，其他地方点击时更新）
  if (isTauri()) {
    try {
      unlisteners.push(
        await listen<OpenReviewChartParams>('load-review-signal', async (e) => {
          const { symbol: newSym, eventId: newId, version: newVer, snapshotId: newSnap } = e.payload
          if (newSnap && newSnap !== currentSnapshotId.value) {
            currentSnapshotId.value = newSnap
            await loadSignalPool(newSnap)
          }
          currentEventId.value = newId
          if (newVer) currentVersion.value = newVer
          if (newSym && newSym !== currentSymbol.value) {
            currentSymbol.value = newSym
            await loadKlines(newSym)
          }
          await loadDetail(newId, newVer)
        }),
      )
    } catch {}
  }
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', handleKeydown)
  for (const fn of unlisteners) fn()
})
</script>

<template>
  <div class="review-chart-page">
    <!-- 1. 原生风格标题栏（支持整行拖拽、双击最大化） -->
    <TitleBar
      :title="`复盘打点K线 · [${currentSymbol}] ${activeSignal ? `#${activeSignal.event_id}` : ''}`"
      :show-window-controls="true"
    >
      <template #left>
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
      </template>

      <template #right>
        <n-button quaternary size="tiny" title="重新加载数据" @click="loadKlines(currentSymbol)">
          <template #icon>
            <n-icon :component="Refresh" />
          </template>
          刷新K线
        </n-button>
      </template>
    </TitleBar>

    <!-- 2. 两栏主体：左侧K线（无左侧品种树） + 右侧信号池 -->
    <div class="chart-content-row">
      <!-- K线主体 -->
      <section class="chart-viewport-box">
        <!-- 图表头部精简指标栏 -->
        <div class="chart-top-bar">
          <div class="ct-symbol-meta">
            <span class="ct-code">{{ currentSymbol }}</span>
            <span class="ct-name">{{ symbolInfo?.name || currentSymbol }}</span>
            <span class="ct-tf-badge">固定 15m 复盘</span>

            <template v-if="activeSignal">
              <span
                class="dir-badge"
                :class="activeSignal.direction === 'up' ? 'is-up' : 'is-down'"
              >
                <n-icon :component="activeSignal.direction === 'up' ? ArrowUp : ArrowDown" :size="10" />
                {{ activeSignal.direction === 'up' ? '做多' : '做空' }}
              </span>
              <span class="spec-tag">{{ activeSignal.level === 'fine' ? '精细' : activeSignal.level }}N · {{ activeSignal.grade }}</span>
              <span class="event-id-tag">#{{ activeSignal.event_id }}</span>
            </template>
          </div>

          <div class="ct-actions">
            <n-button size="tiny" secondary type="primary" @click="focusCurrentSignal">
              <template #icon>
                <n-icon :component="Focus2" />
              </template>
              居中聚焦形态打点
            </n-button>
          </div>
        </div>

        <!-- 纯净轻量 K 线核心图表组件 -->
        <div class="chart-canvas-wrap">
          <KLineChart
            :symbol="currentSymbol"
            timeframe="15m"
            :rows="klinesStore.rows"
            :signals="activePatternDto"
            :review-exit="reviewExit"
            :focus-ts="focusTs"
            :focus-key="focusKey"
            :loading="klinesStore.loading"
          />
        </div>
      </section>

      <!-- 右侧信号池（粗中有细） -->
      <aside class="signal-pool-sidebar">
        <ReviewSignalPool
          :signals="signalList"
          :selected-id="currentEventId"
          :detail="activeDetail"
          :detail-loading="detailLoading"
          :pool-loading="poolLoading"
          @select-signal="selectSignal"
        />
      </aside>
    </div>
  </div>
</template>

<style scoped>
.review-chart-page {
  display: flex;
  flex-direction: column;
  height: 100vh;
  width: 100vw;
  background: #f8fafc;
  color: #1e293b;
  overflow: hidden;
  box-sizing: border-box;
}

/* 顶部导航工具 */
.tb-signal-nav {
  display: flex;
  align-items: center;
  gap: 6px;
}
.tb-nav-counter {
  font-size: 11px;
  color: #64748b;
  padding: 0 4px;
}

/* 两栏主体布局 */
.chart-content-row {
  display: flex;
  flex: 1;
  min-height: 0;
  width: 100%;
  overflow: hidden;
}

/* 左侧K线视图容器（左侧无品种树） */
.chart-viewport-box {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
  height: 100%;
  background: #ffffff;
  overflow: hidden;
}

.chart-top-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 16px;
  background: #ffffff;
  border-bottom: 1px solid #e2e8f0;
  flex: none;
}
.ct-symbol-meta {
  display: flex;
  align-items: center;
  gap: 8px;
}
.ct-code {
  font-size: 14px;
  font-weight: 700;
  color: #0f172a;
  font-family: monospace;
}
.ct-name {
  font-size: 13px;
  color: #475569;
}
.ct-tf-badge {
  font-size: 10px;
  background: #eff6ff;
  color: #2563eb;
  padding: 1px 6px;
  border-radius: 4px;
  font-weight: 500;
}
.dir-badge {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  font-size: 10px;
  font-weight: 600;
  padding: 1px 6px;
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
  color: #64748b;
  padding: 1px 6px;
  border-radius: 3px;
}
.event-id-tag {
  font-size: 10px;
  color: #94a3b8;
  font-family: monospace;
}

.chart-canvas-wrap {
  flex: 1;
  min-height: 0;
  position: relative;
}

/* 右侧信号池容器 */
.signal-pool-sidebar {
  display: flex;
  height: 100%;
  flex: none;
}
</style>
