import { defineStore } from 'pinia'
import { api } from '../services/api'
import type { GroupRow } from '../types'

export const useGroupsStore = defineStore('groups', {
  state: () => ({
    groups: [] as GroupRow[],
    /** 当前选中的分组 id；null 表示“全部品种” */
    selectedId: null as number | null,
    /** “全部品种”在标签页/管理列表中的排序位置（位于该下标的真实分组之前，0 为最前） */
    allPosition: 0,
    /** 分组/组内排序版本号：列表页与K线页靠它互相通知“顺序已变，请重拉” */
    revision: 0,
    loading: false,
  }),
  getters: {
    selected: (s) => s.groups.find((g) => g.id === s.selectedId) ?? null,
  },
  actions: {
    async load() {
      this.loading = true
      try {
        this.groups = await api.listGroups()
        this.allPosition = await api.getGroupAllPosition()
      } finally {
        this.loading = false
      }
    },
    async create(name: string) {
      const group = await api.createGroup(name)
      await this.load()
      this.selectedId = group.id
      return group
    },
    async rename(id: number, name: string) {
      await api.renameGroup(id, name)
      await this.load()
    },
    async remove(id: number) {
      await api.deleteGroup(id)
      if (this.selectedId === id) this.selectedId = null
      await this.load()
    },
    /**
     * 按传入的 id 顺序重排分组：先本地更新让标签页/弹窗立即跟随，
     * 再落库；失败时回滚为服务端顺序并抛出错误。
     */
    async reorder(ids: number[], allPosition: number) {
      const uniqueIds = new Set(ids)
      const currentIds = this.groups.map((g) => g.id)
      // 防御：ids 必须是当前分组的完整排列（无重复、无缺失），
      // 拖拽异常时回滚，避免重复分组被写进内存导致标签页/弹窗出现重复项
      if (ids.length !== currentIds.length || !currentIds.every((id) => uniqueIds.has(id))) {
        await this.load()
        return
      }
      const byId = new Map(this.groups.map((g) => [g.id, g]))
      const next = ids.map((id) => byId.get(id)).filter((g): g is GroupRow => g != null)
      this.groups = next
      this.allPosition = Math.min(Math.max(allPosition, 0), this.groups.length)
      try {
        await api.reorderGroups(ids, allPosition)
        await this.load()
      } catch (err) {
        await this.load()
        throw err
      }
    },
    async select(id: number | null) {
      this.selectedId = id
    },
    bumpRevision() {
      this.revision++
    },
  },
})
