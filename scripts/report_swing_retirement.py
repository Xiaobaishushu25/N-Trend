"""Report all frozen A-state hypotheses, controls, opportunity effects and failures."""
import collections,json,math,statistics
import research_swing_retirement as r
import research_swing_origin_break as price
import research_swing_abstop as s
from research_swing_combination import weekly_delta
from report_swing_wave_selection import identification_bars

RUN=r.RUN;w=r.w
MODELS={**r.MODELS,**price.MODELS}
def same(a,b):
    if isinstance(a,(int,float)) and isinstance(b,(int,float)):return abs(a-b)<1e-7
    return a==b
def identity(row):return row['symbol'],row['direction'],row['warning_ts']
def closed(rows):return {identity(z):z for z in rows if s.ev(z)['status']=='closed'}

def run():
    p=w.read(RUN/'protocol.json');fees=w.read(p['periods']['early']['fees']);reference=w.read(r.SOURCE/'results.json')
    results={policy:{q:{} for q in ('early','recent','recent_aug_oct')} for policy in ('original','two_paths_flat')};accepted={};ledgers={};audits={};regression=[];qa=collections.Counter();funnels={};all_events={};traces={};leave={};unknown_ids={}
    for model,mode in MODELS.items():
        folder=RUN/model;events={};cases=collections.defaultdict(list);ends={};audits[model]={};funnels[model]={};traces[model]={}
        for q in ('early','recent'):
            pool=w.read(folder/(q+'-pool.json'));snapshots={z['event_key']:z for z in pool};events.update({k:z['event'] for k,z in snapshots.items()});inp=w.read(p['periods'][q]['input']);cal=s.TradingDates(inp['symbols'])
            for code,t in w.read(folder/q/'manifest.json')['contract_ends'].items():ends[code]=max(t,ends.get(code,''))
            trace_summary=collections.Counter();blocked=set();retirements=[];causes=collections.defaultdict(set)
            for f in sorted((folder/(q+'-symbols')).glob('*.json')):
                saved=w.read(f);times=saved['identification_times'];last={};rawkeys=saved['raw_keys'];kept=saved['kept_keys'];trace_summary['raw_warnings']+=len(rawkeys);trace_summary['service_kept']+=len(kept)
                bars=identification_bars(inp['symbols'][f.stem]['bars']);assert [b['datetime'] for b in bars]==times
                if model=='near_start':
                    old_checkpoint=w.read(r.SOURCE/'near_start'/(q+'-symbols')/f.name);assert rawkeys==old_checkpoint['raw_keys'] and kept==old_checkpoint['kept_keys'];qa['exact_raw_and_service_warning_keys']+=1
                for z in saved['state_trace']:
                    cause='no_current_owner'
                    assert z['w']<len(times);assert z['owner_s0'] is None or z['owner_s0']+2<=z['owner_s1']<=z['w']-2
                    if z['owner_s0'] is not None:
                        i,j,k=z['owner_s0'],z['owner_s1'],z['w'];up=z['direction']=='up';a0=bars[i]['low' if up else 'high'];a1=bars[j]['high' if up else 'low'];bend=bars[k]['low' if up else 'high'];sgn=1 if up else -1
                        assert a1==(max(b['high'] for b in bars[i+1:k]) if up else min(b['low'] for b in bars[i+1:k]));amove=(a1-a0)*sgn;bmove=(a1-bend)*sgn;assert amove>0
                        extreme=(min(b['low'] for b in bars[j+1:k+1]) if up else max(b['high'] for b in bars[j+1:k+1]));path=(extreme-a0)*sgn<=0;deep=bmove>0 and (bend-extreme)*sgn>.5*bmove
                        assert z['owner_failed']==(path if mode>=8 else path or deep or bmove/amove>.8);qa['independent_price_failure_check']+=1
                        cause='actual_S0_break' if path else 'deep_V' if deep else 'retracement_over80' if bmove/amove>.8 else 'other_owner_or_shallow'
                    if z['newly_retired']:
                        assert mode in (6,9) and z['owner_failed'] and z['retired']==z['owner_s0'];assert z['retired']>last.get(z['direction'],-1);last[z['direction']]=z['retired'];trace_summary['retirements_full_input']+=1
                        retirements.append(dict(symbol=f.stem,direction=z['direction'],ts=times[z['w']],s0_ts=times[z['owner_s0']],s1_ts=times[z['owner_s1']]))
                    if z['blocked_s0'] is None:continue
                    if mode in (5,8):assert z['owner_failed'] and z['blocked_s0']<=z['owner_s0']
                    elif mode in (6,9):assert z['retired']==last[z['direction']] and z['blocked_s0']<=z['retired']
                    elif mode==7:assert (z['blocked_s0'],z['blocked_s1'])!=(z['owner_s0'],z['owner_s1'])
                    qa['state_trace_causal_invariants']+=1;trace_summary['blocked_level_patterns_full_input']+=1
                    if z['would_warn'] and z['warning_score']>=2.5:
                        key=(f.stem,z['direction'],times[z['w']]);blocked.add(key);causes[key].add('memory_only' if mode in (6,9) and not z['owner_failed'] else cause)
                if model=='near_start':assert not saved['state_trace']
            trace_summary['unique_blocked_scored_warning_times_full_input']=len(blocked);traces[model][q]=dict(summary=dict(trace_summary),blocked_warning_times=[list(k) for k in sorted(blocked)],blocked_causes={'|'.join(k):sorted(v) for k,v in causes.items()},retirements=retirements)
            statuses=collections.defaultdict(collections.Counter);n=0
            for line in (folder/q/'evaluations.jsonl').open(encoding='utf-8'):
                z=json.loads(line);suffix=z['scenario'].split('/',1)[1];e=s.ev(z);snap=snapshots[z['event_key']]
                assert snap['event']['outcome'] is None and snap['event']['evaluation'] is None and snap['event']['entry_ts'] is None;qa['unlabelled_warning']+=1
                for f in z['result']['fills']:
                    assert f['ts']>z['warning_ts'];assert abs(f['price']/z['tick_size']-round(f['price']/z['tick_size']))<1e-6;qa['fill_tick_time']+=1
                if e['status']=='closed':
                    fs=z['result']['fills'];assert len(fs)==2;gross=(fs[1]['price']-fs[0]['price'])*(1 if z['direction']=='up' else -1)*z['multiplier'];assert abs(gross-e['net_amount'])<1e-6;qa['cash_reconciled']+=1
                statuses[suffix][e['status']]+=1;n+=1
                z=s.reprice(z,fees,cal);z['period']=q
                if s.ev(z)['status']=='closed':z['exit_trading_day']=cal.day(s.ev(z)['exit_ts']);assert abs(s.ev(z)['net_r']-s.ev(z)['net_amount']/(s.ev(z)['risk']*z['multiplier']))<1e-9
                cases[suffix].append(z)
            funnels[model][q]={k:dict(v) for k,v in statuses.items()};audits[model][q]=dict(pool=len(pool),evaluations=n,trace=dict(trace_summary))
            if model=='near_start':
                assert pool==w.read(r.SOURCE/'near_start'/(q+'-pool.json'));qa['exact_control_pools']+=1
        all_events[model]=events
        for suffix,rows in cases.items():
            case=model+'/'+suffix
            for policy in results:
                select=(lambda zz,reverse=False:s.portfolio(zz,events,ends,reverse=reverse)) if policy=='original' else (lambda zz,reverse=False:s.flat.exposure_portfolio(zz,ends,reverse))
                a,ledger=select(rows);rev,_=select(rows,True);accepted[(policy,case)]=a;ledgers[(policy,case)]=ledger
                for q in results[policy]:
                    keep=lambda z:z['period']==q if q!='recent_aug_oct' else z['period']=='recent' and z['warning_ts']>='2026-08-01 00:00:00'
                    zs=[z for z in a if keep(z)];m=s.describe(zs,events);m['reverse_tie_net']=s.metrics([z for z in rev if keep(z)])['net'];results[policy][q][case]=m
                    unknown_ids[(policy,q,case)]=[list(identity(z)) for z in zs if s.ev(z)['status'] in s.flat.s.UNKNOWN]
                    if model=='near_start':
                        ref=reference[policy][q]['near_start/adaptive/'+suffix];bad={k:[m[k],ref[k]] for k in ref if k in m and not same(m[k],ref[k])};assert not bad,('control mismatch',policy,q,suffix,bad);regression.append(dict(policy=policy,period=q,scenario=suffix,exact=True))
                leave[(policy,case)]={sym:{q:s.metrics([z for z in a if z['period']==q and z['symbol']!=sym]) for q in ('early','recent')} for sym in sorted({z['symbol'] for z in rows})}
    gates={};attribution={};eligible=[]
    for model in MODELS:
        gates[model]={};dominates=True;full=True;attribution[model]={}
        for policy in results:
            gates[model][policy]={};attribution[model][policy]={}
            for q in ('early','recent'):
                gates[model][policy][q]={};attribution[model][policy][q]={}
                for suffix in ('corrected_touch/slip1','corrected_touch/slip2','next_minute_open/slip1','next_minute_open/slip2'):
                    case=model+'/'+suffix;base='near_start/'+suffix;m=results[policy][q][case];b=results[policy][q][base]
                    flags=dict(positive_cash=m['net']>0,positive_R=m['mean_r'] is not None and m['mean_r']>0,minimum_sample=m['n']>=max(35,.3*b['n']),unknown=m['unknown']<=b['unknown'],
                        recent_retention=q!='recent' or m['net']>=.8*b['net']-1e-8,win_rate=q!='recent' or (m['win_rate'] or 0)>=(b['win_rate'] or 0)-1e-12,
                        drawdown=m['realized_cash_drawdown']<=b['realized_cash_drawdown']+1e-8,tail10=m['tail10_cash'] is not None and m['tail10_cash']>=b['tail10_cash']-1e-8,
                        early_cash=q!='early' or m['net']>=b['net']-1e-8,early_R=q!='early' or m['mean_r'] is not None and m['mean_r']>=b['mean_r']-1e-12)
                    gates[model][policy][q][suffix]=flags;full &= all(flags.values());dominates &= flags['unknown'] and flags['minimum_sample'] and (flags['recent_retention'] if q=='recent' else flags['early_cash'] and flags['early_R'] and flags['drawdown'])
                    old=[z for z in accepted[(policy,base)] if z['period']==q];new=[z for z in accepted[(policy,case)] if z['period']==q];aa=closed(old);bb=closed(new);common=aa.keys()&bb.keys();lost=aa.keys()-bb.keys();added=bb.keys()-aa.keys()
                    delta_common=sum(s.ev(bb[k])['net_amount']-s.ev(aa[k])['net_amount'] for k in common);old_only=sum(s.ev(aa[k])['net_amount'] for k in lost);new_only=sum(s.ev(bb[k])['net_amount'] for k in added)
                    assert abs(m['net']-b['net']-(delta_common+new_only-old_only))<1e-6
                    blocked={tuple(k) for k in traces[model][q]['blocked_warning_times']};products=collections.defaultdict(float)
                    for sign,zz in ((1,new),(-1,old)):
                        for z in zz:
                            if s.ev(z)['status']=='closed':products[z['symbol']]+=sign*s.ev(z)['net_amount']
                    reasons=collections.defaultdict(list)
                    for k in lost:
                        if k in blocked:reasons['+'.join(traces[model][q]['blocked_causes']['|'.join(k)])].append(aa[k])
                    attribution[model][policy][q][suffix]=dict(common_closed=len(common),common_cash_delta=delta_common,old_only_n=len(lost),old_only_net=old_only,new_only_n=len(added),new_only_net=new_only,
                        removed_reference_closed_by_state=sum(k in blocked for k in lost),removed_reference_net_by_state=sum(s.ev(aa[k])['net_amount'] for k in lost if k in blocked),
                        removed_reference_by_causal_reason={k:dict(n=len(zs),net=sum(s.ev(z)['net_amount'] for z in zs),wins=sum(s.ev(z)['net_amount']>0 for z in zs)) for k,zs in reasons.items()},
                        by_product_delta=dict(sorted(products.items(),key=lambda x:x[1],reverse=True)),warning_week_delta_interval=weekly_delta(old,new))
        gates[model]['defensive_noninferior']=bool(dominates);gates[model]['full_stability']=bool(full)
        if model!='near_start' and dominates:eligible.append(model)
    assert all(w.sha(k)==h for k,h in p['production_before'].items())
    for root in (RUN,price.RUN):assert all(w.sha(root/'fork'/k)==h for k,h in w.read(root/'implementation-manifest.json').items())
    w.write(RUN/'results.json',results);w.write(RUN/'gates.json',gates);w.write(RUN/'attribution.json',attribution);w.write(RUN/'state-audit.json',traces);w.write(RUN/'source-funnel.json',funnels)
    w.write(RUN/'leave-product-out.json',{str(k):v for k,v in leave.items()});w.write(RUN/'unknown-identities.json',{str(k):v for k,v in unknown_ids.items()});w.write(RUN/'opportunity-ledgers.json',{str(k):v for k,v in ledgers.items()})
    with (RUN/'accepted.jsonl').open('x',encoding='utf-8') as f:
        for (policy,case),zs in accepted.items():
            for z in zs:f.write(json.dumps(dict(policy=policy,case=case,period=z['period'],row=z),ensure_ascii=False)+'\n')
    w.write(RUN/'verification.json',dict(qa=dict(qa),controls=regression,period_stats=audits,defensive_candidates=eligible,full_passed=[k for k,v in gates.items() if v['full_stability']],production_unchanged=len(p['production_before']),production_changed=False,qualified_for_trading=False,results_sha256=w.sha(RUN/'results.json')))
    lines=['# 最近推动失效后，旧A是否应停止复用','', '冻结3种机制，并在任何收益汇总前增加“仅实际穿越S0”的当前/永久两种对照；共5种新机制，与上一轮near_start/adaptive作完整再生成对照。两时期均为反复研究的开发数据，没有独立验证。生产未改。',
        '', '当前失效拦截：先识别最近有实际推动的A，再看它是否按原规则破位、深V或回撤超过80%；失效时拦截同起点及更旧A。',
        '', '起点永久退役：每根已完成15分钟柱更新同方向起点下界，失效后即使同起点后来延伸出新S1，也不复活；只有更晚确认的新S0能重新开始。方向分别记忆。',
        '', '最近推动独占：只允许当前最新有实际推动的S0/S1形成警报；其不合格时也不回看旧A。它与永久退役分开检验。',
        '', 'current_origin_break / retired_origin_break：只把B路径实际触及或穿越S0当失效，分别做当前拦截和起点永久退役；不用80%回撤或深V单独判死。扩展定义在开收益汇总前冻结，原3组未改。',
        '', '“有实际推动”采用原A/B长度上限、最少3根A/2根B、非微型N、至少1根放宽趋势柱、换月隔离；暂不考虑当前B等级和S0是否存活。这是可检验的代理定义，不等于真实走势标签。原止损、目标规则、小时执行、日历、费用、去重与机会竞争均保留。',
        '', '|规则|上半年笔数|净额|平均R|胜率|已实现回撤|近期净额|近期保留|近期未知|','|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for model in MODELS:
        e=results['original']['early'][model+'/corrected_touch/slip1'];n=results['original']['recent'][model+'/corrected_touch/slip1'];b=results['original']['recent']['near_start/corrected_touch/slip1'];lines.append(f"|{model}|{e['n']}|{e['net']:.2f}|{e['mean_r']:.4f}|{e['win_rate']:.1%}|{e['realized_cash_drawdown']:.2f}|{n['net']:.2f}|{n['net']/b['net']:.1%}|{n['unknown']}|")
    lines+=['', '## 全部成本与延迟压力','', '|规则|场景|上半年净额|平均R|近期净额|近期保留|','|---|---|---:|---:|---:|---:|']
    for model in MODELS:
        for suffix in ('corrected_touch/slip1','corrected_touch/slip2','next_minute_open/slip1','next_minute_open/slip2'):
            e=results['original']['early'][model+'/'+suffix];n=results['original']['recent'][model+'/'+suffix];b=results['original']['recent']['near_start/'+suffix];lines.append(f"|{model}|{suffix}|{e['net']:.2f}|{e['mean_r']:.4f}|{n['net']:.2f}|{n['net']/b['net']:.1%}|")
    lines+=['', '两种占用口径、四压力场景满足近期>=80%、上半年现金/R/回撤不劣于冻结观察版且未知/样本合格：'+str(eligible), '全部严格稳定门槛通过：'+str([k for k,v in gates.items() if v['full_stability']]),
        '', '控制24个时期/场景/占用指标与归档一致（含8—10月子集）；前缀测试同时核对警报和状态日志。共同交易、新纳入、不再纳入机会的归因及预警周描述区间见attribution.json；不能把改变机会数的净额差直接说成信号准确率提升。',
        '', '未知交易收益继续单列，不能按0计。原口径预留名额到合约结束；敏感性口径仅在两条路径都已空仓时释放。不同品种近期起点不齐，非等覆盖四个月。金额为单手已平仓净额，回撤为已实现现金回撤，非账户浮动权益回撤。next_minute_open是一分钟额外入场延迟，非真实下一报价。',
        '', '本轮不继续追加阈值网格或逐品种删选。是否保留某机制需同时看收益、成本压力、样本、未知和贡献来源；研究最优也不能直接转实盘。']
    lines+=['', '永久状态从各品种输入历史起点逐根推进。本轮未实现实盘持久化；将来若接入滚动历史，必须保存状态或证明足够预热，不能仅以本轮完整历史前缀测试代替。']
    (RUN/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8');print('\n'.join(lines[10:18]),flush=True);print('Defensive candidates:',eligible,flush=True)

if __name__=='__main__':run()
