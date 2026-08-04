<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'

const props = defineProps<{
  /** 要显示的文本 */
  text: string
}>()

const el = ref<HTMLSpanElement | null>(null)
/** 文本是否被截断（超长省略） */
const overflow = ref(false)

function check() {
  const node = el.value
  if (!node) return
  overflow.value = node.scrollWidth > node.clientWidth + 1
}

let observer: ResizeObserver | null = null

onMounted(() => {
  check()
  observer = new ResizeObserver(check)
  observer.observe(el.value!)
})

onBeforeUnmount(() => {
  observer?.disconnect()
})
</script>

<template>
  <!-- 只有文本真的被截断时才带 title，悬浮显示完整内容 -->
  <span ref="el" class="overflow-text" :title="overflow ? text : undefined">{{ text }}</span>
</template>

<style scoped>
.overflow-text {
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  vertical-align: top;
}
</style>
