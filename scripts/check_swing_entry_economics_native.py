"""Verify native gated replay against prior full-row veto+competition evaluation."""
import collections,json
import verify_swing_entry_economics_native as n
import research_swing_entry_combo as combo
import research_swing_abstop as s
from report_swing_c_structure import distribution,equal,SUFFIXES
w=n.w;RUN=n.RUN

def run():
    p=w.read(RUN/'protocol.json');masks=w.read(n.r.RUN/'selection-masks.json')['unmerged'];cmasks=w.read(combo.RUN/'selection-masks.json')['unmerged'];fees=w.read(p['periods']['early']['fees']);events={};ends={};cases=collections.defaultdict(list);qa=collections.Counter();direct={}
    for q in ['early','recent']:
        pool=w.read(RUN/(q+'-pool.json'));events.update({z['event_key']:z['event'] for z in pool});source=n.r.FAMILIES['unmerged'];raw={}
        for line in (source/q/'evaluations.jsonl').open(encoding='utf-8'):
            z=json.loads(line);raw[(z['event_key'],z['scenario'].split('/',1)[1])]=z
        for code,end in w.read(RUN/q/'manifest.json')['contract_ends'].items():ends[code]=max(end,ends.get(code,''))
        cal=s.TradingDates(w.read(p['periods'][q]['input'])['symbols'])
        for line in (RUN/q/'evaluations.jsonl').open(encoding='utf-8'):
            z=json.loads(line);profile,suffix=z['scenario'].removeprefix('P2_economics_').split('/',1);key=z['event_key'];expected=(cmasks if profile=='budget_body' else masks)[key][profile]
            assert (z['economic_pass'] and z['body_net_pass'])==expected;src=raw[(key,suffix)];assert all(z[f]==src[f] for f in ['entry','stop','target','new_friction','planned_risk','local_level_time']);qa['native_vs_causal_mask']+=1
            if expected:assert z['result']==src['result'];qa['passing_full_individual_result_exact']+=1
            else:assert not z['result']['fills'] and not s.ev(z)['entry_ts'] and not s.ev(z).get('trigger_ts') and s.ev(z)['status'] not in s.flat.s.UNKNOWN;qa['veto_before_entry_or_reservation']+=1
            z=s.reprice(z,fees,cal);z['period']=q
            if s.ev(z)['status']=='closed':z['exit_trading_day']=cal.day(s.ev(z)['exit_ts'])
            cases[profile+'/'+suffix].append(z)
    original=w.read(n.r.RUN/'results.json');interaction=w.read(combo.RUN/'results.json');checks=[]
    for case,rows in cases.items():
        profile,suffix=case.split('/',1);expected=interaction if profile=='budget_body' else original
        for pol in ['original','two_paths_flat']:
            aa,_=s.portfolio(rows,events,ends) if pol=='original' else s.flat.exposure_portfolio(rows,ends);direct[(pol,case)]=aa
            for q in ['early','recent','recent_aug_oct','combined']:
                keep=lambda z:True if q=='combined' else z['period']=='recent' and z['warning_ts']>='2026-08-01 00:00:00' if q=='recent_aug_oct' else z['period']==q
                m=distribution([z for z in aa if keep(z)],events);b=expected[pol][q]['unmerged/'+case];bad={k:[m[k],b[k]] for k in m if k in b and not equal(m[k],b[k])};assert not bad,(pol,q,case,bad);checks.append(dict(policy=pol,period=q,case=case,exact=True))
    assert all(w.sha(RUN/'fork'/k)==v for k,v in w.read(RUN/'implementation-manifest.json').items())
    w.write(RUN/'verification.json',dict(qa=dict(qa),metric_replays=checks,all_exact=True,source_best_a_unchanged=True,production_changed=False,qualified_for_trading=False));print('Native gate replay verified:',dict(qa),'metrics',len(checks),flush=True)

if __name__=='__main__':run()
