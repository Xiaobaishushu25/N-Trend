"""Research revision 2: completed-day context, causal intraday execution.

The historical tests have been seen. This version is adaptive EXPOSED research.
No original results/code are overwritten. No live orders or online alpha inputs.
"""
import argparse
import collections
import datetime as dt
import itertools
import json
import math
import platform
import shutil
import subprocess
import sys
from pathlib import Path
import numpy as np
import pandas as pd
import simple_wfo as core

ROOT=core.ROOT
PRIOR=ROOT/'artifacts/simple_wfo_20261105'
OUT=ROOT/'artifacts/daily_context_wfo_20261105'
FEATURES=['daily_displacement','daily_location','daily_activity','open_gap']
LOOKBACK=[3,6,12]
HOLD=[12,24,45]
STOP=[.25,.5,1.]
ENTRY=[545,630,810]


def configure():
    # Reuse frozen executor verbatim, only declared parameter grid/I/O changes.
    core.OUT=OUT;core.FEATURES=FEATURES;core.LOOKBACK=LOOKBACK
    core.HOLD=HOLD;core.STOP=STOP;core.ENTRY=ENTRY


def utility(net,neighbor,median_month,drawdown,extra_tick_cost,trades):
    raw=(net+neighbor+6*median_month)/3-.25*drawdown-extra_tick_cost
    return raw*trades/(trades+12.)


def context_frame(raw):
    """Only prior completed days and this known 09:00 open, no current-day extrema."""
    f=raw.copy().reset_index(drop=True)
    daily=[]
    for day,g in f.groupby('day',sort=True):
        first=g.iloc[0]
        full=(g.minute.tolist()==core.SLOTS and g.contract.nunique()==1 and bool(g.valid.all()))
        daily.append(dict(day=day,contract=str(first.contract),day_open=float(first.open) if first.minute==540 else np.nan,
            open=float(first.open) if full else np.nan,high=float(g.high.max()) if full else np.nan,
            low=float(g.low.min()) if full else np.nan,close=float(g.close.iloc[-1]) if full else np.nan,
            volume=float(g.volume.sum()) if full else np.nan,full=full))
    d=pd.DataFrame(daily);d['group']=d.contract.ne(d.contract.shift()).cumsum()
    columns=[f'{feat}_{lb}' for feat,lb in itertools.product(FEATURES,LOOKBACK)]
    for c in columns:d[c]=np.nan
    d['daily_stop_scale']=np.nan
    for _,g in d.groupby('group',sort=False):
        previous=g.close.shift()
        tr=pd.concat([g.high-g.low,(g.high-previous).abs(),(g.low-previous).abs()],axis=1).max(axis=1).where(g.full)
        d.loc[g.index,'daily_stop_scale']=tr.shift().rolling(3,min_periods=3).median()
        for lb in LOOKBACK:
            scale=tr.shift().rolling(lb,min_periods=lb).median().replace(0,np.nan)
            hi=g.high.shift().rolling(lb,min_periods=lb).max();lo=g.low.shift().rolling(lb,min_periods=lb).min()
            d.loc[g.index,f'daily_displacement_{lb}']=(g.close.shift()-g.close.shift(lb+1))/scale
            d.loc[g.index,f'daily_location_{lb}']=(g.close.shift()-lo)/(hi-lo).replace(0,np.nan)
            d.loc[g.index,f'daily_activity_{lb}']=g.volume.shift()/g.volume.shift(2).rolling(lb,min_periods=lb).median().replace(0,np.nan)
            d.loc[g.index,f'open_gap_{lb}']=(g.day_open-g.close.shift())/scale
    mapping=d.set_index('day')
    for c in columns:f[c]=f.day.map(mapping[c])
    f['atr']=f.day.map(mapping.daily_stop_scale)
    return f,d


def prepare():
    OUT.mkdir(parents=True,exist_ok=True);dest=OUT/'prepared';dest.mkdir(exist_ok=False)
    # Check original frozen code and prepared hashes without replaying any strategy.
    for p,h in core.rd(PRIOR/'freeze.json')['hashes'].items():assert core.sha(ROOT/p)==h,p
    old=core.rd(PRIOR/'prepared/manifest.json')
    for p,h in old['prepared_files'].items():assert core.sha(PRIOR/'prepared'/p)==h,p
    for name in ['specs.json','folds.json','coverage.csv','contract_choices.csv']:
        shutil.copy2(PRIOR/'prepared'/name,dest/name)
    for p in sorted((PRIOR/'prepared').glob('*.pkl')):
        f,d=context_frame(pd.read_pickle(p));f.to_pickle(dest/p.name)
        d.to_csv(dest/f'{p.stem}_daily_context.csv',index=False)
        print('daily context prepared',p.stem,flush=True)
    core.js(dest/'manifest.json',dict(database_sha256=old['database_sha256'],prior_prepared_manifest_sha256=core.sha(PRIOR/'prepared/manifest.json'),
        prepared_files={p.name:core.sha(p) for p in sorted(dest.iterdir()) if p.is_file()}))


def protocol():
    return dict(version='daily-context-WFO-2',created_shanghai=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
        exposure='EXPOSED_ADAPTIVE_RESEARCH: follows first WFO feedback; all eight historical test intervals already exposed. Independent prospective sample size zero.',
        reason='expand signal information to multi-day context; retain day-session one-lot executor, account risk and costs. Not a reset or continuation of prior stopped account.',
        prior_candidate_window_evaluations=69120,older_search_count='at least80 templates/256 cells plus R1/R2 and older project, cumulative total unknown',
        folds=core.rd(OUT/'prepared/folds.json'),window='9 calendar month train /3 frozen test /3 step, eight complete windows',
        universe='training valid row count top20 of22, alphabetic tie; UR/V excluded in tied coverage, never by future return',
        feature_grammar=dict(features=FEATURES,lookback_completed_days=LOOKBACK,bands=['bottom_quartile','middle_half','top_quartile'],side=[-1,1],hold_5m_bars=HOLD,stop_previous_day_TR_multiple=STOP,
            templates_per_product=648,entry_minutes=ENTRY,definitions=dict(daily_displacement='(previous close - close lb+1 days ago) / previous lb days median true range',
                daily_location='previous close location within previous lb days high-low',daily_activity='previous day volume / median volume preceding that day over lb days',
                open_gap='known today09:00 open - previous day close, / previous lb days median true range'),
            stop_scale='previous3 completed days median true range, no current-day range',
            history='same concrete contract only; reset on roll; incomplete past day invalidates affected rolling windows; current-day end quality cannot affect current opening signal'),
        validation='first3 calibration, six expanding-past one-month inner validation; final selected thresholds refit using all9 train months only; no shuffled data',
        selection='cash benchmark score0; utility=(net + adjacent-neighbor median net +6*median monthly net)/3 -0.25*trade_drawdown -one-extra-tick-both-sides-cost. Multiply by n/(n+12) to shrink low-count estimates continuously. Rank utility descending, max1 per product, max3 products; select only utility>0 and complete execution paths. No significance tests or trade count cutoffs.',
        utility_notes='weights and smoothing fixed before this batch. Chosen utility is a research judgement informed by prior exposed research, not proven optimal. Month concentration/count/cost sensitivity remain flags.',
        execution='verbatim simple_wfo.py executor: completed previous5m signal then next09:05/10:30/13:30 actual open; hold12/24/45 observed5m bars; planned14:55 flat; fixed tick-rounded stop; delayed fills for locks/missing, no target/ideal limit fills',
        costs='unchanged:20261022 exchange snapshot +1yuan per side broker allowance;2ticks adverse per side. Fixed stress3ticks,2x fees,25% margin. Fees not historical actual statements.',
        account='NEW revision research account starts50000, shared across all8 folds;1 integer lot,planned risk<=600 including3tick stop-exit buffer, totalrisk<=900,margin<=60%equity with20%notional reserve,dayloss900 halt,12%DD permanently stop new entries and next-executable-open close. All state carries across folds; no reset/rearm.',
        margins='20% baseline/25% stress scenario, not actual historical broker-margin certification',
        freeze='all train selections+candidate logs sealed before all8 OOS in one pass; no per-fold OOS stdout or OOS-based changes. No further version in this batch merely to obtain positive returns.',
        limitations='source contract availability may be retrospectively selected; no bid/ask,limit queues or exact intrabar paths; historical margin/fee schedules missing; future gap can exceed600/trade and20%account DD',
        prospective='not started; no live orders. Even positive exposed replay cannot be called independent or ready for deployment.')


def freeze():
    assert not (OUT/'freeze.json').exists()
    test=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p','test_daily_context_wfo.py','-v'],capture_output=True,text=True)
    (OUT/'execution_tests.txt').write_text(test.stdout+test.stderr,encoding='utf8')
    assert test.returncode==0,'pre-freeze tests failed'
    core.js(OUT/'protocol.json',protocol())
    paths=[Path(__file__),ROOT/'scripts/simple_wfo.py',ROOT/'tests/test_daily_context_wfo.py',OUT/'protocol.json',OUT/'prepared/manifest.json',ROOT/'scripts/exchange_fees_20261022.json',ROOT/'crates/n-core/src/precision.rs']
    snap=OUT/'source_snapshot';snap.mkdir()
    for p in paths:shutil.copy2(p,snap/p.name)
    core.js(OUT/'freeze.json',dict(hashes={str(p.relative_to(ROOT)):core.sha(p) for p in paths},database_sha256=core.rd(OUT/'prepared/manifest.json')['database_sha256'],
        python=platform.python_version(),pandas=pd.__version__,numpy=np.__version__,new_candidate_results_seen_before_freeze=0,independent_unseen=False))
    print('Revision2 frozen before new candidate outcomes. Historical exposure retained.',flush=True)


def train():
    core.check();dest=OUT/'training';dest.mkdir(exist_ok=False)
    specs=core.rd(OUT/'prepared/specs.json');folds=core.rd(OUT/'prepared/folds.json')
    allframes={p.stem:pd.read_pickle(p) for p in (OUT/'prepared').glob('*.pkl')}
    candidates=list(itertools.product(FEATURES,LOOKBACK,range(3),[-1,1],HOLD,STOP));assert len(candidates)==648
    total=0;finalfits=0
    for fold in folds:
        fd=dest/f"fold_{fold['fold']:02d}";fd.mkdir()
        frames={p:f.loc[(f.start_ns>=core.ns(fold['train']))&(f.start_ns<core.ns(fold['test']))].reset_index(drop=True) for p,f in allframes.items()}
        coverage=sorted([(p,int(f.valid.sum())) for p,f in frames.items()],key=lambda x:(-x[1],x[0]));universe=[p for p,n in coverage[:20] if n>0]
        core.js(fd/'universe.json',dict(ranking=coverage,selected=universe))
        months=pd.period_range(fold['train'],periods=9,freq='M').astype(str).tolist()[3:];records=[]
        for p in universe:
            f=frames[p];paths={(d,h,k):core.trade_paths(f,d,h,k,specs[p]) for d,h,k in itertools.product([-1,1],HOLD,STOP)}
            cuts={(feat,lb,m):core.thresholds(f.loc[f.month<m],feat,lb) for feat,lb,m in itertools.product(FEATURES,LOOKBACK,months)}
            pd.DataFrame([dict(feature=feat,lookback_days=lb,validation_month=m,q25=q[0],q75=q[1]) for (feat,lb,m),q in cuts.items()]).to_csv(fd/f'{p}_monthly_thresholds.csv',index=False)
            signal_cache={}
            for feat,lb,band in itertools.product(FEATURES,LOOKBACK,range(3)):
                indices=[]
                for m in months:
                    inds=core.signals(f,[feat,lb,band,1,HOLD[0],STOP[0]],cuts[feat,lb,m]);indices.extend(inds[f.month.iloc[inds].to_numpy()==m])
                signal_cache[feat,lb,band]=sorted(indices)
            local=[]
            for ci,c in enumerate(candidates):
                feat,lb,band,d,h,k=c;tr,unknown=core.isolated(signal_cache[feat,lb,band],paths[d,h,k]);m=core.metrics(tr,months)
                local.append(dict(fold=fold['fold'],product=p,candidate_id=f'{p}-D{ci:03d}',candidate=list(c),feature=feat,lookback_days=lb,band=band,side=d,hold=h,stop_atr=k,
                    **{a:b for a,b in m.items() if a!='monthly'},monthly_json=json.dumps(m['monthly']),unknown=unknown))
            lookup={tuple(r['candidate']):r for r in local}
            for r in local:
                c=r['candidate'];neighbors=[]
                for axis,grid in [(1,LOOKBACK),(4,HOLD),(5,STOP)]:
                    ix=grid.index(c[axis])
                    for j in [ix-1,ix+1]:
                        if 0<=j<len(grid):
                            other=c.copy();other[axis]=grid[j];q=lookup[tuple(other)]
                            # Unknown neighbor contributes its partial result conservatively capped at zero.
                            neighbors.append(min(q['net'],0.) if q['unknown'] else q['net'])
                r['neighbor_median_net']=float(np.median(neighbors));r['neighbor_positive_fraction']=float(np.mean(np.array(neighbors)>0))
                extra_cost=r['net']-r['extra_tick_net']
                r['utility']=utility(r['net'],r['neighbor_median_net'],r['median_month'],r['drawdown_yuan'],extra_cost,r['trades'])
                r['score']=r['utility'];monthly=list(json.loads(r['monthly_json']).values());positive=sum(max(v,0.) for v in monthly)
                r['flags']=';'.join(flag for flag,yes in [('few_trades',r['trades']<30),('month_concentration',positive>0 and max(monthly)/positive>.5),
                    ('parameter_peak',r['net']>0 and r['neighbor_median_net']<=0),('extra_tick_sensitive',r['net']>0 and r['extra_tick_net']<=0),
                    ('unknown_path',r['unknown']>0),('nonpositive_median_month',r['median_month']<=0)] if yes)
            records.extend(local);print('train daily',fold['fold'],p,len(local),flush=True)
        df=pd.DataFrame(records);eligible=df.loc[(df.utility>0)&(df.unknown==0)].sort_values(['utility','candidate_id'],ascending=[False,True])
        selected=[]
        for _,r in eligible.drop_duplicates('product').head(3).iterrows():
            c=r['candidate'];selected.append(dict(product=r['product'],candidate_id=r['candidate_id'],candidate=c,cuts=core.thresholds(frames[r['product']],c[0],c[1]),
                score=float(r['utility']),validation_net=float(r['net']),flags=r['flags']))
        df.drop(columns='candidate').to_csv(fd/'all_candidates.csv',index=False)
        core.js(fd/'selection.json',dict(**fold,selected=selected,empty_reason='no_positive_utility_vs_cash' if not selected else None,search_count=len(df),threshold_fits=len(universe)*12*6,final_threshold_refits=len(selected)))
        total+=len(df);finalfits+=len(selected)
    core.js(dest/'manifest.json',dict(candidate_window_evaluations=total,unique_rule_templates=648,threshold_fits=8*20*12*6,final_threshold_refits=finalfits,
        files={str(p.relative_to(dest)):core.sha(p) for p in sorted(dest.rglob('*')) if p.is_file()}))
    print('All revision2 selections sealed; no OOS metrics released.',flush=True)


def evaluate():
    # No edits of original executor. Uses new frozen parameters/selections only.
    core.evaluate()


def report():
    core.verify()
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    sums={};stats={};fold_rows=[];selected=[];search=[]
    for fd in sorted((OUT/'training').glob('fold_*')):
        fold=core.rd(fd/'selection.json');df=pd.read_csv(fd/'all_candidates.csv')
        search.append(dict(fold=fold['fold'],candidates=len(df),positive_net=int((df.net>0).sum()),positive_utility=int(((df.utility>0)&(df.unknown==0)).sum()),unknown=int((df.unknown>0).sum()),selected=len(fold['selected'])))
        for r in fold['selected']:
            feat,lb,band,d,h,k=r['candidate'];selected.append(dict(fold=fold['fold'],product=r['product'],candidate_id=r['candidate_id'],feature=feat,lookback_days=lb,band=band,side=d,
                hold_5m_bars=h,stop_scale_multiple=k,q25=r['cuts'][0],q75=r['cuts'][1],utility=r['score'],validation_net=r['validation_net'],flags=r['flags']))
    pd.DataFrame(selected).to_csv(OUT/'selected_parameters.csv',index=False)
    pd.DataFrame(search).to_csv(OUT/'search_counts.csv',index=False)
    fig,ax=plt.subplots(figsize=(12,4.5))
    for label,factor in [('base',1.),('stress',2.)]:
        summ=core.rd(OUT/f'evaluation/{label}_summary.json');sums[label]=summ
        eq=pd.read_csv(OUT/f'evaluation/{label}_equity.csv');p=OUT/f'evaluation/{label}_trades.csv';tr=pd.read_csv(p) if p.stat().st_size>3 else pd.DataFrame()
        if len(tr):
            assert (tr.lots==1).all() and (tr.planned_risk<=600+1e-8).all()
            np.testing.assert_allclose(tr.gross,(tr.exit-tr.entry)*tr.side*tr.multiplier,atol=1e-7)
            np.testing.assert_allclose(tr.net,tr.gross-tr.fees,atol=1e-7)
            specs=core.rd(OUT/'prepared/specs.json');fees=[]
            for t in tr.itertuples():
                fees.append(core.fee(specs[t.product],'open',t.entry,factor)+core.fee(specs[t.product],'close_today' if t.entry_time[:10]==t.exit_time[:10] else 'close_yesterday',t.exit,factor))
            np.testing.assert_allclose(tr.fees,fees,atol=1e-7)
            for _,g in tr.groupby('product'):
                g=g.sort_values('entry_ns');assert (g.entry_ns.iloc[1:].to_numpy()>=g.exit_ns.iloc[:-1].to_numpy()).all()
            assert abs(50000+tr.net.sum()-eq.balance.iloc[-1])<1e-7
            win=tr.loc[tr.net>0].net;loss=tr.loc[tr.net<0].net
            winrate=float((tr.net>0).mean());payoff=float(win.mean()/-loss.mean()) if len(win) and len(loss) else None
            monthly=tr.groupby(tr.exit_time.str[:7]).net.sum().reindex(pd.period_range('2024-08','2026-07',freq='M').astype(str),fill_value=0.)
            monthly.rename('net_yuan').to_csv(OUT/f'{label}_oos_monthly.csv',index_label='month')
        else:
            winrate=None;payoff=None
            pd.DataFrame({'month':pd.period_range('2024-08','2026-07',freq='M').astype(str),'net_yuan':0.}).to_csv(OUT/f'{label}_oos_monthly.csv',index=False)
        assert eq.time_ns.is_monotonic_increasing and eq.time_ns.is_unique
        assert (eq.planned_risk<=900+1e-7).all()
        if summ['final_equity'] is not None:assert abs(summ['final_equity']-eq.equity.iloc[-1])<1e-7
        firsthalt=eq.loc[eq.halted]
        stats[label]=dict(final_equity=summ['final_equity'],net=summ['final_equity']-50000 if summ['final_equity'] is not None else None,max_drawdown=float(eq.drawdown.max()),
            trades=len(tr),win_rate=winrate,payoff=payoff,worst_trade=float(tr.net.min()) if len(tr) else None,losses_beyond600=int((tr.net< -600).sum()) if len(tr) else 0,
            first_halt=firsthalt.iloc[0].time if len(firsthalt) else None,unknown=summ['unknown'],empty_selection_folds=sum(not f['selected_products'] for f in summ['folds']))
        for f in summ['folds']:
            if len(tr):assert abs(tr.loc[tr.fold==f['fold']].net.sum()-f['net'])<1e-7
            fold_rows.append(dict(scenario=label,**{k:f[k] for k in ['fold','train_start','test_start','test_end','start_equity','end_equity','net','max_drawdown','win_rate','payoff','trades']},
                selected_products=','.join(f['selected_products']),halted=f['halted']))
        ax.plot(pd.to_datetime(eq.time,utc=True),eq.equity,label=label)
    ax.axhline(50000,color='gray',lw=.7);ax.set_ylabel('Shared account equity (CNY)');ax.legend();ax.grid(alpha=.2);fig.tight_layout();fig.savefig(OUT/'oos_equity.png',dpi=160);plt.close(fig)
    pd.DataFrame(fold_rows).to_csv(OUT/'fold_summary.csv',index=False)
    core.js(OUT/'reconciliation.json',dict(scenarios=stats,ledger_fees_and_frozen_hashes_verified=True,independent_unseen=False))
    lines=['# 跨日信息、日内执行：第二版冻结滚动研究','',
        '**这是承接第一版反馈的研究暴露数据再探索，不是独立未见样本。没有覆盖或修改第一版结果。**','',
        '## 结果','', '|情景|期末权益|净额|5分钟盯市最大回撤|交易数|胜率|平均盈利/平均亏损|','|---|---:|---:|---:|---:|---:|---:|']
    for label,s in stats.items():
        fmt=lambda v: '未知' if v is None else f'{v:,.2f}'
        lines.append(f"|{label}|{fmt(s['final_equity'])}|{fmt(s['net'])}|{s['max_drawdown']:.2%}|{s['trades']}|{'—' if s['win_rate'] is None else format(s['win_rate'],'.2%')}|{fmt(s['payoff'])}|")
    lines+=['','|轮次|测试区间（右端不含）|训练选择|净额|局部回撤|胜率|盈亏比|交易数|停机|','|---|---|---|---:|---:|---:|---:|---:|---|']
    for f in sums['base']['folds']:
        lines.append(f"|{f['fold']}|{f['test_start']} → {f['test_end']}|{','.join(f['selected_products']) or '空选'}|{f['net']:.2f}|{f['max_drawdown']:.2%}|{'—' if f['win_rate'] is None else format(f['win_rate'],'.1%')}|{'—' if f['payoff'] is None else format(f['payoff'],'.2f')}|{f['trades']}|{f['halted']}|")
    b=stats['base'];s=stats['stress']
    if b['unknown'] or s['unknown']:conclusion='存在未知成交/未完成仓位，完整账户收益不能确认；CSV中的已知部分不能冒充完整收益。'
    elif b['trades']==0:conclusion='全部轮次选择现金或未形成可准入交易，没有找到可执行的正效用候选；零收益不构成盈利证据。'
    elif b['net']>0 and s['net']>0:conclusion='两个固定成本场景均为正，但只是已暴露历史中的候选，不足以确认稳定泛化，仍需新行情前向模拟。'
    else:conclusion='本版没有得到可确认的稳定正收益流程。没有为得到盈利结果继续改动测试参数。'
    lines+=['',conclusion,'',f"基准空选{b['empty_selection_folds']}轮；首次停机：{b['first_halt'] or '无'}。最大单笔亏损：{b['worst_trade']}。超过600元亏损笔数：{b['losses_beyond600']}。",
        '如停机发生，后续零交易属于同一账户停机持币；如空选，则属于训练期与现金比较后的选择，两者不可混淆。局部回撤为轮内高水位计算；连续账户回撤见上表及资金曲线。','',
        '## 冻结研究方法','',
        '前一版之外，本次把信息窗口扩展至3/6/12个已完成交易日；特征为跨日位移、过去区间位置、相对日成交量、当日已知开盘缺口。方向和高/中/低分位区域由训练学习。每天09:05、10:30、13:30可产生交易，持有12/24/45根5分钟bar，计划14:55开盘退出。每手止损距离为此前3天中位日真实波幅的0.25/0.5/1倍。没有搜索命名策略库或复杂组合条件。','',
        '同一真实合约内计算跨日历史，换月重置。当前日全天高低收及成交量不用于当前信号；只有09:00已观察到开盘可用于缺口。当天后续缺bar或全天完整性不能提前否决当天信号。冻结前通过真实数据前缀重算及合成测试。','',
        '训练9个月，最初3个月校准，后6个月按月扩展向前验证；选定后用训练9个月重估阈值，冻结后测3个月，前移3个月，仅8个完整窗口。每轮训练按数据覆盖度选20个品种，并列按字母；UR/V未纳入本次搜索，未按未来收益排除。','',
        '效用=(六个月净额+相邻参数净额中位数+6×月净额中位数)/3−0.25×已平仓回撤−每边额外1跳的金额；再乘n/(n+12)连续缩减低交易数估计。现金效用0。只比较完整可定价路径且效用为正的候选，每品种最多1条、组合最多3个品种；允许空选。没有交易数量或显著性硬门槛。月份集中、参数尖峰、费用敏感、交易少分别标记。','',
        '该效用是承认前版研究反馈后确定的研究判断，不是证明最优的目标函数。它在本次新候选结果产生前冻结，不因本次样本外收益改变。','',
        '## 搜索账本','',
        f"本次648规则模板×20品种×8轮，共{8*20*648:,}次候选窗口评估，6个月度评分单元/次，共{8*20*648*6:,}个候选月度单元；11,520次扩展分位数拟合。完整保留败例及未知成交候选。与前版合计{69120+8*20*648:,}次新系列候选窗口评估；更早历史累计试验数未知，不能作独立试验相加。",'',
        '每轮training/fold_XX/all_candidates.csv含全部参数、净额、回撤、月分布、笔数、邻域、费用敏感性、效用及标记；selection.json保存冻结选择及阈值；selected_parameters.csv便于逐轮查阅。','',
        '## 共享账户和执行限制','',
        '这是新研究版本的5万元起始共享账户，跨轮不重置，不是向已停止的第一版账户追加资金。风控与前版相同：固定1手，计划一手风险≤600元，已含开仓2跳、止损退出至少3跳及费用；总计划风险≤900元；保证金最多占权益60%；日亏900停止；高水位回撤12%永久停开并请求下一可成交开盘退出。','',
        '真实具体合约、乘数和tick沿用核查表：JD=10元/报价点，BU tick=1，SH tick=1；前一观察日成交量决定当前合约，不读未来主力映射。原始供应商合约覆盖可能含回溯选择偏差，不能证明全市场合约当时均可得。','',
        '费用沿用2026-10-22快照、另每边1元佣金；基准每边2跳。压力情景预先固定为每边3跳、双倍费用和25%保证金占用。基准20%、压力25%是名义价值保证金场景，并非三年逐日真实经纪商保证金；历史费用也未逐合约核验。未满足真实历史执行认证，不得冒充已可实盘策略。','',
        '当前入场bar一字价/零量/无效会标记未知成交，不填零收益；已持仓无法退出则延迟到下一可成交实际开盘，可能被动隔夜。同bar止损按不利滑点处理，不承诺能在封板价成交。缺少bid/ask、盘口队列和tick路径，5分钟盯市回撤不等于精确逐笔最大回撤。跳空、锁板或停牌仍可使单笔亏损超过600元或账户回撤超过20%。','',
        '## 复现与文件','',
        '运行python scripts/daily_context_wfo.py prepare → freeze → train → evaluate → report；每个阶段输出目录拒绝覆盖。verify验证冻结输入、数据及全部训练/测试结果哈希。新复现须在新目录执行并保留为独立运行。依赖为本地已有numpy、pandas、matplotlib及前版不可变准备数据。','',
        '- fold_summary.csv：两情景全部轮次账户指标。\n- evaluation/base_trades.csv、stress_trades.csv：真实准入交易明细。\n- evaluation/base_equity.csv、stress_equity.csv：连续共享账户曲线，包含现金月份。\n- selected_parameters.csv、search_counts.csv：选择参数与所有搜索计数。\n- prepared/*_daily_context.csv：用于构造因果信号的逐日日级数据。\n- protocol.json、freeze.json、source_snapshot：正式评估前冻结的代码与规则。\n- reconciliation.json、execution_tests.txt：账本和测试核验。','',
        '新行情前向模拟尚未启动。paper_forward.json仅冻结流程引用；任何正收益历史候选都必须在冻结后新行情中模拟，记录实际合约费率、保证金和盘口成交。不能从本次已暴露测试继续挑盈利版本并宣称独立验证。']
    (OUT/'REPORT.md').write_text('\n'.join(lines),encoding='utf8')
    core.js(OUT/'paper_forward.json',dict(status='NOT_STARTED',version='daily-context-WFO-2',freeze_sha256=core.sha(OUT/'freeze.json'),independent_prospective_sample=0,live_orders=False,
        requirement='newly occurring data after freeze, concrete broker metadata and quotes validated; same fixed workflow; no backfill counted as prospective'))
    core.js(OUT/'delivery_manifest.json',dict(files={str(p.relative_to(OUT)):core.sha(p) for p in sorted(OUT.rglob('*')) if p.is_file() and p.name!='delivery_manifest.json'}))
    print(json.dumps(stats,ensure_ascii=False,indent=2))


def verify():core.verify()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('stage',choices=['prepare','freeze','train','evaluate','report','verify']);args=parser.parse_args();configure();globals()[args.stage]()
