const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')
const context = { exports: {}, Date, Intl, require: () => ({ calcTradeRecordPnlAmount: () => 0 }) }
vm.runInNewContext(ts.transpileModule(fs.readFileSync('src/utils/tradeTime.ts', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, context)
const { formatTradeDateTime: format, extractTradeDate, parseTradeTimestamp } = context.exports

test('presentation preserves canonical dates across days, weekends and midnight', () => {
  assert.equal(format('20261116 21:45:59'), '2026-11-16 21:45:59')
  assert.equal(format('2026-11-17 21:05:00', '2026-11-16 21:45:59'), '2026-11-17 21:05:00')
  assert.equal(format('2026-11-21 00:30:00'), '2026-11-21 00:30:00')
  assert.equal(format('2026-11-16 21:45:59', '2026-11-13 22:00:00'), '2026-11-16 21:45:59')
  assert.equal(extractTradeDate('2026-11-16 22:59:11'), '2026-11-16')
})
test('timestamps and calendar dates use Shanghai independently of client timezone', () => {
  assert.equal(format('2026-11-16T13:45:59Z'), '2026-11-16 21:45:59')
  assert.equal(parseTradeTimestamp('2026-11-16 21:45:59'), Date.parse('2026-11-16T21:45:59+08:00'))
  assert.equal(extractTradeDate('2026-11-16T20:30:00Z'), '2026-11-17')
  assert.equal(parseTradeTimestamp('2026-02-30 12:00:00'), 0)
  assert.equal(parseTradeTimestamp('bad'), 0)
  assert.equal(extractTradeDate('bad'), null)
  assert.equal(format(''), '—')
})
