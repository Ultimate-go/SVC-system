"""验证会话层的**身份素数路径**：新旧两条路径并存且互不干扰。

这是"去掉全局下标"改造的第 2 关。第 1 关（``verify_identity_primegen.py``）
证明了「换素数映射，密码学内核不用改就能跑」；这一关证明
「**会话**能在两套坐标系之间切换，而且老路径逐位不变」。

三个必须成立的事：

1. **老路径回归**：``Digest.identity == ""`` 时，取到的素数、算出的 ``U``、
   ``commit``/``verify`` 结果与改造前**逐位一致**（所以旧库不用动）；
2. **新路径可用**：``Digest.identity`` 非空时，素数来自
   ``H(owner‖file_key‖block)``，且 ``commit``/``verify`` 全通；
3. **两套坐标独立**：同一份文件在两条路径下拿到的是**不同**的素数
   （证明"全局下标"真的被旁路了，不是换个名字继续用）。
"""

from __future__ import annotations

import dataclasses
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.session import (  # noqa: E402
    ExplicitPrimeGen,
    IdentityPrimeView,
    new_session,
)
from svc import commit, open_subvector, specialize, verify  # noqa: E402
from svc.primegen_identity import IdentityPrimeGen  # noqa: E402
from svc.types import VerifyCode  # noqa: E402
from vds.digest import Digest, make_identity, split_identity  # noqa: E402

OK_CODE = int(VerifyCode.OK)

FAILURES: list[str] = []


def passed(report) -> bool:
    return int(report.code) == OK_CODE


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
# 公共参数
# ---------------------------------------------------------------------------
L = 16
N_MAX = 8
N = 6
VALS = [3, 5, 7, 11, 13, 17]
I = [0, 2, 4]
VALS_I = [VALS[i] for i in I]
OWNER, FILE_KEY = "alice", "病历A"

session = new_session(l=L, n_max=N_MAX, modulus_bits=512, seed=b"session-identity")
CRS = session.crs

section("0. 身份串编解码（全系统唯一格式）")
check("make/split 往返一致", split_identity(make_identity(OWNER, FILE_KEY)) == (OWNER, FILE_KEY))
try:
    make_identity("", "b")
    check("空 owner 被拒", False)
except ValueError:
    check("空 owner 被拒", True)
try:
    split_identity("没有分隔符")
    check("坏身份串被拒", False)
except ValueError:
    check("坏身份串被拒", True)


# ---------------------------------------------------------------------------
# 1. 老路径回归：identity=""，行为必须逐位不变
# ---------------------------------------------------------------------------
section("1. 老路径回归（identity 为空 → 查全局素数表）")

crs_n_old = specialize(CRS, N)
delta_old = Digest(U=crs_n_old.U_n, C=111, n=N, offset=0)
got_old = session.crs_n_for(delta_old)

check("老路径 primegen 仍是公开参数本身（offset=0 不进缓存）",
      got_old.crs.primegen is CRS.primegen)
check("老路径素数 == 全局表前 n 个",
      [got_old.crs.primegen.get(i) for i in range(N)] == CRS.primegen.first(N))
check("老路径 e_all == product_tree(前 n 个素数)", got_old.e_all == crs_n_old.e_all)
check("老路径 U_n 直接取自摘要", got_old.U_n == delta_old.U)

com_old = commit(got_old, VALS)
op_old = open_subvector(got_old, I, VALS_I, VALS)
rep_old = verify(got_old, com_old.C, I, VALS_I, op_old)
check("★ 老路径 commit/verify 全通（旧库不受影响）", passed(rep_old), str(rep_old.code))

bad = list(VALS_I)
bad[0] = (bad[0] + 1) % 1000
check("老路径篡改仍被拒", not passed(verify(got_old, com_old.C, I, bad, op_old)))

check("老路径 primegen_for 与 crs_n_for 一致",
      session.primegen_for(delta_old).first(N) == CRS.primegen.first(N))

check("老路径重复调用命中缓存",
      session.crs_n_for(delta_old) is got_old)


# ---------------------------------------------------------------------------
# 2. 新路径：identity 非空 → 按块身份派生素数
# ---------------------------------------------------------------------------
section("2. 新路径（identity 非空 → 按块身份派生素数）")

ident = make_identity(OWNER, FILE_KEY)
expect_primes = IdentityPrimeGen(OWNER, FILE_KEY, range(N)).positions
E_id = 1
for p in expect_primes:
    E_id *= p
U_id = pow(CRS.g, E_id, CRS.N)

delta_id = Digest(U=U_id, C=111, n=N, identity=ident)
got_id = session.crs_n_for(delta_id)

check("新路径素数 == IdentityPrimeGen 的素数",
      tuple(got_id.crs.primegen.get(i) for i in range(N)) == expect_primes)
check("新路径 e_all == ∏ 身份素数", got_id.e_all == E_id)
check("新路径 U_n 直接取自摘要", got_id.U_n == delta_id.U)
check("★ 新路径与老路径的素数**完全不同**（全局下标真的被旁路）",
      len(set(expect_primes) & set(CRS.primegen.first(N))) == 0)

com_id = commit(got_id, VALS)
op_id = open_subvector(got_id, I, VALS_I, VALS)
rep_id = verify(got_id, com_id.C, I, VALS_I, op_id)
check("★ 新路径 commit/verify 全通", passed(rep_id), str(rep_id.code))
check("新路径篡改仍被拒", not passed(verify(got_id, com_id.C, I, bad, op_id)))

pgen = session.primegen_for(delta_id)
check("primegen_for 返回惰性身份视图", isinstance(pgen, IdentityPrimeView))
check("primegen_for 与 crs_n_for 素数一致", pgen.first(N) == list(expect_primes))
check("primegen_for 的 max_sz ≥ n", pgen.max_sz >= N)

check("primes_of() 与视图一致", session.primes_of(OWNER, FILE_KEY, N) == expect_primes)
check("view_crs_identity().primegen.get(i) 一致",
      [session.view_crs_identity(OWNER, FILE_KEY, N).primegen.get(i)
       for i in range(N)] == list(expect_primes))
check("新路径重复调用命中缓存", session.crs_n_for(delta_id) is got_id)


# ---------------------------------------------------------------------------
# 3. 新路径的"没有上限"与"确定性"
# ---------------------------------------------------------------------------
section("3. 新路径：无上限 / 确定性 / 跨文件隔离")

# 老方案：位置发完就没有了。新方案：块号多大都能算
big_view = session.view_crs_identity("alice", "大文件", 20000)
p_last = big_view.primegen.get(19999)
check("★ 第 20000 块也能取到素数（旧方案卡在 n_max=8192）",
      p_last > 0 and p_last.bit_length() >= 120, f"bits={p_last.bit_length()}")
check("新文件不占用任何'全局位置'（会话不持有登记表，天然无分配）",
      not hasattr(session, "registry"))
check("同一个文件重复取 → 相同素数",
      session.primes_of(OWNER, FILE_KEY, 3) == session.primes_of(OWNER, FILE_KEY, 3))
check("不同文件 → 无交集素数",
      len(set(session.primes_of(OWNER, "病历B", 6)) & set(expect_primes)) == 0)
check("不同 owner → 无交集素数",
      len(set(session.primes_of("bob", FILE_KEY, 6)) & set(expect_primes)) == 0)


# ---------------------------------------------------------------------------
# 4. 跨文件合并要用的 ExplicitPrimeGen
# ---------------------------------------------------------------------------
section("4. 显式素数表视图（跨文件合并的基础）")

merged = session.primes_of(OWNER, FILE_KEY, N) + session.primes_of("bob", "病历B", 3)
mview = session.view_crs_primes(merged)
check("ExplicitPrimeGen 逐位对上", [mview.primegen.get(i) for i in range(len(merged))] == list(merged))
check("ExplicitPrimeGen.max_sz == 个数", mview.primegen.max_sz == len(merged))

crs_n_m = specialize(mview, len(merged))
vals_m = list(VALS) + [2, 4, 6]
com_m = commit(crs_n_m, vals_m)
op_m = open_subvector(crs_n_m, [0, 3, 6], [vals_m[0], vals_m[3], vals_m[6]], vals_m)
check("★ 合并宇宙上 commit/verify 通过",
      passed(verify(crs_n_m, com_m.C, [0, 3, 6],
                    [vals_m[0], vals_m[3], vals_m[6]], op_m)))

try:
    ExplicitPrimeGen([7, 7])
    check("重复素数被拒", False)
except ValueError:
    check("重复素数被拒", True)

check("裸 primegen 也能直接跑 specialize",
      dataclasses.replace(CRS, primegen=ExplicitPrimeGen([3, 5, 7])).primegen.first(3) == [3, 5, 7])


# ---------------------------------------------------------------------------
# 5. 缓存行为（性能是第一性约束）
# ---------------------------------------------------------------------------
section("5. 缓存行为")

fresh = new_session(l=L, n_max=N_MAX, modulus_bits=512, seed=b"cache-test")
IdentityPrimeGen.clear_cache()
t0 = time.perf_counter()
d_cold = Digest(U=1, C=1, n=64, identity=make_identity("c", "cold"))
fresh.primegen_for(d_cold).first(64)
t_cold = time.perf_counter() - t0

t0 = time.perf_counter()
fresh.primegen_for(d_cold).first(64)
t_warm = time.perf_counter() - t0
check("冷派生明显慢于热读取", t_cold > t_warm * 5,
      f"冷 {t_cold * 1000:.0f} ms → 热 {t_warm * 1000:.1f} ms")
print(f"        （64 块：冷 {t_cold * 1000:.0f} ms · 热 {t_warm * 1000:.2f} ms）")


# ---------------------------------------------------------------------------
print()
print("=" * 72)
if FAILURES:
    print(f"结果：{len(FAILURES)} 项未通过")
    for f in FAILURES:
        print("   FAIL " + f)
    sys.exit(1)
print("结果：ALL PASS —— 会话层新旧两条坐标路径并存且互不干扰")
print("=" * 72)
