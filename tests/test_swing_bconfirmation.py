import pathlib,sys,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from research_swing_bconfirmation import anchor
class BConfirm(unittest.TestCase):
    def fixture(self):
        bars=[dict(datetime=str(i),open=o,close=c) for i,(o,c) in enumerate([(100,110),(120,119),(119,118),(118,110),(111,110),(110,112)])]
        e=dict(direction='up',s1_ts='1',warning_ts='5',tick_size=1.,entry=114.,warning_kind='strong')
        return dict(event=e,mechanism=dict(b_weakening_ex_warning=1.2)),bars,{str(i):i for i in range(len(bars))}
    def test_covering_last_small_body_does_not_cover_main_b_push(self):
        snap,bars,index=self.fixture();a=anchor(snap,bars,index)
        self.assertTrue(a['required']);self.assertEqual(a['anchor_open'],118.);self.assertEqual(a['level'],119.)
    def test_future_bars_and_other_warning_types_do_not_activate_rule(self):
        snap,bars,index=self.fixture();a=anchor(snap,bars,index);bars.append(dict(datetime='6',open=200.,close=1.));index['6']=6
        self.assertEqual(anchor(snap,bars,index),a)
        snap['event']['warning_kind']='cumulative';self.assertFalse(anchor(snap,bars,index)['required'])
    def test_already_weakened_b_keeps_existing_entry(self):
        snap,bars,index=self.fixture();snap['mechanism']['b_weakening_ex_warning']=.5;self.assertFalse(anchor(snap,bars,index)['required'])
if __name__=='__main__':unittest.main()
