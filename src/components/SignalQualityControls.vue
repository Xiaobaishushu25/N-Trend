<script setup lang="ts">
import { useSignalQualityStore } from '../stores/signalQuality'
defineProps<{ sort: boolean }>()
const emit = defineEmits<{ 'update:sort': [value: boolean] }>()
const display = useSignalQualityStore()
</script>
<template>
  <span class="quality-controls" @click.stop>
    <label><input type="checkbox" :checked="display.enabled" @change="display.setEnabled(($event.target as HTMLInputElement).checked)"> 新评分</label>
    <label title="仅改变展示顺序，交易和通知仍使用旧入场分"><input type="checkbox" :disabled="!display.enabled" :checked="sort" @change="emit('update:sort', ($event.target as HTMLInputElement).checked)"> 质量排序</label>
  </span>
</template>
<style scoped>
.quality-controls{display:inline-flex;gap:10px;align-items:center;font-size:11px;font-weight:400;color:#64748b;flex-wrap:wrap}.quality-controls label{display:inline-flex;align-items:center;gap:3px;white-space:nowrap}.quality-controls input{accent-color:#5270be}
</style>
