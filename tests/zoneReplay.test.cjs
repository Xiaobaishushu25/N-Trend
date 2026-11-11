const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const { test } = require('node:test')
const ts = require('typescript')

const context = {
  exports: {},
  require: () => ({ researchChartTime: value => Date.parse(value.replace(' ', 'T')+'Z')/1000 }),
}
vm.runInNewContext(ts.transpileModule(fs.readFileSync('src/utils/zoneReplay.ts','utf8'),{
  compilerOptions:{target:ts.ScriptTarget.ES2021,module:ts.ModuleKind.CommonJS},
}).outputText,context)
const { parseZoneReplay, replayBarLabel } = context.exports

const tsAt = time => `2024-08-23 ${time}:00`
const row = (start,actualEnd,ts) => ({start:tsAt(start),actualEnd:tsAt(actualEnd),ts:tsAt(ts),
  symbol:'CZCE.CF501',timeframe:'30m',open:100,high:101,low:99,close:100,volume:20})
const rows = [row('09:30','10:00','10:00'),row('10:00','10:15','10:30'),row('10:30','11:00','11:00')]

test('short-bucket labels map the actual close and keep open fills out of gaps',()=>{
  assert.equal(replayBarLabel(rows,tsAt('10:15')),tsAt('10:30'))
  assert.equal(replayBarLabel(rows,tsAt('10:15'),true),null)
  assert.equal(replayBarLabel(rows,tsAt('10:30'),true),tsAt('11:00'))
  assert.equal(replayBarLabel(rows,tsAt('10:00'),true),tsAt('10:30'))
})

test('import rejects markers referencing an unknown event or contract',()=>{
  const p={version:1,timeframe:'30m',product:'CF',fold:1,bars:rows,
    structures:[{id:'s',kind:'zone',contract:'CZCE.CF501',formed_ns:tsAt('10:00'),confirmed_ns:tsAt('10:00'),
      end_ns:tsAt('11:00'),zones:[{low:99,high:101}]}],
    visits:[{id:'v',structure_id:'s',contract:'CZCE.CF501',touch_ns:tsAt('10:15'),
      classified_ns:tsAt('11:00'),reaction:'reversal'}],
    trades:[]}
  assert.equal(parseZoneReplay(JSON.stringify(p)).visits.length,1)
  p.trades.push({event_id:'wrong',entry_ns:tsAt('11:00'),exit_ns:tsAt('11:00'),entry:100,stop:99,exit:100,net:0})
  assert.throws(()=>parseZoneReplay(JSON.stringify(p)),/成交记录无效/)
  p.trades=[]; p.visits[0].contract='CZCE.CF409'
  assert.throws(()=>parseZoneReplay(JSON.stringify(p)),/到访记录无效/)
})
