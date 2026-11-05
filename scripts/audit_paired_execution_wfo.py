"""Fourth-batch audit. Admission arithmetic only, no new payoff replay/search."""
import collections
from pathlib import Path
import numpy as np
import pandas as pd
import paired_execution_wfo as w


def admission(f,r,specs,raw,end):
    lb,z,orientation,h,k=r['param'];i,d=w.indices(f,lb,z,orientation);q=r['choice'];g=f.iloc[i]
    n=len(g);remaining=np.ones(n,bool);counts=dict(signals=n)
    def remove(name,bad):
        nonlocal remaining
        cut=remaining&bad;counts[name]=int(cut.sum());remaining&=~bad
    remove('last7day_no_open',g.entry_ns.to_numpy()>=end-7*86400*10**9)
    risks=[];margins=[];fees=[];badstop=np.zeros(n,bool);badlot=np.zeros(n,bool);uncertain=np.zeros(n,bool)
    for suffix,p,qty,side in [('a',q['a'],q['qty_a'],d),('b',q['b'],q['qty_b'],-d)]:
        s=specs[p];entry_open=np.array([raw[code].open.iloc[int(j)] for code,j in zip(g[f'contract_{suffix}'],g[f'entry_i_{suffix}'])]);entry=entry_open+side*2*s['tick']
        ref=g[f'close_{suffix}'].to_numpy()-side*k*g[f'atr_{suffix}'].to_numpy()
        stop=np.where(side==1,np.floor(ref/s['tick']),np.ceil(ref/s['tick']))*s['tick'];out=stop-side*3*s['tick']
        fo=w.b.fee(s,entry,1.,True);risk=(entry-out)*side*s['multiplier']+fo+w.b.fee(s,out,1.)
        badstop|=(entry-stop)*side<=0;badlot|=(risk<=0)|(risk>600)|~np.isfinite(risk)
        risks.append(risk*qty);margins.append(entry*s['multiplier']*qty*.2);fees.append(fo*qty)
        for j,(code,ix) in enumerate(zip(g[f'contract_{suffix}'],g[f'entry_i_{suffix}'])):
            row=raw[code].iloc[int(ix)];uncertain[j]|=not(row.valid and row.volume>0 and row.high>row.low)
    remove('invalid_stop_at_open',badstop);remove('perlot_risk_over600_or_invalid',badlot);remove('combined_risk_over900',sum(risks)>900)
    remove('single_pair_margin_over60pct',sum(margins)>(50000-sum(fees))*.6)
    counts['known_budget_feasible']=int(remaining.sum());counts['entry_bar_fill_uncertain_among_feasible']=int((remaining&uncertain).sum())
    return counts


def run():
    w.check();w.b.check();out=w.OUT
    for folder,name in [('training','manifest.json'),('evaluation','complete.json')]:
        for p,h in w.c.rd(out/folder/name)['files'].items():assert w.c.sha(out/folder/p)==h,p
    frames,specs=w.load();admissions=[];records=[];ratios=0;candidates=0
    for fold in w.c.rd(w.PRIOR/'prepared/folds.json'):
        fd=out/f"training/fold_{fold['fold']:02d}";a=pd.read_csv(fd/'all_integer_hedges.csv');assert a.groupby('pair').size().eq(9).all();ratios+=len(a)
        cand=pd.read_csv(fd/'all_candidates.csv');candidates+=len(cand);sel=w.c.rd(fd/'selection.json')
        for r in sel['selected']:
            row=cand.loc[cand.candidate_id==r['candidate_id']].iloc[0];assert row.score>0 and row.unknown==0
            q=r['choice'];assert 1<=q['qty_a']<=3 and 1<=q['qty_b']<=3
            full=w.pair_frame(frames[q['a']],frames[q['b']],q['qty_a'],q['qty_b'],specs,q['a'],q['b'])
            f=full.loc[(full.entry_ns>=w.b.ns(fold['test']))&(full.entry_ns<w.b.ns(fold['end']))].reset_index(drop=True)
            raw=w.b.load_raw(set(f.contract_a)|set(f.contract_b));admissions.append(dict(fold=fold['fold'],pair=q['pair'],**admission(f,r,specs,raw,w.b.ns(fold['end']))))
            records.append(dict(fold=fold['fold'],pair=q['pair'],training_trades=int(row.trades),training_monthly=row.monthly_json,flags=r['flags']))
    assert candidates==w.c.rd(out/'training/manifest.json')['candidate_windows'];assert ratios==w.c.rd(out/'training/manifest.json')['integer_hedge_trials']
    pd.DataFrame(admissions).to_csv(out/'signal_admission_audit.csv',index=False);pd.DataFrame(records).to_csv(out/'selected_training_evidence.csv',index=False)
    stats={}
    for label in ['base','stress']:
        legs=pd.read_csv(out/f'evaluation/{label}_legs.csv');pair=pd.read_csv(out/f'evaluation/{label}_pairs.csv');u=pd.read_csv(out/f'evaluation/{label}_unit_diagnostic.csv')
        assert abs(pair.net.sum()-legs.net.sum())<1e-6
        for _,g in legs.groupby(['entry_fold','pair','entry_ns']):assert len(g)==2 and g.side.sum()==0
        eq=pd.read_csv(out/f'evaluation/{label}_equity.csv');assert abs(50000+legs.net.sum()-eq.balance.iloc[-1])<1e-6
        stats[label]=dict(raw_direction_gross=float(legs.gross.sum()+legs.slippage_yuan.sum()),slippage=float(legs.slippage_yuan.sum()),fees=float(legs.fees.sum()),net=float(legs.net.sum()),
            unit_diagnostic_trades=int(u.net.notna().sum()),unit_diagnostic_net=float(u.net.sum()) if u.net.notna().all() else None,
            max_observed_margin=float(eq.margin.max()),max_observed_planned_risk=float(eq.planned_risk.max()),max_pair_planned_risk=float(pair.planned_risk.max()),worst_pair_net=float(pair.net.min()),
            worst_per_lot_net=float((legs.net/legs.lots).min()),permanent_halt=bool(eq.halted.any()))
    flags=collections.Counter(f for r in records for f in r['flags'].split(';') if f)
    w.c.js(out/'post_batch_audit.json',dict(candidate_windows=candidates,integer_hedge_trials=ratios,selected_rules=len(records),flags=dict(flags),scenarios=stats,
        no_new_payoff_replay=True,admission_audit_uses_known_entry_prices_and_signal_ATR_only=True))
    marker='## 本轮结果回答了什么';text=(out/'REPORT.md').read_text(encoding='utf8').replace('股指/商品共同风险','商品共同风险')
    if marker in text:text=text.split(marker)[0].rstrip()
    lines=['',marker,'',
        '这次不是再靠12%停机把后面行情遮住：基准没有触发永久停机，22笔完整双腿交易分布在前六个窗口；只有第4窗+49元，且仅1笔，不构成稳定盈利。'
        '完整双腿成交44条单腿记录，均已重算真实乘数、整数手数、逐腿费用和资金账本。','',
        '基准未计滑点方向毛额−1150元，滑点1860元，费用372.97元，最终−3382.97元。虽然本次观察到的最大回撤7.98%低于前三版，'
        '样本和成交集合不同，不能把它解释成已统计确认的风险改进；更不能把较低回撤等同于有正期望。','',
        '固定单位诊断账本24笔、净额合计−3525.97元，同样没有展示被账户停机隐藏的稳定盈利。这不是5万元账户收益，不能用它替代正式结果。'
        '本轮第7/8窗正式与诊断均没有可准入成交，原因见signal_admission_audit.csv：汇总逐笔信号触发、单手600、总风险900和保证金的已知开盘准入计算。'
        '缺少可执行机会不是对这些季度所有策略的否定。','',
        '压力情景只发生1笔组合成交，净亏1004元；不能以其2.27%最大回撤声称压力稳健。2025-04-09 EB/PP组合计划总风险724元，'
        'EB夜盘跳空退出后PP按自身保护止损退出，组合总损失1004元。每手均没有亏损超过600元，但总计划风险900不是总损失硬上限。'
        '基准同一组合计划697元、净亏962元。没有删除该交易或在观察后加入事件过滤。','',
        f"本次{len(records)}个轮次入选规则中，{flags.get('few_trades',0)}个训练交易不足30笔，{flags.get('parameter_peak',0)}个有参数尖峰标记。"
        '将统计门槛堆高当然可以把它们全部筛空，但不会创造优势。本轮选择保留风险标记、如实评估，不把薄弱训练正收益叫作赢家。','',
        '因此，更准确的结论是：前三版确有方向集中、学习目标错配和停机证据不足等方法问题；改为两腿执行并获取更多季度证据后，'
        '本次候选仍缺乏能覆盖成本的正毛优势，交易规模也偏小。目前没有支持“可实际交易且具有较好泛化”的证据，但也远未证明这批数据里不存在其他机会。','',
        '执行风险参照：[证监会深圳监管局关于市价与止损成交价风险的说明](https://www.csrc.gov.cn/shenzhen/c105614/c7602078/content.shtml)。'
        '本文并未用这份说明替代逐日历史费率、保证金或盘口核验。新行情前向模拟尚未启动。','',
        '补充核验：python scripts/audit_paired_execution_wfo.py只复核现有结果和已知开盘准入算术，不增加策略或收益回放。']
    lines+=['','|测试轮次|满足信号及此前成交量的触发|窗口末停止新单|单手风险不符|总风险超过900|可通过已知预算|',
        '|---|---:|---:|---:|---:|---:|']
    admission_table=pd.DataFrame(admissions)
    for f in [7,8]:
        g=admission_table.loc[admission_table.fold==f]
        lines.append(f"|{f}|{g.signals.sum()}|{g.last7day_no_open.sum()}|{g.perlot_risk_over600_or_invalid.sum()}|{g.combined_risk_over900.sum()}|{g.known_budget_feasible.sum()}|")
    lines+=['','这显示5万元、整数对冲比率和600/900元预算确实限制了策略表达。不能为了让行情可交易，事后把止损距离硬缩小；'
        '也不能把本轮无可准入成交解释为市场没有任何机会。配对预筛只覆盖最初校准期风险降低较强的12对，没有穷尽所有双腿策略。']
    (out/'REPORT.md').write_text(text+'\n'+'\n'.join(lines)+'\n',encoding='utf8')
    w.c.js(out/'delivery_manifest.json',dict(files={str(p.relative_to(out)):w.c.sha(p) for p in sorted(out.rglob('*')) if p.is_file() and p.name!='delivery_manifest.json'},
        auditor_sha256=w.c.sha(Path(__file__)),no_new_payoff_replay=True))
    print('Fourth batch audit passed:',candidates,'candidate windows;',ratios,'integer hedges; admissions checked without payoff replay.')


if __name__=='__main__':run()
