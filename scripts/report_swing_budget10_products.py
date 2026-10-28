"""Read-only frozen replay attribution; no product selection or execution changes."""
import collections
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'research-runs/swing-entry-economics-20261025-v1'
OUT = ROOT / 'research-runs/swing-budget10-product-report-20261026-v1'
CASE = 'unmerged/budget10/corrected_touch/slip1'
MONTHS = [f'2026-{i:02d}' for i in range(1, 11)]
NAMES = dict(BU='沥青', C='玉米', CF='棉花', CJ='红枣', EB='苯乙烯', EG='乙二醇',
             FG='玻璃', JD='鸡蛋', L='塑料', MA='甲醇', PB='沪铅', PP='聚丙烯',
             RB='螺纹钢', SA='纯碱', SF='硅铁', SH='烧碱', SI='工业硅',
             SM='锰硅', SR='白糖', SS='不锈钢', UR='尿素', V='PVC')


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def stats(rows):
    e = [r['result']['evaluation'] for r in rows]
    net = sum(x['net_amount'] for x in e)
    gain = sum(max(0, x['net_amount']) for x in e)
    loss = -sum(min(0, x['net_amount']) for x in e)
    return dict(n=len(e), net=net, mean_r=sum(x['net_r'] for x in e)/len(e) if e else None,
                win_rate=sum(x['net_amount'] > 0 for x in e)/len(e) if e else None,
                cash_pf=gain/loss if loss else None)


def run():
    OUT.mkdir(exist_ok=True)
    artifacts = ['accepted.jsonl', 'results.json', 'protocol.json']
    hashes = {name: sha(SOURCE/name) for name in artifacts}
    rows, unknown = [], []
    statuses = collections.Counter()
    with (SOURCE/'accepted.jsonl').open(encoding='utf-8') as f:
        for line in f:
            obj = json.loads(line)
            if obj['policy'] != 'original' or obj['case'] != CASE:
                continue
            z = obj['row']
            status = z['result']['evaluation']['status']
            statuses[status] += 1
            if status == 'closed':
                rows.append(z)
            elif status not in ('unfilled', 'filtered_net_rr_fill'):
                unknown.append(z)
    archived = read(SOURCE/'results.json')['original']['combined'][CASE]
    assert len(rows) == archived['n'] == 573 and len(unknown) == archived['unknown'] == 1
    assert abs(stats(rows)['net'] - archived['net']) < 1e-7
    protocol = read(SOURCE/'protocol.json')
    coverage = {}
    for q in ['early', 'recent']:
        inp = read(Path(protocol['periods'][q]['input']))
        coverage[q] = {p: dict(first=v['bars'][0]['datetime'], last=v['bars'][-1]['datetime'])
                       for p, v in inp['symbols'].items() if v['bars']}
    symbols = sorted(set(coverage['early']) | set(coverage['recent']))
    matrix = {m: {p: [] for p in symbols} for m in MONTHS}
    exit_month = collections.defaultdict(list)
    for z in rows:
        matrix[z['warning_ts'][:7]][z['symbol']].append(z)
        exit_month[z['exit_trading_day'][:7]].append(z)
    monthly = {}
    for m, groups in matrix.items():
        active = [dict(symbol=p, **stats(rr)) for p, rr in groups.items() if rr]
        monthly[m] = dict(**stats([z for rr in groups.values() for z in rr]),
                          best3=sorted(active, key=lambda x: (-x['net'], x['symbol']))[:3],
                          worst3=sorted(active, key=lambda x: (x['net'], x['symbol']))[:3],
                          products={p: stats(rr) for p, rr in groups.items()},
                          exit_trading_month=stats(exit_month[m]))
    totals = []
    result = read(SOURCE/'results.json')['original']
    for p in symbols:
        rr = [z for z in rows if z['symbol'] == p]
        ms = {m: stats(matrix[m][p]) for m in MONTHS}
        best = [m for m in MONTHS if p in [x['symbol'] for x in monthly[m]['best3']]]
        worst = [m for m in MONTHS if p in [x['symbol'] for x in monthly[m]['worst3']]]
        st = stats(rr)
        assert abs(st['net'] - archived['bins']['product'].get(p, 0)) < 1e-7
        totals.append(dict(symbol=p, name=NAMES[p.removesuffix('0')], **st,
                           early=stats([z for z in rr if z['period']=='early'])['net'],
                           recent=stats([z for z in rr if z['period']=='recent'])['net'],
                           active_months=sum(v['n'] > 0 for v in ms.values()),
                           positive_months=sum(v['net'] > 0 for v in ms.values()),
                           negative_months=sum(v['net'] < 0 for v in ms.values()),
                           best3_months=best, worst3_months=worst,
                           negative_worst3_months=[m for m in worst if ms[m]['net'] < 0],
                           months=ms,
                           stresses={sf: result['combined']['unmerged/budget10/'+sf]['bins']['product'].get(p, 0)
                                     for sf in ['corrected_touch/slip1', 'corrected_touch/slip2',
                                                'next_minute_open/slip1', 'next_minute_open/slip2']}))
    totals.sort(key=lambda x: -x['net'])
    assert abs(sum(x['net'] for x in totals)-archived['net']) < 1e-7
    assert abs(sum(x['net'] for x in monthly.values())-archived['net']) < 1e-7
    assert abs(sum(stats(v)['net'] for v in exit_month.values())-archived['net']) < 1e-7
    obj = dict(case=CASE, policy='original', monthly_basis='warning month; exit trading month also supplied',
               estimated_roundtrip_cost_to_gross_target_space_max=0.10,
               total=stats(rows), unknown=unknown, accepted_status_counts=dict(statuses), months=monthly, products=totals,
               market_source_coverage=coverage, source_sha256=hashes,
               production_changed=False, product_exclusion_applied=False,
               independent_holdout=False)
    (OUT/'statistics.json').write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    def label(p):
        return f"{p.removesuffix('0')}（{NAMES[p.removesuffix('0')]}）"
    def ranks(xs):
        return '；'.join(f"{label(x['symbol'])} {x['net']:+,.2f}" for x in xs)
    lines = ['# 空间成本过滤：逐月与逐品种已平仓净收益', '',
             '固定候选：不合并摆点＋空间成本过滤（budget10），未加A净实体条件。原占用口径，每边1tick、原统一费率模型、每笔1手。', '',
             '主表按预警月份归属，以便与此前报告一致；按平仓交易月份金额另列。净额已扣模型滑点及手续费，不含浮盈亏；1笔未知结果不按零计。', '',
             '2026年7—10月行情覆盖起点各品种不同，10月截至现有10月23日附近数据。无交易月不算盈利或亏损月；没有完整数据覆盖也不等于策略没有机会。', '',
             '|预警月份|已平仓笔数|净收益（元）|按平仓交易月份净额（元）|表现最好三个品种（元）|表现最差三个品种（元）|',
             '|---|---:|---:|---:|---|---|']
    for m, st in monthly.items():
        lines.append(f"|{m}|{st['n']}|{st['net']:,.2f}|{st['exit_trading_month']['net']:,.2f}|{ranks(st['best3'])}|{ranks(st['worst3'])}|")
    lines += [f"|合计|{len(rows)}|{archived['net']:,.2f}|{sum(stats(v)['net'] for v in exit_month.values()):,.2f}|—|—|", '',
              '排名只在当月有已知平仓交易的品种内计算。最好/最差是相对排名，未必分别盈利/亏损；本报告同额按品种代码排序截取三个。', '',
              '|品种|合计净额|上半年|7—10月已覆盖段|笔数|胜率|平均R|盈利/亏损/交易月份|前3次数|后3次数|',
              '|---|---:|---:|---:|---:|---:|---:|---|---:|---:|']
    for z in totals:
        wr = f"{z['win_rate']:.1%}" if z['win_rate'] is not None else '—'
        mr = f"{z['mean_r']:.3f}" if z['mean_r'] is not None else '—'
        lines.append(f"|{label(z['symbol'])}|{z['net']:,.2f}|{z['early']:,.2f}|{z['recent']:,.2f}|{z['n']}|{wr}|{mr}|{z['positive_months']}/{z['negative_months']}/{z['active_months']}|{len(z['best3_months'])}|{len(z['worst3_months'])}|")
    lines += ['', '## 完整月份×品种矩阵（元）', '',
              '|品种|'+'|'.join(m[5:]+'月' for m in MONTHS)+'|',
              '|---|'+'|'.join(['---:']*10)+'|']
    for z in totals:
        lines.append('|'+label(z['symbol'])+'|'+'|'.join(f"{z['months'][m]['net']:,.2f}" if z['months'][m]['n'] else '—' for m in MONTHS)+'|')
    lines += ['', '## 数据覆盖', '', '|品种|上半年源数据起止|近期源数据起止|', '|---|---|---|']
    for p in symbols:
        intervals = [' → '.join(coverage[q][p][k][:10] for k in ['first','last']) if p in coverage[q] else '缺失' for q in ['early','recent']]
        lines.append('|'+label(p)+'|'+'|'.join(intervals)+'|')
    lines += ['', '## 如何解释品种淘汰', '',
              '经常处于月度后3、很少盈利或进入前3，提示当前策略在该品种上可能没有净优势，可列为暂停观察候选；不能仅凭同一十个月开发历史，断言品种永久不适合。', '',
              '前3/后3受其他品种表现和单手合约风险影响。应同时检查盈利/亏损月份、平均净R、交易样本量、两期方向、成本压力和对少数跳空/大单的依赖。已反复研究的历史不能重新称作独立保留数据。', '',
              '若决定排除，应事先冻结规则和名单，再用后续未见行情记录检验；现有历史删掉亏损品种得到的收益增幅只是事后归因，不能直接认定可复制。本报告未修改任何交易名单。', '',
              '具体观察：SF硅铁19笔、10个月中8个月亏损、后3共4次、前3共1次，四压力场景均累计亏损，是最突出的暂停观察研究候选；SS不锈钢12笔、5个交易月中4个月亏损且4次后3/0次前3，但没有近期已知成交可证明跨期稳定拖累。PB仅6笔，SI仅3笔，证据不足。MA甲醇虽总亏损，但10月+1328.63且近期合计+931.60，不宜简单按总额一刀切。CF在最严延迟成本场景反而累计盈利，结论对执行模型敏感。', '',
              '研究依据：[The Probability of Backtest Overfitting](https://escholarship.org/uc/item/4w1110bb)。', '',
              'statistics.json保留所有精确数值、原始未知结果、四压力品种净额、覆盖和来源哈希。']
    (OUT/'report.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    assert all(sha(SOURCE/k)==v for k,v in hashes.items())
    print('\n'.join(lines[:18]))
    print('PASS:573 closed,1 unknown; monthly/product/exit-month sums match frozen replay; source hashes unchanged.')


if __name__ == '__main__':
    run()
