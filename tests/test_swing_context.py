import pathlib,sys,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from research_swing_context import allows
class Context(unittest.TestCase):
    def test_missing_is_not_neutral_alignment(self):
        self.assertTrue(allows('base',{}))
        for n in ['h60_aligned','d10_aligned','both_aligned','h60_counter','d10_trend_h60_pullback']:self.assertFalse(allows(n,{}))
    def test_sign_and_pullback_are_direction_relative(self):
        e={'market_context':{'trend_alignment_60':-1.,'trend_alignment_10d':2.}}
        self.assertTrue(allows('d10_trend_h60_pullback',e));self.assertFalse(allows('both_aligned',e))
        e['market_context']['trend_alignment_60']=1.;self.assertTrue(allows('both_aligned',e));self.assertFalse(allows('d10_trend_h60_pullback',e))
if __name__=='__main__':unittest.main()
