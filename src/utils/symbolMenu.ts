import { h } from 'vue'
import { Trash } from '@vicons/tabler'
import { openContextMenu, type ContextMenuItem } from './contextMenu'
import type { GroupRow } from '../types'

export interface SymbolMenuContext {
  groups: GroupRow[]
  selectedGroupId: number | null
  symbol: string
  /** 该品种当前已加入的分组 id 集合：子菜单里打勾标识“已在此组” */
  memberGroupIds: ReadonlySet<number>
  onRemoveFromGroup: () => void
  onCopyToGroup: (group: GroupRow) => void
  onMoveToGroup: (group: GroupRow) => void
  onDeleteSymbol: () => void
}

/**
 * 品种行的统一右键菜单：表格页与 K 线图左侧列表共用。
 * 分组视图下有“从该组删除 / 复制到某组 / 移动到某组”，全部视图下只有复制与彻底删除。
 */
export function openSymbolContextMenu(e: MouseEvent, ctx: SymbolMenuContext) {
  const inGroup = ctx.selectedGroupId != null
  const selectedGroupName = ctx.groups.find((g) => g.id === ctx.selectedGroupId)?.name
  const targetGroups = inGroup
    ? ctx.groups.filter((g) => g.id !== ctx.selectedGroupId)
    : ctx.groups
  const emptyPlaceholder = inGroup ? '暂无其他分组' : '暂无分组'
  const copyChildren: ContextMenuItem[] = targetGroups.length
    ? targetGroups.map((g) => ({
        label: g.name,
        checked: ctx.memberGroupIds.has(g.id),
        onClick: () => ctx.onCopyToGroup(g),
      }))
    : [{ label: emptyPlaceholder, disabled: true }]
  const moveChildren: ContextMenuItem[] = targetGroups.length
    ? targetGroups.map((g) => ({
        label: g.name,
        checked: ctx.memberGroupIds.has(g.id),
        onClick: () => ctx.onMoveToGroup(g),
      }))
    : [{ label: emptyPlaceholder, disabled: true }]
  const items: ContextMenuItem[] = []
  // 分组操作区
  items.push({
    label: '复制自选至',
    children: copyChildren,
  })
  if (inGroup) {
    items.push({
      label: '移动自选至',
      children: moveChildren,
    })
  }
  // 删除类操作区：与上面的分组操作用分割线隔开
  if (inGroup) {
    items.push({
      label: selectedGroupName ? `从「${selectedGroupName}」删除` : '从该组删除',
      icon: h(Trash),
      customClass: 'menu-item-danger',
      divided: 'up',
      onClick: ctx.onRemoveFromGroup,
    })
  }
  items.push({
    label: '彻底删除品种',
    icon: h(Trash),
    customClass: 'menu-item-danger',
    divided: inGroup ? undefined : 'up',
    onClick: ctx.onDeleteSymbol,
  })
  openContextMenu(e, { items })
}
