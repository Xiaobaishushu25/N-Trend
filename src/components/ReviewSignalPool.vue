<script setup lang="ts">
import { computed, ref, watch } from 'vue'
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
} from 'naive-ui'
import {
  ArrowDown,
  ArrowUp,
  Award,
  ChevronDown,
  ChevronUp,
  Filter,
  Search,
  Target,
} from '@vicons/tabler'
import type { ReviewResultDetail, ReviewSummary } from '../types/review'
import type { OutcomeDetail } from '../types'
import { qualityText } from '../utils/signalQuality'
import { fmtR } from '../stores/review'
import ReviewSignalDetailDrawer from './ReviewSignalDetailDrawer.vue'

const props = defineProps<{
  signals: (ReviewSummary | OutcomeDetail)[]
  selectedId: number | null
  detail: ReviewResultDetail | null
  detailLoading?: boolean
  poolLoading?: boolean
}>()

const emit = defineEmits<{
  (e: 'select-signal', signal: ReviewSummary | OutcomeDetail): void
}>()

const searchKw = ref('')
const outcomeFilter = ref<'all' | 'win' | 'loss' | 'open'>('all')
const expandedId = ref<number | null>(null)

// 自动跟随外部选中信号展开（如果用户想要自动展开详情，也可以点击按钮手动开关）
watch(
  () => props.selectedId,
  (newId) => {
    // 切换信号时不强行折叠，若当前已展开则切换到新ID
    if (expandedId.value != null && newId != null) {
      expandedId.value = newId
    }
  },
)

const filteredSignals = computed(() => {
  const kw = searchKw.value.trim().toUpperCase()
  return props.signals.filter((s) => {
    const sym = (s.symbol || '').toUpperCase()
    const id = String(s.event_id)
    if (kw && !sym.includes(kw) && !id.includes(kw)) return false

    const outcome = (s as any).exit_reason || (s as any).outcome
    const isWin = (s as any).net_r != null ? (s as any).net_r > 0 : outcome === 'win' || outcome === 'target'
    const isLoss = (s as any).net_r != null ? (s as any).net_r < 0 : outcome === 'loss' || outcome === 'stop'

    if (outcomeFilter.value === 'win' && !isWin) return false
    if (outcomeFilter.value === 'loss' && !isLoss) return false
    if (outcomeFilter.value === 'open' && outcome !== 'open' && s.state !== 'triggered') return false

    return true
  })
})

function getScore(s: ReviewSummary | OutcomeDetail): number | null {
  if ((s as any).scores?.quality_v2 != null) return (s as any).scores.quality_v2
  if ((s as any).review_scores?.quality_score != null) return (s as any).review_scores.quality_score
  if ((s as any).quality?.quality_score != null) return (s as any).quality.quality_score
  return (s as any).entry_score ?? null
}

function getMorphScore(s: ReviewSummary | OutcomeDetail): number | null {
  if ((s as any).scores?.morphology_v2 != null) return (s as any).scores.morphology_v2
  if ((s as any).review_scores?.morphology_score != null) return (s as any).review_scores.morphology_score
  if ((s as any).quality?.morphology_score != null) return (s as any).quality.morphology_score
  return null
}

function getNetR(s: ReviewSummary | OutcomeDetail): number | null {
  if ((s as any).net_r != null) return (s as any).net_r
  if ((s as any).r_multiple != null) return (s as any).r_multiple
  return null
}

function exitReasonLabel(reason: string | null | undefined): { text: string; type: 'default' | 'error' | 'success' | 'warning' | 'info' } {
  switch (reason) {
    case 'target':
      return { text: '🎯 达成止盈', type: 'error' }
    case 'stop':
      return { text: '🛑 触及止损', type: 'success' }
    case 'no_follow':
      return { text: '⚠️ 无跟随', type: 'warning' }
    case 'time_exit':
      return { text: '⏳ 时间退出', type: 'warning' }
    case 'rollover':
      return { text: '🔄 换月退出', type: 'info' }
    case 'win':
      return { text: '🎯 盈利', type: 'error' }
    case 'loss':
      return { text: '🛑 亏损', type: 'success' }
    case 'open':
      return { text: '持仓监控', type: 'info' }
    default:
      return { text: reason || '已结算', type: 'default' }
  }
}

function toggleDetail(eventId: number, e: MouseEvent) {
  e.stopPropagation()
  expandedId.value = expandedId.value === eventId ? null : eventId
}

function selectRow(s: ReviewSummary | OutcomeDetail) {
  emit('select-signal', s)
}
</script>

<template>
  <div class="signal-pool">
    <!-- 信号池顶部工具条 -->
    <div class="pool-header">
      <div class="ph-title-row">
        <div class="ph-title-box">
          <span class="ph-title">复盘信号池</span>
          <span class="ph-count">{{ filteredSignals.length }} / {{ signals.length }}</span>
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
          placeholder="搜索品种代码或 #编号..."
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
        <span>正在加载信号快照...</span>
      </div>

      <div v-else-if="!filteredSignals.length" class="empty-state">
        <n-empty description="当前条件下未找到信号" size="small" />
      </div>

      <n-scrollbar v-else style="height: 100%">
        <div class="cards-feed">
          <article
            v-for="s in filteredSignals"
            :key="s.event_id"
            class="pool-card"
            :class="[
              s.direction === 'up' ? 'is-up' : 'is-down',
              { 'is-active': s.event_id === selectedId },
            ]"
            @click="selectRow(s)"
          >
            <!-- 卡片顶行：品种、方向、等级、ID -->
            <div class="card-row-top">
              <div class="cr-symbol">
                <span class="sym-name">{{ s.symbol }}</span>
                <span class="sym-id">#{{ s.event_id }}</span>
              </div>
              <div class="cr-tags">
                <span
                  class="dir-badge"
                  :class="s.direction === 'up' ? 'is-up' : 'is-down'"
                >
                  <n-icon :component="s.direction === 'up' ? ArrowUp : ArrowDown" :size="10" />
                  {{ s.direction === 'up' ? '多' : '空' }}
                </span>
                <span class="level-badge">{{ s.level === 'fine' ? '精细' : s.level }}N · {{ s.grade }}</span>
              </div>
            </div>

            <!-- 卡片中间：核心评分与结局盈亏（“粗”） -->
            <div class="card-row-mid">
              <div class="score-col">
                <div class="primary-score font-num">
                  {{ qualityText(getScore(s)) }}
                  <span class="score-unit">分</span>
                </div>
                <div class="sub-scores font-num">
                  <span>形态 {{ qualityText(getMorphScore(s)) }}</span>
                </div>
              </div>

              <div class="outcome-col">
                <div class="outcome-pnl font-num" :class="(getNetR(s) ?? 0) >= 0 ? 'text-long' : 'text-short'">
                  {{ fmtR(getNetR(s)) }}
                </div>
                <n-tag
                  size="tiny"
                  :bordered="false"
                  :type="exitReasonLabel((s as any).exit_reason || (s as any).outcome).type"
                  class="outcome-tag"
                >
                  {{ exitReasonLabel((s as any).exit_reason || (s as any).outcome).text }}
                </n-tag>
              </div>
            </div>

            <!-- 卡片时间与持仓极值摘要 -->
            <div class="card-row-meta">
              <span class="meta-time font-num">
                {{ ((s as any).entry_ts || (s as any).trigger_ts || s.warning_ts || '').slice(5, 16) }}
              </span>
              <span v-if="(s as any).bars_held != null" class="meta-bars font-num">
                持仓 {{ (s as any).bars_held }} 根
              </span>
              <span v-if="(s as any).mfe_r != null" class="meta-excursion font-num">
                MFE {{ fmtR((s as any).mfe_r) }}
              </span>
            </div>

            <!-- 一键展开/收起细则按钮 -->
            <div class="card-row-actions">
              <button
                type="button"
                class="expand-btn"
                :class="{ 'is-expanded': expandedId === s.event_id }"
                @click="toggleDetail(s.event_id, $event)"
              >
                <span>{{ expandedId === s.event_id ? '收起评分细则与因果链' : '展开 A/B段评分细则' }}</span>
                <n-icon :component="expandedId === s.event_id ? ChevronUp : ChevronDown" :size="12" />
              </button>
            </div>

            <!-- “细”：按需展开的评分细则抽屉面板 -->
            <div v-if="expandedId === s.event_id" class="card-expanded-body" @click.stop>
              <ReviewSignalDetailDrawer
                :detail="s.event_id === selectedId ? detail : null"
                :summary="s"
                :loading="s.event_id === selectedId && detailLoading"
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
  width: 400px;
  background: #f8fafc;
  border-left: 1px solid #e2e8f0;
  box-sizing: border-box;
  flex: none;
}

/* 顶部工具条 */
.pool-header {
  padding: 10px 14px;
  background: #ffffff;
  border-bottom: 1px solid #e2e8f0;
  display: flex;
  flex-direction: column;
  gap: 8px;
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
  gap: 8px;
}
.ph-title {
  font-size: 13px;
  font-weight: 700;
  color: #0f172a;
}
.ph-count {
  font-size: 11px;
  color: #64748b;
  font-variant-numeric: tabular-nums;
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
  padding: 10px 12px;
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
  transition: all 0.15s ease;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.02);
}
.pool-card:hover {
  border-color: #cbd5e1;
  box-shadow: 0 2px 6px rgba(15, 23, 42, 0.06);
}
.pool-card.is-up {
  border-left-color: #ef4444;
}
.pool-card.is-down {
  border-left-color: #10b981;
}

/* 高亮选中态 */
.pool-card.is-active {
  border-color: #93c5fd;
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
  gap: 6px;
}
.sym-name {
  font-size: 13px;
  font-weight: 700;
  color: #0f172a;
}
.sym-id {
  font-size: 11px;
  color: #94a3b8;
  font-family: monospace;
}
.cr-tags {
  display: flex;
  align-items: center;
  gap: 6px;
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
.level-badge {
  font-size: 10px;
  color: #64748b;
  background: #f1f5f9;
  padding: 1px 5px;
  border-radius: 3px;
}

.card-row-mid {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 2px 0;
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
  color: #64748b;
}

.outcome-col {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
}
.outcome-pnl {
  font-size: 14px;
  font-weight: 700;
}
.text-long {
  color: #ef4444;
}
.text-short {
  color: #10b981;
}
.outcome-tag {
  font-size: 10px;
  padding: 0 4px;
}

.card-row-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 11px;
  color: #94a3b8;
  border-top: 1px dashed #f1f5f9;
  padding-top: 5px;
}
.meta-time {
  flex: 1;
}

.card-row-actions {
  display: flex;
  justify-content: flex-end;
  padding-top: 2px;
}
.expand-btn {
  border: none;
  background: transparent;
  color: #3b82f6;
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

/* 展开的细则面板容器 */
.card-expanded-body {
  margin-top: 8px;
  border-top: 1px solid #e2e8f0;
  padding-top: 8px;
  height: 480px;
  border-radius: 6px;
  overflow: hidden;
}
</style>
