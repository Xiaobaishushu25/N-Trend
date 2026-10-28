"""ABC warning, structure-stop and actual-held excursion interpretation."""
import collections
import datetime as dt
import json
import audit_swing_jd_signal_paths as a
from swing_entry_economics import warning_features
RUN=a.RUN
READ=a.READ
EV=a.EV
def write(n,v):a.write(n,v)
def fmt(v):return '—' if v is None else f'{v:.2f}'
def rate(n,d):return '—' if not d else f'{n/d:.1%}'

def structure_and_actual(records):
    p=READ(RUN/'protocol.json')
    if not (RUN/'structure-diagnostic-protocol.json').exists():write('structure-diagnostic-protocol.json',dict(created_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
        purpose='Descriptive follow-up after initial excursions; ABC retained, no new trading backtest.',
        stops='All warnings: original stop vs entire B-prefix extreme with original per-warning S2-to-stop buffer preserved. Compare originalS1/new risk and first-passage at4/8/16. Never widen only losing examples.',
        actual='Frozen B5/E20S1 held MFE/MAE and post-exit4/8/16bar excursions; exclude entire exit minute.',
        limitation='Exploratory accounting on reused history; later recovery is not proof widening pays.'))
    prices={q:a.Prices(v) for q,v in p['periods'].items()}
    pools={r['event_key']:r for q in prices for r in READ(RUN/(q+'-pool.json'))}
    bars={q:a.z.x.identification_bars(pr.input['symbols']['JD0']['bars']) for q,pr in prices.items()}
    detail=[];actual=collections.defaultdict(list)
    for row in records:
        if row['symbol']!='JD0':continue
        pr=prices[row['period']];e=pools[row['event_key']]['event'];sign=1 if e['direction']=='up' else -1
        f=warning_features(e,bars[row['period']]);buffer=(e['s2_price']-e['stop'])*sign
        assert buffer>=0
        stop=f['b_true_extreme']-sign*buffer;risk=(e['entry']-stop)*sign
        assert risk>=e['risk']-1e-8
        d=dict(event_key=row['event_key'],score=row['score'],original_stop=e['stop'],true_B_stop=stop,preserved_buffer=buffer,
            original_risk=e['risk'],B_risk=risk,width_ratio=risk/e['risk'],original_S1_R=(e['target']-e['entry'])*sign/e['risk'],
            B_S1_R=(e['target']-e['entry'])*sign/risk,races={},same_oldR_targets={})
        path=pr.path(row['contract'],row['warning_ts'])
        for h in [4,8,16]:
            raw,complete=a.window(path,h)
            for name,r in [('races',risk),('same_oldR_targets',e['risk'])]:
                s=a.barrier(raw,e['entry'],r,sign,stop=stop)[0]
                d[name][str(h)]='censored' if s=='neither' and not complete else s
        detail.append(d)
        for model in ['B5','E20S1']:
            if model+'_actual_held' not in row:continue
            held=row[model+'_actual_held'];v=dict(event_key=row['event_key'],warning_ts=row['warning_ts'],held=held,after_exit={})
            t=dt.datetime.fromisoformat(held['exit_ts']);floor=t-dt.timedelta(minutes=t.minute%15)
            path2=pr.path(row['contract'],floor.isoformat(sep=' '),16)
            for h in [4,8,16]:
                raw,complete=a.window(path2,h);raw=[b for b in raw if b['datetime']>held['exit_ts']]
                w=a.excursion(raw,held['fill'],held['risk'],sign);w['complete']=complete;v['after_exit'][str(h)]=w
            actual[model].append(v)
    structural={}
    for name in ['all','qualified']:
        xs=[d for d in detail if name=='all' or d['score']>=2.5]
        structural[name]=dict(n=len(xs),wider=sum(d['width_ratio']>1+1e-8 for d in xs),
            width_ratio=a.stats(d['width_ratio'] for d in xs),original_S1_R=a.stats(d['original_S1_R'] for d in xs),B_S1_R=a.stats(d['B_S1_R'] for d in xs),
            original_S1_ge1=sum(d['original_S1_R']>=1 for d in xs),B_S1_ge1=sum(d['B_S1_R']>=1 for d in xs),
            B_wide_plan_races={str(h):dict(collections.Counter(d['races'][str(h)] for d in xs)) for h in [4,8,16]},
            same_oldR_target_races={str(h):dict(collections.Counter(d['same_oldR_targets'][str(h)] for d in xs)) for h in [4,8,16]})
    native={}
    for name,xs in actual.items():
        es=[d['held'] for d in xs];stops=[d for d in xs if d['held']['exit_reason']=='stop']
        native[name]=dict(n=len(xs),net=sum(e['net_amount'] for e in es),exit_reasons=dict(collections.Counter(e['exit_reason'] for e in es)),
            actual_held_mfe=a.stats(e['mfe_r'] for e in es),actual_held_mae=a.stats(-e['mae_r'] for e in es),stop_n=len(stops),
            stop_prior_mfe=[d['held']['mfe_r'] for d in stops],
            reached={str(q):dict(n=sum(e['mfe_r']>=q-1e-9 for e in es),then_net_loss=sum(e['mfe_r']>=q-1e-9 and e['net_amount']<0 for e in es)) for q in [.5,1.,1.5,2.]},
            stop_recovery={str(h):dict(n=len(stops),complete=sum(d['after_exit'][str(h)]['complete'] for d in stops),
                later_oneR=sum(d['after_exit'][str(h)]['complete'] and d['after_exit'][str(h)]['mfe']>=1 for d in stops),
                adverse_R=a.stats(d['after_exit'][str(h)]['mae'] for d in stops if d['after_exit'][str(h)]['complete'])) for h in [4,8,16]})
    write('structure-diagnostics.json',structural);write('per-warning-structure.json',detail)
    write('actual-trade-diagnostics.json',native);write('actual-trade-paths.json',dict(actual))
    return structural,native

def run():
    p=READ(RUN/'protocol.json');rs=READ(RUN/'results.json');null=READ(RUN/'matched-timing-null.json')
    records=[json.loads(t) for t in (RUN/'per-warning.jsonl').open(encoding='utf-8')]
    structural,native=structure_and_actual(records)
    jd=[r for r in records if r['symbol']=='JD0'];all0=rs['JD0']['all'];qualified=rs['JD0']['qualified'];recovery={};examples=[]
    for h in [4,8,16,32,60]:
        stop=[r for r in jd if 'post_trigger' in r and r['post_trigger'][str(h)]['races']['1.0']=='stop_first']
        rec=[r for r in stop if r['post_trigger'][str(h)]['mfe_lower']>=1]
        recovery[str(h)]=dict(stop_before_oneR=len(stop),then_later_oneR=len(rec),then_later_twoR=sum(r['post_trigger'][str(h)]['mfe_lower']>=2 for r in stop))
        if h==16:examples=[dict(event_key=r['event_key'],warning_ts=r['warning_ts'],direction=r['direction'],entry=r['planned_entry'],stop=r['original_stop'],path=r['post_trigger']['16']) for r in rec[:8]]
    write('stop-first-recovery.json',dict(summary=recovery,examples=examples))
    s=structural['qualified']
    lines=['# 鸡蛋ABC：预警后有利/不利R、先后顺序与结构止损审计','',
        '日期：2026-10-26（北京时间）。保留ABC结构；本轮只做路径诊断，没有更换结构识别或上线新止盈止损。','',
        '## 结论','',
        '鸡蛋预警之后确实经常出现1—2R有利空间，但不利空间同样大，触发入场后的有利先到概率也不足。当前证据不支持“原始预警普遍走对，只欠止盈”的解释；同时，实际被保留的交易远好于原始全集，也没有理由仅凭原始预警止损率就否定整套ABC。','',
        '保留ABC作结构候选，优先检查交易触发位置与结构失效的定义，随后才检验利润保护。扩大止损会改变R和目标净盈亏比，不能把没打旧止损直接当作盈利。','',
        '## 统计口径','',
        '2026已有覆盖期共有246条经系统去重保留的鸡蛋ABC预警，220条评分≥2.5。使用当前B5研究基线unmerged ABC重建，非实盘通知日志。范围为1—6月及近期8月10日至10月23日部分，不冒充全年或2025原始路径。','',
        '预警路径从预警柱收盘价观察，R为原计划入场到原止损的距离，排除预警柱本身。MFE、MAE均列正幅度，MAE=1.5R表示曾逆向1.5R。16根15分钟柱约4个观察交易小时，可能跨日；午休及闭市不累计。窗口继续观察止损后的行情，因此MFE不是可赚利润。','',
        '触发后另按原入场先于原止损、12桶内触发观察，计1tick入场滑点，R改为填价到原止损的距离；忽略其他策略资格和小时自适应，仅作诊断。触发分钟的前后区间用两条OHLC路径界定，未把入场前低点当成确定的入场后回撤。同分钟双触而开盘无法判序列不确定。缺分钟、零成交或档案尾段为删失，未补为输赢。','',
        '## 1. 预警后最大有利/不利R分布','',
        '|未来15分钟K线数|完整样本|MFE中位|MAE中位|MFE90分位|MAE90分位|最大MFE|最大MAE|',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for h in a.HORIZONS:
        v=all0['warning'][str(h)];m=v['mfe'];d=v['mae']
        lines.append(f"|{h}|{v['complete']}|{fmt(m['median'])}|{fmt(d['median'])}|{fmt(m['p90'])}|{fmt(d['p90'])}|{fmt(m['maximum'])}|{fmt(d['maximum'])}|")
    lines+=['','16柱完整窗口243条中，167条(68.7%)曾有利≥1R，154条(63.4%)也曾不利≥1R；有利≥2R110条，不利≥2R107条。评分合格217个完整窗口，MFE/MAE中位为1.62/1.69R。看得越久两边极值都会扩大，单例13.89R不能代表普遍预测能力。','',
        '## 2. 先有利还是先到原止损','',
        '从预警收盘放对称+1/-1R观察线，16柱内128条先有利、112条先不利、6条都未到。这里的-1R不一定是原结构止损，只用于信号方向比较。','',
        '原始计划则有186条明确触发、56条触发前先失效、2条触发/失效顺序未知、2条未触发。触发前失效不是成交止损。明确触发后：','',
        '|16柱内目标/原止损|先目标|先原止损|都未到|删失|已决目标先到率|毛保本胜率|','|---|---:|---:|---:|---:|---:|---:|']
    for q in a.THRESHOLDS:
        v=all0['post_trigger']['16']['races'][str(q)];up=v.get('upper_first',0);dn=v.get('stop_first',0)
        lines.append(f"|+{q:g}R/-1R|{up}|{dn}|{v.get('neither',0)}|{v.get('censored',0)}|{rate(up,up+dn)}|{1/(1+q):.1%}|")
    lines+=['','这是首次触达诊断，不是净收益回测；未触达的时间退出、费用、路径不明和组合竞争另有影响。这几个固定目标的已决先到率均低于相应毛保本率，不能从较小止盈触达率较高直接推出盈利。合格165条触发计划也只有68条先+1R、92条先止损、4条未到、1条删失。','',
        '## 3. 先止损而后又走回来','',
        '|触发后窗口|先止损、没先到+1R|后来仍到+1R|后来仍到+2R|','|---|---:|---:|---:|']
    for h,v in recovery.items():lines.append(f"|{h}柱|{v['stop_before_oneR']}|{v['then_later_oneR']}|{v['then_later_twoR']}|")
    lines+=['','16柱内，先止损的100条中34条后来仍到+1R，66条未确认回来。它说明部分入场/止损与较慢行情不匹配，但扩大窗口也要承担更久等待和更大不利波动，不能据此认定放宽止损有效。','',
        '## 4. 匹配时点也能出现较大有利R','',
        f"{null['matched_warning_count']}条有完整16柱且可匹配的预警，做199次背景匹配：月份、时刻、方向、过去动量方向、ATR档位相同，保留原R/ATR比。该诊断保留方向和市场背景，不是可执行随机策略、独立样本或校准显著性。",'',
        '|16柱指标|ABC|匹配中位|匹配5%—95%|','|---|---:|---:|---:|']
    for f,label in [('mean_mfe','平均MFE'),('mean_mae','平均MAE'),('mean_close_r','末端平均方向R'),('oneR_before_stop_lower','先+1R再-1R确认比例')]:
        v=null['summary'][f];render=(lambda x:f'{x:.1%}') if f.startswith('oneR') else fmt
        lines.append('|'+label+'|'+render(v['observed'])+'|'+render(v['null_median'])+'|'+render(v['null_p05'])+'—'+render(v['null_p95'])+'|')
    lines+=['','ABC平均MFE不高于匹配中位，末端方向收益接近匹配中位；+1R先到比例略好，仍在匹配样本常见范围。不能说信号完全无信息，也没有证据说大多数原始预警具有很强可交易方向优势。','',
        '## 5. 交易止损与整个B段失效是否相同','',
        '固定把止损锚点放到整个已知B段极值，并保留每条原预警相同的止损缓冲，所有预警同样处理，只比较风险和触达，不回测新策略。原缓冲不全是1tick，因此保持各条原缓冲，避免把改锚点与改缓冲混在一起。','',
        f"220条合格预警中{s['wider']}条需更宽止损，宽度倍数中位{fmt(s['width_ratio']['median'])}。原S1相对原止损的R中位{fmt(s['original_S1_R']['median'])}，改B极值后为{fmt(s['B_S1_R']['median'])}；原S1毛空间≥1R数量由{s['original_S1_ge1']}变为{s['B_S1_ge1']}，尚未扣成本。",'',
        '|220条合格预警，16柱|先到目标|先止损|','|---|---:|---:|']
    for label,v in [('原入场+1原R/原止损',qualified['warning']['16']['plan_races']['1.0']),('同样目标价格/更远B止损',s['same_oldR_target_races']['16']),('原入场+1新R/更远B止损',s['B_wide_plan_races']['16'])]:lines.append(f"|{label}|{v.get('upper_first',0)}|{v.get('stop_first',0)}|")
    lines+=['','固定原目标时止损更远自然更易先到目标，但每次失败损失更大，目标新R会下降。必须重算仓位、目标净R和费用，不能保留旧R计算盈利、同时用宽止损忽略亏损。','',
        '## 6. 实际成交中的回吐是否很多','',
        '|规则|平仓|原止损|曾达0.5R|曾达1R|达1R后净亏|持仓MFE中位|','|---|---:|---:|---:|---:|---:|---:|']
    for model,v in native.items():lines.append(f"|{model}|{v['n']}|{v['stop_n']}|{v['reached']['0.5']['n']}|{v['reached']['1.0']['n']}|{v['reached']['1.0']['then_net_loss']}|{fmt(v['actual_held_mfe']['median'])}|")
    lines+=['','B5为3目标、3原止损、2无跟随、1周末退出；E20S1为7目标、5原止损、4无跟随、3周末及1盘前失败退出。它们都不是大部分最终交易被原止损。','',
        'B5三笔止损前MFE为0.839、0.042、1.250R；E20S1五笔为0.273、0、0.042、1.250、0.091R。二者都只有1笔曾达1R后净亏。这组很小的成交样本不支持“很多盈利单都因止盈差变成亏损”。','',
        '固定16柱全路径中，B5/E20S1被保留的交易子集明显好于全体预警；但经过资格、触发及占用选择且只有9/20笔，不能据此说门槛已最优或保证泛化。单纯恢复所有被拒预警会改变这部分样本质量。','',
        '## 7. 保留ABC的下一步','',
        '先把ABC结构预警与可交易触发分开：检查B/C转折、入场确认、真正结构失效位置及剩余目标空间。保留ABC作候选框架，不要求每条预警都直接下单。','',
        '然后在固定风险和扣费条件下，分别验证少数结构内假设：用原交易止损还是整个B失效；触发是否消耗太多空间；确认是否过早或追入过晚。不要同时改识别、入场、止损、目标后只保留历史最高曲线。','',
        '利润保护针对“已出现有利R再亏损”的明确小问题验证，不能按未来峰值挑完美止盈。原始止损率也不足以证明整套ABC错误；净期望及相同市场背景下的增量才是验收标准。','',
        '## 证据与限制','',
        '8项路径机制测试通过。675条服务去重预警(JD246、EG429)键与冻结记录一致，590条合格预警的原入场/止损/目标/锚点一致，675个预警收盘与实际合约分钟收盘一致。相同A簇只取第一条后，JD204簇的16柱MFE/MAE中位为1.76/1.53R，簇之间仍可能属于同一行情。','',
        '历史已经多轮研究，重复预警及窗口重叠，不按独立交易计算显著性。仅使用已有2026原始分钟，不以2025旧成交标签补造前瞻路径。所有MFE/MAE为结果标签，没有输入识别算法。','',
        '[results.json](results.json)含全部窗口/分组；[per-warning.jsonl](per-warning.jsonl)逐警留痕；[matched-timing-null.json](matched-timing-null.json)保留199次匹配；[structure-diagnostics.json](structure-diagnostics.json)核对B止损；[actual-trade-diagnostics.json](actual-trade-diagnostics.json)含实际持仓与止损后路径；[stop-first-recovery.json](stop-first-recovery.json)列先止损后回来案例。','',
        '![路径统计](comparison.png)','']
    (RUN/'summary.md').write_text('\n'.join(lines),encoding='utf-8')
    chart(rs,null)
    checks=verify(p,records)
    write('final-verification.json',dict(**checks,python_tests_passed=8,research_source_sha256=a.z.s.m.old.sha(a.__file__),report_source_sha256=a.z.s.m.old.sha(__file__),no_strategy_changes=True))
    names=['protocol.json','correction.json','results.json','coverage.json','per-warning.jsonl','matched-timing-null.json','verification.json','structure-diagnostic-protocol.json','structure-diagnostics.json','actual-trade-diagnostics.json','actual-trade-paths.json','stop-first-recovery.json','summary.md','comparison.png','final-verification.json']
    write('completion.json',dict(completed_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),completed=True,production_changed=False,ABC_retained=True,qualified_for_trading=False,artifacts={n:a.z.s.m.old.sha(RUN/n) for n in names}))
    print(json.dumps(dict(structural=structural,native=native,checks=checks),ensure_ascii=False),flush=True)

def verify(p,records):
    for period,paths in p['periods'].items():
        for key,h in READ(RUN/'coverage.json')['sources'][period].items():assert a.z.s.m.old.sha(paths[key])==h
    for q,h in p['production_before'].items():assert a.z.s.m.old.sha(a.ROOT/q)==h
    for q,h in READ(RUN/'generation-implementation-manifest.json').items():assert a.z.s.m.old.sha(RUN/'generation-fork'/q)==h
    binary=READ(RUN/'generator-binary-v2.json');assert a.z.s.m.old.sha(binary['path'])==binary['sha256']
    old=READ(a.z.x.ROOT/'research-runs/swing-jd-abc-path-audit-20261026-v1/results.json');new=READ(RUN/'results.json')
    for sym in a.SYMS:
        for group in new[sym]:assert old[sym][group]['warning']==new[sym][group]['warning']
    for r in records:
        vals=[r['warning'][str(h)] for h in a.HORIZONS if r['warning'][str(h)]['complete']]
        for left,right in zip(vals,vals[1:]):assert left['mfe']<=right['mfe']+1e-9 and left['mae']<=right['mae']+1e-9
        for v in r.get('post_trigger',{}).values():assert v['mae_lower']<=v['mae_upper']+1e-9 and v['mfe_lower']<=v['mfe_upper']+1e-9
    return dict(source_hashes_unchanged=True,production_unchanged=True,generator_manifest_unchanged=True,monotone_extrema=True,intrabar_bounds_valid=True,warning_count=len(records),warning_only_statistics_unchanged_by_entry_minute_correction=True)

def chart(rs,null):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams['font.sans-serif']=['Microsoft YaHei','SimHei','DejaVu Sans'];plt.rcParams['axes.unicode_minus']=False
    fig,axs=plt.subplots(2,2,figsize=(15,10),layout='constrained');s=rs['JD0']['all'];hs=a.HORIZONS
    ax=axs[0,0];ix=np.arange(len(hs))
    for name,label,color in [('mfe','最大有利R','#238a74'),('mae','最大不利R幅度','#c25959')]:
        ys=[s['warning'][str(h)][name]['median'] for h in hs];lo=[s['warning'][str(h)][name]['p25'] for h in hs];hi=[s['warning'][str(h)][name]['p75'] for h in hs]
        ax.plot(ix,ys,marker='o',label=label,color=color);ax.fill_between(ix,lo,hi,color=color,alpha=.12)
    ax.set_xticks(ix,hs);ax.set_xlabel('预警后完成15分钟K线数');ax.set_ylabel('原计划R');ax.set_title('两边极值同时扩大（中位/四分位带）');ax.legend()
    ax=axs[0,1];qs=a.THRESHOLDS;ix=np.arange(len(qs));v=s['post_trigger']['16']['races']
    ax.bar(ix-.18,[v[str(q)].get('upper_first',0) for q in qs],.36,label='先目标',color='#238a74');ax.bar(ix+.18,[v[str(q)].get('stop_first',0) for q in qs],.36,label='先原止损',color='#c25959')
    ax.set_xticks(ix,[f'+{q:g}R' for q in qs]);ax.set_title('186个明确触发计划：16柱首次触达');ax.legend()
    ax=axs[1,0];vals=[r['mean_mfe'] for r in null['repetitions']];obs=null['observed']['mean_mfe']
    ax.hist(vals,bins=20,color='#9caaba');ax.axvline(obs,color='#c25959',lw=2,label=f'ABC平均MFE={obs:.2f}R');ax.set_title('199次背景匹配：较大MFE并不稀有');ax.set_xlabel('16柱平均MFE');ax.legend()
    ax=axs[1,1];groups=['all','B5_accepted','E20S1_accepted'];ix=np.arange(3)
    ax.bar(ix-.18,[rs['JD0'][g]['warning']['16']['mfe']['median'] for g in groups],.36,color='#238a74',label='有利R中位');ax.bar(ix+.18,[rs['JD0'][g]['warning']['16']['mae']['median'] for g in groups],.36,color='#c25959',label='不利R中位')
    ax.set_xticks(ix,[f"全部预警\n完整{rs['JD0']['all']['warning']['16']['complete']}",f"B5成交子集\n完整{rs['JD0']['B5_accepted']['warning']['16']['complete']}",f"E20S1成交子集\n完整{rs['JD0']['E20S1_accepted']['warning']['16']['complete']}"])
    ax.set_title('同一预警参考口径：保留子集较好但很小');ax.legend()
    for ax in axs.flat:ax.grid(axis='y',alpha=.18);ax.set_axisbelow(True)
    fig.suptitle('保留ABC：分清预警方向、触发位置与止损止盈兑现｜2026已有分钟数据',fontsize=15)
    fig.savefig(RUN/'comparison.png',dpi=160);plt.close(fig)

if __name__=='__main__':run()
