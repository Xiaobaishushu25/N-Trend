"""Frozen B0--B5 backward extension, normal TqBacktest stream in memory.

Raw minute histories remain in memory and are piped to the frozen research
engines. Persist only derived warning/execution artifacts, coverage and hashes.
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
import shutil
import subprocess
import sys
import time

sys.dont_write_bytecode = True
REPO = pathlib.Path(__file__).resolve().parents[1]
RUN = REPO / 'research-runs/swing-older-four-20261026-v1'
SNAP = REPO / 'research-strategies/best-a-entry-economics-observation-v1'
BASE = pathlib.Path('G:/待处理文件/交易系统/探索/P2P3分散优化-20261025/external')
TABLE = pathlib.Path('G:/待处理文件/交易系统/探索/新N策略回测-20261024/多源复核/raw/tq_continuous_table.response')
START, END = '2025-09-01 00:00:00', '2025-12-31 15:00:00'
WARMUP, TAIL = '2025-08-01 00:00:00', '2026-01-16 15:00:00'
TARGET = REPO / 'target/swing-older-four-isolated'
EXCH = {**dict.fromkeys(['BU','PB','RB','SS'],'SHFE'),
        **dict.fromkeys(['C','EB','EG','JD','L','PP','V'],'DCE'),
        **dict.fromkeys(['CF','CJ','FG','MA','SA','SF','SH','SM','SR','UR'],'CZCE'), 'SI':'GFEX'}


def read(p):
    return json.loads(pathlib.Path(p).read_text(encoding='utf-8'))


def write(p, value):
    with pathlib.Path(p).open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)


def sha(p):
    return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def replace(source, old, new):
    assert source.count(old) == 1, (source.count(old), old[:100])
    return source.replace(old, new)


MEMREAD = '''
static MEMORY: std::sync::OnceLock<Value> = std::sync::OnceLock::new();
fn read(p: impl AsRef<Path>) -> Result<Value> {
    let p=p.as_ref();
    if let Some(key)=p.to_str().unwrap().strip_prefix("memory:") {
        let all=MEMORY.get_or_init(||serde_json::from_reader(std::io::stdin().lock()).unwrap());
        anyhow::ensure!(!all[key].is_null(),"missing memory input {key}");
        return Ok(all[key].clone());
    }
    Ok(serde_json::from_slice(&fs::read(p)?)?)
}
'''


def prepare():
    RUN.mkdir(exist_ok=True)
    assert not (RUN / 'protocol.json').exists(), 'Existing frozen protocol'
    for snap in [SNAP, REPO/'research-strategies/best-a-v1', REPO/'research-strategies/best-a-portfolio-observation-v1']:
        assert all(sha(snap/k) == h for k,h in read(snap/'manifest.json').items())
    fork = RUN/'fork'
    shutil.copytree(SNAP/'generation-fork', fork)
    pool = (fork/'crates/n-core/src/bin/swing_wave_pool.rs').read_text(encoding='utf-8')
    pool = replace(pool, 'fn read(p:impl AsRef<Path>)->Result<Value>{Ok(serde_json::from_slice(&fs::read(p)?)?) }', MEMREAD)
    old = '''            if (period=="early"&&!("2026-01-01 00:00:00"..="2026-06-30 15:00:00").contains(&e.warning_ts.as_str()))||(period=="recent"&&!("2026-07-01 00:00:00"..="2026-10-23 11:05:00").contains(&e.warning_ts.as_str())){continue;}'''
    pool = replace(pool, old, '''            if !(p["warning_start"].as_str().unwrap()..=p["warning_end"].as_str().unwrap()).contains(&e.warning_ts.as_str()){continue;}''')
    (fork/'crates/n-core/src/bin/swing_older_pool.rs').write_text(pool, encoding='utf-8')
    exe = (SNAP/'execution-fork/crates/n-core/src/bin/swing_entry_economics_execution.rs').read_text(encoding='utf-8')
    exe = replace(exe, '''fn read(p: impl AsRef<Path>) -> Result<Value> {
    Ok(serde_json::from_slice(&fs::read(p)?)?)
}''', MEMREAD.strip())
    (fork/'crates/n-core/src/bin/swing_older_execution.rs').write_text(exe, encoding='utf-8')
    # Economic execution code is identical; only input transport changes.
    diffs=[]
    for f in (SNAP/'execution-fork/crates/n-core/src').rglob('*.rs'):
        if 'bin' in f.relative_to(SNAP/'execution-fork/crates/n-core/src').parts: continue
        other=fork/f.relative_to(SNAP/'execution-fork')
        if other.exists() and f.read_bytes()!=other.read_bytes():diffs.append(f.relative_to(SNAP/'execution-fork').as_posix())
    assert set(diffs)=={'crates/n-core/src/analyze/event.rs','crates/n-core/src/analyze/pattern.rs'}
    original = read(SNAP/'budget_body.protocol.json')
    specs = read(BASE/'input.json')
    p = dict(id=RUN.name, created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        requested_warning_start=START, requested_warning_end=END, warmup_start=WARMUP, execution_tail=TAIL,
        fixed_models=['B0','B1','B2','B3','B4','B5'],
        selection_basis='B3/B4/B5 are highest development-profit candidates; retain B2 economic control, B0 original and B1 ablation. No new parameters.',
        modes=['corrected_touch','next_minute_open'], slips=[1,2], policies=['original','two_paths_flat'],
        data_source='Authenticated ordinary TqBacktest/TqSim minute subscription; in-memory research; no DataDownloader/EDB export',
        warning_timestamps='Closed-minute END timestamps, SDK start timestamp +1 minute. Full five-minute groups only, unchanged service15m aggregation and dedup.',
        fees='Frozen20261022 fee model, not reconstructed2025actual bills; unchanged original1tick planning costs; execution stress1/2ticks per side.',
        chronology='All potential opportunities compete in trigger-time order; no month resets. Unknown PnL remains unknown.',
        evidence='First test of these fixed versions on this older window in this batch; not proof of untouched historical holdout or independence.',
        specs={s:{k:z[k] for k in ['variety','tick_size']} for s,z in specs['symbols'].items()},
        execution_config=specs['execution_config'], templates=original['templates'],
        frozen_references={str(s):sha(s/'manifest.json') for s in [SNAP,REPO/'research-strategies/best-a-v1',REPO/'research-strategies/best-a-portfolio-observation-v1']},
        fees_path=original['periods']['early']['fees'], fees_sha256=sha(original['periods']['early']['fees']),
        continuous_table_path=str(TABLE), continuous_table_sha256=sha(TABLE),
        production_before=original['production_before'], production_changed=False, qualified_for_trading=False)
    write(RUN/'protocol.json', p)
    windows=[]
    for name,last,reopen in [('国庆中秋','2025-09-30','2025-10-09'),('元旦','2025-12-31','2026-01-05')]:
        windows.append(dict(name=name,block_from=last+' 14:30:00',flat_at=last+' 14:45:00',last_close=last+' 15:00:00',reopen=reopen+' 09:00:00'))
    write(RUN/'holidays.json',dict(windows=windows,sources=['https://www.shfe.com.cn/publicnotice/notice/202412/t20241223_824109.html','https://www.shfe.com.cn/services/calenderandholidays/holiday/']))
    write(RUN/'implementation-manifest.json',{f.relative_to(fork).as_posix():sha(f) for f in fork.rglob('*') if f.is_file()})
    print('Frozen six baselines and two memory-input adapters', flush=True)


def build():
    args=['--offline','--manifest-path',str(RUN/'fork/Cargo.toml'),'--target-dir',str(TARGET),'-p','n-core']
    subprocess.run(['cargo','test',*args,'--bin','swing_older_execution','--test','swing_wave_selection','--','--test-threads=1'],check=True)
    subprocess.run(['cargo','build','--release',*args,'--bin','swing_older_pool','--bin','swing_older_execution'],check=True)
    write(RUN/'binaries.json',{n:sha(TARGET/'release'/n) for n in ['swing_older_pool.exe','swing_older_execution.exe']})


def requirements(p):
    table=read(TABLE); ranges={}; selected={}
    for sym in p['specs']:
        product=sym.rstrip('0123456789');ex=EXCH[product]
        var=ex+'.'+(product if ex=='CZCE' else product.lower())
        entries=table[var]; ds=[str(z[0]) for z in entries]
        a=bisect.bisect_right(ds,WARMUP[:10].replace('-',''))-1
        assert a>=0
        keep=entries[a:bisect.bisect_right(ds,TAIL[:10].replace('-',''))]
        selected[sym]=keep
        for i,(date,code) in enumerate(keep):
            first=max(WARMUP,(dt.datetime.strptime(str(date),'%Y%m%d')-dt.timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S'))
            last=min(TAIL,(dt.datetime.strptime(str(keep[i+1][0]),'%Y%m%d')+dt.timedelta(days=14)).strftime('%Y-%m-%d 23:59:59')) if i+1<len(keep) else TAIL
            if code in ranges:first=min(first,ranges[code][0]);last=max(last,ranges[code][1])
            ranges[code]=(first,last)
    return selected,ranges


def collect(p,max_minutes):
    """Ordinary SDK backtest, keep prices only in this process's memory."""
    import numpy as np
    from tqsdk import TqApi,TqAuth,TqBacktest,TqSim,BacktestFinished
    mapping,ranges=requirements(p)
    print('SDK backtest actual contracts',len(ranges),'products',len(mapping), flush=True)
    cfg=read(REPO/'data/config.json')['data_source'];secrets=read(REPO/'data/secrets.json')
    username=cfg['tq_account'];password=cfg.get('tq_password') or secrets.get('tq_password')
    api=None;rows={c:[] for c in ranges};last_ids={};seen={};finished=False;calls=0
    zone=dt.timezone(dt.timedelta(hours=8));started=time.monotonic();last_print=started
    limit_ns=int(dt.datetime.fromisoformat(WARMUP).replace(tzinfo=zone).timestamp())*10**9
    try:
        api=TqApi(TqSim(),auth=TqAuth(username,password),backtest=TqBacktest(start_dt=dt.date(2025,8,1),end_dt=dt.date(2026,1,16)),debug=False,disable_print=True,web_gui=False)
        password=None
        # Future contracts may not yet have any data at the backtest's start.
        # Request them a week before their supplier-recorded main transition,
        # keeping the full normal backtest stream moving in chronological order.
        frames={}
        for c,span in ranges.items():
            if span[0]<=WARMUP:frames[c]=api.get_kline_serial(c,60,data_length=128)
        print('Initially active minute subscriptions',len(frames),flush=True)
        dates={c:f.datetime.values for c,f in frames.items()}
        fields=['id','open','high','low','close','volume','close_oi']
        buffers={c:{k:f[k].values for k in fields} for c,f in frames.items()}
        while time.monotonic()-started<max_minutes*60:
            api.wait_update(deadline=time.time()+5);calls+=1
            clock_ns=max(float(v[-1]) for v in dates.values())
            clock=(dt.datetime.fromtimestamp(int(clock_ns)//10**9,zone).replace(tzinfo=None)+dt.timedelta(minutes=1)).isoformat(sep=' ')
            for c,span in ranges.items():
                if c not in frames and span[0]<=clock:
                    frames[c]=api.get_kline_serial(c,60,data_length=128);dates[c]=frames[c].datetime.values
                    buffers[c]={k:frames[c][k].values for k in fields}
                    print('Added active contract',c,'at',clock,flush=True)
            for code,frame in frames.items():
                newest=int(dates[code][-2])
                if newest<limit_ns or newest<=seen.get(code,0):continue
                buf=buffers[code];times=dates[code][:-1]
                indices=np.flatnonzero((times>seen.get(code,0)) & (times>=limit_ns) & (buf['id'][:-1]>=0))
                for i in indices:
                    ns=int(times[i]);rid=int(buf['id'][i])
                    if code in last_ids:assert rid==last_ids[code]+1,('SDK buffer skipped',code,last_ids[code],rid)
                    seen[code]=ns;last_ids[code]=rid
                    ts=(dt.datetime.fromtimestamp(ns//10**9,zone).replace(tzinfo=None)+dt.timedelta(minutes=1)).isoformat(sep=' ')
                    if not ranges[code][0]<=ts<=ranges[code][1]:continue
                    vals={k:float(buf[k][i]) for k in ['open','high','low','close','volume']}
                    if not all(math.isfinite(x) for x in vals.values()):continue
                    assert vals['low']<=min(vals['open'],vals['close'])<=max(vals['open'],vals['close'])<=vals['high'] and vals['volume']>=0
                    oi=float(buf['close_oi'][i]);assert math.isfinite(oi)
                    rows[code].append(dict(datetime=ts,**vals,hold=oi))
            now=time.monotonic()
            if now-last_print>=15:
                at=max(seen.values(),default=0)
                print('SDK',dt.datetime.fromtimestamp(at//10**9,zone).isoformat() if at else 'waiting','stored',sum(map(len,rows.values())),'updates',calls,'elapsed_sec',round(now-started),flush=True)
                last_print=now
        raise TimeoutError('Backtest stream did not finish within specified time')
    except BacktestFinished:
        finished=True
    finally:
        password=None
        if api is not None:api.close()
    assert finished and all(rows.values()),'Incomplete required actual-contract streams'
    audit={c:dict(minutes=len(bs),first=bs[0]['datetime'],last=bs[-1]['datetime'],canonical_sha256=digest(bs)) for c,bs in rows.items()}
    write(RUN/'actual-contract-coverage.json',dict(contracts=audit,complete_backtest=True,elapsed_seconds=time.monotonic()-started,api_updates=calls,raw_prices_saved=False))
    return mapping,{c:dict(minutes=bs) for c,bs in rows.items()}


def aggregate(rows):
    groups=collections.defaultdict(list)
    for b in rows:
        t=dt.datetime.fromisoformat(b['datetime']);end=t+dt.timedelta(minutes=(-t.minute)%5);groups[end].append(b)
    out=[]
    for end,bs in sorted(groups.items()):
        expect=[(end-dt.timedelta(minutes=i)).isoformat(sep=' ') for i in reversed(range(5))]
        if [b['datetime'] for b in bs]!=expect:continue
        out.append(dict(datetime=end.isoformat(sep=' '),open=bs[0]['open'],high=max(b['high'] for b in bs),low=min(b['low'] for b in bs),close=bs[-1]['close'],volume=sum(b['volume'] for b in bs),hold=bs[-1]['hold']))
    return out


def stitch(p,mapping,contracts):
    dates=sorted({b['datetime'][:10] for c in contracts.values() for b in c['minutes'] if '09:00:00'<=b['datetime'][11:]<='15:00:00'})
    def day(ts):
        i=bisect.bisect_right(dates,ts[:10]) if ts[11:]>='20:00:00' else bisect.bisect_left(dates,ts[:10])
        return dates[i].replace('-','') if i<len(dates) else None
    inp=dict(execution_config=p['execution_config'],symbols={});maps={};audit={}
    for sym,entries in mapping.items():
        ds=[str(z[0]) for z in entries];good=[]
        for code in {z[1] for z in entries}:
            for b in contracts[code]['minutes']:
                d=day(b['datetime'])
                if not d or not WARMUP<=b['datetime']<=TAIL:continue
                i=bisect.bisect_right(ds,d)-1
                if i>=0 and entries[i][1]==code:good.append((b,code))
        good.sort(key=lambda z:z[0]['datetime']);times=[b['datetime'] for b,c in good]
        assert times==sorted(set(times)) and times[0][:10]=='2025-08-01' and times[-1][:10]>='2026-01-15',(sym,times[:1],times[-1:])
        groups=[]
        for b,code in good:
            if not groups or groups[-1]['underlying']!=code:groups.append(dict(underlying=code,first=b['datetime'],last=b['datetime'],n=1))
            else:groups[-1]['last']=b['datetime'];groups[-1]['n']+=1
        rolls=[dict(symbol=sym,ts=b['first'],from_contract=a['underlying'],to_contract=b['underlying'],confirmed=True) for a,b in zip(groups,groups[1:])]
        bs=aggregate([b for b,c in good]);inp['symbols'][sym]=dict(**p['specs'][sym],bars=bs,rollovers=rolls);maps[sym]=groups
        monthly=collections.Counter(b['datetime'][:7] for b,c in good if START<=b['datetime']<=END)
        assert all(monthly['2025-'+str(i)]>0 for i in range(10,13)) and monthly['2025-09']>0
        audit[sym]=dict(first=times[0],last=times[-1],minutes=len(good),bars5=len(bs),rollovers=len(rolls),scored_month_minutes=dict(sorted(monthly.items())),canonical_5m_sha256=digest(bs))
    old=read(BASE/'input.json');overlap={}
    for sym,z in inp['symbols'].items():
        previous={b['datetime']:b for b in old['symbols'][sym]['bars']};common=0;bad=[]
        for b in z['bars']:
            if b['datetime'] not in previous:continue
            common+=1;before=previous[b['datetime']]
            different=[k for k in ['open','high','low','close','volume','hold'] if abs(b[k]-before[k])>1e-7]
            if different:bad.append(dict(datetime=b['datetime'],fields=different))
        assert common>0,(sym,'no overlap with frozen EDB archive')
        overlap[sym]=dict(compared_5m_bars=common,mismatched_bars=len(bad),first_mismatches=bad[:10])
    write(RUN/'sdk-edb-overlap-audit.json',overlap)
    write(RUN/'data-audit.json',audit);write(RUN/'segments.json',maps);write(RUN/'trading-dates.json',dates)
    return inp,maps


def engine(binary,protocol,stage,out,memory):
    args=[str(TARGET/'release'/binary),str(protocol),'older']
    args+= [str(out.parent/(out.stem+'-symbols')),str(out)] if stage=='pool' else ['replay',str(out)]
    subprocess.run(args,input=json.dumps(memory,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode(),check=True)


def evaluate(p,inp,contracts,maps):
    from report_swing_wave_selection import identification_bars
    from swing_entry_economics import warning_features
    memory=dict(input=inp,contracts=contracts)
    base=copy.deepcopy(p['templates'][0]);base.update(name='P2_economics_base',variant='base',economic_gate=False,body_net_gate=False)
    budget=copy.deepcopy(base);budget.update(name='P2_economics_budget10',variant='budget10',economic_gate=True)
    body=copy.deepcopy(budget);body.update(name='P2_economics_budget_body',variant='budget_body',body_net_gate=True)
    bars={s:identification_bars(z['bars']) for s,z in inp['symbols'].items()}
    for family,selector,templates in [('best_a',2,[base]),('unmerged',4,[base,budget,body])]:
        root=RUN/family;root.mkdir()
        protocol=dict(selector=selector,warning_start=START,warning_end=END,templates=templates,modes=p['modes'],
            hashes={'older':{'input':digest(inp)}},periods={'older':dict(input='memory:input',contracts='memory:contracts',fees=p['fees_path'],segments=str(RUN/'segments.json'),holidays=str(RUN/'holidays.json'))})
        write(root/'protocol.json',protocol)
        engine('swing_older_pool.exe',root/'protocol.json','pool',root/'older-pool.json',dict(input=inp))
        pool=read(root/'older-pool.json')
        for z in pool:
            e=z['event'];assert e['evaluation'] is None and e['outcome'] is None and e['entry_ts'] is None
            z['entry_quality']=warning_features(e,bars[e['symbol']])
        # Derived warning pool is saved separately; the unlabelled source is retained.
        (root/'older-pool.json').rename(root/'older-pool-unlabelled.json')
        write(root/'older-pool.json',pool)
        engine('swing_older_execution.exe',root/'protocol.json','replay',root/'older',memory)


def verify():
    """Reproduce frozen EG early pools and complete executions on old prices."""
    from report_swing_wave_selection import identification_bars
    from swing_entry_economics import warning_features
    p=read(RUN/'protocol.json');inp=read(BASE/'input.json')
    inp['symbols']={s:z for s,z in inp['symbols'].items() if s=='EG0'}
    contracts=read(BASE/'contracts.json')
    contracts={c:z for c,z in contracts.items() if c.startswith('DCE.eg')}
    bars=identification_bars(inp['symbols']['EG0']['bars'])
    checks=[]
    for family,selector,expected_root in [('best_a',2,REPO/'research-runs/swing-retirement-20261025-v1/near_start'),('unmerged',4,REPO/'research-runs/swing-c-structure-20261025-v1/candidate')]:
        root=RUN/('control-'+family);root.mkdir()
        native=read(SNAP/'budget_body.protocol.json')
        ts=[]
        for profile in (['base'] if family=='best_a' else ['base','budget10','budget_body']):
            t=copy.deepcopy(native['templates'][0]);t.update(name='P2_economics_'+profile,variant=profile,economic_gate=profile!='base',body_net_gate=profile=='budget_body');ts.append(t)
        paths=dict(native['periods']['early']);paths.update(input='memory:input',contracts='memory:contracts')
        protocol=dict(selector=selector,warning_start='2026-01-01 00:00:00',warning_end='2026-06-30 15:00:00',templates=ts,modes=p['modes'],hashes={'older':{'input':digest(inp)}},periods={'older':paths})
        write(root/'protocol.json',protocol)
        engine('swing_older_pool.exe',root/'protocol.json','pool',root/'older-pool.json',dict(input=inp))
        pool=read(root/'older-pool.json');expected=[z for z in read(expected_root/'early-pool.json') if z['event']['symbol']=='EG0']
        assert pool==expected,(family,'generated pool differs from frozen archive')
        for z in pool:z['entry_quality']=warning_features(z['event'],bars)
        (root/'older-pool.json').rename(root/'older-pool-unlabelled.json');write(root/'older-pool.json',pool)
        engine('swing_older_execution.exe',root/'protocol.json','replay',root/'older',dict(input=inp,contracts=contracts))
        expected_file=(expected_root/'early/evaluations.jsonl') if family=='best_a' else REPO/'research-runs/swing-entry-economics-20261025-v1/native-verification/early/evaluations.jsonl'
        expected_rows={}
        for line in expected_file.open(encoding='utf-8'):
            z=json.loads(line)
            if z['symbol']!='EG0':continue
            prefix,sf=z['scenario'].split('/',1)
            if family=='best_a' and prefix!='P2_wave_adaptive':continue
            if family=='best_a':prefix='P2_economics_base'
            expected_rows[(z['event_key'],prefix+'/'+sf)]=z
        count=0
        for line in (root/'older/evaluations.jsonl').open(encoding='utf-8'):
            z=json.loads(line);ref=expected_rows.pop((z['event_key'],z['scenario']))
            for k in ['result','execution_contract','new_friction','planned_risk','entry','stop','target','local_level_time']:
                assert z[k]==ref[k],(family,z['event_key'],z['scenario'],k)
            count+=1
        assert not expected_rows
        checks.append(dict(family=family,pool_size=len(pool),exact_execution_rows=count,pool_exact=True,all_complete_results_exact=True))
    write(RUN/'regression-verification.json',dict(checks=checks,isolated_build=True))
    print('Frozen EG controls reproduced exactly',checks,flush=True)


def run(max_minutes):
    p=read(RUN/'protocol.json')
    assert all(sha(RUN/'fork'/k)==h for k,h in read(RUN/'implementation-manifest.json').items())
    assert sha(TABLE)==p['continuous_table_sha256'] and sha(p['fees_path'])==p['fees_sha256']
    mapping,contracts=collect(p,max_minutes)
    deadline=time.monotonic()+900
    while not (RUN/'binaries.json').exists():
        assert time.monotonic()<deadline,'Isolated engine build not ready'
        time.sleep(1)
    assert all(sha(TARGET/'release'/n)==h for n,h in read(RUN/'binaries.json').items())
    inp,maps=stitch(p,mapping,contracts)
    evaluate(p,inp,contracts,maps)
    assert all(sha(k)==v for k,v in p['production_before'].items())
    print('All older individual executions finished; raw minutes discarded after process exit.',flush=True)


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('stage',choices=['prepare','build','run','verify']);ap.add_argument('--max-minutes',type=int,default=90);args=ap.parse_args()
    run(args.max_minutes) if args.stage=='run' else globals()[args.stage]()
