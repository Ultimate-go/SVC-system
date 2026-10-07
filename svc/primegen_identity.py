"""按**块身份**派生素数 —— 取代「全局位置分配」的素数映射。

为什么要有它
------------
旧方案（:class:`~svc.primegen.PrimeGen`）要求位置是 ``[0, n_max)`` 内的**密集
整数**，所以必须有一个**全局分配器**：上传时从游标取一段号，删文件后那段号
**永不回收**（回收会让同一位置对应上两个素数，整个方案的前提就崩了）。

那套机制能跑，但代价是三条硬伤：

1. **有天花板**：位置发完（``n_max = 8192``）就只能重建 CRS，全库数据作废；
2. **会留空洞**：删文件/截断留下的号段永久废弃，于是 ``store.n``（存活块数）
   **不等于**"合法下标的上界" —— 这类语义错配是好几处 bug 的根源；
3. **要一本账**：谁拿了哪几号必须记在登记表里，删了还必须记得清。

本模块把"分配位置"换成"**按块的身份直接算**"：

    块的身份 = (owner, file_key, block_idx)
        ↓ 拼成字节串（用 ``\\x1f`` 分隔，防 "a"+"bc" 与 "ab"+"c" 撞）
        ↓ hash_prime：反复哈希直到得到素数
    = 这个块的素数（同时也就是它的"位置"）

论文允许这么做
--------------
论文 §5.2 只要求 :math:`e_i` 是**互不相同**的素数；原文明确说
「even just a bijective mapping (which is inherently collision resistant)
would be enough」—— **要的是一一对应，不是"按顺序对应"**。
把"第 ``i`` 个素数"换成"块身份哈希出来的素数"，仍然是一一对应
（碰撞概率见下），所以代数完全不变。

碰撞概率
--------
``prime_bytes = 16``（128 位）时，生日问题下要 **2.2×10¹⁹ 个块**才有 50%
碰撞概率；8192 个块时约 :math:`10^{-31}`。本类仍然**默认开启互异性检查**
（``check_distinct=True``），因为一旦碰撞，``shamir_trick`` 会在
:math:`\\gcd \\ne 1` 时**静默返回错误结果** —— 那种 bug 几乎不可能靠读代码定位。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .mathbase import hash_prime

__all__ = ["IdentityPrimeGen", "identity_key"]


#: 字段分隔符。用 ``\x1f``（ASCII Unit Separator）而不是 ``"-"`` 之类，
#: 是为了让 ``("a", "b-c")`` 与 ``("a-b", "c")`` 拼出不同的字节串。
#: （``core/registry.py`` 里对同一个坑有同样的处理。）
_SEP = b"\x1f"


def identity_key(owner: str, file_key: str, block_idx: int) -> bytes:
    """块身份的**规范字节串** —— 派生该块素数时喂给哈希的东西。

    ★ 这是整个模块的**唯一**身份定义。所有地方（派生、缓存、互异性检查）
      必须走这一个函数，否则会出现"同一个块算出两个素数"这种最难查的错。
    """
    if not owner:
        raise ValueError("owner 不能为空")
    if not file_key:
        raise ValueError("file_key 不能为空")
    idx = int(block_idx)
    if idx < 0:
        raise ValueError(f"block_idx 不能为负，收到 {block_idx}")
    return owner.encode("utf-8") + _SEP + file_key.encode("utf-8") + _SEP + str(idx).encode("ascii")


class IdentityPrimeGen:
    """接口与 :class:`~svc.primegen.PrimeGen` **完全相同**的素数映射。

    :mod:`svc` 层的算法只依赖 ``get(i)`` / ``first(n)`` / ``max_sz`` 三样
    （见 ``svc/scheme.py``），所以把它换成这个类，**密码学内核一行都不用改**。

    ``get(i)`` 里的 ``i`` 是**这份文件内部的块号**（``0..n-1``），
    与旧方案一致 —— 差别只在"第 ``i`` 块配哪个素数"由**身份**决定，
    而不是"第 ``i`` 个素数"。

    :param owner: 文件所有者
    :param file_key: 文件标识
    :param block_indices: 这份文件**实际存在的**块号（按局部顺序）。
        正常就是 ``range(n)``；保留这个参数是为了将来支持"中间挖块"时
        不必再改接口。
    :param prime_bytes: 派生素数的字节长度，默认 16（128 位）。调小会显著
        提高碰撞概率 —— 128 位时 8192 个块的碰撞概率约 :math:`10^{-31}`。
    :param check_distinct: 是否检查互异性（默认开）。关掉它能在超大
        ``n`` 时省一点时间，但**不建议**。

    .. warning::

       本类的实例是**一份文件一个**（``max_sz`` 必须等于该文件的块数，
       ``svc/scheme.py`` 会断言这一点）。它**不是**全局共享的素数表 ——
       这正是它能摆脱 ``n_max`` 天花板的原因。
    """

    __slots__ = ("_owner", "_file_key", "_blocks", "_prime_bytes", "_primes", "_max_sz")

    #: 类级缓存：``键字节串 -> 素数``。**跨实例共享** —— 同一块在
    #: 上传 / 验证 / 改块 / 聚合各条路径上只会被真正派生一次。
    #: 这是把"每次 5.38ms"摊平成"首次才 5.38ms"的关键。
    _CACHE: dict[bytes, int] = {}

    #: 类级互异表：``素数 -> 它的身份键``。用于检测碰撞。
    _SEEN: dict[int, bytes] = {}

    def __init__(
        self,
        owner: str,
        file_key: str,
        block_indices: Iterable[int],
        *,
        prime_bytes: int = 16,
        check_distinct: bool = True,
    ) -> None:
        if prime_bytes < 1:
            raise ValueError("prime_bytes 必须为正")
        self._owner = owner
        self._file_key = file_key
        self._blocks = tuple(int(b) for b in block_indices)
        if any(b < 0 for b in self._blocks):
            raise ValueError("block_indices 不能含负数")
        if len(set(self._blocks)) != len(self._blocks):
            raise ValueError("block_indices 里有重复块号 —— 那会让两个位置配同一个素数")
        self._prime_bytes = int(prime_bytes)
        self._max_sz = len(self._blocks)
        self._primes: list[int] = []
        self._derive_all(check_distinct=check_distinct)

    # -- 派生 ---------------------------------------------------------------

    def _derive_all(self, *, check_distinct: bool) -> None:
        """把这份文件的**全部**素数一次派生出来。

        ★ 一次性做完而不是逐个懒加载：``svc`` 层的 ``commit`` / ``verify``
          会遍历整份文件，逐个派生的话每次都要查缓存字典；一次做完之后
          ``get(i)`` 就是纯列表下标，与旧方案的开销完全一样。
        """
        cache = IdentityPrimeGen._CACHE
        seen = IdentityPrimeGen._SEEN
        out: list[int] = []
        for b in self._blocks:
            key = identity_key(self._owner, self._file_key, b)
            got = cache.get(key)
            if got is None:
                got = hash_prime(key, self._prime_bytes)
                if check_distinct:
                    other = seen.get(got)
                    if other is not None and other != key:
                        raise ValueError(
                            "按身份派生的素数发生碰撞：两个不同的块算出了同一个素数。\n"
                            f"  已登记：{other!r}\n"
                            f"  本次：  {key!r}\n"
                            f"  （prime_bytes={self._prime_bytes} 太小；"
                            "碰撞会让 shamir_trick 静默算错，所以这里直接拒绝）"
                        )
                    seen[got] = key
                cache[key] = got
            out.append(got)
        self._primes = out

    # -- 与 PrimeGen 相同的接口 ---------------------------------------------

    @property
    def max_sz(self) -> int:
        """这份文件的最大块数（``svc/scheme.py`` 会断言它等于 ``n``）。"""
        return self._max_sz

    @property
    def bits(self) -> int:
        """派生素数的位长 —— 与 :class:`~svc.primegen.PrimeGen` 的口径一致。"""
        return self._prime_bytes * 8

    @property
    def positions(self) -> tuple[int, ...]:
        """这份文件各块的"位置"。

        ★ 在旧方案里"位置"是分配来的小整数；这里**位置就是素数本身** ——
          它天然唯一、确定性、不需要分配器，也不需要账本。
          存储层拿它当槽位键，语义与以前完全一致（唯一整数）。
        """
        return tuple(self._primes)

    @property
    def blocks(self) -> tuple[int, ...]:
        """这份文件的局部块号（按序）。"""
        return self._blocks

    def get(self, i: int) -> int:
        """取该文件第 ``i`` 块的素数（``i`` 是**局部块号**）。"""
        i = int(i)
        if i < 0 or i >= self._max_sz:
            raise IndexError(f"局部下标 {i} 越界（这份文件 {self._max_sz} 块）")
        return self._primes[i]

    def get_many(self, indices: Iterable[int]) -> list[int]:
        return [self.get(i) for i in indices]

    def first(self, count: int) -> list[int]:
        count = int(count)
        if count < 0 or count > self._max_sz:
            raise IndexError(f"要 {count} 个素数，但这份文件只有 {self._max_sz} 块")
        return self._primes[:count]

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return (
            f"IdentityPrimeGen({self._owner}/{self._file_key}, "
            f"blocks={self._max_sz}, prime_bytes={self._prime_bytes})"
        )

    # -- 缓存管理（给测试与运维用） -----------------------------------------

    @classmethod
    def cache_size(cls) -> int:
        """当前已派生并缓存的素数个数。"""
        return len(cls._CACHE)

    @classmethod
    def clear_cache(cls) -> None:
        """清空派生缓存。

        ★ 只在测试里用。生产环境清空它不会算错（素数会被重新派生，
          结果逐位相同），只是下一次操作要重新付 5.38ms/块。
        """
        cls._CACHE.clear()
        cls._SEEN.clear()

    @classmethod
    def prime_for(cls, owner: str, file_key: str, block_idx: int, *, prime_bytes: int = 16) -> int:
        """单个块的素数 —— 不需要构造整份文件的实例时用这个。

        块密钥封装 / 节点寻址这类"只关心一个块"的地方走这里，
        避免为了一个块把整份文件的素数都派生出来。
        """
        key = identity_key(owner, file_key, block_idx)
        got = cls._CACHE.get(key)
        if got is None:
            got = hash_prime(key, prime_bytes)
            cls._CACHE[key] = got
        return got


def positions_of(owner: str, file_key: str, block_indices: Sequence[int], *, prime_bytes: int = 16) -> tuple[int, ...]:
    """便捷函数：一份文件的全部"位置"（= 各块的素数）。"""
    return IdentityPrimeGen(owner, file_key, block_indices, prime_bytes=prime_bytes).positions
