"""Audit latest real fills and compare frozen candidates on disclosed signal cohorts."""
import argparse
import collections
import csv
import datetime as dt
import json
import math
import pathlib

from report_signal_ablation import jsonl, read_json
from reprice_signal_costs import TradingDates, reprice
from validate_execution_research import capacity, evaluation
from report_paper_candidate_history import summary, grouped, csv_write, digest, week, fmt, pct, table, UNKNOWN

ROOT=pathlib.Path(__file__).resolve().parents[1]
STUDY='server-execution-20261023'
PREVIOUS_CUTOFF='2026-10-22 11:25:00'


def check_ledgers(rows):
    for r in rows:
        e=evaluation(r)
        if e['status']!='closed':continue
        fills=r['result']['fills'];sign=1 if r['direction']=='up' else -1
        gross=math.fsum(sign*(f['price']-fills[0]['price'])*f['volume']*r['multiplier'] for f in fills[1:])
        fees=math.fsum(f['fee'] for f in r['cost_repricing']['fee_legs'])
        if abs(gross-fees-e['net_amount'])>1e-7 or sum(f['volume'] for f in fills[1:])!=fills[0]['volume']:
            raise ValueError('Fill accounting mismatch')


def load_cases(candidate_dir,legacy_dir,source,profile,spec):
    names={c['research_name']:p for p,c in spec['candidates'].items()}
    manifest=read_json(candidate_dir/'manifest.json');cal=TradingDates(source['symbols'])
    for p,c in spec['candidates'].items():
        actual=next(d for d in manifest['definitions'] if d['name']==c['research_name'])
        if actual!=c['definition']:raise ValueError('Candidate definition drift: '+p)
    cases=collections.defaultdict(list)
    for path in [candidate_dir/'evaluations.jsonl',legacy_dir/'evaluations.jsonl']:
        for r in jsonl(path):
            name,slip=r['scenario'].split('/')
            if slip not in ('slip1','slip2') or name not in names and name!='LEGACY':continue
            strategy=names.get(name,'LEGACY');r=reprice(r,profile,cal)
            if evaluation(r)['status']=='closed':r['exit_trading_day']=cal.day(evaluation(r)['exit_ts'])
            cases[(strategy,int(slip[-1]))].append(r)
    for rows in cases.values():
        if len({r['event_key'] for r in rows})!=len(rows):raise ValueError('Duplicate source key')
    return cases


def cohort(cases,source,predicate=lambda r:True):
    out={};admitted_rows={};audits=[]
    for (strategy,slip),raw in sorted(cases.items()):
        rows=[r for r in raw if predicate(r)]
        cap=capacity(rows,source['symbols'],'entry',1)
        take={r['event_key'] for r in cap['ledger'] if r['accepted']}
        accepted=[r for r in rows if r['event_key'] in take]
        check_ledgers(accepted)
        s=summary(accepted)
        info=dict(strategy=strategy,slip=slip,input_signals=len(rows),admitted=len(accepted),
                  blocked=len(cap['ledger'])-len(accepted),unknown=sum(evaluation(r)['status'] in UNKNOWN for r in accepted),
                  status_counts=dict(collections.Counter(evaluation(r)['status'] for r in rows)),
                  admitted_statuses=dict(collections.Counter(evaluation(r)['status'] for r in accepted)),summary=s,
                  products=grouped(accepted,lambda r:r['symbol'],source['symbols']),
                  warning_month=grouped(accepted,lambda r:r['warning_ts'][:7]),
                  exit_month=grouped(accepted,lambda r:r['exit_trading_day'][:7] if evaluation(r)['status']=='closed' else 'unknown'),
                  exit_week=grouped(accepted,lambda r:week(r['exit_trading_day']) if evaluation(r)['status']=='closed' else 'unknown'))
        for label in ('products','warning_month','exit_month','exit_week'):
            if sum(v['n'] for v in info[label].values())!=s['n'] or abs(math.fsum(v['net'] for v in info[label].values())-s['net'])>1e-7:
                raise ValueError('Group totals mismatch')
        name=f'{strategy}/slip{slip}';out[name]=info;admitted_rows[name]=accepted
        for ledger in cap['ledger']:audits.append(dict(strategy=strategy,slip=slip,**ledger))
    return out,admitted_rows,audits


def run(output):
    output=pathlib.Path(output)
    if output.exists():raise FileExistsError('Use a new report directory')
    runs=ROOT/'research-runs';archive=ROOT/'research-archives'/f'{STUDY}-v1'
    source=read_json(runs/f'{STUDY}-input-v1.json')
    audit_dir=runs/f'{STUDY}-audit-v1';audit=read_json(audit_dir/'audit-summary.json')
    profile=read_json(ROOT/'scripts/exchange_fees_20261022.json')
    spec=read_json(ROOT/'research-protocols/paper-candidates-spec-20261023-v1.json')
    full=load_cases(runs/f'{STUDY}-full-candidates-v1',runs/f'{STUDY}-full-legacy-v2',source,profile,spec)
    live=load_cases(runs/f'{STUDY}-live-candidates-v1',runs/f'{STUDY}-live-legacy-v2',source,profile,spec)
    actual=read_json(audit_dir/'actual-as-rows.json');actual_keys={r['event_key'] for r in actual}
    lineage=read_json(audit_dir/'lineage.json');by_key={r['event_key']:r for r in lineage}
    state=read_json(archive/'trader_execution.json')
    filled_keys={r['event_key'] for r in lineage if r['condition_id'] in state['positions']}
    begin=min(r['warning_ts'] for r in lineage)
    definitions=[('live_all_original',live,lambda r:True),
                 ('actual_closed_original',live,lambda r:r['event_key'] in actual_keys),
                 ('actual_filled_original',live,lambda r:r['event_key'] in filled_keys),
                 ('current_detector_live_period',full,lambda r:r['warning_ts']>=begin),
                 ('full_history_current_detector',full,lambda r:True)]
    result=dict(audit=audit,actual_summary=summary(actual),
                actual_products=grouped(actual,lambda r:r['symbol'],source['symbols']),
                actual_exit_day=grouped(actual,lambda r:r['exit_trading_day']),
                cohorts={},previous_cutoff=PREVIOUS_CUTOFF,production_changed=False,
                execution_counterfactual='normal signal execution under frozen OHLC assumptions; actual economic losses are retained',
                account_cap=None,fees='frozen fee profile estimates, not actual broker settlement bills')
    accepted_all={};capacity_audit=[];summaries=[];products=[];periods=[]
    for label,cases,pred in definitions:
        stats,accepted,ledger=cohort(cases,source,pred)
        result['cohorts'][label]=stats;accepted_all[label]=accepted
        capacity_audit.extend(dict(cohort=label,**r) for r in ledger)
        for name,v in stats.items():
            summaries.append(dict(cohort=label,strategy=v['strategy'],slip=v['slip'],unknown=v['unknown'],blocked=v['blocked'],**v['summary']))
            for symbol,s in v['products'].items():products.append(dict(cohort=label,strategy=v['strategy'],slip=v['slip'],symbol=symbol,**s))
            for period in ('warning_month','exit_month','exit_week'):
                for name,s in v[period].items():periods.append(dict(cohort=label,strategy=v['strategy'],slip=v['slip'],period=period,group=name,**s))
    # New-warning contribution retains full-history slot continuity.
    result['new_warnings_after_previous_cutoff']={name:summary([r for r in rows if r['warning_ts']>PREVIOUS_CUTOFF])
        for name,rows in accepted_all['full_history_current_detector'].items()}
    # Availability sensitivity: no retries. Mark triggers before actual signal availability as rejected,
    # then recompute slots. Never relabel this sensitivity as ideal or achievable broker execution.
    available_cases={}
    for lane,rows in live.items():
        changed=[]
        for row in rows:
            r=json.loads(json.dumps(row));e=evaluation(r)
            if e.get('entry_ts'):
                bar_start=(dt.datetime.fromisoformat(e['entry_ts'])-dt.timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S')
                if bar_start<by_key[r['event_key']]['safe_available_at']:
                    e.update(status='unavailable_at_counterfactual_entry',entry_ts=None,exit_ts=None,net_amount=None,net_r=None)
            changed.append(r)
        available_cases[lane]=changed
    avail_stats,_,_=cohort(available_cases,source)
    result['availability_no_retry_sensitivity']=avail_stats
    # Explicit correction/replay ledger, including rejected and unknown signals.
    paired=[]
    actual_by={r['event_key']:r for r in actual}
    raw_live={name:{r['event_key']:r for r in rows} for name,rows in live.items()}
    accepted_closed={name:{r['event_key']:r for r in rows} for name,rows in accepted_all['actual_closed_original'].items()}
    with (audit_dir/'actual-trades-corrected.csv').open(encoding='utf-8-sig') as f:
        actual_detail={r['event_key']:r for r in csv.DictReader(f)}
    for event_key,a in sorted(actual_by.items()):
        e=evaluation(a);detail=actual_detail[event_key]
        for strategy in ('LEGACY','P0','P1','P2','P3'):
            for slip in (1,2):
                r=raw_live[(strategy,slip)].get(event_key)
                if r is None:raise ValueError('Original live signal missing from replay')
                x=evaluation(r);take=event_key in accepted_closed[f'{strategy}/slip{slip}']
                status='blocked_symbol_slot' if not take and (x.get('entry_ts') or x['status'] in UNKNOWN) else x['status']
                net=x['net_amount'] if take and status=='closed' else None
                paired.append(dict(condition_id=by_key[event_key]['condition_id'],event_key=event_key,symbol=a['symbol'],
                    strategy=strategy,slip=slip,actual_net_estimate=e['net_amount'],actual_gross=detail['gross'],
                    actual_flags=detail['flags'],replay_status=status,replay_entry=x.get('entry_ts'),replay_exit=x.get('exit_ts'),
                    replay_net=net,replay_minus_actual=net-e['net_amount'] if net is not None else None))
    result['execution_exception_examples']=[r for r in paired if r['strategy']=='LEGACY' and r['slip']==1
        and 'documented_exit_reject_or_sync_block' in r['actual_flags']]
    # Reconcile the archived current-day broker trade callbacks against persistent history/order/position.
    broker=read_json(archive/'execution-snapshot.json')['data']['trades'];history={r['id']:r for r in state['history']}
    broker_checked=0
    for t in broker:
        order=state['orders'][t['order_ref']];cid=order['condition_id']
        if t['offset']=='open':
            p=state['positions'][cid]
            if abs(p['fill_price']-t['price'])>1e-7:raise ValueError('Broker open fill differs from position')
        else:
            trade_key=':'.join([t['trading_day'],t['exchange_id'],t['instrument_id'],t['direction'],t['trade_id']])
            h=history[trade_key]
            if abs(h['exit_price']-t['price'])>1e-7 or h['volume']!=t['volume']:raise ValueError('Broker close fill differs from history')
        broker_checked+=1
    # Test the new legacy adapter on every prior frozen opportunity, after the identical fee repricing.
    prev=read_json(runs/'execution-validation-20261022-v2/summary.json')
    old_input=pathlib.Path(next(p for p in prev['source_hashes'] if p.endswith('-input.json')))
    old_source=read_json(old_input);oldcal=TradingDates(old_source['symbols'])
    control={(r['scenario'],r['event_key']):reprice(r,profile,oldcal) for r in jsonl(runs/f'{STUDY}-legacy-regression-v2/evaluations.jsonl')}
    old_rows=pathlib.Path(next(p for p in prev['source_hashes'] if p.endswith('evaluations.jsonl')))
    exact=0
    for r in jsonl(old_rows):
        if r['scenario'] not in ('actual_tier/nf5/slip1_fee','actual_tier/nf5/slip2_fee'):continue
        lane='LEGACY/slip'+r['scenario'][-5];new=control[(lane,r['event_key'])]
        for field in ('evaluation','fills','disagreement'):
            if new['result'].get(field)!=r['result'].get(field):raise ValueError('Frozen legacy control drift: '+field)
        exact+=1
    old_bars={(s,b['datetime']):b for s,v in old_source['symbols'].items() for b in v['bars']}
    new_bars={(s,b['datetime']):b for s,v in source['symbols'].items() for b in v['bars']}
    result['raw_history_increment']=dict(previous=len(old_bars),current=len(new_bars),added=len(new_bars.keys()-old_bars.keys()),
        removed=len(old_bars.keys()-new_bars.keys()),revised=sum(old_bars[k]!=new_bars[k] for k in old_bars.keys()&new_bars.keys()))
    result['verification']=dict(prior_legacy_rows_exact=exact,current_day_broker_callbacks_exact=broker_checked,
        current_day_broker_callbacks_total=len(broker),actual_closed_conditions_volume_reconciled=len(actual),
        grouped_totals_reconcile=True,all_candidate_definitions_equal_frozen_spec=True)
    paths=[ROOT/'scripts/report_server_strategy_comparison.py',ROOT/'scripts/exchange_fees_20261022.json',
           ROOT/'crates/n-core/src/bin/signal_execution_audit.rs',audit_dir/'live-signals.json',archive/'collection-manifest.json']
    for scope in ['full','live']:
        for typ in ['candidates','legacy']:
            version='v2' if typ=='legacy' else 'v1'
            manifest_path=runs/f'{STUDY}-{scope}-{typ}-{version}/manifest.json'
            if typ=='legacy' and read_json(manifest_path)['runner_sha256']!=digest(ROOT/'crates/n-core/src/bin/signal_execution_audit.rs'):
                raise ValueError('Legacy adapter source fingerprint mismatch')
            paths.extend([manifest_path,runs/f'{STUDY}-{scope}-{typ}-{version}/evaluations.jsonl'])
    result['source_hashes']={str(p):digest(p) for p in paths}
    output.mkdir(parents=True)
    csv_write(output/'strategy-summary.csv',summaries)
    csv_write(output/'products.csv',products);csv_write(output/'periods.csv',periods);csv_write(output/'paired-actual-vs-replay.csv',paired)
    with (output/'accepted-replays.jsonl').open('w',encoding='utf-8') as f:
        for label,lanes in accepted_all.items():
            for name,rows in lanes.items():
                for r in rows:f.write(json.dumps(dict(cohort=label,lane=name,**r),ensure_ascii=False,allow_nan=False)+'\n')
    with (output/'capacity-audit.jsonl').open('w',encoding='utf-8') as f:
        for r in capacity_audit:f.write(json.dumps(r,ensure_ascii=False,allow_nan=False)+'\n')
    (output/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    write_report(result,output)
    print(json.dumps(dict(actual=result['actual_summary'],
        main={k:dict(n=v['summary']['n'],win_rate=v['summary']['win_rate'],net=v['summary']['net'],unknown=v['unknown'])
              for k,v in result['cohorts']['live_all_original'].items()},verification=result['verification']),ensure_ascii=True,indent=2))


def write_report(result,output):
    audit=result['audit'];actual=result['actual_summary'];names=['LEGACY','P0','P1','P2','P3']
    def case(scope,p,slip=1):return result['cohorts'][scope][f'{p}/slip{slip}']
    def compact(scope):
        return table(['策略','1 tick平仓','胜率','净盈亏元','现金PF','平均净R','2 tick净盈亏元','未知1/2 tick'],[
            [p,(s:=case(scope,p)['summary'])['n'],pct(s['win_rate']),fmt(s['net']),fmt(s['cash_pf']),f"{s['mean_r']:.4f}" if s['mean_r'] is not None else '无样本',
             fmt(case(scope,p,2)['summary']['net']),f"{case(scope,p)['unknown']}/{case(scope,p,2)['unknown']}"] for p in names])
    lines=['# 最新服务器成交审计与候选策略重放（2026-10-23）','',
        f"归档时间：{audit['collection']['started_at']}～{audit['collection']['finished_at']}。行情截至 **2026-10-23 11:05:00**，83,483根原始5分钟K，22个品种。服务器持久化保存189条条件单、4,169条委托、83个有成交条件单，其中79笔已完全平仓、4笔仍持仓；88条平仓腿不是88笔独立交易。实际开仓记录从10月16日21:42开始。",'',
        '仅在服务器临时目录生成一次性数据库副本；认证设置从副本删除并VACUUM后下载。75个归档文件校验SHA256；未改服务器策略、账户、委托或原始交易记录。成交API与持久化history一致。委托分页API在部署版本返回404，全部4,169条委托已从持久化状态归档，不依赖该API。', '',
        '## 先修正可核实的记账问题','',
        f"早期10条平仓腿缺少event_id/评分/预警时刻等字段，已由相同condition_id回连原条件单，全部189条条件单均能追溯数据库中的原始信号。9条腿的金额字段不等于成交价差×手数×乘数（另1条本来就是零收益）；原金额字段合计{fmt(audit['stored_gross_amount'])}元，按全部成交重算后**毛盈亏{fmt(audit['corrected_gross'])}元**。这里保留实际成交价格，没有把未按规则退出的亏损改成理想盈利。",'',
        f"再用与上轮相同的冻结费率逐腿估算手续费{fmt(audit['estimated_fees'])}元，得到**实际已平仓估算净额{fmt(audit['estimated_net'])}元**；79笔按信号归并后的净胜率{pct(actual['win_rate'])}。手续费不是券商结算单，4笔当前持仓不按零盈亏结算；服务器当前连接的broker_id为9999（SimNow），本文的“实际成交”指该服务器账户收到的成交回报。",'',
        '## 哪些是执行异常，哪些不是','',
        '- 已平仓的11笔出现平仓拒单/持仓同步阻塞，属于有委托证据的执行异常；全部原损益保留。',
        '- 记录中还有1笔在标记的15分钟预警结束前开仓、2笔超过候选策略追价上限、4笔止损成交比结构止损不利超过2 tick。后两类是规则偏离/滑点诊断，本身不证明服务器故障。',
        '- 3,951次拒单为资金不足；另有24次状态禁止、10次请求超过许可数、11次持仓同步等待。拒单次数不是独立信号数，也不能把资金不足归为网络故障。',
        '- 对照旧条件单目标与原始S1，64条曾被旧规则预设到1R；P0～P3重放恢复原S1，避免把旧规则加工过的目标当作上游信号。',
        '- 不删除某个亏损或收益日期来“修好”策略。重放对全部信号一致执行入场、止损、目标、无跟随、时间和休市规则。', '',
        '### 有平仓异常委托证据的11笔逐笔对照','',
        '实际列使用校正记账后的原始成交净额估算；旧规则重放列在79笔原信号子集上统一执行1 tick假设。两列成交时间/数量可能不同；拒绝或未知不填零，也不把反事实结果回写为实际成交。','']
    lines+=table(['品种','条件单ID前8位','实际估算净额','旧规则重放状态','旧规则重放净额'],[
        [r['symbol'],r['condition_id'][:8],fmt(r['actual_net_estimate']),r['replay_status'],fmt(r['replay_net'])]
        for r in result['execution_exception_examples']])+['',
        '## 189条服务器原始条件信号：完整机会集合','',
        '这是主要比较：包括最后成交、资金拒绝、过期及尚持仓的原始条件信号。各策略各自筛选、独立进行同品种单入场槽位处理。P0～P3固定一手；LEGACY保留冻结两手/一手及旧退出规则。全部暂不设账户资金上限，因此会包含当时实际账户没资金开出的机会。', '']
    lines+=compact('live_all_original')+['',
        '## 79笔实际已平仓所对应的相同原始信号','',
        '此表回答“实际做过的这些信号换策略会怎样”。输入固定为实际79笔的信号，再分别应用每组过滤及机会槽。未满足条件的信号算拒绝，不算亏损；数量不同。此子集受旧策略及资金/异常执行选择影响，不能作为新策略独立验证。实际估算净额见前文，不能把本表的反事实净额叫做修正后的实际账户盈亏。','']
    lines+=compact('actual_closed_original')+['',
        '## 当前冻结识别器：全部最新历史','',
        '新原始行情从7月3日开始前向重建，共3,067条信号，其中2,783条评分≥2.5且风险为正。原有82,149根行情未删除或改写，仅新增1,334根。服务器原始189条条件信号与当前完整K线识别器只有181条身份匹配、176条几何参数匹配，故上面两表保留原信号，没有用新检测器结果冒充当时信号。', '']
    lines+=compact('full_history_current_detector')+['',
        '### 上轮截止时刻之后的新预警贡献','',
        '仅取预警时刻晚于2026-10-22 11:25的已接纳机会，保持完整历史的槽位连续性。旧预警后续平仓不计入此表；样本非常小，不是全新的独立验证集。','']
    lines+=table(['策略','1 tick已平仓','1 tick净额','2 tick已平仓','2 tick净额'],[
        [p,(a:=result['new_warnings_after_previous_cutoff'][f'{p}/slip1'])['n'],fmt(a['net']),
         (b:=result['new_warnings_after_previous_cutoff'][f'{p}/slip2'])['n'],fmt(b['net'])] for p in names])+['',
        '## 189条原信号的分品种与时间分布','',
        '每侧1 tick；单元格=平仓笔数 / 净胜率 / 净盈亏元。无平仓标无样本。少于10笔只作描述，不足以推断品种优势。','']
    lines+=table(['品种']+names,[[symbol]+[
        f"{(s:=case('live_all_original',p)['products'][symbol])['n']} / {pct(s['win_rate'])} / {fmt(s['net'])}" if case('live_all_original',p)['products'][symbol]['n'] else '无样本'
        for p in names] for symbol in result['actual_products']])+['',
        '### 按预警月归属','']
    month_keys=sorted({m for p in names for m in case('live_all_original',p)['warning_month']})
    lines+=table(['月份']+names,[[m]+[fmt(case('live_all_original',p)['warning_month'].get(m,{}).get('net')) for p in names] for m in month_keys])+['',
        '### 按退出交易周归属','']
    weeks=sorted({w for p in names for w in case('live_all_original',p)['exit_week'] if w!='unknown'})
    lines+=table(['周一日期']+names,[[w]+[fmt(case('live_all_original',p)['exit_week'].get(w,{}).get('net')) for p in names] for w in weeks])+['',
        '### 单笔盈亏分布','']
    lines+=table(['策略','平均盈利','平均亏损','中位数','最好单笔','最差单笔','已实现现金回撤'],[
        [p]+[fmt(case('live_all_original',p)['summary'][f]) for f in ('mean_win','mean_loss','median','best','worst','realized_cash_drawdown')] for p in names])+['',
        '## 信号可用时间和反事实边界','',
        '主表是“服务器正常、按信号规定执行”的research-v5反事实：使用冻结原始价格，从预警收盘之后回放。它纠正的是执行偏离的比较口径，不保证当时停机期间真的能按该价成交。原条件单的created_at、预警时间、实际入场时间都保留在signal-lineage.csv。', '',
        '额外做保守可用时间敏感性：只有拟入场5分钟区间起点不早于max(预警收盘,条件单实际创建时刻)才接纳，否则终止该信号、不重试，再重算品种槽位。这个限制刻意丢弃区间内才收到信号的机会，不是完整延迟/盘口重放。', '']
    lines+=table(['策略','1 tick平仓','胜率','净额','因当时尚不可用拒绝数'],[
        [p,(v:=result['availability_no_retry_sensitivity'][f'{p}/slip1'])['summary']['n'],pct(v['summary']['win_rate']),fmt(v['summary']['net']),v['status_counts'].get('unavailable_at_counterfactual_entry',0)] for p in names])+['',
        '只有5分钟连续合约行情，没有逐笔历史盘口与完整历史券商收费单，不能精确还原所有错误报单本应在哪一秒成交。路径歧义、换月、缺数或未结束均保持未知，不补理想价；滑点压力为每侧2 tick。实际手续费与连续合约基差仍可能与这里不同。', '',
        '## 交付文件和核验','',
        '- `strategy-summary.csv`：所有原始/已成交/已平仓/完整历史/同期当前识别器队列的两档成本指标。',
        '- `products.csv`、`periods.csv`：每个队列的分品种、月度及周度笔数、胜率、毛额、费用、净额和R。',
        '- `paired-actual-vs-replay.csv`：79笔逐策略对照，含原实际损益、异常标记、策略拒绝/未知原因、反事实净额及差额。',
        '- `accepted-replays.jsonl`、`capacity-audit.jsonl`：接纳交易腿与每品种槽位证据。',
        '- `summary.json`：全量统计、原始归档元数据和源文件SHA256。',
        '- `research-runs/server-execution-20261023-audit-v1`：原始信号溯源、记账修正逐腿表、拒单表和异常逐笔清单。', '',
        f"校验：新旧策略定义与冻结规格逐项相等；新旧行情公共部分逐行无变化；新LEGACY适配器与旧研究{result['verification']['prior_legacy_rows_exact']}条机会的完整结果/成交腿/歧义记录一致；本次柜台返回的{result['verification']['current_day_broker_callbacks_exact']}条当日成交回报与持久化持仓/历史一致；79笔实际交易开平手数守恒；所有分组的笔数、净额对上总表。",'',
        '保留策略参数、费用和拒绝规则，没有用本次结果调整阈值或启动任何自动交易。历史全样本反复使用过，且原始实成交队列较短，结果只能支持本次描述与执行审计。','']
    (output/'report.md').write_text('\n'.join(lines),encoding='utf-8')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True)
    run(p.parse_args().output)
