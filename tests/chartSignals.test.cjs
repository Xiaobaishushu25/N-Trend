const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')
const { computed, ref } = require('vue')

function harness(rows, history = []) {
  const source = fs.readFileSync('src/views/ChartView.vue', 'utf8')
  const comparator = source.slice(source.indexOf('function compareSignalTime'), source.indexOf('/** 把新事件表记录'))
  const lists = source.slice(source.indexOf('const signals = computed'), source.indexOf('async function loadRecentPatterns'))
  const context = {
    computed, ref, symbol: ref('RB0'), scansStore: { latestSignals: rows },
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
