import hashlib,json,pathlib,subprocess,sys
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
import backtest_b5_p2_year as r
expected=hashlib.sha256(r.R(r.REPO/'data/config.json')['data_source']['tq_account'].encode()).hexdigest()
code='EXPECTED='+repr(expected)+'\n'+r'''
import asyncio,hashlib,json,pathlib,urllib.parse
from tqsdk import TqAuth
import websockets
p=pathlib.Path('/home/ubuntu/pjf/N-Trend');cfg=json.loads((p/'data/config.json').read_text())['data_source'];secrets=json.loads((p/'data/secrets.json').read_text())
assert hashlib.sha256(cfg['tq_account'].encode()).hexdigest()==EXPECTED
auth=TqAuth(cfg['tq_account'],cfg.get('tq_password') or secrets.get('tq_password'));auth.init(mode='bt');auth.login()
out={}
try:out['expiration']=str(auth.expire_datetime)
except Exception as exc:out['expiration_status']=type(exc).__name__
out['product_type']=getattr(auth,'_product_type',None)
url=auth._get_md_url(stock=False,backtest=True);parsed=urllib.parse.urlsplit(url);out.update(host=parsed.hostname,path=parsed.path)
async def check():
 try:
  async with websockets.connect(url,additional_headers=auth._base_headers,max_size=None,open_timeout=15,close_timeout=2) as ws:
   out['websocket']='connected'
 except Exception as exc:
  out['websocket_error']=type(exc).__name__;response=getattr(exc,'response',None)
  out['http_status']=getattr(response,'status_code',None)
asyncio.run(check());print(json.dumps(out))
'''
p=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=15','wlyserver','python3 -'],input=code.encode(),capture_output=True,timeout=90)
if p.returncode:out=dict(status='diagnostic_error',returncode=p.returncode)
else:out=json.loads(p.stdout.decode().strip().splitlines()[-1])
(r.RUN/'connection-diagnostic.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(out,ensure_ascii=False))
