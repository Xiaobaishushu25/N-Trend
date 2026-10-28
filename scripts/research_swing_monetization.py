"""Explicit exploratory follow-up AFTER entry-quality failures; not a holdout."""
import argparse,copy,datetime as dt,pathlib,shutil,subprocess
import research_swing_cycle as c
PARENT=c.RUN
c.RUN=PARENT/'monetization-supplement'

def prepare():
    c.RUN.mkdir(exist_ok=False);root=c.RUN/'fork';shutil.copytree(PARENT/'fork',root)
    path=root/'crates/n-core/src/v3/ablation.rs';s=path.read_text(encoding='utf-8')
    s=c.replace_once(s,'            fill + sign * distance','''            let price=fill+sign*distance;
            if sign>0. {(price/ev.tick_size+1e-9).floor()*ev.tick_size}
            else {(price/ev.tick_size-1e-9).ceil()*ev.tick_size}''');path.write_text(s,encoding='utf-8')
    shutil.copy2(c.REPO/'tests/swing_monetization.rs',root/'crates/n-core/tests/swing_monetization.rs')
    for period in ('early','recent'):shutil.copy2(PARENT/(period+'-pool.json'),c.RUN/(period+'-pool.json'))
    p=c.read(PARENT/'protocol.json');base=copy.deepcopy(p['templates'][0]);templates=[]
    for name,cap in [('base',None),('cap125',1.25),('cap150',1.5),('cap200',2.)]:
        t=copy.deepcopy(base);t['name']='P2_cycle_'+name;t['variant']=name;t['rules']['target_cap_r']=cap;templates.append(t)
    p.update(id=c.RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),templates=templates,online_windows=[],
        parent_results_sha256=c.sha(PARENT/'results.json'),historical='Exploratory AFTER entry-quality/online failures. Both periods repeatedly inspected; no holdout claim.',
        entry_quality='Entries identical to P2_local_wide. Only target changes: closer of confirmed local target and actual-fill risk multiple1.25/1.5/2, conservatively rounded toward fill to valid tick. Frozen forecast/actual netRR>=1 retained; no relaxed cost guard. No new stop or post-hoc product/month selection.',
        geometry='Local target capped at frozen multiple of actual initial S2 risk. No fractional-tick target. No dynamic stop or sizing.',
        online='No online gate in this supplementary batch.')
    c.write(c.RUN/'protocol.json',p)
    c.write(c.RUN/'implementation-manifest.json',{str(q.relative_to(root)):c.sha(q) for q in root.rglob('*') if q.is_file()})
    c.write(c.RUN/'test-fixture-amendment-before-outcomes.json',dict(files={},reason='All fixtures finalized before supplementary outcomes.'))
    print(c.RUN,flush=True)

def run():
    subprocess.run(['cargo','test','--offline','--manifest-path',str(c.RUN/'fork/Cargo.toml'),'--target-dir',str(c.REPO/'target'),'-p','n-core','--test','swing_monetization'],check=True)
    c.run()

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('stage',choices=['prepare','run','report']);a=p.parse_args()
    (c.report if a.stage=='report' else globals()[a.stage])()
