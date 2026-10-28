"""Exploratory in-sample diagnostics; no deployment or parameter selection."""
import csv
import json
import sqlite3
from collections import defaultdict
import backtest_three_sma_15m as b


def make_signal(mode):
    target = 0
    def calc(x):
        nonlocal target
        if len(x) < 20:
            return 0
        a, m, slow = sum(x[-5:])/5, sum(x[-10:])/10, sum(x[-20:])/20
        raw = 1 if a > m > slow else -1 if a < m < slow else 0
        if mode == 'baseline':
            return raw
        if mode == 'slope':
            old = sum(x[-13:-3])/10
            return raw if raw and raw*(m-old)>0 else 0
        if mode == 'gap10':
            return raw if raw and abs(a-slow)>=10 else 0
        if mode == 'hold':
            if target == 1 and a <= m: target = 0
            elif target == -1 and a >= m: target = 0
            if target == 0: target = raw
            return target
        raise ValueError(mode)
    return calc

def main():
    db=sqlite3.connect(f'file:{b.DB.as_posix()}?mode=ro',uri=True)
    bars=db.execute('select end_ns,close from bars where contract=? and period_seconds=900 and complete=1 and end_ns<=? order by end_ns',(b.CONTRACT,b.ns(b.END))).fetchall()
    minutes=db.execute('select start_ns,open,close,volume from bars where contract=? and period_seconds=60 and start_ns>=? and start_ns<? order by start_ns',(b.CONTRACT,b.ns(b.START),b.ns(b.END))).fetchall()
    db.close()
    # Warm the averages but start each candidate's position state flat.
    bars = [x for x in bars if x[0] <= b.ns(b.START)][-19:] + [x for x in bars if x[0] > b.ns(b.START)]
    outputs={}
    original=b.signal
    for mode in ('baseline','slope','gap10','hold'):
        b.signal=make_signal(mode)
        stats,trades,equity=b.run(minutes,bars)
        splits={}
        for label,predicate in [('first',lambda t:t['exit_time']<'2026-08-24'),('second',lambda t:t['exit_time']>='2026-08-24')]:
            selected=[t for t in trades if predicate(t)]
            splits[label]={'trades':len(selected),'net_pnl':round(sum(t['net_pnl'] for t in selected),2)}
        wins=[t['net_pnl'] for t in trades if t['net_pnl']>0]
        losses=[t['net_pnl'] for t in trades if t['net_pnl']<0]
        fees=sum((t['entry_price']+t['exit_price'])*b.MULTIPLIER*b.FEE_RATE for t in trades)
        gross_fills=sum(t['direction']*(t['exit_price']-t['entry_price'])*b.MULTIPLIER for t in trades)
        outputs[mode]={'stats':stats,'splits_by_exit':splits,'fees':round(fees,2),'gross_after_slippage':round(gross_fills,2),
                       'gross_before_slippage':round(gross_fills+2*b.TICK*b.MULTIPLIER*len(trades),2),
                       'win_total':round(sum(wins),2),'loss_total':round(sum(losses),2),
                       'direction':{str(d):{'trades':len([t for t in trades if t['direction']==d]),'pnl':round(sum(t['net_pnl'] for t in trades if t['direction']==d),2)} for d in (1,-1)}}
    b.signal=original
    out=b.OUT/'diagnostics.json'
    out.write_text(json.dumps(outputs,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(outputs,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
