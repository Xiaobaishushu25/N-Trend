<script setup lang="ts">
import { onMounted, ref } from 'vue'
import {
  NButton,
  NCard,
  NForm,
  NFormItem,
  NInput,
  NInputNumber,
  NSwitch,
  NSelect,
  NSpace,
  NDivider,
  NText,
} from 'naive-ui'
import { useSettingsStore } from '../stores/settings'
import { useSymbolsStore } from '../stores/symbols'
import { notify } from '../utils/notify'
import type { Settings } from '../types'

const settingsStore = useSettingsStore()
const symbolsStore = useSymbolsStore()

const form = ref<Settings>({ ...settingsStore.settings })
const saving = ref(false)
const refreshingSymbols = ref(false)

const logLevels = [
  { label: 'trace', value: 'trace' },
  { label: 'debug', value: 'debug' },
  { label: 'info', value: 'info' },
  { label: 'warn', value: 'warn' },
  { label: 'error', value: 'error' },
]

async function save() {
  saving.value = true
  try {
    await settingsStore.save(form.value)
    notify.success('设置已保存')
  } catch (e) {
    notify.error(String(e))
  } finally {
    saving.value = false
  }
}

async function refreshSymbols() {
  refreshingSymbols.value = true
  try {
    const count = await symbolsStore.refreshList()
    notify.success(`已刷新品种列表，共 ${count} 个`)
  } catch (e) {
    notify.error(String(e))
  } finally {
    refreshingSymbols.value = false
  }
}

onMounted(async () => {
  await settingsStore.load()
  form.value = { ...settingsStore.settings }
  await symbolsStore.load()
})
</script>

<template>
  <n-space vertical size="large">
    <n-card title="定时扫描" size="small">
      <n-form label-placement="left" label-width="180">
        <n-form-item label="数据刷新间隔（秒）">
          <n-input-number v-model:value="form.refresh_interval_secs" :min="60" :max="3600" style="width: 200px" />
        </n-form-item>
        <n-form-item label="扫描间隔（秒）">
          <n-input-number v-model:value="form.scan_interval_secs" :min="300" :max="7200" style="width: 200px" />
        </n-form-item>
        <n-form-item label="仅交易时段运行">
          <n-switch v-model:value="form.trading_only" />
        </n-form-item>
        <n-form-item label="启动时自动运行">
          <n-switch v-model:value="form.auto_start_scheduler" />
        </n-form-item>
        <n-form-item label="定时扫描当前状态">
          <n-text :type="settingsStore.status.running ? 'success' : 'warning'">
            {{ settingsStore.status.running ? '运行中' : '已暂停' }}
          </n-text>
          <n-button
            size="small"
            style="margin-left: 12px"
            :type="settingsStore.status.running ? 'warning' : 'success'"
            @click="settingsStore.setRunning(!settingsStore.status.running)"
          >
            {{ settingsStore.status.running ? '暂停' : '启动' }}
          </n-button>
        </n-form-item>
      </n-form>
    </n-card>

    <n-card title="请求节流" size="small">
      <n-form label-placement="left" label-width="180">
        <n-form-item label="单请求间隔（毫秒）">
          <n-input-number v-model:value="form.request_interval_ms" :min="100" :max="10000" style="width: 200px" />
        </n-form-item>
        <n-form-item label="每分钟请求上限">
          <n-input-number v-model:value="form.minutely_budget" :min="5" :max="300" style="width: 200px" />
        </n-form-item>
        <n-form-item label="首抓/回补根数">
          <n-input-number v-model:value="form.backfill_count" :min="50" :max="2000" style="width: 200px" />
        </n-form-item>
        <n-form-item label="增量抓取根数">
          <n-input-number v-model:value="form.incremental_count" :min="3" :max="100" style="width: 200px" />
        </n-form-item>
      </n-form>
    </n-card>

    <n-card title="品种管理" size="small">
      <n-space>
        <n-button :loading="refreshingSymbols" @click="refreshSymbols">从新浪刷新品种列表</n-button>
        <n-text depth="3">当前库内 {{ symbolsStore.symbols.length }} 个品种</n-text>
      </n-space>
    </n-card>

    <n-card title="邮件通知" size="small">
      <n-form label-placement="left" label-width="180">
        <n-form-item label="启用邮件">
          <n-switch v-model:value="form.email.enabled" />
        </n-form-item>
        <n-form-item label="收件人（逗号分隔）">
          <n-input v-model:value="form.email.to" placeholder="a@example.com,b@example.com" />
        </n-form-item>
        <n-form-item label="发件人">
          <n-input v-model:value="form.email.from" placeholder="your@qq.com" />
        </n-form-item>
        <n-form-item label="SMTP 主机">
          <n-input v-model:value="form.email.smtp_host" placeholder="smtp.qq.com" />
        </n-form-item>
        <n-form-item label="SMTP 端口">
          <n-input-number v-model:value="form.email.smtp_port" :min="1" :max="65535" style="width: 200px" />
        </n-form-item>
        <n-form-item label="SMTP 账号">
          <n-input v-model:value="form.email.smtp_user" placeholder="your@qq.com" />
        </n-form-item>
        <n-form-item label="SMTP 授权码">
          <n-input v-model:value="form.email.smtp_password" type="password" show-password-on="click" placeholder="QQ邮箱授权码" />
        </n-form-item>
      </n-form>
    </n-card>

    <n-card title="日志" size="small">
      <n-form label-placement="left" label-width="180">
        <n-form-item label="日志级别">
          <n-select v-model:value="form.log_level" :options="logLevels" style="width: 200px" />
        </n-form-item>
      </n-form>
    </n-card>

    <n-divider />
    <n-space justify="end">
      <n-button type="primary" :loading="saving" @click="save">保存设置</n-button>
    </n-space>
  </n-space>
</template>
