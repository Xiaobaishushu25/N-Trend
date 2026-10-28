"""Post-selective exploratory structural exits, with fixed profiles and routes."""
import argparse,collections,copy,datetime as dt,json,pathlib,shutil,subprocess
import research_swing_selective as s
from research_swing_consistency import replace_once
PARENT=s.PARENT
RUN=s.RUN/'structural-supplement'

def patch(root):
    path=root/'crates/n-core/src/v3/ablation.rs';x=path.read_text(encoding='utf-8')
    x=replace_once(x,'use chrono::{Datelike, NaiveDateTime, TimeZone, Timelike};','use chrono::{Datelike, NaiveDateTime, TimeZone, Timelike};\npub mod warning_failure;')
    x=replace_once(x,'    pub entry_mode: EntryMode,','    pub entry_mode: EntryMode,\n    pub body_failure_level: Option<f64>,\n    pub body_failure_count: Option<usize>,')
    x=replace_once(x,'    holidays: Vec<HolidayWindow>,','    holidays: Vec<HolidayWindow>,\n    body_failure: warning_failure::BodyFailure,\n    pending_body_exit: bool,')
    x=replace_once(x,'        holidays: holidays.to_vec(),','        holidays: holidays.to_vec(),\n        body_failure: Default::default(),\n        pending_body_exit: false,')
    x=replace_once(x,'        let had_fill = state.e.fill.is_some();','''        if state.pending_body_exit {
            state.realize(bar.open-sign*ex.slip_ticks*ev.tick_size,state.remaining,"warning_body_failed_close",sign,&bar.datetime);
            break;
        }
        let had_fill = state.e.fill.is_some();''')
    x=replace_once(x,'''    }
    if state.e.status == "open" {''','''        if let (Some(level),Some(count),Some(entry))=(rules.body_failure_level,rules.body_failure_count,state.e.entry_ts.as_deref()) {
            if state.body_failure.observe(bar,entry,level,sign,count){state.pending_body_exit=true;}
        }
    }
    if state.e.status == "open" {''');path.write_text(x,encoding='utf-8')
    sub=root/'crates/n-core/src/v3/ablation';sub.mkdir(exist_ok=True)
    shutil.copy2(s.REPO/'scripts/swing_failure_exit.rs',sub/'warning_failure.rs')
    runner=(PARENT/'fork/crates/n-core/src/bin/swing_cycle_runner.rs').read_text(encoding='utf-8')
    runner=replace_once(runner,'            let new_friction = friction(&ev, fee, mult);','''            if template["half_body"]==true {
                let old=ev.stop;let mut body=ev.clone();geom::body_stop(&mut body,&warning);
                ev.stop=geom::grid((old+body.stop)*0.5,ev.tick_size,ev.direction!="up");geom::normalize(&mut ev);
            }
            let new_friction = friction(&ev, fee, mult);''')
    runner=replace_once(runner,'                rules.next_minute_open = mode == "next_minute_open";','''                rules.next_minute_open = mode == "next_minute_open";
                if let Some(count)=template["close_body_count"].as_u64() {
                    rules.body_failure_level=Some(if ev.direction=="up" {warning.open.min(warning.close)-ev.tick_size}
                        else {warning.open.max(warning.close)+ev.tick_size});
                    rules.body_failure_count=Some(count as usize);
                }''')
    (root/'crates/n-core/src/bin/swing_failure_runner.rs').write_text(runner,encoding='utf-8')

def routes():
    out=[dict(name='base',condition='none',profile='base',budget=False)]
    for name,condition,profile in [('counter_half','counter','half'),('strong_half','strong_counter','half'),('opposed_half','opposed','half'),
        ('counter_close1','counter','close1'),('counter_close2','counter','close2'),('opposed_close1','opposed','close1'),('opposed_close2','opposed','close2'),
        ('graded_body_kind','graded','graded_body'),('graded_half_kind','graded','graded_half'),('counter_body','counter','body')]:
        out.append(dict(name=name,condition=condition,profile=profile,budget=False))
    for name in ('counter_body','counter_half','counter_close1','counter_close2','graded_half_kind'):
        t=dict(next(x for x in out if x['name']==name));t.update(name=name+'_failure_budget',budget=True);out.append(t)
    return out

def choose(t,e):
    st=s.state(e)
    if t['condition']=='graded':
        return ('body' if t['profile']=='graded_body' else 'half') if st['strong_counter'] else 'kind' if st['counter'] else 'base'
    return t['profile'] if st[t['condition']] else 'base'

def failure_portfolio(rows,events,ends,reverse=False):
    # Same chronology and same-cluster restriction as frozen counter_stop;
    # treat this branch's completed-body invalidation as a structural failure too.
    mapped=[]
    for r in rows:
        e=s.ev(r)
        if e['status']=='closed' and e['exit_reason']=='warning_body_failed_close' and e['net_amount']<0:
            rr=dict(r);rr['result']=dict(r['result']);ee=dict(e);ee['exit_reason']='stop';rr['result']['evaluation']=ee;mapped.append(rr)
        else:mapped.append(r)
    a,l=s.counter_stop_portfolio(mapped,events,ends,reverse)
    original={r['event_key']:r for r in rows}
    return [original[r['event_key']] for r in a],l

def prepare():
    RUN.mkdir(parents=True,exist_ok=False);root=RUN/'fork';shutil.copytree(PARENT/'fork',root);patch(root)
    shutil.copy2(s.REPO/'tests/swing_failure.rs',root/'crates/n-core/tests/swing_failure.rs')
    for period in ('early','recent'):shutil.copy2(PARENT/(period+'-pool.json'),RUN/(period+'-pool.json'))
    p=s.read(PARENT/'protocol.json');base=p['templates'][0];templates=[]
    for name,half,count in [('base',False,None),('half',True,None),('close1',False,1),('close2',False,2)]:
        t=copy.deepcopy(base);t.update(name='P2_failure_'+name,variant=name,half_body=half,close_body_count=count);templates.append(t)
    p.update(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),templates=templates,online_windows=[],
        historical='Exploratory AFTER selective18 results. No new threshold search; all data development.',
        half='Stop halfway between original S2 stop and warning-body stop, rounded adversely to valid tick; recheck full nominal friction/min10tick/forecast and actual netRR.',
        close='Keep original S2 hard stop. One/two consecutive FULL post-entry15m closes beyond warning-body edge plus adverse1tick queue exit at next minute open with adverse slip; complete15m means15 consecutive valid minute records. Never use entry minute or incomplete candle. Original stops/targets/no-follow/calendar exits retain priority.',
        routes=routes(),parent_selective_results_sha256=s.sha(s.RUN/'results.json'),script_sha256=s.sha(__file__),
        observer_sha256=s.sha(s.REPO/'scripts/swing_failure_exit.rs'),production_changed=False,qualified_for_trading=False)
    s.write(RUN/'protocol.json',p);s.write(RUN/'implementation-manifest.json',{str(q.relative_to(root)):s.sha(q) for q in root.rglob('*') if q.is_file()})
    print('Frozen4 profiles and16 routes',RUN,flush=True)

def run():
    p=s.read(RUN/'protocol.json');assert s.sha(__file__)==p['script_sha256'];assert s.sha(s.REPO/'scripts/swing_failure_exit.rs')==p['observer_sha256']
    args=['--offline','--manifest-path',str(RUN/'fork/Cargo.toml'),'--target-dir',str(s.REPO/'target'),'-p','n-core']
    subprocess.run(['cargo','test',*args,'--test','swing_failure','--test','swing_cycle'],check=True)
    subprocess.run(['cargo','build',*args,'--bin','swing_failure_runner'],check=True)
    for period in ('early','recent'):
        assert all(s.sha(v)==p['hashes'][period][k] for k,v in p['periods'][period].items())
        subprocess.run([str(s.REPO/'target/debug/swing_failure_runner.exe'),str(RUN/'protocol.json'),period,'replay',str(RUN/period)],check=True)

def evaluate():
    p=s.read(RUN/'protocol.json');assert s.sha(__file__)==p['script_sha256']
    original_counter=s.counter_stop_portfolio
    s.RUN=RUN/'combined';s.RUN.mkdir(exist_ok=False)
    s.PROFILES={k:s.PROFILES[k] for k in ('base','kind','body')}
    s.PROFILES.update({k:(RUN,'P2_failure_'+k) for k in ('half','close1','close2')})
    cp=s.read(PARENT/'protocol.json');cp.update(id=s.RUN.name,templates=p['routes'],script_sha256=s.sha(s.__file__),supervisor_sha256=s.sha(__file__),
        raw_source_sha256={str(root/period/'evaluations.jsonl'):s.sha(root/period/'evaluations.jsonl') for root in {v[0] for v in s.PROFILES.values()} for period in ('early','recent')},
        pool_sha256={period:s.sha(PARENT/(period+'-pool.json')) for period in ('early','recent')},profiles={k:dict(root=str(root),scenario=name) for k,(root,name) in s.PROFILES.items()},
        historical='Exploratory structural exits and graded environment routing; no independent data.',
        budget='Same counter-cluster rule; original/profile stop or negative warning_body_failed_close blocks repeats only AFTER accepted failure exits strictly before next trigger.',
        routing='graded:strong_counter body/half, weaker counter kind-confirm, aligned/missing base. Other templates apply frozen half/close profiles only in counter/opposed environment.',
        online_windows=[],production_changed=False,qualified_for_trading=False)
    s.write(s.RUN/'protocol.json',cp);s.choose=choose
    # Keep access to the original function after installing the wrapper.
    global _original_counter
    _original_counter=original_counter;s.counter_stop_portfolio=wrapped_failure_portfolio
    s.evaluate()
    assert all(s.sha(RUN/'fork'/k)==h for k,h in s.read(RUN/'implementation-manifest.json').items())

def wrapped_failure_portfolio(rows,events,ends,reverse=False):
    mapped=[]
    for r in rows:
        e=s.ev(r)
        if e['status']=='closed' and e['exit_reason']=='warning_body_failed_close' and e['net_amount']<0:
            r=copy.deepcopy(r);s.ev(r)['exit_reason']='stop'
        mapped.append(r)
    aa,ledger=_original_counter(mapped,events,ends,reverse)
    originals={r['event_key']:r for r in rows};return [originals[r['event_key']] for r in aa],ledger

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('stage',choices=['prepare','run','evaluate']);globals()[p.parse_args().stage]()
