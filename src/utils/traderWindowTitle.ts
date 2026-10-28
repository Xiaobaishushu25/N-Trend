export function traderWindowTitle(strategyId?: string, instanceName?: string): string {
  const names: Record<string, string> = {
    n_b5_six: '六品种 B5', n_p2: 'P2', n_p3: 'P3', n_pattern: 'N 字形',
  }
  return [strategyId ? (names[strategyId] || strategyId) : '', instanceName, '实盘量化执行终端'].filter(Boolean).join(' · ')
}
