"""Offline fee sensitivity: published snapshot, not reconstructed historical billing.

The evaluator already embeds slippage in each fill. Only replace its extra fee.
Never opens a broker or database. Frozen inputs and outputs are never overwritten.
"""
import argparse
import bisect
import collections
import copy
import hashlib
import json
import pathlib
import re

from report_signal_ablation import block_interval, jsonl, paired, read_json, reserved_portfolio, stats, write_new


class TradingDates:
    """Infer exchange trading dates from observed day sessions across all symbols.

    Night >=18:00 maps to the next observed daytime date; after midnight <06:00
    maps to the first observed daytime date >= calendar date. Friday night and
    Saturday early morning therefore both map to Monday, including holiday gaps.
    This is an inference from archived sessions, not an official holiday calendar.
    """
    def __init__(self, symbols):
        self.dates = sorted({b["datetime"][:10] for s in symbols.values() for b in s["bars"]
                             if 6 <= int(b["datetime"][11:13]) < 18})

    def day(self, ts):
        date, hour = ts[:10], int(ts[11:13])
        if 6 <= hour < 18:
            if date not in self.dates:
                raise ValueError("day session absent from inferred calendar: " + ts)
            return date
        index = bisect.bisect_right(self.dates, date) if hour >= 18 else bisect.bisect_left(self.dates, date)
        if index >= len(self.dates):
            raise ValueError("night session has no following observed day: " + ts)
        return self.dates[index]


def fee_for(rule, price, volume, multiplier):
    fixed, rate = rule["fixed_yuan_per_lot"], rule["notional_rate"]
    if min(fixed, rate, price, volume, multiplier) < 0:
        raise ValueError("negative fee or fill input")
    return volume * (fixed + rate * price * multiplier)


def reprice(row, profile, calendar):
    row = copy.deepcopy(row)
    e = row["result"]["evaluation"]
    product = re.sub(r"\d+$", "", row["symbol"]).upper()
    spec = profile["products"][product]  # missing products must fail, never silently free
    if e["status"] != "closed":
        return row
    fills = row["result"]["fills"]
    if not fills or fills[0]["offset"] != "open" or any(x["offset"] != "close" for x in fills[1:]):
        raise ValueError("invalid or absent fill ledger")
    opened, closed = fills[0]["volume"], sum(x["volume"] for x in fills[1:])
    if opened != closed or opened != e["volume"]:
        raise ValueError("fill volumes do not reconcile")
    sign = 1 if row["direction"] == "up" else -1
    points = sum(sign * (x["price"] - fills[0]["price"]) * x["volume"] for x in fills[1:])
    if abs(points - e["gross_points"]) > 1e-6:
        raise ValueError("fill cash does not reconcile")
    details = []
    if "roundtrip_fixed_yuan_per_lot" in spec:
        # Explicit user clarification overrides the published sugar close-yesterday schedule.
        commission = spec["roundtrip_fixed_yuan_per_lot"] * opened
        details.append(dict(kind="user_roundtrip_override", volume=opened, fee=commission))
    else:
        open_day = calendar.day(fills[0]["ts"])
        for fill in fills:
            day = calendar.day(fill["ts"])
            kind = "open" if fill["offset"] == "open" else "close_today" if day == open_day else "close_yesterday"
            fee = fee_for(spec[kind], fill["price"], fill["volume"], row["multiplier"])
            details.append(dict(kind=kind, trading_day=day, ts=fill["ts"], price=fill["price"], volume=fill["volume"], fee=fee))
        commission = sum(x["fee"] for x in details)
    gross = e["gross_points"] * row["multiplier"]
    row["cost_repricing"] = dict(product=product, original_proxy_net_amount=e["net_amount"],
                                 original_proxy_net_r=e["net_r"], gross_amount_after_slippage=gross,
                                 commission=commission, fee_legs=details)
    e["net_amount"] = gross - commission
    e["net_r"] = e["net_amount"] / (e["risk"] * row["multiplier"] * e["volume"])
    return row


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(input_path, batch, profile_path, output):
    input_path, batch, profile_path, output = map(pathlib.Path, (input_path, batch, profile_path, output))
    if output.exists():
        raise FileExistsError("frozen output must be new: " + str(output))
    source, profile = read_json(input_path), read_json(profile_path)
    manifest = read_json(batch / "manifest.json")
    if digest(input_path) != manifest["input_sha256"]:
        raise ValueError("source differs from frozen evaluator input")
    archive = pathlib.Path(manifest["provenance"]["archive"])
    if digest(archive) != manifest["provenance"]["archive_sha256"]:
        raise ValueError("source archive differs from frozen provenance")
    products = {re.sub(r"\d+$", "", s).upper() for s in source["symbols"]}
    if products - profile["products"].keys():
        raise ValueError("fee profile lacks products: " + str(products - profile["products"].keys()))
    calendar = TradingDates(source["symbols"])
    cases = collections.defaultdict(dict)
    output.mkdir()
    with (output / "evaluations.jsonl").open("x", encoding="utf-8") as f:
        for row in jsonl(batch / "evaluations.jsonl"):
            repriced = reprice(row, profile, calendar)
            f.write(json.dumps(repriced, ensure_ascii=False) + "\n")
            cases[row["scenario"]][row["event_key"]] = repriced
    summary = dict(fee_profile=profile, scenarios={}, pairs={}, groups={}, reservation_portfolios={},
                   input_sha256=digest(input_path), evaluator_manifest_sha256=digest(batch / "manifest.json"),
                   evaluator_rows_sha256=digest(batch / "evaluations.jsonl"), profile_sha256=digest(profile_path),
                   repricer_sha256=digest(pathlib.Path(__file__)),
                   statistics_module_sha256=digest(pathlib.Path(__file__).with_name("report_signal_ablation.py")), inference_notes=[
                       "Latest published main-contract rates applied to historical continuous-series fills; not historical actual bills.",
                       "No broker markup/rebate assumed. SR total roundtrip 2 yuan/lot explicitly overrides exchange schedule.",
                       "Trading dates inferred from union of observed daytime sessions; not official exchange calendar.",
                       "Slippage already embedded in each actual fill; never deducted a second time.",
                       "All closed signals independently summed, not a real account equity curve; reservation shown separately.",
                       "Two monotone OHLC paths agreement is not exhaustive proof of intrabar execution.",
                       "Fees retain floating precision; exchange/broker rounding not reconstructed.",
                   ])
    for name, mapping in cases.items():
        rows = list(mapping.values())
        summary["scenarios"][name] = stats(rows)
        closed = [r for r in rows if r["result"]["evaluation"]["status"] == "closed"]
        summary["scenarios"][name].update(
            gross_amount_after_slippage=sum(r["cost_repricing"]["gross_amount_after_slippage"] for r in closed),
            commission=sum(r["cost_repricing"]["commission"] for r in closed),
            original_proxy_net_amount=sum(r["cost_repricing"]["original_proxy_net_amount"] for r in closed),
        )
    baseline = {r["event_key"]: r["evaluation"] for r in jsonl(batch / "baseline.jsonl")}
    current = cases["actual_tier/nf5/slip1_fee"]
    baseline_closed = {k for k, e in baseline.items() if e["status"] == "closed"}
    original_rows = [current[k] for k in sorted(baseline_closed)]
    for row in original_rows:
        previous, e = baseline[row["event_key"]], row["result"]["evaluation"]
        if e["status"] != "closed" or e["exit_ts"] != previous["exit_ts"] or abs(
                row["cost_repricing"]["original_proxy_net_amount"] - previous["net_amount"]) > 1e-6:
            raise ValueError("original closed cohort does not reconcile to frozen replay")
    summary["original_baseline_closed_cohort"] = stats(original_rows)
    summary["original_baseline_closed_cohort"].update(
        previous_proxy_net_amount=sum(baseline[k]["net_amount"] for k in baseline_closed),
        previous_proxy_win_rate=sum(baseline[k]["net_r"] > 0 for k in baseline_closed) / len(baseline_closed),
        commission=sum(r["cost_repricing"]["commission"] for r in original_rows),
        gross_amount_after_slippage=sum(r["cost_repricing"]["gross_amount_after_slippage"] for r in original_rows))
    policies = ("original_s1", "fixed_one_r", "legacy_staircase", "actual_tier")
    for policy in policies:
        for cost in ("zero", "slip1_fee", "slip2_fee"):
            left, right = f"{policy}/nf5/{cost}", f"{policy}/nf_off/{cost}"
            summary["pairs"][left + " -> " + right] = paired(cases[left], cases[right])
        left, right = "actual_tier/nf5/slip1_fee", f"{policy}/nf5/slip1_fee"
        summary["pairs"][left + " -> " + right] = paired(cases[left], cases[right])
        for cost in ("zero", "slip1_fee", "slip2_fee"):
            name = f"{policy}/nf5/{cost}"
            rows = list(cases[name].values())
            summary["reservation_portfolios"][name] = reserved_portfolio(rows, source["symbols"])
            summary["scenarios"][name]["block_interval"] = block_interval(rows)
            groups = {}
            for key, grouper in (("symbol", lambda r: r["symbol"]), ("month", lambda r: r["warning_ts"][:7]),
                                 ("direction", lambda r: r["direction"]),
                                 ("rr", lambda r: "1+" if r["rr"] >= 1 else "below1")):
                bins = collections.defaultdict(list)
                for row in rows:
                    bins[grouper(row)].append(row)
                groups[key] = {k: stats(v) for k, v in sorted(bins.items())}
            summary["groups"][name] = groups
    write_new(output / "summary.json", summary)
    write_new(output / "fee-profile.json", profile)
    lines = ["# 最新公开手续费标准重估（非历史真实账单）", "", "白糖按用户确认的往返合计2元/手；其他按费率快照，平今/平昨分开、按逐笔成交金额计算。",
             "`zero`表示零滑点，仍扣本次手续费；`slip1_fee`/`slip2_fee`表示每侧1/2跳滑点，已包含在成交价中。", "",
             "|策略/nf5|滑点/侧|平仓数|净胜率|净金额（元）|平均净R|手续费（元）|",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for policy in policies:
        for cost, slip in (("zero",0),("slip1_fee",1),("slip2_fee",2)):
            s = summary["scenarios"][f"{policy}/nf5/{cost}"]
            lines.append(f'|{policy}|{slip}|{s["n"]}|{s["win_rate"]:.2%}|{s["net_amount"]:.2f}|{s["mean_net_r"]:.4f}|{s["commission"]:.2f}|')
    lines.extend(["", "金额为所有独立平仓信号的模拟净额合计，包含重叠信号，不是实盘账户收益。不同滑点情景的可确定平仓样本会变化；同样本比较见summary.json的pairs。",
                  "", "当前档位退出规则在每侧1跳滑点后、手续费前已为负；降低手续费不能单独修复退出规则。", "",
                  "## 资料与限制", ""])
    for key, value in profile["sources"].items():
        lines.append(f'- {key}: [{value["title"]}]({value["url"]})；{value["verification"]}')
    lines.extend(["", *('- ' + x for x in summary["inference_notes"])])
    (output / "手续费重估报告.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if digest(archive) != manifest["provenance"]["archive_sha256"]:
        raise ValueError("archive hash changed during run")
    print(json.dumps({name: {k: s[k] for k in ("n", "win_rate", "net_amount", "mean_net_r", "commission")}
                      for name, s in summary["scenarios"].items() if "/nf5/" in name and not name.endswith("fee_only")}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("batch")
    parser.add_argument("profile")
    parser.add_argument("output")
    args = parser.parse_args()
    run(args.input, args.batch, args.profile, args.output)
