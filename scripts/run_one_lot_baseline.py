"""Freeze and run a one-lot, one-symbol-slot, 50k diagnostic baseline.

The score never changes size or exit. No parameter selection or production writes.
Unpriceable accepted paths prohibit a final-account equity claim.
"""
import argparse
import collections
import datetime as dt
import json
import pathlib

from report_signal_ablation import jsonl, paired, read_json, stats, write_new
from validate_capital_capacity import margin_profile, simulate
from validate_execution_research import digest, evaluation
from validate_score_sizing import band, block_differences, high_low


def freeze_protocol(input_path, batch, fee_dir):
    now=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")
    return dict(id="one-lot-baseline-20261022-v1", frozen_at_shanghai=now,
        historical_data_role="already-used development diagnostics; not holdout or prospective",
        forward_observation_after=now, initial_capital_yuan=50000, exchange_margin_multiplier=1.10,
        policies=["original_s1","fixed_one_r"], quantity_per_signal=1,
        min_score=2.5,min_planned_risk_ticks=10,no_follow_15m_bucket=5,
        no_follow_mfe_r=.5,max_pending_15m_buckets=12,max_holding_15m_buckets=60,
        max_active_opportunities_per_symbol=1,capacity_begins="entry; unknown paths conservatively reserve from warning",
        priority="earlier warning then event key; score never changes priority, size or exit",
        primary_adverse_slippage_ticks_per_side=1,stress_adverse_slippage_ticks_per_side=2,
        diagnostic_adverse_slippage_ticks_per_side=0,score_observation_bands=[2.5,3,3.5,4],
        no_parameter_search=True,no_model_retraining=True,
        simultaneous_bar_orderings=["entry_first","exit_first_existing_positions"],
        funding_modes=["dynamic_known_component","static_50k_capacity_reference"],
        unknown_policy="reserve margin, keep P&L unknown, final equity null, flag every later funding decision conditional",
        source_hashes={str(p):digest(p) for p in [input_path,batch/"summary.json",batch/"evaluations.jsonl",
            pathlib.Path(__file__),pathlib.Path(__file__).with_name("validate_capital_capacity.py"),
            pathlib.Path(__file__).with_name("validate_execution_research.py"),
            pathlib.Path(__file__).with_name("validate_score_sizing.py"),
            pathlib.Path(__file__).with_name("report_signal_ablation.py")]},
        public_margin_snapshot_hashes={str(p):digest(p) for p in sorted(fee_dir.glob("*-mirror.html"))},
        prospective_readiness_requirements=[
            "Actually integrate and start an authenticated live observer; a frozen config alone is not collection.",
            "Only newly observed warnings after protocol freeze; no historical backfill or known outcomes.",
            "Capture actual contract, quote/order/fill timestamps, bid/ask, fees and trading-day margin.",
            "At least 30 trading days and 200 nonoverlapping opportunity groups for first review; counts do not guarantee success.",
            "Resolve unpriced exposures before reporting complete account equity; inspect costs, pressure, concentration and drawdown.",
            "Revisions create a new protocol and cohort; do not overwrite this version or pick new score thresholds from this cohort."])


def run(input_path,batch,fee_dir,output):
    input_path,batch,fee_dir,output=map(pathlib.Path,(input_path,batch,fee_dir,output))
    if output.exists():
        raise FileExistsError("immutable batch must be new")
    source,cost=read_json(input_path),read_json(batch/"summary.json")
    if digest(input_path)!=cost["input_sha256"]:
        raise ValueError("input differs from frozen repricing source")
    margins=margin_profile(fee_dir)
    signals_path=input_path.parent/"final-20261022-v3"/"signals.json"
    signals=read_json(signals_path)
    def key(e):
        return "|".join(e[k] for k in ("symbol","direction","s0_ts","s1_ts","s2_ts","warning_ts"))
    frozen={key(e):e for e in signals}
    protocol=freeze_protocol(input_path,batch,fee_dir)
    protocol["source_hashes"][str(signals_path)]=digest(signals_path)
    output.mkdir()
    # Persist rules before any result aggregation. No silent overwrites.
    write_new(output/"protocol.json",protocol)
    cases=collections.defaultdict(dict)
    for row in jsonl(batch/"evaluations.jsonl"):
        policy,timing,cost_name=row["scenario"].split("/")
        if policy not in protocol["policies"] or timing!="nf5" or cost_name=="fee_only":
            continue
        if evaluation(row)["volume"]!=1:
            raise ValueError("one-lot invariant failed")
        if row["event_key"] in cases[row["scenario"]]:
            raise ValueError("duplicate frozen opportunity")
        signal=frozen[row["event_key"]]
        row["planned_entry"],row["planned_risk"]=signal["entry"],signal["risk"]
        cases[row["scenario"]][row["event_key"]]=row
    universe=set(next(iter(cases.values())))
    if len(cases)!=6 or len(universe)!=1352 or any(set(m)!=universe for m in cases.values()):
        raise ValueError("expected six cases over the same 1352 opportunities")
    result=dict(protocol=protocol,source="historical_development",eligible_opportunities=len(universe),
                scenarios={},same_accepted_pairs={},qualified_for_trading=False,
                prospective_observer_running=False,new_prospective_observations=0)
    accepted_cases=collections.defaultdict(dict)
    with (output/"admission-ledger.jsonl").open("x",encoding="utf-8") as f:
        for name,mapping in sorted(cases.items()):
            rows=list(mapping.values())
            variants={}
            for dynamic in (False,True):
                for exits_first in (False,True):
                    label=f"{'dynamic_known' if dynamic else 'static_50k'}/{'exit_first' if exits_first else 'entry_first'}"
                    simulation=simulate(rows,source["symbols"],margins,dynamic=dynamic,
                        exits_first=exits_first,slots_per_symbol=1)
                    decisions=simulation.pop("decisions")
                    accepted={d["event_key"] for d in decisions if d["accepted"]}
                    accepted_rows=[r for r in rows if r["event_key"] in accepted]
                    priced={r["event_key"]:r for r in accepted_rows if evaluation(r)["status"]=="closed"}
                    accepted_cases[label][name]=priced
                    first_unknown=(simulation["first_unknown_acceptance"] or {}).get("ts")
                    for d in decisions:
                        d["conditional_on_unpriced_pnl"]=bool(dynamic and first_unknown is not None and d["ts"]>first_unknown)
                        f.write(json.dumps(dict(scenario=name,portfolio=label,**d),ensure_ascii=False)+"\n")
                    months=collections.defaultdict(list)
                    scores=collections.defaultdict(list)
                    for r in accepted_rows:
                        months[r["warning_ts"][:7]].append(r)
                        scores[band(r["score"])].append(r)
                    simulation["accepted_by_warning_month"]={k:stats(v) for k,v in sorted(months.items())}
                    simulation["score_observation_only"]={k:stats(v) for k,v in sorted(scores.items())}
                    simulation["high_low_score_diagnostic"]=high_low(accepted_rows)
                    simulation["high_low_score_block_difference"]=block_differences(accepted_rows)
                    simulation["accepted_keys"]=sorted(accepted)
                    simulation["accepted_priced_risk_yuan"]={
                        "max":max((evaluation(r)["risk"]*r["multiplier"] for r in priced.values()),default=None),
                        "mean":sum(evaluation(r)["risk"]*r["multiplier"] for r in priced.values())/len(priced) if priced else None}
                    variants[label]=simulation
            result["scenarios"][name]=dict(independent_signal_reference=stats(rows),portfolios=variants)
    for label,names in accepted_cases.items():
        comparisons={}
        for cost_name in ("zero","slip1_fee","slip2_fee"):
            a,b=f"original_s1/nf5/{cost_name}",f"fixed_one_r/nf5/{cost_name}"
            comparisons[cost_name]=paired(names[a],names[b])
        result["same_accepted_pairs"][label]=comparisons
    # A complete portfolio cannot pass merely because its priced subset wins.
    result["readiness"]=dict(independent_new_samples=False,minimum_sample_gate=False,
        complete_account_paths=all(v["accepted_unknown_count"]==0 for s in result["scenarios"].values() for v in s["portfolios"].values()),
        score_sizing_enabled=False,production_changed=False,
        next_required_evidence="New live-observed nonoverlap opportunities and complete execution/fee paths; not more threshold tuning.")
    unresolved=[]
    for name,s in sorted(result["scenarios"].items()):
        if not name.endswith("slip1_fee"):
            continue
        needed={k for v in s["portfolios"].values() for k in v["accepted_unknown_keys"]}
        for key in sorted(needed):
            row=cases[name][key]
            e=evaluation(row)
            symbol=source["symbols"][row["symbol"]]
            when=e["exit_ts"] or symbol["bars"][-1]["datetime"]
            context=[b for b in symbol["bars"] if row["warning_ts"]<=b["datetime"]<=when][-8:]
            requirement=("Finer timestamped prices/quotes at the disagreeing 5m bar" if e["status"]=="ambiguous"
                else "Actual old/new contract mapping and both price series through rollover" if e["status"]=="rollover"
                else "Missing raw bars through the unfilled/open observation window" if e["status"]=="missing_data"
                else "Later raw observations through exit or maximum holding window")
            unresolved.append(dict(scenario=name,event_key=key,symbol=row["symbol"],warning_ts=row["warning_ts"],
                status=e["status"],entry_ts=e["entry_ts"],uncertainty_ts=when,
                planned_entry=frozen[key]["entry"],planned_stop=frozen[key]["stop"],planned_target=frozen[key]["target"],
                known_fills=row["result"]["fills"],path_disagreement=row["result"]["disagreement"],
                last_raw_context=context,required_evidence=requirement,
                rollovers=[r for r in symbol["rollovers"] if row["warning_ts"]<=r["ts"]<=when]))
    with (output/"unresolved-exposures.jsonl").open("x",encoding="utf-8") as f:
        for row in unresolved:
            f.write(json.dumps(row,ensure_ascii=False)+"\n")
    result["unresolved_primary_exposures"]=dict(rows=len(unresolved),
        by_status=dict(collections.Counter(r["status"] for r in unresolved)),
        unique_opportunities=len({r["event_key"] for r in unresolved}))
    write_new(output/"summary.json",result)
    lines=["# 下一轮：统一一手与五万元联合验证", "",
        "历史开发诊断；没有独立新样本，未部署或启动前瞻采集。协议已冻结，评分只作观察。",
        "规则：每笔1手；S1/1R并行；同品种入场起最多1个机会；5万元；交易所保证金×1.10；无跟随第5根；1跳主情景、2跳压力、0跳诊断。", "",
        "## 联合组合结果（动态已知盈亏组件）", "",
        "下表净额仅包括可定价平仓组件；未知路径仍占保证金，其盈亏未知。存在未知机会时账户最终权益为null，不能把组件净额当成完整账户收益。", "",
        "|策略|滑点/侧|同根顺序|可定价平仓|净胜率|可定价净额|平均净R|槽位拦截|资金拦截|未知接纳|",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|"]
    for name,s in sorted(result["scenarios"].items()):
        for label,v in s["portfolios"].items():
            if not label.startswith("dynamic_known"):
                continue
            stat=v["accepted_priced"]
            slip=0 if name.endswith("zero") else 1 if name.endswith("slip1_fee") else 2
            lines.append(f'|{name.split("/")[0]}|{slip}|{label.split("/")[1]}|{stat["n"]}|{stat["win_rate"]:.2%}|{stat["net_amount"]:.2f}|{stat["mean_net_r"]:.4f}|{v["rejected_by_symbol_capacity"]}|{v["rejected_capital"]}|{v["accepted_unknown_count"]}|')
    lines.extend(["", "## 样本与限制", "",
        "候选1352个。槽位与资金在同一事件循环接纳，资金不足不会留下占槽位的幽灵仓位。评分不排序、不筛出高分或翻倍手数。",
        "未知路径保守从预警占位，且不填0收益。未知接纳后的动态资金决策单列条件标记。当前结果因此不能通过完整账户验证。",
        "现有费用、保证金均为公开快照；连续合约和逐日参数仍未完全复原。未模拟队列、挂单冻结、资金不足后重试或强平；5分钟同根顺序用两种固定情景核验。",
        "协议文件包含后续新样本起点、来源与脚本指纹。已有7—10月数据不得改称留出集。前瞻首次评审至少30交易日、200非重叠机会组，数量并不保证通过。",
        "前瞻观察器尚未部署或运行，当前新增前瞻样本为0。本次没有模型重训、生产参数变更或后台持续任务。",
        "未定价机会按缺数据、换月、同根路径歧义及未结束分类，附原始K线、已知成交与所需证据，见unresolved-exposures.jsonl。"])
    (output/"单手联合验证报告.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    print(json.dumps({name:{label:{k:v[k] for k in ("accepted_unknown_count","rejected_capital","rejected_by_symbol_capacity","total_account_final_equity")} |
            dict(priced_n=v["accepted_priced"]["n"],priced_net=v["accepted_priced"]["net_amount"]) for label,v in s["portfolios"].items() if label.startswith("dynamic_known")}
            for name,s in result["scenarios"].items()},indent=2))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("input","batch","fee_dir","output"):
        parser.add_argument(name)
    args=parser.parse_args()
    run(args.input,args.batch,args.fee_dir,args.output)
