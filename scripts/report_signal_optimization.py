"""Price all predefined optimization candidates; no 50k admission limit.

Development diagnostics, not a parameter winner selected for production.
"""
import argparse
import collections
import json
import pathlib

from report_signal_ablation import block_interval, jsonl, paired, read_json, stats, write_new
from reprice_signal_costs import TradingDates, reprice
from validate_execution_research import capacity, digest, evaluation


def summarize(rows,symbols):
    s=stats(rows)
    s["block_interval"]=block_interval(rows)
    s["unknown_candidates"]=sum(evaluation(r)["status"] in ("ambiguous","missing_data","rollover","unfinished","pending") for r in rows)
    s["gross_after_slippage"]=sum(r["cost_repricing"]["gross_amount_after_slippage"] for r in rows if evaluation(r)["status"]=="closed")
    s["commission"]=sum(r["cost_repricing"]["commission"] for r in rows if evaluation(r)["status"]=="closed")
    diagnostic=capacity(rows,symbols,"entry",1)
    accepted={d["event_key"] for d in diagnostic.pop("ledger") if d["accepted"]}
    accepted_rows=[r for r in rows if r["event_key"] in accepted]
    diagnostic["accepted_block_interval"]=block_interval(accepted_rows)
    diagnostic["accepted_unknown_count"]=sum(evaluation(r)["status"] in ("ambiguous","missing_data","rollover","unfinished","pending") for r in accepted_rows)
    groups={}
    for kind,key in (("month",lambda r:r["warning_ts"][:7]),("symbol",lambda r:r["symbol"]),
                     ("direction",lambda r:r["direction"]),
                     ("score",lambda r:"below3.5" if r["score"]<3.5 else "3.5+")):
        bins=collections.defaultdict(list)
        abins=collections.defaultdict(list)
        for r in rows:
            bins[key(r)].append(r)
            if r["event_key"] in accepted:
                abins[key(r)].append(r)
        groups[kind]=dict(independent={k:stats(v) for k,v in sorted(bins.items())},
                          one_symbol_slot={k:stats(v) for k,v in sorted(abins.items())})
    # Sensitivity to reliance on a single symbol; no symbol whitelist is selected.
    slot_leave_one_out={symbol:stats([r for r in accepted_rows if r["symbol"]!=symbol])
                        for symbol in sorted({r["symbol"] for r in rows})}
    return dict(independent=s,one_symbol_slot=diagnostic,groups=groups,
                one_slot_leave_one_symbol_out=slot_leave_one_out),accepted


def run(input_path,batch,profile_path,reference_path,output):
    input_path,batch,profile_path,reference_path,output=map(pathlib.Path,(input_path,batch,profile_path,reference_path,output))
    if output.exists():
        raise FileExistsError("immutable output must be new")
    source,manifest,profile=read_json(input_path),read_json(batch/"manifest.json"),read_json(profile_path)
    if digest(input_path)!=manifest["input_sha256"] or digest(profile_path)!=manifest["fees_sha256"]:
        raise ValueError("frozen input/fee fingerprint mismatch")
    calendar=TradingDates(source["symbols"])
    cases=collections.defaultdict(dict)
    output.mkdir()
    with (output/"evaluations.jsonl").open("x",encoding="utf-8") as f:
        for row in jsonl(batch/"evaluations.jsonl"):
            row=reprice(row,profile,calendar)
            if row["event_key"] in cases[row["scenario"]]:
                raise ValueError("duplicate event")
            cases[row["scenario"]][row["event_key"]]=row
            f.write(json.dumps(row,ensure_ascii=False)+"\n")
    if len(cases)!=15 or any(len(m)!=1352 for m in cases.values()):
        raise ValueError("predeclared candidate universe changed")
    reference={}
    for r in jsonl(reference_path/"evaluations.jsonl"):
        if r["scenario"] in ("original_s1/nf5/zero","original_s1/nf5/slip1_fee","original_s1/nf5/slip2_fee"):
            slip=0 if r["scenario"].endswith("zero") else 1 if r["scenario"].endswith("slip1_fee") else 2
            reference[(slip,r["event_key"])]=r
    max_delta=0.
    for slip in (0,1,2):
        for k,row in cases[f"s1_baseline/slip{slip}"].items():
            current,old=evaluation(row),evaluation(reference[(slip,k)])
            for field in current:
                a,b=current[field],old[field]
                if isinstance(a,(int,float)) and isinstance(b,(int,float)):
                    max_delta=max(max_delta,abs(a-b))
                    if abs(a-b)>1e-6:
                        raise ValueError("baseline numeric regression: "+field)
                elif a!=b:
                    raise ValueError("baseline semantic regression: "+field)
    result=dict(account_cap=None,manifest=manifest,source="historical_development",scenarios={},pairs={},slippage_common_closed={},
        same_accepted_pairs={},baseline_regression=dict(rows=len(reference),max_numeric_delta=max_delta),
        qualified_for_trading=False,production_changed=False,independent_validation=False,
        source_hashes={str(p):digest(p) for p in (input_path,batch/"evaluations.jsonl",profile_path,
            pathlib.Path(__file__),pathlib.Path(__file__).with_name("reprice_signal_costs.py"),
            pathlib.Path(__file__).with_name("validate_execution_research.py"))})
    accepted_by_case={}
    for name,mapping in sorted(cases.items()):
        summary,accepted=summarize(list(mapping.values()),source["symbols"])
        result["scenarios"][name]=summary
        accepted_by_case[name]={k:r for k,r in mapping.items() if k in accepted}
    for slip in (0,1,2):
        base=f"s1_baseline/slip{slip}"
        for definition in manifest["definitions"]:
            name=f'{definition["name"]}/slip{slip}'
            result["pairs"][base+" -> "+name]=paired(cases[base],cases[name])
            result["same_accepted_pairs"][base+" -> "+name]=paired(accepted_by_case[base],accepted_by_case[name])
    for definition in manifest["definitions"]:
        name=definition["name"]
        a,b=name+"/slip1",name+"/slip2"
        result["slippage_common_closed"][name]=dict(
            independent=paired(cases[a],cases[b]),one_symbol_slot=paired(accepted_by_case[a],accepted_by_case[b]))
    write_new(output/"summary.json",result)
    lines=["# 无资金上限：信号与退出候选优化", "",
        "全部一手，移除五万元及保证金约束。固定五个逻辑候选×三档滑点，不自动选择最优阈值或修改实盘。",
        "净空间门槛：在预警时与拟成交时分别检查，扣预定滑点及开仓+平今/平昨较高费用后，S1净奖励至少覆盖完整止损损失。",
        "门槛估计固定每侧1跳，不随0/2跳执行压力重选预警；实际拟成交价格变化仍会因果地触发入场复核。", "",
        "|候选|每侧滑点|独立平仓数|净胜率|独立净额|平均净R|单品种一槽净额|一槽平均净R|一槽未知数|",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for name,s in sorted(result["scenarios"].items()):
        a,b=s["independent"],s["one_symbol_slot"]["accepted"]
        lines.append(f'|{name.split("/")[0]}|{name[-1]}|{a["n"]}|{a["win_rate"]:.2%}|{a["net_amount"]:.2f}|{a["mean_net_r"]:.4f}|{b["net_amount"]:.2f}|{b["mean_net_r"]:.4f}|{s["one_symbol_slot"]["accepted_unknown_count"]}|')
    lines.extend(["", "候选含义：", "",
        "- s1_baseline：原S1和第5根无跟随对照。",
        "- s1_planned_rr1：计划S1/原止损空间至少1:1。",
        "- s1_net_rr1：净奖励/完整止损损失至少1:1，预警和拟成交双重检查。",
        "- s1_net_rr1_no_follow_off：上述门槛，关闭无跟随，其余不变。",
        "- s1_net_rr1_protect_at1r：上述门槛，到1R后止损移到入场价；仍扣费用和滑点，不能称净保本。", "",
        "## 限制", "",
        "全部是已参与开发的7—10月数据，包含多个比较；没有新独立样本，不因结果最佳而自动上线。",
        "费用使用公开快照，净空间判断保守使用平今/平昨较高费用，但实际净额仍按推断交易日计费。",
        "同品种一槽是容量诊断，先入场先占用，无法定价路径保守占用；未知盈亏保留为未知。表内净额仅为可定价部分，不是完整账户收益。",
        "OHLC两条典型路径一致仍非穷尽盘中成交证明。移到入场价的保护退出仍可能因滑点、费用或跳空亏损。",
        "计划RR与净空间过滤同时改变信号集合；配对子集、逐月、移除最大5笔盈利及逐品种排除敏感度见summary.json。"])
    (output/"无资金上限优化报告.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    print(json.dumps({k:dict(n=v["independent"]["n"],net=v["independent"]["net_amount"],mean_r=v["independent"]["mean_net_r"],
        slot_n=v["one_symbol_slot"]["accepted"]["n"],slot_net=v["one_symbol_slot"]["accepted"]["net_amount"],
        slot_mean_r=v["one_symbol_slot"]["accepted"]["mean_net_r"])
        for k,v in result["scenarios"].items() if not k.endswith("slip0")},indent=2))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("input","batch","profile","reference","output"):
        parser.add_argument(name)
    args=parser.parse_args()
    run(args.input,args.batch,args.profile,args.reference,args.output)
