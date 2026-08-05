<script setup lang="ts">
import { dismiss, notifyItems, resume, suspend } from '../utils/notify'
</script>

<template>
  <div class="notify-root">
    <transition-group name="notify" tag="div" class="notify-list">
      <div
        v-for="item in notifyItems"
        :key="item.id"
        class="notify-item"
        :class="`is-${item.type}`"
        @mouseenter="item.keepAliveOnHover && suspend(item.id)"
        @mouseleave="item.keepAliveOnHover && resume(item.id)"
      >
        <span class="notify-icon">
          <svg
            v-if="item.type === 'success'"
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
            v-else-if="item.type === 'error'"
            viewBox="0 0 16 16"
            width="14"
            height="14"
            fill="none"
          >
            <path d="M4 4l8 8M12 4l-8 8" stroke="#fff" stroke-width="2" stroke-linecap="round" />
          </svg>
          <svg
            v-else-if="item.type === 'info'"
            viewBox="0 0 16 16"
            width="14"
            height="14"
            fill="none"
          >
            <path
              d="M8 7v4.5M8 4.2v.02"
              stroke="#fff"
              stroke-width="2"
              stroke-linecap="round"
            />
          </svg>
          <svg v-else viewBox="0 0 16 16" width="14" height="14" fill="none">
            <path d="M8 4v5.5M8 12v.02" stroke="#fff" stroke-width="2" stroke-linecap="round" />
          </svg>
        </span>

        <div class="notify-body">
          <div v-if="item.title" class="notify-title">{{ item.title }}</div>
          <div class="notify-content">{{ item.content }}</div>
        </div>

        <button
          v-if="item.closable"
          type="button"
          class="notify-close"
          aria-label="关闭通知"
          @click="dismiss(item.id)"
        >
          <svg viewBox="0 0 12 12" width="10" height="10" fill="none">
            <path d="M2.5 2.5l7 7M9.5 2.5l-7 7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
          </svg>
        </button>
      </div>
    </transition-group>
  </div>
</template>

<style scoped>
.notify-root {
  position: fixed;
  bottom: 66px;
  right: 16px;
  z-index: 9999;
  pointer-events: none;
}
.notify-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  align-items: flex-end;
}
.notify-item {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 280px;
  max-width: 380px;
  padding: 12px 12px 12px 14px;
  background: #fff;
  border: 1px solid #eef0f3;
  border-radius: 6px;
  box-shadow: 0 8px 24px rgba(15, 23, 42, 0.12);
  pointer-events: auto;
  font-family:
    "Segoe UI",
    "PingFang SC",
    "Microsoft YaHei",
    system-ui,
    sans-serif;
}
.notify-icon {
  flex: none;
  width: 26px;
  height: 26px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.notify-item.is-success .notify-icon {
  background: #18a058;
}
.notify-item.is-error .notify-icon {
  background: #e03131;
}
.notify-item.is-info .notify-icon {
  background: #1677ff;
}
.notify-item.is-warning .notify-icon {
  background: #f59e0b;
}
.notify-body {
  flex: 1;
  min-width: 0;
}
.notify-title {
  font-size: 15px;
  font-weight: 700;
  color: #1f2329;
  line-height: 1.4;
  margin-bottom: 2px;
}
.notify-content {
  font-size: 14px;
  color: #475569;
  line-height: 1.55;
  word-break: break-word;
}
.notify-close {
  flex: none;
  width: 20px;
  height: 20px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: #64748b;
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}
.notify-close:hover {
  background: #f1f5f9;
  color: #1f2329;
}

/* 进入/离开/位移动画 */
.notify-enter-active,
.notify-leave-active {
  transition:
    opacity 0.25s ease,
    transform 0.25s ease;
}
.notify-enter-from {
  opacity: 0;
  transform: translateX(20px);
}
.notify-leave-to {
  opacity: 0;
  transform: translateX(20px) scale(0.98);
}
.notify-move {
  transition: transform 0.25s ease;
}
</style>
