import type { ManualLevelAlert } from '../types'

export function manualLevelEventLabel(eventType: string): string {
  if (eventType === 'breakout_up') return '向上突破'
  if (eventType === 'breakout_down') return '向下突破'
  if (eventType === 'retest_support') return '回踩支撑'
  if (eventType === 'retest_resistance') return '回踩压力'
  if (eventType === 'rejection') return '拒绝'
  if (eventType === 'testing') return '测试'
  if (eventType === 'approach') return '接近'
  if (eventType === 'reentry') return '重新进入'
  return '状态变化'
}

export function manualLevelRoleLabel(role: string): string {
  if (role === 'support') return '支撑'
  if (role === 'resistance') return '压力'
  return '待确认'
}

export function manualLevelPhaseLabel(phase: string): string {
  if (phase === 'approaching') return '接近区域'
  if (phase === 'testing') return '测试中'
  if (phase === 'rejection_confirmed') return '拒绝确认'
  if (phase === 'breakout_confirmed') return '突破确认'
  if (phase === 'retest_confirmed') return '回踩确认'
  if (phase === 'pending') return '待确认'
  return phase || '待确认'
}

export function manualLevelAlertTitle(alert: ManualLevelAlert): string {
  return `${manualLevelEventLabel(alert.event_type)} · ${manualLevelRoleLabel(alert.role)}`
}
