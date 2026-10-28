"""Check existing local Tq authentication against older minute backtest data.

No orders, exported prices, credentials, or exception internals are saved.
"""
import datetime as dt
import json
import pathlib
import time

REPO = pathlib.Path(__file__).resolve().parents[1]
OUT = REPO / 'research-runs/swing-older-four-20261026-v1'


def main():
    from tqsdk import TqApi, TqAuth, TqBacktest, TqSim, BacktestFinished

    OUT.mkdir(exist_ok=True)
    config = json.loads((REPO / 'data/config.json').read_text(encoding='utf-8'))['data_source']
    secrets = json.loads((REPO / 'data/secrets.json').read_text(encoding='utf-8'))
    username = config['tq_account']
    password = config.get('tq_password') or secrets.get('tq_password')
    assert username and password, 'Missing configured Tianqin credentials'
    zone = dt.timezone(dt.timedelta(hours=8))
    first = dt.datetime(2025, 9, 1, tzinfo=zone).timestamp() * 10**9
    last = dt.datetime(2025, 9, 3, tzinfo=zone).timestamp() * 10**9
    result = dict(checked_at=dt.datetime.now(zone).isoformat(),
                  status='unconfirmed', requested_dates=['2025-09-01', '2025-09-02'],
                  mode='TqBacktest/TqSim, no orders; small sample only',
                  symbols={}, credentials_saved=False)
    api = None
    try:
        api = TqApi(TqSim(), auth=TqAuth(username, password),
                    backtest=TqBacktest(start_dt=dt.date(2025, 9, 1),
                                       end_dt=dt.date(2025, 9, 2)),
                    debug=False, disable_print=True, web_gui=False)
        password = None
        series = {s: api.get_kline_serial(s, 60, data_length=20)
                  for s in ('KQ.m@DCE.eg', 'DCE.eg2601')}
        seen = {s: set() for s in series}
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            api.wait_update(deadline=time.time() + 1)
            for symbol, frame in series.items():
                valid = frame[(frame.datetime >= first) & (frame.datetime < last) & frame.close.notna()]
                seen[symbol].update(int(v) for v in valid.datetime)
            if all(len(v) >= 3 for v in seen.values()):
                result['status'] = 'older_minute_backtest_access_observed'
                break
        for symbol, values in seen.items():
            result['symbols'][symbol] = dict(observed_timestamps=len(values),
                first_start_label=dt.datetime.fromtimestamp(min(values)/10**9, zone).isoformat() if values else None,
                last_start_label=dt.datetime.fromtimestamp(max(values)/10**9, zone).isoformat() if values else None)
    except BacktestFinished:
        result['status'] = 'backtest_finished_before_all_samples'
    except Exception as exc:
        result.update(status='connection_or_permission_error', exception_type=type(exc).__name__)
    finally:
        password = None
        if api is not None:
            api.close()
    result['meaning'] = 'Sample backtest access only; does not imply bulk-download/EDB entitlement or complete four-month coverage.'
    (OUT / 'tqsdk-access-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
