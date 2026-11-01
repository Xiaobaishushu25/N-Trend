<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import {
  NButton,
  NEmpty,
  NIcon,
  NInput,
  NRadioGroup,
  NRadioButton,
  NScrollbar,
  NSpin,
  NTag,
  NTooltip,
} from 'naive-ui'
import {
  ArrowDown,
  ArrowUp,
  Award,
  ChevronDown,
  ChevronUp,
  Eye,
  EyeOff,
  Filter,
  Search,
  Target,
} from '@vicons/tabler'
import type { ReviewResultDetail, ReviewSummary, ReviewScoreMetric } from '../types/review'
import { publishedScore, reviewState, sameReview, excursionText } from '../utils/reviewPresentation'
import { reviewScoreStatus } from '../utils/reviewFilters'
import { reviewScoreOptions } from '../utils/reviewFilters'
import { qualityText } from '../utils/signalQuality'
import { fmtR } from '../stores/review'
import { useSymbolsStore } from '../stores/symbols'
import ReviewSignalDetailDrawer from './ReviewSignalDetailDrawer.vue'

const props = defineProps<{
  signals: ReviewSummary[]
  selectedId: number | null
  detail: ReviewResultDetail | null
  detailLoading?: boolean
  detailError?: string
  metric?: ReviewScoreMetric
  poolLoading?: boolean
  isSignalHidden?: boolean
}>()

const emit = defineEmits<{
  (e: 'select-signal', signal: ReviewSummary): void
  (e: 'toggle-visibility'): void
  (e: 'retry'): void
}>()

const symbolsStore = useSymbolsStore()
const searchKw = ref('')
const outcomeFilter = ref<'all' | 'win' | 'loss' | 'open'>('all')
const expandedId = ref<number | null>(null)
const scrollbarRef = ref<any>(null)

// 自动滚动定位当前选中的卡片
const cardElements = new Map<number, HTMLElement>()
function setCardRef(id: number, el: any) {
  if (el) {
    cardElements.set(id, el.$el || el)
    // 刚刚挂载的卡片正好是选中的信号时，立即触发聚焦滚动
    if (id === props.selectedId) {
      scrollToActive('smooth')
    }
  } else {
    cardElements.delete(id)
  }
}

function scrollToActive(behavior: ScrollBehavior = 'smooth') {
  if (props.selectedId == null) return
  const id = props.selectedId

  const doScroll = () => {
    if (props.selectedId !== id) return true
    const el = cardElements.get(id)
    if (!el) return false

    // 优先调用 Naive UI NScrollbar 组件的 scrollTo 方法以更新虚拟滚动条
    if (scrollbarRef.value?.scrollTo) {
      try {
        scrollbarRef.value.scrollTo({ el, behavior, debounce: false })
      } catch {
        el.scrollIntoView({ block: 'nearest', behavior })
      }
    } else {
      el.scrollIntoView({ block: 'nearest', behavior })
    }
    return true
  }

  // 1. 尝试立即滚动
  if (!doScroll()) {
    // 2. DOM 渲染后尝试
    nextTick(() => {
      if (!doScroll()) {
        // 3. 延时重试，应对跨窗口时序和布局排版延迟
        setTimeout(doScroll, 60)
      }
    })
  } else {
    // 容器可能在微任务中发生布局重排，多重保险
    setTimeout(doScroll, 60)
  }
}

watch(
  () => props.selectedId,
  (newId) => {
    if (newId != null) {
      // 若当前关键字或筛选状态隐藏了选中的信号，自动重置以便该信号卡片可见
      const target = props.signals.find((s) => s.event_id === newId)
      if (target) {
        if (searchKw.value) {
          const kw = searchKw.value.trim().toUpperCase()
          const sym = (target.symbol || '').toUpperCase()
          const idStr = String(target.event_id)
          if (!sym.includes(kw) && !idStr.includes(kw)) {
            searchKw.value = ''
          }
        }
      }
    }
    if (expandedId.value != null && newId != null) {
      expandedId.value = newId
    }
    scrollToActive('smooth')
  },
  { immediate: true },
)

watch(
  () => props.signals,
  () => {
    scrollToActive('smooth')
  },
  { deep: false },
)

const filteredSignals = computed(() => {
  const kw = searchKw.value.trim().toUpperCase()
  return props.signals.filter((s) => {
    const sym = (s.symbol || '').toUpperCase()
    const name = (symbolsStore.symbols.find((x) => x.code === s.symbol)?.name || '').toUpperCase()
    const id = String(s.event_id)
    if (kw && !sym.includes(kw) && !name.includes(kw) && !id.includes(kw)) return false

    const netR = s.state === 'closed' ? s.net_r : null
    if (outcomeFilter.value === 'win' && !(netR != null && netR > 0)) return false
    if (outcomeFilter.value === 'loss' && !(netR != null && netR < 0)) return false
    if (outcomeFilter.value === 'open' && s.state !== 'open') return false

    return true
  })
})

watch(
  () => [searchKw.value, outcomeFilter.value, filteredSignals.value.length],
  () => {
    nextTick(() => scrollToActive('auto'))
  },
)

function getSymbolName(code: string): string {
  return symbolsStore.symbols.find((s) => s.code === code)?.name || code
}

const metricLabel = computed(() => reviewScoreOptions.find(o => o.value === props.metric)?.label || 'v2 综合 · 候选')
function getScore(s: ReviewSummary) { return publishedScore(s, props.metric || 'quality_v2') }
function getMorphScore(s: ReviewSummary) { return publishedScore(s, 'morphology_v2') }
function getNetR(s: ReviewSummary) { return s.net_r }

function fmtPrice(val: number | null | undefined): string {
  if (val == null || Number.isNaN(val)) return '—'
  return Number(val).toFixed(1)
}

function toggleDetail(signal: ReviewSummary, e: MouseEvent) {
  e.stopPropagation()
  if (expandedId.value === signal.event_id) { expandedId.value = null; return }
  expandedId.value = signal.event_id
  if (props.selectedId !== signal.event_id || !sameReview(signal, props.detail)) emit('select-signal', signal)
}

function selectRow(s: ReviewSummary) {
  emit('select-signal', s)
}

function onEyeClick(e: MouseEvent) {
  e.stopPropagation()
  emit('toggle-visibility')
}
</script>

<template>
  <div class="signal-pool">
    <!-- 信号池顶部工具条 -->
    <div class="pool-header">
      <div class="ph-title-row">
        <div class="ph-title-box">
          <span class="ph-title">复盘信号池</span>
          <span class="ph-count font-num">{{ filteredSignals.length }} / {{ signals.length }}</span>
        </div>
        <n-radio-group v-model:value="outcomeFilter" size="small" class="outcome-tabs">
          <n-radio-button value="all">全部</n-radio-button>
          <n-radio-button value="win">盈利</n-radio-button>
          <n-radio-button value="loss">亏损</n-radio-button>
        </n-radio-group>
      </div>

      <div class="ph-search-row">
        <n-input
          v-model:value="searchKw"
          placeholder="按品种代码、中文名或 #ID 筛选..."
          size="tiny"
          clearable
          class="search-input"
        >
          <template #prefix>
            <n-icon :component="Search" :size="12" />
          </template>
        </n-input>
      </div>
    </div>

    <!-- 信号卡片列表 -->
    <div class="pool-list-wrap">
      <div v-if="poolLoading" class="loading-state">
        <n-spin size="small" />
        <span>正在加载复盘信号池...</span>
      </div>

      <div v-else-if="!filteredSignals.length" class="empty-state">
        <n-empty description="当前条件下未找到信号" size="small" />
      </div>

      <n-scrollbar v-else ref="scrollbarRef" style="height: 100%">
        <div class="cards-feed">
          <article
            v-for="s in filteredSignals"
            :key="s.event_id"
            :ref="(el) => setCardRef(s.event_id, el)"
            class="pool-card"
            :class="[
              s.direction === 'up' ? 'is-up' : 'is-down',
              { 'is-active': s.event_id === selectedId },
            ]"
            @click="selectRow(s)"
          >
            <!-- 卡片顶行：品种名称+代码、方向、等级、ID、眼睛展示切换 -->
            <div class="card-row-top">
              <div class="cr-symbol">
                <span class="sym-name font-bold">{{ getSymbolName(s.symbol) }}</span>
                <span class="sym-code font-num">{{ s.symbol }}</span>
                <span class="sym-id font-num">#{{ s.event_id }}</span>
              </div>
              <div class="cr-tags">
                <span
                  class="dir-badge"
                  :class="s.direction === 'up' ? 'is-up' : 'is-down'"
                >
                  <n-icon :component="s.direction === 'up' ? ArrowUp : ArrowDown" :size="10" />
                  {{ s.direction === 'up' ? '做多' : '做空' }}
                </span>
                <span class="level-badge">{{ s.level === 'fine' ? '精细' : s.level }}N · {{ s.grade }}</span>

                <!-- K线图形态可见性开关（仅当前选中的信号上提供或常驻） -->
                <button
                  v-if="s.event_id === selectedId"
                  type="button"
                  class="eye-btn"
                  :class="{ 'is-hidden-eye': isSignalHidden }"
                  :title="isSignalHidden ? '点击在K线图上显示该形态打点' : '点击在K线图上隐藏该形态打点'"
                  @click="onEyeClick"
                >
                  <n-icon :component="isSignalHidden ? EyeOff : Eye" :size="14" />
                </button>
              </div>
            </div>

            <!-- 卡片中间：核心评分与结局盈亏（“粗”） -->
            <div class="card-row-mid">
              <div class="score-col">
                <div class="primary-score font-num">
                  {{ qualityText(getScore(s)) }}
                  <span class="score-unit">{{ metricLabel }} /{{ metric === 'old' ? 5 : 100 }}</span>
                </div>
                <div class="sub-scores font-num">
                  <span>v2 形态 {{ qualityText(getMorphScore(s)) }}</span><span v-if="getScore(s) == null"> · {{ reviewScoreStatus(s.score_status) }}</span>
                  <span v-if="s.plan.rr != null" class="rr-text">· 空间 {{ Number(s.plan.rr).toFixed(1) }}R</span>
                </div>
              </div>

              <div class="outcome-col">
                <div
                  v-if="getNetR(s) != null"
                  class="outcome-pnl font-num"
                  :class="(getNetR(s) ?? 0) >= 0 ? 'text-long' : 'text-short'"
                >
                  {{ fmtR(getNetR(s)) }}
                </div>
                <n-tag
                  size="tiny"
                  :bordered="false"
                  :type="reviewState(s).type"
                  class="outcome-tag"
                >
                  {{ reviewState(s).text }}
                </n-tag>
              </div>
            </div>

            <!-- 关键价位阶梯：入场 -> 止损 -> 目标 与 风险点数 -->
            <div class="card-row-prices font-num">
              <span class="p-step">入 <b>{{ fmtPrice(s.plan.entry) }}</b></span>
              <span class="p-arrow">→</span>
              <span class="p-step p-stop">止 <b>{{ fmtPrice(s.plan.stop) }}</b></span>
              <span class="p-arrow">→</span>
              <span class="p-step p-target">参考目标 <b>{{ fmtPrice(s.plan.reference_target) }}</b></span>
              <span v-if="s.plan.risk" class="p-risk text-muted">
                (风险 {{ Number(s.plan.risk).toFixed(1) }}点)
              </span>
            </div>

            <!-- 卡片时间、量能与持仓极值摘要 -->
            <div class="card-row-meta">
              <span class="meta-time font-num">
                {{ (s.entry_ts || s.warning_ts || '').slice(5, 16) }}
              </span>
              <span v-if="s.diagnostics.vol_ratio != null" class="meta-vol font-num">
                量比 {{ Number(s.diagnostics.vol_ratio).toFixed(1) }}x（5m）
              </span>
              <span v-if="s.bars_held != null" class="meta-bars font-num">
                持仓 {{ s.bars_held }}根（15m）
              </span>
              <span class="meta-excursion font-num text-long">
                MFE {{ excursionText(s.mfe, s.mfe_r) }}
              </span>
              <span class="meta-excursion font-num text-short">
                MAE {{ excursionText(s.mae, s.mae_r) }}
              </span>
            </div>

            <!-- 一键展开/收起平仓流水与细则按钮 -->
            <div class="card-row-actions">
              <button
                type="button"
                class="expand-btn"
                :class="{ 'is-expanded': expandedId === s.event_id }"
                @click="toggleDetail(s, $event)"
              >
                <span>{{ expandedId === s.event_id ? '收起平仓流水与细则' : '展开平仓流水与 A/B段细则' }}</span>
                <n-icon :component="expandedId === s.event_id ? ChevronUp : ChevronDown" :size="12" />
              </button>
            </div>

            <!-- “细”：按需展开的评分细则抽屉面板 -->
            <div v-if="expandedId === s.event_id" class="card-expanded-body" @click.stop>
              <ReviewSignalDetailDrawer
                :detail="s.event_id === selectedId && sameReview(s, detail) ? detail : null"
                :summary="s"
                :loading="s.event_id === selectedId && detailLoading"
                :error="s.event_id === selectedId ? detailError : undefined"
                @retry="emit('retry')"
                @close="expandedId = null"
              />
            </div>
          </article>
        </div>
      </n-scrollbar>
    </div>
  </div>
</template>

<style scoped>
.signal-pool {
  display: flex;
  flex-direction: column;
  height: 100%;
  width: 100%;
  background: #f8fafc;
  box-sizing: border-box;
}

/* 顶部工具条 */
.pool-header {
  padding: 8px 12px;
  background: #ffffff;
  border-bottom: 1px solid #e2e8f0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  flex: none;
}
.ph-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.ph-title-box {
  display: flex;
  align-items: baseline;
  gap: 6px;
}
.ph-title {
  font-size: 13px;
  font-weight: 700;
  color: #0f172a;
}
.ph-count {
  font-size: 11px;
  color: #64748b;
  background: #f1f5f9;
  padding: 1px 6px;
  border-radius: 4px;
}
.outcome-tabs {
  background: #f1f5f9;
  border-radius: 4px;
  padding: 2px;
}

.search-input {
  width: 100%;
}

/* 列表主容器 */
.pool-list-wrap {
  flex: 1;
  min-height: 0;
  position: relative;
}

.loading-state,
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 40px 16px;
  gap: 8px;
  font-size: 12px;
  color: #64748b;
}

.cards-feed {
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

/* 紧凑卡片（“粗”） */
.pool-card {
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-left-width: 4px;
  border-radius: 8px;
  padding: 10px 12px 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  cursor: pointer;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.02);
}
.pool-card:hover {
  border-color: #cbd5e1;
  box-shadow: 0 2px 6px rgba(15, 23, 42, 0.06);
}
.pool-card.is-up {
  border-left-color: #dc2626;
}
.pool-card.is-down {
  border-left-color: #16a34a;
}

/* 高亮选中态 */
.pool-card.is-active {
  border-color: #3b82f6;
  border-left-color: #2563eb;
  background: #f8fbff;
  box-shadow: 0 2px 8px rgba(37, 99, 235, 0.12);
}

.card-row-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.cr-symbol {
  display: flex;
  align-items: baseline;
  gap: 5px;
}
.sym-name {
  font-size: 13px;
  color: #0f172a;
}
.sym-code {
  font-size: 11px;
  color: #475569;
}
.sym-id {
  font-size: 11px;
  color: #94a3b8;
}
.cr-tags {
  display: flex;
  align-items: center;
  gap: 5px;
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
  color: #dc2626;
}
.dir-badge.is-down {
  background: #ecfdf5;
  color: #16a34a;
}
.level-badge {
  font-size: 10px;
  color: #475569;
  background: #f1f5f9;
  padding: 1px 5px;
  border-radius: 3px;
}

/* 眼睛图表显示切换按钮 */
.eye-btn {
  border: none;
  background: transparent;
  color: #2563eb;
  padding: 2px;
  border-radius: 3px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.eye-btn:hover {
  background: #e0e7ff;
}
.eye-btn.is-hidden-eye {
  color: #94a3b8;
}

.card-row-mid {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 1px 0;
}
.score-col {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.primary-score {
  font-size: 18px;
  font-weight: 700;
  color: #2563eb;
}
.score-unit {
  font-size: 10px;
  font-weight: normal;
  color: #94a3b8;
}
.sub-scores {
  font-size: 11px;
  color: #475569;
}
.rr-text {
  color: #64748b;
  margin-left: 2px;
}

.outcome-col {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
}
.outcome-pnl {
  font-size: 13px;
  font-weight: 700;
}
.text-long {
  color: #dc2626;
}
.text-short {
  color: #16a34a;
}
.outcome-tag {
  font-size: 10px;
  padding: 0 4px;
}

/* 关键价格链 */
.card-row-prices {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  color: #475569;
  background: #f8fafc;
  padding: 3px 6px;
  border-radius: 4px;
}
.p-step {
  display: inline-flex;
  gap: 2px;
}
.p-step b {
  color: #1e293b;
  font-weight: 600;
}
.p-stop b {
  color: #dc2626;
}
.p-target b {
  color: #16a34a;
}
.p-arrow {
  color: #cbd5e1;
  font-size: 10px;
}
.p-risk {
  font-size: 10px;
  margin-left: auto;
}

.card-row-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  color: #64748b;
  border-top: 1px dashed #f1f5f9;
  padding-top: 4px;
}
.meta-time {
  flex: 1;
  color: #64748b;
}

.card-row-actions {
  display: flex;
  justify-content: flex-end;
  padding-top: 2px;
}
.expand-btn {
  border: none;
  background: transparent;
  color: #2563eb;
  font-size: 11px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 4px;
  border-radius: 4px;
}
.expand-btn:hover {
  background: #eff6ff;
}
.expand-btn.is-expanded {
  color: #1d4ed8;
  font-weight: 600;
}

/* 展开的细则面板容器：不再使用死高截断，支持优雅展开和内部滚动 */
.card-expanded-body {
  margin-top: 6px;
  border-top: 1px solid #e2e8f0;
  padding-top: 6px;
  max-height: 580px;
  display: flex;
  flex-direction: column;
  border-radius: 6px;
  overflow: hidden;
}

/* 现代清晰表格字体 */
.font-num {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", "PingFang SC", "Microsoft YaHei", sans-serif;
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
