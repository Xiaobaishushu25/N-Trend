import { defineStore } from 'pinia'
import { api } from '../services/api'
import { notify } from '../utils/notify'
import { useSettingsStore } from './settings'
import { useSymbolsStore } from './symbols'
import type { PatternEvent, ScanResult } from '../types'

/** 图表右侧只展示仍在途的信号：已了结、已失效、未知状态一律不进入列表 */
const ACTIVE_STATES = new Set(['pending', 'triggered'])

function activeSignals(rows: PatternEvent[]): PatternEvent[] {
  return rows.filter((e) => ACTIVE_STATES.has(e.state))
}

function toNotificationSignal(e: PatternEvent, name: string) {
  return {
    code: e.symbol,
    name,
    direction: e.direction,
    level: e.level,
    grade: e.grade,
    score: e.entry_score,
    entry: e.entry,
    stop: e.stop,
    target: e.target,
  }
}

export const useScansStore = defineStore('scans', {
  state: () => ({
    latest: null as ScanResult | null,
    latestSignals: [] as PatternEvent[],
    running: false,
  }),
  actions: {
    async runScan() {
      this.running = true
      try {
        const result = await api.runScanNow()
        this.applyScanResult(result)
      } finally {
        this.running = false
      }
    },
    /** 页面加载时同步最新形态：已有扫描结果直接复用，否则触发一次扫描 */
    async refreshLatestSignals() {
      if (!this.latest) await this.runScan()
      else this.latestSignals = activeSignals(this.latest.signals)
    },
    /** 扫描完成后统一处理：更新内存结果，并按事件类型弹通知 */
    applyScanResult(result: ScanResult) {
      this.latest = result
      this.latestSignals = activeSignals(result.signals)
      const notifySettings = useSettingsStore().settings.notify
      const symbolsStore = useSymbolsStore()
      const nameOf = (code: string) => {
        const sym = symbolsStore.symbols.find((x) => x.code === code)
        return sym && sym.name !== code ? sym.name : ''
      }
      if (notifySettings.in_app_new_pattern) {
        for (const e of result.new_warnings) {
          const signal = toNotificationSignal(e, nameOf(e.symbol))
          if (e.entry_score >= notifySettings.new_pattern_min_score) notify.signal(signal)
          else notify.recordSignal(signal)
        }
      }
      for (const e of result.newly_triggered) {
        notify.entryTrigger({
          symbol: e.symbol,
          name: nameOf(e.symbol),
          direction: e.direction,
          entry: e.entry,
          latest: e.trigger_price ?? e.entry,
        })
      }
    },
    ingest(result: ScanResult) {
      this.applyScanResult(result)
    },
  },
})
