"""Finite product-specialization study; frozen candidates, chronological validation.

Uses archived, unlabelled warning pools and the frozen execution library. No live
orders or production edits. Each invocation writes only into a new research run.
"""
import argparse
import collections
import copy
import datetime as dt
import json
import math
import pathlib
import shutil
import statistics
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import research_swing_monthly_products as m
import research_swing_may_aug2025 as acquisition
from reprice_signal_costs import TradingDates, reprice

ROOT = m.REPO
RUN = ROOT / 'research-runs/swing-specialists-walkforward-20261026-v1'
ARCHIVE = RUN
PARENT = ROOT / 'research-runs/swing-four-product-relaxation-20261026-v1'
MAY = ROOT / 'research-runs/swing-universe-may-aug2025-20261026-v1'
NATIVE = ROOT / 'research-runs/swing-entry-economics-20261025-v1/native-verification'
FORK = RUN / 'fork'
TARGET = ROOT / 'target/swing-specialists-isolated'
EXE = TARGET / 'release/swing_specialists.exe'
SIX = ['EG0', 'PP0', 'V0', 'JD0', 'SR0', 'CJ0']
FOUR = ['EG0', 'V0', 'JD0', 'SR0']
AGR = ['JD0', 'SR0', 'CJ0']
MONTHS = [f'2025-{i:02}' for i in range(5, 13)] + [f'2026-{i:02}' for i in range(1, 11)]
TEMPLATES = {
    'C_B5': {}, 'C_B2': {'body_net_gate': False},
    'E20': {'max_cost_space': .20}, 'S1': {'local': False},
    'N8': {'no_follow_bar': 8},
    'E20N8': {'max_cost_space': .20, 'no_follow_bar': 8},
    'E20S1': {'max_cost_space': .20, 'local': False},
}
VARIANTS = ['B5', 'B2', 'JD20', 'AG20', 'AG_S1', 'AG_N8',
            'JD20_AG_N8', 'AG20_N8', 'AG20_S1', 'AG20_aligned', 'AG_B2']
LABELS = {'B5': 'B5原版', 'B2': 'B2原版', 'JD20': '仅JD成本比20%',
          'AG20': '农产品成本比20%', 'AG_S1': '农产品原S1目标',
          'AG_N8': '农产品第8桶无跟随', 'JD20_AG_N8': 'JD20%＋农产品第8桶',
          'AG20_N8': '农产品20%＋第8桶', 'AG20_S1': '农产品20%＋S1',
          'AG20_aligned': '农产品仅小时顺向放至20%', 'AG_B2': '农产品取消A净实体'}
FOLDS = [('2025-09-01 00:00:00', '2026-01-01 00:00:00'),
         ('2026-01-01 00:00:00', '2026-04-01 00:00:00'),
         ('2026-04-01 00:00:00', '2026-07-01 00:00:00'),
         ('2026-07-01 00:00:00', '2026-10-01 00:00:00'),
         ('2026-10-01 00:00:00', '2026-11-01 00:00:00')]


def write(name, value):
    m.old.write(RUN / name, value)


def prepare():
    RUN.mkdir(exist_ok=False)
    base = m.old.read(PARENT / 'protocol.json')['templates'][0]
    templates = []
    for name, changes in TEMPLATES.items():
        t = copy.deepcopy(base)
        t.update(name=name, no_follow_bar=5)
        t.update(changes)
        templates.append(t)
    production = ['crates/n-core/src/trader/candidates.rs',
                  'crates/n-core/src/trader/candidate_execution_tests.rs',
                  'src/stores/liveTrader.ts', 'src/types.ts',
                  'src/views/LiveTraderView.vue', 'tests/liveTrader.test.cjs']
    p = dict(id=RUN.name, created_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
             primary_universe=FOUR, comparison_universe=SIX, templates=templates,
             variants=VARIANTS, template_routes='As implemented by route(); chemicals remain C_B5 except the B2 control.',
             folds=FOLDS, months=MONTHS, modes=['corrected_touch', 'next_minute_open'],
             primary='original/corrected_touch/slip1', policies=['original', 'two_paths_flat'],
             training='Expanding history from2025-05. Only CLOSED exits strictly before each cutoff and warnings before cutoff. All four execution stresses and both unknown policies; no test outcome read for selection.',
             selection=dict(min_train_closed=20, min_train_agriculture_closed=5,
                 require_every_stress_net_not_below_B5=True, require_primary_ag_net_improvement=True,
                 require_primary_positive_product_hhi_not_above_B5=True,
                 require_primary_net_ex_best_trade_not_below_B5=True,
                 require_unknown_not_above_B5=True,
                 objective='max(min_8cases(net minus largest positive trade) - .25*max_8cases(realizedDD)); stable declared variant order breaks ties; fallback B5'),
             test='Frozen choice at warning time per fold; positions/orders/stop histories continue across folds. Initial2025May-Aug uses B5. Four-product schedule applied unchanged to six-product comparator and every execution scenario.',
             candidates='11 fixed portfolios including2 controls;7 native execution templates; no search of alternative thresholds, months, or lists after outcomes.',
             limitations='Repeatedly researched historical data and retrospectively selected products/strategy; chronological validation is not untouched independent OOS. October and recent symbol coverage incomplete. Fees frozen20261022; one lot, no account equity or margin model.',
             source_protocol_sha256=m.old.sha(PARENT/'protocol.json'),
             source_runner_sha256=m.old.sha(PARENT/'fork/crates/n-core/src/bin/swing_product_relaxation.rs'),
             fees_path=m.old.read(PARENT/'protocol.json')['fees_path'],
             production_before={q:m.old.sha(ROOT/q) for q in production},
             script_sha256=m.old.sha(__file__), production_changed=False)
    write('protocol.json', p)
    shutil.copytree(PARENT/'fork', FORK)
    src = (FORK/'crates/n-core/src/bin/swing_product_relaxation.rs').read_text(encoding='utf-8')
    src = m.old.replace(src, 'no_follow_bar: Some(5),',
                        'no_follow_bar: Some(template["no_follow_bar"].as_u64().unwrap_or(5) as usize),')
    (FORK/'crates/n-core/src/bin/swing_specialists.rs').write_text(src, encoding='utf-8')
    write('implementation-manifest.json', {q.relative_to(FORK).as_posix():m.old.sha(q) for q in FORK.rglob('*') if q.is_file()})
    print('Protocol frozen: 11 portfolios, 7 templates, 5 expanding chronological test groups.', flush=True)


def build():
    args = ['--release', '--offline', '--manifest-path', str(FORK/'Cargo.toml'),
            '--target-dir', str(TARGET), '-p', 'n-core', '--bin', 'swing_specialists']
    subprocess.run(['cargo', 'test', *args], check=True)
    subprocess.run(['cargo', 'build', *args], check=True)
    write('binary.json', dict(path=str(EXE), sha256=m.old.sha(EXE)))


def replay(period, inp, contracts, pool, paths):
    p = m.old.read(RUN/'protocol.json')
    dest = RUN/period
    dest.mkdir(exist_ok=False)
    pool = [z for z in pool if z['event']['symbol'] in SIX]
    m.old.write(dest/(period+'-pool.json'), pool)
    paths = dict(paths, input='memory:input', contracts='memory:contracts')
    protocol = dict(templates=p['templates'], modes=p['modes'], periods={period:paths})
    m.old.write(dest/'execution.protocol.json', protocol)
    assert m.old.sha(EXE) == m.old.read(RUN/'binary.json')['sha256']
    payload = json.dumps(dict(input=inp, contracts=contracts), ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    subprocess.run([str(EXE), str(dest/'execution.protocol.json'), period, 'replay', str(dest/'executions')], input=payload, check=True)
    print(period, 'native execution complete', flush=True)


def recent():
    p = m.old.read(PARENT/'protocol.json')
    prefixes = ('DCE.eg', 'DCE.pp', 'DCE.v', 'DCE.jd', 'CZCE.SR', 'CZCE.CJ')
    for period in ['early', 'recent']:
        paths = p['periods'][period]
        raw = m.old.read(paths['input'])
        inp = dict(execution_config=raw['execution_config'], symbols={s:{k:raw['symbols'][s][k] for k in ['variety','tick_size']} for s in SIX})
        del raw
        contracts = {c:z for c,z in m.old.read(paths['contracts']).items() if c.startswith(prefixes)}
        replay(period, inp, contracts, m.old.read(NATIVE/(period+'-pool.json')), paths)
        del contracts


def older(period):
    """Reacquire same minute hashes; reuse archived signals, never regenerate them."""
    from tqsdk import TqBacktest
    source = MAY if period == 'may_aug' else m.old.RUN
    start, tail = ('2025-04-01 00:00:00','2025-09-16 15:00:00') if period == 'may_aug' else ('2025-08-01 00:00:00','2026-01-16 15:00:00')
    acquisition_root = RUN/(period+'-acquisition')
    acquisition_root.mkdir(exist_ok=True)
    dest = acquisition_root / ('attempt-%02d' % (1+len(list(acquisition_root.glob('attempt-*')))))
    dest.mkdir(exist_ok=False)
    p = m.old.read(MAY/'protocol.json')
    acquisition.RUN = dest
    acquisition.SYMS = SIX
    acquisition.WARMUP, acquisition.TAIL = start, tail
    original_kwargs = acquisition.api_kwargs
    def kwargs(explicit):
        out = original_kwargs(explicit)
        out['backtest'] = TqBacktest(start_dt=dt.date.fromisoformat(start[:10]), end_dt=dt.date.fromisoformat(tail[:10]))
        return out
    acquisition.api_kwargs = kwargs
    m.old.write(dest/'protocol.json', p)
    acquisition.prepare_metadata()
    server, thread = acquisition.start_metadata_server()
    try:
        _, contracts = acquisition.collect(p, 15, False)
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)
    expected = m.old.read(source/'actual-contract-coverage.json')['contracts']
    observed = m.old.read(dest/'actual-contract-coverage.json')['contracts']
    assert all(v == expected[c] for c,v in observed.items()), 'Minute hashes/coverage differ from frozen acquisition'
    write(period+'-source-equality.json', dict(exact_contracts=len(observed), exact_minute_hashes=True, raw_prices_saved=False))
    sp = source/'execution.protocol.json' if period == 'may_aug' else source/'unmerged/protocol.json'
    paths = m.old.read(sp)['periods']['older']
    pool = m.old.read(source/'older-pool.json' if period == 'may_aug' else source/'unmerged/older-pool.json')
    inp = dict(execution_config=p['execution_config'], symbols=p['specs'])
    replay(period, inp, contracts, pool, paths)
    shutil.copyfile(source/'trading-dates.json', RUN/period/'trading-dates.json')


def route(name, row, events):
    s = row['symbol']
    if name == 'B2': return 'C_B2'
    if s not in AGR: return 'C_B5'
    if name == 'JD20': return 'E20' if s == 'JD0' else 'C_B5'
    if name == 'AG20': return 'E20'
    if name == 'AG_S1': return 'S1'
    if name == 'AG_N8': return 'N8'
    if name == 'JD20_AG_N8': return 'E20N8' if s == 'JD0' else 'N8'
    if name == 'AG20_N8': return 'E20N8'
    if name == 'AG20_S1': return 'E20S1'
    if name == 'AG_B2': return 'C_B2'
    if name == 'AG20_aligned':
        h = (events[row['event_key']].get('market_context') or {}).get('trend_alignment_60')
        return 'E20' if h is not None and h > 0 else 'C_B5'
    return 'C_B5'


def metric(rows, events, months=None):
    if months is None: months=MONTHS
    z = m.report.distribution(rows, events, months)
    closed = [r for r in rows if m.report.s.ev(r)['status'] == 'closed']
    cash = sorted([m.report.s.ev(r)['net_amount'] for r in closed], reverse=True)
    top = sum(max(0,v) for v in cash[:5])
    gains = sum(max(0,v) for v in cash)
    z.update(net_without_best_trade=z['net']-max([0]+cash),
             net_without_top5_trades=z['net']-top,
             top5_share_of_gross_wins=top/gains if gains else None,
             agriculture_net=sum(m.report.s.ev(r)['net_amount'] for r in closed if r['symbol'] in AGR),
             agriculture_n=sum(r['symbol'] in AGR for r in closed))
    gains_product = [max(0,v) for v in z['bins']['product'].values()]
    z['largest_positive_product_share'] = max(gains_product)/sum(gains_product) if sum(gains_product) else None
    month_pos = [max(0,v) for v in z['warning_month_net'].values()]
    z['largest_positive_month_share'] = max(month_pos)/sum(month_pos) if sum(month_pos) else None
    z['net_without_best_month'] = z['net']-max([0]+list(z['warning_month_net'].values()))
    return z


def load():
    controller, pp, old_events, old_ends, prior, sources, coverage = m.load()
    p = m.old.read(RUN/'protocol.json')
    if p.get('scope')=='cost14':
        evs=old_events['unmerged']; data=collections.defaultdict(list); features={}
        for path in [m.old.RUN/'unmerged/older-pool.json',NATIVE/'early-pool.json',NATIVE/'recent-pool.json']:
            features.update({z['event_key']:z['entry_quality'] for z in m.old.read(path)})
            sources[str(path)]=m.old.sha(path)
        for sf in m.SUFFIXES:
            body={z['event_key']:z for z in prior['unmerged']['budget_body',sf]}
            data['C_B5',sf]=list(body.values())
            data['C_B2',sf]=prior['unmerged']['budget10',sf]
            for z in prior['unmerged']['base',sf]:
                sign=1 if z['direction']=='up' else -1
                room=(z['target']-z['entry'])*sign
                allowed=room>0 and z['new_friction']*z['planned_risk']/room<=.20+1e-12 and features[z['event_key']]['a_body_net_ex_s0']>0
                data['E20',sf].append(z if allowed else body[z['event_key']])
                if not allowed: assert controller.reservation(body[z['event_key']],old_ends['unmerged'],'original') is None
        # Execution equivalence: compare every reserved2026 E20 row to the new native runner.
        native={}
        for period in ['early','recent']:
            paths=m.old.read(PARENT/'protocol.json')['periods'][period]
            cal=TradingDates(m.old.read(paths['input'])['symbols'])
            for line in (ARCHIVE/period/'executions/evaluations.jsonl').open(encoding='utf-8'):
                z=json.loads(line);t,sf=z['scenario'].split('/',1)
                if t=='E20': native[z['event_key'],sf]=reprice(z,m.old.read(p['fees_path']),cal)
        qa=0
        for sf in m.SUFFIXES:
            for z in data['E20',sf]:
                if z['symbol'] not in SIX or z['period']=='older':continue
                n=native[z['event_key'],sf]
                if controller.reservation(z,old_ends['unmerged'],'original') is not None or controller.reservation(n,old_ends['unmerged'],'original') is not None:
                    for k in ['result','entry','stop','target','planned_risk','new_friction','cost_repricing']:
                        assert z.get(k)==n.get(k),(z['event_key'],sf,k)
                    qa+=1
        write('cost20-native-equivalence.json',dict(exact_reserved_2026_rows=qa,gate='Warning-only body>0 and planned cost/space<=20%; other original base guards retained. Not counterfactual labeling of geometry-rejected signals.'))
        return controller,pp,evs,old_ends['unmerged'],data,sources,coverage
    events = {}; ends = {}; data = collections.defaultdict(list)
    fees = m.old.read(m.old.read(RUN/'protocol.json')['fees_path'])
    for period in p.get('evaluation_periods',['may_aug', 'sep_dec', 'early', 'recent']):
        root = ARCHIVE/period
        events.update({z['event_key']:z['event'] for z in m.old.read(root/(period+'-pool.json'))})
        for c,end in m.old.read(root/'executions/manifest.json')['contract_ends'].items():
            ends[c] = max(end, ends.get(c,''))
        if period in ['may_aug','sep_dec']:
            calendar = TradingDates({}); calendar.dates = m.old.read(root/'trading-dates.json')
        else:
            path = m.old.read(PARENT/'protocol.json')['periods'][period]['input']
            calendar = TradingDates(m.old.read(path)['symbols'])
        path = root/'executions/evaluations.jsonl'
        sources[str(path)] = m.old.sha(path)
        for line in path.open(encoding='utf-8'):
            r = reprice(json.loads(line),fees,calendar)
            profile,sf = r['scenario'].split('/',1)
            r['period'] = period
            if m.report.s.ev(r)['status'] == 'closed': r['exit_trading_day'] = calendar.day(m.report.s.ev(r)['exit_ts'])
            data[profile,sf].append(r)
    assert len(events) == len(set(events))
    return controller,pp,events,ends,data,sources,coverage


def research():
    global MONTHS,FOLDS,VARIANTS,TEMPLATES
    p = m.old.read(RUN/'protocol.json')
    MONTHS=p['months'];FOLDS=p['folds'];VARIANTS=p['variants']
    TEMPLATES={k:v for k,v in TEMPLATES.items() if k in p.get('template_names',list(TEMPLATES))}
    permitted_hashes={p['script_sha256']} | {m.old.read(q)['script_sha256'] for q in RUN.glob('*amendment*.json')}
    assert m.old.sha(__file__) in permitted_hashes, 'Implementation changed after protocol; record amendment first'
    controller, pp, events, ends, data, sources, coverage = load()
    results = {}; accepted = {}; ledgers = {}; checks = []
    reference = m.old.read(ROOT/'research-runs/swing-universe-mix-20261026-v1/results.json') if p.get('scope')=='cost14' else m.old.read(MAY/'combined18-results.json')
    if p.get('scope')=='native2026':
        # Independent controls from original frozen individual executions, clean Jan start.
        c0,p0,e0,d0,raw,_,_=m.load();reference={}
        for pol in p['policies']:
            reference[pol]={}
            for model,profile,rule in [('B2','budget10','base'),('B5','budget_body','sector2_counter_stops')]:
                reference[pol][model]={}
                for sf in m.SUFFIXES:
                    reference[pol][model][sf]={}
                    for un,syms in [('four_EG_V_JD_SR',FOUR),('six_original',SIX)]:
                        rr=[z for z in raw['unmerged'][profile,sf] if z['period']!='older' and z['symbol'] in syms]
                        aa,_,_=c0.select(rr,e0['unmerged'],d0['unmerged'],p0['sector_map'],rule,pol)
                        reference[pol][model][sf][un]=metric(aa,e0['unmerged'])
    for univ,syms in [('four',FOUR),('six',SIX)]:
        for pol in p['policies']:
            for sf in m.SUFFIXES:
                bytemplate = {t:{r['event_key']:r for r in data[t,sf] if r['symbol'] in syms} for t in TEMPLATES}
                keys = list(bytemplate['C_B5'])
                assert all(set(d)==set(keys) for d in bytemplate.values())
                for name in VARIANTS:
                    rr = [bytemplate[route(name,bytemplate['C_B5'][k],events)][k] for k in keys]
                    rule = 'base' if name=='B2' else 'sector2_counter_stops'
                    aa,ll,risk = controller.select(rr,events,ends,pp['sector_map'],rule,pol)
                    m.report.audit(ll,rr)
                    case = '/'.join([univ,pol,sf,name])
                    z = metric(aa,events)
                    z['risk'] = {k:v for k,v in risk.items() if k!='realized_settlements'}
                    z['products'] = {s:metric([r for r in aa if r['symbol']==s],events) for s in syms}
                    results[case]=z; accepted[case]=aa
                    if pol=='original' and sf==m.PRIMARY: ledgers[case]=ll
                    if name in ['B2','B5']:
                        un = 'four_EG_V_JD_SR' if univ=='four' else 'six_original'
                        ref = reference[pol][name][sf][un]
                        for k in ['n','wins','losses','net','mean_r','realized_cash_drawdown','unknown','warning_month_net','bins']:
                            assert m.equal(z[k],ref[k]),(case,k,z[k],ref[k])
                        checks.append(dict(case=case, exact=True))
                    for month in MONTHS:
                        assert abs(sum(v['warning_month_net'][month] for v in z['products'].values())-z['warning_month_net'][month])<1e-6
    write('baseline-controls.json',checks)
    write('fixed-results.json',results)
    schedule=[]; training=[]
    for start,end in FOLDS:
        metrics = {}
        for name in VARIANTS:
            if name=='B2': continue
            metrics[name]={}
            for pol in p['policies']:
                for sf in m.SUFFIXES:
                    shadow=accepted['/'.join(['four',pol,sf,name])]
                    rs=[r for r in shadow if r['warning_ts']<start and m.report.s.ev(r)['status']=='closed' and m.report.s.ev(r)['exit_ts']<start]
                    assert all(m.report.s.ev(r)['exit_ts']<start for r in rs)
                    z=metric(rs,events,[mo for mo in MONTHS if mo<start[:7]])
                    z['unsettled_at_cutoff']=sum(bool(res) and res[0]<start<=res[1] for r in shadow if r['warning_ts']<start for res in [controller.reservation(r,ends,pol)])
                    metrics[name][pol+'/'+sf]=z
        primary = 'original/'+m.PRIMARY
        base = metrics['B5']; bp=base[primary]
        eligible=[]; reasons={}
        for name,cases in metrics.items():
            if name=='B5': continue
            q=cases[primary]
            fail=[]
            if q['n']<20: fail.append('train_closed<20')
            if q['agriculture_n']<5: fail.append('train_agriculture_closed<5')
            if not all(z['net']>=base[k]['net']-1e-7 for k,z in cases.items()): fail.append('some_execution_net_below_B5')
            if q['agriculture_net']<=bp['agriculture_net']+1e-7: fail.append('no_primary_agriculture_improvement')
            if q['positive_product_hhi'] is None or bp['positive_product_hhi'] is None or q['positive_product_hhi']>bp['positive_product_hhi']+1e-12: fail.append('product_HHI_worse_or_unavailable')
            if q['net_without_best_trade']<bp['net_without_best_trade']-1e-7: fail.append('ex_best_trade_net_below_B5')
            if not all(z['unsettled_at_cutoff']<=base[k]['unsettled_at_cutoff'] for k,z in cases.items()): fail.append('more_unsettled_at_cutoff')
            if fail: reasons[name]=fail; continue
            score=min(z['net_without_best_trade'] for z in cases.values())-.25*max(z['realized_cash_drawdown'] for z in cases.values())
            eligible.append((score,name))
        eligible.sort(key=lambda x:(-x[0],VARIANTS.index(x[1])))
        chosen=eligible[0][1] if eligible else 'B5'
        schedule.append(dict(start=start,end=end,variant=chosen))
        training.append(dict(cutoff=start,selected=chosen,eligible=eligible,rejection_reasons=reasons,visible_metrics=metrics))
    write('selection-before-test.json',dict(schedule=schedule,training=training))
    # No fold reset: build warning-time routes, then replay the complete stream.
    wf = {}; wf_accepted = {}; fold_metrics = {}
    for univ,syms in [('four',FOUR),('six',SIX)]:
        for pol in p['policies']:
            for sf in m.SUFFIXES:
                bt={t:{r['event_key']:r for r in data[t,sf] if r['symbol'] in syms} for t in TEMPLATES}
                rr=[]
                for k,row in bt['C_B5'].items():
                    name=next((f['variant'] for f in schedule if f['start']<=row['warning_ts']<f['end']),'B5')
                    rr.append(bt[route(name,row,events)][k])
                aa,ll,risk=controller.select(rr,events,ends,pp['sector_map'],'sector2_counter_stops',pol)
                m.report.audit(ll,rr)
                case='/'.join([univ,pol,sf]); wf_accepted[case]=aa
                scored=[r for r in aa if r['warning_ts']>=FOLDS[0][0]]
                test_months=[mo for mo in MONTHS if mo>=FOLDS[0][0][:7]]
                wf[case]=metric(scored,events,test_months)
                wf[case]['products']={s:metric([r for r in scored if r['symbol']==s],events,test_months) for s in syms}
                wf[case]['risk_full_history']={k:v for k,v in risk.items() if k!='realized_settlements'}
                fold_metrics[case]=[]
                for f in schedule:
                    rs=[r for r in aa if f['start']<=r['warning_ts']<f['end']]
                    b=[r for r in accepted[case+'/B5'] if f['start']<=r['warning_ts']<f['end']]
                    mos=[mo for mo in MONTHS if f['start'][:7]<=mo<f['end'][:7]]
                    fold_metrics[case].append(dict(**f,selected=metric(rs,events,mos),B5=metric(b,events,mos)))
                if pol=='original' and sf==m.PRIMARY: ledgers[case+'/walkforward']=ll
    write('walkforward-results.json',wf)
    write('fold-results.json',fold_metrics)
    write('opportunity-ledgers.json',ledgers)
    with (RUN/'accepted.jsonl').open('x',encoding='utf-8') as f:
        for case,rs in accepted.items():
            if '/original/'+m.PRIMARY+'/' not in case: continue
            for r in rs: f.write(json.dumps(dict(case=case,row=r),ensure_ascii=False)+'\n')
        for case,rs in wf_accepted.items():
            if '/original/'+m.PRIMARY not in case: continue
            for r in rs:f.write(json.dumps(dict(case=case+'/walkforward',row=r),ensure_ascii=False)+'\n')
    # Same opportunity vs changed execution vs recompetition decomposition.
    attr={}
    for name in VARIANTS:
        base={r['event_key']:r for r in accepted['four/original/'+m.PRIMARY+'/B5'] if m.report.s.ev(r)['status']=='closed'}
        new={r['event_key']:r for r in accepted['four/original/'+m.PRIMARY+'/'+name] if m.report.s.ev(r)['status']=='closed'}
        common=base.keys()&new.keys(); added=new.keys()-base.keys(); removed=base.keys()-new.keys()
        common_delta=sum(m.report.s.ev(new[k])['net_amount']-m.report.s.ev(base[k])['net_amount'] for k in common)
        an=sum(m.report.s.ev(new[k])['net_amount'] for k in added); rn=sum(m.report.s.ev(base[k])['net_amount'] for k in removed)
        delta=results['four/original/'+m.PRIMARY+'/'+name]['net']-results['four/original/'+m.PRIMARY+'/B5']['net']
        assert abs(delta-common_delta-an+rn)<1e-6
        attr[name]=dict(net_delta=delta,common_n=len(common),common_net_delta=common_delta,added_n=len(added),added_net=an,removed_n=len(removed),removed_net=rn,
                        added=[dict(event_key=k,symbol=new[k]['symbol'],warning_ts=new[k]['warning_ts'],evaluation=m.report.s.ev(new[k])) for k in sorted(added)],
                        changed_common=[dict(event_key=k,symbol=new[k]['symbol'],old=m.report.s.ev(base[k]),new=m.report.s.ev(new[k])) for k in sorted(common) if new[k]['result']!=base[k]['result']])
    write('attribution.json',attr)
    write('coverage.json',coverage)
    diffs=[q for q,h in p['production_before'].items() if m.old.sha(ROOT/q)!=h]
    assert not diffs,diffs
    write('verification.json',dict(exact_controls=len(checks),monthly_product_reconciliation=True,causal_interval_audits=True,
        strictly_visible_training_exits=True,no_boundary_reset=True,production_hash_differences=diffs,source_hashes=sources,
        warning='Historical OOS-like replay only. Full untouched OOS not available. No promise of generalization.'))
    print(json.dumps(dict(schedule=schedule,primary={n:{k:results['four/original/'+m.PRIMARY+'/'+n][k] for k in ['n','net','agriculture_net','realized_cash_drawdown','positive_product_hhi']} for n in VARIANTS},walkforward=wf['four/original/'+m.PRIMARY]),ensure_ascii=False),flush=True)


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('stage',choices=['prepare','build','recent','may_aug','sep_dec','research'])
    ap.add_argument('--futures-route',action='store_true')
    args=ap.parse_args();stage=args.stage
    acquisition.TRANSPORT['futures_route']=args.futures_route
    if stage in ['may_aug','sep_dec']: older(stage)
    else: globals()[stage]()
