"""Equal-signal paired estimates and seeded host assignments; no fitted gates."""
import sys
sys.dont_write_bytecode=True
import collections
import json
import random
import statistics as st
import research_swing_jd_reclaim_matched_execution as m


def mean(xs):return st.mean(xs) if xs else None
def cash(row):
    e=m.a.routing.EV(row)
    if e['status']=='closed':
        assert e['net_amount'] is not None
        return e['net_amount']/(row['planned_risk']*row['multiplier'])
    if e['status'].startswith('filtered_') or e['status'] in ['unfilled','invalidated']:
        assert not row['result']['fills'] and e['entry_ts'] is None
        return 0.
    return None


def main():
    protocol=m.R(m.RUN/'protocol.json')
    assert protocol['script_sha256']==m.r.sha(m.__file__)
    matches=[];paths={};executions={};checks=[]
    for per in ['early','recent','validation2025']:
        matches+=m.R(m.RUN/per/'matches.json');paths.update(m.R(m.RUN/per/'paths.json'))
        checks.append(m.R(m.RUN/per/'verification.json'))
        for z in m.R(m.RUN/per/'fee-priced-decisions.json'):
            row=z['row'];key=(z['model']+'/'+z['suffix'],row['event_key'])
            assert key not in executions;executions[key]=row
    assert sum(z['exact_original_native_rows'] for z in checks)==672
    cases=sorted({k[0] for k in executions});matched=[q for q in matches if q['status']=='matched']
    pairs=[]
    for q in matched:
        for case in cases:
            realrow=executions[case,q['key']];real=cash(realrow)
            hosts=[executions[case,h['key']] for h in q['hosts']]
            values=[cash(z) for z in hosts];known=[z for z in values if z is not None]
            delta=real-mean(known) if real is not None and known else None
            pairs.append(dict(case=case,key=q['key'],cluster=q['cluster'],month=q['month'],year=q['year'],direction=q['direction'],
                real=real,control=mean(known),difference=delta,controls=len(hosts),known_controls=len(known),
                real_status=m.a.routing.EV(realrow)['status'],control_statuses=dict(collections.Counter(m.a.routing.EV(z)['status'] for z in hosts))))
    summary={}
    for field in ['year','month','direction','cluster']:
        for value in sorted({q[field] for q in matches}):
            cohort=[q for q in matches if q[field]==value]
            for case in cases:
                ps=[q for q in pairs if q[field]==value and q['case']==case];known=[q for q in ps if q['difference'] is not None]
                deltas=[q['difference'] for q in known]
                summary[field+'/'+value+'/'+case]=dict(all_signals=len(cohort),matched=len(ps),known_pairs=len(known),
                    mean_real=mean([q['real'] for q in known]),mean_control=mean([q['control'] for q in known]),
                    mean_difference=mean(deltas),positive_pairs=sum(z>0 for z in deltas),
                    mean_difference_without_best=mean(sorted(deltas)[:-1]),
                    unknown_real=sum(q['real'] is None for q in ps),unknown_hosts=sum(q['controls']-q['known_controls'] for q in ps))
    # Outcome-independent, unique-host assignments. Failed assignments are disclosed.
    rng=random.Random(protocol['seed']);assignments=[];failures=0
    for _ in range(m.N):
        used=set();chosen={}
        for q in sorted(matched,key=lambda z:(len(z['hosts']),z['ts'],z['key'])):
            available=[h for h in q['hosts'] if h['host_id'] not in used]
            if not available:chosen={};failures+=1;break
            h=rng.choice(available);used.add(h['host_id']);chosen[q['key']]=h['key']
        if chosen:assignments.append(chosen)
    replications={}
    for year in ['2025','2026','ALL']:
        selected=[q for q in matched if year=='ALL' or q['year']==year]
        for case in cases:
            zs=[]
            for assignment in assignments:
                ds=[];real=[];control=[]
                for q in selected:
                    av=cash(executions[case,q['key']]);bv=cash(executions[case,assignment[q['key']]])
                    if av is not None and bv is not None:ds.append(av-bv);real.append(av);control.append(bv)
                zs.append(dict(known_pairs=len(ds),unknown_pairs=len(selected)-len(ds),difference=mean(ds),real=mean(real),control=mean(control)))
            replications[year+'/'+case]=zs
    path_pairs=[]
    for q in matched:
        for n in m.a.HORIZONS:
            real=paths[q['key']][str(n)]
            controls=[paths[h['key']][str(n)] for h in q['hosts']]
            complete=[h for h in controls if h['status']=='ok']
            record=dict(key=q['key'],year=q['year'],month=q['month'],horizon=n,actual_status=real['status'],
                        controls=len(controls),complete_controls=len(complete))
            if real['status']=='ok' and complete:
                record.update(actual=real['end_r'],control=mean([h['end_r'] for h in complete]),
                    difference=real['end_r']-mean([h['end_r'] for h in complete]))
            path_pairs.append(record)
    # Explicit host-prefix checks, independent of outcome values.
    for q in matched:
        rp=q['prefix']
        assert len(q['hosts'])>=3
        for h in q['hosts']:
            hp=h['prefix'];assert hp['code']==rp['code'] and hp['ts'][:7]==rp['ts'][:7] and hp['ts'][11:]==rp['ts'][11:]
            assert hp['momentum_sign']==rp['momentum_sign'] and .5<=hp['atr']/rp['atr']<=2
            assert all(abs(hp['index']-z['prefix']['index'])>32 for z in matches if z['period']==q['period'] and 'prefix' in z)
    m.write('paired-results.json',pairs);m.write('summary.json',summary)
    m.write('path-pairs.json',path_pairs);m.write('assignments.json',assignments);m.write('replications.json',replications)
    m.write('analysis-verification.json',dict(all_signals=len(matches),matched=len(matched),original_exact_native_rows=672,
        native_rows=len(executions),replications=len(assignments),assignment_failures=failures,
        matching_prefix_checks=True,script_sha256=m.r.sha(__file__),qualified_for_trading=False))
    print(json.dumps(dict(coverage={y:dict(collections.Counter(q['status'] for q in matches if q['year']==y)) for y in ['2025','2026']},
        primary={y:summary['year/'+y+'/BASE/corrected_touch/slip1'] for y in ['2025','2026']},f2s8={y:summary['year/'+y+'/F2S8/corrected_touch/slip1'] for y in ['2025','2026']},assignments=len(assignments),failures=failures),ensure_ascii=False),flush=True)


if __name__=='__main__':main()
