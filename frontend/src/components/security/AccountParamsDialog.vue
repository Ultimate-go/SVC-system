<script setup>
/**
 * 本账号的加密参数 —— 新账号**第一次**进入系统时显示一次。
 *
 * 为什么要这一步
 * --------------
 * 注册时服务端就把 SM2 密钥对生成好了（`mgr.ensure_user_key`），公钥此后
 * 固定不变、而且它就是「谁能解开我上传的文件」的答案。这些参数只在别处
 * 零散出现过（个人中心、文件详情），第一次用的人往往不知道它们从哪来。
 * 所以这里在账号第一次进入系统时集中讲一遍，之后不再打扰 ——
 * 去重记录见 `utils/accountIntro.js`。
 *
 * 为什么**不**显示私钥
 * --------------------
 * 后端从不返回私钥材料（`user_public` 里只有公钥），明文私钥只活在浏览器
 * 内存里（`stores/crypto.js`）。要完整查看（含手动揭示私钥）走「个人中心」——
 * 那里有显式的一次性揭示开关。这里把「私钥放在哪」讲清楚就够了。
 *
 * 关闭即算「已看过」：走 `@closed`，所以点按钮、按 Esc、点右上角 ×
 * 三条路都会落地，不存在"关掉了但下次又弹"。
 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import KeyStatusTag from './KeyStatusTag.vue'
import PubKeyFingerprint from './PubKeyFingerprint.vue'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  /** 当前登录用户（`auth.user` 的形状）。 */
  user: { type: Object, default: null },
})

const emit = defineEmits(['update:modelValue', 'seen'])

const router = useRouter()

const pubHex = computed(() => String(props.user?.pub_key || ''))
/** 未压缩公钥是 `04 ‖ x(32B) ‖ y(32B)` 的十六进制（2 + 64 + 64 个字符）。 */
const coords = computed(() => {
  const h = pubHex.value
  if (h.length < 130) return null
  return { x: h.slice(2, 66), y: h.slice(66, 130) }
})

function goProfile() {
  emit('update:modelValue', false)
  router.push('/profile')
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="本账号的系统参数"
    width="min(760px, 94vw)"
    class="acct-params-dialog"
    append-to-body
    @update:model-value="emit('update:modelValue', $event)"
    @closed="emit('seen')"
  >
    <p class="lead text-2">
      这是本账号第一次进入系统。下面这些参数在注册时由服务端生成，此后不再改变；
      以后可以在「个人中心」随时查看。
    </p>

    <h5 class="sec">账号</h5>
    <div class="kv">
      <div class="row"><span class="k text-3">用户名</span><span class="mono">{{ user?.username || '—' }}</span></div>
      <div class="row"><span class="k text-3">显示名</span><span>{{ user?.display_name || user?.username || '—' }}</span></div>
      <div class="row">
        <span class="k text-3">角色</span>
        <span>{{ user?.role === 'admin' ? '管理员' : '普通用户' }}</span>
      </div>
    </div>

    <h5 class="sec">SM2 密钥对</h5>
    <div class="kv">
      <div class="row"><span class="k text-3">密钥对状态</span><KeyStatusTag :user="user" /></div>
      <div class="row">
        <span class="k text-3">私钥存放</span>
        <span>以口令封装的形式存在服务端，明文私钥只在本浏览器内存里，登录时用口令解封</span>
      </div>
      <div class="row">
        <span class="k text-3">公钥指纹</span>
        <PubKeyFingerprint :pub-key="pubHex" />
      </div>
      <template v-if="coords">
        <div class="row"><span class="k text-3">公钥 X 坐标</span><span class="mono break">{{ coords.x }}</span></div>
        <div class="row"><span class="k text-3">公钥 Y 坐标</span><span class="mono break">{{ coords.y }}</span></div>
      </template>
      <div class="row" v-else>
        <span class="k text-3">公钥</span><span class="text-3">本账号还没有公钥，请联系管理员</span>
      </div>
      <details v-if="pubHex" class="note-collapse">
        <summary>完整公钥</summary>
        <p class="mono break">{{ pubHex }}</p>
      </details>
    </div>

    <ul class="notes">
      <li>上传文件时，每个分块的 SM4 密钥都用本账号的公钥封装；只有本账号的私钥可以解密。</li>
      <li>浏览器的解锁与解密都用本账号的口令完成，服务端不持有私钥。</li>
      <li>可验证查询不受账号限制：任何登录用户都能验证任意文件的承诺。</li>
    </ul>

    <p class="warn-line">
      口令用于解封私钥。口令遗失后，本账号已上传的文件无法解密。
    </p>

    <template #footer>
      <el-button @click="goProfile">去个人中心查看</el-button>
      <el-button type="primary" @click="emit('update:modelValue', false)">已了解</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.lead {
  margin: 0 0 4px;
  font-size: 13px;
  line-height: 1.7;
}
.sec {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-1);
  margin: 18px 0 10px;
}
.sec:first-of-type {
  margin-top: 14px;
}
.kv {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.row {
  display: flex;
  gap: 12px;
  align-items: baseline;
  font-size: 13px;
  line-height: 1.6;
}
.row .k {
  width: 96px;
  flex: none;
}
.break {
  word-break: break-all;
}
.notes {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.8;
  color: var(--text-2);
}
.warn-line {
  margin: 14px 0 0;
  font-size: 13px;
  line-height: 1.7;
  color: var(--warn);
}
</style>
