"""Fixed Sep-Dec2025 transfer test via authorized ordinary minute backtest."""
import argparse
import bisect
import collections
import copy
import datetime as dt
import hashlib
import http.server
import json
import math
import os
import subprocess
import sys
import threading
import time
import research_swing_jd_reclaim_execution_opt as x
import research_swing_older_four as old

b=x.b
R=x.R
RUN=b.ROOT/'research-runs/swing-jd-reclaim-other-months-20261027-v1'
START='2025-09-01 00:00:00'
END='2025-12-31 15:00:00'
WARMUP='2025-08-01 00:00:00'
TAIL='2026-01-16 15:00:00'
MONTHS=['2025-09','2025-10','2025-11','2025-12']
write=lambda n,z:old.write(RUN/n,z)
sha=x.sha
digest=old.digest

def requirements():
    source=R(old.RUN/'protocol.json');table=R(source['continuous_table_path'])['DCE.jd'];dates=[str(q[0]) for q in table];at=bisect.bisect_right(dates,'20250801')-1;keep=table[at:bisect.bisect_right(dates,'20260116')];ranges={}
    for i,(d,code) in enumerate(keep):
        first=max(WARMUP,(dt.datetime.strptime(str(d),'%Y%m%d')-dt.timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S'))
        last=min(TAIL,(dt.datetime.strptime(str(keep[i+1][0]),'%Y%m%d')+dt.timedelta(days=14)).strftime('%Y-%m-%d 23:59:59')) if i+1<len(keep) else TAIL
        ranges[code]=(first,last)
    return keep,ranges

def prepare():
    RUN.mkdir(exist_ok=False);source=R(old.RUN/'protocol.json');mapping,ranges=requirements();p0=R(x.RUN/'protocol.json');combo=R(x.RUN/'F2S8-followup/protocol.json')['template']
    exe=R(x.RUN/'binary.json');poolbin=old.TARGET/'release/swing_older_pool.exe';poolsha=R(old.RUN/'binaries.json')['swing_older_pool.exe'];assert sha(poolbin)==poolsha;assert sha(exe['path'])==exe['sha256']
    for q,h in R(x.r.SNAP/'manifest.json')['files'].items():assert sha(x.r.SNAP/q)==h
    write('protocol.json',dict(id=RUN.name,created_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
        score_start=START,score_end=END,warmup=WARMUP,execution_tail=TAIL,months=MONTHS,models=['E20_ONLY','BASE','F2S8'],
        templates=dict(E20S1=R(x.r.f.RUN/'protocol.json')['templates']['E20S1'],BASE=p0['templates']['BASE'],F2S8=combo),
        execution_binary=exe,generator_binary=dict(path=str(poolbin),sha256=poolsha),selector=4,
        purpose='ApplyalreadyfixedbidirectionalS0reclaimplanned2R andF2S8toothermonths. No newthresholds,sidefilters,optimizationorrescuesafterresults.',
        evidence='Sep-Dec2025 notusedtochoosethisreclaim/exitfamily; originalB2/B5resultsfor2025hadbeenviewed. Additionalmonthtransfercheck, notclaimedentirelyuntouchedhistory/statisticalproof.',
        sourcing='Read-onlyrepositoryandDBauditfound2025derivedresults butraw_prices_saved=false andnohistorical1mDB. FreshordinaryTqBacktest/TqSim viaexistingaccount, futures-only ordinaryroute, verifiedstaticmetadata cache; noorders/noexportAPI/noentitlementchanges. Rawminutepricesstayinmemory, feednativeengineviaalreadyexistingmemorytransport; savecoverage/hashes/deriveddecisions.',
        chronology='OnecontinuousSep-Decorder/positionledger; nomonthreset. Augustpricewarmup, Januaryactualexecutiontail, unknownnever0. RegenerateallABCeligibleopportunities, notoldacceptedwinners.',
        execution='Frozen20261022fees,1..4ticks/side,normalandextra1mentrydelay,2unknownpolicies. Notactualhistoricalbrokerfeebills. Planned2R andallinvalidationrulesunchanged.',
        mapping=mapping,contract_ranges=ranges,spec=source['specs']['JD0'],execution_config=source['execution_config'],
        mapping_path=source['continuous_table_path'],mapping_sha256=sha(source['continuous_table_path']),
        production_before=p0['production_before'],snapshot_manifest_sha256=sha(x.r.SNAP/'manifest.json'),parent_rules_sha256=sha(x.RUN/'protocol.json'),combo_rules_sha256=sha(x.RUN/'F2S8-followup/protocol.json'),script_sha256=sha(__file__)))
    # Existing official static metadata only, stripped ofallobservedprices.
    meta_path=old.BASE.parent.parent/'新N策略回测-20261024/多源复核/raw/tq_symbols.response'
    # Resolve fromsameverifiedsource receipt, notguessedfilesystemlayout.
    receipt=R(b.ROOT/'research-runs/swing-universe-may-aug2025-20261026-v1/metadata-cache-source.json');meta_path=receipt['source'];assert sha(meta_path)==receipt['source_sha256'];allmeta=R(meta_path)
    symbols=set(ranges)|{'KQ.m@DCE.jd','KQ.i@DCE.jd','CSI.000300'};assert all(s in allmeta for s in symbols)
    fields=['class','instrument_id','exchange_id','margin','commission','price_tick','price_decs','volume_multiple','max_limit_order_volume','max_market_order_volume','min_limit_order_volume','min_market_order_volume','underlying_symbol','strike_price','expired','trading_time','expire_datetime','delivery_month','delivery_year','option_class','product_id']
    subset={s:{k:v for k,v in allmeta[s].items() if k in fields} for s in sorted(symbols)};write('metadata-cache.json',subset);write('metadata-source.json',dict(source=meta_path,source_sha256=receipt['source_sha256'],cache_sha256=sha(RUN/'metadata-cache.json'),excluded='observedprices,settlement,open_interest,accountinformation'))
    # Thepriorpublishedholidaywindowsareidenticalreferenceguardinputs.
    shutil_path=RUN/'holidays.json';old.write(shutil_path,R(old.RUN/'holidays.json'))
    print('Frozen September-December2025,',len(ranges),'actualJDcontracts; no newrules.',flush=True)

def collect(max_minutes=8):
    import numpy as np
    from tqsdk import TqApi,TqAuth,TqBacktest,TqSim,BacktestFinished
    p=R(RUN/'protocol.json');ranges=p['contract_ranges'];metadata=(RUN/'metadata-cache.json').read_bytes();assert sha(RUN/'metadata-cache.json')==R(RUN/'metadata-source.json')['cache_sha256']
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path!='/symbols.json':self.send_error(404);return
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(metadata)));self.end_headers();self.wfile.write(metadata)
        def log_message(self,*args):pass
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start();os.environ['NO_PROXY']=os.environ.get('NO_PROXY','')+',127.0.0.1,localhost'
    cfg=R(b.ROOT/'data/config.json')['data_source'];secrets=R(b.ROOT/'data/secrets.json');api=None;rows={code:[] for code in ranges};seen={};lastids={};started=time.monotonic();last_print=started;finished=False;calls=0;zone=dt.timezone(dt.timedelta(hours=8));limit=int(dt.datetime.fromisoformat(WARMUP).replace(tzinfo=zone).timestamp())*10**9
    try:
        print('Initializingordinaryfutures-onlybacktestwithconfiguredaccount;credentialsnotprinted.',flush=True)
        api=TqApi(TqSim(),auth=TqAuth(cfg['tq_account'],cfg.get('tq_password') or secrets.get('tq_password')),backtest=TqBacktest(start_dt=dt.date(2025,8,1),end_dt=dt.date(2026,1,16)),debug=False,disable_print=True,web_gui=False,_stock=False,_ins_url=f'http://127.0.0.1:{server.server_port}/symbols.json')
        cfg=secrets=None;frames={code:api.get_kline_serial(code,60,data_length=128) for code,span in ranges.items() if span[0]<=WARMUP};dates={code:frame.datetime.values for code,frame in frames.items()};fields=['id','open','high','low','close','volume','close_oi'];buffers={code:{k:frame[k].values for k in fields} for code,frame in frames.items()}
        while time.monotonic()-started<max_minutes*60:
            api.wait_update(deadline=time.time()+3);calls+=1;positive=[int(v[-1]) for v in dates.values() if math.isfinite(float(v[-1])) and v[-1]>0]
            if not positive:continue
            clock=(dt.datetime.fromtimestamp(max(positive)//10**9,zone).replace(tzinfo=None)+dt.timedelta(minutes=1)).isoformat(sep=' ')
            for code,span in ranges.items():
                if code not in frames and span[0]<=clock:
                    frame=api.get_kline_serial(code,60,data_length=128);frames[code]=frame;dates[code]=frame.datetime.values;buffers[code]={k:frame[k].values for k in fields};print('Subscribe',code,'at',clock,flush=True)
            for code in frames:
                times=dates[code][:-1];buf=buffers[code];indices=np.flatnonzero((times>seen.get(code,0))&(times>=limit)&(buf['id'][:-1]>=0))
                for i in indices:
                    ns=int(times[i]);rid=int(buf['id'][i])
                    if code in lastids:assert rid==lastids[code]+1,('minute_serial_gap',code)
                    seen[code]=ns;lastids[code]=rid;label=(dt.datetime.fromtimestamp(ns//10**9,zone).replace(tzinfo=None)+dt.timedelta(minutes=1)).isoformat(sep=' ')
                    if not ranges[code][0]<=label<=ranges[code][1]:continue
                    v={k:float(buf[k][i]) for k in ['open','high','low','close','volume']};hold=float(buf['close_oi'][i])
                    if not all(math.isfinite(z) for z in list(v.values())+[hold]):continue
                    assert v['low']<=min(v['open'],v['close'])<=max(v['open'],v['close'])<=v['high'] and v['volume']>=0
                    rows[code].append(dict(datetime=label,**v,hold=hold))
            now=time.monotonic()
            if now-last_print>=15:print('SDKclock',clock,'minutes',sum(len(q) for q in rows.values()),'updates',calls,'elapsed',round(now-started),flush=True);last_print=now
        raise TimeoutError('ordinary_backtest_time_budget')
    except BacktestFinished:finished=True
    except Exception as e:
        write('access-failure.json',dict(status='failed',error_type=type(e).__name__,observed_minutes=sum(len(q) for q in rows.values()),elapsed_seconds=time.monotonic()-started,credentials_saved=False));raise RuntimeError('Fresh ordinary data read failed; sanitized receipt saved.') from None
    finally:
        cfg=secrets=None
        if api is not None:api.close()
        server.shutdown();server.server_close()
    assert finished and all(rows.values()),'incomplete_actual_contract_data'
    coverage={code:dict(minutes=len(q),first=q[0]['datetime'],last=q[-1]['datetime'],canonical_sha256=digest(q)) for code,q in rows.items()};prior=R(old.RUN/'actual-contract-coverage.json')['contracts']
    write('actual-contract-coverage.json',dict(contracts=coverage,finished=True,elapsed_seconds=time.monotonic()-started,api_updates=calls,raw_prices_saved=False,prior_coverage_hash_match={code:prior.get(code,{}).get('canonical_sha256')==v['canonical_sha256'] for code,v in coverage.items()}))
    return {code:dict(minutes=q) for code,q in rows.items()}

def stitch(contracts):
    p=R(RUN/'protocol.json');mapping=p['mapping'];keys=[str(v[0]) for v in mapping];good=[]
    for code,record in contracts.items():
        for q in record['minutes']:
            label=q['datetime'];day=label[:10].replace('-','');i=bisect.bisect_right(keys,day)-1
            if i>=0 and mapping[i][1]==code and WARMUP<=label<=TAIL:good.append((q,code))
    good.sort(key=lambda v:v[0]['datetime']);assert [q['datetime'] for q,_ in good]==sorted({q['datetime'] for q,_ in good});segments=[]
    for q,code in good:
        if not segments or segments[-1]['underlying']!=code:segments.append(dict(underlying=code,first=q['datetime'],last=q['datetime']))
        else:segments[-1]['last']=q['datetime']
    bars=old.aggregate([q for q,_ in good]);rolls=[dict(symbol='JD0',ts=z['first'],from_contract=a['underlying'],to_contract=z['underlying'],confirmed=True) for a,z in zip(segments,segments[1:])];inp=dict(execution_config=p['execution_config'],symbols={'JD0':dict(**p['spec'],bars=bars,rollovers=rolls)})
    previous=R(old.BASE/'input.json')['symbols']['JD0']['bars'];before={q['datetime']:q for q in previous};common=bad=0
    for q in bars:
        if q['datetime'] in before:
            common+=1;bad+=any(abs(q[k]-before[q['datetime']][k])>1e-7 for k in ['open','high','low','close','volume','hold'])
    assert common>0;assert bad==0,('overlap_price_mismatch',bad)
    write('segments.json',{'JD0':segments});write('trading-dates.json',sorted({q['datetime'][:10] for record in contracts.values() for q in record['minutes']}));write('data-audit.json',dict(first=bars[0]['datetime'],last=bars[-1]['datetime'],bars5=len(bars),canonical_input_sha256=digest(inp),scored_month_minutes=dict(collections.Counter(q['datetime'][:7] for q,_ in good if START<=q['datetime']<=END)),overlap_existing5m=common,mismatched_overlap5m=bad,raw_prices_saved=False))
    return inp,segments

def candidates(inp,segments,contracts):
    p=R(RUN/'protocol.json');paths=dict(input='memory:input',contracts='memory:contracts',segments=str(RUN/'segments.json'),fees=str(x.r.SNAP/'fees.json'),holidays=str(RUN/'holidays.json'));period='validation2025'
    gp=dict(selector=4,warning_start=START,warning_end=END,periods={period:paths},hashes={period:{'input':digest(inp)}});write('generation.protocol.json',gp);exe=p['generator_binary'];assert sha(exe['path'])==exe['sha256']
    subprocess.run([exe['path'],str(RUN/'generation.protocol.json'),period,str(RUN/'symbols'),str(RUN/'all-pool.json')],input=json.dumps(dict(input=inp),ensure_ascii=False,separators=(',',':')).encode(),check=True)
    seeds=R(RUN/'all-pool.json');bs=b.z.x.identification_bars(inp['symbols']['JD0']['bars']);indexes={q['datetime']:i for i,q in enumerate(bs)};counts=collections.Counter(q['datetime'] for q in bs);codes=[next((s['underlying'] for s in segments if s['first']<=q['datetime']<=s['last']),None) for q in bs];raw={code:{q['datetime']:q for q in record['minutes']} for code,record in contracts.items()};valid=[];actual={}
    for q,code in zip(bs,codes):
        stamp=dt.datetime.fromisoformat(q['datetime']);mins=[raw.get(code,{}).get((stamp-dt.timedelta(minutes=k)).isoformat(sep=' ')) for k in range(14,-1,-1)];ok=counts[q['datetime']]==1 and all(v is not None and v['volume']>0 for v in mins)
        if ok:
            candle=dict(q,open=mins[0]['open'],close=mins[-1]['close'],high=max(v['high'] for v in mins),low=min(v['low'] for v in mins),volume=sum(v['volume'] for v in mins),hold=mins[-1]['hold']);ok=all(abs(candle[k]-q[k])<1e-7 for k in ['open','high','low','close'])
            if ok:actual[q['datetime']]=candle
        valid.append(ok)
    meta={};proposals=[];ledger=[]
    for s in seeds:
        e=s['event'];assert e['setup_features']['setup_quality']>=2.5;s['entry_quality']=b.warning_features(e,bs);key=s['event_key'];cl='|'.join(e[k] for k in ['symbol','direction','s0_ts','s1_ts']);at=indexes[e['warning_ts']];s0=indexes[e['s0_ts']];s1=indexes[e['s1_ts']];code=codes[at];end=at
        meta[key]=dict(parent_key=key,cluster=cl,seed_ts=e['warning_ts'],warning_ts=e['warning_ts'],contract=code)
        for j in range(at+1,min(at+73,len(bs))):
            if codes[j]!=code or bs[j]['datetime']>END:break
            end=j
        reverse=-x.r.f.direction(e);reason=None
        if any(c!=code for c in codes[s0:at+1]):reason='ABC_crossed_contract'
        elif not valid[at]:reason='seed_unverified'
        elif any((q['close']-e['s0_price'])*reverse>=e['tick_size']-1e-8 for q in bs[s1+1:at+1]):reason='S0_already_failed_during_B'
        fi=None
        if reason is None:fi,reason=x.r.f.failure(bs,at,end,e,e['s0_price'],valid)
        ledger.append(dict(parent_key=key,seed_ts=e['warning_ts'],cluster=cl,reason=reason,failure_ts=None if fi is None else bs[fi]['datetime']))
        if fi is not None:
            assert x.r.f.failure(bs[:fi+1],at,fi,e,e['s0_price'],valid[:fi+1])==(fi,'failure_confirmed');proposals.append(dict(source=s,cluster=cl,fi=fi,end=end,code=code))
    seen=set();reclaims=[];activations=[]
    for a in sorted(proposals,key=lambda a:(bs[a['fi']]['datetime'],a['source']['event']['warning_ts'],a['source']['event_key'])):
        if a['cluster'] in seen:continue
        seen.add(a['cluster']);source=a['source'];e=source['event'];fi=a['fi'];j,reason=x.r.c.reclaim(bs,fi,a['end'],e['s0_price'],e,valid);activations.append(dict(parent_key=source['event_key'],cluster=a['cluster'],failure_ts=bs[fi]['datetime'],reason=reason,reclaim_ts=None if j is None else bs[j]['datetime']))
        if j is None:continue
        assert x.r.c.reclaim(bs[:j+1],fi,j,e['s0_price'],e,valid[:j+1])==(j,'level_reclaimed');snap=copy.deepcopy(source);key='ABCRECLAIM|S0|'+source['event_key']+'|'+bs[j]['datetime'];snap['event'].update(x.r.c.geometry(bs,fi,j,e,'R2'),warning_ts=bs[j]['datetime'],market_context=None,warning_kind='abc_failure_reclaim');snap.update(event_key=key,warning_candle=actual[bs[j]['datetime']],prior=[]);reclaims.append(snap)
        meta[key]=dict(parent_key=source['event_key'],cluster=a['cluster'],seed_ts=e['warning_ts'],warning_ts=bs[j]['datetime'],failure_ts=bs[fi]['datetime'],contract=a['code'])
    # ComparepreexistingderivedJDwarnings, notoldacceptedportfolioselection.
    previous=[s for s in R(old.RUN/'unmerged/older-pool.json') if s['event']['symbol']=='JD0'];before={s['event_key']:s for s in previous};current={s['event_key']:s for s in seeds};common=before.keys()&current.keys();bad=sum(any(before[k]['event'][z]!=current[k]['event'][z] for z in ['entry','stop','target','s0_price','s1_price','s2_price']) for k in common)
    assert before.keys()==current.keys();assert bad==0
    write('seeds-with-features.json',seeds);write('failure-ledger.json',ledger);write('first-activations.json',activations);write('metadata.json',meta);write('generation-checks.json',dict(qualified_seeds=len(seeds),reclaim_decisions=len(reclaims),seed_months=dict(collections.Counter(s['event']['warning_ts'][:7] for s in seeds)),reclaim_months=dict(collections.Counter(s['event']['warning_ts'][:7] for s in reclaims)),exact_prior_JD_seed_keys=len(common),geometry_mismatches=bad,prefix_checks=True))
    for task,pool,templates in [('E20',seeds,[p['templates']['E20S1']]),('reclaim',reclaims,[p['templates']['BASE'],p['templates']['F2S8']])]:
        dest=RUN/task;dest.mkdir();old.write(dest/(period+'-pool.json'),pool);old.write(dest/'execution.protocol.json',dict(templates=templates,modes=['corrected_touch','next_minute_open'],periods={period:paths}));exe=p['execution_binary'];assert sha(exe['path'])==exe['sha256'];subprocess.run([exe['path'],str(dest/'execution.protocol.json'),period,'replay',str(dest/'executions')],input=json.dumps(dict(input=inp,contracts=contracts),ensure_ascii=False,separators=(',',':')).encode(),check=True)
    print('Generated',len(seeds),'qualifiedABCseeds and',len(reclaims),'S0reclaims; no pricefilesexported.',flush=True)

def analyze():
    p=R(RUN/'protocol.json');ctl,_,_,_,_=x.r.load();meta=R(RUN/'metadata.json');data=collections.defaultdict(list);fees=R(x.r.SNAP/'fees.json');cal=b.z.s.TradingDates({});cal.dates=R(RUN/'trading-dates.json');ends={}
    for task in ['E20','reclaim']:
        ends.update(R(RUN/task/'executions/manifest.json')['contract_ends'])
        for line in (RUN/task/'executions/evaluations.jsonl').open(encoding='utf-8'):
            row=b.z.s.reprice(json.loads(line),fees,cal);m,suffix=row['scenario'].split('/',1);data[m,suffix].append(row)
    cases=[policy+'/'+mode+'/slip'+str(s) for policy in ['original','two_paths_flat'] for mode in ['corrected_touch','next_minute_open'] for s in [1,2,3,4]];result={};monthly={};accepted={}
    for case in cases:
        policy,suffix=case.split('/',1)
        for model in ['E20_ONLY','BASE','F2S8']:
            stream=[];mm=dict(meta)
            for prefix,m in [('0','E20S1')]+([] if model=='E20_ONLY' else [('1','BASE' if model=='BASE' else 'REFAIL2')]):
                for src in data[m,suffix]:
                    row=copy.deepcopy(src);key=prefix+'|'+row['event_key'];mm[key]=meta[row['event_key']];row.update(original_event_key=row['event_key'],event_key=key,module='E20S1' if m=='E20S1' else 'S0_RECLAIM_R2');stream.append(row)
            aa,_=b.admit(stream,ends,policy,ctl,mm);accepted[case,model]=aa;result[case+'/'+model]=b.z.metric(aa,MONTHS)
            for month in MONTHS:monthly[case+'/'+model+'/'+month]=b.z.metric([q for q in aa if q['warning_ts'].startswith(month)],[month])
    write('results.json',result);write('monthly-results.json',monthly)
    with (RUN/'accepted-primary.jsonl').open('x',encoding='utf-8') as out:
        for model in ['E20_ONLY','BASE','F2S8']:
            for row in accepted['original/corrected_touch/slip1',model]:out.write(json.dumps(dict(model=model,row=row),ensure_ascii=False)+'\n')
    print(json.dumps({m:{k:result['original/corrected_touch/slip1/'+m][k] for k in ['n','net','normalized_net','unknown']} for m in ['E20_ONLY','BASE','F2S8']}),flush=True)

def pipeline():
    p=R(RUN/'protocol.json');assert sha(__file__)==p['script_sha256'];contracts=collect();inp,segments=stitch(contracts);candidates(inp,segments,contracts);del contracts,inp;analyze()

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('stage',choices=['prepare','pipeline','analyze']);args=ap.parse_args();globals()[args.stage]()
