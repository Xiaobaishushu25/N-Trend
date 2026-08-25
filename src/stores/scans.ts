import { defineStore } from 'pinia'
import { api } from '../services/api'
import { notify } from '../utils/notify'
import { useSettingsStore } from './settings'
import { useSymbolsStore } from './symbols'
import type { PatternEvent, ScanResult, SingleBarEvent } from '../types'

/** 图表右侧只展示仍在途的信号：已了结、已失效、未知状态一律不进入列表 */
const ACTIVE_STATES = new Set(['pending', 'triggered'])

function activeSignals(rows: PatternEvent[]): PatternEvent[] {
  return rows.filter((e) => ACTIVE_STATES.has(e.state))
}

let _cleanupInterval: ReturnType<typeof setInterval> | null = null
function ensureCleanupInterval(store: any) {
  if (_cleanupInterval != null || typeof window === "undefined") return
  _cleanupInterval = setInterval(() => store.cleanupSingleBars(), 30000)
}

function toSingleBarTimes(e: SingleBarEvent): SingleBarEvent { const toMs = (v: any) => typeof v === "number" ? v : new Date(String(v).replace(" ", "T")).getTime(); const t = (e as any).trigger_bar_ts ?? (e as any).triggerTime; const ex = (e as any).expire_bar_ts ?? (e as any).expireTime; const triggerTime = typeof t === "number" ? t : toMs(t); const expireTime = typeof ex === "number" ? ex : toMs(ex); return { ...e, trigger_bar_ts: typeof t === "string" ? t : new Date(t).toISOString().slice(0,16).replace("T"," ")+":00", expire_bar_ts: typeof ex === "string" ? ex : new Date(ex).toISOString().slice(0,16).replace("T"," ")+":00", triggerTime, expireTime }; }

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
    /** 页面加载时同步最新形态：优先用 DB 缓存秒级渲染，后台静默扫描更新 */
    async refreshLatestSignals() {
      if (this.latest) {
        this.latestSignals = activeSignals(this.latest.signals)
        return
      }
      try {
        const cached = await api.getActiveEvents()
        this.latestSignals = activeSignals(cached as unknown as PatternEvent[])
        // 后台静默刷新，不阻塞表格首绘；结果通过 scan-completed 事件回流到 ingest
        if (!cached.length) {
          this.runScan().catch(() => {})
        } else {
          setTimeout(() => this.runScan().catch(() => {}), 1200)
        }
      } catch {
        await this.runScan()
      }
    },
    /** 扫描完成后统一处理：更新内存结果，并按事件类型弹通知 */
    applyScanResult(result: ScanResult) {
      this.latest = result
      this.latestSignals = activeSignals(result.signals)
      if ((result as any).single_bars && Array.isArray((result as any).single_bars)) {
        ensureCleanupInterval(this as any); this.upsertSingleBars((result as any).single_bars as SingleBarEvent[])
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
        if (!raw || (raw as any).timeframe !== "15m") continue
        const e = toSingleBarTimes(raw as any)
        if (now > e.expireTime) continue
        this.singleBars.set(e.symbol, e)
      }
      for (const [k, v] of this.singleBars.entries()) {
        if (now > v.expireTime) this.singleBars.delete(k)
      }
    },
    cleanupSingleBars() {
      const now = Date.now()
      for (const [k, v] of this.singleBars.entries()) {
        if (now > v.expireTime) this.singleBars.delete(k)
      }
    },

  },
})
