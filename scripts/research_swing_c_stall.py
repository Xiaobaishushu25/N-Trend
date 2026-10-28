"""One final hypothesis: failed-to-form C at existing no-follow checkpoint."""
import argparse,copy,datetime as dt,shutil,subprocess
import research_swing_c_first_retest as first
w=first.w;main=first.main;RUN=first.base.RUN/'not-formed';PROFILES=['base','stall5']

def prepare():
    RUN.mkdir(exist_ok=False);fork=RUN/'fork';shutil.copytree(first.RUN/'fork',fork)
    f=fork/'crates/n-core/src/v3/ablation/c_structure.rs';x=f.read_text(encoding='utf-8');x=w.replace_once(x,'impl Structure{','''impl Structure{
    pub fn has_formed(&self)->bool{self.active.is_some()}
    pub fn record_stall(ts:&str,close:f64,fill:f64){record("stall",ts,None,None,None,Some(fill),Some(close));}
''');f.write_text(x,encoding='utf-8')
    f=fork/'crates/n-core/src/v3/ablation.rs';x=f.read_text(encoding='utf-8');x=w.replace_once(x,'    pub c_structure: bool,','    pub c_structure: bool,\n    pub c_stall: bool,');x=w.replace_once(x,'else{"c_structure_failed"}','else if rules.c_stall{"c_not_formed"}else{"c_structure_failed"}')
    x=w.replace_once(x,'                if state.c_state.observe(bar,entry,sign,ev.tick_size,rules.c_require_push,rules.c_wick_failure){state.pending_c_exit=true;}','''                if !rules.c_stall || !state.c_state.has_formed(){
                    let failed=state.c_state.observe(bar,entry,sign,ev.tick_size,rules.c_require_push,rules.c_wick_failure);
                    if failed && !rules.c_stall{state.pending_c_exit=true;}
                }
                if rules.c_stall && bar.datetime==bucket && held==5 && !state.c_state.has_formed()
                    && (bar.close-state.e.fill.unwrap())*sign<0.{state.pending_c_exit=true;c_structure::Structure::record_stall(&bar.datetime,bar.close,state.e.fill.unwrap());}''');f.write_text(x,encoding='utf-8')
    f=fork/'crates/n-core/src/bin/swing_c_first_execution.rs';x=f.read_text(encoding='utf-8');x=w.replace_once(x,'                rules.c_structure=template["c_mode"]!="base";','                rules.c_structure=template["c_mode"]!="base";\n                rules.c_stall=template["c_stall"]==true;');(fork/'crates/n-core/src/bin/swing_c_stall_execution.rs').write_text(x,encoding='utf-8')
    shutil.copy2(w.REPO/'tests/swing_c_stall.rs',fork/'crates/n-core/tests/swing_c_stall.rs')
    p=w.read(first.RUN/'protocol.json');b=p['templates'][0];ts=[]
    for name in PROFILES:
        t=copy.deepcopy(b);t.update(name='P2_transition_'+name,variant=name,c_mode=name,c_first=name!='base',c_stall=name!='base',c_require_push=False,c_wick=False);ts.append(t)
    p.update(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),templates=ts,script_sha256=w.sha(__file__),
        chronology='AFTER6 C-stop models and3 payoff/time models failed. One focused stage-specific hypothesis; no new threshold or parameter search. C-formed original cohort was profitable in BOTH periods, so protect failure-to-form, not normal C retrace.',
        rule='Original best-a entries/stops/targets and exits unchanged. At existing held==5 completed15m bucket checkpoint, if NO confirmed first C higher-low/lower-high (seed known B extreme; intervening new post-entry favorable pivot required) AND current close is gross losing, schedule next1m open exit. Original no-follow exits already occur first for MFE<.5R, so extra rule affects only stalled positions with larger earlier excursion. Once C formed, ignore C breaks and stop observing extra state; no profit clipping by new C guard.',
        objective='Same combined net primary; no80 constraint. Final singleton hypothesis of this round, all10 new models retained.')
    for q in ('early','recent'):shutil.copy2(first.RUN/(q+'-pool.json'),RUN/(q+'-pool.json'))
    w.write(RUN/'protocol.json',p);w.write(RUN/'implementation-manifest.json',{f.relative_to(fork).as_posix():w.sha(f) for f in fork.rglob('*') if f.is_file()});print('Frozen singleton no-C AND losing at original fifth-bucket checkpoint',flush=True)

def build():
    p=w.read(RUN/'protocol.json');assert w.sha(__file__)==p['script_sha256'];args=['--offline','--manifest-path',str(RUN/'fork/Cargo.toml'),'--target-dir',str(w.REPO/'target'),'-p','n-core'];subprocess.run(['cargo','test',*args,'--test','swing_c_stall','--','--test-threads=1'],check=True);subprocess.run(['cargo','build','--release',*args,'--bin','swing_c_stall_execution'],check=True);w.write(RUN/'built-binaries.json',{'swing_c_stall_execution.exe':w.sha(w.REPO/'target/release/swing_c_stall_execution.exe')})

def period_run(q):
    main.verify_snapshot();p=w.read(RUN/'protocol.json');assert all(w.sha(path)==p['hashes'][q][k] for k,path in p['periods'][q].items());assert all(w.sha(RUN/'fork'/k)==v for k,v in w.read(RUN/'implementation-manifest.json').items());assert w.sha(RUN/(q+'-pool.json'))==w.sha(first.RUN/(q+'-pool.json'));assert all(w.sha(w.REPO/'target/release'/k)==v for k,v in w.read(RUN/'built-binaries.json').items());subprocess.run([str(w.REPO/'target/release/swing_c_stall_execution.exe'),str(RUN/'protocol.json'),q,'replay',str(RUN/q)],check=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('stage',choices=['prepare','build','early','recent']);stage=p.parse_args().stage;(period_run(stage) if stage in ('early','recent') else globals()[stage]())
