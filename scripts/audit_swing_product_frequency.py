"""Read-only attribution of sparse baseline trades and latest server fills."""
import collections
import datetime as dt
import json
import pathlib
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import research_swing_monthly_products as m

ROOT = m.REPO
OUT = ROOT / 'research-runs/swing-product-frequency-audit-20261026-v1'
SERVER = ROOT / 'research-archives/server-execution-20261026-product-frequency-v2'
PRODUCTS = ['JD0', 'SR0', 'CJ0', 'SI0']
BOUNDS = {
    'older': ('2025-09-01 00:00:00', '2025-12-31 15:00:00'),
    'early': ('2026-01-01 00:00:00', '2026-06-30 15:00:00'),
    'recent': ('2026-07-01 00:00:00', '2026-10-23 11:05:00'),
}


def read(p):
    return json.loads(pathlib.Path(p).read_text(encoding='utf-8'))


def write(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def run():
    OUT.mkdir(exist_ok=False)
    controller, pp, events, ends, data, sources, coverage = m.load()
    generated = {}
    raw_sets = {}
    kept_sets = {}
    for family in ['best_a', 'unmerged']:
        generated[family] = {}
        for sym in PRODUCTS:
            counts = collections.Counter()
            raw_sets[family, sym] = set()
            kept_sets[family, sym] = set()
            for period, (start, end) in BOUNDS.items():
                if period == 'older':
                    path = m.old.RUN / family / 'older-pool-symbols' / (sym + '.json')
                else:
                    parent = ROOT / ('research-runs/swing-retirement-20261025-v1/near_start' if family == 'best_a' else 'research-runs/swing-wave-selection-20261025-v1/unmerged')
                    path = parent / (period + '-symbols') / (sym + '.json')
                z = read(path)
                raw = {k for k in z['raw_keys'] if start <= k.rsplit('|', 1)[1] <= end}
                kept = {k for k in z['kept_keys'] if start <= k.rsplit('|', 1)[1] <= end}
                pool = {r['event_key'] for r in z['pool']}
                assert pool <= kept <= raw, (path, len(pool - kept))
                counts.update(raw=len(raw), service_kept=len(kept), quality_pool=len(pool))
                raw_sets[family, sym].update(raw)
                kept_sets[family, sym].update(kept)
                sources[str(path)] = m.old.sha(path)
            generated[family][sym] = dict(counts)
    models = {}
    detailed_rows = []
    all_accepted = {}
    for model, (family, profile, rule) in m.report.MODELS.items():
        rows = data[family][profile, m.PRIMARY]
        accepted, ledger, _ = controller.select(rows, events[family], ends[family], pp['sector_map'], rule, 'original')
        m.report.audit(ledger, rows)
        all_accepted[model] = {r['event_key'] for r in accepted}
        ls = {r['event_key']: r for r in ledger}
        models[model] = {}
        for sym in PRODUCTS:
            rs = [r for r in rows if r['symbol'] == sym]
            aa = [r for r in accepted if r['symbol'] == sym]
            assert len(rs) == generated[family][sym]['quality_pool']
            statuses = collections.Counter(m.report.s.ev(r)['status'] for r in rs)
            blocked = collections.Counter()
            checks = collections.Counter()
            for r in rs:
                e = m.report.s.ev(r)
                if r['planned_risk'] / r['tick_size'] + 1e-6 < 10:
                    checks['geometry_min10_first'] += 1
                elif r['new_friction'] > .2 + 1e-9:
                    checks['geometry_friction_first'] += 1
                else:
                    checks['geometry_pass'] += 1
                if r['planned_risk'] / r['tick_size'] + 1e-6 < 10 and r['new_friction'] > .2 + 1e-9:
                    checks['geometry_both_fail'] += 1
                l = ls.get(r['event_key'])
                if l and not l['accepted']:
                    blocked[l['reason']] += 1
                if model == 'B5':
                    detailed_rows.append(dict(symbol=sym, event_key=r['event_key'], period=r['period'],
                        status=e['status'], accepted=r['event_key'] in all_accepted[model],
                        controller_reason=l['reason'] if l else None,
                        planned_risk_ticks=r['planned_risk']/r['tick_size'], friction=r['new_friction'],
                        economic_pass=r['economic_pass'], body_net_pass=r['body_net_pass'],
                        entry=r['entry'], stop=r['stop'], target=r['target'], evaluation=e))
            assert statuses['filtered_geometry'] == checks['geometry_min10_first'] + checks['geometry_friction_first']
            closed = [r for r in aa if m.report.s.ev(r)['status'] == 'closed']
            models[model][sym] = dict(generation=generated[family][sym], plan_checks=dict(checks),
                evaluation_statuses=dict(statuses), controller_rejections=dict(blocked),
                accepted_statuses=dict(collections.Counter(m.report.s.ev(r)['status'] for r in aa)),
                closed=len(closed), net=sum(m.report.s.ev(r)['net_amount'] for r in closed))
    history = read(SERVER / 'trade-history.json')['data']
    conditions = {r['id']:r for r in read(SERVER / 'conditions-api.json')['data']}
    reference = read(m.RUN / 'frequencies.json')
    for model in models:
        for sym, result in models[model].items():
            assert result['closed'] == reference[model][sym]['n']
            assert abs(result['net'] - reference[model][sym]['net']) < 1e-7
    live = {}
    for sym in PRODUCTS:
        rs = [r for r in history if r['symbol'] == sym]
        mult = 10 if sym in ['JD0', 'SR0'] else 5
        groups = collections.defaultdict(list)
        for r in rs:
            groups[r['condition_order_id']].append(r)
        trades = []
        for cid, legs in groups.items():
            signal = (legs[0].get('signal_evidence') or {}).get('signal')
            if signal is None:
                signal = conditions[cid].get('frozen', {}).get('plan', {}).get('evidence', {}).get('source_evidence', {}).get('signal')
            key = '|'.join(str(signal[k]) for k in ['symbol', 'direction', 's0_ts', 's1_ts', 's2_ts', 'warning_ts']) if signal else None
            comparisons = {}
            for model, (family, profile, _) in m.report.MODELS.items():
                found = next((r for r in data[family][profile, m.PRIMARY] if r['event_key'] == key), None)
                if found:
                    e = m.report.s.ev(found)
                    comparisons[model] = dict(status=e['status'], accepted=key in all_accepted[model],
                        risk_ticks=found['planned_risk']/found['tick_size'], friction=found['new_friction'],
                        entry=found['entry'], stop=found['stop'], target=found['target'], evaluation=e)
                else:
                    comparisons[model] = dict(status='absent_from_quality_pool', raw=key in raw_sets[family, sym], service_kept=key in kept_sets[family, sym])
                    # Diagnostic match only: do not silently equate different S0 anchors.
                    near = [r for r in data[family][profile, m.PRIMARY] if signal and r['symbol']==sym and r['direction']==signal['direction'] and r['warning_ts']==signal['warning_ts']]
                    comparisons[model]['same_warning_alternative_events'] = [dict(event_key=r['event_key'], status=m.report.s.ev(r)['status'],
                        risk_ticks=r['planned_risk']/r['tick_size'], friction=r['new_friction'],
                        entry=r['entry'], stop=r['stop'], target=r['target']) for r in near]
            trades.append(dict(condition_id=cid, event_id=legs[0].get('event_id'), event_key=key,
                legs=[{k:r.get(k) for k in ['id', 'symbol', 'instrument_id', 'direction', 'volume', 'fill_price', 'exit_price', 'open_ts', 'exit_ts', 'exit_reason', 'realized_pnl_amount']} for r in legs],
                recorded_amount=sum(r['realized_pnl_amount'] for r in legs),
                rebuilt_gross=sum((r['exit_price']-r['fill_price'])*(1 if r['direction']=='up' else -1)*r['volume']*mult for r in legs),
                comparisons=comparisons))
        live[sym] = dict(closing_legs=len(rs), grouped_conditions=len(groups),
            recorded_amount=sum(r['realized_pnl_amount'] for r in rs),
            rebuilt_gross=sum(t['rebuilt_gross'] for t in trades), trades=trades)
    write('funnel.json', models)
    write('b5-opportunities.json', detailed_rows)
    write('live-reconciliation.json', live)
    write('coverage.json', coverage)
    write('sources.json', sources)
    write('audit.json', dict(created_at=dt.datetime.now().astimezone().isoformat(),
        server_manifest=read(SERVER / 'collection-manifest.json'),
        baseline_unchanged=True, production_changed=False, reference_product_count_and_net_checks=24,
        method='Existing immutable single-lot executions; primary corrected_touch/slip1; full-universe B0-B5 reservations. Counts are warning-key opportunities, not exit legs. Geometry attribution uses min10 first then friction to avoid double-counting; overlap also reported.'))
    print(json.dumps({'models':models, 'live_summary':{s:{k:v for k,v in x.items() if k != 'trades'} for s,x in live.items()}}), flush=True)


if __name__ == '__main__':
    run()
