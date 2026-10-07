"""端到端验证：**身份坐标真正生效**后，VectorStore 的全部业务流仍然通。

这是"去掉全局下标"改造的第 3 关（继 ``verify_identity_primegen.py``、
``verify_session_identity.py`` 之后）。前两关证明了「素数换源」这件事在
``svc`` 与会话层成立；这一关把它接进真实业务流，并逐项确认：

* 新建的摘要**带身份**（``Digest.identity`` 非空）；
* 上传 / 改块 / 追加 / 截断 / 删除之后，身份**一路传得下去**；
* 读回明文、``commit`` 自检（``authoritative_digest``）、全面自检（``check``）
  三项全部与改造前**同样通过**；
* 跨文件聚合（``universe_for`` 那条路）在新坐标下照样成立。

★ 最后一节是**负面对照**：把身份抹掉，验证必须挂 —— 证明身份不是摆设，
真的是"哪块配哪个素数"的唯一来源。
"""

from __future__ import annotations

import dataclasses
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.session import new_session  # noqa: E402
from core.store import PlainKeyStore  # noqa: E402
from vds.digest import make_identity  # noqa: E402

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


def ok_verify(rep) -> bool:
    """``cipher_of`` 的 ``verify`` 子字典是不是通过了。"""
    return bool(rep["ok"])


# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------
BLOCK = 8
N_MAX = 32
session = new_session(l=256, n_max=N_MAX, modulus_bits=1024, seed=b"store-identity-e2e")
store = PlainKeyStore(
    session, node_ids=("n1", "n2", "n3"), segment_bytes=BLOCK, replica_factor=1
)

F1 = ("alice", "f1")
F2 = ("alice", "f2")
ID1 = make_identity(*F1)

#: 每块 8 字节，所以直接用 8 字节一块拼出来 —— 期望值**派生**而不用手算，
#: 免得块数算错导致“代码没问题但测试红”（我第一版就是这么错的）。
B0, B1, B2, B3, B4 = (b"AAAABBBB", b"CCCCDDDD", b"EEEEFFFF",
b"GGGGGGGG", b"HHHHHHHH")
B1X = b"XXXXXXXX"
PLAIN1 = B0 + B1 + B2
PLAIN2 = b"1111222233334444555566667777"


def ident_of(fid):
    return getattr(store.delta_of(*fid), "identity", "") or ""


section("1. 上传：新建摘要必须带身份")

t0 = time.perf_counter()
rec1 = store.upload(*F1, PLAIN1)  # 24 字节 → 3 块
t_up = time.perf_counter() - t0

check("f1 上传后 n = 3", store.delta_of(*F1).n == 3, f"n={store.delta_of(*F1).n}")
check("★ f1 的摘要带身份", ident_of(F1) == ID1, ident_of(F1))
check("读回明文与原文一致", store.read(*F1) == PLAIN1)
check("cipher_of 的承诺验证通过", ok_verify(store.cipher_of(*F1)["verify"]))
check("authoritative_digest 与增量维护的 δ 相同",
      store.authoritative_digest(F1) == store.delta_of(*F1))
check("全面自检 check() 无问题", store.check() == [], str(store.check()[:2]))
print(f"        （3 块上传耗时 {t_up:.2f}s，含首次素数派生）")


section("2. 改块 / 追加：身份必须一路带下去")

store.modify(*F1, 1, B1X)
check("改块后 n 不变", store.delta_of(*F1).n == 3)
check("★ 改块后身份仍在", ident_of(F1) == ID1)
check("改块后读回正确", store.read(*F1) == B0 + B1X + B2)
check("改块后 cipher_of 验证通过", ok_verify(store.cipher_of(*F1)["verify"]))
check("改块后 self-check 通过", store.authoritative_digest(F1) == store.delta_of(*F1))

store.append(*F1, B3 + B4)  # +2 块
check("追加后 n = 5", store.delta_of(*F1).n == 5, f"n={store.delta_of(*F1).n}")
check("★ 追加后身份仍在", ident_of(F1) == ID1)
check("追加后读回正确", store.read(*F1) == B0 + B1X + B2 + B3 + B4)
check("追加后 cipher_of 验证通过", ok_verify(store.cipher_of(*F1)["verify"]))
check("追加后 check() 无问题", store.check() == [], str(store.check()[:2]))


section("3. 第二份文件 + 跨文件聚合")

store.upload(*F2, PLAIN2)  # 28 字节 → 4 块
check("★ f2 的摘要带身份", ident_of(F2) == make_identity(*F2))
check("f1 / f2 身份不同", ident_of(F1) != ident_of(F2))

pos1 = store.registry.positions_of(*F1)
pos2 = store.registry.positions_of(*F2)
check("两份文件位置不重叠", not (set(pos1) & set(pos2)))
check("f2 读回正确", store.read(*F2) == PLAIN2)

mixed = list(pos1[:2]) + list(pos2[:2])  # 跨两个文件
qr = store.query(mixed)
check("★ 跨文件检索 + 聚合验证通过", bool(qr.ok), qr.report.message)
check("跨文件取回的下标齐全", set(qr.indices) == set(mixed))

crn, C, P, loc = store.universe_for([F1, F2])
check("universe_for 的位置集 = 两份文件并集",
      set(P) == set(pos1) | set(pos2), f"|P|={len(P)}")
check("universe_for 的 n 与位置数一致", crn.n == len(P) and len(loc) == len(P))
prime_at: dict[int, int] = {}
for _f in (F1, F2):
    for _p, _e in zip(store.registry.positions_of(*_f), store._primes_of_file(_f)):
        prime_at[_p] = _e
check("★ 合并向量的素数 = 各文件自己的素数（按位置对齐）",
      [crn.crs.primegen.get(i) for i in range(len(P))] == [prime_at[p] for p in P])


section("4. 截断 / 删除：身份仍要保住")

K, _ = store.truncate(*F1, 2)
check("截断返回了末尾 2 块", len(K) == 2)
check("截断后 n = 3", store.delta_of(*F1).n == 3, f"n={store.delta_of(*F1).n}")
check("★ 截断后身份仍在", ident_of(F1) == ID1)
check("截断后读回正确", store.read(*F1) == B0 + B1X + B2)
check("截断后 cipher_of 验证通过", ok_verify(store.cipher_of(*F1)["verify"]))
check("截断后 check() 无问题", store.check() == [], str(store.check()[:2]))

dropped, files_gone = store.delete_from(*F2)
check("删除 f2 后它不在 files 里", F2 not in store.files)
check("删除后 check() 无问题", store.check() == [], str(store.check()[:2]))
check("f1 在 f2 之后仍可读", store.read(*F1) == B0 + B1X + B2)


section("5. 负面对照：抹掉身份 → 必须挂（证明身份是承重的）")

delta = store.delta_of(*F1)
stripped = dataclasses.replace(delta, identity="")
good = session.crs_n_for(delta)
bad = session.crs_n_for(stripped)
check("★ 抹掉身份后拿到的素数与正确值不同",
      [good.crs.primegen.get(i) for i in range(delta.n)]
      != [bad.crs.primegen.get(i) for i in range(delta.n)])
check("★ 用错误坐标算出的 e_all 也不同", good.e_all != bad.e_all)
check("★ 用错误坐标算出的 U 与摘要里的 U 不符",
      pow(session.crs.g, bad.e_all, session.crs.N) != delta.U)
check("正确坐标算出的 U 与摘要里的 U 相符",
      pow(session.crs.g, good.e_all, session.crs.N) == delta.U)


section("6. 无上限：块数不再受 n_max 约束")

big = session.view_crs_identity(*F1, 20000)
check("★ 第 20000 块仍能取到素数（n_max=32 却不受限）",
      big.primegen.get(19999).bit_length() >= 120)
check("n_max 仍是 32（公开参数没变）", session.n_max == N_MAX)

# ★★ 这一条是「8192 天花板消失」的**直接**验证：连续上传，让**全系统块数之和**
#    明确越过 n_max。改造前 `registry.alloc(..., n_max=...)` 会在这里报
#    「超出全系统位置预算」；现在位置只是存储槽位编号，放开增长。
cap_store = PlainKeyStore(
    new_session(l=256, n_max=N_MAX, modulus_bits=1024, seed=b"cap-test"),
    node_ids=("c1", "c2"),
    segment_bytes=8,
    replica_factor=1,
)
total = 0
for i in range(9):
    cap_store.upload("bob", f"cap{i}", bytes(8 * 4))  # 每次 4 块
    total = cap_store.n
check(f"★ 全系统 {total} 块 &gt; n_max={N_MAX} 仍能继续上传（天花板没了）",
      total > N_MAX, f"共 {total} 块")
check("★ 此时自检仍全绿", cap_store.check() == [], str(cap_store.check()[:2]))
check("★ 超预算的文件仍能读回",
      cap_store.read("bob", "cap8") == bytes(8 * 4))


# ---------------------------------------------------------------------------
print()
print("=" * 72)
if FAILURES:
    print(f"结果：{len(FAILURES)} 项未通过")
    for f in FAILURES:
        print("   FAIL " + f)
    sys.exit(1)
print("结果：ALL PASS —— 身份坐标在真实业务流中全程生效，且旧功能无一退化")
print("=" * 72)
