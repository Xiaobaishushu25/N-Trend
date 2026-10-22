import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  clearScreen: false,
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    watch: {
      // Windows registers a watcher per file. Rust build output and runtime data
      // can block Vite's event loop for tens of seconds before it serves HTML.
      // Tauri watches Rust sources separately; these paths do not drive the UI.
      ignored: [
        '**/target/**',
        '**/src-tauri/**',
        '**/crates/**',
        '**/reports/**',
        '**/backtests/**',
        '**/data/**',
        '**/flow_*/**',
        '**/__pycache__/**',
        '**/_tmp*/**',
        '**/release-signing/**',
      ],
    },
  },
  build: {
    outDir: 'dist',
    target: 'esnext',
    chunkSizeWarningLimit: 1600,
  },
})
