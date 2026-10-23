"""Two-sided tail diagnostics and causal-mechanism breadth experiments.

Never changes production or selects products using retrospective profitability.
Cash and R rankings, trade/date/symbol units and admission variants stay separate.
"""
import argparse
import collections
import json
import pathlib
import statistics

from report_signal_ablation import block_interval, jsonl, read_json, stats, write_new
from report_signal_search import distribution
from report_profit_concentration import gap_index
from reprice_signal_costs import TradingDates, reprice
from validate_execution_research import capacity, digest, evaluation, occupancy

UNKNOWN=('ambiguous','missing_data','rollover','unfinished','pending')
CONTROLS=('net_s1','score3_friction20','score3_confirm_no_follow_off')


def closed(rows):return [r for r in rows if evaluation(r)['status']=='closed']


def symmetric_trim(rows,unit='trade',rank='cash',ks=(1,3,5,10)):
    rows=closed(rows)
    key={'trade':lambda r:r['event_key'],'exit_date':lambda r:evaluation(r)['exit_ts'][:10],
         'trading_day':lambda r:r['exit_trading_day'],
         'symbol':lambda r:r['symbol']}[unit]
    field='net_amount' if rank=='cash' else 'net_r'
    bins=collections.defaultdict(list)
    for r in rows:bins[key(r)].append(r)
    values={k:sum(evaluation(r)[field] for r in v) for k,v in bins.items()}
    positives=sorted((k for k,v in values.items() if v>0),key=lambda k:(-values[k],k))
    negatives=sorted((k for k,v in values.items() if v<0),key=lambda k:(values[k],k))
    base=stats(rows)
    out={}
    for n in ks:
        best,worst=positives[:n],negatives[:n]
        def drop(keys):return stats([r for r in rows if key(r) not in set(keys)])
        out[str(n)]=dict(best_keys=best,worst_keys=worst,best_sum=sum(values[k] for k in best),
            worst_sum=sum(values[k] for k in worst),without_best=drop(best),without_worst=drop(worst),
            without_both=drop(best+worst),removed_best_units=len(best),removed_worst_units=len(worst))
    return dict(unit=unit,rank=rank,base=base,trim=out,
        note='Remove positive/negative units separately, then their disjoint union; no hypothetical replacement entries.')


def sector_admission(rows,symbols,sector_map,reverse=False):
    intervals=[(occupancy(r,symbols,'entry'),r) for r in rows]
    intervals=[(i,r) for i,r in intervals if i[0] is not None]
    intervals.sort(key=lambda x:x[1]['event_key'],reverse=reverse)
    intervals.sort(key=lambda x:(x[0][0],x[1]['warning_ts']))
    active=[];accepted=[];blocked=[];ledger=[]
    for (start,end,unknown),r in intervals:
        active=[x for x in active if x['end']>=start]
        bucket=(sector_map[r['symbol']],r['direction'])
        blockers=[x for x in active if x['symbol']==r['symbol'] or x['bucket']==bucket]
        take=not blockers
        ledger.append(dict(event_key=r['event_key'],accepted=take,start=start,end=end,
            blockers=[x['key'] for x in blockers],uncertainty_reserved=unknown))
        (accepted if take else blocked).append(r)
        if take:
            if end is None or end<start:raise ValueError('invalid occupancy')
            active.append(dict(key=r['event_key'],symbol=r['symbol'],bucket=bucket,end=end))
    return accepted,dict(accepted=stats(accepted),blocked=stats(blocked),ledger=ledger)


def product_diagnostics(rows,universe):
    bins=collections.defaultdict(list)
    for r in rows:bins[r['symbol']].append(r)
    result={}
    for symbol in sorted(universe):
        allrows=bins[symbol];rs=closed(allrows);es=[evaluation(r) for r in rs]
        def median(v):return statistics.median(v) if v else None
        risks=[e['risk']*r['multiplier'] for e,r in zip(es,rs)]
        fee_r=[r['cost_repricing']['commission']/risk for r,risk in zip(rs,risks)]
        gross=sum(r['cost_repricing']['gross_amount_after_slippage'] for r in rs)
        fees=sum(r['cost_repricing']['commission'] for r in rs)
        atrs=[r['planned_risk']/r['warning_context']['atr14'] for r in rs
            if r.get('warning_context') and r['warning_context'].get('atr14') and r['warning_context']['as_of']==r['warning_ts']]
        result[symbol]=dict(stats=stats(allrows),gross_after_slippage=gross,commission=fees,
            median_initial_risk_yuan=median(risks),median_planned_risk_ticks=median([r['planned_risk_ticks'] for r in rs]),
            median_planned_risk_atr14=median(atrs),mean_commission_r=statistics.mean(fee_r) if fee_r else None,
            mean_gross_r=statistics.mean([e['net_r']+f for e,f in zip(es,fee_r)]) if es else None,
            losses_mfe_at_least1r=sum(e['net_amount']<0 and e['mfe_r']>=1 for e in es),
            exit_reasons=dict(collections.Counter(e['exit_reason'] for e in es)),
            sample_at_least10=len(rs)>=10,sample_at_least20=len(rs)>=20,
            interpretation='no priced trades' if not rs else 'too few trades to infer product edge' if len(rs)<10 else
                'gross already negative: signal/path/exit losses, not fees alone' if gross<0 else
                'gross positive, costs erased result' if gross-fees<0 else 'priced positive; independence unproven')
    return result


def breadth_metrics(rows,universe,interval=True,gaps=None):
    products=product_diagnostics(rows,universe)
    active={k:v for k,v in products.items() if v['stats']['n']}
    positive=sorted([v['stats']['net_amount'] for v in active.values() if v['stats']['net_amount']>0],reverse=True)
    total=sum(positive);top3=sum(positive[:3]);net=sum(v['stats']['net_amount'] for v in active.values())
    hhi=sum((v/total)**2 for v in positive) if total else None
    t={unit:{rank:symmetric_trim(rows,unit,rank) for rank in ('cash','r')} for unit in ('trade','exit_date','trading_day','symbol')}
    carry={kind:sum(any(g['kind']==kind and evaluation(r)['entry_ts']<=g['previous_ts']
        and evaluation(r)['exit_ts']>=g['next_ts'] for g in (gaps or {}).get(r['symbol'],[]))
        for r in closed(rows)) for kind in ('holiday','weekend')}
    months=collections.defaultdict(list)
    for r in rows:months[r['warning_ts'][:7]].append(r)
    return dict(stats=stats(rows),distribution=distribution(rows),products=products,
        breadth=dict(positive_products=len(positive),negative_products=sum(v['stats']['net_amount']<0 for v in active.values()),
            active_products=len(active),products_n_at_least10=sum(v['stats']['n']>=10 for v in active.values()),
            positive_products_n_at_least10=sum(v['stats']['n']>=10 and v['stats']['net_amount']>0 for v in active.values()),
            median_product_mean_r=statistics.median(v['stats']['mean_net_r'] for v in active.values()) if active else None,
            top3_positive_product_net_share=top3/total if total else None,
            top3_share_of_net=top3/net if net>0 else None,effective_positive_products=1/hhi if hhi else None,
            net_without_EG_PP_BU=sum(v['stats']['net_amount'] for k,v in active.items() if k not in ('EG0','PP0','BU0')),
            EG_PP_BU_exclusion_is_diagnostic_not_a_whitelist=True),
        symmetric=t,months={k:stats(v) for k,v in sorted(months.items())},
        unknown=sum(evaluation(r)['status'] in UNKNOWN for r in rows),observed_closed_carry=carry,
        mean_r_block_interval=block_interval(rows) if interval else None)


def diagnostics_flags(primary,stress,targets):
    return dict(primary_and_stress_positive=all(v['stats']['net_amount']>0 and (v['stats']['mean_net_r'] or 0)>0 for v in (primary,stress)),
        primary_and_stress_without_best3_trading_days_positive=all(v['symmetric']['trading_day']['cash']['trim']['3']['without_best']['net_amount']>0 for v in (primary,stress)),
        both_symmetric3_trading_days_positive=all(v['symmetric']['trading_day']['cash']['trim']['3']['without_both']['net_amount']>0 for v in (primary,stress)),
        primary_and_stress_without_best3_calendar_exit_dates_positive=all(v['symmetric']['exit_date']['cash']['trim']['3']['without_best']['net_amount']>0 for v in (primary,stress)),
        both_without_best3_symbols_positive=all(v['symmetric']['symbol']['cash']['trim']['3']['without_best']['net_amount']>0 for v in (primary,stress)),
        top3_positive_product_share_at_most60pct_both=all((v['breadth']['top3_positive_product_net_share'] or 1)<=.6 for v in (primary,stress)),
        primary_at_least150_closed=primary['stats']['n']>=150,
        independent_validation=False,qualified_for_trading=False)


def run(input_path,batch,profile_path,previous,sector_path,output):
    input_path,batch,profile_path,previous,sector_path,output=map(pathlib.Path,(input_path,batch,profile_path,previous,sector_path,output))
    if output.exists():raise FileExistsError('immutable output already exists')
    source,manifest,profile,sector=read_json(input_path),read_json(batch/'manifest.json'),read_json(profile_path),read_json(sector_path)
    if digest(input_path)!=manifest['input_sha256'] or digest(profile_path)!=manifest['fees_sha256']:raise ValueError('input mismatch')
    sector_map={s:g for g,ss in sector['groups'].items() for s in ss}
    if set(source['symbols'])!=set(sector_map):raise ValueError('sector universe mismatch')
    calendar=TradingDates(source['symbols']);cases=collections.defaultdict(dict)
    gaps=gap_index(source['symbols'],manifest['holiday_schedule']['windows'])
    for r in jsonl(batch/'evaluations.jsonl'):
        r=reprice(r,profile,calendar)
        if evaluation(r)['status']=='closed':r['exit_trading_day']=calendar.day(evaluation(r)['exit_ts'])
        if r['event_key'] in cases[r['scenario']]:raise ValueError('duplicate event')
        cases[r['scenario']][r['event_key']]=r
    if len(cases)!=manifest['case_count'] or len({frozenset(v) for v in cases.values()})!=1:raise ValueError('cohort mismatch')
    checked=0
    for old in jsonl(previous/'evaluations.jsonl'):
        base,slip=old['scenario'].split('/')
        if not base.endswith('_holiday_flat'):continue
        base=base.removesuffix('_holiday_flat')
        if base not in CONTROLS:continue
        old=reprice(old,profile,calendar);new=cases[base+'/'+slip][old['event_key']]
        if new['result']!=old['result'] or new.get('cost_repricing')!=old.get('cost_repricing'):raise ValueError('control regression')
        checked+=1
    result=dict(manifest=manifest,sector_protocol=sector,cohort=len(next(iter(cases.values()))),controls_exact_rows=checked,
        historical_development=True,new_independent_observations=0,production_changed=False,qualified_for_trading=False,
        quantity=1,account_cap=None,cases={},flags={},primary_day_unit='inferred exchange trading day; night maps to next observed daytime session; calendar exit date also retained',
        source_hashes={str(p):digest(p) for p in (input_path,batch/'manifest.json',batch/'evaluations.jsonl',profile_path,sector_path,pathlib.Path(__file__))},
        disclosures=['No future profitability ranks used for admission. All experiments use already-researched history.',
            'Trimming is ex-post sensitivity, not an executable exit rule; cash and R ranks reported separately.',
            'Sector slots are a broad fixed exposure proxy; not fitted correlations, no retries or broker queue model.',
            'Same-slot changes free later opportunities. Unknowns keep conservative occupancy and are excluded from priced PnL.',
            'One lot differs in cash risk across products. R diagnostics do not imply fractional contracts are tradable.',
            'A pyramid-shaped rank plot can be forced by sorting. Inspect breadth, losses, sample counts and dependence instead.'])
    output.mkdir()
    with (output/'accepted_evaluations.jsonl').open('x',encoding='utf-8') as f:
        for name,mapping in sorted(cases.items()):
            rows=list(mapping.values());single=capacity(rows,source['symbols'],'entry',1)
            keys={v['event_key'] for v in single['ledger'] if v['accepted']}
            accepted=[r for r in rows if r['event_key'] in keys]
            sector_rows,sector_diag=sector_admission(rows,source['symbols'],sector_map)
            reverse_rows,_=sector_admission(rows,source['symbols'],sector_map,True)
            result['cases'][name]=dict(independent=stats(rows),status_counts=dict(collections.Counter(evaluation(r)['status'] for r in rows)),
                per_symbol_all_opportunity_statuses={s:dict(collections.Counter(evaluation(r)['status'] for r in rows if r['symbol']==s)) for s in sorted(source['symbols'])},
                admissions={})
            for label,selected in [('symbol_slot',accepted),('sector_direction_slot',sector_rows)]:
                metrics=breadth_metrics(selected,source['symbols'],not name.endswith('slip0'),gaps)
                definition=next(d for d in manifest['definitions'] if d['name']==name.split('/')[0])
                if metrics['observed_closed_carry']['holiday']:raise ValueError('holiday flat left priced carry')
                if definition['rules']['flat_before_weekend'] and metrics['observed_closed_carry']['weekend']:raise ValueError('weekend rule left priced carry')
                result['cases'][name]['admissions'][label]=metrics
                for r in selected:f.write(json.dumps(dict(admission=label,**r),ensure_ascii=False)+'\n')
            result['cases'][name]['sector_reverse_tie_sensitivity']=dict(stats=stats(reverse_rows),
                trading_day_trim3=symmetric_trim(reverse_rows,'trading_day')['trim']['3'],
                note='Reverse lexical tie order only, not sorting scores or outcomes')
            result['cases'][name]['sector_blocked']=sector_diag['blocked']
    for d in manifest['definitions']:
        base=d['name'];result['flags'][base]={}
        for admission in ('symbol_slot','sector_direction_slot'):
            a,b=[result['cases'][f'{base}/slip{i}']['admissions'][admission] for i in (1,2)]
            result['flags'][base][admission]=diagnostics_flags(a,b,manifest['diagnostic_targets'])
    write_new(output/'summary.json',result)
    lines=['# 品种广度、日期依赖及对称剔除研究','',
        f"全部长假避仓、一手、不限制资金；{len(manifest['definitions'])}个有限机制组合×3档滑点，另做行业同向槽位对照。阶段{manifest['exploration_stages']}，后续阶段看过早期结果。全部是历史开发探索。",'',
        '|方案|机会约束|1跳笔数|1跳胜率|1跳净额|2跳净额|1跳删最佳3交易日|2跳删最佳3交易日|1跳删最好最差各3交易日|2跳对称删3交易日|盈利品种数|前3正品种贡献占比|',
        '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for d in manifest['definitions']:
        base=d['name']
        for admission in ('symbol_slot','sector_direction_slot'):
            a,b=[result['cases'][f'{base}/slip{i}']['admissions'][admission] for i in (1,2)]
            ta,tb=[v['symmetric']['trading_day']['cash']['trim']['3'] for v in (a,b)]
            fmt=lambda x:format(x,'.2%') if x is not None else '—'
            lines.append(f"|{base}|{admission}|{a['stats']['n']}|{fmt(a['stats']['win_rate'])}|{a['stats']['net_amount']:.2f}|{b['stats']['net_amount']:.2f}|{ta['without_best']['net_amount']:.2f}|{tb['without_best']['net_amount']:.2f}|{ta['without_both']['net_amount']:.2f}|{tb['without_both']['net_amount']:.2f}|{a['breadth']['positive_products']}/{a['breadth']['active_products']}|{fmt(a['breadth']['top3_positive_product_net_share'])}|")
    lines+=['','完整JSON同时保存逐笔、日期、品种的现金/R双向剔除，分品种费用及信号表现、反向同刻排序敏感性。',
        '前3品种的分母是盈利品种的净利润之和；另存占整体净额比例，避免亏损抵消后放大分母解释。',
        f'原三方案控制逐行核对{checked}行完全一致；未知敞口未当0收益，没有新独立验证。']
    (output/'品种广度与对称剔除.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    for d in manifest['definitions']:
        for admission in ('symbol_slot','sector_direction_slot'):
            a,b=[result['cases'][f"{d['name']}/slip{i}"]['admissions'][admission] for i in (1,2)]
            print(d['name'],admission,a['stats']['n'],round(a['stats']['net_amount'],2),round(b['stats']['net_amount'],2),
                'no3tradingdays',round(a['symmetric']['trading_day']['cash']['trim']['3']['without_best']['net_amount'],2),
                round(b['symmetric']['trading_day']['cash']['trim']['3']['without_best']['net_amount'],2),
                'breadth',a['breadth']['positive_products'],a['breadth']['active_products'],round(a['breadth']['top3_positive_product_net_share'] or 0,3))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('input','batch','profile','previous','sector','output'):p.add_argument(name)
    a=p.parse_args();run(a.input,a.batch,a.profile,a.previous,a.sector,a.output)
