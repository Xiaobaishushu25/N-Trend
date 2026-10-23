"""Recalculate frozen paper-candidate cash statistics, without production writes.

Uses the exact research-v5 fills/fees/entry-slot convention in the candidate spec.
Legacy is a frozen research control, never labelled as a native account ledger.
"""
import argparse
import collections
import csv
import datetime as dt
import hashlib
import json
import math
import pathlib
import sqlite3
import statistics

from report_signal_ablation import jsonl, read_json
from reprice_signal_costs import TradingDates, reprice
from validate_execution_research import capacity, evaluation

ROOT = pathlib.Path(__file__).resolve().parents[1]
UNKNOWN = {'ambiguous', 'missing_data', 'rollover', 'unfinished', 'pending'}
FIELDS = ('n', 'wins', 'losses', 'flat', 'win_rate', 'gross_after_slippage',
          'commission', 'net', 'profit_sum', 'loss_sum', 'mean', 'mean_win',
          'mean_loss', 'payoff_ratio', 'cash_pf', 'total_r', 'mean_r', 'r_pf',
          'median', 'p05', 'p25', 'p75', 'p95', 'best', 'worst',
          'realized_cash_drawdown', 'longest_loss_run')


def digest(path):
    h = hashlib.sha256()
    with pathlib.Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def check_hash(path, expected):
    actual = digest(path)
    if actual != expected:
        raise ValueError('Frozen input hash mismatch: ' + str(path))
    return actual


def priced(rows):
    return [r for r in rows if evaluation(r)['status'] == 'closed']


def summary(rows):
    rows = priced(rows)
    es = [evaluation(r) for r in rows]
    cash = sorted(e['net_amount'] for e in es)
    rs = [e['net_r'] for e in es]
    pos = [x for x in cash if x > 0]
    neg = [x for x in cash if x < 0]
    n = len(cash)
    def mean(xs): return statistics.mean(xs) if xs else None
    def quantile(p): return cash[math.floor(p * (n - 1))] if n else None
    equity = peak = dd = 0.0
    run = longest = 0
    for r in sorted(rows, key=lambda r: (evaluation(r)['exit_ts'], r['event_key'])):
        x = evaluation(r)['net_amount']
        equity += x
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
        run = run + 1 if x < 0 else 0
        longest = max(longest, run)
    gain, loss = math.fsum(pos), math.fsum(neg)
    return dict(n=n, wins=len(pos), losses=len(neg), flat=n-len(pos)-len(neg),
                win_rate=len(pos)/n if n else None,
                gross_after_slippage=math.fsum(r['cost_repricing']['gross_amount_after_slippage'] for r in rows),
                commission=math.fsum(r['cost_repricing']['commission'] for r in rows),
                net=math.fsum(cash), profit_sum=gain, loss_sum=loss, mean=mean(cash),
                mean_win=mean(pos), mean_loss=mean(neg),
                payoff_ratio=mean(pos)/-mean(neg) if pos and neg else None,
                cash_pf=gain/-loss if neg else None,
                total_r=math.fsum(rs), mean_r=mean(rs),
                r_pf=math.fsum(x for x in rs if x > 0)/-math.fsum(x for x in rs if x < 0)
                     if any(x < 0 for x in rs) else None,
                median=statistics.median(cash) if cash else None,
                p05=quantile(.05), p25=quantile(.25), p75=quantile(.75), p95=quantile(.95),
                best=max(cash) if cash else None, worst=min(cash) if cash else None,
                realized_cash_drawdown=dd, longest_loss_run=longest)


def grouped(rows, key, universe=None):
    bins = collections.defaultdict(list)
    for r in rows:
        bins[key(r)].append(r)
    return {k: summary(bins[k]) for k in sorted(universe if universe is not None else bins)}


def week(date):
    date = dt.date.fromisoformat(date[:10])
    return (date-dt.timedelta(days=date.weekday())).isoformat()


def csv_write(path, rows, fields=None):
    rows = list(rows)
    fields = fields or list(rows[0])
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def fmt(x):
    return '无样本' if x is None else f'{x:,.2f}'


def pct(x):
    return '无样本' if x is None else f'{x:.2%}'


def table(headers, rows):
    return ['|'+'|'.join(headers)+'|', '|'+'|'.join('---' for _ in headers)+'|'] + [
        '|'+'|'.join(map(str, row))+'|' for row in rows]


def run(output):
    output = pathlib.Path(output)
    if output.exists():
        raise FileExistsError('Use a new output directory: ' + str(output))
    breadth_dir = ROOT/'research-runs/breadth-search-report-20261023-v5'
    batch = ROOT/'research-runs/breadth-search-20261023-v5'
    reference = read_json(breadth_dir/'summary.json')
    execution = read_json(ROOT/'research-runs/execution-validation-20261022-v2/summary.json')
    spec_path = ROOT/'research-protocols/paper-candidates-spec-20261023-v1.json'
    spec = read_json(spec_path)
    source_path = next(pathlib.Path(p) for p in execution['source_hashes'] if p.endswith('-input.json'))
    legacy_path = next(pathlib.Path(p) for p in execution['source_hashes'] if p.endswith('evaluations.jsonl'))
    hashes = {str(source_path): check_hash(source_path, reference['manifest']['input_sha256']),
              str(legacy_path): check_hash(legacy_path, execution['source_hashes'][str(legacy_path)]),
              str(batch/'evaluations.jsonl'): check_hash(batch/'evaluations.jsonl', reference['source_hashes'][str(pathlib.Path('research-runs/breadth-search-20261023-v5/evaluations.jsonl'))]),
              str(ROOT/'scripts/exchange_fees_20261022.json'): check_hash(ROOT/'scripts/exchange_fees_20261022.json', reference['manifest']['fees_sha256'])}
    source = read_json(source_path)
    calendar = TradingDates(source['symbols'])
    profile = read_json(ROOT/'scripts/exchange_fees_20261022.json')
    symbols = sorted(source['symbols'])
    # Read-only comparison with the newest local raw-5m snapshot, every row.
    db_path = ROOT/'data-dev/ntrend.db'
    con = sqlite3.connect(db_path.as_uri()+'?mode=ro', uri=True)
    raw = {(r[0], r[1]): r[2:] for r in con.execute(
        "select symbol,ts,open,high,low,close,volume,hold from klines where timeframe='5m'")}
    con.close()
    frozen = {(s,b['datetime']): tuple(b[k] for k in ('open','high','low','close','volume','hold'))
              for s,v in source['symbols'].items() for b in v['bars']}
    if frozen != raw:
        raise ValueError('Frozen bars differ from current local raw-5m snapshot')
    coverage = {s: dict(bars=len(v['bars']), start=min(b['datetime'] for b in v['bars']),
                        end=max(b['datetime'] for b in v['bars'])) for s,v in source['symbols'].items()}
    start, end = min(k[1] for k in frozen), max(k[1] for k in frozen)
    months = sorted({d[:7] for d in calendar.dates})
    weeks = []
    d = dt.date.fromisoformat(week(start))
    while d.isoformat() <= week(end):
        weeks.append(d.isoformat())
        d += dt.timedelta(days=7)
    names = {v['research_name']: k for k,v in spec['candidates'].items()}
    cases = collections.defaultdict(list)
    for r in jsonl(batch/'evaluations.jsonl'):
        base, slip = r['scenario'].split('/')
        if base in names and slip in ('slip1','slip2'):
            cases[(names[base], int(slip[-1]))].append(reprice(r, profile, calendar))
    legacy_original = {}
    for r in jsonl(legacy_path):
        if r['scenario'] in ('actual_tier/nf5/slip1_fee','actual_tier/nf5/slip2_fee'):
            slip = int(r['scenario'][-5])
            legacy_original[(slip,r['event_key'])] = r
            # Keep the old repricing exact; do not charge the original proxy twice.
            cases[('LEGACY_RESEARCH',slip)].append(r)
    accepted_reference = collections.defaultdict(dict)
    for r in jsonl(breadth_dir/'accepted_evaluations.jsonl'):
        base, slip = r['scenario'].split('/')
        if r['admission']=='symbol_slot' and base in names and slip in ('slip1','slip2'):
            r.pop('admission')
            accepted_reference[(names[base],int(slip[-1]))][r['event_key']] = r
    result = dict(timezone='Asia/Shanghai', data_start=start, data_end=end,
                  raw_5m_rows=len(frozen), products=symbols, product_coverage=coverage,
                  spec_id=spec['id'], source_hashes=hashes,
                  legacy_execution_config=source['execution_config'],
                  new_independent_observations=0, production_changed=False,
                  quantiles='empirical order statistic floor(p*(n-1)); median uses middle/mean middle two',
                  current_local_raw5m_exact_match=True, cases={})
    alltrades, unknowns, admissions, status_rows, checks = [], [], [], [], []
    for (strategy, slip), rows in sorted(cases.items()):
        if len({r['event_key'] for r in rows}) != len(rows):
            raise ValueError('Duplicate opportunity')
        slot = capacity(rows, source['symbols'], 'entry', 1)
        keys = {r['event_key'] for r in slot['ledger'] if r['accepted']}
        admitted = [r for r in rows if r['event_key'] in keys]
        for r in admitted:
            if evaluation(r)['status']=='closed':
                r['exit_trading_day'] = calendar.day(evaluation(r)['exit_ts'])
        if strategy != 'LEGACY_RESEARCH':
            actual = {r['event_key']:r for r in admitted}
            if actual != accepted_reference[(strategy,slip)]:
                raise ValueError('Selected fills differ from frozen v5 reference')
            ref = reference['cases'][spec['candidates'][strategy]['research_name']+f'/slip{slip}']['admissions']['symbol_slot']['stats']
            s = summary(admitted)
            for field, expected in [('n',ref['n']), ('net',ref['net_amount']), ('total_r',ref['net_r']), ('win_rate',ref['win_rate'])]:
                if abs(s[field]-expected) > 1e-7:
                    raise ValueError('Reference aggregate mismatch: '+field)
            checks.append(dict(strategy=strategy,slip=slip,exact_admitted_rows=len(admitted)))
        else:
            ref = execution['capacity'][f'actual_tier/nf5/slip{slip}_fee']['entry/slots1/forward']['accepted']
            if summary(admitted)['n'] != ref['n'] or abs(summary(admitted)['net']-ref['net_amount'])>1e-7:
                raise ValueError('Legacy slot reference mismatch')
        closed = priced(admitted)
        info = dict(strategy=strategy, slip_ticks_per_side=slip,
                    opportunities=len(rows), all_statuses=dict(collections.Counter(evaluation(r)['status'] for r in rows)),
                    admitted=len(admitted), unknown=sum(evaluation(r)['status'] in UNKNOWN for r in admitted),
                    admitted_statuses=dict(collections.Counter(evaluation(r)['status'] for r in admitted)),
                    blocked=len(slot['ledger'])-len(admitted), summary=summary(admitted),
                    products=grouped(closed,lambda r:r['symbol'],symbols),
                    warning_month=grouped(closed,lambda r:r['warning_ts'][:7],months),
                    exit_trading_month=grouped(closed,lambda r:r['exit_trading_day'][:7],months),
                    exit_calendar_month=grouped(closed,lambda r:evaluation(r)['exit_ts'][:7],months),
                    exit_trading_week=grouped(closed,lambda r:week(r['exit_trading_day']),weeks),
                    exit_trading_day=grouped(closed,lambda r:r['exit_trading_day'],calendar.dates),
                    direction=grouped(closed,lambda r:r['direction'],['up','down']),
                    exit_reason=grouped(closed,lambda r:evaluation(r)['exit_reason']))
        # Fixed bins, no data-driven rebinning per strategy. Left inclusive.
        for label, field, bounds in [('cash_bins','net_amount',[-math.inf,-1000,-500,-200,0,200,500,1000,math.inf]),
                                      ('r_bins','net_r',[-math.inf,-2,-1,-.5,0,.5,1,2,3,math.inf])]:
            info[label] = {f'[{a},{b})':summary([r for r in closed if a<=evaluation(r)[field]<b])
                           for a,b in zip(bounds,bounds[1:])}
        for period in ('warning_month','exit_trading_month','exit_calendar_month','exit_trading_week','exit_trading_day','products','direction','exit_reason','cash_bins','r_bins'):
            bins = info[period]
            if sum(v['n'] for v in bins.values()) != info['summary']['n'] or abs(math.fsum(v['net'] for v in bins.values())-info['summary']['net'])>1e-7:
                raise ValueError('Group does not reconcile: '+period)
        gross, fee, net = [info['summary'][f] for f in ('gross_after_slippage','commission','net')]
        if abs(gross-fee-net)>1e-7:
            raise ValueError('Accounting identity failed')
        info['positive_products'] = sum(s['net']>0 for s in info['products'].values())
        info['active_products'] = sum(s['n']>0 for s in info['products'].values())
        for r in closed:
            # Independent per-leg cash, fee, volume and R verification.
            e, fills = evaluation(r), r['result']['fills']
            sign = 1 if r['direction']=='up' else -1
            g = math.fsum(sign*(f['price']-fills[0]['price'])*f['volume']*r['multiplier'] for f in fills[1:])
            f = math.fsum(f['fee'] for f in r['cost_repricing']['fee_legs'])
            risk = e['risk']*r['multiplier']*e['volume']
            if abs(g-f-e['net_amount'])>1e-7 or abs(e['net_amount']/risk-e['net_r'])>1e-7 or sum(f['volume'] for f in fills[1:])!=e['volume']:
                raise ValueError('Trade ledger does not reconcile')
            alltrades.append(dict(strategy=strategy,slip=slip,event_key=r['event_key'],symbol=r['symbol'],direction=r['direction'],
                                  warning_ts=r['warning_ts'],entry_ts=e['entry_ts'],exit_ts=e['exit_ts'],
                                  exit_trading_day=r['exit_trading_day'],exit_week_monday=week(r['exit_trading_day']),
                                  initial_volume=e['volume'],entry_fill=e['fill'],initial_risk_yuan=risk,
                                  gross_after_slippage=g,commission=f,net=e['net_amount'],net_r=e['net_r'],
                                  exit_reason=e['exit_reason'],mfe_r=e['mfe_r'],mae_r=e['mae_r']))
        for r in admitted:
            if evaluation(r)['status'] in UNKNOWN:
                unknowns.append(dict(strategy=strategy,slip=slip,**r))
        for ledger in slot['ledger']:
            admissions.append(dict(strategy=strategy,slip=slip,**ledger))
        for scope, counts in [('all_opportunities', info['all_statuses']),('admitted',info['admitted_statuses'])]:
            for status,n in counts.items():status_rows.append(dict(strategy=strategy,slip=slip,scope=scope,status=status,n=n))
        result['cases'][f'{strategy}/slip{slip}']=info
    result['verification'] = dict(candidate_reference_exact=checks, closed_trade_ledgers_checked=len(alltrades),
                                  all_group_totals_reconcile=True, raw_5m_db_rows_checked=len(raw),legacy_reference_totals_match=True)
    for p in [spec_path,ROOT/'scripts/report_paper_candidate_history.py',breadth_dir/'accepted_evaluations.jsonl',breadth_dir/'summary.json',ROOT/'research-runs/execution-validation-20261022-v2/summary.json']:
        result['source_hashes'][str(p)] = digest(p)
    output.mkdir(parents=True)
    (output/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    csv_write(output/'overall.csv',(dict(strategy=v['strategy'],slip=v['slip_ticks_per_side'],unknown=v['unknown'],**v['summary']) for v in result['cases'].values()))
    for label in ('products','warning_month','exit_trading_month','exit_calendar_month','exit_trading_week','direction','exit_reason','cash_bins','r_bins'):
        csv_write(output/(label+'.csv'),(dict(strategy=v['strategy'],slip=v['slip_ticks_per_side'],group=k,**s)
                  for v in result['cases'].values() for k,s in v[label].items()))
    csv_write(output/'closed_trades.csv',alltrades)
    csv_write(output/'statuses.csv',status_rows)
    for name,rows in [('unknown_opportunities.jsonl',unknowns),('admission_ledger.jsonl',admissions)]:
        with (output/name).open('w',encoding='utf-8') as f:
            for r in rows:f.write(json.dumps(r,ensure_ascii=False,allow_nan=False)+'\n')
    write_report(result,output)
    print(json.dumps({k:dict(n=v['summary']['n'],wins=v['summary']['wins'],net=v['summary']['net'],unknown=v['unknown'])
                      for k,v in result['cases'].items()},ensure_ascii=False,indent=2))


def write_report(result,output):
    strategies = ['P0','P1','P2','P3']
    def case(p,slip=1):return result['cases'][f'{p}/slip{slip}']
    lines = ['# 候选策略历史信号表现明细（2026-10-23）','',
             f"本次从冻结逐笔回放重新扣费、重做同品种入场槽位并汇总。行情范围 **{result['data_start']}～{result['data_end']}**（上海时间），22个品种、82,149根原始5分钟K；逐行核对与当前本地 `data-dev/ntrend.db` 原始5分钟行情完全一致。候选预警母集2,735条。",'',
             'P0～P3严格使用规格中的research-v5，一手、无资金上限、每品种一个入场槽位。1/2 tick均指每侧滑点，已计入成交价；再逐腿扣冻结手续费，SR往返2元/手。结果仅含双路径一致且可定价的已平仓机会；未知、未入场、过滤和冲突均不计为亏损或零盈亏。', '',
             '## 总体比较','',
             '盈利合计/亏损合计均为逐笔扣费后金额之和；现金PF=盈利合计÷亏损合计绝对值。平仓数按信号归并，不按成交腿计数。', '']
    for slip in (1,2):
        lines += [f'### 每侧{slip} tick','']
        lines += table(['策略','平仓/盈利/亏损','净胜率','盈利合计','亏损合计','净盈亏','手续费','现金PF','平均净R','已实现现金回撤','未知机会'],[
            [p,f"{(s:=case(p,slip)['summary'])['n']}/{s['wins']}/{s['losses']}",pct(s['win_rate']),fmt(s['profit_sum']),fmt(s['loss_sum']),fmt(s['net']),fmt(s['commission']),fmt(s['cash_pf']),f"{s['mean_r']:.4f}",fmt(s['realized_cash_drawdown']),case(p,slip)['unknown']] for p in strategies])
        lines += ['']
    lines += ['P2在这批历史中净额最高；P3胜率最高，但缩短目标削弱了盈利尾部。1/2 tick重查拟成交净空间，接纳集合会改变，不能逐笔机械多扣1 tick来代替压力回放。现金回撤只按退出时间与event_key累计净额，不包含未实现浮盈亏及未知敞口；没有据此计算账户收益率。','',
              '## 旧策略研究对照','',
              '以下为 `LEGACY_RESEARCH`（冻结 `actual_tier/nf5`），保留≥2.5入场、≥3.5两手/其余一手、1R分层及保本/跟踪退出；重算为同品种一个入场槽位，统一冻结手续费。旧研究母集1,352条已通过原基线过滤的预警；P组母集2,735条包含计划风险不足10 tick等随后会被拒绝的预警。两者来自同一套行情/前向信号来源，入口筛选口径分别保留。','',
              '旧组未新增P组的长假/周末退出及净空间、摩擦、ATR过滤。冻结配置见summary.json；资金、实时委托和人工决策未复现，**不能标作LEGACY_NATIVE实际账户业绩**。两手/一手混合使现金金额不能直接当作等风险排名。','']
    lines += table(['每侧滑点','平仓数','净胜率','净额','手续费','现金PF','平均净R','现金回撤','未知'],[
        [slip,(s:=case('LEGACY_RESEARCH',slip)['summary'])['n'],pct(s['win_rate']),fmt(s['net']),fmt(s['commission']),fmt(s['cash_pf']),f"{s['mean_r']:.4f}",fmt(s['realized_cash_drawdown']),case('LEGACY_RESEARCH',slip)['unknown']] for slip in (1,2)])
    lines += ['','## 月度盈亏','',
              '先列预警归属月以复现规格，再列退出交易日归属月表示利润实现时间。7月从3日开始，10月截至22日11:25，均不是完整月份。金额单位元；CSV另有每月笔数、胜率、盈利/亏损合计、费用和R。','']
    for mode,title in [('warning_month','按预警月'),('exit_trading_month','按退出交易月')]:
        for slip in (1,2):
            lines += [f'### {title}，每侧{slip} tick','']
            lines += table(['月份']+strategies,[[m]+[fmt(case(p,slip)[mode][m]['net']) for p in strategies] for m in case('P0')[mode]])
            lines += ['']
    lines += ['## 周度盈亏','',
              '按退出的推断交易日归入周一～周日；夜盘映射下一观察日盘交易日，所以周五夜盘通常计入下一周。交易日来自归档日盘日期推断，非结算单认证。首尾为不完整周；无平仓周显示0.00，仅表示没有实现利润，不代表已验证保本。','']
    for slip in (1,2):
        lines += [f'### 每侧{slip} tick','']
        lines += table(['周一日期']+strategies,[[w]+[fmt(case(p,slip)['exit_trading_week'][w]['net']) for p in strategies] for w in case('P0')['exit_trading_week']])
        lines += ['']
    lines += ['## 分品种胜率和盈亏','',
              '单元格为 **平仓笔数 / 净胜率 / 净盈亏元**。少于10笔标†；没有可定价平仓标“无样本”，不算保本或有效覆盖。各品种行情起止范围另见summary.json的product_coverage。','']
    for slip in (1,2):
        lines += [f'### 每侧{slip} tick','']
        product_rows=[]
        for symbol in result['products']:
            cells=[]
            for p in strategies:
                s=case(p,slip)['products'][symbol]
                cells.append(f"{s['n']}{'†' if s['n']<10 else ''} / {pct(s['win_rate'])} / {fmt(s['net'])}" if s['n'] else '无样本')
            product_rows.append([symbol]+cells)
        lines += table(['品种']+strategies,product_rows)+['']
    lines += ['## 盈亏分布（每侧1 tick）','',
              '平均盈利/平均亏损基于扣费后的正/负交易；分位数采用floor(p×(n−1))经验顺序统计，单笔中位数采用通常中位数。所有数值在计算中保留精度，表格最后四舍五入。','']
    lines += table(['策略','平均每笔','平均盈利','平均亏损','盈亏比','中位数','P05','P95','最好单笔','最差单笔','最长连亏'],[
        [p]+[fmt((s:=case(p)['summary'])[k]) for k in ('mean','mean_win','mean_loss','payoff_ratio','median','p05','p95','best','worst')]+[s['longest_loss_run']] for p in strategies])
    lines += ['','### 单笔净金额区间笔数（左闭右开）','']
    lines += table(['金额区间']+strategies,[[b]+[case(p)['cash_bins'][b]['n'] for p in strategies] for b in case('P0')['cash_bins']])
    lines += ['','### 单笔净R区间笔数（左闭右开）','']
    lines += table(['净R区间']+strategies,[[b]+[case(p)['r_bins'][b]['n'] for p in strategies] for b in case('P0')['r_bins']])
    lines += ['','## 解释与数据边界','',
              'P2/P3的接纳未知机会在每侧1 tick下各3条；压力场景请查总体表及unknown_opportunities.jsonl。未知机会保守占槽，已平仓利润合计不是所有敞口的最终利润。OHLC只比较两条单调路径，且原持仓桶计数保留午夜兼容逻辑，无法复原真实盘口与排队。', '',
              '这批历史已被多轮候选筛选反复使用，新增独立观察为0。盈利品种和月份是描述统计；少样本、成本压力转亏及利润集中都应保留，不能按本期排名建白名单。费用是2026-10-22冻结快照，非逐笔历史结算单。','',
              '## 文件与复算','',
              '- `overall.csv`：含旧研究对照及两档滑点的总体完整指标。',
              '- `products.csv`：所有22个品种的胜率、现金盈亏、费用和R。',
              '- `warning_month.csv` / `exit_trading_month.csv` / `exit_calendar_month.csv`：三种月份口径。',
              '- `exit_trading_week.csv`：周度完整指标；周起始日为周一。',
              '- `cash_bins.csv` / `r_bins.csv`：固定区间盈亏分布。',
              '- `closed_trades.csv`：每笔净额、成交时刻、手续费、R和退出原因，可独立核查。',
              '- `direction.csv` / `exit_reason.csv`：方向和退出原因统计。',
              '- `statuses.csv` / `unknown_opportunities.jsonl` / `admission_ledger.jsonl`：未定价状态与接纳/冲突审计。',
              '- `summary.json`：全量数据、数据范围、冻结配置、SHA256和校验结果。','',
              '程序：`scripts/report_paper_candidate_history.py --output <一个尚不存在的目录>`。只读取研究归档与本地行情，不改交易策略、数据库或账户配置。', '',
              f"本次校验：{result['verification']['closed_trade_ledgers_checked']}笔已平仓逐笔核对成交腿/费用/初始风险R；全部品种、月份、周、方向、退出原因和分布区间的笔数及净额与总表相等；P0～P3共8个场景的接纳逐笔与冻结v5完全一致；旧组与冻结槽位报告总数/净额一致。",'']
    (output/'report.md').write_text('\n'.join(lines),encoding='utf-8')


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',required=True)
    args=p.parse_args()
    run(args.output)
