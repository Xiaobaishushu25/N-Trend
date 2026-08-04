import { createDiscreteApi, dateZhCN, zhCN } from 'naive-ui'

/** 通用确认框配置 */
export interface ConfirmOptions {
  /** 标题，默认「确认操作」 */
  title?: string
  /** 正文内容 */
  content?: string
  /** 确认按钮文字，默认「确定」 */
  positiveText?: string
  /** 取消按钮文字，默认「取消」 */
  negativeText?: string
  /** 弹窗类型，决定图标与强调色，默认 warning（删除等危险操作可传 'error'） */
  type?: 'warning' | 'error' | 'info' | 'success'
}

type DiscreteDialogApi = ReturnType<typeof createDiscreteApi<'dialog'>>['dialog']

let dialogApi: DiscreteDialogApi | null = null

/**
 * 懒加载一个独立的 naive-ui 对话框实例。
 * 不依赖页面里是否有 NDialogProvider，因此可以在任意模块/回调里使用。
 */
function getDialogApi(): DiscreteDialogApi | null {
  if (dialogApi) return dialogApi
  try {
    const api = createDiscreteApi(['dialog'], {
      configProviderProps: { locale: zhCN, dateLocale: dateZhCN },
    })
    dialogApi = api.dialog
  } catch {
    dialogApi = null
  }
  return dialogApi
}

/**
 * 弹出确认框并返回 Promise<boolean>：true = 用户点了确认，false = 取消/关闭。
 *
 * 通用用法（任何组件/模块里都能用）：
 *   if (await confirmAction({ title: '删除', content: '确定要删除吗？', positiveText: '删除' })) {
 *     // 执行删除
 *   }
 */
export function confirmAction(options: ConfirmOptions = {}): Promise<boolean> {
  return new Promise((resolve) => {
    const dialog = getDialogApi()
    if (!dialog) {
      resolve(false)
      return
    }
    // 同一弹窗只结算一次结果，防止 onClose 与按钮回调重复触发
    let settled = false
    const finish = (value: boolean) => {
      if (!settled) {
        settled = true
        resolve(value)
      }
    }
    const common = {
      title: options.title ?? '确认操作',
      content: options.content ?? '确定要执行此操作吗？',
      positiveText: options.positiveText ?? '确定',
      negativeText: options.negativeText ?? '取消',
      onPositiveClick: () => finish(true),
      onNegativeClick: () => finish(false),
      onClose: () => finish(false),
      onMaskClick: () => finish(false),
    }
    const type = options.type ?? 'warning'
    if (type === 'error') dialog.error(common)
    else if (type === 'info') dialog.info(common)
    else if (type === 'success') dialog.success(common)
    else dialog.warning(common)
  })
}
