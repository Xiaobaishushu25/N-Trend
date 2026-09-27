/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}

interface AndroidBridge {
  setOrientation(orientation: 'landscape' | 'portrait' | 'unspecified'): void
}

interface Window {
  AndroidBridge?: AndroidBridge
}
