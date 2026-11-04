"""Verify persisted pair joins and report summaries without market data."""
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import run_s3_alpha as runner
import report_s3_alpha as report
import pyarrow.parquet as pq
import pandas as pd


class ReportTests(unittest.TestCase):
    def test_pair_summary_and_duplicate_control_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);events=[];controls=[]
            for i in range(2):
                e=dict(event_id=f'e{i}',signal_ns=100+i,product='C',month='2025-01',minute=540,vol_bin=1,side=1)
                for h in (6,12,24):
                    for k in ('gross_R','net_R','MFE_R','MAE_R','p1_lower','p1_upper','p2_lower','p2_upper'):e[f'{k}_{h}']=1.
                events.append(e)
            c=dict(events[0],event_id='c0',signal_ns=500)
            for h in (6,12,24):
                for k in ('gross_R','net_R','MFE_R','MAE_R','p1_lower','p1_upper','p2_lower','p2_upper'):c[f'{k}_{h}']=0.
            for name,rows in [('raw_alpha_events',events),('baseline_pool',[c])]:
                sink=runner.Sink(out/(name+'.parquet'),runner.event_schema());sink.add(rows);sink.close()
            sink=runner.Sink(out/'matched_baseline.parquet',runner.MATCH_SCHEMA)
            sink.add([dict(fold_id='F1',candidate_id='H1-C01',event_id='e0',control_id='c0',event_day='2025-01-01',control_day='2025-01-02',replicate=0,primary_delta_R=1.)]);sink.close()
            def run():
                schemas={n:pq.ParquetFile(out/(n+'.parquet')) for n in ('raw_alpha_events','baseline_pool','matched_baseline')}
                checks=dict(pairs_with_same_event_and_control_time=0)
                report.check_pairs_and_secondary(schemas,out,checks);return checks
            self.assertEqual(run()['invalid_or_reused_matched_pairs'],0)
            f=pd.read_csv(out/'secondary_matched_comparison.csv');self.assertEqual(f.delta_net_R.tolist(),[1.,1.,1.])
            sink=runner.Sink(out/'matched_baseline.parquet',runner.MATCH_SCHEMA)
            sink.add([dict(fold_id='F1',candidate_id='H1-C01',event_id='e'+str(i),control_id='c0',event_day='2025-01-01',control_day='2025-01-02',replicate=0,primary_delta_R=1.) for i in range(2)]);sink.close()
            self.assertGreater(run()['invalid_or_reused_matched_pairs'],0)


if __name__=='__main__':unittest.main()
