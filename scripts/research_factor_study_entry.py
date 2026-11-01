"""Frozen entry-timing experiment; all scoring and structural exits stay fixed."""
import argparse
import bisect
import collections
import datetime as dt
import pathlib
from decimal import Decimal

from factor_study_data import (Calendar, read, rows, write, write_rows, new_output, seal, verify_seal, digest)
from factor_study_execution import MinuteBars, evaluate, capacity, metrics, audit_label, TERMINAL
from factor_study_quote_audit import check_quote
from factor_study_analysis import load_context, outer_windows, fdr_adjust
from factor_study_models import baseline_score
from research_factor_study_followup import accepted, cohort, interval, validate_ledger


def key(mode,target,slip):
    return f'{target}_{slip}tick' if mode=='breakout' else f'{mode}_{target}_{slip}tick'


def quote_evidence(label,quotes,indices):
    mode=label.get('entry_mode')
    if mode=='close_confirm':
        confirmation=quotes[label['confirmation_ts']]
        return dict(confirmation=confirmation,next_observed_minute=
                    indices[label['entry_ts']]==indices[label['confirmation_ts']]+1)
    if mode=='retest25':return dict(breakout=quotes[label['break_ts']])
    return None


def bounded_release(label,minutes,gaps,config):
    """Bound occupancy only, never infer the cash of an ambiguous execution.

    A pending branch could enter as late as pending bucket 12; it must time-exit
    by bucket 12+60-1. Already-open branches have their earlier entry buckets.
    Require an observed minute sequence through the bound, with no source gaps.
    """
    if label['status']!='ambiguous' or label.get('release_ts') is not None:return label
    paths=label['disagreement']['paths']
    latest=max((p['entry_bucket'] if p['entry'] is not None else config['pending_buckets'])
               +config['holding_buckets']-1 for p in paths if p['status'] not in TERMINAL)
    warning=label['warning_ts'];first=bisect.bisect_left(minutes.starts,warning)
    gap_index=bisect.bisect_left(gaps,warning);count=0;previous=None
    for i in range(first,len(minutes)):
        b=minutes[i]
        if gap_index<len(gaps) and gaps[gap_index]<b['start']:return label
        t=dt.datetime.fromisoformat(b['start'])
        bucket=(t.replace(minute=t.minute//15*15)+dt.timedelta(minutes=15)).isoformat(sep=' ')
        if bucket!=previous:count+=1;previous=bucket
        if count>=latest and b['ts']==bucket:
            return dict(label,release_ts=b['ts'],capacity_bound=dict(kind='observed_latest_mandatory_exit',
                        last_bucket=latest,ts=b['ts'],cash_still_unknown=True))
    return label


def replay_stage(original,protocol_path,output):
    prepared,scan,snapshots,maps,base,coverage=load_context(original)
    protocol=read(protocol_path)
    if (protocol['schema']!='factor-study-entry-v1'
        or protocol['entry_modes']!=['breakout','close_confirm','retest25']
        or protocol['targets']!=['cash200','s1'] or protocol['slip_ticks']!=[1,2]
        or protocol['planned_loss_cap_yuan']!=500 or protocol['no_follow_bucket']!=5):
        raise ValueError('entry experiment must keep its preregistered six structures')
    out=new_output(output)
    write(out/'protocol.json',protocol)
    write(out/'context.json',dict(original_replay=str(pathlib.Path(original).resolve()),
                                 prepared=str(prepared),scan=str(scan)))
    calendar=Calendar(read(prepared/'calendar.json'),base['night_ends'])
    fees=read(prepared/'fees.json');counts=collections.Counter();target_gaps=0
    bycontract=collections.defaultdict(list)
    for s in snapshots:
        if baseline_score(s) is not None and baseline_score(s)>=protocol['fixed_score_min']:
            bycontract[s['contract']].append(s)
    def generated():
        nonlocal target_gaps
        for info in coverage['contracts']:
            selected=bycontract[info['contract']]
            if not selected:continue
            bars=MinuteBars(rows(prepared/info['minutes_file']))
            quotes={b['ts']:b for b in bars};indices={b['ts']:i for i,b in enumerate(bars)}
            gaps=read(prepared/info['gaps_file']);spec=fees['products'][info['symbol'][:-1]]
            for s in selected:
                for mode in protocol['entry_modes']:
                    config=dict(base['execution'],entry_mode=mode)
                    for target in protocol['targets']:
                        for slip in protocol['slip_ticks']:
                            label=evaluate(s,bars,gaps,spec,calendar,config,target,slip)
                            if mode=='breakout':
                                frozen={k:v for k,v in maps[key(mode,target,slip)][s['candidate_id']].items()
                                        if k!='warning_trading_day'}
                                if label!=frozen:raise ValueError('original breakout changed: '+s['candidate_id'])
                                counts['original_labels_reproduced']+=1
                            label=bounded_release(label,bars,gaps,config)
                            if label.get('capacity_bound'):counts['expiry_bounded_ambiguities']+=1
                            label['warning_trading_day']=calendar.day(label['warning_ts'])
                            audit_label(s,label)
                            if label['status']=='closed':
                                gap=check_quote(s,label,quotes[label['entry_ts']],quotes[label['exit_ts']],spec,
                                                calendar,quote_evidence(label,quotes,indices))
                                target_gaps+=int(gap);counts['closed_quote_and_fee_verified']+=1
                            label['scenario']=key(mode,target,slip)
                            counts[mode+':'+label['status']]+=1
                            yield label
            print(info['contract']+': entry alternatives and original control verified',flush=True)
    write_rows(out/'labels.jsonl',generated())
    write(out/'verification.json',dict(status='passed',counts=dict(counts),conservative_target_gaps=target_gaps,
        note='Two OHLC paths agree on priced outcomes. Quote checks establish model consistency, not tick ordering or fills.'))
    seal(out,'entry-replay',[pathlib.Path(original)/'manifest.json',prepared/'manifest.json',
                             scan/'manifest.json',protocol_path])
    return out


def economics(labels,snapshots):
    closed=[l for l in labels if l['status']=='closed']
    gross=sum(l['gross_yuan'] for l in closed);fees=sum(l['fees'] for l in closed)
    slip=sum(2*l['slip_ticks']*snapshots[l['candidate_id']]['tick']*
             snapshots[l['candidate_id']]['multiplier'] for l in closed)
    m=metrics(labels);chase=[]
    for l in closed:
        s=snapshots[l['candidate_id']];sign=1 if s['event']['direction']=='up' else -1
        risk=sign*(s['event']['entry']-l['plan']['stop'])
        if risk>0:chase.append(sign*(l['entry']-sign*l['slip_ticks']*s['tick']-s['event']['entry'])/risk)
    return dict(gross_after_slippage_yuan=gross,fees_yuan=fees,modeled_slippage_yuan=slip,
                gross_before_modeled_slippage_yuan=gross+slip,
                mean_entry_chase_fraction=sum(chase)/len(chase) if chase else None,
                break_even_win_rate=abs(m['mean_loss'])/(m['mean_win']+abs(m['mean_loss']))
                    if m['mean_win'] and m['mean_loss'] else None,
                note='Entry chase uses original trigger-to-stop distance. Pre-slip gross is arithmetic decomposition on the same fills, not a zero-slip replay.')


def matched_paths(baseline,alternative,start):
    """Original accepted warnings only: isolates paths; does not rerun capacity."""
    counts=collections.Counter();left=[];right=[];avoided=[];missed=[]
    for row in cohort(baseline,start):
        if row['decision']!='accepted':continue
        a=row['label'];b=alternative[row['candidate_id']]
        counts[a['status']+'->'+b['status']]+=1
        if a['status']=='closed' and b['status']=='closed':
            left.append(a['net_yuan']);right.append(b['net_yuan'])
        elif a['status']=='closed' and b['status'] in {'invalidated','untriggered','invalid_target'}:
            (avoided if a['net_yuan']<0 else missed).append(a['net_yuan'])
    return dict(status_transitions=dict(counts),both_closed=len(left),
                matched_original_net_yuan=sum(left),matched_alternative_net_yuan=sum(right),
                matched_delta_yuan=sum(right)-sum(left),avoided_original_losses_yuan=sum(avoided),
                missed_original_nonnegative_yuan=sum(missed),
                note='Selected by original acceptance, not alternative acceptance. Unknowns excluded only from paired cash and explicitly counted; no hypothetical zero P&L assigned.')


def candidate_status(one,two,zero,versus,protocol,q_zero,q_versus):
    if min(one['known_fraction'],two['known_fraction'])<protocol['minimum_known_fraction']:
        return 'insufficient_priced_coverage'
    if min(one['closed'],two['closed'])<protocol['minimum_closed']:return 'insufficient_trades'
    positive=lambda comparisons:all(comparisons[str(b)].get('interval95',[float('-inf')])[0]>0 for b in (5,10))
    if not (one['net_yuan']>0 and two['net_yuan']>0 and positive(zero)
            and q_zero is not None and q_zero<=protocol['fdr']):return 'not_supported'
    if versus is not None and not (positive(versus) and q_versus is not None and q_versus<=protocol['fdr']):
        return 'no_supported_entry_improvement'
    return 'development_candidate_only'


def guard_intervals(comparisons,coverage_ok):
    if not coverage_ok:
        for value in comparisons.values():
            value['priced_subset_diagnostic']={k:value.pop(k) for k in
                ('interval95','centered_bootstrap_p') if k in value}
            value['status']='insufficient_priced_coverage'
            value['note']='Separately priced cash subsets do not identify the full policy difference. No inference or promotion.'
    return comparisons


def evaluate_stage(replayed,output):
    replayed=pathlib.Path(replayed);verify_seal(replayed)
    context=read(replayed/'context.json');prepared,scan,snapshots,maps,base,coverage=load_context(context['original_replay'])
    for l in rows(replayed/'labels.jsonl'):maps[l['scenario']][l['candidate_id']]=l
    protocol=read(replayed/'protocol.json');out=new_output(output)
    write(out/'protocol.json',protocol);write(out/'context.json',dict(replay=str(replayed.resolve())))
    first=next(outer_windows(coverage['trading_dates'],base),None)
    start=first[1] if first else base['end'];scores={s['candidate_id']:baseline_score(s) for s in snapshots}
    thresholds={k:protocol['fixed_score_min'] for k in scores};byid={s['candidate_id']:s for s in snapshots}
    books={};results={}
    for mode in protocol['entry_modes']:
        for target in protocol['targets']:
            name=f'{mode}_{target}';reports={}
            for slip in protocol['slip_ticks']:
                ledger=capacity(snapshots,maps[key(mode,target,slip)],scores,thresholds)
                validate_ledger(ledger);books[name,slip]=ledger
                labels=accepted(ledger,start)
                reports[str(slip)]=dict(metrics=metrics(labels),economics=economics(labels,byid),
                    by_month={m:metrics(l for l in labels if l['warning_ts'][:7]==m)
                              for m in sorted({l['warning_ts'][:7] for l in labels})},
                    by_symbol={s:metrics(l for l in labels if l['symbol']==s)
                               for s in sorted({l['symbol'] for l in labels})},
                    by_reason={r:metrics(l for l in labels if l['reason']==r)
                               for r in ('stop','target','no_follow','time_exit')})
                write_rows(out/f'{name}_{slip}tick.ledger.jsonl',ledger)
            results[name]=dict(mode=mode,target=target,reports=reports,
                versus_no_trade=interval([],books[name,1],coverage,start,base))
            results[name]['priced_coverage_ok']=min(reports[str(t)]['metrics']['known_fraction']
                                                   for t in (1,2))>=protocol['minimum_known_fraction']
            guard_intervals(results[name]['versus_no_trade'],results[name]['priced_coverage_ok'])
            if mode=='breakout':
                legacy={k:dict(l,release_ts=None) if l.get('capacity_bound') else l
                        for k,l in maps[key(mode,target,1)].items()}
                oldbook=capacity(snapshots,legacy,scores,thresholds)
                results[name]['legacy_indefinite_occupancy']=metrics(accepted(oldbook,start))
    alternatives=[]
    for name,item in results.items():
        if item['mode']=='breakout':continue
        baseline=books['breakout_'+item['target'],1]
        item['versus_same_target_breakout']=interval(baseline,books[name,1],coverage,start,base)
        item['entry_comparison_coverage_ok']=item['priced_coverage_ok'] and results['breakout_'+item['target']]['priced_coverage_ok']
        guard_intervals(item['versus_same_target_breakout'],item['entry_comparison_coverage_ok'])
        item['matched_paths']=matched_paths(baseline,maps[key(item['mode'],item['target'],1)],start)
        alternatives.append(item)
    # Unidentifiable comparisons spend their preregistered multiplicity slot
    # with p=1 rather than shrinking the hypothesis family after inspection.
    q_zero=fdr_adjust([r['versus_no_trade']['5'].get('centered_bootstrap_p',1.) for r in results.values()])
    q_entry=fdr_adjust([r['versus_same_target_breakout']['5'].get('centered_bootstrap_p',1.) for r in alternatives])
    for r,q in zip(alternatives,q_entry):r['bh_q_entry']=q
    for r,q in zip(results.values(),q_zero):
        r['bh_q_zero']=q
        r['status']=candidate_status(r['reports']['1']['metrics'],r['reports']['2']['metrics'],
            r['versus_no_trade'],r.get('versus_same_target_breakout'),protocol,q,r.get('bh_q_entry'))
    result=dict(schema='factor-entry-results-v1',period=[base['start'],base['end']],evaluation_start=start,
                strategies=results,development_only=True,deployment='disabled',
                no_trade=dict(net_yuan=0,trades=0),
                warning='Recently inspected development history; unknown cash remains unknown. Long and recent windows overlap and are not independent confirmations.')
    write(out/'summary.json',result)
    lines=['# 入场时机对照研究','',f'统一评估起点：{start}。历史开发诊断，正式参数保持原样。','',
           '固定评分≥70、原结构止损、计划损失上限500元、第5段无跟随退出。各自重新计算从预警开始的订单占用。','',
           '|入场与目标|完成|未知|净元|胜率|达到200元比例|平均净R|2tick净元|状态|',
           '|---|---:|---:|---:|---:|---:|---:|---:|---|']
    def fmt(x):return '—' if x is None else f'{x:,.2f}'
    for name,r in results.items():
        a,b=r['reports']['1']['metrics'],r['reports']['2']['metrics']
        lines.append(f'|{name}|{a["closed"]}|{a["unknown"]}|{fmt(a["net_yuan"])}|{fmt(100*a["win_rate"]) if a["win_rate"] is not None else "—"}%|{fmt(100*a["hit200_rate"]) if a["hit200_rate"] is not None else "—"}%|{fmt(a["mean_net_r"])}|{fmt(b["net_yuan"])}|{r["status"]}|')
    lines+=['','## 入场变化相对同目标原突破','',
            '|入场与目标|已定价部分净元差|5日块95%区间|10日块95%区间|BH q|同预警双方均完成的路径差|',
            '|---|---:|---|---|---:|---:|']
    for name,r in results.items():
        if 'matched_paths' not in r:continue
        c=r['versus_same_target_breakout']
        ci5=c['5'].get('interval95','覆盖不足，不推断');ci10=c['10'].get('interval95','覆盖不足，不推断')
        lines.append(f'|{name}|{fmt(c["5"]["delta_total_yuan"])}|{ci5}|{ci10}|{fmt(r["bh_q_entry"])}|{fmt(r["matched_paths"]["matched_delta_yuan"])}|')
    lines+=['','## 使用范围','',
        '收盘确认只使用完成的1分钟K线，下一条实际分钟开盘才可成交。回踩规则在原突破后激活，按原触发价至结构止损距离的25%计算，按最小跳动向回踩方向取整；市场触价并加入不利滑点，不假设限价排队成交。',
        '挂单12段和持仓60段不变。回踩或确认不会延长挂单期限。两条OHLC路径结果不一致的分钟保留为未知，不能挑有利路径。',
        '新入场只因最高浮盈数值不同、而成交状态和是否达到0.5R完全相同的路径，按退出决策等价合并。peak和peak_upper保留浮盈下上界；跨过0.5R与否不同仍保留未知。原突破原始标签维持逐条复现。',
        '三种入场统一修正歧义订单占用：价格全程有观测且无缺口时，等待最长12段、此后持仓最长60段，最迟第71段必已释放；已入场的歧义分支使用其实际入场段加59段。这里只证明最晚释放，不补写未知盈亏。原突破另列legacy_indefinite_occupancy供核对旧轮口径。',
        '账户比较包含重新占用造成的后续信号变化；matched_paths仅看原策略接纳的相同预警，单列未成交、错过盈利和避开亏损，不能当作账户收益。',
        'summary.json包含按品种、预警月份和退出原因的明细，以及费用和模型滑点分解；同成交的滑点分解不代表重新运行零滑点策略。',
        '六项对零比较与四项入场增量比较分别做BH调整。候选需完成≥100、已定价覆盖≥95%、1/2tick均盈利、5/10日块下界均为正及q≤0.10。历史区间已被观察；即使通过也只进入新信号验证。',
        '覆盖不足的规则不提供策略增量推断，其仅可定价子集区间另存为诊断；在原预注册比较族中按p=1占位，不能因未定价而缩小多重比较数。',
        '费用为冻结快照，历史主力映射时点可知性未确认；分钟模型不证明实际成交。未知结果未按零收益填补，净元仅代表可定价已完成部分。']
    (out/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    seal(out,'entry-evaluate',[replayed/'manifest.json',prepared/'manifest.json',scan/'manifest.json'])
    return out


def verify_stage(run,output):
    run=pathlib.Path(run)
    for stage in ('replay','evaluate'):verify_seal(run/stage)
    context=read(run/'replay/context.json');original=pathlib.Path(context['original_replay'])
    verify_seal(original);old=read(original/'context.json');verify_seal(old['prepared'])
    scan=pathlib.Path(old['scan']);invocation=read(scan/'invocation.json')
    if digest(scan/'snapshots.jsonl')!=invocation['snapshots_sha256']:
        raise ValueError('original scan snapshots changed')
    if digest(pathlib.Path(old['prepared'])/'manifest.json')!=invocation['prepared_manifest_sha256']:
        raise ValueError('scan preparation provenance changed')
    if any(r['prefix_mismatches'] for r in read(scan/'manifest.json')['contracts']):
        raise ValueError('original prefix audit failed')
    snapshots={s['candidate_id']:s for s in rows(pathlib.Path(old['scan'])/'snapshots.jsonl')}
    summary=read(run/'evaluate/summary.json');protocol=read(run/'evaluate/protocol.json')
    bounded=collections.defaultdict(list)
    for l in rows(run/'replay/labels.jsonl'):
        if l.get('capacity_bound'):bounded[l['contract']].append(l)
    expiry_checked=0;prepared=pathlib.Path(old['prepared'])
    for info in read(prepared/'coverage.json')['contracts']:
        if not bounded[info['contract']]:continue
        bars=rows(prepared/info['minutes_file']);ends={b['ts'] for b in bars};buckets=set()
        for b in bars:
            t=dt.datetime.fromisoformat(b['start'])
            buckets.add((t.replace(minute=t.minute//15*15)+dt.timedelta(minutes=15)).isoformat(sep=' '))
        buckets=sorted(buckets);gaps=read(prepared/info['gaps_file'])
        config=read(prepared/'protocol.json')['execution']
        for l in bounded[info['contract']]:
            latest=max((p['entry_bucket'] if p['entry'] is not None else config['pending_buckets'])
                       +config['holding_buckets']-1 for p in l['disagreement']['paths'] if p['status'] not in TERMINAL)
            first=bisect.bisect_right(buckets,l['warning_ts']);g=bisect.bisect_left(gaps,l['warning_ts'])
            if (first+latest-1>=len(buckets) or l['release_ts']!=buckets[first+latest-1]
                or l['release_ts'] not in ends or (g<len(gaps) and gaps[g]<l['release_ts'])
                or latest!=l['capacity_bound']['last_bucket'] or l['status']!='ambiguous' or l['net_yuan'] is not None):
                raise ValueError('unsupported ambiguous occupancy expiry')
            expiry_checked+=1
    checked=0
    for path in sorted((run/'evaluate').glob('*.ledger.jsonl')):
        name=path.name.removesuffix('.ledger.jsonl');strategy,slip=name.rsplit('_',1);slip=slip.removesuffix('tick')
        expected=summary['strategies'][strategy]['reports'][slip]['metrics'];active={};closed=[]
        for r in rows(path):
            if r['decision']!='accepted':continue
            s=snapshots[r['candidate_id']];l=r['label']
            if s['quality']['quality_score']<protocol['fixed_score_min'] or not l['plan']['planned_risk_ok']:
                raise ValueError('warning admission changed')
            if l['plan']['planned_loss_yuan']>500+1e-8:raise ValueError('risk cap violated')
            if active.get(r['symbol'],'')>r['warning_ts']:raise ValueError('product capacity violated')
            active[r['symbol']]=l.get('release_ts') or '9999-12-31 23:59:59'
            audit_label(s,l)
            if l['status']=='closed' and r['warning_ts']>=summary['evaluation_start']:closed.append(l)
        cash=sum((Decimal(str(l['net_yuan'])) for l in closed),Decimal(0))
        if len(closed)!=expected['closed'] or abs(float(cash)-expected['net_yuan'])>1e-5:
            raise ValueError('ledger does not reconcile '+name)
        checked+=1
    if checked!=12:raise ValueError('expected all twelve strategy/slippage books')
    result=dict(status='passed',books_reconciled=checked,unknown_expiry_bounds_verified=expiry_checked,
                replay=read(run/'replay/verification.json'),
                development_only=True,deployment='disabled')
    out=new_output(output);write(out/'verification.json',result)
    seal(out,'entry-independent-verification',[run/'replay/manifest.json',run/'evaluate/manifest.json'])
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',choices=['replay','evaluate','verify','run'])
    parser.add_argument('--input',required=True);parser.add_argument('--out',required=True)
    parser.add_argument('--protocol',default=str(pathlib.Path(__file__).with_name('factor-study-entry-v1.json')))
    args=parser.parse_args()
    if args.stage=='run':
        run=pathlib.Path(args.out)
        if run.exists():raise ValueError('use a new immutable run directory')
        replay_stage(args.input,args.protocol,run/'replay')
        evaluate_stage(run/'replay',run/'evaluate');print(verify_stage(run,run/'verify'))
    elif args.stage=='replay':replay_stage(args.input,args.protocol,args.out)
    elif args.stage=='evaluate':evaluate_stage(args.input,args.out)
    else:print(verify_stage(args.input,args.out))


if __name__=='__main__':main()
