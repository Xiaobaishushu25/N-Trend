import { defineStore } from 'pinia'
import { api } from '../services/api'
import type { ManualLevelDto, ManualLevelInput } from '../types'

export const useManualLevelsStore = defineStore('manualLevels', {
  state: () => ({
    levels: [] as ManualLevelDto[],
    loading: false,
    saving: false,
    error: '' as string,
  }),
  getters: {
    forChart: (state) => (symbol: string, timeframe: string) =>
      state.levels.filter((level) =>
        level.symbol === symbol && level.timeframe === timeframe && level.status !== 'archived',
      ),
  },
  actions: {
    async load(symbol?: string, timeframe?: string) {
      this.loading = true
      this.error = ''
      try {
        this.levels = await api.listManualLevels(symbol, timeframe, false)
      } catch (error) {
        this.error = String(error)
      } finally {
        this.loading = false
      }
    },
    async create(input: ManualLevelInput) {
      this.saving = true
      try {
        const created = await api.createManualLevel(input)
        this.levels = [created, ...this.levels.filter((level) => level.id !== created.id)]
        return created
      } finally {
        this.saving = false
      }
    },
    async update(id: number, input: ManualLevelInput) {
      this.saving = true
      const index = this.levels.findIndex((level) => level.id === id)
      const previous = index >= 0 ? { ...this.levels[index] } : null
      if (index >= 0 && previous) {
        // 先更新本地草稿，避免拖拽松手后在 API 返回前闪回旧矩形。
        this.levels.splice(index, 1, { ...previous, ...input })
      }
      try {
        const updated = await api.updateManualLevel(id, input)
        const currentIndex = this.levels.findIndex((level) => level.id === id)
        if (currentIndex >= 0) this.levels.splice(currentIndex, 1, updated)
        else this.levels.unshift(updated)
        return updated
      } catch (error) {
        if (index >= 0 && previous) this.levels.splice(index, 1, previous)
        throw error
      } finally {
        this.saving = false
      }
    },
    async setMonitoring(id: number, enabled: boolean) {
      const updated = await api.setManualLevelMonitoring(id, enabled)
      const index = this.levels.findIndex((level) => level.id === id)
      if (index >= 0) this.levels.splice(index, 1, updated)
      return updated
    },
    async archive(id: number) {
      await api.archiveManualLevel(id)
      const level = this.levels.find((item) => item.id === id)
      if (level) {
        level.status = 'archived'
        level.monitor_enabled = false
      }
    },
    async remove(id: number) {
      await api.deleteManualLevel(id)
      this.levels = this.levels.filter((level) => level.id !== id)
    },
  },
})
