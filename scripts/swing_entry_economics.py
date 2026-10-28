"""Causal warning/plan-only entry descriptors; never consume trade outcomes."""
import datetime as dt

MODELS=['base','budget10','body_net','no_gap_majority','no_bar_majority','orderly_B','clean_A','budget_orderly','budget_clean']

def warning_features(e,bars,index=None):
    # Use official bar counts as well as labels. Partial identification candles
    # can have identical end labels; a timestamp-only dictionary can lose identity.
    ends=[i for i,b in enumerate(bars) if b['datetime']==e['warning_ts']]
    possible=[]
    for at in ends:
        j=at-e['b_bars'];i=j-e['a_bars']+1
        if i<0 or j<0:continue
        if bars[i]['datetime']!=e['s0_ts'] or bars[j]['datetime']!=e['s1_ts']:continue
        price=bars[at]['low' if e['direction']=='up' else 'high']
        if abs(price-e['s2_price'])>1e-7:continue
        if abs(bars[i]['low' if e['direction']=='up' else 'high']-e['s0_price'])>1e-7:continue
        if abs(bars[j]['high' if e['direction']=='up' else 'low']-e['s1_price'])>1e-7:continue
        possible.append((i,j,at))
    assert possible,('No identity-consistent anchors',e['symbol'],e['warning_ts'])
    i,j,at=possible[0];a=bars[i:j+1];b=bars[j+1:at+1];sign=1 if e['direction']=='up' else -1;amove=e['a_move'];assert amove>0 and abs((e['s1_price']-e['s0_price'])*sign-amove)<1e-7
    assert len(a)==e['a_bars'] and len(b)==e['b_bars'] and all(x['datetime']<=e['warning_ts'] for x in a+b)
    body_net=sign*sum(x['close']-x['open'] for x in a[1:]);body_abs=sum(abs(x['close']-x['open']) for x in a[1:])
    gaps=[]
    for left,right in zip(a,a[1:]):
        if (dt.datetime.fromisoformat(right['datetime'])-dt.datetime.fromisoformat(left['datetime'])).total_seconds()>900:gaps.append(max(0.,sign*(right['open']-left['close'])))
    bextreme=min(x['low'] for x in b) if sign>0 else max(x['high'] for x in b)
    true_b_move=(e['s1_price']-bextreme)*sign;assert true_b_move>=-1e-8
    return dict(as_of=e['warning_ts'],parent_leg_id='|'.join(e[k] for k in ('symbol','direction','s0_ts','s1_ts')),anchor_matches=len(possible),
        a_body_net_ex_s0=body_net,a_body_eff_ex_s0=body_net/body_abs if body_abs else None,
        a_positive_session_gap_share=sum(gaps)/amove,a_largest_abs_body_share=max(abs(x['close']-x['open']) for x in a)/amove,
        b_true_extreme=bextreme,b_true_speed_to_A=(true_b_move/len(b))/(amove/len(a)),a_bars=len(a),b_bars=len(b))

def plan_features(row):
    # Explicit whitelist. In particular, no actual fill/risk, realized fee,
    # trigger time, exit, MFE, return, status or future exposure is read.
    sign=1 if row['direction']=='up' else -1;space=(row['target']-row['entry'])*sign;cost=row['new_friction']*row['planned_risk']
    return dict(planned_entry=row['entry'],planned_stop=row['stop'],planned_target=row['target'],planned_gross_space=space,planned_roundtrip_cost_points=cost,
        cost_to_gross_space=cost/space if space>0 else None)

def allows(name,f,p):
    economic=p['cost_to_gross_space'] is not None and p['cost_to_gross_space']<=.10+1e-12
    body=f['a_body_net_ex_s0']>0
    gap=f['a_positive_session_gap_share']<=.5+1e-12
    bar=f['a_largest_abs_body_share']<=.5+1e-12
    orderly=f['b_true_speed_to_A']<=1.+1e-12
    clean=body and gap and bar
    return dict(base=True,budget10=economic,body_net=body,no_gap_majority=gap,no_bar_majority=bar,orderly_B=orderly,clean_A=clean,budget_orderly=economic and orderly,budget_clean=economic and clean)[name]

def eligible_rows(rows,decisions,model):
    return [row for row in rows if decisions[row['event_key']][model]]
