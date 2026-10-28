"""Freeze two economics candidates and deliver all shape/cost failures and audits."""
import copy,json,shutil
import research_swing_entry_economics as r
import research_swing_entry_combo as combo
import verify_swing_entry_economics_native as native
w=r.w;RUN=r.RUN
SF=['corrected_touch/slip1','corrected_touch/slip2','next_minute_open/slip1','next_minute_open/slip2']
SNAP=w.REPO/'research-strategies/best-a-entry-economics-observation-v1'

def run():
    res=w.read(RUN/'results.json');inter=w.read(combo.RUN/'results.json');a=w.read(RUN/'attribution.json');ca=w.read(combo.RUN/'attribution.json');score=w.read(RUN/'scorecard.json');score += [z for z in w.read(combo.RUN/'scorecard.json') if z['model']=='budget_body'];score.sort(key=lambda z:z['total'],reverse=True)
    assert len(score)==20 and score[0]['name']=='unmerged/budget_body';v=w.read(RUN/'verification.json');iv=w.read(combo.RUN/'verification.json');nv=w.read(native.RUN/'verification.json');assert nv['all_exact'];assert len(nv['metric_replays'])==96
    selected={'budget10':('unmerged/budget10',res,a),'budget_body':('unmerged/budget_body',inter,ca)}
    for profile,(name,stats,_) in selected.items():
        for pol in ['original','two_paths_flat']:
            for sf in SF:assert stats[pol]['combined'][name+'/'+sf]['net']>res[pol]['combined']['unmerged/base/'+sf]['net']
    w.write(RUN/'integrated-scorecard.json',score)
    SNAP.mkdir(exist_ok=False);(SNAP/'bin').mkdir();shutil.copytree(native.RUN/'fork',SNAP/'execution-fork');shutil.copytree(w.RUN/'unmerged_extension/fork',SNAP/'generation-fork')
    shutil.copy2(w.REPO/'target/release/swing_entry_economics_execution.exe',SNAP/'bin/swing_entry_economics_execution.exe');shutil.copy2(w.REPO/'scripts/swing_entry_economics.py',SNAP/'warning-feature-transform.py')
    for q in ['early','recent']:shutil.copy2(native.RUN/(q+'-pool.json'),SNAP/(q+'-pool.json'))
    native_p=w.read(native.RUN/'protocol.json');defs={}
    for profile,(name,stats,attrs) in selected.items():
        param=copy.deepcopy(native_p);t=next(t for t in param['templates'] if t['variant']==profile);param.update(id='best-a-unmerged-'+profile+'-observation-v1',display_name='最佳a不合并摆点分支：'+('空间成本过滤' if profile=='budget10' else '空间成本过滤及A实体方向'),templates=[t],selector=4,
            warning_generation='Use generation-fork with selector4 (fine2/large5,merge_gap0), not the inherited pool stage in execution-fork. The supplied execution binary is used ONLY in replay mode; its A-selector field is not consulted during execution.',
            feature_generation='Attach entry_quality from warning-feature-transform.py.warning_features to each unmerged warning; only prewarning closed bars. Seeded research pools included. Live integration is not implemented.')
        w.write(SNAP/(profile+'.protocol.json'),param);m=stats['original']['combined'][name+'/'+SF[0]];early=stats['original']['early'][name+'/'+SF[0]];recent=stats['original']['recent'][name+'/'+SF[0]]
        defs[profile]=dict(id=param['id'],status='frozen_research_observation',source_family='unmerged/adaptive',model=profile,
            rule='At warning-time, planned nominal roundtrip cost <=10% of planned gross target space. Planning cost uses original fixed1tick-per-side model and conservative close-fee estimate, not future realized friction.',
            additional='Directional sum of A real bodies excludingS0 >0.' if profile=='budget_body' else 'No additional A/body/gap/speed filter.',
            execution='Original entry,stop,absolute local target,fee model,size and adaptive hourly execution retained. Veto before order simulation and re-run all opportunity competition; no new profit protection or A-breakout condition.',
            results=dict(early_net=early['net'],recent_net=recent['net'],combined_net=m['net'],drawdown=m['realized_cash_drawdown'],worst_month=m['worst_warning_month'],negative_month_burden=m['negative_warning_month_burden'],product_hhi=m['positive_product_hhi'],top3_positive_product=m['top3_product'],unknown=m['unknown']),
            development=True,independent_validation=False,paper_collection_started=False,production_changed=False,qualified_for_trading=False)
        w.write(SNAP/(profile+'.definition.json'),defs[profile])
    readme=['# 空间成本过滤：两个固定研究候选','',
        '最佳a原版best-a-v1保持不变。此快照使用已验证的不合并摆点A分支，另加入场经济性条件；均为反复研究历史上的观察候选，没有实盘资格。',
        '', '- budget10：计划往返成本不超过计划毛目标空间的10%。侧重较简单规则与较小月度尾损。',
        '- budget_body：同一成本条件，再要求A段排除S0柱后的方向实体净推进>0。开发期总收益较高，但品种贡献更集中。',
        '', '两者都保持原入场、止损、绝对目标和一手仓位，不增加A突破条件，不改变持仓后的退出规则。',
        '', '执行示例：bin/swing_entry_economics_execution.exe budget10.protocol.json early replay 新输出目录。程序参数使用完整路径，replay数据从参数文件同级early-pool.json/recent-pool.json读取。',
        '', '本快照的二进制用于replay。新增行情先用generation-fork及selector4提取unmerged预警，再用warning-feature-transform.py和同口径15m聚合补充entry_quality；execution-fork包含用于可复现编译的执行源码。未实现实盘/在线接入。',
        '', '当前成本采用与原版一致的既定费率表和每边1tick名义滑点，压力测试改变真实模拟成交滑点与延迟，但不事后改写警报时的成本预测。新的真实成本资料需要重新验证，不能把本研究等同逐报价成交验证。']
    (SNAP/'README.md').write_text('\n'.join(readme)+'\n',encoding='utf-8');w.write(SNAP/'manifest.json',{f.relative_to(SNAP).as_posix():w.sha(f) for f in SNAP.rglob('*') if f.is_file()})
    decision=dict(status='two_fixed_research_candidates_frozen',recommended_simple_candidate=defs['budget10']['id'],highest_primary_profit_candidate=defs['budget_body']['id'],reason='Both increase total vsboth A families under all4 stresses and both ambiguity policies. Simple budget has lower DD,negative-month burden,worst month and product concentration than added-body interaction; highest-profit alternative explicitly preserved.',
        main_new_models=16,additional_exploratory_interactions=2,controls_not_independent=True,baseline_best_a_preserved=True,recent80_required=False,full_cycle_stability_reached=False,
        unresolved=['Early still losing in all4 stresses','Strongest cost/latency total still negative','Product dependence not solved; added-body version concentrates further','Warning-week intervals cross0 without multiple-search adjustment','Historical minute data/full quote execution and fee history not independently revalidated'],production_changed=False,qualified_for_trading=False)
    w.write(RUN/'decision.json',decision);w.write(RUN/'candidate-definitions.json',defs)
    base=res['original']['combined']['unmerged/base/'+SF[0]];economic=res['original']['combined']['unmerged/budget10/'+SF[0]];profit=inter['original']['combined']['unmerged/budget_body/'+SF[0]];best=res['original']['combined']['best_a/base/'+SF[0]]
    names={'base':'原执行','budget10':'空间成本≤10%','body_net':'A净实体同向','no_gap_majority':'A非跳空主导','no_bar_majority':'A非单根实体主导','orderly_B':'B速度不快于A','clean_A':'三项A质量并用','budget_orderly':'成本＋B速度','budget_clean':'成本＋三项A质量','budget_body':'成本＋A净实体同向'}
    lines=['# 新方向：先检验小波段是否有足够的净交易空间','',
        '本轮将重点从等待确认/提前退出转到警报当时的机会经济性与推动质量。目标沿用用户要求：两期合计已知净收益提高，分布次之，不设近期80%保留门槛。最佳a原版未覆盖、生产未改。',
        '', '结论：可保留两个固定研究候选。空间成本条件在两个A版本、四种成本/延迟场景、两种未知敞口口径下均提高合计收益。追加A净实体同向后开发期收益进一步提高，但月度尾损和品种集中度不如单成本条件；因此同时保存简单候选与收益较高的对照。',
        '', '## 固定规则','',
        '1. 警报时估算往返成本（沿用原版每边1tick名义滑点和既定费率模型），要求不超过计划毛目标空间的10%。空间取原计划目标−原入场价的方向差，不以实际后续填单、MFE、盈亏或未来费用作判断。',
        '2. 可选附加条件：A段排除S0转折柱后，方向实体净推进>0。S0柱可能包含上一腿，故不把其整根实体硬算进新推动。此条件没有要求A突破此前高低点。',
        '', '所有原入场、原止损、绝对目标、仓位、小时环境执行和退出规则保持原样。不是放大止损来降低成本比例；成本预算针对毛收益空间，单纯改变止损宽度不会改变同一计划的成本/空间比例。',
        '', '## 每边1tick、统一费率模型，单手已知平仓金额','', '|版本|上半年净额|近期净额|合计|合计回撤|','|---|---:|---:|---:|---:|']
    for label,name,stats in [('最佳a原版','best_a/base',res),('已有不合并摆点分支','unmerged/base',res),('不合并摆点＋空间成本过滤','unmerged/budget10',res),('再加A净实体同向','unmerged/budget_body',inter)]:
        e=stats['original']['early'][name+'/'+SF[0]];n=stats['original']['recent'][name+'/'+SF[0]];m=stats['original']['combined'][name+'/'+SF[0]];lines.append(f"|{label}|{e['net']:.2f}|{n['net']:.2f}|{m['net']:.2f}|{m['realized_cash_drawdown']:.2f}|")
    lines+=['', f"单成本条件相对已有不合并摆点分支增加{economic['net']-base['net']:.2f}元；叠加实体条件增加{profit['net']-base['net']:.2f}元。相对最初最佳a分别增加{economic['net']-best['net']:.2f}/{profit['net']-best['net']:.2f}元。未知收益均另列，主场景都是1笔；近期不同品种起点不齐，不能称完整等覆盖四个月。",
        '', '## 分布改善与尚未解决的集中','', '|指标|已有不合并摆点|单成本条件|成本＋实体|','|---|---:|---:|---:|']
    for key,label,fmt in [('negative_warning_month_burden','亏损预警月份亏损合计','.2f'),('worst_warning_month','最差预警月份','.2f'),('positive_warning_months','正收益预警月份数','d'),('top3_product','前三盈利品种占正贡献比','.2%'),('positive_product_hhi','正贡献品种HHI','.4f')]:lines.append('|'+label+'|'+'|'.join(format(z[key],fmt) for z in (base,economic,profit))+'|')
    lines+=['', '单成本条件改善了最差月份、亏损月份负担与回撤，但2月、5月、6月的净额反而变差，不能说每个月都更稳定。品种集中度没有改善；组合版进一步集中。HHI是盈利贡献集中度，不代表真实相关性风险已经分散。',
        '', '## 四种压力：合计净额','', '|场景|已有不合并摆点|单成本条件|成本＋实体|','|---|---:|---:|---:|']
    for sf in SF:lines.append(f"|{sf}|{res['original']['combined']['unmerged/base/'+sf]['net']:.2f}|{res['original']['combined']['unmerged/budget10/'+sf]['net']:.2f}|{inter['original']['combined']['unmerged/budget_body/'+sf]['net']:.2f}|")
    lines+=['', '两种未知占用口径下增益方向相同。最严压力下总额仍负，上半年也仍负；还不能称全周期稳定。next_minute_open是额外一分钟延迟，不是真实下一报价。',
        '', '## 增益从哪里来','',
        '单成本条件在主场景中剔除原已知48笔（上半年37、近期11），这批原净亏1699.26元；573笔共同成交的执行和费用完全不变，完整重算竞争后没有新增已平仓交易。不是事后只删亏单，筛选只读取警报和计划字段，所有原潜在机会均参与重算。',
        '', '固定这48笔作成本拆分：扣回显式模型滑点和手续费后的现金为−1699.26元；把204.26元手续费和1620元显式滑点加回，仅为+125元。该固定成交组本身的价格差优势很薄。此拆分不重现零成本下的填单、保护或竞争，不能当作可交易的无成本回测。近期这批11笔即使加回显式成本也为−430元，故不能把所有问题都归结为成本。',
        '', '单成本增益的主要贡献为SH约+1140、SF约+560、SS约+472元，CF约−854、JD约−448元抵消部分收益。剔除任意品种后的结果保留；去掉SH后增益仍正，但去掉SH和SF后几乎消失，改善不是广泛均匀发生。',
        '', 'A净实体条件与成本条件并用后，原72笔不再纳入、原净亏2301.66元，名额重竞争另增加1笔−77元，净增2224.66元；549笔共同交易的成交收益仍完全相同。',
        '', '## 其余假设全部保留','', '|A版本 / 条件|上半年|近期|合计|相对该A对照|','|---|---:|---:|---:|---:|']
    for z in score:lines.append(f"|{z['family']} / {names[z['model']]}|{z['early']:.2f}|{z['recent']:.2f}|{z['total']:.2f}|{z['delta_within_family']:+.2f}|")
    lines+=['', '“B必须更慢”“A不能由跳空或一根大K贡献多数”等形态直觉，单独或组合后大多降低总收益，本轮不加入。不能因为看起来符合健康回调就当成硬门槛。',
        '', '## 已知净额按预警月份','', '|月份|已有不合并摆点|单成本条件|成本＋实体|','|---|---:|---:|---:|']
    for month,val in base['warning_month_net'].items():lines.append(f"|{month}|{val:.2f}|{economic['warning_month_net'][month]:.2f}|{profit['warning_month_net'][month]:.2f}|")
    lines+=['', '## 冻结与证据边界','',
        '主实验8个规则×2个A版本=16个新组合，随后根据主结果只追加“成本＋A净实体”一个交互×2版本=2个新组合。后者是探索性追加，不称预先独立验证。全部失败结果保留，没有扫描10%附近的阈值、没有逐品种/月选参。两个A版本共用大量相同交易，不能当两份独立样本。',
        '', '主实验48个时期/成本/占用对照复现归档；交互中的192个重复统计与主实验一致。14405条警报的名义成本及警报前局部目标独立复核，43215次额外压力场景检查证实同一警报的计划不随未来模拟成交变化。',
        '', '随后在独立研究副本中把两个守卫接入原生Rust执行器，完整重放86880条机会/场景结果；41452条放行后的完整个体结果与原执行一致，45428条拒绝在成交或占用之前完成，96个组合统计与离线筛选重竞争一致。该核验是实现一致性，不是外部行情或新时期验证。7项Python测试及2项Rust守卫测试通过。',
        '', '单成本增益的描述性预警周95%差额区间约[−1780.70,+5725.16]；组合约[−3759.83,+8679.80]。均跨零，且未校正此前反复研究和本轮多重选择，不能称统计确认的泛化改善。',
        '', '已冻结两个研究候选于research-strategies/best-a-entry-economics-observation-v1。以较简单的单成本版作均衡观察，与收益较高的组合版及最佳a原版一起等待新时期记录；本轮没有启动纸面采集或实盘。最佳a快照153个文件和生产169个文件哈希均未变。',
        '', '仍需独立分钟行情核验、真实盘口/下一报价及实际成本验证，且本轮沿用统一费率模型，未另行验证历史逐日费率。已实现现金回撤不等于包含浮盈亏的账户权益回撤。']
    (RUN/'summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    full=['# 全场景净额与分布','']
    for pol in ['original','two_paths_flat']:
        full+=['## '+pol,'','|A版本/条件|场景|上半年|近期|合计|未知|','|---|---|---:|---:|---:|---:|']
        for z in score:
            stats=inter if z['model']=='budget_body' else res
            for sf in SF:
                key=z['name']+'/'+sf;m=stats[pol]['combined'][key];full.append(f"|{z['name']}|{sf}|{stats[pol]['early'][key]['net']:.2f}|{stats[pol]['recent'][key]['net']:.2f}|{m['net']:.2f}|{m['unknown']}|")
        full+=['']
    (RUN/'report.md').write_text('\n'.join(full),encoding='utf-8')
    w.write(w.REPO/'research-protocols/best-a-entry-economics-observations-20261026-v1.json',dict(snapshot=str(SNAP),snapshot_manifest_sha256=w.sha(SNAP/'manifest.json'),candidates={k:z['id'] for k,z in defs.items()},highest_total='budget_body',simpler_balance_observation='budget10',named_best_a_unchanged=True,production_changed=False,qualified_for_trading=False))
    assert all(w.sha(r.SNAP/k)==v for k,v in w.read(r.SNAP/'manifest.json').items());p=w.read(RUN/'protocol.json');assert all(w.sha(k)==v for k,v in p['production_before'].items());assert all(w.sha(native.RUN/'fork'/k)==v for k,v in w.read(native.RUN/'implementation-manifest.json').items())
    artifacts=['summary.md','report.md','protocol.json','integrated-scorecard.json','decision.json','candidate-definitions.json','results.json','scorecard.json','evaluation.json','attribution.json','verification.json','economic-implementation-audit.json','cost-mechanism-diagnosis.json','test-verification.json','body-interaction/protocol.json','body-interaction/results.json','body-interaction/scorecard.json','body-interaction/verification.json','body-interaction/repeated-control-verification.json','native-verification/protocol.json','native-verification/verification.json','native-verification/built-binaries.json']
    sources=['scripts/swing_entry_economics.py','scripts/research_swing_entry_economics.py','scripts/report_swing_entry_economics.py','scripts/audit_swing_entry_economics.py','scripts/research_swing_entry_combo.py','scripts/report_swing_entry_combo.py','scripts/verify_swing_entry_economics_native.py','scripts/check_swing_entry_economics_native.py','scripts/finish_swing_entry_economics.py','tests/test_swing_entry_economics.py']
    w.write(RUN/'completion.json',dict(status='complete_two_new_research_candidates_frozen',main_new_combinations=16,additional_new_interactions=2,baseline_controls=48,native_metric_checks=96,native_records=86880,best_a_preserved=True,production_unchanged=169,independent_validation=False,qualified_for_trading=False,candidate_snapshot=str(SNAP),candidate_manifest_sha256=w.sha(SNAP/'manifest.json'),artifacts={a:w.sha(RUN/a) for a in artifacts},sources={s:w.sha(w.REPO/s) for s in sources}))
    print('Complete: budget10 total16043.306; budget+body total16568.7055; two candidates frozen; original最佳a and169 production files unchanged.',flush=True)

if __name__=='__main__':run()
