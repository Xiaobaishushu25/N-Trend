<script setup lang="ts">
import { ref } from 'vue'
import type { SimnowLoginInput } from '../types'

const model = defineModel<SimnowLoginInput>({ required: true })
defineProps<{ saved?: boolean; lockUser?: boolean }>()

const showPassword = ref(false)

function applyPreset(preset: 'simnow' | 'gtja') {
  if (preset === 'simnow') {
    model.value.broker_id = '9999'
    model.value.td_front = 'tcp://182.254.243.31:30001'
    model.value.md_front = 'tcp://182.254.243.31:30011'
    model.value.app_id = 'simnow_client_test'
    model.value.auth_code = '0000000000000000'
  } else if (preset === 'gtja') {
    model.value.broker_id = '2071'
    model.value.td_front = 'tcp://114.94.128.1:42205'
    model.value.md_front = 'tcp://114.94.128.1:42213'
    model.value.app_id = 'client_ntrend_3.0.1'
    model.value.auth_code = '20261028PANGJF01'
  }
}
</script>

<template>
  <div class="simnow-fields">
    <div v-if="!lockUser" class="preset-row">
      <span class="preset-label">快捷填入环境:</span>
      <button
        type="button"
        class="preset-chip"
        :class="{ active: model.broker_id === '9999' }"
        @click="applyPreset('simnow')"
      >
        SimNow 官方 (9999)
      </button>
      <button
        type="button"
        class="preset-chip"
        :class="{ active: model.broker_id === '2071' }"
        @click="applyPreset('gtja')"
      >
        国泰君安仿真 (2071)
      </button>
    </div>

    <label>
      券商编号 (BrokerID)
      <input
        v-model="model.broker_id"
        :readonly="lockUser"
        inputmode="numeric"
        autocomplete="off"
        pattern="[0-9]+"
        maxlength="16"
        placeholder="如 9999 或 2071"
        required
      >
    </label>

    <label>
      投资者账号 (UserID)
      <input
        v-model="model.user_id"
        :readonly="lockUser"
        inputmode="numeric"
        autocomplete="off"
        pattern="[0-9]+"
        maxlength="16"
        placeholder="期货资金账号"
        required
      >
    </label>

    <label>
      交易密码
      <div class="password-wrapper">
        <input
          v-model="model.password"
          :type="showPassword ? 'text' : 'password'"
          autocomplete="new-password"
          :required="!saved"
          maxlength="40"
          :placeholder="saved ? '留空沿用已保存密码' : '填写交易密码'"
        >
        <button
          type="button"
          class="pwd-toggle-btn"
          :title="showPassword ? '隐藏密码' : '显示密码'"
          @click="showPassword = !showPassword"
        >
          {{ showPassword ? '👁' : '🔒' }}
        </button>
      </div>
    </label>

    <label>
      交易前置 (TdFront)
      <input
        v-model="model.td_front"
        autocomplete="off"
        placeholder="tcp://主机:端口"
        required
      >
    </label>

    <label>
      行情前置 (MdFront，选填)
      <input
        v-model="model.md_front"
        autocomplete="off"
        placeholder="tcp://主机:端口（留空沿用系统行情）"
      >
    </label>

    <label>
      AppID (穿透式应用代码)
      <input
        v-model="model.app_id"
        autocomplete="off"
        maxlength="32"
        placeholder="如 client_ntrend_3.0.1"
        required
      >
    </label>

    <label>
      认证码 (AuthCode)
      <input
        v-model="model.auth_code"
        type="password"
        autocomplete="new-password"
        maxlength="32"
        :required="!saved"
        :placeholder="saved ? '留空沿用已保存认证码' : '填写穿透式授权码'"
      >
    </label>

    <p class="hint">
      支持任意 CTP 仿真及实盘柜台。填写完毕后，系统将自动在服务端凭据目录生成安全凭据文件并立即登录建立专有通信桥，无需手动创建文件。
    </p>
  </div>
</template>

<style scoped>
.simnow-fields { display: flex; flex-direction: column; gap: 13px; }
.preset-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  background: #f8fafc;
  border-radius: 6px;
  border: 1px dashed #cbd5e1;
}
.preset-label { font-size: 12px; color: #64748b; font-weight: 500; }
.preset-chip {
  background: white;
  border: 1px solid #cbd5e1;
  border-radius: 4px;
  padding: 3px 8px;
  font-size: 12px;
  color: #334155;
  cursor: pointer;
  transition: all 0.15s ease;
}
.preset-chip:hover {
  background: #eff6ff;
  border-color: #93c5fd;
  color: #1d4ed8;
}
.preset-chip.active {
  background: #2563eb;
  border-color: #2563eb;
  color: white;
  font-weight: 500;
}
label { display: flex; justify-content: space-between; align-items: center; gap: 12px; font-size: 13px; color: #334155; }
input { width: 65%; min-width: 0; box-sizing: border-box; color: #1e293b; background: white; border: 1px solid #cbd5e1; border-radius: 6px; padding: 8px; font-size: 13px; }
input:read-only { background: #f1f5f9; color: #64748b; }
.password-wrapper { width: 65%; display: flex; position: relative; }
.password-wrapper input { width: 100%; padding-right: 36px; }
.pwd-toggle-btn {
  position: absolute;
  right: 6px;
  top: 50%;
  transform: translateY(-50%);
  background: none;
  border: none;
  cursor: pointer;
  padding: 4px 6px;
  font-size: 14px;
  opacity: 0.6;
}
.pwd-toggle-btn:hover { opacity: 1; }
.hint { font-size: 12px; line-height: 1.6; margin: 2px 0 0; color: #64748b; }
</style>
