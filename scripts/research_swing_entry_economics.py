"""Finite warning-only quality/economics filters on TWO frozen A families."""
import argparse,collections,copy,datetime as dt,json
import research_swing_wave_selection as w
from freeze_best_a_strategy import SNAP
from report_swing_wave_selection import identification_bars
from swing_entry_economics import warning_features,plan_features,allows,MODELS
RUN=w.REPO/'research-runs/swing-entry-economics-20261025-v1'
FAMILIES={'best_a':w.REPO/'research-runs/swing-retirement-20261025-v1/near_start','unmerged':w.REPO/'research-runs/swing-c-structure-20261025-v1/candidate'}

def prepare():
    assert all(w.sha(SNAP/k)==v for k,v in w.read(SNAP/'manifest.json').items());RUN.mkdir(exist_ok=False);p=w.read(SNAP/'protocol.json')
    p.update(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),families={k:str(v) for k,v in FAMILIES.items()},models=MODELS,script_sha256=w.sha(__file__),feature_sha256=w.sha(w.REPO/'scripts/swing_entry_economics.py'),
        experiment='Eight entry-quality/economics rules x two already frozen A families (16 new combinations plus2 controls). No new entry,stop,target,exit,size,warning-type/product/month thresholds or A-breakout. Existing price-action descriptors had only descriptive inspection; explicitly exploratory on repeatedly researched history.',
        models_definition='budget10:1tick-per-side frozen nominal roundtrip cost<=10% planned gross target space; body_net:net directional A bodies EXCLUDING S0 >0; no_gap_majority:aligned positive session-open gaps<=50%A amplitude; no_bar_majority:largest ABS body inclusiveS0<=50%A amplitude; orderly_B:wholeB adverse-extreme speed<=A speed; clean_A=body_net+no_gap_majority+no_bar_majority; budget_orderly=budget10+orderlyB; budget_clean=budget10+cleanA.',
        economics='Use warning-plan whitelist only: entry/stop/target/planned_risk/new_friction. Estimated cost=new_friction*plannedrisk, same frozen1tick planning cost for all4 hypothetical execution stresses. Do not inspect actual fills,status,MFE,realizedfee,R or outcomes in allows(). This budgets cost vs reward SPACE, not vs stop width.',
        structure='Same actual service15m aggregation (split on source gaps). Resolve duplicate labels using official A/B counts and all anchor prices. Signed body net excludesS0 because pivot candle may include prior leg. Aligned gap sum only across>15min source-bar gaps. Bspeed uses deepest CLOSED B path including warning, not just warning endpoint.',
        reuse='Reuse every frozen individual best-a/unmerged raw execution, including rejected/untriggered/unknown rows. A pure warning-time new veto cannot change surviving individual executions, so no redundant engine replay. Filter BEFORE trigger-time capacity and recompute whole merged chronology including freed slots; upstream service signal dedup remains fixed. Never filter only known losing trades.',
        objective='Primary combined known closed single-lot net, distribution secondary. Compare to named最佳a AND unmerged within family, all4 cost/latency stresses and both unknown occupancy policies. No80% recent-retention gate, no mandatory early positivity. No automatic live promotion or holdout claim.',
        production_changed=False,qualified_for_trading=False)
    p['sources']={family:{q:dict(pool=str(root/(q+'-pool.json')),pool_sha256=w.sha(root/(q+'-pool.json')),raw=str(root/q/'evaluations.jsonl'),raw_sha256=w.sha(root/q/'evaluations.jsonl'),manifest=str(root/q/'manifest.json'),manifest_sha256=w.sha(root/q/'manifest.json')) for q in ('early','recent')} for family,root in FAMILIES.items()}
    for q in ('early','recent'):assert all(w.sha(path)==p['hashes'][q][k] for k,path in p['periods'][q].items())
    features={};audit={};qa=collections.Counter()
    for q in ('early','recent'):
        inp=w.read(p['periods'][q]['input']);bars={sym:identification_bars(z['bars']) for sym,z in inp['symbols'].items()}
        for family,root in FAMILIES.items():
            pool=w.read(root/(q+'-pool.json'));cache={}
            for snap in pool:
                e=snap['event'];assert e['evaluation'] is None and e['outcome'] is None and e['entry_ts'] is None
                f=warning_features(e,bars[e['symbol']]);cache[snap['event_key']]=f;qa['causal_unlabelled_features']+=1;qa['duplicate_anchor_choices']+=f['anchor_matches']>1
            features[family+'/'+q]=cache;audit[family+'/'+q]=dict(pool=len(pool),body_net_positive=sum(f['a_body_net_ex_s0']>0 for f in cache.values()),gap_majority=sum(f['a_positive_session_gap_share']>.5 for f in cache.values()),bar_majority=sum(f['a_largest_abs_body_share']>.5 for f in cache.values()),B_faster=sum(f['b_true_speed_to_A']>1 for f in cache.values()))
    w.write(RUN/'protocol.json',p);w.write(RUN/'warning-features.json',features);w.write(RUN/'feature-audit-before-outcomes.json',dict(qa=dict(qa),counts=audit));print('Frozen16 quality/economics combinations and2 controls',audit,flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('stage',choices=['prepare']);p.parse_args();prepare()
