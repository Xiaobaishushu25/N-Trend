"""Aggregate frozen outcomes by score band and local warning hour; no new scoring."""
import argparse
import collections
from decimal import Decimal
import hashlib
import json
from pathlib import Path


SCENARIO = 's1_1tick/snapshot_fee'
VERSIONS = {'quality_v1': 'v1综合', 'quality_v2': 'v2候选综合'}


def summarize(rows):
    outcomes = [r['outcomes'][SCENARIO] for r in rows]
    priced = [e for e in outcomes if e['status'] == 'closed'
              and e.get('net_amount') is not None and e.get('net_r') is not None]
    assert all(e['volume'] == 1 for e in outcomes)
    amounts = [Decimal(str(e['net_amount'])) for e in priced]
    wins = sum(x > 0 for x in amounts)
    profit = sum((x for x in amounts if x > 0), Decimal(0))
    loss = sum((x for x in amounts if x < 0), Decimal(0))
    total_r = sum(e['net_r'] for e in priced)
    return dict(candidates=len(rows), completed=len(priced), wins=wins,
                losses=sum(x < 0 for x in amounts), flat=sum(x == 0 for x in amounts),
                win_rate=wins / len(priced) if priced else None,
                profit_yuan=str(profit), loss_yuan=str(loss), net_yuan=str(profit + loss),
                average_net_yuan=float((profit + loss) / len(priced)) if priced else None,
                total_net_r=total_r, mean_net_r=total_r / len(priced) if priced else None,
                statuses=dict(collections.Counter(e['status'] for e in outcomes)))


def check_partition(total, parts):
    for key in ['candidates', 'completed', 'wins', 'losses', 'flat']:
        assert sum(p[key] for p in parts) == total[key], key
    assert abs(sum((Decimal(p['net_yuan']) for p in parts), Decimal(0))
               - Decimal(total['net_yuan'])) < Decimal('0.000001')
    assert abs(sum(p['total_net_r'] for p in parts) - total['total_net_r']) < 1e-8
    counted = collections.Counter()
    for part in parts:
        counted.update(part['statuses'])
    assert dict(counted) == total['statuses']


def band_index(score, step):
    assert 0 <= score <= 100
    return min(int(score // step), 100 // step - 1)


def band_label(index, step):
    lo = index * step
    return f'{lo}–<{lo + step}' if lo + step < 100 else f'{lo}–100'


def hour_label(hour):
    return f'{hour:02d}:00–{hour + 1:02d}:00'


def pct(value):
    return f'{value:.1%}' if value is not None else '—'


def number(value, digits=2):
    return f'{float(value):+,.{digits}f}' if value is not None else '—'


def table_row(label, stat):
    return (f"|{label}|{stat['candidates']}|{stat['completed']}|"
            f"{stat['wins']}/{stat['losses']}|{pct(stat['win_rate'])}|"
            f"{number(stat['profit_yuan'])}|{number(stat['loss_yuan'])}|"
            f"{number(stat['net_yuan'])}|{number(stat['mean_net_r'], 3)}|")


def table(lines, entries):
    lines.extend(['', '|分组|候选|完成|赢/亏|净胜率|盈利合计元|亏损合计元|净盈亏元|平均净R|',
                  '|---|---:|---:|---:|---:|---:|---:|---:|---:|'])
    lines.extend(table_row(label, stat) for label, stat in entries)
    lines.append('')


def run(source, out, step, start, end):
    assert step > 0 and 100 % step == 0
    out.mkdir(exist_ok=False, parents=True)
    raw = source.read_bytes()
    all_rows = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line]
    assert len({r['candidate_key'] for r in all_rows}) == len(all_rows)
    rows = [r for r in all_rows if start <= r['warning_ts'] < end]
    assert rows
    result = dict(source=str(source.resolve()), source_sha256=hashlib.sha256(raw).hexdigest(),
                  script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  start_inclusive=start, end_exclusive=end, scenario=SCENARIO,
                  score_step=step, timezone='Asia/Shanghai', hour_basis='warning_ts',
                  cohort='common scored, overlaps retained, no old-score or risk10 gate',
                  one_lot=True, totals=summarize(rows), versions={})
    lines = ['# 近一个月：分数段与每小时交易统计', '',
             f'预警时间范围：{start}（含）至{end}（不含），北京时间。归档数据截至2026-10-29 14:55。', '',
             f'分数每{step}分一档，下限包含、上限不包含，最后一档包含100分。小时按预警时刻分组，09:00–10:00含09点、不含10点；15:00归15点档，00点为自然日00:00–01:00，不是交易日归属。', '',
             '执行口径：每信号一手，S1目标退出，保留既有无跟随及期限退出；进出各1 tick滑点及分品种费率快照（白糖往返2元）。快照不是逐日真实账单。保留重叠，不加旧分或风险≥10 tick准入。', '',
             '胜率只对完成且可定价交易计算；未完成、失效、未成交及路径不明确单列。人民币为已完成净盈亏，不包含期末持仓浮盈亏。小时是信号归属，不是该小时内的平仓现金流。']
    table(lines, [('全部候选', result['totals'])])
    for key, title in VERSIONS.items():
        bands = [[] for _ in range(100 // step)]
        hours = [[] for _ in range(24)]
        cells = [[[] for _ in range(24)] for _ in bands]
        selected_hours = [[] for _ in range(24)]
        high = []
        for row in rows:
            index = band_index(row['scores'][key], step)
            hour = int(row['warning_ts'][11:13])
            bands[index].append(row)
            hours[hour].append(row)
            cells[index][hour].append(row)
            if row['scores'][key] >= 70:
                high.append(row)
                selected_hours[hour].append(row)
        band_stats = [summarize(g) for g in bands]
        hour_stats = [summarize(g) for g in hours]
        cell_stats = [[summarize(g) for g in block] for block in cells]
        selected_hour_stats = [summarize(g) for g in selected_hours]
        for partitions in [band_stats, hour_stats, [c for block in cell_stats for c in block]]:
            check_partition(result['totals'], partitions)
        for index, stats in enumerate(cell_stats):
            check_partition(band_stats[index], stats)
        for hour in range(24):
            check_partition(hour_stats[hour], [block[hour] for block in cell_stats])
        high_total = summarize(high)
        check_partition(high_total, selected_hour_stats)
        result['versions'][key] = dict(bands=band_stats, hours=hour_stats,
                                      score_by_hour=cell_stats, ge70=high_total,
                                      ge70_by_hour=selected_hour_stats)
        lines.extend([f'## {title}：每{step}分分段'])
        table(lines, [(band_label(i, step), s) for i, s in enumerate(band_stats) if s['candidates']]
              + [('合计', result['totals'])])
        lines.extend([f'## {title}≥70：每小时分段'])
        table(lines, [(hour_label(i), s) for i, s in enumerate(selected_hour_stats)]
              + [('合计', high_total)])
        lines.extend([f'## {title}≥70：分数段×小时', '',
                      '每格为“完成笔数 / 净胜率 / 净盈亏元”；—表示没有完成可定价交易，候选及全部状态在后续完整交叉表和JSON中保留。', '',
                      '|预警小时|' + '|'.join(band_label(i, step) for i in range(70 // step, 100 // step)) + '|',
                      '|---|' + '---:|' * (100 // step - 70 // step)])
        for hour in range(24):
            values = []
            for index in range(70 // step, 100 // step):
                s = cell_stats[index][hour]
                values.append(f"{s['completed']} / {pct(s['win_rate'])} / {number(s['net_yuan'])}"
                              if s['completed'] else '—')
            lines.append('|' + hour_label(hour) + '|' + '|'.join(values) + '|')
        lines.extend(['', f'## {title}：全部分数段×小时明细', '',
                      '下表保留有候选的格子；未列出的格子无候选。JSON保存所有分数段及24小时空格。'])
        table(lines, [(band_label(i, step) + ' · ' + hour_label(h), s)
                      for i, block in enumerate(cell_stats) for h, s in enumerate(block)
                      if s['candidates']])
    lines.extend(['## 全部分数：每小时汇总', '',
                  '共同候选不变，未按分数筛选时v1/v2的小时汇总完全相同，只列一次。'])
    table(lines, [(hour_label(i), s) for i, s in enumerate(hour_stats)] + [('合计', result['totals'])])
    lines.extend(['## 使用边界', '',
                  '本表是已用于开发的历史分组统计，不是新验证。小时和分数交叉后样本更小，并且不同品种的交易时段、每手风险与并行持仓不同；不能直接把盈利格子拼成已验证的交易规则。小样本的100%胜率和零完成候选均不表示稳定优势。', '',
                  '逐格状态计数、精确净金额、净R与来源哈希见summary.json。所有边际表与交叉表的候选、完成、胜负、状态及金额合计均已核对。'])
    (out / 'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (out / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(json.dumps(dict(output=str(out), totals=result['totals'],
                          ge70={k: v['ge70'] for k, v in result['versions'].items()}), ensure_ascii=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--step', default=10, type=int)
    parser.add_argument('--start', default='2026-09-30 00:00:00')
    parser.add_argument('--end', default='2026-10-30 00:00:00')
    args = parser.parse_args()
    run(args.source, args.out, args.step, args.start, args.end)
