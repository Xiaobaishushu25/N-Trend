<script setup lang="ts">
import { defineComponent, h, onMounted, ref } from 'vue'
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
  useMessage,
} from 'naive-ui'
import {
  Activity,
  Clock,
  Database,
  FileText,
  Help,
  Mail,
  Palette,
  Settings as SettingsIcon,
} from '@vicons/tabler'
import { api } from '../services/api'
import { useSettingsStore } from '../stores/settings'
import type { Config } from '../types'

const settingsStore = useSettingsStore()
const message = useMessage()

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

onMounted(async () => {
  try {
    await settingsStore.load()
  } catch {
    // 浏览器预览环境下无后端命令，保持默认值
  }
  form.value = cloneConfig(settingsStore.settings)
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
                启动时自动运行定时任务
                <Tip text="应用启动后自动开始定时刷新与扫描，无需手动点击启动。" />
              </div>
              <n-switch v-model:value="form.app_config.auto_start_scheduler" />
            </div>
          </div>
        </div>
      </n-tab-pane>

      <n-tab-pane name="ui">
        <template #tab>
          <div class="custom-tab-label">
            <span class="tab-icon"><n-icon :component="Palette" /></span>
            <span>界面</span>
          </div>
        </template>
        <div class="tab-body">
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
        </div>
      </n-tab-pane>

      <n-tab-pane name="scheduler">
        <template #tab>
          <div class="custom-tab-label">
            <span class="tab-icon"><n-icon :component="Clock" /></span>
            <span>定时任务</span>
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
        </div>
      </n-tab-pane>

      <n-tab-pane name="fetch">
        <template #tab>
          <div class="custom-tab-label">
            <span class="tab-icon"><n-icon :component="Database" /></span>
            <span>数据抓取</span>
          </div>
        </template>
        <div class="tab-body">
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
        </div>
      </n-tab-pane>

      <n-tab-pane name="quote">
        <template #tab>
          <div class="custom-tab-label">
            <span class="tab-icon"><n-icon :component="Activity" /></span>
            <span>实时行情</span>
          </div>
        </template>
        <div class="tab-body">
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

      <n-tab-pane name="email">
        <template #tab>
          <div class="custom-tab-label">
            <span class="tab-icon"><n-icon :component="Mail" /></span>
            <span>邮件通知</span>
          </div>
        </template>
        <div class="tab-body">
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

      <n-tab-pane name="log">
        <template #tab>
          <div class="custom-tab-label">
            <span class="tab-icon"><n-icon :component="FileText" /></span>
            <span>日志</span>
          </div>
        </template>
        <div class="tab-body">
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

    </n-tabs>

    <div class="footer">
      <n-space justify="end">
        <n-button type="primary" :loading="saving" @click="save">保存设置</n-button>
      </n-space>
    </div>
  </div>
</template>

<style scoped>
.settings-page {
  display: flex;
  flex-direction: column;
  height: 100vh;
  box-sizing: border-box;
  padding: 10px 12px 8px;
  gap: 8px;
  background: #fff;
}

.setting-tabs {
  flex: 1;
  min-height: 0;
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
  overflow-y: auto;
  padding-right: 10px;
}

.section-title {
  display: block;
  font-size: 18px;
  font-weight: 700;
  color: #1f2329;
  margin: 2px 0 10px 4px;
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

.footer {
  flex: none;
  padding: 4px 8px 0;
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
