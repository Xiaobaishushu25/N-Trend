import { defineStore } from 'pinia'
import { api } from '../services/api'
import type { IntradayPoint, IntradayChartResponse, MarketSnapshot } from '../types'

interface CacheItem {
  prevSettle: number
  points: IntradayPoint[]
  date: string
}

export const useIntradayStore = defineStore('intraday', {
  state: () => ({
    symbol: '' as string,
    prevSettle: 0 as number,
    points: [] as IntradayPoint[],
    loading: false,
    loadSeq: 0,
    error: '' as string,
    /** 内存缓存池：保留今日已拉取过的品种分时底稿，二次切回 0 延迟、0 网络请求 */
    cache: new Map<string, CacheItem>(),
  }),

  actions: {
    async load(symbol: string) {
      if (!symbol) {
        this.clear()
        return
      }

      const seq = ++this.loadSeq
      const today = new Date().toDateString()
      const cached = this.cache.get(symbol)

      this.symbol = symbol

      // 1. 命中当天缓存：0ms 瞬开，立刻渲染旧走势
      if (cached && cached.date === today) {
        this.prevSettle = cached.prevSettle
        this.points = [...cached.points]
        this.loading = false
        this.error = ''
        return
      }

      // 2. 未命中缓存：发起请求获取开盘至今历史分时底稿
      this.loading = true
      this.error = ''
      this.points = []
      this.prevSettle = 0

      try {
        const resp: IntradayChartResponse = await api.getIntradayChart(symbol)
        if (seq !== this.loadSeq) return

        this.prevSettle = resp.prev_settle
        this.points = resp.points || []

        // 存入内存缓存
        this.cache.set(symbol, {
          prevSettle: resp.prev_settle,
          points: [...(resp.points || [])],
          date: today,
        })
      } catch (e) {
        if (seq !== this.loadSeq) return
        this.error = String(e)
      } finally {
        if (seq === this.loadSeq) {
          this.loading = false
        }
      }
    },

    /** 由现有 3s 轮询 (onQuotesUpdated) 触发的实时增量更新，纯内存操作，0 网络请求 */
    onRealtimeTick(snapshot: MarketSnapshot) {
      if (snapshot.code !== this.symbol || this.points.length === 0 || snapshot.latest == null) {
        return
      }

      const latestPrice = snapshot.latest
      const now = new Date()
      const pad = (n: number) => String(n).padStart(2, '0')
      const currentMinTs = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}:00`

      const last = this.points[this.points.length - 1]

      // 简单提取分钟前缀 "YYYY-MM-DD HH:mm" 对比
      const lastMinPrefix = last.ts.substring(0, 16)
      const curMinPrefix = currentMinTs.substring(0, 16)

      if (lastMinPrefix === curMinPrefix) {
        // 同一分钟内：原地刷新最新价格（分时图最右端上下跳动）
        last.price = latestPrice
      } else if (currentMinTs > last.ts) {
        // 跨入新的一分钟：追加新分钟点（分时图横轴右延）
        const newPoint: IntradayPoint = {
          ts: currentMinTs,
          price: latestPrice,
          avg_price: last.avg_price > 0 ? last.avg_price : latestPrice,
          volume: 0,
        }
        this.points.push(newPoint)

        // 同步更新缓存中对应品种的数据
        const cached = this.cache.get(this.symbol)
        if (cached) {
          cached.points.push(newPoint)
        }
      }
    },

    clear() {
      this.loadSeq++
      this.symbol = ''
      this.prevSettle = 0
      this.points = []
      this.loading = false
      this.error = ''
    },

    clearCache() {
      this.cache.clear()
    },
  },
})
