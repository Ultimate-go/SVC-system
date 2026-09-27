r"""**测量**：跨文件批量验证到底值不值。

规划 §10 的 P1 第 7 条写着「跨文件**批量验证**（随机系数把多条方程合成一条模幂）」。
这个脚本就是去**量**它，而不是先把它实现出来再找理由。

要量的问题
----------

设手上有 m 份证据 :math:`\pi_{I_1},\dots,\pi_{I_m}`（可以来自不同文件、不同用户），
验它们有两条路：

* **逐份验**：对每份跑一遍 :func:`svc.verify`。每份的代价是一趟
  :func:`svc.add_back` 链 —— :math:`|I_j|` 次模幂，指数只有 :math:`\ell+1` 位；
* **批量验**：挑随机系数 :math:`\rho_j`，把 m 条方程合成一条：

  .. math::

     \prod_j \left(S_{I_j}^{\rho_j}\right)^{e_{I_j}} \;=\; U_n^{\sum_j \rho_j}

  也就是**摊成 m 次模幂，但每次的指数有** :math:`|I_j|\cdot(\ell+1)`
  **位**（因为要一次乘上 :math:`e_{I_j}` 这个连乘积）。

所以两者的模幂**次数差不多**，差别在**指数大小**：逐份验用的是"多次小指数"，
批量验用的是"少次大指数"。大指数模幂的多乘次数与其位数成正比，于是在
:math:`|I_j| > 1` 时两者应当**同阶**，批量不但省不下时间，还要多算一个连乘积。

这个脚本把上面这段话变成数字。

用法::

    python scripts/bench_batch.py                # 默认 64 块
    python scripts/bench_batch.py --blocks 128 --files 16
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("PYTHONWARNINGS", "ignore")

from core import L, new_session  # noqa: E402
from core.console import force_utf8_console  # noqa: E402
from core.store import PlainKeyStore  # noqa: E402
from svc import verify as svc_verify  # noqa: E402
from svc import verify_batch as svc_verify_batch  # noqa: E402

# 结论里要打印 ⇒（U+21D2），它**不在 GBK 里** —— 不先做这一步的话，
# 那行 print 会抛 UnicodeEncodeError，把结论埋在一堆 traceback 里。
force_utf8_console()


def e_of(primegen, indices) -> int:
    """\\(e_I = \\prod_{i\\in I} p_i\\) —— 把下标集合乘成一个整数。"""
    e = 1
    for i in indices:
        e *= primegen.get(i)
    return e


def build(blocks: int, files: int, *, modulus_bits: int = 1024):
    """造一个刚好 ``blocks`` 块、摊在 ``files`` 个文件上的向量。"""
    seg = 1024
    per = max(1, blocks // files)
    n_max = 1 << max(4, (blocks * 4).bit_length())
    session = new_session(l=L, n_max=n_max, modulus_bits=modulus_bits, seed=b"vds-batch")
    store = PlainKeyStore(session, segment_bytes=seg)
    made = 0
    i = 0
    while made < blocks:
        take = min(per, blocks - made)
        store.upload(f"u{i % 3}", f"f{i}", bytes((i + 1) % 251 for _ in range(take * seg)))
        made += take
        i += 1
    return store


def timed(fn):
    t0 = time.perf_counter()
    out = fn()
    return out, (time.perf_counter() - t0) * 1000.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="批量验证的代价测量")
    ap.add_argument("--blocks", type=int, default=64)
    ap.add_argument("--files", type=int, default=8)
    ap.add_argument("--modulus-bits", type=int, default=1024)
    args = ap.parse_args(argv)

    store = build(args.blocks, args.files, modulus_bits=args.modulus_bits)
    n = store.n
    crs_n = store.session.crs_n_for(store.delta)
    print(f"配置：|N| = {args.modulus_bits}、l = {L}、n_max = {store.session.n_max}、"
          f"块数 n = {n}、文件数 = {args.files}")

    # ------------------------------------------------------------------
    # ① 一次性查全部 —— 设计 B 的主路径：一份证据覆盖所有块
    # ------------------------------------------------------------------
    _, one_ms = timed(lambda: store.query(list(range(n))))
    print(f"\n① 一次查全部 {n} 块（一份证据）：          {one_ms:8.1f} ms")

    # ------------------------------------------------------------------
    # ② 逐块各查一次 —— 拿 m 份**单块**证据，并单独量验证那一部分
    # ------------------------------------------------------------------
    singles = []
    per_query = []

    def gather():
        for i in range(n):
            t0 = time.perf_counter()
            r = store.query([i])
            per_query.append((time.perf_counter() - t0) * 1000.0)
            singles.append((r.indices, r.values, r.proof))

    _, many_ms = timed(gather)
    print(f"② 逐块各查一次（{n} 份单块证据）：        {many_ms:8.1f} ms"
          f"   —— 平均每份 {many_ms / n:.1f} ms")
    print("   （其中每份都含一次「生成证明」；批量验证省不掉这一段）")
    # 只量 ``svc.verify`` 本身：这才是"批量"能碰的那一段
    C = store.delta.C
    cases = [
        (list(I), list(v), pi) for (I, v, pi) in singles
    ]
    _, verify_sep_ms = timed(
        lambda: [svc_verify(crs_n, C, I, v, pi) for (I, v, pi) in cases]
    )
    print(f"\n③ 逐份 svc.verify（{n} 份单块证据）：       {verify_sep_ms:8.1f} ms"
          f"   —— 平均每份 {verify_sep_ms / n:.2f} ms")

    # ------------------------------------------------------------------
    # ④ 批量验 —— **直接调库里发货的那个函数**
    #
    #    ★ 一开始这里是把"批量方程"原型写在脚本里量的；后来改成直接调
    #      ``svc.verify_batch`` —— 否则量的不是发货的那段代码。
    #    ★ 它**两条信道都合**（ok_s / ok_lambda 分开报）：只合 S 那条会
    #      "看起来快一大截"，那是因为它少验了一半，这种对比是偷工减料。
    # ------------------------------------------------------------------
    rep, batch_ms = timed(lambda: svc_verify_batch(crs_n, C, cases))
    print(
        f"④ 批量验 svc.verify_batch：             {batch_ms:8.1f} ms"
        f"   —— {'通过' if rep.ok else '不通过（实现有问题）'}"
        f"（ok_s = {rep.ok_s}、ok_lambda = {rep.ok_lambda}）"
    )

    # --- 反面对照：故意改掉一个值，批量检查必须报错，而且要指名 ---
    bad = list(cases)
    I0, v0, pi0 = bad[0]
    bad[0] = (I0, [int(v0[0]) ^ 1, *v0[1:]], pi0)
    bad_rep, _ = timed(lambda: svc_verify_batch(crs_n, C, bad))
    located = [j + 1 for j in bad_rep.bad]
    print(
        f"   反面对照：改掉一个值 → {'被拒（对）' if not bad_rep.ok else '仍然通过（错！）'}"
        f"，逐份定位到第 {located} 份"
    )
    assert not bad_rep.ok, "反面对照没被拦下 —— 批量验证等于没在验"
    assert bad_rep.bad == (0,), f"定位错了：{bad_rep.bad}"

    # ------------------------------------------------------------------
    # ⑤ 主路径的对照组：**一份**覆盖全部 64 块的证据，验证要多久
    # ------------------------------------------------------------------
    big = store.query(list(range(n)))
    _, big_verify_ms = timed(
        lambda: svc_verify(crs_n, C, list(big.indices), list(big.values), big.proof)
    )
    print(f"\n⑤ 对照：验证**一份**覆盖 {n} 块的证据：    {big_verify_ms:8.1f} ms"
          f"   —— 主路径上“验证”那一栏的真实代价")

    # ------------------------------------------------------------------
    # 结论
    # ------------------------------------------------------------------
    print("\n" + "=" * 72)
    print(f"主路径（一份证据覆盖 {n} 块）")
    print(f"  查询整体 {one_ms:8.1f} ms，其中生成证明占大头；验证 {big_verify_ms:8.1f} ms")
    print(f"逐块各查一次：{many_ms:8.1f} ms —— 比合成一份慢 {many_ms / one_ms:.1f} 倍")
    print("\n「验 m 份已有证据」这个场景")
    print(f"  逐份 svc.verify      ：{verify_sep_ms:8.1f} ms（{len(cases)} 份）")
    print(f"  批量（两条信道都合）：{batch_ms:8.1f} ms")
    ratio = batch_ms / verify_sep_ms if verify_sep_ms else float("inf")
    print(f"  ⇒ 批量{'快' if ratio < 1 else '慢'} {abs(1 / ratio if ratio < 1 else ratio):.2f} 倍")
    print("=" * 72)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
