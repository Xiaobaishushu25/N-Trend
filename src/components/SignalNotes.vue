<script setup lang="ts">
import { ref, watch } from 'vue'
import { NButton, NCheckbox, NIcon, NInput } from 'naive-ui'
import { Trash } from '@vicons/tabler'
import { api } from '../services/api'
import { notify } from '../utils/notify'
import type { SignalAnnotation } from '../types'

const props = defineProps<{ eventId: number }>()

const annotations = ref<SignalAnnotation[]>([])
const opened = ref<boolean | null>(null)
const loading = ref(false)
const adding = ref(false)
const saving = ref(false)
const input = ref('')

let seq = 0
async function load() {
  if (!props.eventId) return
  const current = ++seq
  loading.value = true
  try {
    const data = await api.getSignalUserData(props.eventId)
    if (current !== seq) return
    annotations.value = data.annotations
    opened.value = data.opened
  } catch (e) {
    if (current === seq) notify.error(String(e))
  } finally {
    if (current === seq) loading.value = false
  }
}

watch(() => props.eventId, load, { immediate: true })

async function add() {
  const content = input.value.trim()
  if (!content) return
  adding.value = true
  try {
    const row = await api.addSignalAnnotation(props.eventId, content)
    annotations.value = [...annotations.value, row]
    input.value = ''
  } catch (e) {
    notify.error(String(e))
  } finally {
    adding.value = false
  }
}

async function remove(row: SignalAnnotation) {
  try {
    await api.deleteSignalAnnotation(row.id)
    annotations.value = annotations.value.filter((a) => a.id !== row.id)
  } catch (e) {
    notify.error(String(e))
  }
}

async function saveOpened(value: boolean | string | number) {
  const checked = value === true || value === 1 || value === 'true'
  saving.value = true
  try {
    const row = await api.setSignalDecision(props.eventId, checked)
    opened.value = row.opened
  } catch (e) {
    notify.error(String(e))
  } finally {
    saving.value = false
  }
}

function fmtTime(s: string) {
  return s.slice(0, 16).replace('T', ' ')
}
</script>

<template>
  <div class="signal-notes" @click.stop>
    <div class="sn-top">
      <n-checkbox
        :checked="opened === true"
        :loading="saving"
        size="small"
        @update:checked="saveOpened"
      >
        已按建议开仓
      </n-checkbox>
      <span class="sn-count">{{ loading ? '批注读取中' : `批注 ${annotations.length}` }}</span>
    </div>

    <div v-if="annotations.length" class="sn-list">
      <div v-for="row in annotations" :key="row.id" class="sn-item">
        <div class="sn-text">{{ row.content }}</div>
        <div class="sn-meta">
          <span>{{ fmtTime(row.created_at) }}</span>
          <n-button
            size="tiny"
            quaternary
            type="error"
            title="删除批注"
            @click.stop="remove(row)"
          >
            <template #icon>
              <n-icon :component="Trash" />
            </template>
          </n-button>
        </div>
      </div>
    </div>

    <div class="sn-add">
      <n-input
        v-model:value="input"
        size="small"
        placeholder="记录当时的想法"
        @keyup.enter="add"
      />
      <n-button size="small" secondary :loading="adding" @click.stop="add">
        添加
      </n-button>
    </div>
  </div>
</template>

<style scoped>
.signal-notes {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed #d8dee8;
}
.sn-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.sn-count {
  font-size: 11px;
  color: #7c8698;
}
.sn-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 120px;
  overflow: auto;
}
.sn-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px 8px;
  border-radius: 6px;
  background: #f6f8fb;
}
.sn-text {
  font-size: 12px;
  line-height: 1.5;
  color: #1f2937;
  white-space: pre-wrap;
  word-break: break-word;
}
.sn-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  font-size: 11px;
  color: #9aa3b2;
}
.sn-add {
  display: flex;
  align-items: center;
  gap: 6px;
}
</style>
