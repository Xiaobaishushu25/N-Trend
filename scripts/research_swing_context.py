"""Small causal context hypotheses, frozen after entry-quality failures."""
import argparse,copy,datetime as dt,json,shutil
import research_swing_cycle as c
PARENT=c.RUN
c.RUN=PARENT/'context-supplement'
NAMES=['base','h60_aligned','d10_aligned','both_aligned','h60_counter','d10_trend_h60_pullback']

def allows(name,e):
    m=e.get('market_context') or {};h=m.get('trend_alignment_60');d=m.get('trend_alignment_10d')
    if name=='base':return True
    if name=='h60_aligned':return h is not None and h>0
    if name=='d10_aligned':return d is not None and d>0
    if name=='both_aligned':return h is not None and d is not None and h>0 and d>0
    if name=='h60_counter':return h is not None and h<=0
    if name=='d10_trend_h60_pullback':return h is not None and d is not None and h<=0 and d>0
    raise ValueError(name)

def prepare():
    c.RUN.mkdir(exist_ok=False);root=c.RUN/'fork';shutil.copytree(PARENT/'fork',root)
    for period in ('early','recent'):shutil.copy2(PARENT/(period+'-pool.json'),c.RUN/(period+'-pool.json'))
    p=c.read(PARENT/'protocol.json');templates=[]
    for name in NAMES:
        t=copy.deepcopy(p['templates'][0]);t['name']='P2_cycle_'+name;t['variant']=name;templates.append(t)
    p.update(id=c.RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),templates=templates,online_windows=[],
        parent_results_sha256=c.sha(PARENT/'results.json'),historical='Exploratory AFTER entry-quality results. All development data, no independent validation.',
        entry_quality='P2_local_wide execution reused exactly. Before trigger-time portfolio capacity, warning-time causal context filter:60m alignment>0;10day alignment>0;both>0;60m<=0;10day>0 and60m<=0. Missing required feature rejects. No threshold tuning, no warning type/product/month removal.',
        geometry='Identical to parent P2_local_wide. No exit/stop changes.',online='No online gate in context supplement.',
        context_source_sha256=c.sha(c.REPO/'crates/n-core/src/v2/features/market_context.rs'),
        parent_raw_sha256={period:c.sha(PARENT/period/'evaluations.jsonl') for period in ('early','recent')},
        filter_implementation_sha256=c.sha(__file__))
    c.write(c.RUN/'protocol.json',p)
    c.write(c.RUN/'implementation-manifest.json',{str(q.relative_to(root)):c.sha(q) for q in root.rglob('*') if q.is_file()})
    c.write(c.RUN/'test-fixture-amendment-before-outcomes.json',dict(files={},reason='Exact copied execution; no test amendment.'))
    print(c.RUN,flush=True)

def run():
    p=c.read(c.RUN/'protocol.json');audit={}
    assert c.sha(__file__)==p['filter_implementation_sha256']
    for period in ('early','recent'):
        assert c.sha(PARENT/period/'evaluations.jsonl')==p['parent_raw_sha256'][period]
        pool={x['event_key']:x['event'] for x in c.read(c.RUN/(period+'-pool.json'))};checked=0;missing60=missing10=0
        for e in pool.values():
            m=e.get('market_context') or {};w=e['warning_ts']
            if m:
                assert m['as_of_ts']==w
                for k in ('latest_15m_close_ts','latest_60m_close_ts'):
                    if m.get(k):assert m[k]<=w
                # Verify source implementation excludes current trading day, too.
                if m.get('latest_daily_trading_day'):assert m['latest_daily_trading_day']<=w[:10]
            missing60+=m.get('trend_alignment_60') is None;missing10+=m.get('trend_alignment_10d') is None;checked+=1
        out=c.RUN/period;out.mkdir(exist_ok=False);count=0;rejected={n:0 for n in NAMES}
        with (out/'evaluations.jsonl').open('x',encoding='utf-8') as file:
            for line in (PARENT/period/'evaluations.jsonl').open(encoding='utf-8'):
                r=json.loads(line)
                if r['scenario'].split('/')[0]!='P2_cycle_base':continue
                suffix=r['scenario'].split('/',1)[1]
                for name in NAMES:
                    if not allows(name,pool[r['event_key']]):rejected[name]+=1;continue
                    z=dict(r,scenario='P2_cycle_'+name+'/'+suffix);file.write(json.dumps(z,ensure_ascii=False)+'\n');count+=1
        manifest=c.read(PARENT/period/'manifest.json');manifest.update(evaluations=count)
        c.write(out/'manifest.json',manifest);audit[period]=dict(context_timestamp_checks=checked,missing60=missing60,missing10=missing10,excluded_rows=rejected)
    c.write(c.RUN/'context-audit.json',audit);print(json.dumps(audit,ensure_ascii=False),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('stage',choices=['prepare','run','report']);a=p.parse_args()
    (c.report if a.stage=='report' else globals()[a.stage])()
