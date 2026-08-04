import { defineStore } from 'pinia'
import { api } from '../services/api'
import type { SymbolRow } from '../types'

export const useSymbolsStore = defineStore('symbols', {
  state: () => ({
    symbols: [] as SymbolRow[],
    loading: false,
  }),
  getters: {
    watchlist: (s) => s.symbols.filter((x) => x.watchlist && x.enabled),
  },
  actions: {
    async load() {
      this.loading = true
      try {
        this.symbols = await api.getSymbols()
      } finally {
        this.loading = false
      }
    },
    async add(code: string) {
      const count = await api.addSymbol(code)
      await this.load()
      return count
    },
    async remove(code: string) {
      await api.removeSymbol(code)
      await this.load()
    },
    async setFlags(code: string, watchlist: boolean, enabled: boolean) {
      await api.setSymbolFlags(code, watchlist, enabled)
      await this.load()
    },
    async enrichNames() {
      const count = await api.enrichSymbolNames()
      await this.load()
      return count
    },
    async refreshList() {
      const count = await api.refreshSymbolList()
      await this.load()
      return count
    },
  },
})

