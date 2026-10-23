"""Audit frozen walk-forward probabilities against their own execution labels.

No fitting, threshold selection, registry writes, or production changes. Existing
history remains development data even though the predictions are out of fold.
"""
import argparse
import collections
import hashlib
import json
import math
import pathlib


THRESHOLDS = (0.0, 0.4, 0.5, 0.6, 0.7)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def validate_oof(data):
    rows, probabilities = data["rows"], data["p_win"]
    if len(rows) != len(probabilities):
        raise ValueError("row/probability length mismatch")
    seen = set()
    previous = ""
    for row, probability in zip(rows, probabilities):
        key = row["event_id"]
        if key in seen:
            raise ValueError("duplicate out-of-fold event: " + key)
        seen.add(key)
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError("invalid probability")
        net_r = row.get("r_multiple")
        if net_r is None or not math.isfinite(net_r):
            raise ValueError("missing/non-finite realized return")
        if row["label_win"] != int(net_r > 0):
            raise ValueError("label and net return disagree")
        decision, end = row.get("trigger_bar_ts"), row.get("exit_ts")
        if not decision or not end or end <= decision or decision < previous:
            raise ValueError("invalid decision/exit chronology")
        previous = decision
    return list(zip(rows, probabilities))


def stats(pairs):
    returns = [row["r_multiple"] for row, _ in pairs]
    n = len(returns)
    positive, negative = [r for r in returns if r > 0], [r for r in returns if r < 0]
    mean_win = sum(positive) / len(positive) if positive else None
    mean_loss = -sum(negative) / len(negative) if negative else None
    equity = peak = drawdown = 0.0
    for row, _ in sorted(pairs, key=lambda pair: (pair[0]["exit_ts"], pair[0]["event_id"])):
        equity += row["r_multiple"]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return dict(
        n=n, wins=len(positive), losses=len(negative), flat=n-len(positive)-len(negative),
        mean_predicted_probability=sum(p for _, p in pairs) / n if n else None,
        net_win_rate=len(positive) / n if n else None,
        mean_net_r=sum(returns) / n if n else None, total_net_r=sum(returns),
        net_r_without_top5=sum(returns)-sum(sorted(positive, reverse=True)[:5]),
        mean_win_r=mean_win, mean_loss_magnitude_r=mean_loss,
        descriptive_break_even_probability=(mean_loss / (mean_win + mean_loss)
                                             if mean_win and mean_loss else None),
        realized_exit_order_drawdown_r=drawdown,
    )


def threshold_table(pairs):
    return {f"{threshold:.1f}": stats([(row, p) for row, p in pairs if p >= threshold])
            for threshold in THRESHOLDS}


def summarize(data, report, model):
    pairs = validate_oof(data)
    folds = report["walk_forward"]
    if sum(fold["validation"] for fold in folds) != len(pairs):
        raise ValueError("fold sizes do not reconcile with out-of-fold predictions")
    results, offset = [], 0
    for index, fold in enumerate(folds, 1):
        segment = pairs[offset:offset + fold["validation"]]
        metrics = fold["metrics"]
        if metrics["n"] != len(segment):
            raise ValueError("fold metric sample size mismatch")
        brier = sum((p - row["label_win"]) ** 2 for row, p in segment) / len(segment)
        if not math.isclose(brier, metrics["brier"], abs_tol=1e-10):
            raise ValueError("frozen fold Brier score does not reproduce")
        results.append(dict(fold=index, train=fold["train"], n=len(segment),
                            start=segment[0][0]["trigger_bar_ts"],
                            end=segment[-1][0]["trigger_bar_ts"],
                            auc=metrics["auc"], brier_skill=metrics["brier_skill"],
                            thresholds=threshold_table(segment)))
        offset += len(segment)
    months = collections.defaultdict(list)
    for row, p in pairs:
        months[row["trigger_bar_ts"][:7]].append((row, p))
    bins = {}
    for lower, upper in zip((0, .3, .4, .5, .6, .7), (.3, .4, .5, .6, .7, 1.01)):
        bins[f"{lower:.1f}-{min(upper, 1):.1f}"] = stats(
            [(row, p) for row, p in pairs if lower <= p < upper])
    return dict(
        source="historical_development", contract=report["contract"], policy=report["policy"],
        cost_version=report["cost_version"], features=model["feature_names"],
        model_id=report.get("model_id"), thresholds=threshold_table(pairs),
        probability_bands=bins, folds=results,
        months={key: threshold_table(value) for key, value in sorted(months.items())},
        qualified_for_trading=False, production_changed=False,
        limits=[
            "Thresholds are sensitivity diagnostics, not an optimized or validated entry rule.",
            "OOF covers closed, feature-complete, non-overlapping rows of the original policy only.",
            "Do not transfer these probabilities to S1/confirmation/close-entry policy labels.",
            "Costs are the frozen tick proxy, not historical actual contract commissions.",
            "No new capacity simulation; totals are not a funded account equity curve.",
            "Payoff and break-even values use realized outcomes; they are not entry-time forecasts.",
            "Reused development history and cross-symbol dependence preclude a fresh validation claim.",
        ],
    )


def markdown(summary):
    lines = ["# 模型开仓价值诊断", "", "仅历史开发诊断；不训练、不晋升、不修改生产。", "",
             f"标签合同：`{summary['contract']}`；执行策略：`{summary['policy']}`。", "",
             "|概率门槛|笔数|实际净胜率|平均净R|累计净R|剔除最大5笔盈利后R|",
             "|---|---:|---:|---:|---:|---:|"]
    for threshold, group in summary["thresholds"].items():
        rate = f"{group['net_win_rate']:.2%}" if group["n"] else "—"
        mean = f"{group['mean_net_r']:+.4f}" if group["n"] else "—"
        lines.append(f"|≥{float(threshold):.0%}|{group['n']}|{rate}|{mean}|"
                     f"{group['total_net_r']:+.4f}|{group['net_r_without_top5']:+.4f}|")
    lines += ["", "高概率样本很少；阈值提高后的正收益不构成上线依据。", "",
              "盈亏平衡概率由样本内已实现平均盈亏反算，仅作解释，不能用于当时开仓。", "",
              "## 口径限制", ""]
    lines += ["- " + limit for limit in summary["limits"]]
    return "\n".join(lines) + "\n"


def run(source, output):
    source, output = pathlib.Path(source), pathlib.Path(output)
    if output.exists():
        raise FileExistsError("immutable output already exists")
    files = [source / name for name in ("oof.json", "report.json", "model.json")]
    summary = summarize(*(read_json(path) for path in files))
    summary["source_sha256"] = {
        str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [*files, pathlib.Path(__file__)]}
    output.mkdir(parents=True)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2,
                                                  allow_nan=False), encoding="utf-8")
    (output / "模型开仓诊断.md").write_text(markdown(summary), encoding="utf-8")
    print(json.dumps(summary["thresholds"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="frozen v3 run containing OOF/report/model")
    parser.add_argument("--out", required=True, help="new immutable output directory")
    args = parser.parse_args()
    run(args.source, args.out)
