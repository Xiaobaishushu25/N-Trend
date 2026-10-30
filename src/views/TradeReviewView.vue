<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { isTauri } from '@tauri-apps/api/core'
import { emit } from '@tauri-apps/api/event'
import { getCurrentWindow } from '@tauri-apps/api/window'
import { WebviewWindow } from '@tauri-apps/api/webviewWindow'
import {
  NButton,
  NEmpty,
  NIcon,
  NInput,
  NRadioGroup,
  NRadioButton,
  NSelect,
  NSpin,
  NTag,
} from 'naive-ui'
import {
  ArrowDown,
  ArrowUp,
  ChartLine,
  Check,
  Clock,
  Download,
  Hourglass,
  Refresh,
  Search,
  Target,
  X,
} from '@vicons/tabler'
import {
  useTradeReviewStore,
  type TradeReviewStage,
  type TradeReviewTicket,
} from '../stores/tradeReview'
import { formatPnlAmount, formatPnlPoints, formatRMultiple } from '../utils/multipliers'
import { notify } from '../utils/notify'
import { openReviewWindow } from '../utils/openReviewWindow'
import type { OpenReviewChartPayload } from '../types'
import SignalQuality from '../components/SignalQuality.vue'
import SignalQualityControls from '../components/SignalQualityControls.vue'
import { compareQuality } from '../utils/signalQuality'
import { useSignalQualityStore } from '../stores/signalQuality'

const router = useRouter()
const review = useTradeReviewStore()
const qualityDisplay = useSignalQualityStore()
const qualitySort = ref(false)
const displayedTickets = computed(() => qualitySort.value && qualityDisplay.enabled ? [...review.filteredTickets].sort((a,b) => compareQuality(a.raw.quality,b.raw.quality)) : review.filteredTickets)

function onHeaderMouseDown(e: MouseEvent) {
  if (!isTauri() || e.button !== 0) return
  const target = e.target as Element | null
  if (target?.closest('button, input, select, [role="button"], a, .n-base-selection, .n-radio-group')) return
  e.preventDefault()
  const win = getCurrentWindow()
  if (e.detail === 2) {
    void win.toggleMaximize()
  } else {
    void win.startDragging()
  }
}

const stageTabs = [
  { label: '全部交易', value: 'all' },
  { label: '持仓中', value: 'open' },
  { label: '已平仓', value: 'closed' },
  { label: '待触发暂存池', value: 'staged' },
]

const exitReasonOptions = [
  { label: '全部平仓原因', value: '' },
  { label: '🎯 打到止盈点', value: 'target' },
  { label: '🛑 打到止损点', value: 'stop' },
  { label: '⏳ 时间止损', value: 'time_exit' },
  { label: '🔄 换月/结构破坏', value: 'other' },
]

const directionOptions = [
  { label: '全部方向', value: '' },
  { label: '仅做多', value: 'up' },
  { label: '仅做空', value: 'down' },
]

const lotOptions = [
  { label: '1 手', value: 1 },
  { label: '2 手', value: 2 },
  { label: '5 手', value: 5 },
  { label: '10 手', value: 10 },
]

onMounted(async () => {
  await review.load()
  review.initAutoRefresh()
})

onBeforeUnmount(() => {
  review.destroyAutoRefresh()
})

function fmtPrice(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '—'
  return Number.isInteger(v) ? v.toFixed(0) : v.toFixed(1)
}

function dirLabel(direction: string): string {
  return direction === 'up' ? '做多' : '做空'
}

function levelLabel(level: string): string {
  return level === 'fine' ? '精细' : level === 'large' ? '较大' : level === 'box' ? '箱体' : level
}

function exitReasonBadge(reason: string) {
  switch (reason) {
    case 'target':
      return { text: '🎯 达成止盈', type: 'error' as const, tip: '触及预期目标位或移动止盈' }
    case 'stop':
      return { text: '🛑 击穿止损', type: 'success' as const, tip: '价格达到初始保护止损点' }
    case 'time_exit':
      return { text: '⏳ 时间止损', type: 'warning' as const, tip: '达到最长持仓K线数，动能衰竭退出' }
    case 'rollover':
      return { text: '🔄 换月退出', type: 'info' as const, tip: '合约换月平仓' }
    case 'no_follow':
      return { text: '⚠️ 无跟随退出', type: 'warning' as const, tip: '突破后无后续资金跟随，提早离场' }
    default:
      return { text: reason ? `离场(${reason})` : '已平仓', type: 'default' as const, tip: '' }
  }
}

/** 一键图表复盘：聚焦主窗口并绘制开平仓打点 */
async function openChartForTicket(ticket: TradeReviewTicket) {
  try {
    const payload: OpenReviewChartPayload = {
      symbol: ticket.symbol,
      eventId: ticket.event_id,
      filters: {},
    }
    if (isTauri()) {
      await emit('open-review-chart', payload)
      try {
        const main = await WebviewWindow.getByLabel('main')
        if (main != null) {
          await main.show()
          await main.unminimize()
          await main.setFocus()
        }
      } catch (err) {
        console.warn('获取主窗口失败:', err)
      }
    }

    const isMainWin = !isTauri() || getCurrentWindow().label === 'main'
    if (isMainWin) {
      void router.push({
        name: 'chart',
        params: { symbol: ticket.symbol },
        query: { review: String(ticket.event_id) },
      })
    }
    notify.success(`已在图表上定位 [${ticket.symbol_name || ticket.symbol}] 复盘打点`)
  } catch (e) {
    notify.error(`定位图表失败: ${e}`)
  }
}
</script>

<template>
  <div class="review-page">
    <!-- 1. 顶部工作台标题栏与核心指标看板（支持拖拽与双击最大化） -->
    <header class="review-header" @mousedown="onHeaderMouseDown">
      <div class="header-main-row">
        <div class="header-title-box">
          <h1 class="header-title">量化交易复盘工作台</h1>
          <span class="header-subtitle">交易全生命周期 · 盈亏归因 · 策略账本</span>
        </div>
        <div class="header-action-group">
          <n-button size="small" type="primary" secondary @click="openReviewWindow">形态与评分筛选</n-button>
          <n-button size="small" secondary :loading="review.refreshing" @click="review.refresh">
            <template #icon>
              <n-icon :component="Refresh" />
            </template>
            刷新数据
          </n-button>
          <n-button size="small" secondary type="primary" @click="review.exportCsv">
            <template #icon>
              <n-icon :component="Download" />
            </template>
            导出量化账本 (CSV)
          </n-button>
        </div>
      </div>

      <!-- KPI 指标汇总卡条 -->
      <div class="kpi-strip">
        <div class="kpi-item">
          <span class="kpi-label">已结算交易</span>
          <span class="kpi-val">{{ review.metrics.totalSettled }} 笔</span>
        </div>
        <div class="kpi-divider"></div>
        <div class="kpi-item">
          <span class="kpi-label">实盘胜率</span>
          <span
            class="kpi-val"
            :class="review.metrics.winRate >= 0.5 ? 'is-up' : 'is-down'"
          >
            {{ (review.metrics.winRate * 100).toFixed(1) }}%
            <span class="kpi-sub">({{ review.metrics.winCount }}胜 / {{ review.metrics.lossCount }}负)</span>
          </span>
        </div>
        <div class="kpi-divider"></div>
        <div class="kpi-item">
          <span class="kpi-label">累计净盈亏 (点)</span>
          <span
            class="kpi-val font-num"
            :class="review.metrics.totalPoints >= 0 ? 'is-up' : 'is-down'"
          >
            {{ formatPnlPoints(review.metrics.totalPoints) }}
          </span>
        </div>
        <div class="kpi-divider"></div>
        <div class="kpi-item">
          <span class="kpi-label">累计净盈亏 (元)</span>
          <span
            class="kpi-val font-num"
            :class="review.metrics.totalAmount >= 0 ? 'is-up' : 'is-down'"
          >
            {{ formatPnlAmount(review.metrics.totalAmount) }}
          </span>
        </div>
        <div class="kpi-divider"></div>
        <div class="kpi-item">
          <span class="kpi-label">盈亏比 (Payoff)</span>
          <span class="kpi-val font-num">{{ review.metrics.payoffRatio.toFixed(2) }}</span>
        </div>
        <div class="kpi-divider"></div>
        <div class="kpi-item">
          <span class="kpi-label">累计 R 倍数</span>
          <span
            class="kpi-val font-num"
            :class="review.metrics.totalR >= 0 ? 'is-up' : 'is-down'"
          >
            {{ formatRMultiple(review.metrics.totalR) }}
          </span>
        </div>
      </div>
    </header>

    <!-- 2. 分类分段切换标签与快速过滤器 -->
    <div class="filter-toolbar">
      <div class="tabs-row">
        <n-radio-group v-model:value="review.activeTab" size="small" class="stage-tabs">
          <n-radio-button value="all">
            全部交易 ({{ review.tickets.filter((t) => t.stage === 'open' || t.stage === 'closed').length }})
          </n-radio-button>
          <n-radio-button value="open">
            <span class="tab-with-dot">
              <span v-if="review.metrics.openCount > 0" class="live-dot"></span>
              持仓中 ({{ review.metrics.openCount }})
            </span>
          </n-radio-button>
          <n-radio-button value="closed">
            已平仓 ({{ review.metrics.totalSettled }})
          </n-radio-button>
          <n-radio-button value="staged">
            待触发暂存池 ({{ review.metrics.stagedCount }})
          </n-radio-button>
        </n-radio-group>
      </div>

      <div class="filter-inputs">
        <n-input
          v-model:value="review.searchKeyword"
          placeholder="搜索代码或品种名..."
          size="small"
          clearable
          style="width: 170px"
        >
          <template #prefix>
            <n-icon :component="Search" />
          </template>
        </n-input>

        <n-select
          v-model:value="review.filterDirection"
          :options="directionOptions"
          size="small"
          placeholder="方向"
          style="width: 110px"
        />

        <n-select
          v-if="review.activeTab !== 'staged'"
          v-model:value="review.filterExitReason"
          :options="exitReasonOptions"
          size="small"
          placeholder="平仓原因"
          style="width: 140px"
        />

        <SignalQualityControls v-model:sort="qualitySort" />
        <div class="lots-selector"><span class="lots-label">按冻结策略手数统计；费用为研究估算</span></div>
      </div>
    </div>

    <!-- 3. 卡片时间流列表 -->
    <main class="tickets-container">
      <div v-if="review.loading" class="state-placeholder">
        <n-spin size="medium" />
        <span class="placeholder-text">加载复盘交易数据中...</span>
      </div>

      <div v-else-if="!review.filteredTickets.length" class="state-placeholder">
        <n-empty
          :description="
            review.activeTab === 'staged'
              ? '当前暂存池无待触发预警（所有预警均已触发入场或已失效归档）'
              : '暂无符合筛选条件的交易记录'
          "
        />
      </div>

      <div v-else class="tickets-feed">
        <article
          v-for="ticket in displayedTickets"
          :key="ticket.event_id"
          class="ticket-card"
          :class="[
            `stage-${ticket.stage}`,
            ticket.stage === 'closed'
              ? ticket.is_win
                ? 'is-pnl-win'
                : 'is-pnl-loss'
              : '',
          ]"
        >
          <!-- 卡片顶行：状态图标、品种、方向、进出场总览、右侧大字盈亏 -->
          <div class="card-head">
            <div class="head-left">
              <!-- 图标 -->
              <span
                class="status-icon"
                :class="{
                  'is-win': ticket.stage === 'closed' && ticket.is_win,
                  'is-loss': ticket.stage === 'closed' && !ticket.is_win,
                  'is-open': ticket.stage === 'open',
                  'is-staged': ticket.stage === 'staged',
                }"
              >
                <n-icon
                  v-if="ticket.stage === 'closed' && ticket.is_win"
                  :component="Check"
                  size="15"
                />
                <n-icon
                  v-else-if="ticket.stage === 'closed' && !ticket.is_win"
                  :component="X"
                  size="15"
                />
                <n-icon
                  v-else-if="ticket.stage === 'open'"
                  :component="Target"
                  size="15"
                />
                <n-icon v-else :component="Hourglass" size="15" />
              </span>

              <!-- 品种代码与名称 -->
              <div class="symbol-meta">
                <span class="symbol-code">{{ ticket.symbol }}</span>
                <span class="symbol-name">{{ ticket.symbol_name }}</span>
              </div>

              <!-- 方向胶囊 -->
              <span
                class="dir-badge"
                :class="ticket.direction === 'up' ? 'is-up' : 'is-down'"
              >
                <n-icon
                  :component="ticket.direction === 'up' ? ArrowUp : ArrowDown"
                  size="12"
                />
                {{ dirLabel(ticket.direction) }}
              </span>

              <!-- 形态评分标签 -->
              <span class="spec-tag">{{ levelLabel(ticket.level) }}N · {{ ticket.grade }}</span>
              <span class="score-tag">评分 {{ ticket.entry_score.toFixed(2) }}</span>

              <SignalQuality :quality="ticket.raw.quality" :old-score="ticket.entry_score" :rr="ticket.raw.rr" />

              <!-- 合约乘数提示 -->
              <span class="multiplier-tag">{{ ticket.multiplier }}吨/点</span>
            </div>

            <!-- 右侧盈亏核算区域（针对已平仓 / 持仓中） -->
            <div class="head-right">
              <template v-if="ticket.stage === 'closed'">
                <div class="pnl-main-box">
                  <span
                    class="pnl-points font-num"
                    :class="ticket.is_win ? 'is-up' : 'is-down'"
                  >
                    {{ formatPnlPoints(ticket.pnl_points) }}
                  </span>
                  <span
                    class="pnl-amount font-num"
                    :class="ticket.is_win ? 'is-up' : 'is-down'"
                  >
                    {{ formatPnlAmount(ticket.pnl_amount) }}
                  </span>
                </div>
                <div class="pnl-sub-box">
                  <span class="pnl-r font-num" :class="ticket.is_win ? 'is-up' : 'is-down'">
                    {{ formatRMultiple(ticket.r_multiple) }}
                  </span>
                  <span class="pnl-pct font-num">
                    ({{ ticket.pnl_pct >= 0 ? '+' : '' }}{{ ticket.pnl_pct.toFixed(2) }}%)
                  </span>
                </div>
              </template>

              <template v-else-if="ticket.stage === 'open'">
                <div class="pnl-main-box">
                  <span class="live-status-tag">持仓监控中</span>
                </div>
                <div class="pnl-sub-box">
                  <span class="live-ts">入场: {{ ticket.entry_ts?.slice(5, 16) || '—' }}</span>
                </div>
              </template>

              <template v-else-if="ticket.stage === 'staged'">
                <div class="pnl-main-box">
                  <span class="staged-status-tag">预警暂存 · 待突破</span>
                </div>
                <div class="pnl-sub-box">
                  <span class="staged-target font-num">触发价 {{ fmtPrice(ticket.planned_entry) }}</span>
                </div>
              </template>
            </div>
          </div>

          <!-- 卡片主体：开平仓因果链三段式生命周期 -->
          <div class="card-timeline">
            <!-- 阶段 1：预警溯源 -->
            <div class="timeline-step">
              <span class="step-pill is-warning">预警溯源</span>
              <span class="step-time font-num">{{ ticket.warning_ts || ticket.created_at }}</span>
              <span class="step-desc">
                识别到 {{ levelLabel(ticket.level) }}N 结构，计划入场位
                <b class="highlight-val">{{ fmtPrice(ticket.planned_entry) }}</b>，
                初始止损位 <b class="highlight-val">{{ fmtPrice(ticket.planned_stop) }}</b>
                (单笔风险 {{ ticket.initial_risk_points.toFixed(1) }} 点)，
                计划第一目标位 {{ fmtPrice(ticket.planned_target) }}
              </span>
            </div>

            <!-- 阶段 2：开仓执行（暂存预警阶段不展示） -->
            <div v-if="ticket.stage !== 'staged'" class="timeline-step">
              <span class="step-pill is-entry">开仓执行</span>
              <span class="step-time font-num">{{ ticket.entry_ts || '—' }}</span>
              <span class="step-desc">
                价格突破触发入场，实际成交
                <b class="highlight-val">{{ fmtPrice(ticket.entry_price) }}</b>
                <template v-if="ticket.trigger_volume_ratio != null">
                  · 放量确认 {{ ticket.trigger_volume_ratio.toFixed(2) }}倍
                </template>
                <template v-if="ticket.overshoot_r != null && ticket.overshoot_r > 0">
                  · 追价深度 {{ ticket.overshoot_r.toFixed(2) }}R
                </template>
                · 开仓 {{ ticket.lots }} 手
              </span>
            </div>

            <!-- 阶段 3：平仓归因（已平仓订单） -->
            <div v-if="ticket.stage === 'closed'" class="timeline-step">
              <span class="step-pill is-exit">平仓归因</span>
              <span class="step-time font-num">{{ ticket.exit_ts || '—' }}</span>
              <div class="step-exit-content">
                <n-tag
                  size="small"
                  :type="exitReasonBadge(ticket.exit_reason).type"
                  class="exit-reason-tag"
                >
                  {{ exitReasonBadge(ticket.exit_reason).text }}
                </n-tag>
                <span class="step-desc">
                  平仓成交价 <b class="highlight-val">{{ fmtPrice(ticket.exit_price) }}</b>
                  <template v-if="ticket.bars_held != null">
                    · 持仓 {{ ticket.bars_held }} 根K线 (约 {{ ticket.bars_held * 15 }} 分钟)
                  </template>
                  <template v-if="ticket.mfe_r != null">
                    · 最大有利浮盈 (MFE) {{ ticket.mfe_r.toFixed(2) }}R
                  </template>
                  <template v-if="ticket.mae_r != null">
                    · 最大不利回撤 (MAE) {{ ticket.mae_r.toFixed(2) }}R
                  </template>
                </span>
              </div>
            </div>

            <!-- 阶段 3B：持仓动态追踪（针对当前持仓订单） -->
            <div v-else-if="ticket.stage === 'open'" class="timeline-step">
              <span class="step-pill is-live">动态追踪</span>
              <span class="step-desc">
                正在持仓监控中，目标止盈位 {{ fmtPrice(ticket.planned_target) }}，
                保护止损位 {{ fmtPrice(ticket.planned_stop) }}，
                <template v-if="ticket.mfe_r != null">
                  最高浮盈到达 {{ ticket.mfe_r.toFixed(2) }}R
                </template>
              </span>
            </div>

            <!-- 阶段 3C：暂存预警待触发说明（针对暂存池订单） -->
            <div v-else-if="ticket.stage === 'staged'" class="timeline-step">
              <span class="step-pill is-staged">暂存等待</span>
              <span class="step-desc">
                形态预警已暂存至缓冲池，避免干扰正常交易；若价格突破
                <b class="highlight-val">{{ fmtPrice(ticket.planned_entry) }}</b>
                将自动激活晋升为正式开仓单并向您发出开仓通知。
              </span>
            </div>
          </div>

          <!-- 卡片底栏：快捷操作工具条 -->
          <div class="card-footer">
            <div class="footer-left">
              <span class="record-id">事件编号: #{{ ticket.event_id }}</span>
            </div>
            <div class="footer-right">
              <n-button
                size="tiny"
                secondary
                type="primary"
                @click="openChartForTicket(ticket)"
              >
                <template #icon>
                  <n-icon :component="ChartLine" />
                </template>
                查看图表复盘打点
              </n-button>
            </div>
          </div>
        </article>
      </div>
    </main>
  </div>
</template>

<style scoped>
.review-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: #f8fafc;
  color: #1e293b;
  box-sizing: border-box;
}

/* 顶部标题与指标看板 */
.review-header {
  flex: none;
  background: #ffffff;
  border-bottom: 1px solid #e2e8f0;
  padding: 14px 20px 12px;
}
.header-main-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}
.header-title-box {
  display: flex;
  align-items: baseline;
  gap: 12px;
}
.header-title {
  margin: 0;
  font-size: 19px;
  font-weight: 700;
  color: #0f172a;
}
.header-subtitle {
  font-size: 12px;
  color: #64748b;
}
.header-action-group {
  display: flex;
  align-items: center;
  gap: 10px;
}

/* KPI 统计条 */
.kpi-strip {
  display: flex;
  align-items: center;
  gap: 16px;
  background: #f1f5f9;
  border-radius: 8px;
  padding: 8px 16px;
  overflow-x: auto;
}
.kpi-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.kpi-label {
  font-size: 11px;
  color: #64748b;
  font-weight: 500;
}
.kpi-val {
  font-size: 15px;
  font-weight: 700;
  color: #0f172a;
  white-space: nowrap;
}
.kpi-sub {
  font-size: 11px;
  font-weight: normal;
  color: #94a3b8;
}
.kpi-divider {
  width: 1px;
  height: 24px;
  background: #cbd5e1;
}

/* 过滤工具栏 */
.filter-toolbar {
  flex: none;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 20px;
  background: #ffffff;
  border-bottom: 1px solid #e2e8f0;
  gap: 12px;
  flex-wrap: wrap;
}
.stage-tabs {
  background: #f1f5f9;
  padding: 2px;
  border-radius: 6px;
}
.tab-with-dot {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.live-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #3b82f6;
  animation: pulse 1.5s infinite;
}
@keyframes pulse {
  0% { transform: scale(0.9); opacity: 0.7; }
  50% { transform: scale(1.3); opacity: 1; }
  100% { transform: scale(0.9); opacity: 0.7; }
}
.filter-inputs {
  display: flex;
  align-items: center;
  gap: 10px;
}
.lots-selector {
  display: flex;
  align-items: center;
  gap: 6px;
}
.lots-label {
  font-size: 12px;
  color: #64748b;
  white-space: nowrap;
}

/* 列表主容器 */
.tickets-container {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px;
}
.state-placeholder {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 260px;
  gap: 12px;
}
.placeholder-text {
  font-size: 13px;
  color: #94a3b8;
}

.tickets-feed {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* 交易卡片 */
.ticket-card {
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 10px;
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.03);
  padding: 14px 16px 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  transition: all 0.2s ease;
}
.ticket-card:hover {
  border-color: #cbd5e1;
  box-shadow: 0 3px 8px rgba(15, 23, 42, 0.06);
}

/* 赢单左侧红条、亏单绿条 */
.ticket-card.is-pnl-win {
  border-left: 4px solid #ef4444;
}
.ticket-card.is-pnl-loss {
  border-left: 4px solid #10b981;
}
.ticket-card.stage-open {
  border-left: 4px solid #3b82f6;
}
.ticket-card.stage-staged {
  border-left: 4px solid #f59e0b;
}

/* 卡片头部 */
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.head-left {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.status-icon {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: #ffffff;
}
.status-icon.is-win {
  background: #ef4444;
}
.status-icon.is-loss {
  background: #10b981;
}
.status-icon.is-open {
  background: #3b82f6;
}
.status-icon.is-staged {
  background: #f59e0b;
}

.symbol-meta {
  display: flex;
  align-items: baseline;
  gap: 6px;
}
.symbol-code {
  font-size: 13px;
  color: #64748b;
  font-family: monospace;
}
.symbol-name {
  font-size: 16px;
  font-weight: 700;
  color: #0f172a;
}

.dir-badge {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: 12px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 999px;
}
.dir-badge.is-up {
  color: #ef4444;
  background: rgba(239, 68, 68, 0.1);
}
.dir-badge.is-down {
  color: #10b981;
  background: rgba(16, 185, 129, 0.1);
}

.spec-tag,
.score-tag,
.multiplier-tag {
  font-size: 12px;
  font-weight: 500;
  padding: 2px 7px;
  border-radius: 999px;
  background: #f1f5f9;
  color: #475569;
}
.score-tag {
  color: #7c3aed;
  background: rgba(124, 58, 237, 0.08);
}
.multiplier-tag {
  color: #0284c7;
  background: rgba(2, 132, 199, 0.08);
}

/* 右侧盈亏 */
.head-right {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
}
.pnl-main-box {
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.pnl-points {
  font-size: 17px;
  font-weight: 700;
}
.pnl-amount {
  font-size: 17px;
  font-weight: 800;
}
.pnl-sub-box {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}
.pnl-r {
  font-weight: 600;
}
.pnl-pct {
  color: #64748b;
}

.live-status-tag {
  font-size: 13px;
  font-weight: 700;
  color: #3b82f6;
  background: rgba(59, 130, 246, 0.1);
  padding: 3px 10px;
  border-radius: 6px;
}
.live-ts {
  font-size: 11px;
  color: #94a3b8;
}
.staged-status-tag {
  font-size: 13px;
  font-weight: 700;
  color: #d97706;
  background: rgba(245, 158, 11, 0.1);
  padding: 3px 10px;
  border-radius: 6px;
}
.staged-target {
  font-size: 12px;
  color: #d97706;
}

/* 时间轴生命周期 */
.card-timeline {
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding: 8px 10px;
  background: #f8fafc;
  border-radius: 6px;
  border: 1px dashed #e2e8f0;
}
.timeline-step {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  line-height: 1.4;
  color: #334155;
  flex-wrap: wrap;
}
.step-pill {
  flex: none;
  font-size: 11px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 4px;
}
.step-pill.is-warning {
  color: #7c3aed;
  background: rgba(124, 58, 237, 0.1);
}
.step-pill.is-entry {
  color: #0284c7;
  background: rgba(2, 132, 199, 0.1);
}
.step-pill.is-exit {
  color: #b91c1c;
  background: rgba(185, 28, 28, 0.1);
}
.step-pill.is-live {
  color: #2563eb;
  background: rgba(37, 99, 235, 0.1);
}
.step-pill.is-staged {
  color: #d97706;
  background: rgba(217, 119, 6, 0.1);
}
.step-time {
  font-size: 12px;
  color: #94a3b8;
}
.step-desc {
  color: #475569;
}
.step-exit-content {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.highlight-val {
  color: #0f172a;
}

/* 卡片底栏 */
.card-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-top: 4px;
  font-size: 12px;
  color: #94a3b8;
}
.record-id {
  font-family: monospace;
}

/* 红涨绿跌色彩规范（中国期货惯例） */
.is-up {
  color: #ef4444 !important;
}
.is-down {
  color: #10b981 !important;
}
.font-num {
  font-variant-numeric: tabular-nums;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
}
</style>
