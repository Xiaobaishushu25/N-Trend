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
  NSelect,
  NSpace,
  NTag,
  NText,
  type DataTableColumns,
} from 'naive-ui'
import { onScanCompleted } from '../services/api'
import { REVIEW_DIMENSIONS, fmtPct, fmtR, useReviewStore } from '../stores/review'
import { notify } from '../utils/notify'
import type { GroupStat, OutcomeDetail } from '../types'

const review = useReviewStore()
const loading = ref(false)
const error = ref('')
/** 品种筛选本地输入（防抖后再生效） */
const symbolInput = ref('')
/** 评分段筛选（映射为 scoreMin/scoreMax） */
const scoreBand = ref<string | null>(null)

const dirLabel = (d: string) => (d === 'up' ? '做多' : d === 'down' ? '做空' : d)
const levelLabel = (l: string) => (l === 'fine' ? '精细' : l === 'large' ? '较大' : l)

const outcomeLabel: Record<string, { text: string; type: 'success' | 'error' | 'warning' | 'default' }> = {
  win: { text: '盈利', type: 'error' },
  loss: { text: '亏损', type: 'success' },
  no_trigger: { text: '未触发', type: 'default' },
  open: { text: '持仓中', type: 'warning' },
  insufficient_data: { text: '数据不足', type: 'default' },
}

const exitLabel: Record<string, string> = {
  stop: '止损',
  target: '止盈',
  no_follow: '无跟随退出',
  time_exit: '时间退出',
  '': '—',
}

/** 绿跌红涨：盈利/正向用红，亏损/负向用绿 */
const rColor = (v: number | null | undefined) => (v == null || v >= 0 ? '#e03131' : '#0f9d58')

function outcomeTag(outcome: string) {
  const meta = outcomeLabel[outcome] ?? { text: outcome, type: 'default' as const }
  return h(NTag, { type: meta.type, size: 'small' }, { default: () => meta.text })
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
  { title: '在途', key: 'pending', width: 70, align: 'right' },
  { title: '未触发', key: 'no_trigger', width: 80, align: 'right' },
]

const recentColumns: DataTableColumns<OutcomeDetail> = [
  { title: 'ID', key: 'signal_id', width: 70 },
  { title: '品种', key: 'symbol', width: 80 },
  { title: '方向', key: 'direction', width: 70, render: (r) => dirLabel(r.direction) },
  { title: '级别', key: 'level', width: 70, render: (r) => levelLabel(r.level) },
  { title: '等级', key: 'grade', width: 90 },
  {
    title: '评分',
    key: 'score',
    width: 80,
    align: 'right',
    render: (r) => r.score.toFixed(2),
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
    title: '量能',
    key: 'vol_ratio',
    width: 80,
    align: 'right',
    render: (r) => (r.vol_ratio == null ? '—' : r.vol_ratio.toFixed(2)),
  },
  {
    title: '增仓',
    key: 'oi_increase',
    width: 70,
    render: (r) => (r.oi_increase == null ? '—' : r.oi_increase ? '是' : '否'),
  },
  {
    title: '60m分',
    key: 'trend60_score',
    width: 80,
    align: 'right',
    render: (r) => (r.trend60_score == null ? '—' : r.trend60_score.toFixed(2)),
  },
]

const directionOptions = [
  { label: '做多', value: 'up' },
  { label: '做空', value: 'down' },
]
const levelOptions = [
  { label: '精细', value: 'fine' },
  { label: '较大', value: 'large' },
]
const gradeOptions = [
  { label: 'A级', value: 'A级' },
  { label: 'B级', value: 'B级' },
  { label: 'C级', value: 'C级' },
  { label: '回撤过浅', value: '回撤过浅' },
  { label: '回撤过深', value: '回撤过深' },
]
const scoreOptions = [
  { label: '<2.5', value: '<2.5' },
  { label: '2.5-3.5', value: '2.5-3.5' },
  { label: '3.5-5.0', value: '3.5-5.0' },
]
const outcomeOptions = [
  { label: '盈利', value: 'win' },
  { label: '亏损', value: 'loss' },
  { label: '持仓中', value: 'open' },
  { label: '未触发', value: 'no_trigger' },
  { label: '数据不足', value: 'insufficient_data' },
]

async function load() {
  loading.value = true
  error.value = ''
  try {
    await review.load()
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

let symbolTimer: ReturnType<typeof setTimeout> | undefined
function onSymbolInput() {
  if (symbolTimer) clearTimeout(symbolTimer)
  symbolTimer = setTimeout(() => {
    void review.setRecentFilter({ symbol: symbolInput.value.trim() })
  }, 400)
}

function applyScoreBand(v: string | null) {
  if (!v) {
    void review.setRecentFilter({ scoreMin: null, scoreMax: null })
  } else if (v === '<2.5') {
    void review.setRecentFilter({ scoreMin: null, scoreMax: 2.5 })
  } else if (v === '2.5-3.5') {
    void review.setRecentFilter({ scoreMin: 2.5, scoreMax: 3.5 })
  } else {
    void review.setRecentFilter({ scoreMin: 3.5, scoreMax: null })
  }
}

async function resetFilters() {
  scoreBand.value = ''
  symbolInput.value = ''
  await review.resetRecentFilters()
}

/** 点击明细行：通知主窗口打开对应K线图并重绘形态与进出场点位，同时聚焦主窗口 */
async function openReviewChart(row: OutcomeDetail) {
  try {
    await emit('open-review-chart', { symbol: row.symbol, signalId: row.signal_id })
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
  unlisten?.()
})
</script>

<template>
  <div class="page">
    <n-card size="small" class="head-card">
      <n-space justify="space-between" align="center" style="width: 100%">
        <n-text strong style="font-size: 16px">复盘统计</n-text>
        <n-space align="center" :size="10">
          <n-select
            v-model:value="review.dimension"
            :options="REVIEW_DIMENSIONS.map((d) => ({ label: d.label, value: d.key }))"
            size="small"
            style="width: 150px"
            @update:value="() => load()"
          />
          <n-button size="small" type="primary" :loading="review.refreshing" @click="refresh">
            刷新
          </n-button>
        </n-space>
      </n-space>
      <n-text v-if="error" type="error" style="display: block; margin-top: 8px">{{ error }}</n-text>
      <n-text depth="3" style="display: block; font-size: 12px; margin-top: 8px">
        止盈口径：第一目标 1R；目标位R&gt;1 时第二目标=0.8×目标R（不低于1R），触及第二目标或从1R回落则止盈；目标位R≤1 时达到 1R 才止盈。同一结构跨扫描只保留首条；未触发不计胜率；同根K线双触按止损；不含手续费/滑点。
      </n-text>
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

      <n-card size="small" title="分组统计" class="groups-card">
        <n-data-table
          :columns="groupColumns"
          :data="review.stats?.groups ?? []"
          size="small"
          :bordered="false"
          :loading="loading"
          max-height="240"
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
            v-model:value="scoreBand"
            size="small"
            clearable
            placeholder="评分"
            style="width: 110px"
            :options="scoreOptions"
            @update:value="applyScoreBand"
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
          :scroll-x="1500"
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
</style>
