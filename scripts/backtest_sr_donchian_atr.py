"""Frozen Donchian/ATR baseline: daily primary, 15-minute separate experiment.

Read-only cached public market observations; no trading/broker imports.
python scripts/backtest_sr_donchian_atr.py
python -m unittest discover -s tests -p test_sr_donchian_atr.py
"""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports/sr_donchian_atr_20261021"
SOURCE = ROOT / "reports/sr_dual_thrust_history_20261021"
RECENT = ROOT / "reports/sr_dual_thrust_20261021"
START, END = date(2026, 9, 21), date(2026, 10, 20)
ZONE = timezone(timedelta(hours=8))
HOLIDAYS = {date(2026, 6, 19), date(2026, 9, 25)} | {date(2026, 10, d) for d in range(1, 8)}
NO_NIGHTS = {date(2026, 6, 18), date(2026, 9, 24), date(2026, 9, 30)}


@dataclass(frozen=True)
class Config:
    entry: int = 20
    exit: int = 10
    atr: int = 20
    stop_n: float = 2.
    initial_capital: float = 100000.
    unit_n_fraction: float = .01
    multiplier: int = 10
    tick: float = 1.
    slip_ticks: int = 1
    fee_side: float = 3.
    assumed_margin_rate: float = .12
    max_margin_fraction: float = .5


@dataclass(frozen=True)
class Bar:
    dt: datetime
    o: float
    h: float
    l: float
    c: float
    v: float = 100.
    oi: float = 1000.


def save(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT/name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def csv_save(name, rows):
    if rows:
        with (OUT/name).open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)


def freeze():
    plan = dict(contract="CZCE.SR701 / SR2701", selection="Chosen before this run: cached complete minute/daily history and recent dominant membership, no strategy-performance selection.",
        start=str(START), end=str(END), config=asdict(Config()),
        variants=["daily_primary", "15m_separate_short_swing"], optimization=False,
        rules=["Prior completed 20 bars high + 1 tick / low - 1 tick; stop-market entries intrabar.",
               "Prior completed 10 bars opposite extreme +/- 1 tick exits; 2N protective stop frozen at entry.",
               "N is Wilder ATR20 (first 20 true ranges arithmetic seed, then (19*N+TR)/20), prior completed bars only.",
               "One unit=floor(1% current equity / (N*10)); 2N initial stop therefore approximately 2% equity risk, NOT 1%.",
               "No pyramiding, no prior-winner skipping, no 55-day failsafe: classic-parameter simplified Donchian baseline, NOT full Turtle System 1.",
               "Start flat; pre-window history warms indicators only. Positions may cross nights/weekends/holidays.",
               "Assume 12% margin, cap initial margin at 50% equity. These are modelling assumptions, not claimed historical broker rates.",
               "Fee=3 CNY/lot/side assumed total commission, adverse 1 tick/side; stress scenarios keep strategy parameters fixed.",
               "Daily signal levels held fixed for exchange trading date. 15m levels update only after complete 15m close.",
               "Stops fill at trigger or worse minute open, plus slip; a new position may stop out in same minute.",
               "At most one entry per minute; after an exit no reentry until next minute. Zero-volume minutes cannot fill.",
               "Evaluate O-H-L-C and O-L-H-C when needed; primary chooses lower current-minute close equity, not a mathematical monthly lower bound.",
               "No natural exit at endpoint: leave position marked to last close; separately report hypothetical endpoint liquidation and its win rate.",
               "Minute close equity includes entry fee and floating PnL; terminal liquidation deducts exit slippage and fee."],
        limitations=["One product, one month; not out-of-sample evidence, no parameter tuning.",
                     "OHLC execution approximation, no bid/ask/queue/limit-lock simulation; close-price marks, not daily settlement cash flows.",
                     "No carried-in positions from prior months; monthly results depend on this flat-start convention."],
        sources=["https://www.tradingblox.com/originalturtles/originalturtlerules.htm",
                 "https://github.com/vnpy/vnpy_ctastrategy/blob/main/vnpy_ctastrategy/strategies/turtle_signal_strategy.py",
                 "https://doc.shinnytech.com/edb/latest/md_server.html",
                 "https://www.cjfco.com.cn/main/a/20260922/83225.shtml",
                 "https://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2026/6/9c4c45f4d3594b28934613798bfccb4a.htm"])
    path = OUT/"analysis_plan.json"
    if path.exists():
        assert json.loads(path.read_text(encoding="utf-8")) == plan, "Frozen plan changed"
    else:
        save("analysis_plan.json", plan)


def trading_day(d):
    return d.weekday() < 5 and d not in HOLIDAYS


def previous_day(d):
    d -= timedelta(days=1)
    while not trading_day(d): d -= timedelta(days=1)
    return d


def trading_date(dt):
    d = dt.date()
    if dt.hour >= 21:
        d += timedelta(days=1)
        while not trading_day(d): d += timedelta(days=1)
    return d


def expected_minutes(d):
    result = set()
    for hh, mm, count in [(9, 0, 75), (10, 30, 60), (13, 30, 90)]:
        start = datetime.combine(d, time(hh, mm))
        result.update(start + timedelta(minutes=i) for i in range(count))
    prev = previous_day(d)
    if prev not in NO_NIGHTS:
        start = datetime.combine(prev, time(21))
        result.update(start + timedelta(minutes=i) for i in range(120))
    return result


def load(path):
    bars = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            dt = datetime.fromtimestamp(int(row["datetime_nano"])//10**9, ZONE).replace(tzinfo=None)
            b = Bar(dt, *(float(row[k]) for k in ["open", "high", "low", "close", "volume", "close_oi"]))
            assert all(math.isfinite(x) for x in [b.o,b.h,b.l,b.c,b.v,b.oi])
            assert b.l <= min(b.o,b.c) <= max(b.o,b.c) <= b.h
            assert all(x == round(x) for x in [b.o,b.h,b.l,b.c]) and b.v >= 0
            bars.append(b)
    assert bars and bars == sorted(bars, key=lambda b: b.dt)
    assert len({b.dt for b in bars}) == len(bars)
    return bars


def prepare():
    manifests = {folder:json.loads((folder/"sources.json").read_text(encoding="utf-8")) for folder in [SOURCE, RECENT]}
    used = []
    for folder, names in [(SOURCE,["SR701_1m.csv","SR701_daily.csv"]),
                          (RECENT,["main_daily.csv","SR611_daily.csv","SR705_daily.csv"])]:
        for name in names:
            item = next(s for s in manifests[folder] if s["file"] == name)
            assert hashlib.sha256((folder/name).read_bytes()).hexdigest() == item["sha256"]
            used.append(dict(item, local_path=str(folder/name)))
    save("sources.json", used)
    minutes = load(SOURCE/"SR701_1m.csv")
    daily = [b for b in load(SOURCE/"SR701_daily.csv") if b.dt.date() <= END]
    groups = defaultdict(list)
    for b in minutes:
        if trading_date(b.dt) <= END: groups[trading_date(b.dt)].append(b)
    scored = [d for d in groups if START <= d <= END]
    expected_days = []; d = START
    while d <= END:
        if trading_day(d): expected_days.append(d)
        d += timedelta(days=1)
    assert sorted(scored) == expected_days
    ds = {b.dt.date():b for b in daily}
    main = {b.dt.date():b for b in load(RECENT/"main_daily.csv")}
    others = {name:{b.dt.date():b for b in load(RECENT/(name+"_daily.csv"))} for name in ["SR611","SR705"]}
    coverage = []
    for d in expected_days:
        bs = groups[d]
        assert {b.dt for b in bs} == expected_minutes(d), (d,"missing or extra minute")
        b = ds[d]
        actual = (bs[0].o,max(x.h for x in bs),min(x.l for x in bs),bs[-1].c,bs[-1].oi)
        assert actual == (b.o,b.h,b.l,b.c,b.oi), (d,"minute/day mismatch")
        assert (b.h,b.l,b.c,b.oi) == (main[d].h,main[d].l,main[d].c,main[d].oi)
        prev = previous_day(d)
        assert (ds[prev].oi > max(other[prev].oi for other in others.values()) and
                ds[prev].v > max(other[prev].v for other in others.values())), (d,"prior-day dominance")
        coverage.append(dict(trading_date=str(d), minutes=len(bs), missing=0, extra=0,
            minute_daily_match=True, supplier_main_hlc_oi_match=True, prior_volume_oi_max_among_SR611_SR701_SR705=True))
    # Validate all full warmup days too, discard only incomplete leading download day.
    good_minutes = []
    for d, bs in sorted(groups.items()):
        if {b.dt for b in bs} == expected_minutes(d): good_minutes.extend(bs)
        else: assert d < min(expected_days) and d == min(groups), (d,"unexpected warmup hole")
    quarters = []
    for i in range(0,len(good_minutes),1):
        b = good_minutes[i]
        if b.dt.minute % 15: continue
        chunk = good_minutes[i:i+15]
        assert len(chunk)==15 and chunk[-1].dt-b.dt == timedelta(minutes=14)
        quarters.append(Bar(b.dt, b.o,max(x.h for x in chunk),min(x.l for x in chunk),chunk[-1].c,
                            sum(x.v for x in chunk),chunk[-1].oi))
    save("data_verification.json",dict(coverage=coverage,scored_days=len(expected_days),
        scored_minutes=sum(x["minutes"] for x in coverage),daily_warmup=sum(b.dt.date()<START for b in daily),
        warmup_start=str(daily[0].dt),last_scored_minute=str(groups[END][-1].dt),
        margin_and_commission_are_assumptions=True))
    return good_minutes,daily,quarters


def indicator_snapshots(bars, cfg=Config()):
    """At index i, snapshot uses only bars strictly BEFORE i."""
    result = []; trs = []; n = None
    for i,b in enumerate(bars):
        if i >= max(cfg.entry,cfg.exit) and n is not None:
            result.append(dict(n=n, upper=max(x.h for x in bars[i-cfg.entry:i])+cfg.tick,
                lower=min(x.l for x in bars[i-cfg.entry:i])-cfg.tick,
                exit_long=min(x.l for x in bars[i-cfg.exit:i])-cfg.tick,
                exit_short=max(x.h for x in bars[i-cfg.exit:i])+cfg.tick))
        else: result.append(None)
        prev = bars[i-1].c if i else b.o
        tr = max(b.h-b.l,abs(b.h-prev),abs(b.l-prev)); trs.append(tr)
        if len(trs)==cfg.atr: n = sum(trs)/cfg.atr
        elif len(trs)>cfg.atr: n = ((cfg.atr-1)*n+tr)/cfg.atr
    return result


def equity(state, price, cfg):
    p = state["position"]
    return state["cash"] + ((price-p["entry_fill"])*p["direction"]*p["lots"]*cfg.multiplier if p else 0)


def size(equity_value, n, fill, cfg):
    if equity_value <= 0 or n <= 0: return 0,0
    unit = math.floor(cfg.unit_n_fraction*equity_value/(n*cfg.multiplier))
    cap = math.floor(cfg.max_margin_fraction*equity_value/(fill*cfg.multiplier*cfg.assumed_margin_rate+cfg.fee_side))
    return max(0,min(unit,cap)),unit


def open_position(state, direction, base, when, levels, cfg):
    fill = base + direction*cfg.slip_ticks*cfg.tick
    lots, desired = size(state["cash"],levels["n"],fill,cfg)
    if lots < 1: return False
    hard = fill-direction*cfg.stop_n*levels["n"]
    hard = (math.ceil(hard/cfg.tick) if direction==1 else math.floor(hard/cfg.tick))*cfg.tick
    state["position"] = dict(entry_time=str(when),direction=direction,lots=lots,desired_lots=desired,
        entry_base=base,entry_fill=fill,n=levels["n"],hard_stop=hard,
        initial_stop_risk=abs(fill-hard)*lots*cfg.multiplier,
        margin_capped=lots<desired)
    state["cash"] -= lots*cfg.fee_side
    return True


def close_position(state, base, when, reason, cfg):
    p = state["position"]; direction=p["direction"]; lots=p["lots"]
    fill = base-direction*cfg.slip_ticks*cfg.tick
    gross = (base-p["entry_base"])*direction*lots*cfg.multiplier
    slip = 2*cfg.slip_ticks*cfg.tick*lots*cfg.multiplier
    fees = 2*lots*cfg.fee_side
    net = gross-slip-fees
    state["cash"] += (fill-p["entry_fill"])*direction*lots*cfg.multiplier-lots*cfg.fee_side
    state["trades"].append(dict(p,exit_time=str(when),exit_base=base,exit_fill=fill,
        exit_reason=reason,gross_pnl=gross,slippage_cost=slip,fees=fees,net_pnl=net,
        holding_clock_hours=(when-datetime.fromisoformat(p["entry_time"])).total_seconds()/3600))
    state["position"]=None


def stop_level(p, levels, cfg):
    if p["direction"]==1:
        price=max(p["hard_stop"],levels["exit_long"])
        reason="atr_stop" if p["hard_stop"]>=levels["exit_long"] else "channel_exit"
    else:
        price=min(p["hard_stop"],levels["exit_short"])
        reason="atr_stop" if p["hard_stop"]<=levels["exit_short"] else "channel_exit"
    return price,reason


def minute_path(state, bar, levels, cfg, high_first):
    """Piecewise OHLC walk; orders/levels all known before this minute."""
    s = dict(cash=state["cash"],position=copy.copy(state["position"]),trades=[])
    if bar.v <= 0: return s
    entered=False; exited=False
    path=[bar.o,bar.h,bar.l,bar.c] if high_first else [bar.o,bar.l,bar.h,bar.c]

    def action_at(price, is_open=False):
        nonlocal entered,exited
        p=s["position"]
        if p:
            stop,reason=stop_level(p,levels,cfg)
            if (price-stop)*p["direction"]<=0:
                close_position(s,price,bar.dt,reason,cfg); exited=True
        elif not entered and not exited:
            direction=1 if price>=levels["upper"] else -1 if price<=levels["lower"] else 0
            if direction: entered=open_position(s,direction,price,bar.dt,levels,cfg)

    action_at(path[0],True)
    for left,right in zip(path,path[1:]):
        cursor=left
        while cursor!=right:
            p=s["position"]; target=None
            if p:
                stop,_=stop_level(p,levels,cfg)
                if min(cursor,right)<=stop<=max(cursor,right) and (right-cursor)*p["direction"]<0:
                    target=stop
            elif not entered and not exited:
                trigger=levels["upper"] if right>cursor else levels["lower"]
                if min(cursor,right)<=trigger<=max(cursor,right): target=trigger
            if target is None: break
            action_at(target)
            cursor=target
            if exited: break
            # Margin/zero-unit rejection: do not retry at the identical trigger.
            if not s["position"]: break
            # Protective stop is behind a new entry; next directional segment handles it.
            if (right-cursor)*s["position"]["direction"]>=0: break
        if exited: break
    return s


def outcome_signature(s):
    return (s["cash"],json.dumps(s["position"],sort_keys=True),json.dumps(s["trades"],sort_keys=True))


def run(minutes, signals, frame, cfg=Config(), path_policy="conservative"):
    snapshots=indicator_snapshots(signals,cfg)
    key=lambda b: b.dt.date() if frame=="daily" else b.dt
    lookup={key(b):snap for b,snap in zip(signals,snapshots)}
    state=dict(cash=cfg.initial_capital,position=None,trades=[])
    curve=[]; audits=[]; ambiguous=[]
    for b in minutes:
        d=trading_date(b.dt)
        if not START<=d<=END: continue
        last_scored=b
        bucket=d if frame=="daily" else b.dt.replace(minute=b.dt.minute//15*15,second=0)
        levels=lookup[bucket]
        assert levels and levels["n"]>0
        a=minute_path(state,b,levels,cfg,True)
        z=minute_path(state,b,levels,cfg,False)
        different=outcome_signature(a)!=outcome_signature(z)
        if different:
            ambiguous.append(dict(minute=str(b.dt),ohlc_high_first_equity=equity(a,b.c,cfg),
                ohlc_low_first_equity=equity(z,b.c,cfg)))
        chosen=(a if equity(a,b.c,cfg)<=equity(z,b.c,cfg) else z) if path_policy=="conservative" else (a if path_policy=="high_first" else z)
        state["cash"],state["position"]=chosen["cash"],chosen["position"]
        state["trades"].extend(chosen["trades"])
        eq=equity(state,b.c,cfg)
        curve.append(dict(minute_end=str(b.dt+timedelta(minutes=1)),trading_date=str(d),equity=eq,
            net_pnl=eq-cfg.initial_capital,cash=state["cash"],lots=state["position"]["lots"] if state["position"] else 0,
            direction=state["position"]["direction"] if state["position"] else 0))
        if not audits or audits[-1]["signal_bucket"]!=str(bucket):
            audits.append(dict(signal_bucket=str(bucket),valid_from=str(b.dt),**levels))
    assert curve
    natural=copy.deepcopy(state["trades"])
    end_position=copy.deepcopy(state["position"])
    marked=curve[-1]["equity"]-cfg.initial_capital
    if state["position"]:
        last=last_scored
        close_position(state,last.c,last.dt+timedelta(minutes=1),"endpoint_liquidation_only",cfg)
    all_trades=state["trades"]
    net=sum(t["net_pnl"] for t in all_trades)
    assert math.isclose(state["cash"]-cfg.initial_capital,net,abs_tol=1e-7)
    assert math.isclose(net,sum(t["gross_pnl"]-t["slippage_cost"]-t["fees"] for t in all_trades),abs_tol=1e-7)
    floating=(marked-sum(t["net_pnl"] for t in natural))
    if end_position:
        expected_floating=(last.c-end_position["entry_fill"])*end_position["direction"]*end_position["lots"]*cfg.multiplier-end_position["lots"]*cfg.fee_side
        assert math.isclose(floating,expected_floating,abs_tol=1e-7)
    else: assert math.isclose(marked,net,abs_tol=1e-7)
    peak=cfg.initial_capital; dd=0; dd_percent=0
    for row in curve+[{"equity":cfg.initial_capital+net}]:
        peak=max(peak,row["equity"]);dd=max(dd,peak-row["equity"]);dd_percent=max(dd_percent,(peak-row["equity"])/peak)
    daily={}
    for row in curve: daily[row["trading_date"]]=row["equity"]
    previous=cfg.initial_capital;days=[]
    for d,eq in daily.items():
        days.append(dict(trading_date=d,close_equity=eq,net_change=eq-previous));previous=eq
    summary=dict(frame=frame,config=asdict(cfg),natural_closed=stats(natural),
        endpoint_liquidated=stats(all_trades),marked_net_pnl=marked,
        floating_net_of_entry_cost=floating,endpoint_liquidation_net_pnl=net,
        return_pct=100*net/cfg.initial_capital,end_open_position=end_position,
        minute_close_max_drawdown=dd,minute_close_max_drawdown_pct=100*dd_percent,
        ambiguity_minutes=len(ambiguous),path_policy=path_policy,
        positive_equity_days=sum(x["net_change"]>0 for x in days),negative_equity_days=sum(x["net_change"]<0 for x in days),
        largest_positive_day=max(x["net_change"] for x in days),largest_negative_day=min(x["net_change"] for x in days))
    return summary,all_trades,curve,days,audits,ambiguous


def stats(trades):
    p=[t["net_pnl"] for t in trades];wins=sorted((x for x in p if x>0),reverse=True);losses=[x for x in p if x<0]
    buckets=[(-math.inf,-2000),(-2000,-1000),(-1000,0),(0,1000),(1000,2000),(2000,math.inf)]
    distribution=[dict(lower=str(lo),upper=str(hi),count=sum(lo<=x<hi for x in p if x!=0),net_pnl=sum(x for x in p if lo<=x<hi and x!=0)) for lo,hi in buckets]
    positive=sum(wins);negative=-sum(losses)
    loss_streak=max_loss_streak=0
    for x in p:
        loss_streak=loss_streak+1 if x<0 else 0
        max_loss_streak=max(max_loss_streak,loss_streak)
    gross=sum(t["gross_pnl"] for t in trades);slip=sum(t["slippage_cost"] for t in trades);fees=sum(t["fees"] for t in trades)
    assert math.isclose(sum(p),gross-slip-fees,abs_tol=1e-7)
    return dict(trades=len(p),wins=len(wins),losses=len(losses),breakeven=p.count(0),
        win_rate_pct=100*len(wins)/len(p) if p else None,net_pnl=sum(p),gross_pnl=gross,slippage_cost=slip,fees=fees,
        mean_win=statistics.mean(wins) if wins else None,mean_loss=statistics.mean(losses) if losses else None,
        payoff_ratio=statistics.mean(wins)/-statistics.mean(losses) if wins and losses else None,
        profit_factor=positive/negative if negative else None,profit_factor_infinite=bool(positive and not negative),
        mean_trade=statistics.mean(p) if p else None,median_trade=statistics.median(p) if p else None,
        best_trade=max(p) if p else None,worst_trade=min(p) if p else None,
        top1_winner_share_pct=100*sum(wins[:1])/positive if positive else None,
        top3_winner_share_pct=100*sum(wins[:3])/positive if positive else None,
        net_without_best_winner=sum(p)-sum(wins[:1]),net_without_top3_winners=sum(p)-sum(wins[:3]),
        positive_trade_total=positive,negative_trade_total=negative,
        max_lots=max((t["lots"] for t in trades),default=0),margin_capped_trades=sum(t["margin_capped"] for t in trades),
        max_consecutive_losses=max_loss_streak,
        effective_number_of_winners=positive**2/sum(x*x for x in wins) if wins else None,
        long_trades=sum(t["direction"]==1 for t in trades),short_trades=sum(t["direction"]==-1 for t in trades),
        long_net_pnl=sum(t["net_pnl"] for t in trades if t["direction"]==1),
        short_net_pnl=sum(t["net_pnl"] for t in trades if t["direction"]==-1),
        top_winners=[dict(entry=t["entry_time"],exit=t["exit_time"],net=t["net_pnl"],lots=t["lots"])
                     for t in sorted(trades,key=lambda t:t["net_pnl"],reverse=True)[:3] if t["net_pnl"]>0],
        distribution=distribution,average_clock_hours=statistics.mean(t["holding_clock_hours"] for t in trades) if p else None)


def main():
    freeze()
    minutes,daily,quarters=prepare()
    summaries=[];sensitivity=[]
    for name,signals,frame in [("daily",daily,"daily"),("15m",quarters,"15m")]:
        s,ts,curve,days,audit,ambiguous=run(minutes,signals,frame)
        summaries.append(s)
        for filename,rows in [("trades",ts),("equity",curve),("days",days),("signals",audit),("ambiguity",ambiguous)]:
            csv_save(name+"_"+filename+".csv",rows)
        for policy in ["high_first","low_first"]:
            other=run(minutes,signals,frame,path_policy=policy)[0]
            sensitivity.append(dict(frame=frame,scenario=policy,net_pnl=other["endpoint_liquidation_net_pnl"],
                trades=other["endpoint_liquidated"]["trades"],win_rate_pct=other["endpoint_liquidated"]["win_rate_pct"]))
        for slip,fee in [(0,3),(2,3),(3,3),(1,6),(1,9)]:
            other=run(minutes,signals,frame,replace(Config(),slip_ticks=slip,fee_side=fee))[0]
            sensitivity.append(dict(frame=frame,scenario=f"slip{slip}_fee{fee}",net_pnl=other["endpoint_liquidation_net_pnl"],
                trades=other["endpoint_liquidated"]["trades"],win_rate_pct=other["endpoint_liquidated"]["win_rate_pct"]))
        # Causality: truncating observations after a scored minute cannot change any earlier fill/equity.
        cut=next(i for i,b in enumerate(minutes) if trading_date(b.dt)==date(2026,10,12) and b.dt.hour==10)
        short_minutes=minutes[:cut+1]
        cutoff=minutes[cut].dt
        # run() has endpoint liquidation only after building the normal equity path.
        prefix=run(short_minutes,signals,frame)
        assert prefix[2]==curve[:len(prefix[2])], (frame,"future minute affected past equity")
        truncated=signals[:next(i for i,b in enumerate(signals) if (b.dt.date()>trading_date(cutoff) if frame=="daily" else b.dt>cutoff))+1]
        assert indicator_snapshots(truncated)==indicator_snapshots(signals)[:len(truncated)]
    save("summary.json",summaries);save("sensitivity.json",sensitivity)
    save("checks.json",dict(source_hashes=True,full_scored_minute_calendar=True,minute_daily_reconciliation=True,
        prior_day_dominance_among_three_candidates=True,prefix_equity_causality=True,
        indicator_prefix_causality=True,cash_trade_cost_reconciliation=True))
    print(json.dumps(dict(summary=summaries,sensitivity=sensitivity),ensure_ascii=False,indent=2))


if __name__=="__main__": main()
