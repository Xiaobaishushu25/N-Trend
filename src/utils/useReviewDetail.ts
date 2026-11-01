import { ref } from 'vue'
import { api } from '../services/api'
import type { ReviewResultDetail } from '../types/review'

/** Each selection owns all of its loading, error and detail state. */
export function useReviewDetail() {
  const detail = ref<ReviewResultDetail | null>(null)
  const loading = ref(false)
  const error = ref('')
  let request = 0
  function cancel() { ++request; detail.value = null; loading.value = false; error.value = '' }
  async function load(eventId: number, version?: string) {
    const ticket = ++request
    detail.value = null; error.value = ''; loading.value = true
    try {
      const result = await api.reviewDetail(eventId, version)
      if (ticket !== request) return null
      if (result.summary?.event_id !== eventId || version && result.summary.result_version !== version || !result.summary?.result_version) throw new Error('复盘详情与所选信号或版本不一致')
      detail.value = result
      return result
    } catch (e) {
      if (ticket === request) error.value = `加载复盘详情失败：${String(e)}。请重试或检查复盘任务。`
      return null
    } finally { if (ticket === request) loading.value = false }
  }
  return { detail, loading, error, load, cancel }
}
