"""Isolated build / approved cutover. Never imports or calls a trading API."""
import datetime as dt
import hashlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import time

STAGE = pathlib.Path(__file__).resolve().parent
LIVE = pathlib.Path('/home/ubuntu/pjf/N-Trend').resolve()
CARGO = '/home/ubuntu/.cargo/bin/cargo'
FILES = [
    'crates/n-core/Cargo.toml', 'crates/n-core/src/trader/execution.rs',
    'crates/n-core/src/trader/execution_replay.rs', 'crates/n-core/src/trader/mod.rs',
    'crates/n-core/src/trader/overnight.rs', 'crates/n-core/src/trader/trade_time.rs',
    'crates/n-core/src/trader/execution_trade_time.rs', 'crates/n-core/src/trader/execution_trade_time_tests.rs',
    'crates/n-core/examples/normalize_execution_archive.rs', 'crates/n-core/examples/normalize_overnight_archive.rs',
    'crates/n-core/examples/verify_trade_time_fix.rs',
]

def run(args, **kwargs):
    subprocess.run(args, check=True, **kwargs)

def status(**fields):
    (STAGE/'status.json').write_text(json.dumps({'at':dt.datetime.now().astimezone().isoformat(), **fields}))

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def atomic_copy(source, destination, mode=None):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if mode is None and destination.exists(): mode = stat.S_IMODE(destination.stat().st_mode)
    temporary = destination.with_name(destination.name + '.time-fix-new')
    shutil.copy2(source, temporary)
    if mode is not None: temporary.chmod(mode)
    os.replace(temporary, destination)

def build():
    status(phase='copying_build_cache')
    cache = STAGE/'target'
    cache.mkdir(exist_ok=True)
    assert cache.resolve().is_relative_to(STAGE)
    if not (cache/'release').exists():
        run(['cp', '-a', '--reflink=auto', str(LIVE/'target/release'), str(cache/'release')])
    env = {**os.environ, 'CARGO_TARGET_DIR':str(cache), 'CARGO_BUILD_JOBS':'1'}
    for phase, args in [
        ('linux_time_regressions', ['test','-p','n-core','--example','verify_trade_time_fix','--release','--offline','--quiet']),
        ('linux_headless_tests', ['test','-p','n-headless','--release','--offline','--quiet']),
        ('linux_server_build', ['build','-p','n-headless','--release','--offline','--quiet']),
        ('linux_migration_build', ['build','-p','n-core','--release','--offline','--example','normalize_execution_archive','--example','normalize_overnight_archive','--quiet']),
    ]:
        status(phase=phase)
        run(['nice','-n','15',CARGO,*args], cwd=STAGE/'source', env=env)
    status(phase='ready', binary_sha256=sha(cache/'release/n-headless'))

def apply():
    assert json.loads((STAGE/'status.json').read_text())['phase']=='ready', 'build is not verified'
    assert not subprocess.check_output(['git','-C',str(LIVE),'diff','--name-only','--','crates/n-core','crates/n-headless','Cargo.lock'],text=True).strip(), 'production source changed during build'
    backup = STAGE/'backup'
    backup.mkdir(mode=0o700)
    binary = LIVE/'target/release/n-headless'
    shutil.copy2(binary, backup/'n-headless')
    existed = []
    for name in FILES:
        src = LIVE/name
        if src.exists():
            target=backup/'source'/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,target);existed.append(name)
    manifest={'at':dt.datetime.now().astimezone().isoformat(),'old_binary_sha256':sha(binary),'existing_sources':existed,'states':{},'journals':{}}
    (backup/'manifest.json').write_text(json.dumps(manifest,indent=2))
    status(phase='stopping_service')
    run(['systemctl','--user','stop','ntrend-server.service'])
    service = subprocess.run(['systemctl','--user','is-active','ntrend-server.service'],capture_output=True,text=True)
    assert service.stdout.strip()=='inactive', service.stdout
    start_attempted = False
    try:
        status(phase='migrating_final_state')
        for path in (LIVE/'data/trader_instances').glob('*/trader_execution.json'):
            ident=path.parent.name;dest=backup/'data'/ident;dest.mkdir(parents=True)
            shutil.copy2(path,dest/path.name);manifest['states'][ident]=sha(path)
            repaired=STAGE/'repaired'/ident;repaired.mkdir(parents=True)
            run([str(STAGE/'target/release/examples/normalize_execution_archive'),str(path),str(repaired/path.name),str(LIVE/'data/trading_calendar.json'),str(STAGE/'contract-opens.json')])
            before=json.loads(path.read_text());after=json.loads((repaired/path.name).read_text())
            for key in ['orders','closes','seen_trades','conditions','account','trading_day','loss_r']:assert before.get(key)==after.get(key),(ident,key)
            assert len(before['history'])==len(after['history'])
            for a,b in zip(before['history'],after['history']):
                for field in ['id','volume','fill_price','exit_price','risk','r_multiple','realized_pnl_pts','realized_pnl_amount','exit_reason']:assert a.get(field)==b.get(field),(ident,field)
            for pid,a in before['positions'].items():
                b=after['positions'][pid]
                for field in ['volume','fill_price','initial_stop_price','current_stop_price','risk','is_closed','exit_price']:assert a.get(field)==b.get(field),(ident,pid,field)
            for name in ['overnight_tracking.jsonl','overnight_attribution.jsonl']:
                journal=path.parent/name
                if journal.exists():
                    shutil.copy2(journal,dest/name);manifest['journals'][ident+'/'+name]=sha(journal)
                    run([str(STAGE/'target/release/examples/normalize_overnight_archive'),str(journal),str(repaired/name),str(repaired/path.name),str(LIVE/'data/trading_calendar.json'),str(STAGE/'contract-opens.json')])
        (backup/'manifest.json').write_text(json.dumps(manifest,indent=2))
        for ident in manifest['states']:
            target=LIVE/'data/trader_instances'/ident;repaired=STAGE/'repaired'/ident
            atomic_copy(repaired/'trader_execution.json',target/'trader_execution.json')
            for name in ['overnight_tracking.jsonl','overnight_attribution.jsonl']:
                if (repaired/name).exists():
                    with (target/name).open('a') as f:f.write((repaired/name).read_text());f.flush();os.fsync(f.fileno())
        for name in FILES:atomic_copy(STAGE/'source'/name,LIVE/name)
        atomic_copy(STAGE/'target/release/n-headless',binary,0o755)
        start_attempted = True
        run(['systemctl','--user','start','ntrend-server.service'])
        time.sleep(2)
        run(['systemctl','--user','is-active','--quiet','ntrend-server.service'])
        status(phase='deployed',binary_sha256=sha(binary),instances=len(manifest['states']),backup=str(backup))
    except BaseException as e:
        # Roll back only while the service has not processed fresh events. Once
        # started, do not overwrite its newer ledger with a pre-start backup.
        active=subprocess.run(['systemctl','--user','is-active','--quiet','ntrend-server.service']).returncode==0
        if not start_attempted:
            for ident in manifest['states']:
                for saved in (backup/'data'/ident).iterdir():atomic_copy(saved,LIVE/'data/trader_instances'/ident/saved.name)
            for name in existed:atomic_copy(backup/'source'/name,LIVE/name)
            atomic_copy(backup/'n-headless',binary,0o755)
            subprocess.run(['systemctl','--user','start','ntrend-server.service'])
        status(phase='cutover_error',error=str(e),service_active=active,backup=str(backup))
        raise

if __name__=='__main__':
    assert STAGE.parent==pathlib.Path('/home/ubuntu/pjf/ntrend-deployments').resolve()
    try:
        if sys.argv[1]=='build':build()
        elif sys.argv[1]=='apply':apply()
        else:raise ValueError('expected build or apply')
    except Exception as e:
        if sys.argv[1]=='build':status(phase='build_failed',error=str(e))
        raise
