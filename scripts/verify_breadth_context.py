"""Independent Python reconstruction of completed15m OHLC and trailing ATR14.

Validate exported Rust contexts against past/current raw bars, including midnight.
"""
import argparse
import collections
import datetime as dt
import json
import math
import pathlib

from report_signal_ablation import jsonl, read_json, write_new
from validate_execution_research import digest


def run(input_path,batch,output):
    input_path,batch,output=map(pathlib.Path,(input_path,batch,output))
    source=read_json(input_path);contexts={}
    for symbol,raw in source['symbols'].items():
        groups=collections.defaultdict(list)
        for b in raw['bars']:
            t=dt.datetime.fromisoformat(b['datetime'])
            bucket=t.replace(minute=0,second=0)+dt.timedelta(minutes=math.ceil(t.minute/15)*15)
            groups[str(bucket)].append(b)
        bars=[];true_ranges=[]
        for ts,items in sorted(groups.items()):
            if len(items)==3 and items[-1]['datetime']==ts:
                bars.append(dict(as_of=ts,open=items[0]['open'],high=max(x['high'] for x in items),
                    low=min(x['low'] for x in items),close=items[-1]['close'],volume=sum(x['volume'] for x in items)))
        for i,b in enumerate(bars):
            tr=b['high']-b['low']
            if i:tr=max(tr,abs(b['high']-bars[i-1]['close']),abs(b['low']-bars[i-1]['close']))
            true_ranges.append(tr)
            b['atr14']=sum(true_ranges[-14:])/14 if len(true_ranges)>=14 else None
        contexts[symbol]={b['as_of']:b for b in bars}
    checked=0;max_delta=0.;exact_warning=0
    for r in jsonl(batch/'evaluations.jsonl'):
        if r['scenario']!='net_s1/slip1':continue
        c=r['warning_context']
        if c is None:raise ValueError('context missing')
        if c['as_of']>r['warning_ts']:raise ValueError('future context')
        exact_warning+=c['as_of']==r['warning_ts']
        b=contexts[r['symbol']][c['as_of']]
        for key,value in b.items():
            if key=='as_of' or value is None:
                if c[key]!=value:raise ValueError('context mismatch '+key)
            else:
                delta=abs(c[key]-value);max_delta=max(delta,max_delta)
                if delta>1e-8:raise ValueError('context numeric mismatch '+key+' '+r['event_key'])
        checked+=1
    result=dict(checked_warnings=checked,exact_completed_warning_buckets=exact_warning,
        max_numeric_delta=max_delta,no_context_after_warning=True,midnight_end_labels_preserved=True,
        input_sha256=digest(input_path),batch_sha256=digest(batch/'evaluations.jsonl'),verifier_sha256=digest(pathlib.Path(__file__)))
    write_new(output,result);print(json.dumps(result))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('input','batch','output'):p.add_argument(name)
    a=p.parse_args();run(a.input,a.batch,a.output)
