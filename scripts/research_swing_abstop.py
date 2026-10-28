"""AB recognition, entry location, structural-stop and post-stop path audit.
Separate fixed-entry mechanisms from full causal portfolio changes.
"""
import argparse,bisect,collections,copy,datetime as dt,json,math,pathlib,shutil,statistics,subprocess
from research_swing_consistency import REPO,read,write,sha,replace_once,portfolio,ev,metrics
from research_swing_combination import describe
from reprice_signal_costs import TradingDates,reprice
import audit_swing_flat_ambiguity as flat

ROOT=REPO/'research-runs/swing-selective-20261025-v1'
SOURCE=REPO/'research-runs/swing-cycle-20261025-v1'
RUN=REPO/'research-runs/swing-structure-mechanisms-20261025-v1'

def aggregate15(raw):
    bins=collections.defaultdict(list)
    for b in raw:
        t=dt.datetime.fromisoformat(b['datetime']);end=t+dt.timedelta(minutes=(-t.minute)%15);bins[end.isoformat(sep=' ')].append(b)
    out=[]
    for t,bs in sorted(bins.items()):
        if t>raw[-1]['datetime']:continue
        out.append(dict(datetime=t,open=bs[0]['open'],high=max(x['high'] for x in bs),low=min(x['low'] for x in bs),close=bs[-1]['close'],volume=sum(x['volume'] for x in bs),components=len(bs)))
    return out

def features(snap,bars,index):
    e=snap['event'];sgn=1 if e['direction']=='up' else -1;tick=e['tick_size']
    ids=[index.get(e[k]) for k in ('s0_ts','s1_ts','warning_ts')]
    if any(x is None for x in ids):return dict(anchors_available=False,true_b_stop=e['stop'])
    i,j,w=ids;a=bars[i:j+1];b=bars[j+1:w+1];warning=bars[w];assert e['s2_ts']==e['warning_ts']
    def body_eff(xs):
        d=sum(abs(x['close']-x['open']) for x in xs)
        return sgn*sum(x['close']-x['open'] for x in xs)/d if d else None
    b_extreme=min(x['low'] for x in b) if sgn>0 else max(x['high'] for x in b)
    buffer=abs(e['s2_price']-e['stop']);raw=b_extreme-sgn*buffer
    stop=(math.floor(raw/tick+1e-9) if sgn>0 else math.ceil(raw/tick-1e-9))*tick
    stop=min(stop,e['stop']) if sgn>0 else max(stop,e['stop'])
    opposite=[max(0.,-sgn*(x['close']-x['open'])) for x in b[:-1]];opposite=[x for x in opposite if x>0]
    ratio=None
    if len(opposite)>=2:
        m=len(opposite)//2;ratio=statistics.mean(opposite[m:])/statistics.mean(opposite[:m])
    gaps=[(r['open']-l['close'])*sgn for l,r in zip(a,a[1:]) if (dt.datetime.fromisoformat(r['datetime'])-dt.datetime.fromisoformat(l['datetime'])).total_seconds()>900]
    return dict(anchors_available=True,anchor_counts_match=len(a)==e['a_bars'] and len(b)==e['b_bars'],
        a_bars=len(a),b_bars=len(b),a_body_eff=body_eff(a),a_body_eff_ex_s0=body_eff(a[1:]),
        a_gap_share=sum(gaps)/e['a_move'],a_largest_body_share=max(abs(x['close']-x['open']) for x in a)/e['a_move'],
        b_speed_to_a=(e['b_move']/max(1,e['b_bars']))/(e['a_move']/max(1,e['a_bars'])),b_weakening_ex_warning=ratio,
        b_extreme=b_extreme,true_b_stop=stop,stop_inside_b_range=(e['stop']-b_extreme)*sgn>1e-9,
        prior_b_trough_offset_r=(e['s2_price']-b_extreme)*sgn/e['risk'],true_b_risk_multiple=abs(e['entry']-stop)/e['risk'],
        warning_range_atr=(warning['high']-warning['low'])/snap['atr14'],warning_range_to_b=(warning['high']-warning['low'])/e['b_move'],
        warning_recovered_b=(warning['close']-e['s2_price'])*sgn/e['b_move'],risk_atr=e['risk']/snap['atr14'],
        partial_bars_in_ab=sum(x['components']!=3 for x in a+b),s2_is_warning=True)

def prepare():
    RUN.mkdir(exist_ok=False);fork=RUN/'fork';shutil.copytree(ROOT/'session-supplement/fork',fork)
    runner=(fork/'crates/n-core/src/bin/swing_session_runner.rs').read_text(encoding='utf-8')
    runner=replace_once(runner,'            let wide = template["wide"].as_bool().unwrap();','''            if template["b_extreme"]==true {
                ev.stop=snap["mechanism"]["true_b_stop"].as_f64().unwrap();geom::normalize(&mut ev);
            }
            let wide = template["wide"].as_bool().unwrap();''')
    runner=replace_once(runner,'                rules.session_failure_exit=template["session_loss"]==true;','''                rules.session_failure_exit=template["session_loss"]==true;
                if template["adaptive"]==true {
                    if original.market_context.as_ref().and_then(|m|m.trend_alignment_60).is_some_and(|h|h<=0.) {
                        rules.entry_mode=if original.warning_kind=="cumulative" {ablation::EntryMode::Touch}else{ablation::EntryMode::CloseConfirmNextOpen};
                        if original.market_context.as_ref().and_then(|m|m.trend_alignment_60).is_some_and(|h|h<=-1.) {
                            rules.skip_prebreak_entries=true;rules.session_failure_exit=true;
                        }
                    }
                }''')
    (fork/'crates/n-core/src/bin/swing_bextreme_runner.rs').write_text(runner,encoding='utf-8')
    shutil.copy2(REPO/'scripts/swing_structure_diagnostic.rs',fork/'crates/n-core/src/bin/swing_structure_diagnostic.rs')
    p=read(SOURCE/'protocol.json');base=p['templates'][0];templates=[]
    for name,extreme,adaptive in [('base',False,False),('b_extreme',True,False),('adaptive',False,True),('adaptive_b_extreme',True,True)]:
        t=copy.deepcopy(base);t.update(name='P2_ab_'+name,variant=name,b_extreme=extreme,adaptive=adaptive,session_loss=False,half_body=False,close_body_count=None);templates.append(t)
    cohorts=collections.defaultdict(list)
    for root,case,label,policy in [(SOURCE,'P2_cycle_base/corrected_touch/slip1','reference',None),
        (ROOT/'execution-interaction','strong_kind_session/corrected_touch/slip1','observation','original')]:
        for line in (root/'accepted.jsonl').open(encoding='utf-8'):
            z=json.loads(line)
            if z['case']!=case or policy and z.get('policy')!=policy or ev(z['row'])['status']!='closed':continue
            cohorts[z['period']].append(dict(label=label,event_key=z['row']['event_key'],baseline=z['row']))
    anchor_audit={}
    for period in ('early','recent'):
        inp=read(p['periods'][period]['input']);bars={sym:aggregate15(v['bars']) for sym,v in inp['symbols'].items()};indices={sym:{b['datetime']:i for i,b in enumerate(v)} for sym,v in bars.items()}
        pool=read(SOURCE/(period+'-pool.json'));bykey={}
        for snap in pool:
            e=snap['event'];snap['mechanism']=features(snap,bars[e['symbol']],indices[e['symbol']]);bykey[snap['event_key']]=snap
        for c in cohorts[period]:
            c['event']=bykey[c['event_key']]['event'];c['mechanism']=bykey[c['event_key']]['mechanism']
        write(RUN/(period+'-pool.json'),pool);write(RUN/(period+'-cohorts.json'),cohorts[period])
        anchor_audit[period]=dict(pool=len(pool),cohorts=len(cohorts[period]),available=sum(x['mechanism']['anchors_available'] for x in pool),counts_match=sum(x['mechanism'].get('anchor_counts_match',False) for x in pool))
    p.update(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),templates=templates,online_windows=[],
        historical='Mechanism audit plus one structural-stop hypothesis, all periods researched development data. No new holdout claim.',
        diagnosis='Fixed accepted CLOSED entries of reference and observation, primary1tick. Full control rerun must match entry/fills and repriced cash. Force same observed entry for target barriers .5/1/2 originalR at8/16/60 holding buckets; original stop. Stop1/1.5/2 originalR or true prior-B extreme, fixed absolute target at16/60 buckets. Disable no-follow/session profit rule in DIAGNOSTIC only, retain holidays/weekends/data guards. No guard/reselection, explicitly NOT deployable portfolio returns.',
        recovery='After actual stop minute ONLY, scan4/8/16 observed15m buckets, keep missing/zero-volume/source-end censored, entry and1R recovery vs reaching2R adverse first with same-minute ambiguity separated. Report cross-session recovery separately. No inference that wider stop could keep every recovering loser.',
        structural='S2 is current warning bar extreme, not necessarily B-segment extreme. New stop=adverse edge of ALL CLOSED B bars from afterS1 through warning, plus original warning stop buffer; only widen, adverse tick rounding. Recompute costs/minrisk/netRR and capacity across full warning pool, identical filters otherwise.',
        recent3m='Also report warning timestamps2026-08-01 through2026-10-23; do not relabel incomplete symbol coverage as full three calendar months.',
        anchor_audit=anchor_audit,production_changed=False,qualified_for_trading=False,
        source_code_sha256={str(REPO/x):sha(REPO/x) for x in ['crates/n-core/src/analyze/event.rs','crates/n-core/src/analyze/pattern.rs','crates/n-core/src/analyze/scoring.rs']})
    write(RUN/'protocol.json',p);write(RUN/'implementation-manifest.json',{str(q.relative_to(fork)):sha(q) for q in fork.rglob('*') if q.is_file()})
    print('Frozen AB/stop audit',anchor_audit,flush=True)

def run():
    p=read(RUN/'protocol.json');args=['--offline','--manifest-path',str(RUN/'fork/Cargo.toml'),'--target-dir',str(REPO/'target'),'-p','n-core']
    subprocess.run(['cargo','build',*args,'--bin','swing_bextreme_runner','--bin','swing_structure_diagnostic'],check=True)
    for period in ('early','recent'):
        assert all(sha(v)==p['hashes'][period][k] for k,v in p['periods'][period].items())
        subprocess.run([str(REPO/'target/debug/swing_bextreme_runner.exe'),str(RUN/'protocol.json'),period,'replay',str(RUN/period)],check=True)
        subprocess.run([str(REPO/'target/debug/swing_structure_diagnostic.exe'),str(RUN/'protocol.json'),period,str(RUN/(period+'-cohorts.json')),str(RUN/(period+'-diagnostics.jsonl'))],check=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('stage',choices=['prepare','run']);globals()[p.parse_args().stage]()
