"""Present frozen monthly-universe diagnostics, without changing selection rules."""
import collections
import datetime as dt
import json
import pathlib
import sys

sys.dont_write_bytecode=True
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
import research_swing_monthly_products as r
from diagnose_swing_periods import composition


def write_matching(path,value):
    if path.exists():assert r.old.read(path)==json.loads(json.dumps(value)),('Existing diagnostic changed',str(path))
    else:r.old.write(path,value)


def heatmap(ranks,freq):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import TwoSlopeNorm
    plt.rcParams.update({'font.sans-serif':['Microsoft YaHei','SimHei','DejaVu Sans'],'axes.unicode_minus':False})
    syms=sorted((s for s in r.NAMES if freq[s]['n']),key=lambda s:(-freq[s]['score'],freq[s]['bottom3'],-freq[s]['positive_top5'],s))
    vals=np.array([[ranks[m].get(s,{}).get('net',np.nan) for m in r.MONTHS] for s in syms])
    fig,ax=plt.subplots(figsize=(16,11),layout='constrained',facecolor='#f8fafc');ax.set_facecolor('#e2e8f0')
    cmap=plt.get_cmap('RdYlGn').copy();cmap.set_bad('#e2e8f0')
    im=ax.imshow(np.ma.masked_invalid(vals),cmap=cmap,norm=TwoSlopeNorm(vmin=-2500,vcenter=0,vmax=2500),aspect='auto')
    for i in range(len(syms)):
        for j in range(len(r.MONTHS)):
            if np.isfinite(vals[i,j]):ax.text(j,i,f'{vals[i,j]:.0f}',ha='center',va='center',fontsize=8,color='#102030' if abs(vals[i,j])<1700 else 'white')
    ax.set_xticks(range(len(r.MONTHS)),[m[2:] for m in r.MONTHS],rotation=45,ha='right')
    ax.set_yticks(range(len(syms)),[f"{r.NAMES[s]}  前五{freq[s]['top5']} / 后三{freq[s]['bottom3']}" for s in syms])
    ax.set_title('B5 · 每月品种已知净额（元）\n灰色＝没有已知平仓贡献；按预警月份归属，非完整等覆盖账户收益',loc='left',fontsize=15,pad=15)
    ax.set_xticks(np.arange(-.5,len(r.MONTHS),1),minor=True);ax.set_yticks(np.arange(-.5,len(syms),1),minor=True)
    ax.grid(which='minor',color='white',lw=.5);ax.tick_params(which='minor',bottom=False,left=False)
    fig.colorbar(im,ax=ax,shrink=.6,label='净额；颜色在±2500元处饱和，格内数字保留实际金额')
    fig.savefig(r.RUN/'monthly-product-heatmap.png',dpi=160);plt.close(fig)


def run():
    ranks=r.old.read(r.RUN/'rankings.json');freq=r.old.read(r.RUN/'frequencies.json')
    selection=r.old.read(r.RUN/'selection.json');results=r.old.read(r.RUN/'universe-results.json');regimes=r.old.read(r.RUN/'period-diagnosis.json');coverage=r.old.read(r.RUN/'coverage.json')
    # Rebuild the base report before adding interpretation, making reruns stable.
    r.make_report(ranks,freq,selection['full_sample_hindsight'],selection['walk_forward'],results,regimes,coverage)
    subset=[]
    for line in (r.RUN/'accepted-summary.jsonl').open(encoding='utf-8'):
        z=json.loads(line)
        if z['model']=='B5' and z['variant']=='all' and z['evaluation']['status']=='closed':
            subset.append(dict(product=z['symbol'],net_r=z['evaluation']['net_r'],warning_ts=z['warning_ts']))
    earlier=[z for z in subset if z['warning_ts']<'2026-08-01 00:00:00'];recent=[z for z in subset if z['warning_ts']>='2026-08-01 00:00:00']
    decomposition={str(n):composition(earlier,recent,['product'],n) for n in [1,5]}
    write_matching(r.RUN/'matched-product-decomposition.json',decomposition)
    write_matching(r.RUN/'month-total-checks.json',{model:dict(monthly_known_net=sum(v['net'] for cells in rr.values() for v in cells.values()),monthly_known_n=sum(v['n'] for cells in rr.values() for v in cells.values())) for model,rr in ranks.items()})
    # Check every monthly sum against the reproduced full-history baseline.
    for model,rr in ranks.items():
        m=results['original'][model][r.PRIMARY]['all']['all_history']
        assert abs(sum(v['net'] for cells in rr.values() for v in cells.values())-m['net'])<1e-6
        assert sum(v['n'] for cells in rr.values() for v in cells.values())==m['n']
    b=results['original']['B5'][r.PRIMARY];allm=b['all']['evaluation_2026'];walk=b['walk_expanding3']['evaluation_2026']
    strict=results['original']['B5'][r.SUFFIXES[-1]]
    notes=['## 本轮判断','',
        '**方向值得研究，但“十几个月已经足够综合”这个前提不成立。** 反复选择规则与事后选择品种都会产生选择偏差。不是每轮试过的规则都已叠加进B5；B5相对B0实际增加的是不合并摆点、成本预算、A净实体、选择性组并发、两次止损停试。问题既包括规则自由度，也包括反复使用同一历史进行选择。','',
        '**乙二醇是最明确的观察对象；聚丙烯、苯乙烯是次一级备选。** B5中乙二醇13个活跃月、11次正收益前五、1次后三；其他两者分别8/7次正收益前五，但均有3次后三，不能说同样符合“很少落后三”。沥青净额较高但4次后三、最差月份约−4527元；红枣只有15笔、3次前五都集中在2025年10—12月，不能按14个月厚样本对待。','',
        '**按月只看此前结果的筛选，显示了成本和回撤方面的观察价值，同时牺牲主场景净额。** '
        f"2026同覆盖计分段，B5净额{allm['net']:.2f}→{walk['net']:.2f}元，已实现回撤{allm['realized_cash_drawdown']:.2f}→{walk['realized_cash_drawdown']:.2f}元；最严压力净额{strict['all']['evaluation_2026']['net']:.2f}→{strict['walk_expanding3']['evaluation_2026']['net']:.2f}元。平仓数{allm['n']}→{walk['n']}，所以不能把回撤降低全部归因于预测变好，也不是等风险账户比较。",'',
        '**没有必要继续假定更复杂B5一定优于简单B2。** 同一份逐月名单下，B2主场景净额14378.93元、回撤4059元，B5为14258.93元/4451元。这仍是反复研究历史上的内部比较，不是B2已被独立证明更优。','',
        '**近期突出有可观察的成交表现差异，但宏观原因未被证明。** B5在2026年8—10月的平均净R约0.3775，之前约0.0209；胜率46.51%对35.78%，平均赢单约570元对415元，平均亏单约−227元对−226元。不能只用“近期更趋势”一句话解释，也不能断言选对品种后跨期差距就消失。','',
        '建议先冻结策略规则，优先让较简单B2与原B0、B5在同一观察名单上积累新的前瞻记录；不继续围绕当前14个月扫描阈值。EG/PP/EB全部属于现有粗分组中的能源化工，选出3个不等于得到3份独立收益来源。','']
    text=(r.RUN/'summary.md').read_text(encoding='utf-8');split=text.index('## B5每月前五与后三')
    text=text[:split]+'\n'.join(notes)+'\n'+text[split:]
    text+='\n## 排名与样本的额外核对\n\n当一个月只有7个活跃品种时，前五与后三可能重叠，后三也可能是盈利品种（如2026年8月）。本轮同时保留“正收益前五”和“负收益后三”次数；滚动规则仍按研究前保存的全体后三次数扣分，没有根据结果修改规则。无交易既不加前五也不加后三，不能把不活跃当稳定。\n\n'
    text+='2026年7月主体从7月14日起，BU从8月25日、EB/SH从9月16日起；10月截至10月23日的已有窗口。因此各月参与排名的品种数和已知笔数不同，前五占比的机会也不同。\n\n'
    text+='### 共同品种内的平均净R分解（B5，2026年8月前/8—10月）\n\n|共同品种最少笔数/每期|早期覆盖|近期覆盖|子样本平均净R差|品种权重变化项|同品种内部变化项|\n|---|---:|---:|---:|---:|---:|\n'
    for n,d in decomposition.items():text+=f"|{n}|{d['coverage_early']:.2%}|{d['coverage_recent']:.2%}|{d['mean_r_recent']-d['mean_r_early']:.4f}|{d['composition_effect']:.4f}|{d['within_cell_effect']:.4f}|\n"
    text+='\n对共同品种子样本做对称权重分解，两项精确加总为该子样本差额。未覆盖品种不补零；这不是因果识别、不是所有交易差额的完整解释，也没有校正历史反复研究。\n\n[六个基线各月前五/后三完整表](monthly-all-baselines.md) · [月度盈亏热图](monthly-product-heatmap.png)\n'
    text+='\n同品种分解显示，较大的变化来自沥青、塑料、苯乙烯自身的前后段收益变化，品种占比变化不能解释大部分提升。乙二醇在8月前54笔平均净R约0.6067，8—10月19笔约0.4779，反而略降，说明它的优势并不只来自最近三个月；苯乙烯近期只有5笔，个别大赢单影响很大，不应把其阶段均值视为稳定参数。\n'
    text=text.replace('[Bailey等：回测过拟合概率](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)','[Bailey等：统计过拟合与回测表现](https://sdm.lbl.gov/oapapers/ssrn-id2507040-bailey.pdf)')
    text=text.replace('描述性上限参考','事后描述性结果')
    (r.RUN/'summary.md').write_text(text,encoding='utf-8')
    appendix=['# B0—B5各月品种前五及后三','', '相同净额并列共享名次；没有已知平仓的品种不排名；金额按一手已知净额，括号为笔数。','']
    for model,months in ranks.items():
        appendix+=['## '+model,'','|月份|前五|后三|活跃品种数|','|---|---|---|---:|']
        for month,cells in months.items():
            top=sorted(((s,v) for s,v in cells.items() if v['top5']),key=lambda x:(-x[1]['net'],x[0]));bottom=sorted(((s,v) for s,v in cells.items() if v['bottom3']),key=lambda x:(x[1]['net'],x[0]))
            def label(pair):s,v=pair;return f"{r.NAMES[s]} {v['net']:+.2f}（{v['n']}笔）"
            appendix.append('|'+month+'|'+'；'.join(map(label,top))+'|'+'；'.join(map(label,bottom))+f'|{len(cells)}|')
        appendix+=['']
    (r.RUN/'monthly-all-baselines.md').write_text('\n'.join(appendix)+'\n',encoding='utf-8')
    heatmap(ranks['B5'],freq['B5'])
    # Financial timing checks: a future-settled result must not alter earlier lists.
    fixture=[]
    symbols=['EG0','PP0','EB0','BU0','CJ0','L0','MA0','SF0']
    for month in ['2025-09','2025-10','2025-11','2025-12']:
        for symbol,cash in zip(symbols,[100,80,60,40,20,-20,-40,-60]):
            fixture.append(dict(symbol=symbol,warning_ts=month+'-05 10:00:00',multiplier=1,
                result=dict(evaluation=dict(status='closed',exit_ts=month+'-06 10:00:00',net_amount=cash,net_r=cash/100,risk=100))))
    base,_=r.schedule(fixture)
    assert len(base['2026-01'])==3
    future=[dict(symbol='C0',warning_ts=m+'-01 10:00:00',multiplier=1,
        result=dict(evaluation=dict(status='closed',exit_ts='2026-02-01 00:00:00',net_amount=1e9,net_r=1e7,risk=100))) for m in ['2025-09','2025-10','2025-11','2025-12']]
    changed,_=r.schedule(fixture+future)
    assert changed['2026-01']==base['2026-01'] and changed['2026-02']==base['2026-02']
    assert 'C0' in changed['2026-03'] and 'C0' not in base['2026-03']
    absent=r.frequency(r.month_cells([]))
    assert not r.shortlist(absent) and all(z['top5']==0 and z['bottom3']==0 for z in absent.values())
    original=r.old.read(r.old.RUN/'results.json');controls=0
    for pol in results:
        for model in results[pol]:
            for sf in r.SUFFIXES:
                current=results[pol][model][sf]['all']['evaluation_2026']
                previous=original[pol]['prior_2026'][model+'/'+sf]
                assert all(r.report.equal(current[k],v) for k,v in previous.items() if k in current),(pol,model,sf,'2026 projection differs')
                controls+=1
    qa_path=r.RUN/'temporal-ranking-checks.json'
    qa=dict(future_huge_profit_does_not_change_prior_selection=True,exit_at_month_start_is_not_yet_visible=True,no_trade_does_not_gain_rank=True,known_monthly_totals_reconcile=True,same_2026_evaluation_controls_exact=controls)
    qa_path.write_text(json.dumps(qa,indent=2),encoding='utf-8')
    done=r.old.read(r.RUN/'completion.json')
    done.update(finalized_at=dt.datetime.now(dt.timezone.utc).isoformat(),sources={str(pathlib.Path(__file__).resolve()):r.old.sha(__file__),str(pathlib.Path(r.__file__).resolve()):r.old.sha(r.__file__),str(REPO/'scripts/diagnose_swing_periods.py'):r.old.sha(REPO/'scripts/diagnose_swing_periods.py')})
    done['artifacts']={f.name:r.old.sha(f) for f in r.RUN.iterdir() if f.is_file() and f.name!='completion.json'}
    (r.RUN/'completion.json').write_text(json.dumps(done,ensure_ascii=False,indent=2),encoding='utf-8')
    assert all(r.old.sha(r.RUN/f)==h for f,h in done['artifacts'].items())
    print(json.dumps(decomposition,ensure_ascii=False),flush=True)


REPO=r.REPO
if __name__=='__main__':run()
