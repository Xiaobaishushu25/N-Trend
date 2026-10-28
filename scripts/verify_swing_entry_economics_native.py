"""Compile/replay isolated pre-entry gates, verify equivalent to cached veto study."""
import argparse,copy,datetime as dt,json,shutil,subprocess
import research_swing_entry_economics as r
w=r.w;RUN=r.RUN/'native-verification'

def prepare():
    RUN.mkdir(exist_ok=False);fork=RUN/'fork';shutil.copytree(r.SNAP/'fork',fork)
    source=(fork/'crates/n-core/src/bin/swing_wave_execution.rs').read_text(encoding='utf-8')
    helper='''
fn room_covers_cost(entry:f64,target:f64,sign:f64,cost:f64)->bool{
    let space=(target-entry)*sign;
    space.is_finite()&&cost.is_finite()&&space>0.&&cost>=0.&&cost/space<=0.10+1e-12
}
'''
    source=w.replace_once(source,'fn main() -> Result<()> {',helper+'\nfn main() -> Result<()> {')
    source=w.replace_once(source,'            for mode in p["modes"].as_array().unwrap() {','''            let economic_ok=template["economic_gate"]!=true || room_covers_cost(ev.entry,ev.target,if ev.direction=="up"{1.}else{-1.},new_friction*ev.risk);
            let body_ok=template["body_net_gate"]!=true || snap["entry_quality"]["a_body_net_ex_s0"].as_f64().is_some_and(|x|x>0.);
            for mode in p["modes"].as_array().unwrap() {''')
    source=w.replace_once(source,'let mut result = if eligible && geometry_ok {','let mut result = if eligible && geometry_ok && economic_ok && body_ok {')
    source=w.replace_once(source,'''} else {
                            "filtered_geometry"
                        }
                        .into();''','''} else if !geometry_ok {
                            "filtered_geometry"
                        } else if !economic_ok {"filtered_entry_economics"} else {"filtered_A_net_body"}
                        .into();''')
    source=w.replace_once(source,'"local_level_time":local_source,','"local_level_time":local_source,"economic_pass":economic_ok,"body_net_pass":body_ok,')
    source+='''
#[cfg(test)] mod economic_tests{use super::*;
#[test]fn symmetric_budget_and_zero_or_invalid_space(){assert!(room_covers_cost(100.,120.,1.,2.));assert!(room_covers_cost(100.,80.,-1.,2.));assert!(!room_covers_cost(100.,120.,1.,2.01));assert!(!room_covers_cost(100.,100.,1.,0.));assert!(!room_covers_cost(100.,120.,1.,f64::NAN));}
#[test]fn room_budget_is_independent_of_stop_width_and_trade_outcome(){let cost=2.;assert!(room_covers_cost(100.,120.,1.,cost));assert!(!room_covers_cost(100.,110.,1.,cost));assert!(room_covers_cost(100.,120.,1.,0.));}
}
'''
    (fork/'crates/n-core/src/bin/swing_entry_economics_execution.rs').write_text(source,encoding='utf-8')
    p=w.read(r.SNAP/'protocol.json');base=p['templates'][0];ts=[]
    for name in ['base','budget10','budget_body']:
        t=copy.deepcopy(base);t.update(name='P2_economics_'+name,variant=name,economic_gate=name!='base',body_net_gate=name=='budget_body');ts.append(t)
    p.update(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),templates=ts,script_sha256=w.sha(__file__),parent_protocol_sha256=w.sha(r.RUN/'protocol.json'),
        purpose='Implementation verification ONLY after economics study; same fixed10% andnetbody>0. Full isolated price execution applies gates before simulated trigger. No new parameter or model. Reproduce selected unmerged results and frozen base exactly.',production_changed=False,qualified_for_trading=False)
    feat=w.read(r.RUN/'warning-features.json')
    for q in ['early','recent']:
        pool=w.read(r.FAMILIES['unmerged']/(q+'-pool.json'))
        for z in pool:z['entry_quality']=feat['unmerged/'+q][z['event_key']]
        w.write(RUN/(q+'-pool.json'),pool)
    w.write(RUN/'protocol.json',p);w.write(RUN/'implementation-manifest.json',{f.relative_to(fork).as_posix():w.sha(f) for f in fork.rglob('*') if f.is_file()});print('Isolated native economics gate frozen; no new strategy hypotheses',flush=True)

def build():
    args=['--offline','--manifest-path',str(RUN/'fork/Cargo.toml'),'--target-dir',str(w.REPO/'target'),'-p','n-core'];subprocess.run(['cargo','test',*args,'--bin','swing_entry_economics_execution','--','--test-threads=1'],check=True);subprocess.run(['cargo','build','--release',*args,'--bin','swing_entry_economics_execution'],check=True);w.write(RUN/'built-binaries.json',{'swing_entry_economics_execution.exe':w.sha(w.REPO/'target/release/swing_entry_economics_execution.exe')})

def period_run(q):
    p=w.read(RUN/'protocol.json');assert all(w.sha(path)==p['hashes'][q][k] for k,path in p['periods'][q].items());assert all(w.sha(RUN/'fork'/k)==v for k,v in w.read(RUN/'implementation-manifest.json').items());assert all(w.sha(w.REPO/'target/release'/k)==v for k,v in w.read(RUN/'built-binaries.json').items());subprocess.run([str(w.REPO/'target/release/swing_entry_economics_execution.exe'),str(RUN/'protocol.json'),q,'replay',str(RUN/q)],check=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('stage',choices=['prepare','build','early','recent']);stage=p.parse_args().stage;(period_run(stage) if stage in ['early','recent'] else globals()[stage]())
