"""Frozen A/B/combined exit validation, isolated fork, no production mutation."""
import argparse
import collections
import datetime as dt
import json
import math
import pathlib
import random
import shutil
import statistics
import subprocess
import sys

sys.dont_write_bytecode=True
from research_swing_consistency import REPO,RUN as SOURCE,OLD,read,write,sha,replace_once,portfolio,metrics,ev,cluster,UNKNOWN
from reprice_signal_costs import TradingDates,reprice

RUN=REPO/'research-runs/swing-combination-20261025-v1'


def definitions():
    return [dict(name=base+'_'+variant,base=base,variant=variant,
                 rules=dict(target_cap_r=cap,min_actual_risk_ticks=10.,flat_before_weekend=True,
                            adaptive_ongoing=variant in ('A','C'),adaptive_structure=variant in ('B','C')))
            for base,cap in [('P2',None),('P3',2.)] for variant in ('base','A','B','C')]


def patch(root):
    path=root/'crates/n-core/src/v3/ablation.rs';s=path.read_text(encoding='utf-8')
    s=replace_once(s,'use chrono::{Datelike, NaiveDateTime, TimeZone, Timelike};','use chrono::{Datelike, NaiveDateTime, TimeZone, Timelike};\npub mod swing_adaptive;')
    s=replace_once(s,'    pub entry_mode: EntryMode,','    pub entry_mode: EntryMode,\n    pub adaptive_ongoing: bool,\n    pub adaptive_structure: bool,')
    s=replace_once(s,'    holidays: Vec<HolidayWindow>,','    holidays: Vec<HolidayWindow>,\n    adaptive: swing_adaptive::AdaptiveExit,\n    pending_adaptive_exit: Option<String>,')
    s=replace_once(s,'        holidays: holidays.to_vec(),','        holidays: holidays.to_vec(),\n        adaptive: Default::default(),\n        pending_adaptive_exit: None,')
    s=replace_once(s,'        if !self.first {\n            "stop"','        if self.first && self.rules.adaptive_structure {\n            "structure_protection"\n        } else if !self.first {\n            "stop"')
    s=replace_once(s,'        let had_fill = state.e.fill.is_some();','''        // A completed-bar exit is submitted only after that bar closes.
        // Missing data was checked first; do not invent a delayed fill over a gap.
        if let Some(reason) = state.pending_adaptive_exit.take() {
            state.realize(bar.open-sign*ex.slip_ticks*ev.tick_size,state.remaining,&reason,sign,&bar.datetime);
            break;
        }
        let had_fill = state.e.fill.is_some();''')
    # After all existing price/session/no-follow exits: baseline priorities remain exact.
    s=replace_once(s,'''    }
    if state.e.status == "open" {''','''        if state.e.fill.is_some() && (rules.adaptive_ongoing || rules.adaptive_structure) {
            let action=state.adaptive.observe(bar,state.e.entry_ts.as_deref().unwrap(),state.peak,
                state.e.fill.unwrap(),state.e.risk,sign,ev.tick_size,ev.warning_extreme,ev.s2_price,state.stop,
                rules.adaptive_ongoing,rules.adaptive_structure);
            if let Some(stop)=action.stop {state.stop=stop;state.first=true;}
            if let Some(reason)=action.close {state.pending_adaptive_exit=Some(reason.into());}
        }
    }
    if state.e.status == "open" {''')
    path.write_text(s,encoding='utf-8')
    shutil.copy2(REPO/'scripts/swing_adaptive_exit.rs',root/'crates/n-core/src/v3/ablation/swing_adaptive.rs')


def prepare():
    RUN.mkdir(parents=True,exist_ok=False);root=RUN/'fork';old=SOURCE/'fork'
    for name in ('n-core','n-protocol'):shutil.copytree(old/'crates'/name,root/'crates'/name)
    for name in ('Cargo.toml','Cargo.lock'):shutil.copy2(old/name,root/name)
    (root/'crates/n-core/src/v3/ablation').mkdir(exist_ok=True)
    patch(root)
    shutil.copy2(REPO/'scripts/swing_combination_runner.rs',root/'crates/n-core/src/bin/swing_combination_runner.rs')
    shutil.copy2(REPO/'tests/swing_combination.rs',root/'crates/n-core/tests/swing_combination.rs')
    original=read(SOURCE/'protocol.json')
    for period in ('early','recent'):
        rows=[]
        mask=read(SOURCE/period/'service-dedup-raw-plan.json');allowed={k for v in mask['symbols'].values() for k in v['keys']}
        for line in (SOURCE/period/'events.jsonl').open(encoding='utf-8'):
            r=json.loads(line)
            if r['event_key'] in allowed:rows.append(r)
        write(RUN/(period+'-pool.json'),rows)
    p=dict(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),templates=definitions(),
        modes=['corrected_touch','next_minute_open'],slips=[1,2],periods=original['periods'],
        previous_protocol_sha256=sha(SOURCE/'protocol.json'),production_before=original['production_before'],
        rules=dict(A='Keep original no-follow. A1: two consecutive complete post-entry15m closes back inside warning extreme, second no stronger, peak<0.5R => exit next observed minute open. A2: peak>=0.5R, five complete observed candles without an extra tick of peak progress, and close back inside warning extreme => exit next observed minute open.',
                   B='Arm only after observed1R. Three consecutive complete post-entry15m candles confirm center strict pivot low/high; center interval must begin at/after arm timestamp. Pivot must improve previous pivot (initial S2); move stop one adverse tick outside pivot, only tighten. Effective after confirming right-bar close.',
                   C='A plus B; original price/holiday/weekend/no-follow exits have priority. All new close decisions fill at next minute open with adverse slip. No fractional lots or new entry filters.',
                   partial='Entry minute and incomplete first15m candle never used for pivot/close pattern; session gaps do not form consecutive pivots.'),
        experiment='Four variants per P2/P3, fixed parameters; no optimization or post-outcome retuning. Both histories already inspected, development evidence only.',
        paired='Same baseline accepted event identities within each period/cost/entry mode; compare common closed and report unknown transitions separately.',
        full='Re-run trigger-time one-symbol capacity with all frozen upstream-service-accepted warnings; unknown reserved to contract end, same-minute reverse-order sensitivity. Repeat A-cluster exposure diagnosed, no new cluster limiter.',
        gates=dict(recent='BOTH costs and entry modes: >=35closed; positive net; >=80% of BOTH original and corrected recent positive baseline; win rate >=corrected baseline; realized DD <=baseline; unknown<=baseline; improve >=2 of product/day/trade concentration; worst10% mean cash loss no worse.',
                   early='BOTH costs and entry modes: improve net AND mean netR and no worse realized DD, unknown and worst10% cash loss.',
                   stable='Defensive gate plus positive cash and mean netR in BOTH periods at both costs/modes. No deployment even if passed; prospective data still required.'),
        hashes={period:{k:sha(v) for k,v in paths.items()} for period,paths in original['periods'].items()},
        production_changed=False,qualified_for_trading=False)
    write(RUN/'protocol.json',p)
    write(RUN/'implementation-manifest.json',dict(fork={str(f.relative_to(root)):sha(f) for f in root.rglob('*') if f.is_file()},
        source={str(f.relative_to(REPO)):sha(f) for f in [pathlib.Path(__file__),REPO/'scripts/swing_adaptive_exit.rs',REPO/'scripts/swing_combination_runner.rs',REPO/'tests/swing_combination.rs']}))
    print('Frozen combination protocol',RUN,flush=True)


def run():
    root=RUN/'fork';args=['--offline','--manifest-path',str(root/'Cargo.toml'),'--target-dir',str(REPO/'target'),'-p','n-core']
    subprocess.run(['cargo','test',*args,'--test','swing_combination'],check=True)
    subprocess.run(['cargo','build',*args,'--bin','swing_combination_runner'],check=True)
    p=read(RUN/'protocol.json')
    for period in ('recent','early'):
        assert all(sha(v)==p['hashes'][period][k] for k,v in p['periods'][period].items())
        subprocess.run([str(REPO/'target/debug/swing_combination_runner.exe'),str(RUN/'protocol.json'),period,str(RUN/(period+'-pool.json')),str(RUN/period)],check=True)


def describe(rows,events):
    out=metrics(rows);closed=[r for r in rows if ev(r)['status']=='closed'];values=sorted(ev(r)['net_amount'] for r in closed)
    rs=sorted(ev(r)['net_r'] for r in closed);n=max(1,math.ceil(len(closed)*.1))
    filled=[r for r in rows if ev(r)['entry_ts'] is not None];groups=collections.defaultdict(list)
    for r in filled:groups[cluster(events[r['event_key']])].append(r)
    out.update(tail10_cash=statistics.mean(values[:n]) if values else None,
        tail10_r=statistics.mean(rs[:n]) if rs else None,accepted_attempts=len(rows),filled=len(filled),
        filled_clusters=len(groups),repeat_fills=sum(max(0,len(v)-1) for v in groups.values()),
        repeated_cluster_net=sum(ev(r)['net_amount'] for v in groups.values() if len(v)>1 for r in v if ev(r)['status']=='closed'),
        exit_reasons=dict(collections.Counter(ev(r)['exit_reason'] for r in closed)),
        status_counts=dict(collections.Counter(ev(r)['status'] for r in rows)))
    return out


def compare_gate(m,b,original=None,recent=True):
    tail=m['tail10_cash'] is not None and b['tail10_cash'] is not None and m['tail10_cash']>=b['tail10_cash']-1e-9
    common=dict(drawdown=m['realized_cash_drawdown']<=b['realized_cash_drawdown']+1e-9,
                unknown=m['unknown']<=b['unknown'],tail10=tail)
    if recent:
        return dict(common,minimum_trades=m['n']>=35,positive=m['net']>0,
            retention_corrected=m['net']>=.8*b['net']-1e-9,
            retention_original=m['net']>=.8*original['net']-1e-9,
            win_rate=(m['win_rate'] or 0)>=(b['win_rate'] or 0)-1e-12,
            concentration=sum(m[k]<b[k]-1e-9 for k in ('top3_product','top3_day','top5_trade'))>=2)
    return dict(common,net_improved=m['net']>b['net']+1e-9,
                mean_r_improved=m['mean_r'] is not None and m['mean_r']>b['mean_r']+1e-9)


def paired_summary(base_rows,mapping):
    pairs=[(r,mapping[r['event_key']]) for r in base_rows]
    transitions=collections.Counter(ev(a)['status']+' -> '+ev(b)['status'] for a,b in pairs)
    common=[(a,b) for a,b in pairs if ev(a)['status']==ev(b)['status']=='closed']
    delta=sum(ev(b)['net_amount']-ev(a)['net_amount'] for a,b in common)
    return dict(cohort=len(pairs),common_closed=len(common),delta_common_closed=delta,
        improved=sum(ev(b)['net_amount']>ev(a)['net_amount']+1e-9 for a,b in common),
        worsened=sum(ev(b)['net_amount']<ev(a)['net_amount']-1e-9 for a,b in common),
        unchanged=sum(abs(ev(b)['net_amount']-ev(a)['net_amount'])<=1e-9 for a,b in common),
        transitions=dict(transitions))


def weekly_delta(base_rows,rows):
    def week(t):
        d=dt.date.fromisoformat(t[:10]);return (d-dt.timedelta(days=d.weekday())).isoformat()
    bins=collections.defaultdict(float)
    for sign,zz in ((-1,base_rows),(1,rows)):
        for r in zz:
            if ev(r)['status']=='closed':bins[week(r['warning_ts'])]+=sign*ev(r)['net_amount']
    values=list(bins.values());rng=random.Random(20261025);samples=[]
    for _ in range(2000):samples.append(sum(rng.choice(values) for _ in values))
    samples.sort()
    return dict(weeks=len(values),delta95=[samples[49],samples[1949]],
        note='Descriptive warning-week cluster resampling, not independent validation or multiple-search-corrected significance')


def report():
    p=read(RUN/'protocol.json');fees=read(p['periods']['recent']['fees']);old_positive=read(OLD/'development-results.json')
    oldaccepted=collections.defaultdict(dict)
    for line in (SOURCE/'accepted.jsonl').open(encoding='utf-8'):
        z=json.loads(line)
        if z['case'].split('/')[0] in ('P2','P3') and len(z['case'].split('/'))==3:
            oldaccepted[(z['period'],z['case'])][z['row']['event_key']]=z['row']
    result={};accepted={};fixed={};paired={};ledgers={};checks={};leave={}
    for period in ('recent','early'):
        source=read(p['periods'][period]['input']);cal=TradingDates(source['symbols'])
        events={r['event_key']:r['event'] for r in read(RUN/(period+'-pool.json'))}
        ends=read(RUN/period/'manifest.json')['contract_ends'];cases=collections.defaultdict(dict)
        for line in (RUN/period/'evaluations.jsonl').open(encoding='utf-8'):
            r=reprice(json.loads(line),fees,cal)
            if ev(r)['status']=='closed':r['exit_trading_day']=cal.day(ev(r)['exit_ts'])
            assert r['event_key'] not in cases[r['scenario']]
            cases[r['scenario']][r['event_key']]=r
        assert len(cases)==32
        result[period]={};accepted[period]={};fixed[period]={};paired[period]={};ledgers[period]={};leave[period]={};regression=0;entry_checks=0
        for case,mapping in cases.items():
            rows,ledger=portfolio(list(mapping.values()),events,ends)
            result[period][case]=describe(rows,events);accepted[period][case]=rows;ledgers[period][case]=ledger
            reversed_rows,_=portfolio(list(mapping.values()),events,ends,reverse=True)
            result[period][case]['reverse_tie_net']=describe(reversed_rows,events)['net']
        for base in ('P2','P3'):
            for mode in p['modes']:
                for slip in (1,2):
                    suffix=f'/{mode}/slip{slip}';control=base+'_base'+suffix;prior=oldaccepted[(period,base+suffix)]
                    now={r['event_key']:r for r in accepted[period][control]}
                    assert now.keys()==prior.keys(),('baseline population',period,control,len(now),len(prior))
                    for k,row in now.items():
                        assert row['result']==prior[k]['result'],('baseline result',period,control,k)
                        assert row.get('cost_repricing')==prior[k].get('cost_repricing'),('baseline fees',period,control,k)
                        regression+=1
                    for variant in ('base','A','B','C'):
                        case=base+'_'+variant+suffix;mapping=cases[case];cohort=[mapping[k] for k in now]
                        for k,r in now.items():
                            assert all(ev(r)[f]==ev(mapping[k])[f] for f in ('entry_ts','fill','risk')),('entry changed',period,case,k)
                            entry_checks+=1
                        fixed[period][case]=describe(cohort,events);paired[period][case]=paired_summary(list(now.values()),mapping)
                        if variant!='base':
                            result[period][case]['weekly_delta']=weekly_delta(list(now.values()),accepted[period][case])
                        leave[period][case]={s:metrics([r for r in accepted[period][case] if r['symbol']!=s])
                            for s in sorted(source['symbols'])}
        # Pin exact monthly observations at every accepted fill and verify cash.
        contracts=read(p['periods'][period]['contracts']);needed=collections.defaultdict(set)
        for rows in accepted[period].values():
            for r in rows:needed[r['execution_contract']].update(f['ts'] for f in r['result']['fills'])
        index={code:{b['datetime']:b for b in contracts[code]['minutes'] if b['datetime'] in times} for code,times in needed.items()};del contracts
        fills_checked=cash_checked=zero_unknown=0
        for case,rows in accepted[period].items():
            for r in rows:
                e=ev(r);sign=1 if r['direction']=='up' else -1;zero_unknown+=r['zero_volume_execution_unknown']
                for fill in r['result']['fills']:
                    assert fill['ts']>r['warning_ts'];b=index[r['execution_contract']][fill['ts']]
                    assert b['volume']>0 or r['zero_volume_execution_unknown']
                    assert abs(fill['price']/r['tick_size']-round(fill['price']/r['tick_size']))<1e-6
                    fills_checked+=1
                if e['status']=='closed':
                    fs=r['result']['fills'];assert len(fs)==2 and fs[0]['volume']==fs[1]['volume']==1
                    gross=(fs[1]['price']-fs[0]['price'])*sign*r['multiplier']
                    assert abs(gross-r['cost_repricing']['commission']-e['net_amount'])<1e-6
                    assert abs(e['net_r']-e['net_amount']/(e['risk']*r['multiplier']))<1e-9
                    cash_checked+=1
        checks[period]=dict(baseline_exact_results_and_fees=regression,identical_entry_checks=entry_checks,
            actual_contract_fill_checks=fills_checked,closed_cash_checks=cash_checked,zero_volume_unknown=zero_unknown,
            raw_evaluations=read(RUN/period/'manifest.json')['evaluations'])
    gates={};defensive=[];stable=[]
    for base in ('P2','P3'):
        for variant in ('A','B','C'):
            name=base+'_'+variant;detail={}
            for period in ('recent','early'):
                detail[period]={}
                for mode in p['modes']:
                    for slip in (1,2):
                        suffix=f'/{mode}/slip{slip}';m=result[period][name+suffix];b=result[period][base+'_base'+suffix]
                        detail[period][mode+f'/slip{slip}']=compare_gate(m,b,old_positive[base][str(slip)]['all'],period=='recent')
            passed=all(all(v.values()) for pp in detail.values() for v in pp.values())
            positive=all(result[period][name+f'/{mode}/slip{slip}']['net']>0 and result[period][name+f'/{mode}/slip{slip}']['mean_r']>0
                for period in ('recent','early') for mode in p['modes'] for slip in (1,2))
            gates[name]=dict(defensive_pass=passed,stable_historical_pass=passed and positive,checks=detail)
            if passed:defensive.append(name)
            if passed and positive:stable.append(name)
    write(RUN/'results.json',result);write(RUN/'fixed-cohort.json',fixed);write(RUN/'paired.json',paired)
    write(RUN/'gates.json',gates);write(RUN/'leave-product-out.json',leave);write(RUN/'opportunity-ledgers.json',ledgers)
    with (RUN/'accepted.jsonl').open('x',encoding='utf-8') as file:
        for period,cases in accepted.items():
            for case,rows in cases.items():
                for row in rows:file.write(json.dumps(dict(period=period,case=case,row=row),ensure_ascii=False)+'\n')
    assert all(sha(f)==h for f,h in p['production_before'].items())
    write(RUN/'verification.json',dict(checks=checks,production_files_unchanged=len(p['production_before']),
          production_changed=False,rust_tests=8,protocol_sha256=sha(RUN/'protocol.json'),results_sha256=sha(RUN/'results.json'),
          final_source_sha256={str(f.relative_to(REPO)):sha(f) for f in [pathlib.Path(__file__),REPO/'scripts/swing_adaptive_exit.rs',REPO/'scripts/swing_combination_runner.rs',REPO/'tests/swing_combination.rs']}))
    write(RUN/'paper-freeze.json',dict(defensive_candidates=defensive,stable_candidates=stable,qualified_for_trading=False,
          protocol_sha256=sha(RUN/'protocol.json'),min_trading_days=60,min_closed_trades=100,
          status='no_candidate' if not defensive else 'paper_only',historical_periods_already_inspected=True))
    render(result,fixed,paired,gates,checks)
    print('Combination report:',RUN/'report.md','defensive:',defensive,'stable:',stable,flush=True)


def render(results,fixed,paired,gates,checks):
    lines=['# 持续退出与1R结构保护：组合验证','',
        '本轮冻结四组：基线、A持续检查启动失败/推进停滞、B达到1R后采用已确认回踩结构保护、C=A+B。P2/P3各自验证，固定一手，22品种，1/2tick每边并扣冻结费用。两个历史时期均已被研究，本轮不属于独立样本验证。','',
        '新增收盘退出在下一可用分钟开盘执行；结构止损在确认右侧完整15分钟线之后生效。入场所在不完整桶不参与结构识别；缺行情、路径歧义和无法成交保留未知。入场沿用触价/下一分钟开盘两种模式，后者是压力情景，不是逐笔盘口复刻。','',
        '## 全机会重放','',
        '金额为机会汇总，回撤为已平仓现金回撤，无保证金/资金限制。收益保留率同时对照原研究正基线及本轮复现的修复基线；不把少做交易导致的敞口下降直接当作净期望改善。','']
    for period,label in [('recent','7—10月'),('early','1—6月')]:
        lines += ['### '+label,'','|规则/入场模式/每边滑点|笔数|净额|平均净R|胜率|已实现回撤|未知|最差10%均额|重复成交次数|',
            '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
        for base in ('P2','P3'):
            for mode in ('corrected_touch','next_minute_open'):
                for slip in (1,2):
                    for variant in ('base','A','B','C'):
                        case=f'{base}_{variant}/{mode}/slip{slip}';m=results[period][case]
                        lines.append(f"|{case}|{m['n']}|{m['net']:.2f}|{m['mean_r']:.3f}|{m['win_rate']:.2%}|{m['realized_cash_drawdown']:.2f}|{m['unknown']}|{m['tail10_cash']:.2f}|{m['repeat_fills']}|")
        lines += ['', '重复成交按相同品种、方向、S0/S1的A段簇统计，含同簇第一笔以外的已确认入场；同簇总收益不等于可以避免的重复交易损失。','']
    lines += ['## 相同入场对照（触价/1tick）','',
        '|时期/规则|共同已平仓|共同样本净额变化|改善/变差/不变|固定入场净额|全机会净额|',
        '|---|---:|---:|---:|---:|---:|']
    for period in ('recent','early'):
        for base in ('P2','P3'):
            for variant in ('A','B','C'):
                case=f'{base}_{variant}/corrected_touch/slip1';z=paired[period][case]
                lines.append(f"|{period}/{base}_{variant}|{z['common_closed']}|{z['delta_common_closed']:.2f}|{z['improved']}/{z['worsened']}/{z['unchanged']}|{fixed[period][case]['net']:.2f}|{results[period][case]['net']:.2f}|")
    lines += ['', '固定入场对照沿用基线已接受事件，未决状态转换见paired.json；不把未知当零。全机会重放则允许退出时机改变后续机会接纳，最终资格以全机会结果判断。','',
              '## 门槛与冻结结论','']
    for name,g in gates.items():
        failed={period:{case:[k for k,v in flags.items() if not v] for case,flags in cases.items() if not all(flags.values())} for period,cases in g['checks'].items()}
        lines += [f"- {name}：防守候选通过={g['defensive_pass']}；两期正期望门槛通过={g['stable_historical_pass']}。",
                  '  未通过项：'+json.dumps(failed,ensure_ascii=False)]
    passed=[name for name,g in gates.items() if g['defensive_pass']]
    lines += ['', '符合全部防守候选门槛：'+('、'.join(passed) if passed else '无。'),'',
        '近期要求：两档成本、两种入场模式均≥35笔、正收益、保留原与修复后正基线至少80%、胜率不下降、回撤/未知/最差10%均损不恶化、三项集中度至少两项改善。上半年要求净额与平均净R均改善，回撤/未知/尾部不恶化。两期正期望另列，不能只因减少亏损就称为稳定盈利。','',
        '## 规则细节','',
        'A1：峰值不足0.5R，两根连续且完整的持仓后15m线收盘回到原预警极值内，第二根没有方向性改善，下一分钟退出。A2：已有至少0.5R峰值，连续5根完整15m线没有再推进至少1tick，收盘回到预警极值内，下一分钟退出。保留原第5桶无跟随退出。','',
        'B：达到过1R后，用三根连续、完整、持仓后15m线确认中间严格回踩低点/反抽高点；中间线的开始时间不得早于1R被观察到的时刻。新结构点需优于前一个结构点（初始为S2），止损放在其外侧1tick，只收紧。当前确认线收盘后才生效。','',
        'C叠加A/B，不新增入场、评分或品种过滤；原价位/周末/长假/时间退出优先。新收盘退出若没有下一分钟成交证据则记未知，不补一个信号收盘成交。','',
        '## 核验与复现','',
        json.dumps(checks,ensure_ascii=False,indent=2),'',
        '基线全部已接受身份、成交、评估与费用逐笔复现。8项Rust边界测试覆盖对称性、确认时点、部分桶、停滞重置、下一分钟退出和跳空穿越保护止损。implementation-manifest.json保存结果前源码，verification.json保存最终脚本哈希；补充测试未改变交易规则。','',
        '完整结果见results.json；配对见paired.json和fixed-cohort.json；留品种、同分钟反序、逐周描述性差额区间均保留。两期已被反复研究，描述性区间不代表校正后的显著性；新候选仍需冻结后的纸面行情与实际报价档案。','',
        '复现顺序：prepare → run → report；各批次禁止覆盖，另建批次需修改脚本RUN路径。实盘代码、参数和已有研究批次未改动。']
    (RUN/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def complete():
    """Payoff attribution of the frozen entries; does not select new parameters."""
    p=read(RUN/'protocol.json');results=read(RUN/'results.json');fixed=read(RUN/'fixed-cohort.json')
    fees=read(p['periods']['recent']['fees']);baseline=collections.defaultdict(dict);attribution={}
    for line in (SOURCE/'accepted.jsonl').open(encoding='utf-8'):
        z=json.loads(line)
        if z['case'] in ('P2/corrected_touch/slip1','P3/corrected_touch/slip1'):
            baseline[(z['period'],z['case'][:2])][z['row']['event_key']]=z['row']
    for period in ('recent','early'):
        source=read(p['periods'][period]['input']);cal=TradingDates(source['symbols']);mapping=collections.defaultdict(dict)
        for line in (RUN/period/'evaluations.jsonl').open(encoding='utf-8'):
            row=json.loads(line)
            if row['scenario'].endswith('/corrected_touch/slip1'):
                mapping[row['scenario']][row['event_key']]=reprice(row,fees,cal)
        attribution[period]={}
        for base in ('P2','P3'):
            for variant in ('A','B','C'):
                case=f'{base}_{variant}/corrected_touch/slip1';groups=collections.defaultdict(lambda:dict(n=0,baseline_net=0.,new_net=0.,delta=0.))
                for key,old in baseline[(period,base)].items():
                    new=mapping[case][key]
                    if ev(old)['status']!='closed' or ev(new)['status']!='closed':continue
                    group='baseline_winners' if ev(old)['net_amount']>0 else 'baseline_losers' if ev(old)['net_amount']<0 else 'baseline_flat'
                    g=groups[group];g['n']+=1;g['baseline_net']+=ev(old)['net_amount'];g['new_net']+=ev(new)['net_amount'];g['delta']+=ev(new)['net_amount']-ev(old)['net_amount']
                attribution[period][case]=dict(groups,additional_opportunity_net=results[period][case]['net']-fixed[period][case]['net'])
    attribution_path=RUN/'payoff-attribution.json'
    if attribution_path.exists():assert read(attribution_path)==attribution
    else:write(attribution_path,attribution)
    lines=['## 本轮结论','',
        'A、B及C组合均未通过冻结门槛；没有候选进入实盘。即使仅对照修复后基线，近期收益保留率也全部低于80%。以下为触价入场、每边1tick并扣费的完整机会重放，单位元。','',
        '|方案|近期P2净额/保留率|上半年P2净额|近期P3净额/保留率|上半年P3净额|','|---|---:|---:|---:|---:|']
    for variant,label in [('base','基线'),('A','A 持续检查'),('B','B 1R结构保护'),('C','C 两者组合')]:
        vals=[]
        for base in ('P2','P3'):
            case=f'{base}_{variant}/corrected_touch/slip1';m=results['recent'][case];b=results['recent'][f'{base}_base/corrected_touch/slip1']
            vals.extend([f"{m['net']:.2f}/{m['net']/b['net']:.1%}",f"{results['early'][case]['net']:.2f}"])
        lines.append('|'+label+'|'+'|'.join(vals)+'|')
    lines += ['', '主要失败机制：','']
    for base in ('P2','P3'):
        case=f'{base}_B/corrected_touch/slip1';a=attribution['recent'][case]
        lines.append(f"- {base}的B组，在固定近期入场对照中：原盈利单净额变化{a.get('baseline_winners',{}).get('delta',0):.2f}元，原亏损单净额变化{a.get('baseline_losers',{}).get('delta',0):.2f}元。结构保护节省损失的同时，也提前结束了随后能够盈利的持仓。")
    for base in ('P2','P3'):
        case=f'{base}_C/corrected_touch/slip1';baseline_net=results['early'][f'{base}_base/corrected_touch/slip1']['net'];x=fixed['early'][case]['net']
        lines.append(f"- {base}的C组，上半年固定入场净额改善{x-baseline_net:.2f}元；释放机会后额外净额变化{attribution['early'][case]['additional_opportunity_net']:.2f}元，最终改善{results['early'][case]['net']-baseline_net:.2f}元。")
    lines += ['', 'C组在下一分钟入场、每边2tick的近期压力情景中，P2净额'+f"{results['recent']['P2_C/next_minute_open/slip2']['net']:.2f}元、P3净额{results['recent']['P3_C/next_minute_open/slip2']['net']:.2f}元。",'',
        '这些结果检验了本轮冻结的具体规则：简单的启动失败/停滞加三根K线结构保护，尚不能可靠地区分应保留的正常回踩和应退出的失效。减少尾部亏损、提高部分胜率，不等于改善完整策略；也不能从这轮失败推断所有动态退出必然无效。','']
    report=RUN/'report.md';text=report.read_text(encoding='utf-8')
    if '## 本轮结论' not in text:
        report.write_text(text.split('\n',1)[0]+'\n\n'+'\n'.join(lines)+text.split('\n',1)[1],encoding='utf-8')
    manifest=read(RUN/'implementation-manifest.json');root=RUN/'fork'
    frozen_engine_files=['crates/n-core/src/v3/ablation.rs','crates/n-core/src/v3/ablation/swing_adaptive.rs','crates/n-core/src/bin/swing_combination_runner.rs']
    frozen_hashes={k.replace('\\','/'):v for k,v in manifest['fork'].items()}
    assert all(sha(root/path)==frozen_hashes[path] for path in frozen_engine_files)
    assert definitions()==p['templates']
    assert all(sha(path)==h for path,h in p['production_before'].items())
    write(RUN/'completion.json',dict(report_sha256=sha(report),protocol_sha256=sha(RUN/'protocol.json'),
        frozen_engine_files_unchanged=frozen_engine_files,rule_definitions_unchanged=True,
        production_files_unchanged=len(p['production_before']),production_changed=False,rust_tests=8,
        source_sha256=sha(pathlib.Path(__file__)),qualified_for_trading=False))
    print('Completed combination attribution and frozen-rule audit',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('stage',choices=['prepare','run','report','complete'])
    globals()[parser.parse_args().stage]()
