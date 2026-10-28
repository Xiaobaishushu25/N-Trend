"""Frozen causal A invalidation / no-resurrection experiment; isolated fork only."""
import argparse,copy,datetime as dt,json,shutil,subprocess
import research_swing_wave_selection as w

SOURCE=w.RUN
RUN=w.REPO/'research-runs/swing-retirement-20261025-v1'
MODELS={'near_start':2,'current_failure':5,'retired_origin':6,'current_owner':7}

HELPER=r'''
// Research only. A meaningful impulse uses the existing size/body/length/rollover
// tests, deliberately BEFORE current B grade or S0-price survival is considered.
pub fn research_impulse_owner(level:&'static str,dir:Dir,b_end:Swing,bars:&[Bar],starts:&[Swing],trend_k:&[(bool,bool)],max_a:usize,max_b:usize)->Option<NPattern>{
    let atr=indicators::atr(bars,ATR_PERIOD);
    let relaxed=indicators::trend_flags_relaxed(bars,&atr);
    let mut owner:Option<NPattern>=None;
    for s0 in starts.iter().rev(){
        if s0.index>=b_end.index{continue;}
        if b_end.index-s0.index>max_a+max_b{break;}
        let Some(s1)=extreme_between(bars,s0.index,b_end.index,dir==Dir::Up)else{continue;};
        if s1.index-s0.index<2 || b_end.index-s1.index<2 || s1.index-s0.index>max_a || b_end.index-s1.index>max_b{continue;}
        let Some(p)=make_pattern(level,dir,*s0,s1,b_end,bars,trend_k)else{continue;};
        if is_small_n(&p,&atr)||a_leg_strong_count(&p,&relaxed)<MIN_STRONG_A_LEG||crate::analyze::pattern_window_has_rollover_until(bars,&p,b_end.index){continue;}
        if owner.as_ref().is_none_or(|b|(p.s0.index,p.s1.index)>(b.s0.index,b.s1.index)){owner=Some(p);}
    }
    owner
}
pub fn research_owner_failed(p:&NPattern)->bool{p.hard_failure||p.retracement>0.80}
#[derive(Default,Debug)]
pub struct ResearchOriginState{pub retired:Option<usize>}
impl ResearchOriginState{
    pub fn observe(&mut self,mode:u8,owner:Option<(usize,usize,bool)>){
        if mode==6{if let Some((s0,_,true))=owner{self.retired=Some(self.retired.map_or(s0,|r|r.max(s0)));}}
    }
    pub fn allows(&self,mode:u8,owner:Option<(usize,usize,bool)>,s0:usize,s1:usize)->bool{
        match mode{
            5=>owner.is_none_or(|(r,_,bad)|!bad||s0>r),
            6=>self.retired.is_none_or(|r|s0>r),
            7=>owner.is_some_and(|(a,b,_)|s0==a&&s1==b),
            _=>true,
        }
    }
}
#[derive(Clone,serde::Serialize)]
pub struct ResearchStateTrace{pub w:usize,pub direction:&'static str,pub owner_s0:Option<usize>,pub owner_s1:Option<usize>,pub owner_failed:bool,pub retired:Option<usize>,pub newly_retired:bool,pub blocked_s0:Option<usize>,pub blocked_s1:Option<usize>,pub would_warn:bool,pub warning_score:Option<f64>}
static RESEARCH_TRACE:std::sync::Mutex<Vec<ResearchStateTrace>>=std::sync::Mutex::new(Vec::new());
pub fn research_trace_clear(){RESEARCH_TRACE.lock().unwrap().clear();}
pub fn research_trace_push(x:ResearchStateTrace){RESEARCH_TRACE.lock().unwrap().push(x);}
pub fn research_trace_take()->Vec<ResearchStateTrace>{std::mem::take(&mut *RESEARCH_TRACE.lock().unwrap())}
'''

EVENT=r'''
        if pattern::research_selector()>=5 {
            let mode=pattern::research_selector();
            for (di,dir) in [Dir::Up,Dir::Down].into_iter().enumerate(){
                let mut owners=Vec::new();
                for (level,swings,ma,mb) in [("fine",&swings_fine,pattern::FINE_MAX_A_BARS,pattern::FINE_MAX_B_BARS),("large",&swings_large,pattern::LARGE_MAX_A_BARS,pattern::LARGE_MAX_B_BARS)]{
                    let starts=swings.iter().copied().filter(|s|s.is_high==(dir==Dir::Down)).collect::<Vec<_>>();
                    let end=Swing{index:w,price:if dir==Dir::Up{bars[w].low}else{bars[w].high},is_high:dir==Dir::Down};
                    if let Some(p)=pattern::research_impulse_owner(level,dir,end,prefix,&starts,&trend_k,ma,mb){owners.push(p);}
                }
                let owner=owners.iter().max_by_key(|p|(p.s0.index,p.s1.index));
                let key=owner.map(|p|(p.s0.index,p.s1.index,pattern::research_owner_failed(p)));
                let before=origin_states[di].retired;origin_states[di].observe(mode,key);
                let newly=before!=origin_states[di].retired;
                if newly{pattern::research_trace_push(pattern::ResearchStateTrace{w,direction:if dir==Dir::Up{"up"}else{"down"},owner_s0:key.map(|k|k.0),owner_s1:key.map(|k|k.1),owner_failed:key.is_some_and(|k|k.2),retired:origin_states[di].retired,newly_retired:true,blocked_s0:None,blocked_s1:None,would_warn:false,warning_score:None});}
                patterns.retain(|p|{
                    if p.dir!=dir||origin_states[di].allows(mode,key,p.s0.index,p.s1.index){return true;}
                    let c=candidate_for(prefix,&atr20,&trend_k,&trend_k_relaxed,p,w,tick);
                    pattern::research_trace_push(pattern::ResearchStateTrace{w,direction:if dir==Dir::Up{"up"}else{"down"},owner_s0:key.map(|k|k.0),owner_s1:key.map(|k|k.1),owner_failed:key.is_some_and(|k|k.2),retired:origin_states[di].retired,newly_retired:false,blocked_s0:Some(p.s0.index),blocked_s1:Some(p.s1.index),would_warn:c.is_some(),warning_score:c.map(|c|c.entry_score)});
                    false
                });
            }
        }
'''

def prepare():
    RUN.mkdir(exist_ok=False);fork=RUN/'fork';shutil.copytree(SOURCE/'fork',fork)
    f=fork/'crates/n-core/src/analyze/pattern.rs';x=f.read_text(encoding='utf-8');x=w.replace_once(x,'assert!(mode<=3)','assert!(mode<=7)');x+=HELPER;f.write_text(x,encoding='utf-8')
    f=fork/'crates/n-core/src/analyze/event.rs';x=f.read_text(encoding='utf-8');x=w.replace_once(x,'    for w in 0..bars.len() {','    pattern::research_trace_clear();\n    let mut origin_states=[pattern::ResearchOriginState::default(),pattern::ResearchOriginState::default()];\n    for w in 0..bars.len() {');x=w.replace_once(x,'        for p in patterns {',EVENT+'\n        for p in patterns {');f.write_text(x,encoding='utf-8')
    runner=(w.REPO/'scripts/swing_wave_pool.rs').read_text(encoding='utf-8')
    runner=w.replace_once(runner,'        let (bars,_) =engine.aggregate_with_rollovers(&raw,&rolls);','        let trace=n_core::analyze::pattern::research_trace_take();\n        let (bars,_) =engine.aggregate_with_rollovers(&raw,&rolls);')
    runner=w.replace_once(runner,'"raw_keys":rawkeys,"kept_keys":kept,"pool":pool','"raw_keys":rawkeys,"kept_keys":kept,"state_trace":trace,"identification_times":bars.iter().map(|b|b.dt.to_bar_ts()).collect::<Vec<_>>(),"pool":pool')
    (fork/'crates/n-core/src/bin/swing_retirement_pool.rs').write_text(runner,encoding='utf-8')
    shutil.copy2(fork/'crates/n-core/src/bin/swing_wave_execution.rs',fork/'crates/n-core/src/bin/swing_retirement_execution.rs')
    shutil.copy2(w.REPO/'tests/swing_retirement.rs',fork/'crates/n-core/tests/swing_retirement.rs')
    p=w.read(SOURCE/'protocol.json');t=next(t for t in p['templates'] if t['variant']=='adaptive');p.update(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),templates=[t],models=MODELS,
        parent_results_sha256=w.sha(SOURCE/'results.json'),script_sha256=w.sha(__file__),production_changed=False,qualified_for_trading=False,
        experiment='Three pre-frozen state mechanisms plus exact near_start/adaptive control. All source bars and raw warnings regenerated; service forward dedup and trigger-time capacity recomputed. No product/month selection or execution tuning.',
        owner='Latest confirmed S0 then interval S1 across fine/large, existing minA3/minB2/max lengths, nonmicroN, relaxed strongA>=1 and rollover eligibility; before current B grade and S0 survival. This is a hypothesis proxy for a meaningful impulse, not ground truth.',
        current_failure='If latest meaningful impulse hard-fails existing endpoint/path/deepV rules OR retracement>.80, disallow candidates S0<=itsS0 on current bar. No lasting memory.',
        retired_origin='Same failures observed on every completed15m bar permanently retire that direction S0 and all earlier origins. A fresh later confirmedS0 can restart. No reset by renewed high/low at the same origin, no future fill/outcome state.',
        current_owner='Only current latest meaningful impulse S0/S1 can warn; no fallback even when too shallow. No lasting memory. Stricter mechanism ablation.',
        preserved='Prior frozen near_start/adaptive execution, original S2/local target, fees, calendars, all4 entry/slippage stresses, two ambiguity exposure policies. No A breakout condition.',
        gates='Vs frozen near_start/adaptive: recent>=80% cash in all4 stresses and both occupancy policies; early cash/meanR/DD/unknown noninferior in all4; full stability additionally both periods cash>0 and meanR>0, counts>=35 and>=30% reference, recent win>=reference, tail10 noninferior. No live qualification on researched data.')
    w.write(RUN/'protocol.json',p)
    for name,mode in MODELS.items():
        d=RUN/name;d.mkdir();q=copy.deepcopy(p);q.update(selector=mode,parent_protocol_sha256=w.sha(RUN/'protocol.json'));w.write(d/'protocol.json',q)
    w.write(RUN/'implementation-manifest.json',{q.relative_to(fork).as_posix():w.sha(q) for q in fork.rglob('*') if q.is_file()})
    print('Frozen 3 mechanisms +1 control before replay:',RUN,flush=True)

def build():
    p=w.read(RUN/'protocol.json');assert w.sha(__file__)==p['script_sha256'];args=['--offline','--manifest-path',str(RUN/'fork/Cargo.toml'),'--target-dir',str(w.REPO/'target'),'-p','n-core']
    subprocess.run(['cargo','test',*args,'--test','swing_retirement','--','--test-threads=1'],check=True)
    subprocess.run(['cargo','build','--release',*args,'--bin','swing_retirement_pool','--bin','swing_retirement_execution'],check=True)
    w.write(RUN/'built-binaries.json',{name:w.sha(w.REPO/'target/release'/name) for name in ('swing_retirement_pool.exe','swing_retirement_execution.exe')})

def period_run(q):
    p=w.read(RUN/'protocol.json');assert all(w.sha(v)==p['hashes'][q][k] for k,v in p['periods'][q].items());assert all(w.sha(RUN/'fork'/k)==h for k,h in w.read(RUN/'implementation-manifest.json').items());assert all(w.sha(w.REPO/'target/release'/k)==h for k,h in w.read(RUN/'built-binaries.json').items())
    for model in MODELS:
        folder=RUN/model
        subprocess.run([str(w.REPO/'target/release/swing_retirement_pool.exe'),str(folder/'protocol.json'),q,str(folder/(q+'-symbols')),str(folder/(q+'-pool.json'))],check=True)
        subprocess.run([str(w.REPO/'target/release/swing_retirement_execution.exe'),str(folder/'protocol.json'),q,'replay',str(folder/q)],check=True)

if __name__=='__main__':
    a=argparse.ArgumentParser(description=__doc__);a.add_argument('stage',choices=['prepare','build','early','recent']);stage=a.parse_args().stage;(period_run(stage) if stage in ('early','recent') else globals()[stage]())
