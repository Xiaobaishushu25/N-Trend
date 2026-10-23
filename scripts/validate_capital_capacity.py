"""50k capital diagnostics using exchange margin * 1.10, never broker orders.

Unpriceable opportunities reserve margin rather than vanishing. Their P&L remains
unknown: final account equity must not be reported when such exposure is accepted.
"""
import argparse
import bisect
import collections
import json
import pathlib
import re

from build_exchange_fee_profile import Table
from report_signal_ablation import jsonl, read_json, stats, write_new
from validate_execution_research import digest, evaluation, worst_end


def margin_profile(directory):
    result = {}
    for exchange in ("dce", "czce", "shfe", "gfex"):
        path = directory / (exchange + "-mirror.html")
        table = Table()
        table.feed(path.read_text(encoding="utf-8"))
        for cells in table.rows:
            if len(cells) < 11 or not re.fullmatch(r"[A-Za-z]+\d+", cells[1]):
                continue
            product = re.sub(r"\d+$", "", cells[1]).upper()
            result[product] = dict(long=float(cells[5].rstrip("%"))/100,
                                   short=float(cells[6].rstrip("%"))/100,
                                   observed_contract=cells[1], source_sha256=digest(path), exchange=exchange)
    return result


class Prices:
    def __init__(self, symbols):
        self.data = {s:([b["datetime"] for b in d["bars"]],[b["close"] for b in d["bars"]]) for s,d in symbols.items()}

    def prior(self, symbol, timestamp, fallback, include_current=False, not_before=None):
        times, closes = self.data[symbol]
        index = (bisect.bisect_right if include_current else bisect.bisect_left)(times,timestamp)-1
        return closes[index] if index >= 0 and (not_before is None or times[index] >= not_before) else fallback


def simulate(rows, symbols, margins, capital=50000., buffer=1.10, dynamic=False, exits_first=False,
             slots_per_symbol=None):
    prices = Prices(symbols)
    active, decisions, completed, unknown_accepted = {}, [], [], []
    running_cash, cumulative_known_net = capital, 0.
    max_margin = max_positions = max_lots = 0
    min_known_available, min_known_equity = capital, capital
    cash_peak, drawdown = capital, 0.
    events = []
    monthly_cash_flow = collections.defaultdict(float)
    first_unknown = None
    rejected_by_reason = collections.Counter()
    for row in rows:
        e = evaluation(row)
        if e["status"] == "closed":
            fills = row["result"]["fills"]
            for index, fill in enumerate(fills):
                # Even in exit-first sensitivity, a same-bar roundtrip cannot
                # close before its own open. Existing-position closes go first.
                same_bar_roundtrip = index > 0 and fill["ts"] == fills[0]["ts"]
                priority = (1 if index == 0 else 2 if same_bar_roundtrip else 0) if exits_first else (0 if index == 0 else 1)
                events.append((fill["ts"],priority,row,index))
        elif e["status"] in ("ambiguous","missing_data","rollover","unfinished","pending"):
            # Unknown economics are never assigned a simulated win/loss or zero.
            # Reserve planned notional margin for the maximum observation window.
            events.extend([(row["warning_ts"],0 if not exits_first else 1,row,-1),
                           (worst_end(row,symbols),1 if not exits_first else 0,row,-2)])

    def mark(ts):
        margin = floating = 0.
        for pos in active.values():
            price = prices.prior(pos["symbol"],ts,pos["entry"],not_before=pos["opened_at"])
            margin += price * pos["multiplier"] * pos["remaining"] * pos["rate"] * buffer
            if not pos["unknown"]:
                floating += (price-pos["entry"]) * pos["sign"] * pos["multiplier"] * pos["remaining"]
        known_equity = running_cash + floating
        available = (known_equity if dynamic else capital) - margin
        return margin, known_equity, available

    # Same timestamp entries ordered by earlier warning then key, never profitability.
    events.sort(key=lambda x:(x[0],x[1],x[2]["warning_ts"],x[2]["event_key"],x[3]))
    rejected, charged = set(), 0.
    for ts,_,row,index in events:
        key, e = row["event_key"], evaluation(row)
        is_open = index in (0,-1)
        if is_open:
            unknown = index == -1
            fill = row.get("planned_entry", e["fill"]) if unknown else row["result"]["fills"][0]["price"]
            if fill is None:
                raise ValueError("missing planned entry for unknown exposure")
            product = re.sub(r"\d+$", "", row["symbol"]).upper()
            rate = margins[product]["long" if row["direction"] == "up" else "short"]
            qty = e["volume"]
            margin, equity, available = mark(ts)
            required = fill * row["multiplier"] * qty * rate * buffer
            fee = 0. if unknown else row["cost_repricing"]["fee_legs"][0]["fee"]
            # Charge only the known opening fee at admission. Future exit prices
            # and their ad-valorem fees must not affect this entry decision.
            same_symbol = sum(p["symbol"] == row["symbol"] for p in active.values())
            reason = ("symbol_capacity" if slots_per_symbol is not None and same_symbol >= slots_per_symbol
                      else "capital" if available + 1e-8 < required + fee else None)
            accept = reason is None
            decisions.append(dict(event_key=key, symbol=row["symbol"], ts=ts, accepted=accept,
                unknown_reserved=unknown, required_margin=required, opening_fee=fee,
                existing_margin=margin, known_component_equity=equity, known_component_available=available,
                planned_lots=qty, rate_after_buffer=rate*buffer, rejection_reason=reason,
                active_same_symbol=same_symbol))
            if not accept:
                rejected.add(key)
                rejected_by_reason[reason] += 1
                continue
            active[key] = dict(symbol=row["symbol"],entry=fill,multiplier=row["multiplier"],remaining=qty,
                               rate=rate,sign=1 if row["direction"]=="up" else -1,unknown=unknown,opened_at=ts)
            if unknown:
                unknown_accepted.append(key)
                if first_unknown is None:
                    first_unknown = dict(ts=ts,event_key=key,prior_priced_closed=len(completed),
                        prior_known_equity=equity,prior_known_cash=running_cash)
            else:
                running_cash -= fee
                charged += fee
                cumulative_known_net -= fee
                monthly_cash_flow[ts[:7]] -= fee
        elif key in active:
            pos = active[key]
            if index == -2:
                del active[key]
            else:
                fill = row["result"]["fills"][index]
                gross = (fill["price"]-pos["entry"]) * pos["sign"] * pos["multiplier"] * fill["volume"]
                details=row["cost_repricing"]["fee_legs"]
                exit_fee=0. if details[0]["kind"]=="user_roundtrip_override" else details[index]["fee"]
                running_cash += gross-exit_fee
                cumulative_known_net += gross-exit_fee
                monthly_cash_flow[ts[:7]] += gross-exit_fee
                charged += exit_fee
                pos["remaining"] -= fill["volume"]
                if pos["remaining"] == 0:
                    completed.append(row)
                    del active[key]
                elif pos["remaining"] < 0:
                    raise ValueError("negative remaining lots")
        margin, equity, available = mark(ts)
        max_margin = max(max_margin,margin)
        max_positions = max(max_positions,len(active))
        max_lots = max(max_lots,sum(p["remaining"] for p in active.values()))
        min_known_available = min(min_known_available,available)
        min_known_equity = min(min_known_equity,equity)
        cash_peak = max(cash_peak,equity)
        drawdown = max(drawdown,cash_peak-equity)
    # Unknown reservation is not an economically flat trade. Refuse total-equity
    # claims even after its diagnostic occupation horizon expires.
    return dict(initial_capital=capital, margin_multiplier=buffer, dynamic_known_equity=dynamic,
        exits_first_same_5m=exits_first, accepted_priced=stats(completed),
        rejected_capital=rejected_by_reason["capital"], rejected_total=len(rejected),
        rejected_by_symbol_capacity=rejected_by_reason["symbol_capacity"],
        slots_per_symbol=slots_per_symbol, accepted_unknown_count=len(unknown_accepted),
        accepted_unknown_keys=unknown_accepted, maximum_observed_margin=max_margin,
        maximum_concurrent_signals=max_positions, maximum_concurrent_lots=max_lots,
        known_component_net_realized=cumulative_known_net, known_component_cash=running_cash,
        total_account_final_equity=None if unknown_accepted else running_cash,
        minimum_known_component_equity=min_known_equity, minimum_known_component_available=min_known_available,
        known_component_mark_drawdown=drawdown, fees_charged=charged, remaining_known_positions=sum(not p["unknown"] for p in active.values()),
        first_unknown_acceptance=first_unknown,monthly_known_cash_flow=dict(sorted(monthly_cash_flow.items())),
        decisions=decisions)


def run(input_path,batch,fee_dir,output):
    input_path,batch,fee_dir,output=map(pathlib.Path,(input_path,batch,fee_dir,output))
    if output.exists():
        raise FileExistsError("frozen output exists")
    source,cost=read_json(input_path),read_json(batch/"summary.json")
    if digest(input_path)!=cost["input_sha256"]:
        raise ValueError("source mismatch")
    margins=margin_profile(fee_dir)
    cases=collections.defaultdict(list)
    for row in jsonl(batch/"evaluations.jsonl"):
        if row["scenario"].split("/")[0] in ("original_s1","actual_tier") and "/nf5/" in row["scenario"] and not row["scenario"].endswith("fee_only"):
            cases[row["scenario"]].append(row)
    # Attach frozen planned entries, not inferred from future prices.
    # Frozen raw-input old_events can omit some regenerated events: use original
    # evaluator signal file explicitly, supplied by sibling frozen final run.
    signals=read_json(input_path.parent/"final-20261022-v3"/"signals.json")
    planned={"|".join(e[k] for k in ("symbol","direction","s0_ts","s1_ts","s2_ts","warning_ts")):e["entry"] for e in signals}
    for rows in cases.values():
        for row in rows:
            row["planned_entry"]=planned[row["event_key"]]
    output.mkdir()
    result=dict(scenarios={},initial_capital=50000,margin_multiplier=1.10,
        margin_profile={k:v for k,v in margins.items() if k in cost["fee_profile"]["products"]},
        source_hashes={str(p):digest(p) for p in (input_path,batch/"evaluations.jsonl",pathlib.Path(__file__))},
        qualified_for_trading=False,notes=[
            "Exchange margin snapshot from public mirrors, 10% relative buffer; not +10 percentage points.",
            "Static budget tests margin capacity only; dynamic variant includes priced realized/floating components.",
            "Unknown opportunities reserve margin, their P&L remains unpriced: final account equity is null if any accepted.",
            "No margin netting between signals or opposite positions, no retry after capital rejection, no forced liquidation.",
            "Fees charged per fill; SR user roundtrip override charged once at entry. Marks use prior 5m close after entry.",
            "Same-5m open/close sequencing tested both ways; no tick-level queue or fill confirmation simulation.",
            "Results conditional on historical continuous-series paths and current published margin/fee snapshots."])
    with (output/"capital-decisions.jsonl").open("x",encoding="utf-8") as f:
        for name,rows in cases.items():
            variants={}
            for dynamic in (False,True):
                for exits_first in (False,True):
                    label=f"{'dynamic_known' if dynamic else 'static_50k'}/{'exit_first' if exits_first else 'entry_first'}"
                    s=simulate(rows,source["symbols"],margins,dynamic=dynamic,exits_first=exits_first)
                    for d in s.pop("decisions"):
                        f.write(json.dumps(dict(scenario=name,capacity=label,**d),ensure_ascii=False)+"\n")
                    variants[label]=s
            result["scenarios"][name]=variants
    write_new(output/"summary.json",result)
    print(json.dumps({name:{v:{k:s[k] for k in ("rejected_capital","accepted_unknown_count","maximum_observed_margin","known_component_net_realized","total_account_final_equity","minimum_known_component_available")} for v,s in variants.items()} for name,variants in result["scenarios"].items()},indent=2))


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("input","batch","fee_dir","output"):
        parser.add_argument(name)
    args=parser.parse_args()
    run(args.input,args.batch,args.fee_dir,args.output)
