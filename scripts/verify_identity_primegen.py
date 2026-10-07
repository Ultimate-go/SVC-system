"""验证 IdentityPrimeGen 能否**真正替代** PrimeGen 跑通 svc 的全部算法。

这是"去掉全局下标"改造的**前置关卡**：
密码学内核如果在新素数映射下算不出/验不过，后面全都免谈。

判据是**双轨对照** —— 同一份数据，用旧的分配式素数映射和新的身份式映射
各跑一遍完整流程，两边都必须通过，且两边都不能通过时的行为一致。
"""

from __future__ import annotations

import dataclasses
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from svc import (  # noqa: E402
    commit,
    disagg,
    is_probable_prime,
    open_subvector,
    setup,
    specialize,
    verify,
)
from svc.types import VerifyCode  # noqa: E402
from svc.primegen import PrimeGen  # noqa: E402
from svc.primegen_identity import IdentityPrimeGen, identity_key  # noqa: E402

#: 实测确认：``VerifyCode.OK`` 的整数值是 0（篡改/越界都是 3）。
OK_CODE = int(VerifyCode.OK)


def passed(report) -> bool:
    """这份验证报告是不是“通过了”。"""
    return int(report.code) == OK_CODE

FAILURES: list[str] = []


def _raises(fn, exc_type) -> bool:
    """``fn()`` 是否抛出了 ``exc_type``。"""
    try:
        fn()
    except exc_type:
        return True
    except Exception:  # noqa: BLE001 - 抛了别的类型也算没通过
        return False
    return False


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  PASS  " if cond else "  FAIL  ") + name + (("   :: " + extra) if extra else ""))
    if not cond:
        FAILURES.append(name)


def section(t: str) -> None:
    print()
    print("=" * 72)
    print(t)
    print("=" * 72)


# ---------------------------------------------------------------------------
# 1. IdentityPrimeGen 自身的基本性质
# ---------------------------------------------------------------------------
section("1. IdentityPrimeGen 基本性质")

ipg = IdentityPrimeGen("alice", "病历A", range(6))
primes = [ipg.get(i) for i in range(6)]

check("get(i) 返回的都是素数", all(is_probable_prime(p) for p in primes))
check("6 个块拿到 6 个互不相同的素数", len(set(primes)) == 6)
check("max_sz 等于块数（svc 会断言这一点）", ipg.max_sz == 6)
check("bits 与旧类口径一致（prime_bytes*8）", ipg.bits == 128, f"bits={ipg.bits}")
check("first(n) 与逐个 get 一致", ipg.first(4) == primes[:4])
check("越界抛 IndexError", _raises(lambda: ipg.get(99), IndexError))

# 确定性：同样的输入必须得到同样的输出（否则重启后老数据全废）
again = IdentityPrimeGen("alice", "病历A", range(6))
check("确定性：同输入同输出", again.positions == ipg.positions)

# 不同文件之间不能撞
other = IdentityPrimeGen("alice", "病历B", range(6))
check("不同 file_key 不撞", len(set(other.positions) & set(primes)) == 0)
other2 = IdentityPrimeGen("bob", "病历A", range(6))
check("不同 owner 不撞", len(set(other2.positions) & set(primes)) == 0)

# 键的构造：分隔符必须能防住 "a"+bc 与 ab"+c 这类拼接歧义
check(
    "键分隔符防拼接歧义",
    identity_key("a", "b-c", 0) != identity_key("a-b", "c", 0),
    f"{identity_key('a','b-c',0)!r} vs {identity_key('a-b','c',0)!r}",
)

# 【关键收益】删了再传同一个块，拿到的还是同一个素数 —— 旧方案这里会留空洞
cyc = IdentityPrimeGen("alice", "病历A", range(3))
before = cyc.positions
cyc2 = IdentityPrimeGen("alice", "病历A", range(3))  # 模拟"删了再传"
check("★ 删了再传同一块 → 同一素数（旧方案会留空洞）", cyc2.positions == before)

# 【关键收益】没有位置预算上限 —— 旧方案 n_max=8192 就用满了
t0 = time.perf_counter()
big = IdentityPrimeGen("alice", "大文件", range(2000))
t1 = time.perf_counter()
check("★ 2000 个块可派生（旧方案要看 n_max 脸色）", big.max_sz == 2000)
print(f"        （2000 个块派生耗时 {t1 - t0:.2f}s，未命中缓存时）")

# 重复块号必须被拒绝（否则两个位置配同一个素数）
check("重复块号被拒", _raises(lambda: IdentityPrimeGen("a", "b", [0, 1, 1]), ValueError))


# ---------------------------------------------------------------------------
# 2. 双轨对照：同一份数据，两种素数映射都要能跑通完整流程
# ---------------------------------------------------------------------------
section("2. 双轨对照（基线 PrimeGen vs 新 IdentityPrimeGen）")

L = 16
N_MAX = 8
N = 6
VALS = [3, 5, 7, 11, 13, 17]
I = [0, 2, 4]
VALS_I = [VALS[i] for i in I]


def run_pipeline(primegen, label: str) -> dict:
    """走一遍 specialize → commit → open → verify(+disagg)。"""
    print(f"\n  --- {label} ---")
    base = setup(lambda_bits=128, l=L, n=N_MAX, modulus_bits=512)
    crs = dataclasses.replace(base, primegen=primegen)
    crs_n = specialize(crs, N)

    com = commit(crs_n, VALS)
    op = open_subvector(crs_n, I, VALS_I, VALS)
    rep = verify(crs_n, com.C, I, VALS_I, op)
    ok = passed(rep)
    print(f"       commit.C 位长 = {com.C.bit_length()}")
    print(f"       verify.code   = {rep.code}  ({rep.message})")
    check(f"{label}：verify 通过", ok, str(rep.code))

    # 负例：篡改一个值，必须验不过
    bad = list(VALS_I)
    bad[0] = (bad[0] + 1) % 1000
    rep_bad = verify(crs_n, com.C, I, bad, op)
    check(f"{label}：篡改后必须被拒", not passed(rep_bad), str(rep_bad.code))

    # 负例：值越界
    rep_oob = verify(crs_n, com.C, I, [10**9] * len(I), op)
    check(f"{label}：越界值被拒", not passed(rep_oob), str(rep_oob.code))

    return {"crs_n": crs_n, "C": com.C, "aux": com.aux, "op": op}


t0 = time.perf_counter()
base_res = run_pipeline(PrimeGen(N_MAX, L + 1), "基线 PrimeGen")
t_base = time.perf_counter() - t0

t0 = time.perf_counter()
id_res = run_pipeline(IdentityPrimeGen("alice", "病历A", range(N)), "新 IdentityPrimeGen")
t_id = time.perf_counter() - t0

print(f"\n  耗时对照：基线 {t_base * 1000:.0f} ms · 新方案 {t_id * 1000:.0f} ms"
      f"（含首次派生，差值就是素数生成的成本）")


# ---------------------------------------------------------------------------
# 3. disagg（分解）在新素数映射下也要成立
# ---------------------------------------------------------------------------
section("3. disagg 分解")

for label, res in (("基线", base_res), ("新方案", id_res)):
    crs_n = res["crs_n"]
    K = [0, 2]
    try:
        pi_K = disagg(crs_n, I, VALS_I, res["op"], K)
        # ★ 比“能调用”有意义得多：拆出来的证据必须**真的能验过**
        rep_K = verify(crs_n, res["C"], K, [VALS[i] for i in K], pi_K)
        print(f"  {label}: disagg → K={K} 的证明，再 verify：code={rep_K.code}")
        check(f"{label}：disagg 拆出的证据能验过", passed(rep_K), str(rep_K.code))
    except Exception as exc:  # noqa: BLE001
        check(f"{label}：disagg 拆出的证据能验过", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 4. 缓存行为
# ---------------------------------------------------------------------------
section("4. 派生缓存")

IdentityPrimeGen.clear_cache()
t0 = time.perf_counter()
IdentityPrimeGen("carol", "缓存测试", range(300))
t_cold = time.perf_counter() - t0
t0 = time.perf_counter()
IdentityPrimeGen("carol", "缓存测试", range(300))
t_warm = time.perf_counter() - t0
print(f"  300 块：冷 {t_cold * 1000:.0f} ms → 热 {t_warm * 1000:.2f} ms")
check("第二次构造走缓存（应快 10 倍以上）", t_warm < t_cold / 10)
print(f"  缓存条目数 = {IdentityPrimeGen.cache_size()}")


# ---------------------------------------------------------------------------
section("结论")
if FAILURES:
    print(f"✗ {len(FAILURES)} 项失败：")
    for f in FAILURES:
        print("   - " + f)
    sys.exit(1)
print("✓ 全部通过 —— IdentityPrimeGen 可以替代 PrimeGen")
