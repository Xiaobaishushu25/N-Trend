"""Four-product day-session SMA research; frozen variants, actual-contract segments."""
import csv, json, sqlite3, hashlib
from collections import deque, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'reports/three_sma_multi_20261028'
DB=ROOT/'data/research_market_history.sqlite'
TZ=timezone(timedelta(hours=8))
PRODUCTS={'DCE.jd':10,'DCE.c':10,'DCE.eg':10,'DCE.eb':5}
START='2024-01-01'; END='2026-10-28'; RATE=0.00015
VARIANTS=('baseline','atr_entry','atr_entry_relaxed_exit')
PLAN={'period':[START,END], 'products':PRODUCTS,'variants':list(VARIANTS),
      'signal':'15m complete day bars, SMA 5/10/20; ATR20 arithmetic true range, includes day gaps',
      'entry':'baseline full ordering; candidates additionally abs(SMA5-SMA20)>=0.2 ATR20',
      'exit':'baseline and atr_entry exit on loss of full ordering or filter; relaxed exits on SMA5 crossing SMA10, no filter applied to exits',
      'execution':'next available nonzero-volume day minute open; 1 or 2 adverse ticks each side; zero-volume minutes cannot fill',
      'contract':'mapping effective calendar day applied at day session open; position closed on last available minute close before segment/year end, both legs charged; new contract warms from its own historical day bars, no adjusted continuous prices',
      'costs':'uniform 0.00015 notional each side hypothetical, tick 1; multipliers in products; not audited historical rates',
      'limitation':'daytime only, night omitted from indicators and fills; positions carry nights; split each year independently flat, no rollover linkage; no calendar or fee audit; previous research has used these data'}

def ns(s):return int(datetime.fromisoformat(s).replace(tzinfo=TZ).timestamp()*1e9)
def time_str(n):return datetime.fromtimestamp(n/1e9,TZ).strftime('%Y-%m-%d %H:%M')
def day(n):return datetime.fromtimestamp(n/1e9,TZ).strftime('%Y-%m-%d')
def daytime(n):
    d=datetime.fromtimestamp(n/1e9,TZ)
    return 9<=d.hour<15

def signals(bars, mode):
    prices=deque(maxlen=20); trs=deque(maxlen=20); old=None; state=0; result=[]
    for end,h,l,c in bars:
        tr=h-l if old is None else max(h-l,abs(h-old),abs(l-old))
        old=c; prices.append(c); trs.append(tr)
        if len(prices)<20: continue
        x=list(prices); a=sum(x[-5:])/5; m=sum(x[-10:])/10; z=sum(x)/20
        raw=1 if a>m>z else -1 if a<m<z else 0
        allowed=raw and abs(a-z)>=0.2*sum(trs)/20
        if mode=='baseline': target=raw
        elif mode=='atr_entry':target=raw if allowed else 0
        else:
            if state==1 and a<=m:state=0
            elif state==-1 and a>=m:state=0
            if state==0 and allowed:state=raw
            target=state
        result.append((end,target))
    return result

def replay(minutes,bars,mode,mult,slip):
    # Warm indicator only: skip position state from before this segment.
    first=minutes[0][0]
    prior=[x for x in bars if x[0]<=first][-20:]
    warm=prior[-19:] # first valid state is produced in the segment
    bars=warm+[x for x in bars if x[0]>first]
    events=iter(signals(bars,mode)); ev=next(events,None); pending=None
    pos=0; entry=None; cash=0.; equity=[]; trades=[]
    for t,o,c,v in minutes:
        while ev is not None and ev[0]<=t:
            pending=ev[1];ev=next(events,None)
        if pending is not None and v>0:
            target=pending;pending=None
            if target!=pos:
                if pos:
                    price=o-pos*slip;fee=price*mult*RATE
                    net=pos*(price-entry[1])*mult-entry[2]-fee
                    cash+=pos*(price-entry[1])*mult-fee
                    trades.append((entry[0],time_str(t),pos,round(net,4),'signal'))
                pos=target; entry=None
                if pos:
                    price=o+pos*slip;fee=price*mult*RATE;cash-=fee
                    entry=(time_str(t),price,fee)
        equity.append(cash+(pos*(c-entry[1])*mult if pos else 0))
    if pos:
        t,o,c,v=minutes[-1];price=c-pos*slip;fee=price*mult*RATE
        net=pos*(price-entry[1])*mult-entry[2]-fee
        cash+=pos*(price-entry[1])*mult-fee
        trades.append((entry[0],time_str(t+60_000_000_000),pos,round(net,4),'segment_end'))
        equity[-1]=cash
    return cash,trades,equity

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    path=OUT/'plan.json'
    if path.exists() and json.loads(path.read_text(encoding='utf8'))!=PLAN:raise ValueError('plan changed')
    path.write_text(json.dumps(PLAN,ensure_ascii=False,indent=2),encoding='utf8')
    db=sqlite3.connect(DB.resolve().as_uri()+'?mode=ro',uri=True)
    out=[]; ledger=[]; audit=[]; digest=hashlib.sha256()
    for product,mult in PRODUCTS.items():
        mappings=db.execute('select effective_date,contract from mappings where product=? and effective_date<? order by effective_date',(product,END)).fetchall()
        aggregates=defaultdict(lambda: {'pnl':0.,'trades':[],'equity':[],'segments':0})
        for i,(effective,contract) in enumerate(mappings):
            until=mappings[i+1][0] if i+1<len(mappings) else END
            if until<=START or effective>=END:continue
            lo=max(effective,START); hi=min(until,END)
            raw=db.execute('select start_ns,open,close,volume from bars where contract=? and period_seconds=60 and start_ns>=? and start_ns<? order by start_ns',(contract,ns(lo+' 00:00:00'),ns(hi+' 00:00:00'))).fetchall()
            raw=[x for x in raw if daytime(x[0])]
            if not raw:
                audit.append({'product':product,'contract':contract,'start':lo,'end_exclusive':hi,'minutes':0});continue
            warm=(date.fromisoformat(lo)-timedelta(days=30)).isoformat()
            bars=db.execute('select end_ns,high,low,close from bars where contract=? and period_seconds=900 and complete=1 and start_ns>=? and start_ns<? order by end_ns',(contract,ns(warm+' 00:00:00'),ns(hi+' 00:00:00'))).fetchall()
            bars=[x for x in bars if daytime(x[0]-900_000_000_000)]
            digest.update(repr((product,contract,raw,bars)).encode())
            for year in range(2024,2027):
                mm=[x for x in raw if day(x[0])[:4]==str(year)]
                if not mm:continue
                ee=ns(str(year+1)+'-01-01 00:00:00'); bb=[x for x in bars if x[0]<ee]
                audit.append({'product':product,'contract':contract,'year':year,'start':lo,'end_exclusive':hi,'minutes':len(mm),'first':time_str(mm[0][0]),'last':time_str(mm[-1][0]),'days':len({day(x[0]) for x in mm}),'warm_bars':len([x for x in bb if x[0]<=mm[0][0]])})
                for variant in VARIANTS:
                    for slip in (1,2):
                        pnl,ts,eq=replay(mm,bb,variant,mult,slip)
                        row=aggregates[year,variant,slip];off=row['pnl'];row['pnl']+=pnl;row['trades'].extend(ts);row['equity'].extend(off+e for e in eq);row['segments']+=1
                        for t in ts:ledger.append(dict(product=product,year=year,contract=contract,variant=variant,slip=slip,entry=t[0],exit=t[1],direction=t[2],net_pnl=t[3],reason=t[4]))
        for (year,variant,slip),v in aggregates.items():
            peak=0;dd=0
            for e in v['equity']:peak=max(peak,e);dd=max(dd,peak-e)
            ts=v['trades']; wins=[t[3] for t in ts if t[3]>0];loss=[t[3] for t in ts if t[3]<0]
            out.append(dict(product=product,year=year,variant=variant,slip=slip,trades=len(ts),net_pnl=round(v['pnl'],2),max_drawdown=round(dd,2),win_rate=round(len(wins)/len(ts),4) if ts else None,profit_factor=round(sum(wins)/-sum(loss),3) if loss else None,segments=v['segments']))
        print(product,'done',flush=True)
    db.close()
    for name,rows in [('results.csv',out),('trades.csv',ledger),('coverage.csv',audit)]:
        keys=list(dict.fromkeys(k for row in rows for k in row))
        with (OUT/name).open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=keys);w.writeheader();w.writerows(rows)
    (OUT/'input_sha256.txt').write_text(digest.hexdigest()+'\n',encoding='ascii')
    print('rows',len(out),'trades',len(ledger),'hash',digest.hexdigest())
if __name__=='__main__':main()
