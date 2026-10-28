"""Exploratory AFTER entry-confirmation outcomes: use SAME B evidence for exits."""
import argparse,copy,datetime as dt,shutil,subprocess
import research_swing_b_transition as main
w=main.w;RUN=main.RUN/'progress-rejection';PROFILES=['base','rejection_any','rejection_profit']

def prepare():
    RUN.mkdir(exist_ok=False);fork=RUN/'fork';shutil.copytree(main.RUN/'fork',fork)
    f=fork/'crates/n-core/src/v3/ablation.rs';x=f.read_text(encoding='utf-8')
    x=w.replace_once(x,'    pub entry_mode: EntryMode,','    pub entry_mode: EntryMode,\n    pub micro_progress_level: Option<f64>,\n    pub micro_rejection_level: Option<f64>,\n    pub micro_profit_only: bool,')
    x=w.replace_once(x,'    pending_session_exit: bool,','    pending_session_exit: bool,\n    micro_cleared_at: Option<String>,\n    pending_micro_exit: bool,')
    x=w.replace_once(x,'        pending_session_exit: false,','        pending_session_exit: false,\n        micro_cleared_at: None,\n        pending_micro_exit: false,')
    x=w.replace_once(x,'if state.pending_body_exit || state.pending_session_exit {','if state.pending_body_exit || state.pending_session_exit || state.pending_micro_exit {')
    x=w.replace_once(x,'if state.pending_body_exit {"warning_body_failed_close"}else{"prebreak_failed_start"}','if state.pending_body_exit {"warning_body_failed_close"}else if state.pending_session_exit {"prebreak_failed_start"}else{"micro_progress_rejected"}')
    old='''        if rules.session_failure_exit && state.e.fill.is_some() && pre_long_break(&ev.symbol,&bar.datetime)'''
    new='''        if let (Some(level),Some(reject),Some(fill))=(rules.micro_progress_level,rules.micro_rejection_level,state.e.fill){
            if state.micro_cleared_at.is_none() && (bar.close-level)*sign>=0.{state.micro_cleared_at=Some(bar.datetime.clone());}
            else if state.micro_cleared_at.as_deref().is_some_and(|at|at<bar.datetime.as_str()) && (bar.close-reject)*sign<0.
                && (!rules.micro_profit_only || (bar.close-fill)*sign>0.){state.pending_micro_exit=true;}
        }
'''+old
    x=w.replace_once(x,old,new);f.write_text(x,encoding='utf-8')
    f=fork/'crates/n-core/src/bin/swing_b_transition_execution.rs';x=f.read_text(encoding='utf-8')
    x=w.replace_once(x,'                for slip in [1., 2.] {','''                let micro_exit=template["micro_exit"]==true && f["required"]==true;
                if micro_exit{rules.micro_progress_level=f["level"].as_f64();rules.micro_rejection_level=f["pivot_price"].as_f64();rules.micro_profit_only=template["micro_profit_only"]==true;}
                for slip in [1., 2.] {''')
    x=w.replace_once(x,'"micro_applied":apply_micro,','"micro_exit_enabled":micro_exit,"micro_exit_profit_only":template["micro_profit_only"]==true,"micro_applied":apply_micro,')
    (fork/'crates/n-core/src/bin/swing_b_rejection_execution.rs').write_text(x,encoding='utf-8');shutil.copy2(w.REPO/'tests/swing_b_rejection.rs',fork/'crates/n-core/tests/swing_b_rejection.rs')
    p=w.read(main.RUN/'protocol.json');base=p['templates'][0];ts=[]
    for profile in PROFILES:
        t=copy.deepcopy(base);t.update(name='P2_transition_'+profile,variant=profile,transition='base',micro_confirm=False,micro_exit=profile!='base',micro_profit_only=profile=='rejection_profit');ts.append(t)
    p.update(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),templates=ts,script_sha256=w.sha(__file__),main_results_sha256=w.sha(main.RUN/'results.json'),
        experiment='Two exploratory exit hypotheses AFTER four raised-entry models failed, plus exact best-a reference. Same frozen parentB evidence, original ENTRY/STOP/ABSOLUTE TARGET. No threshold grid or product/month filters.',
        exit_rule='Only original warnings still below/above the known internalB turn (same required flag). AFTER actual entry, first completed1m close clears micro level (pivot+1tick longs/-1tick shorts). On a LATER completed1m close strictly back through raw pivot, schedule exit next1m open with adverse slip. any always exits; profit variant requires current gross close profit>0, not a promise next open is profitable. No pre-entry arming or same-bar rejection.',
        qualification='Same best-a both-period cost/occupancy gates; keep all failed raised-entry and exit models visible. Independent holdout absent, named best-a snapshot never overwritten.')
    for q in ('early','recent'):shutil.copy2(main.RUN/(q+'-pool.json'),RUN/(q+'-pool.json'))
    w.write(RUN/'protocol.json',p);w.write(RUN/'implementation-manifest.json',{f.relative_to(fork).as_posix():w.sha(f) for f in fork.rglob('*') if f.is_file()});print('Frozen2 exploratory B-progress rejection exits, best-a entry unchanged',flush=True)

def build():
    p=w.read(RUN/'protocol.json');assert w.sha(__file__)==p['script_sha256'];args=['--offline','--manifest-path',str(RUN/'fork/Cargo.toml'),'--target-dir',str(w.REPO/'target'),'-p','n-core']
    subprocess.run(['cargo','test',*args,'--test','swing_b_identity','--test','swing_b_rejection','--','--test-threads=1'],check=True)
    subprocess.run(['cargo','build','--release',*args,'--bin','swing_b_rejection_execution'],check=True);w.write(RUN/'built-binaries.json',{'swing_b_rejection_execution.exe':w.sha(w.REPO/'target/release/swing_b_rejection_execution.exe')})

def period_run(q):
    main.verify_snapshot();p=w.read(RUN/'protocol.json');assert all(w.sha(path)==p['hashes'][q][k] for k,path in p['periods'][q].items());assert all(w.sha(RUN/'fork'/k)==v for k,v in w.read(RUN/'implementation-manifest.json').items());assert w.sha(RUN/(q+'-pool.json'))==w.sha(main.RUN/(q+'-pool.json'));assert all(w.sha(w.REPO/'target/release'/k)==v for k,v in w.read(RUN/'built-binaries.json').items())
    subprocess.run([str(w.REPO/'target/release/swing_b_rejection_execution.exe'),str(RUN/'protocol.json'),q,'replay',str(RUN/q)],check=True)

if __name__=='__main__':
    a=argparse.ArgumentParser(description=__doc__);a.add_argument('stage',choices=['prepare','build','early','recent']);stage=a.parse_args().stage;(period_run(stage) if stage in ('early','recent') else globals()[stage]())
