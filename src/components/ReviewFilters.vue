<script setup lang="ts">
import { computed, ref } from 'vue'
import { NButton, NDatePicker, NInputNumber, NSelect } from 'naive-ui'
import type { RecentOutcomeFilters, ReviewScoreMetric } from '../types'
import { reviewScoreOptions } from '../utils/reviewFilters'

const props = defineProps<{ filters: RecentOutcomeFilters; symbols: { label: string; value: string }[]; compact?: boolean }>()
const emit = defineEmits<{ change: [patch: Partial<RecentOutcomeFilters>]; reset: [] }>()
const advanced = ref(false)
const maximum = computed(() => props.filters.qualityMetric === 'old' ? 5 : 100)
const range = computed<[string, string] | null>(() => props.filters.timeStart && props.filters.timeEnd ? [props.filters.timeStart, props.filters.timeEnd] : null)
function changeMetric(qualityMetric: ReviewScoreMetric) { emit('change', { qualityMetric, qualityMin: null, qualityMax: null }) }
function changeDates(value: [string, string] | null) { emit('change', { timeStart: value?.[0] || null, timeEnd: value?.[1] || null }) }
function changeStatus(qualityStatus: 'all' | 'scored' | 'unscored') {
  emit('change', { qualityStatus, ...(qualityStatus === 'unscored' ? { qualityMin: null, qualityMax: null } : {}) })
}
const sortOptions = [
  { label: '时间 · 最新在前', value: 'time_desc' }, { label: '时间 · 最早在前', value: 'time_asc' },
  { label: '评分 · 从高到低', value: 'score_desc' }, { label: '评分 · 从低到高', value: 'score_asc' },
]
</script>

<template>
  <section class="review-filters" :class="{ compact }" aria-label="复盘筛选">
    <div class="filter-grid">
      <label class="field products"><span>品种</span><n-select :value="filters.symbols || []" multiple filterable clearable :max-tag-count="2" :options="symbols" placeholder="全部品种，可多选" @update:value="value => emit('change', { symbols: value, symbol: '' })" /></label>
      <label class="field"><span>评分口径</span><n-select :value="filters.qualityMetric || 'quality_v2'" :options="reviewScoreOptions" @update:value="changeMetric" /></label>
      <div class="field score-range"><span>评分区间 <small>{{ maximum === 5 ? '0～5' : '0～100' }}</small></span><div class="range-inputs"><n-input-number :value="filters.qualityMin ?? null" :min="0" :max="maximum" :step="maximum === 5 ? .1 : 1" :show-button="false" clearable placeholder="最低分" @update:value="value => emit('change', { qualityMin: value, ...(value != null ? { qualityStatus: 'scored' } : {}) })" /><i>—</i><n-input-number :value="filters.qualityMax ?? null" :min="0" :max="maximum" :show-button="false" clearable placeholder="最高分" @update:value="value => emit('change', { qualityMax: value, ...(value != null ? { qualityStatus: 'scored' } : {}) })" /></div></div>
      <label class="field"><span>排序</span><n-select :value="filters.sortBy || 'time_desc'" :options="sortOptions" @update:value="value => emit('change', { sortBy: value })" /></label>
    </div>
    <div class="time-grid">
      <label class="field"><span>时间口径</span><n-select :value="filters.timeField || 'warning'" :options="[{ label: '预警时间', value: 'warning' }, { label: '触发时间', value: 'trigger' }]" @update:value="value => emit('change', { timeField: value })" /></label>
      <label class="field dates"><span>日期与时间范围 <small>北京时间</small></span><n-date-picker :formatted-value="range" type="datetimerange" value-format="yyyy-MM-dd HH:mm:ss" clearable :update-value-on-close="true" start-placeholder="开始时间" end-placeholder="结束时间" @update:formatted-value="changeDates" /></label>
      <div class="field"><span>每日时段 <small>支持跨午夜</small></span><div class="range-inputs clock"><input aria-label="时段开始" type="time" :value="filters.hourStart || ''" @change="emit('change', { hourStart: ($event.target as HTMLInputElement).value || null })"><i>—</i><input aria-label="时段结束" type="time" :value="filters.hourEnd || ''" @change="emit('change', { hourEnd: ($event.target as HTMLInputElement).value || null })"></div></div>
    </div>
    <div class="filter-footer">
      <div class="quick-scores"><span>快速查看</span><n-button v-for="cut in (maximum === 5 ? [2.5, 3, 3.5, 4] : [60, 70, 80, 90])" :key="cut" size="tiny" :type="filters.qualityMin === cut && filters.qualityMax == null ? 'primary' : 'default'" secondary @click="emit('change', { qualityMin: cut, qualityMax: null, qualityStatus: 'scored', sortBy: 'score_desc' })">≥ {{ cut }}</n-button></div>
      <div class="filter-actions"><n-select class="status" size="small" :value="filters.qualityStatus || 'all'" :options="[{ label: '全部评分状态', value: 'all' }, { label: '仅有评分', value: 'scored' }, { label: '仅无法评分', value: 'unscored' }]" @update:value="changeStatus" /><n-button size="small" text @click="advanced = !advanced">{{ advanced ? '收起条件' : '更多条件' }}</n-button><n-button size="small" @click="emit('reset')">重置全部</n-button></div>
    </div>
    <div v-if="advanced" class="advanced-grid">
      <n-select :value="filters.direction" clearable placeholder="全部方向" :options="[{ label: '做多', value: 'up' }, { label: '做空', value: 'down' }]" @update:value="value => emit('change', { direction: value })" />
      <n-select :value="filters.level" clearable placeholder="全部级别" :options="[{ label: '精细', value: 'fine' }, { label: '较大', value: 'large' }, { label: '箱体', value: 'box' }]" @update:value="value => emit('change', { level: value })" />
      <n-select :value="filters.grade" clearable placeholder="全部等级" :options="['A级', 'B级', 'C级', '回撤过浅', '回撤过深'].map(value => ({ label: value, value }))" @update:value="value => emit('change', { grade: value })" />
      <n-select :value="filters.outcome" clearable placeholder="全部结局" :options="[{ label: '盈利', value: 'win' }, { label: '亏损', value: 'loss' }, { label: '持仓中', value: 'open' }, { label: '未触发', value: 'no_trigger' }, { label: '数据不足', value: 'insufficient_data' }, { label: '换月', value: 'rollover' }]" @update:value="value => emit('change', { outcome: value })" />
    </div>
  </section>
</template>

<style scoped>
.review-filters{padding:14px 18px;background:#fff;border:1px solid #e4eaf2;border-radius:12px}.filter-grid{display:grid;grid-template-columns:minmax(170px,1.2fr) minmax(155px,.9fr) minmax(210px,1.25fr) minmax(165px,1fr);gap:16px}.field{display:flex;flex-direction:column;gap:5px;min-width:0}.field>span{font-size:12px;font-weight:600;color:#475569;display:flex;align-items:center;gap:8px}small{font-size:11px;color:#94a3b8;font-weight:400}.range-inputs{display:flex;gap:7px;align-items:center;min-width:0}.range-inputs>*{min-width:0;flex:1}.range-inputs i{flex:none;color:#94a3b8;font-style:normal}.time-grid{margin-top:12px;display:grid;grid-template-columns:150px minmax(300px,1.5fr) minmax(220px,1fr);gap:16px}.clock input{box-sizing:border-box;width:100%;height:34px;border:1px solid #e0e0e6;border-radius:4px;padding:0 7px;background:white;color:#334155;font:inherit}.filter-footer{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:12px;padding-top:10px;border-top:1px solid #f0f3f8}.quick-scores,.filter-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.quick-scores>span{font-size:11px;color:#94a3b8;margin-right:3px}.status{width:145px}.advanced-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:12px}.compact{padding:12px}.compact .filter-grid,.compact .time-grid{grid-template-columns:1fr 1fr;gap:10px}.compact .dates,.compact .time-grid>.field:last-child{grid-column:1/-1}.compact .filter-footer{flex-wrap:wrap}.compact .advanced-grid{grid-template-columns:1fr 1fr}@media(max-width:1000px){.filter-grid{grid-template-columns:1fr 1fr}.time-grid{grid-template-columns:140px 1fr}.time-grid>.field:last-child{grid-column:1/-1}.filter-footer{flex-wrap:wrap}}@media(max-width:600px){.review-filters{padding:12px}.filter-grid,.time-grid{grid-template-columns:1fr}.time-grid>.field:last-child{grid-column:auto}.advanced-grid{grid-template-columns:1fr 1fr}.quick-scores>span{display:none}}
</style>
