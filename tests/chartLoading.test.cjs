const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')

const flush = () => new Promise(setImmediate)

function klinesHarness() {
  const requests = []
  const context = { exports: {}, performance, require(name) {
    if (name === 'pinia') return { defineStore: (_, options) => () => {
      const store = options.state()
      for (const [key, action] of Object.entries(options.actions)) store[key] = action.bind(store)
      return store
    } }
    if (name === '../services/api') return { api: { getChartKlines() {
      const request = deferred()
      requests.push(request)
      return request.promise
    } } }
    if (name === '../utils/notify') return { notify: { error() {} } }
    if (name === '../utils/performanceLog') return { reportPerformance() {} }
    throw Error(`Unexpected module ${name}`)
  } }
  const source = ts.transpileModule(fs.readFileSync('src/stores/klines.ts', 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  })
  vm.runInNewContext(source.outputText, context)
  return { store: context.exports.useKlinesStore(), requests }
}

const response = (close) => ({ rows: [{ close }], status: 'tqsdk', message: '' })

const bar = (close, symbol = 'CF0', timeframe = '15m', ts = '2026-10-29 14:15:00') => ({ symbol, timeframe, ts, close })

test('live push before the history response keeps its newer candle without dismissing the mask', async () => {
  const { store, requests } = klinesHarness()
  const load = store.load('CF0', '15m')
  assert.equal(store.acceptPartial('CF0', '15m', bar(123)), true)
  assert.equal(store.loading, true, 'a partial candle cannot replace the pending history request')
  requests[0].resolve({ ...response(100), partial_bar: bar(99) })
  await load
  assert.equal(store.rows[0].close, 100)
  assert.equal(store.partialBar.close, 123)
  assert.equal(store.loading, false)
})

test('every explicit reload clears prior data and a failed request never falls back to it', async () => {
  const { store, requests } = klinesHarness()
  const first = store.load('CF0', '15m')
  requests[0].resolve(response(100))
  await first
  const second = store.load('CF0', '15m')
  assert.equal(store.rows.length, 0)
  assert.equal(store.dataReceivedAt, 0)
  requests[1].reject(Error('network unavailable'))
  await second
  assert.equal(store.rows.length, 0)
  assert.equal(store.error, 'network unavailable')
  assert.equal(store.loading, false)
})

test('superseded loads and other-symbol pushes cannot alter the current chart', async () => {
  const { store, requests } = klinesHarness()
  const first = store.load('CF0', '15m')
  const second = store.load('SR0', '15m')
  requests[0].resolve(response(100))
  await first
  assert.equal(store.loading, true)
  assert.equal(store.rows.length, 0)
  assert.equal(store.acceptPartial('CF0', '15m', bar(456)), false)
  requests[1].resolve(response(600))
  await second
  assert.equal(store.loading, false)
  assert.equal(store.rows[0].close, 600)
})

test('a missing-slice push invalidates live data and older candle pushes are rejected', async () => {
  const { store, requests } = klinesHarness()
  const load = store.load('CF0', '15m')
  requests[0].resolve(response(100))
  await load
  store.acceptPartial('CF0', '15m', bar(123))
  assert.equal(store.acceptPartial('CF0', '15m', bar(99, 'CF0', '15m', '2026-10-29 14:00:00')), false)
  assert.equal(store.partialBar.close, 123)
  store.acceptPartial('CF0', '15m', null)
  assert.equal(store.partialBar, null)
  assert.equal(store.partialReceivedAt, 0)
  assert.equal(store.partialExpected, true)
})

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

test('final history retires a live candle and rejects delayed partial pushes', async () => {
  const { store, requests } = klinesHarness()
  const load = store.load('CF0', '15m')
  const final = bar(110)
  store.acceptPartial('CF0', '15m', bar(99))
  requests[0].resolve({ rows: [final], status: 'tqsdk', message: '', partial_bar: bar(98) })
  await load
  assert.equal(store.latestClosedTs, final.ts)
  assert.equal(store.partialBar, null)
  assert.equal(store.acceptPartial('CF0', '15m', bar(97)), false)
  assert.equal(store.rows[0].close, 110)
  assert.equal(store.acceptPartial('CF0', '15m', bar(120, 'CF0', '15m', '2026-10-29 14:30:00')), true)
  const reload = store.load('CF0', '15m', 500, true)
  requests[1].resolve({ rows: [final], status: 'tqsdk', message: '' })
  await reload
  assert.equal(store.acceptPartial('CF0', '15m', bar(90)), false)
})

test('closure watermark resets when switching symbols', async () => {
  const { store, requests } = klinesHarness()
  const first = store.load('CF0', '15m')
  requests[0].resolve({ rows: [bar(110)], status: 'tqsdk', message: '' })
  await first
  const next = store.load('SR0', '15m')
  assert.equal(store.latestClosedTs, '')
  assert.equal(store.acceptPartial('SR0', '15m', bar(95, 'SR0', '15m', '2026-10-29 14:00:00')), true)
  requests[1].resolve({ rows: [bar(90, 'SR0', '15m', '2026-10-29 13:45:00')], status: 'tqsdk', message: '' })
  await next
  assert.equal(store.partialBar.close, 95)
})

// Execute the view's real request and switch handlers with controlled network ordering.
function chartHarness() {
  const source = fs.readFileSync('src/views/ChartView.vue', 'utf8')
  const trend = source.slice(source.indexOf('const trendPoints ='), source.indexOf('/** 记录“默认隐藏规则”'))
  const switchHandler = source.slice(source.indexOf('watch([symbol, timeframe], async () => {'), source.indexOf('// 完整性检测与最近形态只针对当前品种'))
  assert.ok(trend.length && switchHandler.length)
  const cell = (value) => ({ value })
  const trendRequests = [], klineRequests = []
  let switchChart
  const context = {
    ref: cell,
    symbol: cell('CF0'), timeframe: cell('15m'), chartLoadLimit: cell(1200),
    klinesStore: { chartStatus: 'tqsdk', partialBar: null, load: (symbol, timeframe) => {
      const request = { symbol, timeframe, ...deferred() }
      klineRequests.push(request)
      return request.promise
    } },
    api: { getTrendSeries: (symbol, timeframe) => {
      const request = { symbol, timeframe, ...deferred() }
      trendRequests.push(request)
      return request.promise
    } },
    manualLevelsStore: { load: async () => {} },
    scansStore: { refreshLatestSignals: async () => {} },
    applyDefaultHidden() {}, updateLiveBar() {},
    snapshots: cell({}),
    watch: (_, handler) => { switchChart = handler },
  }
  for (const key of ['lastRepairResult', 'manualLevelChartPreviews', 'editingManualLevelId',
    'manualLevelDraft', 'hiddenManualLevelIds', 'hiddenApplied', 'liveBars', 'shownRecentNumbers']) context[key] = cell(null)
  const compiled = ts.transpileModule(`${trend}\n${switchHandler}\nglobalThis.trendPoints = trendPoints; globalThis.loadTrendLine = loadTrendLine`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  })
  vm.runInNewContext(compiled.outputText, context)
  return { context, trendRequests, klineRequests, async switchTo(symbol, timeframe = '15m') {
    context.symbol.value = symbol
    context.timeframe.value = timeframe
    return switchChart()
  } }
}

test('old symbol MA cannot enter the new chart while its K-lines are still loading', async () => {
  const h = chartHarness()
  const old = h.context.loadTrendLine()
  const next = h.switchTo('SR0')
  h.trendRequests[0].resolve([{ value: 15600 }])
  await old
  assert.equal(h.context.trendPoints.value.length, 0)
  h.klineRequests[0].resolve()
  await next
  h.trendRequests[1].resolve([{ value: 6311.5 }])
  await flush()
  assert.equal(h.context.trendPoints.value[0].value, 6311.5)
})

test('returning to the same symbol still rejects the first visit response', async () => {
  const h = chartHarness()
  const old = h.context.loadTrendLine()
  const middle = h.switchTo('SR0')
  const latest = h.switchTo('CF0')
  h.trendRequests[0].resolve([{ value: 15000 }])
  await old
  assert.equal(h.context.trendPoints.value.length, 0)
  h.klineRequests[0].resolve()
  await middle
  assert.equal(h.trendRequests.length, 1, 'a superseded K-line load must not start another MA request')
  h.klineRequests[1].resolve()
  await latest
  assert.equal(h.trendRequests.length, 2)
})

test('an old timeframe rejection cannot clear the new timeframe MA', async () => {
  const h = chartHarness()
  const old = h.context.loadTrendLine()
  const next = h.switchTo('CF0', '60m')
  h.klineRequests[0].resolve()
  await next
  h.trendRequests[1].resolve([{ value: 15635 }])
  await flush()
  h.trendRequests[0].reject(Error('old request failed'))
  await old
  assert.equal(h.context.trendPoints.value[0].value, 15635)
})

test('symbol change rejects an old MA even before the queued switch handler runs', async () => {
  const h = chartHarness()
  const old = h.context.loadTrendLine()
  h.context.symbol.value = 'SR0'
  h.trendRequests[0].resolve([{ value: 15600 }])
  await old
  assert.equal(h.context.trendPoints.value.length, 0)
})
