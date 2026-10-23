"""Audit profit concentration, signed session-gap attribution and holiday-only exits.

All sums refer to priced closed trades. Gap subtraction is accounting attribution,
not a strategy backtest. Holiday exits are independently replayed with capacity.
"""
import argparse
import collections
import datetime as dt
import math
import pathlib
import statistics

from report_signal_ablation import jsonl, read_json, stats, write_new
from report_signal_optimization import summarize
from report_signal_search import distribution
from reprice_signal_costs import TradingDates, reprice
from validate_execution_research import digest, evaluation

BASES=("net_s1","score3_friction20","score3_confirm_no_follow_off")


def concentration(values):
    """Separate concentration of positive cash from its share of net profit."""
    values=list(values)
    positive=sorted((x for x in values if x>0),reverse=True)
    profit=sum(positive)
    net=sum(values)
    top={}
    for n in (1,3,5,10):
        amount=sum(positive[:n])
        top[str(n)]=dict(amount=amount,share_of_positive_profit=amount/profit if profit else None,
            share_of_net=amount/net if net>0 else None,net_without=net-amount)
    running=0.;erase=None
    for i,x in enumerate(positive,1):
        running+=x
        if net>0 and running>=net:
            erase=i;break
    hhi=sum((x/profit)**2 for x in positive) if profit else None
    n=len(positive)
    gini=(2*sum((i+1)*x for i,x in enumerate(sorted(positive)))/(n*profit)-(n+1)/n) if n else None
    return dict(n=len(values),winners=n,positive_profit=profit,absolute_loss=-sum(x for x in values if x<0),
        net=net,top=top,largest_winners_needed_to_erase_net=erase,positive_profit_hhi=hhi,
        effective_equal_winners=1/hhi if hhi else None,gini_positive_profit=gini,
        top10pct_all_trades_count=math.ceil(len(values)*.1),
        net_without_top10pct_all_trades=net-sum(positive[:math.ceil(len(values)*.1)]),
        positive_trade_median=statistics.median(positive) if n else None)


def gap_index(symbols,windows):
    result={}
    for symbol,raw in symbols.items():
        gaps=[]
        for a,b in zip(raw['bars'],raw['bars'][1:]):
            ta,tb=[dt.datetime.fromisoformat(x['datetime']) for x in (a,b)]
            minutes=(tb-ta).total_seconds()/60
            if minutes<=5:continue
            holiday=next((w for w in windows if a['datetime']<=w['last_close']<w['reopen']<b['datetime']),None)
            kind='holiday' if holiday else 'weekend' if minutes>=36*60 else 'overnight' if minutes>=180 else 'intraday_or_missing'
            exact=not holiday or (a['datetime']==holiday['last_close'] and tb==dt.datetime.fromisoformat(holiday['reopen'])+dt.timedelta(minutes=5))
            gaps.append(dict(kind=kind,holiday=holiday['name'] if holiday else None,previous_ts=a['datetime'],
                next_ts=b['datetime'],previous_close=a['close'],next_open=b['open'],minutes=minutes,
                exact_holiday_boundary=exact))
        result[symbol]=gaps
    return result


def gap_attribution(row,gaps,slip):
    """Credit only positions already open before closure, cap favorable gap at exit.

    Targets fill at target minus slip, even if the reopening open is better.
    Removing the entire raw close/open jump would overstate their benefit.
    """
    e=evaluation(row)
    if e['status']!='closed':raise ValueError('only priced closed attribution')
    if e['volume']!=1 or len(row['result']['fills'])!=2:raise ValueError('one-lot single-exit audit only')
    sign=1 if row['direction']=='up' else -1
    components=[]
    for g in gaps:
        if not (e['entry_ts']<=g['previous_ts'] and e['exit_ts']>=g['next_ts']):continue
        endpoint=g['next_open']
        capped=False
        if e['exit_ts']==g['next_ts'] and e['exit_reason'] in ('target_s1','target_1r'):
            unslipped_exit=row['result']['fills'][-1]['price']+sign*slip*row['tick_size']
            if (endpoint-unslipped_exit)*sign>0:
                endpoint=unslipped_exit;capped=True
        cash=(endpoint-g['previous_close'])*sign*row['multiplier']
        components.append(dict(**g,effective_endpoint=endpoint,target_capped=capped,gap_cash=cash,
            gap_r=cash/(e['risk']*row['multiplier'])))
    sums={kind:sum(x['gap_cash'] for x in components if x['kind']==kind)
          for kind in ('holiday','weekend','overnight','intraday_or_missing')}
    residual=e['net_amount']-sum(sums.values())
    return dict(components=components,signed_gap_cash=sums,residual_after_fees_and_slippage=residual,
        holiday_carry=any(x['kind']=='holiday' for x in components))


def bins(values,edges):
    return {f'[{a},{b})':sum(a<=x<b for x in values) for a,b in zip(edges,edges[1:])}


def audit(rows,gaps,slip,symbols=None):
    priced=[r for r in rows if evaluation(r)['status']=='closed']
    ledgers=[]
    for r in priced:
        e=evaluation(r);g=gap_attribution(r,gaps[r['symbol']],slip)
        markers=[x for x in (symbols or {}).get(r['symbol'],{}).get('rollovers',[])
            if not x['confirmed'] and e['entry_ts']<=x['ts']<=e['exit_ts']]
        ledgers.append(dict(event_key=r['event_key'],symbol=r['symbol'],direction=r['direction'],
            warning_ts=r['warning_ts'],entry_ts=e['entry_ts'],exit_ts=e['exit_ts'],exit_reason=e['exit_reason'],
            net_amount=e['net_amount'],net_r=e['net_r'],gross_amount=r['cost_repricing']['gross_amount_after_slippage'],
            commission=r['cost_repricing']['commission'],unconfirmed_contract_markers=markers,**g))
    concentration_cash=concentration(x['net_amount'] for x in ledgers)
    concentration_r=concentration(x['net_r'] for x in ledgers)
    carry=[r for r in ledgers if r['holiday_carry']]
    noncarry=[r for r in ledgers if not r['holiday_carry']]
    signed={kind:sum(r['signed_gap_cash'][kind] for r in ledgers) for kind in ('holiday','weekend','overnight','intraday_or_missing')}
    contributions=[g for r in ledgers for g in r['components'] if g['kind']=='holiday']
    positive_holiday=sum(max(0,g['gap_cash']) for g in contributions)
    negative_holiday=sum(min(0,g['gap_cash']) for g in contributions)
    net=concentration_cash['net']
    grouped={}
    for label,key in [('warning_month',lambda r:r['warning_ts'][:7]),('exit_month',lambda r:r['exit_ts'][:7]),
                      ('exit_date',lambda r:r['exit_ts'][:10]),('symbol',lambda r:r['symbol'])]:
        by=collections.defaultdict(list)
        for r in ledgers:by[key(r)].append(r)
        grouped[label]={k:dict(n=len(v),net=sum(r['net_amount'] for r in v),
            holiday_gap=sum(r['signed_gap_cash']['holiday'] for r in v),
            positive_profit=sum(max(0,r['net_amount']) for r in v)) for k,v in sorted(by.items())}
    dates=grouped['exit_date'];symbols=grouped['symbol'];months=grouped['warning_month']
    core=dict(priced_n=len(priced),cash=concentration_cash,r=concentration_r,distribution=distribution(priced),
        cash_bins=bins([r['net_amount'] for r in ledgers],[-math.inf,-1000,-500,-250,-100,0,100,250,500,1000,math.inf]),
        r_bins=bins([r['net_r'] for r in ledgers],[-math.inf,-3,-2,-1,0,1,2,3,5,math.inf]),
        gap_accounting=dict(signed_cash=signed,positive_holiday_gap=positive_holiday,negative_holiday_gap=negative_holiday,
            net_without_signed_holiday_gap=net-signed['holiday'],net_without_positive_holiday_gap=net-positive_holiday,
            net_without_all_signed_session_gaps=net-sum(signed.values()),
            net_without_any_positive_session_gap=net-sum(max(0,g['gap_cash']) for r in ledgers for g in r['components']),
            holiday_boundary_all_exact=all(g['exact_holiday_boundary'] for g in contributions),
            reconciliation_error=net-sum(r['residual_after_fees_and_slippage'] for r in ledgers)-sum(signed.values())),
        holiday_carry_subset=dict(n=len(carry),net=sum(r['net_amount'] for r in carry),
            positive_profit=sum(max(0,r['net_amount']) for r in carry),
            net_excluding_all_carry_trades=sum(r['net_amount'] for r in noncarry),
            cash_excluding_all_carry_trades=concentration(r['net_amount'] for r in noncarry)),
        unconfirmed_contract_marker_subset=dict(n=sum(bool(r['unconfirmed_contract_markers']) for r in ledgers),
            net=sum(r['net_amount'] for r in ledgers if r['unconfirmed_contract_markers']),
            net_excluding=sum(r['net_amount'] for r in ledgers if not r['unconfirmed_contract_markers']),
            note='A marker means missing contract-chain evidence, not a confirmed rollover or proof of a price jump artifact; exclusion is diagnostic, not new admission simulation.'),
        groups=grouped,positive_warning_months=sum(v['net']>0 for v in months.values()),
        positive_symbols=sum(v['net']>0 for v in symbols.values()),
        top3_symbols_net=sum(sorted([v['net'] for v in symbols.values()],reverse=True)[:3]),
        net_excluding_best_exit_date=net-max((v['net'] for v in dates.values()),default=0),
        net_excluding_best3_exit_dates=net-sum(sorted([v['net'] for v in dates.values() if v['net']>0],reverse=True)[:3]),
        top_trades=sorted(ledgers,key=lambda r:r['net_amount'],reverse=True)[:10],
        bottom_trades=sorted(ledgers,key=lambda r:r['net_amount'])[:5],holiday_trades=carry)
    assert abs(core['gap_accounting']['reconciliation_error'])<1e-6
    return core,ledgers


def run(input_path,batch,profile_path,previous_path,output):
    input_path,batch,profile_path,previous_path,output=map(pathlib.Path,(input_path,batch,profile_path,previous_path,output))
    if output.exists():raise FileExistsError('new immutable output required')
    source,manifest,profile=read_json(input_path),read_json(batch/'manifest.json'),read_json(profile_path)
    assert digest(input_path)==manifest['input_sha256'] and digest(profile_path)==manifest['fees_sha256']
    windows=manifest['holiday_schedule']['windows']
    calendar=TradingDates(source['symbols']);gaps=gap_index(source['symbols'],windows)
    cases=collections.defaultdict(dict)
    for row in jsonl(batch/'evaluations.jsonl'):
        row=reprice(row,profile,calendar)
        if row['event_key'] in cases[row['scenario']]:raise ValueError('duplicate opportunity')
        cases[row['scenario']][row['event_key']]=row
    if len(cases)!=18 or len({frozenset(v) for v in cases.values()})!=1:raise ValueError('cohort differs')
    checked=0
    for r in jsonl(previous_path/'evaluations.jsonl'):
        if r['scenario'].split('/')[0] not in BASES:continue
        actual=cases[r['scenario']][r['event_key']]
        if actual['result']!=r['result'] or actual.get('cost_repricing')!=r.get('cost_repricing'):
            raise ValueError('base replay regression '+r['scenario']+' '+r['event_key'])
        checked+=1
    result=dict(manifest=manifest,account_cap=None,quantity=1,qualified_for_trading=False,production_changed=False,
        independent_new_observations=0,original_control_exact_rows=checked,cases={},
        disclosures=['Historical repeatedly researched sample, no independent validation.',
            'All amounts exclude unknown exposures; not a complete account curve.',
            'Positive-profit concentration denominator excludes losses; share-of-net uses net including losses.',
            'Signed gap attribution is capped at modeled favorable target exit; not raw uncapped reopening gain.',
            'Gap subtraction and dropping carry trades keep original admissions; neither is a tradable counterfactual.',
            'Holiday-flat variants rerun exits, fees and one-symbol capacity; no ordinary weekend/lunch flatten.',
            'Nonholiday raw gaps may include absent data; intraday_or_missing is explicitly not holiday.',
            'Independent signal diagnostics and same-symbol capacity diagnostic both retained.'],
        source_hashes={str(p):digest(p) for p in [input_path,batch/'manifest.json',batch/'evaluations.jsonl',profile_path,
            previous_path/'evaluations.jsonl',pathlib.Path(__file__)]})
    output.mkdir()
    with (output/'trade_attribution.jsonl').open('x',encoding='utf-8') as f:
        for name,mapping in sorted(cases.items()):
            rows=list(mapping.values());summary,keys=summarize(rows,source['symbols'])
            slip=int(name.rsplit('slip',1)[1]);accepted=[r for r in rows if r['event_key'] in keys]
            summary['profit_audit'],ledger=audit(accepted,gaps,slip,source['symbols'])
            summary['independent_profit_audit'],_=audit(rows,gaps,slip,source['symbols'])
            for r in ledger:
                import json
                f.write(json.dumps(dict(scenario=name,**r),ensure_ascii=False)+'\n')
            result['cases'][name]=summary
    result['holiday_comparisons']={}
    for base in BASES:
        result['holiday_comparisons'][base]={}
        for slip in (1,2):
            a=result['cases'][f'{base}/slip{slip}'];b=result['cases'][f'{base}_holiday_flat/slip{slip}']
            result['holiday_comparisons'][base][str(slip)]=dict(
                delta_net=b['one_symbol_slot']['accepted']['net_amount']-a['one_symbol_slot']['accepted']['net_amount'],
                holiday_exit_n=sum(evaluation(r)['exit_reason']=='pre_holiday' and evaluation(r)['status']=='closed'
                    for r in cases[f'{base}_holiday_flat/slip{slip}'].values()),
                priced_accepted_holiday_carry=b['profit_audit']['holiday_carry_subset']['n'])
            if b['profit_audit']['holiday_carry_subset']['n']:raise ValueError('holiday policy left priced carry exposure')
    write_new(output/'summary.json',result)
    lines=['# 三方案利润集中与法定长假避仓审计','',
        '同品种同时一个机会、一手、无资金上限；仅可定价已平仓。长假单独处理，未扩展到周末或午休。','',
        '|方案|滑点|笔数|胜率|净额|前5盈利/正盈利总额|删前5净额|删前10净额|长假跳空正贡献|长假跳空负贡献|剔除所有跨长假单净额|',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for base in BASES:
        for variant in [base,base+'_holiday_flat']:
            for slip in (1,2):
                v=result['cases'][f'{variant}/slip{slip}'];a=v['one_symbol_slot']['accepted'];p=v['profit_audit'];c=p['cash'];g=p['gap_accounting']
                lines.append(f"|{variant}|{slip}|{a['n']}|{a['win_rate']:.2%}|{a['net_amount']:.2f}|{c['top']['5']['share_of_positive_profit']:.2%}|{c['top']['5']['net_without']:.2f}|{c['top']['10']['net_without']:.2f}|{g['positive_holiday_gap']:.2f}|{g['negative_holiday_gap']:.2f}|{p['holiday_carry_subset']['net_excluding_all_carry_trades']:.2f}|")
    lines+=['','跳空贡献以持仓实际跨越的相邻K线收盘/开盘计算，盈利跳空受实际目标成交上限约束。',
        '删大单、减跳空、删跨假期交易是原集合的敏感性诊断；真正避仓的结果看holiday_flat重放，包含机会释放后的新入场。',
        '所有大单、日期、月份、品种、R分布与现金分布详见summary.json及trade_attribution.jsonl。',
        f'原三方案逐行控制核对：{checked}行完全一致。新增独立样本为零。']
    (output/'利润集中审计.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    for base in BASES:
        for suffix in ['', '_holiday_flat']:
            for slip in (1,2):
                name=f'{base}{suffix}/slip{slip}';p=result['cases'][name]['profit_audit'];g=p['gap_accounting']
                print(name,p['priced_n'],round(p['cash']['net'],2),'no5',round(p['cash']['top']['5']['net_without'],2),
                    'no10',round(p['cash']['top']['10']['net_without'],2),'holiday+',round(g['positive_holiday_gap'],2),
                    'holiday-',round(g['negative_holiday_gap'],2),'carry',p['holiday_carry_subset']['n'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('input','batch','profile','previous','output'):parser.add_argument(name)
    args=parser.parse_args()
    run(args.input,args.batch,args.profile,args.previous,args.output)
