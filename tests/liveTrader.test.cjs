const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')

// Exercise the actual store with mocked native IPC; no Tauri or trading connection.
function setup(listenOverride) {
  const poolModule = { exports: {} }
  vm.runInNewContext(ts.transpileModule(fs.readFileSync('src/utils/conditionPool.ts', 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText, poolModule)
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
      if (name === '../utils/tradingHours') return { isMarketTradingWindow: () => true }
      if (name === '../utils/conditionPool') return poolModule.exports
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

test('relogin invalidates a pending old snapshot and still refreshes the selected account', async () => {
  const { store, api } = setup()
  store.instancesSupported = true; store.selectedInstanceId = 'sim-a'
  let resolveOld, reads = 0
  const pending = new Promise(resolve => { resolveOld = resolve })
  const credentials = { user_id: '123456', password: 'fixture-password', td_front: 'tcp://localhost:12345', app_id: 'fixture', auth_code: 'fixture' }
  api.traderInstanceRequest = async (operation, id, payload) => {
    if (operation === 'snapshot') return ++reads === 1 ? pending : { ...snapshot(), instance_id: 'sim-a', health: { ready: false, connected: false }, account: null }
    if (operation === 'login') { assert.equal(id, 'sim-a'); assert.equal(payload.password, 'fixture-password'); return { accepted: true } }
    if (operation === 'list') return { instances: [], modules: {}, default_instance_id: 'default' }
    throw Error(operation)
  }
  const old = store.refresh()
  await store.loginSimnow('sim-a', credentials)
  assert.equal(reads, 2)
  assert.equal(store.refreshing, false)
  resolveOld({ ...snapshot(), instance_id: 'sim-a', account: { balance: 999 } })
  await old
  assert.equal(store.account, null)
  assert.equal(store.health.ready, false)
  assert.ok(!JSON.stringify(store).includes('fixture-password'))
})

test('a late login response for another instance leaves the active account intact', async () => {
  const { store, api } = setup()
  let resolveLogin
  api.traderInstanceRequest = async operation => {
    assert.equal(operation, 'login')
    return new Promise(resolve => { resolveLogin = resolve })
  }
  const request = store.loginSimnow('sim-a', { user_id: '123456', password: 'fixture-password' })
  store.selectedInstanceId = 'sim-b'; store.applySnapshot({ ...snapshot(), instance_id: 'sim-b', account: { balance: 222 } })
  resolveLogin({ accepted: true })
  await request
  assert.equal(store.account.balance, 222)
  assert.equal(store.health.ready, true)
})

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
  assert.equal(removed, count)
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

test('switching instance clears all account state and rejects the old in-flight response', async () => {
  const { store, api } = setup()
  store.instancesSupported = true
  store.applySnapshot({ ...snapshot(), instance_id: 'default', history: [{ id: 'old' }], conditions: [{ id: 'old' }] })
  let resolveOld
  api.traderInstanceRequest = async (operation, id) => id === 'default'
    ? new Promise(resolve => { resolveOld = resolve })
    : { ...snapshot(), instance_id: id, account: { balance: 2000 }, history: [], conditions: [] }
  const old = store.refresh()
  await store.selectInstance('paper-b')
  assert.equal(store.account.balance, 2000)
  assert.equal(store.history.length, 0)
  resolveOld({ ...snapshot(), instance_id: 'default', account: { balance: 9000 } })
  await old
  assert.equal(store.account.balance, 2000)
  store.applySnapshot({ ...snapshot(), instance_id: 'default', account: { balance: 8000 } })
  assert.equal(store.account.balance, 2000)
})

test('instance actions explicitly target the selected account and never use legacy writes', async () => {
  const { store, api } = setup()
  store.instancesSupported = true
  store.selectedInstanceId = 'paper-b'
  const writes = []
  api.traderInstanceRequest = async (operation, id, payload) => {
    if (operation === 'action') writes.push({ id, payload })
    if (operation === 'list') return { instances: [] }
    if (operation === 'snapshot') return { ...snapshot(), instance_id: id }
  }
  api.confirmConditionOrder = () => { throw Error('must not call default endpoint') }
  await store.confirmCondition('condition-b')
  assert.equal(writes[0].id, 'paper-b')
  assert.equal(writes[0].payload.action, 'confirm')
  assert.equal(writes[0].payload.condition_id, 'condition-b')
})

test('instance deltas require the selected stream and a contiguous revision', async () => {
  const { store, api } = setup()
  store.instancesSupported = true
  store.selectedInstanceId = 'paper-b'
  store.applySnapshot({ ...snapshot(), instance_id: 'paper-b', _stream_id: 'b', _revision: 2 })
  store.applyInstanceDelta({ instance_id: 'other', delta: { stream_id: 'other', base_revision: 2, revision: 3, changes: { account: { balance: 9999 } } } })
  assert.equal(store.account.balance, 1000)
  store.applyInstanceDelta({ instance_id: 'paper-b', delta: { stream_id: 'b', base_revision: 2, revision: 3, changes: { account: { balance: 1200 } } } })
  assert.equal(store.account.balance, 1200)
  let reads = 0
  api.traderInstanceRequest = async () => { reads++; return { ...snapshot(), instance_id: 'paper-b', account: { balance: 1300 }, _stream_id: 'b', _revision: 5 } }
  store.applyInstanceDelta({ instance_id: 'paper-b', delta: { stream_id: 'b', base_revision: 4, revision: 5, changes: { account: { balance: 7777 } } } })
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(reads, 1)
  assert.equal(store.account.balance, 1300)
  assert.equal(store.instanceRevision, 5)
})

test('an old server reports missing multi-account support and can be retried after deployment', async () => {
  const { store, api } = setup()
  api.traderInstanceRequest = async () => { throw '服务端返回错误状态码: 404 Not Found' }
  await store.loadInstances()
  assert.equal(store.instancesSupported, false)
  assert.equal(store.instancesLoading, false)
  assert.match(store.instancesError, /更新服务端/)
  api.traderInstanceRequest = async () => ({ instances: [], default_instance_id: 'default' })
  await store.loadInstances()
  assert.equal(store.instancesSupported, true)
  assert.equal(store.instancesError, '')
})

test('temporary catalog failure retains the selected account and its instance routing', async () => {
  const { store, api } = setup()
  store.instancesSupported = true
  store.selectedInstanceId = 'paper-b'
  store.instanceCatalog = { instances: [{ instance: { id: 'paper-b' } }] }
  api.traderInstanceRequest = async () => { throw Error('连接失败') }
  await store.loadInstances()
  assert.equal(store.instancesSupported, true)
  assert.equal(store.selectedInstanceId, 'paper-b')
  assert.equal(store.instanceCatalog.instances.length, 1)
  assert.match(store.instancesError, /连接失败/)
})

test('a missing native command identifies the client update instead of hiding the panel', async () => {
  const { store, api } = setup()
  api.traderInstanceRequest = async () => { throw 'Command trader_instance_request not found' }
  await store.loadInstances()
  assert.match(store.instancesError, /重新编译并启动客户端/)
  assert.equal(store.instancesLoading, false)
})

test('activeConditions and waitingConfirmConditions support both snake_case and PascalCase states', () => {
  const { store } = setup()
  store.conditions = [
    { id: '1', state: 'pending' },
    { id: '2', state: 'waiting_confirm' },
    { id: '3', state: 'triggered' },
    { id: '4', state: 'working' },
    { id: '5', state: 'part_filled' },
    { id: '6', state: 'closing' },
    { id: '7', state: 'rejected' },
    { id: '8', state: 'unknown' },
    { id: '9', state: 'closed' },
    { id: '10', state: 'expired' },
    { id: '11', state: 'canceled' },
    { id: '12', state: 'invalidated' },
    { id: '13', state: 'filled' },
    { id: '14', state: 'WaitingConfirm' },
    { id: '15', state: 'Pending' },
  ]

  assert.equal(store.activeConditions.length, 10)
  const activeIds = store.activeConditions.map(c => c.id)
  assert.deepEqual(activeIds, ['1', '2', '3', '4', '5', '6', '7', '8', '14', '15'])

  assert.equal(store.waitingConfirmConditions.length, 2)
  const waitingIds = store.waitingConfirmConditions.map(c => c.id)
  assert.deepEqual(waitingIds, ['2', '14'])
})

test('activeConditions sorts by trigger_ts/warning_bar_ts/created_at descending', () => {
  const { store } = setup()
  store.conditions = [
    { id: 'older', state: 'pending', warning_bar_ts: '2026-10-23 20:00:00' },
    { id: 'newest', state: 'pending', trigger_ts: '2026-10-23 21:45:00' },
    { id: 'middle', state: 'pending', warning_bar_ts: '2026-10-23 21:00:00' },
  ]
  const activeIds = store.activeConditions.map(c => c.id)
  assert.deepEqual(activeIds, ['newest', 'middle', 'older'])
})

function candidateAudit(reason = 'risk_ticks_below_min') {
  return { states: { p2: { decisions: {
    'PB0|up|s0|s1|s2|2026-10-26 10:15:00': { reason, values: {
      warning: { id: 123 },
      snapshot: { symbol: 'PB0', instrument: 'pb2611', direction: 'long',
        warning_ts: '2026-10-26 10:15:00', available_at: 1792980900,
        entry: 1000, stop: 991, target: 1100, risk: 9, tick: 1, score: 3.5 },
    } },
    accepted: { reason: 'accepted_warning', values: {} },
  } }, p3: { decisions: { other: { reason: 'score_below_min', values: {} } } } } }
}

test('P2 pool lists filtered signals with concrete reasons without creating executable conditions', () => {
  const { store } = setup()
  store.applySnapshot({ ...snapshot(), strategy: { revision: 'p2' }, strategy_diagnostics: candidateAudit(), conditions: [] })
  assert.equal(store.conditionPool.length, 1)
  assert.equal(store.conditions.length, 0)
  assert.equal(store.activeConditions.length, 0)
  assert.equal(store.waitingConfirmConditions.length, 0)
  const row = store.conditionPool[0]
  assert.equal(row.instrument_id, 'pb2611')
  assert.equal(row.direction, 'up')
  assert.equal(row.event_id, 123)
  assert.equal(row.entry_price, 1000)
  assert.equal(row.volume, 0)
  assert.equal(row.display_only, true)
  assert.equal(row.state, 'filtered')
  assert.match(row.note, /止损距离 9.00 tick.*10 tick.*不会开仓/)
})

test('filtered audit survives account deltas, clears on instance switch and excludes old strategy revisions', async () => {
  const { store, api } = setup()
  store.instancesSupported = true
  store.selectedInstanceId = 'p2-account'
  store.applySnapshot({ ...snapshot(), instance_id: 'p2-account', _stream_id: 'stream', _revision: 1,
    strategy: { revision: 'p2' }, strategy_diagnostics: candidateAudit() })
  store.applyInstanceDelta({ instance_id: 'p2-account', delta: {
    stream_id: 'stream', base_revision: 1, revision: 2, changes: { account: { balance: 2000 } },
  } })
  assert.equal(store.conditionPool.length, 1)
  assert.equal(store.account.balance, 2000)
  store.applyInstanceDelta({ instance_id: 'p2-account', delta: {
    stream_id: 'stream', base_revision: 2, revision: 3, changes: { strategy: { revision: 'p3' } },
  } })
  assert.equal(store.conditionPool.length, 1)
  assert.match(store.conditionPool[0].id, /filtered:p3:/)
  api.traderInstanceRequest = async () => { throw Error('offline') }
  await store.selectInstance('other-account')
  assert.equal(store.conditionPool.length, 0)
  assert.equal(store.strategyDiagnostics, null)
  store.applySnapshot({ ...snapshot(), instance_id: 'other-account', strategy: { revision: 'p2' }, strategy_diagnostics: candidateAudit() })
  assert.equal(store.conditionPool.length, 1)
  store.applySnapshot({ ...snapshot(), instance_id: 'other-account', source: 'local' })
  assert.equal(store.conditionPool.length, 0)
})

test('legacy invalid-input audits retain per-signal identity and show unavailable data safely', () => {
  const { store } = setup()
  const diagnostics = { states: { p2: { decisions: {
    'PB0|down|s0|s1|s2|2026-10-26 10:00:00': {
      reason: 'invalid_input:fee_unavailable', values: { entry: 1000, stop: 1020, target: 900, score: 3.8 },
    },
  } } } }
  store.applySnapshot({ ...snapshot(), strategy: { revision: 'p2' }, strategy_diagnostics: diagnostics,
    conditions: [{ id: 'real', state: 'pending', warning_bar_ts: '2026-10-26 10:15:00', created_at: 1 }] })
  assert.equal(store.conditionPool.length, 2)
  assert.equal(store.conditionPool[0].id, 'real')
  const row = store.conditionPool[1]
  assert.equal(row.symbol, 'PB0')
  assert.equal(row.direction, 'down')
  assert.equal(row.warning_bar_ts, '2026-10-26 10:00:00')
  assert.equal(row.entry_price, 1000)
  assert.ok(Number.isNaN(row.risk))
  assert.match(row.note, /缺少该合约的费用参数/)
})

test('B5 filtered signals retain their strategy identity without inventing an event ID', () => {
  const { store } = setup()
  const key = 'CJ0|up|2026-10-28 09:00:00|2026-10-28 09:15:00|2026-10-28 09:30:00|2026-10-28 09:30:00'
  store.applySnapshot({ ...snapshot(), strategy: { revision: '1' }, strategy_diagnostics: {
    states: { '1': { decisions: { [key]: { reason: 'target_cost_above10', values: {
      snapshot: { symbol: 'CJ0', instrument: 'CJ701', direction: 'Long', warning_ts: '2026-10-28 09:30:00' },
      cost_to_gross_space: 0.32,
    } } } } },
  } })
  assert.equal(store.conditionPool[0].signal_key, key)
  assert.equal(store.conditionPool[0].event_id, null)
  assert.equal(store.conditionPool[0].symbol, 'CJ0')
})

test('all P2/P3 pre-entry filters have readable reasons', () => {
  const { store } = setup()
  for (const reason of ['score_below_min', 'risk_ticks_below_min', 'friction_above20', 'atr_unavailable',
    'atr_out_of_range', 'filtered_net_rr_plan', 'filtered_holiday_window', 'filtered_weekend_window', 'calendar_uncovered']) {
    store.applySnapshot({ ...snapshot(), strategy: { revision: 'p2' }, strategy_diagnostics: candidateAudit(reason) })
    assert.equal(store.conditionPool.length, 1)
    assert.ok(!store.conditionPool[0].note.includes(reason), reason)
    assert.match(store.conditionPool[0].note, /准入过滤.*本信号不会开仓/)
  }
  store.applySnapshot({ ...snapshot(), strategy: { revision: 'p2' }, strategy_diagnostics: null })
  assert.equal(store.conditionPool.length, 0)
})

test('actual condition-pool template shows blocked reasons and omits execution controls', async () => {
  const Vue = require('vue')
  const { compile } = require('@vue/compiler-dom')
  const { renderToString } = require('@vue/server-renderer')
  const { store } = setup()
  store.applySnapshot({ ...snapshot(), strategy: { revision: 'p2' }, strategy_diagnostics: candidateAudit() })
  store.activeTab = 'conditions'
  const source = fs.readFileSync('src/views/LiveTraderView.vue', 'utf8')
  const template = source.match(/<section v-else-if="trader.activeTab === 'conditions'"[\s\S]*?<\/section>/)[0]
    .replace('v-else-if', 'v-if')
  const render = new Function('Vue', compile(template).code)(Vue)
  const identity = value => value
  const context = { trader: store, getInstrumentName: identity, dirLabel: identity,
    conditionBadgeType: () => 'warning', conditionLabel: () => '已过滤，不开仓',
    fmtPrice: identity, formatCondTs: identity, isWarningNote: () => true,
    getNoteCategory: () => '准入过滤', formatNoteText: identity, getNoteTooltip: identity,
    AlertTriangle: {}, Bolt: {}, ExternalLink: {}, InfoCircle: {}, }
  const app = Vue.createSSRApp({ render() { return render(context, []) } })
  for (const name of ['n-tag', 'n-tooltip', 'n-icon', 'n-button', 'n-empty']) {
    app.component(name, { setup(_, { slots }) {
      return () => Vue.h('span', slots.trigger ? slots.trigger() : slots.default?.())
    } })
  }
  const html = await renderToString(app)
  assert.match(html, /pb2611/)
  assert.match(html, /评分 3.50/)
  assert.match(html, /止损距离 9.00 tick/)
  assert.match(html, /已过滤，不开仓/)
  assert.ok(!html.includes('cond-actions'))
  assert.ok(!html.includes('行情触价触发'))
  assert.ok(!html.includes('立即确认开仓'))
  assert.ok(!html.includes('取消剩余开仓'))
  assert.ok(!html.includes('当前实例暂无条件单'))
})
