<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  NButton,
  NEmpty,
  NIcon,
  NSpin,
  NTabs,
  NTabPane,
  NTag,
  NText,
} from 'naive-ui'
import {
  ArrowDown,
  ArrowUp,
  Award,
  Check,
  ChevronDown,
  ChevronUp,
  Clock,
  Flame,
  Scale,
  Target,
  History,
  TrendingUp,
  X,
} from '@vicons/tabler'
import type { ReviewResultDetail, ReviewSummary } from '../types/review'
import type { OutcomeDetail } from '../types'
import { qualityText } from '../utils/signalQuality'
import { fmtR } from '../stores/review'

const props = defineProps<{
  detail?: ReviewResultDetail | null
  summary?: ReviewSummary | OutcomeDetail | null
  loading?: boolean
}>()

const emit = defineEmits<{
  (e: 'close'): void
}>()

const activeTab = ref<'scores' | 'lifecycle' | 'diagnostics'>('scores')

/** 绿跌红涨色彩助手 */
const rColor = (v: number | null | undefined) => (v == null || v >= 0 ? '#e03131' : '#0f9d58')

const reviewScores = computed(() => props.detail?.review_scores || (props.summary as OutcomeDetail)?.review_scores)
const quality = computed(() => props.detail?.quality || (props.summary as OutcomeDetail)?.quality)
const lifecycle = computed(() => props.detail?.lifecycle || [])
const evidenceModules = computed(() => reviewScores.value?.evidence?.modules || {})

// A段证据模块
const aEvidence = computed(() => {
  const mods = evidenceModules.value
  return mods['a_leg'] || mods['a'] || Object.values(mods).find(m => m.title?.includes('A段') || m.title?.includes('A腿'))
})

// B段证据模块
const bEvidence = computed(() => {
  const mods = evidenceModules.value
  return mods['b_leg'] || mods['b'] || Object.values(mods).find(m => m.title?.includes('B段') || m.title?.includes('B腿'))
})

// 其他证据模块
const otherEvidence = computed(() => {
  const mods = evidenceModules.value
  return Object.entries(mods).filter(([k, v]) => v !== aEvidence.value && v !== bEvidence.value)
})

function stageIcon(stage: string) {
  switch (stage) {
    case 'warning':
      return Clock
    case 'entry':
      return Target
    case 'exit':
      return Award
    default:
      return History
  }
}

function stageLabel(stage: string) {
  switch (stage) {
    case 'warning':
      return '预警溯源'
    case 'entry':
      return '突破触发'
    case 'exit':
      return '终结平仓'
    case 'holding':
      return '持仓极值'
    default:
      return stage
  }
}
</script>

<template>
  <div class="detail-drawer">
    <div class="drawer-header">
      <div class="dh-left">
        <n-icon :component="Scale" size="18" color="#3b82f6" />
        <span class="dh-title">形态评分细则与归因</span>
        <span v-if="summary" class="dh-id">#{{ summary.event_id }}</span>
        <span v-if="detail?.profile" class="dh-badge">{{ detail.profile }}</span>
      </div>
      <n-button quaternary circle size="tiny" class="close-btn" @click="emit('close')">
        <template #icon>
          <n-icon :component="X" />
        </template>
      </n-button>
    </div>

    <div v-if="loading" class="loading-box">
      <n-spin size="small" />
      <span>正在装载形态细则与逐K生命周期...</span>
    </div>

    <div v-else class="drawer-body">
      <!-- 顶部综合评分与结构拆解总览条 -->
      <div class="scores-overview-bar">
        <div class="so-item highlight">
          <span class="so-lbl">v2 综合 · 候选</span>
          <b class="so-val text-brand">{{ qualityText(reviewScores?.quality_score ?? (summary as any)?.scores?.quality_v2) }}</b>
        </div>
        <div class="so-item">
          <span class="so-lbl">v2 形态</span>
          <b class="so-val">{{ qualityText(reviewScores?.morphology_score ?? (summary as any)?.scores?.morphology_v2) }}</b>
        </div>
        <div class="so-item">
          <span class="so-lbl">A段得分</span>
          <b class="so-val text-long">{{ qualityText(reviewScores?.a_score ?? (summary as any)?.scores?.a_score ?? quality?.a_score) }}</b>
        </div>
        <div class="so-item">
          <span class="so-lbl">B段得分</span>
          <b class="so-val text-warn">{{ qualityText(reviewScores?.b_score ?? (summary as any)?.scores?.b_score ?? quality?.b_score) }}</b>
        </div>
        <div class="so-item">
          <span class="so-lbl">空间 / 关键位</span>
          <b class="so-val">{{ qualityText(quality?.space_score) }} / {{ reviewScores?.levels_partial ? '覆盖不足' : qualityText(reviewScores?.levels_score) }}</b>
        </div>
      </div>

      <!-- 选项卡切换：评分细则 / 生命周期因果链 / 诊断 -->
      <n-tabs v-model:value="activeTab" type="line" size="small" class="detail-tabs" justify-content="space-between">
        <n-tab-pane name="scores" tab="形态与A/B段评分细则">
          <div class="scroll-pane">
            <!-- A段评分细则专区 -->
            <div class="section-box a-section">
              <div class="sec-head">
                <div class="sec-title">
                  <span class="sec-dot a-dot"></span>
                  <strong>A段评分细则（推进动能与放量结构）</strong>
                </div>
                <span class="sec-score font-num">{{ qualityText(reviewScores?.a_score ?? quality?.a_score) }} 分</span>
              </div>
              <div v-if="aEvidence" class="evidence-content">
                <div class="evidence-title">{{ aEvidence.title }}</div>
                <ul class="facts-list">
                  <li v-for="(fact, idx) in aEvidence.facts" :key="idx">{{ fact }}</li>
                </ul>
                <div v-if="aEvidence.reading" class="reading-box">
                  <span class="reading-lbl">解读：</span>{{ aEvidence.reading }}
                </div>
                <div v-if="aEvidence.limitations && aEvidence.limitations !== '无'" class="limitations-box">
                  <span class="lim-lbl">局限：</span>{{ aEvidence.limitations }}
                </div>
              </div>
              <div v-else class="empty-hint">
                <p>当前记录仅保留基础形态特征，未包含独立 A段 冻结细则。</p>
              </div>
            </div>

            <!-- B段评分细则专区 -->
            <div class="section-box b-section">
              <div class="sec-head">
                <div class="sec-title">
                  <span class="sec-dot b-dot"></span>
                  <strong>B段评分细则（回调深度与缩量洗盘）</strong>
                </div>
                <span class="sec-score font-num">{{ qualityText(reviewScores?.b_score ?? quality?.b_score) }} 分</span>
              </div>
              <div v-if="bEvidence" class="evidence-content">
                <div class="evidence-title">{{ bEvidence.title }}</div>
                <ul class="facts-list">
                  <li v-for="(fact, idx) in bEvidence.facts" :key="idx">{{ fact }}</li>
                </ul>
                <div v-if="bEvidence.reading" class="reading-box">
                  <span class="reading-lbl">解读：</span>{{ bEvidence.reading }}
                </div>
                <div v-if="bEvidence.limitations && bEvidence.limitations !== '无'" class="limitations-box">
                  <span class="lim-lbl">局限：</span>{{ bEvidence.limitations }}
                </div>
              </div>
              <div v-else class="empty-hint">
                <p>当前记录无独立 B段 证据明细。</p>
              </div>
            </div>

            <!-- 其他维度细则（趋势、空间、关键位） -->
            <div v-if="otherEvidence.length" class="section-box other-section">
              <div class="sec-head">
                <div class="sec-title">
                  <span class="sec-dot c-dot"></span>
                  <strong>空间与关键位特征证据</strong>
                </div>
              </div>
              <div v-for="[key, mod] in otherEvidence" :key="key" class="other-mod">
                <div class="evidence-title">{{ mod.title }}</div>
                <ul class="facts-list">
                  <li v-for="(fact, idx) in mod.facts" :key="idx">{{ fact }}</li>
                </ul>
              </div>
            </div>

            <!-- 规则公式提示 -->
            <div class="formula-card">
              <span class="formula-title">评分权重说明：</span>
              <p>v2 综合 = 形态 80% (趋势25% + A段45% + B段30%) + 到S1空间 15% + 关键位 5%。</p>
              <span class="formula-ver">{{ reviewScores?.candidate_version || 'quality-rules-v2-candidate' }} · 固定一手回放</span>
            </div>
          </div>
        </n-tab-pane>

        <n-tab-pane name="lifecycle" tab="生命周期因果链">
          <div class="scroll-pane">
            <div v-if="lifecycle.length" class="timeline-container">
              <div v-for="node in lifecycle" :key="node.sequence" class="timeline-node">
                <div class="node-icon-box">
                  <n-icon :component="stageIcon(node.stage)" size="14" />
                </div>
                <div class="node-content">
                  <div class="node-head">
                    <span class="node-stage-pill" :class="'stage-' + node.stage">{{ stageLabel(node.stage) }}</span>
                    <span class="node-time font-num">{{ node.occurred_at }}</span>
                  </div>
                  <div class="node-comment">{{ node.comment }}</div>
                  <div v-if="node.evidence && Object.keys(node.evidence).length" class="node-evidence">
                    <span v-for="(v, k) in node.evidence" :key="k" class="node-ev-chip">
                      {{ k }}: {{ v }}
                    </span>
                  </div>
                </div>
              </div>
            </div>

            <div v-else class="empty-placeholder">
              <n-empty description="该信号暂无独立存储的生命周期事件链" size="small" />
            </div>

            <!-- 极值指标核对卡 -->
            <div v-if="detail?.mfe || detail?.mae" class="excursion-summary">
              <div class="ex-item">
                <span class="ex-lbl">最大有利浮盈 (MFE)</span>
                <b class="ex-val text-long font-num">{{ fmtR(detail.mfe?.r) }}</b>
                <span class="ex-sub font-num">{{ detail.mfe?.amount != null ? `${detail.mfe.amount}元` : '' }}</span>
              </div>
              <div class="ex-divider"></div>
              <div class="ex-item">
                <span class="ex-lbl">最大不利回撤 (MAE)</span>
                <b class="ex-val text-short font-num">{{ fmtR(detail.mae?.r) }}</b>
                <span class="ex-sub font-num">{{ detail.mae?.amount != null ? `${detail.mae.amount}元` : '' }}</span>
              </div>
              <div class="ex-divider"></div>
              <div class="ex-item">
                <span class="ex-lbl">持仓5m采样K线</span>
                <b class="ex-val font-num">{{ detail.holding_count || 0 }} 根</b>
              </div>
            </div>
          </div>
        </n-tab-pane>

        <n-tab-pane name="diagnostics" tab="执行与成本诊断">
          <div class="scroll-pane">
            <div class="diag-grid">
              <div class="diag-card">
                <span class="diag-lbl">开仓模拟成交</span>
                <b class="diag-val font-num">{{ detail?.entry_price ?? (summary as any)?.planned_entry ?? '—' }}</b>
              </div>
              <div class="diag-card">
                <span class="diag-lbl">平仓模拟成交</span>
                <b class="diag-val font-num">{{ detail?.exit_price ?? '—' }}</b>
              </div>
              <div class="diag-card">
                <span class="diag-lbl">合约乘数</span>
                <b class="diag-val font-num">{{ detail?.multiplier ?? '—' }}</b>
              </div>
              <div class="diag-card">
                <span class="diag-lbl">成本模型</span>
                <span class="diag-val-text">{{ detail?.cost_version || 'roundtrip-2.5tick-v1' }}</span>
              </div>
              <div class="diag-card">
                <span class="diag-lbl">毛盈亏 / 净盈亏 (R)</span>
                <b class="diag-val font-num" :style="{ color: rColor((summary as any)?.net_r) }">
                  {{ fmtR((summary as any)?.gross_r) }} / {{ fmtR((summary as any)?.net_r) }}
                </b>
              </div>
              <div class="diag-card">
                <span class="diag-lbl">毛金额 / 净金额</span>
                <b class="diag-val font-num" :style="{ color: rColor((summary as any)?.net_amount) }">
                  {{ (summary as any)?.gross_amount ?? '—' }} / {{ (summary as any)?.net_amount ?? '—' }} 元
                </b>
              </div>
            </div>

            <div v-if="(summary as any)?.diagnostics && Object.keys((summary as any).diagnostics).length" class="section-box" style="margin-top:12px">
              <div class="sec-head">
                <strong style="font-size:12px;color:#334155">量能与回撤诊断指标</strong>
              </div>
              <div class="diag-chips">
                <span v-for="(v, k) in (summary as any).diagnostics" :key="k" class="diag-chip">
                  {{ k }}: <b>{{ v }}</b>
                </span>
              </div>
            </div>
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
  padding: 10px 14px;
  background: #f8fafc;
  border-bottom: 1px solid #e2e8f0;
  flex: none;
}
.dh-left {
  display: flex;
  align-items: center;
  gap: 8px;
}
.dh-title {
  font-size: 13px;
  font-weight: 700;
  color: #0f172a;
}
.dh-id {
  font-size: 11px;
  color: #64748b;
  font-family: monospace;
}
.dh-badge {
  font-size: 10px;
  background: #eff6ff;
  color: #2563eb;
  padding: 1px 6px;
  border-radius: 4px;
}
.close-btn {
  color: #94a3b8;
}

.loading-box {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 40px 16px;
  font-size: 12px;
  color: #64748b;
}

.drawer-body {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

/* 评分总览条 */
.scores-overview-bar {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 6px;
  padding: 10px 14px;
  background: #f1f5f9;
  border-bottom: 1px solid #e2e8f0;
  flex: none;
}
.so-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.so-lbl {
  font-size: 10px;
  color: #64748b;
  white-space: nowrap;
}
.so-val {
  font-size: 14px;
  font-variant-numeric: tabular-nums;
  color: #0f172a;
}
.text-brand {
  color: #2563eb;
  font-size: 16px;
}
.text-long {
  color: #ef4444;
}
.text-short {
  color: #10b981;
}
.text-warn {
  color: #f59e0b;
}

.detail-tabs {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding: 4px 14px 0;
}
:deep(.n-tabs-pane) {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: 0 !important;
}

.scroll-pane {
  flex: 1;
  overflow-y: auto;
  padding: 10px 2px 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

/* 细则卡片 */
.section-box {
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.a-section {
  border-left: 3px solid #ef4444;
}
.b-section {
  border-left: 3px solid #f59e0b;
}
.sec-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.sec-title {
  display: flex;
  align-items: center;
  gap: 6px;
}
.sec-title strong {
  font-size: 12px;
  color: #1e293b;
}
.sec-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
}
.a-dot {
  background: #ef4444;
}
.b-dot {
  background: #f59e0b;
}
.c-dot {
  background: #3b82f6;
}
.sec-score {
  font-size: 13px;
  font-weight: 700;
  color: #2563eb;
}

.evidence-content {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12px;
}
.evidence-title {
  font-weight: 600;
  color: #334155;
  font-size: 11px;
}
.facts-list {
  margin: 0;
  padding-left: 18px;
  color: #475569;
  line-height: 1.6;
}
.facts-list li {
  margin-bottom: 2px;
}
.reading-box {
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  padding: 6px 8px;
  color: #334155;
  line-height: 1.5;
}
.reading-lbl {
  font-weight: 600;
  color: #2563eb;
}
.limitations-box {
  color: #94a3b8;
  font-size: 11px;
}
.lim-lbl {
  font-weight: 600;
}
.empty-hint {
  font-size: 11px;
  color: #94a3b8;
}

.formula-card {
  padding: 8px 10px;
  background: #f1f5f9;
  border-radius: 6px;
  font-size: 11px;
  color: #64748b;
  line-height: 1.5;
}
.formula-title {
  font-weight: 600;
  color: #475569;
}
.formula-card p {
  margin: 2px 0;
}
.formula-ver {
  font-size: 10px;
  color: #94a3b8;
}

/* 生命周期链 */
.timeline-container {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 6px 4px;
}
.timeline-node {
  display: flex;
  gap: 10px;
  position: relative;
}
.timeline-node:not(:last-child)::after {
  content: '';
  position: absolute;
  left: 11px;
  top: 24px;
  bottom: -12px;
  width: 2px;
  background: #e2e8f0;
}
.node-icon-box {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: #eff6ff;
  color: #2563eb;
  display: flex;
  align-items: center;
  justify-content: center;
  flex: none;
  z-index: 1;
}
.node-content {
  flex: 1;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.node-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.node-stage-pill {
  font-size: 10px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 4px;
}
.stage-warning {
  background: #fff7ed;
  color: #ea580c;
}
.stage-entry {
  background: #eff6ff;
  color: #2563eb;
}
.stage-exit {
  background: #f0fdf4;
  color: #16a34a;
}
.stage-holding {
  background: #f5f3ff;
  color: #7c3aed;
}
.node-time {
  font-size: 11px;
  color: #64748b;
}
.node-comment {
  font-size: 12px;
  color: #1e293b;
  line-height: 1.4;
}
.node-evidence {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 4px;
}
.node-ev-chip {
  font-size: 10px;
  background: #e2e8f0;
  color: #475569;
  padding: 1px 5px;
  border-radius: 3px;
  font-family: monospace;
}

.excursion-summary {
  display: flex;
  align-items: center;
  justify-content: space-around;
  padding: 10px 14px;
  background: #f1f5f9;
  border-radius: 8px;
  margin-top: 6px;
}
.ex-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  align-items: center;
}
.ex-lbl {
  font-size: 10px;
  color: #64748b;
}
.ex-val {
  font-size: 14px;
}
.ex-sub {
  font-size: 10px;
  color: #94a3b8;
}
.ex-divider {
  width: 1px;
  height: 24px;
  background: #cbd5e1;
}

/* 诊断与成本 */
.diag-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.diag-card {
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.diag-lbl {
  font-size: 10px;
  color: #64748b;
}
.diag-val {
  font-size: 13px;
  color: #0f172a;
}
.diag-val-text {
  font-size: 11px;
  color: #334155;
  font-family: monospace;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.diag-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.diag-chip {
  font-size: 11px;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 4px;
  padding: 2px 6px;
  color: #475569;
}
.diag-chip b {
  color: #0f172a;
}
</style>
