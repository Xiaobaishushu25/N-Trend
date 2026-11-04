import copy
import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest

import yaml
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import s2_preregister as s


class S2Tests(unittest.TestCase):
    def setUp(self):
        self.hs=s.hypotheses();self.b=s.budget(['C','JD']);self.cs=s.candidates(self.hs,self.b['universe'])

    def fixture(self,out):
        for name,obj in [('hypotheses.yaml',{'hypotheses':self.hs}),('search_budget.yaml',self.b)]:
            (out/name).write_text(yaml.safe_dump(obj),encoding='utf8')
        s.write_json(out/'candidate_manifest.json',{'candidates':self.cs})
        s.write_csv(out/'wfo_folds.csv',list(s.folds()[0]),s.folds())
        (out/'wfo_reservations.jsonl').write_text('',encoding='utf8')
        fake=out/'synthetic.sqlite';fake.write_bytes(b'synthetic database, no bars read')
        s.write_json(out/'freeze_manifest.json',dict(frozen_files={n:s.sha(out/n) for n in ['hypotheses.yaml','search_budget.yaml','candidate_manifest.json','wfo_folds.csv']},
            code_sha256={},input_sha256={},database={'path':str(fake),'sha256':s.sha(fake)}))

    def test_exact_grid_and_stable_ids(self):
        s.validate_documents(self.hs,self.b,self.cs)
        self.assertEqual(len(self.cs),24)
        self.assertEqual(self.cs,s.candidates(self.hs,self.b['universe']))
        self.assertTrue(all(c['signal_implementation_sha'] is None for c in self.cs))

    def test_extra_candidate_and_third_continuous_parameter_rejected(self):
        with self.assertRaises(ValueError):s.validate_documents(self.hs,self.b,self.cs+[copy.deepcopy(self.cs[0])])
        hs=copy.deepcopy(self.hs);hs[0]['continuous_parameters'].append('hidden')
        with self.assertRaises(ValueError):s.validate_documents(hs,self.b,self.cs)

    def test_unbudgeted_side_subset_and_session_rejected_even_if_rehashed(self):
        for key,value in [('directions',['long']),('universe',['JD']),('entry_sessions',['night'])]:
            cs=copy.deepcopy(self.cs);cs[0][key]=value
            cs[0]['candidate_sha256']=s.canonical({k:v for k,v in cs[0].items() if k!='candidate_sha256'})
            with self.assertRaises(ValueError):s.validate_documents(self.hs,self.b,cs)

    def test_parameter_tampering_rejected(self):
        cs=copy.deepcopy(self.cs);cs[0]['parameters']['impulse_atr_min']=99
        with self.assertRaises(ValueError):s.validate_documents(self.hs,self.b,cs)

    def test_chronological_fold_windows_and_final_diagnostic(self):
        f=s.folds()
        self.assertEqual(f[0]['test_start'],'2024-11-01')
        self.assertEqual(f[-1]['test_end'],'2026-05-01')
        for a,b in zip(f,f[1:]):self.assertEqual(a['test_end'],b['test_start'])
        self.assertEqual(self.b['final_diagnostic']['end_exclusive'],'2026-11-01')

    def test_file_seal_detects_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);self.fixture(out);s.verify(out)
            with (out/'hypotheses.yaml').open('a',encoding='utf8') as f:f.write('\n# changed\n')
            with self.assertRaisesRegex(ValueError,'frozen artifact'):s.verify(out)

    def test_wfo_two_reservations_then_exhausted_failures_not_refunded(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);self.fixture(out)
            self.assertEqual(s.reserve_wfo(out,'test failed run')['reservation_id'],'WFO-01')
            self.assertEqual(s.reserve_wfo(out,'second evaluation')['reservation_id'],'WFO-02')
            with self.assertRaisesRegex(ValueError,'exhausted'):s.reserve_wfo(out,'third')
            self.assertEqual(len(s.reservations(out)),2)
            self.assertFalse((out/'.reservation.lock').exists())

    def test_reservation_chain_and_changed_database_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);self.fixture(out);s.reserve_wfo(out,'initial')
            journal=out/'wfo_reservations.jsonl';journal.write_text(journal.read_text(encoding='utf8').replace('initial','changed'),encoding='utf8')
            with self.assertRaisesRegex(ValueError,'chain changed'):s.verify(out)
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);self.fixture(out);(out/'synthetic.sqlite').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'database changed'):s.reserve_wfo(out,'initial')

    def test_preregistered_inference_and_roles_are_not_search_axes(self):
        self.assertEqual(self.b['s3_design']['primary_horizon_bars'],12)
        self.assertEqual(self.b['s3_design']['matching']['seed'],20261104)
        self.assertEqual(self.b['selection']['max_selected_per_family'],1)
        self.assertFalse(self.b['selection']['rule_changes_after_selection'])
        self.assertIn('Holm',self.b['s3_design']['inference']['primary_multiple_testing'])
        self.assertEqual(self.b['leakage']['enforcement'],'logical API/runner contract, not operating-system access isolation')


if __name__=='__main__':unittest.main()
