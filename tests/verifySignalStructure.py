"""Artifact QA, not a second scoring/diagnostic implementation. Standard library only."""
import hashlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(sys.argv[1]).resolve()
BASE = ROOT / 'research-runs/signal-quality-v1-20261029'
read = lambda path: json.loads(path.read_text(encoding='utf-8'))
task = read(OUT / 'qualification-task.json')
old_task = read(OUT / 'input-v1-task.json')
labels = read(OUT / 'input-v1-labels.json')
manifest = read(OUT / 'manifest.json')
summary = read(OUT / 'summary.json')
audit = read(BASE / 'release-fe7b270/audit.json')
evidence = read(OUT / 'evidence.json')
modules = ['trend_ma', 'a_process', 'b_process', 'levels', 'warning', 'space']

assert task['version'] == 'qualification-evidence-v2'
assert task['display_version'] == 'qualification-modules-v2'
assert task['previous_task_hash'] == old_task['task_hash']
assert task['previous_case_assessments'] == labels['case_assessments']
assert task['previous_case_drafts'] == labels['case_drafts']
assert task['legacy_labels'] == labels['labels']
assert task['new_queue'] == old_task['new_queue']
assert task['revisit_queue'] == old_task['revisit_queue']
assert len(task['cases']) == 60
raw = (OUT / 'qualification-task.json').read_bytes()
claimed = task['task_hash']
assert hashlib.sha256(raw.replace(claimed.encode(), b'0' * 64, 1)).hexdigest() == claimed
for name in ['blind-manifest.json', 'assistant-notes.json']:
    assert (OUT / name).read_bytes() == (BASE / 'qualification-v1-final' / name).read_bytes()
for forbidden in ['quality_score', 'old_score', 'net_r', 'exit_ts', 'label_win']:
    assert ('"' + forbidden + '"').encode() not in raw
for case_id, case in task['cases'].items():
    for key in ['bars', 'points', 'input_hash', 'entry', 'stop', 'rr']:
        assert case[key] == old_task['cases'][case_id][key]
    diag = case['diagnostics']
    assert diag['module_order'] == modules
    assert set(diag['modules']) == set(modules)
    assert all(m['scoring_effect'] == 'none' for m in diag['modules'].values())
    assert all(p['confirmed_index'] < diag['s0_index']
               for p in diag['modules']['levels']['metrics']['confirmed_pivots'])

case = lambda n: task['cases'][task['new_queue'][n - 1]]['diagnostics']['modules']
assert case(5)['a_process']['metrics']['interior']['forward_efficiency'] == 0
assert case(5)['a_process']['metrics']['interior']['signed_efficiency'] == -1
assert case(8)['b_process']['metrics']['reverse_close_steps'] == 5
assert case(8)['b_process']['metrics']['outside_s1_before_warning_atr'] == 0
assert case(10)['b_process']['metrics']['bars_without_warning'] == 2
assert case(10)['b_process']['metrics']['reverse_close_steps'] == 1
assert case(10)['b_process']['metrics']['max_close_retreat_from_s1_close_atr'] == 0
assert case(16)['b_process']['metrics']['bars_without_warning'] == 29
assert math.isclose(case(20)['a_process']['metrics']['interior']['forward_efficiency'], 1 / 35)

valid = {r['event_id']: r for r in audit if r['quality']['status'] == 'ok'}
by_id = {r['event_id']: r for r in evidence}
assert set(valid) == set(by_id)
assert len(valid) == manifest['diagnosed_cases'] == manifest['recomputed_hashes_and_legacy_features_verified']
assert manifest['source_rows'] == len(audit)
assert manifest['source_database_sha256'] == hashlib.sha256((OUT.parent / 'input-original.sqlite').read_bytes()).hexdigest()
assert manifest['source_code_sha256'] == hashlib.sha256(b''.join(
    (ROOT / 'crates/n-core/src/quality' / name).read_bytes()
    for name in ['structure.rs', 'structure_study.rs', 'qualification-review.html'])).hexdigest()


def check_stats(ids, stats, scenario):
    rows = [valid[i]['evaluations'][scenario] for i in ids]
    closed = [r['net_r'] for r in rows if r['status'] == 'closed' and r['net_r'] is not None]
    assert stats['candidates'] == len(ids)
    assert sum(stats['statuses'].values()) == len(ids)
    assert stats['closed_priced'] == len(closed)
    assert stats['not_closed_or_unpriced'] == len(ids) - len(closed)
    if closed:
        assert math.isclose(stats['mean_net_r'], sum(closed) / len(closed), abs_tol=1e-12)
        assert math.isclose(stats['win_rate'], sum(x > 0 for x in closed) / len(closed), abs_tol=1e-12)
    else:
        assert stats['mean_net_r'] is None and stats['win_rate'] is None


checks = 0
for name, cohort in summary['cohorts'].items():
    ids = cohort['event_ids']
    assert len(set(ids)) == len(ids)
    for label, scenario in [('one_tick', 'fixed_one_1tick'), ('two_tick', 'fixed_one_2tick')]:
        check_stats(ids, cohort['stats'][label], scenario)
    for removed, ablation in cohort['v1_fixed_weight_ablation'].items():
        assert set(ablation['event_ids']) <= set(ids)
        for label, scenario in [('one_tick', 'fixed_one_1tick'), ('two_tick', 'fixed_one_2tick')]:
            check_stats(ablation['event_ids'], ablation['stats'][label], scenario)
    for path, group in summary['metric_groups'].items():
        module, *keys = path.split('.')
        bins = group['cohorts'][name]
        bounds = bins['bounds_inclusive_upper']
        groups = [[] for _ in bins['bins']]
        missing = []
        for event_id in ids:
            value = by_id[event_id]['evidence']['modules'][module]['metrics']
            for key in keys:
                value = value[key]
            if value is None:
                missing.append(event_id)
            else:
                i = next((i for i, upper in enumerate(bounds) if value <= upper), len(bounds))
                groups[i].append(event_id)
        for bin_ids, output in list(zip(groups, (b['stats'] for b in bins['bins']))) + [(missing, bins['missing'])]:
            for label, scenario in [('one_tick', 'fixed_one_1tick'), ('two_tick', 'fixed_one_2tick')]:
                check_stats(bin_ids, output[label], scenario)
            checks += 1

# Synthetic roundtrip fixture is explicitly excluded from human-label research.
first = task['cases'][task['new_queue'][0]]
form = dict(morphology_qualification='uncertain', opportunity_qualification='uncertain',
            pattern_type='uncertain', reasons=[], note='QA ONLY - synthetic, never a human label')
label = dict(form, case_id=first['case_id'], input_hash=first['input_hash'], assistant_reference=dict(
    viewed=True, version=task['version'], task_hash=task['task_hash'],
    opened_at='2026-10-30T00:00:00+08:00', before=form, viewed_modules=modules))
bundle = dict(manifest_hash=task['base_manifest_hash'], display_version=task['base_display_version'],
              labels=task['legacy_labels'], qualification_task_hash=task['task_hash'],
              assessment_version=task['version'], assessment_display_version=task['display_version'],
              case_assessments=[label], case_drafts=[])
(OUT / 'QA-v2-labels.json').write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding='utf-8')
result = dict(full_cases=len(valid), review_cases=len(task['cases']), modules=modules,
              prior_labels_preserved=True, no_scores_or_outcomes_in_review=True,
              task_hash_verified=True, source_database_hash_verified=True,
              legacy_and_cohort_stats_crosschecked=True, grouped_stats_crosschecked=checks,
              diagnostic_fixtures_checked=[5, 8, 10, 16, 20], qa_labels_are_synthetic=True)
(OUT / 'verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(result, ensure_ascii=False))
