"""Portfolio-only opportunity budgets on frozen profitable economic candidates."""
import datetime as dt
import research_swing_entry_economics as source
from swing_portfolio_balance import RULES
w=source.w;SNAP=source.SNAP;ECON_SNAP=w.REPO/'research-strategies/best-a-entry-economics-observation-v1'
RUN=w.REPO/'research-runs/swing-portfolio-balance-20261026-v1'
NATIVE=source.RUN/'native-verification';PROFILES=['budget10','budget_body'];SECTORS=w.REPO/'research-protocols/breadth-sector-map-20261023-v1.json'

def prepare():
    assert all(w.sha(SNAP/k)==v for k,v in w.read(SNAP/'manifest.json').items());assert all(w.sha(ECON_SNAP/k)==v for k,v in w.read(ECON_SNAP/'manifest.json').items());RUN.mkdir(exist_ok=False)
    p=w.read(NATIVE/'protocol.json');sector=w.read(SECTORS);mapping={sym:group for group,syms in sector['groups'].items() for sym in syms};assert len(mapping)==sum(map(len,sector['groups'].values()))
    p.update(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),profiles=PROFILES,rules=RULES,sector_map=mapping,sector_map_sha256=w.sha(SECTORS),sector_map_source=str(SECTORS),script_sha256=w.sha(__file__),controller_sha256=w.sha(w.REPO/'scripts/swing_portfolio_balance.py'),
        experiment='Seven portfolio opportunity budgets x two fixed economic candidates=14 new combinations plus2 controls. No entry/stop/target/exit/lot/fee changes, no per-product profitability whitelist, no parameter grid. Existing fixed3 broad sector map used as exposure proxy, not fitted correlation.',
        definitions='one_attempt:max1 accepted trigger attempt per exactsymbol/direction/S0/S1; after_loss:deny only once same exact wave cumulative ACCEPTED realized net is negative; after_stop:deny only after accepted original stop exit. sector1/2_same:max1/2 live reservations persector+direction; sector1_any:max1 persector bothdirections. sector2_loss combines2 same-direction slots and realized cluster loss.',
        causal='Closing/cancellation/known-flat events release reservations only when end<nexttrigger, same-minute stays occupied. Closed accepted cash becomes visible ONLY after exit<nexttrigger. Unknown PnL never updates loss history; conservative contract-end and both-path-flat exposure policies both tested. Wave budgets carry acrossJune/July, no monthly reset; new parent can retry.',
        balance='Primary combined known closed net vs each economic reference and current highest-profit reference. Secondary product positive-PnL HHI/top3 share, worst month/negative-month burden, realized drawdown and PEAK RESERVED NOMINAL cashR. Peak includes triggered-but-unfilled orders/unknown exposure; not margin or full-equity risk.',
        size='All accepted orders retain1lot. Analyze differing one-lot cash risk and R-profit distribution, do NOT increase size to inflate profit or use fractional futures lots. No account capital amount assumed.',
        source_usage='Reuse all raw individual executions from native gated executor, including unfilled/filtered/unknowns; recompute complete chronology and reservations, not only previously accepted trades. Independent individual fills unchanged by a pure portfolio veto in this research model.',
        objective='User: improve combined net and product distribution, no80% recent constraint or mandatory every-period profit. All repeatedly researched history; no holdout or live promotion.',production_changed=False,qualified_for_trading=False)
    p['sources']={q:dict(pool=str(NATIVE/(q+'-pool.json')),pool_sha256=w.sha(NATIVE/(q+'-pool.json')),raw=str(NATIVE/q/'evaluations.jsonl'),raw_sha256=w.sha(NATIVE/q/'evaluations.jsonl'),manifest=str(NATIVE/q/'manifest.json'),manifest_sha256=w.sha(NATIVE/q/'manifest.json')) for q in ['early','recent']}
    for q in ['early','recent']:
        for z in w.read(p['sources'][q]['pool']):assert z['event']['symbol'] in mapping
    w.write(RUN/'protocol.json',p);print('Frozen14 portfolio combinations, fixed sector map and one-lot sizing',flush=True)

if __name__=='__main__':prepare()
