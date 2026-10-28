"""Reuse exact economics controls, add independent selective-limit audits."""
import bisect,collections
import report_swing_portfolio_balance as report
import research_swing_portfolio_selective as r
from swing_portfolio_selective import RULES,select,counter_hour

original_audit=report.interval_audit
def interval_audit(ledger,rows,events,rule,qa):
    original_audit(ledger,rows,events,'base',qa)
    conf=RULES[rule];bykey={z['event_key']:z for z in rows};accepted=[l for l in ledger if l['accepted']];stops=collections.defaultdict(list)
    for l in accepted:
        e=report.s.ev(bykey[l['event_key']])
        if e['status']=='closed' and e['exit_reason']=='stop':stops[l['cluster']].append(e['exit_ts'])
    for ts in stops.values():ts.sort()
    for l in ledger:
        count=bisect.bisect_left(stops[l['cluster']],l['start']);assert count==l['visible_cluster_stop_count']
        event=events[l['event_key']];context=event.get('market_context') or {};value=context.get('trend_alignment_60');assert l['counter_hour']==(value is not None and value<=0)
        if l['accepted'] and conf.get('stop_limit'):assert count<conf['stop_limit']
        qa['independent_realized_stop_count']+=1
    for i,l in enumerate(accepted):
        if conf.get('cap') and l['counter_hour']:
            active=[x for x in accepted[:i] if x['end']>=l['start'] and x['sector']==l['sector'] and x['direction']==l['direction'] and x['counter_hour']]
            assert len(active)<conf['cap'];qa['independent_counter_sector_limit']+=1

def run():
    report.r=r;report.RUN=r.RUN;report.RULES=RULES;report.select=select;report.interval_audit=interval_audit
    report.run()

if __name__=='__main__':run()
