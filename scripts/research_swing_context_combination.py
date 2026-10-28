"""Frozen pairing of hourly alignment with already tested entry rules; exploratory."""
import argparse,copy,datetime as dt,json,shutil
import research_swing_cycle as c
ROOT=c.RUN
from research_swing_context import allows
c.RUN=ROOT/'context-combinations'
MAPPING={'base':'base','h60_base':'base','h60_fresh4':'fresh4','h60_confirm':'confirm','h60_confirm_pre_body':'confirm_pre_body','h60_kind_confirm':'kind_confirm','h60_reclaim':'reclaim'}

def prepare():
    c.RUN.mkdir(exist_ok=False);fork=c.RUN/'fork';shutil.copytree(ROOT/'fork',fork)
    for period in ('early','recent'):shutil.copy2(ROOT/(period+'-pool.json'),c.RUN/(period+'-pool.json'))
    p=c.read(ROOT/'protocol.json');old={t['variant']:t for t in p['templates']};templates=[]
    for name,source in MAPPING.items():
        t=copy.deepcopy(old[source]);t.update(name='P2_cycle_'+name,variant=name);templates.append(t)
    p.update(id=c.RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),templates=templates,online_windows=[],
        historical='Exploratory AFTER hourly-alignment finding: primary early +.0506R but cost/entry pressure negative and recent retention fails. No heldout claim.',
        parent_context_results_sha256=c.sha(ROOT/'context-supplement/results.json'),
        combinations=MAPPING,entry_quality='Pair fixed warning-time hourly alignment>0 with previously frozen fresh4/confirm/confirm_pre_body/kind_confirm/reclaim entries. Missing hourly feature rejects. No new thresholds, product/month rules or exits.',
        online='No online performance gating.',geometry='Unchanged original S2 stop/local target of each source template.',
        filter_implementation_sha256=c.sha(__file__),context_implementation_sha256=c.sha(c.REPO/'scripts/research_swing_context.py'),
        parent_raw_sha256={period:c.sha(ROOT/period/'evaluations.jsonl') for period in ('early','recent')})
    c.write(c.RUN/'protocol.json',p)
    c.write(c.RUN/'implementation-manifest.json',{str(q.relative_to(fork)):c.sha(q) for q in fork.rglob('*') if q.is_file()})
    c.write(c.RUN/'test-fixture-amendment-before-outcomes.json',dict(files={},reason='No engine or fixture changed.'))
    print(c.RUN,flush=True)

def run():
    p=c.read(c.RUN/'protocol.json');assert c.sha(__file__)==p['filter_implementation_sha256']
    assert c.sha(c.REPO/'scripts/research_swing_context.py')==p['context_implementation_sha256']
    for period in ('early','recent'):
        assert c.sha(ROOT/period/'evaluations.jsonl')==p['parent_raw_sha256'][period]
        events={x['event_key']:x['event'] for x in c.read(c.RUN/(period+'-pool.json'))};out=c.RUN/period;out.mkdir(exist_ok=False);count=0
        with (out/'evaluations.jsonl').open('x',encoding='utf-8') as file:
            for line in (ROOT/period/'evaluations.jsonl').open(encoding='utf-8'):
                r=json.loads(line);source,suffix=r['scenario'].split('/',1);source=source.removeprefix('P2_cycle_')
                for name,origin in MAPPING.items():
                    if origin!=source or name!='base' and not allows('h60_aligned',events[r['event_key']]):continue
                    z=dict(r,scenario='P2_cycle_'+name+'/'+suffix);file.write(json.dumps(z,ensure_ascii=False)+'\n');count+=1
        m=c.read(ROOT/period/'manifest.json');m.update(evaluations=count);c.write(out/'manifest.json',m)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('stage',choices=['prepare','run','report']);a=p.parse_args()
    (c.report if a.stage=='report' else globals()[a.stage])()
