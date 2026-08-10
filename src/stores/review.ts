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
          api.getReviewStats(this.dimension, this.statsScope),
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
        direction: null,
        level: null,
        grade: null,
        outcome: null,
        scoreMin: null,
        scoreMax: null,
      }
      await this.loadRecent()
    },
    /** 先回填未终结信号的结局，再重新拉取统计 */
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
