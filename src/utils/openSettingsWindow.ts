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

export async function openSettingsWindow() {
  const { isMobile } = usePlatform()
  if (isNativeMobile || isMobile.value || !isTauri()) {
    void router.push({ name: 'settings' })
    return
  }
  try {
    if (await showAndFocusWindow("settings")) return
    const webview = new WebviewWindow("settings", {
      url: "/#/settings",
      title: "设置",
      width: 760,
      height: 640,
      minWidth: 680,
      minHeight: 520,
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
    console.warn("打开设置独立窗口失败，回退至内嵌路由跳转:", e)
    void router.push({ name: 'settings' })
  }
}

