import type { CtpHealth, SimnowLoginInput } from '../types'

export function emptySimnowLogin(): SimnowLoginInput {
  return { user_id: '', password: '', td_front: 'tcp://182.254.243.31:30001', app_id: 'simnow_client_test', auth_code: '0000000000000000' }
}

export function ctpConnectionLabel(health: CtpHealth | null, online: boolean): string {
  if (!online) return '交易桥未连接'
  if (health?.login_error) return health.login_error
  if (!health?.connected) return '正在连接交易前置'
  if (!health.authenticated) return '正在认证'
  if (!health.logged_in) return '正在登录'
  if (!health.settlement_confirmed) return '已登录，正在确认结算'
  if (health.sync_error) return `同步失败：${health.sync_error}`
  if (!health.ready) return '已登录，正在同步资金和持仓'
  return '已登录 · 资金持仓已同步'
}
