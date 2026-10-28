"""Close revised-objective round; preserve every failed experiment and old baseline."""
import copy,json,pathlib
import research_swing_c_structure as c
import research_swing_c_first_retest as first
import research_swing_total_payoff as pay
import research_swing_c_stall as stall
w=c.w;RUN=c.RUN;SF=['corrected_touch/slip1','corrected_touch/slip2','next_minute_open/slip1','next_minute_open/slip2']

def run():
    groups=[('C结构',c.RUN,c.PROFILES),('C首次回踩',first.RUN,first.PROFILES),('兑现与时限',pay.RUN,pay.PROFILES),('C未形成',stall.RUN,stall.PROFILES)];stats={};combined={pol:{} for pol in ('original','two_paths_flat')};rank=[];verified=[];base_metrics=None
    for label,root,profiles in groups:
        stats[label]=w.read(root/'results.json');score=w.read(root/'scorecard.json');verification=w.read(root/'verification.json');verified.append(verification)
        for z in score:
            if z['profile']=='base':
                if base_metrics is None:base_metrics=z
                else:
                    for key in ('total','early','recent','drawdown','product_hhi','unknown'):assert abs(z[key]-base_metrics[key])<1e-7
                continue
            rank.append(dict(z,source_group=label,source=str(root),new_this_round=True))
        for pol in combined:
            for sf in SF:
                for profile in profiles:
                    if profile=='base' and pol in combined and 'base/'+sf in combined[pol]:continue
                    name='base' if profile=='base' else profile
                    combined[pol][name+'/'+sf]={q:stats[label][pol][q][profile+'/'+sf] for q in ('early','recent','combined')}
    rank.append(dict(base_metrics,source_group='固定对照',new_this_round=False,source=str(c.main.SNAP)))
    old=w.read(RUN/'previous-unmerged-new-objective.json');original=w.read(w.RUN/'results.json')
    for pol in combined:
        for sf in SF:
            combined[pol]['unmerged/'+sf]=dict(combined=old[pol][sf],**{q:original[pol][q]['unmerged/adaptive/'+sf] for q in ('early','recent')})
    m=old['original'][SF[0]];rank.append(dict(profile='unmerged',source_group='按新目标重新比较的旧分支',new_this_round=False,source=str(w.RUN/'unmerged'),total=m['net'],delta_vs_best_a=m['net']-base_metrics['total'],early=original['original']['early']['unmerged/adaptive/'+SF[0]]['net'],recent=original['original']['recent']['unmerged/adaptive/'+SF[0]]['net'],negative_month_burden=m['negative_warning_month_burden'],worst_month=m['worst_warning_month'],drawdown=m['realized_cash_drawdown'],product_hhi=m['positive_product_hhi'],top3_product=m['top3_product'],positive_months=m['positive_warning_months'],unknown=m['unknown'],primary_gain_both_policies=True,all_stress_gain_both_policies=True))
    rank.sort(key=lambda z:z['total'],reverse=True);assert len([z for z in rank if z['new_this_round']])==10;assert all(z['delta_vs_best_a']<=0 for z in rank if z['new_this_round']);assert rank[0]['profile']=='unmerged'
    candidate=w.read(RUN/'candidate/replay-verification.json');assert candidate['all_exact'];assert sum(z['raw_results_exact'] for z in candidate['checks'])==28960
    w.write(RUN/'integrated-scorecard.json',rank);w.write(RUN/'integrated-results.json',combined)
    cp=w.read(RUN/'candidate/protocol.json');detail=dict(id='best-a-unmerged-observation-v1',status='weak_development_candidate_reselected_under_new_objective',reference='最佳a策略best-a-v1',definition='Keep confirmed finewindow2/largewindow5; merge_gap0 instead8/10. Latest eligibleA plus original adaptive execution; no new C/target/time rule from this round.',new_C_model_winner=False,source='Already-known unmerged/adaptive, re-ranked after user changed objective; not newly discovered improvement.',
        primary=dict(combined_net=m['net'],delta_vs_best_a=m['net']-base_metrics['total'],early=rank[0]['early'],recent=rank[0]['recent'],drawdown=m['realized_cash_drawdown'],top3_positive_product_share=m['top3_product']),
        explanation='Primary gain132 comes from one fewer early losing trade. Other3 stresses improve152/122/142. Negative-month burden and worst month unchanged; small product concentration/DD improvement is not evidence of full-cycle stability.',
        protocol_sha256=w.sha(RUN/'candidate/protocol.json'),replay_verification_sha256=w.sha(RUN/'candidate/replay-verification.json'),all_historical_data_researched=True,independent_holdout=False,paper_collection_started=False,production_changed=False,qualified_for_trading=False)
    w.write(RUN/'observation-candidate.json',detail)
    balance=next(z for z in rank if z['profile']=='stall5');w.write(RUN/'balance-observation.json',dict(id='best-a-stall5-balance-observation',status='near_neutral_diagnostic_only_not_profit_upgrade',total_net=balance['total'],delta=balance['delta_vs_best_a'],rule='At original fifth held bucket, gross loss AND first post-entry C structure never formed => next observed minute open exit. Do nothing new onceCformed.',reason='Five early original trades +271.967 cash,one recent original winner -280; total-8.033, no new entries. Small balance change is six-trade noise; no threshold tuning for8yuan.',source=str(stall.RUN),production_changed=False,qualified_for_trading=False))
    decision=dict(status='round_complete_no_new_rule_raises_primary_total_old_unmerged_marginally_best',objective='Combined known closed net primary, distribution secondary; recent80 NOT used',new_rules=10,selected_development_candidate=detail['id'],named_best_a_preserved=True,balance_observation=balance['profile'],full_cycle_stability_reached=False,
        limitations=['Old unmerged improvement is one trade,132yuan, not independent validation','Primary best-a and candidate remain negative in strongest cost/latency stress','All histories repeatedly researched; unknown1 kept outside totals','No account capital sizing, floating-equity or quote-level guarantee'],production_changed=False,qualified_for_trading=False)
    w.write(RUN/'decision.json',decision)
    names={'base':'最佳a原版','unmerged':'不合并摆点（已有分支）','hl_close':'C抬高低点破坏：收盘','hl_wick':'C抬高低点破坏：影线','push_close':'C再推进后破坏：收盘','push_wick':'C再推进后破坏：影线','first_close':'C首回踩破坏：收盘','first_wick':'C首回踩破坏：影线','cap2':'2R兑现上限','hold16':'16桶持仓上限','cap2_hold16':'2R＋16桶上限','stall5':'第5桶未形成C且浮亏'}
    baseline=combined['original']['base/'+SF[0]]['combined'];proposal=combined['original']['unmerged/'+SF[0]]['combined'];near=combined['original']['stall5/'+SF[0]]['combined']
    lines=['# 总收益优先：C结构、兑现与持仓分布验证','',
        '已按用户新要求取消近期80%收益保留条件。主指标是上半年＋现有近期窗口的单手已知平仓净收益；分月/品种/交易日分布与已实现现金回撤作为次指标，不再强制每期正收益。固定最佳a策略best-a-v1没有覆盖。',
        '', '结论：本轮新增10个规则均未提高主场景合计收益。按新目标重新比较并完整复现的已有unmerged/adaptive分支，比最佳a多132元、回撤少132元、品种集中度略降；它只是一个很弱的开发期观察候选，收益差仅来自少一笔亏损。不是本轮C规则带来的新突破。',
        '', '## 主场景：每边1tick并扣实际手续费','', '|规则|上半年净额|近期净额|合计净额|比最佳a|','|---|---:|---:|---:|---:|']
    for z in rank:lines.append(f"|{names[z['profile']]}|{z['early']:.2f}|{z['recent']:.2f}|{z['total']:.2f}|{z['delta_vs_best_a']:+.2f}|")
    lines+=['', '金额是原一手仓位、已知平仓现金，不是账户收益率。未知收益仍单列，主场景各对照均有1笔未知。两期不同长度、近期各品种历史起点不齐；此处按用户要求直接相加，不作年化，也不把各压力场景加总或平均当成额外收益。',
        '', '## 分布与回撤','', '|指标|最佳a原版|不合并摆点旧分支|未形成C观察版|','|---|---:|---:|---:|']
    for key,label,fmt in [('realized_cash_drawdown','已实现现金回撤','.2f'),('negative_warning_month_burden','亏损预警月份亏损合计','.2f'),('worst_warning_month','最差预警月份净额','.2f'),('positive_warning_months','正收益预警月份数','d'),('top3_product','前三盈利品种占正贡献比','.2%'),('positive_product_hhi','正贡献品种HHI','.4f')]:lines.append('|'+label+'|'+'|'.join(format(x[key],fmt) for x in (baseline,proposal,near))+'|')
    lines+=['', 'HHI/前三占比衡量盈利贡献集中度，不能代替品种相关性或账户风险分散。不合并摆点版的亏损月份负担、最差月份均没有改善；不能用132元提升声称跨期稳定。',
        '', '## 四种成本与延迟压力','', '|场景|最佳a合计|不合并摆点合计|增量|未形成C合计|','|---|---:|---:|---:|---:|']
    for sf in SF:
        b=combined['original']['base/'+sf]['combined']['net'];u=combined['original']['unmerged/'+sf]['combined']['net'];v=combined['original']['stall5/'+sf]['combined']['net'];lines.append(f'|{sf}|{b:.2f}|{u:.2f}|{u-b:+.2f}|{v:.2f}|')
    lines+=['', '两种未知占用口径下，旧不合并摆点分支相对增量方向一致。最严场景仍亏损，因此没有达到全周期稳定，也没有实盘资格。全部10新规则的四场景与两种占用数表见report.md和integrated-results.json。',
        '', '## 这轮识别到的机制','',
        '直接按已形成C的支撑破坏退出，保护的位置仍不对。以两次新C低点的收盘版为例，上半年86笔受影响原交易合计原赚7616.22元，改后只赚2009.46元；近期31笔原赚8245.36元，改后赚3353.69元。这组原机会在两期都具有正现金贡献，新增保护剪掉了本来有价值的波动空间。',
        '', '2R封顶、缩短到16个已观察15分钟持仓桶及组合也降低总收益。16桶约4个交易小时，不能当成固定4小时自然时间；原第五桶无跟随、日历和原止损仍优先。没有继续扫描止盈倍数或持仓长度。',
        '', '因此最后只检验“尚未形成C”的持仓：在原第五桶检查时，只有未确认新C且当前浮亏才次分钟退出，已形成C后完全沿用最佳a。它保留了所有622笔原已知交易、没有增加交易；上半年影响5笔（原4止损1止盈）少亏271.97元，近期影响1笔原止盈少赚280元，合计−8.03元。回撤、亏损月份负担与品种集中度仅微降，描述性预警周差额95%区间约[−699.99,+515.89]，未做多重研究校正，无法称确定改善。不会围绕8元差额继续调阈值。',
        '', '## 预警月份已知净额','', '|预警月份|最佳a|不合并摆点旧分支|未形成C观察版|','|---|---:|---:|---:|']
    for month,val in baseline['warning_month_net'].items():lines.append(f"|{month}|{val:.2f}|{proposal['warning_month_net'][month]:.2f}|{near['warning_month_net'][month]:.2f}|")
    lines+=['', '以上按预警月份归集，以免不同退出时间改变月份归因；实际平仓交易月也保存在各结果bins.month。表中为已知现金，未知笔数独立保留，不把未知收益记为0。',
        '', '## 实验顺序与核验','',
        '先冻结4个新C规则，再在查看其收益前增加2个首回踩语义对照；发现C退出组都降低收益后增加2R/16桶有限2×2兑现试验（3个新规则）；最后基于受影响原交易两期都盈利的归因，固定1个“未形成C”阶段保护。总计10个新规则，分阶段探索全部公开，不称为一次预先注册的独立检验。此前已研究的6个B转折规则也按新目标复核，主场景合计均低于最佳a。',
        '', 'C的确认只使用实际入场后完整且连续、正成交量的5分钟柱；破坏必须晚于确认，次个已观察分钟开盘退出。首回踩的B极值是当时已知参考，未伪装成入场后观察到的转折。各阶段独立核验结构确认、价格破坏与实际成交顺序，原进场、止损、仓位和费用不变。兑现试验使用最佳a冻结执行程序并检查2R目标实际毛R。',
        '', '四组对照共96个时期/成本/占用统计复现最佳a；重保留的旧不合并摆点候选以冻结程序重放，19644＋9316=28960条原始结果与归档逐条一致。7项C基础测试、首回踩扩展8项（包含前7项）和未形成C阶段3项测试通过。最佳a快照153个文件及生产169个文件哈希不变。',
        '', '原始数据独立全量分钟核验、真实下一报价/盘口、完整浮动权益与资本约束仍未补齐。所有历史已反复研究，没有新增独立验证、纸面采集或实盘接入。']
    (RUN/'summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    full=['# 全部模型压力数表','', '|规则|场景|上半年|近期|合计|增量|未知|','|---|---|---:|---:|---:|---:|---:|']
    for pol in combined:
        full+=['', '## '+pol,'', '|规则|场景|上半年|近期|合计|增量|未知|','|---|---|---:|---:|---:|---:|---:|']
        for z in rank:
            for sf in SF:
                val=combined[pol][z['profile']+'/'+sf];n=val['combined'];ref=combined[pol]['base/'+sf]['combined'];full.append(f"|{names[z['profile']]}|{sf}|{val['early']['net']:.2f}|{val['recent']['net']:.2f}|{n['net']:.2f}|{n['net']-ref['net']:+.2f}|{n['unknown']}|")
    (RUN/'report.md').write_text('\n'.join(full)+'\n',encoding='utf-8')
    w.write(RUN/'candidate/definition.json',detail);w.write(w.REPO/'research-protocols/best-a-unmerged-observation-20261025-v1.json',dict(id=detail['id'],status=detail['status'],candidate_protocol=str(RUN/'candidate/protocol.json'),definition=str(RUN/'candidate/definition.json'),definition_sha256=w.sha(RUN/'candidate/definition.json'),named_best_a_unchanged=True,production_changed=False,qualified_for_trading=False))
    c.main.verify_snapshot();p=w.read(RUN/'protocol.json');assert all(w.sha(k)==v for k,v in p['production_before'].items())
    for root in (c.RUN,first.RUN,stall.RUN):assert all(w.sha(root/'fork'/k)==v for k,v in w.read(root/'implementation-manifest.json').items())
    artifacts=['summary.md','report.md','protocol.json','integrated-scorecard.json','integrated-results.json','decision.json','observation-candidate.json','balance-observation.json','previous-six-models-new-objective.json','previous-unmerged-new-objective.json','candidate/protocol.json','candidate/definition.json','candidate/source-manifest.json','candidate/replay-verification.json']
    for root in (c.RUN,first.RUN,pay.RUN,stall.RUN):
        for name in ('results.json','scorecard.json','verification.json','attribution.json','protocol.json'):artifacts.append((root/name).relative_to(RUN).as_posix())
    artifacts=list(dict.fromkeys(artifacts));sources=['scripts/swing_c_structure.rs','scripts/research_swing_c_structure.py','scripts/report_swing_c_structure.py','scripts/research_swing_c_first_retest.py','scripts/report_swing_c_first_retest.py','scripts/research_swing_total_payoff.py','scripts/report_swing_total_payoff.py','scripts/research_swing_c_stall.py','scripts/report_swing_c_stall.py','scripts/finish_swing_total_net.py','tests/swing_c_structure.rs','tests/swing_c_stall.rs']
    w.write(RUN/'completion.json',dict(status=decision['status'],new_models=10,old_unmerged_reselected=True,new_model_primary_winner=None,controls=96,candidate_raw_exact=28960,best_a_preserved=True,production_unchanged=169,qualified_for_trading=False,artifacts={a:w.sha(RUN/a) for a in artifacts},sources={s:w.sha(w.REPO/s) for s in sources}))
    print('Complete:10 new rules rejected for primary total; old unmerged candidate +132 selected as weak observation;96 controls,28960 raw replay matches; frozen最佳a unchanged.',flush=True)

if __name__=='__main__':run()
