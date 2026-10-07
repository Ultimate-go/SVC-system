"""节点服务 —— 一台存储服务器就是一个独立进程。

跑法::

    python -m node_service --node-id node-1 --port 9101 --data-dir nodes/node-1

它对外的接口很少，而且**没有一个是"把状态设成这样"**：

====================  ====================================================
``GET  /node/health``   活着吗，有没有状态
``POST /node/crs``    收下公开参数（**绝不自制**）
``GET  /node/report``   我持有哪些下标、在哪个 ``n`` 上、视图是否合法
``POST /node/retrieve`` 给你哪些下标的内容 + 证据 + 密文
``POST /node/append``   一次追加发生了，你负责这几个新位置，自己跟上
``POST /node/adopt``    收下一份**别的节点**给的检索凭证（台数变小时搬块用）
====================  ====================================================

最后一条是关键：协调者发的是"**发生了什么**"（:math:`\\Delta` 与
:math:`\\Upsilon_\\Delta`）和"你负责哪些位置"，状态由节点自己算。
这也是这个服务唯一需要小心的地方 —— 它必须**独立算出**与协调者逐位相同的
新摘要，算出来不一致就报错（而不是盲信协调者给的摘要）。

.. important::

   **这五个接口全部要带节点令牌**（请求头 ``X-Node-Token``）。
  ``/node/reset`` 是破坏性的、``/node/retrieve`` 会把该节点持有的全部密文
  吐出来 —— 这些不能只靠"绑回环"护着（绑哪个地址是**部署参数**）。
  令牌由 ``create_node_app(..., token=...)`` 必传，见 :data:`NODE_TOKEN_HEADER`。
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from core import GlobalSession, NodeRejected, NodeState, crs_from_dict
from vds.digest import Digest, LocalView
from vds.pos import Challenge, pos_prove
from vds.updates import UpdateDelta, UpdateWitness
from svc.types import Opening

from .persist import NodeDB

__all__ = ["NodeRuntime", "create_node_app", "NODE_TOKEN_HEADER"]

#: 节点鉴权用的请求头。
#:
#: ★ 为什么这件事不能省：节点上的 ``/node/reset`` 会清空该节点的整个库，
#: ``/node/retrieve`` 不判断"你是不是该拿这些下标"就把内容吐出去。
#: 此前唯一的防护是"绑回环"，而绑哪个地址是**部署参数**、不是代码里的控制
#: —— 有人为了演示跨机部署把 ``--host`` 改成 ``0.0.0.0``，这几条接口就直接敞着了。
#:
#: 演示级用**共享**令牌（节点与协调者同一个值，从 ``VDS_NODE_TOKEN`` 注入）。
#: 生产应当每节点独立、可轮换；再往上就是 mTLS。
NODE_TOKEN_HEADER = "X-Node-Token"


class Utf8JSONResponse(JSONResponse):
    """带 charset 的 JSON 响应（与后端保持一致，省得客户端按 Latin-1 解）。"""

    media_type = "application/json; charset=utf-8"


# ---------------------------------------------------------------------------
# 运行态
# ---------------------------------------------------------------------------

class NodeRuntime:
    """一台节点的运行态：公开参数 + 本地状态 + 自己的库。"""

    def __init__(self, node_id: str, data_dir: str | Path) -> None:
        self.node_id = node_id
        self.data_dir = Path(data_dir)
        self.db = NodeDB(self.data_dir / "node.db")
        self._lock = threading.RLock()
        self.session: GlobalSession | None = None
        self.state: NodeState | None = None
        self._reload()

    # -- 启动 / 收下公开参数 ------------------------------------------------

    def _reload(self) -> None:
        """从自己的库恢复。**公开参数来自库里那几行，不重新生成。**

        ★ 新方案下一台节点可能同时参与好几份文件，所以这里是**逐份恢复**：
        库里 ``state`` 表每份文件一行，密文按 ``(offset, 局部块号)`` 存。
        """
        crs = self.db.load_crs()
        if crs is None:
            return
        self.session = GlobalSession(crs_from_dict(crs))
        self.state = NodeState(self.node_id, self.session)
        states = self.db.load_states()
        blobs = self.db.load_blobs()
        for off, saved in states.items():
            U, C, n = saved["delta"]
            if int(n) == 0:
                # ★ 老库（或半途失败）里可能留下一个 n=0 的空段。这种段在协调者
                #   眼里**不存在**（它是直接删行的），读回来就成了分叉 ——
                #   表现是启动自检报"停在 (off, 0)"并拒绝启动。跳过，顺手删行。
                self.db.delete_state(int(off))
                continue
            S_I, Lam = saved["st"]
            I = tuple(saved["I"])
            # ★★ 重建 δ 时**必须**把 identity 与 chunks 一并带上 ——
            #    它们决定「第 i 块配哪个素数」（见 ``node_service/persist.py``
            #    的 :meth:`_migrate` 与 ``client._delta_dict``）。
            #    漏掉 identity 的后果不是启动就报错，而是**重启后一切看着
            #    正常、下一次更新才以「S_I 校验失败」炸掉** —— 那时协调者、
            #    库、网络全是好的，排查方向会被完全带偏。
            self.state.restore(
                off,
                Digest(
                    U=int(U),
                    C=int(C),
                    n=int(n),
                    offset=int(off),
                    chunks=tuple(
                        (int(a), int(b)) for a, b in saved.get("chunks", ()) or ()
                    ),
                    identity=str(saved.get("identity", "") or ""),
                ),
                Opening(int(S_I), int(Lam), I),
                I,
                tuple(saved["FI"]),
                blobs=blobs.get(off, {}),
            )

    def adopt_crs(self, crs_dict: dict) -> dict:
        """收下协调者给的公开参数。

        已经有一套时**必须逐位一致** —— 不一致说明这个节点连错了协调者，
        继续跑下去只会算出一堆验证不过的东西。宁可当场报错。
        """
        with self._lock:
            existing = self.db.load_crs()
            if existing is not None:
                if (
                    str(existing["N"]) != str(crs_dict["N"])
                    or str(existing["g"]) != str(crs_dict["g"])
                    or int(existing["l"]) != int(crs_dict["l"])
                    or int(existing["n_max"]) != int(crs_dict["n_max"])
                ):
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        "本节点已有另一套公开参数。要换一套必须清空本节点的数据 "
                        "（N、g 变了就是另一个群，旧状态全部失效）。",
                    )
                return {"ok": True, "changed": False, "node_id": self.node_id}

            self.db.set_meta(
                node_id=self.node_id,
                N=str(crs_dict["N"]),
                g=str(crs_dict["g"]),
                l=int(crs_dict["l"]),
                n_max=int(crs_dict["n_max"]),
            )
            self.session = GlobalSession(crs_from_dict(crs_dict))
            # 公开参数刚落地，把库里已有的视图一并恢复出来（可能有好几份文件）
            self._reload()
            if self.state is None:
                self.state = NodeState(self.node_id, self.session)
            return {"ok": True, "changed": True, "node_id": self.node_id}

    def _need_session(self) -> GlobalSession:
        if self.session is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "还没有公开参数 —— 请先让协调者调用 POST /node/crs",
            )
        return self.session

    # -- 读 -----------------------------------------------------------------

    def report(self, verify: bool) -> dict:
        with self._lock:
            self._need_session()
            assert self.state is not None
            row = self.state.describe(verify=verify)
            row["db"] = self.db.stats()
            return row

    def retrieve(self, offset: int, indices: list[int]) -> dict:
        """取回某些块的 ``(π_Q, 密文段)``。``indices`` 是它的**文件内局部块号**。"""
        with self._lock:
            self._need_session()
            assert self.state is not None
            off = int(offset)
            want = sorted({int(i) for i in indices})
            if not want:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "下标集合不能为空")
            if not self.state.has_view(off):
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    f"本节点没有 offset={off} 那份向量"
                    f"（现持有 {list(self.state.offsets)}）",
                )
            held = self.state.I_of(off)
            missing = sorted(set(want) - set(held))
            if missing:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    f"本节点在 offset={off} 那份里不持有下标 {missing}"
                    f"（持有 {list(held)}）",
                )
            pi_Q, cts = _retrieve_from(self.state, off, want)
            return {
                "node_id": self.node_id,
                "indices": want,
                "proof": {
                    "S_I": str(pi_Q.S_I),
                    "Lambda_I": str(pi_Q.Lambda_I),
                    "I": list(pi_Q.I),
                },
                "blobs": [ct.hex() for ct in cts],
            }

    def pos_prove(self, offset: int, indices: list[int]) -> dict:
        """回答一次**存储证明**（PoR）挑战 —— 只回自己那一份。

        ``Q := I ∩ r``。一个下标都不沾就返回**空份额**（``proof`` 为 ``None``）——
        它是合法的回答，不是错误：聚合阶段会跳过它，而它参与不了验证
        （空份额不带群元素，拿它去验只会得到看不出根因的结果）。

        这里**故意不回密文**：PoR 的目的就是“不下载任何内容就确认数据还在”。
        想拿内容请走 ``/node/retrieve``。
        """
        with self._lock:
            self._need_session()
            assert self.state is not None
            want = sorted({int(i) for i in indices})
            if not want:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "挑战下标不能为空")
            off = int(offset)
            ch = Challenge(indices=tuple(want), n=self.state.delta_of(off).n)
            proof = pos_prove(self.state.node(off), ch)
            if not proof.has_proof():
                return {
                    "node_id": self.node_id,
                    "indices": [],
                    "values": [],
                    "proof": None,
                }
            return {
                "node_id": self.node_id,
                "indices": list(proof.Q),
                "values": [str(v) for v in proof.F_Q],
                "proof": {
                    "S_I": str(proof.pi_Q.S_I),
                    "Lambda_I": str(proof.pi_Q.Lambda_I),
                    "I": list(proof.pi_Q.I),
                },
            }

    # -- 写 -----------------------------------------------------------------

    def append(self, payload: dict) -> dict:
        """一次追加：先 ``ApplyUpdate`` 跟上新 ``n``，再 ``AddStorage`` 收下指派的位置。

        ★ 这个方法**幂等**：同一批追加被投递两次不会追加两遍。

        靠的不是幂等键，而是下面那句 ``self.state.delta != delta_old`` ——
        ``delta_old`` 描述的是"变更**前**"的状态，而节点应用过一次之后
        ``state.delta`` 已经前进了，所以重放时两者永不可能相等，直接 409。

        （守门测试：``tests/test_nodes.py::Test节点行为::test_过时摘要的更新会被拒``。）
        """
        with self._lock:
            self._need_session()
            assert self.state is not None

            delta_old = _delta_in(payload["delta_old"])
            delta_new = _delta_in(payload["delta_new"])
            op_delta = _op_in(payload["op_delta"])
            witness = _witness_in(payload["witness"])
            assigned = [int(x) for x in payload.get("assigned", [])]
            blobs = {int(k): bytes.fromhex(v) for k, v in payload.get("blobs", {}).items()}

            # ★ 按 offset 定位“这份文件”：一台节点可能同时参与好几份，
            #   各有一份视图。``delta_old.offset`` 就是这份文件的段起点。
            off = int(delta_old.offset)
            if self.state.has_view(off) and self.state.delta_of(off) != delta_old:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"本节点在 offset={off} 那份文件上停在 n="
                    f"{self.state.delta_of(off).n}，但协调者认为它在 "
                    f"n={delta_old.n} —— 它漏掉了一次更新，需要重新同步",
                )

            try:
                if not self.state.has_view(off):
                    # 这份文件还没参与过：从空视图起步（π_∅ = (U_n, C_n)）
                    self.state.adopt_empty(delta_new)
                else:
                    self.state.adapt(off, op_delta, witness, delta_new)
            except NodeRejected as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"拒绝更新：{exc}") from exc

            # ★ 节点**独立算出**的摘要必须与协调者算的逐位相同。
            #   这一步不能省：盲信协调者给的 delta_new 就等于放弃了
            #   "节点自己跟上更新"这条性质。
            if self.state.delta_of(off) != delta_new:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"本节点算出的摘要与协调者不一致（{self.state.delta_of(off).n} vs "
                    f"{delta_new.n}）—— 更新密钥或新值有问题",
                )

            try:
                self.state.absorb(
                    delta_old=delta_old,
                    new_positions=tuple(op_delta.K),
                    assigned=assigned,
                    values_all=tuple(op_delta.F_new),
                    blobs=blobs,
                    delta_new=delta_new,
                )
            except (NodeRejected, ValueError) as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"合并失败：{exc}") from exc

            if not self.state.check(off):
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "应用更新后本地视图不合法 —— 拒绝落库",
                )

            # 落库：状态整行覆写（这份文件一行），密文只写新增的
            self.db.save_state(
                off,
                delta=self.state.delta_of(off),
                st=self.state.st_of(off),
                I=self.state.I_of(off),
                FI=self.state.FI_of(off),
            )
            self.db.save_blobs(off, blobs)
            return {
                "ok": True,
                "node_id": self.node_id,
                "n": self.state.delta_of(off).n,
                "held": len(self.state.I_of(off)),
                "span": self.state.describe(offset=off)["span"],
            }

    def update(self, payload: dict) -> dict:
        """一次**修改**（``mod``）：节点自己跟上新摘要，再换掉那一块的密文。

        与 :meth:`append` 有两处不同：

        1. **不** ``absorb`` —— 位置集合 ``I`` 不变，只是值变了
           （``vds`` 层 ``_mod_node_state``：「位置在 I 内的：只有值要换，证明不动」）；
        2. ``delta_old`` 不由协调者给 —— 节点的**当前**摘要就是「改动前」。
           ``mod`` 不改变 ``U`` 但会改变 ``C``，所以下面那道
           ``state.delta != delta_new`` 交叉校验照样挡得住掉队的节点。

        ``del`` 不走这条路：它的 :math:`\\Upsilon_\\Delta` 里要带
        :math:`\\pi_K`，而 :func:`_witness_in` 目前把它解析成 ``None``。
        与其默认当 ``None`` 静默走歪，不如在这里直接报 400。
        """
        with self._lock:
            self._need_session()
            assert self.state is not None

            delta_new = _delta_in(payload["delta_new"])
            op_delta = _op_in(payload["op_delta"])
            witness = _witness_in(payload["witness"])

            if op_delta.op != "mod":
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"/node/update 只接受 mod，收到 {op_delta.op!r}"
                    f"（add 走 /node/append；del 走 /node/delete —— 它的 Υ∆ 要带 π_K）",
                )
            off = int(delta_new.offset)
            if not self.state.has_view(off):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "本节点还没有 offset="
                    f"{off} 那份向量的状态，改不了 —— mod 只能作用于已分发下去的数据",
                )

            blobs = {
                int(k): bytes.fromhex(v) for k, v in payload.get("blobs", {}).items()
            }

            off = int(delta_new.offset)

            try:
                self.state.adapt(off, op_delta, witness, delta_new)
            except NodeRejected as exc:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, f"拒绝更新：{exc}"
                ) from exc

            # ★ 与 append 同一道交叉验证：节点独立算出的摘要必须与协调者逐位相同。
            if self.state.delta_of(off) != delta_new:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"本节点算出的摘要与协调者不一致（{self.state.delta_of(off).n} vs "
                    f"{delta_new.n}）—— 更新密钥或新值有问题",
                )

            try:
                self.state.replace_blobs(off, blobs)
            except NodeRejected as exc:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, f"换密文失败：{exc}"
                ) from exc

            if not self.state.check(off):
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "应用更新后本地视图不合法 —— 拒绝落库",
                )

            # 落库：状态整行覆写（C 变了），密文按 (offset, 局部块号) upsert
            self.db.save_state(
                off,
                delta=self.state.delta_of(off),
                st=self.state.st_of(off),
                I=self.state.I_of(off),
                FI=self.state.FI_of(off),
            )
            self.db.save_blobs(off, blobs)
            return {
                "ok": True,
                "node_id": self.node_id,
                "n": self.state.delta_of(off).n,
                "held": len(self.state.I_of(off)),
                "span": self.state.describe(offset=off)["span"],
            }

    def drop(self, payload: dict) -> dict:
        """交回一部分数据（论文 ``StrgNode.RmvStorage``）—— ``del`` 的前置步骤。

        为什么要交回：``vds.updates._apply_del`` 要求每个节点与待删集合 ``K``
        「要么全包含、要么全不相交」，**部分相交直接报错**；而我们的分片是
        「轮转 + 每块 2 份副本」，一个跨台的末尾区间几乎必然让某些节点部分相交。

        ★ **幂等**：本来就不在本节点手里的下标直接跳过（``dropped`` 为空）。
        否则「交回了 2 台、第 3 台掉线」之后整个截断就没法重来了。

        落库时**必须真删密文**（``delete_blobs``）—— 只把名字从 ``I`` 里抹掉
        会让 :meth:`~core.node_state.NodeState.check` 抓到「下标与密文对不上」
        而拒绝落库，而且「删掉的数据还躺在磁盘上」本身就是删除语义的反面。
        """
        with self._lock:
            self._need_session()
            off = int(payload.get("offset", -1))
            if self.state is None or not self.state.has_view(off):
                return {
                    "ok": True,
                    "node_id": self.node_id,
                    "dropped": [],
                    "held": 0,
                    "span": "—",
                    "removed": 0,
                    "note": "本节点不参与这份向量，无需交回",
                }
            positions = [int(x) for x in payload.get("positions", [])]
            if not positions:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, "要交回的下标不能为空"
                )
            # ★ 线路上来的是**全局位置号**（协调者记账用那一套），而本地视图
            #   ``I`` 与密文表用的是**文件内局部块号**。两套下标长得一样，
            #   混用**不会报错**，只会悄悄删错块（然后在 check 里以
            #   “本地视图不合法”收场）。转换口径与 ``LocalTransport.drop``
            #   逐字一致，否则同进程与跨进程会走出两种行为。
            idx_of = {
                g: i for i, g in enumerate(self.state.delta_of(off).positions)
            }
            try:
                local = [idx_of[p] for p in positions]
            except KeyError as exc:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"要交回的位置 {exc.args[0]} 不属于本节点这份文件",
                ) from exc
            try:
                out = self.state.drop(off, local)
            except NodeRejected as exc:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, f"交回失败：{exc}"
                ) from exc
            self.db.save_state(
                off,
                delta=self.state.delta_of(off),
                st=self.state.st_of(off),
                I=self.state.I_of(off),
                FI=self.state.FI_of(off),
            )
            removed = self.db.delete_blobs(off, out["dropped"])
            return {**out, "node_id": self.node_id, "removed": removed}

    def adopt(self, payload: dict) -> dict:
        """接收一批**已经承诺过**的位置（``StrgNode.AddStorage`` 的凭证入口）。

        这是节点侧唯一一个「把非本次追加的数据收进来」的入口，用途只有一个：
        **台数变小**时，把即将被摘掉的机器上的块搬到留下来的机器上。

        与 :meth:`append` 的关键差别是**前置材料不同**：
        ``append`` 收的是刚产生的新位置，那批位置的证据恰好是旧摘要本身；
        这里收的位置早就承诺过了，必须由**当前持有者**拆出一份
        :math:`\\pi_Q`（协调者去要、然后原样转交，它自己造不出来）。

        ★ 本方法**自己验凭证**（``NodeState.adopt`` 里 ``verify_cert=True``）：
        协调者是搬运工，不是权威 —— 一份来源不明的份额如果直接合并进来，
        本节点会被悄悄毒掉，而 :meth:`check` 只比对下标集合、不比内容。

        .. important::

           这一步**不改变** ``δ``：``n`` 不变、``C`` 不变，变的只是
           「谁持有哪些下标」。所以它不需要 :math:`\\Upsilon_\\Delta`，
           也就与两段式更新那套机制完全正交。
        """
        with self._lock:
            self._need_session()
            if self.state is None:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "本节点还没有公开参数 —— 请先让协调者调用 POST /node/crs",
                )
            raw = payload.get("proof") or {}
            if not raw:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "缺少 proof（π_Q）")
            proof = Opening(
                int(raw["S_I"]),
                int(raw["Lambda_I"]),
                tuple(int(x) for x in raw["I"]),
            )
            positions = [int(x) for x in payload.get("positions", [])]
            values = [int(x) for x in payload.get("values", [])]
            blobs = {
                int(k): bytes.fromhex(v) for k, v in payload.get("blobs", {}).items()
            }
            if not positions:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "要接收的下标不能为空")
            off = int(payload.get("offset", -1))
            try:
                out = self.state.adopt(off, positions, values, proof, blobs=blobs)
            except NodeRejected as exc:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, f"接收失败：{exc}"
                ) from exc
            if not self.state.check(off):
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "接收后本地视图不合法 —— 拒绝落库",
                )
            # 状态整行覆写（I / F_I 都变大了），密文只写新收的这几段
            self.db.save_state(
                off,
                delta=self.state.delta_of(off),
                st=self.state.st_of(off),
                I=self.state.I_of(off),
                FI=self.state.FI_of(off),
            )
            self.db.save_blobs(off, blobs)
            return {
                "ok": True,
                "node_id": self.node_id,
                "n": self.state.delta_of(off).n,
                "adopted": out["adopted"],
                "held": out["held"],
                "span": out["span"],
            }

    def delete(self, payload: dict) -> dict:
        """一次删除（``del``）：跟上新摘要，并把被删掉的那些密文**真删掉**。

        与 :meth:`update`（``mod``）的三点不同：

        1. :math:`\\Upsilon_\\Delta` 里**带** :math:`\\pi_K`（它就是新摘要本身），
           所以 :func:`_witness_in` 要解析它；
        2. 节点持有的集合会**变小**（``_apply_del`` 给出新的 ``I``）；
        3. 因此必须**真删密文** —— 只改 ``I`` 会让 :meth:`check
           <core.node_state.NodeState.check>` 判处"下标与密文对不上"。

        ``del`` 单独一个路由（而不是并进 :meth:`update`）是为了让「补推」机制
        原样复用：``core.transport.WriteError`` 的 ``op`` 就是路由名，
        ``retry_write`` 直接拼 ``/node/{op}`` —— 所以 ``op="delete"``
        天然对上 ``/node/delete``，一行都不用改。
        """
        with self._lock:
            self._need_session()
            off = int(payload.get("offset", -1))
            if self.state is None or not self.state.has_view(off):
                return {
                    "ok": True,
                    "node_id": self.node_id,
                    "n": -1,
                    "held": 0,
                    "span": "—",
                    "removed": 0,
                    "note": "本节点不参与这份向量，无需跟上",
                }
            delta_new = _delta_in(payload["delta_new"])
            op_delta = _op_in(payload["op_delta"])
            witness = _witness_in(payload["witness"])
            if op_delta.op != "del":
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"/node/delete 只接受 del，收到 {op_delta.op!r}"
                    f"（add 走 /node/append；mod 走 /node/update）",
                )
            # 必须在 apply_delete **之前**记下来：它会把 I 换成新的（更小的）那个
            gone = sorted(set(op_delta.K) & set(self.state.I_of(off)))
            try:
                self.state.apply_delete(off, op_delta, witness, delta_new)
            except NodeRejected as exc:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, f"拒绝删除更新：{exc}"
                ) from exc
            # （apply_delete 内部已经做完"节点自己算的摘要 == 协调者给的"这道交叉校验）
            removed = self.db.delete_blobs(off, gone)
            # ★ 整段被删光（n = 0）时，apply_delete 已经把这一段**整个摘掉**了 ——
            #   这时绝不能再 delta_of / I_of / describe 去读它（那几个都按 off 查，
            #   段没了就是 KeyError，表现成 /node/delete 回 500，而协调者只知道
            #   "有节点没能跟上这次删除"，真正的报错句一点也看不到）。
            #   该做的是**跟着把它从库里删掉**：协调者那边本来就是直接删行的，
            #   两边对"现在有哪些段"必须一致，否则节点重启时就分叉了。
            if not self.state.has_view(off):
                self.db.delete_state(off)
                return {
                    "ok": True,
                    "node_id": self.node_id,
                    "n": 0,
                    "held": 0,
                    "span": "—",
                    "removed": removed,
                    "dropped": list(gone),
                    "note": "这一段已被删空，节点已把它整个摘掉",
                }
            self.db.save_state(
                off,
                delta=self.state.delta_of(off),
                st=self.state.st_of(off),
                I=self.state.I_of(off),
                FI=self.state.FI_of(off),
            )
            return {
                "ok": True,
                "node_id": self.node_id,
                "n": self.state.delta_of(off).n,
                "held": len(self.state.I_of(off)),
                "span": self.state.describe(offset=off)["span"],
                "removed": removed,
                "dropped": list(gone),
            }

    def reset(self) -> dict:
        """清空本节点（演示重置）。"""
        with self._lock:
            self.db.wipe()
            self.session = None
            self.state = None
            return {"ok": True, "node_id": self.node_id}


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------

def _delta_in(o: dict) -> Digest:
    r"""线格式 → :class:`~vds.digest.Digest`（与 ``client._delta_dict`` 一一对应）。

    ★ 三个字段都是**前提**，少一个都会以极难归因的方式炸掉：

    * ``offset`` 是“哪一份向量”的标识（节点按它存视图）。缺了它，
      新文件会退回第 0 段，把老文件那份视图覆盖掉。
    * ``chunks`` 让节点能建出“局部块号 → 素数”的视图（老路径）。
    * ``identity`` 决定「第 ``i`` 块配哪个素数」（新路径）。**缺了它，
      节点会静默退回旧路径查全局素数表** —— 于是 :math:`e_i` 全错，
      节点侧 :meth:`~core.node_state.NodeState.absorb` 报

          S_I 校验失败：S_I^{e_I} ≠ U_n，S_I 被伪造或下标集合不对

      而协调者只看到“有 N 台节点没跟上”。**这个坑真踩过**：
      身份素数改造完成后本地 221 条断言全绿（走 LocalTransport），
      跨进程每一次上传都 503。所以这一行不是可有可无的兼容代码。

    ★ 缺字段时**退化成老路径**（``identity=""``）而不是报错 —— 这是有意的：
    老协调者（改造前）发的请求体里本来就没有这个字段，而那时所有文件
    都是老坐标，退化成老路径恰好是对的。
    """
    raw = o.get("chunks") or []
    return Digest(
        U=int(o["U"]),
        C=int(o["C"]),
        n=int(o["n"]),
        offset=int(o.get("offset", 0)),
        chunks=tuple((int(a), int(b)) for a, b in raw),
        identity=str(o.get("identity", "") or ""),
    )


def _op_in(o: dict) -> UpdateDelta:
    return UpdateDelta(
        op=o["op"], K=[int(x) for x in o["K"]], F_new=[int(x) for x in o["F_new"]]
    )


def _witness_in(o: dict) -> UpdateWitness:
    """解析更新密钥 :math:`\\Upsilon_\\Delta`。

    ``del`` 的 :math:`\\Upsilon_\\Delta` 里**带** :math:`\\pi_K`
    （它就是新摘要本身），``mod`` / ``add`` 的没有 —— 所以它是可选字段。
    ★ 以前这里把它一律解析成 ``None``（当时没有 del 路径），
    那样一个 del 请求会静默地被当成“没有 π_K”而走歪 —— 现在按字段有无解析。
    """
    raw = o.get("pi_K")
    pi_K = None
    if raw is not None:
        pi_K = Opening(
            int(raw["S_I"]),
            int(raw["Lambda_I"]),
            tuple(int(x) for x in raw["I"]),
        )
    return UpdateWitness(
        op=o["op"],
        K=[int(x) for x in o["K"]],
        S_K=int(o["S_K"]),
        F_K=[int(x) for x in o.get("F_K", [])],
        pi_K=pi_K,
    )


def _retrieve_from(state: NodeState, offset: int, want: list[int]):
    """取 ``(π_Q, 密文段)``。走 ``NodeState.retrieve`` + 自己的密文表。

    ★ 回给调用方的是**密文**而不是分量 —— 分量一律由验证方自己从密文重算，
    节点声称的那一份不参与判定（见 ``core/store.py`` 模块说明）。
    """
    off = int(offset)
    _F_Q, pi_Q = state.retrieve(off, want)
    cts = tuple(state.blobs_of(off)[i] for i in want)
    return pi_Q, cts


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------

def create_node_app(node_id: str, data_dir: str | Path, *, token: str) -> FastAPI:
    """建一台节点的应用。

    :param token: 节点令牌。**必传，没有默认值。**

        故意不给默认值：一旦可以"不传就当不校验"，就会存在一个
        **没鉴权也跑得起来**的节点，而那种状态在答辩台上是致命的
        （"节点互不信任"那句话会当场站不住）。必传之后，
        "没鉴权的节点"这个状态根本不存在。
        空字符串也拒绝 —— 否则它就退化成"请求头空着就放行"。
    """
    if not token:
        raise ValueError(
            "节点令牌不能为空 —— 不传令牌就等于不鉴权，见 NODE_TOKEN_HEADER 的注释"
        )

    runtime = NodeRuntime(node_id, data_dir)

    def guard(request: Request) -> None:
        """节点鉴权：请求头里的令牌必须与配置的一致。"""
        got = request.headers.get(NODE_TOKEN_HEADER, "")
        # 用 compare_digest 而不是 ==：逐字节短路比较会把"前几位对了"泄露出去，
        # 理论上可用来逐位猜令牌。这里是常量时间比较。
        if not secrets.compare_digest(got, token):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                f"节点令牌不对或没带（请在 {NODE_TOKEN_HEADER} 里带上 VDS_NODE_TOKEN 的值）",
            )

    app = FastAPI(
        title=f"VDS 存储节点 {node_id}",
        description="一台存储服务器：只存每份文件的一段，自己算自己的证据。",
        version="0.2.0",
        default_response_class=Utf8JSONResponse,
        # ★ 全局依赖：**所有**路由都要令牌，包括以后新加的。
        #   逐条挂 dependency 容易漏，而漏掉的那一条就是真的漏洞。
        dependencies=[Depends(guard)],
    )
    app.state.runtime = runtime

    def rt(request: Request) -> NodeRuntime:
        return request.app.state.runtime

    @app.get("/node/health")
    def health(request: Request):
        r = rt(request)
        return {
            "ok": True,
            "node_id": r.node_id,
            "has_crs": r.session is not None,
            "has_state": bool(r.state and r.state.offsets),
            "db": r.db.stats(),
        }

    @app.post("/node/crs")
    def set_crs(payload: dict, request: Request):
        return rt(request).adopt_crs(payload)

    @app.get("/node/report")
    def report(request: Request, verify: int = 0):
        return rt(request).report(bool(verify))

    @app.post("/node/retrieve")
    def retrieve(payload: dict, request: Request):
        return rt(request).retrieve(
            int(payload.get("offset", -1)), payload.get("indices", [])
        )

    @app.post("/node/pos")
    def pos_prove_route(payload: dict, request: Request):
        return rt(request).pos_prove(
            int(payload.get("offset", -1)), payload.get("indices", [])
        )

    @app.post("/node/append")
    def append(payload: dict, request: Request):
        return rt(request).append(payload)

    @app.post("/node/update")
    def update(payload: dict, request: Request):
        return rt(request).update(payload)

    @app.post("/node/drop")
    def drop(payload: dict, request: Request):
        return rt(request).drop(payload)

    @app.post("/node/adopt")
    def adopt(payload: dict, request: Request):
        return rt(request).adopt(payload)

    @app.post("/node/delete")
    def delete(payload: dict, request: Request):
        return rt(request).delete(payload)

    @app.post("/node/reset")
    def reset(request: Request):
        return rt(request).reset()

    @app.get("/", include_in_schema=False)
    def root():
        return {"node": node_id, "hint": "见 /docs"}

    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="VDS 存储节点")
    ap.add_argument("--node-id", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument(
        "--token",
        default=os.environ.get("VDS_NODE_TOKEN", ""),
        help="节点令牌，默认取环境变量 VDS_NODE_TOKEN。「不能为空」（节点不鉴权就不许跑）",
    )
    args = ap.parse_args(argv)

    import uvicorn

    try:
        app = create_node_app(args.node_id, args.data_dir, token=args.token)
    except ValueError as exc:
        print(f"起不来：{exc}", file=sys.stderr)
        return 2

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
