"""Frozen eight-way execution study, matured ablations and B/positive-EV diagnostics."""
import argparse
import collections
import copy
import math
import pathlib

import numpy as np

from factor_study_data import (ROOT, Calendar, read, rows, write, write_rows,
                               new_output, seal, verify_seal)
from factor_study_execution import (MinuteBars, evaluate, capacity, metrics, audit_label)
from factor_study_quote_audit import check_quote
from factor_study_analysis import (load_context, eligible, observed_training, outer_windows,
                                   paired_blocks, refit_ablations, parameter_evidence)
from factor_study_models import (GROUPS, features, baseline_score, target_samples,
                                 fit_model, predict, groups_count)


def variant_id(target, no_follow, cap):
    return f'{target}_nf{no_follow if no_follow is not None else "off"}_cap{cap:g}'


def variants(protocol):
    return [dict(id=variant_id(target,nf,cap),target=target,no_follow=nf,cap=cap)
            for target in protocol['targets'] for nf in protocol['no_follow']
            for cap in protocol['risk_caps_yuan']]


def scenario_key(variant, slip):
    return (f'{variant["target"]}_{slip}tick' if variant['no_follow']==5
            else f'{variant["target"]}_nf_off_{slip}tick')


def admission_labels(labels, cap):
    """Counterfactual paths stay intact; the known-at-warning cap controls admission."""
    return {key:dict(label,plan=dict(label['plan'],
                planned_risk_ok=0<label['plan']['planned_loss_yuan']<=cap+1e-8))
            for key,label in labels.items()}


def trade_days(coverage, ledgers, start):
    dates={d for d in coverage['trading_dates'] if d>=start[:10]}
    dates.update(r['label'].get('warning_trading_day',r['warning_ts'][:10])
                 for ledger in ledgers for r in ledger if r['warning_ts']>=start)
    return sorted(dates)


def accepted(ledger,start):
    return [r['label'] for r in ledger if r['decision']=='accepted' and r['warning_ts']>=start]


def cohort(ledger,start):
    return [r for r in ledger if r['warning_ts']>=start]


def interval(left,right,coverage,start,protocol):
    dates=trade_days(coverage,[left,right],start)
    stat=protocol['statistics']
    return {str(block):paired_blocks(cohort(left,start),cohort(right,start),dates,
               stat['bootstrap_draws'],block,protocol['optimization']['seed']) for block in (5,10)}


def extra_replay(original,protocol_path,output):
    prepared,scan,snapshots,_,base,coverage=load_context(original)
    protocol=read(protocol_path)
    if protocol['schema']!='factor-study-followup-v1' or len(variants(protocol))!=8:
        raise ValueError('followup must contain exactly eight frozen structures')
    out=new_output(output)
    write(out/'protocol.json',protocol)
    write(out/'context.json',dict(original_replay=str(pathlib.Path(original).resolve()),
                                 prepared=str(prepared),scan=str(scan)))
    calendar=Calendar(read(prepared/'calendar.json'),base['night_ends'])
    fees=read(prepared/'fees.json');config=dict(base['execution'],no_follow_bucket=None)
    bycontract=collections.defaultdict(list)
    for snapshot in snapshots:bycontract[snapshot['contract']].append(snapshot)
    counts=collections.Counter();gaps=[]
    def generated():
        for info in coverage['contracts']:
            bars=MinuteBars(rows(prepared/info['minutes_file']))
            quotes={b['ts']:b for b in bars};missing=read(prepared/info['gaps_file'])
            spec=fees['products'][info['symbol'][:-1]]
            for snapshot in bycontract[info['contract']]:
                for target in protocol['targets']:
                    for slip in protocol['slip_ticks']:
                        label=evaluate(snapshot,bars,missing,spec,calendar,config,target,slip)
                        label['scenario']=f'{target}_nf_off_{slip}tick'
                        label['warning_trading_day']=calendar.day(label['warning_ts'])
                        audit_label(snapshot,label)
                        if label['status']=='closed':
                            gap=check_quote(snapshot,label,quotes[label['entry_ts']],quotes[label['exit_ts']],spec,calendar)
                            counts['quote_and_fee_verified']+=1
                            if gap:gaps.append(dict(candidate_id=label['candidate_id'],scenario=label['scenario'],
                                                    convention='frozen target, no favorable opening improvement'))
                        counts[label['status']]+=1
                        yield label
            print(f'{info["contract"]}: no-follow-off replay complete',flush=True)
    write_rows(out/'labels.jsonl',generated())
    write(out/'verification.json',dict(status='passed',counts=dict(counts),conservative_target_gaps=gaps,
                                      risk_cap_derivation='200 cap is an admission filter on known plan risk; no stop is tightened.'))
    seal(out,'followup-replay',[pathlib.Path(original)/'manifest.json',prepared/'manifest.json',
                              scan/'snapshots.jsonl',protocol_path])
    return out


def load_followup(path):
    path=pathlib.Path(path);verify_seal(path)
    context=read(path/'context.json')
    prepared,scan,snapshots,maps,base,coverage=load_context(context['original_replay'])
    for label in rows(path/'labels.jsonl'):
        maps[label['scenario']][label['candidate_id']]=label
    return prepared,scan,snapshots,maps,base,coverage,read(path/'protocol.json')


def exit_counterfactuals(snapshots,maps,study_start,score_min,books=None,evaluation_start=None):
    eligible_ids={s['candidate_id'] for s in snapshots if s['warning_ts']>=study_start
                  and s['quality']['status']=='ok' and baseline_score(s)>=score_min}
    output=[]
    for target in ('cash200','s1'):
        for slip in (1,2):
            original=maps[f'{target}_{slip}tick'];off=maps[f'{target}_nf_off_{slip}tick']
            for cap in (200.,500.):
                pairs=[];unknown=0
                for key in eligible_ids:
                    a,b=original[key],off[key]
                    if not a['plan']['planned_risk_ok'] or a['plan']['planned_loss_yuan']>cap:continue
                    if a['status']!='closed' or a['reason']!='no_follow':continue
                    if b['status']!='closed' or a['entry']!=b['entry'] or a['entry_ts']!=b['entry_ts']:
                        unknown+=1;continue
                    pairs.append((a,b))
                differences=[b['net_yuan']-a['net_yuan'] for a,b in pairs]
                output.append(dict(target=target,slip=slip,cap=cap,paired=len(pairs),unresolved_or_entry_mismatch=unknown,
                    mean_off_minus_on=float(np.mean(differences)) if differences else None,
                    total_off_minus_on=sum(differences),off_better=sum(x>1e-7 for x in differences),
                    off_worse=sum(x< -1e-7 for x in differences),
                    original_mean=float(np.mean([a['net_yuan'] for a,b in pairs])) if pairs else None,
                    off_mean=float(np.mean([b['net_yuan'] for a,b in pairs])) if pairs else None,
                    scope='Matched independent no-follow exits only; full capacity is evaluated separately.'))
                if books:
                    ids={r['candidate_id'] for r in books[variant_id(target,5,cap)][slip]
                         if r['decision']=='accepted' and r['warning_ts']>=(evaluation_start or study_start)}
                    taken=[(a,b) for a,b in pairs if a['candidate_id'] in ids]
                    delta=[b['net_yuan']-a['net_yuan'] for a,b in taken]
                    output.append(dict(target=target,slip=slip,cap=cap,paired=len(taken),
                        mean_off_minus_on=float(np.mean(delta)) if delta else None,total_off_minus_on=sum(delta),
                        off_better=sum(x>1e-7 for x in delta),off_worse=sum(x< -1e-7 for x in delta),
                        scope='Accepted NF-on post-training cohort; paired exit-only counterfactual, not a rerun account.'))
    return output


def strategy_candidate(one,two,comparison,minimum):
    if min(one['closed'],two['closed'])<minimum:return 'insufficient_trades'
    ci=comparison['5'].get('interval95')
    if (one['net_yuan']>0 and two['net_yuan']>0 and ci and ci[0]>0
        and min(one['known_fraction'],two['known_fraction'])>=.95):
        return 'development_candidate_on_priced_subset'
    return 'not_supported'


def evaluate_structures(snapshots,maps,base,coverage,protocol,out):
    first=next(outer_windows(coverage['trading_dates'],base),None)
    evaluation_start=first[1] if first else base['end']
    scores={s['candidate_id']:baseline_score(s) for s in snapshots}
    thresholds={k:protocol['fixed_score_min'] for k in scores}
    ledgers={};output={}
    for variant in variants(protocol):
        byslip={};reports={}
        for slip in protocol['slip_ticks']:
            labels=admission_labels(maps[scenario_key(variant,slip)],variant['cap'])
            ledger=capacity(snapshots,labels,scores,thresholds)
            byslip[slip]=ledger
            full=accepted(ledger,base['start']);post=accepted(ledger,evaluation_start)
            reports[str(slip)]=dict(full_window=metrics(full),post_training=metrics(post),
                                  reasons={reason:metrics(l for l in post if l['reason']==reason)
                                           for reason in ('stop','target','no_follow','time_exit')},
                                  by_symbol={symbol:metrics(l for l in post if l['symbol']==symbol)
                                             for symbol in sorted({l['symbol'] for l in post})},
                                  by_month={month:metrics(l for l in post if l['warning_ts'][:7]==month)
                                            for month in sorted({l['warning_ts'][:7] for l in post})})
            write_rows(out/f'{variant["id"]}_{slip}tick.ledger.jsonl',ledger)
        ledgers[variant['id']]=byslip
        zero=interval([],byslip[1],coverage,evaluation_start,base)
        output[variant['id']]=dict(variant=variant,metrics=reports,versus_no_trade=zero,
            status=strategy_candidate(reports['1']['post_training'],reports['2']['post_training'],zero,
                                      protocol['minimum_forward_candidate_closed']))
        print(variant['id']+': fixed score execution evaluated',flush=True)
    baseline=ledgers[protocol['model_primary_variant']][1]
    for name,value in output.items():
        value['versus_original_structure']=interval(baseline,ledgers[name][1],coverage,evaluation_start,base)
    return dict(evaluation_start=evaluation_start,variants=output,no_trade=dict(net_yuan=0.,trades=0)),ledgers


def model_spec(name,protocol):
    names=[n for group in GROUPS.values() for n in group]
    if name=='without_retracement':names.remove('retracement')
    return dict(names=names,family='linear' if name in ('linear','without_retracement') else
                'spline' if name=='b_spline' else 'interaction',alpha=protocol['model_alpha'],
                knot_features=['retracement'],interactions=protocol['b_interactions'] if name=='b_conditional' else [])


def positive_threshold():
    return math.nextafter(0.,math.inf)


def model_study(snapshots,maps,base,coverage,protocol,out):
    labels=maps['cash200_1tick'];population=eligible(snapshots,labels)
    byid={s['candidate_id']:s for s in snapshots}
    first=next(outer_windows(coverage['trading_dates'],base),None)
    first_start=first[1] if first else base['end']
    score_maps={f'{model}_{gate}':{} for model in protocol['b_models'] for gate in protocol['admissions']}
    threshold_maps={name:{} for name in score_maps};artifacts=[];forecasts=[];curves=[]
    for name in score_maps:
        for s in snapshots:
            if s['warning_ts']<first_start:
                k=s['candidate_id'];score_maps[name][k]=baseline_score(s);threshold_maps[name][k]=protocol['fixed_score_min']
    for index,start,end in outer_windows(coverage['trading_dates'],base):
        train=observed_training(snapshots,labels,start,end)
        samples=target_samples(train,labels)
        if groups_count(samples)<base['validation']['minimum_training_groups']:
            artifacts.append(dict(start=start,status='insufficient_training_groups'));continue
        # Same warning-time gate across models; labels are never used for admissions.
        reference=[s for s in population if s['warning_ts']<start and baseline_score(s)>=protocol['fixed_score_min']]
        test=[s for s in population if start<=s['warning_ts']<end and baseline_score(s)>=protocol['fixed_score_min']]
        for name in protocol['b_models']:
            spec=model_spec(name,protocol)
            model=fit_model(samples,**spec)
            if spec['family']!='linear' and groups_count(samples)<max(base['validation']['minimum_complex_groups'],
                    base['validation']['groups_per_design_column']*model['design_columns']):
                artifacts.append(dict(start=start,model=name,status='insufficient_complex_groups'));continue
            historical=predict(target_samples(reference,labels),model) if reference else []
            if not historical:
                artifacts.append(dict(start=start,model=name,status='no_prior_gate_population'));continue
            rank_cut=float(np.quantile(historical,.8))
            predictions=predict(target_samples(test,labels),model)
            for s,prediction in zip(test,predictions):
                k=s['candidate_id']
                for gate in protocol['admissions']:
                    key=name+'_'+gate;score_maps[key][k]=prediction
                    threshold_maps[key][k]=rank_cut if gate=='rank20' else positive_threshold()
                forecasts.append(dict(candidate_id=k,model=name,fold_start=start,fold_end=end,
                                      predicted_net_yuan=prediction,label=labels[k]))
            artifacts.append(dict(start=start,end=end,model=name,status='fit',artifact=model,rank_threshold=rank_cut,
                                  training_last_exit=model['training_last_exit'],threshold_positive=positive_threshold()))
        # Condition cells use observed test samples with training-defined bounds, not invented feature combinations.
        for a in ('a_move_atr','a_core_efficiency'):
            bounds={}
            for feature in (a,'retracement'):
                observed=[r['features'].get(feature) for r in samples if r['features'].get(feature) is not None]
                bounds[feature]=np.unique(np.quantile(observed,[1/3,2/3])).tolist() if observed else []
            cells=collections.defaultdict(list)
            for s in test:
                f=features(s,labels[s['candidate_id']]);x,y=f.get(a),f.get('retracement')
                if x is not None and y is not None:
                    key=f'{np.searchsorted(bounds[a],x,side="right")},{np.searchsorted(bounds["retracement"],y,side="right")}'
                    cells[key].append(s['candidate_id'])
            curves.append(dict(start=start,end=end,features=[a,'retracement'],training_bounds=bounds,
                               cells={key:dict(groups=len({byid[k]['group_id'] for k in ids}),
                                     sufficient=len({byid[k]['group_id'] for k in ids})>=30,
                                     metrics=metrics(labels[k] for k in ids)) for key,ids in cells.items()}))
        print(f'{start[:10]}: fixed B-models and positive-EV forecasts complete',flush=True)
    ledgers={};summary={}
    for name in score_maps:
        results={};byslip={}
        for slip in (1,2):
            ledger=capacity(snapshots,maps[f'cash200_{slip}tick'],score_maps[name],threshold_maps[name])
            byslip[slip]=ledger;results[str(slip)]=metrics(accepted(ledger,first_start))
            write_rows(out/f'{name}_{slip}tick.ledger.jsonl',ledger)
        zero=interval([],byslip[1],coverage,first_start,base)
        summary[name]=dict(metrics=results,versus_no_trade=zero,
            status=strategy_candidate(results['1'],results['2'],zero,protocol['minimum_forward_candidate_closed']))
        ledgers[name]=byslip
    comparisons={}
    for gate in protocol['admissions']:
        reference=ledgers['linear_'+gate][1]
        for name in protocol['b_models']:
            comparisons[name+'_'+gate]=interval(reference,ledgers[name+'_'+gate][1],coverage,first_start,base)
    calibration={}
    for name in protocol['b_models']:
        grouped=collections.defaultdict(list)
        for f in forecasts:
            if f['model']!=name or f['label']['status']!='closed':continue
            grouped[str(np.searchsorted(protocol['calibration_edges_yuan'],f['predicted_net_yuan'],side='right'))].append(f)
        calibration[name]={key:dict(n=len(v),mean_predicted=float(np.mean([r['predicted_net_yuan'] for r in v])),
                              mean_realized=float(np.mean([r['label']['net_yuan'] for r in v]))) for key,v in grouped.items()}
    write(out/'model-artifacts.json',artifacts)
    write_rows(out/'forecasts.jsonl',forecasts)
    write(out/'b-condition-cells.json',curves)
    write(out/'ev-calibration.json',dict(edges=protocol['calibration_edges_yuan'],bins=calibration,
                                       scope='Matured closed outcomes only, unknown counts remain in strategy metrics.'))
    return dict(evaluation_start=first_start,strategies=summary,versus_linear=comparisons,
                selection='Four preregistered model forms, no search for a winning alpha or threshold.'),ledgers


def validate_ledger(ledger):
    active={}
    for row in ledger:
        if row['decision']!='accepted':continue
        label=row['label'];symbol=row['symbol']
        if active.get(symbol,'')>row['warning_ts']:raise ValueError('overlapping product occupancy')
        if not label['plan']['planned_risk_ok']:raise ValueError('accepted risk-ineligible candidate')
        active[symbol]=label.get('release_ts') or (label['exit_ts'] if label['status'] in
                       {'closed','invalidated','untriggered','invalid_target'} else '9999-12-31 23:59:59')


def evaluate_stage(extra_path,output):
    prepared,scan,snapshots,maps,base,coverage,protocol=load_followup(extra_path)
    out=new_output(output)
    write(out/'protocol.json',protocol)
    structures,books=evaluate_structures(snapshots,maps,base,coverage,protocol,out)
    models,modelbooks=model_study(snapshots,maps,base,coverage,protocol,out)
    for byslip in [*books.values(),*modelbooks.values()]:
        for ledger in byslip.values():validate_ledger(ledger)
    artifacts=read(out/'model-artifacts.json')
    for item in artifacts:
        if item['status']=='fit' and item['training_last_exit']>=item['start']:
            raise ValueError('future payoff entered model training')
    summary=dict(schema='factor-followup-results-v1',development_only=True,base_period=[base['start'],base['end']],
                 structures=structures,b_models=models,
                 no_follow_counterfactuals=exit_counterfactuals(snapshots,maps,base['start'],protocol['fixed_score_min'],
                                                               books,structures['evaluation_start']),
                 verification=dict(status='passed',ledgers=32,training_cutoffs=True),
                 deployment='disabled',unknown_policy='Report priced outcomes and unknowns separately, never zero-fill actual unknown P&L.')
    write(out/'summary.json',summary)
    seal(out,'followup-evaluate',[pathlib.Path(extra_path)/'manifest.json',prepared/'manifest.json',scan/'manifest.json'])
    return out


def reassess_stage(original,old_diagnosis,output):
    prepared,scan,snapshots,maps,base,coverage=load_context(original)
    old_diagnosis=pathlib.Path(old_diagnosis);verify_seal(old_diagnosis)
    out=new_output(output)
    correlated=read(old_diagnosis/'correlated-groups.json')
    refitted=refit_ablations(snapshots,maps,coverage['trading_dates'],base,correlated)
    names=[n for group in GROUPS.values() for n in group]
    evidence=parameter_evidence(refitted,names,base,coverage['trading_dates'])
    for row in refitted:
        if 'training_last_exit' in row and row['training_last_exit']>=row['start']:
            raise ValueError('matured result leaked into training')
    write(out/'refitted-ablations.json',refitted)
    write(out/'parameter-evidence.json',evidence)
    summary=dict(schema='factor-matured-evidence-v1',status='passed',
                 matured_after_fold=sum(r.get('matured_after_fold',0) for r in refitted),
                 states=dict(collections.Counter(r['status'] for r in evidence)),
                 priorities=dict(collections.Counter(r['priority'] for r in evidence)),
                 correction='End-of-fold cohort admission is unchanged; outcomes mature until research end. Inner selection remains as-of.',
                 unknown_caveat='Any positive evidence is conditional on priced outcomes; unknown payoff is not bounded or imputed.')
    write(out/'summary.json',summary)
    seal(out,'followup-reassess',[pathlib.Path(original)/'manifest.json',old_diagnosis/'manifest.json',prepared/'manifest.json'])
    return out


def report_stage(evaluation,reassessment,output):
    evaluation,reassessment=map(pathlib.Path,(evaluation,reassessment))
    verify_seal(evaluation);verify_seal(reassessment)
    out=new_output(output);summary=read(evaluation/'summary.json');evidence=read(reassessment/'parameter-evidence.json')
    write(out/'summary.json',summary)
    def fmt(x):return '—' if x is None else f'{x:,.2f}'
    lines=['# 下一轮交易结构与B回撤研究','',
           '**历史开发诊断；正式权重保持原样，未启动实盘或前瞻。未知结果未按零收益填补。**','',
           f'数据窗口：{summary["base_period"][0]} 至 {summary["base_period"][1]}。',
           f'统一评估起点：{summary["structures"]["evaluation_start"]}。所有8组固定v1≥70、原突破及原结构止损。','',
           '## 八组交易结构','',
           '下表为评估期预警归属的可确认已完成部分；持仓可以跨月继续退出。计划风险上限不是跳空实际损失保证。','',
           '|结构|1tick完成|未知|净元|均净元|净R|2tick净元|对不交易的95%区间|',
           '|---|---:|---:|---:|---:|---:|---:|---|']
    for name,item in summary['structures']['variants'].items():
        a,b=item['metrics']['1']['post_training'],item['metrics']['2']['post_training']
        ci=item['versus_no_trade']['5'].get('interval95')
        lines.append(f'|{name}|{a["closed"]}|{a["unknown"]}|{fmt(a["net_yuan"])}|{fmt(a["mean_net_yuan"])}|{fmt(a["mean_net_r"])}|{fmt(b["net_yuan"])}|{ci}|')
    lines+=['','## B回撤模型与不交易基线','',
            '本部分固定cash200、无跟随第5根、计划上限500元。四种模型只在过去已结束交易上拟合，岭惩罚固定10；正式评分未改。',
            'rank20门槛来自过去合格预警的预测分布；positive_ev只接受预测净元严格>0。模型尚不能训练时不接新单，既有仓位继续。','',
            '|模型与准入|完成|未知|净元|均净元|2tick净元|研究状态|','|---|---:|---:|---:|---:|---:|---|']
    for name,item in summary['b_models']['strategies'].items():
        a,b=item['metrics']['1'],item['metrics']['2']
        lines.append(f'|{name}|{a["closed"]}|{a["unknown"]}|{fmt(a["net_yuan"])}|{fmt(a["mean_net_yuan"])}|{fmt(b["net_yuan"])}|{item["status"]}|')
    lines+=['','## 修正后的参数证据','',
            '训练仍严格按当时可见结果；外层本折预警的后续退出追踪到冻结研究末日。最终歧义及研究尾部仓位仍单列。',
            '原先“每折任何未决均否决”改为覆盖检查与明确的已定价子集证据，另列39项探索的BH调整；任何积极结果都不能写成完整策略或因果验证。','',
            '|参数|正增量折/总折|保留相对删除的净元差95%区间|BH q|证据状态|',
            '|---|---|---|---:|---|']
    for item in evidence:
        lines.append(f'|{item["feature"]}|{item["positive_folds"]}/{item["total_folds"]}|{item["paired_date_interval"].get("interval95")}|{fmt(item.get("bh_q"))}|{item["status"]}|')
    lines+=['','## 解释与产物','',
            '八组收益差同时包含退出与占用变化；no_follow_counterfactuals另列原提前退出样本取消规则后的同入场路径差，不据一个亏损分组直接删规则。',
            'B条件格仅使用实际观察到的A/B组合与训练期边界，不造出数学上不一致的特征组合，不从最佳格子直接设新阈值。',
            'EV分箱预测与实现均值见ev-calibration.json；预测>0不保证实现>0。对不交易基线净元为0、交易数为0。',
            '费用仍为快照，合约映射原时点可知性未确认；全部历史已参与开发，不作为独立确认集。',
            '阶段代码、参数、全部账本及输入输出哈希已冻结。下一候选必须在未用于设计的新信号上验证。']
    (out/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    seal(out,'followup-report',[evaluation/'manifest.json',reassessment/'manifest.json'])
    return out


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',choices=['replay','evaluate','reassess','report','run'])
    parser.add_argument('--input',required=True)
    parser.add_argument('--protocol',default=str(pathlib.Path(__file__).with_name('factor-study-followup-v1.json')))
    parser.add_argument('--diagnosis');parser.add_argument('--reassessment');parser.add_argument('--out',required=True)
    args=parser.parse_args()
    if args.stage in {'run','reassess'} and not args.diagnosis:parser.error('--diagnosis is required')
    if args.stage=='run':
        out=new_output(args.out)
        extra=extra_replay(args.input,args.protocol,out/'replay')
        evaluation=evaluate_stage(extra,out/'evaluate')
        reassessment=reassess_stage(args.input,args.diagnosis,out/'reassess')
        report_stage(evaluation,reassessment,out/'report')
    elif args.stage=='replay':extra_replay(args.input,args.protocol,args.out)
    elif args.stage=='evaluate':evaluate_stage(args.input,args.out)
    elif args.stage=='reassess':reassess_stage(args.input,args.diagnosis,args.out)
    else:
        if not args.reassessment:parser.error('--reassessment is required')
        report_stage(args.input,args.reassessment,args.out)


if __name__=='__main__':main()
