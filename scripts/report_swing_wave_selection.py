"""Evaluate fully regenerated AB models and preserve all failures/controls."""
import collections,datetime as dt,json,math,pathlib,statistics
import research_swing_wave_selection as w
import research_swing_abstop as s
RUN=w.RUN
WAVES=['legacy','valid_score','near_start','unmerged']
PROFILES=['base','adaptive','b_strong','b_all']

def equivalent(a,b):
    if isinstance(a,(int,float)) and isinstance(b,(int,float)):return abs(a-b)<1e-7
    return a==b

def shape_key(e):return e['symbol'],e['direction'],e['warning_ts']
def identification_bars(raw):
    # Match derive::aggregate: a missing5m/source session gap splits candles,
    # even if the two partial candles share the same nominal end timestamp.
    out=[];cur=None;previous=None
    for b in raw:
        t=dt.datetime.fromisoformat(b['datetime']);label=(t+dt.timedelta(minutes=(-t.minute)%15)).isoformat(sep=' ')
        if cur is None or cur['datetime']!=label or previous is not None and (t-previous).total_seconds()>300:
            if cur is not None:out.append(cur)
            cur=dict(datetime=label,open=b['open'],high=b['high'],low=b['low'],close=b['close'])
        else:cur.update(high=max(cur['high'],b['high']),low=min(cur['low'],b['low']),close=b['close'])
        previous=t
    if cur is not None:out.append(cur)
    return [b for b in out if b['datetime']<=raw[-1]['datetime']]

def first_index(bars):
    index={}
    for i,b in enumerate(bars):index.setdefault(b['datetime'],i)
    return index
def weak_from_bars(e,bars,index):
    sign=1 if e['direction']=='up' else -1;lo=index[e['s1_ts']]+1;hi=index[e['warning_ts']]
    bodies=[-(b['close']-b['open'])*sign for b in bars[lo:hi] if -(b['close']-b['open'])*sign>0]
    if len(bodies)<2:return None
    m=len(bodies)//2;return statistics.mean(bodies[m:])/statistics.mean(bodies[:m])

def run():
    p=w.read(RUN/'protocol.json');old=w.read(w.SOURCE/'full-portfolio.json')
    duplicate={q:w.sha(RUN/'near_start'/q/'evaluations.jsonl')==w.sha(RUN/'near_turn'/q/'evaluations.jsonl') for q in ('early','recent')};assert all(duplicate.values())
    results={policy:{q:{} for q in ('early','recent','recent_aug_oct')} for policy in ('original','two_paths_flat')};accepted={};ledger={};leave={};audit={};funnel={};qa=collections.Counter();pool_diffs={};pool_archive={};regression=[]
    inputs={q:w.read(p['periods'][q]['input']) for q in ('early','recent')};bars={q:{sym:identification_bars(v['bars']) for sym,v in inp['symbols'].items()} for q,inp in inputs.items()};indices={q:{sym:first_index(arr) for sym,arr in symbols.items()} for q,symbols in bars.items()}
    calendars={q:s.TradingDates(inp['symbols']) for q,inp in inputs.items()};fees=w.read(p['periods']['early']['fees']);del inputs
    original_pools={q:{x['event_key']:x for x in w.read(w.SOURCE/(q+'-pool.json'))} for q in ('early','recent')};legacy_shapes={}
    for wave in WAVES:
        folder=RUN/wave;events={};snapshots={};ends={};cases=collections.defaultdict(list);audit[wave]={};funnel[wave]={}
        for q in ('early','recent'):
            pool=w.read(folder/(q+'-pool.json'));bykey={x['event_key']:x for x in pool};snapshots.update(bykey);events.update({k:x['event'] for k,x in bykey.items()})
            shaped={shape_key(x['event']):x for x in pool};assert len(shaped)==len(pool)
            for snap in pool:
                e=snap['event'];ratio=weak_from_bars(e,bars[q][e['symbol']],indices[q][e['symbol']]);actual=snap['b_weakening_ex_warning']
                assert (ratio is None and actual is None) or ratio is not None and actual is not None and abs(ratio-actual)<1e-9;qa['causal_b_weakness']+=1
                assert e['evaluation'] is None and e['outcome'] is None and e['entry_ts'] is None;qa['unlabelled_warning']+=1
            if wave=='legacy':
                legacy_shapes[q]=shaped;prior=original_pools[q];common=bykey.keys()&prior.keys()
                diffs={}
                for field in ('s0_ts','s1_ts','s2_ts','entry','stop','target','a_bars','b_bars','a_move','b_move'):
                    mismatch=[k for k in common if not equivalent(bykey[k]['event'][field],prior[k]['event'][field])]
                    if mismatch:diffs[field]=mismatch
                pool_archive[q]=dict(new=len(pool),old=len(prior),added=sorted(bykey.keys()-prior.keys()),removed=sorted(prior.keys()-bykey.keys()),field_differences=diffs)
            reference=legacy_shapes[q];common=reference.keys()&shaped.keys();changes={}
            for field in ('s0_ts','s1_ts','entry','stop','target','a_bars','b_bars','level'):
                changes[field]=sum(not equivalent(reference[k]['event'][field],shaped[k]['event'][field]) for k in common)
            assert changes['entry']==changes['stop']==0,'Wave choice unexpectedly changed warning entry/stop'
            pool_diffs[(wave,q)]=dict(pool=len(pool),common_warning_times=len(common),new_warning_times=len(shaped.keys()-reference.keys()),missing_warning_times=len(reference.keys()-shaped.keys()),changes=changes)
            for code,t in w.read(folder/q/'manifest.json')['contract_ends'].items():ends[code]=max(t,ends.get(code,''))
            status=collections.defaultdict(collections.Counter);base_results={};rows=0
            for line in (folder/q/'evaluations.jsonl').open(encoding='utf-8'):
                r=json.loads(line);profile=r['scenario'].split('/')[0].removeprefix('P2_wave_');suffix=r['scenario'].split('/',1)[1];snap=bykey[r['event_key']];kind=snap['event']['warning_kind'];weak=snap['b_weakening_ex_warning'];weak=weak is not None and weak<=.75
                if profile=='base':base_results[(r['event_key'],suffix)]=r['result']
                if profile in ('b_strong','b_all') and (weak or kind=='cumulative' or profile=='b_strong' and kind!='strong'):
                    assert r['result']==base_results[(r['event_key'],suffix)];qa['unchanged_B_routes_exact']+=1
                rows+=1;status[r['scenario']][s.ev(r)['status']]+=1
                for fill in r['result']['fills']:
                    assert fill['ts']>r['warning_ts'];assert abs(fill['price']/r['tick_size']-round(fill['price']/r['tick_size']))<1e-6;qa['fill_tick_time']+=1
                e=s.ev(r)
                if e['status']=='closed':
                    fs=r['result']['fills'];assert len(fs)==2 and fs[0]['volume']==fs[1]['volume']==1
                    gross=(fs[1]['price']-fs[0]['price'])*(1 if r['direction']=='up' else -1)*r['multiplier'];assert abs(gross-e['net_amount'])<1e-6;assert abs(e['risk']-abs(fs[0]['price']-r['stop']))<1e-6;qa['raw_cash_risk']+=1
                if not(e.get('trigger_ts') or e['entry_ts']) and e['status'] not in s.flat.s.UNKNOWN:continue
                r=s.reprice(r,fees,calendars[q]);r['period']=q
                if s.ev(r)['status']=='closed':r['exit_trading_day']=calendars[q].day(s.ev(r)['exit_ts']);assert abs(s.ev(r)['net_r']-s.ev(r)['net_amount']/(s.ev(r)['risk']*r['multiplier']))<1e-9;qa['net_R']+=1
                cases[r['scenario']].append(r)
            audit[wave][q]=dict(pool=len(pool),raw_evaluations=rows);funnel[wave][q]={k:dict(v) for k,v in status.items()}
        for scenario,rows in cases.items():
            short=scenario.removeprefix('P2_wave_');case=wave+'/'+short
            for policy in results:
                select=(lambda rs,reverse=False:s.portfolio(rs,events,ends,reverse=reverse)) if policy=='original' else (lambda rs,reverse=False:s.flat.exposure_portfolio(rs,ends,reverse))
                a,l=select(rows);reverse,_=select(rows,True);accepted[(policy,case)]=a;ledger[(policy,case)]=l;leave[(policy,case)]={}
                for q in results[policy]:
                    keep=lambda r:r['period']==q if q!='recent_aug_oct' else r['period']=='recent' and r['warning_ts']>='2026-08-01 00:00:00'
                    zz=[r for r in a if keep(r)];m=s.describe(zz,events);m['reverse_tie_net']=s.metrics([r for r in reverse if keep(r)])['net'];m['active_warning_months']=len({r['warning_ts'][:7] for r in zz if s.ev(r)['status']=='closed'});results[policy][q][case]=m
                    if wave=='legacy' and short.split('/')[0] in ('base','adaptive') and q!='recent_aug_oct':
                        name=short.split('/')[0];b=old[policy][q]['P2_ab_'+short];bad={k:(m[k],b[k]) for k in b if k in m and not equivalent(m[k],b[k])}
                        regression.append(dict(policy=policy,period=q,case=case,exact=not bad,differences=bad));assert not bad,('Legacy reference changed',regression[-1])
                # No shared account/capital/online state: removing a symbol does
                # not change any other symbol's accepted trades.
                for sym in sorted({e['symbol'] for e in events.values()}):leave[(policy,case)][sym]={q:s.metrics([r for r in a if r['period']==q and r['symbol']!=sym]) for q in ('early','recent')}
    gates={};compromises=[]
    for wave in WAVES:
        for profile in PROFILES:
            name=wave+'/'+profile;checks={};dominates=True
            for policy in results:
                checks[policy]={}
                for q in ('early','recent'):
                    checks[policy][q]={}
                    for mode in p['modes']:
                        for slip in (1,2):
                            suffix=f'{mode}/slip{slip}';m=results[policy][q][name+'/'+suffix];b=results[policy][q]['legacy/base/'+suffix];obs=old[policy][q]['P2_ab_adaptive/'+suffix]
                            floor=.8*max(b['net'],obs['net'])
                            flags=dict(positive_cash=m['net']>0,positive_R=m['mean_r'] is not None and m['mean_r']>0,recent_retention=q!='recent' or m['net']>=floor-1e-8,
                                minimum_sample=m['n']>=max(35,.3*b['n']),months=m['active_warning_months']>=3,
                                win_rate=q!='recent' or (m['win_rate'] or 0)>=(b['win_rate'] or 0)-1e-12,drawdown=m['realized_cash_drawdown']<=b['realized_cash_drawdown']+1e-8,
                                tail10=m['tail10_cash'] is not None and m['tail10_cash']>=b['tail10_cash']-1e-8,unknown=m['unknown']<=b['unknown'])
                            checks[policy][q][suffix]=flags
                            if q=='recent':dominates &= flags['recent_retention'] and m['unknown']<=obs['unknown']
                            else:dominates &= m['net']>=obs['net']-1e-8 and m['mean_r'] is not None and m['mean_r']>=obs['mean_r']-1e-12 and m['realized_cash_drawdown']<=obs['realized_cash_drawdown']+1e-8 and m['unknown']<=obs['unknown']
            passed=all(all(f.values()) for policy in checks.values() for q in policy.values() for f in q.values());gates[name]=dict(passed=passed,checks=checks,defensive_dominates_previous=bool(dominates))
            if dominates and name!='legacy/adaptive':compromises.append(name)
    assert all(w.sha(k)==h for k,h in p['production_before'].items())
    for root in (RUN,RUN/'unmerged_extension'):assert all(w.sha(root/'fork'/k)==v for k,v in w.read(root/'implementation-manifest.json').items())
    w.write(RUN/'results.json',results);w.write(RUN/'gates.json',gates);w.write(RUN/'pool-archive-comparison.json',pool_archive);w.write(RUN/'wave-reselection.json',{str(k):v for k,v in pool_diffs.items()});w.write(RUN/'source-funnel.json',funnel)
    w.write(RUN/'leave-product-out.json',{str(k):v for k,v in leave.items()});w.write(RUN/'opportunity-ledgers.json',{str(k):v for k,v in ledger.items()})
    with (RUN/'accepted.jsonl').open('x',encoding='utf-8') as f:
        for (policy,case),a in accepted.items():
            for r in a:f.write(json.dumps(dict(policy=policy,case=case,period=r['period'],row=r),ensure_ascii=False)+'\n')
    verification=dict(qa=dict(qa),archive_regressions=regression,duplicate_near_turn=duplicate,unique_wave_models=4,execution_profiles=4,unique_models=16,
        period_stats=audit,production_unchanged=len(p['production_before']),production_changed=False,qualified_for_trading=False,
        full_passed=[k for k,v in gates.items() if v['passed']],defensive_compromises=compromises,results_sha256=w.sha(RUN/'results.json'))
    w.write(RUN/'verification.json',verification)
    lines=['# 重新选择A/B与B衰竭分层进场','',
        '本轮从完整5分钟历史重新提取全部预警，重做真实服务收盘线去重、评分/成本/风险筛选及触发时刻同品种机会竞争。所有历史均反复研究，不能称新独立验证。未改生产代码和实盘参数。',
        '', '## 冻结实验','',
        'A选择：原评分；先剔除不合格结构再评分；最近有效S0；保留所有已确认摆点、不做相近摆点合并后选择最近S0。所谓最近S1与最近S0两种方案在本数据的预警池及完整评估逐字节相同，按重复模型处理。',
        '', 'B执行：原触发；上一轮strong_kind_session观察版；只有B未显出衰竭的strong警报等分钟收盘确认；对所有未衰竭非累计警报等确认。衰竭比值使用预警前B向实体后半均值/前半均值<=.75，缺失单列且按未证实处理，累计警报沿用原触发。共4×4=16个不同模型，不逐品种/月选参。',
        '', '保留原S2止损、目标计算规则、最低10tick、摩擦<=.2R、净空间>=1、假日/周末、两档滑点及额外一分钟入场压力。更近A会改变S1、评分、目标与事件身份，对共同预警的原入场价和止损价没有改变。',
        '', '## 每边1tick结果','', '|A选择 / 执行|上半年笔数|净额|平均R|近期净额|相对此前观察版|','|---|---:|---:|---:|---:|---:|']
    for wave in WAVES:
        for profile in PROFILES:
            name=wave+'/'+profile;e=results['original']['early'][name+'/corrected_touch/slip1'];n=results['original']['recent'][name+'/corrected_touch/slip1'];benchmark=old['original']['recent']['P2_ab_adaptive/corrected_touch/slip1']['net']
            lines.append(f"|{name}|{e['n']}|{e['net']:.2f}|{e['mean_r']:.4f}|{n['net']:.2f}|{n['net']/benchmark:.1%}|")
    lines+=['', '全部严格门槛通过：'+str(verification['full_passed']), '两种占用口径与四压力场景均满足近期80%保留、上半年现金/R/回撤/未知不劣于此前观察版的分支：'+str(compromises),
        '', '所有门槛、未通过项目、8—10月子样本和逆序同刻竞争保留于results.json/gates.json。未知盈亏继续单列；只有两条OHLC路径都已空仓时才在占用敏感性中释放名额，不能把未知盈亏记为0。',
        '', '## 实现核验与边界','',
        f"完整再生成的原策略/上一轮观察版与归档结果共{len(regression)}个时期/成本/占用场景回归一致。重选波段前后通过symbol/direction/warning_time配对；共同预警entry/stop未改变。B衰竭独立Python重算、路由应保持不变的完整成交结果、成交tick/时间/现金/风险和扣费净R均核对。",
        '', '生成阶段没有读取/构造未来成交或收益标签；调用实际服务has_similar_warning与advance_event_model，仅按收盘前缀推进。两套前缀测试覆盖原选择、结构资格优先、最近锚点与取消摆点合并。元数据摩擦占位和测试fixture字段在任何收益回放前修正，原清单与修正记录均保留。',
        '', '独立核验最初采用按桶汇总，发现5处B衰竭数值差异、其中2处跨越.75分类阈值。实际识别聚合会在5分钟断档处分割部分柱，即使结束标签相同。核验脚本改为独立复刻这一规则后，全部B衰竭值及B索引根数一致；没有修改信号、行情或重跑选择收益。原差异与核验记录均保留。此前报告个别AB根数差异也涉及这一聚合边界，不能据此断言波段生成存在1根错位。',
        '', '近期仍是品种起点不齐的现有窗口，不能称完整等覆盖三/四个月。没有真实逐报价盘口、账户资本/保证金或浮动权益回放，本表为每手已平仓现金/R，不是账户收益率或完整权益回撤。']
    (RUN/'report.md').write_text('\n'.join(lines),encoding='utf-8');print('\n'.join(lines[12:35]),flush=True)

if __name__=='__main__':run()
