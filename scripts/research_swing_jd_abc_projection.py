"""Sequential ABC target hypothesis: one A-leg measured move, no target grid."""
import collections
import copy
import datetime as dt
import json
import subprocess
import research_swing_jd_abc_execution as b

RUN=b.RUN/'A-projection'
READ=b.READ
EV=b.EV
MODELS=['A1_TOUCH','A1_RETEST','A1_C15']
LABELS={'A1_TOUCH':'A等幅目标＋原触发','A1_RETEST':'A等幅目标＋突破回踩','A1_C15':'A等幅目标＋C确认'}
def write(n,x):b.z.s.m.old.write(RUN/n,x)

def run():
    RUN.mkdir(exist_ok=False)
    parent=READ(b.RUN/'protocol.json');templates={}
    for m,old in [('A1_TOUCH','TOUCH_BTRUE'),('A1_RETEST','RETEST_BTRUE'),('A1_C15','C15_BTRUE')]:
        templates[m]=copy.deepcopy(parent['templates'][old]);templates[m]['name']=m
    protocol=dict(created_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),templates=templates,
        purpose='After inspecting8-family signal funnel, freeze ONE structural target formula before viewing its executions. OriginalS1 caps reject many plans before portfolio occupancy. RetainABC detector and entireB stop.',
        target='entire_B_extreme + direction*A_move, grid-rounded toward entry. Both A_move and B_extreme known at originalABC warning. No target fitted from future MFE; do not force target beyondS1 if formula is nearer.',
        entries='3 inherited entries: touch,retest,C15; same gates,costs,pending,maxhold,stop buffers,peak-only safe equivalence. C15 regeneration cancels on projection target rather thanS1 becauseS1 is no longer exit. Parent grade/body remain warning-known.',
        limitation='Sequential exploratory hypothesis AFTER old-family outcomes observed. Old date groups are not fresh OOS. No grid of A multiples, no rescue after seeing results. Compare all failures, density and risk-normalized stresses.',
        primary=parent['primary'],models=MODELS,script_sha256=b.z.s.m.old.sha(__file__))
    write('protocol.json',protocol)
    pp=READ(b.ROOT/'research-strategies/best-a-portfolio-observation-v1/protocol.json');meta={};qa=collections.Counter()
    for period in ['early','recent']:
        paths=pp['periods'][period];inp=READ(paths['input']);bars=b.z.x.identification_bars(inp['symbols']['JD0']['bars']);count=collections.Counter(q['datetime'] for q in bars)
        seg=READ(paths['segments'])['JD0'];codes=[next((s['underlying'] for s in seg if s['first']<=q['datetime']<=s['last']),None) for q in bars]
        seeds=[];cs=[];seen=set();hi='2026-07-10 15:00:00' if period=='early' else '2026-10-23 11:05:00'
        for source in READ(b.RUN/period/'seed'/(period+'-pool.json')):
            snap=copy.deepcopy(source);e=snap['event'];sign=1 if e['direction']=='up' else -1
            oldtarget=e['target'];target=snap['entry_quality']['b_true_extreme']+sign*e['a_move'];target=b.z.grid(target,e['tick_size'],sign<0)
            e['target']=target;e['rr']=abs(target-e['entry'])/abs(e['risk']) if e['risk'] else 0.
            key='A1|'+snap['event_key'];snap['event_key']=key
            snap['mechanism']['original_S1']=oldtarget;seeds.append(snap)
            cl='|'.join(e[f] for f in ['symbol','direction','s0_ts','s1_ts']);at=next(i for i,q in enumerate(bars) if q['datetime']==e['warning_ts']);end=at;code=codes[at]
            meta[key]=dict(parent_key=source['event_key'],cluster=cl,seed_ts=e['warning_ts'],warning_ts=e['warning_ts'],period=period,contract=code,target=target,original_S1=oldtarget)
            for j in range(at+1,min(at+13,len(bars))):
                if codes[j]!=code or count[bars[j]['datetime']]!=1 or bars[j]['datetime']>hi:break
                end=j
            ci,reason=b.c_confirmation(bars,at,end,e,snap['mechanism']['true_b_stop'],snap['entry_quality']['b_true_extreme']);qa[reason]+=1
            if ci is None:continue
            ci2,_=b.c_confirmation(bars[:ci+1],at,ci,e,snap['mechanism']['true_b_stop'],snap['entry_quality']['b_true_extreme']);assert ci==ci2
            q=bars[ci];ident=cl,q['datetime']
            if ident in seen:qa['duplicate_C_clock']+=1;continue
            seen.add(ident);c=copy.deepcopy(snap);ce=c['event'];entry=q['high']+e['tick_size'] if sign>0 else q['low']-e['tick_size'];ckey='C15|'+key+'|'+q['datetime']
            ce.update(warning_ts=q['datetime'],entry=entry,risk=(entry-e['stop'])*sign,rr=abs(target-entry)/abs(entry-e['stop']) if entry!=e['stop'] else 0.,trigger_level=entry,
                warning_extreme=q['high'] if sign>0 else q['low'],market_context=None)
            c.update(event_key=ckey,warning_candle=dict(q,volume=0.,hold=0.),prior=[]);cs.append(c)
            meta[ckey]=dict(parent_key=source['event_key'],cluster=cl,seed_ts=e['warning_ts'],warning_ts=q['datetime'],period=period,contract=code,target=target,original_S1=oldtarget)
        for task,pool,names in [('seed',seeds,MODELS[:2]),('C15',cs,MODELS[2:])]:
            dest=RUN/period/task;dest.mkdir(parents=True)
            proto=READ(b.RUN/period/task/'execution.protocol.json');proto['templates']=[templates[n] for n in names]
            b.z.s.m.old.write(dest/(period+'-pool.json'),pool);b.z.s.m.old.write(dest/'execution.protocol.json',proto)
            binary=READ(b.RUN/'binary.json');assert b.z.s.m.old.sha(binary['path'])==binary['sha256']
            subprocess.run([binary['path'],str(dest/'execution.protocol.json'),period,'replay',str(dest/'executions')],check=True)
            print(period,task,len(pool),'projection candidates',flush=True)
    write('signal-metadata.json',meta);write('generation-checks.json',dict(counts=dict(qa)))
    c,_,_,ends,_,_,_=b.load();data=collections.defaultdict(list);fees=READ(b.ROOT/'scripts/exchange_fees_20261022.json')
    for period in ['early','recent']:
        for task in ['seed','C15']:
            cal=b.z.s.TradingDates({});cal.dates=READ(b.RUN/period/task/'trading-dates.json')
            for line in (RUN/period/task/'executions/evaluations.jsonl').open(encoding='utf-8'):
                r=b.z.s.reprice(json.loads(line),fees,cal);r['period']=period;m,sf=r['scenario'].split('/',1);data[m,sf].append(r)
    cases=[p+'/'+s for p in parent['policies'] for s in parent['scenarios']];results={};accepted={}
    for case in cases:
        policy,sf=case.split('/',1)
        for m in MODELS:
            aa,ll=b.admit(data[m,sf],ends,policy,c,meta);accepted[case,m]=aa
            q=b.z.metric(aa);q['blocks']={f['label']:b.z.metric([r for r in aa if f['start']<=r['warning_ts']<f['end']]) for f in b.z.x.blocks()};results[case+'/'+m]=q
    # Explicitly retrospective only: strict-cutoff schedule is a diagnostic of
    # chronology, not an untouched discovery claim after hypothesis formation.
    dates=READ(b.RUN/'generation-checks.json')['covered_dates'];schedule=[];train=[]
    for start,end in b.FOLDS:
        nday=sum(d<start[:10] for d in dates);eligible=[];reason={}
        for m in MODELS:
            zs={case:b.z.metric(b.z.x.visible(accepted[case,m],start)) for case in cases};q=zs[parent['primary']];fail=[]
            if q['n']<20 or q['n']/nday<.35:fail.append('insufficient_training_frequency')
            if min(x['normalized_net'] for x in zs.values())<=0:fail.append('some_execution_nonpositive')
            if q['normalized_ex_best']<=0:fail.append('ex_best_nonpositive')
            if fail:reason[m]=fail
            else:eligible.append((min(x['normalized_net'] for x in zs.values())-.25*max(x['normalized_realized_dd'] for x in zs.values()),m))
        eligible.sort(key=lambda q:(-round(q[0],8),MODELS.index(q[1])));chosen=eligible[0][1] if eligible else 'CASH'
        schedule.append(dict(start=start,end=end,model=chosen));train.append(dict(cutoff=start,chosen=chosen,rejections=reason,eligible=eligible))
    write('fixed-results.json',results);write('chronological-selection.json',dict(schedule=schedule,training=train))
    with (RUN/'accepted-primary.jsonl').open('x',encoding='utf-8') as f:
        for m in MODELS:
            for r in accepted[parent['primary'],m]:f.write(json.dumps(dict(model=m,row=r),ensure_ascii=False)+'\n')
    lines=['# ABC内部追加：一倍A段投射目标','',
        '此假设在看到原S1空间漏斗后提出，属于顺序探索，不是独立未触碰样本。目标公式在新回放前冻结，未扫描倍数。保留整个B止损与全部费用/净盈亏比门槛。C确认按新目标取消，不因曾碰S1就取消。','',
        '|方案|平仓数|主净额|8场景最低净额|归一净额|8场景最低归一|最多未知|','|---|---:|---:|---:|---:|---:|---:|']
    for m in MODELS:
        q=results[parent['primary']+'/'+m];zs=[results[c+'/'+m] for c in cases]
        lines.append(f"|{LABELS[m]}|{q['n']}|{q['net']:.2f}|{min(v['net'] for v in zs):.2f}|{q['normalized_net']:.2f}|{min(v['normalized_net'] for v in zs):.2f}|{max(v['unknown'] for v in zs)}|")
    lines+=['','|方案|Q1训练|Q2|Q3覆盖段|10月部分|删除最佳3笔后归一净额|','|---|---:|---:|---:|---:|---:|']
    for m in MODELS:
        q=results[parent['primary']+'/'+m];lines.append('|'+LABELS[m]+'|'+'|'.join(f"{q['blocks'][n]['net']:.2f}" for n in ['Q1_train','Q2','Q3','October_partial'])+f"|{q['normalized_ex_top3']:.2f}|")
    lines+=['','附带严格按截止日训练的选择，仅作时间迁移诊断；假设已受旧历史启发，不能重新宣称独立OOS：','']
    lines.extend(f"- {f['start'][:7]}：{f['model']}" for f in schedule)
    lines+=['','[protocol.json](protocol.json)为新回放前固定协议；[fixed-results.json](fixed-results.json)保存全部场景。','']
    (RUN/'summary.md').write_text('\n'.join(lines),encoding='utf-8')
    for q,h in parent['production_before'].items():assert b.z.s.m.old.sha(b.ROOT/q)==h
    for q in results.values():assert abs(sum(q['month_cash'].values())-q['net'])<1e-7
    write('completion.json',dict(completed=True,production_changed=False,not_independent_OOS=True,artifacts={q.name:b.z.s.m.old.sha(q) for q in RUN.glob('*.json') if q.name!='completion.json'},report_sha256=b.z.s.m.old.sha(RUN/'summary.md')))
    print(json.dumps(dict(main={m:dict(n=results[parent['primary']+'/'+m]['n'],net=results[parent['primary']+'/'+m]['net'],worst=min(results[c+'/'+m]['net'] for c in cases),unknown=max(results[c+'/'+m]['unknown'] for c in cases)) for m in MODELS},schedule=schedule),ensure_ascii=False),flush=True)

if __name__=='__main__':run()
