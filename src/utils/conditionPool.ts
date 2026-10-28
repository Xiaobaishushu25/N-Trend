import type { ConditionPoolRow, StrategyDiagnostics } from '../types'

function number(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : NaN
}

function metric(value: unknown): string {
  const n = number(value)
  return Number.isFinite(n) ? n.toFixed(2) : '未知'
}

function rejectionNote(reason: string, values: Record<string, any>): string {
  const s = values.snapshot || {}
  const warning = values.warning || values
  const risk = number(s.risk ?? warning.risk)
  const tick = number(s.tick ?? warning.tick)
  const reasons: Record<string, string> = {
    score_below_min: `评分 ${metric(s.score ?? warning.score)}，低于准入门槛 ${metric(values.min_score ?? 3)}`,
    risk_ticks_below_min: `止损距离 ${metric(risk / tick)} tick，小于准入门槛 10 tick`,
    friction_above20: `预估往返摩擦成本 ${metric(values.friction_yuan)} 元，超过计划风险金额的 20%`,
    atr_unavailable: '预警时点的完整 ATR14 数据不可用',
    atr_out_of_range: `止损距离 / ATR14 为 ${metric(values.risk_atr)}，超出允许区间 0.75～2.50`,
    filtered_net_rr_plan: '扣除手续费及滑点后，S1 目标净空间不足 1R',
    filtered_holiday_window: '处于节假日禁止开仓窗口',
    filtered_weekend_window: '处于周末禁止开仓窗口',
    calendar_uncovered: '交易日历未覆盖当前时点',
    target_cost_above10: `预估往返成本占计划毛目标空间 ${metric(number(values.cost_to_gross_space) * 100)}%，超过 10%`,
    a_body_net_nonpositive: `A 段排除起点柱后的方向净实体为 ${metric(values.a_body_net_ex_s0)}，未沿交易方向推进`,
    incomplete_warning_bar: '预警柱不是完整的 15 分钟行情',
    stale_warning: '预警超过新信号入场时效，不补开历史交易',
    calendar_coverage_ending: '日历覆盖即将到期，停止新入场',
    hour_counter_pre_long_break: '小时强逆向，处于长休市前最后 10 分钟',
  }
  if (reason.startsWith('invalid_input:')) {
    const detail = reason.slice('invalid_input:'.length)
    const message = detail === 'fee_unavailable' ? '缺少该合约的费用参数'
      : detail === 'invalid geometry' ? '信号价格或止损、目标结构无效'
      : detail === 'signal identity missing' ? '信号标识或实际合约不完整'
      : detail.startsWith('missing finite ') ? `信号缺少有效数值：${detail.slice(15)}`
      : `信号输入数据无效（${detail}）`
    return `准入过滤：${message}；本信号不会开仓`
  }
  return `准入过滤：${reasons[reason] || reason}；本信号不会开仓`
}

// Audit rows never enter the execution engine or its confirm/cancel actions.
export function filteredConditionRows(diagnostics: StrategyDiagnostics | null, revision?: string): ConditionPoolRow[] {
  if (!revision) return []
  const decisions = diagnostics?.states?.[revision]?.decisions || {}
  return Object.entries(decisions).flatMap(([key, decision]) => {
    if (decision.reason === 'accepted_warning') return []
    const values = decision.values || {}
    const s = values.snapshot || {}
    const warning = values.warning || values
    const [symbol, direction, , , , warningTs] = key.split('|')
    return [{
      id: `filtered:${revision}:${key}`,
      signal_key: key,
      event_id: typeof warning.id === 'number' ? warning.id : null,
      symbol: s.symbol || warning.symbol || symbol || '',
      instrument_id: s.instrument || warning.instrument_id || symbol || '',
      direction: s.direction === 'Long' || s.direction === 'long' ? 'up'
        : s.direction === 'Short' || s.direction === 'short' ? 'down' : warning.direction || direction,
      entry_price: number(s.entry ?? warning.entry),
      stop_price: number(s.stop ?? warning.stop),
      target_price: number(s.target ?? warning.target),
      risk: number(s.risk ?? warning.risk),
      score: number(s.score ?? warning.score),
      tier: 'small' as const,
      volume: 0,
      warning_bar_ts: s.warning_ts || warning.warning_ts || warningTs || '',
      created_at: number(s.available_at ?? warning.created_at_epoch) || 0,
      state: 'filtered' as const,
      display_only: true,
      note: rejectionNote(decision.reason, values),
    }]
  })
}
