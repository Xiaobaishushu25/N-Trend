import { defineStore } from 'pinia'
import { api, onQuotesUpdated } from '../services/api'
import { listen, type UnlistenFn } from '@tauri-apps/api/event'
import { DEFAULT_MULTIPLIERS, getSymbolPrefix } from '../utils/multipliers'
import { notify } from '../utils/notify'
import type {
  CtpHealth,
  LiveTraderSnapshot,
  ExecutionConfig,
  TraderAccount,
  LiveBrokerPosition,
  LiveConditionOrder,
  LiveManagedPosition,
  LiveTradeRecord,
  LiveTradeStatistics,
} from '../types'

export type LiveTraderTab = 'positions' | 'conditions' | 'history'

export const useLiveTraderStore = defineStore('liveTrader', {
  state: () => ({
    health: null as CtpHealth | null,
    config: null as ExecutionConfig | null,
    account: null as TraderAccount | null,
    brokerPositions: [] as LiveBrokerPosition[],
    managedPositions: [] as LiveManagedPosition[],
    conditions: [] as LiveConditionOrder[],
    history: [] as LiveTradeRecord[],
    executionOrders: [] as NonNullable<LiveTraderSnapshot['execution_orders']>,
    statistics: null as LiveTradeStatistics | null,
    strategyComparison: null as LiveTraderSnapshot['strategy_comparison'],
    updatingEntryFilter: false,
    loading: false,
    refreshing: false,
    activeTab: 'positions' as LiveTraderTab,
    filterReason: '' as string,
    errorMsg: null as string | null,
    pollTimer: null as ReturnType<typeof setInterval> | null,
    realtimeListeners: [] as UnlistenFn[],
    realtimeGeneration: 0,
    pushRevision: 0,
    lastPushAt: 0,
    bridgeOnline: false,
    snapshotSource: '' as string,
    lastRecoveryAt: 0,
    now: Date.now(),
    quotes: {} as Record<string, { price: number; receivedAt: number }>,
  }),

  getters: {
    isReady(): boolean {
      return Boolean(this.health?.ready)
    },
    activePositions(): LiveManagedPosition[] {
      return this.managedPositions.filter((p) => !p.is_closed)
    },
    combinedPositions(): Array<{
      instrument_id: string
      direction: string
      position: number
      today_position: number
      yd_position: number
      open_cost: number
      avg_price: number
      position_profit: number
      profit_is_estimated: boolean
      margin: number
      is_broker: boolean
      managed?: LiveManagedPosition
    }> {
      const list: Array<{
        instrument_id: string
        direction: string
        position: number
        today_position: number
        yd_position: number
        open_cost: number
        avg_price: number
        position_profit: number
        profit_is_estimated: boolean
        margin: number
        is_broker: boolean
        managed?: LiveManagedPosition
      }> = []

      for (const bp of this.brokerPositions) {
        if (bp.position <= 0) continue
        const managed = this.managedPositions.find(
          (m) => m.instrument_id.toLowerCase() === bp.instrument_id.toLowerCase() && !m.is_closed
            && (m.direction === 'up' ? 'buy' : 'sell') === bp.direction,
        )
        const inst = bp.instrument_id.toLowerCase()
        const mult = DEFAULT_MULTIPLIERS[getSymbolPrefix(inst)]
        const avgPrice = mult && bp.position > 0 && bp.open_cost > 0 ? bp.open_cost / (bp.position * mult) : 0
        // Never substitute a continuous/main contract for the actual held contract.
        const quote = this.quotes[inst]
        const canEstimate = Boolean(this.isReady && !this.health?.sync_pending && mult && quote
          && this.now - quote.receivedAt < 15000 && bp.position_cost > 0)
        const estimatedProfit = canEstimate
          ? (bp.direction === 'buy' ? 1 : -1) * (quote.price * mult * bp.position - bp.position_cost)
          : bp.position_profit
        list.push({
          instrument_id: bp.instrument_id,
          direction: bp.direction,
          position: bp.position,
          today_position: bp.today_position,
          yd_position: bp.yd_position,
          open_cost: bp.open_cost,
          avg_price: avgPrice,
          position_profit: estimatedProfit,
          profit_is_estimated: canEstimate,
          margin: bp.margin,
          is_broker: true,
          managed,
        })
      }

      for (const mp of this.managedPositions) {
        if (mp.is_closed) continue
        const exists = list.some(
          (item) => item.instrument_id.toLowerCase() === mp.instrument_id.toLowerCase(),
        )
        if (!exists) {
          list.push({
            instrument_id: mp.instrument_id,
            direction: mp.direction === 'up' ? 'buy' : 'sell',
            position: mp.volume,
            today_position: mp.volume,
            yd_position: 0,
            open_cost: mp.fill_price * mp.volume,
            avg_price: mp.fill_price,
            position_profit: 0,
            profit_is_estimated: false,
            margin: 0,
            is_broker: false,
            managed: mp,
          })
        }
      }

      return list
    },
    activeConditions(): LiveConditionOrder[] {
      return this.conditions.filter(
        (c) => ['Pending', 'WaitingConfirm', 'Triggered', 'Unknown', 'Working', 'PartFilled', 'Closing', 'Rejected'].includes(c.state),
      )
    },
    waitingConfirmConditions(): LiveConditionOrder[] {
      return this.conditions.filter((c) => c.state === 'WaitingConfirm')
    },
    filteredHistory(): LiveTradeRecord[] {
      const list = this.filterReason
        ? this.history.filter((h) => h.exit_reason === this.filterReason)
        : [...this.history]
      return list.sort((a, b) => {
        const timeDiff = (b.exit_ts || '').localeCompare(a.exit_ts || '')
        if (timeDiff !== 0) return timeDiff
        return (b.id || '').localeCompare(a.id || '')
      })
    },
    totalRealizedPnlAmount(): number {
      if (this.statistics?.total_pnl != null && this.statistics.total_pnl !== 0) {
        return this.statistics.total_pnl
      }
      return this.history.reduce((sum, h) => {
        if (h.realized_pnl_amount != null && !Number.isNaN(h.realized_pnl_amount) && h.realized_pnl_amount !== 0) {
          return sum + h.realized_pnl_amount
        }
        const mult = DEFAULT_MULTIPLIERS[getSymbolPrefix(h.instrument_id || h.symbol)] ?? 10
        return sum + (h.realized_pnl_pts || 0) * mult * (h.volume || 1)
      }, 0)
    },
    totalRealizedPnlPoints(): number {
      return this.history.reduce((sum, h) => sum + (h.realized_pnl_pts || 0), 0)
    },
    conditionMap(): Record<string, LiveConditionOrder> {
      const map: Record<string, LiveConditionOrder> = {}
      for (const c of this.conditions) {
        map[c.id] = c
      }
      return map
    },
    resolveEventId(): (item?: { event_id?: number | null; condition_order_id?: string } | null) => number | null {
      return (item) => {
        if (!item) return null
        if (item.event_id != null) return item.event_id
        return this.conditionMap[item.condition_order_id || '']?.event_id ?? null
      }
    },
    resolvePlannedEntryPrice(): (item?: { planned_entry_price?: number | null; condition_order_id?: string } | null) => number | null {
      return (item) => {
        if (!item) return null
        if (item.planned_entry_price != null) return item.planned_entry_price
        return this.conditionMap[item.condition_order_id || '']?.entry_price ?? null
      }
    },
    resolveScore(): (item?: { score?: number | null; condition_order_id?: string } | null) => number | null {
      return (item) => {
        if (!item) return null
        if (item.score != null) return item.score
        return this.conditionMap[item.condition_order_id || '']?.score ?? null
      }
    },
    resolveWarningBarTs(): (item?: { warning_bar_ts?: string | null; condition_order_id?: string } | null) => string | null {
      return (item) => {
        if (!item) return null
        if (item.warning_bar_ts) return item.warning_bar_ts
        return this.conditionMap[item.condition_order_id || '']?.warning_bar_ts ?? null
      }
    },
    resolveTargetPrice(): (item?: { target_price?: number | null; condition_order_id?: string } | null) => number | null {
      return (item) => {
        if (!item) return null
        if (item.target_price != null) return item.target_price
        return this.conditionMap[item.condition_order_id || '']?.target_price ?? null
      }
    },
    resolveInitialStopPrice(): (item?: { initial_stop_price?: number | null; condition_order_id?: string } | null) => number | null {
      return (item) => {
        if (!item) return null
        if (item.initial_stop_price != null) return item.initial_stop_price
        return this.conditionMap[item.condition_order_id || '']?.stop_price ?? null
      }
    },
    resolveTriggerTs(): (item?: { trigger_ts?: string | null; condition_order_id?: string } | null) => string | null {
      return (item) => {
        if (!item) return null
        if (item.trigger_ts) return item.trigger_ts
        return this.conditionMap[item.condition_order_id || '']?.trigger_ts ?? null
      }
    },
    resolveOrderInsertTs(): (item?: { order_insert_ts?: string | null; condition_order_id?: string } | null) => string | null {
      return (item) => {
        if (!item) return null
        if (item.order_insert_ts) return item.order_insert_ts
        return null
      }
    },
  },

  actions: {
    async loadAll() {
      this.loading = true
      this.errorMsg = null
      const savedMode = localStorage.getItem('ntrend_trader_mode') as 'MonitorOnly' | 'SemiAuto' | 'FullAuto' | null
      if (savedMode && ['MonitorOnly', 'SemiAuto', 'FullAuto'].includes(savedMode)) {
        if (!this.config) {
          this.config = {
            mode: savedMode,
            max_risk_percent: 1.0,
            default_large_volume: 2,
            default_small_volume: 1,
            daily_loss_limit_r: 3.0,
          }
        } else {
          this.config.mode = savedMode
        }
      }
      await this.refresh()
      this.loading = false
    },

    applySnapshot(snapshot: LiveTraderSnapshot) {
      if (snapshot.source && snapshot.source !== this.snapshotSource) {
        this.quotes = {}
        this.strategyComparison = null
      }
      if (snapshot.source) this.snapshotSource = snapshot.source
      this.health = snapshot.health
      this.bridgeOnline = snapshot.bridge_online !== false
      if (snapshot.account !== undefined) this.account = snapshot.account
      this.brokerPositions = snapshot.broker_positions || []
      if (snapshot.config) this.config = snapshot.config
      if (snapshot.managed_positions) this.managedPositions = snapshot.managed_positions
      if (snapshot.conditions) this.conditions = snapshot.conditions
      if (snapshot.history) this.history = snapshot.history
      if (snapshot.statistics !== undefined) this.statistics = snapshot.statistics
      if (snapshot.strategy_comparison !== undefined) this.strategyComparison = snapshot.strategy_comparison
      if (snapshot.execution_orders) this.executionOrders = snapshot.execution_orders
      this.errorMsg = snapshot.execution_error || snapshot.health?.sync_error || null
    },

    async refresh() {
      if (this.refreshing) return
      this.refreshing = true
      const revision = this.pushRevision
      try {
        const res: any = await api.getTraderSnapshot()
        if (revision !== this.pushRevision) return // A newer push supersedes this HTTP response.
        if (res?.success === false) throw new Error(res.error || '交易快照尚未就绪')
        const snapshot = res?.data !== undefined ? res.data : res
        if (snapshot) this.applySnapshot(snapshot)
      } catch (e: any) {
        if (revision !== this.pushRevision) return
        this.errorMsg = typeof e === 'string' ? e : e?.message || '获取交易快照失败'
        this.bridgeOnline = false
        if (this.health) this.health = { ...this.health, ready: false, healthy: false }
      } finally {
        this.refreshing = false
      }
    },

    async startRealtime() {
      this.stopRealtime()
      const generation = this.realtimeGeneration
      const add = async (listener: Promise<UnlistenFn>) => {
        const unlisten = await listener
        if (generation !== this.realtimeGeneration) unlisten()
        else this.realtimeListeners.push(unlisten)
      }
      try {
        await add(listen<LiveTraderSnapshot>('trader-updated', ({ payload }) => {
          this.pushRevision++
          this.lastPushAt = Date.now()
          this.applySnapshot(payload)
        }))
        await add(listen('trader-heartbeat', () => {
          this.lastPushAt = Date.now()
          if (!this.bridgeOnline || this.errorMsg || !this.account) {
            void this.refresh()
          }
        }))
        await add(listen('trader-resync-needed', () => { void this.refresh() }))
        await add(listen<string>('connection-status-changed', ({ payload }) => {
          if (payload !== 'connected' && this.snapshotSource !== 'local') {
            this.lastPushAt = 0
            this.bridgeOnline = false
            if (this.health) this.health = { ...this.health, ready: false, healthy: false }
          }
        }))
        // Trader messages are complete snapshots. Global sequence gaps also occur
        // for intentionally filtered chart events and do not require a trader read.
        await add(listen('trader-stream-disconnected', () => {
          this.lastPushAt = 0
          this.bridgeOnline = false
          if (this.health) this.health = { ...this.health, ready: false, healthy: false }
        }))
        await add(onQuotesUpdated((quotes) => {
          for (const quote of quotes) {
            if (quote.latest != null && Number.isFinite(quote.latest) && quote.latest > 0) {
              this.quotes[quote.code.toLowerCase()] = { price: quote.latest, receivedAt: Date.now() }
            }
          }
        }))
      } catch {
        // Browser preview or unavailable native events: use cache-only recovery reads.
      }
      if (generation !== this.realtimeGeneration) return
      this.pollTimer = setInterval(() => {
        this.now = Date.now()
        // Healthy streams send a heartbeat every 15 s. Only recover via HTTP when stale.
        // When there is an error or the trader data is not yet synchronized, retry more actively (5s).
        const hasError = Boolean(this.errorMsg) || !this.bridgeOnline || !this.account
        const staleTimeout = hasError ? 5000 : 45000
        const minInterval = hasError ? 5000 : 15000
        if (this.now - this.lastPushAt > staleTimeout && this.now - this.lastRecoveryAt > minInterval) {
          this.lastRecoveryAt = this.now
          void this.refresh()
        }
      }, 1000)
    },

    stopRealtime() {
      this.realtimeGeneration++
      if (this.pollTimer) clearInterval(this.pollTimer)
      this.pollTimer = null
      this.realtimeListeners.splice(0).forEach((unlisten) => unlisten())
    },

    async confirmCondition(id: string) {
      try {
        await api.confirmConditionOrder(id)
        notify.success('开仓任务已确认，等待柜台委托和成交回报')
        await this.refresh()
      } catch (e: any) {
        notify.error(`开仓确认失败: ${e}`)
      }
    },

    async cancelCondition(id: string) {
      try {
        await api.cancelConditionOrder(id)
        notify.success('条件单已撤销')
        await this.refresh()
      } catch (e: any) {
        notify.error(`撤销失败: ${e}`)
      }
    },

    async setEntryRiskFilter(ticks: number) {
      if (!this.config || this.updatingEntryFilter || ![0, 10].includes(ticks)) return
      this.updatingEntryFilter = true
      const next = { ...this.config, min_entry_risk_ticks: ticks }
      try {
        await api.updateTraderConfig(next)
        this.config = next
        notify.success(ticks ? '实际新开仓启用10 tick过滤；原规则继续影子观察' : '实际新开仓采用原规则；10 tick组继续影子观察')
      } catch (e: any) {
        notify.error(`修改入场过滤失败: ${e}`)
      } finally {
        this.updatingEntryFilter = false
      }
    },

    async setMode(mode: 'MonitorOnly' | 'SemiAuto' | 'FullAuto') {
      const current = this.config || {
        mode: 'SemiAuto',
        max_risk_percent: 1.0,
        default_large_volume: 2,
        default_small_volume: 1,
        daily_loss_limit_r: 3.0,
      }
      const nextCfg = { ...current, mode }
      this.config = nextCfg
      localStorage.setItem('ntrend_trader_mode', mode)
      try {
        await api.updateTraderConfig(nextCfg)
        const label = mode === 'FullAuto' ? '全自动执行' : mode === 'SemiAuto' ? '半自动确认' : '仅监控'
        notify.success(`已切换交易执行模式为: ${label}`)
      } catch (e: any) {
        notify.error(`修改执行模式失败: ${e}`)
      }
    },

    async emergencyCancelAll() {
      try {
        await api.emergencyCancelAll()
        notify.warning('已向柜台发送全撤指令')
        await this.refresh()
      } catch (e: any) {
        notify.error(`紧急全撤失败: ${e}`)
      }
    },

    async emergencyCloseAll() {
      try {
        await api.emergencyCloseAll()
        notify.error('已触发紧急一键全平！')
        await this.refresh()
      } catch (e: any) {
        notify.error(`一键全平失败: ${e}`)
      }
    },
  },
})
