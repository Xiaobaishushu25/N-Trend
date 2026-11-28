import type { ExecutionConfig, StrategyDefinition, StrategyOriginFields, StrategyRevision } from '../types'
import type { LiveTradeRecord } from '../types'
import { getTradeRecordPnl, summarizeTradeFees } from './tradeTime'

export const unassignedStrategy = 'unassigned'

export interface StrategyRevisionStats {
  completed: number
  wins: number
  gross: number
  fees: ReturnType<typeof summarizeTradeFees>
}

export function versionTradeStats(
  history: LiveTradeRecord[],
  belongsTo: (record: LiveTradeRecord) => string,
  openConditionIds: Set<string>,
): Record<string, StrategyRevisionStats> {
  const versions = new Map<string, LiveTradeRecord[]>()
  for (const row of history) {
    const key = belongsTo(row)
    const rows = versions.get(key)
    if (rows) rows.push(row)
    else versions.set(key, [row])
  }
  return Object.fromEntries([...versions].map(([key, rows]) => {
    let gross = 0
    const completed = new Map<string, number>()
    for (const row of rows) {
      const pnl = getTradeRecordPnl(row)
      gross += pnl
      if (!openConditionIds.has(row.condition_order_id)) {
        const id = row.condition_order_id || row.id
        completed.set(id, (completed.get(id) || 0) + pnl)
      }
    }
    return [key, { completed: completed.size, wins: [...completed.values()].filter(pnl => pnl > 0).length,
      gross, fees: summarizeTradeFees(rows) }]
  }))
}

export function strategyRevisionKey(strategyId: string, revision: string): string {
  return JSON.stringify([strategyId, revision])
}

export function originRevisionKey(row?: StrategyOriginFields | null): string | undefined {
  if (row?.strategy_id && row.strategy_version) {
    return strategyRevisionKey(row.strategy_id, row.strategy_version)
  }
}

const names: Record<string, string> = { n_pattern: '原策略', n_p2: 'P2', n_p3_v1: 'P3', n_b5_six: '六品种 B5' }
const values: Record<string, string> = {
  fixed_s1: '到目标全平', hybrid_s1: '首手止盈、余仓追踪',
  FullAuto: '全自动', SemiAuto: '半自动确认', MonitorOnly: '仅监控',
  tianqin: '天勤行情', ctp: 'CTP 行情',
}
const fields: Record<string, string> = {
  strategy_id: '信号模块', code_version: '代码版本', sizing_policy: '仓位模块', exit_policy: '退出模块',
  exit_mode: '出场方式', max_lots: '最多手数', risk_budget_yuan: '单笔风险预算（元）',
  min_net_rr: '最低净 RR', fees: '费率表', calendar: '交易日历', entry_cutoffs: '按合约限制开仓',
  mode: '执行模式', max_risk_percent: '最大风险比例', default_large_volume: '大仓手数',
  default_small_volume: '小仓手数', daily_loss_limit_r: '日亏损上限（R）', order_wait_seconds: '委托等待（秒）',
  slippage_ticks: '报单偏移（tick）', max_slippage_ticks: '最大入场滑点（tick）',
  max_exit_slippage_ticks: '最大平仓滑点（tick）', min_entry_risk_ticks: '最小入场风险（tick）',
  filter_long_s1_wick: '过滤 S1 长影线', quote_source: '执行行情来源',
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

export function strategyLabel(strategyId: string, codeVersion?: string | null): string {
  const name = names[strategyId] || strategyId
  return codeVersion ? `${name} V${codeVersion}` : `${name} · 版本待核`
}

export function strategyOverview(definition?: StrategyDefinition | null, execution?: ExecutionConfig | null): string {
  const p = record(definition?.parameters)
  const parts: string[] = []
  if (typeof p.risk_budget_yuan === 'number') parts.push(`${p.risk_budget_yuan} 元风险预算`)
  if (typeof p.max_lots === 'number') parts.push(`最多 ${p.max_lots} 手`)
  if (typeof p.exit_mode === 'string') parts.push(values[p.exit_mode] || p.exit_mode)
  if (typeof p.min_net_rr === 'number') parts.push(`最低净 RR ${p.min_net_rr.toFixed(2)}`)
  if (definition?.strategy_id === 'n_p2') parts.push('固定一手 · 原 S1 全平')
  if (definition?.strategy_id === 'n_pattern' && execution) {
    parts.push(`大仓 ${execution.default_large_volume} 手 / 小仓 ${execution.default_small_volume} 手`)
  }
  if (execution?.filter_long_s1_wick) parts.push('S1 长影线过滤')
  return parts.join(' · ')
}

function displayValue(key: string, value: unknown): string {
  if (value == null) return '—'
  if (typeof value === 'boolean') return value ? '开启' : '关闭'
  if (typeof value === 'object') return String(record(value).id || '查看配置快照')
  if (key === 'strategy_id') return names[String(value)] || String(value)
  return values[String(value)] || String(value)
}

export function strategyRevisionChanges(row: StrategyRevision, before?: StrategyRevision) {
  if (!before?.definition || !row.definition) return []
  const current = row.definition
  const previous = before.definition
  const definitionFields = ['strategy_id', 'code_version', 'exit_policy', 'sizing_policy'] as const
  const currentFields = Object.fromEntries(definitionFields.map(key => [key, current[key]]))
  const previousFields = Object.fromEntries(definitionFields.map(key => [key, previous[key]]))
  const groups = [
    ['策略', previousFields, currentFields],
    ['策略参数', record(previous.parameters), record(current.parameters)],
    ['执行设置', record(before.execution), record(row.execution)],
  ] as const
  return groups.flatMap(([group, oldFields, newFields]) =>
    [...new Set([...Object.keys(oldFields), ...Object.keys(newFields)])].flatMap(key => {
      const oldValue = oldFields[key]
      const newValue = newFields[key]
      return JSON.stringify(oldValue) === JSON.stringify(newValue) ? [] : [{
        key: `${group}:${key}`, label: fields[key] || key,
        before: displayValue(key, oldValue), after: displayValue(key, newValue),
      }]
    }))
}
