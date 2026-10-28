"""Report every frozen product hypothesis, including failed branches."""
import collections
import datetime as dt
import json
import statistics as st
import research_swing_product_mechanisms as x

RUN=x.RUN
READ=x.READ
EV=x.EV

def fmt(v): return '—' if v is None else f'{v:,.2f}'
def write(n,z): x.write(n,z)

def verify_controls():
    count=0
    for period in ['early','recent']:
        old={}
        for line in (x.s.ARCHIVE/period/'executions/evaluations.jsonl').open(encoding='utf-8'):
            a=json.loads(line)
            if a['symbol']=='JD0' and a['scenario'].split('/')[0] in ['S1','E20S1']:
                old[a['event_key'],a['scenario']]=a
        for line in (RUN/period/'JD/executions/evaluations.jsonl').open(encoding='utf-8'):
            a=json.loads(line)
            if a['scenario'].split('/')[0] not in ['S1','E20S1']:continue
            b=old[a['event_key'],a['scenario']]
            for k in ['result','entry','stop','target','planned_risk','new_friction','economic_pass','body_net_pass']:
                assert a.get(k)==b.get(k),(period,a['event_key'],a['scenario'],k)
            count+=1
    p=READ(RUN/'protocol.json')
    gen=READ(RUN/'generation-checks.json')
    pp=READ(x.ROOT/'research-strategies/best-a-portfolio-observation-v1/protocol.json')
    for period,hs in gen['sources'].items():
        for key,h in hs.items():assert x.s.m.old.sha(pp['periods'][period][key])==h
    for q,h in p['production_before'].items():assert x.s.m.old.sha(x.ROOT/q)==h
    # Every old frozen-library file was copied unchanged; one new runner is added.
    oldfork=x.s.ARCHIVE/'fork';newfork=RUN/'fork'
    same=0
    for q in oldfork.rglob('*'):
        if q.is_file():assert x.s.m.old.sha(q)==x.s.m.old.sha(newfork/q.relative_to(oldfork));same+=1
    return dict(exact_previously_seen_S1_E20S1_native_rows=count,
        copied_original_fork_files_unchanged=same,raw_input_hashes_unchanged=True,production_unchanged=True)

def run():
    p=READ(RUN/'protocol.json');primary=p['primary'];fixed=READ(RUN/'fixed-results.json')
    wf=READ(RUN/'walkforward-results.json');selection=READ(RUN/'selection-before-test.json')
    attr=READ(RUN/'attribution.json');funnel=READ(RUN/'funnel.json')
    controls=verify_controls();cases=list(wf)
    saved=[json.loads(z) for z in (RUN/'accepted-primary.jsonl').open(encoding='utf-8')]
    rows={(sym,name):[z['row'] for z in saved if z['product']==sym and z['model']==name]
          for sym,names in [('JD0',x.JD),('SR0',x.SR)] for name in names}
    changes=[]
    a={z['event_key']:z for z in rows['JD0','S1'] if EV(z)['status']=='closed'}
    b={z['event_key']:z for z in rows['JD0','S1_PATIENT'] if EV(z)['status']=='closed'}
    for k in sorted(a.keys()&b.keys()):
        old,new=EV(a[k]),EV(b[k])
        if old==new:continue
        changes.append(dict(key=k,warning_ts=a[k]['warning_ts'],old=old,new=new,net_delta=new['net_amount']-old['net_amount']))
    delta=sum(z['net_delta'] for z in changes)
    assert abs(delta-fixed[primary+'/JD0/S1_PATIENT']['net']+fixed[primary+'/JD0/S1']['net'])<1e-7
    best=max(changes,key=lambda z:z['net_delta'])
    summary={'products':{},'walkforward':{},'patient_vs_S1':dict(total_delta=delta,
        changes=changes,largest_change=best,delta_without_largest=delta-best['net_delta']),
        'controls':controls}
    for sym,names in [('JD0',x.JD),('SR0',x.SR)]:
        summary['products'][sym]={}
        for name in names:
            zs=[fixed[c+'/'+sym+'/'+name] for c in cases]
            summary['products'][sym][name]=dict(primary=fixed[primary+'/'+sym+'/'+name],
                min8_known_net=min(z['net'] for z in zs),min8_normalized=min(z['normalized_net'] for z in zs),
                max8_unknown=max(z['unknown'] for z in zs),
                test_min8_known_net=min(z['test']['net'] for z in zs),
                test_min8_normalized=min(z['test']['normalized_net'] for z in zs))
    for case,z in wf.items():
        summary['walkforward'][case]=dict(net=z['net'],net_delta=z['net']-z['B5_test']['net'],
            normalized=z['normalized_net'],normalized_delta=z['normalized_net']-z['B5_test']['normalized_net'],
            unknown=z['unknown'],JD=z['products']['JD0'])
    chemistry={}
    for sym in ['EG0','V0']:
        allrows=[z['row'] for z in saved if z['product']==sym and z['model']=='walkforward']
        chemistry[sym]=x.metric(allrows,{})
    summary['unchanged_chemistry']=chemistry
    half=[]
    for name in x.JD:
        z=fixed[primary+'/JD0/'+name]
        half.append(dict(variant=name,full2026_cash_ratio_to_EG=z['net']/chemistry['EG0']['net'],
            full2026_normalized_ratio_to_EG=z['normalized_net']/chemistry['EG0']['normalized_net'],
            AprOct_cash_ratio=z['test']['net']/wf[primary]['products']['EG0']['net'],
            AprOct_normalized_ratio=z['test']['normalized_net']/wf[primary]['products']['EG0']['normalized_net']))
    summary['half_EG_evaluation']=half
    write('comparison-summary.json',summary)
    lines=['# 鸡蛋退出约束与白糖独立机制：有限候选研究',
        '', '日期：2026-10-26（北京时间）。新分钟回放仅使用已保存的2026年行情。EG/PVC完整B5未改，生产文件未改。',
        '', '## 结论', '',
        '鸡蛋的主要可改善环节是目标空间：恢复原S1会改变成本/盈亏比资格并放进更多机会。延长最长持仓60→120桶没有增加本轮已接受收益；完全取消无跟随的额外改善集中在一笔交易。固定候选有改善，但按季度前推选出的组合在更高滑点/延迟下落后原B5，因此暂不足以将新分支升级为正式规则。',
        '', '白糖的15分钟失败突破回归、完整小时趋势突破均未通过；失败回归在Q1盈利、Q2/Q3转亏，小时突破整体亏损。结果只否定这两个固定实现，不能推成所有反转/慢趋势均无效。',
        '', '## 数据、计分与约束', '',
        '- 信号计分：2026-01-01至06-30，以及近期保存段至10-23 11:05；早期原始识别行情有2025-12热身和7月退出尾段。近期EG/PVC/SR从7月14日附近开始、JD从8月10日开始。缺失的7月初及JD7月至8月初不是无交易/零收益。不同品种覆盖不齐。',
        '- 原始实际合约分钟执行，冻结费用；金额为各一手、扣费用及滑点后的已知平仓净额。分数手300元计划风险归一值为研究口径，不是账户收益、可直接下单手数或统一累计风险占用。',
        '- 2025原始分钟缺失，没有补造旧年新退出结果。现有历史已经多轮研究、候选包含此前见过的S1/E20S1，前推验证并非未触碰样本。',
        '- 主场景为corrected_touch/slip1；两种成交模式×两档滑点×两种未知占用政策共8项敏感性。它们共享行情，不能当8份独立证据。所有本研究已接受组合的未知数量均另列。',
        '- 保留同品种占用、严格先平仓才释放、跨月/跨组不断档和B5组合控制。SR新信号没有ABC的A簇含义，使用每个信号独立标识；其相同A簇两止损限制不会产生虚构的ABC过滤。',
        '', '## 鸡蛋：目标空间比简单延长时间更重要', '',
        '|固定规则|2026平仓数|主场景净额/元|8场景最低净额|300风险归一净额|8场景最低归一|',
        '|---|---:|---:|---:|---:|---:|']
    for name in x.JD:
        z=summary['products']['JD0'][name];q=z['primary']
        lines.append(f"|{x.LABELS[name]}|{q['n']}|{fmt(q['net'])}|{fmt(z['min8_known_net'])}|{fmt(q['normalized_net'])}|{fmt(z['min8_normalized'])}|")
    lines += ['', 'S1与B5共享的8笔平仓，执行净额只改善119.98元；另外增加10笔净额797.94元，移除1笔亏损332.89元，合计改善1,250.81元。因同品种占用会重新竞争，移除一笔不等于事前能够识别该亏损。不能把总改善全部解释为原有交易拿得更久。',
        '', 'JD同一组220条候选中，主场景几何过滤仍是116条；原B5成本空间过滤75条，S1降为62条，个别净盈亏比/触发结果也改变。说明目标截近确实影响可交易空间，但单改目标仍未解决全部频率问题。漏斗是逐机会执行结果，独立closed数与竞争后组合平仓数不同。',
        '', 'S1的60→120个15分钟桶与S1在全部场景的接受收益一致：本轮接受交易多先由目标、止损、无跟随、周末或节假日结束，最长时限没有成为收益瓶颈。JD的60桶约为15个观察交易小时，远长于一个日间交易日。',
        '', f"取消无跟随并延至120桶，相比S1多{fmt(delta)}元；最大单笔改变贡献{fmt(best['net_delta'])}元，去掉这项改变后其余净影响{fmt(delta-best['net_delta'])}元。该关键预警为{best['warning_ts']}，原无跟随退出{fmt(best['old']['net_amount'])}元，放宽后目标退出{fmt(best['new']['net_amount'])}元。其余改变也包含无跟随小亏转成完整止损，不能据此直接取消保护。",
        '', '|规则|Q1训练净额|Q2净额|Q3净额|10月部分净额|4—10月主净额|4—10月最低场景净额|',
        '|---|---:|---:|---:|---:|---:|---:|']
    for name in x.JD:
        z=summary['products']['JD0'][name];q=z['primary'];bs=q['blocks']
        lines.append('|'+x.LABELS[name]+'|'+'|'.join(fmt(bs[b]['net']) for b in ['Q1_train','Q2','Q3','October_partial'])+f"|{fmt(q['test']['net'])}|{fmt(z['test_min8_known_net'])}|")
    lines += ['', '表中静态S1_N8、E20S1和PATIENT在后续固定计分段表现改善，但这些不是由Q1就已选出的规则。尤其JD只有2笔S1类Q1平仓，不足以在4月开始前选出它们。报告保留此区别。',
        '', '## 白糖：两条机制均未取得可迁移改善', '',
        '|规则|平仓数|主场景净额|8场景最低净额|Q1净额|Q2净额|Q3净额|10月部分|',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for name in x.SR:
        z=summary['products']['SR0'][name];q=z['primary'];bs=q['blocks']
        lines.append(f"|{x.LABELS[name]}|{q['n']}|{fmt(q['net'])}|{fmt(z['min8_known_net'])}|"+'|'.join(fmt(bs[b]['net']) for b in ['Q1_train','Q2','Q3','October_partial'])+'|')
    lines += ['', '失败突破规则固定为：当根15分钟柱越过此前20柱区间一侧至少1tick后收回区间，向反方向突破该柱极值1tick才触发，另一极值外1tick止损，目标为旧区间中点。双侧扫过的柱不交易，挂单最长4桶；原风险、费用、净盈亏比门槛保留。307条信号中26条目标已落在触发位后方，均由经济门槛排除，没有事后改成更远目标。307条中主场景200条因成本空间被拒、72条因几何被拒，仅15笔最终平仓；9笔止损、2笔目标退出。回归空间不足与后续再次延伸均在限制这个实现。',
        '', '小时趋势只用12根连续唯一5分钟柱构成的完整小时，忽略不完整时段；最近20个完整小时为通道，1ATR止损、2R目标。它不是15分钟ABC改一个参数：没有ABC形态和小时逆向自适应，等待1小时、无跟随5小时、最长60观察交易小时。73条候选，独立执行有39条closed，竞争后29笔；其中16笔止损、4笔目标退出。放慢周期、扩大单笔空间并未自动产生正期望。',
        '', '两个实现的已接受未知平仓数在8场景均为0。失败回归Q1虽盈利742元，但只有4笔、删除最大归一盈利后训练不再为正，并在后续组亏损；本轮预先规定的训练门槛阻止了采纳。不能只截取这一季支持“白糖适合反转”。',
        '', '## 按季度前推：主场景改善，压力条件下未过关', '',
        '每个截止日仅使用此前已平仓的影子结果；要求至少8笔训练平仓、最差场景正、删除最大盈利仍正、所有场景不低于B5且未知占用不增加。按最差归一收益减回撤惩罚选一个规则，没通过则B5；不是在测试组结束后回头选最好的曲线。',
        '', '|后续组|鸡蛋选定规则|白糖选定规则|', '|---|---|---|']
    for f in selection['schedule']:
        lines.append(f"|{f['start'][:7]}起|{x.LABELS[f['choices']['JD0']]}|{x.LABELS[f['choices']['SR0']]}|")
    lines += ['', '|执行情景（original政策）|组合前推净额|原B5同段净额|增量|归一增量|鸡蛋前推净额|',
        '|---|---:|---:|---:|---:|---:|']
    for case,z in wf.items():
        if not case.startswith('original/'):continue
        lines.append(f"|{case.removeprefix('original/')}|{fmt(z['net'])}|{fmt(z['B5_test']['net'])}|{fmt(z['net']-z['B5_test']['net'])}|{fmt(z['normalized_net']-z['B5_test']['normalized_net'])}|{fmt(z['products']['JD0']['net'])}|")
    lines += ['', '组合主场景增加736.23元，但其他三项成交压力下增量为负。鸡蛋前推13笔、主场景826.35元，归一后删除最大盈利变为-96.03元；高滑点下鸡蛋本身转亏。组合保持正净额主要仍依靠保留的EG/PVC，不能把组合为正当作新鸡蛋规则通过。两种未知占用政策结果相同，本轮没有额外证据因此翻倍。',
        '', '## 距离EG一半还有多远', '',
        f"同一2026已覆盖数据，EG完整B5一手净额{fmt(chemistry['EG0']['net'])}元，其一半{fmt(chemistry['EG0']['net']/2)}元；计划风险归一值{fmt(chemistry['EG0']['normalized_net'])}，一半{fmt(chemistry['EG0']['normalized_net']/2)}。",
        '', '|鸡蛋静态规则|一手现金/EG|同计划风险/EG|4—10月同风险/EG|', '|---|---:|---:|---:|']
    for z in half:
        lines.append(f"|{x.LABELS[z['variant']]}|{100*z['full2026_cash_ratio_to_EG']:.1f}%|{100*z['full2026_normalized_ratio_to_EG']:.1f}%|{100*z['AprOct_normalized_ratio']:.1f}%|")
    lines += ['', '某个局部计分段、主场景的同风险收益比例达到一半，不代表跨组/全部执行场景达标，也未统一各品种累计风险占用与历史覆盖。以本轮事后高收益候选直接替换再宣称目标实现，会重复历史选优偏差。',
        '', '## 决策与下一轮边界', '',
        '1. EG/PVC继续保留完整B5；本轮没有支持修改它们的证据。',
        '2. 鸡蛋保留原S1及E20S1作为目标空间观察分支，S1_N8作为有时间前推记录的挑战者；不立即替换。取消无跟随的增量过度依赖单次路径，最长时限扩展无效。先在新到数据中比较，而不是继续扫第6/7/9桶直到旧历史更漂亮。',
        '3. 白糖两条新分支本轮判失败，不做参数微调救曲线。原B5也尚未证明有稳定收益优势；若仍研究白糖，先用更长、跨年度且实际换月一致的数据审计可交易的费用后空间与行情状态，再预先固定新机制。不能以必须达到EG一半为由强行增加交易。',
        '4. 下一批真实新增行情到来前，这些均是研究观察规则。后续验收同时看独立时间组、执行压力、删最大赢家、未知占用和成本后的增量；目前证据不够承诺泛化或每品种半数收益。',
        '', '## 复现与核验', '',
        f"本轮原生新回放6,800条（早期4,940、近期1,860）。4项Python机制测试和4项原生边界测试通过；{READ(RUN/'verification.json')['exact_native_B5_JD_rows']}条JD完整B5执行与旧结果逐字段一致；另{controls['exact_previously_seen_S1_E20S1_native_rows']}条S1/E20S1对照与此前冻结结果一致。原始输入hash、复制库文件、EG/PVC接受行和生产文件核验未变。",
        '', '[protocol.json](protocol.json)固定全部候选与选择规则；[comparison-summary.json](comparison-summary.json)含全部8场景和目标比例；[selection-before-test.json](selection-before-test.json)保存各截止日训练信息及拒绝理由；[attribution.json](attribution.json)分解共同机会、增加和移除；[verification.json](verification.json)及[final-verification.json](final-verification.json)记录核验。',
        '', '![各产品固定规则和前推压力结果](comparison.png)', '']
    (RUN/'summary.md').write_text('\n'.join(lines),encoding='utf-8')
    chart(fixed,wf,primary)
    # Reconcile all product/model/month cells, independently of plotting.
    monthly=0
    for z in fixed.values():
        assert abs(sum(z['month_cash'].values())-z['net'])<1e-7
        assert abs(sum(z['warning_month_normalized_net'].values())-z['normalized_net'])<1e-7
        monthly+=1
    native=sum(READ(RUN/q/t/'executions/manifest.json')['evaluations'] for q in ['early','recent'] for t in ['JD','FAIL20','HOUR20'])
    assert native==6800
    write('final-verification.json',dict(**controls,monthly_cells=monthly,native_evaluations=native,
        report_source_sha256=x.s.m.old.sha(__file__),research_source_sha256=x.s.m.old.sha(x.__file__),
        python_tests_passed=4,native_tests_passed=4,static_candidates_not_called_unseen_OOS=True))
    names=['protocol.json','binary.json','generation-checks.json','signal-metadata.json','fixed-results.json',
           'walkforward-results.json','selection-before-test.json','attribution.json','accepted-primary.jsonl',
           'comparison-summary.json','summary.md','comparison.png','verification.json','final-verification.json']
    write('completion.json',dict(completed_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
        status='completed_available_2026_data',production_changed=False,qualified_for_trading=False,
        artifacts={n:x.s.m.old.sha(RUN/n) for n in names},missing='No new2025 exit replay: raw minute archives unavailable.',
        conclusion='JD target-space hypothesis supported historically, forward selection not robust to execution. Both frozen SR mechanisms failed.'))
    print('Report, chart and independent controls complete.',flush=True)

def chart(fixed,wf,primary):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams['font.sans-serif']=['Microsoft YaHei','SimHei','DejaVu Sans']
    plt.rcParams['axes.unicode_minus']=False
    fig,axs=plt.subplots(2,2,figsize=(15,9.5),layout='constrained')
    ax=axs[0,0];labels=['B5','S1','20%+S1','S1+N8','S1+H120','S1+耐心']
    vals=[fixed[primary+'/JD0/'+n]['net'] for n in x.JD]
    worst=[min(fixed[c+'/JD0/'+n]['net'] for c in wf) for n in x.JD]
    ticks=np.arange(len(labels));ax.bar(ticks-.18,vals,.36,label='主场景');ax.bar(ticks+.18,worst,.36,label='8场景最低')
    ax.set_xticks(ticks,labels,rotation=15);ax.set_title('鸡蛋：固定规则一手已知平仓净额');ax.set_ylabel('元');ax.legend();ax.axhline(0,color='#777',lw=.8)
    ax=axs[0,1];bs=['Q1_train','Q2','Q3','October_partial'];ticks=np.arange(4)
    for i,n in enumerate(x.SR):
        vals=[fixed[primary+'/SR0/'+n]['blocks'][b]['net'] for b in bs]
        ax.bar(ticks+(i-1)*.25,vals,.25,label=x.LABELS[n])
    ax.set_xticks(ticks,['Q1训练','Q2','Q3','10月部分']);ax.set_title('白糖：两条新机制未能跨组改善');ax.set_ylabel('元');ax.legend(fontsize=9);ax.axhline(0,color='#777',lw=.8)
    ax=axs[1,0];mo=x.MONTHS
    for n in ['B5','S1','E20S1','S1_PATIENT']:
        z=fixed[primary+'/JD0/'+n];ys=[];v=0.
        for m in mo:v+=z['warning_month_normalized_net'][m];ys.append(v)
        ax.plot(range(len(mo)),ys,marker='o',ms=3,label=x.LABELS[n])
    ax.set_xticks(range(len(mo)),[m[5:] for m in mo]);ax.set_title('鸡蛋：按预警月累计的300计划风险归一净额');ax.set_xlabel('2026年月份（覆盖不齐；非账户净值）');ax.legend(fontsize=9);ax.axhline(0,color='#777',lw=.8)
    ax=axs[1,1];cs=[c for c in wf if c.startswith('original/')]
    vals=[wf[c]['net']-wf[c]['B5_test']['net'] for c in cs]
    ax.bar(range(4),vals,color=['#27816b' if v>=0 else '#b95454' for v in vals]);ax.set_xticks(range(4),['触价1tick','触价2tick','延迟1tick','延迟2tick'])
    ax.set_title('4—10月前推组合相对B5的增量');ax.set_ylabel('元');ax.axhline(0,color='#777',lw=.8)
    for ax in axs.flat:ax.grid(axis='y',alpha=.2);ax.set_axisbelow(True)
    fig.suptitle('有限候选研究｜EG/PVC保持B5｜仅已有2026行情，无独立新样本',fontsize=16)
    fig.savefig(RUN/'comparison.png',dpi=160);plt.close(fig)

if __name__=='__main__':run()
