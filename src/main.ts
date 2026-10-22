import { createApp, nextTick } from 'vue'
import { createPinia } from 'pinia'
import { invoke, isTauri } from '@tauri-apps/api/core'
import App from './App.vue'
import router from './router'
import './styles.css'
import { reportPerformance } from './utils/performanceLog'

function reportStartup(stage: string, elapsedMs = performance.now()) {
  if (isTauri()) {
    void invoke('report_frontend_stage', { stage, elapsedMs }).catch(() => {})
  }
}

reportStartup('entry_evaluated')
const navigation = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming | undefined
if (navigation) {
  reportStartup('document_response_started', navigation.responseStart)
  reportStartup('document_waiting', navigation.responseStart - navigation.requestStart)
  reportStartup('document_downloaded', navigation.responseEnd - navigation.responseStart)
}
if (isTauri() && typeof PerformanceObserver !== 'undefined') {
  const paints = new PerformanceObserver((list) => {
    const firstPaint = list.getEntries().find((entry) => entry.name === 'first-contentful-paint')
    if (firstPaint) {
      reportStartup('first_contentful_paint', firstPaint.startTime)
      paints.disconnect()
    }
  })
  paints.observe({ type: 'paint', buffered: true })
}
function reportFrontendError(stage: string, message: string) {
  if (isTauri()) void invoke('report_frontend_error', { stage, message }).catch(() => {})
}
window.addEventListener('error', (event) => reportFrontendError('javascript_error', event.message || 'unknown error'))
window.addEventListener('unhandledrejection', (event) => reportFrontendError('unhandled_rejection', String(event.reason)))
let navigationStarted = performance.now()
router.beforeEach((to) => {
  navigationStarted = performance.now()
  reportPerformance('route_navigation_started', navigationStarted, {
    symbol: typeof to.params.symbol === 'string' ? to.params.symbol : undefined,
    timeframe: typeof to.query.tf === 'string' ? to.query.tf : undefined,
  })
})
router.afterEach((to, _from, failure) => {
  void nextTick(() => reportPerformance(failure ? 'route_navigation_failed' : 'route_navigation_finished', performance.now() - navigationStarted, {
    symbol: typeof to.params.symbol === 'string' ? to.params.symbol : undefined,
    timeframe: typeof to.query.tf === 'string' ? to.query.tf : undefined,
  }))
})
router.onError((error) => {
  reportPerformance('route_navigation_failed', performance.now() - navigationStarted)
  reportFrontendError('router_error', error.message)
})
const app = createApp(App)
app.config.errorHandler = (error, _instance, info) => {
  reportFrontendError('vue_error', `${info}: ${String(error)}`)
  console.error(error)
}
app.use(createPinia())
app.use(router)
app.mount('#app')
reportStartup('app_mounted')
void router.isReady().then(async () => {
  await nextTick()
  reportStartup('route_ready')
}).catch(() => {})
