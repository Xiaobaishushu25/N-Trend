"""Small chronological, data-driven rule search. Never imports prior alpha results.

Commands: prepare, freeze, train, evaluate, report, verify. Evaluate is single-shot.
All artifacts are historical research exposure, not independent prospective evidence.
"""
import argparse
import collections
import datetime as dt
import hashlib
import itertools
import json
import math
import platform
import re
import shutil
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts/simple_wfo_20261105'
DB = ROOT / 'data/research_market_history.sqlite'
STEP = 300_000_000_000
SLOTS = list(range(540, 615, 5)) + list(range(630, 690, 5)) + list(range(810, 900, 5))
ENTRY = [570, 600, 630, 660, 810, 840, 870]
FEATURES = ['displacement', 'efficiency', 'location', 'volume_ratio']
LOOKBACK = [6, 12, 24]
HOLD = [6, 12, 24]
STOP = [1., 2.]


def sha(p):
    h = hashlib.sha256()
    with Path(p).open('rb') as f:
        for b in iter(lambda: f.read(4 * 1024 * 1024), b''): h.update(b)
    return h.hexdigest()


def js(p, v):
    Path(p).write_text(json.dumps(v, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf8')


def rd(p): return json.loads(Path(p).read_text(encoding='utf8'))
def ns(s): return int(pd.Timestamp(s, tz='Asia/Shanghai').value)
def iso(t): return str(pd.Timestamp(int(t), tz='UTC').tz_convert('Asia/Shanghai'))


def spec_table():
    text = (ROOT/'crates/n-core/src/precision.rs').read_text(encoding='utf8')
    def table(name):
        section = text.split('pub const ' + name, 1)[1].split('];', 1)[0]
        return {k: float(v) for k, v in re.findall(r'\("([A-Z]+)",\s*([\d.]+)\)', section)}
    ticks, mult = table('DEFAULT_TICKS'), table('DEFAULT_MULTIPLIERS')
    ticks.update(BU=1., SH=1.); mult.update(JD=10.)
    fees = rd(ROOT/'scripts/exchange_fees_20261022.json')['products']
    # SR snapshot contains an old user roundtrip override, not fee legs.
    # Restore its archived published exchange legs, consistently across products.
    if 'published_schedule_before_override' in fees.get('SR',{}):
        fees['SR']={**fees['SR'],**fees['SR']['published_schedule_before_override']}
    return {p: dict(tick=ticks[p], multiplier=mult[p], fees=fees[p], margin_rate=.20) for p in fees}


def fee(s, kind, price, factor=1.):
    f = s['fees'][kind]
    return factor * (f['fixed_yuan_per_lot'] + abs(price)*s['multiplier']*f['notional_rate'] + 1.)


def prepare():
    """No mapping table access; prior observed day's volume determines today's contract."""
    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT/'prepared'; dest.mkdir(exist_ok=False)
    specs = spec_table(); js(dest/'specs.json', specs)
    con = sqlite3.connect(DB.resolve().as_uri()+'?mode=ro', uri=True)
    con.execute('PRAGMA query_only=ON'); con.execute('BEGIN')
    contracts = [r[0] for r in con.execute('select distinct contract from bars where period_seconds=300')]
    products = sorted(set(re.sub(r'\d+$', '', c.split('.')[-1]).upper() for c in contracts))
    coverage = []; choices = []
    for p in products:
        cs = [c for c in contracts if re.sub(r'\d+$', '', c.split('.')[-1]).upper() == p]
        f = pd.read_sql_query('select contract,start_ns,end_ns,open,high,low,close,volume,complete,constituent_count '
                             'from bars where period_seconds=300 and contract in ('+','.join('?' for _ in cs)+') order by start_ns,contract', con, params=cs)
        t = pd.to_datetime(f.start_ns, utc=True).dt.tz_convert('Asia/Shanghai')
        f['day'] = t.dt.strftime('%Y-%m-%d'); f['month'] = t.dt.strftime('%Y-%m'); f['minute'] = t.dt.hour*60+t.dt.minute
        f = f.loc[f.minute.isin(SLOTS)].copy()
        f['valid'] = (np.isfinite(f[['open','high','low','close','volume']]).all(axis=1) & (f.low>0) &
                      (f.low<=f[['open','close']].min(axis=1)) & (f.high>=f[['open','close']].max(axis=1)) &
                      (f.complete==1) & (f.constituent_count==5) & (f.end_ns-f.start_ns==STEP) & (f.start_ns%STEP==0) & (f.volume>=0))
        # Expired/delivery-month contracts may never be opened. Month from code, no future mapping.
        def delivery(c, day):
            code = re.search(r'(\d+)$', c).group(1); year = int(day[:4])
            yy = (year//10*10+int(code[0])) if len(code)==3 else (2000+int(code[:2]))
            if len(code)==3 and yy < year-5: yy += 10
            return yy*100+int(code[-2:])
        prev = None; selected = []
        for day, g in f.groupby('day', sort=True):
            if prev is not None:
                prior_day, stats = prev
                eligible = [(c,v) for c,v in stats.items() if delivery(c,day)>int(day[:7].replace('-',''))]
                if eligible:
                    chosen = sorted(eligible, key=lambda q:(-q[1],q[0]))[0][0]
                    selected.append(g.loc[g.contract==chosen])
                    choices.append(dict(product=p,day=day,known_at_day=prior_day,contract=chosen,previous_day_volume=int(stats[chosen]),available_rows=int((g.contract==chosen).sum())))
            stats = g.loc[g.valid].groupby('contract').volume.sum().to_dict()
            prev = (day, stats)
        q = pd.concat(selected, ignore_index=True) if selected else f.iloc[:0].copy()
        q = q.sort_values('start_ns').reset_index(drop=True)
        # A missing expected day-session slot resets features; breaks are explicitly represented by slot sequence.
        si = q.minute.map({v:i for i,v in enumerate(SLOTS)})
        reset = q.day.ne(q.day.shift()) | q.contract.ne(q.contract.shift()) | si.ne(si.shift()+1) | ~q.valid | ~q.valid.shift(fill_value=False)
        q['segment'] = reset.cumsum()
        q['atr'] = np.nan
        for feature in FEATURES:
            for lb in LOOKBACK: q[f'{feature}_{lb}'] = np.nan
        for _, g in q.loc[q.valid].groupby('segment', sort=False):
            cl = g.close; delta = cl.diff(); tr = pd.concat([g.high-g.low,(g.high-cl.shift()).abs(),(g.low-cl.shift()).abs()],axis=1).max(axis=1)
            atr = tr.rolling(6,min_periods=6).mean(); q.loc[g.index,'atr'] = atr
            for lb in LOOKBACK:
                path = delta.abs().rolling(lb,min_periods=lb).sum()
                high = g.high.rolling(lb).max(); low = g.low.rolling(lb).min()
                q.loc[g.index,f'displacement_{lb}'] = (cl-cl.shift(lb))/atr.replace(0,np.nan)
                q.loc[g.index,f'efficiency_{lb}'] = (cl-cl.shift(lb))/path.replace(0,np.nan)
                q.loc[g.index,f'location_{lb}'] = (cl-low)/(high-low).replace(0,np.nan)
                q.loc[g.index,f'volume_ratio_{lb}'] = g.volume/g.volume.shift().rolling(lb).mean().replace(0,np.nan)
        q.to_pickle(dest/f'{p}.pkl')
        coverage.append(dict(product=p,raw_day_rows=len(f),selected_rows=len(q),valid_rows=int(q.valid.sum()),days=q.day.nunique(),first=q.day.min(),last=q.day.max()))
        print('prepared', p, len(q), flush=True)
    con.close()
    pd.DataFrame(choices).to_csv(dest/'contract_choices.csv',index=False)
    pd.DataFrame(coverage).to_csv(dest/'coverage.csv',index=False)
    # Full calendar months only, using database coverage (not last profitable/active day).
    first, last = sqlite3.connect(DB.resolve().as_uri()+'?mode=ro', uri=True).execute('select min(start_ns),max(end_ns) from bars where period_seconds=300').fetchone()
    start = pd.Timestamp(iso(first)).tz_localize(None).to_period('M').start_time
    if ns(str(start)) < first: start += pd.offsets.MonthBegin(1)
    end = pd.Timestamp(iso(last)).tz_localize(None).to_period('M').start_time
    folds = []; train = start
    while train + pd.DateOffset(months=12) <= end:
        test = train+pd.DateOffset(months=9); finish=test+pd.DateOffset(months=3)
        folds.append(dict(fold=len(folds)+1,train=str(train.date()),test=str(test.date()),end=str(finish.date())))
        train += pd.DateOffset(months=3)
    js(dest/'folds.json',folds)
    js(dest/'manifest.json',dict(database_sha256=sha(DB),prepared_files={p.name:sha(p) for p in sorted(dest.iterdir()) if p.is_file()},raw_first=iso(first),raw_last=iso(last)))


def protocol():
    return dict(version='simple-data-rule-WFO-1',created_shanghai=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
        historical_exposure='ALL historical intervals research-exposed; past research counts incomplete, not independent unseen holdout',
        folds=rd(OUT/'prepared/folds.json'),window='9 calendar months train / 3 months test; step 3; complete windows only',
        universe='22 available products; each fold top20 by training valid row count, alphabetical tie; all product selection training-only',
        search=dict(features=FEATURES,lookback_bars=LOOKBACK,bands=['bottom_quartile','middle_half','top_quartile'],directions=[-1,1],holding_bars=HOLD,stop_atr=STOP,
                    templates_per_product=432,entry_minutes=ENTRY,comment='single axis learned rule, no library of named strategy families'),
        inner_validation='first3 months calibrate quantiles; months4..9 expanding-past quantile calibration and one-month sequential scoring; threshold refit on all9 only after rule selection',
        ranking='equal percentile ranks: net, negative realized trade DD, median monthly net, positive-month fraction, log(1+trades), neighbor median net; score average. Neighbors adjacent lookback/hold/stop with same feature/band/direction',
        selection='net>0 on six chronological validation months is sole economic gate; pick top ranked candidate per product then top3 products; cash allowed; risk flags are annotations not extra significance gates',
        execution='signal at previous completed day-session5m bar; next scheduled actual open; one lot; no overlap per product; stop fixed from signal close +/- stop_atr*known6barATR rounded away; timed exit next open after6/12/24 observed bars, forced14:55 open; gap fills adverse open, no profit target',
        locking='prior zero-volume/one-price bars prevent submission; current locked or invalid entry bar marks unknown fill, never hindsight zero profit. Unknown training exposures cannot be selected; unknown test exposure invalidates complete equity. Locked existing stop exits defer; missing slot exits next executable actual open.',
        costs='20261022 open/close_today/close_yesterday exchange snapshot +1yuan broker commission/side; baseline2 adverse ticks/side; stress3 ticks/side and2x all fees; historical fees NOT verified',
        margin='20% notional reserve baseline,25% stress, no offset, max60% equity; conservative scenario, NOT certified actual historical broker margin',
        account='50000 shared;1 integer lot;planned stop loss including3tick exit buffer+fees <=600; total planned risk<=900; day loss900 halts entries;12% equity DD permanently halts new entries and requests next-open exits; never reset per fold',
        timestamps='Asia/Shanghai; no random shuffle; feature reset daily/contract/missing slot; no adjusted roll PnL; prior observed day volume only, no mappings table',
        reporting='all candidates including losers, per-month validation, neighborhood, frozen selections; evaluation one pass all folds without stdout performance; aggregate released only after all complete',
        limitation='OHLC cannot prove queue, limit availability or executable liquidity; historical spec/fee/margin schedules incomplete. Results conditional, cannot certify actual tradeability. 20% future DD not guaranteed.',
        forward='workflow/code/selection frozen; before any live forward simulation require current concrete-contract fees/margin/limits and fresh data; no historical test-based tuning; no live orders from this script')


def freeze():
    assert not (OUT/'freeze.json').exists()
    js(OUT/'protocol.json',protocol())
    shutil.copy2(__file__,OUT/'frozen_simple_wfo.py')
    inputs=[Path(__file__),ROOT/'tests/test_simple_wfo.py',OUT/'protocol.json',ROOT/'scripts/exchange_fees_20261022.json',ROOT/'crates/n-core/src/precision.rs',OUT/'prepared/manifest.json']
    js(OUT/'freeze.json',dict(hashes={str(p.relative_to(ROOT)):sha(p) for p in inputs},database_sha256=rd(OUT/'prepared/manifest.json')['database_sha256'],python=platform.python_version(),numpy=np.__version__,pandas=pd.__version__,evaluations_before_freeze=0))
    print('Protocol frozen. No candidate results inspected.',flush=True)


def check():
    f=rd(OUT/'freeze.json')
    for p,h in f['hashes'].items(): assert sha(ROOT/p)==h,p
    for p,h in rd(OUT/'prepared/manifest.json')['prepared_files'].items(): assert sha(OUT/'prepared'/p)==h,p
    return f


def thresholds(f, feature, lb):
    prior_slots=[max(v for v in SLOTS if v<entry) for entry in ENTRY]
    a=f.loc[f.valid & f.minute.isin(prior_slots),f'{feature}_{lb}'].dropna().to_numpy()
    if not len(a): return [0.,0.]
    return np.quantile(a,[.25,.75]).tolist()


def band_mask(values, band, cuts):
    low,high=cuts
    return np.isfinite(values) & ((values<=low) if band==0 else ((values>low)&(values<high) if band==1 else values>=high))


def signals(f, candidate, cuts):
    feature,lb,band,direction,hold,stop=candidate
    value=f[f'{feature}_{lb}'].shift().to_numpy()
    prev=f.shift(); mask=band_mask(value,band,cuts)
    # Current OHLC/volume/completeness are not known at the opening decision.
    mask &= f.minute.isin(ENTRY).to_numpy() & prev.valid.fillna(False).to_numpy(bool)
    mask &= (f.day==prev.day).to_numpy() & (f.contract==prev.contract).to_numpy()
    mask &= np.isfinite(prev.atr.to_numpy(float))
    return np.flatnonzero(mask)


def trade_paths(f, direction, hold, k, s, slip=2, fee_factor=1.):
    """All scheduled potential trades. Independent outcomes reused, signals never inspect them."""
    a={c:f[c].to_numpy() for c in f}; results={}; tick=s['tick']; mult=s['multiplier']
    possible=np.flatnonzero(f.minute.isin(ENTRY).to_numpy())
    for i in possible:
        if i==0 or not a['valid'][i-1] or a['day'][i]!=a['day'][i-1] or a['contract'][i]!=a['contract'][i-1] or not np.isfinite(a['atr'][i-1]):continue
        # Eligibility uses the completed bar only. Current one-price bars are
        # execution ambiguity, never a profitable hindsight veto of an entry.
        if a['volume'][i-1]<=0 or a['high'][i-1]<=a['low'][i-1]:continue
        entry=a['open'][i]+direction*slip*tick
        rawstop=a['close'][i-1]-direction*k*a['atr'][i-1]
        stop=(math.floor(rawstop/tick) if direction==1 else math.ceil(rawstop/tick))*tick
        risk=(entry-(stop-direction*max(3,slip)*tick))*direction*mult+fee(s,'open',entry,fee_factor)+max(fee(s,'close_today',stop,fee_factor),fee(s,'close_yesterday',stop,fee_factor))
        if (entry-stop)*direction<=0 or not 0<risk<=600:continue
        if not a['valid'][i] or a['volume'][i]<=0 or a['high'][i]<=a['low'][i]:
            results[i]=dict(unresolved=True,entry_i=int(i),reason='entry_liquidity_unknown');continue
        due=False; reason=None; exit_i=None; base=None; gap=False; adverse=entry; worst=0.
        for j in range(i,len(f)):
            if a['contract'][j]!=a['contract'][i]: break
            executable=a['valid'][j] and a['volume'][j]>0 and a['high'][j]>a['low'][j]
            if j>i:
                oldslot=SLOTS.index(int(a['minute'][j-1])); newslot=SLOTS.index(int(a['minute'][j]))
                if a['day'][j]!=a['day'][i] or newslot!=oldslot+1: due=True
            if j-i>=hold or a['minute'][j]>=895: due=True
            if executable:
                if (a['open'][j]-stop)*direction<=0:
                    base=a['open'][j];reason='gap_stop';gap=True;exit_i=j;break
                if due:
                    base=a['open'][j];reason='time_or_session_or_missing';exit_i=j;break
                adverse=a['low'][j] if direction==1 else a['high'][j]
                worst=min(worst,(adverse-entry)*direction*mult-fee(s,'open',entry,fee_factor)-fee(s,'close_today',adverse,fee_factor)-slip*tick*mult)
                if (adverse-stop)*direction<=0:
                    base=stop;reason='stop';exit_i=j;break
            else:
                adverse=a['low'][j] if direction==1 else a['high'][j]
                if (adverse-stop)*direction<=0:due=True
                if due:gap=True
        if exit_i is None:
            results[i]=dict(unresolved=True,entry_i=int(i)); continue
        j=exit_i; exit_price=base-direction*slip*tick
        fo=fee(s,'open',entry,fee_factor); fc=fee(s,'close_today' if a['day'][j]==a['day'][i] else 'close_yesterday',exit_price,fee_factor)
        gross=(exit_price-entry)*direction*mult; net=gross-fo-fc
        results[i]=dict(unresolved=False,entry_i=int(i),exit_i=int(j),entry_ns=int(a['start_ns'][i]),exit_ns=int(a['start_ns'][j] if reason!='stop' else a['end_ns'][j]),
                        day=str(a['day'][i]),month=str(a['month'][i]),contract=str(a['contract'][i]),side=direction,lots=1,entry=float(entry),exit=float(exit_price),stop=float(stop),
                        planned_risk=float(risk),gross=float(gross),fees=float(fo+fc),open_fee=float(fo),net=float(net),slippage_yuan=float(2*slip*tick*mult),reason=reason,gap=gap,
                        worst_intratrade=float(min(worst,net)),multiplier=mult,tick=tick,margin_rate=s['margin_rate'])
    return results


def isolated(indices, paths):
    trades=[]; last=-1; unknown=0
    for i in indices:
        if i<=last:continue
        t=paths.get(int(i))
        if t is None:continue
        if t['unresolved']: unknown+=1;last=10**12;continue
        trades.append(t);last=t['exit_i']
    return trades,unknown


def metrics(trades, months=None):
    x=np.array([t['net'] for t in trades],float); curve=np.r_[50000.,50000.+np.cumsum(x)]
    dd=float(np.max(np.maximum.accumulate(curve)-curve)); month={m:0. for m in (months or [])}
    for t in trades:month[t['month']]=month.get(t['month'],0.)+t['net']
    win=x[x>0]; loss=x[x<0]; costs=sum(t['fees']+t['slippage_yuan'] for t in trades)
    return dict(net=float(x.sum()),drawdown_yuan=dd,trades=len(x),win_rate=float((x>0).mean()) if len(x) else None,
                payoff=float(win.mean()/(-loss.mean())) if len(win) and len(loss) else None,profit_factor=float(win.sum()/(-loss.sum())) if len(loss) else None,
                monthly=month,median_month=float(np.median(list(month.values()))) if month else 0.,positive_fraction=float(np.mean(np.array(list(month.values()))>0)) if month else 0.,
                cost_yuan=float(costs),extra_tick_net=float(x.sum()-sum(2*t['tick']*t['multiplier'] for t in trades)),
                risk_breaches=sum(t['net'] < -600 for t in trades))


def train():
    check(); dest=OUT/'training';dest.mkdir(exist_ok=False)
    specs=rd(OUT/'prepared/specs.json');folds=rd(OUT/'prepared/folds.json');total=0
    candidates=list(itertools.product(FEATURES,LOOKBACK,range(3),[-1,1],HOLD,STOP)); assert len(candidates)==432
    for fold in folds:
        fd=dest/f"fold_{fold['fold']:02d}";fd.mkdir()
        allframes={p.stem:pd.read_pickle(p) for p in (OUT/'prepared').glob('*.pkl')}
        frames={p:f.loc[(f.start_ns>=ns(fold['train']))&(f.start_ns<ns(fold['test']))].reset_index(drop=True) for p,f in allframes.items()}
        coverage=sorted([(p,int(f.valid.sum())) for p,f in frames.items()],key=lambda q:(-q[1],q[0]))
        universe=[p for p,n in coverage[:20] if n>0]
        js(fd/'universe.json',dict(ranking=coverage,selected=universe))
        records=[]; selected=[]
        months=pd.period_range(fold['train'],periods=9,freq='M').astype(str).tolist(); valmonths=months[3:]
        for product in universe:
            f=frames[product];s=specs[product]; paths={}
            for d,h,k in itertools.product([-1,1],HOLD,STOP):paths[d,h,k]=trade_paths(f,d,h,k,s)
            cuts={}
            for feature,lb in itertools.product(FEATURES,LOOKBACK):
                for month in valmonths:cuts[feature,lb,month]=thresholds(f.loc[f.month<month],feature,lb)
            pd.DataFrame([dict(feature=feat,lookback=lb,validation_month=m,q25=q[0],q75=q[1]) for (feat,lb,m),q in cuts.items()]).to_csv(fd/f'{product}_monthly_thresholds.csv',index=False)
            signal_cache={}
            # Signal sets do not depend on direction/holding/stop; compute once.
            for feature,lb,band in itertools.product(FEATURES,LOOKBACK,range(3)):
                indices=[]
                for month in valmonths:
                    inds=signals(f,[feature,lb,band,1,6,1.],cuts[feature,lb,month])
                    indices.extend(inds[f.month.iloc[inds].to_numpy()==month])
                signal_cache[feature,lb,band]=sorted(indices)
            local=[]
            for ci,c in enumerate(candidates):
                feature,lb,band,d,h,k=c
                trades,unknown=isolated(signal_cache[feature,lb,band],paths[d,h,k]);m=metrics(trades,valmonths)
                rec=dict(fold=fold['fold'],product=product,candidate_id=f'{product}-{ci:03d}',feature=feature,lookback=lb,band=band,side=d,hold=h,stop_atr=k,
                         **{key:v for key,v in m.items() if key!='monthly'},unknown=unknown,monthly_json=json.dumps(m['monthly']),candidate=list(c))
                local.append(rec)
            lookup={tuple(r['candidate']):r for r in local}
            for r in local:
                neighbors=[];c=r['candidate']
                for axis,vals in [(1,LOOKBACK),(4,HOLD),(5,STOP)]:
                    ix=vals.index(c[axis])
                    for ni in [ix-1,ix+1]:
                        if 0<=ni<len(vals):
                            other=c.copy();other[axis]=vals[ni];neighbors.append(lookup[tuple(other)]['net'])
                r['neighbor_median_net']=float(np.median(neighbors));r['neighbor_positive_fraction']=float(np.mean(np.array(neighbors)>0))
                monthly=list(json.loads(r['monthly_json']).values());positive=sum(max(v,0) for v in monthly)
                r['flags']=';'.join(flag for flag,yes in [('few_trades',r['trades']<30),('month_concentration',positive>0 and max(monthly)/positive>.5),('parameter_peak',r['net']>0 and r['neighbor_median_net']<=0),('extra_tick_sensitive',r['net']>0 and r['extra_tick_net']<=0),('unknown_path',r['unknown']>0),('nonpositive_median_month',r['median_month']<=0)] if yes)
            records.extend(local)
            print('train',fold['fold'],product,'candidates',len(local),flush=True)
        df=pd.DataFrame(records)
        keys=['net','drawdown_yuan','median_month','positive_fraction','trades','neighbor_median_net']
        score=sum(df[k].rank(pct=True,ascending=k!='drawdown_yuan') for k in keys)/len(keys)
        df['score']=score
        eligible=df.loc[(df.net>0)&(df.unknown==0)].sort_values(['score','candidate_id'],ascending=[False,True])
        chosen=eligible.drop_duplicates('product').head(3)
        for _,r in chosen.iterrows():
            c=r['candidate'];cutsfinal=thresholds(frames[r['product']],c[0],c[1])
            selected.append(dict(product=r['product'],candidate_id=r['candidate_id'],candidate=c,cuts=cutsfinal,score=r['score'],validation_net=r['net'],flags=r['flags']))
        df.drop(columns='candidate').to_csv(fd/'all_candidates.csv',index=False)
        js(fd/'selection.json',dict(**fold,selected=selected,empty_reason='no_positive_net_eligible_rule' if not selected else None,search_count=len(df),threshold_fits=len(universe)*12*6,final_threshold_refits=len(selected)))
        total+=len(df)
    js(dest/'manifest.json',dict(candidate_window_evaluations=total,unique_rule_templates=432,files={str(p.relative_to(dest)):sha(p) for p in sorted(dest.rglob('*')) if p.is_file()}))
    print('All training complete. All selections frozen; OOS evaluation has not begun.',flush=True)


def account(folds, allframes, specs, slip=2, fee_factor=1., margin_rate=.20):
    cash=50000.;peak=50000.;halt=False;day=None;day_start=cash;dayhalt=False;active={};trades=[];snap=[];reject=[];summaries=[];unknown=[]
    for fold in folds:
        fno=fold['fold'];before=len(trades);startcash=cash;fold_snaps=[]
        selection=rd(OUT/f'training/fold_{fno:02d}/selection.json')['selected']
        frames={};orders=collections.defaultdict(list)
        for rank,r in enumerate(selection):
            f=allframes[r['product']]; f=f.loc[(f.start_ns>=ns(fold['test']))&(f.start_ns<ns(fold['end']))].reset_index(drop=True)
            frames[r['product']]=f;c=r['candidate'];paths=trade_paths(f,c[3],c[4],c[5],specs[r['product']],slip,fee_factor)
            # Do not suppress later signals based on hypothetical rejected trades.
            for i in signals(f,c,r['cuts']):
                t=paths.get(int(i))
                if t:
                    orders[int(f.start_ns.iloc[i])].append((rank,r,t))
        # Include all actual day-session bars for MTM even when selection is cash.
        timeline=sorted(set().union(*(set(f.loc[(f.start_ns>=ns(fold['test']))&(f.start_ns<ns(fold['end']))].start_ns) for f in allframes.values())))
        rows={p:{int(r.start_ns):r for r in f.itertuples()} for p,f in frames.items()}
        def equity(marks):
            return cash+sum((marks.get(p,pos['mark'])-pos['trade']['entry'])*pos['trade']['side']*pos['trade']['multiplier'] for p,pos in active.items())
        for tm in timeline:
            d=iso(tm)[:10];opening={p:float(rs[tm].open) for p,rs in rows.items() if tm in rs};closing={p:float(rs[tm].close) for p,rs in rows.items() if tm in rs}
            if d!=day:day=d;day_start=equity(opening);dayhalt=False
            for p in active:active[p]['mark']=opening.get(p,active[p]['mark'])
            eqopen=equity(opening);peak=max(peak,eqopen)
            if eqopen<=peak*.88:halt=True
            if day_start-eqopen>=900:dayhalt=True
            riskhalt=halt or dayhalt
            # All entries admitted before same-bar exits; future cash never funds current opens.
            for rank,r,t in sorted(orders.get(tm,[]),key=lambda x:(x[0],x[1]['product'])):
                p=r['product'];reason=None
                if t['unresolved']:
                    # Cannot invent a flat price. Only invalidate if this exposure could be admitted.
                    if p not in active and not riskhalt:unknown.append(dict(fold=fno,product=p,time=iso(tm)))
                    continue
                eq=equity(opening);margin=sum(pos['mark']*pos['trade']['multiplier']*margin_rate for pos in active.values());risk=sum(pos['trade']['planned_risk'] for pos in active.values())
                need=t['entry']*t['multiplier']*margin_rate
                if p in active:reason='position_active'
                elif riskhalt:reason='account_halt'
                elif risk+t['planned_risk']>900:reason='total_risk'
                elif margin+need>(eq-t['open_fee'])*.6 or margin+need+t['open_fee']>eq:reason='margin'
                if reason:reject.append(dict(fold=fno,time=iso(tm),product=p,reason=reason));continue
                cash-=t['open_fee'];active[p]=dict(trade=t.copy(),mark=opening[p],candidate_id=r['candidate_id'],fold=fno)
            # Lower bound with every open position at its adverse intrabar extreme; conservative simultaneous marks.
            adverse={}
            for p,pos in active.items():
                row=rows[p].get(tm);t=pos['trade']
                adverse[p]=(row.low if t['side']==1 else row.high) if row is not None else pos['mark']
            stress_eq=equity(adverse)
            for p,pos in list(active.items()):
                t=pos['trade'];exit_now=t['exit_ns']<=tm or (t['reason']=='stop' and t['exit_ns']==tm+STEP)
                # Account liquidation uses current known open, only after a previous mark triggered halt.
                row=rows[p].get(tm)
                if riskhalt and tm>t['entry_ns'] and row is not None and row.valid and row.volume>0 and row.high>row.low:
                    exit_price=row.open-t['side']*slip*t['tick'];s=specs[p];fc=fee(s,'close_today' if day==t['day'] else 'close_yesterday',exit_price,fee_factor)
                    t.update(exit=float(exit_price),exit_ns=tm,net=float((exit_price-t['entry'])*t['side']*t['multiplier']-t['open_fee']-fc),fees=float(t['open_fee']+fc),reason='account_risk_exit')
                    t['gross']=t['net']+t['fees'];exit_now=True
                if exit_now:
                    cash+=t['net']+t['open_fee'];trades.append(dict(**t,fold=fno,product=p,candidate_id=pos['candidate_id'],entry_time=iso(t['entry_ns']),exit_time=iso(t['exit_ns'])));del active[p]
                else:pos['mark']=closing.get(p,pos['mark'])
            eq=equity(closing);peak=max(peak,eq)
            if eq<=peak*.88:halt=True
            if day_start-eq>=900:dayhalt=True
            # Intrabar drawdown is a bound, not a tradable intrabar equity quote; stop fill caps where executable.
            row=dict(fold=fno,time=iso(tm+STEP),time_ns=tm+STEP,balance=cash,equity=eq,intrabar_equity_bound=stress_eq,peak=peak,drawdown=(peak-eq)/peak,
                     margin=sum(pos['mark']*pos['trade']['multiplier']*margin_rate for pos in active.values()),planned_risk=sum(pos['trade']['planned_risk'] for pos in active.values()),halted=halt)
            snap.append(row);fold_snaps.append(row)
        if active:unknown.append(dict(fold=fno,reason='positions_unresolved_at_fold_end',products=list(active)))
        m=metrics(trades[before:]);curve=np.r_[startcash,[r['equity'] for r in fold_snaps]];peaks=np.maximum.accumulate(curve)
        m.update(fold=fno,train_start=fold['train'],test_start=fold['test'],test_end=fold['end'],start_equity=startcash,end_equity=cash,max_drawdown=float(np.max((peaks-curve)/peaks)),selected_products=[r['product'] for r in selection],halted=halt)
        summaries.append(m)
    return dict(trades=trades,equity=snap,rejections=reject,folds=summaries,unknown=unknown,final_equity=cash if not unknown else None,halted=halt)


def evaluate():
    check();manifest=rd(OUT/'training/manifest.json')
    for p,h in manifest['files'].items():assert sha(OUT/'training'/p)==h,p
    dest=OUT/'evaluation';dest.mkdir(exist_ok=False)
    js(dest/'started.json',dict(time=dt.datetime.now(dt.timezone.utc).isoformat(),training_manifest_sha256=sha(OUT/'training/manifest.json'),policy='single-shot,no_score_stdout'))
    frames={p.stem:pd.read_pickle(p) for p in (OUT/'prepared').glob('*.pkl')};specs=rd(OUT/'prepared/specs.json');folds=rd(OUT/'prepared/folds.json')
    for label,slip,factor,margin in [('base',2,1.,.20),('stress',3,2.,.25)]:
        result=account(folds,frames,specs,slip,factor,margin)
        for key in ['trades','equity','rejections']:pd.DataFrame(result.pop(key)).to_csv(dest/f'{label}_{key}.csv',index=False)
        js(dest/f'{label}_summary.json',result)
    js(dest/'complete.json',dict(folds=len(folds),completed_utc=dt.datetime.now(dt.timezone.utc).isoformat(),files={p.name:sha(p) for p in sorted(dest.iterdir()) if p.is_file()}))
    print('ALL OOS and fixed stress evaluations completed; aggregate release now permitted.',flush=True)


def report():
    assert (OUT/'evaluation/complete.json').exists()
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    base=rd(OUT/'evaluation/base_summary.json');stress=rd(OUT/'evaluation/stress_summary.json')
    eq=pd.read_csv(OUT/'evaluation/base_equity.csv');se=pd.read_csv(OUT/'evaluation/stress_equity.csv')
    trades=pd.read_csv(OUT/'evaluation/base_trades.csv') if (OUT/'evaluation/base_trades.csv').stat().st_size>3 else pd.DataFrame()
    fig,ax=plt.subplots(figsize=(12,4.5))
    ax.plot(pd.to_datetime(eq.time,utc=True),eq.equity,label='2 ticks/side + fees + broker allowance')
    ax.plot(pd.to_datetime(se.time,utc=True),se.equity,label='3 ticks/side + double fees + 25% margin',alpha=.8)
    ax.axhline(50000,color='gray',lw=.7);ax.set_ylabel('Shared account equity (CNY)');ax.legend();ax.grid(alpha=.2);fig.tight_layout();fig.savefig(OUT/'oos_equity.png',dpi=160);plt.close(fig)
    rows=['# 简单数据驱动滚动研究：完整结果','', '**全部历史均为研究暴露数据；以下是时间顺序回放样本外，不能称为完全独立样本。**','',
          '流程在正式测试前冻结；所有轮次的选择先完成并保存，之后一次性评估全部测试及事先固定压力情景。没有根据样本外收益再调参。','',
          '## 样本外共享账户','',f"基准最终权益：{base['final_equity']} 元；净额：{(base['final_equity']-50000) if base['final_equity'] is not None else '未知'} 元；5分钟收盘盯市最大回撤：{eq.drawdown.max():.2%}。",
          f"压力最终权益：{stress['final_equity']} 元；最大回撤：{se.drawdown.max():.2%}。基准成交数：{len(trades)}。",'',
          '|轮次|训练开始|测试区间（右端不含）|选中品种|净收益元|最大回撤|胜率|平均盈利/平均亏损|笔数|',
          '|---|---|---|---|---:|---:|---:|---:|---:|']
    for f in base['folds']:
        wr='—' if f['win_rate'] is None else f"{f['win_rate']:.1%}";pay='—' if f['payoff'] is None else f"{f['payoff']:.2f}"
        rows.append(f"|{f['fold']}|{f['train_start']}|{f['test_start']} → {f['test_end']}|{','.join(f['selected_products']) or '空仓'}|{f['net']:.2f}|{f['max_drawdown']:.2%}|{wr}|{pay}|{f['trades']}|")
    rows+=['','## 参数及训练选择','', 'band=0为训练分布最低四分位，1为中间一半，2为最高四分位。方向为-1空、+1多；hold/回看单位为5分钟bar。参数顺序：[特征,回看,band,方向,最长持有,止损ATR倍数]。阈值是对应训练窗内重估的绝对值。','']
    for f in base['folds']:
        selected=rd(OUT/f"training/fold_{f['fold']:02d}/selection.json")['selected'];rows.append(f"- 轮{f['fold']}："+('; '.join(f"{r['candidate_id']} {r['candidate']}，阈值{r['cuts']}，标记{r['flags'] or '无'}" for r in selected) or '空选'))
    manifest=rd(OUT/'training/manifest.json')
    rows+=['','## 复现及搜索账本','',f"本次{manifest['candidate_window_evaluations']:,}次候选×窗口评估；432条规则模板×20品种×{len(base['folds'])}轮。每次含6个月度扩展窗校准/验证，不作为独立试验。无失败候选删除。更早研究至少80模板/256配置窗口，另有R1/R2及大量项目历史，真实累计次数未知。",'',
          '各轮training/fold_XX/all_candidates.csv保存所有候选及6个月净收益、交易数、回撤、费用敏感性、参数邻域和排序；selection.json保存冻结参数及阈值。prepared/contract_choices.csv保存合约选择及其可得日。evaluation/base_trades.csv保存所有实际准入交易，base_rejections.csv保存账户拒绝；base_equity.csv是连续5万元共享账户曲线。','',
          '复现顺序：python scripts/simple_wfo.py prepare → freeze → train → evaluate → report。输出目录不可覆盖；复现需更换脚本OUT为新目录，属于新运行，不可覆盖本次冻结记录。python scripts/simple_wfo.py verify核验本次快照。','',
          '## 执行及证据边界','',
          '固定1手，逐品种仅1个持仓；单手计划止损含进场2跳、出场至少3跳及双边手续费/佣金缓冲，超过600元不交易；共享总计划风险900元、保证金占权益60%、日亏900停止开仓、账户12%回撤后永久停开并下一可成交开盘退出。不同轮次权益、高水位及停机状态不重置。跳空、涨跌停、交易中断仍可突破600元及20%回撤；该止损不是损失保证。','',
          '信号只读已完成5分钟K线，按固定半小时时点在下一实际开盘参考价成交；价格均为真实合约，换月不连接价格。前一观察交易日成交量选择当日合约，不使用mappings。全历史只包含供应商采集的部分合约，仍有数据供应及幸存者覆盖偏差，无法证明当时全市场最大成交量合约均在库内。','',
          '单价或零成交量bar不准入，退出延至可执行bar；缺bar时下一已观察到的可成交开盘强退。14:55开盘平仓，防止假设15:00收盘市价必成交。OHLC无逐笔盘口、封板价、排队或bid/ask；2跳/3跳为保守研究假设，不能保证真实成交。','',
          '乘数来自现有规格表并修正JD=10元/报价点、BU tick=1、SH tick=1。手续费为2026-10-22快照，另加每边1元佣金；没有三年逐日、逐合约实际费用及保证金记录。20%/25%名义本金保证金占用是审慎场景，不能标作真实历史保证金。因此本次是条件成本场景下的完整回放，未满足“实际历史费用/保证金核验”的执行认证。','',
          '排序的回撤是候选单品种已平仓序列；账户正式回撤包含5分钟收盘浮盈亏。intrabar_equity_bound是所有活跃仓位同时处于本bar最不利极值的保守下界，不是可实现的精确账户路径，同bar止损之后极值也可能使它偏低。','',
          '## 结论和前向模拟','']
    positive=base['final_equity'] is not None and base['final_equity']>50000 and stress['final_equity'] is not None and stress['final_equity']>50000
    rows.append('本次基准及压力情景历史净额均为正，但这不足以确认泛化或真实可交易性，需新行情前向模拟。' if positive else '本次没有得到可确认的稳定正收益流程。不得据此继续在这批测试收益上调参寻找盈利版本。')
    rows+=['','流程、候选空间、排序、账户规则已冻结。paper_forward.json记录冻结引用及新行情模拟要求；本次未启动实时行情采集或模拟下单，不能称前向模拟已完成。下一步须接入冻结之后新发生行情，校验每个实际合约的交易时段、手续费、保证金、涨跌停和bid/ask后，原样运行并累计报告所有月份；任何变更另立版本/队列。未来回撤超过20%的风险无法完全排除。','',
           '规格核查来源：[上期所沥青合约](https://www.shfe.com.cn/products/futures/energyandchemical/bu_f/)；[郑商所烧碱业务细则](https://www.czce.com.cn/cn/uploadfile/2025/01/14/20250114111531878.pdf)。其余规格和费率来源保留于prepared/specs.json；并非逐日历史审计。']
    (OUT/'REPORT.md').write_text('\n'.join(rows),encoding='utf8')
    js(OUT/'paper_forward.json',dict(status='NOT_STARTED',workflow_freeze_sha256=sha(OUT/'freeze.json'),historical_evaluation_sha256=sha(OUT/'evaluation/complete.json'),activation='first new complete day after live data and concrete metadata are validated; no backfilled history as prospective evidence',retrain='quarterly first trading day: last9 complete calendar months, same search/selection; no tuning from paper outcomes',risk='same frozen shared-account risk rules; no actual orders authorized by this script',required_inputs=['all actual day-session5m bars','bid/ask and limit lock status','contract multiplier/tick','actual open/close_today/close_yesterday fees','broker long/short margin','trading calendar'],independent_prospective_results=0))
    print(json.dumps(dict(base=base,stress=stress),ensure_ascii=False,indent=2))


def verify():
    check();assert sha(DB)==rd(OUT/'freeze.json')['database_sha256']
    for directory in ['training','evaluation']:
        m=rd(OUT/directory/('manifest.json' if directory=='training' else 'complete.json'))
        for p,h in m['files'].items():assert sha(OUT/directory/p)==h,p
    print('All source, data, prepared, selection and OOS artifact hashes verified.')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('stage',choices=['prepare','freeze','train','evaluate','report','verify']);args=parser.parse_args();globals()[args.stage]()
