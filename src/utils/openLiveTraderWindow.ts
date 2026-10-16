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

export async function openLiveTraderWindow() {
  const { isMobile } = usePlatform()
  if (isNativeMobile || isMobile.value || !isTauri()) {
    void router.push({ name: 'live-trader' })
    return
  }
  try {
    if (await showAndFocusWindow("live-trader")) return
    const webview = new WebviewWindow("live-trader", {
      url: "/#/live-trader",
      title: "实盘量化执行终端",
      width: 1080,
      height: 820,
      minWidth: 800,
      minHeight: 600,
      resizable: true,
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
    void router.push({ name: 'live-trader' })
  }
}
