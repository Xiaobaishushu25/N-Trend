"""Exposed research revision4: data-selected integer hedges, actual two-leg payoff.

No return forecasting surrogate. Unit diagnostic ledger never funds/chooses trades.
"""
import argparse
import collections
import datetime as dt
import itertools
import json
import shutil
import subprocess
import sys
from pathlib import Path
import numpy as np
import pandas as pd
import broad_session_wfo as b
from daily_context_wfo import utility

c=b.c;ROOT=b.ROOT;PRIOR=b.OUT;OUT=ROOT/'artifacts/paired_execution_wfo_20261105'
LOOKBACK=[12,48,144];THRESHOLD=[1.,2.];ORIENTATION=[-1,1];HOLDS=[12,48,144];STOPS=[3.,6.]
GRID=list(itertools.product(LOOKBACK,THRESHOLD,ORIENTATION,HOLDS,STOPS))


def analysis():
    OUT.mkdir(parents=True,exist_ok=True);rows=[]
    for name in ['simple_wfo_20261105','daily_context_wfo_20261105','broad_session_wfo_20261105']:
        p=ROOT/'artifacts'/name;t=pd.read_csv(p/'evaluation/base_trades.csv')
        win=t.loc[t.net>0].net;loss=t.loc[t.net<0].net;pay=float(win.mean()/-loss.mean());cost=t.fees+t.slippage_yuan
        protective=t.reason.isin(['stop','gap_stop','account_risk_exit'])
        rows.append(dict(version=name,trades=len(t),short_trades=int((t.side<0).sum()),net=float(t.net.sum()),raw_gross=float(t.gross.sum()+t.slippage_yuan.sum()),
            fees=float(t.fees.sum()),slippage=float(t.slippage_yuan.sum()),median_cost_to_risk=float((cost/t.planned_risk).median()),win_rate=float((t.net>0).mean()),
            payoff=pay,breakeven_win_rate=1/(1+pay),protective_exit_fraction=float(protective.mean()),first_trade=t.entry_time.min(),last_trade=t.exit_time.max()))
    pd.DataFrame(rows).to_csv(OUT/'prior_diagnostics.csv',index=False)
    text='''# 三轮失败的结构性复盘（第四版结果产生前）

难点真实存在，但此前的设计也限制了研究结论，不能把方法失败解释成市场没有优势。

1. **搜索数量不等于信息广度。** 188,160次候选窗口大多是同源价格信息、参数变体与相关拟合。三年约七百多个观察日，月度验证仅6个月/轮；相关品种、重叠训练窗不会创造独立样本。大量参数搜索会把偶然较好的训练片段送入组合。
2. **品种分散不等于方向分散。** 前两版68/68和77/77笔均为空，第三版50/62笔为空。每品种最多一条、最多三个品种并没有约束共同方向。不是说应该强行多空各半，而是此前没有做真实腿级风险对冲。
3. **成本重，但不能作为唯一解释。** 三版未计摩擦毛额分别−565、+1775、−535元，净额均亏。每笔摩擦/计划风险中位数约19%—23%。第二版的微弱毛优势无法负担执行；另外两版连已成交样本的毛优势也没有。
4. **学习目标和执行目标不一致。** 第三版用固定远期原始收益做回归标签，实际约75.8%交易被止损、跳空止损或账户风控提前终止。预测到持有终点上涨，不意味着途中可在600元计划风险里活到终点。下一版直接按真实扣费执行路径排序，不再用远期裸收益代理交易效用。
5. **停机保护了资金，也截断了证据。** 三版主要成交只覆盖2024年8—12月，首版8月30日已停机，不能都归咎于9月后的某一事件。后续0收益是持币而不是策略稳定，也不能推断后面7个季度都没有机会。不能通过事后放宽12%停机或重置账户修饰结果。
6. **执行认证仍不完整。** 费用为静态快照，保证金为20/25%占用场景，缺盘口和历史限价参数。把一字价都当不可成交、把不完整尾盘bar都延期，既保守又可能失真；不能通过补一个漂亮统计框架解决原始数据缺口。

## 本轮改变什么

只做一版预先固定的实质变化：从过去校准窗选择有共同波动的品种对和1—3手整数比率，实际交易两腿；信号方向、持有和止损从训练执行净额选择，既允许回归也允许延续，不指定哪种必须获胜。不是把期限结构或跨品种收益继续当成单腿预测因子。

为降低腿风险，只比较已知日夜盘时段相同的品种对。所有整数比率都登记，不能只保存最佳。对冲效果以过去月度方差降低估计，不能保证未来市场中性。手续费/滑点对每一腿、每一手分别扣除，不用理想价差成交或保证金净额抵扣。

保留原12%停机、5万元共享账户，绝不重置。另提前登记一份冻结信号的固定单位组合诊断账本：不共享资金、不声称满足组合资金约束、不绘制伪5万元账户曲线，也不参与选择。它只回答后续冻结信号是否仍有经济性，避免把“账户已停止”误判为“所有季度都失败”。两套证据最后统一释放。

所有历史已暴露；这是承接反馈后的自适应研究。新版本不是独立确认，也不承诺必须盈利。实际行情前向模拟仍是独立证据的必要下一步。
'''
    (OUT/'ANALYSIS.md').write_text(text,encoding='utf8')


def pair_frame(a,z,na,nb,specs,pa,pb):
    cols=['entry_ns','signal_ns','contract','entry_i','close','atr','month','volume','product']
    f=a[cols].merge(z[cols],on='entry_ns',suffixes=('_a','_b'),validate='one_to_one').sort_values('entry_ns').reset_index(drop=True)
    f['month']=f.month_a;f['spread']=f.close_a*na*specs[pa]['multiplier']-f.close_b*nb*specs[pb]['multiplier']
    group=(f.contract_a.ne(f.contract_a.shift())|f.contract_b.ne(f.contract_b.shift())).cumsum()
    for lb in LOOKBACK:
        mean=f.groupby(group).spread.transform(lambda x:x.shift().rolling(lb,min_periods=lb).mean())
        std=f.groupby(group).spread.transform(lambda x:x.shift().rolling(lb,min_periods=lb).std(ddof=1))
        f[f'z{lb}']=(f.spread-mean)/std.replace(0,np.nan)
    f['liquid']=(f.volume_a>=100*na)&(f.volume_b>=100*nb)
    return f


def hedge_search(frames,specs,start,cal_end,night):
    coverage={p:int(((f.entry_ns>=start)&(f.entry_ns<cal_end)).sum()) for p,f in frames.items()}
    universe=sorted(frames,key=lambda p:(-coverage[p],p))[:20];records=[];best=[]
    for pa,pb in itertools.combinations(sorted(universe),2):
        if night[pa+'0']!=night[pb+'0']:continue
        a=frames[pa].loc[(frames[pa].entry_ns>=start)&(frames[pa].entry_ns<cal_end),['entry_ns','close','contract','month']]
        z=frames[pb].loc[(frames[pb].entry_ns>=start)&(frames[pb].entry_ns<cal_end),['entry_ns','close','contract','month']]
        f=a.merge(z,on='entry_ns',suffixes=('_a','_b')).sort_values('entry_ns')
        same=f.contract_a.eq(f.contract_a.shift())&f.contract_b.eq(f.contract_b.shift())
        x=f.close_a.diff().where(same)*specs[pa]['multiplier'];y=f.close_b.diff().where(same)*specs[pb]['multiplier']
        local=[]
        for na,nb in itertools.product([1,2,3],repeat=2):
            ratios=[];corr=[];counts=[]
            for month in sorted(f.month_a.unique()):
                mask=(f.month_a==month)&x.notna()&y.notna();xx=x.loc[mask].to_numpy();yy=y.loc[mask].to_numpy();counts.append(len(xx))
                if len(xx)>1:
                    den=na**2*np.var(xx,ddof=1)+nb**2*np.var(yy,ddof=1)
                    if den>0:ratios.append(float(np.var(na*xx-nb*yy,ddof=1)/den))
            score=float(np.median(ratios)) if ratios else None
            r=dict(pair=f'{pa}__{pb}',a=pa,b=pb,qty_a=na,qty_b=nb,residual_variance_ratio=score,monthly_ratios=json.dumps(ratios),monthly_samples=json.dumps(counts))
            records.append(r);local.append(r)
        valid=[r for r in local if r['residual_variance_ratio'] is not None]
        if valid:
            r=min(valid,key=lambda q:(q['residual_variance_ratio'],q['qty_a']+q['qty_b'],q['qty_a']))
            if r['residual_variance_ratio']<1:best.append(r)
    best=sorted(best,key=lambda r:(r['residual_variance_ratio'],r['pair']))[:12]
    return universe,records,best


def scaled(t,qty):
    t=t.copy();t['planned_risk_per_lot']=t['planned_risk'];t['lots']=qty
    for key in ['net','gross','fees','open_fee','planned_risk','slippage_yuan']:t[key]*=qty
    return t


def align_exits(legs,raw,specs,end,slip,factor):
    """Standing independent leg stops; peer market exit at NEXT quote after trigger.

    No synthetic spread extreme or atomic midpoint execution is assumed.
    """
    legs=[t.copy() for t in legs];earliest=min(t['exit_ns'] for t in legs)
    first=min(legs,key=lambda t:t['exit_ns'])
    deadline=earliest if first['reason']=='stop' else earliest+c.STEP
    for t in legs:
        if t['exit_ns']<=earliest:continue
        a=raw[t['contract']];j=int(np.searchsorted(a.start_ns.to_numpy(),deadline));valid=a.valid.to_numpy()&(a.volume.to_numpy()>0)&(a.high.to_numpy()>a.low.to_numpy())
        possible=np.flatnonzero(valid[j:]&(a.start_ns.to_numpy()[j:]<end))
        if not len(possible):continue  # own protective order may close it sooner
        j+=int(possible[0]);tm=int(a.start_ns.iloc[j])
        if tm>=t['exit_ns']:continue
        price=float(a.open.iloc[j]-t['side']*slip*t['tick']);s=specs[t['product']];fc=float(b.fee(s,price,factor))*t['lots']
        gross=(price-t['entry'])*t['side']*t['multiplier']*t['lots']
        t.update(exit=price,exit_ns=tm,gross=gross,fees=t['open_fee']+fc,net=gross-t['open_fee']-fc,reason='peer_exit_next_quote')
    return legs


def pair_paths(f,choice,specs,raw,hold,k,end,slip=2,factor=1.,margin_rate=.20):
    tables=[]
    for suffix,p in [('a',choice['a']),('b',choice['b'])]:
        g=pd.DataFrame(dict(entry_ns=f.entry_ns,signal_ns=f[f'signal_ns_{suffix}'],contract=f[f'contract_{suffix}'],entry_i=f[f'entry_i_{suffix}'],
            close=f[f'close_{suffix}'],atr=f[f'atr_{suffix}'],month=f.month,product=p))
        tables.append(b.paths(g,raw,hold,k,specs[p],end,slip,factor))
    result={}
    for i in f.index[f.liquid]:
        for side in [-1,1]:
            a=tables[0].get((int(i),side));z=tables[1].get((int(i),-side))
            if a is None or z is None:continue
            if a['unknown'] or z['unknown']:
                result[int(i),side]=dict(unknown=True,entry_ns=int(f.entry_ns.iloc[i]),pair=choice['pair']);continue
            legs=[scaled(a,choice['qty_a']),scaled(z,choice['qty_b'])]
            if sum(t['planned_risk'] for t in legs)>900:continue
            if sum(t['entry']*t['multiplier']*t['lots']*margin_rate for t in legs)>(50000-sum(t['open_fee'] for t in legs))*.6:continue
            legs=align_exits(legs,raw,specs,end,slip,factor)
            result[int(i),side]=dict(unknown=False,pair=choice['pair'],entry_ns=int(f.entry_ns.iloc[i]),exit_ns=max(t['exit_ns'] for t in legs),
                legs=legs,net=sum(t['net'] for t in legs),gross=sum(t['gross'] for t in legs),fees=sum(t['fees'] for t in legs),planned_risk=sum(t['planned_risk'] for t in legs),
                # core.metrics extra-tick repricing: sum currency tick values over all lots.
                tick=1.,multiplier=sum(t['tick']*t['multiplier']*t['lots'] for t in legs),slippage_yuan=sum(t['slippage_yuan'] for t in legs),month=str(f.month.iloc[i]))
    return result


def indices(f,lb,threshold,orientation):
    value=f[f'z{lb}'].to_numpy();i=np.flatnonzero(np.isfinite(value)&(abs(value)>=threshold)&f.liquid.to_numpy());return i,(np.sign(value[i])*orientation).astype(int)


def isolated(i,side,table):
    tr=[];last=-1;unknown=0
    for idx,d in zip(i,side):
        t=table.get((int(idx),int(d)))
        if t is None or t['entry_ns']<=last:continue
        if t['unknown']:unknown+=1;break
        tr.append(t);last=t['exit_ns']
    return tr,unknown


def protocol():
    return dict(version='paired-execution-WFO-4',created=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),exposure='EXPOSED_ADAPTIVE_RESEARCH; previous3 versions188160 candidate-window evaluations. Older count unknown. Independent prospective0.',
        rationale='ANALYSIS.md and prior_diagnostics.csv: directional concentration, payoff-target mismatch, material costs and stop-censored evidence; paired paths are a genuinely new execution dimension, not proof of alpha',
        folds=c.rd(PRIOR/'prepared/folds.json'),inner='train9,test3,step3; first3 calibration chooses top20 liquidity coverage and up to12 same-session pairs by past monthly variance reduction; fixed integer hedge for all6 validation months and following3 test. Only train data selects instruments/parameters.',
        hedge='every same-session pair and all9 integer ratios1..3 x1..3 logged. Opposite legs; pick smallest median monthly residual variance ratio, break ties fewer lots. Pair benefit versus independent variances must be positive. No future PnL in pair preselection, no assumption of stationarity.',
        signal='actual cash spread qtyA*multA*closeA-qtyB*multB*closeB; rolling past-only mean/std within unchanged concrete contract pair. At completed synchronous5m quotes sampled05/35. Orientation+- learned, not forced reversion/trend.',
        grid=dict(lookback_observed30min_samples=LOOKBACK,abs_z=THRESHOLD,orientation=ORIENTATION,hold_observed5m=HOLDS,per_leg_ATR_stop=STOPS,configs_per_pair=72,max_pair_window_evaluations=6912,integer_ratio_searches='actual count retained; upper bound13680'),
        selection='same continuous utility as prior, computed directly from two-leg executable net paths (not raw forward return). Monthly trades assigned entry month; neighbor median adjacent lookback/threshold/hold/stop, fixed orientation. Cash0, max2 pairs without shared products; unknown paths ineligible. All losers and ratio trials saved.',
        execution='each leg integer1..3 lots, own concrete open plus2 adverse ticks/side; individual stops stand independently. First leg exit triggers peer liquidation NEXT executable quote (stop at bar end -> next open; gap/open exit -> following5m open), keep earlier own stop. Not atomic spread/midpoint fills; no synchronized-extreme stop.',
        limits='reuse standing-stop core: perlot plan<=600 incl3tick exit buffer, qty-weighted total<=900, prior bar volume>=100*qty, max3day clock and window last7days no opens/last3days liquidation requests. Unknown entry liquidity is unknown, not zero return.',
        account='50000 shared across8 folds, integer legs, no margin netting, margin20%baseline andmax60%equity,totalrisk900,09:00riskdayloss900halt,12%DDpermanent halt and next actual leg-open liquidation; no rearm/reset. Stress3ticks/side,2xfees,25%margin.',
        diagnostic='separate fixed-unit selected-pair opportunity ledger through all8 tests, each pair at most1 active bet; ignores shared capital and account halt. NOT a second50000 account, NOT tradable portfolio return, never used for selection. Both ledgers released only after all tests.',
        fees='20261022 snapshot +1yuan/side; max(close_today,close_yesterday) for every actual leg/lot. No certified historical broker schedules or limit/orderbook.',
        freeze='one batch only; train selections all frozen before formal and diagnostic evaluation, base and stress fixed. Historical feedback admitted. No continuation within batch to manufacture profit.',
        future='new-market simulation NOT_STARTED. No real orders. Hedging can break, legs can lock, gap loss can exceed600/lot and20%account DD.')


def freeze():
    assert not (OUT/'freeze.json').exists();b.check();analysis()
    tests=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p','test_paired_execution_wfo.py','-v'],capture_output=True,text=True)
    (OUT/'tests.txt').write_text(tests.stdout+tests.stderr,encoding='utf8');assert tests.returncode==0,tests.stderr
    c.js(OUT/'protocol.json',protocol());snapshot=OUT/'snapshot';snapshot.mkdir()
    paths=[Path(__file__),ROOT/'scripts/broad_session_wfo.py',ROOT/'scripts/daily_context_wfo.py',ROOT/'scripts/simple_wfo.py',ROOT/'tests/test_paired_execution_wfo.py',ROOT/'scripts/factor-study-v1.json',OUT/'protocol.json',OUT/'ANALYSIS.md',PRIOR/'prepared/manifest.json']
    for p in paths:shutil.copy2(p,snapshot/p.name)
    c.js(OUT/'freeze.json',dict(files={str(p.relative_to(ROOT)):c.sha(p) for p in paths},source_database_sha256=c.rd(PRIOR/'prepared/manifest.json')['database_sha256']))
    print('Fourth workflow and structural analysis frozen before new candidate results.',flush=True)


def check():
    for p,h in c.rd(OUT/'freeze.json')['files'].items():assert c.sha(ROOT/p)==h,p


def load():
    return {p.stem:pd.read_pickle(p) for p in (PRIOR/'prepared').glob('*.pkl')},c.rd(PRIOR/'prepared/specs.json')


def train():
    check();dest=OUT/'training';dest.mkdir(exist_ok=False);frames,specs=load();night=c.rd(ROOT/'scripts/factor-study-v1.json')['night_ends'];total=0;ratios=0
    for fold in c.rd(PRIOR/'prepared/folds.json'):
        fd=dest/f"fold_{fold['fold']:02d}";fd.mkdir();start=b.ns(fold['train']);end=b.ns(fold['test']);cal_end=int((pd.Timestamp(fold['train'],tz='Asia/Shanghai')+pd.DateOffset(months=3)).value)
        universe,trials,choices=hedge_search(frames,specs,start,cal_end,night);pd.DataFrame(trials).to_csv(fd/'all_integer_hedges.csv',index=False);ratios+=len(trials)
        c.js(fd/'pair_universe.json',dict(selected_products=universe,calibration_end=b.iso(cal_end),pairs=choices));records=[];cache={}
        months=pd.period_range(fold['train'],periods=9,freq='M').astype(str).tolist()[3:]
        for choice in choices:
            pa,pb=choice['a'],choice['b'];full=pair_frame(frames[pa],frames[pb],choice['qty_a'],choice['qty_b'],specs,pa,pb)
            f=full.loc[(full.entry_ns>=start)&(full.entry_ns<end)].reset_index(drop=True)
            raw=b.load_raw(set(f.contract_a)|set(f.contract_b));tables={(h,k):pair_paths(f,choice,specs,raw,h,k,end) for h,k in itertools.product(HOLDS,STOPS)}
            local=[]
            for ci,param in enumerate(GRID):
                lb,threshold,orientation,h,k=param;i,d=indices(f,lb,threshold,orientation);keep=f.month.iloc[i].isin(months).to_numpy();tr,unknown=isolated(i[keep],d[keep],tables[h,k]);m=c.metrics(tr,months)
                local.append(dict(pair=choice['pair'],candidate_id=f"{choice['pair']}-P{ci:02d}",param=list(param),lookback=lb,threshold=threshold,orientation=orientation,hold=h,stop=k,unknown=unknown,
                    **{key:value for key,value in m.items() if key!='monthly'},monthly_json=json.dumps(m['monthly'])))
            lookup={tuple(r['param']):r for r in local}
            for r in local:
                param=r['param'];values=[]
                for axis,grid in [(0,LOOKBACK),(1,THRESHOLD),(3,HOLDS),(4,STOPS)]:
                    ix=grid.index(param[axis])
                    for j in [ix-1,ix+1]:
                        if 0<=j<len(grid):
                            other=param.copy();other[axis]=grid[j];q=lookup[tuple(other)];values.append(min(q['net'],0) if q['unknown'] else q['net'])
                r['neighbor_median_net']=float(np.median(values));r['score']=utility(r['net'],r['neighbor_median_net'],r['median_month'],r['drawdown_yuan'],r['net']-r['extra_tick_net'],r['trades'])
                monthly=list(json.loads(r['monthly_json']).values());positive=sum(max(v,0.) for v in monthly)
                r['flags']=';'.join(flag for flag,yes in [('few_trades',r['trades']<30),('month_concentration',positive>0 and max(monthly)/positive>.5),('parameter_peak',r['net']>0 and r['neighbor_median_net']<=0),('extra_tick_sensitive',r['net']>0 and r['extra_tick_net']<=0),('unknown_path',r['unknown']>0)] if yes)
            records.extend(local);cache[choice['pair']]=choice;print('paired train',fold['fold'],choice['pair'],72,flush=True)
        df=pd.DataFrame(records);selected=[];used=set()
        if len(df):
            for _,r in df.loc[(df.score>0)&(df.unknown==0)].sort_values(['score','candidate_id'],ascending=[False,True]).iterrows():
                choice=cache[r['pair']]
                if {choice['a'],choice['b']}&used:continue
                selected.append(dict(choice=choice,param=r['param'],candidate_id=r['candidate_id'],score=float(r['score']),validation_net=float(r['net']),flags=r['flags']))
                used|={choice['a'],choice['b']}
                if len(selected)==2:break
            df.drop(columns='param').to_csv(fd/'all_candidates.csv',index=False)
        else:pd.DataFrame(columns=['pair','candidate_id','net','score','unknown']).to_csv(fd/'all_candidates.csv',index=False)
        total+=len(df);c.js(fd/'selection.json',dict(**fold,selected=selected,search_count=len(df),integer_hedge_trials=len(trials),empty_reason=None if selected else 'no_complete_positive_utility_pair'))
    c.js(dest/'manifest.json',dict(candidate_windows=total,integer_hedge_trials=ratios,files={str(p.relative_to(dest)):c.sha(p) for p in sorted(dest.rglob('*')) if p.is_file()}))
    print('ALL paired selections frozen. No account or unit diagnostic results seen.',flush=True)


def evaluate():
    check();manifest=c.rd(OUT/'training/manifest.json')
    for p,h in manifest['files'].items():assert c.sha(OUT/'training'/p)==h,p
    dest=OUT/'evaluation';dest.mkdir(exist_ok=False);c.js(dest/'started.json',dict(training_sha256=c.sha(OUT/'training/manifest.json'),frozen_unit_diagnostic=True))
    frames,specs=load();folds=c.rd(PRIOR/'prepared/folds.json')
    for label,slip,factor,margin in [('base',2,1.,.20),('stress',3,2.,.25)]:
        orders=collections.defaultdict(list);needed=set();unit=[];selections={}
        for fold in folds:
            fno=fold['fold'];sel=c.rd(OUT/f'training/fold_{fno:02d}/selection.json')['selected'];selections[fno]=sel
            for rank,r in enumerate(sel):
                q=r['choice'];pa,pb=q['a'],q['b'];full=pair_frame(frames[pa],frames[pb],q['qty_a'],q['qty_b'],specs,pa,pb)
                f=full.loc[(full.entry_ns>=b.ns(fold['test']))&(full.entry_ns<b.ns(fold['end']))].reset_index(drop=True)
                raw=b.load_raw(set(f.contract_a)|set(f.contract_b));needed.update(raw)
                lb,z,orientation,h,k=r['param'];table=pair_paths(f,q,specs,raw,h,k,b.ns(fold['end']),slip,factor,margin);i,d=indices(f,lb,z,orientation)
                tr,unknown=isolated(i,d,table)
                for t in tr:unit.append(dict(fold=fno,pair=q['pair'],candidate_id=r['candidate_id'],entry_ns=t['entry_ns'],exit_ns=t['exit_ns'],net=t['net'],fees=t['fees'],slippage=t['slippage_yuan'],diagnostic_only=True,shared_capital_applied=False))
                if unknown:unit.append(dict(fold=fno,pair=q['pair'],candidate_id=r['candidate_id'],net=None,diagnostic_only=True,unknown=True,shared_capital_applied=False))
                for idx,side in zip(i,d):
                    t=table.get((int(idx),int(side)))
                    if t:orders[t['entry_ns']].append((rank,r,t,fno))
        first=b.ns(folds[0]['test']);end=b.ns(folds[-1]['end']);raw=b.load_raw(needed);quotes=collections.defaultdict(dict)
        for code,f in raw.items():
            for row in f.loc[(f.start_ns>=first)&(f.start_ns<end),['start_ns','open','high','low','close','volume','valid']].itertuples():quotes[int(row.start_ns)][code]=row
        timeline=sorted(set(quotes)|set(orders)|set().union(*(set(f.entry_ns.loc[(f.entry_ns>=first)&(f.entry_ns<end)]) for f in frames.values())))
        result=account(timeline,quotes,orders,folds,specs,slip,factor,margin)
        for key in ['pairs','legs','equity','rejections']:pd.DataFrame(result.pop(key)).to_csv(dest/f'{label}_{key}.csv',index=False)
        pd.DataFrame(unit,columns=['fold','pair','candidate_id','entry_ns','exit_ns','net','fees','slippage','diagnostic_only','shared_capital_applied','unknown']).to_csv(dest/f'{label}_unit_diagnostic.csv',index=False)
        c.js(dest/f'{label}_summary.json',dict(**result,selections=selections))
    c.js(dest/'complete.json',dict(files={p.name:c.sha(p) for p in sorted(dest.iterdir()) if p.is_file()}));print('ALL formal account and fixed-unit diagnostics finished. Aggregate results may now be released.',flush=True)


def account(timeline,quotes,orders,folds,specs,slip=2,factor=1.,margin_rate=.20):
    cash=50000.;peak=cash;halt=False;dayhalt=False;daykey=None;daystart=cash;active={};pairs=[];legs=[];snap=[];rejected=[];unknown=[]
    intervals=[(b.ns(f['test']),b.ns(f['end']),f['fold']) for f in folds]
    def equity():return cash+sum((leg['mark']-leg['t']['entry'])*leg['t']['side']*leg['t']['multiplier']*leg['t']['lots'] for p in active.values() for leg in p['legs'] if not leg['closed'])
    def open_legs():return [leg for p in active.values() for leg in p['legs'] if not leg['closed']]
    for tm in timeline:
        fno=next((n for lo,hi,n in intervals if lo<=tm<hi),None)
        if fno is None:continue
        bars=quotes.get(tm,{})
        for leg in open_legs():
            row=bars.get(leg['t']['contract'])
            if row is not None:leg['mark']=row.open
        stamp=pd.Timestamp(tm,tz='UTC').tz_convert('Asia/Shanghai');key=str((stamp-pd.Timedelta(hours=9)).date())
        if key!=daykey:daykey=key;daystart=equity();dayhalt=False
        eq=equity();peak=max(peak,eq);halt|=eq<=peak*.88;dayhalt|=daystart-eq>=900;riskhalt=halt or dayhalt
        for rank,r,t,entryfold in sorted(orders.get(tm,[]),key=lambda x:(x[0],x[1]['candidate_id'])):
            pair=t['pair'];q=r['choice'];products={q['a'],q['b']};used={leg['t']['product'] for leg in open_legs()}
            risk=sum(leg['t']['planned_risk'] for leg in open_legs());margin=sum(leg['mark']*leg['t']['multiplier']*leg['t']['lots']*margin_rate for leg in open_legs());eq=equity()
            if products&used or pair in active:reason='active_product_or_pair'
            elif riskhalt:reason='account_halt'
            elif t['unknown']:unknown.append(dict(time=b.iso(tm),pair=pair));continue
            elif risk+t['planned_risk']>900:reason='shared_risk'
            elif margin+sum(leg['entry']*leg['multiplier']*leg['lots']*margin_rate for leg in t['legs'])>(eq-sum(leg['open_fee'] for leg in t['legs']))*.6:reason='margin'
            else:reason=None
            if reason:rejected.append(dict(fold=fno,time=b.iso(tm),pair=pair,reason=reason));continue
            cash-=sum(leg['open_fee'] for leg in t['legs']);active[pair]=dict(candidate_id=r['candidate_id'],entry_fold=entryfold,entry_ns=t['entry_ns'],
                legs=[dict(t=leg.copy(),mark=leg['entry']-leg['side']*slip*leg['tick'],closed=False) for leg in t['legs']])
        for pair,p in list(active.items()):
            for leg in p['legs']:
                if leg['closed']:continue
                t=leg['t'];row=bars.get(t['contract']);close=t['exit_ns']<=tm or (t['reason']=='stop' and t['exit_ns']==tm+c.STEP)
                if riskhalt and tm>t['entry_ns'] and row is not None and row.valid and row.volume>0 and row.high>row.low:
                    price=row.open-t['side']*slip*t['tick'];fc=float(b.fee(specs[t['product']],price,factor))*t['lots'];gross=(price-t['entry'])*t['side']*t['multiplier']*t['lots']
                    t.update(exit=float(price),exit_ns=tm,fees=t['open_fee']+fc,gross=float(gross),net=float(gross-t['open_fee']-fc),reason='account_risk_exit');close=True
                if close:
                    cash+=t['net']+t['open_fee'];leg['closed']=True;legs.append(dict(**t,pair=pair,candidate_id=p['candidate_id'],entry_fold=p['entry_fold'],exit_fold=fno,entry_time=b.iso(t['entry_ns']),exit_time=b.iso(t['exit_ns'])))
                elif row is not None:leg['mark']=row.close
            if all(leg['closed'] for leg in p['legs']):
                ts=[leg['t'] for leg in p['legs']];pairs.append(dict(pair=pair,candidate_id=p['candidate_id'],entry_fold=p['entry_fold'],exit_fold=fno,
                    entry_ns=p['entry_ns'],exit_ns=max(t['exit_ns'] for t in ts),net=sum(t['net'] for t in ts),gross=sum(t['gross'] for t in ts),fees=sum(t['fees'] for t in ts),
                    slippage_yuan=sum(t['slippage_yuan'] for t in ts),planned_risk=sum(t['planned_risk'] for t in ts),entry_time=b.iso(p['entry_ns']),exit_time=b.iso(max(t['exit_ns'] for t in ts))));del active[pair]
        eq=equity();peak=max(peak,eq);halt|=eq<=peak*.88;dayhalt|=daystart-eq>=900
        snap.append(dict(time_ns=tm+c.STEP,time=b.iso(tm+c.STEP),fold=fno,balance=cash,equity=eq,peak=peak,drawdown=(peak-eq)/peak,halted=bool(halt),
            planned_risk=sum(leg['t']['planned_risk'] for leg in open_legs()),margin=sum(leg['mark']*leg['t']['multiplier']*leg['t']['lots']*margin_rate for leg in open_legs())))
    if active:unknown.append(dict(reason='unfinished_legs',pairs=list(active)))
    return dict(pairs=pairs,legs=legs,equity=snap,rejections=rejected,unknown=unknown,final_equity=cash if not unknown else None)


def report():
    check()
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    manifest=c.rd(OUT/'training/manifest.json');rows=[];selections=[];stats={};unitrows=[];fig,ax=plt.subplots(figsize=(12,4.5))
    for fold in c.rd(PRIOR/'prepared/folds.json'):
        selected=c.rd(OUT/f"training/fold_{fold['fold']:02d}/selection.json")['selected']
        for r in selected:selections.append(dict(fold=fold['fold'],pair=r['choice']['pair'],qty_a=r['choice']['qty_a'],qty_b=r['choice']['qty_b'],param_json=json.dumps(r['param']),validation_net=r['validation_net'],utility=r['score'],flags=r['flags']))
    pd.DataFrame(selections,columns=['fold','pair','qty_a','qty_b','param_json','validation_net','utility','flags']).to_csv(OUT/'selected_parameters.csv',index=False)
    specs=c.rd(PRIOR/'prepared/specs.json')
    for label,factor in [('base',1.),('stress',2.)]:
        summary=c.rd(OUT/f'evaluation/{label}_summary.json');eq=pd.read_csv(OUT/f'evaluation/{label}_equity.csv')
        def read_optional(key):
            p=OUT/f'evaluation/{label}_{key}.csv';return pd.read_csv(p) if p.stat().st_size>3 else pd.DataFrame()
        pair=read_optional('pairs');leg=read_optional('legs');unit=read_optional('unit_diagnostic')
        if len(leg):
            assert leg.lots.between(1,3).all() and (leg.lots==leg.lots.astype(int)).all();assert (leg.planned_risk_per_lot<=600+1e-7).all()
            np.testing.assert_allclose(leg.gross,(leg.exit-leg.entry)*leg.side*leg.multiplier*leg.lots,atol=1e-6);np.testing.assert_allclose(leg.net,leg.gross-leg.fees,atol=1e-6)
            fees=[float(b.fee(specs[t.product],t.entry,factor,True)+b.fee(specs[t.product],t.exit,factor))*t.lots for t in leg.itertuples()];np.testing.assert_allclose(leg.fees,fees,atol=1e-6)
            assert abs(50000+leg.net.sum()-eq.balance.iloc[-1])<1e-6
        assert (eq.planned_risk<=900+1e-6).all();assert eq.time_ns.is_unique and eq.time_ns.is_monotonic_increasing
        wins=pair.loc[pair.net>0].net if len(pair) else [];losses=pair.loc[pair.net<0].net if len(pair) else []
        halted=eq.loc[eq.halted];stats[label]=dict(final_equity=summary['final_equity'],net=summary['final_equity']-50000 if summary['final_equity'] is not None else None,
            max_drawdown=float(eq.drawdown.max()),pair_trades=len(pair),leg_trades=len(leg),win_rate=float((pair.net>0).mean()) if len(pair) else None,
            payoff=float(np.mean(wins)/-np.mean(losses)) if len(wins) and len(losses) else None,unknown=summary['unknown'],first_halt=str(halted.iloc[0].time) if len(halted) else None,
            perlot_losses_beyond600=int((leg.net/leg.lots< -600).sum()) if len(leg) else 0)
        previous=50000.
        for f in c.rd(PRIOR/'prepared/folds.json'):
            es=eq.loc[eq.fold==f['fold']];tr=pair.loc[pair.exit_fold==f['fold']] if len(pair) else []
            curve=np.r_[previous,es.equity.to_numpy()];peak=np.maximum.accumulate(curve);ending=float(curve[-1]);sel=summary['selections'][str(f['fold'])]
            win=tr.loc[tr.net>0].net if len(tr) else [];loss=tr.loc[tr.net<0].net if len(tr) else []
            rows.append(dict(scenario=label,fold=f['fold'],test_start=f['test'],test_end=f['end'],selected_pairs=','.join(r['choice']['pair'] for r in sel),net=ending-previous,max_drawdown=float(((peak-curve)/peak).max()),
                pair_trades=len(tr),win_rate=float((tr.net>0).mean()) if len(tr) else None,payoff=float(np.mean(win)/-np.mean(loss)) if len(win) and len(loss) else None,halted=bool(es.halted.iloc[-1]) if len(es) else False));previous=ending
            u=unit.loc[unit.fold==f['fold']] if len(unit) else []
            unitrows.append(dict(scenario=label,fold=f['fold'],diagnostic_only=True,shared_capital_applied=False,unit_pair_trades=int(u.net.notna().sum()) if len(u) else 0,
                unit_net=float(u.net.sum()) if len(u) and u.net.notna().all() else (0. if not len(u) else None)))
        ax.plot(pd.to_datetime(eq.time,utc=True),eq.equity,label=label)
    ax.axhline(50000,color='gray',lw=.7);ax.set_ylabel('Shared account equity CNY');ax.grid(alpha=.2);ax.legend();fig.tight_layout();fig.savefig(OUT/'oos_equity.png',dpi=160);plt.close(fig)
    pd.DataFrame(rows).to_csv(OUT/'fold_summary.csv',index=False);pd.DataFrame(unitrows).to_csv(OUT/'unit_diagnostic_summary.csv',index=False);c.js(OUT/'reconciliation.json',dict(scenarios=stats,integer_legs_fees_and_account_reconciled=True))
    lines=['# 第四轮：结构性复盘后的双腿执行研究','',
        '**全部历史为研究暴露数据；本版根据前三版反馈改变研究维度，不是独立确认。深刻复盘见ANALYSIS.md。**','',
        '|情景|期末权益|净收益|5分钟最大盯市回撤|完整双腿交易|单腿记录|胜率|盈亏比|','|---|---:|---:|---:|---:|---:|---:|---:|']
    fmt=lambda x:'未知' if x is None else f'{x:,.2f}'
    for label,s in stats.items():lines.append(f"|{label}|{fmt(s['final_equity'])}|{fmt(s['net'])}|{s['max_drawdown']:.2%}|{s['pair_trades']}|{s['leg_trades']}|{'—' if s['win_rate'] is None else format(s['win_rate'],'.2%')}|{fmt(s['payoff'])}|")
    lines+=['','|轮次|测试开始|训练选中组合|净额|轮内回撤|完整组合交易数|胜率|盈亏比|停机|','|---|---|---|---:|---:|---:|---:|---:|---|']
    for r in rows:
        if r['scenario']=='base':lines.append(f"|{r['fold']}|{r['test_start']}|{r['selected_pairs'] or '空选'}|{r['net']:.2f}|{r['max_drawdown']:.2%}|{r['pair_trades']}|{'—' if r['win_rate'] is None else format(r['win_rate'],'.1%')}|{fmt(r['payoff'])}|{r['halted']}|")
    if any(s['unknown'] for s in stats.values()):conclusion='存在未知成交或未结束单腿，完整账户收益不能确认。已知部分不能当作可交易盈利。'
    elif all(s['pair_trades']==0 for s in stats.values()):conclusion='没有找到可准入的正效用组合，现金0收益不构成盈利证据。'
    elif all(s['net']>0 for s in stats.values()):conclusion='固定两成本场景均为正，但尚只是暴露历史候选，必须接受新行情前向模拟，不能宣称已泛化或可实盘。'
    else:conclusion='本版仍未形成可确认的稳定正收益账户流程。不在看过测试后继续改动规则制造盈利。'
    lines+=['',conclusion,'',
        f"完整保存{manifest['candidate_windows']:,}次候选窗口、{manifest['integer_hedge_trials']:,}次整数对冲比率比较。每一笔费用、滑点、保证金按两腿及实际手数计算。最多2对不共享品种，账户仍为5万元、计划每手≤600、同时在险≤900、保证金≤权益60%、12%永久停机且跨轮不重置。",'',
        '信号过去滚动均值/标准差对应12/48/144个同步30分钟样本，同合约对内预热，任一腿换月重置；开仓区间1或2倍标准差，方向±均搜索。不是强制均值回归，也不宣称价差平稳。整数比率仅在各轮最初3个月按月度残差方差降低选择，固定后做6个月验证及3个月测试；不根据未来收益挑对。','',
        '独立腿保护止损按每手3/6倍当时ATR向外取整，两腿总计划风险超过900或单腿每手超过600不交易。单腿先退出后，另一腿在随后实际可执行开盘退出；保留更早发生的自身保护止损。既无理想价差极值止损，也没有把两腿按中间价同时成交。股指/商品共同风险降低是过去估计，不保证未来中性；裸露单腿、锁板或跳空仍可突破每手600和账户20%回撤。','',
        '基准每腿每边2跳、压力3跳及双倍费用，平仓费取平今/平昨较高者，另每边每手1元佣金；20%/25%保证金不净额抵扣。它们是条件成本场景，缺三年实际逐日经纪商费率/保证金、盘口和历史限价。不能宣称完成真实执行认证。','',
        '## 冻结信号诊断账本（不是资金账户）','',
        '另有unit_diagnostic_summary.csv及evaluation/*_unit_diagnostic.csv，按每轮训练选定的固定整数对记录全8轮潜在交易。每个组合自身不重叠，但不应用共享账户、跨组合资金或停机；不能拼成第二条5万元曲线，不能当作满足20%回撤约束的收益。它只诊断账户停机后信号是否继续失效，不参与任何选择或参数改动。','',
        '|轮次|基准固定单位组合笔数|单位净额（非账户）|','|---|---:|---:|']
    for r in unitrows:
        if r['scenario']=='base':lines.append(f"|{r['fold']}|{r['unit_pair_trades']}|{fmt(r['unit_net'])}|")
    lines+=['','## 文件与复现','',
        '- ANALYSIS.md、prior_diagnostics.csv：新试验前写入的结构复盘和数值依据。\n- protocol.json、freeze.json、snapshot、tests.txt：新试验前冻结的规则、代码和测试。\n- training/fold_XX/all_integer_hedges.csv：包括败例的全部整数比率比较。\n- training/fold_XX/all_candidates.csv、selection.json：全部候选与冻结选择。\n- selected_parameters.csv、fold_summary.csv：参数及各轮正式账户指标。\n- evaluation/*_pairs.csv、*_legs.csv、*_equity.csv：完整组合、每腿成交和5万元连续账户曲线。\n- unit_diagnostic_summary.csv：明确非账户的诊断，不与正式收益混用。','',
        '顺序python scripts/paired_execution_wfo.py freeze → train → evaluate → report。输入复用第三版不可变具体合约数据，输出拒绝覆盖。所有选择先冻结，正式账户与诊断账本在所有8轮完成后统一释放。新行情前向模拟尚未启动。']
    (OUT/'REPORT.md').write_text('\n'.join(lines),encoding='utf8');c.js(OUT/'paper_forward.json',dict(status='NOT_STARTED',independent_prospective_count=0,freeze_sha256=c.sha(OUT/'freeze.json'),live_orders=False))
    c.js(OUT/'delivery_manifest.json',dict(files={str(p.relative_to(OUT)):c.sha(p) for p in sorted(OUT.rglob('*')) if p.is_file() and p.name!='delivery_manifest.json'}));print(json.dumps(stats,ensure_ascii=False,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('stage',choices=['analysis','freeze','train','evaluate','report']);args=parser.parse_args();globals()[args.stage]()
