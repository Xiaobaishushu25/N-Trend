const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')

// Exercise the actual store with mocked native IPC; no Tauri or trading connection.
function setup(listenOverride) {
  const events = new Map()
  const api = { getTraderSnapshot: async () => snapshot() }
  const context = {
    exports: {}, Date, console,
    localStorage: { getItem: () => null },
    setInterval: () => 1, clearInterval: () => {},
    require(name) {
      if (name === 'pinia') return { defineStore: (_, options) => () => {
        const store = options.state()
        for (const [key, action] of Object.entries(options.actions)) store[key] = action.bind(store)
        for (const [key, getter] of Object.entries(options.getters)) {
          Object.defineProperty(store, key, { get: () => getter.call(store, store) })
        }
        return store
      } }
      if (name === '@tauri-apps/api/event') return { listen: listenOverride || (async (name, handler) => {
        events.set(name, handler)
        return () => events.delete(name)
      }) }
      if (name === '../services/api') return { api, onQuotesUpdated: async () => () => {} }
      if (name === '../utils/notify') return { notify: { success() {}, error() {} } }
      if (name === '../utils/multipliers') return {
        DEFAULT_MULTIPLIERS: { PB: 5 }, getSymbolPrefix: (s) => s.match(/^[a-z]+/i)?.[0].toUpperCase(),
      }
      throw Error(`Unexpected module ${name}`)
    },
  }
  const source = fs.readFileSync('src/stores/liveTrader.ts', 'utf8')
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } })
  vm.runInNewContext(compiled.outputText, context)
  return { store: context.exports.useLiveTraderStore(), events, api }
}

function snapshot() {
  return { source: 'server', bridge_online: true, health: { ready: true, healthy: true },
    account: { balance: 1000 }, broker_positions: [] }
}

test('changing the live comparison lane preserves all other execution settings', async () => {
  const { store, api } = setup()
  store.config = { mode: 'FullAuto', max_risk_percent: 0.5, default_large_volume: 2,
    default_small_volume: 1, daily_loss_limit_r: 3, min_entry_risk_ticks: 0, slippage_ticks: 2 }
  let sent
  api.updateTraderConfig = async (config) => { sent = config }
  await store.setEntryRiskFilter(10)
  assert.equal(sent.mode, 'FullAuto')
  assert.equal(sent.max_risk_percent, 0.5)
  assert.equal(sent.default_large_volume, 2)
  assert.equal(store.config.min_entry_risk_ticks, 10)
  api.updateTraderConfig = async () => { throw new Error('offline') }
  await store.setEntryRiskFilter(0)
  assert.equal(store.config.min_entry_risk_ticks, 10)
  assert.equal(store.updatingEntryFilter, false)
})

test('switching to local account data clears the server-only strategy comparison', () => {
  const { store } = setup()
  store.applySnapshot({ ...snapshot(), strategy_comparison: { runs: [] } })
  assert.ok(store.strategyComparison)
  store.applySnapshot({ ...snapshot(), source: 'local' })
  assert.equal(store.strategyComparison, null)
})

test('push replaces snapshot without HTTP reads and listeners are removed on close', async () => {
  const { store, events, api } = setup()
  let reads = 0
  api.getTraderSnapshot = async () => { reads++; return snapshot() }
  await store.startRealtime()
  events.get('trader-updated')({ payload: snapshot() })
  assert.equal(store.account.balance, 1000)
  assert.equal(reads, 0)
  assert.ok(store.lastPushAt)
  store.stopRealtime()
  assert.equal(events.size, 0)
  assert.equal(store.pollTimer, null)
})

test('a late HTTP response cannot overwrite a newer pushed snapshot', async () => {
  const { store, api, events } = setup()
  let resolve
  api.getTraderSnapshot = () => new Promise((r) => { resolve = r })
  await store.startRealtime()
  const read = store.refresh()
  const current = snapshot()
  current.account.balance = 2000
  events.get('trader-updated')({ payload: current })
  resolve(snapshot())
  await read
  assert.equal(store.account.balance, 2000)
})

test('a failed snapshot read retains account values but removes ready state', async () => {
  const { store, api } = setup()
  store.applySnapshot(snapshot())
  api.getTraderSnapshot = async () => { throw new Error('offline') }
  await store.refresh()
  assert.equal(store.account.balance, 1000)
  assert.equal(store.isReady, false)
  assert.equal(store.bridgeOnline, false)
})

test('floating profit requires exact, fresh contract quotes and valid synchronized positions', () => {
  const { store } = setup()
  store.applySnapshot({ ...snapshot(), broker_positions: [{ instrument_id: 'pb2611', direction: 'buy',
    position: 2, today_position: 2, yd_position: 0, open_cost: 1000, position_cost: 1000,
    position_profit: 5, margin: 10 }] })
  store.quotes.pb0 = { price: 110, receivedAt: store.now }
  assert.equal(store.combinedPositions[0].profit_is_estimated, false)
  store.quotes.pb2611 = { price: 110, receivedAt: store.now }
  assert.equal(store.combinedPositions[0].position_profit, 100)
  assert.equal(store.combinedPositions[0].profit_is_estimated, true)
  store.brokerPositions[0].direction = 'sell'
  assert.equal(store.combinedPositions[0].position_profit, -100)
  store.health.sync_pending = true
  assert.equal(store.combinedPositions[0].position_profit, 5)
  store.health.sync_pending = false
  store.now += 16000
  assert.equal(store.combinedPositions[0].profit_is_estimated, false)
})

test('closing during listener registration cleans up all late listeners', async () => {
  let release
  let removed = 0
  let count = 0
  const { store } = setup(async () => {
    if (++count === 1) await new Promise((r) => { release = r })
    return () => removed++
  })
  const opening = store.startRealtime()
  store.stopRealtime()
  release()
  await opening
  assert.equal(store.realtimeListeners.length, 0)
  assert.equal(store.pollTimer, null)
  assert.equal(removed, 5)
})

test('history is fetched only on demand and legacy pushes cannot replace the selected page', async () => {
  const { store, api, events } = setup()
  let calls = 0
  api.getExecutionOrders = async (page, pageSize) => {
    calls++
    return { items: [{ note: `page ${page}` }], total: 4031, page, page_size: pageSize }
  }
  await store.startRealtime()
  await store.loadAll()
  assert.equal(calls, 0)
  await store.loadExecutionOrders(2)
  assert.equal(calls, 1)
  assert.equal(store.executionOrdersPage, 2)
  events.get('trader-updated')({ payload: { ...snapshot(), execution_orders: [{ note: 'entire archive' }] } })
  assert.equal(store.executionOrders[0].note, 'page 2')
  assert.equal(store.executionOrdersTotal, 4031)
})

test('late page responses and failures cannot overwrite a newer page', async () => {
  const { store, api } = setup()
  let resolveFirst
  api.getExecutionOrders = () => new Promise((resolve) => { resolveFirst = resolve })
  const first = store.loadExecutionOrders(1)
  api.getExecutionOrders = async () => ({ items: [{ note: 'new page' }], total: 30, page: 2, page_size: 20 })
  await store.loadExecutionOrders(2)
  resolveFirst({ items: [{ note: 'old page' }], total: 30, page: 1, page_size: 20 })
  await first
  assert.equal(store.executionOrders[0].note, 'new page')
  assert.equal(store.executionOrdersLoading, false)
  api.getExecutionOrders = async () => { throw new Error('offline') }
  await store.loadExecutionOrders(1)
  assert.equal(store.executionOrdersPage, 2)
  assert.equal(store.executionOrdersError, 'offline')
  assert.equal(store.executionOrders[0].note, 'new page')
})

test('closing or switching account source invalidates an outstanding history request', async () => {
  const { store, api } = setup()
  store.applySnapshot(snapshot())
  let resolve
  api.getExecutionOrders = () => new Promise((r) => { resolve = r })
  const pending = store.loadExecutionOrders(2)
  store.applySnapshot({ ...snapshot(), source: 'local' })
  resolve({ items: [{ note: 'server archive' }], total: 10, page: 2, page_size: 20 })
  await pending
  assert.equal(store.executionOrders.length, 0)
  assert.equal(store.executionOrdersPage, 1)
  assert.equal(store.executionOrdersLoading, false)
  const closed = store.loadExecutionOrders(1)
  store.stopRealtime()
  resolve({ items: [{ note: 'closed page' }], total: 10, page: 1, page_size: 20 })
  await closed
  assert.equal(store.executionOrders.length, 0)
})
