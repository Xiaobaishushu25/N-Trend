"""Sequential, explicitly disclosed B8 filter hypotheses and July tail check."""
import collections
import copy
import datetime as dt
import json
import subprocess
import research_swing_jd_frequency as z

RUN=z.RUN/'breakout-followup'
READ=z.READ
EV=z.EV
NAMES=['B8','ALIGNED','EFFICIENT','SAME_DAY']
LABELS={'B8':'8柱突破日内原版','ALIGNED':'突破＋8/32慢趋势顺向','EFFICIENT':'突破＋ER16≥0.30','SAME_DAY':'突破通道全部形成于当天','CASH':'空仓'}

def write(n,x):z.s.m.old.write(RUN/n,x)
def qualifies(n,m):
    if n=='B8':return True
    sign=1 if m['direction']=='up' else -1
    if n=='ALIGNED':return sign*(m['ma8']-m['ma32'])>0 and sign*(m['ma32']-m['slow4'])>0
    if n=='EFFICIENT':return m['er16']>=.30
    if n=='SAME_DAY':return m['same_day8']
    raise AssertionError(n)

def run():
    RUN.mkdir(exist_ok=False)
    protocol=dict(created_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
        candidates=NAMES,filters=LABELS,profile='Original frozen QUICK; no entry/exit/cost changes.',
        reason='After observing24-system discovery: B8_QUICK180trades main1803yuan but thin and stress-negative. Freeze3 simple causal filters before viewing their PnL and before evaluating unused July score tail. No parameter grid or filter conjunction search.',
        extra_score=['2026-07-01 00:00:00','2026-07-10 15:00:00'],exit_data='Existing early actual minutes throughJuly15; signals<=14:15, predeclared daily flattening unchanged.',
        ALIGNED='Long SMA8>SMA32 and SMA32>its4bar-lag; symmetric short.',
        EFFICIENT='Existing fixed ER16>=.30, no optimized threshold.',
        SAME_DAY='Every one of the8preceding channel bars has same calendar trading date as completed signal bar.',
        selection='Freeze July1 allocation using Jan-Jun closed exits strictly beforeJuly1: >=20closed, rate>=.35per shared day, all8case normalized net positive, primary ex-best and ex-best-month positive; max min8norm-.25max8DD, declared tie, elseCASH. Exactly one July evaluation, no post-result filter rescue.',
        limitation='Sequential hypotheses selected after inspecting other2026 dates; original Q1/Q2/Q3/October outcomes are reused exploration, not independent OOS. July score tail not used to choose these filters, but8trading days are far too few for stability or annual claims.',
        parent_results_sha256=z.s.m.old.sha(z.RUN/'fixed-results.json'),script_sha256=z.s.m.old.sha(__file__))
    write('protocol.json',protocol)
    p=READ(z.RUN/'protocol.json');meta=READ(z.RUN/'signal-metadata.json')
    pp=READ(z.ROOT/'research-strategies/best-a-portfolio-observation-v1/protocol.json')
    # Add same-day and direction attributes to existing metadata strictly from prefix.
    for period in ['early','recent']:
        raw=READ(pp['periods'][period]['input']);bs=z.x.identification_bars(raw['symbols']['JD0']['bars'])
        same={bs[i]['datetime']:all(b['datetime'][:10]==bs[i]['datetime'][:10] for b in bs[i-8:i]) for i in range(8,len(bs))}
        for key,m in meta.items():
            if m['period']!=period:continue
            m['same_day8']=same[m['warning_ts']];m['direction']=key.split('|')[3]
    c,ends,data,_,_,_=z.load()
    cases=[pol+'/'+sf for pol in p['policies'] for sf in p['scenarios']]
    old_results={};old_accepted={};training={};eligible=[]
    train_days=sum(d<'2026-07-01' for d in READ(z.RUN/'generation-checks.json')['shared_dates'])
    for n in NAMES:
        training[n]={}
        for case in cases:
            pol,sf=case.split('/',1);rr=[a for a in data['BREAK8/QUICK',sf] if qualifies(n,meta[a['event_key']])]
            aa,_,_=z.select_orders(rr,ends,pol,c);old_accepted[n,case]=aa
            q=z.metric(aa);old_results[n+'/'+case]=q
            training[n][case]=z.metric(z.x.visible(aa,'2026-07-01 00:00:00'),[m for m in z.MONTHS if m<'2026-07'])
        q=training[n][p['primary']];zs=list(training[n].values())
        if q['n']>=20 and q['n']/train_days>=.35 and min(a['normalized_net'] for a in zs)>0 and q['normalized_ex_best']>0 and q['normalized_ex_best_month']>0:
            score=min(a['normalized_net'] for a in zs)-.25*max(a['normalized_realized_dd'] for a in zs)
            eligible.append((score,n))
    eligible.sort(key=lambda q:(-round(q[0],8),NAMES.index(q[1])))
    chosen=eligible[0][1] if eligible else 'CASH'
    write('selection-before-july.json',dict(selected=chosen,eligible=eligible,train_days=train_days,training=training))
    # Freeze allocation above before generating or replaying any July signal outcomes.
    period='early';paths=pp['periods'][period];inp=READ(paths['input']);segments=READ(paths['segments'])['JD0']
    bs=z.x.identification_bars(inp['symbols']['JD0']['bars']);count=collections.Counter(b['datetime'] for b in bs)
    codes=[next((s['underlying'] for s in segments if s['first']<=b['datetime']<=s['last']),None) for b in bs]
    source=READ(z.RUN/'early/QUICK/early-pool.json');proto=copy.deepcopy(source[0]['event']);pool=[];jmeta={};dates=set()
    for i in range(36,len(bs)):
        b=bs[i];ts=b['datetime']
        if not protocol['extra_score'][0]<=ts<=protocol['extra_score'][1] or ts[11:16]>'14:15':continue
        if any(count[a['datetime']]!=1 for a in bs[i-36:i+1]) or codes[i] is None or len(set(codes[i-36:i+1]))!=1:continue
        dates.add(ts[:10]);f=z.features(bs,i);ops=z.opportunities(bs,i,f,set())
        if 'BREAK8' not in ops:continue
        op=ops['BREAK8'];g=z.geometry(b,f,op,'QUICK',1.);key='|'.join(['QUICK','BREAK8','JD0',op['direction'],ts])
        e=copy.deepcopy(proto);e.update(direction=op['direction'],warning_ts=ts,s0_ts=ts,s1_ts=ts,s2_ts=ts,**g)
        e['setup_features']['direction']=op['direction']
        pool.append(dict(event_key=key,event=e,warning_candle=dict(b,volume=0.,hold=0.),prior=[],atr14=f['atr'],original_friction=0.,original_risk_atr=g['risk']/f['atr'],entry_quality={}))
        jmeta[key]=dict(direction=op['direction'],same_day8=all(a['datetime'][:10]==ts[:10] for a in bs[i-8:i]),**f)
    task=RUN/'july';task.mkdir()
    z.s.m.old.write(task/'early-pool.json',pool)
    ep=READ(z.RUN/'early/QUICK/execution.protocol.json')
    z.s.m.old.write(task/'execution.protocol.json',ep)
    b=READ(z.RUN/'binary.json');assert z.s.m.old.sha(b['path'])==b['sha256']
    subprocess.run([b['path'],str(task/'execution.protocol.json'),'early','replay',str(task/'executions')],check=True)
    rows=collections.defaultdict(list);fees=READ(z.ROOT/'scripts/exchange_fees_20261022.json');cal=z.s.TradingDates({});cal.dates=READ(z.RUN/'early/QUICK/trading-dates.json')
    for line in (task/'executions/evaluations.jsonl').open(encoding='utf-8'):
        a=z.s.reprice(json.loads(line),fees,cal);sf=a['scenario'].split('/',1)[1];rows[sf].append(a)
    extra={};combined={}
    for n in NAMES:
        for case in cases:
            pol,sf=case.split('/',1);new=[a for a in rows[sf] if qualifies(n,jmeta[a['event_key']])]
            old=[a for a in data['BREAK8/QUICK',sf] if qualifies(n,meta[a['event_key']])]
            aa,_,_=z.select_orders(old+new,ends,pol,c)
            combined[n+'/'+case]=z.metric(aa)
            extra[n+'/'+case]=z.metric([a for a in aa if protocol['extra_score'][0]<=a['warning_ts']<=protocol['extra_score'][1]],['2026-07'])
    write('existing-history-results.json',old_results);write('july-results.json',extra);write('combined-results.json',combined)
    write('signal-metadata.json',jmeta)
    notes=['# 短通道突破的三种过滤与7月补充检验','',
        '此项在观察本轮24个系统后才提出，因此旧时间组结果属于继续探索；不称作独立验证。7月信号结果在条件和7月分配冻结后才生成。所有条件共用既定日内执行，不调止损、目标、费用或持仓。','',
        '|规则|原覆盖笔数|原覆盖净额|原覆盖最低场景净额|补充7月笔数|补充7月净额|补充7月最低场景|合并覆盖笔数|合并净额|',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    primary=p['primary']
    for n in NAMES:
        a=old_results[n+'/'+primary];j=extra[n+'/'+primary];v=combined[n+'/'+primary]
        notes.append(f"|{LABELS[n]}|{a['n']}|{a['net']:.2f}|{min(old_results[n+'/'+c]['net'] for c in cases):.2f}|{j['n']}|{j['net']:.2f}|{min(extra[n+'/'+c]['net'] for c in cases):.2f}|{v['n']}|{v['net']:.2f}|")
    notes+=['',f"7月1—10日实际共享日期{len(dates)}天，原生候选{len(pool)}条；7月15日前分钟仅作退出尾段。7月1日仅用此前平仓训练选择：**{LABELS[chosen]}**。",'',
        '7月组很短，不能据此确认月度稳定性、推全年收益或宣布过滤有效。旧日期所有结果已参与过探索；不得把事后最高过滤结果冒充选定规则的收益。','',
        '|规则|原Q1净额|原Q2净额|原Q3覆盖段净额|原10月部分净额|','|---|---:|---:|---:|---:|']
    for n in NAMES:
        aa=old_accepted[n,primary];vals=[]
        for f in z.x.blocks():vals.append(z.metric([a for a in aa if f['start']<=a['warning_ts']<f['end']])['net'])
        notes.append('|'+LABELS[n]+'|'+'|'.join(f'{v:.2f}' for v in vals)+'|')
    (RUN/'summary.md').write_text('\n'.join(notes)+'\n',encoding='utf-8')
    for q,h in READ(z.RUN/'protocol.json')['production_before'].items():assert z.s.m.old.sha(z.ROOT/q)==h
    write('completion.json',dict(completed=True,production_changed=False,selection=chosen,july_shared_dates=sorted(dates),july_candidate_count=len(pool),
        artifacts={q.name:z.s.m.old.sha(q) for q in RUN.glob('*.json') if q.name!='completion.json'},report_sha256=z.s.m.old.sha(RUN/'summary.md')))
    print(json.dumps(dict(selected=chosen,main={n:dict(n=old_results[n+'/'+primary]['n'],net=old_results[n+'/'+primary]['net'],worst=min(old_results[n+'/'+c]['net'] for c in cases),july=extra[n+'/'+primary]['net']) for n in NAMES}),ensure_ascii=False),flush=True)

if __name__=='__main__':run()
