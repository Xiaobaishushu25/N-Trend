"""Replay the self-contained frozen portfolio policy against pinned research inputs."""
import argparse,collections,importlib.util,json,pathlib
import research_swing_abstop as s
from research_swing_consistency import read,write,sha,REPO
from report_swing_c_structure import distribution,equal,SUFFIXES

def run(snapshot,output):
    snapshot=pathlib.Path(snapshot);output=pathlib.Path(output);assert not output.exists()
    assert all(sha(snapshot/k)==v for k,v in read(snapshot/'manifest.json').items())
    p=read(snapshot/'protocol.json');ref=read(p['reference_results']);assert sha(p['reference_results'])==p['reference_results_sha256']
    assert all(sha(REPO/k)==v for k,v in p['replay_dependency_hashes'].items())
    spec=importlib.util.spec_from_file_location('frozen_portfolio_policy',snapshot/'controller.py');policy=importlib.util.module_from_spec(spec);spec.loader.exec_module(policy)
    fees=read(p['periods']['early']['fees']);events={};ends={};raw=collections.defaultdict(list)
    for q in ['early','recent']:
        paths=p['sources'][q]
        for k in ['raw','pool','manifest']:assert sha(paths[k])==paths[k+'_sha256']
        events.update({z['event_key']:z['event'] for z in read(paths['pool'])});cal=s.TradingDates(read(p['periods'][q]['input'])['symbols'])
        for c,end in read(paths['manifest'])['contract_ends'].items():ends[c]=max(end,ends.get(c,''))
        for line in open(paths['raw'],encoding='utf-8'):
            z=json.loads(line)
            if not z['scenario'].startswith('P2_economics_budget_body/'):continue
            sf=z['scenario'].split('/',1)[1];z=s.reprice(z,fees,cal);z['period']=q
            if s.ev(z)['status']=='closed':z['exit_trading_day']=cal.day(s.ev(z)['exit_ts'])
            raw[sf].append(z)
    checks=[];sets={}
    for pol in ['original','two_paths_flat']:
        for sf in SUFFIXES:
            for model in ['base','sector2_counter','sector2_counter_stops']:
                rows=raw[sf];aa,_,risk=policy.select(rows,events,ends,p['sector_map'],model,pol);rev,_,_=policy.select(rows,events,ends,p['sector_map'],model,pol,True);key='budget_body/'+model+'/'+sf;sets[pol+'/'+key]=[z['event_key'] for z in aa]
                for q in ['early','recent','recent_aug_oct','combined']:
                    keep=lambda z:True if q=='combined' else z['period']=='recent' and z['warning_ts']>='2026-08-01 00:00:00' if q=='recent_aug_oct' else z['period']==q
                    m=distribution([z for z in aa if keep(z)],events);m['reverse_tie_net']=s.metrics([z for z in rev if keep(z)])['net']
                    if q=='combined':m.update({k:v for k,v in risk.items() if k!='realized_settlements'})
                    bad={k:[v,ref[pol][q][key].get(k)] for k,v in m.items() if not equal(v,ref[pol][q][key].get(k))};assert not bad,(pol,sf,model,q,bad)
                    checks.append(dict(policy=pol,suffix=sf,model=model,period=q,all_metrics_exact=True))
    expected=collections.defaultdict(list)
    for line in pathlib.Path(p['reference_accepted']).open(encoding='utf-8'):
        z=json.loads(line)
        if z['case'].split('/')[0]=='budget_body' and z['case'].split('/')[1] in ['base','sector2_counter','sector2_counter_stops']:expected[z['policy']+'/'+z['case']].append(z['row']['event_key'])
    assert dict(expected)==sets;assert all(sha(k)==v for k,v in p['production_before'].items())
    write(output,dict(all_exact=True,metrics=checks,accepted_sets_exact=len(sets),accepted_reservations=sum(map(len,sets.values())),snapshot_manifest_sha256=sha(snapshot/'manifest.json'),standalone_controller=True,production_unchanged=True,independent_validation=False))
    print('Frozen policy reproduced96 metric records and24 exact accepted sequences.',flush=True)

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('snapshot');ap.add_argument('output');args=ap.parse_args();run(args.snapshot,args.output)
