import sys
import unittest
from pathlib import Path
import numpy as np
import pandas as pd
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import paired_execution_wfo as w

S=dict(tick=1.,multiplier=10.,fees={k:dict(fixed_yuan_per_lot=1.,notional_rate=0.) for k in ['open','close_today','close_yesterday']})
END=w.b.ns('2024-11-01')


def market(code='DCE.a2409'):
    start=w.b.ns('2024-08-01 09:00')
    return pd.DataFrame([dict(contract=code,start_ns=start+j*w.c.STEP,end_ns=start+(j+1)*w.c.STEP,open=100.,high=102.,low=98.,close=100.,volume=1000,valid=True) for j in range(200)])


def entry_panel(code,p):
    start=w.b.ns('2024-08-01 09:05')
    return pd.DataFrame([dict(entry_ns=start+j*1800*10**9,signal_ns=start+j*1800*10**9,contract=code,entry_i=j+1,close=100+j/20+(j%3)/10,atr=1.,month='2024-08',volume=1000,product=p) for j in range(160)])


def one_leg(p,code,side,exit_ns,reason,net):
    entry=w.b.ns('2024-08-01 09:05');price=100.+side*net/10
    return dict(product=p,contract=code,side=side,lots=1,entry_ns=entry,exit_ns=exit_ns,entry=100.,exit=price,stop=70. if side==1 else 130.,planned_risk_per_lot=300.,
        planned_risk=300.,open_fee=2.,fees=4.,gross=net,net=net-4.,tick=1.,multiplier=10.,slippage_yuan=40.,reason=reason,month='2024-08')


class PairedTests(unittest.TestCase):
    def test_feature_prefix_and_roll_reset(self):
        a=entry_panel('DCE.a2409','A');b=entry_panel('DCE.b2409','B');b['close']=100+np.arange(len(b))/40
        f=w.pair_frame(a,b,1,1,{'A':S,'B':S},'A','B');short=w.pair_frame(a.iloc[:100],b.iloc[:100],1,1,{'A':S,'B':S},'A','B')
        np.testing.assert_allclose(f[['z12','z48','z144']].iloc[:100],short[['z12','z48','z144']],equal_nan=True)
        a.loc[a.index>=100,'contract']='DCE.a2501';g=w.pair_frame(a,b,1,1,{'A':S,'B':S},'A','B')
        self.assertTrue(g.z12.iloc[100:112].isna().all())

    def test_integer_hedge_fitting_does_not_use_validation_returns(self):
        times=pd.date_range('2023-11-01',periods=150,freq='D',tz='Asia/Shanghai');rng=np.random.default_rng(7);x=100+np.cumsum(rng.normal(size=150))
        def f(p,prices):return pd.DataFrame(dict(entry_ns=times.as_unit('ns').astype('int64'),close=prices,contract=p+'2409',month=times.strftime('%Y-%m')))
        a=f('A',x);b=f('B',2*x+rng.normal(0,.1,150));frames={'A':a,'B':b};args=({'A':S,'B':S},w.b.ns('2023-11-01'),w.b.ns('2024-02-01'),{'A0':1380,'B0':1380})
        _,r,best=w.hedge_search(frames,*args);b.loc[b.entry_ns>=w.b.ns('2024-02-01'),'close']=1e9
        _,r2,best2=w.hedge_search(frames,*args);self.assertEqual(r,r2);self.assertEqual(best,best2);self.assertEqual(len(r),9)
        self.assertEqual((best[0]['qty_a'],best[0]['qty_b']),(2,1))

    def test_own_stop_and_peer_next_open_not_synthetic_spread_fill(self):
        a=market('A');b=market('B');t=int(a.end_ns.iloc[2]);legs=[one_leg('A','A',1,t,'stop',-100),one_leg('B','B',-1,int(b.start_ns.iloc[20]),'time_or_window',50)]
        b.loc[3,'open']=90.
        q=w.align_exits(legs,{'A':a,'B':b},{'A':S,'B':S},END,2,1.)
        self.assertEqual(q[1]['exit_ns'],int(b.start_ns.iloc[3]));self.assertEqual(q[1]['exit'],92.)
        self.assertEqual(q[0]['exit_ns'],t);self.assertEqual(q[0]['net'],-104)

    def test_gap_open_exit_peer_waits_one_more_quote(self):
        a=market('A');b=market('B');tm=int(a.start_ns.iloc[2]);legs=[one_leg('A','A',1,tm,'gap_stop',-100),one_leg('B','B',-1,int(b.start_ns.iloc[20]),'time_or_window',0)]
        q=w.align_exits(legs,{'A':a,'B':b},{'A':S,'B':S},END,2,1.)
        self.assertEqual(q[1]['exit_ns'],int(b.start_ns.iloc[3]))

    def test_earlier_peer_protection_stays_live(self):
        a=market('A');b=market('B');tm=int(a.start_ns.iloc[2]);t2=int(b.end_ns.iloc[2]);legs=[one_leg('A','A',1,tm,'gap_stop',-100),one_leg('B','B',-1,t2,'stop',-30)]
        b.loc[3,['high','low','open','close']]=100.
        q=w.align_exits(legs,{'A':a,'B':b},{'A':S,'B':S},END,2,1.)
        self.assertEqual(q[1]['reason'],'stop');self.assertEqual(q[1]['exit_ns'],t2)

    def test_scaled_costs_and_risk_use_integer_lots(self):
        t=one_leg('A','A',1,1,'stop',-100);q=w.scaled(t,3)
        self.assertEqual(q['lots'],3);self.assertEqual(q['fees'],12.);self.assertEqual(q['planned_risk'],900.);self.assertEqual(q['planned_risk_per_lot'],300.)

    def test_combined900_budget_and_unknown_entry(self):
        a=market('A');b=market('B');fa=entry_panel('A','A').iloc[:1];fb=entry_panel('B','B').iloc[:1];fa['atr']=10.;fb['atr']=10.
        f=w.pair_frame(fa,fb,3,3,{'A':S,'B':S},'A','B');choice=dict(pair='A__B',a='A',b='B',qty_a=3,qty_b=3)
        t=w.pair_paths(f,choice,{'A':S,'B':S},{'A':a,'B':b},12,3.,END)
        self.assertNotIn((0,1),t)
        fa['atr']=1.;fb['atr']=1.;f=w.pair_frame(fa,fb,1,1,{'A':S,'B':S},'A','B');choice.update(qty_a=1,qty_b=1)
        a.loc[1,['open','high','low','close']]=100.;t=w.pair_paths(f,choice,{'A':S,'B':S},{'A':a,'B':b},12,3.,END)
        self.assertTrue(t[0,1]['unknown']);self.assertNotIn('net',t[0,1])

    def test_same_session_requirement_before_search(self):
        times=pd.date_range('2023-11-01',periods=80,freq='D',tz='Asia/Shanghai');f=pd.DataFrame(dict(entry_ns=times.as_unit('ns').astype('int64'),close=np.arange(80.),contract='X',month=times.strftime('%Y-%m')))
        _,tr,sel=w.hedge_search({'A':f,'B':f},{'A':S,'B':S},w.b.ns('2023-11-01'),w.b.ns('2024-02-01'),{'A0':None,'B0':1380})
        self.assertEqual(tr,[]);self.assertEqual(sel,[])

    def test_leg_cash_not_booked_early_and_common_move_offsets(self):
        a=market('A');b=market('B');entry=int(a.start_ns.iloc[1]);exit_a=int(a.end_ns.iloc[2]);exit_b=int(a.start_ns.iloc[3])
        t1=one_leg('A','A',1,exit_a,'stop',-100);t2=one_leg('B','B',-1,exit_b,'peer_exit_next_quote',160)
        pair=dict(unknown=False,pair='A__B',entry_ns=entry,exit_ns=exit_b,planned_risk=600.,legs=[t1,t2]);r=dict(candidate_id='x',choice=dict(a='A',b='B'))
        quotes={int(row.start_ns):{'A':row,'B':b.iloc[i]} for i,row in enumerate(a.itertuples())}
        result=w.account(sorted(quotes),quotes,{entry:[(0,r,pair,1)]},[dict(fold=1,test='2024-08-01',end='2024-11-01')],{'A':S,'B':S})
        before=[s for s in result['equity'] if s['time_ns']==exit_b][0]
        self.assertAlmostEqual(before['balance'],49894.);self.assertAlmostEqual(result['final_equity'],50052.)
        self.assertEqual(len(result['pairs']),1);self.assertEqual(len(result['legs']),2)

    def test_search_size_fixed(self):self.assertEqual(len(w.GRID),72)


if __name__=='__main__':unittest.main()
