const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')

function getTradingHoursModule() {
  const context = { exports: {} }
  const source = fs.readFileSync('src/utils/tradingHours.ts', 'utf8')
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  })
  vm.runInNewContext(compiled.outputText, context)
  return context.exports
}

const { isMarketTradingWindow } = getTradingHoursModule()

function createDate(year, month, day, hours, minutes) {
  // month is 1-based for readability
  return new Date(year, month - 1, day, hours, minutes, 0, 0)
}

test('Friday night session opening transition at 20:55', () => {
  // 2026-10-23 is Friday
  const date2035 = createDate(2026, 10, 23, 20, 35) // user's screenshot
  assert.equal(isMarketTradingWindow(date2035), false, '20:35 before night session should be closed')

  const date2054 = createDate(2026, 10, 23, 20, 54)
  assert.equal(isMarketTradingWindow(date2054), false, '20:54 should still be closed')

  const date2055 = createDate(2026, 10, 23, 20, 55)
  assert.equal(isMarketTradingWindow(date2055), true, '20:55 call auction should be trading window')

  const date2130 = createDate(2026, 10, 23, 21, 30)
  assert.equal(isMarketTradingWindow(date2130), true, '21:30 should be trading window')
})

test('Weekend and Saturday post-midnight session', () => {
  // 2026-10-24 is Saturday
  const sat0130 = createDate(2026, 10, 24, 1, 30)
  assert.equal(isMarketTradingWindow(sat0130), true, 'Saturday 01:30 (Friday night spillover) should be trading window')

  const sat0230 = createDate(2026, 10, 24, 2, 30)
  assert.equal(isMarketTradingWindow(sat0230), true, 'Saturday 02:30 boundary should be trading window')

  const sat0235 = createDate(2026, 10, 24, 2, 35)
  assert.equal(isMarketTradingWindow(sat0235), false, 'Saturday 02:35 after close should be closed')

  const sat1400 = createDate(2026, 10, 24, 14, 0)
  assert.equal(isMarketTradingWindow(sat1400), false, 'Saturday daytime should be closed')

  // 2026-10-25 is Sunday
  const sun1000 = createDate(2026, 10, 25, 10, 0)
  assert.equal(isMarketTradingWindow(sun1000), false, 'Sunday morning should be closed')

  const sun2100 = createDate(2026, 10, 25, 21, 0)
  assert.equal(isMarketTradingWindow(sun2100), false, 'Sunday night should be closed')
})

test('Weekday day sessions and intermissions', () => {
  // 2026-10-26 is Monday
  const mon0830 = createDate(2026, 10, 26, 8, 30)
  assert.equal(isMarketTradingWindow(mon0830), false, 'Monday 08:30 before call auction should be closed')

  const mon0855 = createDate(2026, 10, 26, 8, 55)
  assert.equal(isMarketTradingWindow(mon0855), true, 'Monday 08:55 call auction should be trading window')

  const mon0930 = createDate(2026, 10, 26, 9, 30)
  assert.equal(isMarketTradingWindow(mon0930), true, 'Monday 09:30 should be trading window')

  const mon1020 = createDate(2026, 10, 26, 10, 20)
  assert.equal(isMarketTradingWindow(mon1020), false, 'Monday 10:20 tea break should be closed')

  const mon1030 = createDate(2026, 10, 26, 10, 30)
  assert.equal(isMarketTradingWindow(mon1030), true, 'Monday 10:30 should be trading window')

  const mon1200 = createDate(2026, 10, 26, 12, 0)
  assert.equal(isMarketTradingWindow(mon1200), false, 'Monday 12:00 lunch break should be closed')

  const mon1330 = createDate(2026, 10, 26, 13, 30)
  assert.equal(isMarketTradingWindow(mon1330), true, 'Monday 13:30 afternoon should be trading window')

  const mon1500 = createDate(2026, 10, 26, 15, 0)
  assert.equal(isMarketTradingWindow(mon1500), true, 'Monday 15:00 close should be trading window')

  const mon1515 = createDate(2026, 10, 26, 15, 15)
  assert.equal(isMarketTradingWindow(mon1515), false, 'Monday 15:15 settlement intermission should be closed')

  const mon2055 = createDate(2026, 10, 26, 20, 55)
  assert.equal(isMarketTradingWindow(mon2055), true, 'Monday 20:55 night opening should be trading window')
})
