import { defineStore } from 'pinia'
import { api } from '../services/api'
import type { IntradayPoint, IntradayChartResponse, MarketSnapshot } from '../types'

interface CacheItem {
  prevSettle: number
  points: IntradayPoint[]
  date: string
}

export function getNightCloseTime(symbol: string): { hour: number; minute: number } | null {
  const prefix = symbol.replace(/\d+$/, '').toUpperCase()
  // 02:30 贵金属与原油
  if (['AU', 'AG', 'SC'].includes(prefix)) {
    return { hour: 2, minute: 30 }
  }
  // 01:00 有色金属与不锈钢
  if (['CU', 'AL', 'ZN', 'PB', 'NI', 'SN', 'BC', 'SS', 'AO'].includes(prefix)) {
    return { hour: 1, minute: 0 }
  }
  // 无夜盘品种（农产品、部分能化、中金所股指国债）
  if ([
    'AP', 'CJ', 'JD', 'LH', 'PK', 'SI', 'LC', 'UR', 'WH', 'PM', 'RI', 'JR',
    'LR', 'BB', 'FB', 'IF', 'IH', 'IC', 'IM', 'TF', 'T', 'TS', 'TL'
  ].includes(prefix)) {
    return null
  }
  // 其余黑色、化工、油脂、玻璃(FG)、纯碱(SA)等默认为 23:00
  return { hour: 23, minute: 0 }
}

export function isTradingHour(symbol: string, date: Date = new Date()): boolean {
  const d = date.getDay()
  if (d === 0) return false // 周日休市
  const h = date.getHours()
  const m = date.getMinutes()
  const timeNum = h * 60 + m

  const prefix = symbol.replace(/\d+$/, '').toUpperCase()
  const isCffex = ['IF', 'IH', 'IC', 'IM', 'TF', 'T', 'TS', 'TL'].includes(prefix)
  const isBond = ['TF', 'T', 'TS', 'TL'].includes(prefix)

  // 日盘判断
  if (isCffex) {
    // 中金所 09:28 - 11:32, 12:58 - 15:02 (国债至 15:17)
    const afternoonClose = isBond ? 15 * 60 + 17 : 15 * 60 + 2
    if (timeNum >= 9 * 60 + 28 && timeNum <= 11 * 60 + 32) return true
    if (timeNum >= 12 * 60 + 58 && timeNum <= afternoonClose) return true
    return false // 中金所无夜盘
  } else {
    // 普通商品期货: 08:58 - 10:17, 10:28 - 11:32, 13:28 - 15:02
    if (timeNum >= 8 * 60 + 58 && timeNum <= 10 * 60 + 17) return true
    if (timeNum >= 10 * 60 + 28 && timeNum <= 11 * 60 + 32) return true
    if (timeNum >= 13 * 60 + 28 && timeNum <= 15 * 60 + 2) return true
  }

  // 夜盘判断
  const nightClose = getNightCloseTime(symbol)
  if (!nightClose) {
    return false // 无夜盘品种
  }

  // 周六仅在跨日夜盘收盘前有效
  if (d === 6) {
    if (nightClose.hour < 8) {
      const closeMin = nightClose.hour * 60 + nightClose.minute + 2
      return timeNum <= closeMin
    }
    return false
  }

  // 周一到周五的夜盘：前半夜 20:58 开始
  if (timeNum >= 20 * 60 + 58) {
    if (nightClose.hour >= 20) {
      const closeMin = nightClose.hour * 60 + nightClose.minute + 2
      return timeNum <= closeMin
    } else {
      // 跨午夜品种（如 01:00 或 02:30），前半夜 21:00-24:00 均为有效交易时段
      return true
    }
  }

  // 跨日凌晨时段 (00:00 - 08:00)
  if (nightClose.hour < 8 && timeNum < 8 * 60) {
    const closeMin = nightClose.hour * 60 + nightClose.minute + 2
    return timeNum <= closeMin
  }

  return false
}

export function getTradingDayKey(date: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  const y = date.getFullYear()
  const m = date.getMonth()
  const d = date.getDate()
  const hour = date.getHours()
  const minute = date.getMinutes()
  const timeNum = hour * 60 + minute

  // 晚上 20:30 以后开始进入下一个交易日（夜盘时段）
  if (timeNum >= 20 * 60 + 30) {
    const nextDate = new Date(y, m, d)
    const dayOfWeek = nextDate.getDay() // 0 周日, 5 周五
    if (dayOfWeek === 5) {
      // 周五晚上夜盘归属于下周一交易日
      nextDate.setDate(nextDate.getDate() + 3)
    } else {
      nextDate.setDate(nextDate.getDate() + 1)
    }
    return `${nextDate.getFullYear()}-${pad(nextDate.getMonth() + 1)}-${pad(nextDate.getDate())}`
  }

  // 周六凌晨 08:00 之前（周五夜盘尾声）归属下周一交易日
  const dayOfWeek = date.getDay()
  if (dayOfWeek === 6 && timeNum < 8 * 60) {
    const nextDate = new Date(y, m, d)
    nextDate.setDate(nextDate.getDate() + 2)
    return `${nextDate.getFullYear()}-${pad(nextDate.getMonth() + 1)}-${pad(nextDate.getDate())}`
  }

  // 其余时间（日盘、正常凌晨或下午休市）归属于当天日历交易日
  return `${y}-${pad(m + 1)}-${pad(d)}`
}

export const useIntradayStore = defineStore('intraday', {
  state: () => ({
    symbol: '' as string,
    prevSettle: 0 as number,
    points: [] as IntradayPoint[],
    loading: false,
    loadSeq: 0,
    error: '' as string,
    /** 内存缓存池：保留当前交易日已拉取过的品种分时底稿，二次切回 0 延迟、0 网络请求 */
    cache: new Map<string, CacheItem>(),
  }),

  actions: {
    async load(symbol: string, force = false) {
      if (!symbol) {
        this.clear()
        return
      }

      if (force) {
        this.cache.delete(symbol)
      }

      const seq = ++this.loadSeq
      const tradingDay = getTradingDayKey()
      const cached = this.cache.get(symbol)

      this.symbol = symbol

      // 1. 命中当前交易日缓存：0ms 瞬开，立刻渲染旧走势
      if (!force && cached && cached.date === tradingDay) {
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

        const now = new Date()
        const pad = (n: number) => String(n).padStart(2, '0')
        const nowLimitStr = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}:59`
        const validPoints = (resp.points || []).filter((p) => p.ts <= nowLimitStr)

        this.prevSettle = resp.prev_settle
        this.points = validPoints

        // 存入内存缓存（绑定交易日）
        this.cache.set(symbol, {
          prevSettle: resp.prev_settle,
          points: [...validPoints],
          date: tradingDay,
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
      const last = this.points[this.points.length - 1]
      const now = new Date()

      // 检查是否跨交易日（例如：内存缓存保留的是白天 15:00 结束的旧走势，而现在已到 20:55 后的夜盘）
      const currentTradingDay = getTradingDayKey(now)
      const cached = this.cache.get(this.symbol)
      if (cached && cached.date !== currentTradingDay) {
        this.cache.delete(this.symbol)
        this.load(this.symbol, true)
        return
      }

      // 如果内存 points 最后一根 bar 的时间与当前时间跨越了日盘和夜盘（白天收盘 -> 晚间夜盘）：
      // 说明是新交易日开启，必须强制从后端重新拉取新交易日分时数据，不能直接 append
      const lastHour = parseInt(last.ts.substring(11, 13), 10)
      const nowHour = now.getHours()
      if (lastHour <= 16 && nowHour >= 20) {
        this.cache.delete(this.symbol)
        this.load(this.symbol, true)
        return
      }

      // 非交易时段（休市期间）：不追加新分钟点，仅同步确保末端报价与实时快照绝对一致
      if (!isTradingHour(this.symbol, now)) {
        if (last.price !== latestPrice) {
          last.price = latestPrice
          if (cached && cached.points.length > 0) {
            cached.points[cached.points.length - 1].price = latestPrice
          }
        }
        return
      }

      const pad = (n: number) => String(n).padStart(2, '0')
      const currentMinTs = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}:00`

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
