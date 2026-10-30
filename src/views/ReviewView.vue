<script setup lang="ts">
import { h, onMounted, onBeforeUnmount, ref } from 'vue'
import ReviewWorkbench from '../components/ReviewWorkbench.vue'
import {
  NButton,
  NCard,
  NDataTable,
  NEmpty,
  NInputNumber,
  NIcon,
  NSelect,
  NSpace,
  NTag,
  NText,
  type DataTableColumns,
} from 'naive-ui'
import { api, onScanCompleted } from '../services/api'
import { confirmAction } from '../utils/confirm'
import {
  REVIEW_DIMENSIONS,
  REVIEW_STATS_SCOPES,
  fmtPct,
  fmtR,
  useReviewStore,
  type StatsScopeKey,
} from '../stores/review'
import { notify } from '../utils/notify'
import { ChevronDown, ChevronUp } from '@vicons/tabler'
import type { GroupStat, V2ModelRow } from '../types'

const review = useReviewStore()
const v2Columns: any = [
  { title: "event", key: "event_id", width: 90 },
  { title: "model", key: "model_id", width: 160, ellipsis: { tooltip: true } },
  { title: "P(win)", key: "p_win", width: 90, render: (row: any) => row.p_win == null ? "—" : (row.p_win as number).toFixed(3) },
  { title: "logit", key: "logit", width: 90, render: (row: any) => row.logit == null ? "—" : (row.logit as number).toFixed(2) },
  { title: "predicted_at", key: "predicted_at", width: 160 },
]

const loading = ref(false)
const rebuilding = ref(false)
const error = ref('')
/** V2 预测表属于诊断明细，默认收起，避免挤压下方复盘统计和最近信号明细 */
const showV2Details = ref(false)
const showLegacyStats = ref(false)
function toggleLegacyStats() { showLegacyStats.value = !showLegacyStats.value; if (showLegacyStats.value) void load() }
/** 评分区间筛选：上下限为空表示不限制 */
const scoreMinInput = ref<number | null>(null)
const scoreMaxInput = ref<number | null>(null)

/** 绿跌红涨：盈利/正向用红，亏损/负向用绿 */
const rColor = (v: number | null | undefined) => (v == null || v >= 0 ? '#e03131' : '#0f9d58')

function sampleTag(n: number) {
  if (n < 20) {
    return h(
      NTag,
      { type: 'warning', size: 'tiny', bordered: false },
      { default: () => `样本${n}` },
    )
  }
  return h(NText, { depth: 3, style: 'font-size:12px' }, { default: () => `${n}` })
}

const groupColumns: DataTableColumns<GroupStat> = [
  { title: '分组', key: 'key', minWidth: 90, ellipsis: { tooltip: true } },
  {
    title: '实例',
    key: 'n',
    width: 90,
    align: 'right',
    render: (r) => sampleTag(r.n),
  },
  { title: '已结算', key: 'settled', width: 80, align: 'right' },
  { title: '胜', key: 'wins', width: 70, align: 'right', render: (r) => h('span', { style: 'color:#e03131' }, r.wins) },
  { title: '负', key: 'losses', width: 70, align: 'right', render: (r) => h('span', { style: 'color:#0f9d58' }, r.losses) },
  {
    title: '胜率',
    key: 'win_rate',
    width: 90,
    align: 'right',
    render: (r) => {
      if (!r.settled || r.win_rate == null) return '—'
      const color = r.win_rate >= 0.5 ? '#e03131' : '#0f9d58'
      return h('span', { style: `color:${color};font-weight:600` }, fmtPct(r.win_rate))
    },
  },
  {
    title: '平均R',
    key: 'avg_r',
    width: 100,
    align: 'right',
    render: (r) => {
      const v = r.avg_r
      if (v == null) return h(NText, { depth: 3 }, { default: () => '—' })
      return h('span', { style: `color:${rColor(v)};font-weight:600` }, fmtR(v))
    },
  },
  {
    title: '平均K线',
    key: 'avg_bars',
    width: 90,
    align: 'right',
    render: (r) => (r.avg_bars == null ? '—' : r.avg_bars.toFixed(1)),
  },
  {
    title: '均盈R',
    key: 'avg_win_r',
    width: 90,
    align: 'right',
    render: (r) => (r.avg_win_r == null ? '—' : fmtR(r.avg_win_r)),
  },
  {
    title: '均亏R',
    key: 'avg_loss_r',
    width: 90,
    align: 'right',
    render: (r) => (r.avg_loss_r == null ? '—' : fmtR(r.avg_loss_r)),
  },
  {
    title: '盈亏比',
    key: 'payoff',
    width: 80,
    align: 'right',
    render: (r) => (r.payoff == null ? '—' : r.payoff.toFixed(2)),
  },
  {
    title: '盈利因子',
    key: 'profit_factor',
    width: 90,
    align: 'right',
    render: (r) => (r.profit_factor == null ? '—' : r.profit_factor.toFixed(2)),
  },
  {
    title: 'R≥1',
    key: 'r_ge1_rate',
    width: 80,
    align: 'right',
    render: (r) => fmtPct(r.r_ge1_rate),
  },
  {
    title: 'R≥2',
    key: 'r_ge2_rate',
    width: 80,
    align: 'right',
    render: (r) => fmtPct(r.r_ge2_rate),
  },
  {
    title: 'MFE≥1',
    key: 'mfe_ge1_rate',
    width: 90,
    align: 'right',
    render: (r) => fmtPct(r.mfe_ge1_rate),
  },
  {
    title: 'MAE≤-1',
    key: 'mae_le_neg1_rate',
    width: 90,
    align: 'right',
    render: (r) => fmtPct(r.mae_le_neg1_rate),
  },
  {
    title: '均R(MFE≥1)',
    key: 'avg_r_mfe_ge1',
    width: 110,
    align: 'right',
    render: (r) => (r.avg_r_mfe_ge1 == null ? '—' : fmtR(r.avg_r_mfe_ge1)),
  },
  {
    title: '均R(MAE≤-1)',
    key: 'avg_r_mae_le_neg1',
    width: 115,
    align: 'right',
    render: (r) => (r.avg_r_mae_le_neg1 == null ? '—' : fmtR(r.avg_r_mae_le_neg1)),
  },
  {
    title: '净R',
    key: 'avg_net_r',
    width: 90,
    align: 'right',
    render: (r) => {
      if (r.avg_net_r == null) return '—'
      return h('span', { style: `color:${rColor(r.avg_net_r)};font-weight:600` }, fmtR(r.avg_net_r))
    },
  },
  { title: '扩展目标', key: 'ext_target_n', width: 90, align: 'right' },
  { title: 'TP1', key: 'tp1_exits', width: 70, align: 'right' },
  { title: 'TP2', key: 'tp2_exits', width: 70, align: 'right' },
  {
    title: 'TP2转化',
    key: 'tp2_conversion',
    width: 90,
    align: 'right',
    render: (r) => fmtPct(r.tp2_conversion),
  },
  {
    title: 'TP2/扩展止盈',
    key: 'tp2_of_ext_rate',
    width: 110,
    align: 'right',
    render: (r) => fmtPct(r.tp2_of_ext_rate),
  },
  { title: '在途', key: 'pending', width: 70, align: 'right' },
  { title: '未触发', key: 'no_trigger', width: 80, align: 'right' },
  { title: '换月', key: 'rollover', width: 70, align: 'right' },
  {
    title: '缺口成交',
    key: 'gap_entry',
    width: 90,
    align: 'right',
    render: (r) => (r.gap_entry || r.gap_exit ? `${r.gap_entry}/${r.gap_exit}` : '—'),
  },
]

async function load(dim?: string, scope?: StatsScopeKey) {
  loading.value = true
  error.value = ''
  try {
    await review.load(dim, scope)
  } catch (e) {
    error.value = String(e)
    notify.error(String(e))
  } finally {
    loading.value = false
  }
}

async function refresh() {
  try {
    await review.refresh()
    notify.success('复盘数据已刷新')
  } catch (e) {
    notify.error(String(e))
  }
}

async function rebuildAll() {
  const ok = await confirmAction({
    title: '重建全部识别',
    content: '将清空所有旧信号记录，并用当前K线重新识别一遍，结果不可撤销。确定继续吗？',
    positiveText: '重建并重新识别',
  })
  if (!ok) return
  rebuilding.value = true
  try {
    const result = await api.rebuildEventsNow()
    await review.load()
    notify.success(
      `重建完成：${result.scanned} 个品种，当前 ${result.active_count} 个信号`,
    )
  } catch (e) {
    notify.error(String(e))
  } finally {
    rebuilding.value = false
  }
}

let scoreTimer: ReturnType<typeof setTimeout> | undefined
function onScoreRangeInput() {
  if (scoreTimer) clearTimeout(scoreTimer)
  scoreTimer = setTimeout(() => {
    void review.setScoreRange(scoreMinInput.value, scoreMaxInput.value)
  }, 400)
}

let unlisten: (() => void) | null = null

onMounted(async () => {
  try {
    unlisten = await onScanCompleted(() => {
      void review.loadReviewPage()
      if (showLegacyStats.value) void load()
    })
  } catch {
    // 旧构建/权限缺失时监听失败不应阻塞数据加载
  }
  // 形态工作台自行只读加载；旧统计在展开时再请求。
})

onBeforeUnmount(() => {
  if (scoreTimer) clearTimeout(scoreTimer)
  unlisten?.()
})
</script>

<template>
  <div class="page review-workspace">
    <header class="workspace-header">
      <div><div class="workspace-eyebrow">SIGNAL REVIEW</div><h1>形态与评分复盘</h1><p>按评分挑选信号，逐条对照形态与评分分项。</p></div>
      <div class="workspace-actions"><span>v1基线 · v2研究候选</span><n-button secondary type="primary" :loading="review.browserLoading" @click="review.loadReviewPage()">刷新评分与明细</n-button></div>
    </header>
    <ReviewWorkbench />
    <section class="legacy-section">
      <button class="legacy-toggle" @click="toggleLegacyStats">{{ showLegacyStats ? '收起' : '展开' }}历史交易统计与概率模型 <span>旧入场分口径，独立于上方形态筛选</span></button>
      <div v-if="showLegacyStats" class="legacy-body">
        <n-space align="center"><n-select v-model:value="review.statsScope" :options="REVIEW_STATS_SCOPES.map(s => ({ label: s.label, value: s.key }))" style="width:140px" @update:value="(v: string) => load(undefined, v as StatsScopeKey)" /><n-select v-model:value="review.dimension" :options="REVIEW_DIMENSIONS.map(d => ({ label: d.label, value: d.key }))" style="width:180px" @update:value="() => load()" /><n-button :loading="review.refreshing" @click="refresh">刷新结局统计</n-button><n-button :loading="rebuilding" @click="rebuildAll">重建全部识别</n-button></n-space>
        <n-text v-if="error" type="error">{{ error }}</n-text>
    <n-card size="small" class="v2-card">
      <template #header>
        <div class="v2-header">
          <div>
            <span class="v2-title">突破前概率模型</span>
            <span class="v2-subtitle">预警确认时冻结；预测突破成交后按既定规则退出的净盈利概率</span>
          </div>
          <n-button
            v-if="review.v2Predictions.length || review.v2Report"
            secondary
            size="small"
            class="v2-toggle"
            @click="showV2Details = !showV2Details"
          >
            <template #icon>
              <n-icon :component="showV2Details ? ChevronUp : ChevronDown" />
            </template>
            {{ showV2Details ? '收起模型明细' : '展开模型明细' }}
          </n-button>
        </div>
      </template>
      <n-space vertical :size="12">
        <n-space align="center" :size="8" wrap>
          <n-text depth="3" style="font-size:12px">模型</n-text>
          <n-select
            v-model:value="review.v2SelectedModel"
            :options="review.v2Models.map(m => ({ label: `${m.model_id} (${m.name})`, value: m.model_id }))"
            placeholder="选择模型查看预测"
            clearable
            style="width: 280px"
            size="small"
          />
          <n-button size="small" type="primary" :disabled="!review.v2SelectedModel" @click="review.loadV2Predictions()">加载预测</n-button>
          <n-tag v-if="review.v2Models.length" type="info" size="small">{{ review.v2Models.length }} 个模型</n-tag>
          <n-tag v-else type="warning" size="small">暂无经验证概率</n-tag>
        </n-space>
        <div v-if="showV2Details" class="v2-details">
          <n-text v-if="review.v2Report" depth="3" class="v2-report">
            {{ JSON.stringify(review.v2Report.summary ?? {}, null, 2).slice(0, 6000) }}
          </n-text>
          <n-data-table
            v-if="review.v2Predictions.length"
            :columns="v2Columns"
            :data="review.v2Predictions"
            :pagination="{ pageSize: 5 }"
            size="small"
            striped
          />
          <n-empty v-else description="暂无经验证概率；候选模型继续影子观察" />
          <n-text depth="3" class="v2-help">
            仅使用预警确认时已知特征。历史重建与实时预测分别记录；候选模型未通过前瞻验证时，不用于日常决策。
          </n-text>
        </div>
      </n-space>
    </n-card>

      <n-card size="small" class="overall-card">
        <n-space size="large" wrap>
          <div class="stat-item">
            <div class="stat-label">实例数</div>
            <div class="stat-value">{{ review.stats?.overall.n ?? 0 }}</div>
          </div>
          <div class="stat-item">
            <div class="stat-label">已结算</div>
            <div class="stat-value">{{ review.stats?.overall.settled ?? 0 }}</div>
          </div>
          <div class="stat-item">
            <div class="stat-label">胜率</div>
            <div class="stat-value">
              {{ review.stats?.overall.settled ? fmtPct(review.stats.overall.win_rate) : '—' }}
            </div>
          </div>
          <div class="stat-item">
            <div class="stat-label">平均R</div>
            <div class="stat-value" :style="{ color: rColor(review.stats?.overall.avg_r) }">
              {{ fmtR(review.stats?.overall.avg_r) }}
            </div>
          </div>
          <div class="stat-item">
            <div class="stat-label">缺口成交</div>
            <div class="stat-value">
              {{ (review.stats?.overall.gap_entry ?? 0) + (review.stats?.overall.gap_exit ?? 0) }}
            </div>
          </div>
          <div class="stat-item">
            <div class="stat-label">平均持仓K线</div>
            <div class="stat-value">
              {{
                review.stats?.overall.avg_bars == null
                  ? '—'
                  : review.stats.overall.avg_bars.toFixed(1)
              }}
            </div>
          </div>
          <div class="stat-item">
            <div class="stat-label">在途/未触发</div>
            <div class="stat-value">
              {{ review.stats?.overall.pending ?? 0 }} / {{ review.stats?.overall.no_trigger ?? 0 }}
            </div>
          </div>
        </n-space>
      </n-card>

      <n-card size="small" class="groups-card">
        <template #header>
          <div class="groups-header">
            <n-text strong>分组统计</n-text>
            <n-space align="center" :size="6">
              <n-text depth="3" style="font-size: 12px">评分</n-text>
              <n-input-number
                v-model:value="scoreMinInput"
                size="small"
                clearable
                :show-button="false"
                placeholder="下限"
                style="width: 84px"
                :min="0"
                :max="5"
                :step="0.1"
                @update:value="onScoreRangeInput"
              />
              <n-text depth="3" style="font-size: 12px">~</n-text>
              <n-input-number
                v-model:value="scoreMaxInput"
                size="small"
                clearable
                :show-button="false"
                placeholder="上限"
                style="width: 84px"
                :min="0"
                :max="5"
                :step="0.1"
                @update:value="onScoreRangeInput"
              />
            </n-space>
          </div>
        </template>
        <n-data-table
          :columns="groupColumns"
          :data="review.stats?.groups ?? []"
          size="small"
          :bordered="false"
          :loading="loading"
          max-height="240"
          :scroll-x="2100"
        />
        <n-empty
          v-if="!loading && !review.stats?.groups.length"
          description="暂无已回填的结局，点击右上角「刷新」"
          style="margin-top: 12px"
        />
      </n-card>

      </div>
    </section>
  </div>
</template>

<style scoped>
.page {
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 20px 24px;
  box-sizing: border-box;
  overflow: hidden;
}
.head-card {
  flex: none;
}
.body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 16px;
  overflow-y: auto;
  scrollbar-width: none;
}
.body::-webkit-scrollbar {
  width: 0;
  height: 0;
}
.overall-card {
  flex: none;
}
.groups-card {
  flex: none;
}
.groups-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
}
.details-card {
  flex: 1 1 260px;
  min-height: 220px;
  display: flex;
  flex-direction: column;
}
.details-table {
  flex: 1;
  min-height: 0;
}
.filter-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}
.stat-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 100px;
}
.stat-label {
  font-size: 12px;
  color: #97a0b3;
}
.stat-value {
  font-size: 22px;
  font-weight: 700;
  color: #1f2329;
  font-variant-numeric: tabular-nums;
}
.v2-card { flex: none; }
.v2-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.v2-title {
  font-weight: 600;
}
.v2-subtitle {
  margin-left: 8px;
  color: #97a0b3;
  font-size: 12px;
  font-weight: 400;
}
.v2-toggle {
  flex: none;
  border-radius: 7px;
  font-size: 12px;
  font-weight: 500;
}
.v2-details {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-top: 2px;
}
.v2-report {
  display: block;
  max-height: 180px;
  overflow: auto;
  padding: 8px;
  border-radius: 6px;
  background: #f8f9fb;
  white-space: pre-wrap;
  font-size: 12px;
  scrollbar-width: thin;
}
.v2-help {
  font-size: 11px;
  line-height: 1.6;
}
.review-workspace{background:#f5f7fb;overflow:auto}.workspace-header{display:flex;justify-content:space-between;align-items:center;gap:16px;flex:none}.workspace-eyebrow{font-size:9px;letter-spacing:2.1px;color:#8699b6;font-weight:600}.workspace-header h1{font-size:21px;color:#273d5c;letter-spacing:.3px;margin:3px 0 4px;font-weight:650}.workspace-header p{margin:0;color:#8a99ae;font-size:11px}.workspace-actions{display:flex;align-items:center;gap:15px}.workspace-actions>span{font-size:10px;color:#8a99ae}.legacy-section{flex:none}.legacy-toggle{border:0;background:none;color:#7f8ea3;font-size:11px;cursor:pointer;padding:0 0 4px}.legacy-toggle span{color:#a8b2c0;margin-left:15px;font-size:10px}.legacy-body{display:flex;flex-direction:column;gap:14px;padding-top:12px}@media(max-width:800px){.review-workspace{padding:14px}.workspace-actions>span,.legacy-toggle span{display:none}.workspace-header h1{font-size:18px}.workspace-actions{gap:0}}
</style>
