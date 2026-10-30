"""Frozen offline score ablations, fill audit and causal per-product occupancy.

Consumes an existing score-scan; never writes to the source DB, score rules or broker.
Full-history top-quintile cuts are development diagnostics, not forward signals.
"""
import argparse
import bisect
import collections
import copy
import datetime as dt
import hashlib
import json
import math
import pathlib
import random
import re
import sqlite3
import statistics

from reprice_signal_costs import TradingDates, fee_for, reprice
from audit_score_history import crosscheck


VERSIONS = {
    'v1': 'v1综合原版', 'v1_no_trend': 'v1移除趋势',
    'v1_no_a': 'v1移除A', 'v1_no_b': 'v1移除B',
    'v1_no_space': 'v1形态', 'space_only': '仅空间',
    'v2': 'v2候选原版', 'v2_no_a_adjust': 'v2撤销A调整',
    'v2_no_b_adjust': 'v2撤销B调整', 'v2_no_levels': 'v2移除关键位权重',
}
SCENARIOS = ('s1_1tick', 's1_2tick', 'fixed_1r_1tick', 'fixed_1r_2tick')
TERMINAL = {'closed', 'invalidated', 'invalid_target', 'unfilled', 'untriggered',
            'filtered', 'risk_budget_cancelled_at_entry'}
INF = '9999-12-31 23:59:59'


def check(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    h = hashlib.sha256()
    with pathlib.Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, value):
    with path.open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write('\n')


def write_rows(path, rows):
    with path.open('x', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')


def mean(xs):
    return statistics.mean(xs) if xs else None


def score_variants(raw, rules, candidate):
    """Drop a flat contribution, normalize remaining known weights. No refitting."""
    q, s = raw['quality'], raw['scores']
    t, a, b, space = (q[k] for k in ('trend_score', 'a_score', 'b_score', 'space_score'))
    mw, qw = rules['morphology_weights'], rules['quality_weights']
    morph = sum(w * x for w, x in zip(mw, (t, a, b)))
    original = qw[0] * morph + qw[1] * space
    check(math.isclose(original, s['quality_v1'], abs_tol=1e-8), 'v1 formula drift')
    v = dict(v1=original, v1_no_space=morph, space_only=space)
    for key, x, w in zip(('trend', 'a', 'b'), (t, a, b), mw):
        v['v1_no_' + key] = (original - qw[0] * w * x) / (1 - qw[0] * w)
    cm, cq = candidate['morphology_weights'], candidate['quality_weights']
    m2 = sum(w * x for w, x in zip(cm, (t, s['a_v2'], s['b_v2'])))
    known = cq[0] + cq[1] + (cq[2] if s['levels'] is not None else 0)
    v['v2'] = (cq[0] * m2 + cq[1] * space + cq[2] * (s['levels'] or 0)) / known
    check(math.isclose(v['v2'], s['quality_v2'], abs_tol=1e-8), 'v2 formula drift')
    v['v2_no_a_adjust'] = v['v2'] + cq[0] * cm[1] * (a - s['a_v2']) / known
    v['v2_no_b_adjust'] = v['v2'] + cq[0] * cm[2] * (b - s['b_v2']) / known
    v['v2_no_levels'] = (cq[0] * m2 + cq[1] * space) / (cq[0] + cq[1])
    check(set(v) == set(VERSIONS) and all(math.isfinite(x) for x in v.values()), 'invalid scores')
    return v


class Market:
    def __init__(self, db):
        conn = sqlite3.connect(pathlib.Path(db).resolve().as_uri() + '?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        self.bars = collections.defaultdict(list)
        for r in conn.execute("SELECT * FROM klines WHERE timeframe='5m' ORDER BY symbol,ts"):
            self.bars[r['symbol']].append(dict(r))
        self.rolls = collections.defaultdict(list)
        for r in conn.execute('SELECT * FROM rollovers ORDER BY symbol,ts'):
            self.rolls[r['symbol']].append(dict(r))
        conn.close()
        self.times = {s: [b['ts'] for b in bars] for s, bars in self.bars.items()}
        self.calendar = TradingDates({s: dict(bars=[dict(datetime=b['ts']) for b in bars])
                                     for s, bars in self.bars.items()})

    def quote(self, symbol, before):
        i = bisect.bisect_left(self.times[symbol], before) - 1
        while i >= 0:
            b = self.bars[symbol][i]
            if valid_bar(b):
                return b
            i -= 1
        return None

    def window(self, symbol, start, end):
        times = self.times[symbol]
        return self.bars[symbol][bisect.bisect_left(times, start):bisect.bisect_right(times, end)]


def valid_bar(b):
    return (all(math.isfinite(b[k]) and b[k] > 0 for k in ('open', 'high', 'low', 'close'))
            and b['low'] <= min(b['open'], b['close']) <= max(b['open'], b['close']) <= b['high'])


def product_rule(row, profile):
    return profile['products'][re.sub(r'\d+$', '', row['symbol']).upper()]


def fees(row, price, ts, calendar, profile):
    """One-lot opening and closing charges. SR override allocated 50/50 in cash time."""
    spec = product_rule(row, profile)
    if 'roundtrip_fixed_yuan_per_lot' in spec:
        half = spec['roundtrip_fixed_yuan_per_lot'] / 2
        return half, half
    opened = calendar.day(row['entry_ts'])
    closed = calendar.day(ts)
    return (fee_for(spec['open'], row['fill'], 1, row['multiplier']),
            fee_for(spec['close_today' if opened == closed else 'close_yesterday'],
                    price, 1, row['multiplier']))


def conditions(raw, profile, slip):
    e, m, tick = raw['event'], raw['multiplier'], raw['tick']
    sign = 1 if e['direction'] == 'up' else -1
    entry = e['entry'] + sign * slip * tick
    risk = (entry - e['stop']) * sign * m
    target = e['target'] - sign * slip * tick
    spec = product_rule(raw, profile)
    commission = spec.get('roundtrip_fixed_yuan_per_lot')
    if commission is None:
        commission = fee_for(spec['open'], entry, 1, m) + max(
            fee_for(spec[k], target, 1, m) for k in ('close_today', 'close_yesterday'))
    return dict(planned_risk_yuan_per_lot=risk, planned_commission_r=commission / risk,
                net_space_to_s1_r=((target - entry) * sign * m - commission) / risk,
                levels_partial=raw['scores']['levels_partial'], morphology_v1=raw['scores']['morphology_v1'])


def prepare(raw, scenario, scores, market, profile):
    wrapped = reprice(dict(symbol=raw['symbol'], direction=raw['event']['direction'],
                           multiplier=raw['multiplier'], result=raw['evaluations'][scenario]),
                      profile, market.calendar)
    e = wrapped['result']['evaluation']
    row = dict(id=raw['id'], candidate_key=raw['candidate_key'], symbol=raw['symbol'],
               warning_ts=raw['event']['warning_ts'], direction=raw['event']['direction'],
               multiplier=raw['multiplier'], tick=raw['tick'], stop=raw['event']['stop'], target=raw['event']['target'],
               signal_risk_ticks=raw['event']['risk'] / raw['tick'], scores=scores,
               scenario=scenario, **e)
    row['fills'] = wrapped['result']['fills']
    row['disagreement'] = wrapped['result']['disagreement']
    row['conditions'] = conditions(raw, profile, 2 if scenario.endswith('2tick') else 1)
    row['risk_yuan_per_lot'] = e['risk'] * row['multiplier'] if e['fill'] is not None else None
    row['commission'] = wrapped.get('cost_repricing', {}).get('commission')
    row['commission_r'] = (row['commission'] / row['risk_yuan_per_lot']
                           if row['commission'] is not None else None)
    row['release_ts'], row['uncertain_from'] = release(row)
    return row


def release(row):
    status, end = row['status'], row['exit_ts']
    if status in TERMINAL and end:
        return end, None
    if status == 'rollover' and row['entry_ts'] is None:
        return end, None  # Cancel a known unentered order at the mapping boundary.
    if status == 'ambiguous' and row.get('disagreement'):
        paths = [row['disagreement'][k] for k in ('open_high_low_close', 'open_low_high_close')]
        if all(p['status'] in TERMINAL and p['exit_ts'] for p in paths):
            return max(p['exit_ts'] for p in paths), None
    if status in ('unfinished', 'pending'):
        return INF, None  # Still occupied at archive tail.
    return INF, end or row['warning_ts']  # Cannot infer subsequent executable orders.


def audit_fill(row, market):
    """Recompute every priced fill ledger; never cap stop losses or delete outliers."""
    fs = row['fills']
    if row['entry_ts'] is not None:
        check(fs and fs[0]['offset'] == 'open', 'missing entry ledger')
        check(row['entry_ts'] > row['warning_ts'], 'entry before warning was available')
        sign = 1 if row['direction'] == 'up' else -1
        check(math.isclose((row['fill'] - row['stop']) * sign * row['multiplier'],
                           row['risk_yuan_per_lot'], abs_tol=1e-7), 'risk denominator mismatch')
    if row['status'] != 'closed':
        return None
    check(fs[0]['volume'] == 1 and sum(f['volume'] for f in fs[1:]) == 1, 'not a one-lot ledger')
    sign = 1 if row['direction'] == 'up' else -1
    gross = sum(sign * (f['price'] - fs[0]['price']) * f['volume'] for f in fs[1:]) * row['multiplier']
    net = gross - row['commission']
    check(math.isclose(net, row['net_amount'], abs_tol=1e-6), 'cash mismatch')
    check(math.isclose(net / row['risk_yuan_per_lot'], row['net_r'], abs_tol=1e-8), 'R mismatch')
    slip = row['tick'] * (2 if row['scenario'].endswith('2tick') else 1)
    conservative_targets = []
    for f in fs:
        bars = market.window(row['symbol'], f['ts'], f['ts'])
        check(len(bars) == 1, 'fill timestamp has no unique quote')
        b = bars[0]
        observed = f['price'] - sign * slip if f['offset'] == 'open' else f['price'] + sign * slip
        if not b['low'] - 1e-6 <= observed <= b['high'] + 1e-6:
            expected = row['target'] if f['reason'] == 'target_s1' else row['fill'] + sign * row['risk']
            # Existing replay deliberately gives no price improvement when an
            # opening gap jumps over a resting target. Preserve this frozen,
            # conservative convention, but disclose it rather than claiming
            # that the target price was observed inside the current bar.
            check(f['offset'] == 'close' and f['reason'] in ('target_s1', 'target_1r')
                  and math.isclose(observed, expected, abs_tol=1e-6)
                  and (b['open'] - observed) * sign >= 0, 'fill outside OHLC after slippage')
            conservative_targets.append(dict(ts=f['ts'], frozen_target=observed, observed_open=b['open']))
    return dict(gross_yuan=gross, commission_yuan=row['commission'], net_yuan=net,
                risk_yuan=row['risk_yuan_per_lot'], net_r=net / row['risk_yuan_per_lot'],
                conservative_target_gap_fills=conservative_targets)


def audit_detail(raw, row, market, reconciliation, profile):
    end = row['exit_ts'] or market.times[row['symbol']][-1]
    bars = market.window(row['symbol'], row['warning_ts'], end)
    gaps = []
    for a, b in zip(bars, bars[1:]):
        minutes = (dt.datetime.fromisoformat(b['ts']) - dt.datetime.fromisoformat(a['ts'])).total_seconds() / 60
        if minutes > 5:
            gaps.append(dict(before=a['ts'], after=b['ts'], minutes=minutes,
                             classification='unverified_session_break_or_missing_data'))
    exit_bar = next((b for b in bars if b['ts'] == row['exit_ts']), None)
    sign = 1 if row['direction'] == 'up' else -1
    gap_stop = bool(exit_bar and row['status'] == 'closed' and row['exit_reason'] == 'stop'
                    and (exit_bar['open'] - row['stop']) * sign < 0)
    return dict(id=row['id'], symbol=row['symbol'], warning_ts=row['warning_ts'],
                frozen_event=raw['event'], original_result_before_fee=raw['evaluations'][row['scenario']],
                priced_result=row, reconciliation=reconciliation,
                observed_open_beyond_stop=gap_stop, invalid_ohlc=[b['ts'] for b in bars if not valid_bar(b)],
                gaps=gaps, bars=bars,
                rollovers=[r for r in market.rolls[row['symbol']] if row['warning_ts'] < r['ts'] <= end],
                tail_mark=(period_account([row], '0001-01-01 00:00:00', INF, market, profile)
                           if row['status'] == 'unfinished' else None),
                actual_contract_mapping_verified=False,
                conclusion=('OHLC opening beyond stop reproduces fill; actual-contract jump versus continuous-series/data issue unresolved'
                            if gap_stop else 'ledger checked; unknown results retained; no causal module attribution'))


def top_cut(rows, version):
    xs = sorted(r['scores'][version] for r in rows)
    return xs[-max(1, len(xs) // 5)] if xs else None


def replay(rows, version, threshold, budget=None):
    """Select at warning, reserve through the actual terminal event. Full-history warmup."""
    active, ledger = {}, []
    for source in sorted(rows, key=lambda r: (r['warning_ts'], -r['scores'][version], r['candidate_key'])):
        if threshold is None or source['scores'][version] < threshold:
            continue
        row = copy.copy(source)
        row['risk_budget_yuan'] = budget
        row['selection_score'] = source['scores'][version]
        row['lots'] = 1
        block = active.get(row['symbol'])
        if block and block['release_ts'] > row['warning_ts']:
            row['decision'] = ('unconfirmed_segment' if block['uncertain_from']
                               and block['uncertain_from'] <= row['warning_ts'] else 'skipped_occupied')
            row['blocked_by'] = block['id']
            ledger.append(row)
            continue
        planned_risk = row['conditions']['planned_risk_yuan_per_lot']
        if budget is not None:
            row['lots'] = math.floor(budget / planned_risk)
            if row['lots'] < 1:
                row['decision'] = 'skipped_budget_at_warning'
                ledger.append(row)
                continue
        row['decision'] = 'accepted'
        if budget is not None and row['entry_ts']:
            # Actual proposed fill is observable at this event, not at warning.
            # Reduce the warned quantity for gaps; never increase it using later data.
            row['lots'] = min(row['lots'], math.floor(budget / row['risk_yuan_per_lot']))
            if row['lots'] == 0:
                row = copy.copy(row)
                row.update(status='risk_budget_cancelled_at_entry', release_ts=row['entry_ts'],
                           exit_ts=row['entry_ts'], entry_ts=None, fill=None, fills=[],
                           net_r=None, net_amount=None, commission=None, risk_yuan_per_lot=None,
                           uncertain_from=None)
        active[row['symbol']] = row
        ledger.append(row)
    accepted = [r for r in ledger if r['decision'] == 'accepted']
    for symbol in {r['symbol'] for r in accepted}:
        previous = None
        for row in (r for r in accepted if r['symbol'] == symbol):
            check(previous is None or previous['release_ts'] <= row['warning_ts'], 'overlapping product occupancy')
            previous = row
    return ledger


def drawdown(rows, amount):
    settled = collections.defaultdict(float)
    for r in rows:
        settled[r['exit_ts']] += r[amount] * (r.get('lots', 1) if amount == 'net_amount' else 1)
    equity = peak = worst = 0
    for _, cash in sorted(settled.items()):
        equity += cash
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def metrics(rows):
    # Caller supplies accepted orders, or all independently evaluated candidates.
    closed = [r for r in rows if r['status'] == 'closed' and r['net_r'] is not None]
    values = [r['net_r'] for r in closed]
    trimmed = sorted(closed, key=lambda r: (-r['net_r'], r['candidate_key']))[5:]
    return dict(signals=len(rows), statuses=dict(collections.Counter(r['status'] for r in rows)),
                confirmed_entries=sum(r['entry_ts'] is not None for r in rows), completed=len(closed),
                invalidated=sum(r['status'] in ('invalidated', 'invalid_target') for r in rows),
                unfilled=sum(r['status'] in ('unfilled', 'untriggered', 'pending') for r in rows),
                ambiguous=sum(r['status'] == 'ambiguous' for r in rows),
                tail_open=sum(r['status'] == 'unfinished' for r in rows),
                completed_fraction=len(closed) / len(rows) if rows else None,
                wins=sum(x > 0 for x in values), losses=sum(x < 0 for x in values),
                breakevens=sum(x == 0 for x in values),
                net_win_rate=mean([int(x > 0) for x in values]), mean_net_r=mean(values), total_net_r=sum(values),
                mean_win_r=mean([x for x in values if x > 0]), mean_loss_r=mean([x for x in values if x < 0]),
                net_yuan=sum(r['net_amount'] * r.get('lots', 1) for r in closed),
                commission_yuan=sum(r['commission'] * r.get('lots', 1) for r in closed),
                mean_risk_yuan_per_lot=mean([r['risk_yuan_per_lot'] for r in closed]),
                mean_commission_r=mean([r['commission_r'] for r in closed]),
                mean_roundtrip_slippage_r=mean([2 * r['tick'] * r['multiplier'] *
                                              (2 if r['scenario'].endswith('2tick') else 1) /
                                              r['risk_yuan_per_lot'] for r in closed]),
                closed_drawdown_yuan=drawdown(closed, 'net_amount'), closed_drawdown_r=drawdown(closed, 'net_r'),
                without_best5=dict(completed=len(trimmed), mean_net_r=mean([r['net_r'] for r in trimmed]),
                                   net_yuan=sum(r['net_amount'] * r.get('lots', 1) for r in trimmed)),
                symbol_counts=dict(collections.Counter(r['symbol'] for r in rows)),
                warning_date_counts=dict(sorted(collections.Counter(r['warning_ts'][:10] for r in rows).items())))


def accepted(ledger):
    return [r for r in ledger if r['decision'] == 'accepted']


def maximum_positions(rows, start, end):
    # Bar timestamps do not order fills across products: entries before exits
    # gives a conservative bound for simultaneous positions/lots.
    confirmed, possible = [], []
    for r in rows:
        entry, exit_ts = r['entry_ts'], r['exit_ts']
        qty = r.get('lots', 1)
        if entry and entry < end:
            a, b = max(start, entry), min(end, exit_ts or end)
            if a <= b:
                confirmed.append((a, b, qty))
        if r.get('uncertain_from') and r['uncertain_from'] < end:
            # A break in evidence does not close a position. At most one
            # position per product can remain or arise in the unknown segment.
            possible.append((max(start, entry or r['uncertain_from']), end, qty))
        elif entry and entry < end:
            a, b = max(start, entry), min(end, exit_ts or end)
            if a <= b:
                possible.append((a, b, qty))
        elif r['status'] == 'ambiguous' and start <= exit_ts < end:
            possible.append((exit_ts, exit_ts, qty))

    def peak(intervals):
        events = collections.defaultdict(lambda: [0, 0, 0, 0])
        for a, b, qty in intervals:
            events[a][0] += 1
            events[a][1] += qty
            events[b][2] += 1
            events[b][3] += qty
        n = lots = pn = pq = 0
        for _, (opened, oq, closed, cq) in sorted(events.items()):
            n += opened
            lots += oq
            pn, pq = max(pn, n), max(pq, lots)
            n -= closed
            lots -= cq
        return pn, pq

    known_n, known_qty = peak(confirmed)
    possible_n, possible_qty = peak(possible)
    unknown = sum(r.get('uncertain_from') is not None and r['uncertain_from'] < end for r in rows)
    integer_unknown = unknown and any(r.get('risk_budget_yuan') is not None for r in rows)
    return dict(confirmed_interval_positions_bar_order_upper_bound=known_n,
                confirmed_interval_lots_bar_order_upper_bound=known_qty,
                possible_positions_upper_bound=possible_n,
                possible_lots_upper_bound=None if integer_unknown else possible_qty,
                unknown_exposure_segments=unknown,
                note='Unknown product segments may retain one position; integer lots after an unknown future entry cannot be bounded by the known lot count.')


def period_account(rows, start, end, market, profile):
    """Cash by fill date and terminal marks; independent from warning-date cohort."""
    realized = [r for r in rows if r['status'] == 'closed' and start <= r['exit_ts'] < end]
    flows, positions, unknown = [], [], []
    for r in rows:
        if r['warning_ts'] >= end:
            continue
        qty = r.get('lots', 1)
        if r.get('uncertain_from') and r['uncertain_from'] < end:
            unknown.append(r['id'])
        entry = r['entry_ts']
        if not entry or entry >= end:
            if r['status'] == 'ambiguous' and r['exit_ts'] < end:
                unknown.append(r['id'])
            continue
        spec = product_rule(r, profile)
        opening_fee = (spec['roundtrip_fixed_yuan_per_lot'] / 2
                       if 'roundtrip_fixed_yuan_per_lot' in spec else fee_for(spec['open'], r['fill'], 1, r['multiplier']))
        if start <= entry < end:
            flows.append(dict(id=r['id'], ts=entry, kind='opening_commission', yuan=-opening_fee * qty))
        if r['status'] == 'closed' and r['exit_ts'] < end:
            if start <= r['exit_ts']:
                flows.append(dict(id=r['id'], ts=r['exit_ts'], kind='closing_cash',
                                  yuan=(r['net_amount'] + opening_fee) * qty))
            continue
        if r['exit_ts'] and r['exit_ts'] < end:
            # Missing data / rollover / divergence terminated the evidence,
            # not the real position. Do not mark it as an ordinary tail holding.
            unknown.append(r['id'])
            continue
        quote = market.quote(r['symbol'], end)
        if quote is None or quote['ts'] < entry:
            unknown.append(r['id'])
            continue
        sign = 1 if r['direction'] == 'up' else -1
        slip = r['tick'] * (2 if r['scenario'].endswith('2tick') else 1)
        liquidation = quote['close'] - sign * slip
        try:
            _, close_fee = fees(r, liquidation, quote['ts'], market.calendar, profile)
        except ValueError:
            unknown.append(r['id'])
            continue
        gross = (quote['close'] - r['fill']) * sign * r['multiplier'] * qty
        exit_cost = (slip * r['multiplier'] + close_fee) * qty
        positions.append(dict(id=r['id'], symbol=r['symbol'], lots=qty, entry_ts=entry,
                              valuation_ts=quote['ts'], close=quote['close'], gross_unrealized_yuan=gross,
                              estimated_exit_cost_yuan=exit_cost, opening_commission_yuan=opening_fee * qty,
                              unrealized_after_entry_exit_cost_yuan=gross - exit_cost - opening_fee * qty))
    return dict(start=start, end_exclusive=end, realized_by_close_date=metrics(realized),
                known_cash_flow_yuan=sum(f['yuan'] for f in flows), cash_flows=sorted(flows, key=lambda f: (f['ts'], f['id'])),
                ending_positions=positions, gross_unrealized_yuan=sum(p['gross_unrealized_yuan'] for p in positions),
                estimated_exit_cost_yuan=sum(p['estimated_exit_cost_yuan'] for p in positions),
                unrealized_after_entry_exit_cost_yuan=sum(p['unrealized_after_entry_exit_cost_yuan'] for p in positions),
                unknown_result_ids=sorted(set(unknown)), equity_confirmed=not unknown,
                maximum_concurrent=maximum_positions(rows, start, end),
                margin_estimate=None, margin_missing_inputs=['dated_actual_contract_mapping', 'contract_margin_rates',
                                                            'available_capital', 'portfolio_and_product_risk_limits'])


def ledger_report(ledger, start, end, market, profile):
    orders = accepted(ledger)
    month = [r for r in ledger if start <= r['warning_ts'] < end]
    return dict(coverage=coverage_metrics(ledger), all_history=dict(decisions=dict(collections.Counter(r['decision'] for r in ledger)),
                                 accepted_orders=metrics(orders)),
                last_month_warning_cohort=dict(decisions=dict(collections.Counter(r['decision'] for r in month)),
                                               accepted_orders=metrics(accepted(month))),
                all_history_cash=period_account(orders, '0001-01-01 00:00:00', end, market, profile),
                last_month_cash=period_account(orders, start, end, market, profile))


def coverage_metrics(ledger):
    rows = accepted(ledger)
    return dict(eligible_signals=len(ledger), accepted_orders=len(rows),
                decisions=dict(collections.Counter(r['decision'] for r in ledger)),
                execution_fully_confirmed=(not any(r['decision'] == 'unconfirmed_segment' for r in ledger)
                                           and not any(r['status'] in ('ambiguous', 'missing_data', 'rollover') for r in rows)),
                unknown_result_orders=[r['id'] for r in rows if r['status'] in ('ambiguous', 'missing_data', 'rollover')],
                unconfirmed_segment_signals=[r['id'] for r in ledger if r['decision'] == 'unconfirmed_segment'])


def block_difference(left, right, days, draws=500):
    """Fixed selections, joint-market five-observed-date blocks; development only."""
    block = {day: i // 5 for i, day in enumerate(sorted(days))}
    buckets = [[[], []] for _ in range(max(block.values(), default=-1) + 1)]
    for i, rows in enumerate((left, right)):
        for r in rows:
            if r['status'] == 'closed':
                buckets[block[r['warning_ts'][:10]]][i].append(r['net_r'])
    if not buckets:
        return dict(lower=None, upper=None, valid_draws=0)
    rng, diffs = random.Random(20261030), []
    for _ in range(draws):
        a, b = [], []
        for _ in buckets:
            sample = rng.choice(buckets)
            a.extend(sample[0])
            b.extend(sample[1])
        if a and b:
            diffs.append(statistics.mean(b) - statistics.mean(a))
    diffs.sort()
    return dict(lower=diffs[int((len(diffs) - 1) * .025)] if diffs else None,
                upper=diffs[int((len(diffs) - 1) * .975)] if diffs else None,
                valid_draws=len(diffs), draws=draws, blocks=len(buckets),
                interpretation='candidate minus frozen v1 top20; fixed accepted orders, no replay of artificial resampled chronology; development only')


def rolling_diagnostics(rows):
    """No fitted parameters: previous scores set a cut, boundary outcomes isolated."""
    results = []
    months = sorted({r['warning_ts'][:7] for r in rows})
    for i, month in enumerate(months[1:], 1):
        start = month + '-01 00:00:00'
        d = dt.datetime.fromisoformat(start)
        end = (d.replace(year=d.year + 1, month=1) if d.month == 12 else d.replace(month=d.month + 1)).isoformat(' ')
        train = [r for r in rows if r['warning_ts'] < start]
        test = [r for r in rows if start <= r['warning_ts'] < end]
        for version in VERSIONS:
            cut = top_cut(train, version)
            chosen = [r for r in test if cut is not None and r['scores'][version] >= cut]
            # Cross-boundary labels are unpriced in this diagnostic, not false losses.
            isolated = []
            for r in chosen:
                x = copy.copy(r)
                if r['release_ts'] >= end:
                    x.update(status='boundary_isolated', net_r=None, net_amount=None)
                isolated.append(x)
            results.append(dict(month=month, version=version, train_score_count=len(train), threshold=cut,
                                train_outcomes_crossing_boundary=sum(r['release_ts'] >= start for r in train),
                                test=metrics(isolated), test_crossing_boundary=sum(r['release_ts'] >= end for r in chosen)))
    return dict(method='Expanding previous-warning-score thresholds, no return fitting; test labels crossing next month isolated. Independent signal diagnostic, not portfolio or unseen validation.',
                rows=results)


def fmt(x, digits=3):
    return '未知' if x is None else f'{x:.{digits}f}'


def render_report(summary, output):
    lines = ['# 评分与交易收益优化：离线执行结果', '',
             '版本：score-execution-v1。正式评分、实盘准入和退出策略未改；全部既有历史为开发资料。', '',
             '## 基线与审计', '',
             f"共同可评分候选{summary['common_candidates']}条；4个情景的已完成成交均可复算金额、R分母和手数。",
             f"OHLC成交核查单列{summary['conservative_target_gap_count']}笔情景记录：有利跳空越过目标时，旧回放仍按目标价扣滑点，不计开盘价格改善；这是冻结的保守约定，并非声称该价格在本根OHLC内出现。",
             f"逐笔异常/未完成审计{summary['audit_count']}条，见audit.jsonl；无法核实实际合约映射和历史休市/缺数归因。",
             'BU0等开盘越过止损的结果保留，不截断为−1R；连续合约跳变不能直接认定是真实合约跳空。', '',
             '## v1≥70 可执行持仓基线', '',
             '|情景/核算|全历史接受/完成|近月接受/完成|近月净元|近月平均净R|近月平仓回撤元|',
             '|---|---:|---:|---:|---:|---:|']
    for scenario, modes in summary['baseline'].items():
        for mode, report in modes.items():
            a, b = report['all_history']['accepted_orders'], report['last_month_warning_cohort']['accepted_orders']
            lines.append(f"|{scenario}/{mode}|{a['signals']}/{a['completed']}|{b['signals']}/{b['completed']}|{fmt(b['net_yuan'],2)}|{fmt(b['mean_net_r'])}|{fmt(b['closed_drawdown_yuan'],2)}|")
    lines.extend(['', '近月按预警归属，先用全历史预热。按平仓日期/现金流的期间结果、期末持仓、估值时间及未知结果另见summary.json的cash字段和逐笔账本。',
                  '表中盈亏只汇总可确认已完成交易。受历史持仓未知区段影响，这些数值不是完整策略收益或保守收益下界；无法确认的后续信号可能盈利也可能亏损。',
                  '一手账本的累计/平均R同时作为等初始风险的标准化结果。整数手数按冻结预算计算，未包含资金/保证金约束，不称为账户收益。', '',
                  '## 10组固定消融：S1、每侧1 tick、一手', '',
                  '|集合/版本|前20%候选|独立完成/平均净R|占用后完成/平均净R|占用后净元|2tick均R|去最好5笔均R|相对v1差异区间|',
                  '|---|---:|---:|---:|---:|---:|---:|---|'])
    for subset, data in summary['ablations'].items():
        for version, case in data.items():
            main = case['scenarios']['s1_1tick']
            a, b = main['independent'], main['executable']
            stress = case['scenarios']['s1_2tick']['executable']
            ci = main['v1_difference_interval']
            lines.append(f"|{subset}/{VERSIONS[version]}|{case['selected_signals']}|{a['completed']}/{fmt(a['mean_net_r'])}|{b['completed']}/{fmt(b['mean_net_r'])}|{fmt(b['net_yuan'],2)}|{fmt(stress['mean_net_r'])}|{fmt(b['without_best5']['mean_net_r'])}|[{fmt(ci['lower'])}, {fmt(ci['upper'])}]|")
    lines.extend(['', '前20%以各集合全部预警分数确定，边界同分全部保留；它使用开发期分数分布，不能充当前瞻门槛。v1≥70是独立基线。',
                  '移除v1分项时先展开综合贡献再归一剩余权重；v2只撤销指定调整，其他冻结参数不动。', '',
                  '## 决策与边界', '',
                  '当前证据不支持修改正式评分，也不新增风险tick或成本硬门槛。空间、形态与执行条件分列在候选表中；A/B新调整继续保留为研究观测。',
                  '消融均为开发证据。日期块区间跨零、2 tick成本转负、最好几笔或单一品种改变结果时均不能宣布稳定改善。',
                  '路径两分支都明确终结时才释放占用；否则从分歧/数据中断起该品种后续段标为不可确认。未知交易现金和权益不按零处理，也不宣称完整可执行收益。',
                  '最大同时持仓按同时间先开后平给上界，未知品种区段按最多一笔持续持有计入可能持仓上界；整数手数的未知后续手数不强行给界。保证金还缺逐日合约映射、保证金率、资金及组合风险上限。',
                  '手续费为旧快照；SR往返费用为期间现金流按开平各半分摊。浮盈亏按品种最后有效5分钟收盘估值并扣预计平仓滑点及费用。',
                  '所有超过5分钟的行情间隔仅列证据，不把休市直接当缺数，也不把未确认换月直接当真实价格风险。',
                  '滚动诊断仅用之前的分数设门槛，跨边界标签隔离；它不是未见验证。',
                  '已发现本地实际合约长历史库；若传入--history，inventory和cross-source-discrepancies记录覆盖与全库交叉核查。长历史尚未重扫，修订版映射的当时可用性未核实。',
                  '新增冻结后信号尚未采集，前瞻登记处于未启动状态；现有历史均不能冒充前瞻。', '',
                  '复现输入和代码哈希见protocol.json；逐笔订单见ledgers/，独立候选与交易条件见candidates.jsonl。'])
    (output / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def render_audit(audits, output):
    lines = ['# 逐笔异常与未知结果核查', '',
             'S1、每侧1 tick及冻结费率。超过1.5R只用于审计筛选，未删交易；完整5分钟OHLC、成交、原始结果、费率重估结果及R分母在audit.jsonl。', '',
             '|ID|品种|预警|状态|风险元/手|净元|净R|开盘越过止损|',
             '|---|---|---|---|---:|---:|---:|---|']
    for audit in audits:
        r = audit['priced_result']
        lines.append(f"|{r['id']}|{r['symbol']}|{r['warning_ts']}|{r['status']}|{fmt(r['risk_yuan_per_lot'],2)}|{fmt(r['net_amount'],2)}|{fmt(r['net_r'])}|{'是' if audit['observed_open_beyond_stop'] else '否/不适用'}|")
    lines.extend(['', '未知状态不按零盈亏计；unfinished条目含最后有效行情估值，ambiguous保留两路径结果。',
                  '缺数与换月后已入场持仓不能自动视为平仓。后续该品种被标为不可确认，详见基线账本blocked_by。'])
    bu = [a for a in audits if a['symbol'] == 'BU0' and a['priced_result']['status'] == 'closed']
    if bu:
        a = min(bu, key=lambda a: a['priced_result']['net_amount'])
        r, rec = a['priced_result'], a['reconciliation']
        lines.extend(['', f"BU0 #{r['id']}：{r['fill']}入场，原始止损{r['stop']}，初始风险{fmt(r['risk_yuan_per_lot'],2)}元；{r['exit_ts']}以{r['final_price']}平仓。",
                      f"滑点后毛金额{fmt(rec['gross_yuan'],3)}元，快照费用{fmt(rec['commission_yuan'],3)}元，净额{fmt(rec['net_yuan'],3)}元，净R={fmt(rec['net_r'],6)}。",
                      '换月事实及5分钟证据见同ID的audit.jsonl；未确认标记不足以确认实际合约跳空。'])
    lines.append('全库交叉源核查单列缺失和不一致，不替换原成交或挑选盈利结果。')
    (output / 'audit-reconciliation.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def run(args):
    batch, db, fee_path, output = map(pathlib.Path, (args.batch, args.db, args.fees, args.out))
    check(math.isfinite(args.risk_budget) and args.risk_budget > 0, 'positive frozen risk budget required')
    start, end = args.period_start + ' 00:00:00', args.period_end + ' 00:00:00'
    check(dt.datetime.fromisoformat(start) < dt.datetime.fromisoformat(end), 'invalid period')
    manifest = json.loads((batch / 'manifest.json').read_text(encoding='utf-8'))
    signal_hash, db_hash = digest(batch / 'signals.jsonl'), digest(db)
    check(signal_hash == manifest['signals_sha256'], 'signal archive hash mismatch')
    check(db_hash == manifest['protocol']['source_database_sha256'], 'database hash mismatch')
    profile = json.loads(fee_path.read_text(encoding='utf-8'))
    history = pathlib.Path(args.history) if args.history else None
    history_hash = digest(history) if history else None
    plan = pathlib.Path(args.plan) if args.plan else None
    rules, candidate = manifest['protocol']['baseline_rules'], manifest['protocol']['candidate']
    output.mkdir(parents=True, exist_ok=False)
    protocol = dict(version='score-execution-v1', frozen_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                    signals_sha256=signal_hash, database_sha256=db_hash, fee_sha256=digest(fee_path),
                    longer_history_sha256=history_hash,
                    execution_plan_sha256=digest(plan) if plan else None,
                    manifest_sha256=digest(batch / 'manifest.json'),
                    code_sha256={p.name: digest(p) for p in (pathlib.Path(__file__), pathlib.Path(__file__).with_name('reprice_signal_costs.py'),
                                                           pathlib.Path(__file__).with_name('report_signal_ablation.py'),
                                                           pathlib.Path(__file__).with_name('audit_score_history.py'))},
                    baseline='quality_v1 >= 70; original S1, nf5, horizon60; 1 tick per side; frozen fee snapshot',
                    scenarios=SCENARIOS, versions=VERSIONS, risk_budget_yuan=args.risk_budget,
                    sizing='floor(B/planned warning risk); at proposed entry reduce to min(warned lots, floor(B/actual fill risk)); insufficient lot cancels then, never at a future exit',
                    occupancy='one pending order OR position per symbol; accept in warning/descending selected score/candidate_key order; release known events before same-time warnings',
                    ambiguity='release only if both paths explicitly terminal; otherwise subsequent symbol segment unconfirmed',
                    top20='floor(n/5), at least one, retain all threshold ties; fixed per subset; development-only distribution',
                    ablation_normalization='flatten v1 quality contributions, drop named module, renormalize all remaining weights',
                    period_start=start, period_end_exclusive=end, development_only=True, production_changed=False,
                    new_score_candidate=None, decision='current evidence does not support changing scores',
                    cash_timing='SR roundtrip override allocated half at entry, half at close; no double deduction of slippage',
                    forward_status='not_started_requires_new_data_and_execution_data_review')
    write_json(output / 'protocol.json', protocol)
    snapshot = output / 'source-snapshot'
    snapshot.mkdir()
    for name in protocol['code_sha256']:
        (snapshot / name).write_bytes(pathlib.Path(__file__).with_name(name).read_bytes())
    (output / 'input-manifest.json').write_bytes((batch / 'manifest.json').read_bytes())
    (output / 'fee-profile.json').write_bytes(fee_path.read_bytes())
    if plan:
        (output / 'execution-plan.md').write_bytes(plan.read_bytes())
    market = Market(db)
    history_evidence = None
    if history:
        inventory, discrepancies = crosscheck(market, history)
        write_json(output / 'history-inventory.json', inventory)
        write_rows(output / 'cross-source-discrepancies.jsonl', discrepancies)
        history_evidence = {k: inventory[k] for k in ('first_start', 'last_end', 'bars_5m', 'counts', 'limitations')}
    raw_rows = [json.loads(line) for line in (batch / 'signals.jsonl').open(encoding='utf-8')]
    common = [r for r in raw_rows if r['quality']['status'] == 'ok' and r['scores'] is not None]
    check(len({r['candidate_key'] for r in common}) == len(common), 'duplicate candidate identity')
    scenarios = {s: [] for s in SCENARIOS}
    audits, reconciled, target_gaps = [], collections.Counter(), []
    with (output / 'candidates.jsonl').open('x', encoding='utf-8') as dest:
        for raw in common:
            scores = score_variants(raw, rules, candidate)
            for scenario in SCENARIOS:
                row = prepare(raw, scenario, scores, market, profile)
                check(row['conditions']['planned_risk_yuan_per_lot'] > 0, 'nonpositive planned risk')
                reconciliation = audit_fill(row, market)
                reconciled[scenario] += reconciliation is not None
                if reconciliation and reconciliation['conservative_target_gap_fills']:
                    target_gaps.append(dict(id=row['id'], symbol=row['symbol'], scenario=scenario,
                                            fills=reconciliation['conservative_target_gap_fills']))
                scenarios[scenario].append(row)
                dest.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
                if scenario == 's1_1tick' and ((row['net_r'] is not None and row['net_r'] < -1.5)
                        or (scores['v1'] >= 70 and (row['status'] in ('missing_data', 'rollover')
                            or (start <= row['warning_ts'] < end and row['status'] in ('unfinished', 'ambiguous'))))):
                    audits.append(audit_detail(raw, row, market, reconciliation, profile))
    write_rows(output / 'audit.jsonl', audits)
    render_audit(audits, output)
    write_rows(output / 'conservative-target-gaps.jsonl', target_gaps)
    common_keys = {r['candidate_key'] for r in common}
    summary = dict(protocol=protocol, common_candidates=len(common), excluded_statuses=dict(collections.Counter(
        r['quality']['status'] if r['quality']['status'] != 'ok' else 'candidate_score_unavailable'
        for r in raw_rows if r['candidate_key'] not in common_keys)), audit_count=len(audits), reconciled_closed=dict(reconciled),
        conservative_target_gap_count=len(target_gaps),
        baseline={}, ablations={}, independent_baseline={}, cost_risk_strata={}, longer_history=history_evidence)
    ledger_dir = output / 'ledgers'
    ledger_dir.mkdir()
    for scenario, rows in scenarios.items():
        independent = [r for r in rows if r['scores']['v1'] >= 70]
        month = [r for r in independent if start <= r['warning_ts'] < end]
        summary['independent_baseline'][scenario] = dict(all_history=metrics(independent), last_month=metrics(month),
            last_month_signal_marks=[dict(id=r['id'], **period_account([r], start, end, market, profile))
                                     for r in month if r['status'] in ('unfinished', 'ambiguous')])
        summary['cost_risk_strata'][scenario] = {name: metrics([r for r in rows if (r['signal_risk_ticks'] + 1e-6 >= 10) == keep])
                                                for name, keep in (('below10', False), ('risk10', True))}
        summary['baseline'][scenario] = {}
        for mode, budget in (('one_lot', None), ('integer_budget', args.risk_budget)):
            ledger = replay(rows, 'v1', 70, budget)
            write_rows(ledger_dir / f'baseline-{scenario}-{mode}.jsonl', ledger)
            summary['baseline'][scenario][mode] = ledger_report(ledger, start, end, market, profile)
    for subset in ('all', 'risk10'):
        source = {s: [r for r in rs if subset == 'all' or r['signal_risk_ticks'] + 1e-6 >= 10]
                  for s, rs in scenarios.items()}
        days = {r['warning_ts'][:10] for r in source['s1_1tick']}
        cuts = {v: top_cut(source['s1_1tick'], v) for v in VERSIONS}
        refs = {s: accepted(replay(rows, 'v1', cuts['v1'])) for s, rows in source.items()}
        v2refs = {s: accepted(replay(rows, 'v2', cuts['v2'])) for s, rows in source.items()}
        summary['ablations'][subset] = {}
        for version in VERSIONS:
            cut = cuts[version]
            case = dict(threshold=cut, selected_signals=sum(cut is not None and r['scores'][version] >= cut
                                                           for r in source['s1_1tick']), scenarios={})
            for scenario, rows in source.items():
                chosen = [r for r in rows if cut is not None and r['scores'][version] >= cut]
                ledger = replay(rows, version, cut)
                orders = accepted(ledger)
                write_rows(ledger_dir / f'{subset}-{version}-{scenario}.jsonl', ledger)
                case['scenarios'][scenario] = dict(independent=metrics(chosen), executable=metrics(orders),
                                                   coverage=coverage_metrics(ledger),
                                                   decisions=dict(collections.Counter(r['decision'] for r in ledger)),
                                                   warning_months={m: metrics([r for r in orders if r['warning_ts'][:7] == m])
                                                                   for m in sorted({r['warning_ts'][:7] for r in rows})},
                                                   symbols={s: metrics([r for r in orders if r['symbol'] == s])
                                                            for s in sorted({r['symbol'] for r in rows})},
                                                   last_month_warning_cohort=metrics([r for r in orders if start <= r['warning_ts'] < end]),
                                                   leave_one_symbol_out={s: metrics([r for r in orders if r['symbol'] != s])
                                                                         for s in sorted({r['symbol'] for r in orders})},
                                                   v1_difference_interval=block_difference(refs[scenario], orders, days),
                                                   module_reference='v2' if version.startswith('v2') else 'v1',
                                                   module_difference_interval=block_difference(
                                                       v2refs[scenario] if version.startswith('v2') else refs[scenario], orders, days))
            summary['ablations'][subset][version] = case
    write_json(output / 'rolling.json', rolling_diagnostics(scenarios['s1_1tick']))
    write_json(output / 'summary.json', summary)
    # Registration is reviewable and deliberately does not pretend to collect future signals.
    write_json(output / 'forward-registration.json', dict(
        status='not_started', baseline='v1 >=70, unchanged', candidate=None,
        reason='No supported new score; need rescan of the inventoried longer history, verified contracts/calendar and frozen causal threshold before a candidate forward comparison.',
        earliest_eligible_signal='strictly after protocol.frozen_at; archived history is development only',
        symbols=sorted(market.bars), risk_budget_yuan=args.risk_budget, scenarios=SCENARIOS,
        rules=protocol['occupancy'], primary='executable after-cost mean netR improvement versus frozen v1',
        promotion='paired joint-date-block improvement interval excludes <=0; acceptable currency risk and coverage; no early profitability stop',
        protocol_sha256=digest(output / 'protocol.json'),
        review_requirement='Register candidate, threshold, start and review date/sample size before collection; changes archive old segment and restart.'))
    render_report(summary, output)
    check(digest(db) == db_hash and digest(batch / 'signals.jsonl') == signal_hash, 'source mutated during analysis')
    check(not history or digest(history) == history_hash, 'longer history mutated during analysis')
    check(all(digest(pathlib.Path(__file__).with_name(name)) == expected
              and digest(snapshot / name) == expected for name, expected in protocol['code_sha256'].items()),
          'code changed during frozen analysis')
    write_json(output / 'outputs.json', {str(p.relative_to(output)): digest(p) for p in sorted(output.rglob('*')) if p.is_file()})
    print(json.dumps(dict(output=str(output.resolve()), common_candidates=len(common), audit_count=len(audits),
                          baseline_month=summary['baseline']['s1_1tick']['one_lot']['last_month_warning_cohort']),
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('batch', 'db', 'fees', 'out'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--risk-budget', required=True, type=float, help='Freeze an illustrative B before inspecting this run; not account capital')
    parser.add_argument('--history', help='Optional existing concrete-contract SQLite: inventory and cross-source audit, read-only')
    parser.add_argument('--plan', help='Optional execution plan to snapshot and hash with the protocol')
    parser.add_argument('--period-start', default='2026-09-30')
    parser.add_argument('--period-end', default='2026-10-30')
    run(parser.parse_args())
