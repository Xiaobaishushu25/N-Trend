"""Purged time validation, fixed/refitted ablations and explicit research evidence."""
import collections
import bisect
import copy
import datetime as dt
import math
import pathlib
import numpy as np
from scipy import stats
from scipy.cluster.hierarchy import linkage, fcluster
from scipy.spatial.distance import squareform

from factor_study_data import Calendar,read, rows, write, write_rows, new_output, seal, verify_seal
from factor_study_execution import capacity, metrics, UNKNOWN, audit_label
from factor_study_models import (GROUPS, INTERACTIONS, features, fit_model, predict, module_scores,
                                baseline_score, weight_candidates, target_samples, groups_count, group_weights)
from factor_study_models import feature_dictionary


class LabelMaps(collections.defaultdict):
    def __init__(self):
        super().__init__(dict)
        self.warm_cache={}


def load_context(replayed):
    replayed=pathlib.Path(replayed); verify_seal(replayed)
    context=read(replayed/'context.json'); prepared=pathlib.Path(context['prepared']); scan=pathlib.Path(context['scan'])
    verify_seal(prepared)
    snapshots=rows(scan/'snapshots.jsonl'); all_labels=rows(replayed/'labels.jsonl')
    protocol=read(prepared/'protocol.json')
    calendar=Calendar(read(prepared/'calendar.json'),protocol['night_ends'])
    maps=LabelMaps()
    for label in all_labels:
        label['warning_trading_day']=calendar.day(label['warning_ts'])
        maps[label['scenario']][label['candidate_id']]=label
    for s in snapshots:
        s['_factor_features']=features(s,maps['cash200_1tick'][s['candidate_id']])
    return prepared,scan,snapshots,maps,protocol,read(prepared/'coverage.json')


def eligible(snapshots,labels):
    return [s for s in snapshots if s.get('in_window',True) and s['quality']['status']=='ok'
            and labels[s['candidate_id']]['plan']['planned_risk_ok']]


def observed_training(snapshots,labels,cutoff,end):
    test_groups={s['group_id'] for s in snapshots if cutoff<=s['warning_ts']<end}
    return [s for s in eligible(snapshots,labels) if s['warning_ts']<cutoff and s['group_id'] not in test_groups
            and labels[s['candidate_id']]['status']=='closed' and labels[s['candidate_id']]['exit_ts']<cutoff]


def month_start_shift(date,offset):
    value=dt.date.fromisoformat(date[:10]).replace(day=1)
    number=value.year*12+value.month-1+offset
    return dt.date(number//12,number%12+1,1).isoformat()


def outer_windows(days,protocol):
    v=protocol['validation']
    if v.get('schedule','trading_days')=='monthly':
        first=month_start_shift(protocol['start'],v.get('initial_months',6)+1)
        boundary=first
        while boundary<protocol['end'][:10]:
            index=bisect.bisect_left(days,boundary)
            if index>=len(days):break
            following=month_start_shift(boundary,1)
            end_index=bisect.bisect_left(days,following)
            end=days[end_index]+' 00:00:00' if end_index<len(days) else protocol['end']
            yield index,days[index]+' 00:00:00',end
            boundary=following
    else:
        for index in range(v['initial_days'],len(days),v['outer_days']):
            end_index=min(index+v['outer_days'],len(days))
            end=days[end_index]+' 00:00:00' if end_index<len(days) else protocol['end']
            yield index,days[index]+' 00:00:00',end


def inner_windows(days,outer_index,protocol):
    v=protocol['validation']
    if v.get('schedule','trading_days')=='monthly':
        current=days[outer_index]
        for offset in (3,2,1):
            start=month_start_shift(current,-offset)+' 00:00:00'
            end=(month_start_shift(current,-offset+1)+' 00:00:00' if offset>1 else current+' 00:00:00')
            yield start,end
    else:
        for offset in reversed(range(v['inner_folds'])):
            end_index=outer_index-offset*v['inner_days']
            begin=end_index-v['inner_days']
            yield days[begin]+' 00:00:00',days[end_index]+' 00:00:00'


def asof_label(label,end,inclusive=False):
    if label['status'] in {'filtered_risk','unscorable'}:return label
    def visible(ts):return ts is not None and (ts<end or inclusive and ts==end)
    if visible(label['exit_ts']): return label
    value=copy.deepcopy(label)
    entered=visible(value.get('entry_ts'))
    if not entered:
        value.update(entry=None,entry_ts=None,possibly_entered=False,fills=[],target=None,risk_yuan=None,entry_bucket=None)
        value.pop('open_fee',None)
    else:
        value['fills']=[fill for fill in value.get('fills',[]) if visible(fill['ts'])]
    value.update(status='unfinished' if value['entry'] is not None else 'pending',exit_ts=None,
                 exit=None,net_yuan=None,net_r=None,gross_yuan=None,fees=None,hit200=None,reason=None,peak=None,
                 release_ts=None,disagreement=None,observed_through=end)
    if value.get('last_observed_ts') and not visible(value['last_observed_ts']):value['last_observed_ts']=None
    for key in ('close_fee','mark_to_market_yuan'):
        value.pop(key,None)
    return value


def policy_evaluation(snapshots,maps,scores,threshold,start,end,outcome_end=None):
    """Warm existing positions with frozen v1>=70; change only new warnings."""
    observation_end=outcome_end or end
    if observation_end<end:raise ValueError('outcome cutoff precedes cohort end')
    selected=[s for s in snapshots if start<=s['warning_ts']<end]
    score_map={s['candidate_id']:scores[s['candidate_id']] for s in selected if s['candidate_id'] in scores}
    cut_map={k:threshold for k in score_map}
    outputs={}
    ledgers={}
    for slip in (1,2):
        labels=maps[f'cash200_{slip}tick']
        cache=getattr(maps,'warm_cache',{})
        cache_key=(start,slip)
        if cache_key not in cache:
            prior=[s for s in snapshots if s['warning_ts']<start]
            warm=capacity(prior,labels,{s['candidate_id']:baseline_score(s) for s in prior},
                          {s['candidate_id']:70. for s in prior})
            active={}
            for row in warm:
                if row['decision']=='accepted':
                    label=row['label']
                    active[row['symbol']]=label.get('release_ts') or (label['exit_ts'] if label['status'] in
                        {'closed','invalidated','untriggered','invalid_target'} else '9999-12-31 23:59:59')
            cache[cache_key]=active
        ledger=capacity(selected,labels,score_map,cut_map,cache[cache_key])
        accepted=[asof_label(r['label'],observation_end,inclusive=outcome_end is not None)
                  for r in ledger if r['decision']=='accepted' and start<=r['warning_ts']<end]
        outputs[str(slip)]=metrics(accepted)
        ledgers[str(slip)]=[dict(r,label=asof_label(r['label'],observation_end,inclusive=outcome_end is not None),
                               fold_end_status=asof_label(r['label'],end)['status'])
                             for r in ledger if start<=r['warning_ts']<end]
    return outputs,ledgers


def train_configuration(config,train,population,labels,protocol):
    artifact=None
    if not population:return None
    if config['kind']=='weights':
        scores={s['candidate_id']:float(np.dot(module_scores(s),config['weights'])) for s in population}
    else:
        samples=target_samples(train,labels)
        if groups_count(samples)<protocol['validation']['minimum_training_groups']:
            return None
        artifact=fit_model(samples,config['names'],config['family'],config['alpha'],config.get('interactions',[]))
        v=protocol['validation']
        if config['family']!='linear' and groups_count(samples)<max(v['minimum_complex_groups'],v['groups_per_design_column']*artifact['design_columns']):
            return None
        values=predict(target_samples(population,labels),artifact)
        scores={s['candidate_id']:p for s,p in zip(population,values)}
    training_ids={s['candidate_id'] for s in train}
    # Threshold population uses all earlier eligible warnings, including untriggered ones.
    cutoff=config.get('cutoff')
    reference=[v for k,v in scores.items() if k in cutoff] if cutoff is not None else [scores[k] for k in training_ids]
    if not reference: return None
    threshold=float(np.quantile(reference,protocol['optimization']['selection_quantile']))
    return scores,threshold,artifact


def configurations(protocol):
    candidates=[dict(id='weights:'+name,kind='weights',weights=w) for name,w in
                weight_candidates(protocol['optimization']['sobol_weights'],protocol['optimization']['seed'])]
    names=[n for values in GROUPS.values() for n in values]
    for family in ('linear','spline','interaction'):
        for alpha in protocol['optimization']['alphas']:
            candidates.append(dict(id=f'{family}:{alpha}',kind='model',family=family,alpha=alpha,names=names,
                                   interactions=INTERACTIONS))
    for alpha in protocol['optimization']['alphas']:
        candidates.append(dict(id=f'space_cost:{alpha}',kind='model',family='linear',alpha=alpha,
                               names=GROUPS['space_cost'],interactions=[]))
    return candidates


def valid_candidate(results,baseline,protocol):
    v,o=protocol['validation'],protocol['optimization']
    for r,b in zip(results,baseline):
        a=r['1']; base=b['1']
        if a['closed']<v['minimum_inner_closed'] or a['known_fraction']<v['minimum_known_fraction']: return False
        if a['closed']<base['closed']*o['minimum_baseline_trade_fraction']: return False
        if a['max_drawdown_yuan']>base['max_drawdown_yuan']+o['allowed_drawdown_increase_yuan']: return False
        if r['2']['net_yuan']<b['2']['net_yuan']: return False
    return True


def inner_select(snapshots,maps,days,outer_index,protocol):
    labels=maps['cash200_1tick']; pop=eligible(snapshots,labels)
    v=protocol['validation']; windows=[]
    for start,end in inner_windows(days,outer_index,protocol):
        train=observed_training(snapshots,labels,start,end)
        if groups_count(target_samples(train,labels))<v['minimum_training_groups']:
            return None,[],dict(reason='insufficient_inner_training_groups',start=start,groups=len({s['group_id'] for s in train}))
        population=[s for s in pop if s['warning_ts']<end]
        baseline_scores={s['candidate_id']:baseline_score(s) for s in population}
        cutoff_ids={s['candidate_id'] for s in population if s['warning_ts']<start}
        cut=float(np.quantile([baseline_scores[k] for k in cutoff_ids],.8))
        baseline,_=policy_evaluation(snapshots,maps,baseline_scores,cut,start,end)
        windows.append((start,end,train,population,cutoff_ids,baseline))
    trials=[]; winners=[]
    for config in configurations(protocol):
        results=[]
        for start,end,train,population,cutoff_ids,baseline in windows:
            configured=dict(config,cutoff=cutoff_ids)
            fitted=train_configuration(configured,train,population,labels,protocol)
            if fitted is None: break
            scores,threshold,_=fitted
            evaluated,_=policy_evaluation(snapshots,maps,scores,threshold,start,end)
            results.append(evaluated)
        admitted=(len(results)==len(windows) and valid_candidate(results,[w[-1] for w in windows],protocol))
        total=sum(r['1']['net_yuan'] for r in results)
        count=sum(r['1']['closed'] for r in results)
        trials.append(dict(config=config,valid=admitted,inner=results,objective_total_yuan=total,
                           objective_mean_yuan=total/count if count else None))
        if admitted:
            # Simpler models win exact ties; no extra search is appended after inspection.
            complexity=0 if config['kind']=='weights' else 1+('linear','spline','interaction').index(config['family'])
            winners.append(((total,total/count if count else -math.inf,-complexity,config['id']),config))
    best=max(winners,key=lambda x:x[0])[1] if winners else None
    return best,trials,dict(reason='selected' if best else 'no_candidate_passed',windows=[dict(start=w[0],end=w[1]) for w in windows])


def paired_blocks(left,right,days,draws,block,seed):
    if len(days)<block: return dict(status='insufficient_dates')
    indices={d:i for i,d in enumerate(days)}; values=np.zeros(len(days))
    for sign,ledger in ((-1,left),(1,right)):
        for r in ledger:
            label=r['label']
            if r['decision']=='accepted' and label['status']=='closed':
                date=label.get('warning_trading_day',label['warning_ts'][:10])
                if date not in indices:raise ValueError('bootstrap date grid drops a closed trade: '+date)
                values[indices[date]]+=sign*label['net_yuan']
    rng=np.random.default_rng(seed); chunks=math.ceil(len(days)/block)
    starts=rng.integers(0,len(days)-block+1,size=(draws,chunks))
    positions=(starts[:,:,None]+np.arange(block)).reshape(draws,-1)[:,:len(days)]
    sampled=values[positions].sum(axis=1)
    return dict(delta_total_yuan=float(values.sum()),interval95=np.quantile(sampled,[.025,.975]).tolist(),
                centered_bootstrap_p=float((1+np.sum(np.abs(sampled-values.sum())>=abs(values.sum())))/(draws+1)),
                block_days=block,draws=draws,method='joint-date fixed-ledger descriptive blocks; not whole-search uncertainty')


def fit_stage(replayed,output):
    prepared,scan,snapshots,maps,protocol,coverage=load_context(replayed)
    out=new_output(output); labels=maps['cash200_1tick']; population=eligible(snapshots,labels)
    days=coverage['trading_dates']; v=protocol['validation']; seed=protocol['optimization']['seed']
    folds=[]; artifacts=[]; trials_all=[]; auxiliary=[]; score_maps={k:{} for k in ('adaptive','v1','equal','space_cost')}
    thresholds={k:{} for k in score_maps}; outer_start=None
    (out/'folds').mkdir()
    for index,start,end in outer_windows(days,protocol):
        if outer_start is None: outer_start=start
        best,trials,selection=inner_select(snapshots,maps,days,index,protocol)
        train=observed_training(snapshots,labels,start,end)
        validpop=[s for s in population if s['warning_ts']<end]
        cutoff_ids={s['candidate_id'] for s in validpop if s['warning_ts']<start}
        default=dict(id='baseline_fallback',kind='weights',weights=[.20,.36,.24,.20])
        chosen=best or default
        configured=dict(chosen,cutoff=cutoff_ids)
        fitted=train_configuration(configured,train,validpop,labels,protocol)
        if fitted is None: fitted=train_configuration(dict(default,cutoff=cutoff_ids),train,validpop,labels,protocol)
        scores,cut,artifact=fitted if fitted is not None else ({},1e300,None)
        fold=dict(index=len(folds),start=start,end=end,chosen=chosen,selection=selection,
                  training_groups=len({s['group_id'] for s in train}),threshold=cut)
        evaluated,_=policy_evaluation(snapshots,maps,scores,cut,start,end); fold['metrics']=evaluated
        configs={'adaptive':(scores,cut),'v1':({s['candidate_id']:baseline_score(s) for s in validpop},None),
                 'equal':({s['candidate_id']:float(np.mean(module_scores(s))) for s in validpop},None)}
        space_config=dict(id='space',kind='model',family='linear',alpha=10.,names=GROUPS['space_cost'],cutoff=cutoff_ids)
        space_fit=train_configuration(space_config,train,validpop,labels,protocol)
        configs['space_cost']=(space_fit[0],space_fit[1]) if space_fit else configs['v1']
        for name,(values,threshold) in configs.items():
            if threshold is None: threshold=float(np.quantile([values[k] for k in cutoff_ids],.8)) if cutoff_ids else 1e300
            for s in population:
                if start<=s['warning_ts']<end:
                    k=s['candidate_id']; score_maps[name][k]=values[k]; thresholds[name][k]=threshold
        artifacts.append(dict(fold=fold['index'],artifact=artifact,group_coefficients=group_weights(artifact) if artifact else None))
        auxiliary.append(auxiliary_models(train,validpop,labels,start,end,fold['index'],protocol))
        for t in trials: t['fold']=fold['index']
        trials_all.extend(trials); folds.append(fold)
        write(out/'folds'/f'{fold["index"]:02}.json',dict(fold=fold,artifact=artifact))
        print(f'fold {fold["index"]}: {start} {chosen["id"]}, train groups {fold["training_groups"]}',flush=True)
    ledgers={}; reports={}
    for name in score_maps:
        for s in snapshots:
            if outer_start and s['warning_ts']<outer_start:
                k=s['candidate_id'];score_maps[name][k]=baseline_score(s);thresholds[name][k]=70.
        for slip in (1,2):
            ledger=capacity(snapshots,maps[f'cash200_{slip}tick'],score_maps[name],thresholds[name])
            cohort=[r for r in ledger if outer_start and r['warning_ts']>=outer_start]
            key=f'{name}_{slip}tick';ledgers[key]=cohort
            reports[key]=metrics(r['label'] for r in cohort if r['decision']=='accepted')
            write_rows(out/(key+'.ledger.jsonl'),ledger)
    # Fixed >=70 remains a distinct baseline, not an equal selection-rate comparator.
    for policy in ('cash200','s1'):
        ledger=capacity(snapshots,maps[f'{policy}_1tick'],{s['candidate_id']:baseline_score(s) for s in population},
                        {s['candidate_id']:70. for s in population})
        reports[policy+'_v1_ge70']=metrics(r['label'] for r in ledger if r['decision']=='accepted')
        write_rows(out/(policy+'_v1_ge70.ledger.jsonl'),ledger)
    stats_config=protocol['statistics']; evaldays=sorted(set(d for d in days if outer_start and d>=outer_start[:10])|
        {r['label']['warning_trading_day'] for ledger in ledgers.values() for r in ledger})
    comparisons={}
    for name in ('adaptive','equal','space_cost'):
        comparisons[name]={str(block):paired_blocks(ledgers['v1_1tick'],ledgers[name+'_1tick'],evaldays,
                            stats_config['bootstrap_draws'],block,seed) for block in (5,10)}
    summary=dict(schema='factor-fit-summary-v1',folds=folds,metrics=reports,comparisons=comparisons,
                 preliminary_coverage_ok=len(folds)>=v['minimum_outer_folds'] and reports['adaptive_1tick']['closed']>=v['minimum_outer_closed'],
                 selected_nonbaseline_folds=sum(f['selection']['reason']=='selected' and f['chosen']['id']!='weights:original' for f in folds),
                 promotion='not_eligible_historical_development',confirmation=protocol['confirmation'],
                 assumptions=['Independent outcomes are only labels; capacity is rerun per policy.',
                              'Snapshot fees and revised contract mappings remain conditional.',
                              'Outer history was previously inspected; it is not unseen validation.'])
    write(out/'summary.json',summary);write_rows(out/'trials.jsonl',trials_all);write(out/'artifacts.json',artifacts)
    write(out/'auxiliary-models.json',auxiliary)
    write(out/'predictions.json',dict(scores=score_maps,thresholds=thresholds))
    # Refit one candidate for future observation, without claiming its final training performance as a test.
    trained=[s for s in population if labels[s['candidate_id']]['status']=='closed' and labels[s['candidate_id']]['exit_ts']<protocol['end']]
    final_config=next((f['chosen'] for f in reversed(folds) if f['selection']['reason']=='selected'),None)
    a,b=reports['adaptive_1tick'],reports['v1_1tick']
    interval=comparisons['adaptive']['5'].get('interval95')
    historical_support=bool(final_config and a['mean_net_yuan'] is not None and b['mean_net_yuan'] is not None
        and a['mean_net_yuan']-b['mean_net_yuan']>=protocol['confirmation']['minimum_mean_improvement_yuan']
        and a['net_yuan']>0 and reports['adaptive_2tick']['net_yuan']>0 and interval and interval[0]>0
        and a['max_drawdown_yuan']<=b['max_drawdown_yuan']+protocol['optimization']['allowed_drawdown_increase_yuan'])
    if final_config and historical_support:
        final=train_configuration(dict(final_config,cutoff={s['candidate_id'] for s in population}),trained,population,labels,protocol)
        write(out/'frozen-candidate.json',dict(config=final_config,threshold=final[1] if final else None,artifact=final[2] if final else None,
                                             status='development_candidate_only',requires_new_signal_confirmation=True))
    else:
        write(out/'frozen-candidate.json',dict(status='no_supported_candidate',last_inner_candidate=final_config,
            reason='No inner candidate, or outer development failed positive profit, improvement, stress or drawdown requirements.'))
    seal(out,'fit',[pathlib.Path(replayed)/'manifest.json',prepared/'manifest.json',scan/'manifest.json'])
    return out


def auxiliary_models(train,population,labels,start,end,fold,protocol):
    training=target_samples(train,labels)
    if groups_count(training)<protocol['validation']['minimum_training_groups']:
        return dict(fold=fold,status='insufficient_training_groups')
    names=[n for group in GROUPS.values() for n in group]
    out=dict(fold=fold,status='fit',selection_effect='none',models={},evaluation={})
    test=[s for s in population if start<=s['warning_ts']<end and labels[s['candidate_id']]['status']=='closed'
          and labels[s['candidate_id']]['exit_ts']<end]
    samples=target_samples(test,labels)
    for target,task in (('net_r','regression'),('hit200','binary')):
        model=fit_model(training,names,'linear',10.,target=target,task=task)
        out['models'][target]=model
        if not samples:continue
        predicted=np.asarray(predict(samples,model));actual=np.asarray([s[target] for s in samples],dtype=float)
        result=dict(n=len(samples),mean_prediction=float(predicted.mean()),mean_actual=float(actual.mean()))
        if task=='binary':
            prior=float(np.mean([s[target] for s in training]));result['brier']=float(np.mean((predicted-actual)**2))
            result['constant_brier']=float(np.mean((prior-actual)**2))
            positive=predicted[actual==1];negative=predicted[actual==0]
            result['auc']=float(np.mean(positive[:,None]>negative[None,:])+.5*np.mean(positive[:,None]==negative[None,:])) if len(positive) and len(negative) else None
        else:result['rmse']=float(np.sqrt(np.mean((predicted-actual)**2)))
        out['evaluation'][target]=result
    return out


def fdr_adjust(pvalues):
    adjusted=[None]*len(pvalues)
    valid=sorted((p,i) for i,p in enumerate(pvalues) if p is not None)
    previous=1.
    for rank in range(len(valid),0,-1):
        p,i=valid[rank-1];previous=min(previous,p*len(valid)/rank);adjusted[i]=previous
    return adjusted


def describe(samples,labels):
    selected=[labels[s['candidate_id']] for s in samples]
    result=metrics(selected)
    closed=[r for r in selected if r['status']=='closed']
    dates=sorted({r.get('warning_trading_day',r['warning_ts'][:10]) for r in closed})
    if len(dates)>=5:
        ix={d:i for i,d in enumerate(dates)}
        sums=np.zeros(len(dates));counts=np.zeros(len(dates));hits=np.zeros(len(dates))
        for r in closed:
            i=ix[r.get('warning_trading_day',r['warning_ts'][:10])];sums[i]+=r['net_yuan'];counts[i]+=1;hits[i]+=r['hit200']
        rng=np.random.default_rng(20261031)
        starts=rng.integers(0,len(dates)-4,size=(2000,math.ceil(len(dates)/5)))
        positions=(starts[:,:,None]+np.arange(5)).reshape(2000,-1)[:,:len(dates)]
        n=counts[positions].sum(axis=1)
        result['mean_net_yuan_interval95']=np.quantile(sums[positions].sum(axis=1)/n,[.025,.975]).tolist()
        result['hit200_interval95']=np.quantile(hits[positions].sum(axis=1)/n,[.025,.975]).tolist()
        result['interval_method']='5-day joint within-bin fixed-outcome blocks; descriptive'
    else:
        result['mean_net_yuan_interval95']=None
        result['hit200_interval95']=None
    return result


def diagnose_stage(replayed,output):
    prepared,scan,snapshots,maps,protocol,coverage=load_context(replayed)
    out=new_output(output);labels=maps['cash200_1tick'];population=eligible(snapshots,labels)
    days=coverage['trading_dates'];cutoff=next(outer_windows(days,protocol),(0,protocol['end'],protocol['end']))[1]
    training=[s for s in population if s['warning_ts']<cutoff]
    evaluated=[s for s in population if s['warning_ts']>=cutoff and labels[s['candidate_id']]['status']=='closed']
    f={s['candidate_id']:features(s,labels[s['candidate_id']]) for s in population}
    names=[n for group in GROUPS.values() for n in group]; curves=[]; pvalues=[]
    for name in names:
        reference=[f[s['candidate_id']].get(name) for s in training if f[s['candidate_id']].get(name) is not None]
        bounds=np.unique(np.quantile(reference,[.2,.4,.6,.8])).tolist() if reference else []
        bins=collections.defaultdict(list);pairs=[]
        for s in evaluated:
            value=f[s['candidate_id']].get(name)
            bins['missing' if value is None else str(bisect_right(bounds,value))].append(s)
            if value is not None: pairs.append((value,labels[s['candidate_id']]['net_yuan']))
        result=stats.spearmanr(*zip(*pairs)) if len(pairs)>=3 and len({x[0] for x in pairs})>1 else None
        p=float(result.pvalue) if result is not None and math.isfinite(result.pvalue) else None;pvalues.append(p)
        curves.append(dict(feature=name,training_bounds=bounds,raw_spearman=float(result.statistic) if p is not None else None,
                           descriptive_independence_p=p,bins={k:dict(metrics=describe(v,labels),groups=len({s['group_id'] for s in v}),
                           sufficient=len({s['group_id'] for s in v})>=protocol['statistics']['minimum_cell_groups']) for k,v in bins.items()}))
    for curve,q in zip(curves,fdr_adjust(pvalues)):curve['bh_q']=q
    surfaces=[]
    for a,b in INTERACTIONS:
        bounds={}
        for name in (a,b):
            v=[f[s['candidate_id']].get(name) for s in training if f[s['candidate_id']].get(name) is not None]
            bounds[name]=np.unique(np.quantile(v,[1/3,2/3])).tolist() if v else []
        bins=collections.defaultdict(list)
        for s in evaluated:
            x,y=f[s['candidate_id']].get(a),f[s['candidate_id']].get(b)
            key='missing' if x is None or y is None else f'{bisect_right(bounds[a],x)},{bisect_right(bounds[b],y)}'
            bins[key].append(s)
        surfaces.append(dict(features=[a,b],training_bounds=bounds,cells={k:dict(metrics=describe(v,labels),
                        groups=len({s['group_id'] for s in v}),sufficient=len({s['group_id'] for s in v})>=30) for k,v in bins.items()}))
    fixed=[]
    baseline=[.20,.36,.24,.20]
    for group in ('original','trend','a','b','space'):
        weights=list(baseline)
        if group!='original':
            weights[('trend','a','b','space').index(group)]=0.;total=sum(weights);weights=[x/total for x in weights]
        values={s['candidate_id']:float(np.dot(module_scores(s),weights)) for s in population}
        ref=[values[s['candidate_id']] for s in training]
        threshold=float(np.quantile(ref,.8)) if ref else math.inf
        result,ledger=policy_evaluation(snapshots,maps,values,threshold,cutoff,protocol['end'])
        fixed.append(dict(removed=group,weights=weights,threshold=threshold if math.isfinite(threshold) else None,metrics=result))
    # Groups are determined only by the first training period, never by test targets.
    trainmatrix=[]; validnames=[]
    for name in names:
        values=[f[s['candidate_id']].get(name) for s in training]
        finite=[v for v in values if v is not None]
        if len(finite)>2 and np.std(finite)>0:
            validnames.append(name); med=float(np.median(finite));trainmatrix.append([med if v is None else v for v in values])
    correlated=[]
    if len(validnames)>1:
        matrix=np.column_stack(trainmatrix)
        ranks=np.column_stack([stats.rankdata(matrix[:,i]) for i in range(matrix.shape[1])])
        correlation=np.nan_to_num(np.corrcoef(ranks,rowvar=False));distance=1-np.abs(correlation)
        np.fill_diagonal(distance,0.)
        cluster=fcluster(linkage(squareform(np.clip(distance,0,1),checks=False),method='complete'),.2,criterion='distance')
        groups=collections.defaultdict(list)
        for name,k in zip(validnames,cluster):groups[int(k)].append(name)
        correlated=[v for v in groups.values() if len(v)>1]
    write(out/'feature-curves.json',curves);write(out/'interaction-surfaces.json',surfaces)
    write(out/'fixed-ablations.json',fixed);write(out/'correlated-groups.json',correlated)
    write(out/'feature-dictionary.json',dict(groups=GROUPS,interactions=INTERACTIONS,cutoff=cutoff,
          **feature_dictionary(read(scan/'manifest.json')['baseline_rules']),
          baseline_rules=read(scan/'manifest.json')['baseline_rules'],
          pvalue_caveat='Raw Spearman p-values assume independent observations; BH is descriptive and cannot establish trading value.'))
    refitted=refit_ablations(snapshots,maps,days,protocol,correlated)
    write(out/'refitted-ablations.json',refitted)
    write(out/'parameter-evidence.json',parameter_evidence(refitted,names,protocol,days))
    weight_sensitivity=weight_sweep(snapshots,maps,days,protocol)
    write(out/'weight-sensitivity.json',weight_sensitivity)
    seal(out,'diagnose',[pathlib.Path(replayed)/'manifest.json',prepared/'manifest.json',scan/'manifest.json'])
    return out


def bisect_right(bounds,value):
    import bisect
    return bisect.bisect_right(bounds,value)


def refit_ablations(snapshots,maps,days,protocol,correlated):
    labels=maps['cash200_1tick'];pop=eligible(snapshots,labels);names=[x for v in GROUPS.values() for x in v]
    removals=[('none',[]),('interaction:all',[])]+[('module:'+g,n) for g,n in GROUPS.items()]+[('feature:'+n,[n]) for n in names]
    removals += [('correlated:'+str(i),g) for i,g in enumerate(correlated)]
    removals += [('interaction:'+a+'*'+b,[]) for a,b in INTERACTIONS]
    output=[];v=protocol['validation']
    for index,start,end in outer_windows(days,protocol):
        train=observed_training(snapshots,labels,start,end)
        if len({s['group_id'] for s in train})<v['minimum_training_groups']:
            output.append(dict(start=start,status='insufficient_training_groups'));continue
        population=[s for s in pop if s['warning_ts']<end]
        for name,removed in removals:
            interactions=[p for p in INTERACTIONS if not any(n in removed for n in p)
                          and name!='interaction:'+p[0]+'*'+p[1]]
            # Fixed regularization is deliberate: refit evidence, not a new winner search.
            family='interaction' if name.startswith('interaction:') else 'linear'
            config=dict(kind='model',family=family,alpha=10.,names=[n for n in names if n not in removed],
                        interactions=interactions,cutoff={s['candidate_id'] for s in population if s['warning_ts']<start})
            fitted=train_configuration(config,train,population,labels,protocol)
            if fitted is None:
                output.append(dict(start=start,removed=name,status='insufficient_complex_model_groups'));continue
            scores,cut,artifact=fitted;result,ledger=policy_evaluation(snapshots,maps,scores,cut,start,end,
                                                                    outcome_end=protocol['end'])
            daily=collections.defaultdict(float)
            for row in ledger['1']:
                if row['decision']=='accepted' and row['label']['status']=='closed':
                    daily[row['label']['warning_trading_day']]+=row['label']['net_yuan']
            output.append(dict(start=start,end=end,removed=name,family=family,metrics=result,
                               daily_yuan=dict(daily),training_groups=artifact['training_groups'],
                               training_last_exit=artifact['training_last_exit'],observed_through=protocol['end'],
                               fold_end_statuses=dict(collections.Counter(r['fold_end_status'] for r in ledger['1'] if r['decision']=='accepted')),
                               matured_after_fold=sum(r['fold_end_status']!=r['label']['status'] for r in ledger['1'] if r['decision']=='accepted')))
    return output


def parameter_evidence(refitted,names,protocol,all_days=()):
    baseline={r['start']:r for r in refitted if r.get('removed')=='none' and 'metrics' in r}
    output=[]
    for name in names:
        comparisons=[]
        for row in refitted:
            if row.get('removed')!='feature:'+name or row['start'] not in baseline or 'metrics' not in row:continue
            base=baseline[row['start']]
            comparisons.append(dict(start=row['start'],delta_keep_yuan=base['metrics']['1']['net_yuan']-row['metrics']['1']['net_yuan'],
                                    baseline_closed=base['metrics']['1']['closed'],removed_closed=row['metrics']['1']['closed'],
                                    known_fraction=min(base['metrics']['1']['known_fraction'],row['metrics']['1']['known_fraction']),
                                    unknown=base['metrics']['1']['unknown']+row['metrics']['1']['unknown']))
        positive=sum(r['delta_keep_yuan']>0 for r in comparisons)
        base_daily=collections.defaultdict(float);removed_daily=collections.defaultdict(float)
        for row in refitted:
            if row.get('removed')=='none':target=base_daily
            elif row.get('removed')=='feature:'+name:target=removed_daily
            else:continue
            if row.get('start') not in {c['start'] for c in comparisons}:continue
            for date,value in row.get('daily_yuan',{}).items():target[date]+=value
        first=min((r['start'][:10] for r in comparisons),default='9999')
        last=max((r['end'][:10] for r in refitted if r.get('removed')=='none' and 'end' in r),default='0000')
        dates=sorted(set(base_daily)|set(removed_daily)|{d for d in all_days if first<=d<last})
        def ledger(values):
            return [dict(decision='accepted',label=dict(status='closed',warning_ts=d+' 00:00:00',net_yuan=values.get(d,0.))) for d in dates]
        interval=paired_blocks(ledger(removed_daily),ledger(base_daily),dates,
                               protocol['statistics']['bootstrap_draws'],5,protocol['optimization']['seed'])
        bounds=interval.get('interval95');status='insufficient_evidence'
        coverage_ok=bool(comparisons) and all(c['known_fraction']>=protocol['validation']['minimum_known_fraction'] for c in comparisons)
        if len(comparisons)>=3 and bounds and coverage_ok:
            if bounds[0]>0 and positive/len(comparisons)>=.7:status='provisional_incremental_value_on_priced_subset'
            elif bounds[1]<0 and positive/len(comparisons)<=.3:status='provisional_removal_candidate_on_priced_subset'
        output.append(dict(feature=name,status=status,paired_date_interval=interval,
                           folds=comparisons,positive_folds=positive,total_folds=len(comparisons),
                           decision='retain_current_until_new_data_confirmation',
                           priced_subset_only=True,coverage_ok=coverage_ok,
                           reason='Deletion changes selection and occupancy; a coefficient or one good fold does not establish value.'))
    adjusted=fdr_adjust([r['paired_date_interval'].get('centered_bootstrap_p') for r in output])
    for row,q in zip(output,adjusted):
        row['bh_q']=q
        row['priority']=('forward_check_candidate' if row['status'].startswith('provisional_') and q is not None
                         and q<=protocol['statistics']['fdr'] else 'exploratory_only')
    return output


def weight_sweep(snapshots,maps,days,protocol):
    """Predeclared +/-5pp neighborhood: sensitivity only, never selects a new candidate."""
    labels=maps['cash200_1tick'];population=eligible(snapshots,labels)
    first=next(outer_windows(days,protocol),None)
    if first is None:return dict(status='insufficient_dates')
    cutoff=first[1];base=[.20,.36,.24,.20];outputs=[]
    for i in range(4):
        for direction in (-1,0,1):
            w=list(base);w[i]=max(0,w[i]+direction*.05);total=sum(w);w=[x/total for x in w]
            values={s['candidate_id']:float(np.dot(module_scores(s),w)) for s in population}
            ref=[values[s['candidate_id']] for s in population if s['warning_ts']<cutoff]
            threshold=float(np.quantile(ref,.8)) if ref else math.inf
            result,_=policy_evaluation(snapshots,maps,values,threshold,cutoff,protocol['end'])
            outputs.append(dict(varied_module=i,delta=direction*.05,weights=w,metrics=result))
    return dict(status='development_sensitivity_only',origin=base,results=outputs)
