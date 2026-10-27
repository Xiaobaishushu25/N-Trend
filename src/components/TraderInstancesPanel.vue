<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api } from '../services/api'
import { useLiveTraderStore } from '../stores/liveTrader'
import type { CtpConnectionSettings, ExecutionConfig, StrategyDefinition } from '../types'
import SimnowLoginFields from './SimnowLoginFields.vue'
import { emptySimnowLogin, ctpConnectionLabel } from '../utils/simnow'


const trader = useLiveTraderStore()
const selected = computed(() => trader.instanceCatalog?.instances.find(r => r.instance.id === trader.selectedInstanceId))
const dialog = ref<'create' | 'configure' | 'login' | 'rename' | null>(null)
const busy = ref(false)
const error = ref('')
const name = ref('')
const renameInstanceName = ref('')
const kind = ref<'paper' | 'ctp' | 'simnow'>('paper')
const simnow = ref(emptySimnowLogin())
const savedCredentials = ref(false)
const loginTarget = ref('')
const notice = ref('')
const accountLabel = (account?: { kind: string; broker_id?: string }) => account?.kind === 'paper' ? '本地模拟' : account?.broker_id === '9999' ? 'SimNow 仿真（CTP）' : 'CTP 实盘'
watch(dialog, value => { if (!value) { simnow.value = emptySimnowLogin(); savedCredentials.value = false } })
const broker = ref('')
const user = ref('')
const port = ref(8767)
const credential = ref('')
const balance = ref(50000)
const copyFrom = ref<string | undefined>()
const contracts = ref([{ symbol: '', multiplier: '', margin: '', open: '', close: '', today: '' }])
const config = ref<ExecutionConfig | null>(null)
const strategy = ref<StrategyDefinition | null>(null)
const parameters = ref('{}')
const createPreset = ref('')
const presets = computed(() => trader.instanceCatalog?.modules?.presets || [])
const isCandidate = computed(() => ['n_p2', 'n_p3', 'n_b5_six'].includes(strategy.value?.strategy_id || ''))
const isB5 = computed(() => strategy.value?.strategy_id === 'n_b5_six')
const compareOpen = ref(false)
const compareBusy = ref(false)
const compare = ref<any>(null)
function openRename() {
  if (!selected.value) return
  renameInstanceName.value = selected.value.instance.name
  error.value = ''
  notice.value = ''
  dialog.value = 'rename'
}
async function commitRename() {
  if (busy.value || !selected.value) return
  const trimmed = renameInstanceName.value.trim()
  if (!trimmed) { error.value = '实例名称不能为空'; return }
  busy.value = true; error.value = ''
  try {
    await trader.instanceAction({ action: 'rename', name: trimmed })
    dialog.value = null
    notice.value = `实例已重命名为【${trimmed}】`
  } catch (e) {
    error.value = String(e)
  } finally {
    busy.value = false
  }
}
const localDate = (timestamp: number) => {
  const d = new Date(timestamp); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16)
}
const from = ref(localDate(Date.now() - 30 * 86400000))
const to = ref(localDate(Date.now()))
const number = (v: unknown, digits = 2) => typeof v === 'number' && Number.isFinite(v) ? v.toFixed(digits) : '—'
const moduleLabel = (id: string) => ({ n_b5_six: '六品种 B5', b5_structural_exit_v1: 'B5 结构目标与固定止损', n_pattern: 'N 字形', n_exit_v1: '现有 N 字形退出', n_score_v1: '现有评分分档', fixed_v1: '计划手数', protect_only_v1: '保护价退出', n_p2: 'P2 · ATR / 原 S1', n_p3: 'P3 · ATR / 2R 上限', p2_fixed_exit_v1: 'P2 固定止损与原 S1', p3_fixed_exit_v1: 'P3 固定止损与 2R 上限', candidate_one_lot_v1: 'P2/P3 固定一手' }[id] || id)

const connectionTooltip = computed(() => {
  if (trader.errorMsg) return trader.errorMsg
  if (!trader.bridgeOnline) return '交易桥后台服务未启动或未连接'
  if (!trader.health?.connected) {
    return '正在连接 CTP 交易前置机。\n提示：在盘后结算、夜盘停盘、周末或节假日休市期间，期货公司/SimNow 通常会关闭前置服务端口，属正常重试状态；开市前会自动恢复连通。'
  }
  if (!trader.health?.authenticated || !trader.health?.logged_in) {
    return '交易前置机已连通，正在进行穿透式认证与资金账号登录…'
  }
  if (!trader.health?.ready) {
    return '已登录 CTP 柜台，正在同步资金账户与持仓记录…'
  }
  return 'CTP 柜台通信正常，资金与持仓已同步完成。'
})

function applyPreset(id: string) {
  const preset = presets.value.find(p => p.definition.strategy_id === id)
  if (!preset) {
    if (id === 'n_pattern' && strategy.value) {
      strategy.value = { ...strategy.value, strategy_id: id, code_version: '1', exit_policy: 'n_exit_v1', sizing_policy: 'n_score_v1', parameters: null }
      parameters.value = 'null'
    }
    return
  }
  strategy.value = JSON.parse(JSON.stringify(preset.definition)) as StrategyDefinition
  parameters.value = JSON.stringify(strategy.value.parameters, null, 2)
  if (config.value) Object.assign(config.value, { min_entry_risk_ticks: 10, default_large_volume: 1, default_small_volume: 1, slippage_ticks: 0 })
}

function openCreate(copy: boolean) {
  name.value = copy ? `${selected.value?.instance.name || '策略'} 副本` : 'N 字形策略'
  copyFrom.value = copy ? trader.selectedInstanceId : undefined
  createPreset.value = ''
  kind.value = 'paper'; simnow.value = emptySimnowLogin(); error.value = ''; notice.value = ''; dialog.value = 'create'
}
async function openLogin() {
  if (!selected.value || busy.value) return
  const id = trader.selectedInstanceId
  loginTarget.value = id; error.value = ''; notice.value = ''; simnow.value = emptySimnowLogin()
  simnow.value.user_id = selected.value.account.user_id || ''
  savedCredentials.value = false; dialog.value = 'login'; busy.value = true
  try {
    const connection = await api.traderInstanceRequest<CtpConnectionSettings>('connection', id)
    simnow.value.user_id = connection.user_id || simnow.value.user_id
    simnow.value.td_front = connection.td_front || simnow.value.td_front
    simnow.value.app_id = connection.app_id || simnow.value.app_id
    savedCredentials.value = !!connection.password_saved && !!connection.auth_code_saved
    if (savedCredentials.value) simnow.value.auth_code = ''
  } catch (e) { error.value = `读取连接配置失败：${String(e)}` }
  finally { busy.value = false }
}
async function login() {
  if (busy.value) return
  busy.value = true; error.value = ''
  try {
    await trader.loginSimnow(loginTarget.value, { ...simnow.value })
    dialog.value = null
    notice.value = '连接请求已提交，正在等待柜台认证和资金持仓同步；策略启用状态未改变。'
  } catch (e) { error.value = String(e) }
  finally { simnow.value.password = ''; busy.value = false }
}
function openConfigure() {
  if (!selected.value) return
  config.value = { ...selected.value.config }
  strategy.value = { ...selected.value.strategy }
  parameters.value = JSON.stringify(selected.value.strategy.parameters ?? {}, null, 2)
  renameInstanceName.value = selected.value.instance.name
  error.value = ''
  dialog.value = 'configure'
}
async function create() {
  if (busy.value) return
  busy.value = true; error.value = ''
  try {
    const id = `account-${crypto.randomUUID()}`
    let account: Record<string, unknown> = { id, name: name.value, kind: kind.value }
    if (kind.value === 'ctp') {
      account = { ...account, port: port.value, broker_id: broker.value.trim(), user_id: user.value.trim(), credential_ref: credential.value.trim() }
    } else if (kind.value === 'paper') {
      const mapping: Record<string, unknown> = {}
      for (const row of contracts.value) {
        if (!row.symbol.trim() || [row.multiplier, row.margin, row.open, row.close, row.today].some(v => v === '' || !Number.isFinite(Number(v)))) throw Error('请完整填写每个合约的乘数、保证金和手续费；费用可以明确填写 0')
        const symbol = row.symbol.trim().toUpperCase()
        if (mapping[symbol]) throw Error('模拟合约重复')
        mapping[symbol] = { multiplier: Number(row.multiplier), margin_rate: Number(row.margin) / 100,
          open_fee_per_lot: Number(row.open), close_fee_per_lot: Number(row.close), close_today_fee_per_lot: Number(row.today) }
      }
      account.config = { initial_balance: balance.value, contracts: mapping }
    }
    const preset = presets.value.find(p => p.definition.strategy_id === createPreset.value)
    const candidateConfig: ExecutionConfig = { mode: 'SemiAuto', max_risk_percent: 1, daily_loss_limit_r: 3, default_large_volume: 1, default_small_volume: 1, order_wait_seconds: 3, slippage_ticks: 0, max_slippage_ticks: 20, max_exit_slippage_ticks: 0, min_entry_risk_ticks: 10 }
    const result = await api.traderInstanceRequest<{ instance_id: string }>('create', undefined, { name: name.value, ...(kind.value === 'simnow' ? { simnow: { ...simnow.value } } : { account }), copy_from: copyFrom.value,
      ...(preset ? { strategy: preset.definition, config: candidateConfig } : {}) })
    await trader.loadInstances(); await trader.selectInstance(result.instance_id)
    dialog.value = null
    notice.value = kind.value === 'simnow' ? '已创建独立 SimNow 账户并提交连接；策略保持暂停，请核对资金持仓和配置后启用。' : ''
  } catch (e) { error.value = String(e) } finally { simnow.value.password = ''; busy.value = false }
}
async function save() {
  if (busy.value || !config.value || !strategy.value) return
  busy.value = true; error.value = ''
  try {
    const trimmedRename = renameInstanceName.value.trim()
    if (!trimmedRename) throw new Error('实例名称不能为空')
    if (selected.value && trimmedRename !== selected.value.instance.name) {
      await trader.instanceAction({ action: 'rename', name: trimmedRename })
    }
    const next = {
      ...strategy.value,
      parameters: JSON.parse(parameters.value),
    }
    // One operation commits module selection and execution parameters together.
    await trader.instanceAction({ action: 'configure_strategy', strategy: next, config: config.value })
    dialog.value = null
    notice.value = '配置已保存。'
  } catch (e) { error.value = String(e) } finally { busy.value = false }
}
async function togglePause() {
  if (!selected.value || busy.value) return
  busy.value = true; error.value = ''
  try { await trader.instanceAction({ action: selected.value.paused ? 'resume' : 'pause' }) }
  catch (e) { error.value = String(e) } finally { busy.value = false }
}
async function loadComparison() {
  compareBusy.value = true; error.value = ''
  try {
    compare.value = await api.traderInstanceRequest('comparison', undefined, {
      from: Math.floor(new Date(from.value).getTime() / 1000), to: Math.floor(new Date(to.value).getTime() / 1000),
    })
  } catch (e) { error.value = String(e) } finally { compareBusy.value = false }
}
async function showComparison() { compareOpen.value = !compareOpen.value; if (compareOpen.value) await loadComparison() }
function curve(samples: { equity: number }[]) {
  if (samples.length < 2) return ''
  const low = Math.min(...samples.map(p => p.equity)), high = Math.max(...samples.map(p => p.equity))
  return samples.map((p, i) => `${i * 160 / (samples.length - 1)},${32 - (p.equity - low) / (high - low || 1) * 28}`).join(' ')
}

function popOutWindow(instanceId?: string, instanceName?: string) {
  if (!instanceId) return
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('open-live-trader-window', { detail: { instanceId, name: instanceName } }))
  }
}
</script>

<template>
  <section class="instances-panel" aria-label="策略实例与账户">
    <!-- 服务不支持或未准备好提示 -->
    <div v-if="!trader.instancesSupported || trader.instancesError" class="instance-status-banner" aria-live="polite">
      <div class="banner-content">
        <svg class="icon-svg banner-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="10" />
          <line x1="12" y1="8" x2="12" y2="12" />
          <line x1="12" y1="16" x2="12.01" y2="16" />
        </svg>
        <span>{{ trader.instancesLoading ? '正在检查多账户服务…' : trader.instancesError || '正在检查多账户服务…' }}</span>
      </div>
      <button type="button" class="btn btn-outline btn-sm" :disabled="trader.instancesLoading" @click="trader.loadInstances()">
        {{ trader.instancesLoading ? '检查中…' : '重新检查' }}
      </button>
    </div>

    <!-- 顶部综合控制工具栏 -->
    <template v-if="trader.instancesSupported">
      <div class="instance-bar">
        <!-- 左侧：实例切换、账户类型、连接状态、启停与新窗口盯盘 -->
        <div class="bar-left">
          <div class="instance-selector-group">
            <span class="selector-label">策略实例</span>
            <div class="select-box">
              <select
                :value="trader.selectedInstanceId"
                :disabled="busy"
                class="instance-select"
                @change="trader.selectInstance(($event.target as HTMLSelectElement).value)"
              >
                <option v-for="row in trader.instanceCatalog?.instances" :key="row.instance.id" :value="row.instance.id">
                  {{ row.instance.name }} ({{ row.account.user_id || row.account.name || '本地模拟' }})
                </option>
              </select>
              <svg class="select-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polyline points="6 9 12 15 18 9" />
              </svg>
            </div>

            <!-- 快捷重命名当前实例名称 -->
            <button
              type="button"
              class="btn-icon-rename"
              title="重命名当前实例"
              :disabled="busy || !selected"
              @click="openRename"
            >
              <svg class="icon-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M12 20h9" />
                <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
              </svg>
            </button>
          </div>

          <!-- 账户类型徽章 -->
          <span class="badge-kind" :class="selected?.account.kind">
            {{ accountLabel(selected?.account) }}
          </span>

          <!-- 实例运行 / 暂停状态快速切换 -->
          <button
            type="button"
            class="btn btn-sm btn-state"
            :class="selected?.paused ? 'is-paused' : 'is-running'"
            :disabled="busy"
            :title="selected?.paused ? '已暂停新入场，点击恢复运行' : '运行中，点击暂停新入场（继续管理现有持仓）'"
            @click="togglePause"
          >
            <span class="state-dot" />
            {{ selected?.paused ? '已暂停 (点此启用)' : '运行中 (点此暂停)' }}
          </button>

          <!-- 核心功能：在新窗口独立打开此账户盯盘 -->
          <button
            type="button"
            class="btn btn-sm btn-popout"
            title="在独立新窗口打开此账户，方便多屏或并排同时盯盘"
            :disabled="!selected"
            @click="popOutWindow(selected?.instance.id, selected?.instance.name)"
          >
            <svg class="icon-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M12 6h-6a2 2 0 0 0 -2 2v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2 -2v-6" />
              <path d="M11 13l9 -9" />
              <path d="M15 4h5v5" />
            </svg>
            在新窗口盯盘
          </button>
        </div>

        <!-- 右侧：前置连接状态与登录认证 (独立置于右上角)，以及配置、新建、克隆与账户对比 -->
        <div class="bar-right">
          <!-- 柜台连接与登录认证组（右上角独立） -->
          <div v-if="selected?.account.kind === 'ctp'" class="connection-group">
            <!-- CTP 前置机连接状态徽章 -->
            <span
              class="badge-conn"
              :class="{
                'is-ready': trader.isReady,
                'is-syncing': trader.health?.sync_pending,
                'is-err': trader.health?.login_error || trader.health?.sync_error,
              }"
              :title="connectionTooltip"
              role="status"
            >
              <span class="conn-dot" />
              {{ ctpConnectionLabel(trader.health, trader.bridgeOnline) }}
            </span>

            <!-- SimNow 快速登录/认证配置 -->
            <button
              v-if="!selected.account.broker_id || selected.account.broker_id === '9999'"
              type="button"
              class="btn btn-sm btn-login"
              :disabled="busy"
              title="配置或更换 SimNow 账号与穿透式认证码"
              @click="openLogin"
            >
              <svg class="icon-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4" />
                <polyline points="10 17 15 12 10 7" />
                <line x1="15" y1="12" x2="3" y2="12" />
              </svg>
              SimNow 登录 / 认证
            </button>
          </div>

          <div v-if="selected?.account.kind === 'ctp'" class="divider-v" />

          <div class="actions-group">
            <button type="button" class="btn btn-sm btn-action" :disabled="busy" @click="openConfigure">
              <svg class="icon-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <circle cx="12" cy="12" r="3" />
                <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
              </svg>
              配置
            </button>

            <button type="button" class="btn btn-sm btn-action" :disabled="busy" @click="openCreate(false)">
              <svg class="icon-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <line x1="12" y1="5" x2="12" y2="19" />
                <line x1="5" y1="12" x2="19" y2="12" />
              </svg>
              新建
            </button>

            <button type="button" class="btn btn-sm btn-action" :disabled="busy" title="复制当前策略配置到新账户" @click="openCreate(true)">
              <svg class="icon-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
                <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
              </svg>
              克隆
            </button>

            <button
              type="button"
              class="btn btn-sm btn-action"
              :class="{ 'is-active': compareOpen }"
              @click="showComparison"
            >
              <svg class="icon-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <line x1="18" y1="20" x2="18" y2="10" />
                <line x1="12" y1="20" x2="12" y2="4" />
                <line x1="6" y1="20" x2="6" y2="14" />
              </svg>
              {{ compareOpen ? '收起对比' : '账户对比' }}
            </button>
          </div>
        </div>
      </div>

      <!-- 消息反馈横条 -->
      <div v-if="error" role="alert" class="alert-bar is-error">
        <svg class="icon-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="10" />
          <line x1="15" y1="9" x2="9" y2="15" />
          <line x1="9" y1="9" x2="15" y2="15" />
        </svg>
        <span>{{ error }}</span>
      </div>

      <div v-if="notice" role="status" class="alert-bar is-notice">
        <svg class="icon-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="10" />
          <line x1="12" y1="16" x2="12" y2="12" />
          <line x1="12" y1="8" x2="12.01" y2="8" />
        </svg>
        <span>{{ notice }}</span>
      </div>

      <!-- 账户对比面板（优雅抽屉式展开） -->
      <div v-if="compareOpen" class="account-comparison-drawer">
        <div class="drawer-header">
          <div class="drawer-title">
            <svg class="icon-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="18" y1="20" x2="18" y2="10" />
              <line x1="12" y1="20" x2="12" y2="4" />
              <line x1="6" y1="20" x2="6" y2="14" />
            </svg>
            <span>多策略/多账户实盘与模拟净值对比</span>
          </div>

          <div class="drawer-controls">
            <label class="date-label">开始 <input v-model="from" type="datetime-local" class="input-date"></label>
            <label class="date-label">结束 <input v-model="to" type="datetime-local" class="input-date"></label>
            <button type="button" class="btn btn-sm btn-primary" :disabled="compareBusy" @click="loadComparison">
              {{ compareBusy ? '计算中…' : '刷新对比' }}
            </button>
            <button type="button" class="btn btn-sm btn-outline" @click="compareOpen = false">收起</button>
          </div>
        </div>

        <div class="table-scroll">
          <table class="comparison-table">
            <thead>
              <tr>
                <th>策略 / 账户</th>
                <th>类型</th>
                <th>净值走势</th>
                <th>净值变化</th>
                <th>收益率</th>
                <th>最大回撤</th>
                <th>完整交易</th>
                <th>毛胜率</th>
                <th>毛盈亏因子</th>
                <th>最大保证金</th>
                <th>观察费用</th>
                <th>数据质量</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in compare?.instances" :key="row.instance.id" :class="{ 'row-active': row.instance.id === trader.selectedInstanceId }">
                <td>
                  <div class="inst-cell">
                    <span class="inst-name-text">{{ row.instance.name }}</span>
                    <small class="inst-sub-text">{{ row.account.name }}</small>
                  </div>
                </td>
                <td><span class="badge-kind" :class="row.account.kind">{{ accountLabel(row.account) }}</span></td>
                <td>
                  <svg v-if="row.sample_count > 1" viewBox="0 0 160 36" width="160" height="36" class="equity-chart" aria-label="账户净值走势">
                    <polyline :points="curve(row.equity_curve)" fill="none" stroke="#2563eb" stroke-width="1.8" />
                  </svg>
                  <span v-else class="text-muted">样本不足</span>
                </td>
                <td :class="(row.net_profit || 0) >= 0 ? 'text-up' : 'text-down'">{{ number(row.net_profit) }}</td>
                <td :class="(row.return_pct || 0) >= 0 ? 'text-up' : 'text-down'">{{ number(row.return_pct) }}%</td>
                <td>{{ number(row.max_drawdown_pct) }}%</td>
                <td>{{ row.trades }}</td>
                <td>{{ number(row.win_rate_pct) }}%</td>
                <td>{{ number(row.profit_factor) }}</td>
                <td>{{ number(row.max_margin) }}</td>
                <td>{{ number(row.observed_fees) }}</td>
                <td>
                  <span v-if="!row.quality_flags?.length" class="quality-badge is-good">完整</span>
                  <span v-else class="quality-badge is-warn">
                    {{ row.quality_flags.map((q: string) => ({ quote_gap: '行情有缺口', equity_sample_gap: '净值缺样本', fees_unconfirmed: '费用待确认', insufficient_samples: '样本不足' }[q] || q)).join('、') }}
                  </span>
                </td>
                <td>
                  <button
                    type="button"
                    class="btn btn-xs btn-popout-table"
                    title="新窗口独立盯盘"
                    @click="popOutWindow(row.instance.id, row.instance.name)"
                  >
                    新窗口盯盘
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div v-if="compare?.signal_pairs?.length" class="drawer-pairs">
          <p v-for="pair in compare?.signal_pairs" :key="`${pair.baseline_instance_id}:${pair.candidate_instance_id}`" class="pair-hint">
            <strong>{{ trader.instanceCatalog?.instances.find(r => r.instance.id === pair.candidate_instance_id)?.instance.name }}</strong> 相对
            <strong>{{ trader.instanceCatalog?.instances.find(r => r.instance.id === pair.baseline_instance_id)?.instance.name }}</strong>：
            同源完整信号 {{ pair.paired_signals }} 笔，毛收益差额
            <span :class="(pair.gross_profit_difference || 0) >= 0 ? 'text-up' : 'text-down'">{{ number(pair.gross_profit_difference) }} 元</span>。
          </p>
        </div>
        <p class="drawer-footer-note">{{ compare?.note || '各账户可能只有部分区间的数据；交易胜率与盈亏因子按毛收益计算，未平仓交易不计入。净值样本每分钟保存一次。' }}</p>
      </div>

      <!-- 弹窗设计：创建 / 登录 / 配置 / 重命名 -->
      <div v-if="dialog" class="modal-backdrop" @click.self="!busy && (dialog = null)">
        <form class="instance-dialog" @submit.prevent="dialog === 'create' ? create() : dialog === 'login' ? login() : dialog === 'rename' ? commitRename() : save()">
          <div class="dialog-header">
            <h3>{{ dialog === 'create' ? '创建策略实例' : dialog === 'login' ? 'SimNow 仿真账户登录' : dialog === 'rename' ? '重命名策略实例' : '配置策略与执行参数' }}</h3>
            <button type="button" class="btn-close" :disabled="busy" @click="dialog = null">✕</button>
          </div>

          <div class="dialog-body">
            <!-- 创建实例 -->
            <template v-if="dialog === 'create'">
              <div class="form-row">
                <label class="form-label">实例名称</label>
                <input v-model="name" class="form-input" required maxlength="160" placeholder="例如：N字形螺纹实盘">
              </div>
              <p v-if="copyFrom" class="field-hint">将复制当前所选策略的模块及参数；条件单、在手持仓、交易历史与资金均完全独立。</p>

              <div class="form-row">
                <label class="form-label">策略方案预设</label>
                <select v-model="createPreset" class="form-select">
                  <option value="">{{ copyFrom ? '沿用所选策略配置' : '现有 N 字形策略' }}</option>
                  <option v-for="p in presets" :key="p.definition.strategy_id" :value="p.definition.strategy_id">{{ p.label }}</option>
                </select>
              </div>
              <p v-if="createPreset" class="field-hint">候选策略使用固定一手、10 tick 最小风险和半自动模式。创建后请核对费用与日历；六品种 B5 固定交易 EG、PP、PVC、JD、SR、CJ。</p>

              <div class="form-row">
                <label class="form-label">账户类型</label>
                <select v-model="kind" class="form-select">
                  <option value="paper">本地模拟账户（无柜台连接，独立账本撮合）</option>
                  <option value="simnow">SimNow 仿真账户（CTP 协议仿真柜台）</option>
                  <option value="ctp">其他 CTP 实盘账户（服务端凭据文件）</option>
                </select>
              </div>

              <SimnowLoginFields v-if="kind === 'simnow'" v-model="simnow" />

              <template v-if="kind === 'ctp'">
                <div class="form-grid">
                  <div class="form-row"><label class="form-label">券商编号 (BrokerID)</label><input v-model="broker" class="form-input" required></div>
                  <div class="form-row"><label class="form-label">用户编号 (UserID)</label><input v-model="user" class="form-input" required></div>
                  <div class="form-row"><label class="form-label">独立桥接端口</label><input v-model.number="port" type="number" min="1" max="65535" class="form-input" required></div>
                  <div class="form-row"><label class="form-label">服务端凭据名称</label><input v-model="credential" pattern="[A-Za-z0-9_-]+" class="form-input" required placeholder="如 account_b"></div>
                </div>
                <p class="field-hint">由服务端 ctp_credentials 目录中同名 JSON 文件提供凭据。页面只保存引用，账户身份必须与文件严格一致。</p>
              </template>

              <template v-else-if="kind === 'paper'">
                <div class="form-row">
                  <label class="form-label">初始资金 (元)</label>
                  <input v-model.number="balance" type="number" min="1" step="any" class="form-input" required>
                </div>
                <p class="field-hint">按实际合约填写撮合参数。手续费单位为元/手，填写 0 表示零手续费；未配置的合约拒绝开仓。</p>

                <fieldset v-for="(row, index) in contracts" :key="index" class="contract-fieldset">
                  <legend>合约配置 #{{ index + 1 }}</legend>
                  <div class="contract-fields">
                    <div class="form-row"><label class="form-label">实际合约</label><input v-model="row.symbol" class="form-input" placeholder="例如 rb2701" required></div>
                    <div class="form-row"><label class="form-label">合约乘数</label><input v-model="row.multiplier" type="number" min="0.000001" step="any" class="form-input" required></div>
                    <div class="form-row"><label class="form-label">保证金率 %</label><input v-model="row.margin" type="number" min="0.000001" max="100" step="any" class="form-input" required></div>
                    <div class="form-row"><label class="form-label">开仓费 (元/手)</label><input v-model="row.open" type="number" min="0" step="any" class="form-input" required></div>
                    <div class="form-row"><label class="form-label">平昨费 (元/手)</label><input v-model="row.close" type="number" min="0" step="any" class="form-input" required></div>
                    <div class="form-row"><label class="form-label">平今费 (元/手)</label><input v-model="row.today" type="number" min="0" step="any" class="form-input" required></div>
                  </div>
                  <button v-if="contracts.length > 1" type="button" class="btn btn-sm btn-danger mt-2" @click="contracts.splice(index, 1)">移除此合约</button>
                </fieldset>
                <button type="button" class="btn btn-sm btn-outline mt-2" @click="contracts.push({ symbol: '', multiplier: '', margin: '', open: '', close: '', today: '' })">+ 添加更多合约</button>
              </template>
              <p class="field-hint">新创建的实例默认保持暂停状态，核对配置无误后手动启用。新实例仅接收创建后的新信号。</p>
            </template>

            <!-- 登录 SimNow -->
            <template v-else-if="dialog === 'login'">
              <div class="dialog-desc">
                当前正在连接 <strong>{{ selected?.instance.name }}</strong>（{{ selected?.account.name }}）。重新登录将重建账户专属通信桥，并重新同步资金、在手持仓与在途委托。
              </div>
              <SimnowLoginFields v-model="simnow" :saved="savedCredentials" :lock-user="!!selected?.account.user_id" />
            </template>

            <!-- 快捷重命名策略实例 -->
            <template v-else-if="dialog === 'rename'">
              <div class="dialog-desc">
                重命名当前实例（席位显示名称）。此操作仅更改各窗口和下拉框的识别名称，不会影响底层交易或账户资金。
              </div>
              <div class="form-row">
                <label class="form-label">实例名称</label>
                <input
                  v-model="renameInstanceName"
                  class="form-input"
                  required
                  maxlength="160"
                  placeholder="例如：螺纹实盘1号、模拟测试A"
                  autofocus
                >
              </div>
            </template>

            <!-- 配置策略参数 -->
            <template v-else-if="config && strategy">
              <!-- 实例名称 -->
              <div class="form-row">
                <label class="form-label">
                  实例名称
                  <span class="label-tip">席位与盯盘显示名称</span>
                </label>
                <input
                  v-model="renameInstanceName"
                  class="form-input"
                  required
                  maxlength="160"
                  placeholder="例如：螺纹实盘1号"
                >
              </div>

              <!-- 快速套用策略模板 -->
              <div class="preset-load-row">
                <span class="ctrl-label">快速套用策略模板:</span>
                <div class="preset-buttons">
                  <button v-for="p in presets" :key="p.definition.strategy_id" type="button" class="btn btn-xs btn-outline" @click="applyPreset(p.definition.strategy_id)">
                    {{ p.label }}
                  </button>
                </div>
              </div>

              <div class="form-grid">
                <div class="form-row">
                  <label class="form-label">信号模块</label>
                  <select v-model="strategy.strategy_id" class="form-select" @change="applyPreset(strategy.strategy_id)">
                    <option v-for="s in trader.instanceCatalog?.modules.strategies" :key="s.id" :value="s.id">{{ moduleLabel(s.id) }}</option>
                  </select>
                </div>

                <div class="form-row">
                  <label class="form-label">代码版本</label>
                  <select v-model="strategy.code_version" class="form-select">
                    <option v-for="s in trader.instanceCatalog?.modules.strategies.filter(s => s.id === strategy?.strategy_id)" :key="s.version" :value="s.version">{{ s.version }}</option>
                  </select>
                </div>

                <div class="form-row">
                  <label class="form-label">退出模块</label>
                  <select v-model="strategy.exit_policy" class="form-select">
                    <option v-for="id in trader.instanceCatalog?.modules.exit_policies" :key="id" :value="id">{{ moduleLabel(id) }}</option>
                  </select>
                </div>

                <div class="form-row">
                  <label class="form-label">仓位模块</label>
                  <select v-model="strategy.sizing_policy" class="form-select">
                    <option v-for="id in trader.instanceCatalog?.modules.sizing_policies" :key="id" :value="id">{{ moduleLabel(id) }}</option>
                  </select>
                </div>

                <div class="form-row">
                  <label class="form-label">大仓手数</label>
                  <input v-model.number="config.default_large_volume" type="number" min="1" class="form-input" required>
                </div>

                <div class="form-row">
                  <label class="form-label">小仓手数</label>
                  <input v-model.number="config.default_small_volume" type="number" min="1" class="form-input" required>
                </div>

                <div class="form-row">
                  <label class="form-label">最小入场风险 (tick)</label>
                  <input v-model.number="config.min_entry_risk_ticks" type="number" min="0" class="form-input" required>
                </div>

                <div class="form-row">
                  <label class="form-label">报单偏移滑点 (tick)</label>
                  <input v-model.number="config.slippage_ticks" type="number" min="0" max="100" class="form-input" required>
                </div>
              </div>

              <div class="form-row full-width">
                <label class="form-label">策略模块参数 (JSON)</label>
                <textarea v-model="parameters" rows="4" spellcheck="false" class="form-textarea font-mono" />
              </div>

              <p v-if="isB5" class="field-hint">六品种 B5：最近合格 A、不合并摆点、目标成本比≤10%、A 净实体同向；小时不顺向同组同向最多两单，同 A 两次原止损后停试。固定一手，独立生成新预警。</p>
              <p v-else-if="isCandidate" class="field-hint">P2/P3 固定一手、原结构止损；P3 按实际成交价锁定目标。修改参数仅对保存后产生的新信号生效，已有条件单和持仓保留原版本运行。</p>
            </template>

            <div v-if="error" role="alert" class="alert-bar is-error">{{ error }}</div>
          </div>

          <div class="dialog-actions">
            <button type="button" class="btn btn-outline" :disabled="busy" @click="dialog = null">取消</button>
            <button type="submit" class="btn btn-primary" :disabled="busy">
              {{ busy ? '处理中…' : dialog === 'login' ? '登录 / 重新连接' : kind === 'simnow' && dialog === 'create' ? '创建并连接' : dialog === 'rename' ? '保存重命名' : '保存配置' }}
            </button>
          </div>
        </form>
      </div>
    </template>
  </section>
</template>

<style scoped>
.instances-panel {
  background: #ffffff;
  border-bottom: 1px solid #e2e8f0;
  padding: 6px 16px;
  flex-shrink: 0;
  box-sizing: border-box;
}

/* 顶部状态提示条 */
.instance-status-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  background: #f8fafc;
  border: 1px solid #cbd5e1;
  border-radius: 6px;
  font-size: 13px;
  color: #334155;
}
.banner-content {
  display: flex;
  align-items: center;
  gap: 8px;
}
.banner-icon {
  width: 16px;
  height: 16px;
  color: #0284c7;
}

/* 综合控制栏 */
.instance-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px 14px;
}

.bar-left, .bar-right {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

/* 实例选择器 */
.instance-selector-group {
  display: flex;
  align-items: center;
  gap: 6px;
}

.selector-label {
  font-size: 12px;
  font-weight: 600;
  color: #475569;
  white-space: nowrap;
}

.select-box {
  position: relative;
  display: inline-flex;
  align-items: center;
}

.instance-select {
  appearance: none;
  background: #f8fafc;
  border: 1px solid #cbd5e1;
  border-radius: 6px;
  padding: 4px 26px 4px 10px;
  font-size: 13px;
  font-weight: 600;
  color: #0f172a;
  cursor: pointer;
  outline: none;
  transition: all 0.15s ease;
  max-width: 280px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.instance-select:hover {
  border-color: #2563eb;
  background: #ffffff;
}

.instance-select:focus {
  border-color: #2563eb;
  box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.15);
}

.select-chevron {
  position: absolute;
  right: 8px;
  width: 14px;
  height: 14px;
  color: #64748b;
  pointer-events: none;
}

/* 实例重命名小按钮 */
.btn-icon-rename {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  padding: 0;
  border-radius: 5px;
  background: #f8fafc;
  border: 1px solid #cbd5e1;
  color: #64748b;
  cursor: pointer;
  transition: all 0.15s ease;
}
.btn-icon-rename:hover:not(:disabled) {
  background: #eff6ff;
  border-color: #2563eb;
  color: #2563eb;
}

/* 徽章胶囊 */
.badge-kind {
  display: inline-flex;
  align-items: center;
  padding: 3px 8px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
}
.badge-kind.paper {
  background: #dbeafe;
  color: #1e40af;
}
.badge-kind.ctp {
  background: #fef3c7;
  color: #92400e;
}
.badge-kind.simnow {
  background: #fef3c7;
  color: #92400e;
}

.badge-conn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 8px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 500;
  background: #f1f5f9;
  color: #475569;
  cursor: help;
}
.badge-conn.is-ready {
  background: #dcfce7;
  color: #166534;
}
.badge-conn.is-syncing {
  background: #fef9c3;
  color: #854d0e;
}
.badge-conn.is-err {
  background: #fee2e2;
  color: #991b1b;
}

.conn-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}

/* 按钮基础风格 */
.btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 10px;
  font-size: 12px;
  font-weight: 500;
  border-radius: 6px;
  border: 1px solid transparent;
  cursor: pointer;
  outline: none;
  transition: all 0.15s ease;
  white-space: nowrap;
  user-select: none;
}
.btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.btn-sm {
  padding: 3px 9px;
  font-size: 12px;
}
.btn-xs {
  padding: 2px 7px;
  font-size: 11px;
}

.btn-outline {
  background: #ffffff;
  border-color: #cbd5e1;
  color: #334155;
}
.btn-outline:hover:not(:disabled) {
  border-color: #2563eb;
  color: #2563eb;
  background: #eff6ff;
}

.btn-primary {
  background: #2563eb;
  color: #ffffff;
}
.btn-primary:hover:not(:disabled) {
  background: #1d4ed8;
}

.btn-danger {
  background: #fee2e2;
  color: #dc2626;
  border-color: #fecaca;
}
.btn-danger:hover:not(:disabled) {
  background: #fca5a5;
}

/* 启停状态按钮 */
.btn-state {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
}
.btn-state.is-running {
  background: #ecfdf5;
  color: #047857;
  border: 1px solid #a7f3d0;
}
.btn-state.is-running:hover:not(:disabled) {
  background: #d1fae5;
  border-color: #6ee7b7;
}
.btn-state.is-paused {
  background: #fffbeb;
  color: #b45309;
  border: 1px solid #fde68a;
}
.btn-state.is-paused:hover:not(:disabled) {
  background: #fef3c7;
  border-color: #fcd34d;
}

.state-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
}

/* 核心：独立新窗口盯盘按钮 */
.btn-popout {
  background: #eff6ff;
  color: #1d4ed8;
  border: 1px solid #bfdbfe;
  font-weight: 600;
}
.btn-popout:hover:not(:disabled) {
  background: #dbeafe;
  border-color: #93c5fd;
  color: #1e40af;
}

.icon-svg {
  width: 14px;
  height: 14px;
  stroke: currentColor;
}

.label-tip {
  font-size: 11px;
  color: #94a3b8;
  font-weight: 400;
  margin-left: 6px;
}

/* 连接与认证状态组（独立右上角） */
.connection-group {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.divider-v {
  width: 1px;
  height: 18px;
  background: #e2e8f0;
  margin: 0 4px;
}

.btn-login {
  background: #ffffff;
  border-color: #cbd5e1;
  color: #334155;
  font-weight: 500;
}
.btn-login:hover:not(:disabled) {
  border-color: #2563eb;
  color: #2563eb;
  background: #eff6ff;
}

/* 右侧操作按钮组 */
.actions-group {
  display: flex;
  align-items: center;
  gap: 6px;
}

.btn-action {
  background: #ffffff;
  border-color: #cbd5e1;
  color: #334155;
}
.btn-action:hover:not(:disabled) {
  border-color: #94a3b8;
  background: #f8fafc;
  color: #0f172a;
}
.btn-action.is-active {
  background: #eff6ff;
  border-color: #2563eb;
  color: #2563eb;
  font-weight: 600;
}

/* 反馈横条 */
.alert-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
  padding: 6px 10px;
  border-radius: 6px;
  font-size: 12px;
}
.alert-bar.is-error {
  background: #fef2f2;
  border: 1px solid #fecaca;
  color: #b91c1c;
}
.alert-bar.is-notice {
  background: #f0fdf4;
  border: 1px solid #bbf7d0;
  color: #15803d;
}

/* 账户对比抽屉面板 */
.account-comparison-drawer {
  margin-top: 8px;
  padding: 12px 14px;
  background: #f8fafc;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
}

.drawer-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 10px;
}

.drawer-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 700;
  color: #0f172a;
}

.drawer-controls {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.date-label {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: #475569;
}

.input-date {
  border: 1px solid #cbd5e1;
  border-radius: 4px;
  padding: 2px 6px;
  font-size: 11px;
  background: #ffffff;
  color: #0f172a;
}

.table-scroll {
  overflow-x: auto;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  background: #ffffff;
}

.comparison-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  white-space: nowrap;
}

.comparison-table th {
  background: #f1f5f9;
  color: #475569;
  font-weight: 600;
  text-align: left;
  padding: 8px 10px;
  border-bottom: 1px solid #e2e8f0;
}

.comparison-table td {
  padding: 7px 10px;
  border-bottom: 1px solid #f1f5f9;
  color: #334155;
}

.comparison-table tr:hover td {
  background: #f8fafc;
}
.comparison-table tr.row-active td {
  background: #eff6ff;
}

.inst-cell {
  display: flex;
  flex-direction: column;
}
.inst-name-text {
  font-weight: 600;
  color: #0f172a;
}
.inst-sub-text {
  font-size: 11px;
  color: #64748b;
}

.equity-chart {
  display: block;
}

.text-up {
  color: #dc2626;
  font-weight: 700;
}
.text-down {
  color: #16a34a;
  font-weight: 700;
}
.text-muted {
  color: #94a3b8;
}

.quality-badge {
  display: inline-block;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 500;
}
.quality-badge.is-good {
  background: #dcfce7;
  color: #166534;
}
.quality-badge.is-warn {
  background: #fef3c7;
  color: #92400e;
}

.btn-popout-table {
  background: #eff6ff;
  border: 1px solid #bfdbfe;
  color: #1d4ed8;
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 4px;
  cursor: pointer;
}
.btn-popout-table:hover {
  background: #dbeafe;
}

.drawer-pairs {
  margin-top: 10px;
  padding: 8px 10px;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
}
.pair-hint {
  font-size: 12px;
  color: #334155;
  margin: 2px 0;
}
.drawer-footer-note {
  margin: 6px 0 0 0;
  font-size: 11px;
  color: #64748b;
  line-height: 1.5;
}

/* 现代化弹窗 (Modal) */
.modal-backdrop {
  position: fixed;
  inset: 0;
  z-index: 2100;
  background: rgba(15, 23, 42, 0.45);
  backdrop-filter: blur(4px);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
}

.instance-dialog {
  background: #ffffff;
  border-radius: 12px;
  box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 8px 10px -6px rgba(0, 0, 0, 0.1);
  width: 680px;
  max-width: 100%;
  max-height: 85vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid #cbd5e1;
}

.dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 20px;
  border-bottom: 1px solid #e2e8f0;
  background: #f8fafc;
}
.dialog-header h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 700;
  color: #0f172a;
}
.btn-close {
  background: none;
  border: none;
  font-size: 16px;
  color: #64748b;
  cursor: pointer;
  padding: 4px;
  border-radius: 4px;
}
.btn-close:hover {
  background: #e2e8f0;
  color: #0f172a;
}

.dialog-body {
  padding: 18px 20px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.dialog-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 12px 20px;
  border-top: 1px solid #e2e8f0;
  background: #f8fafc;
}

.form-row {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.form-label {
  font-size: 12px;
  font-weight: 600;
  color: #334155;
}
.form-input, .form-select, .form-textarea {
  background: #ffffff;
  border: 1px solid #cbd5e1;
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 13px;
  color: #0f172a;
  outline: none;
  transition: all 0.15s ease;
  width: 100%;
  box-sizing: border-box;
}
.form-input:focus, .form-select:focus, .form-textarea:focus {
  border-color: #2563eb;
  box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.15);
}

.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px 14px;
}
.full-width {
  grid-column: 1 / -1;
}

.field-hint {
  font-size: 11px;
  color: #64748b;
  line-height: 1.5;
  margin: 2px 0 6px 0;
}

.contract-fieldset {
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 10px 14px;
  margin-top: 8px;
}
.contract-fieldset legend {
  font-size: 12px;
  font-weight: 600;
  color: #334155;
  padding: 0 6px;
}
.contract-fields {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px 10px;
}

.preset-load-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  background: #f8fafc;
  border-radius: 6px;
  border: 1px solid #e2e8f0;
}
.preset-buttons {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.dialog-desc {
  font-size: 13px;
  color: #475569;
  line-height: 1.6;
  background: #eff6ff;
  border: 1px solid #bfdbfe;
  padding: 8px 12px;
  border-radius: 6px;
}

.mt-2 {
  margin-top: 8px;
}

@media (max-width: 640px) {
  .contract-fields {
    grid-template-columns: 1fr;
  }
  .form-grid {
    grid-template-columns: 1fr;
  }
}
</style>
