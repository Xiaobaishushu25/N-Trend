"""Build embedded, frozen implementation appendices; never touches a trading account."""
import hashlib
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOC = ROOT / 'docs/候选策略实现与模拟测试规格-2026-10-23.md'
SPEC = ROOT / 'research-protocols/paper-candidates-spec-20261023-v1.json'
MARKER = '<!-- GENERATED_SPEC_APPENDICES -->'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def load(relative):
    return json.loads((ROOT / relative).read_text(encoding='utf-8'))


def build():
    protocol = load('research-protocols/breadth-shadow-20261023-v1.json')
    definitions = {x['definition']['name']: x['definition'] for x in protocol['frozen_candidates']}
    definitions.update({x['name']: x for x in protocol['controls']})
    names = {
        'P0': 'score3_friction20', 'P1': 'fric_weekend',
        'P2': 'fric_atr_weekend', 'P3': 'fric_atr_cap20_weekend',
    }
    fees = load('scripts/exchange_fees_20261022.json')
    holidays = load('research-protocols/holiday-windows-2026-jul-oct-v1.json')
    parameters = {}
    replay_path = ROOT / 'research-runs/breadth-search-20261023-v5/evaluations.jsonl'
    with replay_path.open(encoding='utf-8') as stream:
        for line in stream:
            # Each replay row includes immutable instrument economics, independent of P&L.
            row = json.loads(line)
            product = re.sub(r'\d+$', '', row['symbol']).upper()
            observed = dict(tick_size=row['tick_size'], multiplier=row['multiplier'])
            if product in parameters:
                assert parameters[product] == observed, 'instrument economics changed: ' + product
            parameters[product] = observed
    assert parameters.keys() == fees['products'].keys()
    sources = [
        'crates/n-core/src/analyze/model.rs',
        'crates/n-core/src/derive/mod.rs',
        'crates/n-core/src/analyze/indicators.rs',
        'crates/n-core/src/analyze/pattern.rs',
        'crates/n-core/src/analyze/scoring.rs',
        'crates/n-core/src/analyze/event.rs',
        'crates/n-core/src/analyze/mod.rs',
        'crates/n-core/src/v2/replay/engine.rs',
        'crates/n-core/src/v3/ablation.rs',
        'crates/n-core/src/bin/signal_search.rs',
        'crates/n-core/src/trader/policy.rs',
        'crates/n-core/src/trader/trailing.rs',
        'crates/n-core/src/trader/strategy.rs',
        'crates/n-core/src/precision.rs',
        'scripts/reprice_signal_costs.py',
        'scripts/validate_execution_research.py',
        'scripts/report_breadth_research.py',
        'scripts/exchange_fees_20261022.json',
        'research-protocols/breadth-shadow-20261023-v1.json',
        'research-protocols/holiday-windows-2026-jul-oct-v1.json',
    ]
    fingerprints = {p: sha((ROOT / p).read_bytes()) for p in sources}
    spec = dict(
        id='paper-candidates-spec-20261023-v1', timezone='Asia/Shanghai',
        purpose='implementation documentation and future paper testing',
        qualified_for_trading=False, production_changed=False,
        test_priority=['P2', 'P3', 'P1', 'LEGACY_NATIVE', 'P0'],
        candidates={key: dict(research_name=name, definition=definitions[name]) for key, name in names.items()},
        common=dict(
            quantity=1, score_changes_size=False, account_cap_yuan=None,
            min_planned_risk_ticks=10, min_actual_risk_ticks=10,
            forecast_slip_ticks_per_side=1, execution_slip_ticks_per_side=[1, 2],
            friction_warning_only=True, friction_limit_fraction_of_planned_risk=.20,
            net_reward_loss_min=1, warning_and_proposed_fill_net_gate=True,
            max_chase_price='max(0.20*planned_risk,2*tick_size)',
            pending_buckets=12, holding_buckets=60, no_follow_bucket=5,
            no_follow_mfe_r_strictly_below=.5,
            score_min=3, risk_atr_range_inclusive=[.75, 2.5],
            atr_context='mean(last14 complete15m TR), current warning bucket required, midnight preserved',
            research_counting='legacy midnight bucket function; first entry bucket=1',
            price_policy='fixed original structure stop; original S1 unless P3 cap',
            one_r_breakeven=False, partial_exit=False, trailing_stop=False,
            slot='one accepted entry opportunity per symbol per instance; no pending-warning reservation',
            retry_blocked_opportunities=False,
        ),
        legacy=dict(
            mode='snapshot actual instance definition and execution configuration before testing',
            default_score_min=2.5, default_large_score_min=3.5,
            default_large_volume=2, default_small_volume=1,
            first_exit_r=1, first_large_exit_lots=1,
            breakeven_only_after_confirmed_first_exit=True,
            trailing_activate_r=1.6, trailing_distance_r=.8,
            not_equivalent_to_P0=True,
        ),
        paper_execution_proposal=dict(
            version='paper-tick-v1', matching_model='top_of_book_next_quote',
            base_extra_slip_ticks=0, stress_extra_slip_ticks=1,
            max_quote_age_ms=2000, latency_ms=0,
            matching_requires_subsequent_quote=True, signal_trigger='valid_last',
            buy_fill='ask1 plus adverse extra slip', sell_fill='bid1 minus adverse extra slip',
            forecast_gate_slip_ticks=1, midpoint_or_last_fallback=False,
            fill_confirmation_required=True, same_event_exit_reentry=False,
            atr_and_counting_midnight_corrected=True,
            not_identical_to_research_v5=True,
        ),
        holidays=holidays['windows'],
        fee_snapshot=fees,
        historical_product_parameters=parameters,
        review=protocol['future_review'],
        source_sha256=fingerprints,
    )
    SPEC.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    chunks = [
        '## 附录A：机器可读参数（与配套JSON一致）', '',
        '完整JSON包含22个品种费用快照、规则、执行提案及源文件指纹；以下内嵌全部内容，保存本文即可查阅。', '',
        '```json', json.dumps(spec, ensure_ascii=False, indent=2), '```', '',
        '## 附录B：冻结费用与品种参数速查', '',
        '表内费用格式为“每手固定元 + 成交额×比例”；比例是小数。此表用于复现旧研究，不替代未来合约参数核验。', '',
        '|品种|研究tick|研究乘数|开仓费|平今费|平昨费|备注|',
        '|---|---:|---:|---|---|---|---|',
    ]
    def fmt_fee(rule):
        return f"{rule['fixed_yuan_per_lot']:g} + 成交额×{rule['notional_rate']:g}"
    for product, row in sorted(fees['products'].items()):
        if 'roundtrip_fixed_yuan_per_lot' in row:
            columns = ['往返合计2元/手', '已含在往返覆盖', '已含在往返覆盖', '用户覆盖，优先于普通费率']
        else:
            columns = [fmt_fee(row[k]) for k in ('open', 'close_today', 'close_yesterday')] + ['冻结快照']
        chunks.append(f"|{product}|{parameters[product]['tick_size']:g}|{parameters[product]['multiplier']:g}|" + '|'.join(columns) + '|')
    chunks += [
        '', '## 附录C：源文件指纹', '',
        '以下为本次生成时的完整源文件字节SHA-256；它们用于识别文档依据，不代表未提交文件已发布到运行账户。', '',
        '|路径|SHA-256|', '|---|---|',
    ]
    chunks += [f'|`{p}`|`{h}`|' for p, h in fingerprints.items()]
    chunks += [
        '', '<a id="appendix-d"></a>', '## 附录D：N信号生成的冻结算法全文', '',
        '下面保留本次项目算法的Rust表达，可按函数逐步翻译到其他语言。`pub(crate)`仅表示原项目可见性，',
        '不是额外交易条件。仅裁去与本节无关的后续函数或末尾测试模块；不改写所截取函数。', '',
        '若在独立Rust工程组装这些摘录，依赖chrono和serde（含derive）；按原路径建立analyze子模块，',
        '在analyze父模块导入model::{Bar,NPattern}。derive中的rollover模块声明属于原工程，',
        '仅验证本节N生成函数时可提供空模块；实际换月标记仍须按正文处理。D.1是输入适配结构，不是原文件摘录。', '',
        '调用链：原始5m → derive.aggregate(M15) → Bar及已知换月标记 → event.replay_warnings → WarningCandidate → 正文P组策略。',
        '旧聚合保留会话首尾部分桶，并按原桶末标签输出；回放只保留不晚于原始最后标签的聚合结果。',
        '换月记录confirmed时，在首个聚合时间≥换月时间的Bar置rollover=true；只在预警已知记录范围内使用。',
        '本轮不使用趋势快照作为过滤或加分。indicators.analyze_60m虽名称含60m，event内实际传入当前15m前缀，',
        '这里只保留其原有元数据行为，不据此添加60m方向条件。', '',
        '### D.1 输入Kline结构', '',
        '```rust',
        'pub struct Kline {',
        '    pub datetime: String, // Asia/Shanghai, YYYY-MM-DD HH:MM:SS, end-labelled',
        '    pub open: f64, pub high: f64, pub low: f64, pub close: f64,',
        '    pub volume: f64, pub hold: f64,',
        '}', '```', '',
    ]
    ranges = [
        ('D.2 数据结构与枚举', 'crates/n-core/src/analyze/model.rs', 0, 374),
        ('D.3 原始信号聚合及旧桶函数', 'crates/n-core/src/derive/mod.rs', 0, 219),
        ('D.4 ATR、趋势K、摆动点和趋势快照', 'crates/n-core/src/analyze/indicators.rs', 0, 423),
        ('D.5 N结构与候选选优', 'crates/n-core/src/analyze/pattern.rs', 0, 389),
        ('D.6 评分、反转形态及累积覆盖', 'crates/n-core/src/analyze/scoring.rs', 0, 728),
        ('D.7 前向预警生成器', 'crates/n-core/src/analyze/event.rs', 0, 274),
        ('D.8 已知换月窗口检查', 'crates/n-core/src/analyze/mod.rs', 322, 341),
    ]
    snippet_manifest = []
    for title, source, start, end in ranges:
        lines = (ROOT / source).read_text(encoding='utf-8').splitlines(keepends=True)
        snippet = ''.join(lines[start:end]).rstrip('\n') + '\n'
        snippet_manifest.append(dict(source=source, start_line=start+1, end_line=end,
                                     normalized_excerpt_sha256=sha(snippet.encode('utf-8'))))
        chunks += [f'### {title}', '', f'来源：`{source}`，原始第{start+1}～{end}行。', '',
                   '```rust', snippet.rstrip('\n'), '```', '']
    chunks += [
        '### D.9 摘录完整性清单', '',
        '以下hash针对统一LF换行、保留正文末尾一个换行的摘录文本。若未来源文件变化，不应重新生成并冒充原冻结版本。', '',
        '```json', json.dumps(snippet_manifest, ensure_ascii=False, indent=2), '```', '',
    ]
    text = DOC.read_text(encoding='utf-8')
    assert MARKER in text
    DOC.write_text(text.split(MARKER)[0] + MARKER + '\n\n' + '\n'.join(chunks), encoding='utf-8')
    print(json.dumps(dict(document=str(DOC.relative_to(ROOT)), lines=DOC.read_text(encoding='utf-8').count('\n'),
                          spec=str(SPEC.relative_to(ROOT)), candidates=len(names), fee_products=len(fees['products']),
                          source_files=len(fingerprints), embedded_algorithm_sections=len(ranges)), ensure_ascii=True))


if __name__ == '__main__':
    build()
