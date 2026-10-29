import { defineStore } from 'pinia'
import { api } from '../services/api'
import { notify } from '../utils/notify'
import { reportPerformance } from '../utils/performanceLog'
import type { KlineRow, Timeframe } from '../types'

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
  }),
  actions: {
    acceptPartial(symbol: string, timeframe: Timeframe, bar: KlineRow | null) {
      if (this.symbol !== symbol || this.timeframe !== timeframe) return false
      if (bar && (bar.symbol !== symbol || bar.timeframe !== timeframe)) return false
      if (bar && this.partialBar && bar.ts < this.partialBar.ts) return false
      this.partialSeq++
      this.partialBar = bar
      this.partialReceivedAt = bar ? Date.now() : 0
      this.partialExpected = true
      if (this.chartMessage === '历史数据已获取，实时柱正在准备；就绪后自动推送更新。') this.chartMessage = ''
      return true
    },
    async load(symbol: string, timeframe: Timeframe, limit = 500, silent = false) {
      const started = performance.now()
      if (!silent) reportPerformance('chart_load_started', started, { symbol, timeframe })
      // silent：后台静默刷新（如定时入库后），不弹加载遮罩
      if (!silent) {
        this.loadingCount++
        this.loading = true
      }
      this.error = ''
      const isSwitch = this.symbol !== symbol || this.timeframe !== timeframe
      if (isSwitch || !silent) {
        // 切换品种或周期时，立即清空旧数据，防止旧品种数据残留被误用为新数据基准
        this.rows = []
        this.partialBar = null
        this.dataReceivedAt = 0
        this.partialReceivedAt = 0
        this.partialExpected = false
      }
      this.symbol = symbol
      this.timeframe = timeframe
      this.chartStatus = 'tqsdk'
      this.chartMessage = ''
      const seq = ++this.loadSeq
      const partialSeq = this.partialSeq
      try {
        const response = await api.getChartKlines(symbol, timeframe, limit)
        if (seq !== this.loadSeq) {
          if (!silent) reportPerformance('chart_load_superseded', performance.now() - started, { symbol, timeframe })
          return
        }
        this.rows = response.rows
        this.dataReceivedAt = Date.now()
        // 实时推送可能在历史响应之前到达，不能被稍旧的HTTP快照覆盖。
        if (partialSeq === this.partialSeq) {
          this.partialBar = response.partial_bar ?? null
          this.partialReceivedAt = this.partialBar ? Date.now() : 0
          this.partialExpected = !!this.partialBar || response.message.includes('实时柱正在准备')
        }
        this.chartStatus = response.status
        this.chartMessage = this.partialBar && response.message.includes('实时柱正在准备') ? '' : response.message
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
    },
  },
})
