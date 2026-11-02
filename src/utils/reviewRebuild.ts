import { ref } from 'vue'
import { api } from '../services/api'
import { confirmAction } from './confirm'
import { notify } from './notify'
import { usePublishedReviewStore } from '../stores/publishedReview'

export const isRebuildingAllSignals = ref(false)

/**
 * 全量重建全部形态信号与复盘计算体系：
 * 1. 弹出庄重严肃的二次确认对话框，说明该操作的重量级影响；
 * 2. 清空并基于全品种 15m K线重构形态识别事件 (rebuildEventsNow)；
 * 3. 触发并等待全品种复盘引擎任务 (reviewRebuild)，重算质量评分、模拟退出并生成 safe integer 编号；
 * 4. 自动刷新复盘工作台列表与快照。
 */
export async function runRebuildAllSignals(): Promise<boolean> {
  const ok = await confirmAction({
    title: '重建全部信号与复盘体系',
    content: '这是重量级计算操作：将清空旧的识别与复盘快照，重新扫描全品种历史K线重构形态事件，并触发全量复盘引擎重新计算逐条评分、模拟退出及收益指标。执行耗时约数十秒至数分钟，期间不可中断。确定继续执行吗？',
    positiveText: '确认并启动全量重建',
    negativeText: '取消',
  })
  if (!ok) return false

  isRebuildingAllSignals.value = true
  try {
    notify.info('正在扫描历史K线并重构形态识别事件...')
    const result = await api.rebuildEventsNow()
    notify.info(`信号事件识别完成（${result.scanned} 个品种），正在全量重算复盘与评分体系...`)

    const job = await api.reviewRebuild({})
    const startTime = Date.now()
    while (Date.now() - startTime < 180000) {
      await new Promise(r => setTimeout(r, 2000))
      try {
        const status = await api.reviewJob(job.id)
        if (status.status === 'completed') break
        if (status.status === 'failed') {
          throw new Error(status.errors?.join('; ') || '复盘计算任务执行失败')
        }
      } catch (e: any) {
        if (e?.message?.includes('failed') || e?.message?.includes('失败')) throw e
      }
    }

    const store = usePublishedReviewStore()
    await store.loadReviewPage()
    notify.success(`全量信号与复盘重构完成：共扫描 ${result.scanned} 个品种，重构 ${result.active_count} 个当前信号`)
    return true
  } catch (e: any) {
    notify.error(`全量重建失败: ${e?.message || e}`)
    return false
  } finally {
    isRebuildingAllSignals.value = false
  }
}
