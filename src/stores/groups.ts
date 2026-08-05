import { defineStore } from 'pinia'
import { api } from '../services/api'
import type { GroupRow } from '../types'

export const useGroupsStore = defineStore('groups', {
  state: () => ({
    groups: [] as GroupRow[],
    /** 当前选中的分组 id；null 表示“全部品种” */
    selectedId: null as number | null,
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
    async select(id: number | null) {
      this.selectedId = id
    },
  },
})
