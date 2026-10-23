import { WebviewWindow } from "@tauri-apps/api/webviewWindow"
import { isTauri } from "@tauri-apps/api/core"
import { isNativeMobile, usePlatform } from './platform'
import router from '../router'

async function showAndFocusWindow(label: string): Promise<boolean> {
  try {
    const window = await WebviewWindow.getByLabel(label)
    if (window != null) {
      await window.show()
      await window.unminimize()
      await window.setFocus()
      return true
    }
  } catch {}
  return false
}

export async function openLiveTraderWindow(instanceId?: string | unknown, titleName?: string) {
  const { isMobile } = usePlatform()
  const targetId = typeof instanceId === 'string' && instanceId.trim() ? instanceId.trim() : undefined
  const hasInstance = Boolean(targetId)
  const windowLabel = hasInstance ? `live-trader-${targetId!.replace(/[^a-zA-Z0-9_-]/g, '_')}` : 'live-trader'
  const targetUrl = hasInstance ? `/#/live-trader?instance_id=${encodeURIComponent(targetId!)}` : '/#/live-trader'
  const targetTitle = hasInstance ? `实盘量化执行终端 · ${titleName || targetId}` : '实盘量化执行终端'

  if (isNativeMobile || isMobile.value || !isTauri()) {
    void router.push({
      name: 'live-trader',
      query: hasInstance ? { instance_id: targetId } : undefined,
    })
    return
  }
  try {
    if (await showAndFocusWindow(windowLabel)) return
    const webview = new WebviewWindow(windowLabel, {
      url: targetUrl,
      title: targetTitle,
      width: 1120,
      height: 840,
      minWidth: 840,
      minHeight: 600,
      resizable: true,
      maximizable: true,
      minimizable: true,
      closable: true,
      shadow: true,
      decorations: false,
      dragDropEnabled: false,
      visible: false,
    })
    await webview.once("tauri://created", async () => {
      await webview.show()
      await webview.setFocus()
    })
  } catch (e) {
    console.warn("打开实盘量化独立窗口失败，回退至内嵌路由跳转:", e)
    void router.push({
      name: 'live-trader',
      query: hasInstance ? { instance_id: targetId } : undefined,
    })
  }
}
