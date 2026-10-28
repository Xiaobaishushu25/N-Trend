"""Publish causal code findings, counterexamples and deterministic case illustrations."""
import bisect,collections,copy,datetime as dt,json,math,pathlib,statistics
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import research_swing_abstop as s
from report_swing_abstop import summarize
RUN=s.RUN

def run():
    p=s.read(RUN/'protocol.json');fees=s.read(p['periods']['recent']['fees']);cohorts={(q,c['label'],c['event_key']):c for q in ('early','recent') for c in s.read(RUN/(q+'-cohorts.json'))}
    recent_diag=collections.defaultdict(list);recent_rec=collections.defaultdict(list);choices={}
    for q in ('early','recent'):
        cal=s.TradingDates(s.read(p['periods'][q]['input'])['symbols'])
        for line in (RUN/(q+'-diagnostics.jsonl')).open(encoding='utf-8'):
            z=json.loads(line);c=cohorts[(q,z['label'],z['event_key'])];aug=c['baseline']['warning_ts']>='2026-08-01 00:00:00'
            if z['kind']=='recovery':
                if q=='recent' and aug:recent_rec[(z['label'],z['recovery']['horizon'])].append(z['recovery'])
                rr=z['recovery'];category=rr['one_r_vs_two_r']
                if z['label']=='reference' and rr['horizon']==8 and rr['quality']=='ok' and (q=='early' or aug) and category in ('recovery_first','two_r_adverse_first'):
                    key=(q,category)
                    if key not in choices or c['baseline']['warning_ts']<choices[key]['baseline']['warning_ts']:choices[key]=c
            elif z['kind']=='diagnostic' and q=='recent' and aug:
                r=copy.deepcopy(c['baseline']);r['result']=z['result'];r=s.reprice(r,fees,cal);r['frozen_risk']=z['fixed_risk']
                if s.ev(r)['status']=='closed':r['exit_trading_day']=cal.day(s.ev(r)['exit_ts'])
                recent_diag[(z['label'],z['scenario'])].append(r)
    rec3={str(k):dict(n=len(v),quality_ok=sum(r['quality']=='ok' for r in v),complete=sum(r['complete'] for r in v),
        return_entry=sum(r['entry_recovery'] is not None for r in v),return_one_r=sum(r['one_r_recovery'] is not None for r in v),one_r_vs_two_r=dict(collections.Counter(r['one_r_vs_two_r'] for r in v))) for k,v in recent_rec.items()}
    s.write(RUN/'recent-aug-oct-diagnostics.json',dict(fixed_entry={str(k):summarize(v) for k,v in recent_diag.items()},post_stop=rec3))
    # Four illustrations selected by first warning timestamp in each path group,
    # never by maximum profit/loss or visual attractiveness.
    plt.rcParams.update({'font.family':'Microsoft YaHei','axes.unicode_minus':False,'font.size':9})
    fig,axes=plt.subplots(2,2,figsize=(15,9));fig.suptitle('A/B端点与止损后的两类路径（按类别最早预警选例，不代表发生频率）',fontsize=14)
    examples=[]
    for q in ('early','recent'):
        selected=[(key,c) for key,c in choices.items() if key[0]==q];inp=s.read(p['periods'][q]['input']);contracts=s.read(p['periods'][q]['contracts'])
        for key,c in selected:
            category=key[1];ax=axes[0 if q=='early' else 1,0 if category=='recovery_first' else 1];e=c['event'];r=c['baseline'];v=s.ev(r);sgn=1 if e['direction']=='up' else -1
            raw=inp['symbols'][e['symbol']]['bars'];before=[b for b in s.aggregate15(raw) if e['s0_ts']<=b['datetime']<=e['warning_ts']]
            minute=contracts[r['execution_contract']]['minutes'];times=[b['datetime'] for b in minute];a=bisect.bisect_right(times,e['warning_ts']);stop=bisect.bisect_left(times,v['exit_ts']);later=minute[a:min(len(minute),max(stop+135,a+135))]
            # The generic aggregator preserves the final partial candle; chart it
            # only if its end label was actually observed.
            after=s.aggregate15(later) if later else [];bars=before+after;labels=[b['datetime'] for b in bars];idx={t:i for i,t in enumerate(labels)}
            for i,b in enumerate(bars):
                color='#14875d' if b['close']>=b['open'] else '#bf423a';ax.vlines(i,b['low'],b['high'],color=color,lw=.8)
                ax.add_patch(Rectangle((i-.32,min(b['open'],b['close'])),.64,max(abs(b['close']-b['open']),r['tick_size']*.18),facecolor=color,edgecolor=color,lw=.5))
            for field,price in [('s0_ts',e['s0_price']),('s1_ts',e['s1_price']),('s2_ts',e['s2_price'])]:
                if e[field] in idx:ax.annotate(field[:2].upper(),(idx[e[field]],price),xytext=(0,12 if sgn>0 else -16),textcoords='offset points',ha='center',fontsize=8)
            for level,color,text in [(v['fill'],'#333333','入场'),(r['stop'],'#d62728','原止损'),(c['mechanism']['true_b_stop'],'#8c56b4','B段极值止损'),(r['target'],'#1976bd','目标')]:
                ax.axhline(level,color=color,lw=.85,ls='--',label=f'{text} {level:g}')
            if e['warning_ts'] in idx:ax.axvline(idx[e['warning_ts']],color='#b58024',ls=':',lw=1)
            for timestamp,price,marker,color in [(v['entry_ts'],v['fill'],'^','#222222'),(v['exit_ts'],v['final_price'],'x','#d62728')]:
                t=dt.datetime.fromisoformat(timestamp);bucket=(t+dt.timedelta(minutes=(-t.minute)%15)).isoformat(sep=' ')
                if bucket in idx:ax.scatter(idx[bucket],price,marker=marker,color=color,s=34,zorder=5)
            xticks=sorted({round(j*(len(bars)-1)/4) for j in range(5)})
            ax.set_xticks(xticks,[labels[i][5:16] for i in xticks],rotation=15,fontsize=7);ax.grid(alpha=.15);ax.legend(fontsize=7,loc='best')
            title='先恢复至原入场+1R' if category=='recovery_first' else '先继续走到原风险2R不利侧'
            ax.set_title(f'{"上半年" if q=="early" else "8—10月"} {e["symbol"]} {r["warning_ts"][:16]}\n{title}；{e["direction"]}')
            examples.append(dict(period=q,category=category,event_key=c['event_key'],warning=r['warning_ts'],symbol=e['symbol'],selection='earliest warning in fixed8-bucket post-stop category; descriptive only'))
        del contracts,inp
    fig.text(.5,.015,'横轴压缩非交易间隔；15分钟图用于解释结构，实际回放仍用1分钟和双路径。回到+1R不等于扩大止损一定盈利。',ha='center',fontsize=9)
    fig.tight_layout(rect=(0,.04,1,.96));fig.savefig(RUN/'ab-stop-cases.png',dpi=160);plt.close(fig);s.write(RUN/'illustration-selection.json',examples)
    d=s.read(RUN/'descriptive.json');diag=s.read(RUN/'fixed-entry-diagnostics.json');rec=s.read(RUN/'post-stop-recovery.json');mix=s.read(RUN/'composition.json');full=s.read(RUN/'full-portfolio.json');confirm=s.read(RUN/'b-confirmation/results.json');space=s.read(RUN/'structural-space/results.json')
    early=d[str(('early','reference'))];recent=d[str(('recent_aug_oct','reference'))]
    trades=s.read(RUN/'feature-trades.json');payoff={}
    for q in ('early','recent'):
        rr=[s.ev(x['row'])['net_r'] for x in trades if x['period']==q and x['label']=='reference'];w=statistics.mean(x for x in rr if x>0);loss=-statistics.mean(x for x in rr if x<0)
        payoff[q]=dict(n=len(rr),mean_winner_r=w,mean_loser_r=-loss,win_rate=sum(x>0 for x in rr)/len(rr),break_even_win_rate=loss/(w+loss))
    s.write(RUN/'realized-payoff.json',payoff)
    lines=['# A/B识别、反转警报与止损：深层机制审计','',
        '结论：当前实现没有保证“B已结束后抓第二波”，也没有保证低风险高胜率同时成立。识别层把当前预警K极值当S2，成交在预警整根K线之外；典型交易已是多小时A/B结构。上半年与近期的差别主要仍发生在相似形态后的价格路径，不能简单归因于A段不够干净或止损统一太窄。',
        '', '本轮没有改生产代码/参数。所有历史均为开发数据；新优化没有通过稳定/收益保留要求，上一轮观察候选strong_kind_session继续保留。以下各表明确区分实际策略、固定入场诊断与全警报池重算。',
        '', '## 1. 代码实际保证了什么','',
        '- event.rs的pattern_for_current_bar将S2设置为当前预警K的低/高点，不要求右侧已形成确认的波谷/波峰。S1是可用历史中的区间极值，A/B按评分选取。',
        '- 强反转是吞没前一根实体、实体至少0.25ATR且逆向影线小于范围50%；不要求吞没B段主要反向推动。累积覆盖是另一种较长确认，仍保留。',
        '- A段至少只要求出现1根放宽趋势K；B速度快主要扣分，B衰竭主要加分，并非健康回调的硬保证。更干净的A、更慢的B也未在本批形成跨期正期望保证。',
        '- 入场在预警高/低点外1tick；原止损在当前S2外约max(0.1ATR,1点)，目标按S1或近期已确认结构计算。因此它对本次反转是否能直接延续较敏感。',
        '', '## 2. 小波段在代码里的实际尺度','',
        '近三个月按2026-08-01至10-23有数据区间统计；同时保留原7—10月窗口统计。近期品种起点不齐，不能将其称为完整等覆盖三个月。',
        '', '|参考策略已平仓|1—6月|8—10月实际样本|','|---|---:|---:|',
        f"|笔数|{early['n']}|{recent['n']}|",
        f"|平均A段15分钟根数|{early['mean_a_bars']:.2f}|{recent['mean_a_bars']:.2f}|",
        f"|平均B段根数|{early['mean_b_bars']:.2f}|{recent['mean_b_bars']:.2f}|",
        f"|实际初始止损距离/ATR14|{early['mean_actual_risk_atr']:.2f}|{recent['mean_actual_risk_atr']:.2f}|",
        f"|入场已恢复整个B幅度的比例（均值）|{early['mean_actual_entry_recovered_b']:.1%}|{recent['mean_actual_entry_recovered_b']:.1%}|",
        f"|实际成交到计划目标/初始风险|{early['mean_actual_target_r']:.2f}|{recent['mean_actual_target_r']:.2f}|",
        f"|止损仍在整个已知B价格区间内|{early['stop_inside_b_count']}/{early['n']}|{recent['stop_inside_b_count']}/{recent['n']}|",
        '', 'A/B根数对应多小时交易时间，不能把一个短暂反向K自动当成整个回调结束。均值约36%的B幅度已经在入场前恢复，实际止损约1.2ATR，并不是接近已知完整B低点/高点的极小结构风险。该位置仍可以是有效战术入场，但需要它自身的条件概率支撑。',
        '', '目标空间约2.20R不等于每个盈利单能赚2.20R。当前参考上半年实际平均盈利单+1.591R、平均亏损单−1.028R，盈亏平衡胜率约39.3%，实际为34.1%；近期原窗口平均盈利单+1.754R、平均亏损单−0.932R，盈亏平衡胜率约34.7%，实际为42.4%。这是兑现后的账面恒等式，不独立解释原因；上面的端点、位置和下面的固定入场路径检验用来解释理想目标为什么没有兑现。',
        '', '## 3. 止损太窄能否解释亏损','',
        '固定同一批已接纳入场、实际成交价、绝对目标，仅放宽止损。为隔离止损，关闭无跟随与会话利润退出，保留60桶、周末/长假/缺失处理。因此本表基线不能与实际策略−21269元直接混用。R同时按新止损和原风险展示，防止通过扩大分母让R显得更好。',
        '', '|上半年固定入场诊断|盈利率|净额|按原风险平均R|按新风险平均R|','|---|---:|---:|---:|---:|']
    for case,title in [('stop1_h60','原止损'),('stop1.5_h60','1.5倍风险距离'),('stop2_h60','2倍风险距离'),('stop0_h60','B段真实极值外')]:
        m=diag[str(('early','reference',case))];lines.append(f"|{title}|{m['win_rate']:.1%}|{m['net']:.2f}|{m['mean_r_original_denominator']:.4f}|{m['mean_r']:.4f}|")
    lines+=['', '放宽会提高盈利率，但上半年净亏变大。2倍止损按新分母的平均R看似改善，按同一原风险却从约−0.196变成−0.272。不能把这种分母变化视作真实收益改善。',
        '', '在实际255笔上半年止损单中，止损后最多8个被观察15分钟桶内（含首个残桶）：121笔回过原入场价，57笔到过原入场+1R；但只有47笔在先承受2R逆向之前到达+1R。133笔先到2R不利侧、75笔两者都没到。回到原入场还没有覆盖费用，跨会话恢复也不能当作立即启动第二波。止损原分钟不计入恢复，避免OHLC内先后顺序不明造成伪成功。',
        '', '同期近期原窗口81笔止损单，8桶内39笔回入场、26笔到+1R；23笔先回+1R而未先触2R不利侧。两期回到入场价的比例很接近，说明“上半年只是更容易扫损”不足以解释时期差距。完整4/8/16桶、8—10月子样本及缺失/删失见post-stop-recovery.json和recent-aug-oct-diagnostics.json。',
        '', '## 4. 能不能通过抓更小波段提高胜率','',
        '固定同一批参考入场、原止损，目标改为0.5/1/2个原风险，最多16桶，关闭无跟随。它是兑现尺度诊断，未做净空间重选或机会竞争；结果不是可部署策略收益。',
        '', '|固定目标|上半年盈利率|上半年平均净R|近期原窗口盈利率|近期平均净R|','|---|---:|---:|---:|---:|']
    for target in ('0.5','1','2'):
        e=diag[str(('early','reference',f'target{target}_h16'))];n=diag[str(('recent','reference',f'target{target}_h16'))]
        lines.append(f"|{target}R|{e['win_rate']:.1%}|{e['mean_r']:.4f}|{n['win_rate']:.1%}|{n['mean_r']:.4f}|")
    lines+=['', '0.5R止盈对应1R止损，若只考虑理想两种结局，零费用盈亏平衡胜率为2/3；费用、滑点和跳空提高要求。上半年即使盈利率做到约61%，仍明显负期望。因此“能抓到一小段”和“这段足以支付完整止损”是两个不同要求。',
        '', '## 5. A/B与警报精度是不是主要差异','',
        '上半年A段净实体方向效率均值0.444，8—10月0.539，确有变弱；排除S0整根实体后仍分别约0.482/0.560。但按A实体效率>0.5筛成较干净组，该组上半年仍−0.217R，近期+0.221R；不能直接加“干净A”的硬过滤就认定修复。',
        '', '上半年约42.5%的参考交易，B净推进速度快于A；近期8—10月约39.6%。不过快B组上半年−0.112R、近期+0.343R，并非只要B快就一定该删。B衰竭组在两期相对更好，但上半年仍为负。',
        '', f"按A净实体效率与B/A速度四个粗组做对称分解，形态构成变化约贡献{mix['A_B']['composition_effect']:.4f}R，组内收益变化约{mix['A_B']['within_cell_effect']:.4f}R。这是粗分组描述，不是因果比例，也不证明其他A/B信息没有价值；它表明同样粗形态在不同阶段仍走出不同后续路径。",
        '', '强反转组上半年约−0.199R，8—10月约+0.083R；累积覆盖组约−0.026R/+0.284R。当前“strong”标签主要描述形态，本身没有提供高成功率保证。',
        '', '## 6. 已实际测试的优化','',
        '|每边1tick完整机会竞争|上半年净额|近期原窗口净额|结论|','|---|---:|---:|---|']
    for name,title in [('P2_ab_base','参考'),('P2_ab_b_extreme','止损改整个B极值外'),('P2_ab_adaptive','上一轮观察版'),('P2_ab_adaptive_b_extreme','观察版+B极值止损')]:
        e=full['original']['early'][name+'/corrected_touch/slip1'];n=full['original']['recent'][name+'/corrected_touch/slip1'];lines.append(f"|{title}|{e['net']:.2f}|{n['net']:.2f}|B极值止损未改善上半年|")
    for name,title in [('P2_confirm_anchor','仅B主要实体确认'),('P2_confirm_adaptive_anchor','观察版+B主要实体确认')]:
        e=confirm['original']['early'][name+'/corrected_touch/slip1'];n=confirm['original']['recent'][name+'/corrected_touch/slip1'];lines.append(f"|{title}|{e['net']:.2f}|{n['net']:.2f}|损失约四成近期收益，不晋升|")
    e=space['original']['early']['space_adaptive/corrected_touch/slip1'];n=space['original']['recent']['space_adaptive/corrected_touch/slip1'];lines.append(f"|观察版只过滤结构净空间不足|{e['net']:.2f}|{n['net']:.2f}|2tick收益保留与延迟净R不达标，不晋升|")
    lines+=['', 'B主要实体确认只作用于B尚无衰竭证据、强警报还未覆盖主要反向实体的机会；累计覆盖和影线警报保持。它不要求突破A段高低点。目标继续按原“入场前已知附近结构”规则计算，入场门槛变动也可能使所选目标层级变动，不能把全部变化归于确认本身。',
        '', '## 7. 接下来如何优化才贴近原逻辑','',
        '优先把识别语义分清：原预警S2是战术反转点，B段已知极值是结构边界，确认B结束是尚待检验的状态。研究产物已同时记录这三类信息；不能用战术小止损算出的高目标R，代替对整个B结构风险的判断。',
        '', '本轮统一放宽止损、统一缩短目标、更硬B确认均未解决跨期问题。当前保留上一轮观察版；后续应针对“同一A/B是否仍在延续原B推动、何时从反弹转为二波启动”的状态转移建立可前推记录，使用完整覆盖的新时期检验。需要重新提取和竞争候选波段时，应全量重放，不能在已选交易上事后更换A/B起终点。',
        '', '这不要求市场先走出大趋势。它要求进入的小段方向运动相对噪声、止损和成本有足够持续性。小波段策略也会受这种局部路径变化影响；本批不能证明产业、宏观或微观参与者造成该变化的原因。',
        '', '## 8. 路径图与边界','',
        '四例按时期及止损后路径类别的最早预警选择，仅解释机制，不代表胜败频率。',
        '', '![A/B与止损路径](ab-stop-cases.png)',
        '', '参考方法：[Lo、Mamaysky、Wang的技术形态研究](https://www.nber.org/papers/w7613)通过比较形态条件下的收益分布与无条件分布检验信息增量。其股票样本不能证明本期货策略有效，本报告的结论来自本地回放。',
        '', '附带字段审计：event.rs将15分钟prefix传入analyze_60m，产生的trend_state被写入setup_features.trend60_state，存在周期命名/输入混淆。本批参考筛选未使用该字段；上一轮观察候选使用的是独立、按已完成60分钟柱提取的market_context.trend_alignment_60。因此不能拿这个误名字段解释本批时期盈亏，后续接模型或界面前应统一语义。当前未更改生产字段。',
        '', '核验：1321笔实际成交/费用完整复刻；22451条已平仓诊断成交价/时刻一致；原参考与观察版16个完整组合场景回归一致。7136个预警全部可定位锚点，其中1个近期预警A/B根数与当前重聚合不完全一致，已单列；所用新结构极值仍只来自预警前及当前已收盘B。5项Rust缺失/午休/双路径顺序测试、8项Python特征/因果确认/费用假设测试通过。169个生产文件哈希未变。',
        '', '全量协议、原始诊断、分组、构成分解、完整组合及失败实验均保留。固定入场诊断未重选信号、未施加新净空间闸门、未重算机会竞争；完整策略对照则全部重算，二者不得相加或混称策略收益。']
    (RUN/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    assert all(s.sha(k)==h for k,h in p['production_before'].items())
    for root in (RUN,RUN/'b-confirmation'):assert all(s.sha(root/'fork'/k)==h for k,h in s.read(root/'implementation-manifest.json').items())
    s.write(RUN/'completion.json',dict(status='mechanism_round_complete_no_upgrade',qualified_for_trading=False,production_changed=False,
        source_controls_exact=1321,closed_diagnostic_entries_exact=22451,raw_diagnostic_scenarios=22457,
        tests=dict(rust=5,python=8),production_unchanged=169,
        artifacts={str(q.relative_to(RUN)):s.sha(q) for q in [RUN/'report.md',RUN/'ab-stop-cases.png',RUN/'illustration-selection.json',RUN/'recent-aug-oct-diagnostics.json',RUN/'full-portfolio.json',RUN/'b-confirmation/results.json',RUN/'structural-space/results.json']}))
    print('Report and deterministic case charts:',RUN/'report.md',flush=True)

if __name__=='__main__':run()
