"""Freeze research inputs via a read-only SQLite connection; never touches runtime DBs."""
import argparse
import hashlib
import json
import pathlib
import sqlite3


def export(archive, frozen_run, out):
    archive, frozen_run, out = map(pathlib.Path, (archive, frozen_run, out))
    if out.exists():
        raise FileExistsError("Input export must be new")
    signals = json.loads((frozen_run / "signals.json").read_text(encoding="utf-8"))
    report = json.loads((frozen_run / "report.json").read_text(encoding="utf-8"))
    connection = sqlite3.connect(archive.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    symbols = {}
    for symbol in sorted({e["symbol"] for e in signals}):
        row = connection.execute("SELECT variety FROM symbols WHERE code=?", (symbol,)).fetchone()
        bars = [dict(r) for r in connection.execute(
            "SELECT ts AS datetime,open,high,low,close,volume,hold FROM klines WHERE symbol=? AND timeframe='5m' AND source='raw' ORDER BY ts", (symbol,))]
        assert bars and len({b["datetime"] for b in bars}) == len(bars)
        for b in bars:
            assert b["low"] <= min(b["open"], b["close"]) <= max(b["open"], b["close"]) <= b["high"]
        rolls = [dict(r) for r in connection.execute("SELECT symbol,ts,from_contract,to_contract,confirmed FROM rollovers WHERE symbol=? ORDER BY ts", (symbol,))]
        for r in rolls:
            r["confirmed"] = bool(r["confirmed"])
        symbols[symbol] = dict(variety=row["variety"] if row else "", bars=bars, rollovers=rolls)
    old_fields = "id,symbol,direction,s0_ts,s1_ts,s2_ts,warning_ts,entry_score,entry,stop,target,risk,trigger_price,trigger_ts,trigger_bar_ts,exit_price,exit_ts,exit_reason,outcome,r_multiple"
    old = [dict(r) for r in connection.execute("SELECT " + old_fields + " FROM pattern_events ORDER BY id")]
    connection.close()
    provenance = dict(archive=str(archive.resolve()), archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                      frozen_run=str(frozen_run.resolve()), source_run_id=report["run_id"],
                      raw_bars=sum(len(s["bars"]) for s in symbols.values()), symbols=len(symbols),
                      signals_sha256=hashlib.sha256((frozen_run / "signals.json").read_bytes()).hexdigest(),
                      purpose="Predeclared diagnostic ablation; not prospective validation; no production writes")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x", encoding="utf-8") as f:
        json.dump(dict(symbols=symbols, execution_config=report["frozen_execution_config"], provenance=provenance, old_events=old), f, ensure_ascii=False, separators=(",", ":"))
    print(json.dumps(dict(output=str(out), **provenance), ensure_ascii=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True)
    parser.add_argument("--frozen-run", required=True)
    parser.add_argument("--out", required=True)
    a = parser.parse_args()
    export(a.archive, a.frozen_run, a.out)
