"""Isolated, reproducible P2/P3 implementation repair and small-swing experiments.

Never edits the service, database, broker settings, or old research batches.
prepare freezes definitions BEFORE corrected outcomes; run and report are separate.
Both historical periods are already researched. No historical result promotes live.
"""
import argparse
import bisect
import collections
import datetime as dt
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
REPO = pathlib.Path(__file__).resolve().parents[1]
OLD = pathlib.Path('G:/待处理文件/交易系统/探索/P2P3分散优化-20261025')
RUN = REPO / 'research-runs/swing-consistency-20261025-v1'
from reprice_signal_costs import TradingDates, reprice
from report_paper_candidate_history import summary

UNKNOWN = {'ambiguous', 'missing_data', 'rollover', 'unfinished', 'pending'}


def read(p):
    return json.loads(pathlib.Path(p).read_text(encoding='utf-8'))


def sha(p):
    h = hashlib.sha256()
    with pathlib.Path(p).open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def write(p, value):
    with pathlib.Path(p).open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError('Source patch anchor changed: ' + old[:90])
    return text.replace(old, new)


def definitions():
    out = []
    for base, cap in [('P2', None), ('P3', 2.)]:
        variants = [('', {}), ('_be075', {'protect_after_r': .75, 'protect_lock_r': 0.}),
                    ('_lock1', {'protect_after_r': 1., 'protect_lock_r': .25}),
                    ('_trail1', {'protect_after_r': 1., 'protect_lock_r': 0., 'trail_distance_r': .75}),
                    ('_nearclose', {'skip_prebreak_entries': True}),
                    ('_weakclose', {'skip_prebreak_entries': True, 'exit_before_break_below_r': .5})]
        for suffix, rules in variants:
            out.append(dict(name=base+suffix, base=base, policy='original_s1', no_follow_bar=5,
                            rules=dict(target_cap_r=cap, min_actual_risk_ticks=10., flat_before_weekend=True, **rules)))
    return out


def patch_fork(root):
    derive = root/'crates/n-core/src/derive/mod.rs'
    s = derive.read_text(encoding='utf-8')
    s = replace_once(s, '(((elapsed_min + minutes - 1) / minutes).max(1)) * minutes',
                     '((elapsed_min + minutes - 1) / minutes) * minutes')
    derive.write_text(s, encoding='utf-8')
    ablation = root/'crates/n-core/src/v3/ablation.rs'
    s = ablation.read_text(encoding='utf-8')
    s = replace_once(s, '    pub entry_mode: EntryMode,', '''    pub entry_mode: EntryMode,
    pub protect_after_r: Option<f64>,
    pub protect_lock_r: Option<f64>,
    pub trail_distance_r: Option<f64>,
    /// Sensitivity only: minute OHLC cannot reconstruct the next real quote.
    pub next_minute_open: bool,''')
    # Record slot acquisition at trigger, including next-minute orders rejected at fill.
    s = replace_once(s, '        let fill = observed + sign * ex.slip_ticks * ev.tick_size;', '''        if self.e.trigger_ts.is_none() { self.e.trigger_ts = Some(ts.into()); }
        if self.rules.next_minute_open && self.e.trigger_ts.as_deref() == Some(ts) { return; }
        let fill = observed + sign * ex.slip_ticks * ev.tick_size;''')
    s = replace_once(s, '        self.excursion(end, sign);\n        if ex.policy', '''        self.excursion(end, sign);
        if self.rules.protect_after_r.is_some_and(|r| self.peak + 1e-9 >= r) {
            let lock = self.rules.protect_lock_r.unwrap_or(0.).max(
                self.rules.trail_distance_r.map_or(f64::NEG_INFINITY, |d| self.peak-d));
            // Protective sell stops round DOWN; buy stops round UP: never optimistic.
            let raw = fill + sign * lock * self.e.risk;
            let grid = if sign > 0. { (raw / ev.tick_size + 1e-9).floor() }
                       else { (raw / ev.tick_size - 1e-9).ceil() } * ev.tick_size;
            if (grid-self.stop)*sign > 0. { self.stop=grid; self.first=true; }
        }
        if ex.policy''')
    # There are two entry-mode matches; alter only the OPEN branch.
    s = replace_once(s, '            match self.rules.entry_mode {\n                EntryMode::Touch => {\n                    if (price', '''            if self.rules.next_minute_open && self.e.trigger_ts.as_deref().is_some_and(|t| t < ts) {
                self.enter(price, ev, ex, sign, ts);
            }
            if self.e.fill.is_none() { match self.rules.entry_mode {
                EntryMode::Touch => {
                    if (price''')
    s = replace_once(s, '        if self.terminal() || self.e.fill.is_none() {\n            return;\n        }\n        if (price', '''        }
        if self.terminal() || self.e.fill.is_none() {
            return;
        }
        if (price''')
    s = replace_once(s, '            if gap > 5 && gap <= 180 {\n                let missing = prior + chrono::Duration::minutes(5);',
                     '            if gap > 1 {\n                let missing = prior + chrono::Duration::minutes(1);')
    s = replace_once(s, '''                if let Some(local) = chrono::Local.from_local_datetime(&missing).single() {
                    if crate::session::SessionCalendar::is_trading_time(&ev.symbol, &local) {
                        state.finish("missing_data", &bar.datetime);
                        break;
                    }
                }''', '''                let mut missing = missing;
                let mut absent = false;
                while missing < dt {
                    let label = missing.format("%Y-%m-%d %H:%M:%S").to_string();
                    let holiday = holidays.iter().any(|h| label.as_str() > h.last_close.as_str() && label.as_str() < h.reopen.as_str());
                    // SF/SM archive is day-only, as in pre_weekend above.
                    let day_only = matches!(ev.symbol.trim_end_matches(|c:char|c.is_ascii_digit()), "SF"|"SM") && (missing.hour()>=20 || missing.hour()<6);
                    let m = missing.hour()*60+missing.minute();
                    // Quote calendar includes the closing minute; minute bars use
                    // interval END labels. Explicit boundaries prevent 11:31 etc.
                    let night_end = crate::session::SessionCalendar::get(&ev.symbol).night_type;
                    use crate::session::spec::NightSessionType::*;
                    let end_label = (541..=615).contains(&m) || (631..=690).contains(&m) || (811..=900).contains(&m)
                        || (!day_only && match night_end {
                            None => false, Close2300 => (1261..=1380).contains(&m),
                            Close2330 => (1261..=1410).contains(&m),
                            Close0100 => m>=1261 || m<=60, Close0230 => m>=1261 || m<=150 });
                    if end_label && !holiday && !day_only && chrono::Local.from_local_datetime(&missing).single()
                        .is_some_and(|local| crate::session::SessionCalendar::is_trading_time(&ev.symbol, &local)) {
                        absent = true; break;
                    }
                    missing += chrono::Duration::minutes(1);
                }
                if absent { state.finish("missing_data", &bar.datetime); break; }''')
    # First missing minute after warning is checked as well.
    s = replace_once(s, '    let mut previous: Option<NaiveDateTime> = None;',
                     '    let mut previous = NaiveDateTime::parse_from_str(&ev.warning_ts, "%Y-%m-%d %H:%M:%S").ok();')
    s = replace_once(s, '    a.e.status == b.e.status',
                     '    a.e.trigger_ts == b.e.trigger_ts && a.e.status == b.e.status')
    ablation.write_text(s, encoding='utf-8')
    evalpath = root/'crates/n-core/src/v3/replay.rs'
    s = evalpath.read_text(encoding='utf-8')
    s = replace_once(s, '    pub entry_ts: Option<String>,', '    #[serde(default)]\n    pub trigger_ts: Option<String>,\n    pub entry_ts: Option<String>,')
    s = replace_once(s, '            entry_ts: None,', '            trigger_ts: None,\n            entry_ts: None,')
    evalpath.write_text(s, encoding='utf-8')
    service=root/'crates/n-core/src/service/mod.rs'
    service.write_text((REPO/'crates/n-core/src/service/mod.rs').read_text(encoding='utf-8')+'\n'+
                       (REPO/'scripts/swing_service_dedup.rs').read_text(encoding='utf-8'),encoding='utf-8')
    shutil.copy2(REPO/'scripts/swing_dedup_runner.rs',root/'crates/n-core/src/bin/swing_dedup_runner.rs')


def prepare():
    RUN.mkdir(parents=True, exist_ok=False)
    root = RUN/'fork'
    for name in ('n-core', 'n-protocol'):
        shutil.copytree(REPO/'crates'/name, root/'crates'/name)
    shutil.copy2(REPO/'Cargo.lock', root/'Cargo.lock')
    (root/'Cargo.toml').write_text('[workspace]\nresolver="2"\nmembers=["crates/n-core","crates/n-protocol"]\n[workspace.package]\nversion="3.0.1"\nedition="2021"\n', encoding='utf-8')
    patch_fork(root)
    shutil.copy2(REPO/'scripts/swing_consistency_runner.rs', root/'crates/n-core/src/bin/swing_consistency_runner.rs')
    (root/'crates/n-core/tests').mkdir(exist_ok=True)
    shutil.copy2(REPO/'tests/swing_repair.rs', root/'crates/n-core/tests/swing_repair.rs')
    old = read(OLD/'protocol.json')
    recent = old['paths']
    early = dict(input=str(OLD/'external/input.json'), signals=str(OLD/'external/signals.json'),
                 contracts=str(OLD/'external/contracts.json'), segments=str(OLD/'external/segments.json'),
                 holidays=str(OLD/'external/holidays.json'), fees=recent['fees'])
    files = [f for base in ('crates', 'src-tauri/src', 'research-protocols') for f in (REPO/base).rglob('*')
             if f.is_file() and f.suffix in ('.rs', '.toml', '.json')]
    files += list(REPO.glob('Cargo.*')) + list(REPO.glob('config*.toml'))
    p = dict(id=RUN.name, created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
             templates=definitions(), periods=dict(recent=recent, early=early),
             modes=['corrected_touch', 'next_minute_open'],
             cluster_variants=['one_attempt', 'stop_after_loss'],
             historical_status='Both periods already inspected; all outcomes are exploratory. No independent historical holdout claim.',
             slot='Acquire at first trigger; terminal pre-fill rejection releases next timestamp. Same-minute exit/trigger ordering is conservative; reverse tie stress reported.',
             dedup='Exact live candidate identity: symbol,direction,S0,S1,S2,warning. No earliest-AB suppression.',
             atr='Live candidate completed triples, SMA14 true range, no rollover reset; warning_complete required. ATR20 for pattern extraction unchanged.',
             exposure='Unknown fill/ordering reserves through last available contract quote; never count unknown as zero.',
             selection='Choose at most one exit and one cluster candidate using recent history only; freeze before early result reporting. 80% recent net at BOTH costs, win rate no lower, drawdown no higher, unknown no higher; reduce at least two concentration measures. Early must reduce loss and drawdown. No live promotion.',
             group_rules='No per-product tuning. Scale by actual R; descriptors risk/ATR, A efficiency, friction/R, prior observed session-gap/ATR. Leave-product-out diagnostic only.',
             prospective='Freeze eligible candidates or no-candidate outcome; append-only future paper ledger with unchanged rule hashes, at least 60 trading days and 100 closed trades before review.',
             production_before={str(f):sha(f) for f in files},
             hashes={period:{k:sha(v) for k,v in paths.items()} for period,paths in [('recent',recent),('early',early)]})
    write(RUN/'protocol.json', p)
    write(RUN/'fork-manifest.json', {str(f.relative_to(root)):sha(f) for f in root.rglob('*') if f.is_file()})
    write(RUN/'analysis-protocol.json',dict(descriptor_thresholds=dict(risk_atr=1.25,a_eff=.4,friction_r=.15,prior20_gap_atr=.5),
          selection_retention='At least 80% of both original positive recent baseline and corrected positive baseline; other gates compare corrected baseline',
          early_validation='Frozen recent shortlist only; net >= corrected baseline, drawdown <= baseline, unknown <= baseline at both costs and modes',
          purpose='Descriptive grouping; thresholds never optimized against outcomes'))
    print('Prepared isolated fork and frozen protocol:', RUN, flush=True)


def run():
    p = read(RUN/'protocol.json')
    subprocess.run(['cargo', 'build', '--offline', '--manifest-path', str(RUN/'fork/Cargo.toml'),
                    '--target-dir', str(REPO/'target'), '-p', 'n-core', '--bin', 'swing_consistency_runner'], check=True)
    exe = REPO/'target/debug/swing_consistency_runner.exe'
    for period in ('recent', 'early'):
        for k, h in p['hashes'][period].items():
            assert sha(p['periods'][period][k]) == h, (period, k)
        subprocess.run([str(exe), str(RUN/'protocol.json'), period, str(RUN/period)], check=True)
    dedup()


def dedup():
    subprocess.run(['cargo','build','--offline','--manifest-path',str(RUN/'fork/Cargo.toml'),
                    '--target-dir',str(REPO/'target'),'-p','n-core','--bin','swing_dedup_runner'],check=True)
    for period in ('recent','early'):
        subprocess.run([str(REPO/'target/debug/swing_dedup_runner.exe'),str(RUN/'protocol.json'),period,str(RUN/period/'service-dedup-raw-plan.json')],check=True)


def sync_fork():
    """Repair implementation before any batch outcome; never silently patch a completed run."""
    assert not any((RUN/x).exists() for x in ('recent','early')), 'Use a new batch after outcomes exist'
    root=RUN/'fork'
    for name in ('derive/mod.rs','v3/ablation.rs','v3/replay.rs'):
        shutil.copy2(REPO/'crates/n-core/src'/name,root/'crates/n-core/src'/name)
    patch_fork(root)
    shutil.copy2(REPO/'scripts/swing_consistency_runner.rs',root/'crates/n-core/src/bin/swing_consistency_runner.rs')
    (root/'crates/n-core/tests').mkdir(exist_ok=True)
    shutil.copy2(REPO/'tests/swing_repair.rs',root/'crates/n-core/tests/swing_repair.rs')
    write(RUN/('fork-revision-'+dt.datetime.now().strftime('%H%M%S')+'.json'),
          dict(reason='Compilation and boundary-test fixes before any corrected outcome; candidate definitions unchanged',
               files={str(f.relative_to(root)):sha(f) for f in root.rglob('*') if f.is_file()}))


def ev(r):
    return r['result']['evaluation']


def cluster(e):
    return '|'.join(e[k] for k in ('symbol', 'direction', 's0_ts', 's1_ts'))


def portfolio(rows, events, contract_ends, mode='', reverse=False):
    """Trigger-time slot; cluster restrictions use ACCEPTED attempts/REALIZED loss only."""
    active = {}; attempts = collections.Counter(); realized = collections.defaultdict(float)
    pending = []; accepted = []; ledger = []
    rows = sorted(rows, key=lambda r:r['event_key'], reverse=reverse)
    rows.sort(key=lambda r:(ev(r).get('trigger_ts') or ev(r)['entry_ts'] or r['warning_ts'], r['warning_ts']))
    for r in rows:
        e = ev(r); start = e.get('trigger_ts') or e['entry_ts']
        if not start and e['status'] not in UNKNOWN:
            continue
        start = start or r['warning_ts']
        end = contract_ends[r['execution_contract']] if e['status'] in UNKNOWN else e['exit_ts']
        assert end and start <= end
        remaining = []
        for t, c, net in pending:
            if t < start: realized[c] += net
            else: remaining.append((t,c,net))
        pending = remaining
        c = cluster(events[r['event_key']]); sym = r['symbol']
        reason = ('symbol_slot' if active.get(sym, '') >= start else
                  'cluster_attempt' if mode == 'one_attempt' and attempts[c] else
                  'cluster_realized_loss' if mode == 'stop_after_loss' and realized[c] < 0 else None)
        ledger.append(dict(event_key=r['event_key'], cluster=c, start=start, end=end, accepted=reason is None, reason=reason))
        if reason: continue
        accepted.append(r); active[sym] = end; attempts[c] += 1
        if e['status'] == 'closed': pending.append((e['exit_ts'], c, e['net_amount']))
    return accepted, ledger


def metrics(rows):
    out = summary(rows)
    known = [r for r in rows if ev(r)['status'] == 'closed']
    groups = {}
    for name, fun in [('product',lambda r:r['symbol']), ('day',lambda r:r['exit_trading_day']),
                      ('month',lambda r:r['exit_trading_day'][:7])]:
        bins = collections.defaultdict(float)
        for r in known: bins[fun(r)] += ev(r)['net_amount']
        groups[name] = dict(sorted(bins.items()))
    def share(values, n):
        xs = sorted((v for v in values if v > 0), reverse=True)
        return sum(xs[:n])/sum(xs) if xs else 1.
    out.update(unknown=sum(ev(r)['status'] in UNKNOWN for r in rows), bins=groups,
               top3_product=share(groups['product'].values(),3), top3_day=share(groups['day'].values(),3),
               top5_trade=share([ev(r)['net_amount'] for r in known],5))
    for label,n,values in [('best5',5,[ev(r)['net_amount'] for r in known]),
                            ('best3days',3,list(groups['day'].values())), ('best3products',3,list(groups['product'].values()))]:
        out['without_'+label] = out['net']-sum(sorted(values,reverse=True)[:n])
    out['gap_stop_extra'] = sum(max(0., -(ev(r)['gross_points']/ev(r)['volume'])-ev(r)['risk']-r['slip']*r['tick_size'])*r['multiplier']
                               for r in known if ev(r)['exit_reason']=='stop')
    return out


def recent_gate(m,b):
    return (m['n'] >= 35 and m['net'] > 0 and m['net'] >= .8*b['net']
            and m['win_rate'] >= b['win_rate'] and m['realized_cash_drawdown'] <= b['realized_cash_drawdown']
            and m['unknown'] <= b['unknown']
            and sum(m[k] < b[k]-1e-9 for k in ('top3_product','top3_day','top5_trade')) >= 2)


def report():
    p = read(RUN/'protocol.json'); fees=read(p['periods']['recent']['fees'])
    original_recent=read(OLD/'development-results.json')
    results={}; accepted_all={}; audit={}; ledgers_all={}; feature_groups={}; checks={}
    # Selection is frozen after recent only, before opening early evaluations.
    for period in ('recent','early'):
        source=read(p['periods'][period]['input']); cal=TradingDates(source['symbols'])
        snapshots={r['event_key']:r for r in (json.loads(s) for s in (RUN/period/'events.jsonl').read_text(encoding='utf-8').splitlines())}
        events={k:r['event'] for k,r in snapshots.items()}
        prior_gaps={}
        for sym,raw in source['symbols'].items():
            gaps=[];times=[];rolls=[r['ts'] for r in raw['rollovers'] if r['confirmed']]
            for a,b in zip(raw['bars'],raw['bars'][1:]):
                if (dt.datetime.fromisoformat(b['datetime'])-dt.datetime.fromisoformat(a['datetime'])).total_seconds()>300:
                    if not any(a['datetime']<t<=b['datetime'] for t in rolls):
                        times.append(b['datetime']);gaps.append(abs(b['open']-a['close']))
            prior_gaps[sym]=(times,gaps)
        for k,snap in snapshots.items():
            e=snap['event'];ts,gaps=prior_gaps[e['symbol']];i=bisect.bisect_right(ts,e['warning_ts'])
            snap['prior20_gap_atr']=max(gaps[max(0,i-20):i],default=0)/snap['atr14']
        ends=read(RUN/period/'manifest.json')['contract_ends']; cases=collections.defaultdict(list)
        mask=read(RUN/period/'service-dedup-raw-plan.json')
        service_keys={k for v in mask['symbols'].values() for k in v['keys']}
        with (RUN/period/'evaluations.jsonl').open(encoding='utf-8') as f:
            for line in f:
                r=reprice(json.loads(line),fees,cal)
                if r['event_key'] not in service_keys: continue
                if ev(r)['status']=='closed': r['exit_trading_day']=cal.day(ev(r)['exit_ts'])
                cases[r['scenario']].append(r)
        metrics_all={}; accepted_all[period]={}; ledgers_all[period]={}
        for name,rows in cases.items():
            for cmode in (['','one_attempt','stop_after_loss'] if name.split('/')[0] in ('P2','P3') else ['']):
                label=name + ('/'+cmode if cmode else '')
                accepted,ledger=portfolio(rows,events,ends,cmode)
                m=metrics(accepted); reverse,_=portfolio(rows,events,ends,cmode,True)
                m['reverse_tie_net']=metrics(reverse)['net']
                metrics_all[label]=m;accepted_all[period][label]=accepted;ledgers_all[period][label]=ledger
        results[period]=metrics_all
        feature_groups[period]={}
        for base in ('P2','P3'):
            rows=accepted_all[period][base+'/corrected_touch/slip1']
            groupdefs={
                'risk_atr_low':lambda s:s['risk_atr']<=1.25,
                'risk_atr_high':lambda s:s['risk_atr']>1.25,
                'a_eff_low':lambda s:s['a_eff']<.4,
                'a_eff_high':lambda s:s['a_eff']>=.4,
                'friction_low':lambda s:s['friction_r']<=.15,
                'friction_high':lambda s:s['friction_r']>.15,
                'gap_low':lambda s:s['prior20_gap_atr']<.5,
                'gap_high':lambda s:s['prior20_gap_atr']>=.5}
            feature_groups[period][base]={label:metrics([r for r in rows if fn(snapshots[r['event_key']])]) for label,fn in groupdefs.items()}
        if period=='recent':
            eligible=[]
            for name in [t['name'] for t in p['templates'] if t['name'] not in ('P2','P3')]+[b+'/'+c for b in ('P2','P3') for c in p['cluster_variants']]:
                base=name[:2]; okay=True
                for mode in p['modes']:
                    for slip in (1,2):
                        suffix='/'+name.split('/',1)[1] if '/' in name else ''
                        key=f"{name.split('/')[0]}/{mode}/slip{slip}"+suffix
                        benchmark=dict(metrics_all[f'{base}/{mode}/slip{slip}'])
                        benchmark['net']=max(benchmark['net'],original_recent[base][str(slip)]['all']['net'])
                        okay &= recent_gate(metrics_all[key],benchmark)
                if okay: eligible.append(name)
            # No ranking by early outcomes. A failed gate yields no candidate.
            shortlist=dict(selected=eligible[:2], eligible=eligible, created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                           early_evaluations_consulted=False, protocol_sha256=sha(RUN/'protocol.json'), qualified_for_trading=False)
            write(RUN/'shortlist.json',shortlist)
        # Causal cluster diagnostics: accepted baseline, repeat losses and MFE giveback.
        diagnostics={}
        for base in ('P2','P3'):
            rows=accepted_all[period][base+'/corrected_touch/slip1']; groups=collections.defaultdict(list)
            for r in rows: groups[cluster(events[r['event_key']])].append(r)
            closed=[r for r in rows if ev(r)['status']=='closed']
            diagnostics[base]=dict(clusters=len(groups), repeated_clusters=sum(len(v)>1 for v in groups.values()),
                repeat_trades=sum(max(0,len(v)-1) for v in groups.values()),
                repeated_cluster_net=sum(ev(r)['net_amount'] for v in groups.values() if len(v)>1 for r in v if ev(r)['status']=='closed'),
                mfe_then_loss={str(v):dict(n=sum(ev(r)['mfe_r']>=v and ev(r)['net_amount']<0 for r in closed),
                                           net=sum(ev(r)['net_amount'] for r in closed if ev(r)['mfe_r']>=v and ev(r)['net_amount']<0)) for v in (.5,.75,1.)},
                cluster_ledger=[dict(cluster=c,events=[r['event_key'] for r in v],net=sum(ev(r)['net_amount'] for r in v if ev(r)['status']=='closed')) for c,v in groups.items()])
        audit[period]=diagnostics
        # Validate fills against the pinned MONTH contract, not continuous alias.
        contracts=read(p['periods'][period]['contracts'])
        needed=collections.defaultdict(set)
        for rows in accepted_all[period].values():
            for r in rows:
                needed[r['execution_contract']].update(f['ts'] for f in r['result']['fills'])
        index={code:{b['datetime']:b for b in contracts[code]['minutes'] if b['datetime'] in times} for code,times in needed.items()}
        del contracts
        nfill=nclosed=0
        for case,rows in accepted_all[period].items():
            for r in rows:
                e=ev(r);sign=1 if r['direction']=='up' else -1
                for fill in r['result']['fills']:
                    assert fill['ts']>r['warning_ts']
                    bar=index[r['execution_contract']][fill['ts']]
                    assert bar['volume']>0,(period,case,r['event_key'],fill)
                    assert abs(fill['price']/r['tick_size']-round(fill['price']/r['tick_size']))<1e-6
                    nfill+=1
                if e['entry_ts'] and 'next_minute_open' in case:
                    assert e['entry_ts']>e['trigger_ts']
                if e['status']=='closed':
                    fills=r['result']['fills'];gross=sum((f['price']-fills[0]['price'])*sign*f['volume']*r['multiplier'] for f in fills[1:])
                    assert abs(gross-r['cost_repricing']['commission']-e['net_amount'])<1e-6
                    nclosed+=1
        checks[period]=dict(fill_checks=nfill,closed_cash_checks=nclosed,manifest=read(RUN/period/'manifest.json'))
    write(RUN/'results.json',results); write(RUN/'cluster-diagnostics.json',audit)
    write(RUN/'feature-groups.json',feature_groups)
    frozen=read(RUN/'shortlist.json'); validation={}
    for name in frozen['selected']:
        base=name[:2];values={}
        for mode in p['modes']:
            for slip in (1,2):
                suffix='/'+name.split('/',1)[1] if '/' in name else ''
                key=f"{name.split('/')[0]}/{mode}/slip{slip}"+suffix
                m=results['early'][key];b=results['early'][f'{base}/{mode}/slip{slip}']
                values[f'{mode}/slip{slip}']=(m['net']>=b['net'] and m['realized_cash_drawdown']<=b['realized_cash_drawdown'] and m['unknown']<=b['unknown'])
        validation[name]=dict(cases=values,passed=all(values.values()))
    write(RUN/'validation.json',validation)
    with (RUN/'accepted.jsonl').open('x',encoding='utf-8') as f:
        for period,cases in accepted_all.items():
            for case,rows in cases.items():
                for r in rows: f.write(json.dumps(dict(period=period,case=case,row=r),ensure_ascii=False)+'\n')
    write(RUN/'opportunity-ledgers.json',ledgers_all)
    # Leave one product out: report every product, never tune exceptions to it.
    leave={}
    for period,cases in accepted_all.items():
        leave[period]={case:{sym:metrics([r for r in rows if r['symbol']!=sym]) for sym in sorted({r['symbol'] for r in rows})}
                       for case,rows in cases.items() if case.split('/')[0] in ('P2','P3')}
    write(RUN/'leave-product-out.json',leave)
    changed=[f for f,h in p['production_before'].items() if sha(f)!=h]
    assert not changed, changed
    write(RUN/'verification.json',dict(production_files_unchanged=len(p['production_before']), production_changed=False,checks=checks,
          protocol_sha256=sha(RUN/'protocol.json'), results_sha256=sha(RUN/'results.json'), qualified_for_trading=False))
    render_report(p,results,audit)


def render_report(p, results, audit):
    lines=['# 小波段策略修复与退出研究（独立副本）','',
           '原系统源码、实盘配置未改动。两个时期都已被研究，本轮全部结果属于探索；没有新增独立样本。', '',
           '修复：00:00聚合；完整15分钟ATR14采用候选服务连续口径；调用服务原有相似预警抑制与状态推进函数，按完整历史逐根收盘线前推，再按执行器身份去重；触发时占用机会；逐分钟缺口检查；未知敞口保留到合约数据末尾。', '',
           'corrected_touch 为修正后的分钟触价模型；next_minute_open 为触发后下一分钟开盘成交压力模型，均每边1/2tick并扣冻结费用。真实下一报价和盘口无法由分钟OHLC还原，未宣称完成实盘等价复刻。','',
           '回撤为已平仓现金回撤；金额是品种机会汇总，无保证金/资金约束。未知敞口不按零盈亏处理。','']
    lines += ['## 原研究与修复后基线','',
              '|时期/规则/1tick|原研究净额|修复后净额|修复后未知|', '|---|---:|---:|---:|']
    for period,oldpath in [('recent',OLD/'development-results.json'),('early',OLD/'external-results.json')]:
        old=read(oldpath)
        for base in ('P2','P3'):
            m=results[period][base+'/corrected_touch/slip1']
            lines.append(f"|{period}/{base}|{old[base]['1']['all']['net']:.2f}|{m['net']:.2f}|{m['unknown']}|")
    lines += ['', '这张表同时包含实现口径和未知敞口占用方式变化，不将差额归为策略优化收益。','']
    for period,label in [('recent','7—10月'),('early','1—6月')]:
        lines += ['## '+label,'','|规则（模式/每边tick）|笔数|净额|胜率|已实现回撤|未知|前三品种占比|前三日占比|前五单占比|',
                  '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
        for name,m in results[period].items():
            lines.append(f"|{name}|{m['n']}|{m['net']:.2f}|{(m['win_rate'] or 0):.2%}|{m['realized_cash_drawdown']:.2f}|{m['unknown']}|{m['top3_product']:.2%}|{m['top3_day']:.2%}|{m['top5_trade']:.2%}|")
        lines += ['', '交易簇诊断（corrected_touch/1tick）：','']
        for base,d in audit[period].items():
            lines.append(f"- {base}：{d['clusters']}簇，{d['repeated_clusters']}簇重复进场，额外{d['repeat_trades']}笔；重复簇合计{d['repeated_cluster_net']:.2f}元。")
            for threshold,v in d['mfe_then_loss'].items():
                lines.append(f"  浮盈达到{threshold}R后净亏：{v['n']}笔，合计{v['net']:.2f}元。")
        lines.append('')
    s=read(RUN/'shortlist.json')
    fg=read(RUN/'feature-groups.json')
    lines += ['## 共用尺度分组（诊断）','','阈值在协议中预先固定；使用当时已知的风险/ATR、A段效率、摩擦/R和此前20次已观察会话间隙的最大跳空/当前ATR。无逐品种独立参数。','',
              '|分组/P2/1tick|近期笔数/净额|上半年笔数/净额|','|---|---:|---:|']
    for group,m in fg['recent']['P2'].items():
        early=fg['early']['P2'][group]
        lines.append(f"|{group}|{m['n']}/{m['net']:.2f}|{early['n']}/{early['net']:.2f}|")
    lines += ['', '分组结果仅用于检查机制，不根据本表临时新增品种白名单或拟合阈值。','']
    validation=read(RUN/'validation.json')
    passed=[name for name,v in validation.items() if v['passed']]
    lines += ['## 冻结结论','', '通过近期双成本、双成交模式全部门槛的候选：'+('、'.join(s['eligible']) if s['eligible'] else '无。'),'',
              '近期收益保留门槛同时对照原正收益基线和修复后基线；胜率、回撤、未知敞口与集中度对照修复后基线。', '',
              '通过冻结名单上半年复核、可进入后续纸面观察的候选：'+('、'.join(passed) if passed else '无。'),'',
              '未达标的防守规则仅保留诊断，不进入实盘。没有增加“A段必须突破此前高低点”，没有调整评分参数或逐品种选参。', '',
              '后续纸面记录冻结窗口至少60个交易日、100笔已平仓。记录警报、触发报价、下一报价、买卖盘、拒单/竞争原因、成交及费用，按固定规则对比；达到数量后复核，不自动上线。','',
              '复现：`python scripts/research_swing_consistency.py prepare` → `run` → `report`。输出不可覆盖；重跑需更改RUN批次路径。输入和独立副本哈希见protocol.json、fork-manifest.json。', '',
              '限制：分钟级独立全量校验未完成；两条OHLC单调路径不能穷尽全部振荡；同分钟机会顺序通过反序敏感性报告，真实报价排序仍待前瞻记录。服务去重对齐的是收盘线前推路径；实盘盘中报价可能提前改变上游事件状态，仍需逐笔档案复核。']
    (RUN/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    write(RUN/'paper-freeze.json',dict(protocol_sha256=sha(RUN/'protocol.json'), selected=passed,
          status='no_candidate' if not passed else 'paper_only', min_trading_days=60,min_closed_trades=100,
          production_changed=False, qualified_for_trading=False))
    print('Report:',RUN/'report.md', 'eligible:',s['eligible'],flush=True)


def audit():
    """Attribute the upstream population change; no new candidate selection."""
    p=read(RUN/'protocol.json');fees=read(p['periods']['recent']['fees']);result={}
    for period in ('recent','early'):
        source=read(p['periods'][period]['input']);cal=TradingDates(source['symbols'])
        events={r['event_key']:r['event'] for r in (json.loads(s) for s in (RUN/period/'events.jsonl').read_text(encoding='utf-8').splitlines())}
        ends=read(RUN/period/'manifest.json')['contract_ends'];cases=collections.defaultdict(list)
        for line in (RUN/period/'evaluations.jsonl').open(encoding='utf-8'):
            r=json.loads(line)
            if r['scenario'].split('/')[0] not in ('P2','P3'):continue
            r=reprice(r,fees,cal)
            if ev(r)['status']=='closed':r['exit_trading_day']=cal.day(ev(r)['exit_ts'])
            cases[r['scenario']].append(r)
        result[period]={}
        for case,rows in cases.items():
            accepted,_=portfolio(rows,events,ends)
            result[period][case]=metrics(accepted)
    write(RUN/'without-upstream-dedup-sensitivity.json',result)
    corrected=read(RUN/'results.json');clusters=read(RUN/'cluster-diagnostics.json')
    lines=['## 结果摘要与去重归因','',
           '已完成独立研究副本修复和本轮优化验证；本轮没有候选达到全部门槛，未修改实盘。','',
           '|每边1tick|原研究近期净额|修复后未加上游去重|修复后加入上游去重|上半年统一基线|',
           '|---|---:|---:|---:|---:|']
    old=read(OLD/'development-results.json')
    for base in ('P2','P3'):
        case=base+'/corrected_touch/slip1'
        lines.append(f"|{base}|{old[base]['1']['all']['net']:.2f}|{result['recent'][case]['net']:.2f}|{corrected['recent'][case]['net']:.2f}|{corrected['early'][case]['net']:.2f}|")
    lines += ['', '中间两列只差上游服务去重掩码；其他实现修复共同保持。原研究到中间列同时含午夜、ATR、触发占位和缺失检测等变化，不能拆成单项修复收益。', '',
              '规则定义：one_attempt 每个同品种/方向/S0/S1簇最多接受一次触发尝试；stop_after_loss 同簇累计已实现净额为负后阻止新尝试。be075 在浮盈0.75R后将止损移至入场；lock1 在浮盈1R后锁0.25R；trail1 在浮盈1R后追踪距峰值0.75R。所有保护按tick取不利方向，跳空按观察价退出。', '',
              'nearclose 阻止距预计长休市不足10分钟的新开；weakclose 另在该窗口退出当前浮盈不足0.5R的持仓。保留原S1/P3目标、初始止损、无跟随和周末/长假规则。','',
              '重复簇总净额包含簇内第一笔，不能当作“去掉重复单可以挽回”的金额。干预效果以one_attempt/stop_after_loss的完整因果重放为准。浮盈后转亏金额同样是诊断，不是可以无代价回收的利润。','',
              '留品种复核见leave-product-out.json，双向同分钟排序结果一致。跳空分组阈值在两个时期均使绝大多数交易落入高组，辨识度不足，未据此生成品种适配规则。','',
              '真实盘口、逐笔下一报价、盘中上游状态变化和分钟跨源全量核验仍未闭环，因此本轮是更接近服务口径的离线基线，不是已证实的实盘等价引擎。','']
    report=RUN/'report.md';text=report.read_text(encoding='utf-8')
    report.write_text(text.split('\n',1)[0]+'\n\n'+'\n'.join(lines)+text.split('\n',1)[1],encoding='utf-8')
    changed=[f for f,h in p['production_before'].items() if sha(f)!=h];assert not changed,changed
    write(RUN/'final-verification.json',dict(production_files_unchanged=len(p['production_before']),production_changed=False,
          code_sha256={str(f.relative_to(REPO)):sha(f) for f in [pathlib.Path(__file__),*list((REPO/'scripts').glob('swing_*.rs')),REPO/'tests/swing_repair.rs',REPO/'tests/test_swing_consistency.py']},
          report_sha256=sha(report),results_sha256=sha(RUN/'results.json'),python_tests=5,rust_boundary_tests=6,
          raw_evaluations={period:read(RUN/period/'manifest.json')['evaluations'] for period in ('recent','early')},
          qualified_for_trading=False))
    print('Final audit complete',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',choices=['prepare','sync_fork','run','dedup','report','audit'])
    args=parser.parse_args()
    globals()[args.stage]()
