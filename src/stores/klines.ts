import { defineStore } from 'pinia'
import { api } from '../services/api'
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
  }),
  actions: {
    async load(symbol: string, timeframe: Timeframe, limit = 500, silent = false) {
      // silent：后台静默刷新（如定时入库后），不弹加载遮罩
      if (!silent) {
        this.loadingCount++
        this.loading = true
      }
      this.error = ''
      const isSwitch = this.symbol !== symbol || this.timeframe !== timeframe
      if (isSwitch) {
        // 切换品种或周期时，立即清空旧数据，防止旧品种数据残留被误用为新数据基准
        this.rows = []
        this.partialBar = null
      }
      this.symbol = symbol
      this.timeframe = timeframe
      this.chartStatus = 'tqsdk'
      this.chartMessage = ''
      const seq = ++this.loadSeq
      try {
        const response = await api.getChartKlines(symbol, timeframe, limit)
        if (seq !== this.loadSeq) return
        this.rows = response.rows
        this.partialBar = response.partial_bar ?? null
        this.chartStatus = response.status
        this.chartMessage = response.message
      } catch (e) {
        if (seq !== this.loadSeq) return
        this.error = String(e)
      } finally {
        if (!silent) {
          this.loadingCount = Math.max(0, this.loadingCount - 1)
        }
        // 当所有非静默请求已结束，或最新请求（即使是静默更新）已落定，确保 loading 状态正确关闭
        if (this.loadingCount === 0 || seq === this.loadSeq) {
          this.loading = false
          if (seq === this.loadSeq) {
            this.loadingCount = 0
          }
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
    },
  },
})
