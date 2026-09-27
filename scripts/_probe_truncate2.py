"""探针 v2：截断能不能由**协调者自己**发起？

★ 名字里带下划线只是历史习惯 —— 它**不是**用完就扔的：`架构说明.md`
（"实测：`scripts/_probe_truncate2.py`（14/14）"）和 ``core/store.py`` 里
``update_truncate`` 的 docstring 都把它当作那组实测的**复现入口**在引用。
原先第一行写的"（做完会删）"已经不成立，删了会让那两处引用悬空，故改掉。
（它由一版只做代数推导的数学稿演进而来 —— 那一版已删，结论都合进这里了。）

问题一：「协调者扮成持有整个向量的节点」时，它的 ``st`` 到底是什么？
候选：

  a) ``session.bootstrap()`` 的 ``(U=1, C=g)`` —— 论文的空**文件**约定
  b) ``(g, 1)`` —— 代数意义上的 :math:`d(\\varnothing)`
  c) :func:`svc.specialize` + :func:`svc.commit` 对**空向量**承诺一次

判据有两条，缺一不可：``check_local_view()`` 要过（这叫"合法本地视图"），
并且 ``push_update`` 算出的新摘要要与「对截断后向量做一次性承诺」**逐位相同**。

问题二：节点持有集合与 ``K`` 部分相交时，是不是真的报错？
问题三：先对那台做 ``rmv_storage`` 再删，是不是真的能过？

跑法（在工作区根目录）::

    <python> scripts/_probe_truncate2.py
"""

from __future__ import annotations

import sys
import traceback

from svc import commit, disagg, specialize
from svc.types import Opening
from vds import VDSSession
from vds.digest import Digest, LocalView
from vds.storage_node import StorageNode
from vds.updates import UpdateDelta, apply_update, push_update, update_truncate

BLOCK_BYTES = 16
N_MAX = 16
N_BLOCKS = 12
SPLIT = 6
K = [10, 11]
KEEP = N_BLOCKS - len(K)
ALL = tuple(range(N_BLOCKS))

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f" —— {detail}" if detail else ""))


def make_env(seed=b"probe-truncate"):
    s = VDSSession(
        n_max=N_MAX, l=BLOCK_BYTES * 8, lambda_bits=16,
        modulus_bits=512, seed=seed,
    )
    payload = bytes((i * 7 + 3) % 256 for i in range(N_BLOCKS * BLOCK_BYTES))
    delta, crs_n, values, _ = s.commit_bytes(payload, BLOCK_BYTES)
    nodes = s.distribute(
        delta, values,
        [list(range(0, SPLIT)), list(range(SPLIT, N_BLOCKS))],
        crs_n=crs_n,
    )
    return s, delta, values, nodes


def make_node(s, delta, values, seg, name="manual") -> StorageNode:
    """手工造一个持有 ``seg`` 的节点。

    ★ 起点必须是 :math:`d(\\varnothing)`（即「什么都被排除」的那个摘要），
    不是 :math:`d(v)` —— ``disagg`` 的做法是「从 st 出发，把 I\\K 逐个加回来」，
    所以 st 必须是「排除集 = I」那个摘要。拿 ``(U_n, C_n)``（排除集 = 空）
    当起点，加回来的就是无源之水，算出的东西连 ``check_local_view`` 都过不了。
    """
    crs_n = s.crs_n_for(delta)
    st = disagg(crs_n, list(ALL), list(values), empty_vector_view(s), list(seg))
    return StorageNode(
        name, s,
        LocalView(delta=delta, st=st, I=tuple(seg), FI=tuple(values[i] for i in seg)),
    )


def expected_digest(s, values, keep: int) -> Digest:
    """对截断后的向量**重新做一次**一次性承诺 —— 独立于增量路径。"""
    crs_n = specialize(s.crs, keep)
    return Digest(U=crs_n.U_n, C=commit(crs_n, list(values[:keep])).C, n=keep)


def empty_vector_view(s) -> Opening:
    """代数意义上的 :math:`d(\\varnothing)`：用公开参数对**空向量**承诺一次。

    实测（本探针候选 c）：``specialize(crs, 0).U_n`` 就是 ``g``，
    ``commit(specialize(crs, 0), []).C`` 就是 ``1``，于是这里得到 ``(g, 1)``；
    它作为「持有整个向量的节点」的 ``st`` 能通过 ``check_local_view()``。

    ★ **不要用** ``session.bootstrap()`` —— 它返回 ``(U=1, C=g)``，那是论文
    里「空**文件**」的约定取值（``st_0 ← g``），与 ``add_back`` 的代数前提不合：
    实测拿它当 ``st`` 算出的新摘要与一次性承诺对不上，``check_local_view`` 也是 False。
    """
    crs_0 = specialize(s.crs, 0)
    return Opening(crs_0.U_n, commit(crs_0, []).C, ALL)


def main() -> int:
    s, delta, values, nodes = make_env()
    want = expected_digest(s, values, KEEP)
    print("=" * 74)
    print(f"当前 n = {delta.n}，要删 K = {K}，删后 n' = {KEEP}")
    print(f"期望的新摘要（一次性承诺）：{want!r}")
    print("=" * 74)

    boot = s.bootstrap()
    print(f"  bootstrap()                = {boot!r}")
    crs_0 = specialize(s.crs, 0)
    print(f"  specialize(crs,0).U_n      = {crs_0.U_n}")
    print(f"  crs.g                      = {s.crs.g}")
    print(f"  commit(specialize(crs,0),[]).C = {commit(crs_0, []).C}")
    print()

    cands = [
        ("a) bootstrap() 的 (U=1, C=g)", Opening(boot.U, boot.C, ALL), False),
        ("b) 手写 (g, 1)", Opening(s.crs.g, 1, ALL), True),
        ("c) specialize+commit 空向量", empty_vector_view(s), True),
    ]
    op_delta = UpdateDelta("del", tuple(K), ())
    old_values = {i: values[i] for i in K}
    winner = None
    for label, st, should_match in cands:
        holder = StorageNode(
            "__coordinator__", s,
            LocalView(delta=delta, st=st, I=ALL, FI=tuple(values)),
        )
        try:
            valid = holder.check_local_view()
        except Exception as exc:  # noqa: BLE001
            valid = f"抛异常 {exc!r}"
        try:
            pushed = push_update(s, delta, holder, op_delta, old_values)
            got: object = pushed.delta
            hit = got == want
            if hit and winner is None:
                winner = label
        except Exception as exc:  # noqa: BLE001
            got, hit = f"抛异常 {exc!r}", False
        print(f"  候选 {label}")
        print(f"      check_local_view() = {valid}")
        print(f"      算出的新摘要       = {got!r}")
        if should_match:
            check(f"{label} 与一次性承诺一致", hit)
        else:
            # 反面对照：这条**应当**对不上，否则说明“约定值”与“代数值”的区别不存在
            check(f"{label} 故意对不上（反面对照）", not hit, "确实对不上" if not hit else "居然对上了？！")

    print()
    print("=" * 74)
    print("探针 2：节点持有集合与 K 部分相交时，是不是真的报错")
    print("=" * 74)
    node_a = make_node(s, delta, values, list(range(0, SPLIT)) + [10], "node-a")
    node_b = make_node(s, delta, values, list(range(SPLIT, N_BLOCKS)), "node-b")
    print(f"  node-a 持有 {node_a.I}（K ∩ I = [10]）")
    print(f"  node-b 持有 {node_b.I}（K 整段都在它手里）")
    check("node-a 的本地视图合法", node_a.check_local_view())
    check("node-b 的本地视图合法", node_b.check_local_view())
    try:
        update_truncate(s, delta, [node_a, node_b], K)
        check("部分相交应当报错", False, "居然通过了（说明约束不存在？）")
    except ValueError as exc:
        check("部分相交应当报错", "部分相交" in str(exc), str(exc))

    print()
    print("=" * 74)
    print("探针 3：先 rmv_storage 把 K 那部分交回去，再删（推送方 = 协调者）")
    print("=" * 74)
    trimmed_a = node_a.rmv_storage([10])
    print(f"  交回后 node-a 持有 {trimmed_a.I}")
    check("node-a 交回后视图仍合法", trimmed_a.check_local_view())
    holder = StorageNode(
        "__coordinator__", s,
        LocalView(delta=delta, st=empty_vector_view(s), I=ALL, FI=tuple(values)),
    )
    try:
        pushed = push_update(s, delta, holder, op_delta, old_values)
        outs = []
        for nd in (trimmed_a, node_b):
            out = apply_update(s, delta, nd, op_delta, pushed.witness)
            outs.append((nd.node_id, out))
            check(
                f"{nd.node_id} 的 ApplyUpdate 通过",
                out.ok,
                out.message or f"新 I = {out.node.I}",
            )
            if out.node is not None and out.node.I:
                check(f"{out.node.node_id} 删后视图合法", out.node.check_local_view())
        by_id = dict(outs)
        _ = by_id  # 只用一次，留着方便调试
        b_out = next(o for nid, o in outs if nid == node_b.node_id)
        check(
            "node-b（K 整段持有）只丢下标、(S_I, Lambda_I) 一字未改",
            b_out.node.st.S_I == node_b.st.S_I
            and b_out.node.st.Lambda_I == node_b.st.Lambda_I
            and set(b_out.node.I) == set(node_b.I) - set(K),
            f"新 I = {b_out.node.I}",
        )
        a_out = next(o for nid, o in outs if nid == trimmed_a.node_id)
        check(
            "node-a（已交回、与 K 不相交）下标不变、状态变",
            set(a_out.node.I) == set(trimmed_a.I)
            and a_out.node.st.Lambda_I != trimmed_a.st.Lambda_I,
        )
        check(
            "两条路给出的新摘要一致且等于一次性承诺",
            b_out.delta == a_out.delta == want,
            f"{b_out.delta!r}",
        )
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        check("交回后再删能过", False, f"抛异常：{exc!r}")

    print()
    print("=" * 74)
    bad = [n for n, o in results if not o]
    print(f"合计 {len(results)} 项，通过 {len(results) - len(bad)}，失败 {len(bad)}")
    for n in bad:
        print(f"  未通过：{n}")
    print(f"胜出的 st 候选：{winner}")
    print("=" * 74)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
