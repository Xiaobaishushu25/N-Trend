import { defineStore } from 'pinia'
import { api } from '../services/api'
import { notify } from '../utils/notify'
import type {
  CtpHealth,
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
    statistics: null as LiveTradeStatistics | null,
    loading: false,
    refreshing: false,
    activeTab: 'positions' as LiveTraderTab,
    filterReason: '' as string,
    errorMsg: null as string | null,
    pollTimer: null as any | null,
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
        margin: number
        is_broker: boolean
        managed?: LiveManagedPosition
      }> = []

      for (const bp of this.brokerPositions) {
        if (bp.position <= 0) continue
        const managed = this.managedPositions.find(
          (m) => m.instrument_id.toLowerCase() === bp.instrument_id.toLowerCase() && !m.is_closed,
        )
        // 合约乘数估算开仓均价（若铅pb为5，大部分为10）
        const inst = bp.instrument_id.toLowerCase()
        const mult = inst.startsWith('pb') || inst.startsWith('cu') || inst.startsWith('al') || inst.startsWith('zn') ? 5 : 10
        const avgPrice = bp.position > 0 && bp.open_cost > 0 ? bp.open_cost / (bp.position * mult) : 0
        list.push({
          instrument_id: bp.instrument_id,
          direction: bp.direction,
          position: bp.position,
          today_position: bp.today_position,
          yd_position: bp.yd_position,
          open_cost: bp.open_cost,
          avg_price: avgPrice,
          position_profit: bp.position_profit,
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
        (c) => c.state === 'Pending' || c.state === 'WaitingConfirm' || c.state === 'Triggered',
      )
    },
    waitingConfirmConditions(): LiveConditionOrder[] {
      return this.conditions.filter((c) => c.state === 'WaitingConfirm')
    },
    filteredHistory(): LiveTradeRecord[] {
      if (!this.filterReason) return this.history
      return this.history.filter((h) => h.exit_reason === this.filterReason)
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

    async refresh() {
      try {
        this.refreshing = true
        // 1. 获取 CTP 状态与运行配置
        try {
          const res: any = await api.getTraderStatus()
          const payload = res?.data !== undefined ? res.data : res
          if (payload) {
            this.health = payload.health || null
            if (payload.config) {
              this.config = payload.config
            }
          }
        } catch (e: any) {
          this.errorMsg = typeof e === 'string' ? e : e?.message || '获取交易服务状态失败'
        }

        // 2. 获取资金账户
        try {
          const res: any = await api.getTraderAccount()
          const payload = res?.data !== undefined ? res.data : res
          if (payload) {
            if (payload.margin === undefined && payload.curr_margin !== undefined) {
              payload.margin = payload.curr_margin
            }
            if (payload.risk_ratio === undefined && (payload.balance || 0) > 0) {
              payload.risk_ratio = (payload.margin || 0) / payload.balance
            }
            this.account = payload
          }
        } catch {}

        // 3. 获取持仓（柜台持仓 + 策略托管持仓）
        try {
          const res: any = await api.getTraderPositions()
          const payload = res?.data !== undefined ? res.data : res
          if (payload) {
            if (Array.isArray(payload)) {
              this.brokerPositions = payload
              this.managedPositions = []
            } else {
              this.brokerPositions = payload.broker_positions || []
              this.managedPositions = payload.managed_positions || []
            }
          }
        } catch {}

        // 4. 获取合成条件单列表
        try {
          const res: any = await api.getConditionOrders()
          const payload = res?.data !== undefined ? res.data : res
          if (Array.isArray(payload)) {
            this.conditions = payload
          }
        } catch {}

        // 5. 获取平仓历史明细与多维统计
        try {
          const resHist: any = await api.getTradeHistory()
          const payloadHist = resHist?.data !== undefined ? resHist.data : resHist
          if (Array.isArray(payloadHist)) {
            this.history = payloadHist
          }
          const resStats: any = await api.getTradeStatistics()
          const payloadStats = resStats?.data !== undefined ? resStats.data : resStats
          if (payloadStats) {
            this.statistics = payloadStats
          }
        } catch {}
      } finally {
        this.refreshing = false
      }
    },

    startPolling(intervalMs = 3000) {
      if (this.pollTimer) clearInterval(this.pollTimer)
      this.pollTimer = setInterval(() => {
        void this.refresh()
      }, intervalMs)
    },

    stopPolling() {
      if (this.pollTimer) {
        clearInterval(this.pollTimer)
        this.pollTimer = null
      }
    },

    async confirmCondition(id: string) {
      try {
        await api.confirmConditionOrder(id)
        notify.success('开仓确认成功，委托已送达柜台')
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
