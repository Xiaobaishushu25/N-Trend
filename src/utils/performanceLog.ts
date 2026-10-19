import { invoke, isTauri } from '@tauri-apps/api/core'

/** Persist sparse UI timings in the client log so a blank screen can be diagnosed later. */
export function reportPerformance(
  stage: string,
  elapsedMs: number,
  context: { symbol?: string; timeframe?: string; rows?: number } = {},
) {
  if (!isTauri()) return
  void invoke('report_frontend_perf', {
    stage,
    elapsedMs,
    symbol: context.symbol,
    timeframe: context.timeframe,
    rows: context.rows,
  }).catch(() => {})
}
