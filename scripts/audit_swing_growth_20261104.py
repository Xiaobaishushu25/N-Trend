"""Independent fill reconciliation and full chronological1m account replay."""
import collections
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import NamedTuple

import pandas as pd

import research_swing_growth_20261104 as g
import research_swing_year_20261104 as b
import research_swing_expansion_20261104 as old


class MinuteBar(NamedTuple):
    start_ns:int
    end_ns:int
    open:float
    high:float
    low:float
    close:float
    volume:int
    day:str
    contract:str

    def __getitem__(self,key):
        return getattr(self,key) if isinstance(key,str) else tuple.__getitem__(self,key)


def minute_tape(tape):
    membership={};bounds={};decision=collections.defaultdict(set)
    for _,batch in tape:
        for sym,row in batch.items():
            code=row['contract'];membership[(code,row['start_ns'])]=(sym,row['day'])
            lo,hi=bounds.get(code,(row['start_ns'],row['end_ns']))
            bounds[code]=(min(lo,row['start_ns']),max(hi,row['end_ns']));decision[sym].add(row['end_ns'])
    conn=sqlite3.connect(f'file:{b.DB.as_posix()}?mode=ro',uri=True);out=collections.defaultdict(dict);digest=hashlib.sha256();count=0
    step=900_000_000_000
    for code,(lo,hi) in sorted(bounds.items()):
        rows=conn.execute('SELECT start_ns,end_ns,open,high,low,close,volume FROM bars WHERE contract=? AND period_seconds=60 AND start_ns>=? AND start_ns<? ORDER BY start_ns',(code,lo,hi)).fetchall()
        digest.update(code.encode());digest.update(json.dumps(rows,separators=(',',':')).encode())
        for row in rows:
            key=(code,row[0]//step*step)
            if key not in membership:continue
            sym,day=membership[key];out[row[0]][sym]=MinuteBar(*row,day,code);count+=1
    conn.close()
    expected=sum(len(batch) for _,batch in tape)*15
    assert count==expected,(count,expected)
    g.write('minute-data-audit.json',dict(selected_minutes=count,expected_constituents=expected,input_sha256=digest.hexdigest(),steps=len(out)))
    return sorted(out.items()),dict(decision)


def reconcile(ts,eq):
    errors=[]
    for t in ts:
        mult=b.SPECS[t['symbol']][0];direction=t['side']
        opens=sum(f['lots'] for f in t['fills'] if f['kind'] in ['open','add']);closes=sum(f['lots'] for f in t['fills'] if f['kind']=='close')
        assert opens==closes==t['lots']
        net=sum((direction if f['kind']=='close' else -direction)*f['price']*mult*f['lots']-f['fee'] for f in t['fills'])
        errors.append(abs(net-t['net']));assert abs(net-t['net'])<1e-6
    assert abs(sum(t['net'] for t in ts)-eq.iloc[-1])<1e-6
    return dict(trades=len(ts),fill_ledger_max_error=max(errors,default=0),account_error=abs(sum(t['net'] for t in ts)-eq.iloc[-1]))


def collateral_check(tape,ts,eq,margin=.2):
    opened=[];closed=[]
    for t in ts:
        for f in t['fills']:
            (closed if f['kind']=='close' else opened).append((f['ns'],t['symbol'],f['lots']))
    opened.sort();closed.sort();i=j=0;qty=collections.Counter();marks={};minimum=50000.;peak_fraction=0
    for time,batch in tape:
        end=max(r['end_ns'] for r in batch.values())
        while i<len(opened) and opened[i][0]<end:
            _,sym,q=opened[i];qty[sym]+=q;i+=1
        while j<len(closed) and closed[j][0]<=end:
            _,sym,q=closed[j];qty[sym]-=q;j+=1
        for sym,row in batch.items():marks[sym]=row['close']
        used=sum(q*marks[sym]*b.SPECS[sym][0]*margin for sym,q in qty.items() if q>0)
        equity=50000+float(eq.loc[end]);minimum=min(minimum,equity-used);peak_fraction=max(peak_fraction,used/equity)
    return dict(minimum_equity_less_margin=round(minimum,2),maximum_close_margin_fraction=round(peak_fraction,4),margin_deficit_observed=minimum<0)


def main():
    tape,signals,opens,atrs=g.load();mini,decision=minute_tape(tape)
    configs={c['id']:c for c in g.configurations()};results={};checks={}
    # Selection is a diagnostic allocation shortlist, not ranked on test profit.
    cases=[('A_scale_1250_m80','base',{}),('A_scale_1250_m80','slip3',dict(slip=3)),('A_scale_1500_m80','base',{}),('A_scale_1500_m80','slip3',dict(slip=3)),('A_scale_750_m60','base',{}),('A_scale_750_m60','slip3',dict(slip=3)),('A_pyramid','base',{}),('B_target2_guard3','base',{})]
    for name,label,kw in cases:
        ts,eq,lim=g.run(mini,signals,opens,atrs,configs[name],decision_ends=decision,**kw)
        checks[name+'/'+label]=reconcile(ts,eq)
        result=old.evaluate(ts,eq,lim);result['collateral_check']=collateral_check(mini,ts,eq)
        results.setdefault(name,{})[label]=result
        pd.DataFrame([{k:v for k,v in t.items() if k!='fills'} for t in ts]).to_csv(g.OUT/f'minute-{name}-{label}-trades.csv',index=False)
        (g.OUT/f'minute-{name}-{label}-fills.json').write_text(json.dumps(ts,ensure_ascii=False,indent=2),encoding='utf8')
        eq.to_csv(g.OUT/f'minute-{name}-{label}-equity.csv')
        g.write('minute-replay-progress.json',results)
        print('MINUTE',name,label,result['full']['net'],result['full']['max_drawdown'],flush=True)
    g.write('minute-replay.json',results);g.write('fill-reconciliation.json',checks)
    g.write('audit-implementation.json',{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__),Path(g.__file__),Path(old.__file__),Path(b.__file__),b.FEES]})


if __name__=='__main__':main()
