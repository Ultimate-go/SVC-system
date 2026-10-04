"""``NodeState`` —— 一个存储节点的**本地状态机**。

这是"节点进程化"的关键抽象：把「节点自己该干的事」从协调者里抽出来，
使同一份逻辑能跑在两种形态下 ——

* **同进程对象**（:class:`~core.transport.LocalTransport`）：单进程演示与测试
* **独立进程 + 独立 SQLite**：真分布式（``node_service/``）

节点自己干什么
--------------
论文 §8.2 的两段式更新里，节点的职责是**自己算出新状态**，而不是等别人推一份
算好的状态过来。本类实现的就是这一点：

1. **ApplyUpdate** —— 收到 :math:`\\Delta` 与更新密钥 :math:`\\Upsilon_\\Delta`，
   先校验、再自己算出适应新 ``n`` 的 :math:`(S_I, \\Lambda_I)`；
2. **AddStorage** —— 若这次还被指派了若干新位置，把它们的证据并进来。

第 2 步用到一个很漂亮的恒等式（也是"为什么新位置不花代价"的原因）：

.. math::

    S_K = g^{E_{new}/e_K} = g^{E_{old}} = U_{old}
    \\qquad
    \\Lambda_K = C_{new} \\Big/ \\prod_{j \\in K} S_j^{v_j}

—— **新位置那一整块的证据就是旧的摘要本身**，因为"除 K 之外的其余部分"
恰好就是旧向量。所以节点不需要任何新的模幂就能拿到 :math:`\\pi_K`，
再用 :func:`svc.disagg` 从它拆出自己那一段。

节点还需要什么
--------------
* ``session``（提供 ``crs`` 与 ``crs_n_for``）—— 公开参数，**必须由协调者注入**，
  节点绝不能自己再生成一套（那会得到另一个群，验证必然失败）；
* 自己那一段的**密文**。节点是唯一存着它的地方。

.. warning::

   :meth:`apply_append` 需要 :math:`\\Delta` 里的**全部**新值，
   包括它自己不持有的那些位置。这是方案本身的要求，不是实现疏忽：
   :math:`\\Lambda_I = (\\prod_{j \\notin I} S_j^{y_j})^{1/e_I}` 的修正项
   必须用到 :math:`j \\notin I` 的值。所以"节点之间互不信任"成立，
   但"节点彼此不知道对方的**密文摘要**"不成立 —— 秘密只有**明文**。
"""

from __future__ import annotations

from typing import Mapping, Sequence

from svc import disagg
from svc.types import Opening, as_index_set

from vds.digest import Digest, LocalView
from vds.storage_node import StorageNode
from vds.updates import UpdateDelta, UpdateWitness, apply_update

from .crypto import vector_element

__all__ = ["NodeState", "NodeRejected", "span"]


class NodeRejected(Exception):
    """节点拒绝一次更新（更新密钥不合法，或更新后本地视图不再合法）。"""


def span(indices: Sequence[int]) -> str:
    """把下标集合压成紧凑区间描述，如 ``0-3, 8, 16-19``。"""
    if not indices:
        return "—"
    idx = sorted(indices)
    parts: list[str] = []
    lo = prev = idx[0]
    for x in idx[1:]:
        if x == prev + 1:
            prev = x
            continue
        parts.append(f"{lo}" if lo == prev else f"{lo}-{prev}")
        lo = prev = x
    parts.append(f"{lo}" if lo == prev else f"{lo}-{prev}")
    return ", ".join(parts)


class NodeState:
    """一个存储节点的本地状态：``(δ, st=(S_I, Λ_I), I, F_I)`` + 自己那段密文。

    :param node_id: 节点标识
    :param session: 会话（只为拿 ``crs`` 与 ``crs_n_for``）
    :param view: 初始本地视图；``None`` 表示尚未拿到任何状态
    :param blobs: ``全局下标 -> 密文段``
    """

    __slots__ = ("node_id", "session", "_views", "_blobs")

    def __init__(
        self,
        node_id: str,
        session,
        view: LocalView | None = None,
        blobs: Mapping[int, bytes] | None = None,
    ) -> None:
        self.node_id = node_id
        self.session = session
        #: ★ 新方案：**每一份参与的文件各一份视图**。
        #:
        #: key 是该文件的 ``offset``（= 它在全局素数表里第一段的起点）——
        #: 它唯一（段互不重叠）、稳定（段永不回收），所以天然可以当
        #: “这是哪一份向量”的标识，不必在这里再存 owner/file_key。
        #:
        #: ★ 视图里的下标（``I`` / ``FI`` 以及密文的键）都是**文件内局部块号**
        #: （``svc`` 层假定下标密集 ``0..n-1``）——“第 i 块对素数表第 offset+i 个”
        #: 这层关系由素数视图承担。
        self._views: dict[int, LocalView] = {}
        #: ``offset -> {局部块号: 密文段}``
        self._blobs: dict[int, dict[int, bytes]] = {}
        if view is not None:
            self._views[view.delta.offset] = view
            if blobs:
                self._blobs[view.delta.offset] = {
                    int(k): bytes(v) for k, v in blobs.items()
                }

    # -------------------------------------------------------------------
    # 只读视图
    # -------------------------------------------------------------------

    @property
    def offsets(self) -> tuple[int, ...]:
        """本节点当前参与的全部向量（每份文件一个），按 offset 升序。"""
        return tuple(sorted(self._views))

    def _view_of(self, offset: int | None) -> LocalView:
        """取某一份向量的本地视图。

        ★ ``offset=None`` 表示“不指定” —— 只在**恰好只有一份视图**时可用；
        有多份就必须显式说出是哪一个。这是刻意的：迁移期任何“忘了指定是
        哪份文件”的调用点会**立刻报错**，而不是悄悄操作错的那一份
        —— 后者不会崩，只会让验证结果莫名其妙地对不上。
        """
        if offset is None:
            if len(self._views) == 1:
                return next(iter(self._views.values()))
            raise NodeRejected(
                f"{self.node_id} 现在持有 {len(self._views)} 份视图"
                f"（offset = {sorted(self._views)}），操作时必须指明是哪一份"
            )
        try:
            return self._views[int(offset)]
        except KeyError:
            raise NodeRejected(
                f"{self.node_id} 没有 offset={offset} 的那份视图"
                f"（现持有 {sorted(self._views)}）"
            ) from None

    def _blobs_of(self, offset: int | None) -> dict[int, bytes]:
        if offset is None:
            if len(self._views) != 1:
                raise NodeRejected(
                    f"{self.node_id} 持有 {len(self._views)} 份视图，"
                    f"必须指明是哪一份（offset = {sorted(self._views)}）"
                )
            offset = next(iter(self._views))
        return self._blobs.setdefault(int(offset), {})

    @property
    def has_state(self) -> bool:
        """是否已经拿到过状态（任意一份即可）。

        "空向量 + 空持有集"就是初始状态，所以 ``n == 0`` 与 ``I == ()``
        同时成立就是"还没有状态" —— 两者必须同时，单看一个会误判。
        """
        return any(
            not (v.delta.n == 0 and not v.I) for v in self._views.values()
        )

    def has_view(self, offset: int) -> bool:
        return int(offset) in self._views

    @property
    def delta(self) -> Digest:
        return self._view_of(None).delta

    def delta_of(self, offset: int) -> Digest:
        return self._view_of(offset).delta

    @property
    def I(self) -> tuple[int, ...]:
        return self._view_of(None).I if self._views else ()

    def I_of(self, offset: int) -> tuple[int, ...]:
        return self._view_of(offset).I

    @property
    def FI(self) -> tuple[int, ...]:
        return self._view_of(None).FI if self._views else ()

    def FI_of(self, offset: int) -> tuple[int, ...]:
        return self._view_of(offset).FI

    @property
    def st(self) -> Opening:
        return self._view_of(None).st

    def st_of(self, offset: int) -> Opening:
        return self._view_of(offset).st

    @property
    def blobs(self) -> dict[int, bytes]:
        return self._blobs_of(None)

    def blobs_of(self, offset: int) -> dict[int, bytes]:
        return self._blobs_of(offset)

    def node(self, offset: int | None = None) -> StorageNode:
        """包成 :class:`~vds.storage_node.StorageNode` 以复用 ``vds`` 层的算法。"""
        return StorageNode(self.node_id, self.session, self._view_of(offset))

    # -------------------------------------------------------------------
    # 初始化 / 自检
    # -------------------------------------------------------------------

    def restore(
        self,
        offset: int,
        delta: Digest,
        st: Opening,
        I: Sequence[int],
        FI: Sequence[int],
        blobs: Mapping[int, bytes] | None = None,
    ) -> None:
        """**仅供持久化恢复**：直接把一份视图与密文塞回去。

        刻意不做任何校验 —— 它的调用方是从自家库里读出来的东西
        （不是外部输入），而校验该在节点正常干活的时候做
        （:meth:`check` / :meth:`adapt` / :meth:`absorb`）。
        """
        off = int(offset)
        self._views[off] = LocalView(
            delta=delta, st=st, I=tuple(I), FI=tuple(FI)
        )
        self._blobs[off] = {int(k): bytes(v) for k, v in (blobs or {}).items()}

    def adopt_empty(self, delta: Digest) -> None:
        """采用一个空视图（第一次被指派任务时的起点）。

        空视图的数学含义是 :math:`\\pi_\\varnothing = (U_n, C_n)`：
        :math:`S_\\varnothing = g^{e_{[n]}/e_\\varnothing} = g^{e_{[n]}} = U_n`，
        而 :math:`\\Lambda_\\varnothing = C_n`（空乘积不是 1，这点容易记错）。
        它满足 ``check_local_view()``，所以后面能直接用 ``add_storage`` 长出来。
        """
        self._views[delta.offset] = LocalView(
            delta=delta, st=Opening(delta.U, delta.C, ()), I=(), FI=()
        )

    def check(self, offset: int | None = None) -> bool:
        """本地视图合法 **且** 声称持有的下标与实际存着的密文一致。

        ``offset=None``（默认）检查**本节点全部的**视图。
        """
        offs = self.offsets if offset is None else (int(offset),)
        for off in offs:
            view = self._views.get(off)
            if view is None:
                return False
            if view.delta.n == 0 and not view.I:
                continue  # 规范空视图：合法，没什么可验
            if not self.node(off).check_local_view():
                return False
            if set(view.I) != set(self._blobs.get(off, {})):
                return False
        return True

    def describe(self, verify: bool = False, offset: int | None = None) -> dict:
        """给协调者看的一行现状。

        :param verify: 是否顺带做一遍**密码学**验证（``check_local_view``，
            即 :func:`svc.verify` 的两步校验）。默认**不做** —— 它的代价与
            ``|I|`` 线性，而检索路径根本不需要它（聚合后的证据自己会被验），
            放在那里只会把每次查询拖慢好几倍。

        两个字段分工明确：

        * ``valid`` —— **便宜**的一致性：声称持有的下标 == 实际存着的密文
        * ``proved`` —— **贵**的那一项：本地视图能通过 :func:`svc.verify`；
          ``verify=False`` 时为 ``None``（"没算过"，不是"不合法"）
        """
        if offset is not None:
            return self._describe_one(int(offset), verify)
        rows = [self._describe_one(off, verify) for off in self.offsets]
        if not rows:
            return {
                "node_id": self.node_id,
                "vectors": [],
                "offsets": [],
                "held": 0,
                "valid": True,
                "proved": None,
                "fresh": True,
            }
        proved = None
        if verify and all(r["valid"] for r in rows):
            proved = all(r["proved"] for r in rows)
        return {
            "node_id": self.node_id,
            "vectors": rows,
            "offsets": [r["offset"] for r in rows],
            "held": sum(r["held"] for r in rows),
            "valid": all(r["valid"] for r in rows),
            "proved": proved,
            "fresh": False,
        }

    def _describe_one(self, offset: int, verify: bool) -> dict:
        """一份向量的现状（形状与 ``describe`` 单份时一致）。"""
        view = self._views.get(offset)
        if view is None:
            return {
                "node_id": self.node_id,
                "offset": offset,
                "n": 0,
                "held": 0,
                "indices": [],
                "span": "—",
                "valid": False,
                "proved": None,
                "fresh": True,
            }
        mine = view.I
        consistent = set(mine) == set(self._blobs.get(offset, {}))
        proved = None
        # ★ 空视图（n=0，即“这台机器上这份文件一点数据都没有”：可能只是
        #   被通知了摘要变化，或被 del 掏空过）没有 crs_n 可算，去验证只会炸。
        #   空视图本来也没什么可验的 —— :meth:`check` 里也是同样跳过。
        if verify and consistent and view.delta.n > 0:
            proved = self.node(offset).check_local_view()
        return {
            "node_id": self.node_id,
            "offset": offset,
            "n": view.delta.n,
            "held": len(mine),
            "indices": list(mine),
            "span": span(mine),
            "valid": consistent,
            "proved": proved,
            "fresh": bool(view.delta.n == 0 and not mine),
        }

    # -------------------------------------------------------------------
    # 检索
    # -------------------------------------------------------------------

    def retrieve(
        self, offset: int, Q: Sequence[int]
    ) -> tuple[tuple[int, ...], Opening]:
        """返回 ``(F_Q, π_Q)``。要求 ``Q ⊆ I``。

        ★ ``offset`` 必填：``Q`` 是**文件内局部块号**，不同文件的同名块号
        指的是完全不同的位置，所以“问哪一份”必须说清楚。
        """
        return self.node(offset).retrieve(Q)

    # -------------------------------------------------------------------
    # 测试钩子 —— 模拟一个不老实（或坏掉）的节点
    # -------------------------------------------------------------------

    def tamper_value(self, offset: int, index: int, delta: int = 1) -> None:
        """**仅供测试**：偷改自己存的一个分量。

        改完之后节点手里的证据与它**实际交付的密文**就自相矛盾了 ——
        验证方是自己从密文算分量的，算出来的值与这份证据对不上，
        当场就被承诺验证拒掉（``BAD_LAMBDA``）。跨进程时这类攻击根本
        做不了 —— 协调者碰不到节点内部，所以这个钩子只该在同进程测试里出现。
        """
        view = self._view_of(offset)
        pos = view.I.index(index)
        vals = list(view.FI)
        vals[pos] = (vals[pos] + delta) % (1 << self.session.l)
        self._views[int(offset)] = LocalView(
            delta=view.delta, st=view.st, I=view.I, FI=tuple(vals)
        )

    def overwrite_blob(self, offset: int, index: int, data: bytes) -> None:
        """**仅供测试**：把某个下标的密文换成别的字节。

        分量与证据都不动 —— 但验证方是**自己从这串密文算分量**的，
        算出来的值与承诺不符，照样被拒。这就是"不需要单独一层密文自检"
        的现场证明。
        """
        store = self._blobs_of(offset)
        if index not in store:
            raise NodeRejected(
                f"{self.node_id} 在 offset={offset} 那份里不持有下标 {index}"
            )
        store[index] = bytes(data)

    def replace_blobs(self, offset: int, blobs: Mapping[int, bytes]) -> None:
        """把若干**已持有**下标的密文换成新的（``mod`` 的正常路径）。

        与 :meth:`overwrite_blob` 的区别必须说清楚，否则很容易用错：

        * :meth:`overwrite_blob` 是**测试钩子** —— 故意不校验，用来模拟
          「服务器偷改自己存的数据」，看验证能不能当场拒掉（能：验证方
          从那串密文自己算出的分量对不上承诺）；
        * 本方法是**正常路径** —— 改一块之后协调者送来新密文，这里顺手用
          :func:`~core.crypto.vector_element` 校验
          :math:`\\mathrm{SM3}(\\text{新密文})` 是否等于节点**自己**从
          :math:`\\Upsilon_\\Delta` 算出来的新分量。

        为什么要校验：分量是节点自己按更新密钥算出来的，密文是协调者给的。
        不校验的话，一个不老实的协调者可以塞一段与 :math:`\\delta` 无关的
        密文进来，而 :meth:`check` 只比对**下标集合**、不比密文内容
        （那份便宜的一致性检查是刻意的，见其文档），于是要等到客户端取回、
        **自己从这段密文算出分量**时才会暴露（算出的值与承诺不符）
        —— 那时代价已经付出去了。

        ``I`` 由 ``adapt`` 负责（``mod`` 不改变持有集合），这里**只管密文**；
        两者不同步会被 :meth:`check` 抓住。

        :raises NodeRejected: 下标不在自己手里，或密文与分量对不上
        """
        if not blobs:
            return
        store = self._blobs_of(offset)
        view = self._view_of(offset)
        for idx, data in blobs.items():
            index = int(idx)
            if index not in store:
                raise NodeRejected(
                    f"{self.node_id} 在 offset={offset} 那份里不持有下标 {index}，"
                    f"无法替换它的密文"
                )
            data = bytes(data)
            want = view.FI[view.I.index(index)]
            got = vector_element(data)
            if got != want:
                raise NodeRejected(
                    f"{self.node_id} 收到的下标 {index} 的密文与分量对不上："
                    f"SM3(新密文) 与更新后算出的分量不一致"
                )
            store[index] = data

    # -------------------------------------------------------------------
    # 更新 —— 节点自己算，不靠协调者推状态
    # -------------------------------------------------------------------

    def adapt(
        self,
        offset: int,
        op_delta: UpdateDelta,
        witness: UpdateWitness,
        delta_new: "Digest | None" = None,
    ) -> None:
        """``ApplyUpdate``：校验 :math:`\\Upsilon_\\Delta` 并跟上到新的 ``n``。

        三种更新共用它（**新状态一律由节点自己按更新密钥算出来**，
        协调者只告诉它"发生了什么"）：

        * ``mod`` / ``add``：**位置集合 ``I`` 不变**，只是 :math:`e_{[n]}` 抬升了；
        * ``del``：``I`` 会**变小**（也可能被掏空 → 归一成规范空视图）。

        ``del`` 还要把被删掉的密文一起删掉 —— 那件事在 :meth:`apply_delete` 里
        （本方法只管 :math:`(\\delta, S_I, \\Lambda_I, I, F_I)` 这一份状态）。
        """
        view = self._view_of(offset)
        # ★ add 会给本地视图添上**新**块（局部号 n..n+k-1），而这些号在
        #   ``view.delta`` 的位置段里根本不存在。素数视图必须来自**新** δ，
        #   否则 ``e_K`` 会取到别的素数、ΥΔ 校验必然失败（报错还长成
        #   “S_K^(e_K) ≠ U，S_K 是伪造的”）。
        #   注意 U/C/n 仍然是**旧**值：add 的公式要用 :math:`U_{old}`。
        push_delta = view.delta
        if op_delta.op == "add" and delta_new is not None:
            push_delta = Digest(
                U=view.delta.U,
                C=view.delta.C,
                n=view.delta.n,
                offset=view.delta.offset,
                chunks=tuple(delta_new.segments),
            )
        out = apply_update(
            self.session, push_delta, self.node(offset), op_delta, witness
        )
        if not out.ok or out.node is None:
            raise NodeRejected(out.message or "ApplyUpdate 失败")
        new_view = out.node.view
        if delta_new is not None:
            # ★ 节点自己算出的 δ 只保证 **(U, C, n, offset)** 对得上；**位置段**
            #   （``chunks``）在节点这里无从得知 —— 一份文件的位置可能是好几段
            #   （段永不回收：追加时原段末尾被后来的文件占住，就只能另起一段），
            #   而“第 i 块 ↔ 素数表里第几个”全靠它。所以位置以**协调者下发的**
            #   δ 为准：校验用节点自己算的，位置用协调者给的。
            #   （不这么做的话，节点会拿着“从 offset 起连续 n 个”这个错误视图
            #    去算 e_i，之后每一次更新的 Υ∆ 校验都会莫名失败。）
            if new_view.delta != delta_new:
                raise NodeRejected(
                    f"{self.node_id} 算出的摘要与协调者不一致："
                    f"{new_view.delta!r} vs {delta_new!r}"
                )
            new_view = LocalView(
                delta=delta_new, st=new_view.st, I=new_view.I, FI=new_view.FI
            )
        self._views[int(offset)] = new_view
        if not new_view.I:
            # ★ 数据被删光了（``del`` 刚把本节点掏空）：``vds.updates._apply_del``
            #   第一分支给的是「空集合 + 旧 (S_I, Λ_I)」的**占位**视图，它不是
            #   合法本地视图（空集合的证明必须是 (U_n, C_n)，见 adopt_empty）。
            #   这里归一成规范空视图 —— 不归一的话 :meth:`check` 立刻判它不合法，
            #   节点会以 500「应用更新后本地视图不合法」拒掉这次删除。
            self.adopt_empty(out.node.view.delta)

    def drop(self, offset: int, positions: Sequence[int]) -> dict:
        """``StrgNode.RmvStorage`` 的节点侧：**把 K 那部分先交回去**。

        为什么删末尾块之前要先交回：见 ``vds.updates._apply_del`` —— 它要求每个
        节点与待删集合 ``K``「要么全包含、要么全不相交」，**部分相交直接报错**。
        而我们的分片是「轮转 + 每块 2 份副本」，所以一个跨台的末尾区间几乎必然
        让某些节点"部分相交"（它拿着 ``K`` 里的几块、又拿着别的块）。
        这些块本来就要被删掉，所以让它们先把这部分交出来是最自然的解法。

        ★ **幂等**：不在本地集合里的下标直接跳过。这样「交回了 2 台、
        第 3 台掉线」之后整个截断重来一次是安全的（已交回的那些当无事发生）。

        .. note::

           交回之后、删除之前的这一小段窗口里，协调者记的副本表与节点实际
           持有的东西**暂时对不上**（协调者还没删那些下标）。这是有意的：
           紧接着的那次 ``del`` 会把它们一起抹掉，而这一窗口里没有任何人
           会跑整体自检（写路径全程持锁）。

        :returns: ``{"ok", "dropped", "held", "span", "note"}``
        """
        view = self._views.get(int(offset))
        store = self._blobs.get(int(offset), {})
        if view is None:
            # 不参与这份向量的节点没什么可交的 —— 也算成功（幂等）
            return {
                "ok": True, "dropped": [], "held": 0, "span": "—",
                "note": "本节点不参与这份向量，无需交回",
            }
        want = set(as_index_set(int(p) for p in positions))
        mine = set(view.I)
        gone = tuple(sorted(want & mine))
        if not gone:
            return {
                "ok": True, "dropped": [], "held": len(mine), "span": span(mine),
                "note": "要交回的下标本来就不在本节点手里",
            }
        if set(gone) == mine:
            # 全交出去 → 本节点空掉。不能走 rmv_storage（它会报"什么都不剩"），
            # 归一成规范空视图即可。
            store.clear()
            self.adopt_empty(view.delta)
            return {
                "ok": True, "dropped": list(gone), "held": 0, "span": "—",
                "note": "本节点持有的下标全在被删区间里，已交空",
            }
        # ★ 只取 view，**不取 node_id**：``rmv_storage`` 返回的节点名带着它去掉的
        #   下标（``node-a[10]`` 这种），而节点名必须保持稳定（集群页、审计、
        #   协调者的副本表都按名字认机器）。
        shrunk = self.node(offset).rmv_storage(gone)
        self._views[int(offset)] = LocalView(
            delta=shrunk.delta, st=shrunk.st, I=shrunk.I, FI=shrunk.FI
        )
        for i in gone:
            store.pop(i, None)
        if set(shrunk.I) != set(store):  # pragma: no cover - 兜底不变式
            raise NodeRejected(
                f"{self.node_id} 交回后下标集合与密文对不上："
                f"{len(self.I)} 个下标 / {len(self._blobs)} 段密文"
            )
        return {
            "ok": True, "dropped": list(gone), "held": len(shrunk.I),
            "span": span(shrunk.I),
        }

    def _forget_segment_if_empty(self, offset: int, delta_new: Digest) -> bool:
        """整段被删光（``n = 0``）就把这一段的键摘掉。摘了返回 ``True``。

        ★ 为什么非摘不可：协调者与节点各自记着"每一份文件（= 位置段）现在
          有多少块"，启动时**逐份比对**。而协调者那边的账目
          （``backend.models.FileDeltaRow``）在一份文件被删掉时是**直接删行**
          的 —— 它眼里就没有这个段了；节点这边若只是把视图归一成空
          （``n=0, I=()``）而键还留在 ``self._views`` 里，就会：落库 → 重启 →
          读回来 → 自检报「node-1 停在 [... (10, 0)]，协调者是 [...]」并
          **拒绝启动整个后端**。（第一份真的被删掉的文件当场证明了这件事：
          我拿它当冒烟探针传上去又删掉，后端就再也起不来了。）

        ``n > 0`` 的段必须**留着**：那只是"这次删掉了我手里的一部分"，
        后面还要接着追加 / 改块，段必须还在。
        """
        if delta_new.n:
            return False
        self._views.pop(int(offset), None)
        self._blobs.pop(int(offset), None)
        return True

    def apply_delete(
        self,
        offset: int,
        op_delta: UpdateDelta,
        witness: UpdateWitness,
        delta_new: Digest,
    ) -> None:
        """``del``：跟上新摘要，并把被删掉的那些密文**真删掉**。

        与 ``mod`` 的两点不同：

        1. **位置集合会变小**（``_apply_del`` 会给出新的 ``I``），所以密文也要跟着删；
        2. 删完之后 ``set(I) == set(blobs)`` 这条不变式必须仍然成立 ——
           :meth:`check` 就靠它，少删一行密文当场就会暴露（节点自己拒落库）。

        三条路（都已在 ``vds`` 层测过）：

        * ``K ⊆ I``：只丢下标，``(S_I, Λ_I)`` 一个字都不改；
        * 与 ``K`` 不相交：下标不变，状态变成 ``agg(π_I, π_K)``；
        * 部分相交：**不允许** —— 协调者必须先让它 :meth:`drop`。

        :raises NodeRejected: 不是 del、还没有状态、摘要对不上、或删后视图不合法
        """
        if op_delta.op != "del":
            raise NodeRejected(f"这不是一次删除更新（op = {op_delta.op!r}）")
        view = self._views.get(int(offset))
        if view is None or not view.I:
            # 手里什么都没有的节点（从来没被指派过，或者上一轮刚被删空）：
            # 没有密文要删，也没有证据要跟 —— 只是把 δ 换成新的空视图。
            # δ 是公开的，它手里没有东西可验证，所以这里不需要任何密码学。
            # ★ 但这一步**不能省**：协调者自检会逐个比对各台的 n，落下一台就报
            #   "它停在 n=旧，没跟上更新"。
            if not self._forget_segment_if_empty(offset, delta_new):
                self.adopt_empty(delta_new)
            return
        gone = set(op_delta.K) & set(view.I)
        self.adapt(offset, op_delta, witness, delta_new)
        now = self._views[int(offset)].delta
        if now != delta_new:
            raise NodeRejected(
                f"{self.node_id} 自己算出的摘要与协调者不一致"
                f"（n={now.n} vs n={delta_new.n}）—— 更新密钥或新摘要有问题"
            )
        store = self._blobs_of(offset)
        for i in gone:
            store.pop(i, None)
        if not self.check(offset):
            raise NodeRejected(f"{self.node_id} 删除后本地视图不合法，拒绝落库")
        # ★ 万一这次把我手里的块全删了、而且这一段也就此空了 —— 那就把整个
        #   段摘掉（理由见 _forget_segment_if_empty）。n > 0 时它什么都不做：
        #   那种情况**不能** adopt_empty，否则会把刚缩小的 I 抹成空。
        self._forget_segment_if_empty(offset, delta_new)

    def absorb(
        self,
        *,
        delta_old: Digest,
        new_positions: Sequence[int],
        assigned: Sequence[int],
        values_all: Sequence[int],
        blobs: Mapping[int, bytes] | None = None,
        delta_new: "Digest | None" = None,
    ) -> None:
        """``AddStorage``：把被指派的新位置并进本地视图。

        :param delta_old: 追加**前**的摘要 —— 用来构造 :math:`\\pi_K`
        :param new_positions: 这次追加的全部新下标 ``K``
        :param assigned: 指派给本节点的那些下标（``K`` 的**子集**）
        :param values_all: 与 ``new_positions`` 一一对应的新值
        """
        off = int(delta_old.offset)
        seg = as_index_set(assigned)
        if not seg:
            return
        K = tuple(new_positions)
        if not set(seg) <= set(K):
            raise NodeRejected(
                f"指派的下标 {sorted(set(seg) - set(K))} 不在本次新增的 {list(K)} 里"
            )
        if len(values_all) != len(K):
            raise NodeRejected("新位置的个数与值的个数不一致")

        val_of = dict(zip(K, values_all))
        vals = [val_of[j] for j in seg]

        # ★ ``π_K`` 就是**旧摘要本身** —— 这不是巧合，而是 add 的定义：
        #   新版里“除 K 之外的其余部分”正是旧向量，而 ``verify`` 对一个
        #   候选证明做的事就是从 ``(S_I, Λ_I)`` 出发对 ``I`` 逐个
        #   :func:`svc.add_back`，看能不能迭代出 ``(U_n, C)``。
        #   拿 ``(U_old, C_old, K)`` 迭代 |K| 次，正好得到 ``(U_new, C_new)``。
        #
        #   ★ 别想着“用新承诺除以新成员见证”去自己拼 ``Λ_K``：那样得到的是
        #     一个**承诺**，而 ``Λ`` 要的是 :math:`e_I` 次根，隐藏阶群里
        #     开不出来（``svc.groups`` 生成完 φ(N) 就丢掉了）。
        pi_K = Opening(delta_old.U, delta_old.C, K)

        # 我可能只被指派了其中一段，于是从 π_K 拆出 π_seg
        current = self.node(off)
        if seg == K:
            pi_seg = pi_K
        else:
            crs_n = self.session.crs_n_for(current.delta)
            pi_seg = disagg(crs_n, K, list(values_all), pi_K, seg)

        merged = current.add_storage((seg, vals, pi_seg))
        merged_delta = merged.view.delta
        if delta_new is not None:
            # ★ 同 adapt：位置段以协调者下发的 δ 为准
            #   （``add_storage`` 自己拼出来的 δ 会沿用旧 ``chunks`` 而 n 已经变了，
            #    用它算 e_i 必然错位）。
            if merged_delta != delta_new:
                raise NodeRejected(
                    f"{self.node_id} 合并后算出的摘要与协调者不一致："
                    f"{merged_delta!r} vs {delta_new!r}"
                )
            merged_delta = delta_new
        self._views[off] = LocalView(
            delta=merged_delta,
            st=merged.view.st,
            I=merged.view.I,
            FI=merged.view.FI,
        )
        if blobs:
            self._blobs_of(off).update({int(k): bytes(v) for k, v in blobs.items()})

    def adopt(
        self,
        offset: int,
        positions: Sequence[int],
        values: Sequence[int],
        proof: Opening,
        blobs: Mapping[int, bytes] | None = None,
    ) -> dict:
        r"""从**一份检索凭证**接收若干位置（``StrgNode.AddStorage`` 的凭证入口）。

        与 :meth:`absorb` 的分工必须说清楚，否则很容易用错：

        * :meth:`absorb` 收的是**本次追加刚产生的新位置**。这些位置在旧文件里
          没有成员见证，所以整块 :math:`\pi_K` 恰好就是**旧的摘要本身**
          （见本模块文档里那个恒等式），一个模幂都不用做；
        * 本方法收的是**已经承诺过的位置** —— 执行 ``AddStorage`` 时
          那次追加早就提交完了，旧摘要里没有它们。所以「那一块的证据就是旧摘要」
          这条近路**走不通**，必须由**当前持有者**给出 :math:`\pi_Q = d(v \setminus Q)`
          （它手里有 :math:`I \supseteq Q`，一次 :func:`~svc.disagg` 就能拆出来）。

        这正是论文 §7 那句「任何人拿到一份合法凭证都能成为存储节点」的落地点：
        与本节点是否参与过当初那次追加**无关**。

        .. important::

           **这不是"协调者直接写节点状态"。** 凭证来自另一台**节点**的
           :meth:`~core.node_state.NodeState.retrieve`，本节点拿它把
           :func:`~svc.agg` 自己的 :math:`(S_I,\Lambda_I)` 长出新的一段 ——
           决策权仍在节点这一侧（它会先自己验一遍凭证，见 ``verify_cert``）。

        :param positions: 要接收的下标 ``Q``，必须与本节点**已有的**下标不相交
            （:func:`~svc.agg` 的前提）
        :param values: 与 ``positions`` 一一对应的分量
        :param proof: :math:`\pi_Q`，一份对**当前摘要**合法的子向量打开证明
        :param blobs: ``下标 -> 密文段``。**不给就会当场被 :meth:`check` 抓住**
            （它要求"声称持有的下标 == 实际存着的密文"，那条不变式在这里
            照样成立 —— 收了证据却没有内容，等于谎报）。
        :raises NodeRejected: 下标重叠、长度不符、没有状态、凭证不合法、合并后视图不合法

        :returns: 一行新的现状（``{"adopted", "held", "span"}`` 等）
        """
        Q = as_index_set(int(p) for p in positions)
        if not Q:
            return {"adopted": [], "held": len(self.I), "span": span(self.I)}
        vals = tuple(int(v) for v in values)
        if len(vals) != len(Q):
            raise NodeRejected(
                f"要接收的 {len(Q)} 个下标与 {len(vals)} 个值对不上"
            )
        view = self._views.get(int(offset))
        if view is None:
            # 不参与这份向量的节点连“合并进哪条视图”都没有 —— 得先从别处拿到 δ。
            # （正常流程里不会发生：任何一次追加都会给**每一台**节点
            #    adopt_empty，所以只要 n>0，全集群的节点都有状态。）
            raise NodeRejected(
                f"{self.node_id} 还没有 offset={offset} 那份向量的状态，"
                f"无法接收迁移过来的位置"
            )
        overlap = sorted(set(Q) & set(view.I))
        if overlap:
            # AddStorage 要求两份存储不相交；重叠说明协调者的副本表与
            # 本节点实际的持有集**已经对不上**了，那不是能"顺手修一下"的状态。
            raise NodeRejected(
                f"{self.node_id} 已经持有下标 {overlap}，不能再收一遍"
                f"（AddStorage 要求两份存储不相交）"
            )

        current = self.node(offset)
        try:
            # verify_cert=True：先按**本节点自己的**摘要验一遍凭证再合并。
            # 不验就等于把一个来源不明的份额塞进本地视图，
            # 而 :meth:`check` 只比对下标集合、不比内容 —— 会被悄悄毒掉。
            merged = current.add_storage((Q, vals, proof), verify_cert=True)
        except ValueError as exc:
            raise NodeRejected(f"{self.node_id} 拒绝这份凭证：{exc}") from exc

        # ★ 摘要必须**一个字节都不变**：这次操作不改变 n、不改变 C，
        #   只是把"谁持有哪些下标"换了个分布。变了就说明凭证对错了版本。
        if merged.view.delta != view.delta:
            raise NodeRejected(
                f"{self.node_id} 合并后摘要变了"
                f"（{merged.view.delta!r} vs {view.delta!r}）—— 凭证对错了版本"
            )

        self._views[int(offset)] = LocalView(
            delta=merged.view.delta,
            st=merged.view.st,
            I=merged.view.I,
            FI=merged.view.FI,
        )
        if blobs:
            self._blobs_of(offset).update({int(k): bytes(v) for k, v in blobs.items()})
        held = self.I_of(offset)
        d = self.delta_of(offset)
        return {
            "adopted": list(Q),
            "held": len(held),
            "span": span(held),
            "delta": (d.U, d.C, d.n),
        }

    # -------------------------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        if not self._views:
            return f"NodeState({self.node_id!r}, 未初始化)"
        parts = [
            f"offset={off}: n={v.delta.n}, |I|={len(v.I)}"
            for off, v in sorted(self._views.items())
        ]
        return (
            f"NodeState({self.node_id!r}, {len(self._views)} 份向量: "
            + " | ".join(parts) + ")"
        )
