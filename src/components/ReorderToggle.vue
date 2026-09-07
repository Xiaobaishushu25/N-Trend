<script setup lang="ts">
import { NIcon, NSwitch } from 'naive-ui'
import { GripVertical } from '@vicons/tabler'

withDefaults(
  defineProps<{
    modelValue: boolean
    disabled?: boolean
    title?: string
  }>(),
  {
    disabled: false,
    title: '拖拽排序开关',
  },
)

const emit = defineEmits<{
  (event: 'update:modelValue', value: boolean): void
}>()
</script>

<template>
  <div
    class="reorder-toggle"
    :class="{ 'is-enabled': modelValue && !disabled, disabled }"
    :title="title"
  >
    <n-icon :component="GripVertical" class="reorder-icon" :size="14" />
    <n-switch
      :value="modelValue"
      size="small"
      :disabled="disabled"
      :rail-style="() => ({ background: modelValue && !disabled ? '#3b82f6' : undefined })"
      aria-label="拖拽排序"
      @update:value="(value) => emit('update:modelValue', value)"
    />
  </div>
</template>

<style scoped>
.reorder-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex: none;
  padding: 4px 7px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 999px;
  transition: all 0.2s ease;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}
.reorder-toggle:hover {
  border-color: #cbd5e1;
  background: #f1f5f9;
}
.reorder-toggle.is-enabled {
  background: linear-gradient(135deg, #eff6ff 0%, #dbeafe 100%);
  border-color: #93c5fd;
  box-shadow: 0 1px 6px rgba(59, 130, 246, 0.18);
}
.reorder-toggle.disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.reorder-icon {
  color: #94a3b8;
  transition: color 0.2s;
  flex: none;
}
.reorder-toggle.is-enabled .reorder-icon {
  color: #3b82f6;
}
</style>
