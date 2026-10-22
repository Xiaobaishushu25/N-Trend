const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')
function store() {
 const module = {exports:{}}
 const source = ts.transpileModule(fs.readFileSync('src/stores/tradeReview.ts','utf8'), {compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText
 const requireMock = name => name==='pinia' ? {defineStore:(_,options)=>options} : name.includes('symbols') ? {useSymbolsStore:()=>({symbols:[]})} : name.includes('multipliers') ? {getSymbolMultiplier:()=>10,calcTradePnl:()=>({points:10,amount:100,pct:1,isWin:true})} : {}
 vm.runInNewContext(source,{module,exports:module.exports,require:requireMock,console})
 return module.exports.useTradeReviewStore
}
const row = {event_id:1,symbol:'RB0',direction:'up',state:'closed',outcome:'loss',entry:100,stop:90,exit_price:110,entry_score:3,entry_score_dims:JSON.stringify({evaluation:{net_amount:-5,gross_points:10,volume:2}})}
test('fees can make a gross winner a net loser without changing fractional exits',()=>{
 const s=store();const tickets=s.getters.tickets.call({rawOutcomes:[row],lots:1});assert.equal(tickets[0].pnl_amount,-5);assert.equal(tickets[0].lots,2)
 const metrics=s.getters.metrics.call({tickets});assert.equal(metrics.winCount,0);assert.equal(metrics.lossCount,1)
})
test('unknown paths and unpriced results never count as completed losses',()=>{
 const s=store();const tickets=s.getters.tickets.call({rawOutcomes:['ambiguous','filtered','unpriced'].map(state=>({...row,state,outcome:state})),lots:1})
 const metrics=s.getters.metrics.call({tickets});assert.equal(metrics.totalSettled,0)
})
