"""Read-only availability probe; no credentials or process command lines printed."""
import json
import subprocess
import argparse
import pathlib
import hashlib

REMOTE=r'''
import importlib.util,json,pathlib,subprocess
root=pathlib.Path('/home/ubuntu/pjf/N-Trend')
out=[]
for py in ['python3',str(root/'.venv/bin/python'),str(root/'python-bridge/.venv/bin/python')]:
    if py!='python3' and not pathlib.Path(py).exists():continue
    code="import importlib.util,json,sys;print(json.dumps({'python':sys.version.split()[0],'tqsdk':bool(importlib.util.find_spec('tqsdk'))}))"
    p=subprocess.run([py,'-c',code],capture_output=True,text=True,timeout=20)
    out.append(dict(path=py,returncode=p.returncode,metadata=json.loads(p.stdout) if p.returncode==0 else None))
print(json.dumps(out))
'''

PROBE=r'''
import datetime as dt,hashlib,json,pathlib,sys,time
root=pathlib.Path('/home/ubuntu/pjf/N-Trend')
cfg=json.loads((root/'data/config.json').read_text())['data_source']
secrets=json.loads((root/'data/secrets.json').read_text()) if (root/'data/secrets.json').exists() else {}
assert hashlib.sha256(cfg['tq_account'].encode()).hexdigest()==EXPECTED,'Different configured research account'
mem={k:int(v.split()[0]) for k,v in (line.split(':',1) for line in pathlib.Path('/proc/meminfo').read_text().splitlines())}
from tqsdk import TqApi,TqAuth,TqSim,TqBacktest,BacktestFinished
import tqsdk
out=dict(same_configured_account=True,sdk_version=tqsdk.__version__,available_memory_kib=mem.get('MemAvailable'),started_at=dt.datetime.now().astimezone().isoformat())
api=None
try:
    api=TqApi(TqSim(),auth=TqAuth(cfg['tq_account'],cfg.get('tq_password') or secrets.get('tq_password')),backtest=TqBacktest(start_dt=dt.date(2025,5,6),end_dt=dt.date(2025,5,7)),debug=False,disable_print=True,web_gui=False)
    out['initialized']=True
    serial=api.get_kline_serial('DCE.eg2509',60,data_length=128)
    deadline=time.monotonic()+45
    while time.monotonic()<deadline:
        api.wait_update(deadline=time.time()+2)
        values=[int(v) for v in serial.datetime[:-1] if int(v)>1746403200000000000]
        if len(values)>=3:
            out.update(status='older_minute_access_observed',timestamps=len(values),first_ns=min(values),last_ns=max(values));break
    else:out['status']='no_sample_before_deadline'
except BacktestFinished:out['status']='finished_before_sample'
except Exception as e:out.update(status='connection_or_permission_error',error_type=type(e).__name__)
finally:
    if api is not None:api.close()
out['finished_at']=dt.datetime.now().astimezone().isoformat()
print(json.dumps(out))
'''
if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--data-probe',action='store_true');a=ap.parse_args()
    code=REMOTE
    if a.data_probe:
        cfg=json.loads(pathlib.Path('data/config.json').read_text(encoding='utf-8'))['data_source']
        code='EXPECTED='+repr(hashlib.sha256(cfg['tq_account'].encode()).hexdigest())+'\n'+PROBE
    p=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=15','wlyserver','python3 -'],input=code.encode(),capture_output=True,timeout=145)
    if p.returncode:print(json.dumps(dict(returncode=p.returncode,error='SSH/environment probe failed')))
    else:
        output=p.stdout.decode();print(output)
        if a.data_probe:
            result=json.loads(output.strip().splitlines()[-1])
            pathlib.Path('research-runs/swing-universe-may-aug2025-20261026-v1/access-remote.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
