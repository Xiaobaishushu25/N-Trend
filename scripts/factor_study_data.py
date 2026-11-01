"""Immutable research I/O and concrete-contract preparation. Source stores are read-only."""
import bisect
import collections
import datetime as dt
import hashlib
import json
import math
import platform
import importlib.metadata
import pathlib
import re
import sqlite3

ROOT = pathlib.Path(__file__).resolve().parents[1]
ZONE = dt.timezone(dt.timedelta(hours=8))
MINUTE = 60_000_000_000


def read(path):
    return json.loads(pathlib.Path(path).read_text(encoding='utf-8'))


def rows(path):
    with pathlib.Path(path).open(encoding='utf-8') as stream:
        return [json.loads(line) for line in stream if line.strip()]


def write(path, value):
    with pathlib.Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write('\n')


def write_rows(path, values):
    with pathlib.Path(path).open('x', encoding='utf-8') as stream:
        for value in values:
            stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))+'\n')


def digest(path):
    h = hashlib.sha256()
    with pathlib.Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4*1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def new_output(path):
    path = pathlib.Path(path).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError('research output must remain in the repository')
    path.mkdir(parents=True, exist_ok=False)
    sources=path/'source-snapshot'
    sources.mkdir()
    for source in sorted(pathlib.Path(__file__).parent.glob('*factor_study*.py')):
        (sources/source.name).write_bytes(source.read_bytes())
    for name in ('reprice_signal_costs.py','report_signal_ablation.py'):
        source=pathlib.Path(__file__).with_name(name)
        (sources/name).write_bytes(source.read_bytes())
    requirements=pathlib.Path(__file__).with_name('factor-study-requirements.txt')
    if requirements.exists():(sources/requirements.name).write_bytes(requirements.read_bytes())
    return path


def seal(out, stage, inputs, extra=None):
    snapshots=out/'source-snapshot'
    if snapshots.exists():
        for source in snapshots.iterdir():
            if digest(source)!=digest(pathlib.Path(__file__).parent/source.name):
                raise ValueError('research source changed during stage: '+source.name)
    manifest = dict(schema='factor-study-stage-v1', stage=stage, development_only=True,
                    inputs={str(pathlib.Path(p).resolve()): digest(p) for p in inputs},
                    files={str(p.relative_to(out)): digest(p) for p in sorted(out.rglob('*'))
                           if p.is_file() and p.name != 'manifest.json'},
                    code={p.name: digest(p) for p in sorted((snapshots if snapshots.exists() else pathlib.Path(__file__).parent).glob('*.py'))
                          if snapshots.exists() or 'factor_study' in p.name})
    manifest.update(extra or {})
    runtime={'python':platform.python_version()}
    for name in ('numpy','scipy','matplotlib'):
        try:runtime[name]=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:runtime[name]=None
    manifest['runtime']=runtime
    write(out/'manifest.json', manifest)
    return manifest


def verify_seal(path):
    path = pathlib.Path(path)
    manifest = read(path/'manifest.json')
    for name, expected in manifest['files'].items():
        if digest(path/name) != expected:
            raise ValueError('stage output changed: '+str(path/name))
    for name, expected in manifest['inputs'].items():
        if digest(name) != expected:
            raise ValueError('stage input changed: '+name)
    return manifest


def local(ns):
    return dt.datetime.fromtimestamp(ns/1e9, ZONE).replace(tzinfo=None)


def stamp(value):
    return value.isoformat(sep=' ', timespec='seconds')


class Calendar:
    def __init__(self, data, night_ends):
        self.days = {r['date']: r['trading'] for r in data['records']}
        self.trading = sorted(k for k,v in self.days.items() if v)
        self.night_ends = night_ends

    def is_day(self, date):
        key = date.isoformat()
        if key not in self.days:
            raise ValueError('calendar outside frozen coverage: '+key)
        return self.days[key]

    def has_night(self, date):
        if not self.is_day(date):
            return False
        following = date + dt.timedelta(days=1)
        for _ in range(15):
            if self.is_day(following):
                return True
            if following.weekday() < 5:
                return False
            following += dt.timedelta(days=1)
        raise ValueError('no successor trading date')

    def day(self, ts):
        time = dt.datetime.fromisoformat(ts) if isinstance(ts,str) else ts
        date, hour = time.date().isoformat(), time.hour
        index = (bisect.bisect_right(self.trading,date) if hour >= 18 else
                 bisect.bisect_left(self.trading,date))
        if index == len(self.trading):
            raise ValueError('calendar lacks next trading day')
        return date if 6 <= hour < 18 and self.days.get(date) else self.trading[index]

    def expected(self, time, symbol):
        """Minute START timestamps: session openings included, closings excluded."""
        minute = time.hour*60+time.minute
        if any(a <= minute < b for a,b in ((540,615),(630,690),(810,900))):
            return self.is_day(time.date())
        end = self.night_ends[symbol]
        if end is None:
            return False
        if 1260 <= minute < (end if end >= 1260 else 1440):
            return self.has_night(time.date())
        if end < 1260 and minute < end:
            return self.has_night(time.date()-dt.timedelta(days=1))
        return False


def aggregate(raw):
    buckets = collections.defaultdict(list)
    for bar in raw:
        time = dt.datetime.fromisoformat(bar['start'])
        start = time.replace(minute=time.minute//15*15)
        buckets[start].append(bar)
    result, partial = [], []
    for start, group in sorted(buckets.items()):
        if [b['start'] for b in group] != [stamp(start+dt.timedelta(minutes=i)) for i in range(15)]:
            partial.append(stamp(start)); continue
        result.append(dict(ts=stamp(start+dt.timedelta(minutes=15)), open=group[0]['open'],
                           high=max(b['high'] for b in group), low=min(b['low'] for b in group),
                           close=group[-1]['close'], volume=sum(b['volume'] for b in group), hold=group[-1]['hold']))
    return result, partial


def specifications(protocol):
    source = (ROOT/'crates/n-core/src/precision.rs').read_text(encoding='utf-8')
    def table(name):
        section = source.split('pub const '+name,1)[1].split('];',1)[0]
        return {k:float(v) for k,v in re.findall(r'\("([A-Z]+)",\s*([\d.]+)\)',section)}
    ticks, multipliers = table('DEFAULT_TICKS'), table('DEFAULT_MULTIPLIERS')
    ticks.update({s[:-1]:v for s,v in protocol.get('tick_overrides',{}).items()})
    multipliers.update({s[:-1]:v for s,v in protocol['multiplier_overrides'].items()})
    return {s:dict(tick=ticks[s[:-1]], multiplier=multipliers[s[:-1]])
            for s in protocol['symbols']}


def read_contract(db_paths,contract,start_ns,end_ns,conflicts):
    """Bound memory by one contract, including when preparing the longer archive."""
    raw={};invalid=set()
    for db_path in db_paths:
        conn=sqlite3.connect(db_path.resolve().as_uri()+'?mode=ro',uri=True)
        conn.row_factory=sqlite3.Row
        try:
            for r in conn.execute('SELECT * FROM bars WHERE contract=? AND period_seconds=60 AND start_ns>=? AND start_ns<? ORDER BY start_ns',
                                  (contract,start_ns,end_ns)):
                b=dict(start=stamp(local(r['start_ns'])),ts=stamp(local(r['end_ns'])),
                       open=r['open'],high=r['high'],low=r['low'],close=r['close'],volume=r['volume'],hold=r['close_oi'] or 0)
                valid=(r['end_ns']-r['start_ns']==MINUTE and r['complete']==1
                       and all(math.isfinite(b[k]) for k in ('open','high','low','close','volume','hold'))
                       and 0 < b['low'] <= min(b['open'],b['close']) <= max(b['open'],b['close']) <= b['high'] and b['volume']>=0)
                if not valid:invalid.add(b['start']);continue
                previous=raw.get(b['start'])
                if previous and previous!=b:
                    conflicts.append(dict(contract=contract,start=b['start'],old=previous,new=b));invalid.add(b['start'])
                elif b['start'] not in invalid:raw[b['start']]=b
        finally:conn.close()
    for ts in invalid:raw.pop(ts,None)
    return [b for _,b in sorted(raw.items())],invalid


def prepare(protocol_path, output):
    protocol = read(protocol_path)
    if protocol['schema'] != 'factor-study-protocol-v1':
        raise ValueError('unsupported protocol')
    if protocol['start']>=protocol['end']:raise ValueError('start must precede end')
    if protocol['execution']['lots']!=1:raise ValueError('only one-lot research is supported')
    if set(protocol['execution']['slip_scenarios'])!={0,1,2}:raise ValueError('required cost scenarios are 0/1/2 ticks')
    if set(protocol['symbols'])-set(protocol['night_ends']):raise ValueError('missing explicit session specification')
    if min(protocol['execution']['planned_loss_cap_yuan'],protocol['execution']['target_net_yuan'])<=0:
        raise ValueError('risk and target must be positive')
    out = new_output(output)
    write(out/'protocol.json', protocol)
    calendar_path = ROOT/protocol['calendar']
    calendar_data = read(calendar_path)
    write(out/'calendar.json',calendar_data)
    fee_path = ROOT/protocol['fees']
    write(out/'fees.json',read(fee_path))
    calendar = Calendar(calendar_data,protocol['night_ends'])
    specs = specifications(protocol)
    start, end = (dt.datetime.fromisoformat(protocol[k]) for k in ('start','end'))
    load_start = start-dt.timedelta(days=protocol['warmup_calendar_days'])
    start_ns, end_ns = [int(t.replace(tzinfo=ZONE).timestamp()*1e9) for t in (load_start,end)]
    db_paths = [ROOT/p for p in protocol['history_sources']]
    contracts, mappings, conflicts, source_meta = {}, collections.defaultdict(dict), [], []
    for db_path in db_paths:
        conn = sqlite3.connect(db_path.resolve().as_uri()+'?mode=ro',uri=True)
        conn.row_factory = sqlite3.Row
        source_meta.append(dict(path=str(db_path),metadata=dict(conn.execute('SELECT key,value FROM metadata'))))
        for r in conn.execute('SELECT * FROM mappings ORDER BY effective_date'):
            symbol = r['product'].split('.')[-1].upper()+'0'
            if symbol not in specs: continue
            old = mappings[symbol].get(r['effective_date'])
            if old and old['contract'] != r['contract']:
                raise ValueError('conflicting contract mapping')
            mappings[symbol][r['effective_date']] = dict(r)
        conn.close()
    for symbol,schedule in mappings.items():
        dates=sorted(schedule);a=max(0,bisect.bisect_right(dates,load_start.date().isoformat())-1)
        for day in dates[a:bisect.bisect_left(dates,end.date().isoformat())]:
            contracts[schedule[day]['contract']]=dict(symbol=symbol)
    (out/'contracts').mkdir()
    coverage, scan_paths = [], []
    all_dates = [d for d in calendar.trading if start.date().isoformat() <= d < end.date().isoformat()]
    for contract, info in sorted(contracts.items()):
        symbol = info['symbol']
        raw,invalid=read_contract(db_paths,contract,start_ns,end_ns,conflicts)
        if not raw: continue
        bars, partial = aggregate(raw)
        schedule = mappings[symbol]
        dates = sorted(schedule)
        def active(ts):
            day = calendar.day(ts)
            index = bisect.bisect_right(dates,day)-1
            return index >= 0 and schedule[dates[index]]['contract'] == contract
        eligible = [b['ts'] for b in bars if load_start <= dt.datetime.fromisoformat(b['ts']) < end and active(b['ts'])]
        known = {b['start'] for b in raw}
        gaps = []
        # Include missing whole sessions between first/last quotes; holidays are calendar-driven.
        cursor, last = dt.datetime.fromisoformat(raw[0]['start']),dt.datetime.fromisoformat(raw[-1]['start'])
        while cursor <= last:
            if calendar.expected(cursor,symbol) and stamp(cursor) not in known:
                gaps.append(stamp(cursor))
            cursor += dt.timedelta(minutes=1)
        shard = dict(symbol=symbol,contract=contract,bars=bars,eligible_ts=eligible,window_start=protocol['start'],**specs[symbol])
        filename = contract.replace('.','_')
        write(out/'contracts'/f'{filename}.json',shard)
        write_rows(out/'contracts'/f'{filename}.minutes.jsonl',raw)
        write(out/'contracts'/f'{filename}.gaps.json',gaps)
        scan_paths.append('contracts/'+filename+'.json')
        coverage.append(dict(symbol=symbol,contract=contract,first=raw[0]['start'],last=raw[-1]['ts'],
                             minutes=len(raw),bars15=len(bars),eligible_bars=len(eligible),partial15=len(partial),
                             missing_expected_minutes=len(gaps),invalid_minutes=len(invalid),
                             shard='contracts/'+filename+'.json',minutes_file='contracts/'+filename+'.minutes.jsonl',
                             gaps_file='contracts/'+filename+'.gaps.json'))
        print(f'{contract}: {len(raw)} minutes, {len(eligible)} eligible bars, {len(gaps)} missing',flush=True)
    with (out/'scan-input.jsonl').open('x',encoding='utf-8') as stream:
        stream.write(''.join(s+'\n' for s in scan_paths))
    write_rows(out/'conflicts.jsonl',conflicts)
    write(out/'coverage.json',dict(contracts=coverage,trading_dates=all_dates,mappings=dict(mappings),
                                 source_metadata=source_meta,specifications=specs,
                                 actual_last=max((x['last'] for x in coverage),default=None),
                                 limitations=['Revised mapping snapshot: original availability not verified.',
                                              'Fee snapshot and current contract specification assumptions, not historical bills.',
                                              'Calendar provenance and missing sessions remain audit evidence; no inferred profitable fills.']))
    seal(out,'prepare',[protocol_path,calendar_path,fee_path,*db_paths,ROOT/'crates/n-core/src/precision.rs'])
    return out
