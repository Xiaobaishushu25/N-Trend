<script setup lang="ts">
import { computed, defineComponent, h, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  NAlert,
  NButton,
  NButtonGroup,
  NCard,
  NDataTable,
  NIcon,
  NInput,
  NInputNumber,
  NPopconfirm,
  NRadioButton,
  NRadioGroup,
  NScrollbar,
  NSelect,
  NSpace,
  NSpin,
  NSwitch,
  NTabPane,
  NTabs,
  NTag,
  NText,
  NTooltip,
  useDialog,
  useMessage,
  type DataTableColumns,
} from 'naive-ui'
import {
  ArrowLeft,
  Bell,
  ChartCandle,
  Check,
  Cloud,
  Cpu,
  Database,
  DeviceFloppy,
  Devices,
  Eye,
  EyeOff,
  Folder,
  Help,
  Key,
  Lock,
  Mail,
  Refresh,
  RotateClockwise,
  Ruler,
  Server,
  Settings as SettingsIcon,
  Shield,
  Trash,
} from '@vicons/tabler'
import { isTauri } from '@tauri-apps/api/core'
import { emit } from '@tauri-apps/api/event'
import {
  disable as disableAutoLaunch,
  enable as enableAutoLaunch,
  isEnabled as isAutoLaunchEnabled,
} from '@tauri-apps/plugin-autostart'
import { api } from '../services/api'
import { useAppStore } from '../stores/app'
import { useSettingsStore } from '../stores/settings'
import { useSymbolsStore } from '../stores/symbols'
import { useGroupsStore } from '../stores/groups'
import { usePlatform } from '../utils/platform'
import type {
  AuthRecord,
  BackupStatus,
  ClientLocalSettings,
  DeviceItemDto,
  MetaDto,
  ServerSettingsDto,
  ServerSettingsUpdate,
  ServerStatusDto,
  SymbolRow,
} from '../types'

const appStore = useAppStore()
const settingsStore = useSettingsStore()
const message = useMessage()
const dialog = useDialog()
const inTauri = isTauri()
const router = useRouter()
const { isMobile } = usePlatform()

function goBack() {
  if (window.history.length > 1) {
    router.back()
  } else {
    void router.push({ name: 'dashboard' })
  }
}

/** 工具提示组件 */
const Tip = defineComponent({
  name: 'Tip',
  props: { text: { type: String, required: true } },
  setup(props) {
    return () =>
      h(
        NTooltip,
        { trigger: 'hover' },
        {
          trigger: () => h(NIcon, { component: Help, size: 15, class: 'help-icon' }),
          default: () =>
            h(
              'div',
              {
                style:
                  'max-width: 280px; white-space: normal; line-height: 1.5; word-break: break-word;',
              },
              props.text,
            ),
        },
      )
  },
})

// ── 1. 服务端连接状态与配对 ──
const authRecord = ref<AuthRecord>({
  server_url: 'http://127.0.0.1:8081',
  device_id: '',
  device_token: '',
  device_role: 'pc_admin',
  device_name: 'Desktop PC',
})
const serverMeta = ref<MetaDto | null>(null)
const serverStatus = ref<ServerStatusDto | null>(null)
const testingConnection = ref(false)
const savingAuth = ref(false)
const pairingKey = ref('')
const pairingDeviceName = ref('Desktop PC')
const pairingBusy = ref(false)
const showPairingKey = ref(false)

async function pasteServerUrl() {
  try {
    const text = await navigator.clipboard?.readText()
    if (text) {
      authRecord.value.server_url = text.trim()
      message.success('已粘贴服务器地址')
    } else {
      message.warning('剪贴板为空')
    }
  } catch {
    message.warning('无法自动读取剪贴板，请长按输入框直接粘贴')
  }
}

async function pastePairingKey() {
  try {
    const text = await navigator.clipboard?.readText()
    if (text) {
      pairingKey.value = text.trim()
      message.success('已从剪贴板粘贴密钥')
    } else {
      message.warning('剪贴板为空')
    }
  } catch {
    message.warning('无法自动读取剪贴板，请长按输入框直接粘贴')
  }
}

const isAdmin = computed(() => authRecord.value.device_role === 'pc_admin' || authRecord.value.device_role === 'admin')

async function loadAuth() {
  try {
    authRecord.value = await api.getAuthRecord()
    if (authRecord.value.device_name) {
      pairingDeviceName.value = authRecord.value.device_name
    } else if (isMobile.value) {
      pairingDeviceName.value = '手机端设备'
    }
  } catch (e) {
    // 忽略
  }
}

async function testConnection() {
  testingConnection.value = true
  try {
    // 自动同步当前输入的地址与参数
    await api.updateAuthRecord(authRecord.value)
    const [meta, status] = await Promise.all([api.getMeta(), api.getServerStatus()])
    serverMeta.value = meta
    serverStatus.value = status
    message.success(`连接成功！服务版本: v${meta.server_version}，行情延迟: ${status.quote_delay_ms ?? 0}ms`)
  } catch (e: any) {
    message.error(`连接失败: ${e?.message || e}`)
  } finally {
    testingConnection.value = false
  }
}

async function saveAuth() {
  savingAuth.value = true
  try {
    await api.updateAuthRecord(authRecord.value)
    message.success('连接信息已保存，正在重新建立连接...')
  } catch (e: any) {
    message.error(`保存失败: ${e?.message || e}`)
  } finally {
    savingAuth.value = false
  }
}

async function pairDevice() {
  if (!pairingKey.value.trim()) {
    message.warning('请输入管理配对密钥')
    return
  }
  pairingBusy.value = true
  try {
    // 自动保存当前输入的服务端地址，确保 Rust 后端即时使用最新地址发起配对
    await api.updateAuthRecord(authRecord.value)
    const res = await api.pairDevice({
      deviceName: pairingDeviceName.value.trim() || 'Desktop PC',
      adminKey: pairingKey.value.trim(),
    })
    authRecord.value.device_id = res.deviceId
    authRecord.value.device_token = res.token
    authRecord.value.device_role = res.role
    await api.updateAuthRecord(authRecord.value)
    pairingKey.value = ''
    message.success(`设备配对成功！分配角色: ${res.role}`)
    await testConnection()
    await loadSymbols()
    await useSymbolsStore().load().catch(() => {})
    await useGroupsStore().load().catch(() => {})
    try {
      await emit('symbols-updated')
    } catch {
      // 浏览器预览等非 Tauri 环境静默忽略
    }
    if (isAdmin.value) {
      await loadServerSettings()
    }
  } catch (e: any) {
    message.error(`配对失败: ${e?.message || e}`)
  } finally {
    pairingBusy.value = false
  }
}

// ── 2. 本地终端偏好 ──
const clientSettings = ref<ClientLocalSettings>({
  serverUrl: 'http://127.0.0.1:8081',
  deviceId: '',
  deviceName: 'Desktop PC',
  theme: 'dark',
  chartDisplayBars: 200,
  chartRightGap: 15,
  minBarSpacing: 6,
  timeframes: ['5m', '15m', '30m', '1h', '2h', '4h', '1d'],
  lastGroupId: null,
  desktopNotificationEnabled: true,
})
const autoLaunch = ref(false)
const autoLaunchBusy = ref(false)
const savingClientSettings = ref(false)

const availableTimeframes = [
  { label: '5分钟 (5m)', value: '5m' },
  { label: '15分钟 (15m)', value: '15m' },
  { label: '30分钟 (30m)', value: '30m' },
  { label: '1小时 (1h)', value: '1h' },
  { label: '2小时 (2h)', value: '2h' },
  { label: '4小时 (4h)', value: '4h' },
  { label: '日线 (1d)', value: '1d' },
]

async function loadClientSettings() {
  try {
    clientSettings.value = await api.getClientSettings()
  } catch (e) {
    // 忽略
  }
  if (inTauri) {
    try {
      autoLaunch.value = await isAutoLaunchEnabled()
    } catch {
      // 忽略
    }
  }
}

async function saveClientSettings() {
  savingClientSettings.value = true
  try {
    await api.updateClientSettings(clientSettings.value)
    message.success('终端偏好已保存')
  } catch (e: any) {
    message.error(`保存失败: ${e?.message || e}`)
  } finally {
    savingClientSettings.value = false
  }
}

async function toggleAutoLaunch(enabled: boolean) {
  if (!inTauri) return
  autoLaunchBusy.value = true
  try {
    if (enabled) await enableAutoLaunch()
    else await disableAutoLaunch()
    autoLaunch.value = enabled
    message.success(enabled ? '开机自启已开启' : '开机自启已关闭')
  } catch (e: any) {
    message.error(`切换开机自启失败: ${e?.message || e}`)
  } finally {
    autoLaunchBusy.value = false
  }
}

// ── 3. 服务端全局设置 ──
const serverSettings = ref<ServerSettingsDto | null>(null)
const serverSettingsUpdate = ref<ServerSettingsUpdate>({ configRevision: 0 })
const newTqPassword = ref('')
const newSmtpPassword = ref('')
const showTqPassword = ref(false)
const showSmtpPassword = ref(false)
const savingServerSettings = ref(false)

const logLevels = [
  { label: 'trace', value: 'trace' },
  { label: 'debug', value: 'debug' },
  { label: 'info', value: 'info' },
  { label: 'warn', value: 'warn' },
  { label: 'error', value: 'error' },
]

async function loadServerSettings() {
  try {
    const s = await api.getServerSettings()
    serverSettings.value = s
    serverSettingsUpdate.value = {
      configRevision: s.configRevision,
      appConfig: { ...s.appConfig },
      scheduler: { ...s.scheduler },
      fetch: { ...s.fetch },
      quote: { ...s.quote },
      notify: { ...s.notify },
      preclose: { ...s.preclose },
      log: { ...s.log },
      dataSource: { ...s.dataSource },
      email: { ...s.email },
    }
    newTqPassword.value = ''
    newSmtpPassword.value = ''
  } catch (e: any) {
    console.warn('获取服务端配置失败:', e)
  }
}

async function saveServerSettings() {
  if (!serverSettings.value) return
  savingServerSettings.value = true
  try {
    const req: ServerSettingsUpdate = {
      configRevision: serverSettings.value.configRevision,
      appConfig: serverSettingsUpdate.value.appConfig,
      scheduler: serverSettingsUpdate.value.scheduler,
      fetch: serverSettingsUpdate.value.fetch,
      quote: serverSettingsUpdate.value.quote,
      notify: serverSettingsUpdate.value.notify,
      preclose: serverSettingsUpdate.value.preclose,
      log: serverSettingsUpdate.value.log,
      dataSource: {
        ...serverSettingsUpdate.value.dataSource,
        tqPassword: newTqPassword.value ? newTqPassword.value : undefined,
      },
      email: {
        ...serverSettingsUpdate.value.email,
        smtpPassword: newSmtpPassword.value ? newSmtpPassword.value : undefined,
      },
    }
    const res = await api.updateServerSettings(req)
    message.success(`服务端设置已更新 (版本号: rev${res.newRevision})`)
    if (res.restartRequired && res.restartRequired.length > 0) {
      message.warning(`以下配置修改需要重启服务后生效: ${res.restartRequired.join(', ')}`)
    }
    await loadServerSettings()
  } catch (e: any) {
    message.error(`保存失败: ${e?.message || e}`)
  } finally {
    savingServerSettings.value = false
  }
}

// ── 4. 多端设备管理 ──
const deviceList = ref<DeviceItemDto[]>([])
const loadingDevices = ref(false)

async function loadDevices() {
  if (!isAdmin.value) return
  loadingDevices.value = true
  try {
    deviceList.value = await api.listDevices()
  } catch (e: any) {
    console.warn('加载设备列表失败:', e)
  } finally {
    loadingDevices.value = false
  }
}

async function revokeDevice(deviceId: string) {
  try {
    await api.revokeDevice(deviceId)
    message.success('已撤销该设备授权')
    await loadDevices()
  } catch (e: any) {
    message.error(`撤销失败: ${e?.message || e}`)
  }
}

async function changeDeviceRole(deviceId: string, currentRole: string) {
  const targetRole = currentRole.includes('admin') ? 'standard' : 'admin'
  try {
    await api.updateDeviceRole(deviceId, targetRole)
    message.success(`设备角色已更新为: ${targetRole === 'admin' ? '主管理员' : '标准终端'}`)
    await loadDevices()
  } catch (e: any) {
    message.error(`修改角色失败: ${e?.message || e}`)
  }
}

const deviceColumns: DataTableColumns<DeviceItemDto> = [
  { title: '设备名称', key: 'name' },
  { title: '设备ID', key: 'id', ellipsis: { tooltip: true } },
  {
    title: '角色',
    key: 'role',
    render(row) {
      return h(
        NTag,
        { size: 'small', type: row.role.includes('admin') ? 'primary' : 'default', round: true },
        () => (row.role.includes('admin') ? '主管理员' : '标准终端'),
      )
    },
  },
  { title: '配对时间', key: 'createdAt', ellipsis: { tooltip: true } },
  {
    title: '最后活跃',
    key: 'lastSeenAt',
    render(row) {
      return row.lastSeenAt || '—'
    },
  },
  {
    title: '操作',
    key: 'actions',
    render(row) {
      if (row.id === authRecord.value.device_id) {
        return h(NTag, { size: 'small', type: 'info' }, () => '当前设备 (本机)')
      }
      const isTargetAdmin = row.role.includes('admin')
      return h(NSpace, { size: 'small' }, () => [
        h(
          NButton,
          {
            size: 'tiny',
            quaternary: true,
            type: isTargetAdmin ? 'warning' : 'primary',
            onClick: () => changeDeviceRole(row.id, row.role),
          },
          () => (isTargetAdmin ? '降为标准终端' : '提权为主管'),
        ),
        h(
          NPopconfirm,
          {
            onPositiveClick: () => revokeDevice(row.id),
          },
          {
            trigger: () =>
              h(NButton, { size: 'tiny', type: 'error', quaternary: true }, () => '撤销授权'),
            default: () => '确定撤销该设备的访问授权吗？撤销后该终端将无法访问服务。',
          },
        ),
      ])
    },
  },
]

// ── 5. PC 数据库备份 ──
const backupStatus = ref<BackupStatus | null>(null)
const backingUp = ref(false)

async function loadBackupStatus() {
  try {
    backupStatus.value = await api.getBackupStatus()
  } catch (e) {
    // 忽略
  }
}

async function triggerBackup() {
  backingUp.value = true
  try {
    const filename = await api.triggerDatabaseBackup()
    message.success(`一致性数据库备份成功！文件: ${filename}`)
    await loadBackupStatus()
  } catch (e: any) {
    message.error(`备份失败: ${e?.message || e}`)
  } finally {
    backingUp.value = false
  }
}

async function openBackupDir() {
  try {
    await api.openBackupDirectory()
  } catch (e: any) {
    message.error(`打开目录失败: ${e?.message || e}`)
  }
}

// ── 6. 运维控制 ──
const restartingServer = ref(false)
const restartingBridge = ref(false)

async function handleRestartServer() {
  dialog.warning({
    title: '受控重启服务端',
    content: '确定向云服务端发送重启指令吗？服务端将保存状态并退出，由 systemd 自动拉起恢复。',
    positiveText: '确认重启',
    negativeText: '取消',
    onPositiveClick: async () => {
      restartingServer.value = true
      try {
        await api.restartServer()
        message.info('重启指令已发送，服务端将在几秒内恢复...')
      } catch (e: any) {
        message.error(`重启请求失败: ${e?.message || e}`)
      } finally {
        restartingServer.value = false
      }
    },
  })
}

async function handleRestartBridge() {
  restartingBridge.value = true
  try {
    await api.restartBridge()
    message.success('天勤 Python 桥接进程已重启并完成健康检查')
  } catch (e: any) {
    message.error(`重启桥接失败: ${e?.message || e}`)
  } finally {
    restartingBridge.value = false
  }
}

// ── 7. 品种与精度 ──
const symbolRows = ref<SymbolRow[]>([])
const symbolFilter = ref('')
const filteredSymbols = computed(() => {
  const kw = symbolFilter.value.trim().toUpperCase()
  if (!kw) return symbolRows.value
  return symbolRows.value.filter(
    (r) =>
      r.code.toUpperCase().includes(kw) ||
      r.name.toUpperCase().includes(kw) ||
      r.variety.toUpperCase().includes(kw),
  )
})

async function loadSymbols() {
  try {
    symbolRows.value = await api.getSymbols()
  } catch {
    // 忽略
  }
}

function onTickChange(row: SymbolRow, value: number | null) {
  const tick = value ?? 0
  row.tick_size = tick
  api.setSymbolTick(row.code, tick).catch((e) => message.error(String(e)))
}

function tickStep(tick: number): number {
  if (tick <= 0) return 1
  const text = String(tick)
  const dot = text.indexOf('.')
  if (dot < 0) return 1
  return Math.pow(10, -(text.length - dot - 1))
}

function showVariety(row: SymbolRow): boolean {
  const v = row.variety.trim()
  if (!v) return false
  return row.name !== v && !row.name.includes(v)
}

onMounted(async () => {
  await Promise.all([
    loadAuth(),
    loadClientSettings(),
    loadBackupStatus(),
    loadSymbols(),
  ])
  if (isAdmin.value) {
    loadServerSettings()
    loadDevices()
  }
})
</script>

<template>
  <div class="settings-page" :class="{ 'is-mobile-page': isMobile }">
    <!-- 移动端顶部便捷返回导航 -->
    <div v-if="isMobile" class="mobile-settings-header">
      <n-button quaternary circle size="small" class="m-back-btn" title="返回主界面" @click="goBack">
        <template #icon><n-icon :component="ArrowLeft" size="18" /></template>
      </n-button>
      <span class="m-header-title">系统设置</span>
      <div class="m-header-extra">
        <n-tag :type="isAdmin ? 'primary' : 'default'" size="tiny" round>
          {{ isAdmin ? '管理员' : '标准终端' }}
        </n-tag>
      </div>
    </div>

    <n-tabs
      type="line"
      :placement="isMobile ? 'top' : 'left'"
      class="setting-tabs"
      :class="{ 'is-mobile-tabs': isMobile }"
      default-value="connection"
      scrollable
    >
      <!-- 1. 服务端连接 -->
      <n-tab-pane name="connection">
        <template #tab>
          <div class="custom-tab-label">
            <span class="tab-icon"><n-icon :component="Cloud" /></span>
            <span>服务端连接</span>
          </div>
        </template>
        <div class="tab-pane-container">
          <n-scrollbar class="tab-scroll-area">
            <div class="tab-body-inner">
              <label class="section-title">服务地址与设备凭据</label>
              <div class="setting-card">
                <div class="setting-card-row">
                  <div class="row-label">
                    服务器 API 地址
                    <Tip text="权威服务端 API 地址，如 http://127.0.0.1:8081 或云服务器 HTTPS 域名" />
                  </div>
                  <n-input
                    v-model:value="authRecord.server_url"
                    placeholder="http://127.0.0.1:8081"
                    class="setting-input-wide"
                    clearable
                  >
                    <template #suffix>
                      <n-button quaternary size="tiny" type="primary" title="粘贴地址" @click="pasteServerUrl">
                        粘贴
                      </n-button>
                    </template>
                  </n-input>
                </div>
                <div class="setting-card-row">
                  <div class="row-label">
                    设备认证 Token
                    <Tip text="本设备的认证令牌，配对后由服务端下发，妥善保存在系统凭据管理器中。" />
                  </div>
                  <n-input
                    v-model:value="authRecord.device_token"
                    type="password"
                    show-password-on="click"
                    placeholder="Bearer Token"
                    class="setting-input-wide"
                  />
                </div>
                <div class="setting-card-row">
                  <div class="row-label">设备身份角色</div>
                  <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
                    <n-tag :type="isAdmin ? 'primary' : 'default'" size="small" round>
                      {{ isAdmin ? '主管理员 (PC Admin)' : '标准终端 (Standard)' }}
                    </n-tag>
                    <n-text depth="3" style="font-size: 12px">ID: {{ authRecord.device_id || '未配对' }}</n-text>
                  </div>
                </div>
                <div class="setting-card-row">
                  <div class="row-label">连接与测试</div>
                  <n-space>
                    <n-button size="small" :loading="testingConnection" @click="testConnection">
                      测试连通性
                    </n-button>
                    <n-button type="primary" size="small" :loading="savingAuth" @click="saveAuth">
                      保存配置
                    </n-button>
                  </n-space>
                </div>
              </div>

              <label class="section-title">管理密钥快速配对</label>
              <!-- 帮助提示说明卡片 -->
              <n-alert type="info" :show-icon="false" class="pairing-alert-tip">
                <div class="pairing-tip-content">
                  <div class="tip-line"><b>❓ 服务端配对 Admin Key 是什么？</b></div>
                  <div class="tip-line text-muted">
                    这是服务端（云端或本地）在 <code>secrets.json</code> 中配置的管理员主密钥（或通过 <code>NTREND_ADMIN_KEY</code> 设定），仅用于新终端首次接入时的配对鉴权。
                  </div>
                  <div class="tip-line" style="margin-top: 6px;"><b>📱 PC 端与手机端用同一个 Key 吗？</b></div>
                  <div class="tip-line text-muted">
                    <b>是的，完全用同一个！</b>只要连的是同一个后端服务，PC 端和手机端配对时填写的是<b>同一个服务端的 Admin Key</b>。点击“一键配对”后，服务端会自动为这台手机分发专属的 Token，后续通信无需再输入 Key。
                  </div>
                </div>
              </n-alert>

              <div class="setting-card">
                <div class="setting-card-row">
                  <div class="row-label">
                    设备名称
                    <Tip text="用于在服务端管理列表中标识本台设备，如 '我的手机' 或 '张三的办公电脑'。" />
                  </div>
                  <n-input
                    v-model:value="pairingDeviceName"
                    :placeholder="isMobile ? '手机端设备' : 'Desktop PC'"
                    class="setting-input-wide"
                  />
                </div>
                <div class="setting-card-row">
                  <div class="row-label">
                    服务端配对 Admin Key
                    <Tip text="云端 secrets.json 中的 admin_key，仅在初次配对授权时使用。" />
                  </div>
                  <n-input
                    v-model:value="pairingKey"
                    :type="showPairingKey ? 'text' : 'password'"
                    placeholder="输入服务端的 admin_key"
                    class="setting-input-wide"
                    clearable
                    @keyup.enter="pairDevice"
                  >
                    <template #suffix>
                      <n-space :size="4" align="center">
                        <n-button
                          quaternary
                          circle
                          size="tiny"
                          :title="showPairingKey ? '隐藏密钥' : '显示明文'"
                          @click="showPairingKey = !showPairingKey"
                        >
                          <template #icon>
                            <n-icon :component="showPairingKey ? EyeOff : Eye" size="14" />
                          </template>
                        </n-button>
                        <n-button
                          quaternary
                          size="tiny"
                          type="primary"
                          title="从剪贴板粘贴"
                          @click="pastePairingKey"
                        >
                          粘贴
                        </n-button>
                      </n-space>
                    </template>
                  </n-input>
                </div>
                <div class="setting-card-row">
                  <div class="row-label" />
                  <n-button type="primary" size="medium" :block="isMobile" :loading="pairingBusy" @click="pairDevice">
                    一键配对并获取令牌
                  </n-button>
                </div>
              </div>
            </div>
          </n-scrollbar>
        </div>
      </n-tab-pane>

      <!-- 2. 终端偏好 -->
      <n-tab-pane name="client_prefs">
        <template #tab>
          <div class="custom-tab-label">
            <span class="tab-icon"><n-icon :component="ChartCandle" /></span>
            <span>终端偏好</span>
          </div>
        </template>
        <div class="tab-pane-container">
          <n-scrollbar class="tab-scroll-area">
            <div class="tab-body-inner">
              <label class="section-title">系统与显示</label>
              <div class="setting-card">
                <div class="setting-card-row is-switch-row">
                  <div class="row-label">
                    开机自动启动
                    <Tip text="开机后自动启动客户端；可在 Windows 任务管理器中随时管理。" />
                  </div>
                  <n-switch :value="autoLaunch" :loading="autoLaunchBusy" @update:value="toggleAutoLaunch" />
                </div>
                <div class="setting-card-row">
                  <div class="row-label">
                    K线图默认显示根数
                    <Tip text="初次进入图表时可视区默认展示的K线数量。" />
                  </div>
                  <n-input-number v-model:value="clientSettings.chartDisplayBars" :min="50" :max="1000" class="setting-input-number" />
                </div>
                <div class="setting-card-row">
                  <div class="row-label">
                    右侧间隙（根）
                    <Tip text="图表最新K线距离右边界的留白距离。" />
                  </div>
                  <n-input-number v-model:value="clientSettings.chartRightGap" :min="2" :max="50" class="setting-input-number" />
                </div>
                <div class="setting-card-row">
                  <div class="row-label">
                    K线最小间距（像素）
                    <Tip text="图表横向缩放时防止K线过细的最小像素间距。" />
                  </div>
                  <n-input-number v-model:value="clientSettings.minBarSpacing" :min="2" :max="30" class="setting-input-number" />
                </div>
                <div class="setting-card-row">
                  <div class="row-label">
                    启用K线周期
                    <Tip text="选择需要在图表切换栏中显示的分析周期。" />
                  </div>
                  <n-select
                    v-model:value="clientSettings.timeframes"
                    multiple
                    :options="availableTimeframes"
                    class="setting-input-wide"
                  />
                </div>
              </div>
            </div>
          </n-scrollbar>
          <!-- 底部固定保存栏 -->
          <div class="tab-footer-bar" :class="{ 'is-mobile-footer': isMobile }">
            <div class="footer-tip">
              <n-text depth="3" style="font-size: 12px">偏好保存在本地客户端配置中</n-text>
            </div>
            <n-button type="primary" :size="isMobile ? 'small' : 'medium'" :block="isMobile" :loading="savingClientSettings" @click="saveClientSettings">
              <template #icon><n-icon :component="DeviceFloppy" /></template>
              保存终端偏好
            </n-button>
          </div>
        </div>
      </n-tab-pane>

      <!-- 3. 服务端设置 (管理员) -->
      <n-tab-pane name="server_settings" :disabled="!isAdmin">
        <template #tab>
          <div class="custom-tab-label">
            <span class="tab-icon"><n-icon :component="Server" /></span>
            <span>服务端设置</span>
          </div>
        </template>
        <div class="tab-pane-container" v-if="serverSettingsUpdate.scheduler">
          <!-- 中间滚动区域，使用 Native 级 NScrollbar，消除丑陋原生滚动条 -->
          <n-scrollbar class="tab-scroll-area">
            <div class="tab-body-inner">
              <label class="section-title">定时调度与形态</label>
              <div class="setting-card">
                <div class="setting-card-row is-switch-row">
                  <div class="row-label">启动时自动运行定时任务</div>
                  <n-switch v-model:value="serverSettingsUpdate.appConfig!.auto_start_scheduler" />
                </div>
                <div class="setting-card-row">
                  <div class="row-label">信号分析版本</div>
                  <n-radio-group v-model:value="serverSettingsUpdate.appConfig!.logic_version" :size="isMobile ? 'small' : 'medium'">
                    <n-radio-button value="1">1.x 原版</n-radio-button>
                    <n-radio-button value="2">2.0 严格N字+箱体</n-radio-button>
                  </n-radio-group>
                </div>
                <div class="setting-card-row">
                  <div class="row-label">数据刷新间隔（秒）</div>
                  <n-input-number v-model:value="serverSettingsUpdate.scheduler!.refresh_interval_secs" :min="10" :max="600" class="setting-input-number" />
                </div>
                <div class="setting-card-row">
                  <div class="row-label">形态扫描间隔（秒）</div>
                  <n-input-number v-model:value="serverSettingsUpdate.scheduler!.scan_interval_secs" :min="10" :max="600" class="setting-input-number" />
                </div>
                <div class="setting-card-row is-switch-row">
                  <div class="row-label">仅在期货交易时段运行</div>
                  <n-switch v-model:value="serverSettingsUpdate.scheduler!.trading_only" />
                </div>
              </div>

              <label class="section-title">天勤数据源与桥接</label>
              <div class="setting-card">
                <div class="setting-card-row">
                  <div class="row-label">主力数据源</div>
                  <n-radio-group v-model:value="serverSettingsUpdate.dataSource!.primary_source" :size="isMobile ? 'small' : 'medium'">
                    <n-radio-button value="tqsdk">天勤 (tqsdk)</n-radio-button>
                    <n-radio-button value="sina">新浪 (sina)</n-radio-button>
                  </n-radio-group>
                </div>
                <div class="setting-card-row">
                  <div class="row-label">快期账号</div>
                  <n-input v-model:value="serverSettingsUpdate.dataSource!.tq_account" placeholder="快期账号" class="setting-input-wide" />
                </div>
                <div class="setting-card-row">
                  <div class="row-label">
                    快期密码
                    <Tip text="密码在服务端以 0600 权限单独存储于 secrets.json，从不返回客户端。" />
                  </div>
                  <div class="password-row-wrap">
                    <n-input
                      v-model:value="newTqPassword"
                      :type="showTqPassword ? 'text' : 'password'"
                      placeholder="留空保持不变"
                      class="setting-input-wide"
                    >
                      <template #suffix>
                        <n-button quaternary circle size="tiny" @click="showTqPassword = !showTqPassword">
                          <template #icon><n-icon :component="showTqPassword ? EyeOff : Eye" size="14" /></template>
                        </n-button>
                      </template>
                    </n-input>
                    <n-tag size="small" type="success" v-if="serverSettings?.dataSource?.tqPasswordConfigured">已配置</n-tag>
                    <n-tag size="small" type="warning" v-else>未配置</n-tag>
                  </div>
                </div>
                <div class="setting-card-row">
                  <div class="row-label">Python 桥接端口</div>
                  <n-input-number v-model:value="serverSettingsUpdate.dataSource!.bridge_port" class="setting-input-number" />
                </div>
              </div>

              <label class="section-title">邮件报警 (SMTP)</label>
              <div class="setting-card">
                <div class="setting-card-row is-switch-row">
                  <div class="row-label">启用邮件通知</div>
                  <n-switch v-model:value="serverSettingsUpdate.email!.enabled" />
                </div>
                <div class="setting-card-row">
                  <div class="row-label">SMTP 服务器地址</div>
                  <n-input v-model:value="serverSettingsUpdate.email!.smtp_host" placeholder="smtp.qq.com" class="setting-input-wide" />
                </div>
                <div class="setting-card-row">
                  <div class="row-label">SMTP 端口</div>
                  <n-input-number v-model:value="serverSettingsUpdate.email!.smtp_port" class="setting-input-number" />
                </div>
                <div class="setting-card-row">
                  <div class="row-label">发件人账号</div>
                  <n-input v-model:value="serverSettingsUpdate.email!.smtp_user" placeholder="user@example.com" class="setting-input-wide" />
                </div>
                <div class="setting-card-row">
                  <div class="row-label">授权码 / 密码</div>
                  <div class="password-row-wrap">
                    <n-input
                      v-model:value="newSmtpPassword"
                      :type="showSmtpPassword ? 'text' : 'password'"
                      placeholder="留空保持不变"
                      class="setting-input-wide"
                    >
                      <template #suffix>
                        <n-button quaternary circle size="tiny" @click="showSmtpPassword = !showSmtpPassword">
                          <template #icon><n-icon :component="showSmtpPassword ? EyeOff : Eye" size="14" /></template>
                        </n-button>
                      </template>
                    </n-input>
                    <n-tag size="small" type="success" v-if="serverSettings?.email?.smtpPasswordConfigured">已配置</n-tag>
                    <n-tag size="small" type="warning" v-else>未配置</n-tag>
                  </div>
                </div>
                <div class="setting-card-row">
                  <div class="row-label">接收邮箱</div>
                  <n-input v-model:value="serverSettingsUpdate.email!.to" placeholder="recipient@example.com" class="setting-input-wide" />
                </div>
              </div>
            </div>
          </n-scrollbar>

          <!-- 底部固定保存栏：固定在底部，不随内容滚动 -->
          <div class="tab-footer-bar" :class="{ 'is-mobile-footer': isMobile }">
            <div class="footer-tip">
              <n-text depth="3" style="font-size: 12px">
                配置版本: rev{{ serverSettings?.configRevision ?? 0 }}
              </n-text>
            </div>
            <n-button
              type="primary"
              :size="isMobile ? 'small' : 'medium'"
              :block="isMobile"
              :loading="savingServerSettings"
              @click="saveServerSettings"
            >
              <template #icon>
                <n-icon :component="DeviceFloppy" />
              </template>
              保存服务端设置
            </n-button>
          </div>
        </div>

        <div v-else class="tab-empty-tip">
          <n-spin size="medium" description="正在加载服务端设置..." />
        </div>
      </n-tab-pane>

      <!-- 4. 多端设备管理 (管理员) -->
      <n-tab-pane name="device_mgr" :disabled="!isAdmin">
        <template #tab>
          <div class="custom-tab-label">
            <span class="tab-icon"><n-icon :component="Devices" /></span>
            <span>设备管理</span>
          </div>
        </template>
        <div class="tab-pane-container">
          <n-scrollbar class="tab-scroll-area">
            <div class="tab-body-inner">
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px">
                <label class="section-title" style="margin: 0">已配对终端设备</label>
                <n-button size="small" :loading="loadingDevices" @click="loadDevices">
                  <template #icon><n-icon :component="Refresh" /></template>
                  刷新设备
                </n-button>
              </div>
              <n-data-table
                :columns="deviceColumns"
                :data="deviceList"
                :loading="loadingDevices"
                size="small"
                :scroll-x="isMobile ? 560 : undefined"
                :pagination="{ pageSize: 10 }"
              />
            </div>
          </n-scrollbar>
        </div>
      </n-tab-pane>

      <!-- 5. 数据灾备与备份 (PC) -->
      <n-tab-pane name="backup">
        <template #tab>
          <div class="custom-tab-label">
            <span class="tab-icon"><n-icon :component="Database" /></span>
            <span>数据库备份</span>
          </div>
        </template>
        <div class="tab-pane-container">
          <n-scrollbar class="tab-scroll-area">
            <div class="tab-body-inner">
              <label class="section-title">PC 每日一致性备份</label>
              <div class="setting-card">
                <div class="setting-card-row">
                  <div class="row-label">备份目录位置</div>
                  <n-text depth="2" style="font-family: monospace; font-size: 13px; word-break: break-all;">
                    {{ backupStatus?.backup_dir || '加载中...' }}
                  </n-text>
                </div>
                <div class="setting-card-row">
                  <div class="row-label">今日备份状态</div>
                  <n-space align="center">
                    <n-tag :type="backupStatus?.last_backup_date ? 'success' : 'warning'" size="small" round>
                      {{ backupStatus?.last_backup_date ? `已备份 (${backupStatus.last_backup_date})` : '今日未备份' }}
                    </n-tag>
                    <n-text depth="3" style="font-size: 12px">保留最近 3 份每日快照 (已存 {{ backupStatus?.backup_count ?? 0 }} 份)</n-text>
                  </n-space>
                </div>
                <div class="setting-card-row">
                  <div class="row-label">备份操作</div>
                  <n-space>
                    <n-button type="primary" size="small" :loading="backingUp" @click="triggerBackup">
                      <template #icon><n-icon :component="Database" /></template>
                      立即执行快照备份
                    </n-button>
                    <n-button size="small" @click="openBackupDir">
                      <template #icon><n-icon :component="Folder" /></template>
                      打开备份目录
                    </n-button>
                  </n-space>
                </div>
              </div>
            </div>
          </n-scrollbar>
        </div>
      </n-tab-pane>

      <!-- 6. 服务运维控制 (管理员) -->
      <n-tab-pane name="ops" :disabled="!isAdmin">
        <template #tab>
          <div class="custom-tab-label">
            <span class="tab-icon"><n-icon :component="Shield" /></span>
            <span>服务运维</span>
          </div>
        </template>
        <div class="tab-pane-container">
          <n-scrollbar class="tab-scroll-area">
            <div class="tab-body-inner">
              <label class="section-title">受控服务重启</label>
              <div class="setting-card">
                <div class="setting-card-row">
                  <div class="row-label">
                    重启 ntrend 服务端
                    <Tip text="云端服务将完成当前正在进行的批处理后受控退出，由 systemd 守护进程安全拉起。" />
                  </div>
                  <n-button type="warning" size="small" :block="isMobile" :loading="restartingServer" @click="handleRestartServer">
                    重启服务端主进程
                  </n-button>
                </div>
                <div class="setting-card-row">
                  <div class="row-label">
                    单独重启天勤 Python 桥接
                    <Tip text="当行情连接卡死或需要重置天勤 Python 进程时使用。" />
                  </div>
                  <n-button size="small" :block="isMobile" :loading="restartingBridge" @click="handleRestartBridge">
                    重启天勤 Bridge
                  </n-button>
                </div>
              </div>
            </div>
          </n-scrollbar>
        </div>
      </n-tab-pane>

      <!-- 7. 品种精度设置 -->
      <n-tab-pane name="symbols">
        <template #tab>
          <div class="custom-tab-label">
            <span class="tab-icon"><n-icon :component="Ruler" /></span>
            <span>品种精度</span>
          </div>
        </template>
        <div class="tab-pane-container">
          <div class="symbol-table-header">
            <n-input
              v-model:value="symbolFilter"
              placeholder="搜索品种代码或名称..."
              clearable
              size="small"
              class="symbol-filter-input"
            />
            <span class="symbol-count">共 {{ filteredSymbols.length }} 个品种</span>
          </div>
          <n-scrollbar class="symbol-scroll-area">
            <div class="symbol-list">
              <div v-for="row in filteredSymbols" :key="row.code" class="symbol-item">
                <div class="symbol-info">
                  <span class="sym-code">{{ row.code }}</span>
                  <span class="sym-name">{{ row.name }}</span>
                  <span v-if="showVariety(row)" class="sym-variety">{{ row.variety }}</span>
                </div>
                <div class="symbol-tick-input">
                  <span class="tick-label">最小跳动</span>
                  <n-input-number
                    :value="row.tick_size"
                    :min="0"
                    :step="tickStep(row.tick_size)"
                    size="small"
                    style="width: 110px"
                    @update:value="(val) => onTickChange(row, val)"
                  />
                </div>
              </div>
            </div>
          </n-scrollbar>
        </div>
      </n-tab-pane>
    </n-tabs>
  </div>
</template>

<style scoped>
.settings-page {
  height: 100%;
  max-height: 100%;
  box-sizing: border-box;
  background: var(--n-color);
  padding: 12px 16px 12px 20px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.is-mobile-page {
  padding: 0 !important;
}

.mobile-settings-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--n-border-color);
  background: var(--n-card-color);
  flex-shrink: 0;
  z-index: 10;
}

.m-back-btn {
  color: var(--n-text-color);
}

.m-header-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--n-text-color);
  flex: 1;
}

.m-header-extra {
  flex-shrink: 0;
}

.pairing-alert-tip {
  margin-bottom: 14px;
  border-radius: 8px;
}

.pairing-tip-content {
  font-size: 12px;
  line-height: 1.6;
}

.tip-line code {
  background: rgba(0, 0, 0, 0.06);
  padding: 1px 4px;
  border-radius: 4px;
  font-size: 11px;
}

.setting-tabs {
  height: 100%;
  flex: 1;
  min-height: 0;
}

.setting-tabs :deep(.n-tabs-pane-wrapper) {
  height: 100%;
  overflow: hidden;
}

.setting-tabs :deep(.n-tab-pane) {
  height: 100%;
  box-sizing: border-box;
  padding: 0 !important;
  overflow: hidden;
}

.custom-tab-label {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  padding: 4px 0;
  white-space: nowrap;
}

.tab-icon {
  display: flex;
  align-items: center;
  font-size: 16px;
}

.tab-pane-container {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
  box-sizing: border-box;
}

.tab-scroll-area {
  flex: 1;
  min-height: 0;
  height: 100%;
}

.tab-body-inner {
  padding: 12px 28px 28px 28px;
  max-width: 1080px;
  width: 100%;
  box-sizing: border-box;
}

.tab-footer-bar {
  flex-shrink: 0;
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 28px;
  max-width: 1080px;
  width: 100%;
  box-sizing: border-box;
  background: var(--n-color);
  border-top: 1px solid var(--n-border-color);
  z-index: 10;
}

.footer-tip {
  display: flex;
  align-items: center;
  gap: 8px;
}

.tab-empty-tip {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  min-height: 240px;
}

.section-title {
  display: block;
  font-size: 14px;
  font-weight: 600;
  margin: 16px 0 10px 0;
  color: var(--n-text-color);
}

.section-title:first-child {
  margin-top: 0;
}

.setting-card {
  border: 1px solid var(--n-border-color);
  border-radius: 8px;
  background: var(--n-card-color);
  padding: 4px 20px;
  margin-bottom: 16px;
  transition: border-color 0.2s;
}

.setting-card-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 20px;
  padding: 12px 0;
  border-bottom: 1px solid var(--n-border-color);
}

.setting-card-row:last-child {
  border-bottom: none;
}

.password-row-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
  max-width: 480px;
  width: 100%;
}

.password-row-wrap .setting-input-wide {
  flex: 1;
}

.setting-input-wide {
  width: 100% !important;
  max-width: 480px;
  min-width: 240px;
}

.setting-input-number {
  width: 100% !important;
  max-width: 200px;
  min-width: 120px;
}

.row-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--n-text-color);
  flex-shrink: 0;
}

.help-icon {
  color: var(--n-text-color-3);
  cursor: pointer;
}

.symbol-table-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 28px 12px 28px;
  max-width: 1080px;
  width: 100%;
  box-sizing: border-box;
  flex-shrink: 0;
}

.symbol-filter-input {
  width: 280px;
  max-width: 100%;
}

.symbol-count {
  font-size: 12px;
  color: var(--n-text-color-3);
}

.symbol-scroll-area {
  flex: 1;
  min-height: 0;
  padding: 0 28px 28px 28px;
  max-width: 1080px;
  width: 100%;
  box-sizing: border-box;
}

.symbol-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 10px;
}

.symbol-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 14px;
  border: 1px solid var(--n-border-color);
  border-radius: 6px;
  background: var(--n-card-color);
  transition: border-color 0.2s;
}

.symbol-item:hover {
  border-color: var(--n-primary-color);
}

.symbol-info {
  display: flex;
  align-items: center;
  gap: 10px;
}

.sym-code {
  font-weight: 600;
  font-family: monospace;
  font-size: 13px;
}

.sym-name {
  font-size: 13px;
}

.sym-variety {
  font-size: 12px;
  color: var(--n-text-color-3);
}

.symbol-tick-input {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.tick-label {
  font-size: 12px;
  color: var(--n-text-color-3);
}

/* 移动端专属类样式（适配仿真与原生） */
.is-mobile-page .setting-tabs :deep(.n-tabs-nav) {
  padding: 0 6px;
  background: var(--n-color);
  border-bottom: 1px solid var(--n-border-color);
}

.is-mobile-page .setting-tabs :deep(.n-tabs-wrapper) {
  overflow-x: auto;
}

.is-mobile-page .custom-tab-label {
  gap: 4px;
  font-size: 12px;
  padding: 4px 2px;
}

.is-mobile-page .tab-body-inner {
  padding: 8px 12px 28px 12px;
}

.is-mobile-page .setting-card {
  padding: 4px 12px;
  margin-bottom: 12px;
}

.is-mobile-page .setting-card-row {
  flex-direction: column;
  align-items: stretch;
  gap: 8px;
  padding: 10px 0;
}

.is-mobile-page .setting-card-row.is-switch-row {
  flex-direction: row;
  justify-content: space-between;
  align-items: center;
}

.is-mobile-page .row-label {
  font-size: 13px;
  width: 100%;
  justify-content: flex-start;
}

.is-mobile-page .setting-input-wide,
.is-mobile-page .setting-input-number {
  width: 100% !important;
  max-width: 100% !important;
  min-width: 0 !important;
}

.is-mobile-page .tab-footer-bar {
  padding: 10px 12px;
  flex-direction: column;
  gap: 8px;
  align-items: stretch;
}

.is-mobile-page .tab-footer-bar .footer-tip {
  justify-content: center;
}

.is-mobile-page .password-row-wrap {
  max-width: 100%;
}

.is-mobile-page .symbol-table-header {
  padding: 8px 12px;
  flex-direction: column;
  align-items: stretch;
  gap: 8px;
}

.is-mobile-page .symbol-filter-input {
  width: 100% !important;
  max-width: 100% !important;
}

.is-mobile-page .symbol-scroll-area {
  padding: 0 12px 20px 12px;
}

.is-mobile-page .symbol-list {
  grid-template-columns: 1fr;
  gap: 8px;
}

.is-mobile-page .symbol-item {
  padding: 8px 12px;
}

/* 屏幕媒体查询（保证任何<=768px宽度自动生效） */
@media (max-width: 768px) {
  .settings-page {
    padding: 0 !important;
  }
  .setting-tabs :deep(.n-tabs-nav) {
    padding: 0 6px;
    background: var(--n-color);
    border-bottom: 1px solid var(--n-border-color);
  }
  .setting-tabs :deep(.n-tabs-wrapper) {
    overflow-x: auto;
  }
  .custom-tab-label {
    gap: 4px;
    font-size: 12px;
    padding: 4px 2px;
  }
  .tab-body-inner {
    padding: 8px 12px 28px 12px;
  }
  .setting-card {
    padding: 4px 12px;
    margin-bottom: 12px;
  }
  .setting-card-row {
    flex-direction: column;
    align-items: stretch;
    gap: 8px;
    padding: 10px 0;
  }
  .setting-card-row.is-switch-row {
    flex-direction: row;
    justify-content: space-between;
    align-items: center;
  }
  .row-label {
    font-size: 13px;
    width: 100%;
    justify-content: flex-start;
  }
  .setting-input-wide,
  .setting-input-number {
    width: 100% !important;
    max-width: 100% !important;
    min-width: 0 !important;
  }
  .tab-footer-bar {
    padding: 10px 12px;
    flex-direction: column;
    gap: 8px;
    align-items: stretch;
  }
  .tab-footer-bar .footer-tip {
    justify-content: center;
  }
  .password-row-wrap {
    max-width: 100%;
  }
  .symbol-table-header {
    padding: 8px 12px;
    flex-direction: column;
    align-items: stretch;
    gap: 8px;
  }
  .symbol-filter-input {
    width: 100% !important;
    max-width: 100% !important;
  }
  .symbol-scroll-area {
    padding: 0 12px 20px 12px;
  }
  .symbol-list {
    grid-template-columns: 1fr;
    gap: 8px;
  }
  .symbol-item {
    padding: 8px 12px;
  }
}
</style>

