"""Score association under identical one-lot exits, not a sizing optimizer."""
import argparse
import collections
import pathlib
import random

from report_signal_ablation import jsonl, read_json, stats, write_new
from validate_execution_research import capacity, digest, evaluation


def band(score):
    return "2.5-3" if score < 3 else "3-3.5" if score < 3.5 else "3.5-4" if score < 4 else "4+"


def high_low(rows):
    low,high=[stats([r for r in rows if (r["score"]>=3.5)==v]) for v in (False,True)]
    return dict(low=low,high=high,delta_win_rate=high["win_rate"]-low["win_rate"] if low["n"] and high["n"] else None,
                delta_mean_r=high["mean_net_r"]-low["mean_net_r"] if low["n"] and high["n"] else None)


def block_differences(rows,repetitions=2000):
    bins=collections.defaultdict(lambda:[0.,0,0.,0,0,0])
    for r in rows:
        e=evaluation(r)
        if e["status"]!="closed":
            continue
        index=0 if r["score"]<3.5 else 1
        d=bins[r["warning_ts"][:10]]
        d[index*2]+=e["net_r"]
        d[index*2+1]+=1
        d[4+index]+=e["net_r"]>0
    dates=sorted(bins)
    if len(dates)<10:
        return None
    values=[bins[d] for d in dates]
    rng=random.Random(20261022)
    differences,win=[] ,[]
    for _ in range(repetitions):
        sampled=[]
        while len(sampled)<len(values):
            i=rng.randrange(len(values)-4)
            sampled.extend(values[i:i+5])
        totals=[sum(x[j] for x in sampled[:len(values)]) for j in range(6)]
        if totals[1] and totals[3]:
            differences.append(totals[2]/totals[3]-totals[0]/totals[1])
            win.append(totals[5]/totals[3]-totals[4]/totals[1])
    def interval(values):
        values=sorted(values)
        return [values[int(.025*len(values))],values[int(.975*len(values))]]
    return dict(delta_mean_r_interval95=interval(differences),delta_win_rate_interval95=interval(win),
                method="5 warning-calendar-date joint blocks, fixed threshold3.5, diagnostic only",repetitions=repetitions)


def run(input_path,batch,output):
    input_path,batch,output=map(pathlib.Path,(input_path,batch,output))
    if output.exists():
        raise FileExistsError("frozen output must be new")
    source,cost=read_json(input_path),read_json(batch/"summary.json")
    if digest(input_path)!=cost["input_sha256"]:
        raise ValueError("source differs")
    cases=collections.defaultdict(list)
    for r in jsonl(batch/"evaluations.jsonl"):
        if r["scenario"].split("/")[0] in ("original_s1","fixed_one_r") and "/nf5/" in r["scenario"] and not r["scenario"].endswith("fee_only"):
            if evaluation(r)["volume"]!=1:
                raise ValueError("score comparison must hold lots fixed at one")
            cases[r["scenario"]].append(r)
    result=dict(scenarios={},threshold=3.5,source="historical_development",qualified_for_sizing=False,
        hashes={str(p):digest(p) for p in (input_path,batch/"evaluations.jsonl",pathlib.Path(__file__))})
    for name,rows in cases.items():
        bins=collections.defaultdict(list)
        months=collections.defaultdict(list)
        for r in rows:
            bins[band(r["score"])].append(r)
            months[r["warning_ts"][:7]].append(r)
        reservation=capacity(rows,source["symbols"],"entry",1)
        accepted={r["event_key"] for r in reservation["ledger"] if r["accepted"]}
        nonoverlap=[r for r in rows if r["event_key"] in accepted]
        result["scenarios"][name]=dict(bands={k:stats(v) for k,v in sorted(bins.items())},
            threshold_comparison=high_low(rows),block_differences=block_differences(rows),
            months={k:high_low(v) for k,v in sorted(months.items())},
            nonoverlap_threshold_comparison=high_low(nonoverlap),
            nonoverlap_block_differences=block_differences(nonoverlap))
    output.mkdir()
    write_new(output/"summary.json",result)
    for name,s in result["scenarios"].items():
        if not name.endswith("slip1_fee"):
            continue
        print(name)
        print('bands',{k:{x:v[x] for x in ('n','win_rate','mean_net_r','net_amount')} for k,v in s['bands'].items()})
        print('high_minus_low',s['threshold_comparison']['delta_win_rate'],s['threshold_comparison']['delta_mean_r'])
        print('block_differences',s['block_differences'])
        print('nonoverlap_high_minus_low',s['nonoverlap_threshold_comparison']['delta_win_rate'],s['nonoverlap_threshold_comparison']['delta_mean_r'])
        print('nonoverlap_interval',s['nonoverlap_block_differences'])


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("input","batch","output"):
        parser.add_argument(name)
    args=parser.parse_args()
    run(args.input,args.batch,args.output)
