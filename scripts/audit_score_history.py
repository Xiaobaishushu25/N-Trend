"""Read-only inventory and cross-source audit of the existing concrete-contract store.

This is data evidence, not an independent backtest or a historical as-of mapping.
"""
import bisect
import collections
import datetime as dt
import pathlib
import sqlite3

ZONE = dt.timezone(dt.timedelta(hours=8))
FIVE_MIN_NS = 300_000_000_000


def timestamp(ns):
    return dt.datetime.fromtimestamp(ns / 1e9, ZONE).strftime('%Y-%m-%d %H:%M:%S')


def crosscheck(market, path):
    conn = sqlite3.connect(pathlib.Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    mappings = collections.defaultdict(list)
    for r in conn.execute('SELECT * FROM mappings ORDER BY product,effective_date'):
        mappings[r['product'].split('.')[-1].upper() + '0'].append(dict(r))
    inventory = []
    for r in conn.execute('''SELECT contract,MIN(start_ns) AS first,MAX(end_ns) AS last,
                            COUNT(*) AS bars,SUM(complete) AS complete
                            FROM bars WHERE period_seconds=300 GROUP BY contract'''):
        inventory.append(dict(contract=r['contract'], first_start=timestamp(r['first']),
                              last_end=timestamp(r['last']), bars_5m=r['bars'],
                              complete_5m=r['complete'], incomplete_5m=r['bars'] - r['complete']))
    records, counts = [], {}
    for symbol, bars in market.bars.items():
        schedule = mappings[symbol]
        days = [r['effective_date'] for r in schedule]
        counter = collections.Counter()
        for bar in bars:
            try:
                day = market.calendar.day(bar['ts'])
            except ValueError:
                records.append(dict(symbol=symbol, ts=bar['ts'], status='calendar_unresolved'))
                counter['calendar_unresolved'] += 1
                continue
            index = bisect.bisect_right(days, day) - 1
            if index < 0:
                records.append(dict(symbol=symbol, ts=bar['ts'], status='mapping_absent'))
                counter['mapping_absent'] += 1
                continue
            mapping = schedule[index]
            end_ns = int(dt.datetime.fromisoformat(bar['ts']).replace(tzinfo=ZONE).timestamp()) * 1_000_000_000
            # Primary key uses start_ns. A partial aggregate may start later, so
            # inspect the entire five-minute start interval and match the end.
            sources = conn.execute('''SELECT start_ns,end_ns,open,high,low,close,complete,constituent_count
                                      FROM bars WHERE contract=? AND period_seconds=300
                                      AND start_ns>=? AND start_ns<? AND end_ns=?''',
                                   (mapping['contract'], end_ns - FIVE_MIN_NS, end_ns, end_ns)).fetchall()
            source = dict(sources[0]) if len(sources) == 1 else None
            if source is None:
                status = 'source_bar_absent'
            elif not source['complete']:
                status = 'source_bar_incomplete'
            elif all(abs(bar[k] - source[k]) < 1e-6 for k in ('open', 'high', 'low', 'close')):
                status = 'ohlc_match'
            else:
                status = 'ohlc_mismatch'
            counter[status] += 1
            # Matching observations are counted, discrepancies retain both sets
            # of prices. Neither source is silently declared the ground truth.
            if status != 'ohlc_match':
                records.append(dict(symbol=symbol, ts=bar['ts'], trading_date_inferred=day,
                                    status=status, mapped_contract=mapping['contract'],
                                    mapping_effective_date=mapping['effective_date'],
                                    frozen_ohlc={k: bar[k] for k in ('open', 'high', 'low', 'close')},
                                    source_bar=source))
        counts[symbol] = dict(counter)
    metadata = dict(conn.execute('SELECT key,value FROM metadata').fetchall())
    requests = [dict(r) for r in conn.execute('SELECT status,COUNT(*) AS requests FROM requests GROUP BY status')]
    conn.close()
    return dict(source=str(pathlib.Path(path).resolve()), metadata=metadata, contracts=inventory,
                first_start=min((x['first_start'] for x in inventory), default=None),
                last_end=max((x['last_end'] for x in inventory), default=None),
                bars_5m=sum(x['bars_5m'] for x in inventory), request_statuses=requests,
                counts=counts, mappings=dict(mappings),
                limitations=['Mapping table is a revised historical snapshot, availability at original signal times unverified.',
                             'Trading dates inferred from frozen daytime sessions, not an official exchange calendar.',
                             'Cross-source mismatch is evidence to review, not permission to remove losing trades.',
                             'Longer history inventoried, not rescanned or represented as unseen validation.']), records
