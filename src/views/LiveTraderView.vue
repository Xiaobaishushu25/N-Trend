<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted } from 'vue'
import { isTauri } from '@tauri-apps/api/core'
import { getCurrentWindow } from '@tauri-apps/api/window'
import {
  NBadge,
  NButton,
  NDivider,
  NEmpty,
  NIcon,
  NPopconfirm,
  NRadioGroup,
  NRadioButton,
  NSelect,
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
  Flame,
  LayersSubtract,
  Refresh,
  ShieldCheck,
  Target,
  Trash,
} from '@vicons/tabler'
import { useLiveTraderStore } from '../stores/liveTrader'
import { useTradeReviewStore } from '../stores/tradeReview'
import { formatPnlPoints, formatRMultiple } from '../utils/multipliers'

const trader = useLiveTraderStore()
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

onMounted(async () => {
  await trader.loadAll()
  trader.startPolling(2500)
  if (!review.rawOutcomes.length) {
    void review.load()
  }
})

onBeforeUnmount(() => {
  trader.stopPolling()
})

function fmtPrice(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '—'
  return Number.isInteger(v) ? v.toFixed(0) : v.toFixed(1)
}

function dirLabel(direction: string): string {
  return direction === 'up' || direction === 'buy' ? '做多' : '做空'
}

function exitReasonBadge(reason: string) {
  switch (reason) {
    case 'target_1r':
      return { text: '🎯 1.0R 锁定保本', type: 'success' as const, tip: '浮盈触及 1.0R，第一手平仓保本' }
    case 'trailing':
      return { text: '📈 阶梯追踪止盈', type: 'success' as const, tip: '浮盈推进触发阶梯锁盈，回撤跌破锁定线' }
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
  const reviewRate = review.metrics.winRate * 100
  return liveRate - reviewRate
})
</script>

<template>
  <div class="live-trader-page">
    <!-- 1. 顶部控制栏与柜台状态（浅色高对比度） -->
    <header class="trader-header" @mousedown="onHeaderMouseDown">
      <div class="header-top-row">
        <div class="header-title-box">
          <div class="title-with-badge">
            <n-icon :component="Bolt" size="22" color="#eab308" />
            <h1 class="header-title">实盘量化执行终端</h1>
            <n-tag
              :type="trader.isReady ? 'success' : 'error'"
              size="small"
              round
              class="status-tag"
            >
              {{ trader.isReady ? 'SimNow 柜台在线 (READY)' : 'CTP 柜台离线' }}
            </n-tag>
          </div>
          <span class="header-subtitle">
            SimNow 真实撮合持仓 · 突破条件单托管 · 动态移动保本 · 实盘与理论复盘比对
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
            确定要向 SimNow 柜台撤回所有在途挂单吗？
          </n-popconfirm>

          <n-popconfirm @positive-click="trader.emergencyCloseAll">
            <template #trigger>
              <n-button size="small" secondary type="error">
                <template #icon><n-icon :component="AlertTriangle" /></template>
                紧急全平
              </n-button>
            </template>
            确定要市价对手价平掉当前所有活跃持仓吗？此操作不可逆！
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
          <span class="acc-val font-num">{{ trader.account?.account_id || trader.health?.account_id || '278272' }}</span>
        </div>
        <div class="acc-divider" />
        <div class="acc-col">
          <span class="acc-label">动态权益 (元)</span>
          <span class="acc-val font-num is-bold">
            {{ trader.account?.balance != null ? Number(trader.account.balance).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—' }}
          </span>
        </div>
        <div class="acc-divider" />
        <div class="acc-col">
          <span class="acc-label">可用资金 (元)</span>
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
          <span class="acc-label">浮动盈亏</span>
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
            <span class="kpi-name">实盘成交单数</span>
            <span class="kpi-num font-num">{{ trader.statistics?.total_trades || 0 }} <span class="unit">笔</span></span>
            <span class="kpi-sub">大仓 {{ trader.statistics?.large_trades_count || 0 }} / 小仓 {{ trader.statistics?.small_trades_count || 0 }}</span>
          </div>
          <div class="kpi-divider" />
          <div class="kpi-box">
            <span class="kpi-name">实盘真实胜率</span>
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
            <span class="kpi-name">实盘累计 R 倍数</span>
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
            <span class="kpi-name">实盘盈亏比</span>
            <span class="kpi-num font-num">
              {{ (trader.statistics?.profit_factor || 0).toFixed(2) }}
            </span>
            <span class="kpi-sub">获利金额/亏损金额</span>
          </div>
        </div>

        <!-- 理论复盘 vs 实盘对比卡条 -->
        <div class="comparison-card">
          <div class="cmp-title">
            <n-icon :component="LayersSubtract" size="15" color="#0284c7" />
            <span>实盘表现 vs 理论复盘</span>
          </div>
          <div class="cmp-content">
            <div class="cmp-item">
              <span class="cmp-label">理论胜率 (复盘):</span>
              <span class="cmp-val font-num">{{ (review.metrics.winRate * 100).toFixed(1) }}%</span>
            </div>
            <div class="cmp-item">
              <span class="cmp-label">实盘真实胜率:</span>
              <span class="cmp-val font-num" :class="(trader.statistics?.win_rate_pct || 0) >= 50 ? 'is-up' : 'is-down'">
                {{ (trader.statistics?.win_rate_pct || 0).toFixed(1) }}%
              </span>
            </div>
            <div class="cmp-item">
              <span class="cmp-label">实盘偏差:</span>
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
            * 理论复盘按历史信号理想打点推演；实盘则反映 SimNow 真实撮合排队、跳价与两手分批保本规则。
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
      <!-- A. 在手持仓标签页（展示 SimNow 柜台真实持仓如 pb2612 与托管策略持仓） -->
      <section v-if="trader.activeTab === 'positions'" class="tab-pane">
        <div v-if="!trader.combinedPositions.length" class="empty-holder">
          <n-empty description="当前 SimNow 柜台与策略没有在手持仓（已空仓等待新信号突破）" />
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
                <n-tag v-if="pos.managed" size="small" :type="pos.managed.tier === 'Large' ? 'warning' : 'default'">
                  {{ pos.managed.tier === 'Large' ? '策略托管: 大仓分批' : '策略托管: 小仓1:1' }}
                </n-tag>
                <n-tag v-else size="small" type="default">
                  SimNow 柜台直持仓
                </n-tag>
              </div>

              <div class="head-right">
                <span v-if="pos.managed?.lot1_closed" class="lot1-tag">
                  <n-icon :component="Check" size="14" /> 第一手已在 +1.0R 平仓锁定保本
                </span>
                <span v-if="pos.managed" class="held-bars">已持仓 {{ pos.managed.bars_held }} 根K线</span>
              </div>
            </div>

            <div class="pos-card-body">
              <div class="price-flow-row">
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
                    {{ pos.position_profit >= 0 ? '+' : '' }}{{ pos.position_profit.toFixed(2) }}
                  </span>
                  <span v-if="pos.managed" class="stop-nature-tag" :class="pos.managed.lot1_closed ? 'is-breakeven' : ''">
                    {{ pos.managed.lot1_closed ? (pos.managed.trailing_grade > 1 ? `阶梯锁盈档位 ${pos.managed.trailing_grade}` : '开仓成本线保本') : '初始硬止损' }}
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
                    {{ (pos.position_profit ?? 0) >= 0 ? '+' : '' }}{{ pos.position_profit != null ? Number(pos.position_profit).toFixed(2) : '0.00' }} 元
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
                <n-tag size="small" :type="cond.tier === 'Large' ? 'warning' : 'default'">
                  {{ cond.tier === 'Large' ? '大仓 (2手)' : '小仓 (1手)' }}
                </n-tag>
                <span class="score-badge">评分 {{ cond.score.toFixed(2) }}</span>
              </div>

              <div class="head-right">
                <n-tag
                  size="small"
                  :type="cond.state === 'WaitingConfirm' ? 'warning' : cond.state === 'Triggered' ? 'info' : 'default'"
                >
                  {{ cond.state === 'WaitingConfirm' ? '⚠️ 等待人工确认开仓' : cond.state === 'Triggered' ? '突破已报单柜台' : '监听盘中突破' }}
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
                  <span class="val font-num">{{ fmtPrice(cond.target_price) }}</span>
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
                @click="trader.cancelCondition(cond.id)"
              >
                <template #icon><n-icon :component="CircleX" /></template>
                撤销条件单
              </n-button>
            </div>
          </div>
        </div>
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
              </div>

              <div class="head-right font-num">
                <span class="pnl-pts" :class="rec.realized_pnl_pts >= 0 ? 'is-up' : 'is-down'">
                  {{ formatPnlPoints(rec.realized_pnl_pts) }}
                </span>
                <span class="pnl-r" :class="rec.r_multiple >= 0 ? 'is-up' : 'is-down'">
                  {{ formatRMultiple(rec.r_multiple) }}
                </span>
              </div>
            </div>

            <div class="hist-card-body">
              <div class="hist-timeline">
                <div class="hist-step">
                  <span class="step-lbl">开仓</span>
                  <span class="step-val font-num">{{ fmtPrice(rec.fill_price) }}</span>
                  <span class="step-ts font-num">{{ rec.open_ts }}</span>
                </div>
                <div class="hist-arrow">➔</div>
                <div class="hist-step">
                  <span class="step-lbl">平仓</span>
                  <span class="step-val font-num">{{ fmtPrice(rec.exit_price) }}</span>
                  <span class="step-ts font-num">{{ rec.exit_ts }}</span>
                </div>
                <div class="hist-meta">
                  <span>持仓 {{ rec.bars_held }} 根K线</span>
                  <span>MFE +{{ rec.max_mfe_r.toFixed(2) }}R</span>
                  <span>MAE {{ rec.max_mae_r.toFixed(2) }}R</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>
    </main>
  </div>
</template>

<style scoped>
/* 全局页面：纯白/浅灰高雅现代风格 */
.live-trader-page {
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: #f8fafc;
  color: #1e293b;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  user-select: none;
  box-sizing: border-box;
}

/* 顶部 Header */
.trader-header {
  padding: 14px 20px 12px 20px;
  background: #ffffff;
  border-bottom: 1px solid #e2e8f0;
}

.header-top-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
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
  align-items: center;
  gap: 10px;
}

.mode-switch-group {
  display: flex;
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
  padding: 12px 20px;
  border-radius: 10px;
  border: 1px solid #e2e8f0;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
  margin-bottom: 12px;
}

.acc-col {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex: 1;
}

.acc-label {
  font-size: 12px;
  font-weight: 600;
  color: #64748b;
}

.acc-val {
  font-size: 20px;
  font-weight: 800;
  line-height: 1.25;
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
  height: 32px;
  background: #e2e8f0;
  margin: 0 14px;
}

/* KPI & 比对行 */
.kpi-and-diff-row {
  display: flex;
  gap: 12px;
  align-items: stretch;
}

.live-kpi-bar {
  flex: 3;
  display: flex;
  align-items: center;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 10px;
  padding: 12px 18px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
}

.kpi-box {
  display: flex;
  flex-direction: column;
  flex: 1;
  gap: 3px;
}

.kpi-name {
  font-size: 12px;
  font-weight: 600;
  color: #64748b;
}

.kpi-num {
  font-size: 22px;
  font-weight: 800;
  line-height: 1.2;
  color: #0f172a;
}

.kpi-num .unit {
  font-size: 13px;
  font-weight: 500;
  color: #64748b;
}

.kpi-sub {
  font-size: 12px;
  color: #94a3b8;
}

.kpi-divider {
  width: 1px;
  height: 36px;
  background: #e2e8f0;
  margin: 0 14px;
}

.comparison-card {
  flex: 2;
  background: #f0fdf4;
  border: 1px solid #bbf7d0;
  border-radius: 8px;
  padding: 8px 14px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
}

.cmp-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 600;
  color: #15803d;
}

.cmp-content {
  display: flex;
  justify-content: space-between;
  margin: 4px 0;
}

.cmp-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.cmp-label {
  font-size: 11px;
  color: #64748b;
}

.cmp-val {
  font-size: 13px;
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
  justify-content: space-between;
  align-items: center;
  padding: 10px 20px;
  background: #ffffff;
  border-bottom: 1px solid #e2e8f0;
}

.tabs-buttons {
  display: flex;
  gap: 8px;
}

/* 主内容区 */
.trader-content-box {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px;
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
}

/* 持仓卡片（白色卡片，左侧红绿状态边框） */
.pos-card {
  background: #ffffff;
  border-radius: 8px;
  border: 1px solid #e2e8f0;
  padding: 14px 18px;
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

.price-flow-row {
  display: flex;
  align-items: center;
  background: #f8fafc;
  padding: 10px 16px;
  border-radius: 6px;
  gap: 16px;
  margin-bottom: 10px;
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

.pnl-pts {
  font-size: 15px;
  font-weight: 700;
  margin-right: 10px;
}

.pnl-r {
  font-size: 16px;
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
</style>
