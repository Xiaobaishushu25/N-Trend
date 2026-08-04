import { defineStore } from 'pinia'
import { api } from '../services/api'
import type { ScanResult, ScanRow, SignalRow } from '../types'

export const useScansStore = defineStore('scans', {
  state: () => ({
    history: [] as ScanRow[],
    latest: null as ScanResult | null,
    detail: [] as SignalRow[],
    running: false,
  }),
  actions: {
    async loadHistory(limit = 20) {
      this.history = await api.getScanHistory(limit)
    },
    async runScan() {
      this.running = true
      try {
        this.latest = await api.runScanNow()
        await this.loadHistory(20)
      } finally {
        this.running = false
      }
    },
    async loadDetail(scanId: number) {
      this.detail = await api.getScanDetail(scanId)
    },
    ingest(result: ScanResult) {
      this.latest = result
      this.loadHistory(20)
    },
  },
})
