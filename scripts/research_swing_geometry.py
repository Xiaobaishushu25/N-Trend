"""Mechanism expansion: entry location, risk geometry, local target and clustered stops.
Frozen small hypothesis set, no post-outcome tuning, no production writes.
"""
import argparse
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
from research_swing_consistency import REPO,RUN as SOURCE,OLD,read,write,sha,replace_once,portfolio,ev,metrics,cluster
from research_swing_combination import describe,compare_gate,weekly_delta
from reprice_signal_costs import TradingDates,reprice

RUN=REPO/'research-runs/swing-geometry-20261025-v1'


def definitions():
    specs=[('base',False,False,False,False),('retest',True,False,False,False),('body',False,True,False,False),
           ('local',False,False,True,False),('retest_local',True,False,True,False),('body_local',False,True,True,False),
           ('retest_body_local',True,True,True,False),('retest_body_local_wide',True,True,True,True)]
    return [dict(name=base+'_'+name,base=base,variant=name,retest=retest,body=body,local=local,wide=wide,
                 rules=dict(entry_mode='breakout_retest_quarter' if retest else 'touch',target_cap_r=cap,min_actual_risk_ticks=10.,flat_before_weekend=True))
            for base,cap in [('P2',None),('P3',2.)] for name,retest,body,local,wide in specs]


def prepare():
    RUN.mkdir(parents=True,exist_ok=False);root=RUN/'fork'
    for name in ('n-core','n-protocol'):shutil.copytree(SOURCE/'fork/crates'/name,root/'crates'/name)
    for name in ('Cargo.toml','Cargo.lock'):shutil.copy2(SOURCE/'fork'/name,root/name)
    mod=root/'crates/n-core/src/v3/mod.rs';mod.write_text(mod.read_text(encoding='utf-8')+'\npub mod geometry_research;\n',encoding='utf-8')
    shutil.copy2(REPO/'scripts/swing_geometry_rules.rs',root/'crates/n-core/src/v3/geometry_research.rs')
    path=root/'crates/n-core/src/v3/ablation.rs';s=path.read_text(encoding='utf-8')
    s=replace_once(s,'''        ev.entry + sign * slip * ev.tick_size
            - if self.entry_mode == EntryMode::BreakoutRetestQuarter {
                sign * ev.risk * 0.25
            } else {
                0.
            }''','''        (if self.entry_mode == EntryMode::BreakoutRetestQuarter {
            crate::v3::geometry_research::retest_price(ev,sign)
        } else {ev.entry}) + sign*slip*ev.tick_size''')
    s=replace_once(s,'(price - (ev.entry - sign * ev.risk * 0.25))','(price - crate::v3::geometry_research::retest_price(ev,sign))')
    s=replace_once(s,'let retest = ev.entry - sign * ev.risk * 0.25;','let retest = crate::v3::geometry_research::retest_price(ev,sign);')
    path.write_text(s,encoding='utf-8')
    shutil.copy2(REPO/'scripts/swing_geometry_runner.rs',root/'crates/n-core/src/bin/swing_geometry_runner.rs')
    shutil.copy2(REPO/'tests/swing_geometry.rs',root/'crates/n-core/tests/swing_geometry.rs')
    old=read(SOURCE/'protocol.json');p=dict(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        source=str(SOURCE),periods=old['periods'],hashes=old['hashes'],production_before=old['production_before'],templates=definitions(),
        modes=['corrected_touch','next_minute_open'],slips=[1,2],
        common_pool='All original warnings score>=2.5; PB/SS regenerated against corrected aggregation. Frozen service-forward upstream mask applied before strategy filters. No warning type removed, no new A breakout requirement.',
        baseline='Original score>=3, friction<=0.2R, original risk/ATR14 in [.75,2.5], minrisk10ticks, net reward/loss>=1, original no-follow5 and holidays/weekends.',
        geometry='Body stop: one tick beyond warning body invalidation, only tighten. Setup ATR gate remains based on original full-candle risk, while new geometry rechecks min10ticks, friction<=0.2 new nominalR and guarded netspace. Local target: nearest strict confirmed pivot in last12 complete pre-warning15m candles, no cross-session pivots, otherwise S1. Retest: original breakout then quarter of transformed risk pullback in a later minute; grid rounded toward breakout, market trigger plus adverse slip.',
        wide='Only full retest/body/local wide candidate: score>=2.5, remove original risk/ATR band, retain new geometry friction<=0.2, min10ticks and netRR; no fit by product.',
        cluster_variants=['base','body_local','retest_body_local','retest_body_local_wide'],
        cluster='Block subsequent entries in same symbol/direction/S0/S1 only after an accepted trade has CLOSED at stop. Completed stop must precede next trigger timestamp, not eventual/future outcome; no-follow losses do not block. Unknown capacity unchanged.',
        gates=read(REPO/'research-runs/swing-combination-20261025-v1/protocol.json')['gates'],
        historical='All inspected history; exploratory consistency evidence, no independent holdout claim. Every candidate and failure reported; no parameter changes after outcomes.',
        production_changed=False,qualified_for_trading=False)
    write(RUN/'protocol.json',p)
    write(RUN/'implementation-manifest.json',{str(f.relative_to(root)):sha(f) for f in root.rglob('*') if f.is_file()})
    print('Frozen broader mechanism protocol',RUN,flush=True)


def run():
    root=RUN/'fork';args=['--offline','--manifest-path',str(root/'Cargo.toml'),'--target-dir',str(REPO/'target'),'-p','n-core']
    subprocess.run(['cargo','test',*args,'--test','swing_geometry'],check=True)
    subprocess.run(['cargo','build',*args,'--bin','swing_geometry_runner'],check=True)
    p=read(RUN/'protocol.json')
    for period in ('recent','early'):
        assert all(sha(v)==p['hashes'][period][k] for k,v in p['periods'][period].items())
        subprocess.run([str(REPO/'target/debug/swing_geometry_runner.exe'),str(RUN/'protocol.json'),period,'pool',str(RUN/(period+'-pool.json'))],check=True)
        subprocess.run([str(REPO/'target/debug/swing_geometry_runner.exe'),str(RUN/'protocol.json'),period,'replay',str(RUN/period)],check=True)


def stop_once(rows,events,ends,reverse=False):
    ordered=sorted(rows,key=lambda r:r['event_key'],reverse=reverse);ordered.sort(key=lambda r:(ev(r).get('trigger_ts') or ev(r)['entry_ts'] or r['warning_ts'],r['warning_ts']))
    accepted=[];ledger=[];active={};blocked_clusters=set();pending=[]
    for r in ordered:
        e=ev(r);start=e.get('trigger_ts') or e['entry_ts'];unknown=e['status'] in ('ambiguous','missing_data','rollover','unfinished','pending')
        if not start and not unknown:continue
        start=start or r['warning_ts'];end=ends[r['execution_contract']] if unknown else e['exit_ts'];assert end and end>=start
        keep=[]
        for stop,c in pending:
            if stop<start:blocked_clusters.add(c)
            else:keep.append((stop,c))
        pending=keep;c=cluster(events[r['event_key']])
        reason='symbol_slot' if active.get(r['symbol'],'')>=start else 'cluster_stop_once' if c in blocked_clusters else None
        ledger.append(dict(event_key=r['event_key'],start=start,end=end,accepted=reason is None,reason=reason))
        if reason:continue
        accepted.append(r);active[r['symbol']]=end
        if e['status']=='closed' and e['exit_reason']=='stop':pending.append((e['exit_ts'],c))
    return accepted,ledger


def report():
    p=read(RUN/'protocol.json');fees=read(p['periods']['recent']['fees']);original=read(OLD/'development-results.json')
    prior=collections.defaultdict(dict)
    for line in (SOURCE/'accepted.jsonl').open(encoding='utf-8'):
        z=json.loads(line)
        if z['case'].split('/')[0] in ('P2','P3') and len(z['case'].split('/'))==3:
            prior[(z['period'],z['case'])][z['row']['event_key']]=z['row']
    results={};accepted_all={};checks={};ledgers={};leave={};funnel={}
    for period in ('recent','early'):
        pool=read(RUN/(period+'-pool.json'));events={x['event_key']:x['event'] for x in pool};source=read(p['periods'][period]['input']);cal=TradingDates(source['symbols'])
        ends=read(RUN/period/'manifest.json')['contract_ends'];cases=collections.defaultdict(dict)
        for line in (RUN/period/'evaluations.jsonl').open(encoding='utf-8'):
            row=reprice(json.loads(line),fees,cal)
            if ev(row)['status']=='closed':row['exit_trading_day']=cal.day(ev(row)['exit_ts'])
            assert row['event_key'] not in cases[row['scenario']]
            cases[row['scenario']][row['event_key']]=row
        assert len(cases)==len(p['templates'])*len(p['modes'])*len(p['slips'])
        results[period]={};accepted_all[period]={};ledgers[period]={};leave[period]={};funnel[period]={}
        for case,mapping in cases.items():
            rows=list(mapping.values());name=case.split('/')[0];base=name[:2];variant=name[3:]
            for cluster_rule in ([False,True] if variant in p['cluster_variants'] else [False]):
                label=case+('/stop_once' if cluster_rule else '')
                if cluster_rule:
                    accepted,ledger=stop_once(rows,events,ends);reversed_rows,_=stop_once(rows,events,ends,True)
                else:
                    rr=portfolio(rows,events,ends);accepted,ledger=rr;reversed_rows,_=portfolio(rows,events,ends,reverse=True)
                m=describe(accepted,events);m['reverse_tie_net']=describe(reversed_rows,events)['net']
                risk=sum(ev(r)['risk']*r['multiplier'] for r in accepted if ev(r)['status']=='closed')
                m['closed_initial_risk_sum']=risk;m['net_per_closed_initial_risk']=m['net']/risk if risk else None
                results[period][label]=m;accepted_all[period][label]=accepted;ledgers[period][label]=ledger
                leave[period][label]={symbol:metrics([r for r in accepted if r['symbol']!=symbol]) for symbol in sorted(source['symbols'])}
            funnel[period][case]=dict(collections.Counter(ev(r)['status'] for r in rows))
        regression=0
        for base in ('P2','P3'):
            for mode in p['modes']:
                for slip in (1,2):
                    suffix=f'/{mode}/slip{slip}';case=base+'_base'+suffix
                    current={r['event_key']:r for r in accepted_all[period][case]};old=prior[(period,base+suffix)]
                    assert current.keys()==old.keys(),('baseline set',period,case,len(current),len(old))
                    for k,row in current.items():
                        assert row['result']==old[k]['result'],('baseline result',period,case,k)
                        assert row.get('cost_repricing')==old[k].get('cost_repricing'),('baseline fee',period,case,k)
                        regression+=1
        # Descriptive comparative intervals do not alter frozen qualification.
        for case,rows in accepted_all[period].items():
            if case.split('/')[0].endswith('_base') and not case.endswith('/stop_once'):continue
            base=case[:2];parts=case.split('/');control=base+'_base/'+parts[1]+'/'+parts[2]
            results[period][case]['weekly_delta']=weekly_delta(accepted_all[period][control],rows)
        contracts=read(p['periods'][period]['contracts']);needed=collections.defaultdict(set)
        for rows in accepted_all[period].values():
            for r in rows:needed[r['execution_contract']].update(f['ts'] for f in r['result']['fills'])
        index={c:{b['datetime']:b for b in contracts[c]['minutes'] if b['datetime'] in ts} for c,ts in needed.items()};del contracts
        fillchecks=cashchecks=localchecks=0
        for case,rows in accepted_all[period].items():
            for r in rows:
                e=ev(r);sign=1 if r['direction']=='up' else -1
                if r['local_level_time']:
                    assert r['local_level_time']<r['warning_ts'];localchecks+=1
                for f in r['result']['fills']:
                    assert f['ts']>r['warning_ts'];assert abs(f['price']/r['tick_size']-round(f['price']/r['tick_size']))<1e-6
                    assert index[r['execution_contract']][f['ts']]['volume']>0 or r['zero_volume_execution_unknown'];fillchecks+=1
                if e['status']=='closed':
                    fs=r['result']['fills'];assert len(fs)==2 and fs[0]['volume']==fs[1]['volume']==1
                    net=(fs[1]['price']-fs[0]['price'])*sign*r['multiplier']-r['cost_repricing']['commission']
                    assert abs(net-e['net_amount'])<1e-6;assert abs(e['net_r']-net/(e['risk']*r['multiplier']))<1e-9;cashchecks+=1
        checks[period]=dict(baseline_exact=regression,fill_checks=fillchecks,cash_checks=cashchecks,causal_local_target_checks=localchecks,
            pool=len(pool),raw_evaluations=read(RUN/period/'manifest.json')['evaluations'])
    gates={};eligible=[];stable=[]
    for case in results['recent']:
        parts=case.split('/');name=parts[0];base=name[:2];cluster_rule=case.endswith('/stop_once')
        candidate=name+('/stop_once' if cluster_rule else '')
        if candidate in gates or candidate in ('P2_base','P3_base'):continue
        detail={}
        for period in ('recent','early'):
            detail[period]={}
            for mode in p['modes']:
                for slip in (1,2):
                    k=name+f'/{mode}/slip{slip}'+('/stop_once' if cluster_rule else '');control=base+f'_base/{mode}/slip{slip}'
                    detail[period][f'{mode}/slip{slip}']=compare_gate(results[period][k],results[period][control],original[base][str(slip)]['all'],period=='recent')
        passed=all(all(flags.values()) for z in detail.values() for flags in z.values())
        positive=all(results[period][name+f'/{mode}/slip{slip}'+('/stop_once' if cluster_rule else '')]['net']>0
            and (results[period][name+f'/{mode}/slip{slip}'+('/stop_once' if cluster_rule else '')]['mean_r'] or -1)>0
            for period in ('recent','early') for mode in p['modes'] for slip in (1,2))
        gates[candidate]=dict(defensive_pass=passed,stable_historical_pass=passed and positive,checks=detail)
        if passed:eligible.append(candidate)
        if passed and positive:stable.append(candidate)
    write(RUN/'results.json',results);write(RUN/'gates.json',gates);write(RUN/'funnel.json',funnel)
    write(RUN/'leave-product-out.json',leave);write(RUN/'opportunity-ledgers.json',ledgers)
    with (RUN/'accepted.jsonl').open('x',encoding='utf-8') as file:
        for period,cases in accepted_all.items():
            for case,rows in cases.items():
                for row in rows:file.write(json.dumps(dict(period=period,case=case,row=row),ensure_ascii=False)+'\n')
    assert all(sha(path)==h for path,h in p['production_before'].items())
    write(RUN/'verification.json',dict(checks=checks,production_files_unchanged=len(p['production_before']),production_changed=False,rust_tests=5,
        protocol_sha256=sha(RUN/'protocol.json'),results_sha256=sha(RUN/'results.json'),
        source_sha256={str(q.relative_to(REPO)):sha(q) for q in (pathlib.Path(__file__),REPO/'scripts/swing_geometry_rules.rs',REPO/'scripts/swing_geometry_runner.rs',REPO/'tests/swing_geometry.rs')}))
    write(RUN/'paper-freeze.json',dict(defensive_candidates=eligible,stable_candidates=stable,status='paper_only' if eligible else 'no_candidate',
        qualified_for_trading=False,historical_periods_already_inspected=True,min_trading_days=60,min_closed_trades=100))
    render(results,gates,checks)
    print('Broader geometry report',RUN/'report.md','defensive',eligible,'stable',stable,flush=True)


def render(results,gates,checks):
    def f(x,fmt='.2f'):return format(x,fmt) if x is not None else '—'
    protocol=read(RUN/'protocol.json')
    lines=['# 小波段机制扩展：成交位置、风险与近端目标','',
        f"本批冻结{len(protocol['templates'])}个执行模板，另对{len(protocol['cluster_variants'])}类模板加入同A段止损一次后停新开。双成本、两种入场成交模式；全部历史已被研究，结果属于探索。",'',
        '逻辑合理并不保证净优势：更紧止损会增加被正常波动击中的次数，回踩成交会错过不回头的赢家，近端目标会截掉覆盖亏损的右尾。每种机制均需验证这些反面效果。','',
        '## 触价入场、每边1tick并扣费','',
        '|规则|近期净额|上半年净额|近期平均R|上半年平均R|近期胜率|上半年胜率|近期保留率（修复基线）|近期/上半年未知|',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    if protocol.get('supplementary_exploration'):
        lines[2:2]=['本补充批次是在主批次结果已查看后提出的追加探索，不属于原预注册搜索或独立验证；协议保存了这一顺序和主批次结果哈希。','']
    for base in ('P2','P3'):
        for variant in [x['variant'] for x in protocol['templates'] if x['base']==base]:
            for once in ([False,True] if variant in read(RUN/'protocol.json')['cluster_variants'] else [False]):
                key=f'{base}_{variant}/corrected_touch/slip1'+('/stop_once' if once else '')
                a,b=results['recent'][key],results['early'][key];baseline=results['recent'][base+'_base/corrected_touch/slip1']
                lines.append(f"|{base}_{variant}"+('/stop_once' if once else '')+f"|{a['net']:.2f}|{b['net']:.2f}|{f(a['mean_r'],'.3f')}|{f(b['mean_r'],'.3f')}|{f(a['win_rate'],'.2%')}|{f(b['win_rate'],'.2%')}|{a['net']/baseline['net']:.1%}|{a['unknown']}/{b['unknown']}|")
    lines += ['', '净额为固定一手、品种机会汇总，无账户资金/保证金约束。平均R按各笔实际止损风险归一化，避免把缩小风险金额误认为提高净期望。回撤为已平仓现金回撤，完整双成本和成交模式结果见results.json。','',
        '## 冻结规则与边界','',
        '- base：现有修复后P2/P3。',
        '- retest：先突破原入场价，随后另一分钟回踩四分之一计划风险时触发市价；价格向原突破方向取整到tick，再加不利滑点。next_minute_open仍另等下一分钟开盘成交。',
        '- body：初始止损只收紧到预警实体外1tick；重新检查至少10tick、摩擦≤0.2新风险及净盈亏比。保留原形态尺度ATR资格。',
        '- local：警报之前12根完整15m线中，最近的已确认严格拐点阻力/支撑；三根必须连续，不跨会话；取入场方向最近且不远于S1的目标，无合适拐点则保持S1。P3再受2R上限。',
        '- retest_local / body_local / retest_body_local：上述机制的事先冻结组合。',
        '- wide：研究分支降低评分至2.5并取消原风险/ATR区间，保留新几何摩擦、至少10tick和净盈亏比闸门。具体适用模板见本批protocol.json；无逐品种参数。',
        '- stop_once：同品种/方向/S0/S1簇中，已接受的一笔交易实际以stop平仓后，阻止之后新触发；未平仓和未来止损均不触发，no_follow亏损不触发。','',
        '原小波段—回调—反转警报池保留，多类反转均在；没有添加A段必须突破此前高低点。所有新目标仅使用预警前已完成数据。','',
        '## 资格检查','']
    for name,g in gates.items():
        fails={period:{k:[f for f,v in flags.items() if not v] for k,flags in z.items() if not all(flags.values())} for period,z in g['checks'].items()}
        lines += [f"- {name}：防守门槛={g['defensive_pass']}；两期正期望门槛={g['stable_historical_pass']}。",'  未通过：'+json.dumps(fails,ensure_ascii=False)]
    eligible=[n for n,g in gates.items() if g['defensive_pass']];stable=[n for n,g in gates.items() if g['stable_historical_pass']]
    lines += ['', '通过全部防守门槛：'+('、'.join(eligible) if eligible else '无。'),'',
        '通过两期正期望门槛：'+('、'.join(stable) if stable else '无。'),'',
        '近期必须在两档成本、两种成交模式下均达到35笔、正收益、保留原/修复正基线80%、胜率不下降、回撤/未知/最差10%均额不恶化、集中度至少两项改善。上半年同时改善净额和平均R且尾部/回撤/未知不恶化。规则相同，未因失败降低门槛。','',
        '## 核验','',json.dumps(checks,ensure_ascii=False,indent=2),'',
        '基线事件、成交、评估及费用逐笔复现。7项Rust测试及5项机会簇Python测试覆盖回踩tick取整与对称性、实体止损不放宽、局部目标右侧确认和未来隔离、跨会话排除、同分钟不得回踩成交、已实现止损之后才停开。所有已接受成交核对实际月份合约、tick、成交量和净额/R会计。','',
        '部分方案机会变少或现金风险缩小，须结合平均R、笔数和净额阅读。未知敞口保守保留，OHLC两路径不等于完整盘口。没有新增独立数据验证；留品种和逐周区间为描述性诊断，不能替代后续纸面记录。','',
        '实盘及旧研究批次未改。复现：prepare → run → report，输出不可覆盖。']
    (RUN/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def supplement():
    global RUN
    parent=RUN;child=parent/'qualification-supplement'
    child.mkdir(exist_ok=False)
    p=read(parent/'protocol.json');p['id']=child.name;p['created_at']=dt.datetime.now(dt.timezone.utc).isoformat()
    p['supplementary_exploration']=True;p['parent_results_sha256']=sha(parent/'results.json')
    p['disclosure']='Planned after parent results inspected. Tests whether original score/ATR gating suppresses the local-target mechanism; not an independent holdout or original predeclared search.'
    p['wide']='All supplementary wide=true templates use score>=2.5, no original risk/ATR band, retained min10ticks/new nominal friction<=0.2/net reward loss>=1. The template booleans independently specify retest/body/local.'
    templates=[]
    for base,cap in [('P2',None),('P3',2.)]:
        templates.append(next(t for t in definitions() if t['name']==base+'_base'))
        for variant,body,local in [('base_wide',False,False),('local_wide',False,True),('body_local_wide',True,True)]:
            templates.append(dict(name=base+'_'+variant,base=base,variant=variant,retest=False,body=body,local=local,wide=True,
                rules=dict(entry_mode='touch',target_cap_r=cap,min_actual_risk_ticks=10.,flat_before_weekend=True)))
    p['templates']=templates;p['cluster_variants']=['base_wide','local_wide','body_local_wide']
    write(child/'protocol.json',p)
    for period in ('recent','early'):shutil.copy2(parent/(period+'-pool.json'),child/(period+'-pool.json'))
    write(child/'executor-manifest.json',dict(executable_sha256=sha(REPO/'target/debug/swing_geometry_runner.exe'),
        frozen_parent_implementation_sha256=sha(parent/'implementation-manifest.json'),source_sha256=sha(pathlib.Path(__file__))))
    for period in ('recent','early'):
        assert all(sha(v)==p['hashes'][period][k] for k,v in p['periods'][period].items())
        subprocess.run([str(REPO/'target/debug/swing_geometry_runner.exe'),str(child/'protocol.json'),period,'replay',str(child/period)],check=True)
    RUN=child
    report()
    print('Supplementary exploration complete',child,flush=True)


def complete():
    global RUN
    parent=RUN;child=parent/'qualification-supplement';main=read(parent/'results.json');extra=read(child/'results.json')
    for folder,data in ((parent,main),(child,extra)):
        RUN=folder;render(data,read(folder/'gates.json'),read(folder/'verification.json')['checks'])
    RUN=parent
    unified={period:dict(main[period]) for period in ('recent','early')}
    for period in ('recent','early'):
        for case,value in extra[period].items():
            if case in unified[period]:assert unified[period][case]==value
            else:unified[period][case]=value
    write(parent/'all-results.json',unified)
    # Freeze an OBSERVATION, not a passed candidate, after all inspected results.
    p=read(child/'protocol.json');candidate=next(t for t in p['templates'] if t['name']=='P2_local_wide')
    write(parent/'observation-candidate.json',dict(name='P2_local_wide',status='research_observation_only',
        passed_frozen_gates=False,qualified_for_trading=False,selected_after_history_inspection=True,
        reason='Simplest changed-geometry candidate improving recent cash/win/DD and earlier meanR/DD in all execution/cost cases; earlier expectation remains negative and worst-decile cash loss slightly worse in touch cases.',
        template=candidate,common_pool=p['common_pool'],wide='For this observation: score>=2.5, no original risk/ATR band; retain min10ticks, friction<=0.2R and guarded netRR. retest=false, body=false, local=true.',
        source_protocol_sha256=sha(child/'protocol.json'),source_results_sha256=sha(child/'results.json'),
        comparator='P2_base under identical cost and entry mode; legacy profit-floor target shown separately',
        production_changed=False,min_paper_trading_days=60,min_paper_closed_trades=100,
        prospective_fields=['warning_identity','snapshot_time','forecast_target','forecast_stop','quote_trigger_time','bid_ask','next_quote','planned_action','actual_or_paper_fill','fee','reason','missed_or_blocked','cluster_identity']))
    lines=['# 扩展优化总览：已找到局部改善，尚未形成跨期正期望','',
        '主批24组策略/组合，补充批另12组，共36组（含P2/P3基线），完成685,056次评估。补充批在主批结果后提出，属于追加探索；所有历史已被研究，没有把本轮选择宣称为独立验证。','',
        '最值得继续观察的是P2_local_wide：保留原N字推动—回调—反转池、S2结构止损和费用闸门，降低评分资格至原警报池的2.5，取消原风险/ATR区间，目标改为入场前已确认的最近拐点阻力/支撑（无合适点则S1）。不添加A段突破此前高低点要求，不按品种独立选参。','',
        '## 相同口径的主要结果','',
        '|触价/每边1tick扣费|近期基线|近期观察候选|上半年基线|上半年观察候选|','|---|---:|---:|---:|---:|']
    b0=extra['recent']['P2_base/corrected_touch/slip1'];c0=extra['recent']['P2_local_wide/corrected_touch/slip1']
    be=extra['early']['P2_base/corrected_touch/slip1'];ce=extra['early']['P2_local_wide/corrected_touch/slip1']
    for label,field,fmt in [('笔数','n','d'),('净额（元）','net','.2f'),('平均净R','mean_r','.3f'),('净胜率','win_rate','.2%'),
        ('已实现回撤（元）','realized_cash_drawdown','.2f'),('最差10%均额（元）','tail10_cash','.2f'),('未知','unknown','d')]:
        lines.append('|'+label+'|'+'|'.join(format(z[field],fmt) for z in (b0,c0,be,ce))+'|')
    lines += ['', '## 成本与入场成交敏感性','',
        '|候选净额|近期|上半年|对应近期基线净额|','|---|---:|---:|---:|']
    for mode in ('corrected_touch','next_minute_open'):
        for slip in (1,2):
            k=f'P2_local_wide/{mode}/slip{slip}';b=f'P2_base/{mode}/slip{slip}'
            lines.append(f"|{mode}/每边{slip}tick|{extra['recent'][k]['net']:.2f}|{extra['early'][k]['net']:.2f}|{extra['recent'][b]['net']:.2f}|")
    lines += ['', '四种场景中，候选相对同口径基线的近期现金、平均R、胜率、回撤与未知控制均改善/不恶化；上半年现金、平均R与已实现回撤也改善，仍全部负收益。触价场景的最差10%均损略恶化，未满足冻结的不恶化门槛。','',
        '还保留了原研究利润的80%作为额外收益底线。它与不同入场成交模式的基线不能混为同口径性能比较：下一分钟开盘/2tick候选为11238.89元，高于相同模式基线7456.07元，但没有达到旧触价研究2tick利润的80%。本轮没有事后降低这一冻结门槛；同时分别披露可比口径的改善。','',
        '## 机制与代价','',
        '- 更紧止损：通常减少单笔现金风险，也会增加噪声止损；本轮多个body方案现金亏损收窄，但平均R未改善，不能算找到新优势。',
        '- 回踩成交：可能获得更好价格，同时错过不回头的赢家。分钟OHLC无法判清部分目标/回踩先后顺序，未知占用较多；相应结果不能直接用于否定回踩机制。',
        '- 近端目标：近期有明显改善，上半年与宽资格组合后也改善；单独在原严格资格下，上半年每笔平均R反而更差。作用取决于它选择的交易集合，并非越近越好。',
        '- 止损后停开：会避免一些重复损失，也会丢掉后续成功尝试；不默认叠加。观察候选不包含这个附加约束。','',
        '宽资格S1版本上半年平均R为-0.123，宽资格近端目标为-0.134，虽然二者均好于原严格资格的-0.194。这说明联合方案的改善不能全部归功于近端目标：放宽原评分/ATR筛选有贡献，近端目标更明确地改善了近期兑现和回撤，上半年交易质量优势仍待验证。','',
        '## 冻结状态','',
        '没有策略通过全部冻结条件；上半年正期望尚未建立。只将P2_local_wide保存为研究观察版本，不改生产注册、不改实盘参数、不自动开仓。已有两段历史继续用于机制诊断；后续至少60交易日/100笔的纸面对照只是复核起点，不是获利保证。','',
        '主批完整报告见report.md，追加报告见qualification-supplement/report.md；all-results.json合并所有模式/成本，observation-candidate.json冻结观察规则。','']
    (parent/'study-summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    manifest=read(parent/'implementation-manifest.json');manifest={k.replace('\\','/'):v for k,v in manifest.items()}
    critical=['crates/n-core/src/v3/ablation.rs','crates/n-core/src/v3/geometry_research.rs','crates/n-core/src/bin/swing_geometry_runner.rs']
    assert all(sha(parent/'fork'/q)==manifest[q] for q in critical)
    assert definitions()==read(parent/'protocol.json')['templates']
    before=read(parent/'protocol.json')['production_before'];assert all(sha(q)==h for q,h in before.items())
    write(parent/'completion.json',dict(study_summary_sha256=sha(parent/'study-summary.md'),observation_sha256=sha(parent/'observation-candidate.json'),
        initial_rules_and_executor_unchanged=True,supplement_disclosed=True,production_files_unchanged=len(before),production_changed=False,
        rust_tests=7,python_tests=5,total_evaluations=685056,
        source_sha256={str(q.relative_to(REPO)):sha(q) for q in (pathlib.Path(__file__),REPO/'scripts/swing_geometry_rules.rs',REPO/'scripts/swing_geometry_runner.rs',REPO/'tests/swing_geometry.rs',REPO/'tests/test_swing_geometry.py')},
        qualified_for_trading=False))
    print('Completed expanded study and froze research-only observation',parent/'study-summary.md',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('stage',choices=['prepare','run','report','supplement','complete'])
    globals()[parser.parse_args().stage]()
