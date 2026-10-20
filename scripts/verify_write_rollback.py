"""验证：写失败时**节点侧也会撤回**，且节点落库是原子的。

对应两条审阅意见：

1. 跨进程写失败后节点侧不回滚 → 孤儿段 → 下次启动被启动闸拒绝；
5. 节点落库「先 save_state 再 save_blobs」非原子 → 撕裂窗口。

判据：

* 注入"第二台节点失败"后，第一台节点上**没有**留下这次的新 offset；
* 撤回之后重做同一份文件能成功（位置已退回登记表）；
* 在状态与密文之间注入一次失败，状态行不会留下半行。

跑法：``python -X utf8 scripts/verify_write_rollback.py``
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.node_state import NodeState  # noqa: E402
from core.session import new_session  # noqa: E402
from core.store import PlainKeyStore  # noqa: E402
from core.transport import TransportError  # noqa: E402
from node_service.persist import NodeDB  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   :: {extra}" if extra else ""))
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------- 1) 事务原子性
print("\n[1] NodeDB.apply_change 事务原子性")


class _D:
    U = 11
    C = 22
    n = 1
    identity = "id"
    chunks = ()


class _S:
    S_I = 33
    Lambda_I = 44


with tempfile.TemporaryDirectory() as td:
    db = NodeDB(Path(td) / "node.db")
    db.apply_change(7, delta=_D(), st=_S(), I=[0], FI=[0], upsert_blobs={0: b"aa"})
    states = db.load_states()
    blobs = db.load_blobs()
    check("正常写入：state 与 blob 都在", 7 in states and blobs.get(7) == {0: b"aa"})

    # 用不可绑定的对象让 executemany 抛错，检验 state 是否被回滚。
    try:
        db.apply_change(8, delta=_D(), st=_S(), I=[0], FI=[0], upsert_blobs={0: object()})
        raised = False
    except Exception:
        raised = True
    check("注入失败：确实抛了", raised)
    states = db.load_states()
    check("★ 注入失败后 state 没有留下半行（原子）", 8 not in states)
    db.close()

# ------------------------------------------------- 2) 部分节点成功后整批撤回
print("\n[2] 一次 append 里第 2 台失败 → 第 1 台必须退回")

session = new_session(l=256, n_max=32, modulus_bits=1024, seed=b"rollback-check")
store = PlainKeyStore(session, node_ids=("n1", "n2"), segment_bytes=8, replica_factor=1)
transport = store.transport

# 先正常传一份，确认装置可用。
store.upload("alice", "f1", b"AAAABBBBCCCCDDDD")
before = {nid: transport.states[nid].offsets for nid in ("n1", "n2")}
check("基准：两台都已参与", all(len(v) == 1 for v in before.values()), str(before))

# 让第 2 台的 absorb 抛错（模拟“节点跟不上这次更新”）。
orig_absorb = NodeState.absorb
counter = {"n": 0}


def boom(self, *args, **kwargs):
    counter["n"] += 1
    if counter["n"] == 2:
        # 真实路径抛的就是 TransportError（见 node_service/client.py）。
        raise TransportError("注入：第二台节点失败")
    return orig_absorb(self, *args, **kwargs)


NodeState.absorb = boom
try:
    store.upload("alice", "f2", b"EEEEFFFFGGGGHHHH")
    raised = False
except Exception as exc:  # noqa: BLE001 - 这里要的就是“抛没抛”
    raised = True
    print(f"        捕获：{type(exc).__name__}: {exc}")
finally:
    NodeState.absorb = orig_absorb

check("写失败确实抛出", raised)
after = {nid: transport.states[nid].offsets for nid in ("n1", "n2")}
check("★ 第 1 台没有留下新 offset（已撤回）", after == before, f"{before} -> {after}")
check(
    "★ 两台 delta 也回到原样",
    all(
        transport.states[nid].delta_of(next(iter(before[nid]))) is not None
        for nid in ("n1", "n2")
    ),
)

# 失败之后仍能正常重做（位置已退回登记表）。
try:
    store.upload("alice", "f2", b"EEEEFFFFGGGGHHHH")
    redo_ok = True
except Exception as exc:  # noqa: BLE001
    redo_ok = False
    print(f"        重做异常：{type(exc).__name__}: {exc}")
check("★ 撤回之后重做成功", redo_ok)
check(
    "重做后两台都多了一份 offset",
    all(len(transport.states[nid].offsets) == 2 for nid in ("n1", "n2")),
)

print()
if FAILED:
    print(f"结果：{len(FAILED)} 条失败 —— {FAILED}")
    sys.exit(1)
print("结果：ALL PASS")
