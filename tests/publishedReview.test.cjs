const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')
const vue = require('vue')
const { parse, compileScript } = require('@vue/compiler-sfc')
const { renderToString } = require('@vue/server-renderer')

function evaluate(file, mocks = {}, source) {
  const module = { exports: {} }
  const code = ts.transpileModule(source ?? fs.readFileSync(file, 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
  vm.runInNewContext(code, { module, exports: module.exports, console, setTimeout, clearTimeout, localStorage: { getItem: () => null, setItem() {} }, require: name => mocks[name] ?? require(name) })
  return module.exports
}
const presentation = evaluate('src/utils/reviewPresentation.ts')
const filters = evaluate('src/utils/reviewFilters.ts')
const quality = evaluate('src/utils/signalQuality.ts')
const tick = () => new Promise(setImmediate)
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const excursion = (r = null, precision = '', lower_r = null, upper_r = null) => ({ r, precision, lower_r, upper_r, amount: null, price: null, interval_start: null, interval_end: null })
const row = (id = 7057, changes = {}) => ({ event_id: id, result_version: `v${id}`, symbol: 'SM0', direction: 'down', level: 'fine', grade: 'B级', state: 'closed', exit_reason: 'target_1r', terminal_reason: null,
  warning_ts: '2026-10-29 10:00:00', entry_ts: '2026-10-29 10:05:00', exit_ts: '2026-10-29 10:10:00', plan: { entry: 5902, stop: 5923.6, reference_target: 5900, risk: 21.6, rr: 0.1 },
  scores: { old: 2.7, quality_v1: 61, quality_v2: null, morphology_v2: null }, score_status: 'missing_data', score_explanations: ['缺预警前K线'],
  risk_points: 23.6, gross_r: 1, net_r: 0.9, gross_amount: 100, net_amount: 90, bars_held: 2, wait_bars: 1, mfe_r: null, mae_r: null, mfe: excursion(), mae: excursion(), entry_price: 5900, exit_price: 5878.4,
  diagnostics: { vol_ratio: 1.23, overshoot: 0.12, b_a_speed: 0.4 }, coverage: [], ...changes })
const detail = (summary = row(), changes = {}) => ({ summary, profile: 'review-fixed-one-r-5m-v1', cost_version: 'cost-v1', multiplier: 5, entry_price: summary.entry_price, exit_price: summary.exit_price,
  cost: { settled: true, entry_slippage_ticks: 1, exit_slippage_ticks: 1, additional_roundtrip_fee_ticks: 0.5, additional_fee_amount: 10 }, original_event: {}, quality: null, review_scores: null, lifecycle: [], mfe: summary.mfe, mae: summary.mae, holding_count: 6, ...changes })
const response = (items = [], extra = {}) => ({ snapshot_id: 'snapshot', revision: 1, items, total: items.length, next_cursor: null, scored: 0, unscored: items.length, overview: { count: items.length, metrics: {}, counts: {} }, groups: {}, equity: [], ...extra })

function sfc(file, props = {}, api = {}, expose = []) {
  const { descriptor } = parse(fs.readFileSync(file, 'utf8'))
  const emitted = []
  const useDetail = evaluate('src/utils/useReviewDetail.ts', { vue, '../services/api': { api } })
  const mocks = { vue: { ...vue, watch() {}, onMounted() {}, onBeforeUnmount() {} }, 'vue-router': { useRoute: () => ({ query: {}, params: {} }), useRouter: () => ({}) },
    '@tauri-apps/api/core': { isTauri: () => false }, '../stores/klines': { useKlinesStore: () => ({ rows: [], load: async () => {} }) }, '../stores/symbols': { useSymbolsStore: () => ({ symbols: [], load: async () => {} }) },
    '../services/api': { api }, '../utils/useReviewDetail': useDetail, '../utils/reviewPresentation': presentation, '../utils/reviewFilters': filters, '../utils/signalQuality': quality }
  const module = { exports: {} }
  const source = descriptor.scriptSetup.content + `\nglobalThis.exposed = {${expose.join(',')}}`
  const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
  const context = { module, exports: module.exports, console, setTimeout, clearTimeout, defineProps: () => props, defineEmits: () => (...args) => emitted.push(args), require: name => mocks[name] || {} }
  vm.runInNewContext(code, context)
  return { ...context.exposed, emitted }
}
async function renderDrawer(props) {
  const { descriptor } = parse(fs.readFileSync('src/components/ReviewSignalDetailDrawer.vue', 'utf8'))
  const Pass = vue.defineComponent({ inheritAttrs: false, setup: (_, { slots }) => () => vue.h('div', slots.default?.()) })
  const source = compileScript(descriptor, { id: 'published-review', inlineTemplate: true }).content
  const component = evaluate('', { vue, 'naive-ui': new Proxy({}, { get: () => Pass }), '@vicons/tabler': {}, '../utils/reviewPresentation': presentation, '../utils/reviewFilters': filters,
    '../utils/signalQuality': quality, '../stores/review': { fmtR: v => v == null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}R` } }, source).default
  const app = vue.createSSRApp(component, props)
  app.config.warnHandler = () => {}
  return (await renderToString(app)).replace(/<[^>]*>/g, '').replace(/\s+/g, ' ')
}

test('published score selection never falls back between v2, v1 and the five-point original', () => {
  const s = row()
  assert.equal(presentation.publishedScore(s), null)
  assert.equal(presentation.publishedScore(s, 'quality_v1'), 61)
  assert.equal(presentation.publishedScore(s, 'old'), 2.7)
  assert.equal(presentation.publishedScore(row(1, { scores: { quality_v2: 0 } })), 0)
})
test('unknown and bounded excursions preserve uncertainty; real zero remains zero', () => {
  assert.equal(presentation.excursionText(null, null), '未知')
  assert.equal(presentation.excursionText(excursion(), 9), '未知')
  assert.equal(presentation.excursionText(excursion(0, 'bar_interval')), '+0.00R')
  assert.equal(presentation.excursionText(excursion(null, 'bounded', 0.2, 0.6)), '+0.20R ～ +0.60R（区间）')
  assert.equal(presentation.excursionText(excursion(null, 'bounded', -0.9, -0.1)), '-0.90R ～ -0.10R（区间）')
})
test('prices alone never imply settled costs; R and currency coverage are separate', () => {
  assert.notEqual(presentation.costExplanation(null).type, 'success')
  assert.notEqual(presentation.costExplanation(detail(row(), { cost: {} })).type, 'success')
  assert.equal(presentation.costExplanation(detail(row(), { multiplier: null })).title, 'R 已结算，金额覆盖不足')
  assert.equal(presentation.costExplanation(detail()).type, 'success')
  assert.notEqual(presentation.costExplanation(detail(row(1, { state: 'ambiguous' }))).type, 'success')
})
test('nonsettled states and legacy targets retain their actual meaning', () => {
  assert.equal(presentation.reviewState(row(1, { state: 'missing_data', exit_reason: null, terminal_reason: 'missing_data' })).text, '行情不足')
  assert.equal(presentation.reviewState(row(1, { exit_reason: 'target' })).text, '目标止盈')
  assert.equal(presentation.sameReview(row(), detail(row(7056))), false)
})
test('real drawer renders complete detail amounts and diagnostics, 15m bars, 5m records and excursion bounds', async () => {
  const summary = row(1, { gross_r: null, gross_amount: null, net_amount: null, diagnostics: {} })
  const full = row(1, { mfe: excursion(null, 'bounded', 0.2, 0.6), mae: excursion(), bars_held: 2 })
  const html = await renderDrawer({ summary, detail: detail(full) })
  for (const text of ['100 / 90 元', '触发量比（5m）：1.230', '持仓 2 根交易K（15分钟）', '持有记录 6 条（5分钟）', '+0.20R ～ +0.60R（区间）', 'MAE 未知', 'B/A速度比0.4', '计划入场5902.0', '模拟成交价5900.0']) assert.ok(html.includes(text), text)
  assert.ok(!html.includes('MFE +0.00R'))
})
test('missing detail, stale detail, unfilled signals and failures cannot invent settlement or fills', async () => {
  const pending = row(1, { state: 'pending', entry_ts: null, exit_ts: null, entry_price: null, exit_price: null })
  const html = await renderDrawer({ summary: pending, detail: null, error: '查询失败' })
  assert.ok(html.includes('查询失败'))
  assert.ok(html.includes('尚未取得成本明细'))
  assert.ok(html.includes('模拟成交价—'))
  assert.ok(!html.includes('结算完成'))
  const stale = await renderDrawer({ summary: row(1), detail: detail(row(2, { net_amount: 9999 })) })
  assert.ok(stale.includes('详情与当前信号版本不一致'))
  assert.ok(!stale.includes('9999'))
})
test('expanding an unselected row requests its own versioned detail', () => {
  const props = { signals: [row()], selectedId: 7056, detail: null }
  const h = sfc('src/components/ReviewSignalPool.vue', props, {}, ['toggleDetail', 'expandedId', 'getScore'])
  h.toggleDetail(props.signals[0], { stopPropagation() {} })
  assert.equal(h.emitted[0][0], 'select-signal')
  assert.equal(h.emitted[0][1].result_version, 'v7057')
  assert.equal(h.getScore(props.signals[0]), null)
})
test('selection clears old details and rejects reverse-order responses and stale loading changes', async () => {
  const requests = []
  const { useReviewDetail } = evaluate('src/utils/useReviewDetail.ts', { vue, '../services/api': { api: { reviewDetail: (id, version) => { const p = deferred(); requests.push({ id, version, ...p }); return p.promise } } } })
  const h = useReviewDetail()
  const first = h.load(1, 'v1')
  requests[0].resolve(detail(row(1))); await first
  const older = h.load(2, 'v2')
  assert.equal(h.detail.value, null)
  const latest = h.load(3, 'v3')
  requests[1].reject(Error('late old error')); await older
  assert.equal(h.loading.value, true); assert.equal(h.error.value, '')
  requests[2].resolve(detail(row(3))); await latest
  assert.equal(h.detail.value.summary.event_id, 3)
  const slow = h.load(4, 'v4'), fast = h.load(5, 'v5')
  requests[4].resolve(detail(row(5))); await fast
  requests[3].resolve(detail(row(4))); await slow
  assert.equal(h.detail.value.summary.event_id, 5)
})
test('wrong event/version fails visibly; an event-only link requests its published head without a fake profile', async () => {
  const calls = []
  const { useReviewDetail } = evaluate('src/utils/useReviewDetail.ts', { vue, '../services/api': { api: { reviewDetail: async (id, version) => { calls.push({ id, version }); return detail(row(2)) } } } })
  const h = useReviewDetail()
  await h.load(1, 'v1'); assert.equal(h.detail.value, null); assert.match(h.error.value, /不一致/)
  await h.load(2); assert.equal(calls[1].version, undefined); assert.equal(h.detail.value.summary.result_version, 'v2')
})

function storeHarness() {
  const queries = [], pages = []
  const api = { reviewMetadata: async () => ({ symbols: ['SM0'], jobs: [] }), reviewQuery: q => { const p = deferred(); queries.push({ q, ...p }); return p.promise }, reviewItems: (id, req) => { const p = deferred(); pages.push({ id, req, ...p }); return p.promise } }
  const options = evaluate('src/stores/publishedReview.ts', { pinia: { defineStore: (_, o) => o }, '../services/api': { api }, '../utils/reviewFilters': filters, '../utils/reviewPresentation': presentation }).usePublishedReviewStore
  const store = options.state(); for (const [key, action] of Object.entries(options.actions)) store[key] = action.bind(store)
  return { store, queries, pages }
}
test('new published list keeps an empty generation empty and never invokes legacy APIs', async () => {
  const h = storeHarness(), pending = h.store.loadReviewPage(); await tick()
  h.queries[0].resolve(response()); await pending
  assert.equal(h.store.browserRows.length, 0); assert.equal(h.store.currentSnapshotId, 'snapshot'); assert.equal(h.store.browserError, '')
})
test('published list pagination preserves snapshot and order; stale filter responses cannot overwrite it', async () => {
  const h = storeHarness(), first = h.store.loadReviewPage(); await tick()
  h.queries[0].resolve(response([row(1)], { total: 161, unscored: 161 })); await first
  h.store.browserPage = 2
  const second = h.store.loadReviewPage(true); await tick()
  assert.equal(h.pages[0].id, 'snapshot'); assert.equal(h.pages[0].req.cursor, 80)
  h.pages[0].resolve(response([row(81)], { total: 161 })); await second
  assert.equal(h.store.browserRows[0].event_id, 81)
  const stale = h.store.loadReviewPage(); await tick()
  h.store.updateBrowserFilterDraft({ qualityMin: 80 })
  const latest = h.store.loadReviewPage(); await tick()
  h.queries[2].resolve(response([row(5)], { snapshot_id: 'new' })); await latest
  h.queries[1].resolve(response([row(9)])); await stale
  assert.equal(h.store.browserRows[0].event_id, 5); assert.equal(h.store.currentSnapshotId, 'new')
})
test('expired snapshots show an error and do not switch to unfiltered or legacy rows', async () => {
  const h = storeHarness(), pending = h.store.loadReviewPage(); await tick()
  h.queries[0].resolve(response([row()])); await pending
  h.store.browserPage = 2
  const next = h.store.loadReviewPage(true); await tick()
  h.pages[0].reject(Error('SNAPSHOT_EXPIRED')); await next
  assert.match(h.store.browserError, /SNAPSHOT_EXPIRED/); assert.equal(h.store.browserRows.length, 0); assert.equal(h.queries.length, 1)
})
test('refreshing after the population shrinks moves to a valid page without fetching beyond the snapshot', async () => {
  const h = storeHarness()
  h.store.browserPage = 3
  const pending = h.store.loadReviewPage(); await tick()
  h.queries[0].resolve(response([row(1)])); await pending
  assert.equal(h.store.browserPage, 1)
  assert.equal(h.store.browserRows[0].event_id, 1)
  assert.equal(h.pages.length, 0)
})
test('query transport preserves score scale, outcome and trigger-time semantics', () => {
  const q = presentation.publishedQuery({ qualityMetric: 'old', qualityMin: 2.5, outcome: 'win', timeField: 'trigger', sortBy: 'score_asc' }, 80)
  assert.equal(q.filters.score_metric, 'old'); assert.equal(q.filters.score_min, 2.5); assert.equal(q.filters.outcomes[0], 'win')
  assert.equal(q.filters.time_field, 'entry'); assert.equal(q.sort_by, 'score'); assert.equal(q.descending, false)
})
test('chart opens matching immutable details and replaces reused-window context without legacy fallbacks', async () => {
  const requests = [], pools = []
  const api = { reviewDetail: (id, version) => { const p = deferred(); requests.push({ id, version, ...p }); return p.promise }, reviewItems: (id, req) => { const p = deferred(); pools.push({ id, req, ...p }); return p.promise } }
  const h = sfc('src/views/ReviewChartView.vue', {}, api, ['applyContext', 'activeDetail', 'signalList', 'currentSnapshotId', 'currentEventId'])
  const a = h.applyContext({ symbol: 'SM0', eventId: 1, version: 'v1', snapshotId: 'a', query: { page_size: 40 }, cursor: 80 })
  const b = h.applyContext({ symbol: 'SM0', eventId: 2, version: 'v2', snapshotId: 'b', query: { page_size: 40 }, cursor: 120 })
  assert.equal(pools[1].req.cursor, 120)
  pools[1].resolve(response([row(2)], { snapshot_id: 'b', total: 161 })); requests[1].resolve(detail(row(2))); await b
  pools[0].resolve(response([row(1)], { snapshot_id: 'a' })); requests[0].resolve(detail(row(1))); await a
  assert.equal(h.currentSnapshotId.value, 'b'); assert.equal(h.currentEventId.value, 2); assert.equal(h.activeDetail.value.summary.event_id, 2); assert.equal(h.signalList.value[0].event_id, 2)
})
test('chart automatically recovers from expired snapshots on pagination by requesting a new snapshot at requested offset', async () => {
  const requests = [], pools = [], queries = []
  const api = {
    reviewDetail: (id, version) => { const p = deferred(); requests.push({ id, version, ...p }); return p.promise },
    reviewItems: (id, req) => { const p = deferred(); pools.push({ id, req, ...p }); return p.promise },
    reviewQuery: q => { const p = deferred(); queries.push({ q, ...p }); return p.promise }
  }
  const h = sfc('src/views/ReviewChartView.vue', {}, api, ['applyContext', 'changePoolPage', 'signalList', 'currentSnapshotId', 'cursor', 'total', 'poolError'])
  const a = h.applyContext({ symbol: 'SM0', eventId: 1, version: 'v1', snapshotId: 'snap-old', query: { page_size: 120 }, cursor: 0 })
  pools[0].resolve(response([row(1)], { snapshot_id: 'snap-old', total: 200 })); requests[0].resolve(detail(row(1))); await a
  assert.equal(h.currentSnapshotId.value, 'snap-old')

  const pageNav = h.changePoolPage(120); await tick()
  assert.equal(pools[1].id, 'snap-old'); assert.equal(pools[1].req.cursor, 120)
  pools[1].reject(new Error('SNAPSHOT_EXPIRED: query snapshot expired or unavailable'))
  await tick()

  assert.equal(queries.length, 1)
  queries[0].resolve(response([row(1)], { snapshot_id: 'snap-fresh', total: 200 }))
  await tick()

  assert.equal(pools.length, 3)
  assert.equal(pools[2].id, 'snap-fresh'); assert.equal(pools[2].req.cursor, 120)
  pools[2].resolve(response([row(121)], { snapshot_id: 'snap-fresh', total: 200 }))
  await tick()
  if (requests[1]) requests[1].resolve(detail(row(121)))
  await pageNav

  assert.equal(h.currentSnapshotId.value, 'snap-fresh')
  assert.equal(h.signalList.value[0].event_id, 121)
  assert.equal(h.cursor.value, 120)
  assert.equal(h.poolError.value, '')
})

