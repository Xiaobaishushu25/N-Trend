<script setup lang="ts">
import { NCard, NTag, NSpace, NText, NDescriptions, NDescriptionsItem, NDivider } from 'naive-ui'
import type { SignalOutcome, SignalRow } from '../types'

const props = defineProps<{ signal: SignalOutcome | SignalRow }>()

const s = props.signal as any
const dirLabel = s.direction === 'up' ? '做多' : s.direction === 'down' ? '做空' : s.direction
const dirType = s.direction === 'up' ? 'success' : s.direction === 'down' ? 'error' : 'default'
const levelLabel = s.level === 'fine' ? '精细' : s.level === 'large' ? '较大' : s.level
const stateType =
  s.state === '即将触发'
    ? 'info'
    : s.state === '当前已触发'
      ? 'success'
      : s.state === '已触发，接近时效边界'
        ? 'warning'
        : 'default'
</script>

<template>
  <n-card size="small" :bordered="true">
    <n-space align="center" justify="space-between">
      <n-space align="center">
        <n-text strong style="font-size: 15px">{{ s.symbol }}</n-text>
        <n-tag :type="dirType" size="small" round>{{ dirLabel }}</n-tag>
        <n-tag size="small" round>{{ levelLabel }} N</n-tag>
        <n-tag :type="stateType" size="small" round>{{ s.state }}</n-tag>
      </n-space>
      <n-text :depth="s.score >= 3.5 ? 1 : 3">
        评分 <n-text strong :type="s.score >= 3.5 ? 'success' : 'warning'">{{ Number(s.score).toFixed(2) }}</n-text>
      </n-text>
    </n-space>
    <n-descriptions size="small" :column="4" style="margin-top: 10px">
      <n-descriptions-item label="入场">{{ Number(s.entry).toFixed(1) }}</n-descriptions-item>
      <n-descriptions-item label="止损">{{ Number(s.stop).toFixed(1) }}</n-descriptions-item>
      <n-descriptions-item label="目标">{{ Number(s.target).toFixed(1) }}</n-descriptions-item>
      <n-descriptions-item label="RR">{{ Number(s.rr).toFixed(2) }}</n-descriptions-item>
    </n-descriptions>
    <n-text depth="3" style="font-size: 12px">{{ s.note || s.category }}</n-text>
  </n-card>
</template>
