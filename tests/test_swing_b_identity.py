import copy,datetime as dt,pathlib,sys,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from swing_b_identity import features,active

class IdentityTests(unittest.TestCase):
    def fixture(self):
        raw=[]
        for minute,high,low in [(5,104,100),(10,110,102),(15,105,101),(20,106,102),(25,107,102),(30,108,101),(35,200,50),(40,200,50),(45,200,50),(50,500,1)]:
            raw.append(dict(datetime=f'2026-01-05 09:{minute:02}:00',open=103.,high=float(high),low=float(low),close=104.))
        bars=[dict(datetime=f'2026-01-05 09:{m:02}:00',open=103.,high=112.,low=low,close=104.) for m,low in [(0,100),(15,101),(30,101),(45,100)]]
        e=dict(symbol='RB0',direction='up',s0_ts='2026-01-05 08:30:00',s1_ts=bars[0]['datetime'],warning_ts=bars[-1]['datetime'],a_bars=7,b_bars=3,entry=108.,tick_size=1.)
        return e,raw,bars,{b['datetime']:i for i,b in enumerate(bars)}
    def test_parent_scope_and_excludes_warning_candle_components(self):
        e,raw,bars,index=self.fixture();f=features(e,raw,bars,index)
        self.assertEqual(f['pivot_ts'],'2026-01-05 09:10:00');self.assertEqual(f['confirmation_ts'],'2026-01-05 09:15:00');self.assertEqual(f['level'],111.);self.assertTrue(f['required']);self.assertTrue(f['fresh_b_extreme'])
        self.assertIn(e['s1_ts'],f['parent_leg_id']);self.assertLessEqual(f['confirmation_ts'],f['anchor_cutoff'])
    def test_future_append_and_warning_component_mutation_do_not_move_anchor(self):
        e,raw,bars,index=self.fixture();a=features(e,raw,bars,index);b=features(e,raw[:6],bars,index);self.assertEqual(a,b)
        raw[7]['high']=10000.;self.assertEqual(a,features(e,raw,bars,index))
    def test_no_pivot_across_gap_or_parent_boundary(self):
        e,raw,bars,index=self.fixture();raw[2]['datetime']='2026-01-05 09:16:00';self.assertFalse(features(e,raw,bars,index)['required'])
        e['s1_ts']=bars[1]['datetime'];e['b_bars']=2;self.assertFalse(features(e,raw,bars,index)['required'])
    def test_short_mirror_and_grid(self):
        e,raw,bars,index=self.fixture();a=features(e,raw,bars,index);e['direction']='down';e['entry']=200.-e['entry']
        for arr in (raw,bars):
            for b in arr:b.update(open=200.-b['open'],high=200.-b['low'],low=200.-b['high'],close=200.-b['close'])
        b=features(e,raw,bars,index);self.assertEqual(b['level'],200.-a['level']);self.assertEqual(b['required'],a['required']);self.assertEqual(b['fresh_b_extreme'],a['fresh_b_extreme'])
    def test_already_cleared_is_unchanged(self):
        e,raw,bars,index=self.fixture();e['entry']=112.;f=features(e,raw,bars,index);self.assertFalse(f['required']);self.assertEqual(f['level'],112.)
    def test_fresh_and_inside_routes_are_complements_only_where_required(self):
        e,raw,bars,index=self.fixture();f=features(e,raw,bars,index);self.assertTrue(active('fresh_close',f));self.assertFalse(active('inside_close',f));bars[-1]['low']=102.;f=features(e,raw,bars,index);self.assertFalse(active('fresh_close',f));self.assertTrue(active('inside_close',f));self.assertFalse(active('base',f))

if __name__=='__main__':unittest.main()
