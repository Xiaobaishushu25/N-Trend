<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { listen } from '@tauri-apps/api/event'
import { getCurrentWindow } from '@tauri-apps/api/window'
import {
  NAutoComplete,
  NBadge,
  NButton,
  NDropdown,
  NIcon,
  NLayout,
  NLayoutContent,
  NTag,
  NText,
  type DropdownOption,
} from 'naive-ui'
import {
  BellX,
  Clock,
  DotsVertical,
  History,
  Plus,
  Refresh,
  Scan,
  Search,
  Settings as SettingsIcon,
  Tag,
  TrendingUp,
} from '@vicons/tabler'
import { api, onDataUpdated, onQuotesUpdated, onScanCompleted } from '../services/api'
import { useActionsStore } from '../stores/actions'
import { useAppStore } from '../stores/app'
import { useSettingsStore } from '../stores/settings'
import { useSymbolsStore } from '../stores/symbols'
import { openNotificationsWindow } from '../utils/openNotificationsWindow'
import { openReviewWindow } from '../utils/openReviewWindow'
import { openSettingsWindow } from '../utils/openSettingsWindow'
import { dismissAll, notify, notifyItems } from '../utils/notify'
import TitleBar from './TitleBar.vue'
import type { ContractSuggestion, OpenReviewChartPayload, SymbolRow } from '../types'

const route = useRoute()
const router = useRouter()
const appStore = useAppStore()
const settingsStore = useSettingsStore()
const actionsStore = useActionsStore()
const symbolsStore = useSymbolsStore()

/** 设置窗口（独立窗口，路由 /settings）：标题栏只保留窗口标题 */
const isStandaloneWindow = computed(
  () => route.name === 'settings' || route.name === 'review' || route.name === 'notifications',
)
const windowTitle = computed(() =>
  route.name === 'settings'
    ? '设置'
    : route.name === 'review'
      ? '复盘统计'
      : route.name === 'notifications'
        ? '历史通知'
        : '',
)
const activeNotifyCount = computed(() => notifyItems.length)

/** bare 路由（K线图/设置）：内容区不额外加内边距，由页面自行布局 */
const bare = computed(() => Boolean(route.meta.bare))

const newCode = ref('')

/** n-auto-complete 在选择/清空时会输出 null，统一归一为空串，避免 trim() 崩溃 */
function onNewCodeUpdate(v: string | null) {
  newCode.value = v ?? ''
}

/** 远端合约搜索结果：输入前缀（RB / 螺纹）时提示该品种的连续合约与各月份合约 */
const contractMatches = ref<ContractSuggestion[]>([])
const searching = ref(false)
let lookupTimer: ReturnType<typeof setTimeout> | undefined
let contractSearchWarned = false

/** 品种搜索/添加的自动补全：库内匹配 + 新浪各月合约，最多展示 10 条 */
const symbolOptions = computed(() => {
  const kw = newCode.value.trim().toUpperCase()
  if (!kw) return []
  const seen = new Set<string>()
  // 以库内记录为准判断“已添加”：即使某条建议来自新浪合约结果，只要代码在库中就标记已添加
  const dbByCode = new Map(symbolsStore.symbols.map((s) => [s.code, s]))
  const rows = [
    ...symbolsStore.symbols.filter(
      (s) =>
        s.code.toUpperCase().includes(kw) ||
        s.name.toUpperCase().includes(kw) ||
        s.variety.toUpperCase().includes(kw),
    ),
    ...contractMatches.value,
  ]
  return rows
    .filter((s) => {
      if (seen.has(s.code)) return false
      seen.add(s.code)
      return true
    })
    .slice(0, 10)
    .map((s) => {
      const dbRow = dbByCode.get(s.code)
      return {
        value: s.code,
        label: s.code,
        symbol: dbRow ?? s,
      }
    })
})

// 输入变化时防抖查询远端合约；纯数字不查（太宽泛），其余前缀都会尝试
watch(newCode, () => {
  if (lookupTimer) clearTimeout(lookupTimer)
  contractMatches.value = []
  const kw = newCode.value.trim().toUpperCase()
  if (!kw) return
  if (/^\d+$/.test(kw)) return
  lookupTimer = setTimeout(async () => {
    searching.value = true
    try {
      contractMatches.value = await api.searchContracts(kw)
    } catch (e) {
      contractMatches.value = []
      // 命令不存在说明运行的是旧构建（后端新增命令未编译进去）
      if (!contractSearchWarned && /not found|unknown command/i.test(String(e))) {
        contractSearchWarned = true
        notify.warning('合约搜索暂不可用：当前运行的程序版本较旧，请完全退出后重新启动应用')
      }
    } finally {
      searching.value = false
    }
  }, 350)
})

/** 下拉选项富文本：代码 + 中文名 + 交易所 + 「已添加」标记 */
function renderSymbolLabel(option: { value?: unknown; symbol?: unknown }) {
  const row = option.symbol as SymbolRow | undefined
  if (!row) return String(option.value ?? '')
  // 以本地库为准判断「已添加」：即使建议来自新浪合约结果，只要代码在库中就标记已添加
  const s = symbolsStore.symbols.find((x) => x.code === row.code) ?? row
  const added = Boolean(s.watchlist)
  return h(
    'div',
    {
      class: 'sym-option',
      title: added ? '已添加，点击打开K线图' : '点击添加',
    },
    [
      h('span', { class: 'sym-option-code' }, s.code),
      s.name && s.name !== s.code ? h('span', { class: 'sym-option-name' }, s.name) : null,
      added ? h('span', { class: 'sym-option-added' }, '已添加') : null,
    ],
  )
}

/** 聚焦搜索框时刷新本地品种列表，保证「已添加」判断用的是最新数据 */
async function reloadSymbols() {
  try {
    await symbolsStore.load()
  } catch {
    // 浏览器预览等环境忽略
  }
}

/** 选中下拉建议：已有品种直接打开K线图，新品种直接添加 */
function onSymbolSelect(code: string) {
  const existing = symbolsStore.symbols.find((s) => s.code === code)
  if (existing?.watchlist) {
    void router.push({ name: 'chart', params: { symbol: code } })
    return
  }
  void actionsStore.addSymbol(code)
}

/** 回车：新代码添加；已有品种直接打开对应K线图 */
async function doAddSymbol() {
  const raw = newCode.value.trim().toUpperCase()
  if (!raw) return
  // 输入不完整（如 rb）时，按当前下拉第一条建议解析，避免把前缀当成完整代码提交
  let code = raw
  if (!symbolsStore.symbols.some((s) => s.code === raw) && symbolOptions.value.length) {
    code = symbolOptions.value[0].value
  }
  const existing = symbolsStore.symbols.find((s) => s.code === code)
  if (existing?.watchlist) {
    newCode.value = ''
    void router.push({ name: 'chart', params: { symbol: code } })
    return
  }
  if (await actionsStore.addSymbol(code)) newCode.value = ''
}

/** 回车键传给内部 input（中文输入法组词确认时不触发） */
const addInputProps = {
  onKeydown: (e: KeyboardEvent) => {
    if (e.isComposing || e.key !== 'Enter') return
    e.preventDefault()
    void doAddSymbol()
  },
}

/** 「更多操作」下拉：只放执行类命令，设置入口独立成按钮（导航与命令分离） */
const actionOptions = computed<DropdownOption[]>(() => [
  {
    label: '刷新数据',
    key: 'refresh',
    icon: () => h(NIcon, { component: Refresh, size: 16 }),
    disabled: actionsStore.refreshing,
  },
  {
    label: '立即扫描',
    key: 'scan',
    icon: () => h(NIcon, { component: Scan, size: 16 }),
    disabled: actionsStore.scanning,
  },
  {
    label: '刷新名称',
    key: 'enrich',
    icon: () => h(NIcon, { component: Tag, size: 16 }),
    disabled: actionsStore.enriching,
  },
])

function onActionSelect(key: string) {
  if (key === 'refresh') void actionsStore.refreshData()
  else if (key === 'scan') void actionsStore.scanNow()
  else if (key === 'enrich') void actionsStore.enrichNames()
}

/** 状态时间只显示「MM-DD HH:mm:ss」，完整时间放 tooltip */
function shortTime(v: string | null | undefined): string {
  return v ? v.slice(5) : '—'
}

/**
 * 由「行情请求事件」驱动：每次收到实时行情/K线刷新事件就呼吸，
 * 一段时间没新事件就熄灭，起到实时指示效果。
 */
const PREVIEW_ALWAYS_BREATHING = false

const dataActive = ref(PREVIEW_ALWAYS_BREATHING)
let breatheTimer: ReturnType<typeof setTimeout> | undefined

function kickBreathe() {
  if (PREVIEW_ALWAYS_BREATHING) return
  dataActive.value = true
  if (breatheTimer) clearTimeout(breatheTimer)
  breatheTimer = setTimeout(() => {
    dataActive.value = false
  }, settingsStore.settings.ui.breathe_hold_ms)
}

const unlisteners: (() => void)[] = []

onMounted(async () => {
  // 复盘窗口点击明细行时：主窗口接收事件并打开对应K线图（复盘点位重绘见 ChartView）
  try {
    if (getCurrentWindow().label === 'main') {
      unlisteners.push(
        await listen<OpenReviewChartPayload>('open-review-chart', (e) => {
          const { symbol: sym, signalId, filters } = e.payload
          if (sym) {
            appStore.reviewJumpFilters = filters ?? null
            void router.push({
              name: 'chart',
              params: { symbol: sym },
              query: { review: String(signalId) },
            })
          }
        }),
      )
    }
  } catch {
    // 浏览器预览等环境无 Tauri 事件 API，忽略
  }
  try {
    await settingsStore.load()
  } catch {
    // 浏览器预览环境下无后端命令，忽略
  }
  try {
    await symbolsStore.load()
  } catch {
    // 浏览器预览环境下无后端命令，忽略
  }
  // 定时任务每次成功后，顶部时间信息实时跟随刷新
  unlisteners.push(
    await onDataUpdated(() => {
      settingsStore.refreshStatus()
      kickBreathe()
    }),
  )
  // 实时现价轮询每 3 秒返回一次行情，作为呼吸灯的主要驱动信号
  unlisteners.push(await onQuotesUpdated(() => kickBreathe()))
  unlisteners.push(await onScanCompleted(() => settingsStore.refreshStatus()))
})

onBeforeUnmount(() => {
  if (breatheTimer) clearTimeout(breatheTimer)
  if (lookupTimer) clearTimeout(lookupTimer)
  for (const fn of unlisteners) fn()
})
</script>

<template>
  <n-layout position="absolute" style="--app-header-h: 40px">
    <TitleBar :title="windowTitle">
      <template v-if="!isStandaloneWindow" #left>
        <div class="brand">
          <n-icon :component="TrendingUp" size="20" color="#f5c23f" />
          <span class="brand-name">N趋势</span>
          <n-text depth="3" style="font-size: 12px">v{{ appStore.info.version }}</n-text>
        </div>
        <div class="add-box">
          <n-auto-complete
            :value="newCode"
            @update:value="onNewCodeUpdate"
            :options="symbolOptions"
            :input-props="addInputProps"
            :render-label="renderSymbolLabel"
            :loading="searching"
            clear-after-select
            placeholder="搜索品种/合约，如 RB"
            size="small"
            class="add-input"
            @focus="reloadSymbols"
            @select="onSymbolSelect"
          >
            <template #prefix>
              <n-icon :component="Search" size="15" />
            </template>
          </n-auto-complete>
          <n-button
            type="primary"
            size="small"
            title="添加品种"
            class="add-button"
            :loading="actionsStore.adding"
            @click="doAddSymbol"
          >
            <template #icon>
              <n-icon :component="Plus" size="15" />
            </template>
          </n-button>
        </div>
      </template>

      <template v-if="!isStandaloneWindow" #center>
        <div class="status-area">
          <div
            v-if="settingsStore.status.running"
            class="live-tag"
            :class="{ 'is-breathing': dataActive }"
            title="定时刷新扫描运行中"
          >
            <span class="live-dot" />
            <span>运行中</span>
          </div>
          <n-tag v-else type="warning" size="small" round>已暂停</n-tag>
          <div class="status-meta">
            <div
              class="status-item"
              :title="`数据最近更新于：${settingsStore.status.last_refresh || '—'}`"
            >
              <span class="dot dot-data" />
              <span class="status-label">数据</span>
              <span class="status-value" :class="{ empty: !settingsStore.status.last_refresh }">
                {{ shortTime(settingsStore.status.last_refresh) }}
              </span>
            </div>
            <div class="status-divider" />
            <div
              class="status-item"
              :title="`形态最新识别于：${settingsStore.status.last_scan || '—'}`"
            >
              <span class="dot dot-scan" />
              <span class="status-label">形态</span>
              <span class="status-value" :class="{ empty: !settingsStore.status.last_scan }">
                {{ shortTime(settingsStore.status.last_scan) }}
              </span>
            </div>
          </div>
        </div>
      </template>

      <template v-if="!isStandaloneWindow" #right>
        <n-dropdown trigger="click" :options="actionOptions" @select="onActionSelect">
          <n-button quaternary circle size="small" title="更多操作">
            <template #icon>
              <n-icon :component="DotsVertical" />
            </template>
          </n-button>
        </n-dropdown>
        <n-button
          quaternary
          circle
          size="small"
          title="复盘统计"
          class="review-button"
          @click="openReviewWindow"
        >
          <template #icon>
            <n-icon :component="History" size="18" />
          </template>
        </n-button>
        <n-button
          quaternary
          circle
          size="small"
          title="历史通知"
          class="notifications-button"
          @click="openNotificationsWindow"
        >
          <template #icon>
            <n-icon :component="Clock" size="18" />
          </template>
        </n-button>
        <n-button
          quaternary
          circle
          size="small"
          title="设置"
          class="settings-button"
          @click="openSettingsWindow"
        >
          <template #icon>
            <n-icon :component="SettingsIcon" size="18" />
          </template>
        </n-button>
        <n-badge
          v-if="activeNotifyCount > 0"
          :value="activeNotifyCount"
          :max="99"
          :offset="[-8, 2]"
          class="clear-notifications-badge"
        >
          <n-button
            quaternary
            circle
            size="small"
            :title="`清空全部通知（${activeNotifyCount}）`"
            class="clear-notifications-button"
            @click="dismissAll"
          >
            <template #icon>
              <n-icon :component="BellX" size="18" />
            </template>
          </n-button>
        </n-badge>
      </template>
    </TitleBar>

    <n-layout-content
      position="absolute"
      style="top: var(--app-header-h)"
      :native-scrollbar="false"
      :content-style="bare ? 'height: 100%; padding: 0' : 'height: 100%; box-sizing: border-box; padding: 16px'"
    >
      <!-- 只缓存列表页（DashboardView）：返回时不再全量重载；K线图页不缓存，保持每次进入重置视图 -->
      <router-view v-slot="{ Component }">
        <keep-alive include="DashboardView">
          <component :is="Component" />
        </keep-alive>
      </router-view>
    </n-layout-content>
  </n-layout>
</template>

<style scoped>
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
}
.brand-name {
  font-size: 16px;
  font-weight: 700;
  letter-spacing: 1px;
}
.add-box {
  display: flex;
  align-items: center;
  gap: 4px;
}
.add-input {
  width: 230px;
}
.add-button {
  width: 20px;
  height: 20px;
  padding: 0;
  border-radius: 5px;
}
.status-area {
  display: flex;
  align-items: center;
  gap: 12px;
}
.live-tag {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 24px;
  padding: 0 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
  color: #0f9d58;
  background: rgba(15, 157, 88, 0.12);
  white-space: nowrap;
}
.live-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex: none;
  background: #18a058;
  box-shadow: 0 0 4px rgba(24, 160, 88, 0.6);
}
.live-tag.is-breathing .live-dot {
  animation: breathe 1.6s ease-in-out infinite;
}
@keyframes breathe {
  0%,
  100% {
    transform: scale(0.85);
    opacity: 0.55;
    box-shadow: 0 0 3px rgba(24, 160, 88, 0.35);
  }
  50% {
    transform: scale(1.35);
    opacity: 1;
    box-shadow: 0 0 10px rgba(24, 160, 88, 0.9);
  }
}
.status-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 22px;
  padding-left: 12px;
  border-left: 1px solid #e8ecf1;
}
.status-item {
  display: flex;
  align-items: center;
  gap: 5px;
  white-space: nowrap;
}
.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex: none;
}
.dot-data {
  background: #4098ff;
}
.dot-scan {
  background: #18a058;
}
.status-label {
  font-size: 12px;
  color: #97a0b3;
}
.status-value {
  font-size: 12px;
  color: #3d4757;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.status-value.empty {
  color: #c2c9d4;
  font-weight: 400;
}
.status-divider {
  width: 1px;
  height: 14px;
  background: #e5e9ef;
  flex: none;
}
.clear-notifications-badge :deep(.n-badge-sup) {
  min-width: 16px;
  height: 14px;
  line-height: 14px;
  padding: 0 4px;
  border-radius: 7px;
  box-sizing: border-box;
  font-size: 10px;
}
</style>
