const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')
const { computed } = require('vue')

const fixture = JSON.parse(fs.readFileSync('tests/fixtures/researchBoxParity.json', 'utf8'))
const source = fs.readFileSync('src/utils/researchBoxes.ts', 'utf8')
const context = { exports: {}, require: path => require(`../src/utils/${path.replace('./', '')}`) }
vm.runInNewContext(ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2021, module: ts.ModuleKind.CommonJS, esModuleInterop: true },
}).outputText, context)
const { detectResearchBoxes, researchChartTime } = context.exports
const geometry = boxes => JSON.parse(JSON.stringify(boxes.map(({ confirmedIndex, lower, upper }) => ({ confirmedIndex, lower, upper }))))

for (const sample of fixture) {
  for (const n of [24, 48, 96]) {
    test(`${sample.symbol}/${n}: exact parity with frozen Python recognition on actual training bars`, () => {
      assert.deepEqual(geometry(detectResearchBoxes(sample.rows, n, sample.symbol)), sample.expected[n])
    })
  }
}

function oscillations(count = 70) {
  return fixture[0].rows.slice(0, count).map((row, i) => ({
    ...row, open: 105, close: 105, high: i % 8 === 0 ? 110 : 107,
    low: i % 8 === 4 ? 100 : 103, volume: 100, valid: true, segmentReset: i === 0,
  }))
}

for (const [timeframe, closes] of [
  ['15m', ['09:15', '09:30', '09:45', '10:00', '10:15', '10:45', '11:00', '11:15', '11:30', '13:45', '14:00', '14:15', '14:30', '14:45', '15:00']],
  ['30m', ['09:30', '10:00', '10:30', '11:00', '11:30', '14:00', '14:30', '15:00']],
]) {
  test(`${timeframe}: scheduled chart buckets bridge session breaks, but a missing bucket ends the box`, () => {
    const timestamps = ['02', '03', '04', '05'].flatMap(day => closes.map(close => `2026-11-${day} ${close}:00`))
    const rows = oscillations(timestamps.length).map((row, i) => ({ ...row, ts: timestamps[i] }))
    const boxes = detectResearchBoxes(rows, 24, 'CJ0', timeframe)
    assert.equal(boxes[0].confirmedIndex, 23)
    assert.equal(boxes[0].upper, 110)
    assert.equal(boxes[0].lower, 100)
    rows.splice(25, 1)
    const interrupted = detectResearchBoxes(rows, 24, 'CJ0', timeframe)[0]
    assert.equal(interrupted.endReason, 'gap')
    assert.equal(interrupted.endIndex, 24)
  })
}

test('confirmation occurs only at the current close, rails remain fixed and expire after N bars', () => {
  const rows = oscillations()
  const boxes = detectResearchBoxes(rows, 24, 'C0')
  assert.equal(boxes[0].confirmedIndex, 23)
  assert.equal(boxes[0].lower, 100)
  assert.equal(boxes[0].upper, 110)
  assert.equal(boxes[0].endIndex, 47)
  assert.equal(boxes[0].endReason, 'expired')
  rows[25].high = 110.5
  assert.deepEqual(geometry(detectResearchBoxes(rows, 24, 'C0'))[0], geometry(boxes)[0])
})

test('future prices cannot alter already confirmed geometry', () => {
  const rows = oscillations()
  const prefix = rows.slice(0, 40)
  const before = geometry(detectResearchBoxes(prefix, 24, 'C0'))
  rows[44].high = 5000; rows[44].close = 4999
  assert.deepEqual(geometry(detectResearchBoxes(rows, 24, 'C0')).filter(b => b.confirmedIndex < 40), before)
})

test('a dual-touch bar invalidates the entire formation window', () => {
  const rows = oscillations(24); rows[10].high = 110; rows[10].low = 100
  assert.equal(detectResearchBoxes(rows, 24, 'C0').length, 0)
})

test('close breakout ends the fixed box at its actual bar', () => {
  const rows = oscillations(); rows[28].close = 111; rows[28].high = 112
  const box = detectResearchBoxes(rows, 24, 'C0')[0]
  assert.equal(box.endIndex, 28); assert.equal(box.endReason, 'breakout')
})

test('missing expected slot resets warmup rather than joining across the gap', () => {
  const rows = oscillations(44); rows.splice(22, 1)
  assert.equal(detectResearchBoxes(rows, 24, 'C0').length, 0)
})

test('rollover terminates the old box and starts a new warmup', () => {
  const rows = oscillations(45); rows[25].rollover = true
  const boxes = detectResearchBoxes(rows, 24, 'C0')
  assert.equal(boxes.length, 1); assert.equal(boxes[0].endReason, 'rollover'); assert.equal(boxes[0].endIndex, 24)
})

test('bad OHLC does not become formation history', () => {
  const rows = oscillations(44); rows[22].low = 999
  assert.equal(detectResearchBoxes(rows, 24, 'C0').length, 0)
})

function closedRowHarness(props) {
  const component = fs.readFileSync('src/components/KLineChart.vue', 'utf8')
  const part = component.slice(component.indexOf('const researchClosedRows ='), component.indexOf('const researchBoxes ='))
  const c = { props, computed, researchChartTime, Date }
  vm.runInNewContext(`${part.replace(/const researchClosedRows/, 'globalThis.closedRows')}`, c)
  return c.closedRows.value
}

test('unfinished live candle never participates in research recognition', () => {
  const closed = { ...fixture[0].rows[0], symbol: 'C0', timeframe: '5m' }
  const live = { ...closed, ts: '2099-01-01 09:05:00' }
  assert.equal(closedRowHarness({ rows: [closed, live], researchRows: [closed], symbol: 'C0', timeframe: '5m' }).length, 1)
  assert.equal(closedRowHarness({ rows: [closed, live], symbol: 'C0', timeframe: '5m' }).length, 1)
})

test('stale previous-symbol and previous-timeframe rows cannot draw on the new chart', () => {
  const row = { ...fixture[0].rows[0], symbol: 'C0', timeframe: '5m' }
  assert.equal(closedRowHarness({ rows: [], researchRows: [row], symbol: 'EB0', timeframe: '5m' }).length, 0)
  assert.equal(closedRowHarness({ rows: [], researchRows: [row], symbol: 'C0', timeframe: '15m' }).length, 0)
})
