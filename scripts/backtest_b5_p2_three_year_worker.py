import os,sys,sqlite3,json,datetime as dt,collections,bisect,pathlib,inspect,importlib.util,shutil,gc
sys.path.insert(0,'scripts')
import backtest_b5_p2_year as y
import research_swing_older_four as old
from reprice_signal_costs import TradingDates
R=old.read
out=pathlib.Path(os.environ['THREE_YEAR_PART']);out.mkdir(exist_ok=True)
y.RUN=out;y.START='2023-10-30';y.END='2026-10-27';y.WARM='2023-10-28 00:00:00';y.CUTOFF='2026-10-27 15:00:00';y.WARNING_START='2023-10-28 00:00:00'
if not (out/'p2_year.exe').exists():shutil.copy2('research-runs/b5-p2-trailing-year-20261027-v9/p2_year.exe',out/'p2_year.exe')
c=sqlite3.connect('file:data/research_market_history.sqlite?mode=ro',uri=True)
table=collections.defaultdict(list)
for p,d,code in c.execute('select product,effective_date,contract from mappings order by effective_date'): table[p].append([d.replace('-',''),code])
# Daytime sessions across products, from complete hourly observations.
dates=[x[0] for x in c.execute("select distinct date(start_ns/1000000000,'unixepoch','+8 hours') from bars where period_seconds=3600 and cast(strftime('%H',start_ns/1000000000,'unixepoch','+8 hours') as integer) between 9 and 14 order by 1")]
cal=TradingDates({});cal.dates=dates
windows=[]
for a,b in zip(dates,dates[1:]):
 da,db=dt.date.fromisoformat(a),dt.date.fromisoformat(b)
 if (db-da).days>3:
  windows.append(dict(name='inferred_holiday',block_from=a+' 14:30:00',flat_at=a+' 14:45:00',last_close=a+' 15:00:00',reopen=b+' 09:00:00'))
(out/'holidays.json').write_text(json.dumps(dict(windows=windows),ensure_ascii=False,indent=2),encoding='utf8')
p=R(old.RUN/'protocol.json');p['specs']={s:p['specs'][s] for s in y.NAMES}
# Reuse frozen evaluation through the extraction point before portfolio selection.
src=inspect.getsource(y.evaluate); src=src[:src.index('    spec=importlib.util.spec_from_file_location')]+"    return rows_all,events_all,ends\n"
exec(src,y.__dict__);evaluate=y.__dict__['evaluate']
audit={};allrows=collections.defaultdict(list);events={};ends={}
for sym in json.loads(os.environ['THREE_YEAR_SYMBOLS']):
 print('Loading',sym,flush=True)
 prod=sym[:-1];ex=old.EXCH[prod];var=ex+'.'+(prod if ex=='CZCE' else prod.lower());entries=table[var];ds=[x[0] for x in entries]
 contracts={};good=[]
 for _,code in entries:
  if code in contracts:continue
  raw=c.execute('select end_ns,open,high,low,close,volume,close_oi from bars where contract=? and period_seconds=60 order by start_ns',(code,)).fetchall()
  bs=[]
  for ns,o,h,l,cl,v,oi in raw:
   t=(dt.datetime(1970,1,1)+dt.timedelta(seconds=ns//1000000000,hours=8)).isoformat(sep=' ')
   if t>y.CUTOFF:continue
   b=dict(datetime=t,open=o,high=h,low=l,close=cl,volume=v,hold=oi or 0);bs.append(b)
   day=cal.day(t).replace('-','');i=bisect.bisect_right(ds,day)-1
   if i>=0 and entries[i][1]==code:good.append((b,code))
  if bs:contracts[code]={'minutes':bs}
 good.sort(key=lambda x:x[0]['datetime']);assert good and len(good)==len({b['datetime'] for b,_ in good})
 groups=[]
 for b,code in good:
  if not groups or groups[-1]['underlying']!=code:groups.append(dict(underlying=code,first=b['datetime'],last=b['datetime'],n=1))
  else:groups[-1]['last']=b['datetime'];groups[-1]['n']+=1
 rolls=[dict(symbol=sym,ts=b['first'],from_contract=a['underlying'],to_contract=b['underlying'],confirmed=True) for a,b in zip(groups,groups[1:])]
 bars=old.aggregate([b for b,_ in good]);inp=dict(execution_config=p['execution_config'],symbols={sym:dict(**p['specs'][sym],bars=bars,rollovers=rolls)});maps={sym:groups}
 # Research engine reads all segment records from this shared file.
 (out/'segments.json').write_text(json.dumps(maps,ensure_ascii=False),encoding='utf8')
 audit[sym]=dict(first=good[0][0]['datetime'],last=good[-1][0]['datetime'],minutes=len(good),bars5=len(bars),rolls=len(rolls),months=dict(sorted(collections.Counter(cal.day(b['datetime'])[:7] for b,_ in good).items())),segments=groups)
 rows,ev,en=evaluate(p,inp,contracts,maps,cal)
 for case,rs in rows.items():allrows[case].extend(rs)
 events.update(ev);ends.update(en)
 print('Collected',sym,[(k,len(v)) for k,v in rows.items()],flush=True)
 del contracts,good,inp,bars,rows,ev,en;gc.collect()
(out/'audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf8')

import pickle
with (out/'partial.pkl').open('wb') as f:pickle.dump((dict(allrows),events,ends,audit),f,protocol=pickle.HIGHEST_PROTOCOL)
print('PART COMPLETED',flush=True)
