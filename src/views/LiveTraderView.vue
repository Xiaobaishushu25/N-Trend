<script setup lang="ts">
import TraderInstancesPanel from '../components/TraderInstancesPanel.vue'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { openLiveTraderWindow } from '../utils/openLiveTraderWindow'
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
watch(() => trader.activeTab, (tab) => {
  if (tab === 'orders') void trader.loadExecutionOrders(1)
})

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
  window.addEventListener('open-live-trader-window', onPopoutCustomEvent)
  const queryInstanceId = typeof route.query.instance_id === 'string' && route.query.instance_id.trim()
    ? route.query.instance_id.trim()
    : null
  if (queryInstanceId) {
    trader.selectedInstanceId = queryInstanceId
  }
  await trader.startRealtime()
  await trader.loadAll()
  if (queryInstanceId) {
    await trader.selectInstance(queryInstanceId)
  }
  if (trader.activeTab === 'orders') void trader.loadExecutionOrders(1)
})

onBeforeUnmount(() => {
  window.removeEventListener('open-live-trader-window', onPopoutCustomEvent)
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
  const map: Record<string, string> = {
    pending: '等待突破',
    waiting_confirm: '等待确认开仓',
    triggered: '已触发，尚未提交',
    unknown: '提交结果待核实',
    working: '委托中，尚未成交',
    part_filled: '部分成交',
    closing: '自动平仓处理中',
    filled: '已成交',
    rejected: '提交失败，等待重试',
    filtered: '已过滤，不开仓',
    canceled: '已取消后续开仓',
    closed: '已平仓',
    expired: '已过期',
    invalidated: '已失效',
  }
  const key = (state || '').toLowerCase()
  return map[key] || ({
    Pending: '等待突破', WaitingConfirm: '等待确认开仓', Triggered: '已触发，尚未提交',
    Unknown: '提交结果待核实', Working: '委托中，尚未成交', PartFilled: '部分成交',
    Closing: '自动平仓处理中', Filled: '已成交', Rejected: '提交失败，等待重试',
    Canceled: '已取消后续开仓', Closed: '已平仓', Expired: '已过期', Invalidated: '已失效'
  } as Record<string, string>)[state] || state
}

function formatOrderActionTime(ts?: number | null): string {
  if (!ts) return ''
  const ms = ts > 1e11 ? ts : ts * 1000
  const d = new Date(ms)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function formatOffset(offset?: string): string {
  if (!offset) return ''
  switch (offset) {
    case 'open':
      return '开仓'
    case 'close':
      return '平仓'
    case 'close_today':
      return '平今'
    case 'close_yesterday':
      return '平昨'
    case 'auto':
      return '平仓'
    default:
      return ''
  }
}

function orderLabel(phase: string): string {
  return ({ Unknown: '等待柜台确认', Working: '柜台委托中', CancelPending: '等待撤单确认',
    Terminal: '委托已结束', Rejected: '提交失败' } as Record<string, string>)[phase] || phase
}

function isWarningNote(note?: string | null): boolean {
  if (!note) return false
  return /筛选|暂缓|小于|滑点|资金不足|风控|拦截|过滤|超入场|不可用|熔断|已越过止损/i.test(note)
}

function getNoteCategory(note?: string | null): string {
  if (!note) return '说明'
  if (/入场筛选|小于.*tick/i.test(note)) return '入场筛选'
  if (/资金不足|资金/i.test(note)) return '资金风控'
  if (/滑点/i.test(note)) return '滑点保护'
  if (/平仓|退出/i.test(note)) return '平仓说明'
  if (/超时|过期/i.test(note)) return '状态提醒'
  if (/越过止损|拦截|风控/i.test(note)) return '风控拦截'
  return '说明'
}

function formatNoteText(note?: string | null): string {
  if (!note) return ''
  return note.replace(/^(说明[:：]|入场筛选[:：])\s*/, '').trim()
}

function isConditionClosing(state?: string | null): boolean {
  if (!state) return false
  const s = state.toLowerCase()
  return s === 'closing' || s === 'closed'
}

function getNoteTooltip(note?: string | null): string {
  if (!note) return ''
  const trimmed = note.trim()

  // 1. 退出任务已锁定: [reason]
  if (/退出任务已锁定/i.test(trimmed)) {
    const reasonMatch = trimmed.match(/退出任务已锁定[:：]\s*(\w+)/i)
    const reason = reasonMatch ? reasonMatch[1].toLowerCase() : ''

    const reasonMap: Record<string, string> = {
      market_gap: '【行情跳号/跳空风控】行情推送序列出现断层或跳空，超出系统容忍阈值。为防止数据中断期间产生不可控的极端穿透滑点，系统启动最高优先级风控，锁定任务并立即平仓保护本金。',
      stop: '【触及硬止损】盘中价格击穿建仓时设定的初始止损保护线，按纪律坚决全平止损，严格控制单笔最大风险。',
      target_1r: '【+1.0R第1止盈位】浮盈达到1倍初始风险目标。若是1手则全平落袋；若是多手则平第1手止盈，并将剩余持仓止损线自动移至开仓成本线保本。',
      breakeven: '【保本平仓离场】在第1手止盈落袋后，行情未能进一步走高而是回落至开仓成本价，系统自动平掉剩余持仓，保住已获利润，做到零回撤。',
      trailing: '【追踪移动止盈】浮盈大幅扩大后，动态保护线随最高点持续跟进；当价格自高位回撤触及移动止损线时平仓，锁住大部分趋势波段利润。',
      target_s1: '【结构阻力位止盈】行情冲刺触及前高/前低或战略结构位 S1，完成既定目标，全平获利了结。',
      no_follow: '【动能衰竭离场】开仓后持有满5根K线但最大浮盈依然小于0.5R，显示突破缺乏跟进动能、走势停滞，主动平仓微利或微损离场，释放资金槽位。',
      time_exit: '【时间窗口离场】持仓达到策略设定的时间周期上限（60根K线），强制退出，防范陷入无序震荡。',
      pre_close: '【日盘收盘避险】临近日盘收盘，主动平仓离场，规避休市期间外部市场异动带来的跳空风险。',
      pre_weekend: '【周末休市避险】周五夜盘收盘前主动清仓，规避周末长休市期间宏观消息面引发的周一大幅跳空风险。',
      pre_holiday: '【长假避险平仓】节假日前主动清仓，彻底防范长假期间外盘剧烈波动风险。',
    }

    if (reason && reasonMap[reason]) {
      return reasonMap[reason]
    }
    return `【平仓任务锁定】系统检测到持仓触发退出条件（${reason || '策略触发'}），已切断加仓通道并原子锁定平仓任务，正在向柜台提交平仓委托。`
  }

  // 2. 入场与风控筛选说明
  if (/止损距离.*小于.*tick/i.test(trimmed)) {
    return '【入场空间筛选】当前信号计算出的止损保护距离小于策略设定的最小保护阈值（如10 tick）。过窄的止损极易被盘口随机噪音频繁扫损，盈亏比与胜率性价比低，系统主动过滤该开仓信号以减少磨损。'
  }
  if (/资金不足/i.test(trimmed)) {
    return '【资金风控拦截】当前账户可用资金低于该合约开仓1手所需保证金与安全缓冲金，暂缓报单以保障账户免于透支与强平风险。'
  }
  if (/超入场滑点限制/i.test(trimmed)) {
    return '【滑点保护拦截】盘口瞬时突破幅度过大，对价已超出策略允许的追单容差上限，系统主动放弃追高/追空，避免在不利高位追价接盘。'
  }
  if (/越过止损/i.test(trimmed)) {
    return '【极速反向拦截】条件单触发瞬间盘口剧烈跳空，价格在发出报单前已跌破/涨破止损保护线，紧急终止本次入场，避免开仓即深套。'
  }
  if (/仅监控/i.test(trimmed)) {
    return '【监控模式】当前策略处于仅监控模式（MonitorOnly），仅记录信号触发与行情轨迹，不向柜台发送真实交易委托。'
  }
  if (/已确认.*等待有效盘口/i.test(trimmed)) {
    return '【等待盘口挂单】开仓已获得确认，系统正在等待买卖盘口出现符合滑点和深度要求的有效对价时自动报单。'
  }
  if (/已取消后续开仓/i.test(trimmed)) {
    return '【取消开仓确认】已向柜台撤销未成交的开仓报单，正在等待柜台回报完成状态结算。'
  }
  if (/撤单未确认/i.test(trimmed)) {
    return '【撤单防并发保护】撤单请求已发给柜台但尚未收到确认回报，为防重复开仓引发敞口超额，系统暂时锁定该品种槽位。'
  }
  if (/提交结果未知/i.test(trimmed)) {
    return '【提交异常核实】向柜台报单后返回异常或超时，禁止盲目重报以避免双倍持仓，系统正在核实委托状态。'
  }

  return trimmed
}

function formatCondTs(ts?: string | null): string {
  if (!ts) return ''
  const match = ts.match(/^\d{4}-(\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)/)
  if (match) return match[1]
  return ts
}

function conditionBadgeType(state?: string | null): 'default' | 'primary' | 'info' | 'success' | 'warning' | 'error' {
  const s = (state || '').toLowerCase()
  switch (s) {
    case 'waiting_confirm':
    case 'waitingconfirm':
      return 'warning'
    case 'triggered':
      return 'info'
    case 'working':
    case 'part_filled':
      return 'primary'
    case 'filled':
      return 'success'
    case 'closing':
      return 'warning'
    case 'rejected':
      return 'error'
    case 'filtered':
      return 'warning'
    default:
      return 'default'
  }
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



function getTradeRecordPnl(rec: LiveTradeRecord): number {
  if (rec.realized_pnl_amount != null && !Number.isNaN(rec.realized_pnl_amount) && rec.realized_pnl_amount !== 0) {
    return rec.realized_pnl_amount
  }
  return calcTradeRecordPnlAmount(rec.instrument_id || rec.symbol, rec.realized_pnl_pts, rec.volume)
}

const router = useRouter()
const route = useRoute()

function handleOpenInstanceWindow(instanceId?: string, name?: string) {
  const targetId = instanceId || trader.selectedInstanceId
  const targetName = name || selectedInstance.value?.instance.name || targetId
  void openLiveTraderWindow(targetId, targetName)
}

function onPopoutCustomEvent(e: Event) {
  const detail = (e as CustomEvent).detail
  if (detail?.instanceId) {
    handleOpenInstanceWindow(detail.instanceId, detail.name)
  }
}

watch(() => route.query.instance_id, async (newId) => {
  if (typeof newId === 'string' && newId.trim() && newId.trim() !== trader.selectedInstanceId) {
    await trader.selectInstance(newId.trim())
  }
})

watch(selectedInstance, (inst) => {
  if (inst && isTauri()) {
    const accId = inst.account.user_id || inst.account.name || inst.account.id
    const winTitle = `实盘量化执行终端 · ${inst.instance.name} (${accId})`
    void getCurrentWindow().setTitle(winTitle)
  }
}, { immediate: true })

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
    <TraderInstancesPanel @open-window="handleOpenInstanceWindow" />
    <!-- 1. 顶部控制栏与柜台状态（高密度专业布局） -->
    <header class="trader-header" @mousedown="onHeaderMouseDown">
      <div class="header-top-row">
        <div class="header-status-box">
          <span class="live-status-pill" :class="trader.bridgeOnline ? 'is-online' : 'is-offline'">
            <span class="status-indicator-dot" />
            {{ trader.bridgeOnline ? '数据通信正常' : '数据通信中断' }}
          </span>
          <span class="header-subtitle font-num">
            柜台更新: {{ formatAccountUpdateTime(trader.account?.updated_at) }}
          </span>
          <span v-if="trader.errorMsg" class="error-inline">
            · <span class="error-msg-text">{{ trader.errorMsg }}</span>
            <n-button text size="tiny" type="error" class="retry-btn" :loading="trader.refreshing" @click="trader.refresh">
              重试
            </n-button>
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

      <!-- 3. 实盘真实 KPI 看板 -->
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
          <span v-else style="margin-left: 4px">({{ trader.conditionPool.length }})</span>
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
        <div v-if="!trader.conditionPool.length" class="empty-holder cond-empty-container">
          <n-empty description="当前实例暂无条件单或准入过滤记录">
            <template #extra>
              <div class="empty-hint-box">
                <div class="empty-hint-text">
                  <span class="empty-hint-main">收到信号后，将逐条列出等待状态或未开仓原因</span>
                  <span class="empty-hint-sub">
                    被准入规则拦截的信号会标记为“已过滤，不开仓”
                  </span>
                </div>
                <n-button
                  size="small"
                  secondary
                  type="primary"
                  @click="trader.activeTab = 'comparison'"
                >
                  <template #icon><n-icon :component="LayersSubtract" /></template>
                  查看策略前瞻对比
                </n-button>
              </div>
            </template>
          </n-empty>
        </div>

        <div v-else class="cards-grid">
          <div v-if="trader.filteredConditions.length" class="condition-pool-summary">
            活跃条件单 {{ trader.activeConditions.length }} 条 · 准入过滤 {{ trader.filteredConditions.length }} 条
            <span>过滤记录仅供查看，不会报单</span>
          </div>
          <div
            v-for="cond in trader.conditionPool"
            :key="cond.id"
            class="cond-card"
            :class="[
              cond.direction === 'up' ? 'border-up' : 'border-down',
              cond.state === 'WaitingConfirm' || cond.state === 'waiting_confirm' ? 'pulse-confirm' : '',
            ]"
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
                <n-tag v-if="!cond.display_only" size="small" :type="cond.tier === 'large' ? 'warning' : 'default'">
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
                <span class="score-badge">评分 {{ Number.isFinite(cond.score) ? cond.score.toFixed(2) : '—' }}</span>
              </div>

              <div class="head-right">
                <span v-if="!cond.display_only" class="wait-progress font-num">
                  已等待 {{ cond.bars_waited ?? 0 }} / {{ cond.max_wait_bars ?? 12 }} 根K线
                </span>
                <n-tag
                  size="small"
                  :type="conditionBadgeType(cond.state)"
                >
                  {{ conditionLabel(cond.state) }}
                </n-tag>
              </div>
            </div>

            <div class="cond-body">
              <div class="cond-metrics-panel">
                <!-- 左半部：核心点位与风险 -->
                <div class="metrics-group-price">
                  <div class="metric-item">
                    <span class="lbl">突破买点 (Entry)</span>
                    <span class="val font-num highlight-entry">{{ fmtPrice(cond.entry_price) }}</span>
                  </div>
                  <div class="metric-item">
                    <span class="lbl">初始止损 (Stop)</span>
                    <span class="val font-num highlight-stop-val">{{ fmtPrice(cond.stop_price) }}</span>
                  </div>
                  <div class="metric-item">
                    <span class="lbl">目标止盈 (Target)</span>
                    <span class="val font-num text-success">{{ fmtPrice(cond.target_price) }}</span>
                  </div>
                  <div class="metric-item">
                    <span class="lbl">计划风险 (1.0R)</span>
                    <span class="val font-num">{{ fmtPrice(cond.risk) }} 点</span>
                  </div>
                </div>

                <div class="metrics-divider" />

                <!-- 右半部：时间线与触价进度 -->
                <div class="metrics-group-time">
                  <div class="metric-item">
                    <span class="lbl">预警 K 线时间</span>
                    <span class="val font-num font-time" :title="`预警时间: ${cond.warning_bar_ts || '—'}`">
                      {{ formatCondTs(cond.warning_bar_ts) || '—' }}
                    </span>
                  </div>
                  <div v-if="!cond.display_only" class="metric-item">
                    <span class="lbl">行情触价触发</span>
                    <span
                      class="val font-num font-time"
                      :class="cond.trigger_ts ? 'text-triggered' : 'text-muted'"
                      :title="`触价时间: ${cond.trigger_ts || '等待突破触价'}`"
                    >
                      {{ formatCondTs(cond.trigger_ts) || '等待突破触价' }}
                    </span>
                  </div>
                </div>
              </div>
            </div>

            <div class="cond-footer">
              <n-tooltip v-if="cond.note" trigger="hover" placement="top" :style="{ maxWidth: '420px' }">
                <template #trigger>
                  <div class="cond-note-banner" :class="isWarningNote(cond.note) ? 'is-warning' : 'is-info'">
                    <n-icon :component="isWarningNote(cond.note) ? AlertTriangle : InfoCircle" class="note-icon" />
                    <div class="note-content">
                      <span class="note-tag">{{ getNoteCategory(cond.note) }}</span>
                      <span class="note-text">{{ formatNoteText(cond.note) }}</span>
                    </div>
                  </div>
                </template>
                <div class="note-tooltip-box">
                  <div class="note-tooltip-header">
                    <span class="note-tooltip-badge" :class="isWarningNote(cond.note) ? 'is-warning' : 'is-info'">
                      {{ getNoteCategory(cond.note) }}
                    </span>
                    <span class="note-tooltip-title">{{ formatNoteText(cond.note) }}</span>
                  </div>
                  <div class="note-tooltip-body">{{ getNoteTooltip(cond.note) }}</div>
                </div>
              </n-tooltip>
              <div v-else class="cond-footer-spacer" />

              <div v-if="!cond.display_only" class="cond-actions">
                <n-button
                  v-if="cond.state === 'WaitingConfirm' || cond.state === 'waiting_confirm'"
                  type="primary"
                  size="small"
                  @click="trader.confirmCondition(cond.id)"
                >
                  <template #icon><n-icon :component="CircleCheck" /></template>
                  立即确认开仓
                </n-button>
                <n-tooltip trigger="hover" placement="top" :style="{ maxWidth: '320px' }">
                  <template #trigger>
                    <span class="cond-btn-wrapper">
                      <n-button
                        size="small"
                        secondary
                        :type="isConditionClosing(cond.state) ? 'default' : 'error'"
                        :disabled="isConditionClosing(cond.state)"
                        @click="trader.cancelCondition(cond.id)"
                      >
                        <template #icon>
                          <n-icon :component="isConditionClosing(cond.state) ? ShieldCheck : CircleX" />
                        </template>
                        {{ isConditionClosing(cond.state) ? '平仓执行中' : '取消剩余开仓' }}
                      </n-button>
                    </span>
                  </template>
                  {{ isConditionClosing(cond.state)
                    ? '持仓已进入自动平仓阶段，开仓已全部完成，无需取消开仓。系统正按退出策略执行清仓委托。'
                    : '撤销该条件单未成交的剩余开仓委托，已成交持仓将继续受平仓策略保护。' }}
                </n-tooltip>
              </div>
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
                <div class="head-left">
                  <span class="inst-name">{{ getInstrumentName(order.request.instrument_id) }}</span>
                  <span class="inst-code font-num">({{ order.request.instrument_id }})</span>
                  <n-tag
                    :type="order.request.direction === 'buy' ? 'error' : 'success'"
                    size="small"
                    round
                  >
                    {{ order.request.direction === 'buy' ? '买入' : '卖出' }}{{ formatOffset(order.request.offset) }}
                  </n-tag>
                  <span v-if="order.action_at" class="order-time font-num">
                    {{ formatOrderActionTime(order.action_at) }}
                  </span>
                </div>
                <n-tag size="small">
                  {{ orderLabel(order.phase) }}
                </n-tag>
              </div>
              <div class="cond-body">
                <div>委托 {{ order.request.volume }} 手 @ {{ fmtPrice(order.request.price) }} · 柜台成交 {{ order.filled }} 手</div>
                <div class="order-meta font-num">
                  <span v-if="order.action_at">时间 {{ formatOrderActionTime(order.action_at) }} · </span>
                  <span>交易日 {{ order.trading_day }}</span>
                  <span> · 报单号 {{ order.request.order_ref }}</span>
                </div>
                <div v-if="order.note" class="cond-note">{{ order.note }}</div>
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

.header-status-box {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.live-status-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 7px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
}
.live-status-pill.is-online {
  background: #f0fdf4;
  color: #166534;
  border: 1px solid #bbf7d0;
}
.live-status-pill.is-offline {
  background: #fef2f2;
  color: #991b1b;
  border: 1px solid #fecaca;
}
.status-indicator-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
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

/* 实盘 KPI 看板 */
.live-kpi-bar {
  min-width: 0;
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
  align-items: center;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 8px 16px;
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

.cond-empty-container {
  padding: 40px 16px;
}

.condition-pool-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  color: #475569;
  font-size: 13px;
}

.condition-pool-summary span {
  color: #94a3b8;
}

.empty-hint-box {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
  margin-top: 12px;
}

.empty-hint-text {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  max-width: 520px;
  text-align: center;
}

.empty-hint-main {
  font-size: 13px;
  font-weight: 500;
  color: #475569;
}

.empty-hint-sub {
  font-size: 12px;
  color: #94a3b8;
  line-height: 1.5;
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
  flex-wrap: wrap;
  gap: 8px;
  min-width: 0;
}

.inst-name {
  font-size: 16px;
  font-weight: 700;
  color: #0f172a;
  white-space: nowrap;
}

.inst-code {
  font-size: 13px;
  color: #64748b;
  font-weight: 500;
  white-space: nowrap;
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
  gap: 12px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
  transition: all 0.2s ease;
}

.cond-card:hover {
  box-shadow: 0 3px 8px rgba(0, 0, 0, 0.06);
}

.pulse-confirm {
  border-color: #facc15;
  box-shadow: 0 0 10px rgba(250, 204, 21, 0.3);
}

.cond-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px 16px;
}

.score-badge {
  font-size: 11px;
  color: #b45309;
  background: #fef3c7;
  padding: 2px 6px;
  border-radius: 4px;
  font-weight: 600;
  white-space: nowrap;
  line-height: 1.5;
}

.cond-metrics-panel {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #f8fafc;
  padding: 10px 16px;
  border-radius: 6px;
  border: 1px solid #f1f5f9;
  gap: 20px;
  flex-wrap: wrap;
}

.metrics-group-price {
  display: flex;
  align-items: center;
  gap: 28px;
  flex: 1;
  min-width: 320px;
}

.metrics-divider {
  width: 1px;
  height: 28px;
  background: #e2e8f0;
  flex-shrink: 0;
}

.metrics-group-time {
  display: flex;
  align-items: center;
  gap: 24px;
  flex-shrink: 0;
}

.metric-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.metric-item .lbl {
  font-size: 11px;
  color: #64748b;
  font-weight: 500;
}

.metric-item .val {
  font-size: 14px;
  font-weight: 700;
  color: #0f172a;
  white-space: nowrap;
}

.font-time {
  font-size: 13px !important;
  font-weight: 600 !important;
  color: #334155;
}

.text-triggered {
  color: #0284c7 !important;
}

.highlight-entry {
  color: #d97706;
}

.highlight-stop-val {
  color: #16a34a;
}

.cond-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 2px;
  min-height: 32px;
}

.cond-footer-spacer {
  flex: 1;
}

.cond-note {
  font-size: 12px;
  color: #64748b;
}

.cond-note-banner {
  flex: 1;
  margin-top: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 12px;
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.4;
  min-width: 0;
  transition: all 0.2s ease;
  cursor: help;
}

.cond-note-banner:hover {
  filter: brightness(0.97);
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06);
}

.note-tooltip-box {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 2px 0;
  line-height: 1.5;
}

.note-tooltip-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  font-size: 13px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.15);
  padding-bottom: 4px;
}

.note-tooltip-badge {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 3px;
  font-weight: 500;
}

.note-tooltip-badge.is-warning {
  background-color: rgba(234, 179, 8, 0.25);
  color: #fde047;
}

.note-tooltip-badge.is-info {
  background-color: rgba(56, 189, 248, 0.25);
  color: #7dd3fc;
}

.note-tooltip-title {
  color: #f8fafc;
  font-weight: 600;
}

.note-tooltip-body {
  font-size: 12px;
  color: #cbd5e1;
  white-space: normal;
  word-break: break-word;
}

.cond-btn-wrapper {
  display: inline-flex;
  align-items: center;
}

.cond-note-banner.is-warning {
  background-color: #fefce8;
  border: 1px solid #fef08a;
  color: #854d0e;
}

.cond-note-banner.is-info {
  background-color: #f0f9ff;
  border: 1px solid #bae6fd;
  color: #0369a1;
}

.note-icon {
  font-size: 15px;
  flex-shrink: 0;
}

.note-content {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}

.note-tag {
  font-size: 11px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 4px;
  letter-spacing: 0.2px;
}

.cond-note-banner.is-warning .note-tag {
  background-color: #fde047;
  color: #713f12;
}

.cond-note-banner.is-info .note-tag {
  background-color: #bae6fd;
  color: #0369a1;
}

.note-text {
  font-size: 12px;
  font-weight: 500;
}

.order-meta {
  font-size: 12px;
  color: #64748b;
  margin-top: 2px;
}

.order-time {
  font-size: 12px;
  color: #64748b;
  margin-left: 4px;
}

.cond-actions {
  display: flex;
  gap: 10px;
  align-items: center;
  justify-content: flex-end;
  flex-shrink: 0;
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

.warning-ts-tag,
.trigger-ts-tag {
  display: inline-flex;
  align-items: center;
  font-size: 11px;
  color: #475569;
  background: #f1f5f9;
  border: 1px solid #e2e8f0;
  padding: 2px 7px;
  border-radius: 4px;
  white-space: nowrap;
  line-height: 1.5;
  flex-shrink: 0;
}

.trigger-ts-tag {
  color: #0284c7;
  background: #f0f9ff;
  border-color: #bae6fd;
  font-weight: 500;
}

.head-right {
  display: flex;
  align-items: center;
  gap: 10px;
  white-space: nowrap;
  flex-shrink: 0;
}

.wait-progress {
  font-size: 12px;
  color: #64748b;
  white-space: nowrap;
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
