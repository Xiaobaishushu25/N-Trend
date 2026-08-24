import { WebviewWindow } from "@tauri-apps/api/webviewWindow"

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

export async function openSettingsWindow() {
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
}
