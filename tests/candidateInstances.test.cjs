const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')

function setup() {
  const preset = { label: 'P3', definition: { strategy_id: 'n_p3', code_version: '1', revision: '1', exit_policy: 'p3_fixed_exit_v1', sizing_policy: 'candidate_one_lot_v1', parameters: { fees: { id: 'frozen' }, calendar: { id: 'frozen-calendar' } } } }
  const calls = []
  const trader = { instanceCatalog: { instances: [], modules: { presets: [preset] } }, selectedInstanceId: 'default',
    async loadInstances() {}, async selectInstance(id) { this.selectedInstanceId = id } }
  const context = { exports: {}, Date, console, crypto: { randomUUID: () => 'fixture' }, defineEmits: () => () => {},
    require(name) {
      if (name === 'vue') return { ref: value => ({ value }), computed: getter => ({ get value() { return getter() } }), watch() {} }
      if (name === '../stores/liveTrader') return { useLiveTraderStore: () => trader }
      if (name === '../services/api') return { api: { async traderInstanceRequest(...args) { calls.push(args); return { instance_id: 'created' } } } }
      if (name === './SimnowLoginFields.vue') return {}
      if (name === '../utils/simnow') return { emptySimnowLogin: () => ({}), ctpConnectionLabel: () => '' }
      if (name === '../utils/contextMenu') return { showContextMenu: () => {} }
      if (name === 'naive-ui') return { useMessage: () => ({ error: () => {}, success: () => {}, warning: () => {} }) }
      throw Error(name)
    } }
  const source = fs.readFileSync('src/components/TraderInstancesPanel.vue', 'utf8').match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
  const compiled = ts.transpileModule(source + '\nexport { applyPreset, strategy, parameters, config, createPreset, contracts, create, kind, error, dialogError, formatErrorMessage };', { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } })
  vm.runInNewContext(compiled.outputText, context)
  return { ui: context.exports, trader, preset, calls }
}

test('P3 preset pairs its modules, preserves mode, and copies frozen parameters', () => {
  const { ui, preset } = setup()
  ui.config.value = { mode: 'MonitorOnly', slippage_ticks: 2, min_entry_risk_ticks: 0, default_large_volume: 8, default_small_volume: 4 }
  ui.applyPreset('n_p3')
  assert.equal(ui.strategy.value.exit_policy, 'p3_fixed_exit_v1')
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

test('creation sends a paired P3 definition and a conservative execution configuration', async () => {
  const { ui, calls, trader } = setup()
  ui.createPreset.value = 'n_p3'
  ui.contracts.value = [{ symbol: 'pb2611', multiplier: '5', margin: '10', open: '3', close: '3', today: '3' }]
  await ui.create()
  assert.equal(ui.error.value, '')
  assert.equal(calls.length, 1)
  const payload = calls[0][2]
  assert.equal(payload.strategy.strategy_id, 'n_p3')
  assert.equal(payload.config.mode, 'SemiAuto')
  assert.equal(payload.config.min_entry_risk_ticks, 10)
  assert.equal(payload.config.slippage_ticks, 0)
  assert.equal(payload.account.config.contracts.PB2611.open_fee_per_lot, 3)
  assert.equal(trader.selectedInstanceId, 'created')
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
