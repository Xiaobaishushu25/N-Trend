import { defineStore } from 'pinia'
import { api } from '../services/api'
import { notify } from '../utils/notify'
import { useSettingsStore } from './settings'
import { useSymbolsStore } from './symbols'
import type { PatternDto, ScanResult, ScanRow, SignalOutcome, SignalRow } from '../types'

/** 形态身份键：同一品种内用 方向+级别+s1/s2 索引 识别“同一个形态” */
function signalKey(s: SignalOutcome): string {
  return `${s.symbol}|${s.direction}|${s.level}|${s.s1.index}|${s.s2.index}`
}

/** 对比上一次扫描，找出“新出现”的即将触发信号：本次是即将触发，且上次不是 */
function newPendingSignals(prev: SignalOutcome[] | null, next: SignalOutcome[]): SignalOutcome[] {
  const prevPending = new Set<string>()
  for (const s of prev ?? []) {
    if (s.state === '即将触发') prevPending.add(signalKey(s))
  }
  return next.filter((s) => s.state === '即将触发' && !prevPending.has(signalKey(s)))
}

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
        const result = await api.runScanNow()
        this.applyScanResult(result)
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
          const { vol_ratio, vol_confirmed, ...rest } = d
          out.push({
            symbol: r.symbol,
            ...rest,
            vol_ratio: vol_ratio ?? null,
            vol_confirmed: vol_confirmed === true,
          })
        } catch {
          // 单条记录解析失败不影响整体
        }
      }
      this.latestSignals = out
    },
    /**
     * 扫描完成后统一处理：更新内存中的最新扫描结果，并对比上一次扫描，
     * 对“新出现”的即将触发信号弹出持久通知（不自动关闭）。
     * 应用首次启动后的第一次扫描没有上一次结果可对比，不弹通知。
     */
    applyScanResult(result: ScanResult) {
      const prev = this.latest
      this.latest = result
      this.loadHistory(20)
      const pendings = newPendingSignals(prev?.signals ?? null, result.signals)
      if (!pendings.length) return
      // 局内新形态通知开关与评分阈值
      const notifySettings = useSettingsStore().settings.notify
      if (!notifySettings.in_app_new_pattern) return
      const symbolsStore = useSymbolsStore()
      for (const s of pendings) {
        const sym = symbolsStore.symbols.find((x) => x.code === s.symbol)
        const signal = {
          code: s.symbol,
          name: sym && sym.name !== s.symbol ? sym.name : '',
          direction: s.direction,
          level: s.level,
          grade: s.grade,
          score: s.score,
          entry: s.entry,
          stop: s.stop,
          target: s.target,
        }
        if (s.score >= notifySettings.new_pattern_min_score) {
          notify.signal(signal)
        } else {
          notify.recordSignal(signal)
        }
      }
    },
    ingest(result: ScanResult) {
      this.applyScanResult(result)
    },
  },
})
