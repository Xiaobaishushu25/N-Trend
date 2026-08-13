<script setup lang="ts">
import { h, onMounted, ref } from 'vue'
import {
  NButton,
  NCard,
  NDataTable,
  NEmpty,
  NSpace,
  NTag,
  NText,
  type DataTableColumns,
} from 'naive-ui'
import { useScansStore } from '../stores/scans'
import { notify } from '../utils/notify'
import type { ScanRow, SignalRow } from '../types'

const scansStore = useScansStore()
const selectedScan = ref<ScanRow | null>(null)

const scanColumns: DataTableColumns<ScanRow> = [
  { title: 'ID', key: 'id', width: 70 },
  { title: '开始时间', key: 'started_at', width: 170 },
  { title: '完成时间', key: 'finished_at', width: 170 },
  { title: '扫描品种', key: 'scanned', width: 90 },
  { title: '信号数', key: 'active_count', width: 90 },
  {
    title: '状态',
    key: 'status',
    width: 90,
    render: (r) =>
      h(
        NTag,
        { type: r.status === 'ok' ? 'success' : 'warning', size: 'small' },
        { default: () => r.status },
      ),
  },
  {
    title: '操作',
    key: 'actions',
    render: (r) =>
      h(NButton, { size: 'small', onClick: () => openDetail(r) }, { default: () => '查看信号' }),
  },
]

const signalColumns: DataTableColumns<SignalRow> = [
  { title: '品种', key: 'symbol', width: 90 },
  {
    title: '方向',
    key: 'direction',
    width: 80,
    render: (r) => (r.direction === 'up' ? '做多' : '做空'),
  },
  {
    title: '级别',
    key: 'level',
    width: 80,
    render: (r) =>
      r.level === 'fine' ? '精细' : r.level === 'large' ? '较大' : r.level === 'box' ? '箱体' : r.level,
  },
  { title: '状态', key: 'state', width: 130 },
  { title: '入场', key: 'entry', width: 90, render: (r) => r.entry.toFixed(1) },
  { title: '止损', key: 'stop', width: 90, render: (r) => r.stop.toFixed(1) },
  { title: '目标', key: 'target', width: 90, render: (r) => r.target.toFixed(1) },
  { title: 'RR', key: 'rr', width: 80, render: (r) => r.rr.toFixed(2) },
  { title: '评分', key: 'score', width: 90, render: (r) => r.score.toFixed(2) },
  { title: '备注', key: 'note', ellipsis: { tooltip: true } },
]

async function openDetail(row: ScanRow) {
  selectedScan.value = row
  await scansStore.loadDetail(row.id)
}

async function runScan() {
  await scansStore.runScan()
  notify.success('扫描完成')
  if (selectedScan.value && scansStore.latest) {
    await openDetail(scansStore.latest as unknown as ScanRow)
  }
}

onMounted(() => scansStore.loadHistory(50))
</script>

<template>
  <n-space vertical size="large">
    <n-card size="small">
      <n-space justify="space-between" align="center">
        <n-text strong style="font-size: 16px">扫描历史</n-text>
        <n-button type="success" :loading="scansStore.running" @click="runScan">立即扫描</n-button>
      </n-space>
      <n-data-table
        :columns="scanColumns"
        :data="scansStore.history"
        size="small"
        style="margin-top: 12px"
      />
      <n-empty v-if="!scansStore.history.length" description="暂无扫描记录" style="margin-top: 12px" />
    </n-card>

    <n-card v-if="selectedScan" :title="`扫描 #${selectedScan.id} 信号明细`" size="small">
      <n-data-table :columns="signalColumns" :data="scansStore.detail" size="small" />
      <n-empty v-if="!scansStore.detail.length" description="该次扫描没有信号" style="margin-top: 12px" />
    </n-card>
  </n-space>
</template>
