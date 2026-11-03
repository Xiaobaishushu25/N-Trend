const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')
const { computed, ref } = require('vue')

function harness(rows, history = []) {
  const source = fs.readFileSync('src/views/ChartView.vue', 'utf8')
  const comparator = source.slice(source.indexOf('function compareSignalTime'), source.indexOf('/** 把新事件表记录'))
  const lists = source.slice(source.indexOf('const signals = computed'), source.indexOf('function loadRecentPatterns'))
  const context = {
    computed, ref, symbol: ref('RB0'), scansStore: { latestSignals: rows },
    qualitySort: ref(false), qualityDisplay: { enabled: false },
    toChartSignal: row => ({ ...row, number: row.id }),
  }
  vm.runInNewContext(ts.transpileModule(`${comparator}\n${lists}\nObject.assign(globalThis, { signals, recentSignals, recentHistorySignals })`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText, context)
  context.recentSignals.value = history
  return {
    active: () => Array.from(context.signals.value, row => row.number),
    recent: () => Array.from(context.recentHistorySignals.value, row => row.number),
  }
}

const signal = (id, state, warning_ts, trigger_ts = null, extra = {}) => ({
  id, number: id, symbol: 'RB0', state, warning_ts, trigger_ts, score: 1, ...extra,
})

test('chart shows only current-symbol active signals in recognition/trigger time order', () => {
  const h = harness([
    signal(6001, 'pending', '2026-10-20 09:00:00', null, { score: 100 }),
    signal(3, 'triggered', '2026-10-22 09:00:00', '2026-10-22 10:00:00'),
    signal(4, 'triggered', '2026-10-22 09:00:00', '2026-10-22 11:00:00'),
    signal(5, 'closed', '2026-10-23 09:00:00'),
    signal(6, 'invalidated', '2026-10-23 09:00:00'),
    signal(7, 'triggered', '2026-10-23 09:00:00', null, { symbol: 'CF0' }),
    signal(8, 'filtered', '2026-10-23 09:00:00'),
  ])
  assert.deepEqual(h.active(), [4, 3, 6001])
})

test('recent signals exclude all in-flight states and keep the newest five with stable ties', () => {
  const h = harness([signal(1, 'pending', '2026-10-23 09:00:00')], [
    signal(1, 'pending', '2026-10-23 09:00:00'),
    signal(2, 'triggered', '2026-10-23 09:00:00'),
    signal(9000, 'closed', '2026-10-20 09:00:00'),
    signal(3, 'closed', '2026-10-22 09:00:00', '2026-10-22 10:00:00'),
    signal(4, 'closed', '2026-10-22 09:00:00', '2026-10-22 11:00:00'),
    signal(5, 'closed', '2026-10-22 09:00:00', '2026-10-22 11:00:00'),
    signal(6, 'invalidated', '2026-10-21 10:00:00'),
    signal(7, 'unfilled', '2026-10-21 09:00:00'),
    signal(8, 'closed', null),
  ])
  assert.deepEqual(h.recent(), [5, 4, 3, 6, 7])
})


function loadingHarness() {
  const source = fs.readFileSync('src/views/ChartView.vue', 'utf8')
  const code = source.slice(source.indexOf('const recentSignals ='), source.indexOf('/** 被点击隐藏的形态编号'))
  const requests = []
  const context = {
    ref, computed, performance, symbol: ref('RB0'), signals: ref([]),
    qualitySort: ref(false), qualityDisplay: { enabled: false },
    compareSignalTime: (a, b) => b.number - a.number,
    toChartSignalFromOutcome: row => row, reportPerformance() {},
    api: { getRecentOutcomes: (_limit, filters) => new Promise((resolve, reject) => requests.push({ ...filters, resolve, reject })) },
  }
  vm.runInNewContext(ts.transpileModule(`${code}\nObject.assign(globalThis, { loadRecentPatterns, recentSignals, recentLoading })`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText, context)
  return { context, requests }
}

test('overlapping recent-signal refreshes share the pending request', async () => {
  const { context: c, requests } = loadingHarness()
  const first = c.loadRecentPatterns()
  const second = c.loadRecentPatterns()
  assert.equal(first, second)
  assert.equal(requests.length, 1)
  requests[0].resolve([{ number: 1 }])
  await first
  assert.equal(c.recentSignals.value[0].number, 1)
  const refresh = c.loadRecentPatterns()
  assert.equal(c.recentSignals.value[0].number, 1, 'same-symbol refresh keeps displayed cards')
  requests[1].resolve([{ number: 2 }])
  await refresh
  assert.equal(c.recentSignals.value[0].number, 2)
})

test('switching symbols clears old cards and rejects a late old-symbol response', async () => {
  const { context: c, requests } = loadingHarness()
  const first = c.loadRecentPatterns()
  requests[0].resolve([{ number: 1 }])
  await first
  const old = c.loadRecentPatterns()
  c.symbol.value = 'SA0'
  const next = c.loadRecentPatterns()
  assert.equal(c.recentSignals.value.length, 0)
  requests[1].resolve([{ number: 99 }])
  await old
  assert.equal(c.recentLoading.value, true)
  assert.equal(c.recentSignals.value.length, 0)
  requests[2].resolve([{ number: 2 }])
  await next
  assert.equal(c.recentSignals.value[0].number, 2)
  assert.equal(c.recentLoading.value, false)
})
