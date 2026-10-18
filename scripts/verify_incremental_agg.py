"""端到端验证：**增量聚合**与**一步分解**在代数上真的成立。

这一关回答两个问题（都是界面上「增量聚合与分解」要讲的那件事）：

1. **两段聚合、再合并**，结果与「一次性取全」**逐位相同**吗？
   即 :math:`\\mathrm{Agg}(\\pi_I, \\pi_J) \\stackrel{?}{=} \\pi_{I \\cup J}`。
   如果成立，"先聚合一部分、以后再把新来的并进来"就是**复用**，
   而不是"换个说法重算一遍" —— 这正是「增量」二字的实处。
   还要确认它与**划分方式、合并顺序**无关（结合律、交换律）。

2. **分解能不能一步拆到位**？即 :math:`\\pi_I \\to \\pi_K`（任意
   :math:`K \\subseteq I`）是否一步可达，而不是只能逐层往回退。
   顺带确认「拆出来的 :math:`\\pi_K`」与「当初聚合出来的 :math:`\\pi_K`」
   是同一份东西 —— 这决定了两条路能不能对得上账。

★ 最后一节是**负面对照**：拆不出覆盖范围之外的下标、合并要求两个集合不相交。
   没有这两条，上面那些「成立」就说明不了任何事。

全程离线，不起服务、不碰网络。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.session import new_session  # noqa: E402
from core.store import PlainKeyStore  # noqa: E402
from svc import agg as svc_agg, disagg as svc_disagg, verify as svc_verify  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  PASS  " if cond else "  FAIL  ") + name
          + (("   :: " + extra) if extra else ""))
    if not cond:
        FAILURES.append(name)


def section(t: str) -> None:
    print()
    print("=" * 72)
    print(t)
    print("=" * 72)


# ---------------------------------------------------------------------------
# 装置：一份 6 块的文件，3 台节点
# ---------------------------------------------------------------------------
BLOCK = 8
session = new_session(l=256, n_max=64, modulus_bits=1024, seed=b"incr-agg-e2e")
store = PlainKeyStore(
    session, node_ids=("n1", "n2", "n3"), segment_bytes=BLOCK, replica_factor=1
)

FID = ("alice", "f6")
PLAIN = b"".join(bytes([65 + i]) * BLOCK for i in range(6))  # 6 块，每块 8 字节

store.upload(*FID, PLAIN)  # type: ignore[arg-type]
G = list(store.registry.positions_of(*FID))
FD = store.delta_of(*FID)
CRS_N = session.crs_n_for(FD)
LOC = {g: i for i, g in enumerate(FD.positions)}
BITS = int(CRS_N.N).bit_length()


def take(gs):
    """按**全局位置号**取一份证据（这就是 ``/api/query`` 那条路）。"""
    r = store.query(sorted(gs))
    if not r.ok:
        raise SystemExit(f"查询本身没通过：{r.report.message}")
    if [LOC[g] for g in r.indices] != list(r.proof.I):
        raise SystemExit("下标口径对不上：全局号与文件内局部号没对齐")
    return r


def merge(a, b):
    """把两份证据合并成一份 —— 论文 ``VC.Agg``（``svc.agg``）。

    ★ 输入是**两份已经存在的证据**（各自的 :math:`(I, v_I, \\pi_I)`），
      不向节点要任何新的数据。这正是「增量」那一步。

    参数与返回值都是 ``(证据, 分量表)`` 这一对 —— 合并既需要两份证据，
    也需要它们各自的分量（``Agg`` 要用交叉项 `∏φ_j^{v_j}` 把它们除掉）。
    """
    pa, va = a
    pb, vb = b
    pi = svc_agg(CRS_N, list(pa.I), list(va), pa, list(pb.I), list(vb), pb)
    by_local = {i: v for i, v in zip(list(pa.I), list(va))}
    by_local.update({i: v for i, v in zip(list(pb.I), list(vb))})
    return pi, [by_local[i] for i in pi.I]


def ev(r):
    """一次查询的结果 → ``(证据, 分量表)``。"""
    return r.proof, list(r.values)


def prove_by(pi, vals):
    """用**独立入口**把一份证据验一遍（不采信「和直算一样」这种说法）。"""
    rep = svc_verify(CRS_N, FD.C, list(pi.I), list(vals), pi)
    return bool(rep.ok), rep


def same(p, q) -> bool:
    return p.S_I == q.S_I and p.Lambda_I == q.Lambda_I


def size_bytes(pi) -> int:
    bits = max(pi.S_I.bit_length(), pi.Lambda_I.bit_length())
    return 2 * ((bits + 7) // 8)


section("0. 装置自检")

check("上传成 6 块", len(G) == 6, f"positions={G}")
check("全局位置号连续", G == list(range(G[0], G[0] + 6)), str(G))
check("读回明文与原文一致", store.read(*FID) == PLAIN)  # type: ignore[arg-type]
full = take(G)
check("六块一次取：公开检验通过", full.ok, full.summary())
check("六块一次取的证据为两个群元素（≤ 256 字节）", size_bytes(full.proof) <= 256,
      f"模数约 {BITS} bit，证据 {size_bytes(full.proof)} 字节")


section("1. 两段聚合再合并  ?=  一次取全")

A = take(G[0:3])       # 前 3 块
B = take(G[3:6])       # 后 3 块
AB, vals_AB = merge(ev(A), ev(B))       # 合并两份**已有**证据

check("前 3 块单独取：公开检验通过", A.ok, A.summary())
check("后 3 块单独取：公开检验通过", B.ok, B.summary())
check("★ 合并结果与一次取全逐位相同", same(AB, full.proof),
      f"S 相同={AB.S_I == full.proof.S_I}，Λ 相同={AB.Lambda_I == full.proof.Lambda_I}")
check("合并结果的覆盖集恰是两段之并", set(AB.I) == set(A.proof.I) | set(B.proof.I),
      f"{sorted(AB.I)}")
check("合并结果仍是两个群元素", size_bytes(AB) <= 256, f"{size_bytes(AB)} 字节")

ok_ab, rep_ab = prove_by(AB, vals_AB)
check("★ 合并结果可独立通过公开检验（走 verify 入口，不看「和直算一样」）", ok_ab,
      getattr(rep_ab, "message", "") or "")


section("2. 与划分方式、合并顺序无关")

check("1 + 5 的划分同样等于一次取全",
      same(merge(ev(take(G[0:1])), ev(take(G[1:6])))[0], full.proof))

p01 = merge(ev(take(G[0:1])), ev(take(G[1:2])))
p23 = merge(ev(take(G[2:3])), ev(take(G[3:4])))
p45 = ev(take(G[4:6]))
check("四层嵌套合并（(0+1)+(2+3)，再与 4、5 合）逐位相同",
      same(merge(merge(p01, p23), p45)[0], full.proof))

check("交换两段的先后，结果不变", same(merge(ev(B), ev(A))[0], AB))

step = merge(merge(ev(take(G[0:3])), ev(take(G[3:4]))), ev(take(G[4:6])))
check("先合前 4 块、再并最后 2 块，同样逐位相同", same(step[0], full.proof))
check("分步合并得到的分量表与一次取全一致", list(step[1]) == list(full.values))


section("3. 分解：一步拆到位 ?= 只能退回上一层的程度")

six = full.proof                                  # 覆盖 6 块的一份证据
LOCAL = list(six.I)
K = [LOC[G[0]], LOC[G[2]], LOC[G[4]]]             # 想要第 1、3、5 块（跳着要）
pi_K = svc_disagg(CRS_N, LOCAL, list(full.values), six, K)
vals_K = [full.values[LOCAL.index(i)] for i in K]

ok_k, _ = prove_by(pi_K, vals_K)
check("★ 从 6 块**一步**拆出「第 1、3、5 块」（跳着拆，不经中间层）", ok_k)
check("拆出来的覆盖集正是 K", set(pi_K.I) == set(K), f"{sorted(pi_K.I)}")

pi_back_A = svc_disagg(CRS_N, LOCAL, list(full.values), six, list(A.proof.I))
vals_back_A = [full.values[LOCAL.index(i)] for i in A.proof.I]
check("★ 拆出的子集证据与第一段聚合的结果逐位相同", same(pi_back_A, A.proof))
ok_ba, _ = prove_by(pi_back_A, vals_back_A)
check("拆出的子集证据可独立通过公开检验", ok_ba)

pi_3 = svc_disagg(CRS_N, list(pi_K.I), vals_K, pi_K, [K[1]])
ok_3, _ = prove_by(pi_3, [vals_K[1]])
check("拆出来的还能再拆（层层往下，每层输入都是上一次的结果）", ok_3)
check("连拆两层的第 3 块证据，与直接取第 3 块的证据逐位相同",
      same(pi_3, take([G[2]]).proof))


section("4. 负面对照（否则上面那些「成立」说明不了什么）")

try:
    svc_disagg(CRS_N, LOCAL, list(full.values), six, [LOC[G[0]], 999])
    check("拆出覆盖范围之外的下标必须报错", False, "居然没报错")
except ValueError as exc:
    check("拆出覆盖范围之外的下标必须报错", "K ⊆ I" in str(exc), str(exc)[:60])

try:
    merge(ev(A), ev(A))
    check("合并两个有交集的集合必须报错", False, "居然没报错")
except ValueError as exc:
    check("合并两个有交集的集合必须报错", "I ∩ J" in str(exc), str(exc)[:60])

# 负面对照：改后 3 块里的一个分量。
# ★ 两种结局都算「检出」：要么 agg 当场认出两边不同源（ShamirTrick 失败），
#   要么合出一个其余通不过公开检验的东西。绝不能是「照样得到一个合法证据」。
by_local = {i: v for i, v in zip(list(full.proof.I), list(full.values))}
tampered = dict(by_local)
tampered[max(tampered)] ^= 1
wrong_vals = [tampered[i] for i in B.proof.I]
try:
    wrong, wrong_vals = merge(ev(A), (B.proof, wrong_vals))
    check("负面对照：篡改后 3 块的一个分量，合并结果不再等于一次取全",
          not same(wrong, full.proof))
    ok_wrong, _ = prove_by(wrong, wrong_vals)
    check("负面对照：那份被篡改的合并结果通不过公开检验", not ok_wrong)
except ValueError as exc:
    check("负面对照：篡改后 3 块的一个分量，合并当场就被拒（不同源）", True,
          str(exc)[:50])
    check("负面对照：那份被篡改的合并结果通不过公开检验", True, "根本合不出来")

check("重复合并同一对证据，结果稳定",
      same(merge(ev(take(G[0:3])), ev(take(G[3:6])))[0], AB))


print()
print("=" * 72)
if FAILURES:
    print(f"结果：{len(FAILURES)} 项未通过")
    for f in FAILURES:
        print("  - " + f)
    raise SystemExit(1)
print("结果：全部通过")
print("=" * 72)
