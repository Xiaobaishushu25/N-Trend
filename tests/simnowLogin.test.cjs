const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')
const vue = require('vue')

function compile(source, modules) {
  const context = { exports: {}, console, crypto: require('node:crypto'), Date,
    require: name => { if (Object.hasOwn(modules, name)) return modules[name]; throw Error(name) } }
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText, context)
  return context.exports
}
const helpers = compile(fs.readFileSync('src/utils/simnow.ts', 'utf8'), {})
function panel() {
  const sent = []
  const row = { instance: { id: 'sim-a', name: 'A' }, account: { kind: 'ctp', broker_id: '9999', user_id: '123456', name: 'A' } }
  const trader = {
    selectedInstanceId: 'sim-a', instanceCatalog: { instances: [row] },
    loadInstances: async () => {}, selectInstance: async id => { trader.selectedInstanceId = id },
    loginSimnow: async (id, input) => sent.push({ operation: 'login', id, input }),
  }
  const api = { traderInstanceRequest: async (operation, id, payload) => {
    sent.push({ operation, id, payload })
    if (operation === 'connection') return { configured: true, user_id: '123456', td_front: 'tcp://localhost:10001', app_id: 'saved-app', password_saved: true, auth_code_saved: true }
    return { instance_id: 'new-sim' }
  } }
  let source = fs.readFileSync('src/components/TraderInstancesPanel.vue', 'utf8').match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
  source += '\nexport { openCreate, openLogin, create, login, kind, simnow, dialog, savedCredentials, name };'
  const form = compile(source, { vue, '../services/api': { api }, '../stores/liveTrader': { useLiveTraderStore: () => trader }, './SimnowLoginFields.vue': {}, '../utils/simnow': helpers })
  return { form, trader, sent }
}
test('SimNow creation sends credentials without manual file or port configuration', async () => {
  const { form, sent, trader } = panel()
  form.openCreate(false); form.kind.value = 'simnow'
  Object.assign(form.simnow.value, { user_id: '123456', password: 'fixture-password' })
  await form.create(); await vue.nextTick()
  assert.equal(sent[0].operation, 'create')
  assert.equal(sent[0].payload.simnow.user_id, '123456')
  assert.equal(sent[0].payload.simnow.password, 'fixture-password')
  assert.equal(sent[0].payload.account, undefined)
  assert.equal(trader.selectedInstanceId, 'new-sim')
  assert.equal(form.dialog.value, null)
  assert.equal(form.simnow.value.password, '')
})
test('existing login loads public settings, targets its instance and clears the password', async () => {
  const { form, sent } = panel()
  await form.openLogin()
  assert.equal(sent[0].operation, 'connection')
  assert.equal(form.simnow.value.password, '')
  assert.equal(form.simnow.value.auth_code, '')
  assert.equal(form.savedCredentials.value, true)
  form.simnow.value.password = 'replacement-fixture'
  await form.login(); await vue.nextTick()
  assert.equal(sent[1].operation, 'login')
  assert.equal(sent[1].id, 'sim-a')
  assert.equal(sent[1].input.password, 'replacement-fixture')
  assert.equal(sent[1].input.app_id, 'saved-app')
  assert.equal(sent.length, 2, 'login must not resume the strategy')
  assert.equal(form.simnow.value.password, '')
})
test('closing a login form clears unsent credentials', async () => {
  const { form } = panel()
  await form.openLogin()
  form.simnow.value.password = 'unsent-fixture'
  form.dialog.value = null
  await vue.nextTick()
  assert.equal(form.simnow.value.password, '')
})
test('status distinguishes counter login from settlement and reconciliation', () => {
  const label = helpers.ctpConnectionLabel
  assert.equal(label(null, false), '交易桥未连接')
  assert.equal(label({ connected: true }, true), '正在认证')
  assert.equal(label({ connected: true, authenticated: true }, true), '正在登录')
  const logged = { connected: true, authenticated: true, logged_in: true }
  assert.match(label(logged, true), /确认结算/)
  assert.match(label({ ...logged, settlement_confirmed: true }, true), /同步资金/)
  assert.match(label({ ...logged, settlement_confirmed: true, ready: true }, true), /已同步/)
  assert.equal(label({ login_error: '登录失败 (3)' }, true), '登录失败 (3)')
})
