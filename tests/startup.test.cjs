const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')

function loadStore(path, modules) {
  const context = { exports: {}, console, Map, require(name) {
    if (name === 'pinia') return { defineStore: (_, options) => () => {
      const store = options.state()
      for (const [key, action] of Object.entries(options.actions)) store[key] = action.bind(store)
      return store
    } }
    if (Object.hasOwn(modules, name)) return modules[name]
    throw Error(`Unexpected module ${name}`)
  } }
  const source = ts.transpileModule(fs.readFileSync(path, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  })
  vm.runInNewContext(source.outputText, context)
  return context.exports
}
function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const flush = () => new Promise(setImmediate)

test('local settings apply while both server reads are pending and concurrent loads share requests', async () => {
  const config = deferred(), status = deferred()
  let configReads = 0, statusReads = 0, localReads = 0
  const { useSettingsStore } = loadStore('src/stores/settings.ts', { '../services/api': { api: {
    getConfig: () => { configReads++; return config.promise },
    schedulerStatus: () => { statusReads++; return status.promise },
    getClientSettings: async () => { localReads++; return { chartDisplayBars: 321 } },
  } } })
  const store = useSettingsStore()
  const first = store.load(), second = store.load()
  assert.equal(first, second)
  await flush()
  assert.equal(store.clientSettings.chartDisplayBars, 321)
  assert.equal(configReads, 1)
  assert.equal(statusReads, 1)
  assert.equal(localReads, 1)
  config.resolve({ ui: { last_group_id: 7 } })
  status.resolve({ running: true })
  await first
  assert.equal(store.settings.ui.last_group_id, 7)
  assert.equal(store.status.running, true)
  await store.load()
  assert.equal(configReads, 2, 'a completed load must allow a later refresh')
})

test('all live listeners register even if notification history never responds; late history retains live alerts', async () => {
  const history = deferred()
  const events = new Map()
  const register = (name) => async (cb) => { events.set(name, cb); return () => events.delete(name) }
  const settings = { clientSettings: {}, status: {} }
  const api = {
    appInfo: async () => ({ name: 'ntrend', version: 'test' }),
    getConnectionStatus: async () => ({ status: 'reconnecting', server_url: '', quote_delay_ms: null }),
    getNotificationHistory: () => history.promise,
  }
  const services = { api }
  for (const name of ['onDataUpdated', 'onScanCompleted', 'onEntryTrigger', 'onManualLevelAlert',
    'onConnectionStatusChanged', 'onServerStatus']) services[name] = register(name)
  const { useAppStore } = loadStore('src/stores/app.ts', {
    '../services/api': services,
    '@tauri-apps/api/event': { listen: async (name, cb) => register(name)(cb) },
    '@tauri-apps/plugin-notification': {},
    './settings': { useSettingsStore: () => settings },
    './symbols': { useSymbolsStore: () => ({ load: async () => {} }) },
    './groups': { useGroupsStore: () => ({ load: async () => {} }) },
    './actions': { useActionsStore: () => ({ reloadTick: 0 }) },
    '../utils/notify': { isMainWindow: () => true, notify: {} },
    '../utils/manualLevel': { isManualLevelEventAllowed: () => false },
  })
  const store = useAppStore()
  let ready = false
  const init = store.init().then(() => { ready = true })
  await flush()
  assert.equal(ready, true, 'init must not wait for the history request')
  assert.ok(events.has('onEntryTrigger'))
  assert.ok(events.has('onManualLevelAlert'))
  assert.ok(events.has('data-source-failover'))
  events.get('onManualLevelAlert')([{ level_id: 1, reason: 'live' }])
  history.resolve([{ manual_level: { level_id: 1, reason: 'old' } },
    { manual_level: { level_id: 2, reason: 'history' } }])
  await init
  await flush()
  assert.equal(store.manualLevelAlerts[0].reason, 'live')
  assert.equal(store.manualLevelAlerts.length, 2)
})
