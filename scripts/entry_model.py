"""Small, serializable warning-time S1 research models (Python standard library).

This module consumes snapshots only. Realized fills and excursions are labels,
never features. Nothing here connects to a database, broker, or model registry.
"""
import collections
import datetime as dt
import math
import statistics

from reprice_signal_costs import fee_for


BASE = ["a_move_atr", "b_move_atr", "a_speed", "retracement",
        "warning_volume_ratio", "setup_quality", "direction_num"]
NORMALIZED = ["a_speed_atr" if name == "a_speed" else name for name in BASE]
SPACE = ["planned_rr", "risk_ticks", "net_reward_r", "full_stop_loss_r",
         "friction_r", "warning_chase_r"]
SHAPE = ["a_bars", "b_bars", "speed_ratio", "warning_close_directional",
         "warning_body_atr", "warning_wick_ratio"]
CONTEXT = ["trend60_alignment", "trend60_strength", "atr14_to_prior50",
           "clock_sin", "clock_cos"]
FEATURE_GROUPS = dict(legacy7=BASE, space_cost=NORMALIZED + SPACE,
                      shape=NORMALIZED + SPACE + SHAPE,
                      context=NORMALIZED + SPACE + SHAPE + CONTEXT)
CONTRACT = "warning-s1-net-expectancy-research-v1"
CONFIG = dict(l2=10.0, epochs=600, learning_rate=0.05, scaled_clip=5.0,
              calibration_fraction=0.25, calibration_min_rows=20,
              fit_min_rows=30, payoff_prior_count=20, probability_gate=0.5,
              expected_r_gate=0.0)


def sign_of(event):
    if event["direction"] not in ("up", "down"):
        raise ValueError("invalid direction")
    return 1 if event["direction"] == "up" else -1


def commission_bound(spec, entry, exit_price, multiplier):
    if "roundtrip_fixed_yuan_per_lot" in spec:
        return spec["roundtrip_fixed_yuan_per_lot"]
    return fee_for(spec["open"], entry, 1, multiplier) + max(
        fee_for(spec[kind], exit_price, 1, multiplier)
        for kind in ("close_today", "close_yesterday"))


def geometry(event, spec, multiplier, fill=None):
    """One-tick forecast is fixed independently of execution stress."""
    sign, tick = sign_of(event), event["tick_size"]
    if min(tick, multiplier, event["risk"]) <= 0:
        raise ValueError("nonpositive tick/multiplier/risk")
    fill = event["entry"] + sign * tick if fill is None else fill
    target_fill, stop_fill = event["target"] - sign * tick, event["stop"] - sign * tick
    target_fee = commission_bound(spec, fill, target_fill, multiplier)
    stop_fee = commission_bound(spec, fill, stop_fill, multiplier)
    unit = event["risk"] * multiplier
    reward = sign * (target_fill - fill) * multiplier - target_fee
    loss = sign * (fill - stop_fill) * multiplier + stop_fee
    return dict(forecast_fill=fill, net_reward_r=reward / unit,
                full_stop_loss_r=loss / unit,
                friction_r=(2 * tick * multiplier + stop_fee) / unit,
                net_reward_loss=reward / loss if loss > 0 else None,
                forecast_stop_risk_yuan=loss, forecast_target_profit_yuan=reward)


def price_limit(event, spec, multiplier):
    """Tick-rounded boundary for net reward >= full stop loss; NOT a model EV bound."""
    sign, tick = sign_of(event), event["tick_size"]
    width = sign * (event["target"] - event["stop"])
    if width <= 0:
        return None
    def gap(progress):
        g = geometry(event, spec, multiplier, event["stop"] + sign * progress)
        return g["net_reward_r"] - g["full_stop_loss_r"]
    if gap(0) < 0:
        return None
    lo, hi = 0.0, width
    for _ in range(60):
        mid = (lo + hi) / 2
        if gap(mid) >= 0:
            lo = mid
        else:
            hi = mid
    boundary = event["stop"] + sign * lo
    return (math.floor(boundary / tick + 1e-10) if sign > 0 else
            math.ceil(boundary / tick - 1e-10)) * tick


def completed_context(raw, rollovers=()):
    """Causal complete 15m triples. TR history resets at confirmed rollover."""
    groups = collections.defaultdict(list)
    for bar in sorted(raw, key=lambda b: b["datetime"]):
        time = dt.datetime.fromisoformat(bar["datetime"])
        bucket = time.replace(second=0, microsecond=0) + dt.timedelta(minutes=(-time.minute) % 15)
        groups[bucket].append(bar)
    result, ranges, last_close = {}, [], None
    roll_times = sorted(r["ts"] for r in rollovers if r["confirmed"])
    roll_index = 0
    for end, bars in sorted(groups.items()):
        expected = [(end - dt.timedelta(minutes=m)).isoformat(sep=" ") for m in (10, 5, 0)]
        if [b["datetime"] for b in bars] != expected:
            continue
        timestamp = end.isoformat(sep=" ")
        reset = False
        while roll_index < len(roll_times) and roll_times[roll_index] <= timestamp:
            roll_index += 1
            reset = True
        if reset:
            ranges, last_close = [], None
        high, low = max(b["high"] for b in bars), min(b["low"] for b in bars)
        tr = high - low if last_close is None else max(high - low, abs(high-last_close), abs(low-last_close))
        ranges.append(tr)
        last_close = bars[-1]["close"]
        atr = statistics.mean(ranges[-14:]) if len(ranges) >= 14 else None
        prior = statistics.mean(ranges[-64:-14]) if len(ranges) >= 64 else None
        result[timestamp] = dict(open=bars[0]["open"], high=high, low=low, close=last_close,
                                 atr14=atr, atr14_to_prior50=atr / prior if atr and prior else None)
    return result


def features(event, spec, multiplier, context):
    """Never read event.evaluation/trigger_features/market_context outcomes."""
    sf, sign = event["setup_features"], sign_of(event)
    geo = geometry(event, spec, multiplier)
    a_bars, b_bars = max(1, sf["a_bars"]), max(1, sf["b_bars"])
    a_speed, b_speed = sf["a_move"] / a_bars, sf["b_move"] / b_bars
    clock = dt.datetime.fromisoformat(event["warning_ts"])
    angle = 2 * math.pi * (clock.hour * 60 + clock.minute) / 1440
    trend = sf["trend60_state"]
    trend_sign = 1 if "UP" in trend else -1 if "DOWN" in trend else 0
    strength = 2 if "STRONG" in trend else 1 if "WEAK" in trend else 0
    close_loc = sf.get("warning_close_location")
    f = {name: sf.get(name) for name in BASE if name not in ("direction_num",)}
    f.update(direction_num=sign, a_speed_atr=sf["a_move_atr"] / a_bars,
             planned_rr=event["rr"], risk_ticks=event["risk"] / event["tick_size"],
             net_reward_r=geo["net_reward_r"], full_stop_loss_r=geo["full_stop_loss_r"],
             friction_r=geo["friction_r"],
             warning_chase_r=(sign * (context["close"] - event["entry"]) / event["risk"]
                              if context else None),
             a_bars=sf["a_bars"], b_bars=sf["b_bars"],
             speed_ratio=b_speed / a_speed if a_speed > 0 else None,
             warning_close_directional=(close_loc if sign > 0 else 1-close_loc)
             if close_loc is not None else None,
             warning_body_atr=sf.get("warning_body_atr"),
             warning_wick_ratio=sf.get("warning_wick_ratio"),
             trend60_alignment=sign * trend_sign, trend60_strength=strength,
             atr14_to_prior50=context.get("atr14_to_prior50") if context else None,
             clock_sin=math.sin(angle), clock_cos=math.cos(angle))
    if any(value is not None and not math.isfinite(value) for value in f.values()):
        raise ValueError("non-finite warning feature")
    return f


def sigmoid(value):
    return 1 / (1 + math.exp(-max(-35, min(35, value))))


def logit(value):
    value = max(1e-9, min(1-1e-9, value))
    return math.log(value / (1-value))


def vector(model, values):
    clip = model["scaled_clip"]
    return [max(-clip, min(clip, ((values.get(name) if values.get(name) is not None else median)
                                 - mean) / std))
            for name, median, mean, std in zip(model["names"], model["medians"], model["means"], model["stds"])]


def raw_probability(model, values):
    return sigmoid(model["intercept"] + sum(c*x for c, x in zip(model["coefficients"], vector(model, values))))


def fit_classifier(rows, names):
    if len(rows) < CONFIG["fit_min_rows"]:
        raise ValueError("insufficient fitting rows")
    model = dict(names=names, medians=[], means=[], stds=[], scaled_clip=CONFIG["scaled_clip"])
    for name in names:
        observed = [r["features"][name] for r in rows if r["features"].get(name) is not None]
        median = statistics.median(observed) if observed else 0.0
        values = [r["features"].get(name) if r["features"].get(name) is not None else median for r in rows]
        model["medians"].append(median)
        model["means"].append(statistics.mean(values))
        model["stds"].append(statistics.pstdev(values) or 1.0)
    x, y = [vector(model, r["features"]) for r in rows], [int(r["net_r"] > 0) for r in rows]
    model.update(coefficients=[0.0] * len(names), intercept=logit(sum(y) / len(y)))
    for _ in range(CONFIG["epochs"]):
        gradient, intercept_gradient = [0.0] * len(names), 0.0
        for values, target in zip(x, y):
            error = sigmoid(model["intercept"] + sum(c*v for c, v in zip(model["coefficients"], values))) - target
            intercept_gradient += error
            for j, value in enumerate(values):
                gradient[j] += error * value
        model["intercept"] -= CONFIG["learning_rate"] * intercept_gradient / len(rows)
        for j in range(len(names)):
            model["coefficients"][j] -= CONFIG["learning_rate"] * (
                gradient[j] + CONFIG["l2"] * model["coefficients"][j]) / len(rows)
    return model


def fit_calibrator(model, rows):
    # Strong identity shrinkage; always preserve ordering with positive slope.
    a, b, penalty = 1.0, 0.0, 10.0
    values = [(logit(raw_probability(model, r["features"])), int(r["net_r"] > 0)) for r in rows]
    for _ in range(400):
        da, db = penalty * (a-1), penalty * b
        for value, target in values:
            error = sigmoid(a * value + b) - target
            da += error * value
            db += error
        a = max(.05, a - .05 * da / len(rows))
        b -= .05 * db / len(rows)
    return dict(method="purged_time_tail_sigmoid_identity_shrink10", slope=a, intercept=b, n=len(rows))


def payoff_band(values):
    rr = values["planned_rr"]
    return "below2" if rr < 2 else "2to3" if rr < 3 else "3plus"


def fit_payoffs(rows):
    wins = [r["net_r"] for r in rows if r["net_r"] > 0]
    losses = [-r["net_r"] for r in rows if r["net_r"] <= 0]
    if not wins or not losses:
        raise ValueError("payoff estimation requires both outcome branches")
    global_win, global_loss = statistics.mean(wins), statistics.mean(losses)
    groups = {}
    for band in ("below2", "2to3", "3plus"):
        group = [r for r in rows if payoff_band(r["features"]) == band]
        w, l = [r["net_r"] for r in group if r["net_r"] > 0], [-r["net_r"] for r in group if r["net_r"] <= 0]
        prior = CONFIG["payoff_prior_count"]
        groups[band] = dict(win_r=(sum(w) + prior * global_win) / (len(w)+prior),
                            loss_r=(sum(l) + prior * global_loss) / (len(l)+prior),
                            n=len(group), wins=len(w), losses=len(l))
    return dict(groups=groups, global_win_r=global_win, global_loss_r=global_loss,
                n=len(rows), losses_over1r=sum(r["net_r"] < -1 for r in rows),
                losses_over2r=sum(r["net_r"] < -2 for r in rows),
                mean_holding_minutes=statistics.mean(
                    (dt.datetime.fromisoformat(r["exit_ts"]) - dt.datetime.fromisoformat(r["entry_ts"])).total_seconds()/60
                    for r in rows))


def fit_bundle(rows, names, cutoff):
    rows = sorted(rows, key=lambda r: (r["warning_ts"], r["event_key"]))
    if any(r["exit_ts"] >= cutoff or r["warning_ts"] >= cutoff for r in rows):
        raise ValueError("label reaches into prediction period")
    if len(rows) < CONFIG["fit_min_rows"]:
        raise ValueError("insufficient training rows")
    dates = sorted({r["warning_ts"][:10] for r in rows})
    split = dates[min(len(dates)-1, int(len(dates) * (1-CONFIG["calibration_fraction"])))]
    fit = [r for r in rows if r["warning_ts"][:10] < split and r["exit_ts"] < split + " 00:00:00"]
    calibration = [r for r in rows if r["warning_ts"][:10] >= split]
    both_classes = len({r["net_r"] > 0 for r in calibration}) == 2
    enabled = len(fit) >= CONFIG["fit_min_rows"] and len(calibration) >= CONFIG["calibration_min_rows"] and both_classes
    fitting_rows = fit if enabled else rows
    model = fit_classifier(fitting_rows, names)
    calibrator = fit_calibrator(model, calibration) if enabled else dict(
        method="identity_insufficient_time_tail", slope=1.0, intercept=0.0, n=0)
    return dict(contract=CONTRACT, qualified_for_trading=False, classifier=model,
                calibrator=calibrator, payoffs=fit_payoffs(rows),
                audit=dict(cutoff=cutoff, n=len(rows), fit_n=len(fitting_rows),
                           calibration_n=len(calibration) if enabled else 0,
                           calibration_start=split if enabled else None,
                           fit_max_label_end=max(r["exit_ts"] for r in fitting_rows),
                           max_label_end=max(r["exit_ts"] for r in rows),
                           constant_win_rate=sum(r["net_r"] > 0 for r in rows) / len(rows)))


def predict(bundle, values):
    raw = raw_probability(bundle["classifier"], values)
    cal = bundle["calibrator"]
    probability = sigmoid(cal["slope"] * logit(raw) + cal["intercept"])
    group = bundle["payoffs"]["groups"][payoff_band(values)]
    win, loss = group["win_r"], group["loss_r"]
    return dict(p_win_raw=raw, p_win=probability,
                expected_net_r=probability * win - (1-probability) * loss,
                conditional_win_r=win, conditional_loss_r=loss,
                break_even_probability=loss/(win+loss), payoff_group_n=group["n"],
                training_loss_over2r_count=bundle["payoffs"]["losses_over2r"],
                tail_risk_status="insufficient_evidence_for_conditional_tail_model",
                probability_accept=probability >= CONFIG["probability_gate"],
                expectancy_accept=probability * win - (1-probability) * loss > CONFIG["expected_r_gate"])
