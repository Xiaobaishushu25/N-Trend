"""Serializable ridge/spline research models. Preprocessing is fitted on training only."""
import collections
import math
import numpy as np
from scipy.optimize import minimize
from scipy.special import expit
from scipy.stats import qmc

GROUPS = {
    'trend':['ma20_slope_pre','ma20_slope_a','ma20_slope_warning','warning_close_ma_atr',
             'ma_residence','a_ma_crosses','b_ma_crosses','trend60_alignment'],
    'a':['a_move_atr','a_bars','a_speed_atr','a_core_efficiency','a_full_efficiency',
         'a_drawdown_ratio','a_stall_fraction','a_overlap_stagnation','a_gap_penalty','a_largest_body_share'],
    'b':['retracement','b_move_atr','b_a_time_ratio','b_a_speed_ratio','b_decay',
         'b_reverse_body_bars','b_reverse_close_steps','b_mean_overlap','b_signed_efficiency',
         'ma_departure_atr','ma_proximity_atr','ma_damage_atr'],
    'warning':['warning_improvement_atr','warning_close_location','warning_body_atr','warning_opposing_wick'],
    'space_cost':['planned_risk_yuan','risk_ticks','cost_r','rr_to_s1','nearest_obstacle_r'],
}
INTERACTIONS = [('a_move_atr','retracement'),('a_core_efficiency','retracement'),
                ('a_bars','b_a_time_ratio'),('trend60_alignment','retracement'),
                ('trend60_alignment','a_core_efficiency')]

DEFINITIONS = {
    'ma20_slope_pre':'Mean directional MA20 change over 5/10 bars at S0-1, divided by ATR20 there.',
    'ma20_slope_a':'Median of the same directional slope over S0 through S1.',
    'ma20_slope_warning':'Directional MA20 slope at the completed warning.',
    'warning_close_ma_atr':'Direction * (warning close - MA20) / ATR20(warning).',
    'ma_residence':'Fraction of A closes on the favorable MA side with frozen noise tolerance.',
    'a_ma_crosses':'Hysteresis MA crossings during A; noise threshold frozen in Rust Rules.',
    'b_ma_crosses':'Hysteresis MA crossings during B through warning.',
    'trend60_alignment':'Signal direction times frozen warning-time 60m trend sign; range=0.',
    'a_move_atr':'A amplitude less frozen large-gap adjustment, divided by ATR20(S1).',
    'a_bars':'Frozen detector A duration in completed 15m bars.',
    'a_speed_atr':'Gap-adjusted A amplitude / A bars / ATR20(S1).',
    'a_core_efficiency':'Rust path efficiency inside A endpoints, or flagged full-path fallback.',
    'a_full_efficiency':'Same Rust path efficiency including A endpoints.',
    'a_drawdown_ratio':'Rust core A path drawdown relative to A amplitude.',
    'a_stall_fraction':'Rust core A stall fraction with frozen tick/ATR progress definition.',
    'a_overlap_stagnation':'Rust core A overlap/stagnation measure, not raw wick overlap.',
    'a_gap_penalty':'Frozen Rust diagnostic for A gaps; gap policy is not retuned here.',
    'a_largest_body_share':'Largest directional body contribution share over full A.',
    'retracement':'Detector B amplitude divided by detector A amplitude.',
    'b_move_atr':'Detector B amplitude / ATR20(S1).',
    'b_a_time_ratio':'Detector B bars / A bars.',
    'b_a_speed_ratio':'(B amplitude / B bars) / (A amplitude / A bars).',
    'b_decay':'One minus second/first-half reverse body mean, excluding warning; null when unavailable.',
    'b_reverse_body_bars':'Reverse-body count in B excluding the warning.',
    'b_reverse_close_steps':'Reverse close-step count in B excluding the warning.',
    'b_mean_overlap':'B range overlap excluding warning, from Rust structural evidence.',
    'b_signed_efficiency':'Net retreat from S1 close divided by close travel before warning.',
    'ma_departure_atr':'Maximum directional close/MA distance during A.',
    'ma_proximity_atr':'Minimum distance from MA20 to B candle range through warning, / ATR20.',
    'ma_damage_atr':'Mean adverse close/MA distance during B through warning, / ATR20.',
    'warning_improvement_atr':'Directional warning close change / ATR20(warning).',
    'warning_close_location':'Directional close position inside warning high-low range.',
    'warning_body_atr':'Directional warning body / ATR20(S1), from normalized Rust evidence.',
    'warning_opposing_wick':'Warning opposing wick / warning high-low range.',
    'planned_risk_yuan':'One-lot forecast entry to initial tick-rounded stop, before fees.',
    'risk_ticks':'Forecast entry to initial stop / minimum tick.',
    'cost_r':'Forecast stop loss including costs minus price risk, divided by price risk.',
    'rr_to_s1':'Detector directional entry-to-S1 space / detector initial risk.',
    'nearest_obstacle_r':'Known forward obstacle / initial risk; missing is unknown, not no obstacle.',
}


def feature_dictionary(rules):
    fields={name:dict(group=group,definition=DEFINITIONS[name],available_at='warning_close',
                     missing='training-only median plus missing indicator; never zero-fill labels')
            for group,names in GROUPS.items() for name in names}
    registry={}
    definitions={'warmup_min','warmup_max','core_min','slope_lags','ma_noise_atr','progress_atr',
                 'overlap_displacement_atr','decay_min_bars'}
    for key,value in rules.items():
        kind=('version' if key=='version' else 'feature_definition_frozen' if key in definitions else
              'module_weight_search' if key in {'morphology_weights','quality_weights'} else
              'within_module_weight_reestimated_via_joint_raw_model' if key.endswith('_weights') else
              'mapping_compared_with_training_fitted_splines')
        registry[key]=dict(original=value,study=kind)
    return dict(fields=fields,current_parameter_registry=registry,
                scope_note='Raw feature construction and recognition gates are frozen; a redesigned detector requires a separate candidate population.')


def get(value, *keys):
    for k in keys:
        if not isinstance(value,dict): return None
        value=value.get(k)
    return value


def features(snapshot,label):
    if '_factor_features' in snapshot:return snapshot['_factor_features']
    f=dict(snapshot['features']); e=snapshot['evidence']; plan=label['plan']
    f.update({k:plan[k] for k in ('planned_risk_yuan','risk_ticks','cost_r')})
    paths={
        'a_largest_body_share':('a_process','directional_body','largest_share'),
        'b_reverse_body_bars':('b_process','reverse_body_bars'),
        'b_reverse_close_steps':('b_process','reverse_close_steps'),
        'b_mean_overlap':('b_process','mean_overlap'),
        'b_signed_efficiency':('b_process','signed_efficiency_from_s1'),
        'nearest_obstacle_r':('levels','nearest_forward_obstacle_r'),
        'warning_body_atr':('warning','warning','signed_body_atr'),
        'warning_opposing_wick':('warning','warning','opposing_wick_fraction')}
    for name,path in paths.items():
        f[name]=get(e,'modules',path[0],'metrics',*path[1:])
    # Match detector's actual frozen 60m state; it is not inferred from labels.
    import json
    state=json.loads(snapshot['event']['entry_score_dims']).get('trend_state','').upper()
    trend=1 if 'UP' in state else -1 if 'DOWN' in state else 0
    f['trend60_alignment']=trend*(1 if snapshot['event']['direction']=='up' else -1)
    for k,v in list(f.items()):
        if v is not None and (not isinstance(v,(float,int)) or not math.isfinite(v)):
            raise ValueError('invalid numeric feature '+k)
    return f


def module_scores(s):
    q=s['quality']
    return [q.get(k) for k in ('trend_score','a_score','b_score','space_score')]


def baseline_score(s):
    return s['quality'].get('quality_score')


def weight_candidates(count,seed):
    values=[('original',[.20,.36,.24,.20]),('equal',[.25]*4)]
    values += [('only_'+name,[float(i==j) for i in range(4)]) for j,name in enumerate(('trend','a','b','space'))]
    points=qmc.Sobol(d=4,scramble=True,seed=seed).random_base2(int(math.ceil(math.log2(max(count,1)))))[:count]
    for i,p in enumerate(points):
        x=-np.log(np.maximum(p,1e-12)); values.append((f'sobol_{i:03}',(x/x.sum()).tolist()))
    return values


def fit_model(samples, names, family, alpha, interactions=None, target='net_yuan', task='regression',knot_features=None):
    """samples contain only explicit features and completed labels; groups sum to one."""
    transforms=[]
    for name in names:
        finite=[r['features'].get(name) for r in samples if r['features'].get(name) is not None]
        if not finite: continue
        median=float(np.median(finite)); scale=float(np.std(finite)) or 1.
        knots=(np.quantile(finite,[.1,.5,.9]).tolist() if family!='linear'
               and (knot_features is None or name in knot_features) else [])
        transforms.append(dict(name=name,median=median,scale=scale,knots=sorted(set(knots)),
                               missing_indicator=any(r['features'].get(name) is None for r in samples)))
    artifact=dict(schema='FitArtifactV1',family=family,alpha=alpha,transforms=transforms,
                  target=target,task=task,
                  interactions=interactions if family=='interaction' else [],
                  products=sorted({r['symbol'] for r in samples}),coefficients=[],columns=[])
    x,columns=matrix(samples,artifact)
    artifact['columns']=columns
    groups=collections.Counter(r['group_id'] for r in samples)
    weights=np.asarray([1/groups[r['group_id']] for r in samples])
    y=np.asarray([r[target] for r in samples],dtype=float)
    penalty=np.eye(x.shape[1])*alpha; penalty[0,0]=0.
    a=x.T@(x*weights[:,None])+penalty
    b=x.T@(weights*y)
    if task=='binary':
        def objective(beta):
            z=x@beta
            value=np.sum(weights*(np.logaddexp(0,z)-y*z))+.5*beta@penalty@beta
            gradient=x.T@(weights*(expit(z)-y))+penalty@beta
            return float(value),gradient
        solved=minimize(objective,np.zeros(x.shape[1]),jac=True,method='L-BFGS-B',options={'maxiter':1000})
        if not solved.success:raise ValueError('logistic fit did not converge: '+solved.message)
        coefficients=solved.x
    else:
        coefficients=np.linalg.lstsq(a,b,rcond=None)[0]
    artifact['coefficients']=coefficients.tolist()
    artifact.update(training_rows=len(samples),training_groups=len(groups),design_columns=len(columns),
                    training_last_warning=max((r['warning_ts'] for r in samples),default=None),
                    training_last_exit=max((r['exit_ts'] for r in samples),default=None))
    return artifact


def matrix(samples,artifact):
    columns=['intercept']; values=[np.ones(len(samples))]; standardized={}
    for tr in artifact['transforms']:
        name=tr['name']
        raw=[r['features'].get(name) for r in samples]
        v=np.asarray([tr['median'] if v is None else v for v in raw])
        z=(v-tr['median'])/tr['scale']; standardized[name]=z
        values.append(z); columns.append(name)
        for i,knot in enumerate(tr['knots']):
            values.append(np.maximum(0,(v-knot)/tr['scale'])); columns.append(f'{name}:hinge{i}')
        if tr['missing_indicator']:
            values.append(np.asarray([float(v is None) for v in raw])); columns.append(name+':missing')
    for a,b in artifact['interactions']:
        if a in standardized and b in standardized:
            values.append(standardized[a]*standardized[b]); columns.append(a+'*'+b)
    for symbol in artifact['products'][1:]:
        values.append(np.asarray([float(r['symbol']==symbol) for r in samples])); columns.append('control_product:'+symbol)
    values.append(np.asarray([1. if r['direction']=='up' else -1. for r in samples])); columns.append('control_direction')
    return np.column_stack(values),columns


def predict(samples,artifact):
    x,columns=matrix(samples,artifact)
    if columns!=artifact['columns']: raise ValueError('design matrix drift')
    values=x@np.asarray(artifact['coefficients'])
    return (expit(values) if artifact.get('task')=='binary' else values).tolist()


def target_samples(snapshots,labels):
    return [dict(candidate_id=s['candidate_id'],group_id=s['group_id'],symbol=s['symbol'],
                 direction=s['event']['direction'],warning_ts=s['warning_ts'],exit_ts=labels[s['candidate_id']]['exit_ts'],
                 features=features(s,labels[s['candidate_id']]),net_yuan=labels[s['candidate_id']]['net_yuan'],
                 net_r=labels[s['candidate_id']].get('net_r'),hit200=labels[s['candidate_id']].get('hit200'))
            for s in snapshots]


def groups_count(samples):
    return len({s['group_id'] for s in samples})


def group_weights(artifact):
    """Coefficient magnitude is explanatory; never presented as formula weight."""
    out={g:0. for g in GROUPS}
    for column,beta in zip(artifact['columns'],artifact['coefficients']):
        if '*' in column: continue
        raw=column.split(':')[0]
        for group,names in GROUPS.items():
            if raw in names: out[group]+=abs(beta)
    return dict(kind='standardized_coefficient_magnitude_not_formula_weight',values=out)
