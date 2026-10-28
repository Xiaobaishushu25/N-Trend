import pathlib,sys,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from research_swing_execution_interaction import choose
class Interaction(unittest.TestCase):
    def test_strong_and_opposed_are_different_natural_conditions(self):
        e=dict(direction='up',market_context=dict(trend_alignment_60=-2.,trend_slope_60=1.))
        self.assertEqual(choose('strong_kind_session',e),'kind_loss');self.assertEqual(choose('opposed_kind_session',e),'kind')
        e['market_context'].update(trend_alignment_60=-.2,trend_slope_60=-1.)
        self.assertEqual(choose('strong_kind_session',e),'kind');self.assertEqual(choose('opposed_kind_session',e),'kind_loss')
    def test_unknown_context_and_aligned_context_preserve_reference(self):
        for e in [dict(direction='up'),dict(direction='up',market_context=dict(trend_alignment_60=1.,trend_slope_60=-1.))]:
            self.assertEqual(choose('counter_kind_session',e),'base')
if __name__=='__main__':unittest.main()
