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

/** 全局禁用浏览器默认右键菜单：整个 App 内右键统一走自定义菜单，但放行输入框让原生粘贴菜单正常呼出 */
function preventNativeContextMenu(e: MouseEvent) {
  const target = e.target as HTMLElement | null
  if (target) {
    const tagName = target.tagName?.toLowerCase()
    if (tagName === 'input' || tagName === 'textarea' || target.isContentEditable) {
      return
    }
  }
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
          <div v-if="appStore.connectionStatus === 'version_mismatch'" role="alert" class="version-error">
            版本不匹配，已停止连接。请将客户端和服务端同时升级到同一最新版本（当前客户端 {{ appStore.info.version }}）。
          </div>
        </n-message-provider>
      </n-notification-provider>
    </n-dialog-provider>
  </n-config-provider>
</template>

<style scoped>
.version-error{position:fixed;top:48px;left:16px;right:16px;z-index:10000;padding:14px 18px;border:1px solid #f1a7a7;border-radius:8px;background:#fff1f0;color:#a61d24;font-size:14px;box-shadow:0 2px 12px #0001}
</style>
