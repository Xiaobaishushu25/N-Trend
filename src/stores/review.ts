import { defineStore } from 'pinia'
import { api } from '../services/api'
import type { OutcomeDetail, RecentOutcomeFilters, ReviewStats, V2ModelRow, V2PredictionRow, V2ReportBundle } from '../types'
import { defaultReviewFilters, validateReviewFilters } from '../utils/reviewFilters'

let pageRequest = 0
const filterKey = 'ntrend.review-browser.filters.v1'
function savedFilters(): RecentOutcomeFilters {
  try { return { ...defaultReviewFilters(), ...JSON.parse(localStorage.getItem(filterKey) || '{}'), pageOffset: undefined } }
  catch { return defaultReviewFilters() }
}

export const REVIEW_DIMENSIONS = [
  { key: 'score_band', label: '评分段' },
  { key: 'grade', label: '结构等级' },
  { key: 'direction', label: '方向' },
  { key: 'level', label: '级别' },
  { key: 'hour', label: '小时' },
  { key: 'symbol', label: '品种' },
  { key: 'symbol_hour', label: '品种×小时' },
  { key: 'exit_reason', label: '出场原因' },
  { key: 'vol_confirm', label: '量能确认' },
  { key: 'vol_band', label: '放量分桶' },
  { key: 'trigger_lag', label: '预警延迟' },
  { key: 'overshoot', label: '追价深度' },
  { key: 'gap_combo', label: '跳空成交' },
  { key: 'retracement', label: '回撤率' },
  { key: 'b_a_speed', label: 'b/a速度比' },
  { key: 'dim_a', label: '评分·A腿' },
  { key: 'dim_b', label: '评分·B腿' },
  { key: 'p_win_band', label: "P(win)" },
  { key: 'dim_warning', label: '评分·预警K线' },
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
    v2Models: [] as V2ModelRow[],
    v2Report: null as V2ReportBundle | null,
    v2Predictions: [] as V2PredictionRow[],
    v2SelectedModel: '' as string,
    loading: false,
    refreshing: false,
    recentLoading: false,
    browserFilters: savedFilters(),
    browserRows: [] as OutcomeDetail[],
    browserTotal: 0,
    browserScored: 0,
    browserUnscored: 0,
    browserSymbols: [] as string[],
    browserPage: 1,
    browserPageSize: 80,
    browserLoading: false,
    browserError: '',
  }),
  actions: {
    async loadReviewPage() {
      const request = ++pageRequest
      const invalid = validateReviewFilters(this.browserFilters)
      if (invalid) { this.browserError = invalid; this.browserLoading = false; this.browserRows = []; this.browserTotal = 0; this.browserScored = 0; this.browserUnscored = 0; return }
      const filters = { ...this.browserFilters, symbols: [...(this.browserFilters.symbols || [])] }
      const page = this.browserPage
      this.browserLoading = true
      this.browserError = ''
      this.browserRows = []
      try {
        const result = await api.getReviewPage(this.browserPageSize, (page - 1) * this.browserPageSize, filters)
        if (request !== pageRequest) return
        // Older servers ignore `paged=true` and return the legacy row array.
        // Validate before changing reactive state: undefined symbols break rendering
        // before Vue can remove the table's loading overlay.
        if (Array.isArray(result)) {
          throw new Error('版本不匹配：复盘接口返回了旧版数据。请将客户端和服务端同时升级到同一最新版本后重新加载。')
        }
        if (!result || !Array.isArray(result.items) || !Array.isArray(result.symbols)
          || !result.symbols.every(symbol => typeof symbol === 'string')
          || ![result.total, result.scored, result.unscored].every(count => Number.isSafeInteger(count) && count >= 0)
          || result.scored + result.unscored !== result.total) {
          throw new Error('复盘接口返回的数据格式不完整，已停止加载。请将客户端和服务端同时升级到同一最新版本后重新加载。')
        }
        this.browserRows = result.items
        this.browserTotal = result.total
        this.browserScored = result.scored
        this.browserUnscored = result.unscored
        this.browserSymbols = result.symbols
        try { localStorage.setItem(filterKey, JSON.stringify(filters)) } catch { /* session remains usable */ }
      } catch (e) {
        if (request === pageRequest) { this.browserError = String(e); this.browserTotal = 0; this.browserScored = 0; this.browserUnscored = 0 }
      } finally {
        if (request === pageRequest) this.browserLoading = false
      }
    },
    updateBrowserFilterDraft(patch: Partial<RecentOutcomeFilters>) {
      ++pageRequest
      this.browserFilters = { ...this.browserFilters, ...patch, pageOffset: undefined }
      this.browserPage = 1
      this.browserRows = []
      this.browserLoading = true
      this.browserError = ''
    },
    async setBrowserFilters(patch: Partial<RecentOutcomeFilters>) {
      this.updateBrowserFilterDraft(patch)
      await this.loadReviewPage()
    },
    async resetBrowserFilters() {
      this.browserFilters = defaultReviewFilters()
      this.browserPage = 1
      await this.loadReviewPage()
    },
    async load(dim?: string, scope?: StatsScopeKey) {
      this.dimension = dim ?? this.dimension
      this.statsScope = scope ?? this.statsScope
      this.loading = true
      try {
        const [stats, v2Models, v2Report] = await Promise.all([
          api.getReviewStats(
            this.dimension,
            this.statsScope,
            this.recentFilters.version,
            this.recentFilters.scoreMin,
            this.recentFilters.scoreMax,
          ),
          api.getV2Models().catch(() => [] as V2ModelRow[]),
          api.getV2Report().catch(() => null as unknown as V2ReportBundle),
        ])
        await this.loadRecent()
        this.stats = stats as ReviewStats
        this.v2Models = (v2Models as V2ModelRow[]) ?? []
        this.v2Report = (v2Report as V2ReportBundle) ?? null
        if (!this.v2Models.some((model) => model.model_id === this.v2SelectedModel)) {
          this.v2SelectedModel =
            this.v2Models.find((model) => model.model_id.startsWith('logistic-'))?.model_id
            ?? this.v2Models[0]?.model_id
            ?? ''
        }
        if (this.v2SelectedModel) await this.loadV2Predictions()
      } finally {
        this.loading = false
      }
    },
    async loadV2Predictions(modelId?: string) {
      this.v2SelectedModel = modelId ?? this.v2SelectedModel
      try { this.v2Predictions = await api.getV2Predictions(this.v2SelectedModel || null) } catch { this.v2Predictions = [] }
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
    /** 设置自定义评分区间并同时刷新分组统计与明细 */
    async setScoreRange(min: number | null, max: number | null) {
      this.recentFilters = { ...this.recentFilters, scoreMin: min, scoreMax: max }
      await this.load()
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
