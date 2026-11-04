"""Predeclared two-track nested chronological research; no outer-driven tuning."""
import hashlib,itertools,json,pathlib,time
import numpy as np
import pandas as pd
from fresh_year_research import prepare,trades_for,portfolio,OUT
from fresh_year_audit import audit_frozen

DEST=OUT/'chronological_v1'

def intraday_features(d):
    x=dict(d);f=pd.DataFrame({k:d[k] for k in ('start_ns','open','high','low','close','contract','date')})
    group=((f.start_ns.diff()>6*3600e9)|(f.date!=f.date.shift())|(f.contract!=f.contract.shift())).cumsum()
    prev=f.close.groupby(group).shift();tr=pd.concat([f.high-f.low,(f.high-prev).abs(),(f.low-prev).abs()],axis=1).max(axis=1)
    x['atr']=tr.groupby(group).transform(lambda s:s.rolling(4).mean().shift()).to_numpy()
    for n in (4,8):
        for col,op,label in [('high','max','hi'),('low','min','lo'),('close','mean','mean'),('close','std','std')]:
            x[f'{label}{n}']=f[col].groupby(group).transform(lambda s:getattr(s.rolling(n),op)().shift()).to_numpy()
        change=f.close.groupby(group).diff().abs()
        displacement=(f.close-f.close.groupby(group).shift(n)).abs()
        x[f'er{n}']=(displacement/change.groupby(group).transform(lambda s:s.rolling(n).sum())).to_numpy()
    return x

def signal_for(d,cfg):
    family,n,k,stop,reward,hold,swing,cap=cfg
    if family=='breakout':sig=np.where(d['close']>d[f'hi{n}'],1,np.where(d['close']<d[f'lo{n}'],-1,0))
    else:
        z=(d['close']-d[f'mean{n}'])/np.maximum(d[f'std{n}'],d['tick'])
        sig=np.where(z>k,1,np.where(z<-k,-1,0))
        if family=='revert':sig=-sig
    # Efficiency is causal at the signal close. Filter definition is fixed before outer evaluation.
    er=d[f'er{n}'];valid=(er<=.35) if family=='revert' else (er>=.25)
    valid&=np.isfinite(er)
    sig=np.where(valid,sig,0)
    # One entry opportunity per uninterrupted directional signal episode.
    sig=np.where(sig!=np.r_[0,sig[:-1]],sig,0)
    # Predeclared minimum distance/cost ratio; uses prior ATR and current observed close.
    distance=np.ceil(d['atr']*stop/d['tick'])*d['tick']
    fee=np.maximum(5,d['close']*d['mult']*.00015)
    # At signal close, next entry ATR is the rolling value for the following bar.
    next_distance=np.r_[distance[1:],np.nan]
    sig=np.where((2*fee+2*d['tick']*d['mult'])<=.2*next_distance*d['mult'],sig,0)
    return sig

def configs():
    out=[]
    for family,n,stop,reward,hold in itertools.product(('revert',),(4,8),(1.5,2.),(1.5,2.),(4,8)):
        for k in (1.5,2.):out.append(dict(track='intraday',config=(family,n,k,stop,reward,hold,False,450)))
    for n,stop,reward,hold in itertools.product((4,8),(1.5,2.),(1.5,2.),(4,8)):
        out.append(dict(track='intraday',config=('breakout',n,2.,stop,reward,hold,False,450)))
    for n,k,stop,reward,hold in itertools.product((64,128),(1.5,2.),(2.,3.),(2.,4.),(64,128)):
        out.append(dict(track='swing',config=('momentum',n,k,stop,reward,hold,True,450)))
    for n,stop,reward,hold in itertools.product((32,64),(2.,3.),(2.,4.),(64,128)):
        out.append(dict(track='swing',config=('breakout',n,2.,stop,reward,hold,True,450)))
    return out

def fit_candidates(base,candidates,cache,fit_months):
    ranked=[]
    for cid,candidate in enumerate(candidates):
        rankings=[]
        for nm,trs in cache[cid].items():
            tt=[t for t in trs if t[10] in fit_months]
            monthly=np.array([sum(t[6] for t in tt if t[10]==m) for m in fit_months])
            score=float(monthly.sum()-.5*monthly.std())
            if len(tt)>=4 and (monthly>0).sum()>=2 and score>0 and all(t[6]>=-600 for t in tt):rankings.append((score,nm))
        universe=[nm for _,nm in sorted(rankings,reverse=True)[:6]]
        if len(universe)<2:continue
        trs={nm:cache[cid][nm] for nm in universe}
        stats,rows,_=portfolio({nm:base[nm] for nm in universe},trs,fit_months)
        if stats['trades']<12 or stats['net']<=0 or stats['max_drawdown_pct']>15:continue
        score=stats['net']-np.std([stats['monthly'][m] for m in fit_months])
        ranked.append(dict(candidate=cid,track=candidate['track'],config=candidate['config'],universe=universe,fit=stats,fit_score=float(score)))
    return sorted(ranked,key=lambda r:r['fit_score'],reverse=True)

def select_for_window(base,candidates,cache,fit_months,inner_months,track):
    shortlist=[r for r in fit_candidates(base,candidates,cache,fit_months) if r['track']==track][:3]
    eligible=[]
    for row in shortlist:
        trs={nm:cache[row['candidate']][nm] for nm in row['universe']}
        stats,tt,_=portfolio({nm:base[nm] for nm in row['universe']},trs,inner_months)
        row['inner_validation']=stats
        if stats['net']>0 and stats['trades']>=12 and stats['max_drawdown_pct']<=15 and sum(stats['monthly'][m]>0 for m in inner_months)>=2 and all(t['pnl']>=-600 for t in tt):eligible.append(row)
    selected=max(eligible,key=lambda r:r['inner_validation']['net']) if eligible else None
    return dict(fit_months=fit_months,inner_months=inner_months,shortlist=shortlist,selected=selected,action='trade_selected' if selected else 'cash')

def run(candidates=None,signal_builder=None,destination=DEST,protocol_overrides=None):
    global DEST
    DEST=destination
    DEST.mkdir(parents=True,exist_ok=True);candidates=configs() if candidates is None else candidates
    if signal_builder is None:signal_builder=lambda d,c:signal_for(d,c['config'])
    folds=[dict(fold=i+1,fit=list(range(s,s+3)),inner=list(range(s+3,s+6)),outer=list(range(s+6,s+9))) for i,s in enumerate(range(0,18,3))]
    protocol=dict(version=1,development_window=['2024-10-28','2026-10-28'],data_already_observed=True,unopened_historical_challenge=['2024-04-28','2024-10-28'],challenge_is_backward_not_future=True,folds=folds,candidates=candidates,selection='Each 6-month training window: first 3 months rank products and configs; next 3 months select among top 3 training candidates per track. Next 3 months outer evaluation.',products='All 22 products in archive, no prior selected four restriction; rank on first 3 months only; <=6 products; >=2 profitable fit months, >=4 trades each, >=2 products.',inner_eligibility='>=12 trades, positive total net, >=2 positive months, <=15% close/intrabar coarse drawdown, no actual loss below -600; no eligible candidate means cash.',cost='Same commission max(5, notional*.00015) each side and one tick slippage per side.',capital=50000,margin=.2,entry_margin_limit=.8,lot_risk=450,portfolio_risk=1800,entry='First opportunity of each uninterrupted filtered signal episode; signal-side estimated round-trip costs <=20% initial stop amount.',intraday='Only current date/session block data, reset after >6h gap or contract change; ATR4; n4/8; max 4/8 bars and flat before recorded gap>30m, date, month, or contract change.',swing='ATR20, n32/64/128, hold64/128; monthly and contract flattening.',regime='ER(n)=abs(close-close[n])/sum(abs(diff(close)),n); revert <=.35, momentum/breakout >=.25.',outer_gate='18-month continuous return CAGR >=20%, conservative 1m DD<=20%, no trade loss >600, >=4 of 6 independently funded folds profitable, >=60 outer trades, double-cost outer net positive, net after removing 10 largest winners positive.',final_selection='Track highest stitched outer net among tracks passing all gates. Latest 6-month nested fit freezes final config before opening older historical challenge. If no track passes, leave challenge unopened.',source_sha256=hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest())
    protocol.update(protocol_overrides or {})
    (DEST/'protocol.json').write_text(json.dumps(protocol,indent=2),encoding='utf8')
    print('PREDECLARED',len(candidates),'configurations',flush=True)
    base,p=prepare(start='2024-10-28',endday='2026-10-28',output_dir=DEST/'data_manifest')
    day={nm:intraday_features(d) for nm,d in base.items()};cache={};began=time.time()
    for cid,candidate in enumerate(candidates):
        data=day if candidate['track']=='intraday' else base;cfg=candidate['config']
        cache[cid]={nm:trades_for(d,cfg,signal_override=signal_builder(d,candidate)) for nm,d in data.items()}
        if cid%16==0:print('GENERATE',cid,'seconds',round(time.time()-began),flush=True)
    selections={track:[] for track in ('intraday','swing')}
    for track in selections:
        for fold in folds:
            choice=select_for_window(base,candidates,cache,fold['fit'],fold['inner'],track)
            choice.update(fold=fold['fold'],outer_months=fold['outer']);selections[track].append(choice)
            print('FREEZE',track,fold['fold'],choice['action'],flush=True)
    # Persist every outer decision before accessing any outer performance.
    (DEST/'frozen_outer_decisions.json').write_text(json.dumps(selections,indent=2),encoding='utf8')
    results={};audits={}
    for track,choices in selections.items():
        directory=DEST/track;directory.mkdir(exist_ok=True);sources={nm:[] for nm in base};rewards={};foldstats=[]
        for choice in choices:
            selected=choice['selected'];trs={nm:[] for nm in base}
            if selected:
                for nm in selected['universe']:
                    tt=[t for t in cache[selected['candidate']][nm] if t[10] in choice['outer_months']]
                    trs[nm]=tt;sources[nm].extend(tt)
                    for tr in tt:rewards[(nm,tr[0])]=selected['config'][4]
            stats,rr,_=portfolio(base,trs,choice['outer_months']);stats.update(fold=choice['fold'],action=choice['action'],selected=selected,outer_months=choice['outer_months']);foldstats.append(stats)
        active=[nm for nm,tt in sources.items() if tt]
        if active:
            descriptor=dict(config=None,universe=active,policy='nested chronological per frozen_outer_decisions.json',track=track)
            audit=audit_frozen({nm:base[nm] for nm in active},p,descriptor,directory,segments=[('outer_oos',list(range(6,24)))],trade_sources=sources,reward_by_entry=rewards)
            stats=audit['outer_oos'];rr=pd.read_csv(directory/'audited_outer_oos_trades.csv');stress=audit['outer_oos_stress'];audits[track]=audit
            positive=rr.loc[rr.pnl>0,'pnl'].sort_values(ascending=False)
            stats.update(annualized_return_pct=((1+stats['return_pct']/100)**(12/18)-1)*100,profitable_folds=sum(s['net']>0 for s in foldstats),active_folds=sum(s['action']=='trade_selected' for s in foldstats),net_without_top10=float(stats['net']-positive.head(10).sum()),double_cost_net=float(stress['net_less_extra_commission']))
            stats['passed']=not audit['minute_coverage_issues'] and stats['annualized_return_pct']>=20 and stats['conservative_minute_drawdown_pct']<=20 and stats['losses_over_600']==0 and stats['profitable_folds']>=4 and stats['trades']>=60 and stats['double_cost_net']>0 and stats['net_without_top10']>0
        else:stats=dict(net=0,return_pct=0,trades=0,annualized_return_pct=0,profitable_folds=0,active_folds=0,passed=False,note='All folds stayed in cash.')
        results[track]=dict(summary=stats,folds=foldstats)
        print('OUTER',track,json.dumps(stats),flush=True)
    (DEST/'outer_results.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    eligible=[track for track,r in results.items() if r['summary']['passed']]
    decision=dict(eligible_tracks=eligible,challenge_opened=False)
    if eligible:
        winner=max(eligible,key=lambda track:results[track]['summary']['net'])
        final=select_for_window(base,candidates,cache,list(range(18,21)),list(range(21,24)),winner)
        decision.update(winner_track=winner,final_choice=final)
        (DEST/'frozen_challenge_decision.json').write_text(json.dumps(decision,indent=2),encoding='utf8')
        if final['selected']:
            frozen=final['selected'];sub=DEST/'historical_challenge';sub.mkdir(exist_ok=True)
            challenge,cp=prepare(start='2024-04-28',endday='2024-10-28',names=frozen['universe'],output_dir=sub)
            transformed={nm:intraday_features(d) for nm,d in challenge.items()} if winner=='intraday' else challenge
            cfg=frozen['config'];tt={nm:trades_for(d,cfg,signal_override=signal_builder(d,candidates[frozen['candidate']])) for nm,d in transformed.items()}
            rw={(nm,tr[0]):cfg[4] for nm,trs in tt.items() for tr in trs}
            challenge_audit=audit_frozen(challenge,cp,dict(config=cfg,universe=frozen['universe'],track=winner),sub,segments=[('six_months',list(range(6)))],trade_sources=tt,reward_by_entry=rw)
            decision.update(challenge_opened=True,challenge_result=challenge_audit['six_months'])
    (DEST/'decision.json').write_text(json.dumps(decision,indent=2),encoding='utf8')
    write_report(protocol,results,decision,p)

def write_report(protocol,results,decision,p):
    counts={track:sum(c['track']==track for c in protocol['candidates']) for track in ('intraday','swing')}
    lines=['# 两条路线的按时间前推研究','','这是一套在已观察历史上重新规定流程的研究。外层时间顺序没有使用未来收益选当期品种和参数，但这些历史区间已在前轮看过，因此不能称为新的、完全独立的样本外证据。尚未读取的2024-04-28至2024-10-27仅在预设外层门槛通过后作额外历史挑战，仍不等于未来实盘验证。','',f"每折先用三个月筛选，再用紧随的三个月验证选优，之后的三个月作为外层检验。日内{counts['intraday']}组、跨日{counts['swing']}组配置；每折每路线最多3个候选进入内层验证，无合格候选则持有现金。未沿用原来四品种限制，所有22个品种只在训练阶段筛选。全部外层决策先落盘，再计算外层表现。",'',protocol.get('hypotheses',''),'', '| 路线 | 18个月实际收益率 | 年化收益率 | 交易数 | 盈利折数 | 是否达到预设门槛 |','|---|---:|---:|---:|---:|---|']
    for track,r in results.items():
        s=r['summary'];lines.append(f"| {track} | {s['return_pct']:.2f}% | {s['annualized_return_pct']:.2f}% | {s['trades']} | {s['profitable_folds']}/6 | {s['passed']} |")
    lines+=['','收益率使用五万元起始、外层18个月连续模拟；各折表格另按每折五万元独立起算，用于观察不同阶段表现。具体指标和持仓期限见下述协议。两者均单品种最多一手，计划风险450元，组合计划风险1800元，保证金20%、准入占用上限80%。','',protocol['intraday'],'',protocol['swing'],'','## 各外层季度','', '| 路线 | 日期区间（结束日不含） | 动作 | 独立季度净利润/元 | 收益率 |','|---|---|---|---:|---:|']
    for track,r in results.items():
        for f in r['folds']:
            first,last=f['outer_months'][0],f['outer_months'][-1]+1
            lines.append(f"| {track} | {p['blocks'][first][:10]}～{p['blocks'][last][:10]} | {f['action']} | {f['net']:,.2f} | {f['return_pct']:.2f}% |")
    lines+=['','## 执行复核与风险','']
    for track,r in results.items():
        s=r['summary']
        if not s['trades']:lines.append(f'- {track}：没有入选并交易的外层季度，不能用零回撤证明存在盈利能力。');continue
        lines.append(f"- {track}：保守一分钟回撤{s['conservative_minute_drawdown_pct']:.2f}%；最大单笔亏损{-s['worst_trade']:.2f}元；单笔亏损超过600元共{s['losses_over_600']}笔；手续费滑点均加倍后净利润{s['double_cost_net']:,.2f}元；扣除最大10笔盈利后净利润{s['net_without_top10']:,.2f}元。")
    lines+=['','外层门槛在读取结果前固定：年化收益不低于20%、分钟回撤不超过20%、没有单笔亏损超过600元、至少4/6折盈利、至少60笔交易、双倍成本后盈利、扣除最大10笔盈利后仍盈利。隔夜路线不保证未来实际损失能被固定止损金额封顶。','',f"额外历史挑战是否打开：**{decision['challenge_opened']}**。若两条路线均未通过，不以放宽门槛、强迫入场或查看保留区间寻找借口。",'', '完整配置与门槛见 protocol.json，每折拟合、内层验证及选定规则见 frozen_outer_decisions.json；分钟复核、逐笔交易见各路线子目录。复现：`python scripts/fresh_year_walkforward.py`。']
    (DEST/'REPORT.md').write_text('\n'.join(lines),encoding='utf8')

if __name__=='__main__':run()
