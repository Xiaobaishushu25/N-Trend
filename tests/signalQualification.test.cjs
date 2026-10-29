const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')

function harness() {
  class Element {
    constructor() { this.textContent = ''; this.value = ''; this.checked = false; this.children = []; this.attributes = {}; this.scrollWidth = 1000 }
    setAttribute(k, v) { this.attributes[k] = v }
    append(...children) { this.children.push(...children) }
    replaceChildren(...children) { this.children = children }
    click() {}
  }
  const nodes = new Map(), get = id => nodes.get(id) || (nodes.set(id, new Element()), nodes.get(id))
  const reasons = ['a', 'b', 'trend', 'space', 'warning', 'scale'].map(value => Object.assign(new Element(), { value }))
  const path = { mean_overlap: .7, mean_wick_fraction: .2, close_net_atr: .5, close_travel_atr: 1, second_half_range_ratio: null }
  const makeCase = id => ({ case_id: id, input_hash: id + '-hash', direction: 'up', warning_index: 2,
    bars: [[0, 1, 0, .8, .3], [.8, 1.1, .7, 1, .5], [1, 1, .7, .9, .7]], points: [[0, 0], [1, 1.1], [2, .7]], entry: 1, stop: .6, target: 1.1, rr: .25,
    diagnostics: { interpretation: ['推进需要结合贡献根数；不自动判断合格。'], a: { bars: 2, interior_bars: 0, amplitude_atr: 1.1, directional_body: { largest_share: .8, largest_two_share: 1, effective_bars: 1.5 }, closing_records: { largest_share: 1 }, legacy_process_window: 'full_fallback', s1_opposing_wick_fraction: .2 }, b: { bars_including_warning_if_s2: 1, bars_without_warning: 0, outside_s1_before_warning_atr: 0, close_outside_s1_before_warning_atr: 0, first_range_exit_bar: null, range_exit_bars: 0, close_exit_bars: 0, path_without_warning: path }, background: path } })
  const label = { pair_id: 'old-pair', left_case_id: 'old-left', right_case_id: 'old-right', left_input_hash: 'old-left-hash', right_input_hash: 'old-right-hash', morphology: 'left', opportunity: 'uncertain', reasons: ['a'], note: '两边都不好，左边只是相对好' }
  const task = { task_hash: 'task', version: 'qualification-evidence-v1', display_version: 'qualification-single-v1', base_manifest_hash: 'base', base_display_version: 'blind-quality-v1', legacy_labels: [label],
    new_queue: ['new-1', 'new-2'], revisit_queue: ['old-left', 'old-right'], cases: Object.fromEntries(['new-1', 'new-2', 'old-left', 'old-right'].map(id => [id, makeCase(id)])),
    context: Object.fromEntries(['new-1', 'new-2', 'old-left', 'old-right'].map(id => [id, { pair_id: 'old-pair', side: 'left' }])), pair_identities: [label] }
  get('payload').textContent = JSON.stringify(task); get('queue').value = 'new'; get('scale').value = 'shared'
  const storage = new Map(); let downloaded
  const document = { getElementById: get, createElementNS: () => new Element(), createElement: () => new Element(), querySelectorAll: () => reasons }
  const source = fs.readFileSync('crates/n-core/src/quality/qualification-review.html', 'utf8').match(/<script>\s*([\s\S]*?)<\/script>/)[1]
  vm.runInNewContext(source, { document, localStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) }, Blob, URL: { createObjectURL: blob => { downloaded = blob; return 'blob:qualification' }, revokeObjectURL() {} }, setTimeout: f => f() })
  return { get, task, reasons, download: async () => { get('download').onclick(); return JSON.parse(await downloaded.text()) },
    import: async bundle => { get('file').files = [{ text: async () => JSON.stringify(bundle) }]; await get('file').onchange() } }
}
function fill(h) { h.get('morphology').value = 'qualified'; h.get('opportunity').value = 'marginal'; h.get('pattern').value = 'pullback_n'; h.get('note').value = '形态合格，但综合条件勉强'; h.reasons[0].checked = true }

test('absolute judgments remain independent of relative legacy choices and export both identities', async () => {
  const h = harness(), $ = h.get
  assert.equal($('morphology').value, '')
  assert.equal($('assistant').open, false)
  fill(h); $('save').onclick()
  const b = await h.download()
  assert.equal(b.labels[0].note, h.task.legacy_labels[0].note)
  assert.equal(b.labels[0].morphology, 'left')
  assert.equal(b.case_assessments[0].morphology_qualification, 'qualified')
  assert.equal(b.case_assessments[0].opportunity_qualification, 'marginal')
  assert.equal(b.case_assessments[0].case_id, 'new-1')
  assert.equal(b.case_assessments[0].assistant_reference.viewed, false)
  assert.equal(b.qualification_task_hash, 'task')
})
test('same ATR pixel scale, space toggle and navigation preserve unsaved forms', async () => {
  const h = harness(), $ = h.get
  fill(h)
  assert.equal($('chart').children[0].attributes['data-pixels-per-atr'], 40)
  $('space').onclick(); assert.equal($('note').value, '形态合格，但综合条件勉强')
  assert.ok($('space-text').textContent.includes('0.25R'))
  $('next').onclick(); assert.equal($('chart').children[0].attributes['data-pixels-per-atr'], 40)
  assert.equal($('note').value, '')
  $('prev').onclick(); assert.equal($('note').value, '形态合格，但综合条件勉强')
  assert.equal((await h.download()).case_drafts[0].note, '形态合格，但综合条件勉强')
  assert.equal((await h.download()).case_assessments.length, 0)
})
test('first reference opening freezes the original absolute judgment and navigation closes it', async () => {
  const h = harness(), $ = h.get
  fill(h); $('assistant').open = true; $('assistant').ontoggle()
  $('morphology').value = 'marginal'; $('note').value = '看完解读后仍有分歧'; $('save').onclick()
  $('assistant').open = false; $('assistant').open = true; $('assistant').ontoggle()
  const l = (await h.download()).case_assessments[0]
  assert.equal(l.assistant_reference.before.morphology_qualification, 'qualified')
  assert.equal(l.assistant_reference.before.note, '形态合格，但综合条件勉强')
  assert.equal(l.note, '看完解读后仍有分歧')
  $('next').onclick(); assert.equal($('assistant').open, false)
})
test('incomplete decisions stay drafts; unknown is an explicit complete judgment', async () => {
  const h = harness(), $ = h.get
  $('note').value = '暂时只写说明'; $('save').onclick()
  let b = await h.download(); assert.equal(b.case_assessments.length, 0); assert.equal(b.case_drafts[0].note, '暂时只写说明')
  $('morphology').value = $('opportunity').value = $('pattern').value = 'uncertain'; $('save').onclick()
  b = await h.download(); assert.equal(b.case_assessments[0].morphology_qualification, 'uncertain')
})
test('legacy import preserves absolute work; tampered import is atomic; new bundles round-trip', async () => {
  const h = harness(), $ = h.get; fill(h); $('save').onclick()
  const bundle = await h.download()
  const old = { manifest_hash: 'base', display_version: 'blind-quality-v1', labels: [{ ...h.task.legacy_labels[0], note: '<script>原始说明</script>' }] }
  await h.import(old)
  assert.equal((await h.download()).case_assessments.length, 1)
  assert.equal((await h.download()).labels[0].note, old.labels[0].note)
  const bad = structuredClone(bundle); bad.case_assessments[0].input_hash = 'wrong'; bad.labels[0].note = '不得覆盖'
  await h.import(bad)
  assert.ok($('message').textContent.includes('哈希'))
  assert.equal((await h.download()).labels[0].note, old.labels[0].note)
  const other = harness(); await other.import(bundle)
  assert.equal((await other.download()).case_assessments[0].note, bundle.case_assessments[0].note)
})
test('importing an older unseen snapshot cannot erase the first reference exposure', async () => {
  const h = harness(), $ = h.get; fill(h); $('save').onclick()
  const unseen = await h.download()
  $('assistant').open = true; $('assistant').ontoggle()
  await h.import(unseen)
  const again = await h.download()
  assert.equal(again.case_assessments[0].assistant_reference.viewed, true)
  assert.equal(again.case_assessments[0].assistant_reference.before.morphology_qualification, 'qualified')
})
