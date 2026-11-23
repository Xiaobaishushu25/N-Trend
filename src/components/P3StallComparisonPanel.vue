<script setup lang="ts">
import type { P3StallComparison } from '../types'

defineProps<{ comparison: P3StallComparison }>()

const shanghai = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
})
const time = (seconds: number) => shanghai.format(seconds * 1000)
const r = (value: number) => `${value > 0 ? '+' : ''}${value.toFixed(2)}R`
const reason: Record<string, string> = {
  entry_not_single: '增至两手', entry_time_corrected: '开仓时间已校正',
  quote_gap: '行情中断', execution_restarted: '服务重启',
  invalid_history: 'K 线无效', bar_gap: '缺收盘 K 线', late_bar: '收盘 K 线到达过晚',
  quote_timeout: '60 秒内缺有效报价', real_exit_boundary: '原单已到出场线',
  closed_before_quote: '取到报价前原单已平', actual_fill_unverified: '原单成交待确认',
  target_1r: '1R 止盈', stop: '止损', external_manual: '外部平仓',
  no_follow: '无跟随退出', time_exit: '持仓超时',
}
</script>

<template>
  <section class="stall" aria-label="P3 单手失速影子对比">
    <strong>P3 单手失速影子对比</strong>
    <p>观察原 S1 不足 1R 的单手单。达到 0.85R 后，两根已收盘的 15 分钟 K 线未刷新有利极值，且收回 S1 内侧时记录信号。影子退出按后续买一或卖一、滑点和参考手续费估算。</p>
    <div class="counts">
      <div><small>已观察 / 观察中</small><b>{{ comparison.observed }} / {{ comparison.active }}</b></div>
      <div><small>已触发 / 已配对</small><b>{{ comparison.triggered }} / {{ comparison.paired }}</b></div>
      <div><small>原单随后 1R 止盈</small><b>{{ comparison.target_later }} 笔</b></div>
      <div><small>累计配对净差额 · 1 跳</small><b>{{ comparison.paired ? r(comparison.delta_net_r) : '—' }}</b></div>
      <div><small>缺数据或无效</small><b>{{ comparison.unavailable }} 笔</b></div>
    </div>
    <div v-if="comparison.recent.length" class="table-scroll">
      <table>
        <thead><tr><th>合约 / 信号</th><th>失速信号</th><th>影子估算</th><th>原单平仓</th><th>净差额</th></tr></thead>
        <tbody><tr v-for="row in comparison.recent" :key="row.id">
          <td>{{ row.instrument }} / {{ row.event_id ?? '—' }}<small>{{ time(row.opened_at) }} 开仓</small></td>
          <td>
            <template v-if="row.signal">{{ time(row.signal.bar_at) }} · {{ row.signal.close }}<small>最高 {{ r(row.signal.peak_r) }}</small></template>
            <span v-else>{{ row.target_seen ? '先到目标' : row.open ? '观察中' : '未触发' }}</span>
            <small v-if="row.unavailable">{{ reason[row.unavailable] ?? row.unavailable }}</small>
          </td>
          <td v-if="row.reference">
            {{ row.reference.one_tick_price }} · {{ r(row.reference.one_tick_net_r) }}
            <small>{{ time(row.reference.quote_at) }} · 盘口 {{ row.reference.opposite_price }} · 2 跳 {{ r(row.reference.two_tick_net_r) }}</small>
          </td>
          <td v-else>—</td>
          <td v-if="row.actual">
            {{ row.actual.price }}<template v-if="row.actual.net_r != null"> · {{ r(row.actual.net_r) }}</template>
            <small>{{ row.actual.at }} · {{ reason[row.actual.reason] ?? row.actual.reason }}</small>
          </td>
          <td v-else>—</td>
          <td>{{ row.delta_net_r == null ? '—' : r(row.delta_net_r) }}</td>
        </tr></tbody>
      </table>
    </div>
    <p v-else>等待下一笔符合条件的 P3 开仓成交。</p>
  </section>
</template>

<style scoped>
.stall { background: #fff; border: 1px solid #dbe4ef; border-radius: 12px; padding: 18px; margin-bottom: 12px; color: #1e293b; }
p, small { font-size: 12px; line-height: 1.6; color: #64748b; }
.counts { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; }
.counts div { padding: 10px 12px; border: 1px solid #e2e8f0; border-radius: 8px; background: #f8fafc; }
.counts b, td small { display: block; }
.table-scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; white-space: nowrap; font-size: 12px; }
th, td { padding: 8px; text-align: left; border-bottom: 1px solid #e2e8f0; }
th { background: #f8fafc; }
</style>
