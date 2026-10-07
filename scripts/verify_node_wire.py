"""跨进程（HTTP）端到端验证 —— **线格式不能丢字段**。

这是"去掉全局下标"改造的第 5 关，也是**唯一**一关真的走 HTTP 的。

为什么前面 4 关（svc 内核 / 会话层 / store 业务流 / 后端落库）全绿了还不够：

* 它们走的都是 :class:`~core.transport.LocalTransport` —— 递过去的是
  :class:`~vds.digest.Digest` **对象本身**，字段一个不少；
* 而跨进程走的是 ``client._delta_dict`` → JSON → ``server._delta_in``，
  **每多一个字段就多一处可能漏**。

本项目真踩过：身份素数改造完成后 221 条断言全过，一上真机
**每一次上传都 503**，报的却是

    有 4 台存储节点没跟上这次更新（node-1、node-2、node-3、node-4）

—— 因为线格式没带 ``identity``，节点退回旧路径查全局素数表，
于是 :math:`e_i` 全错，节点侧 :meth:`~core.node_state.NodeState.absorb`
抛「S_I 校验失败」。协调者那一侧看不到任何线索。

本脚本因此死盯三件事：

1. **线格式**（``_delta_dict`` / ``_delta_in``）必须带 ``identity``；
2. **跨进程真写一遍**：上传 / 改块 / 追加 / 截断 / 检索 / 自检；
3. **节点重启之后还能写** —— ``identity`` 也要落进节点自己的 ``node.db``，
   否则"重启后一切正常，下一次更新才炸"。

.. note::

   这里**不起端口、不动你正在跑的那套服务**：用一份同步 ASGI 传输把请求
   直接交给各节点的 ASGI 应用。该走的东西（线格式、鉴权头、状态码）
   一样不少，只是不经过 socket。数据目录建在 ``scripts/`` 下、用完即删。
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from core.session import new_session  # noqa: E402
from core.store import PlainKeyStore  # noqa: E402
from node_service import HttpTransport  # noqa: E402
from node_service.client import (  # noqa: E402
    _append_payload,
    _delta_dict,
    _delete_payload,
    _update_payload,
)
from node_service.persist import NodeDB  # noqa: E402
from node_service.server import (  # noqa: E402
    NODE_TOKEN_HEADER,
    _delta_in,
    create_node_app,
)
from vds.digest import Digest, make_identity  # noqa: E402
from vds.storage_node import UpdateDelta, UpdateWitness  # noqa: E402

FAILURES: list[str] = []

TOKEN = "wire-test-token"
NODES = ("node-1", "node-2", "node-3")
DATA_DIR = ROOT / "scripts" / "_tmp_node_wire"
SEG = 32
BLOCK = b"A" * SEG


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  PASS  " if cond else "  FAIL  ") + name
          + (("   :: " + extra) if extra else ""))
    if not cond:
        FAILURES.append(name)


def ok_verify(rep) -> bool:
    return bool(rep["ok"])


def section(t: str) -> None:
    print()
    print("=" * 72)
    print(t)
    print("=" * 72)


# ---------------------------------------------------------------------------
# 同步 ASGI 传输：不起端口、不动真服务
# ---------------------------------------------------------------------------


class AsgiTransport(httpx.BaseTransport):
    """把请求**直接**交给对应节点的 ASGI 应用（按 URL 主机名分派）。

    为什么要它：本脚本要打的正是 ``HttpTransport`` 那条真实线格式
    （序列化 → JSON → 反序列化 → 鉴权头 → 状态码），但不想占端口，
    也不想碰用户正在跑的那套服务。ASGI 直连把这条路上该有的东西全走到了，
    只是不经过 socket。

    :param apps: ``{节点名: ASGI 应用}``。**可变** —— 模拟"节点重启"时
        我们换掉字典里的应用，而 ``HttpTransport`` 手里那个 client 不用动。
    """

    def __init__(self, apps: Mapping[str, object]) -> None:
        self.apps = dict(apps)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        app = self.apps[request.url.host]
        body = request.content
        status = 500
        headers: list[tuple[bytes, bytes]] = []
        chunks: list[bytes] = []

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(msg):
            nonlocal status, headers
            if msg["type"] == "http.response.start":
                status = int(msg["status"])
                headers = list(msg.get("headers") or [])
            elif msg["type"] == "http.response.body":
                chunks.append(msg.get("body") or b"")

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": request.method,
            "headers": [(k.lower(), v) for k, v in request.headers.raw],
            "path": request.url.path,
            "raw_path": request.url.raw_path.split(b"?")[0],
            "query_string": request.url.query,
            "scheme": "http",
            "server": (request.url.host, request.url.port or 80),
            "client": ("testclient", 0),
            "root_path": "",
        }
        asyncio.run(app(scope, receive, send))
        return httpx.Response(
            status, headers=headers, content=b"".join(chunks), request=request
        )


# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------

if DATA_DIR.exists():
    shutil.rmtree(DATA_DIR)
DATA_DIR.mkdir(parents=True)

session = new_session(
    l=256, n_max=32, modulus_bits=1024, seed=b"node-wire-e2e"
)
CRS_DICT = {
    "N": str(session.crs.N),
    "g": str(session.crs.g),
    "l": int(session.crs.l),
    "n_max": int(session.n_max),
}


def build_apps() -> dict[str, object]:
    """每台节点一个应用；**同一个数据目录**上重建 = 这台机器重启了一次。"""
    return {
        nid: create_node_app(nid, DATA_DIR / nid, token=TOKEN) for nid in NODES
    }


apps = build_apps()
asgi = AsgiTransport(apps)
transport = HttpTransport(
    {nid: f"http://{nid}" for nid in NODES},
    token=TOKEN,
    client=httpx.Client(transport=asgi, timeout=120),
)
transport.set_crs(CRS_DICT)
store = PlainKeyStore(
    session, NODES, segment_bytes=SEG, transport=transport, replica_factor=1
)

F1 = ("alice", "wire-f1")
F2 = ("alice", "wire-f2")
ID1 = make_identity(*F1)
ID2 = make_identity(*F2)
PLAIN1 = BLOCK * 3
PLAIN2 = b"B" * SEG + b"C" * SEG


# ---------------------------------------------------------------------------
section("0. 线格式：identity 必须过线（这就是那次线上事故的根因）")

probe = Digest(U=11, C=22, n=3, offset=7, chunks=((7, 3),), identity=ID1)
wire = _delta_dict(probe)
check("★ _delta_dict 带 identity", wire.get("identity") == ID1, repr(wire.get("identity")))
check("_delta_dict 带 chunks", wire.get("chunks") == [[7, 3]], repr(wire.get("chunks")))
check("_delta_dict 带 offset / n", wire["offset"] == 7 and wire["n"] == 3)
round_trip = _delta_in(wire)
check("★ _delta_in 能读回 identity", round_trip.identity == ID1, repr(round_trip.identity))
check("_delta_in 读回 chunks", round_trip.chunks == ((7, 3),))
check("_delta_in 读回 offset / n", round_trip.offset == 7 and round_trip.n == 3)

# 老协调者（改造前）发的请求体里没有 identity —— 必须**退化成老坐标**而不是报错。
legacy_wire = {k: v for k, v in wire.items() if k != "identity"}
check("★ 老请求体（无 identity）退化成老坐标，不报错",
      _delta_in(legacy_wire).identity == "")

# 三个 payload 都走同一个助手 —— 逐个查一遍，免得将来有人只改了两处。
op_add = UpdateDelta("add", K=(0, 1), F_new=(1, 2))
wt_add = UpdateWitness("add", K=(0, 1), S_K=12345, F_K=(1, 2))
BODIES = {
    "append": lambda: _append_payload(
        delta_old=probe, delta_new=probe, op_delta=op_add,
        witness=wt_add, assigned=(0,), blobs={}),
    "update": lambda: _update_payload(
        delta_new=probe, op_delta=UpdateDelta("mod", K=(0,), F_new=(9,)),
        witness=UpdateWitness("mod", K=(0,), S_K=1, F_K=(9,)), blobs={}),
    "delete": lambda: _delete_payload(
        delta_new=probe, op_delta=UpdateDelta("del", K=(2,)),
        witness=UpdateWitness("del", K=(2,), S_K=1, F_K=(3,), pi_K=None)),
}
for _name, _mk in BODIES.items():
    body = _mk()
    deltas = [v for k, v in body.items() if k.startswith("delta_")]
    check(f"★ {_name} 线格式：每个 δ 都带 identity",
          bool(deltas) and all(x.get("identity") == ID1 for x in deltas),
          repr([x.get("identity") for x in deltas]))
    check(f"★ {_name} 线格式能被 _delta_in 读回 identity",
          all(_delta_in(x).identity == ID1 for x in deltas))


# ---------------------------------------------------------------------------
section("1. 跨进程上传（线上就是卡在这一步）")

rec1 = store.upload(*F1, PLAIN1)  # noqa: F841 - 只看它成不成功
check("★ 跨进程上传成功（不再 503）", store.delta_of(*F1).n == 3,
      f"n={store.delta_of(*F1).n}")
check("f1 的摘要带身份", store.delta_of(*F1).identity == ID1)
check("读回明文与原文一致", store.read(*F1) == PLAIN1)
check("cipher_of 的承诺验证通过", ok_verify(store.cipher_of(*F1)["verify"]))
check("★ 各节点的视图都合法", store.check() == [], str(store.check()[:2]))


# ---------------------------------------------------------------------------
section("2. 改块 / 追加 / 第二份文件 / 跨文件检索")

store.modify(*F1, 1, b"X" * SEG)
check("改块后 n 不变", store.delta_of(*F1).n == 3)
check("改块后读回正确", store.read(*F1) == BLOCK + b"X" * SEG + BLOCK)
check("改块后身份仍在", store.delta_of(*F1).identity == ID1)

store.append(*F1, BLOCK * 2)
check("追加后 n = 5", store.delta_of(*F1).n == 5, f"n={store.delta_of(*F1).n}")
check("追加后读回正确", store.read(*F1) == BLOCK + b"X" * SEG + BLOCK * 3)
check("★ 追加后各节点自检无问题", store.check() == [], str(store.check()[:2]))

store.upload(*F2, PLAIN2)
check("第二份文件的身份独立", store.delta_of(*F2).identity == ID2)
check("★ 两份文件的身份不同（各配各的素数）",
      store.delta_of(*F1).identity != store.delta_of(*F2).identity)
check("f2 读回正确", store.read(*F2) == PLAIN2)

pos1 = tuple(store.registry.positions_of(*F1))
pos2 = tuple(store.registry.positions_of(*F2))
check("两份文件位置不重叠", not (set(pos1) & set(pos2)))
mixed = list(pos1[:2]) + list(pos2[:2])
qr = store.query(mixed)
check("★ 跨文件检索 + 聚合验证通过", bool(qr.ok), qr.report.message)


# ---------------------------------------------------------------------------
section("3. ★ 节点重启之后**还要能继续写**（identity 必须落进 node.db）")

with sqlite3.connect(DATA_DIR / "node-1" / "node.db") as _c:
    cols = {r[1] for r in _c.execute("PRAGMA table_info(state)")}
check("节点库 state 表有 identity 列", "identity" in cols, str(sorted(cols)))
check("节点库 state 表有 chunks_json 列", "chunks_json" in cols)
con1 = sqlite3.connect(DATA_DIR / "node-1" / "node.db")
ident_rows = sorted(r[0] for r in con1.execute("SELECT identity FROM state"))
con1.close()
check("★ 落库的 identity 就是这两份文件的身份",
      ident_rows == sorted([ID1, ID2]), repr(ident_rows))

# 关掉旧连接、在**同一个数据目录**上重建应用 = 这台机器重启了一次
for app in apps.values():
    app.state.runtime.db.close()
fresh = build_apps()
asgi.apps.update(fresh)   # ★ 换的是**传输层手里**那份字典，不是本地变量
apps.update(fresh)
check("重启后节点仍认得公开参数（它存在节点自己的库里）",
      all(a.state.runtime.session is not None for a in apps.values()))
check("★ 从库里恢复出来的 δ 带 identity",
      apps["node-1"].state.runtime.state.delta_of(0).identity == ID1,
      repr(apps["node-1"].state.runtime.state.delta_of(0).identity))

report = transport.report()
check("重启后各节点仍能报告自己的视图",
      all(r.get("valid") for r in report), str([r.get("valid") for r in report]))

store.modify(*F1, 0, b"Y" * SEG)
check("★ 重启后改块成功（identity 没丢）", store.read(*F1) == b"Y" * SEG + b"X" * SEG + BLOCK * 3)
store.append(*F1, BLOCK)
check("★ 重启后追加成功", store.delta_of(*F1).n == 6, f"n={store.delta_of(*F1).n}")
check("★ 重启后各节点自检无问题", store.check() == [], str(store.check()[:2]))
check("重启后 f2 仍然读得出来", store.read(*F2) == PLAIN2)


# ---------------------------------------------------------------------------
section("4. 截断 / 老库自动迁移")

# F2 是最后上传的，尾部那几块属于它 —— 只有"排在向量末尾"的文件删得动尾巴。
K, _rec = store.truncate(*F2, 1)
check("截断退回被删的下标", tuple(K) == (pos2[-1],), repr(K))
check("截断后 f2 的 n = 1", store.delta_of(*F2).n == 1, f"n={store.delta_of(*F2).n}")
check("截断后 f2 读回正确", store.read(*F2) == PLAIN2[:SEG])
check("★ 截断后各节点自检无问题", store.check() == [], str(store.check()[:2]))

# 老库（state 表没有那两列）必须能被自动补列，而不是整台机器起不来。
old_dir = DATA_DIR / "node-old"
old_dir.mkdir(parents=True, exist_ok=True)
con = sqlite3.connect(old_dir / "node.db")
con.executescript(
    """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE state (
        offset    INTEGER PRIMARY KEY,
        delta_U   TEXT NOT NULL,
        delta_C   TEXT NOT NULL,
        delta_n   INTEGER NOT NULL,
        S_I       TEXT NOT NULL,
        Lambda_I  TEXT NOT NULL,
        I_json    TEXT NOT NULL,
        FI_json   TEXT NOT NULL
    );
    CREATE TABLE blobs (
        offset      INTEGER NOT NULL,
        local_index INTEGER NOT NULL,
        ciphertext  BLOB NOT NULL,
        PRIMARY KEY (offset, local_index)
    );
    INSERT INTO state VALUES (0, '1', '2', 3, '4', '5', '[]', '[]');
    """
)
con.commit()
con.close()
old_db = NodeDB(old_dir / "node.db")
cols_old = {r[1] for r in old_db._conn.execute("PRAGMA table_info(state)")}
check("★ 老库被自动补上 identity 列", "identity" in cols_old, str(sorted(cols_old)))
check("★ 老库被自动补上 chunks_json 列", "chunks_json" in cols_old)
lost = old_db.load_states()
check("老行读回来是**老坐标**（identity 空）而不是报错",
      lost[0]["identity"] == "" and lost[0]["chunks"] == [], repr(lost[0].get("identity")))
check("老行其它字段照读不误", lost[0]["delta"] == ("1", "2", 3))
old_db.close()


# ---------------------------------------------------------------------------
print()
print("=" * 72)
if FAILURES:
    print(f"失败 {len(FAILURES)} 项：")
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("全部通过 ✓")
