"""R1 descriptive diagnostics only. Hard bounded to exposed development history.

freeze -> run -> verify; no strategy selection and no reserved-period outcome API.
"""
import argparse
import datetime as dt
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.s1-deps'))
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import explore_v3_signals as v3
import explore_v4_logic as v4
import s3_signals as prep
from factor_study_data import Calendar, specifications, digest
from s1_market_regimes import select_main, fee_rule

BASE = ROOT / 'artifacts/r1'
SOURCE = ROOT / 'data/research_market_history.sqlite'
CUTOFF = '2025-11-01'
EXPECTED = '9319a94c9c9a125ce70cd260466eea84e0f1747fb2716c35e518fb7772b577c8'
PERIODS = [('D1', '2023-11-01', '2024-11-01'), ('D2', '2024-11-01', CUTOFF)]
RAW = ['contract','start_ns','end_ns','open','high','low','close','volume','constituent_count','complete']


def stamp(day):
    return int(pd.Timestamp(day, tz='Asia/Shanghai').value)


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf8')


def latest(version):
    b = ROOT / ('artifacts/s3' if version == 'V1' else 'artifacts/exploration_' + version.lower())
    p = ROOT / json.loads((b / 'latest_run.json').read_text(encoding='utf8'))['path']
    return p


def protocol():
    return dict(scope='R1-0/R1-1 only; stop after T1/T2/T3', periods=PERIODS,
        prohibited_outcomes_from=CUTOFF, new_strategy_templates=0, prior_templates=80, prior_evaluations=256,
        earlier_feedback='unknown', horizons=[6,12,24,36], slip_ticks_each_side=[1,2],
        weighting=['complete event arithmetic mean', 'within date mean then equal dates'],
        no_matched_subset_inference=True,
        funnel='fixed raw12-bar rejection morphology; no cooldown at root; same event IDs in five rows per 400/600 branch',
        funnel_steps=['base','tradability_cost_activity','lot_budget','original_quiet_and_ER','original_confirmation_and_cooldown'],
        funnel_confirmation='Q-C01 immediate setup-close confirmation is in base morphology; final step restores original 24 active-bar cooldown, calculated from original Q-C01 alerts before budget rejection',
        funnel_outcome='same fixed H12 direction label for all retained/removed cohorts; supplemental frozen V4 structural trade at budget 400/600; never choose best branch',
        candidate_state='exact contiguous wall-clock last30m close-open / sqrt(sum(last6 completed5m true_range squared)); same length, no threshold fitted; >=2 other exact timestamp peers',
        higher_ATR='12 completed exact clock buckets, no crossing segments, asof bucket end; missing remains null',
        gross_definition='unadjusted next actual open to Hth close; subtract both adverse slips and fees at scenario fill prices',
        censor='H active bars, same day/segment, all valid positive-volume, strict period end; no partial returns filled',
        execution='OHLC execution_proxy; unknown spread/margin/history fees; no shared account certification',
        route='evidence recommendation only; no R1-2 execution')


def freeze():
    BASE.mkdir(exist_ok=False)
    files = set()
    for b in ['s0','s1','s2','s3','exploration_v2','exploration_v3','exploration_v4']:
        for p in (ROOT / 'artifacts' / b).rglob('*.json'):
            if 'freeze' in p.name or p.name in ('manifest.json','artifact_hashes.json'):
                files.add(p)
    files.update((ROOT/'scripts').glob('*.py'))
    files.update((ROOT/'tests').glob('test_*explore*.py'))
    files.add(ROOT/'tests/test_r1_diagnostics.py')
    files.update([ROOT/'scripts/exchange_fees_20261022.json', ROOT/'scripts/factor-study-v1.json',
        ROOT/'data/trading_calendar.json', ROOT/'crates/n-core/src/precision.rs',
        ROOT/'R1 止损诊断与研究重启方案.md', ROOT/'中国内盘期货策略探索阶段总结与问题清单_2026-11-05.md'])
    snapshots = BASE/'snapshot'
    snapshots.mkdir()
    hashes = {}
    for p in sorted(files):
        rel = p.relative_to(ROOT); dest = snapshots/rel; dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest); hashes[str(rel)] = digest(p)
    logs = []
    for script in ['s2_preregister.py','run_s3_alpha.py','explore_v2.py','explore_v3.py','explore_v4.py']:
        r = subprocess.run([sys.executable, str(ROOT/'scripts'/script), 'verify'], capture_output=True, text=True)
        logs.append(dict(script=script, returncode=r.returncode, stdout=r.stdout, stderr=r.stderr))
    write_json(BASE/'legacy_verify.json', logs)
    if any(r['returncode'] for r in logs):
        raise ValueError('legacy verification failed; preserved logs, stop')
    sha = digest(SOURCE)
    if sha != EXPECTED:
        write_json(BASE/'source_mismatch.json', dict(expected=EXPECTED, actual=sha))
        raise ValueError('source hash differs; stop without rewriting history')
    write_json(BASE/'protocol.json', protocol())
    events = []
    for version in ['V1','V2','V3','V4']:
        run = latest(version)
        exposures = pd.read_csv(run/'evaluation_exposures.csv')
        manifest = ROOT/'artifacts/s2/candidate_manifest.json' if version=='V1' else run.parent/'candidate_manifest.json'
        candidates=json.loads(manifest.read_text(encoding='utf8'))['candidates']
        for row in exposures.to_dict('records'):
            for c in candidates:
                events.append(dict(version=version,candidate_id=c['candidate_id'], exposure_type='historical_strategy_evaluation',
                    result_seen=True, classification='EXPOSED_DEVELOPMENT', source=str((run/'evaluation_exposures.csv').relative_to(ROOT)),
                    reason='prior V1-V4 research feedback; R1 retains historical exposure', **row))
    assert len(events)==256
    registry = ROOT/'registry'; registry.mkdir(exist_ok=True)
    registry_path = registry/'evaluation_exposures.csv'
    if registry_path.exists():
        raise ValueError('registry already exists; merge explicitly rather than overwrite')
    pd.DataFrame(events).to_csv(registry_path,index=False)
    write_json(BASE/'R1_freeze_manifest.json', dict(created_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
        git_sha=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        git_status=subprocess.check_output(['git','status','--short'],text=True).splitlines(),
        git_commit_created=False, snapshot_method='byte copies + SHA256; excludes unrelated working tree from git staging',
        source_sha256=sha, expected_source_matches=True, snapshot_sha256=hashes,
        protocol_sha256=digest(BASE/'protocol.json'), registry_sha256=digest(registry_path),
        historical_evaluation_rows=len(events), period_tags=dict(D1='EXPOSED_DEVELOPMENT',D2='EXPOSED_DEVELOPMENT',
            after_20251101='RESERVED_DIAGNOSTIC_HOLDOUT')))
    (BASE/'R1_exposure_audit.md').write_text(
        '# R1 历史暴露与冻结\n\nD1/D2 为 EXPOSED_DEVELOPMENT；2025-11-01 起为 RESERVED_DIAGNOSTIC_HOLDOUT。\n'
        f'已导入 {len(events)} 条配置×窗口登记，80 模板；更早反馈未知。未重跑旧策略。\n'
        '当前工作树包含原有未提交研究代码，已逐字节复制代码和全部实验冻结清单并校验 SHA256。'
        '没有把这些未提交修改自动纳入 git 提交，因此本快照是可校验文件归档，不冒充 git commit。\n'
        'SQLite 以 mode=ro/query_only 读取，SHA256 与总结一致。只执行旧入口 verify，日志见 legacy_verify.json。\n'
        'T1 补充既有事件的 H36 及成本分解、T2 消融属于新增的已暴露历史反馈，非新增独立策略验证。\n',encoding='utf8')
    print('R1 frozen', len(events), sha, flush=True)


def check_freeze():
    seal=json.loads((BASE/'R1_freeze_manifest.json').read_text(encoding='utf8'))
    assert digest(SOURCE)==seal['source_sha256']
    assert digest(BASE/'protocol.json')==seal['protocol_sha256']
    for name,sha in seal['snapshot_sha256'].items():
        assert digest(BASE/'snapshot'/name)==sha, name
        assert digest(ROOT/name)==sha, 'live source changed: '+name
    return seal


def load_frames(db, start, end, calendar, specs, fees):
    assert end <= CUTOFF and start >= '2023-11-01'
    maps=pd.read_sql_query('select product,effective_date,contract from mappings where effective_date<? order by product,effective_date',db,params=(end,))
    frames={}
    for product,mp in maps.groupby('product',sort=True):
        name=product.split('.')[1].upper(); parts=[]
        for contract in mp.contract.unique():
            q=pd.read_sql_query('select '+','.join(RAW)+' from bars where contract=? and period_seconds=300 and start_ns>=? and end_ns<? order by start_ns',
                db,params=(contract,stamp(start),stamp(end)))
            if len(q):parts.append(q)
        if parts:
            frames[name]=v3.features(prep.prepare(select_main(pd.concat(parts,ignore_index=True),mp),calendar),specs[name+'0'],fee_rule(fees,name))
    return v4.peers(frames)


def add_scales(f):
    f=f.copy()
    for m in [30,60]:
        agg=prep.aggregate(f,m)
        values=pd.Series(np.nan,index=agg.index)
        for _,g in agg.groupby('segment',sort=False):
            values.loc[g.index]=g.tr.rolling(12,min_periods=12).mean()
        f['atr'+str(m)]=np.nan
        for seg,g in f.groupby('segment',sort=False):
            z=agg.loc[agg.segment==seg]; idx=np.searchsorted(z.end_ns.to_numpy(),g.end_ns.to_numpy(),side='right')-1
            if len(z):f.loc[g.index,'atr'+str(m)]=np.where(idx>=0, values.loc[z.index].to_numpy()[np.maximum(idx,0)],np.nan)
    f['fixed30']=np.nan
    for _,g in f.loc[f.valid].groupby(['day','segment'],sort=False):
        # Require six uninterrupted wall-clock bars; do not bridge lunch/break.
        continuous=g.end_ns-g.start_ns.shift(5)==30*60*10**9
        denom=np.sqrt(g.tr.pow(2).rolling(6,min_periods=6).sum()).replace(0,np.nan)
        f.loc[g.index,'fixed30']=((g.close-g.open.shift(5))/denom).where(continuous)
    return f


def state_panel(frames):
    out={n:f.copy() for n,f in frames.items()}
    for f in out.values():f['fixed_peer']=np.nan;f['fixed_peer_count']=0
    for group in v3.GROUPS.values():
        names=[n for n in group if n in frames]
        panel=pd.concat({n:frames[n].set_index('end_ns').fixed30 for n in names},axis=1)
        for name in names:
            other=panel.drop(columns=name);count=other.notna().sum(axis=1)
            med=other.median(axis=1).where(count>=2)
            out[name]['fixed_peer']=out[name].end_ns.map(med)
            out[name]['fixed_peer_count']=out[name].end_ns.map(count).fillna(0).astype(int)
    return out


def path_label(a,i,side,h,spec,fees,end):
    j=i+1;k=j+h
    r=dict(status='censored_day_or_gap',gross_yuan=np.nan,cost_yuan=np.nan,net_yuan=np.nan,
        gross_R=np.nan,cost_R=np.nan,net_R=np.nan,MFE_R=np.nan,MAE_R=np.nan,net_2tick_yuan=np.nan)
    if j>=len(a['day']):return r
    z=slice(j,k)
    if k>len(a['day']) or a['day'][k-1]!=a['day'][i] or np.any(a['segment'][z]!=a['segment'][i]) or not np.all(a['valid'][z]):return r
    if int(a['end_ns'][k-1])>=end:r['status']='purged_boundary';return r
    if a['minute'][j]>=890 or np.any(a['volume'][z]<=0):r['status']='unknown_execution_or_late';return r
    mult=spec['multiplier'];tick=spec['tick'];op=float(a['open'][j]);cl=float(a['close'][k-1]);scale=float(a['atr'][i])*mult
    if not np.isfinite(scale) or scale<=0:r['status']='missing_scale';return r
    gross=(cl-op)*side*mult
    def cost(s):return 2*s*tick*mult+v4.fee(fees['open'],op+side*s*tick,mult)+v4.fee(fees['close_today'],cl-side*s*tick,mult)
    c=cost(1);r.update(status='complete',gross_yuan=gross,cost_yuan=c,net_yuan=gross-c,
        gross_R=gross/scale,cost_R=c/scale,net_R=(gross-c)/scale,net_2tick_yuan=gross-cost(2),
        MFE_R=max(0,(np.max(a['high'][z])-op) if side==1 else (op-np.min(a['low'][z])))*mult/scale,
        MAE_R=max(0,(op-np.min(a['low'][z])) if side==1 else (np.max(a['high'][z])-op))*mult/scale)
    return r


def base_events(f,tick):
    a={k:f[k].to_numpy() for k in f};out=[]
    for i in range(1,len(f)):
        if not a['valid'][i] or a['day'][i]!=a['day'][i-1] or a['end_ns'][i-1]!=a['start_ns'][i] or a['segment'][i]!=a['segment'][i-1]:continue
        upper=a['range_high12'][i];lower=a['range_low12'][i];atr=a['atr'][i-1]
        if not np.isfinite(upper+lower+atr) or upper<=lower or atr<=0:continue
        hi,lo,op,cl=[a[k][i] for k in ['high','low','open','close']]
        up=hi>=upper+.25*atr;down=lo<=lower-.25*atr;d=0
        if up and not down and lower<=cl<upper and cl<op and cl<a['close'][i-1]:d=-1
        elif down and not up and lower<cl<=upper and cl>op and cl>a['close'][i-1]:d=1
        if d:out.append(dict(index=i,side=d,stop=float(hi+tick if d==-1 else lo-tick),target=float((upper+lower)/2)))
    return out


def summarize_events(frame,keys):
    out=[]
    for key,g in frame.groupby(keys,dropna=False,sort=True):
        if not isinstance(key,tuple):key=(key,)
        q=g.loc[g.status=='complete'];row=dict(zip(keys,key))
        row.update(event_count=len(g),complete_events=len(q),unique_days=g.day.nunique(),complete_days=q.day.nunique(),
            censored_fraction=1-len(q)/len(g),execution='execution_proxy', matched_subset_date_equal='not_computed')
        for col in ['gross_R','cost_R','net_R','gross_yuan','cost_yuan','net_yuan','MFE_R','MAE_R','net_2tick_yuan']:
            row[col]=q[col].mean();row[col+'_date_equal']=q.groupby('day')[col].mean().mean()
        row['cost_to_positive_gross']=row['cost_R']/row['gross_R'] if row['gross_R']>0 else np.nan
        row['stop_budget_pass_400']=g.budget400.mean() if 'budget400' in g else np.nan
        row['stop_budget_pass_600']=g.budget600.mean() if 'budget600' in g else np.nan
        out.append(row)
    return pd.DataFrame(out)


def run():
    check_freeze()
    out=BASE/'diagnostics';out.mkdir(exist_ok=False)
    meta=json.loads((ROOT/'scripts/factor-study-v1.json').read_text(encoding='utf8'));specs=specifications(meta)
    fees=json.loads((ROOT/'scripts/exchange_fees_20261022.json').read_text(encoding='utf8'))
    cal=Calendar(json.loads((ROOT/'data/trading_calendar.json').read_text(encoding='utf8')),meta['night_ends'])
    legacy=[];input_hashes={}
    for version in ['V1','V2','V3','V4']:
        path=latest(version)/('raw_alpha_events.parquet' if version=='V1' else 'events.parquet');input_hashes[str(path.relative_to(ROOT))]=digest(path)
        # Predicate pushdown forbids loading later strategy outcomes, including accidental future run files.
        q=pq.read_table(path,columns=['event_id','product','signal_ns','side','stop','candidate_id','fold_id','hypothesis_id'],
            filters=[('signal_ns','>=',stamp('2023-11-01')),('signal_ns','<',stamp(CUTOFF))]).to_pandas()
        q['version']=version;legacy.append(q)
    legacy=pd.concat(legacy,ignore_index=True)
    # Deduplicate overlapping V1 discovery windows, but retain family/template identities for a supplemental table.
    legacy=legacy.drop_duplicates(['version','product','signal_ns','side','candidate_id'])
    maps=[];states=[];paths=[];funnel_events=[];checks=[];access=[]
    db=sqlite3.connect(SOURCE.resolve().as_uri()+'?mode=ro',uri=True);db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
    try:
        for period,start,end in PERIODS:
            print('R1 preparing',period,flush=True)
            frames=state_panel({n:add_scales(f) for n,f in load_frames(db,start,end,cal,specs,fees).items()})
            # Recompute actual prefix from fresh SQL, features and peers; never compare merely sliced full features.
            cut='2024-05-08 11:00' if period=='D1' else '2025-05-08 11:00'
            prefix=state_panel({n:add_scales(f) for n,f in load_frames(db,start,cut,cal,specs,fees).items()})
            for name in ['C','JD','PB']:
                cols=['end_ns','norm_day_move','peer_median','peer_abs_median','fixed30','fixed_peer','fixed_peer_count','atr30','atr60']
                actual=frames[name].loc[frames[name].end_ns<stamp(cut),cols].reset_index(drop=True)
                pd.testing.assert_frame_equal(actual,prefix[name][cols].reset_index(drop=True))
                checks.append(dict(period=period,product=name,rows=len(actual),fresh_sql_prefix_pass=True))
            for name,f in sorted(frames.items()):
                print('R1',period,name,len(f),flush=True)
                a={k:f[k].to_numpy() for k in f};spec=specs[name+'0'];fee=fee_rule(fees,name);mult=spec['multiplier'];tick=spec['tick']
                access.append(dict(period=period,product=name,start=start,end_exclusive=end,rows=len(f),max_end_ns=int(f.end_ns.max())))
                for (year,minute),g in f.groupby([f.day.str[:4],f.minute]):
                    cost=2*tick*mult+g.close.map(lambda p:v4.fee(fee['open'],p,mult)+v4.fee(fee['close_today'],p,mult))
                    row=dict(period=period,product=name,year=year,minute=int(minute),bars=len(g),unique_days=g.day.nunique(),contract_multiplier=mult,tick_price=tick,tick_yuan=tick*mult,
                        typical_spread='unknown',margin_proxy='unknown',notional_yuan_median=(g.close*mult).median(),fee_source='exchange_fees_20261022 fixed snapshot; not historical verified',
                        specification_flag='unverified_conflict_or_override' if name in ['BU','JD','SH'] else 'existing_table_proxy',
                        planned_cost_yuan_median=cost.median(),planned_2tick_cost_yuan_median=(cost+2*tick*mult).median(),
                        open_fee_yuan_median=g.close.map(lambda p:v4.fee(fee['open'],p,mult)).median(),
                        close_today_fee_yuan_median=g.close.map(lambda p:v4.fee(fee['close_today'],p,mult)).median(),
                        close_yesterday_fee_yuan_median=g.close.map(lambda p:v4.fee(fee['close_yesterday'],p,mult)).median(),
                        calendar_known_fraction=g.calendar_known.mean())
                    for label,col in [('5','atr'),('30','atr30'),('60','atr60')]:
                        ratio=cost/(g[col]*mult)
                        for quant in [.1,.5,.9]:
                            row[f'ATR{label}_ticks_q{int(100*quant)}']=(g[col]/tick).quantile(quant)
                            row[f'cost_to_ATR{label}_q{int(100*quant)}']=ratio.quantile(quant)
                        row[f'ATR{label}_missing_fraction']=g[col].isna().mean()
                    maps.append(row)
                for minute,g in f.groupby('minute'):
                    for label,col,countcol in [('original','peer_median','peer_count'),('fixed30','fixed_peer','fixed_peer_count')]:
                        x=g[col].replace([np.inf,-np.inf],np.nan)
                        row=dict(period=period,product=name,minute=int(minute),feature=label,bars=len(g),unique_days=g.day.nunique(),
                            missing_fraction=x.isna().mean(),coverage=x.notna().mean(),effective_peer_count_mean=g[countcol].mean(),
                            quiet_coverage=(g.peer_median.abs().le(1)&g.peer_abs_median.le(1)).mean() if label=='original' else np.nan)
                        for quant in [.01,.1,.5,.9,.99]:row['q'+str(int(quant*100))]=x.quantile(quant)
                        states.append(row)
                index={int(ns):i for i,ns in enumerate(f.end_ns)}
                es=legacy.loc[(legacy['product']==name)&legacy.signal_ns.ge(stamp(start))&legacy.signal_ns.lt(stamp(end))]
                for e in es.to_dict('records'):
                    i=index.get(int(e['signal_ns']))
                    if i is None:continue
                    stop=e['stop'];side=int(e['side']);ref=float(a['close'][i]+side*tick)
                    risk=(ref-stop+side*tick)*side*mult+v4.fee(fee['open'],ref,mult)+v4.fee(fee['close_today'],stop-side*tick,mult) if pd.notna(stop) else np.nan
                    for h in [6,12,24,36]:
                        paths.append(dict(period=period,signal_group=e['version']+'-'+e['hypothesis_id'],candidate_id=e['candidate_id'],
                            product=name,event_id=e['event_id'],signal_ns=int(e['signal_ns']),side=side,day=str(a['day'][i]),minute=int(a['minute'][i]),year=str(a['day'][i])[:4],horizon=h,
                            budget400=bool(0<risk<=400),budget600=bool(0<risk<=600),cost_to_stop=(2*tick*mult+v4.fee(fee['open'],ref,mult)+v4.fee(fee['close_today'],ref,mult))/(abs(ref-stop)*mult) if pd.notna(stop) and ref!=stop else np.nan,
                            **path_label(a,i,side,h,spec,fee,stamp(end))))
                original=v4.signals(f,v4.templates([name])[0],tick);original_ids={q['index'] for q in original}
                eligible=0
                for q in base_events(f,tick):
                    i=q['index'];side=q['side'];ref=float(a['close'][i]+side*tick)
                    stopfill=q['stop']-side*tick
                    risk=(ref-stopfill)*side*mult+v4.fee(fee['open'],ref,mult)+v4.fee(fee['close_today'],stopfill,mult)
                    tradable=bool(a['economic_gate'][i] and a['activity_known'][i] and a['minute'][i]+5<890)
                    state=bool(v4.state_at(a,i-1,'quiet') and np.isfinite(a['er12'][i-1]) and a['er12'][i-1]<=.4)
                    final=i in original_ids
                    eligible+=int(tradable and state and final)
                    row=dict(period=period,product=name,event_id=f'{period}:BASE12:{name}:{int(a["end_ns"][i])}:{side}',
                        signal_ns=int(a['end_ns'][i]),day=str(a['day'][i]),minute=int(a['minute'][i]),side=side,
                        pass_tradability=tradable,pass_state=state,pass_original_confirmation=final,
                        budget400=bool(0<risk<=400),budget600=bool(0<risk<=600),signal_plan_risk_yuan=risk,
                        friction_R=float(a['friction_R'][i]),activity_known=bool(a['activity_known'][i]),
                        late=bool(a['minute'][i]+5>=890),state_known=bool(np.isfinite(a['peer_median'][i-1]+a['peer_abs_median'][i-1])),
                        **path_label(a,i,side,12,spec,fee,stamp(end)))
                    for budget in [400,600]:
                        trade=v4.trade(a,i,side,q['stop'],q['target'],spec,fee,stamp(end),budget=budget)
                        row[f'trade{budget}_status']=trade['trade_status'];row[f'trade{budget}_net_yuan']=trade['trade_net_cny'];row[f'trade{budget}_reason']=trade['trade_reason']
                    funnel_events.append(row)
                assert eligible==len(original), (period,name,eligible,len(original))
    finally:
        db.close()
    pd.DataFrame(maps).to_csv(out/'R1_market_cost_map.csv',index=False)
    pd.DataFrame(states).to_csv(out/'R1_regime_scale_audit.csv',index=False)
    pd.DataFrame(access).to_csv(out/'data_access.csv',index=False)
    write_json(out/'prefix_checks.json',checks)
    paths=pd.DataFrame(paths);paths.to_parquet(out/'R1_existing_event_paths.parquet',index=False)
    paths['identity']=paths.signal_group+':'+paths['product']+':'+paths.signal_ns.astype(str)+':'+paths.side.astype(str)
    distinct=paths.drop_duplicates(['period','identity','horizon'])
    feasibility=summarize_events(distinct,['signal_group','period','product','horizon'])
    feasibility.to_csv(out/'R1_cost_feasibility.csv',index=False)
    summarize_events(distinct,['signal_group','period','year','minute','horizon']).to_csv(out/'R1_cost_by_slot_year.csv',index=False)
    summarize_events(paths,['signal_group','candidate_id','period','product','horizon']).to_csv(out/'R1_cost_by_candidate.csv',index=False)
    summarize_events(distinct,['signal_group','period','horizon']).to_csv(out/'R1_family_overview.csv',index=False)
    fe=pd.DataFrame(funnel_events);fe.to_parquet(out/'R1_funnel_event_audit.parquet',index=False)
    s1path=ROOT/'artifacts/s1/regime_features.parquet'
    s1=pq.read_table(s1path,columns=['product','end_ns','market_state'],filters=[('end_ns','<',stamp(CUTOFF))]).to_pandas()
    input_hashes[str(s1path.relative_to(ROOT))]=digest(s1path)
    # Descriptive S1 classifications never gated V4: measure overlap, do not invent an exclusion.
    s1['product']=s1['product'].str.split('.').str[-1].str.upper().str.replace('0','',regex=False)
    linked=fe.merge(s1,left_on=['product','signal_ns'],right_on=['product','end_ns'],how='left',validate='many_to_one')
    linked.groupby(['period','market_state'],dropna=False).agg(events=('event_id','size'),unique_days=('day','nunique')).reset_index().to_csv(out/'R1_s1_classification_overlap.csv',index=False)
    rows=[]
    for (period,budget),_ in (( (p,b),None) for p,_,_ in PERIODS for b in [400,600]):
        g=fe.loc[fe.period==period].copy();previous=pd.Series(True,index=g.index)
        masks=[previous.copy(),g.pass_tradability,g.pass_tradability&g[f'budget{budget}'],
            g.pass_tradability&g[f'budget{budget}']&g.pass_state,
            g.pass_tradability&g[f'budget{budget}']&g.pass_state&g.pass_original_confirmation]
        for step,mask in enumerate(masks):
            for cohort,selection in [('retained',mask),('removed_at_step',previous&~mask),('all_removed',~mask)]:
                q=g.loc[selection];complete=q.loc[q.status=='complete']
                row=dict(period=period,budget=budget,step=step+1,condition=protocol()['funnel_steps'][step],cohort=cohort,
                    event_count=len(q),unique_days=q.day.nunique(),complete_events=len(complete),complete_days=complete.day.nunique(),
                    share_of_base=len(q)/len(g),matched_subset_date_equal='not_computed')
                for col in ['gross_yuan','cost_yuan','net_yuan','gross_R','cost_R','net_R']:
                    row[col]=complete[col].mean();row[col+'_date_equal']=complete.groupby('day')[col].mean().mean()
                trades=q.loc[q[f'trade{budget}_status']=='complete']
                row.update(structural_trade_count=len(trades),structural_net_yuan=trades[f'trade{budget}_net_yuan'].mean(),
                    structural_net_yuan_date_equal=trades.groupby('day')[f'trade{budget}_net_yuan'].mean().mean())
                rows.append(row)
            previous=mask
    pd.DataFrame(rows).to_csv(out/'R1_funnel.csv',index=False)
    reasons=[]
    for period,g in fe.groupby('period'):
        for key,mask in [('cost_gate_failed_or_unknown',~g.friction_R.le(.25)),('activity_unknown',~g.activity_known),('late',g.late),('state_unknown',~g.state_known),('quiet_or_ER_failed',~g.pass_state),('budget400_failed',~g.budget400),('budget600_failed',~g.budget600)]:
            q=g.loc[mask];reasons.append(dict(period=period,reason=key,events=len(q),unique_days=q.day.nunique(),overlap='nonexclusive'))
    pd.DataFrame(reasons).to_csv(out/'R1_funnel_reasons.csv',index=False)
    # Reconcile final frozen branch with archived Q-C01, not with a newly optimized rule.
    archived=pq.read_table(latest('V4')/'events.parquet',filters=[('candidate_id','==','Q-C01')]).to_pandas()
    reconcile=[]
    for period,g in fe.groupby('period'):
        q=g.loc[g.pass_tradability&g.budget400&g.pass_state&g.pass_original_confirmation]
        old=archived.loc[(archived.fold_id==period)&(archived.trade_status=='complete')]
        new=q.loc[q.trade400_status=='complete']
        assert len(new)==len(old),(period,len(new),len(old))
        assert abs(new.trade400_net_yuan.sum()-old.trade_net_cny.sum())<1e-7
        reconcile.append(dict(period=period,complete_trades=len(new),archive_complete_trades=len(old),net_sum_yuan=float(new.trade400_net_yuan.sum()),exact_aggregate_match=True))
    write_json(out/'Q_C01_reconciliation.json',reconcile)
    assert all(r['max_end_ns']<stamp(CUTOFF) for r in access)
    assert int(paths.signal_ns.max())<stamp(CUTOFF)
    write_json(out/'verification.json',dict(prefix_checks=checks,reconciliation=reconcile,reserved_outcomes_accessed=False,
        prior_strategy_evaluations_not_rerun=True,new_strategy_templates=0,shared_account_tested=False,
        event_path_rows=len(paths),funnel_base_events=len(fe),max_signal_ns=int(paths.signal_ns.max()),input_sha256=input_hashes))
    print('R1 diagnostics computed; report next',flush=True)


def seal():
    check_freeze()
    out=BASE/'diagnostics'
    write_json(out/'artifact_hashes.json',{str(p.relative_to(out)):digest(p) for p in sorted(out.rglob('*')) if p.is_file() and p.name!='artifact_hashes.json'})


def verify():
    check_freeze();out=BASE/'diagnostics'
    for name,sha in json.loads((out/'artifact_hashes.json').read_text(encoding='utf8')).items():assert digest(out/name)==sha,name
    for name,sha in json.loads((out/'verification.json').read_text(encoding='utf8'))['input_sha256'].items():assert digest(ROOT/name)==sha,name
    print('R1 source/snapshot/input/output hashes verified')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['freeze','run','seal','verify']);args=parser.parse_args()
    globals()[args.action]()
