"""Probe legitimate TqSDK backtest access; no trading or bulk data export.

Credentials are entered locally with a hidden password, never written to the
result. This probes account access rather than assuming EDB and SDK entitlements
are identical. The probe does not request professional download functions.
"""
import argparse
from datetime import date, datetime, timedelta, timezone
from getpass import getpass
import json
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[1]
ZONE = timezone(timedelta(hours=8))


def main():
    parser = argparse.ArgumentParser(description="Test EG minute history in TqSDK backtest mode; no orders.")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2025, 8, 18))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2025, 8, 20))
    parser.add_argument("--timeout", type=int, default=90, help="Wall-clock limit after SDK initialization")
    args = parser.parse_args()
    if args.start >= args.end:
        parser.error("--end must be later than --start")

    from tqsdk import TqApi, TqAuth, TqBacktest, TqSim, BacktestFinished

    username = input("Tianqin / Kuaqi account (local input): ").strip()
    password = getpass("Password (hidden, not saved): ")
    if not username or not password:
        raise SystemExit("Account and password are required. No connection attempted.")
    api = None
    result = {"checked_at": datetime.now(ZONE).isoformat(),
              "mode": "TqBacktest with TqSim; no orders; no bulk export", "symbols": {},
              "requested_dates": [args.start.isoformat(), args.end.isoformat()],
              "status": "not_confirmed", "edb_permission_test": False}
    start_ns = int(datetime.combine(args.start, datetime.min.time(), ZONE).timestamp()) * 10**9
    end_ns = int(datetime.combine(args.end + timedelta(days=1), datetime.min.time(), ZONE).timestamp()) * 10**9
    try:
        api = TqApi(TqSim(), auth=TqAuth(username, password),
                    backtest=TqBacktest(start_dt=args.start, end_dt=args.end), web_gui=False)
        serials = {}
        for symbol in ["KQ.m@DCE.eg", "DCE.eg2509", "DCE.eg2601"]:
            for period in [60, 900]:
                serials[(symbol, period)] = api.get_kline_serial(symbol, period, data_length=60)
        password = None
        seen = {key: set() for key in serials}
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            api.wait_update(deadline=time.time()+1)
            for key, frame in serials.items():
                valid = frame[(frame.datetime >= start_ns) & (frame.datetime < end_ns) & frame.close.notna()]
                seen[key].update(int(v) for v in valid.datetime)
            # A small sample establishes access; do not harvest the whole history.
            if all(len(values) >= 3 for values in seen.values()):
                result["status"] = "target_history_observed"
                break
        for (symbol, period), values in seen.items():
            result["symbols"][f"{symbol}:{period}s"] = {
                "observed_bar_timestamps": len(values),
                "first_start_label": datetime.fromtimestamp(min(values)/10**9, ZONE).isoformat() if values else None,
                "last_start_label": datetime.fromtimestamp(max(values)/10**9, ZONE).isoformat() if values else None}
    except BacktestFinished:
        result["status"] = "backtest_finished_before_probe_completed"
    except Exception as exc:
        # Avoid writing authentication objects or exception internals to disk.
        result["status"] = "connection_or_permission_error"
        result["exception_type"] = type(exc).__name__
        print("Connection or permission check failed:", type(exc).__name__)
    finally:
        password = None
        if api is not None:
            api.close()
    result["meaning"] = "Confirms only observed sample access; does not grant professional bulk-download or EDB permissions."
    out = ROOT/"reports"/"eg_tqsdk_history_probe_20261019"
    out.mkdir(parents=True, exist_ok=True)
    path = out/"access_result.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("Saved:", path)


if __name__ == "__main__":
    main()
