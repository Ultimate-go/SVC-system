"""``HttpTransport`` —— 通过 HTTP 与独立进程里的节点说话。

它是 :class:`~core.transport.NodeTransport` 的第二种实现，与
:class:`~core.transport.LocalTransport` 走**同一份** ``NodeState`` 逻辑
（在节点进程里）—— 这一点很重要：如果本地模式与分布式模式各有一套实现，
两条路径迟早会悄悄分叉，测试也就失去意义了。

放在 ``node_service/`` 而不是 ``core/`` 是刻意的：``core`` 不该依赖 HTTP，
它只定义协议。
"""

from __future__ import annotations

import time
from typing import Mapping, Sequence

import httpx

from core.transport import TransportError, WriteError
from svc.types import Opening
from vds.pos import EMPTY_OPENING, PoSProof
from vds.updates import UpdateDelta, UpdateWitness

from .server import NODE_TOKEN_HEADER

__all__ = ["HttpTransport"]


class HttpTransport:
    """node_id → base_url 的映射，加一个 ``httpx.Client``。

    :param token: 节点令牌，**必传**。节点一律要求鉴权（见
        :data:`~node_service.server.NODE_TOKEN_HEADER` 的注释），
        协调者必须拿同一个值去说话。
    """

    def __init__(
        self,
        base_urls: Mapping[str, str],
        *,
        token: str,
        timeout: float = 120.0,
        client: httpx.Client | None = None,
        retries: int = 2,
        backoff: float = 0.1,
    ) -> None:
        """
        :param retries: **暂时的**错误（连不上 / 超时 / 502-504）额外重试几次。

            为什么需要它：一次上传/改块/追加要给**每一台**节点发一遍，
            任何一台在那几秒里重启，就会让**整次操作**报错、用户得重来。
            这类失败绝大多数是瞬态的，重试一两次就能消掉。

            刻意**不重试 4xx**：那是确定性错误（令牌不对、下标越界、视图不合法），
            重试只会把同一个错误拖慢几倍再报出来，而且会把真正的原因埋掉。
        :param backoff: 退避基数（秒）。第 n 次重试前等 ``backoff * 2^(n-1)``。
        """
        if not base_urls:
            raise ValueError("至少要给一台节点的地址")
        if not token:
            raise ValueError(
                "节点令牌不能为空 —— 节点要求鉴权，空令牌会被 401；"
                "跨进程模式请设 VDS_NODE_TOKEN"
            )
        self.base_urls = {k: v.rstrip("/") for k, v in base_urls.items()}
        self.node_ids: tuple[str, ...] = tuple(base_urls)
        self._owns_client = client is None
        # ★★★ ``trust_env=False`` 是**必须的**，不是洁癖 —— 它挡掉一整类
        #    “节点明明起好了，后端却说连不上”的假故障。实机踩到过，记录如下。
        #
        #    httpx 默认 ``trust_env=True``：除了读 ``HTTP_PROXY`` / ``HTTPS_PROXY``
        #    这些环境变量，**在环境变量为空时它还会回落到
        #    ``urllib.request.getproxies()``** —— 而在 Windows 上那个函数会去读
        #    **系统代理**（``HKCU\...\Internet Settings``，也就是 IE/设置面板里那
        #    一项，被 Clash / ProxyBridge / 各种 VPN 客户端改的就是它）。
        #
        #    于是：机器上开着系统代理 ``127.0.0.1:12450`` 而它没在跑时，
        #    连 ``http://127.0.0.1:9101/node/crs`` 都会被送去 12450 →
        #    ``[WinError 10061] 由于目标计算机积极拒绝``。
        #    报错里写的却是 node-1 —— 看起来像“节点没起来”，
        #    而 ``scripts/start_all.py`` 的探活（走 urllib）同时一切正常，
        #    因为 Windows 的 ``urllib`` 会查 ``ProxyOverride`` 并**绕过回环地址**，
        #    httpx 不做这一步。两边结论相反，排查方向直接带偏。
        #
        #    为什么正解是“不信任环境”而不是“加 no_proxy”：节点地址来自
        #    ``VDS_NODE_URLS``，是一份**显式给定**的清单 —— 协调者要说的就是
        #    那个地址，**任何情况下都不该把给节点的请求交给一个 HTTP 代理**。
        #    ``no_proxy`` 只挡得住环境变量那一路，挡不住上面那条注册表回落；
        #    ``trust_env=False`` 把两路一起关掉。本地回环也不需要 .netrc / 证书环境。
        self._client = client or httpx.Client(timeout=timeout, trust_env=False)
        self._headers = {NODE_TOKEN_HEADER: token}
        self._last_report: dict[str, dict] = {}
        self.retries = max(0, int(retries))
        self.backoff = max(0.0, float(backoff))

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "HttpTransport":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- 内部 ---------------------------------------------------------------

    #: 值得重试的 HTTP 状态码 —— 这三个都是“对方暂时处理不了”，不是请求本身错。
    _RETRY_STATUS = frozenset({502, 503, 504})

    def _request(
        self,
        method: str,
        node_id: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        retries: int | None = None,
        timeout: float | None = None,
    ) -> dict:
        """带退避重试的请求。连接类错误与 502/503/504 重试，4xx 立即报错。

        :param retries: 覆盖本次请求的重试次数（``None`` = 用实例默认值）。
            ★ 只读的**状态探活**会传 ``0``，理由见 :meth:`report`。
        :param timeout: 覆盖本次请求的超时（秒）。
        """
        url = f"{self.base_urls[node_id]}{path}"
        n_retries = self.retries if retries is None else max(0, int(retries))
        last: Exception | None = None
        for attempt in range(n_retries + 1):
            if attempt:
                time.sleep(self.backoff * (2 ** (attempt - 1)))
            try:
                kw: dict = {"headers": self._headers}
                if timeout is not None:
                    kw["timeout"] = timeout
                r = self._client.request(
                    method, url, json=json, params=params, **kw
                )
            except httpx.HTTPError as exc:
                last = exc
                continue
            if r.status_code in self._RETRY_STATUS:
                last = TransportError(
                    f"{node_id} 拒绝 {path}：{r.status_code} {r.text[:120]}"
                )
                continue
            if r.status_code >= 400:
                # 确定性错误：重试无意义，立即抛
                detail = ""
                try:
                    detail = r.json().get("detail", "")
                except Exception:  # noqa: BLE001 - 响应不是 JSON 就用原文
                    detail = r.text[:200]
                raise TransportError(f"{node_id} 拒绝 {path}：{r.status_code} {detail}")
            return r.json()

        # 重试机会用完了 —— 报错里要说清“试过几次”，否则会被当成“这台一直不行”
        tried = f"（已重试 {n_retries} 次）" if n_retries else ""
        if isinstance(last, TransportError):
            raise TransportError(f"{last.args[0]}{tried}")
        raise TransportError(f"{node_id} 通信失败（{path}）{tried}：{last}")

    def _post(
        self,
        node_id: str,
        path: str,
        payload: dict,
        *,
        retries: int | None = None,
        timeout: float | None = None,
    ) -> dict:
        return self._request(
            "POST", node_id, path, json=payload, retries=retries, timeout=timeout
        )

    #: 只读状态探活的超时上界（秒）。
    #:
    #: ★ 为什么探活要单独给一个**短**超时：实例默认的 ``timeout`` 是 120 秒
    #:   （那是给上传/改块那类真干活的请求的）。而“这台机器到底活着吗”
    #:   应该是个**快问快答** —— 一台真卡住的机器不应该把整个状态页拖住。
    PROBE_TIMEOUT = 10.0

    def _get(
        self,
        node_id: str,
        path: str,
        *,
        retries: int | None = None,
        timeout: float | None = None,
        **params,
    ) -> dict:
        """GET。``retries`` / ``timeout`` 是**关键字**参数，不会混进查询串。

        ★ 这个区分很重要：``params`` 会被原样当成 URL 查询参数发出去，
        所以 ``retries`` / ``timeout`` 必须放成 ``*`` 后面的关键字参数 ——
        否则 ``/node/report?retries=0&timeout=10`` 会被当真发给节点。
        """
        return self._request(
            "GET", node_id, path, params=params, retries=retries, timeout=timeout
        )

    # -- 协议实现 -----------------------------------------------------------

    def set_crs(self, crs_dict: Mapping[str, object]) -> None:
        """把公开参数告诉每台节点。

        节点**绝不能**自己生成 ``N``、``g`` —— 那会得到另一个群，
        所有验证必然失败。这也是跨进程后最容易踩的坑，所以走的是显式接口。
        """
        payload = {
            "N": str(crs_dict["N"]),
            "g": str(crs_dict["g"]),
            "l": int(crs_dict["l"]),  # type: ignore[arg-type]
            "n_max": int(crs_dict["n_max"]),  # type: ignore[arg-type]
        }
        for nid in self.node_ids:
            self._post(nid, "/node/crs", payload)

    def report(self, *, verify: bool = False) -> list[dict]:
        """每台节点的现状。

        一台**联系不上不会让整份报告失败** —— 那台被标成 ``unreachable``、
        持有集合记为空。这是多副本能起作用的前提：检索路径靠 ``report``
        知道“谁能给”，而如果这里直接抛，主副本一掉线整次查询就整体失败，
        副本就等于白存。

        ``宁可吵不要哑``这条原则没有丢，它挪到了**需要严格视图**的地方：

        * ``VectorStore.check()`` 见到 ``unreachable`` 直接拒绝下结论；
        * 启动守卫把“**所有**节点都联系不上”当致命错误（令牌配错也是这个表现）。

        ★★★ **这里刻意不重试**（``retries=0``）并只给一个短超时，
        与写入路径（上传 / 改块 / 追加，它们按实例默认值重试）刚好相反。
        理由是实测出来的：

        * 一台机器**死掉**时，连接被拒 + 退避重试 2 次在 Windows 上要
          **6.45 秒**（实测：3 次尝试 × ≈ 2.1 s）。
        * 而这个函数是**只读探活**：失败已经由调用方优雅处理（标 ``unreachable``），
          重试只是把“这台真死了”这个**早就知道的结论**推迟 6.5 秒报出来。
        * 后果很具体：``/api/nodes`` 要 6.5 s、``/api/status`` 要 13 s
          —— 存储节点页与集群页在节点掉线后会**卡十几秒**，
          而那正是要现场演示“掉一台看降级”的页。

        写入路径保留重试的理由没变（一次上传要给每一台发一遍，
        任何一台在那几秒里重启就会让整次操作报错、用户得重来）。
        """
        out: list[dict] = []
        for nid in self.node_ids:
            try:
                row = self._get(
                    nid,
                    "/node/report",
                    verify=1 if verify else 0,
                    retries=0,
                    timeout=self.PROBE_TIMEOUT,
                )
            except TransportError:
                out.append(
                    {
                        "node_id": nid,
                        "unreachable": True,
                        # n = -1 是刻意的“不合法值”：万一有人忘了看 unreachable，
                        # 后续的 n 比对也会失败，而不是恰好撞上一个合法值。
                        "n": -1,
                        "held": 0,
                        "indices": [],
                        "span": "—",
                        "valid": False,
                        "proved": None,
                        "fresh": False,
                    }
                )
                continue
            self._last_report[nid] = row
            out.append(row)
        return out

    def retrieve(
        self, node_id: str, Q: Sequence[int]
    ) -> tuple[Opening, tuple[bytes, ...]]:
        body = self._post(node_id, "/node/retrieve", {"indices": [int(i) for i in Q]})
        want = tuple(int(i) for i in body["indices"])
        p = body["proof"]
        pi = Opening(int(p["S_I"]), int(p["Lambda_I"]), tuple(int(i) for i in p["I"]))
        cts = tuple(bytes.fromhex(h) for h in body["blobs"])
        if not (len(want) == len(cts)):
            raise TransportError(
                f"{node_id} 返回的字段长度不一致："
                f"{len(want)} 个下标 / {len(cts)} 段密文"
            )
        return pi, cts

    def pos_prove(self, node_id: str, indices: Sequence[int]) -> PoSProof:
        """让一台节点回答一次存储证明（PoR）挑战。

        ``proof`` 为 ``None`` 表示节点回的是**空份额**（挑战没打到它）——
        那是合法回答，交给聚合阶段跳过，不要当成错误。
        """
        body = self._post(node_id, "/node/pos", {"indices": [int(i) for i in indices]})
        raw = body.get("proof")
        if raw is None:
            return PoSProof(Q=(), F_Q=(), pi_Q=EMPTY_OPENING)
        want = tuple(int(i) for i in body["indices"])
        values = tuple(int(v) for v in body["values"])
        if len(want) != len(values):
            raise TransportError(
                f"{node_id} 返回的 PoS 字段长度不一致："
                f"{len(want)} 个下标 / {len(values)} 个值"
            )
        pi = Opening(
            int(raw["S_I"]), int(raw["Lambda_I"]), tuple(int(i) for i in raw["I"])
        )
        return PoSProof(Q=want, F_Q=values, pi_Q=pi)

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

        与 :meth:`apply_append` 不是同一条路：那边送的是"本次追加刚产生的新位置"
        （证据就是旧摘要本身），这边送的是一份**来自另一台节点的检索凭证**
        :math:`(Q, F_Q, \\pi_Q)`，节点自己验过才合并。

        ★ 只发一台、失败就直接抛 ``TransportError``，而且**不建补推现场**：
        这不是一次全网状态推进（``δ`` 一个字节都不动），失败只影响"这一批块
        有没有搬成功"。调用方（``StoreManager.redistribute``）会把它如实记进
        ``lost`` 里汇报，而不是留一份需要人工补推的现场 ——
        补推机制是给"δ 已经推进、但有台没跟上"那种真正的不一致用的。

        :param blobs: ``下标 -> 密文段``（协调者刚从源节点取回来的）
        """
        if not positions:
            return
        payload = {
            "positions": [int(i) for i in positions],
            "values": [str(int(v)) for v in values],
            "proof": {
                "S_I": str(proof.S_I),
                "Lambda_I": str(proof.Lambda_I),
                "I": [int(i) for i in proof.I],
            },
            "blobs": {str(int(k)): bytes(v).hex() for k, v in (blobs or {}).items()},
        }
        self._post(node_id, "/node/adopt", payload)

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
        """把一次追加通知**每一台**节点。

        抽出一个 ``_append_payload`` 是为了本地测试能复用同一份序列化逻辑，
        免得"测试里发的东西"和"生产里发的东西"长得不一样。
        """
        failures: list[dict] = []
        payloads: dict[str, dict] = {}
        for nid in self.node_ids:
            payload = _append_payload(
                delta_old=delta_old,
                delta_new=delta_new,
                op_delta=op_delta,
                witness=witness,
                assigned=tuple(assignments.get(nid, ())),
                blobs=blobs.get(nid, {}),
            )
            try:
                self._post(nid, "/node/append", payload)
            except TransportError as exc:
                # ★ 记 下“是哪一台、为什么” + **它没收到的那份请求体** ——
                #   前者给人看，后者给“补推”用（见 WriteError 的注释）。
                failures.append({"node": nid, "reason": str(exc)})
                payloads[nid] = payload

        if failures:
            # ★ 一次追加只要有一台没跟上，全网就处于不一致状态 ——
            #   必须让调用方知道，而不是当作成功。
            raise WriteError(
                "有节点没能跟上这次更新（全网现在不一致）：\n  "
                + "\n  ".join(f"{f['node']}：{f['reason']}" for f in failures),
                failures,
                "append",
                payloads,
            )

    def retry_write(self, error: WriteError) -> list[str]:
        """把上次没推成功的更新**只补推给失败的节点**，返回仍失败的节点。

        ★ 只推给失败的那些：已经跟上的节点它的 δ 已经是新的了，再推一次
        同一份更新会被它（**正确地**）拒掉 —— 那不是故障，是“重复投递”。

        :returns: 仍然失败的节点名（空列表 = 都补上了）
        """
        still: list[str] = []
        for nid, payload in error.payloads.items():
            try:
                self._post(nid, f"/node/{error.op}", payload)
            except TransportError:
                still.append(nid)
        return still

    def apply_update(
        self,
        *,
        delta_new,
        op_delta: UpdateDelta,
        witness: UpdateWitness,
        blobs: Mapping[str, Mapping[int, bytes]],
    ) -> None:
        """把一次修改通知**每一台**节点。

        与 :meth:`apply_append` 一样，任何一台没跟上就整次失败 ——
        「全网现在不一致」这件事必须让调用方知道，而不是当成成功。
        """
        failures: list[dict] = []
        payloads: dict[str, dict] = {}
        for nid in self.node_ids:
            payload = _update_payload(
                delta_new=delta_new,
                op_delta=op_delta,
                witness=witness,
                blobs=blobs.get(nid, {}),
            )
            try:
                self._post(nid, "/node/update", payload)
            except TransportError as exc:
                failures.append({"node": nid, "reason": str(exc)})
                payloads[nid] = payload

        if failures:
            raise WriteError(
                "有节点没能跟上这次修改（全网现在不一致）：\n  "
                + "\n  ".join(f"{f['node']}：{f['reason']}" for f in failures),
                failures,
                "update",
                payloads,
            )

    def drop(self, positions: Mapping[str, Sequence[int]]) -> None:
        """让被点名的节点**交出**某些位置（``del`` 的前置步骤）。

        与写路径不同：这一步**不改**全局摘要，所以失败时**不建补推现场** ——
        节点侧的交回是**幂等**的（不在手里的下标直接跳过），所以
        「修好那台，重来一次整次截断」就是正确的修法，比补推简单得多。
        错误里会直说这一句，免得有人以为要走补推。
        """
        failures: list[dict] = []
        for nid, want in positions.items():
            if not want:
                continue
            try:
                self._post(
                    nid, "/node/drop", {"positions": [int(i) for i in want]}
                )
            except TransportError as exc:
                failures.append({"node": nid, "reason": str(exc)})
        if failures:
            raise TransportError(
                "有节点没能交回待删区间（本次没有改动任何全局状态）：\n  "
                + "\n  ".join(f"{f['node']}：{f['reason']}" for f in failures)
                + "\n  交回是幂等的 —— 把那台修好之后重来一次整个截断即可，不需要补推"
            )

    def apply_delete(
        self,
        *,
        delta_new,
        op_delta: UpdateDelta,
        witness: UpdateWitness,
    ) -> None:
        """把一次**删除**通知**每一台**节点。

        任何一台没跟上就整次失败（与 :meth:`apply_append` / :meth:`apply_update`
        同一条纪律：``WriteError`` 里带上「哪几台」与「它们没收到的那份请求体」，
        供 ``retry_write`` 补推）。

        ★ 路由名就是 ``op``：``retry_write`` 拼的是 ``/node/{error.op}``，
        所以这里的 ``"delete"`` 恰好对上 ``/node/delete``。
        """
        failures: list[dict] = []
        payloads: dict[str, dict] = {}
        payload = _delete_payload(
            delta_new=delta_new, op_delta=op_delta, witness=witness
        )
        for nid in self.node_ids:
            try:
                self._post(nid, "/node/delete", payload)
            except TransportError as exc:
                failures.append({"node": nid, "reason": str(exc)})
                payloads[nid] = payload

        if failures:
            raise WriteError(
                "有节点没能跟上这次删除（全网现在不一致）：\n  "
                + "\n  ".join(f"{f['node']}：{f['reason']}" for f in failures),
                failures,
                "delete",
                payloads,
            )

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"HttpTransport({', '.join(self.base_urls.values())})"


# ---------------------------------------------------------------------------
# 序列化 —— 与 node_service.server 的解析端一一对应
# ---------------------------------------------------------------------------

def _update_payload(
    *,
    delta_new,
    op_delta: UpdateDelta,
    witness: UpdateWitness,
    blobs: Mapping[int, bytes],
) -> dict:
    """修改的线格式 —— 与 :func:`_append_payload` 故意长得像。

    差别只有两处：没有 ``assigned``（位置不再变化），没有 ``delta_old``
    （节点现在的摘要就是「改动前」，协调者说了不算）。
    """
    return {
        "delta_new": {
            "U": str(delta_new.U),
            "C": str(delta_new.C),
            "n": int(delta_new.n),
        },
        "op_delta": {
            "op": op_delta.op,
            "K": [int(x) for x in op_delta.K],
            "F_new": [str(x) for x in op_delta.F_new],
        },
        "witness": {
            "op": witness.op,
            "K": [int(x) for x in witness.K],
            "S_K": str(witness.S_K),
            "F_K": [str(x) for x in witness.F_K],
        },
        "blobs": {str(int(k)): v.hex() for k, v in blobs.items()},
    }


def _delete_payload(
    *,
    delta_new,
    op_delta: UpdateDelta,
    witness: UpdateWitness,
) -> dict:
    """删除的线格式 —— 与 :func:`_update_payload` 只差 ``Υ∆`` 里的 ``π_K``。

    ``del`` 的 :math:`\\Upsilon_\\Delta` 里**必须**带 :math:`\\pi_K`（它就是新摘要），
    因为 :math:`\\Lambda` 那一信道要用到被删位置的**旧值**（``F_K``）与 ``π_K``
    才能做 ``agg``（见 ``vds.updates._apply_del``）。
    没有 ``blobs``：这次没有任何新密文要发，反而不该把被删块的密文放到线上。
    """
    return {
        "delta_new": {
            "U": str(delta_new.U),
            "C": str(delta_new.C),
            "n": int(delta_new.n),
        },
        "op_delta": {
            "op": op_delta.op,
            "K": [int(x) for x in op_delta.K],
            "F_new": [str(x) for x in op_delta.F_new],
        },
        "witness": {
            "op": witness.op,
            "K": [int(x) for x in witness.K],
            "S_K": str(witness.S_K),
            "F_K": [str(x) for x in witness.F_K],
            "pi_K": None
            if witness.pi_K is None
            else {
                "S_I": str(witness.pi_K.S_I),
                "Lambda_I": str(witness.pi_K.Lambda_I),
                "I": [int(x) for x in witness.pi_K.I],
            },
        },
    }


def _append_payload(
    *,
    delta_old,
    delta_new,
    op_delta: UpdateDelta,
    witness: UpdateWitness,
    assigned: Sequence[int],
    blobs: Mapping[int, bytes],
) -> dict:
    return {
        "delta_old": {"U": str(delta_old.U), "C": str(delta_old.C), "n": int(delta_old.n)},
        "delta_new": {"U": str(delta_new.U), "C": str(delta_new.C), "n": int(delta_new.n)},
        "op_delta": {
            "op": op_delta.op,
            "K": [int(x) for x in op_delta.K],
            "F_new": [str(x) for x in op_delta.F_new],
        },
        "witness": {
            "op": witness.op,
            "K": [int(x) for x in witness.K],
            "S_K": str(witness.S_K),
            "F_K": [str(x) for x in witness.F_K],
        },
        "assigned": [int(x) for x in assigned],
        "blobs": {str(int(k)): v.hex() for k, v in blobs.items()},
    }
