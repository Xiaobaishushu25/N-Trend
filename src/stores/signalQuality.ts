import { defineStore } from 'pinia'

const key = 'ntrend.signal-quality.display'
function initial(): boolean {
  try { return typeof localStorage === 'undefined' || localStorage.getItem(key) !== 'off' } catch { return true }
}
export const useSignalQualityStore = defineStore('signal-quality-display', {
  state: () => ({ enabled: initial() }),
  actions: {
    setEnabled(value: boolean) {
      this.enabled = value
      try { localStorage.setItem(key, value ? 'on' : 'off') } catch { /* in-memory preference still works */ }
    },
  },
})
