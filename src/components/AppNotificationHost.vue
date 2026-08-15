<script setup lang="ts">
import { dismiss, notifyItems, resume, suspend } from '../utils/notify'
import type { NotifyItem } from '../utils/notify'
import router from '../router'

/** 点击信号通知跳转到对应品种的K线图，并顺手关闭该通知 */
function openSignalChart(item: NotifyItem) {
  const code = item.signal?.code ?? item.entryTrigger?.symbol
  if (!code) return
  dismiss(item.id)
  router.push({ name: 'chart', params: { symbol: code } })
}

/** 价格显示：整数不带小数，否则保留 1 位 */
function fmtPrice(v: number): string {
  return Number.isInteger(v) ? v.toFixed(0) : v.toFixed(1)
}
</script>

<template>
  <div class="notify-root">
    <transition-group name="notify" tag="div" class="notify-list">
      <div
        v-for="item in notifyItems"
        :key="item.id"
        class="notify-item"
        :class="[`is-${item.type}`, { 'is-clickable': item.signal || item.entryTrigger }]"
        @click="openSignalChart(item)"
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
          <template v-if="item.entryTrigger">
            <div class="ns-entry">
              <div class="ns-entry-head">
                <span class="ns-entry-name">
                  {{ item.entryTrigger.name || item.entryTrigger.symbol }}
                </span>
                <span
                  class="ns-entry-dir"
                  :class="item.entryTrigger.direction === 'up' ? 'is-up' : 'is-down'"
                >
                  {{ item.entryTrigger.direction === 'up' ? '做多' : '做空' }}
                </span>
              </div>
              <div class="ns-entry-prices">
                <span class="ns-entry-price">
                  入场价 <b>{{ fmtPrice(item.entryTrigger.entry) }}</b>
                </span>
                <span class="ns-entry-arrow">→</span>
                <span class="ns-entry-price">
                  最新 <b>{{ fmtPrice(item.entryTrigger.latest) }}</b>
                </span>
              </div>
            </div>
          </template>
          <template v-else-if="item.signal">
            <div class="ns-inline">
              <span class="ns-code">{{ item.signal.code }}</span>
              <span class="ns-name">{{ item.signal.name || item.signal.code }}</span>
              <span class="ns-dir" :class="item.signal.direction === 'up' ? 'is-up' : 'is-down'">
                {{ item.signal.direction === 'up' ? '做多' : '做空' }}
              </span>
              <span class="ns-state">
                <span class="ns-state-dot"></span>即将触发
              </span>
              <span class="ns-score">评分 <b>{{ item.signal.score.toFixed(2) }}</b></span>
              <span class="ns-time">{{ item.signal.time }}</span>
            </div>
          </template>
          <template v-else>
            <div v-if="item.title" class="notify-title">{{ item.title }}</div>
            <div class="notify-content">{{ item.content }}</div>
          </template>
        </div>

        <button
          v-if="item.closable"
          type="button"
          class="notify-close"
          aria-label="关闭通知"
          @click.stop="dismiss(item.id)"
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
  display: flex;
  flex-direction: column;
  /* 40px 标题栏 + 12px 间距 + 66px 底部留白，通知区始终不盖住标题栏 */
  max-height: calc(100vh - 40px - 66px - 12px);
}
.notify-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  align-items: flex-end;
  min-height: 0;
  max-height: 100%;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding-right: 4px;
  pointer-events: auto;
  scrollbar-width: thin;
  scrollbar-color: #cbd5e1 transparent;
}
.notify-list::-webkit-scrollbar {
  width: 6px;
}
.notify-list::-webkit-scrollbar-thumb {
  background: #cbd5e1;
  border-radius: 3px;
}
.notify-list::-webkit-scrollbar-track {
  background: transparent;
}
.notify-item {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 300px;
  max-width: 420px;
  padding: 10px 12px 10px 14px;
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
.notify-item.is-clickable {
  cursor: pointer;
}
.notify-item.is-clickable:hover {
  border-color: #dbe4ee;
  box-shadow: 0 10px 28px rgba(15, 23, 42, 0.16);
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
/* 信号卡片通知：单行紧凑布局，全部信息放在同一行 */
.ns-inline {
  display: flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
  min-width: 0;
}
.ns-code {
  flex: none;
  font-size: 12px;
  color: #64748b;
  font-variant-numeric: tabular-nums;
}
.ns-name {
  flex: 1;
  min-width: 0;
  font-size: 14px;
  font-weight: 700;
  color: #1f2329;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ns-state {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  font-weight: 600;
  color: #1677ff;
  background: rgba(22, 119, 255, 0.08);
  padding: 2px 8px;
  border-radius: 999px;
}
.ns-state-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #1677ff;
}
.ns-dir {
  flex: none;
  display: inline-flex;
  align-items: center;
  font-size: 12px;
  font-weight: 600;
  line-height: 1;
  padding: 2px 8px;
  border-radius: 999px;
}
.ns-dir.is-up {
  color: #e03131;
  background: rgba(224, 49, 49, 0.1);
}
.ns-dir.is-down {
  color: #0f9d58;
  background: rgba(15, 157, 88, 0.1);
}
.ns-score {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: 12px;
  font-weight: 600;
  color: #7c5cff;
  background: rgba(124, 92, 255, 0.08);
  padding: 2px 8px;
  border-radius: 999px;
  white-space: nowrap;
}
.ns-score b {
  font-size: 12px;
  font-weight: 800;
  color: #7c5cff;
  font-variant-numeric: tabular-nums;
}
.ns-time {
  flex: none;
  font-size: 12px;
  color: #94a3b8;
  font-variant-numeric: tabular-nums;
}
/* 入场价提醒卡片：名称 + 方向 + 入场/最新价 */
.ns-entry {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.ns-entry-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.ns-entry-name {
  font-size: 15px;
  font-weight: 800;
  color: #1f2329;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ns-entry-dir {
  flex: none;
  display: inline-flex;
  align-items: center;
  font-size: 12px;
  font-weight: 600;
  line-height: 1;
  padding: 2px 8px;
  border-radius: 999px;
}
.ns-entry-dir.is-up {
  color: #e03131;
  background: rgba(224, 49, 49, 0.1);
}
.ns-entry-dir.is-down {
  color: #0f9d58;
  background: rgba(15, 157, 88, 0.1);
}
.ns-entry-prices {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: #64748b;
}
.ns-entry-price b {
  font-size: 14px;
  color: #1f2329;
  font-variant-numeric: tabular-nums;
}
.ns-entry-arrow {
  color: #c2c9d4;
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
