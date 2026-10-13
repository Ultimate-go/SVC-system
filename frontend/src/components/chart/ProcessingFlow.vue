<script setup>
/**
 * Processing 数据处理链路：用六个语义化 SVG 节点表达从上传到验证的闭环。
 * 动画分成两层：数据包沿轨道顺序传递，当前阶段卡片周期性扫描，
 * 让动效看起来像真实的数据处理，而不是同时闪烁的装饰。
 *
 * 六张卡片都可点开：弹出的原理说明见下方 PRINCIPLES（内容取自作品报告第二章）。
 */
import { computed, ref } from 'vue'

const STAGES = [
  { key: 'upload', label: '上传', en: 'INGEST', detail: '接收原始文件', tone: 'cyan' },
  { key: 'split', label: '分片', en: 'CHUNK', detail: '切成固定大小的块', tone: 'blue' },
  { key: 'commit', label: '承诺生成', en: 'COMMIT', detail: '计算摘要与证明', tone: 'violet' },
  { key: 'store', label: '分布式存储', en: 'REPLICATE', detail: '写入节点并创建副本', tone: 'green' },
  { key: 'query', label: '查询取证', en: 'PROVE', detail: '按指定块取证', tone: 'amber' },
  { key: 'verify', label: '完整性验证', en: 'VERIFY', detail: '公开验证完整性', tone: 'rose' },
]

/**
 * 六个阶段各自的密码学原理（点开卡片时弹出）。
 *
 * 内容取自作品报告第二章「基础知识」，每条都标出对应小节，便于与原文逐句对照；
 * 每个阶段只保留与它直接相关的那几件事，不做百科式铺陈。
 */
const PRINCIPLES = {
  upload: {
    headline: '按段加密：机密性由 SM4-CTR 提供，完整性由向量承诺承担',
    blocks: [
      { t: '分组密码', d: 'SM4 的分组长度与密钥长度均为 128 位，采用 32 轮非平衡 Feistel 结构，轮函数由非线性变换 τ（逐字节过 8 位 S 盒）与线性变换 L 复合而成（GB/T 32907—2016）。' },
      { t: '工作模式的选择（CTR）', d: 'CBC 具有错误传播：一段被改动会破坏其后所有分段。本系统的核心操作是仅替换指定分块、其余分块保持不变；若采用带传播性的模式，改写一个分块将导致其后全部分段重算，改写与追加的代价不再与改动量成正比。CTR 各段相互独立，任意一段均可单独解密，这是随机访问与增量更新的前提。' },
      { t: '每段一把密钥、IV 不复用', d: '每个分块用一把即时生成的 16 字节密钥独立加密，目的是按段隔离：某一块出问题只影响该块；代价是每段都要单独保护自己的密钥，这正是下一阶段用 SM2 逐块封装块密钥的原因。CTR 下 IV 复用等价于同一密钥下重复使用计数器序列，两段明文异或后会泄露信息，因此每次加密都必须换新的随机 IV。' },
      { t: '边界', d: '纯 CTR 仅保证机密性，不提供完整性：被篡改的密文仍能解出内容。本方案的完整性由向量承诺承担：验证方自行从收到的密文重算分量，能通过承诺验证即说明密文未被改动，因此无需再叠加认证加密层。' },
    ],
    formulas: ['c_j = m_j ⊕ E_k(ctr_j)　　（第 j 段密文；k 为该段独立密钥）'],
    refs: ['报告 2.7.2　SM4 分组密码算法与 CTR 模式'],
  },
  split: {
    headline: '定长分段：每个分块对应向量上的一个位置',
    blocks: [
      { t: '位置即素数', d: '向量下标 i 由素数映射 PrimeGen 对应到一个素数 e_i，同时要求三件事：确定性（同一个 i 永远得到同一个 e_i，否则系统重启后无法复现承诺）、无碰撞、位长恰为 ℓ+1 位。' },
      { t: '互异性是正确性的前提', d: '若 e_i = e_j，则 gcd(e_i, e_j) ≠ 1；此时 Shamir 技巧解出的 Bézout 系数满足 a·x + b·y = d > 1，代回后不报错，会静默给出错误结果 —— 这类缺陷最难排查。' },
      { t: '实现纪律', d: '不满足互素前提的调用一律显式返回空值或抛错，绝不静默给出计算结果。' },
      { t: '素数表的生成与缓存', d: '素数表在系统引导阶段一次性生成，容量为 n_max，并在全局会话中缓存；若每次上传都重新生成，实测会额外增加约 1.4 秒。' },
    ],
    formulas: ['i ↦ e_i，|e_i| = ℓ + 1', 'gcd(e_i, e_j) = 1　(i ≠ j)'],
    refs: ['报告 2.1.4　素数映射'],
  },
  commit: {
    headline: '把整条向量压成两个群元素，并为每个位置留下可公开计算的见证',
    blocks: [
      { t: '被承诺的对象', d: '承诺的对象是密文段的 SM3 摘要 v_i = SM3(c_i)。由此每个位置仅需 32 字节即可参与承诺，与分块大小无关；代价是额外依赖 SM3 的抗碰撞性 —— 若两段不同密文能算出同一个 v_i，「分量与承诺相符」便不再等价于「密文未被篡改」。' },
      { t: '累加器与整除性', d: 'RSA 累加器把集合压成一个群元素 U_n = g^{e_[n]}；位置 i 的见证 S_i = g^{e_[n]/e_i} 是一条普通模幂 —— 因为 e_i 整除 e_[n]，指数是整数，不需要求任何方根。正是这种整除性使方案能够公开全部参数与见证。' },
      { t: '承诺', d: '在累加器之上再叠一层线性组合，把位置 i 的值 v_i 作为见证 S_i 的指数权重：C = ∏ S_i^{v_i}。向量中任意位置的任何改动都会改变 C，而 C 自始至终只是一个群元素。' },
      { t: '批量分解', d: '承诺阶段需一次产出全部 S_i。逐个处理需要 m 次大数除法与 m 次模幂；改用分治分解后只需一次分解即可获得全部结果，n 较大时差异显著。' },
    ],
    formulas: ['v_i = SM3(c_i)', 'U_n = g^{e_1 e_2 ⋯ e_n} mod N', 'C = ∏_{i=1}^{n} S_i^{v_i} mod N'],
    refs: ['报告 2.1.2　RSA 累加器', '报告 2.1.5　Shamir 技巧与批量分解', '报告 2.7.1　SM3'],
  },
  store: {
    headline: '节点各自持有向量的一段，公开参数只有群描述与素数映射',
    blocks: [
      { t: '群阶未知是安全性的来源', d: '隐藏阶群 G = (N = p·q, g) 的阶 φ(N) 在输出中不可得。敌手若获知 φ(N)，即可对任意整数求 e 次根、伪造任意位置的见证，方案随即失效。实现上在生成群描述之后立即销毁 p 与 q，序列化层只导出 N、g 与素数映射。' },
      { t: '群除法：由群阶未知决定', d: '聚合时需要除去交叉项，实现为群元素相除 C / C′。这一处极易被误写成指数相减：指数上的运算必须在整数层完成，不能在模群阶的意义下做减法，因为群阶本身未知。' },
      { t: 'VDS 模型', d: '客户端只持有一个常数大小的摘要 δ = ((U, C), n)，各存储节点各持向量的一个片段；双方通过更新协议（PushUpdate / ApplyUpdate）与检索协议交互，共同完成更新与检索两类操作。' },
      { t: '全系统一条向量', d: '本实现把全部用户的文件按上传顺序追加进同一条全局向量，因此一次检索得到的证据天然覆盖多个文件与多个用户 —— 这是「全系统一条向量」这一设计决策带来的能力。' },
    ],
    formulas: ['δ = ((U, C), n)', 'G = (N, g)，N = p · q，φ(N) 不可得'],
    refs: ['报告 2.1.1　隐藏阶群', '报告 2.6　可验证去中心化存储'],
  },
  query: {
    headline: '取出若干块，并附上一份恒为两个群元素的证据',
    blocks: [
      { t: '子向量打开', d: '对位置子集 I 生成证据 π_I = (S_I, Λ_I)，仅由两个群元素构成；承诺与证据的大小只与安全参数有关，不随向量长度 n 或打开位置个数 |I| 增长（简洁性）。' },
      { t: '可聚合', d: '同一承诺下两个不相交集合 I、J 的证据可合并为覆盖 I ∪ J 的一份证据，大小仍是两个群元素；合并次数不受限制，顺序任意，结果相同。' },
      { t: '增量性', d: '随长度变化的公开量从「公开参数」转移到「摘要」，公开参数只需生成一次，此后的增删与更新只影响摘要中的两个分量。' },
      { t: '合并的代数基础：Shamir 技巧', d: '已知 g 的 1/x 次根与 1/y 次根（x、y 互素），由扩展欧几里得求出 a·x + b·y = 1，即可合成 1/(xy) 次根。实现内置两道自检：同源（两个根必须是同一个 g 的根）与互素（gcd(x, y) = 1），缺一则拒绝合并，被篡改的证据拼不出一份能通过验证的证据。' },
    ],
    formulas: ['π_I = (S_I, Λ_I)，|π_I| = 2 个群元素', 'S_I = g^{e_[n] / e_I} mod N', 'root = root_x^{b} · root_y^{a} mod N，　a x + b y = 1'],
    refs: ['报告 2.2　向量承诺与子向量打开', '报告 2.4　增量聚合', '报告 2.1.5　Shamir 技巧'],
  },
  verify: {
    headline: '两条检查相互配合，零信任地判定内容未被篡改',
    blocks: [
      { t: '检查①：结构合法性', d: 'S_I^{e_I} = U_n。该检查把 S_I 唯一确定为 U_n 的 e_I 次根，不涉及任何数据值。' },
      { t: '检查②：内容一致性', d: 'C = Λ_I^{e_I} · ∏_{i∈I} S_i^{v_i}。它把承诺 C、声明的值 v_I 与证据 Λ_I 相互关联。' },
      { t: '两者缺一不可', d: '只保留②，敌手可任意选取 S_I 来凑等式；只保留①，等式右端可被随意替换。① 之所以站得住，依赖低阶假设 —— 敌手无法用一个低阶元素同时满足两条检查，而这样的元素并不对应真实的向量。' },
      { t: '位置绑定', d: '承诺者无法对同一位置 i 给出两份都能通过验证、但取值不同的打开结果，因此节点无法事后否认它保存过的内容。其安全性归约到强 RSA 假设；在动态更新场景下还需更强的 Strong DPR 假设，以排除「通过多次更新累积出一次非法开方」的路径。' },
      { t: '零信任与常数大小', d: '客户端仅需保存一个常数大小的摘要，即可验证聚合后的那一份证据，从而确认取回的块均未被篡改，无需信任任何一台服务器。' },
    ],
    formulas: ['S_I^{e_I} = U_n mod N', 'C = Λ_I^{e_I} · ∏_{i∈I} S_i^{v_i} mod N'],
    refs: ['报告 2.3　数学困难问题（低阶假设 / 自适应根假设）', '报告 2.5　位置绑定'],
  },
}

/** 当前打开的阶段（null = 弹框关闭）。 */
const openKey = ref(null)
const dialogOpen = computed({
  get: () => openKey.value !== null,
  set: (v) => {
    if (!v) openKey.value = null
  },
})
const currentStage = computed(() => STAGES.find((s) => s.key === openKey.value) || null)
const currentPrinciple = computed(() => (openKey.value ? PRINCIPLES[openKey.value] : null))
const currentIndex = computed(() => {
  const i = STAGES.findIndex((s) => s.key === openKey.value)
  return i < 0 ? 1 : i + 1
})

/** 弹框与卡片同色：tone 名 → 具体色值（与 .tone-* 中的 --stage-color 保持一致）。 */
const TONE_COLOR = { cyan: '#22d3ee', blue: '#60a5fa', violet: '#a78bfa', green: '#34d399', amber: '#fbbf24', rose: '#fb7185' }
const toneColor = computed(() => TONE_COLOR[currentStage.value?.tone] || '#22d3ee')

function openPrinciple(key) {
  openKey.value = key
}
</script>

<template>
  <div class="processing" aria-label="数据处理链路">
    <div class="flow-head">
      <div>
        <span class="flow-kicker mono">LIVE PIPELINE /  STAGES</span>
        <p class="flow-caption">明文入库至完整性证明的处理链路</p>
      </div>
      <span class="flow-state"><i />数据处理链路 · 点击任一阶段查看原理</span>
    </div>

    <div class="pipeline">
      <!-- ① 贯穿整条链路的扫描光：一个流从左到右依次经过六块（纯装饰） -->
      <span class="beam-tail" aria-hidden="true" />
      <span class="beam" aria-hidden="true" />

      <template v-for="(s, i) in STAGES" :key="s.key">
        <div
          class="stage"
          :class="`tone-${s.tone}`"
          :style="{ '--i': i }"
          role="button"
          tabindex="0"
          :aria-label="`${s.label}（${s.en}）：查看对应的密码学原理`"
          @click="openPrinciple(s.key)"
          @keydown.enter="openPrinciple(s.key)"
          @keydown.space.prevent="openPrinciple(s.key)"
        >
          <!-- ② 被"点亮"时的响应层（全部绝对定位，压在文字下面） -->
          <span class="stage-ring" aria-hidden="true" />
          <span class="stage-halo" aria-hidden="true" />
          <span class="stage-bloom" aria-hidden="true" />
          <span class="stage-sheen" aria-hidden="true" />
          <span class="stage-scan" aria-hidden="true" />
          <span class="spark s1" aria-hidden="true" />
          <span class="spark s2" aria-hidden="true" />
          <span class="spark s3" aria-hidden="true" />

          <div class="stage-topline">
            <span class="stage-index mono">0{{ i + 1 }}</span>
            <span class="stage-open mono" aria-hidden="true">原理</span>
          </div>
          <div class="icon-wrap">
            <svg viewBox="0 0 64 64" aria-hidden="true" class="stage-icon">
              <template v-if="s.key === 'upload'">
                <path d="M32 43V15m0 0L21 26m11-11 11 11" class="icon-stroke" />
                <path d="M17 39v10a4 4 0 0 0 4 4h22a4 4 0 0 0 4-4V39" class="icon-stroke" />
                <path d="M13 53h38" class="icon-stroke faint" />
              </template>
              <template v-else-if="s.key === 'split'">
                <rect x="13" y="16" width="38" height="32" rx="4" class="icon-stroke" />
                <path d="M26 16v32M39 16v32M13 32h38" class="icon-stroke faint" />
                <circle cx="20" cy="24" r="2" class="icon-fill" /><circle cx="33" cy="40" r="2" class="icon-fill" /><circle cx="46" cy="24" r="2" class="icon-fill" />
              </template>
              <template v-else-if="s.key === 'commit'">
                <path d="M32 11 48 17v13c0 11-6.5 18.5-16 23-9.5-4.5-16-12-16-23V17l16-6Z" class="icon-stroke" />
                <path d="m24 31 5 5 11-12" class="icon-stroke" />
              </template>
              <template v-else-if="s.key === 'store'">
                <ellipse cx="32" cy="17" rx="17" ry="7" class="icon-stroke" />
                <path d="M15 17v14c0 4 7.6 7 17 7s17-3 17-7V17M15 31v14c0 4 7.6 7 17 7s17-3 17-7V31" class="icon-stroke" />
                <path d="M32 24v7" class="icon-stroke faint" />
              </template>
              <template v-else-if="s.key === 'query'">
                <circle cx="28" cy="28" r="13" class="icon-stroke" /><path d="m38 38 12 12" class="icon-stroke" />
                <path d="M22 28h12M28 22v12" class="icon-stroke faint" />
              </template>
              <template v-else>
                <path d="M32 11 50 18v13c0 12-7.5 19-18 23-10.5-4-18-11-18-23V18l18-7Z" class="icon-stroke" />
                <path d="m23 32 6 6 13-14" class="icon-stroke" />
              </template>
            </svg>
            <span class="icon-pulse" />
            <span class="icon-shock" />
          </div>
          <div class="stage-label">{{ s.label }}</div>
          <div class="stage-en mono">{{ s.en }}</div>
          <div class="stage-detail">{{ s.detail }}</div>
          <span class="stage-progress" aria-hidden="true" />
        </div>
        <div v-if="i < STAGES.length - 1" class="link" :style="{ '--i': i }" aria-hidden="true">
          <!-- ③ 块与块之间的传递：流动的轨道 + 一颗带长尾的彗星 -->
          <span class="rail" /><span class="packet" /><span class="arrow">›</span>
        </div>
      </template>
    </div>

    <!-- 点任一阶段 → 弹出该阶段对应的密码学原理（内容取自作品报告第二章）。 -->
    <el-dialog
      v-model="dialogOpen"
      width="860px"
      top="7vh"
      class="principle-dialog"
      :style="{ '--stage-color': toneColor }"
      append-to-body
      destroy-on-close
    >
      <template #header>
        <div class="pd-head">
          <span class="pd-index mono">0{{ currentIndex }}</span>
          <div>
            <div class="pd-title">
              {{ currentStage?.label }}
              <span class="pd-en mono">{{ currentStage?.en }}</span>
            </div>
            <div class="pd-sub">{{ currentPrinciple?.headline }}</div>
          </div>
        </div>
      </template>

      <div v-if="currentPrinciple" class="pd-body">
        <div v-for="(b, i) in currentPrinciple.blocks" :key="i" class="pd-block">
          <h5>{{ b.t }}</h5>
          <p>{{ b.d }}</p>
        </div>

        <div v-if="currentPrinciple.formulas && currentPrinciple.formulas.length" class="pd-formulas">
          <div v-for="(f, i) in currentPrinciple.formulas" :key="i" class="pd-f">{{ f }}</div>
        </div>

        <div class="pd-refs">
          <span class="pd-ref-tag">依据</span>
          <span v-for="r in currentPrinciple.refs" :key="r" class="pd-ref">{{ r }}</span>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
/* ============================================================================
   数据处理链路 —— 一条能量流依次穿过六个阶段。
   动效分四层，全部只动 transform / opacity（跑在合成器上，不触发布局）：
     ① .beam / .beam-tail   贯穿整条链路的扫描光 + 长尾（"一个流从左到右"）
     ② .stage-*             每块被点亮时的响应：抬起 + 旋转光环 + 斜向扫光 +
                             纵向扫描线 + 底部进度 + 上升粒子 + 图标冲击波
     ③ .rail / .packet      块与块之间的传递：流动轨道 + 带长尾的彗星
     ④ .pipeline::before    整条链路背后的极光（缓慢漂移，压得很低）
   时间轴只有一个参数 --step：第 i 块在第 i 步被点亮，六步一个循环。
   ★ 无障碍：html.no-motion 与 prefers-reduced-motion 会把 animation-duration 压到
     0.001ms ⇒ 动画会停在 100% 那一帧。所以**每个 @keyframes 的 100% 都必须是静止态**
     （装饰层透明度归 0），否则关掉动效后会留下一地发光的东西。
   ========================================================================= */
.processing {
  --cycle: 10s; /* 六块依次跑完一轮 = 10 秒（用户指定的节奏） */
  --step: calc(var(--cycle) / 6); /* 每一步（一块被点亮）的时长 ≈ 1.67s */
  position: relative;
  padding: 2px 0 4px;
  overflow: hidden;
}
.flow-head { position: relative; z-index: 3; display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; margin-bottom: 18px; }
.flow-kicker { position: relative; color: var(--text-3); font-size: 13px; letter-spacing: .14em; }
.flow-kicker::after {
  content: ''; position: absolute; left: 0; bottom: -4px; width: 46px; height: 1px;
  background: linear-gradient(90deg, var(--accent), transparent);
  animation: kicker-glow 4.6s ease-in-out infinite;
}
.flow-caption { color: var(--text-1); font-size: 18px; font-weight: 600; letter-spacing: -.01em; margin: 6px 0 0; }
.flow-state { display: inline-flex; align-items: center; gap: 7px; color: var(--text-3); font-size: 13px; white-space: nowrap; }
.flow-state i {
  width: 6px; height: 6px; border-radius: 50%; background: var(--ok);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--ok) 13%, transparent);
  animation: state-breathe 2.8s ease-in-out infinite;
}

.pipeline { position: relative; display: flex; align-items: stretch; gap: 0; min-width: 690px; }

/* ④ 链路背后的极光：只在链路范围内，压得很低，不抢卡片 */
.pipeline::before {
  content: ''; position: absolute; inset: -26px -14px -20px; z-index: 0; pointer-events: none; border-radius: 20px;
  background:
    radial-gradient(38% 58% at 12% 46%, color-mix(in srgb, var(--accent) 14%, transparent), transparent 70%),
    radial-gradient(30% 52% at 84% 54%, color-mix(in srgb, var(--accent-2) 14%, transparent), transparent 72%);
  opacity: .9;
  animation: aurora-drift 15s ease-in-out infinite;
}

/* ① 贯穿整条链路的扫描光：宽约"一块"，按 --cycle 匀速从左扫到右。
   两个元素同尺寸、同关键帧，只有 --tail（横向拉伸倍数）与模糊程度不同 ——
   这样"头"永远对齐，尾巴拖在左边。 */
.beam,
.beam-tail {
  position: absolute; top: -14px; bottom: -10px; left: .8%; width: 15%; z-index: 1;
  pointer-events: none; opacity: 0; transform-origin: 100% 50%;
  animation: beam-run var(--cycle) linear infinite;
}
.beam {
  --peak: 1;
  background: linear-gradient(90deg, transparent, color-mix(in srgb, var(--accent) 40%, transparent) 40%, color-mix(in srgb, #fff 48%, transparent) 55%, transparent 92%);
  filter: blur(8px);
}
.beam-tail { --peak: .55; --tail: 4; background: linear-gradient(90deg, transparent, color-mix(in srgb, var(--accent-2) 22%, transparent) 74%, transparent); filter: blur(16px); }
/* 光柱顶部那条"飞过的亮线"：让"流"在链路顶边上也是连续的 */
.beam::after {
  content: ''; position: absolute; left: -45%; right: -45%; top: 14px; height: 2px; border-radius: 2px;
  background: linear-gradient(90deg, transparent, color-mix(in srgb, #fff 92%, transparent), transparent); opacity: .8;
}

.pipeline > .stage { z-index: 2; }
.stage {
  --stage-color: var(--accent);
  position: relative; flex: 1 1 112px; min-width: 106px; padding: 11px 10px 12px;
  border: 1px solid color-mix(in srgb, var(--stage-color) 30%, var(--line)); border-radius: 8px;
  background: linear-gradient(145deg, color-mix(in srgb, var(--stage-color) 8%, var(--bg-raised)), var(--bg-panel));
  animation: stage-live var(--cycle) cubic-bezier(.34, .01, .2, 1) infinite;
  animation-delay: calc(var(--i) * var(--step));
  will-change: transform;
}
.stage::after {
  content: ''; position: absolute; inset: 0; z-index: 4; border-radius: inherit;
  border-top: 2px solid var(--stage-color); opacity: .7; pointer-events: none;
  animation: topline-lit var(--cycle) ease-out infinite;
  animation-delay: calc(var(--i) * var(--step));
}
.tone-cyan { --stage-color: #22d3ee; }.tone-blue { --stage-color: #60a5fa; }.tone-violet { --stage-color: #a78bfa; }.tone-green { --stage-color: #34d399; }.tone-amber { --stage-color: #fbbf24; }.tone-rose { --stage-color: #fb7185; }

/* ② 被点亮时的响应层：默认全部静止（透明 / 缩到 0），只在轮到这一块时亮起。
   层级：卡片背景(0) < 这些装饰(1) < 文字内容(2) < 顶部彩条(4)。
   ★ .stage-sheen / .stage-scan 是**裁剪容器**（不能给它们 opacity:0 ——
     那会把里面正在跑的伪元素一起乘没），动画在它们的 ::before 上。 */
.stage-ring,
.stage-bloom,
.stage-progress,
.spark { position: absolute; z-index: 1; pointer-events: none; opacity: 0; will-change: transform, opacity; }
.stage-sheen,
.stage-scan { position: absolute; z-index: 1; pointer-events: none; border-radius: inherit; overflow: hidden; }
.stage > .stage-topline, .stage > .icon-wrap, .stage > .stage-label,
.stage > .stage-en, .stage > .stage-detail { position: relative; z-index: 2; }

/* 旋转的描边光环：外层裁剪 + 内层盖住中间 ⇒ 只留下一条绕边走的光带（不用 mask，兼容性最好） */
.stage-ring { inset: 0; border-radius: inherit; overflow: hidden; animation: ring-fade var(--cycle) linear infinite; animation-delay: calc(var(--i) * var(--step)); }
.stage-halo { position: absolute; pointer-events: none; opacity: 0; will-change: transform, opacity; }
.stage-ring::before {
  content: ''; position: absolute; inset: -45%;
  background: conic-gradient(from 0deg, transparent 0 58%, color-mix(in srgb, var(--stage-color) 92%, #fff) 74%, transparent 90%);
  animation: ring-spin 2.1s linear infinite;
}
.stage-ring::after {
  content: ''; position: absolute; inset: 1.5px; border-radius: 6.5px;
  background: linear-gradient(145deg, color-mix(in srgb, var(--stage-color) 8%, var(--bg-raised)), var(--bg-panel));
}
/* 卡片外圈辉光：被点亮时鼓出一圈彩色光晕（用 box-shadow + blur，比 drop-shadow 便宜） */
.stage-halo {
  inset: -7px; border-radius: 13px; z-index: 0;
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--stage-color) 50%, transparent),
              0 0 28px 7px color-mix(in srgb, var(--stage-color) 36%, transparent);
  filter: blur(3px);
  animation: halo-pulse var(--cycle) ease-out infinite;
  animation-delay: calc(var(--i) * var(--step));
}
/* 卡片内部的径向辉光：被点亮时鼓起来 */
.stage-bloom {
  inset: 0; border-radius: inherit;
  background: radial-gradient(70% 60% at 50% 34%, color-mix(in srgb, var(--stage-color) 34%, transparent), transparent 72%);
  animation: bloom-pop var(--cycle) ease-out infinite;
  animation-delay: calc(var(--i) * var(--step));
}
/* 斜向扫光：一束白光从左上扫到右下。
   ★ 手法：容器负责裁剪（overflow:hidden），伪元素是**整层大小**，只动 translateX
     —— 百分比位移的基准是元素自身尺寸，所以“整层平移”才能扫过整张卡；
     若把 2px 的扫査线自己拿去做 translateX(100%)，它只会动 2px（踩过）。 */
.stage-sheen, .stage-scan { inset: 0; border-radius: inherit; overflow: hidden; background: none; }
.stage-sheen::before,
.stage-scan::before {
  content: ''; position: absolute; inset: 0; opacity: 0; transform: translateX(-100%);
  animation-duration: var(--cycle); animation-iteration-count: infinite;
  animation-delay: calc(var(--i) * var(--step));
}
.stage-sheen::before {
  background: linear-gradient(105deg, transparent 42%, color-mix(in srgb, #fff 34%, transparent) 50%, transparent 58%);
  animation-name: sheen-sweep; animation-timing-function: ease-in-out;
}
/* 纵向掠过的柔光带：中间亮、两边渐隐。
   ★ 不能做成一硬边的线：卡面上突然出现一根自带锐边的竖线很磕眼睛
     （用户反馈）。这里是 40% 宽的光带 + 中间一点点高光 + 模糊，
     所以它掠过去的时候与两侧是自然过度的。 */
.stage-scan { inset: 3px 0; }
.stage-scan::before {
  background: linear-gradient(
    90deg,
    transparent 0%,
    color-mix(in srgb, var(--stage-color) 7%, transparent) 24%,
    color-mix(in srgb, var(--stage-color) 34%, transparent) 44%,
    color-mix(in srgb, #fff 26%, transparent) 50%,
    color-mix(in srgb, var(--stage-color) 34%, transparent) 56%,
    color-mix(in srgb, var(--stage-color) 7%, transparent) 76%,
    transparent 100%
  );
  filter: blur(6px);
  animation-name: sheen-sweep; animation-timing-function: ease-in-out;
}
/* 底部进度条：这一段"跑完"的含义 */
.stage-progress {
  left: 9px; right: 9px; bottom: 5px; height: 2px; border-radius: 2px; transform: scaleX(0); transform-origin: left center;
  background: linear-gradient(90deg, color-mix(in srgb, var(--stage-color) 70%, #fff), var(--stage-color));
  box-shadow: 0 0 8px color-mix(in srgb, var(--stage-color) 60%, transparent);
  animation: progress-run var(--cycle) ease-out infinite;
  animation-delay: calc(var(--i) * var(--step));
}
/* 上升的三颗光点 */
.spark { bottom: 12px; width: 3px; height: 3px; border-radius: 50%; background: color-mix(in srgb, var(--stage-color) 85%, #fff); box-shadow: 0 0 8px color-mix(in srgb, var(--stage-color) 80%, transparent); }
.spark.s1 { left: 26%; animation: spark-rise var(--cycle) ease-out infinite; animation-delay: calc(var(--i) * var(--step) + .06s); }
.spark.s2 { left: 50%; animation: spark-rise var(--cycle) ease-out infinite; animation-delay: calc(var(--i) * var(--step) + .34s); }
.spark.s3 { left: 72%; animation: spark-rise var(--cycle) ease-out infinite; animation-delay: calc(var(--i) * var(--step) + .62s); }

.stage-topline { display: flex; justify-content: space-between; align-items: center; color: var(--text-3); font-size: 13px; letter-spacing: .08em; }
.stage-status { color: var(--stage-color); font-size: 13px; opacity: .8; }
.stage-index { color: var(--stage-color); animation: index-glow var(--cycle) ease-in-out infinite; animation-delay: calc(var(--i) * var(--step)); }
.icon-wrap { position: relative; width: 48px; height: 48px; margin: 8px auto 6px; display: grid; place-items: center; color: var(--stage-color); }
.stage-icon { width: 42px; height: 42px; overflow: visible; position: relative; z-index: 1; animation: icon-lift var(--cycle) cubic-bezier(.34, .01, .2, 1) infinite; animation-delay: calc(var(--i) * var(--step)); }
.icon-stroke { fill: none; stroke: currentColor; stroke-width: 2.5; stroke-linecap: round; stroke-linejoin: round; }
.icon-stroke.faint { opacity: .55; stroke-width: 1.8; }.icon-fill { fill: currentColor; }
.icon-pulse { position: absolute; inset: 6px; border: 1px solid currentColor; border-radius: 50%; opacity: .12; animation: icon-ring 2.4s ease-out infinite; animation-delay: calc(var(--i) * .8s); }
/* 图标外的冲击波：点亮的那一刻扩出去一圈 */
.icon-shock { position: absolute; inset: 3px; border: 1.5px solid currentColor; border-radius: 50%; opacity: 0; animation: icon-shock var(--cycle) cubic-bezier(.2, .7, .3, 1) infinite; animation-delay: calc(var(--i) * var(--step)); }
.stage-label { color: var(--text-1); font-size: 13px; font-weight: 600; text-align: center; white-space: nowrap; }
.stage-en { color: var(--stage-color); font-size: 13px; letter-spacing: .12em; text-align: center; margin-top: 3px; animation: en-glow var(--cycle) ease-in-out infinite; animation-delay: calc(var(--i) * var(--step)); }
.stage-detail { color: var(--text-3); font-size: 13px; line-height: 1.5; text-align: center; margin-top: 8px; min-height: 40px; }

/* ③ 块与块之间：轨道 + 流动虚线 + 彗星 + 依次亮起的箭头 */
.link { position: relative; z-index: 2; flex: 0 0 30px; height: 2px; align-self: center; margin: -28px 2px 0; }
.rail { position: absolute; inset: 0; background: var(--line-strong); overflow: hidden; }
.rail::after {
  content: ''; position: absolute; inset: 0; opacity: .8;
  background-image: linear-gradient(90deg, color-mix(in srgb, var(--accent) 78%, transparent) 0 5px, transparent 5px 12px);
  background-size: 12px 100%;
  animation: rail-dash 1.05s linear infinite;
}
.packet {
  position: absolute; top: 50%; left: 0; width: 26px; height: 8px; border-radius: 4px;
  transform: translate(-110%, -50%);
  background: linear-gradient(90deg, transparent, color-mix(in srgb, var(--accent) 80%, #fff));
  filter: drop-shadow(0 0 7px color-mix(in srgb, var(--accent) 75%, transparent));
  animation: packet-run var(--cycle) linear infinite;
  animation-delay: calc(var(--i) * var(--step) + var(--step) * .55);
}
.packet::after {
  content: ''; position: absolute; right: -2px; top: 50%; width: 7px; height: 7px; border-radius: 50%;
  transform: translateY(-50%); background: #fff;
  box-shadow: 0 0 10px 2px color-mix(in srgb, var(--accent) 85%, transparent);
}
.arrow { position: absolute; right: -2px; top: 50%; transform: translateY(-57%); color: var(--text-3); font-size: 15px; animation: arrow-lit var(--cycle) ease-out infinite; animation-delay: calc(var(--i) * var(--step) + var(--step) * .7); }

/* ---------- 关键帧（100% 一律是静止态，理由见文件头）---------- */
@keyframes stage-live {
  0%, 100% { transform: translateY(0) scale(1); border-color: color-mix(in srgb, var(--stage-color) 30%, var(--line)); box-shadow: 0 0 0 0 transparent; }
  4% { transform: translateY(-6px) scale(1.035); border-color: color-mix(in srgb, var(--stage-color) 82%, transparent); box-shadow: 0 16px 40px -8px color-mix(in srgb, var(--stage-color) 42%, transparent), 0 0 22px color-mix(in srgb, var(--stage-color) 22%, transparent); }
  13% { transform: translateY(-4px) scale(1.02); border-color: color-mix(in srgb, var(--stage-color) 62%, transparent); box-shadow: 0 10px 28px -10px color-mix(in srgb, var(--stage-color) 32%, transparent); }
  21% { transform: translateY(0) scale(1); }
}
@keyframes ring-fade { 0% { opacity: 0; } 3%, 18% { opacity: 1; } 26%, 100% { opacity: 0; } }
@keyframes ring-spin { to { transform: rotate(360deg); } }
@keyframes bloom-pop { 0%, 100% { opacity: 0; transform: scale(.86); } 4% { opacity: .95; transform: scale(1.04); } 16% { opacity: .5; transform: scale(1); } 24%, 100% { opacity: 0; transform: scale(1); } }
@keyframes halo-pulse { 0%, 100% { opacity: 0; transform: scale(.94); } 4% { opacity: 1; transform: scale(1); } 15% { opacity: .5; transform: scale(1.015); } 26%, 100% { opacity: 0; transform: scale(1.02); } }
@keyframes topline-lit {
  0%, 100% { opacity: .7; box-shadow: none; }
  5% { opacity: 1; box-shadow: 0 0 14px 1px color-mix(in srgb, var(--stage-color) 80%, transparent); }
  18% { opacity: .75; box-shadow: none; }
}
@keyframes sheen-sweep { 0%, 100% { opacity: 0; transform: translateX(-100%); } 3% { opacity: .9; } 15% { opacity: 0; transform: translateX(100%); } }
@keyframes progress-run { 0% { opacity: 0; transform: scaleX(0); } 2% { opacity: 1; } 15% { opacity: 1; transform: scaleX(1); } 24%, 100% { opacity: 0; transform: scaleX(1); } }
@keyframes spark-rise { 0%, 100% { opacity: 0; transform: translateY(0) scale(.6); } 3% { opacity: .95; transform: translateY(-6px) scale(1); } 16% { opacity: 0; transform: translateY(-30px) scale(.5); } }
@keyframes index-glow { 0%, 100% { text-shadow: none; } 5% { text-shadow: 0 0 12px color-mix(in srgb, var(--stage-color) 85%, transparent); } 18% { text-shadow: none; } }
@keyframes en-glow { 0%, 100% { text-shadow: none; letter-spacing: .12em; } 5% { text-shadow: 0 0 10px color-mix(in srgb, var(--stage-color) 80%, transparent); } 18% { text-shadow: none; } }
@keyframes icon-lift { 0%, 100% { transform: scale(1) rotate(0deg); } 4% { transform: scale(1.16) rotate(-3deg); } 13% { transform: scale(1.06) rotate(1.5deg); } 21% { transform: scale(1) rotate(0deg); } }
@keyframes icon-shock { 0%, 100% { opacity: 0; transform: scale(.55); } 3% { opacity: .8; } 14% { opacity: 0; transform: scale(1.75); } }
@keyframes beam-run {
  0% { opacity: 0; transform: translateX(0) scaleX(var(--tail, 1)); }
  4% { opacity: var(--peak, .9); }
  88% { opacity: var(--peak, .9); }
  96%, 100% { opacity: 0; transform: translateX(660%) scaleX(var(--tail, 1)); }
}
@keyframes packet-run { 0%, 100% { transform: translate(-110%, -50%); opacity: 0; } 3% { opacity: 1; } 24% { transform: translate(calc(100% + 8px), -50%); opacity: 1; } 30%, 100% { transform: translate(calc(100% + 8px), -50%); opacity: 0; } }
@keyframes arrow-lit { 0%, 100% { color: var(--text-3); text-shadow: none; transform: translateY(-57%) scale(1); } 4% { color: var(--accent); text-shadow: 0 0 10px color-mix(in srgb, var(--accent) 90%, transparent); transform: translateY(-57%) scale(1.35); } 20% { color: var(--text-3); text-shadow: none; transform: translateY(-57%) scale(1); } }
@keyframes rail-dash { to { background-position: 12px 0; } }
@keyframes aurora-drift { 0%, 100% { opacity: .55; transform: translate3d(-1.2%, 0, 0) scale(1); } 50% { opacity: .95; transform: translate3d(1.6%, -2%, 0) scale(1.035); } }
@keyframes kicker-glow { 0%, 100% { opacity: .45; transform: scaleX(.75); transform-origin: left; } 50% { opacity: 1; transform: scaleX(1.15); transform-origin: left; } }
@keyframes state-breathe { 0%, 100% { box-shadow: 0 0 0 3px color-mix(in srgb, var(--ok) 10%, transparent); } 50% { box-shadow: 0 0 0 6px color-mix(in srgb, var(--ok) 20%, transparent); } }

@media (max-width: 900px) { .processing { overflow-x: auto; padding-bottom: 10px; } .flow-head { align-items: flex-start; } }
@media (max-width: 560px) { .flow-state { display: none; } }
@media (prefers-reduced-motion: reduce) {
  .stage, .stage-ring, .stage-ring::before, .stage-halo, .stage-bloom, .stage-sheen::before, .stage-scan::before,
  .stage-progress, .stage-icon, .icon-pulse, .icon-shock, .spark, .beam, .beam-tail,
  .rail::after, .packet, .arrow, .stage-index, .stage-en,
  .pipeline::before, .flow-kicker::after, .flow-state i { animation: none; }
}

/* ---------------------------------------------------------------------------
   六张卡片可点开：指针 + 键盘都能进入，视觉上只改边框与焦点环，
   不动 transform（卡片自身有 animation 在用 transform）。
--------------------------------------------------------------------------- */
.stage { cursor: pointer; }
.stage:hover { border-color: color-mix(in srgb, var(--stage-color) 62%, var(--line)); }
.stage:focus-visible { outline: 2px solid color-mix(in srgb, var(--stage-color) 70%, transparent); outline-offset: 3px; }
.stage-topline { display: flex; align-items: center; justify-content: space-between; gap: 6px; }
.stage-open { color: var(--stage-color); font-size: 13px; letter-spacing: .02em; opacity: 0; transition: opacity .18s ease; }
.stage:hover .stage-open, .stage:focus-visible .stage-open { opacity: .9; }
</style>

<!--
  el-dialog 默认 teleport 到 body，scoped 样式够不着它，
  因此这部分用非 scoped 样式，统一以 .principle-dialog 作前缀避免污染。
-->
<style>
.principle-dialog {
  --stage-color: var(--accent);
  background: var(--bg-panel);
  border: 1px solid color-mix(in srgb, var(--stage-color) 34%, var(--line));
  border-radius: 12px;
  overflow: hidden;
}
.principle-dialog .el-dialog__header {
  padding: 16px 22px 14px;
  margin: 0;
  border-bottom: 1px solid var(--line);
  background: linear-gradient(120deg, color-mix(in srgb, var(--stage-color) 14%, transparent), transparent 72%);
}
.principle-dialog .el-dialog__headerbtn { top: 14px; right: 14px; }
.principle-dialog .el-dialog__body { padding: 18px 22px 20px; }

.pd-head { display: flex; align-items: center; gap: 14px; }
.pd-index {
  flex: 0 0 auto;
  width: 42px;
  height: 42px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  font-size: 15px;
  font-weight: 600;
  color: var(--stage-color);
  background: color-mix(in srgb, var(--stage-color) 14%, transparent);
  border: 1px solid color-mix(in srgb, var(--stage-color) 38%, transparent);
}
.pd-title { display: flex; align-items: baseline; gap: 8px; font-size: 17px; font-weight: 600; color: var(--text-1); }
.pd-en { font-size: 13px; letter-spacing: .12em; color: var(--stage-color); }
.pd-sub { margin-top: 4px; font-size: 13.5px; line-height: 1.6; color: var(--text-2); }

.pd-body { display: flex; flex-direction: column; gap: 14px; }
.pd-block h5 {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 4px;
  font-size: 13.5px;
  font-weight: 600;
  color: var(--text-1);
}
.pd-block h5::before { content: ''; flex: 0 0 auto; width: 3px; height: 13px; border-radius: 2px; background: var(--stage-color); }
.pd-block p { margin: 0; font-size: 13.5px; line-height: 1.78; color: var(--text-2); }

.pd-formulas {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 14px;
  border-radius: 10px;
  border: 1px solid color-mix(in srgb, var(--stage-color) 24%, var(--line));
  background: color-mix(in srgb, var(--stage-color) 7%, var(--bg-raised));
}
.pd-f { font-family: ui-monospace, Consolas, monospace; font-size: 13.5px; text-align: center; color: var(--text-1); }

.pd-refs { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding-top: 6px; border-top: 1px dashed var(--line); }
.pd-ref-tag { font-size: 13px; color: var(--text-3); }
.pd-ref { font-size: 13px; color: var(--text-2); padding: 2px 9px; border-radius: 999px; border: 1px solid var(--line); background: var(--bg-raised); }
</style>
