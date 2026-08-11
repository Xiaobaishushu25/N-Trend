import { WebviewWindow } from '@tauri-apps/api/webviewWindow'

/** 已存在则显示并聚焦，返回 true；否则返回 false 由调用方新建窗口。 */
async function showAndFocusWindow(label: string): Promise<boolean> {
  const window = await WebviewWindow.getByLabel(label)
  if (window != null) {
    await window.show()
    await window.unminimize()
    await window.setFocus()
    return true
  }
  return false
}

/** 唤起独立历史通知窗口：只允许一个实例，重复调用只聚焦。 */
export async function openNotificationsWindow() {
  if (await showAndFocusWindow('notifications')) return
  const webview = new WebviewWindow('notifications', {
    url: '/#/notifications',
    center: true,
    title: '历史通知',
    width: 840,
    height: 700,
    minWidth: 680,
    minHeight: 520,
    resizable: true,
    decorations: false,
    dragDropEnabled: false,
    visible: false,
  })
  await webview.once('tauri://created', async () => {
    await webview.show()
    await webview.setFocus()
  })
}
