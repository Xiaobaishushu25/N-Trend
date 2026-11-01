import { WebviewWindow } from '@tauri-apps/api/webviewWindow'
import { isTauri } from '@tauri-apps/api/core'
import { emit } from '@tauri-apps/api/event'
import router from '../router'
import { notify } from './notify'
import type { ReviewQuery, ReviewScoreMetric } from '../types/review'

export interface OpenReviewChartParams {
  symbol: string
  eventId: number
  version?: string
  snapshotId?: string
  query?: ReviewQuery
  cursor?: number
  metric?: ReviewScoreMetric
}

export async function openReviewChartWindow(params: OpenReviewChartParams) {
  const { symbol, eventId, version, snapshotId } = params
  const sym = symbol || 'FG0'
  const contextQuery = {
    ...(params.query ? { context: JSON.stringify(params.query) } : {}),
    ...(params.cursor != null ? { cursor: String(params.cursor) } : {}),
    ...(params.metric ? { metric: params.metric } : {}),
  }

  if (!isTauri()) {
    void router.push({
      name: 'review-chart',
      params: { symbol: sym },
      query: {
        ...contextQuery,
        eventId: String(eventId),
        ...(version ? { version } : {}),
        ...(snapshotId ? { snapshotId } : {}),
      },
    })
    return
  }

  try {
    const existing = await WebviewWindow.getByLabel('review-chart')
    if (existing != null) {
      await existing.show()
      await existing.unminimize()
      await existing.setFocus()
      await emit('load-review-signal', params)
      notify.success(`已在复盘独立窗口定位 [${sym}] Event #${eventId}`)
      return
    }

    const queryParts = [`eventId=${encodeURIComponent(eventId)}`]
    if (version) queryParts.push(`version=${encodeURIComponent(version)}`)
    if (snapshotId) queryParts.push(`snapshotId=${encodeURIComponent(snapshotId)}`)
    for (const [key, value] of Object.entries(contextQuery)) queryParts.push(`${key}=${encodeURIComponent(value)}`)
    const queryString = queryParts.join('&')

    const webview = new WebviewWindow('review-chart', {
      url: `/#/review-chart/${encodeURIComponent(sym)}?${queryString}`,
      title: `复盘打点K线 · ${sym}`,
      width: 1400,
      height: 880,
      minWidth: 1000,
      minHeight: 640,
      resizable: true,
      decorations: false,
      dragDropEnabled: false,
      visible: false,
    })

    await webview.once('tauri://created', async () => {
      await webview.show()
      await webview.setFocus()
    })
    notify.success(`已开启独立复盘窗口定位 [${sym}] Event #${eventId}`)
  } catch (e: any) {
    console.warn('打开独立复盘窗口失败，回退到路由跳转:', e)
    notify.error(`打开独立复盘窗口失败: ${e?.message || e}`)
    void router.push({
      name: 'review-chart',
      params: { symbol: sym },
      query: {
        ...contextQuery,
        eventId: String(eventId),
        ...(version ? { version } : {}),
        ...(snapshotId ? { snapshotId } : {}),
      },
    })
  }
}
