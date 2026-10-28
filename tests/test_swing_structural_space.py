import pathlib,sys,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from research_swing_structural_space import structural_rr
class StructuralSpace(unittest.TestCase):
    def test_wider_structure_reduces_forecast_rr_without_changing_trade(self):
        r=dict(direction='up',tick_size=1.,multiplier=10.,entry=100.,target=125.)
        fee=dict(roundtrip_fixed_yuan_per_lot=2.)
        self.assertGreater(structural_rr(r,90.,fee),structural_rr(r,80.,fee));self.assertEqual(r['entry'],100.)
    def test_forecast_is_not_reselected_by_execution_stress(self):
        r=dict(direction='up',tick_size=1.,multiplier=10.,entry=100.,target=125.,slip=1.)
        fee=dict(roundtrip_fixed_yuan_per_lot=2.);before=structural_rr(r,80.,fee);r['slip']=2.
        self.assertEqual(structural_rr(r,80.,fee),before)
if __name__=='__main__':unittest.main()
