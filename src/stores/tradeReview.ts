import { defineStore } from 'pinia'
import { api, onScanCompleted } from '../services/api'
import { useSymbolsStore } from './symbols'
import { calcTradePnl, getSymbolMultiplier } from '../utils/multipliers'
import type { OutcomeDetail } from '../types'

export type TradeReviewStage = 'all' | 'open' | 'closed' | 'staged'

export interface TradeReviewTicket {
  event_id: number
  symbol: string
  symbol_name: string
  direction: 'up' | 'down'
  stage: 'open' | 'closed' | 'staged' | 'expired'
  warning_ts: string
  warning_kind: string
  grade: string
  level: string
  entry_score: number
  planned_entry: number
  planned_stop: number
  planned_target: number
  initial_risk_points: number
  entry_ts: string | null
  entry_price: number | null
  trigger_volume_ratio: number | null
  overshoot_r: number | null
  exit_ts: string | null
  exit_price: number | null
  exit_reason: string
  bars_held: number | null
  r_multiple: number | null
  mfe_r: number | null
  mae_r: number | null
  multiplier: number
  lots: number
  pnl_points: number
  pnl_amount: number
  pnl_pct: number
  is_win: boolean
  created_at: string
  raw: OutcomeDetail
}

export interface ReviewMetrics {
  totalSettled: number
  winCount: number
  lossCount: number
  winRate: number
  totalPoints: number
  totalAmount: number
  avgWinAmount: number
  avgLossAmount: number
  payoffRatio: number
  profitFactor: number
  avgR: number
  totalR: number
  maxDrawdownR: number
  openCount: number
  stagedCount: number
}

export const useTradeReviewStore = defineStore('tradeReview', {
  state: () => ({
    rawOutcomes: [] as OutcomeDetail[],
    activeTab: 'all' as TradeReviewStage,
    searchKeyword: '',
    filterDirection: null as string | null,
    filterExitReason: null as string | null,
    lots: 1,
    loading: false,
    refreshing: false,
    unlistenScan: null as (() => void) | null,
  }),

  getters: {
    tickets(): TradeReviewTicket[] {
      const symbolsStore = useSymbolsStore()
      const nameMap = new Map<string, string>()
      for (const s of symbolsStore.symbols) {
        nameMap.set(s.code, s.name && s.name !== s.code ? s.name : s.code)
      }

      return this.rawOutcomes.map((item) => {
        const symbol = item.symbol
        const symbolName = nameMap.get(symbol) || symbol
        const dir = (item.direction === 'down' ? 'down' : 'up') as 'up' | 'down'
        const multiplier = getSymbolMultiplier(symbol)
        const initialRisk = Math.abs(item.entry - item.stop)

        // 判断交易阶段 (stage)
        let stage: 'open' | 'closed' | 'staged' | 'expired' = 'staged'
        if (item.state === 'pending') {
          stage = 'staged'
        } else if (item.state === 'expired') {
          stage = 'expired'
        } else if (
          item.exit_price != null ||
          item.outcome === 'win' ||
          item.outcome === 'loss' ||
          item.outcome === 'rollover' ||
          (item.exit_reason && item.exit_reason !== '')
        ) {
          stage = 'closed'
        } else if (item.state === 'triggered' || item.outcome === 'open') {
          stage = 'open'
        }

        // 入场价以实际触发价优先，其次计划价
        const actualEntry = item.trigger_price ?? item.entry
        // 平仓价
        const actualExit = item.exit_price ?? actualEntry

        // 计算盈亏
        const pnl = calcTradePnl(dir, actualEntry, actualExit, symbol, this.lots)
        const pnlPoints = stage === 'closed' ? pnl.points : 0
        const pnlAmount = stage === 'closed' ? pnl.amount : 0
        const pnlPct = stage === 'closed' ? pnl.pct : 0
        const isWin = stage === 'closed' ? pnl.isWin : false

        return {
          event_id: item.event_id,
          symbol,
          symbol_name: symbolName,
          direction: dir,
          stage,
          warning_ts: item.warning_ts,
          warning_kind: item.warning_kind,
          grade: item.grade,
          level: item.level,
          entry_score: item.entry_score,
          planned_entry: item.entry,
          planned_stop: item.stop,
          planned_target: item.target,
          initial_risk_points: initialRisk,
          entry_ts: item.trigger_ts ?? item.trigger_bar_ts,
          entry_price: actualEntry,
          trigger_volume_ratio: item.trigger_volume_ratio ?? null,
          overshoot_r: item.overshoot_r ?? null,
          exit_ts: item.exit_ts,
          exit_price: item.exit_price ?? null,
          exit_reason: item.exit_reason || '',
          bars_held: item.bars_held ?? null,
          r_multiple: item.r_multiple ?? null,
          mfe_r: item.mfe_r ?? null,
          mae_r: item.mae_r ?? null,
          multiplier,
          lots: this.lots,
          pnl_points: pnlPoints,
          pnl_amount: pnlAmount,
          pnl_pct: pnlPct,
          is_win: isWin,
          created_at: item.created_at,
          raw: item,
        }
      })
    },

    filteredTickets(): TradeReviewTicket[] {
      const q = this.searchKeyword.trim().toUpperCase()
      return this.tickets.filter((t) => {
        // Tab 过滤
        if (this.activeTab === 'open') {
          if (t.stage !== 'open') return false
        } else if (this.activeTab === 'closed') {
          if (t.stage !== 'closed') return false
        } else if (this.activeTab === 'staged') {
          if (t.stage !== 'staged') return false
        } else {
          // 'all': 主视图默认展示已入场和已平仓的交易（排除大量单纯未触发的staged和expired以消除噪音）
          if (t.stage !== 'open' && t.stage !== 'closed') return false
        }

        // 搜索过滤
        if (q) {
          const matchCode = t.symbol.toUpperCase().includes(q)
          const matchName = t.symbol_name.toUpperCase().includes(q)
          if (!matchCode && !matchName) return false
        }

        // 方向过滤
        if (this.filterDirection && this.filterDirection !== '' && t.direction !== this.filterDirection) {
          return false
        }

        // 平仓原因过滤
        if (this.filterExitReason) {
          if (this.filterExitReason === 'other') {
            if (['target', 'stop', 'time_exit'].includes(t.exit_reason)) return false
          } else if (t.exit_reason !== this.filterExitReason) {
            return false
          }
        }

        return true
      })
    },

    metrics(): ReviewMetrics {
      const settled = this.tickets.filter((t) => t.stage === 'closed')
      const totalSettled = settled.length
      const wins = settled.filter((t) => t.pnl_points > 0)
      const losses = settled.filter((t) => t.pnl_points < 0)
      const winCount = wins.length
      const lossCount = losses.length
      const winRate = totalSettled > 0 ? winCount / totalSettled : 0

      let totalPoints = 0
      let totalAmount = 0
      let totalR = 0
      let totalWinAmount = 0
      let totalLossAmount = 0

      for (const t of settled) {
        totalPoints += t.pnl_points
        totalAmount += t.pnl_amount
        if (t.r_multiple != null) totalR += t.r_multiple
        if (t.pnl_amount > 0) totalWinAmount += t.pnl_amount
        else if (t.pnl_amount < 0) totalLossAmount += Math.abs(t.pnl_amount)
      }

      const avgWinAmount = winCount > 0 ? totalWinAmount / winCount : 0
      const avgLossAmount = lossCount > 0 ? totalLossAmount / lossCount : 0
      const payoffRatio = avgLossAmount > 0 ? avgWinAmount / avgLossAmount : 0
      const profitFactor = totalLossAmount > 0 ? totalWinAmount / totalLossAmount : totalWinAmount > 0 ? 99 : 0
      const avgR = totalSettled > 0 ? totalR / totalSettled : 0

      // 计算最大回撤 R
      let peakR = 0
      let curR = 0
      let maxDdR = 0
      for (const t of settled) {
        curR += t.r_multiple ?? 0
        if (curR > peakR) peakR = curR
        const dd = peakR - curR
        if (dd > maxDdR) maxDdR = dd
      }

      const openCount = this.tickets.filter((t) => t.stage === 'open').length
      const stagedCount = this.tickets.filter((t) => t.stage === 'staged').length

      return {
        totalSettled,
        winCount,
        lossCount,
        winRate,
        totalPoints,
        totalAmount,
        avgWinAmount,
        avgLossAmount,
        payoffRatio,
        profitFactor,
        avgR,
        totalR,
        maxDrawdownR: maxDdR,
        openCount,
        stagedCount,
      }
    },
  },

  actions: {
    async load() {
      this.loading = true
      try {
        const symbolsStore = useSymbolsStore()
        if (!symbolsStore.symbols.length) {
          await symbolsStore.load().catch(() => {})
        }
        const data = await api.getRecentOutcomes(2000)
        this.rawOutcomes = data ?? []
      } catch (e) {
        console.error('加载复盘交易数据失败:', e)
      } finally {
        this.loading = false
      }
    },

    async refresh() {
      this.refreshing = true
      try {
        await api.refreshOutcomesNow().catch(() => {})
        await this.load()
      } finally {
        this.refreshing = false
      }
    },

    initAutoRefresh() {
      if (this.unlistenScan) return
      onScanCompleted(() => {
        void this.load()
      }).then((un) => {
        this.unlistenScan = un
      }).catch(() => {})
    },

    destroyAutoRefresh() {
      if (this.unlistenScan) {
        this.unlistenScan()
        this.unlistenScan = null
      }
    },

    /** 导出量化回测 CSV */
    exportCsv() {
      const tickets = this.filteredTickets
      if (!tickets.length) return

      const headers = [
        'event_id',
        'symbol',
        'symbol_name',
        'direction',
        'stage',
        'warning_ts',
        'grade',
        'level',
        'entry_score',
        'planned_entry',
        'planned_stop',
        'planned_target',
        'entry_ts',
        'entry_price',
        'exit_ts',
        'exit_price',
        'exit_reason',
        'bars_held',
        'pnl_points',
        'pnl_amount',
        'multiplier',
        'lots',
        'r_multiple',
        'mfe_r',
        'mae_r',
      ]

      const rows = tickets.map((t) => [
        t.event_id,
        t.symbol,
        `"${t.symbol_name}"`,
        t.direction,
        t.stage,
        `"${t.warning_ts || ''}"`,
        t.grade,
        t.level,
        t.entry_score.toFixed(2),
        t.planned_entry,
        t.planned_stop,
        t.planned_target,
        `"${t.entry_ts || ''}"`,
        t.entry_price ?? '',
        `"${t.exit_ts || ''}"`,
        t.exit_price ?? '',
        `"${t.exit_reason || ''}"`,
        t.bars_held ?? '',
        t.pnl_points.toFixed(1),
        t.pnl_amount.toFixed(0),
        t.multiplier,
        t.lots,
        t.r_multiple != null ? t.r_multiple.toFixed(2) : '',
        t.mfe_r != null ? t.mfe_r.toFixed(2) : '',
        t.mae_r != null ? t.mae_r.toFixed(2) : '',
      ])

      const csvContent = '\uFEFF' + [headers.join(','), ...rows.map((r) => r.join(','))].join('\n')
      const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `ntrend_trade_review_${new Date().toISOString().slice(0, 10)}.csv`
      document.body.appendChild(link)
      link.click()
      document.body.removeChild(link)
      URL.revokeObjectURL(url)
    },

    /** 导出量化 JSONL */
    exportJson() {
      const tickets = this.filteredTickets
      if (!tickets.length) return
      const jsonStr = JSON.stringify(tickets, null, 2)
      const blob = new Blob([jsonStr], { type: 'application/json;charset=utf-8;' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `ntrend_trade_tickets_${new Date().toISOString().slice(0, 10)}.json`
      document.body.appendChild(link)
      link.click()
      document.body.removeChild(link)
      URL.revokeObjectURL(url)
    },
  },
})
