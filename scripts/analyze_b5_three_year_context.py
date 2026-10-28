import json,pathlib,collections,statistics,datetime
p=pathlib.Path('research-runs/b5-p2-three-year-20261028');ev={}
for s,i in {'EG0':2,'PP0':2,'V0':0,'JD0':1,'SR0':0,'CJ0':0}.items():
 for z in json.loads((p/f'part{i}'/s/'older-pool.json').read_text(encoding='utf8')):ev[z['event_key']]=z['event']
rows=[json.loads(line) for line in (p/'b5-ambiguity-flat-accepted.jsonl').open(encoding='utf8')];rows=[r for r in rows if r['result']['evaluation']['status']=='closed']
missing=[r['event_key'] for r in rows if r['event_key'] not in ev];print('events',len(ev),'closed',len(rows),'missing',len(missing));assert not missing
periods={'2024':('2024-01-01','2024-12-31'),'recent':('2025-10-27','2026-10-27')};groups=collections.defaultdict(list);issues=[]
for r in rows:
 e=ev[r['event_key']];m=e.get('market_context') or {};w=e['warning_ts']
 for f in ('as_of_ts','latest_15m_close_ts','latest_60m_close_ts'):
  if m.get(f) and m[f]>w:issues.append((w,f,m[f]))
 for pn,(a,b) in periods.items():
  if not a<=r['exit_trading_day']<=b:continue
  for field in ('trend_alignment_60','trend_alignment_10d'):
   v=m.get(field);label='unknown' if v is None else 'aligned' if v>0 else 'opposed_or_equal'
   groups[(pn,field,label,'ALL')].append(r);groups[(pn,field,label,r['symbol'])].append(r)
  state=e['setup_features'].get('trend60_state') or 'unknown'
  groups[(pn,'state',state,'ALL')].append(r);groups[(pn,'state',state,r['symbol'])].append(r)
print('future timestamp issues',len(issues))
def stats(rs):
 net=[r['result']['evaluation']['net_amount'] for r in rs];R=[r['result']['evaluation']['net_r'] for r in rs];return dict(n=len(rs),net=round(sum(net),2),mean=round(statistics.mean(net),2),win=round(100*sum(x>0 for x in net)/len(net),1),mean_R=round(statistics.mean(R),3))
for k,rs in sorted(groups.items()):
 if k[3]=='ALL' or (k[1]=='trend_alignment_60' and k[3]=='EG0'):print(k,stats(rs))
out={ '|'.join(k):stats(rs) for k,rs in groups.items()};(p/'b5-context-descriptive.json').write_text(json.dumps(dict(groups=out,time_checks=len(rows),time_issues=issues,limitations=['accepted closed trades, no re-selection; descriptive only','1h EMA direction, not a direct measure of trend or ranging','10-day feature omits rollover-crossing observations','20/60 EMA may span roll, no adjusted prices','correlated observations, no holdout']),indent=2,ensure_ascii=False),encoding='utf8')
