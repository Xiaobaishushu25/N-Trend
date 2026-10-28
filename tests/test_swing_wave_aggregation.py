import pathlib,sys,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from report_swing_wave_selection import identification_bars,first_index
class WaveAggregation(unittest.TestCase):
    def test_gap_inside_bucket_preserves_two_partial_candles(self):
        raw=[dict(datetime=t,open=o,high=c+1.,low=o-1.,close=c) for t,o,c in [('2026-01-05 09:05:00',1.,2.),('2026-01-05 09:15:00',5.,7.)]]
        bars=identification_bars(raw);self.assertEqual(len(bars),2);self.assertEqual(bars[0]['datetime'],bars[1]['datetime'])
        self.assertEqual([b['close']-b['open'] for b in bars],[1.,2.]);self.assertEqual(first_index(bars)[bars[0]['datetime']],0)
    def test_contiguous_midnight_stays_one_completed_bucket(self):
        raw=[dict(datetime=t,open=1.,high=2.,low=0.,close=1.) for t in ['2026-01-05 23:50:00','2026-01-05 23:55:00','2026-01-06 00:00:00']]
        bars=identification_bars(raw);self.assertEqual(len(bars),1);self.assertEqual(bars[0]['datetime'],'2026-01-06 00:00:00')
if __name__=='__main__':unittest.main()
