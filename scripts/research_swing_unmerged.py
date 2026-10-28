"""One structural extension: retain all confirmed pivots, nearest eligible A."""
import argparse,copy,datetime as dt,json,shutil,subprocess
import research_swing_wave_selection as s
RUN=s.RUN/'unmerged_extension';MODEL=s.RUN/'unmerged'

def prepare():
    RUN.mkdir(exist_ok=False);MODEL.mkdir(exist_ok=False);fork=RUN/'fork';shutil.copytree(s.RUN/'fork',fork)
    path=fork/'crates/n-core/src/analyze/pattern.rs';x=path.read_text(encoding='utf-8');x=s.replace_once(x,'assert!(mode<=3)','assert!(mode<=4)');path.write_text(x,encoding='utf-8')
    path=fork/'crates/n-core/src/analyze/event.rs';x=path.read_text(encoding='utf-8');x=s.replace_once(x,'indicators::find_swings(prefix, &atr20, 2, 8)','indicators::find_swings(prefix, &atr20, 2, if pattern::research_selector()==4 {0}else{8})');x=s.replace_once(x,'indicators::find_swings(prefix, &atr20, 5, 10)','indicators::find_swings(prefix, &atr20, 5, if pattern::research_selector()==4 {0}else{10})');path.write_text(x,encoding='utf-8')
    shutil.copy2(fork/'crates/n-core/src/bin/swing_wave_pool.rs',fork/'crates/n-core/src/bin/swing_wave_pool_unmerged.rs')
    test=fork/'crates/n-core/tests/swing_wave_selection.rs';x=test.read_text(encoding='utf-8').replace('for mode in 0..=3','for mode in 0..=4');test.write_text(x,encoding='utf-8')
    p=s.read(s.RUN/'protocol.json');p.update(id='unmerged_extension',created_at=dt.datetime.now(dt.timezone.utc).isoformat(),selector=4,
        extension='Before model PnL inspection, pool comparison and nested-range definition show near_start and near_turn coincide. Add one structural ablation: keep window2/window5 confirmed pivots but merge_gap=0, retain SAME min-N/grade/score/cost/stop/target rules and choose nearest valid S0. No new threshold grid or product/month rules.',
        script_sha256=s.sha(__file__),qualified_for_trading=False,production_changed=False)
    s.write(RUN/'protocol.json',p);s.write(MODEL/'protocol.json',p);s.write(RUN/'implementation-manifest.json',{str(q.relative_to(fork)):s.sha(q) for q in fork.rglob('*') if q.is_file()});print(RUN,flush=True)

def build():
    p=s.read(RUN/'protocol.json');assert s.sha(__file__)==p['script_sha256'];args=['--offline','--manifest-path',str(RUN/'fork/Cargo.toml'),'--target-dir',str(s.REPO/'target'),'-p','n-core']
    subprocess.run(['cargo','test',*args,'--test','swing_wave_selection','--','--test-threads=1'],check=True)
    subprocess.run(['cargo','build','--release',*args,'--bin','swing_wave_pool_unmerged'],check=True)

def period_run(period):
    p=s.read(RUN/'protocol.json');assert all(s.sha(v)==p['hashes'][period][k] for k,v in p['periods'][period].items());assert all(s.sha(RUN/'fork'/k)==v for k,v in s.read(RUN/'implementation-manifest.json').items())
    subprocess.run([str(s.REPO/'target/release/swing_wave_pool_unmerged.exe'),str(MODEL/'protocol.json'),period,str(MODEL/(period+'-symbols')),str(MODEL/(period+'-pool.json'))],check=True)
    subprocess.run([str(s.REPO/'target/release/swing_wave_execution.exe'),str(MODEL/'protocol.json'),period,'replay',str(MODEL/period)],check=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('stage',choices=['prepare','build','early','recent']);a=p.parse_args();(period_run(a.stage) if a.stage in ('early','recent') else globals()[a.stage]())
