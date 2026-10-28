"""After C-state failure, finite2x2 payoff/holding experiment under new objective."""
import argparse,copy,datetime as dt,shutil,subprocess
import research_swing_c_structure as parent
w=parent.w;main=parent.main;RUN=parent.RUN/'payoff';PROFILES=['base','cap2','hold16','cap2_hold16']

def prepare():
    main.verify_snapshot();RUN.mkdir(exist_ok=False);p=w.read(main.SNAP/'protocol.json');base=p['templates'][0];templates=[]
    for name in PROFILES:
        t=copy.deepcopy(base);t.update(name='P2_pay_'+name,variant=name);t['rules']['target_cap_r']=2. if name in ('cap2','cap2_hold16') else None;t['rules']['max_holding_buckets']=16 if name in ('hold16','cap2_hold16') else None;templates.append(t)
    p.update(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),templates=templates,script_sha256=w.sha(__file__),executor_sha256=w.sha(main.SNAP/'bin/swing_wave_execution.exe'),
        chronology='Exploratory AFTER6 C-owned exit models all lowered combined profit. Test existing2R cap (P3-style) and16 held15m buckets (4 trading hours), plus combination; finite2x2, no grid, month/product filters or entry/stop/size change.',
        objective='Combined EARLY+RECENT known closed net higher than最佳a, distribution secondary. No recent80% veto or mandatory early positivity; disclose all4 stress and both unknown occupancy policies.',
        execution='Use exact frozen最佳a executable and frozen full dedup warning pools. target_cap_r=2 limits gross reward to original target or2 actual entry-risk units; max_holding16 exits at held bucket close using original service counting. Preserve actual fees, slip, entry guards, no-follow5, calendars and one-symbol opportunity competition.',production_changed=False,qualified_for_trading=False)
    for q in ('early','recent'):shutil.copy2(main.SNAP/(q+'-pool.json'),RUN/(q+'-pool.json'))
    w.write(RUN/'protocol.json',p);print('Frozen original-executable payoff2x2: cap2,hold16 and combination',flush=True)

def period_run(q):
    main.verify_snapshot();p=w.read(RUN/'protocol.json');assert w.sha(__file__)==p['script_sha256'];assert w.sha(main.SNAP/'bin/swing_wave_execution.exe')==p['executor_sha256'];assert all(w.sha(path)==p['hashes'][q][k] for k,path in p['periods'][q].items());assert w.sha(RUN/(q+'-pool.json'))==w.sha(main.SNAP/(q+'-pool.json'));subprocess.run([str(main.SNAP/'bin/swing_wave_execution.exe'),str(RUN/'protocol.json'),q,'replay',str(RUN/q)],check=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('stage',choices=['prepare','early','recent']);stage=p.parse_args().stage;(period_run(stage) if stage in ('early','recent') else globals()[stage]())
