<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { NIcon, NLayout, NLayoutContent, NLayoutHeader, NTag, NText } from 'naive-ui'
import { TrendingUp } from '@vicons/tabler'
import { onDataUpdated, onQuotesUpdated, onScanCompleted } from '../services/api'
import { useAppStore } from '../stores/app'
import { useSettingsStore } from '../stores/settings'

const route = useRoute()
const appStore = useAppStore()
const settingsStore = useSettingsStore()

const bare = computed(() => Boolean(route.meta.bare))

/**
 * 由「行情请求事件」驱动：每次收到实时行情/K线刷新事件就呼吸，
 * 一段时间没新事件就熄灭，起到实时指示效果。
 */
const PREVIEW_ALWAYS_BREATHING = false
/** 收到行情请求事件后保持呼吸的时间（毫秒）：现价轮询 3 秒一轮，5 秒内没新事件才熄灭 */
const BREATHE_HOLD_MS = 5000

const dataActive = ref(PREVIEW_ALWAYS_BREATHING)
let breatheTimer: ReturnType<typeof setTimeout> | undefined

function kickBreathe() {
  if (PREVIEW_ALWAYS_BREATHING) return
  dataActive.value = true
  if (breatheTimer) clearTimeout(breatheTimer)
  breatheTimer = setTimeout(() => {
    dataActive.value = false
  }, BREATHE_HOLD_MS)
}

const unlisteners: (() => void)[] = []

onMounted(async () => {
  try {
    await settingsStore.load()
  } catch {
    // 浏览器预览环境下无后端命令，忽略
  }
  // 定时任务每次成功后，顶部时间信息实时跟随刷新
  unlisteners.push(
    await onDataUpdated(() => {
      settingsStore.refreshStatus()
      kickBreathe()
    }),
  )
  // 实时现价轮询每 3 秒返回一次行情，作为呼吸灯的主要驱动信号
  unlisteners.push(await onQuotesUpdated(() => kickBreathe()))
  unlisteners.push(await onScanCompleted(() => settingsStore.refreshStatus()))
})

onBeforeUnmount(() => {
  if (breatheTimer) clearTimeout(breatheTimer)
  for (const fn of unlisteners) fn()
})
</script>

<template>
  <n-layout position="absolute" style="--app-header-h: 48px">
    <n-layout-header v-if="!bare" bordered class="topbar">
      <div class="brand">
        <n-icon :component="TrendingUp" size="22" color="#f5c23f" />
        <span class="brand-name">N趋势</span>
        <n-text depth="3" style="font-size: 12px">v{{ appStore.info.version }}</n-text>
      </div>
      <div class="status-area">
        <div
          v-if="settingsStore.status.running"
          class="live-tag"
          :class="{ 'is-breathing': dataActive }"
        >
          <span class="live-dot" />
          <span>定时刷新扫描运行中</span>
        </div>
        <n-tag v-else type="warning" size="small" round>定时扫描已暂停</n-tag>
        <div class="status-meta">
          <div class="status-item">
            <span class="dot dot-data" />
            <span class="status-label">数据最近更新于：</span>
            <span class="status-value" :class="{ empty: !settingsStore.status.last_refresh }">
              {{ settingsStore.status.last_refresh || '—' }}
            </span>
          </div>
          <div class="status-divider" />
          <div class="status-item">
            <span class="dot dot-scan" />
            <span class="status-label">形态最新识别于：</span>
            <span class="status-value" :class="{ empty: !settingsStore.status.last_scan }">
              {{ settingsStore.status.last_scan || '—' }}
            </span>
          </div>
        </div>
      </div>
    </n-layout-header>
    <n-layout-content
      position="absolute"
      :style="bare ? 'top: 0' : 'top: var(--app-header-h)'"
      :native-scrollbar="false"
      :content-style="bare ? 'height: 100%; padding: 0' : 'height: 100%; box-sizing: border-box; padding: 16px'"
    >
      <!-- 只缓存列表页（DashboardView）：返回时不再全量重载；K线图页不缓存，保持每次进入重置视图 -->
      <router-view v-slot="{ Component }">
        <keep-alive include="DashboardView">
          <component :is="Component" />
        </keep-alive>
      </router-view>
    </n-layout-content>
  </n-layout>
</template>

<style scoped>
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 48px;
  padding: 0 16px;
  background: #fff;
}
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
}
.brand-name {
  font-size: 17px;
  font-weight: 700;
  letter-spacing: 1px;
}
.status-area {
  display: flex;
  align-items: center;
  gap: 14px;
}
.live-tag {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  height: 24px;
  padding: 0 12px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
  color: #0f9d58;
  background: rgba(15, 157, 88, 0.12);
  white-space: nowrap;
}
.live-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex: none;
  background: #18a058;
  box-shadow: 0 0 4px rgba(24, 160, 88, 0.6);
}
.live-tag.is-breathing .live-dot {
  animation: breathe 1.6s ease-in-out infinite;
}
@keyframes breathe {
  0%,
  100% {
    transform: scale(0.85);
    opacity: 0.55;
    box-shadow: 0 0 3px rgba(24, 160, 88, 0.35);
  }
  50% {
    transform: scale(1.35);
    opacity: 1;
    box-shadow: 0 0 10px rgba(24, 160, 88, 0.9);
  }
}
.status-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  height: 22px;
  padding-left: 14px;
  border-left: 1px solid #e8ecf1;
}
.status-item {
  display: flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
}
.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex: none;
}
.dot-data {
  background: #4098ff;
}
.dot-scan {
  background: #18a058;
}
.status-label {
  font-size: 12px;
  color: #97a0b3;
}
.status-value {
  font-size: 12px;
  color: #3d4757;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.status-value.empty {
  color: #c2c9d4;
  font-weight: 400;
}
.status-divider {
  width: 1px;
  height: 14px;
  background: #e5e9ef;
  flex: none;
}
</style>
