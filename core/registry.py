"""位置段登记表（一文件一条向量）。

★ **新方案用 :class:`SegmentRegistry`**：每份文件占一段或几段互不重叠的
全局位置，段**永不回收**。

为什么位置段不能回收
--------------------
回收会让同一个位置先后对应两个不同的素数，而"两个不同位置对应两个不同
素数"是整个方案的前提（论文：*even just a bijective mapping (which is
inherently collision resistant) would be enough*）。

★ **唯一的例外是 :meth:`SegmentRegistry.drop_tail`**：它只退**末尾连续**
的一段（永不制造中间空洞），而且退掉之后那些位置上的内容已经从承诺里
消失了 —— 所以它不与上面这条前提冲突。

为什么位置**允许稀疏**
----------------------
段是"每文件一段"的，删一份文件就在全局轴上留下空洞。
所以"全局位置号"必须当作普通的整数坐标看，**不能**当作"第几块"。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["BlockRef", "SegmentRegistry"]



@dataclass(frozen=True)
class BlockRef:
    """一个块的逻辑身份（所有者 + 文件名 + 块序号）。"""

    owner: str
    file_key: str
    block_idx: int

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return f"{self.owner}/{self.file_key}#{self.block_idx}"


# ===========================================================================
# ★ 新方案：位置段登记表（一文件一条向量）
# ===========================================================================

class SegmentRegistry:
    r"""文件 → 位置段的登记表（每份文件一条独立向量）。

    与"全系统一条向量"那套设计的根本差别
    ------------------------------------
    那套设计要求“下标 ``0..n-1`` 全系统密集且唯一” —— 它是"所有文件压进
    同一条向量"的必然结果。新方案里每份文件是**自己一条向量**，于是：

    * 一份文件占**一段或几段**连续位置 ``(起点, 长度)``，段之间不重叠；
    * **段永不回收**：删文件/删除只是把段“缩掉”或“摘掉”，位置不会分给
      别人 —— 同一个位置一旦换主人，就可能出现“两份承诺同时引用一个素数”，
      而那正是整个方案的前提被破坏时的样子；
    * 位置**允许稀疏**：段是“每文件一段”的，全局看本来必然有空洞。

    于是本类里**没有** ``tail_plan`` / ``free_tail`` 那一套“退回全局末尾”的
    机制 —— 那些机制存在的前提（下标必须密集）在新方案里已经不存在了。

    ★ 为什么一份文件会有**几段**：追加时，它原段的末尾可能已经被后来的文件
    占住了（段永不回收 ⇒ 不能侵占），新块只能另起一段。数学上无影响：
    :math:`E = \prod_{j \in P} e_j`，位置集合 :math:`P` 是一段还是几段
    都一样。

    ★ ``offset`` 就是“一份向量”的天然标识：唯一（段不重叠）、稳定
    （永不回收）、可反查回 ``(owner, file_key)``。
    """

    __slots__ = ("_segs", "_next")

    def __init__(self, *, next_offset: int = 0) -> None:
        #: ``(owner, file_key) -> [(起点, 长度), ...]``（按分配顺序）
        self._segs: dict[tuple[str, str], list[tuple[int, int]]] = {}
        #: 下一个可分配的全局位置号。**单调不减**（段永不回收）。
        self._next: int = int(next_offset)

    # -- 查询 ---------------------------------------------------------------

    @property
    def next_offset(self) -> int:
        """下一个可分配的全局位置号（单调不减）。"""
        return self._next

    def has(self, owner: str, file_key: str) -> bool:
        return (owner, file_key) in self._segs

    def segments_of(
        self, owner: str, file_key: str
    ) -> tuple[tuple[int, int], ...]:
        """该文件的位置段列表（按分配顺序）。"""
        try:
            return tuple(self._segs[(owner, file_key)])
        except KeyError:
            raise KeyError(f"{owner}/{file_key} 没有登记过") from None

    def positions_of(self, owner: str, file_key: str) -> tuple[int, ...]:
        """该文件占用的全部全局位置号，按**逻辑块号**顺序展开。"""
        out: list[int] = []
        for off, cnt in self.segments_of(owner, file_key):
            out.extend(range(off, off + cnt))
        return tuple(out)

    def block_count(self, owner: str, file_key: str) -> int:
        return sum(cnt for _, cnt in self.segments_of(owner, file_key))

    def key_of_position(self, position: int) -> tuple[str, str] | None:
        """全局位置号 → 它属于哪份文件。找不到返回 ``None``。

        线性扫描：段是按**文件**组织的，位置号本身并不排序，所以没有
        便宜的反查结构。演示规模（几十份文件）下够用；真要更快可以再
        维护一棵按位置排序的区间树 —— 那只是工程优化，不影响正确性。
        """
        pos = int(position)
        for key, segs in self._segs.items():
            for off, cnt in segs:
                if off <= pos < off + cnt:
                    return key
        return None

    def keys(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._segs)

    def owner_files(self, owner: str) -> tuple[str, ...]:
        return tuple(fk for (o, fk) in self._segs if o == owner)

    def total_blocks(self) -> int:
        """当前所有文件的块数之和。

        它**不等于** :attr:`next_offset` —— 后者把已废弃的空洞也算在内
        （段永不回收）。
        """
        return sum(cnt for segs in self._segs.values() for _, cnt in segs)

    # -- 分配与变更 ---------------------------------------------------------

    def _take(self, count: int, n_max: int | None, *, what: str) -> int:
        count = int(count)
        if count <= 0:
            raise ValueError(f"{what}：块数必须为正，收到 {count}")
        if n_max is not None and self._next + count > int(n_max):
            raise ValueError(
                f"{what}：要占位置 {self._next}..{self._next + count - 1}，"
                f"超出全系统位置预算 {n_max}。段在 Bootstrap 阶段定死、"
                f"且永不回收（回收会让同一位置对应上两个素数），"
                f"用满只能重建 CRS"
            )
        off = self._next
        self._next = off + count
        return off

    def alloc(
        self, owner: str, file_key: str, count: int, *, n_max=None
    ) -> tuple[int, int]:
        """给一份**新文件**分配一段位置。返回 ``(起点, 长度)``。"""
        if not owner:
            raise ValueError("owner 不能为空")
        if not file_key:
            raise ValueError("file_key 不能为空")
        if (owner, file_key) in self._segs:
            raise ValueError(f"{owner}/{file_key} 已经登记过（要加块请走 extend）")
        off = self._take(count, n_max, what=f"{owner}/{file_key} 首次分配")
        self._segs[(owner, file_key)] = [(off, int(count))]
        return off, int(count)

    def extend(
        self, owner: str, file_key: str, count: int, *, n_max=None
    ) -> tuple[int, int]:
        """给一份**已有文件**追加 ``count`` 块。返回新块占的 ``(起点, 长度)``。

        能接在最后一段后面就接着长（结果仍是一段）；接不上就另起一段
        （见类文档里“为什么一份文件会有几段”）。
        """
        segs = self._segs.get((owner, file_key))
        if segs is None:
            raise KeyError(f"{owner}/{file_key} 没有登记过（新文件请走 alloc）")
        off = self._take(count, n_max, what=f"{owner}/{file_key} 追加")
        last_off, last_cnt = segs[-1]
        if last_off + last_cnt == off:
            segs[-1] = (last_off, last_cnt + int(count))
        else:
            segs.append((off, int(count)))
        return off, int(count)

    def drop_tail(self, owner: str, file_key: str, count: int) -> tuple[int, ...]:
        """把一份文件的**末尾** ``count`` 块退掉，返回退掉那些位置号（**升序**）。

        位置**不回收**（本类的核心约定）：退掉之后它们只是变成空洞，
        不会再分给任何文件。
        """
        segs = self._segs.get((owner, file_key))
        if segs is None:
            raise KeyError(f"{owner}/{file_key} 没有登记过")
        count = int(count)
        if count < 0:
            raise ValueError("要退的块数不能为负")
        total = sum(cnt for _, cnt in segs)
        if count == 0:
            return ()
        if count > total:
            raise ValueError(
                f"要退 {count} 块，但 {owner}/{file_key} 一共只有 {total} 块"
            )
        dropped: list[int] = []
        left = count
        while left:
            off, cnt = segs[-1]
            take = min(left, cnt)
            dropped.extend(range(off + cnt - take, off + cnt))
            if take == cnt:
                segs.pop()
            else:
                segs[-1] = (off, cnt - take)
            left -= take
        if not segs:
            del self._segs[(owner, file_key)]
        # 按位置号**升序**返回，与 positions_of 的顺序一致。
        # 不能写 reverse()：每段内部本来就是按升序取的，只有「跨多段」时
        # 段与段之间的先后才是倒着的 —— 一律 sort() 最简单、也最不会错。
        dropped.sort()
        return tuple(dropped)

    def restore_position(self, owner: str, file_key: str, pos: int) -> None:
        """把某个**全局位置号**接回某份文件名下 —— **只用于从库里重建**。

        重建时手里只有一级一級的 ``global_index``（按升序），而登记表的
        正常入口 :meth:`alloc` / :meth:`extend` 只能“往后长”（后者还会
        检查 ``n_max`` 预算）。这里直接把位置接回去：挨着上一段末尾就接着长，
        否则另起一段 —— 与当初分配时得到的形状逐个字节一致。

        调用方必须**按位置升序**、逐份文件地喂（:meth:`~backend.manager.StoreManager._reload`
        就是这么做的），否则段会被切得很碎（虽然数学上仍然正确）。
        """
        p = int(pos)
        segs = self._segs.setdefault((owner, file_key), [])
        if segs and segs[-1][0] + segs[-1][1] == p:
            off, cnt = segs[-1]
            segs[-1] = (off, cnt + 1)
        else:
            segs.append((p, 1))
        if p >= self._next:
            self._next = p + 1

    def forget(self, owner: str, file_key: str) -> tuple[int, ...]:
        """整份文件从登记表里摘掉，返回它占过的位置号（**不回收**）。"""
        try:
            segs = self._segs.pop((owner, file_key))
        except KeyError:
            raise KeyError(f"{owner}/{file_key} 没有登记过") from None
        out: list[int] = []
        for off, cnt in segs:
            out.extend(range(off, off + cnt))
        return tuple(out)

    def rename_owner(self, owner: str, new_owner: str) -> int:
        """把 ``owner`` 名下全部文件的段改挂到 ``new_owner``（测试 / 旧数据迁移用）。

        只改“这块是谁的”：位置、摘要、承诺、份额一律不动，所以这些块
        照样能被任何人验证。

        :returns: 被改挂的文件数。
        """
        if not new_owner:
            raise ValueError("新 owner 不能为空")
        if new_owner == owner:
            raise ValueError("新旧 owner 相同，无需改名")
        moved = 0
        for key in list(self._segs):
            o, fk = key
            if o != owner:
                continue
            if (new_owner, fk) in self._segs:
                raise ValueError(f"改名后会与已有的 {new_owner}/{fk} 撞名")
            self._segs[(new_owner, fk)] = self._segs.pop(key)
            moved += 1
        return moved

    # -- 持久化与自检 -------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "next_offset": self._next,
            "segs": [
                {
                    "owner": o,
                    "file_key": fk,
                    "chunks": [[int(a), int(b)] for a, b in segs],
                }
                for (o, fk), segs in self._segs.items()
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SegmentRegistry":
        reg = cls(next_offset=int(d.get("next_offset", 0)))
        for rec in d.get("segs", ()):
            reg._segs[(rec["owner"], rec["file_key"])] = [
                (int(a), int(b)) for a, b in rec["chunks"]
            ]
        return reg

    def check(self) -> list[str]:
        """自检：段长均为正、段互不重叠、``next_offset`` 不低于任何段末尾。"""
        problems: list[str] = []
        spent: list[tuple[int, int, str]] = []
        for (o, fk), segs in self._segs.items():
            if not segs:
                problems.append(f"{o}/{fk} 的段列表为空（应当整条摘掉）")
                continue
            for off, cnt in segs:
                if cnt <= 0:
                    problems.append(f"{o}/{fk} 有一段长度 {cnt}（应当为正）")
                if off < 0:
                    problems.append(f"{o}/{fk} 有一段起点 {off}（应当非负）")
                spent.append((off, off + cnt, f"{o}/{fk}"))
        spent.sort()
        for i in range(1, len(spent)):
            if spent[i][0] < spent[i - 1][1]:
                problems.append(
                    f"位置段重叠：{spent[i - 1][2]} 占到 {spent[i - 1][1] - 1}，"
                    f"而 {spent[i][2]} 从 {spent[i][0]} 开始"
                )
        if spent:
            used_max = max(e for _, e, _ in spent)
            if self._next < used_max:
                problems.append(
                    f"next_offset = {self._next} 小于已用到的下一个位置 {used_max}"
                )
        return problems

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return (
            f"SegmentRegistry({len(self._segs)} 份文件, "
            f"{self.total_blocks()} 块在用, next_offset={self._next})"
        )



