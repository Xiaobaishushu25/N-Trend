"""Selective environment routing, preserving favorable and unclassified opportunities.

No production mutation. Each branch selects an already frozen execution profile
from WARNING-time context only, before trigger-time portfolio competition.
All historical data have been studied; this is development, never a holdout.
"""
import argparse,collections,datetime as dt,json,math,pathlib,statistics,sys
sys.dont_write_bytecode=True
from research_swing_consistency import REPO,read,write,sha,portfolio,ev,metrics,cluster
from research_swing_combination import describe
from reprice_signal_costs import TradingDates,reprice

PARENT=REPO/'research-runs/swing-cycle-20261025-v1'
GEOM=REPO/'research-runs/swing-geometry-20261025-v1/qualification-supplement'
MONET=PARENT/'monetization-supplement'
RUN=REPO/'research-runs/swing-selective-20261025-v1'
UNKNOWN={'ambiguous','missing_data','rollover','unfinished','pending'}
PROFILES={'base':(PARENT,'P2_cycle_base'),'kind':(PARENT,'P2_cycle_kind_confirm'),
    'confirm_body_veto':(PARENT,'P2_cycle_confirm_pre_body'),
    'body':(GEOM,'P2_body_local_wide'),'cap150':(MONET,'P2_cycle_cap150')}

def definitions():
    out=[dict(name='base',condition='none',profile='base',budget=False)]
    for condition in ('strong_counter','opposed','dual','momentum'):
        out.append(dict(name=condition+'_veto',condition=condition,profile='veto',budget=False))
    for condition in ('opposed','dual'):
        for profile in ('kind','body','confirm_body_veto','cap150'):
            out.append(dict(name=condition+'_'+profile,condition=condition,profile=profile,budget=False))
    for profile in ('kind','body'):
        out.append(dict(name='counter_'+profile,condition='counter',profile=profile,budget=False))
    for name in ('base','opposed_kind','opposed_body'):
        t=dict(next(x for x in out if x['name']==name));t.update(name=name+'_counter_stop',budget=True);out.append(t)
    assert len(out)==18
    return out

def state(e):
    m=e.get('market_context') or {};h=m.get('trend_alignment_60');s=m.get('trend_slope_60')
    slope=None if s is None else s*(-1 if e['direction']=='down' else 1)
    counter=h is not None and h<=0;strong=h is not None and h<=-1
    momentum=slope is not None and slope<=0
    return dict(h=h,slope=slope,none=False,counter=counter,strong_counter=strong,momentum=momentum,
                opposed=counter and momentum,dual=strong and momentum)

def choose(template,event):
    # The chooser never receives a future fill, label or realized return.
    return template['profile'] if state(event)[template['condition']] else 'base'

def prepare():
    RUN.mkdir(exist_ok=False)
    p=read(PARENT/'protocol.json');raw={str(root/period/'evaluations.jsonl'):sha(root/period/'evaluations.jsonl')
        for root in {v[0] for v in PROFILES.values()} for period in ('early','recent')}
    p.update(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),templates=definitions(),profiles={k:dict(root=str(root),scenario=name) for k,(root,name) in PROFILES.items()},
        historical='AFTER cycle/context findings; all periods repeatedly researched development data. No independent validation.',
        baseline='P2_local_wide reference unchanged; do not give up full recent baseline simply by requiring all hourly alignment.',
        selective='Missing context passes original baseline, fixed BEFORE outcomes. counter:h<=0; strong_counter:h<=-1ATR EMA spread; momentum:direction*EMA20 four-hour slope<=0; opposed:counter AND momentum; dual:strong_counter AND momentum. No optimized threshold grid.',
        routing='A predicate chooses veto or a pre-existing frozen kind-confirm/body-stop/confirm-pending-body/1.5R-cap profile. Otherwise use original baseline. Warning identity and upstream service dedup unchanged. Choose before trigger; merged chronology capacity after routing.',
        budget='Optional: an accepted counter-hourly trade that CLOSED with original stop blocks further counter-hourly entries in same symbol/direction/S0/S1 cluster only after exit_ts<nexttrigger. Aligned entries can continue; no future labels or per-month reset.',
        raw_source_sha256=raw,pool_sha256={period:sha(PARENT/(period+'-pool.json')) for period in ('early','recent')},
        script_sha256=sha(__file__),production_changed=False,qualified_for_trading=False,
        gates='ALL4 scenarios and BOTH periods: positive cash AND meanR, recent>=80% reference cash and winrate nondecreasing, >=35 and>=30% baseline trades, >=3 warning months, DD/tail10/unknown no worse. Report Pareto tradeoffs even if all gates fail.')
    write(RUN/'protocol.json',p);print('Frozen18 selective templates',RUN,flush=True)

def counter_stop_portfolio(rows,events,ends,reverse=False):
    ordered=sorted(rows,key=lambda r:r['event_key'],reverse=reverse)
    ordered.sort(key=lambda r:(ev(r).get('trigger_ts') or ev(r)['entry_ts'] or r['warning_ts'],r['warning_ts']))
    accepted=[];ledger=[];active={};stops=[];blocked=set()
    for r in ordered:
        e=ev(r);start=e.get('trigger_ts') or e['entry_ts'];unknown=e['status'] in UNKNOWN
        if not start and not unknown:continue
        start=start or r['warning_ts'];end=ends[r['execution_contract']] if unknown else e['exit_ts'];assert end and end>=start
        keep=[]
        for t,c in stops:
            if t<start:blocked.add(c)
            else:keep.append((t,c))
        stops=keep;event=events[r['event_key']];c=cluster(event);counter=state(event)['counter']
        reason='symbol_slot' if active.get(r['symbol'],'')>=start else 'counter_cluster_stopped' if counter and c in blocked else None
        ledger.append(dict(event_key=r['event_key'],start=start,end=end,accepted=reason is None,reason=reason,counter=counter,blocked_prior_stop=c in blocked))
        if reason:continue
        accepted.append(r);active[r['symbol']]=end
        if counter and e['status']=='closed' and e['exit_reason']=='stop':stops.append((e['exit_ts'],c))
    return accepted,ledger

def load(p):
    assert sha(__file__)==p['script_sha256'],'Frozen implementation changed'
    assert all(sha(path)==h for path,h in p['raw_source_sha256'].items())
    events={};periods={};ends={};rows={};funnel=collections.defaultdict(collections.Counter)
    for period in ('early','recent'):
        assert sha(PARENT/(period+'-pool.json'))==p['pool_sha256'][period]
        for x in read(PARENT/(period+'-pool.json')):
            e=x['event'];m=e.get('market_context') or {}
            if m:
                assert m['as_of_ts']==e['warning_ts']
                assert all(not m.get(k) or m[k]<=e['warning_ts'] for k in ('latest_15m_close_ts','latest_60m_close_ts'))
            events[x['event_key']]=e;periods[x['event_key']]=period
        for c,t in read(PARENT/period/'manifest.json')['contract_ends'].items():ends[c]=max(t,ends.get(c,''))
        cal=TradingDates(read(p['periods'][period]['input'])['symbols']);fees=read(p['periods'][period]['fees'])
        for root in {v[0] for v in PROFILES.values()}:
            desired={name:k for k,(r,name) in PROFILES.items() if r==root}
            for line in (root/period/'evaluations.jsonl').open(encoding='utf-8'):
                r=json.loads(line);name,suffix=r['scenario'].split('/',1)
                if name not in desired:continue
                profile=desired[name];funnel[(period,profile)][ev(r)['status']]+=1
                # Retain even untriggered/filtered profile rows so routing cannot
                # fall back to baseline after seeing that a selected order fails.
                r=reprice(r,fees,cal);r['period']=period;r['selected_profile']=profile
                if ev(r)['status']=='closed':r['exit_trading_day']=cal.day(ev(r)['exit_ts'])
                key=(r['event_key'],suffix,profile);assert key not in rows;rows[key]=r
    assert len(rows)==len(events)*4*len(PROFILES),(len(rows),len(events))
    return events,periods,ends,rows,{str(k):dict(v) for k,v in funnel.items()}

def evaluate():
    p=read(RUN/'protocol.json');events,periods,ends,mapping,funnel=load(p)
    results={'early':{},'recent':{},'full':{}};accepted={};ledgers={};routing={};leave={};rawcases={};qa=collections.Counter()
    for t in p['templates']:
        profiles={k:choose(t,e) for k,e in events.items()};routing[t['name']]=dict(collections.Counter(profiles.values()))
        for mode in p['modes']:
            for slip in (1,2):
                suffix=f'{mode}/slip{slip}';case=t['name']+'/'+suffix
                rows=[mapping[(k,suffix,profile)] for k,profile in profiles.items() if profile!='veto'];rawcases[case]=rows
                selector=counter_stop_portfolio if t['budget'] else portfolio
                a,l=selector(rows,events,ends);rev,_=selector(rows,events,ends,reverse=True)
                accepted[case]=a;ledgers[case]=l;leave[case]={}
                for period in results:
                    zz=[r for r in a if period=='full' or r['period']==period];m=describe(zz,events)
                    m['reverse_tie_net']=metrics([r for r in rev if period=='full' or r['period']==period])['net']
                    m['active_warning_months']=len({r['warning_ts'][:7] for r in zz if ev(r)['status']=='closed'})
                    results[period][case]=m
                for symbol in sorted({e['symbol'] for e in events.values()}):
                    aa,_=selector([r for r in rows if r['symbol']!=symbol],events,ends)
                    leave[case][symbol]={period:metrics([r for r in aa if r['period']==period]) for period in ('early','recent')}
                for r in a:
                    e=ev(r)
                    for f in r['result']['fills']:
                        assert f['ts']>r['warning_ts'];assert abs(f['price']/r['tick_size']-round(f['price']/r['tick_size']))<1e-6;qa['fills']+=1
                    if e['status']=='closed':
                        fs=r['result']['fills'];assert len(fs)==2 and fs[0]['volume']==fs[1]['volume']==1
                        cash=(fs[1]['price']-fs[0]['price'])*(1 if r['direction']=='up' else -1)*r['multiplier']-r['cost_repricing']['commission']
                        assert abs(cash-e['net_amount'])<1e-6;assert abs(e['net_r']-cash/(e['risk']*r['multiplier']))<1e-9;qa['cash_r']+=1
    old=read(PARENT/'results.json');regression=0
    for period in ('early','recent'):
        for mode in p['modes']:
            for slip in (1,2):
                suffix=f'{mode}/slip{slip}';m=results[period]['base/'+suffix];b=old[period]['P2_cycle_base/'+suffix]
                assert all(m[k]==b[k] for k in b if k in m),(period,suffix);regression+=1
    gates={}
    for t in p['templates']:
        checks={}
        for period in ('early','recent'):
            checks[period]={}
            for mode in p['modes']:
                for slip in (1,2):
                    suffix=f'{mode}/slip{slip}';m=results[period][t['name']+'/'+suffix];b=results[period]['base/'+suffix]
                    checks[period][suffix]=dict(cash=m['net']>0,mean_r=m['mean_r'] is not None and m['mean_r']>0,
                        retain=period!='recent' or m['net']>=.8*b['net']-1e-9,win_rate=period!='recent' or (m['win_rate'] or 0)>=(b['win_rate'] or 0)-1e-12,
                        sample=m['n']>=max(35,.3*b['n']),months=m['active_warning_months']>=3,
                        drawdown=m['realized_cash_drawdown']<=b['realized_cash_drawdown']+1e-9,unknown=m['unknown']<=b['unknown'],
                        tail10=m['tail10_cash'] is not None and m['tail10_cash']>=b['tail10_cash']-1e-9)
        gates[t['name']]=dict(passed=all(all(v.values()) for z in checks.values() for v in z.values()),checks=checks)
    assert all(sha(k)==h for k,h in p['production_before'].items())
    write(RUN/'results.json',results);write(RUN/'gates.json',gates);write(RUN/'routing.json',routing);write(RUN/'source-funnel.json',funnel)
    write(RUN/'leave-product-out.json',leave);write(RUN/'opportunity-ledgers.json',ledgers)
    with (RUN/'accepted.jsonl').open('x',encoding='utf-8') as f:
        for case,rows in accepted.items():
            for row in rows:f.write(json.dumps(dict(case=case,period=row['period'],row=row),ensure_ascii=False)+'\n')
    write(RUN/'verification.json',dict(baseline_scenarios_exact=regression,qa=dict(qa),profiles=len(PROFILES),templates=len(p['templates']),
        production_unchanged=len(p['production_before']),protocol_sha256=sha(RUN/'protocol.json'),results_sha256=sha(RUN/'results.json'),
        no_new_execution_simulation='Frozen execution-profile records reused; all profile routing and capacity recomputed before evaluating outcomes.',
        passed=[k for k,v in gates.items() if v['passed']],qualified_for_trading=False))
    lines=['# 选择性环境适配研究','',
        '优先保留原基线在有利和未知环境的机会，只在小时结构与斜率不利时过滤或替换执行方式。18个模板事前冻结，历史均为开发数据；不是独立验证。每手净额、已平仓净R，未知敞口单列。',
        '', '|方案（每边1tick）|上半年笔数|净额|平均R|近期净额|收益保留|近期胜率|', '|---|---:|---:|---:|---:|---:|---:|']
    for t in p['templates']:
        e=results['early'][t['name']+'/corrected_touch/slip1'];r=results['recent'][t['name']+'/corrected_touch/slip1'];b=results['recent']['base/corrected_touch/slip1']
        lines.append(f"|{t['name']}|{e['n']}|{e['net']:.2f}|{e['mean_r']:.4f}|{r['net']:.2f}|{r['net']/b['net']:.1%}|{r['win_rate']:.1%}|")
    lines+=['', '所有场景完整通过：'+str([k for k,v in gates.items() if v['passed']]),'',
        '缺失上下文统一回到原基线，不能按历史盈亏决定放行。strong_counter是小时EMA20−EMA60反向差至少1ATR；opposed是小时结构反向且EMA20四小时斜率仍反向；dual是两者交集。',
        '', 'kind保留累积覆盖原触发，其余等分钟确认；body更改止损到预警实体外1tick并重检最小风险/费用；confirm_body_veto只在入场前用实体破坏取消，入场后原止损不变；cap150截断目标至1.5R。source-funnel和routing保留被选择但无法成交的记录，不允许事后回退。',
        '', '两时期数据覆盖仍不齐；本轮没有补齐历史或将“7—10月”窗口称为完整四个月，完整边界见上一轮input-coverage-audit.json。仅有分钟OHLC，两条单调路径的未知敞口保持保守处理；延迟一分钟是额外压力，不是真实下一报价成交。',
        '', '生产文件哈希未变。没有部署、交易或更改实盘参数。']
    (RUN/'report.md').write_text('\n'.join(lines),encoding='utf-8');print('\n'.join(lines[:26]),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('stage',choices=['prepare','evaluate']);globals()[p.parse_args().stage]()
