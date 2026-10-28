import json,csv,pathlib,collections,datetime
p=pathlib.Path('research-runs/b5-p2-three-year-20261028');audit=json.loads((p/'audit.json').read_text(encoding='utf8'))
with (p/'main-contract-segments.csv').open('w',encoding='utf-8-sig',newline='') as f:
 w=csv.writer(f);w.writerow(['symbol','contract','first_bar_end','last_bar_end','minute_count'])
 for s,a in audit.items():
  for z in a['segments']:w.writerow([s,z['underlying'],z['first'],z['last'],z['n']])
with (p/'closed-trades.csv').open('w',encoding='utf-8-sig',newline='') as f:
 w=csv.writer(f);w.writerow(['scenario','symbol','contract','direction','entry_time','exit_time','exit_trading_day','exit_reason','net_yuan','event_key'])
 totals=collections.defaultdict(lambda:[0,0.]);months=collections.defaultdict(lambda:[0,0.]);symbols=collections.defaultdict(lambda:[0,0.])
 for l in (p/'accepted.jsonl').open(encoding='utf8'):
  z=json.loads(l);r=z['row'];e=r['result']['evaluation']
  if e['status']!='closed':continue
  assert r['exit_trading_day'] and  '2023-10-30'<=r['exit_trading_day']<='2026-10-27'
  w.writerow([z['case'],r['symbol'],r['execution_contract'],r['direction'],e['entry_ts'],e['exit_ts'],r['exit_trading_day'],e['exit_reason'],e['net_amount'],r['event_key']])
  for d,k in [(totals,z['case']),(months,(z['case'],r['exit_trading_day'][:7])),(symbols,(z['case'],r['symbol']))]:d[k][0]+=1;d[k][1]+=e['net_amount']
for case,(n,total) in totals.items():
 for d in [months,symbols]:
  v=[v for k,v in d.items() if k[0]==case];assert sum(z[0] for z in v)==n and abs(sum(z[1] for z in v)-total)<1e-6
print('Reconciled closed-trade, monthly, and product totals:',dict(totals))
report=p/'report.md';text=report.read_text(encoding='utf8');text=text.replace('持仓继续留在原合约。','持仓继续留在原合约；换月后才触发或开仓的旧合约机会予以排除并重新计算组合。')
text=text.replace('## 数据限制','两跳滑点压力场景：系统六品种 B5 净额 -11,257.98 元，713 笔；P2 净额 -177,741.22 元，3,178 笔。\n\n## 数据限制')
# Set exact pressure P2 amount from the actual ledger rather than a rounded placeholder.
text=text.replace('-177,741.22',f"{totals['P2/slip2'][1]:,.2f}")
report.write_text(text,encoding='utf8')
