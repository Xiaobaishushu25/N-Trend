<script setup lang="ts">
import { computed } from 'vue'
import type { SignalQualityDto } from '../types'
import { useSignalQualityStore } from '../stores/signalQuality'
import { qualityText } from '../utils/signalQuality'
const props = defineProps<{ quality?: SignalQualityDto | null; oldScore: number; rr?: number | null }>()
const display = useSignalQualityStore()
const ready = computed(() => props.quality?.status === 'ok')
const metrics: Record<string, string> = {
  ma20_slope_pre:'原有均线方向',ma20_slope_a:'A段均线方向',ma20_slope_warning:'预警时均线方向',warning_close_ma_atr:'预警收盘距均线ATR',ma_residence:'均线有利侧驻留比例',a_ma_crosses:'A段穿线次数',b_ma_crosses:'B段穿线次数',a_core_efficiency:'A段内部路径效率',a_full_efficiency:'完整A段路径效率',a_drawdown_ratio:'A段内部回撤比例',a_stall_fraction:'最长停滞比例',a_stall_bars:'最长停滞根数',a_terminal_stall_bars:'末端停滞根数',a_overlap_stagnation:'重叠争夺程度',a_net_move:'A段净幅度',a_move_atr:'A段净幅度ATR',atr_s1:'ATR20(S1)',a_speed_atr:'旧A段速度ATR/根',a_gap_sum:'大跳空合计',a_gap_count:'大跳空次数',a_gap_penalty:'旧跳空扣分',retracement:'回撤比例',b_a_speed_ratio:'B/A速度比',b_decay:'排除预警后的实体衰减',ma_departure_atr:'A段离开均线ATR',ma_proximity_atr:'回撤接近均线ATR',ma_damage_atr:'均线不利侧平均深度ATR',warning_improvement_atr:'预警顺向改善ATR',warning_close_location:'预警收盘有利位置',ma_retest:'均线回踩确认系数',rr_to_s1:'到S1的空间R',legacy_a_q:'旧逐K干净度',legacy_a_big_reverse_count:'旧A段大反向K数量',legacy_a_score:'旧A段重算',legacy_b_score:'旧B段重算',legacy_warning_score:'旧预警重算',
}
const partNames: Record<string,string[]> = {T:['原有背景','A段趋势','驻留与少穿线'],A:['路径效率','进展保护','停滞','重叠争夺'],B:['回撤深度','相对速度','末端衰减','均线回踩确认']}
function label(key:string):string {
  if (metrics[key]) return metrics[key]
  const m=key.match(/^([TAB])_part_(\d+)_(score|weight)$/)
  return m ? `${partNames[m[1]]?.[Number(m[2])-1] ?? key}${m[3]==='score'?'分':'权重'}` : key
}
</script>
<template>
  <details v-if="display.enabled" class="signal-quality" @click.stop>
    <summary>
      <span>旧入场 {{ qualityText(oldScore, 2) }}</span>
      <template v-if="ready"><span>形态 <b>{{ qualityText(quality?.morphology_score) }}</b></span><span>综合 <b>{{ qualityText(quality?.quality_score) }}</b></span></template>
      <span v-else>{{ quality ? '数据不足 · 展开查看' : '新评分未计算' }}</span>
      <span v-if="rr != null">到S1 {{ qualityText(rr, 2) }}R</span>
    </summary>
    <div class="quality-detail">
      <template v-if="ready">
        <div class="quality-parts"><span>趋势 {{ qualityText(quality?.trend_score) }}</span><span>A段 {{ qualityText(quality?.a_score) }}</span><span>B段 {{ qualityText(quality?.b_score) }}</span><span>空间 {{ qualityText(quality?.space_score) }}</span></div>
        <div v-for="c in quality?.contributions" :key="c.name" class="quality-contribution">{{ c.name }} {{ qualityText(c.score) }} × {{ (c.weight * 100).toFixed(0) }}% → {{ qualityText(c.contribution) }}</div>
      </template>
      <ul><li v-for="reason in quality?.explanations" :key="reason">{{ reason }}</li></ul>
      <p v-if="!quality">历史记录可在隔离副本重算；旧分继续有效。</p>
      <p v-if="quality">{{ quality.origin === 'live_frozen' ? '预警时冻结' : '历史快照重算' }} · {{ quality.rules_version }} · {{ quality.status }}</p>
      <p>新分采用0～100分；仅用于形态比较，交易仍使用旧入场分。</p>
      <details v-if="quality"><summary>原始因子与权重</summary><dl><template v-for="(value, key) in quality.features" :key="key"><dt>{{ label(String(key)) }}</dt><dd>{{ qualityText(value, 3) }}</dd></template></dl><p class="hash">参数 {{ quality.parameter_hash }}<br>行情 {{ quality.input_hash }}</p></details>
    </div>
  </details>
</template>
<style scoped>
.signal-quality{margin:8px 0;font-size:11px;color:#475569;line-height:1.6;max-width:100%;font-weight:400}.signal-quality>summary{cursor:pointer;display:flex;gap:9px;flex-wrap:wrap;border:1px solid #dce5f0;border-radius:6px;padding:6px 8px;background:#f7f9fd}.signal-quality b{color:#395b9d}.quality-detail{padding:10px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px}.quality-parts{display:flex;gap:12px;flex-wrap:wrap}.quality-contribution{font-variant-numeric:tabular-nums}ul{padding-left:18px}p{margin:6px 0;color:#64748b}dl{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:3px 10px}dt,dd{margin:0}.hash{word-break:break-all;font-size:10px}
</style>
