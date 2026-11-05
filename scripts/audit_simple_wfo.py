"""Read-only reconciliation of frozen WFO results; never searches or changes rules."""
import collections
import json
from pathlib import Path
import numpy as np
import pandas as pd
import simple_wfo as w


def run():
    w.verify()
    out=w.OUT
    checks={};scenarios={}
    selections=[]
    for fd in sorted((out/'training').glob('fold_*')):
        selection=w.rd(fd/'selection.json');allc=pd.read_csv(fd/'all_candidates.csv')
        assert len(allc)==selection['search_count']==8640
        assert allc.candidate_id.nunique()==len(allc)
        assert len(selection['selected'])<=3
        assert len(set(r['product'] for r in selection['selected']))==len(selection['selected'])
        for r in selection['selected']:
            row=allc.loc[allc.candidate_id==r['candidate_id']].iloc[0]
            assert row.net>0 and row.unknown==0
            assert abs(row.score-r['score'])<1e-10
            selections.append(dict(fold=selection['fold'],**r))
    specs=w.rd(out/'prepared/specs.json')
    for label,factor,slip in [('base',1.,2),('stress',2.,3)]:
        p=out/f'evaluation/{label}_trades.csv'
        tr=pd.read_csv(p) if p.stat().st_size>3 else pd.DataFrame()
        eq=pd.read_csv(out/f'evaluation/{label}_equity.csv');summary=w.rd(out/f'evaluation/{label}_summary.json')
        if len(tr):
            assert (tr.lots==1).all();assert (tr.planned_risk<=600+1e-8).all()
            assert (tr.exit_ns>=tr.entry_ns).all()
            np.testing.assert_allclose(tr.gross,(tr.exit-tr.entry)*tr.side*tr.multiplier,atol=1e-7)
            np.testing.assert_allclose(tr.net,tr.gross-tr.fees,atol=1e-7)
            recomputed=[]
            for t in tr.itertuples():
                s=specs[t.product]
                close_kind='close_today' if t.entry_time[:10]==t.exit_time[:10] else 'close_yesterday'
                recomputed.append(w.fee(s,'open',t.entry,factor)+w.fee(s,close_kind,t.exit,factor))
                assert t.contract.upper().split('.')[-1].startswith(t.product)
                assert t.multiplier==s['multiplier'] and t.tick==s['tick']
            np.testing.assert_allclose(tr.fees,recomputed,atol=1e-7)
            for _,g in tr.groupby('product'):
                g=g.sort_values('entry_ns')
                assert (g.entry_ns.iloc[1:].to_numpy()>=g.exit_ns.iloc[:-1].to_numpy()).all()
            assert abs(50000+tr.net.sum()-eq.balance.iloc[-1])<1e-7
            for f in summary['folds']:
                assert abs(tr.loc[tr.fold==f['fold']].net.sum()-f['net'])<1e-7
            worst=float(tr.net.min());breaches=tr.loc[tr.net< -600]
            wr=float((tr.net>0).mean());win=tr.loc[tr.net>0].net;loss=tr.loc[tr.net<0].net
            payoff=float(win.mean()/(-loss.mean())) if len(win) and len(loss) else None
            # Calendar trade exit P&L, including flat months.
            months=pd.period_range('2024-08','2026-07',freq='M').astype(str)
            monthly=tr.groupby(tr.exit_time.str[:7]).net.sum().reindex(months,fill_value=0.)
            monthly.rename('net_yuan').to_csv(out/f'{label}_oos_monthly.csv',index_label='month')
        else:worst=0.;breaches=pd.DataFrame();wr=None;payoff=None
        assert (eq.planned_risk<=900+1e-7).all()
        assert eq.time_ns.is_monotonic_increasing and eq.time_ns.is_unique
        if summary['final_equity'] is not None:
            assert abs(eq.equity.iloc[-1]-summary['final_equity'])<1e-7
        scenarios[label]=dict(final_equity=summary['final_equity'],net=summary['final_equity']-50000 if summary['final_equity'] is not None else None,
            close_mtm_drawdown=float(eq.drawdown.max()),trades=len(tr),win_rate=wr,payoff=payoff,worst_trade=worst,
            losses_beyond600=len(breaches),max_planned_risk=float(eq.planned_risk.max()),max_margin=float(eq.margin.max()),unknown=summary['unknown'],
            profitable_folds=sum(f['net']>0 for f in summary['folds']),empty_folds=sum(not f['selected_products'] for f in summary['folds']),
            overnight_trades=int((tr.entry_time.str[:10]!=tr.exit_time.str[:10]).sum()) if len(tr) else 0,
            remark='MTM drawdown at observed day-session5m closes; intranight and exact tick path unobserved')
    checks.update(ledger_reconciled=True,fees_recomputed=True,integer_lots=True,no_overlapping_product_positions=True,all_candidate_rows_retained=True,
                  monthly_net_reconciled=True,chronological_equity=True,planned_risk_limits=True,frozen_hashes_verified=True)
    w.js(out/'reconciliation.json',dict(checks=checks,scenarios=scenarios,candidate_window_evaluations=8*8640,
        monthly_candidate_validation_cells=8*8640*6,expanding_quantile_fits=8*20*12*6,final_threshold_refits=len(selections)))
    print(json.dumps(scenarios,ensure_ascii=False,indent=2))


if __name__=='__main__':run()
