"""Causal entry-quality and shadow-performance tests for negative early-period R.
All periods already researched. Positive historical fits are NOT full-cycle proof.
"""
import argparse
import bisect
import collections
import datetime as dt
import json
import math
import pathlib
import shutil
import statistics
import subprocess
import sys

sys.dont_write_bytecode=True
from research_swing_consistency import REPO,read,write,sha,replace_once,portfolio,ev,metrics
from research_swing_combination import describe
from reprice_signal_costs import TradingDates,reprice

GEOMETRY=REPO/'research-runs/swing-geometry-20261025-v1'
REFERENCE=GEOMETRY/'qualification-supplement'
RUN=REPO/'research-runs/swing-cycle-20261025-v1'
UNKNOWN={'ambiguous','missing_data','rollover','unfinished','pending'}


def definitions():
    specs=[('base','touch',None,False,False,1.),('fresh4','touch',4,False,False,1.),
        ('pre_body','touch',None,True,False,1.),('confirm','close_confirm_next_open',None,False,False,1.),
        ('confirm_fresh4','close_confirm_next_open',4,False,False,1.),('confirm_pre_body','close_confirm_next_open',None,True,False,1.),
        ('kind_confirm','close_confirm_next_open',None,False,True,1.),
        ('reclaim','breakout_retest_reclaim',None,False,False,1.),('reclaim_fresh4','breakout_retest_reclaim',4,False,False,1.),
        ('reclaim_pre_body','breakout_retest_reclaim',None,True,False,1.),('space2','touch',None,False,False,2.),
        ('confirm_space2','close_confirm_next_open',None,False,False,2.)]
    return [dict(name='P2_cycle_'+name,base='P2',variant=name,wide=True,body=False,local=True,retest=False,
        pre_body=pre,kind_confirm=kind,min_net_rr=rr,rules=dict(entry_mode=mode,pending_buckets=pending,
            target_cap_r=None,min_actual_risk_ticks=10.,flat_before_weekend=True))
        for name,mode,pending,pre,kind,rr in specs]


def patch(root):
    path=root/'crates/n-core/src/v3/ablation.rs';s=path.read_text(encoding='utf-8')
    s=replace_once(s,'    BreakoutRetestQuarter,','    BreakoutRetestQuarter,\n    BreakoutRetestReclaim,')
    s=replace_once(s,'    pub entry_mode: EntryMode,','    pub entry_mode: EntryMode,\n    pub pending_body_invalidation: Option<f64>,')
    s=replace_once(s,'self.entry_mode == EntryMode::BreakoutRetestQuarter','matches!(self.entry_mode,EntryMode::BreakoutRetestQuarter|EntryMode::BreakoutRetestReclaim)')
    s=replace_once(s,'''        if self.e.fill.is_none() {
            if (price - ev.stop)''','''        if self.e.fill.is_none() {
            if self.rules.pending_body_invalidation.is_some_and(|p|(price-p)*sign<=0.) {
                self.finish("invalidated_before_entry_body",ts);return;
            }
            if (price - ev.stop)''')
    s=replace_once(s,'''                EntryMode::BreakoutRetestQuarter => {
                    if (price - ev.target)''','''                EntryMode::BreakoutRetestReclaim => {
                    if self.confirmed_at.as_deref().is_some_and(|at|at<ts) {
                        self.enter(price,ev,ex,sign,ts);
                    }
                }
                EntryMode::BreakoutRetestQuarter => {
                    if (price - ev.target)''')
    s=replace_once(s,'''        if self.e.fill.is_none() {
            match self.rules.entry_mode {''','''        if self.e.fill.is_none() {
            if !favorable && self.rules.pending_body_invalidation.is_some_and(|p|(to-p)*sign<=0.) {
                self.finish("invalidated_before_entry_body",ts);return;
            }
            match self.rules.entry_mode {''')
    s=replace_once(s,'''                EntryMode::BreakoutRetestQuarter => {
                    let retest''','''                EntryMode::BreakoutRetestReclaim => {}
                EntryMode::BreakoutRetestQuarter => {
                    let retest''')
    s=replace_once(s,'''        if state.e.fill.is_none()
            && rules.entry_mode == EntryMode::CloseConfirmNextOpen''','''        if state.e.fill.is_none() && rules.entry_mode==EntryMode::BreakoutRetestReclaim && state.confirmed_at.is_none() {
            let favorable=if sign>0.{bar.high}else{bar.low};
            let adverse=if sign>0.{bar.low}else{bar.high};
            let retest=crate::v3::geometry_research::retest_price(ev,sign);
            if (adverse-ev.stop)*sign<=0. {state.finish("invalidated",&bar.datetime);}
            else if (favorable-ev.target)*sign>=0. {state.finish("missed_target",&bar.datetime);}
            else if state.breakout_at.as_deref().is_some_and(|t|t<bar.datetime.as_str())
                && (adverse-retest)*sign<=0. && (bar.close-retest)*sign>=0. {
                state.confirmed_at=Some(bar.datetime.clone());
            } else if (favorable-ev.entry)*sign>=0. {
                state.breakout_at.get_or_insert_with(||bar.datetime.clone());
            }
            if state.terminal(){break;}
        }
        if state.e.fill.is_none()
            && rules.entry_mode == EntryMode::CloseConfirmNextOpen''')
    path.write_text(s,encoding='utf-8')
    runner=(REPO/'scripts/swing_geometry_runner.rs').read_text(encoding='utf-8')
    runner=replace_once(runner,'let g = guard(fee)?;','let base_guard = guard(fee)?;')
    runner=replace_once(runner,'            let mut ev = original.clone();','''            let mut ev = original.clone();
            let mut g=base_guard;g.min_net_reward_loss=template["min_net_rr"].as_f64().unwrap();''')
    runner=replace_once(runner,'                rules.next_minute_open = mode == "next_minute_open";','''                rules.next_minute_open = mode == "next_minute_open";
                if template["pre_body"]==true {
                    rules.pending_body_invalidation=Some(if ev.direction=="up" {
                        warning.open.min(warning.close)-ev.tick_size
                    }else{warning.open.max(warning.close)+ev.tick_size});
                }
                if template["kind_confirm"]==true && original.warning_kind=="cumulative" {
                    rules.entry_mode=ablation::EntryMode::Touch;
                }''')
    (root/'crates/n-core/src/bin/swing_cycle_runner.rs').write_text(runner,encoding='utf-8')


def prepare():
    RUN.mkdir(parents=True,exist_ok=False);root=RUN/'fork'
    for name in ('n-core','n-protocol'):shutil.copytree(GEOMETRY/'fork/crates'/name,root/'crates'/name)
    for name in ('Cargo.toml','Cargo.lock'):shutil.copy2(GEOMETRY/'fork'/name,root/name)
    patch(root);shutil.copy2(REPO/'tests/swing_cycle.rs',root/'crates/n-core/tests/swing_cycle.rs')
    for period in ('recent','early'):shutil.copy2(GEOMETRY/(period+'-pool.json'),RUN/(period+'-pool.json'))
    p=read(REFERENCE/'protocol.json');p.update(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),templates=definitions(),
        source_observation_sha256=sha(GEOMETRY/'observation-candidate.json'),cluster_variants=[],online_windows=[30,60],
        baseline='P2_local_wide: score>=2.5, no ATR band, original S2 stop, friction<=.2R/min10ticks/netspace>=1, confirmed nearby pivot target, original no-follow5 and holidays/weekends.',
        geometry='All templates preserve original S2 stop; body=False, retest=False, local=True. New reclaim is its own completed-minute rule, never the previous touch-only retest. Pending body veto never changes a filled stop.',
        entry_quality='Only entries change. Original S2 stop/nearby target, score>=2.5, friction/min10tick/netRR remain. Pending body veto affects pre-fill only, never tightens held stop. Reclaim: breakout on completed1m, later1m retests quarter-risk and closes back above/below level, then next minute open; pre-fill target/stop touched cancels. Entry minute and stress delay follow frozen engine.',
        chronology='Merge BOTH warning periods before one-symbol capacity. Contract end reservation=max observed end across archives; never reset exposure at period boundary. Each row attributed by warning period, not exit month.',
        online='Reference-only shadow strategy always runs, including while live gate OFF. Paper-only warmup30/60closed global shadow trades, then ON iff mean lastN realized netR>0. Only shadow exits strictly earlier than current trigger. Carry state across June/July, no per-symbol fit, no future outcome used.',
        gates=dict(historical='BOTH periods, BOTH cost/entry modes: positive cash AND average netR, >=35closed and >=30% baseline closed count; >=3active warning months; recent>=80% same-mode current reference cash; DD/unknown/worst10% cash no worse. Report original legacy floor separately.',
                   stress='Leave-product-out and removal of best3 days/products diagnostic; positive historical fit still requires prospective quotes and new data.'),
        historical='All inspected development data; do not label optimization on early-period outcomes independent validation.',
        production_changed=False,qualified_for_trading=False)
    write(RUN/'protocol.json',p)
    write(RUN/'implementation-manifest.json',{str(q.relative_to(root)):sha(q) for q in root.rglob('*') if q.is_file()})
    print('Frozen entry-quality and online protocol',RUN,flush=True)


def run():
    args=['--offline','--manifest-path',str(RUN/'fork/Cargo.toml'),'--target-dir',str(REPO/'target'),'-p','n-core']
    subprocess.run(['cargo','test',*args,'--test','swing_cycle'],check=True)
    subprocess.run(['cargo','build',*args,'--bin','swing_cycle_runner'],check=True)
    p=read(RUN/'protocol.json')
    for period in ('recent','early'):
        assert all(sha(v)==p['hashes'][period][k] for k,v in p['periods'][period].items())
        subprocess.run([str(REPO/'target/debug/swing_cycle_runner.exe'),str(RUN/'protocol.json'),period,'replay',str(RUN/period)],check=True)


def online_gate(rows,events,ends,window,reverse=False):
    shadow,_=portfolio(rows,events,ends,reverse=reverse)
    completed=sorted((r for r in shadow if ev(r)['status']=='closed'),key=lambda r:(ev(r)['exit_ts'],r['event_key']))
    shadow_cursor=0;history=[];accepted=[];ledger=[];active={}
    ordered=sorted(rows,key=lambda r:r['event_key'],reverse=reverse)
    ordered.sort(key=lambda r:(ev(r).get('trigger_ts') or ev(r)['entry_ts'] or r['warning_ts'],r['warning_ts']))
    for r in ordered:
        e=ev(r);start=e.get('trigger_ts') or e['entry_ts'];unknown=e['status'] in UNKNOWN
        if not start and not unknown:continue
        start=start or r['warning_ts'];end=ends[r['execution_contract']] if unknown else e['exit_ts'];assert end and end>=start
        while shadow_cursor<len(completed) and ev(completed[shadow_cursor])['exit_ts']<start:
            history.append(ev(completed[shadow_cursor])['net_r']);shadow_cursor+=1
        value=statistics.mean(history[-window:]) if len(history)>=window else None
        allowed=value is not None and value>0.
        reason='shadow_warmup' if value is None else 'shadow_nonpositive' if not allowed else 'symbol_slot' if active.get(r['symbol'],'')>=start else None
        ledger.append(dict(event_key=r['event_key'],period=r['period'],start=start,end=end,accepted=reason is None,reason=reason,
            past_shadow_closed=len(history),past_window_r=value,last_shadow_exit=ev(completed[shadow_cursor-1])['exit_ts'] if shadow_cursor else None))
        if reason:continue
        accepted.append(r);active[r['symbol']]=end
    return accepted,ledger,shadow


def report():
    p=read(RUN/'protocol.json');fees=read(p['periods']['recent']['fees'])
    events={};cases=collections.defaultdict(list);ends={};funnel={};regression={};counts={}
    for period in ('early','recent'):
        events.update({x['event_key']:x['event'] for x in read(RUN/(period+'-pool.json'))})
        for c,t in read(RUN/period/'manifest.json')['contract_ends'].items():ends[c]=max(t,ends.get(c,''))
    old=collections.defaultdict(dict)
    for line in (REFERENCE/'accepted.jsonl').open(encoding='utf-8'):
        z=json.loads(line)
        if z['case'].split('/')[0]=='P2_local_wide':old[(z['period'],z['case'].split('/',1)[1])][z['row']['event_key']]=z['row']
    for period in ('early','recent'):
        source=read(p['periods'][period]['input']);cal=TradingDates(source['symbols']);del source
        f=collections.defaultdict(collections.Counter);local=collections.defaultdict(list);count=0
        for line in (RUN/period/'evaluations.jsonl').open(encoding='utf-8'):
            row=json.loads(line);e=ev(row);case=row['scenario'];count+=1;f[case][e['status']]+=1
            if not(e.get('trigger_ts') or e['entry_ts']) and e['status'] not in UNKNOWN:continue
            row=reprice(row,fees,cal);row['period']=period
            if ev(row)['status']=='closed':row['exit_trading_day']=cal.day(ev(row)['exit_ts'])
            cases[case].append(row);local[case].append(row)
        counts[period]=count;funnel[period]={k:dict(v) for k,v in f.items()};regression[period]=0
        localends=read(RUN/period/'manifest.json')['contract_ends']
        for case,rows in local.items():
            if case.split('/')[0]!='P2_cycle_base':continue
            a,_=portfolio(rows,events,localends);current={r['event_key']:r for r in a};prior=old[(period,case.split('/',1)[1])]
            assert current.keys()==prior.keys(),('reference mismatch',period,case,len(current),len(prior))
            for k,r in current.items():
                assert r['result']==prior[k]['result'],('result regression',period,case,k)
                assert r.get('cost_repricing')==prior[k].get('cost_repricing')
                regression[period]+=1
    results={'early':{},'recent':{},'full':{}};accepted={};ledgers={};leave={};qa=collections.Counter();seam={}
    for mode in p['modes']:
        for slip in (1,2):
            suffix=f'/{mode}/slip{slip}';raw=cases['P2_cycle_base'+suffix]
            for window in p['online_windows']:cases[f'P2_cycle_online{window}'+suffix]=raw
    for case,rows in cases.items():
        name=case.split('/')[0];window=int(name.removeprefix('P2_cycle_online')) if name.startswith('P2_cycle_online') else None
        def select(rs,reverse=False):
            if window:return online_gate(rs,events,ends,window,reverse)[:2]
            return portfolio(rs,events,ends,reverse=reverse)
        a,l=select(rows);rev,_=select(rows,True);accepted[case]=a;ledgers[case]=l
        if name=='P2_cycle_base':
            prior={(period,k) for period in ('early','recent') for k in old[(period,case.split('/',1)[1])]}
            now={(r['period'],r['event_key']) for r in a}
            seam[case]=dict(removed=sorted(prior-now),added=sorted(now-prior))
        for period in results:
            aa=[r for r in a if period=='full' or r['period']==period];m=describe(aa,events)
            m['reverse_tie_net']=describe([r for r in rev if period=='full' or r['period']==period],events)['net']
            m['active_warning_months']=len({r['warning_ts'][:7] for r in aa if ev(r)['status']=='closed'})
            results[period][case]=m
        # Online gates must be replayed with removed-product shadow history too.
        leave[case]={}
        for symbol in sorted({r['symbol'] for r in rows}):
            aa,_=select([r for r in rows if r['symbol']!=symbol])
            leave[case][symbol]={period:metrics([r for r in aa if r['period']==period]) for period in ('early','recent')}
        for r in a:
            e=ev(r)
            if r['local_level_time']:assert r['local_level_time']<r['warning_ts']
            for f in r['result']['fills']:
                assert f['ts']>r['warning_ts'];assert abs(f['price']/r['tick_size']-round(f['price']/r['tick_size']))<1e-6;qa['fills']+=1
            if e['status']=='closed':
                fs=r['result']['fills'];assert len(fs)==2 and fs[0]['volume']==fs[1]['volume']==1
                net=(fs[1]['price']-fs[0]['price'])*(1 if r['direction']=='up' else -1)*r['multiplier']-r['cost_repricing']['commission']
                assert abs(net-e['net_amount'])<1e-6;assert abs(e['net_r']-net/(e['risk']*r['multiplier']))<1e-9;qa['closed_cash']+=1
        if window:
            for z in l:
                if z['last_shadow_exit']:assert z['last_shadow_exit']<z['start']
    # Online rows overlap reference; verify every accepted closed row regardless of case.
    for a in accepted.values():
        for r in a:
            e=ev(r)
            if e['status']=='closed':assert abs(e['net_r']-e['net_amount']/(e['risk']*r['multiplier']))<1e-9
    for period in ('early','recent'):
        needed=collections.defaultdict(set)
        for a in accepted.values():
            for r in a:
                if r['period']==period:needed[r['execution_contract']].update(f['ts'] for f in r['result']['fills'])
        contracts=read(p['periods'][period]['contracts'])
        index={c:{b['datetime']:b['volume'] for b in contracts[c]['minutes'] if b['datetime'] in ts} for c,ts in needed.items()};del contracts
        for a in accepted.values():
            for r in a:
                if r['period']!=period:continue
                for f in r['result']['fills']:
                    assert index[r['execution_contract']][f['ts']]>0 or r['zero_volume_execution_unknown'];qa['source_volume']+=1
    gates={}
    for name in [t['name'] for t in p['templates']]+[f'P2_cycle_online{n}' for n in p['online_windows']]:
        flags={}
        for period in ('early','recent'):
            flags[period]={}
            for mode in p['modes']:
                for slip in (1,2):
                    suffix=f'/{mode}/slip{slip}';m=results[period][name+suffix];b=results[period]['P2_cycle_base'+suffix]
                    flags[period][suffix]=dict(positive_cash=m['net']>0,positive_r=m['mean_r'] is not None and m['mean_r']>0,
                        sample=m['n']>=max(35,.3*b['n']),months=m['active_warning_months']>=3,
                        retention=period!='recent' or m['net']>=.8*b['net']-1e-9,
                        win_rate=period!='recent' or (m['win_rate'] or 0)>=(b['win_rate'] or 0)-1e-12,
                        drawdown=m['realized_cash_drawdown']<=b['realized_cash_drawdown']+1e-9,unknown=m['unknown']<=b['unknown'],
                        tail10=m['tail10_cash'] is not None and m['tail10_cash']>=b['tail10_cash']-1e-9)
        gates[name]=dict(passed=all(all(v.values()) for z in flags.values() for v in z.values()),checks=flags)
    assert all(sha(path)==h for path,h in p['production_before'].items())
    manifest=read(RUN/'implementation-manifest.json');manifest.update(read(RUN/'test-fixture-amendment-before-outcomes.json')['files'])
    assert all(sha(RUN/'fork'/q)==h for q,h in manifest.items())
    write(RUN/'results.json',results);write(RUN/'gates.json',gates);write(RUN/'funnel.json',funnel)
    write(RUN/'leave-product-out.json',leave);write(RUN/'opportunity-ledgers.json',ledgers)
    with (RUN/'accepted.jsonl').open('x',encoding='utf-8') as out:
        for case,a in accepted.items():
            for row in a:out.write(json.dumps(dict(case=case,period=row['period'],row=row),ensure_ascii=False)+'\n')
    verification=dict(reference_exact=regression,seam_control=seam,raw_evaluations=counts,qa=dict(qa),production_unchanged=len(p['production_before']),
        engine_manifest_unchanged=True,protocol_sha256=sha(RUN/'protocol.json'),results_sha256=sha(RUN/'results.json'),
        production_changed=False,qualified_for_trading=False,passed_historical=[k for k,v in gates.items() if v['passed']])
    write(RUN/'verification.json',verification)
    lines=['# 全周期负R研究：入场质量与在线状态','',
        '两段历史均已经多轮研究，只是开发数据；不构成独立验证。所有金额为已平仓每手回放、扣费与滑点后，未知敞口单列。跨期合并进行机会竞争和纸面状态更新，不在7月重置。',
        '', '|规则（触发口径，每边1tick）|上半年笔数|净额|平均R|近期笔数|净额|平均R|', '|---|---:|---:|---:|---:|---:|---:|']
    for name in gates:
        e=results['early'][name+'/corrected_touch/slip1'];r=results['recent'][name+'/corrected_touch/slip1']
        fmt=lambda x:'无' if x is None else f'{x:.4f}'
        lines.append(f"|{name}|{e['n']}|{e['net']:.2f}|{fmt(e['mean_r'])}|{r['n']}|{r['net']:.2f}|{fmt(r['mean_r'])}|")
    lines+=['',f"四场景、两时期全部门槛通过：{verification['passed_historical']}。",'',
        '门槛包括两个时期净额和平均R为正、样本保留、近期至少保留80%当前参考净收益且胜率不降、回撤/尾损/未知不恶化。无交易或极小样本不算稳定。原始旧版盈利不作为同模式比较基准。',
        '', '实现验证：'+json.dumps(verification,ensure_ascii=False), '',
        '在线窗口30/60是事前冻结的两个对照，只看已结束的参考纸面交易；暂停时纸面引擎继续运行。留品种结果为重新计算在线历史后的诊断，不能用全品种未来收益选择品种。',
        '', '后续需新增时期或持续纸面逐报价记录。不能把本批最好的上半年结果直接提升为实盘规则。']
    (RUN/'study-summary.md').write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps(verification,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('stage',choices=['prepare','run','report'])
    globals()[parser.parse_args().stage]()
