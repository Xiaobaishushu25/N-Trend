"""Regenerate AB candidates causally; compare finite wave/entry models, no live edits."""
import argparse,copy,datetime as dt,json,pathlib,shutil,subprocess
from research_swing_consistency import REPO,read,write,sha,replace_once
SOURCE=REPO/'research-runs/swing-structure-mechanisms-20261025-v1'
RUN=REPO/'research-runs/swing-wave-selection-20261025-v1'
SELECTORS={'legacy':0,'valid_score':1,'near_start':2,'near_turn':3}

def patch(fork):
    path=fork/'crates/n-core/src/analyze/pattern.rs';x=path.read_text(encoding='utf-8')
    pre='''
// Isolated research selector; default0 preserves legacy ranking exactly.
static RESEARCH_SELECTOR: std::sync::atomic::AtomicU8=std::sync::atomic::AtomicU8::new(0);
pub fn research_set_selector(mode:u8){assert!(mode<=3);RESEARCH_SELECTOR.store(mode,std::sync::atomic::Ordering::Relaxed);}
pub fn research_selector()->u8{RESEARCH_SELECTOR.load(std::sync::atomic::Ordering::Relaxed)}
pub fn research_priority(mode:u8,a0:usize,a1:usize,b0:usize,b1:usize)->Option<bool>{
    if mode==3 && a1!=b1{return Some(a1>b1);}
    if mode>=2 && a0!=b0{return Some(a0>b0);}
    None
}
'''
    x=replace_once(x,'pub const FINE_MAX_A_BARS: usize = 16;',pre+'\npub const FINE_MAX_A_BARS: usize = 16;')
    x=replace_once(x,'''    let mut best: Option<NPattern> = None;
    let atr20 = indicators::atr(bars, ATR_PERIOD);''','''    let mut best: Option<NPattern> = None;
    let atr20 = indicators::atr(bars, ATR_PERIOD);
    let selector=research_selector();
    let relaxed=if selector>0 {Some(indicators::trend_flags_relaxed(bars,&atr20))}else{None};''')
    x=replace_once(x,'''        if let Some(p) = make_pattern(level, dir, *s0, s1, b_end, bars, trend_k) {
            let cur_score''','''        if let Some(p) = make_pattern(level, dir, *s0, s1, b_end, bars, trend_k) {
            if selector>0 && (!matches!(p.grade,Grade::A|Grade::B|Grade::C)
                || is_small_n(&p,&atr20) || a_leg_strong_count(&p,relaxed.as_ref().unwrap())<MIN_STRONG_A_LEG
                || crate::analyze::pattern_window_has_rollover_until(bars,&p,b_end.index)) {continue;}
            let cur_score''')
    x=replace_once(x,'''                if p.hard_failure != b.hard_failure {
                    !p.hard_failure
                } else if cur_score''','''                if let Some(prefer)=research_priority(selector,p.s0.index,p.s1.index,b.s0.index,b.s1.index) {
                    prefer
                } else if p.hard_failure != b.hard_failure {
                    !p.hard_failure
                } else if cur_score''')
    path.write_text(x,encoding='utf-8')
    path=fork/'crates/n-core/src/analyze/event.rs';x=path.read_text(encoding='utf-8')
    x=replace_once(x,'Some(existing) if existing.entry_score >= c.entry_score => {}','''Some(existing) if !pattern::research_priority(pattern::research_selector(),c.s0_index,c.s1_index,existing.s0_index,existing.s1_index)
                        .unwrap_or(c.entry_score>existing.entry_score) => {}''')
    path.write_text(x,encoding='utf-8')
    path=fork/'crates/n-core/src/v2/replay/engine.rs';x=path.read_text(encoding='utf-8')
    start=x.index('            let result = crate::v3::replay::evaluate(ev, raw5m, rollovers, false, 1.0);');end=x.index('            ev.evaluation = Some(result);',start)+len('            ev.evaluation = Some(result);')
    x=x[:start]+'            // Research pool generation stops at warning-time context; no future labels.\n'+x[end:];path.write_text(x,encoding='utf-8')
    runner=(SOURCE/'fork/crates/n-core/src/bin/swing_bextreme_runner.rs').read_text(encoding='utf-8')
    runner=replace_once(runner,'                for slip in [1., 2.] {','''                if template["b_confirm"]==true {
                    let weakened=snap["b_weakening_ex_warning"].as_f64().is_some_and(|r|r<=0.75);
                    let eligible_kind=template["b_all"]==true || original.warning_kind=="strong";
                    if !weakened && eligible_kind && original.warning_kind!="cumulative" {
                        rules.entry_mode=ablation::EntryMode::CloseConfirmNextOpen;
                    }
                }
                for slip in [1., 2.] {''')
    (fork/'crates/n-core/src/bin/swing_wave_execution.rs').write_text(runner,encoding='utf-8')
    shutil.copy2(REPO/'scripts/swing_wave_pool.rs',fork/'crates/n-core/src/bin/swing_wave_pool.rs')
    shutil.copy2(REPO/'tests/swing_wave_selection.rs',fork/'crates/n-core/tests/swing_wave_selection.rs')

def prepare():
    RUN.mkdir(exist_ok=False);fork=RUN/'fork';shutil.copytree(SOURCE/'fork',fork);patch(fork)
    parent=read(SOURCE/'protocol.json');base=parent['templates'][0];templates=[]
    for name,adaptive,b_confirm,b_all in [('base',False,False,False),('adaptive',True,False,False),('b_strong',False,True,False),('b_all',False,True,True)]:
        t=copy.deepcopy(base);t.update(name='P2_wave_'+name,variant=name,adaptive=adaptive,b_confirm=b_confirm,b_all=b_all,b_extreme=False);templates.append(t)
    parent.update(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),templates=templates,selectors=SELECTORS,
        experiment='Four wave selectors x four execution profiles,16 finite models. All historical data previously researched, no independent holdout. Regenerate ALL warning candidates and service-forward dedup; never replace AB anchors only on selected losing trades.',
        selectors_definition='legacy exact score selection; valid_score prefilters the same warning-eligible grades/microN/minstrong/rollover checks BEFORE best-score selection; near_start picks latest eligible S0 then score; near_turn latest eligible S1 then S0 then score. Same priorities across fine/large. No new lengths, A-breakout, score weights, product/month fit.',
        b_definition='Exclude current warning candle, use opposing B bodies and second-half/first-half average size ratio<=.75 as existing weakening definition. If ratio missing or>.75, strong-only / all noncumulative warnings require completed1m close then next1m open. All stops/targets/costs remain original rule. Do not choose mode by later fills or outcomes.',
        baseline='Regenerated legacy is a NEW implementation-consistent reference. Compare retained archive baseline/observation too; disclose all mismatches, do not silently replace earlier results.',
        gates='Both periods positive cash AND meanR in all4 entry/cost scenarios; recent>=80% BOTH regenerated legacy reference and archived observation; >=35 trades and>=30% legacy count; DD/tail10/unknown/winrate no worse than regenerated reference. Never live-promote on researched history.',
        script_sha256=sha(__file__),production_changed=False,qualified_for_trading=False)
    write(RUN/'protocol.json',parent)
    for name,selector in SELECTORS.items():
        folder=RUN/name;folder.mkdir();p=copy.deepcopy(parent);p.update(id=name,selector=selector,parent_protocol_sha256=sha(RUN/'protocol.json'));write(folder/'protocol.json',p)
    write(RUN/'implementation-manifest.json',{str(q.relative_to(fork)):sha(q) for q in fork.rglob('*') if q.is_file()})
    print('Frozen wave/entry factorial16 models',RUN,flush=True)

def build():
    p=read(RUN/'protocol.json');assert sha(__file__)==p['script_sha256']
    args=['--offline','--manifest-path',str(RUN/'fork/Cargo.toml'),'--target-dir',str(REPO/'target'),'-p','n-core']
    subprocess.run(['cargo','test',*args,'--test','swing_wave_selection','--','--test-threads=1'],check=True)
    subprocess.run(['cargo','build','--release',*args,'--bin','swing_wave_pool','--bin','swing_wave_execution'],check=True)

def period_run(period):
    p=read(RUN/'protocol.json');assert all(sha(v)==p['hashes'][period][k] for k,v in p['periods'][period].items())
    assert all(sha(RUN/'fork'/k)==v for k,v in read(RUN/'implementation-manifest.json').items())
    for selector in SELECTORS:
        folder=RUN/selector
        subprocess.run([str(REPO/'target/release/swing_wave_pool.exe'),str(folder/'protocol.json'),period,str(folder/(period+'-symbols')),str(folder/(period+'-pool.json'))],check=True)
        subprocess.run([str(REPO/'target/release/swing_wave_execution.exe'),str(folder/'protocol.json'),period,'replay',str(folder/period)],check=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('stage',choices=['prepare','build','early','recent']);a=p.parse_args()
    (period_run(a.stage) if a.stage in ('early','recent') else globals()[a.stage]())
