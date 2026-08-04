import type { VNode } from 'vue'
import ContextMenu from '@imengyu/vue3-context-menu'
import type { MenuOptions } from '@imengyu/vue3-context-menu'
import '@imengyu/vue3-context-menu/lib/vue3-context-menu.css'

/** 通用右键菜单项 */
export interface ContextMenuItem {
  /** 显示文本 */
  label: string
  /** 图标：可传 tabler 图标组件的 VNode，例如 h(Trash) */
  icon?: string | VNode
  /** 是否禁用 */
  disabled?: boolean
  /** 分隔线：true 表示下方分割线，'up' 表示上方分割线 */
  divided?: boolean | 'up' | 'down'
  /** 右侧快捷键提示文本（仅展示，不绑定按键） */
  shortcut?: string
  /** 自定义 class，可用于危险操作标红（如 menu-item-danger） */
  customClass?: string
  /** 点击菜单项后的回调 */
  onClick?: () => void
}

/** 通用右键菜单配置 */
export interface ContextMenuConfig {
  /** 菜单项列表 */
  items: ContextMenuItem[]
  /** 菜单最小宽度，默认 160 */
  minWidth?: number
  /** 菜单最大高度，超出后内部滚动 */
  maxHeight?: number
  /** 主题：round / default / flat / win10 / mac 等，可加 " dark" 后缀 */
  theme?: string
}

/**
 * 在鼠标位置弹出右键菜单（内部已阻止浏览器默认菜单）。
 *
 * 通用用法（任何组件里都能用）：
 *   <div @contextmenu="onMenu">...</div>
 *   function onMenu(e: MouseEvent) {
 *     openContextMenu(e, {
 *       items: [
 *         { label: '删除', icon: h(Trash), customClass: 'menu-item-danger', onClick: doDelete },
 *       ],
 *     })
 *   }
 */
export function openContextMenu(event: MouseEvent, config: ContextMenuConfig) {
  event.preventDefault()
  event.stopPropagation()
  const options: MenuOptions = {
    x: event.clientX,
    y: event.clientY,
    theme: config.theme ?? 'round',
    minWidth: config.minWidth ?? 160,
    maxHeight: config.maxHeight,
    items: config.items.map((item) => ({
      label: item.label,
      icon: item.icon,
      disabled: item.disabled,
      divided: item.divided,
      shortcut: item.shortcut,
      customClass: item.customClass,
      onClick: item.onClick,
    })),
  }
  ContextMenu.showContextMenu(options)
}
