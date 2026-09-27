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

两条验证线
----------
查询回来的是**向量分量**（密文摘要），所以验证分两层：

* **第 1 层 · 块哈希自洽** —— ``SM3(密文段) == 收到的分量``。挡住"节点给了
  别的段的密文"。
* **第 2 层 · 向量承诺** —— :func:`svc.verify` 的两步校验，对着全局 δ。挡住
  "节点改了自己存的分量"。

两层都过才算完整，**缺一层都有洞**：6b 那类攻击（换掉密文、分量不动）只有
第 1 层抓得住；而分量被改（密文没换）只有第 2 层抓得住。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from svc import commit, specialize
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
from .node_state import NodeState
from .registry import BlockRef, BlockRegistry
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
    hash_ok: dict[int, bool]
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
    def hash_layer_ok(self) -> bool:
        return all(self.hash_ok.values())

    @property
    def ok(self) -> bool:
        return bool(self.report.ok) and self.hash_layer_ok

    def summary(self) -> str:
        """给人看的一行结论。"""
        if not self.report.ok:
            return f"承诺验证失败：{self.report.message}"
        if not self.hash_layer_ok:
            bad = sorted(i for i, v in self.hash_ok.items() if not v)
            return f"块哈希不自洽，可疑下标 {bad}"
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
        "delta",
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
        self.registry = BlockRegistry()
        self.delta: Digest = session.bootstrap()
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
    # 只读视图
    # -------------------------------------------------------------------

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(self.transport.node_ids)

    @property
    def n(self) -> int:
        """全局向量当前长度（= 已用位置数 = 全局块数）。"""
        return self.delta.n

    def holder_of(self, global_index: int) -> str:
        """这个下标的**主副本**在哪台。检索默认去问它。"""
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

    def file_indices(self, owner: str, file_key: str) -> tuple[int, ...]:
        """一个文件占用的全局下标。"""
        return self.registry.indices_of(owner, file_key)

    def describe(self, global_index: int) -> BlockRef:
        """全局下标 → "这是谁的第几块"。"""
        return self.registry.describe(global_index)

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

               * 顺序有意义 —— 轮转按你给的顺序转（所以界面上按集群顺序发就行）；
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

        # 登记表与向量必须同步；不同步说明别处动过 n
        if self.registry.cursor != self.delta.n:
            raise RuntimeError(
                f"登记表游标 {self.registry.cursor} 与向量长度 {self.delta.n} 不一致"
            )
        # 先查容量再登记 —— 否则超限时会在登记表里留下一段没人持有的脏下标
        if self.delta.n + len(elements) > self.session.n_max:
            raise ValueError(
                f"这个文件要 {len(elements)} 块，加上已有的 {self.delta.n} 块共 "
                f"{self.delta.n + len(elements)} 块，超过全局上限 n_max = "
                f"{self.session.n_max}（位置上限在 Bootstrap 阶段就定死了；"
                f"调大 n_max 或把块切大一点）"
            )
        K = self.registry.alloc_file(owner, file_key, len(elements))
        if K != tuple(range(self.delta.n, self.delta.n + len(elements))):
            raise RuntimeError(f"登记表分配的下标 {K} 不是向量的下一段区间")

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

        self._append(elements, cts, pool=nodes, on_success=_register)
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

        delta_old = self.delta
        op_delta = UpdateDelta(op="mod", K=(gidx,), F_new=(v_new,))
        # 推送方只要出 S_K，而 S_K 与谁发起无关（K 不在 I 里时退回直接模幂，
        # 见 vds.updates._S_K），所以协调者用空视图占位即可 ——
        # 它**不需要**持有这一块，也就不必为了一次改块把明文调进来。
        with stage("承诺（push_update）"):
            pushed = push_update(
                self.session,
                delta_old,
                _pusher_placeholder(self.session, delta_old),
                op_delta,
                {gidx: self.values[gidx]},
            )
        holders = self.replicas_of(gidx)

        def _patch_record() -> None:
            """新 IV / 新长度回到文件账目上（正常路径与补推路径共用）。"""
            rec.ivs[block_idx] = iv_new
            rec.plain_lengths[block_idx] = len(plaintext)
            rec.total_bytes = sum(rec.plain_lengths)

        context = {
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

        if self.registry.cursor != self.delta.n:
            raise RuntimeError(
                f"登记表游标 {self.registry.cursor} 与向量长度 {self.delta.n} 不一致"
            )
        if self.delta.n + len(elements) > self.session.n_max:
            raise ValueError(
                f"追加 {len(elements)} 块后共 {self.delta.n + len(elements)} 块，"
                f"超过全局上限 n_max = {self.session.n_max}"
            )

        K = tuple(
            self.registry.alloc_block(owner, file_key, base_pos + t)
            for t in range(len(elements))
        )
        if K != tuple(range(self.delta.n, self.delta.n + len(elements))):
            raise RuntimeError(f"追加分配的下标 {K} 不是向量的下一段区间")

        # ★ 跟着**这份文件自己已有的机器**走（见 _pool_of）。
        #   否则“我当初只勾了 1、2 号”，一次追加就会把新块漏到没勾的机器上。
        def _extend() -> None:
            """把新块接到这份文件的账目上（正常路径与补推路径共用）。"""
            rec.indices = rec.indices + K
            rec.ivs.extend(ivs)
            rec.plain_lengths.extend(lens)
            rec.total_bytes += len(plaintext)

        self._append(elements, cts, pool=pool, on_success=_extend)
        return rec

    # -------------------------------------------------------------------
    # 追加的内部实现
    # -------------------------------------------------------------------

    def _append(
        self,
        elements: list[int],
        cts: list[bytes],
        *,
        pool: Sequence[str] | None = None,
        on_success: Callable[[], None] | None = None,
    ) -> None:
        """推进全局摘要，并**通知每一台节点各自跟上**。

        :param on_success: 这次追加**自己的收尾**（登记 :class:`FileRecord`、
            把新块接到文件账目上…）。它在**账目推进之后**被调用 ——
            正常路径在下面调，补推路径在 :meth:`retry_pending` 里调。
            抽成回调是为了让"补推成功"与"第一次就成功"走**同一段**收尾代码。
        """
        delta_old = self.delta
        k = len(elements)
        K = tuple(range(delta_old.n, delta_old.n + k))

        op_delta = UpdateDelta("add", K, tuple(elements))
        # 走 vds 层已审计过的 PushUpdate 来算新摘要与更新密钥；
        # 推送方只是一个占位（它的新状态与本次追加无关，直接丢掉）。
        with stage("承诺（push_update）"):
            pushed = push_update(
                self.session,
                delta_old,
                _pusher_placeholder(self.session, delta_old),
                op_delta,
            )
        delta_new = pushed.delta
        if delta_new.n != delta_old.n + k:
            raise RuntimeError(f"追加后长度不对：{delta_new.n} != {delta_old.n + k}")

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
        if self.registry.cursor != self.delta.n:
            raise RuntimeError(
                f"登记表游标 {self.registry.cursor} 与向量长度 {self.delta.n} 不一致"
            )

        n = self.delta.n
        K = tuple(range(n - drop, n))
        mine = rec.indices[-drop:]
        if mine != K:
            blocked = sorted(set(K) - set(mine))
            raise ValueError(
                f"只能删「当前向量末尾」的块，而末尾 {list(K)} 里有下标 {blocked} "
                f"不属于这份文件（{owner}/{file_key} 的最后 {drop} 块是 {list(mine)}）"
                f"—— 它们后面还压着之后写的块。要删这份文件的尾巴，先清掉那几块，"
                f"或者重新上传一份"
            )

        # ① 先让“与 K 部分相交”的节点交回 K 那部分
        #    （整段持有 K 的节点不动它 —— 底层 del 走“K ⊆ I”那条更便宜的路：
        #      只丢下标，(S_I, Λ_I) 一个字不改）
        drops = self._plan_drops(K)
        if drops:
            self.transport.drop(drops)

        # ② 协调者自己发起 del（它手里有整个向量，不依赖任何一台正好持有整个尾巴）
        delta_old = self.delta
        op_delta = UpdateDelta("del", K, ())
        old_values = {i: self.values[i] for i in K}
        pushed = push_update(
            self.session,
            delta_old,
            _full_holder(self.session, delta_old, self.values),
            op_delta,
            old_values,
        )
        if pushed.delta.n != n - drop:
            raise RuntimeError(
                f"删除后长度不对：{pushed.delta.n} != {n - drop}"
                f"（这是实现自检，不该发生）"
            )

        def _settle() -> None:
            """这次删除**自己的收尾**（正常路径与补推路径共用）。

            两件事：退回末尾下标（登记表游标要与向量长度一起回落）、
            把文件账目截短。
            """
            self.registry.free_tail(drop)
            rec.indices = rec.indices[:-drop]
            rec.ivs = rec.ivs[:-drop]
            rec.plain_lengths = rec.plain_lengths[:-drop]
            rec.total_bytes = sum(rec.plain_lengths)
            # 删掉的块如果有块密钥留在实现里，也要跟着抹掉（本类没有，子类有）
            self._forget_block_keys(owner, file_key, rec.block_count)

        context = {
            "kind": "del",
            "delta_new": pushed.delta,
            "K": K,
            "per_index": {},
            "elements": (),
            "on_success": _settle,
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
        return K, rec

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
        self.delta = p["delta_new"]
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
        self.delta = p["delta_new"]
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

        副本从主副本的位置**往后连续**取（而不是另抽），这样：

        * ``replica_factor = 1`` 时结果与“没有副本”的实现**逐位相同**；
        * “哪几台合起来能凑齐全部块”仍然均匀 —— 副本也参与轮转。

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
        per_index: dict[int, tuple[str, ...]] = {}
        # ★ 桶覆盖全部节点（含没参与的）；轮转只在 nodes 里发生。
        buckets: dict[str, list[int]] = {nid: [] for nid in self.node_ids}
        for t, j in enumerate(K):
            base = (self._offset + t) % m
            holders = tuple(
                nodes[(base + c) % m] for c in range(self.replica_factor)
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
        if Q[-1] >= self.delta.n:
            raise ValueError(f"下标 {Q[-1]} 超出当前向量长度 {self.delta.n}")

        certs, holders, used, cts, missing = self._collect(Q, allow_partial=allow_partial)

        # 允许部分结果时，下面的聚合与验证只针对**真的拿到了**的那些块 ——
        # 把缺的也塞进 F_Q / 证据里就是伪造（那样验出来的“通过”毫无意义）。
        if missing:
            Q = tuple(i for i in Q if i not in missing)

        client = ClientNode(self.session, self.delta)
        with stage("聚合凭证"):
            pi_K = client.aggregate_certificates(certs)

        valmap: dict[int, int] = {}
        for c in certs:
            valmap.update(dict(zip(c.Q, c.F_Q)))
        F_Q = tuple(valmap[i] for i in Q)

        with stage("承诺验证（VerRetrieve）"):
            report = client.ver_retrieve(Q, F_Q, pi_K)

        # 第 1 层：块哈希自洽 —— 拿回来的密文必须真的对得上那个分量
        with stage("块哈希自检（SM3）"):
            hash_ok = {i: vector_element(cts[i]) == valmap[i] for i in Q}

        return QueryResult(
            indices=Q,
            values=F_Q,
            proof=pi_K,
            report=report,
            holders=dict(holders),
            refs=self.registry.blocks_of(Q),
            hash_ok=hash_ok,
            cert_count=len(certs),
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
        if Q[-1] >= self.delta.n:
            raise ValueError(f"下标 {Q[-1]} 超出当前向量长度 {self.delta.n}")
        certs, holders, used, cts, _missing = self._collect(Q)
        return certs, holders, used, cts

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
                F_part, pi_part, ct_part = self.transport.retrieve(nid, take)
            certs.append(Certificate(take, F_part, pi_part, nid))
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

        :raises TransportError: 某下标的**每一份**副本都拿不到
        """
        if self.replica_factor == 1:
            return {i: self.holder_of(i) for i in indices}

        with stage("探测各节点（report）"):
            report = {row["node_id"]: row for row in self.transport.report()}
        held: dict[str, set[int]] = {}
        for nid, row in report.items():
            if not row.get("unreachable"):
                held[nid] = set(row["indices"])

        out: dict[int, str] = {}
        missing: list[int] = []
        for i in indices:
            for nid in self.replicas_of(i):
                if i in held.get(nid, ()):
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
        want = rec.indices if indices is None else as_index_set(indices)

        for i in want:
            ref = self.registry.describe(i)
            if ref.owner != owner or ref.file_key != file_key:
                raise ValueError(f"下标 {i} 属于 {ref}，不属于 {owner}/{file_key}")
        # 主副本掉线时退到别的副本 —— 多副本在**读**这条路上的价值全在这里
        picked = self._pick_holders(want)
        by_node: dict[str, list[int]] = {}
        for i in want:
            by_node.setdefault(picked[i], []).append(i)

        ct_of: dict[int, bytes] = {}
        with stage("取回密文"):
            for nid, idxs in by_node.items():
                _, _, ct_part = self.transport.retrieve(nid, idxs)
                ct_of.update(dict(zip(idxs, ct_part, strict=True)))

        out: dict[int, bytes] = {}
        for i in want:
            pos = self.registry.describe(i).block_idx
            # ★ 解封块密钥要单独一步、不能塞进下面那个 with 里 ——
            #   参数求值发生在 with 体内，混在一起会把椭圆曲线标量乘的时间
            #   算到 SM4 头上；那两段量级差很多，混了就没意义了。
            key = key_of(pos)
            with stage("解密（SM4）"):
                pt = decrypt_segment(ct_of[i], key, rec.ivs[pos])
            if len(pt) != rec.plain_lengths[pos]:
                raise ValueError(f"下标 {i} 解密后长度不对")
            out[i] = pt
        with stage("拼接明文"):
            return b"".join(out[i] for i in want)

    # -------------------------------------------------------------------
    # 存储证明（PoR）—— 审计"节点到底还存没存着"
    # -------------------------------------------------------------------

    def pos_audit(
        self,
        *,
        lambda_pos: int = DEFAULT_LAMBDA_POS,
        rng=None,
    ) -> dict:
        """一次**存储证明**审计：不下载任何内容，确认各节点确实还存着它那份。

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
        n = self.n
        if n == 0:
            raise ValueError("全局向量还是空的，没有可挑战的数据")

        with stage("生成挑战"):
            challenge = pos_challenge(n, lambda_pos, rng)

        # ① 把 r 按主副本归属拆开 —— 副本会让多台持有同一下标，
        #    而聚合要求份额两两不交。
        per_node: dict[str, list[int]] = {}
        with stage("按主副本拆挑战"):
            for i in challenge.indices:
                per_node.setdefault(self.holder_of(i), []).append(i)

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
                    proof = self.transport.pos_prove(nid, want)
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
            "challenge": list(challenge.indices),
            "lambda_pos": lambda_pos,
            "n": n,
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
        crs_n = self.session.crs_n_for(self.delta)
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
            report = pos_ver(ClientNode(self.session, self.delta), challenge, agg)
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

    def authoritative_digest(self) -> Digest:
        """用"一次性承诺"独立重算全局摘要 —— 只用于自检。

        增量维护的 ``(U, C)`` 必须与"对整条向量做一次 ``commit``"逐位相同。
        两条路径在实现上完全独立（一条是 :func:`svc.add_back` 迭代，
        一条是 :func:`svc.specialize` + :func:`svc.commit`），所以这个比对
        是真的交叉验证，不是自我复读。
        """
        n = self.delta.n
        if n == 0:
            return self.session.bootstrap()
        crs_n = specialize(self.session.crs, n)
        with stage("对整条向量做一次 commit"):
            com = commit(crs_n, [self.values[i] for i in range(n)])
        return Digest(U=crs_n.U_n, C=com.C, n=n)

    def check(self) -> None:
        """全面自检。任何一处不对就抛异常。

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
        with stage("登记表自检"):
            self.registry.check()
        if self.registry.cursor != self.delta.n:
            raise ValueError(
                f"登记表游标 {self.registry.cursor} != 向量长度 {self.delta.n}"
            )

        with stage("逐台核对节点视图"):
            report = self.transport.report(verify=True)
        held_by: dict[int, set[str]] = {}
        for row in report:
            nid = row["node_id"]
            if row.get("unreachable"):
                # “宁可吵不要哑”：看不全就别下结论，否则自检会给出一个假的安全感
                raise ValueError(f"{nid} 现在联系不上 —— 自检需要看到全部节点")
            if row.get("fresh"):
                continue
            if row["n"] != self.delta.n:
                raise ValueError(
                    f"{nid} 停在 n={row['n']}，而协调者是 n={self.delta.n} —— "
                    f"它没跟上某次更新"
                )
            if not row["valid"]:
                raise ValueError(f"{nid} 声称持有的下标与实际存着的密文不一致")
            if row["proved"] is not True:
                raise ValueError(f"{nid} 的本地视图不合法（通不过 svc.verify 的两步校验）")
            for i in row["indices"]:
                held_by.setdefault(i, set()).add(nid)

        want = set(range(self.delta.n))
        if set(held_by) != want:
            missing = sorted(want - set(held_by))
            raise ValueError(f"下标 {missing} 没有任何节点持有")
        if set(self._holder) != want or set(self._replicas) != want:
            raise ValueError("持有者映射与向量长度不一致")

        for i in range(self.delta.n):
            mine = set(self.replicas_of(i))
            if not mine:
                raise ValueError(f"下标 {i} 一份副本都没有")
            if mine != held_by[i]:
                raise ValueError(
                    f"下标 {i} 记的副本是 {sorted(mine)}，实际持有的是 "
                    f"{sorted(held_by[i])}"
                )
            # ★ 这里**不断言**副本数恰好等于配置值。因为“副本数不足”还可能是
            #   一个**合法**的历史状态：开副本之前传的文件就只有一份。
            #   该报的是“账实不符”（上面那条），而副本不足单独用
            #   :meth:`under_replicated` 报 —— 它是提示，不是错误。

        ref = self.authoritative_digest()
        if ref != self.delta:
            raise ValueError(
                f"增量摘要与一次性承诺不一致：\n  {ref!r}\n  {self.delta!r}"
            )

    def under_replicated(self) -> list[int]:
        """副本数**少于配置值**的下标。

        最常见的原因是：这些块是**开副本之前**传的（账上就写着只有一份）。
        它不是“账实不符”，所以**不放进** :meth:`check` 拦下 —— 那是提示，
        不是错误；界面把它显示出来，想出副本重新传一次就行。
        """
        return [
            i for i in range(self.delta.n)
            if len(self.replicas_of(i)) < self.replica_factor
        ]

    def node_report(self) -> list[dict]:
        """各服务器现状 —— 给界面上的"块分布矩阵"用。

        这里**带上密码学验证**（``proved`` 字段），因为这是显式请求现状的
        接口，不是检索路径上顺手拿一下数据。
        """
        return self.transport.report(verify=True)

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