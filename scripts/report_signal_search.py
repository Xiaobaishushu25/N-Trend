"""Multiobjective diagnostics for finite, disclosed mechanism experiments.

No account cap, no automatic winner promotion, no claim of new independent data.
"""
import argparse
import collections
import json
import math
import pathlib
import statistics

from report_signal_ablation import jsonl, paired, read_json, write_new
from report_signal_optimization import summarize
from reprice_signal_costs import TradingDates, reprice
from validate_execution_research import digest, evaluation


def distribution(rows):
    rows=[r for r in rows if evaluation(r)["status"]=="closed"]
    rs=sorted(evaluation(r)["net_r"] for r in rows)
    amounts=sorted(evaluation(r)["net_amount"] for r in rows)
    def quantile(v,p):
        return v[int((len(v)-1)*p)] if v else None
    tail=max(1,math.ceil(len(rows)*.1))
    losses=run=0
    equity=peak=drawdown=0.
    for r in sorted(rows,key=lambda r:(evaluation(r)["exit_ts"],r["event_key"])):
        run=run+1 if evaluation(r)["net_amount"]<0 else 0
        losses=max(losses,run)
        equity+=evaluation(r)["net_amount"]
        peak=max(peak,equity)
        drawdown=max(drawdown,peak-equity)
    return dict(n=len(rows),median_net_r=statistics.median(rs) if rs else None,
        r_p05=quantile(rs,.05),r_p95=quantile(rs,.95),worst_r=min(rs) if rs else None,
        worst_amount=min(amounts) if amounts else None,worst10pct_mean_r=sum(rs[:tail])/tail if rs else None,
        worst10pct_mean_amount=sum(amounts[:tail])/tail if amounts else None,
        net_amount_without_top5=sum(amounts)-sum(sorted([x for x in amounts if x>0],reverse=True)[:5]),
        longest_realized_loss_run=losses,realized_exit_order_drawdown_amount=drawdown,
        quantile_method="empirical order statistic floor(p*(n-1)); no confidence bound")


def frontier(summaries):
    candidates={k:v for k,v in summaries.items() if k.endswith("slip1") and v["one_symbol_slot"]["accepted"]["n"]>=100}
    def axes(v):
        s=v["one_symbol_slot"]["accepted"]
        return [s["win_rate"],s["mean_net_r"],s["net_amount"],
                v["distribution"]["one_symbol_slot"]["worst10pct_mean_r"],-s["realized_exit_order_drawdown_r"]]
    keep=[]
    for name,s in candidates.items():
        a=axes(s)
        if not any(all(y>=x for x,y in zip(a,axes(v))) and any(y>x for x,y in zip(a,axes(v)))
                   for k,v in candidates.items() if k!=name):
            keep.append(name)
    return dict(names=sorted(keep),min_priced_n=100,dimensions=["win_rate","mean_net_r","net_amount","worst10pct_mean_r","negative_realized_R_drawdown"],
                note="Retrospective non-dominance, unadjusted for multiple comparisons, not validation or sizing approval")


def run(input_path,batch,profile_path,reference_path,output):
    input_path,batch,profile_path,reference_path,output=map(pathlib.Path,(input_path,batch,profile_path,reference_path,output))
    if output.exists():
        raise FileExistsError("immutable output already exists")
    source,manifest,profile=read_json(input_path),read_json(batch/"manifest.json"),read_json(profile_path)
    if digest(input_path)!=manifest["input_sha256"] or digest(profile_path)!=manifest["fees_sha256"]:
        raise ValueError("frozen source mismatch")
    calendar=TradingDates(source["symbols"])
    cases=collections.defaultdict(dict)
    for row in jsonl(batch/"evaluations.jsonl"):
        row=reprice(row,profile,calendar)
        if row["event_key"] in cases[row["scenario"]]:
            raise ValueError("duplicate opportunity")
        cases[row["scenario"]][row["event_key"]]=row
    universe=set(next(iter(cases.values())))
    if len(cases)!=manifest["case_count"] or any(set(v)!=universe for v in cases.values()):
        raise ValueError("candidate universe is inconsistent")
    result=dict(manifest=manifest,cohort=len(universe),scenarios={},paired_stress={},paired_against_net_s1={},
        risk10_ablation={},source="historical_development_exploration",account_cap=None,
        qualified_for_trading=False,new_independent_observations=0,production_changed=False,
        source_hashes={str(p):digest(p) for p in (input_path,batch/"evaluations.jsonl",profile_path,pathlib.Path(__file__),
            pathlib.Path(__file__).with_name("report_signal_optimization.py"),pathlib.Path(__file__).with_name("reprice_signal_costs.py"))})
    # Replay controls must reproduce the previously frozen restricted universe.
    mapping={"touch_s1_risk10":"s1_baseline","net_s1":"s1_net_rr1","net_s1_no_follow_off":"s1_net_rr1_no_follow_off"}
    n=0;max_delta=0.
    for ref in jsonl(reference_path/"evaluations.jsonl"):
        old,slip=ref["scenario"].split("/")
        new=next((a for a,b in mapping.items() if b==old),None)
        if new is None:
            continue
        actual=evaluation(cases[new+"/"+slip][ref["event_key"]]);expected=evaluation(ref)
        for key,value in expected.items():
            current=actual[key]
            if isinstance(value,(float,int)) and isinstance(current,(float,int)):
                max_delta=max(max_delta,abs(value-current))
                if abs(value-current)>1e-6:raise ValueError("control numeric regression "+key)
            elif current!=value:raise ValueError("control semantic regression "+key)
        n+=1
    result["control_regression"]=dict(rows=n,max_numeric_delta=max_delta)
    output.mkdir()
    accepted_maps={}
    with (output/"evaluations.jsonl").open("x",encoding="utf-8") as f:
        for name,mapping in sorted(cases.items()):
            rows=list(mapping.values())
            summary,keys=summarize(rows,source["symbols"])
            selected=[r for r in rows if r["event_key"] in keys]
            summary["distribution"]=dict(independent=distribution(rows),one_symbol_slot=distribution(selected))
            summary["month_stability"]=dict(
                positive_cash=sum(v["net_amount"]>0 for v in summary["groups"]["month"]["one_symbol_slot"].values()),
                positive_r=sum(v["net_r"]>0 for v in summary["groups"]["month"]["one_symbol_slot"].values()))
            result["scenarios"][name]=summary
            accepted_maps[name]={r["event_key"]:r for r in selected}
            for row in rows:
                row["accepted_one_symbol_slot"]=row["event_key"] in keys
                f.write(json.dumps(row,ensure_ascii=False)+"\n")
    for definition in manifest["definitions"]:
        name=definition["name"]
        result["paired_stress"][name]=dict(independent=paired(cases[name+"/slip1"],cases[name+"/slip2"]),
            one_symbol_slot=paired(accepted_maps[name+"/slip1"],accepted_maps[name+"/slip2"]))
        result["paired_against_net_s1"][name]={str(slip):paired(accepted_maps["net_s1/slip"+str(slip)],accepted_maps[name+"/slip"+str(slip)]) for slip in (1,2)}
    for guard in (False,True):
        root="net_s1" if guard else "touch_s1"
        ten="net_s1" if guard else "touch_s1_risk10"
        for slip in (1,2):
            a,b=root+"_risk0/slip"+str(slip),ten+"/slip"+str(slip)
            result["risk10_ablation"][root+"/slip"+str(slip)]=dict(
                no_risk_filter=result["scenarios"][a]["one_symbol_slot"]["accepted"],
                risk10=result["scenarios"][b]["one_symbol_slot"]["accepted"],
                common_closed=paired(cases[a],cases[b]))
    result["retrospective_pareto_frontier"]=frontier(result["scenarios"])
    result["development_flags"]={}
    for definition in manifest["definitions"]:
        name=definition["name"]
        a,b=[result["scenarios"][name+"/slip"+str(i)] for i in (1,2)]
        stress=b["one_symbol_slot"]["accepted"]
        result["development_flags"][name]=dict(
            primary_and_stress_cash_and_r_positive=all(s["one_symbol_slot"]["accepted"]["net_amount"]>0 and (s["one_symbol_slot"]["accepted"]["mean_net_r"] or 0)>0 for s in (a,b)),
            stress_without_top5_cash_and_r_positive=stress["net_r_without_top5"]>0 and b["distribution"]["one_symbol_slot"]["net_amount_without_top5"]>0,
            all_warning_months_cash_positive=a["month_stability"]["positive_cash"]==4,
            at_least200_priced_primary=a["one_symbol_slot"]["accepted"]["n"]>=200,
            all_account_exposures_priced=a["one_symbol_slot"]["accepted_unknown_count"]==0,
            independent_live_validation=False)
    write_new(output/"summary.json",result)
    lines=["# 扩展优化：入场、止盈、早退及信号环境", "",
        f"全部历史开发探索，无资金上限、一手。{len(manifest['definitions'])}个候选×3档滑点；后续阶段在前一阶段结果后决定。无新独立样本，不自动晋升最优候选。",
        "保留10tick对照、0tick消融，净空间预估始终固定1跳；确认后入场用下一5m开盘，回踩不允许同5m触发并成交。", "",
        "|候选|1跳平仓数|1跳胜率|1跳净额|2跳净额|1跳平均R|2跳平均R|2跳去头5R|1跳亏损尾部均R|正现金月份/4|未知数|",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for definition in manifest["definitions"]:
        name=definition["name"]
        a,b=[result["scenarios"][name+"/slip"+str(i)] for i in (1,2)]
        x,y=a["one_symbol_slot"]["accepted"],b["one_symbol_slot"]["accepted"]
        fmt=lambda v,p:format(v,p) if v is not None else "—"
        lines.append(f'|{name}|{x["n"]}|{fmt(x["win_rate"],".2%")}|{x["net_amount"]:.2f}|{y["net_amount"]:.2f}|{fmt(x["mean_net_r"],".4f")}|{fmt(y["mean_net_r"],".4f")}|{y["net_r_without_top5"]:.2f}|{fmt(a["distribution"]["one_symbol_slot"]["worst10pct_mean_r"],".3f")}|{a["month_stability"]["positive_cash"]}|{a["one_symbol_slot"]["accepted_unknown_count"]}|')
    lines.extend(["", "数值为同品种一槽的可定价部分，另存独立信号。未知敞口不当0收益；非完整账户收益。",
        "收盘确认、回踩等机制会改变入场机会集合和歧义数量；配对只统计双方都可确定的交集，不能当作全部机会因果结论。",
        "尾部指标为最差10%交易平均R；回撤为已实现退出顺序曲线，未计完整浮盈亏。",
        "所有比较已看过历史、多重尝试；Pareto前沿仅是历史多目标不被支配集合，不是可交易通过名单。",
        "费用快照、历史合约映射、成交路径和队列仍有限制；没有自动调整生产仓位或上线新规则。"])
    (output/"扩展优化报告.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    print("cohort",len(universe),"cases",len(cases),"control",result["control_regression"])
    for definition in manifest["definitions"]:
        name=definition["name"]
        a,b=[result["scenarios"][name+"/slip"+str(i)]["one_symbol_slot"]["accepted"] for i in (1,2)]
        print(name,a["n"],round(a["win_rate"] or 0,4),round(a["net_amount"],2),round(b["net_amount"],2),round(a["mean_net_r"] or 0,4),round(b["mean_net_r"] or 0,4),round(b["net_r_without_top5"],2))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("input","batch","profile","reference","output"):
        parser.add_argument(name)
    args=parser.parse_args()
    run(args.input,args.batch,args.profile,args.reference,args.output)
