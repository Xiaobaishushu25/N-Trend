<script setup lang="ts">
import { computed, ref } from 'vue'
import { NAlert, NButton, NIcon, NSpin, NTabs, NTabPane } from 'naive-ui'
import { InfoCircle, Scale, X } from '@vicons/tabler'
import type { ReviewResultDetail, ReviewSummary } from '../types/review'
import { qualityText } from '../utils/signalQuality'
import { reviewScoreStatus } from '../utils/reviewFilters'
import { costExplanation, excursionText, publishedScore, reviewState, sameReview } from '../utils/reviewPresentation'
import { fmtR } from '../stores/review'

const props = defineProps<{ detail?: ReviewResultDetail | null; summary?: ReviewSummary | null; loading?: boolean; error?: string }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'retry'): void }>()
const activeTab = ref<'lifecycle' | 'scores' | 'diagnostics'>('lifecycle')
const matchedDetail = computed(() => sameReview(props.summary, props.detail) ? props.detail! : null)
const effectiveSummary = computed(() => matchedDetail.value?.summary ?? props.summary)
const detailMismatch = computed(() => !!props.detail && !matchedDetail.value)
const reviewScores = computed(() => matchedDetail.value?.review_scores)
const quality = computed(() => matchedDetail.value?.quality)
const origEvent = computed(() => matchedDetail.value?.original_event ?? {})
const evidenceModules = computed(() => reviewScores.value?.evidence?.modules ?? {})
const aEvidence = computed(() => evidenceModules.value['a_process'] ?? Object.values(evidenceModules.value).find(m => m.title?.includes('A段')))
const bEvidence = computed(() => evidenceModules.value['b_process'] ?? Object.values(evidenceModules.value).find(m => m.title?.includes('B段')))
const rColor = (v: number | null | undefined) => v == null ? '#64748b' : v >= 0 ? '#dc2626' : '#16a34a'
function fmtPrice(v: number | null | undefined) { return typeof v === 'number' && Number.isFinite(v) ? v.toFixed(1) : '—' }
function eventNumber(key: string): number | null { const v = origEvent.value[key]; return typeof v === 'number' && Number.isFinite(v) ? v : null }
function nodeNumber(stage: string, key: string): number | null {
  const v = matchedDetail.value?.lifecycle.find(n => n.stage === stage)?.evidence[key]
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}
const plannedEntry = computed(() => effectiveSummary.value?.plan?.entry ?? eventNumber('entry'))
const plannedStop = computed(() => effectiveSummary.value?.plan?.stop ?? eventNumber('stop'))
const plannedTarget = computed(() => effectiveSummary.value?.plan?.reference_target ?? eventNumber('target'))
const fillPrice = computed(() => effectiveSummary.value?.entry_ts ? matchedDetail.value?.entry_price ?? effectiveSummary.value.entry_price : null)
const exitPriceVal = computed(() => effectiveSummary.value?.exit_ts ? matchedDetail.value?.exit_price ?? effectiveSummary.value.exit_price : null)
const netRVal = computed(() => effectiveSummary.value?.net_r)
const terminalNode = computed(() => matchedDetail.value?.lifecycle.find(n => n.stage === 'terminal'))
const terminalEvidence = computed(() => (terminalNode.value?.evidence ?? {}) as Record<string, any>)
const riskPoints = computed(() => effectiveSummary.value?.risk_points)
const costStatusExplanation = computed(() => costExplanation(matchedDetail.value))
const slippageLabel = computed(() => {
  const ticks = matchedDetail.value?.cost.entry_slippage_ticks
  return fillPrice.value == null || ticks == null ? '未知' : `${ticks} tick（入场）`
})
const mfeText = computed(() => excursionText(matchedDetail.value?.mfe ?? effectiveSummary.value?.mfe, effectiveSummary.value?.mfe_r))
const maeText = computed(() => excursionText(matchedDetail.value?.mae ?? effectiveSummary.value?.mae, effectiveSummary.value?.mae_r))
const diagnosticLabels: Record<string, string> = { retracement: '回撤率', b_a_speed: 'B/A速度比', b_vol_ratio: 'B/A均量比（15m）', a_move_atr: 'A段幅度/ATR', vol_ratio: '触发量比（5m）', overshoot: '追价深度（R）', trigger_lag: '等待交易K（15m）', gap_entry: '入场跳空', gap_exit: '出场跳空', dim_a: '原A项', dim_b: '原B项', dim_warning: '原预警项', oi_increase: '持仓量增加' }
function diagnosticText(v: number | null) { return v == null ? '未知' : Number.isFinite(v) ? v.toFixed(3) : '未知' }
</script>

<template>
  <div class="detail-drawer">
    <div class="drawer-header">
      <div class="dh-left"><n-icon :component="Scale" :size="15" color="#2563eb" /><span class="dh-title">信号明细</span><span class="dh-id font-num">#{{ summary?.event_id }}</span><span v-if="matchedDetail" class="dh-badge">{{ matchedDetail.profile }}</span></div>
      <n-button quaternary circle size="tiny" @click="emit('close')"><template #icon><n-icon :component="X" /></template></n-button>
    </div>
    <div v-if="loading" class="loading-box"><n-spin size="small" /><span>正在加载同版本复盘详情…</span></div>
    <div v-else class="drawer-body">
      <n-alert v-if="error || detailMismatch" type="warning" :show-icon="false">{{ error || '详情与当前信号版本不一致，已停止显示。' }}<n-button size="tiny" @click="emit('retry')">重试</n-button></n-alert>
      <div class="scores-overview-bar">
        <div class="so-item highlight"><span class="so-lbl">v2 综合 /100</span><b class="so-val text-brand">{{ qualityText(publishedScore(effectiveSummary, 'quality_v2')) }}</b></div>
        <div class="so-item"><span class="so-lbl">v2 形态 /100</span><b class="so-val">{{ qualityText(publishedScore(effectiveSummary, 'morphology_v2')) }}</b></div>
        <div class="so-item"><span class="so-lbl">v2 A段 /100</span><b class="so-val text-long">{{ qualityText(publishedScore(effectiveSummary, 'a_score')) }}</b></div>
        <div class="so-item"><span class="so-lbl">v2 B段 /100</span><b class="so-val text-warn">{{ qualityText(publishedScore(effectiveSummary, 'b_score')) }}</b></div>
        <div class="so-item"><span class="so-lbl">旧入场 /5</span><b class="so-val">{{ qualityText(publishedScore(effectiveSummary, 'old')) }}</b></div>
      </div>
      <n-tabs v-model:value="activeTab" type="line" size="small" class="detail-tabs" justify-content="space-between">
        <n-tab-pane name="lifecycle" tab="平仓流水">
          <div class="scroll-pane">
            <div class="hist-full-timeline">
              <div class="timeline-node node-warning">
                <div class="node-title"><span class="node-badge">1. 预警拟定</span><span class="node-ts">{{ effectiveSummary?.warning_ts || '—' }}</span></div>
                <div class="node-content">
                  <div class="node-field"><span class="field-lbl">计划入场</span><span class="field-val">{{ fmtPrice(plannedEntry) }}</span></div>
                  <div class="node-field"><span class="field-lbl">初始止损</span><span class="field-val">{{ fmtPrice(plannedStop) }}</span></div>
                  <div class="node-field"><span class="field-lbl">原始参考目标</span><span class="field-val">{{ fmtPrice(plannedTarget) }}</span></div>
                </div>
              </div>
              <div v-if="effectiveSummary?.state === 'filtered'" class="timeline-node node-filtered">
                <div class="node-title"><span class="node-badge" style="background: #eab308; color: #fff;">2. 准入与风控决策</span><span class="node-ts">{{ terminalNode?.occurred_at || effectiveSummary?.warning_ts || '—' }}</span></div>
                <div class="node-content">
                  <div class="node-field"><span class="field-lbl">准入判定</span><span class="field-val" style="color: #ca8a04; font-weight: 600;">未通过（已过滤）</span></div>
                  <div class="node-field"><span class="field-lbl">决策原因</span><span class="field-val">{{ terminalEvidence.filter_reason || terminalNode?.comment || '未满足准入门槛或风控限制' }}</span></div>
                  <div class="node-field"><span class="field-lbl">v2 综合分</span><span class="field-val">{{ qualityText(publishedScore(effectiveSummary, 'quality_v2')) }}</span></div>
                  <div class="node-field"><span class="field-lbl">计划风险跳数</span><span class="field-val">{{ terminalEvidence.planned_risk_ticks != null ? `${Number(terminalEvidence.planned_risk_ticks).toFixed(1)} tick` : '—' }} <template v-if="terminalEvidence.min_risk_ticks != null">（门槛 {{ terminalEvidence.min_risk_ticks }} tick）</template></span></div>
                  <div class="node-field"><span class="field-lbl">模拟成交价</span><span class="field-val">—</span></div>
                </div>
              </div>
              <div v-else-if="effectiveSummary?.state === 'expired'" class="timeline-node node-expired">
                <div class="node-title"><span class="node-badge" style="background: #64748b; color: #fff;">2. 等待触发</span><span class="node-ts">{{ terminalNode?.occurred_at || effectiveSummary?.warning_ts || '—' }}</span></div>
                <div class="node-content">
                  <div class="node-field"><span class="field-lbl">等待状态</span><span class="field-val">已到期未触发</span></div>
                  <div class="node-field"><span class="field-lbl">到期原因</span><span class="field-val">{{ terminalNode?.comment || '等待12根15分钟K线未触发或结构失效' }}</span></div>
                  <div class="node-field"><span class="field-lbl">模拟成交价</span><span class="field-val">—</span></div>
                </div>
              </div>
              <div v-else-if="effectiveSummary?.state === 'unfilled'" class="timeline-node node-unfilled">
                <div class="node-title"><span class="node-badge" style="background: #f97316; color: #fff;">2. 触价未成交</span><span class="node-ts">{{ terminalNode?.occurred_at || effectiveSummary?.warning_ts || '—' }}</span></div>
                <div class="node-content">
                  <div class="node-field"><span class="field-lbl">拒单原因</span><span class="field-val">{{ terminalNode?.comment || '跳空或滑点超过追价上限' }}</span></div>
                  <div class="node-field"><span class="field-lbl">模拟成交价</span><span class="field-val">—</span></div>
                </div>
              </div>
              <div v-else class="timeline-node node-open">
                <div class="node-title"><span class="node-badge">2. 模拟入场</span><span class="node-ts">{{ effectiveSummary?.entry_ts || '未成交' }}</span></div>
                <div class="node-content">
                  <div class="node-field"><span class="field-lbl">模拟成交价</span><span class="field-val">{{ fmtPrice(fillPrice) }}</span></div>
                  <div class="node-field"><span class="field-lbl">执行滑点</span><span class="field-val">{{ slippageLabel }}</span></div>
                  <div class="node-field"><span class="field-lbl">追价深度</span><span class="field-val">{{ fmtR(effectiveSummary?.diagnostics.overshoot) }}</span></div>
                  <div class="node-field"><span class="field-lbl">成交后1R风险</span><span class="field-val">{{ riskPoints == null ? '—' : `${riskPoints.toFixed(1)} 点` }}</span></div>
                  <div class="node-field"><span class="field-lbl">本次固定1R目标</span><span class="field-val">{{ fmtPrice(nodeNumber('holding', 'target_1r')) }}</span></div>
                </div>
              </div>

              <div v-if="effectiveSummary?.state === 'filtered' || effectiveSummary?.state === 'expired' || effectiveSummary?.state === 'unfilled'" class="timeline-node node-terminal-end">
                <div class="node-title"><span class="node-badge" style="background: #64748b; color: #fff;">3. 信号终态</span><span class="node-ts">{{ terminalNode?.occurred_at || effectiveSummary?.warning_ts || '—' }}</span></div>
                <div class="node-content">
                  <div class="node-field"><span class="field-lbl">终态结果</span><span class="field-val">{{ reviewState(effectiveSummary).text }}</span></div>
                  <div class="node-field"><span class="field-lbl">终态说明</span><span class="field-val">{{ terminalNode?.comment || '信号已终止，不进入模拟持仓与平仓' }}</span></div>
                  <div class="node-field"><span class="field-lbl">模拟平仓价</span><span class="field-val">—</span></div>
                </div>
              </div>
              <div v-else class="timeline-node node-exit">
                <div class="node-title"><span class="node-badge">3. 模拟退出</span><span class="node-ts">{{ effectiveSummary?.exit_ts || '未平仓' }}</span></div>
                <div class="node-content">
                  <div class="node-field"><span class="field-lbl">模拟平仓价</span><span class="field-val">{{ fmtPrice(exitPriceVal) }}</span></div>
                  <div class="node-field"><span class="field-lbl">状态 / 原因</span><span class="field-val">{{ reviewState(effectiveSummary).text }}</span></div>
                  <div class="node-field"><span class="field-lbl">净收益</span><span class="field-val" :style="{ color: rColor(netRVal) }">{{ fmtR(netRVal) }} · {{ effectiveSummary?.net_amount ?? '—' }} 元</span></div>
                </div>
              </div>
            </div>
            <div class="hist-meta-bar">
              <span>乘数 <b>{{ matchedDetail?.multiplier ?? '—' }}</b> 元/点</span>
              <span>持仓 <b>{{ effectiveSummary?.bars_held ?? '—' }}</b> 根交易K（15分钟）</span>
              <span>持有记录 <b>{{ matchedDetail?.holding_count ?? '—' }}</b> 条（5分钟）</span>
              <span class="mfe-tag">MFE <b>{{ mfeText }}</b></span><span class="mae-tag">MAE <b>{{ maeText }}</b></span>
            </div>
            <div v-if="matchedDetail?.lifecycle.length" class="section-box"><strong>本次复盘生命周期</strong><div v-for="node in matchedDetail.lifecycle" :key="node.sequence" class="node-field"><span>{{ node.occurred_at }}</span><span>{{ node.comment }}</span></div></div>
          </div>
        </n-tab-pane>
        <n-tab-pane name="scores" tab="A/B段细则">
          <div class="scroll-pane">
            <n-alert v-if="effectiveSummary?.score_status !== 'ok'" type="warning" :show-icon="false">{{ reviewScoreStatus(effectiveSummary?.score_status) }}。{{ effectiveSummary?.score_explanations?.join('；') }}</n-alert>
            <div class="section-box a-section">
              <div class="sec-head"><strong>A段推进细则</strong><span class="sec-score">{{ qualityText(publishedScore(effectiveSummary, 'a_score')) }} /100</span></div>
              <div class="metrics-mini-grid"><div class="metric-cell"><span>A浪幅度</span><b>{{ eventNumber('a_move') ?? '—' }} 点</b></div><div class="metric-cell"><span>根数（15m）</span><b>{{ eventNumber('a_bars') ?? '—' }}</b></div><div class="metric-cell"><span>幅度/ATR</span><b>{{ qualityText(effectiveSummary?.diagnostics.a_move_atr) }}</b></div></div>
              <div v-if="aEvidence" class="evidence-content"><strong>{{ aEvidence.title }}</strong><ul class="facts-list"><li v-for="fact in aEvidence.facts" :key="fact">{{ fact }}</li></ul><p>{{ aEvidence.reading }}</p><small>{{ aEvidence.limitations }}</small></div>
              <p v-else class="empty-hint">{{ matchedDetail ? '本次评分未生成A段过程证据，请查看评分状态。' : '尚未取得本次评分详情。' }}</p>
            </div>
            <div class="section-box b-section">
              <div class="sec-head"><strong>B段回调细则</strong><span class="sec-score">{{ qualityText(publishedScore(effectiveSummary, 'b_score')) }} /100</span></div>
              <div class="metrics-mini-grid"><div class="metric-cell"><span>B浪幅度</span><b>{{ eventNumber('b_move') ?? '—' }} 点</b></div><div class="metric-cell"><span>根数（15m）</span><b>{{ eventNumber('b_bars') ?? '—' }}</b></div><div class="metric-cell"><span>B/A速度比</span><b>{{ qualityText(effectiveSummary?.diagnostics.b_a_speed) }}</b></div><div class="metric-cell"><span>回撤率</span><b>{{ effectiveSummary?.diagnostics.retracement == null ? '未知' : `${(effectiveSummary.diagnostics.retracement * 100).toFixed(1)}%` }}</b></div></div>
              <div v-if="bEvidence" class="evidence-content"><strong>{{ bEvidence.title }}</strong><ul class="facts-list"><li v-for="fact in bEvidence.facts" :key="fact">{{ fact }}</li></ul><p>{{ bEvidence.reading }}</p><small>{{ bEvidence.limitations }}</small></div>
              <p v-else class="empty-hint">{{ matchedDetail ? '本次评分未生成B段过程证据，请查看评分状态。' : '尚未取得本次评分详情。' }}</p>
            </div>
            <div class="formula-card"><span class="formula-title">评分版本与口径</span><p>v2 候选按0～100分展示，不表示盈利概率；旧入场分按0～5分单列。</p><p>v1 综合 {{ qualityText(publishedScore(effectiveSummary, 'quality_v1')) }} /100；v1 形态 {{ qualityText(publishedScore(effectiveSummary, 'morphology_v1')) }} /100。</p><p>到S1空间分 {{ qualityText(quality?.status === 'ok' ? quality.space_score : null) }}；关键位 {{ reviewScores?.levels_partial ? '覆盖不足' : qualityText(reviewScores?.levels_score) }}。</p><p>{{ reviewScores?.candidate_version || '尚未取得评分版本' }}</p></div>
          </div>
        </n-tab-pane>
        <n-tab-pane name="diagnostics" tab="成本与诊断">
          <div class="scroll-pane">
            <div class="cost-explanation-banner" :class="'is-' + costStatusExplanation.type"><div class="ce-head"><n-icon :component="InfoCircle" :size="14" /><strong>{{ costStatusExplanation.title }}</strong></div><p class="ce-desc">{{ costStatusExplanation.desc }}</p></div>
            <div class="diag-grid">
              <div class="diag-card"><span class="diag-lbl">模拟开仓价</span><b class="diag-val">{{ fmtPrice(fillPrice) }}</b></div>
              <div class="diag-card"><span class="diag-lbl">模拟平仓价</span><b class="diag-val">{{ fmtPrice(exitPriceVal) }}</b></div>
              <div class="diag-card"><span class="diag-lbl">合约乘数</span><b class="diag-val">{{ matchedDetail?.multiplier ?? '—' }} 元/点</b></div>
              <div class="diag-card"><span class="diag-lbl">估算成本版本</span><span class="diag-val-text">{{ matchedDetail?.cost_version || '尚未取得' }}</span></div>
              <div class="diag-card"><span class="diag-lbl">毛/净盈亏（R）</span><b class="diag-val">{{ fmtR(effectiveSummary?.gross_r) }} / {{ fmtR(netRVal) }}</b></div>
              <div class="diag-card"><span class="diag-lbl">毛/净金额</span><b class="diag-val">{{ effectiveSummary?.gross_amount ?? '—' }} / {{ effectiveSummary?.net_amount ?? '—' }} 元</b></div>
              <div class="diag-card"><span class="diag-lbl">入场/出场滑点估算</span><span class="diag-val-text">{{ matchedDetail?.cost.entry_slippage_amount ?? '—' }} / {{ matchedDetail?.cost.exit_slippage_amount ?? '—' }} 元（已计入成交价）</span></div>
              <div class="diag-card"><span class="diag-lbl">附加往返费用估算</span><span class="diag-val-text">{{ matchedDetail?.cost.additional_fee_amount ?? '—' }} 元</span></div>
            </div>
            <div v-if="effectiveSummary?.coverage.length" class="section-box"><strong>数据覆盖</strong><span>{{ effectiveSummary.coverage.join('；') }}</span></div>
            <div v-if="effectiveSummary && Object.keys(effectiveSummary.diagnostics).length" class="section-box"><strong>量能与回撤特征指标</strong><div class="diag-chips"><span v-for="(value, key) in effectiveSummary.diagnostics" :key="key" class="diag-chip">{{ diagnosticLabels[key] || key }}：<b>{{ diagnosticText(value) }}</b></span></div></div>
          </div>
        </n-tab-pane>
      </n-tabs>
    </div>
  </div>
</template>

<style scoped>
.detail-drawer {
  display: flex;
  flex-direction: column;
  max-height: 560px;
  height: 100%;
  background: #ffffff;
  color: #1e293b;
  overflow: hidden;
  box-sizing: border-box;
}

.drawer-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 10px;
  background: #f8fafc;
  border-bottom: 1px solid #e2e8f0;
  flex: none;
}
.dh-left {
  display: flex;
  align-items: center;
  gap: 5px;
}
.dh-title {
  font-size: 12px;
  font-weight: 700;
  color: #0f172a;
}
.dh-id {
  font-size: 11px;
  color: #64748b;
}
.dh-badge {
  font-size: 10px;
  background: #eff6ff;
  color: #2563eb;
  padding: 1px 4px;
  border-radius: 3px;
}

.loading-box {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 160px;
  gap: 8px;
  color: #64748b;
  font-size: 12px;
}

.drawer-body {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

/* 顶部总览条：5列均衡网格，杜绝横向滚动截断 */
.scores-overview-bar {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  background: #f8fafc;
  border-bottom: 1px solid #edf2f7;
  padding: 6px 4px;
  gap: 2px;
  flex: none;
}
.so-item {
  display: flex;
  flex-direction: column;
  gap: 1px;
  text-align: center;
}
.so-lbl {
  font-size: 10px;
  color: #64748b;
  white-space: nowrap;
}
.so-val {
  font-size: 12px;
  font-weight: 700;
  color: #1e293b;
}

.detail-tabs {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
:deep(.n-tabs-nav) {
  padding: 0 8px;
  background: #ffffff;
}
:deep(.n-tabs-pane-wrapper) {
  flex: 1;
  min-height: 0;
  height: 100%;
}

.scroll-pane {
  padding: 8px 10px 16px;
  overflow-y: auto;
  max-height: 440px;
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
  gap: 8px;
  scrollbar-width: thin;
}

/* 3阶段平仓流水时间线 */
.hist-full-timeline {
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: #f8fafc;
  border: 1px solid #f1f5f9;
  border-radius: 6px;
  padding: 8px;
}

.timeline-node {
  display: flex;
  flex-direction: column;
  gap: 4px;
  background: #ffffff;
  border-radius: 6px;
  border: 1px solid #e2e8f0;
  padding: 6px 8px;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.02);
}

.timeline-node.node-warning {
  border-top: 3px solid #6366f1;
}
.timeline-node.node-open {
  border-top: 3px solid #0284c7;
}
.timeline-node.node-exit {
  border-top: 3px solid #10b981;
}

.timeline-connector {
  display: flex;
  align-items: center;
  justify-content: center;
  color: #94a3b8;
  font-size: 11px;
  user-select: none;
  transform: rotate(90deg);
  margin: -5px 0;
}

.node-title {
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid #f1f5f9;
  padding-bottom: 3px;
}
.node-badge {
  font-size: 11px;
  font-weight: 700;
  color: #1e293b;
}
.node-ts {
  font-size: 10px;
  color: #64748b;
}

.node-content {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.node-field {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 11px;
}
.field-lbl {
  color: #64748b;
}
.field-val {
  color: #1e293b;
}
.highlight-planned {
  font-weight: 600;
  color: #2563eb;
}
.highlight-fill {
  font-weight: 700;
  color: #0284c7;
}
.slip-val {
  color: #d97706;
}
.exit-reason-text {
  font-weight: 600;
}
.amount-sub {
  font-size: 10px;
  margin-left: 2px;
  font-weight: normal;
}

/* 底部博弈关键指标 */
.hist-meta-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 10px;
  font-size: 11px;
  color: #475569;
  padding: 6px 8px;
  background: #f1f5f9;
  border-radius: 6px;
}
.mfe-tag {
  color: #dc2626;
}
.mae-tag {
  color: #16a34a;
}

/* A/B段细则模块 */
.section-box {
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.a-section {
  border-left: 3px solid #dc2626;
}
.b-section {
  border-left: 3px solid #d97706;
}

.sec-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.sec-title {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  color: #0f172a;
}
.sec-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
}
.a-dot {
  background: #dc2626;
}
.b-dot {
  background: #d97706;
}
.sec-score {
  font-size: 11px;
  font-weight: 700;
  color: #2563eb;
}

.metrics-mini-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 4px;
  background: #f8fafc;
  padding: 5px 6px;
  border-radius: 4px;
}
.metric-cell {
  display: flex;
  justify-content: space-between;
  font-size: 10px;
}
.mc-lbl {
  color: #64748b;
}
.mc-val {
  color: #1e293b;
}

.evidence-content {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 11px;
}
.evidence-title {
  font-weight: 600;
  color: #334155;
  font-size: 11px;
}
.facts-list {
  margin: 0;
  padding-left: 16px;
  color: #475569;
  line-height: 1.4;
}
.reading-box {
  background: #eff6ff;
  color: #1e3a8a;
  padding: 5px 6px;
  border-radius: 4px;
  line-height: 1.4;
  font-size: 10px;
}
.reading-lbl {
  font-weight: 700;
}
.limitations-box {
  background: #fffbeb;
  color: #92400e;
  padding: 5px 6px;
  border-radius: 4px;
  line-height: 1.4;
  font-size: 10px;
}
.lim-lbl {
  font-weight: 700;
}

.formula-card {
  background: #fafafa;
  border: 1px dashed #cbd5e1;
  padding: 6px 8px;
  border-radius: 6px;
  font-size: 10px;
  color: #64748b;
}
.formula-title {
  font-weight: 600;
  color: #1e293b;
}
.formula-card p {
  margin: 2px 0;
}
.formula-ver {
  font-size: 10px;
  color: #94a3b8;
}

/* 成本诊断状态解释卡片 (Point 7) */
.cost-explanation-banner {
  padding: 8px 10px;
  border-radius: 6px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.cost-explanation-banner.is-default {
  background: #f1f5f9;
  border-color: #cbd5e1;
  color: #475569;
}
.cost-explanation-banner.is-warning {
  background: #fffbeb;
  border-color: #fde68a;
  color: #92400e;
}
.cost-explanation-banner.is-info {
  background: #eff6ff;
  border-color: #bfdbfe;
  color: #1e40af;
}
.cost-explanation-banner.is-success {
  background: #f0fdf4;
  border-color: #bbf7d0;
  color: #166534;
}
.ce-head {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
}
.ce-desc {
  margin: 0;
  font-size: 10px;
  line-height: 1.4;
  opacity: 0.9;
}

/* 诊断格网 */
.diag-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 6px;
}
.diag-card {
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding: 6px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 4px;
}
.diag-lbl {
  font-size: 10px;
  color: #64748b;
}
.diag-val {
  font-size: 12px;
  color: #1e293b;
}
.diag-val-text {
  font-size: 10px;
  color: #334155;
}
.diag-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.diag-chip {
  background: #f1f5f9;
  padding: 1px 5px;
  border-radius: 3px;
  font-size: 10px;
  color: #475569;
}

/* 辅助字体 */
.font-num {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", "PingFang SC", "Microsoft YaHei", sans-serif;
  font-variant-numeric: tabular-nums;
  font-feature-settings: "tnum";
  font-weight: 500;
}
.font-bold {
  font-weight: 700;
}
.text-brand {
  color: #2563eb;
}
.text-long {
  color: #dc2626;
}
.text-warn {
  color: #d97706;
}
.text-danger {
  color: #dc2626;
}
.text-success {
  color: #16a34a;
}
.text-muted {
  color: #94a3b8;
}
.empty-hint {
  color: #94a3b8;
  font-size: 10px;
}
</style>
