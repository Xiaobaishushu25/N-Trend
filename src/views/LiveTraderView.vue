<script setup lang="ts">
import TraderInstancesPanel from '../components/TraderInstancesPanel.vue'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { isTauri } from '@tauri-apps/api/core'
import { getCurrentWindow } from '@tauri-apps/api/window'
import { emit } from '@tauri-apps/api/event'
import { WebviewWindow } from '@tauri-apps/api/webviewWindow'
import {
  NBadge,
  NButton,
  NDivider,
  NEmpty,
  NIcon,
  NModal,
  NPagination,
  NPopconfirm,
  NRadioGroup,
  NRadioButton,
  NSelect,
  NSpin,
  NTag,
  NTooltip,
} from 'naive-ui'
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Bolt,
  Check,
  CircleCheck,
  CircleX,
  Clock,
  ExternalLink,
  Eye,
  Flame,
  InfoCircle,
  LayersSubtract,
  Refresh,
  ShieldCheck,
  Target,
  Trash,
} from '@vicons/tabler'
import { useLiveTraderStore } from '../stores/liveTrader'
import StrategyComparisonPanel from '../components/StrategyComparisonPanel.vue'
import { useTradeReviewStore } from '../stores/tradeReview'
import { api } from '../services/api'
import { notify } from '../utils/notify'
import {
  calcSlippage,
  calcTradeRecordPnlAmount,
  formatPnlAmount,
  formatPnlPoints,
  formatRMultiple,
  getSymbolMultiplier,
} from '../utils/multipliers'
import type { LiveTradeRecord, OpenReviewChartPayload, ReviewSignalDetail } from '../types'

const trader = useLiveTraderStore()
const selectedInstance = computed(() => trader.instanceCatalog?.instances.find(r => r.instance.id === trader.selectedInstanceId))
const executionLabel = computed(() => selectedInstance.value?.account.kind === 'paper' ? '模拟' : '实盘')
const isNStrategy = computed(() => !selectedInstance.value || selectedInstance.value.strategy.strategy_id === 'n_pattern')
watch(() => trader.activeTab, (tab) => {
  if (tab === 'orders') void trader.loadExecutionOrders(1)
})
const review = useTradeReviewStore()

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

const exitReasonOptions = [
  { label: '全部平仓原因', value: '' },
  { label: '🎯 第一手 1.0R 止盈', value: 'target_1r' },
  { label: '📈 第二手阶梯追踪', value: 'trailing' },
  { label: '🛡️ 成本线保本出场', value: 'breakeven' },
  { label: '🛑 初始硬止损', value: 'stop' },
  { label: '⚠️ 第5根无跟随离场', value: 'no_follow' },
  { label: '⏳ 60根Bar超时出场', value: 'time_exit' },
  { label: '🏁 原形态目标S1', value: 'target_s1' },
]

const CHINESE_FUTURES_NAMES: Record<string, string> = {
  pb: '沪铅',
  rb: '螺纹钢',
  cu: '沪铜',
  al: '沪铝',
  zn: '沪锌',
  ni: '沪镍',
  sn: '沪锡',
  au: '沪金',
  ag: '沪银',
  hc: '热轧卷板',
  ss: '不锈钢',
  i: '铁矿石',
  j: '焦炭',
  jm: '焦煤',
  jd: '鸡蛋',
  m: '豆粕',
  y: '豆油',
  p: '棕榈油',
  c: '玉米',
  cs: '淀粉',
  v: 'PVC',
  pp: '聚丙烯',
  l: '塑料',
  eg: '乙二醇',
  eb: '苯乙烯',
  sa: '纯碱',
  fg: '玻璃',
  ma: '甲醇',
  ta: 'PTA',
  sc: '原油',
  fu: '燃料油',
  lu: '低硫燃料油',
  bu: '沥青',
  sp: '纸浆',
  ru: '天然橡胶',
  nr: '20号胶',
  br: '丁二烯橡胶',
  ec: '集运指数欧线',
  si: '工业硅',
  lc: '碳酸锂',
  lh: '生猪',
  ap: '苹果',
  pk: '花生',
  cj: '红枣',
  cf: '棉花',
  sr: '白糖',
  oi: '菜籽油',
  rm: '菜籽粕',
}

function getInstrumentName(inst: string): string {
  const code = inst.toLowerCase()
  let prefix = ''
  for (const ch of code) {
    if (ch >= 'a' && ch <= 'z') prefix += ch
    else break
  }
  const name = CHINESE_FUTURES_NAMES[prefix]
  if (name) {
    return `${name} ${code.slice(prefix.length).toUpperCase()}`
  }
  return inst.toUpperCase()
}

const theoreticalWinRate = ref<number | null>(null)

async function loadTheoreticalWinRate() {
  try {
    const stats = await api.getReviewStats('overall', 'all')
    if (stats?.overall?.win_rate != null) {
      theoreticalWinRate.value = stats.overall.win_rate * 100
    }
  } catch (e) {
    console.warn('获取理论复盘胜率失败:', e)
  }
}

onMounted(async () => {
  await trader.startRealtime()
  await trader.loadAll()
  if (trader.activeTab === 'orders') void trader.loadExecutionOrders(1)
  void loadTheoreticalWinRate()
})

onBeforeUnmount(() => {
  trader.stopRealtime()
})

function fmtPrice(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '—'
  return Number.isInteger(v) ? v.toFixed(0) : v.toFixed(1)
}

function formatAccountUpdateTime(sec?: number | null): string {
  if (!sec) return '等待同步'
  const d = new Date(sec * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function dirLabel(direction: string): string {
  return direction === 'up' || direction === 'buy' ? '做多' : '做空'
}

function conditionLabel(state: string): string {
  return ({ Pending: '等待突破', WaitingConfirm: '等待确认开仓', Triggered: '已触发，尚未提交',
    Unknown: '提交结果待核实', Working: '委托中，尚未成交', PartFilled: '部分成交',
    Closing: '自动平仓处理中', Filled: '已成交', Rejected: '提交失败，等待重试',
    Canceled: '已取消后续开仓', Closed: '已平仓', Expired: '已过期', Invalidated: '已失效' } as Record<string, string>)[state] || state
}

function orderLabel(phase: string): string {
  return ({ Unknown: '等待柜台确认', Working: '柜台委托中', CancelPending: '等待撤单确认',
    Terminal: '委托已结束', Rejected: '提交失败' } as Record<string, string>)[phase] || phase
}

function exitReasonBadge(reason: string) {
  switch (reason) {
    case 'target_1r':
      return { text: '🎯 1.0R 锁定保本', type: 'success' as const, tip: '浮盈触及 1.0R，第一手平仓保本' }
    case 'trailing':
      return { text: '📈 追踪止盈', type: 'success' as const, tip: '回撤触及追踪止损线；当前规则为1.6R起按最高浮盈回撤0.8R连续追踪' }
    case 'breakeven':
      return { text: '🛡️ 成本线保本出场', type: 'info' as const, tip: '第一手止盈后回落至开仓成本线，零损失了结' }
    case 'stop':
      return { text: '🛑 触及初始止损', type: 'error' as const, tip: '触及 -1.0R 初始硬止损点' }
    case 'no_follow':
      return { text: '⚠️ 无跟随出场', type: 'warning' as const, tip: '入场第 5 根 K 线浮盈不足 0.5R 提前离场' }
    case 'time_exit':
      return { text: '⏳ 60根Bar超时', type: 'warning' as const, tip: '持仓达 60 根 K 线自动清仓' }
    case 'target_s1':
      return { text: '🏁 触及原形态目标', type: 'success' as const, tip: '触及前高/前低 S1 阻力支撑位' }
    default:
      return { text: reason || '平仓了结', type: 'default' as const, tip: '' }
  }
}

/** 理论复盘胜率 vs 实盘胜率差额 */
const winRateDiff = computed(() => {
  if (!trader.statistics || trader.statistics.total_trades === 0) return null
  const liveRate = trader.statistics.win_rate_pct
  const reviewRate = theoreticalWinRate.value ?? (review.rawOutcomes.length ? review.metrics.winRate * 100 : null)
  if (reviewRate == null) return null
  return liveRate - reviewRate
})

function getTradeRecordPnl(rec: LiveTradeRecord): number {
  if (rec.realized_pnl_amount != null && !Number.isNaN(rec.realized_pnl_amount) && rec.realized_pnl_amount !== 0) {
    return rec.realized_pnl_amount
  }
  return calcTradeRecordPnlAmount(rec.instrument_id || rec.symbol, rec.realized_pnl_pts, rec.volume)
}

const router = useRouter()

/**
 * 活跃信号（当前在手持仓 / 活跃条件单）：
 * 在主窗口（main）中打开对应合约的实时 K 线图，呈现当前真实活跃盘口、K线与运行中的形态，不走历史复盘逻辑
 */
async function openActiveChart(symbol?: string | null) {
  const sym = symbol || 'FG0'
  if (!isTauri()) {
    void router.push({
      name: 'chart',
      params: { symbol: sym },
      query: { tf: '15m' },
    })
    return
  }
  try {
    await emit('open-active-chart', { symbol: sym, timeframe: '15m' })
    const main = await WebviewWindow.getByLabel('main')
    if (main != null) {
      await main.show()
      await main.unminimize()
      await main.setFocus()
    }
    notify.success(`已在主窗口打开 [${sym}] 实时活跃行情图表`)
  } catch (e: any) {
    notify.error(`打开主窗口图表失败: ${e?.message || e}`)
  }
}

/**
 * 历史平仓流水信号：
 * 在专门的独立历史复盘图表窗口（history-chart 单例）中打开，不污染主窗口，也不会每次重复新建
 */
async function openHistoryChart(symbol?: string | null, eventId?: number | null) {
  if (!eventId) return
  const sym = symbol || 'FG0'
  if (!isTauri()) {
    void router.push({
      name: 'chart',
      params: { symbol: sym },
      query: { review: String(eventId) },
    })
    return
  }
  try {
    const existing = await WebviewWindow.getByLabel('history-chart')
    if (existing != null) {
      await existing.show()
      await existing.unminimize()
      await existing.setFocus()
      await emit('open-history-chart', { symbol: sym, eventId })
      notify.success(`已在历史复盘窗口定位 [${sym}] Event #${eventId}`)
      return
    }

    const webview = new WebviewWindow('history-chart', {
      url: `/#/chart/${encodeURIComponent(sym)}?review=${encodeURIComponent(eventId)}`,
      title: `历史复盘图表 · ${sym}`,
      width: 1280,
      height: 820,
      minWidth: 960,
      minHeight: 640,
      resizable: true,
      decorations: false,
      dragDropEnabled: false,
      visible: false,
    })
    await webview.once('tauri://created', async () => {
      await webview.show()
      await webview.setFocus()
    })
    notify.success(`已新建历史复盘窗口定位 [${sym}] Event #${eventId}`)
  } catch (e: any) {
    console.warn('打开历史复盘独立窗口失败:', e)
    notify.error(`打开历史复盘窗口失败: ${e?.message || e}`)
  }
}

function getSlippageInfo(fillPrice: number, plannedPrice?: number | null, direction: string = 'up', instrumentId: string = '') {
  if (plannedPrice == null || plannedPrice <= 0 || fillPrice <= 0) return null
  return calcSlippage(fillPrice, plannedPrice, direction, instrumentId)
}
</script>

<template>
  <div class="live-trader-page">
    <TraderInstancesPanel />
    <!-- 1. 顶部控制栏与柜台状态（浅色高对比度） -->
    <header class="trader-header" @mousedown="onHeaderMouseDown">
      <div class="header-top-row">
        <div class="header-title-box">
          <div class="title-with-badge">
            <n-icon :component="Bolt" size="22" color="#eab308" />
            <h1 class="header-title">{{ executionLabel }}量化执行终端</h1>
            <n-tooltip trigger="hover">
              <template #trigger>
                <n-tag
                  :type="trader.isReady ? 'success' : trader.health?.sync_pending ? 'warning' : 'error'"
                  size="small"
                  round
                  class="status-tag"
                >
                  {{ trader.isReady ? 'CTP 柜台就绪' : trader.health?.sync_pending ? 'CTP 数据同步中' : 'CTP 未就绪' }}
                </n-tag>
              </template>
              <span>
                {{
                  trader.isReady
                    ? 'CTP 柜台已就绪，行情与报单通道正常'
                    : trader.health?.sync_pending
                      ? '正在从柜台同步最新资金、持仓与委托流...'
                      : !trader.health?.connected
                        ? 'CTP 柜台未连接（当前处于非交易时段或周末休市，期货柜台服务关闭；当前呈现的数据为最后一次成功同步的历史镜像）'
                        : !trader.health?.logged_in
                          ? 'CTP 柜台网络已连通，等待认证登录'
                          : 'CTP 柜台未就绪'
                }}
              </span>
            </n-tooltip>
          </div>
          <span class="header-subtitle">
            {{ trader.bridgeOnline ? '交易数据已连接' : '交易数据连接中断' }} · 柜台资金更新：{{ formatAccountUpdateTime(trader.account?.updated_at) }}
            <span v-if="trader.errorMsg" class="error-inline">
              · <span class="error-msg-text">{{ trader.errorMsg }}</span>
              <n-button text size="tiny" type="error" class="retry-btn" :loading="trader.refreshing" @click="trader.refresh">
                重试
              </n-button>
            </span>
          </span>
        </div>

        <div class="header-control-box">
          <!-- 执行模式快速切换 -->
          <div class="mode-switch-group">
            <span class="mode-label">执行模式:</span>
            <div class="mode-buttons">
              <button
                type="button"
                class="mode-btn"
                :class="{ 'is-active monitor': (trader.config?.mode || 'SemiAuto') === 'MonitorOnly' }"
                @click="trader.setMode('MonitorOnly')"
              >
                仅监控
              </button>
              <button
                type="button"
                class="mode-btn"
                :class="{ 'is-active semi': (trader.config?.mode || 'SemiAuto') === 'SemiAuto' }"
                @click="trader.setMode('SemiAuto')"
              >
                半自动确认
              </button>
              <button
                type="button"
                class="mode-btn"
                :class="{ 'is-active auto': (trader.config?.mode || 'SemiAuto') === 'FullAuto' }"
                @click="trader.setMode('FullAuto')"
              >
                <n-icon :component="Flame" size="13" style="margin-right: 2px" />
                全自动
              </button>
            </div>
            <n-tag
              size="small"
              round
              :type="(trader.config?.mode || 'SemiAuto') === 'FullAuto' ? 'warning' : (trader.config?.mode || 'SemiAuto') === 'SemiAuto' ? 'info' : 'default'"
              class="active-mode-tag"
            >
              {{ (trader.config?.mode || 'SemiAuto') === 'FullAuto' ? '⚡ 自动报单中' : (trader.config?.mode || 'SemiAuto') === 'SemiAuto' ? '🛡️ 待人工核准' : '👁️ 仅生成信号' }}
            </n-tag>
            <n-tooltip v-if="(trader.config?.min_entry_risk_ticks ?? 0) > 0">
              <template #trigger>
                <n-tag size="small" round type="info">
                  入场止损 ≥ {{ trader.config?.min_entry_risk_ticks }} tick
                </n-tag>
              </template>
              过滤止损距离过窄的新开仓信号；资金不足时等待资金恢复。已有持仓的止损和平仓继续执行。
            </n-tooltip>
          </div>

          <n-divider vertical style="height: 22px; margin: 0 4px" />

          <!-- 紧急处置操作 -->
          <n-popconfirm @positive-click="trader.emergencyCancelAll">
            <template #trigger>
              <n-button size="small" secondary type="warning">
                <template #icon><n-icon :component="Trash" /></template>
                一键全撤
              </n-button>
            </template>
            确定要撤回当前所选账户的全部在途委托吗？
          </n-popconfirm>

          <n-popconfirm @positive-click="trader.emergencyCloseAll">
            <template #trigger>
              <n-button size="small" secondary type="error">
                <template #icon><n-icon :component="AlertTriangle" /></template>
                {{ trader.instancesSupported ? '平本实例持仓' : '紧急全平' }}
              </n-button>
            </template>
            {{ trader.instancesSupported ? '确定要平掉当前实例的全部托管持仓吗？' : '确定要平掉当前账户的全部活跃持仓吗？' }}
          </n-popconfirm>

          <n-button size="small" secondary :loading="trader.refreshing" @click="trader.refresh">
            <template #icon><n-icon :component="Refresh" /></template>
            刷新
          </n-button>
        </div>
      </div>

      <!-- 2. SimNow 真实账户资金横条（白色卡片条，大字号高清晰度） -->
      <div class="account-strip">
        <div class="acc-col">
          <span class="acc-label">投资者账号</span>
          <span class="acc-val font-num">{{ trader.account?.account_id || trader.health?.account_id || '—' }}</span>
        </div>
        <div class="acc-divider" />
        <div class="acc-col">
          <span class="acc-label">柜台权益 (元)</span>
          <span class="acc-val font-num is-bold">
            {{ trader.account?.balance != null ? Number(trader.account.balance).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—' }}
          </span>
        </div>
        <div class="acc-divider" />
        <div class="acc-col">
          <span class="acc-label">柜台可用资金 (元)</span>
          <span class="acc-val font-num is-available">
            {{ trader.account?.available != null ? Number(trader.account.available).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—' }}
          </span>
        </div>
        <div class="acc-divider" />
        <div class="acc-col">
          <span class="acc-label">保证金占用</span>
          <span class="acc-val font-num is-margin">
            {{ trader.account?.margin != null ? Number(trader.account.margin).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—' }}
          </span>
        </div>
        <div class="acc-divider" />
        <div class="acc-col">
          <span class="acc-label">柜台浮动盈亏</span>
          <span
            class="acc-val font-num"
            :class="(trader.account?.position_profit ?? 0) >= 0 ? 'is-up' : 'is-down'"
          >
            {{ (trader.account?.position_profit ?? 0) >= 0 ? '+' : '' }}{{ trader.account?.position_profit != null ? Number(trader.account.position_profit).toFixed(2) : '0.00' }}
          </span>
        </div>
        <div class="acc-divider" />
        <div class="acc-col">
          <span class="acc-label">风险度</span>
          <span class="acc-val font-num">
            {{ trader.account?.risk_ratio != null ? (trader.account.risk_ratio * 100).toFixed(2) + '%' : (trader.account?.balance ? (((trader.account.margin || 0) / trader.account.balance) * 100).toFixed(2) + '%' : '—') }}
          </span>
        </div>
        <div class="acc-divider" />
        <div class="acc-col">
          <span class="acc-label">交易日</span>
          <span class="acc-val font-num">{{ trader.health?.trading_day || '—' }}</span>
        </div>
      </div>

      <!-- 3. 实盘真实 KPI 看板 & 理论复盘比对条 -->
      <div class="kpi-and-diff-row">
        <div class="live-kpi-bar">
          <div class="kpi-box">
            <span class="kpi-name">{{ executionLabel }}成交单数</span>
            <span class="kpi-num font-num">{{ trader.statistics?.total_trades || 0 }} <span class="unit">笔</span></span>
            <span class="kpi-sub">大仓 {{ trader.statistics?.large_trades_count || 0 }} / 小仓 {{ trader.statistics?.small_trades_count || 0 }}</span>
          </div>
          <div class="kpi-divider" />
          <div class="kpi-box">
            <span class="kpi-name">{{ executionLabel }}真实胜率</span>
            <span
              class="kpi-num font-num"
              :class="(trader.statistics?.win_rate_pct || 0) >= 50 ? 'is-up' : 'is-down'"
            >
              {{ (trader.statistics?.win_rate_pct || 0).toFixed(1) }}%
            </span>
            <span class="kpi-sub">
              {{ trader.statistics?.win_trades || 0 }}胜 / {{ trader.statistics?.loss_trades || 0 }}负
            </span>
          </div>
          <div class="kpi-divider" />
          <div class="kpi-box">
            <span class="kpi-name">{{ executionLabel }}平仓盈亏</span>
            <span
              class="kpi-num font-num"
              :class="trader.totalRealizedPnlAmount >= 0 ? 'is-up' : 'is-down'"
            >
              {{ formatPnlAmount(trader.totalRealizedPnlAmount, { decimals: 2 }) }}
            </span>
            <span class="kpi-sub">
              累计 {{ formatPnlPoints(trader.totalRealizedPnlPoints) }}
            </span>
          </div>
          <div class="kpi-divider" />
          <div class="kpi-box">
            <span class="kpi-name">{{ executionLabel }}累计 R 倍数</span>
            <span
              class="kpi-num font-num"
              :class="(trader.statistics?.total_r || 0) >= 0 ? 'is-up' : 'is-down'"
            >
              {{ formatRMultiple(trader.statistics?.total_r || 0) }}
            </span>
            <span class="kpi-sub">单笔期望 {{ (trader.statistics?.avg_r || 0).toFixed(2) }}R</span>
          </div>
          <div class="kpi-divider" />
          <div class="kpi-box">
            <span class="kpi-name">{{ executionLabel }}盈亏比</span>
            <span class="kpi-num font-num">
              {{ (trader.statistics?.profit_factor || 0).toFixed(2) }}
            </span>
            <span class="kpi-sub">获利金额/亏损金额</span>
          </div>
        </div>

        <!-- 理论复盘 vs 实盘对比卡条 -->
        <div v-if="isNStrategy" class="comparison-card">
          <div class="cmp-title">
            <n-icon :component="LayersSubtract" size="15" color="#0284c7" />
            <span>{{ executionLabel }}表现 vs 理论复盘</span>
          </div>
          <div class="cmp-content">
            <div class="cmp-item">
              <span class="cmp-label">理论胜率 (复盘):</span>
              <span class="cmp-val font-num">
                {{ theoreticalWinRate !== null ? theoreticalWinRate.toFixed(1) + '%' : (review.metrics.winRate ? (review.metrics.winRate * 100).toFixed(1) + '%' : '—') }}
              </span>
            </div>
            <div class="cmp-item">
              <span class="cmp-label">{{ executionLabel }}真实胜率:</span>
              <span class="cmp-val font-num" :class="(trader.statistics?.win_rate_pct || 0) >= 50 ? 'is-up' : 'is-down'">
                {{ (trader.statistics?.win_rate_pct || 0).toFixed(1) }}%
              </span>
            </div>
            <div class="cmp-item">
              <span class="cmp-label">{{ executionLabel }}偏差:</span>
              <span
                v-if="winRateDiff !== null"
                class="cmp-val font-num"
                :class="winRateDiff >= 0 ? 'is-up' : 'is-down'"
              >
                {{ winRateDiff >= 0 ? '+' : '' }}{{ winRateDiff.toFixed(1) }}%
              </span>
              <span v-else class="cmp-val font-num">—</span>
            </div>
          </div>
          <div class="cmp-tip">
            理论复盘按历史信号推演。实盘统计使用柜台成交；模拟统计使用明确的盘口成交假设，不能代替实际成交效果。
          </div>
        </div>
      </div>
    </header>

    <!-- 4. 核心工作区分组标签页 -->
    <div class="main-tabs-toolbar">
      <div class="tabs-buttons">
        <n-button
          :type="trader.activeTab === 'positions' ? 'primary' : 'default'"
          size="small"
          @click="trader.activeTab = 'positions'"
        >
          <template #icon><n-icon :component="Flame" /></template>
          当前在手持仓
          <n-badge
            v-if="trader.combinedPositions.length > 0"
            :value="trader.combinedPositions.length"
            type="info"
            style="margin-left: 6px"
          />
        </n-button>

        <n-button
          :type="trader.activeTab === 'conditions' ? 'primary' : 'default'"
          size="small"
          @click="trader.activeTab = 'conditions'"
        >
          <template #icon><n-icon :component="Target" /></template>
          活跃条件单池
          <n-badge
            v-if="trader.waitingConfirmConditions.length > 0"
            :value="`待确认 ${trader.waitingConfirmConditions.length}`"
            type="warning"
            style="margin-left: 6px"
          />
          <span v-else style="margin-left: 4px">({{ trader.activeConditions.length }})</span>
        </n-button>

        <n-button
          :type="trader.activeTab === 'history' ? 'primary' : 'default'"
          size="small"
          @click="trader.activeTab = 'history'"
        >
          <template #icon><n-icon :component="Clock" /></template>
          真实平仓流水 ({{ trader.history.length }})
        </n-button>

        <n-button
          :type="trader.activeTab === 'orders' ? 'primary' : 'default'"
          size="small"
          @click="trader.activeTab = 'orders'"
        >
          <template #icon><n-icon :component="Clock" /></template>
          报单记录
        </n-button>

        <n-button
          :type="trader.activeTab === 'comparison' ? 'primary' : 'default'"
          size="small"
          @click="trader.activeTab = 'comparison'"
        >
          <template #icon><n-icon :component="LayersSubtract" /></template>
          策略前瞻对比
        </n-button>
      </div>

      <!-- 仅在流水页显示的过滤器 -->
      <div v-if="trader.activeTab === 'history'" class="history-filter">
        <n-select
          v-model:value="trader.filterReason"
          :options="exitReasonOptions"
          size="small"
          placeholder="筛选平仓原因"
          style="width: 170px"
        />
      </div>
    </div>

    <!-- 5. 视图主体内容区（浅色背景卡片流） -->
    <main class="trader-content-box">
      <section v-if="trader.activeTab === 'comparison'" class="tab-pane">
        <StrategyComparisonPanel :comparison="trader.strategyComparison" :live-config="trader.config"
          :updating="trader.updatingEntryFilter" @change-filter="trader.setEntryRiskFilter" />
      </section>

      <!-- A. 在手持仓标签页（展示 SimNow 柜台真实持仓如 pb2612 与托管策略持仓） -->
      <section v-if="trader.activeTab === 'positions'" class="tab-pane">
        <div v-if="!trader.combinedPositions.length" class="empty-holder">
          <n-empty description="当前账户与策略没有在手持仓" />
        </div>

        <div v-else class="cards-grid">
          <div
            v-for="pos in trader.combinedPositions"
            :key="`${pos.instrument_id}_${pos.direction}`"
            class="pos-card"
            :class="pos.direction === 'buy' || pos.direction === 'up' ? 'border-up' : 'border-down'"
          >
            <div class="pos-card-head">
              <div class="head-left">
                <span class="inst-name">{{ getInstrumentName(pos.instrument_id) }}</span>
                <span class="inst-code font-num">({{ pos.instrument_id }})</span>
                <n-tag
                  :type="pos.direction === 'buy' || pos.direction === 'up' ? 'error' : 'success'"
                  size="small"
                  round
                >
                  <template #icon>
                    <n-icon :component="pos.direction === 'buy' || pos.direction === 'up' ? ArrowUp : ArrowDown" />
                  </template>
                  {{ dirLabel(pos.direction) }}
                </n-tag>
                <n-tag size="small" type="info">
                  在手 {{ pos.position }} 手 (今仓 {{ pos.today_position }} / 昨仓 {{ pos.yd_position }})
                </n-tag>
                <n-tag v-if="pos.managed" size="small" :type="pos.managed.tier === 'large' ? 'warning' : 'default'">
                  {{ pos.managed.tier === 'large' ? '策略托管: 大仓分批' : '策略托管: 小仓1:1' }}
                </n-tag>
                <n-tag v-else size="small" type="default">
                  账户直接持仓
                </n-tag>

                <!-- 关联预警信号标签 (点击直达主窗口K线复盘打点) -->
                <n-tag
                  v-if="trader.resolveEventId(pos.managed)"
                  size="small"
                  type="info"
                  round
                  class="signal-badge-btn"
                  @click="openActiveChart(pos.managed?.symbol || pos.instrument_id)"
                >
                  <template #icon><n-icon :component="Bolt" /></template>
                  Event #{{ trader.resolveEventId(pos.managed) }}
                  <n-icon :component="ExternalLink" size="12" style="margin-left: 3px" />
                </n-tag>
                <span v-if="trader.resolveScore(pos.managed)" class="score-badge">
                  评分 {{ trader.resolveScore(pos.managed)?.toFixed(2) }}
                </span>
              </div>

              <div class="head-right">
                <span
                  class="pos-profit-pill font-num"
                  :class="(pos.position_profit ?? 0) >= 0 ? 'is-up' : 'is-down'"
                >
                  浮盈 {{ (pos.position_profit ?? 0) >= 0 ? '+' : '' }}{{ pos.position_profit != null ? Number(pos.position_profit).toFixed(2) : '0.00' }} 元{{ pos.profit_is_estimated ? '（估）' : '' }}
                </span>
                <span v-if="pos.managed?.lot1_closed" class="lot1-tag">
                  <n-icon :component="Check" size="14" /> 第一手已在 +1.0R 平仓锁定保本
                </span>
                <span v-if="pos.managed" class="held-bars">已持仓 {{ pos.managed.bars_held }} 根K线</span>
              </div>
            </div>

            <div class="pos-card-body">
              <!-- 信号拟定 vs 实际开仓成交对比栏 (包含预警时间、触价时间、报单时间、成交时间) -->
              <div v-if="pos.managed" class="exec-plan-bar">
                <div class="plan-item">
                  <span class="plan-lbl">预警 K 线时间</span>
                  <span class="plan-val font-num">{{ trader.resolveWarningBarTs(pos.managed) || '—' }}</span>
                </div>
                <div class="plan-divider" />
                <div class="plan-item">
                  <span class="plan-lbl">行情触价触发</span>
                  <span class="plan-val font-num">{{ trader.resolveTriggerTs(pos.managed) || '已触发' }}</span>
                </div>
                <div v-if="trader.resolveOrderInsertTs(pos.managed)" class="plan-divider" />
                <div v-if="trader.resolveOrderInsertTs(pos.managed)" class="plan-item">
                  <span class="plan-lbl">报单委托时间</span>
                  <span class="plan-val font-num">{{ trader.resolveOrderInsertTs(pos.managed) }}</span>
                </div>
                <div class="plan-divider" />
                <div class="plan-item">
                  <span class="plan-lbl">柜台成交时间</span>
                  <span class="plan-val font-num highlight-fill">{{ pos.managed.open_ts }}</span>
                </div>
                <div class="plan-divider" />
                <div class="plan-item">
                  <span class="plan-lbl">拟定突破入场</span>
                  <span class="plan-val font-num highlight-planned">{{ fmtPrice(trader.resolvePlannedEntryPrice(pos.managed)) }}</span>
                </div>
                <div class="plan-arrow">➔</div>
                <div class="plan-item">
                  <span class="plan-lbl">实际成交均价</span>
                  <span class="plan-val font-num highlight-fill">{{ fmtPrice(pos.managed.fill_price) }}</span>
                </div>
                <div class="plan-divider" />
                <div class="plan-item">
                  <span class="plan-lbl">入场执行滑点</span>
                  <span
                    v-if="getSlippageInfo(pos.managed.fill_price, trader.resolvePlannedEntryPrice(pos.managed), pos.managed.direction, pos.instrument_id)"
                    class="plan-val font-num"
                    :class="getSlippageInfo(pos.managed.fill_price, trader.resolvePlannedEntryPrice(pos.managed), pos.managed.direction, pos.instrument_id)?.isAdverse ? 'slip-adverse' : 'slip-favorable'"
                  >
                    {{ getSlippageInfo(pos.managed.fill_price, trader.resolvePlannedEntryPrice(pos.managed), pos.managed.direction, pos.instrument_id)?.label }}
                  </span>
                  <span v-else class="plan-val font-num text-muted">零滑点</span>
                </div>
                <div class="plan-divider" />
                <div class="plan-item">
                  <span class="plan-lbl">单笔 1.0R 风险</span>
                  <span class="plan-val font-num">{{ pos.managed.risk.toFixed(1) }} 点 (初损: {{ fmtPrice(pos.managed.initial_stop_price) }})</span>
                </div>
              </div>

              <!-- 价格流转轴 -->
              <div class="price-flow-row">
                <div v-if="pos.managed && trader.resolvePlannedEntryPrice(pos.managed)" class="flow-cell">
                  <span class="cell-label">拟定突破价</span>
                  <span class="cell-val font-num highlight-planned">
                    {{ fmtPrice(trader.resolvePlannedEntryPrice(pos.managed)) }}
                  </span>
                </div>
                <div v-if="pos.managed && trader.resolvePlannedEntryPrice(pos.managed)" class="flow-arrow">➔</div>
                <div class="flow-cell">
                  <span class="cell-label">真实开仓均价</span>
                  <span class="cell-val font-num">
                    {{ pos.managed ? fmtPrice(pos.managed.fill_price) : fmtPrice(pos.avg_price) }}
                  </span>
                </div>
                <div class="flow-arrow">➔</div>
                <div class="flow-cell" :class="pos.managed ? 'highlight-stop' : ''">
                  <span class="cell-label">{{ pos.managed ? '当前动态止损线' : '浮动盈亏 (元)' }}</span>
                  <span v-if="pos.managed" class="cell-val font-num">
                    {{ fmtPrice(pos.managed.current_stop_price) }}
                  </span>
                  <span
                    v-else
                    class="cell-val font-num"
                    :class="pos.position_profit >= 0 ? 'is-up' : 'is-down'"
                  >
                    {{ pos.position_profit >= 0 ? '+' : '' }}{{ pos.position_profit.toFixed(2) }}{{ pos.profit_is_estimated ? '（估算）' : '（柜台）' }}
                  </span>
                  <span v-if="pos.managed" class="stop-nature-tag" :class="pos.managed.lot1_closed ? 'is-breakeven' : ''">
                    {{ pos.managed.lot1_closed ? (pos.managed.trailing_grade > 1 ? '连续追踪 · 最高浮盈回撤0.8R' : '成本线保本 · 1.6R起连续追踪') : '初始硬止损' }}
                  </span>
                </div>
                <div class="flow-arrow">➔</div>
                <div class="flow-cell">
                  <span class="cell-label">{{ pos.managed ? '形态第一目标位' : '占用保证金 (元)' }}</span>
                  <span class="cell-val font-num">
                    {{ pos.managed ? fmtPrice(pos.managed.target_price) : (pos.margin != null ? Number(pos.margin).toLocaleString('zh-CN', { minimumFractionDigits: 2 }) : '0.00') }}
                  </span>
                </div>
              </div>

              <div class="stats-mini-row">
                <div class="stat-bubble">
                  <span class="lbl">持仓浮动盈亏</span>
                  <span class="val font-num" :class="(pos.position_profit ?? 0) >= 0 ? 'is-up' : 'is-down'">
                    {{ (pos.position_profit ?? 0) >= 0 ? '+' : '' }}{{ pos.position_profit != null ? Number(pos.position_profit).toFixed(2) : '0.00' }} 元{{ pos.profit_is_estimated ? '（估算）' : '（柜台）' }}
                  </span>
                </div>
                <div class="stat-bubble">
                  <span class="lbl">占用保证金</span>
                  <span class="val font-num">{{ pos.margin != null ? Number(pos.margin).toLocaleString('zh-CN', { minimumFractionDigits: 2 }) : '0.00' }} 元</span>
                </div>
                <div v-if="pos.managed" class="stat-bubble">
                  <span class="lbl">最大有利浮盈 (MFE)</span>
                  <span class="val is-up">+{{ pos.managed.max_mfe_r.toFixed(2) }}R</span>
                </div>
                <div v-if="pos.managed" class="stat-bubble">
                  <span class="lbl">最大不利回撤 (MAE)</span>
                  <span class="val is-down">{{ pos.managed.max_mae_r.toFixed(2) }}R</span>
                </div>
                <div v-if="pos.managed" class="stat-bubble">
                  <span class="lbl">单笔风险 (1.0R)</span>
                  <span class="val">{{ pos.managed.risk.toFixed(1) }} 点</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- B. 活跃条件单标签页 -->
      <section v-else-if="trader.activeTab === 'conditions'" class="tab-pane">
        <div v-if="!trader.activeConditions.length" class="empty-holder">
          <n-empty description="当前暂存池无等待突破的条件单" />
        </div>

        <div v-else class="cards-grid">
          <div
            v-for="cond in trader.activeConditions"
            :key="cond.id"
            class="cond-card"
            :class="cond.state === 'WaitingConfirm' ? 'pulse-confirm' : ''"
          >
            <div class="cond-head">
              <div class="head-left">
                <span class="inst-name">{{ getInstrumentName(cond.instrument_id) }}</span>
                <span class="inst-code font-num">({{ cond.instrument_id }})</span>
                <n-tag
                  :type="cond.direction === 'up' ? 'error' : 'success'"
                  size="small"
                  round
                >
                  {{ dirLabel(cond.direction) }}
                </n-tag>
                <n-tag size="small" :type="cond.tier === 'large' ? 'warning' : 'default'">
                  {{ cond.tier === 'large' ? '大仓' : '小仓' }}（计划 {{ cond.volume }} 手）
                </n-tag>
                <!-- 关联预警信号标签 (点击直达主窗口K线复盘打点) -->
                <n-tag
                  v-if="cond.event_id"
                  size="small"
                  type="info"
                  round
                  class="signal-badge-btn"
                  @click="openActiveChart(cond.symbol)"
                >
                  <template #icon><n-icon :component="Bolt" /></template>
                  Event #{{ cond.event_id }}
                  <n-icon :component="ExternalLink" size="12" style="margin-left: 3px" />
                </n-tag>
                <span class="score-badge">评分 {{ cond.score.toFixed(2) }}</span>
                <span v-if="cond.warning_bar_ts" class="warning-ts-tag font-num">
                  预警: {{ cond.warning_bar_ts }}
                </span>
                <span v-if="cond.trigger_ts" class="trigger-ts-tag font-num">
                  触发: {{ cond.trigger_ts }}
                </span>
              </div>

              <div class="head-right">
                <span class="wait-progress font-num">
                  已等待 {{ cond.bars_waited ?? 0 }} / {{ cond.max_wait_bars ?? 12 }} 根K线
                </span>
                <n-tag
                  size="small"
                  :type="cond.state === 'WaitingConfirm' ? 'warning' : cond.state === 'Triggered' ? 'info' : 'default'"
                >
                  {{ conditionLabel(cond.state) }}
                </n-tag>
              </div>
            </div>

            <div class="cond-body">
              <div class="cond-price-row">
                <div class="cond-price-item">
                  <span class="lbl">突破买点 (Entry)</span>
                  <span class="val font-num highlight-entry">{{ fmtPrice(cond.entry_price) }}</span>
                </div>
                <div class="cond-price-item">
                  <span class="lbl">初始止损 (Stop)</span>
                  <span class="val font-num highlight-stop-val">{{ fmtPrice(cond.stop_price) }}</span>
                </div>
                <div class="cond-price-item">
                  <span class="lbl">目标止盈 (Target)</span>
                  <span class="val font-num text-success">{{ fmtPrice(cond.target_price) }}</span>
                </div>
                <div class="cond-price-item">
                  <span class="lbl">计划风险 (1.0R)</span>
                  <span class="val font-num">{{ cond.risk.toFixed(1) }} 点</span>
                </div>
              </div>

              <div v-if="cond.note" class="cond-note">
                说明: {{ cond.note }}
              </div>
            </div>

            <div class="cond-actions">
              <n-button
                v-if="cond.state === 'WaitingConfirm'"
                type="primary"
                size="small"
                @click="trader.confirmCondition(cond.id)"
              >
                <template #icon><n-icon :component="CircleCheck" /></template>
                立即确认开仓
              </n-button>
              <n-button
                size="small"
                secondary
                type="error"
                :disabled="cond.state === 'Closing'"
                @click="trader.cancelCondition(cond.id)"
              >
                <template #icon><n-icon :component="CircleX" /></template>
                取消剩余开仓
              </n-button>
            </div>
          </div>
        </div>
      </section>

      <section v-else-if="trader.activeTab === 'orders'" class="tab-pane">
        <div class="orders-toolbar">
          <span>报单记录 · 共 {{ trader.executionOrdersTotal }} 条（包含未成交、撤销与拒单）</span>
          <n-button size="small" :loading="trader.executionOrdersLoading" @click="trader.loadExecutionOrders()">
            <template #icon><n-icon :component="Refresh" /></template>
            刷新
          </n-button>
        </div>
        <div v-if="trader.executionOrdersError" class="cond-note">{{ trader.executionOrdersError }}</div>
        <n-spin :show="trader.executionOrdersLoading">
          <div v-if="!trader.executionOrders.length && !trader.executionOrdersLoading" class="empty-holder">
            <n-empty :description="trader.executionOrdersError ? '暂时无法读取报单记录，请刷新重试' : '暂无报单记录'" />
          </div>
          <div class="history-list">
            <div v-for="order in trader.executionOrders" :key="`${order.trading_day}-${order.request.order_ref}`" class="cond-card">
              <div class="cond-head">
                <span>{{ order.request.instrument_id }} · {{ order.request.direction === 'buy' ? '买入' : '卖出' }}</span>
                <n-tag size="small">
                  {{ orderLabel(order.phase) }}
                </n-tag>
              </div>
              <div class="cond-body">
                委托 {{ order.request.volume }} 手 @ {{ fmtPrice(order.request.price) }} · 柜台成交 {{ order.filled }} 手
                <div>交易日 {{ order.trading_day }} · 报单号 {{ order.request.order_ref }}</div>
                <div class="cond-note">{{ order.note }}</div>
              </div>
            </div>
          </div>
        </n-spin>
        <n-pagination class="orders-pagination" :page="trader.executionOrdersPage"
          :page-size="trader.executionOrdersPageSize" :item-count="trader.executionOrdersTotal"
          :disabled="trader.executionOrdersLoading" @update:page="trader.loadExecutionOrders" />
      </section>

      <!-- C. 真实成交平仓流水标签页 -->
      <section v-else-if="trader.activeTab === 'history'" class="tab-pane">
        <div v-if="!trader.filteredHistory.length" class="empty-holder">
          <n-empty description="暂无符合筛选条件的真实平仓成交记录" />
        </div>

        <div v-else class="history-list">
          <div
            v-for="rec in trader.filteredHistory"
            :key="rec.id"
            class="history-card"
            :class="rec.r_multiple > 0 ? 'hist-win' : rec.r_multiple < 0 ? 'hist-loss' : 'hist-breakeven'"
          >
            <div class="hist-card-head">
              <div class="head-left">
                <span class="hist-inst">{{ getInstrumentName(rec.instrument_id) }}</span>
                <span class="hist-code font-num">({{ rec.instrument_id }})</span>
                <span class="hist-dir" :class="rec.direction === 'up' ? 'is-up' : 'is-down'">
                  {{ dirLabel(rec.direction) }} {{ rec.volume }} 手
                </span>
                <n-tag
                  size="small"
                  :type="exitReasonBadge(rec.exit_reason).type"
                >
                  {{ exitReasonBadge(rec.exit_reason).text }}
                </n-tag>
                <!-- 关联预警信号标签 (点击直达主窗口K线复盘打点) -->
                <n-tag
                  v-if="trader.resolveEventId(rec)"
                  size="small"
                  type="info"
                  round
                  class="signal-badge-btn"
                  @click="openHistoryChart(rec.symbol, trader.resolveEventId(rec))"
                >
                  <template #icon><n-icon :component="Bolt" /></template>
                  Event #{{ trader.resolveEventId(rec) }}
                  <n-icon :component="ExternalLink" size="12" style="margin-left: 3px" />
                </n-tag>
                <span v-if="trader.resolveScore(rec)" class="score-badge">
                  评分 {{ trader.resolveScore(rec)?.toFixed(2) }}
                </span>
              </div>

              <div class="head-right font-num">
                <span class="pnl-amount" :class="getTradeRecordPnl(rec) >= 0 ? 'is-up' : 'is-down'">
                  {{ formatPnlAmount(getTradeRecordPnl(rec), { decimals: 2 }) }}
                </span>
                <span class="pnl-pts" :class="rec.realized_pnl_pts >= 0 ? 'is-up' : 'is-down'">
                  {{ formatPnlPoints(rec.realized_pnl_pts) }}
                </span>
                <span class="pnl-r" :class="rec.r_multiple >= 0 ? 'is-up' : 'is-down'">
                  {{ formatRMultiple(rec.r_multiple) }}
                </span>
              </div>
            </div>

            <div class="hist-card-body">
              <!-- 全流程执行时间线 (预警拟定 -> 突破开仓 -> 动态平仓) -->
              <div class="hist-full-timeline">
                <!-- 阶段1: 预警拟定 -->
                <div class="timeline-node node-warning">
                  <div class="node-title">
                    <span class="node-badge">1. 预警拟定</span>
                    <span class="node-ts font-num">{{ trader.resolveWarningBarTs(rec) || '—' }}</span>
                  </div>
                  <div class="node-content">
                    <div class="node-field">
                      <span class="field-lbl">触价触发时间</span>
                      <span class="field-val font-num">{{ trader.resolveTriggerTs(rec) || '已触发' }}</span>
                    </div>
                    <div class="node-field">
                      <span class="field-lbl">计划突破入场</span>
                      <span class="field-val font-num highlight-planned">{{ fmtPrice(trader.resolvePlannedEntryPrice(rec) ?? rec.fill_price) }}</span>
                    </div>
                    <div class="node-field">
                      <span class="field-lbl">初始硬止损</span>
                      <span class="field-val font-num text-danger">{{ fmtPrice(trader.resolveInitialStopPrice(rec) ?? rec.initial_stop_price) }}</span>
                    </div>
                    <div class="node-field">
                      <span class="field-lbl">理论形态目标</span>
                      <span class="field-val font-num text-success">{{ fmtPrice(trader.resolveTargetPrice(rec) ?? rec.target_price) }}</span>
                    </div>
                  </div>
                </div>

                <div class="timeline-connector">➔</div>

                <!-- 阶段2: 突破开仓 -->
                <div class="timeline-node node-open">
                  <div class="node-title">
                    <span class="node-badge">2. 突破开仓</span>
                    <span class="node-ts font-num">{{ rec.open_ts }}</span>
                  </div>
                  <div class="node-content">
                    <div v-if="trader.resolveOrderInsertTs(rec)" class="node-field">
                      <span class="field-lbl">报单委托时间</span>
                      <span class="field-val font-num">{{ trader.resolveOrderInsertTs(rec) }}</span>
                    </div>
                    <div class="node-field">
                      <span class="field-lbl">实际成交时间</span>
                      <span class="field-val font-num">{{ rec.open_ts }}</span>
                    </div>
                    <div class="node-field">
                      <span class="field-lbl">实际成交均价</span>
                      <span class="field-val font-num highlight-fill">{{ fmtPrice(rec.fill_price) }}</span>
                    </div>
                    <div class="node-field">
                      <span class="field-lbl">入场执行滑点</span>
                      <span
                        v-if="getSlippageInfo(rec.fill_price, trader.resolvePlannedEntryPrice(rec), rec.direction, rec.instrument_id)"
                        class="field-val font-num"
                        :class="getSlippageInfo(rec.fill_price, trader.resolvePlannedEntryPrice(rec), rec.direction, rec.instrument_id)?.isAdverse ? 'slip-adverse' : 'slip-favorable'"
                      >
                        {{ getSlippageInfo(rec.fill_price, trader.resolvePlannedEntryPrice(rec), rec.direction, rec.instrument_id)?.label }}
                      </span>
                      <span v-else class="field-val font-num text-muted">零滑点</span>
                    </div>
                    <div class="node-field">
                      <span class="field-lbl">实际 1R 风险</span>
                      <span class="field-val font-num">{{ rec.risk.toFixed(1) }} 点</span>
                    </div>
                  </div>
                </div>

                <div class="timeline-connector">➔</div>

                <!-- 阶段3: 动态平仓 -->
                <div class="timeline-node node-exit">
                  <div class="node-title">
                    <span class="node-badge">3. 动态平仓</span>
                    <span class="node-ts font-num">{{ rec.exit_ts }}</span>
                  </div>
                  <div class="node-content">
                    <div class="node-field">
                      <span class="field-lbl">实际平仓价格</span>
                      <span class="field-val font-num font-bold">{{ fmtPrice(rec.exit_price) }}</span>
                    </div>
                    <div class="node-field">
                      <span class="field-lbl">平仓触发原因</span>
                      <span class="field-val exit-reason-text">{{ exitReasonBadge(rec.exit_reason).text }}</span>
                    </div>
                    <div class="node-field">
                      <span class="field-lbl">净收益实现</span>
                      <span class="field-val font-num font-bold" :class="rec.realized_pnl_pts >= 0 ? 'is-up' : 'is-down'">
                        {{ formatPnlPoints(rec.realized_pnl_pts) }} ({{ formatRMultiple(rec.r_multiple) }})
                      </span>
                    </div>
                  </div>
                </div>
              </div>

              <!-- 底部博弈关键指标 -->
              <pre v-if="rec.signal_evidence" style="white-space:pre-wrap;font-size:11px;max-height:180px;overflow:auto">原始信号依据（升级前归档）{{ JSON.stringify(rec.signal_evidence.signal, null, 2) }}</pre>
              <div class="hist-meta">
                <span>合约乘数 {{ getSymbolMultiplier(rec.instrument_id) }} 元/点</span>
                <span>持仓 {{ rec.bars_held }} 根K线</span>
                <span class="mfe-tag">最大有利 MFE +{{ rec.max_mfe_r.toFixed(2) }}R</span>
                <span class="mae-tag">最大不利 MAE {{ rec.max_mae_r.toFixed(2) }}R</span>
                <button
                  v-if="trader.resolveEventId(rec)"
                  class="chart-jump-link"
                  @click="openHistoryChart(rec.symbol, trader.resolveEventId(rec))"
                >
                  <n-icon :component="Eye" size="13" /> 在行情图表中复盘
                </button>
              </div>
            </div>
          </div>
        </div>
      </section>
    </main>
  </div>
</template>

<style scoped>
.orders-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}
.orders-pagination { margin-top: 20px; }
/* 全局页面：纯白/浅灰高雅现代风格 */
.live-trader-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  max-height: 100%;
  overflow: hidden;
  background: #f8fafc;
  color: #1e293b;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  user-select: none;
  box-sizing: border-box;
}

/* 顶部 Header */
.trader-header {
  padding: 10px 18px 8px 18px;
  background: #ffffff;
  border-bottom: 1px solid #e2e8f0;
  flex-shrink: 0;
}

.header-top-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.header-title-box {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.title-with-badge {
  display: flex;
  align-items: center;
  gap: 8px;
}

.header-title {
  margin: 0;
  font-size: 19px;
  font-weight: 700;
  color: #0f172a;
}

.status-tag {
  font-weight: 600;
}

.header-subtitle {
  font-size: 12px;
  color: #64748b;
}

.header-control-box {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}

.mode-switch-group {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  background: #f8fafc;
  padding: 4px 10px;
  border-radius: 8px;
  border: 1px solid #cbd5e1;
}

.mode-label {
  font-size: 12px;
  color: #475569;
  font-weight: 600;
}

.mode-buttons {
  display: inline-flex;
  background: #e2e8f0;
  padding: 2px;
  border-radius: 6px;
  gap: 2px;
}

.mode-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: #475569;
  font-size: 12px;
  font-weight: 500;
  padding: 4px 10px;
  border-radius: 4px;
  cursor: pointer;
  transition: all 0.15s ease-in-out;
  outline: none;
}

.mode-btn:hover {
  background: rgba(255, 255, 255, 0.6);
  color: #0f172a;
}

.mode-btn.is-active.monitor {
  background: #ffffff;
  color: #334155;
  font-weight: 700;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08);
}

.mode-btn.is-active.semi {
  background: #0284c7;
  color: #ffffff;
  font-weight: 700;
  box-shadow: 0 1px 3px rgba(2, 132, 199, 0.35);
}

.mode-btn.is-active.auto {
  background: #d97706;
  color: #ffffff;
  font-weight: 700;
  box-shadow: 0 1px 4px rgba(217, 119, 6, 0.4);
}

.active-mode-tag {
  font-weight: 600;
}

/* 资金横条（高对比度，数字醒目放大） */
.account-strip {
  display: flex;
  align-items: center;
  background: #ffffff;
  padding: 8px 16px;
  border-radius: 8px;
  border: 1px solid #e2e8f0;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
  margin-bottom: 8px;
}

.acc-col {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
}

.acc-label {
  font-size: 11px;
  font-weight: 600;
  color: #64748b;
}

.acc-val {
  font-size: 18px;
  font-weight: 800;
  line-height: 1.2;
  color: #0f172a;
  letter-spacing: -0.3px;
}

.is-available {
  color: #15803d;
}

.is-margin {
  color: #2563eb;
}

.is-up {
  color: #dc2626 !important;
}

.is-down {
  color: #16a34a !important;
}

.is-bold {
  font-weight: 800;
}

.acc-divider {
  width: 1px;
  height: 28px;
  background: #e2e8f0;
  margin: 0 12px;
}

/* KPI & 比对行 */
.kpi-and-diff-row {
  display: grid;
  grid-template-columns: minmax(0, 3fr) minmax(0, 2fr);
  gap: 12px;
  align-items: stretch;
}

.live-kpi-bar {
  min-width: 0;
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
  align-items: center;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 8px 14px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
}

.kpi-box {
  display: flex;
  flex-direction: column;
  flex: 1;
  gap: 2px;
}

.kpi-name {
  font-size: 11px;
  font-weight: 600;
  color: #64748b;
}

.kpi-num {
  font-size: 19px;
  font-weight: 800;
  line-height: 1.2;
  color: #0f172a;
  white-space: nowrap;
}

.kpi-num .unit {
  font-size: 12px;
  font-weight: 500;
  color: #64748b;
}

.kpi-sub {
  font-size: 11px;
  color: #94a3b8;
}

.kpi-divider {
  display: none;
}

.comparison-card {
  min-width: 0;
  background: #f0fdf4;
  border: 1px solid #bbf7d0;
  border-radius: 8px;
  padding: 6px 12px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
}

.cmp-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  font-weight: 600;
  color: #15803d;
}

.cmp-content {
  display: flex;
  justify-content: space-between;
  margin: 2px 0;
}

.cmp-item {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.cmp-label {
  font-size: 10px;
  color: #64748b;
}

.cmp-val {
  font-size: 12px;
  font-weight: 700;
  color: #0f172a;
}

.cmp-tip {
  font-size: 10px;
  color: #64748b;
}

/* 工具栏 Tabs */
.main-tabs-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: space-between;
  align-items: center;
  padding: 8px 18px;
  background: #ffffff;
  border-bottom: 1px solid #e2e8f0;
  flex-shrink: 0;
}

.tabs-buttons {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

/* 主内容区 */
.trader-content-box {
  flex: 1 1 0%;
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
  padding: 14px 20px 60px 20px;
  box-sizing: border-box;
}

@media (max-width: 1100px) {
  .kpi-and-diff-row {
    grid-template-columns: minmax(0, 1fr);
  }
}

@media (max-width: 640px) {
  .live-kpi-bar {
    grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  }
}

.trader-content-box::-webkit-scrollbar {
  width: 6px;
}

.trader-content-box::-webkit-scrollbar-track {
  background: transparent;
}

.trader-content-box::-webkit-scrollbar-thumb {
  background-color: rgba(140, 150, 170, 0.35);
  border-radius: 6px;
}

.trader-content-box::-webkit-scrollbar-thumb:hover {
  background-color: rgba(140, 150, 170, 0.65);
}

.empty-holder {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 260px;
  background: #ffffff;
  border-radius: 8px;
  border: 1px solid #e2e8f0;
}

.cards-grid {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding-bottom: 32px;
}

/* 持仓卡片（白色卡片，左侧红绿状态边框） */
.pos-card {
  background: #ffffff;
  border-radius: 8px;
  border: 1px solid #e2e8f0;
  padding: 12px 16px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
}

.pos-card.border-up {
  border-left: 4px solid #dc2626;
}

.pos-card.border-down {
  border-left: 4px solid #16a34a;
}

.pos-card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.head-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.inst-name {
  font-size: 16px;
  font-weight: 700;
  color: #0f172a;
}

.inst-code {
  font-size: 13px;
  color: #64748b;
  font-weight: 500;
}

.lot1-tag {
  font-size: 12px;
  color: #16a34a;
  display: flex;
  align-items: center;
  gap: 4px;
  background: #dcfce7;
  padding: 2px 8px;
  border-radius: 4px;
  font-weight: 600;
}

.held-bars {
  font-size: 12px;
  color: #64748b;
}

.pos-profit-pill {
  font-size: 13px;
  font-weight: 700;
  padding: 2px 8px;
  border-radius: 4px;
  background: #f1f5f9;
}

.pos-profit-pill.is-up {
  color: #dc2626 !important;
  background: #fef2f2;
}

.pos-profit-pill.is-down {
  color: #16a34a !important;
  background: #f0fdf4;
}

.price-flow-row {
  display: flex;
  align-items: center;
  background: #f8fafc;
  padding: 8px 14px;
  border-radius: 6px;
  gap: 14px;
  margin-bottom: 8px;
  border: 1px solid #f1f5f9;
}

.flow-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.cell-label {
  font-size: 11px;
  color: #64748b;
}

.cell-val {
  font-size: 15px;
  font-weight: 700;
  color: #0f172a;
}

.flow-arrow {
  color: #cbd5e1;
  font-size: 14px;
}

.highlight-stop {
  background: #fefce8;
  padding: 4px 10px;
  border-radius: 4px;
  border: 1px dashed #facc15;
}

.stop-nature-tag {
  font-size: 11px;
  color: #ca8a04;
  font-weight: 600;
}

.stop-nature-tag.is-breakeven {
  color: #16a34a;
}

.stats-mini-row {
  display: flex;
  gap: 20px;
}

.stat-bubble {
  display: flex;
  gap: 6px;
  font-size: 12px;
}

.stat-bubble .lbl {
  color: #64748b;
}

.stat-bubble .val {
  color: #1e293b;
  font-weight: 600;
}

/* 条件单卡片 */
.cond-card {
  background: #ffffff;
  border-radius: 8px;
  border: 1px solid #e2e8f0;
  padding: 14px 18px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
}

.pulse-confirm {
  border-color: #facc15;
  box-shadow: 0 0 10px rgba(250, 204, 21, 0.3);
}

.cond-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.score-badge {
  font-size: 11px;
  color: #b45309;
  background: #fef3c7;
  padding: 2px 6px;
  border-radius: 4px;
  font-weight: 600;
}

.cond-price-row {
  display: flex;
  gap: 24px;
  background: #f8fafc;
  padding: 8px 14px;
  border-radius: 6px;
  border: 1px solid #f1f5f9;
}

.cond-price-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.cond-price-item .lbl {
  font-size: 11px;
  color: #64748b;
}

.cond-price-item .val {
  font-size: 14px;
  font-weight: 700;
}

.highlight-entry {
  color: #d97706;
}

.highlight-stop-val {
  color: #16a34a;
}

.cond-note {
  font-size: 12px;
  color: #64748b;
}

.cond-actions {
  display: flex;
  gap: 10px;
  justify-content: flex-end;
}

/* 流水卡片 */
.history-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding-bottom: 32px;
}

.history-card {
  background: #ffffff;
  border-radius: 6px;
  border: 1px solid #e2e8f0;
  padding: 12px 16px;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.03);
}

.history-card.hist-win {
  border-left: 4px solid #dc2626;
}

.history-card.hist-loss {
  border-left: 4px solid #16a34a;
}

.history-card.hist-breakeven {
  border-left: 4px solid #2563eb;
}

.hist-card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.hist-inst {
  font-size: 15px;
  font-weight: 700;
  color: #0f172a;
}

.hist-code {
  font-size: 12px;
  color: #64748b;
  margin-left: 4px;
}

.hist-dir {
  font-size: 12px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 3px;
}

.pnl-amount {
  font-size: 16px;
  font-weight: 800;
  margin-right: 12px;
}

.pnl-pts {
  font-size: 14px;
  font-weight: 600;
  margin-right: 10px;
  opacity: 0.9;
}

.pnl-r {
  font-size: 15px;
  font-weight: 800;
}

.hist-timeline {
  display: flex;
  align-items: center;
  gap: 14px;
  background: #f8fafc;
  padding: 8px 14px;
  border-radius: 4px;
  font-size: 12px;
  border: 1px solid #f1f5f9;
}

.hist-step {
  display: flex;
  align-items: center;
  gap: 6px;
}

.step-lbl {
  color: #64748b;
}

.step-val {
  font-weight: 700;
  color: #0f172a;
}

.step-ts {
  font-size: 11px;
  color: #94a3b8;
}

.hist-arrow {
  color: #cbd5e1;
}

.hist-meta {
  margin-left: auto;
  display: flex;
  gap: 14px;
  color: #64748b;
  font-size: 11px;
}

.font-num {
  font-family: "JetBrains Mono", Consolas, Menlo, Monaco, monospace;
}

.is-up {
  color: #dc2626 !important;
}

.is-down {
  color: #16a34a !important;
}

.error-inline {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.error-msg-text {
  color: #ef4444;
}

.retry-btn {
  margin-left: 2px;
  font-weight: 600;
  text-decoration: underline;
  cursor: pointer;
}

/* 预警信号与执行透明度增强样式 */
.signal-badge-btn {
  cursor: pointer;
  font-weight: 600;
  transition: all 0.15s ease;
  user-select: none;
}

.signal-badge-btn:hover {
  filter: brightness(0.92);
  transform: translateY(-1px);
}

.warning-ts-tag {
  font-size: 11px;
  color: #64748b;
  background: #f1f5f9;
  padding: 2px 6px;
  border-radius: 4px;
}

.wait-progress {
  font-size: 12px;
  color: #64748b;
  margin-right: 8px;
}

/* 计划与实际执行比对栏 (持仓卡片) */
.exec-plan-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  padding: 8px 14px;
  margin-bottom: 10px;
}

.plan-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.plan-lbl {
  font-size: 11px;
  color: #64748b;
  font-weight: 500;
}

.plan-val {
  font-size: 13px;
  font-weight: 700;
  color: #0f172a;
}

.plan-divider {
  width: 1px;
  height: 22px;
  background: #e2e8f0;
}

.plan-arrow {
  color: #94a3b8;
  font-size: 12px;
}

.highlight-planned {
  color: #2563eb;
  font-weight: 700;
}

.highlight-fill {
  color: #0f172a;
  font-weight: 800;
}

.slip-adverse {
  color: #d97706;
  font-weight: 700;
}

.slip-favorable {
  color: #16a34a;
  font-weight: 700;
}

.text-muted {
  color: #94a3b8;
}

.text-danger {
  color: #dc2626;
}

.text-success {
  color: #16a34a;
}

.font-bold {
  font-weight: 700;
}

/* 真实平仓历史三段全生命周期时间线 */
.hist-full-timeline {
  display: flex;
  align-items: stretch;
  gap: 10px;
  background: #f8fafc;
  border: 1px solid #f1f5f9;
  border-radius: 6px;
  padding: 10px 12px;
  margin-top: 6px;
  margin-bottom: 8px;
}

.timeline-node {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 8px;
  background: #ffffff;
  border-radius: 6px;
  border: 1px solid #e2e8f0;
  padding: 8px 10px;
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
  font-size: 13px;
  user-select: none;
  flex-shrink: 0;
}

.node-title {
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid #f1f5f9;
  padding-bottom: 4px;
}

.node-badge {
  font-size: 11px;
  font-weight: 700;
  color: #334155;
}

.node-ts {
  font-size: 11px;
  color: #94a3b8;
}

.node-content {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.node-field {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 12px;
}

.field-lbl {
  color: #64748b;
  font-size: 11px;
}

.field-val {
  font-size: 12px;
  font-weight: 600;
  color: #0f172a;
}

.exit-reason-text {
  font-weight: 600;
  color: #475569;
}

.mfe-tag {
  color: #dc2626;
  font-weight: 600;
}

.mae-tag {
  color: #16a34a;
  font-weight: 600;
}

.chart-jump-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: #eff6ff;
  color: #2563eb;
  border: 1px solid #bfdbfe;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s ease;
  margin-left: auto;
}

.chart-jump-link:hover {
  background: #dbeafe;
  color: #1d4ed8;
  border-color: #93c5fd;
}

/* 预警信号全景复盘弹窗 */
.modal-loading-box {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 48px 0;
}

.signal-modal-body {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.modal-signal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: #f8fafc;
  padding: 10px 14px;
  border-radius: 6px;
  border: 1px solid #e2e8f0;
}

.msh-left {
  display: flex;
  align-items: center;
  gap: 10px;
}

.msh-symbol {
  font-size: 16px;
  font-weight: 800;
  color: #0f172a;
}

.msh-grade {
  font-size: 13px;
  font-weight: 600;
  color: #475569;
}

.msh-right {
  font-size: 12px;
  color: #64748b;
}

.score-pill {
  font-size: 12px;
  font-weight: 700;
  color: #b45309;
  background: #fef3c7;
  padding: 2px 8px;
  border-radius: 12px;
  border: 1px solid #fde68a;
}

.modal-grid-sections {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.modal-section-card {
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  padding: 12px 14px;
}

.msc-title {
  font-size: 13px;
  font-weight: 700;
  color: #1e293b;
  margin-bottom: 10px;
  padding-bottom: 4px;
  border-bottom: 1px solid #f1f5f9;
}

.msc-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 8px 16px;
}

.msc-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 12px;
}

.msc-item .lbl {
  color: #64748b;
}

.msc-item .val {
  font-weight: 600;
  color: #0f172a;
}

.modal-action-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  width: 100%;
}
</style>
