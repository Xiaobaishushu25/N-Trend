"""Record all20 combinations, two policy observations and their strict limits."""
import datetime as dt,inspect,json,shutil
import research_swing_portfolio_balance as main
import research_swing_portfolio_selective as selective
import swing_portfolio_selective as controller
import swing_portfolio_balance as parent
import research_swing_consistency as consistency
from audit_swing_flat_ambiguity import ambiguity
w=main.w;RUN=main.RUN;EXT=selective.RUN;SF=['corrected_touch/slip1','corrected_touch/slip2','next_minute_open/slip1','next_minute_open/slip2']
SNAP=w.REPO/'research-strategies/best-a-portfolio-observation-v1'
NAMES={'base':'原经济性候选','one_attempt':'同A仅首次触发','after_loss':'同A累计已实现亏损后停止','after_stop':'同A首次止损后停止','sector1_same':'同组同向最多1个','sector2_same':'同组同向最多2个','sector1_any':'同组最多1个','sector2_loss':'同组同向2个＋亏损停止','sector2_counter':'仅小时不顺向同组同向2个','after_two_stops':'同A两次止损后停止','sector2_counter_stops':'小时不顺向2个＋两次止损停止'}

def run():
    p=w.read(EXT/'protocol.json');res=w.read(EXT/'results.json');parentres=w.read(RUN/'results.json');attr=w.read(EXT/'attribution.json');rob=w.read(EXT/'additional-robustness.json');diag=w.read(EXT/'mechanism-diagnosis.json');score=w.read(RUN/'scorecard.json')+[z for z in w.read(EXT/'scorecard.json') if z['rule']!='base'];score.sort(key=lambda z:z['total'],reverse=True);assert len(score)==22
    models=['sector2_counter','sector2_counter_stops'];assert score[0]['name']=='budget_body/sector2_counter_stops'
    for model in models:
        for pol in ['original','two_paths_flat']:
            for sf in SF:assert res[pol]['combined']['budget_body/'+model+'/'+sf]['net']>res[pol]['combined']['budget_body/base/'+sf]['net']
        assert rob['summary'][model]['leave_product_positive']==rob['summary'][model]['leave_product_total'];assert rob['summary'][model]['tie_positive']==rob['summary'][model]['tie_total']
    assert all(w.sha(k)==v for k,v in p['production_before'].items());assert all(w.sha(main.SNAP/k)==v for k,v in w.read(main.SNAP/'manifest.json').items());assert all(w.sha(main.ECON_SNAP/k)==v for k,v in w.read(main.ECON_SNAP/'manifest.json').items())
    SNAP.mkdir(exist_ok=False)
    body=(w.REPO/'scripts/swing_portfolio_selective.py').read_text(encoding='utf-8').replace('from research_swing_consistency import ev,UNKNOWN,cluster\n','').replace('from swing_portfolio_balance import reservation\n','')
    source='"""Self-contained frozen policy, assembled from tested function sources."""\nUNKNOWN='+repr(consistency.UNKNOWN)+'\n'+inspect.getsource(consistency.ev)+'\n'+inspect.getsource(consistency.cluster)+'\n'+inspect.getsource(ambiguity).replace('s.ev(','ev(')+'\n'+inspect.getsource(parent.reservation)+'\n'+body
    (SNAP/'controller.py').write_text(source,encoding='utf-8')
    shutil.copy2(w.REPO/'scripts/check_swing_portfolio_frozen.py',SNAP/'replay-checker.py')
    for file in ['swing_portfolio_selective.py','swing_portfolio_balance.py']:shutil.copy2(w.REPO/'scripts'/file,SNAP/('source-'+file))
    p.update(id='best-a-portfolio-observation-v1',reference_results=str(EXT/'results.json'),reference_results_sha256=w.sha(EXT/'results.json'),reference_accepted=str(EXT/'accepted.jsonl'),base_economic_candidate='best-a-unmerged-budget_body-observation-v1',policy_controller_sha256=w.sha(SNAP/'controller.py'),frozen_economic_snapshot_manifest_sha256=w.sha(main.ECON_SNAP/'manifest.json'))
    deps=['scripts/check_swing_portfolio_frozen.py','scripts/reprice_signal_costs.py','scripts/report_signal_ablation.py','scripts/report_paper_candidate_history.py','scripts/research_swing_consistency.py','scripts/research_swing_abstop.py','scripts/report_swing_c_structure.py','scripts/research_swing_combination.py']
    p['replay_dependency_hashes']={k:w.sha(w.REPO/k) for k in deps};w.write(SNAP/'protocol.json',p)
    defs={}
    for model in models:
        key='budget_body/'+model;row=next(z for z in score if z['name']==key)
        defs[model]=dict(id='best-a-budget-body-'+model.replace('_','-')+'-observation-v1',status='frozen_research_observation',base=p['base_economic_candidate'],rule=NAMES[model],counter_definition='Warning-time trend_alignment_60 is nonnull and <=0, including neutral. Max2 active counter-category reservations per fixed sector and direction; aligned/missing-hour opportunities not counted in this cap.',retry_definition='Same symbol,direction,S0 timestamp,S1 timestamp; third+ attempt blocked only after2 accepted original stop exits strictly before current trigger.' if model.endswith('stops') else 'No additional wave retry veto.',results=row,independent_validation=False,paper_collection_started=False,production_changed=False,qualified_for_trading=False)
        w.write(SNAP/(model+'.definition.json'),defs[model])
    (SNAP/'README.md').write_text('\n'.join(['# 最佳a经济性分支：组合观察候选','','保留两种观察：sector2_counter侧重较简单规则及略低盈利集中；sector2_counter_stops侧重当前开发期总净收益。两者都以已有budget_body为基础，不覆盖最佳a或上一轮快照。','','controller.py是自包含的冻结组合控制器。输入需来自已冻结经济性执行器的完整个体回放；个体成交结果不改动，再按触发时间重算全部机会竞争。不能只对旧已接受交易筛选。研究协议保留原数据文件路径及哈希，因此依赖当前研究归档，并非独立可移植实盘程序。','','复现：在仓库根目录运行 python scripts/check_swing_portfolio_frozen.py research-strategies/best-a-portfolio-observation-v1 新核验文件.json 。输出文件必须尚不存在。','','全部保持一手、原止损、目标与退出。未知损益单列；平仓退出必须严格早于新触发才释放名额。小时不顺向类别包含0及负值，不等同单纯反向趋势。只允许已实现的原stop退出更新次数，no_follow等亏损不计为stop。','','这批规则是在主实验14个失败组合之后追加的6个探索组合，所有时期均已反复研究；没有独立保留数据或新纸面记录。主场景增益只涉及9/12笔交易，上半年和最严成本延迟场景仍负。品种均衡尚未解决；峰值名义占用风险未下降。'])+'\n',encoding='utf-8')
    w.write(SNAP/'manifest.json',{f.relative_to(SNAP).as_posix():w.sha(f) for f in SNAP.rglob('*') if f.is_file()})
    w.write(RUN/'integrated-scorecard.json',score);w.write(RUN/'candidate-definitions.json',defs)
    w.write(RUN/'decision.json',dict(status='two_portfolio_policy_observations_frozen',highest_profit=defs[models[1]]['id'],simpler_balance=defs[models[0]]['id'],main_combinations=14,post_main_exploratory_combinations=6,objective='Higher total known closed net and product balance; no80% gate; unchanged1lot.',full_cycle_stability_reached=False,product_balance_solved=False,production_changed=False,qualified_for_trading=False))
    baseline=res['original']['combined']['budget_body/base/'+SF[0]]
    lines=['# 小波段策略：收益与品种均衡的组合验证','',
        '本轮有小幅推进：在上一轮收益最高的“空间成本＋A净实体同向”候选上，只限制小时不顺向类别的同组同向并发，开发期合计已知净额提高；再限制同A两次止损后的重试，总额进一步提高。品种集中度只轻微变化，仍未解决跨期稳定。',
        '', '## 固定口径与新增规则','',
        '原最佳a、两种经济性候选及生产参数均保持不变。相同一手、费率模型、每边1tick主场景；原止损、目标、退出不变。两期合并后按触发时刻重排全部个体机会，包含未成交和未知；不只筛旧成交。近期各品种数据起点不同，不是等覆盖四个月。',
        '', '1. 小时不顺向2个：预警时trend_alignment_60非空且≤0（含中性）为不顺向类别。同一固定行业组、同方向最多同时保留2个该类别占用；顺向和缺失小时信息的机会不受新增行业限制，仍受原同品种限制。',
        '2. 可选两次止损后停试：完全相同symbol/direction/S0/S1簇，已有2次被接受交易以原stop退出后，后续机会停止。只看exit<新trigger的已实现退出，未退出、未知和同分钟退出均不能提前利用；no_follow等亏损不计为stop。新A身份不受旧A次数限制。',
        '', '所有行业组来自既有固定分类，未拟合相关系数、未按品种历史盈亏设白名单。这是粗略风险代理，不代表同组内实际相关性都相同。',
        '', '## 主场景：一手已知平仓净额（元）','', '|版本|上半年|近期|合计|已知笔数|合计已实现回撤|','|---|---:|---:|---:|---:|---:|']
    for model in ['base']+models:
        k='budget_body/'+model+'/'+SF[0];m=res['original']['combined'][k];lines.append(f"|{NAMES[model]}|{res['original']['early'][k]['net']:.2f}|{res['original']['recent'][k]['net']:.2f}|{m['net']:.2f}|{m['n']}|{m['realized_cash_drawdown']:.2f}|")
    lines+=['', '主场景每版均1笔未知损益单列，未按0计入。已实现现金回撤不是包含浮盈亏的账户权益回撤。', '', '|分布指标|原经济性候选|仅小时不顺向2个|再加两次止损|','|---|---:|---:|---:|']
    for key,label,fmt in [('win_rate','胜率','.2%'),('mean_r','每笔平均净R','.4f'),('top3_product','前三盈利品种占正贡献比','.2%'),('positive_product_hhi','盈利贡献HHI（越低越分散）','.4f'),('negative_warning_month_burden','亏损预警月份负担','.2f'),('worst_warning_month','最差预警月份','.2f'),('peak_reserved_nominal_cashR','峰值名义占用现金R','.2f')]:
        vals=[res['original']['combined']['budget_body/'+model+'/'+SF[0]][key] for model in ['base']+models];lines.append('|'+label+'|'+'|'.join(format(v,fmt) for v in vals)+'|')
    lines+=['', '仅行业限制同时小幅降低HHI与前三占比；再加止损次数后HHI略高于原版，虽然前三占比仍低。不能把最高收益版称为所有均衡指标都更好。峰值名义占用风险仍为3595元，原因是新增上限只针对小时不顺向类别；未削减所有顺向持仓。该数含未填单及未知占用，不是保证最大损失、保证金或账户风险。',
        '', '## 更深的诊断','',
        '一手不等于相同风险：单成本候选SH平均初始现金R约738元，SR约161元，约84%的累计已知平仓初始风险来自能源化工组。这是累计入场风险份额，不能直接解释为84%的同期敞口。',
        '', f"重复进场不宜一刀切：收益最高旧候选的{diag['repeated_waves']['repeated_known_closed_clusters']}个已知重复簇，首次交易合计{diag['repeated_waves']['first_legs']['net']:.2f}元，后续交易合计{diag['repeated_waves']['subsequent_legs']['net']:.2f}元。此前重复簇总计亏约5463元包含了首次亏损，不能据此推断重试本身亏损。这个簇归集含事后重复身份，只作描述；真正的触发限制另按因果顺序回放。",
        '', '硬行业上限同样会切掉有效小波段。组合版同组同向最多2个，上半年改善至−4807.92元，但近期降至20585.50元，合计15777.58元，比旧收益候选少791.13元；已实现回撤降至10473.99元，可作风险收益取舍观察。其前三盈利品种占比反升至80.81%，同分钟排序反转后相对增益还会翻正，不能称收益均衡已稳定改善。',
        '', '选择性限制的机制是减少小时不顺向时同类机会叠加，同时保留顺向机会和有价值的首次重试。它不要求市场必须突破或走大趋势，也没有证明所有逆小时信号无效。历史关联可支持继续观察，不能证明市场因果。',
        '', '## 新增净额从哪里来','']
    for model in models:
        a=attr['budget_body/'+model]['original'][SF[0]];lines.append(f"- {NAMES[model]}：原{a['removed_n']}笔净额{a['removed_net']:.2f}元不再纳入，新增已知平仓{a['added_n']}笔；{a['common_closed']}笔共同交易的完整结果及费用不变。")
    lines+=['', '仅行业限制增益主要为L+543、MA+316.93、EG+296、EB+102、PP+12元，V−143元抵消。追加止损次数仅再少3笔、提高225元，主要来自EG；证据量有限，保留简单版作为对照。均衡改善依然发生在能源化工组内部，未创造农业或金属的新优势。',
        '', '## 四种压力：合计已知净额','', '|场景|原经济性候选|仅小时不顺向2个|再加两次止损|','|---|---:|---:|---:|']
    for sf in SF:lines.append('|'+sf+'|'+'|'.join(f"{res['original']['combined']['budget_body/'+model+'/'+sf]['net']:.2f}" for model in ['base']+models)+'|')
    lines+=['', '两个候选在两种未知占用口径、四个成本/延迟场景中均高于原收益候选；同分钟反向排序也保留增益。最严“额外一分钟＋每边2tick”仍亏，next_minute_open是分钟延迟代理，不是真实下一报价。上半年在全部压力场景仍亏。',
        '', '逐一去除固定分组内22个品种（含无机会品种），先移除其全部潜在机会，再重算竞争：每个候选176个品种/压力/占用检查均保留相对增益，最小分别+242/+452元。该核验不同于只从原成交表减去某品种；它仍是已反复研究历史的内部扰动，不是独立留品种验证。',
        '', '另用3个固定种子改变同trigger及warning时刻的机会顺序，每个候选24个检查增益均正；所有结果保留。两个预警周描述性95%差额区间约[211,2254]/[220,2704]元；因为规则是在主结果之后追加，且历史长期反复研究，没有多重选择校正，区间未跨零也不能当作独立显著性证明。',
        '', '## 全部探索结果（不省略失败）','', '先固定7个组合规则×2个经济性候选=14个新组合，均降低主场景总收益。随后针对失败机制，只追加上述3个规则×2候选=6个探索组合；追加前另存协议，未扫描阈值。下表共20个新组合及2个原对照。','', '|基础 / 组合规则|上半年|近期|合计|相对其基础|HHI|','|---|---:|---:|---:|---:|---:|']
    for z in score:lines.append(f"|{z['profile']} / {NAMES[z['rule']]}|{z['early']:.2f}|{z['recent']:.2f}|{z['total']:.2f}|{z['delta']:+.2f}|{z['product_hhi']:.4f}|")
    lines+=['', '单成本budget10上仅行业限制反而少36.07元；组合收益依赖已有A净实体筛选，说明不能把新的行业规则推广到所有A版本。两次止损阈值只带来3笔差异，不继续在当前样本上扫“几次”或“几个持仓”。',
        '', '## 记录与下一步','',
        '两种组合观察候选已保存在research-strategies/best-a-portfolio-observation-v1，包含自包含控制器、定义、协议、原来源、哈希和复现核验入口。个体执行仍使用上一轮已核对的原生经济性执行器，新增组合层没有接入实盘。最佳a153个文件、经济性快照293个文件及生产169个文件均未变。',
        '', '本轮主实验和追加各64个原对照统计重现；独立区间重叠审计和已实现止损次数审计通过。冻结控制器另核验完整接受序列及统计。实现核验不代替独立行情、逐报价成交或新时期验证。',
        '', '下一步优先在新记录里并行观察原收益候选、仅行业限制及止损组合，按真实费用、报价、机会排序和未知敞口一致记录。若继续历史研究，应分析农业/金属的“可交易目标空间与实际成本”、以及A/B局部尺度是否失配；先做机制归因，再决定是否使用共用归一化条件，避免按品种亏赚硬删或加仓把收益凑平。账户资金与最大并发风险约束未给出，本轮不以加手数提升总额。']
    (RUN/'summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    full=['# 全成本、占用与时期结果','']
    for pol in ['original','two_paths_flat']:
        full+=['## '+pol,'','|版本|场景|上半年|近期|合计|未知|','|---|---|---:|---:|---:|---:|']
        for z in score:
            rr=res if z['rule'] in ['sector2_counter','after_two_stops','sector2_counter_stops'] else parentres
            for sf in SF:
                k=z['name']+'/'+sf;full.append(f"|{z['name']}|{sf}|{rr[pol]['early'][k]['net']:.2f}|{rr[pol]['recent'][k]['net']:.2f}|{rr[pol]['combined'][k]['net']:.2f}|{rr[pol]['combined'][k]['unknown']}|")
    (RUN/'report.md').write_text('\n'.join(full)+'\n',encoding='utf-8')
    w.write(w.REPO/'research-protocols/best-a-portfolio-observations-20261026-v1.json',dict(snapshot=str(SNAP),manifest_sha256=w.sha(SNAP/'manifest.json'),candidates={k:v['id'] for k,v in defs.items()},originals_preserved=True,production_changed=False,qualified_for_trading=False))
    w.write(RUN/'frozen-snapshot-created.json',dict(snapshot=str(SNAP),manifest_sha256=w.sha(SNAP/'manifest.json'),created_at=dt.datetime.now(dt.timezone.utc).isoformat(),needs_frozen_replay_check=True))
    print('Recorded20 new combinations, froze2 policy observations; pending frozen replay check.',flush=True)

if __name__=='__main__':run()
