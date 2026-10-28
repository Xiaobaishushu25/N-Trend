"""Conditional session-risk follow-up; frozen after structural results, development only."""
import argparse,collections,copy,datetime as dt,json,shutil,subprocess
import research_swing_selective as s
from research_swing_consistency import replace_once
SOURCE=s.RUN/'structural-supplement'
RUN=s.RUN/'session-supplement'

def definitions():
    out=[dict(name='base',condition='none',profile='base',budget=False)]
    for name,condition,profile in [('counter_skip','counter','skip'),('counter_loss','counter','loss'),('opposed_loss','opposed','loss'),
        ('strong_loss','strong_counter','loss'),('counter_half_loss','counter','half_loss'),('counter_body_loss','counter','body_loss'),
        ('counter_close1_loss','counter','close1_loss'),('graded_body_kind_loss','graded','all_loss'),('graded_strong_loss','graded','strong_loss')]:
        out.append(dict(name=name,condition=condition,profile=profile,budget=False))
    t=dict(next(t for t in out if t['name']=='graded_body_kind_loss'));t.update(name='graded_body_kind_loss_failure_budget',budget=True);out.append(t)
    return out

def choose(t,e):
    st=s.state(e)
    if t['condition']=='graded':return 'body_loss' if st['strong_counter'] else ('kind_loss' if t['profile']=='all_loss' else 'kind') if st['counter'] else 'base'
    return t['profile'] if st[t['condition']] else 'base'

def prepare():
    RUN.mkdir(exist_ok=False);root=RUN/'fork';shutil.copytree(SOURCE/'fork',root)
    path=root/'crates/n-core/src/v3/ablation.rs';x=path.read_text(encoding='utf-8')
    x=replace_once(x,'    pub body_failure_count: Option<usize>,','    pub body_failure_count: Option<usize>,\n    pub session_failure_exit: bool,')
    x=replace_once(x,'    pending_body_exit: bool,','    pending_body_exit: bool,\n    pending_session_exit: bool,')
    x=replace_once(x,'        pending_body_exit: false,','        pending_body_exit: false,\n        pending_session_exit: false,')
    x=replace_once(x,'        if state.pending_body_exit {','        if state.pending_body_exit || state.pending_session_exit {')
    x=replace_once(x,'state.remaining,"warning_body_failed_close",sign,&bar.datetime);','state.remaining,if state.pending_body_exit {"warning_body_failed_close"}else{"prebreak_failed_start"},sign,&bar.datetime);')
    x=replace_once(x,'''    }
    if state.e.status == "open" {''','''        if rules.session_failure_exit && state.e.fill.is_some() && pre_long_break(&ev.symbol,&bar.datetime)
            && (bar.close-state.e.fill.unwrap())*sign<0. {
            state.pending_session_exit=true;
        }
    }
    if state.e.status == "open" {''');path.write_text(x,encoding='utf-8')
    runner=(root/'crates/n-core/src/bin/swing_failure_runner.rs').read_text(encoding='utf-8')
    runner=replace_once(runner,'                rules.next_minute_open = mode == "next_minute_open";','                rules.next_minute_open = mode == "next_minute_open";\n                rules.session_failure_exit=template["session_loss"]==true;')
    (root/'crates/n-core/src/bin/swing_session_runner.rs').write_text(runner,encoding='utf-8')
    shutil.copy2(s.REPO/'tests/swing_session.rs',root/'crates/n-core/tests/swing_session.rs')
    for period in ('early','recent'):shutil.copy2(s.PARENT/(period+'-pool.json'),RUN/(period+'-pool.json'))
    p=s.read(SOURCE/'protocol.json');base=p['templates'][0];templates=[]
    for name,skip,loss,half,body,count,kind in [('base',False,False,False,False,None,False),('skip',True,False,False,False,None,False),
        ('loss',True,True,False,False,None,False),('half_loss',True,True,True,False,None,False),('body_loss',True,True,False,True,None,False),
        ('close1_loss',True,True,False,False,1,False),('kind_loss',True,True,False,False,None,True)]:
        t=copy.deepcopy(base);t.update(name='P2_session_'+name,variant=name,half_body=half,body=body,close_body_count=count,kind_confirm=kind,session_loss=loss)
        t['rules']['skip_prebreak_entries']=skip
        if kind:t['rules']['entry_mode']='close_confirm_next_open'
        templates.append(t)
    p.update(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),templates=templates,routes=definitions(),
        script_sha256=s.sha(__file__),parent_results_sha256=s.sha(SOURCE/'combined/results.json'),
        historical='Exploratory AFTER structural16 outcomes, not an independent validation.',
        session='Only selected warning-time adverse environments route here. Reject new entries in known final10min before long session break, using existing calendar pre_long_break predicate. If held trade has gross close-to-fill PnL<0 in that window, submit market exit at NEXT minute OPEN with adverse slip. Keep profitable positions, original target/hardstop/no-follow/weekend/holiday priority. Missing quote remains unknown, no invented fill over missing data.',
        context='No month/product fitting, no new gap-duration or profit threshold grid; threshold gross0R fixed.',production_changed=False,qualified_for_trading=False)
    s.write(RUN/'protocol.json',p);s.write(RUN/'implementation-manifest.json',{str(q.relative_to(root)):s.sha(q) for q in root.rglob('*') if q.is_file()})
    print('Frozen7 execution profiles and11 conditional routes',RUN,flush=True)

def run():
    p=s.read(RUN/'protocol.json');assert s.sha(__file__)==p['script_sha256']
    args=['--offline','--manifest-path',str(RUN/'fork/Cargo.toml'),'--target-dir',str(s.REPO/'target'),'-p','n-core']
    subprocess.run(['cargo','test',*args,'--test','swing_session','--test','swing_failure'],check=True)
    subprocess.run(['cargo','build',*args,'--bin','swing_session_runner'],check=True)
    for period in ('early','recent'):
        assert all(s.sha(v)==p['hashes'][period][k] for k,v in p['periods'][period].items())
        subprocess.run([str(s.REPO/'target/debug/swing_session_runner.exe'),str(RUN/'protocol.json'),period,'replay',str(RUN/period)],check=True)

def failure_portfolio(rows,events,ends,reverse=False):
    mapped=[]
    for r in rows:
        e=s.ev(r)
        if e['status']=='closed' and e['exit_reason'] in ('warning_body_failed_close','prebreak_failed_start') and e['net_amount']<0:
            r=copy.deepcopy(r);s.ev(r)['exit_reason']='stop'
        mapped.append(r)
    a,l=_original(mapped,events,ends,reverse);originals={r['event_key']:r for r in rows}
    return [originals[r['event_key']] for r in a],l

def evaluate():
    p=s.read(RUN/'protocol.json');assert s.sha(__file__)==p['script_sha256']
    s.RUN=RUN/'combined';s.RUN.mkdir(exist_ok=False);s.PROFILES={k:s.PROFILES[k] for k in ('base','kind')}
    s.PROFILES.update({k:(RUN,'P2_session_'+k) for k in ('skip','loss','half_loss','body_loss','close1_loss','kind_loss')})
    cp=s.read(s.PARENT/'protocol.json');cp.update(id=s.RUN.name,templates=p['routes'],script_sha256=s.sha(s.__file__),supervisor_sha256=s.sha(__file__),
        raw_source_sha256={str(root/period/'evaluations.jsonl'):s.sha(root/period/'evaluations.jsonl') for root in {v[0] for v in s.PROFILES.values()} for period in ('early','recent')},
        pool_sha256={period:s.sha(s.PARENT/(period+'-pool.json')) for period in ('early','recent')},profiles={k:dict(root=str(root),scenario=name) for k,(root,name) in s.PROFILES.items()},
        historical=p['historical'],session=p['session'],routing='Warning-time counter/opposed/strong condition only. graded uses body_loss in strong counter and kind_loss/original kind in weak counter; otherwise original baseline.',
        budget='Accepted negative structural/session exit or stop blocks subsequent counter warnings in same A cluster only after exit<nexttrigger.',online_windows=[],production_changed=False,qualified_for_trading=False)
    s.write(s.RUN/'protocol.json',cp);s.choose=choose
    global _original
    _original=s.counter_stop_portfolio;s.counter_stop_portfolio=failure_portfolio;s.evaluate()
    assert all(s.sha(RUN/'fork'/k)==h for k,h in s.read(RUN/'implementation-manifest.json').items())

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('stage',choices=['prepare','run','evaluate']);globals()[p.parse_args().stage]()
