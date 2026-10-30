"""Score/outcome associations on a frozen rescan. No scoring formulas or model fitting."""
import argparse
import collections
import hashlib
import json
import math
import pathlib
import random
import sqlite3
import statistics

from reprice_signal_costs import TradingDates, reprice

SCORES = ['old', 'morphology_v1', 'quality_v1', 'morphology_v2', 'quality_v2']
LABELS = {'old': '旧评分', 'morphology_v1': 'v1形态', 'quality_v1': 'v1综合',
          'morphology_v2': 'v2候选形态', 'quality_v2': 'v2候选综合'}


def ranks(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    result = [0.] * len(xs)
    start = 0
    while start < len(xs):
        end = start + 1
        while end < len(xs) and xs[order[end]] == xs[order[start]]:
            end += 1
        for i in order[start:end]:
            result[i] = (start + 1 + end) / 2
        start = end
    return result


def pearson(xs, ys):
    if len(xs) < 3:
        return None
    a, b = statistics.mean(xs), statistics.mean(ys)
    aa = sum((x - a) ** 2 for x in xs)
    bb = sum((y - b) ** 2 for y in ys)
    return sum((x - a) * (y - b) for x, y in zip(xs, ys)) / math.sqrt(aa * bb) if aa * bb else None


def associations(rows, score, scenario):
    closed = [r for r in rows if r['outcomes'][scenario]['status'] == 'closed'
              and r['outcomes'][scenario].get('net_r') is not None]
    xs = [r['scores'][score] for r in closed]
    ys = [r['outcomes'][scenario]['net_r'] for r in closed]
    wins = [int(y > 0) for y in ys]
    sranks = ranks(xs)
    pos, neg = sum(wins), len(wins) - sum(wins)
    auc = (sum(rank for rank, win in zip(sranks, wins) if win) - pos * (pos + 1) / 2) / (pos * neg) if pos * neg else None
    return dict(closed_priced=len(closed), auc_net_win=auc, score_net_r_pearson=pearson(xs, ys),
                score_net_r_spearman=pearson(sranks, ranks(ys)), score_win_point_biserial=pearson(xs, wins))


def stats(rows, scenario):
    es = [r['outcomes'][scenario] for r in rows]
    closed = [e for e in es if e['status'] == 'closed' and e.get('net_r') is not None]
    values = [e['net_r'] for e in closed]
    n, wins = len(values), sum(x > 0 for x in values)
    settled = sorted(closed, key=lambda e: e.get('exit_ts') or '')
    equity = peak = drawdown = 0.
    for e in settled:
        equity += e['net_r']
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return dict(candidates=len(rows), statuses=dict(collections.Counter(e['status'] for e in es)),
                closed_priced=n, wins=wins, losses=sum(x < 0 for x in values),
                win_rate=wins / n if n else None, mean_net_r=statistics.mean(values) if values else None,
                total_net_r=sum(values), closed_drawdown_r=drawdown,
                median_net_r=statistics.median(values) if values else None,
                trim_best5_mean_net_r=statistics.mean(sorted(values)[:-5]) if n > 5 else None,
                priced_fraction=n / len(rows) if rows else None,
                mean_win_r=statistics.mean([x for x in values if x > 0]) if wins else None,
                mean_loss_r=statistics.mean([x for x in values if x < 0]) if any(x < 0 for x in values) else None)


def boundaries(rows, score, n):
    xs = sorted(r['scores'][score] for r in rows)
    return sorted(set(xs[(len(xs) - 1) * i // n] for i in range(1, n))) if xs else []


def bins(rows, score, scenario, bounds):
    result = [[] for _ in range(len(bounds) + 1)]
    for r in rows:
        i = next((i for i, upper in enumerate(bounds) if r['scores'][score] <= upper), len(bounds))
        result[i].append(r)
    return [dict(bin=i + 1, lower_exclusive=bounds[i - 1] if i else None,
                 upper_inclusive=bounds[i] if i < len(bounds) else None,
                 mean_score=statistics.mean(r['scores'][score] for r in rs) if rs else None,
                 stats=stats(rs, scenario)) for i, rs in enumerate(result)]


def top_threshold(rows, score):
    xs = sorted(r['scores'][score] for r in rows)
    return xs[-max(1, len(xs) // 5)] if xs else math.inf


def nonoverlap(rows):
    end, selected = {}, []
    for r in sorted(rows, key=lambda r: (r['warning_ts'], r['id'])):
        if r['s0_ts'] <= end.get(r['symbol'], ''):
            continue
        selected.append(r)
        end[r['symbol']] = r['reserved_until']
    return selected


def top20_block_difference(rows, scenario, draws=500):
    """Descriptive joint-market 5-date blocks; fixed score cuts, never optimize a cut."""
    old_cut, new_cut = (top_threshold(rows, key) for key in ['old', 'quality_v2'])
    days = sorted(set(r['warning_ts'][:10] for r in rows))
    block_of = {day: i // 5 for i, day in enumerate(days)}
    blocks = collections.defaultdict(list)
    for r in rows:
        blocks[block_of[r['warning_ts'][:10]]].append(r)
    values = list(blocks.values())
    rng = random.Random(20261030)
    differences = []
    for _ in range(draws):
        sample = [r for _ in values for r in rng.choice(values)]
        a = stats([r for r in sample if r['scores']['old'] >= old_cut], scenario)['mean_net_r']
        b = stats([r for r in sample if r['scores']['quality_v2'] >= new_cut], scenario)['mean_net_r']
        if a is not None and b is not None:
            differences.append(b - a)
    differences.sort()
    return dict(blocks=len(values), valid_draws=len(differences), draws=draws,
                lower= differences[int((len(differences) - 1) * .025)] if differences else None,
                upper= differences[int((len(differences) - 1) * .975)] if differences else None,
                meaning='v2 top20 mean netR minus old top20; descriptive 5-calendar-date blocks, all symbols jointly; not independent validation')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_rows(batch, db_path, fee_path, output):
    manifest = json.loads((batch / 'manifest.json').read_text(encoding='utf-8'))
    assert digest(batch / 'signals.jsonl') == manifest['signals_sha256']
    assert digest(db_path) == manifest['protocol']['source_database_sha256']
    conn = sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True)
    dates = [dict(datetime=x[0]) for x in conn.execute("SELECT DISTINCT ts FROM klines WHERE timeframe='5m'")]
    products = set(x[0].upper().rstrip('0123456789') for x in conn.execute("SELECT DISTINCT symbol FROM klines WHERE timeframe='5m'"))
    conn.close()
    calendar = TradingDates({'all': dict(bars=dates)})
    profile = json.loads(fee_path.read_text(encoding='utf-8'))
    assert not products - profile['products'].keys(), 'fee profile lacks scanned products'
    rows = []
    excluded = collections.Counter()
    with (output / 'outcomes.jsonl').open('x', encoding='utf-8') as dest:
        for line in (batch / 'signals.jsonl').open(encoding='utf-8'):
            raw = json.loads(line)
            if raw['quality']['status'] != 'ok':
                excluded[raw['quality']['status']] += 1
                continue
            if raw['scores'] is None:
                excluded['candidate_score_unavailable'] += 1
                continue
            event = raw['event']
            row = dict(id=raw['id'], candidate_key=raw['candidate_key'], symbol=raw['symbol'],
                       warning_ts=event['warning_ts'], s0_ts=event['s0_ts'], direction=event['direction'],
                       reserved_until=raw['reserved_until'], risk_ticks=event['risk'] / raw['tick'],
                       scores=dict(old=20 * event['entry_score'], **{key: raw['scores'][key] for key in SCORES[1:]}),
                       levels_partial=raw['scores']['levels_partial'], outcomes={})
            assert all(math.isfinite(x) for x in row['scores'].values())
            for scenario, result in raw['evaluations'].items():
                gross = result['evaluation'].copy()
                row['outcomes'][scenario + '/gross'] = gross
                proxy = gross.copy()
                if proxy['status'] == 'closed':
                    proxy['net_r'] = (proxy['gross_points'] - 2.5 * raw['tick'] * proxy['volume']) / (proxy['risk'] * proxy['volume'])
                row['outcomes'][scenario + '/proxy_fee'] = proxy
                wrapped = dict(symbol=raw['symbol'], direction=event['direction'], multiplier=raw['multiplier'], result=result)
                repriced = reprice(wrapped, profile, calendar)
                row['outcomes'][scenario + '/snapshot_fee'] = repriced['result']['evaluation']
            dest.write(json.dumps(row, ensure_ascii=False) + '\n')
            rows.append(row)
    assert len(rows) + sum(excluded.values()) == manifest['unique_structures']
    assert len({r['candidate_key'] for r in rows}) == len(rows)
    return rows, manifest, excluded


def run(batch, db_path, fee_path, output):
    batch, db_path, fee_path, output = map(pathlib.Path, [batch, db_path, fee_path, output])
    output.mkdir(exist_ok=False)
    rows, manifest, excluded = build_rows(batch, db_path, fee_path, output)
    risk = [r for r in rows if r['risk_ticks'] + 1e-6 >= 10]
    cohorts = dict(all=rows, nonoverlap=nonoverlap(rows), risk10=risk, risk10_nonoverlap=nonoverlap(risk))
    scenarios = sorted(rows[0]['outcomes'])
    quintiles = {key: boundaries(rows, key, 5) for key in SCORES}
    deciles = {key: boundaries(rows, key, 10) for key in SCORES}
    summary = dict(excluded=dict(excluded), common_scored=len(rows), coverage=manifest['coverage'], cohorts={}, strata={},
                   primary='s1_1tick/snapshot_fee', manifest_sha256=digest(batch / 'manifest.json'),
                   fee_profile_sha256=digest(fee_path), candidate_hash=manifest['protocol']['candidate_hash'],
                   script_sha256=digest(pathlib.Path(__file__)), repricer_sha256=digest(pathlib.Path(__file__).with_name('reprice_signal_costs.py')),
                   all_history_is_development=True, no_outcome_fitting=True)
    summary['score_correlations'] = {a + ':' + b: pearson(ranks([r['scores'][a] for r in rows]), ranks([r['scores'][b] for r in rows]))
                                     for i, a in enumerate(SCORES) for b in SCORES[i + 1:]}
    for name, rs in cohorts.items():
        group = dict(candidates=len(rs), event_ids=[r['id'] for r in rs], scenarios={})
        for scenario in scenarios:
            detail = dict(baseline=stats(rs, scenario), scores={})
            for score in SCORES:
                threshold = top_threshold(rs, score)
                top = [r for r in rs if r['scores'][score] >= threshold]
                detail['scores'][score] = dict(associations=associations(rs, score, scenario),
                                              quintiles=bins(rs, score, scenario, quintiles[score]),
                                              deciles=bins(rs, score, scenario, deciles[score]),
                                              top20=dict(threshold=threshold, stats=stats(top, scenario)))
            group['scenarios'][scenario] = detail
        for scenario in ['s1_1tick/snapshot_fee', 'fixed_1r_1tick/snapshot_fee']:
            group['scenarios'][scenario]['v2_minus_old_top20_block_interval'] = top20_block_difference(rs, scenario)
        summary['cohorts'][name] = group
    # Same full-cohort score boundaries within symbols/months; no locally tuned cuts.
    for category, key in [('symbol', lambda r: r['symbol']), ('month', lambda r: r['warning_ts'][:7]),
                          ('risk', lambda r: '10+' if r['risk_ticks'] + 1e-6 >= 10 else 'below10'),
                          ('levels', lambda r: 'partial' if r['levels_partial'] else 'known')]:
        groups = collections.defaultdict(list)
        for r in rows:
            groups[key(r)].append(r)
        summary['strata'][category] = {}
        for name, rs in sorted(groups.items()):
            summary['strata'][category][name] = {}
            for scenario in ['s1_1tick/snapshot_fee', 'fixed_1r_1tick/snapshot_fee']:
                summary['strata'][category][name][scenario] = dict(baseline=stats(rs, scenario), scores={
                    score: dict(associations=associations(rs, score, scenario), quintiles=bins(rs, score, scenario, quintiles[score])) for score in SCORES})
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    (output / 'fee-profile.json').write_bytes(fee_path.read_bytes())
    write_report(summary, manifest, output)
    make_plot(summary, output)
    print(json.dumps(dict(common_scored=len(rows), excluded=dict(excluded), cohorts={key: group['candidates'] for key, group in summary['cohorts'].items()}), ensure_ascii=False))


def fmt(x, percent=False):
    return '—' if x is None else f'{x:.1%}' if percent else f'{x:.3f}'


def write_report(s, manifest, output):
    raw_n = sum(c['raw5m'] for c in manifest['coverage'])
    names = dict(all='全部共同可评分', nonoverlap='先去重叠', risk10='仅风险≥10 tick', risk10_nonoverlap='风险≥10 tick且去重叠')
    lines = ['# 全库重扫：评分与后续表现关联', '',
             f"扫描22个品种、{raw_n}根5分钟K线，重新聚合已完成15分钟K线、逐个收盘时点识别N预警；得到{manifest['unique_structures']}个候选，{s['common_scored']}个进入旧/v1/v2共同评分对照。不是仅重算数据库已保存的2831条事件。", '',
             '研究扫描保留现有N结构识别器的几何与强K等基础条件，并保留细/大级别候选，不由旧总分先选唯一赢家；不是穷举所有可能的AB端点。原识别器和实盘默认行为不变。', '',
             '只冻结一套手工候选参数 quality-rules-v2-candidate-01：在v1基础上加入A贡献集中度软惩罚、B反向尝试与新增深度支持、少量已知前高前低空间；趋势与MA20保留，长影线不新增惩罚。权重是待验证的起点，不是从20例拟合、搜索出的最优参数。所有历史都属于已部分看过的开发材料。', '',
             f"未进入共同评分的覆盖状态：`{dict(s['excluded'])}`。SF/SM采用归档已知的日盘规约纠正夜盘误判，仅用于本次离线研究；UR/V的异常OHLC等不修补为有效评分。", '',
             '## 固定口径', '',
             '主表为S1退出、每侧1 tick滑点、既有分品种费率快照（白糖按用户确认往返2元）。同时给出固定1R、0/1/2 tick滑点、无额外手续费与旧2.5 tick代理费。费率快照不是逐日历史真实账单。两条典型OHLC路径须一致才定价，未成交/失效/路径不明/缺数据等单列，未当作亏损。退出保留第5根无跟随与60根上限。', '',
             '所有分数使用同一候选集，不先用旧分≥2.5过滤。风险≥10 tick子集单列；去重叠先按最早预警选择，保守占用至预警后12+60根15分钟窗口，之后再对评分分组，避免每个分数组各挑一遍。去重叠不是实盘组合回测。', '',
             '## 基线', '', '|集合|候选|完成可定价|净胜率|平均净R|', '|---|---:|---:|---:|---:|']
    for name, label in names.items():
        b = s['cohorts'][name]['scenarios'][s['primary']]['baseline']
        lines.append(f"|{label}|{b['candidates']}|{b['closed_priced']}|{fmt(b['win_rate'], True)}|{fmt(b['mean_net_r'])}|")
    for cohort in ['all', 'risk10_nonoverlap']:
        lines += ['', '## ' + names[cohort] + '：关联与前20%', '',
                  '|评分|可定价数|净胜负AUC|评分/净R Spearman|前20%候选|前20%完成|前20%净胜率|前20%平均净R|去掉最大5笔盈利后平均净R|',
                  '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
        detail = s['cohorts'][cohort]['scenarios'][s['primary']]
        for score in SCORES:
            item = detail['scores'][score]
            a, top = item['associations'], item['top20']['stats']
            lines.append(f"|{LABELS[score]}|{a['closed_priced']}|{fmt(a['auc_net_win'])}|{fmt(a['score_net_r_spearman'])}|{top['candidates']}|{top['closed_priced']}|{fmt(top['win_rate'], True)}|{fmt(top['mean_net_r'])}|{fmt(top['trim_best5_mean_net_r'])}|")
        ci = detail['v2_minus_old_top20_block_interval']
        lines += ['', f"v2综合前20%减旧分前20%的平均净R差，5日期块描述性重采样区间：[{fmt(ci['lower'])}, {fmt(ci['upper'])}]（{ci['valid_draws']}次有效抽样，{ci['blocks']}块）。该区间不是未见验证资格。"]
    lines += ['', '## 风险≥10 tick且去重叠：五档分数', '',
              '各档边界取自全部共同可评分候选的特征分位数，相同值不拆分；空档保留。胜率分母只含完成可定价，不同档的成交覆盖也应一起看。', '',
              '|评分|档位（低→高）|平均分|候选|完成|净胜率|平均净R|', '|---|---:|---:|---:|---:|---:|---:|']
    detail = s['cohorts']['risk10_nonoverlap']['scenarios'][s['primary']]
    for score in SCORES:
        for b in detail['scores'][score]['quintiles']:
            st = b['stats']
            lines.append(f"|{LABELS[score]}|{b['bin']}|{fmt(b['mean_score'])}|{st['candidates']}|{st['closed_priced']}|{fmt(st['win_rate'], True)}|{fmt(st['mean_net_r'])}|")
    lines += ['', '## 退出与成本敏感性：v2综合前20%', '',
              '|退出/成本|选中候选|完成|净胜率|平均净R|', '|---|---:|---:|---:|---:|']
    for scenario in ['s1_0tick/gross', 's1_1tick/gross', 's1_1tick/snapshot_fee', 's1_1tick/proxy_fee', 's1_2tick/snapshot_fee', 'fixed_1r_1tick/snapshot_fee', 'fixed_1r_2tick/snapshot_fee']:
        b = s['cohorts']['risk10_nonoverlap']['scenarios'][scenario]['scores']['quality_v2']['top20']['stats']
        lines.append(f"|{scenario}|{b['candidates']}|{b['closed_priced']}|{fmt(b['win_rate'], True)}|{fmt(b['mean_net_r'])}|")
    lines += ['', '## 如何判断是否有效', '',
              'AUC=0.5接近无净胜负排序能力，Spearman接近0表示缺少净R的单调排序关系。要同时看高分档是否改善、分数是否大体单调、去重叠和成本压力后是否保留、月份/品种是否一致，不能只挑一个高分小组。完整summary.json保留每个品种、月份、风险与关键位缺失层的结果；分组差异和相关性都不证明因果。', '',
              '本次没有训练盈利概率、遍历参数找最优、上线新分或改动数据库。单一候选如果缺少稳定排序作用，应否定/修订这一候选的映射，再用新的冻结版本研究；不能反复在同一历史上调到漂亮再当未来验证。', '',
              '参数与扫描方法见上级protocol.json；逐条候选/评分/完整成交明细见上级signals.jsonl；按成本重估的结果见outcomes.jsonl。', '']
    (output / 'report.md').write_text('\n'.join(lines), encoding='utf-8')


def make_plot(s, output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), constrained_layout=True)
    colors = {'old': '#64748b', 'quality_v1': '#2563eb', 'quality_v2': '#ea580c'}
    for row, (policy, title) in enumerate([('s1', 'S1 exit'), ('fixed_1r', '1R exit')]):
        detail = s['cohorts']['risk10_nonoverlap']['scenarios'][policy + '_1tick/snapshot_fee']
        for score, color in colors.items():
            bs = detail['scores'][score]['deciles']
            xs = [b['bin'] for b in bs]
            axes[row, 0].plot(xs, [b['stats']['win_rate'] if b['stats']['win_rate'] is not None else math.nan for b in bs], marker='o', label=score, color=color)
            axes[row, 1].plot(xs, [b['stats']['mean_net_r'] if b['stats']['mean_net_r'] is not None else math.nan for b in bs], marker='o', label=score, color=color)
        axes[row, 0].set_title(title + ': net win rate')
        axes[row, 1].set_title(title + ': mean net R')
        axes[row, 0].set_ylim(0, 1)
        axes[row, 1].axhline(0, color='#94a3b8', lw=1, ls='--')
        for ax in axes[row]:
            ax.grid(alpha=.2)
            ax.set_xlabel('Score decile, low to high (ties retained)')
    axes[0, 0].legend()
    fig.suptitle('Frozen history: risk >= 10 ticks, overlap removed\n1 tick per side + fee snapshot; developmental evidence', fontsize=13)
    fig.savefig(output / 'score-association.png', dpi=170)
    plt.close(fig)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ['batch', 'db', 'fees', 'out']:
        parser.add_argument('--' + key, required=True)
    args = parser.parse_args()
    run(args.batch, args.db, args.fees, args.out)
