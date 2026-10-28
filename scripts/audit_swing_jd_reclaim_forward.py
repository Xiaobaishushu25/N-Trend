"""All-decision, exit-independent JD reclaim path audit. No strategy changes."""
import sys
sys.dont_write_bytecode = True
import bisect
import collections
import datetime as dt
import json
import statistics as st
import unittest
import research_swing_jd_reclaim_routing as routing
import audit_swing_jd_signal_paths as paths

r = routing.x.r
R = r.R
RUN = r.b.ROOT / 'research-runs/swing-jd-reclaim-forward-20261027-v1'
HORIZONS = [4, 8, 16, 32]


def write(name, obj):
    r.b.z.s.m.old.write(RUN / name, obj)


def forward(contracts, dates, segments, code, ts, n):
    """Require every expected minute; never skip gaps, zero volume or rolls."""
    labels = [f'{d} {m//60:02}:{m%60:02}:00' for d in dates for m in paths.SLOTS]
    start = bisect.bisect_right(labels, ts)
    lookup = contracts[code]
    out = []
    for label in labels[start:start+n]:
        member = next((s['underlying'] for s in segments if s['first'] <= label <= s['last']), None)
        if member != code:
            return out, 'roll_or_continuous_coverage_end'
        end = dt.datetime.fromisoformat(label)
        group = []
        for j in range(14, -1, -1):
            minute = (end-dt.timedelta(minutes=j)).isoformat(sep=' ')
            q = lookup.get(minute)
            if q is None:
                return out, 'missing_minute_or_archive_end'
            if q['volume'] <= 0:
                return out, 'zero_volume'
            group.append(q)
        out.append(group)
    return out, 'ok' if len(out) == n else 'calendar_end'


def measures(raw, entry, risk, sign):
    assert risk > 0 and raw
    return dict(**paths.excursion(raw, entry, risk, sign),
                race1=paths.barrier(raw, entry, risk, sign)[0],
                race2=paths.barrier(raw, entry, risk, sign, upper=2)[0])


def summarize(rows):
    good = [q for q in rows if q['status'] == 'ok']
    out = dict(total=len(rows), complete=len(good),
               censored=dict(collections.Counter(q['status'] for q in rows if q['status'] != 'ok')),
               clusters=len({q['cluster'] for q in good}))
    for line in ['signal', 'execution']:
        xs = [q[line] for q in good if q.get(line)]
        out[line] = dict(n=len(xs))
        if not xs:
            continue
        out[line].update({k: dict(mean=st.mean(q[k] for q in xs), median=st.median(q[k] for q in xs))
                          for k in ['mfe', 'mae', 'end_r']})
        for k in ['race1', 'race2']:
            out[line][k] = dict(collections.Counter(q[k] for q in xs))
        out[line]['positive_end'] = sum(q['end_r'] > 0 for q in xs)
    return out


def audit(period, contracts, dates, segments, pool, dataset):
    lookup = {c: {q['datetime']: q for q in z['minutes']} for c, z in contracts.items()}
    evaluations = {q['event_key']: q for q in dataset['data']['F2S8', 'corrected_touch/slip1']}
    output = []
    for s in pool:
        e = s['event']; key = s['event_key']; meta = dataset['meta'][key]
        row = evaluations[key]; ev = routing.EV(row)
        code = meta['contract']; sign = 1 if e['direction'] == 'up' else -1
        assert abs(e['risk'] - row['planned_risk']) < 1e-8
        groups, quality = forward(lookup, dates, segments, code, e['warning_ts'], 32)
        for n in HORIZONS:
            q = dict(period=period, year=e['warning_ts'][:4], month=e['warning_ts'][:7],
                     quarter=e['warning_ts'][:4]+'Q'+str((int(e['warning_ts'][5:7])-1)//3+1),
                     key=key, cluster=meta['cluster'], direction=e['direction'], contract=code,
                     warning_ts=e['warning_ts'], horizon=n, economic_pass=row['economic_pass'],
                     native_status=ev['status'], native_exit=ev['exit_reason'],
                     native_entry_ts=ev['entry_ts'], native_exit_ts=ev['exit_ts'],
                     status='ok' if len(groups) >= n else quality, available_bars=min(n,len(groups)))
            if q['status'] == 'ok':
                raw = [v for group in groups[:n] for v in group]
                q['signal'] = measures(raw, e['entry'], e['risk'], sign)
                if ev['entry_ts'] and ev['fill'] is not None:
                    executable = [v for v in raw if v['datetime'] >= ev['entry_ts']]
                    if executable:
                        q['execution'] = measures(executable, ev['fill'], e['risk'], sign)
                        q['execution']['planned_target_race'] = paths.barrier(executable, ev['fill'], e['risk'], sign,
                            upper=(e['target']-ev['fill'])*sign/e['risk'], stop=e['stop'])[0]
                        # Descriptive fixed-window mark; ignores strategy stops and occupancy.
                        final = executable[-1]['close'] - sign*e['tick_size']
                        fee = (ev['fill']+final)*row['multiplier']*.00015
                        q['execution']['fixed_window_net'] = (final-ev['fill'])*sign*row['multiplier']-fee
                        q['execution']['fixed_window_net_r'] = q['execution']['fixed_window_net']/(e['risk']*row['multiplier'])
                        if ev['exit_ts']:
                            after = [v for v in executable if v['datetime'] > ev['exit_ts']]
                            q['after_exit'] = dict(minutes=len(after), target_hit=any((v['high' if sign>0 else 'low']-e['target'])*sign>=0 for v in after),
                                target_stop_race=paths.barrier(after, ev['fill'], e['risk'], sign,
                                    upper=(e['target']-ev['fill'])*sign/e['risk'], stop=e['stop'])[0] if after else None)
            output.append(q)
    write(period+'-paths.json', output)
    print(period, 'decisions',len(pool),'complete windows',sum(q['status']=='ok' for q in output),flush=True)
    return output


def run():
    RUN.mkdir(exist_ok=False)
    protected = R(routing.x.RUN/'protocol.json')['production_before']
    before = {p:r.sha(r.b.ROOT/p) for p in protected}
    write('protocol.json', dict(created_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
        script_sha256=r.sha(__file__), horizons=HORIZONS, universe='ALL frozen S0_RECLAIM_R2 decisions in main 2026 and Sep-Dec2025, including economic rejection and occupied signals; no July add-on.',
        signal='From decision CLOSE, next actual minute onward, planned risk denominator; exit-independent.',
        execution='Native F2S8 primary fill onward, same horizon endpoints and planned risk denominator; no pre-entry extrema. Individual hypothetical execution for blocked signals, not accepted portfolio.',
        censor='Every expected positive-volume minute in 15m trading slots; stop at missing, zero-volume, roll or coverage end. Incomplete windows excluded explicitly, not zero. Exchange session breaks are not bars.',
        order='Minute OPEN resolves gaps; simultaneous high/low crossing is same_minute_unknown, preserving both possible OHLC orders.',
        net='Descriptive horizon close minus1tick exit; native slipped entry and frozen JD .00015 notional fee per leg. Ignores stops, holding cap, portfolio and holidays: NOT a tradable strategy PnL.',
        design='Sequential descriptive diagnosis on already-viewed history, no optimization or new OOS claim. No production changes.',
        production_before=before, qualified_for_trading=False))
    ctl, datasets, qa = routing.load()
    write('baseline-load-checks.json',qa)
    allrows=[]; sources={}
    for per in ['early','recent']:
        p, recognition, _, dates = r.f.t.period_paths(per)
        poolfile=r.c.RUN/per/'S0_RECLAIM_R2'/(per+'-pool.json')
        allrows += audit(per,R(p['contracts']),dates,R(p['segments'])['JD0'],R(poolfile),datasets['Y2026'])
        for f in [p['contracts'],p['segments'],poolfile]:sources[str(f)]=r.sha(f)
    # Reuse authorized ordinary read-only SDK collector; writes redirected to NEW audit directory.
    v=routing.v
    v.write=lambda n,z:write('2025-'+n,z)
    contracts=v.collect()
    coverage=R(RUN/'2025-actual-contract-coverage.json')
    previous=R(v.RUN/'actual-contract-coverage.json')['contracts']
    assert coverage['contracts']==previous, '2025 raw data differs from frozen transfer run'
    poolfile=v.RUN/'reclaim/validation2025-pool.json'
    allrows += audit('validation2025',contracts,R(v.RUN/'trading-dates.json'),R(v.RUN/'segments.json')['JD0'],R(poolfile),datasets['Y2025'])
    for f in [poolfile,v.RUN/'metadata.json',v.RUN/'segments.json',v.RUN/'trading-dates.json']:sources[str(f)]=r.sha(f)
    write('sources.json',sources)
    grouped={}
    for field in ['year','month','quarter','direction','cluster']:
        for value in sorted({q[field] for q in allrows}):
            for n in HORIZONS:
                for subset in ['all','economic_pass']:
                    rows=[q for q in allrows if q[field]==value and q['horizon']==n and (subset=='all' or q['economic_pass'])]
                    grouped['/'.join([field,value,str(n),subset])]=summarize(rows)
    write('summary.json',grouped)
    assert before=={p:r.sha(r.b.ROOT/p) for p in before}
    write('completion.json',dict(decisions=len(allrows)//4,windows=len(allrows),production_unchanged=True,
        all_keys_unique_per_horizon=len({(q['key'],q['horizon']) for q in allrows})==len(allrows),
        raw_2025_hash_exact=True,qualified_for_trading=False))


class Tests(unittest.TestCase):
    def test_barrier_ambiguity_and_open(self):
        q=dict(datetime='2026-01-01 09:16:00',open=100,high=120,low=80,close=100)
        self.assertEqual(paths.barrier([q],100,10,1)[0],'same_minute_unknown')
        q['open']=115
        self.assertEqual(paths.barrier([q],100,10,1)[0],'upper_first')
    def test_missing_and_roll(self):
        ts='2026-01-01 09:15:00'; end='2026-01-01 09:30:00'
        rows={}
        for i in range(16,31):
            t=f'2026-01-01 09:{i}:00';rows[t]=dict(datetime=t,volume=1)
        segments=[dict(underlying='JD',first=ts,last=end)]
        self.assertEqual(len(forward({'JD':rows},['2026-01-01'],segments,'JD',ts,1)[0]),1)
        del rows['2026-01-01 09:20:00']
        self.assertEqual(forward({'JD':rows},['2026-01-01'],segments,'JD',ts,1)[1],'missing_minute_or_archive_end')
        segments[0]['underlying']='OTHER'
        self.assertEqual(forward({'JD':rows},['2026-01-01'],segments,'JD',ts,1)[1],'roll_or_continuous_coverage_end')


if __name__=='__main__':
    if '--test' in sys.argv:unittest.main(argv=[__file__])
    else:run()
