<script setup lang="ts">
import { NButton } from 'naive-ui'
import type { P3RunnerComparison, P3ShadowExit } from '../types'

defineProps<{ comparison?: P3RunnerComparison; updating: boolean }>()
const emit = defineEmits<{ change: [enabled: boolean] }>()

const shanghai = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
})
const time = (ts: number) => shanghai.format(ts * 1000)
const money = (amount: number) => `${amount > 0 ? '+' : ''}${amount.toFixed(2)} 元`
const showExit = (exit: P3ShadowExit) => `${exit.price} · ${exit.realized_r.toFixed(2)}R · 回吐 ${exit.giveback_r.toFixed(2)}R`
const reasons: Record<string, string> = {
  quote_gap: '行情中断', atr_unavailable: '缺已收盘 ATR', execution_restarted: '服务重启，行情不连续',
  risk_budget: '实盘风险预算复核未通过', unavailable_close_book: '触发时缺可成交盘口',
  entry_time_corrected: '开仓时间校正，影子路径不完整',
  stop: '初始止损', pre_holiday: '节前退出', pre_weekend: '周末前退出',
  time_exit: '持仓超时', no_follow: '无跟随', contract_expiry: '合约到期退出', pre_calendar_end: '日历到期退出',
  trailing: '追踪止盈', breakeven: '保本退出',
}
</script>

<template>
  <section class="p3-runner" aria-label="P3 余仓前瞻对比">
    <header>
      <div>
        <strong>P3 余仓前瞻对比</strong>
        <small v-if="comparison?.started_at"> · {{ time(comparison.started_at) }} 启动</small>
      </div>
      <n-button v-if="comparison" size="small" :type="comparison.enabled ? 'default' : 'primary'"
        :disabled="(!comparison.eligible && !comparison.enabled) || updating" :loading="updating"
        @click="emit('change', !comparison.enabled)">
        {{ comparison.enabled ? '停止收新样本' : '启用影子对比' }}
      </n-button>
    </header>
    <p v-if="!comparison">当前服务端尚未提供 P3 余仓实验。</p>
    <template v-else>
      <p v-if="!comparison.eligible">新样本需要 P3 两手分批追踪模式。</p>
      <p>以确认开仓的两手为起点，共用首手影子成交。余仓比较 0.8R 与 max(0.8R, 2 × 已收盘15分钟ATR14)。按盘口与参考手续费估算。</p>
      <div class="counts">
        <div><small>有效配对 · 动态减现行</small><b>{{ comparison.paired }} 笔 · {{ comparison.paired ? money(comparison.delta_yuan) : '—' }}</b></div>
        <div><small>手动干预后配对 · 单独统计</small><b>{{ comparison.interrupted_pairs }} 笔 · {{ comparison.interrupted_pairs ? money(comparison.interrupted_delta_yuan) : '—' }}</b></div>
        <div><small>观察中 / 缺数据或无效 / 未进入余仓</small><b>{{ comparison.active }} / {{ comparison.unavailable }} / {{ comparison.no_runner }}</b></div>
      </div>
      <p>外部手动平仓会单独记录，两组影子继续观察。停止收新样本后，已经开始的观察继续。</p>
      <div v-if="comparison.recent.length" class="table-scroll">
        <table>
          <thead><tr><th>信号 / 合约</th><th>首手与观察状态</th><th>现行 0.8R 余仓</th><th>动态 ATR 余仓</th><th>估算净差额</th><th>实际手动操作</th></tr></thead>
          <tbody><tr v-for="row in comparison.recent" :key="row.id">
            <td>{{ row.event_id ?? '—' }} / {{ row.instrument }}<small>{{ time(row.started_at) }}</small></td>
            <td>
              <span v-if="row.first_fill">{{ row.first_fill.price }} · {{ time(row.first_fill.at) }}</span>
              <span v-else-if="row.common_exit">{{ reasons[row.common_exit.reason] ?? row.common_exit.reason }}</span>
              <span v-else-if="row.active">等待首手目标</span>
              <small v-if="row.invalid">{{ reasons[row.invalid] ?? row.invalid }}</small>
            </td>
            <td v-for="(exit, idx) in [row.base, row.dynamic]" :key="idx">
              <template v-if="exit">
                {{ showExit(exit) }}<small>{{ time(exit.at) }} · {{ reasons[exit.reason] ?? exit.reason }}</small>
              </template>
              <template v-else>{{ row.first_fill && row.active ? '观察中' : '—' }}</template>
            </td>
            <td>{{ row.delta_yuan == null || row.invalid ? '—' : money(row.delta_yuan) }}</td>
            <td><template v-if="row.interventions.length">
              <small v-for="(fill, idx) in row.interventions" :key="idx">{{ fill.at || '时间待核' }} · {{ fill.lots }} 手 @ {{ fill.price }}{{ fill.broker_fill ? '' : '（核销/记账）' }}</small>
            </template><template v-else>—</template></td>
          </tr></tbody>
        </table>
      </div>
      <p v-else>{{ comparison.enabled ? '等待启用后首次确认开仓两手。' : '启用后开始记录新的两手交易。' }}</p>
    </template>
  </section>
</template>

<style scoped>
.p3-runner { background: #fff; border: 1px solid #dbe4ef; border-radius: 12px; padding: 18px; color: #1e293b; }
header { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 10px; }
p, small { font-size: 12px; line-height: 1.6; color: #64748b; }
.counts { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; }
.counts div { padding: 10px 12px; border: 1px solid #e2e8f0; border-radius: 8px; background: #f8fafc; }
.counts b, td small { display: block; }
.table-scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; white-space: nowrap; font-size: 12px; }
th, td { padding: 8px; text-align: left; vertical-align: top; border-bottom: 1px solid #e2e8f0; }
th { background: #f8fafc; }
</style>
