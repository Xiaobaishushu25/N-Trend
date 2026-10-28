"""One sequential routing hypothesis; unchanged bidirectional source executions."""
import sys
sys.dont_write_bytecode = True
import argparse
import ast
import collections
import copy
import datetime as dt
import json
import shutil
import subprocess
import research_swing_jd_reclaim_execution_opt as x
import validate_swing_jd_reclaim_2025 as v

b = x.b
R = x.R
EV = x.EV
RUN = b.ROOT / 'research-runs/swing-jd-reclaim-routing-20261027-v1'
COMBO = x.RUN / 'F2S8-followup'
PRIMARY = 'original/corrected_touch/slip1'
ROUTES = ['E20_FIRST', 'RECLAIM_FIRST']
write = lambda name, obj: b.z.s.m.old.write(RUN / name, obj)


def sources():
    paths = [v.RUN / 'offline-order-replay' / q for q in
             ['data.json', 'individual-decisions.jsonl', 'replay.py', 'controller.py', 'manifest.json']]
    paths += [v.RUN / 'results.json', x.RUN / 'main-results.json', COMBO / 'main-results.json',
              x.RUN / 'protocol.json', COMBO / 'protocol.json', x.r.SNAP / 'manifest.json',
              b.ROOT / 'scripts/exchange_fees_20261022.json']
    for per in ['early', 'recent', 'july']:
        for parent in [x.RUN, COMBO, x.r.RUN / per / 'E20S1']:
            base = parent if parent.name == 'E20S1' else parent / per
            paths += [base / 'executions' / q for q in ['evaluations.jsonl', 'manifest.json']]
    paths += [b.ROOT / 'scripts' / q for q in ['research_swing_jd_reclaim_execution_opt.py',
              'research_swing_jd_reclaim_robustness.py', 'research_swing_jd_abc_execution.py',
              'research_swing_jd_frequency.py', 'validate_swing_jd_reclaim_2025.py']]
    return {str(p.relative_to(b.ROOT)): x.sha(p) for p in paths}


def prepare():
    RUN.mkdir(exist_ok=False)
    write('protocol.json', dict(id=RUN.name,
        created_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
        hypothesis='Only reverse cross-module priority at exactly equal warning_ts. A completed S0 failure-and-reclaim may deserve priority over a new E20 touch order at that same close.',
        declaration='Sequential AFTER observed 2025 transfer failure and its missed Oct31/Nov26 opportunities. Both 2025 and 2026 have been inspected; this is mechanism diagnosis, NOT new OOS or promotion evidence.',
        models=['E20_ONLY', 'BASE', 'F2S8'], routes=ROUTES,
        invariant='One JD slot reserved at warning, strict release<next warning, two realized original A-stops gate unchanged. No preemption, cancellation, extra concurrent position, direction/season/month filter, altered entry, target, stop, fees or exit. Lexical original key tie inside each module unchanged.',
        sources='All fee-priced native individual decisions including rejects. Re-run continuous admission, never sum standalone accepted profits. F2S8 uses its own actual combined exit replay, not REFAIL2 alone.',
        periods=dict(old='2025-09-01 through 2025-12-31 warning cohort plus actual execution tail',
                     development='2026 Jan-Jun, Jul1-10, Aug10-Oct23 11:05, existing gaps; known Jul13-24 excluded just as original main'),
        cases=R(v.RUN / 'offline-order-replay/data.json')['cases'], primary=PRIMARY,
        evaluation='Both declared exit families, both periods, every 16 fill/slip/unknown-policy cases, monthly attribution and removal of best actual winner. No threshold search or outcome-driven same-clock selection.',
        risks='Cash results are one lot with frozen fee schedule; risk300 normalization is hypothetical fractional sizing, not account return. Closed-equity DD is not intratrade MTM DD. Unknown remains unknown; 16 cases are not independent market samples.',
        production_before=R(x.RUN / 'protocol.json')['production_before'],
        source_hashes=sources(), script_sha256=x.sha(__file__), qualified_for_trading=False))
    print('Frozen one same-clock priority hypothesis, both exit families, both periods.', flush=True)


def load():
    ctl, meta, data, ends, qa = x.load()
    fees = R(x.r.SNAP / 'fees.json')
    for per in ['early', 'recent', 'july']:
        cal = b.z.s.TradingDates({}); cal.dates = x.r.f.t.period_paths(per)[3]
        for line in (COMBO / per / 'executions/evaluations.jsonl').open(encoding='utf-8'):
            row = b.z.s.reprice(json.loads(line), fees, cal)
            internal, suffix = row['scenario'].split('/', 1)
            assert internal == 'REFAIL2'
            row.update(period=per, scenario='F2S8/' + suffix)
            data['F2S8', suffix].append(row)
    data26 = collections.defaultdict(list)
    for (model, suffix), rows in data.items():
        if model in ['E20S1', 'BASE', 'F2S8']:
            data26[model, suffix] = [q for q in rows if q['period'] != 'july']
    spec = R(v.RUN / 'offline-order-replay/data.json'); data25 = collections.defaultdict(list)
    for line in (v.RUN / 'offline-order-replay/individual-decisions.jsonl').open(encoding='utf-8'):
        q = json.loads(line); model = 'F2S8' if q['model'] == 'REFAIL2' else q['model']
        data25[model, q['suffix']].append(q['row'])
    return ctl, dict(Y2025=dict(data=data25, meta=spec['metadata'], ends=spec['ends'], months=v.MONTHS),
                     Y2026=dict(data=data26, meta=meta, ends=ends, months=b.MONTHS)), qa


def stream_for(dataset, suffix, model, route):
    stream = []; meta = dict(dataset['meta'])
    components = ['E20S1'] if model == 'E20_ONLY' else ['E20S1', model]
    for component in components:
        prefix = '0' if (component == 'E20S1') == (route == 'E20_FIRST') else '1'
        for original in dataset['data'][component, suffix]:
            row = copy.deepcopy(original); oldkey = row['event_key']; key = prefix + '|' + oldkey
            meta[key] = meta[oldkey]
            row.update(original_event_key=oldkey, event_key=key,
                       module='E20S1' if component == 'E20S1' else 'S0_RECLAIM_R2')
            stream.append(row)
    return stream, meta


def identity(row):
    return row['module'] + '|' + row['original_event_key']


def brief(row):
    e = EV(row)
    return dict(id=identity(row), warning_ts=row['warning_ts'], direction=row['direction'],
                module=row['module'], status=e['status'], entry_ts=e['entry_ts'], exit_ts=e['exit_ts'],
                exit_reason=e['exit_reason'], net=e['net_amount'], mfe_r=e['mfe_r'], mae_r=e['mae_r'],
                normalized_net=e['net_amount']*300/(row['planned_risk']*row['multiplier']) if e['status']=='closed' else None)


def trace(stream, meta, ends, policy, ctl, accepted):
    """Diagnostic future individual PnL never participates in admission."""
    keep = {r['event_key'] for r in accepted}; active = None; until = ''; out = []
    for row in sorted(stream, key=lambda q: (q['warning_ts'], q['event_key'])):
        ts = row['warning_ts']
        if active is not None and until < ts: active = None
        if b.z.order_interval(row, ends, policy, ctl) is None: continue
        if row['event_key'] in keep:
            assert active is None
            active = row; until = b.z.order_interval(row, ends, policy, ctl)[1]
        elif active is not None:
            fills = [z for z in active['result']['fills'] if z['offset']=='open' and z['ts'] <= ts]
            out.append(dict(candidate=brief(row), blocker=brief(active),
                            same_warning=active['warning_ts']==ts,
                            blocker_observed_state='entered' if fills else 'pending',
                            note='Individual future result is hypothetical if blocked; not an allocation input.'))
    return out


def same_numbers(q, expected):
    for key in ['n', 'net', 'unknown', 'normalized_net', 'normalized_realized_dd', 'normalized_ex_top3']:
        assert abs(q[key]-expected[key]) < 1e-7, (key, q[key], expected[key])


def analyze():
    p = R(RUN / 'protocol.json')
    assert p['script_sha256'] == x.sha(__file__)
    assert p['source_hashes'] == sources()
    for name, h in R(x.r.SNAP / 'manifest.json')['files'].items(): assert x.sha(x.r.SNAP/name)==h
    ctl, datasets, qa = load(); results = {}; monthly = {}; accepted = {}; traces = {}; checks = 0
    for year, ds in datasets.items():
        for case in p['cases']:
            policy, suffix = case.split('/', 1)
            for model in ['E20_ONLY', 'BASE', 'F2S8']:
                for route in (['E20_FIRST'] if model=='E20_ONLY' else ROUTES):
                    stream, meta = stream_for(ds, suffix, model, route)
                    aa, ledger = b.admit(stream, ds['ends'], policy, ctl, meta)
                    key = '/'.join([year, case, model, route]); q = b.z.metric(aa, ds['months'])
                    closed = [z for z in aa if EV(z)['status']=='closed']
                    q['ex_best_cash'] = q['net']-max([0]+[EV(z)['net_amount'] for z in closed])
                    q['ex_top3_cash'] = q['net']-sum(sorted([max(0,EV(z)['net_amount']) for z in closed],reverse=True)[:3])
                    q['directions'] = {side:b.z.metric([z for z in aa if z['direction']==side],ds['months']) for side in ['up','down']}
                    results[key] = q; accepted[key] = aa
                    for month in ds['months']:
                        monthly[key+'/'+month] = b.z.metric([z for z in aa if z['warning_ts'].startswith(month)], [month])
                    if route=='E20_FIRST':
                        ref = R(v.RUN/'results.json') if year=='Y2025' else R((COMBO if model=='F2S8' else x.RUN)/'main-results.json')
                        same_numbers(q, ref[case+'/'+model]); checks += 1
                    if case==PRIMARY:
                        traces[key] = trace(stream,meta,ds['ends'],policy,ctl,aa)
                        write('ledger-'+year+'-'+model+'-'+route+'.json', ledger)
    attribution = {}
    for year in datasets:
        for case in p['cases']:
            for model in ['BASE','F2S8']:
                stem = '/'.join([year,case,model]); old = {identity(z):z for z in accepted[stem+'/E20_FIRST']}; new = {identity(z):z for z in accepted[stem+'/RECLAIM_FIRST']}
                added = [brief(new[k]) for k in sorted(new.keys()-old.keys())]; removed = [brief(old[k]) for k in sorted(old.keys()-new.keys())]
                for k in old.keys()&new.keys(): assert old[k]['result']==new[k]['result']
                delta = sum(z['net'] for z in added if z['status']=='closed')-sum(z['net'] for z in removed if z['status']=='closed')
                assert abs(delta-(results[stem+'/RECLAIM_FIRST']['net']-results[stem+'/E20_FIRST']['net']))<1e-7
                attribution[stem] = dict(added=added,removed=removed,net_delta=delta,
                    common_individual_results_unchanged=len(old.keys()&new.keys()))
    write('results.json',results); write('monthly-results.json',monthly); write('attribution.json',attribution); write('primary-conflicts.json',traces)
    with (RUN/'accepted-primary.jsonl').open('x',encoding='utf-8') as out:
        for key, rows in accepted.items():
            if '/'+PRIMARY+'/' in key:
                for row in rows: out.write(json.dumps(dict(key=key,row=row),ensure_ascii=False)+'\n')
    offline(datasets, results, monthly, p['cases'])
    verification = dict(exact_published_baseline_cases=checks,prior_native_checks=qa,
        source_hashes_unchanged=p['source_hashes']==sources(),
        production_unchanged=all(x.sha(b.ROOT/q)==h for q,h in p['production_before'].items()))
    assert checks==96 and verification['production_unchanged']
    write('verification.json',verification)
    for year in datasets:
        for model in ['E20_ONLY','BASE','F2S8']:
            for route in (['E20_FIRST'] if model=='E20_ONLY' else ROUTES):
                key='/'.join([year,PRIMARY,model,route]); q=results[key]
                print(year,model,route,json.dumps({k:q[k] for k in ['n','net','unknown','normalized_net','ex_best_cash','cash_realized_dd']}),
                      'min16',min(results['/'.join([year,case,model,route])]['net'] for case in p['cases']),flush=True)


def offline(datasets, results, monthly, cases):
    dest=RUN/'offline-order-replay';dest.mkdir()
    oldwrite=b.z.s.m.old.write
    shutil.copy2(v.RUN/'offline-order-replay/controller.py',dest/'controller.py')
    oldwrite(dest/'data.json',dict(cases=cases,years={y:{k:z for k,z in ds.items() if k!='data'} for y,ds in datasets.items()}))
    oldwrite(dest/'expected-results.json',results);oldwrite(dest/'expected-monthly.json',monthly)
    with (dest/'individual-decisions.jsonl').open('x',encoding='utf-8') as out:
        for year,ds in datasets.items():
            for (model,suffix),rows in ds['data'].items():
                for row in rows:out.write(json.dumps(dict(year=year,model=model,suffix=suffix,row=row),ensure_ascii=False)+'\n')
    src=(v.RUN/'offline-order-replay/replay.py').read_text(encoding='utf-8');tree=ast.parse(src)
    helpers='\n\n'.join(ast.get_source_segment(src,n) for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ['ev','admit','metric','same'])
    code='''"""Read-only stdlib replay of both priorities and periods."""
import sys
sys.dont_write_bytecode=True
import collections,copy,hashlib,json
from pathlib import Path
import controller
ROOT=Path(__file__).resolve().parent
UNKNOWN={'rollover','pending','unfinished','ambiguous','missing_data'}
IMMEDIATE={'filtered','filtered_setup','filtered_geometry','filtered_entry_economics','filtered_A_net_body','filtered_net_rr_plan'}
def read(n):return json.loads((ROOT/n).read_text(encoding='utf-8'))
'''+helpers+'''
def main():
    manifest=read('manifest.json')
    for name,h in manifest.items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==h,name
    spec=read('data.json');data=collections.defaultdict(list);checks=0
    for line in (ROOT/'individual-decisions.jsonl').open(encoding='utf-8'):
        z=json.loads(line);data[z['year'],z['model'],z['suffix']].append(z['row'])
    expected=read('expected-results.json');monthly=read('expected-monthly.json')
    for year,ds in spec['years'].items():
        for case in spec['cases']:
            policy,suffix=case.split('/',1)
            for model in ['E20_ONLY','BASE','F2S8']:
                for route in (['E20_FIRST'] if model=='E20_ONLY' else ['E20_FIRST','RECLAIM_FIRST']):
                    meta=dict(ds['meta']);stream=[]
                    for component in (['E20S1'] if model=='E20_ONLY' else ['E20S1',model]):
                        prefix='0' if (component=='E20S1')==(route=='E20_FIRST') else '1'
                        for orig in data[year,component,suffix]:
                            row=copy.deepcopy(orig);key=prefix+'|'+row['event_key'];meta[key]=meta[row['event_key']];row['event_key']=key;stream.append(row)
                    aa=admit(stream,ds['ends'],policy,meta);key='/'.join([year,case,model,route])
                    same(metric(aa,ds['months']),expected[key]);checks+=1
                    for month in ds['months']:
                        same(metric([r for r in aa if r['warning_ts'].startswith(month)],[month]),monthly[key+'/'+month]);checks+=1
    print(json.dumps(dict(verified_records=checks,files_verified=len(manifest))))
if __name__=='__main__':main()
'''
    (dest/'replay.py').write_text(code,encoding='utf-8')
    oldwrite(dest/'manifest.json',{q.name:x.sha(q) for q in dest.iterdir() if q.is_file()})
    result=subprocess.run(['python',str(dest/'replay.py')],capture_output=True,text=True,check=True)
    write('offline-verification.json',json.loads(result.stdout))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('stage',choices=['prepare','analyze']);args=parser.parse_args()
    (prepare if args.stage=='prepare' else analyze)()
