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

    \\pi_K^{new} = d_{new}(v' \\setminus K) = (U_{old}, C_{old})

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

    __slots__ = ("node_id", "session", "_view", "_blobs")

    def __init__(
        self,
        node_id: str,
        session,
        view: LocalView | None = None,
        blobs: Mapping[int, bytes] | None = None,
    ) -> None:
        self.node_id = node_id
        self.session = session
        self._view = view
        self._blobs: dict[int, bytes] = dict(blobs or {})

    # -------------------------------------------------------------------
    # 只读视图
    # -------------------------------------------------------------------

    @property
    def has_state(self) -> bool:
        """是否已经拿到过状态。

        "空向量 + 空持有集"就是初始状态，所以 ``n == 0`` 与 ``I == ()``
        同时成立就是"还没有状态" —— 两者必须同时，单看一个会误判。
        """
        return self._view is not None and not (
            self._view.delta.n == 0 and not self._view.I
        )

    @property
    def delta(self) -> Digest:
        if self._view is None:
            raise NodeRejected(f"{self.node_id} 还没有任何状态")
        return self._view.delta

    @property
    def I(self) -> tuple[int, ...]:
        return self._view.I if self._view else ()

    @property
    def FI(self) -> tuple[int, ...]:
        return self._view.FI if self._view else ()

    @property
    def st(self) -> Opening:
        if self._view is None:
            raise NodeRejected(f"{self.node_id} 还没有任何状态")
        return self._view.st

    @property
    def blobs(self) -> dict[int, bytes]:
        return self._blobs

    def node(self) -> StorageNode:
        """包成 :class:`~vds.storage_node.StorageNode` 以复用 ``vds`` 层的算法。"""
        if self._view is None:
            raise NodeRejected(f"{self.node_id} 还没有任何状态")
        return StorageNode(self.node_id, self.session, self._view)

    # -------------------------------------------------------------------
    # 初始化 / 自检
    # -------------------------------------------------------------------

    def adopt_empty(self, delta: Digest) -> None:
        """采用一个空视图（第一次被指派任务时的起点）。

        空视图的数学含义是 :math:`\\pi_\\varnothing = (U_n, C_n)`：
        :math:`S_\\varnothing = g^{e_{[n]}/e_\\varnothing} = g^{e_{[n]}} = U_n`，
        而 :math:`\\Lambda_\\varnothing = C_n`（空乘积不是 1，这点容易记错）。
        它满足 ``check_local_view()``，所以后面能直接用 ``add_storage`` 长出来。
        """
        self._view = LocalView(delta=delta, st=Opening(delta.U, delta.C, ()), I=(), FI=())

    def check(self) -> bool:
        """本地视图合法 **且** 声称持有的下标与实际存着的密文一致。"""
        if not self.has_state:
            return True
        if not self.node().check_local_view():
            return False
        return set(self.I) == set(self._blobs)

    def describe(self, verify: bool = False) -> dict:
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
        if not self.has_state:
            return {
                "node_id": self.node_id,
                "n": 0,
                "held": 0,
                "indices": [],
                "span": "—",
                "valid": True,
                "proved": None,
                "fresh": True,
            }
        consistent = set(self.I) == set(self._blobs)
        proved = None
        if verify and consistent:
            proved = self.node().check_local_view()
        return {
            "node_id": self.node_id,
            "n": self._view.delta.n,
            "held": len(self.I),
            "indices": list(self.I),
            "span": span(self.I),
            "valid": consistent,
            "proved": proved,
            "fresh": False,
        }

    # -------------------------------------------------------------------
    # 检索
    # -------------------------------------------------------------------

    def retrieve(self, Q: Sequence[int]) -> tuple[tuple[int, ...], Opening]:
        """返回 ``(F_Q, π_Q)``。要求 ``Q ⊆ I``。"""
        return self.node().retrieve(Q)

    # -------------------------------------------------------------------
    # 测试钩子 —— 模拟一个不老实（或坏掉）的节点
    # -------------------------------------------------------------------

    def tamper_value(self, index: int, delta: int = 1) -> None:
        """**仅供测试**：偷改自己存的一个分量。

        这会同时破坏两层验证（承诺那层与块哈希那层），因为节点"说的"与
        "存的"对不上了。跨进程时这类攻击根本做不了 —— 协调者碰不到节点内部，
        所以这个钩子只该在同进程测试里出现。
        """
        if self._view is None:
            raise NodeRejected(f"{self.node_id} 还没有任何状态")
        pos = self._view.I.index(index)
        vals = list(self._view.FI)
        vals[pos] = (vals[pos] + delta) % (1 << self.session.l)
        self._view = LocalView(
            delta=self._view.delta,
            st=self._view.st,
            I=self._view.I,
            FI=tuple(vals),
        )

    def overwrite_blob(self, index: int, data: bytes) -> None:
        """**仅供测试**：把某个下标的密文换成别的字节。

        分量与证据都不动，所以**只有块哈希那层抓得住** —— 这正是必须有
        那一层的原因。
        """
        if index not in self._blobs:
            raise NodeRejected(f"{self.node_id} 不持有下标 {index}")
        self._blobs[index] = bytes(data)

    def replace_blobs(self, blobs: Mapping[int, bytes]) -> None:
        """把若干**已持有**下标的密文换成新的（``mod`` 的正常路径）。

        与 :meth:`overwrite_blob` 的区别必须说清楚，否则很容易用错：

        * :meth:`overwrite_blob` 是**测试钩子** —— 故意不校验，用来模拟
          「服务器偷改自己存的数据」，看块哈希那层抓不抓得住；
        * 本方法是**正常路径** —— 改一块之后协调者送来新密文，这里顺手用
          :func:`~core.crypto.vector_element` 校验
          :math:`\\mathrm{SM3}(\\text{新密文})` 是否等于节点**自己**从
          :math:`\\Upsilon_\\Delta` 算出来的新分量。

        为什么要校验：分量是节点自己按更新密钥算出来的，密文是协调者给的。
        不校验的话，一个不老实的协调者可以塞一段与 :math:`\\delta` 无关的
        密文进来，而 :meth:`check` 只比对**下标集合**、不比密文内容
        （那份便宜的一致性检查是刻意的，见其文档），于是要等到客户端取回、
        做第 1 层块哈希校验时才会暴露 —— 那时代价已经付出去了。

        ``I`` 由 ``adapt`` 负责（``mod`` 不改变持有集合），这里**只管密文**；
        两者不同步会被 :meth:`check` 抓住。

        :raises NodeRejected: 下标不在自己手里，或密文与分量对不上
        """
        if not blobs:
            return
        if not self.has_state:
            raise NodeRejected(f"{self.node_id} 还没有任何状态，改不了")
        for idx, data in blobs.items():
            index = int(idx)
            if index not in self._blobs:
                raise NodeRejected(
                    f"{self.node_id} 不持有下标 {index}，无法替换它的密文"
                )
            data = bytes(data)
            want = self.FI[self.I.index(index)]
            got = vector_element(data)
            if got != want:
                raise NodeRejected(
                    f"{self.node_id} 收到的下标 {index} 的密文与分量对不上："
                    f"SM3(新密文) 与更新后算出的分量不一致"
                )
            self._blobs[index] = data

    # -------------------------------------------------------------------
    # 更新 —— 节点自己算，不靠协调者推状态
    # -------------------------------------------------------------------

    def adapt(self, op_delta: UpdateDelta, witness: UpdateWitness) -> None:
        """``ApplyUpdate``：校验 :math:`\\Upsilon_\\Delta` 并跟上到新的 ``n``。

        三种更新共用它（**新状态一律由节点自己按更新密钥算出来**，
        协调者只告诉它"发生了什么"）：

        * ``mod`` / ``add``：**位置集合 ``I`` 不变**，只是 :math:`e_{[n]}` 抬升了；
        * ``del``：``I`` 会**变小**（也可能被掏空 → 归一成规范空视图）。

        ``del`` 还要把被删掉的密文一起删掉 —— 那件事在 :meth:`apply_delete` 里
        （本方法只管 :math:`(\\delta, S_I, \\Lambda_I, I, F_I)` 这一份状态）。
        """
        if not self.has_state:
            raise NodeRejected(f"{self.node_id} 还没有状态，无法适应更新")
        old = self._view.delta
        out = apply_update(self.session, old, self.node(), op_delta, witness)
        if not out.ok or out.node is None:
            raise NodeRejected(out.message or "ApplyUpdate 失败")
        self._view = out.node.view
        if not self._view.I:
            # ★ 数据被删光了（``del`` 刚把本节点掏空）：``vds.updates._apply_del``
            #   第一分支给的是「空集合 + 旧 (S_I, Λ_I)」的**占位**视图，它不是
            #   合法本地视图（空集合的证明必须是 (U_n, C_n)，见 adopt_empty）。
            #   这里归一成规范空视图 —— 不归一的话 :meth:`check` 立刻判它不合法，
            #   节点会以 500「应用更新后本地视图不合法」拒掉这次删除。
            self.adopt_empty(self._view.delta)

    def drop(self, positions: Sequence[int]) -> dict:
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
        if not self.has_state:
            # 没有状态的节点没什么可交的 —— 也算成功（幂等）
            return {
                "ok": True, "dropped": [], "held": 0, "span": "—",
                "note": "本节点还没有状态，无需交回",
            }
        want = set(as_index_set(int(p) for p in positions))
        mine = set(self.I)
        gone = tuple(sorted(want & mine))
        if not gone:
            return {
                "ok": True, "dropped": [], "held": len(self.I), "span": span(self.I),
                "note": "要交回的下标本来就不在本节点手里",
            }
        if set(gone) == mine:
            # 全交出去 → 本节点空掉。不能走 rmv_storage（它会报"什么都不剩"），
            # 归一成规范空视图即可。
            self._blobs.clear()
            self.adopt_empty(self.delta)
            return {
                "ok": True, "dropped": list(gone), "held": 0, "span": "—",
                "note": "本节点持有的下标全在被删区间里，已交空",
            }
        # ★ 只取 view，**不取 node_id**：``rmv_storage`` 返回的节点名带着它去掉的
        #   下标（``node-a[10]`` 这种），而节点名必须保持稳定（集群页、审计、
        #   协调者的副本表都按名字认机器）。
        shrunk = self.node().rmv_storage(gone)
        self._view = LocalView(
            delta=shrunk.delta, st=shrunk.st, I=shrunk.I, FI=shrunk.FI
        )
        for i in gone:
            self._blobs.pop(i, None)
        if set(self.I) != set(self._blobs):  # pragma: no cover - 兜底不变式
            raise NodeRejected(
                f"{self.node_id} 交回后下标集合与密文对不上："
                f"{len(self.I)} 个下标 / {len(self._blobs)} 段密文"
            )
        return {
            "ok": True, "dropped": list(gone), "held": len(self.I),
            "span": span(self.I),
        }

    def apply_delete(
        self, op_delta: UpdateDelta, witness: UpdateWitness, delta_new: Digest
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
        if not self.I:
            # 手里什么都没有的节点（从来没被指派过，或者上一轮刚被删空）：
            # 没有密文要删，也没有证据要跟 —— 只是把 δ 换成新的空视图。
            # δ 是公开的，它手里没有东西可验证，所以这里不需要任何密码学。
            # ★ 但这一步**不能省**：协调者自检会逐个比对各台的 n，落下一台就报
            #   "它停在 n=旧，没跟上更新"。
            self.adopt_empty(delta_new)
            return
        if not self.has_state:
            raise NodeRejected(f"{self.node_id} 还没有任何状态，无法应用删除")
        # 必须在 adapt **之前**记下来：adapt 会把 I 换成新的（更小的）那个
        gone = set(op_delta.K) & set(self.I)
        self.adapt(op_delta, witness)
        if self.delta != delta_new:
            raise NodeRejected(
                f"{self.node_id} 自己算出的摘要与协调者不一致"
                f"（n={self.delta.n} vs n={delta_new.n}）—— 更新密钥或新摘要有问题"
            )
        for i in gone:
            self._blobs.pop(i, None)
        if not self.check():
            raise NodeRejected(f"{self.node_id} 删除后本地视图不合法，拒绝落库")

    def absorb(
        self,
        *,
        delta_old: Digest,
        new_positions: Sequence[int],
        assigned: Sequence[int],
        values_all: Sequence[int],
        blobs: Mapping[int, bytes] | None = None,
    ) -> None:
        """``AddStorage``：把被指派的新位置并进本地视图。

        :param delta_old: 追加**前**的摘要 —— 用来构造 :math:`\\pi_K`
        :param new_positions: 这次追加的全部新下标 ``K``
        :param assigned: 指派给本节点的那些下标（``K`` 的**子集**）
        :param values_all: 与 ``new_positions`` 一一对应的新值
        """
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

        # 新位置那一整块的证据 **就是旧摘要本身** —— 不需要任何模幂
        pi_K = Opening(delta_old.U, delta_old.C, K)

        # 我可能只被指派了其中一段，于是从 π_K 拆出 π_seg
        current = self.node()
        if seg == K:
            pi_seg = pi_K
        else:
            crs_n = self.session.crs_n_for(current.delta)
            pi_seg = disagg(crs_n, K, list(values_all), pi_K, seg)

        merged = current.add_storage((seg, vals, pi_seg))
        self._view = LocalView(
            delta=merged.view.delta,
            st=merged.view.st,
            I=merged.view.I,
            FI=merged.view.FI,
        )
        if blobs:
            self._blobs.update({int(k): bytes(v) for k, v in blobs.items()})

    # -------------------------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        if not self.has_state:
            return f"NodeState({self.node_id!r}, 未初始化)"
        return (
            f"NodeState({self.node_id!r}, n={self._view.delta.n}, "
            f"|I|={len(self.I)}, 跨度 {span(self.I)})"
        )
