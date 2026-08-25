import { defineStore } from 'pinia'
import { api } from '../services/api'
import { notify } from '../utils/notify'
import { useSettingsStore } from './settings'
import { useSymbolsStore } from './symbols'
import type { PatternEvent, ScanResult, SingleBarEvent } from '../types'
import { normalizeSingleBar, isExpired } from '../utils/singleBar'

const ACTIVE_STATES = new Set(['pending', 'triggered'])

function activeSignals(rows: PatternEvent[]): PatternEvent[] {
  return rows.filter((e) => ACTIVE_STATES.has(e.state))
}

let cleanupInterval: ReturnType<typeof setInterval> | null = null
function ensureCleanup(store: { cleanupSingleBars: () => void }) {
  if (cleanupInterval != null || typeof window === 'undefined') return
  cleanupInterval = setInterval(() => store.cleanupSingleBars(), 30_000)
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
    singleBars: new Map<string, SingleBarEvent>(),
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
    async refreshLatestSignals() {
      if (this.latest) {
        this.latestSignals = activeSignals(this.latest.signals)
        return
      }
      try {
        const cached = await api.getActiveEvents()
        this.latestSignals = activeSignals(cached as unknown as PatternEvent[])
        if (!cached.length) {
          this.runScan().catch(() => {})
        } else {
          setTimeout(() => this.runScan().catch(() => {}), 1200)
        }
      } catch {
        await this.runScan()
      }
    },
    applyScanResult(result: ScanResult) {
      this.latest = result
      this.latestSignals = activeSignals(result.signals)
      if (result.single_bars?.length) {
        ensureCleanup(this)
        this.upsertSingleBars(result.single_bars)
      }
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
    upsertSingleBars(events: SingleBarEvent[]) {
      const now = Date.now()
      for (const raw of events) {
        if (!raw || raw.timeframe !== '15m') continue
        const e = normalizeSingleBar(raw)
        if (isExpired(e, now)) continue
        this.singleBars.set(e.symbol, e)
      }
      for (const [k, v] of this.singleBars.entries()) {
        if (isExpired(v, now)) this.singleBars.delete(k)
      }
    },
    cleanupSingleBars() {
      const now = Date.now()
      for (const [k, v] of this.singleBars.entries()) {
        if (isExpired(v, now)) this.singleBars.delete(k)
      }
    },
  },
})