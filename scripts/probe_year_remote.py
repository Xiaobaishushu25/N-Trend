"""Read-only historical connectivity probe on the project's configured server."""
import hashlib,json,pathlib,subprocess,sys
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
import backtest_b5_p2_year as r
cfg=r.R(r.REPO/'data/config.json')['data_source']
source=r.R(pathlib.Path('G:/待处理文件/交易系统/探索/新N策略回测-20261024/多源复核/raw/tq_symbols.response'))
fields={'class','instrument_id','exchange_id','margin','commission','price_tick','price_decs','volume_multiple','max_limit_order_volume','max_market_order_volume','min_limit_order_volume','min_market_order_volume','underlying_symbol','strike_price','expired','trading_time','expire_datetime','delivery_month','delivery_year','option_class','product_id'}
metadata={s:{k:v for k,v in source[s].items() if k in fields} for s in ['CSI.000300','DCE.eg2601','KQ.m@DCE.eg','KQ.i@DCE.eg']}
code='EXPECTED='+repr(hashlib.sha256(cfg['tq_account'].encode()).hexdigest())+'\nMETADATA='+repr(metadata)+'\n'+r'''
import datetime as dt,hashlib,http.server,json,os,pathlib,threading,time
root=pathlib.Path('/home/ubuntu/pjf/N-Trend')
cfg=json.loads((root/'data/config.json').read_text())['data_source'];secrets=json.loads((root/'data/secrets.json').read_text())
assert hashlib.sha256(cfg['tq_account'].encode()).hexdigest()==EXPECTED,'Different account'
body=json.dumps(METADATA).encode()
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 def log_message(self,*args):pass
server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
os.environ['NO_PROXY']=os.environ.get('NO_PROXY','')+',127.0.0.1,localhost'
from tqsdk import TqApi,TqAuth,TqBacktest,TqSim,BacktestFinished
out=dict(same_account=True,status='unconfirmed');api=None
try:
 api=TqApi(TqSim(),auth=TqAuth(cfg['tq_account'],cfg.get('tq_password') or secrets.get('tq_password')),backtest=TqBacktest(start_dt=dt.date(2025,9,22),end_dt=dt.date(2025,9,23)),debug=False,disable_print=True,web_gui=False,_stock=False,_ins_url='http://127.0.0.1:'+str(server.server_port)+'/symbols.json')
 out['initialized']=True;frame=api.get_kline_serial('DCE.eg2601',60,data_length=20);deadline=time.monotonic()+45
 while time.monotonic()<deadline:
  api.wait_update(deadline=time.time()+2)
  n=sum(1 for v in frame.datetime[:-1] if v>0)
  if n>=3:out.update(status='historical_minutes_observed',n=n);break
except Exception as exc:out.update(status='connection_or_permission_error',error_type=type(exc).__name__)
finally:
 if api is not None:api.close()
 server.shutdown();server.server_close()
print(json.dumps(out))
'''
p=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=15','wlyserver','python3 -'],input=code.encode(),capture_output=True,timeout=150)
if p.returncode:out=dict(status='remote_probe_error',returncode=p.returncode)
else:out=json.loads(p.stdout.decode().strip().splitlines()[-1])
(r.RUN/'remote-probe.json').write_text(json.dumps(out,indent=2),encoding='utf-8')
print(json.dumps(out))
