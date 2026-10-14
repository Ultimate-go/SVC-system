/**
 * 极简 LaTeX 子集渲染器 —— 只覆盖界面知识点里真正用到的那几种记号。
 *
 * ★ 为什么自己写而不引 KaTeX
 * ---------------------------
 * 本项目的界面依赖只有 Vue / Element Plus / Pinia / axios（图表也是自研 SVG）。
 * 为了几个下标、上标和希腊字母引入一整套 TeX 排版引擎（连同它的字体文件）不值当，
 * 而知识点里的公式远没到需要真排版的程度。这里只做**转义 + 下标上标 + 符号表**。
 *
 * 支持的写法（够用即可，多余的会原样显示而不是报错）
 * --------------------------------------------------
 * * 上标 / 下标：``x^2``、``S_i``、``S_{I}^{e_I}``（大括号分组，可嵌套）
 * * 分式：``\\frac{a}{b}``（竖排）
 * * 希腊字母与常用符号：``\\delta`` ``\\pi`` ``\\Lambda`` ``\\ell`` ``\\varphi``
 *   ``\\cdot`` ``\\times`` ``\\prod`` ``\\mapsto`` ``\\ne`` ``\\le`` ``\\equiv``
 *   ``\\in`` ``\\cup`` ``\\emptyset`` ``\\ldots`` …（见下表）
 * * 正体：``\\mathrm{SM3}``、``\\text{第 j 段}``、``\\operatorname{SM3}``
 * * 空格：``\\,`` ``\\;`` ``\\ `` ``\\quad``
 * * 单字母变量自动排成斜体（数学惯例），命令名与正体内容不斜。
 *
 * ★ 安全性：输入只来自本仓库源码里的常量文案，没有任何用户输入经过这里；
 *   即便如此仍然先做 HTML 转义，再拼标签 —— 不让一条文案有机会注入标签。
 */

/** 符号表：LaTeX 命令名 → 显示的字符。加号表示「只是写法不同，显示同一个字符」。 */
const SYM = {
  // 希腊字母
  alpha: 'α', beta: 'β', gamma: 'γ', Gamma: 'Γ', delta: 'δ', Delta: 'Δ',
  epsilon: 'ε', varepsilon: 'ε', zeta: 'ζ', eta: 'η', theta: 'θ', Theta: 'Θ',
  iota: 'ι', kappa: 'κ', lambda: 'λ', Lambda: 'Λ', mu: 'μ', nu: 'ν',
  xi: 'ξ', Xi: 'Ξ', pi: 'π', Pi: 'Π', rho: 'ρ', sigma: 'σ', Sigma: 'Σ',
  tau: 'τ', upsilon: 'υ', phi: 'φ', varphi: 'φ', Phi: 'Φ', chi: 'χ',
  psi: 'ψ', Psi: 'Ψ', omega: 'ω', Omega: 'Ω', ell: 'ℓ',
  // 运算与关系
  cdot: '·', times: '×', div: '÷', pm: '±', mp: '∓', ast: '∗',
  prod: '∏', sum: '∑', int: '∫', circ: '∘', oplus: '⊕',
  mapsto: '↦', to: '→', rightarrow: '→', leftarrow: '←', Rightarrow: '⇒',
  ne: '≠', neq: '≠', equiv: '≡', approx: '≈', le: '≤', leq: '≤',
  ge: '≥', geq: '≥', ll: '≪', gg: '≫', propto: '∝',
  in: '∈', notin: '∉', ni: '∋', subset: '⊂', subseteq: '⊆', supseteq: '⊇',
  cup: '∪', cap: '∩', setminus: '∖', emptyset: '∅', varnothing: '∅',
  forall: '∀', exists: '∃', neg: '¬', land: '∧', lor: '∨',
  // 省略号与空格
  ldots: '…', cdots: '⋯', dots: '…', vdots: '⋮',
  quad: '　', qquad: '　　',
  ',': ' ', ';': ' ', ':': ' ', '!': '', ' ': ' ',
}

/** 函数名：排正体，且与后面的参数之间自动留一个细空格。 */
const FUNCS = new Set(['gcd', 'bmod', 'pmod', 'mod', 'log', 'ln', 'exp', 'max', 'min', 'deg'])

/** 按字面量显示、不做任何排版处理的命令（``\mathrm{}`` 这一类）。 */
const VERBATIM_CMDS = new Set(['mathrm', 'text', 'operatorname', 'mathit'])

function esc(text) {
  return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

/**
 * 读一个「组」：``{...}`` 取花括号内（支持嵌套）；否则取单个字符或一整条命令。
 *
 * :returns: ``{ body, next }`` —— ``body`` 是组内容，``next`` 是组结束后的位置。
 */
function readGroup(src, i) {
  if (src[i] === '{') {
    let depth = 0
    for (let j = i; j < src.length; j += 1) {
      if (src[j] === '{') depth += 1
      else if (src[j] === '}') {
        depth -= 1
        if (depth === 0) return { body: src.slice(i + 1, j), next: j + 1 }
      }
    }
    // 括号不配对：把剩下的都当组内容（宁可显示全，也不要吞掉半截公式）
    return { body: src.slice(i + 1), next: src.length }
  }
  if (src[i] === '\\') {
    const m = /^\\([A-Za-z]+|[\s\S])/.exec(src.slice(i))
    if (m) return { body: m[0], next: i + m[0].length }
  }
  return { body: src[i] || '', next: i + 1 }
}

/** 递归渲染一段 LaTeX（返回 HTML 字符串）。 */
function tex(src) {
  const s = String(src ?? '')
  let out = ''
  let i = 0
  while (i < s.length) {
    const c = s[i]

    // 命令
    if (c === '\\') {
      const m = /^\\([A-Za-z]+|[\s\S])/.exec(s.slice(i))
      if (!m) {
        out += esc(c)
        i += 1
        continue
      }
      const name = m[1]
      i += m[0].length

      if (VERBATIM_CMDS.has(name)) {
        const g = readGroup(s, i)
        i = g.next
        out += `<span class="tx-rm">${esc(g.body)}</span>`
      } else if (name === 'frac' || name === 'dfrac') {
        const a = readGroup(s, i)
        const b = readGroup(s, a.next)
        i = b.next
        out += `<span class="tx-frac"><i>${tex(a.body)}</i><b>${tex(b.body)}</b></span>`
      } else if (name === 'sqrt') {
        const g = readGroup(s, i)
        i = g.next
        out += `√<span class="tx-over">${tex(g.body)}</span>`
      } else if (FUNCS.has(name)) {
        // ``\bmod`` 是「二元运算的 mod」：惯例上显示为 ``mod``（前面留一个细空格），
        // 而不是把命令名原样摊出来。
        if (name === 'bmod') out += '&thinsp;<span class="tx-fn">mod</span>&thinsp;'
        else out += `<span class="tx-fn">${name}</span>&thinsp;`
      } else if (Object.prototype.hasOwnProperty.call(SYM, name)) {
        out += SYM[name]
      } else {
        // 不认识的一律原样显示（不静默吞掉，便于一眼看出漏支持了哪个）
        out += esc(name)
      }
      continue
    }

    // 上标 / 下标
    if (c === '^' || c === '_') {
      const g = readGroup(s, i + 1)
      i = g.next
      out += c === '^' ? `<sup>${tex(g.body)}</sup>` : `<sub>${tex(g.body)}</sub>`
      continue
    }

    if (c === '{') {
      const g = readGroup(s, i)
      i = g.next
      out += tex(g.body)
      continue
    }
    if (c === '}') {
      i += 1
      continue
    }

    // 单个拉丁字母按变量排斜体（数学惯例）；连续字母要靠 \mathrm 才会连排
    if (/[A-Za-z]/.test(c)) {
      out += `<i class="tx-v">${c}</i>`
      i += 1
      continue
    }

    out += esc(c)
    i += 1
  }
  return out
}

/**
 * 渲染一整段文案：``$...$`` 之间按 LaTeX 排，其余按普通文字（先转义）。
 *
 * 用法：``<p v-html="rich(text)" />`` —— 文案是源码里的常量，不经用户输入。
 */
export function rich(text) {
  const s = String(text ?? '')
  let out = ''
  let i = 0
  while (i < s.length) {
    const open = s.indexOf('$', i)
    if (open < 0) {
      out += esc(s.slice(i))
      break
    }
    out += esc(s.slice(i, open))
    const close = s.indexOf('$', open + 1)
    if (close < 0) {
      // 只有半个 $：原样显示，别把后面整段吞掉
      out += esc(s.slice(open))
      break
    }
    out += `<span class="tx-inline">${tex(s.slice(open + 1, close))}</span>`
    i = close + 1
  }
  return out
}

/** 只渲染纯公式（整行都是 LaTeX 时用这个）。 */
export function texOnly(text) {
  return tex(text)
}

export default { rich, texOnly }
