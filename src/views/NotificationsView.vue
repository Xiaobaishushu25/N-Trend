<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { api, onNotificationHistoryUpdated } from '../services/api'
import type { NotificationHistoryItem } from '../types'

const items = ref<NotificationHistoryItem[]>([])
const loading = ref(true)
const unlisteners: (() => void)[] = []

async function loadHistory() {
  loading.value = true
  try {
    items.value = await api.getNotificationHistory()
  } catch {
    items.value = []
  } finally {
    loading.value = false
  }
}

function fmtPrice(v: number): string {
  return Number.isInteger(v) ? v.toFixed(0) : v.toFixed(1)
}

function kindLabel(item: NotificationHistoryItem): string {
  if (item.entry_trigger) return '入场提醒'
  // 信号卡片本身已有完整信息，历史列表里不再重复显示“信号”
  if (item.signal) return ''
  switch (item.kind) {
    case 'success':
      return '成功'
    case 'warning':
      return '警告'
    case 'error':
      return '错误'
    default:
      return '提示'
  }
}

function directionLabel(direction: string): string {
  return direction === 'up' ? '做多' : '做空'
}

function levelLabel(level: string): string {
  return level === 'fine' ? '精细' : level === 'large' ? '较大' : level
}

onMounted(async () => {
  try {
    unlisteners.push(
      await onNotificationHistoryUpdated((list) => {
        items.value = list
        loading.value = false
      }),
    )
  } catch {
    // 浏览器预览等环境没有 Tauri 事件 API，历史列表已由 loadHistory 填充
  }
  await loadHistory()
})

onBeforeUnmount(() => {
  for (const fn of unlisteners) fn()
})
</script>

<template>
  <div class="page">
    <header class="page-header">
      <h1>历史通知</h1>
      <span v-if="items.length" class="page-count">{{ items.length }} 条</span>
    </header>

    <div v-if="loading" class="empty-state">加载中...</div>
    <div v-else-if="!items.length" class="empty-state">暂无历史通知</div>

    <div v-else class="history-list">
      <article
        v-for="item in items"
        :key="item.id"
        class="history-item"
        :class="`is-${item.kind}`"
      >
        <span class="history-icon">
          <svg
            v-if="item.kind === 'success'"
            viewBox="0 0 16 16"
            width="14"
            height="14"
            fill="none"
          >
            <path
              d="M3 8.5 6.5 12 13 4.5"
              stroke="#fff"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
          <svg
            v-else-if="item.kind === 'error'"
            viewBox="0 0 16 16"
            width="14"
            height="14"
            fill="none"
          >
            <path d="M4 4l8 8M12 4l-8 8" stroke="#fff" stroke-width="2" stroke-linecap="round" />
          </svg>
          <svg
            v-else-if="item.kind === 'warning'"
            viewBox="0 0 16 16"
            width="14"
            height="14"
            fill="none"
          >
            <path d="M8 3.5v6.5M8 13.5v.02" stroke="#fff" stroke-width="2" stroke-linecap="round" />
          </svg>
          <svg v-else viewBox="0 0 16 16" width="14" height="14" fill="none">
            <path d="M8 7v4.5M8 4.2v.02" stroke="#fff" stroke-width="2" stroke-linecap="round" />
          </svg>
        </span>

        <div class="history-body">
          <div v-if="item.entry_trigger" class="entry-line">
            <span class="entry-name">
              {{ item.entry_trigger.name || item.entry_trigger.symbol }}
            </span>
            <span
              class="entry-dir"
              :class="item.entry_trigger.direction === 'up' ? 'is-up' : 'is-down'"
            >
              {{ directionLabel(item.entry_trigger.direction) }}
            </span>
            <span class="entry-price">入场 {{ fmtPrice(item.entry_trigger.entry) }}</span>
            <span class="entry-price">最新 {{ fmtPrice(item.entry_trigger.latest) }}</span>
          </div>

          <div v-else-if="item.signal" class="signal-line">
            <span class="signal-code">{{ item.signal.code }}</span>
            <span class="signal-name">{{ item.signal.name || item.signal.code }}</span>
            <span
              class="signal-dir"
              :class="item.signal.direction === 'up' ? 'is-up' : 'is-down'"
            >
              {{ directionLabel(item.signal.direction) }}
            </span>
            <span class="signal-level">
              {{ levelLabel(item.signal.level) }}N · {{ item.signal.grade }}
            </span>
            <span class="signal-score">评分 {{ item.signal.score.toFixed(2) }}</span>
            <span v-if="item.signal.time" class="signal-time">{{ item.signal.time }}</span>
          </div>

          <template v-else>
            <div class="history-text-row">
              <span v-if="kindLabel(item)" class="history-kind">{{ kindLabel(item) }}</span>
              <div class="history-text">
                <div v-if="item.title" class="history-title">{{ item.title }}</div>
                <div class="history-content">{{ item.content }}</div>
              </div>
            </div>
          </template>
        </div>
        <time class="history-time">{{ item.created_at }}</time>
      </article>
    </div>
  </div>
</template>

<style scoped>
.page {
  height: 100%;
  box-sizing: border-box;
  overflow-y: auto;
  padding: 16px;
  background: #f5f7fa;
}
.page-header {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 2px 2px 14px;
}
.page-header h1 {
  margin: 0;
  font-size: 18px;
  font-weight: 700;
  color: #1f2329;
}
.page-count {
  font-size: 13px;
  color: #94a3b8;
  font-variant-numeric: tabular-nums;
}
.history-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding-bottom: 8px;
}
.history-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 14px;
  background: #fff;
  border: 1px solid #eef0f3;
  border-radius: 8px;
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.04);
}
.history-icon {
  flex: none;
  width: 26px;
  height: 26px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.history-item.is-success .history-icon {
  background: #18a058;
}
.history-item.is-error .history-icon {
  background: #e03131;
}
.history-item.is-info .history-icon {
  background: #1677ff;
}
.history-item.is-warning .history-icon {
  background: #f59e0b;
}
.history-body {
  flex: 1;
  min-width: 0;
}
.history-text-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.history-text {
  min-width: 0;
}
.history-kind {
  flex: none;
  font-size: 13px;
  font-weight: 700;
  color: #334155;
}
.history-time {
  flex: none;
  font-size: 15px;
  font-weight: 700;
  color: #334155;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.history-title {
  font-size: 15px;
  font-weight: 700;
  color: #1f2329;
  line-height: 1.4;
  margin-bottom: 2px;
}
.history-content {
  font-size: 14px;
  color: #475569;
  line-height: 1.55;
  word-break: break-word;
  overflow-wrap: anywhere;
}
.signal-line,
.entry-line {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 14px;
  color: #334155;
}
.signal-code {
  font-size: 12px;
  color: #64748b;
  font-variant-numeric: tabular-nums;
}
.signal-name,
.entry-name {
  font-weight: 700;
  color: #1f2329;
}
.signal-dir,
.entry-dir {
  flex: none;
  display: inline-flex;
  align-items: center;
  font-size: 12px;
  font-weight: 600;
  line-height: 1;
  padding: 3px 8px;
  border-radius: 999px;
}
.signal-dir.is-up,
.entry-dir.is-up {
  color: #e03131;
  background: rgba(224, 49, 49, 0.1);
}
.signal-dir.is-down,
.entry-dir.is-down {
  color: #0f9d58;
  background: rgba(15, 157, 88, 0.1);
}
.signal-level,
.signal-score {
  flex: none;
  display: inline-flex;
  align-items: center;
  font-size: 12px;
  font-weight: 600;
  line-height: 1;
  padding: 3px 8px;
  border-radius: 999px;
  color: #7c5cff;
  background: rgba(124, 92, 255, 0.1);
}
.signal-time {
  font-size: 12px;
  color: #94a3b8;
  font-variant-numeric: tabular-nums;
}
.entry-price {
  font-size: 13px;
  color: #64748b;
  font-variant-numeric: tabular-nums;
}
.empty-state {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 240px;
  font-size: 14px;
  color: #94a3b8;
}
</style>
