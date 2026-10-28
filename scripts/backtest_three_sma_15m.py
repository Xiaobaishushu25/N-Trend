"""Standalone 15-minute SMA baseline; research only, no trading integration."""
import csv
import hashlib
import json
import sqlite3
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / 'data/research_market_history.sqlite'
OUT = ROOT / 'reports/three_sma_jd_15m_baseline'
ZONE = timezone(timedelta(hours=8))
CONTRACT = 'DCE.jd2610'
START = '2026-08-10 09:00:00'
END = '2026-09-07 15:00:00'
MULTIPLIER, TICK, FEE_RATE, CAPITAL = 10, 1, 0.00015, 100000

def ns(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=ZONE).timestamp()) * 1000000000

def stamp(value):
    return datetime.fromtimestamp(value / 1e9, ZONE).isoformat(sep=' ', timespec='minutes')

def signal(closes):
    if len(closes) < 20:
        return 0
    a = sum(closes[-5:]) / 5
    b = sum(closes[-10:]) / 10
    c = sum(closes[-20:]) / 20
    return 1 if a > b > c else -1 if a < b < c else 0

def run(minutes, bars, slip_ticks=1):
    # Bars are (end_ns, close); only completed bars are allowed to set targets.
    closes, targets = deque(maxlen=20), {}
    for end, close in bars:
        closes.append(close)
        targets[end] = signal(list(closes)) if len(closes) == 20 else None
    events = iter((end, target) for end, target in targets.items() if end > ns(START) and target is not None)
    event = next(events, None)
    position = 0
    pending = None
    entry = None
    cash = 0.0
    trades, equity = [], []
    last = None
    for t, op, close, volume in minutes:
        if t >= ns(END):
            break
        if t < ns(START):
            continue
        while event is not None and event[0] <= t:
            pending = event[1]
            event = next(events, None)
        if pending is not None and volume > 0:
            target, pending = pending, None
            if target != position:
                if position:
                    fill = op - position * TICK * slip_ticks
                    fee = fill * MULTIPLIER * FEE_RATE
                    pnl = position * (fill - entry['price']) * MULTIPLIER - entry['fee'] - fee
                    cash += position * (fill - entry['price']) * MULTIPLIER - fee
                    trades.append(dict(entry_time=entry['time'], exit_time=stamp(t), direction=position,
                                       entry_price=entry['price'], exit_price=fill, net_pnl=round(pnl, 4), reason='signal'))
                position = target
                if position:
                    fill = op + position * TICK * slip_ticks
                    fee = fill * MULTIPLIER * FEE_RATE
                    cash -= fee
                    entry = dict(time=stamp(t), price=fill, fee=fee)
                else:
                    entry = None
        equity.append(dict(time=stamp(t), equity=round(cash + (position * (close-entry['price']) * MULTIPLIER if position else 0), 4)))
        last = (t, close)
    if position and last:
        t, close = last
        fill = close - position * TICK * slip_ticks
        fee = fill * MULTIPLIER * FEE_RATE
        pnl = position * (fill-entry['price']) * MULTIPLIER - entry['fee'] - fee
        cash += position * (fill-entry['price']) * MULTIPLIER - fee
        trades.append(dict(entry_time=entry['time'], exit_time=stamp(t+60_000_000_000), direction=position,
                           entry_price=entry['price'], exit_price=fill, net_pnl=round(pnl, 4), reason='endpoint'))
        equity[-1]['equity'] = round(cash, 4)
    peak = 0
    dd = 0
    for row in equity:
        peak = max(peak, row['equity'])
        dd = max(dd, peak-row['equity'])
    wins = [t['net_pnl'] for t in trades if t['net_pnl'] > 0]
    losses = [t['net_pnl'] for t in trades if t['net_pnl'] < 0]
    stats = dict(trades=len(trades), wins=len(wins), win_rate=round(len(wins)/len(trades), 4) if trades else None,
                 net_pnl=round(cash, 2), return_on_100k=round(cash/CAPITAL, 6), max_drawdown=round(dd, 2),
                 profit_factor=round(sum(wins)/-sum(losses), 3) if losses else None,
                 initial_capital=CAPITAL, slip_ticks_per_side=slip_ticks)
    return stats, trades, equity

def save_csv(path, rows):
    if rows:
        with path.open('w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys()); w.writeheader(); w.writerows(rows)

def main():
    db = sqlite3.connect(f'file:{DB.as_posix()}?mode=ro', uri=True)
    # Load warm-up history without carrying a position into the evaluation.
    bars = db.execute('select end_ns,close from bars where contract=? and period_seconds=900 and complete=1 and end_ns<=? order by end_ns', (CONTRACT, ns(END))).fetchall()
    minutes = db.execute('select start_ns,open,close,volume from bars where contract=? and period_seconds=60 and start_ns>=? and start_ns<? order by start_ns', (CONTRACT, ns(START), ns(END))).fetchall()
    db.close()
    if not minutes or len([b for b in bars if b[0] <= ns(START)]) < 20:
        raise ValueError('Missing minute data or 20 warm-up bars')
    stats, trades, equity = run(minutes, bars)
    stress = [run(minutes, bars, slip)[0] for slip in (0, 1, 2)]
    OUT.mkdir(parents=True, exist_ok=True)
    save_csv(OUT/'trades.csv', trades); save_csv(OUT/'equity.csv', equity)
    # Hash exactly the ordered source rows, so later database changes are detectable.
    digest = hashlib.sha256(json.dumps([bars, minutes], separators=(',', ':')).encode()).hexdigest()
    result = dict(contract=CONTRACT, start=START, end_exclusive=END, source_rows_sha256=digest,
                  minute_count=len(minutes), complete_15m_bars_in_window=sum(ns(START)<b[0]<=ns(END) for b in bars),
                  stats=stats, cost_stress=stress)
    (OUT/'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
