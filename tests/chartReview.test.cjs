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
  assert.ok(loader.length && watcher.length)
  const listRequests = [], detailRequests = [], stops = []
  const context = {
    ref, reactive, computed,
    watch: (...args) => { stops.push(watch(...args)) },
    symbol: ref('CF0'), timeframe: ref('15m'),
    route: reactive({ query: { review: '1' } }),
    appStore: reactive({ reviewJumpFilters: filters }),
    api: {
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
  const compiled = ts.transpileModule(`${loader}\n${watcher}\nObject.assign(globalThis, { reviewRows, reviewLoading })`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  })
  vm.runInNewContext(compiled.outputText, context)
  return {
    context, listRequests, detailRequests,
    dispose: () => stops.forEach(stop => stop()),
    ids: () => Array.from(context.reviewRows.value, r => r.event_id),
    async navigate(symbol, id) {
      context.symbol.value = symbol
      context.route.query.review = id == null ? undefined : String(id)
      await nextTick()
    },
  }
}

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
