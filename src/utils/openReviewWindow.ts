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

export async function openReviewWindow() {
  const { isMobile } = usePlatform()
  if (isNativeMobile || isMobile.value || !isTauri()) {
    void router.push({ name: 'review' })
    return
  }
  try {
    if (await showAndFocusWindow("review")) return
    const webview = new WebviewWindow("review", {
      url: "/#/review",
      title: "复盘统计",
      width: 1280,
      height: 820,
      minWidth: 960,
      minHeight: 640,
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
    console.warn("打开复盘独立窗口失败，回退至内嵌路由跳转:", e)
    void router.push({ name: 'review' })
  }
}

