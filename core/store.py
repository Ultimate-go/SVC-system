"""``VectorStore`` —— 设计 B（全系统一条向量）的**协调者**。

它做什么
--------
1. **全局向量** —— 全系统唯一的 :math:`V`，所有用户的所有文件按上传顺序追加到末尾；
2. **登记表** —— :class:`~core.registry.BlockRegistry` 记录"哪一块在向量上的哪个位置"；
3. **分片决策** —— 哪个新位置归哪台服务器；
4. **检索与验证** —— 向服务器要内容与证据，聚合成**一份**，对着全局 δ 验证。

它**不做什么**（这是"节点进程化"后的关键区别）
---------------------------------------------
* **不持有任何节点的状态。** 服务器的 :math:`(S_I, \\Lambda_I, I, F_I)`
  与它那段密文都在节点自己那边（:mod:`core.node_state` /
  :mod:`core.transport`）。
* **不替节点算新状态。** 追加时它只广播"发生了什么"（:math:`\\Delta` 与
  :math:`\\Upsilon_\\Delta`）以及"你负责哪些新位置"，各节点自己算出新状态。
* **不存密文。** 它只在 ``self._holder`` 里记"哪块归谁"，用来知道该去问谁。
* **不保管任何密钥。** 块密钥生成后立刻交给调用方（``upload`` 的
  ``key_sink``），解密时再从外面取（``read`` 的 ``key_of``）。
  这样"密钥该怎么保护"就成了调用方的显式决定（现在是
  :mod:`core.keywrap`：用**所有者的 SM2 公钥**包起来），
  本类里不可能悄悄留下一份明文密钥。

所以本类在**单进程**与**多进程**两种部署下跑的是同一份代码，区别只在
注入的 :class:`~core.transport.NodeTransport`。

因为没有"每个文件一条向量"这回事，所以：

* 全系统**只有一份摘要 δ** —— 任何用户都能验证任意位置（验证不受限）；
* 一个证据可以**横跨多个文件** —— 只要下标集合覆盖到那几个文件就行。

一条验证线：分量由验证方**自己从密文算**
----------------------------------------
向量里承诺的"值"是 :func:`~core.crypto.vector_element`（``SM3``）算出的**分量**。
本项目的一条纪律是：

    验证方**不采用节点声称的分量** —— 它拿回来的密文段**自己算**
    ``F_i := vector_element(c_i)``，再拿这组自己算出来的值去跑承诺验证
    （:func:`svc.verify` 的两步校验，对着全局 δ）。

于是承诺验证的结论就是**唯一且完整**的正确性保证，不需要另设一层"密文与
分量对不对得上"的自检：

* 分量对不上承诺 ⇒ 拒绝（节点改了自己存的分量）；
* 分量对得上、但密文被换过 ⇒ 验证方自己算出的值随之改变 ⇒ 一样拒绝。

也就是说，"承诺的分量"与"实际交付的字节"之间的绑定由**推导方向**保证：
分量在这一侧的**唯一来源就是收到的密文本身**，对方没有"另行声明"的自由度。
（相对"把密文段直接当值"的做法，这里多依赖一条 ``SM3`` 抗碰撞假设；换来的
是每块只占 1 个位置、上传速度与容量都不受影响。）
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable, Collection, Mapping, Sequence

from svc import CRSn, commit, open_subvector, specialize, verify
from svc.types import Opening, VerifyReport, as_index_set

from vds.client_node import Certificate, ClientNode
from vds.digest import Digest, LocalView
from vds.pos import (
    DEFAULT_LAMBDA_POS,
    PoSProof,
    pos_aggregate_all,
    pos_challenge,
    pos_ver,
)
from vds.storage_node import StorageNode
from vds.updates import UpdateDelta, push_update

from .crypto import (
    SEGMENT_BYTES,
    decrypt_segment,
    encrypt_segment,
    file_digest,
    new_iv,
    new_key,
    split_segments,
    vector_element,
)
from .node_state import NodeState, span
from .registry import BlockRef, BlockRegistry, SegmentRegistry
from .session import GlobalSession
from .timing import stage
from .transport import LocalTransport, NodeTransport, TransportError

__all__ = ["VectorStore", "PlainKeyStore", "FileRecord", "QueryResult"]

#: 默认的存储服务器台数。设计 B 下"台数"与文件无关 —— 它是全网的分片数。
DEFAULT_NODE_IDS: tuple[str, ...] = ("node-1", "node-2", "node-3", "node-4")


@dataclass
class FileRecord:
    """一次上传的账目。**它不属于密码学层**，是应用层（数据库）要存的东西。

    :param indices: 这个文件占用的全局下标
    :param ivs: 每段的 IV。**IV 不是秘密**（它只保证同一把密钥下的密文不同），
        所以可以明文留着 —— 真正要保护的是密钥。
    :param plain_lengths: 每段的**明文**字节数（最后一段通常不足一整段）。
        密文与明文等长（CTR 是流式异或）。

    .. important::

       **这里没有 ``keys`` 字段**，而且不是忘了写。密钥一生成就被
       :meth:`VectorStore.upload` 的 ``key_sink`` 交出去了，本类不保管；
       解密时由 :meth:`VectorStore.read` 的 ``key_of`` 从外面取。

       这么切是有原因的：整条访问控制的意义就在于"**块密钥只有所有者算得出来**"。
       只要协调者手里还留着一份明文密钥，那道门就只是个装饰 ——
       一旦数据库或进程被拿走，全部明文照旧泄露。
       所以密钥的**唯一**存放处是它的密文（``blocks.key_ct`` 那一列）。
    """

    owner: str
    file_key: str
    indices: tuple[int, ...]
    ivs: list[bytes] = field(repr=False)
    plain_lengths: list[int]
    total_bytes: int
    segment_bytes: int
    content_digest: str

    @property
    def block_count(self) -> int:
        return len(self.indices)

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return (
            f"FileRecord({self.owner}/{self.file_key}, "
            f"{self.block_count} 块, {self.total_bytes} 字节)"
        )


def proof_bytes(S_I: int, Lambda_I: int) -> int:
    """证据的字节数 —— **两个群元素**，与文件大小、与打开多少块都无关。

    提成模块级函数是为了让"证据有多大"只有**一处**算法：
    :class:`QueryResult` 与 ``StoreManager.disagg`` 都调它。
    两边各算一遍迟早会分叉，而分叉的表现是界面上两个地方报出不同的字节数。
    """
    bits = max(S_I.bit_length(), Lambda_I.bit_length())
    return 2 * ((bits + 7) // 8)


@dataclass
class QueryResult:
    """一次查询的完整交代。"""

    indices: tuple[int, ...]
    values: tuple[int, ...]
    proof: Opening
    report: VerifyReport
    holders: dict[int, str]
    refs: tuple[BlockRef, ...]
    cert_count: int
    node_used: tuple[str, ...]
    #: **没拿到**的下标（只在 ``allow_partial=True`` 时非空）。
    #:
    #: 它是“这份结论**不覆盖**哪些块”的唯一依据 —— 界面必须把它显示出来，
    #: 不能拿一份部分证据去宣称“整个文件验过了”。
    missing: tuple[int, ...] = ()

    @property
    def partial(self) -> bool:
        """这份结果是**部分的**（有块没拿到）。"""
        return bool(self.missing)

    @property
    def proof_size_bytes(self) -> int:
        """证据规模 —— **两个群元素**，与文件大小、与查询块数都无关。"""
        return proof_bytes(self.proof.S_I, self.proof.Lambda_I)

    @property
    def ok(self) -> bool:
        """承诺验证（:func:`svc.verify`）的结论 —— **唯一的正确性结论**。

        向量里承诺的分量是本端从收到的密文**自己算**出来的（见
        :meth:`_collect`），所以这一个结论已经连带盖住了"交付的字节
        对不对得上承诺的分量"，没有第二层可看。
        """
        return bool(self.report.ok)

    def summary(self) -> str:
        """给人看的一行结论。"""
        if not self.report.ok:
            return f"承诺验证失败：{self.report.message}"
        return (
            f"通过：{len(self.indices)} 块 / {self.cert_count} 份凭证 / "
            f"证据 {self.proof_size_bytes} 字节"
        )

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"QueryResult({list(self.indices)}, ok={self.ok})"


class VectorStore:
    """全系统一条向量的协调者。

    :param session: 公开参数会话
    :param node_ids: 存储服务器标识（**固定不变的分片数**）
    :param segment_bytes: 一个向量分量对应的密文字节数
    :param transport: 与节点通信的方式；``None`` 时用同进程的
        :class:`~core.transport.LocalTransport`
    """

    __slots__ = (
        "session",
        "registry",
        "deltas",
        "transport",
        "values",
        "files",
        "segment_bytes",
        "_holder",
        "_replicas",
        "_offset",
        "replica_factor",
        #: 写失败时留住的现场（见 retry_pending）。★ 有 __slots__ 的类**新增字段
        #: 必须写在这里**，否则赋值时直接 AttributeError（我在这一步上踩过一次：
        #: 忘了加，于是 100+ 个用例一起报 "'PlainKeyStore' object has no attribute
        #: '_pending'"）。
        "_pending",
    )

    def __init__(
        self,
        session: GlobalSession,
        node_ids: tuple[str, ...] = DEFAULT_NODE_IDS,
        segment_bytes: int = SEGMENT_BYTES,
        transport: NodeTransport | None = None,
        replica_factor: int = 1,
    ) -> None:
        """
        :param replica_factor: 每块存**几份**。默认 1 —— 与“每块只存一份”的
            旧行为完全一致（所以算法层的既有测试一字不改）。部署默认开副本，
            由 ``Settings.replica_factor`` 决定（见 backend/config.py）。

        .. important::

           **副本不占全局位置。** ``n`` 仍然等于“块数”，不乘副本数 ——
           多副本只多占各节点自己的存储，不影响承诺、不影响证据大小，
           所以“一次上传 = 一次全网事件”这个代价不会因副本翻倍。
        """
        if segment_bytes <= 0:
            raise ValueError("segment_bytes 必须为正")
        if transport is None:
            if not node_ids:
                raise ValueError("至少需要一台存储服务器")
            if len(set(node_ids)) != len(node_ids):
                raise ValueError("节点标识不能重复")
            transport = LocalTransport(session, node_ids)
        if replica_factor < 1:
            raise ValueError(f"副本数至少为 1，收到 {replica_factor}")
        if replica_factor > len(transport.node_ids):
            raise ValueError(
                f"副本数 {replica_factor} 比服务器台数 {len(transport.node_ids)} 还多 —— "
                f"副本必须落在不同的机器上，否则掉一台就一起没了"
            )

        self.session = session
        self.transport = transport
        self.segment_bytes = int(segment_bytes)
        self.replica_factor = int(replica_factor)
        # ★ 新方案：每份文件占自己一段（或几段）位置，段永不回收。
        #   ``BlockRegistry``（要求“下标全系统密集唯一”）换成 ``SegmentRegistry``。
        self.registry = SegmentRegistry()
        #: ★ 新方案：**每份文件各自的摘要**。设计 B 里这里是 ``self.delta``（一条），
        #: 每次上传都会推进它，并让所有节点的份额一起失效；现在每份文件独立，
        #: 上传新文件只写它自己那一项，老文件的 δ 逐位不变。
        self.deltas: dict[tuple[str, str], Digest] = {}
        #: 全局位置号 -> 分量值（键是**全局位置号**，不是“文件内第几块”）
        self.values: dict[int, int] = {}
        self.files: dict[tuple[str, str], FileRecord] = {}
        #: 全局下标 -> **主副本**（对外“去问谁拿”就是它）
        self._holder: dict[int, str] = {}
        #: 全局下标 -> **全部**持有者（主副本在前）。副本数 > 1 时才有意义。
        self._replicas: dict[int, tuple[str, ...]] = {}
        self._offset: int = 0
        #: 上一次写失败时留住的现场（见 :meth:`retry_pending`）。``None`` = 没有待补推的。
        self._pending: dict | None = None

    # -------------------------------------------------------------------
    # 按文件取摘要
    # -------------------------------------------------------------------

    def _vals_of(self, file_id: tuple[str, str]) -> list[int]:
        """一份文件的**全部分量值**，按它自己的块号顺序。

        vds 层要的 ``aux`` / ``vals`` 就是这个形状（密集下标 0..n-1 顺序）——
        而 :attr:`values` 的键是全局位置号，所以这里要过一次映射。
        """
        return [self.values[g] for g in self.registry.positions_of(*file_id)]

    def _delta_before(self, file_id: tuple[str, str], K: tuple[int, ...]) -> Digest:
        """这次操作**之前**那份文件的摘要 δ。

        已经登记过的文件直接用它的 δ；新文件则从“空向量”起步，段起点取它刚
        分配到的第一段（:meth:`~core.registry.SegmentRegistry.alloc` 已记下）。

        ★ 空向量写作 ``(U=g, C=1, n=0)`` 而不是论文 §8.2 的 ``(1, g)``：
        后者不满足 :math:`U = g^{E}` 这个不变式，从它出发做 ``add_back``
        会得到两项都错的结果（见 :meth:`GlobalSession.bootstrap`）。
        """
        got = self.deltas.get(file_id)
        if got is not None:
            return got
        segs = self.registry.segments_of(*file_id)
        return Digest(U=self.session.crs.g, C=1, n=0, offset=segs[0][0])

    # -------------------------------------------------------------------
    # 只读视图
    # -------------------------------------------------------------------

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(self.transport.node_ids)

    @property
    def n(self) -> int:
        """全系统当前**在用**的块数（= 各文件的块数之和）。

        ★ 它与设计 B 的 ``n`` 不是一回事：那边 ``n`` 既是“块数”又是“向量长度”，
        两者恒等；新方案里每份文件各有自己的长度，所以这里的 ``n`` 只是
        “一共存了多少块”。而 :attr:`registry.next_offset` 才是“位置用到了哪”
        —— 它更大，因为废弃的段（删文件、截断留下的空洞）**永不回收**。
        """
        return self.registry.total_blocks()

    def holder_of(self, global_index: int) -> str:
        try:
            return self._holder[global_index]
        except KeyError:
            raise KeyError(f"不知道全局下标 {global_index} 归谁持有") from None

    def replicas_of(self, global_index: int) -> tuple[str, ...]:
        """这个下标**全部**的持有者，**主副本在前**。

        没有副本时退化成 ``(主副本,)`` —— 所以调用方（如改块时要推给谁）
        不需要分“开没开副本”两种情况。
        """
        got = self._replicas.get(global_index)
        if got:
            return got
        one = self._holder.get(global_index)
        if one is None:
            raise KeyError(f"不知道全局下标 {global_index} 的副本") from None
        return (one,)

    def set_replicas(self, global_index: int, replicas: Sequence[str]) -> None:
        """改写一个下标**全部**持有者（主副本取第一项）。

        ★ 只有"改服务器台数"这一条路会调它。**它是记账，不是数据搬运** ——
        调用方必须已经把内容真的搬到了目标机器上（见
        ``backend.manager.StoreManager.redistribute``）。单改这一张表
        只会得到一个"账上有人、实际没人"的状态，而那种状态要等到**读**的时候
        才暴露（``_pick_holders`` 报"每一份副本都拿不到"），归因很难。

        **顺序有意义**：第一项就是主副本（``holder_of`` 取它）。台数变小时
        调用方必须把"留下来的"排在前面 —— 否则重启后
        ``manager._reload`` 会以"副本列表与主副本不一致"直接拒绝启动。

        :raises ValueError: 空列表、有重复、或名字不在本集群里
        """
        got = tuple(replicas)
        if not got:
            raise ValueError(f"下标 {global_index} 的副本列表不能为空")
        if len(set(got)) != len(got):
            raise ValueError(f"下标 {global_index} 的副本列表有重复：{list(got)}")
        unknown = [n for n in got if n not in self.node_ids]
        if unknown:
            raise ValueError(
                f"下标 {global_index} 的副本里有不在本集群的机器 {unknown}"
                f"（本集群是 {list(self.node_ids)}）"
            )
        self._replicas[global_index] = got
        self._holder[global_index] = got[0]

    def file_indices(self, owner: str, file_key: str) -> tuple[int, ...]:
        """一份文件占用的全部**全局位置号**（按它自己的块号顺序）。"""
        return self.registry.positions_of(owner, file_key)

    def delta_of(self, owner: str, file_key: str) -> Digest:
        """一份文件当前的摘要 δ（每份文件一条向量，所以各有一份）。"""
        try:
            return self.deltas[(owner, file_key)]
        except KeyError:
            raise KeyError(f"{owner}/{file_key} 没有登记过（或还没有摘要）") from None

    def describe(self, global_index: int) -> BlockRef:
        """全局位置号 → “这是谁的第几块”。"""
        key = self.registry.key_of_position(global_index)
        if key is None:
            raise KeyError(
                f"全局位置 {global_index} 不属于任何文件"
                f"（要么从未分配，要么是删文件/截断留下的空洞 —— 段永不回收）"
            )
        owner, file_key = key
        pos = self.registry.positions_of(owner, file_key)
        return BlockRef(owner, file_key, pos.index(int(global_index)))

    def _group_by_file(
        self, Q: Sequence[int]
    ) -> dict[tuple[str, str], tuple[int, ...]]:
        """把一串**全局位置号**按“属于哪份文件”分组。

        每份文件是一条独立向量，所以“验证”这件事只能在同一份文件内部完成
        （证据与它那条向量的 :math:`E` 绑死）；跨文件要先合并成一条，
        见 :meth:`_aggregate_files`。

        :returns: ``{文件键: (该文件里的全局位置号, ...)}``，每组按位置升序。
        """
        out: dict[tuple[str, str], list[int]] = {}
        for g in Q:
            key = self.registry.key_of_position(int(g))
            if key is None:
                raise KeyError(
                    f"全局位置 {g} 不属于任何文件"
                    f"（要么从未分配，要么是删文件/截断留下的空洞 —— 段永不回收）"
                )
            out.setdefault(key, []).append(int(g))
        return {k: tuple(sorted(v)) for k, v in out.items()}

    def _local_of(self, file_id: tuple[str, str], Q: Sequence[int]) -> tuple[int, ...]:
        """全局位置号 → **该文件内**的局部块号（``svc`` 层只认后者）。"""
        pos = self.registry.positions_of(*file_id)
        idx = {g: i for i, g in enumerate(pos)}
        return tuple(idx[int(g)] for g in Q)

    def owner_files(self, owner: str) -> tuple[str, ...]:
        """某个用户上传过的全部文件键。"""
        return tuple(fk for (o, fk) in self.files if o == owner)

    def local_state(self, node_id: str) -> NodeState:
        """拿到节点的本地状态 —— **仅同进程模式支持**。

        测试与演示要用它模拟"服务器偷改自己存的数据"。跨进程时这种攻击
        做不了（好事），所以这个入口也只该在本地模式存在。
        """
        getter = getattr(self.transport, "local_state", None)
        if getter is None:
            raise NotImplementedError(
                f"{type(self.transport).__name__} 不支持直接访问节点内部状态"
            )
        return getter(node_id)

    # -------------------------------------------------------------------
    # 上传 —— 追加到全局向量
    # -------------------------------------------------------------------

    def upload(
        self,
        owner: str,
        file_key: str,
        plaintext: bytes,
        *,
        key_sink: Callable[[int, bytes], None],
        segment_bytes: int | None = None,
        nodes: Sequence[str] | None = None,
    ) -> FileRecord:
        """加密 + 分块 + 承诺 + 分发，把新块追加到全局向量末尾。

        :param key_sink: ``key_sink(pos, key)`` —— 每生成一把块密钥就交出去一次。
            **必传，没有默认值**：本类**不保管任何密钥**，调用方必须显式
            决定"这把钥匙放哪里"。现在就是在这里用**所有者的公钥**
            把它包成密文（``core.keywrap.wrap_key``）。
        :param segment_bytes: 这份文件按多少字节切一块。``None`` = 用本向量的
            默认值。

            .. important::

               **块大小是逐文件记的**（它进 :class:`FileRecord`），所以
               :meth:`modify` 与 :meth:`append` 都按**这份文件自己的**值
               校验与切分，而不是按本向量当下的默认值 —— 否则换个默认值
               就会让老文件改不了、或者切出错位的块。

        :param nodes: **这次上传只往哪几台机器上摊块**。``None`` = 全部机器。
            传了就是“手动指定分发”：轮转只在这些机器里发生，其它机器
            **一块也拿不到**（但他们仍然会收到状态推进通知）。

            .. important::

               * **顺序不再决定谁拿哪块** —— 这几台会先被打乱再轮转（界面上按
                 什么顺序勾都一样；只有"哪几台参与"有意义）；
               * 同一台不能重复；不认识的机器名会直接报错（不会静默忽略）；
               * 台数不能少于副本数（副本必须落在**不同的**机器上）。

            .. note::

               **它只影响分到哪台，不影响密码学。** 全局下标的语义、
               承诺、δ、证据大小全都与“摊给几台”无关 —— 所以完全
               相同的两份文件，选不同的机器，摘要一模一样。

        :raises ValueError: 文件为空 / 同名文件已存在 / 块大小非正 / 超出 ``n_max``
        """
        if not plaintext:
            raise ValueError("空文件没有意义")
        seg_bytes = self.segment_bytes if segment_bytes is None else int(segment_bytes)
        if seg_bytes <= 0:
            raise ValueError(f"块大小必须为正，收到 {seg_bytes}")
        # ★ “往哪儿摊”必须在这里就校 —— 放到 _plan 里才发现的话，
        #   登记表已经分配了，报错后会在登记表里留下一段**没人持有**的脏下标
        #   （既有用例 ``test_超过n_max被拒且不留脏登记`` 防的就是这个）。
        self._resolve_pool(nodes)
        key = (owner, file_key)
        if key in self.files:
            raise ValueError(f"文件 {owner}/{file_key} 已存在（修改走后续的更新接口）")

        # ★ 下面这些 ``with stage(...)`` 是给前端报耗时用的（见 core/timing.py）。
        #   没有人在收集时它们返回 nullcontext —— **一次计时调用都不会发生**，
        #   所以对算法行为零影响。
        with stage("切块（split_segments）"):
            segs = split_segments(plaintext, seg_bytes)
        elements: list[int] = []
        cts: list[bytes] = []
        ivs: list[bytes] = []
        lens: list[int] = []
        for pos, seg in enumerate(segs):
            with stage("生成密钥与 IV"):
                k_i, iv_i = new_key(), new_iv()
            with stage("逐块加密（SM4）"):
                ct = encrypt_segment(seg, k_i, iv_i)
            # ★ 即时交给调用方（由它用所有者公钥包起来），本函数不留副本、本类不留副本
            key_sink(pos, k_i)
            with stage("算向量分量（SM3）"):
                v_i = vector_element(ct)
            ivs.append(iv_i)
            lens.append(len(seg))
            cts.append(ct)
            elements.append(v_i)

        # 先查容量再登记 —— 否则超限时会在登记表里留下一段没人持有的脏位置。
        # ★ 新方案下要查的是“**全系统位置预算**”：每份文件占一段、
        #   段永不回收，所以 n_max 约束的是“所有文件块数之和 + 历史空洞”的上界。
        offset, _seg = self.registry.alloc(
            owner, file_key, len(elements), n_max=self.session.n_max
        )
        K = tuple(range(offset, offset + len(elements)))

        # ★ FileRecord 要在**推之前**就造好，并把"登记它"作为收尾回调交给
        #   _append —— 这样写推失败时，补推成功之后那份文件才会真的出现
        #   （否则向量是自洽的，但系统里"没有这个文件"）。
        rec = FileRecord(
            owner=owner,
            file_key=file_key,
            indices=K,
            ivs=ivs,
            plain_lengths=lens,
            total_bytes=len(plaintext),
            segment_bytes=seg_bytes,
            content_digest=file_digest(plaintext),
        )

        def _register() -> None:
            self.files[key] = rec

        self._append(
            elements, cts, file_id=key, K=K, pool=nodes, on_success=_register
        )
        return rec

    def modify(
        self,
        owner: str,
        file_key: str,
        block_idx: int,
        plaintext: bytes,
        *,
        key_sink: Callable[[int, bytes], None],
    ) -> tuple[bytes, int]:
        """改**一块**的内容：重新加密它，并让全网跟上新的向量分量。

        一次修改的网络代价与文件大小无关 —— 只有那**一块**的密文被替换，
        其余块的密文、密钥、IV 一个字都不动。这是 ``upload`` 里
        「先 :func:`split_segments`、再逐块独立 :func:`encrypt_segment`」
        这个顺序换来的：每块一把密钥、一个 IV，块与块互不牽连，
        所以也不需要按块大小重新切。

        :param key_sink: 同 :meth:`upload`，``key_sink(块号, 新密钥)``。
            改块必然产生**新的**块密钥（旧密钥连同旧密文一起作废），
            调用方必须**重新封装**它（现在就是拿所有者的 SM2 公钥再封一次）
            —— 否则新内容连所有者自己也解不开。
        :returns: ``(新 IV, 新明文长度)``；调用方拿它更新自己那份账目

        .. warning::

           :attr:`FileRecord.content_digest` **不会**跟着变。它是**上传时刻**
           整份明文的 SHA-256，而协调者手里从来没有完整明文（只有各块的
           密文摘要），算不出新的整体摘要。改过块的文件，那个字段只代表
           「上传时的样子」，界面与接口都不该拿它当当前内容的指纹。

        :raises KeyError: 文件不存在
        :raises IndexError: 块号越界
        :raises ValueError: 空块，或超过了 ``segment_bytes``
        """
        key = (owner, file_key)
        rec = self.files.get(key)
        if rec is None:
            raise KeyError(f"文件 {owner}/{file_key} 不存在（先上传再改）")
        n_blocks = len(rec.indices)
        if not 0 <= block_idx < n_blocks:
            raise IndexError(
                f"块号 {block_idx} 越界：{owner}/{file_key} 只有 {n_blocks} 块"
            )
        if not plaintext:
            raise ValueError("空块没有意义")
        if len(plaintext) > rec.segment_bytes:
            raise ValueError(
                f"这一块有 {len(plaintext)} 字节，超过单块上限 {rec.segment_bytes} "
                f"—— 块的大小是上传时切好的（这份文件切的是 {rec.segment_bytes} 字节），"
                f"改块只换内容、不重新切；需要更多空间请重新上传"
            )

        gidx = rec.indices[block_idx]
        with stage("生成密钥与 IV"):
            key_new, iv_new = new_key(), new_iv()
        with stage("逐块加密（SM4）"):
            ct_new = encrypt_segment(plaintext, key_new, iv_new)
        with stage("算向量分量（SM3）"):
            v_new = vector_element(ct_new)
        # ★ 与 upload 同一条纪律：密钥一生成就交出去，本类不留副本
        key_sink(block_idx, key_new)

        # ★ 两套下标：gidx 是这个块在**全局素数表**里的位置（记账、问节点都用它）；
        #   block_idx 是它在**这份文件内**的块号 —— vds 层只认后者（它假定
        #   下标密集 0..n-1，“第 i 块对素数表第 offset+i 个”由素数视图承担）。
        delta_old = self.delta_of(owner, file_key)
        op_delta = UpdateDelta(op="mod", K=(block_idx,), F_new=(v_new,))
        # 推送方只要出 S_K，而 S_K 与谁发起无关（K 不在 I 里时退回直接模幂，
        # 见 vds.updates._S_K），所以协调者用空视图占位即可 ——
        # 它**不需要**持有这一块，也就不必为了一次改块把明文调进来。
        with stage("承诺（push_update）"):
            pushed = push_update(
                self.session,
                delta_old,
                # ★ 必须用「**持有整份文件**」的占位节点：``_mod_node_state`` 会把
                #   ``K`` 里不在 ``node.I`` 中的下标当成“节点不持有的部分”，
                #   去走 ShamirTrick 求 :math:`S_I^{1/e_i}`；而空视图的占位节点
                #   ``e_I = 1``，两根根本不同源，必炸。
                #   协调者手里本来就有全部分量的值（``_vals_of``），直接给一个
                #   ``I = range(n)`` 的节点，``outside`` 为空、``Λ_I`` 一个字都
                #   不用动 —— 这在数学上也正是对的：``Λ_I`` 只含
                #   :math:`i \notin I` 的值，改块**不该**碰到它。
                #
                #   （原注释说“推送方只要出 S_K，用空视图占位即可”只对一半：
                #     ``_S_K`` 那条“K ⊆ I 走 disagg、否则退回直接模幂”的分支确实
                #     能容忍空视图，但 ``_mod_node_state`` 不能。单块文件时
                #     ``U == S_0`` 恰好歪打正着，``n ≥ 2`` 就必挂。）
                _full_holder(self.session, delta_old, self._vals_of(key)),
                op_delta,
                {block_idx: self.values[gidx]},
            )
        holders = self.replicas_of(gidx)

        def _patch_record() -> None:
            """新 IV / 新长度回到文件账目上（正常路径与补推路径共用）。"""
            rec.ivs[block_idx] = iv_new
            rec.plain_lengths[block_idx] = len(plaintext)
            rec.total_bytes = sum(rec.plain_lengths)

        context = {
            "file_id": key,
            "delta_new": pushed.delta,
            "per_index": {gidx: tuple(holders)},
            "K": (gidx,),
            "elements": (v_new,),
            "on_success": _patch_record,
        }

        # 每一份副本都要拿到新密文 —— 只推一份的话，从别的副本读到的就是旧内容
        try:
            with stage("分发到节点"):
                self.transport.apply_update(
                    delta_new=pushed.delta,
                    op_delta=op_delta,
                    witness=pushed.witness,
                    blobs={nid: {gidx: ct_new} for nid in holders},
                )
        except TransportError as exc:
            # ★ 与 _append 完全同理（见那边的注释）：一台没跟上，全网就不一致。
            #   而改块比追加**更不能**“重来一次” —— 重来会生成新的块密钥与新的
            #   密文，已经跟上的那几台会当场拒（它们算出的摘要已经是新值）。
            #   而且上面那行 ``key_sink`` 已经把**新**密钥交出去了，
            #   所以“回滚”根本无从谈起 —— 补推是**唯一**的出路。
            self._pending = (
                {"error": exc, **context} if getattr(exc, "payloads", None) else None
            )
            raise

        # 节点都跟上之后才动自己的账 —— 与 upload / append 同一顺序、同一段代码。
        # 反过来的话，「本地记录改了、节点没收到」会让协调者比实际存储更乐观。
        self._pending = None
        self._absorb_pushed(context)
        return iv_new, len(plaintext)

    def append(
        self,
        owner: str,
        file_key: str,
        plaintext: bytes,
        *,
        key_sink: Callable[[int, bytes], None],
    ) -> FileRecord:
        """在文件**末尾追加**一段 —— 只新增块，已有的块一个字节都不动。

        与 :meth:`modify` 是同一条纪律的两种用法：改块只换**一块**、追加只加
        **末尾几块**，两者都不重新切、不重新分发旧块，所以代价都只与
        "动了多少"成正比，与文件本身多大无关。

        :param key_sink: 同 :meth:`upload`，但 ``pos`` 是**块序号**（接着已有的
            往下排，不是从 0 开始）—— 与 :meth:`upload` / :meth:`modify`
            的约定一致，调用方可以照同样的方式按块号逐一封装新密钥。

        .. important::

           **新块按这份文件自己的块大小切**（不是本向量当下的默认值）。
           否则上传后改过默认值，追加进来的块就与老块尺寸不一 ——
           读与验证其实仍然正确（每块的明文长度是逐块记的），
           但 "一份文件 = 一序列等长块 + 一个尾块" 这个形状就被破坏了，
           而不少地方的直觉（包括界面上的预估）都建立在那个形状上。

        .. important::

           **追加不回头填上一块的空位。** 上一块宁可只写半截，也不去补它 ——
           因为"补上一块"本质是一次 :meth:`modify`（要换密钥、要重推密文），
           那样"追加"就变成了"改块 + 追加"，代价与语义都变味了。

           代价是**会留一点空隙**（最后一块不满一整段）。读取时块按序拼接，
           内容一个字节都不会错，所以这只是空间利用率的问题、不是正确性问题。

        .. important::

           **追加之后这个文件的全局下标不再保证是一段连续区间。** 若在两次
           追加之间别的文件也占了位置，本文件的块就会是 ``(0, 1, 4)`` 这样
           带缺口的集合。顺序仍然是块序，所以拼接照旧正确；但任何"用
           ``first``..``last`` 当区间"的展示或推理都会**多算**别人的块。

        .. warning::

           与 :meth:`modify` 同：:attr:`FileRecord.content_digest` **不会**更新
           （协调者手里没有完整明文，算不出新的整体摘要）。

        :raises KeyError: 文件不存在（追加只能追加到已有文件）
        :raises ValueError: 追加内容为空 / 超出 ``n_max``
        """
        if not plaintext:
            raise ValueError("追加空内容没有意义")
        key = (owner, file_key)
        rec = self.files.get(key)
        if rec is None:
            raise KeyError(f"文件 {owner}/{file_key} 不存在（追加只能追加到已有文件）")

        # ★ 这份文件住在哪几台：提前算、提前校（理由同 upload：在 _plan 里才
        #   报错的话，登记表已经分配了，会留下没人持有的脏下标）。
        pool = self._pool_of(rec)
        self._resolve_pool(pool)

        # 新块的块序号接着已有的往下排 —— 块号与全局下标是两回事，
        # 前者是文件内的序号（稳定、不受别的文件影响），后者是向量上的位置。
        #
        # ★ 按**这份文件自己的**块大小切，不是本向量当下的默认值：
        #   否则上传后改过默认值，追加的块就会与老块尺寸不一，
        #   而 “一份文件 = 一序列等长块 + 一个尾块” 这个形状是读/验都默认的前提。
        base_pos = len(rec.indices)
        with stage("切块（split_segments）"):
            segs = split_segments(plaintext, rec.segment_bytes)
        elements: list[int] = []
        cts: list[bytes] = []
        ivs: list[bytes] = []
        lens: list[int] = []
        for t, seg in enumerate(segs):
            with stage("生成密钥与 IV"):
                k_i, iv_i = new_key(), new_iv()
            with stage("逐块加密（SM4）"):
                ct = encrypt_segment(seg, k_i, iv_i)
            key_sink(base_pos + t, k_i)  # ★ 与 upload 同：密钥一生成就交出去
            with stage("算向量分量（SM3）"):
                v_i = vector_element(ct)
            ivs.append(iv_i)
            lens.append(len(seg))
            cts.append(ct)
            elements.append(v_i)

        # ★ 新方案：新块接在**这份文件自己**的位置之后。段末尾接得上就接着长，
        #   接不上就另起一段（见 SegmentRegistry.extend）—— 别人的文件一点不动。
        off, _seg = self.registry.extend(
            owner, file_key, len(elements), n_max=self.session.n_max
        )
        K = tuple(range(off, off + len(elements)))

        # ★ 跟着**这份文件自己已有的机器**走（见 _pool_of）。
        #   否则“我当初只勾了 1、2 号”，一次追加就会把新块漏到没勾的机器上。
        def _extend() -> None:
            """把新块接到这份文件的账目上（正常路径与补推路径共用）。"""
            rec.indices = rec.indices + K
            rec.ivs.extend(ivs)
            rec.plain_lengths.extend(lens)
            rec.total_bytes += len(plaintext)

        self._append(
            elements,
            cts,
            file_id=(owner, file_key),
            K=K,
            pool=pool,
            on_success=_extend,
        )
        return rec

    # -------------------------------------------------------------------
    # 追加的内部实现
    # -------------------------------------------------------------------

    def _append(
        self,
        elements: list[int],
        cts: list[bytes],
        *,
        file_id: tuple[str, str],
        K: tuple[int, ...],
        pool: Sequence[str] | None = None,
        on_success: Callable[[], None] | None = None,
    ) -> None:
        """推进**这份文件**的摘要。

        :param file_id: 这份文件是谁的 ``(owner, file_key)``。新方案下每份文件
            一条向量，所以“推进谁的摘要”必须显式说出来。
        :param K: 这批新块占的**全局位置号**（记账用：``values`` / ``_holder``
            / ``_replicas`` / :meth:`_plan` 都用它）。vds 层要的是同长度的
            **文件内局部下标**，函数内部自己算（见 ``K_local``）——
            两者长得一样但含义不同，混用**不会报错**，只会算出另一个素数的向量。
        """
        """推进全局摘要，并**通知每一台节点各自跟上**。

        :param on_success: 这次追加**自己的收尾**（登记 :class:`FileRecord`、
            把新块接到文件账目上…）。它在**账目推进之后**被调用 ——
            正常路径在下面调，补推路径在 :meth:`retry_pending` 里调。
            抽成回调是为了让"补推成功"与"第一次就成功"走**同一段**收尾代码。
        """
        delta_old = self._delta_before(file_id, K)
        k = len(elements)
        if len(K) != k:
            raise RuntimeError(f"要追加 {k} 块，却给了 {len(K)} 个全局位置")
        K_local = tuple(range(delta_old.n, delta_old.n + k))

        op_delta = UpdateDelta("add", K_local, tuple(elements))
        # ★ 给 vds 层的 δ 必须带**新**的位置段，哪怕 U/C/n 还是旧值：
        #   “第 i 块 ↔ 素数表里第几个”全靠 delta.positions 那张视图，
        #   而这次新增的块（局部号 n..n+k-1）在**旧** δ 里根本不存在 ——
        #   拿旧视图去查会静默查到别的素数，算出的 U' 与 e_K 全错，
        #   最后以“S_K^{e_K} ≠ U”这种看着像“密钥被篡改”的错误收场。
        #   （这里也是**唯一**知道新块全局位置的地方，所以只能在这里预告。）
        delta_pv = Digest(
            U=delta_old.U,
            C=delta_old.C,
            n=delta_old.n,
            offset=delta_old.offset,
            chunks=_chunks_of(tuple(delta_old.positions) + tuple(K)),
        )
        # 走 vds 层已审计过的 PushUpdate 来算新摘要与更新密钥；
        # 推送方只是一个占位（它的新状态与本次追加无关，直接丢掉）。
        with stage("承诺（push_update）"):
            pushed = push_update(
                self.session,
                delta_pv,
                _pusher_placeholder(self.session, delta_pv),
                op_delta,
            )
        delta_new = pushed.delta
        if delta_new.n != delta_old.n + k:
            raise RuntimeError(f"追加后长度不对：{delta_new.n} != {delta_old.n + k}")
        # ★ 位置集合变了（新块接在本文件末尾，可能是**另起一段**），所以要把
        #   新的位置段如实记进摘要 —— 否则 ``delta_new.positions`` 还会是
        #   “从 offset 起连续 n 个”，而节点侧/传输层都靠它做
        #   “全局位置号 <-> 文件内局部块号”的换算，会直接 KeyError。
        #   （这里是**唯一**知道新块全局位置的地方，所以修正也只能在这里做。）
        delta_new = Digest(
            U=delta_new.U,
            C=delta_new.C,
            n=delta_new.n,
            offset=delta_new.offset,
            chunks=_chunks_of(tuple(delta_old.positions) + tuple(K)),
        )
        pushed = PushedUpdate(delta_new, pushed.node, pushed.witness)

        with stage("分片规划（_plan）"):
            assignments, per_index = self._plan(K, pool)
        with stage("打包待推密文"):
            ct_of = dict(zip(K, cts))
            blobs = {
                nid: {j: ct_of[j] for j in seg} for nid, seg in assignments.items() if seg
            }

        # ★ 这份 context 是"这次操作的全部收尾信息"：正常路径立刻用它，
        #   失败时它会连着 error 一起留在 _pending 里，等补推成功再用。
        #   两条路共用同一份 —— 所以不会出现"补推出来的账少一半"。
        context = {
            "file_id": file_id,
            "delta_new": delta_new,
            "per_index": per_index,
            "K": K,
            "elements": elements,
            "on_success": on_success,
        }

        try:
            # 节点自己算新状态；协调者只告诉它们"发生了什么"和"你负责哪些位置"
            with stage("分发到节点"):
                self.transport.apply_append(
                    delta_old=delta_old,
                    delta_new=delta_new,
                    op_delta=op_delta,
                    witness=pushed.witness,
                    assignments=assignments,
                    blobs=blobs,
                )
        except TransportError as exc:
            # ★ 一次追加只要有**一台**没跟上，全网就不一致了：已经跟上的 δ 已经是新的，
            #   没跟上的还是旧的，而**协调者自己不推进**（下面那几行没执行）。
            #   更麻烦的是：登记表**已经分配过**这批下标，所以"再传一次文件"
            #   是错的（会撞上"登记表游标与向量长度不一致"）。
            #   唯一正确的修法是**把同一份更新补推给那几台**。
            #
            #   传输层能补推时（带了 payloads）就把现场留下来，供 retry_pending() 用。
            self._pending = (
                {"error": exc, **context} if getattr(exc, "payloads", None) else None
            )
            raise

        self._pending = None
        self._absorb_pushed(context)

    # -------------------------------------------------------------------
    # 截断（删掉末尾若干块）
    # -------------------------------------------------------------------

    def truncate(
        self, owner: str, file_key: str, drop_blocks: int
    ) -> tuple[tuple[int, ...], FileRecord]:
        r"""删掉文件**末尾**的若干块（论文 §8.2 的 ``op = del``）。

        与 :meth:`append` / :meth:`modify` 是同一族：只动“末尾的那几块”，
        不重切、不重新分发、不重新加密任何保留下来的块。

        :returns: ``(被删掉的下标 K, 更新后的 FileRecord)``

        .. important::

           **只能删“向量末尾”的块** —— 这不是实现偷懒，是方案本身的限制：
           ``vds.updates._push_del`` 要求 :math:`K` 恰好是 ``range(n-k, n)``，
           因为新摘要用到了 :math:`e_{[n]} = e_{[n-k]} \cdot e_K` 这个因式分解。
           "删文件**中间**的块"在数学上就不是一次 ``del``。

           由此推出一条使用上的前提：**只有最后写进向量的那份文件才删得动尾巴**。
           别的文件就算排在末尾，它前面也可能压着别人后写的块 —— 那种情况会报错
           并说清是哪些下标卡住了（而不是默默做错事）。

        .. important::

           删之前**必须先让“与 K 部分相交”的节点把 K 那部分交回去**：
           ``_apply_del`` 要求每个节点与 ``K``「要么全包含、要么全不相交」，
           而我们的分片（轮转 + 副本）几乎必然让某些节点部分相交。
           这是节点侧的 :meth:`RmvStorage <core.node_state.NodeState.drop>`，
           不是把数据挪个地方 —— 那些块本来就要被删掉。

        :raises KeyError: 文件不存在
        :raises ValueError: 块数不合法、或末尾那几块不属于这份文件
        """
        key = (owner, file_key)
        rec = self.files.get(key)
        if rec is None:
            raise KeyError(f"文件 {owner}/{file_key} 不存在（截断只能作用于已有文件）")
        try:
            drop = int(drop_blocks)
        except (TypeError, ValueError):
            raise ValueError(f"要删的块数必须是整数，收到 {drop_blocks!r}") from None
        if drop <= 0:
            raise ValueError(f"要删的块数必须为正，收到 {drop}")
        if drop >= rec.block_count:
            raise ValueError(
                f"这份文件只有 {rec.block_count} 块，要删 {drop} 块 —— "
                f"不能删到一块不剩：向量不允许缩到 n=0（那会让全局摘要回到"
                f"起点，与 bootstrap 的约定不符）。整份删除是另一件事"
            )
        K = tuple(rec.indices[-drop:])
        mine = rec.indices[-drop:]
        if mine != K:
            blocked = sorted(set(K) - set(mine))
            raise ValueError(
                f"只能删「当前向量末尾」的块，而末尾 {list(K)} 里有下标 {blocked} "
                f"不属于这份文件（{owner}/{file_key} 的最后 {drop} 块是 {list(mine)}）"
                f"—— 它们后面还压着之后写的块。要删这份文件的尾巴，先清掉那几块，"
                f"或者重新上传一份"
            )
        # ★ 协议开动**之前**先问清"账退得回去吗"：收账（_settle）跑在向量已经缩短、
        #   节点已经交回份额**之后** —— 到那一步才发现退不掉，就成了一次
        #   「向量短了、登记表没退」的说不清账的删除。合同一份文件那条路
        #   （free_tail）的校验全在这里做。
        # 新方案里这一步不需要预检：K 取自**这份文件自己**的末尾，
        # 一定退得回去 —— 位置段永不回收，所以不存在“跨文件退账”这回事。

        def _settle() -> None:
            """这次删除**自己的收尾**（正常路径与补推路径共用）。

            两件事：退回末尾下标（登记表游标要与向量长度一起回落）、
            把文件账目截短。
            """
            self.registry.drop_tail(owner, file_key, drop)
            rec.indices = rec.indices[:-drop]
            rec.ivs = rec.ivs[:-drop]
            rec.plain_lengths = rec.plain_lengths[:-drop]
            rec.total_bytes = sum(rec.plain_lengths)
            # 删掉的块如果有块密钥留在实现里，也要跟着抹掉（本类没有，子类有）
            self._forget_block_keys(owner, file_key, rec.block_count)

        self._delete_tail(key, K, _settle)
        return K, rec

    def _delete_tail(
        self,
        file_id: tuple[str, str],
        K: tuple[int, ...],
        settle: Callable[[], None],
    ) -> None:
        r"""把**向量末尾**的连续区间 ``K`` 删掉（论文 §8.2 的 ``op = del``）。

        :func:`truncate` 与 :meth:`delete_from` 共用它 —— 这条协议路径**只能有
        一份定义**（各写一遍，迟早会出现"一条路把账收干净、另一条漏了一半"，
        而那属于最难查的一类偏差）。

        :param settle: 这次删除**自己的收尾**（退回登记表游标、把文件账目截短……）。
            它在**账目推进之后**被调；正常路径与补推路径共用同一份
            （理由同 :meth:`_absorb_pushed` 的 ``on_success``）。
        """
        drop = len(K)
        delta_old = self.delta_of(*file_id)
        n_local = delta_old.n
        tail_global = tuple(self.registry.positions_of(*file_id)[-drop:])
        if not K or tuple(K) != tail_global:
            raise ValueError(
                f"只能删这份文件「末尾」的连续区间：收到 {list(K)}，"
                f"而 {file_id[0]}/{file_id[1]} 的末尾 {drop} 块是 {list(tail_global)}"
            )
        # vds 层只认**文件内局部块号**（它假定下标密集 0..n-1）
        K_local = tuple(range(n_local - drop, n_local))

        # ① 先让“与 K 部分相交”的节点交回 K 那部分
        #    （整段持有 K 的节点不动它 —— 底层 del 走“K ⊆ I”那条更便宜的路：
        #      只丢下标，(S_I, Λ_I) 一个字不改）
        drops = self._plan_drops(K)
        if drops:
            self.transport.drop(int(delta_old.offset), drops)

        # ② 协调者自己发起 del（它手里有整个向量，不依赖任何一台正好持有整个尾巴）
        delta_old = self.delta_of(*file_id)
        op_delta = UpdateDelta("del", K_local, ())
        old_values = {i: self.values[g] for i, g in zip(K_local, K)}
        pushed = push_update(
            self.session,
            delta_old,
            _full_holder(self.session, delta_old, self._vals_of(file_id)),
            op_delta,
            old_values,
        )
        if pushed.delta.n != n_local - drop:
            raise RuntimeError(
                f"删除后长度不对：{pushed.delta.n} != {n_local - drop}"
                f"（这是实现自检，不该发生）"
            )
        # ★ 位置集合也变了（末尾 drop 块没了）—— 同样要把新的位置段记进摘要。
        keep = tuple(self.registry.positions_of(*file_id))[: n_local - drop]
        delta_fixed = Digest(
            U=pushed.delta.U,
            C=pushed.delta.C,
            n=pushed.delta.n,
            offset=pushed.delta.offset,
            chunks=_chunks_of(keep),
        )
        pushed = PushedUpdate(delta_fixed, pushed.node, pushed.witness)

        context = {
            "kind": "del",
            "file_id": file_id,
            "delta_new": pushed.delta,
            "K": K,
            "per_index": {},
            "elements": (),
            "on_success": settle,
        }

        try:
            self.transport.apply_delete(
                delta_new=pushed.delta, op_delta=op_delta, witness=pushed.witness
            )
        except TransportError as exc:
            # 与 _append / modify 同一套：一台没跟上就留现场，等补推
            # （**不能**靠“重来一次”：交回过的节点再收到同一批 del 会被
            #   摘要闸拒掉 —— 已经跟上的那几台 δ 已经是新的了）
            self._pending = (
                {"error": exc, **context} if getattr(exc, "payloads", None) else None
            )
            raise

        self._pending = None
        self._absorb_deleted(context)

    def delete_from(
        self, owner: str, file_key: str
    ) -> tuple[tuple[int, ...], tuple[tuple[str, str], ...]]:
        r"""把这份文件**以及它之后写进向量的所有块**一起删掉。

        为什么只能是"从这里删到末尾"：``del`` 只支持向量末尾的连续区间
        （理由见 :meth:`truncate`）。而各文件在向量上占的是**连续区间、
        顺序就是上传顺序**（见 :meth:`~core.registry.Registry.alloc_file`），
        所以"从这份文件的第一块删到向量末尾"**恰好是一次合法的 ``del``**。

        ★ 代价必须说清楚：被连带删掉的**只有它后面**的文件。想"只删中间某份
        文件、把后面的块整体前移"是不行的 —— 那要重编号、重新承诺整条向量，
        论文里没有这个操作（见 :meth:`truncate` 的说明）。

        :returns: ``(被删掉的末尾区间 K, 被删掉的文件键列表)``
        """
        key = (owner, file_key)
        rec = self.files.get(key)
        if rec is None:
            raise KeyError(f"文件 {owner}/{file_key} 不存在")
        if not rec.indices:
            raise KeyError(f"文件 {owner}/{file_key} 没有任何块")
        # ★ 新方案：每份文件的位置互不重叠、也不共用段，所以“删一份文件”
        #   就是**只删它自己** —— 不再需要连带删掉排在它后面的文件
        #   （那是“全系统一条向量”才有的束缚）。
        K = tuple(rec.indices)
        doomed = (key,)
        # （新方案下不需要“区间 vs 账目覆盖”的交叉校验：K 就是这份文件的全部位置）
        # ★ 协议开动**之前**先把"账退得回去吗"问清楚。
        #   收账（_settle）是在向量已经缩短、节点已经交回份额**之后**才跑的；
        #   如果到那一步才发现退不掉，就留下一次说不清账的删除。这里预检一次，
        #   让所有"退不掉"的情况在动手之前就变成异常。
        # 新方案下不需要预检：K 取自这份文件自己，一定退得回去。

        def _settle() -> None:
            """退回末尾下标 + 把这几份文件的账目整个摘掉。"""
            # 跨多份文件 —— 必须用 free_tail_multi（free_tail 只认单份文件）
            for k in doomed:
                self.registry.forget(k[0], k[1])
                self.files.pop(k, None)
                self.deltas.pop(k, None)
                self._forget_block_keys(k[0], k[1], 0)

        self._delete_tail(key, K, _settle)
        return K, doomed

    def rename_owner(self, owner: str, new_owner: str) -> int:
        """把 ``owner`` 名下所有文件与块改挂到 ``new_owner``（删号专用）。

        只动"这是谁的东西"：**一个下标都不动，一个代数运算都不做** ——
        所以向量、承诺、节点手里的份额、摘要 ``δ`` 全都原样有效，
        这些块照样能被任何人验证（"验证不受限"不受影响）。

        为什么要改名而不是删掉：见 :meth:`core.registry.BlockRegistry.rename_owner`
        —— 登记表只支持从**末尾**截断，压在中间的那些块物理删不掉。
        而改名之后新账号与它们不再同名，界面上的"可解密"判据就**如实**了。

        ★ 内存与库必须一起改（库那半边由调用方负责）：只改一边的话，
          下次重启 ``manager._reload`` 会重建出另一套身份，两边分叉，
          而分叉的表现是"重启之后文件列表变了"。

        :returns: 被改挂的文件数。``owner`` 名下什么都没有时返回 ``0``。
        :raises ValueError: 新旧同名、新名为空，或改名会与已有文件撞键。
        """
        if not new_owner:
            raise ValueError("新 owner 不能为空")
        if new_owner == owner:
            raise ValueError(f"新旧 owner 同名（{owner!r}），没有可改的东西")

        mine = sorted((k for k in self.files if k[0] == owner), key=lambda k: k[1])
        for _, file_key in mine:
            if (new_owner, file_key) in self.files:
                raise ValueError(
                    f"改名会撞键：{new_owner}/{file_key} 已经有一份文件了"
                )

        # 登记表先改：它有冲突检查，抛了就不会走到下面改 files 那一步
        self.registry.rename_owner(owner, new_owner)
        for _, file_key in mine:
            rec = self.files.pop((owner, file_key))
            rec.owner = new_owner
            self.files[(new_owner, file_key)] = rec
            # ★★ 摘要在**另一张表**里（``self.deltas`` 的键同样是
            #   ``(owner, file_key)``）。漏掉它不是一个"少改一行"的小事（审计 S1）：
            #
            #   * 运行期：库里已改名、内存没改 ⇒ ``delta_of(墓碑名)`` 直接 KeyError，
            #     而 ``/api/files`` 是**逐行序列化**的 ⇒ 任一份墓碑文件就能让
            #     **所有用户**的文件列表 500；
            #   * 重启后：``_reload`` 从 ``blocks``（墓碑名）与 ``file_deltas``（旧名）
            #     重建出**两套身份** —— 库自己就不一致，所以**不自愈**。
            #
            #   子类（``PlainKeyStore``）的块密钥表由它自己的 ``rename_owner``
            #   处理（它只 ``super()`` 一次），所以这里改的正是父类该管的那部分。
            if (owner, file_key) in self.deltas:
                self.deltas[(new_owner, file_key)] = self.deltas.pop(
                    (owner, file_key)
                )
        return len(mine)

    def _forget_block_keys(self, owner: str, file_key: str, keep_pos: int) -> None:
        """钩子：删块之后丢弃 ``块序号 >= keep_pos`` 的块密钥。

        本类**不保管任何密钥**（密钥一生成就交给 ``key_sink``），所以这里是空实现。
        真正的用处是子类：:class:`PlainKeyStore` 把密钥存在内存里（仅演示与测试），
        删块时必须跟着抹掉 —— "已经删掉的数据还留着一把钥匙"是删除语义的反面。

        ★ 提成钩子（而不是在 :class:`PlainKeyStore` 里覆写 ``truncate``）是为了让
        **补推路径**也走到它：截断的落库/收尾只有一份（``_settle``），
        覆写公有方法只能覆盖"第一次就成功"那一次。
        """
        return None

    def _plan_drops(self, K: Sequence[int]) -> dict[str, tuple[int, ...]]:
        """算出「哪些节点必须先交出哪些下标」—— 只点名**部分相交**的那些。

        判据用协调者自己的副本表（``replicas_of``），不去再问一遍节点：那张表
        是 :meth:`check` 逐块对过账的，而且写路径全程持锁、中间没人能改它。

        :returns: ``{节点: 要交回的下标}``（整段持有者与无关节点都不在里面）
        """
        K_set = set(K)
        held: dict[str, list[int]] = {nid: [] for nid in self.node_ids}
        for j in K:
            for nid in self.replicas_of(j):
                held[nid].append(j)
        out: dict[str, tuple[int, ...]] = {}
        for nid, got in held.items():
            if not got or set(got) == K_set:
                continue
            out[nid] = tuple(sorted(got))
        return out

    def _absorb_pushed(self, p: Mapping) -> None:
        r"""**写推成功之后**统一的那一套账 —— 一次成功与补推成功必须逐字一致。

        两处都调它，是为了让"补推成功"与"第一次就成功"产生**完全相同**的结果；
        各写一遍的话，迟早会出现"补推之后账目少了一半"这种只在故障恢复路径上
        才看得见的偏差。

        ``p`` 就是 :meth:`_append` / :meth:`modify` 留下的那份 context：

        * ``delta_new`` —— 推进全局摘要；
        * ``per_index`` —— 每个新下标的**全部**持有者（副本）；
        * ``K`` / ``elements`` —— 新分量；
        * ``on_success`` —— 这次操作**自己的收尾**（登记 FileRecord /
          把新 IV 写回文件账目…）。它是一个闭包，因为它要碰的字段
          各操作不一样，而"什么时候调"必须由这里统一决定。

        .. important::

           ``on_success`` 必须在**账目推进之后**调 —— 它读的就是刚推进的
           那些账（比如 :meth:`upload` 的收尾要读 ``self.values``）。
        """
        self.deltas[p["file_id"]] = p["delta_new"]
        for j, holders in p["per_index"].items():
            self._replicas[j] = holders
            self._holder[j] = holders[0]
        for j, v in zip(p["K"], p["elements"]):
            self.values[j] = v
        done = p.get("on_success")
        if done is not None:
            done()

    def _absorb_deleted(self, p: Mapping) -> None:
        r"""**删除推成功后**的那一套账 —— 与 :meth:`_absorb_pushed` 并列。

        与“追加/改块”的差别只有一处方向：那边是把 ``K`` 上的分量**加进来**，
        这边是把它们连同 ``_holder`` / ``_replicas`` 里的名字一起**抹掉**。

        ``on_success`` 的调用时机与理由与那边完全一致（**账目推进之后**才调，
        它读的就是刚推进的那些账）—— 所以“第一次就成功”与“补推成功”
        逐字产生同一结果这件事，在这里同样成立。
        """
        self.deltas[p["file_id"]] = p["delta_new"]
        for j in p["K"]:
            self.values.pop(j, None)
            self._holder.pop(j, None)
            self._replicas.pop(j, None)
        done = p.get("on_success")
        if done is not None:
            done()

    def retry_pending(self) -> dict:
        """把上次没推成功的更新**补推给失败的那几台**。

        ★ 为什么必须要它（而不是"再传一次文件"）：

        * 已经跟上的节点 δ 已经是新的，没跟上的还是旧的 —— 全网不一致；
        * 协调者自己的 δ **没有**推进，但**登记表已经分配过**这批下标了 ——
          再传一次文件会拿到新下标，而且会撞上
          "登记表游标与向量长度不一致"。

        所以唯一正确的出路就是**补推同一份负载**（负载在
        :class:`~core.transport.WriteError` 里带着），全部补上之后才推进自己的账。

        ★ 它**不只是把向量对上**：补成功之后还会调用那次操作自己的收尾回调
        （``on_success``）—— 比如上传要把 :class:`FileRecord` 登记进
        :attr:`files`。少了这一步，向量是自洽的，但那份文件在系统里
        "不存在"，重启重建时还会以"节点与协调者不同步"炸出来。

        :returns: ``{"ok": bool, "nodes": [仍失败的节点], "op": str, "detail": str}``

            ``ok=False`` **不是**“没救” —— 它表示“补了，但还有几台没通”，
            修好那几台再点一次就行（已经补上的不会再被打扰）。
        """
        retry = getattr(self.transport, "retry_write", None)
        if self._pending is None:
            return {"ok": True, "nodes": [], "op": "", "detail": "没有待补推的更新"}
        if retry is None:  # pragma: no cover - 单进程模式的本地传输不走这条路
            return {
                "ok": False,
                "nodes": [],
                "op": self._pending["error"].op,
                "detail": f"{type(self.transport).__name__} 不支持补推",
            }

        p = self._pending
        still = retry(p["error"])
        if still:
            return {"ok": False, "nodes": [str(n) for n in still], "op": p["error"].op}

        op = p["error"].op
        # 与成功路径**逐字一致**（同一个方法）—— 删除走删除那一套：
        # 它除了推进 δ，还要把 K 上的分量与持有者记录抹掉。
        if p.get("kind") == "del":
            self._absorb_deleted(p)
        else:
            self._absorb_pushed(p)
        self._pending = None
        return {"ok": True, "nodes": [], "op": op, "detail": "已补齐"}

    def pending_write(self) -> dict | None:
        """有没有"推失败了、还没补"的更新（界面用它决定要不要报警）。"""
        if self._pending is None:
            return None
        exc = self._pending["error"]
        return {
            "op": exc.op,
            "nodes": list(exc.nodes),
            "failures": list(exc.failures),
            "blocks": len(self._pending["K"]),
        }

    def _plan(
        self, K: tuple[int, ...], pool: Sequence[str] | None = None
    ) -> tuple[dict[str, tuple[int, ...]], dict[int, tuple[str, ...]]]:
        r"""把新区间摊给各台服务器 —— **带偏移的轮转**，并把副本也定下来。

        为什么是轮转而不是连续切块：连续切块下，一个 2 块的短文件永远落在
        前两台，于是短文件一多就会出现"后几台常年空闲"。轮转 + 跨次上传
        递推偏移能让任意块数都摊得匀：

        * 每次上传 4 块、4 台 → 每台 1 块；
        * 连着两次各传 2 块 → 第一次落第 1/2 台，第二次落第 3/4 台。

        偏移量按本次块数递推，所以连续多次小上传也能覆盖到所有服务器。

        副本在**打乱后的环**上从主副本的位置往后连续取（而不是另抽），这样：

        * ``replica_factor = 1`` 时行为与“没有副本”的实现一致；
        * “哪几台合起来能凑齐全部块”仍然均匀 —— 副本也参与轮转；
        * 而环**每次上传都重新打乱**、每块还再抽一个随机起点 ⇒ “主 i、副本 i+1”
          这种固定配对和周期性条纹都不再出现（每块都是独立随机的两/三台）。

        :param pool: 只在哪几台里轮转。``None`` = 全部机器。就是“手动指定分发”。

            .. important::

               **没参与的机器也要被通知到。** 本函数返回的 ``assignments``
               仍然覆盖**所有**节点（没分到的是空元组）—— 因为 `_append`
               要靠它把“又长了几块”告诉每一台，包括这次一块都没拿到的。
               漏掉通知的话，那些节点的 delta 不前进，之后验证就会和
               没参与的那些台对不上（这是一个**很难查**的故障）。

        :returns: ``(每台负责的下标, 每个下标的副本列表)``。
            “谁持有哪块”只有**这一处**定义 —— 协调者记账、推数据、自检
            全用它，所以不可能出现两份定义不一致的情况。
        """
        nodes = self._resolve_pool(pool)
        m = len(nodes)
        # ★ 主/副本的**配对**要随机：原来固定是"主 i、副本 i+1"，在块矩阵上
        #   一眼就看得出规律（用户明确要求"随机一点"）。做法是把整圈机器
        #   **打乱一次**再照原来的偏移走：
        #
        #   * 每块的不同副本仍然落在**不同的**机器上（那条硬性前提没动）；
        #   * 一次上传仍然走完整圈 ⇒ "任意块数都摊得匀"也没丢；
        #   * 但谁是主、谁是副本每次都不同，看不出固定搭配。
        #
        #   打乱只影响"分到哪台" —— 摘要、证据、验证与谁存哪块无关
        #   （见 :meth:`upload` 的 ``nodes`` 参数说明）。
        ring = list(nodes)
        random.shuffle(ring)
        per_index: dict[int, tuple[str, ...]] = {}
        # ★ 桶覆盖全部节点（含没参与的）；轮转只在 nodes 里发生。
        buckets: dict[str, list[int]] = {nid: [] for nid in self.node_ids}
        for t, j in enumerate(K):
            # 两级随机：① 环已打乱（改变"谁和谁搭伴"）；② 每块再抽一个随机
            # 起点（打散"每隔几块就重复一次"的周期性条纹）。
            base = (self._offset + t + random.randrange(m)) % m
            holders = tuple(
                ring[(base + c) % m] for c in range(self.replica_factor)
            )
            per_index[j] = holders
            for nid in holders:
                buckets[nid].append(j)
        self._offset = (self._offset + len(K)) % m
        return {nid: tuple(v) for nid, v in buckets.items()}, per_index

    def _resolve_pool(self, pool: Sequence[str] | None) -> tuple[str, ...]:
        """校验“这次往哪儿摊”，并归一化成元组。``None`` → 全部机器。"""
        if pool is None:
            return self.node_ids
        got = tuple(pool)
        if not got:
            raise ValueError("参与分发的服务器不能为空（不勾任何一台就没地方存了）")
        if len(set(got)) != len(got):
            raise ValueError(f"参与分发的服务器不能重复：{list(got)}")
        known = self.node_ids
        unknown = [n for n in got if n not in known]
        if unknown:
            raise ValueError(
                f"这些服务器不在本集群里：{', '.join(unknown)}"
                f"（本集群是 {', '.join(known)}）"
            )
        if self.replica_factor > len(got):
            raise ValueError(
                f"每块要存 {self.replica_factor} 份，但只勾了 {len(got)} 台机器 —— "
                f"副本必须落在不同的机器上，否则掉一台就一起没了。"
                f"请多勾几台，或把副本数调小"
            )
        return got

    def _pool_of(self, rec: FileRecord) -> tuple[str, ...] | None:
        """这份文件**现在**住在哪几台机器上 —— 追加时接着住那儿。

        ★ 不给 :class:`FileRecord` 加字段（那要改库表、要迁移），而是从
        “各块的持有者”**推**出来：它就是事实本身。重启后 ``_holder`` /
        ``_replicas`` 由协调者库重建（见 ``backend/manager.py::_reload``），
        所以推得与重启前一样。

        台数变少时这里会自动滤掉已经不在集群里的机器；滤光了就返回
        ``None``（退回全部机器）—— 不让追加因为一个恢复不了的历史状态而卡死。
        """
        used = {n for j in rec.indices for n in self._replicas.get(j, ())}
        pool = tuple(n for n in self.node_ids if n in used)
        return pool or None

    # -------------------------------------------------------------------
    # 检索 —— 可以横跨多个文件
    # -------------------------------------------------------------------

    def query(self, indices, *, allow_partial: bool = False) -> QueryResult:
        """向服务器索取若干下标的**内容 + 一份聚合证据**，并验证。

        ``indices`` 是**全局下标**，天然可以横跨任意多个文件、任意多个用户 ——
        这是设计 B 相对"一文件一向量"的核心差别，不需要任何额外机制。

        :param allow_partial: 有下标**没有任何在线副本**时怎么办。

            * ``False``（默认，读路径 A）：直接报错，并在报错里**列清楚是哪几个
              下标、以及各台各持有多少块** —— 报错要能直接指向原因；
            * ``True``（读路径 B，“允许部分结果”）：把拿得到的那些块照常聚合验证，
              同时把缺的下标放进 :attr:`QueryResult.missing`，**绝不假装收齐了**。

            .. important::

               全缺时不返回“空的部分结果”，而是照样报错 —— “一个都没拿到”
               与“拿到一部分”是两回事，前者没有可验的东西。
        """
        Q = as_index_set(indices)
        if not Q:
            raise ValueError("查询下标集合不能为空")

        groups = self._group_by_file(Q)
        cts_by_pos, holders, used, missing = self._fetch(
            groups, allow_partial=allow_partial
        )

        # 允许部分结果时，下面的聚合与验证只针对**真的拿到了**的那些块 ——
        # 把缺的也塞进 F_Q / 证据里就是伪造（那样验出来的“通过”毫无意义）。
        if missing:
            Q = tuple(i for i in Q if i not in missing)

        # 证据的生成与验证放到 :meth:`_prove`：单文件走“节点凭证聚合”，
        # 跨文件走“合并位置集重算”（下面各算完 valmap / F_Q 再调）。

        # 各凭证携带的值已经是**本地从密文重算**出来的（见 _collect），
        # 所以这里的 F_Q 不是"节点声称的那份"，而是"由交付的字节推出来的那份"。
        valmap: dict[int, int] = {
            g: vector_element(ct) for g, ct in cts_by_pos.items()
        }
        F_Q = tuple(valmap[g] for g in Q)

        report, pi_K = self._prove(groups, Q, valmap)

        return QueryResult(
            indices=Q,
            values=F_Q,
            proof=pi_K,
            report=report,
            holders=dict(holders),
            refs=[self.describe(g) for g in Q],
            cert_count=len(used),
            node_used=tuple(used),
            missing=tuple(sorted(missing)),
        )

    def query_files(self, targets, *, allow_partial: bool = False) -> QueryResult:
        """一次查询**若干个文件**的全部块 —— 需求里"多人的多个文件"那条。

        :param targets: ``[(owner, file_key), ...]``
        :param allow_partial: 同 :meth:`query` —— 有块收不齐时是报错还是
            只把拿得到的算进结论（缺的列在 ``missing`` 里）。
        """
        idx: list[int] = []
        for owner, file_key in targets:
            got = self.file_indices(owner, file_key)
            if not got:
                raise KeyError(f"文件 {owner}/{file_key} 不存在")
            idx.extend(got)
        return self.query(sorted(idx), allow_partial=allow_partial)

    def cipher_of(self, owner: str, file_key: str, indices=None) -> dict:
        """取回**密文**（不解密）—— 供客户端自己解密、自己验证。

        ★ 与 :meth:`read` 的分工是这条路的全部意义：

        * :meth:`read` 在**服务端**把密文解开（``key_of`` 拿钥匙 → SM4），
          于是"服务器返回给我的东西"本身无法被客户端独立检验；
        * ``cipher_of`` **一个字节都不解** —— 它只把节点上的密文段、IV、
          明文长度，以及一份覆盖这些位置的证据取回来。块密钥的解封
          （SM2 ECDH + KDF）与内容解密（SM4）都留给调用方，
          服务端**不掌握任何秘密**。

        交付物全部可被客户端独立校验：密文 → 分量（自己 ``SM3``）→
        承诺（对着**自己保存的** δ 跑 :func:`svc.verify`）。所以这里的
        返回里刻意只有"材料"，没有"结论"。

        :param indices: **文件内的块序号**（0 基，与前端"第几块"一致）。
            ``None`` 表示整份文件。
        :raises KeyError: 文件不存在（上层转 404）。
        :raises IndexError: 块号越界（上层转 400）。
        """
        fid = (owner, file_key)
        rec = self.files.get(fid)
        if rec is None:
            raise KeyError(f"文件 {owner}/{file_key} 不存在")
        delta = self.delta_of(owner, file_key)
        pos_all = tuple(self.registry.positions_of(owner, file_key))
        want_local = (
            tuple(range(delta.n)) if indices is None else as_index_set(indices)
        )
        for i in want_local:
            if not 0 <= i < delta.n:
                raise IndexError(
                    f"块号 {i} 越界：{owner}/{file_key} 只有 {delta.n} 块"
                )
        want = tuple(pos_all[i] for i in want_local)  # 对应的全局位置

        groups = self._group_by_file(want)
        cts, holders, used, _missing = self._fetch(groups)
        # ★ 分量在这个进程里也是"从密文算"的 —— 与客户端要做的完全一致，
        #   所以这里的 ``element`` 只是给界面做对照，不是判定依据。
        valmap = {g: vector_element(ct) for g, ct in cts.items()}
        report, pi_I = self._prove(groups, want, valmap)

        return {
            "owner": owner,
            "file_key": file_key,
            "offset": int(delta.offset),
            "n": int(delta.n),
            "segment_bytes": int(rec.segment_bytes),
            "blocks": [
                {
                    "block_idx": loc,
                    "global_index": pos_all[loc],
                    "ciphertext_hex": cts[pos_all[loc]].hex(),
                    "iv_hex": rec.ivs[loc].hex(),
                    "plain_len": int(rec.plain_lengths[loc]),
                    "element": str(valmap[pos_all[loc]]),
                    "holder": holders.get(pos_all[loc], ""),
                }
                for loc in want_local
            ],
            "proof": {
                "S_I": str(pi_I.S_I),
                "Lambda_I": str(pi_I.Lambda_I),
                "I": list(pi_I.I),
            },
            "verify": {
                "ok": bool(report.ok),
                "code": int(report.code),
                "message": report.message,
            },
            "nodes_used": list(used),
        }

    def collect_certificates(self, indices):
        """向服务器收齐覆盖 ``indices`` 的凭证，**不做聚合**。

        单独暴露出来是为了让调用方能分别计量"证明生成"与"聚合"两段。
        返回 ``(凭证列表, 下标→节点, 参与节点, 下标→密文)``。

        ★ 它**不支持部分结果**：要的就是"收齐"。收不齐时 ``_collect`` 报错
        （报错里会列清楚缺哪几个下标），这里只把 5 元组里的 ``missing`` 丢掉。
        """
        Q = as_index_set(indices)
        if not Q:
            raise ValueError("下标集合不能为空")
        if Q[-1] >= self.n:
            raise ValueError(f"下标 {Q[-1]} 超出当前向量长度 {self.delta.n}")
        groups = self._group_by_file(Q)
        if len(groups) != 1:
            # 凭证聚合（agg）要求各凭证的下标两两不交、而且属于**同一份文件**
            # （每份文件的 E 不同，份额没法跨文件混合）。跨文件请走 query()。
            raise ValueError(
                f"凭证聚合要求这些位置属于「同一份文件」，实际跨了 {len(groups)} 份；"
                f"跨文件请用 query() —— 它会在合并位置集上重算一份证据"
            )
        fid = next(iter(groups))
        pos = self.registry.positions_of(*fid)
        certs = self._certs_for(fid, Q)
        # ★ Certificate 的字段叫 ``source``（“这份凭证是哪台节点给的”）。
        holders = {int(pos[li]): c.source for c in certs for li in c.Q}
        used = [c.source for c in certs]
        #: ★ 新架构下凭证本身**不含密文**（密文只走 query / read 那条路），
        #:   所以第四项保留为兼容占位，不再回填。
        cts: dict[int, bytes] = {}
        return certs, holders, used, cts

    def _fetch(self, groups, *, allow_partial: bool = False):
        """按文件分组，把要检索的那些块的**密文**取回来。

        与 :meth:`_collect` 的差别：这里**不产生凭证**（凭证只在单文件那条路上
        用得到），而且知道“块属于哪份文件” —— 节点侧是按文件分视图的，
        所以问它的时候必须说清是哪一份，并且用**文件内局部块号**。

        :returns: ``(全局位置号 -> 密文, 全局位置号 -> 主副本, 用到的节点, 缺的位置)``
        """
        with stage("探测各节点（report）"):
            report = {row["node_id"]: row for row in self.transport.report()}
        held: dict[int, dict[str, set[int]]] = {}
        for nid, row in report.items():
            for v in row.get("vectors", ()):
                held.setdefault(int(v["offset"]), {})[nid] = set(v["indices"])

        cts: dict[int, bytes] = {}
        holders: dict[int, str] = {}
        used: list[str] = []
        remaining: set[int] = set()
        for file_id, gs in groups.items():
            delta = self.delta_of(*file_id)
            off = int(delta.offset)
            local_of = {g: i for i, g in enumerate(delta.positions)}
            want = sorted(local_of[g] for g in gs)
            for nid in self.node_ids:
                mine = held.get(off, {}).get(nid)
                if not mine:
                    continue
                take = sorted(set(want) & mine)
                if not take:
                    continue
                with stage("取回密文"):
                    _pi, ct_part = self.transport.retrieve(nid, off, take)
                for li, ct in zip(take, ct_part):
                    g = delta.positions[li]
                    cts[g] = ct
                    holders[g] = nid
                used.append(nid)
            for g in gs:
                if g not in cts:
                    remaining.add(g)

        if remaining:
            if allow_partial and cts:
                return cts, holders, used, remaining
            why = "、".join(
                f"{nid} 持 {row.get('held', 0)} 块"
                + ("（联系不上）" if row.get("unreachable") else "")
                for nid, row in report.items()
            )
            raise ValueError(
                f"没有服务器覆盖位置 {sorted(remaining)}（各台现状：{why}）—— "
                f"这些块的每一份副本都不在线，或者已经被删除。"
            )
        return cts, holders, used, set()

    def _certs_for(self, file_id, gs):
        """向持有者收**同一份文件**内这些块的凭证（供 ``agg`` 聚合）。

        ★ ``agg`` 要求各凭证的下标集合**两两不相交**，所以这里用 ``remaining``
        逐个“认领”：一个块只交给**第一个**答得上来的节点，后面的节点就跳过它。
        开了副本之后同一个块会有多台持有 —— 不做这一步，两份 ``I = {i}`` 的
        凭证会让 ``agg`` 当场报“I ∩ J ≠ ∅”，而且报错里看不出是谁的错。
        """
        delta = self.delta_of(*file_id)
        off = int(delta.offset)
        local_of = {g: i for i, g in enumerate(delta.positions)}
        remaining = {local_of[g] for g in gs}
        certs: list[Certificate] = []
        with stage("探测各节点（report）"):
            report = {row["node_id"]: row for row in self.transport.report()}
        for nid, row in report.items():
            if not remaining:
                break
            mine = None
            for v in row.get("vectors", ()):
                if int(v["offset"]) == off:
                    mine = set(v["indices"])
                    break
            if not mine:
                continue
            take = sorted(remaining & mine)
            if not take:
                continue
            with stage("取回分量与凭证"):
                pi_part, ct_part = self.transport.retrieve(nid, off, take)
            # ★ 值不采信对方声称的那份，自己从回来的密文段重算
            F_derived = tuple(vector_element(ct) for ct in ct_part)
            certs.append(Certificate(tuple(take), F_derived, pi_part, nid))
            remaining -= set(take)
        if remaining:
            raise ValueError(
                f"没有服务器覆盖位置 {sorted(delta.positions[i] for i in remaining)}"
                f" —— 这些块的每一份副本都不在线，或者已经被删除。"
            )
        return certs

    def _prove(self, groups, Q, valmap):
        """生成证据并验证。

        * **同一份文件**：把各节点的凭证聚合起来（走论文 §6.5.2 的 ``VC.Agg``
          / ``AggManyToOne``）—— 节点参与了证明，快，而且**不需要全量值**；
        * **跨多份文件**：在合并位置集上重算一份证据（慢，秒级）。

        跨文件为什么不能像单文件那样把现成凭证 **Agg** 起来？不是缺一个算法，
        而是论文的聚合**以同一个承诺 C 为前提**：

        * 学位论文 ``Def. 29``（Aggregatable Subvector Openings）原文：
          「any commitment C and triple (I, v_I, π_I) s.t. Ver(pp, C, I, v_I, π_I) = 1 …
          for any (J, v_J, π_J) such that Ver(pp, **C**, J, v_J, π_J) = 1 …」
          —— 两份凭证必须验在**同一个 C** 上，而一文件一向量 ⇒ 每份文件各有 ``C_f``
          与 ``U_f = g^{E_f}``。
        * 把 ``S`` 抬到共同宇宙是可行的：``(S_I)^{E/E_f} = g^{E/e_I} = U'^{1/e_I}``；
          但 ``Λ`` 抬不动 —— ``Λ_I`` 的支撑集是**本文件的补集**
          （``Λ_I = (∏ S_j^{v_j})^{1/e_I}``，连乘只过本文件那些位置），
          它不含另一份文件的任何块。于是 ``Agg`` 里「用 ∏φ_j^{v_j} 抵消交叉项」
          那一步**没有东西可抵消**，差额恰是「对方文件那些块的 1/e_j 次方」，
          隐藏阶群里求不出来（``svc/groups.generate_primes`` 明确丢弃 φ(N)）。
        * 要补齐那一半，只能**知道合并集里所有块的值**、在合并向量上重开
          （见 :meth:`_prove_merged`）。因此这条路的代价是
          O(Σ_f |f|) 次大指数模幂，**与你要验几块无关** —— 只跟涉及文件的总位置数有关。

        实测拆解与曲线见 ``docs/开发与验收记录.md`` 第四节。
        """
        if len(groups) == 1:
            file_id = next(iter(groups))
            delta = self.delta_of(*file_id)
            certs = self._certs_for(file_id, groups[file_id])
            client = ClientNode(self.session, delta)
            with stage("聚合凭证"):
                pi = client.aggregate_certificates(certs)
            Q_local = self._local_of(file_id, Q)
            with stage("承诺验证（VerRetrieve）"):
                rep = client.ver_retrieve(Q_local, tuple(valmap[g] for g in Q), pi)
            return rep, pi
        return self._prove_merged(groups, Q, valmap)

    def universe_for(self, file_ids):
        r"""把一组文件**归约成一条向量** —— 返回 ``(crs_n, C, P, loc)``。

        * ``P`` = 这些文件全部位置的并集（有序）；
        * ``E = ∏_{j∈P} e_j``、``U' = g^E``、``C' = ∏_f C_f^{E/E_f}``
          —— 「把每份文件在 ``E_f`` 世界里的承诺搬进 ``E`` 世界」，一份一次模幂；
        * ``loc`` = ``全局位置 → 合并向量里的局部下标``（CRS 的指数用它）。

        ★★ **一份文件时它退化为那份文件自己**：``P = pos(f)`` ⇒ ``E = E_f``、
        ``U' = U_f``、``C' = C_f``，而 ``loc`` 就是文件内局部块号。
        所以“单文件”与“跨文件”能走**同一段代码** —— 单文件那条走
        :meth:`core.session.Session.crs_n_for` 的快路（直接用摘要里的 ``U``，不重算）。

        这是论文 ``StrgNode.CreateFrom``（子集 → 新摘要）的跨文件版：
        换宇宙的那一步只是重算承诺（公开可算，不需要任何块的值）。
        """
        fids = sorted({(str(o), str(k)) for o, k in file_ids})
        if not fids:
            raise ValueError("universe_for 至少要一份文件")
        if len(fids) == 1:
            fd = self.delta_of(*fids[0])
            P = tuple(self.registry.positions_of(*fids[0]))
            return self.session.crs_n_for(fd), fd.C, P, {p: i for i, p in enumerate(P)}

        with stage("合并位置集 P"):
            P = tuple(
                sorted({p for f in fids for p in self.registry.positions_of(*f)})
            )
        with stage(f"合并素数积 E（{len(P)} 个位置）"):
            E = self.session.e_all_of(P)
        N, g = self.session.crs.N, self.session.crs.g
        with stage(f"合并承诺 C'（{len(fids)} 份增量乘）"):
            C = 1
            for f in fids:
                pos = self.registry.positions_of(*f)
                E_f = self.session.e_all_of(pos)
                C = C * pow(self.delta_of(*f).C, E // E_f, N) % N
        crn = CRSn(
            crs=self.session.view_crs_for(P), U_n=pow(g, E, N), e_all=E, n=len(P)
        )
        return crn, C, P, {p: i for i, p in enumerate(P)}

    def _prove_merged(self, groups, Q, valmap):
        """跨文件：造出一条合并向量 ``(U', C')``，再在它上面重开一份证据。

        宇宙的构造全在 :meth:`universe_for`；这里只负责“在它上面开一份证据”。

        **抬得动的是承诺，抬不动的是证据**（理由见 :meth:`_prove`）。
        所以这里不是把两边的凭证拼起来，而是拿合并集的**全量值**重开一份。
        """
        crn, C, P, loc = self.universe_for(groups.keys())
        I = tuple(loc[g] for g in Q)
        vals = tuple(valmap[g] for g in Q)
        full = [self.values[pos] for pos in P]
        with stage(f"重算合并证据（Open，|P|={len(P)}）"):
            pi = open_subvector(crn, I, vals, full)
        with stage("承诺验证（合并向量）"):
            rep = verify(crn, C, I, vals, pi)
        return rep, pi

    # ★ 旧设计（全系统一条向量）的遗留：它直接拿节点的**全局下标**，
    #   而新架构下节点是按文件分视图的 —— 这条路径已不再被调用
    #   （凭证收集走 _certs_for）。留着只为对照，**不要**接新调用。
    def _collect(self, Q: tuple[int, ...], *, allow_partial: bool = False):
        """按固定顺序问节点，收齐覆盖 ``Q`` 的凭证。

        先拿一次 ``report()`` 知道各台持有哪些下标，再只向真正要用的那几台
        发检索请求 —— 跨进程时这是 1 + k 次 HTTP，而不是 N 次。

        ``agg`` 要求各凭证的下标集合**两两不相交** —— 这里每个下标只取一次，
        所以天然满足。

        :param allow_partial: 收不齐时是**报错**（默认）还是**把缺的报出来**。
            两者都**不会**静默少拿：报错里直接列出缺哪几个下标，
            部分结果里缺的下标进 ``missing``。

        :returns: ``(凭证, 每个下标的主副本, 用到的节点, 密文段, 缺的下标集合)``
        """
        with stage("探测各节点（report）"):
            report = {row["node_id"]: row for row in self.transport.report()}
        remaining = set(Q)
        certs: list[Certificate] = []
        holders: dict[int, str] = {}
        used: list[str] = []
        cts: dict[int, bytes] = {}

        for nid in self.node_ids:
            row = report.get(nid)
            if not row or not row["held"]:
                continue
            take = sorted(remaining & set(row["indices"]))
            if not take:
                continue
            with stage("取回分量与凭证"):
                pi_part, ct_part = self.transport.retrieve(nid, take)
            # ★ 值**不采信**对方声称的那一份，自己从回来的密文段重算：
            #   分量的唯一来源就是交付的字节，于是"承诺的分量"与"实
            #   际交付的字节"被推导方向绑死 —— 密文被换 ⇒ 这里算出的
            #   值就变 ⇒ 后面的承诺验证必然不过。
            F_derived = tuple(vector_element(ct) for ct in ct_part)
            certs.append(Certificate(take, F_derived, pi_part, nid))
            for j, ct in zip(take, ct_part):
                holders[j] = nid
                cts[j] = ct
            remaining -= set(take)
            used.append(nid)

        if remaining:
            # ★ 一个都没拿到时，就算开了 allow_partial 也照样报错 ——
            #   “拿到一部分”与“一个都没拿到”是两回事，后者没有可验的东西。
            if allow_partial and certs:
                return certs, holders, used, cts, remaining
            why = "、".join(
                f"{nid} 持 {row['held']} 块"
                + ("（联系不上）" if row.get("unreachable") else "")
                for nid, row in report.items()
            )
            hint = "" if certs else f"（各台现状：{why}）"
            raise ValueError(
                f"没有服务器覆盖下标 {sorted(remaining)}{hint} —— "
                f"这些块的每一份副本都不在线（或已被删除）。"
                f"要么等那几台恢复，要么开启「允许部分结果」"
                f"（那时只会把拿得到的块算进结论，缺的会明确列出来）"
            )
        return certs, holders, used, cts, set()

    def _pick_holders(self, indices: Sequence[int]) -> dict[int, str]:
        """给每个下标挑一台**当前能用的**持有者，优先主副本。

        **多副本的价值就落在这里**：主副本那台掉线时自动退到下一份副本，
        而不是整次读取直接失败。挑的顺序按 :meth:`replicas_of`，所以正常情况下
        “从哪拿”仍然是确定性的（就是主副本）。

        单副本时走快路 —— 不必多发一次 ``report()``，单副本的行为与
        “没有副本”的实现逐位相同。

        ★ 节点侧的视图是按**文件**分的（``vectors`` 里每一项对应一份文件），
        而 ``indices`` 是**全局位置号** —— 两边靠每份文件的**位置段**对上：
        ``位置 = positions_of(文件)[该节点的局部块号]``。

        :raises TransportError: 某下标的**每一份**副本都拿不到
        """
        if self.replica_factor == 1:
            return {i: self.holder_of(i) for i in indices}

        by_off: dict[int, tuple[str, str]] = {
            int(d.offset): fid for fid, d in self.deltas.items()
        }
        with stage("探测各节点（report）"):
            report = {row["node_id"]: row for row in self.transport.report()}
        #: 全局位置 -> 真的有密文、而且答得上话的那几台
        held: dict[int, set[str]] = {}
        for nid, row in report.items():
            if row.get("unreachable"):
                continue
            for v in row.get("vectors", ()):
                fid = by_off.get(int(v["offset"]))
                if fid is None:
                    # 协调者库里没有这份文件的摘要 —— 正常不该发生（节点参与过
                    # 一份已经被删掉的文件，或者库不一致）。
                    continue
                pos = self.registry.positions_of(*fid)
                for li in v.get("indices", ()):
                    held.setdefault(pos[int(li)], set()).add(nid)

        out: dict[int, str] = {}
        missing: list[int] = []
        for i in indices:
            for nid in self.replicas_of(i):
                if nid in held.get(i, ()):
                    out[i] = nid
                    break
            else:
                missing.append(i)
        if missing:
            raise TransportError(
                f"下标 {missing[:8]} 的每一份副本都拿不到（节点掉线？）"
            )
        return out

    # -------------------------------------------------------------------
    # 解密 —— 只有拿到块密钥才做得成
    # -------------------------------------------------------------------

    def read(
        self,
        owner: str,
        file_key: str,
        indices=None,
        *,
        key_of: Callable[[int], bytes],
    ) -> bytes:
        """取回明文。``indices`` 为 ``None`` 时取整个文件。

        :param indices: **文件内的块序号**（0 基，与前端“第几块”一致），
            不是全局位置号 —— 新方案里一份文件一条向量，界面上说的
            “第 3 块”永远是这份文件的第 3 块，与别的文件无关。
        :param key_of: ``key_of(pos) -> bytes`` —— 取第 ``pos`` 块（**文件内的
            块序号**）的 SM4 密钥。**必传，没有默认值**：本类不保管密钥，
            只负责"取密文 + 用你给的钥匙解"。

            现在这个回调是"用**解密者自己的 SM2 私钥**解封那一块的密钥
            密文"（:func:`core.keywrap.unwrap_key`）；解不开时它会抛错，
            于是解密失败。**"解密受限"那道门就卡在这个回调上**，不在本类里。
        """
        rec = self.files.get((owner, file_key))
        if rec is None:
            raise KeyError(f"文件 {owner}/{file_key} 不存在")
        delta = self.delta_of(owner, file_key)
        pos_all = tuple(self.registry.positions_of(owner, file_key))
        want_local = (
            tuple(range(delta.n)) if indices is None else as_index_set(indices)
        )
        for i in want_local:
            if not 0 <= i < delta.n:
                raise IndexError(
                    f"块号 {i} 越界：{owner}/{file_key} 只有 {delta.n} 块"
                )
        want = tuple(pos_all[i] for i in want_local)  # 对应的全局位置

        # 主副本掉线时退到别的副本 —— 多副本在**读**这条路上的价值全在这里
        picked = self._pick_holders(want)
        by_node: dict[str, list[int]] = {}
        for loc, g in zip(want_local, want):
            by_node.setdefault(picked[g], []).append(loc)

        ct_of: dict[int, bytes] = {}
        with stage("取回密文"):
            for nid, locs in by_node.items():
                # ★ 传输层要的是**文件内局部块号** + 这份文件的偏移
                #   （节点按 offset 认“这是哪份文件”）。
                _, ct_part = self.transport.retrieve(nid, delta.offset, locs)
                ct_of.update(dict(zip(locs, ct_part, strict=True)))

        out: dict[int, bytes] = {}
        for loc in want_local:
            # ★ 解封块密钥要单独一步、不能塞进下面那个 with 里 ——
            #   参数求值发生在 with 体内，混在一起会把椭圆曲线标量乘的时间
            #   算到 SM4 头上；那两段量级差很多，混了就没意义了。
            key = key_of(loc)
            with stage("解密（SM4）"):
                pt = decrypt_segment(ct_of[loc], key, rec.ivs[loc])
            if len(pt) != rec.plain_lengths[loc]:
                raise ValueError(f"块 {loc} 解密后长度不对")
            out[loc] = pt
        with stage("拼接明文"):
            return b"".join(out[loc] for loc in want_local)

    # -------------------------------------------------------------------
    # 存储证明（PoR）—— 审计"节点到底还存没存着"
    # -------------------------------------------------------------------

    def pos_audit(
        self,
        *,
        file_id: tuple[str, str] | None = None,
        lambda_pos: int = DEFAULT_LAMBDA_POS,
        rng=None,
    ) -> dict:
        """一次**存储证明**审计：不下载任何内容，确认各节点确实还存着它那份。

        ★ 新方案里**一份文件一条向量**，所以“挑战”只能按**某一份文件**发。
        ``file_id=None`` 时对**每一份**文件各跑一轮，汇总成
        ``{"ok": ..., "files": [...]}``；给了 ``file_id`` 就只跑那一份，
        返回的还是单份的老形状（少一层包装）。

        和 :meth:`query` 问的不是同一件事：

        * :meth:`query` 问"你给我的这几块**对不对**"（检索 + 验证），
          而且**允许横跨多台**、最后合成一份证据；
        * ``pos_audit`` 问"你**还存着**吗" —— 不需要先说清要哪儿块，
          挑战方随机点名 :math:`\\lambda_{pos}` 个位置，谁都答不上来就算丢数据。

        流程就是论文附录 D.1 的四步：挑战 → 各节点证明 → 聚合 → 验证。

        .. important::

           **挑战按"主副本归属"拆给各台。** 开了副本之后同一个下标存在于
           多台上，而 ``PoS-Aggregate`` 要求各份额**互不相交**（部分重叠时
           它宁可报错也不静默出错）。所以本方法把 ``r`` 切成
           ``r_node = r ∩ {i : 主副本(i) == node}`` 再分头发下去 ——
           主副本集合天然是 ``[n]`` 的一个划分，各份额必然两两不交。

           代价要说清：这**只审计主副本那一份**。要连副本一起审计，
           得按"第 k 份副本"分组再跑一轮（``replicas_of(i)[k]``）——
           结构上就是这个方法的自然推广，没做。

        :returns: 一份汇报：挑战 ``r``、各台回答了几个下标、聚合后证明多大、
            以及验证结论。``ok=False`` 时 ``message`` 说清是哪一环没过。
        """
        if file_id is None:
            # ★ 逐份跑 —— 审计要的是“每一份文件都还在”，而不是“全局向量还在”。
            each = [
                self.pos_audit(file_id=fid, lambda_pos=lambda_pos, rng=rng)
                for fid in sorted(self.files)
            ]
            bad = [e for e in each if not e["ok"]]
            #: 挑战下标：逐份拼起来。``Q`` 与 ``challenge`` 在旧形状里是两个
            #: 字段（一个“收齐的”、一个“发出去的”）；新架构里每份内部这两者
            #: 本来就相等，所以顶层给同一个列表。
            qs = [c for e in each for c in e.get("challenge", [])]
            #: “问了几个 / 答了几个”**按机器归并** —— 否则 5 份文件就会打出
            #: 20 行同样的 node-1..4，读的人只会以为哪里出了错。
            merged: dict[str, dict] = {}
            for e in each:
                for share in e.get("shares", []):
                    row = merged.setdefault(
                        share["node_id"],
                        {"node_id": share["node_id"], "asked": 0, "answered": 0},
                    )
                    row["asked"] += share.get("asked", 0)
                    row["answered"] += share.get("answered", 0)
                    if share.get("unreachable"):
                        row["unreachable"] = True
            merged_shares = [merged[k] for k in sorted(merged)]
            return {
                "ok": not bad,
                "files": each,
                # ---- 兼容旧形状的**汇总**字段 ----
                # ★ 新架构里审计只能按文件做（一份文件一条向量），但调用方
                #   （路由 / 前端 / 冒烟脚本）读的一直是 shares / challenge /
                #   proof_size_bytes 这些**顶层**字段。在这里合一次，
                #   它们就完全不必知道审计已经改成逐份进行了。
                "challenge": qs,
                "Q": qs,
                "lambda_pos": lambda_pos,
                "n": self.n,
                "shares": merged_shares,
                "queried_nodes": len(merged_shares),
                "aggregated_shares": sum(e.get("aggregated_shares", 0) for e in each),
                "proof_size_bytes": sum(e.get("proof_size_bytes", 0) for e in each),
                "audited_files": [
                    {"owner": fid[0], "file_key": fid[1]} for fid in sorted(self.files)
                ],
                "message": (
                    "所有文件都通过了存储证明"
                    if not bad
                    else f"有 {len(bad)} 份文件的存储证明没过"
                ),
            }

        delta = self.delta_of(*file_id)
        pos_all = tuple(self.registry.positions_of(*file_id))
        n = delta.n
        if n == 0:
            raise ValueError(f"{file_id[0]}/{file_id[1]} 没有块，没有可挑战的数据")

        with stage("生成挑战"):
            challenge = pos_challenge(n, lambda_pos, rng)

        # ① 把 r 按主副本归属拆开 —— 副本会让多台持有同一下标，
        #    而聚合要求份额两两不交。
        per_node: dict[str, list[int]] = {}
        with stage("按主副本拆挑战"):
            for i in challenge.indices:
                per_node.setdefault(self.holder_of(pos_all[i]), []).append(i)

        # ② 逐台收份额。有的节点可能一个下标都不沾（挑战没打到它）。
        proofs: list[PoSProof] = []
        shares: list[dict] = []
        for nid in self.node_ids:
            want = per_node.get(nid, [])
            if not want:
                shares.append(
                    {"node_id": nid, "asked": 0, "answered": 0, "unreachable": False}
                )
                continue
            try:
                with stage("各节点生成证明"):
                    proof = self.transport.pos_prove(nid, delta.offset, want)
            except TransportError as exc:
                # 答不上来本身就是审计结论，不要在这里抛 ——
                # "某一台连不上"和"整个审计做不了"是两回事。
                shares.append(
                    {
                        "node_id": nid,
                        "asked": len(want),
                        "answered": 0,
                        "unreachable": True,
                        "error": str(exc)[:200],
                    }
                )
                continue
            proofs.append(proof)
            shares.append(
                {
                    "node_id": nid,
                    "asked": len(want),
                    "answered": len(proof.Q),
                    "unreachable": False,
                }
            )

        base = {
            "file_id": list(file_id),
            "challenge": list(challenge.indices),
            "lambda_pos": lambda_pos,
            "n": n,
            "offset": delta.offset,
            "shares": shares,
            "queried_nodes": sum(1 for s in shares if s["asked"]),
        }

        if not any(p.Q for p in proofs):
            return {
                **base,
                "ok": False,
                "message": "没有任何一台节点回答到挑战里的下标 —— 数据可能已经没了",
                "proof_size_bytes": 0,
            }

        # ③ 合并成**一份**（论文：分布式生成、大小与参与节点数无关）
        crs_n = self.session.crs_n_for(delta)
        try:
            with stage("聚合份额"):
                done, agg = pos_aggregate_all(crs_n, challenge, proofs, strict=False)
        except ValueError as exc:
            return {**base, "ok": False, "message": f"份额合并失败：{exc}"}

        if not done:
            missing = sorted(set(challenge.indices) - set(agg.Q))
            return {
                **base,
                "ok": False,
                "message": f"挑战没被收齐，缺下标 {missing[:12]}"
                f"{'…' if len(missing) > 12 else ''}",
                "proof_size_bytes": proof_bytes(agg.pi_Q.S_I, agg.pi_Q.Lambda_I)
                if agg.has_proof()
                else 0,
            }

        # ④ 验证一次（判据两道：Q = r，且内容没被换掉）
        with stage("验证证明"):
            report = pos_ver(ClientNode(self.session, delta), challenge, agg)
        return {
            **base,
            "ok": bool(report.ok),
            "message": report.message or "存储证明通过",
            "Q": list(agg.Q),
            "proof_size_bytes": proof_bytes(agg.pi_Q.S_I, agg.pi_Q.Lambda_I),
            "aggregated_shares": len([p for p in proofs if p.Q]),
        }

    # -------------------------------------------------------------------
    # 自检
    # -------------------------------------------------------------------

    def authoritative_digest(self, file_id: tuple[str, str]) -> Digest:
        """用"一次性承诺"独立重算全局摘要 —— 只用于自检。

        增量维护的 ``(U, C)`` 必须与"对整条向量做一次 ``commit``"逐位相同。
        两条路径在实现上完全独立（一条是 :func:`svc.add_back` 迭代，
        一条是 :func:`svc.specialize` + :func:`svc.commit`），所以这个比对
        是真的交叉验证，不是自我复读。
        """
        pos = tuple(self.registry.positions_of(*file_id))
        n = len(pos)
        if n == 0:
            return self.session.bootstrap()
        E = self.session.e_all_of(pos)
        crs_n = CRSn(
            crs=self.session.view_crs_for(pos),
            U_n=pow(self.session.crs.g, E, self.session.crs.N),
            e_all=E,
            n=n,
        )
        with stage("对整份文件做一次 commit"):
            com = commit(crs_n, [self.values[g] for g in pos])
        return Digest(
            U=crs_n.U_n,
            C=com.C,
            n=n,
            offset=int(self.delta_of(*file_id).offset),
        )

    def check(self, *, leaving: Collection[str] = ()) -> list[str]:
        """全面自检。任何一处不对就抛异常。

        :param leaving: **即将退出集群、但进程还在跑**的那几台机器
            （缩容保存之后、重启之前那一段窗口）。它们手里还留着旧副本，
            而且那几份副本**结构上删不掉** —— :meth:`truncate` 只能删向量末尾，
            搬块时没法让一台机器吐出它中间的某个下标。所以多出来的副本
            **只允许来自这个名单**；来自名单之外的机器，仍然是账实不符。

        :returns: 这次自检的**提示**（不是错误），空列表 = 没什么可说的。
            目前只有一种：被摘掉的机器在重启前仍持有旧副本。

        查六件事：

        1. 登记表自身一致；
        2. 登记表与向量同步；
        3. 每台节点的本地视图合法、且声称持有的下标与实际存着的密文一致；
        4. 每台节点都在同一个 ``n`` 上（**这一点跨进程后最容易漏** ——
           某台没跟上更新，它就会拿着旧 ``n`` 的证据，验证必然失败）；
        5. 每个下标的**实际持有者集合**与协调者记的副本列表**逐块一致**
           （副本数 = 1 时就退化成“各台两两不相交”）；
        6. 增量摘要 == 一次性承诺。
        """
        with stage("位置段自检"):
            problems = self.registry.check()
        if problems:
            raise ValueError("位置段登记表自检不过：" + "；".join(problems))

        with stage("逐台核对节点视图"):
            report = self.transport.report(verify=True)
        held_by: dict[int, set[str]] = {}
        #: ``offset -> 文件键``：节点侧是按 offset 报现状的，这里换回文件
        by_off = {int(self.delta_of(*f).offset): f for f in self.files}
        for row in report:
            nid = row["node_id"]
            if row.get("unreachable"):
                # “宁可吵不要哑”：看不全就别下结论，否则自检会给出一个假的安全感
                raise ValueError(f"{nid} 现在联系不上 —— 自检需要看到全部节点")
            if row.get("fresh"):
                continue
            # ★ 新方案：一台节点可能同时参与好几份文件，所以它按**文件**报现状
            #   （``vectors`` 每份一行）。逐份核对 n / 视图 / 密文一致，
            #   并把局部块号换回全局位置号统一记账。
            for v in row.get("vectors", ()):
                off = int(v["offset"])
                f = by_off.get(off)
                if f is None:
                    continue  # 协调者账上没有这份（孤儿视图）
                want_n = self.delta_of(*f).n
                if v["n"] != want_n:
                    raise ValueError(
                        f"{nid} 在 offset={off} 那份文件上停在 n={v['n']}，"
                        f"而协调者是 n={want_n} —— 它没跟上某次更新"
                    )
                if not v["valid"]:
                    raise ValueError(
                        f"{nid} 声称持有的下标与实际存着的密文不一致"
                    )
                if v["proved"] is not True:
                    raise ValueError(
                        f"{nid} 的本地视图不合法（通不过 svc.verify 的两步校验）"
                    )
                pos = self.registry.positions_of(*f)
                for li in v["indices"]:
                    held_by.setdefault(pos[li], set()).add(nid)

        want: set[int] = set()
        for f in self.files:
            want |= set(self.registry.positions_of(*f))
        if set(held_by) != want:
            missing = sorted(want - set(held_by))
            extra = sorted(set(held_by) - want)
            raise ValueError(
                f"持有者账实不符：没人持有 {missing[:12]}"
                f"{'（多出来 ' + str(extra[:12]) + '）' if extra else ''}"
            )
        if set(self._holder) != want or set(self._replicas) != want:
            raise ValueError("持有者映射与在用位置集合不一致")

        #: 「即将退出集群」的那几台 —— 只有它们多持有副本是被允许的。
        leaving_set = set(leaving)
        #: ``node_id -> 它还多留着的下标``（只记合法的那些）。
        surplus: dict[str, list[int]] = {}

        for i in sorted(want):
            mine = set(self.replicas_of(i))
            if not mine:
                raise ValueError(f"下标 {i} 一份副本都没有")
            extra = held_by[i] - mine
            # 少持有 = 真的缺数据，任何情况下都拦。
            if mine - held_by[i]:
                raise ValueError(
                    f"下标 {i} 记的副本是 {sorted(mine)}，实际持有的是 "
                    f"{sorted(held_by[i])} —— 少了 {sorted(mine - held_by[i])}"
                )
            # 多持有：只准来自「即将退出集群」的那几台。
            if extra and not extra <= leaving_set:
                raise ValueError(
                    f"下标 {i} 记的副本是 {sorted(mine)}，实际持有的是 "
                    f"{sorted(held_by[i])}"
                    f"（多出来的 {sorted(extra - leaving_set)} 不在"
                    f"「即将退出集群」的名单里）"
                )
            for nid in extra:
                surplus.setdefault(nid, []).append(i)
            # ★ 这里**不断言**副本数恰好等于配置值。因为“副本数不足”还可能是
            #   一个**合法**的历史状态：开副本之前传的文件就只有一份。
            #   该报的是“账实不符”（上面那条），而副本不足单独用
            #   :meth:`under_replicated` 报 —— 它是提示，不是错误。

        for f, rec in self.files.items():
            ref = self.authoritative_digest(f)
            now = self.delta_of(*f)
            if (ref.U, ref.C, ref.n) != (now.U, now.C, now.n):
                raise ValueError(
                    f"{f[0]}/{f[1]} 的增量摘要与一次性承诺不一致：\n"
                    f"  {ref!r}\n  {now!r}"
                )

        notes: list[str] = []
        if surplus:
            head = "；".join(
                f"{nid} 还留着 {len(v)} 块（下标 {v[:8]}"
                f"{'…' if len(v) > 8 else ''}）"
                for nid, v in sorted(surplus.items())
            )
            notes.append(
                "被摘掉的机器在重启前仍持有旧副本："
                + head
                + "。它们已经不参与分发与检索（账目以留下的那几台为准），"
                "但 VDS 只能删向量的末尾，所以这几份副本要等它们下线才会消失 —— "
                "重启之后自检就是干净的，这不是数据错误。"
            )
        return notes

    def under_replicated(self) -> list[int]:
        """副本数**少于配置值**的下标。

        最常见的原因是：这些块是**开副本之前**传的（账上就写着只有一份）。
        它不是“账实不符”，所以**不放进** :meth:`check` 拦下 —— 那是提示，
        不是错误；界面把它显示出来，想出副本重新传一次就行。
        """
        want: set[int] = set()
        for f in self.files:
            want |= set(self.registry.positions_of(*f))
        return [
            i for i in sorted(want)
            if len(self.replicas_of(i)) < self.replica_factor
        ]

    def node_report(self) -> list[dict]:
        """各服务器现状 —— 给界面上的"块分布矩阵"用。

        这里**带上密码学验证**（``proved`` 字段），因为这是显式请求现状的
        接口，不是检索路径上顺手拿一下数据。
        """
        rows = self.transport.report(verify=True)
        # ★ 节点自己只知道**文件内局部块号**（它按 offset 分视图），所以
        #   “这些块在全局向量里占哪个位置”只有协调者补得出来 —— 用各文件的
        #   位置段把局部块号映回全局位置。顺便把旧字段（indices / n / span）
        #   补回去，依赖它们的页面就不必跟着改。
        by_off: dict[int, tuple[str, str]] = {
            int(d.offset): fid for fid, d in self.deltas.items()
        }
        out: list[dict] = []
        for row in rows:
            positions: list[int] = []
            for v in row.get("vectors", ()):
                fid = by_off.get(int(v["offset"]))
                if fid is None:
                    # 节点参与了一份已经被删掉的文件：协调者这儿没它的位置段，
                    # 那些块也就不该再算进“持有位置”。
                    continue
                pos = self.registry.positions_of(*fid)
                for li in v.get("indices", ()):
                    positions.append(int(pos[int(li)]))
            positions.sort()
            row = dict(row)
            row["indices"] = positions
            row["n"] = len(positions)
            row["span"] = span(positions)
            out.append(row)
        return out

    def set_crs(self) -> None:
        """把公开参数交给各节点（跨进程时是第一次通信）。"""
        self.transport.set_crs(
            {
                "N": str(self.session.crs.N),
                "g": str(self.session.crs.g),
                "l": self.session.l,
                "n_max": self.session.n_max,
            }
        )

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return (
            f"VectorStore(n={self.n}, 节点 {len(self.node_ids)}, "
            f"文件 {len(self.files)}, {type(self.transport).__name__})"
        )


from vds.updates import PushedUpdate  # noqa: E402  （放在这里免得动文件头那段 import 块）


def _chunks_of(positions: Sequence[int]) -> tuple[tuple[int, int], ...]:
    """把一串全局位置号压成连续段 ``((起点, 长度), ...)``（按升序）。

    新方案里“一份文件的位置”未必是一段连续区间：追加时如果原段的末尾已经被
    后来的文件占住，新块只能另起一段（段永不回收）。摘要里的 ``chunks``
    就是这件事的记录，而 :meth:`Digest.positions` 靠它展开出“第 i 块到底是
    哪个全局位置” —— 传输层与节点侧的下标换算全指望着它。
    """
    out: list[tuple[int, int]] = []
    for p in sorted(int(x) for x in positions):
        if out and out[-1][0] + out[-1][1] == p:
            off, cnt = out[-1]
            out[-1] = (off, cnt + 1)
        else:
            out.append((p, 1))
    return tuple(out)


def _pusher_placeholder(session: GlobalSession, delta: Digest):
    """``push_update`` 要求一个"推送方"节点。

    对 ``add`` 而言，推送方的状态只被用来算它**自己**的新状态 —— 而协调者
    没有状态，所以用一个空视图占位，算完直接丢掉。数学上合法：
    :math:`\\pi_\\varnothing = (U_n, C_n)` 是真正的空视图。
    """
    return StorageNode(
        "__coordinator__",
        session,
        LocalView(delta=delta, st=Opening(delta.U, delta.C, ()), I=(), FI=()),
    )


def _full_holder(
    session: GlobalSession, delta: Digest, values: Mapping[int, int]
) -> StorageNode:
    """「协调者扮成**持有整个向量**的节点」—— 发起 ``del`` 时的推送方。

    为什么需要它：``vds.updates._push_del`` 要求推送方**持有整个** ``K``
    （它要拆出 ``π_K`` = "把 K 挖掉之后那个向量的摘要"，而拆卸要用到 K 上的值）。
    可我们的分片是「轮转 + 每块 N 份副本」，一个跨台的末尾区间**不可能**
    由某一台完整持有 —— 所以让协调者自己扮这个角色：它手里本来就有整个向量。

    ``st`` 必须是空向量的摘要 ``(g, 1)``。★ **不要**用
    :meth:`GlobalSession.bootstrap` —— 它返回 ``(U=1, C=g)``，那是论文对
    "空**文件**"的约定取值（``st_0 ← g``），拿去当 ``add_back`` 的起点会在
    代数前提上出错（实测：算出的新摘要与一次性承诺对不上，
    ``check_local_view()`` 也是 ``False``）。正规算法就是「用公开参数对
    **空向量**承诺一次」：``specialize(crs, 0)`` 的 ``U_0`` 就是 ``g``，
    ``commit(crs_0, []).C`` 就是 ``1``。

    实测（``scripts/_probe_truncate2.py``）：这样算出的新摘要与「对截断后的
    向量重新做一次 :func:`svc.commit`」**逐位相同**，而且它的
    ``check_local_view()`` 为真 —— 它确实是一个合法本地视图，不是硬凑的数。
    """
    idx = tuple(range(delta.n))
    crs_0 = specialize(session.crs, 0)
    st = Opening(crs_0.U_n, commit(crs_0, []).C, idx)
    return StorageNode(
        "__coordinator__",
        session,
        LocalView(
            delta=delta,
            st=st,
            I=idx,
            FI=tuple(values[i] for i in range(delta.n)),
        ),
    )


class PlainKeyStore(VectorStore):
    """**仅供单机演示与测试**：把块密钥直接放在内存里，不做任何保护。

    :class:`VectorStore` 刻意把密钥的来去做成必填（``upload(key_sink=…)`` /
    ``read(key_of=…)``），逼调用方显式回答"这把钥匙放哪里"。
    可测试与算法演示**根本不关心这件事**（它们测的是分片、承诺、验证、攻击），
    本类就是一个便捷子类：上传时把密钥收进内存，解密时再取出来。

    .. danger::

       **生产路径绝不能用它。** 它不但不封装密钥，还把它们全留在进程内存里。
       真正的系统走 ``backend/manager.py``：块密钥一生成就用**所有者的
       SM2 公钥**封装成密文落到 ``BlockRow.key_ct``，明文用完即丢。
       名字里的 ``PlainKey`` 就是为了在代码里一眼看出这件事。
    """

    __slots__ = ("_plain_keys",)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._plain_keys: dict[tuple[str, str], dict[int, bytes]] = {}

    def _forget_block_keys(self, owner: str, file_key: str, keep_pos: int) -> None:
        """覆写：内存里那些已删块的密钥也要丢掉。

        留着不至于让功能出错（那些下标已经没人读得出来），但本类的全部意义就是
        "把密钥放在内存里"（仅演示与测试）—— 既然删了块，钥匙就应该一起没。
        """
        box = self._plain_keys.get((owner, file_key))
        if not box:
            return
        for pos in [p for p in box if p >= keep_pos]:
            del box[pos]

    def rename_owner(self, owner: str, new_owner: str) -> int:
        """覆写：内存里那份明文档密钥也要跟着换名字。

        不换的话，改名之后 ``read(owner=新名, ...)`` 会在 ``_plain_keys`` 里
        找不到钥匙，表现成"文件明明是我的却解不开" —— 而生产路径上
        同名不同人是应当解不开的，这就没法区分了。
        """
        # 先让父类做（它会先检查冲突，抛了就不会走到下面换键这一步）
        moved = super().rename_owner(owner, new_owner)
        # 列出来再改：字典在迭代中被 pop 会抛 RuntimeError
        stale = [k for k in self._plain_keys if k[0] == owner]
        for key in stale:
            self._plain_keys[(new_owner, key[1])] = self._plain_keys.pop(key)
        return moved

    def upload(
        self,
        owner: str,
        file_key: str,
        plaintext: bytes,
        *,
        segment_bytes: int | None = None,
        nodes: Sequence[str] | None = None,
    ) -> FileRecord:
        """覆写父类签名：把块密钥留在内存里（**仅供演示与测试**）。

        ``segment_bytes`` 与 ``nodes`` 原样转给父类 —— 这一层只管密钥，
        不改变分块与分发语义。
        """
        box: dict[int, bytes] = {}
        try:
            rec = super().upload(
                owner,
                file_key,
                plaintext,
                key_sink=lambda pos, k: box.__setitem__(pos, k),
                segment_bytes=segment_bytes,
                nodes=nodes,
            )
        except BaseException:
            # ★ 失败也要把**已经生成的那几把钥匙**记下来。
            #
            #   块密钥是在加密循环里就生成并交出来的（``key_sink``），
            #   而那已经发生在写推**之前**。所以写推失败时这些钥匙
            #   已经存在了 —— 只不过这个便捷子类习惯在成功之后才登记它们。
            #   若在这里丢掉：补推把向量补齐之后，内容**照样读不出来**
            #   （"内存里没有这份文件的块密钥"）—— 补推看起来成功、
            #   实际用不了，是最难查的那种半成品状态。
            #
            #   （真正的系统走 ``backend/manager.py``：``sink`` 当场把密钥
            #   用所有者公钥封成密文存进 ``key_cts``，所以那边天然没这个问题。
            #   这一条是"调用方自己的收尾也要跟着补推走"的又一例。）
            self._plain_keys[(owner, file_key)] = box
            raise
        self._plain_keys[(owner, file_key)] = box
        return rec

    def read(self, owner: str, file_key: str, indices=None) -> bytes:
        # 先让父类那套"文件在不在"的检查跑在前面 ——
        # "这个文件根本没有"和"我手上没有它的密钥"是两回事，报错要说准。
        if (owner, file_key) not in self.files:
            raise KeyError(f"文件 {owner}/{file_key} 不存在")
        box = self._plain_keys.get((owner, file_key))
        if box is None:
            raise KeyError(
                f"内存里没有 {owner}/{file_key} 的块密钥 —— "
                f"PlainKeyStore 不上库，所以重启/重建后旧文件的密钥就不在手上了"
            )
        return super().read(owner, file_key, indices, key_of=box.__getitem__)

    def modify(
        self, owner: str, file_key: str, block_idx: int, plaintext: bytes
    ) -> tuple[bytes, int]:
        """覆写父类签名：明文密钥直接留在内存里（**仅供演示与测试**）。"""
        if (owner, file_key) not in self.files:
            raise KeyError(f"文件 {owner}/{file_key} 不存在")
        box = self._plain_keys.get((owner, file_key))
        if box is None:
            raise KeyError(
                f"内存里没有 {owner}/{file_key} 的块密钥 —— 改块要换掉那一块的钥匙，"
                f"得先有地方放；PlainKeyStore 不上库，所以重启/重建之后就没法改了"
            )
        return super().modify(
            owner,
            file_key,
            block_idx,
            plaintext,
            key_sink=lambda pos, k: box.__setitem__(pos, k),
        )

    def append(self, owner: str, file_key: str, plaintext: bytes) -> FileRecord:
        """覆写父类签名：新块的明文密钥直接留在内存里（**仅供演示与测试**）。"""
        if (owner, file_key) not in self.files:
            raise KeyError(f"文件 {owner}/{file_key} 不存在")
        box = self._plain_keys.get((owner, file_key))
        if box is None:
            raise KeyError(
                f"内存里没有 {owner}/{file_key} 的块密钥 —— 追加会产生「新的」块密钥，"
                f"得先有地方放；PlainKeyStore 不上库，所以重启/重建之后就没法追加了"
            )
        return super().append(
            owner,
            file_key,
            plaintext,
            key_sink=lambda pos, k: box.__setitem__(pos, k),
        )