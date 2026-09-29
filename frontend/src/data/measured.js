/**
 * 「性能」页要用的实测数字。
 *
 * ★ 这些不是页面现算的。它们是脚本跑完记下来的记录值，画在界面上只是为了让
 *   答辩时不用切 Markdown 文件。页面上必须把「这是记录值」写清楚。
 *
 * ★ 三处必须一致：本文件、README.md 的「关键实测数字」一节、规划 §4。
 *   重新测一次就要同时改这三处。
 */

export const CFG_L256 = {
  key: 'l256',
  label: '本项目实现',
  config: ['|N| = 1024', 'l = 256（每段密文 1 KB → 摘要 32 字节）', 'n_max = 256', '4 台服务器'],
  source: 'scripts/ 下的基准脚本，数字记在 README「关键实测数字」',
}

export const CFG_L128 = {
  key: 'l128',
  label: '规划 §4 的实测',
  config: ['|N| = 1024', 'l = 128（每块 16 字节）', 'n_max = 1024', 'i7-14650HX，CPython 3.12.4 / Windows'],
  source: '规划 §4「实测性能预算（本机）」',
}

/** 一次查询的各阶段耗时（l = 256 档）。evidence 是证据字节数，恒为两个群元素。 */
export const QUERY_SWEEP = [
  { q: 1, certs: 1, servers: 1, prove: 12.2, aggregate: 0.0, verify: 4.0, evidence: 256 },
  { q: 2, certs: 2, servers: 2, prove: 24.5, aggregate: 14.3, verify: 8.4, evidence: 256 },
  { q: 4, certs: 4, servers: 4, prove: 42.9, aggregate: 65.3, verify: 16.9, evidence: 256 },
  { q: 8, certs: 4, servers: 4, prove: 27.6, aggregate: 176.8, verify: 38.2, evidence: 256 },
  { q: 14, certs: 4, servers: 4, prove: 0.1, aggregate: 261.6, verify: 56.6, evidence: 129 },
]

/** 聚合 + 验证（热，l = 128 档）：严格线性，与 n 无关。 */
export const HOT_VERIFY = [
  { q: 1, ms: 2.5, per: 2.5 },
  { q: 8, ms: 17.8, per: 2.2 },
  { q: 32, ms: 73.3, per: 2.3 },
  { q: 64, ms: 145.6, per: 2.3 },
]

/** 三个规模下的主流程（l = 128 档）。 */
export const MAIN_FLOW = [
  { step: 'commit（素数表 + Specialize + 承诺）', n64: 559, n256: 2594, n1024: 10759, grows: true },
  { step: 'distribute 到 4 台（二分拆）', n64: 383, n256: 1280, n1024: 4822, grows: true },
  { step: 'retrieve 8 块（跨 2 台）', n64: 58, n256: 320, n1024: 1376, grows: true },
  { step: '聚合 + 验证（冷，首次）', n64: 82, n256: 67, n1024: 79, grows: false },
]

/** 批量验证的实测 —— 量过之后决定不做（比逐份慢）。 */
export const BATCH_TRIAL = {
  config: ['|N| = 1024', 'l = 256', 'n = 64 块 / 8 个文件'],
  rows: [
    { what: '一次查全部 64 块（一份证据）', ms: 2097.7, note: '设计 B 的主路径，跨文件天然合成一份' },
    { what: '逐块各查一次（64 份单块证据）', ms: 5022.0, note: '比合一份慢 2.4 倍' },
    { what: '逐份 svc.verify（64 份已有证据）', ms: 318.4, note: '主路径上「验证」那一栏' },
    { what: '验证一份覆盖 64 块的证据', ms: 331.0, note: '与上一条同量级 —— 代价看块数，不看份数' },
    { what: '批量方程（两条信道都合，随机系数）', ms: 684.7, note: '⇒ 慢 2.15 倍', bad: true },
  ],
  why: '本项目的 svc.verify 已经把论文里「先重建每个 S_i 再乘起来」的 O(l·|I|²) 换成了 add_back 链的 O(l·|I|) 次小指数模幂。批量法必须回到「少次、但每次指数有 |I|·(l+1) 位」的形式 —— 模幂次数差不多，指数却大了一个数量级，于是净亏。',
  gained: '而「跨文件」那一半收益早就拿到了：一次查询就是一份证据覆盖多文件（上表第 1 行 vs 第 2 行差 2.4 倍）。',
}

/** 设计 B 的真实代价：每次上传都是一次全网事件。 */
export const UPLOAD_COST = {
  rows: [
    { what: '一个 3 块的文件', ms: 393 },
    { what: '一个 5 块的文件', ms: 1271 },
  ],
  note: '代价量级 ≈ 块数 × 服务器台数 次 ShamirTrick。追加同理：它走同一条 add 路径，代价只与新块数有关，与文件原有大小无关。',
  exception: '一个漂亮的例外：收到新块的那一台，它的 S_I 反而不变 —— e_[n] 乘了 e_j，e_I 也乘了 e_j，比值恰好抵消。',
}

/** 一次性成本（l = 128 档）。 */
export const ONE_TIME = [
  { what: 'Setup（生成隐藏阶群 N、g），|N| = 1024', ms: 56.5, text: '56.5 ms' },
  { what: 'PrimeGen.first(64)', ms: 104, text: '104 ms' },
  { what: 'PrimeGen.first(256)', ms: 376, text: '376 ms' },
  { what: 'PrimeGen.first(1024) —— 最贵的一次性开销', ms: 1442, text: '1442 ms' },
  { what: 'SM4-CTR 加密 1 MB', ms: 19.5, text: '19.5 ms（≈ 51 MB/s）' },
]

/** 两条参数律。 */
export const SCALE_LAWS = [
  {
    what: '|N| = 512 → 1024 → 2048（n = 64 的 commit）',
    raw: '98 → 266 → 956 ms',
    verdict: '近似立方律，每档约 ×3.6 ⇒ 不要用 |N| = 2048：n = 1024 时 commit 会涨到约 39 s',
  },
  {
    what: 'l = 64 → 128 → 256（n = 64 的 commit）',
    raw: '146 → 272 → 487 ms',
    verdict: '近似线性 ⇒ 块哈希层把 l 从 128 抬到 256，约 1.8 倍的代价就是这么来的',
  },
]

/** 两档 l 的对照。 */
export const L_COMPARE = [
  { what: 'commit', l128: '10.5 ms/块', l256: '约 19 ms/块（×1.8）' },
  { what: '验证', l128: '2.2 ms/块', l256: '约 4 ms/块' },
  { what: '文件上限（n_max = 1024）', l128: '16 KB', l256: '1 MB' },
]

/** 其它零散数字。 */
export const MISC = [
  { what: '单个文件上传', value: '130 ~ 1300 ms（随全局块数增长）' },
  { what: 'SM4 加密 1 MB', value: '约 20 ms' },
  { what: '全面自检（/api/admin/check）', value: '184 ms' },
]
