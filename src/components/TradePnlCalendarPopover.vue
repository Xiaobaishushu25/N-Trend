<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  NButton,
  NIcon,
  NPopover,
  NTag,
  NTooltip,
} from 'naive-ui'
import {
  Calendar,
  ChevronLeft,
  ChevronRight,
  RotateClockwise,
  X,
} from '@vicons/tabler'
import { extractTradeDate, formatCalendarPnl } from '../utils/tradeTime'
import { formatPnlAmount } from '../utils/multipliers'

interface DailyPnlItem {
  totalPnl: number
  count: number
  winCount: number
  lossCount: number
}

const props = defineProps<{
  modelValue: string // 格式: 'YYYY-MM-DD' 或 ''
  dailyPnlMap: Record<string, DailyPnlItem>
  latestExitTs?: string | null
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void
  (e: 'change', value: string): void
}>()

const popoverVisible = ref(false)

// 当前浏览的年月
const today = new Date()
const currentYear = ref(today.getFullYear())
const currentMonth = ref(today.getMonth()) // 0 - 11

// 初始化年月定位：优先取当前已选中的日期，其次取最新平仓记录的日期，再次取今天
function syncViewToBestMonth() {
  if (props.modelValue) {
    const parts = props.modelValue.split('-')
    if (parts.length === 3) {
      currentYear.value = parseInt(parts[0], 10)
      currentMonth.value = parseInt(parts[1], 10) - 1
      return
    }
  }
  if (props.latestExitTs) {
    const d = extractTradeDate(props.latestExitTs)
    if (d) {
      const parts = d.split('-')
      if (parts.length === 3) {
        currentYear.value = parseInt(parts[0], 10)
        currentMonth.value = parseInt(parts[1], 10) - 1
        return
      }
    }
  }
  currentYear.value = today.getFullYear()
  currentMonth.value = today.getMonth()
}

// 监听弹窗打开时，若未选日期则自动定位到有数据的月份
watch(popoverVisible, (visible) => {
  if (visible) {
    syncViewToBestMonth()
  }
})

// 月份切换
function prevMonth() {
  if (currentMonth.value === 0) {
    currentMonth.value = 11
    currentYear.value -= 1
  } else {
    currentMonth.value -= 1
  }
}

function nextMonth() {
  if (currentMonth.value === 11) {
    currentMonth.value = 0
    currentYear.value += 1
  } else {
    currentMonth.value += 1
  }
}

function resetToLatest() {
  syncViewToBestMonth()
}

// 星期列表（周一至周日）
const weekdays = ['一', '二', '三', '四', '五', '六', '日']

interface CalendarCell {
  dateStr: string
  dayNum: number
  isCurrentMonth: boolean
  isToday: boolean
  pnlItem?: DailyPnlItem
}

// 计算日历网格单元
const calendarCells = computed<CalendarCell[]>(() => {
  const y = currentYear.value
  const m = currentMonth.value

  const firstDay = new Date(y, m, 1)
  // 转换为周一为起始 (0 = 周一, ..., 6 = 周日)
  const firstDayOfWeek = (firstDay.getDay() + 6) % 7
  const daysInMonth = new Date(y, m + 1, 0).getDate()
  const daysInPrevMonth = new Date(y, m, 0).getDate()

  const todayStr = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`

  const cells: CalendarCell[] = []

  // 1. 上月残留日期
  for (let i = firstDayOfWeek - 1; i >= 0; i--) {
    const day = daysInPrevMonth - i
    const prevM = m === 0 ? 11 : m - 1
    const prevY = m === 0 ? y - 1 : y
    const dStr = `${prevY}-${String(prevM + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`
    cells.push({
      dateStr: dStr,
      dayNum: day,
      isCurrentMonth: false,
      isToday: dStr === todayStr,
      pnlItem: props.dailyPnlMap[dStr],
    })
  }

  // 2. 当月日期
  for (let day = 1; day <= daysInMonth; day++) {
    const dStr = `${y}-${String(m + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`
    cells.push({
      dateStr: dStr,
      dayNum: day,
      isCurrentMonth: true,
      isToday: dStr === todayStr,
      pnlItem: props.dailyPnlMap[dStr],
    })
  }

  // 3. 下月补齐到 7 的整倍数（总数 35 或 42）
  const totalSlots = cells.length <= 35 ? 35 : 42
  const remaining = totalSlots - cells.length
  for (let day = 1; day <= remaining; day++) {
    const nextM = m === 11 ? 0 : m + 1
    const nextY = m === 11 ? y + 1 : y
    const dStr = `${nextY}-${String(nextM + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`
    cells.push({
      dateStr: dStr,
      dayNum: day,
      isCurrentMonth: false,
      isToday: dStr === todayStr,
      pnlItem: props.dailyPnlMap[dStr],
    })
  }

  return cells
})

// 当月统计汇总
const currentMonthStats = computed(() => {
  const y = currentYear.value
  const m = currentMonth.value
  const prefix = `${y}-${String(m + 1).padStart(2, '0')}`

  let totalPnl = 0
  let count = 0
  let winCount = 0

  for (const [date, item] of Object.entries(props.dailyPnlMap)) {
    if (date.startsWith(prefix)) {
      totalPnl += item.totalPnl
      count += item.count
      winCount += item.winCount
    }
  }

  return {
    totalPnl,
    count,
    winRate: count > 0 ? (winCount / count) * 100 : 0,
  }
})

// 点击日期单元格
function handleCellClick(cell: CalendarCell) {
  // 如果单元格无流水，则不作处理
  if (!cell.pnlItem || cell.pnlItem.count === 0) return

  if (props.modelValue === cell.dateStr) {
    // 再次点击已选中的日期，取消筛选
    emit('update:modelValue', '')
    emit('change', '')
  } else {
    emit('update:modelValue', cell.dateStr)
    emit('change', cell.dateStr)
    popoverVisible.value = false
  }
}

// 清除日期筛选
function handleClear(e?: MouseEvent) {
  if (e) e.stopPropagation()
  emit('update:modelValue', '')
  emit('change', '')
}

// 选中日期的盈亏信息
const selectedPnlItem = computed(() => {
  if (!props.modelValue) return null
  return props.dailyPnlMap[props.modelValue] || null
})
</script>

<template>
  <div class="trade-pnl-calendar-trigger-wrap">
    <n-popover
      v-model:show="popoverVisible"
      trigger="click"
      placement="bottom-start"
      :style="{ padding: 0, width: '360px', maxWidth: '95vw', borderRadius: '8px' }"
    >
      <template #trigger>
        <!-- 外部按钮触发器 -->
        <n-button
          size="small"
          :type="modelValue ? 'primary' : 'default'"
          :secondary="Boolean(modelValue)"
          class="calendar-trigger-btn"
          :class="{ 'is-selected': Boolean(modelValue) }"
        >
          <template #icon>
            <n-icon :component="Calendar" />
          </template>
          <span v-if="!modelValue" class="trigger-text">盈亏日历</span>
          <span v-else class="trigger-text-selected">
            <span class="sel-date font-num">{{ modelValue }}</span>
            <span
              v-if="selectedPnlItem"
              class="sel-pnl font-num"
              :class="selectedPnlItem.totalPnl >= 0 ? 'is-up' : 'is-down'"
            >
              {{ selectedPnlItem.totalPnl >= 0 ? '+' : '' }}{{ Math.round(selectedPnlItem.totalPnl).toLocaleString('zh-CN') }}
            </span>
          </span>
          <n-icon
            v-if="modelValue"
            :component="X"
            size="14"
            class="clear-date-icon"
            title="清除日期筛选"
            @click.stop="handleClear"
          />
        </n-button>
      </template>

      <!-- 弹出日历主体内容 -->
      <div class="pnl-calendar-box">
        <!-- 1. 顶部控制栏: 月份切换与快速定位 -->
        <div class="calendar-header">
          <div class="header-nav">
            <button class="nav-arrow-btn" title="上一月" @click="prevMonth">
              <n-icon :component="ChevronLeft" size="16" />
            </button>
            <span class="current-month-label font-num">
              {{ currentYear }} 年 {{ currentMonth + 1 }} 月
            </span>
            <button class="nav-arrow-btn" title="下一月" @click="nextMonth">
              <n-icon :component="ChevronRight" size="16" />
            </button>
          </div>

          <div class="header-actions">
            <button class="nav-jump-btn" title="定位到最新记录月份" @click="resetToLatest">
              <n-icon :component="RotateClockwise" size="13" />
              <span>最新</span>
            </button>
            <n-button
              v-if="modelValue"
              size="tiny"
              quaternary
              type="primary"
              @click="handleClear"
            >
              全部流水
            </n-button>
          </div>
        </div>

        <!-- 2. 当月概览战报栏 -->
        <div class="calendar-month-summary">
          <div class="sum-item">
            <span class="sum-lbl">当月平仓</span>
            <span class="sum-val font-num">{{ currentMonthStats.count }} 笔</span>
          </div>
          <div class="sum-item">
            <span class="sum-lbl">当月累计盈亏</span>
            <span
              class="sum-val font-num font-bold"
              :class="currentMonthStats.totalPnl >= 0 ? 'is-up' : 'is-down'"
            >
              {{ formatPnlAmount(currentMonthStats.totalPnl, { decimals: 0 }) }}
            </span>
          </div>
          <div v-if="currentMonthStats.count > 0" class="sum-item">
            <span class="sum-lbl">当月胜率</span>
            <span class="sum-val font-num">
              {{ currentMonthStats.winRate.toFixed(1) }}%
            </span>
          </div>
        </div>

        <!-- 3. 星期表头 -->
        <div class="calendar-week-row">
          <div
            v-for="(w, idx) in weekdays"
            :key="w"
            class="week-col"
            :class="{ 'is-weekend': idx >= 5 }"
          >
            {{ w }}
          </div>
        </div>

        <!-- 4. 日历网格 -->
        <div class="calendar-grid">
          <div
            v-for="cell in calendarCells"
            :key="cell.dateStr"
            class="calendar-day-cell"
            :class="[
              !cell.isCurrentMonth ? 'is-other-month' : '',
              cell.isToday ? 'is-today' : '',
              cell.dateStr === modelValue ? 'is-active-selected' : '',
              cell.pnlItem && cell.pnlItem.count > 0 ? (cell.pnlItem.totalPnl >= 0 ? 'pnl-profit' : 'pnl-loss') : 'has-no-trade',
            ]"
            :title="cell.pnlItem && cell.pnlItem.count > 0
              ? `${cell.dateStr}：平仓 ${cell.pnlItem.count} 笔，盈亏 ${cell.pnlItem.totalPnl >= 0 ? '+' : ''}${cell.pnlItem.totalPnl.toFixed(2)} 元 (点击筛选)`
              : cell.dateStr"
            @click="handleCellClick(cell)"
          >
            <div class="cell-day-num font-num">
              <span>{{ cell.dayNum }}</span>
              <span v-if="cell.isToday" class="today-dot" title="今天" />
            </div>

            <div v-if="cell.pnlItem && cell.pnlItem.count > 0" class="cell-pnl-val font-num">
              {{ formatCalendarPnl(cell.pnlItem.totalPnl) }}
            </div>
            <div v-else class="cell-empty-dot" />
          </div>
        </div>

        <!-- 5. 底部说明与快捷重置 -->
        <div class="calendar-footer">
          <div class="footer-legend">
            <span class="legend-item"><span class="dot-profit" /> 盈利</span>
            <span class="legend-item"><span class="dot-loss" /> 亏损</span>
            <span class="legend-tip">点击日期即可筛选</span>
          </div>
          <div v-if="modelValue" class="footer-reset">
            <button class="clear-btn" @click="handleClear">
              清除筛选 (显示全部)
            </button>
          </div>
        </div>
      </div>
    </n-popover>
  </div>
</template>

<style scoped>
.trade-pnl-calendar-trigger-wrap {
  display: inline-flex;
  align-items: center;
}

.calendar-trigger-btn {
  display: inline-flex;
  align-items: center;
  font-size: 12px;
  height: 28px;
}

.trigger-text {
  margin-left: 2px;
}

.trigger-text-selected {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-left: 2px;
}

.sel-date {
  font-weight: 600;
}

.sel-pnl {
  font-weight: 700;
  font-size: 11px;
}

.clear-date-icon {
  margin-left: 4px;
  opacity: 0.65;
  transition: opacity 0.15s, transform 0.15s;
  cursor: pointer;
}

.clear-date-icon:hover {
  opacity: 1;
  transform: scale(1.15);
  color: #ef4444;
}

/* 弹窗日历主体 */
.pnl-calendar-box {
  background: #ffffff;
  padding: 12px;
  border-radius: 8px;
  user-select: none;
  color: #1e293b;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
}

/* 头部导航 */
.calendar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
  padding-bottom: 8px;
  border-bottom: 1px solid #f1f5f9;
}

.header-nav {
  display: flex;
  align-items: center;
  gap: 4px;
}

.nav-arrow-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border-radius: 4px;
  border: 1px solid #e2e8f0;
  background: #f8fafc;
  color: #475569;
  cursor: pointer;
  transition: all 0.15s;
}

.nav-arrow-btn:hover {
  background: #e2e8f0;
  color: #0f172a;
}

.current-month-label {
  font-size: 13px;
  font-weight: 700;
  color: #0f172a;
  min-width: 100px;
  text-align: center;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 6px;
}

.nav-jump-btn {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 2px 7px;
  border-radius: 4px;
  border: 1px solid #e2e8f0;
  background: #ffffff;
  font-size: 11px;
  color: #64748b;
  cursor: pointer;
  transition: all 0.15s;
}

.nav-jump-btn:hover {
  background: #f1f5f9;
  color: #2563eb;
  border-color: #cbd5e1;
}

/* 当月战报汇总 */
.calendar-month-summary {
  display: flex;
  align-items: center;
  justify-content: space-around;
  padding: 6px 10px;
  background: #f8fafc;
  border-radius: 6px;
  margin-bottom: 10px;
  border: 1px solid #edf2f7;
}

.sum-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 1px;
}

.sum-lbl {
  font-size: 10px;
  color: #94a3b8;
}

.sum-val {
  font-size: 12px;
  font-weight: 600;
  color: #334155;
}

/* 星期表头 */
.calendar-week-row {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: 4px;
  margin-bottom: 4px;
  text-align: center;
}

.week-col {
  font-size: 11px;
  font-weight: 600;
  color: #64748b;
  padding: 3px 0;
}

.week-col.is-weekend {
  color: #94a3b8;
}

/* 日历格子网格 */
.calendar-grid {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: 4px;
}

.calendar-day-cell {
  position: relative;
  min-height: 44px;
  padding: 3px 2px 2px 2px;
  border-radius: 5px;
  border: 1px solid transparent;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  align-items: center;
  background: #ffffff;
  transition: all 0.15s cubic-bezier(0.4, 0, 0.2, 1);
  box-sizing: border-box;
}

.cell-day-num {
  position: relative;
  font-size: 11px;
  font-weight: 500;
  line-height: 1;
  color: #334155;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.today-dot {
  position: absolute;
  top: -2px;
  right: -5px;
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: #2563eb;
}

.cell-pnl-val {
  font-size: 10px;
  font-weight: 700;
  line-height: 1.1;
  text-align: center;
  white-space: nowrap;
  letter-spacing: -0.2px;
}

.cell-empty-dot {
  height: 10px;
}

/* 非当月日期 */
.calendar-day-cell.is-other-month {
  opacity: 0.28;
}

.calendar-day-cell.is-other-month .cell-day-num {
  color: #94a3b8;
}

/* 无平仓记录 */
.calendar-day-cell.has-no-trade {
  background: #fafafa;
  border-color: #f1f5f9;
}

.calendar-day-cell.has-no-trade:hover {
  background: #f1f5f9;
}

/* 盈利红底 */
.calendar-day-cell.pnl-profit {
  background: #fef2f2;
  border-color: #fecaca;
  cursor: pointer;
}

.calendar-day-cell.pnl-profit .cell-pnl-val {
  color: #dc2626;
}

.calendar-day-cell.pnl-profit:hover {
  background: #fee2e2;
  border-color: #fca5a5;
  transform: translateY(-1px);
  box-shadow: 0 2px 4px rgba(220, 38, 38, 0.12);
}

/* 亏损绿底 */
.calendar-day-cell.pnl-loss {
  background: #f0fdf4;
  border-color: #bbf7d0;
  cursor: pointer;
}

.calendar-day-cell.pnl-loss .cell-pnl-val {
  color: #16a34a;
}

.calendar-day-cell.pnl-loss:hover {
  background: #dcfce7;
  border-color: #86efac;
  transform: translateY(-1px);
  box-shadow: 0 2px 4px rgba(22, 163, 74, 0.12);
}

/* 当前选中状态 */
.calendar-day-cell.is-active-selected {
  outline: 2px solid #2563eb !important;
  outline-offset: -1px;
  box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.25) !important;
  z-index: 2;
}

.calendar-day-cell.is-today .cell-day-num {
  font-weight: 700;
  color: #2563eb;
}

/* 底部操作与图例 */
.calendar-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid #f1f5f9;
  font-size: 11px;
}

.footer-legend {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #64748b;
}

.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 10px;
}

.dot-profit {
  width: 7px;
  height: 7px;
  border-radius: 2px;
  background: #dc2626;
}

.dot-loss {
  width: 7px;
  height: 7px;
  border-radius: 2px;
  background: #16a34a;
}

.legend-tip {
  font-size: 10px;
  color: #94a3b8;
  margin-left: 2px;
}

.clear-btn {
  background: none;
  border: none;
  color: #2563eb;
  font-size: 11px;
  cursor: pointer;
  padding: 2px 4px;
  border-radius: 4px;
}

.clear-btn:hover {
  text-decoration: underline;
  background: #eff6ff;
}

.font-num {
  font-variant-numeric: tabular-nums;
}

.is-up {
  color: #dc2626 !important;
}

.is-down {
  color: #16a34a !important;
}
</style>
