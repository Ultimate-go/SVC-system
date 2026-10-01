"""全局块索引登记表 —— 设计 B 的关键部件。

为什么必须有它
--------------
设计 B 把**所有用户的所有文件**放进同一条向量，于是素数表索引必须是
**全系统唯一**的。而 `(用户, 文件, 块序号)` 三元组天然唯一，却没法直接
映射到素数表 —— :meth:`svc.primegen.PrimeGen.get` 只接受
``[0, n_max)`` 内的**密集整数下标**：

.. code-block:: python

    def get(self, i: int) -> int:
        if i < 0 or i >= self._max_sz:
            raise IndexError(f"下标 {i} 越界（容量 {self._max_sz}）")

所以「把三个字段绑成一个哈希，然后映射」这一步（规划里讨论过的做法）
**不能**写成 ``hash % n_max``：

* ``n_max = 8192`` 时，按生日问题，**约 90 个块就有约 50% 的碰撞概率**；
* 而碰撞的后果**不是报错** —— ``shamir_trick`` 在 ``gcd(x, y) ≠ 1`` 时
  **静默返回错误结果**（见 ``svc/mathbase.py``）。表现出来是
  「聚合结果偶尔不对」，几乎不可能靠读代码定位。

正确做法：**哈希当哈希表的键，不当索引**。本类维护

    bind_key(owner, file_key, block_idx)  ──哈希──▶  密集下标 0, 1, 2, ...

密集下标由 :attr:`cursor` 单调分配，**永不回收** —— 回收会让素数复用，
而"两个不同位置对应两个不同素数"是整个方案的前提
（论文：*even just a bijective mapping (which is inherently collision
resistant) would be enough*）。

★ **唯一的例外是 :meth:`free_tail`（截断用）**：它只退**末尾连续**的一段
（永不制造中间空洞），而且退掉之后那些位置上的内容已经从承诺里消失了
（见该方法的注释）—— 所以它不与上面这条前提冲突。

与向量的同步不变式
------------------
:attr:`cursor` 必须**恒等于**全局向量的当前长度 ``n``。追加是推进两者的操作，
:meth:`free_tail` 是**同时回退**两者的操作，:class:`~core.store.VectorStore`
在每次追加/截断前后都断言这一点。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

__all__ = ["BlockRef", "BlockRegistry"]


#: 字段分隔符：用 ``\x1f``（ASCII Unit Separator）避免
#: ``"a" + "bc"`` 与 ``"ab" + "c"`` 撞成同一个字符串。
_SEP = "\x1f"


@dataclass(frozen=True)
class BlockRef:
    """一个块的逻辑身份。全局下标由 :class:`BlockRegistry` 分配。"""

    owner: str
    file_key: str
    block_idx: int

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return f"{self.owner}/{self.file_key}#{self.block_idx}"


class BlockRegistry:
    """把 ``(用户, 文件, 块序号)`` 登记成全局密集下标。

    三个方向都要能查：

    * :meth:`index_of`     逻辑身份 → 全局下标（检索/验证时用）
    * :meth:`describe`     全局下标 → 逻辑身份（展示"这块是谁的"时用）
    * :meth:`indices_of`   一个文件占用的全部全局下标（跨文件查询时用）
    """

    __slots__ = ("_by_key", "_by_index", "_by_file", "_cursor")

    def __init__(self) -> None:
        self._by_key: dict[str, int] = {}
        self._by_index: dict[int, BlockRef] = {}
        self._by_file: dict[tuple[str, str], list[int]] = {}
        self._cursor: int = 0

    # -- 哈希绑定 -----------------------------------------------------------

    @staticmethod
    def bind_key(owner: str, file_key: str, block_idx: int) -> str:
        """把三元组绑成一个哈希键（就是"做哈希再映射"里的那一步哈希）。

        不取模、不截断 —— 它是一个**字典的键**，不做索引用。
        """
        if not owner:
            raise ValueError("owner 不能为空")
        if not file_key:
            raise ValueError("file_key 不能为空")
        if block_idx < 0:
            raise ValueError("block_idx 不能为负")
        raw = f"{owner}{_SEP}{file_key}{_SEP}{block_idx}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    # -- 分配与登记 ---------------------------------------------------------

    @property
    def cursor(self) -> int:
        """下一个可用的全局下标；必须恒等于向量当前长度 ``n``。"""
        return self._cursor

    def alloc_block(self, owner: str, file_key: str, block_idx: int) -> int:
        """登记一个块并返回它的全局下标。

        :raises ValueError: 该块已登记过（重复上传同一块必须走显式的新文件键）
        """
        key = self.bind_key(owner, file_key, block_idx)
        if key in self._by_key:
            raise ValueError(
                f"块 {BlockRef(owner, file_key, block_idx)} 已经登记过"
                f"（全局下标 {self._by_key[key]}）"
            )
        idx = self._cursor
        if idx in self._by_index:
            raise ValueError(f"全局下标 {idx} 已被占用，登记表与向量失去同步")

        self._by_key[key] = idx
        self._by_index[idx] = BlockRef(owner, file_key, block_idx)
        self._by_file.setdefault((owner, file_key), []).append(idx)
        self._cursor = idx + 1
        return idx

    def alloc_file(
        self, owner: str, file_key: str, count: int
    ) -> tuple[int, ...]:
        """给一个文件的 ``count`` 个块连续分配下标，返回下标元组。

        连续分配让一个文件在向量上占一段**区间** —— 后面做"访问某人的
        多个文件"时，区间比散点好处理得多。
        """
        if count <= 0:
            raise ValueError("count 必须为正")
        return tuple(
            self.alloc_block(owner, file_key, i) for i in range(count)
        )

    def tail_plan(
        self, count: int, *, allow_multi: bool = False
    ) -> tuple[tuple[int, ...], tuple[tuple[str, str], ...]]:
        r"""**只校验、不改状态**：退回末尾 ``count`` 个下标可行吗？

        :returns: ``(要退回的下标, 涉及的整份文件键)``

        为什么要把"校验"从"动手"里拆出来：删尾巴这件事在 :class:`~core.store.VectorStore`
        里**先跑一遍协议、再收账**（``_settle``）。如果"账退不回去"这件事到收账那一步
        才发现，向量已经短了、节点已经交回份额了 —— 那就成了一次**说不清账的删除**。
        所以调用方必须在协议开动**之前**先把这件事问清楚（见
        :meth:`~core.store.VectorStore.delete_from`）。

        :param allow_multi: 允不允许一次退掉**多份文件**的尾巴。

            * ``False``（默认，截断用）：只能作用于**一份**文件 ——
              退跨文件的尾巴会让"谁的文件少了块"说不清；
            * ``True``（删文件用）：允许跨文件，但每份被牵进来的文件都必须
              **整份**退出去。删文件恰好满足这一点：它删的是"从这份文件的第一块
              到向量末尾"，而各文件在下标上占的是连续区间、顺序就是上传顺序，
              所以区间里**不可能**出现"某份文件只被退掉一半"。
        """
        count = int(count)
        if count < 0:
            raise ValueError(f"要退回的下标个数不能为负，收到 {count}")
        if count == 0:
            return (), ()
        if count > self._cursor:
            raise ValueError(
                f"要退回 {count} 个下标，但一共只登记过 {self._cursor} 个"
            )
        tail = tuple(range(self._cursor - count, self._cursor))
        refs = []
        for idx in tail:
            ref = self._by_index.get(idx)
            if ref is None:
                raise ValueError(f"全局下标 {idx} 没有登记过，无法退回")
            refs.append(ref)

        keys: list[tuple[str, str]] = []
        for r in refs:
            k = (r.owner, r.file_key)
            if not keys or keys[-1] != k:
                if k in keys:
                    raise ValueError(
                        f"{k[0]}/{k[1]} 的块没有连成一段（它的下标是 "
                        f"{self._by_file[k]}）—— 要退的末尾必须按文件连着"
                    )
                keys.append(k)
        if len(keys) > 1 and not allow_multi:
            # 退跨文件的尾巴会让"谁的文件少了块"说不清；截断本来就只该作用于
            # 一份文件（调用方已经把过关），这里再兜一道
            raise ValueError(
                f"要退回的末尾 {count} 个下标跨了 {len(keys)} 份文件："
                f"{sorted(keys)} —— 截断只能作用于一份文件的末尾"
            )

        tset = set(tail)
        for k in keys:
            listed = self._by_file[k]
            inside = [i for i in listed if i in tset]
            if not inside:
                raise ValueError(
                    f"{k[0]}/{k[1]} 的块一块都不在要退的末尾区间 {list(tail)} 里"
                    f"（它登记的是 {listed}）"
                )
            if allow_multi:
                # 删文件：每份被牵进来的文件都必须**整份**退出去 ——
                # 只退一半会留下一份「账面上还在、内容已经没了」的文件。
                if len(inside) != len(listed):
                    raise ValueError(
                        f"要退回的下标只有一部分属于 {k[0]}/{k[1]}"
                        f"（它登记的是 {listed}，其中落在要退区间里的是 {inside}）"
                        f"—— 退一半会留下一份「账面上还在、内容已经没了」的文件"
                    )
            else:
                # 截断：**只退这份文件的末尾几块**是合法的（正是它的用途），
                # 所以这里只要求「要退的就是它最后那几个下标」，不要求整份都退。
                if len(inside) != len(tail) or tuple(listed[-len(inside):]) != tail:
                    raise ValueError(
                        f"要退回的下标 {list(tail)} 不是 {k[0]}/{k[1]} 的末尾几块"
                        f"（它的下标是 {listed}）"
                    )
        return tail, tuple(keys)

    def _forget(self, tail: tuple[int, ...], keys: tuple[tuple[str, str], ...]) -> None:
        """把 ``tail`` 这些下标与 ``keys`` 这些文件**从登记表里彻底忘掉**。

        ★ 分两种情形（同一个 ``_forget`` 要同时服务它们，因为"退账"这一步
          在两条路上是同一件事 —— 写两份迟早会出现"一条把账收干净、另一条漏了一半"）：

        * 整份文件都在 ``tail`` 里（删文件）→ 这个文件键整个摘掉；
        * 只有该文件的**末尾几块**在 ``tail`` 里（截断）→ 它的下标列表截短，
          文件还在。
        """
        tset = set(tail)
        for idx in tail:
            ref = self._by_index.pop(idx)
            del self._by_key[self.bind_key(ref.owner, ref.file_key, ref.block_idx)]
        for k in keys:
            listed = self._by_file.get(k)
            if listed is None:
                continue
            left = [i for i in listed if i not in tset]
            if left:
                self._by_file[k] = left
            else:
                self._by_file.pop(k, None)
        self._cursor -= len(tail)

    def free_tail(self, count: int) -> tuple[int, ...]:
        """退回**末尾**的 ``count`` 个下标（截断用），返回被退回的那些下标。

        ★ 只允许退**末尾连续**的一段 —— 本类的不变式是「下标 ``0..cursor-1``
        密集且一一对应」，退中间会留空洞，那是**不允许**的
        （``check()`` 会立刻报"下标不密集"）。

        退回的下标会被**彻底忘掉**（哈希键、反查、按文件索引三处一起删），
        于是之后的分配可以把它们重新发出去。这一步是安全的，理由要说清：

        * 本类开头那句「下标永不回收」防的是**中间留空洞**（那会让某个位置
          对应的素数在两次使用之间“换主人”，而两者的承诺同时存在）；
        * 而截断之后，这些位置上的内容已经从承诺里**消失**了
          （新的 :math:`\\delta' = d(\\mathbf{v} \\setminus K)` 里根本没有 K），
          所以「同一个位置换一份新内容」与 :meth:`~core.store.VectorStore.modify`
          做的完全是同一件事，不违背"两个位置对应两个素数"的前提。

        :raises ValueError: ``count`` 为负、超过已分配数量，或要退的下标不是
            连续的一段末尾 / 不属于同一个文件
        """
        count = int(count)
        tail, keys = self.tail_plan(count)
        if not tail:
            return ()
        self._forget(tail, keys)
        return tail

    def free_tail_multi(self, count: int) -> tuple[int, ...]:
        """退回**末尾** ``count`` 个下标，**允许跨多份文件**（删文件用）。

        与 :meth:`free_tail` 只差一条：不作"必须属于同一份文件"的限制。
        其余要求一样 —— 必须是末尾连续的一段、每份被牵进来的文件都得
        **整份**退出（校验统一在 :meth:`tail_plan` 里，见它的 ``allow_multi``）。

        ★ 为什么值得单独一个方法、而不是给 :meth:`free_tail` 加个开关：
          ``free_tail`` 的调用者（截断）依赖"单一文件"这条不变式，
          名字里带 ``_multi`` 让"这次退的可能是好几份文件"在调用点就看得见。
        """
        count = int(count)
        tail, keys = self.tail_plan(count, allow_multi=True)
        if not tail:
            return ()
        self._forget(tail, keys)
        return tail

    # -- 改名（删号用） -----------------------------------------------------

    def rename_owner(self, owner: str, new_owner: str) -> tuple[int, ...]:
        """把 ``owner`` 名下的**全部**块改挂到 ``new_owner`` 名上。

        删用户时用（``DELETE /api/admin/users/{id}``）。它只改**逻辑身份**：

        * 全局下标一个都不动（``_by_index`` 的键仍是原来那些数字）；
        * 向量分量、承诺、节点手里的份额、摘要 :math:`\\delta` 全都原样有效 ——
          所以这些块**照样能被任何人验证**（"验证不受限"那条线不受影响）；
        * 变的只有"这块是谁的"。

        为什么要有它：本类的 ``cursor`` 不回收中间空洞，:meth:`free_tail`
        又只能退**末尾连续**的一段 —— 一个已删用户的块常常压在中间，
        物理删掉会让下标不密集，``check()`` 会立刻报错。改名则完全不动下标。

        ★ 顺带解决了「同名复用」那个坑：块密钥是用**当时那个账号的公钥**封的，
        光看用户名分不出"同一个人"还是"同名的新人"。把旧数据改挂到一个
        墓碑名（如 ``ghost（已删号#6）``）之后，新 ``ghost`` 与它不再同名，
        于是 ``backend.routers.files`` 里那个 ``can_decrypt`` 会**如实**报
        false —— 界面显示 🔒、按钮灰掉，不会再出现"列表说能解、点了 403"。

        :returns: 被改挂的全局下标（升序）。``owner`` 名下没有块时返回 ``()``。

        :raises ValueError: ``new_owner`` 为空、与 ``owner`` 同名、
            或改挂之后会与已登记的块撞键。
        """
        if not new_owner:
            raise ValueError("新 owner 不能为空")
        if new_owner == owner:
            raise ValueError(f"新旧 owner 同名（{owner!r}），没有可改的东西")

        moved_files = [k for k in self._by_file if k[0] == owner]
        # ★ 先把冲突全部检查完再动手 —— 改到一半失败会留下
        #   "一半新、一半旧"的登记表，而那是最难查的一种损坏。
        for _, file_key in moved_files:
            if (new_owner, file_key) not in self._by_file:
                continue
            raise ValueError(
                f"改名会撞键：{new_owner}/{file_key} 已经登记过了"
                f"（{owner} 名下也有同名文件）"
            )

        moved: list[int] = []
        for _, file_key in moved_files:
            idxs = self._by_file.pop((owner, file_key))
            for idx in idxs:
                ref = self._by_index[idx]
                del self._by_key[self.bind_key(ref.owner, ref.file_key, ref.block_idx)]
                self._by_key[self.bind_key(new_owner, ref.file_key, ref.block_idx)] = idx
                self._by_index[idx] = BlockRef(new_owner, ref.file_key, ref.block_idx)
            self._by_file[(new_owner, file_key)] = idxs
            moved.extend(idxs)
        return tuple(sorted(moved))

    # -- 查询 ---------------------------------------------------------------

    def index_of(self, owner: str, file_key: str, block_idx: int) -> int:
        """逻辑身份 → 全局下标。"""
        key = self.bind_key(owner, file_key, block_idx)
        try:
            return self._by_key[key]
        except KeyError:
            raise KeyError(
                f"块 {BlockRef(owner, file_key, block_idx)} 没有登记过"
            ) from None

    def describe(self, global_index: int) -> BlockRef:
        """全局下标 → 逻辑身份。"""
        try:
            return self._by_index[int(global_index)]
        except KeyError:
            raise KeyError(f"全局下标 {global_index} 没有登记过") from None

    def indices_of(self, owner: str, file_key: str) -> tuple[int, ...]:
        """一个文件占用的全部全局下标（升序）。"""
        return tuple(self._by_file.get((owner, file_key), ()))

    def blocks_of(self, global_indices) -> tuple[BlockRef, ...]:
        """把一组全局下标翻译成逻辑身份 —— 展示"这次查了谁的文件"用。"""
        return tuple(self.describe(i) for i in global_indices)

    # -- 一致性自检 ---------------------------------------------------------

    def check(self) -> None:
        """校验登记表自身的一致性。

        :raises ValueError: 出现下标缺口、重复、或三个映射对不上
        """
        expected = list(range(self._cursor))
        if sorted(self._by_index) != expected:
            raise ValueError(
                f"登记表下标不密集：cursor={self._cursor}，"
                f"实际 {len(self._by_index)} 个"
            )
        for idx, ref in self._by_index.items():
            key = self.bind_key(ref.owner, ref.file_key, ref.block_idx)
            if self._by_key.get(key) != idx:
                raise ValueError(f"全局下标 {idx} 的正反查对不上：{ref}")
        total = sum(len(v) for v in self._by_file.values())
        if total != self._cursor:
            raise ValueError(
                f"按文件索引出的块数 {total} 与 cursor {self._cursor} 不一致"
            )

    def __len__(self) -> int:
        return self._cursor

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return (
            f"BlockRegistry(已登记 {self._cursor} 块, "
            f"覆盖 {len(self._by_file)} 个文件)"
        )
