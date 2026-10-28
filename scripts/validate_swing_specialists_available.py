"""Scope amendment due to inaccessible old raw minutes, saved BEFORE analysis."""
import copy
import datetime as dt
import sys
import research_swing_specialists as s

scope=sys.argv[1]
assert scope in ['cost14','native2026']
parent=s.RUN
s.RUN=parent/scope
if not s.RUN.exists():
    s.RUN.mkdir()
    p=copy.deepcopy(s.m.old.read(parent/'protocol.json'))
    p.update(id=parent.name+'/'+scope,scope=scope,
             created_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
             parent_protocol_sha256=s.m.old.sha(parent/'protocol.json'),
             reason='Old raw-minute service repeatedly failed SDK initialization/WS handshake. Freeze available-data validation before inspecting any new candidate outcomes; do not mix missing old exit replays into completed coverage.',
             script_sha256=s.m.old.sha(s.__file__))
    if scope=='cost14':
        p.update(months=s.m.MONTHS,folds=s.FOLDS[1:],
                 template_names=['C_B5','C_B2','E20'],
                 variants=['B5','B2','JD20','AG20','AG20_aligned','AG_B2'])
    else:
        p.update(months=s.m.report.OLD_MONTHS,folds=s.FOLDS[2:],evaluation_periods=['early','recent'])
    s.write('protocol.json',p)
s.research()
