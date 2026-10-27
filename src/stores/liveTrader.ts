import { defineStore } from 'pinia'
import { api, onQuotesUpdated } from '../services/api'
import { listen, type UnlistenFn } from '@tauri-apps/api/event'
import { DEFAULT_MULTIPLIERS, getSymbolPrefix } from '../utils/multipliers'
import { notify } from '../utils/notify'
import { isMarketTradingWindow } from '../utils/tradingHours'
import { filteredConditionRows } from '../utils/conditionPool'
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
  ExecutionOrder,
  TraderInstanceCatalog,
  SimnowLoginInput,
  StrategyDefinition,
  StrategyDiagnostics,
  ConditionPoolRow,
} from '../types'

export type LiveTraderTab = 'positions' | 'conditions' | 'history' | 'comparison' | 'orders'

export const useLiveTraderStore = defineStore('liveTrader', {
  state: () => ({
    selectedInstanceId: 'default',
    instanceCatalog: null as TraderInstanceCatalog | null,
    instancesSupported: false,
    instancesLoading: false,
    instancesError: '',
    instanceGeneration: 0,
    instanceStreamId: '',
    instanceRevision: 0,
    health: null as CtpHealth | null,
    config: null as ExecutionConfig | null,
    account: null as TraderAccount | null,
    brokerPositions: [] as LiveBrokerPosition[],
    managedPositions: [] as LiveManagedPosition[],
    conditions: [] as LiveConditionOrder[],
    history: [] as LiveTradeRecord[],
    executionOrders: [] as ExecutionOrder[],
    executionOrdersTotal: 0,
    executionOrdersPage: 1,
    executionOrdersPageSize: 20,
    executionOrdersLoading: false,
    executionOrdersError: '',
    executionOrdersSeq: 0,
    statistics: null as LiveTradeStatistics | null,
    strategyComparison: null as LiveTraderSnapshot['strategy_comparison'],
    currentStrategy: null as StrategyDefinition | null,
    strategyDiagnostics: null as StrategyDiagnostics | null,
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
    isTradingWindow(): boolean {
      return isMarketTradingWindow(new Date(this.now))
    },
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
      const activeStates = new Set([
        'pending', 'waiting_confirm', 'triggered', 'unknown', 'working', 'part_filled', 'closing', 'rejected',
        'Pending', 'WaitingConfirm', 'Triggered', 'Unknown', 'Working', 'PartFilled', 'Closing', 'Rejected',
      ])
      const list = this.conditions.filter((c) => activeStates.has(c.state))
      return list.sort((a, b) => {
        const timeA = a.trigger_ts || a.warning_bar_ts || a.created_at || 0
        const timeB = b.trigger_ts || b.warning_bar_ts || b.created_at || 0
        if (timeA && timeB) {
          if (typeof timeB === 'string' && typeof timeA === 'string') {
            const cmp = timeB.localeCompare(timeA)
            if (cmp !== 0) return cmp
          } else {
            const numA = typeof timeA === 'number' ? timeA : Number(new Date(timeA)) || 0
            const numB = typeof timeB === 'number' ? timeB : Number(new Date(timeB)) || 0
            if (numB !== numA) return numB - numA
          }
        }
        return 0
      })
    },
    filteredConditions(): ConditionPoolRow[] {
      return filteredConditionRows(this.strategyDiagnostics, this.currentStrategy?.revision)
    },
    conditionPool(): ConditionPoolRow[] {
      const time = (row: ConditionPoolRow) => row.trigger_ts || row.warning_bar_ts || row.created_at || 0
      return [...this.activeConditions, ...this.filteredConditions].sort((a, b) => {
        const ta = time(a), tb = time(b)
        if (typeof ta === 'string' && typeof tb === 'string') return tb.localeCompare(ta)
        const epoch = (t: string | number) => typeof t === 'number' ? t * 1000 : Date.parse(t) || 0
        return epoch(tb) - epoch(ta)
      })
    },
    waitingConfirmConditions(): LiveConditionOrder[] {
      return this.conditions.filter((c) => c.state === 'waiting_confirm' || c.state === 'WaitingConfirm')
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
    resolveEventId(): (item?: { event_id?: number | null; condition_order_id?: string; signal_evidence?: unknown } | null) => number | null {
      return (item) => {
        if (!item) return null
        if (item.signal_evidence) return null
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
      await this.loadInstances()
      await this.refresh()
      this.loading = false
    },

    async loadInstances() {
      if (this.instancesLoading) return
      if (typeof api.traderInstanceRequest !== 'function') {
        this.instancesError = '当前客户端没有多账户接口，请更新并重新启动客户端。'
        return
      }
      this.instancesLoading = true
      this.instancesError = ''
      try {
        const catalog = await api.traderInstanceRequest<TraderInstanceCatalog>('list')
        if (!catalog || !Array.isArray(catalog.instances)) throw new Error('服务端返回的账户列表格式不正确')
        this.instanceCatalog = catalog
        this.instancesSupported = true
      } catch (e: any) {
        const message = typeof e === 'string' ? e : e?.message || String(e)
        if (/\b404\b/.test(message)) {
          this.instancesError = '当前连接的服务端尚未提供多账户接口，需要更新服务端后使用。'
        } else if (/trader_instance_request.*not found|unknown command/i.test(message)) {
          this.instancesError = '当前客户端尚未加载多账户命令，请重新编译并启动客户端。'
        } else {
          this.instancesError = `多账户信息加载失败：${message}`
        }
        // Keep the selected instance and its routing on transient errors.
      } finally {
        this.instancesLoading = false
      }
    },
    async selectInstance(id: string) {
      if (id === this.selectedInstanceId) return
      this.selectedInstanceId = id
      this.instanceGeneration++
      this.pushRevision++
      this.refreshing = false
      this.health = null; this.account = null; this.config = null
      this.brokerPositions = []; this.managedPositions = []; this.conditions = []; this.history = []
      this.statistics = null; this.strategyComparison = null; this.quotes = {}
      this.currentStrategy = null; this.strategyDiagnostics = null
      this.executionOrdersSeq++; this.executionOrders = []; this.executionOrdersTotal = 0
      this.executionOrdersPage = 1; this.executionOrdersError = ''; this.executionOrdersLoading = false
      this.errorMsg = null; this.bridgeOnline = false; this.lastPushAt = 0
      this.instanceStreamId = ''; this.instanceRevision = 0
      await this.refresh()
      if (this.activeTab === 'orders') await this.loadExecutionOrders()
    },
    async instanceAction(action: Record<string, unknown>) {
      const id = this.selectedInstanceId
      const result = await api.traderInstanceRequest('action', id, action)
      if (id === this.selectedInstanceId) { await this.loadInstances(); await this.refresh() }
      return result
    },
    async loginSimnow(id: string, credentials: SimnowLoginInput) {
      const result = await api.traderInstanceRequest('login', id, credentials)
      if (id === this.selectedInstanceId) {
        this.health = null; this.account = null; this.bridgeOnline = false
        this.brokerPositions = []; this.errorMsg = null
        this.instanceGeneration++; this.pushRevision++
        this.refreshing = false
        await this.loadInstances()
        await this.refresh()
      }
      return result
    },
    applyInstanceDelta(payload: { instance_id: string; delta: { stream_id: string; base_revision: number; revision: number; changes: Partial<LiveTraderSnapshot> } }) {
      if (payload.instance_id !== this.selectedInstanceId) return
      const delta = payload.delta
      if (this.instanceStreamId !== delta.stream_id || delta.base_revision !== this.instanceRevision || delta.revision !== delta.base_revision + 1) {
        void this.refresh()
        return
      }
      const baseline: LiveTraderSnapshot = {
        instance_id: this.selectedInstanceId, source: 'server', health: this.health, account: this.account,
        broker_positions: this.brokerPositions, config: this.config || undefined,
        managed_positions: this.managedPositions, conditions: this.conditions, history: this.history,
        statistics: this.statistics, strategy_comparison: this.strategyComparison,
        strategy: this.currentStrategy || undefined, strategy_diagnostics: this.strategyDiagnostics,
        bridge_online: this.bridgeOnline, execution_error: this.errorMsg,
        _stream_id: delta.stream_id, _revision: delta.revision,
      }
      this.applySnapshot({ ...baseline, ...delta.changes })
      this.pushRevision++; this.lastPushAt = Date.now()
    },
    applySnapshot(snapshot: LiveTraderSnapshot) {
      if ((snapshot.instance_id || 'default') !== this.selectedInstanceId) return
      if (snapshot._stream_id && snapshot._stream_id === this.instanceStreamId && (snapshot._revision ?? 0) < this.instanceRevision) return
      if (snapshot._stream_id) { this.instanceStreamId = snapshot._stream_id; this.instanceRevision = snapshot._revision ?? 0 }
      if (snapshot.source && snapshot.source !== this.snapshotSource) {
        this.quotes = {}
        this.strategyComparison = null
        this.currentStrategy = null; this.strategyDiagnostics = null
        this.executionOrdersSeq++
        this.executionOrders = []
        this.executionOrdersTotal = 0
        this.executionOrdersPage = 1
        this.executionOrdersError = ''
        this.executionOrdersLoading = false
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
      if (snapshot.source !== 'local') {
        this.currentStrategy = snapshot.strategy || null
        this.strategyDiagnostics = snapshot.strategy_diagnostics || null
      }
      this.errorMsg = snapshot.execution_error || snapshot.health?.sync_error || null
    },

    async loadExecutionOrders(page?: number) {
      const requestedPage = page ?? this.executionOrdersPage
      const seq = ++this.executionOrdersSeq
      this.executionOrdersLoading = true
      this.executionOrdersError = ''
      try {
        const result = this.instancesSupported
          ? await api.traderInstanceRequest<{ items: ExecutionOrder[]; total: number; page: number; page_size: number }>('orders', this.selectedInstanceId, { page: requestedPage, page_size: this.executionOrdersPageSize })
          : await api.getExecutionOrders(requestedPage, this.executionOrdersPageSize)
        if (seq !== this.executionOrdersSeq) return
        this.executionOrders = result.items
        this.executionOrdersTotal = result.total
        this.executionOrdersPage = result.page
        this.executionOrdersPageSize = result.page_size
      } catch (e: any) {
        if (seq !== this.executionOrdersSeq) return
        this.executionOrdersError = typeof e === 'string' ? e : e?.message || '报单记录加载失败'
      } finally {
        if (seq === this.executionOrdersSeq) this.executionOrdersLoading = false
      }
    },

    async refresh() {
      if (this.refreshing) return
      this.refreshing = true
      const revision = this.pushRevision
      const generation = this.instanceGeneration
      try {
        const res: any = this.instancesSupported
          ? await api.traderInstanceRequest<LiveTraderSnapshot>('snapshot', this.selectedInstanceId)
          : await api.getTraderSnapshot()
        if (generation !== this.instanceGeneration) return
        if (revision !== this.pushRevision) return // A newer push supersedes this HTTP response.
        if (res?.success === false) throw new Error(res.error || '交易快照尚未就绪')
        const snapshot = res?.data !== undefined ? res.data : res
        if (snapshot) this.applySnapshot(snapshot)
      } catch (e: any) {
        if (revision !== this.pushRevision || generation !== this.instanceGeneration) return
        this.errorMsg = typeof e === 'string' ? e : e?.message || '获取交易快照失败'
        this.bridgeOnline = false
        if (this.health) this.health = { ...this.health, ready: false, healthy: false }
      } finally {
        if (generation === this.instanceGeneration) this.refreshing = false
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
          if (this.selectedInstanceId !== 'default') return
          this.pushRevision++
          this.lastPushAt = Date.now()
          this.applySnapshot(payload)
        }))
        await add(listen<{ instance_id: string; delta: any }>('trader.instance.delta', ({ payload }) => this.applyInstanceDelta(payload)))
        await add(listen<{ instance_id: string; heartbeat: { stream_id: string; revision: number } }>('trader.instance.heartbeat', ({ payload }) => {
          if (payload.instance_id !== this.selectedInstanceId) return
          if (payload.heartbeat.stream_id !== this.instanceStreamId || payload.heartbeat.revision !== this.instanceRevision) { void this.refresh(); return }
          this.lastPushAt = Date.now()
        }))
        await add(listen('trader-heartbeat', () => {
          if (this.selectedInstanceId !== 'default') return
          this.lastPushAt = Date.now()
          if ((!this.bridgeOnline || this.errorMsg || !this.account) && isMarketTradingWindow(new Date(this.now))) {
            void this.refresh()
          }
        }))
        await add(listen('trader-resync-needed', () => { void this.refresh() }))
        await add(listen<string>('connection-status-changed', ({ payload }) => {
          if (payload === 'connected') void this.loadInstances()
          if (payload !== 'connected' && this.snapshotSource !== 'local') {
            this.lastPushAt = 0
            this.bridgeOnline = false
            if (this.health) this.health = { ...this.health, ready: false, healthy: false }
          }
        }))
        // Native transport reconstructs versioned trader deltas before emitting.
        // Global sequence gaps from chart filtering do not require a trader read.
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
        const isTrading = isMarketTradingWindow(new Date(this.now))
        const hasError = Boolean(this.errorMsg) || !this.bridgeOnline || !this.account
        const staleTimeout = !isTrading ? 60000 : hasError ? 5000 : 45000
        const minInterval = !isTrading ? 60000 : hasError ? 5000 : 15000
        if (this.now - this.lastPushAt > staleTimeout && this.now - this.lastRecoveryAt > minInterval) {
          this.lastRecoveryAt = this.now
          void this.refresh()
        }
      }, 1000)
    },

    stopRealtime() {
      this.realtimeGeneration++
      this.executionOrdersSeq++
      this.executionOrdersLoading = false
      if (this.pollTimer) clearInterval(this.pollTimer)
      this.pollTimer = null
      this.realtimeListeners.splice(0).forEach((unlisten) => unlisten())
    },

    async confirmCondition(id: string) {
      try {
        if (this.instancesSupported) await this.instanceAction({ action: 'confirm', condition_id: id })
        else await api.confirmConditionOrder(id)
        notify.success('开仓任务已确认，等待柜台委托和成交回报')
        await this.refresh()
      } catch (e: any) {
        notify.error(`开仓确认失败: ${e}`)
      }
    },

    async cancelCondition(id: string) {
      try {
        if (this.instancesSupported) await this.instanceAction({ action: 'cancel', condition_id: id })
        else await api.cancelConditionOrder(id)
        notify.success('条件单已撤销')
        await this.refresh()
      } catch (e: any) {
        notify.error(`撤销失败: ${e}`)
      }
    },

    async setEntryRiskFilter(ticks: number) {
      if (!this.config || this.updatingEntryFilter || ![0, 10].includes(ticks)) return
      this.updatingEntryFilter = true
      const requestedInstance = this.selectedInstanceId
      const next = { ...this.config, min_entry_risk_ticks: ticks }
      try {
        if (this.instancesSupported) await this.instanceAction({ action: 'configure', config: next })
        else await api.updateTraderConfig(next)
        if (requestedInstance !== this.selectedInstanceId) return
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
      const requestedInstance = this.selectedInstanceId
      const nextCfg = { ...current, mode }
      try {
        if (this.instancesSupported) await this.instanceAction({ action: 'configure', config: nextCfg })
        else await api.updateTraderConfig(nextCfg)
        if (requestedInstance !== this.selectedInstanceId) return
        this.config = nextCfg
        localStorage.setItem(this.instancesSupported ? `ntrend_trader_mode:${requestedInstance}` : 'ntrend_trader_mode', mode)
        const label = mode === 'FullAuto' ? '全自动执行' : mode === 'SemiAuto' ? '半自动确认' : '仅监控'
        notify.success(`已切换交易执行模式为: ${label}`)
      } catch (e: any) {
        notify.error(`修改执行模式失败: ${e}`)
      }
    },

    async emergencyCancelAll() {
      try {
        if (this.instancesSupported) await this.instanceAction({ action: 'cancel_all' })
        else await api.emergencyCancelAll()
        notify.warning('已向柜台发送全撤指令')
        await this.refresh()
      } catch (e: any) {
        notify.error(`紧急全撤失败: ${e}`)
      }
    },

    async emergencyCloseAll() {
      try {
        if (this.instancesSupported) await this.instanceAction({ action: 'close_all' })
        else await api.emergencyCloseAll()
        notify.error('已触发紧急一键全平！')
        await this.refresh()
      } catch (e: any) {
        notify.error(`一键全平失败: ${e}`)
      }
    },
  },
})
