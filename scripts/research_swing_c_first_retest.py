"""Before inspecting C-results, add first-retest semantics for small swings."""
import argparse,copy,datetime as dt,shutil,subprocess
import research_swing_c_structure as base
w=base.w;main=base.main;RUN=base.RUN/'first-retest';PROFILES=['base','first_close','first_wick']

def prepare():
    RUN.mkdir(exist_ok=False);fork=RUN/'fork';shutil.copytree(base.RUN/'fork',fork)
    f=fork/'crates/n-core/src/v3/ablation/c_structure.rs';x=f.read_text(encoding='utf-8');x=w.replace_once(x,'impl Structure{','''impl Structure{
    pub fn seed_origin(&mut self,entry:&str,price:f64){let pivot=Pivot{ts:entry.into(),price};record("origin",entry,Some(&pivot),None,None,None,None);self.adverse=Some(pivot);}
''');f.write_text(x,encoding='utf-8')
    f=fork/'crates/n-core/src/v3/ablation.rs';x=f.read_text(encoding='utf-8');x=w.replace_once(x,'    pub c_structure: bool,','    pub c_structure: bool,\n    pub c_initial_origin: Option<f64>,');x=w.replace_once(x,'    pending_c_exit: bool,','    pending_c_exit: bool,\n    c_origin_set: bool,');x=w.replace_once(x,'        pending_c_exit: false,','        pending_c_exit: false,\n        c_origin_set: false,');x=w.replace_once(x,'                if state.c_state.observe(bar,entry,sign,ev.tick_size,rules.c_require_push,rules.c_wick_failure){','                if !state.c_origin_set{if let Some(price)=rules.c_initial_origin{state.c_state.seed_origin(entry,price);}state.c_origin_set=true;}\n                if state.c_state.observe(bar,entry,sign,ev.tick_size,rules.c_require_push,rules.c_wick_failure){');f.write_text(x,encoding='utf-8')
    f=fork/'crates/n-core/src/bin/swing_c_structure_execution.rs';x=f.read_text(encoding='utf-8');x=w.replace_once(x,'                for slip in [1., 2.] {','''                let c_origin=if template["c_first"]==true{
                    let prior=f["prior_b_extreme"].as_f64().unwrap();Some(if original.direction=="up"{prior.min(original.s2_price)}else{prior.max(original.s2_price)})
                }else{None};rules.c_initial_origin=c_origin;
                for slip in [1., 2.] {''');x=w.replace_once(x,'"c_structure_mode":template["c_mode"],','"c_initial_origin":c_origin,"c_structure_mode":template["c_mode"],');(fork/'crates/n-core/src/bin/swing_c_first_execution.rs').write_text(x,encoding='utf-8')
    f=fork/'crates/n-core/tests/swing_c_structure.rs';x=f.read_text(encoding='utf-8');x+='''
#[test]
fn first_retest_needs_a_new_confirmed_push_and_low_above_known_origin(){clear_trace();let mut s=Structure::default();s.seed_origin("2026-01-05 09:00:00",96.);let spec=[(110.,100.),(116.,105.),(113.,103.),(112.,101.),(114.,105.)];for i in 1..=25{let(h,l)=spec[(i-1)/5];assert!(!s.observe(&bar(i,h,l,(h+l)/2.,false),"2026-01-05 09:00:00",1.,1.,false,false));}let tr=take_trace();let c=tr.iter().find(|t|t.kind=="candidate").unwrap();assert_eq!(c.previous_pivot_ts.as_deref(),Some("2026-01-05 09:00:00"));assert_eq!(c.opposite_ts.as_deref(),Some("2026-01-05 09:10:00"));assert_eq!(c.pivot_ts.as_deref(),Some("2026-01-05 09:20:00"));assert!(tr.iter().any(|t|t.kind=="activated"&&t.ts=="2026-01-05 09:25:00"));assert!(s.observe(&bar(26,102.,99.,99.,false),"2026-01-05 09:00:00",1.,1.,false,false));}
''';f.write_text(x,encoding='utf-8')
    p=w.read(base.RUN/'protocol.json');b=p['templates'][0];templates=[]
    for name in PROFILES:
        t=copy.deepcopy(b);t.update(name='P2_transition_'+name,variant=name,c_mode=name,c_first=name!='base',c_require_push=False,c_wick=name.endswith('wick'));templates.append(t)
    p.update(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),templates=templates,script_sha256=w.sha(__file__),
        amendment='Before inspecting any own-C PnL or scorecard: two extra semantic controls, no numerical grid. Strict2 post-entry lows can wait for a third micro push; seed known warning-time wholeB adverse extreme to test FIRST newlyconfirmed C retest instead. Four original models preserved.',
        C_first='Known B extreme=min(prior closedB lows,warningS2) for long/max for short. Seed is a reference price labelled at actual entry, never a fabricated observed pivot. Need a NEW post-entry confirmed favorable5m turn then NEW confirmed adverse5m pivot above/below seed; support itself is post-entry, not oldB line. Subsequent close/wick break1tick schedules next1m exit.',
        user_objective='Same new combined-profit objective; no80% or separate-period positivity veto. Report all original and first-retest models together.')
    for q in ('early','recent'):shutil.copy2(base.RUN/(q+'-pool.json'),RUN/(q+'-pool.json'))
    w.write(RUN/'protocol.json',p);w.write(RUN/'implementation-manifest.json',{f.relative_to(fork).as_posix():w.sha(f) for f in fork.rglob('*') if f.is_file()});print('Frozen2 first-new-C-retest controls BEFORE own-C outcome inspection',flush=True)

def build():
    p=w.read(RUN/'protocol.json');assert w.sha(__file__)==p['script_sha256'];args=['--offline','--manifest-path',str(RUN/'fork/Cargo.toml'),'--target-dir',str(w.REPO/'target'),'-p','n-core'];subprocess.run(['cargo','test',*args,'--test','swing_c_structure','--','--test-threads=1'],check=True);subprocess.run(['cargo','build','--release',*args,'--bin','swing_c_first_execution'],check=True);w.write(RUN/'built-binaries.json',{'swing_c_first_execution.exe':w.sha(w.REPO/'target/release/swing_c_first_execution.exe')})

def period_run(q):
    main.verify_snapshot();p=w.read(RUN/'protocol.json');assert all(w.sha(path)==p['hashes'][q][k] for k,path in p['periods'][q].items());assert all(w.sha(RUN/'fork'/k)==v for k,v in w.read(RUN/'implementation-manifest.json').items());assert w.sha(RUN/(q+'-pool.json'))==w.sha(base.RUN/(q+'-pool.json'));assert all(w.sha(w.REPO/'target/release'/k)==v for k,v in w.read(RUN/'built-binaries.json').items());subprocess.run([str(w.REPO/'target/release/swing_c_first_execution.exe'),str(RUN/'protocol.json'),q,'replay',str(RUN/q)],check=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('stage',choices=['prepare','build','early','recent']);stage=p.parse_args().stage;(period_run(stage) if stage in ('early','recent') else globals()[stage]())
