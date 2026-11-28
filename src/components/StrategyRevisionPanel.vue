<script setup lang="ts">
import { computed } from 'vue'
import { NButton, NTag } from 'naive-ui'
import type { StrategyRevision } from '../types'
import { formatPnlAmount } from '../utils/multipliers'
import {
  strategyOverview, strategyRevisionChanges, strategyRevisionKey,
  type StrategyRevisionStats,
} from '../utils/traderStrategyVersions'

const props = defineProps<{
  rows: StrategyRevision[]
  labels: Record<string, string>
  stats: Record<string, StrategyRevisionStats>
  activeKey: string | undefined
  focusKey: string | undefined
}>()
const emit = defineEmits<{ history: [key: string] }>()
const timeline = computed(() => [...props.rows].reverse())
const byKey = computed(() => new Map(props.rows.map(row => [keyFor(row), row])))
const formatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', dateStyle: 'medium', timeStyle: 'medium', hour12: false,
})
const time = (at: number) => formatter.format(at * 1000)
const keyFor = (row: Pick<StrategyRevision, 'strategy_id' | 'revision'>) =>
  strategyRevisionKey(row.strategy_id, row.revision)

function evidence(row: StrategyRevision) {
  if (row.effective_at != null) return `生效于 ${time(row.effective_at)}`
  if (row.first_condition_at != null) return `首次观测到条件单 ${time(row.first_condition_at)}`
  if (row.first_fill_at != null) return `首次观测到成交 ${time(row.first_fill_at)}`
  return '生效时间待核'
}

function pnl(key: string): string {
  const stats = props.stats[key]
  if (!stats) return ''
  const { fees } = stats
  if (fees.confirmed_count === fees.total_count && fees.net_pnl_cents != null) {
    return `费后净额 ${formatPnlAmount(fees.net_pnl_cents / 100, { decimals: 2 })}`
  }
  return `毛盈亏 ${formatPnlAmount(stats.gross, { decimals: 2 })} · 结算 ${fees.confirmed_count}/${fees.total_count} 条`
}
</script>

<template>
  <section class="revision-timeline" aria-label="策略变更历史">
    <p v-if="!rows.length">暂无可追溯的版本记录。</p>
    <details v-for="row in timeline" :key="keyFor(row)" :open="focusKey === keyFor(row)" class="revision-entry">
      <summary>
        <span class="revision-title">{{ labels[keyFor(row)] }}</span>
        <n-tag v-if="keyFor(row) === activeKey" size="small" type="info">当前</n-tag>
        <span class="revision-time">{{ evidence(row) }}</span>
      </summary>
      <div class="revision-body">
        <p v-if="row.note">{{ row.note }}</p>
        <p v-if="strategyOverview(row.definition, row.execution)">{{ strategyOverview(row.definition, row.execution) }}</p>
        <ul v-if="row.previous && byKey.has(keyFor(row.previous))">
          <li v-for="change in strategyRevisionChanges(row, byKey.get(keyFor(row.previous)))" :key="change.key">
            {{ change.label }}　{{ change.before }} → {{ change.after }}
          </li>
        </ul>
        <div v-if="stats[keyFor(row)]" class="revision-metrics">
          <span>完整平仓 {{ stats[keyFor(row)].completed }} 笔</span>
          <span v-if="stats[keyFor(row)].completed">
            毛胜率 {{ (100 * stats[keyFor(row)].wins / stats[keyFor(row)].completed).toFixed(1) }}%
          </span>
          <span>{{ pnl(keyFor(row)) }}</span>
          <n-button size="small" text type="primary" @click="emit('history', keyFor(row))">查看该版成交</n-button>
        </div>
        <details v-if="row.definition || row.execution" class="revision-config">
          <summary>配置快照</summary>
          <pre>{{ JSON.stringify({ 策略参数: row.definition?.parameters, 执行设置: row.execution }, null, 2) }}</pre>
        </details>
      </div>
    </details>
  </section>
</template>

<style scoped>
.revision-timeline { display: grid; gap: 8px; max-height: 65vh; overflow: auto; }
.revision-entry { padding: 10px 12px; border: 1px solid #dbe4ef; border-radius: 8px; }
.revision-entry > summary, .revision-config > summary { cursor: pointer; }
.revision-title { font-weight: 650; margin-right: 8px; }
.revision-time { color: #475569; margin-left: 8px; }
.revision-body p { margin: 8px 0; }
.revision-body ul { margin: 8px 0; padding-left: 20px; }
.revision-metrics { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; padding: 6px 0; }
pre { margin: 8px 0 0; padding: 8px; max-height: 15rem; overflow: auto; background: #f1f5f9; }
</style>
