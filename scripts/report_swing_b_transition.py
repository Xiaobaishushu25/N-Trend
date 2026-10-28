"""Full-cost B transition replay, exact best-a control and fixed-cohort attribution."""
import collections,json,math,statistics
import research_swing_b_transition as t
import research_swing_abstop as s
from research_swing_combination import weekly_delta
from swing_b_identity import active,features
from report_swing_wave_selection import identification_bars,first_index
w=t.w;RUN=t.RUN
def same(a,b):
    if isinstance(a,(int,float)) and isinstance(b,(int,float)):return abs(a-b)<1e-7
    return a==b
def phase(f):
    return ('awaiting_break_fresh_B_extreme' if f['fresh_b_extreme'] else 'awaiting_break_inside_B') if f['required'] else f['state']
def enhance(rows,events):
    m=s.describe(rows,events);zz=[z for z in rows if s.ev(z)['status']=='closed']
    m['mean_original_nominal_r']=statistics.mean(s.ev(z)['net_amount']/(z['original_risk']*z['multiplier']) for z in zz) if zz else None
    m['mean_actual_cash_risk']=statistics.mean(s.ev(z)['risk']*z['multiplier'] for z in zz) if zz else None
    return m

def run():
    t.verify_snapshot();p=w.read(RUN/'protocol.json');ref=w.read(t.SNAP/'results.json');fees=w.read(p['periods']['early']['fees']);qa=collections.Counter();cases=collections.defaultdict(dict);events={};snaps={};ends={};funnel={};qa_control=[];individual={}
    for q in ('early','recent'):
        pool=w.read(RUN/(q+'-pool.json'));source=w.read(t.SNAP/(q+'-pool.json'));assert len(pool)==len(source)
        assert all({k:v for k,v in a.items() if k!='b_transition'}==b for a,b in zip(pool,source));qa['unchanged_parent_warning_pool']+=len(pool)
        inp=w.read(p['periods'][q]['input']);cal=s.TradingDates(inp['symbols']);bs={sym:identification_bars(z['bars']) for sym,z in inp['symbols'].items()};indices={sym:first_index(arr) for sym,arr in bs.items()};rawtimes={sym:[b['datetime'] for b in z['bars']] for sym,z in inp['symbols'].items()};baseline={};status=collections.defaultdict(collections.Counter)
        for i,snap in enumerate(pool):
            e=snap['event'];f=snap['b_transition'];sym=e['symbol'];snaps[snap['event_key']]=snap;events[snap['event_key']]=e
            assert e['evaluation'] is None and e['outcome'] is None and e['entry_ts'] is None
            if f['pivot_ts']:
                raw=inp['symbols'][sym]['bars'];idx=rawtimes[sym].index(f['pivot_ts']);a,b,c=raw[idx-1:idx+2];up=e['direction']=='up';field='high' if up else 'low'
                assert e['s1_ts']<a['datetime']<b['datetime']<c['datetime']==f['confirmation_ts']<=f['anchor_cutoff']<e['warning_ts'];assert c['datetime'][:16]!=e['warning_ts'][:16]
                assert (b[field]>a[field] and b[field]>c[field]) if up else (b[field]<a[field] and b[field]<c[field]);assert b[field]==f['pivot_price'];qa['independent_internal_pivot_price_time']+=1
            if i%61==0:
                n=__import__('bisect').bisect_right(rawtimes[sym],f['anchor_cutoff']);actual=features(e,inp['symbols'][sym]['bars'][:n],bs[sym],indices[sym]);assert actual==f;qa['feature_full_vs_pre_warning_prefix']+=1
        for code,end in w.read(RUN/q/'manifest.json')['contract_ends'].items():ends[code]=max(end,ends.get(code,''))
        for line in (RUN/q/'evaluations.jsonl').open(encoding='utf-8'):
            z=json.loads(line);profile,suffix=z['scenario'].removeprefix('P2_transition_').split('/',1);snap=snaps[z['event_key']];f=snap['b_transition'];e=snap['event'];expected=active(profile,f);key=(z['event_key'],suffix)
            assert z['micro_applied']==expected;assert z['micro_confirm']==(expected and profile.endswith('close'));assert z['parent_leg_id']==f['parent_leg_id'];assert z['stop']==e['stop'];assert z['target']==z['baseline_target'];qa['routing_fixed_stop_absolute_target']+=1
            assert abs(z['planned_risk']-abs(z['entry']-z['stop']))<1e-8
            if profile=='base':baseline[key]=z
            else:
                assert z['baseline_target']==baseline[key]['target']
                if not expected:assert z['result']==baseline[key]['result'];assert z['entry']==baseline[key]['entry'];qa['inactive_routes_full_result_exact']+=1
            if expected:assert z['entry']==f['level'];assert (z['entry']-e['entry'])*(1 if e['direction']=='up' else -1)>0
            for fill in z['result']['fills']:
                assert fill['ts']>e['warning_ts'];assert abs(fill['price']/z['tick_size']-round(fill['price']/z['tick_size']))<1e-6;qa['fill_tick_time']+=1
            if s.ev(z)['status']=='closed':
                fs=z['result']['fills'];assert len(fs)==2;gross=(fs[1]['price']-fs[0]['price'])*(1 if e['direction']=='up' else -1)*z['multiplier'];assert abs(gross-s.ev(z)['net_amount'])<1e-6;qa['gross_cash']+=1
            status[profile+'/'+suffix][s.ev(z)['status']]+=1
            z=s.reprice(z,fees,cal);z['period']=q
            if s.ev(z)['status']=='closed':z['exit_trading_day']=cal.day(s.ev(z)['exit_ts']);assert abs(s.ev(z)['net_r']-s.ev(z)['net_amount']/(s.ev(z)['risk']*z['multiplier']))<1e-9
            cases[profile+'/'+suffix][z['event_key']]=z
        funnel[q]={k:dict(v) for k,v in status.items()}
    results={pol:{q:{} for q in ('early','recent','recent_aug_oct')} for pol in ('original','two_paths_flat')};accepted={};ledgers={};leave={};states={};unknown={}
    for case,mapping in cases.items():
        rows=list(mapping.values())
        for pol in results:
            select=(lambda rs,reverse=False:s.portfolio(rs,events,ends,reverse=reverse)) if pol=='original' else (lambda rs,reverse=False:s.flat.exposure_portfolio(rs,ends,reverse))
            aa,ll=select(rows);rev,_=select(rows,True);accepted[(pol,case)]=aa;ledgers[(pol,case)]=ll
            for q in results[pol]:
                keep=lambda z:z['period']==q if q!='recent_aug_oct' else z['period']=='recent' and z['warning_ts']>='2026-08-01 00:00:00'
                zz=[z for z in aa if keep(z)];m=enhance(zz,events);m['reverse_tie_net']=s.metrics([z for z in rev if keep(z)])['net'];results[pol][q][case]=m;unknown[(pol,q,case)]=[z['event_key'] for z in zz if s.ev(z)['status'] in s.flat.s.UNKNOWN]
                if case.startswith('base/'):
                    b=ref[pol][q][case.split('/',1)[1]];bad={k:[m[k],b[k]] for k in b if k in m and not same(m[k],b[k])};assert not bad,(pol,q,case,bad);qa_control.append(dict(policy=pol,period=q,case=case,exact=True))
                    groups=collections.defaultdict(list)
                    for z in zz:groups[phase(snaps[z['event_key']]['b_transition'])].append(z)
                    states[(pol,q,case.split('/',1)[1])]={k:enhance(v,events) for k,v in groups.items()}
            leave[(pol,case)]={sym:{q:s.metrics([z for z in aa if z['period']==q and z['symbol']!=sym]) for q in ('early','recent')} for sym in sorted({z['symbol'] for z in rows})}
    gates={};attribution={};fixed={};defensive=[];strict=[]
    for profile in t.PROFILES:
        gates[profile]={};attribution[profile]={};fixed[profile]={};def_ok=True;full_ok=True
        for pol in results:
            gates[profile][pol]={};attribution[profile][pol]={};fixed[profile][pol]={}
            for q in ('early','recent'):
                gates[profile][pol][q]={};attribution[profile][pol][q]={};fixed[profile][pol][q]={}
                for suffix in ('corrected_touch/slip1','corrected_touch/slip2','next_minute_open/slip1','next_minute_open/slip2'):
                    case=profile+'/'+suffix;base='base/'+suffix;m=results[pol][q][case];b=results[pol][q][base];flags=dict(positive_cash=m['net']>0,positive_R=m['mean_r'] is not None and m['mean_r']>0,minimum_sample=m['n']>=max(35,.3*b['n']),unknown=m['unknown']<=b['unknown'],recent_retention=q!='recent' or m['net']>=.8*b['net']-1e-8,
                        early_cash=q!='early' or m['net']>=b['net']-1e-8,early_R=q!='early' or m['mean_r'] is not None and m['mean_r']>=b['mean_r']-1e-12,drawdown=m['realized_cash_drawdown']<=b['realized_cash_drawdown']+1e-8,
                        win_rate=q!='recent' or (m['win_rate'] or 0)>=(b['win_rate'] or 0)-1e-12,tail10=m['tail10_cash'] is not None and m['tail10_cash']>=b['tail10_cash']-1e-8)
                    gates[profile][pol][q][suffix]=flags;full_ok &= all(flags.values());def_ok &= flags['minimum_sample'] and flags['unknown'] and (flags['recent_retention'] if q=='recent' else flags['early_cash'] and flags['early_R'] and flags['drawdown'])
                    old=[z for z in accepted[(pol,base)] if z['period']==q];new=[z for z in accepted[(pol,case)] if z['period']==q];oc={z['event_key']:z for z in old if s.ev(z)['status']=='closed'};nc={z['event_key']:z for z in new if s.ev(z)['status']=='closed'};common=oc.keys()&nc.keys();oldonly=oc.keys()-nc.keys();newonly=nc.keys()-oc.keys()
                    dc=sum(s.ev(nc[k])['net_amount']-s.ev(oc[k])['net_amount'] for k in common);dn=sum(s.ev(nc[k])['net_amount'] for k in newonly);do=sum(s.ev(oc[k])['net_amount'] for k in oldonly);assert abs(m['net']-b['net']-(dc+dn-do))<1e-6
                    products=collections.defaultdict(float)
                    for sign,zz in ((1,new),(-1,old)):
                        for z in zz:
                            if s.ev(z)['status']=='closed':products[z['symbol']]+=sign*s.ev(z)['net_amount']
                    attribution[profile][pol][q][suffix]=dict(common_closed=len(common),common_cash_delta=dc,old_only_n=len(oldonly),old_only_net=do,new_only_n=len(newonly),new_only_net=dn,
                        paired_delta_in_original_actual_risk_units=statistics.mean((s.ev(nc[k])['net_amount']-s.ev(oc[k])['net_amount'])/(s.ev(oc[k])['risk']*oc[k]['multiplier']) for k in common) if common else None,
                        by_product_delta=dict(sorted(products.items(),key=lambda z:z[1],reverse=True)),warning_week_delta_interval=weekly_delta(old,new))
                    # Mechanism only, follow same old accepted CLOSED opportunities;
                    # never count an unknown/unfilled proposed result as zero cash.
                    pairs=[(a,cases[case][k]) for k,a in oc.items()];co=[(a,n) for a,n in pairs if s.ev(n)['status']=='closed'];trans=collections.Counter(s.ev(n)['status'] for a,n in pairs)
                    fixed[profile][pol][q][suffix]=dict(original_closed_cohort=len(pairs),proposed_status=dict(trans),common_closed=len(co),common_cash_delta=sum(s.ev(n)['net_amount']-s.ev(a)['net_amount'] for a,n in co),
                        changed_known_original_net=sum(s.ev(a)['net_amount'] for a,n in pairs if s.ev(n)['status']!='closed'),note='Fixed original accepted cohort only, ignores changed trigger-time competition; not deployable portfolio PnL. Nonclosed proposed outcomes have no cash imputation.')
        gates[profile]['defensive_noninferior']=bool(def_ok);gates[profile]['strict_stable']=bool(full_ok)
        if profile!='base' and def_ok:defensive.append(profile)
        if full_ok:strict.append(profile)
    t.verify_snapshot();assert all(w.sha(k)==h for k,h in p['production_before'].items());assert all(w.sha(RUN/'fork'/k)==v for k,v in w.read(RUN/'implementation-manifest.json').items())
    w.write(RUN/'results.json',results);w.write(RUN/'gates.json',gates);w.write(RUN/'attribution.json',attribution);w.write(RUN/'fixed-opportunity-diagnostic.json',fixed);w.write(RUN/'baseline-state-groups.json',{str(k):v for k,v in states.items()});w.write(RUN/'source-funnel.json',funnel)
    w.write(RUN/'leave-product-out.json',{str(k):v for k,v in leave.items()});w.write(RUN/'unknown-identities.json',{str(k):v for k,v in unknown.items()});w.write(RUN/'opportunity-ledgers.json',{str(k):v for k,v in ledgers.items()})
    with (RUN/'accepted.jsonl').open('x',encoding='utf-8') as f:
        for (pol,case),aa in accepted.items():
            for z in aa:f.write(json.dumps(dict(policy=pol,case=case,period=z['period'],row=z),ensure_ascii=False)+'\n')
    w.write(RUN/'verification.json',dict(qa=dict(qa),control_scenarios=qa_control,defensive_candidates=defensive,strict_stable=strict,production_unchanged=len(p['production_before']),best_a_snapshot_unchanged=True,production_changed=False,qualified_for_trading=False,results_sha256=w.sha(RUN/'results.json')))
    names={'base':'最佳a策略','all_touch':'全体未越过内部转折：触发','all_close':'全体未越过内部转折：收盘确认','fresh_close':'仅B新极值：内部转折收盘确认','inside_close':'仅B内部：内部转折收盘确认'}
    lines=['# B内反弹何时转为第二波推进：同一父波段下的结构证据','', '基准固定为用户命名的最佳a策略best-a-v1。本轮只改变进场证据，不重选A，不将局部推动失败传递给父A，原止损和绝对目标均固定。',
        '', '证据：同一B内最近严格5分钟三柱高/低转折，右侧确认必须早于当前15分钟预警柱开始；不跨缺失或会话间隔确认。已经越过或没有证据的机会沿用最佳a。未越过时，试验更高/更低触发、1分钟收盘后下一分钟开盘，以及只对B新极值/只对B内部的两种互补分层。共4个新规则，未做阈值网格。',
        '', '确认价提高会扩大入场到原止损的实际风险，压缩到原目标的空间。重新检查最低10tick、摩擦<=0.2R与扣费净空间>=1R；不够则不做，不改取更远目标。完整重算触发时刻机会竞争，不能用固定交易删除代替。',
        '', '|规则|上半年笔数|净额|平均净R|原名义风险口径R|近期净额|近期保留|近期胜率|','|---|---:|---:|---:|---:|---:|---:|---:|']
    for profile in t.PROFILES:
        e=results['original']['early'][profile+'/corrected_touch/slip1'];n=results['original']['recent'][profile+'/corrected_touch/slip1'];b=results['original']['recent']['base/corrected_touch/slip1'];lines.append(f"|{names[profile]}|{e['n']}|{e['net']:.2f}|{e['mean_r']:.4f}|{e['mean_original_nominal_r']:.4f}|{n['net']:.2f}|{n['net']/b['net']:.1%}|{n['win_rate']:.1%}|")
    lines+=['', '## 四种成本/延迟场景','', '|规则|场景|上半年净额|平均净R|近期净额|近期保留|','|---|---|---:|---:|---:|---:|']
    for profile in t.PROFILES:
        for suffix in ('corrected_touch/slip1','corrected_touch/slip2','next_minute_open/slip1','next_minute_open/slip2'):
            e=results['original']['early'][profile+'/'+suffix];n=results['original']['recent'][profile+'/'+suffix];b=results['original']['recent']['base/'+suffix];lines.append(f"|{profile}|{suffix}|{e['net']:.2f}|{e['mean_r']:.4f}|{n['net']:.2f}|{n['net']/b['net']:.1%}|")
    lines+=['', '两种未知占用口径及四场景均满足近期80%、上半年现金/R/回撤不劣、未知/样本门槛的新候选：'+str(defensive),'全部严格稳定门槛通过：'+str(strict),
        '', '## 原最佳a的警报状态分组（描述，非新增筛选规则）','', '|时期|状态|已平仓笔数|净额|平均净R|胜率|','|---|---|---:|---:|---:|---:|']
    for q in ('early','recent'):
        groups=states[('original',q,'corrected_touch/slip1')];assert sum(m['n'] for m in groups.values())==results['original'][q]['base/corrected_touch/slip1']['n']
        for state,m in groups.items():lines.append(f"|{q}|{state}|{m['n']}|{m['net']:.2f}|{m['mean_r']:.4f}|{m['win_rate']:.1%}|")
    lines+=['', '## 核验与局限','', f"最佳a的24个时期/压力/占用控制复现一致，{qa['inactive_routes_full_result_exact']}条不受新规则影响的完整成交结果逐条相同。父A/B、原止损、绝对目标和所用预警池全部核对；特征前缀、多空镜像、会话断档、确认后下一分钟成交测试均完成。",
        '', '这些是当时可见的操作性证据，不是事后标注的真实C段；不以最后赚钱来倒填B终点。固定原交易机制诊断另存，不作为组合收益。现金/R、改变风险分母、机会被筛除和新机会竞争的影响分别报告。',
        '', '每手已平仓现金，未知收益单列，回撤为已实现现金回撤。两种占用口径分别保留到合约末，或仅在两条OHLC路径皆已空仓时释放。next_minute_open是额外一分钟延迟，不是真实下一报价。',
        '', '近期各品种起点不齐；所有历史已反复研究，本轮没有新独立样本、实盘接入或自动晋级。最佳a快照与生产169个文件哈希均未改变。']
    (RUN/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8');print('\n'.join(lines[6:13]),flush=True);print('Defensive:',defensive,'Strict:',strict,flush=True)

if __name__=='__main__':run()
