"""Close this falsification round without promoting a failed research model."""
import collections,json,statistics
import research_swing_retirement as r
import research_swing_origin_break as x
import research_swing_abstop as s
from report_swing_wave_selection import identification_bars,first_index
w=r.w;RUN=r.RUN;MODELS={**r.MODELS,**x.MODELS}

def identity(z):return z['symbol'],z['direction'],z['warning_ts']
def run():
    p=w.read(RUN/'protocol.json');results=w.read(RUN/'results.json');audit=w.read(RUN/'state-audit.json');attrib=w.read(RUN/'attribution.json');verification=w.read(RUN/'verification.json');gates=w.read(RUN/'gates.json')
    rows={q:{} for q in ('early','recent')};cases=collections.defaultdict(dict)
    for line in (RUN/'accepted.jsonl').open(encoding='utf-8'):
        z=json.loads(line)
        if z['policy']=='original' and z['case'].endswith('/corrected_touch/slip1') and s.ev(z['row'])['status']=='closed':cases[(z['period'],z['case'].split('/')[0])][identity(z['row'])]=z['row']
    diagnosis={}
    for q in rows:
        inp=w.read(p['periods'][q]['input']);pools={identity(z['event']):z['event'] for z in w.read(RUN/'near_start'/(q+'-pool.json'))};barsets={sym:identification_bars(z['bars']) for sym,z in inp['symbols'].items()};indices={sym:first_index(bs) for sym,bs in barsets.items()};del inp
        trace={}
        for f in sorted((RUN/'current_origin_break'/(q+'-symbols')).glob('*.json')):
            z=w.read(f);ts=z['identification_times']
            for t in z['state_trace']:
                if t['blocked_s0'] is None or not t['would_warn'] or t['warning_score']<2.5:continue
                k=(f.stem,t['direction'],ts[t['w']]);trace.setdefault(k,[]).append(dict(t,owner_s0_ts=ts[t['owner_s0']],owner_s1_ts=ts[t['owner_s1']],blocked_s0_ts=ts[t['blocked_s0']],blocked_s1_ts=ts[t['blocked_s1']]))
        old=cases[(q,'near_start')];new=cases[(q,'current_origin_break')];lost=old.keys()-new.keys();removed=[old[k] for k in lost if k in trace];assert len(removed)==attrib['current_origin_break']['original'][q]['corrected_touch/slip1']['removed_reference_closed_by_state']
        detailed=[];counts=collections.Counter()
        for z in removed:
            k=identity(z);e=pools[k];arr=barsets[z['symbol']];idx=indices[z['symbol']];matches=[t for t in trace[k] if t['blocked_s0_ts']==e['s0_ts'] and t['blocked_s1_ts']==e['s1_ts']];assert matches,('missing blocked source pattern',k)
            t=matches[0];up=e['direction']=='up';sgn=1 if up else -1;i,j,end=[idx[e[f]] for f in ('s0_ts','s1_ts','warning_ts')];original_origin=arr[i]['low' if up else 'high'];bextreme=min(b['low'] for b in arr[j+1:end+1]) if up else max(b['high'] for b in arr[j+1:end+1]);survives=(bextreme-original_origin)*sgn>0
            assert survives,'Accepted original A was already path-broken';counts['original_A_origin_still_intact']+=survives
            counts['same_S1']+=t['owner_s1_ts']==e['s1_ts'];counts['newer_origin']+=t['owner_s0_ts']>e['s0_ts'];counts['new_owner_inside_original_B']+=t['owner_s0_ts']>e['s1_ts']
            detailed.append(dict(identity=list(k),old_s0=e['s0_ts'],old_s1=e['s1_ts'],new_s0=t['owner_s0_ts'],new_s1=t['owner_s1_ts'],original_origin_still_intact=survives,net=s.ev(z)['net_amount'],net_r=s.ev(z)['net_r'],old_A_bars=e['a_bars'],owner_A_bars=t['owner_s1']-t['owner_s0']+1))
        diagnosis[q]=dict(n=len(removed),net=sum(s.ev(z)['net_amount'] for z in removed),mean_r=statistics.mean(s.ev(z)['net_r'] for z in removed),win_rate=sum(s.ev(z)['net_amount']>0 for z in removed)/len(removed),
            counts=dict(counts),mean_reference_A_bars=statistics.mean(z['old_A_bars'] for z in detailed),mean_local_owner_A_bars=statistics.mean(z['owner_A_bars'] for z in detailed),trades=detailed)
    w.write(RUN/'nested-structure-diagnosis.json',diagnosis)
    assert verification['defensive_candidates']==[] and verification['full_passed']==[]
    decision=dict(status='reject_all_five_new_mechanisms_keep_previous_observation',selected='near_start/adaptive',previous_candidate_sha256=w.sha(r.SOURCE/'observation-candidate.json'),production_changed=False,qualified_for_trading=False,
        reason='All five violate recent80% retention across costs. Early cash reductions often reflect reduced count while meanR worsens. Broad permanent retirement only positive in touch1, reverses under slip2 or extra-minute delay.',
        inference='A newer local impulse can fail inside a still-intact larger A/B. Findings reject unconditional cross-scale veto for these definitions, not all causal invalidation or state models.',
        next_research='Separate leg identity/scale and lifecycle; any future invalidation applies to the same leg being traded, not every older/coarser impulse. Diagnose natural B completion and effective 15m A/B duration before another confirmation/stop grid.',
        independent_validation=False,paper_collection_started=False)
    w.write(RUN/'decision.json',decision)
    names={'near_start':'上一轮观察版','current_failure':'当前失效即拦截','retired_origin':'失效起点永久退役','current_owner':'最近推动独占','current_origin_break':'仅穿越起点时拦截','retired_origin_break':'穿越起点后永久退役'}
    lines=['# 本轮结论：不能把局部推动失效，直接当作较大结构失效','',
        '已完成5种新机制的两期完整提取、服务去重和机会竞争回放。全部未满足近期收益至少保留80%的要求，保留上一轮near_start/adaptive研究观察版，生产和实盘参数未改。上半年全周期稳定问题尚未解决。',
        '', '每边1tick、扣实际手续费，原未知占用口径。以下为单手已平仓金额及净R；近期仍有各品种起点不齐的覆盖边界，不是完整等覆盖四个月。',
        '', '|规则|上半年笔数|净额|平均净R|近期净额|近期保留|','|---|---:|---:|---:|---:|---:|']
    for model in MODELS:
        e=results['original']['early'][model+'/corrected_touch/slip1'];n=results['original']['recent'][model+'/corrected_touch/slip1'];b=results['original']['recent']['near_start/corrected_touch/slip1'];lines.append(f"|{names[model]}|{e['n']}|{e['net']:.2f}|{e['mean_r']:.4f}|{n['net']:.2f}|{n['net']/b['net']:.1%}|")
    lines+=['', '## 为什么逻辑合理却不能直接加成硬条件','',
        '“失效后不能复活”对同一个明确波段是合理假设，但本轮新增规则会把最近局部A的失效，传递给更早、可能更大的A。两个尺度并不一定代表同一笔结构。',
        '', '仅按实际S0穿越拦截，对原观察版被删掉的交易独立核对：',
        '', '|时期|直接被拦截原交易|这些交易原净额|平均净R|胜率|原交易自身A起点仍完整|','|---|---:|---:|---:|---:|---:|']
    for q in ('early','recent'):
        d=diagnosis[q];lines.append(f"|{'上半年' if q=='early' else '近期'}|{d['n']}|{d['net']:.2f}|{d['mean_r']:.4f}|{d['win_rate']:.1%}|{d['counts']['original_A_origin_still_intact']}/{d['n']}|")
    lines+=['', '被删机会中，上半年原A平均22.10根15分钟线，局部A仅5.87根；近期为20.96根与6.01根。上半年121/214、近期43/80的局部起点已位于原B段内部。把B内部一个小推动的失败，统一传递给原A，是本次硬性拦截的尺度混淆。',
        '', '这表明被拦截的是“另一个更近的局部推动已破坏，但原交易使用的A仍存活”的机会。近期这批机会本来贡献显著正收益，统一禁止旧A复用会删除大量有效机会。不能据此宣称局部破位永远无用，也不能把所有旧A回看都定性成实现bug。',
        '', '对原观察版再做同一A内部连续性审计：原预警池有上半年62条、近期26条A在形成途中严格穿越自身起点，但最终接纳的已平仓交易仅上半年2笔（原净额−269.36元）、近期0笔。它不足以解释时期差距。这是原交易子集诊断，未将删除两笔视作完整重算后必然可获得的增益。',
        '', '而“上半年总亏损减少”本身也会误导：当前失效拦截让笔数444→231、亏损6863→3779元，但平均净R从−0.028降至−0.092，留下交易的单位风险期望反而更差。',
        '', '最严格永久退役版上半年每边1tick净赚1110.86元，但近期仅保留6.3%收益；每边2tick变成−348.34元，额外一分钟延迟下分别−2084.07/−4725.16元。它没有解决稳健性要求。',
        '', '## 本轮保留什么','',
        '保留上一轮“最近结构合格A优先”的观察规则；不叠加本轮5种硬性失效条件。后续更值得研究同一波段的身份、尺度与生命周期：对正在交易的那个A判断失效，避免用嵌套小推动统一否决较大结构。已有15分钟A/B识别的实际持续长度，也应继续与“小波段兑现”的目标对照。该方向尚未验证，不能作为本轮收益改善交付。',
        '', '## 核验与边界','',
        '主实验4项测试通过，补充两种真实起点穿越模型后4项测试再次通过；包含多空镜像、单向状态退役、新起点重启，以及2/5/6/7/8/9模式的警报和状态前缀一致性。',
        '', f"原观察版24个时期/压力/占用统计与归档一致，44个品种时期的原始警报与服务去重身份一致。独立OHLC复核{verification['qa']['independent_price_failure_check']}条局部结构失效标记，并检查{verification['qa']['state_trace_causal_invariants']}条状态约束。生产169个文件哈希未变。",
        '', '两种未知敞口口径及四种执行/成本场景均保留。近期原观察版未知1笔，新机制可能因删除该机会而无未知，未知收益从未记为0。回撤是已实现现金回撤，非包含浮盈亏的完整账户权益。',
        '', '本轮及此前历史均为反复研究的开发数据。没有新增独立验证、纸面交易或实盘接入。全部失败模型、冻结定义、逐笔归因与品种敏感性保存在本目录。详细数表见report.md、results.json；结构嵌套逐笔核验见nested-structure-diagnosis.json。']
    (RUN/'summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    assert all(w.sha(k)==h for k,h in p['production_before'].items())
    for root in (RUN,x.RUN):assert all(w.sha(root/'fork'/k)==v for k,v in w.read(root/'implementation-manifest.json').items())
    artifacts=['summary.md','report.md','results.json','gates.json','attribution.json','verification.json','state-audit.json','nested-structure-diagnosis.json','original-A-continuity-diagnosis.json','decision.json','protocol.json','price-only-extension/protocol.json','analysis-plan-before-outcome-summary.json','test-verification.json','price-only-extension/test-verification.json']
    scripts=['scripts/research_swing_retirement.py','scripts/research_swing_origin_break.py','scripts/report_swing_retirement.py','scripts/finish_swing_retirement.py','tests/swing_retirement.rs']
    w.write(RUN/'completion.json',dict(status='complete_five_hypotheses_rejected_previous_candidate_retained',qualified_for_trading=False,production_changed=False,production_unchanged=169,unique_new_models=5,control_regressions=24,
        full_stability_reached=False,selected='near_start/adaptive',artifacts={f:w.sha(RUN/f) for f in artifacts},sources={f:w.sha(w.REPO/f) for f in scripts}))
    print(json.dumps({q:{k:v for k,v in d.items() if k!='trades'} for q,d in diagnosis.items()},ensure_ascii=False,indent=2),flush=True)

if __name__=='__main__':run()
