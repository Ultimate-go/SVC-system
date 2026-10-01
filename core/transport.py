"""节点传输层 —— 让协调者不必知道节点在不在同一个进程里。

:class:`NodeTransport` 是协调者与存储节点之间的**唯一**接口。两种实现：

======================  ==========================================
:class:`LocalTransport`  节点是同进程里的对象（单进程演示 / 测试）
``HttpTransport``        节点是独立进程，各自 SQLite（真分布式）
======================  ==========================================

协议刻意只有五个方法，而且**没有一个方法能让协调者改节点的状态**——
协调者只能发"发生了什么"（:math:`\\Delta` 与 :math:`\\Upsilon_\\Delta`）
以及"你负责哪些新位置"，新状态由节点自己算。这是两段式更新的要点，
也是这个抽象存在的意义：如果协调者能直接写节点状态，那"节点自己跟上更新"
就只是句空话。

.. warning::

   :meth:`NodeTransport.apply_append` 会把这次追加的**全部新值**交给每一台
   节点（不只是它被指派的那几个）。这不是实现偷懒：:math:`\\Lambda_I` 的
   修正项 :math:`\\prod_{j \\in K \\setminus I} (S_I^{1/e_j})^{v_j}` 用到了
   :math:`j \\notin I` 的值，方案本身就必须让节点知道。代价是**节点之间
   彼此知道对方的密文摘要**，但不知道明文 —— 秘密在明文那一层。
"""

from __future__ import annotations

from typing import Mapping, Protocol, Sequence, runtime_checkable

from svc.types import Opening, as_index_set

from vds.pos import Challenge, PoSProof, pos_prove
from vds.updates import UpdateDelta, UpdateWitness

from .node_state import NodeRejected, NodeState

__all__ = ["NodeTransport", "LocalTransport", "TransportError", "WriteError"]


class TransportError(Exception):
    """与节点通信失败（进程不在、超时、响应不合法）。"""


class WriteError(TransportError):
    """写路径上“有节点没跟上”—— 带**结构化**的失败清单，能直接拿来补推。

    为什么需要它（而不是一句字符串）：一次上传/改块只要有**一台**没跟上，
    全网就不一致了 —— 而且 **协调者自己的 δ 不会推进**。此时的现场是：

    * 已经跟上的那些节点：δ 已经是新的；
    * 没跟上的那几台：δ 还是旧的；
    * 协调者：δ 还是旧的，**但登记表已经分配过下标了**。

    于是“重新传一次文件”是**错**的（那会生成新的下标，而且会撞上
    “登记表游标与向量长度不一致”），唯一正确的修法是**把同一份更新补推给
    那几台** —— 所以异常里必须带着“哪几台”与“推的是什么”。

    :param failures: ``[{"node": "node-2", "reason": "..."}]``
    :param op: ``"append"`` 或 ``"update"``（决定补推打到哪个路由）
    :param payloads: ``{节点: 它就是没收到的那份请求体}`` —— 补推直接用
    """

    def __init__(
        self,
        message: str,
        failures: "Sequence[dict]",
        op: str = "append",
        payloads: "Mapping[str, dict] | None" = None,
    ) -> None:
        super().__init__(message)
        self.failures: list[dict] = [dict(f) for f in failures]
        self.op = op
        self.payloads: dict[str, dict] = dict(payloads or {})

    @property
    def nodes(self) -> list[str]:
        """没跟上的节点名（界面与审计要用）。"""
        return [str(f.get("node", "?")) for f in self.failures]


@runtime_checkable
class NodeTransport(Protocol):
    """协调者眼里的"一群存储节点"。"""

    node_ids: tuple[str, ...]

    def set_crs(self, crs_dict: Mapping[str, object]) -> None:
        """把公开参数交给各节点。

        节点**绝不能**自己生成一套 ``N``、``g`` —— 那会得到另一个群，
        所有验证必然失败。这是跨进程后最容易踩的坑。
        """

    def report(self, *, verify: bool = False) -> list[dict]:
        """每台节点的现状：``n``、持有多少块、下标跨度、一致性。

        ``verify=True`` 时才顺带做密码学验证（贵，与 ``|I|`` 线性）。
        """

    def retrieve(
        self, node_id: str, Q: Sequence[int]
    ) -> tuple[Opening, tuple[bytes, ...]]:
        """向一台节点索取 ``Q`` 的**内容**与一份子向量证据。

        返回 ``(π_Q, 密文段)``，两者按 ``Q`` 的顺序一一对应 —— 对应论文里
        ``StrgNode.Retrieve`` 的 ``(F_Q, π_Q)``：在本方案里**交付给对方的东西
        就是密文段**，所以检索只回这两样。

        ★ 刻意**不回**节点声称的向量分量：分量由调用方**自己从密文段算**。
        这样"承诺的分量"与"实际交付的字节"之间没有可声明的自由度 —— 节点
        换了密文，调用方算出的值就跟着变，承诺验证必然不过。
        """

    def pos_prove(self, node_id: str, indices: Sequence[int]) -> "PoSProof":
        """让一台节点回答一次**存储证明**（PoR）挑战，只回它自己那一份。

        与 :meth:`retrieve` 的区别不只是“少返回密文”：``retrieve`` 是**检索**，
        要把密文带回来让调用方自己算出分量（那是验证的唯一依据）；而 PoR 的目的恰恰是
        **不下载任何内容**就确认数据还在。

        :param indices: 挑战点名的下标集合 ``r``。节点只答 ``Q = I ∩ r``
            —— 一个下标都不沾的节点会返回一份**空份额**（不带群元素），
            由聚合阶段跳过。空份额**不能**直接拿去验证。
        """

    def adopt(
        self,
        node_id: str,
        *,
        positions: Sequence[int],
        values: Sequence[int],
        proof: Opening,
        blobs: Mapping[int, bytes] | None = None,
    ) -> None:
        """让一台节点**接收**一批已经承诺过的位置（``StrgNode.AddStorage``）。

        这是本协议里唯一一个「往节点里塞东西」的方法，所以它的定位要讲清楚：
        它塞的是**一份来自另一台节点的检索凭证** :math:`(Q, F_Q, \\pi_Q)`，
        节点会自己验一遍再合并 —— 协调者**不**（也无法）直接指定节点的新状态。

        为什么需要它（而不是复用 :meth:`apply_append`）：
        :meth:`apply_append` 处理的是**本次追加刚产生的新位置**，那批位置的
        证据恰好就是旧摘要本身；而这里搬的是**已经承诺过的**位置，
        「旧摘要」那条近路早就失效了，必须由当前持有者拆出一份 :math:`\\pi_Q`。
        两者在代数上都需要 :func:`~svc.agg`，但**前置材料完全不同** ——
        硬合成一个方法就得在里面按「这批位置是不是新产生的」分支，那等于把
        两条语义塞进同一个函数名里。

        用途只有一个：**台数变小**时，把即将被摘掉的机器上的块搬到留下的机器上
        （见 ``backend.manager.StoreManager.redistribute``）。
        时机很关键 —— 必须在那些机器**还活着**的时候搬，重启之后就来不及了。

        :param positions: 要接收的下标
        :param values: 与 ``positions`` 一一对应的分量
        :param proof: :math:`\\pi_Q`
        :param blobs: ``下标 -> 密文段``。节点要用它满足
            "声称持有的下标 == 实际存着的密文"这条不变式
        """

    def apply_append(
        self,
        *,
        delta_old,
        delta_new,
        op_delta: UpdateDelta,
        witness: UpdateWitness,
        assignments: Mapping[str, Sequence[int]],
        blobs: Mapping[str, Mapping[int, bytes]],
    ) -> None:
        """把一次追加通知**每一台**节点，让它们各自跟上。

        :param delta_old: 追加前的摘要（构造 :math:`\\pi_K` 要用）
        :param delta_new: 追加后的摘要
        :param assignments: ``节点 -> 指派给它的新下标``
        :param blobs: ``节点 -> {下标: 密文段}``
        """

    def apply_update(
        self,
        *,
        delta_new,
        op_delta: UpdateDelta,
        witness: UpdateWitness,
        blobs: Mapping[str, Mapping[int, bytes]],
    ) -> None:
        """把一次**修改**（``mod``）通知**每一台**节点，让它们各自跟上。

        与 :meth:`apply_append` 分开而不是合并成一个「通用更新」，是因为两者的
        前提不同：``add`` 要给新位置**分派归属**（``AddStorage``），而 ``mod``
        的位置早就有主了，节点只需要「跟上新摘要 + 换掉那一块的密文」。
        硬合成一个方法，就得在里面按 ``op`` 分支 —— 那等于把两条语义不同的
        路径塞进同一个函数名里。

        :param delta_new: 修改后的摘要（节点自己算出的必须与它逐位相同）
        :param blobs: ``节点 -> {下标: 新密文段}``；只有持有该下标的那台有内容

        .. note::

           这里**没有** ``delta_old``：节点当前的摘要就是「修改前」，
           协调者说了不算。这一条与 :meth:`apply_append` 的 409 幂等守卫
           效果相同 —— 掉队的节点算出的 ``C`` 对不上 ``delta_new``，当场暴露。
        """

    def drop(self, positions: Mapping[str, Sequence[int]]) -> None:
        """让若干节点**交出**自己手里的某些位置（论文的 ``StrgNode.RmvStorage``）。

        这是 ``del`` 的前置步骤，不是可选项：``vds.updates._apply_del`` 要求每个
        节点与待删集合 ``K``「要么全包含、要么全不相交」，**部分相交直接报错**。
        我们的分片是「轮转 + 每块 2 份副本」，一个跨台的末尾区间几乎必然让某些
        节点部分相交，所以必须先让它们把 ``K`` 那部分交出来。

        :param positions: ``节点 -> 要交回的下标``。空元组的节点会被跳过。

        .. important::

           **必须幂等**：交回实现里"本来就不在我手里的下标"直接跳过。否则
           "交回了 2 台、第 3 台掉线"之后整次截断就没法重来了。
        """

    def apply_delete(
        self,
        *,
        delta_new,
        op_delta: UpdateDelta,
        witness: UpdateWitness,
    ) -> None:
        """把一次**删除**（``del``）通知**每一台**节点，让它们各自跟上。

        与 :meth:`apply_update` 的两点不同（也是它没有并进那个方法的原因）：

        * ``Υ∆`` 里**要带** :math:`\\pi_K`（新摘要就是它），而 ``mod`` 的 ``Υ∆``
          里没有这一项；
        * 节点除了跟上新摘要，还要**把自己手里那些被删块的密文真删掉**
          —— "位置集合变小了"这件事 ``mod`` 从来没有过。

        :param delta_new: 删除后的摘要（节点自己算出的必须与它逐位相同）
        """


class LocalTransport:
    """节点是同进程对象。单进程演示与测试用。

    它和 ``HttpTransport`` 共享**同一份** :class:`~core.node_state.NodeState`
    逻辑 —— 这是刻意的：本地模式不能变成"另一个简化实现"，否则两条路径会
    悄悄分叉，测试也就失去意义了。
    """

    node_ids: tuple[str, ...]

    def __init__(self, session, node_ids: Sequence[str]) -> None:
        self.session = session
        self.node_ids = tuple(node_ids)
        self.states: dict[str, NodeState] = {}

    # -- 生命周期 -----------------------------------------------------------

    def set_crs(self, crs_dict: Mapping[str, object]) -> None:
        """本地模式下公开参数已经在 ``session`` 里，什么都不用做。"""
        return None

    def local_state(self, node_id: str) -> NodeState:
        """直接拿到节点的本地状态。

        **只有本地模式支持** —— 这正是它的用处：测试要能模拟"服务器偷改
        自己存的数据"，跨进程时那种攻击根本做不了（好事）。
        """
        state = self.states.get(node_id)
        if state is None:
            raise TransportError(f"节点 {node_id} 不存在（或还没有状态）")
        return state

    # -- 读 -----------------------------------------------------------------

    def report(self, *, verify: bool = False) -> list[dict]:
        out: list[dict] = []
        for nid in self.node_ids:
            state = self.states.get(nid)
            if state is None:
                out.append(
                    {
                        "node_id": nid,
                        "n": 0,
                        "held": 0,
                        "indices": [],
                        "span": "—",
                        "valid": True,
                        "proved": None,
                        "fresh": True,
                    }
                )
            else:
                out.append(state.describe(verify=verify))
        return out

    def retrieve(
        self, node_id: str, Q: Sequence[int]
    ) -> tuple[Opening, tuple[bytes, ...]]:
        state = self.states.get(node_id)
        if state is None:
            raise TransportError(f"节点 {node_id} 不存在")
        want = tuple(int(i) for i in Q)
        _F_Q, pi_Q = state.retrieve(want)
        try:
            cts = tuple(state.blobs[i] for i in want)
        except KeyError as exc:
            raise TransportError(
                f"{node_id} 声称持有下标 {exc.args[0]}，但没有对应的密文"
            ) from exc
        return pi_Q, cts

    def pos_prove(self, node_id: str, indices: Sequence[int]) -> PoSProof:
        """本地模式下直接跑 :func:`vds.pos.pos_prove`。

        走 ``state.node()`` 包出的 :class:`~vds.storage_node.StorageNode`，
        与跨进程模式下节点服务跑的是**同一份**算法 —— 本地模式不能变成
        “另一个简化实现”，否则两条路径会惄惄分叉。
        """
        state = self.states.get(node_id)
        if state is None:
            raise TransportError(f"节点 {node_id} 不存在")
        want = as_index_set(int(i) for i in indices)
        if not want:
            raise TransportError("挑战下标不能为空")
        # Challenge.n 只是记账（pos_prove 不看它），所以用节点自己的 n。
        return pos_prove(state.node(), Challenge(indices=want, n=state.delta.n))

    def adopt(
        self,
        node_id: str,
        *,
        positions: Sequence[int],
        values: Sequence[int],
        proof: Opening,
        blobs: Mapping[int, bytes] | None = None,
    ) -> None:
        """本地模式：直接调 :meth:`~core.node_state.NodeState.adopt`。

        与跨进程模式跑的是**同一份** ``NodeState`` 逻辑 —— 这里不做任何简化，
        否则两条路径会悄悄分叉（见类文档）。
        """
        state = self.states.get(node_id)
        if state is None:
            raise TransportError(f"节点 {node_id} 不存在")
        try:
            state.adopt(positions, values, proof, blobs=blobs)
        except NodeRejected as exc:
            raise TransportError(f"{node_id} 拒绝接收：{exc}") from exc
        if not state.check():
            raise TransportError(f"{node_id} 接收后本地视图不合法")
        self.states[node_id] = state

    # -- 写 -----------------------------------------------------------------

    def apply_append(
        self,
        *,
        delta_old,
        delta_new,
        op_delta: UpdateDelta,
        witness: UpdateWitness,
        assignments: Mapping[str, Sequence[int]],
        blobs: Mapping[str, Mapping[int, bytes]],
    ) -> None:
        for nid in self.node_ids:
            seg = tuple(assignments.get(nid, ()))
            state = self.states.get(nid)

            if state is None or not state.has_state:
                # 第一次被指派任务：从空视图起步（π_∅ = (U_n, C_n)）
                state = NodeState(nid, self.session)
                state.adopt_empty(delta_new)
            else:
                try:
                    state.adapt(op_delta, witness)
                except NodeRejected as exc:
                    raise TransportError(f"{nid} 拒绝更新：{exc}") from exc
                if state.delta != delta_new:
                    # 节点自己算出来的摘要必须与协调者算的逐位相同 ——
                    # 两条路径（apply_update vs push_update）的交叉验证
                    raise TransportError(
                        f"{nid} 自己算出的摘要与协调者不一致："
                        f"{state.delta!r} vs {delta_new!r}"
                    )

            state.absorb(
                delta_old=delta_old,
                new_positions=tuple(op_delta.K),
                assigned=seg,
                values_all=tuple(op_delta.F_new),
                blobs=blobs.get(nid),
            )
            if not state.check():
                raise TransportError(f"{nid} 更新后本地视图不合法")
            self.states[nid] = state

    def apply_update(
        self,
        *,
        delta_new,
        op_delta: UpdateDelta,
        witness: UpdateWitness,
        blobs: Mapping[str, Mapping[int, bytes]],
    ) -> None:
        for nid in self.node_ids:
            state = self.states.get(nid)
            if state is None or not state.has_state:
                raise TransportError(
                    f"{nid} 还没有状态，无法应用 {op_delta.op} 更新"
                    f"（修改只能作用于已经分发下去的数据）"
                )
            try:
                state.adapt(op_delta, witness)
            except NodeRejected as exc:
                raise TransportError(f"{nid} 拒绝更新：{exc}") from exc
            if state.delta != delta_new:
                # 与 apply_append 同一道交叉验证：两条路径（apply_update
                # vs push_update）必须算出逐位相同的摘要。
                raise TransportError(
                    f"{nid} 自己算出的摘要与协调者不一致："
                    f"{state.delta!r} vs {delta_new!r}"
                )
            try:
                state.replace_blobs(blobs.get(nid, {}))
            except NodeRejected as exc:
                raise TransportError(f"{nid} 换密文失败：{exc}") from exc
            if not state.check():
                raise TransportError(f"{nid} 更新后本地视图不合法")
            self.states[nid] = state

    def drop(self, positions: Mapping[str, Sequence[int]]) -> None:
        """让被点名的节点交出某些位置（``del`` 的前置步骤）。

        与写路径不同：这一步**不改**全局摘要，只让节点的 ``I`` 变小、并删掉
        对应的密文。它幂等，所以"交回一半之后重来"是安全的。
        """
        for nid, want in positions.items():
            if not want:
                continue
            state = self.states.get(nid)
            if state is None or not state.has_state:
                # 没有状态的节点没什么可交的 —— 成功返回（幂等），不当作故障
                continue
            try:
                state.drop(tuple(int(i) for i in want))
            except NodeRejected as exc:
                raise TransportError(f"{nid} 交回失败：{exc}") from exc
            if not state.check():  # pragma: no cover - 兜底不变式
                raise TransportError(f"{nid} 交回后本地视图不合法")

    def apply_delete(
        self,
        *,
        delta_new,
        op_delta: UpdateDelta,
        witness: UpdateWitness,
    ) -> None:
        """把一次删除通知每一台节点（节点自己算新摘要、自己删密文）。"""
        for nid in self.node_ids:
            state = self.states.get(nid)
            if state is None or not state.has_state:
                # 从来没有拿到过状态的节点：它没有 δ 要推进，也没有密文要删。
                # `apply_append` 会给它 adopt_empty 后再收数据，所以这里跳过是安全的。
                continue
            try:
                state.apply_delete(op_delta, witness, delta_new)
            except NodeRejected as exc:
                raise TransportError(f"{nid} 拒绝删除更新：{exc}") from exc
            self.states[nid] = state

    # -- 持久化（仅本地模式需要）--------------------------------------------

    def export_states(self) -> dict[str, dict]:
        """导出全部节点状态，供单进程模式写库、重启后恢复。"""
        out: dict[str, dict] = {}
        for nid, state in self.states.items():
            if not state.has_state:
                continue
            out[nid] = {
                "delta": (state.delta.U, state.delta.C, state.delta.n),
                "st": (state.st.S_I, state.st.Lambda_I),
                "I": list(state.I),
                "FI": list(state.FI),
                "blobs": dict(state.blobs),
            }
        return out

    def import_states(self, data: Mapping[str, Mapping[str, object]]) -> None:
        """从 :meth:`export_states` 的结果恢复。"""
        from vds.digest import Digest, LocalView

        for nid, blob in data.items():
            U, C, n = blob["delta"]  # type: ignore[misc]
            S_I, Lambda_I = blob["st"]  # type: ignore[misc]
            I = tuple(blob["I"])  # type: ignore[arg-type]
            self.states[nid] = NodeState(
                nid,
                self.session,
                LocalView(
                    delta=Digest(U=int(U), C=int(C), n=int(n)),
                    st=Opening(int(S_I), int(Lambda_I), I),
                    I=I,
                    FI=tuple(blob["FI"]),  # type: ignore[arg-type]
                ),
                blobs=dict(blob["blobs"]),  # type: ignore[arg-type]
            )

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"LocalTransport({len(self.states)}/{len(self.node_ids)} 台有状态)"
