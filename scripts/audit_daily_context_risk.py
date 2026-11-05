"""Post-batch read-only risk diagnosis; no new parameters, selections or replay."""
import collections
import sqlite3
from pathlib import Path
import pandas as pd
import daily_context_wfo as w


def run():
    w.configure();w.core.verify();out=w.OUT
    s=pd.read_csv(out/'selected_parameters.csv').fillna('')
    flags=collections.Counter(f for cell in s['flags'] for f in str(cell).split(';') if f)
    counts=pd.read_csv(out/'search_counts.csv');assert counts.candidates.sum()==103680
    scenarios={};overnight=[]
    db=sqlite3.connect(w.core.DB.resolve().as_uri()+'?mode=ro',uri=True);db.execute('PRAGMA query_only=ON')
    for label in ['base','stress']:
        t=pd.read_csv(out/f'evaluation/{label}_trades.csv');e=pd.read_csv(out/f'evaluation/{label}_equity.csv')
        for r in t.loc[t.entry_time.str[:10]!=t.exit_time.str[:10]].to_dict('records'):
            f=pd.read_pickle(out/f"prepared/{r['product']}.pkl");g=f.loc[f.day==r['entry_time'][:10]]
            last=g.iloc[-1]
            # Check raw source for night quotes omitted by the day-session executor.
            lower=w.core.ns(r['entry_time'][:10]+' 15:00')
            n=db.execute('select count(*) from bars where contract=? and period_seconds=300 and start_ns>=? and start_ns<?',
                (r['contract'],lower,int(r['exit_ns']))).fetchone()[0]
            q=dict(scenario=label,product=r['product'],contract=r['contract'],entry_time=r['entry_time'],exit_time=r['exit_time'],entry=r['entry'],exit=r['exit'],stop=r['stop'],
                planned_risk=r['planned_risk'],net=r['net'],reason=r['reason'],last_day_open=float(last.open),last_day_close=float(last.close),
                last_day_bar_valid=bool(last.valid),last_day_bar_one_price=bool(last.high==last.low),
                raw_intervening5m_quotes=n,diagnosis='invalid14:55bar_delayed' if not last.valid else 'one_price14:55bar_delayed' if last.high==last.low else 'other')
            overnight.append(q)
            if label=='base':
                g.to_csv(out/f"trace_{r['product']}_{r['entry_time'][:10]}.csv",index=False)
        first=e.loc[e.halted].iloc[0] if e.halted.any() else None
        scenarios[label]=dict(raw_direction_gross=float(t.gross.sum()+t.slippage_yuan.sum()),slippage_cost=float(t.slippage_yuan.sum()),fees=float(t.fees.sum()),
            net=float(t.net.sum()),max_observed_close_margin=float(e.margin.max()),max_observed_close_planned_risk=float(e.planned_risk.max()),
            first_halt_time=str(first.time) if first is not None else None,first_halt_peak=float(first.peak) if first is not None else None,
            overnight_count=int((t.entry_time.str[:10]!=t.exit_time.str[:10]).sum()))
    db.close();pd.DataFrame(overnight).to_csv(out/'overnight_risk_audit.csv',index=False)
    w.core.js(out/'risk_audit.json',dict(post_evaluation_diagnostic_only=True,no_additional_replay=True,selected_rules=len(s),flags=dict(flags),scenarios=scenarios,overnight=overnight,
        interpretation='one-price or incomplete5m bar does not prove no real fill; the frozen conservative model defers it. Stop risk arithmetic is not a bound on actual gap loss.'))
    exposures=[]
    for f in w.core.rd(out/'prepared/folds.json'):
        exposures.append(dict(fold=f['fold'],train=f['train'],test=f['test'],end=f['end'],classification='EXPOSED_ADAPTIVE_RESEARCH',independent_unseen=False,
            protocol_version='daily-context-WFO-2',earlier_family_candidate_windows=69120,new_candidate_windows=12960,feedback_round_in_this_family=2))
    pd.DataFrame(exposures).to_csv(out/'evaluation_exposures.csv',index=False)
    marker='## 超预算亏损及成交模型诊断'
    text=(out/'REPORT.md').read_text(encoding='utf8')
    if marker in text:text=text.split(marker)[0].rstrip()
    lines=['',marker,'',
        '**这部分只检查已冻结结果，没有增加回测、调参或排除亏损交易。**','',
        '基准最差交易为RB2501空单：2024-09-30 09:05以3543入场，止损3581，含成本及滑点缓冲的计划风险419.12元。'
        '当天午后源数据连续一字价3551，冻结成交模型不假设能买回；14:55未平，仓位延至2024-10-08。'
        '节后09:00观察到开盘3680，按买回再加2跳以3682退出，净亏1399.23元。压力情景净亏1428.45元。'
        '这证明“一手计划风险≤600元”不等于“单笔真实损失≤600元”，不能靠止损价格或12%停机线保证账户永不超过20%回撤。','',
        '一字价且有成交量的5分钟bar不能证明真实订单一定无法成交，缺少盘口与排队数据；上述是冻结的保守延迟退出场景，不是已核验的真实锁板订单记录。'
        '本次没有在观察到这笔亏损后新增节假日前过滤或删除RB。','',
        '另一笔被动跨日为SI2411，2024-09-13持有至09-18；原因是14:55bar只有4个分钟构成，冻结模型将它视为无效并延迟退出，'
        '不能把这种数据完整性处理描述成真实市场必然无法平仓。两种情景各有2笔被动跨日；明细见overnight_risk_audit.csv。'
        '原始库中这些跨日间隔的可用夜盘bar数量也已检查并保存；缺少盘口及逐笔行情仍使执行证据有限。','',
        f"本次24个轮次选择中，{flags.get('few_trades',0)}个训练交易少于30笔、{flags.get('month_concentration',0)}个正收益月份集中；"
        f"参数尖峰标记{flags.get('parameter_peak',0)}个、再加一跳转负标记{flags.get('extra_tick_sensitive',0)}个。"
        '训练邻域与费用稳定性改善，并未自动转化为样本外盈利。后六轮基准0收益源于12月2日触发的永久停机，不能声称后六轮策略经过实际成交验证。','',
        '基准已准入交易未滑点方向毛额+1775.00元，扣除滑点5640.00元及手续费/佣金570.12元后，净额−4435.12元。'
        '这是同一批既有成交的账面拆分，不是零费用策略的新回测；不能把毛额为正称作可交易优势。','',
        '补充核验命令：python scripts/audit_daily_context_risk.py。该脚本仅做已完成结果的对账、风险说明与报告补充。']
    (out/'REPORT.md').write_text(text+'\n'+'\n'.join(lines)+'\n',encoding='utf8')
    w.core.js(out/'delivery_manifest.json',dict(files={str(p.relative_to(out)):w.core.sha(p) for p in sorted(out.rglob('*')) if p.is_file() and p.name!='delivery_manifest.json'},
        post_batch_auditor_sha256=w.core.sha(Path(__file__)),no_new_backtest=True))
    print('Risk audit complete. No new replay. Overnight source bar counts:',[q['raw_intervening5m_quotes'] for q in overnight])


if __name__=='__main__':run()
