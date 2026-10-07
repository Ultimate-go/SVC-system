/**
 * 客户端锚定（anchor）—— 「服务器给我的内容有没有被换过」的**本地**判据。
 *
 * ★ 为什么必须有它：
 *   这一版的解密在**服务端**做（`POST /api/files/{id}/decrypt`，私钥是登录时用口令
 *   解封后扣在后端内存里的），所以"明文"这件事客户端没有独立来源 ——
 *   服务器说什么就是什么。要让"服务器不可信"这个前提真正落地，客户端必须自己
 *   留一份**独立记录**：写块时（内容是自己给的，不需要信任服务器）把
 *   `SHA-256(明文)` 存进 localStorage；以后每次拿到内容都来对一遍，
 *   被替换 / 被回滚就会当场暴露。
 *
 * ★ 阶段 2 之后，这里住着**两层**锚（安全审计 S4 的修复）：
 *
 *   ① **内容锚**（``getAnchor`` / ``setAnchor`` …）—— 某一块的**明文 SHA-256**。
 *      写块时（内容是自己给的，不依赖任何服务器）记下来；
 *      以后拿到内容就对一遍，被替换 / 被回滚当场暴露。
 *
 *   ② **δ 锚**（``getDeltaAnchor`` / ``setDeltaAnchor`` …）—— 这份文件的摘要
 *      :math:`\delta = (U, C, n)` 的**本体**（不是指纹）。
 *
 *      ★ 为什么必须有它：浏览器验证证据时，"目标值" ``(U_n, C)`` 如果
 *      **全部取自服务端返回的那一份**，那么一个被控的服务端可以自选
 *      ``(l, N, g, 素数表, δ, S_I, Λ_I)``，为**任意密文**重算出一份自洽的证据 ——
 *      ``addBack`` 链必然给出 ``ok = true``。两边都是攻击者算的，实现再正确也没用。
 *
 *      所以第一次见证某份文件时，把 **δ 本体**钉在本地；此后一律**用本地 δ 验证**，
 *      与远端 δ 不一致就单独报出来（见 ``crypto/index.js`` 的 ``deltaVerdict``）。
 *      这与论文一致：``ClntNode`` 本来就只有 ``δ = ((U, C), n)``。
 *
 * ★ 两层各证明什么、不证明什么（界面上必须同时说清，不能含糊）：
 *   * 内容锚：现在这一块与「我上次见证的那一份」逐字节一致；
 *   * δ 锚：交付的密文与**我见证过的那个版本**自洽（而不是与服务端现编的版本自洽）。
 *   * 两层都**不**证明：我第一次见证的内容就是**原始**内容 ——
 *     那要求"我见证的那一刻"本身可信（TOFU：首次使用即信任）。
 *     但第一次之后，任何替换 / 回滚 / 换 δ 都会被抓出来。
 *
 * ★ 为什么用 WebCrypto 的 SHA-256 而不是 SM3：这只是**客户端自己的记账口径**，
 *   不需要与承诺里的任何哈希对齐（承诺那边用的是 SM3）。零依赖优先；
 *   而 `crypto.subtle` 在 http://127.0.0.1 这种本地来源上可用（localhost 属安全上下文）。
 */

const KEY = 'vds_anchor_v1';

/** localStorage 里的全部锚：``"<fileId>:<blockIdx>" → 记录``。 */
function readAll() {
  try {
    const raw = localStorage.getItem(KEY);
    const o = raw ? JSON.parse(raw) : {};
    return o && typeof o === 'object' ? o : {};
  } catch {
    return {};
  }
}

function writeAll(o) {
  try {
    localStorage.setItem(KEY, JSON.stringify(o));
  } catch {
    /* 配额满或被禁用：锚是"尽力而为"的本地记账，写不进去不能影响主流程。 */
  }
}

export const anchorKey = (fileId, blockIdx) => `${fileId}:${blockIdx}`;

/** 取一块的锚（没有就返回 null）。 */
export function getAnchor(fileId, blockIdx) {
  return readAll()[anchorKey(fileId, blockIdx)] || null;
}

/**
 * 记/更新一块的锚。
 *
 * :param sha256: 明文的 SHA-256（十六进制）—— 判据本体
 * :param version: 记锚时文件的版本号（用于解释"期间有别的改动"）
 * :param deltaFp: 记锚时的全局 δ 指纹（回滚检测用）
 * :param source: 人类可读的来源（"上传"/"改块"/"清零"…），界面要显示它
 */
export function setAnchor(fileId, blockIdx, { sha256, version = null, deltaFp = null, bytes = null, source = '' }) {
  const all = readAll();
  const key = anchorKey(fileId, blockIdx);
  const prev = all[key];
  // ★★ 记住**上几个版本**的明文哈希。
  //
  //   为什么要留历史：服务器完全可能把“改块**前**的那份密文”再交回来
  //   （缓存、回滚、或者干脆是恶意）。只留当前一份哈希的话，我们能说出的
  //   最多是“这和我最后一次见到的不同”—— 而拿到旧数据的人真正需要知道的
  //   是「**它就是我改之前那一版**」，那是**完全不同性质**的一句话：
  //   前者是“内容不对劲”，后者是“服务器在拿旧数据搪塞我”。
  //   留 4 条足够覆盖“改了几次又回滚”的常见情形。
  const history = Array.isArray(prev?.history) ? prev.history.slice(0, 4) : [];
  if (prev?.sha256 && prev.sha256 !== sha256) {
    history.unshift({
      sha256: prev.sha256,
      version: prev.version ?? null,
      source: prev.source || '',
      ts: prev.ts || null,
    });
  }
  all[key] = {
    sha256,
    version,
    delta_fp: deltaFp,
    bytes,
    source,
    //: 历史版本（最近在前）。旧数据检测靠它（见 :func:`matchHistory`）。
    history: history.slice(0, 4),
    ts: Date.now(),
  };
  writeAll(all);
}

/**
 * 这份内容是不是“我以前见过、但**不是现在这一版**”的（= 旧数据）。
 *
 * ★ 用它的地方：详情页拿到内容、算完 SHA-256 之后，先看当前锚对不对；
 *   不对就再问一句“那它是不是命中了我以前的某一版”。命中的话，界面上
 *   说的就不是含糊的“内容与记录不一致”，而是明确的
 *   **“这是第 X 版之前的内容（服务器给了旧数据）”**。
 *
 * @param {object|null} anchor 当前锚
 * @param {string} sha256 刚算出来的明文哈希
 * @returns {{isOld: boolean, record: object|null}}
 */
export function matchHistory(anchor, sha256) {
  if (!anchor || !sha256) return { isOld: false, record: null };
  const hit = (anchor.history || []).find((h) => h.sha256 === sha256);
  return hit ? { isOld: true, record: hit } : { isOld: false, record: null };
}

/** 删掉一块的锚（例如它已不存在）。 */
export function dropAnchor(fileId, blockIdx) {
  const all = readAll();
  delete all[anchorKey(fileId, blockIdx)];
  writeAll(all);
}

/** 某份文件在**本浏览器**里已锚定的块：``{ blockIdx: 记录 }``。 */
export function anchorsOf(fileId) {
  const out = {};
  for (const [k, v] of Object.entries(readAll())) {
    const [fid, bi] = k.split(':');
    if (Number(fid) === Number(fileId)) out[bi] = v;
  }
  return out;
}
// ---------------------------------------------------------------------------
// 第二层：δ 锚（这份文件的摘要本体）—— 安全审计 S4
// ---------------------------------------------------------------------------

const DELTA_KEY = 'vds_delta_anchor_v1';

function readDeltas() {
  try {
    const raw = localStorage.getItem(DELTA_KEY);
    const o = raw ? JSON.parse(raw) : {};
    return o && typeof o === 'object' ? o : {};
  } catch {
    return {};
  }
}

function writeDeltas(o) {
  try {
    localStorage.setItem(DELTA_KEY, JSON.stringify(o));
  } catch {
    /* 同内容锚：写不进去不能影响主流程。 */
  }
}

/**
 * 取这份文件**本地钉住的 δ**（没有就返回 null）。
 *
 * @param {number|string} fileId
 * @returns {{U: string, C: string, n: number, fp: string, source: string, ts: number}|null}
 */
export function getDeltaAnchor(fileId) {
  return readDeltas()[String(fileId)] || null;
}

/**
 * 取这份文件的 δ 锚，**并核对它确实属于这一份内容**。
 *
 * ★ 为什么不能只用 :func:`getDeltaAnchor`：``fileId`` 会被数据库复用
 *   （见 :func:`setDeltaAnchor` 的说明）。旧文件的锚留在本地、新文件拿到
 *   同一个 id 时，**拿旧基准去验新文件**会误报“验证不通过” ——
 *   而那看起来像“服务器给了坏数据”，实际上服务器没问题。
 *
 * 规则：
 *
 * * 锚里**没有** ``digest``（更早的版本写的）⇒ 判不出归属。这时**仍然认它**
 *   （否则老用户的钉扎会全部失效），只是不享受这层保护；下一次写入会补上。
 * * 锚里有 ``digest``、且**与当前内容不同** ⇒ 判定为**上一份文件的锚**，
 *   顺手丢掉并返回 ``null``（等于从没钉过 ⇒ 走“首次见证”）。
 *
 * @param {number|string} fileId
 * @param {string|null|undefined} contentDigest 当前文件的 ``content_digest``
 * @returns {object|null}
 */
export function getDeltaAnchorFor(fileId, contentDigest) {
  const a = getDeltaAnchor(fileId);
  if (!a) return null;
  if (a.digest && contentDigest && String(a.digest) !== String(contentDigest)) {
    dropDeltaAnchor(fileId);
    return null;
  }
  return a;
}

/**
 * 丢掉这份文件**所有**的本地记录：δ 锚 + 每一块的内容锚。
 *
 * ★ 什么时候用：**删掉文件**之后。不丢的话，``fileId`` 被库复用会让新文件
 *   继承旧基准 —— δ 锚导致“验证不通过”误报，内容锚导致“这是旧数据”误报。
 *
 * @param {number|string} fileId
 */
export function forgetAnchorsOf(fileId) {
  dropDeltaAnchor(fileId);
  for (const bi of Object.keys(anchorsOf(fileId))) dropAnchor(fileId, bi);
}

/**
 * 把这份文件的 δ **本体**钉在本地。
 *
 * ★ 存的是 ``U`` / ``C`` 的十进制串本体，不是指纹 —— 验证要用它们做模幂，
 *   指纹只能告诉你"变没变"，不能拿来验。
 *
 * @param {string} U 摘要的 ``U``（十进制串）
 * @param {string} C 摘要的 ``C``（十进制串）
 * @param {number} n 记锚时的向量长度
 * @param {string} source 人类可读来源（界面要显示"这是你什么时候见证的"）
 */
export function setDeltaAnchor(
  fileId,
  { U, C, n = null, fp = null, source = '', N = null, crsFp = null, digest = null },
) {
  if (!U || !C) return;
  const all = readDeltas();
  all[String(fileId)] = {
    U: String(U),
    C: String(C),
    n,
    fp,
    source,
    // ★★ **这份锚属于哪一份文件**（``files.content_digest``）。
    //
    //   为什么必须有：锚是按 ``fileId``（数据库自增主键）索引的，而 SQLite 的
    //   ``INTEGER PRIMARY KEY`` **没有** AUTOINCREMENT —— 删掉最后一行之后，
    //   新插入的账目会**复用同一个 id**。于是“删掉 A、再传 B”能让 B 拿到
    //   A 的 id；解密时就会**拿 A 的基准去验 B**，报“本地验证未通过”。
    //   实测：**改名也躲不开**，因为锚只认数字、不认名字。
    //
    //   存下 ``content_digest`` 之后，取锚时先比对（见 ``getDeltaAnchorFor``）：
    //   不是同一份内容就当**没锚**，走“首次见证”。
    digest: digest == null ? null : String(digest),
    // ★★ 群参数 N **必须一起钉住**（安全审计 S1）：验证等式
    //   ``S == U_n``、``Λ == C (mod N)`` 里的 ``N`` 本身就是方程的一部分。
    //   只钉 ``(U, C)`` 等于把“考场”留给服务端去定 —— 它可以自选一个
    //   **已知阶**的 ``N'``（例如 2048 位素数，则群阶 ``N'-1`` 已知），
    //   再用已知阶反解出 ``S_I`` / ``Λ_I``，使 ``addBack`` 链**恰好**
    //   收敛到钉住的 ``(U*, C*)``：于是“验证通过”与“δ 一致”同时成立，
    //   而密文是它随便给的。钉住 ``N`` 之后，“换 N”当场变成
    //   ``crsVerdict === 'mismatch'`` 而不是静默通过。
    N: N == null ? null : String(N),
    crs_fp: crsFp,
    ts: Date.now(),
  };
  writeDeltas(all);
}

/**
 * 丢掉这份文件的 δ 锚。
 *
 * ★ 什么时候该丢：**你自己**改动了这份文件（改块 / 清零 / 追加 / 截断）——
 *   那之后 δ 必然变，而"新 δ 是什么"只有服务端能告诉你。
 *   此时把锚丢掉（而不是默默跟着更新），让下一次解密变成"重新见证"：
 *   要么你自己确认一下新版本，要么就让它一直处于"未钉扎"状态（界面会如实标出）。
 */
export function dropDeltaAnchor(fileId) {
  const all = readDeltas();
  delete all[String(fileId)];
  writeDeltas(all);
}

/** 本浏览器已钉扎 δ 的文件 id 列表（个人中心 / 调试用）。 */
export function deltasOf() {
  return Object.keys(readDeltas());
}

// ---------------------------------------------------------------------------
// 第三层：群参数锚（**系统级**公开参数）—— 安全审计 S1 / P0-2
// ---------------------------------------------------------------------------
//
// ★ 为什么单开一层，而不是塞进 δ 锚：
//   ``N``（以及 ``l`` / 素数基线的位长与起点）是**整套部署共用**的参数，
//   与“哪一份文件”无关。攻击者拿一个自选的 ``N'`` 能让**任意**密文通过，
//   所以要用一份**全局**锚去锁它；每份文件各存一份只是冗余，不是主机制。
//
// ★ 这是 TOFU（首次使用即信任）：**第一次**见到的参数就是我认下的那个。
//   它能抓住“服务端**中途**换参数”，抓不住“从第一天就给恶意参数” ——
//   后者需要 ``N`` 有**独立于服务端**的来源（部署配置 / 出带分发 /
//   客户端自生成）。这条边界必须写在界面上，不能含糊过去。

const CRS_KEY = 'vds_crs_v1';

function readCrs() {
  try {
    const raw = localStorage.getItem(CRS_KEY);
    const o = raw ? JSON.parse(raw) : null;
    return o && typeof o === 'object' ? o : null;
  } catch {
    return null;
  }
}

/**
 * 取本地钉住的**系统级群参数**（没记过就返回 ``null``）。
 *
 * @returns {{N: string, l: number|null, prime_bits: number|null,
 *            prime_start: string|null, source: string, ts: number}|null}
 */
export function getCrsAnchor() {
  return readCrs();
}

/**
 * 记下系统级群参数。
 *
 * ★ 只该由**首次见证**调用（本地还没有锚时）—— 已经有锚还去覆盖，
 *   等于把 TOFU 的起点让给服务端，整套锚就白钉了。
 */
export function setCrsAnchor({ N, l = null, prime_bits = null, prime_start = null, source = '' }) {
  if (!N) return;
  try {
    localStorage.setItem(
      CRS_KEY,
      JSON.stringify({
        N: String(N),
        l,
        prime_bits,
        prime_start: prime_start == null ? null : String(prime_start),
        source,
        ts: Date.now(),
      }),
    );
  } catch {
    /* 与其它锚一致：写不进去不影响主流程（锚是“尽力而为”的本地记账）。 */
  }
}

/**
 * 丢掉系统参数锚。
 *
 * ★ 什么时候该丢：**换了部署**（服务端重建了群参数）而你自己知道这件事。
 *   换完再解密一次就是“重新见证”。
 */
export function dropCrsAnchor() {
  try {
    localStorage.removeItem(CRS_KEY);
  } catch {
    /* ignore */
  }
}

/**
 * 群参数的**短指纹**（界面上长期可见用，好让“变没变”一眼看出）。
 *
 * ★ 这里刻意不做哈希：WebCrypto 的摘要函数是**异步**的，而这个指纹要在
 *   模板里同步显示；位数 + 头尾已经足够回答“和我上次见的是不是同一个”。
 */
export function crsFingerprint(N) {
  const s = String(N ?? '');
  if (!s) return '—';
  if (s.length <= 28) return s;
  return `${s.length} 位 · ${s.slice(0, 16)}…${s.slice(-8)}`;
}
/** ``SHA-256(bytes)`` → 十六进制小写（WebCrypto，零依赖）。 */
export async function sha256Hex(bytes) {
  const buf = await crypto.subtle.digest('SHA-256', bytes);
  const arr = new Uint8Array(buf);
  let s = '';
  for (const b of arr) s += b.toString(16).padStart(2, '0');
  return s;
}

/** 锚的时间戳 → 人话（"3 分钟前"）。界面要显示它，好让用户判断"这是我什么时候的见证"。 */
export function fmtWhen(ts) {
  if (!ts) return '—';
  const dt = Date.now() - ts;
  if (dt < 60_000) return '刚刚';
  if (dt < 3_600_000) return `${Math.floor(dt / 60_000)} 分钟前`;
  if (dt < 86_400_000) return `${Math.floor(dt / 3_600_000)} 小时前`;
  return new Date(ts).toLocaleString('zh-CN');
}
