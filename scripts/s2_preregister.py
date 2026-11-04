"""S2 preregistration and budget ledger. Never loads bars, signals or returns.

init seals protocol documents once; verify checks hashes/schema; reserve-wfo
consumes one of two evaluator reservations before an evaluation is started.
"""
import argparse
import collections
import csv
import datetime as dt
import hashlib
import itertools
import json
import os
from pathlib import Path
import subprocess

import yaml

ROOT=Path(__file__).resolve().parents[1]
DEFAULT=ROOT/'artifacts/s2'
FIELDS=['experiment_id','created_utc','research_version','fold_id','hypothesis_id','candidate_id',
        'candidate_sha256','status','reason','code_git_sha','data_sha256','feedback_count','result_artifact']


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(4*1024*1024),b''):h.update(b)
    return h.hexdigest()


def canonical(obj):
    return hashlib.sha256(json.dumps(obj,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def write_json(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8')


def write_csv(path,fields,rows):
    with path.open('w',encoding='utf8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)


def utc():return dt.datetime.now(dt.timezone.utc).isoformat()


def month(offset):
    year,mm=divmod(2023*12+10+offset,12)
    return f'{year:04d}-{mm+1:02d}-01'


def folds():
    return [dict(fold_id=f'F{i+1}',discovery_start=month(3*i),discovery_end=month(3*i+9),
        selection_start=month(3*i+9),selection_end=month(3*i+12),test_start=month(3*i+12),test_end=month(3*i+15)) for i in range(6)]


def hypotheses():
    common=dict(decision_time='5m bar end; higher-timeframe buckets must already be complete',
        entry='next eligible 5m concrete-contract open, one adverse tick; no signal-bar fill',
        session_convention='daytime only, no new entry in final 10 minutes before 15:00; session end means final daytime close, scheduled morning/lunch breaks pause active-bar counters',
        event_horizons_bars=[6,12,24],primary_horizon_bars=12,
        risk_scale='R_price=ATR12 of completed 5m bars at signal time; fixed before future labels',
        cooldown='per product keep first event; no new event until prior 24-bar label expires or session closes',
        requirements=['unadjusted concrete-contract OHLCV','main mapping with conservative previous-natural-date convention',
                      'complete 5m and completed 15m/30m/60m aggregation','tick and multiplier proxy with provenance',
                      'pre-known static sessions; official-calendar coverage flags'],
        baselines=['matched mechanical/random entry cohort','same-product/time/direction/volatility stratified baseline'],
        max_candidates_per_fold=8)
    result=[]
    defs=[
      dict(id='H1',title='强势方向动量后的适度回调',
        intuition='Directional inventory adjustment can continue after a bounded counter-move; confirmation may reduce chasing.',
        expected_states='Directional displacement with bounded pullback; high noise/large gaps may invalidate continuation.',
        falsifiable='At h=12, net forward_R exceeds matched baseline; secondary MFE/MAE and +1R/+2R-before--1R are consistent.',
        failure_modes=['momentum exhaustion','reversal mislabeled as pullback','transaction cost exceeds continuation'],
        continuous_parameters=['impulse_atr_min','pullback_fraction_max'],
        parameter_grid=dict(momentum_minutes=[30,60],impulse_atr_min=[1.0,1.5],pullback_fraction_max=[0.33,0.5]),
        rules=[
          'At completion of a selected 30m/60m impulse bar B, d=sign(B.close-B.open). Require abs(body)>=impulse_atr_min*ATR12 of preceding completed same-timeframe bars; ATR excludes B. Zero body/ATR invalid.',
          'Freeze B high/low and directional endpoint (high for long, low for short), body distance abs(B.close-B.open), and d. Earliest pullback observation is a later 5m bar.',
          'During next 6 completed 5m bars, require at least one counter-direction candle. Pullback depth from frozen directional endpoint must be >0 and <=pullback_fraction_max*body distance; use lowest low since B for long / highest high for short.',
          'After counter-direction candle, confirm when a later 5m close exceeds previous 5m high (long) or falls below previous low (short). Confirmation cannot be the counter-direction candle itself.',
          'Cancel on depth violation, 6-bar expiry, session end, data gap or contract change. Consume impulse at first confirmation; structural stop anchor is observed pullback extreme plus one adverse tick.'
        ]),
      dict(id='H2',title='区间突破后的回踩确认',
        intuition='A breakout can consume resting liquidity; a bounded retest may reveal continuing acceptance outside the prior box.',
        expected_states='Prior bounded range with low net drift, volume-supported breakout, then a structurally intact retest.',
        falsifiable='Confirmed retests have positive h=12 matched net effect; unconditioned first-break cohort is retained as a secondary mechanical comparator.',
        failure_modes=['false breakout','retest destroys structure','conditioning only on successful future retests biases the breakout comparison'],
        continuous_parameters=['breakout_volume_multiple','retest_tolerance_atr'],
        parameter_grid=dict(box_30m_bars=[4,8],breakout_volume_multiple=[1.2,1.5],retest_tolerance_atr=[0.25,0.5]),
        rules=[
          'Use last box_30m_bars fully completed 30m bars before the current 5m signal; U=max(high), L=min(low). Require U-L>=2 ticks and abs(last close-first open)/(U-L)<=0.5.',
          'First 5m close strictly outside U/L establishes direction. Current volume must exceed breakout_volume_multiple times the mean volume of prior 20 completed 5m bars (exclude current); zero/missing baseline invalid.',
          'Freeze box, d and ATR12 of preceding completed 5m bars. Search only next 6 completed 5m bars; breakout bar cannot be its own retest.',
          'Long: low<=U+tolerance*ATR, low>=U-tolerance*ATR, close>=U, close>open and close>previous close; short mirrors at L. The first qualifying later bar confirms.',
          'Cancel if long low<U-tolerance*ATR (short high>L+tolerance*ATR), expiry, session end, gap or roll. Structural stop anchor is observed retest extreme plus one adverse tick.',
          'Record every first breakout including those never retested. Comparing only future-confirmed retests to their first breakout is diagnostic conditioning, not an unbiased causal comparator.'
        ]),
      dict(id='H3',title='波动扩张后的短期趋势延续',
        intuition='A volatility expansion after compression can reflect new directional order flow aligned with a slower displacement.',
        expected_states='Past compression, new expansion with directional body, and aligned higher-timeframe displacement.',
        falsifiable='At h=12, expansion events beat activity/volatility-matched entries after costs; advantage cannot be explained only by larger unconditional ranges.',
        failure_modes=['news reversal','large range without directional continuation','cost/volatility matching removes apparent effect'],
        continuous_parameters=['expansion_ratio_min','trend_displacement_atr_min'],
        parameter_grid=dict(expansion_minutes=[5,15],expansion_ratio_min=[1.5,2.0],trend_displacement_atr_min=[0.5,1.0]),
        rules=[
          'At selected 5m/15m bar B completion, compute B true range / median true range of prior 20 completed same-timeframe bars; require >=expansion_ratio_min. Denominator excludes B and must be >0.',
          'Compression: mean of last 5 preceding true ranges <= median of last 20 preceding true ranges. Both are historical at B start.',
          'd=sign(B.close-B.open), require nonzero body and body/true range>=0.5.',
          'Slower displacement uses last two fully completed 30m bars available at B end: last close minus first open. Require same sign d and abs(displacement)>=trend_displacement_atr_min*ATR12 from 30m history strictly preceding those two bars.',
          'Confirm at B end (a 5m boundary), emit once per B; no extra wait rule. Cancel missing/partial buckets, roll or session end. Structural stop anchor is B adverse extreme plus one tick.'
        ])]
    for d in defs:result.append({**common,**d})
    return result


def candidates(hs,universe):
    result=[]
    for h in hs:
        keys=list(h['parameter_grid'])
        for i,values in enumerate(itertools.product(*(h['parameter_grid'][k] for k in keys)),1):
            item=dict(candidate_id=f"{h['id']}-C{i:02d}",hypothesis_id=h['id'],parameters=dict(zip(keys,values)),
                universe=universe,directions=['long','short'],entry_sessions=['day'],allow_overnight=False,
                implementation_status='specification_only_not_executable',signal_implementation_sha=None)
            item['candidate_sha256']=canonical(item);result.append(item)
    return result


def budget(universe):
    return dict(schema='s2-search-budget-v1',research_version='1.0',max_hypotheses=3,
        max_candidates_per_hypothesis_per_fold=8,max_candidates_per_fold=24,
        max_continuous_parameters_per_hypothesis=2,max_full_wfo_reservations_per_major_version=2,
        universe=universe,universe_size=len(universe),universe_decision='retain all 22 under accepted S1 scope; no performance-based deletion',
        entry_sessions=['day'],allow_night=False,allow_overnight=False,
        accounting='Each product subset, side filter, timeframe choice, time filter or rule change is a distinct candidate; current 24 slots fully allocated. No hidden neighbors.',
        templates_global=24,folds=6,maximum_candidate_fold_configurations_per_wfo=144,
        exposures='Reserve before every full WFO, including unsuccessful/aborted runs. More than two requires new major version and contaminated-history declaration.',
        aggregation='Clock-aligned completed buckets only; discard partials. Keep same-contract history over scheduled closures; reset on roll/unexpected missing bar. Unknown calendar/gaps flagged diagnostic, never silently filled.',
        execution=dict(initial_capital=50000,trade_budget=400,max_risk_per_lot=600,max_total_risk=900,max_margin_equity=.6,
            daily_stop=900,weekly_review=2000,drawdown_warning=.08,drawdown_pause=.12,
            fee_snapshot='scripts/exchange_fees_20261022.json',fee_convention='exchange base snapshot, SR published_schedule_before_override; distinguish open/today/yesterday; no broker addition',
            slip_ticks_each_side=1,ambiguous_path='stop_first',limit_queue='reject_unknown',
            s3_raw_events='retain economically infeasible events with flag; report feasibility separately, no stop tightening to force one lot'),
        s3_design=dict(primary_horizon_bars=12,secondary_horizons_bars=[6,24],risk_scale='signal-time ATR12 5m',
            primary_effect='mean net forward_R difference versus matched baseline, equal weight per natural day (day-only entries)',
            labels='Only completed bar observations after next executable entry, max 24 active 5m bars, truncated at known day close; mark censored horizons separately, do not fill with zero.',
            label_window='entry is first subsequent bar; h=6/12/24 label close of sixth/twelfth/twenty-fourth bar including entry bar',
            matching=dict(product='exact',clock_slot='exact 5m slot',direction='exact',month='exact within discovery',
                atr_pct_bins=[0,.0005,.001,.002,.004,'infinity'],volatility_source='ATR12/close at event time, fixed bins',
                pool='only eligible decision times entirely within the same discovery window; exclude event timestamps; match without replacement within replicate',
                unmatched='mark unmatched; never widen strata after results',replicates=200,seed=20261104),
            inference=dict(block_unit='natural trading date for day-only entries; all products on one date resampled together',
                bootstrap_replicates=1000,seed=20261105,interval=.95,
                primary_multiple_testing='one-sided day-block paired tests with Holm correction across all 24 planned candidates per fold; unavailable candidates retain p=1',
                secondary='MFE/MAE, +1R/+2R-before--1R, horizons6/24 are descriptive; no best-horizon selection'),
            decision=dict(minimum_events=60,minimum_independent_days=30,minimum_matched_fraction=.8,
                positive_effect='primary net matched effect >0 and day-block 95% lower confidence bound >0 and Holm adjusted p<=0.05',
                stability='split discovery into three chronological 3-month blocks; at least two blocks positive with >=10 events each',
                insufficiency='insufficient/censored/unmatched evidence => unconfirmed, not proof of effectiveness or ineffectiveness',
                threshold_origin='initial preregistered screening conventions, not historical return-calibrated thresholds')),
        leakage=dict(purge='label_end must be strictly before current discovery/selection end; exclude crossing events',
            embargo='first full trading date of each selection/test window excluded; max label is same-day, so one full day minimum',
            researcher='discovery only, no selection/test strategy results',selector='frozen candidates on selection only; may choose none',
            evaluator='frozen code/data/config hashes before test; batch results released after all decisions frozen',
            enforcement='logical API/runner contract, not operating-system access isolation'),
        selection=dict(min_trades=20,min_independent_days=15,require_positive_net_expectancy=True,
            reject_any_execution_or_account_violation=True,missing_evidence='empty selection',
            ranking=['lower shared-account max drawdown','lower median realized roundtrip cost / signal ATR risk','candidate_id lexical tie break'],
            max_selected_per_family=1,rule_changes_after_selection=False,
            parameter_sensitivity='report already registered adjacent configurations; do not generate or choose new neighbors'),
        final_diagnostic=dict(start=month(30),end_exclusive=month(36),one_time=True,
            independence='historical diagnostic only; prior strategy exposure history incomplete and S1 market descriptors already viewed',
            actual_data_end='2026-10-27 15:00:00 Asia/Shanghai',coverage='incomplete final window; never label as six complete months'),
        modifications='Before results, clerical fixes require a new seal and change record. After any feedback, new variants count toward budget; no retroactive replacement of failed candidates.')


def validate_documents(hs,b,cs):
    if len(hs)!=3 or {h['id'] for h in hs}!={'H1','H2','H3'}:raise ValueError('exactly three hypotheses required')
    if len(cs)!=24 or len({c['candidate_id'] for c in cs})!=24:raise ValueError('24 unique candidates required')
    if b['max_full_wfo_reservations_per_major_version']!=2:raise ValueError('WFO cap must be two')
    counts=collections.Counter(c['hypothesis_id'] for c in cs)
    if set(counts)!=set(h['id'] for h in hs) or any(n!=8 for n in counts.values()):raise ValueError('eight per family')
    for h in hs:
        if len(h['continuous_parameters'])>2:raise ValueError('continuous parameter cap')
        expected={canonical(dict(zip(h['parameter_grid'],v))) for v in itertools.product(*h['parameter_grid'].values())}
        actual={canonical(c['parameters']) for c in cs if c['hypothesis_id']==h['id']}
        if expected!=actual or len(expected)!=8:raise ValueError('candidate space differs from registered grid')
    for c in cs:
        payload={k:v for k,v in c.items() if k!='candidate_sha256'}
        if canonical(payload)!=c['candidate_sha256']:raise ValueError('candidate hash mismatch')
        if c['universe']!=b['universe'] or c['directions']!=['long','short'] or c['entry_sessions']!=['day'] or c['allow_overnight']:
            raise ValueError('unbudgeted universe/side/session variant')
    fs=folds()
    for i,f in enumerate(fs):
        if not (f['discovery_start']<f['discovery_end']==f['selection_start']<f['selection_end']==f['test_start']<f['test_end']):raise ValueError('window overlap')
        if i and fs[i-1]['test_end']!=f['test_start']:raise ValueError('non-contiguous OOS')


def verify(out):
    seal=json.loads((out/'freeze_manifest.json').read_text(encoding='utf8'))
    for name,expected in seal['frozen_files'].items():
        if sha(out/name)!=expected:raise ValueError('frozen artifact changed: '+name)
    for section in ('code_sha256','input_sha256'):
        for name,expected in seal[section].items():
            if sha(ROOT/name)!=expected:raise ValueError('frozen source/input changed: '+name)
    hs=yaml.safe_load((out/'hypotheses.yaml').read_text(encoding='utf8'))['hypotheses']
    b=yaml.safe_load((out/'search_budget.yaml').read_text(encoding='utf8'))
    cs=json.loads((out/'candidate_manifest.json').read_text(encoding='utf8'))['candidates']
    validate_documents(hs,b,cs)
    with (out/'wfo_folds.csv').open(encoding='utf8',newline='') as f:
        recorded=list(csv.DictReader(f))
    if recorded!=folds():raise ValueError('registered fold windows differ from specification')
    reservations(out)
    return seal


def reservations(out):
    journal=out/'wfo_reservations.jsonl';rows=[];previous='0'*64
    if not journal.exists():raise ValueError('reservation journal missing')
    for line in journal.read_text(encoding='utf8').splitlines():
        r=json.loads(line);payload={k:v for k,v in r.items() if k!='sha256'}
        if r['previous_sha256']!=previous or canonical(payload)!=r['sha256']:raise ValueError('reservation chain changed')
        if r['reservation_id']!=f'WFO-{len(rows)+1:02d}':raise ValueError('invalid reservation order')
        rows.append(r);previous=r['sha256']
    if len(rows)>2:raise ValueError('WFO budget exceeded')
    return rows


def reserve_wfo(out,reason):
    """Serialized append-only reservations; failures do not refund search exposure."""
    if not reason.strip():raise ValueError('reason required')
    seal=verify(out)
    if sha(ROOT/seal['database']['path'])!=seal['database']['sha256']:raise ValueError('database changed since freeze')
    lock=out/'.reservation.lock'
    fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    try:
        journal=out/'wfo_reservations.jsonl';rows=reservations(out)
        previous=rows[-1]['sha256'] if rows else '0'*64
        if len(rows)>=2:raise ValueError('major version WFO budget exhausted (2)')
        row=dict(reservation_id=f'WFO-{len(rows)+1:02d}',created_utc=utc(),reason=reason,status='reserved_counts_even_if_failed',
            research_version='1.0',previous_sha256=previous)
        row['sha256']=canonical(row)
        with journal.open('a',encoding='utf8') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n');f.flush();os.fsync(f.fileno())
        return row
    finally:os.close(fd);lock.unlink()


def init(out):
    if (out/'freeze_manifest.json').exists():raise ValueError('already frozen; verify instead of overwriting')
    if out.exists() and any(out.iterdir()):raise ValueError('initialization needs an empty directory')
    out.mkdir(parents=True,exist_ok=True)
    s1=ROOT/'artifacts/s1';prior=json.loads((s1/'manifest.json').read_text(encoding='utf8'))
    if not json.loads((s1/'verification.json').read_text(encoding='utf8'))['s1_passed']:raise ValueError('S1 not passed')
    # Read metadata only, never S1 feature/outcome rows or old strategy returns.
    source=ROOT/'data/research_market_history.sqlite';data_sha=sha(source)
    if data_sha!=prior['input_sha256'][str(source.relative_to(ROOT))]:raise ValueError('source changed since S1')
    universe=sorted(c['product'] for c in prior['coverage']);hs=hypotheses();b=budget(universe);cs=candidates(hs,universe)
    validate_documents(hs,b,cs)
    created=utc();git=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    (out/'hypotheses.yaml').write_text(yaml.safe_dump(dict(schema='s2-hypotheses-v1',hypotheses=hs),allow_unicode=True,sort_keys=False),encoding='utf8')
    (out/'search_budget.yaml').write_text(yaml.safe_dump(b,allow_unicode=True,sort_keys=False),encoding='utf8')
    write_json(out/'candidate_manifest.json',dict(schema='s2-candidate-templates-v1',frozen_at=created,git_sha=git,data_sha256=data_sha,candidates=cs))
    write_csv(out/'wfo_folds.csv',list(folds()[0]),folds())
    rows=[dict(experiment_id='PREREG-'+c['candidate_id'],created_utc=created,research_version='1.0',fold_id='template_all_folds',
        hypothesis_id=c['hypothesis_id'],candidate_id=c['candidate_id'],candidate_sha256=c['candidate_sha256'],status='preregistered_not_run',
        reason='S2 fixed hypothesis grid; no strategy-result feedback',code_git_sha=git,data_sha256=data_sha,feedback_count=0,result_artifact='') for c in cs]
    write_csv(out/'experiment_registry.csv',FIELDS,rows)
    exposure_fields=['exposure_id','stage','start','end_exclusive','kind','strategy_results_accessed_this_stage','prior_strategy_exposure','independence','source']
    exposures=[dict(exposure_id='EXP-S1-MARKET',stage='S1',start=month(0),end_exclusive=month(36),kind='descriptive_market_data',
        strategy_results_accessed_this_stage=False,prior_strategy_exposure='not_reconstructed',independence='not_claimed',source='artifacts/s1/data_exposures.csv'),
        dict(exposure_id='EXP-LEGACY-UNKNOWN',stage='legacy',start=month(0),end_exclusive=month(36),kind='prior_research_history_incomplete',
        strategy_results_accessed_this_stage=False,prior_strategy_exposure='unknown_not_zero',independence='not_claimed',source='S0 report and existing prior research context')]
    write_csv(out/'evaluation_exposures.csv',exposure_fields,exposures)
    write_csv(out/'manual_changes.csv',['change_id','created_utc','reason','feedback_seen','old_seal','new_seal'],[])
    (out/'wfo_reservations.jsonl').write_text('',encoding='utf8')
    report=f'''# S2 预注册报告

**S2 已完成：3 个机制、每个 8 组、共 24 个候选模板在任何本协议选择/测试收益评估之前冻结。没有运行信号、Alpha 检验或收益回测。**

研究大版本 1.0；冻结 UTC 时间 {created}；git 基线 `{git}`。新增未提交代码的 SHA256 与单库数据 SHA256 保存在 freeze_manifest.json。唯一数据源为 data/research_market_history.sqlite；沿用用户说明的历史主力来源，并按 S1 保守映射约定处理重叠。

## 已冻结的机制与参数

|机制|离散维度|连续参数1|连续参数2|组数|
|---|---|---|---|---:|
|H1 动量后适度回调|动量周期30/60分钟|冲量/历史ATR最低1.0/1.5|回调深度上限0.33/0.50|8|
|H2 突破回踩|箱体4/8根30分钟|突破量比1.2/1.5|回踩容差0.25/0.50 ATR|8|
|H3 波动扩张延续|扩张周期5/15分钟|扩张比最低1.5/2.0|大周期位移最低0.5/1.0 ATR|8|

完整可证伪命题、机制解释、适用状态、失败原因、逐步确认/取消规则、数据需求和基线写入 hypotheses.yaml。以上是事前初始研究约定，没有根据 S1 中 EG/PP 等品种的表现定制。

22 个品种全部保留；双方向；首版日盘、不隔夜，与 S0 执行假设一致。只改变一个品种子集、方向、周期或时段也视为新增候选，当前每轮 24 个名额已全部分配。每个机制最多两个连续参数；三种标签跨度不作为可择优交易参数。

## Alpha 检验与负对照

S3 主指标固定为未来12根5m的匹配基线净 forward_R 差异；6/24根、MFE/MAE和先触达概率仅作次要描述，不挑最佳持有期。R为信号当时5m ATR12，最早下一根可成交开盘入场。日内截断/路径歧义/缺数均保留，标签越界删除，不填零。

对照按品种、月份、5分钟时刻、方向、事前ATR固定分箱匹配，200次固定种子重复；无足够对照时标记未匹配，禁止看结果后放宽条件。日期分块Bootstrap 1000次、跨品种同日共同重采样；24个主检验使用Holm修正，未可评估配置保留p=1。至少60事件、30日期、80%匹配率；3个发现子块至少2块方向为正且每块>=10事件。它们是初始筛选门槛，不是已验证统计定律。

H2 另保留所有首次突破（包括没有发生回踩的突破），避免只留下未来成功回踩样本。经济上无法一手交易的事件仍保留原始Alpha统计，经济可行性另列，不为了通过600元约束收紧止损。

## 时间窗、选择与反馈预算

wfo_folds.csv 固定 F1–F6 的9+3+3窗口，OOS为2024-11-01至2026-05-01的连续18个月；历史终检为2026-05-01至2026-11-01，实际行情截至2026-10-27，不能称为完整6个月。

每轮最多24个候选，6轮最多144个候选×轮次配置；同一大版本最多2次完整WFO评估，**未启动评估也未消耗WFO次数**。以后必须先 reserve-wfo 再运行；失败/中止也占用次数。逻辑角色窗口、purging、首个完整交易日embargo及可空选的选择排序在 search_budget.yaml 中固定。

选择期先剔除净预期非正、少于20笔/15日期、违反账户/成交规则的候选，然后依次比较较低共享账户回撤、较低成本/ATR、candidate_id。不按收益或Sharpe最高无条件晋级，每机制最多1个，可全部空选。S4之前必须完成对应信号实现代码冻结和真实数据执行适配；当前 candidate_manifest 只冻结**模板规格**，signal_implementation_sha为空，并未冒充已有可执行策略。

## 既往暴露与验收边界

evaluation_exposures.csv 登记 S1 已查看全区间市场描述，以及旧研究反馈史不完整。旧反馈次数记 unknown，不写成0；本协议本阶段策略收益查看次数为0。历史终检仅作诊断，不宣称独立未见。确认边界来自当前会话和审计上下文，本阶段未打开旧收益文件。

S2 可通过不等于 S3 已有Alpha、S4可直接运行或完整S0执行认证通过。Researcher/Selector/Evaluator 在此为逻辑协议和下游接口契约，不是操作系统权限隔离。带哈希的冻结文件可检测常规修改；不提供对恶意改写文件及封印的外部见证保证。

## 文件与命令

- hypotheses.yaml、search_budget.yaml：完整预注册规则。
- candidate_manifest.json：24个模板及各自哈希。
- experiment_registry.csv：24行 preregistered_not_run 的冻结初始快照；后续运行/失败结果另建按日期命名的追加式日志，不覆盖初始快照。
- evaluation_exposures.csv、manual_changes.csv：暴露及人工修改初始快照；后续同样另建日期日志。
- freeze_manifest.json：冻结配置/代码/来源校验；wfo_reservations.jsonl：后续追加式WFO预约链。

```powershell
python scripts/s2_preregister.py verify
python -m unittest discover -s tests -p test_s2_preregister.py -v
# 仅在实际准备启动完整WFO评估时执行，会消耗次数：
# python scripts/s2_preregister.py reserve-wfo --reason "具体评估原因"
```

下一阶段是 S3 原始Alpha检验；按冻结规则先实现信号、做无前视回归，再仅访问每轮发现窗口，不提前读取选择或测试策略收益。
'''
    (out/'S2_preregistration_report.md').write_text(report,encoding='utf8')
    frozen=['hypotheses.yaml','search_budget.yaml','candidate_manifest.json','wfo_folds.csv','experiment_registry.csv',
            'evaluation_exposures.csv','manual_changes.csv','S2_preregistration_report.md']
    # Registry initial files are sealed; append work to separate dated run files,
    # not these initial registration snapshots. Reservation journal is the mutable exception.
    seal=dict(schema='s2-freeze-v1',created_utc=created,research_version='1.0',git_sha=git,
        frozen_files={n:sha(out/n) for n in frozen},code_sha256={str(p.relative_to(ROOT)):sha(p) for p in [Path(__file__),ROOT/'tests/test_s2_preregister.py']},
        input_sha256={str(p.relative_to(ROOT)):sha(p) for p in [s1/'manifest.json',s1/'research_config.json',s1/'verification.json',s1/'data_exposures.csv',
            ROOT/'中国内盘期货量化策略稳健性挖掘与抗过拟合实施方案_V1.0.md',ROOT/'artifacts/s0/S0_scope_amendment.md']},
        database=dict(path=str(source.relative_to(ROOT)),sha256=data_sha),
        s2_passed=True,candidate_templates=24,strategy_evaluations=0,wfo_reservations_at_freeze=0,
        mutable_journal='wfo_reservations.jsonl',future_logs='append separate dated run/exposure/change CSVs; never replace frozen initial snapshots')
    write_json(out/'freeze_manifest.json',seal);verify(out)
    return seal


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['init','verify','reserve-wfo']);p.add_argument('--out',type=Path,default=DEFAULT);p.add_argument('--reason',default='')
    args=p.parse_args();out=args.out.resolve()
    if args.action=='init':r=init(out)
    elif args.action=='verify':r=verify(out)
    else:r=reserve_wfo(out,args.reason)
    print(json.dumps(dict(action=args.action,s2_passed=r.get('s2_passed'),candidate_templates=r.get('candidate_templates'),reservation=r.get('reservation_id')),ensure_ascii=False))


if __name__=='__main__':main()
