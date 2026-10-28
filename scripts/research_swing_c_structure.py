"""New C-owned confirmed structure; revised user objective is combined net profit."""
import argparse,copy,datetime as dt,shutil,subprocess
import research_swing_b_transition as main
import research_swing_b_rejection as previous
w=main.w;RUN=w.REPO/'research-runs/swing-c-structure-20261025-v1'
PROFILES=['base','hl_close','hl_wick','push_close','push_wick']

def prepare():
    main.verify_snapshot();RUN.mkdir(exist_ok=False);fork=RUN/'fork';shutil.copytree(previous.RUN/'fork',fork)
    module=fork/'crates/n-core/src/v3/ablation/c_structure.rs';shutil.copy2(w.REPO/'scripts/swing_c_structure.rs',module)
    f=fork/'crates/n-core/src/v3/ablation.rs';x=f.read_text(encoding='utf-8');x=w.replace_once(x,'pub mod warning_failure;','pub mod warning_failure;\npub mod c_structure;')
    x=w.replace_once(x,'    pub entry_mode: EntryMode,','    pub entry_mode: EntryMode,\n    pub c_structure: bool,\n    pub c_require_push: bool,\n    pub c_wick_failure: bool,')
    x=w.replace_once(x,'    pending_micro_exit: bool,','    pending_micro_exit: bool,\n    c_state: c_structure::Structure,\n    pending_c_exit: bool,')
    x=w.replace_once(x,'        pending_micro_exit: false,','        pending_micro_exit: false,\n        c_state: Default::default(),\n        pending_c_exit: false,')
    x=w.replace_once(x,'if state.pending_body_exit || state.pending_session_exit || state.pending_micro_exit {','if state.pending_body_exit || state.pending_session_exit || state.pending_micro_exit || state.pending_c_exit {')
    x=w.replace_once(x,'else{"micro_progress_rejected"}','else if state.pending_micro_exit{"micro_progress_rejected"}else{"c_structure_failed"}')
    old='''        if let (Some(level),Some(reject),Some(fill))=(rules.micro_progress_level,rules.micro_rejection_level,state.e.fill){'''
    new='''        if rules.c_structure{
            if let Some(entry)=state.e.entry_ts.as_deref(){
                if state.c_state.observe(bar,entry,sign,ev.tick_size,rules.c_require_push,rules.c_wick_failure){state.pending_c_exit=true;}
            }
        }
'''+old
    x=w.replace_once(x,old,new);f.write_text(x,encoding='utf-8')
    f=fork/'crates/n-core/src/bin/swing_b_rejection_execution.rs';x=f.read_text(encoding='utf-8')
    x=w.replace_once(x,'                for slip in [1., 2.] {','''                rules.c_structure=template["c_mode"]!="base";
                rules.c_require_push=template["c_require_push"]==true;
                rules.c_wick_failure=template["c_wick"]==true;
                for slip in [1., 2.] {
                    ablation::c_structure::clear_trace();''')
    x=w.replace_once(x,'                    let zero = result.fills.iter().any(|f| {','                    let c_trace=ablation::c_structure::take_trace();\n                    let zero = result.fills.iter().any(|f| {')
    x=w.replace_once(x,'"micro_exit_enabled":micro_exit,','"c_structure_mode":template["c_mode"],"c_structure_trace":c_trace,"micro_exit_enabled":micro_exit,')
    (fork/'crates/n-core/src/bin/swing_c_structure_execution.rs').write_text(x,encoding='utf-8');shutil.copy2(w.REPO/'tests/swing_c_structure.rs',fork/'crates/n-core/tests/swing_c_structure.rs')
    p=w.read(previous.RUN/'protocol.json');base=p['templates'][0];ts=[]
    for name in PROFILES:
        t=copy.deepcopy(base);t.update(name='P2_transition_'+name,variant=name,transition='base',micro_confirm=False,micro_exit=False,micro_profit_only=False,c_mode=name,c_require_push=name.startswith('push'),c_wick=name.endswith('wick'));ts.append(t)
    p.update(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),templates=ts,script_sha256=w.sha(__file__),module_sha256=w.sha(w.REPO/'scripts/swing_c_structure.rs'),
        user_objective='User relaxed recent80% requirement. Primary combined EARLY+RECENT known closed net profit vs frozen最佳a, same one-lot size, no annualization/capital enlargement. Prefer better distribution over warning months/products/days and lower loss burden, report all4 costs and both exposure policies. Recent retention and early positivity are diagnostics, NOT vetoes.',
        models='Two structural semantics x two observed failure definitions;4 finite models and exact best-a control. No per-product/month thresholds, no parameter grid or historical outcome-based state.',
        C='Only full5m candles composed of5 contiguous positive-volume1m bars strictly AFTER actual entry minute. Missing/zero/session gaps do not join candles or3-candle pivots. Original parentA/B remains same warning; no oldB line reused.',
        support='Two strict confirmed adverse5m pivots, newer higher(low long)/lower(high short), with an alreadyconfirmed favorable pivot strictly between them. hl activates on right5m close; push additionally needs a LATER1m close beyond intervening favorable peak+1tick (inverse short). Stale candidate invalidates by same failure rule before activation. Active support can only tighten.',
        failure='After support activation minute, close or wick crosses support1tick adverse, schedule exit next observed1m open with adverse slip; do not retroactively apply an intraminute stop or confirmation-bar range. Original stop/target/no-follow/time/calendar exits retain precedence and all entries/stops/absolute targets fixed.',
        qualification='No80% threshold, no forced recent winrate or separate early positivity. Rank primary combined net improvement, disclose cross-cost and both occupancy consistency/unknown risk, balance is secondary rather than a disguised new hard gate. All data already researched, no live qualification.')
    for q in ('early','recent'):shutil.copy2(previous.RUN/(q+'-pool.json'),RUN/(q+'-pool.json'))
    w.write(RUN/'protocol.json',p);w.write(RUN/'implementation-manifest.json',{f.relative_to(fork).as_posix():w.sha(f) for f in fork.rglob('*') if f.is_file()})
    obj=w.REPO/'research-protocols/strategy-objective-20261025-total-net-v2.json'
    w.write(obj,dict(created_at=p['created_at'],source='Explicit user request in this turn',primary='Higher combined Jan-Jun plus available Jul-Oct known closed net profit, equal single-lot exposure and actual fees',secondary=['Less negative-month/period loss','Less dependence on best products/days/trades','Lower realized cash drawdown'],recent80_required=False,separate_period_positive_required=False,reference='最佳a策略best-a-v1',protocol=str(RUN/'protocol.json'),protocol_sha256=w.sha(RUN/'protocol.json'),production_changed=False))
    old={'entry':w.read(main.RUN/'results.json'),'oldB_exit':w.read(previous.RUN/'results.json')};reassessment={}
    for group,stats in old.items():
        reassessment[group]={pol:{suffix:{name:dict(early=stats[pol]['early'][name+'/'+suffix]['net'],recent=stats[pol]['recent'][name+'/'+suffix]['net'],combined=stats[pol]['early'][name+'/'+suffix]['net']+stats[pol]['recent'][name+'/'+suffix]['net']) for name in (main.PROFILES if group=='entry' else previous.PROFILES)} for suffix in ('corrected_touch/slip1','corrected_touch/slip2','next_minute_open/slip1','next_minute_open/slip2')} for pol in ('original','two_paths_flat')}
    w.write(RUN/'previous-six-models-new-objective.json',reassessment);print('Frozen4 own-C models; updated objective: combined net, no80% constraint',flush=True)

def build():
    p=w.read(RUN/'protocol.json');assert w.sha(__file__)==p['script_sha256'];assert w.sha(w.REPO/'scripts/swing_c_structure.rs')==p['module_sha256'];args=['--offline','--manifest-path',str(RUN/'fork/Cargo.toml'),'--target-dir',str(w.REPO/'target'),'-p','n-core']
    subprocess.run(['cargo','test',*args,'--test','swing_c_structure','--','--test-threads=1'],check=True)
    subprocess.run(['cargo','build','--release',*args,'--bin','swing_c_structure_execution'],check=True);w.write(RUN/'built-binaries.json',{'swing_c_structure_execution.exe':w.sha(w.REPO/'target/release/swing_c_structure_execution.exe')})

def period_run(q):
    main.verify_snapshot();p=w.read(RUN/'protocol.json');assert all(w.sha(path)==p['hashes'][q][k] for k,path in p['periods'][q].items());assert all(w.sha(RUN/'fork'/k)==v for k,v in w.read(RUN/'implementation-manifest.json').items());assert w.sha(RUN/(q+'-pool.json'))==w.sha(previous.RUN/(q+'-pool.json'));assert all(w.sha(w.REPO/'target/release'/k)==v for k,v in w.read(RUN/'built-binaries.json').items())
    subprocess.run([str(w.REPO/'target/release/swing_c_structure_execution.exe'),str(RUN/'protocol.json'),q,'replay',str(RUN/q)],check=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('stage',choices=['prepare','build','early','recent']);stage=p.parse_args().stage;(period_run(stage) if stage in ('early','recent') else globals()[stage]())
