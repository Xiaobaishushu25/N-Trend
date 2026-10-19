"""Repeat the declared40 hypotheses under the user's weekend/holiday rule."""
import hashlib
import json
from pathlib import Path

import discover_eg_swing as discovery

BASE = discovery.OUT
OUT = BASE.parent / "eg_swing_calendarflat_20261019"


def configure():
    OUT.mkdir(parents=True, exist_ok=True)
    for name in ["main_reserve_1m.csv", "eg2601_reserve_1m.csv", "reserve_sources.json"]:
        source = (BASE / name).read_bytes()
        path = OUT / name
        if path.exists():
            assert path.read_bytes() == source
        else:
            path.write_bytes(source)
    original_configs = discovery.configs
    discovery.configs = lambda: [{**c, "calendar_flat": True} for c in original_configs()]
    discovery.OUT = OUT
    discovery.save("research_history.json", {"prior_unrestricted_run": str(BASE.resolve()),
         "change": "user explicitly confirms close before weekends and long holidays; ordinary day15/23 not compulsory",
         "trial_count": "same40 parameter sets under second user-requested calendar policy; initial40 and their results remain disclosed",
         "warning": "market/performance in all periods already inspected; this is NOT pristine out-of-sample confirmation",
         "prior_plan_sha256": hashlib.sha256((BASE / "analysis_plan.json").read_bytes()).hexdigest()})


if __name__ == "__main__":
    configure()
    discovery.main()
