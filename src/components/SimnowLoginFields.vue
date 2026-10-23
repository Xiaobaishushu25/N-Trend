<script setup lang="ts">
import type { SimnowLoginInput } from '../types'
const model = defineModel<SimnowLoginInput>({ required: true })
defineProps<{ saved?: boolean; lockUser?: boolean }>()
</script>

<template>
  <div class="simnow-fields">
    <label>SimNow 投资者账号 <input v-model="model.user_id" :readonly="lockUser" inputmode="numeric" autocomplete="off" pattern="[0-9]+" maxlength="16" required></label>
    <label>交易密码 <input v-model="model.password" type="password" autocomplete="new-password" :required="!saved" maxlength="40" :placeholder="saved ? '留空沿用已保存密码' : '填写交易密码'"></label>
    <label>交易前置 <input v-model="model.td_front" autocomplete="off" placeholder="tcp://主机:端口" required></label>
    <label>AppID <input v-model="model.app_id" autocomplete="off" maxlength="32" required></label>
    <label>认证码 <input v-model="model.auth_code" type="password" autocomplete="new-password" maxlength="16" :required="!saved" :placeholder="saved ? '留空沿用已保存认证码' : '填写认证码'"></label>
    <p class="hint">券商编号为 9999。交易前置可按 SimNow 当前可用环境修改；行情继续使用系统共享行情。凭据保存在服务端，登录成功后仍需单独启用策略。</p>
    <a href="https://www.simnow.com.cn/" target="_blank" rel="noopener noreferrer">SimNow 官网：注册、激活账号及查询前置</a>
  </div>
</template>

<style scoped>
.simnow-fields { display: flex; flex-direction: column; gap: 14px; }
label { display: flex; justify-content: space-between; align-items: center; gap: 12px; font-size: 13px; }
input { width: 65%; min-width: 0; box-sizing: border-box; color: #1e293b; background: white; border: 1px solid #cbd5e1; border-radius: 6px; padding: 8px; }
input:read-only { background: #f1f5f9; }
.hint, a { font-size: 12px; line-height: 1.6; margin: 0; } .hint { color: #64748b; } a { color: #2563eb; }
</style>
