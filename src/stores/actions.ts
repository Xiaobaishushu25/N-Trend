import { defineStore } from 'pinia'
import { api } from '../services/api'
import { notify } from '../utils/notify'
import { useScansStore } from './scans'
import { useSettingsStore } from './settings'
import { useSymbolsStore } from './symbols'

/**
 * 标题栏上的全局操作（刷新数据 / 立即扫描 / 刷新名称 / 添加品种）。
 * 与 DashboardView 解耦：操作完成后通过 reloadTick 通知列表页重拉表格，
 * 避免部分命令不广播事件（手动刷新、添加品种）时界面停留在旧数据。
 */
export const useActionsStore = defineStore('actions', {
  state: () => ({
    refreshing: false,
    scanning: false,
    enriching: false,
    adding: false,
    /** 每完成一次操作 +1，供列表页监听后重拉表格 */
    reloadTick: 0,
  }),
  actions: {
    async refreshData() {
      if (this.refreshing) return
      this.refreshing = true
      try {
        const stats = await api.refreshDataNow()
        notify.success(`数据刷新完成：成功 ${stats.succeeded}，失败 ${stats.failures}`)
        await this.syncStatus()
        this.reloadTick++
      } catch (e) {
        notify.error(String(e))
      } finally {
        this.refreshing = false
      }
    },
    async enrichNames() {
      if (this.enriching) return
      this.enriching = true
      try {
        const n = await useSymbolsStore().enrichNames()
        notify.success(`已补齐 ${n} 个品种名称`)
        this.reloadTick++
      } catch (e) {
        notify.error(String(e))
      } finally {
        this.enriching = false
      }
    },
    async scanNow() {
      if (this.scanning) return
      this.scanning = true
      try {
        await useScansStore().runScanFast()
        const result = useScansStore().latest
        notify.success(`扫描完成：${result?.scanned ?? 0} 个品种，${result?.active_count ?? 0} 个信号`)
        await this.syncStatus()
        // 后端会广播 scan-completed 事件，列表页/图表页各自刷新，无需 reloadTick
      } catch (e) {
        notify.error(String(e))
      } finally {
        this.scanning = false
      }
    },
    async addSymbol(code: string) {
      const trimmed = code.trim().toUpperCase()
      if (!trimmed || this.adding) return false
      this.adding = true
      try {
        const count = await useSymbolsStore().add(trimmed)
        notify.success(`${trimmed} 已添加，回填 ${count} 根K线`)
        this.reloadTick++
        return true
      } catch (e) {
        const msg = String(e)
        // 无效代码是常见操作失误，用警告而非错误提示
        if (msg.includes('未找到品种')) notify.warning(msg)
        else notify.error(msg)
        return false
      } finally {
        this.adding = false
      }
    },
    async syncStatus() {
      try {
        await useSettingsStore().refreshStatus()
      } catch {
        // 顶部时间同步失败不影响本次操作
      }
    },
  },
})

