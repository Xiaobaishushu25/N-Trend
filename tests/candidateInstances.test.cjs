const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')

function setup({ p3V4 = false } = {}) {
  const preset = { label: 'P2', definition: { strategy_id: 'n_p2', code_version: '1', revision: '1', exit_policy: 'p2_fixed_exit_v1', sizing_policy: 'candidate_one_lot_v1', parameters: { fees: { id: 'frozen' }, calendar: { id: 'frozen-calendar' } } } }
  const p2v2Preset = { label: 'P2 V2', definition: { strategy_id: 'n_p2', code_version: '2', revision: '1', exit_policy: 'p2_fixed_exit_v1', sizing_policy: 'candidate_one_lot_v1', parameters: { fees: { id: 'frozen' }, calendar: { id: 'frozen-calendar' } } } }
  const p3Preset = { label: p3V4 ? 'P3 V4' : 'P3', definition: { strategy_id: 'n_p3_v1', code_version: p3V4 ? '4' : '3', revision: '1', exit_policy: 'p3_exit_v1', sizing_policy: 'risk_budget_v1', parameters: { fees: { id: 'p3-fees' }, calendar: { id: 'p3-calendar' }, risk_budget_yuan: 600, max_lots: p3V4 ? 1 : 2, exit_mode: 'fixed_s1', min_net_rr: 1, entry_cutoffs: {} } } }
  const calls = []
  const trader = { instanceCatalog: { instances: [], modules: { presets: [preset, p2v2Preset, p3Preset] } }, selectedInstanceId: 'default',
    async loadInstances() {}, async selectInstance(id) { this.selectedInstanceId = id } }
  const context = { exports: {}, Date, console, crypto: { randomUUID: () => 'fixture' }, defineEmits: () => () => {},
    require(name) {
      if (name === 'vue') return { ref: value => ({ value }), computed: getter => ({ get value() { return typeof getter === 'function' ? getter() : getter.get() }, set value(v) { getter.set(v) } }), watch() {}, onMounted() {}, onBeforeUnmount() {} }
      if (name === '../stores/liveTrader') return { useLiveTraderStore: () => trader }
      if (name === '../services/api') return { api: { async traderInstanceRequest(...args) { calls.push(args); return { instance_id: 'created' } } } }
      if (name === './SimnowLoginFields.vue') return {}
      if (name === '../utils/simnow') return { emptySimnowLogin: () => ({}), ctpConnectionLabel: () => '' }
      if (name === '../utils/contextMenu') return { showContextMenu: () => {} }
      if (name === 'naive-ui') return { useMessage: () => ({ error: () => {}, success: () => {}, warning: () => {} }) }
      throw Error(name)
    } }
  const source = fs.readFileSync('src/components/TraderInstancesPanel.vue', 'utf8').match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
  const compiled = ts.transpileModule(source + '\nexport { applyPreset, strategy, parameters, config, createPreset, contracts, create, kind, error, dialogError, formatErrorMessage, p3ExitMode, p3Budget, p3MaxLots, p3MinNetRr, lockP3V4Parameters };', { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } })
  vm.runInNewContext(compiled.outputText, context)
  return { ui: context.exports, trader, preset, p3Preset, calls }
}

test('P3 mode and budget controls edit frozen parameters without mutating the preset', () => {
  const { ui, p3Preset } = setup()
  ui.config.value = { mode: 'MonitorOnly', order_wait_seconds: 9 }
  ui.applyPreset('n_p3_v1')
  assert.equal(ui.strategy.value.exit_policy, 'p3_exit_v1')
  assert.equal(ui.strategy.value.sizing_policy, 'risk_budget_v1')
  assert.equal(ui.config.value.order_wait_seconds, 3)
  assert.equal(ui.config.value.mode, 'MonitorOnly')
  ui.p3ExitMode.value = 'hybrid_s1'; ui.p3Budget.value = 450; ui.p3MaxLots.value = 1; ui.p3MinNetRr.value = 0.8
  const params = JSON.parse(ui.parameters.value)
  assert.equal(params.exit_mode, 'hybrid_s1'); assert.equal(params.risk_budget_yuan, 450); assert.equal(params.max_lots, 1)
  assert.equal(params.min_net_rr, 0.8)
  assert.equal(p3Preset.definition.parameters.exit_mode, 'fixed_s1')
  assert.equal(p3Preset.definition.parameters.risk_budget_yuan, 600)
  assert.equal(p3Preset.definition.parameters.min_net_rr, 1)
})

test('P3 V4 creation uses the single-lot fixed-exit preset', async () => {
  const { ui, calls } = setup({ p3V4: true })
  ui.createPreset.value = 'n_p3_v1'
  ui.contracts.value = [{ symbol: 'pb2701', multiplier: '5', margin: '10', open: '3', close: '3', today: '3' }]
  await ui.create()
  assert.equal(calls.length, 1)
  const payload = calls[0][2]
  assert.equal(payload.strategy.strategy_id, 'n_p3_v1')
  assert.equal(payload.strategy.code_version, '4')
  assert.equal(payload.strategy.parameters.exit_mode, 'fixed_s1')
  assert.equal(payload.strategy.parameters.max_lots, 1)
  assert.equal(payload.strategy.parameters.risk_budget_yuan, 600)
  assert.equal(payload.strategy.parameters.min_net_rr, 1)
  assert.equal(payload.strategy.sizing_policy, 'risk_budget_v1')
  assert.equal(payload.config.order_wait_seconds, 3)
  assert.equal(payload.config.mode, 'SemiAuto')
})

test('switching a P3 instance to V4 locks the exit and lots while preserving its admission parameters', () => {
  const { ui } = setup()
  ui.config.value = { mode: 'MonitorOnly' }
  ui.applyPreset('n_p3_v1')
  ui.p3ExitMode.value = 'hybrid_s1'
  ui.p3MinNetRr.value = 0.1
  ui.p3Budget.value = 450
  ui.strategy.value.code_version = '4'
  ui.lockP3V4Parameters()
  const parameters = JSON.parse(ui.parameters.value)
  assert.equal(parameters.exit_mode, 'fixed_s1')
  assert.equal(parameters.max_lots, 1)
  assert.equal(parameters.min_net_rr, 0.1)
  assert.equal(parameters.risk_budget_yuan, 450)
})

test('P2 preset pairs its modules, preserves mode, and copies frozen parameters', () => {
  const { ui, preset } = setup()
  ui.config.value = { mode: 'MonitorOnly', slippage_ticks: 2, min_entry_risk_ticks: 0, default_large_volume: 8, default_small_volume: 4 }
  ui.applyPreset('n_p2')
  assert.equal(ui.strategy.value.exit_policy, 'p2_fixed_exit_v1')
  assert.equal(ui.strategy.value.sizing_policy, 'candidate_one_lot_v1')
  assert.equal(ui.config.value.mode, 'MonitorOnly')
  assert.equal(ui.config.value.min_entry_risk_ticks, 10)
  assert.equal(ui.config.value.default_large_volume, 1)
  ui.strategy.value.parameters.fees.id = 'edited'
  assert.equal(preset.definition.parameters.fees.id, 'frozen')
  ui.applyPreset('n_pattern')
  assert.equal(ui.strategy.value.exit_policy, 'n_exit_v1')
  assert.equal(ui.strategy.value.sizing_policy, 'n_score_v1')
})

test('creation sends a paired P2 definition and a conservative execution configuration', async () => {
  const { ui, calls, trader } = setup()
  ui.createPreset.value = 'n_p2'
  ui.contracts.value = [{ symbol: 'pb2611', multiplier: '5', margin: '10', open: '3', close: '3', today: '3' }]
  await ui.create()
  assert.equal(ui.error.value, '')
  assert.equal(calls.length, 1)
  const payload = calls[0][2]
  assert.equal(payload.strategy.strategy_id, 'n_p2')
  assert.equal(payload.config.mode, 'SemiAuto')
  assert.equal(payload.config.min_entry_risk_ticks, 10)
  assert.equal(payload.config.slippage_ticks, 0)
  assert.equal(payload.account.config.contracts.PB2611.open_fee_per_lot, 3)
  assert.equal(trader.selectedInstanceId, 'created')
})

test('creation with P2 V2 preset sets code_version 2 while P2 v1 remains code_version 1', async () => {
  const { ui, calls } = setup()
  ui.createPreset.value = 'n_p2_v2'
  ui.contracts.value = [{ symbol: 'pb2611', multiplier: '5', margin: '10', open: '3', close: '3', today: '3' }]
  await ui.create()
  assert.equal(calls.length, 1)
  const payload = calls[0][2]
  assert.equal(payload.strategy.strategy_id, 'n_p2')
  assert.equal(payload.strategy.code_version, '2')
  assert.equal(payload.strategy.exit_policy, 'p2_fixed_exit_v1')
  assert.equal(payload.strategy.sizing_policy, 'candidate_one_lot_v1')
})

test('creation failure scopes error to dialogError and provides friendly 422 explanation without polluting main panel error', async () => {
  const { ui } = setup()
  ui.kind.value = 'paper'
  ui.contracts.value = [{ symbol: '', multiplier: '', margin: '', open: '', close: '', today: '' }]
  await ui.create()
  assert.equal(ui.error.value, '', 'main panel error should remain empty')
  assert.match(ui.dialogError.value, /请完整填写每个合约/)

  // Verify 422 friendly formatting
  const formatted422 = ui.formatErrorMessage('服务端返回错误状态码: 422 Unprocessable Entity')
  assert.match(formatted422, /422 Unprocessable Entity/)
  assert.match(formatted422, /BrokerID/)
})
