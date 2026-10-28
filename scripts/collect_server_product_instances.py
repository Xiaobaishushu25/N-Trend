"""Read-only inventory and snapshots for product PnL reconciliation."""
import json
import pathlib
import subprocess

REMOTE = r'''
import datetime,json,urllib.request
def api(path):
    with urllib.request.urlopen('http://127.0.0.1:8081/api/v1'+path,timeout=20) as f:return json.load(f)
print(json.dumps({'captured_at':datetime.datetime.now().astimezone().isoformat(),'instances':api('/trader/instances'),'comparison':api('/trader/instances/comparison')},ensure_ascii=True))
'''
if __name__ == '__main__':
    p=pathlib.Path('research-runs/swing-product-frequency-audit-20261026-v2/instances-api.json')
    if p.exists():raise FileExistsError(p)
    r=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=15','wlyserver','python3 -'],input=REMOTE.encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=90)
    if r.returncode:raise RuntimeError(r.stderr.decode(errors='replace'))
    z=json.loads(r.stdout)
    p.write_text(json.dumps(z,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(z,ensure_ascii=True)[:18000])
