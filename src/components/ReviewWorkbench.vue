<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { isTauri } from '@tauri-apps/api/core'
import { emit } from '@tauri-apps/api/event'
import { WebviewWindow } from '@tauri-apps/api/webviewWindow'
import { NAlert, NButton, NDataTable, NEmpty, NPagination, NSelect, NTag, type DataTableColumns } from 'naive-ui'
import { api } from '../services/api'
import { useReviewStore } from '../stores/review'
import { useAppStore } from '../stores/app'
import type { OutcomeDetail, RecentOutcomeFilters, SymbolRow } from '../types'
import { qualityText } from '../utils/signalQuality'
import { reviewScore, reviewScoreOptions, reviewTime, reviewScoreStatus } from '../utils/reviewFilters'
import { notify } from '../utils/notify'
import ReviewFilters from './ReviewFilters.vue'
import ReviewScoreDetail from './ReviewScoreDetail.vue'

const review = useReviewStore()
const app = useAppStore()
const router = useRouter()
const names = ref<SymbolRow[]>([])
const expanded = ref<number[]>([])
const symbols = computed(() => [...new Set([...review.browserSymbols, ...names.value.map(s => s.code)])].sort().map(code => ({ value: code, label: `${names.value.find(s => s.code === code)?.name || code} · ${code}` })))
const metricLabel = computed(() => reviewScoreOptions.find(o => o.value === review.browserFilters.qualityMetric)?.label || 'v2 综合 · 候选')
const sortLabel = computed(() => ({ score_desc: '评分从高到低', score_asc: '评分从低到高', time_desc: '最新在前', time_asc: '最早在前' }[review.browserFilters.sortBy || 'time_desc']))
const outcomeLabels: Record<string, string> = { win: '盈利', loss: '亏损', open: '持仓中', no_trigger: '未触发', insufficient_data: '数据不足', rollover: '换月' }
const scoreCell = (value: number | null, strong = false) => h('span', { class: ['numeric-score', strong && 'primary-score', value == null && 'missing-score'] }, qualityText(value))
const columns = computed<DataTableColumns<OutcomeDetail>>(() => [
  { type: 'expand', width: 40, renderExpand: row => h(ReviewScoreDetail, { row }) },
  { title: '品种 / 形态', key: 'symbol', width: 155, fixed: 'left', render: row => h('div', { class: 'signal-identity' }, [
    h('strong', names.value.find(s => s.code === row.symbol)?.name || row.symbol),
    h('span', `#${row.event_id}`),
    h('div', { class: 'shape-labels' }, [h('b', { class: row.direction === 'up' ? 'long' : 'short' }, row.direction === 'up' ? '多' : '空'), h('span', `${row.symbol} · ${row.level === 'fine' ? '精细' : row.level === 'large' ? '较大' : row.level} · ${row.grade}`)]),
  ]) },
  { title: metricLabel.value, key: 'selected_score', width: 140, align: 'right', render: row => scoreCell(reviewScore(row, review.browserFilters.qualityMetric), true) },
  { title: 'v2形态', key: 'morphology_v2', width: 92, align: 'right', render: row => scoreCell(reviewScore(row, 'morphology_v2')) },
  { title: 'v1综合 / 形态', key: 'quality_v1', width: 135, align: 'right', render: row => `${qualityText(reviewScore(row, 'quality_v1'))} / ${qualityText(reviewScore(row, 'morphology_v1'))}` },
  { title: '旧入场', key: 'entry_score', width: 85, align: 'right', render: row => row.entry_score.toFixed(2) },
  { title: '到S1', key: 'rr', width: 85, align: 'right', render: row => `${qualityText(row.rr, 2)}R` },
  { title: review.browserFilters.timeField === 'trigger' ? '触发时间' : '预警时间', key: 'time', width: 175, render: row => h('div', { class: 'signal-time' }, [h('span', reviewTime(row, review.browserFilters.timeField) || '未触发'), h('small', reviewScoreStatus(row.review_scores?.status))]) },
  { title: '结局', key: 'outcome', width: 85, render: row => h(NTag, { size: 'small', bordered: false, type: row.outcome === 'win' ? 'error' : row.outcome === 'loss' ? 'success' : 'default' }, { default: () => outcomeLabels[row.outcome] || row.outcome || '等待触发' }) },
  { title: '核对', key: 'action', width: 152, fixed: 'right', render: row => h('div', { class: 'row-actions' }, [
    h(NButton, { size: 'tiny', text: true, onClick: (event: MouseEvent) => { event.stopPropagation(); expanded.value = expanded.value.includes(row.event_id) ? expanded.value.filter(id => id !== row.event_id) : [...expanded.value, row.event_id] } }, { default: () => '分项' }),
    h(NButton, { size: 'small', secondary: true, type: 'primary', onClick: (event: MouseEvent) => { event.stopPropagation(); void openChart(row) } }, { default: () => '查看形态 →' }),
  ]) },
])
let timer: ReturnType<typeof setTimeout> | undefined
function change(patch: Partial<RecentOutcomeFilters>) {
  review.updateBrowserFilterDraft(patch)
  expanded.value = []
  if (timer) clearTimeout(timer)
  timer = setTimeout(() => { void review.loadReviewPage() }, 250)
}
async function reset() { if (timer) clearTimeout(timer); expanded.value = []; await review.resetBrowserFilters() }
async function page(value: number) { if (timer) clearTimeout(timer); expanded.value = []; review.browserPage = value; await review.loadReviewPage() }
async function size(value: number) { review.browserPageSize = value; await page(1) }
async function openChart(row: OutcomeDetail) {
  const filters = { ...review.browserFilters, symbols: [...(review.browserFilters.symbols || [])], pageSize: review.browserPageSize, pageOffset: (review.browserPage - 1) * review.browserPageSize }
  try {
    if (!isTauri()) {
      app.reviewJumpFilters = filters
      await router.push({ name: 'chart', params: { symbol: row.symbol }, query: { review: String(row.event_id) } })
      return
    }
    await emit('open-review-chart', { symbol: row.symbol, eventId: row.event_id, filters })
    const main = await WebviewWindow.getByLabel('main')
    if (main) { await main.show(); await main.unminimize(); await main.setFocus() }
  } catch (error) { notify.error(`打开形态失败：${error}`) }
}
onMounted(() => { void review.loadReviewPage(); void api.getSymbols().then(value => { names.value = value }).catch(() => {}) })
onBeforeUnmount(() => { if (timer) clearTimeout(timer) })
</script>

<template>
  <section class="workbench">
    <ReviewFilters :filters="review.browserFilters" :symbols="symbols" @change="change" @reset="reset" />
    <n-alert v-if="review.browserError" type="error" :bordered="false">{{ review.browserError }} <n-button text @click="review.loadReviewPage()">重新加载</n-button></n-alert>
    <div class="results-panel">
      <div class="results-header"><div><strong>信号清单</strong><span class="count">{{ review.browserLoading ? '…' : review.browserTotal.toLocaleString() }}</span><span v-if="!review.browserLoading" class="coverage">有评分 {{ review.browserScored }} <i>·</i> 无法评分 {{ review.browserUnscored }}</span></div><span class="order-summary">{{ metricLabel }} / {{ sortLabel }}</span></div>
      <div class="table-tip"><span>展开左侧箭头查看分项与结构证据，点击信号查看K线形态。</span><n-button size="tiny" secondary :disabled="!review.browserRows.length" @click="openChart(review.browserRows[0])">从本页第一条开始 →</n-button></div>
      <n-data-table :columns="columns" :data="review.browserRows" :row-key="row => row.event_id" :expanded-row-keys="expanded" @update:expanded-row-keys="keys => expanded = keys as number[]" :row-props="row => ({ style: 'cursor:pointer', onClick: () => openChart(row) })" :loading="review.browserLoading" :scroll-x="1210" flex-height :bordered="false" class="review-table" size="small"><template #empty><n-empty :description="review.browserLoading ? '正在加载评分与明细…' : review.browserError ? '加载失败，修正条件或重试' : '当前条件下没有信号'"><template #extra><n-button size="small" @click="reset">清除筛选</n-button></template></n-empty></template></n-data-table>
      <div class="pagination-bar"><span>先筛选全部记录，再排序分页 <i>·</i> 缺失评分始终排在末尾</span><n-pagination :disabled="review.browserLoading" :page="review.browserPage" :page-size="review.browserPageSize" :item-count="review.browserTotal" :page-sizes="[40, 80, 150]" show-size-picker :page-slot="5" @update:page="page" @update:page-size="size" /></div>
    </div>
  </section>
</template>

<style scoped>
.workbench{display:flex;flex-direction:column;flex:1;min-height:420px;gap:14px;overflow:hidden}.results-panel{flex:1;min-height:240px;display:flex;flex-direction:column;background:white;border:1px solid #e4eaf2;border-radius:12px;overflow:hidden}.results-header{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:18px 20px 11px}.results-header>div{display:flex;align-items:center;gap:10px}.results-header strong{font-size:14px;color:#253a56}.count{border-radius:6px;background:#eef2ff;color:#4c65ac;padding:1px 7px;font-size:12px;font-weight:600}.coverage{font-size:11px;color:#94a3b8;margin-left:6px}.coverage i,.pagination-bar i{font-style:normal;margin:0 7px;color:#cbd5e1}.order-summary{font-size:11px;color:#8292a7}.table-tip{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:0 20px 13px;font-size:11px;color:#94a3b8}.review-table{flex:1;min-height:180px}.pagination-bar{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:13px 18px;border-top:1px solid #edf1f7}.pagination-bar>span{font-size:10px;color:#9aa7b8}:deep(.row-actions){display:flex;gap:10px;align-items:center;justify-content:flex-end}:deep(.numeric-score){font-variant-numeric:tabular-nums;color:#4b5d75}:deep(.primary-score){font-size:20px;font-weight:650;color:#4f67ad}:deep(.missing-score){color:#a6b1c0}:deep(.signal-identity){display:grid;grid-template-columns:1fr auto;gap:4px}:deep(.signal-identity>strong){color:#263b55;font-size:12px}:deep(.signal-identity>span){font-size:10px;color:#94a3b8}:deep(.shape-labels){grid-column:1/-1;display:flex;gap:6px;align-items:center;font-size:10px;color:#7b8ba0}:deep(.shape-labels>b){padding:0 4px;border-radius:3px;font-size:10px}:deep(.long){background:#fff1f0;color:#c86566}:deep(.short){background:#edf8f1;color:#5b9775}:deep(.signal-time){display:flex;flex-direction:column;gap:5px;font-size:11px;font-variant-numeric:tabular-nums}:deep(.signal-time small){font-size:10px;color:#9aa6b7}:deep(.n-data-table-th){font-size:11px;color:#8592a4;background:#f7f9fc}:deep(.n-data-table-td){border-bottom-color:#edf2f7;padding-top:10px;padding-bottom:10px}@media(max-width:800px){.results-header,.pagination-bar{flex-wrap:wrap}.pagination-bar>span,.coverage{display:none}.workbench{overflow:visible}.results-panel{min-height:460px}}
</style>
