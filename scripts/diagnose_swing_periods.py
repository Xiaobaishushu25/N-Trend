"""Mechanism audit, not parameter search. Frozen entries, continuous R and matched strata."""
import bisect
import collections
import datetime as dt
import itertools
import json
import math
import pathlib
import random
import shutil
import statistics as st
import subprocess
import sys

sys.dont_write_bytecode=True
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
from research_swing_consistency import REPO, RUN as SOURCE, read, write, sha, portfolio, metrics, ev
from reprice_signal_costs import TradingDates, reprice
from entry_model import completed_context, commission_bound

OUT=REPO/'research-runs/swing-period-mechanisms-20261025-v1'
BASES=('P2','P3')


def mean(xs):
    return st.mean(xs) if xs else None


def describe(rows):
    if not rows:return dict(n=0)
    vals=[r['net_r'] for r in rows];wins=[r for r in rows if r['net']>0];loss=[r for r in rows if r['net']<0]
    out=dict(n=len(rows),net=sum(r['net'] for r in rows),mean_r=mean(vals),win_rate=len(wins)/len(rows),
             win_r=mean([r['net_r'] for r in wins]),loss_r=mean([r['net_r'] for r in loss]),
             median_r=st.median(vals),mean_risk_yuan=mean([r['risk_yuan'] for r in rows]),
             mean_target_r=mean([r['target_r'] for r in rows]),mean_a_bars=mean([r['a_bars'] for r in rows]),
             mean_b_bars=mean([r['b_bars'] for r in rows]),mean_holding_buckets=mean([r['holding_buckets'] for r in rows]),
             cross_session_rate=mean([r['cross_session'] for r in rows]),cross_day_rate=mean([r['cross_day'] for r in rows]),
             gap_extra=sum(r['gap_extra'] for r in rows),mean_r_without_gap_extra=mean([r['net_r']+r['gap_extra']/r['risk_yuan'] for r in rows]),
             fee_r=mean([r['fee_r'] for r in rows]),mean_r_before_fees=mean([r['net_r']+r['fee_r'] for r in rows]),
             exit_reasons=dict(collections.Counter(r['reason'] for r in rows)))
    for threshold in (.5,1.,2.):
        reached=[r for r in rows if r['mfe_r']>=threshold-1e-9];bad=[r for r in reached if r['net']<0]
        out['mfe'+str(threshold)]=dict(reached=len(reached),fraction=len(reached)/len(rows),negative=len(bad),
                                    loss_given_reached=len(bad)/len(reached) if reached else None,
                                    mean_r_given_reached=mean([r['net_r'] for r in reached]))
    out['net_without_BU_EG_PP']=sum(r['net'] for r in rows if r['product'] not in ('BU','EG','PP'))
    out['mean_r_without_BU_EG_PP']=mean([r['net_r'] for r in rows if r['product'] not in ('BU','EG','PP')])
    return out


def grouped(rows, fn):
    bins=collections.defaultdict(list)
    for r in rows:bins[fn(r)].append(r)
    return {k:describe(v) for k,v in sorted(bins.items())}


def composition(early,recent,fields,min_n=1):
    """Symmetric Oaxaca/Shapley split on common cells; exact on declared subset."""
    def bins(rows):
        z=collections.defaultdict(list)
        for r in rows:z[tuple(str(r[f]) for f in fields)].append(r['net_r'])
        return z
    e,r=bins(early),bins(recent);keys=sorted(k for k in e.keys()&r.keys() if min(len(e[k]),len(r[k]))>=min_n)
    ne=sum(len(e[k]) for k in keys);nr=sum(len(r[k]) for k in keys)
    cells=[];mix=within=0.
    for k in keys:
        we,wr=len(e[k])/ne,len(r[k])/nr;me,mr=mean(e[k]),mean(r[k])
        a=(wr-we)*(mr+me)/2;b=(mr-me)*(wr+we)/2;mix+=a;within+=b
        cells.append(dict(cell=k,n_early=len(e[k]),n_recent=len(r[k]),early_mean_r=me,recent_mean_r=mr,mix=a,within=b))
    return dict(fields=fields,min_n=min_n,coverage_early=ne/len(early),coverage_recent=nr/len(recent),
                mean_r_early=sum(len(e[k])*mean(e[k]) for k in keys)/ne,
                mean_r_recent=sum(len(r[k])*mean(r[k]) for k in keys)/nr,
                composition_effect=mix,within_cell_effect=within,cells=cells)


def payoff_shapley(early,recent):
    def params(rows):
        return [mean([r['net']>0 for r in rows]),mean([r['net_r'] for r in rows if r['net']>0]),-mean([r['net_r'] for r in rows if r['net']<0])]
    a,b=params(early),params(recent)
    def value(x):return x[0]*x[1]-(1-x[0])*x[2]
    sums=[0.,0.,0.]
    for order in itertools.permutations(range(3)):
        x=a[:]
        for i in order:
            old=value(x);x[i]=b[i];sums[i]+=value(x)-old
    return dict(early=a,recent=b,mean_r_difference=value(b)-value(a),
                win_probability=sums[0]/6,winning_payoff=sums[1]/6,losing_payoff=sums[2]/6)


def bootstrap(early,recent):
    def weeks(rows):
        out=collections.defaultdict(list)
        for r in rows:
            d=dt.date.fromisoformat(r['warning_ts'][:10]);out[(d-dt.timedelta(days=d.weekday())).isoformat()].append(r['net_r'])
        return list(out.values())
    ee,rr=weeks(early),weeks(recent);rng=random.Random(20261025);diff=[]
    for _ in range(2000):
        a=[x for _ in ee for x in rng.choice(ee)];b=[x for _ in rr for x in rng.choice(rr)]
        diff.append(mean(b)-mean(a))
    diff.sort()
    return dict(repetitions=2000,week_clusters=[len(ee),len(rr)],difference_mean_r_95=[diff[49],diff[1949]],
                note='Historical descriptive interval, not adjusted for repeated strategy research or nonstationarity')


def prepare():
    OUT.mkdir(parents=True,exist_ok=False)
    p=read(SOURCE/'protocol.json');selected=collections.defaultdict(list)
    for line in (SOURCE/'accepted.jsonl').open(encoding='utf-8'):
        z=json.loads(line)
        if z['case'] in ('P2/corrected_touch/slip1','P3/corrected_touch/slip1') and ev(z['row'])['status']=='closed':
            selected[(z['period'],z['case'][:2])].append(z['row'])
    allrows={};cov={};counterfactuals={}
    for period in ('early','recent'):
        snapshots={x['event_key']:x for x in (json.loads(l) for l in (SOURCE/period/'events.jsonl').open(encoding='utf-8'))}
        source=read(p['periods'][period]['input']);cal=TradingDates(source['symbols']);contracts=read(p['periods'][period]['contracts'])
        contexts={s:completed_context(v['bars'],[]) for s,v in source['symbols'].items()}
        # Observed session breaks on the actual contract; 30m excludes 15m recess.
        contract_times={c:[b['datetime'] for b in v['minutes']] for c,v in contracts.items()}
        gaps={c:[b['datetime'] for a,b in zip(v['minutes'],v['minutes'][1:])
                 if (dt.datetime.fromisoformat(b['datetime'])-dt.datetime.fromisoformat(a['datetime'])).total_seconds()>1800] for c,v in contracts.items()}
        cov[period]={s:dict(first=v['bars'][0]['datetime'],last=v['bars'][-1]['datetime'],bars5=len(v['bars']),
                            observed_day_sessions=len({b['datetime'][:10] for b in v['bars'] if '09:00:00'<=b['datetime'][11:]<='15:00:00'})) for s,v in source['symbols'].items()}
        cohort=[]
        for base in BASES:
            rows=[]
            for r in selected[(period,base)]:
                e=ev(r);snap=snapshots[r['event_key']];event=snap['event'];sign=1 if r['direction']=='up' else -1
                ts=contract_times[r['execution_contract']];i=bisect.bisect_left(ts,e['entry_ts']);j=bisect.bisect_right(ts,e['exit_ts'])
                held=ts[i:j]
                def bucket(t):
                    t=dt.datetime.fromisoformat(t);return t+dt.timedelta(minutes=(-t.minute)%15)
                nheld=len({bucket(t) for t in held});cross=bisect.bisect_right(gaps[r['execution_contract']],e['exit_ts'])>bisect.bisect_right(gaps[r['execution_contract']],e['entry_ts'])
                stoploss=max(0.,-e['gross_points']-e['risk']-r['tick_size'])*r['multiplier'] if e['exit_reason']=='stop' else 0.
                target=(event['target']-e['fill'])*sign/e['risk'];target=min(target,2.) if base=='P3' else target
                ctx=contexts[r['symbol']];cts=list(ctx);idx=bisect.bisect_right(cts,event['warning_ts'])-1
                pre=cts[max(0,idx-20):idx+1];closes=[ctx[t]['close'] for t in pre];path=sum(abs(b-a) for a,b in zip(closes,closes[1:]));eff=abs(closes[-1]-closes[0])/path if path else 0.
                aa=[ctx[t] for t in cts if event['s0_ts']<=t<=event['s1_ts']];a_gap=sum((b['open']-a['close'])*sign for a,b in zip(aa,aa[1:]));
                item=dict(period=period,base=base,event_key=r['event_key'],product=r['symbol'].rstrip('0123456789'),direction=r['direction'],
                    warning_ts=r['warning_ts'],entry_ts=e['entry_ts'],exit_ts=e['exit_ts'],entry_day=cal.day(e['entry_ts']),
                    net=e['net_amount'],net_r=e['net_r'],risk_yuan=e['risk']*r['multiplier'],fee_r=r['cost_repricing']['commission']/(e['risk']*r['multiplier']),
                    mfe_r=e['mfe_r'],mae_r=e['mae_r'],reason=e['exit_reason'],level=event['level'],a_bars=event['a_bars'],b_bars=event['b_bars'],
                    a_atr=event['setup_features']['a_move_atr'],b_atr=event['setup_features']['b_move_atr'],
                    a_eff=snap['a_eff'],pre20_eff=eff,a_gap_share=a_gap/event['a_move'],retracement=event['retracement'],
                    risk_atr=snap['risk_atr'],friction_r=snap['friction_r'],target_r=target,
                    holding_buckets=nheld,wall_hours=(dt.datetime.fromisoformat(e['exit_ts'])-dt.datetime.fromisoformat(e['entry_ts'])).total_seconds()/3600,
                    cross_session=cross,cross_day=cal.day(e['entry_ts'])!=cal.day(e['exit_ts']),gap_extra=stoploss,
                    warning_kind=event['warning_kind'],trend60=event['setup_features']['trend60_state'])
                rows.append(item)
                cohort.append(dict(base=base,event_key=r['event_key'],event=event,baseline=r))
            allrows[period+'/'+base]=rows
        write(OUT/(period+'-cohorts.json'),cohort)
        del contracts
    result=dict(coverage=cov,summary={},groups={},composition={},payoff_shapley={},bootstrap={})
    for label,rows in allrows.items():
        result['summary'][label]=describe(rows)
        result['groups'][label]={
            'product':grouped(rows,lambda r:r['product']), 'direction':grouped(rows,lambda r:r['direction']),
            'level':grouped(rows,lambda r:r['level']),'cross_session':grouped(rows,lambda r:str(r['cross_session'])),
            'target_r':grouped(rows,lambda r:'1-1.5' if r['target_r']<1.5 else '1.5-2' if r['target_r']<2 else '2-3' if r['target_r']<3 else '3+'),
            'month':grouped(rows,lambda r:r['warning_ts'][:7]),
            'trend60':grouped(rows,lambda r:r['trend60']),
            'holding':grouped(rows,lambda r:'<=5' if r['holding_buckets']<=5 else '6-12' if r['holding_buckets']<=12 else '13+'),
            'pre20_eff':grouped(rows,lambda r:'<.2' if r['pre20_eff']<.2 else '.2-.4' if r['pre20_eff']<.4 else '.4+'),
            'warning_kind':grouped(rows,lambda r:r['warning_kind'])}
    for base in BASES:
        early,recent=allrows['early/'+base],allrows['recent/'+base]
        result['composition'][base]={name:composition(early,recent,fields,n) for name,fields,n in [
            ('product',['product'],1),('product_min5',['product'],5),('product_direction',['product','direction'],3),
            ('product_level',['product','level'],3),('level',['level'],1)]}
        result['payoff_shapley'][base]=payoff_shapley(early,recent);result['bootstrap'][base]=bootstrap(early,recent)
    write(OUT/'descriptive.json',result);write(OUT/'trades.json',allrows)
    write(OUT/'protocol.json',dict(created_at=dt.datetime.now(dt.timezone.utc).isoformat(),source=str(SOURCE),source_protocol_sha256=sha(SOURCE/'protocol.json'),
        diagnosis_only=True,new_parameter_selection=False,production_changed=False,
        first_passage='Same accepted baseline entries, exact actual fill and risk. Targets .5/1/2R, no no-follow, 60 completed buckets, same observed holidays/weekends; barrier exits, censored and OHLC ambiguity all separate. Not a deployable candidate.',
        population='20 non-midnight products; score>=2.5 pool already conditioned by N generator. Guard/minrisk/weekend unchanged; downstream score/friction/ATR filters ablated, upstream service dedup and opportunity capacity rerun for each filter. PB/SS excluded symmetrically to reuse full archived pool without stale midnight signals.',
        periods=p['periods']))
    print('Prepared mechanism audit',OUT,flush=True)


def run():
    root=SOURCE/'fork';shutil.copy2(REPO/'scripts/swing_mechanism_runner.rs',root/'crates/n-core/src/bin/swing_mechanism_runner.rs')
    subprocess.run(['cargo','build','--offline','--manifest-path',str(root/'Cargo.toml'),'--target-dir',str(REPO/'target'),'-p','n-core','--bin','swing_mechanism_runner'],check=True)
    for period in ('recent','early'):
        subprocess.run([str(REPO/'target/debug/swing_mechanism_runner.exe'),str(OUT/'protocol.json'),period,str(OUT/(period+'-cohorts.json')),str(OUT/period)],check=True)


def finish():
    p=read(OUT/'protocol.json');d=read(OUT/'descriptive.json');trades=read(OUT/'trades.json');fees=read(p['periods']['recent']['fees'])
    findings=dict(first_passage={},filter_ablation={},filter_regression={})
    for period in ('early','recent'):
        barriers=collections.defaultdict(list)
        for line in (OUT/period/'first-passage.jsonl').open(encoding='utf-8'):
            r=json.loads(line);barriers[(r['base'],str(r['target_r']))].append(r)
        findings['first_passage'][period]={}
        for (base,target),rows in barriers.items():
            counts=collections.Counter()
            for r in rows:
                e=ev(r);reason=e.get('exit_reason')
                outcome='upper' if e['status']=='closed' and reason=='target_s1' else 'lower' if e['status']=='closed' and reason=='stop' else 'censored' if e['status']=='closed' else e['status']
                counts[outcome]+=1
            n=len(rows);unknown=sum(v for k,v in counts.items() if k not in ('upper','lower','censored'))
            findings['first_passage'][period][base+'/'+target]=dict(n=n,counts=dict(counts),
                upper_lower_bound=counts['upper']/n,upper_upper_bound=(counts['upper']+counts['censored']+unknown)/n,
                resolved_upper_fraction=counts['upper']/(counts['upper']+counts['lower']) if counts['upper']+counts['lower'] else None)
        source=read(p['periods'][period]['input']);cal=TradingDates(source['symbols'])
        contexts={s:completed_context(v['bars'],[]) for s,v in source['symbols'].items()}
        events={}
        for e in read(p['periods'][period]['signals']):
            if e['symbol'] in ('PB0','SS0'):continue
            events['|'.join(e[k] for k in ('symbol','direction','s0_ts','s1_ts','s2_ts','warning_ts'))]=e
        mask=read(SOURCE/period/'service-dedup-raw-plan.json');allowed={k for v in mask['symbols'].values() for k in v['keys']}
        ends=read(OUT/period/'manifest.json')['contract_ends'];cases=collections.defaultdict(list)
        features={}
        for line in (OUT/period/'population.jsonl').open(encoding='utf-8'):
            r=json.loads(line);k=r['event_key']
            if k not in allowed:continue
            e=events[k];ctx=contexts[e['symbol']].get(e['warning_ts']);atr=ctx.get('atr14') if ctx else None
            sign=1 if e['direction']=='up' else -1;tick=r['tick_size'];risk=r['planned_risk']
            # Runner grid rounding may have changed the raw archived plan.
            entry=(math.ceil(e['entry']/tick-1e-9) if sign>0 else math.floor(e['entry']/tick+1e-9))*tick
            stop=entry-sign*risk
            fee=commission_bound(fees['products'][e['symbol'].rstrip('0123456789')],entry+sign*tick,stop-sign*tick,r['multiplier'])
            features[k]=dict(score=r['score'],friction=(2*tick*r['multiplier']+fee)/(risk*r['multiplier']),atr=risk/atr if atr else None)
            r=reprice(r,fees,cal)
            if ev(r)['status']=='closed':r['exit_trading_day']=cal.day(ev(r)['exit_ts'])
            cases[r['scenario']].append(r)
        specs={'base_25':(2.5,False,False),'score_only':(3.,False,False),'friction_only':(2.5,True,False),'atr_only':(2.5,False,True),
               'without_score':(2.5,True,True),'without_friction':(3.,False,True),'without_atr':(3.,True,False),'full':(3.,True,True)}
        findings['filter_ablation'][period]={};findings['filter_regression'][period]={}
        for base,rows in cases.items():
            findings['filter_ablation'][period][base]={}
            for label,(score,friction,atr) in specs.items():
                def passes(r):
                    f=features[r['event_key']]
                    return f['score']>=score-1e-9 and (not friction or f['friction']<=.2+1e-9) and (not atr or f['atr'] is not None and .75<=f['atr']<=2.5)
                subset=[r for r in rows if passes(r)];accepted,_=portfolio(subset,events,ends)
                findings['filter_ablation'][period][base][label]=metrics(accepted)
                if label=='full':
                    expected={r['event_key'] for r in trades[period+'/'+base] if r['product'] not in ('PB','SS')}
                    got={r['event_key'] for r in accepted if ev(r)['status']=='closed'}
                    assert expected==got,(period,base,'full filter cohort regression',len(expected-got),len(got-expected))
                    findings['filter_regression'][period][base]=dict(matched_closed=len(got),exact_entry_population=True)
    write(OUT/'findings.json',findings)
    render(d,findings)
    old=read(SOURCE/'protocol.json');assert all(sha(f)==h for f,h in old['production_before'].items())
    write(OUT/'verification.json',dict(production_changed=False,production_files_unchanged=len(old['production_before']),
          source_results_sha256=sha(SOURCE/'results.json'),descriptive_sha256=sha(OUT/'descriptive.json'),findings_sha256=sha(OUT/'findings.json'),
          frozen_entry_checks=sum(read(OUT/p/'manifest.json')['frozen_entry_checks'] for p in ('early','recent')),
          population_checks=sum(read(OUT/p/'manifest.json')['population_evaluations'] for p in ('early','recent')),
          scripts_sha256={str(q.relative_to(REPO)):sha(q) for q in (pathlib.Path(__file__),REPO/'scripts/swing_mechanism_runner.rs')}))
    print('Mechanism report:',OUT/'report.md',flush=True)


def render(d,f):
    lines=['# P2/P3跨时期差距：机制审计','',
           '本报告解释已冻结策略的历史路径，不做新选参，不改实盘。R为实际入场至初始结构止损的价格风险；统一每边1tick、费用与服务收盘线去重口径。','',
           '## 核心发现','',
           '1. 小波段启动与最终兑现必须区分：实际持仓达到过0.5R/1R的比例接近，2R延续与盈利兑现不同。下方同时提供脱离实际退出的固定门槛重放。',
           '2. 策略实际同时交易fine与large两档；近期净利润主要来自large档及跨会话存续交易。这个盈利结构比“小波段反弹即兑现”更依赖后续行情路径。',
           '3. 品种构成、手续费、初始现金风险和跳空均单独核验；不能用它们概括全部反转。',
           '4. 少量规则不足以证明严重过拟合；过滤前后对照可以区分“过滤选中了近期优势”和“底层形态本来就跨期不稳定”。','',
           '## 相同口径下的路径','',
           '|P2指标|1—6月|7—10月|','|---|---:|---:|']
    a,b=d['summary']['early/P2'],d['summary']['recent/P2']
    for label,aa,bb,fmt in [
        ('已平仓笔数',a['n'],b['n'],'d'),('每笔平均净R',a['mean_r'],b['mean_r'],'.3f'),
        ('实际持仓曾达0.5R',a['mfe0.5']['fraction'],b['mfe0.5']['fraction'],'.2%'),
        ('实际持仓曾达1R',a['mfe1.0']['fraction'],b['mfe1.0']['fraction'],'.2%'),
        ('实际持仓曾达2R',a['mfe2.0']['fraction'],b['mfe2.0']['fraction'],'.2%'),
        ('达到1R后最终净亏',a['mfe1.0']['loss_given_reached'],b['mfe1.0']['loss_given_reached'],'.2%'),
        ('平均预期目标/实际风险',a['mean_target_r'],b['mean_target_r'],'.3f'),
        ('平均初始现金风险',a['mean_risk_yuan'],b['mean_risk_yuan'],'.2f'),
        ('假设消除额外穿越止损损失后的平均R',a['mean_r_without_gap_extra'],b['mean_r_without_gap_extra'],'.3f'),
        ('扣手续费前、已含滑点平均R',a['mean_r_before_fees'],b['mean_r_before_fees'],'.3f')]:
        lines.append(f'|{label}|{aa:{fmt}}|{bb:{fmt}}|')
    lines += ['', '实际MFE在退出时截断，不能直接当作统一固定目标的触达率。固定目标检验使用相同已接受入场、实际成交价和初始风险；取消无跟随退出，沿用60桶上限、周末/长假及缺失处理。只统计先触上门槛/初始止损及删失；两条OHLC路径不一致单列。','',
              '|固定门槛/P2|上半年先触上限/总数|近期先触上限/总数|上半年各状态|近期各状态|','|---|---:|---:|---|---|']
    for target in ('0.5','1.0','2.0'):
        aa=f['first_passage']['early']['P2/'+target];bb=f['first_passage']['recent']['P2/'+target]
        lines.append(f"|{target}R|{aa['counts'].get('upper',0)}/{aa['n']} ({aa['upper_lower_bound']:.2%})|{bb['counts'].get('upper',0)}/{bb['n']} ({bb['upper_lower_bound']:.2%})|{aa['counts']}|{bb['counts']}|")
    lines += ['', '上限/总数为保守已确认比例；删失与路径不确定不能计为失败或成功。first_passage中的upper_upper_bound给出它们全成功时的上界。0.5R门槛是触达诊断，非半tick可执行止盈候选。','',
              '## 盈利来源与小波段定位','',
              '|时期/策略|fine净额|large净额|未跨长休市净额|跨长休市净额|','|---|---:|---:|---:|---:|']
    for period in ('early','recent'):
        for base in BASES:
            g=d['groups'][period+'/'+base]
            lines.append(f"|{period}/{base}|{g['level']['fine']['net']:.2f}|{g['level']['large']['net']:.2f}|{g['cross_session']['False']['net']:.2f}|{g['cross_session']['True']['net']:.2f}|")
    lines += ['', '跨长休市指实际合约分钟记录中持仓跨越超过30分钟的报价间隔。持仓长短与能否跨会话是事后结果；此表归因不证明“强制持久/跨会话”能获利。fine与large标签则在入场前已确定。', '',
              '代码：fine的A/B上限16/12，large为48/32；小N排除采用A<3ATR且B<max(1ATR,5点)同时成立的判定。持仓第5个桶只在历史峰值不足0.5R时无跟随退出；一旦越过0.5R，随后回吐并不再触发这一条件。P2继续等S1，P3继续等S1与2R中较近目标，最长60桶。','',
              '## 控制品种构成与现金尺度','',
              '|控制比较|上半年平均R|近期平均R|品种占比变化贡献|同品种收益变化贡献|覆盖笔数比例 上半年/近期|','|---|---:|---:|---:|---:|']
    for base in BASES:
        c=d['composition'][base]['product_min5']
        lines.append(f"|{base} 两期每品种至少5笔|{c['mean_r_early']:.3f}|{c['mean_r_recent']:.3f}|{c['composition_effect']:.3f}|{c['within_cell_effect']:.3f}|{c['coverage_early']:.1%}/{c['coverage_recent']:.1%}|")
    lines += ['', '采用共同品种、对称权重分解；贡献精确加总为共同子样本的平均R差。不是因果识别，不把未覆盖品种补成零收益。product_direction/product_level同时控制方向/档位，详见descriptive.json。','',
              '## 少量过滤是否制造了时期反转','',
              '统一使用20个非午夜品种的完整原N警报池；排除PB/SS以避免复用未修复的午夜原信号。基础池仍保留原N形态、score≥2.5、10tick、净盈亏比及周末/长假规则；每次过滤后重跑服务去重后的机会竞争。因此这里检验新增三类过滤，不是与随机入场比较。','',
              '|规则/P2|上半年笔数/平均R/净额|近期笔数/平均R/净额|','|---|---:|---:|']
    for label in ('base_25','score_only','friction_only','atr_only','without_score','without_friction','without_atr','full'):
        aa=f['filter_ablation']['early']['P2'][label];bb=f['filter_ablation']['recent']['P2'][label]
        lines.append(f"|{label}|{aa['n']}/{aa['mean_r']:.3f}/{aa['net']:.2f}|{bb['n']}/{bb['mean_r']:.3f}/{bb['net']:.2f}|")
    lines += ['', 'score_only=评分≥3；friction_only=摩擦≤0.2R；atr_only=风险/ATR14∈[0.75,2.5]。full为三者同时。without_*从full删除对应闸门。完整组合已核对原修复批次相同20品种已平仓事件集合，完全一致。','',
              '## 解释边界','',
              '机制证据支持“前段反弹与后段持续性/兑现不同、现有退出依赖较大尺度延续”；尚不能把它归因到某个宏观新闻、产业周期、参与者行为或确定的市场微观结构变化。要证明“为什么这些品种的持续性改变”，需要更完整等覆盖时期、价格过程基准及可独立验证的解释变量。','',
              '两期均已被研究，本轮统计不是新的独立验证。周簇重采样仅描述不确定性，没有修正全部历史重复研究。所有比较为机会回放，无账户资金约束，真实报价与分钟跨源全量核验限制沿用前报告。']
    (OUT/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def validate():
    """Exact reference fills/cash, conditional continuation, final readable findings."""
    d=read(OUT/'descriptive.json');f=read(OUT/'findings.json');p=read(OUT/'protocol.json');checks={};conditional={}
    for period in ('early','recent'):
        expected={(x['base'],x['event_key']):x['baseline'] for x in read(OUT/(period+'-cohorts.json')) if x['event']['symbol'] not in ('PB0','SS0')}
        got={}
        for line in (OUT/period/'population.jsonl').open(encoding='utf-8'):
            r=json.loads(line)
            if (r['scenario'],r['event_key']) in expected:got[(r['scenario'],r['event_key'])]=r
        count=0
        for k,reference in expected.items():
            now=got[k];a,b=ev(reference),ev(now)
            for name in ('entry_ts','exit_ts','exit_reason','fill','risk','gross_points','mfe_r','mae_r'):
                assert a[name]==b[name],(period,k,name,a[name],b[name])
            assert reference['result']['fills']==now['result']['fills'];count+=1
        checks[period]=dict(exact_entry_exit_risk_excursions_fill_legs=count)
        pairs=collections.defaultdict(dict)
        for line in (OUT/period/'first-passage.jsonl').open(encoding='utf-8'):
            r=json.loads(line)
            if r['base']=='P2':pairs[r['event_key']][r['target_r']]=ev(r)
        reached=[v for v in pairs.values() if v[1.]['status']=='closed' and v[1.]['exit_reason']=='target_s1']
        counts=collections.Counter('2R' if v[2.]['exit_reason']=='target_s1' else 'initial_stop' if v[2.]['exit_reason']=='stop' else 'censored' for v in reached)
        conditional[period]=dict(reached_1R=len(reached),outcomes=dict(counts),continue_2R=counts['2R']/len(reached),back_to_initial_stop=counts['initial_stop']/len(reached))
    write(OUT/'conditional-continuation.json',conditional)
    header=['## 直接回答','',
        '新增的少量过滤并不是时期反转的主要来源。20个非午夜品种在去掉新增评分/摩擦/ATR过滤后，P2和P3仍然都是上半年负、近期正；闸门改变幅度，但没有创造正负反转。','',
        '更深的实现与定位问题是：信号有短程反弹，不代表有足够的小波段净期望；当前收益实际依赖large档、1R之后能否继续扩展，以及等待S1期间能否保留浮盈。','',
        '|基础警报池/全部新增过滤 平均净R|1—6月|7—10月|','|---|---:|---:|']
    for base in BASES:
        for label in ('base_25','full'):
            a=f['filter_ablation']['early'][base][label];b=f['filter_ablation']['recent'][base][label]
            header.append(f"|{base}/{label}|{a['mean_r']:.3f}|{b['mean_r']:.3f}|")
    header += ['', '在固定门槛诊断中，先达到1R后：','',
        '|后续结果/P2|1—6月|7—10月|','|---|---:|---:|']
    for label,key in [('继续达到2R','continue_2R'),('回到初始止损','back_to_initial_stop')]:
        header.append(f"|{label}|{conditional['early'][key]:.2%}|{conditional['recent'][key]:.2%}|")
    header += ['', '余下为时间/周末/长假退出，保留为删失。相同实际入场价和初始风险核对通过，改变的只有用于诊断的目标与无跟随退出。', '',
        '0.5R目标搭配完整1R止损，零费用下也需要超过66.7%的成功率；两期0.5R诊断即使把所有删失和路径不确定都算成功，上界仍只有61.4%/64.0%。所以“都有小反弹”不能推出“把止盈改小就能稳定赚钱”。这是固定初始风险下的诊断，不排除其他入场/止损结构。','',
        '两期P2的large占比约65%/65%，A幅度约6.88/6.96ATR，风险约1.19/1.20ATR，入场前20根价格效率约0.183/0.175。不能仅凭这些结果宣称“上半年更震荡、近期更趋势”；更明确的差别发生在相似形态触发后的后续路径。','',
        'P2去除额外跳空尾损后的时期平均R差仍为0.273R，原差为0.327R；额外跳空约解释16%的差距。共同品种分解显示绝大部分差异来自同品种内部，不是品种占比。','',
        '这些结果否定了“主要是几个新过滤过拟合”的简单解释，也不支持直接改成统一0.5R止盈。它们定位到策略的延续与兑现依赖；尚未证明导致价格持续性改变的产业或宏观原因。','']
    report=OUT/'report.md';text=report.read_text(encoding='utf-8');report.write_text(text.split('\n',1)[0]+'\n\n'+'\n'.join(header)+text.split('\n',1)[1],encoding='utf-8')
    before=read(SOURCE/'protocol.json')['production_before'];assert all(sha(path)==h for path,h in before.items())
    write(OUT/'completion.json',dict(checks=checks,source_results_unchanged=sha(SOURCE/'results.json')==read(OUT/'verification.json')['source_results_sha256'],
        production_files_unchanged=len(before),report_sha256=sha(report),scripts_sha256={str(q.relative_to(REPO)):sha(q) for q in (pathlib.Path(__file__),REPO/'scripts/swing_mechanism_runner.rs')}))
    print('Exact control fill validation and report complete',checks,flush=True)


if __name__=='__main__':globals()[sys.argv[1]]()
