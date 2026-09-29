"""VDS 系统核心层 —— 设计 B（全系统一条向量）。

与 ``svc`` / ``vds`` 的关系
--------------------------
``svc`` 是向量承诺本体（论文 §5.2），``vds`` 是"一个文件 = 一条向量"的
可验证分布式存储（§8.2）。本层在此之上做**设计 B** 的改造：

    全系统只有一条向量 V = (V_0, V_1, ...)，所有用户的所有文件
    都按上传顺序**追加**到这条向量的末尾。于是：

    * 全系统**只有一份**摘要 δ —— 任何用户都能验证任意位置；
    * 一个证据可以**横跨多个文件**（只要能覆盖那些下标）；
    * 跨文件聚合 = 同一条向量上的普通聚合，天然成立。

代价（必须知道）
----------------
1. **``n`` 是"全局已用位置数"**，不是文件长度。"文件"这个概念只存在于
   登记表（:mod:`core.registry`）里。
2. **任何一次上传都会推进 δ**（``n`` 变大 ⇒ ``e_[n]`` 变大 ⇒ 每个节点的
   ``S_I``、``Λ_I`` 都变），所以每次上传都要把新状态同步给**所有**节点。
   这正是论文 §8.2 两段式更新（``PushUpdate`` / ``ApplyUpdate``）的用武之地，
   本层直接复用 :func:`vds.updates.update_append`。
3. **删除只能删末尾**（``del`` 是截断），中间的文件只能标记删除。

三条安全红线（规划 §6.4）
-----------------------
* ``N``、``g`` 公开；**``p``、``q``、``φ(N)`` 绝不落库**；
* RNG 种子必须随机（``None``），否则知道种子的人能重跑出 ``p``、``q``；
* 日志与 API 响应不带生成过程的中间量。

分层
----
::

    VectorStore（协调者）──持有──> 全局向量、登记表、分片决策
          │
          └── NodeTransport ──> NodeState × k（每台存储节点的本地状态）
                  │                    └── 自己那段的密文
                  ├─ LocalTransport  同进程对象（单进程演示 / 测试）
                  └─ HttpTransport   独立进程 + 各自 SQLite（真分布式）

**协调者不持有任何节点状态，也不替节点算新状态。** 追加时它只广播
"发生了什么"（Δ 与 Υ_Δ）以及"你负责哪些新位置"，各节点自己算出来。
这是两段式更新的要点，也是 :mod:`core.transport` 这个抽象存在的意义。
"""

from __future__ import annotations

from .crypto import (
    DIGEST_BYTES,
    L,
    SEGMENT_BYTES,
    decrypt_segment,
    encrypt_segment,
    new_key,
    vector_element,
)
from .node_state import NodeRejected, NodeState
from .registry import BlockRef, BlockRegistry
from .session import (
    DEFAULT_L,
    DEFAULT_MODULUS_BITS,
    DEFAULT_N_MAX,
    GlobalSession,
    crs_from_dict,
    crs_to_dict,
    get_primegen,
    make_crs,
    new_session,
)
from .store import FileRecord, QueryResult, VectorStore
from .transport import LocalTransport, NodeTransport, TransportError

__all__ = [
    # session
    "GlobalSession",
    "new_session",
    "make_crs",
    "get_primegen",
    "crs_to_dict",
    "crs_from_dict",
    "DEFAULT_L",
    "DEFAULT_N_MAX",
    "DEFAULT_MODULUS_BITS",
    # registry
    "BlockRegistry",
    "BlockRef",
    # crypto
    "SEGMENT_BYTES",
    "DIGEST_BYTES",
    "L",
    "new_key",
    "encrypt_segment",
    "decrypt_segment",
    "vector_element",
    # store
    "VectorStore",
    "FileRecord",
    "QueryResult",
    # node
    "NodeState",
    "NodeRejected",
    "NodeTransport",
    "LocalTransport",
    "TransportError",
]
