const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')
const { parse, compileScript } = require('@vue/compiler-sfc')
const { createSSRApp } = require('vue')
const { renderToString } = require('@vue/server-renderer')

function evaluate(source, requireMock = require) {
  const module = { exports: {} }
  const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
  vm.runInNewContext(code, { module, exports: module.exports, require: requireMock, console })
  return module.exports
}
const utils = evaluate(fs.readFileSync('src/utils/signalQuality.ts', 'utf8'))
const quality = { status: 'ok', quality_score: 67.1, morphology_score: 72, trend_score: 60, a_score: 80, b_score: 50, space_score: 20, origin: 'historical_recompute', rules_version: 'quality-rules-v1', features: { a_stall_bars: 4 }, contributions: [{ name: 'A段推动', score: 80, weight: .45, contribution: 36 }], explanations: ['A段末端停滞', '<script>bad</script>'], parameter_hash: 'param', input_hash: 'input' }

test('display sorting puts unknowns last, keeps ties, and rejects NaN or insufficient data', () => {
  assert.equal(utils.qualityNumber({ status: 'missing_data', quality_score: 99 }), null)
  assert.equal(utils.qualityNumber({ status: 'ok', quality_score: NaN }), null)
  assert.equal(utils.compareQuality(null, quality), 1)
  assert.equal(utils.compareQuality(quality, null), -1)
  assert.equal(utils.compareQuality(quality, quality), 0)
  assert.equal(utils.qualityText(null), '—')
})

async function renderQuality(props, enabled = true) {
  const { descriptor } = parse(fs.readFileSync('src/components/SignalQuality.vue', 'utf8'))
  const script = compileScript(descriptor, { id: 'quality-test', inlineTemplate: true })
  const component = evaluate(script.content, name => name === 'vue' ? require('vue') : name.includes('stores/signalQuality') ? { useSignalQualityStore: () => ({ enabled }) } : utils).default
  return renderToString(createSSRApp(component, props))
}
test('quality card presents both scales, causal explanations and safely escapes text', async () => {
  const html = await renderQuality({ quality, oldScore: 3.5, rr: .31 })
  for (const text of ['旧入场 3.50', '72.0', '67.1', '0.31R', '历史快照重算', '最长停滞根数']) assert.ok(html.includes(text), text)
  assert.ok(!html.includes('<script>bad</script>'))
  assert.ok(html.includes('&lt;script&gt;'))
})
test('missing and insufficient scores degrade explicitly; display switch hides the new card', async () => {
  assert.ok((await renderQuality({ oldScore: 3.1 })).includes('新评分未计算'))
  assert.ok((await renderQuality({ oldScore: 3.1, quality: { ...quality, status: 'missing_data', quality_score: null } })).includes('数据不足'))
  assert.ok(!(await renderQuality({ quality, oldScore: 3.1 }, false)).includes('综合'))
})

test('scan sidecar enriches display without promoting old notification thresholds', async () => {
  const notifications = []
  const exported = evaluate(fs.readFileSync('src/stores/scans.ts', 'utf8'), name => {
    if (name === 'pinia') return { defineStore: (_, options) => options }
    if (name.includes('services/api')) return { api: { getActiveSingleBars: async () => [] } }
    if (name.includes('settings')) return { useSettingsStore: () => ({ clientSettings: { inAppNotificationEnabled: true, notificationMinScore: 3.5 } }) }
    if (name.includes('symbols')) return { useSymbolsStore: () => ({ symbols: [] }) }
    if (name.includes('notify')) return { notify: { signal: x => notifications.push(['signal', x.score]), recordSignal: x => notifications.push(['record', x.score]) } }
    return {}
  })
  const options = exported.useScansStore, state = options.state()
  const event = { id: 1, symbol: 'L0', state: 'pending', direction: 'up', entry_score: 3.1 }
  options.actions.applyScanResult.call(state, { signals: [event], new_warnings: [event], newly_triggered: [], quality_by_event: { 1: quality } })
  assert.equal(state.latestSignals[0].quality.quality_score, 67.1)
  assert.equal(state.latest.signals[0].entry_score, 3.1)
  assert.deepEqual(notifications, [['record', 3.1]])
  await options.actions.refreshLatestSignals.call(state)
  assert.equal(state.latestSignals[0].quality.quality_score, 67.1)
})

test('offline blind review preserves unsaved choices when revealing space and exports bound labels', async () => {
  class Element {
    constructor() { this.textContent = ''; this.value = ''; this.checked = false; this.children = []; this.scrollWidth = 1000 }
    setAttribute() {}
    append(...nodes) { this.children.push(...nodes) }
    replaceChildren(...nodes) { this.children = nodes }
    click() {}
  }
  const nodes = new Map(), get = id => nodes.get(id) || (nodes.set(id, new Element()), nodes.get(id))
  const inputs = ['trend', 'a', 'b', 'space'].map(value => Object.assign(new Element(), { value }))
  const makeCase = id => ({ case_id: id, input_hash: id + '-hash', direction: 'up', bars: [[0, 1, -.1, .8, .3], [.8, 1.1, .7, 1, .5]], points: [[0, 0], [1, 1], [1, .7]], entry: .9, stop: .6, target: 1, rr: .33 })
  get('payload').textContent = JSON.stringify({ manifest_hash: 'manifest', display_version: 'blind-quality-v1', pairs: [{ pair_id: 'pair', left: makeCase('left'), right: makeCase('right') }] })
  const storage = new Map(); let downloaded
  const document = { getElementById: get, createElementNS: () => new Element(), createElement: () => new Element(), querySelectorAll: selector => selector === 'input:checked' ? inputs.filter(n => n.checked) : inputs }
  const source = fs.readFileSync('crates/n-core/src/quality/blind-review.html', 'utf8').match(/<script>\s*([\s\S]*?)<\/script>/)[1]
  vm.runInNewContext(source, { document, localStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value) }, Blob, URL: { createObjectURL: blob => { downloaded = blob; return 'blob:labels' }, revokeObjectURL() {} }, setTimeout: callback => callback() })
  get('morphology').value = 'left'; get('opportunity').value = 'right'; get('note').value = '连续推进'; inputs[1].checked = true
  get('reveal').onclick()
  assert.equal(get('morphology').value, 'left')
  assert.equal(get('note').value, '连续推进')
  get('save').onclick(); get('download').onclick()
  const result = JSON.parse(await downloaded.text())
  assert.equal(result.manifest_hash, 'manifest')
  assert.equal(result.labels[0].left_input_hash, 'left-hash')
  assert.deepEqual(result.labels[0].reasons, ['a'])
})

function referenceHarness() {
  class Element {
    constructor() { this.textContent = ''; this.value = ''; this.checked = false; this.open = false; this.children = []; this.scrollWidth = 1000 }
    setAttribute() {} append(...nodes) { this.children.push(...nodes) } replaceChildren(...nodes) { this.children = nodes } click() {}
  }
  const nodes = new Map(), get = id => nodes.get(id) || (nodes.set(id, new Element()), nodes.get(id))
  const inputs = ['trend', 'a', 'b', 'space'].map(value => Object.assign(new Element(), { value }))
  const makeCase = id => ({ case_id: id, input_hash: id + '-hash', direction: 'up', bars: [[0, 1, -.1, .8, .3], [.8, 1.1, .7, 1, .5]], points: [[0, 0], [1, 1], [1, .7]], entry: .9, stop: .6, target: 1, rr: .33 })
  const pairs = [1, 2].map(n => ({ pair_id: 'pair-' + n, left: makeCase('left-' + n), right: makeCase('right-' + n) }))
  const data = { manifest_hash: 'manifest', display_version: 'blind-quality-v1', pairs }
  const readings = { trend: ['趋势背景'], push: ['小K有进展'], pullback: ['回到均线'], space: ['结构空间'] }
  const notes = { base_manifest_hash: 'manifest', version: 'notes-v1', notes_hash: 'notes-hash', pairs: Object.fromEntries(pairs.map(p => [p.pair_id, { review_status: 'structure_draft', left: readings, right: readings, comparison: ['单独A段偏左；整体有取舍。'], uncertainties: ['尚未逐图复核。'] }])) }
  get('payload').textContent = JSON.stringify(data); get('assistant-payload').textContent = JSON.stringify(notes)
  const storage = new Map(); let downloaded
  const document = { getElementById: get, createElementNS: () => new Element(), createElement: () => new Element(), querySelectorAll: selector => selector === 'input:checked' ? inputs.filter(n => n.checked) : inputs }
  const source = fs.readFileSync('crates/n-core/src/quality/blind-review.html', 'utf8').match(/<script>\s*([\s\S]*?)<\/script>/)[1]
  vm.runInNewContext(source, { document, localStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value) }, Blob, URL: { createObjectURL: blob => { downloaded = blob; return 'blob:labels' }, revokeObjectURL() {} }, setTimeout: callback => callback() })
  return { get, pairs, inputs, download: async () => { get('download').onclick(); return JSON.parse(await downloaded.text()) } }
}

test('reference starts hidden and preserves first independent choices, prose and ever-viewed state', async () => {
  const h = referenceHarness(), $ = h.get
  assert.equal($('assistant-panel').open, false)
  assert.equal($('assistant-body').children.length, 0)
  $('morphology').value = 'left'; $('opportunity').value = 'tie'; $('note').value = '先独立写：连续推进'; h.inputs[1].checked = true
  $('assistant-panel').open = true; $('assistant-panel').ontoggle()
  assert.ok($('assistant-body').children.length > 0)
  assert.equal($('note').value, '先独立写：连续推进')
  $('morphology').value = 'right'; $('note').value = '对照后补充：更看重趋势'
  $('assistant-panel').open = false; $('assistant-panel').ontoggle()
  $('reveal').onclick()
  assert.equal($('note').value, '对照后补充：更看重趋势')
  $('assistant-panel').open = true; $('assistant-panel').ontoggle()
  $('save').onclick()
  const first = (await h.download()).labels[0]
  assert.equal(first.morphology, 'right')
  assert.equal(first.assistant_reference.before.morphology, 'left')
  assert.equal(first.assistant_reference.before.note, '先独立写：连续推进')
  assert.equal(first.note, '对照后补充：更看重趋势')
  assert.equal(first.assistant_reference.viewed, true)
  assert.equal(first.assistant_reference.notes_hash, 'notes-hash')
  $('next').onclick(); assert.equal($('assistant-panel').open, false)
  $('morphology').value = 'tie'; $('opportunity').value = 'uncertain'; $('save').onclick()
  assert.equal((await h.download()).labels[1].assistant_reference.viewed, false)
})

test('legacy JSON imports without losing notes; wrong task import leaves existing work intact', async () => {
  const h = referenceHarness(), $ = h.get, p = h.pairs[0]
  const label = { pair_id: p.pair_id, left_case_id: p.left.case_id, right_case_id: p.right.case_id, left_input_hash: p.left.input_hash, right_input_hash: p.right.input_hash, morphology: 'left', opportunity: 'right', reasons: ['a'], note: '旧页已经写好的补充' }
  const data = { manifest_hash: 'manifest', display_version: 'blind-quality-v1', labels: [label] }
  $('labels-file').files = [{ text: async () => JSON.stringify(data) }]; await $('labels-file').onchange()
  assert.equal($('note').value, label.note)
  assert.equal((await h.download()).labels[0].note, label.note)
  data.manifest_hash = 'other-task'; data.labels[0].note = '不能覆盖'
  $('labels-file').files = [{ text: async () => JSON.stringify(data) }]; await $('labels-file').onchange()
  assert.equal($('note').value, '旧页已经写好的补充')
  assert.ok($('message').textContent.includes('其他盲评任务'))
})
