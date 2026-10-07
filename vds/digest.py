"""VDS 的摘要与本地视图。

论文 §8.2 对 VDS2 的结构约定::

    δ := ((U, C), n)        ← 摘要（digest），客户端只保存它
    st := π_I := (S_I, Λ_I) ← 存储节点的本地状态（state）

其中把 ``U`` 一起放进摘要，是 §8.2 解决「specialize 阶段无法可信生成」这个
难点的关键手法。论文原文：

    U depends on the current size of the file (though not on its content),
    meaning that normally at each addition (or deletion) to the file it
    should be updated. To solve this problem, we attach U to the VDS's
    digest (together with n for technical reasons).
"""

from __future__ import annotations

from dataclasses import dataclass

from svc.types import Opening, fingerprint

__all__ = ["Digest", "LocalView", "make_identity", "split_identity", "IDENTITY_SEP"]


#: 身份串的字段分隔符 —— 与 :data:`svc.primegen_identity._SEP` 同一个字符。
#: 用 ASCII Unit Separator 而不是 ``"-"`` 之类，是为了让 ``("a", "b-c")``
#: 与 ``("a-b", "c")`` 拼出**不同**的串（否则两份不同文件会共用素数）。
IDENTITY_SEP = "\x1f"


def make_identity(owner: str, file_key: str) -> str:
    """把 ``(owner, file_key)`` 打成一个身份串 —— :attr:`Digest.identity` 的格式。

    ★ 全系统**只有这一处**定义这个格式。别在别处手写
      ``f"{owner}\x1f{file_key}"`` —— 一旦分裂成两种写法，
      「同一个块算出两个素数」这种错会极难定位。
    """
    owner, file_key = str(owner), str(file_key)
    if not owner or not file_key:
        raise ValueError("owner / file_key 都不能为空")
    if IDENTITY_SEP in owner or IDENTITY_SEP in file_key:
        raise ValueError(f"owner / file_key 不能含分隔符 {IDENTITY_SEP!r}")
    return owner + IDENTITY_SEP + file_key


def split_identity(identity: str) -> tuple[str, str]:
    """:func:`make_identity` 的逆运算。格式不对时抛 :class:`ValueError`。"""
    got = str(identity)
    parts = got.split(IDENTITY_SEP)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(
            f"身份串格式不对（应为 'owner{IDENTITY_SEP}file_key'）：{got!r}"
        )
    return parts[0], parts[1]


@dataclass(frozen=True, eq=False)
class Digest:
    """文件摘要 :math:`\\delta = ((U, C), n)`。

    :param U: :math:`U_n = g^{e_{[n]}}`，对**全体位置**的累加器。
              注意它不是常量：文件增删位置时 ``U`` 会跟着变，
              所以必须挂在摘要上，而不是放在公开参数 ``pp`` 里。
    :param C: 承诺 :math:`C = \\prod_i S_i^{v_i}`。**单个群元素**。
    :param n: 当前文件被分成的块数。

    客户端只需要保存这**一个**摘要（两个群元素 + 一个整数），
    就能验证任意子集的检索结果 —— 这是整个 VDS 的意义所在。

    .. important::

       **相等性只看** ``(U, C, n, offset)`` —— ``chunks`` **不参与**。

       理由：``chunks`` 是“这份文件占了哪些位置段”的**本地记录**，只有
       协调者（唯一知道全局位置分配的人）才有。存储节点手里的 δ 是从
       公开参数 + 自己那份状态算出来的，它既不必要也不可能知道别的文件
       占到哪里 —— 而在节点眼里，两个 ``(U, C, n)`` 相同的 δ 就是同一个
       摘要。如果拿 ``chunks`` 去比，会得到“打印出来一模一样却判定不等”
       那种见鬼的错误（节点侧明明算对了却被当成没跟上）。

       需要比段信息时显式比 :attr:`chunks` 或 :attr:`segments`。
    """

    U: int
    C: int
    n: int
    #: ★ 新方案（一文件一向量）：本文件在**全局素数表**里的段起点。
    #:
    #: 文件的第 ``i`` 块对应素数 :math:`e_{offset+i}`，于是
    #:
    #: .. math:: U = g^{\\prod_{offset \\le j < offset+n} e_j}
    #:
    #: 是**区间积**而不是前缀积（设计 B 的 :math:`e_{[n]}` 才是前缀积）。
    #: 段由协调者单调分配、**永不回收**，所以 ``offset`` 一旦定了就不再变；
    #: 变的是 ``n``（追加/截断）。
    #:
    #: 设计 B（全系统一条向量）里恒为 0，旧数据反序列化时也按 0 处理 ——
    #: 于是"每个文件都有自己的段"这条新语义不会让旧库立刻失效。
    offset: int = 0

    #: ★ 本文件占用的**位置段列表** ``((起点, 长度), ...)``，按时间顺序。
    #:
    #: 为什么需要它：位置段**永不回收**（回收会让同一个位置对应上两个不同的
    #: 素数，而两份承诺可能同时存在）。于是「给一份老文件追加块」时，它原段的
    #: 末尾可能已经被后来的文件占住了 —— 新块只能**另起一段**。也就是说
    #: 一份文件的位置未必是一段连续区间，而是若干段的并集。
    #:
    #: 数学上丝毫不受影响：:math:`E_{\text{file}} = \prod_{j \in P} e_j`，
    #: 位置集合 :math:`P` 是一段还是几段都无所谓。
    #:
    #: ``()`` 表示「单段」，等价于 ``((offset, n),)`` —— 于是旧数据（以及
    #: 绝大多数只上传过一次的文件）不必额外记录任何东西。
    chunks: tuple[tuple[int, int], ...] = ()

    #: ★★ 本文件的**身份** —— 新方案里决定「第 ``i`` 块配哪个素数」的东西。
    #:
    #: 格式：``"<owner>\x1f<file_key>"``（见 :mod:`svc.primegen_identity`）。
    #: 空串 ``""`` 表示**老路径**：素数仍按全局位置 ``offset + i`` 取
    #: （见 :class:`~core.session.ShiftedPrimeGen`），于是所有旧库、
    #: 旧测试、旧序列化数据都能原样继续工作。
    #:
    #: 为什么放在这里：``offset`` 决定了「密文在节点上放哪个槽位」，
    #: 而 ``identity`` 决定「密码学上配哪个素数」——**这两件事以前由同一个
    #: 数兼任**。拆开之后传输层、节点服务、磁盘格式全都不用动，
    #: 而密码学上的「全局下标」被彻底去掉：素数不再来自一张有容量上限的
    #: 全局素数表，而是由块的身份直接派生（同样的块无论何时再次上传，
    #: 拿到的都是同一个素数；也没有 8192 这种每文件块数上限）。
    #:
    #: .. important:::
    #:
    #:    **不参与相等性**（:meth:`_key` 里没有它）。理由与 ``chunks`` 相同：
    #:    存储节点手里的 δ 可能是从公开参数 + 自己那份状态重建的，
    #:    重建物未必带 identity，而"两个 ``(U, C, n, offset)`` 相同的 δ
    #:    就是同一个摘要"这条判据必须保持成立，否则节点侧明明算对了
    #:    却会被当成"没跟上"。
    identity: str = ""

    @property
    def segments(self) -> tuple[tuple[int, int], ...]:
        """归一化后的位置段列表（``()`` → ``((offset, n),)``）。"""
        if self.chunks:
            return tuple(self.chunks)
        return () if self.n == 0 else ((self.offset, self.n),)

    @property
    def positions(self) -> tuple[int, ...]:
        """本文件占用的**全部全局位置号**，按逻辑块号顺序展开。

        第 ``i`` 块的全局位置就是 ``positions[i]`` —— :mod:`svc` 层看到的
        「密集下标 ``i``」（0 基）与「全局位置」之间的桥就在这一处：
        前者是算法要的，后者才是素数表的地址。
        """
        if not self.chunks:
            return tuple(range(self.offset, self.offset + self.n))
        out: list[int] = []
        for off, cnt in self.chunks:
            out.extend(range(off, off + cnt))
        return tuple(out)

    def _key(self) -> tuple[int, int, int, int]:
        """相等/哈希的依据：``(U, C, n, offset)`` —— **不含** ``chunks``。"""
        return (self.U, self.C, self.n, self.offset)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Digest):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        off = "" if self.offset == 0 else f", offset={self.offset}"
        return (
            f"Digest(n={self.n}{off}, U={fingerprint(self.U)}, "
            f"C={fingerprint(self.C)})"
        )


@dataclass
class LocalView:
    """存储节点的本地视图 ``(pp, δ, n, st, I, FI)``。

    方案正确性证明里给了一个很有用的判据：

        a local view of a storage node ``(pp, δ, n, st, I, FI)`` is valid
        if :math:`st_1^{a_I} = \\delta_1 \\wedge st_2^{b_I} = \\delta_2`

    换成方案 §5.2 的记号就是：:math:`S_I^{e_I} = U_n` 且
    :math:`\\Lambda_I^{e_I} \\cdot \\prod_{i \\in I} S_i^{F_i} = C` ——
    **正好就是 :func:`svc.verify` 的两步校验**。
    即：「某个存储节点确实老老实实存着它声称的那部分数据」
    这件事，可以用同一个 :func:`svc.verify` 直接检查。
    见 :meth:`~vds.storage_node.StorageNode.check_local_view`。
    """

    delta: Digest
    st: Opening
    I: tuple[int, ...] = ()
    FI: tuple[int, ...] = ()

    @property
    def n(self) -> int:
        return self.delta.n

    def value_of(self, i: int) -> int:
        """取下标 ``i`` 的值；不在本地视图里则抛 ``KeyError``。"""
        try:
            pos = self.I.index(i)
        except ValueError:
            raise KeyError(f"本节点不持有下标 {i}") from None
        return self.FI[pos]

    def has(self, indices) -> bool:
        """是否持有全部给定下标。"""
        held = set(self.I)
        return all(i in held for i in indices)

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return (
            f"LocalView(n={self.n}, |I|={len(self.I)}, "
            f"I={list(self.I[:8])}{'...' if len(self.I) > 8 else ''})"
        )
