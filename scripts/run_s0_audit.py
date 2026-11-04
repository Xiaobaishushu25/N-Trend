"""Read-only S0 archive audit. No signals, strategy results, network or orders."""
import argparse
import bisect
import collections
import csv
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import subprocess
from factor_study_data import Calendar, local, stamp, digest

ROOT=Path(__file__).resolve().parents[1]
NS=1_000_000_000


def json_write(path, value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8')


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--out',type=Path,default=ROOT/'artifacts/s0')
    ap.add_argument('--db',type=Path,action='append')
    args=ap.parse_args();out=args.out.resolve();out.mkdir(parents=True,exist_ok=True)
    sources=args.db or [ROOT/'data/research_market_history.sqlite',ROOT/'data/research_market_backfill.sqlite']
    protocol=json.loads((ROOT/'scripts/factor-study-v1.json').read_text(encoding='utf8'))
    cal_path=ROOT/'data/trading_calendar.json';cal_data=json.loads(cal_path.read_text(encoding='utf8'))
    calendar=Calendar(cal_data,protocol['night_ends'])
    precision=(ROOT/'crates/n-core/src/precision.rs').read_text(encoding='utf8')
    def table(name):
        return {k:float(v) for k,v in re.findall(r'\("([A-Z]+)",\s*([\d.]+)\)',precision.split('pub const '+name)[1].split('];')[0])}
    ticks=table('DEFAULT_TICKS');mult=table('DEFAULT_MULTIPLIERS')
    fee_path=ROOT/'scripts/exchange_fees_20261022.json'
    fee_snapshot=json.loads(fee_path.read_text(encoding='utf8'))
    details=out/'audit_data.csv'; summaries=[];specs=[];inputs={};issue_counts=collections.Counter()
    cols=['source','contract','start_ns','end_ns','issue','detail','independent_evidence']
    with details.open('w',encoding='utf8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=cols);writer.writeheader()
        def issue(source,c,start,end,kind,detail):
            issue_counts[kind]+=1
            writer.writerow(dict(source=source,contract=c,start_ns=start,end_ns=end,issue=kind,detail=detail,independent_evidence=False))
        for path in sources:
            path=path.resolve();name=str(path.relative_to(ROOT))
            if not path.exists():issue(name,'','','','missing_source','required archive absent');continue
            print('AUDIT',name,flush=True)
            inputs[name]=digest(path)
            db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True);db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            maps=db.execute('select product,effective_date,contract,source,source_sha256 from mappings order by product,effective_date').fetchall()
            products=sorted({r[0] for r in maps});mapping_contracts={r[2] for r in maps}
            issue(name,'*','','','mapping_publication_unknown','snapshot effective_date lacks available_at; all mapped research remains diagnostic')
            summary=dict(source=name,products=products,mapping_rows=len(maps),periods=db.execute('select period_seconds,count(*),min(start_ns),max(end_ns) from bars group by period_seconds').fetchall(),contracts=[])
            duplicates=db.execute('select contract,start_ns,count(*) from bars where period_seconds=300 group by contract,start_ns having count(*)>1').fetchall()
            for c,t,n in duplicates:issue(name,c,t,t,'duplicate',n)
            contracts=[r[0] for r in db.execute('select distinct contract from bars where period_seconds=300 order by contract')]
            for c in contracts:
                match=re.fullmatch(r'(SHFE|DCE|CZCE|GFEX|INE)\.([a-zA-Z]+)(\d{3,4})',c)
                if not match:issue(name,c,'','','not_real_contract','contract identity not validated');continue
                product=match[2].upper();symbol=product+'0'
                # Export local evidence only. Unknown effective dates/fees/margins stay blank.
                specs.append(dict(source=name,contract=c,product=product,multiplier_proxy=mult.get(product),tick_proxy=ticks.get(product),
                    valid_from='',valid_to='',open_fee='',close_today_fee='',close_yesterday_fee='',margin_rate='',limit_up='',limit_down='',
                    status='UNVERIFIED_STATIC_PROXY',proxy_source='crates/n-core/src/precision.rs',
                    snapshot_fee_available=product in fee_snapshot.get('products',{})))
                issue(name,c,'','','historical_metadata_missing','no dated true-contract fee/margin/price-limit/session records; exclude entire contract from independent evidence')
                if c not in mapping_contracts:issue(name,c,'','','unmapped_contract','not in local mapping snapshot')
                rows=db.execute('select start_ns,end_ns,open,high,low,close,volume,constituent_count,complete from bars where contract=? and period_seconds=300 order by start_ns',(c,))
                raw=iter(db.execute('select start_ns,open,high,low,close,volume from bars where contract=? and period_seconds=60 order by start_ns',(c,)))
                minute=next(raw,None);previous=None;count=0;first=None;last=None;months=set()
                for t,end,op,hi,lo,cl,vol,n,complete in rows:
                    count+=1;first=t if first is None else first;last=end
                    time=local(t);months.add(time.strftime('%Y-%m'))
                    if previous is not None and t<previous:issue(name,c,t,end,'overlap_or_nonmonotonic','previous end='+str(previous))
                    if previous is not None and t>previous:
                        # Gap endpoints are exact; missing vs holiday needs dated session validation.
                        cursor=local(previous);finish=local(t);missing=0;unknown=False
                        while cursor<finish:
                            try:missing+=bool(calendar.expected(cursor,symbol))
                            except (ValueError,KeyError):unknown=True;break
                            cursor+=dt.timedelta(minutes=5)
                        if unknown or missing:issue(name,c,previous,t,'calendar_unknown_gap' if unknown else 'expected_session_gap_proxy',f'proxy missing 5m slots={missing}; inactive contracts may be illiquid; never auto-fill')
                    previous=end
                    if not all(math.isfinite(x) for x in (op,hi,lo,cl,vol)) or not 0<lo<=min(op,cl)<=max(op,cl)<=hi or vol<0:
                        issue(name,c,t,end,'invalid_ohlcv','invalid range/nonfinite/negative volume')
                    if end-t!=300*NS or n!=5 or complete!=1 or t%(300*NS):issue(name,c,t,end,'incomplete_5m',f'count={n}; complete={complete}; duration={end-t}')
                    try:
                        if not calendar.expected(time,symbol):issue(name,c,t,end,'outside_session_proxy',stamp(time))
                        calendar.day(time)
                    except (ValueError,KeyError) as exc:issue(name,c,t,end,'calendar_outside_coverage',str(exc))
                    group=[]
                    while minute is not None and minute[0]<t:minute=next(raw,None)
                    while minute is not None and minute[0]<end:
                        group.append(minute);minute=next(raw,None)
                    if [g[0] for g in group]!=list(range(t,end,60*NS)):
                        issue(name,c,t,end,'missing_1m_constituents',f'actual={len(group)}')
                    elif group:
                        expected=(group[0][1],max(g[2] for g in group),min(g[3] for g in group),group[-1][4],sum(g[5] for g in group))
                        if any(not math.isclose(a,b,rel_tol=1e-10,abs_tol=1e-8) for a,b in zip(expected,(op,hi,lo,cl,vol))):issue(name,c,t,end,'aggregation_mismatch',str(expected))
                summary['contracts'].append(dict(contract=c,rows=count,start_ns=first,end_ns=last,months=sorted(months)))
            db.close()
            if digest(path)!=inputs[name]:issue(name,'*','','','source_changed_during_audit','rerun required')
            summaries.append(summary)
        for p,legacy in [('BU',1),('SH',1),('JD',10)]:
            expected=ticks.get(p) if p!='JD' else mult.get(p)
            if expected!=legacy:issue('local_specs',p,'','','spec_conflict',f'precision={expected}, legacy={legacy}; requires authoritative dated verification')
    with (out/'contract_specs.csv').open('w',encoding='utf8',newline='') as f:
        fields=list(specs[0]) if specs else ['contract','status']
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(specs)
    inputs.update({str(p.relative_to(ROOT)):digest(p) for p in [cal_path,fee_path,ROOT/'data/research_market_main_mapping.json']})
    code_paths=[ROOT/'scripts/s0_engine.py',Path(__file__),ROOT/'tests/test_no_lookahead.py',ROOT/'scripts/factor_study_data.py',ROOT/'crates/n-core/src/precision.rs']
    manifest=dict(run_id='s0-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ'),created_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
        research_version='1.0-S0',git_sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        code_sha256={str(p.relative_to(ROOT)):digest(p) for p in code_paths},inputs_sha256=inputs,
        calendar_coverage=[min(calendar.days),max(calendar.days)],data=summaries,issues=dict(issue_counts),
        s0_passed=False,reason='historical metadata/publication timing/universe unresolved; all archive bars diagnostic only',
        full_month_window=['2023-11-01','2026-11-01'],seed=None,strategies_evaluated=0)
    json_write(out/'manifest.json',manifest)
    print(json.dumps(dict(issues=manifest['issues'],s0_passed=False),ensure_ascii=False),flush=True)


if __name__=='__main__':main()
