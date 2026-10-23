<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api } from '../services/api'
import { useLiveTraderStore } from '../stores/liveTrader'
import type { CtpConnectionSettings, ExecutionConfig, StrategyDefinition } from '../types'
import SimnowLoginFields from './SimnowLoginFields.vue'
import { emptySimnowLogin, ctpConnectionLabel } from '../utils/simnow'

const trader = useLiveTraderStore()
const selected = computed(() => trader.instanceCatalog?.instances.find(r => r.instance.id === trader.selectedInstanceId))
const dialog = ref<'create' | 'configure' | 'login' | null>(null)
const busy = ref(false)
const error = ref('')
const name = ref('')
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
const isCandidate = computed(() => strategy.value?.strategy_id === 'n_p2' || strategy.value?.strategy_id === 'n_p3')
const compareOpen = ref(false)
const compareBusy = ref(false)
const compare = ref<any>(null)
const localDate = (timestamp: number) => {
  const d = new Date(timestamp); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16)
}
const from = ref(localDate(Date.now() - 30 * 86400000))
const to = ref(localDate(Date.now()))
const number = (v: unknown, digits = 2) => typeof v === 'number' && Number.isFinite(v) ? v.toFixed(digits) : '—'
const moduleLabel = (id: string) => ({ n_pattern: 'N 字形', n_exit_v1: '现有 N 字形退出', n_score_v1: '现有评分分档', fixed_v1: '计划手数', protect_only_v1: '保护价退出', n_p2: 'P2 · ATR / 原 S1', n_p3: 'P3 · ATR / 2R 上限', p2_fixed_exit_v1: 'P2 固定止损与原 S1', p3_fixed_exit_v1: 'P3 固定止损与 2R 上限', candidate_one_lot_v1: 'P2/P3 固定一手' }[id] || id)

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
  error.value = ''; dialog.value = 'configure'
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
    const next = { ...strategy.value, parameters: JSON.parse(parameters.value) }
    // One operation commits module selection and execution parameters together.
    await trader.instanceAction({ action: 'configure_strategy', strategy: next, config: config.value })
    dialog.value = null
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
</script>

<template>
  <section class="instances-panel" aria-label="策略实例与账户">
    <div v-if="!trader.instancesSupported || trader.instancesError" class="instance-status" aria-live="polite">
      <strong>策略实例与账户</strong>
      <span :role="trader.instancesError ? 'alert' : 'status'">{{ trader.instancesLoading ? '正在检查多账户服务…' : trader.instancesError || '正在检查多账户服务…' }}</span>
      <button :disabled="trader.instancesLoading" @click="trader.loadInstances()">{{ trader.instancesLoading ? '检查中…' : '重新检查' }}</button>
    </div>
    <template v-if="trader.instancesSupported">
    <div class="instance-controls">
      <label>策略实例
        <select :value="trader.selectedInstanceId" :disabled="busy" @change="trader.selectInstance(($event.target as HTMLSelectElement).value)">
          <option v-for="row in trader.instanceCatalog?.instances" :key="row.instance.id" :value="row.instance.id">
            {{ row.instance.name }} · {{ accountLabel(row.account) }} · {{ row.account.name }}
          </option>
        </select>
      </label>
      <span class="kind" :class="selected?.account.kind">{{ accountLabel(selected?.account) }}</span>
      <span v-if="selected?.account.kind === 'ctp'" class="connection-state" :class="{ error: trader.health?.login_error || trader.health?.sync_error }" role="status">{{ ctpConnectionLabel(trader.health, trader.bridgeOnline) }}</span>
      <button v-if="selected?.account.kind === 'ctp' && (!selected.account.broker_id || selected.account.broker_id === '9999')" :disabled="busy" @click="openLogin">SimNow 登录 / 重连</button>
      <span>{{ selected?.paused ? '已暂停新入场' : '运行中' }}</span>
      <button :disabled="busy" @click="togglePause">{{ selected?.paused ? '启用实例' : '暂停新入场' }}</button>
      <button :disabled="busy" @click="openCreate(false)">新建</button>
      <button :disabled="busy" @click="openCreate(true)">复制到新账户</button>
      <button :disabled="busy" @click="openConfigure">配置</button>
      <button @click="showComparison">{{ compareOpen ? '收起账户对比' : '账户对比' }}</button>
    </div>
    <p class="hint">{{ moduleLabel(selected?.strategy.strategy_id || '') }} · 版本 {{ selected?.strategy.revision.slice(0, 8) }}。暂停后继续管理已有持仓；配置修改只用于新信号。</p>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="notice" role="status" class="hint">{{ notice }}</p>
    <div v-if="compareOpen" class="account-comparison">
      <div class="instance-controls">
        <label>开始 <input v-model="from" type="datetime-local"></label>
        <label>结束 <input v-model="to" type="datetime-local"></label>
        <button :disabled="compareBusy" @click="loadComparison">{{ compareBusy ? '加载中…' : '刷新对比' }}</button>
      </div>
      <div class="table-scroll"><table>
        <thead><tr><th>策略 / 账户</th><th>类型</th><th>净值走势</th><th>净值变化</th><th>收益率</th><th>最大回撤</th><th>完整交易</th><th>毛胜率</th><th>毛盈亏因子</th><th>最大保证金</th><th>观察费用</th><th>数据质量</th></tr></thead>
        <tbody><tr v-for="row in compare?.instances" :key="row.instance.id">
          <td>{{ row.instance.name }}<small>{{ row.account.name }}</small></td>
          <td>{{ accountLabel(row.account) }}</td>
          <td><svg v-if="row.sample_count > 1" viewBox="0 0 160 36" width="160" height="36" aria-label="账户净值走势"><polyline :points="curve(row.equity_curve)" fill="none" stroke="#2563eb" stroke-width="1.5" /></svg><span v-else>样本不足</span></td>
          <td>{{ number(row.net_profit) }}</td><td>{{ number(row.return_pct) }}%</td><td>{{ number(row.max_drawdown_pct) }}%</td>
          <td>{{ row.trades }}</td><td>{{ number(row.win_rate_pct) }}%</td><td>{{ number(row.profit_factor) }}</td><td>{{ number(row.max_margin) }}</td>
          <td>{{ number(row.observed_fees) }}</td><td>{{ row.quality_flags?.length ? row.quality_flags.map((q: string) => ({ quote_gap: '行情有缺口', equity_sample_gap: '净值缺样本', fees_unconfirmed: '费用待确认', insufficient_samples: '样本不足' }[q] || q)).join('、') : '完整' }}</td>
        </tr></tbody>
      </table></div>
      <p v-for="pair in compare?.signal_pairs" :key="`${pair.baseline_instance_id}:${pair.candidate_instance_id}`" class="hint">
        {{ trader.instanceCatalog?.instances.find(r => r.instance.id === pair.candidate_instance_id)?.instance.name }} 相对
        {{ trader.instanceCatalog?.instances.find(r => r.instance.id === pair.baseline_instance_id)?.instance.name }}：
        同源完整信号 {{ pair.paired_signals }} 笔，毛收益差额 {{ number(pair.gross_profit_difference) }} 元。
      </p>
      <p class="hint">{{ compare?.note }}各账户可能只有部分区间的数据；交易胜率与盈亏因子按毛收益计算，未平仓交易不计入。净值样本每分钟保存一次。</p>
    </div>
    <div v-if="dialog" class="modal-backdrop" @click.self="!busy && (dialog = null)">
      <form class="instance-dialog" @submit.prevent="dialog === 'create' ? create() : dialog === 'login' ? login() : save()">
        <h3>{{ dialog === 'create' ? '创建策略实例' : dialog === 'login' ? 'SimNow 账户登录' : '配置新版本' }}</h3>
        <template v-if="dialog === 'create'">
          <label>实例名称 <input v-model="name" required maxlength="160"></label>
          <p v-if="copyFrom" class="hint">复制所选策略的模块及参数；条件单、持仓、历史与资金均独立。</p>
          <label>策略方案 <select v-model="createPreset"><option value="">{{ copyFrom ? '沿用所选策略配置' : '现有 N 字形策略' }}</option><option v-for="p in presets" :key="p.definition.strategy_id" :value="p.definition.strategy_id">{{ p.label }}</option></select></label>
          <p v-if="createPreset" class="hint">P2/P3 使用固定一手、10 tick 最小风险和半自动模式。创建后请在配置中核对冻结费用与交易日历；参考日历仅覆盖至 2026-10-31，覆盖外拒绝新入场。</p>
          <label>账户类型 <select v-model="kind"><option value="paper">本地模拟账户</option><option value="simnow">SimNow 仿真账户（CTP）</option><option value="ctp">其他 CTP 账户（服务端凭据）</option></select></label>
          <SimnowLoginFields v-if="kind === 'simnow'" v-model="simnow" />
          <template v-if="kind === 'ctp'">
            <label>券商编号 <input v-model="broker" required></label><label>用户编号 <input v-model="user" required></label>
            <label>独立桥接端口 <input v-model.number="port" type="number" min="1" max="65535" required></label>
            <label>服务端凭据名称 <input v-model="credential" pattern="[A-Za-z0-9_-]+" required></label>
            <p class="hint">由服务端 ctp_credentials 目录中同名 JSON 文件提供凭据。页面只保存引用；账户身份必须与文件一致。</p>
          </template>
          <template v-else-if="kind === 'paper'">
            <label>初始资金 <input v-model.number="balance" type="number" min="1" step="any" required></label>
            <p class="hint">按实际合约填写参数。手续费单位为元/手，填写 0 表示明确采用零手续费假设；未配置的合约不能开仓。</p>
            <fieldset v-for="(row, index) in contracts" :key="index">
              <legend>合约 {{ index + 1 }}</legend>
              <div class="contract-fields">
                <label>实际合约 <input v-model="row.symbol" placeholder="例如 rb2701" required></label>
                <label>合约乘数 <input v-model="row.multiplier" type="number" min="0.000001" step="any" required></label>
                <label>保证金 % <input v-model="row.margin" type="number" min="0.000001" max="100" step="any" required></label>
                <label>开仓费 <input v-model="row.open" type="number" min="0" step="any" required></label>
                <label>平昨费 <input v-model="row.close" type="number" min="0" step="any" required></label>
                <label>平今费 <input v-model="row.today" type="number" min="0" step="any" required></label>
              </div>
              <button v-if="contracts.length > 1" type="button" @click="contracts.splice(index, 1)">移除此合约</button>
            </fieldset>
            <button type="button" @click="contracts.push({ symbol: '', multiplier: '', margin: '', open: '', close: '', today: '' })">添加合约</button>
          </template>
          <p class="hint">创建后保持暂停，核对配置后再启用。新实例只接收创建后的新信号。</p>
        </template>
        <template v-else-if="dialog === 'login'">
          <p class="hint">{{ selected?.instance.name }} · {{ selected?.account.name }}。重新登录会重建当前账户的连接并重新核对资金、持仓和委托。</p>
          <SimnowLoginFields v-model="simnow" :saved="savedCredentials" :lock-user="!!selected?.account.user_id" />
        </template>
        <template v-else-if="config && strategy">
          <div class="instance-controls"><button v-for="p in presets" :key="p.definition.strategy_id" type="button" @click="applyPreset(p.definition.strategy_id)">载入 {{ p.label }}</button></div>
          <label>信号模块 <select v-model="strategy.strategy_id" @change="applyPreset(strategy.strategy_id)"><option v-for="s in trader.instanceCatalog?.modules.strategies" :key="s.id" :value="s.id">{{ moduleLabel(s.id) }}</option></select></label>
          <label>代码版本 <select v-model="strategy.code_version"><option v-for="s in trader.instanceCatalog?.modules.strategies.filter(s => s.id === strategy?.strategy_id)" :key="s.version" :value="s.version">{{ s.version }}</option></select></label>
          <label>退出模块 <select v-model="strategy.exit_policy"><option v-for="id in trader.instanceCatalog?.modules.exit_policies" :key="id" :value="id">{{ moduleLabel(id) }}</option></select></label>
          <label>仓位模块 <select v-model="strategy.sizing_policy"><option v-for="id in trader.instanceCatalog?.modules.sizing_policies" :key="id" :value="id">{{ moduleLabel(id) }}</option></select></label>
          <label>大仓手数 <input v-model.number="config.default_large_volume" type="number" min="1" required></label>
          <label>小仓手数 <input v-model.number="config.default_small_volume" type="number" min="1" required></label>
          <label>最小入场风险 tick <input v-model.number="config.min_entry_risk_ticks" type="number" min="0" required></label>
          <label>报单偏移 tick <input v-model.number="config.slippage_ticks" type="number" min="0" max="100" required></label>
          <label>策略模块参数 <textarea v-model="parameters" rows="4" spellcheck="false" /></label>
          <p v-if="isCandidate" class="hint">P2/P3 固定一手、原结构止损；P3 按实际成交价锁定目标。参数中的 fees 为筛选费用快照，需核对实际合约与账户费用；本地模拟账本手续费另按账户合约配置。参考日历仅覆盖至 2026-10-31，覆盖外拒绝新入场。</p>
          <p class="hint">已有条件单、持仓和委托保留原版本；这里的更改只用于新信号。</p>
        </template>
        <p v-if="error" role="alert" class="error">{{ error }}</p>
        <div class="dialog-actions"><button type="button" :disabled="busy" @click="dialog = null">取消</button><button type="submit" :disabled="busy">{{ busy ? '处理中…' : dialog === 'login' ? '登录 / 重新连接' : kind === 'simnow' && dialog === 'create' ? '创建并登录' : '保存' }}</button></div>
      </form>
    </div>
    </template>
  </section>
</template>

<style scoped>
.instances-panel { padding: 14px 18px; background: #fff; color: #1e293b; border: 1px solid #dbe4ef; border-radius: 12px; margin-bottom: 14px; }
.instance-status { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; color: #475569; font-size: 13px; }
.instance-status strong { color: #1e293b; }
.instance-controls { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
label { display: flex; align-items: center; gap: 8px; font-size: 13px; }
input, select, textarea { color: #1e293b; background: #fff; border: 1px solid #cbd5e1; border-radius: 6px; padding: 7px; min-width: 0; }
button { border: 1px solid #cbd5e1; background: #f8fafc; color: #1e293b; border-radius: 6px; padding: 7px 10px; cursor: pointer; }
button:hover { border-color: #2563eb; } button:disabled { opacity: .5; cursor: default; }
.kind { border-radius: 6px; padding: 5px 8px; font-size: 12px; }.paper { background: #dbeafe; color: #1d4ed8; }.ctp { background: #fef3c7; color: #92400e; }
.hint { font-size: 12px; color: #64748b; line-height: 1.6; margin: 10px 0 0; }.error { color: #b91c1c; overflow-wrap: anywhere; }
.account-comparison { border-top: 1px solid #e2e8f0; padding-top: 14px; margin-top: 14px; }.table-scroll { overflow-x: auto; } table { border-collapse: collapse; width: 100%; font-size: 12px; margin-top: 12px; white-space: nowrap; }td, th { text-align: left; border-bottom: 1px solid #e2e8f0; padding: 9px; }small { display: block; color: #64748b; }
.modal-backdrop { position: fixed; inset: 0; z-index: 2000; background: #0f172a77; display: flex; align-items: center; justify-content: center; padding: 20px; }
.instance-dialog { background: white; border-radius: 14px; padding: 24px; width: 660px; max-width: 100%; max-height: 85vh; overflow-y: auto; display: flex; flex-direction: column; gap: 14px; }.instance-dialog label { justify-content: space-between; }.instance-dialog input, .instance-dialog select, .instance-dialog textarea { width: 65%; }.instance-dialog fieldset { border: 1px solid #e2e8f0; border-radius: 8px; }.contract-fields { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }.dialog-actions { display: flex; justify-content: flex-end; gap: 10px; }
@media (max-width: 640px) { .contract-fields { grid-template-columns: 1fr; } .instance-controls label { width: 100%; } .instance-controls select { flex: 1; } }
</style>
