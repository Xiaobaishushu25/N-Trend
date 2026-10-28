"""Causal B-to-C evidence with fixed selected parent leg, 最佳a策略 control."""
import argparse,collections,copy,datetime as dt,json,shutil,subprocess
import research_swing_wave_selection as w
from freeze_best_a_strategy import SNAP
from swing_b_identity import features
from report_swing_wave_selection import identification_bars,first_index
RUN=w.REPO/'research-runs/swing-b-transition-20261025-v1'
PROFILES=['base','all_touch','all_close','fresh_close','inside_close']

def verify_snapshot():
    assert all(w.sha(SNAP/k)==v for k,v in w.read(SNAP/'manifest.json').items())

def prepare():
    verify_snapshot();RUN.mkdir(exist_ok=False);fork=RUN/'fork';shutil.copytree(SNAP/'fork',fork)
    source=(fork/'crates/n-core/src/bin/swing_wave_execution.rs').read_text(encoding='utf-8')
    source=w.replace_once(source,'            let new_friction = friction(&ev, fee, mult);','''            let baseline_target=ev.target;
            let f=&snap["b_transition"];
            let apply_micro=f["required"]==true && match template["transition"].as_str().unwrap(){
                "all_touch"|"all_close"=>true,
                "fresh_close"=>f["fresh_b_extreme"]==true,
                "inside_close"=>f["fresh_b_extreme"]==false,
                _=>false,
            };
            if apply_micro{ev.entry=f["level"].as_f64().unwrap();geom::normalize(&mut ev);}
            let new_friction = friction(&ev, fee, mult);''')
    source=w.replace_once(source,'let geometry_ok = ev.risk / ev.tick_size + 1e-6 >= 10. && new_friction <= 0.2 + 1e-9;','let geometry_ok = ev.risk / ev.tick_size + 1e-6 >= 10. && new_friction <= 0.2 + 1e-9 && (!apply_micro || (ev.target-ev.entry)*(if ev.direction=="up"{1.}else{-1.})>0.);')
    source=w.replace_once(source,'                for slip in [1., 2.] {','''                if apply_micro && template["micro_confirm"]==true{rules.entry_mode=ablation::EntryMode::CloseConfirmNextOpen;}
                for slip in [1., 2.] {''')
    source=w.replace_once(source,'"local_level_time":local_source,','"local_level_time":local_source,"baseline_target":baseline_target,"micro_applied":apply_micro,"micro_confirm":apply_micro&&template["micro_confirm"]==true,"parent_leg_id":f["parent_leg_id"],"fresh_b_extreme":f["fresh_b_extreme"],')
    (fork/'crates/n-core/src/bin/swing_b_transition_execution.rs').write_text(source,encoding='utf-8')
    shutil.copy2(w.REPO/'tests/swing_b_identity.rs',fork/'crates/n-core/tests/swing_b_identity.rs')
    p=w.read(SNAP/'protocol.json');base=p['templates'][0];templates=[]
    for profile in PROFILES:
        t=copy.deepcopy(base);t.update(name='P2_transition_'+profile,variant=profile,transition=profile,micro_confirm=profile.endswith('close'));templates.append(t)
    p.update(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),templates=templates,script_sha256=w.sha(__file__),feature_source_sha256=w.sha(w.REPO/'scripts/swing_b_identity.py'),snapshot_manifest_sha256=w.sha(SNAP/'manifest.json'),
        experiment='Four pre-frozen internal-B structural entry hypotheses plus exact 最佳a策略 control. No warning or A/B reselection; frozen service-dedup pool unchanged. Full raw opportunity execution, downstream geometry/fee/netRR and trigger-time capacity recomputed, not a fixed losing-trade deletion.',
        parent_identity='symbol/direction/S0/S1 of frozen best-a candidate, never substitute a failed nested impulse for parentA. Internal evidence only from source5m AFTER parentS1 and before current warning15m components.',
        evidence='Latest strict3-candle5m favorable pivot (long high,short low), both adjacent intervals exactly5m, all three within B. Right confirmation<=warning-15min. Trigger one adverse-direction tick beyond pivot, grid rounded. Only raise entry when original warning has not cleared it; missing/alreadycleared retain best-a exactly.',
        profiles='all_touch uses raised micro level with existing adaptive mode; all_close adds completed1m close then next open; fresh_close only where warning touches/makes wholeB fresh extreme vs all priorB15m; inside_close complementary oldB-range category. Four finite rules, no parameter/product/month grid.',
        preserved='Compute original best-a local target FIRST, then raise entry; stop and ABSOLUTE target unchanged even if new entry lacks space. No fallback to farther target or another anchor. Recheck nominal friction/min10ticks AND actual projected netRR; pending12/no-follow5/hold60 and calendars unchanged. Higher entry-to-stop risk and compressed reward explicitly measured.',
        qualification='All histories repeatedly researched, no holdout or live promotion. Vs named best-a in both occupancy policies and all4 stresses: recentcash>=80%, early cash/meanR/DD noninferior, unknown nonincreasing and n>=max(35,.3ref). Strict additionally positive both cash/R everywhere, recent win>=ref, tail10 no worse. Report original-risk-denominator R too.',
        source_results_sha256=w.sha(SNAP/'results.json'),production_changed=False,qualified_for_trading=False)
    audit={}
    for q in ('early','recent'):
        assert all(w.sha(path)==p['hashes'][q][k] for k,path in p['periods'][q].items());inp=w.read(p['periods'][q]['input']);bars={sym:identification_bars(z['bars']) for sym,z in inp['symbols'].items()};indices={sym:first_index(arr) for sym,arr in bars.items()};times={sym:[b['datetime'] for b in z['bars']] for sym,z in inp['symbols'].items()};pool=w.read(SNAP/(q+'-pool.json'));counts=collections.Counter()
        for snap in pool:
            e=snap['event'];f=features(e,inp['symbols'][e['symbol']]['bars'],bars[e['symbol']],indices[e['symbol']],times[e['symbol']]);snap['b_transition']=f;counts[f['state']]+=1
            if f['required']:counts['required_fresh' if f['fresh_b_extreme'] else 'required_inside']+=1
        w.write(RUN/(q+'-pool.json'),pool);audit[q]=dict(counts)
    p['feature_counts_before_outcomes']=audit;w.write(RUN/'protocol.json',p);w.write(RUN/'implementation-manifest.json',{f.relative_to(fork).as_posix():w.sha(f) for f in fork.rglob('*') if f.is_file()});w.write(RUN/'feature-manifest.json',{q:w.sha(RUN/(q+'-pool.json')) for q in ('early','recent')});print('Frozen4 B-transition hypotheses + best-a control',audit,flush=True)

def build():
    p=w.read(RUN/'protocol.json');assert w.sha(__file__)==p['script_sha256'];assert w.sha(w.REPO/'scripts/swing_b_identity.py')==p['feature_source_sha256'];args=['--offline','--manifest-path',str(RUN/'fork/Cargo.toml'),'--target-dir',str(w.REPO/'target'),'-p','n-core']
    subprocess.run(['cargo','test',*args,'--test','swing_b_identity','--','--test-threads=1'],check=True)
    subprocess.run(['cargo','build','--release',*args,'--bin','swing_b_transition_execution'],check=True)
    w.write(RUN/'built-binaries.json',{'swing_b_transition_execution.exe':w.sha(w.REPO/'target/release/swing_b_transition_execution.exe')})

def period_run(q):
    verify_snapshot();p=w.read(RUN/'protocol.json');assert all(w.sha(path)==p['hashes'][q][k] for k,path in p['periods'][q].items());assert all(w.sha(RUN/'fork'/k)==h for k,h in w.read(RUN/'implementation-manifest.json').items());assert w.sha(RUN/(q+'-pool.json'))==w.read(RUN/'feature-manifest.json')[q];assert all(w.sha(w.REPO/'target/release'/k)==h for k,h in w.read(RUN/'built-binaries.json').items())
    subprocess.run([str(w.REPO/'target/release/swing_b_transition_execution.exe'),str(RUN/'protocol.json'),q,'replay',str(RUN/q)],check=True)

if __name__=='__main__':
    a=argparse.ArgumentParser(description=__doc__);a.add_argument('stage',choices=['prepare','build','early','recent']);stage=a.parse_args().stage;(period_run(stage) if stage in ('early','recent') else globals()[stage]())
