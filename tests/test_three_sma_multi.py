import unittest,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import research_three_sma_multi as s

class Tests(unittest.TestCase):
 def test_signal_is_causal_and_delayed_over_break(self):
  start=s.ns('2024-01-02 09:00:00')
  warm=[(start-(19-i)*900_000_000_000, i+1., i+1., i+1.) for i in range(19)]
  bars=warm+[(s.ns('2024-01-02 10:15:00'),20.,20.,20.)]
  minutes=[(s.ns('2024-01-02 10:14:00'),100.,100.,1),(s.ns('2024-01-02 10:30:00'),101.,102.,1)]
  pnl,trades,eq=s.replay(minutes,bars,'baseline',10,1)
  self.assertEqual(trades[0][0],'2024-01-02 10:30')
  self.assertEqual(trades[0][-1],'segment_end')
  self.assertAlmostEqual(pnl,trades[0][3],places=4)
  self.assertAlmostEqual(eq[-1],pnl,places=5)
 def test_pending_waits_for_volume(self):
  start=s.ns('2024-01-02 09:00:00')
  bars=[(start-(19-i)*900_000_000_000,i+1.,i+1.,i+1.) for i in range(19)]
  bars.append((s.ns('2024-01-02 10:15:00'),20.,20.,20.))
  minutes=[(s.ns('2024-01-02 10:14:00'),100.,100.,1),(s.ns('2024-01-02 10:15:00'),100.,100.,0),(s.ns('2024-01-02 10:16:00'),101.,102.,1)]
  pnl,trades,_=s.replay(minutes,bars,'baseline',10,1)
  self.assertEqual(trades[0][0],'2024-01-02 10:16')
 def test_flat_on_each_segment(self):
  start=s.ns('2024-01-02 09:00:00')
  bars=[(start-(19-i)*900_000_000_000,i+1.,i+1.,i+1.) for i in range(19)]
  minutes=[(start,100.,100.,1)]
  self.assertEqual(s.replay(minutes,bars,'atr_entry_relaxed_exit',10,1)[1],[])
if __name__=='__main__':unittest.main()
