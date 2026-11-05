import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import r1_diagnostics as r


class R1Tests(unittest.TestCase):
    def test_label_cost_reconciliation_and_censoring(self):
        n=10
        a=dict(day=r.np.array(['2024-01-03']*n),segment=r.np.ones(n),valid=r.np.ones(n,dtype=bool),
            volume=r.np.ones(n),minute=r.np.arange(540,590,5),end_ns=r.np.arange(1,n+1),
            open=r.np.full(n,100.),close=r.np.full(n,103.),high=r.np.full(n,105.),low=r.np.full(n,99.),atr=r.np.full(n,2.))
        rule=dict(fixed_yuan_per_lot=1.,notional_rate=0.)
        fee=dict(open=rule,close_today=rule)
        z=r.path_label(a,0,1,6,dict(tick=1.,multiplier=10.),fee,100)
        self.assertEqual(z['gross_yuan'],30.)
        self.assertEqual(z['cost_yuan'],22.)
        self.assertEqual(z['net_yuan'],8.)
        self.assertEqual(z['net_2tick_yuan'],-12.)
        self.assertAlmostEqual(z['gross_R']-z['cost_R'],z['net_R'])
        a['day'][4]='2024-01-04'
        a['segment'][4:]=2
        self.assertNotEqual(r.path_label(a,0,1,6,dict(tick=1.,multiplier=10.),fee,100)['status'],'complete')

    def test_date_weighting_is_distinct(self):
        rows=[]
        for day,v in [('a',10.),('a',10.),('b',-10.)]:
            row=dict(group='g',day=day,status='complete')
            row.update({c:v for c in ['gross_R','cost_R','net_R','gross_yuan','cost_yuan','net_yuan','MFE_R','MAE_R','net_2tick_yuan']})
            rows.append(row)
        x=r.summarize_events(r.pd.DataFrame(rows),['group']).iloc[0]
        self.assertAlmostEqual(x.net_R,10/3)
        self.assertEqual(x.net_R_date_equal,0.)

    def test_reserved_period_rejected_before_sql(self):
        with self.assertRaises(AssertionError):r.load_frames(None,'2024-11-01','2026-01-01',None,None,None)


if __name__=='__main__':unittest.main()
