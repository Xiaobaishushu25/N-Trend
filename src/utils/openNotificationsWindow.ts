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

export async function openNotificationsWindow() {
  if (await showAndFocusWindow("notifications")) return
  const webview = new WebviewWindow("notifications", {
    url: "/#/notifications",
    title: "历史通知",
    width: 840,
    height: 700,
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
