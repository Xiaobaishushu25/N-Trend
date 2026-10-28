"""Frozen finite relaxations on JD/SR/CJ/SI; no production mutation or raw export."""
import argparse
import collections
import copy
import datetime as dt
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import research_swing_monthly_products as m

ROOT = m.REPO
RUN = ROOT / 'research-runs/swing-four-product-relaxation-20261026-v1'
FORK = RUN / 'fork'
TARGET = ROOT / 'target/swing-four-product-relaxation-isolated'
SYMS = ['JD0', 'SR0', 'CJ0', 'SI0']
EXE = TARGET / 'release/swing_product_relaxation.exe'
VARIANTS = {
    'C_control': dict(min_risk_ticks=10., max_friction=.20, max_cost_space=.10, min_net_rr=1., local=True),
    'E_cost_space20': dict(max_cost_space=.20),
    'F_risk8_friction30': dict(min_risk_ticks=8., max_friction=.30),
    'Q_net_rr08': dict(min_net_rr=.8),
    'L_original_S1': dict(local=False),
    'M_mild_combination': dict(min_risk_ticks=8., max_friction=.30, max_cost_space=.20, min_net_rr=.8),
    'D_unrestricted_diagnostic': dict(min_risk_ticks=1., max_friction=1e100, economic_gate=False,
                                     body_net_gate=False, diagnostic_open=True),
}


def write(path, obj):
    m.old.write(path, obj)


def prepare():
    RUN.mkdir(exist_ok=False)
    snap = m.old.SNAP
    assert all(m.old.sha(snap/k)==h for k,h in m.old.read(snap/'manifest.json').items())
    original = m.old.read(snap / 'budget_body.protocol.json')
    templates = []
    for name, changes in VARIANTS.items():
        template = copy.deepcopy(original['templates'][0])
        template.update(VARIANTS['C_control'])
        template.update(changes, name=name)
        templates.append(template)
    p = dict(id=RUN.name, created_at=dt.datetime.now().astimezone().isoformat(), symbols=SYMS,
        templates=templates, modes=['corrected_touch', 'next_minute_open'], slips=[1,2],
        policies=['original', 'two_paths_flat'], periods=original['periods'],
        primary='original/corrected_touch/slip1',
        purpose='Finite same-threshold four-product diagnostics; replace only JD/SR/CJ/SI executions in full B5 universe; all other product executions/rules fixed.',
        variants=VARIANTS,
        diagnostic='D lifts plan risk/friction/economics/body/netRR gates to label originally rejected candidate opportunities under SAME local target and SAME single-lot execution/exit. It is NOT a recommended portfolio. Untriggered/invalid/ambiguous outcomes remain separate, never zero-profit trades.',
        no_grid='One preset value per named relaxation, shared by all four products, declared before new outcomes. Existing data have been repeatedly researched; no independent holdout claim.',
        temporal='Chronological trigger-time recompetition including pending/unknown reservations, same-symbol slot, counter-hour sector2 and two original stops; no month resets. Primary original unknown archive-end occupancy plus alternate two-monotone-paths-flat sensitivity.',
        costs='Frozen 20261022 fees; SR explicit2yuan roundtrip; slippage embedded once per fill. Planning uses frozen1tick assumptions in every execution stress.',
        unchanged='Same warning pools, >=2.5 score, adaptive confirmation, initial stop geometry, holiday/weekend/data guards, chase limit, no-follow and holding exits. L alone changes target to original S1. No position-size increase.',
        older_source='Ordinary authenticated TqBacktest, four products only, raw minutes stay in memory. Require exact per-contract canonical hashes versus original acquisition before evaluation.',
        production_before={str(q):m.old.sha(q) for q in ['crates/n-core/src/trader/candidates.rs','crates/n-core/src/trader/candidate_execution_tests.rs','src/stores/liveTrader.ts','src/types.ts','src/views/LiveTraderView.vue','tests/liveTrader.test.cjs']},
        references={str(m.old.RUN/'completion.json'):m.old.sha(m.old.RUN/'completion.json'),
                    str(m.RUN/'completion.json'):m.old.sha(m.RUN/'completion.json')},
        fees_path=original['periods']['early']['fees'], production_changed=False,
        script_sha256=m.old.sha(__file__))
    write(RUN/'protocol.json', p)
    # All library code remains the frozen execution snapshot; only the research caller changes.
    shutil.copytree(snap/'execution-fork', FORK)
    src = (FORK/'crates/n-core/src/bin/swing_entry_economics_execution.rs').read_text(encoding='utf-8')
    replace = m.old.replace
    src = replace(src, '''fn read(p: impl AsRef<Path>) -> Result<Value> {
    Ok(serde_json::from_slice(&fs::read(p)?)?)
}''', m.old.MEMREAD.strip())
    src = replace(src, '''fn room_covers_cost(entry:f64,target:f64,sign:f64,cost:f64)->bool{
    let space=(target-entry)*sign;
    space.is_finite()&&cost.is_finite()&&space>0.&&cost>=0.&&cost/space<=0.10+1e-12
}''', '''fn room_covers_cost_limit(entry:f64,target:f64,sign:f64,cost:f64,limit:f64)->bool{
    let space=(target-entry)*sign;
    space.is_finite()&&cost.is_finite()&&space>0.&&cost>=0.&&cost/space<=limit+1e-12
}
#[cfg(test)] fn room_covers_cost(entry:f64,target:f64,sign:f64,cost:f64)->bool{
    room_covers_cost_limit(entry,target,sign,cost,0.10)
}''')
    src = replace(src, '            let geometry_ok = ev.risk / ev.tick_size + 1e-6 >= 10. && new_friction <= 0.2 + 1e-9;', '''            let min_risk=template["min_risk_ticks"].as_f64().unwrap_or(10.);
            cfg.min_entry_risk_ticks=min_risk as _;
            let max_friction=template["max_friction"].as_f64().unwrap_or(0.2);
            let geometry_ok = ev.risk / ev.tick_size + 1e-6 >= min_risk && new_friction <= max_friction + 1e-9;''')
    src = replace(src, 'room_covers_cost(ev.entry,ev.target,if ev.direction=="up"{1.}else{-1.},new_friction*ev.risk)', 'room_covers_cost_limit(ev.entry,ev.target,if ev.direction=="up"{1.}else{-1.},new_friction*ev.risk,template["max_cost_space"].as_f64().unwrap_or(0.10))')
    src = replace(src, '                rules.next_minute_open = mode == "next_minute_open";', '                rules.next_minute_open = mode == "next_minute_open";\n                rules.min_actual_risk_ticks=Some(min_risk);')
    src = replace(src, '                            Some(g),', '                            if template["diagnostic_open"]==true {None} else {Some(g)},')
    src += '''
#[cfg(test)] mod relaxation_tests {
    use super::*;
    #[test] fn relaxed_cost_threshold_keeps_boundary_and_direction() {
        assert!(!room_covers_cost_limit(100.,110.,1.,1.5,0.1));
        assert!(room_covers_cost_limit(100.,110.,1.,1.5,0.2));
        assert!(room_covers_cost_limit(100.,90.,-1.,2.,0.2));
        assert!(!room_covers_cost_limit(100.,90.,-1.,2.01,0.2));
        assert!(!room_covers_cost_limit(100.,100.,1.,0.,0.2));
    }
}
'''
    target_src=FORK/'crates/n-core/src/bin/swing_product_relaxation.rs'
    target_src.write_text(src,encoding='utf-8')
    write(RUN/'implementation-manifest.json', {f.relative_to(FORK).as_posix():m.old.sha(f) for f in FORK.rglob('*') if f.is_file()})
    # Publish all-product counts immediately; do not defer this part to market acquisition.
    freq=m.old.read(m.RUN/'frequencies.json')
    write(RUN/'all-product-counts.json', {model:{s:dict(name=m.NAMES[s],n=v['n'],net=v['net']) for s,v in fs.items()} for model,fs in freq.items()})
    print('Frozen 5 relaxations, control and one outcome diagnostic; no per-product tuning.',flush=True)


def build():
    args=['--release','--offline','--manifest-path',str(FORK/'Cargo.toml'),'--target-dir',str(TARGET),'-p','n-core','--bin','swing_product_relaxation']
    subprocess.run(['cargo','test',*args],check=True)
    subprocess.run(['cargo','build',*args],check=True)
    write(RUN/'binary.json',dict(path=str(EXE),sha256=m.old.sha(EXE)))


def replay(period, inp, contracts, paths, pool):
    root=RUN/period
    root.mkdir()
    write(root/(period+'-pool.json'),pool)
    p=m.old.read(RUN/'protocol.json')
    paths=dict(paths,input='memory:input',contracts='memory:contracts')
    protocol=dict(templates=p['templates'],modes=p['modes'],periods={period:paths})
    write(root/'execution.protocol.json',protocol)
    deadline=time.monotonic()+1800
    while not (RUN/'binary.json').exists():
        if time.monotonic()>deadline:raise TimeoutError('Isolated research engine not ready')
        time.sleep(1)
    assert m.old.sha(EXE)==m.old.read(RUN/'binary.json')['sha256']
    payload=json.dumps(dict(input=inp,contracts=contracts),ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
    subprocess.run([str(EXE),str(root/'execution.protocol.json'),period,'replay',str(root/'executions')],input=payload,check=True)


def run_2026():
    p=m.old.read(RUN/'protocol.json')
    roots=ROOT/'research-runs/swing-entry-economics-20261025-v1/native-verification'
    prefixes=['DCE.jd','CZCE.SR','CZCE.CJ','GFEX.si']
    for period in ['early','recent']:
        paths=p['periods'][period]
        pool=[z for z in m.old.read(roots/(period+'-pool.json')) if z['event']['symbol'] in SYMS]
        raw=m.old.read(paths['input'])
        inp=dict(execution_config=raw['execution_config'],symbols={s:{k:raw['symbols'][s][k] for k in ['variety','tick_size']} for s in SYMS})
        del raw
        contracts={c:z for c,z in m.old.read(paths['contracts']).items() if c.startswith(tuple(prefixes))}
        replay(period,inp,contracts,paths,pool)
        print(period,'all individual relaxations finished',flush=True)


def run_older(max_minutes):
    p=m.old.read(m.old.RUN/'protocol.json')
    p['specs']={s:p['specs'][s] for s in SYMS}
    original_run=m.old.RUN
    dest=RUN/'older-acquisition'
    dest.mkdir(exist_ok=True)
    assert not (dest/'actual-contract-coverage.json').exists(), 'Acquisition already completed'
    try:
        m.old.RUN=dest
        _,contracts=m.old.collect(p,max_minutes)
    finally:
        m.old.RUN=original_run
    reference=m.old.read(original_run/'actual-contract-coverage.json')['contracts']
    observed=m.old.read(dest/'actual-contract-coverage.json')['contracts']
    assert all(v==reference[c] for c,v in observed.items()),'Older raw minutes differ from frozen source'
    write(dest/'source-equality.json',dict(exact_contracts=len(observed),canonical_minute_hashes_match=True,raw_prices_saved=False))
    protocol=m.old.read(original_run/'unmerged/protocol.json')
    paths=dict(protocol['periods']['older'])
    inp=dict(execution_config=p['execution_config'],symbols=p['specs'])
    pool=[z for z in m.old.read(original_run/'unmerged/older-pool.json') if z['event']['symbol'] in SYMS]
    replay('older',inp,contracts,paths,pool)
    print('Older individual relaxations finished; raw minutes discarded.',flush=True)


def connection_diagnostic(futures_only=False):
    """Observe connection errors without logging packets, headers or credentials."""
    import re
    import urllib.parse
    import tqsdk.auth
    if futures_only:
        import tqsdk
        original_api=tqsdk.TqApi
        def futures_api(*args,**kwargs):
            kwargs['_stock']=False
            return original_api(*args,**kwargs)
        tqsdk.TqApi=futures_api
        print('Using SDK futures-only metadata mode; same account and backtest service.',flush=True)
    logger_type=tqsdk.auth.ShinnyLoggerAdapter
    cfg=m.old.read(ROOT/'data/config.json')['data_source']
    secrets=m.old.read(ROOT/'data/secrets.json')
    private=[cfg.get('tq_account'),cfg.get('tq_password'),secrets.get('tq_password')]
    packet_counts=collections.Counter()
    def debug(self,msg,*args,**kwargs):
        if msg=='websocket connection closed':
            error=str(kwargs.get('error',''))
            for item in private:
                if item:error=error.replace(item,'[redacted]')
            error=re.sub(r'(?:wss?|https?)://[^\s\"\']+',lambda a:urllib.parse.urlsplit(a.group()).hostname or '[url]',error)
            print('SDK connection diagnostic:',error[:350],flush=True)
        elif msg in ['websocket connected','websocket reconnected']:
            print('SDK connection diagnostic:',msg,flush=True)
        elif msg in ['websocket received data','websocket send data']:
            packet_counts[msg]+=1
            if packet_counts[msg]<=8:
                pack=kwargs.get('pack',{})
                if isinstance(pack,str):
                    try:pack=json.loads(pack)
                    except ValueError:pack={}
                if isinstance(pack,dict):
                    meta=dict(event=msg,keys=list(pack),aid=pack.get('aid'))
                    if isinstance(pack.get('data'),list):
                        meta['data_keys']=[list(x) for x in pack['data'][:3] if isinstance(x,dict)]
                        meta['history_flags']=[x['mdhis_more_data'] for x in pack['data'] if isinstance(x,dict) and 'mdhis_more_data' in x]
                    print('SDK packet structure:',json.dumps(meta),flush=True)
    logger_type.debug=debug
    run_older(40)


def connection_futures():
    connection_diagnostic(futures_only=True)


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('stage',choices=['prepare','build','run_2026','run_older','connection_diagnostic','connection_futures'])
    ap.add_argument('--max-minutes',type=int,default=40)
    a=ap.parse_args()
    run_older(a.max_minutes) if a.stage=='run_older' else globals()[a.stage]()
