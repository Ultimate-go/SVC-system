r"""全局向量会话（设计 B）。

与 :class:`vds.vds.VDSSession` 的三处差别
----------------------------------------
===== ================================== ==========================================
         ``VDSSession``（论文原样）           ``GlobalSession``（设计 B）
===== ================================== ==========================================
向量    一个文件一条                        **全系统一条**
``n``   文件块数                            **全局已用位置数**
N、g   由 ``__init__`` 现场生成              由外界**注入**，绝不重新生成
===== ================================== ==========================================

第二条差异的后果最容易被忽略：**任何一次上传都会推进 ``n``**，从而让
``e_[n]`` 变大，于是**每个节点**的 :math:`S_I = g^{e_{[n]}/e_I}` 与
:math:`\Lambda_I` 都失效。所以设计 B 下"追加"天然是一次全网事件 ——
本层用 :func:`vds.updates.update_append` 把它做成一次全网两段式更新。

鸭子类型接口
------------
``vds`` 层的 ``StorageNode`` / ``ClientNode`` / ``updates`` 只用到会话的四个成员：

* ``session.crs``                —— ``CRS``（含 ``N``、``g``、``primegen``、``l``）
* ``session.crs_n_for(delta)``   —— 由摘要取 ``CRSn``
* ``session.n_max``              —— 位置上限
* ``session.l``                  —— 每个分量的比特数

本类只实现这些，就能让上面三层原封不动地跑在设计 B 上。
"""

from __future__ import annotations

from svc import CRS, CRSn, DeterministicRNG, PrimeGen, generate_primes, product_tree

from vds.digest import Digest

__all__ = [
    "GlobalSession",
    "make_crs",
    "get_primegen",
    "crs_to_dict",
    "crs_from_dict",
    "new_session",
    "DEFAULT_L",
    "DEFAULT_N_MAX",
    "DEFAULT_MODULUS_BITS",
]


#: 每个分量的比特数。取 256 是为了正好装下一个 SM3 摘要（32 字节）。
DEFAULT_L: int = 256

#: 全局位置上限（素数表容量）。**一旦定死不可改** —— 隐藏阶群方案的结构性约束。
DEFAULT_N_MAX: int = 1024

#: 演示档模数位长。**无安全强度，仅演示**；真实部署论文配置是 16λ = 2048。
DEFAULT_MODULUS_BITS: int = 1024


# ---------------------------------------------------------------------------
# 素数表缓存（规划 §6.2）
# ---------------------------------------------------------------------------

#: ``(n_max, bits) -> PrimeGen``。素数只由这两个参数决定，可以全局共享。
_PRIMEGEN_CACHE: dict[tuple[int, int], PrimeGen] = {}


def get_primegen(n_max: int, bits: int) -> PrimeGen:
    """取（或新建）一个按 ``(n_max, bits)`` 缓存的素数映射。

    :class:`~svc.primegen.PrimeGen` 是**惰性**的：构造耗时 0 ms，成本全在
    第一次 :meth:`~svc.primegen.PrimeGen.first`。而 ``svc.setup()`` 每调用一次
    就新建一个实例 —— 意味着**每上传一个文件都要重新生成一遍素数表**
    （实测 n=1024 时 1442 ms）。本函数就是为堵这个洞而存在。

    快照之所以安全：``PrimeGen`` 完全由 ``(max_sz, bits)`` 决定
    （起点恒为 :math:`2^{\\text{bits}-1}`，单调扫描），同一个键必然给出
    逐位相同的素数序列。
    """
    if n_max <= 0:
        raise ValueError("n_max 必须为正")
    if bits < 3:
        raise ValueError("bits 至少为 3")
    key = (n_max, bits)
    pg = _PRIMEGEN_CACHE.get(key)
    if pg is None:
        pg = PrimeGen(max_sz=n_max, bits=bits)
        _PRIMEGEN_CACHE[key] = pg
    return pg


# ---------------------------------------------------------------------------
# CRS 的生成与（反）序列化
# ---------------------------------------------------------------------------

def make_crs(
    l: int = DEFAULT_L,
    n_max: int = DEFAULT_N_MAX,
    modulus_bits: int = DEFAULT_MODULUS_BITS,
    seed: bytes | str | None = None,
) -> CRS:
    """生成公开参数 ``crs = (N, g, primegen, l)``。

    :param seed: ``None``（默认）走真随机 —— **系统里必须保持这个默认**。
        固定种子 = 知道种子的人能重跑出 ``p``、``q`` = 方案彻底失效
        （见 :mod:`svc.rng` 的文档与规划 §6.4）。

    与 ``svc.setup()`` 的唯一区别：素数表走 :func:`get_primegen` 的全局缓存。
    """
    rng = DeterministicRNG(seed)
    N, g = generate_primes(rng, modulus_bits)
    primegen = get_primegen(n_max, l + 1)
    return CRS(N=N, g=g, primegen=primegen, l=l)


def crs_to_dict(crs: CRS) -> dict:
    """把公开参数序列化成可落库的字典。

    **只导出 ``N``、``g``、``l``、``n_max``** —— 这四个都是公开量。
    ``p``、``q``、``φ(N)`` 不在此列，也绝不应出现在任何别的地方：
    知道 ``φ(N)`` 就能求 ``e`` 次根，从而**伪造任意证据**。

    素数映射不必存：它只由 ``(n_max, l+1)`` 决定，可以用
    :func:`get_primegen` 逐位重建。
    """
    return {
        "N": str(crs.N),          # 大整数一律走十进制字符串
        "g": str(crs.g),
        "l": int(crs.l),
        "n_max": int(crs.primegen.max_sz),
    }


def crs_from_dict(d: dict) -> CRS:
    """由 :func:`crs_to_dict` 的结果重建 ``CRS``。

    这是跨进程 / 重启后复用同一套公开参数的**唯一**正确方式
    （规划 §6.1）。自己再 ``make_crs()`` 一次会得到另一个群，验证必然失败。
    """
    l = int(d["l"])
    n_max = int(d["n_max"])
    return CRS(
        N=int(d["N"]),
        g=int(d["g"]),
        primegen=get_primegen(n_max, l + 1),
        l=l,
    )


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------

class GlobalSession:
    """全系统一条向量的会话：持有公开参数，缓存各 ``n`` 下的 ``crs_n``。

    除了 :mod:`vds` 层需要的四个成员，本类**不持有任何状态数据** ——
    向量内容、节点份额、登记表都在 :class:`~core.store.VectorStore` 里。
    这样"公开参数"与"业务数据"的边界是干净的：前者只需一份，后者按需扩展。
    """

    __slots__ = ("crs", "l", "n_max", "_e_all_cache", "_crsn_cache")

    def __init__(self, crs: CRS, n_max: int | None = None) -> None:
        self.crs = crs
        self.l = int(crs.l)
        self.n_max = int(n_max if n_max is not None else crs.primegen.max_sz)
        if self.n_max > crs.primegen.max_sz:
            raise ValueError(
                f"n_max = {self.n_max} 超过素数表容量 {crs.primegen.max_sz}："
                f"位置上限在 Bootstrap 阶段就定死了，事后无法扩"
            )
        self._e_all_cache: dict[int, int] = {}
        self._crsn_cache: dict[int, CRSn] = {}

    # -- 缓存 ---------------------------------------------------------------

    def e_all_for(self, n: int) -> int:
        """:math:`e_{[n]} = \\prod_{i<n} e_i`，带缓存。

        取前 ``n`` 个（0 基）素数 —— 注意与论文的 1 基编号差一位，
        但 ``e_[n]`` 的含义完全一致。
        """
        if n < 0:
            raise ValueError("n 不能为负")
        cached = self._e_all_cache.get(n)
        if cached is None:
            cached = product_tree(self.crs.primegen.first(n)) if n else 1
            self._e_all_cache[n] = cached
        return cached

    def crs_n_for(self, delta: Digest) -> CRSn:
        """由摘要取出与它匹配的 :class:`~svc.CRSn`。

        :math:`U_n` **直接取摘要里的值** —— 不自己重算 :math:`g^{e_{[n]}}`
        （那是一次指数位长 :math:`n(l+1)` 的模幂，设计 B 下每次上传都会变）。
        这正是论文把 ``U`` 挂进摘要而不是放进公开参数的原因。
        """
        n = int(delta.n)
        if n <= 0:
            raise ValueError("空向量（n=0）没有 crs_n")
        cached = self._crsn_cache.get(n)
        if cached is None or cached.U_n != delta.U:
            cached = CRSn(
                crs=self.crs, U_n=delta.U, e_all=self.e_all_for(n), n=n
            )
            self._crsn_cache[n] = cached
        return cached

    # -- 初始摘要 -----------------------------------------------------------

    def bootstrap(self) -> Digest:
        """空向量的摘要 :math:`\\delta_0 = (U_0, C_0, 0)`。

        .. math::
            U_0 = g^{e_{[0]}} = g^{1} = g, \\qquad C_0 = \\prod_{\\varnothing} = 1

        注意这**不是** ``(1, g)``。论文 §8.2 写的 ``δ0 ← ((1, g), n0)`` 是
        「文件还没建立」的占位约定，它不满足 ``U_n = g^{e_[n]}`` 这个不变式，
        因此**不能**作为增量追加的起点 —— 从 ``(1, g)`` 出发做 ``add_back``
        会得到 ``(1, g^{e_0})``，两项都错。本实现取代数上自洽的 ``(g, 1)``，
        并有一条测试断言「增量追加的结果 == 一次性 ``commit`` 的结果」。
        """
        return Digest(U=self.crs.g, C=1, n=0)

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return (
            f"GlobalSession(l={self.l}, n_max={self.n_max}, "
            f"|N|={self.crs.N.bit_length()}位)"
        )


def new_session(
    l: int = DEFAULT_L,
    n_max: int = DEFAULT_N_MAX,
    modulus_bits: int = DEFAULT_MODULUS_BITS,
    seed: bytes | str | None = None,
) -> GlobalSession:
    """一步建好公开参数与会话（演示/测试用）。

    生产路径应当分成两步：``make_crs`` → 落库 → 之后一律 ``crs_from_dict``。
    """
    return GlobalSession(make_crs(l=l, n_max=n_max, modulus_bits=modulus_bits, seed=seed))
