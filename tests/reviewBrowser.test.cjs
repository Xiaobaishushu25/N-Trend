const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')
const { parse, compileScript } = require('@vue/compiler-sfc')
const { createSSRApp } = require('vue')
const { renderToString } = require('@vue/server-renderer')

function evaluate(path, requireMock = require) {
  const module = { exports: {} }
  const code = ts.transpileModule(fs.readFileSync(path, 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
  vm.runInNewContext(code, { module, exports: module.exports, require: requireMock, console })
  return module.exports
}
const filters = evaluate('src/utils/reviewFilters.ts')
function deferred() { let resolve, reject; const promise = new Promise((a,b) => { resolve=a; reject=b }); return { promise, resolve, reject } }
function harness() {
  const requests = [], storage = new Map()
  const module = { exports: {} }
  const code = ts.transpileModule(fs.readFileSync('src/stores/review.ts', 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
  vm.runInNewContext(code, { module, exports: module.exports, console, localStorage: { getItem: k => storage.get(k), setItem: (k,v) => storage.set(k,v) }, require: name => {
    if (name === 'pinia') return { defineStore: (_, options) => options }
    if (name.includes('reviewFilters')) return filters
    if (name.includes('services/api')) return { api: { getReviewPage(limit, offset, filters) { const request = { limit, offset, filters, ...deferred() }; requests.push(request); return request.promise } } }
    throw Error(name)
  } })
  const options = module.exports.useReviewStore, store = options.state()
  for (const [key, action] of Object.entries(options.actions)) store[key] = action.bind(store)
  return { store, requests, storage }
}
const page = id => ({ items: [{ event_id: id }], total: 161, scored: 150, unscored: 11, symbols: ['BU0','SA0'], offset: 0, limit: 80 })

test('all selected fields survive transport; navigation offsets are not filter inputs', () => {
  const query = filters.reviewFilterQuery({ symbols: ['BU0','SA0'], qualityMetric: 'quality_v2', qualityMin: 70.5, qualityMax: 90, timeField: 'trigger', timeStart: '2026-10-01 09:00:00', hourStart: '21:00', hourEnd: '02:30', sortBy: 'score_asc', pageOffset: 80 })
  assert.equal(query.symbols, 'BU0,SA0')
  assert.equal(query.quality_min, 70.5)
  assert.equal(query.score_min, null, 'new scores must not accidentally use the legacy 0–5 gate')
  assert.equal(query.time_start, '2026-10-01 09:00:00')
  assert.equal(query.hour_end, '02:30')
  assert.equal(query.sort_by, 'score_asc')
  assert.equal(query.pageOffset, undefined)
})

test('invalid and missing candidate scores do not fall back to v1, zero or a probability', () => {
  const row = { entry_score: 3, warning_ts: '2026-10-01 09:00:00', trigger_ts: null, trigger_bar_ts: null,
    quality: { status: 'ok', quality_score: 80 }, review_scores: { status: 'missing_data', quality_score: 99 } }
  assert.equal(filters.reviewScore(row, 'quality_v2'), null)
  assert.equal(filters.reviewScore(row, 'quality_v1'), 80)
  assert.equal(filters.reviewTime(row, 'trigger'), null)
  assert.equal(filters.reviewScoreStatus('missing_data'), '行情不连续')
})

test('older results cannot overwrite a newer filter or clear its loading state', async () => {
  const { store, requests } = harness()
  const older = store.loadReviewPage()
  const newer = store.setBrowserFilters({ qualityMin: 80, sortBy: 'score_desc' })
  requests[0].resolve(page(1)); await older
  assert.equal(store.browserLoading, true)
  assert.equal(store.browserRows.length, 0)
  requests[1].resolve(page(2)); await newer
  assert.equal(store.browserRows[0].event_id, 2)
  assert.equal(store.browserLoading, false)
})

test('debounced filter drafts invalidate in-flight responses immediately', async () => {
  const { store, requests } = harness()
  const pending = store.loadReviewPage()
  store.updateBrowserFilterDraft({ symbols: ['SA0'], timeField: 'trigger' })
  requests[0].resolve(page(1)); await pending
  assert.equal(store.browserRows.length, 0)
  assert.equal(store.browserLoading, true)
  const current = store.loadReviewPage()
  assert.equal(requests[1].filters.symbols[0], 'SA0')
  requests[1].resolve(page(2)); await current
  assert.equal(store.browserRows[0].event_id, 2)
})

test('pagination and invalid ranges never silently reuse stale results', async () => {
  const { store, requests } = harness()
  store.browserPage = 3; store.browserPageSize = 40
  const pending = store.loadReviewPage()
  assert.equal(requests[0].offset, 80)
  assert.equal(requests[0].limit, 40)
  requests[0].resolve(page(3)); await pending
  await store.setBrowserFilters({ qualityMin: 90, qualityMax: 70 })
  assert.equal(requests.length, 1)
  assert.equal(store.browserRows.length, 0)
  assert.equal(store.browserTotal, 0)
  assert.match(store.browserError, /下限/)
  assert.equal(store.browserLoading, false)
})

test('legacy server arrays show an upgrade error without corrupting reactive state', async () => {
  const { store, requests } = harness()
  const pending = store.loadReviewPage()
  requests[0].resolve([{ event_id: 1 }]); await pending
  assert.match(store.browserError, /版本不匹配.*客户端和服务端同时升级/)
  assert.equal(store.browserLoading, false)
  assert.equal(store.browserRows.length, 0)
  assert.ok(Array.isArray(store.browserSymbols))
  assert.equal(store.browserTotal, 0)

  const retry = store.loadReviewPage()
  requests[1].resolve(page(2)); await retry
  assert.equal(store.browserError, '')
  assert.equal(store.browserRows[0].event_id, 2)
  assert.equal(store.browserSymbols[0], 'BU0')
})

test('incomplete pagination responses fail before any state is assigned', async () => {
  for (const response of [null, {}, { ...page(1), symbols: undefined }, { ...page(1), symbols: [null] }, { ...page(1), total: '161' }, { ...page(1), scored: 1 }]) {
    const { store, requests } = harness()
    const pending = store.loadReviewPage()
    requests[0].resolve(response); await pending
    assert.match(store.browserError, /格式不完整/)
    assert.equal(store.browserLoading, false)
    assert.equal(store.browserRows.length, 0)
    assert.ok(Array.isArray(store.browserSymbols))
    assert.equal(store.browserTotal, 0)
  }
})

test('score explanations render candidate identity, partial levels and escaped evidence', async () => {
  const { descriptor } = parse(fs.readFileSync('src/components/ReviewScoreDetail.vue', 'utf8'))
  const source = compileScript(descriptor, { id: 'review-detail-test', inlineTemplate: true }).content
  const module = { exports: {} }
  const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
  vm.runInNewContext(code, { module, exports: module.exports, require: name => name === 'vue' ? require('vue') : name.includes('reviewFilters') ? filters : evaluate('src/utils/signalQuality.ts') })
  const row = { event_id: 1, symbol: 'BU0', entry_score: 3.5, rr: 1.2, quality: { status: 'ok', quality_score: 75, morphology_score: 80, explanations: [] }, review_scores: { status: 'ok', candidate_version: 'quality-rules-v2-candidate-01', quality_score: 70, morphology_score: 72, levels_partial: true, explanations: [], evidence: { modules: { a: { title: 'A段推动', facts: ['<script>unsafe</script>'], reading: '检查持续推进', limitations: '范围有限' } } } } }
  const html = await renderToString(createSSRApp(module.exports.default, { row }))
  for (const text of ['v2 综合 · 候选', '覆盖不足', 'quality-rules-v2-candidate-01', '当前历史行情重算']) assert.ok(html.includes(text), text)
  assert.ok(!html.includes('<script>unsafe</script>'))
  assert.ok(html.includes('&lt;script&gt;unsafe'))
})
