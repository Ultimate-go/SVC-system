"""标定脚本：**块大小选多少最划算**（用本项目自己的实现实测，不照搬旧仓库）。

为什么必须有它
--------------
旧仓库的 ``tune_for`` 是按**旧系统**标定的（16 字节一块），它那条
U 形代价曲线属于"块小到每块固定开销压不住"的那个区间。本项目的块是
**1024 字节起**、每块还要单独封装一次块密钥，量级完全不同 ——
照搬会给出错的数量级。所以这里实测一遍，让"推荐"有据可依。

测什么
------
对每个候选块大小，各起一个干净的向量，量四件事：

1. **上传**（切块 + 逐块 SM4 + 承诺 + 推到 4 台节点，走 :class:`~core.transport.LocalTransport`）
2. **一次验完整条向量**（``store.query(range(n))`` —— 检索 + 聚合 + 两层验证）
3. **证据多大**（群元素个数 × 元素字节数）
4. **反面对照**：偷改一个分量之后，同一次查询**必须失败**
   （不做这一步的话，"很快"可能只是因为它没在验）

跑法（工作区根目录）::

    <python> scripts/bench_blocksize.py
    <python> scripts/bench_blocksize.py --size 65536 --sizes 512,1024,2048,4096

.. warning::

   这是**标定**脚本，不是压测：用的是测试档公开参数（``|N| = 512`` 位）
   与单进程节点。★ 数字量于 2026-09-26，当时**每块还含一次 ABE 策略封装**；
   现在封装换成了更便宜的 ECIES，所以下表是**上界**。
   数字是**本机**的，换机器要重跑。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import L, new_session  # noqa: E402
from core.store import PlainKeyStore  # noqa: E402
# ★ 下面两个助手原来是 ``from tests.conftest import TEST_NODES, blob``。
#   2026-09-27 本副本移除了测试目录，所以**内联到这里** —— 这份基准脚本被
#   ``backend/advisor.py`` 的「实测来源」与 ``架构说明.md`` 引用着，它必须能
#   **独立跑起来**（``python scripts/bench_blocksize.py``），否则那两处引用
#   就成了悬空出处。算法原样照拄，所以数字仍可复现。
#: 标定用的节点名（与演示部署一致：4 台）。
TEST_NODES = ("node-1", "node-2", "node-3", "node-4")


def blob(size: int, seed: int = 0) -> bytes:
    """造一段**确定性的伪随机**数据（与原 ``tests.conftest.blob`` 逐字节一致）。

    不用 ``os.urandom`` 是为了失败可复现；这里的数据只是"内容"，
    真正的随机性在 SM4 的密钥与 IV 上（那些走 ``os.urandom``）。
    """
    out = bytearray(size)
    x = (seed * 2654435761) & 0xFFFFFFFF
    for i in range(size):
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        out[i] = (x >> 16) & 0xFF
    return bytes(out)

#: 本机标定用的公开参数（测试档，跑得快）。生产档 |N| 更大，绝对耗时同比例放大。
N_MAX = 4096
MODULUS_BITS = 512


def payload(nbytes: int, seed: int = 7) -> bytes:
    """确定性伪随机内容 —— 用 :func:`blob` 的同一套算法，可复现。"""
    return blob(nbytes, seed)


def bench_one(session, data: bytes, seg: int) -> dict:
    """对一种块大小做一次完整测量，返回这一行。"""
    nodes = 4
    store = PlainKeyStore(session, node_ids=TEST_NODES[:nodes], segment_bytes=seg)

    t0 = time.perf_counter()
    rec = store.upload("u1", "f1", data)
    t_upload = (time.perf_counter() - t0) * 1000.0

    indices = list(range(store.n))
    t0 = time.perf_counter()
    r = store.query(indices)
    t_verify = (time.perf_counter() - t0) * 1000.0
    assert r.ok, f"块大小 {seg} 的一次全量验证没通过：{r.summary()}"

    # 反面对照：偷改一个分量，同一次查询**必须**不通过。
    #
    # ★ 两种"不通过"都算抓到，而且**必须都接受**：
    #   * 聚合阶段先炸（``ShamirTrick 合并 Λ 失败``）—— 分量对不上，这条链当场闭不上；
    #   * 或者聚合过了、但两层验证判 ``ok=False``。
    #   第一版只认第二种，于是脚本自己在负面对照上抛异常退出了
    #   （实测：块多的时候走的正是"聚合先炸"那条）。
    victim = store.holder_of(indices[0])
    state = store.transport.states[victim]
    state.tamper_value(indices[0], 1)
    try:
        tampered_ok: bool | None = store.query(indices).ok
    except Exception:  # noqa: BLE001 - 聚合阶段直接炸也算抓到
        tampered_ok = None
    state.tamper_value(indices[0], -1)  # 改回来（虽然这一轮之后就不再用了）

    return {
        "seg": seg,
        "blocks": rec.block_count,
        "upload_ms": t_upload,
        "upload_per_block": t_upload / max(1, rec.block_count),
        "verify_ms": t_verify,
        "verify_per_block": t_verify / max(1, rec.block_count),
        "proof_bytes": r.proof_size_bytes,
        "tamper_caught": tampered_ok is not True,
        "tamper_way": "聚合阶段先炸" if tampered_ok is None else ("验证判不通过" if tampered_ok is False else "漏网!"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="块大小标定（本机实测）")
    ap.add_argument("--size", type=int, default=32768, help="文件字节数（默认 32 KiB）")
    ap.add_argument(
        "--sizes",
        default="256,512,1024,2048,4096,8192",
        help="候选块大小，逗号分隔",
    )
    args = ap.parse_args(argv)
    sizes = [int(x) for x in args.sizes.split(",") if x.strip()]

    session = new_session(
        l=L, n_max=N_MAX, modulus_bits=MODULUS_BITS, seed=b"bench-blocksize"
    )
    data = payload(args.size)
    print("=" * 88)
    print(f"块大小标定：文件 {args.size} 字节，4 台节点，|N|={MODULUS_BITS} 位，l={L}")
    print("（单进程；数字量于 2026-09-26，当时每块还含一次 ABE 封装 ⇒ 上界；数字是本机的，换机器要重跑）")
    print("=" * 88)
    print(
        f"{'块大小':>8} {'块数':>6} {'上传ms':>9} {'每块ms':>8} "
        f"{'全量验ms':>10} {'每块ms':>8} {'证据B':>7} {'篡改被抓':>8}"
    )
    rows: list[dict] = []
    for seg in sizes:
        if args.size / seg > N_MAX:
            print(f"{seg:>8} {'—':>6}   需要 {int(args.size / seg) + 1} 块 > n_max={N_MAX}，跳过")
            continue
        row = bench_one(session, data, seg)
        rows.append(row)
        print(
            f"{row['seg']:>8} {row['blocks']:>6} {row['upload_ms']:>9.1f} "
            f"{row['upload_per_block']:>8.2f} {row['verify_ms']:>10.1f} "
            f"{row['verify_per_block']:>8.2f} {row['proof_bytes']:>7} "
            f"{('是' if row['tamper_caught'] else '否!'):>8}"
        )

    print("=" * 88)
    if not rows:
        print("没有可测的候选 —— 文件相对 n_max 太大了，只能把块切大。")
        return 1
    best_verify = min(rows, key=lambda r: r["verify_ms"])
    best_upload = min(rows, key=lambda r: r["upload_ms"])
    print(f"一次全量验证最快：块大小 {best_verify['seg']}（{best_verify['verify_ms']:.1f} ms）")
    print(f"上传最快：块大小 {best_upload['seg']}（{best_upload['upload_ms']:.1f} ms）")
    small, big = rows[0], rows[-1]
    print(
        f"趋势：块从 {small['seg']} 涨到 {big['seg']}，块数 "
        f"{small['blocks']} → {big['blocks']}，"
        f"上传 {small['upload_ms']:.1f} → {big['upload_ms']:.1f} ms，"
        f"全量验证 {small['verify_ms']:.1f} → {big['verify_ms']:.1f} ms"
    )
    bad = [r["seg"] for r in rows if not r["tamper_caught"]]
    ways = sorted({r["tamper_way"] for r in rows})
    print(
        f"反面对照：篡改全部被抓到 ✓（方式：{'、'.join(ways)}）"
        if not bad
        else f"★ 有漏网：块大小 {bad} —— 不可信！"
    )
    print("=" * 88)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
