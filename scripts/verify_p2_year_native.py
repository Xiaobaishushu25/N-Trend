"""Compare the new stdin P2 harness with frozen archived executions."""
import json
import pathlib
import sys
sys.dont_write_bytecode=True
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
import backtest_b5_p2_year as r

def main():
    root=r.RUN/'regression-p2';root.mkdir(exist_ok=True)
    source=r.R(r.BASE/'external/input.json');source['symbols']={s:z for s,z in source['symbols'].items() if s=='EG0'}
    contracts={c:z for c,z in r.R(r.BASE/'external/contracts.json').items() if r.product(c)=='EG0'}
    protocol=dict(warning_start='2026-01-01 00:00:00',warning_end='2026-06-30 15:00:00',fees=r.R(r.REPO/'scripts/exchange_fees_20261022.json'),segments=r.R(r.BASE/'external/segments.json'),holidays=r.R(r.BASE/'external/holidays.json'))
    if not (root/'p2-evaluations.jsonl').exists():r.rust_input(r.RUN/'p2_year.exe',[root],dict(input=source,contracts=contracts,protocol=protocol))
    old={}
    for line in (r.BASE/'external/replay/P2.jsonl').open(encoding='utf-8'):
        z=json.loads(line)
        if z['symbol']=='EG0':old[z['event_key'],z['scenario']]=z
    checked=0;diff=[]
    for line in (root/'p2-evaluations.jsonl').open(encoding='utf-8'):
        z=json.loads(line);ref=old.pop((z['event_key'],'P2/'+z['scenario']))
        # The newer frozen engine also exposes trigger_ts; compare all fields
        # available in the earlier archive, including entire fills/disagreements.
        left=ref['result'];right=z['result']
        def legacy(v):
            if isinstance(v,dict):return {k:legacy(x) for k,x in v.items() if k!='trigger_ts'}
            if isinstance(v,list):return [legacy(x) for x in v]
            return v
        if legacy(left)!=legacy(right):diff.append(dict(key=z['event_key'],scenario=z['scenario'],before=left,after=right))
        assert z['execution_contract']==ref['execution_contract'];checked+=1
    assert not old, len(old)
    r.old.write(root/'legacy-fields-verification.json',dict(checked=checked,result_differences=diff,all_contracts_equal=True,new_field='trigger_ts: used for earlier reservation, absent in legacy archive'))
    assert not diff,('new transport changed native results',diff[:1])
    print('P2 complete raw results and actual contracts exactly match archive:',checked)

if __name__=='__main__':main()
