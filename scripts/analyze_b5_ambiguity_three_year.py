import pathlib,json,pickle,collections,importlib.util
root=pathlib.Path('research-runs/b5-p2-three-year-20261028');allrows=[];events={};ends={}
for i in range(3):
 with (root/('part'+str(i))/'partial.pkl').open('rb') as f:r,e,en,a=pickle.load(f)
 allrows.extend(r.get('B5/slip1.0',[]));events.update(e);ends.update(en)
s=importlib.util.spec_from_file_location('controller','research-strategies/best-a-portfolio-observation-v1/controller.py');ctl=importlib.util.module_from_spec(s);s.loader.exec_module(ctl);sector=json.loads(pathlib.Path('research-strategies/best-a-portfolio-observation-v1/protocol.json').read_text(encoding='utf8'))['sector_map']
periods={'2023':('2023-10-30','2023-12-31'),'2024':('2024-01-01','2024-12-31'),'2025':('2025-01-01','2025-12-31'),'2026':('2026-01-01','2026-10-27'),'trailing':('2025-10-27','2026-10-27'),'full':('2023-10-30','2026-10-27')};out={}
for policy in ['original','two_paths_flat']:
 aa,ledger,risk=ctl.select(allrows,events,ends,sector,'sector2_counter_stops',policy)
 closed=[r for r in aa if r['result']['evaluation']['status']=='closed'];period_results={}
 for name,(start,end) in periods.items():
  xx=[r for r in closed if start<=r['exit_trading_day']<=end];v=[r['result']['evaluation']['net_amount'] for r in xx];pp=[r for r in xx if r['symbol']=='PP0'];period_results[name]={'n':len(xx),'net':sum(v),'win':sum(x>0 for x in v)/len(v) if v else 0,'PP_n':len(pp),'PP_net':sum(r['result']['evaluation']['net_amount'] for r in pp)}
 out[policy]={'periods':period_results,'ambiguous_accepted':[{'warning':r['warning_ts'],'symbol':r['symbol'],'status':r['result']['evaluation']['status']} for r in aa if r['result']['evaluation']['status']=='ambiguous'],'keys':[r['event_key'] for r in closed]}
 print(policy,json.dumps(period_results,ensure_ascii=False))
 if policy=='two_paths_flat':
  with (root/'b5-ambiguity-flat-accepted.jsonl').open('w',encoding='utf8') as f:
   for r in aa:f.write(json.dumps(r,ensure_ascii=False)+'\n')
(root/'b5-ambiguity-sensitivity.json').write_text(json.dumps(out,indent=2,ensure_ascii=False),encoding='utf8')
