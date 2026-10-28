"""Monthly/product report for the frozen earlier-window universe comparison."""
import argparse
import collections
import json
import pathlib
import sys

sys.dont_write_bytecode=True
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
import research_swing_may_aug2025 as r
import research_swing_monthly_products as m

RUN=r.RUN
MONTHS=[f'2025-{i:02}' for i in range(5,9)]


def calculate(data,events,ends,controller,pp,months):
    results={};accepted={};ledger={}
    for pol in ['original','two_paths_flat']:
        results[pol]={}
        for model in ['B2','B5']:
            _,profile,rule=m.report.MODELS[model];results[pol][model]={}
            for sf in m.SUFFIXES:
                results[pol][model][sf]={}
                for name,syms in r.UNIVERSES.items():
                    rr=[z for z in data[profile,sf] if z['symbol'] in syms]
                    aa,ll,risk=controller.select(rr,events,ends,pp['sector_map'],rule,pol)
                    m.report.audit(ll,rr)
                    metrics=m.report.distribution(aa,events,months)
                    metrics.update({k:v for k,v in risk.items() if k!='realized_settlements'})
                    metrics['products']={s:m.report.distribution([z for z in aa if z['symbol']==s],events,months) for s in syms}
                    results[pol][model][sf][name]=metrics
                    if pol=='original' and sf==m.PRIMARY:
                        accepted[model+'/'+name]=aa;ledger[model+'/'+name]=ll
    return results,accepted,ledger


def verify_existing():
    controller,pp,events,ends,data,sources,_=m.load()
    results,accepted,_=calculate(data['unmerged'],events['unmerged'],ends['unmerged'],controller,pp,m.MONTHS)
    reference=m.old.read(m.REPO/'research-runs/swing-universe-mix-20261026-v1/results.json')
    qa=[]
    for pol in results:
        for model in results[pol]:
            for sf,cases in results[pol][model].items():
                for name,z in cases.items():
                    expected=reference[pol][model][sf][name]
                    for k in ['n','net','win_rate','realized_cash_drawdown','unknown','warning_month_net','bins']:
                        assert m.equal(z[k],expected[k]),(pol,model,sf,name,k)
                    # Product-month contributions must reconcile to portfolio totals.
                    for month in m.MONTHS:
                        assert abs(sum(v['warning_month_net'][month] for v in z['products'].values())-z['warning_month_net'][month])<1e-7
                    assert sum(v['n'] for v in z['products'].values())==z['n']
                    qa.append(dict(policy=pol,version=model,scenario=sf,universe=name,exact=True))
    m.old.write(RUN/'existing-control-verification.json',dict(exact_cases=len(qa),cases=qa,
        existing_window='2025-09 through2026-10-23; this is NOT new May-Aug data',
        monthly_product_totals_reconcile=True,source_hashes=sources))
    print('Verified',len(qa),'existing universe cases and monthly/product reconciliation; no new-window outcomes claimed.',flush=True)


def report():
    assert (RUN/'older/manifest.json').exists(),'New earlier-window executions not yet available'
    p=m.old.read(RUN/'protocol.json');controller,pp,events,ends,prior,sources,_=m.load()
    calendar=m.report.TradingDates({});calendar.dates=m.old.read(RUN/'trading-dates.json')
    data=m.report.row_cases(RUN/'older/evaluations.jsonl','unmerged','new_older',m.old.read(p['fees_path']),calendar)
    pool=m.old.read(RUN/'older-pool.json');new_events={z['event_key']:z['event'] for z in pool}
    new_ends=m.old.read(RUN/'older/manifest.json')['contract_ends']
    results,accepted,ledger=calculate(data,new_events,new_ends,controller,pp,MONTHS)
    joined={key:data.get(key,[])+rs for key,rs in prior['unmerged'].items()}
    all_events=dict(events['unmerged']);assert not all_events.keys()&new_events.keys();all_events.update(new_events)
    all_ends=dict(ends['unmerged'])
    for c,end in new_ends.items():all_ends[c]=max(end,all_ends.get(c,''))
    combined,_,_=calculate(joined,all_events,all_ends,controller,pp,MONTHS+m.MONTHS)
    for name,obj in [('results.json',results),('combined18-results.json',combined),('opportunity-ledgers.json',ledger)]:m.old.write(RUN/name,obj)
    with (RUN/'accepted.jsonl').open('x',encoding='utf-8') as f:
        for case,rs in accepted.items():
            for row in rs:f.write(json.dumps(dict(case=case,row=row),ensure_ascii=False)+'\n')
    lines=['# 四组搭配向前回测：2025年5—8月','',
        'B2、B5均沿用冻结定义，没有混入鸡蛋20%放松版。每次一手，已扣冻结手续费模型和对应滑点；费用不是2025年逐日实际账单。原始分钟仅在内存处理，4月预热，9月尾部用于退出。',
        '', '月份按预警时间归属；已实现回撤不含持仓浮亏和保证金。未知结果不填零收益。此处为后来挑选的名单向前探索，不能据此声称策略获得独立样本外验证。',
        '', '## 新四个月B5主场景合计','', '|搭配|平仓笔数|净额/元|胜率|已实现回撤/元|未知|最严压力净额/元|','|---|---:|---:|---:|---:|---:|---:|']
    primary=results['original']['B5'][m.PRIMARY]
    for name,z in primary.items():
        worst=min(results[pol]['B5'][sf][name]['net'] for pol in results for sf in m.SUFFIXES)
        lines.append(f"|{r.LABELS[name]}|{z['n']}|{z['net']:,.2f}|{z['win_rate']:.1%}|{z['realized_cash_drawdown']:,.2f}|{z['unknown']}|{worst:,.2f}|")
    for model in ['B5','B2']:
        lines+=['',f'## {model}每月组合净额','', '|月份|'+'|'.join(r.LABELS[n] for n in r.UNIVERSES)+'|','|---|'+'---:|'*4]
        for month in MONTHS:
            lines.append('|'+month+'|'+'|'.join(f"{results['original'][model][m.PRIMARY][n]['warning_month_net'][month]:,.2f}" for n in r.UNIVERSES)+'|')
    for name,syms in r.UNIVERSES.items():
        z=primary[name];rs=accepted['B5/'+name]
        lines+=['',f'## {r.LABELS[name]}：各月各品种净额与笔数','', '|月份|'+'|'.join(m.NAMES[s] for s in syms)+'|合计|','|---|'+'---:|'*(len(syms)+1)]
        for month in MONTHS:
            values=[]
            for s in syms:
                known=[x for x in rs if x['symbol']==s and x['warning_ts'][:7]==month and m.report.s.ev(x)['status']=='closed']
                unknown=sum(x['symbol']==s and x['warning_ts'][:7]==month and m.report.s.ev(x)['status'] in m.UNKNOWN for x in rs)
                cash=sum(m.report.s.ev(x)['net_amount'] for x in known)
                values.append(f"{cash:,.2f}（{len(known)}笔"+(f"；未知{unknown}" if unknown else '')+'）')
            lines.append('|'+month+'|'+'|'.join(values)+f"|{z['warning_month_net'][month]:,.2f}|")
        lines+=['','|品种|四个月平仓数|净额/元|胜率|已实现回撤/元|未知|','|---|---:|---:|---:|---:|---:|']
        for s,v in z['products'].items():
            wr=f"{v['win_rate']:.1%}" if v['win_rate'] is not None else '—'
            lines.append(f"|{m.NAMES[s]}|{v['n']}|{v['net']:,.2f}|{wr}|{v['realized_cash_drawdown']:,.2f}|{v['unknown']}|")
    lines+=['','## 18个月连续回放','', '|搭配|B5净额/元|平仓笔数|已实现回撤/元|未知|','|---|---:|---:|---:|---:|']
    for name,z in combined['original']['B5'][m.PRIMARY].items():
        lines.append(f"|{r.LABELS[name]}|{z['net']:,.2f}|{z['n']}|{z['realized_cash_drawdown']:,.2f}|{z['unknown']}|")
    lines+=['','连续回放重新计算跨期占用和止损历史，不能把两段回撤相加。完整B2/B5、8组压力、逐月品种及未知状态见results.json和combined18-results.json。',
        '', '节假日依据：[上期所2025年休市公告](https://www.shfe.com.cn/publicnotice/notice/202412/t20241223_824109.html)。']
    (RUN/'summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    m.old.write(RUN/'completion.json',dict(complete=True,new_window=[r.START,r.END],artifacts={q.name:m.old.sha(q) for q in RUN.iterdir() if q.is_file()},production_changed=False))
    print('Earlier-window and18-month monthly/product reports finished.',flush=True)


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('stage',choices=['verify_existing','report']);a=ap.parse_args();globals()[a.stage]()
