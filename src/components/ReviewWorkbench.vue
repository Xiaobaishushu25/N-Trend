<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  NButton,
  NDataTable,
  NEmpty,
  NIcon,
  NInput,
  NPagination,
  NSelect,
  NSpace,
  NTag,
  NText,
  NTooltip,
  type DataTableColumns,
} from 'naive-ui'
import { AlertTriangle, ArrowDown, ArrowUp, Refresh, Search } from '@vicons/tabler'
import { api } from '../services/api'
import { fmtR } from '../stores/review'
import { usePublishedReviewStore } from '../stores/publishedReview'
import type { RecentOutcomeFilters, SymbolRow } from '../types'
import type { ReviewSummary } from '../types/review'
import { qualityText } from '../utils/signalQuality'
import { reviewScoreOptions } from '../utils/reviewFilters'
import { publishedScore as reviewScore, reviewState, excursionText } from '../utils/reviewPresentation'
import { notify } from '../utils/notify'
import { openReviewChartWindow } from '../utils/openReviewChartWindow'
import { isRebuildingAllSignals, runRebuildAllSignals } from '../utils/reviewRebuild'
import PublishedReviewDetail from './PublishedReviewDetail.vue'

const review = usePublishedReviewStore()
const names = ref<SymbolRow[]>([])
const expanded = ref<number[]>([])
const symbolInput = ref('')

const dirLabel = (d: string) => (d === 'up' ? '做多' : d === 'down' ? '做空' : d)
const levelLabel = (l: string) =>
  l === 'fine' ? '精细' : l === 'large' ? '较大' : l === 'box' ? '箱体' : l

const exitLabels: Record<string, string> = {
  stop: '止损',
  target: '止盈',
  target_1r: '止盈 1R',
  no_follow: '无跟随',
  time_exit: '时间退出',
  rollover: '换月',
  no_trigger: '未触发',
  filtered: '已过滤',
  ambiguous: '歧义K',
  unfilled: '未成交',
  unfinished: '持仓中',
}

const directionOptions = [
  { label: '做多', value: 'up' },
  { label: '做空', value: 'down' },
]

const levelOptions = [
  { label: '精细', value: 'fine' },
  { label: '较大', value: 'large' },
  { label: '箱体', value: 'box' },
]

const gradeOptions = [
  { label: 'A级', value: 'A级' },
  { label: 'B级', value: 'B级' },
  { label: 'C级', value: 'C级' },
  { label: '回撤过浅', value: '回撤过浅' },
  { label: '回撤过深', value: '回撤过深' },
]

const outcomeOptions = [
  { label: '盈利', value: 'win' },
  { label: '亏损', value: 'loss' },
  { label: '持仓中', value: 'open' },
  { label: '未触发', value: 'no_trigger' },
  { label: '数据不足', value: 'missing_data' },
  { label: '等待触发', value: 'pending' },
  { label: '已过滤', value: 'filtered' },
  { label: '同根歧义', value: 'ambiguous' },
  { label: '缺交易日历', value: 'missing_calendar' },
  { label: '缺合约参数', value: 'missing_parameters' },
  { label: '换月', value: 'rollover' },
]

const emptyMessage = computed(() => {
  if (review.browserError) return review.browserError
  const meta = review.metadata
  if (!meta || meta.coverage.published_signals > 0) return '当前筛选条件下暂无已发布信号'
  const job = meta.jobs.find(j => j.status === 'running' || j.status === 'queued')
  if (job) return `复盘计算中：${job.completed_symbols}/${job.total_symbols} 个品种`
  if (meta.jobs[0]?.status === 'failed') return `复盘任务失败：${meta.jobs[0].errors.join('；')}`
  return '尚无已发布复盘结果，请检查服务端后台任务'
})

const quickScoreCuts = computed(() => {
  return review.browserFilters.qualityMetric === 'old'
    ? [2.5, 3.0, 3.5, 4.0]
    : [60, 70, 80, 90]
})

const rColor = (v: number | null | undefined) => (v == null || v >= 0 ? '#e03131' : '#0f9d58')

const metricLabel = computed(
  () => reviewScoreOptions.find((o) => o.value === review.browserFilters.qualityMetric)?.label || 'v2 综合 · 候选',
)

function scoreCell(value: number | null, strong = false) {
  return h(
    'span',
    {
      class: [
        'numeric-score font-num',
        strong && 'primary-score',
        value == null && 'missing-score',
      ],
      style: strong && value != null && value >= 70 ? 'color: #3b82f6; font-weight: 700;' : undefined,
    },
    qualityText(value),
  )
}

function handleSortByMetric() {
  const current = review.browserFilters.sortBy
  const next = current === 'score_desc' ? 'score_asc' : 'score_desc'
  change({ sortBy: next })
}

function handleSortByTime() {
  const current = review.browserFilters.sortBy
  const next = current === 'time_desc' ? 'time_asc' : 'time_desc'
  change({ sortBy: next })
}

const columns = computed<DataTableColumns<ReviewSummary>>(() => [
  {
    type: 'expand',
    width: 36,
    renderExpand: (row) => h(PublishedReviewDetail, { row }),
  },
  {
    title: 'ID',
    key: 'event_id',
    width: 76,
    render: (r) => {
      const isLong = r.event_id > 99999999
      if (!isLong) {
        return h('span', { class: 'font-num text-muted', style: 'white-space: nowrap' }, `#${r.event_id}`)
      }
      const shortStr = `#${String(r.event_id).slice(0, 4)}…${String(r.event_id).slice(-3)}`
      return h(
        NTooltip,
        { trigger: 'hover' },
        {
          trigger: () =>
            h(
              'span',
              { class: 'font-num text-muted', style: 'white-space: nowrap; cursor: help' },
              shortStr,
            ),
          default: () => `完整事件编号: #${r.event_id} (历史回放重构)`,
        },
      )
    },
  },
  {
    title: '品种 / 名称',
    key: 'symbol',
    width: 140,
    fixed: 'left',
    render: (row) => {
      const symName = names.value.find((s) => s.code === row.symbol)?.name || row.symbol
      return h('div', { class: 'symbol-cell' }, [
        h('span', { class: 'sym-code font-bold font-num' }, row.symbol),
        h('span', { class: 'sym-name text-muted' }, symName),
      ])
    },
  },
  {
    title: '方向',
    key: 'direction',
    width: 72,
    render: (r) =>
      h(
        'span',
        {
          class: ['dir-pill', r.direction === 'up' ? 'dir-up' : 'dir-down'],
        },
        dirLabel(r.direction),
      ),
  },
  {
    title: '级别',
    key: 'level',
    width: 68,
    render: (r) => levelLabel(r.level),
  },
  {
    title: '等级',
    key: 'grade',
    width: 82,
    render: (r) => h('span', { class: 'grade-pill' }, r.grade),
  },
  {
    title: () =>
      h(
        'div',
        {
          class: 'sortable-th-header',
          onClick: handleSortByMetric,
          title: '点击切换评分排序',
        },
        [
          h('span', metricLabel.value),
          h('span', { class: 'sort-indicator' }, review.browserFilters.sortBy === 'score_desc' ? '↓' : review.browserFilters.sortBy === 'score_asc' ? '↑' : '↕'),
        ],
      ),
    key: 'selected_score',
    width: 130,
    align: 'right',
    render: (row) => scoreCell(reviewScore(row, review.browserFilters.qualityMetric), true),
  },
  {
    title: 'v2形态',
    key: 'morphology_v2',
    width: 88,
    align: 'right',
    render: (row) => scoreCell(reviewScore(row, 'morphology_v2')),
  },
  {
    title: 'v1形态',
    key: 'morphology_v1',
    width: 88,
    align: 'right',
    render: (row) => scoreCell(reviewScore(row, 'morphology_v1')),
  },
  {
    title: '计划入场',
    key: 'entry',
    width: 88,
    align: 'right',
    render: (r) => h('span', { class: 'font-num' }, r.plan.entry?.toFixed(1) ?? '—'),
  },
  {
    title: '止损价',
    key: 'stop',
    width: 88,
    align: 'right',
    render: (r) => h('span', { class: 'font-num text-danger' }, r.plan.stop?.toFixed(1) ?? '—'),
  },
  {
    title: '原始参考目标',
    key: 'target',
    width: 88,
    align: 'right',
    render: (r) => h('span', { class: 'font-num text-success' }, r.plan.reference_target?.toFixed(1) ?? '—'),
  },
  {
    title: () =>
      h(
        'div',
        {
          class: 'sortable-th-header',
          onClick: handleSortByTime,
          title: '点击切换时间排序',
        },
        [
          h('span', review.browserFilters.timeField === 'trigger' ? '触发时间' : '预警时间'),
          h('span', { class: 'sort-indicator' }, review.browserFilters.sortBy === 'time_desc' ? '↓' : review.browserFilters.sortBy === 'time_asc' ? '↑' : '↕'),
        ],
      ),
    key: 'time',
    width: 155,
    render: (row) =>
      h('div', { class: 'signal-time font-num' }, [
        h('span', (review.browserFilters.timeField === 'trigger' ? row.entry_ts : row.warning_ts) || '未触发'),
      ]),
  },
  {
    title: '结局',
    key: 'outcome',
    width: 84,
    render: (row) => {
      const meta = reviewState(row)
      return h(NTag, { size: 'small', bordered: false, type: meta.type }, { default: () => meta.text })
    },
  },
  {
    title: '出场原因',
    key: 'exit_reason',
    width: 90,
    render: (r) => r.exit_reason ? exitLabels[r.exit_reason] ?? r.exit_reason : '—',
  },
  {
    title: '出场价',
    key: 'exit_price',
    width: 85,
    align: 'right',
    render: (r) => h('span', { class: 'font-num' }, r.exit_price == null ? '—' : r.exit_price.toFixed(1)),
  },
  {
    title: '净R',
    key: 'net_r',
    width: 90,
    align: 'right',
    render: (row) => {
      const r = row.net_r
      if (r == null) return h(NText, { depth: 3 }, { default: () => '—' })
      return h(
        'span',
        { style: `color:${rColor(r)};font-weight:700`, class: 'font-num' },
        fmtR(r),
      )
    },
  },
  {
    title: 'MFE',
    key: 'mfe_r',
    width: 80,
    align: 'right',
    render: (r) => excursionText(r.mfe, r.mfe_r),
  },
  {
    title: 'MAE',
    key: 'mae_r',
    width: 80,
    align: 'right',
    render: (r) => excursionText(r.mae, r.mae_r),
  },
  {
    title: '持仓K（15m）',
    key: 'bars_held',
    width: 75,
    align: 'right',
    render: (r) => h('span', { class: 'font-num' }, r.bars_held ?? '—'),
  },
  {
    title: '独立复盘操作',
    key: 'action',
    width: 145,
    fixed: 'right',
    render: (row) =>
      h('div', { class: 'row-actions' }, [
        h(
          NButton,
          {
            size: 'tiny',
            quaternary: true,
            onClick: (event: MouseEvent) => {
              event.stopPropagation()
              expanded.value = expanded.value.includes(row.event_id)
                ? expanded.value.filter((id) => id !== row.event_id)
                : [...expanded.value, row.event_id]
            },
          },
          { default: () => (expanded.value.includes(row.event_id) ? '收起' : '分项') },
        ),
        h(
          NButton,
          {
            size: 'tiny',
            secondary: true,
            type: 'primary',
            onClick: (event: MouseEvent) => {
              event.stopPropagation()
              void openChart(row)
            },
          },
          { default: () => '独立K线 →' },
        ),
      ]),
  },
])

let timer: ReturnType<typeof setTimeout> | undefined

function change(patch: Partial<RecentOutcomeFilters>) {
  review.updateBrowserFilterDraft(patch)
  expanded.value = []
  if (timer) clearTimeout(timer)
  timer = setTimeout(() => {
    void review.loadReviewPage()
  }, 250)
}

function onSymbolInput() {
  const sym = symbolInput.value.trim().toUpperCase()
  change({ symbol: sym, symbols: sym ? [sym] : [] })
}

function selectScoreCut(cut: number | null) {
  if (cut == null) {
    change({ qualityMin: null, qualityMax: null, qualityStatus: 'all' })
  } else {
    change({ qualityMin: cut, qualityMax: null, qualityStatus: 'scored', sortBy: 'score_desc' })
  }
}

async function reset() {
  if (timer) clearTimeout(timer)
  symbolInput.value = ''
  expanded.value = []
  await review.resetBrowserFilters()
}

async function page(value: number) {
  if (timer) clearTimeout(timer)
  expanded.value = []
  review.browserPage = value
  await review.loadReviewPage(true)
}

async function size(value: number) {
  review.browserPageSize = value
  await page(1)
}

async function openChart(row: ReviewSummary) {
  try {
    await openReviewChartWindow({
      symbol: row.symbol,
      eventId: row.event_id,
      version: row.result_version,
      snapshotId: review.currentSnapshotId,
      query: review.query || undefined,
      cursor: (review.browserPage - 1) * review.browserPageSize,
      metric: review.browserFilters.qualityMetric || 'quality_v2',
    })
  } catch (error) {
    notify.error(`打开独立复盘图表失败：${error}`)
  }
}

function rowProps(row: ReviewSummary) {
  return {
    style: 'cursor: pointer',
    onClick: (e: MouseEvent) => {
      // 关键修复：点击展开三角、按钮或链接时不触发跳转独立窗口
      const target = e.target as HTMLElement | null
      if (target?.closest('.n-data-table-expand-trigger, button, a, .n-button, .n-tag, .n-base-icon')) {
        return
      }
      void openChart(row)
    },
  }
}

onMounted(() => {
  void review.loadReviewPage()
  void api
    .getSymbols()
    .then((value) => {
      names.value = value
    })
    .catch(() => {})
})

const rebuilding = ref(false)
async function handleRetryRebuild() {
  rebuilding.value = true
  try {
    await api.reviewRebuild({})
    notify.success('已触发复盘任务重新计算')
    setTimeout(() => {
      void review.loadReviewPage()
    }, 1200)
  } catch (e: any) {
    notify.error(`重试复盘任务失败: ${e?.message || e}`)
  } finally {
    rebuilding.value = false
  }
}

onBeforeUnmount(() => {
  if (timer) clearTimeout(timer)
})
</script>

<template>
  <section class="workbench">
    <!-- 1. 表头一体化筛选控制条（对齐 main 分支专业工作台体验） -->
    <div class="workbench-header-card">
      <div class="filter-bar">
        <n-input
          v-model:value="symbolInput"
          size="small"
          clearable
          placeholder="品种代码，如 RB"
          style="width: 145px"
          @update:value="onSymbolInput"
        >
          <template #prefix>
            <n-icon :component="Search" :size="13" color="#94a3b8" />
          </template>
        </n-input>

        <n-select
          :value="review.browserFilters.direction"
          size="small"
          clearable
          placeholder="方向"
          style="width: 95px"
          :options="directionOptions"
          @update:value="(val) => change({ direction: val })"
        />

        <n-select
          :value="review.browserFilters.level"
          size="small"
          clearable
          placeholder="级别"
          style="width: 95px"
          :options="levelOptions"
          @update:value="(val) => change({ level: val })"
        />

        <n-select
          :value="review.browserFilters.grade"
          size="small"
          clearable
          placeholder="等级"
          style="width: 105px"
          :options="gradeOptions"
          @update:value="(val) => change({ grade: val })"
        />

        <n-select
          :value="review.browserFilters.outcome"
          size="small"
          clearable
          placeholder="结局"
          style="width: 105px"
          :options="outcomeOptions"
          @update:value="(val) => change({ outcome: val })"
        />

        <n-select
          :value="review.browserFilters.qualityMetric || 'quality_v2'"
          size="small"
          style="width: 135px"
          :options="reviewScoreOptions"
          @update:value="(val) => change({ qualityMetric: val, qualityMin: null, qualityMax: null })"
        />

        <!-- 快速评分分桶快捷钮 -->
        <div class="score-cuts-group">
          <span class="cuts-label">评分下限:</span>
          <n-button
            size="tiny"
            secondary
            :type="review.browserFilters.qualityMin == null ? 'primary' : 'default'"
            @click="selectScoreCut(null)"
          >
            不限
          </n-button>
          <n-button
            v-for="cut in quickScoreCuts"
            :key="cut"
            size="tiny"
            secondary
            :type="review.browserFilters.qualityMin === cut ? 'primary' : 'default'"
            @click="selectScoreCut(cut)"
          >
            ≥{{ cut }}
          </n-button>
        </div>

        <div class="filter-actions">
          <n-tooltip trigger="hover">
            <template #trigger>
              <n-button size="small" secondary @click="reset">重置</n-button>
            </template>
            重置所有筛选条件到默认值
          </n-tooltip>
          <n-tooltip trigger="hover">
            <template #trigger>
              <n-button
                size="small"
                secondary
                type="primary"
                :loading="review.browserLoading"
                @click="review.loadReviewPage()"
              >
                <template #icon>
                  <n-icon :component="Refresh" />
                </template>
                刷新列表
              </n-button>
            </template>
            根据当前筛选条件重新查询并刷新信号列表
          </n-tooltip>
        </div>
      </div>
    </div>

    <!-- 2. 数据表格与分页结果面板 -->
    <div class="results-panel">
      <div class="results-header">
        <div class="rh-left">
          <strong>复盘信号清单</strong>
          <span class="count font-num">{{ review.browserLoading ? '…' : review.browserTotal.toLocaleString() }} 笔</span>
          <span v-if="!review.browserLoading" class="coverage">
            已评分 <b class="font-num">{{ review.browserScored }}</b> <i>·</i> 待评分 <b class="font-num">{{ review.browserUnscored }}</b>
          </span>
        </div>

        <div class="rh-right">
          <span class="tip-text">展开左侧箭头查看评分证据，点击任意行弹出独立K线窗口复盘</span>
          <n-button
            v-if="review.browserRows.length"
            size="tiny"
            secondary
            type="primary"
            @click="openChart(review.browserRows[0])"
          >
            从本页第一条开始复盘 →
          </n-button>
        </div>
      </div>

      <n-data-table
        :columns="columns"
        :data="review.browserRows"
        :row-key="(row) => row.event_id"
        :expanded-row-keys="expanded"
        @update:expanded-row-keys="(keys) => (expanded = keys as number[])"
        :row-props="rowProps"
        :loading="review.browserLoading"
        :scroll-x="1860"
        flex-height
        :bordered="false"
        class="review-table"
        size="small"
      >
        <template #empty>
          <n-empty
            :description="
              review.browserLoading
                ? '正在加载复盘信号明细与评分…'
                : emptyMessage
            "
          >
            <template #extra>
              <n-space justify="center" size="small">
                <n-button
                  v-if="review.browserError || review.metadata?.jobs?.[0]?.status === 'failed'"
                  size="small"
                  type="warning"
                  secondary
                  :loading="isRebuildingAllSignals"
                  @click="runRebuildAllSignals"
                >
                  <template #icon>
                    <n-icon :component="AlertTriangle" />
                  </template>
                  重建全部信号
                </n-button>
                <n-button size="small" secondary @click="reset">清除全部筛选</n-button>
              </n-space>
            </template>
          </n-empty>
        </template>
      </n-data-table>

      <div class="pagination-bar">
        <span class="pg-tip">支持按评分与时间双向排序 <i>·</i> 点击表头指标切换</span>
        <n-pagination
          :disabled="review.browserLoading"
          :page="review.browserPage"
          :page-size="review.browserPageSize"
          :item-count="review.browserTotal"
          :page-sizes="[40, 80, 150]"
          show-size-picker
          :page-slot="5"
          @update:page="page"
          @update:page-size="size"
        />
      </div>
    </div>
  </section>
</template>

<style scoped>
.workbench {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 480px;
  gap: 12px;
  overflow: hidden;
}

/* 顶部一体化表头控制条 */
.workbench-header-card {
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 10px 14px;
  flex: none;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.02);
}

.filter-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.score-cuts-group {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-left: 4px;
}
.cuts-label {
  font-size: 11px;
  color: #64748b;
  margin-right: 2px;
}

.filter-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
}

/* 结果面板 */
.results-panel {
  flex: 1;
  min-height: 260px;
  display: flex;
  flex-direction: column;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  overflow: hidden;
}

.results-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 12px 16px 8px;
  border-bottom: 1px solid #f1f5f9;
}
.rh-left {
  display: flex;
  align-items: center;
  gap: 8px;
}
.rh-left strong {
  font-size: 13px;
  color: #1e293b;
}
.count {
  border-radius: 4px;
  background: #eff6ff;
  color: #2563eb;
  padding: 1px 6px;
  font-size: 11px;
  font-weight: 600;
}
.coverage {
  font-size: 11px;
  color: #64748b;
  margin-left: 4px;
}
.coverage i,
.pagination-bar i {
  font-style: normal;
  margin: 0 5px;
  color: #cbd5e1;
}

.rh-right {
  display: flex;
  align-items: center;
  gap: 10px;
}
.tip-text {
  font-size: 11px;
  color: #94a3b8;
}

.review-table {
  flex: 1;
  min-height: 200px;
}

.pagination-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 10px 16px;
  border-top: 1px solid #f1f5f9;
  background: #fafafa;
}
.pg-tip {
  font-size: 11px;
  color: #94a3b8;
}

/* 表格内字段样式 */
:deep(.sortable-th-header) {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
  user-select: none;
  color: #1e293b;
}
:deep(.sortable-th-header:hover) {
  color: #2563eb;
}
:deep(.sort-indicator) {
  font-size: 11px;
  color: #3b82f6;
  font-weight: 700;
}

:deep(.symbol-cell) {
  display: flex;
  flex-direction: column;
  gap: 1px;
}
:deep(.sym-code) {
  font-size: 12px;
  color: #0f172a;
}
:deep(.sym-name) {
  font-size: 10px;
}

:deep(.dir-pill) {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 11px;
  font-weight: 600;
}
:deep(.dir-up) {
  background: #fef2f2;
  color: #ef4444;
}
:deep(.dir-down) {
  background: #ecfdf5;
  color: #10b981;
}

:deep(.grade-pill) {
  display: inline-block;
  padding: 1px 5px;
  border-radius: 3px;
  background: #f1f5f9;
  color: #475569;
  font-size: 11px;
}

:deep(.numeric-score) {
  font-variant-numeric: tabular-nums;
  font-feature-settings: "tnum";
  color: #1e293b;
  font-weight: 500;
}
:deep(.primary-score) {
  font-size: 14px;
  font-weight: 700;
  color: #1e293b;
}
:deep(.missing-score) {
  color: #94a3b8;
}

:deep(.signal-time) {
  font-size: 12px;
  color: #334155;
  font-weight: 500;
}

:deep(.row-actions) {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 6px;
}

:deep(.font-num) {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", "PingFang SC", "Microsoft YaHei", sans-serif;
  font-variant-numeric: tabular-nums;
  font-feature-settings: "tnum";
  font-weight: 500;
  color: #1e293b;
}
:deep(.font-bold) {
  font-weight: 700;
}
:deep(.text-muted) {
  color: #64748b;
}
:deep(.text-danger) {
  color: #dc2626;
  font-weight: 600;
}
:deep(.text-success) {
  color: #16a34a;
  font-weight: 600;
}
</style>
