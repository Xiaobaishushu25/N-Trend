const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')
const { ref, reactive, computed, watch, nextTick } = require('vue')

const flush = () => new Promise(setImmediate)
function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const row = (event_id, symbol = 'CF0') => ({ event_id, symbol, entry_score: 0.8 })

// Run the view's real list loader and reactive route watcher against delayed API responses.
function reviewHarness(filters = null) {
  const source = fs.readFileSync('src/views/ChartView.vue', 'utf8')
  const loader = source.slice(source.indexOf('/** 复盘跳转模式：'), source.indexOf('\nfunction selectReviewSignal'))
  const watcher = source.slice(source.indexOf('// 复盘模式：首次进入'), source.indexOf('// 复盘模式：当前行变化'))
  const pager = source.slice(source.indexOf('async function changeReviewPage('), source.indexOf('/** 返回导航：'))
  assert.ok(loader.length && watcher.length)
  const listRequests = [], pageRequests = [], detailRequests = [], pageSelections = [], stops = []
  const context = {
    ref, reactive, computed,
    watch: (...args) => { stops.push(watch(...args)) },
    symbol: ref('CF0'), timeframe: ref('15m'),
    route: reactive({ query: { review: '1' } }),
    appStore: reactive({ reviewJumpFilters: filters }),
    selectReviewSignal: row => pageSelections.push(row.event_id),
    notify: { error() {} },
    api: {
      getReviewPage: (limit, offset, filters) => {
        const request = { limit, offset, filters, ...deferred() }
        pageRequests.push(request)
        return request.promise
      },
      getRecentOutcomes: (limit, filters) => {
        const request = { limit, filters, ...deferred() }
        listRequests.push(request)
        return request.promise
      },
      getReviewSignal: (id) => {
        const request = { id, ...deferred() }
        detailRequests.push(request)
        return request.promise
      },
    },
  }
  context.chartReviewFilters = computed(() => context.appStore.reviewJumpFilters || {})
  const compiled = ts.transpileModule(`${loader}\n${watcher}\n${pager}\nObject.assign(globalThis, { reviewRows, reviewLoading, changeReviewPage })`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  })
  vm.runInNewContext(compiled.outputText, context)
  return {
    context, listRequests, pageRequests, detailRequests, pageSelections,
    dispose: () => stops.forEach(stop => stop()),
    ids: () => Array.from(context.reviewRows.value, r => r.event_id),
    async navigate(symbol, id) {
      context.symbol.value = symbol
      context.route.query.review = id == null ? undefined : String(id)
      await nextTick()
    },
  }
}

test('a score-review handoff preserves the selected page and server order', async (t) => {
  const h = reviewHarness({ qualityMetric: 'quality_v2', qualityMin: 70, sortBy: 'score_desc', pageOffset: 160, pageSize: 40 })
  t.after(h.dispose)
  assert.equal(h.listRequests.length, 0)
  assert.equal(h.pageRequests[0].offset, 160)
  assert.equal(h.pageRequests[0].limit, 40)
  assert.equal(h.pageRequests[0].filters.qualityMin, 70)
  h.pageRequests[0].resolve({ items: [row(9), row(1), row(7)], total: 500, offset: 160, limit: 40 })
  await flush()
  assert.deepEqual(h.ids(), [9, 1, 7])
})

test('explicit score filters do not inject an out-of-filter active event into the results', async (t) => {
  const h = reviewHarness({ qualityMetric: 'quality_v2', qualityMin: 90, pageOffset: 0 })
  t.after(h.dispose)
  h.pageRequests[0].resolve({ items: [row(2)], total: 1, offset: 0, limit: 80 })
  await flush()
  assert.deepEqual(h.ids(), [2])
  assert.equal(h.detailRequests.length, 0)
})

test('a stale score-review page cannot replace a newly filtered page', async (t) => {
  const h = reviewHarness({ qualityMin: 70, pageOffset: 0 })
  t.after(h.dispose)
  h.context.appStore.reviewJumpFilters = { qualityMin: 80, pageOffset: 0 }
  await nextTick()
  h.pageRequests[1].resolve({ items: [row(3)], total: 1, offset: 0, limit: 80 })
  await flush()
  h.pageRequests[0].resolve({ items: [row(1)], total: 1, offset: 0, limit: 80 })
  await flush()
  assert.deepEqual(h.ids(), [3])
})

test('a late next-page response cannot undo a newer chart filter', async (t) => {
  const h = reviewHarness({ qualityMin: 70, pageOffset: 0 })
  t.after(h.dispose)
  h.pageRequests[0].resolve({ items: [row(1), row(2)], total: 200, offset: 0, limit: 80 })
  await flush()
  const paging = h.context.changeReviewPage(1)
  assert.equal(h.pageRequests[1].offset, 80)
  h.context.appStore.reviewJumpFilters = { qualityMin: 90, pageOffset: 0 }
  await nextTick()
  h.pageRequests[2].resolve({ items: [row(3)], total: 1, offset: 0, limit: 80 })
  await flush()
  h.pageRequests[1].resolve({ items: [row(9)], total: 200, offset: 80, limit: 80 })
  await paging
  assert.deepEqual(h.ids(), [3])
  assert.equal(h.context.appStore.reviewJumpFilters.qualityMin, 90)
  assert.deepEqual(h.pageSelections, [])
})

test('reusing the history chart for another symbol refreshes the right-hand list', async (t) => {
  const h = reviewHarness()
  t.after(h.dispose)
  h.listRequests[0].resolve([row(1)])
  await flush()
  await h.navigate('SR0', 2)
  assert.equal(h.listRequests.length, 2)
  assert.equal(h.listRequests[1].filters.symbol, 'SR0')
  assert.deepEqual(h.ids(), [], 'old-symbol rows must not remain navigable during loading')
  h.listRequests[1].resolve([row(2, 'SR0')])
  await flush()
  assert.deepEqual(h.ids(), [2])
})

test('switching to an existing event of the same symbol reuses the list', async (t) => {
  const h = reviewHarness()
  t.after(h.dispose)
  h.listRequests[0].resolve([row(1), row(2)])
  await flush()
  await h.navigate('CF0', 2)
  assert.equal(h.listRequests.length, 1)
  assert.equal(h.detailRequests.length, 0)
  assert.deepEqual(h.ids(), [1, 2])
})

test('an older event absent from a cached list is added as an outcome row with user data', async (t) => {
  const h = reviewHarness()
  t.after(h.dispose)
  h.listRequests[0].resolve([row(1)])
  await flush()
  await h.navigate('CF0', 99)
  assert.equal(h.detailRequests.length, 1)
  assert.equal(h.detailRequests[0].id, 99)
  h.detailRequests[0].resolve({
    event: { id: 99 }, outcome: row(99),
    annotations: [{ id: 5, content: 'note' }], opened: true,
  })
  await flush()
  assert.deepEqual(h.ids(), [99, 1])
  const active = h.context.reviewRows.value[0]
  assert.equal(active.entry_score, 0.8)
  assert.equal(active.annotations[0].content, 'note')
  assert.equal(active.opened, true)
})

test('an initial event outside the recent limit uses the detail outcome, not its wrapper', async (t) => {
  const h = reviewHarness()
  t.after(h.dispose)
  h.listRequests[0].resolve([row(2)])
  await flush()
  h.detailRequests[0].resolve({ event: { id: 1 }, outcome: row(1), annotations: [], opened: null })
  await flush()
  assert.deepEqual(h.ids(), [1, 2])
  assert.equal(h.context.reviewRows.value[0].entry_score, 0.8)
})

test('returning to a cached event invalidates a pending detail lookup', async (t) => {
  const h = reviewHarness()
  t.after(h.dispose)
  h.listRequests[0].resolve([row(1), row(2)])
  await flush()
  await h.navigate('CF0', 99)
  assert.equal(h.detailRequests.length, 1)
  await h.navigate('CF0', 2)
  assert.equal(h.context.reviewLoading.value, false)
  h.detailRequests[0].resolve({ event: { id: 99 }, outcome: row(99), annotations: [], opened: null })
  await flush()
  assert.deepEqual(h.ids(), [1, 2])
  assert.equal(h.listRequests.length, 1)
})

test('explicit review filters preserve a cross-symbol list and refresh when filters change', async (t) => {
  const h = reviewHarness({ direction: 'up' })
  t.after(h.dispose)
  h.listRequests[0].resolve([row(1), row(2, 'SR0')])
  await flush()
  await h.navigate('SR0', 2)
  assert.equal(h.listRequests.length, 1)
  assert.equal(h.listRequests[0].filters.direction, 'up')
  h.context.appStore.reviewJumpFilters = { direction: 'down' }
  await nextTick()
  assert.equal(h.listRequests.length, 2)
  h.listRequests[1].resolve([row(2, 'SR0')])
  await flush()
  assert.deepEqual(h.ids(), [2])
})

test('a late old-symbol response cannot overwrite the latest event list', async (t) => {
  const h = reviewHarness()
  t.after(h.dispose)
  await h.navigate('SR0', 2)
  h.listRequests[1].resolve([row(2, 'SR0')])
  await flush()
  h.listRequests[0].resolve([row(1)])
  await flush()
  assert.deepEqual(h.ids(), [2])
  assert.equal(h.context.reviewLoading.value, false)
})

test('exiting review invalidates pending work and reentering reloads the current symbol', async (t) => {
  const h = reviewHarness()
  t.after(h.dispose)
  await h.navigate('CF0', null)
  assert.equal(h.context.reviewLoading.value, false)
  h.listRequests[0].resolve([row(1)])
  await flush()
  assert.deepEqual(h.ids(), [])
  await h.navigate('SR0', 2)
  h.listRequests[1].resolve([row(2, 'SR0')])
  await flush()
  assert.deepEqual(h.ids(), [2])
})
