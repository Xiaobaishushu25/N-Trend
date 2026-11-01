import { defineStore } from 'pinia'
import { api } from '../services/api'
import type { RecentOutcomeFilters } from '../types'
import type { ReviewMetadata, ReviewQuery, ReviewQueryResponse, ReviewSummary } from '../types/review'
import { defaultReviewFilters, validateReviewFilters } from '../utils/reviewFilters'
import { publishedQuery, validatePublishedPage } from '../utils/reviewPresentation'

const filterKey = 'ntrend.published-review.filters.v1'
function savedFilters(): RecentOutcomeFilters {
  try { return { ...defaultReviewFilters(), ...JSON.parse(localStorage.getItem(filterKey) || '{}') } } catch { return defaultReviewFilters() }
}
let request = 0
export const usePublishedReviewStore = defineStore('published-review', {
  state: () => ({
    browserFilters: savedFilters(), browserRows: [] as ReviewSummary[], browserTotal: 0, browserScored: 0, browserUnscored: 0,
    browserSymbols: [] as string[], browserPage: 1, browserPageSize: 80, browserLoading: false, browserError: '',
    currentSnapshotId: '', query: null as ReviewQuery | null, result: null as ReviewQueryResponse | null, metadata: null as ReviewMetadata | null,
  }),
  actions: {
    async loadReviewPage(reuseSnapshot = false) {
      const ticket = ++request
      const invalid = validateReviewFilters(this.browserFilters)
      this.browserRows = []; this.browserError = ''; this.browserLoading = true
      try {
        if (invalid) throw new Error(invalid)
        const query = publishedQuery(this.browserFilters, this.browserPageSize)
        let offset = (this.browserPage - 1) * this.browserPageSize
        const canReuse = reuseSnapshot && this.currentSnapshotId && this.query && JSON.stringify(this.query) === JSON.stringify(query)
        if (!canReuse || !this.metadata) {
          const meta = await api.reviewMetadata()
          if (ticket !== request) return
          this.metadata = meta; this.browserSymbols = meta.symbols
        }
        if (canReuse) {
          const page = await api.reviewItems(this.currentSnapshotId, { cursor: offset, limit: this.browserPageSize })
          if (ticket !== request) return
          validatePublishedPage(page)
          if (page.snapshot_id !== this.currentSnapshotId || page.revision !== this.result?.revision || page.total !== this.browserTotal) throw new Error('复盘分页版本不一致，请刷新')
          this.browserRows = page.items
        } else {
          const result = await api.reviewQuery(query)
          if (ticket !== request) return
          validatePublishedPage(result)
          if (!Number.isSafeInteger(result.scored) || !Number.isSafeInteger(result.unscored) || result.scored + result.unscored !== result.total) throw new Error('评分覆盖计数不完整')
          this.currentSnapshotId = result.snapshot_id; this.query = query; this.result = result
          this.browserTotal = result.total; this.browserScored = result.scored; this.browserUnscored = result.unscored
          if (offset >= result.total) {
            this.browserPage = Math.max(1, Math.ceil(result.total / this.browserPageSize))
            offset = (this.browserPage - 1) * this.browserPageSize
          }
          if (offset) {
            const page = await api.reviewItems(result.snapshot_id, { cursor: offset, limit: this.browserPageSize })
            if (ticket !== request) return
            validatePublishedPage(page)
            if (page.snapshot_id !== result.snapshot_id || page.revision !== result.revision || page.total !== result.total) throw new Error('复盘分页版本不一致，请刷新')
            this.browserRows = page.items
          } else this.browserRows = result.items
        }
        try { localStorage.setItem(filterKey, JSON.stringify(this.browserFilters)) } catch { /* storage is optional */ }
      } catch (e) {
        if (ticket === request) { this.browserRows = []; this.browserError = `${String(e)}；快照失效时请刷新，筛选条件将保留。`; this.browserTotal = 0; this.browserScored = 0; this.browserUnscored = 0; this.currentSnapshotId = ''; this.result = null }
      } finally { if (ticket === request) this.browserLoading = false }
    },
    updateBrowserFilterDraft(patch: Partial<RecentOutcomeFilters>) {
      ++request
      this.browserFilters = { ...this.browserFilters, ...patch }; this.browserPage = 1; this.currentSnapshotId = ''; this.browserRows = []; this.browserLoading = true; this.browserError = ''
    },
    async resetBrowserFilters() { this.browserFilters = defaultReviewFilters(); this.browserPage = 1; await this.loadReviewPage() },
  },
})
