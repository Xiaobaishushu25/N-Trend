import json,collections,pathlib,csv,datetime as dt
p=pathlib.Path('research-runs/b5-p2-three-year-20261028')
data=collections.defaultdict(list)
for line in (p/'accepted.jsonl').open(encoding='utf8'):
 z=json.loads(line);data[z['case']].append(z['row'])
months=[];d=dt.date(2023,10,1)
while d<=dt.date(2026,10,1):
 months.append(d.strftime('%Y-%m'));d=dt.date(d.year+(d.month==12),d.month%12+1,1)
labels={'B5/slip1.0':'系统六品种 B5','P2/slip1':'P2'}
lines=['# 三年回测结果（主场景）','','区间：2023-10-30 至 2026-10-27，按平仓交易日归月，完整平仓一笔记一笔；盈亏为单手净额，元。','', '每边滑点一跳，按项目 2026-10-22 手续费快照计费，非历史逐日费率。主力切换使用库内映射，具体合约原始价格；持仓继续留在原合约。初始空仓且无此前预热数据。节假日由行情交易日间隔推断。','','## 每月净盈亏与平仓笔数','','| 月份 | 系统六品种 B5 盈亏 | 笔数 | P2 盈亏 | 笔数 |','|---|---:|---:|---:|---:|']
summary={}; monthly={}
for k in labels:
 rows=data[k]; closed=[r for r in rows if r['result']['evaluation']['status']=='closed' and '2023-10-30'<=r['exit_trading_day']<='2026-10-27'];closed.sort(key=lambda r:(r['result']['evaluation']['exit_ts'],r['event_key']))
 bymonth=collections.defaultdict(list);bysym=collections.defaultdict(list)
 for r in closed:bymonth[r['exit_trading_day'][:7]].append(r);bysym[r['symbol']].append(r)
 loss=[];best=[];acc=0;best_sum=0
 for r in closed:
  v=r['result']['evaluation']['net_amount']
  if v<0:
   loss.append(r);acc+=v
   if acc<best_sum:best_sum=acc;best=loss[:]
  else:loss=[];acc=0
 unknown=collections.Counter(r['result']['evaluation']['status'] for r in rows if r['result']['evaluation']['status'] in ('rollover','pending','unfinished','ambiguous','missing_data'))
 summary[k]=dict(closed=closed,bymonth=bymonth,bysym=bysym,best=best,best_sum=best_sum,unknown=unknown)
for m in months:
 cells=[]
 for k in labels:
  rs=summary[k]['bymonth'][m];cells.extend([f"{sum(r['result']['evaluation']['net_amount'] for r in rs):,.2f}",str(len(rs))])
 lines.append('| '+m+' | '+' | '.join(cells)+' |')
lines.extend(['','## 分品种及最大连亏',''])
for k,name in labels.items():
 z=summary[k];lines.extend([f'### {name}','','| 品种 | 平仓笔数 | 净盈亏（元） |','|---|---:|---:|'])
 import sys;sys.path.insert(0,'scripts');from backtest_b5_p2_year import NAMES,SIX
 syms=SIX if k.startswith('B5') else list(NAMES)
 for s in syms:
  rs=z['bysym'][s];lines.append(f"| {NAMES[s]} ({s}) | {len(rs)} | {sum(r['result']['evaluation']['net_amount'] for r in rs):,.2f} |")
 total=sum(r['result']['evaluation']['net_amount'] for r in z['closed']);lines.append(f"| 合计 | {len(z['closed'])} | {total:,.2f} |")
 b=z['best'];lines.extend(['',f"最大连续亏损：{abs(z['best_sum']):,.2f} 元，共 {len(b)} 笔；平仓时间 {b[0]['result']['evaluation']['exit_ts'] if b else '无'} 至 {b[-1]['result']['evaluation']['exit_ts'] if b else '无'}。按已知平仓时间及事件键排序，非负净额中断连亏。",f"无法确定盈亏的已接纳机会：{dict(z['unknown'])}。未知机会未计入盈亏与笔数，也未作为连亏中断，故连亏值仅适用于已知平仓序列。",''])
lines.extend(['## 数据限制','','行情覆盖审计不是交易所官方日历的逐日完整性证明；历史主力映射生效时点、历史合约规格和历史费率还未独立核验。K线无法确定同一分钟内的精确价格路径，回测中的不确定结果单独列示。三年前没有预热数据，起始期信号可能遗漏。',''])
(p/'report.md').write_text('\n'.join(lines),encoding='utf8')
print('\n'.join(lines[-15:]))

