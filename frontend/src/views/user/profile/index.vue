<script setup>
/**
 * 个人中心。
 *
 * - 显示 /api/auth/me 的全部字段，重点 has_key 与 session_key 分开显示。
 * - 改自己的口令：**在浏览器本地重封私钥**，再把密文交给服务端登记。
 *   （默认模型下服务端不持有私钥 —— 安全审计 I8。）
 * - 「刷新」→ /api/auth/me。
 * - 「退出登录」：先跳登录页，再弹后端返回的 notice。
 */
import { ref, reactive, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '../../../stores/auth'
import { useCryptoStore } from '../../../stores/crypto'
import { usersApi } from '../../../api/users'
import PageHeader from '../../../components/common/PageHeader.vue'
import KeyStatusTag from '../../../components/security/KeyStatusTag.vue'
import PubKeyFingerprint from '../../../components/security/PubKeyFingerprint.vue'
import StageTimeline from '../../../components/security/StageTimeline.vue'

const auth = useAuthStore()
/** 浏览器侧的私钥状态（私钥只活在内存，见 stores/crypto.js）。 */
const crypt = useCryptoStore()
const router = useRouter()

const loading = ref(false)
const pwdForm = reactive({ password: '', confirm: '' })
const changing = ref(false)
const changeTimings = ref(null)
/** 改口令的端到端耗时（毫秒，含浏览器本地重封私钥的 PBKDF2 耗时）。 */
const changeElapsed = ref(0)

const user = computed(() => auth.user)

/* ---------------------------------------------------------------------------
   SM2 密钥对展示
   ---------------------------------------------------------------------------
   私钥（`crypt.key`）是登录时在**本机内存**里用口令解出来的标量，
   后端从不持有明文（见 stores/crypto.js 的三条纪律）。所以这里展示的私钥
   只来自浏览器内存 —— 没解锁就看不到，刷新/退出后也消失。
   公钥在库里存的是 65 字节未压缩点（0x04 ‖ x ‖ y）的十六进制（130 字符）。
--------------------------------------------------------------------------- */
const skBig = computed(() => crypt.key)
const skHex = computed(() =>
  skBig.value != null ? skBig.value.toString(16).padStart(64, '0') : '',
)
const skDecimal = computed(() => (skBig.value != null ? skBig.value.toString() : ''))

const pubHex = computed(() => user.value?.pub_key || '')
const pubX = computed(() => (pubHex.value.length >= 130 ? pubHex.value.slice(2, 66) : ''))
const pubY = computed(() => (pubHex.value.length >= 130 ? pubHex.value.slice(66, 130) : ''))

/** 私钥默认隐藏，点「查看」才显示（避免误植屏）。 */
const revealSk = ref(false)

function copyText(text, label = '已复制') {
  if (!text) return
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).then(
      () => ElMessage.success(label),
      () => ElMessage.warning('复制失败，请手动选择文本复制'),
    )
  } else {
    ElMessage.warning('当前环境不支持自动复制，请手动选择文本复制')
  }
}

async function refresh() {
  loading.value = true
  try {
    await auth.fetchMe()
    ElMessage.success('已刷新')
  } catch {
    // 401 已由拦截器兜底
  } finally {
    loading.value = false
  }
}

async function changePassword() {
  // ★ 默认模型下后端**不持有私钥**（安全审计 I8），所以这里不能再要求
  //   ``session_key`` —— 那个字段在默认模型下恒为 false，以前会直接卡死。
  //   改口令改为两步：① 在**本地**用新口令把私钥重新封装；
  //   ② 把密文连同新口令交给后端，后端只验“解得开 + 私钥还是原来那把”。
  //   私钥本体不变 ⇒ 已上传的文件一把都不用重传。
  if (!crypt.unlocked) {
    ElMessage.warning('浏览器里还没有私钥 —— 请重新登录一次（登录时用口令在本地解封）再改口令')
    return
  }
  if (!pwdForm.password) {
    ElMessage.warning('请输入新口令')
    return
  }
  if (pwdForm.password !== pwdForm.confirm) {
    ElMessage.warning('两次口令不一致')
    return
  }
  changing.value = true
  changeTimings.value = null
  // ★ 端到端：本地重封私钥（PBKDF2-HMAC-SM3 二十万次）也算在内 ——
  //   它确实是“改口令”这件事里用户等的一部分。
  const t0 = performance.now()
  try {
    let skWrapped
    try {
      skWrapped = crypt.rewrap(pwdForm.password)
    } catch (e) {
      ElMessage.error(e?.message || '本地重封私钥失败')
      return
    }
    const { data } = await usersApi.patch(user.value.id, {
      password: pwdForm.password,
      sk_wrapped: skWrapped,
    })
    changeTimings.value = data.timings
    changeElapsed.value = Math.round(performance.now() - t0)
    pwdForm.password = ''
    pwdForm.confirm = ''
    // ★★ 改口令 = 后端**撤销这个人全部已签发的令牌**（正确的止损设计：
    //    口令变了，旧令牌就不该再能用）。但这意味着**手里这张立刻失效** ——
    //    以前这里只弹一句“成功”就留在原页，用户下一次点击才吃 401，
    //    然后被兜底处理器跳登录页并显示“上次的登录状态已失效”，
    //    给出一个**错的解释**（真实原因是“你自己刚改了口令”）。
    //    所以这里主动清会话并跳登录页，把原因说清楚。
    ElMessage.success(
      '口令已更改（私钥在你的浏览器里重新封装，文件不用重传）。' +
        '出于安全，改口令会注销所有已登录的会话 —— 请用新口令重新登录。',
    )
    auth.clear()
    router.replace('/login')
  } catch {
    // 409 / 400 已由拦截器弹出
  } finally {
    changing.value = false
  }
}

async function logout() {
  try {
    await auth.logout()
    router.replace('/login')
  } catch {
    auth.clear()
    router.replace('/login')
  }
}

onMounted(() => {
  if (!user.value) auth.fetchMe().catch(() => {})
})
</script>

<template>
  <div>
    <PageHeader title="个人中心">
      <div class="actions">
        <el-button :loading="loading" @click="refresh">刷新</el-button>
        <el-button type="danger" @click="logout">退出登录</el-button>
      </div>
    </PageHeader>

    <div class="panel mb-3" v-if="user">
      <h4 class="sec-title">账户信息</h4>
      <div class="kv">
        <div class="kv-row"><span class="k">用户名</span><span class="mono">{{ user.username }}</span></div>
        <div class="kv-row"><span class="k">显示名</span><span>{{ user.display_name }}</span></div>
        <div class="kv-row"><span class="k">角色</span><span>{{ user.role === 'admin' ? '管理员' : '普通用户' }}</span></div>
        <div class="kv-row"><span class="k">密钥对</span><KeyStatusTag :user="user" /></div>
        <div class="kv-row"><span class="k">公钥指纹</span><PubKeyFingerprint :pub-key="user.pub_key" /></div>
      </div>
      <!-- ★ 判据是**浏览器里有没有私钥**（``crypt.unlocked``），不是
           ``user.session_key``：默认模型下后端不持有私钥、那个字段恒为 false，
           拿它当条件会让这一条警告**永远挂着** —— 明明状态栏写着“已解锁 ·
           私钥在本浏览器”，下面却说“没有私钥”。自相矛盾（安全审计 I8 配套修正）。 -->
      <el-alert
        v-if="!crypt.unlocked"
        type="warning"
        :closable="false"
        class="mt-3"
        title="这次会话里没有私钥"
        description="重新登录就能解密。上传与验证不受影响。"
      />
    </div>

    <div class="panel mb-3" v-if="user">
      <h4 class="sec-title">SM2 密钥对</h4>
      <el-alert
        type="info"
        :closable="false"
        class="mb-3"
        title="私钥仅限本次登录使用"
        description="私钥只保存在当前浏览器中，用您的口令解锁。闲置 5 分钟或退出登录后会自动清除，再次查看需重新登录"
      />
      <div class="kv">
        <div class="kv-row">
          <span class="k">公钥（完整）</span>
          <span class="mono key-cell">
            <span v-if="pubHex">{{ pubHex }}</span><span v-else>—</span>
            <el-button v-if="pubHex" size="small" text type="primary" @click="copyText(pubHex, '公钥已复制')">复制</el-button>
          </span>
        </div>
        <div class="kv-row"><span class="k">公钥 Qx</span><span class="mono key-cell">{{ pubX || '—' }}</span></div>
        <div class="kv-row"><span class="k">公钥 Qy</span><span class="mono key-cell">{{ pubY || '—' }}</span></div>

        <template v-if="crypt.unlocked && skHex">
          <div class="kv-row">
            <span class="k" style="white-space: nowrap; flex-shrink: 0;">私钥 d（SM2）</span>
            <span class="mono key-cell">
              <el-button size="small" :type="revealSk ? 'info' : 'primary'" @click="revealSk = !revealSk">
                {{ revealSk ? '隐藏' : '查看' }}
              </el-button>
              <span v-if="revealSk" class="sk-value">{{ skHex }}</span>
              <el-button v-if="revealSk" size="small" text type="primary" @click="copyText(skHex, '私钥已复制')">复制</el-button>
            </span>
          </div>
          <div class="kv-row" v-if="revealSk">
            <span class="k" style="white-space: nowrap; flex-shrink: 0;">私钥（十进制）</span>
            <span class="mono key-cell sk-decimal" style="flex: 1; word-break: break-all;">{{ skDecimal }}</span>
          </div>
        </template>
        <div class="kv-row" v-else>
          <span class="k">私钥 d（SM2）</span>
          <span>—（未解锁；请重新登录以在本地解封后查看）</span>
        </div>
      </div>
    </div>

    <div class="panel">
      <h4 class="sec-title">修改口令</h4>
      <el-form label-width="90px" class="pwd-form" @submit.prevent>
        <el-form-item label="新口令">
          <el-input v-model="pwdForm.password" type="password" show-password />
        </el-form-item>
        <el-form-item label="确认口令">
          <el-input v-model="pwdForm.confirm" type="password" show-password />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="changing" @click="changePassword">修改口令</el-button>
        </el-form-item>
      </el-form>
      <StageTimeline v-if="changeTimings" :timings="changeTimings" :total-ms="changeElapsed" class="mt-3" />
      <details class="note-collapse">
        <summary>说明</summary>
        <p class="note">修改口令会重新生成并保存密钥</p>
      </details>
    </div>
  </div>
</template>

<style scoped>
.sec-title {
  font-size: 17px;
  font-weight: 600;
  color: var(--text-1);
  margin-bottom: 14px;
}
.actions {
  display: flex;
  gap: 8px;
}
.kv {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.kv-row {
  display: flex;
  gap: 12px;
  align-items: baseline;
  font-size: 13px;
}
.kv-row .k {
  color: var(--text-3);
  width: 90px;
  flex-shrink: 0;
}
.key-cell {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  word-break: break-all;
}
.sk-value {
  color: #43e4bf;
  word-break: break-all;
}
.sk-decimal {
  word-break: break-all;
}
.pwd-form {
  max-width: 420px;
}
.note {
  font-size: 13px;
  color: var(--text-3);
}
</style>
