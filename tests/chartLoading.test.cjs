const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')

const flush = () => new Promise(setImmediate)
function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

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
