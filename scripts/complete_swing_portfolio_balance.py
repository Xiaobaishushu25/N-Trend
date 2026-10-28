"""Final integrity ledger after frozen replay; never changes frozen policies."""
import datetime as dt,subprocess,sys
import research_swing_portfolio_balance as r
w=r.w;RUN=r.RUN;EXT=RUN/'selective';SNAP=w.REPO/'research-strategies/best-a-portfolio-observation-v1'

def run():
    p=w.read(RUN/'protocol.json');ep=w.read(EXT/'protocol.json');fv=w.read(RUN/'frozen-replay-verification.json');assert fv['all_exact'] and len(fv['metrics'])==96 and fv['accepted_sets_exact']==24
    proc=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p','test_swing_portfolio_selective.py','-v'],cwd=w.REPO,capture_output=True,text=True);assert proc.returncode==0,proc.stderr
    w.write(RUN/'final-test-verification.json',dict(command='python -m unittest discover -s tests -p test_swing_portfolio_selective.py -v',exit_code=proc.returncode,unique_tests=10,description='7 base chronology/unknown tests plus3 selective counter-category/stop-count boundary tests.',output=proc.stdout+proc.stderr))
    for q in ['early','recent']:
        for k,path in p['periods'][q].items():assert w.sha(path)==p['hashes'][q][k],(q,k)
        for k in ['raw','pool','manifest']:assert w.sha(p['sources'][q][k])==p['sources'][q][k+'_sha256']
    for root in [r.SNAP,r.ECON_SNAP,SNAP]:assert all(w.sha(root/k)==v for k,v in w.read(root/'manifest.json').items())
    assert all(w.sha(k)==v for k,v in p['production_before'].items());assert w.sha(r.SECTORS)==p['sector_map_sha256'];assert w.sha(w.REPO/'scripts/swing_portfolio_balance.py')==p['controller_sha256'];assert w.sha(w.REPO/'scripts/swing_portfolio_selective.py')==ep['controller_sha256']
    assert w.sha(EXT/'parent-report-before-extension.py')==w.read(RUN/'analysis-plan-before-outcomes.json')['report_sha256']
    checks=[w.read(folder/'verification.json') for folder in [RUN,EXT]];assert all(len(z['controls'])==64 for z in checks)
    artifacts=['summary.md','report.md','protocol.json','analysis-plan-before-outcomes.json','integrated-scorecard.json','candidate-definitions.json','decision.json','results.json','scorecard.json','evaluation.json','attribution.json','risk-contributions.json','reservation-risk.json','opportunity-ledgers.json','leave-product-out.json','verification.json','final-test-verification.json','frozen-replay-verification.json','selective/protocol.json','selective/analysis-plan-before-outcomes.json','selective/results.json','selective/scorecard.json','selective/evaluation.json','selective/attribution.json','selective/verification.json','selective/robustness-plan-before-checks.json','selective/additional-robustness.json','selective/mechanism-diagnosis.json','selective/validation-metadata-fix.json']
    sources=['scripts/swing_portfolio_balance.py','scripts/research_swing_portfolio_balance.py','scripts/report_swing_portfolio_balance.py','scripts/swing_portfolio_selective.py','scripts/research_swing_portfolio_selective.py','scripts/report_swing_portfolio_selective.py','scripts/validate_swing_portfolio_selective.py','scripts/finish_swing_portfolio_balance.py','scripts/check_swing_portfolio_frozen.py','scripts/complete_swing_portfolio_balance.py','tests/test_swing_portfolio_balance.py','tests/test_swing_portfolio_selective.py']
    w.write(RUN/'completion.json',dict(status='complete_two_portfolio_observations_frozen',completed_at=dt.datetime.now(dt.timezone.utc).isoformat(),main_new_combinations=14,post_main_exploratory_combinations=6,baseline_controls=128,frozen_metric_records=96,frozen_accepted_sequences=24,unique_unit_tests=10,per_candidate_full_leave_product_checks=176,per_candidate_seeded_tie_checks=24,best_a_preserved=len(w.read(r.SNAP/'manifest.json')),economic_snapshot_preserved=len(w.read(r.ECON_SNAP/'manifest.json')),production_unchanged=len(p['production_before']),frozen_snapshot=str(SNAP),snapshot_manifest_sha256=w.sha(SNAP/'manifest.json'),independent_validation=False,full_cycle_stability_reached=False,product_balance_solved=False,paper_collection_started=False,production_changed=False,qualified_for_trading=False,artifacts={k:w.sha(RUN/k) for k in artifacts},sources={k:w.sha(w.REPO/k) for k in sources}))
    print('Complete:20 new combinations;2 frozen research policies;96 frozen metrics/24 sequences exact;10 tests; all source data and snapshots unchanged.',flush=True)

if __name__=='__main__':run()
