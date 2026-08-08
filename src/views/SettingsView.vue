<script setup lang="ts">
import { computed, defineComponent, h, onMounted, ref } from 'vue'
import {
  NButton,
  NIcon,
  NInput,
  NInputNumber,
  NSelect,
  NSpace,
  NSwitch,
  NTabPane,
  NTabs,
  NText,
  NTooltip,
  useDialog,
  useMessage,
} from 'naive-ui'
import {
  Bell,
  Database,
  DeviceFloppy,
  Help,
  RotateClockwise,
  Ruler,
  Settings as SettingsIcon,
} from '@vicons/tabler'
import { isTauri } from '@tauri-apps/api/core'
import {
  disable as disableAutoLaunch,
  enable as enableAutoLaunch,
  isEnabled as isAutoLaunchEnabled,
} from '@tauri-apps/plugin-autostart'
import { api } from '../services/api'
import { useSettingsStore } from '../stores/settings'
import type { Config, SymbolRow } from '../types'

const settingsStore = useSettingsStore()
const message = useMessage()
const dialog = useDialog()

/** 工具提示：参考模板的“?”小图标，悬停展示说明文字。 */
const Tip = defineComponent({
  name: 'Tip',
  props: {
    text: { type: String, required: true },
  },
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

const form = ref<Config>(cloneConfig(settingsStore.settings))
const saving = ref(false)
const symbolRows = ref<SymbolRow[]>([])
const symbolFilter = ref('')
/** 浏览器预览（纯前端 npm run dev）下不调用任何 Tauri 插件 API */
const inTauri = isTauri()
/** 开机自启状态（由操作系统注册项读取，不写入 config.json） */
const autoLaunch = ref(false)
const autoLaunchBusy = ref(false)
/** 表单与当前已保存配置是否不同（有改动才允许保存） */
const dirty = computed(
  () => JSON.stringify(form.value) !== JSON.stringify(settingsStore.settings),
)
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
    // 浏览器预览环境下无后端命令，忽略
  }
}

function onTickChange(row: SymbolRow, value: number | null) {
  const tick = value ?? 0
  row.tick_size = tick
  api.setSymbolTick(row.code, tick).catch((e) => message.error(String(e)))
}

/** 按小数位精度取步进（0.02→0.01，1/50→1，0.005→0.001），避免步进随当前值漂移 */
function tickStep(tick: number): number {
  if (tick <= 0) return 1
  const text = String(tick)
  const dot = text.indexOf('.')
  if (dot < 0) return 1
  return Math.pow(10, -(text.length - dot - 1))
}

/** 品种列只在名称里没体现品种名时才显示，避免“甲醇连续 + 甲醇”重复 */
function showVariety(row: SymbolRow): boolean {
  const v = row.variety.trim()
  if (!v) return false
  return row.name !== v && !row.name.includes(v)
}

const logLevels = [
  { label: 'trace', value: 'trace' },
  { label: 'debug', value: 'debug' },
  { label: 'info', value: 'info' },
  { label: 'warn', value: 'warn' },
  { label: 'error', value: 'error' },
]

function cloneConfig(config: Config): Config {
  return JSON.parse(JSON.stringify(config))
}

async function save() {
  saving.value = true
  try {
    await settingsStore.save(form.value)
    message.success('设置已保存')
  } catch (e) {
    message.error(String(e))
  } finally {
    saving.value = false
  }
}

/** 恢复默认设置：先弹窗确认，确认后重置并覆盖当前表单 */
function confirmReset() {
  dialog.warning({
    title: '恢复默认设置',
    content: '确定将所有设置恢复为默认值吗？当前配置将被覆盖，且无法撤销。',
    positiveText: '恢复默认',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        settingsStore.settings = await api.resetConfig()
        form.value = cloneConfig(settingsStore.settings)
        message.success('已恢复默认设置')
      } catch (e) {
        message.error(String(e))
      }
    },
  })
}

async function toggleRunning() {
  try {
    await settingsStore.setRunning(!settingsStore.status.running)
  } catch (e) {
    message.error(String(e))
  }
}

async function openLogDirectory() {
  try {
    await api.openLogDirectory()
  } catch (e) {
    message.error(String(e))
  }
}

/** 读取系统当前的开机自启状态，用于同步开关 */
async function syncAutoLaunch() {
  if (!inTauri) return
  try {
    autoLaunch.value = await isAutoLaunchEnabled()
  } catch (e) {
    console.warn('读取开机自启状态失败', e)
  }
}

/** 切换开机自启：写入系统注册项；失败时回滚开关状态 */
async function toggleAutoLaunch(value: boolean) {
  if (!inTauri || autoLaunchBusy.value) return
  autoLaunchBusy.value = true
  try {
    if (value) await enableAutoLaunch()
    else await disableAutoLaunch()
    autoLaunch.value = value
    message.success(value ? '已开启开机自启' : '已关闭开机自启')
  } catch (e) {
    autoLaunch.value = !value
    message.error(String(e))
  } finally {
    autoLaunchBusy.value = false
  }
}

onMounted(async () => {
  try {
    await settingsStore.load()
  } catch {
    // 浏览器预览环境下无后端命令，保持默认值
  }
  await loadSymbols()
  form.value = cloneConfig(settingsStore.settings)
  await syncAutoLaunch()
})
</script>

<template>
  <div class="settings-page">
    <n-tabs type="line" placement="left" class="setting-tabs" default-value="app">
      <n-tab-pane name="app">
        <template #tab>
          <div class="custom-tab-label">
            <span class="tab-icon"><n-icon :component="SettingsIcon" /></span>
            <span>应用</span>
          </div>
        </template>
        <div class="tab-body">
          <label class="section-title">应用</label>
          <div class="setting-card">
            <div class="setting-card-row">
              <div class="row-label">
                开机自动启动
                <Tip text="登录 Windows 后自动启动本应用；可在设置界面或系统任务管理器中随时关闭。" />
              </div>
              <n-switch
                :value="autoLaunch"
                :loading="autoLaunchBusy"
                @update:value="toggleAutoLaunch"
              />
            </div>
            <div class="setting-card-row">
              <div class="row-label">
                启动时自动运行定时任务
                <Tip text="应用启动后自动开始定时刷新与扫描，无需手动点击启动。" />
              </div>
              <n-switch v-model:value="form.app_config.auto_start_scheduler" />
            </div>
          </div>

          <label class="section-title">日志</label>
          <div class="setting-card">
            <div class="setting-card-row">
              <div class="row-label">
                日志级别
                <Tip text="修改后重启应用生效；RUST_LOG 环境变量仍可整体覆盖。" />
              </div>
              <n-select v-model:value="form.log.level" :options="logLevels" style="width: 200px" />
            </div>
            <div class="setting-card-row">
              <div class="row-label">
                日志存储位置
                <Tip text="点击查看应用日志文件所在目录。" />
              </div>
              <n-button size="small" @click="openLogDirectory">打开日志目录</n-button>
            </div>
          </div>
        </div>
      </n-tab-pane>

      <n-tab-pane name="notify">
        <template #tab>
          <div class="custom-tab-label">
            <span class="tab-icon"><n-icon :component="Bell" /></span>
            <span>通知</span>
          </div>
        </template>
        <div class="tab-body">
          <label class="section-title">通知</label>
          <div class="setting-card">
            <div class="setting-card-row">
              <div class="row-label">
                局内新形态通知
                <Tip text="扫描发现新的“即将触发”形态时，在应用内右下角弹出信号卡片通知。" />
              </div>
              <n-switch v-model:value="form.notify.in_app_new_pattern" />
            </div>
            <div class="setting-card-row">
              <div class="row-label">
                局内触发价通知
                <Tip text="实时行情轮询发现最新价已触及形态入场价时弹出通知（做空为跌破入场价，做多为突破入场价）；通知不自动消失，需手动关闭。" />
              </div>
              <n-switch v-model:value="form.notify.in_app_entry_trigger" />
            </div>
            <div class="setting-card-row">
              <div class="row-label">
                系统触发价通知
                <Tip text="入场价提醒同时发送系统级通知，需要操作系统通知权限。" />
              </div>
              <n-switch v-model:value="form.notify.system_entry_trigger" />
            </div>
          </div>

          <label class="section-title">界面</label>
          <div class="setting-card">
            <div class="setting-card-row">
              <div class="row-label">
                行情跳动闪烁时长（毫秒）
                <Tip text="自选表格中价格变化时行背景闪烁的时长。" />
              </div>
              <n-input-number
                v-model:value="form.ui.flash_ms"
                :min="100"
                :max="3000"
                style="width: 200px"
              />
            </div>
            <div class="setting-card-row">
              <div class="row-label">
                顶栏呼吸灯保持时长（毫秒）
                <Tip text="收到行情请求事件后，顶栏“运行中”呼吸灯保持亮起的时间。" />
              </div>
              <n-input-number
                v-model:value="form.ui.breathe_hold_ms"
                :min="1000"
                :max="30000"
                style="width: 200px"
              />
            </div>
            <div class="setting-card-row">
              <div class="row-label">
                K线最小间距（像素）
                <Tip text="K线图横向拉宽时防止K线细成一条线的最小间距。" />
              </div>
              <n-input-number
                v-model:value="form.ui.min_bar_spacing"
                :min="2"
                :max="30"
                style="width: 200px"
              />
            </div>
          </div>

          <label class="section-title">邮件通知</label>
          <div class="setting-card">
            <div class="setting-card-row">
              <div class="row-label">启用邮件</div>
              <n-switch v-model:value="form.email.enabled" />
            </div>
            <div class="setting-card-row">
              <div class="row-label">收件人（逗号分隔）</div>
              <n-input
                v-model:value="form.email.to"
                placeholder="a@example.com,b@example.com"
                style="width: 320px"
              />
            </div>
            <div class="setting-card-row">
              <div class="row-label">发件人</div>
              <n-input v-model:value="form.email.from" placeholder="your@qq.com" style="width: 320px" />
            </div>
            <div class="setting-card-row">
              <div class="row-label">SMTP 主机</div>
              <n-input v-model:value="form.email.smtp_host" placeholder="smtp.qq.com" style="width: 320px" />
            </div>
            <div class="setting-card-row">
              <div class="row-label">SMTP 端口</div>
              <n-input-number
                v-model:value="form.email.smtp_port"
                :min="1"
                :max="65535"
                style="width: 200px"
              />
            </div>
            <div class="setting-card-row">
              <div class="row-label">SMTP 账号</div>
              <n-input v-model:value="form.email.smtp_user" placeholder="your@qq.com" style="width: 320px" />
            </div>
            <div class="setting-card-row">
              <div class="row-label">
                SMTP 授权码
                <Tip text="QQ邮箱等需使用授权码而非登录密码；保存后明文写入配置文件，与之前存数据库行为一致。" />
              </div>
              <n-input
                v-model:value="form.email.smtp_password"
                type="password"
                show-password-on="click"
                placeholder="QQ邮箱授权码"
                style="width: 320px"
              />
            </div>
          </div>
        </div>
      </n-tab-pane>

      <n-tab-pane name="data">
        <template #tab>
          <div class="custom-tab-label">
            <span class="tab-icon"><n-icon :component="Database" /></span>
            <span>数据</span>
          </div>
        </template>
        <div class="tab-body">
          <label class="section-title">定时任务</label>
          <div class="setting-card">
            <div class="setting-card-row">
              <div class="row-label">
                数据刷新间隔（秒）
                <Tip text="定时增量刷新5分钟K线的间隔，按分钟网格对齐，保存后立即生效。" />
              </div>
              <n-input-number
                v-model:value="form.scheduler.refresh_interval_secs"
                :min="60"
                :max="3600"
                style="width: 200px"
              />
            </div>
            <div class="setting-card-row">
              <div class="row-label">
                扫描间隔（秒）
                <Tip text="定时运行N形态扫描的间隔；扫描结果会持久化并推送通知。" />
              </div>
              <n-input-number
                v-model:value="form.scheduler.scan_interval_secs"
                :min="300"
                :max="7200"
                style="width: 200px"
              />
            </div>
            <div class="setting-card-row">
              <div class="row-label">
                仅交易时段运行
                <Tip text="仅在国内期货日盘/夜盘窗口内触发刷新与扫描，避免无效请求。" />
              </div>
              <n-switch v-model:value="form.scheduler.trading_only" />
            </div>
            <div class="setting-card-row">
              <div class="row-label">定时任务当前状态</div>
              <n-space align="center" :size="12">
                <n-text :type="settingsStore.status.running ? 'success' : 'warning'">
                  {{ settingsStore.status.running ? '运行中' : '已暂停' }}
                </n-text>
                <n-button
                  size="small"
                  :type="settingsStore.status.running ? 'warning' : 'success'"
                  @click="toggleRunning"
                >
                  {{ settingsStore.status.running ? '暂停' : '启动' }}
                </n-button>
              </n-space>
            </div>
          </div>

          <label class="section-title">数据抓取</label>
          <div class="setting-card">
            <div class="setting-card-row">
              <div class="row-label">
                单请求间隔（毫秒）
                <Tip text="K线抓取的相邻请求最小间隔，越小越快但越容易被接口限流。" />
              </div>
              <n-input-number
                v-model:value="form.fetch.request_interval_ms"
                :min="100"
                :max="10000"
                style="width: 200px"
              />
            </div>
            <div class="setting-card-row">
              <div class="row-label">
                每分钟请求上限
                <Tip text="K线抓取的每分钟请求预算，超过后会在窗口内排队等待。" />
              </div>
              <n-input-number
                v-model:value="form.fetch.minutely_budget"
                :min="5"
                :max="300"
                style="width: 200px"
              />
            </div>
            <div class="setting-card-row">
              <div class="row-label">
                首抓/回补根数
                <Tip text="新品种建档及历史深度不足时一次性回填的5分钟K线根数。" />
              </div>
              <n-input-number
                v-model:value="form.fetch.backfill_count"
                :min="50"
                :max="2000"
                style="width: 200px"
              />
            </div>
            <div class="setting-card-row">
              <div class="row-label">
                增量抓取根数
                <Tip text="定时刷新时按距上次数据的时间差估算需补的根数，至少抓取该数量；缺口过大时自动按回补上限拉取。" />
              </div>
              <n-input-number
                v-model:value="form.fetch.incremental_count"
                :min="3"
                :max="100"
                style="width: 200px"
              />
            </div>
          </div>

          <label class="section-title">实时行情</label>
          <div class="setting-card">
            <div class="setting-card-row">
              <div class="row-label">
                轮询间隔（毫秒）
                <Tip text="交易时段内拉取实时现价的间隔，建议3秒或更长；修改后下一轮即生效。" />
              </div>
              <n-input-number
                v-model:value="form.quote.poll_interval_ms"
                :min="1000"
                :max="30000"
                style="width: 200px"
              />
            </div>
            <div class="setting-card-row">
              <div class="row-label">
                单请求间隔（毫秒）
                <Tip text="实时行情批量接口的相邻请求最小间隔。" />
              </div>
              <n-input-number
                v-model:value="form.quote.request_interval_ms"
                :min="100"
                :max="5000"
                style="width: 200px"
              />
            </div>
            <div class="setting-card-row">
              <div class="row-label">
                每分钟请求上限
                <Tip text="实时行情请求的每分钟预算，与K线抓取的预算互不影响。" />
              </div>
              <n-input-number
                v-model:value="form.quote.minutely_budget"
                :min="10"
                :max="600"
                style="width: 200px"
              />
            </div>
          </div>
        </div>
      </n-tab-pane>

      <n-tab-pane name="symbols">
        <template #tab>
          <div class="custom-tab-label">
            <span class="tab-icon"><n-icon :component="Ruler" /></span>
            <span>品种</span>
          </div>
        </template>
        <div class="tab-body">
          <label class="section-title">品种精度</label>
          <div class="setting-card symbol-tick-card">
            <div class="setting-card-row">
              <div class="row-label">
                品种最小变动价位（tick）
                <Tip text="入场价会在预警K线极值基础上按此 tick 偏移（做多=高点+tick，做空=低点-tick）。未显式设置的品种使用内置默认表；填 0 恢复默认。" />
              </div>
              <n-input
                v-model:value="symbolFilter"
                placeholder="搜索代码 / 名称 / 品种"
                clearable
                size="small"
                style="width: 240px"
              />
            </div>
          </div>
          <div class="symbol-tick-list">
            <div v-for="row in filteredSymbols" :key="row.code" class="symbol-tick-row">
              <span class="st-code">{{ row.code }}</span>
              <span class="st-name">{{ row.name || '—' }}</span>
              <span v-if="showVariety(row)" class="st-variety">{{ row.variety }}</span>
              <n-input-number
                :value="row.tick_size"
                :min="0"
                :step="tickStep(row.tick_size)"
                :precision="3"
                size="small"
                style="width: 110px"
                @update:value="(v: number | null) => onTickChange(row, v)"
              />
            </div>
            <div v-if="!filteredSymbols.length" class="symbol-tick-empty">
              <n-text depth="3">暂无匹配的品种</n-text>
            </div>
          </div>
        </div>
      </n-tab-pane>

    </n-tabs>

    <div class="footer">
      <n-button size="small" quaternary :disabled="saving" @click="confirmReset">
        <template #icon>
          <n-icon :component="RotateClockwise" />
        </template>
        恢复默认
      </n-button>
      <n-button
        type="primary"
        :disabled="!dirty || saving"
        :loading="saving"
        @click="save"
      >
        <template #icon>
          <n-icon :component="DeviceFloppy" />
        </template>
        保存设置
      </n-button>
    </div>
  </div>
</template>

<style scoped>
.settings-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  box-sizing: border-box;
  padding: 0;
  gap: 0;
  background: #fff;
  overflow: hidden;
}

.setting-tabs {
  flex: 1;
  min-height: 0;
  padding: 8px 12px;
  --n-tab-border-radius: 6px;
  --n-text-color-primary: #1f2329;
  --n-text-color-hover: #1677ff;
  --n-color-embedded: #f5f7f9;
}

.setting-tabs :deep(.n-tabs-content) {
  height: 100%;
}

.setting-tabs :deep(.n-tab-pane) {
  height: 100%;
  padding: 0;
}

.tab-body {
  height: 100%;
  min-height: 0;
  overflow-y: auto;
  padding-right: 10px;
}

/* 内容区统一使用细滚动条，观感更接近原生应用 */
.tab-body::-webkit-scrollbar {
  width: 8px;
}

.tab-body::-webkit-scrollbar-thumb {
  background: #d3dae3;
  border-radius: 4px;
}

.tab-body::-webkit-scrollbar-thumb:hover {
  background: #b9c2cd;
}

.tab-body::-webkit-scrollbar-track {
  background: transparent;
}

.section-title {
  display: block;
  font-size: 18px;
  font-weight: 700;
  color: #1f2329;
  margin: 2px 0 10px 4px;
}

.tab-body .section-title:not(:first-child) {
  margin-top: 20px;
}

.setting-card {
  border: 1px solid #eef1f5;
  border-radius: 10px;
  background: #fff;
  padding: 4px 16px;
}

.setting-card-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 12px 0;
  border-bottom: 1px solid #f1f3f6;
}

.setting-card-row:last-child {
  border-bottom: none;
}

.row-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  color: #3d4757;
  white-space: nowrap;
}

.help-icon {
  color: #94a3b8;
  cursor: help;
}

.symbol-tick-card {
  margin-bottom: 10px;
}

.symbol-tick-list {
  border: 1px solid #eef1f5;
  border-radius: 10px;
  padding: 2px 14px;
  background: #fff;
}

.symbol-tick-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 0;
  border-bottom: 1px solid #f1f3f6;
}

.symbol-tick-row:last-child {
  border-bottom: none;
}

.st-code {
  flex: none;
  width: 76px;
  font-size: 13px;
  font-weight: 600;
  color: #1f2329;
  font-variant-numeric: tabular-nums;
}

.st-name {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  color: #3d4757;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.st-variety {
  flex: none;
  width: 76px;
  font-size: 12px;
  color: #94a3b8;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.symbol-tick-empty {
  padding: 18px 0;
  text-align: center;
}

.footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex: none;
  padding: 8px 12px 12px;
  border-top: 1px solid #f0f2f5;
}

/* 左侧标签样式：图标 + 文字，参考模板的结构，配色用当前项目的蓝色系 */
.custom-tab-label {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  font-size: 14px;
  font-weight: 500;
}

.tab-icon {
  font-size: 16px;
  width: 20px;
  text-align: center;
}

.setting-tabs :deep(.n-tabs-tab--active) {
  color: var(--n-text-color-hover);
  background-color: rgba(22, 119, 255, 0.08);
  border-radius: 6px;
}

.setting-tabs :deep(.n-tabs-rail) {
  background-color: transparent;
  padding: 4px;
}

.setting-tabs :deep(.n-tabs-content) {
  background-color: transparent;
  padding-left: 8px;
}

/* 取消切换 tab 的滑动/淡入动画：颜色、背景与选中文字右移效果保留，只改为瞬时切换 */
.setting-tabs :deep(.n-tabs-bar),
.setting-tabs :deep(.n-tabs-tab),
.setting-tabs :deep(.n-tabs-tab-label),
.setting-tabs :deep(.n-tabs-pane-wrapper) {
  transition: none !important;
}
</style>

<style>
/* 长 tooltip 限制宽度并换行，避免弹出层过宽导致窗口出现横向滚动条 */
.n-popover {
  max-width: 340px;
}
</style>
