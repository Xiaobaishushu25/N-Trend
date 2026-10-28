"""Deliver named best-a snapshot and all six B-transition falsification results."""
import collections,json,statistics
import research_swing_b_transition as main
import research_swing_b_rejection as post
import research_swing_abstop as s
w=main.w;RUN=main.RUN

def run():
    p=w.read(RUN/'protocol.json');a=w.read(RUN/'results.json');b=w.read(post.RUN/'results.json');v=w.read(RUN/'verification.json');pv=w.read(post.RUN/'verification.json');groups=w.read(RUN/'baseline-state-groups.json')
    assert not v['defensive_candidates'] and not pv['defensive_candidates'];assert not v['strict_stable'] and not pv['strict_stable']
    # Exact arithmetic split of the mean-R period difference. Descriptive only.
    old=groups[str(('original','early','corrected_touch/slip1'))];new=groups[str(('original','recent','corrected_touch/slip1'))];N=sum(z['n'] for z in old.values());M=sum(z['n'] for z in new.values());mix=within=0;parts={}
    for k in sorted(old):
        x,y=old[k],new[k];we,wr=x['n']/N,y['n']/M;u=(wr-we)*(x['mean_r']+y['mean_r'])/2;z=(y['mean_r']-x['mean_r'])*(wr+we)/2;mix+=u;within+=z;parts[k]=dict(early_n=x['n'],recent_n=y['n'],early_meanR=x['mean_r'],recent_meanR=y['mean_r'],mix_contribution=u,within_state_contribution=z)
    assert abs(mix+within-(a['original']['recent']['base/corrected_touch/slip1']['mean_r']-a['original']['early']['base/corrected_touch/slip1']['mean_r']))<1e-12
    decomposition=dict(early_n=N,recent_n=M,mix_contribution=mix,within_state_contribution=within,total_R_difference=mix+within,states=parts,note='Exact descriptive symmetric arithmetic decomposition of accepted baseline meanR, no causal attribution, no significance/holdout claim; state definitions frozen before replay.')
    existing=w.read(RUN/'state-period-decomposition.json');assert existing==decomposition
    # Keep the same original trades when diagnosing why structural exits fail.
    maps=collections.defaultdict(dict)
    for line in (post.RUN/'accepted.jsonl').open(encoding='utf-8'):
        z=json.loads(line)
        if z['policy']=='original' and z['case'].endswith('/corrected_touch/slip1') and s.ev(z['row'])['status']=='closed':maps[(z['period'],z['case'].split('/')[0])][z['row']['event_key']]=z['row']
    diagnosis={}
    for profile in post.PROFILES[1:]:
        diagnosis[profile]={}
        for q in ('early','recent'):
            oc=maps[(q,'base')];nc=maps[(q,profile)];pairs=[(oc[k],nc[k]) for k in oc.keys()&nc.keys() if s.ev(nc[k])['exit_reason']=='micro_progress_rejected'];diagnosis[profile][q]=dict(n=len(pairs),original_exit_reasons=dict(collections.Counter(s.ev(x)['exit_reason'] for x,y in pairs)),old_net=sum(s.ev(x)['net_amount'] for x,y in pairs),new_net=sum(s.ev(y)['net_amount'] for x,y in pairs),delta=sum(s.ev(y)['net_amount']-s.ev(x)['net_amount'] for x,y in pairs))
    prior=w.read(post.RUN/'same_trade_rejection_diagnosis.json')
    for profile in diagnosis:
        for q in ('early','recent'):
            for k,val in diagnosis[profile][q].items():
                assert abs(val-prior[profile][q][k])<1e-7 if isinstance(val,(int,float)) else val==prior[profile][q][k]
    names={'base':'最佳a策略','all_touch':'内部转折改价触发','all_close':'内部转折改价＋收盘确认','fresh_close':'仅B新极值等待确认','inside_close':'仅B内部等待确认','rejection_any':'越过后回穿退出','rejection_profit':'仅浮盈时回穿退出'}
    versions=[(name,a) for name in main.PROFILES]+[(name,b) for name in post.PROFILES[1:]]
    decision=dict(status='six_new_rules_rejected_named_best_a_preserved',named_baseline='最佳a策略',id='best-a-v1',snapshot=str(main.SNAP),snapshot_manifest_sha256=w.sha(main.SNAP/'manifest.json'),new_models=6,
        sequence='Four entry-evidence models frozen before outcomes, then two disclosed exploratory exit models after the entry results. All failed models and pressure results retained.',
        conclusion='Within these pre-defined B states, period differences largely reside in conditional outcomes, not state proportions. Entry confirmation sacrifices price/space; first micro-level rejection often precedes eventual target in recent data.',
        future_hypothesis='Distinguish the first retest of a reclaimed B pivot from failure of a NEW, independently confirmed C higher-low/lower-high structure. Not verified or applied this round.',
        independent_validation=False,paper_started=False,production_changed=False,qualified_for_trading=False)
    w.write(RUN/'decision.json',decision)
    lines=['# 最佳a策略已冻结；B内反弹与第二波推进的进一步验证','',
        '用户指定名称“最佳a策略”，固定版本best-a-v1，对应上一轮near_start/adaptive。源码、规则、参数、两期预警池、已接纳逐笔交易、四种压力结果和可执行程序已独立快照，manifest.json锁定153个文件。后续试验没有覆盖该版本。',
        '', '本轮4个进场证据模型及随后2个探索性退出模型均未达到近期收益保留80%的要求；没有新增可升级候选。实盘未改，全周期稳定目标尚未达到。',
        '', '## 同一A/B下，分别检验进场和退出','',
        '先锁定最佳a选择的父A/B身份。取同一B内、预警柱开始之前已经确认的最近严格5分钟转折；触发价在该转折外1tick。分开检验改价触发、分钟收盘确认、只对B新极值/只对B内部等待。原止损和绝对目标固定，不因等待而改用更远目标；成本、净空间和机会竞争全部重算。',
        '', '第二组保留原入场价和风险，实际入场后必须先有分钟收盘越过内部转折，再在更晚一分钟回穿原转折，才于下一分钟开盘退出；另检验仅当前浮盈时执行。它是主实验失败后增加的探索，未当成预先独立验证。',
        '', '以下为每边1tick、扣实际手续费后的单手已平仓净额；未知收益单列。近期不同品种起点不齐，非完整等覆盖四个月。',
        '', '|规则|上半年净额|平均净R|近期净额|近期保留|','|---|---:|---:|---:|---:|']
    for name,stats in versions:
        e=stats['original']['early'][name+'/corrected_touch/slip1'];n=stats['original']['recent'][name+'/corrected_touch/slip1'];ref=a['original']['recent']['base/corrected_touch/slip1'];lines.append(f"|{names[name]}|{e['net']:.2f}|{e['mean_r']:.4f}|{n['net']:.2f}|{n['net']/ref['net']:.1%}|")
    lines+=['', '所有模型均同时检查两种未知占用口径和四种成本/延迟压力；两期全场景现金/R转正、近期80%保留、样本/未知/回撤等门槛没有模型全部通过。原始结果和失败项目全部保留。',
        '', '## 时期差距现在定位得更具体','', '|原最佳a的当时状态|上半年笔数|平均净R|近期笔数|平均净R|','|---|---:|---:|---:|---:|']
    for key,label in [('awaiting_break_inside_B','B内部，尚未越过内部转折'),('awaiting_break_fresh_B_extreme','B新极值，尚未越过内部转折'),('warning_already_clears_internal_pivot','预警已越过内部转折'),('no_confirmed_internal_pivot','没有已确认内部转折')]:
        x,y=old[key],new[key];lines.append(f"|{label}|{x['n']}|{x['mean_r']:.4f}|{y['n']}|{y['mean_r']:.4f}|")
    lines+=['', '尚未越过内部转折的两组在上半年占189/444=42.6%，近期占69/178=38.8%，比例没有剧烈变化；但它们在上半年合计净亏13723.53元，近期合计净赚13818.83元，贡献近期净收益约65.6%。统一删掉或推迟这些机会，会同时删掉近期主要优势。',
        '', f"对平均净R差{mix+within:.4f}做对称算术分解，状态占比变化贡献{mix:.4f}，同状态内表现变化贡献{within:.4f}。这只是开发期描述性分解，不能当成因果或统计显著性证明；但不支持将剩余差异主要归结为上半年多识别了一类劣质形态。",
        '', '## 等确认与过早退出，为何都牺牲近期收益','',
        '全面改价触发：上半年308笔共同交易合计反而少赚2685.15元，改善主要来自不再接纳的一批原交易；近期133笔共同交易少赚2155.01元，另有原净赚7974.47元的45笔未再接纳。确认并未凭空提高共同交易收益，等待会扩大到原止损的风险、缩小到原目标的空间。',
        '', '越过后回穿即退出：上半年同一67笔原交易中40笔原本止损，提前退出合计改善6012.15元；近期同一35笔中22笔原本能止盈，提前退出合计少赚9129.48元。仅浮盈时退出也不能解决：近期受影响26笔中19笔原本能止盈，合计少赚8774.53元。',
        '', '因此，首次越界与首次回穿都不足以单独定义真实C段及其失效；在这些数据中，回穿内部转折既可能是继续失败，也可能是正常再回调。不能因后面盈利就倒填B终点，也不能为了提高上半年胜率提前切掉近期第二波收益。',
        '', '## 保留策略与后续方向','',
        '继续以最佳a策略best-a-v1为固定对照，不叠加本轮六个条件。下一条待验证假设是：让C自己形成已确认的更高低点/更低高点，再研究这个新结构是否真正破坏；不能用原B里的旧转折线第一次回穿就代替C失效。该假设尚未回放，本轮不将它包装成已验证优化。',
        '', '## 核验','',
        '6项特征测试、3项进场执行测试、4项退出时序测试通过，包括父B边界、未来追加、会话断档、多空镜像、预警柱排除、入场前不得激活、先越过后回穿和下一分钟退出。',
        '', f"两组各24个时期/压力/占用控制与最佳a归档一致。进场组{v['qa']['inactive_routes_full_result_exact']}条未受影响的完整成交结果一致；退出组{pv['qa']['unchanged_individual_results']}条未改变退出的结果一致，另独立分钟核验{pv['qa']['independent_post_fill_clear_reject_next_open']}笔退出的入场后激活、回穿及下一分钟开盘价格。",
        '', '描述性分组初稿被后一个压力场景覆盖，现已按明确场景索引并核对444/178笔主场景总数；旧报告保留，策略及任何组合收益未改。强化退出测试时，人工夹具被原入场追价保护正确拒绝，已修正夹具开盘价并重新通过；执行器与回放规则未变，记录保留。',
        '', '最佳a快照153个文件和生产169个文件哈希不变。未知盈亏未记为0；压力延迟不是实际下一报价；回撤为已实现现金回撤。所有历史仍是反复研究的开发数据，没有新独立验证、纸面记录或实盘接入。']
    (RUN/'summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    # A compact pressure table for the extra exits, kept beside their raw data.
    ls=['# B内部转折回穿退出：探索性补充','', '这两条规则在四种改价进场模型失败后固定。最佳a的进场、止损、绝对目标不变；仅改变持仓后退出。','', '|规则|场景|上半年净额|平均净R|近期净额|近期保留|','|---|---|---:|---:|---:|---:|']
    for name in post.PROFILES:
        for suffix in ('corrected_touch/slip1','corrected_touch/slip2','next_minute_open/slip1','next_minute_open/slip2'):
            e=b['original']['early'][name+'/'+suffix];n=b['original']['recent'][name+'/'+suffix];ref=b['original']['recent']['base/'+suffix];ls.append(f"|{name}|{suffix}|{e['net']:.2f}|{e['mean_r']:.4f}|{n['net']:.2f}|{n['net']/ref['net']:.1%}|")
    (post.RUN/'report.md').write_text('\n'.join(ls)+'\n',encoding='utf-8')
    w.write(post.RUN/'test-verification.json',dict(entry_tests_passed=3,rejection_tests_passed=4,fixture_strengthening_rerun_passed=True,test_sha256=w.sha(w.REPO/'tests/swing_b_rejection.rs')))
    main.verify_snapshot();assert all(w.sha(k)==h for k,h in p['production_before'].items())
    for root in (RUN,post.RUN):assert all(w.sha(root/'fork'/k)==v for k,v in w.read(root/'implementation-manifest.json').items())
    assert w.sha(RUN/'results.json')==w.sha(RUN/'report-before-state-scenario-index-fix/results.json')
    paths=['summary.md','report.md','protocol.json','results.json','gates.json','attribution.json','baseline-state-groups.json','state-period-decomposition.json','verification.json','decision.json','test-verification.json','report-state-scenario-correction.json','progress-rejection/protocol.json','progress-rejection/results.json','progress-rejection/gates.json','progress-rejection/attribution.json','progress-rejection/same_trade_rejection_diagnosis.json','progress-rejection/verification.json','progress-rejection/report.md','progress-rejection/test-verification.json']
    sources=['scripts/freeze_best_a_strategy.py','scripts/swing_b_identity.py','scripts/research_swing_b_transition.py','scripts/report_swing_b_transition.py','scripts/research_swing_b_rejection.py','scripts/report_swing_b_rejection.py','scripts/finish_swing_b_transition.py','tests/test_swing_b_identity.py','tests/swing_b_identity.rs','tests/swing_b_rejection.rs']
    w.write(RUN/'completion.json',dict(status='complete_named_best_a_preserved_six_new_rules_rejected',new_models=6,control_regressions=48,best_a_snapshot_unchanged=True,best_a_snapshot_manifest_sha256=w.sha(main.SNAP/'manifest.json'),production_changed=False,production_unchanged=169,qualified_for_trading=False,full_stability_reached=False,artifacts={name:w.sha(RUN/name) for name in paths},sources={name:w.sha(w.REPO/name) for name in sources}))
    print('Complete: 最佳a策略best-a-v1 frozen;6 new models rejected;48 exact controls;153 snapshot files/169 production files unchanged.',flush=True)

if __name__=='__main__':run()
