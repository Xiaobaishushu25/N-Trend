"""Frozen-rule cost pressure, minute execution audit and descriptive bootstrap."""
import hashlib
import json
import math
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

import research_swing_expansion_20261104 as x
import research_swing_year_20261104 as base


def day_for(ns):
    t=pd.Timestamp(int(ns),tz='UTC').tz_convert('Asia/Shanghai');day=t.strftime('%Y-%m-%d')
    j=__import__('bisect').bisect_right(base.CALENDAR,day) if t.hour>=18 else __import__('bisect').bisect_left(base.CALENDAR,day)
    return base.CALENDAR[j] if j<len(base.CALENDAR) else day


def minute_audit(trades,delay=0,slip=1):
    c=sqlite3.connect(f'file:{base.DB.as_posix()}?mode=ro',uri=True)
    ledger=[];paths={};digest=hashlib.sha256()
    for index,t in enumerate(trades):
        mult,tick=base.SPECS[t['symbol']];side=t['side'];start=int(t['entry_ns'])+delay*60_000_000_000;end=int(t['exit_ns'])
        rows=c.execute('SELECT start_ns,end_ns,open,high,low,close,volume FROM bars WHERE contract=? AND period_seconds=60 AND start_ns>=? AND start_ns<=? ORDER BY start_ns',(t['contract'],start,end)).fetchall()
        digest.update(json.dumps([t['contract'],rows],separators=(',',':')).encode())
        rows=[r for r in rows if r[0]<end or t['reason'] in ['time','roll','end','gap_stop','gap_target']]
        eligible=[r for r in rows if r[6]>0]
        if not eligible:raise RuntimeError('No tradable minute at audit entry')
        first=eligible[0];entry_time=first[0];entry=first[2]+side*slip*tick
        ef=base.fee(x.FEES[t['symbol']],entry,mult,'open')
        path=[[int(entry_time),-ef,day_for(entry_time)]];result=None
        for ns,en,op,h,l,cl,v in rows:
            if ns<entry_time:continue
            forced=ns>=end and t['reason'] in ['time','roll','gap_stop','gap_target']
            stophit=l<=t['stop'] if side>0 else h>=t['stop']
            # Frozen accepted cohort and baseline order levels/penetration;
            # cost-only stress changes fills, not the frozen trigger threshold.
            target=t['target'];targethit=h>=target+tick if side>0 else l<=target-tick
            if forced:
                px=op;reason=t['reason'];exit_time=ns
            elif side*(op-t['stop'])<=0:
                px=op;reason='gap_stop';exit_time=ns
            elif side*(op-target)>=tick:
                px=target;reason='gap_target';exit_time=ns
            elif stophit:
                px=t['stop'];reason='stop';exit_time=en
            elif targethit:
                px=target;reason='target';exit_time=en
            elif t['reason']=='end' and en>=end:
                px=cl;reason='end';exit_time=en
            else:
                path.append([int(en),side*(cl-entry)*mult-ef,day_for(ns)])
                continue
            fill=px-side*slip*tick;day=day_for(ns)
            ff=base.fee(x.FEES[t['symbol']],fill,mult,'close_today' if day==t['entry_day'] else 'close_yesterday')
            net=side*(fill-entry)*mult-ef-ff
            path.append([int(exit_time),net,day]);result=dict(index=index,symbol=t['symbol'],contract=t['contract'],entry_ns=entry_time,exit_ns=exit_time,exit_day=day,entry_price=entry,exit_price=fill,net=net,r=net/t['risk_cash'],baseline_net=t['net'],delta=net-t['net'],reason=reason,baseline_reason=t['reason'],entry_shift_minutes=(entry_time-t['entry_ns'])/60e9)
            break
        if result is None:raise RuntimeError(f'No minute exit matched baseline horizon {index}: {t}')
        ledger.append(result);paths[str(index)]=path
    c.close()
    curve=base.curve_frame(paths)
    assert abs(curve.iloc[-1]-sum(t['net'] for t in ledger))<1e-6
    stats=dict(full=base.metrics(curve,ledger,base.START,base.END),train=base.metrics(curve,ledger,base.START,base.TRAIN_END),validation=base.metrics(curve,ledger,base.TRAIN_END,base.VALID_END),test=base.metrics(curve,ledger,base.VALID_END,base.END),changed_trades=sum(abs(t['delta'])>1e-6 for t in ledger),maximum_entry_shift_minutes=max(t['entry_shift_minutes'] for t in ledger),data_sha256=digest.hexdigest())
    return ledger,curve,stats


def bootstrap(curve):
    # Descriptive uncertainty only. Selection/multiple-testing bias is not removed.
    dates=pd.to_datetime(curve.index,utc=True).tz_convert('Asia/Shanghai')
    weekly=pd.Series(curve.values,index=dates).resample('W-FRI').last().ffill()
    changes=weekly.diff();changes.iloc[0]=weekly.iloc[0]
    values=changes.to_numpy();rng=np.random.default_rng(20261104);totals=[]
    for _ in range(4000):
        starts=rng.integers(0,len(values),size=math.ceil(len(values)/4))
        sample=np.concatenate([values[(np.arange(4)+start)%len(values)] for start in starts])[:len(values)]
        totals.append(float(sample.sum()))
    return dict(block_weeks=4,samples=4000,descriptive_total_95_interval=[round(float(v),2) for v in np.quantile(totals,[.025,.975])],fraction_nonpositive=round(float(np.mean(np.array(totals)<=0)),4),selection_adjusted=False)


def main():
    comp=json.loads((x.OUT/'comparison.json').read_text(encoding='utf8'))
    models={m['model']['id']:m['model'] for m in comp['results']}
    candidates=[comp['selected'],models['oi_drive_15_32_target3_r500']]
    # The secondary model is explicitly a mechanism comparison, not a replacement
    # selected on its final test profit. Both met original train/validation gates.
    tape,signals,old_open,atrs=x.load();report={}
    for model in candidates:
        name=model['id'];scenarios={};basets=None;baseeq=None
        for label,kw in [('base',{}),('slip2',dict(slip=2,fixed_plan=True)),('slip3',dict(slip=3,fixed_plan=True)),('fee2',dict(fee_scale=2,fixed_plan=True)),('margin20',dict(margin=.2)),('reverse',dict(symbol_order='reverse'))]:
            ts,eq,limits=x.run(tape,signals,old_open,atrs,model,**kw)
            scenarios[label]=x.evaluate(ts,eq,limits)
            pd.DataFrame(ts).to_csv(x.OUT/f'audit-{name}-{label}-trades.csv',index=False)
            eq.to_csv(x.OUT/f'audit-{name}-{label}-equity.csv')
            if label=='base':basets,baseeq=ts,eq
        original=next(r for r in comp['results'] if r['model']['id']==name)
        assert abs(original['full']['net']-scenarios['base']['full']['net'])<.011
        detail={}
        for label,delay,slip in [('minute',0,1),('minute_slip3',0,3),('minute_delay1',1,1)]:
            ledger,curve,stats=minute_audit(basets,delay,slip)
            pd.DataFrame(ledger).to_csv(x.OUT/f'{name}-{label}-ledger.csv',index=False)
            curve.to_csv(x.OUT/f'{name}-{label}-equity.csv')
            detail[label]=stats
            if label=='minute':detail['bootstrap']=bootstrap(curve)
        positives=sorted([t['net'] for t in basets if t['net']>0],reverse=True)
        pnl=sum(t['net'] for t in basets)
        bysym=scenarios['base']['by_product'];top=max(bysym,key=bysym.get)
        filtered=[(time,{sym:r for sym,r in batch.items() if sym!=top}) for time,batch in tape]
        filtered=[(time,batch) for time,batch in filtered if batch]
        ts,eq,lim=x.run(filtered,signals,old_open,atrs,model)
        report[name]=dict(model=model,scenarios=scenarios,minute_audits=detail,concentration=dict(remove_biggest_trade=round(pnl-positives[0],2),remove_top3_winning_trades=round(pnl-sum(positives[:3]),2),top_product=top,remove_top_product_account_replay=x.evaluate(ts,eq,lim),direction={str(side):round(sum(t['net'] for t in basets if t['side']==side),2) for side in [1,-1]}))
        x.write('robustness-progress.json',report)
        print('AUDITED',name,scenarios['base']['full']['net'],detail['minute']['full']['net'],flush=True)
    x.write('robustness.json',report)
    x.write('audit-implementation.json',{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__),Path(x.__file__),Path(base.__file__),base.FEES]})


if __name__=='__main__':main()
