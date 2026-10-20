<script setup lang="ts">
import { computed, ref } from 'vue'
import type { ExecutionConfig, StrategyComparisonSummary } from '../types'

const props = defineProps<{ comparison?: StrategyComparisonSummary | null; liveConfig?: ExecutionConfig | null; updating?: boolean }>()
const emit = defineEmits<{ 'change-filter': [ticks: number] }>()
function changeFilter(event: Event) { emit('change-filter', Number((event.target as HTMLSelectElement).value)) }
const selectedId = ref('')
const run = computed(() => props.comparison?.runs.find((r) => r.definition.id === selectedId.value)
  ?? props.comparison?.runs[0])
const number = (v: number | null | undefined, digits = 2) => v == null ? '—' : v.toFixed(digits)
const money = (v: number) => `${v >= 0 ? '+' : ''}${v.toFixed(2)}`
const time = (ts: number) => new Date(ts * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
const pairNames: Record<string, string> = {
  'base_hold:filter_hold': '10 tick 过滤的影响（常规退出）',
  'base_hold:base_flat': '收盘前清仓的影响（原规则）',
  'filter_hold:filter_flat': '收盘前清仓的影响（过滤组）',
}
const states: Record<string, string> = {
  pending: '等待', filtered: '过滤', open: '持有', closed: '结束',
  expired: '过期', invalidated: '失效', missing_data: '缺行情',
  not_prospective: '首次已知结局',
}
</script>

<template>
  <section class="strategy-comparison" aria-label="策略前瞻对比">
    <div class="comparison-heading">
      <div><strong>策略前瞻对比</strong><span class="shadow-badge">影子观察</span></div>
      <label v-if="comparison?.runs.length">
        实验批次
        <select v-model="selectedId" aria-label="选择实验批次">
          <option value="">最新批次</option>
          <option v-for="r in comparison.runs" :key="r.definition.id" :value="r.definition.id">
            {{ time(r.definition.started_at) }} · {{ r.signals }} 个信号
          </option>
        </select>
      </label>
    </div>
    <label v-if="liveConfig?.min_entry_risk_ticks != null" class="live-choice">
      实际新开仓
      <select :value="liveConfig.min_entry_risk_ticks" :disabled="updating" aria-label="实际新开仓采用的规则" @change="changeFilter">
        <option :value="10">10 tick 过滤（原规则继续影子对比）</option>
        <option :value="0">原规则（10 tick 组继续影子对比）</option>
      </select>
    </label>
    <p class="comparison-note">
      原规则 / 10 tick 过滤 × 常规退出 / 收盘前 2 分钟清仓。四组使用同一盘口模型，真实成交在上方单独统计。
      估算净盈亏在买卖盘口之外额外扣除每手开平 2.5 tick 成本；未模拟五万元保证金容量和排队成交。
    </p>
    <p v-if="comparison?.archive_error" class="negative comparison-note">{{ comparison.archive_error }}</p>
    <template v-if="run">
      <p class="batch-note">
        自 {{ time(run.definition.started_at) }} 起记录 · {{ run.signals }} 个信号 · {{ run.definition.version }}
        · 注册时实际门槛：{{ run.definition.execution_config.min_entry_risk_ticks ? `${run.definition.execution_config.min_entry_risk_ticks} tick` : '原规则' }}
        · {{ run.definition.version === 'paired-quote-v2-continuous-08r' ? '首手1R兑现，次手1.6R起回撤0.8R连续追踪' : '旧版退出规则' }}
      </p>
      <div class="table-scroll">
        <table>
          <thead><tr><th>策略组合</th><th>已结束</th><th>已过滤</th><th>等待 / 持有</th><th>无效 / 缺行情</th><th>毛胜率</th><th>毛盈亏（元）</th><th>估算净盈亏（元）</th><th>平均 R</th></tr></thead>
          <tbody><tr v-for="g in run.groups" :key="g.id">
            <td>{{ g.label }}</td><td>{{ g.closed }}</td><td>{{ g.filtered }}</td>
            <td>{{ g.pending }} / {{ g.opened }}</td><td>{{ g.invalid }}</td>
            <td>{{ g.win_rate_pct == null ? '—' : `${number(g.win_rate_pct, 1)}%` }}</td>
            <td :class="g.gross_amount >= 0 ? 'positive' : 'negative'">{{ g.closed ? money(g.gross_amount) : '—' }}</td>
            <td :class="g.estimated_net_amount >= 0 ? 'positive' : 'negative'">{{ g.closed ? money(g.estimated_net_amount) : '—' }}</td>
            <td>{{ number(g.avg_r, 3) }}</td>
          </tr></tbody>
        </table>
      </div>
      <div class="paired-effects">
        <div v-for="pair in run.pairs" :key="`${pair.base}:${pair.candidate}`" class="paired-effect">
          <span>{{ pairNames[`${pair.base}:${pair.candidate}`] }}</span>
          <strong :class="pair.estimated_net_delta >= 0 ? 'positive' : 'negative'">
            {{ pair.matched_signals ? `${money(pair.estimated_net_delta)} 元` : '等待成对结果' }}
          </strong>
          <small>{{ pair.matched_signals }} 个双方均已结束或明确过滤的信号</small>
        </div>
      </div>
      <p class="comparison-note">正差额表示该候选在已配对样本中更好。胜率只计完整结束交易；未结束、缺行情和行情跳号样本不会作为盈利样本，配对结果排除跳号信号。没有完整结果时显示“—”。</p>
      <details v-if="run.recent.length">
        <summary>最近信号的四组决策</summary>
        <div class="table-scroll"><table>
          <thead><tr><th>信号 / 合约</th><th>计划风险</th><th v-for="g in run.groups" :key="g.id">{{ g.label }}</th></tr></thead>
          <tbody><tr v-for="s in run.recent" :key="s.condition_id">
            <td>{{ s.event_id ?? '—' }} / {{ s.instrument_id }}</td><td>{{ number(s.risk_ticks) }} tick</td>
            <td v-for="v in s.variants" :key="v.id" :title="v.reason">{{ states[v.status] ?? v.status }}</td>
          </tr></tbody>
        </table></div>
      </details>
    </template>
    <p v-else class="empty-comparison">部署后从新信号开始观察，历史成交不会补写成前瞻实验。暂无数据不代表任何策略更优。</p>
  </section>
</template>

<style scoped>
.strategy-comparison { min-width: 0; max-width: 100%; box-sizing: border-box; background: #fff; border: 1px solid #dbe4ef; border-radius: 12px; padding: 18px; margin: 0; color: #1e293b; }
.comparison-heading { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; }
.comparison-heading strong { font-size: 16px; }
.shadow-badge { margin-left: 10px; color: #0369a1; background: #e0f2fe; border-radius: 5px; padding: 2px 7px; font-size: 12px; }
.comparison-heading label { font-size: 12px; color: #64748b; }
.live-choice { display: block; margin-top: 12px; font-size: 12px; color: #475569; }
select { max-width: 280px; margin-left: 6px; border: 1px solid #cbd5e1; border-radius: 5px; padding: 5px; background: white; color: #334155; }
.comparison-note, .batch-note, .empty-comparison { font-size: 12px; color: #64748b; line-height: 1.7; margin: 10px 0; }
.table-scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; white-space: nowrap; font-size: 12px; }
th, td { padding: 10px 12px; text-align: right; border-bottom: 1px solid #e2e8f0; }
th { color: #64748b; font-weight: 500; background: #f8fafc; }
th:first-child, td:first-child { text-align: left; }
.positive { color: #059669; }.negative { color: #dc2626; }
.paired-effects { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-top: 14px; }
.paired-effect { background: #f8fafc; border: 1px solid #e2e8f0; padding: 12px; border-radius: 7px; display: flex; flex-direction: column; gap: 5px; }
.paired-effect span, .paired-effect small { font-size: 12px; color: #64748b; }.paired-effect strong { font-size: 17px; }
summary { cursor: pointer; font-size: 12px; color: #475569; padding: 8px 0; }
@media (max-width: 850px) { .paired-effects { grid-template-columns: 1fr; } }
</style>
