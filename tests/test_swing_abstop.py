import pathlib,sys,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from research_swing_abstop import aggregate15,features
class ABStop(unittest.TestCase):
    def fixture(self):
        prices=[(100,110,100,108),(108,116,106,114),(114,120,112,118),(118,119,110,112),(112,116,112,115)]
        times=['2026-01-05 09:00:00','2026-01-05 09:15:00','2026-01-05 09:30:00','2026-01-05 09:45:00','2026-01-05 10:00:00']
        bars=[dict(datetime=t,open=p[0],high=p[1],low=p[2],close=p[3],components=3) for t,p in zip(times,prices)]
        e=dict(direction='up',tick_size=1.,s0_ts=times[0],s1_ts=times[2],s2_ts=times[4],warning_ts=times[4],a_bars=3,b_bars=2,s2_price=112.,stop=111.,entry=117.,risk=6.,a_move=20.,b_move=8.)
        return dict(event=e,atr14=10.),bars,{t:i for i,t in enumerate(times)}
    def test_warning_extreme_is_not_true_b_extreme(self):
        snap,bars,index=self.fixture();f=features(snap,bars,index)
        self.assertTrue(f['stop_inside_b_range']);self.assertEqual(f['b_extreme'],110.);self.assertEqual(f['true_b_stop'],109.)
    def test_future_mutation_cannot_change_stop_or_quality(self):
        snap,bars,index=self.fixture();before=features(snap,bars,index)
        bars.append(dict(datetime='2026-01-05 10:15:00',open=115.,high=150.,low=80.,close=140.,components=3));index[bars[-1]['datetime']]=5
        self.assertEqual(features(snap,bars,index),before)
    def test_midnight_is_previous_three_source_bars(self):
        raw=[dict(datetime=t,open=1.,high=2.,low=0.,close=1.,volume=1.) for t in ['2026-01-05 23:50:00','2026-01-05 23:55:00','2026-01-06 00:00:00']]
        out=aggregate15(raw);self.assertEqual(len(out),1);self.assertEqual(out[0]['components'],3);self.assertEqual(out[0]['datetime'],'2026-01-06 00:00:00')
if __name__=='__main__':unittest.main()
