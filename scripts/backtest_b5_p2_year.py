"""User-requested trailing-year backtest, with resumable local data caches.

Uses frozen native engines; never writes production config, accounts or databases.
"""
import bisect
import collections
import copy
import csv
import datetime as dt
import gc
import gzip
import hashlib
import http.server
import importlib.util
import inspect
import json
import math
import os
import pathlib
import subprocess
import sys
import threading
import time

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import research_swing_older_four as old
from reprice_signal_costs import TradingDates, reprice
from entry_model import completed_context, commission_bound

REPO = old.REPO
RUN = REPO / 'research-runs/b5-p2-trailing-year-20261027-v9'
BASE = pathlib.Path('G:/待处理文件/交易系统/探索/P2P3分散优化-20261025')
R = old.read
START, END = '2025-10-27', '2026-10-26'
WARM, CUTOFF = '2025-09-22 00:00:00', END + ' 15:00:00'
# Friday night belongs to Monday Oct 27 trading day.
WARNING_START = '2025-10-24 20:00:00'
SIX = ['EG0','PP0','V0','JD0','SR0','CJ0']
NAMES = dict(BU0='沥青',C0='玉米',CF0='棉花',CJ0='红枣',EB0='苯乙烯',EG0='乙二醇',FG0='玻璃',JD0='鸡蛋',L0='塑料',MA0='甲醇',PB0='沪铅',PP0='聚丙烯',RB0='螺纹钢',SA0='纯碱',SF0='硅铁',SH0='烧碱',SI0='工业硅',SM0='锰硅',SR0='白糖',SS0='不锈钢',UR0='尿素',V0='PVC')

def write(name,z):
    path=RUN/name
    if path.exists():
        assert R(path)==z,('Existing immutable output differs',name)
        return
    old.write(RUN/name,z)

def product(code):
    return code.split('.')[-1].rstrip('0123456789').upper()+'0'

def requirements(table,syms,start,end):
    selected={}; ranges={}
    for sym in syms:
        p=sym.rstrip('0123456789'); ex=old.EXCH[p]
        var=ex+'.'+(p if ex=='CZCE' else p.lower())
        entries=table[var]; dates=[str(x[0]) for x in entries]
        at=bisect.bisect_right(dates,start[:10].replace('-',''))-1
        assert at>=0
        keep=entries[at:bisect.bisect_right(dates,end[:10].replace('-',''))]
        selected[sym]=keep
        for i,(day,code) in enumerate(keep):
            a=max(start,(dt.datetime.strptime(str(day),'%Y%m%d')-dt.timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S'))
            b=min(end,(dt.datetime.strptime(str(keep[i+1][0]),'%Y%m%d')+dt.timedelta(days=14)).strftime('%Y-%m-%d 23:59:59')) if i+1<len(keep) else end
            if code in ranges:a=min(a,ranges[code][0]);b=max(b,ranges[code][1])
            ranges[code]=(a,b)
    return selected,ranges

def metadata_server(codes,syms):
    source=R(pathlib.Path('G:/待处理文件/交易系统/探索/新N策略回测-20261024/多源复核/raw/tq_symbols.response'))
    selected=set(codes)|{'CSI.000300'}
    for s in syms:
        p=s.rstrip('0123456789');ex=old.EXCH[p];v=ex+'.'+(p if ex=='CZCE' else p.lower())
        selected.update(['KQ.m@'+v,'KQ.i@'+v])
    assert not selected-source.keys(), sorted(selected-source.keys())
    fields={'class','instrument_id','exchange_id','margin','commission','price_tick','price_decs','volume_multiple','max_limit_order_volume','max_market_order_volume','min_limit_order_volume','min_market_order_volume','underlying_symbol','strike_price','expired','trading_time','expire_datetime','delivery_month','delivery_year','option_class','product_id'}
    body=json.dumps({s:{k:v for k,v in source[s].items() if k in fields} for s in selected}).encode()
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path!='/symbols.json':self.send_error(404);return
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    os.environ['NO_PROXY']=os.environ.get('NO_PROXY','')+',127.0.0.1,localhost'
    return server,thread,'http://127.0.0.1:'+str(server.server_port)+'/symbols.json'

def collect_once(label,table,syms,start,end):
    import numpy as np
    from tqsdk import TqApi,TqAuth,TqSim,TqBacktest,BacktestFinished
    _,ranges=requirements(table,syms,start,end)
    server,thread,url=metadata_server(ranges,syms)
    cfg=R(REPO/'data/config.json')['data_source'];secrets=R(REPO/'data/secrets.json')
    zone=dt.timezone(dt.timedelta(hours=8));begin_ns=int(dt.datetime.fromisoformat(start).replace(tzinfo=zone).timestamp())*10**9
    started=time.monotonic();last_print=started;seen={};last_id={};rows={c:[] for c in ranges};api=None;done=False;calls=0
    print(label,'collecting',len(ranges),'actual contracts',start,end,flush=True)
    try:
        options={}
        if os.environ.get('NTREND_YEAR_MD_URL'):options['url']=os.environ['NTREND_YEAR_MD_URL']
        api=TqApi(account=TqSim(),auth=TqAuth(cfg['tq_account'],cfg.get('tq_password') or secrets.get('tq_password')),
            backtest=TqBacktest(start_dt=dt.date.fromisoformat(start[:10]),end_dt=dt.date.fromisoformat(end[:10])),
            debug=False,disable_print=True,web_gui=False,_stock=False,_ins_url=url,**options)
        print(label,'SDK initialized',flush=True)
        frames={};dates={};buffers={};requested=set();fields=['id','open','high','low','close','volume','close_oi']
        async def subscribe(c):
            # Public async SDK usage: do not block the historical clock waiting
            # for a later member contract to have its first minute.
            f=api.get_kline_serial(c,60,data_length=256);frames[c]=f;dates[c]=f.datetime.values;buffers[c]={k:f[k].values for k in fields}
        def request(c):
            requested.add(c);api.create_task(subscribe(c))
        for c,span in ranges.items():
            if span[0]<=start:request(c)
        print(label,'initial subscriptions requested',len(requested),flush=True)
        while time.monotonic()-started<2400:
            api.wait_update(deadline=time.time()+5);calls+=1
            valid=[float(v[-1]) for v in dates.values() if math.isfinite(float(v[-1])) and v[-1]>0]
            if not valid:continue
            clock=(dt.datetime.fromtimestamp(int(max(valid))//10**9,zone).replace(tzinfo=None)+dt.timedelta(minutes=1)).isoformat(sep=' ')
            for c,span in ranges.items():
                if c not in requested and span[0]<=clock:request(c)
            for c in frames:
                times=dates[c][:-1];buf=buffers[c]
                indices=np.flatnonzero((times>seen.get(c,0))&(times>=begin_ns)&(buf['id'][:-1]>=0))
                for i in indices:
                    ns=int(times[i]);rid=int(buf['id'][i])
                    if c in last_id:assert rid==last_id[c]+1,('SDK buffer skipped',c,last_id[c],rid)
                    seen[c]=ns;last_id[c]=rid
                    ts=(dt.datetime.fromtimestamp(ns//10**9,zone).replace(tzinfo=None)+dt.timedelta(minutes=1)).isoformat(sep=' ')
                    if not ranges[c][0]<=ts<=ranges[c][1]:continue
                    vals={k:float(buf[k][i]) for k in ['open','high','low','close','volume']};oi=float(buf['close_oi'][i])
                    assert all(math.isfinite(x) for x in [*vals.values(),oi]) and vals['volume']>=0
                    assert vals['low']<=min(vals['open'],vals['close'])<=max(vals['open'],vals['close'])<=vals['high']
                    rows[c].append(dict(datetime=ts,**vals,hold=oi))
            if time.monotonic()-last_print>=20:
                print(label,clock,'minutes',sum(map(len,rows.values())),'seconds',round(time.monotonic()-started),flush=True);last_print=time.monotonic()
        raise TimeoutError('Backtest stream deadline')
    except BacktestFinished:
        done=True
        # The final SDK frame has received its CLOSE update, but has no next
        # opening bar to push it into frame[:-1]. Flush only bars whose entire
        # minute ends no later than the explicitly requested cutoff.
        for c,f in frames.items():
            buf=buffers[c]
            for i in range(len(dates[c])):
                ns=int(dates[c][i]) if math.isfinite(float(dates[c][i])) else 0
                if ns<=seen.get(c,0) or ns<begin_ns or buf['id'][i]<0:continue
                ts=(dt.datetime.fromtimestamp(ns//10**9,zone).replace(tzinfo=None)+dt.timedelta(minutes=1)).isoformat(sep=' ')
                if not ranges[c][0]<=ts<=ranges[c][1]:continue
                rid=int(buf['id'][i])
                if c in last_id:assert rid==last_id[c]+1,('SDK final buffer skipped',c,last_id[c],rid)
                vals={k:float(buf[k][i]) for k in ['open','high','low','close','volume']};oi=float(buf['close_oi'][i])
                assert all(math.isfinite(x) for x in [*vals.values(),oi]) and vals['volume']>=0
                assert vals['low']<=min(vals['open'],vals['close'])<=max(vals['open'],vals['close'])<=vals['high']
                rows[c].append(dict(datetime=ts,**vals,hold=oi));seen[c]=ns;last_id[c]=rid
    finally:
        if api is not None:api.close()
        server.shutdown();server.server_close();thread.join(timeout=3)
    assert done and all(rows.values()), [c for c,bs in rows.items() if not bs]
    write(label+'-coverage.json',dict(complete_backtest=done,elapsed_seconds=time.monotonic()-started,updates=calls,raw_prices_saved=False,
        contracts={c:dict(n=len(bs),first=bs[0]['datetime'],last=bs[-1]['datetime'],sha256=old.digest(bs)) for c,bs in rows.items()}))
    return {c:dict(minutes=bs) for c,bs in rows.items()}

def collect(label,table,syms,start,end):
    if '--remote' in sys.argv:
        result={};receipts=[]
        for at in range(0,len(syms),4):
            part=label+'-batch'+str(at//4+1)
            for attempt in range(10):
                try:
                    value=collect_remote(part,table,syms[at:at+4],start,end)
                    break
                except RuntimeError as exc:
                    detail=exc.args[0]
                    if not isinstance(detail,tuple) or detail[0] not in {'TqTimeoutError','ConnectionError','ConnectionResetError','TimeoutError'} or attempt==9:raise
                    print('Retrying transient historical connection',part,attempt+1,detail[0],flush=True);time.sleep(8)
            result.update(value);receipts.append(part+'-coverage.json')
        write(label+'-coverage.json',dict(complete_backtest=True,batches=receipts,raw_prices_saved=True,
            contracts={c:dict(n=len(z['minutes']),first=z['minutes'][0]['datetime'],last=z['minutes'][-1]['datetime'],sha256=old.digest(z['minutes'])) for c,z in result.items()}))
        return result
    for attempt in range(3):
        try:
            if attempt==1:os.environ['NTREND_YEAR_MD_URL']='wss://backtest.shinnytech.com/t/md/front/mobile'
            return collect_once(label,table,syms,start,end)
        except Exception as exc:
            if type(exc).__name__ not in {'TqTimeoutError','ConnectionError','TimeoutError'} or attempt==2:raise
            print(label,'connection retry',attempt+1,type(exc).__name__,flush=True);time.sleep(3)

def collect_remote(label,table,syms,start,end):
    """Same ordinary SDK stream on the already configured server, via stdin.

    Static metadata and code go in; raw observations return over the existing
    SSH channel into memory. No remote source, config or price files are written.
    """
    cache=RUN/'raw-cache'/(label+'.json.gz')
    if cache.exists():
        value=json.loads(gzip.decompress(cache.read_bytes()))
        assert value['request']==dict(symbols=syms,start=start,end=end)
        print('Reusing completed data cache',label,flush=True)
        return value['contracts']
    _,ranges=requirements(table,syms,start,end)
    source=R(pathlib.Path('G:/待处理文件/交易系统/探索/新N策略回测-20261024/多源复核/raw/tq_symbols.response'))
    fields={'class','instrument_id','exchange_id','margin','commission','price_tick','price_decs','volume_multiple','max_limit_order_volume','max_market_order_volume','min_limit_order_volume','min_market_order_volume','underlying_symbol','strike_price','expired','trading_time','expire_datetime','delivery_month','delivery_year','option_class','product_id'}
    selected=set(ranges)|{'CSI.000300'}
    for s in syms:
        p=s.rstrip('0123456789');ex=old.EXCH[p];v=ex+'.'+(p if ex=='CZCE' else p.lower());selected.update(['KQ.m@'+v,'KQ.i@'+v])
    metadata={s:{k:v for k,v in source[s].items() if k in fields} for s in selected}
    expected=hashlib.sha256(R(REPO/'data/config.json')['data_source']['tq_account'].encode()).hexdigest()
    body=inspect.getsource(collect_once)
    body=body.replace('_,ranges=requirements(table,syms,start,end)','ranges=RANGES')
    body=body.replace("cfg=R(REPO/'data/config.json')['data_source'];secrets=R(REPO/'data/secrets.json')", "cfg=R(REPO/'data/config.json')['data_source'];secrets=R(REPO/'data/secrets.json');assert hashlib.sha256(cfg['tq_account'].encode()).hexdigest()==EXPECTED,'Different configured account'")
    # Keep progress off the JSON result channel.
    body=body.replace('flush=True)','flush=True,file=sys.stderr)')
    prelude='RANGES='+repr(ranges)+'\nMETADATA='+repr(metadata)+'\nEXPECTED='+repr(expected)+'\n'+'''
import collections,datetime as dt,hashlib,http.server,json,math,os,pathlib,sys,threading,time,types
REPO=pathlib.Path('/home/ubuntu/pjf/N-Trend')
R=lambda p:json.loads(pathlib.Path(p).read_text(encoding='utf-8'))
old=types.SimpleNamespace(digest=lambda z:hashlib.sha256(json.dumps(z,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest())
receipts={}
def write(name,value):receipts[name]=value
import tqsdk.auth
transport_messages=collections.Counter()
def safe_transport_debug(self,msg,*args,**kwargs):
 if msg in ['websocket connected','websocket reconnected']:
  print('Historical transport connected',file=sys.stderr,flush=True)
 elif msg=='websocket connection closed':
  error=str(kwargs.get('error','')).lower()
  print('Historical transport closed:',[x for x in ['timeout','1006','1008','403','502','503','504','refused','ssl','proxy','handshake','reset'] if x in error],file=sys.stderr,flush=True)
 elif msg=='websocket received data':
  transport_messages[msg]+=1
  if transport_messages[msg]>5:return
  pack=kwargs.get('pack',{})
  if isinstance(pack,str):
   try:pack=json.loads(pack)
   except ValueError:pack={}
  if isinstance(pack,dict):
   flags=[x.get('mdhis_more_data') for x in pack.get('data',[]) if isinstance(x,dict) and 'mdhis_more_data' in x]
   notices=[dict(code=n.get('code'),level=n.get('level')) for x in pack.get('data',[]) if isinstance(x,dict) for n in x.get('notify',{}).values() if isinstance(n,dict)]
   print('Historical transport readiness:',dict(history_flags=flags,notification_codes=notices),file=sys.stderr,flush=True)
tqsdk.auth.ShinnyLoggerAdapter.debug=safe_transport_debug
def metadata_server(codes,syms):
 body=json.dumps(METADATA).encode()
 class Handler(http.server.BaseHTTPRequestHandler):
  def do_GET(self):
   self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
  def log_message(self,*args):pass
 server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
 os.environ['NO_PROXY']=os.environ.get('NO_PROXY','')+',127.0.0.1,localhost'
 return server,thread,'http://127.0.0.1:'+str(server.server_port)+'/symbols.json'
'''
    footer='\ntry:\n value=collect_once('+repr(label)+',None,'+repr(syms)+','+repr(start)+','+repr(end)+')\n result=dict(ok=True,contracts=value,receipts=receipts)\nexcept Exception as exc:\n import traceback\n result=dict(ok=False,error_type=type(exc).__name__,location=[dict(file=pathlib.Path(z.filename).name,function=z.name,line=z.lineno) for z in traceback.extract_tb(exc.__traceback__)])\nprint("YEAR_MINUTES_JSON")\nprint(json.dumps(result,separators=(",",":")))\n'
    print('Remote read-only SDK stream',label,len(ranges),'contracts',flush=True)
    proc=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=15','wlyserver','python3 -'],input=(prelude+body+footer).encode(),stdout=subprocess.PIPE,timeout=2700)
    assert proc.returncode==0,('remote stream failed',proc.returncode)
    marker=b'YEAR_MINUTES_JSON\n';index=proc.stdout.rfind(marker);assert index>=0,'Missing remote result marker'
    value=json.loads(proc.stdout[index+len(marker):])
    if not value['ok']:
        write(label+'-failure-'+dt.datetime.now().strftime('%H%M%S')+'.json',value)
        raise RuntimeError((value.get('error_type'),value.get('location')))
    for name,receipt in value['receipts'].items():
        receipt.update(transport='Ordinary authenticated SDK on existing configured server via SSH; no remote price files',same_configured_account=True,remote_raw_prices_saved=False,raw_prices_saved=True)
        write(name,receipt)
    cache.parent.mkdir(exist_ok=True)
    cache.write_bytes(gzip.compress(json.dumps(dict(request=dict(symbols=syms,start=start,end=end),contracts=value['contracts']),separators=(',',':')).encode(),compresslevel=3))
    return value['contracts']

def merge(dest,source,label):
    matches=conflicts=0;examples=[]
    for c,z in source.items():
        if c not in dest:dest[c]=z;continue
        idx={b['datetime']:b for b in dest[c]['minutes']}
        for b in z['minutes']:
            previous=idx.get(b['datetime'])
            if previous:
                different=[k for k in ['open','high','low','close','volume','hold'] if abs(previous[k]-b[k])>1e-7]
                if different:
                    conflicts+=1
                    if len(examples)<20:examples.append(dict(contract=c,ts=b['datetime'],fields=different))
                else:matches+=1
            # Latest authenticated SDK takes precedence; audit all discrepancies.
            idx[b['datetime']]=b
        dest[c]={'minutes':[idx[t] for t in sorted(idx)]}
    write('merge-'+label+'.json',dict(exact_overlap=matches,conflicts=conflicts,examples=examples))

def stitch(p,table,contracts):
    syms=list(p['specs']);mapping,_=requirements(table,syms,WARM,CUTOFF)
    dates=sorted({b['datetime'][:10] for z in contracts.values() for b in z['minutes'] if '09:00:00'<=b['datetime'][11:]<='15:00:00'})
    cal=TradingDates({});cal.dates=dates
    inp=dict(execution_config=p['execution_config'],symbols={});maps={};audit={}
    for sym,entries in mapping.items():
        ds=[str(z[0]) for z in entries];good=[]
        for code in {z[1] for z in entries}:
            assert code in contracts,('missing contract',code)
            for b in contracts[code]['minutes']:
                if not WARM<=b['datetime']<=CUTOFF:continue
                d=cal.day(b['datetime']).replace('-','');i=bisect.bisect_right(ds,d)-1
                if i>=0 and entries[i][1]==code:good.append((b,code))
        good.sort(key=lambda z:z[0]['datetime']);ts=[b['datetime'] for b,c in good]
        assert len(ts)==len(set(ts)) and ts[0][:10]==WARM[:10] and ts[-1]==CUTOFF,(sym,ts[:1],ts[-1:])
        groups=[]
        for b,c in good:
            if not groups or groups[-1]['underlying']!=c:groups.append(dict(underlying=c,first=b['datetime'],last=b['datetime'],n=1))
            else:groups[-1]['last']=b['datetime'];groups[-1]['n']+=1
        rolls=[dict(symbol=sym,ts=b['first'],from_contract=a['underlying'],to_contract=b['underlying'],confirmed=True) for a,b in zip(groups,groups[1:])]
        bars=old.aggregate([b for b,c in good]);inp['symbols'][sym]=dict(**p['specs'][sym],bars=bars,rollovers=rolls);maps[sym]=groups
        monthly=collections.Counter(cal.day(b['datetime'])[:7] for b,c in good if START<=cal.day(b['datetime'])<=END)
        days=collections.Counter(b['datetime'][:10] for b,c in good if '09:00:00'<=b['datetime'][11:]<='15:00:00' and START<=b['datetime'][:10]<=END)
        audit[sym]=dict(first=ts[0],last=ts[-1],minutes=len(good),bars5=len(bars),months=dict(sorted(monthly.items())),day_sessions=dict(sorted(days.items())),sha256=old.digest(bars))
        assert len(monthly)==13,(sym,'monthly coverage',monthly)
    write('data-audit.json',audit);write('segments.json',maps);write('trading-dates.json',dates)
    return inp,maps,cal

def rust_input(binary,args,memory):
    subprocess.run([str(binary),*map(str,args)],input=json.dumps(memory,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode(),check=True)

def evaluate(p,inp,contracts,maps,cal):
    from report_swing_wave_selection import identification_bars
    from swing_entry_economics import warning_features
    native=old.read(old.RUN/'unmerged/protocol.json');template=[z for z in native['templates'] if z['variant']=='budget_body']
    fees=R(p['fees_path']);ends={c:z['minutes'][-1]['datetime'] for c,z in contracts.items() if z['minutes']}
    accepted_all={};rows_all=collections.defaultdict(list);events_all={};verification=[]
    for sym,z in inp['symbols'].items():
        one=dict(execution_config=inp['execution_config'],symbols={sym:z});cc={c:v for c,v in contracts.items() if product(c)==sym}
        root=RUN/sym;root.mkdir(exist_ok=True)
        pp=dict(selector=0,warning_start=WARNING_START,warning_end=CUTOFF,fees=fees,segments={sym:maps[sym]},holidays=R(RUN/'holidays.json'))
        if not (root/'p2-native-complete.json').exists():
            rust_input(RUN/'p2_year.exe',[root],dict(input=one,contracts=cc,protocol=pp))
            old.write(root/'p2-native-complete.json',dict(input_sha256=old.digest(one),complete=True))
        events=R(root/'p2-events.json');context=completed_context(z['bars'],z['rollovers']);eventmap={}
        for e in events:
            k='|'.join(e[t] for t in ['symbol','direction','s0_ts','s1_ts','s2_ts','warning_ts']);eventmap[k]=e
        for line in (root/'p2-evaluations.jsonl').open(encoding='utf-8'):
            row=json.loads(line);e=eventmap[row['event_key']];ctx=context.get(e['warning_ts']);mult=row['multiplier'];sign=1 if e['direction']=='up' else -1
            fee=fees['products'][sym.rstrip('0123456789')];commission=commission_bound(fee,e['entry']+sign*e['tick_size'],e['stop']-sign*e['tick_size'],mult)
            friction=(2*e['tick_size']*mult+commission)/(e['risk']*mult) if e['risk']>0 else math.inf
            if e['setup_features']['setup_quality']<3-1e-9 or friction>.2+1e-9 or not ctx or not ctx['atr14'] or not .75<=e['risk']/ctx['atr14']<=2.5:continue
            row=reprice(row,fees,cal);row['exit_trading_day']=cal.day(row['result']['evaluation']['exit_ts']) if row['result']['evaluation']['status']=='closed' else None
            rows_all['P2/'+row['scenario']].append(row)
        events_all.update(eventmap)
        if sym in SIX:
            protocol=dict(selector=4,warning_start=WARNING_START,warning_end=CUTOFF,templates=template,modes=['corrected_touch'],hashes={'older':{'input':old.digest(one)}},periods={'older':dict(input='memory:input',contracts='memory:contracts',fees=p['fees_path'],segments=str(RUN/'segments.json'),holidays=str(RUN/'holidays.json'))})
            if not (root/'protocol.json').exists():old.write(root/'protocol.json',protocol)
            if not (root/'older-pool.json').exists():old.engine('swing_older_pool.exe',root/'protocol.json','pool',root/'older-pool.json',dict(input=one))
            pool=R(root/'older-pool.json');bars=identification_bars(z['bars'])
            for snap in pool:snap['entry_quality']=warning_features(snap['event'],bars)
            if not (root/'older-pool-unlabelled.json').exists():
                (root/'older-pool.json').rename(root/'older-pool-unlabelled.json');old.write(root/'older-pool.json',pool)
            if not (root/'older/manifest.json').exists():old.engine('swing_older_execution.exe',root/'protocol.json','replay',root/'older',dict(input=one,contracts=cc))
            events_all.update({snap['event_key']:snap['event'] for snap in pool})
            for line in (root/'older/evaluations.jsonl').open(encoding='utf-8'):
                row=reprice(json.loads(line),fees,cal);row['exit_trading_day']=cal.day(row['result']['evaluation']['exit_ts']) if row['result']['evaluation']['status']=='closed' else None
                rows_all['B5/slip'+str(row['slip'])].append(row)
        print('Finished native product',sym,flush=True)
    spec=importlib.util.spec_from_file_location('frozen_year_controller',REPO/'research-strategies/best-a-portfolio-observation-v1/controller.py');controller=importlib.util.module_from_spec(spec);spec.loader.exec_module(controller)
    sector=R(REPO/'research-strategies/best-a-portfolio-observation-v1/protocol.json')['sector_map']
    with (RUN/'accepted.jsonl').open('x',encoding='utf-8') as out, (RUN/'opportunity-ledgers.jsonl').open('x',encoding='utf-8') as ledgerout:
        for case,rows in sorted(rows_all.items()):
            strategy=case.split('/')[0];aa,ll,risk=controller.select(rows,events_all,ends,sector,'sector2_counter_stops' if strategy=='B5' else 'base','original')
            accepted_all[case]=aa
            for row in aa:out.write(json.dumps(dict(case=case,row=row),ensure_ascii=False)+'\n')
            for row in ll:ledgerout.write(json.dumps(dict(case=case,**row),ensure_ascii=False)+'\n')
            known=[r for r in aa if r['result']['evaluation']['status']=='closed']
            print(case,'closed',len(known),'net',sum(r['result']['evaluation']['net_amount'] for r in known),'accepted',len(aa),flush=True)
    write('contract-ends.json',ends)
    return accepted_all

def main():
    RUN.mkdir(exist_ok=True)
    if (RUN/'replay-complete.json').exists():print('Completed replay already exists.',flush=True);return
    p=R(old.RUN/'protocol.json');p['specs']={s:p['specs'][s] for s in NAMES}
    import requests
    if (RUN/'continuous-table.json').exists():table=R(RUN/'continuous-table.json')
    else:
        table_response=requests.get('https://files.shinnytech.com/continuous_table.json',timeout=45);table_response.raise_for_status();table=table_response.json();write('continuous-table.json',table)
    paths=R(BASE/'protocol.json')['paths'];native=R(REPO/'research-runs/swing-entry-economics-20261025-v1/native-verification/protocol.json')
    holidays=[]
    for hp in [old.RUN/'holidays.json',BASE/'external/holidays.json',REPO/'research-protocols/holiday-windows-2026-jul-oct-v1.json']:
        for h in R(hp)['windows']:
            if h not in holidays:holidays.append(h)
    write('holidays.json',dict(coverage_start=WARM,coverage_end=CUTOFF,windows=sorted(holidays,key=lambda z:z['block_from'])))
    protocol=dict(start_trading_day=START,end_trading_day=END,warning_start=WARNING_START,warmup=WARM,
        universes={'B5':SIX,'P2':list(NAMES)},one_lot=True,slip_ticks_per_side=[1,2],fees_path=p['fees_path'],fees_sha256=old.sha(p['fees_path']),
        table_source='https://files.shinnytech.com/continuous_table.json',table_sha256=old.sha(RUN/'continuous-table.json'),
        monthly='Exit trading month; complete round trips only. Unresolved separate, never zero. Start flat; no monthly reset.',
        max_losing_streak='Sort known closed trades by exit timestamp then event key; any nonnegative net breaks streak. Unknowns separately disclosed.',
        execution='Frozen OHLC research model. Main member chosen by historical trading day, unadjusted member prices. Existing positions remain on original contract.',
        local_price_cache='Compressed per-batch recovery caches; no remote price files.',
        implementation={str(REPO/'scripts/backtest_b5_p2_year.py'):old.sha(__file__),**{str(old.TARGET/'release'/k):v for k,v in R(old.RUN/'binaries.json').items()}},production_changed=False)
    if not (RUN/'protocol.json').exists():write('protocol.json',protocol)
    else:assert R(RUN/'protocol.json')['start_trading_day']==START and R(RUN/'protocol.json')['end_trading_day']==END
    # Build new P2 transport harness directly against the frozen, already built n_core.
    deps=old.TARGET/'release/deps';args=['rustc','--edition=2021','-O',str(REPO/'scripts/p2_year_replay.rs'),'-L','dependency='+str(deps),'-o',str(RUN/'p2_year.exe')]
    for lib in ['n_core','serde_json']:
        rlibs=list(deps.glob('lib'+lib+'-*.rlib'));assert len(rlibs)==1,(lib,rlibs)
        args.extend(['--extern',lib+'='+str(rlibs[0])])
    if not (RUN/'p2_year.exe').exists():subprocess.run(args,check=True)
    # Fill old missing months, then merge existing authenticated EDB snapshots.
    contracts=collect('older',table,list(NAMES),WARM,'2025-12-15 15:00:00')
    merge(contracts,R(BASE/'external/contracts.json'),'early-cache')
    merge(contracts,R(paths['contracts']),'recent-cache')
    gc.collect()
    holes=['BU0','EB0','JD0','MA0','PB0','SH0','SS0']
    merge(contracts,collect('summer-holes',table,holes,'2026-07-13 00:00:00','2026-09-18 15:00:00'),'summer-sdk')
    merge(contracts,collect('latest',table,list(NAMES),'2026-10-21 00:00:00',CUTOFF),'latest-sdk')
    for z in contracts.values():z['minutes']=[b for b in z['minutes'] if WARM<=b['datetime']<=CUTOFF]
    contracts={c:z for c,z in contracts.items() if z['minutes']}
    # Retain the in-memory acquisition briefly if a post-acquisition adapter
    # needs a repair; a local retry marker resumes without exporting prices.
    import traceback
    for attempt in range(3):
        try:
            adapter=sys.modules[__name__]
            if attempt:
                spec=importlib.util.spec_from_file_location('year_repaired_adapter',__file__);adapter=importlib.util.module_from_spec(spec);spec.loader.exec_module(adapter)
            if not (RUN/'data-audit.json').exists():inp,maps,cal=adapter.stitch(p,table,contracts)
            if not (RUN/'input-digest.json').exists():write('input-digest.json',dict(input_sha256=old.digest(inp),contracts_sha256=old.digest(contracts),raw_prices_saved=True))
            adapter.evaluate(p,inp,contracts,maps,cal)
            break
        except Exception:
            (RUN/('adapter-error-'+str(attempt)+'.txt')).write_text(traceback.format_exc(),encoding='utf-8');print(traceback.format_exc(),flush=True)
            deadline=time.monotonic()+300;marker=RUN/'retry-adapter'
            while time.monotonic()<deadline and not marker.exists():time.sleep(2)
            if not marker.exists() or attempt==2:raise
            marker.unlink()
    write('replay-complete.json',dict(complete=True,completed_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat()))

if __name__=='__main__':main()
