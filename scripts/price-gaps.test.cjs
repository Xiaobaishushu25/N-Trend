const assert = require('node:assert/strict')
const fs = require('node:fs')
const Module = require('node:module')
const path = require('node:path')
const test = require('node:test')
const ts = require('typescript')

const filename = path.resolve(__dirname, '../src/utils/priceGaps.ts')
const js = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText
const compiled = new Module(filename, module)
compiled.filename = filename
compiled.paths = module.paths
compiled._compile(js, filename)
const { computePriceGaps, gapBoundaryCoordinate } = compiled.exports

function bar(ts, low, high, extra = {}) {
  return { ts, low, high, open: low, close: high, symbol: 'eg2610', source: 'tq', rollover: false, ...extra }
}

test('detects overnight and daily gaps, but ignores rollovers', () => {
  const rows = [bar('2026-09-17 15:00:00', 98, 100), bar('2026-09-18 09:05:00', 110, 115)]
  assert.deepEqual(computePriceGaps(rows, '5m').map(({ bottom, top }) => [bottom, top]), [[100, 110]])
  assert.equal(computePriceGaps(rows, '1d').length, 1)
  rows[1].rollover = true
  assert.equal(computePriceGaps(rows, '5m').length, 0)
})

test('does not mistake missing intraday bars or provisional quotes for gaps', () => {
  const rows = [bar('2026-09-18 09:05:00', 98, 100), bar('2026-09-18 09:20:00', 110, 115)]
  assert.equal(computePriceGaps(rows, '5m').length, 0)
  rows[1].ts = '2026-09-18 09:10:00'
  rows[1].source = 'live_quote'
  assert.equal(computePriceGaps(rows, '5m').length, 0)
})

test('keeps the scheduled morning break eligible for a price gap', () => {
  const rows = [bar('2026-09-18 10:15:00', 98, 100), bar('2026-09-18 10:35:00', 110, 115)]
  assert.equal(computePriceGaps(rows, '5m').length, 1)
})

test('places a gap at candle times even when chart spacing includes other series', () => {
  const bars = [{ time: 'a' }, { time: 'b' }, { time: 'c' }]
  const coordinates = { a: 8, b: 26, c: 100 }
  assert.equal(gapBoundaryCoordinate(bars, 2, (time) => coordinates[time]), 63)
  assert.equal(gapBoundaryCoordinate(bars, 0, (time) => coordinates[time]), null)
})

test('removes an up gap entirely after a later candle fully fills it', () => {
  const rows = [
    bar('2026-09-18 09:05:00', 98, 100),
    bar('2026-09-18 09:10:00', 110, 115),
    bar('2026-09-18 09:15:00', 105, 114),
    bar('2026-09-18 09:20:00', 99, 108),
  ]
  assert.deepEqual(computePriceGaps(rows, '5m'), [])
})

test('shows only the unfilled remainder of a partly filled up gap', () => {
  const rows = [
    bar('2026-09-18 09:05:00', 98, 100),
    bar('2026-09-18 09:10:00', 110, 115),
    bar('2026-09-18 09:15:00', 105, 114),
  ]
  assert.deepEqual(computePriceGaps(rows, '5m').map(({ fromIndex, bottom, top }) =>
    [fromIndex, bottom, top]), [[1, 100, 105]])
})

test('shows only the unfilled remainder of a partly filled down gap', () => {
  const rows = [
    bar('2026-09-18 09:05:00', 110, 115),
    bar('2026-09-18 09:10:00', 95, 100),
    bar('2026-09-18 09:15:00', 97, 105),
  ]
  assert.deepEqual(computePriceGaps(rows, '5m').map(({ fromIndex, bottom, top, direction }) =>
    [fromIndex, bottom, top, direction]), [[1, 105, 110, 'down']])
  rows.push(bar('2026-09-18 09:20:00', 103, 110))
  assert.deepEqual(computePriceGaps(rows, '5m'), [])
})
