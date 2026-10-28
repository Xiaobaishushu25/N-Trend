"""Extend four fixed universes into May-Aug2025 using frozen native engines."""
import argparse
import bisect
import collections
import copy
import datetime as dt
import json
import math
import pathlib
import os
import subprocess
import sys
import time
import http.server
import threading

sys.dont_write_bytecode=True
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
import research_swing_older_four as old
from research_swing_universe_mix import UNIVERSES,LABELS

ROOT=old.REPO
RUN=ROOT/'research-runs/swing-universe-may-aug2025-20261026-v1'
SYMS=UNIVERSES['six_original']
START,END='2025-05-01 00:00:00','2025-08-31 23:59:59'
WARMUP,TAIL='2025-04-01 00:00:00','2025-09-16 15:00:00'
MD_URL='wss://backtest.shinnytech.com/t/md/front/mobile'
TRANSPORT=dict(futures_only=False,direct_provider=False,futures_route=False)
METADATA_SOURCE=pathlib.Path('G:/待处理文件/交易系统/探索/新N策略回测-20261024/多源复核/raw/tq_symbols.response')
METADATA_SHA='311a186907c59f9881939836fe6600a587f77b93cfe02b8107193a1f4f1848f1'


def write(name,z):old.write(RUN/name,z)


def prepare():
    RUN.mkdir(exist_ok=False)
    source=old.read(old.RUN/'protocol.json')
    hashes=old.read(old.RUN/'binaries.json')
    assert all(old.sha(old.TARGET/'release'/n)==h for n,h in hashes.items())
    p=dict(created_at=dt.datetime.now().astimezone().isoformat(),warning_start=START,warning_end=END,
        warmup=WARMUP,execution_tail=TAIL,universes=UNIVERSES,symbols=SYMS,versions=['B2','B5'],
        modes=['corrected_touch','next_minute_open'],slips=[1,2],policies=['original','two_paths_flat'],
        primary='B5/original/corrected_touch/slip1',selector=4,
        definitions='Unchanged B2 budget10/base controller and B5 budget_body/sector2_counter_stops. No JD20 relaxation or extra pair cap. No parameter or universe search.',
        data='Ordinary authenticated TqBacktest/TqSim, actual contract minute subscriptions, historical main mapping; raw prices only in memory, derived warnings/executions/coverage/hashes saved.',
        transport='Try account default discovery first or documented TqSim url override to official backtest server. Same account and permission checks; no entitlement bypass or fabricated data-ready flag.',
        fees='Frozen20261022 rates, not historical daily broker bills; planning costs frozen1tick across all stresses. One-lot known closed net, not account ROI; realized drawdown excludes open MTM/margin.',
        chronology='Acquire reservations at trigger, release only strictly after exit timestamp; no month resets. Unknown PnL not zero; original archive-end occupancy plus two-paths-flat sensitivity.',
        evidence='New earlier-window evaluation of lists selected after inspecting later history; not an untouched or independent strategy holdout claim.',
        specs={s:source['specs'][s] for s in SYMS},execution_config=source['execution_config'],
        templates=[t for t in old.read(old.RUN/'unmerged/protocol.json')['templates'] if t['name'] in ['P2_economics_budget10','P2_economics_budget_body']],
        fees_path=source['fees_path'],fees_sha256=old.sha(source['fees_path']),
        continuous_table_path=str(old.TABLE),continuous_table_sha256=old.sha(old.TABLE),
        engine_hashes=hashes,engine_source_manifest_sha256=old.sha(old.RUN/'implementation-manifest.json'),
        frozen_references={str(q):old.sha(q/'manifest.json') for q in [old.SNAP,ROOT/'research-strategies/best-a-portfolio-observation-v1']},
        previous_mix_results_sha256=old.sha(ROOT/'research-runs/swing-universe-mix-20261026-v1/results.json'),
        production_before={str(q):old.sha(q) for q in ['crates/n-core/src/trader/candidates.rs','src/stores/liveTrader.ts','src/types.ts','src/views/LiveTraderView.vue']},
        production_changed=False)
    write('protocol.json',p)
    windows=[dict(name=name,block_from=last+' 14:30:00',flat_at=last+' 14:45:00',last_close=last+' 15:00:00',reopen=reopen+' 09:00:00')
             for name,last,reopen in [('清明','2025-04-03','2025-04-07'),('劳动节','2025-04-30','2025-05-06'),('端午','2025-05-30','2025-06-03')]]
    write('holidays.json',dict(windows=windows,source='https://www.shfe.com.cn/publicnotice/notice/202412/t20241223_824109.html'))
    print('Frozen four universes, B2/B5 and May-Aug2025 window.',flush=True)


def api_kwargs(explicit):
    from tqsdk import TqSim,TqAuth,TqBacktest
    cfg=old.read(ROOT/'data/config.json')['data_source'];secrets=old.read(ROOT/'data/secrets.json')
    selected_url='wss://free-openmd.shinnytech.com/t/md/front/mobile' if TRANSPORT['futures_route'] else MD_URL if explicit else None
    return dict(account=TqSim(),auth=TqAuth(cfg['tq_account'],cfg.get('tq_password') or secrets.get('tq_password')),
        backtest=TqBacktest(start_dt=dt.date(2025,4,1),end_dt=dt.date(2025,9,16)),
        debug=False,disable_print=True,web_gui=False,**({'url':selected_url} if selected_url else {}),
        **({'_stock':False} if TRANSPORT['futures_only'] else {}),
        **({'_ins_url':TRANSPORT['metadata_url']} if TRANSPORT.get('metadata_url') else {}))


def prepare_metadata():
    """Subset an existing official metadata snapshot; never include live prices."""
    assert old.sha(METADATA_SOURCE)==METADATA_SHA
    source=old.read(METADATA_SOURCE);p=old.read(RUN/'protocol.json');_,ranges=requirements(p)
    selected=set(ranges)
    for s in SYMS:
        prod=s.rstrip('0123456789');exchange=old.EXCH[prod];var=exchange+'.'+(prod if exchange=='CZCE' else prod.lower())
        selected.update(['KQ.m@'+var,'KQ.i@'+var])
    selected.add('CSI.000300')
    assert all(s in source for s in selected),sorted(selected-source.keys())
    fields=['class','instrument_id','exchange_id','margin','commission','price_tick','price_decs','volume_multiple',
            'max_limit_order_volume','max_market_order_volume','min_limit_order_volume','min_market_order_volume',
            'underlying_symbol','strike_price','expired','trading_time','expire_datetime','delivery_month','delivery_year','option_class','product_id']
    subset={s:{k:v for k,v in source[s].items() if k in fields} for s in sorted(selected)}
    for s in ranges:
        assert subset[s]['class']=='FUTURE' and subset[s]['price_tick']>0 and subset[s]['volume_multiple']>0
    write('metadata-cache.json',subset)
    write('metadata-cache-source.json',dict(source=str(METADATA_SOURCE),source_sha256=METADATA_SHA,
        exact_source_subset=True,contracts=len(ranges),metadata_records=len(subset),prices_saved=False,
        excluded='last_price,settlement,open_interest and all observed market price/outcome fields',
        cache_sha256=old.sha(RUN/'metadata-cache.json')))
    print('Prepared official static metadata cache:',len(subset),'records; no observed price fields.',flush=True)


def start_metadata_server():
    """Serve only the verified static metadata payload on loopback for this run."""
    p=RUN/'metadata-cache.json';receipt=old.read(RUN/'metadata-cache-source.json')
    assert old.sha(p)==receipt['cache_sha256'];body=p.read_bytes()
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path!='/symbols.json':self.send_error(404);return
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    TRANSPORT.update(futures_only=True,metadata_url=f'http://127.0.0.1:{server.server_port}/symbols.json',metadata_cache_sha256=receipt['cache_sha256'])
    os.environ['NO_PROXY']=os.environ.get('NO_PROXY','')+',127.0.0.1,localhost'
    return server,thread


def probe(explicit,direct=False):
    from tqsdk import TqApi,BacktestFinished
    import tqsdk
    result=dict(started_at=dt.datetime.now().astimezone().isoformat(),explicit_official_backtest_url=explicit,direct_provider=direct,
        futures_only=TRANSPORT['futures_only'],futures_route=TRANSPORT['futures_route'],sdk_version=tqsdk.__version__,
        protocol='Normal SDK auth; no orders, no entitlement changes, no raw prices saved',status='unconfirmed')
    api=None
    try:
        api=TqApi(**api_kwargs(explicit))
        print('SDK initialized normally',flush=True)
        p=old.read(RUN/'protocol.json');mapping,_=requirements(p)
        code=mapping['EG0'][0][1]
        series=api.get_kline_serial(code,60,data_length=128)
        deadline=time.monotonic()+90
        first=int(dt.datetime(2025,4,1,tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp())*10**9
        while time.monotonic()<deadline:
            api.wait_update(deadline=time.time()+2)
            times=[int(v) for v in series.datetime[:-1] if int(v)>=first]
            if len(times)>=3:
                result.update(status='older_minute_access_observed',symbol=code,timestamps=len(times),first_ns=min(times),last_ns=max(times));break
    except BacktestFinished:result['status']='finished_before_sample'
    except Exception as e:result.update(status='connection_or_permission_error',error_type=type(e).__name__)
    finally:
        if api is not None:api.close()
    result['finished_at']=dt.datetime.now().astimezone().isoformat()
    write('access-retry-'+dt.datetime.now().strftime('%Y%m%d-%H%M%S')+'.json',result)
    print(json.dumps(result),flush=True)


def requirements(p):
    table=old.read(old.TABLE);selected={};ranges={}
    for sym in SYMS:
        product=sym.rstrip('0123456789');ex=old.EXCH[product]
        var=ex+'.'+(product if ex=='CZCE' else product.lower())
        entries=table[var];dates=[str(z[0]) for z in entries]
        a=bisect.bisect_right(dates,WARMUP[:10].replace('-',''))-1
        assert a>=0
        keep=entries[a:bisect.bisect_right(dates,TAIL[:10].replace('-',''))]
        selected[sym]=keep
        for i,(date,code) in enumerate(keep):
            first=max(WARMUP,(dt.datetime.strptime(str(date),'%Y%m%d')-dt.timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S'))
            last=min(TAIL,(dt.datetime.strptime(str(keep[i+1][0]),'%Y%m%d')+dt.timedelta(days=14)).strftime('%Y-%m-%d 23:59:59')) if i+1<len(keep) else TAIL
            if code in ranges:first=min(first,ranges[code][0]);last=max(last,ranges[code][1])
            ranges[code]=first,last
    return selected,ranges


def collect(p,max_minutes,explicit):
    import numpy as np
    from tqsdk import TqApi,BacktestFinished
    mapping,ranges=requirements(p);rows={c:[] for c in ranges};last_ids={};seen={};calls=0
    api=None;done=False;started=time.monotonic();last_print=started
    zone=dt.timezone(dt.timedelta(hours=8));first_ns=int(dt.datetime.fromisoformat(WARMUP).replace(tzinfo=zone).timestamp())*10**9
    print('Actual contracts',len(ranges),'products',len(SYMS),flush=True)
    try:
        api=TqApi(**api_kwargs(explicit))
        frames={c:api.get_kline_serial(c,60,data_length=256) for c,span in ranges.items() if span[0]<=WARMUP}
        dates={c:f.datetime.values for c,f in frames.items()};fields=['id','open','high','low','close','volume','close_oi']
        buffers={c:{k:f[k].values for k in fields} for c,f in frames.items()}
        print('Initialized, active subscriptions',len(frames),flush=True)
        while time.monotonic()-started<max_minutes*60:
            api.wait_update(deadline=time.time()+5);calls+=1
            valid=[float(v[-1]) for v in dates.values() if math.isfinite(float(v[-1])) and v[-1]>0]
            if not valid:continue
            clock=(dt.datetime.fromtimestamp(int(max(valid))//10**9,zone).replace(tzinfo=None)+dt.timedelta(minutes=1)).isoformat(sep=' ')
            for c,span in ranges.items():
                if c not in frames and span[0]<=clock:
                    f=api.get_kline_serial(c,60,data_length=256);frames[c]=f;dates[c]=f.datetime.values
                    buffers[c]={k:f[k].values for k in fields}
                    print('Added',c,'at',clock,flush=True)
            for c in frames:
                times=dates[c][:-1];buf=buffers[c]
                indices=np.flatnonzero((times>seen.get(c,0)) & (times>=first_ns) & (buf['id'][:-1]>=0))
                for i in indices:
                    ns=int(times[i]);rid=int(buf['id'][i])
                    if c in last_ids:assert rid==last_ids[c]+1,('Skipped SDK buffer',c,last_ids[c],rid)
                    seen[c]=ns;last_ids[c]=rid
                    ts=(dt.datetime.fromtimestamp(ns//10**9,zone).replace(tzinfo=None)+dt.timedelta(minutes=1)).isoformat(sep=' ')
                    if not ranges[c][0]<=ts<=ranges[c][1]:continue
                    vals={k:float(buf[k][i]) for k in ['open','high','low','close','volume']}
                    assert all(math.isfinite(x) for x in vals.values())
                    assert vals['low']<=min(vals['open'],vals['close'])<=max(vals['open'],vals['close'])<=vals['high'] and vals['volume']>=0
                    oi=float(buf['close_oi'][i]);assert math.isfinite(oi)
                    rows[c].append(dict(datetime=ts,**vals,hold=oi))
            if time.monotonic()-last_print>=15:
                print('SDK',clock,'stored',sum(map(len,rows.values())),'elapsed_sec',round(time.monotonic()-started),flush=True);last_print=time.monotonic()
        raise TimeoutError('Full history stream not finished')
    except BacktestFinished:done=True
    finally:
        if api is not None:api.close()
    assert done and all(rows.values())
    write('actual-contract-coverage.json',dict(complete_backtest=True,explicit_url=explicit,transport=dict(TRANSPORT),elapsed_seconds=time.monotonic()-started,api_updates=calls,
        contracts={c:dict(minutes=len(bs),first=bs[0]['datetime'],last=bs[-1]['datetime'],canonical_sha256=old.digest(bs)) for c,bs in rows.items()},raw_prices_saved=False))
    return mapping,{c:dict(minutes=bs) for c,bs in rows.items()}


def stitch(p,mapping,contracts):
    dates=sorted({b['datetime'][:10] for c in contracts.values() for b in c['minutes'] if '09:00:00'<=b['datetime'][11:]<='15:00:00'})
    def day(ts):
        i=bisect.bisect_right(dates,ts[:10]) if ts[11:]>='20:00:00' else bisect.bisect_left(dates,ts[:10])
        return dates[i].replace('-','') if i<len(dates) else None
    inp=dict(execution_config=p['execution_config'],symbols={});maps={};audit={}
    for sym,entries in mapping.items():
        ds=[str(z[0]) for z in entries];good=[]
        for c in {z[1] for z in entries}:
            for b in contracts[c]['minutes']:
                d=day(b['datetime'])
                if not d or not WARMUP<=b['datetime']<=TAIL:continue
                i=bisect.bisect_right(ds,d)-1
                if i>=0 and entries[i][1]==c:good.append((b,c))
        good.sort(key=lambda z:z[0]['datetime']);times=[b['datetime'] for b,c in good]
        assert times==sorted(set(times)) and times[0][:10]=='2025-04-01' and times[-1][:10]>='2025-09-15',(sym,times[:1],times[-1:])
        groups=[]
        for b,c in good:
            if not groups or groups[-1]['underlying']!=c:groups.append(dict(underlying=c,first=b['datetime'],last=b['datetime'],n=1))
            else:groups[-1]['last']=b['datetime'];groups[-1]['n']+=1
        rolls=[dict(symbol=sym,ts=b['first'],from_contract=a['underlying'],to_contract=b['underlying'],confirmed=True) for a,b in zip(groups,groups[1:])]
        bars=old.aggregate([b for b,c in good]);inp['symbols'][sym]=dict(**p['specs'][sym],bars=bars,rollovers=rolls);maps[sym]=groups
        month_bins=collections.Counter(b['datetime'][:7] for b,c in good if START<=b['datetime']<=END)
        assert all(month_bins[f'2025-{i:02}']>0 for i in range(5,9))
        audit[sym]=dict(first=times[0],last=times[-1],minutes=len(good),bars5=len(bars),rollovers=len(rolls),scored_month_minutes=dict(month_bins),canonical_5m_sha256=old.digest(bars))
    write('data-audit.json',audit);write('segments.json',maps);write('trading-dates.json',dates)
    return inp,maps


def run(max_minutes,explicit):
    from report_swing_wave_selection import identification_bars
    from swing_entry_economics import warning_features
    p=old.read(RUN/'protocol.json');mapping,contracts=collect(p,max_minutes,explicit);inp,maps=stitch(p,mapping,contracts)
    # Verify the new transport on a price-only overlap with the previous acquisition.
    overlap=[];mismatches=[]
    overlap_bars={s:identification_bars(v['bars']) for s,v in inp['symbols'].items()}
    index={s:collections.defaultdict(list) for s in SYMS}
    for s,bs in overlap_bars.items():
        for b in bs:index[s][b['datetime']].append(b)
    for snap in old.read(old.RUN/'unmerged/older-pool.json'):
        e=snap['event'];s=e['symbol'];ts=e['warning_ts']
        if s not in SYMS or not '2025-09-01'<=ts[:10]<='2025-09-15':continue
        oldbar=snap['warning_candle'];found=index[s].get(ts,[])
        same=any(all(abs(b[k]-oldbar[k])<1e-7 for k in ['open','high','low','close']) for b in found)
        overlap.append(dict(symbol=s,ts=ts,exact_OHLC=same))
        if not same:mismatches.append(dict(symbol=s,ts=ts))
    write('prior-acquisition-overlap.json',dict(compared=len(overlap),mismatches=mismatches,candles=overlap,
        note='Sep1-15 warning-candle OHLC from original archived SDK acquisition versus new actual-contract stream; these overlap prices are outside new May-Aug scoring.'))
    assert len(overlap)>20 and not mismatches,('New transport overlap mismatch',mismatches[:5])
    execution=dict(selector=4,warning_start=START,warning_end=END,templates=p['templates'],modes=p['modes'],
        hashes={'older':{'input':old.digest(inp)}},periods={'older':dict(input='memory:input',contracts='memory:contracts',fees=p['fees_path'],segments=str(RUN/'segments.json'),holidays=str(RUN/'holidays.json'))})
    write('execution.protocol.json',execution)
    for n,h in p['engine_hashes'].items():assert old.sha(old.TARGET/'release'/n)==h
    old.engine('swing_older_pool.exe',RUN/'execution.protocol.json','pool',RUN/'older-pool.json',dict(input=inp))
    pool=old.read(RUN/'older-pool.json');bars={s:identification_bars(z['bars']) for s,z in inp['symbols'].items()}
    for z in pool:
        assert z['event']['evaluation'] is None and z['event']['outcome'] is None
        z['entry_quality']=warning_features(z['event'],bars[z['event']['symbol']])
    (RUN/'older-pool.json').rename(RUN/'older-pool-unlabelled.json');write('older-pool.json',pool)
    old.engine('swing_older_execution.exe',RUN/'execution.protocol.json','replay',RUN/'older',dict(input=inp,contracts=contracts))
    print('All earlier individual executions finished; raw prices discarded.',flush=True)


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('stage',choices=['prepare','prepare_metadata','probe','run']);ap.add_argument('--explicit-url',action='store_true');ap.add_argument('--direct-provider',action='store_true');ap.add_argument('--futures-only',action='store_true');ap.add_argument('--futures-route',action='store_true');ap.add_argument('--cached-metadata',action='store_true');ap.add_argument('--max-minutes',type=int,default=60);a=ap.parse_args()
    TRANSPORT.update(futures_only=a.futures_only,direct_provider=a.direct_provider,futures_route=a.futures_route)
    if a.direct_provider:
        os.environ['NO_PROXY']=os.environ.get('NO_PROXY','')+',.shinnytech.com'
        print('Provider-only direct routing in this process; global proxy unchanged.',flush=True)
    server=thread=None
    try:
        if a.cached_metadata:server,thread=start_metadata_server()
        if a.stage=='prepare':prepare()
        elif a.stage=='prepare_metadata':prepare_metadata()
        elif a.stage=='probe':probe(a.explicit_url,a.direct_provider)
        else:run(a.max_minutes,a.explicit_url)
    finally:
        if server is not None:server.shutdown();server.server_close();thread.join(timeout=3)
