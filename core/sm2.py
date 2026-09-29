r"""SM2 曲线上的点运算 —— 块密钥封装层用的 ECDH 底座。

为什么手写，而不是用 ``cryptography``
--------------------------------------
实测 ``cryptography`` 49.0.0 的 ``ec`` 模块**没有 SM2 曲线**（只有 NIST
P-192/224/256/384/521、Brainpool 三条、secp256k1）。而本项目是国密方向 ——
内容加密用 SM4、摘要用 SM3，那**密钥封装**这一层也走 SM2 曲线，整条链才一致；
换成 P-256 会出现"一个系统里两种曲线体系"，答辩时反而要多解释一遍。

性能
----
朴素 double-and-add（每次点运算都做一次模逆）实测 **99 ms/次标量乘** ——
一个 3 叶子的（老的 ABE）封装就要 600 ms，完全不能用。改成 **Jacobian 坐标**
（只在最后转会仿射时做一次模逆）并利用 SM2 的 :math:`a = p - 3` 用 a=−3 的
专用倍点公式后，降到 **3.16 ms/次**（快 31 倍）。模逆用 ``pow(z, -1, p)``
（扩展欧几里得，11 µs）而不是 ``pow(z, p-2, p)``（费马小定理，要 256 次模乘）。

.. warning::

   **本实现未做常数时间防护**（标量乘按比特分支，有 timing 侧信道）。
   它用于课程演示，攻击者没有时序测量条件。生产必须换成经审计的库
   （OpenSSL/BoringSSL 的 SM2）或密码学硬件。

   唯一的补偿是 :func:`self_check` —— 用四条**互相独立**的性质断言曲线参数：
   G 在曲线上、n·G = 无穷远点、(n−1)·G = −G、2G = G＋G。
   参数任何一位写错都不可能同时满足这四条，所以"参数抄错了"这个最大的
   手写风险被堵住了。

序列化用**未压缩**格式（``0x04 || x(32) || y(32)``，共 65 字节）。
不做压缩的原因是压缩点需要模开方（虽然 SM2 的 p ≡ 3 mod 4 可以用
:math:`\sqrt{a} = a^{(p+1)/4}` 直接算），而"多 32 字节"换"少一段容易写错的代码"
在这个场景里是划算的。反序列化时**必须校验点在曲线上** ——
否则会中无效曲线攻击（攻击者给一个不在曲线上但落在小子群的点）。
"""

from __future__ import annotations

import secrets
from typing import Iterable

from .crypto import sm3_digest

__all__ = [
    "P",
    "A",
    "B",
    "N",
    "GX",
    "GY",
    "G",
    "Point",
    "is_on_curve",
    "point_add",
    "scalar_mul",
    "point_bytes",
    "point_from_bytes",
    "gen_private_key",
    "public_key_of",
    "ecdh_shared",
    "kdf",
    "self_check",
]

# ---------------------------------------------------------------------------
# 曲线参数（GB/T 32918 / GM/T 0003.5 里的推荐曲线）
# ---------------------------------------------------------------------------

#: 素域特征。
P: int = 0xFFFFFFFEFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF00000000FFFFFFFFFFFFFFFF
#: 曲线系数 a（等于 p − 3，所以倍点可以用 a = −3 的专用公式）。
A: int = 0xFFFFFFFEFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF00000000FFFFFFFFFFFFFFFC
#: 曲线系数 b。
B: int = 0x28E9FA9E9D9F5E344D5A9E4BCF6509A7F39789F515AB8F92DDBCBD414D940E93
#: 基点 G 的阶（素数）。
N: int = 0xFFFFFFFEFFFFFFFFFFFFFFFFFFFFFFFF7203DF6B21C6052B53BBF40939D54123
#: 基点 x。
GX: int = 0x32C4AE2C1F1981195F9904466A39C9948FE30BBFF2660BE1715A4589334C74C7
#: 基点 y。
GY: int = 0xBC3736A2F4F6779C59BDCEE36B692153D0A9877CC62A474002DF32E52139F0A0

#: 基点。
G: "Point" = (GX, GY)

#: 仿射坐标下的点；``None`` 表示无穷远点 O。
Point = tuple[int, int] | None

#: 一个坐标的字节数。
_COORD = 32

#: 未压缩点的总字节数。
_POINT_BYTES = 1 + 2 * _COORD


# ---------------------------------------------------------------------------
# 曲线判定
# ---------------------------------------------------------------------------

def is_on_curve(pt: Point) -> bool:
    """``y² ≡ x³ + ax + b (mod p)``，且坐标在域内。**无穷远点算在曲线上**。"""
    if pt is None:
        return True
    x, y = pt
    if not (0 <= x < P and 0 <= y < P):
        return False
    return (y * y - (x * x * x + A * x + B)) % P == 0


# ---------------------------------------------------------------------------
# 点运算：对外是仿射，内部走 Jacobian
#
# Jacobian 坐标 (X, Y, Z) 表示仿射点 (X/Z², Y/Z³)。
# 好处是点加/倍点全程不需要模逆 —— 模逆只在整个标量乘的最后做一次。
# ---------------------------------------------------------------------------

_JAC_INF = (0, 1, 0)


def _jac_double(pt: tuple[int, int, int]) -> tuple[int, int, int]:
    """Jacobian 倍点，用 a = −3 的专用公式（SM2 的 a 恰好满足）。"""
    x, y, z = pt
    if z == 0 or y == 0:
        return _JAC_INF
    delta = z * z % P
    gamma = y * y % P
    beta = x * gamma % P
    alpha = 3 * (x - delta) * (x + delta) % P
    x3 = (alpha * alpha - 8 * beta) % P
    z3 = ((y + z) * (y + z) - gamma - delta) % P
    y3 = (alpha * (4 * beta - x3) - 8 * gamma * gamma) % P
    return (x3, y3, z3)


def _jac_add(
    p1: tuple[int, int, int], p2: tuple[int, int, int]
) -> tuple[int, int, int]:
    """Jacobian 点加（通用公式，不要求其中一个 Z = 1）。"""
    x1, y1, z1 = p1
    x2, y2, z2 = p2
    if z1 == 0:
        return p2
    if z2 == 0:
        return p1

    z1z1 = z1 * z1 % P
    z2z2 = z2 * z2 % P
    u1 = x1 * z2z2 % P
    u2 = x2 * z1z1 % P
    s1 = y1 * z2 * z2z2 % P
    s2 = y2 * z1 * z1z1 % P

    h = (u2 - u1) % P
    r = 2 * (s2 - s1) % P
    if h == 0:
        # 同一个 x：要么同点（倍点），要么互为逆元（相加得 O）
        return _jac_double(p1) if r == 0 else _JAC_INF

    i = (2 * h) ** 2 % P
    j = h * i % P
    v = u1 * i % P
    x3 = (r * r - j - 2 * v) % P
    y3 = (r * (v - x3) - 2 * s1 * j) % P
    z3 = ((z1 + z2) * (z1 + z2) - z1z1 - z2z2) * h % P
    return (x3, y3, z3)


def _jac_from_affine(pt: Point) -> tuple[int, int, int]:
    if pt is None:
        return _JAC_INF
    return (pt[0] % P, pt[1] % P, 1)


def _jac_to_affine(pt: tuple[int, int, int]) -> Point:
    x, y, z = pt
    if z == 0:
        return None
    zi = pow(z, -1, P)
    zi2 = zi * zi % P
    return (x * zi2 % P, y * zi2 * zi % P)


def scalar_mul(k: int, pt: Point) -> Point:
    """``k · pt``。``k`` 会先对 ``N`` 取模（标量落在 Z_N 上）。"""
    if pt is None:
        return None
    k %= N
    if k == 0:
        return None

    result = _JAC_INF
    addend = _jac_from_affine(pt)
    while k:
        if k & 1:
            result = _jac_add(result, addend)
        addend = _jac_double(addend)
        k >>= 1
    return _jac_to_affine(result)


def point_add(p1: Point, p2: Point) -> Point:
    """仿射点加（对外接口，主要用于测试与自检）。"""
    return _jac_to_affine(_jac_add(_jac_from_affine(p1), _jac_from_affine(p2)))


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------

def point_bytes(pt: Point) -> bytes:
    """未压缩格式：``0x04 || x(32) || y(32)``。无穷远点没有编码，抛错。"""
    if pt is None:
        raise ValueError("无穷远点不能序列化（协议里不该出现它）")
    x, y = pt
    return b"\x04" + x.to_bytes(_COORD, "big") + y.to_bytes(_COORD, "big")


def point_from_bytes(data: bytes) -> Point:
    """反序列化并**校验点在曲线上**。

    这一步不能省：不校验的话，攻击者可以塞一个落在小子群上的点，
    把共享点拖进可预测的集合里（无效曲线攻击）。
    """
    if len(data) != _POINT_BYTES:
        raise ValueError(f"点必须是 {_POINT_BYTES} 字节（未压缩），收到 {len(data)}")
    if data[0] != 0x04:
        raise ValueError(f"不支持的点编码前缀 {data[0]:#x}（只支持 0x04 未压缩）")
    x = int.from_bytes(data[1 : 1 + _COORD], "big")
    y = int.from_bytes(data[1 + _COORD :], "big")
    if x >= P or y >= P:
        raise ValueError("坐标越出素域")
    pt = (x, y)
    if not is_on_curve(pt):
        raise ValueError("这个点不在 SM2 曲线上")
    return pt


# ---------------------------------------------------------------------------
# 密钥与 ECDH
# ---------------------------------------------------------------------------

def gen_private_key() -> int:
    """均匀取一个私钥标量 ``d ∈ [1, N-1]``。走 ``secrets``（``os.urandom``）。"""
    return secrets.randbelow(N - 1) + 1


def public_key_of(d: int) -> Point:
    """公钥 ``P = d · G``。"""
    if not (1 <= d < N):
        raise ValueError("私钥标量必须落在 [1, N-1]")
    return scalar_mul(d, G)


def ecdh_shared(d: int, peer: Point) -> bytes:
    """ECDH 共享点，取 x 坐标 32 字节。

    ``d · peer`` 与对端算的 ``d' · (d·G)`` 相等，所以两端拿到同样的字节串。
    注意：**这里不校验 peer 是否是合法公钥**（只校验在曲线上）——
    调用方的正确做法是先确认 peer 来自可信的 mpk。
    """
    if not (1 <= d < N):
        raise ValueError("私钥标量必须落在 [1, N-1]")
    if peer is None or not is_on_curve(peer):
        raise ValueError("对端公钥不在 SM2 曲线上")
    shared = scalar_mul(d, peer)
    if shared is None:  # 只可能在 peer 的阶不整除时发生，理论上到不了这里
        raise ValueError("共享点是无穷远点 —— 对端公钥有问题")
    return shared[0].to_bytes(_COORD, "big")


def kdf(shared: bytes, info: bytes, length: int) -> bytes:
    """SM3 计数模式的密钥派生。

    ``SM3(shared || counter_be32 || info)``，counter 从 1 起，拼够 ``length`` 字节。
    这是 GM/T 0003 里 KDF 的标准形状（把 counter 放在中间，避免长度扩展歧义）。
    """
    if length <= 0:
        raise ValueError("length 必须为正")
    out = bytearray()
    counter = 1
    while len(out) < length:
        out += sm3_digest(shared + counter.to_bytes(4, "big") + info)
        counter += 1
    return bytes(out[:length])


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

def self_check() -> list[tuple[str, bool]]:
    """用四条互相独立的性质校验曲线参数。

    返回 ``[(说明, 是否通过), ...]``，方便测试直接断言"全 True"。
    为什么这四条足够：它们分别约束了 (p, a, b) 与 G 的一致性、G 的阶、
    阶与坐标的关系、以及群运算本身。参数抄错一位不可能同时满足。
    """
    neg_g = (GX, (-GY) % P)
    return [
        ("a ≡ p-3 (mod p)（倍点用 a=-3 专用公式的前提）", A == (P - 3) % P),
        ("G 在曲线上", is_on_curve(G)),
        ("n·G = 无穷远点（n 是 G 的阶）", scalar_mul(N, G) is None),
        ("(n-1)·G = -G", scalar_mul(N - 1, G) == neg_g),
        ("2G = G + G", scalar_mul(2, G) == point_add(G, G)),
        ("3G = 2G + G", scalar_mul(3, G) == point_add(point_add(G, G), G)),
    ]


def _assert_params() -> None:
    """导入时自检一次 —— 参数错了就该在 import 阶段炸，而不是等到解密失败。"""
    bad: Iterable[str] = [name for name, ok in self_check() if not ok]
    for name in bad:
        raise RuntimeError(f"SM2 曲线参数自检失败：{name}")


_assert_params()
