"""★ 故障演练 —— 把若干台存储节点模拟成「掉线」或「永久损毁」。

为什么在**传输层**做
--------------------
协调者与节点之间的一切交互都必须经过
:class:`~core.transport.NodeTransport`。包一层就能**完整**地模拟
"这台机器没了"，而且对两种实现同时成立（同进程
:class:`~core.transport.LocalTransport`、跨进程
:class:`~node_service.client.HttpTransport`）—— 不动任何一条真链路，
也不会漏掉某条"只有跨进程才会走到"的路径。

两种模式：区别只在**数据还在不在**
----------------------------------
``down``（掉线）
    机器联系不上。对应"网线拔了 / 进程挂了 / 机房断电"。

``destroyed``（永久损毁）
    机器联系不上，而且**在现实里**它存的数据也没了。对应"地震 / 海啸 / 机房烧了"。
    ★ 这一档的意义在于**不可逆**：如果它真的发生，机器回来是**空的**，
      只能靠副本重建；某个块的每一份副本都在这张名单里 ⇒ 那块真丢了。

★★★ 重要：**它一个字节都不会删。**
    两种模式都**只做一件事**：把指定节点标成不可达。
    ``destroyed`` 与 ``down`` 的差别在于**影响面怎么解读** ——
    前者会告诉你"如果这是真的，这 N 块就永远回不来了"（:meth:`影响面`），
    但它绝不会去动节点上的数据。

    .. warning::

       **本模块绝不得调用任何破坏性接口**（如节点服务的 ``/node/reset``）。
       曾经的那个版本就是这么错的：它把"模拟磁盘被毁"实现成了真的去清
       节点数据库 —— 而跨进程模式下节点数据**只存在节点自己那份 SQLite 里**，
       协调者没有副本，清掉就是**真没了**。演练必须是**纯模拟、随时可撤**。

对业务的影响（正是要演示的东西）
--------------------------------
* **验证完整性 / 证据池聚合 / 试解密** —— 都要向节点取密文与凭证。
  主副本掉线时自动退到副本（多副本的价值就在这里）；
  **一份副本都取不到**时，那个块再也验不了、也解不开。
* **改块** —— 先要向持有它的节点取原值，再要广播新摘要，两个环节都会失败。
* **上传 / 追加** —— 新块被指派到掉线机器上时，这次写失败并留成
  "待补推"现场（:class:`~core.transport.WriteError`），与真实故障一致。

诚实边界
--------
它**不改变任何密码学**，也不改变"谁该持有哪一块"。它只做一件事：
让指定节点**联系不上**（读抛 :class:`~core.transport.TransportError`，
写逐台失败并如实汇总成 :class:`~core.transport.WriteError`）。
所以看到的失败，是**真代码**在"节点不可达"下的行为，不是装出来的画面。

★ 注意它**没有能力**让"本来就没起"的东西变真：节点进程一直在跑，
  是我们在请求那一层把它挡住的（见 :meth:`FaultyTransport._patch_inner_requests`）。
  这条边界要守住 —— 一旦有人偷懒去把"故障"实现成"真去删一下"，
  跨进程下节点数据只在节点自己的 SQLite 里，删了就真救不回来了。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .transport import NodeTransport, TransportError

__all__ = ["FaultyTransport", "MODE_DOWN", "MODE_DESTROYED"]

#: 两种故障模式。给接口与界面用的**字面量**，别在别处手写字符串
#: （写错了不会报错，只会让界面显示成另一种故障）。
MODE_DOWN = "down"
MODE_DESTROYED = "destroyed"

#: 与真实"联系不上"时形状**逐字段一致**的占位行。
#: ★ 必须自己造，不能让内层照报：同进程实现不知道我们标记了谁掉线，
#:   它会照常汇报那台机器的现状 —— 而协调者此刻**确实看不到**那些数。
#:   ``valid=False`` 与 ``held=0`` 都是刻意的"不可能值"：万一有人忘了看
#:   ``unreachable``，后续比对也会失败，而不是恰好撞上一个合法值。
_UNREACHABLE_ROW = {
    "vectors": [],
    "offsets": [],
    "held": 0,
    "valid": False,
    "proved": None,
    "fresh": False,
}


def _as_node_list(nodes) -> tuple[str, ...]:
    """把入参规整成节点名元组。

    ★ 挡掉最常见的一种误用：直接给一个**字符串**。
      ``tuple(str(n) for n in "n1")`` 会静默变成 ``('n', '1')`` ——
      然后报“不认识的节点 ['n','1']”，让人完全看不出是自己参数给错了，
      更坏的情况是恰好每个字符都是合法节点名时**静默做错事**。
    """
    if isinstance(nodes, str):
        raise TypeError(
            "节点名单要给一个序列（如 ['n1', 'n2']），不要直接给字符串 —— "
            "字符串会被逐字符拆开"
        )
    out = tuple(str(n) for n in nodes)
    if not out:
        raise ValueError("要指定至少一台节点")
    if any(not n for n in out):
        raise ValueError("节点名不能为空")
    return out


class FaultyTransport:
    """包住真传输层，按两张名单制造故障。完整实现 :class:`~core.transport.NodeTransport`。

    .. important::

       **它不替内层做密码学决定。**

       * **跨进程**实现：不可达要落到**请求**这一层（见
         :meth:`_patch_inner_requests`）—— 因为节点**进程其实还活着**，
         内层不包一层就会照常把更新推过去、还拿到 200，于是写入**成功**了，
         而面板却写着「改块 / 追加 / 截断 会被拦下」。包上之后，广播写
         会像真故障一样逐台失败、抛出带负载的
         :class:`~core.transport.WriteError`，留下**真的**待补推现场 ——
         恢复后点「补推」正好把它推给那几台。
       * **同进程**实现：广播写由 :meth:`_broadcast` **提前整次拦下**。
         那种实现没有 HTTP 负载可重发（补推无从谈起），
         半推成功的不一致会**永久留下**，所以宁可当成"这次写根本没开始"。
    """

    def __init__(
        self, inner: NodeTransport, *, down=(), destroyed=()
    ) -> None:
        self._inner = inner
        self._down: set[str] = set(str(n) for n in down)
        self._destroyed: set[str] = set(str(n) for n in destroyed)
        self.node_ids: tuple[str, ...] = tuple(getattr(inner, "node_ids", ()))
        #: 内层是不是"同进程"实现。判据是它有没有那份内存里的节点状态表 ——
        #: 有就是 ``LocalTransport``，广播写必须由我们摘名单。
        self._local = hasattr(inner, "states")
        self._validate()
        if not self._local:
            self._patch_inner_requests()

    def _patch_inner_requests(self) -> None:
        r"""让内层对故障节点的**每一次**请求都失败 —— 这才是"机器没了"的样子。

        ★ 为什么不靠"内层自己会连不上"：**节点进程其实还活着**。
          演练只在协调者这一侧标记"联系不上"，HTTP 那头一直好好应着。
          于是内层照常把更新推给它、并拿到 200 —— 写入**真的成功了**，
          而面板却写着「改块 / 追加 / 截断 会被拦下」。

          实测踩到：掉线一台之后改块 / 追加照样成功，
          待补推现场永远建不起来，「补推」这条路在演示里根本走不到。

          包上之后：广播写逐台收集失败 → 抛出带负载的 ``WriteError``
          → 协调者不推进自己的账、留下待补推现场；恢复后点一次「补推」，
          负载正好打给那几台 —— 与线上故障提示里描述的流程**逐字一致**。

        ★ ``_request`` 是 ``HttpTransport`` 的**唯一**出口（``_post`` / ``_get``
          都走它），所以包一处就全覆盖：取密文、检索凭证、存储证明、
          广播写、探活，全部一致地"连不上"。

        ★ 判据在**调用时**现取（不是包的时候快照）：名单随时会被
          :meth:`knock_out` / :meth:`restore` 改，包一层固定的就没用了。
        """
        inner = self._inner
        if getattr(inner, "_fault_drill_patched", False):
            return
        original = getattr(inner, "_request", None)
        if original is None:
            return

        def guarded_request(method, node_id, path, **kw):
            nid = str(node_id)
            if self.is_faulty(nid):
                raise TransportError(
                    f"{nid} 联系不上（故障演练：它被标成"
                    f"{'永久损毁' if nid in self._destroyed else '掉线'}）"
                )
            return original(method, node_id, path, **kw)

        try:
            inner._request = guarded_request  # type: ignore[method-assign]
        except (AttributeError, TypeError):  # 有 __slots__ 之类的实现：不改它
            return
        inner._fault_drill_patched = True  # type: ignore[attr-defined]

    def _validate(self) -> None:
        known = set(self.node_ids)
        bad = (self._down | self._destroyed) - known
        if bad:
            raise ValueError(f"不认识的节点 {sorted(bad)}；本集群是 {sorted(known)}")
        both = self._down & self._destroyed
        if both:
            raise ValueError(f"{sorted(both)} 不能同时算「掉线」和「永久损毁」")

    # -- 名单 ---------------------------------------------------------------

    @property
    def down(self) -> list[str]:
        """掉线（可恢复）的节点。"""
        return sorted(self._down)

    @property
    def destroyed(self) -> list[str]:
        """「永久损毁」的节点（**仅标记** —— 它们的数据一个字节都没动）。"""
        return sorted(self._destroyed)

    def faulty(self) -> frozenset[str]:
        """当前**联系不上**的节点 = 掉线 ∪ 损毁。"""
        return frozenset(self._down | self._destroyed)

    def is_faulty(self, node_id: str) -> bool:
        return node_id in self._down or node_id in self._destroyed

    def status(self) -> dict:
        return {
            "node_ids": list(self.node_ids),
            "down": self.down,
            "destroyed": self.destroyed,
            "faulty": sorted(self.faulty()),
            "local_mode": self._local,
        }

    # -- 制造 / 撤销 ---------------------------------------------------------

    def knock_out(self, nodes: Sequence[str], *, destroy: bool = False) -> dict:
        """★ 让若干台节点"没了"。

        :param destroy: ``True`` 标成**永久损毁**（现实里数据也没了）；
            ``False`` 标成**掉线**。

        ★★ **两者都不动数据。** 这个方法只会改两张名单里的名字。
        """
        picked = _as_node_list(nodes)
        unknown = [n for n in picked if n not in set(self.node_ids)]
        if unknown:
            raise ValueError(f"不认识的节点 {unknown}；本集群是 {list(self.node_ids)}")
        for n in picked:
            if destroy:
                self._down.discard(n)
                self._destroyed.add(n)
            elif n not in self._destroyed:
                self._down.add(n)
        return self.status()

    def restore(self, nodes: Sequence[str] | None = None) -> dict:
        """★ 撤销模拟：让节点"回来"。

        ★★ **数据从未被动过，所以回来时与出事前一模一样。**
        这正是它必须是纯模拟的理由：跨进程模式下节点数据只在节点自己那里，
        协调者没有副本 —— 一旦真的删了，就**真的救不回来**。
        """
        if nodes is None:
            targets = tuple(self.node_ids)
        else:
            targets = _as_node_list(nodes)
            unknown = [n for n in targets if n not in set(self.node_ids)]
            if unknown:
                # ★ 与 knock_out 保持同一口径：名字错就是错，不静默忽略。
                #   （以前这里会默默略过，于是“我明明恢复了 n9”看起来成功了，
                #   实际什么都没发生 —— 那种假成功最难查。）
                raise ValueError(
                    f"不认识的节点 {unknown}；本集群是 {list(self.node_ids)}"
                )
        for n in targets:
            self._down.discard(n)
            self._destroyed.discard(n)
        return self.status()

    # -- 内部工具 -----------------------------------------------------------

    def _why(self, nodes: Sequence[str], what: str) -> str:
        """统一的演练报错文案。**必须说清是模拟**，否则会被当成真故障去排查。

        ★ 一个字都不能写成 Markdown（如 ``**粗体**``）：这些文本会**原样**
          显示在界面上（``ElMessage`` / ``el-alert`` 收的是纯文本）。
          要强调就用「」。
        """
        parts = [
            f"{n}（{'永久损毁' if n in self._destroyed else '掉线'}）" for n in nodes
        ]
        return (
            f"{what}失败：{len(nodes)} 台存储节点在「故障演练」里被标记为不可用 —— "
            + "、".join(parts)
            + "。\n  这是模拟出来的故障，点「恢复」即可撤掉（永久损毁的机器回来是空的）。"
        )

    def _guard(self, node_id: str, what: str) -> None:
        """这台节点联系不上就抛。"""
        if node_id in self._destroyed:
            raise TransportError(
                f"{node_id} 已「永久损毁」（故障演练：模拟地震 / 海啸，磁盘没了），"
                f"无法{what}"
            )
        if node_id in self._down:
            raise TransportError(f"{node_id} 已「掉线」（故障演练），无法{what}")

    def _broadcast(self, call, what: str) -> None:
        """广播写：让内层跳过故障节点，或**干脆拦下整次写**。"""
        skipped = sorted(self.faulty())
        if not skipped:
            call()
            return
        if self._local:
            # ★★ 同进程实现**先拦下来**，不让内层跑。
            #
            #    广播写只要有一台没跟上，就已经"半成功"了：协调者**不**推进
            #    自己的账（它等着补推），而收到更新的那几台 δ 已经推前了 ——
            #    两边从此不一致。而 ``LocalTransport`` **不支持补推**
            #    （没有 HTTP 负载可重发），这个不一致就**永久留下**，
            #    之后任何操作都会以「节点算出的摘要与协调者不一致」
            #    这种指不出根因的形式炸掉。
            #
            #    所以宁可当成"这次写根本没开始"：干脆、可恢复、可解释。
            #    这**不是**粉饰故障 —— 掉线期间写本来就做不成，
            #    有差别的只是"留下烂摊子"还是"干净地拒绝"。
            raise TransportError(self._why(skipped, what))
        # ★ 跨进程：**让它自己去试**。故障节点的请求已被
        #   :meth:`_patch_inner_requests` 掐断，于是内层会逐台收集失败
        #   并抛出带负载的 ``WriteError`` —— 协调者不推进自己的账、
        #   现场留住，恢复后「补推」正是打给那几台。
        #   （以前这里指望"内层自己会连不上"，但节点进程其实活着，
        #    于是写居然成功了 —— 待补推现场建不起来。）
        call()

    # -- NodeTransport：生命周期 --------------------------------------------

    def set_crs(self, crs_dict: Mapping[str, object]) -> None:
        self._inner.set_crs(crs_dict)

    def report(self, *, verify: bool = False) -> list[dict]:
        bad = self.faulty()
        out: list[dict] = []
        for row in self._inner.report(verify=verify):
            nid = str(row.get("node_id", ""))
            if nid in bad:
                why = (
                    "永久损毁（故障演练）" if nid in self._destroyed else "掉线（故障演练）"
                )
                out.append(
                    {"node_id": nid, "unreachable": True, **_UNREACHABLE_ROW, "error": why}
                )
            else:
                out.append(row)
        return out

    # -- NodeTransport：读 ---------------------------------------------------

    def retrieve(self, node_id: str, offset: int, Q: Sequence[int]):
        self._guard(node_id, "取密文与检索凭证")
        return self._inner.retrieve(node_id, offset, Q)

    def pos_prove(self, node_id: str, offset: int, indices: Sequence[int]):
        self._guard(node_id, "回答存储证明挑战")
        return self._inner.pos_prove(node_id, offset, indices)

    def local_state(self, node_id: str):
        """同进程模式才有的调试入口 —— 演练时也按"联系不上"处理。"""
        self._guard(node_id, "读它的本地状态")
        return self._inner.local_state(node_id)  # type: ignore[attr-defined]

    # -- NodeTransport：写 ---------------------------------------------------

    def adopt(self, node_id: str, **kw) -> None:
        self._guard(node_id, "接收搬过来的块")
        self._inner.adopt(node_id, **kw)

    def apply_append(self, *, delta_old, delta_new, op_delta, witness, assignments, blobs) -> None:
        self._broadcast(
            lambda: self._inner.apply_append(
                delta_old=delta_old,
                delta_new=delta_new,
                op_delta=op_delta,
                witness=witness,
                assignments=assignments,
                blobs=blobs,
            ),
            "追加",
        )

    def apply_update(self, *, delta_new, op_delta, witness, blobs) -> None:
        self._broadcast(
            lambda: self._inner.apply_update(
                delta_new=delta_new,
                op_delta=op_delta,
                witness=witness,
                blobs=blobs,
            ),
            "改块",
        )

    def drop(self, offset: int, positions: Mapping[str, Sequence[int]]) -> None:
        self._broadcast(
            lambda: self._inner.drop(offset, positions),
            "截断（让节点交回末尾的块）",
        )

    def apply_delete(self, *, delta_new, op_delta, witness) -> None:
        self._broadcast(
            lambda: self._inner.apply_delete(
                delta_new=delta_new, op_delta=op_delta, witness=witness
            ),
            "删除",
        )

    # -- 同进程实现额外提供的两个方法（落库要用） ----------------------------

    def __getattr__(self, name: str):
        """其余方法（如 ``retry_write`` / ``export_states`` / ``close``）原样透传给内层。

        ★★ **必须走 ``__getattr__``、不能在上面写一个同名方法。**
           ``backend/manager.py`` 用 ``getattr(transport, "export_states", None)``
           判断"是不是同进程模式" —— 一旦本类**定义**了 ``export_states``，
           那个 ``getattr`` 就永远不是 ``None``，跨进程模式会误入只属于同进程
           的落库分支，在每一次写入成功后炸一个 ``AttributeError``。
           交给 ``__getattr__`` 转发：内层没有它就真的没有，``getattr`` 会正确
           地给 ``None``。

        ★ dunder 不转发：pickle / copy 会探测一堆双下划线属性，
          转发过去只会得到莫名其妙的 AttributeError 或死循环。
        """
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return getattr(self._inner, name)

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        if not self.faulty():
            return f"FaultyTransport(无故障, 底层={type(self._inner).__name__})"
        return (
            f"FaultyTransport(掉线={self.down}, 损毁={self.destroyed}, "
            f"底层={type(self._inner).__name__})"
        )
