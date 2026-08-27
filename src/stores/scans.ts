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
    async runScanFast() {
      this.running = true
      try {
        const result = await api.runScanFastNow()
        this.applyScanResult(result)
      } finally {
        this.running = false
      }
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
      const notifySettings = useSettingsStore().settings.notify
      const symbolsStore = useSymbolsStore()
      const nameOf = (code: string) => {
        const sym = symbolsStore.symbols.find((x) => x.code === code)
        return sym && sym.name !== code ? sym.name : ''
      }
      this.latest = result
      this.latestSignals = activeSignals(result.signals)
      if (result.single_bars?.length) {
        ensureCleanup(this)
        // 单K锤/针：仅对本轮新增的 symbol+trigger_bar_ts 弹一次局内通知（15m内去重）
        const newBars: SingleBarEvent[] = []
        for (const raw of result.single_bars as SingleBarEvent[]) {
          const norm = normalizeSingleBar(raw as any)
          const key = norm.symbol
          const existed = this.singleBars.get(key)
          if (!existed || existed.trigger_bar_ts !== norm.trigger_bar_ts) {
            if (!isExpired(norm, Date.now())) newBars.push(norm)
          }
        }
        this.upsertSingleBars(result.single_bars)
        if (notifySettings.in_app_new_pattern) {
          for(const sb of newBars){
              notify.singleBar({
                symbol: sb.symbol,
                name: nameOf(sb.symbol),
                label: sb.label,
                kind: sb.kind,
                time: sb.trigger_bar_ts.slice(11,16),
                price: sb.price,
              })
            }
          }
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
    injectMockHammer() {
      const symbolsStore = useSymbolsStore()
      const sym = symbolsStore.symbols[0]
      if (!sym) return null
      const now = new Date()
      const pad = (n:number)=>String(n).padStart(2,'0')
      const ts = `${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}:00`
      const expD = new Date(now.getTime()+15*60*1000)
      const expTs = `${expD.getFullYear()}-${pad(expD.getMonth()+1)}-${pad(expD.getDate())} ${pad(expD.getHours())}:${pad(expD.getMinutes())}:00`
      const mock: SingleBarEvent = {
        symbol: sym.code,
        timeframe: '15m',
        kind: 'hammer',
        label: '下影锤',
        trigger_bar_ts: ts,
        expire_bar_ts: expTs,
        triggerTime: now.getTime(),
        expireTime: expD.getTime(),
        price: 8785,
        high: 8785,
        low: 8725,
      }
      ensureCleanup(this)
      this.singleBars.set(mock.symbol, mock)
      notify.singleBar({ symbol: mock.symbol, name: mock.symbol, label: mock.label, kind: 'hammer', time: ts.slice(11,16), price: mock.price })
      return mock
    },
    cleanupSingleBars() {
      const now = Date.now()
      for (const [k, v] of this.singleBars.entries()) {
        if (isExpired(v, now)) this.singleBars.delete(k)
      }
    },
  },
})




