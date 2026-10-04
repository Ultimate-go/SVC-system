r"""公开参数会话 —— **新方案：一文件一向量**。

★ 与旧设计（设计 B：全系统一条向量）相比，本模块的语义变了一处，其余原样：
每个文件占**自己的一段**全局素数位置 ``[offset, offset+n)``，于是

* :math:`E_{\text{file}} = \prod_{offset \le j < offset+n} e_j` 是**区间积**
  （设计 B 的 :math:`e_{[n]}` 是前缀积）；
* :math:`U_{\text{file}} = g^{E_{\text{file}}}` 只取决于本文件的段与长度，
  所以**上传新文件不会改变任何老文件的** ``U`` / ``C``
  （设计 B 下每次上传都会推进全局 ``n``，让所有节点的份额一起失效）；
* 位置号仍然**全局唯一**（素数表只有一张）：文件的第 ``i`` 块就是素数表第
  ``offset+i`` 个素数。靠 :class:`ShiftedPrimeGen` 这个「素数视图」实现，
  :mod:`svc` 层那些按局部下标 ``0..n-1`` 写的算法**一行都不用改**。

代价转移到了「跨文件聚合」：不同文件的 :math:`E` 不同，把两份份额抬到同一个
:math:`E` 需要求 :math:`e_I` 次根，而隐藏阶群里没有陷门（见
:mod:`svc.groups`）。所以跨文件聚合走「承诺增量乘 + 份额在合并位置集上重算」。

与 :class:`vds.vds.VDSSession` 的差别
----------------------------------------
===== ================================== ==========================================
         ``VDSSession``（论文原样）           ``GlobalSession``（设计 B）
===== ================================== ==========================================
向量    一个文件一条                        **全系统一条**
``n``   文件块数                            **全局已用位置数**
N、g   由 ``__init__`` 现场生成              由外界**注入**，绝不重新生成
===== ================================== ==========================================

第二条差异的后果最容易被忽略：**每一次上传都会推进 ``n``**，从而让
:math:`E` 变大，于是**每个节点**的 :math:`S_I = g^{E/e_I}` 与
:math:`\Lambda_I` 都失效。所以在「全系统一条向量」的旧设计（设计 B）下，
"追加"天然是一次全网事件，靠 :func:`vds.updates.update_append`
做成一次全网两段式更新。

★ 新方案（一文件一向量）把这条代价**限制在一份文件内部**：上传新文件只是
给它分一段**没用过的**位置，老文件的 :math:`E` 逐位不变，于是老文件的
:math:`U`、:math:`C`、以及**各节点手里的份额全都原样有效**，一次更新只牵动
"这份文件所涉及的那些节点"。

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

import copy
import dataclasses

from svc import CRS, CRSn, DeterministicRNG, PrimeGen, generate_primes, product_tree

from vds.digest import Digest

__all__ = [
    "GlobalSession",
    "ShiftedPrimeGen",
    "MappingPrimeGen",
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

#: 素数表容量，也就是**全系统位置总预算**。
#:
#: ★ 新方案（一文件一向量）里每个文件占一段**互不重叠**的位置，所以这个数是
#: 「**所有文件的块数之和**」的上限，而不是「单个文件的块数」上限。
#: 段**永不回收**（回收会让同一个位置对应上两个不同的素数，而两个承诺可能同时
#: 存在），所以用满之后位置不再增长，此时只能重建 CRS。
#: 1 KB 一块 ⇒ 全系统总共约 64 MB。
#: 素数表是惰性生成的，8192 个 257 位素数实测 0.16 s，所以这个数调大几乎无成本。
DEFAULT_N_MAX: int = 65536

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


class ShiftedPrimeGen:
    """把素数表的读指针整体右移 ``offset`` —— 一个文件自己的「素数视图」。

    :meth:`get` 返回素数表第 ``offset + i`` 个素数。:mod:`svc` 层的算法全部按
    "局部下标 ``0..n-1``"写（``primegen.get(i)``），套上这个视图之后它们就自动
    跑在「该文件的段」上 —— **这是新方案落地时最关键的一招**：密码学内核不动，
    只有视图在变。其余属性（``max_sz`` / ``bits`` / ``first`` …）原样透传。
    """

    __slots__ = ("_pg", "_off")

    def __init__(self, pg, off: int) -> None:
        self._pg = pg
        self._off = int(off)

    @property
    def offset(self) -> int:
        return self._off

    def get(self, i: int) -> int:
        return self._pg.get(self._off + int(i))

    def first(self, count: int) -> list[int]:
        return [self.get(i) for i in range(int(count))]

    def get_many(self, indices) -> list[int]:
        return [self.get(i) for i in indices]

    def __getattr__(self, name: str):
        # dunder 不转发：pickle / copy 会探测一堆双下划线属性，转发过去
        # 只会得到莫名其妙的 AttributeError 或死循环。
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return getattr(self._pg, name)

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"ShiftedPrimeGen(offset={self._off}, 底层={type(self._pg).__name__})"


class MappingPrimeGen:
    """把一串**显式位置**按序重编号成密集下标 —— 多段文件的素数视图。

    :meth:`get` 返回全局位置 ``positions[i]`` 上的素数。一份文件如果被追加过
    多次，它的位置是若干段的并集（见 :attr:`vds.digest.Digest.chunks`），
    此时一个单纯的偏移量就不够用了，需要这张显式对照表。

    单段文件仍然走 :class:`ShiftedPrimeGen`（少一层查表），这是绝大多数情况。
    """

    __slots__ = ("_pg", "_pos")

    def __init__(self, pg, positions) -> None:
        self._pg = pg
        self._pos = tuple(int(p) for p in positions)

    @property
    def positions(self) -> tuple[int, ...]:
        return self._pos

    def get(self, i: int) -> int:
        return self._pg.get(self._pos[int(i)])

    def first(self, count: int) -> list[int]:
        return [self.get(i) for i in range(int(count))]

    def get_many(self, indices) -> list[int]:
        return [self.get(i) for i in indices]

    def __getattr__(self, name: str):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return getattr(self._pg, name)

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return (f"MappingPrimeGen({len(self._pos)} 个位置, "
                f"前几个={list(self._pos[:4])})")


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------

class GlobalSession:
    """公开参数会话：持有 ``CRS``，缓存每段、每长度下的 ``crs_n`` 与素数视图。

    除了 :mod:`vds` 层需要的四个成员，本类**不持有任何状态数据** ——
    向量内容、节点份额、登记表都在 :class:`~core.store.VectorStore` 里。
    这样"公开参数"与"业务数据"的边界是干净的：前者只需一份，后者按需扩展。
    """

    __slots__ = (
        "crs",
        "l",
        "n_max",
        "_e_all_cache",
        "_crsn_cache",
        "_view_cache",
        "_map_view_cache",
        "_e_all_pos_cache",
    )

    def __init__(self, crs: CRS, n_max: int | None = None) -> None:
        self.crs = crs
        self.l = int(crs.l)
        self.n_max = int(n_max if n_max is not None else crs.primegen.max_sz)
        if self.n_max > crs.primegen.max_sz:
            raise ValueError(
                f"n_max = {self.n_max} 超过素数表容量 {crs.primegen.max_sz}："
                f"位置上限在 Bootstrap 阶段就定死了，事后无法扩"
            )
        #: ``(offset, n) -> E``：本文件那段素数的连乘，见 :meth:`e_all_for`
        self._e_all_cache: dict[tuple[int, int], int] = {}
        #: ``(位置列表, n) -> CRSn``：按位置集与长度缓存的向量上下文
        self._crsn_cache: dict[tuple[tuple[int, ...], int], CRSn] = {}
        #: ``offset -> CRS``：单段文件的素数视图（offset=0 时直接用 ``self.crs``）
        self._view_cache: dict[int, CRS] = {}
        #: ``位置列表 -> CRS``：多段文件的素数视图，见 :meth:`view_crs_for`
        self._map_view_cache: dict[tuple[int, ...], CRS] = {}
        #: ``位置列表 -> E``：多段文件的区间积，见 :meth:`e_all_of`
        self._e_all_pos_cache: dict[tuple[int, ...], int] = {}

    # -- 视图与缓存 ---------------------------------------------------------

    def view_crs(self, offset: int) -> CRS:
        """该文件段的素数视图：``primegen.get(i)`` 给出素数表第 ``offset+i`` 个。

        ``offset = 0`` 时**就是**公开参数本身（不进缓存、不复制），
        所以旧数据（全部落在第 0 段）的行为与改造前逐位一致。
        """
        offset = int(offset)
        if offset < 0:
            raise ValueError("offset 不能为负")
        if offset == 0:
            return self.crs
        got = self._view_cache.get(offset)
        if got is None:
            pg = ShiftedPrimeGen(self.crs.primegen, offset)
            try:
                got = dataclasses.replace(self.crs, primegen=pg)
            except TypeError:  # CRS 不是 dataclass 时的退路
                got = copy.copy(self.crs)
                object.__setattr__(got, "primegen", pg)
            self._view_cache[offset] = got
        return got

    def view_crs_for(self, positions) -> CRS:
        """按**显式位置列表**取素数视图 —— 多段文件的正确入口。

        位置连续时（占绝大多数）退化成 :meth:`view_crs`：那种情况只需要一个
        偏移量，比逐位置对照表更省，也更好读。
        """
        pos = tuple(int(p) for p in positions)
        if not pos:
            return self.crs
        if pos == tuple(range(pos[0], pos[0] + len(pos))):
            return self.view_crs(pos[0])
        got = self._map_view_cache.get(pos)
        if got is None:
            pg = MappingPrimeGen(self.crs.primegen, pos)
            try:
                got = dataclasses.replace(self.crs, primegen=pg)
            except TypeError:  # CRS 不是 dataclass 时的退路
                got = copy.copy(self.crs)
                object.__setattr__(got, "primegen", pg)
            self._map_view_cache[pos] = got
        return got

    def e_all_of(self, positions) -> int:
        r"""本文件那**一组位置**上的素数连乘 :math:`E = \prod_{j \in P} e_j`。

        与 :meth:`e_all_for` 的关系：那个要求位置是连续区间（能按
        ``(offset, n)`` 缓存，更快），这里是任意位置集的通用入口。
        位置连续时会自动转交给它。
        """
        pos = tuple(int(p) for p in positions)
        if not pos:
            return 1
        if pos == tuple(range(pos[0], pos[0] + len(pos))):
            return self.e_all_for(pos[0], len(pos))
        cached = self._e_all_pos_cache.get(pos)
        if cached is None:
            cached = product_tree([self.crs.primegen.get(p) for p in pos])
            self._e_all_pos_cache[pos] = cached
        return cached

    def e_all_for(self, offset: int, n: int | None = None) -> int:
        r"""本文件那段素数的连乘 :math:`E = \prod_{offset \le j < offset+n} e_j`。

        ★ 新方案里这是**区间积**，不是设计 B 的前缀积 :math:`e_{[n]}`：
        每个文件占自己那段 ``[offset, offset+n)``，于是上传新文件不会改变
        任何老文件的 :math:`E`（也就不会改变它的 :math:`U = g^E`）——
        这正是「一文件一凭证」要的效果。

        位置号是**全局**的（素数表只有一张），所以这里直接用全局位置号取素数，
        不存在「局部下标」这种中间概念。

        兼容旧调用 ``e_all_for(n)``：只传一个参数时按 ``offset=0`` 处理，
        也就是设计 B 的前缀积。
        """
        if n is None:  # 兼容 e_all_for(n)
            offset, n = 0, offset
        offset, n = int(offset), int(n)
        if offset < 0 or n < 0:
            raise ValueError("offset 与 n 都不能为负")
        if offset + n > self.n_max:
            raise ValueError(
                f"位置 {offset}..{offset + n - 1} 超出位置预算 {self.n_max}"
                f"（位置预算在 Bootstrap 阶段定死，事后无法扩）"
            )
        key = (offset, n)
        cached = self._e_all_cache.get(key)
        if cached is None:
            es = [self.crs.primegen.get(offset + i) for i in range(n)]
            cached = product_tree(es) if es else 1
            self._e_all_cache[key] = cached
        return cached
    def primegen_for(self, delta: Digest):
        """该文件段上的素数映射 —— :mod:`svc` 层要用的那一份。

        ★ 新方案下**任何**「按本文件的下标取素数」的地方都必须走这里，
        而不是 ``session.crs.primegen``（那是第 0 段的全局表）。取错表**不会
        报错**，只会让 :math:`e_i` 对不上，然后以「ShamirTrick 同源自检失败」
        这种极难归因的形式炸掉 —— 所以宁可多绕一道，也要收口到这一个入口。

        与 :meth:`crs_n_for` 的区别：这里**不要求** ``n > 0``，
        于是"新文件第一次上传"（``n = 0``）也能取到正确的表。
        """
        pos = tuple(int(p) for p in delta.positions)
        if not pos:
            # ★ 空向量（新文件第一次追加）：``positions`` 是空的，但视图仍必须由
            #   ``offset`` 决定。退回全局表会让素数取错（拿到全局第 0 个，而不是
            #   该段第 0 个），而表现是两种完全不像"取错素数"的报错：
            #   ``U_new`` 与 ``S_K^{e_K}`` 对不上，节点侧 ``add_storage`` 报
            #   「S_I 校验失败」。这个坑很隐蔽，别把这一分支删掉。
            return self.view_crs(int(getattr(delta, "offset", 0))).primegen
        return self.view_crs_for(pos).primegen
    def crs_n_for(self, delta: Digest) -> CRSn:
        """由摘要取出与它匹配的 :class:`~svc.CRSn`。

        :math:`U_n` **直接取摘要里的值** —— 不自己重算 :math:`g^{E}`
        （那是一次指数位长 :math:`n(l+1)` 的模幂）。这正是论文把 ``U``
        挂进摘要而不是放进公开参数的原因。

        ★ 新方案下这里的 ``crs`` 是**该文件段的素数视图**（:meth:`view_crs`）：
        ``crs_n.primegen.get(i)`` 给出素数表第 ``offset+i`` 个素数，于是
        :mod:`svc` 层整套按「局部下标``0..n-1``」写的算法不用改就能跑在
        「文件自己的段」上。
        """
        n = int(delta.n)
        if n <= 0:
            raise ValueError("空向量（n=0）没有 crs_n")
        positions = tuple(int(p) for p in delta.positions)
        key = (positions, n)
        cached = self._crsn_cache.get(key)
        if cached is None or cached.U_n != delta.U:
            cached = CRSn(
                crs=self.view_crs_for(positions),
                U_n=delta.U,
                e_all=self.e_all_of(positions),
                n=n,
            )
            self._crsn_cache[key] = cached
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
        return Digest(U=self.crs.g, C=1, n=0, offset=0)

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
