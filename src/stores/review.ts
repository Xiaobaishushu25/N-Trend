import { defineStore } from 'pinia'
import { api } from '../services/api'
import type { OutcomeDetail, RecentOutcomeFilters, ReviewStats } from '../types'

export const REVIEW_DIMENSIONS = [
  { key: 'score_band', label: '评分段' },
  { key: 'grade', label: '结构等级' },
  { key: 'direction', label: '方向' },
  { key: 'level', label: '级别' },
  { key: 'hour', label: '小时' },
  { key: 'symbol', label: '品种' },
  { key: 'vol_confirm', label: '量能确认' },
  { key: 'oi', label: '持仓量' },
  { key: 'trend60', label: '60m趋势分' },
  { key: 'symbol_hour', label: '品种×小时' },
  { key: 'score_vol', label: '评分×量能' },
  { key: 'hour_atr', label: '小时×波动' },
  { key: 'exit_reason', label: '出场原因' },
  { key: 'vol_band', label: '放量分桶' },
  { key: 'b_vol', label: 'b段缩量' },
  { key: 'retracement', label: '回撤率' },
  { key: 'b_a_speed', label: 'b/a速度比' },
  { key: 'a_strength', label: 'a段强度' },
  { key: 'trigger_lag', label: '预警延迟' },
  { key: 'overshoot', label: '追价深度' },
  { key: 'tp_tier', label: '止盈层级' },
  { key: 'gap_combo', label: '跳空成交' },
  { key: 'dim_trend', label: '评分·趋势' },
  { key: 'dim_a', label: '评分·A腿' },
  { key: 'dim_b', label: '评分·B腿' },
  { key: 'dim_trigger', label: '评分·触发' },
  { key: 'dim_rr', label: '评分·盈亏比' },
  { key: 'dim_momentum', label: '评分·动量' },
] as const

export const REVIEW_STATS_SCOPES = [
  { key: 'all', label: '全部信号' },
  { key: 'tradable', label: '仅可交易' },
  { key: 'standard', label: '仅标准仓' },
] as const

export type StatsScopeKey = (typeof REVIEW_STATS_SCOPES)[number]['key']

export const useReviewStore = defineStore('review', {
  state: () => ({
    dimension: 'score_band' as string,
    statsScope: 'tradable' as StatsScopeKey,
    stats: null as ReviewStats | null,
    recent: [] as OutcomeDetail[],
    recentFilters: {
      symbol: '',
      // 下拉筛选用 null 作为"未选择"，naive-ui 才会显示 placeholder
      version: null,
      direction: null,
      level: null,
      grade: null,
      outcome: null,
      scoreMin: null,
      scoreMax: null,
    } as RecentOutcomeFilters,
    loading: false,
    refreshing: false,
    recentLoading: false,
  }),
  actions: {
    async load(dim?: string, scope?: StatsScopeKey) {
      this.dimension = dim ?? this.dimension
      this.statsScope = scope ?? this.statsScope
      this.loading = true
      try {
        const [stats] = await Promise.all([
          api.getReviewStats(
            this.dimension,
            this.statsScope,
            this.recentFilters.version,
          ),
          this.loadRecent(),
        ])
        this.stats = stats
      } finally {
        this.loading = false
      }
    },
    /** 只刷新明细（保留当前筛选） */
    async loadRecent() {
      this.recentLoading = true
      try {
        this.recent = await api.getRecentOutcomes(2000, this.recentFilters)
      } finally {
        this.recentLoading = false
      }
    },
    /** 更新筛选条件并重新请求明细 */
    async setRecentFilter(patch: Partial<RecentOutcomeFilters>) {
      this.recentFilters = { ...this.recentFilters, ...patch }
      await this.loadRecent()
    },
    /** 清空明细筛选 */
    async resetRecentFilters() {
      this.recentFilters = {
        symbol: '',
        version: null,
        direction: null,
        level: null,
        grade: null,
        outcome: null,
        scoreMin: null,
        scoreMax: null,
      }
      await this.load()
    },
    /** 先回填未终结信号的结果，再重新拉取统计 */
    async refresh() {
      this.refreshing = true
      try {
        await api.refreshOutcomesNow()
        await this.load()
      } finally {
        this.refreshing = false
      }
    },
  },
})

export function fmtPct(v: number | null | undefined): string {
  return v == null ? '—' : `${(v * 100).toFixed(1)}%`
}

export function fmtR(v: number | null | undefined): string {
  return v == null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}R`
}
