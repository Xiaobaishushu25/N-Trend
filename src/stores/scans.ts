import { defineStore } from 'pinia'
import { api } from '../services/api'
import type { PatternDto, ScanResult, ScanRow, SignalOutcome, SignalRow } from '../types'

export const useScansStore = defineStore('scans', {
  state: () => ({
    history: [] as ScanRow[],
    latest: null as ScanResult | null,
    detail: [] as SignalRow[],
    /** 最新一次扫描的活跃信号（直接来自数据库，避免内存里的事件结果过期） */
    latestSignals: [] as SignalOutcome[],
    /** 最近若干次扫描中、指定品种的活跃信号原始记录（按扫描时间排列，含当时的状态） */
    recentSignals: [] as SignalRow[],
    running: false,
  }),
  actions: {
    async loadHistory(limit = 20) {
      this.history = await api.getScanHistory(limit)
    },
    async runScan() {
      this.running = true
      try {
        this.latest = await api.runScanNow()
        await this.loadHistory(20)
      } finally {
        this.running = false
      }
    },
    async loadDetail(scanId: number) {
      this.detail = await api.getScanDetail(scanId)
    },
    /** 拉取最近若干次扫描里某个品种的活跃信号历史，作为「最近活跃信号」时间线数据源 */
    async loadRecentSignals(symbol: string, scansLimit = 10) {
      const history = await api.getScanHistory(scansLimit)
      const rows: SignalRow[] = []
      for (const h of history) {
        const detail = await api.getScanDetail(h.id)
        rows.push(...detail.filter((r) => r.symbol === symbol))
      }
      this.recentSignals = rows
    },
    /** 从数据库拉取最新一次扫描的活跃信号，保证「全部N形态」始终和数据库一致 */
    async refreshLatestSignals() {
      const rows = await api.getLatestSignals(200)
      const out: SignalOutcome[] = []
      for (const r of rows) {
        try {
          const d = JSON.parse(r.detail) as PatternDto
          out.push({ symbol: r.symbol, ...d })
        } catch {
          // 单条记录解析失败不影响整体
        }
      }
      this.latestSignals = out
    },
    ingest(result: ScanResult) {
      this.latest = result
      this.loadHistory(20)
    },
  },
})
