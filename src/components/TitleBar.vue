<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { isTauri } from '@tauri-apps/api/core'
import { getCurrentWindow } from '@tauri-apps/api/window'

const props = withDefaults(
  defineProps<{
    /** 标题栏高度（px），默认 40 */
    height?: number
    /** 未提供 left 插槽时显示的窗口标题文字 */
    title?: string
    /** 主题：light / dark（dark 预留备用） */
    variant?: 'light' | 'dark'
    /** 是否显示最小化/最大化/关闭按钮，默认显示 */
    showWindowControls?: boolean
    /** 是否允许双击最大化/还原，默认允许 */
    maximizable?: boolean
  }>(),
  {
    height: 40,
    title: '',
    variant: 'light',
    showWindowControls: true,
    maximizable: true,
  },
)

/** 浏览器预览（纯前端 npm run dev）下不调用任何 Tauri API */
const inTauri = isTauri()
const isMaximized = ref(false)
let unlisteners: (() => void)[] = []

const controlsVisible = computed(() => inTauri && props.showWindowControls)
const barStyle = computed(() => ({ height: `${props.height}px` }))

/** 交互元素（按钮、链接、输入框等）不参与拖拽/双击最大化 */
function isInteractive(target: EventTarget | null): boolean {
  // 注意用 Element 而非 HTMLElement：按钮图标是 <svg>（SVGElement），
  // 若按 HTMLElement 判断会漏判，导致按下按钮时被当成拖拽、点击被吞掉
  if (!(target instanceof Element)) return false
  return Boolean(
    target.closest('button, a, input, select, textarea, [role="button"], [data-titlebar-ignore]'),
  )
}

function refreshMaximized() {
  if (!inTauri) return
  getCurrentWindow()
    .isMaximized()
    .then((v) => {
      isMaximized.value = v
    })
    .catch(() => {})
}

/**
 * 拖拽/双击最大化统一由本组件驱动。
 * 不依赖 data-tauri-drag-region：Tauri 2 的该属性是“精确目标匹配”，
 * 插槽内容里的子元素不会命中，导致大片区域不可拖拽；改为在根元素上委托处理，
 * 交互元素一律跳过。
 */
function onBarMouseDown(e: MouseEvent) {
  if (!inTauri || e.button !== 0) return
  if (isInteractive(e.target)) return
  e.preventDefault()
  const win = getCurrentWindow()
  if (e.detail === 2) {
    // 双击：切换最大化/还原（与系统行为一致，在第二次 mousedown 时触发）
    if (props.maximizable) void win.toggleMaximize()
  } else {
    void win.startDragging()
  }
}

function minimize() {
  if (!inTauri) return
  void getCurrentWindow().minimize()
}

function toggleMaximize() {
  if (!inTauri || !props.maximizable) return
  void getCurrentWindow().toggleMaximize()
}

function closeWindow() {
  if (!inTauri) return
  // 主窗口关闭会被 Rust 侧 CloseRequested 拦截并隐藏到托盘；设置窗口正常关闭
  void getCurrentWindow().close()
}

onMounted(() => {
  if (!inTauri) return
  void refreshMaximized()
  const win = getCurrentWindow()
  Promise.all([
    win.onResized(() => refreshMaximized()),
    win.onMoved(() => refreshMaximized()),
  ]).then((fns) => {
    unlisteners.push(...fns)
  })
})

onBeforeUnmount(() => {
  for (const fn of unlisteners) fn()
  unlisteners = []
})
</script>

<template>
  <div
    class="titlebar"
    :class="`is-${variant}`"
    :style="barStyle"
    @mousedown="onBarMouseDown"
  >
    <div class="tb-left">
      <slot name="left">
        <span v-if="title" class="tb-title">{{ title }}</span>
      </slot>
    </div>
    <div class="tb-center">
      <slot name="center" />
    </div>
    <div v-if="$slots.right" class="tb-right">
      <slot name="right" />
    </div>
    <div v-if="controlsVisible" class="tb-controls">
      <button type="button" class="tb-btn" title="最小化" aria-label="最小化" @click="minimize">
        <svg viewBox="0 0 10 10" width="10" height="10" aria-hidden="true">
          <path d="M0 5h10" stroke="currentColor" stroke-width="1" />
        </svg>
      </button>
      <button
        v-if="maximizable"
        type="button"
        class="tb-btn"
        :title="isMaximized ? '还原' : '最大化'"
        :aria-label="isMaximized ? '还原' : '最大化'"
        @click="toggleMaximize"
      >
        <svg v-if="isMaximized" viewBox="0 0 10 10" width="10" height="10" aria-hidden="true">
          <path d="M2.5 0.5h7v7M0.5 2.5v7h7" fill="none" stroke="currentColor" stroke-width="1" />
        </svg>
        <svg v-else viewBox="0 0 10 10" width="10" height="10" aria-hidden="true">
          <rect x="0.5" y="0.5" width="9" height="9" fill="none" stroke="currentColor" stroke-width="1" />
        </svg>
      </button>
      <button
        type="button"
        class="tb-btn tb-btn-close"
        title="关闭"
        aria-label="关闭"
        @click="closeWindow"
      >
        <svg viewBox="0 0 10 10" width="10" height="10" aria-hidden="true">
          <path d="M1 1l8 8M9 1L1 9" stroke="currentColor" stroke-width="1" />
        </svg>
      </button>
    </div>
  </div>
</template>

<style scoped>
.titlebar {
  display: flex;
  align-items: center;
  flex: none;
  padding-left: 14px;
  user-select: none;
  -webkit-user-select: none;
}
.titlebar.is-light {
  background: #fff;
  border-bottom: 1px solid #e8ecf1;
  color: #1f2329;
}
.titlebar.is-dark {
  background: #1f2329;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  color: #f2f4f8;
}
.tb-left {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: none;
  min-width: 0;
}
.tb-center {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}
.tb-right {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: none;
  min-width: 0;
}
.tb-title {
  font-size: 13px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.tb-controls {
  display: flex;
  align-items: stretch;
  height: 100%;
  margin-left: 8px;
  flex: none;
}
.tb-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 46px;
  height: 100%;
  padding: 0;
  border: none;
  background: transparent;
  color: inherit;
  cursor: pointer;
}
.tb-btn:hover {
  background: rgba(0, 0, 0, 0.06);
}
.is-dark .tb-btn:hover {
  background: rgba(255, 255, 255, 0.1);
}
.tb-btn-close:hover {
  background: #e03131 !important;
  color: #fff;
}
.tb-btn svg {
  display: block;
}
</style>
