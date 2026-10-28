"""Explicitly incomplete fallback: report available archived main-contract data.

Never labels these results as a complete one-year backtest.
"""
import collections,csv,datetime as dt,importlib.util,json,math,pathlib,sys
sys.dont_write_bytecode=True
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
import backtest_b5_p2_year as r
import research_swing_monthly_products as m
from report_b5_p2_year import stats,ev,MONTHS
from entry_model import completed_context,commission_bound
from reprice_signal_costs import TradingDates,reprice
OUT=r.REPO/'research-runs/b5-p2-available-archive-20261028-v1'
def mapping():
 import bisect
 table=r.R(r.RUN/'continuous-table.json');holidays=r.R(r.RUN/'holidays.json')['windows'];dates=[];day=dt.date.fromisoformat(r.START)
 while day<=dt.date.fromisoformat(r.END):
  s=day.isoformat()
  if day.weekday()<5 and not any(z['last_close'][:10]<s<z['reopen'][:10] for z in holidays):dates.append(s)
  day+=dt.timedelta(days=1)
 rows=[]
 for sym in r.NAMES:
  p=sym.rstrip('0123456789');ex=r.old.EXCH[p];es=table[ex+'.'+(p if ex=='CZCE' else p.lower())];ds=[str(z[0]) for z in es]
  for month in MONTHS:
   groups=[]
   for day in [d for d in dates if d[:7]==month]:
    code=es[bisect.bisect_right(ds,day.replace('-',''))-1][1]
    if not groups or groups[-1]['contract']!=code:groups.append(dict(contract=code,first=day,last=day))
    else:groups[-1]['last']=day
   rows.extend(dict(month=month,symbol=sym,name=r.NAMES[sym],**g) for g in groups)
 (OUT/'monthly-contracts.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
 # Independently check every known execution's warning contract against table.
 data=r.R(OUT/'summary.json');checked=0
 for z in data['trades']:
  s=z['symbol'];p=s.rstrip('0123456789');ex=r.old.EXCH[p];es=table[ex+'.'+(p if ex=='CZCE' else p.lower())];ds=[str(a[0]) for a in es]
  date=z['warning_ts'][:10];hour=int(z['warning_ts'][11:13]);i=bisect.bisect_right(dates,date) if hour>=18 else bisect.bisect_left(dates,date)
  assert i<len(dates)
  member=es[bisect.bisect_right(ds,dates[i].replace('-',''))-1][1]
  assert member==z['contract'],(z['event_key'],member,z['contract']);checked+=1
 (OUT/'main-contract-verification.json').write_text(json.dumps(dict(known_executions_checked=checked,all_actual_main_contracts_match=True),indent=2),encoding='utf-8')
def main():
 OUT.mkdir(exist_ok=True)
 controller,p,events,ends,data,sources,coverage=m.load()
 accepted={}
 for slip in [1,2]:
  rows=[z for z in data['unmerged'][('budget_body','corrected_touch/slip'+str(slip))] if z['symbol'] in r.SIX and r.WARNING_START<=z['warning_ts']<=r.CUTOFF]
  accepted['B5/slip'+str(slip)],_,_=controller.select(rows,events['unmerged'],ends['unmerged'],p['sector_map'],'sector2_counter_stops','original')
 paths=r.R(r.BASE/'protocol.json')['paths'];fees=r.R(paths['fees']);native=r.R(r.REPO/'research-runs/swing-entry-economics-20261025-v1/native-verification/protocol.json')
 sources_by_period={};all_dates=set();pevents={};pends={};prows=collections.defaultdict(list)
 for period in ['early','recent']:
  ip=native['periods'][period]['input'];source=r.R(ip);cal=TradingDates(source['symbols']);all_dates.update(cal.dates)
  es=r.R(native['periods'][period]['signals']);lookup={'|'.join(e[k] for k in ['symbol','direction','s0_ts','s1_ts','s2_ts','warning_ts']):e for e in es};pevents.update(lookup)
  contexts={s:completed_context(v['bars'],v['rollovers']) for s,v in source['symbols'].items()}
  manifest=r.R(r.REPO/('research-runs/swing-entry-economics-20261025-v1/native-verification/'+period+'/manifest.json'))
  for c,end in manifest['contract_ends'].items():pends[c]=max(end,pends.get(c,''))
  raw=r.BASE/('external/replay/P2.jsonl' if period=='early' else 'development-replay/P2.jsonl')
  for line in raw.open(encoding='utf-8'):
   row=json.loads(line);e=lookup[row['event_key']];ctx=contexts[e['symbol']].get(e['warning_ts']);sign=1 if e['direction']=='up' else -1;mult=row['multiplier'];spec=fees['products'][e['symbol'].rstrip('0123456789')]
   cost=commission_bound(spec,e['entry']+sign*e['tick_size'],e['stop']-sign*e['tick_size'],mult)
   friction=(2*e['tick_size']*mult+cost)/(e['risk']*mult) if e['risk'] else math.inf
   if row['score']<3-1e-9 or friction>.2+1e-9 or not ctx or not ctx['atr14'] or not .75<=e['risk']/ctx['atr14']<=2.5:continue
   row=reprice(row,fees,cal)
   if ev(row)['status']=='closed':row['exit_trading_day']=cal.day(ev(row)['exit_ts'])
   prows[row['scenario'].split('/')[-1]].append(row)
 for slip in [1,2]:accepted['P2/slip'+str(slip)],_,_=controller.select(prows['slip'+str(slip)],pevents,pends,p['sector_map'],'base','original')
 summary={k:stats(v) for k,v in accepted.items()};monthly=[];products=[];trades=[];unresolved=[]
 def coverage_note(strategy,month):
  if strategy=='P2' and month<'2026-01':return '缺少该月原P2完整行情/信号，非零交易'
  if month=='2026-10':return '仅截至2026-10-23 11:05，未补至26日'
  if strategy=='P2' and '2026-07'<=month<='2026-09':return '部分品种起点不齐，存在行情缺口'
  if strategy=='B5' and month in ['2026-07','2026-08']:return '鸡蛋7月中旬至8月上旬行情缺口'
  return '现有月份合约档案覆盖'
 for case,rows in accepted.items():
  strategy,slip=case.split('/')
  for month in MONTHS:
   xs=[z for z in rows if ev(z)['status']=='closed' and z['exit_trading_day'][:7]==month];s=stats(xs);missing=strategy=='P2' and month<'2026-01'
   monthly.append(dict(strategy=strategy,slip=slip,month=month,n=None if missing else s['n'],net=None if missing else s['net'],gross_after_slippage=None if missing else s['gross_after_slippage'],commission=None if missing else s['commission'],coverage=coverage_note(strategy,month)))
  for sym in r.SIX if strategy=='B5' else r.NAMES:
   s=stats([z for z in rows if z['symbol']==sym]);products.append(dict(strategy=strategy,slip=slip,symbol=sym,name=r.NAMES[sym],n=s['n'],net=s['net'],unknown=s['unknown'],coverage='仅可用档案片段；不是全年合计'))
  for z in rows:
   e=ev(z)
   if e['status']!='closed':
    if e['status'] in {'ambiguous','missing_data','rollover','unfinished','pending'}:unresolved.append(dict(strategy=strategy,slip=slip,symbol=z['symbol'],contract=z['execution_contract'],warning_ts=z['warning_ts'],entry_ts=e.get('entry_ts'),status=e['status'],event_key=z['event_key']))
    continue
   fs=z['result']['fills'];sign=1 if z['direction']=='up' else -1;gross=sum(sign*(x['price']-fs[0]['price'])*x['volume']*z['multiplier'] for x in fs[1:]);assert abs(gross-z['cost_repricing']['commission']-e['net_amount'])<1e-6
   trades.append(dict(strategy=strategy,slip=slip,symbol=z['symbol'],name=r.NAMES[z['symbol']],contract=z['execution_contract'],direction=z['direction'],warning_ts=z['warning_ts'],entry_ts=e['entry_ts'],exit_ts=e['exit_ts'],exit_trading_day=z['exit_trading_day'],month=z['exit_trading_day'][:7],entry=fs[0]['price'],exit=fs[-1]['price'],gross_after_slippage=gross,commission=z['cost_repricing']['commission'],net=e['net_amount'],exit_reason=e['exit_reason'],event_key=z['event_key']))
 trades.sort(key=lambda z:(z['strategy'],z['slip'],z['exit_ts'],z['event_key']))
 result=dict(status='INCOMPLETE_ARCHIVE_STATISTICS_NOT_A_FULL_YEAR_BACKTEST',summary=summary,monthly=monthly,products=products,trades=trades,unresolved=unresolved,
  limitations=['原计划2025-10-27至2026-10-26完整年度回测未完成。','P2缺2025年10—12月完整原策略回放。','2026年7—9月部分品种行情起点不齐。','全部新增行情仍未补至2026-10-26，旧档案截至2026-10-23 11:05。','汇总金额和连亏仅适用于可用信号片段，不是完整年度结果。'],sources=sources)
 (OUT/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
 for name,rows in [('月度可用记录.csv',monthly),('品种可用记录.csv',products),('逐笔可用记录.csv',trades),('未确定交易.csv',unresolved)]:
  if not rows:continue
  with (OUT/name).open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,list(rows[0]));w.writeheader();w.writerows(rows)
 lines=['# B5与P2可用档案统计（未完成完整一年回测）','',*result['limitations'],'','固定每笔一手，扣2026-10-22冻结手续费及每边1 tick滑点；下表均为可用片段结果。P2和B5统计覆盖不同，不能据此比较完整一年收益。','','|策略|已知平仓笔数|可用片段净额/元|已知序列最大连亏/元|对应连亏笔数|未确定交易|','|---|---:|---:|---:|---:|---:|']
 for name in ['B5','P2']:
  z=summary[name+'/slip1'];lines.append(f"|{name}|{z['n']}|{z['net']:.2f}|{z['max_amount']['loss']:.2f}|{z['max_amount']['n']}|{z['unknown']}|")
 lines+=['','## 月度已知记录','', '|月份|B5笔数|B5净额/元|P2笔数|P2净额/元|覆盖问题|','|---|---:|---:|---:|---:|---|']
 for mo in MONTHS:
  a=next(z for z in monthly if z['strategy']=='B5' and z['slip']=='slip1' and z['month']==mo);b=next(z for z in monthly if z['strategy']=='P2' and z['slip']=='slip1' and z['month']==mo)
  notes=[]
  for z in [a,b]:
   if z['coverage']!='现有月份合约档案覆盖':notes.append(z['strategy']+'：'+z['coverage'])
  lines.append(f"|{mo}|{a['n']}|{a['net']:.2f}|{b['n'] if b['n'] is not None else '缺数据'}|{format(b['net'],'.2f') if b['net'] is not None else '缺数据'}|{'；'.join(notes)}|")
 lines+=['','## 分品种可用记录','', '|品种|B5笔数|B5净额/元|P2笔数|P2净额/元|','|---|---:|---:|---:|---:|']
 for sym in r.NAMES:
  a=next((z for z in products if z['strategy']=='B5' and z['slip']=='slip1' and z['symbol']==sym),None);b=next(z for z in products if z['strategy']=='P2' and z['slip']=='slip1' and z['symbol']==sym)
  lines.append(f"|{r.NAMES[sym]} {sym}|{a['n'] if a else '—'}|{format(a['net'],'.2f') if a else '—'}|{b['n']}|{b['net']:.2f}|")
 lines+=['','最大连亏按可用的已知平仓记录排序，净额<0连续相加，净额≥0中断。缺行情或未知交易可能改变真实全年序列，因此上述连亏不是完整年度最大连亏。','',
 '原始执行均绑定历史主力对应的实际月份合约；已有持仓使用原合约报价。当前阻塞：SDK行情握手持续失败，官方EDB免费接口对超过近一年的分钟数据返回403，无法取得原定暖机期数据。没有用当前主力、复权连续价格或零值补齐缺失月份。']
 (OUT/'可用档案统计_未完成全年.md').write_text('\n'.join(lines),encoding='utf-8')
 mapping()
 print(json.dumps({k:{a:v[a] for a in ['n','net','unknown','max_amount']} for k,v in summary.items()},ensure_ascii=False))
if __name__=='__main__':main()
