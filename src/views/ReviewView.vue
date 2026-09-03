<script setup lang="ts">
import { h, onMounted, onBeforeUnmount, ref } from 'vue'
import { emit } from '@tauri-apps/api/event'
import { WebviewWindow } from '@tauri-apps/api/webviewWindow'
import {
  NButton,
  NCard,
  NDataTable,
  NEmpty,
  NInput,
  NInputNumber,
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
import type { GroupStat, OpenReviewChartPayload, OutcomeDetail, V2ModelRow } from '../types'

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
/** 品种筛选本地输入（防抖后再生效） */
const symbolInput = ref('')
/** 评分区间筛选：上下限为空表示不限制 */
const scoreMinInput = ref<number | null>(null)
const scoreMaxInput = ref<number | null>(null)

const dirLabel = (d: string) => (d === 'up' ? '做多' : d === 'down' ? '做空' : d)
const levelLabel = (l: string) =>
  l === 'fine' ? '精细' : l === 'large' ? '较大' : l === 'box' ? '箱体' : l

const outcomeLabel: Record<string, { text: string; type: 'success' | 'error' | 'warning' | 'default' }> = {
  win: { text: '盈利', type: 'error' },
  loss: { text: '亏损', type: 'success' },
  no_trigger: { text: '未触发', type: 'default' },
  open: { text: '持仓中', type: 'warning' },
  insufficient_data: { text: '数据不足', type: 'default' },
  rollover: { text: '换月', type: 'warning' },
}

const exitLabel: Record<string, string> = {
  stop: '止损',
  target: '止盈',
  no_follow: '无跟随退出',
  time_exit: '时间退出',
  rollover: '换月',
  '': '—',
}

/** 绿跌红涨：盈利/正向用红，亏损/负向用绿 */
const rColor = (v: number | null | undefined) => (v == null || v >= 0 ? '#e03131' : '#0f9d58')

function outcomeTag(outcome: string) {
  const meta = outcomeLabel[outcome] ?? { text: outcome, type: 'default' as const }
  return h(NTag, { type: meta.type, size: 'small' }, { default: () => meta.text })
}

function stateLabel(state: string) {
  switch (state) {
    case 'pending':
      return '等待触发'
    case 'triggered':
      return '已触发'
    case 'closed':
      return '已了结'
    case 'expired':
      return '已失效'
    default:
      return state
  }
}

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

const recentColumns: DataTableColumns<OutcomeDetail> = [
  { title: 'ID', key: 'event_id', width: 70 },
  { title: '品种', key: 'symbol', width: 80 },
  { title: '方向', key: 'direction', width: 70, render: (r) => dirLabel(r.direction) },
  { title: '级别', key: 'level', width: 70, render: (r) => levelLabel(r.level) },
  {
    title: '版本',
    key: 'logic_version',
    width: 70,
    render: (r) => r.logic_version || '1',
  },
  { title: '等级', key: 'grade', width: 90 },
  {
    title: '评分',
    key: 'entry_score',
    width: 80,
    align: 'right',
    render: (r) => r.entry_score.toFixed(2),
  },
  { title: '预警K线', key: 'warning_ts', width: 150, render: (r) => r.warning_ts },
  { title: '状态', key: 'state', width: 80, render: (r) => stateLabel(r.state) },
  {
    title: '开仓',
    key: 'opened',
    width: 80,
    render: (r) => {
      if (r.opened == null) {
        return h('span', { style: 'color:#9aa3b2' }, '未记录')
      }
      return h(
        'span',
        { style: `color:${r.opened ? '#e03131' : '#64748b'};font-weight:600` },
        r.opened ? '已开仓' : '未开仓',
      )
    },
  },
  { title: '入场', key: 'entry', width: 90, align: 'right', render: (r) => r.entry.toFixed(1) },
  { title: '止损', key: 'stop', width: 90, align: 'right', render: (r) => r.stop.toFixed(1) },
  { title: '目标位(参考)', key: 'target', width: 100, align: 'right', render: (r) => r.target.toFixed(1) },
  {
    title: '扫描时间',
    key: 'created_at',
    width: 150,
    render: (r) => h('span', { title: '该行是扫描时刻的快照，盘中快照含未走完的当前桶' }, r.created_at),
  },
  { title: '结局', key: 'outcome', width: 90, render: (r) => outcomeTag(r.outcome) },
  { title: '出场原因', key: 'exit_reason', width: 110, render: (r) => exitLabel[r.exit_reason] ?? r.exit_reason },
  {
    title: '出场价',
    key: 'exit_price',
    width: 90,
    align: 'right',
    render: (r) => (r.exit_price == null ? '—' : r.exit_price.toFixed(1)),
  },
  {
    title: 'R',
    key: 'r_multiple',
    width: 90,
    align: 'right',
    render: (r) => {
      if (r.r_multiple == null) return '—'
      return h('span', { style: `color:${rColor(r.r_multiple)};font-weight:600` }, fmtR(r.r_multiple))
    },
  },
  {
    title: 'MFE',
    key: 'mfe_r',
    width: 80,
    align: 'right',
    render: (r) => {
      if (r.mfe_r == null) return '—'
      return h('span', { style: `color:${rColor(r.mfe_r)}` }, r.mfe_r.toFixed(2))
    },
  },
  {
    title: 'MAE',
    key: 'mae_r',
    width: 80,
    align: 'right',
    render: (r) => {
      if (r.mae_r == null) return '—'
      return h('span', { style: `color:${rColor(r.mae_r)}` }, r.mae_r.toFixed(2))
    },
  },
  { title: 'K线', key: 'bars_held', width: 70, align: 'right', render: (r) => r.bars_held ?? '—' },
  {
    title: '换月',
    key: 'rollover_crossed',
    width: 70,
    render: (r) => (r.rollover_crossed ? '是' : '—'),
  },
  {
    title: '缺口',
    key: 'gap_crossed_entry',
    width: 70,
    render: (r) => {
      const parts: string[] = []
      if (r.gap_crossed_entry) parts.push('入')
      if (r.gap_crossed_exit) parts.push('出')
      return parts.length ? parts.join('/') : '—'
    },
  },
  {
    title: '量能',
    key: 'trigger_volume_ratio',
    width: 80,
    align: 'right',
    render: (r) => (r.trigger_volume_ratio == null ? '—' : r.trigger_volume_ratio.toFixed(2)),
  },
  { title: '触发时间', key: 'trigger_ts', width: 150, render: (r) => r.trigger_ts ?? '—' },
  {
    title: '持仓评分',
    key: 'hold_score',
    width: 80,
    align: 'right',
    render: (r) => (r.hold_score == null ? '—' : r.hold_score.toFixed(2)),
  },
  {
    title: 'b/a速度',
    key: 'speed_ratio',
    width: 90,
    align: 'right',
    render: (r) =>
      r.a_move == null || r.b_move == null || r.a_move === 0
        ? '—'
        : (r.b_move / r.a_move).toFixed(2),
  },
  {
    title: '根数比',
    key: 'bar_ratio',
    width: 80,
    align: 'right',
    render: (r) =>
      r.a_bars == null || r.b_bars == null || r.a_bars === 0
        ? '—'
        : (r.b_bars / r.a_bars).toFixed(2),
  },
  {
    title: '追价深度',
    key: 'overshoot_r',
    width: 100,
    align: 'right',
    render: (r) => (r.overshoot_r == null ? '—' : `${r.overshoot_r.toFixed(2)}R`),
  },
  {
    title: '净R',
    key: 'net_r',
    width: 90,
    align: 'right',
    render: (r) => {
      if (r.net_r == null) return '—'
      return h('span', { style: `color:${rColor(r.net_r)};font-weight:600` }, fmtR(r.net_r))
    },
  },
]

const directionOptions = [
  { label: '做多', value: 'up' },
  { label: '做空', value: 'down' },
]
const levelOptions = [
  { label: '精细', value: 'fine' },
  { label: '较大', value: 'large' },
  { label: '箱体', value: 'box' },
]
const versionOptions = [
  { label: '4.0', value: '4' },
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
  { label: '数据不足', value: 'insufficient_data' },
  { label: '换月', value: 'rollover' },
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

let symbolTimer: ReturnType<typeof setTimeout> | undefined
function onSymbolInput() {
  if (symbolTimer) clearTimeout(symbolTimer)
  symbolTimer = setTimeout(() => {
    void review.setRecentFilter({ symbol: symbolInput.value.trim() })
  }, 400)
}

let scoreTimer: ReturnType<typeof setTimeout> | undefined
function onScoreRangeInput() {
  if (scoreTimer) clearTimeout(scoreTimer)
  scoreTimer = setTimeout(() => {
    void review.setScoreRange(scoreMinInput.value, scoreMaxInput.value)
  }, 400)
}

async function resetFilters() {
  if (scoreTimer) clearTimeout(scoreTimer)
  scoreMinInput.value = null
  scoreMaxInput.value = null
  symbolInput.value = ''
  await review.resetRecentFilters()
}

/** 点击明细行：通知主窗口打开对应K线图并重绘形态与进出场点位，同时聚焦主窗口 */
async function openReviewChart(row: OutcomeDetail) {
  try {
    const payload: OpenReviewChartPayload = {
      symbol: row.symbol,
      eventId: row.event_id,
      filters: { ...review.recentFilters },
    }
    await emit('open-review-chart', payload)
    const main = await WebviewWindow.getByLabel('main')
    if (main != null) {
      await main.show()
      await main.unminimize()
      await main.setFocus()
    }
  } catch (e) {
    notify.error(String(e))
  }
}

function rowProps(row: OutcomeDetail) {
  return {
    style: 'cursor: pointer',
    onClick: () => openReviewChart(row),
  }
}

let unlisten: (() => void) | null = null

onMounted(async () => {
  try {
    unlisten = await onScanCompleted(() => {
      void load()
    })
  } catch {
    // 旧构建/权限缺失时监听失败不应阻塞数据加载
  }
  // 打开窗口即触发一次回填并加载，避免首次打开出现空表
  await refresh()
})

onBeforeUnmount(() => {
  if (symbolTimer) clearTimeout(symbolTimer)
  if (scoreTimer) clearTimeout(scoreTimer)
  unlisten?.()
})
</script>

<template>
  <div class="page">
    <n-card size="small" class="head-card">
      <n-space justify="space-between" align="center" style="width: 100%">
        <n-text strong style="font-size: 16px">复盘统计</n-text>
        <n-space align="center" :size="10">
          <n-text depth="3" style="font-size: 12px">口径</n-text>
          <n-select
            v-model:value="review.statsScope"
            :options="REVIEW_STATS_SCOPES.map((s) => ({ label: s.label, value: s.key }))"
            size="small"
            style="width: 110px"
            @update:value="(v: string) => load(undefined, v as StatsScopeKey)"
          />
          <n-select
            v-model:value="review.dimension"
            :options="REVIEW_DIMENSIONS.map((d) => ({ label: d.label, value: d.key }))"
            size="small"
            style="width: 170px"
            @update:value="() => load()"
          />
          <n-button size="small" type="primary" :loading="review.refreshing" @click="refresh">
            刷新
          </n-button>
          <n-button size="small" :loading="rebuilding" @click="rebuildAll">
            重建全部识别
          </n-button>
        </n-space>
      </n-space>
      <n-text v-if="error" type="error" style="display: block; margin-top: 8px">{{ error }}</n-text>
      <n-text depth="3" style="display: block; font-size: 12px; margin-top: 8px">
        止盈口径：第一目标 1R；目标位R&gt;1 时第二目标=0.8×目标R（不低于1R），触及第二目标或从1R回落则止盈；目标位R≤1 时达到 1R 才止盈。同一结构跨扫描只保留首条；未触发不计胜率；同根K线双触按止损；不含手续费/滑点，另有净R估算（固定 2.5 tick/往返）。
      </n-text>
    </n-card>

    <n-card size="small" class="v2-card" style="margin-bottom: 16px">
      <template #header>
        <span style="font-weight: 600">V2 概率模型 <span style="font-weight:400;color:#97a0b3;font-size:12px">Setup 形态评分不含触发K · Trigger K 收盘冻结</span></span>
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
          <n-tag v-else type="warning" size="small">暂无模型，请先运行 v2-train</n-tag>
        </n-space>
        <n-text v-if="review.v2Report" depth="3" style="font-size:12px;white-space:pre-wrap;max-height:220px;overflow:auto;display:block;background:#f8f9fb;padding:8px;border-radius:6px">{{ (review.v2Report["logistic_report.md"] || review.v2Report["acceptance.md"] || "").slice(0, 4000) || "暂无报告" }}</n-text>
        <n-data-table
          v-if="review.v2Predictions.length"
          :columns="v2Columns"
          :data="review.v2Predictions"
          :pagination="{ pageSize: 8 }"
          size="small"
          striped
          style="margin-top: 4px"
        />
        <n-text depth="3" style="font-size:11px">说明：Setup 阶段只用警示K冻结的形态/A段/B段/回撤特征，不包含Trigger K；Trigger特征在K收盘时冻结，无未来泄漏；P(win)为纯Rust Logistic/GAM推理，详见docs/v2_spec.md</n-text>
      </n-space>
    </n-card>

    <div class="body">
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

      <n-card
        size="small"
        title="最近信号明细"
        class="details-card"
        :content-style="{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }"
      >
        <div class="filter-bar">
          <n-input
            v-model:value="symbolInput"
            size="small"
            clearable
            placeholder="品种，如 RB"
            style="width: 140px"
            @update:value="onSymbolInput"
          />
          <n-select
            v-model:value="review.recentFilters.direction"
            size="small"
            clearable
            placeholder="方向"
            style="width: 100px"
            :options="directionOptions"
            @update:value="() => review.loadRecent()"
          />
          <n-select
            v-model:value="review.recentFilters.version"
            size="small"
            clearable
            placeholder="版本"
            style="width: 90px"
            :options="versionOptions"
            @update:value="() => review.load()"
          />
          <n-select
            v-model:value="review.recentFilters.level"
            size="small"
            clearable
            placeholder="级别"
            style="width: 100px"
            :options="levelOptions"
            @update:value="() => review.loadRecent()"
          />
          <n-select
            v-model:value="review.recentFilters.grade"
            size="small"
            clearable
            placeholder="等级"
            style="width: 110px"
            :options="gradeOptions"
            @update:value="() => review.loadRecent()"
          />
          <n-select
            v-model:value="review.recentFilters.outcome"
            size="small"
            clearable
            placeholder="结局"
            style="width: 110px"
            :options="outcomeOptions"
            @update:value="() => review.loadRecent()"
          />
          <n-button size="small" @click="resetFilters">重置</n-button>
        </div>
        <n-data-table
          class="details-table"
          :columns="recentColumns"
          :data="review.recent"
          :row-props="rowProps"
          size="small"
          :bordered="false"
          :scroll-x="2900"
          flex-height
          :loading="loading || review.recentLoading"
        />
        <n-empty
          v-if="!loading && !review.recent.length"
          description="暂无明细"
          style="margin-top: 12px"
        />
      </n-card>
    </div>
  </div>
</template>

<style scoped>
.page {
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 16px;
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
  flex: 1;
  min-height: 0;
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
</style>
