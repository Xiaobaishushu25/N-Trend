"""Audit and render the frozen robustness study; never selects new candidates."""
import collections
import copy
import json
import pathlib
import random
import shutil

from entry_model import completed_context
from report_signal_ablation import jsonl, key, read_json
from report_paper_candidate_history import csv_write
from research_strategy_robustness import (ROOT,RUN,SOURCE,SIGNALS,FEES,HOLIDAYS,PROTOCOL,
    digest,write,shape_snapshot,paired_interval,research_calendar)
from validate_execution_research import evaluation


def audit():
    result=read_json(RUN/'validation-calendar-audit.json');selection=read_json(RUN/'selection.json')
    source,fees=read_json(SOURCE),read_json(FEES)
    events={key(e):e for e in read_json(SIGNALS)}
    contexts={s:completed_context(v['bars'],v['rollovers']) for s,v in source['symbols'].items()}
    rows=list(jsonl(RUN/'accepted-calendar-audit.jsonl'))
    # Price/fee/volume consistency, month/product totals and integer sizing.
    for row in rows:
        e=evaluation(row)
        if e['status']!='closed':continue
        fills=row['result']['fills'];opening=fills[0]
        assert opening['volume']==e['volume']==sum(f['volume'] for f in fills[1:])
        assert isinstance(e['volume'],int) and e['volume']>=1
        cash=sum((f['price']-opening['price'])*(1 if row['direction']=='up' else -1)*f['volume']*row['multiplier'] for f in fills[1:])
        assert abs(cash-e['net_amount']-row['cost_repricing']['commission'])<1e-6
        assert abs(e['net_r']-e['net_amount']/(e['risk']*row['multiplier']*e['volume']))<1e-9
    for ss in result['cases'].values():
        for gs in ss.values():
            for g,m in gs.items():
                if g=='capacity':continue
                for unit in ('products','months','weeks','days'):
                    assert abs(sum(v['net'] for v in m[unit].values())-m['stats']['net'])<1e-6
                    assert sum(v['n'] for v in m[unit].values())==m['stats']['n']
    # Frozen candidate list remains exactly the one selected before holdout inspection.
    assert set(result['cases'])=={'P2','P3',*selection['selected']}
    assert digest(RUN/'replay-final/evaluations.jsonl')==digest(RUN/'replay/evaluations.jsonl')
    manifest=read_json(RUN/'replay-final/manifest.json')
    assert manifest['runner_sha256']==digest(ROOT/'crates/n-core/src/bin/signal_robustness.rs')
    assert manifest['engine_sha256']==digest(ROOT/'crates/n-core/src/v3/ablation.rs')
    sample=[]
    for s in sorted(source['symbols']):
        candidates=[r for r in rows if r['candidate']=='P2' and r['slip']==1 and r['symbol']==s]
        if candidates:
            sample.extend([candidates[0],candidates[len(candidates)//2],candidates[-1]])
    feature_checks=0
    for row in sample:
        ev=events[row['event_key']];symbol=ev['symbol'];truncated=copy.deepcopy(source['symbols'][symbol])
        truncated['bars']=[b for b in truncated['bars'] if b['datetime']<=ev['warning_ts']]
        truncated['rollovers']=[r for r in truncated['rollovers'] if r['ts']<=ev['warning_ts']]
        prefix=completed_context(truncated['bars'],truncated['rollovers'])
        a=shape_snapshot(ev,row['score_components'],source,contexts,fees,row['multiplier'])
        b=shape_snapshot(ev,row['score_components'],{**source,'symbols':{symbol:truncated}},
                         {symbol:prefix},fees,row['multiplier'])
        assert a==b
        feature_checks+=1
    # Attribute policy delta as common closed trade changes plus portfolio changes.
    decomposition={};intervals={};cal=research_calendar(source,True)
    for name in selection['selected']:
        decomposition[name]={};intervals[name]={}
        for slip in (1,2):
            mappings={base:{r['event_key']:r for r in rows if r['candidate']==base and r['slip']==slip and evaluation(r)['status']=='closed'} for base in ('P2',name)}
            a,b=mappings['P2'],mappings[name];common=set(a)&set(b)
            differences=[dict(event_key=k,symbol=b[k]['symbol'],
                entry_ts=evaluation(b[k])['entry_ts'],exit_ts=evaluation(b[k])['exit_ts'],
                delta=evaluation(b[k])['net_amount']-evaluation(a[k])['net_amount']) for k in common]
            decomposition[name][str(slip)]=dict(common=len(common),
                common_delta=sum(v['delta'] for v in differences),
                candidate_only_n=len(set(b)-set(a)),candidate_only_cash=sum(evaluation(b[k])['net_amount'] for k in set(b)-set(a)),
                baseline_only_n=len(set(a)-set(b)),baseline_only_cash=sum(evaluation(a[k])['net_amount'] for k in set(a)-set(b)),
                changed_trades=sorted([v for v in differences if abs(v['delta'])>1e-7],key=lambda v:-v['delta']))
            subsets=[[r for r in rows if r['candidate']==base and r['slip']==slip] for base in ('P2',name)]
            intervals[name][str(slip)]=paired_interval(*subsets,cal.dates,3000)
    old=read_json(RUN/'validation.json')
    corrections={n:{s:result['cases'][n][s]['all']['stats']['net']-old['cases'][n][s]['all']['stats']['net'] for s in ('1','2')} for n in result['cases']}
    output=dict(fill_and_group_checks=True,ledger_rows=len(rows),prefix_feature_checks=feature_checks,
        warning_score_components_exact=2783,prior_P2P3_regression_rows=3284,
        final_replay_byte_identical=True,calendar_fee_cash_corrections=corrections,
        source_placeholders=[dict(symbol=s,**b) for s,v in source['symbols'].items() for b in v['bars']
            if b['datetime'][:10] in ('2026-09-05','2026-09-12') and int(b['datetime'][11:13])>=6],
        delta_decomposition_vs_P2=decomposition,paired_weekly_intervals_vs_P2=intervals,
        retrospective_fixed_ledgers=True,selection_unchanged_after_calendar_audit=True)
    write(RUN/'verification.json',output)
    return result,selection,output


LABELS={'P2':'P2 原S1','P3':'P3 上限2R','P2_weight701515':'A段权重70%','nf_off_s1':'P2取消无跟随退出','P2P3_equal_legs':'两手各按P2/P3退出'}
def number(x):return '无样本' if x is None else f'{x:,.2f}'
def percent(x):return '无样本' if x is None else f'{100*x:.2f}%'
def table(headers,rows):
    return ['|'+'|'.join(headers)+'|','|'+'|'.join('---' for _ in headers)+'|']+['|'+'|'.join(map(str,r))+'|' for r in rows]


def render(j,selection,verification):
    cases=j['cases'];names=list(cases)
    def m(n,slip=1,g='all'):return cases[n][str(slip)][g]
    lines=['# N字形策略分组验证与拓展研究（2026-10-24）','',
        '完成58个预先冻结候选、19种执行模板×两档滑点，共105,754条机会回放。没有找到同时提高胜率、收益和跨品种/时期稳健性、并通过全部检查的新方案。本轮保留P2/P3作对照，新增方案仅留研究。未改动生产参数、账户或自动交易。','',
        '## 1. 系统现在在做什么','',
        '原策略识别15分钟N形：细级别A/B段最多16/12根，大级别最多48/32根；A段至少3根、B段至少2根，回撤按20%—50%、50%—66%、66%—80%分级，并检查结构破坏、深V、微型N与趋势K。预警有强反转、累积覆盖、长影线。', '',
        '评分不是盈利概率。当前总分为A段60%＋B段20%＋预警K线20%；A段把幅度、推进速度和逐K干净度相乘，再扣跳空，B段考虑回撤级别、过快、过长、动能衰减和反向强K；累积覆盖上限3.9，长影线上限3.0。', '',
        'P2/P3共用：评分≥3、结构与实际风险≥10tick、预测往返摩擦≤计划风险20%、风险/ATR14∈[0.75,2.5]、预警及拟成交净空间至少覆盖完整止损及费用。P2固定S1，P3取S1与实际入场2R较近目标；均固定一手、结构止损不移动。第5个持仓桶MFE<0.5R退出，最多60桶；长假和周末前退出。','',
        '## 2. 数据与真实成交的区别','',
        '原始行情为2026-07-03 09:35至2026-10-23 11:05，22品种83,483根5分钟K线；当前识别器3,067条预警，其中评分≥2.5且风险为正2,783条进入共同研究池。行情截至10月23日上午，不能代表10月24日实时状态。','',
        '上轮服务器审计的79笔已平仓SimNow实际成交，按成交价重算并扣估算费用为−1,477.64元，净胜率40.51%；有4笔未平仓。本报告以下表格为正常执行规则的历史反事实，暂不限制资金，并非该账户实际盈利。旧策略拒单、缺资金和退出异常不能靠回测收益抹掉。','',
        '统一使用冻结费用（白糖往返2元覆盖）、每次成交1tick为主、2tick为压力；重算机会占用，未知保留槽位且不计零盈亏。两档成本可能接纳不同交易。所有回撤只是已实现平仓曲线，缺完整浮盈亏和资金保证金约束。','',
        '## 3. 当前收益、亏损与分布','']
    lines+=table(['方案','1tick笔数','胜率','盈利合计','亏损合计','净额','平均盈利/平均亏损','现金盈亏因子','2tick净额'],[
        [LABELS[n],(s:=m(n)['stats'])['n'],percent(s['win_rate']),number(s['profit_sum']),number(s['loss_sum']),number(s['net']),
         number(s['mean_win'])+' / '+number(s['mean_loss']),number(s['cash_pf']),number(m(n,2)['stats']['net'])] for n in names])
    lines+=['','两手混合方案的名义金额含两倍初始手数。其每初始手净额仅16,412.98/13,497.73元，均低于P2的18,728.95/15,450.79元；不能用32,825.96元宣称仓位策略变好。','']
    lines+=table(['方案','平均R 1/2tick','盈利品种/有平仓品种','前3盈利品种占正品种净额','删最好5笔净额','删最好3交易日净额','删最好3品种净额','已实现现金回撤'],[
        [LABELS[n],number(m(n)['stats']['mean_r'])+' / '+number(m(n,2)['stats']['mean_r']),
         str(m(n)['positive_products'])+'/'+str(m(n)['active_products']),percent(m(n)['top3_positive_product_share']),
         number(m(n)['trim']['trade']['cash']['trim']['5']['without_best']['net_amount']),
         number(m(n)['trim']['trading_day']['cash']['trim']['3']['without_best']['net_amount']),
         number(m(n)['trim']['symbol']['cash']['trim']['3']['without_best']['net_amount']),number(m(n)['stats']['realized_cash_drawdown'])] for n in names])
    lines+=['','最新P2/P3删去最大5笔盈利或最好3个交易日后仍为正，因此不能说全靠几笔/几天；但前三盈利品种占正品种净额仍很高。报告同时保存现金/R、单笔/交易日/自然日期/品种两端各剔除1/3/5/10个单位，不能把事后剔除当成可执行策略。','',
        '### 按退出交易日归属月份（1tick / 2tick，元）','']
    months=sorted({mo for n in names for mo in m(n)['months']})
    lines+=table(['方案']+months,[[LABELS[n]]+[number(m(n)['months'].get(mo,{}).get('net'))+' / '+number(m(n,2)['months'].get(mo,{}).get('net')) for mo in months] for n in names])
    lines+=['','月份按退出交易日归属，和旧报告的预警月口径不能混用。7月起点与10月终点不完整。取消无跟随退出的压力场景8月为负，增加收益同时降低了时期广度。','',
        '## 4. 如何探索与保留验证','',
        '先冻结58个候选和输入哈希，再运行回放。所有品种按交易周共同随机分为5份：3份探索、1份验证、1份测试；10月8日以后另作时间保留。候选只用探索组排序，每类固定保留1个。已研究过的历史重分组只提供本轮选参隔离，新增独立观测仍为0；不能称为全新样本外证明。多次尝试导致回测过拟合是已知问题，参见[原始研究论文](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)。','',
        '使用所有19模板、两档成本中最晚的可能占用终点做共同清洗：预警至终点跨组则剔除，分组交界前后各1个交易日禁入；各策略先运行完整历史同品种槽位，再归属分组，避免人为重置槽位。清洗不使用盈亏大小。', '',
        '日历审计后共同池：探索1,192条、验证205条、测试186条、时间保留560条；跨组剔除123条、边界剔除517条。进一步过策略过滤与槽位后，验证仅4—6笔、测试3—5笔，显著不足。该局限是本轮结论的一部分，没有降低样本门槛或换随机种子来找好看的结果。','',
        '筛选目标同时考虑两档成本平均R、样本量、品种和日期集中；每类探索组至少40笔且现金/R为正才有资格。验证阶段另要求每个保留组两档均≥10笔、现金/R为正、胜率和平均R不低于P3，且完整历史分布满足要求。本轮没有候选满足全部条件。','',
        '### 冻结候选在各组的表现（1tick笔数 / 胜率 / 净额）','']
    lines+=table(['方案','探索','随机验证','随机测试','10月时间保留'],[[LABELS[n]]+[
        f"{(s:=m(n,1,g)['stats'])['n']} / {percent(s['win_rate'])} / {number(s['net'])}" for g in ('development','validation','test','temporal')] for n in names])
    lines+=['','### 2tick压力下各组净额','']
    lines+=table(['方案','探索','随机验证','随机测试','10月时间保留'],[[LABELS[n]]+[number(m(n,2,g)['stats']['net']) for g in ('development','validation','test','temporal')] for n in names])
    lines+=['','## 5. 尝试了什么，哪些方向被否定','',
        '- 形态与评分：比较门槛2.5/3/3.5、A/B分项下限、A/B实体方向效率、B/A推进速度、回撤上限0.50/0.66，以及60/20/20之外的40/30/30、40/40/20、70/15/15权重。保留原预警类型封顶；A段包含S0，B段不包含S1，效率仅用当时完整15分钟实体。只研究原有预警池的筛选，没有改变摆点、扩大识别窗口或重新竞争信号。','',
        '- 执行：S1与1.5/2/2.5/3R上限、达到1R后保本、5分钟收盘确认后下一根开盘、先突破再回踩0.25R、无跟随第3/5/8桶或关闭、24/36桶最长持仓、4桶等待。保本仍扣费与滑点，不称无风险；确认价不直接伪造成交价，回踩不得同根先突破再理想成交。','',
        '- 仓位：每笔预测完整止损金额250/500元整数手数预算、最多3手，预算不足则不做；另测统一两手旧分批追踪，以及两手分别按P2/P3退出。没有小数手、按回看盈利品种加仓或按未来概率选仓。','',
        '下面仅展示探索组，不补看其余55个候选的保留组结果来挑赢家。完整58候选开发排名在selection.json。','']
    dev_names=['P2','P3','P2_weight701515','nf_off_s1','protect_s1','confirm_s1','retest_s1','two_lot_legacy','P2_risk250','P2_risk500','P3_risk500']
    lines+=table(['探索组方案','1tick笔数','净胜率','净额','每初始手净额','平均R 1/2tick'],[[LABELS.get(n,n),
        (a:=selection['development'][n][0]['stats'])['n'],percent(a['win_rate']),number(a['net']),
        number(selection['development'][n][0]['per_initial_lot_net']),number(a['mean_r'])+' / '+number(selection['development'][n][1]['stats']['mean_r'])] for n in dev_names])
    lines+=['','具体判断：','',
        '1. **A段提高至70%不升级。** 探索胜率49.06%，高于P2的45.45%；时间保留却降为44.78%，低于P2的47.22%，净额4,246.07也低于P2的6,257.21。它是典型的探索提升、后段回落，不能凭开发排名改生产评分。','',
        '2. **取消无跟随退出留作观察。** 全期净额相对P2只增加358.18/739.11元，胜率升到45.00%/43.85%；平均亏损由−236.62变为−281.08元，正品种前三集中度从70.23%升至75.46%（1tick）。2tick下8月−543.94，去掉最好3品种仅余1,084.69元，收益集中问题更重。','',
        '3. **两手混合没有证明仓位优势。** 其均手收益和平均R都低于P2，已实现回撤约7,299元，高于P2的3,305元；名义利润增大主要来自初始仓位翻倍。同品种占用保留到较慢那条腿完全退出，不能简单把两个独立策略总额相加。','',
        '4. **确认入场值得保留机制假设，但样本不足。** 探索胜率52%，仅25笔，低于预先40笔门槛。回踩方案机会更少且2tick平均R为负；统一两手旧分批的探索胜率虽55.56%，每初始手收益仅4,321元，明显少于P2。风险预算方案也没有在开发组展示更好的收益/广度综合表现。','',
        '## 6. 改善究竟来自哪里','']
    lines+=table(['取消无跟随 vs P2','共同平仓数','共同交易盈亏变化','新接纳平仓数/现金','原有未再接纳数/现金'],[[f'{slip}tick',
        (d:=verification['delta_decomposition_vs_P2']['nf_off_s1'][str(slip)])['common'],number(d['common_delta']),
        str(d['candidate_only_n'])+' / '+number(d['candidate_only_cash']),str(d['baseline_only_n'])+' / '+number(d['baseline_only_cash'])] for slip in (1,2)])
    lines+=['','共同交易变化、被延长的持仓和释放/阻塞的后续机会均纳入，不把收益差全部归因于“少提前退出”。其中10月20日EG一笔变化贡献+1,950元，已超过1tick全期增益358元；若仅从两方案差额中扣除这一个贡献，增益即为−1,591.82元。这是配对差额敏感性，不能当作可事前删除该交易的策略。完整逐笔变化见verification.json。','',
        '按整周跨品种共同重采样的“取消无跟随－P2”全期净额差95%描述区间为：','']
    lines+=table(['滑点','净额差','周块区间'],[[slip,number((v:=verification['paired_weekly_intervals_vs_P2']['nf_off_s1'][str(slip)])['delta_cash']),
        '['+','.join(number(x) for x in v['cash_interval95'])+']'] for slip in (1,2)])
    lines+=['','区间未校正58次尝试且沿用固定接纳账本，只作描述。时间保留仅3周、随机测试仅2周，保留组周块区间不具有可靠资格检验能力；没有把高胜率或区间端点当作通过证据。','',
        '## 7. 数据问题与核验','',
        'PB0在9月5日和9月12日各有一根周六09:05、OHLC相同且成交量0的占位记录，旧“只要有白天记录就算交易日”的推断把它们计入。新研究日历要求至少一个完整日盘15分钟三根组合，剔除这两个伪交易日。原始行情与原始日历报告均保留，候选名单没有重选。','',
        '本次5个冻结方案的总手续费和净额恰未受影响，但9月4日边界的禁入与组归属发生变化；主表使用日历核验版。此修正只在本轮研究中使用，没有修改共享生产日历或旧报告。费用与交易日仍是归档推断，不是逐笔结算认证。','',
        f"核验：2,783条预警原A/B/预警分数逐项一致；3,284条符合P2/P3筛选的旧机会结果、成交腿与费用一致；最终重新编译回放105,754行与初次回放字节一致；{verification['prefix_feature_checks']}个跨品种前/中/后截断特征抽查不使用未来数据；每笔手数与成交盈亏守恒，分品种/月/周/日金额笔数对上总表。相关Rust执行31项、Python研究8项测试通过。",'',
        '仍有边界：连续合约未具备完整可交易合约映射；只有两种单调OHLC路径敏感性，并非逐笔真实撮合；存在未知/未结束接纳机会，不能忽略尾部；缺完整资金、保证金和浮动盈亏。因此不提供账户收益率或真实最大回撤。','',
        '## 8. 本轮交付与下一步判据','',
        '不修改当前P2/P3。保留“P2取消无跟随”作为低优先级观察分支，重点观察BU等头部品种以外、8月类似弱趋势期的亏损扩张；评分70/15/15及混合两手不晋升。所有58个失败/样本不足候选保留，不删除不利试验。','',
        '真正下一步应冻结后收集新增交易，沿用此前至少200笔新增可定价平仓、60个新增交易日的集中复核检查点；同时要求两档成本、品种广度、日期剔除、尾部与未知敞口可接受。数量满足也不自动取得实盘资格。本次没有启动模拟采集、定时任务或自动交易。','',
        '复现顺序：','',
        '```powershell',
        'python scripts/research_strategy_robustness.py prepare',
        'cargo build --offline -p n-core --bin signal_robustness',
        'target/debug/signal_robustness.exe research-runs/server-execution-20261023-input-v1.json research-runs/server-execution-20261023-rebuild-v1/signals.json scripts/exchange_fees_20261022.json research-protocols/holiday-windows-2026-jul-oct-v1.json research-protocols/strategy-robustness-20261024-v1.json research-runs/strategy-robustness-20261024-v1/replay',
        'python scripts/research_strategy_robustness.py discover',
        'python scripts/research_strategy_robustness.py validate',
        'python scripts/research_strategy_robustness.py calendar_audit',
        '```','',
        '已有冻结文件禁止覆盖；复现应在空目录使用相同冻结协议、输入与源码，prepare只用于首次生成。最终格式化源码二次回放在replay-final，原回放保留，二者逐字节一致。输入、源码快照和哈希见本轮bundle，全部本地产物位于：', '',
        f'- [本轮目录]({RUN.as_posix()})',
        f'- [冻结协议]({PROTOCOL.as_posix()})',
        f'- [冻结选择与全部探索结果]({(RUN/"selection.json").as_posix()})',
        f'- [日历核验后的完整验证]({(RUN/"validation-calendar-audit.json").as_posix()})',
        f'- [逐笔接纳账本]({(RUN/"accepted-calendar-audit.jsonl").as_posix()})',
        f'- [分品种CSV]({(RUN/"products-calendar-audit.csv").as_posix()})',
        f'- [月周日CSV]({(RUN/"periods-calendar-audit.csv").as_posix()})',
        f'- [归因与核验]({(RUN/"verification.json").as_posix()})','',
        f'![固定品种顺序、时期和尾部诊断]({(RUN/"robustness.png").as_posix()})','']
    text='\n'.join(lines)
    (RUN/'report.md').write_text(text,encoding='utf-8')
    (ROOT/'docs/策略分组验证与拓展研究-2026-10-24.md').write_text(text,encoding='utf-8')


def plot(j):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams['font.family']='Microsoft YaHei'
    plt.rcParams['axes.unicode_minus']=False
    names=['P2','P3','P2_weight701515','nf_off_s1'];colors=['#2563eb','#10a37f','#d79c25','#db6575']
    fig,axs=plt.subplots(3,2,figsize=(16,13),constrained_layout=True)
    for column,slip in enumerate(('1','2')):
        def m(n):return j['cases'][n][slip]['all']
        syms=sorted(m('P2')['products']);x=np.arange(len(syms));width=.19
        for i,(n,c) in enumerate(zip(names,colors)):
            axs[0,column].bar(x+(i-1.5)*width,[m(n)['products'][s]['net'] for s in syms],width,color=c,label=LABELS[n])
        axs[0,column].set_xticks(x,syms,rotation=60);axs[0,column].set_title(f'{slip} tick：固定品种顺序净额，未交易≠已验证保本')
        axs[0,column].set_ylabel('元');axs[0,column].axhline(0,color='#444',linewidth=.7)
        months=sorted(m('P2')['months']);x=np.arange(len(months))
        for i,(n,c) in enumerate(zip(names,colors)):
            axs[1,column].bar(x+(i-1.5)*width,[m(n)['months'][s]['net'] for s in months],width,color=c,label=LABELS[n])
        axs[1,column].set_xticks(x,months);axs[1,column].set_title('按退出交易日归属月份');axs[1,column].axhline(0,color='#444',linewidth=.7)
        categories=['全部','删最好5笔','删最好3交易日','删最好3品种'];x=np.arange(4)
        for n,c in zip(names,colors):
            v=m(n);y=[v['stats']['net']]+[v['trim'][unit]['cash']['trim'][str(k)]['without_best']['net_amount'] for unit,k in [('trade',5),('trading_day',3),('symbol',3)]]
            axs[2,column].plot(x,y,'o-',color=c,label=LABELS[n])
        axs[2,column].set_xticks(x,categories);axs[2,column].set_title('事后敏感性：剔除正贡献，非实际过滤器');axs[2,column].axhline(0,color='#444',linewidth=.7)
        for ax in axs[:,column]:ax.grid(axis='y',alpha=.15)
    axs[0,0].legend(ncol=2,fontsize=9)
    fig.suptitle('58个冻结候选：未发现通过全部分组与利润广度要求的新方案\n2026-07-03—10-23 11:05历史研究；一手、无资金上限、扣冻结费用；旧历史无新增独立验证',fontsize=14)
    fig.savefig(RUN/'robustness.png',dpi=150);plt.close(fig)


def bundle():
    folder=RUN/'bundle';folder.mkdir(exist_ok=False)
    files=[SOURCE,SIGNALS,FEES,HOLIDAYS,PROTOCOL,
        ROOT/'crates/n-core/src/bin/signal_robustness.rs',ROOT/'crates/n-core/src/v3/ablation.rs',
        pathlib.Path(__file__),ROOT/'scripts/research_strategy_robustness.py',ROOT/'tests/test_strategy_robustness.py',ROOT/'Cargo.lock']
    manifest={}
    for i,path in enumerate(files):
        dest=folder/(str(i)+'-'+path.name);shutil.copyfile(path,dest)
        manifest[str(path)]=dict(sha256=digest(path),copy=str(dest))
    for name in ('selection.json','validation-calendar-audit.json','verification.json','accepted-calendar-audit.jsonl',
                 'metrics-calendar-audit.csv','products-calendar-audit.csv','periods-calendar-audit.csv','report.md','robustness.png'):
        path=RUN/name;manifest[str(path)]=dict(sha256=digest(path))
    manifest['source_replay']=dict(sha256=digest(RUN/'replay-final/evaluations.jsonl'))
    write(RUN/'artifact-manifest.json',manifest)


if __name__=='__main__':
    data,selection,verification=audit();plot(data);render(data,selection,verification);bundle()
    print(json.dumps(dict(report=str(ROOT/'docs/策略分组验证与拓展研究-2026-10-24.md'),verification=verification['prefix_feature_checks'])))
