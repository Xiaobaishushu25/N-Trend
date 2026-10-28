"""Exactly six exploratory combinations, frozen after main portfolio failures."""
import datetime as dt
import research_swing_portfolio_balance as parent
from swing_portfolio_selective import RULES
w=parent.w;SNAP=parent.SNAP;ECON_SNAP=parent.ECON_SNAP;NATIVE=parent.NATIVE;SECTORS=parent.SECTORS;PROFILES=parent.PROFILES
RUN=parent.RUN/'selective'

def prepare():
    RUN.mkdir(exist_ok=False);p=w.read(parent.RUN/'protocol.json')
    old=w.read(parent.RUN/'analysis-plan-before-outcomes.json')['report_sha256']
    report=(w.REPO/'scripts/report_swing_portfolio_balance.py').read_text(encoding='utf-8')
    before=report.replace("w.REPO/p.get('controller_source','scripts/swing_portfolio_balance.py')","w.REPO/'scripts/swing_portfolio_balance.py'").replace("new_combinations=(len(RULES)-1)*len(r.PROFILES)","new_combinations=14")
    (RUN/'parent-report-before-extension.py').write_text(before,encoding='utf-8',newline='\n')
    # Source files originally used CRLF. Verify reconstructed content independently below.
    original_hash=w.sha(RUN/'parent-report-before-extension.py')
    p.update(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),rules=RULES,controller_source='scripts/swing_portfolio_selective.py',controller_sha256=w.sha(w.REPO/'scripts/swing_portfolio_selective.py'),
        experiment='After inspecting14 main failures, add exactly3 rules x2 fixed profiles=6 exploratory combinations; no further threshold sweep.',
        definitions='sector2_counter: cap2 live COUNTER-HOUR reservations per fixed sector+direction; counter-hour means original warning trend_alignment_60<=0; aligned/missing-hour pass without this cap. after_two_stops: exact wave blocked only after2 accepted original stop exits have realized. Conjunction only, no other variants.',
        rationale='Hard group caps clip good opportunities and first-loss/first-stop blocks remove profitable retries. Restrict cap to original counter-hour category; permit one stop retry. All decisions at trigger use warning features and exits strictly before trigger.',
        parent_outcomes_sha256=w.sha(parent.RUN/'scorecard.json'),parent_results_sha256=w.sha(parent.RUN/'results.json'),source_after_main=True)
    w.write(RUN/'protocol.json',p)
    w.write(RUN/'analysis-plan-before-outcomes.json',dict(created_at=p['created_at'],models=list(RULES),new_combinations=6,current_extension_outcomes_not_inspected=True,parent_outcomes_already_inspected=True,report_sha256=w.sha(w.REPO/'scripts/report_swing_portfolio_balance.py'),wrapper_sha256=w.sha(w.REPO/'scripts/report_swing_portfolio_selective.py'),audit_only_report_changes=['Configurable controller source hash','Dynamic number of combinations'],parent_report_recorded_hash=old,reconstructed_parent_report_hash=original_hash))
    print('Frozen6 post-main exploratory combinations; no independent validation.',flush=True)

if __name__=='__main__':prepare()
