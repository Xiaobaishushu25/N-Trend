"""Warning-time structure evidence scoped to ONE selected parent A/B identity."""
import bisect,datetime as dt,math

def features(event,raw5,bars15,index,raw_times=None):
    e=event;up=e['direction']=='up';sign=1 if up else -1
    times=raw_times if raw_times is not None else [b['datetime'] for b in raw5]
    # Exclude all three components of the current15m warning candle. The anchor
    # must have a completed5m right confirmation before the warning candle opens.
    cutoff=(dt.datetime.fromisoformat(e['warning_ts'])-dt.timedelta(minutes=15)).isoformat(sep=' ')
    lo=bisect.bisect_right(times,e['s1_ts']);hi=bisect.bisect_right(times,cutoff);history=raw5[lo:hi]
    highs=[];lows=[]
    for a,b,c in zip(history,history[1:],history[2:]):
        if (dt.datetime.fromisoformat(b['datetime'])-dt.datetime.fromisoformat(a['datetime'])).total_seconds()!=300 or (dt.datetime.fromisoformat(c['datetime'])-dt.datetime.fromisoformat(b['datetime'])).total_seconds()!=300:continue
        if b['high']>a['high'] and b['high']>c['high']:highs.append((b,c))
        if b['low']<a['low'] and b['low']<c['low']:lows.append((b,c))
    pivots=highs if up else lows;opposite=lows if up else highs
    j=index[e['s1_ts']];at=index[e['warning_ts']];prior=bars15[j+1:at];warning=bars15[at]
    assert prior and at-j==e['b_bars'],'Selected B identity/length disagrees with source aggregation'
    prior_extreme=min(b['low'] for b in prior) if up else max(b['high'] for b in prior)
    fresh=(warning['low']<=prior_extreme+1e-9) if up else (warning['high']>=prior_extreme-1e-9)
    out=dict(parent_leg_id='|'.join(e[k] for k in ('symbol','direction','s0_ts','s1_ts')),evidence_as_of=e['warning_ts'],anchor_cutoff=cutoff,
        parent_s0_ts=e['s0_ts'],parent_s1_ts=e['s1_ts'],parent_a_bars=e['a_bars'],parent_b_bars=e['b_bars'],fresh_b_extreme=fresh,prior_b_extreme=prior_extreme,
        internal_pivot_count=len(pivots),opposite_pivot_count=len(opposite),required=False,level=e['entry'],state='no_confirmed_internal_pivot',pivot_ts=None,confirmation_ts=None)
    if not pivots:return out
    anchor,confirmation=pivots[-1];price=anchor['high' if up else 'low'];tick=e['tick_size'];raw=price+sign*tick
    level=(math.ceil(raw/tick-1e-9) if up else math.floor(raw/tick+1e-9))*tick
    required=(level-e['entry'])*sign>1e-9
    previous=(pivots[-2][0]['high' if up else 'low']) if len(pivots)>1 else None
    before=[p[0] for p in opposite if p[0]['datetime']<anchor['datetime']]
    out.update(required=required,level=level if required else e['entry'],state='awaiting_internal_break' if required else 'warning_already_clears_internal_pivot',
        pivot_ts=anchor['datetime'],confirmation_ts=confirmation['datetime'],pivot_price=price,previous_pivot_price=previous,
        pivot_chain='unavailable' if previous is None else 'toward_A' if (price-previous)*sign>0 else 'against_A' if (price-previous)*sign<0 else 'equal',
        preceding_opposite_pivot_ts=before[-1]['datetime'] if before else None)
    assert e['s1_ts']<anchor['datetime']<confirmation['datetime']<=cutoff<e['warning_ts']
    return out

def active(profile,f):
    return f['required'] and (profile in ('all_touch','all_close') or profile=='fresh_close' and f['fresh_b_extreme'] or profile=='inside_close' and not f['fresh_b_extreme'])
