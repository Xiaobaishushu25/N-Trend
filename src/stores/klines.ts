import { defineStore } from 'pinia'
import { api } from '../services/api'
import { notify } from '../utils/notify'
import { reportPerformance } from '../utils/performanceLog'
import type { ChartHistoryUpdate, ChartKlineResponse, KlineRow, Timeframe } from '../types'

interface PendingLoad {
  key: string
  seq: number
  dirty: boolean
  requiredRevision: number | null
  updates: ChartHistoryUpdate[]
  promise: Promise<void>
}
const pendingLoads = new WeakMap<object, PendingLoad>()
const historyRequests = new WeakMap<object, Map<string, Promise<ChartKlineResponse>>>()

function requestHistory(store: object, key: string, symbol: string, timeframe: Timeframe, limit: number) {
  let requests = historyRequests.get(store)
  if (!requests) {
    requests = new Map()
    historyRequests.set(store, requests)
  }
  const pending = requests.get(key)
  if (pending) return pending
  const current = requests
  const request = api.getChartKlines(symbol, timeframe, limit).finally(() => current.delete(key))
  current.set(key, request)
  return request
}

export const useKlinesStore = defineStore('klines', {
  state: () => ({
    symbol: '' as string,
    rows: [] as KlineRow[],
    partialBar: null as KlineRow | null,
    timeframe: '15m' as Timeframe,
    loading: false,
    loadingCount: 0,
    error: '' as string,
    chartStatus: 'tqsdk' as string,
    chartMessage: '' as string,
    loadSeq: 0,
    dataReceivedAt: 0,
    partialReceivedAt: 0,
    partialSeq: 0,
    partialExpected: false,
    latestClosedTs: '',
    seriesRevision: null as number | null,
    historyLimit: 500,
  }),
  actions: {
    acceptPartial(symbol: string, timeframe: Timeframe, bar: KlineRow | null) {
      if (this.symbol !== symbol || this.timeframe !== timeframe) return false
      if (bar && (bar.symbol !== symbol || bar.timeframe !== timeframe)) return false
      if (bar && bar.ts <= this.latestClosedTs) return false
      if (bar && this.partialBar && bar.ts < this.partialBar.ts) return false
      this.partialSeq++
      this.partialBar = bar
      this.partialReceivedAt = bar ? Date.now() : 0
      this.partialExpected = true
      if (this.chartMessage === '历史数据已获取，实时柱正在准备；就绪后自动推送更新。') this.chartMessage = ''
      return true
    },
    applyHistoryUpdate(update: ChartHistoryUpdate): 'ignored' | 'current' | 'appended' | 'reload' {
      if (update.symbol !== this.symbol) return 'ignored'
      const revision = update.series_revision
      if (this.chartStatus !== 'tqsdk' || this.seriesRevision == null || revision == null) return 'reload'
      if (revision <= this.seriesRevision) return 'current'
      if (update.full_reload_required || update.previous_revision !== this.seriesRevision) return 'reload'
      const bars = (update.bars ?? []).filter(bar => bar.timeframe === this.timeframe)
      let latest = this.latestClosedTs
      for (const bar of bars) {
        if (bar.symbol !== this.symbol || bar.ts <= latest) return 'reload'
        latest = bar.ts
      }
      this.seriesRevision = revision
      if (!bars.length) return 'current'
      const rows = this.rows.concat(bars)
      this.rows = rows.slice(Math.max(0, rows.length - this.historyLimit))
      this.latestClosedTs = latest
      this.dataReceivedAt = Date.now()
      if (this.partialBar && this.partialBar.ts <= latest) {
        this.partialBar = null
        this.partialReceivedAt = 0
        this.partialExpected = false
      }
      return 'appended'
    },
    acceptHistoryUpdate(update: ChartHistoryUpdate): 'ignored' | 'current' | 'appended' | 'reload' {
      if (update.symbol !== this.symbol) return 'ignored'
      const pending = pendingLoads.get(this)
      if (pending?.seq === this.loadSeq) {
        if (pending.updates.length < 32) {
          pending.updates.push(update)
        } else if (update.series_revision == null) {
          pending.dirty = true
        } else {
          pending.requiredRevision = Math.max(pending.requiredRevision ?? 0, update.series_revision)
        }
        // Replay after the baseline arrives. This includes events for a
        // timeframe whose initial request has not completed yet.
        if (this.seriesRevision == null) return 'current'
      }
      return this.applyHistoryUpdate(update)
    },
    async load(symbol: string, timeframe: Timeframe, limit = 500, silent = false, revision?: number) {
      const key = JSON.stringify([symbol, timeframe, limit])
      const pending = pendingLoads.get(this)
      if (revision != null && this.symbol === symbol && this.timeframe === timeframe
        && this.chartStatus === 'tqsdk' && this.seriesRevision != null && this.seriesRevision >= revision) return
      if (pending?.key === key && pending.seq === this.loadSeq) {
        if (silent && revision == null) pending.dirty = true
        if (revision != null) pending.requiredRevision = Math.max(pending.requiredRevision ?? 0, revision)
        if (!silent && !this.rows.length) this.loading = true
        return pending.promise
      }
      const entry: PendingLoad = {
        key, seq: this.loadSeq + 1, dirty: false, requiredRevision: revision ?? null,
        updates: [], promise: Promise.resolve(),
      }
      pendingLoads.set(this, entry)
      entry.promise = (async () => {
        await this.loadOnce(symbol, timeframe, limit, silent, entry)
        while (pendingLoads.get(this) === entry && entry.seq === this.loadSeq
          && (entry.dirty || (this.chartStatus === 'tqsdk' && entry.requiredRevision != null
            && (this.seriesRevision ?? -1) < entry.requiredRevision))) {
          const required = entry.requiredRevision
          entry.dirty = false
          entry.seq = this.loadSeq + 1
          await this.loadOnce(symbol, timeframe, limit, true, entry)
          if (entry.requiredRevision === required && !entry.dirty) break
        }
      })().finally(() => {
        if (pendingLoads.get(this) === entry) pendingLoads.delete(this)
      })
      return entry.promise
    },
    async loadOnce(symbol: string, timeframe: Timeframe, limit: number, silent: boolean, entry: PendingLoad) {
      const started = performance.now()
      if (!silent) reportPerformance('chart_load_started', 0, { symbol, timeframe })
      // silent：后台静默刷新（如定时入库后），不弹加载遮罩
      if (!silent) {
        this.loadingCount++
        this.loading = true
      }
      this.error = ''
      const isSwitch = this.symbol !== symbol || this.timeframe !== timeframe
      if (isSwitch) {
        this.latestClosedTs = ''
        this.seriesRevision = null
      }
      const keepHistory = !isSwitch && this.rows.length > 0 &&
        ['tqsdk', 'tqsdk_recovery_pending', 'offline_snapshot', 'tq_unavailable'].includes(this.chartStatus)
      if (!keepHistory) {
        // 切换品种或周期时，立即清空旧数据，防止旧品种数据残留被误用为新数据基准
        this.rows = []
        this.partialBar = null
        this.dataReceivedAt = 0
        this.partialReceivedAt = 0
        this.partialExpected = false
      }
      this.symbol = symbol
      this.timeframe = timeframe
      this.historyLimit = Math.max(0, Math.min(limit, 5000))
      if (!keepHistory) {
        this.chartStatus = 'tqsdk'
        this.chartMessage = ''
      }
      const seq = ++this.loadSeq
      const partialSeq = this.partialSeq
      try {
        const response = await requestHistory(this, entry.key, symbol, timeframe, limit)
        if (seq !== this.loadSeq) {
          if (!silent) reportPerformance('chart_load_superseded', performance.now() - started, { symbol, timeframe })
          return
        }
        const revision = response.series_revision ?? null
        if (response.status === 'tqsdk' && this.chartStatus === 'tqsdk'
          && this.seriesRevision != null && revision != null && revision < this.seriesRevision) return
        this.rows = response.rows
        this.seriesRevision = revision
        this.latestClosedTs = response.rows[response.rows.length - 1]?.ts ?? ''
        this.dataReceivedAt = Date.now()
        // 实时推送可能在历史响应之前到达，不能被稍旧的HTTP快照覆盖。
        if (partialSeq === this.partialSeq) {
          this.partialBar = response.partial_bar ?? null
          this.partialReceivedAt = this.partialBar ? Date.now() : 0
          this.partialExpected = !!this.partialBar || response.message.includes('实时柱正在准备')
        }
        // A late partial push or an earlier HTTP snapshot cannot overwrite final data.
        if (this.partialBar && this.partialBar.ts <= this.latestClosedTs) {
          this.partialBar = null
          this.partialReceivedAt = 0
          this.partialExpected = false
        }
        this.chartStatus = response.status
        this.chartMessage = this.partialBar && response.message.includes('实时柱正在准备') ? '' : response.message
        for (const update of entry.updates.splice(0)) {
          if (this.applyHistoryUpdate(update) !== 'reload') continue
          if (update.series_revision == null
            || (update.full_reload_required && this.chartStatus === 'tqsdk_recovery_pending')) entry.dirty = true
          else if (this.chartStatus === 'tqsdk') {
            entry.requiredRevision = Math.max(entry.requiredRevision ?? 0, update.series_revision)
          }
        }
        if (!silent) reportPerformance('chart_load_finished', performance.now() - started, { symbol, timeframe, rows: response.rows.length })
      } catch (e: any) {
        if (seq !== this.loadSeq) return
        this.error = e?.message || String(e)
        if (!silent) reportPerformance('chart_load_failed', performance.now() - started, { symbol, timeframe })
        if (!silent) {
          notify.error(`获取 [${symbol}] ${timeframe} K线失败: ${this.error}`)
        }
      } finally {
        // 旧请求结束不能关闭新品种的遮罩；后台推送也会作废当前请求。
        if (seq === this.loadSeq) {
          this.loading = false
          this.loadingCount = 0
        }
      }
    },
    clear() {
      this.loadSeq++
      this.symbol = ''
      this.rows = []
      this.partialBar = null
      this.error = ''
      this.loading = false
      this.loadingCount = 0
      this.chartStatus = 'tqsdk'
      this.chartMessage = ''
      this.dataReceivedAt = 0
      this.partialReceivedAt = 0
      this.partialExpected = false
      this.latestClosedTs = ''
      this.seriesRevision = null
    },
  },
})
