import sqlite3,json,collections,bisect,pathlib,pickle,importlib.util,sys
sys.path.insert(0,'scripts');from research_swing_older_four import EXCH
from reprice_signal_costs import TradingDates
root=pathlib.Path('research-runs/b5-p2-three-year-20261028');c=sqlite3.connect('file:data/research_market_history.sqlite?mode=ro',uri=True);table=collections.defaultdict(list)
for p,d,s in c.execute('select product,effective_date,contract from mappings order by effective_date'):table[p].append((d,s))
cal=TradingDates({});cal.dates=[x[0] for x in c.execute("select distinct date(start_ns/1000000000,'unixepoch','+8 hours') from bars where period_seconds=3600 and cast(strftime('%H',start_ns/1000000000,'unixepoch','+8 hours') as integer) between 9 and 14 order by 1")]
rows=collections.defaultdict(list);events={};ends={};excluded=collections.defaultdict(list)
for i in range(3):
 with (root/('part'+str(i))/'partial.pkl').open('rb') as f:rr,ee,en,a=pickle.load(f)
 for case,rs in rr.items():
  for r in rs:
   s=r['symbol'][:-1];ex=EXCH[s];p=ex+'.'+(s if ex=='CZCE' else s.lower());es=table[p];ds=[x[0] for x in es];e=r['result']['evaluation'];reason=[]
   for field in ('trigger_ts','entry_ts'):
    ts=e.get(field)
    if not ts:continue
    day=cal.day(ts);index=bisect.bisect_right(ds,day)-1;expected=es[index][1] if index>=0 else None
    if expected!=r['execution_contract']:reason.append(dict(field=field,ts=ts,expected=expected,used=r['execution_contract']))
   if reason:excluded[case].append(dict(event_key=r['event_key'],reason=reason,status=e['status']))
   else:rows[case].append(r)
 events.update(ee);ends.update(en)
spec=importlib.util.spec_from_file_location('controller','research-strategies/best-a-portfolio-observation-v1/controller.py');ctl=importlib.util.module_from_spec(spec);spec.loader.exec_module(ctl)
sector=json.loads(pathlib.Path('research-strategies/best-a-portfolio-observation-v1/protocol.json').read_text(encoding='utf8'))['sector_map']
with (root/'accepted.jsonl').open('w',encoding='utf8') as f:
 for case,rs in sorted(rows.items()):
  accepted,ledger,risk=ctl.select(rs,events,ends,sector,'sector2_counter_stops' if case.startswith('B5/') else 'base','original')
  for row in accepted:f.write(json.dumps(dict(case=case,row=row),ensure_ascii=False)+'\n')
  print(case,'excluded opportunities',len(excluded[case]),'accepted',len(accepted),collections.Counter(r['result']['evaluation']['status'] for r in accepted))
(root/'roll-entry-exclusions.json').write_text(json.dumps(excluded,indent=2,ensure_ascii=False),encoding='utf8')
