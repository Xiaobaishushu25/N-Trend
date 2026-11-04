"""Third round: new mechanisms, integrated allocation and profitable pyramids.

20% notional margin; capital CNY50k; original A/B snapshots untouched.
"""
import collections
import hashlib
import json
import math
import pickle
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

import research_swing_expansion_20261104 as old
import research_swing_year_20261104 as b

ROOT=b.ROOT
OUT=ROOT/'reports/swing-growth-20261104'
STREAMS={'A':('pullback_end',60,32),'B':('oi_drive',15,32),
         'C':('oi_pullback',15,32),'D':('oi_retest',15,32),
         'E':('liquidation_reclaim',15,32),'F':('impulse',15,32),
         'G':('oi_pullback',60,32)}


def write(name,value):
    (OUT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf8')


def new_signal(row,family,n,tick):
    if family in old.FAMILIES:return old.signal(row,family,n,tick)
    parent={'oi_pullback':'pullback_end','oi_retest':'retest','liquidation_reclaim':'failed_auction'}
    if family in parent:
        sig=old.signal(row,parent[family],n,tick)
        if sig is None:return None
        oi=row[f'oi{n}']
        if family=='liquidation_reclaim':return sig if oi<-.005 else None
        return sig if oi>.01 else None
    if family=='impulse':
        c,o,h,l,a=row['close'],row['open'],row['high'],row['low'],row['atr']
        if not np.isfinite(row['daily_fast']) or not np.isfinite(a) or a<=0 or h-l<1.5*a:return None
        if row['volume']<=1.5*row['volbase'] or row[f'oi{n}']<=.01:return None
        direction=1 if row['daily_fast']>row['daily_slow'] else -1
        side=1 if direction==1 and c>row[f'h{n}'] and c>o and (c-l)/(h-l)>.8 else -1 if direction==-1 and c<row[f'l{n}'] and c<o and (h-c)/(h-l)>.8 else 0
        if not side:return None
        stop=math.floor((row['sl']-tick)/tick)*tick if side>0 else math.ceil((row['sh']+tick)/tick)*tick
        return dict(side=side,stop=stop,atr=a,signal_close=c,signal_ns=int(row['end_ns']))
    raise ValueError(family)


def load():
    """Cache is local trusted derived data, keyed by input and implementation."""
    cache=OUT/'inputs.pkl'
    identity=dict(script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),old_sha256=hashlib.sha256(Path(old.__file__).read_bytes()).hexdigest(),db_bytes=b.DB.stat().st_size,db_mtime_ns=b.DB.stat().st_mtime_ns)
    if cache.exists() and (OUT/'cache-identity.json').exists() and json.loads((OUT/'cache-identity.json').read_text())==identity:
        with cache.open('rb') as f:data=pickle.load(f)
        b.CALENDAR=data[-1];return data[:-1]
    frames,raws,dates,audit,digest=b.load();signals=collections.defaultdict(dict);atrs={};oi_hash=hashlib.sha256()
    conn=sqlite3.connect(f'file:{b.DB.as_posix()}?mode=ro',uri=True)
    for code,raw in raws.items():
        oi=pd.read_sql_query('SELECT start_ns,close_oi AS oi FROM bars WHERE contract=? AND period_seconds=900 AND start_ns>=? AND start_ns<=? ORDER BY start_ns',conn,params=(code,int(raw.start_ns.min()),int(raw.start_ns.max())))
        oi_hash.update(code.encode());oi_hash.update(oi.to_csv(index=False).encode())
        z=raw.reset_index(drop=True).merge(oi,on='start_ns',how='left',validate='one_to_one')
        sym=''.join(ch for ch in code.split('.')[1] if ch.isalpha()).upper();tick=b.SPECS[sym][1]
        for tf in [15,60]:
            ff=old.feature_frame(z,tf)
            atrs[(code,tf)]=dict(zip(ff.end_ns.astype(int),ff.atr))
            for row in ff.to_dict('records'):
                for stream,(family,period,n) in STREAMS.items():
                    if period!=tf:continue
                    sig=new_signal(row,family,n,tick)
                    if sig:
                        strength=abs(row['daily_fast']-row['daily_slow'])/max(row['atr']*math.sqrt(15/tf),tick)
                        signals[(code,stream)][sig['signal_ns']]={**sig,'contract':code,'stream':stream,'tf':tf,'eff':float(row['daily_eff']),'strength':float(strength),'oi_change':float(row[f'oi{n}'])}
    conn.close()
    tape=collections.defaultdict(dict)
    for sym,z in frames.items():
        for row in z.loc[z.day>=b.START,['start_ns','end_ns','day','contract','open','high','low','close','volume']].to_dict('records'):
            tape[int(row['start_ns'])][sym]=row
    tape=sorted(tape.items());opens={code:dict(zip(z.start_ns.astype(int),z.open)) for code,z in raws.items()}
    write('data-audit.json',dict(base_rows_sha256=digest,oi_rows_sha256=oi_hash.hexdigest(),coverage=audit,zero_volume_selected_bars=sum(row['volume']<=0 for _,batch in tape for row in batch.values()),signal_counts={stream:sum(len(v) for (code,st),v in signals.items() if st==stream) for stream in STREAMS}))
    data=(tape,dict(signals),opens,atrs,dates)
    with cache.open('wb') as f:pickle.dump(data,f,protocol=5)
    write('cache-identity.json',identity)
    return data[:-1]


def configurations():
    configs=[]
    def add(name,streams,**kw):
        configs.append(dict(id=name,streams=streams,gate_risk=kw.pop('gate_risk',500),trade_budget=kw.pop('trade_budget',500),max_lots=kw.pop('max_lots',1),total_risk=kw.pop('total_risk',2000),margin_cap=kw.pop('margin_cap',.6),target=kw.pop('target',3.),exit=kw.pop('exit','fixed'),guard_ticks=kw.pop('guard_ticks',1),filter=kw.pop('filter','none'),priority=kw.pop('priority','order'),pyramid=kw.pop('pyramid',False),**kw))
    add('A_original_20margin',['A'],gate_risk=250,trade_budget=250,total_risk=1000,margin_cap=.4)
    add('B_original_20margin',['B'],total_risk=1000,margin_cap=.4)
    # Allocation controls keep A's original opportunity qualification fixed.
    for budget in [500,750,1000]:
        for cap in [.4,.6]:add(f'A_scale_{budget}_m{int(cap*100)}',['A'],gate_risk=250,trade_budget=budget,max_lots=4,total_risk=2000,margin_cap=cap)
    # Added after user explicitly raised acceptable account drawdown to20%.
    for budget in [1250,1500]:
        for cap in [.6,.8]:add(f'A_scale_{budget}_m{int(cap*100)}',['A'],gate_risk=250,trade_budget=budget,max_lots=6,total_risk=3000,margin_cap=cap)
    for stream in ['B','C','D','E','F','G']:
        for target in [2.,3.,5.]:
            for guard in [1,3]:add(f'{stream}_target{int(target)}_guard{guard}',[stream],target=target,guard_ticks=guard)
    # Complementary mechanisms compete in a single account, never add curves.
    for streams in [['A','B'],['A','C'],['A','D'],['A','G'],['A','B','E']]:
        for filt in ['none','efficient']:
            for guard in [1,3]:add('_'.join(streams)+f'_{filt}_guard{guard}',streams,filter=filt,guard_ticks=guard,priority='strength')
    for stream in ['A','B','C','D','G']:
        for mode in ['runner','pyramid']:
            add(f'{stream}_{mode}',[stream],gate_risk=250 if stream=='A' else 500,trade_budget=500,total_risk=2000,max_lots=2,exit='runner' if mode=='runner' else 'fixed',pyramid=mode=='pyramid',guard_ticks=1)
    return configs


def run(tape,signals,opens,atrs,cfg,slip=1,fee_scale=1.,margin=.2,reverse=False,decision_ends=None):
    cash=50000.;pos={};last={};cool={};trades=[];curve=[];reject=collections.Counter();peakmargin=0.;maxlots=0;adds=0
    # Gate costs are predeclared and FIXED across actual cost pressure scenarios.
    plan_slip=cfg['guard_ticks']
    def fees(sym,px,kind,scale=fee_scale):return scale*b.fee(old.FEES[sym],px,b.SPECS[sym][0],kind)
    def equity():return cash+sum(p['side']*(last[s]-p['entry_price'])*b.SPECS[s][0]*p['lots'] for s,p in pos.items())
    def margin_used():return sum(last[s]*b.SPECS[s][0]*p['lots']*margin for s,p in pos.items())
    def committed_risk():return sum(p['planned_risk']*p['lots'] for p in pos.values())
    def close(sym,px,ns,day,reason,qty=None):
        nonlocal cash
        p=pos[sym];q=qty or p['lots'];mult,tick=b.SPECS[sym];fill=px-p['side']*slip*tick
        fee=fees(sym,fill,'close_today' if day==p['entry_day'] else 'close_yesterday')
        gross=p['side']*(fill-p['entry_price'])*mult
        cash+=(gross-fee)*q
        p['fills'].append(dict(kind='close',ns=int(ns),price=fill,lots=q,fee=fee*q))
        row={**p,'symbol':sym,'lots':q,'exit_ns':int(ns),'exit_day':day,'exit_price':fill,'net':(gross-fee-p['entry_fee'])*q,'r':(gross-fee-p['entry_fee'])/p['risk_cash'],'reason':reason,'hold_hours':(ns-p['entry_ns'])/3.6e12}
        trades.append(row);p['lots']-=q
        if p['lots']==0:del pos[sym];cool[sym]=int(ns)+3600_000_000_000
    for time,batch in tape:
        for sym,row in batch.items():
            last[sym]=row['open']
            if sym not in pos:continue
            p=pos[sym];op=row['open'];side=p['side'];tick=b.SPECS[sym][1]
            if p['contract']!=row['contract']:
                if time not in opens[p['contract']]:raise RuntimeError('Missing roll fill')
                close(sym,opens[p['contract']][time],time,row['day'],'roll')
            elif side*(op-p['stop'])<=0:close(sym,op,time,row['day'],'gap_stop')
            elif p['target'] is not None and side*(op-p['target'])>=slip*tick:close(sym,p['target'],time,row['day'],'gap_target')
            elif p['exit_next']:close(sym,op,time,row['day'],'time')
        # A profitable pyramid is queued ONLY by the prior closed bar.
        if cfg['pyramid']:
            for sym,row in batch.items():
                if sym not in pos:continue
                p=pos[sym]
                if not p['add_pending']:continue
                p['add_pending']=False
                mult,tick=b.SPECS[sym];fill=row['open']+p['side']*slip*tick
                risk=p['side']*(fill-p['stop'])*mult+slip*tick*mult+fees(sym,fill,'open')+fees(sym,p['stop'],'close_today')
                if risk<=0 or risk>cfg['trade_budget'] or p['lots']>=3:reject['pyramid_risk']+=1;continue
                used=margin_used();eq=equity()
                if used+fill*mult*margin>eq*cfg['margin_cap']:reject['pyramid_margin']+=1;continue
                if committed_risk()+risk>cfg['total_risk']:reject['pyramid_total_risk']+=1;continue
                ef=fees(sym,fill,'open');q=p['lots'];cash-=ef
                p['entry_price']=(p['entry_price']*q+fill)/(q+1)
                p['entry_fee']=(p['entry_fee']*q+ef)/(q+1)
                p['planned_risk']=(p['planned_risk']*q+risk)/(q+1)
                p['fills'].append(dict(kind='add',ns=int(time),price=fill,lots=1,fee=ef))
                p['lots']+=1;p['added']=True;adds+=1;peakmargin=max(peakmargin,used+fill*mult*margin)
        candidates=[]
        for sym,row in batch.items():
            if sym in pos or cool.get(sym,0)>time:continue
            for order,stream in enumerate(cfg['streams']):
                sig=signals.get((row['contract'],stream),{}).get(time)
                if sig:
                    if cfg['filter']=='efficient' and stream!='E' and sig['eff']<.25:reject['regime']+=1;continue
                    candidates.append((sym,row,sig,order))
        candidates.sort(key=lambda t:(-t[2]['strength'] if cfg['priority']=='strength' else t[3],t[0]),reverse=reverse)
        for sym,row,sig,order in candidates:
            if sym in pos:continue
            reject['signals']+=1;mult,tick=b.SPECS[sym];side=sig['side']
            gate=250 if sig['stream']=='A' else cfg['gate_risk']
            planfill=row['open']+side*plan_slip*tick;dist=side*(planfill-sig['stop']);orig=side*(sig['signal_close']-sig['stop'])
            if dist<4*tick or dist>1.5*max(orig,4*tick):reject['gap_distance']+=1;continue
            ef0=fees(sym,planfill,'open',1.);sf0=max(fees(sym,sig['stop'],'close_today',1.),fees(sym,sig['stop'],'close_yesterday',1.))
            planrisk=dist*mult+plan_slip*tick*mult+ef0+sf0
            if planrisk>gate:reject['gate_risk']+=1;continue
            if (2*plan_slip*tick*mult+ef0+sf0)/(dist*mult)>.15:reject['friction']+=1;continue
            fill=row['open']+side*slip*tick;actualdist=side*(fill-sig['stop'])
            if actualdist<=0:reject['bad_fill']+=1;continue
            used=margin_used();eq=equity();one=fill*mult*margin
            qty=min(cfg['max_lots'],int(cfg['trade_budget']//planrisk),int(max(0,cfg['total_risk']-committed_risk())//planrisk),int(max(0,min(eq*cfg['margin_cap']-used,eq-used))//one))
            if qty<1:reject['capital_or_risk']+=1;continue
            if len(pos)>=4 or sum(b.SECTORS[s]==b.SECTORS[sym] for s in pos)>=2:reject['concurrency']+=1;continue
            ef=fees(sym,fill,'open');cash-=ef*qty
            target=None if cfg['exit']=='runner' else fill+side*cfg['target']*actualdist
            pos[sym]=dict(stream=sig['stream'],tf=sig['tf'],contract=row['contract'],entry_ns=int(time),entry_day=row['day'],entry_price=fill,entry_fee=ef,side=side,stop=sig['stop'],target=target,risk_cash=actualdist*mult,planned_risk=planrisk,distance=actualdist,lots=qty,initial_lots=qty,bars=0,exit_next=False,atr=sig['atr'],added=False,add_pending=False,fills=[dict(kind='open',ns=int(time),price=fill,lots=qty,fee=ef*qty)])
            peakmargin=max(peakmargin,used+qty*one);reject['entries']+=1
        maxlots=max(maxlots,sum(p['lots'] for p in pos.values()))
        for sym,row in batch.items():
            last[sym]=row['close']
            if sym not in pos:continue
            p=pos[sym];side=p['side'];stop=p['stop'];target=p['target'];tick=b.SPECS[sym][1];end=int(row['end_ns'])
            if row['low']<=stop if side>0 else row['high']>=stop:
                close(sym,stop,end,row['day'],'stop');continue
            if target is not None and (row['high']>=target+slip*tick if side>0 else row['low']<=target-slip*tick):
                close(sym,target,end,row['day'],'target');continue
            # Minute verification executes stops continuously, but counts time
            # and changes protective orders only at the original15m closes.
            if decision_ends is not None and end not in decision_ends[sym]:continue
            p['bars']+=1
            at=atrs.get((p['contract'],p['tf']),{}).get(end)
            if at is not None and np.isfinite(at):p['atr']=at
            gain=side*(row['close']-p['entry_price'])
            if cfg['exit']=='runner' and gain>=2*p['distance']:
                new=row['close']-side*math.ceil(3*p['atr']/tick)*tick
                p['stop']=max(stop,new) if side>0 else min(stop,new)
            if cfg['pyramid'] and not p['added'] and gain>=1.5*p['distance']:
                # Lock half of original R before requesting one extra lot;
                # updated stop starts NEXT bar and includes gap execution.
                lock=p['entry_price']+side*math.floor(.5*p['distance']/tick)*tick
                p['stop']=max(stop,lock) if side>0 else min(stop,lock)
                p['add_pending']=True
            if p['bars']>=192:p['exit_next']=True
        maxlots=max(maxlots,sum(p['lots'] for p in pos.values()))
        curve.append([max(int(r['end_ns']) for r in batch.values()),equity()-50000.])
    for sym in list(pos):
        row=next(batch[sym] for _,batch in reversed(tape) if sym in batch)
        close(sym,row['close'],row['end_ns'],row['day'],'end')
    curve[-1][1]=cash-50000.
    eq=pd.DataFrame(curve,columns=['ns','net']).drop_duplicates('ns',keep='last').set_index('ns').net
    assert abs(sum(t['net'] for t in trades)-eq.iloc[-1])<1e-6
    limits=dict(rejections=dict(reject),peak_entry_margin=peakmargin,max_simultaneous_lots=maxlots,pyramid_adds=adds,margin=margin,slip=slip,fee_scale=fee_scale)
    return trades,eq,limits


def main():
    OUT.mkdir(parents=True,exist_ok=True);configs=configurations()
    write('protocol.json',dict(configurations=configs,configuration_count=len(configs),capital=50000,margin=.2,drawdown_target=10000,previous_drawdown_target=7500,selection='Retain original15% screening (train25 trades/DD4000, validation10/DD5000). Expanded20% screening: train net>0, >=25 trades for new alpha or >=20 campaigns for known-A allocation controls, DD<=10000; validation net>0, >=10 trades, DD<=10000. Rank by training net. Test diagnostic only. Final suitability also requires full/test positive and stressDD<=10000. Selection rules amended explicitly after user raised acceptable drawdown to20%; not pristine prospective selection.',notes=['Same repeatedly observed one-year history; not independent holdout.','First74 combinations were evaluated under15% target; four1250/1500 risk allocation controls added after20% user update.','20% notional margin baseline; entry margin caps40/60/80% equity as listed.','Gate costs and price-risk filters remain fixed during actual slippage/fee stress.','No averaging down: pyramid only after closed1.5R profit and next-bar protective stop.','Pyramid averages entry/fees for cost accounting; exit ledger represents complete campaign, not separate fill lots.','No live deployment.']))
    tape,signals,opens,atrs=load();results=[]
    for cfg in configs:
        ts,eq,lim=run(tape,signals,opens,atrs,cfg)
        row=dict(config=cfg,**old.evaluate(ts,eq,lim));results.append(row)
        print(cfg['id'],row['train']['net'],row['validation']['net'],row['test']['net'],row['full']['max_drawdown'],flush=True)
        write('comparison-progress.json',results)
    ranked=sorted(results,key=lambda r:r['train']['net'],reverse=True)
    eligible=[r for r in ranked if r['train']['net']>0 and r['train']['trades']>=25 and r['train']['max_drawdown']<=4000 and r['validation']['net']>0 and r['validation']['trades']>=10 and r['validation']['max_drawdown']<=5000]
    expanded=[r for r in ranked if r['train']['net']>0 and r['train']['trades']>=(20 if r['config']['id'].startswith('A_scale_') else 25) and r['train']['max_drawdown']<=10000 and r['validation']['net']>0 and r['validation']['trades']>=10 and r['validation']['max_drawdown']<=10000]
    write('comparison.json',dict(results=results,eligible15=[r['config']['id'] for r in eligible],eligible20=[r['config']['id'] for r in expanded],train_rank=[r['config']['id'] for r in ranked]))
    # Retain a bounded shortlist selected without test ranking; include fixed A
    # capital controls so alpha improvement is distinguished from exposure.
    chosen=expanded[:3]
    for kind in ['A_scale_750_m60','A_scale_1000_m60','A_pyramid','B_original_20margin','C_target5_guard1','B_target2_guard3']:
        row=next(r for r in results if r['config']['id']==kind)
        if row not in chosen:chosen.append(row)
    stress={}
    for row in chosen:
        cfg=row['config'];cases={}
        for label,kw in [('base',{}),('slip2',dict(slip=2)),('slip3',dict(slip=3)),('fee2',dict(fee_scale=2)),('margin25',dict(margin=.25)),('reverse',dict(reverse=True))]:
            ts,eq,lim=run(tape,signals,opens,atrs,cfg,**kw)
            cases[label]=old.evaluate(ts,eq,lim)
            pd.DataFrame(ts).to_csv(OUT/f'{cfg["id"]}-{label}-trades.csv',index=False)
            eq.to_csv(OUT/f'{cfg["id"]}-{label}-equity.csv')
        stress[cfg['id']]=dict(config=cfg,scenarios=cases)
        write('shortlist-progress.json',stress)
    write('shortlist.json',stress)
    write('implementation.json',{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__),Path(old.__file__),Path(b.__file__),b.FEES]})
    print('COMPLETE',len(configs),'eligible',len(eligible),flush=True)


if __name__=='__main__':main()
