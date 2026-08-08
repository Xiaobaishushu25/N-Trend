import { defineStore } from 'pinia'
import { api } from '../services/api'
import type { OutcomeDetail, ReviewStats } from '../types'

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

export const useReviewStore = defineStore('review', {
  state: () => ({
    dimension: 'score_band' as string,
    stats: null as ReviewStats | null,
    recent: [] as OutcomeDetail[],
    loading: false,
    refreshing: false,
  }),
  actions: {
    async load(dim?: string) {
      this.dimension = dim ?? this.dimension
      this.loading = true
      try {
        const [stats, recent] = await Promise.all([
          api.getReviewStats(this.dimension),
          api.getRecentOutcomes(100),
        ])
        this.stats = stats
        this.recent = recent
      } finally {
        this.loading = false
      }
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
