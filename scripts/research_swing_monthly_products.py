"""Monthly product rankings and one fixed causal universe-selection diagnostic.

All existing B0--B5 definitions and individual executions remain immutable.
"""
import argparse
import collections
import datetime as dt
import importlib.util
import json
import math
import pathlib
import statistics
import sys

sys.dont_write_bytecode=True
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
import research_swing_older_four as old
import report_swing_older_four as report
from report_swing_c_structure import equal,SUFFIXES
from reprice_signal_costs import TradingDates
from research_swing_consistency import UNKNOWN

REPO=old.REPO
RUN=REPO/'research-runs/swing-monthly-product-selection-20261026-v1'
MONTHS=report.MONTHS+report.OLD_MONTHS
PRIMARY=SUFFIXES[0]
NAMES={'BU0':'沥青','C0':'玉米','CF0':'棉花','CJ0':'红枣','EB0':'苯乙烯','EG0':'乙二醇','FG0':'玻璃','JD0':'鸡蛋','L0':'塑料','MA0':'甲醇','PB0':'沪铅','PP0':'聚丙烯','RB0':'螺纹钢','SA0':'纯碱','SF0':'硅铁','SH0':'烧碱','SI0':'工业硅','SM0':'锰硅','SR0':'白糖','SS0':'不锈钢','UR0':'尿素','V0':'PVC'}


def prepare():
    RUN.mkdir(exist_ok=False)
    op=old.read(old.RUN/'protocol.json')
    p=dict(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),months=MONTHS,
        versions=list(report.MODELS),primary=dict(policy='original',scenario=PRIMARY),
        ranking='Known closed single-lot net by warning month; active=at least one known closed trade. Zero-trade/missing-data months never enter ranks. Shared competition ranks on cash rounded8 decimals; include all cutoff ties.',
        top5='Cash descending rank<=5; also separately count positive-net top5 months.',
        bottom3='Cash ascending rank<=3 among active products; separately count negative-net bottom3 months.',
        candidate_rule=dict(shadow_version='B5',max_products=3,min_active_months=3,require_positive_cumulative_known_cash=True,
            score='positive_top5_months minus bottom3_months; require score>0',
            tie_order=['score descending','bottom3 count ascending','positive_top5 count descending','symbol alphabetical']),
        temporal_rule='At each2026calendar-month start, use ONLY B5 original/primary SHADOW accepted closed exits strictly before month start, warning month earlier than month start. Recompute historical monthly ranks from visible exits. No fixed list until all months are observed.',
        actual_rule='Apply chosen list at WARNING time; existing accepted orders/positions continue unchanged. No month reset of exposure or stop history. Keep independent all-product shadow strategy active even when not selected.',
        tests=['all products control','full-sample fixed top3 hindsight list (descriptive only)','expanding-month top3 using past visible shadow exits'],
        stress='One primary-case list/schedule applied unchanged to both unknown policies and all four execution stresses; do not optimize lists per stress.',
        evaluation_start='2026-01-01 00:00:00',selection_rule_fitted=False,
        disclosure='Threshold3 active months/top3 fixed before this batch outcomes; an exploratory rule proposed after previous strategy results. Walk-forward removes universe-selection lookahead only; underlying B5 was fitted on these histories. NOT independent strategy out-of-sample proof.',
        production_before=op['production_before'],frozen_references=op['frozen_references'],
        sources=dict(older_completion=old.sha(old.RUN/'completion.json'),script_sha256=old.sha(__file__)),
        production_changed=False,qualified_for_trading=False)
    old.write(RUN/'protocol.json',p)
    print('Frozen monthly rankings and one expanding top3 rule; no parameter grid.',flush=True)


def load():
    p=old.read(old.RUN/'protocol.json');fees=old.read(p['fees_path'])
    portfolio_root=REPO/'research-strategies/best-a-portfolio-observation-v1'
    pp=old.read(portfolio_root/'protocol.json')
    spec=importlib.util.spec_from_file_location('monthly_frozen_controller',portfolio_root/'controller.py');controller=importlib.util.module_from_spec(spec);spec.loader.exec_module(controller)
    events={};ends={};data={};sources={};coverage={}
    calendar=TradingDates({});calendar.dates=old.read(old.RUN/'trading-dates.json')
    for family in ['best_a','unmerged']:
        root=old.RUN/family
        events[family]={z['event_key']:z['event'] for z in old.read(root/'older-pool.json')}
        ends[family]=old.read(root/'older/manifest.json')['contract_ends']
        path=root/'older/evaluations.jsonl';sources[str(path)]=old.sha(path)
        data[family]=report.row_cases(path,family,'older',fees,calendar)
    native=REPO/'research-runs/swing-entry-economics-20261025-v1/native-verification'
    for period in ['early','recent']:
        inp=old.read(pp['periods'][period]['input']);cc=TradingDates(inp['symbols'])
        for sym,z in inp['symbols'].items():
            bins=collections.Counter(b['datetime'][:7] for b in z['bars'])
            days=collections.defaultdict(set)
            for b in z['bars']:
                if '09:00:00'<=b['datetime'][11:]<='15:00:00':days[b['datetime'][:7]].add(b['datetime'][:10])
            coverage[period+'/'+sym]=dict(first=z['bars'][0]['datetime'],last=z['bars'][-1]['datetime'],bars5=dict(bins),day_sessions={m:len(v) for m,v in days.items()})
        for family in data:
            root=REPO/'research-runs/swing-retirement-20261025-v1/near_start' if family=='best_a' else native
            events[family].update({z['event_key']:z['event'] for z in old.read(root/(period+'-pool.json'))})
            for code,end in old.read(root/period/'manifest.json')['contract_ends'].items():ends[family][code]=max(end,ends[family].get(code,''))
            path=root/period/'evaluations.jsonl';sources[str(path)]=old.sha(path)
            for key,rows in report.row_cases(path,family,period,fees,cc).items():data[family][key].extend(rows)
    return controller,pp,events,ends,data,sources,coverage


def month_cells(rows,months=MONTHS):
    bins=collections.defaultdict(list);unknown=collections.Counter()
    for z in rows:
        if z['warning_ts'][:7] not in months:continue
        if report.s.ev(z)['status']=='closed':bins[(z['warning_ts'][:7],z['symbol'])].append(z)
        elif report.s.ev(z)['status'] in UNKNOWN:unknown[(z['warning_ts'][:7],z['symbol'])]+=1
    out={m:{} for m in months}
    for (m,sym),rs in bins.items():
        es=[report.s.ev(z) for z in rs];cash=sum(e['net_amount'] for e in es)
        out[m][sym]=dict(n=len(rs),net=cash,mean_r=statistics.mean(e['net_r'] for e in es),wins=sum(e['net_amount']>0 for e in es),
            unknown=unknown[(m,sym)],initial_cash_risk=sum(e['risk']*z['multiplier'] for e,z in zip(es,rs)))
    for m,cells in out.items():
        values=[round(v['net'],8) for v in cells.values()]
        for v in cells.values():
            cash=round(v['net'],8)
            v.update(top_rank=1+sum(a>cash for a in values),bottom_rank=1+sum(a<cash for a in values),active_products=len(cells))
            v['top5']=v['top_rank']<=5;v['positive_top5']=v['top5'] and v['net']>0;v['bottom3']=v['bottom_rank']<=3;v['negative_bottom3']=v['bottom3'] and v['net']<0
    return out


def frequency(cells):
    out={}
    for sym in NAMES:
        xs=[(m,z[sym]) for m,z in cells.items() if sym in z]
        active=len(xs);total=sum(v['net'] for _,v in xs);n=sum(v['n'] for _,v in xs)
        top=sum(v['top5'] for _,v in xs);positive_top=sum(v['positive_top5'] for _,v in xs);bottom=sum(v['bottom3'] for _,v in xs)
        out[sym]=dict(symbol=sym,name=NAMES[sym],active_months=active,inactive_months=len(cells)-active,
            top5=top,positive_top5=positive_top,bottom3=bottom,negative_bottom3=sum(v['negative_bottom3'] for _,v in xs),
            top5_rate=top/active if active else None,bottom3_rate=bottom/active if active else None,
            positive_months=sum(v['net']>0 for _,v in xs),net=total,n=n,
            mean_r=sum(v['mean_r']*v['n'] for _,v in xs)/n if n else None,
            worst_month=min((v['net'] for _,v in xs),default=None),score=positive_top-bottom,
            top_months=[m for m,v in xs if v['top5']],bottom_months=[m for m,v in xs if v['bottom3']])
    return out


def shortlist(stats):
    eligible=[v for v in stats.values() if v['active_months']>=3 and v['net']>0 and v['score']>0]
    eligible.sort(key=lambda v:(-v['score'],v['bottom3'],-v['positive_top5'],v['symbol']))
    return [v['symbol'] for v in eligible[:3]]


def schedule(shadow):
    out={};details={}
    for month in report.OLD_MONTHS:
        cutoff=month+'-01 00:00:00'
        visible=[z for z in shadow if report.s.ev(z)['status']=='closed' and report.s.ev(z)['exit_ts']<cutoff and z['warning_ts'][:7]<month]
        assert all(report.s.ev(z)['exit_ts']<cutoff for z in visible)
        months=[m for m in MONTHS if m<month]
        stats=frequency(month_cells(visible,months))
        out[month]=shortlist(stats);details[month]=dict(cutoff=cutoff,visible_closed=len(visible),latest_visible_exit=max((report.s.ev(z)['exit_ts'] for z in visible),default=None),selected=out[month],frequencies=stats)
    return out,details


def regime_metrics(rows,events):
    out={}
    for name,fun in [('older_2025',lambda z:z['period']=='older'),('early_2026',lambda z:z['period']=='early'),
                     ('recent_2026',lambda z:z['period']=='recent'),('aug_oct_2026',lambda z:z['warning_ts'][:7]>='2026-08'),
                     ('before_aug_2026',lambda z:z['warning_ts'][:7]<'2026-08')]:
        rs=[z for z in rows if fun(z)];m=report.distribution(rs,events,MONTHS)
        closed=[z for z in rs if report.s.ev(z)['status']=='closed'];es=[report.s.ev(z) for z in closed]
        m['mfe_reached_1R']=sum(e['mfe_r']>=1 for e in es)
        m['mfe_reached_2R']=sum(e['mfe_r']>=2 for e in es)
        m['mfe1_then_net_loss']=sum(e['mfe_r']>=1 and e['net_amount']<0 for e in es)
        m['mean_planned_target_to_actual_risk']=statistics.mean((z['target']-e['fill'])*(1 if z['direction']=='up' else -1)/e['risk'] for z,e in zip(closed,es)) if es else None
        m['mean_fee_r']=statistics.mean(z['cost_repricing']['commission']/(e['risk']*z['multiplier']) for z,e in zip(closed,es)) if es else None
        m['mean_actual_initial_cash_risk']=statistics.mean(e['risk']*z['multiplier'] for z,e in zip(closed,es)) if es else None
        out[name]=m
    return out


def run():
    p=old.read(RUN/'protocol.json')
    expected=old.read(RUN/'implementation-repair.json')['script_sha256'] if (RUN/'implementation-repair.json').exists() else p['sources']['script_sha256']
    assert old.sha(__file__)==expected
    controller,pp,events,ends,data,sources,coverage=load();ref=old.read(old.RUN/'results.json')
    accepted={};checks=[];ranks={};freqs={};regimes={}
    for pol in ['original','two_paths_flat']:
        for model,(family,profile,rule) in report.MODELS.items():
            for sf in SUFFIXES:
                rr=data[family][(profile,sf)];aa,ll,risk=controller.select(rr,events[family],ends[family],pp['sector_map'],rule,pol)
                report.audit(ll,rr);m=report.distribution(aa,events[family],MONTHS)
                expected=ref[pol]['combined'][model+'/'+sf]
                bad={k:[m[k],v] for k,v in expected.items() if k in m and not equal(m[k],v)}
                assert not bad,(pol,model,sf,bad)
                accepted[(pol,model,sf)]=aa;checks.append(dict(policy=pol,model=model,scenario=sf,exact=True))
                if pol=='original' and sf==PRIMARY:
                    ranks[model]=month_cells(aa);freqs[model]=frequency(ranks[model]);regimes[model]=regime_metrics(aa,events[family])
    fixed=shortlist(freqs['B5']);walk,walk_details=schedule(accepted[('original','B5',PRIMARY)])
    old.write(RUN/'rankings.json',ranks);old.write(RUN/'frequencies.json',freqs)
    old.write(RUN/'selection.json',dict(full_sample_hindsight=fixed,walk_forward=walk,details=walk_details))
    old.write(RUN/'coverage.json',coverage);old.write(RUN/'period-diagnosis.json',regimes)
    results={};ledgers={};outrows=[]
    for pol in ['original','two_paths_flat']:
        results[pol]={}
        for model,(family,profile,rule) in report.MODELS.items():
            results[pol][model]={}
            for sf in SUFFIXES:
                rr=data[family][(profile,sf)];results[pol][model][sf]={}
                for variant in ['all','hindsight_fixed3','walk_expanding3']:
                    if variant=='all':chosen=rr
                    elif variant=='hindsight_fixed3':chosen=[z for z in rr if z['symbol'] in fixed]
                    else:chosen=[z for z in rr if z['warning_ts'][:7] in walk and z['symbol'] in walk[z['warning_ts'][:7]]]
                    aa,ll,risk=controller.select(chosen,events[family],ends[family],pp['sector_map'],rule,pol)
                    report.audit(ll,chosen)
                    overall=report.distribution(aa,events[family],MONTHS)
                    scored=[z for z in aa if z['warning_ts']>='2026-01-01 00:00:00']
                    test=report.distribution(scored,events[family],report.OLD_MONTHS)
                    if variant=='all':assert equal(overall['net'],ref[pol]['combined'][model+'/'+sf]['net'])
                    results[pol][model][sf][variant]=dict(all_history=overall,evaluation_2026=test)
                    if pol=='original' and sf==PRIMARY:
                        ledgers[model+'/'+variant]=ll
                        for z in aa:outrows.append(dict(model=model,variant=variant,event_key=z['event_key'],symbol=z['symbol'],warning_ts=z['warning_ts'],evaluation=report.s.ev(z)))
    old.write(RUN/'universe-results.json',results)
    old.write(RUN/'opportunity-ledgers.json',ledgers)
    with (RUN/'accepted-summary.jsonl').open('x',encoding='utf-8') as f:
        for z in outrows:f.write(json.dumps(z,ensure_ascii=False)+'\n')
    old.write(RUN/'verification.json',dict(controls=checks,controls_exact=len(checks),visible_exit_assertions=True,
        causal_interval_audits=True,primary_schedule_fixed_across_all_stresses=True,sources=sources,raw_pool_recompetition=True,
        rules_unchanged=True,independent_out_of_sample=False,production_changed=False))
    make_report(ranks,freqs,fixed,walk,results,regimes,coverage)
    for path,h in p['frozen_references'].items():
        root=pathlib.Path(path);assert old.sha(root/'manifest.json')==h
        assert all(old.sha(root/k)==v for k,v in old.read(root/'manifest.json').items())
    assert all(old.sha(k)==v for k,v in p['production_before'].items())
    old.write(RUN/'completion.json',dict(status='complete',controls_exact=len(checks),production_changed=False,
        artifacts={f.name:old.sha(f) for f in RUN.iterdir() if f.is_file()},sources={str(pathlib.Path(__file__).resolve()):old.sha(__file__)}))
    print(json.dumps(dict(fixed=fixed,walk=walk,top_candidates=sorted(freqs['B5'].values(),key=lambda z:-z['score'])[:8],
        B5_primary=results['original']['B5'][PRIMARY]),ensure_ascii=False),flush=True)


def make_report(ranks,freqs,fixed,walk,results,regimes,coverage):
    lines=['# 月度品种前五/后三与泛化性诊断','',
        '日期：2026-10-26。沿用B0—B5冻结策略、一手、费用与四种执行压力、两种未知政策；不改生产。统计2025-09至2026-10共14个预警月份，近期覆盖不齐且10月截至已有数据。','',
        '月度排名按已知平仓净额，归属预警月份。无已知平仓的品种不参加排名，缺数据也不当零收益。并列名次共享，截止名次并列全部列出，因此个别月份可能超过5/3个品种。金额排名受一手现金风险差异影响，不等于风险调整后的优势。','',
        '## B5每月前五与后三','', '|月份|前五：品种/净额/笔数|后三：品种/净额/笔数|活跃品种数|','|---|---|---|---:|']
    def label(sym,v):return f"{NAMES[sym]} {v['net']:+.2f}（{v['n']}笔）"
    for m,cells in ranks['B5'].items():
        top=sorted(((s,z) for s,z in cells.items() if z['top5']),key=lambda x:(-x[1]['net'],x[0]));bottom=sorted(((s,z) for s,z in cells.items() if z['bottom3']),key=lambda x:(x[1]['net'],x[0]))
        lines.append('|'+m+'|'+'；'.join(label(s,z) for s,z in top)+'|'+'；'.join(label(s,z) for s,z in bottom)+f'|{len(cells)}|')
    lines+=['','## B5品种排名频次','', '|品种|活跃月份|前五次数|正收益前五|后三次数|正收益月份|净额|笔数|平均净R|','|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for z in sorted(freqs['B5'].values(),key=lambda z:(-z['score'],z['bottom3'],-z['positive_top5'],z['symbol'])):
        lines.append(f"|{z['name']}({z['symbol']})|{z['active_months']}|{z['top5']}|{z['positive_top5']}|{z['bottom3']}|{z['positive_months']}|{z['net']:.2f}|{z['n']}|{z['mean_r']:.4f}|" if z['n'] else f"|{z['name']}({z['symbol']})|0|0|0|0|0|—|0|—|")
    lines+=['','## 其他基线是否也支持这些品种','', '|品种|'+'|'.join(report.MODELS)+'|','|---|'+'---|'*len(report.MODELS)]
    for sym in fixed:
        lines.append('|'+NAMES[sym]+'|'+'|'.join(f"前五{freqs[k][sym]['top5']}/后三{freqs[k][sym]['bottom3']}；{freqs[k][sym]['net']:.2f}元" for k in report.MODELS)+'|')
    lines+=['','## 名单检验','',
        '只试一套预先保存的示例规则：B5主场景虚拟全品种策略持续记录；至少3个活跃历史月份、累计已知净额为正、正收益前五次数减后三次数>0，再按该差额排序取最多3个。并列依次比较更少后三、更多正收益前五、品种代码。未扫描月份阈值、权重或入选个数。','',
        '全样本事后名单：'+ '、'.join(NAMES[s] for s in fixed)+'。该名单使用全部14个月信息，在同一历史上的收益只是描述性上限参考，不是可在历史起点实施的策略。','',
        '滚动检验从2026年1月开始，每个月只读取月初以前已退出的影子交易，用截至当时已知的数据重算历史月度排名。整月名单固定，新警报按名单准入；已接受订单/持仓按原规则继续，跨月占用和止损历史不清零。','',
        '|计分月份|事前选择品种|','|---|---|']
    for m,syms in walk.items():lines.append('|'+m+'|'+('、'.join(NAMES[s] for s in syms) if syms else '空仓：无合格品种')+'|')
    lines+=['','以下三组均比较2026年1—10月相同已有覆盖，完整重新竞争全部原潜在机会，不是从旧成交表删掉其他品种。','',
        '|基线|全品种净额|事后固定3品种净额|逐月事前3品种净额|全品种回撤|逐月回撤|','|---|---:|---:|---:|---:|---:|']
    for model in report.MODELS:
        z=results['original'][model][PRIMARY];a=z['all']['evaluation_2026'];h=z['hindsight_fixed3']['evaluation_2026'];w=z['walk_expanding3']['evaluation_2026']
        lines.append(f"|{model}|{a['net']:.2f}|{h['net']:.2f}|{w['net']:.2f}|{a['realized_cash_drawdown']:.2f}|{w['realized_cash_drawdown']:.2f}|")
    lines+=['','### B5压力与未知政策（2026计分段）','', '|政策/场景|全品种|事后固定3品种|逐月事前3品种|逐月已知平仓|逐月未知|','|---|---:|---:|---:|---:|---:|']
    for pol in results:
        for sf in SUFFIXES:
            z=results[pol]['B5'][sf];a=z['all']['evaluation_2026'];h=z['hindsight_fixed3']['evaluation_2026'];w=z['walk_expanding3']['evaluation_2026']
            lines.append(f"|{pol}/{sf}|{a['net']:.2f}|{h['net']:.2f}|{w['net']:.2f}|{w['n']}|{w['unknown']}|")
    lines+=['','## 为什么近期差距明显：先看可观察结果','', '|B5时期|笔数|净额|胜率|平均净R|平均盈利|平均亏损|曾达1R|曾达2R|达1R后净亏|','|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for period,m in regimes['B5'].items():
        lines.append(f"|{period}|{m['n']}|{m['net']:.2f}|{m['win_rate']:.2%}|{m['mean_r']:.4f}|{m['mean_win']:.2f}|{m['mean_loss']:.2f}|{m['mfe_reached_1R']}|{m['mfe_reached_2R']}|{m['mfe1_then_net_loss']}|")
    lines+=['','MFE在各自实际退出时截断，上表是已成交路径的描述，不是固定目标命中率或市场环境的因果识别。另有较早P2/P3机制审计显示，同品种后续持续与兑现差异明显；其数值不能直接当成当前B5的证明。','',
        '## 泛化性结论边界','',
        '复杂规则不必然失效，简单规则也会过拟合。关键风险是同一历史被反复用于提出、选择、保留规则；按全样本品种表现选名单同样增加了一层选择。14个月不等于14份独立样本：品种共享行情、交易簇和月度结果相关，且经历的市场状态有限。','',
        '逐月检验只消除了品种筛选环节读取未来收益的偏差，不能撤销B0—B5本身已经在这些历史上反复研究的选择偏差。因此本轮均为内部历史诊断，不称独立样本外验证，也不据此部署。','',
        '参考：[Bailey等：回测过拟合概率](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)、[Harvey与Liu：Backtesting](https://www.cmegroup.com/content/dam/cmegroup/education/files/backtesting.pdf)。','',
        '完整六版逐月品种明细及频次见rankings.json/frequencies.json；选品种时点和可见退出证据见selection.json；四压力×两政策×六基线的名单结果见universe-results.json。']
    (RUN/'summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('stage',choices=['prepare','run']);a=ap.parse_args();globals()[a.stage]()
