"""Diagnose normal backtest chart readiness; no fabricated flags or data export."""
import asyncio
import datetime as dt
import json
import pathlib
import time
import uuid
import argparse
import urllib.parse

ROOT=pathlib.Path(__file__).resolve().parents[1]
OUT=ROOT/'research-runs/swing-universe-may-aug2025-20261026-v1'


async def probe(after_ready=False,futures_only=False):
    from tqsdk import TqAuth,translator
    import websockets
    cfg=json.loads((ROOT/'data/config.json').read_text(encoding='utf-8'))['data_source']
    secrets=json.loads((ROOT/'data/secrets.json').read_text(encoding='utf-8'))
    auth=TqAuth(cfg['tq_account'],cfg.get('tq_password') or secrets.get('tq_password'))
    auth.init(mode='bt');auth.login()
    url=auth._get_md_url(stock=not futures_only,backtest=True)
    zone=dt.timezone(dt.timedelta(hours=8))
    first=int(dt.datetime(2025,5,6,9,0,tzinfo=zone).timestamp())*10**9
    chart_id='PYSDK_backtest_readiness_'+uuid.uuid4().hex
    chart_request=dict(aid='set_chart',chart_id=chart_id,ins_list='DCE.eg2509',duration=60_000_000_000,
                       view_width=128,focus_datetime=first,focus_position=128)
    result=dict(started_at=dt.datetime.now(zone).isoformat(),authenticated=True,
        request_basis='Same set_chart shape as installed TqBacktest._gen_serial; require genuine mdhis_more_data=false and ready chart; no readiness overrides',
        status='unconfirmed',packets=0,notification_codes=[],permission_denied=False,
        after_instrument_ready=after_ready,futures_only=futures_only,server_host=urllib.parse.urlsplit(url).hostname)
    data={};flag=None;chart={};serial={}
    def merge(dest,src):
        for k,v in src.items():
            if v is None:dest.pop(k,None)
            elif isinstance(v,dict):merge(dest.setdefault(k,{}),v)
            else:dest[k]=v
    try:
        async with websockets.connect(url,additional_headers=auth._base_headers,max_size=None,open_timeout=15,close_timeout=3) as ws:
            request_sent=not after_ready
            if request_sent:await ws.send(json.dumps(chart_request))
            await ws.send(json.dumps(dict(aid='peek_message')))
            deadline=time.monotonic()+65
            while time.monotonic()<deadline:
                try:message=await asyncio.wait_for(ws.recv(),timeout=min(10,deadline-time.monotonic()))
                except asyncio.TimeoutError:continue
                result['packets']+=1
                pack=translator.loads(message,rules=translator.MD_TO_CTP)
                for diff in pack.get('data',[]):
                    if not isinstance(diff,dict):continue
                    for n in diff.get('notify',{}).values():
                        if isinstance(n,dict):
                            result['notification_codes'].append(dict(code=n.get('code'),level=n.get('level')))
                            if n.get('level')=='ERROR':
                                content=str(n.get('content','')).lower()
                                result['permission_denied']=any(x in content for x in ['权限','permission','entitlement','forbidden','unauthor'])
                                result['status']='server_error_notification';break
                    merge(data,diff)
                if result['permission_denied']:break
                if not request_sent and (data.get('insserve_ready') is True or data.get('mdhis_more_data') is False):
                    await ws.send(json.dumps(chart_request));request_sent=True
                    result['requested_after_true_instrument_ready']=True
                flag=data.get('mdhis_more_data')
                chart=data.get('charts',{}).get(chart_id,{})
                serial=data.get('klines',{}).get('DCE.eg2509',{}).get('60000000000',{})
                state=chart.get('state',{})
                matching=all(state.get(k)==v for k,v in chart_request.items())
                left,right=chart.get('left_id',-1),chart.get('right_id',-1)
                samples=[z for key,z in serial.get('data',{}).items() if isinstance(z,dict) and z.get('datetime',0)>0 and str(key).isdigit() and left<=int(key)<=right]
                positioned=bool(samples) and min(z['datetime'] for z in samples)<=first<=max(z['datetime'] for z in samples)+60_000_000_000
                if flag is False and chart.get('ready') is True and matching and len(samples)>=3 and positioned:
                    result.update(status='complete_historical_chart_observed',samples=len(samples),first_ns=min(z['datetime'] for z in samples),last_ns=max(z['datetime'] for z in samples));break
                await ws.send(json.dumps(dict(aid='peek_message')))
            result.update(history_complete_flag=flag,chart_ready=chart.get('ready'),chart_left=chart.get('left_id'),chart_right=chart.get('right_id'),serial_last_id=serial.get('last_id'),stored_rows=len(serial.get('data',{})),state_matches_request=matching,focus_positioned=positioned)
            if result['status']=='unconfirmed':result['status']='no_complete_chart_before_deadline'
            await ws.send(json.dumps(dict(aid='set_chart',chart_id=chart_id,ins_list='',duration=60_000_000_000,view_width=128)))
    except Exception as e:result.update(status='connection_error',error_type=type(e).__name__)
    result['finished_at']=dt.datetime.now(zone).isoformat()
    path=OUT/('chart-readiness-'+dt.datetime.now().strftime('%Y%m%d-%H%M%S')+'.json')
    path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result),flush=True)


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--after-ready',action='store_true');ap.add_argument('--futures-only',action='store_true');args=ap.parse_args()
    asyncio.run(probe(args.after_ready,args.futures_only))
