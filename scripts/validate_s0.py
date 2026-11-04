"""Run S0 synthetic regressions and export independent hand-ledger expectations."""
import csv
import json
from pathlib import Path
import subprocess
import sys
from s0_engine import Engine, Bar, Order, Spec

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'artifacts/s0'


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    command=[sys.executable,'-m','unittest','discover','-s','tests','-p','test_no_lookahead.py','-v']
    result=subprocess.run(command,cwd=ROOT,text=True,capture_output=True)
    text=result.stdout+result.stderr
    (OUT/'tests_after.txt').write_text(text,encoding='utf8');print(text)
    s=Spec('DCE.c2601',0,100000,10,1,.1,2,3,4,'synthetic hand ledger')
    engine=Engine([s]);rows=[]
    for i in range(12):
        t=300+i*1200;side=1 if i%2==0 else -1
        engine.submit(Order(str(i),s.contract,t,side,90 if side==1 else 110,120 if side==1 else 80))
        engine.step([Bar(t,t+300,s.contract,'2026-11-04','day',t+1200,100,102,99,101)])
        engine.step([Bar(t+300,t+600,s.contract,'2026-11-04','day',t+1200,100,121 if side==1 else 102,99 if side==1 else 79,100)])
        tr=engine.trades[-1];expected_balance=50000+(i+1)*175
        rows.append(dict(index=i+1,side=side,expected_entry=101 if side==1 else 99,actual_entry=tr['entry'],
            expected_exit=119 if side==1 else 81,actual_exit=tr['exit'],expected_gross=180,actual_gross=tr['gross'],
            expected_fees=5,actual_fees=tr['fees'],expected_net=175,actual_net=tr['net'],
            expected_balance=expected_balance,actual_balance=engine.balance,error=engine.balance-expected_balance))
    with (OUT/'manual_ledger.csv').open('w',encoding='utf8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    with (OUT/'synthetic_account.csv').open('w',encoding='utf8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(engine.snapshots[0]));w.writeheader();w.writerows(engine.snapshots)
    passed=result.returncode==0 and all(r['error']==0 for r in rows)
    (OUT/'validation.json').write_text(json.dumps(dict(command=command,exit_code=result.returncode,
        synthetic_tests_passed=passed,ledger_trades=len(rows),max_ledger_error=max(abs(r['error']) for r in rows)),indent=2),encoding='utf8')
    if not passed:raise SystemExit(1)


if __name__=='__main__':main()
