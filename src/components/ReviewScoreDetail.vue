<script setup lang="ts">
import type { OutcomeDetail } from '../types'
import { qualityText } from '../utils/signalQuality'
import { reviewScoreStatus } from '../utils/reviewFilters'
defineProps<{ row: OutcomeDetail }>()
</script>
<template>
  <div class="score-detail" @click.stop>
    <div class="detail-heading"><strong>形态与评分拆解</strong><span>#{{ row.event_id }} · {{ row.symbol }} · {{ row.quality?.origin === 'live_frozen' ? '预警冻结' : '当前历史行情重算' }}</span></div>
    <div class="scores">
      <div><span>v2 综合 · 候选</span><b>{{ qualityText(row.review_scores?.quality_score) }}</b></div>
      <div><span>v2 形态 · 候选</span><b>{{ qualityText(row.review_scores?.morphology_score) }}</b></div>
      <div><span>v1 综合 / 形态</span><b>{{ qualityText(row.quality?.quality_score) }} / {{ qualityText(row.quality?.morphology_score) }}</b></div>
      <div><span>旧入场 / 到S1空间</span><b>{{ qualityText(row.entry_score, 2) }} / {{ qualityText(row.rr, 2) }}R</b></div>
    </div>
    <div class="parts"><span>趋势 {{ qualityText(row.quality?.trend_score) }}</span><span>A段 {{ qualityText(row.quality?.a_score) }} → {{ qualityText(row.review_scores?.a_score) }}</span><span>B段 {{ qualityText(row.quality?.b_score) }} → {{ qualityText(row.review_scores?.b_score) }}</span><span>空间 {{ qualityText(row.quality?.space_score) }}</span><span>关键位 {{ row.review_scores?.levels_partial ? '覆盖不足' : qualityText(row.review_scores?.levels_score) }}</span></div>
    <p v-if="row.review_scores?.status !== 'ok'" class="unavailable">{{ reviewScoreStatus(row.review_scores?.status) }}。{{ row.review_scores?.explanations.join('；') }}</p>
    <div v-if="row.review_scores?.evidence" class="evidence-grid"><details v-for="(module, key) in row.review_scores.evidence.modules" :key="key"><summary>{{ module.title }}</summary><ul><li v-for="fact in module.facts" :key="fact">{{ fact }}</li></ul><p>{{ module.reading }}</p><small>{{ module.limitations }}</small></details></div>
    <details class="formula"><summary>分数组成与版本</summary><p>v1形态：趋势25% + A段45% + B段30%；v1综合：形态80% + 到S1空间20%。</p><p>v2候选沿用形态权重，分别加入A集中度调整与B过程支持调整；综合为形态80% + 到S1空间15% + 已知关键位5%。关键位未知时归一已知权重。</p><p>{{ row.review_scores?.candidate_version }} · 0～100分用于结构比较，不表示盈利概率。</p><p v-for="reason in row.quality?.explanations" :key="reason">{{ reason }}</p></details>
  </div>
</template>
<style scoped>
.score-detail{container-type:inline-size;padding:18px 24px;background:#f7f9fd;color:#475569;font-size:12px;line-height:1.7}.detail-heading{display:flex;justify-content:space-between;gap:12px;margin-bottom:14px}.detail-heading strong{font-size:13px;color:#263954}.detail-heading>span{color:#8b98aa;font-size:11px}.scores{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.scores>div{background:#fff;border:1px solid #e6edf5;padding:10px 13px;border-radius:8px;display:flex;flex-direction:column;gap:5px}.scores span{color:#7d8da1;font-size:11px}.scores b{font-size:19px;color:#3d5caa;font-variant-numeric:tabular-nums}.parts{display:flex;gap:18px;flex-wrap:wrap;padding:14px 0}.evidence-grid{display:grid;grid-template-columns:1fr 1fr;gap:9px}details{border:1px solid #e3eaf3;border-radius:7px;background:#fff;padding:8px 11px}summary{cursor:pointer;color:#365375;font-weight:500}ul{padding-left:17px}p{margin:7px 0}small{color:#94a3b8}.formula{margin-top:10px}.unavailable{color:#a36c22}@media(max-width:700px){.score-detail{padding:12px}.scores,.evidence-grid{grid-template-columns:1fr 1fr}.detail-heading{flex-wrap:wrap}}
@container(max-width:600px){.scores,.evidence-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.detail-heading{flex-wrap:wrap}.scores b{overflow-wrap:anywhere}}
</style>
