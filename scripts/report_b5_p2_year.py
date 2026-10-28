"""Audit and report the fixed B5/P2 trailing-year replay, no tuning."""
import collections
import csv
import datetime as dt
import json
import math
import pathlib
import sys
sys.dont_write_bytecode=True
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
import backtest_b5_p2_year as r
RUN=r.RUN
UNKNOWN={'ambiguous','missing_data','rollover','unfinished','pending'}
MONTHS=['2025-10','2025-11','2025-12']+[f'2026-{m:02}' for m in range(1,11)]

def ev(row):return row['result']['evaluation']
def order(row):return ev(row)['exit_ts'],row['event_key']
def write(name,z):
    (RUN/name).write_text(json.dumps(z,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
def csvout(name,rows):
    if not rows:return
    with (RUN/name).open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,list(rows[0]));w.writeheader();w.writerows(rows)
def streaks(rows):
    current=[];worst=[];longest=[];cash=peak=dd=0.;peak_time=None;dd_start=None;dd_end=None
    for row in sorted(rows,key=order):
        e=ev(row);v=e['net_amount'];cash+=v
        if cash>peak:peak=cash;peak_time=e['exit_ts']
        if peak-cash>dd:dd=peak-cash;dd_start=peak_time;dd_end=e['exit_ts']
        if v<0:current.append(row)
        else:current=[]
        if sum(ev(z)['net_amount'] for z in current)<sum(ev(z)['net_amount'] for z in worst):worst=list(current)
        if len(current)>len(longest):longest=list(current)
    def compact(xs):
        return dict(n=len(xs),loss=-math.fsum(ev(z)['net_amount'] for z in xs),
            first_exit=ev(xs[0])['exit_ts'] if xs else None,last_exit=ev(xs[-1])['exit_ts'] if xs else None,
            keys=[z['event_key'] for z in xs])
    return dict(max_amount=compact(worst),max_count=compact(longest),realized_drawdown=dd,drawdown_start=dd_start,drawdown_end=dd_end)

def stats(rows):
    known=[z for z in rows if ev(z)['status']=='closed'];unknown=[z for z in rows if ev(z)['status'] in UNKNOWN]
    vals=[ev(z)['net_amount'] for z in known]
    return dict(n=len(known),net=math.fsum(vals),gross_after_slippage=math.fsum(z['cost_repricing']['gross_amount_after_slippage'] for z in known),
        commission=math.fsum(z['cost_repricing']['commission'] for z in known),wins=sum(v>0 for v in vals),losses=sum(v<0 for v in vals),
        win_rate=sum(v>0 for v in vals)/len(vals) if vals else None,unknown=len(unknown),unknown_statuses=dict(collections.Counter(ev(z)['status'] for z in unknown)),**streaks(known))

def run():
    cases=collections.defaultdict(list)
    for line in (RUN/'accepted.jsonl').open(encoding='utf-8'):
        z=json.loads(line);cases[z['case']].append(z['row'])
    summary={k:stats(v) for k,v in cases.items()};monthly=[];products=[];trades=[];unresolved=[];checks=collections.Counter();segments=r.R(RUN/'segments.json');dates=r.R(RUN/'trading-dates.json')
    # Independent exchange-weekday/holiday coverage audit, not a zero-trade test.
    holidays=r.R(RUN/'holidays.json')['windows'];expected=[];day=dt.date.fromisoformat(r.START)
    while day<=dt.date.fromisoformat(r.END):
        text=day.isoformat()
        closed=any(h['last_close'][:10]<text<h['reopen'][:10] for h in holidays)
        if day.weekday()<5 and not closed:expected.append(text)
        day+=dt.timedelta(days=1)
    coverage={};source_coverage=r.R(RUN/'data-audit.json')
    for sym,z in source_coverage.items():
        observed=z['day_sessions'];missing=sorted(set(expected)-observed.keys());extra=sorted(observed.keys()-set(expected))
        assert not missing and not extra,(sym,'incomplete trading dates',missing,extra)
        coverage[sym]=dict(expected_trading_days=len(expected),observed_trading_days=len(observed),
            day_minutes_short_of_225={d:n for d,n in observed.items() if n<225})
    write('independent-calendar-coverage.json',coverage)
    from reprice_signal_costs import TradingDates
    cal=TradingDates({});cal.dates=dates
    for case,rows in sorted(cases.items()):
        assert len({z['event_key'] for z in rows})==len(rows)
        st=summary[case];strategy,slip=case.split('/')
        for month in MONTHS:
            chosen=[z for z in rows if ev(z)['status']=='closed' and z['exit_trading_day'][:7]==month];v=stats(chosen)
            monthly.append(dict(strategy=strategy,slip=slip,month=month,n=v['n'],net=v['net'],gross_after_slippage=v['gross_after_slippage'],commission=v['commission']))
        for sym in r.SIX if strategy=='B5' else r.NAMES:
            v=stats([z for z in rows if z['symbol']==sym])
            products.append(dict(strategy=strategy,slip=slip,symbol=sym,name=r.NAMES[sym],n=v['n'],net=v['net'],unknown=v['unknown']))
        assert sum(z['n'] for z in monthly if z['strategy']==strategy and z['slip']==slip)==st['n']
        assert abs(math.fsum(z['net'] for z in monthly if z['strategy']==strategy and z['slip']==slip)-st['net'])<1e-6
        assert sum(z['n'] for z in products if z['strategy']==strategy and z['slip']==slip)==st['n']
        assert abs(math.fsum(z['net'] for z in products if z['strategy']==strategy and z['slip']==slip)-st['net'])<1e-6
        for row in rows:
            e=ev(row);sym=row['symbol'];sg=next(z for z in segments[sym] if z['first']<=row['warning_ts']<=z['last'])
            assert sg['underlying']==row['execution_contract'];checks['warning_actual_main_contract']+=1
            assert r.START<=cal.day(row['warning_ts'])<=r.END
            if e['status']!='closed':
                if e['status'] in UNKNOWN:unresolved.append(dict(strategy=strategy,slip=slip,symbol=sym,contract=row['execution_contract'],warning_ts=row['warning_ts'],entry_ts=e.get('entry_ts'),status=e['status'],event_key=row['event_key']))
                continue
            fills=row['result']['fills'];assert len(fills)==2 and fills[0]['volume']==fills[1]['volume']==1
            assert fills[0]['offset']=='open' and fills[1]['offset']=='close'
            assert row['warning_ts']<fills[0]['ts']<=fills[1]['ts']<=r.CUTOFF
            assert r.START<=cal.day(e['exit_ts'])<=r.END
            assert all(abs(f['price']/row['tick_size']-round(f['price']/row['tick_size']))<1e-6 for f in fills)
            sign=1 if row['direction']=='up' else -1
            gross=sign*(fills[1]['price']-fills[0]['price'])*row['multiplier']
            assert abs(gross-row['cost_repricing']['gross_amount_after_slippage'])<1e-6
            assert abs(gross-row['cost_repricing']['commission']-e['net_amount'])<1e-6
            checks['closed_fill_cash_tick_and_time']+=1
            trades.append(dict(strategy=strategy,slip=slip,symbol=sym,name=r.NAMES[sym],contract=row['execution_contract'],direction=row['direction'],warning_ts=row['warning_ts'],entry_ts=e['entry_ts'],exit_ts=e['exit_ts'],exit_trading_day=row['exit_trading_day'],month=row['exit_trading_day'][:7],entry=fills[0]['price'],exit=fills[1]['price'],gross_after_slippage=gross,commission=row['cost_repricing']['commission'],net=e['net_amount'],exit_reason=e['exit_reason'],event_key=row['event_key']))
    trades.sort(key=lambda z:(z['strategy'],z['slip'],z['exit_ts'],z['event_key']))
    write('summary.json',dict(summary=summary,monthly=monthly,products=products,trades=trades,unresolved=unresolved,checks=dict(checks)))
    csvout('月度盈亏与交易笔数.csv',monthly);csvout('分品种盈亏与交易笔数.csv',products);csvout('逐笔交易.csv',trades);csvout('未确定交易.csv',unresolved)
    # Detailed historical main contracts: retain within-month changes.
    mapping=[]
    table=r.R(RUN/'continuous-table.json')
    for sym in r.NAMES:
        for month in MONTHS:
            days=[d for d in dates if d[:7]==month and r.START<=d<=r.END]
            p=sym.rstrip('0123456789');ex=r.old.EXCH[p];entries=table[ex+'.'+(p if ex=='CZCE' else p.lower())];ds=[str(z[0]) for z in entries]
            groups=[]
            for day in days:
                code=entries[bisect_right(ds,day.replace('-',''))-1][1]
                if not groups or groups[-1]['contract']!=code:groups.append(dict(contract=code,first=day,last=day))
                else:groups[-1]['last']=day
            for z in groups:mapping.append(dict(month=month,symbol=sym,name=r.NAMES[sym],**z))
    csvout('每月主力合约及切换日期.csv',mapping)
    write('monthly-contracts.json',mapping)
    money=lambda x:f'{x:,.2f}'
    b=summary['B5/slip1'];p=summary['P2/slip1']
    lines=['# B5六品种与P2近一年回测','',f'区间：{r.START}—{r.END}，按交易日归属。2025年10月只含27—31日，2026年10月截至26日。2025年10月24日夜盘归27日；起点空仓，不继承区间前持仓。','',
        '每笔固定一手。主场景为每边1 tick不利滑点，金额已扣2026-10-22冻结手续费；SR往返2元。不是逐日历史经纪商费率。完整开仓—平仓计1笔；按平仓交易月份统计，跨月交易在平仓月确认。', '',
        '|策略|平仓笔数|净盈亏/元|手续费/元|净胜率|已知序列最大连亏/元|对应连亏笔数|已实现最大回撤/元|未确定交易|','|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for name,z in [('B5六品种',b),('P2原22品种',p)]:lines.append(f"|{name}|{z['n']}|{money(z['net'])}|{money(z['commission'])}|{z['win_rate']:.2%}|{money(z['max_amount']['loss'])}|{z['max_amount']['n']}|{money(z['realized_drawdown'])}|{z['unknown']}|")
    lines+=['','最大连亏按已知完整平仓交易的退出时间排序，同一时刻按稳定信号键排序；连续净亏损相加，净额≥0即中断，跨品种连续计数。最大连亏金额与最长连亏笔数分别计算。未确定交易没有可知退出损益，以下是已知平仓序列统计，不能断言是包含所有未知交易后的完整账户最大连亏。最大回撤按已实现净额曲线计算，不包含持仓浮动盈亏。','',
        '## 每月净盈亏与交易笔数','', '|平仓月份|B5笔数|B5净盈亏/元|P2笔数|P2净盈亏/元|','|---|---:|---:|---:|---:|']
    for month in MONTHS:
        a=next(z for z in monthly if z['strategy']=='B5' and z['slip']=='slip1' and z['month']==month);c=next(z for z in monthly if z['strategy']=='P2' and z['slip']=='slip1' and z['month']==month)
        lines.append(f"|{month}|{a['n']}|{money(a['net'])}|{c['n']}|{money(c['net'])}|")
    lines+=[f"|合计|{b['n']}|{money(b['net'])}|{p['n']}|{money(p['net'])}|",'', '## 各品种交易笔数与净盈亏','', '|品种|B5笔数|B5净盈亏/元|P2笔数|P2净盈亏/元|','|---|---:|---:|---:|---:|']
    for sym in r.NAMES:
        a=next((z for z in products if z['strategy']=='B5' and z['slip']=='slip1' and z['symbol']==sym),None);c=next(z for z in products if z['strategy']=='P2' and z['slip']=='slip1' and z['symbol']==sym)
        lines.append(f"|{r.NAMES[sym]} {sym}|{a['n'] if a else '—'}|{money(a['net']) if a else '—'}|{c['n']}|{money(c['net'])}|")
    lines+=['','## 最大连亏明细','', '|策略|按金额最大连亏|对应笔数|第一笔平仓|最后一笔平仓|最长连亏笔数|最长笔数段亏损/元|','|---|---:|---:|---|---|---:|---:|']
    for name,z in [('B5',b),('P2',p)]:
        a=z['max_amount'];c=z['max_count'];lines.append(f"|{name}|{money(a['loss'])}|{a['n']}|{a['first_exit']}|{a['last_exit']}|{c['n']}|{money(c['loss'])}|")
    lines+=['','## 每边2 tick成本对照','', '|策略|平仓笔数|净盈亏/元|已知序列最大连亏/元|未确定交易|','|---|---:|---:|---:|---:|']
    for name in ['B5','P2']:
        z=summary[name+'/slip2'];lines.append(f"|{name}|{z['n']}|{money(z['net'])}|{money(z['max_amount']['loss'])}|{z['unknown']}|")
    lines+=['','## 数据和方法','',
        '使用重新获取的天勤历史主力映射，以每个交易日当时主力的实际月份合约原价生成信号和模拟成交。同月内如有换月，按切换日改变新机会合约；已持仓继续用原月份合约。完整逐月合约表及切换日期见同目录CSV。不是用当前主力倒推全年，也不使用复权主连价格计算盈亏。', '',
        '在已有真实月份合约分钟档案之上，补取2025年9—12月、2026年7—9月原缺口及10月21—26日。所有22品种的日盘日期与分钟数均保存于data-audit.json；冲突及重叠比较另存merge文件。通过项目已有服务器和同一天勤账户进行普通TqBacktest/TqSim只读历史订阅，服务器不保存原始价格文件；本地按批次保存压缩缓存以支持断线续算。没有报单或修改生产配置。','',
        'B5采用冻结budget_body/sector2_counter_stops规则；P2采用冻结原N结构、原S1目标、评分3、风险10 tick、摩擦0.2R、风险/ATR14在[0.75,2.5]、净盈亏比至少1。两者都按整段时间线竞争同品种槽位，不按月清空占用；未知机会保留到该月份合约现有行情末尾。B5另保留不顺向行业同向最多2占用、同A已实现两次原stop退役。', '',
        '这是分钟OHLC研究执行模型，无法复刻真实盘口、排队、成交量容量和全部分钟内价格路径。不同品种每笔一手的资金风险不同，未指定本金或模拟保证金限制，因此只比较人民币净额，不计算账户收益率。已使用过的历史不能作为新的独立样本。', '',
        '[天勤回测及历史主力成员说明](https://doc.shinnytech.com/tqsdk/latest/usage/backtest.html)；[历史主力映射](https://files.shinnytech.com/continuous_table.json)。', '',
        '逐笔成交金额、滑点tick、先后时序、主力合约绑定，以及月度/品种/总额交叉核对均已执行。']
    (RUN/'回测报告.md').write_text('\n'.join(lines),encoding='utf-8')
    write('report-verification.json',dict(checks=dict(checks),monthly_and_product_totals_reconcile=True,main_contract_verified=True))
    print(json.dumps(summary,ensure_ascii=False,indent=2))

from bisect import bisect_right
if __name__=='__main__':run()
