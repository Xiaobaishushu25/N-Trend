"""Separate actual S0 breach from broader existing deep/V invalidity BEFORE PnL inspection."""
import argparse,copy,datetime as dt,json,shutil,subprocess
import research_swing_retirement as r
w=r.w;RUN=r.RUN/'price-only-extension';MODELS={'current_origin_break':8,'retired_origin_break':9}

def prepare():
    RUN.mkdir(exist_ok=False);fork=RUN/'fork';shutil.copytree(r.RUN/'fork',fork)
    f=fork/'crates/n-core/src/analyze/pattern.rs';x=f.read_text(encoding='utf-8');x=w.replace_once(x,'assert!(mode<=7)','assert!(mode<=9)');x=w.replace_once(x,'if mode==6{if let Some((s0,_,true))=owner','if mode==6||mode==9{if let Some((s0,_,true))=owner');x=w.replace_once(x,'            5=>owner.is_none_or','            5|8=>owner.is_none_or');x=w.replace_once(x,'            6=>self.retired.is_none_or','            6|9=>self.retired.is_none_or')
    x+='''\npub fn research_origin_broken(p:&NPattern,bars:&[Bar])->bool{bars[p.s1.index+1..=p.s2.index].iter().any(|b|if p.dir==Dir::Up{b.low<=p.s0.price}else{b.high>=p.s0.price})}\n''';f.write_text(x,encoding='utf-8')
    f=fork/'crates/n-core/src/analyze/event.rs';x=f.read_text(encoding='utf-8');x=w.replace_once(x,'pattern::research_owner_failed(p)))','if mode>=8{pattern::research_origin_broken(p,prefix)}else{pattern::research_owner_failed(p)}))');f.write_text(x,encoding='utf-8')
    shutil.copy2(fork/'crates/n-core/src/bin/swing_retirement_pool.rs',fork/'crates/n-core/src/bin/swing_origin_break_pool.rs')
    test=fork/'crates/n-core/tests/swing_retirement.rs';x=test.read_text(encoding='utf-8').replace('for mode in [2,5,6,7]','for mode in [2,5,6,7,8,9]');x=x.replace('s.observe(6,Some((12,18,true)));','s.observe(6,Some((12,18,true)));\n    let mut price=ResearchOriginState::default();price.observe(9,Some((12,18,true)));assert!(!price.allows(9,None,12,24));assert!(price.allows(9,None,19,24));');test.write_text(x,encoding='utf-8')
    p=w.read(r.RUN/'protocol.json');p.update(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),models=MODELS,script_sha256=w.sha(__file__),
        amendment='Before opening/summarizing any retirement PnL, distinguish literal price break of S0 from original >80% retracement/deepV failure. Two extra models, no numerical grid. Structural-count inspection only preceded amendment; main/extension failures all retained.',
        failure='Only B-path low<=S0 for longs or high>=S0 for shorts; no >.8 or deepV alone. Same meaningful impulse and current-only/permanent memory alternatives, same execution and gates.')
    w.write(RUN/'protocol.json',p)
    for model,mode in MODELS.items():
        folder=r.RUN/model;folder.mkdir();q=copy.deepcopy(p);q.update(selector=mode);w.write(folder/'protocol.json',q)
    w.write(RUN/'implementation-manifest.json',{f.relative_to(fork).as_posix():w.sha(f) for f in fork.rglob('*') if f.is_file()});print('Frozen literal-origin-break extension before PnL inspection',flush=True)

def build():
    p=w.read(RUN/'protocol.json');assert w.sha(__file__)==p['script_sha256'];args=['--offline','--manifest-path',str(RUN/'fork/Cargo.toml'),'--target-dir',str(w.REPO/'target'),'-p','n-core']
    subprocess.run(['cargo','test',*args,'--test','swing_retirement','--','--test-threads=1'],check=True)
    subprocess.run(['cargo','build','--release',*args,'--bin','swing_origin_break_pool'],check=True)
    w.write(RUN/'built-binaries.json',{'swing_origin_break_pool.exe':w.sha(w.REPO/'target/release/swing_origin_break_pool.exe')})

def period_run(q):
    p=w.read(RUN/'protocol.json');assert all(w.sha(v)==p['hashes'][q][k] for k,v in p['periods'][q].items());assert all(w.sha(RUN/'fork'/k)==h for k,h in w.read(RUN/'implementation-manifest.json').items());assert all(w.sha(w.REPO/'target/release'/k)==h for k,h in w.read(RUN/'built-binaries.json').items());assert w.sha(w.REPO/'target/release/swing_retirement_execution.exe')==w.read(r.RUN/'built-binaries.json')['swing_retirement_execution.exe']
    for model in MODELS:
        folder=r.RUN/model;subprocess.run([str(w.REPO/'target/release/swing_origin_break_pool.exe'),str(folder/'protocol.json'),q,str(folder/(q+'-symbols')),str(folder/(q+'-pool.json'))],check=True);subprocess.run([str(w.REPO/'target/release/swing_retirement_execution.exe'),str(folder/'protocol.json'),q,'replay',str(folder/q)],check=True)

if __name__=='__main__':
    a=argparse.ArgumentParser(description=__doc__);a.add_argument('stage',choices=['prepare','build','early','recent']);stage=a.parse_args().stage;(period_run(stage) if stage in ('early','recent') else globals()[stage]())
