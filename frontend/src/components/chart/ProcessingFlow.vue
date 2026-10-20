<script setup>
/**
 * Processing 数据处理链路：用六个语义化 SVG 节点表达从上传到验证的闭环。
 * 动画分成两层：数据包沿轨道顺序传递，当前阶段卡片周期性扫描，
 * 让动效看起来像真实的数据处理，而不是同时闪烁的装饰。
 *
 * 六张卡片都可点开：弹出的原理说明见下方 PRINCIPLES（内容取自作品报告第二章）。
 */
import { computed, ref } from 'vue'
import { rich } from '../../utils/tex'

/*
 * 六张卡片的副标题（卡片上那行小字）。
 *
 * ★ 一句一条，只说**本项目实际做了什么** —— 卡片只是目录，深的东西在点开后的
 *   PRINCIPLES 里。这里写长句会把卡片撑高，而这一屏的重点是下面的内容（总览页是运行性能，
 *   初始化页是规模与参数）。
 */
const STAGES = [
  { key: 'upload', label: '数据块切分', en: 'CHUNK', detail: '明文切 n 块，块号随切分固定', tone: 'cyan' },
  { key: 'encrypt', label: '加密与密钥封装', en: 'ENCRYPT', detail: '每块一把 SM4 密钥，SM2 公钥封装', tone: 'blue' },
  { key: 'commit', label: '承诺生成', en: 'COMMIT', detail: '位置映射素数，分量聚合为 δ', tone: 'violet' },
  { key: 'store', label: '分布式存储', en: 'REPLICATE', detail: '密文按副本因子分发至各节点', tone: 'green' },
  { key: 'query', label: '可验证查询', en: 'QUERY', detail: '取回凭证，凭 δ 当场配对检验', tone: 'amber' },
  { key: 'aggregate', label: '增量聚合与分解', en: 'AGGREGATE', detail: '证据可合可分，代价只随变动', tone: 'rose' },
]

/**
 * 六个阶段各自的密码学原理（点开卡片时弹出）。
 *
 * ★ 卡片与「上传页九步埋点」的对应（清单见 files/list/index.vue 的 UPLOAD_STEPS：
 *   那份 key 与后端 `with stage("…")` 的名字对齐，见 core/timing.py）：
 *     ① 数据块切分   ← ① 明文分段
 *     ② 加密与密钥封装 ← ② 块密钥与初始向量生成 / ③ 数据块加密/ ④ 块密钥封装
 *     ③ 承诺生成     ← ⑤ 块标识生成/ ⑥ 承诺（push_update，含素数映射）
 *     ④ 分布式存储   ← ⑦ 分发至存储节点 / ⑧ 分块登记 / ⑨ 文件登记
 *     ⑤ 可验证查询、⑥ 增量聚合与分解 —— 这两环在上传链路之外。
 *   （⑤ 内部再分「检索取证」与「配对检验」两页：取出证据、当场验它，
 *     本来就是一次查询的两半；⑥ 内部再分「证据聚合」与「证据分解」两页。）
 *   这样分的原因：先在明文层「切块」，再密文层「加密」，再代数层「承诺」；
 *   位置素数既是「这一块落在哪里」的坐标，也是累加器的指数，因此与承诺同卡。
 *
 * 内容取自作品报告第二章「基础知识」；公式写 LaTeX（渲染器见 utils/tex.js）：
 * 上标下标用 `_` 与 `^`（大括号分组，如 `S_{I}^{e_I}`）；希腊字母与运算符用命令
 * （`\delta`、`\Lambda`、`\gcd`、`\prod`、`\cdot`、`\ne`）；正体用 `\mathrm{}`。
 * 模板里交给 rich() 渲染 —— 界面上不出现 `w_i`、`v{i}` 这类裸写法。
 * ★ 渲染器只认 tex.js 里那张符号表，表外的命令会原样显出来，别用 `\lceil` 这类。
 */
const PRINCIPLES = {
  upload: {
    headline: '定长切块：把明文切成固定长度的块序列，为「块号 → 位置」做好准备',
    blocks: [
      { t: '两步组织：先规划、再上传', d: '块大小顾问依据文件体积给出候选切法，列出块数、预计耗时与所依据的实测样本，并给出分发建议；顾问只提供建议，采纳与否由用户确认。' },
      { t: '段大小：部署给区间，文件各自记录', d: '部署配置规定的段大小区间是 64 字节至 1 MiB；某份文件按多少字节切分，会逐文件记录在该文件的元数据里，因此日后调整部署默认值不会影响既有文件。' },
      { t: '最后一段可以短，且不补齐', d: '末段不足段大小时按原样保留，不做填充：每段各自独立加密，填充反而会把长度信息搅乱；明文长度由调用方另行记录，段边界完全由长度决定，拼接时不需要任何填充约定。' },
      { t: '块号自此刻起固定', d: '分块编号在切分完成时即行确定并固定下来；后续的内容替换、置零、追加与删除，都以这个编号为坐标。' },
      { t: '为什么必须定长', d: '每个分块要一一对应到向量上的一个位置，长度不统一就无法用「块号 → 位置」这条简单映射；定长同时使每块参与承诺的分量固定为 256 位，与块本身大小无关。' },
    ],
    formulas: [
      '$s \\cdot (b - 1) < |M| \\le s \\cdot b$　（$|M|$ 为明文字节数，$s$ 为段大小，$b$ 为块数）',
      '$|M_{b-1}| = |M| - (b-1) \\cdot s \\le s$　（末段可以短，不补齐）',
    ],
  },
  encrypt: {
    headline: '机密性由 SM4-CTR 提供，块密钥由 SM2 公钥封装，完整性由向量承诺提供',
    blocks: [
      { t: '分组密码', d: 'SM4 的分组长度与密钥长度均为 128 位，采用 32 轮非平衡 Feistel 结构，轮函数由非线性变换 $\\tau$（逐字节过 8 位 S 盒）与线性变换 $L$ 复合而成（GB/T 32907—2016）。' },
      { t: '每段一把独立密钥', d: '每个分块用一把即时生成的 16 字节密钥独立加密，目的是按段隔离：某一块出问题只影响该块；代价是每段都要单独保护自己的密钥，这正是下面用 SM2 公钥逐块封装的理由。' },
      { t: '工作模式的选择（CTR）', d: 'CBC 具有错误传播：一段被改动会破坏其后所有分段。本系统的核心操作是仅替换指定分块、其余分块保持不变；若采用带传播性的模式，改写一个分块将导致其后全部分段重算，改写与追加的代价不再与改动量成正比。CTR 各段相互独立，任意一段均可单独解密，这是随机访问与增量更新的前提。' },
      { t: 'IV 不复用', d: 'CTR 下 IV 复用等价于同一密钥下重复使用计数器序列，两段明文异或后会泄露信息，因此每次加密都必须换新的随机 IV。' },
      { t: '块密钥的保护：ECIES 封装', d: '该段的 SM4 密钥以文件所有者的 SM2 公钥按 ECIES 封装，明文密钥在封装后立即销毁；系统里不存在能解开它的第二把钥匙，因此「解密仅限所有者」是密码学强制的，而不是靠权限表。' },
      { t: '边界', d: '纯 CTR 仅保证机密性，不提供完整性：被篡改的密文仍能解出内容。本方案的完整性由向量承诺承担：验证方自行从收到的密文重算分量，能通过承诺验证即说明密文未被改动，因此无需再叠加认证加密层。' },
    ],
    formulas: ['$c_j = m_j \\oplus E_k(\\mathrm{ctr}_j)$　　（第 $j$ 段密文；$k$ 为该段独立密钥）'],
  },
  commit: {
    /*
     * ★ 这张卡也有**页签**：素数映射 / 分量与承诺。
     *   位置素数既是「这一块落在哪里」的坐标，又是累加器的指数 ——
     *   两者一体两面，所以放在同一张卡的两页里。
     */
    tabs: [
      {
        key: 'pos',
        label: '素数映射',
        headline: '位置即素数：每个分块绑定一个互不相同的素数指数',
        blocks: [
          { t: '位置即素数', d: '向量下标 $i$ 由素数映射 $\\mathrm{PrimeGen}$ 对应到一个素数 $e_i$，同时要求三件事：确定性（同一个 $i$ 永远得到同一个 $e_i$，否则系统重启后无法复现承诺）、无碰撞、位长恰为 $\\ell + 1$ 位。' },
          { t: '互异性是正确性的前提', d: '若 $e_i = e_j$，则 $\\gcd(e_i, e_j) \\ne 1$；此时 Shamir 技巧解出的 Bézout 系数满足 $a \\cdot x + b \\cdot y = d > 1$，代回后不报错，会静默给出错误结果 —— 这类缺陷最难排查。' },
          { t: '位置由块的身份派生', d: '论文只要求位置素数两两互异；实现不维护全局素数表，改为把所有者、文件标识与块下标拼成字节串反复哈希，直到得到一个素数，该素数同时就是这一块的位置。论文原文明确说「双射映射本身就足够」，因此代数结构不变；换来的是位置不需要全局分配，删除留下的空洞也不必回收。' },
          { t: '实现纪律与客户端校验', d: '不满足互素前提的调用一律显式返回空值或抛错，绝不静默给出计算结果；客户端拿到素数后还要做位长、奇偶、区间与素性检查，而不是无条件采信服务端。' },
        ],
        formulas: [
          '$i \\mapsto e_i$，$|e_i| = \\ell + 1$',
          '$\\gcd(e_i, e_j) = 1$　（$i \\ne j$）',
        ],
      },
      {
        key: 'acc',
        label: '分量与承诺',
        headline: '把整条向量压成两个群元素，并为每个位置留下可公开计算的见证',
        blocks: [
          { t: '被承诺的对象', d: '承诺的对象是密文段的 SM3 摘要 $v_i = \\mathrm{SM3}(c_i)$。由此每个位置仅需 32 字节即可参与承诺，与分块大小无关；代价是额外依赖 SM3 的抗碰撞性 —— 若两段不同密文能算出同一个 $v_i$，「分量与承诺相符」便不再等价于「密文未被篡改」。' },
          { t: '累加器与整除性', d: 'RSA 累加器把集合压成一个群元素 $U_n = g^{E}$（$E$ 为全部素数之积）；位置 $i$ 的见证 $S_i = g^{E / e_i}$ 是一条普通模幂 —— 因为 $e_i$ 整除 $E$，指数是整数，不需要求任何方根。正是这种整除性使方案能够公开全部参数与见证。' },
          { t: '承诺', d: '在累加器之上再叠一层线性组合，把位置 $i$ 的值 $v_i$ 作为见证 $S_i$ 的指数权重：$C = \\prod S_i^{v_i}$（对全部位置连乘）。向量中任意位置的任何改动都会改变 $C$，而 $C$ 自始至终只是一个群元素。' },
          { t: '批量分解', d: '承诺阶段需一次产出全部 $S_i$。逐个处理需要 $m$ 次大数除法与 $m$ 次模幂；改用分治分解后只需一次分解即可获得全部结果，$n$ 较大时差异显著。' },
        ],
        formulas: [
          '$v_i = \\mathrm{SM3}(c_i)$',
          '$U_n = g^{E} \\bmod N$　（$E = e_1 \\cdot e_2 \\cdots e_n$）',
          '$C = S_1^{v_1} \\cdot S_2^{v_2} \\cdots S_n^{v_n} \\bmod N$',
        ],
      },
    ],
  },
  store: {
    headline: '节点各自持有向量的一段，公开参数只有群描述与素数映射',
    blocks: [
      { t: '群阶未知是安全性的来源', d: '隐藏阶群 $G = (N = p \\cdot q,\\ g)$ 的阶 $\\varphi(N)$ 在输出中不可得。敌手若获知 $\\varphi(N)$，即可对任意整数求 $e$ 次根、伪造任意位置的见证，方案随即失效。实现上在生成群描述之后立即销毁 $p$ 与 $q$，序列化层只导出 $N$、$g$ 与素数映射。' },
      { t: '群除法：由群阶未知决定', d: '聚合时需要除去交叉项，实现为群元素相除 $C / C\\prime$。这一处极易被误写成指数相减：指数上的运算必须在整数层完成，不能在模群阶的意义下做减法，因为群阶本身未知。' },
      { t: 'VDS 模型', d: '客户端只持有一个常数大小的摘要 $\\delta = ((U, C),\\ n)$，各存储节点各持向量的一个片段；双方通过更新协议（$\\mathrm{PushUpdate}$ / $\\mathrm{ApplyUpdate}$）与检索协议交互，共同完成更新与检索两类操作。' },
      { t: '一文件一向量', d: '每份文件各自一条向量、各自一份摘要 δ，新建或改动一份完全不触碰别人的摘要。跨文件取证时在合并位置集上重算一份证据与合并承诺，因此一次检索得到的证据仍然可以覆盖多个文件与多个用户。' },
    ],
    formulas: ['$\\delta = ((U, C),\\ n)$', '$G = (N, g)$，$N = p \\cdot q$，$\\varphi(N)$ 不可得'],
  },
  query: {
    /*
     * ★ 这张卡把原来的「结果验证」并了进来：一次可验证查询本来就是两半 ——
     *   先取证（生成 $\pi_I$），再当场验它（两条配对检查）。在此之前，
     *   这两半分在两张卡上，而「配对检验」又和「聚合/分解」挤在同一张卡的
     *   页签里；现在按主题拆干净：
     *     ⑤ 可验证查询    = 检索取证 + 配对检验
     *     ⑥ 增量聚合与分解 = 证据聚合 + 证据分解
     */
    tabs: [
      {
        key: 'prove',
        label: '检索取证',
        headline: '取出若干块，并附上一份恒为两个群元素的证据',
        blocks: [
      { t: '子向量打开', d: '对位置子集 $I$ 生成证据 $\\pi(I) = (S(I),\\ \\Lambda(I))$，仅由两个群元素构成；承诺与证据的大小只与安全参数有关，不随向量长度 $n$ 或打开位置个数 $|I|$ 增长（简洁性）。' },
      { t: '第二个分量 $\\Lambda$ 的定义', d: '$S(I)$ 只由位置集合 $I$ 本身决定，是一条普通模幂；$\\Lambda(I)$ 则要把「不在 $I$ 里」的那些位置的值全部卷进来 —— 它是这些位置的加权见证之积，再开 $E(I)$ 次根。因此它的支撑集是 $I$ 的补集，计算代价与向量总长度同阶。约定：$I$ 覆盖全部位置时乘积为空，按空乘积取 $\\Lambda(I) = 1$。' },
      { t: '可聚合', d: '同一承诺下两个不相交集合 $I$、$J$ 的证据可合并为覆盖 $I \\cup J$ 的一份证据，大小仍是两个群元素；合并次数不受限制，顺序任意，结果相同。' },
      { t: '增量性', d: '随长度变化的公开量从「公开参数」转移到「摘要」，公开参数只需生成一次，此后的增删与更新只影响摘要中的两个分量。' },
      { t: '合并的代数基础：Shamir 技巧', d: '已知 $g$ 的 $1/x$ 次根与 $1/y$ 次根（$x$、$y$ 互素），由扩展欧几里得求出 $a \\cdot x + b \\cdot y = 1$，即可合成 $1/(xy)$ 次根。实现内置两道自检：同源（两个根必须是同一个 $g$ 的根）与互素（$\\gcd(x, y) = 1$），缺一则拒绝合并，被篡改的证据拼不出一份能通过验证的证据。' },
    ],
    formulas: [
      '$\\pi(I) = (S(I),\\ \\Lambda(I))$，大小恒为 2 个群元素',
      '$S(I) = g^{E / E(I)} \\bmod N$　（$E$ 为全局素数之积，$E(I)$ 为子集 $I$ 之积）',
      '$\\Lambda(I) = (\\prod_{j \\notin I} S_j^{v_j})^{1 / E(I)} \\bmod N$　（$j$ 取遍不在 $I$ 中的位置）',
      '$\\Lambda(I) = \\prod_{j \\notin I} (g^{E / (e_j \\cdot E(I))})^{v_j} \\bmod N$　（等价写法：每项都是普通模幂，不求根）',
      '$\\mathrm{root} = \\mathrm{root}_x^{\\,b} \\cdot \\mathrm{root}_y^{\\,a} \\bmod N$，　$a \\cdot x + b \\cdot y = 1$',
        ],
      },
      {
        key: 'ver',
        label: '配对检验',
        headline: '两条检查相互配合，零信任地判定内容未被篡改',
        blocks: [
          { t: '检查①：结构合法性', d: '要求 $S(I)^{E(I)} = U_n$。该检查把 $S(I)$ 唯一确定为 $U_n$ 的 $E(I)$ 次根，不涉及任何数据值。它之所以必要：验证方并不持有单个 $S_i$，后者是从 $S(I)$ 现场重构的；若不先把 $S(I)$ 锁死，敌手可以任选一个 $S(I)$ 去凑第②式。' },
          { t: '检查②：内容一致性', d: '要求 $C = \\Lambda(I)^{E(I)} \\cdot \\prod_{i \\in I} S_i^{v_i}$（$i$ 取遍 $I$）。它把承诺 $C$、声明的值 $v(I)$ 与证据 $\\Lambda(I)$ 相互关联。' },
          { t: '两者缺一不可', d: '只保留②，敌手可任意选取 $S(I)$ 来凑等式；只保留①，等式右端可被随意替换。① 之所以站得住，依赖低阶假设 —— 敌手无法用一个低阶元素同时满足两条检查，而这样的元素并不对应真实的向量。' },
          { t: '公共可验证', d: '验证过程不需要任何秘密：$N$、$g$、素数映射、摘要 $\\delta$ 与证据 $\\pi$ 全部是公开量（或由被验证方交付）。因此任何登录用户都能验证任意位置的完整性，包括不属于自己的文件 —— 「解密受限」与「验证不受限」由此分开：前者靠密钥，后者靠承诺。' },
          { t: '位置绑定', d: '承诺者无法对同一位置 $i$ 给出两份都能通过验证、但取值不同的打开结果，因此节点无法事后否认它保存过的内容。其安全性归约到强 RSA 假设；在动态更新场景下还需更强的 Strong DPR 假设，以排除「通过多次更新累积出一次非法开方」的路径。' },
          { t: '失败环节名', d: '验证失败时返回的不是一句笼统的结论，而是环节码：载荷形状、第①式、第②式各对应一个码。前后端各维护一张对照表必然分叉，因此名字只从枚举一处派生。' },
          { t: '零信任与常数大小', d: '客户端仅需保存一个常数大小的摘要，即可验证聚合后的那一份证据，从而确认取回的块均未被篡改，无需信任任何一台服务器。' },
        ],
        formulas: [
          '$S(I)^{E(I)} = U_n \\bmod N$',
          '$C = \\Lambda(I)^{E(I)} \\cdot \\prod_{i \\in I} S_i^{v_i} \\bmod N$',
          '$S_i = S(I)^{\\,E(I)/e_i}$　（$i \\in I$；由 $S(I)$ 现场重构，无需预先持有）',
        ],
      },
    ],
  },
  aggregate: {
    /*
     * ★ 这张卡原来挂在「结果验证」里当两个页签（证据聚合 / 证据分解）。
     *   聚合与分解是同一件事的两个方向：一个把小集合并大、一个把大集合拆小，
     *   两者配合能处理任意形状的集合运算，所以合成一卡两页。
     *   公式取自已发货的实现（svc/scheme.py 的 agg / disagg 两个函数的文档，
     *   那几段直接引了论文 §5.2 与清单 #22~#24）。
     */
    tabs: [
      {
        key: 'agg',
        label: '证据聚合',
        headline: '同一承诺下，两份不相交的证据可合并为一份，大小仍为两个群元素',
        blocks: [
          { t: '前提：同一承诺、位置集合不相交', d: '可聚合性定义在同一个承诺 $C$ 之上：两份证据必须验在同一个 $C$ 上，且 $I \\cap J = \\varnothing$。存在重叠时先用分解把重叠部分从其中一份上消掉，再合并。' },
          { t: '合并 $S$：Shamir 技巧', d: '已知同一底数的 $1/E(I)$ 次根与 $1/E(J)$ 次根，且 $\\gcd(E(I), E(J)) = 1$（不相交保证这一点），由扩展欧几里得求出 $a \\cdot E(I) + b \\cdot E(J) = 1$，即可合成 $1/[E(I) \\cdot E(J)]$ 次根。实现内置两道自检：同源（两个根必须来自同一个底数）与互素，缺一则拒绝合并。被篡改的证据因此拼不出一份能通过验证的证据。' },
          { t: '合并 $\\Lambda$：先除交叉项', d: '$\\Lambda(I)$ 中属于 $J$ 的那部分是 $1/E(I)$ 次方，而合并后该部分应当是 $1/[E(I) \\cdot E(J)]$ 次方，次数不匹配，直接相乘会重复计数。所以先各自除掉本方的交叉项 $\\prod_{j \\in J} \\varphi_j^{v_j}$，再由 Shamir 技巧把两个残项合并。记两边的残项为 $\\rho_I = \\Lambda(I) / \\prod_{j \\in J} \\varphi_j^{v_j}$ 与 $\\sigma_J = \\Lambda(J) / \\prod_{i \\in I} \\psi_i^{v_i}$，其中 $\\psi_i = S(K)^{E(I)/e_i}$；它们才是次数对齐、可以交给 Shamir 技巧的一对。' },
          { t: '群除法，而不是指数相减', d: '群阶 $\\varphi(N)$ 不可得，指数上的运算必须在整数层完成：把 $1/E(I)$ 与 $1/[E(I) \\cdot E(J)]$ 写在一起相减是没有意义的。而 $\\prod \\varphi_j^{v_j}$ 本身就是一个能算出来的群元素，直接做群除法即可。' },
          { t: '增量可聚合', d: '合并结果仍是一份合法的子向量打开证据，因此可以继续参与合并：合并次数不限、顺序任意、结果逐位相同。这是这套承诺能用于“一次取证覆盖多块”的基础。' },
          { t: '本项目的边界', d: '每份文件各有自己的摘要与承诺，而 $\\Lambda$ 的支撑集又是本文件位置集合的补集 —— 跨文件时两份证据没有共同的 $C$，交叉项也无从抵消。所以跨文件检索改为在合并位置集上重算一份证据，代价与涉及文件的总位置数同阶。' },
        ],
        formulas: [
          '$I \\cap J = \\varnothing$，　$K = I \\cup J$',
          '$S(K) = \\mathrm{ShamirTrick}(S(I),\\ S(J),\\ E(I),\\ E(J))$，　$a \\cdot E(I) + b \\cdot E(J) = 1$',
          '$\\rho_I = \\Lambda(I) / \\prod_{j \\in J} \\varphi_j^{v_j}$，　$\\varphi_j = S(K)^{\\,E(J)/e_j}$',
          '$\\sigma_J = \\Lambda(J) / \\prod_{i \\in I} \\psi_i^{v_i}$，　$\\psi_i = S(K)^{\\,E(I)/e_i}$',
          '$\\Lambda(K) = \\mathrm{ShamirTrick}(\\rho_I,\\ \\sigma_J,\\ E(I),\\ E(J))$',
        ],
      },
      {
        key: 'disagg',
        label: '证据分解',
        headline: '从覆盖大集合的证据里拆出子集的证据，而不必重新向节点取证',
        blocks: [
          { t: '用途', d: '已有一份覆盖 $I$ 的证据，而实际只需要其中一部分位置时，直接拆出覆盖 $K \\subseteq I$ 的那份，无需重新发起检索。界面上的「分解」按钮走的就是这条路径。' },
          { t: '第一步：把 $S$ 收到子集', d: '$S(K) = S(I)^{E(I \\setminus K)}$。$E(I \\setminus K)$ 是整数，所以这一步只是一次普通模幂，不涉及任何求根。' },
          { t: '第二步：把拆掉的位置逐个加回摘要', d: '对每个 $j \\in I \\setminus K$ 计算 $\\chi_j = S(I)^{\\,E(I \\setminus K)/e_j} = g^{\\,E/[E(K) \\cdot e_j]}$。它就是 $\\Lambda(K)$ 定义里「$j \\notin K$ 且 $j \\in I$」那一部分。' },
          { t: '第三步：凑齐 $\\Lambda(K)$', d: '由两部分相乘（群乘法）构成：来自 $j \\notin I$ 那些位置的 $\\Lambda(I)^{E(I \\setminus K)}$，加上来自 $j \\in I \\setminus K$ 那些位置的 $\\prod_{j \\in I \\setminus K} \\chi_j^{v_j}$。两者合起来正好是完整的 $\\Lambda(K)$。' },
          { t: '实现的等价改写', d: '论文公式要求一次 $O(\\ell |E| \\log |E|)$ 的批量求根。实现改用逐个 add_back：视角是 $\\pi_I = d(v \\setminus I)$、目标是 $d(v \\setminus K)$，而 $I \\setminus K$ 正是要加回来的那些位置，于是逐个加回即可。代价降到 $O(\\ell |E|)$，结果与论文公式逐项对应。' },
          { t: '与聚合互逆', d: '分解把大集合拆小、聚合把小集合并大。两者配合可以处理任意形状的集合运算：例如先把一侧的重叠部分分解掉，再把两份聚合起来，即得到并集的证据。' },
        ],
        formulas: [
          '$K \\subseteq I$',
          '$S(K) = S(I)^{\\,E(I \\setminus K)}$',
          '$\\chi_j = S(I)^{\\,E(I \\setminus K)/e_j} = g^{\\,E/[E(K) \\cdot e_j]}$　（$j \\in I \\setminus K$）',
          '$\\Lambda(K) = \\Lambda(I)^{\\,E(I \\setminus K)} \\cdot \\prod_{j \\in I \\setminus K} \\chi_j^{v_j}$',
        ],
      },
    ],
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

/**
 * 带页签的卡片（目前只有「结果验证」）。
 *
 * ``tabs`` 存在 ⇒ 弹框里出现页签条，内容按页渲染；
 * 其余卡片没有这个字段 ⇒ ``shownPage`` 退回整份 principle，与改动前完全一致。
 */
const activeTab = ref(0)
const tabs = computed(() => currentPrinciple.value?.tabs || null)
const shownPage = computed(() => {
  const list = tabs.value
  if (!list || !list.length) return currentPrinciple.value
  return list[Math.min(activeTab.value, list.length - 1)]
})

const currentIndex = computed(() => {
  const i = STAGES.findIndex((s) => s.key === openKey.value)
  return i < 0 ? 1 : i + 1
})

/** 弹框与卡片同色：tone 名 → 具体色值（与 .tone-* 中的 --stage-color 保持一致）。 */
const TONE_COLOR = { cyan: '#22d3ee', blue: '#60a5fa', violet: '#a78bfa', green: '#34d399', amber: '#fbbf24', rose: '#fb7185' }
const toneColor = computed(() => TONE_COLOR[currentStage.value?.tone] || '#22d3ee')

function openPrinciple(key) {
  activeTab.value = 0 // ★ 每次打开都从第一页开始（弹框 destroy-on-close，但 ref 还在）
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
            <div class="pd-sub">{{ shownPage?.headline }}</div>
          </div>
        </div>
      </template>

      <div v-if="shownPage" class="pd-body">
        <!-- ★ 换页：只在有多页的卡片上出现（其余卡片没有 tabs） -->
        <div v-if="tabs" class="pd-tabs" role="tablist">
          <button
            v-for="(t, i) in tabs"
            :key="t.key"
            type="button"
            class="pd-tab"
            :class="{ 'is-on': i === activeTab }"
            role="tab"
            :aria-selected="i === activeTab"
            @click="activeTab = i"
          >
            <span class="pd-tab-i mono">0{{ i + 1 }}</span>{{ t.label }}
          </button>
        </div>

        <div v-for="(b, i) in shownPage.blocks" :key="i" class="pd-block">
          <!-- ★ 小结标题也走 rich()：这两页的标题里有 $S$ / $\Lambda$ 这类记号。
               ★ 段落里不写 Markdown 粗体（界面文案规范禁用，渲染器也不处理）。 -->
          <h5 v-html="rich(b.t)"></h5>
          <p v-html="rich(b.d)"></p>
        </div>

        <div v-if="shownPage.formulas && shownPage.formulas.length" class="pd-formulas">
          <div v-for="(f, i) in shownPage.formulas" :key="i" class="pd-f" v-html="rich(f)" />
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
.flow-head { position: relative; z-index: 3; display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; margin-bottom: 10px; }
.flow-kicker { position: relative; color: var(--text-3); font-size: 12px; letter-spacing: .14em; }
.flow-kicker::after {
  content: ''; position: absolute; left: 0; bottom: -4px; width: 46px; height: 1px;
  background: linear-gradient(90deg, var(--accent), transparent);
  animation: kicker-glow 4.6s ease-in-out infinite;
}
.flow-caption { color: var(--text-1); font-size: 15px; font-weight: 600; letter-spacing: -.01em; margin: 4px 0 0; }
.flow-state { display: inline-flex; align-items: center; gap: 7px; color: var(--text-3); font-size: 12px; white-space: nowrap; }
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
  /* ★ 紧凑一档（2026-10-17）：卡片只是目录，撑高了会把总览的「运行性能」挤下去。
     压的是内边距、图标与字号，**宽度留给文字** —— 六个标题里最长的
     「加密与密钥封装 / 增量聚合与分解」要能在一行里放下。 */
  position: relative; flex: 1 1 104px; min-width: 100px; padding: 8px 8px 9px;
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

.stage-topline { display: flex; justify-content: space-between; align-items: center; color: var(--text-3); font-size: 12px; letter-spacing: .08em; }
.stage-status { color: var(--stage-color); font-size: 12px; opacity: .8; }
.stage-index { color: var(--stage-color); animation: index-glow var(--cycle) ease-in-out infinite; animation-delay: calc(var(--i) * var(--step)); }
.icon-wrap { position: relative; width: 34px; height: 34px; margin: 5px auto 3px; display: grid; place-items: center; color: var(--stage-color); }
.stage-icon { width: 30px; height: 30px; overflow: visible; position: relative; z-index: 1; animation: icon-lift var(--cycle) cubic-bezier(.34, .01, .2, 1) infinite; animation-delay: calc(var(--i) * var(--step)); }
.icon-stroke { fill: none; stroke: currentColor; stroke-width: 2.5; stroke-linecap: round; stroke-linejoin: round; }
.icon-stroke.faint { opacity: .55; stroke-width: 1.8; }.icon-fill { fill: currentColor; }
.icon-pulse { position: absolute; inset: 6px; border: 1px solid currentColor; border-radius: 50%; opacity: .12; animation: icon-ring 2.4s ease-out infinite; animation-delay: calc(var(--i) * .8s); }
/* 图标外的冲击波：点亮的那一刻扩出去一圈 */
.icon-shock { position: absolute; inset: 3px; border: 1.5px solid currentColor; border-radius: 50%; opacity: 0; animation: icon-shock var(--cycle) cubic-bezier(.2, .7, .3, 1) infinite; animation-delay: calc(var(--i) * var(--step)); }
.stage-label { color: var(--text-1); font-size: 12px; font-weight: 600; text-align: center; white-space: nowrap; letter-spacing: -.01em; }
.stage-en { color: var(--stage-color); font-size: 11px; letter-spacing: .12em; text-align: center; margin-top: 2px; animation: en-glow var(--cycle) ease-in-out infinite; animation-delay: calc(var(--i) * var(--step)); }
.stage-detail { color: var(--text-3); font-size: 12px; line-height: 1.45; text-align: center; margin-top: 5px; min-height: 34px; }

/* ③ 块与块之间：轨道 + 流动虚线 + 彗星 + 依次亮起的箭头 */
.link { position: relative; z-index: 2; flex: 0 0 30px; height: 2px; align-self: center; margin: -24px 2px 0; }
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

/* 页签（只有带 tabs 的卡片才渲染）。用自绘而非 el-tabs：页数固定、
   配色要跟着 stage 颜色走，自绘更可控，也不引入额外的样式覆盖。 */
.pd-tabs {
  display: flex;
  gap: 6px;
  padding: 4px;
  border-radius: 10px;
  background: var(--bg-raised);
  border: 1px solid color-mix(in srgb, var(--stage-color) 20%, var(--line));
}
.pd-tab {
  flex: 1 1 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 7px 10px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--text-3);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
  transition: color .16s ease, background .16s ease;
}
.pd-tab:hover { color: var(--text-1); }
.pd-tab:focus-visible {
  outline: 2px solid color-mix(in srgb, var(--stage-color) 60%, transparent);
  outline-offset: 1px;
}
.pd-tab.is-on {
  color: var(--text-1);
  background: color-mix(in srgb, var(--stage-color) 16%, var(--bg-panel));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--stage-color) 40%, transparent);
}
.pd-tab-i { color: var(--stage-color); font-size: 13px; letter-spacing: .06em; }

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

/* LaTeX 子集渲染出的记号（渲染器见 utils/tex.js）。
   ★ 这里必须用 :deep()：公式是用 v-html 插进去的，
     那些节点上没有本组件的 scoped 属性，普通选择器匹配不到。 */
:deep(.tx-v) { font-style: italic; }
:deep(.tx-rm),
:deep(.tx-fn) { font-style: normal; }
:deep(.tx-fn) { margin-right: 1px; }
/* 行内公式整体不折行：一段公式被拆到两行会读不出结构。 */
:deep(.tx-inline) { white-space: nowrap; }
:deep(sup),
:deep(sub) { font-size: .72em; }
/* 分式竖排 */
:deep(.tx-frac) {
  display: inline-flex;
  flex-direction: column;
  vertical-align: middle;
  text-align: center;
  font-size: .92em;
  line-height: 1.15;
  margin: 0 1px;
}
:deep(.tx-frac > b) {
  border-top: 1px solid currentColor;
  font-weight: inherit;
}
:deep(.tx-over) {
  border-top: 1px solid currentColor;
  padding: 0 2px;
}

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
</style>
