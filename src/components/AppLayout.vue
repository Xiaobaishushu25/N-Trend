<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { NIcon, NLayout, NLayoutContent, NLayoutHeader, NTag, NText } from 'naive-ui'
import { TrendingUp } from '@vicons/tabler'
import { useAppStore } from '../stores/app'
import { useSettingsStore } from '../stores/settings'

const route = useRoute()
const appStore = useAppStore()
const settingsStore = useSettingsStore()

const bare = computed(() => Boolean(route.meta.bare))

onMounted(() => {
  settingsStore.load()
})
</script>

<template>
  <n-layout style="height: 100vh">
    <n-layout-header v-if="!bare" bordered class="topbar">
      <div class="brand">
        <n-icon :component="TrendingUp" size="22" color="#f5c23f" />
        <span class="brand-name">N趋势</span>
        <n-text depth="3" style="font-size: 12px">v{{ appStore.info.version }}</n-text>
      </div>
      <n-tag v-if="settingsStore.status.running" type="success" size="small" round>
        定时扫描运行中
      </n-tag>
      <n-tag v-else type="warning" size="small" round>定时扫描已暂停</n-tag>
    </n-layout-header>
    <n-layout-content
      :native-scrollbar="false"
      :content-style="bare ? 'height: 100%; padding: 0' : 'padding: 16px'"
    >
      <router-view />
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
</style>
