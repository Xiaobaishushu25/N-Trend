import { ref, computed } from 'vue'

const userAgent = typeof navigator !== 'undefined' ? navigator.userAgent : ''
export const isNativeMobile = /Android|iPhone|iPad|iPod/i.test(userAgent)
export const isTouchDevice = typeof window !== 'undefined' && ('ontouchstart' in window || navigator.maxTouchPoints > 0)

const windowWidth = ref(typeof window !== 'undefined' ? window.innerWidth : 1360)
const windowHeight = ref(typeof window !== 'undefined' ? window.innerHeight : 860)

const SIMULATE_KEY = 'ntrend_simulate_mobile'
const isSimulatedMobile = ref(typeof localStorage !== 'undefined' && localStorage.getItem(SIMULATE_KEY) === '1')

if (typeof window !== 'undefined') {
  window.addEventListener('resize', () => {
    windowWidth.value = window.innerWidth
    windowHeight.value = window.innerHeight
  })
}

export function setSimulatedMobile(val: boolean) {
  isSimulatedMobile.value = val
  try {
    localStorage.setItem(SIMULATE_KEY, val ? '1' : '0')
  } catch {}
}

export function toggleSimulatedMobile() {
  setSimulatedMobile(!isSimulatedMobile.value)
}

export function usePlatform() {
  const isLandscape = computed(() => windowWidth.value > windowHeight.value)

  /**
   * 核心多端布局判定逻辑：
   * 1. 真实运行在移动端系统（Android/iOS 原生）：无论横屏还是竖屏，始终采用移动端交互与抽屉呈现；
   * 2. 开发者在 PC 端开启了手机仿真模式：强制采用移动端呈现（方便开发调试与实时看盘）；
   * 3. PC 桌面窗口缩窄到 768px 及以下：自适应降级为移动端呈现。
   */
  const isMobile = computed(() => {
    if (isNativeMobile) return true
    if (isSimulatedMobile.value) return true
    return windowWidth.value <= 768
  })

  const isDesktop = computed(() => !isMobile.value)

  return {
    isNativeMobile,
    isTouchDevice,
    isSimulatedMobile,
    setSimulatedMobile,
    toggleSimulatedMobile,
    isMobile,
    isDesktop,
    isLandscape,
    windowWidth,
    windowHeight,
  }
}
