<script setup lang="ts">
import type { P3V4Comparison, P3ShadowExit } from '../types'

defineProps<{ comparison: P3V4Comparison }>()

const money = (value: number | null) => value == null
  ? '—'
  : `${value > 0 ? '+' : ''}${value.toFixed(2)} 元`
const shanghai = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hour12: false,
})
const status = {
  paired: '已配对',
  pending: '观察中',
  over_budget: '预算仅够一手',
  unavailable: '无法配对',
}
const reasons: Record<string, string> = {
  quote_gap: '行情缺口', execution_restarted: '服务重启', risk_budget: '实盘预算复核',
  unavailable_close_book: '缺可成交盘口', entry_time_corrected: '开仓时间校正',
}
const showExit = (exit: P3ShadowExit | null) => exit ? exit.price.toFixed(2) : '—'
</script>

<template>
  <section class="v4-comparison" aria-label="P3 V4 影子对比">
    <h3>P3 V4 影子对比</h3>
    <dl class="v4-summary">
      <div>
        <dt>V3 两手 · 余仓追踪</dt>
        <dd>{{ comparison.v3_two_lot.paired }} 笔配对 · 相比 V4 {{ comparison.v3_two_lot.paired ? money(comparison.v3_two_lot.delta_yuan) : '—' }}</dd>
        <dd class="muted">
          {{ comparison.v3_two_lot.pending }} 笔观察中 · {{ comparison.v3_two_lot.over_budget }} 笔预算仅够一手 ·
          {{ comparison.v3_two_lot.unavailable }} 笔无法配对
        </dd>
      </div>
      <div>
        <dt>仅做原 S1 大于 1R</dt>
        <dd>{{ comparison.far_s1.kept }} 笔保留 · {{ comparison.far_s1.skipped }} 笔跳过</dd>
        <dd class="muted">
          已核实 {{ comparison.far_s1.comparable }} 笔 ·
          保留组 {{ comparison.far_s1.settled ? money(comparison.far_s1.net_yuan) : '—' }} ·
          差额 {{ comparison.far_s1.comparable ? money(comparison.far_s1.delta_yuan) : '—' }}
        </dd>
      </div>
    </dl>
    <p class="muted">以 V4 的实际开仓为起点。两手影子按相同入场价估算，首手到目标，余仓按 V3 的 0.8R 距离追踪。远 S1 对照复用实际自动平仓结果。</p>
    <p v-if="!comparison.observed">等待 V4 的首笔开仓成交。</p>
    <div v-else class="v4-table-scroll">
      <table>
        <thead><tr><th>合约 / 信号</th><th>原 S1 距离</th><th>V4 费后</th><th>V3 两手费后</th><th>影子成交价</th></tr></thead>
        <tbody>
          <tr v-for="row in comparison.recent" :key="row.id">
            <td>{{ row.instrument }} / {{ row.event_id ?? '—' }}<small>{{ shanghai.format(row.opened_at * 1000) }}</small></td>
            <td>{{ row.s1_r.toFixed(2) }}R<small>{{ row.far_s1 ? '保留' : '跳过' }}</small></td>
            <td>{{ money(row.actual_net_yuan) }}</td>
            <td>
              {{ row.v3_status === 'paired' ? money(row.v3_net_yuan) : status[row.v3_status] }}
              <small v-if="row.v3_invalid">{{ reasons[row.v3_invalid] ?? row.v3_invalid }}</small>
            </td>
            <td v-if="row.v3_common_exit">全平 {{ showExit(row.v3_common_exit) }}</td>
            <td v-else>{{ showExit(row.v3_first) }} / {{ showExit(row.v3_tail) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<style scoped>
.v4-comparison { background: #fff; border: 1px solid #dbe4ef; border-radius: 12px; padding: 18px; color: #1e293b; }
h3 { margin: 0; font-size: 16px; }
.v4-summary { display: flex; flex-wrap: wrap; gap: 12px; }
.v4-summary > div { flex: 1 1 240px; background: #f1f5f9; border-radius: 8px; padding: 10px 12px; }
dt, th { font-weight: 600; }
dd { margin: 4px 0 0; }
.muted, small { color: #475569; font-size: 12px; }
small { display: block; }
.v4-table-scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; white-space: nowrap; }
th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid #e2e8f0; }
</style>
