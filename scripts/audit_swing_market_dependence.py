"""Market-regime attribution, no strategy tuning or new trading rule."""
import bisect
import collections
import datetime as dt
import json
import math
import pathlib
import statistics as st
import research_swing_specialists as s
from report_swing_wave_selection import identification_bars

RUN=s.ROOT/'research-runs/swing-market-dependence-20261026-v2'
ev=s.m.report.s.ev
read=s.m.old.read

def write(name,z):s.m.old.write(RUN/name,z)

def describe(rs):
    cs=[r for r in rs if ev(r)['status']=='closed']
    vals=[ev(r)['net_amount'] for r in cs]
    return dict(n=len(cs),net=sum(vals),mean_net=st.mean(vals) if vals else None,
                mean_r=st.mean(ev(r)['net_r'] for r in cs) if cs else None,
                wins=sum(v>0 for v in vals),net_ex_best=sum(vals)-max([0]+vals),
                direction={d:dict(n=sum(r['direction']==d for r in cs),
                    net=sum(ev(r)['net_amount'] for r in cs if r['direction']==d)) for d in ['up','down']})

def market():
    pp=read(s.ROOT/'research-strategies/best-a-portfolio-observation-v1/protocol.json')
    returns={sym:{} for sym in s.FOUR};closes={sym:{} for sym in s.FOUR}
    efficiencies={sym:{} for sym in s.FOUR};er15={sym:[] for sym in s.FOUR}
    raw_eg={};splices=[];sources={};skips=collections.Counter()
    for period in ['early','recent']:
        paths=pp['periods'][period]
        inp=read(paths['input']);raw=read(paths['contracts']);segments=read(paths['segments'])
        sources[period]={k:s.m.old.sha(paths[k]) for k in ['input','contracts','segments']}
        raw_closes={c:{b['datetime'][:10]:b['close'] for b in z['minutes'] if b['datetime'][11:]=='15:00:00'} for c,z in raw.items() if any(c.startswith(p) for p in ['DCE.eg','DCE.v','DCE.jd','CZCE.SR'])}
        days=sorted({b['datetime'][:10] for sym in s.FOUR for b in inp['symbols'][sym]['bars'] if b['datetime'][11:]=='15:00:00'})
        for sym in s.FOUR:
            bars=inp['symbols'][sym]['bars'];daily={b['datetime'][:10]:b['close'] for b in bars if b['datetime'][11:]=='15:00:00'}
            closes[sym].update(daily);ss=segments[sym]
            def contract(ts):
                return next((g['underlying'] for g in ss if g['first']<=ts<=g['last']),None)
            local={}
            for previous,current in zip(days,days[1:]):
                c=contract(current+' 15:00:00')
                if c is None or current not in daily or previous not in daily or previous not in raw_closes.get(c,{}):
                    skips[period+'/'+sym]+=1;continue
                a=raw_closes[c][previous];b=raw_closes[c].get(current)
                if not b:continue
                assert abs(b-daily[current])<1e-7
                z=dict(log_return=math.log(b/a),points=b-a,contract=c,previous=previous,period=period)
                returns[sym][current]=z;local[current]=z
            for i,current in enumerate(days):
                window=days[max(0,i-19):i+1]
                if len(window)!=20 or not all(d in local for d in window):continue
                xs=[local[d]['log_return'] for d in window];den=sum(abs(v) for v in xs)
                efficiencies[sym][current]=dict(er20=abs(sum(xs))/den if den else 0.,momentum20=sum(xs),period=period)
            ids=identification_bars(bars)
            for i in range(16,len(ids)):
                bs=ids[i-16:i+1]
                if not '2026-01-01'<=bs[-1]['datetime'][:10]<='2026-10-23':continue
                codes={contract(b['datetime']) for b in bs}
                if len(codes)!=1 or None in codes:continue
                xs=[b['close'] for b in bs];den=sum(abs(b-a) for a,b in zip(xs,xs[1:]))
                er15[sym].append(abs(xs[-1]-xs[0])/den if den else 0.)
            if sym=='EG0' and period=='recent':
                for c in ['DCE.eg2610','DCE.eg2611']:raw_eg[c]=raw_closes.get(c,{})
                for roll in inp['symbols'][sym]['rollovers']:
                    day=roll['ts'][:10];a=raw_closes.get(roll['from_contract'],{}).get(day);b=raw_closes.get(roll['to_contract'],{}).get(day)
                    splices.append(dict(**roll,same_day_old_close=a,same_day_new_close=b,same_time_spread=(a-b if a and b else None)))
                peak=max(bars,key=lambda b:b['high']);write('eg-recent-peak.json',peak)
        del raw,inp
    common=sorted(set.intersection(*[{d for d in efficiencies[sym] if '2026-01-01'<=d<='2026-10-22'} for sym in s.FOUR]))
    stats={}
    for sym in s.FOUR:
        rr={d:z for d,z in returns[sym].items() if '2026-01-01'<=d<='2026-10-22'}
        trend=[efficiencies[sym][d]['er20'] for d in common]
        stats[sym]=dict(return_days=len(rr),roll_adjusted_price_change=math.expm1(sum(z['log_return'] for z in rr.values())),
            common_er20_days=len(common),median_er20=st.median(trend),mean_er20=st.mean(trend),
            median_er_16x15m=st.median(er15[sym]),up_daily_return_days=sum(z['log_return']>0 for z in rr.values()),
            down_daily_return_days=sum(z['log_return']<0 for z in rr.values()),
            median_abs_daily_return=st.median(abs(z['log_return']) for z in rr.values()),
            rolling_up_days=sum(efficiencies[sym][d]['momentum20']>0 for d in common),
            rolling_down_days=sum(efficiencies[sym][d]['momentum20']<0 for d in common))
    out=dict(stats=stats,returns=returns,daily_closes=closes,er20=efficiencies,splices=splices,raw_eg_daily_closes=raw_eg,
             common_er20_dates=common,skips=dict(skips),source_hashes=sources,
             definition='Daily return uses CURRENT main contracts close/current and SAME actual contracts previous-session15:00 close, excluding cross-contract level differences. Reset at source-period gaps. ER20=abs(sum20 daily log returns)/sum(abs20 daily log returns). ER16x15m uses17 closes within one actual-contract segment. Descriptive fixed scales, not optimized gates. Price index excludes fees/margin and is not a tradable benchmark return.')
    write('market.json',out)
    return out

def run():
    RUN.mkdir(exist_ok=False)
    write('protocol.json',dict(created_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
        purpose='Answer whether EG Sep-Oct failed, whether profit is early-long/late-short, and whether other products truly have no trends. No new candidate or trading-rule optimization.',
        strategy='Frozen original B5, primary four-product universe; primary touch1tick plus all8 pressure/unknown combinations.',
        market='Existing2026 input and actual-member contract archives only. No new data request. Do not imply2025 full daily coverage or complete October2026.',
        diagnostics=['warning vs exit month','long/short and post Sep16 observed main-series peak','same-time roll spreads','20day directional efficiency and16x15minute efficiency','past-completed20day trend at warning'],
        caution='Market-regime diagnostics cannot establish incremental pattern alpha over simple momentum, random signals or always-long exposure. No such benchmark backtest claimed.',
        script_sha256=s.m.old.sha(__file__)))
    mk=market()
    root=s.ARCHIVE/'native2026';trades=[]
    for line in (root/'accepted.jsonl').open(encoding='utf-8'):
        z=json.loads(line)
        if z['case']=='four/original/'+s.m.PRIMARY+'/B5' and ev(z['row'])['status']=='closed':trades.append(z['row'])
    eg=[r for r in trades if r['symbol']=='EG0'];out={}
    for month in ['2026-08','2026-09','2026-10']:
        out[month]=dict(warning_month=describe([r for r in eg if r['warning_ts'].startswith(month)]),
                        civil_exit_month=describe([r for r in eg if ev(r)['exit_ts'].startswith(month)]))
    out['post_peak_sep16']=describe([r for r in eg if r['warning_ts']>='2026-09-16 09:30:00'])
    # Re-select the frozen whole14month candidate stream for every pressure case.
    c,pp,events,ends,data,_,_=s.m.load();stress={}
    for pol in ['original','two_paths_flat']:
        for sf in s.m.SUFFIXES:
            rows=[r for r in data['unmerged']['budget_body',sf] if r['symbol'] in s.FOUR]
            aa,ll,_=c.select(rows,events['unmerged'],ends['unmerged'],pp['sector_map'],'sector2_counter_stops',pol)
            s.m.report.audit(ll,rows)
            rr=[r for r in aa if r['symbol']=='EG0']
            stress[pol+'/'+sf]={mo:describe([r for r in rr if r['warning_ts'].startswith(mo)]) for mo in ['2026-09','2026-10']}
            stress[pol+'/'+sf]['post_peak_sep16']=describe([r for r in rr if r['warning_ts']>='2026-09-16 09:30:00'])
    out['pressure']=stress
    out['sep_oct_trades']=[dict(warning=r['warning_ts'],direction=r['direction'],contract=r['execution_contract'],evaluation=ev(r)) for r in eg if r['warning_ts']>='2026-09-01']
    # Causal regime label: only daily closes already completed strictly before warning.
    regimes={}
    for sym in s.FOUR:
        ds=sorted(mk['daily_closes'][sym]);times=[d+' 15:00:00' for d in ds];bins=collections.defaultdict(list)
        for r in trades:
            if r['symbol']!=sym:continue
            at=bisect.bisect_left(times,r['warning_ts'])-1
            if at<0:bins['unavailable'].append(r);continue
            d=ds[at]
            # Do not carry an old-source trend label across the June-July data gap.
            if d not in mk['er20'][sym] or (r['warning_ts']>='2026-07' and d<'2026-07') or (dt.date.fromisoformat(r['warning_ts'][:10])-dt.date.fromisoformat(d)).days>10:
                bins['unavailable'].append(r);continue
            momentum=mk['er20'][sym][d]['momentum20'];label='daily_up' if momentum>0 else 'daily_down' if momentum<0 else 'daily_flat'
            bins[label].append(r)
        regimes[sym]={k:describe(v) for k,v in bins.items()}
    write('strategy.json',dict(EG=out,causal_daily_regimes=regimes))
    report(mk,out,regimes)
    chart(mk,out)
    write('completion.json',dict(complete=True,production_changed=False,artifacts={q.name:s.m.old.sha(q) for q in RUN.iterdir() if q.is_file()}))
    print(json.dumps(dict(EG={k:v for k,v in out.items() if k not in ['pressure','sep_oct_trades']},market=mk['stats'],regimes=regimes),ensure_ascii=False),flush=True)

def report(mk,out,regimes):
    lines=['# EG回落阶段与四品种市场依赖审计','',
        '日期2026-10-26。行情截至2026-10-23 11:05，日收盘截至10月22日；不含月底。策略是原B5四品种、一手、冻结费用。以下按预警月份统计，另已核对自然退出月份本批EG金额相同。',
        '', '## 直接结论','',
        'EG9月和10月主场景已有覆盖均盈利，但9月大部分收益来自上涨段的两笔多单，10月收益主要来自空头且高度依赖一笔盈利。10月两种延迟成交代理均转亏。不能称下跌阶段完全失效，也不能因此宣称已证明稳健的跨状态优势。',
        '', '|月份|平仓|多头净额/笔数|空头净额/笔数|合计净额|去掉最佳交易|', '|---|---:|---:|---:|---:|---:|']
    for mo in ['2026-09','2026-10']:
        z=out[mo]['warning_month'];a=z['direction']['up'];b=z['direction']['down']
        lines.append(f"|{mo}|{z['n']}|{a['net']:.2f} / {a['n']}|{b['net']:.2f} / {b['n']}|{z['net']:.2f}|{z['net_ex_best']:.2f}|")
    lines+=['', '9月9日与10日两笔多单分别盈利1,394、2,144元，合计3,538元；删去两笔后9月只剩166元。10月20日22:17的空单盈利1,404元；删掉后10月为−726元。删赢家仅作集中度诊断，不是策略收益。',
            '', '## 回落起点与换月口径','',
            '回测主力序列本段最高价为9月16日09:30的6,304。以此事后识别峰值作描述，9月16日及之后入场的EG共10笔，净额1,080元（9月402、10月678）。这个峰值是事后标签，没有用于交易或参数筛选。',
            '', '9月28日夜盘从EG2610换到EG2611。当日15:00两个实际合约收盘分别为5,763和5,280，价差483点；21:01分别为5,758和5,257。未复权主力连续线包含跨合约价差，不能把整个拼接跌幅当成同合约市场下跌。实际回测用成员合约成交，不把这段价差当利润。',
            '', '|执行场景（original）|9月净额|10月净额|9月16日峰值之后净额|', '|---|---:|---:|---:|']
    for sf in s.m.SUFFIXES:
        q=out['pressure']['original/'+sf]
        lines.append(f"|{sf}|{q['2026-09']['net']:.2f}|{q['2026-10']['net']:.2f}|{q['post_peak_sep16']['net']:.2f}|")
    lines+=['', '## 其他品种真的没有趋势吗','',
            '按原始实际合约计算日收益：当日主力收盘除以同一个合约的上一交易日收盘，从而去掉主力换月价差；不同来源覆盖段之间不拼收益。20日方向效率＝20日净变动绝对值／20日逐日变动绝对值之和。取值越高表示该尺度越单向，不表示波幅越大，也不证明可获利。未扫描窗口。',
            '', '|品种|日收益样本数|已覆盖段去换月价差价格变动|共同日期20日方向效率中位|16根15分钟方向效率中位|共同日期20日向上/向下窗口数|', '|---|---:|---:|---:|---:|---:|']
    for sym,z in mk['stats'].items():
        lines.append(f"|{s.m.NAMES[sym]}|{z['return_days']}|{z['roll_adjusted_price_change']:.1%}|{z['median_er20']:.3f}|{z['median_er_16x15m']:.3f}|{z['rolling_up_days']} / {z['rolling_down_days']}|")
    lines += ['',f"20日指标比较限定四品种共同可得的{len(mk['common_er20_dates'])}个日期；15分钟指标在各自有效覆盖和同一实际合约窗口内统计。价格变动列覆盖不齐，不能作为同资金同时间策略收益排名。上表只能检查‘有无趋势’这一假设，无法独立解释策略超额收益。",
        '', '## 同时区分信号质量、频率与一手风险','',
        '原18个月EG为88笔、平均净R0.509；鸡蛋为19笔、平均净R0.437；PVC108笔、0.061；白糖35笔、0.088。鸡蛋历史单笔平均净R接近EG，主要差异还包括频率和现金风险，不能说其信号完全无效。PVC和白糖历史优势则很薄，面对执行压力可能转负。',
        '', '原18个月平均实际初始止损现金风险约：EG353元、PVC145元、JD239元、SR162元。一手金额不是等风险；但换成净R后EG与PVC/SR差异依然很大，不能全部归因于合约乘数。',
        '', '### 入场前已知的20日方向与2026交易结果','', '|品种|预警时已完成日线方向|笔数|净额|平均净R|多头/空头净额|', '|---|---|---:|---:|---:|---:|']
    for sym,bins in regimes.items():
        for label,q in bins.items():
            lines.append(f"|{s.m.NAMES[sym]}|{label}|{q['n']}|{q['net']:.2f}|{q['mean_r']:.3f}|{q['direction']['up']['net']:.2f} / {q['direction']['down']['net']:.2f}|")
    lines+=['', '该日线标签仅使用预警前已完成收盘，并对缺失覆盖保留unavailable；不将日内尚未收盘的日K用于判断。仍是事后归因，没有据此新增交易过滤器。',
        '', '## 对“是否只是趋势暴露”的判断','',
        '盈利来自趋势的形成和延续，本身不否定趋势策略。但当前证据只支持某些品种与方向的条件盈利，尚未证明ABC形态相对简单趋势规则有额外信息。EG回落期也能抓到空头，反驳了“纯粹买入持有才赚钱”的强说法；几笔盈利不能排除它只是弱势中的反弹和顺势波段暴露。',
        '', '需要把问题拆成两层：市场在策略交易尺度上是否给出足够且可成交的空间；ABC是否比更简单的入场规则更好地选择了这些空间。跨品种差异可以来自波动、趋势持续性、回撤形态、费用/目标比例、时段与样本稀疏，也可以来自筛选偏差。现有数据不能把差异唯一归为任何一种。',
        '', '下一轮应冻结ABC参数，加入同成交成本、同风险预算、同会话与换月处理的简单动量/突破基准，以及匹配方向和时段的随机入场对照；按品种、上涨/下跌/震荡和先后时间块检验净R、回撤、换手与暴露。若在相同状态和风险下长期不能优于简单基准，就应承认ABC没有证明增量价值；不能继续靠挑品种和微调门槛自证有效。本轮未运行这些新基准，不能将此建议写成检验结论。',
        '', '![实际合约价格与9—10月多空贡献](comparison.png)','', '逐笔成交和压力数据见strategy.json；行情、同合约日收益与窗口定义见market.json。']
    (RUN/'summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')

def chart(mk,out):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.sans-serif':['Microsoft YaHei','SimHei','DejaVu Sans'],'axes.unicode_minus':False})
    fig,axes=plt.subplots(2,1,figsize=(12,8),layout='constrained',height_ratios=[1.6,1],facecolor='#f8fafc')
    ax=axes[0]
    for c,color in [('DCE.eg2610','#2563eb'),('DCE.eg2611','#0d9488')]:
        rs=mk['raw_eg_daily_closes'][c];ds=sorted(d for d in rs if '2026-09-01'<=d<='2026-10-22')
        ax.plot([dt.datetime.fromisoformat(d) for d in ds],[rs[d] for d in ds],label=c,lw=2,color=color)
    for direction,marker,color in [('up','^','#d97706'),('down','v','#7c3aed')]:
        ts=[r for r in out['sep_oct_trades'] if r['direction']==direction]
        ax.scatter([dt.datetime.fromisoformat(r['evaluation']['entry_ts']) for r in ts],[r['evaluation']['fill'] for r in ts],marker=marker,color=color,s=55,label='多头入场' if direction=='up' else '空头入场',zorder=3)
    ax.axvline(dt.datetime(2026,9,28,21,1),ls='--',color='#64748b',alpha=.6,label='回测换月')
    ax.set_title('分别查看实际合约：9月16日高点、回落与9月28日换月',loc='left');ax.set_ylabel('元/吨');ax.legend(frameon=False,ncol=3)
    ax=axes[1];x=[0,1]
    for delta,d,color,label in [(-.18,'up','#d97706','多头'),(.18,'down','#7c3aed','空头')]:
        vals=[out[mo]['warning_month']['direction'][d]['net'] for mo in ['2026-09','2026-10']]
        bs=ax.bar([v+delta for v in x],vals,width=.35,label=label,color=color)
        ax.bar_label(bs,fmt='%.0f元',padding=3)
    ax.set_xticks(x,['9月：合计3704元','10月已有覆盖：合计678元']);ax.set_ylabel('扣费净额 / 元');ax.axhline(0,color='#475569',lw=.8);ax.legend(frameon=False)
    for ax in axes:ax.grid(axis='y',alpha=.15);ax.spines[['top','right']].set_visible(False)
    fig.suptitle('EG回落阶段：主场景仍盈利，10月对成交方式敏感',fontweight='bold',fontsize=17)
    fig.savefig(RUN/'comparison.png',dpi=155);plt.close(fig)

if __name__=='__main__':run()
