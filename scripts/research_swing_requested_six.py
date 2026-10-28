"""User-specified fixed EG/PP/V/JD/SR/CJ universe and one pair-risk comparison."""
import argparse
import bisect
import collections
import datetime as dt
import inspect
import json
import math
import pathlib
import statistics
import sys

sys.dont_write_bytecode=True
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
import research_swing_monthly_products as m

REPO=m.REPO
RUN=REPO/'research-runs/swing-requested-six-products-20261026-v1'
SYMBOLS=['EG0','PP0','V0','JD0','SR0','CJ0']


def prepare():
    RUN.mkdir(exist_ok=False)
    parent=m.old.read(m.RUN/'protocol.json')
    p=dict(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),symbols=SYMBOLS,
        selection='Exactly the six products requested by the user; fixed for all months; no monthly top3 rule, no thresholds or position increase.',
        versions=list(m.report.MODELS),modes=m.SUFFIXES,policies=['original','two_paths_flat'],
        primary='B5/original/corrected_touch/slip1; B2 and all other baselines retained.',
        pair_comparison='Separate B5 risk observation ONLY: EG and PP together at most one same-direction accepted reservation, including pending/unknown. First trigger wins; same-minute exit does not release. Opposite directions remain allowed. No outcome-based priority or forced liquidation.',
        returns='2026 observed15:00 main-contract closes. Log close-to-close returns; drop source gaps and roll-crossing intervals. Correlate returns, never price levels. Report Jan-Jun/Jul-Oct and complete-window pairs; not a forecast of future correlation.',
        inputs_sha256=m.old.sha(m.old.RUN/'completion.json'),script_sha256=m.old.sha(__file__),
        production_before={k:m.old.sha(k) for k in parent['production_before']},
        fixed_definitions=parent['frozen_references'],production_changed=False,qualified_for_trading=False)
    m.old.write(RUN/'protocol.json',p)
    print('Frozen user six-product universe; EG/PP pair rule saved separately.',flush=True)


def pair_selector(controller):
    source=inspect.getsource(controller.select)
    before="why=('symbol_slot' if row['symbol'] in symbols else"
    after="""why=('symbol_slot' if row['symbol'] in symbols else
             'EG_PP_same_direction_slot' if row['symbol'] in ('EG0','PP0') and any(a['row']['symbol'] in ('EG0','PP0') and a['direction']==row['direction'] for a in active.values()) else"""
    assert source.count(before)==1
    source=source.replace(before,after).replace('def select(','def select_pair(',1)
    space=dict(vars(controller));exec(compile(source,'<frozen-controller-plus-EGPP-slot>','exec'),space)
    (RUN/'pair-controller.py').write_text(source,encoding='utf-8')
    return space['select_pair']


def pair_audit(accepted,ledger):
    rows={z['event_key']:z for z in accepted};ls=[l for l in ledger if l['accepted']];pairs=0;same=0
    for i,l in enumerate(ls):
        r=rows[l['event_key']]
        if r['symbol'] not in ['EG0','PP0']:continue
        for prior in ls[:i]:
            p=rows[prior['event_key']]
            if p['symbol'] in ['EG0','PP0'] and p['symbol']!=r['symbol'] and prior['end']>=l['start']:
                pairs+=1;same+=p['direction']==r['direction']
    return dict(overlapping_reservation_pairs=pairs,same_direction_pairs=same)


def correlation():
    """Source input already contains prices; no new market-data downloads."""
    import numpy as np
    pp=m.old.read(REPO/'research-strategies/best-a-portfolio-observation-v1/protocol.json')
    closes={s:{} for s in SYMBOLS+['EB0']};rolls=collections.defaultdict(set);all_dates=set();conflicts=[];sources={}
    for period in ['early','recent']:
        path=pp['periods'][period]['input'];sources[path]=m.old.sha(path)
        inp=m.old.read(path)
        for sym,z in inp['symbols'].items():
            if sym not in closes:continue
            for bar in z['bars']:
                ts=bar['datetime']
                if not '2026-01-01'<=ts[:10]<='2026-10-23' or ts[11:]!='15:00:00':continue
                if ts[:10] in closes[sym] and abs(closes[sym][ts[:10]]-bar['close'])>1e-7:conflicts.append(dict(symbol=sym,date=ts[:10],previous=closes[sym][ts[:10]],current=bar['close']))
                closes[sym][ts[:10]]=bar['close'];all_dates.add(ts[:10])
            rolls[sym].update(z['ts'] for z in z['rollovers'] if z['confirmed'])
    assert not conflicts,('Overlapping source close conflicts',conflicts[:5])
    dates=sorted(all_dates);returns={s:{} for s in closes};audit={}
    for sym,series in closes.items():
        skipped=collections.Counter();rs=sorted(rolls[sym])
        for previous,current in zip(dates,dates[1:]):
            if previous not in series or current not in series:skipped['missing_adjacent_close']+=1;continue
            if bisect.bisect_right(rs,current+' 15:00:00')>bisect.bisect_right(rs,previous+' 15:00:00'):skipped['roll_crossing']+=1;continue
            assert series[previous]>0 and series[current]>0
            returns[sym][current]=math.log(series[current]/series[previous])
        audit[sym]=dict(day_closes=len(series),return_observations=len(returns[sym]),first=min(series),last=max(series),skipped=dict(skipped))
    def calc(a,b,start,end):
        days=sorted(d for d in returns[a].keys()&returns[b].keys() if start<=d<=end)
        x=[returns[a][d] for d in days];y=[returns[b][d] for d in days]
        c=float(np.corrcoef(x,y)[0,1]) if len(days)>2 else None
        rolling=[float(np.corrcoef(x[i-39:i+1],y[i-39:i+1])[0,1]) for i in range(39,len(days))]
        return dict(n=len(days),first=days[0] if days else None,last=days[-1] if days else None,pearson=c,
            rolling40_min=min(rolling) if rolling else None,rolling40_max=max(rolling) if rolling else None)
    pairs={}
    for i,a in enumerate(closes):
        for b in list(closes)[i+1:]:
            pairs[a+'/'+b]={label:calc(a,b,start,end) for label,start,end in [('all2026','2026-01-01','2026-10-23'),('JanJun','2026-01-01','2026-06-30'),('JulOct','2026-07-01','2026-10-23')]}
    m.old.write(RUN/'market-return-correlations.json',dict(pairs=pairs,coverage=audit,sources=sources,overlap_conflicts=conflicts,
        note='Daily main-contract price-return correlation after removing roll-crossing and missing-adjacent-day intervals; excludes incomplete final day. Not strategy PnL correlation or intraday/constant correlation.'))
    return pairs,audit


def run():
    p=m.old.read(RUN/'protocol.json');assert m.old.sha(__file__)==p['script_sha256']
    controller,pp,events,ends,data,sources,coverage=m.load();pair=pair_selector(controller)
    reference=m.old.read(m.old.RUN/'results.json');results={};checks=[];attribution={};ledgers={};accepted={};pair_checks=[]
    for pol in p['policies']:
        results[pol]={}
        for model,(family,profile,rule) in m.report.MODELS.items():
            results[pol][model]={}
            for sf in m.SUFFIXES:
                rr=data[family][(profile,sf)];results[pol][model][sf]={}
                for variant in ['all','six']+(['six_EGPP_one_direction'] if model=='B5' else []):
                    chosen=rr if variant=='all' else [z for z in rr if z['symbol'] in SYMBOLS]
                    select=pair if variant=='six_EGPP_one_direction' else controller.select
                    aa,ll,risk=select(chosen,events[family],ends[family],pp['sector_map'],rule,pol)
                    m.report.audit(ll,chosen)
                    metrics=m.report.distribution(aa,events[family],m.MONTHS)
                    metrics.update({k:v for k,v in risk.items() if k!='realized_settlements'})
                    results[pol][model][sf][variant]=metrics
                    if variant=='all':
                        expected=reference[pol]['combined'][model+'/'+sf]
                        bad={k:[metrics[k],v] for k,v in expected.items() if k in metrics and not m.report.equal(metrics[k],v)}
                        assert not bad,(pol,model,sf,bad);checks.append(dict(policy=pol,version=model,scenario=sf,exact=True))
                    if model=='B5':
                        pairs=pair_audit(aa,ll)
                        if variant=='six_EGPP_one_direction':assert pairs['same_direction_pairs']==0
                        pair_checks.append(dict(policy=pol,scenario=sf,variant=variant,**pairs))
                    if pol=='original' and sf==m.PRIMARY:
                        accepted[model+'/'+variant]=aa;ledgers[model+'/'+variant]=ll
    for model in m.report.MODELS:
        base={z['event_key']:z for z in accepted[model+'/all'] if m.report.s.ev(z)['status']=='closed'}
        chosen={z['event_key']:z for z in accepted[model+'/six'] if m.report.s.ev(z)['status']=='closed'}
        common=base.keys()&chosen.keys();added=chosen.keys()-base.keys();removed=base.keys()-chosen.keys()
        assert all(base[k]['result']==chosen[k]['result'] and base[k]['cost_repricing']==chosen[k]['cost_repricing'] for k in common)
        old_net=sum(m.report.s.ev(base[k])['net_amount'] for k in removed);new_net=sum(m.report.s.ev(chosen[k])['net_amount'] for k in added)
        delta=results['original'][model][m.PRIMARY]['six']['net']-results['original'][model][m.PRIMARY]['all']['net']
        assert abs(delta-(new_net-old_net))<1e-6
        attribution[model]=dict(common_closed=len(common),removed_n=len(removed),removed_original_net=old_net,new_n=len(added),new_net=new_net,net_delta=delta)
    cells={k:m.month_cells(rs) for k,rs in accepted.items()}
    product_stats={}
    for key,rs in accepted.items():
        family=m.report.MODELS[key.split('/')[0]][0]
        product_stats[key]={s:m.report.distribution([z for z in rs if z['symbol']==s],events[family],m.MONTHS) for s in SYMBOLS}
    pairs,market_coverage=correlation()
    for name,obj in [('results.json',results),('monthly-product.json',cells),('product-statistics.json',product_stats),('attribution.json',attribution),('pair-overlap-audit.json',pair_checks),('opportunity-ledgers.json',ledgers),('verification.json',dict(exact_controls=len(checks),controls=checks,sources=sources,raw_pool_recompetition=True,unchanged_common_execution=True))]:m.old.write(RUN/name,obj)
    with (RUN/'accepted.jsonl').open('x',encoding='utf-8') as f:
        for key,rows in accepted.items():
            for row in rows:f.write(json.dumps(dict(case=key,row=row),ensure_ascii=False)+'\n')
    report(results,cells,product_stats,pairs,market_coverage,pair_checks,attribution)
    chart(results,cells)
    for path,h in p['fixed_definitions'].items():
        root=pathlib.Path(path);assert m.old.sha(root/'manifest.json')==h
        assert all(m.old.sha(root/k)==v for k,v in m.old.read(root/'manifest.json').items())
    changed=[k for k,h in p['production_before'].items() if m.old.sha(k)!=h]
    assert not changed,('Concurrent production changes',changed)
    m.old.write(RUN/'completion.json',dict(status='complete',controls_exact=len(checks),production_changed=False,
        artifacts={f.name:m.old.sha(f) for f in RUN.iterdir() if f.is_file()},sources={str(pathlib.Path(__file__).resolve()):m.old.sha(__file__)}))
    print(json.dumps(dict(B5=results['original']['B5'],B2=results['original']['B2'],correlation=pairs.get('EG0/PP0')),ensure_ascii=False),flush=True)


def report(results,cells,stats,pairs,market_coverage,pair_checks,attribution):
    b5=results['original']['B5'][m.PRIMARY]['six'];b2=results['original']['B2'][m.PRIMARY]['six']
    lines=['# 固定六品种回放：EG、PP、PVC、JD、SR、CJ','',
        '日期：2026-10-26。用户指定固定六品种，2025-09至2026-10已有窗口。主表沿用B5；B2简单版及其余版本保留。每个机会一手，扣冻结手续费和每边1tick滑点。不是逐月选3个，也不是把旧接受成交简单求和；重新竞争全部潜在机会。','',
        f"B5六品种已知净额{b5['net']:.2f}元，{b5['n']}笔平仓、胜率{b5['win_rate']:.2%}、平均净R{b5['mean_r']:.4f}，已实现最大回撤{b5['realized_cash_drawdown']:.2f}元，未知{b5['unknown']}笔。B2对应净额{b2['net']:.2f}元、回撤{b2['realized_cash_drawdown']:.2f}元。",'',
        '## 原逐月筛选到底用了什么','',
        '不是按笔数排序。至少有3个出现已知平仓交易的历史月份，累计已知净额为正；按“正收益前五月份数−后三月份数”打分，分数>0时最多取3个，并列按更少后三、更多正前五、代码排序。每月初只读取已严格早于月初退出的影子交易；胜率没有直接参加打分，单笔笔数也不是排序指标。这是一个简单探索规则，尚未解决小样本问题。','',
        '## B5各月已知净额与分品种贡献','', '|月份|'+'|'.join(m.NAMES[s] for s in SYMBOLS)+'|合计|B2合计|B5笔数|','|---|'+'---:|'*(len(SYMBOLS)+3)]
    for month in m.MONTHS:
        row=cells['B5/six'][month]
        vals=[f"{row[s]['net']:.2f}" if s in row else '—' for s in SYMBOLS]
        lines.append('|'+month+'|'+'|'.join(vals)+f"|{b5['warning_month_net'][month]:.2f}|{b2['warning_month_net'][month]:.2f}|{sum(v['n'] for v in row.values())}|")
    lines+=['','“—”为没有已知平仓贡献；合计只加总已知金额，未知不填零。月份归属预警，不是退出月份。2026年近期品种起点不齐、10月未完整；不是14个月等覆盖账户回测。','',
        '## 鸡蛋、PVC、白糖：在本次B5六品种组合中的表现','', '|品种|平仓数|净额|胜率|平均净R|最差预警月|','|---|---:|---:|---:|---:|---:|']
    for sym in ['JD0','V0','SR0']:
        z=stats['B5/six'][sym]
        lines.append(f"|{m.NAMES[sym]}|{z['n']}|{z['net']:.2f}|{z['win_rate']:.2%}|{z['mean_r']:.4f}|{z['worst_warning_month']:.2f}|")
    lines+=['','## 四压力与两种未知占用政策：六品种净额','', '|版本/政策|触价1tick|触价2tick|延迟1tick|延迟2tick|','|---|---:|---:|---:|---:|']
    for model in m.report.MODELS:
        for pol in results:lines.append('|'+model+'/'+pol+'|'+'|'.join(f"{results[pol][model][sf]['six']['net']:.2f}" for sf in m.SUFFIXES)+'|')
    lines+=['','## EG和PP同向最多一个：独立风险对照','',
        '仅新增EG/PP同向占用最多1个，先触发先占名额，原B5其他规则不变。包括待成交订单和未知占用；同一分钟退出仍不释放；反向机会不被这条规则禁止。这不是禁止两个品种出现在同一名单，也没有按照未来收益优先挑赢家。该对照没有部署。','',
        '|政策/场景|原六品种净额|加EG/PP限制净额|原回撤|加限制回撤|','|---|---:|---:|---:|---:|']
    for pol in results:
        for sf in m.SUFFIXES:
            z=results[pol]['B5'][sf];a=z['six'];b=z['six_EGPP_one_direction']
            lines.append(f"|{pol}/{sf}|{a['net']:.2f}|{b['net']:.2f}|{a['realized_cash_drawdown']:.2f}|{b['realized_cash_drawdown']:.2f}|")
    lines+=['','## 产业关系与实际价格相关','',
        'EG有乙烯路线及煤制路线，PP有油制、煤/甲醇制、丙烷脱氢等原料路线；不能全部视为纯原油价格的复制。EB主流路线为纯苯和乙烯制乙苯再脱氢，属于石化相关品种。PVC也有电石法和乙烯法，仍受能源/化工及建材需求影响；不能把PVC自动当成与能化无关。产业关联不等于固定相关系数。','',
        '相关性按2026已有实际主力拼接15:00收盘到下一完整日收盘的对数收益率计算，剔除跨换月区间和缺少相邻共同交易日收盘的区间。未用价格水平相关，未把手续费或策略利润混入价格相关。40期滚动值只作描述，不能直接当未来相关性。','',
        '|品种对|全窗口相关|样本对数|1—6月相关|7—10月相关|40期滚动最小/最大|','|---|---:|---:|---:|---:|---:|']
    for key in ['EG0/PP0','EG0/EB0','PP0/EB0','EG0/V0','PP0/V0','EG0/JD0','EG0/SR0','EG0/CJ0']:
        z=pairs[key];a=z['all2026'];b=z['JanJun'];c=z['JulOct']
        lines.append(f"|{key}|{a['pearson']:.3f}|{a['n']}|{b['pearson']:.3f}|{c['pearson']:.3f}|{a['rolling40_min']:.3f}/{a['rolling40_max']:.3f}|")
    lines+=['','参考：[乙二醇期货交易手册](https://edu.sxslqh.com/uploadfiles/2021/02/20210223172855011.pdf)、[生态环境部煤制烯烃项目说明](https://www.mee.gov.cn/ywdt/gsgg/gongshi/wqgs_1/202604/t20260423_1149932.shtml)、[苯乙烯产业链研究](https://www.htfc.com/wz_upload/png_upload/20190902/1567411972988cedf13.pdf)、[电石工业协会](https://www.cciac.org.cn/detail.htm?id=22947)。','',
        '同产业品种同向开仓可能放大共同风险，但是否完全禁止并发应结合实际信号重叠、资金风险和收益取舍；本研究的一手仓位不是等风险预算，日频相关也不等于15分钟信号相关。','',
        '## 核验与限制','',
        '48个全品种统计对照精确复现；所有新结果重新竞争全部个体机会，已接受同品种占用与实际止损时序核对。EG/PP风险对照独立检查其同向占用重叠为0。共同交易的完整执行和费用保持相同。费用模型未独立重建历史逐日账单；已实现回撤不含持仓浮动权益。','',
        '这批品种由查看历史结果后提出，仍是历史探索，不能视为独立样本外或未来收益保证；未修改实盘。完整结果、月度品种、相关性处理和机会接受/拒绝证据另存JSON/JSONL。']
    (RUN/'summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def chart(results,cells):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams.update({'font.sans-serif':['Microsoft YaHei','SimHei','DejaVu Sans'],'axes.unicode_minus':False})
    fig,axes=plt.subplots(2,1,figsize=(13,8),layout='constrained')
    for model,color in [('B2','#2563eb'),('B5','#0f9488')]:
        z=results['original'][model][m.PRIMARY]['six'];axes[0].plot(range(15),[0]+list(np.cumsum(list(z['warning_month_net'].values()))),label=model,color=color,marker='o',ms=3)
    z=results['original']['B5'][m.PRIMARY]['six_EGPP_one_direction'];axes[0].plot(range(15),[0]+list(np.cumsum(list(z['warning_month_net'].values()))),label='B5：EG/PP同向不并发',color='#c96a25',ls='--')
    axes[0].set_xticks(range(15),['起点']+[s[2:] for s in m.MONTHS],rotation=45);axes[0].set_ylabel('累计已知净额 / 元');axes[0].legend(frameon=False)
    axes[0].set_title('固定六品种 · 按预警月份累计（非浮动权益）',loc='left');axes[0].axhline(0,color='#6b7280',lw=.8)
    x=np.arange(14);width=.4
    for i,(model,color) in enumerate([('B2','#2563eb'),('B5','#0f9488')]):axes[1].bar(x+(i-.5)*width,list(results['original'][model][m.PRIMARY]['six']['warning_month_net'].values()),width,label=model,color=color)
    axes[1].set_xticks(x,[s[2:] for s in m.MONTHS],rotation=45);axes[1].set_ylabel('每月净额 / 元');axes[1].axhline(0,color='#6b7280',lw=.8)
    for ax in axes:ax.grid(axis='y',alpha=.2);ax.spines[['top','right']].set_visible(False)
    fig.savefig(RUN/'six-products-monthly.png',dpi=160);plt.close(fig)


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('stage',choices=['prepare','run']);a=ap.parse_args();globals()[a.stage]()
