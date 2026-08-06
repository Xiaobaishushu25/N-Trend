<script setup lang="ts">
import {
  NConfigProvider,
  NDialogProvider,
  NMessageProvider,
  NNotificationProvider,
  zhCN,
  dateZhCN,
} from 'naive-ui'
import { onBeforeUnmount, onMounted } from 'vue'
import AppLayout from './components/AppLayout.vue'
import { useAppStore } from './stores/app'

const appStore = useAppStore()

/** 全局禁用浏览器默认右键菜单：整个 App 内右键统一走自定义菜单 */
function preventNativeContextMenu(e: MouseEvent) {
  e.preventDefault()
}

onMounted(() => {
  appStore.init()
  window.addEventListener('contextmenu', preventNativeContextMenu, true)
})

onBeforeUnmount(() => {
  window.removeEventListener('contextmenu', preventNativeContextMenu, true)
})
</script>

<template>
  <n-config-provider :locale="zhCN" :date-locale="dateZhCN">
    <n-dialog-provider>
      <n-notification-provider>
        <n-message-provider>
          <AppLayout />
        </n-message-provider>
      </n-notification-provider>
    </n-dialog-provider>
  </n-config-provider>
</template>
