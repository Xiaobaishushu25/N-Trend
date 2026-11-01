const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const ts = require('typescript')

test('new review client forwards nested filters, versions and pagination without touching legacy calls', async () => {
  const calls = [], module = { exports: {} }
  const code = ts.transpileModule(fs.readFileSync('src/services/api.ts', 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText
  vm.runInNewContext(code, { module, exports: module.exports, require: name => {
    if (name === '@tauri-apps/api/core') return { invoke: (command, args) => { calls.push({ command, args }); return Promise.resolve({}) } }
    if (name === '@tauri-apps/api/event') return { listen() {} }
    if (name.endsWith('reviewFilters')) return { reviewFilterQuery: value => value }
    throw new Error(name)
  } })
  const api = module.exports.api
  const request = { filters: { symbols: ['RB0', 'SA0'], score_metric: 'a_score', score_min: 70, sessions: [{ start: '21:00', end: '02:30' }], exit_reasons: ['no_follow'] }, page_size: 100 }
  await api.reviewQuery(request)
  assert.equal(calls[0].command, 'review_query')
  assert.equal(calls[0].args.request, request)
  await api.reviewItems('snapshot', { cursor: 100, limit: 100 })
  await api.reviewDetail(5, 'result-v1')
  await api.reviewHolding(5, 'result-v1', { cursor: 20, limit: 10 })
  assert.equal(calls[1].args.snapshotId, 'snapshot')
  assert.equal(calls[2].args.version, 'result-v1')
  assert.equal(calls[3].args.request.cursor, 20)
  await api.reviewAggregate({ snapshot_id: 'snapshot', dimensions: ['symbol', 'hour'] })
  await api.reviewRebuild({ symbols: ['RB0'] })
  await api.reviewJob('job')
  await api.reviewMetadata()
  await api.reviewDetail(6)
  assert.equal(calls.at(-1).args.version, null, 'event-only links ask the server for a published head, never for a profile')
  const native = fs.readFileSync('src-tauri/src/lib.rs', 'utf8')
  for (const call of calls) assert.ok(native.includes(`commands::${call.command},`), `${call.command} must be registered`)
})
