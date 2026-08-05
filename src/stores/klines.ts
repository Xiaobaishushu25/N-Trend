import { defineStore } from 'pinia'
import { api } from '../services/api'
import type { KlineRow, Timeframe } from '../types'

export const useKlinesStore = defineStore('klines', {
  state: () => ({
    rows: [] as KlineRow[],
    timeframe: '15m' as Timeframe,
    loading: false,
    error: '' as string,
  }),
  actions: {
    async load(symbol: string, timeframe: Timeframe, limit = 500, silent = false) {
      // silent：后台静默刷新（如定时入库后），不弹加载遮罩
      if (!silent) this.loading = true
      this.error = ''
      this.timeframe = timeframe
      try {
        this.rows = await api.getKlines(symbol, timeframe, limit)
      } catch (e) {
        this.error = String(e)
      } finally {
        if (!silent) this.loading = false
      }
    },
  },
})
