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

export async function openReviewWindow() {
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
}
