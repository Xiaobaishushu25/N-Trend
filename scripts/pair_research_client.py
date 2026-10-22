"""Pair only the isolated research desktop with the local research server. Never prints secrets."""
import argparse,json,os,urllib.request
from pathlib import Path

def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--data-dir',type=Path,default=Path('data-dev'));ap.add_argument('--client-dir',type=Path,default=Path(os.environ.get('APPDATA',str(Path.home())))/'com.ntrend.app.research');a=ap.parse_args()
 if a.client_dir.name!='com.ntrend.app.research':raise ValueError('refusing to modify a production client directory')
 base='http://127.0.0.1:18081'
 with urllib.request.urlopen(base+'/healthz',timeout=5) as response:
  if response.headers.get('x-ntrend-profile')!='research':raise ValueError('target is not an isolated research server')
 key=(a.data_dir/'research-pairing-key.txt').read_text(encoding='utf-8').strip()
 request=urllib.request.Request(base+'/api/v1/devices/register',data=json.dumps({'deviceName':'Desktop PC Research','adminKey':key}).encode(),headers={'Content-Type':'application/json'})
 with urllib.request.urlopen(request,timeout=10) as response:paired=json.load(response)
 record={'server_url':base,'device_id':paired.get('deviceId',paired.get('device_id')),'device_token':paired['token'],'device_role':paired['role'],'device_name':'Desktop PC Research'}
 a.client_dir.mkdir(parents=True,exist_ok=True);dest=a.client_dir/'client_auth.json';temp=a.client_dir/'client_auth.research.tmp';temp.write_text(json.dumps(record,indent=2),encoding='utf-8');os.replace(temp,dest)
 # Probe read-only endpoints with the new token. No data or credential is printed.
 def get(path):
  with urllib.request.urlopen(urllib.request.Request(base+'/api/v1'+path,headers={'Authorization':'Bearer '+record['device_token']}),timeout=20) as response:return json.load(response)
 get('/status');predictions=get('/v2/predictions');chart=get('/chart-klines/RB0/15m?limit=5')
 try:
  urllib.request.urlopen(urllib.request.Request(base+'/api/v1/trader/emergency/close-all',data=b'{}',headers={'Content-Type':'application/json','Authorization':'Bearer '+record['device_token']}),timeout=5)
  raise RuntimeError('research trade boundary unexpectedly allowed a request')
 except urllib.error.HTTPError as error:
  if error.code!=403:raise
 print(json.dumps({'paired':True,'server':base,'profile':'research','qualified_predictions':len(predictions),'chart_status':chart.get('status'),'trading_endpoint':403,'credential_file':str(dest)},ensure_ascii=False))
if __name__=='__main__':main()
